# SPDX-License-Identifier: AGPL-3.0-or-later
"""Assemble the read-only Zendure MQTT telemetry runtime from EMS config.

Bridges validated ``config.json`` entries to the runtime telemetry service. It
is read-only by construction: it selects telemetry-only devices, filters each
device's snapshots to its own broker, and exposes credential-free status. It
never publishes or writes. It builds one broker service per configured broker
profile, but a broker also used by a control device borrows that control
runtime's service (``shared_services``) so the connection and snapshot cache stay
single across telemetry and control.

Each MQTT device references exactly one broker profile via ``mqtt.broker_ref``;
there is no fallback and no cross-broker satisfaction. Old single-broker configs
map to an implicit ``default`` broker so they keep working unchanged.
"""

import json
import time
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ems.zendure_mqtt.config_entries import (
    DEFAULT_BROKER_REF,
    RESERVED_MQTT_BROKER_REFS,
    is_control_zendure_mqtt_device_config,
    is_zendure_mqtt_device_config,
    legacy_default_broker_present,
    zendure_cloud_device_subscriptions,
    validate_zendure_mqtt_device_config,
    zendure_mqtt_broker_ref,
    zendure_mqtt_device_identifier,
    zendure_mqtt_source,
)
from ems.zendure_mqtt.service import (
    SNAPSHOT_STALE,
    SNAPSHOT_UNSEEN,
    SOURCE_LOCAL_MQTT,
    SOURCE_ZENDURE_CLOUD_MQTT,
    ZendureMqttConfigError,
    ZendureMqttRuntimeConfig,
    ZendureMqttService,
    classify_snapshot,
)
from ems.mqtt_credentials import (
    MqttCredentialError,
    resolve_mqtt_cloud_profile_credentials,
    resolve_mqtt_profile_credentials,
)

# Cap the metric-name list so status stays compact for CLI/Admin/Dashboard use.
_MAX_METRIC_NAMES = 16


@dataclass(frozen=True)
class ZendureMqttTelemetryDevice:
    """A validated telemetry-only device selected for the runtime."""

    identifier: str | None
    name: str
    topic_family: str | None
    broker_ref: str = DEFAULT_BROKER_REF
    source: str | None = None


@dataclass(frozen=True)
class InvalidZendureMqttDevice:
    """A rejected telemetry-only entry; ``issues`` never carry secret values."""

    index: int
    name: str
    issues: tuple[dict[str, Any], ...]
    broker_ref: str = DEFAULT_BROKER_REF


def load_zendure_mqtt_runtime_config(raw) -> tuple[ZendureMqttRuntimeConfig, str | None]:
    """Parse the top-level broker block into ``(default_config, error_message)``.

    Kept for backward compatibility with the single-broker shape and offline
    diagnostics. A configuration problem never raises and never carries
    credentials: it yields a disabled config plus a sanitized message.
    """

    if raw is None:
        return ZendureMqttRuntimeConfig(), None
    if not isinstance(raw, dict):
        return ZendureMqttRuntimeConfig(), "zendure_mqtt config must be an object"
    try:
        return ZendureMqttRuntimeConfig.from_dict(raw), None
    except ZendureMqttConfigError as exc:
        return ZendureMqttRuntimeConfig(), str(exc)


def _safe_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_zendure_mqtt_broker_configs(
    raw, *, credential_resolver=None,
) -> tuple[dict[str, ZendureMqttRuntimeConfig], dict[str, str], float]:
    """Parse ``zendure_mqtt`` into named broker profiles.

    Returns ``(brokers, errors, stale_after_seconds)`` where ``brokers`` maps
    ``broker_ref`` to a profile config and ``errors`` maps a ``broker_ref`` to a
    sanitized message. The telemetry feature is always on: the legacy top-level
    ``enabled`` key is ignored (any type, any value) and a profile is active
    exactly when it has a broker host; per-profile ``enabled`` flags still
    apply. Old single-broker configs synthesize a ``default`` broker from the
    top-level fields; new configs add each entry under ``brokers``. Nothing here
    raises and no credential ever reaches ``errors``.
    """

    if raw is None:
        return {}, {}, 60.0
    if not isinstance(raw, dict):
        return {}, {DEFAULT_BROKER_REF: "zendure_mqtt config must be an object"}, 60.0

    from ems.config import require_json_bool

    stale_after = _safe_float(raw.get("stale_after_seconds"), 60.0)

    brokers_block = raw.get("brokers")
    has_brokers = isinstance(brokers_block, dict) and bool(brokers_block)

    brokers: dict[str, ZendureMqttRuntimeConfig] = {}
    errors: dict[str, str] = {}

    def with_credentials(profile, source, *, enabled):
        # A canonically disabled profile is never connected, so its credential
        # secret is not resolved: a disabled broker with a missing credentials_ref
        # must not raise.
        if not enabled:
            return profile
        if source == SOURCE_ZENDURE_CLOUD_MQTT:
            # A Zendure cloud broker resolves its Core-owned runtime credential
            # record (username/password/client_id/app_key) so EMS starts with real
            # in-memory cloud connection material and never needs Admin. A legacy
            # config that stored only the API-token ref has no such record: leave
            # the profile as-is so the broker reports broker_auth_missing rather
            # than aborting the whole block.
            if not (isinstance(profile, dict) and str(profile.get("credentials_ref") or "").strip()):
                return profile
            try:
                return resolve_mqtt_cloud_profile_credentials(profile, credential_resolver)
            except MqttCredentialError:
                return profile
        return resolve_mqtt_profile_credentials(profile, credential_resolver)

    def _has_host(profile) -> bool:
        host = profile.get("host")
        return isinstance(host, str) and bool(host.strip())

    top_source = raw.get("source") or SOURCE_LOCAL_MQTT
    if legacy_default_broker_present(raw):
        try:
            brokers[DEFAULT_BROKER_REF] = ZendureMqttRuntimeConfig.from_dict(
                with_credentials(raw, top_source, enabled=_has_host(raw)),
                broker_ref=DEFAULT_BROKER_REF,
                source=top_source,
                stale_after_seconds=stale_after,
            )
        except (ZendureMqttConfigError, MqttCredentialError) as exc:
            errors[DEFAULT_BROKER_REF] = str(exc)

    if has_brokers:
        for ref, val in brokers_block.items():
            ref = str(ref)
            if ref in RESERVED_MQTT_BROKER_REFS:
                # Safety net for an unvalidated config: never let a named profile
                # overwrite the implicit legacy ``default`` broker already built
                # above. The reference is reserved; report it, keep the legacy one.
                errors[ref] = (
                    f"broker ref '{ref}' is reserved for the implicit top-level "
                    "broker"
                )
                continue
            if not isinstance(val, dict):
                errors[ref] = "broker profile must be an object"
                continue
            # The per-profile flag stays strict: a non-boolean ``enabled`` is
            # always surfaced as an error rather than coerced via truthiness.
            try:
                profile_enabled = require_json_bool(
                    val.get("enabled", True), "enabled"
                )
            except ValueError as exc:
                errors[ref] = str(exc)
                continue
            try:
                brokers[ref] = ZendureMqttRuntimeConfig.from_dict(
                    with_credentials(
                        val,
                        val.get("source"),
                        enabled=profile_enabled and _has_host(val),
                    ),
                    broker_ref=ref,
                    source=val.get("source"),
                    enabled=profile_enabled,
                    stale_after_seconds=stale_after,
                )
            except (ZendureMqttConfigError, MqttCredentialError) as exc:
                errors[ref] = str(exc)

    return brokers, errors, stale_after


def classify_zendure_mqtt_devices(
    devices,
    *,
    known_broker_refs: Any = None,
    brokers_defined: bool = False,
) -> tuple[list[ZendureMqttTelemetryDevice], list[InvalidZendureMqttDevice]]:
    """Split ``devices[]`` into valid telemetry-only entries and invalid ones."""

    valid: list[ZendureMqttTelemetryDevice] = []
    invalid: list[InvalidZendureMqttDevice] = []
    if not isinstance(devices, list):
        return valid, invalid
    for index, item in enumerate(devices):
        if not is_zendure_mqtt_device_config(item):
            continue
        # Control (write-capable) entries are handled by the control path, not
        # the read-only telemetry runtime.
        if is_control_zendure_mqtt_device_config(item):
            continue
        name = item.get("name") if isinstance(item.get("name"), str) else f"device-{index}"
        broker_ref = zendure_mqtt_broker_ref(item)
        issues = [
            issue
            for issue in validate_zendure_mqtt_device_config(
                item,
                known_broker_refs=known_broker_refs,
                brokers_defined=brokers_defined,
            )
            if issue.get("severity") == "error"
        ]
        if issues:
            invalid.append(
                InvalidZendureMqttDevice(index, name, tuple(issues), broker_ref)
            )
            continue
        mqtt = item.get("mqtt")
        topic_family = mqtt.get("topic_family") if isinstance(mqtt, dict) else None
        valid.append(
            ZendureMqttTelemetryDevice(
                identifier=zendure_mqtt_device_identifier(item),
                name=name,
                topic_family=topic_family if isinstance(topic_family, str) else None,
                broker_ref=broker_ref,
                source=zendure_mqtt_source(item),
            )
        )
    return valid, invalid


def _match_snapshot(identifier, snapshots):
    if not identifier:
        return None
    snap = snapshots.get(identifier)
    if snap is not None:
        return snap
    for snap in snapshots.values():
        if getattr(snap, "serial_number", None) == identifier:
            return snap
    return None


def _iso_last_seen(snapshot):
    epoch = getattr(snapshot, "last_seen_epoch", None)
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone().isoformat()


def _sorted_names(values):
    return sorted(str(value) for value in values) if values else []


def _summarize_valid_device(device, snapshots, now_monotonic, stale_after_seconds):
    summary = {
        "name": device.name,
        "identifier": device.identifier,
        "broker_ref": device.broker_ref,
        "source": device.source,
        "topic_family": device.topic_family,
        "write_output_limit": False,
    }
    snapshot = _match_snapshot(device.identifier, snapshots)
    status_verdict = classify_snapshot(
        snapshot, stale_after_seconds, now_monotonic=now_monotonic
    )
    if status_verdict.state == SNAPSHOT_UNSEEN:
        summary.update(
            status="unseen",
            last_seen=None,
            age_seconds=None,
            metric_count=0,
            metrics=[],
            capabilities=[],
        )
        return summary
    metric_names = _sorted_names(getattr(snapshot, "metrics", None) or {})
    age = status_verdict.age_seconds
    status = "stale" if status_verdict.state == SNAPSHOT_STALE else "online"
    summary.update(
        status=status,
        last_seen=_iso_last_seen(snapshot),
        age_seconds=round(age, 3) if age is not None else None,
        metric_count=len(metric_names),
        metrics=metric_names[:_MAX_METRIC_NAMES],
        capabilities=_sorted_names(getattr(snapshot, "capabilities", None)),
    )
    return summary


def _summarize_invalid_device(invalid):
    codes = sorted(
        {
            issue["code"]
            for issue in invalid.issues
            if isinstance(issue, dict) and issue.get("code")
        }
    )
    return {
        "name": invalid.name,
        "identifier": None,
        "broker_ref": getattr(invalid, "broker_ref", DEFAULT_BROKER_REF),
        "source": None,
        "topic_family": None,
        "status": "invalid",
        "last_seen": None,
        "age_seconds": None,
        "metric_count": 0,
        "metrics": [],
        "capabilities": [],
        "issues": codes,
        "write_output_limit": False,
    }


def summarize_zendure_mqtt_devices(
    devices,
    invalid_devices,
    snapshots,
    *,
    now_monotonic,
    stale_after_seconds,
):
    """Build credential-free per-device snapshot summaries.

    Valid devices resolve to ``unseen``/``online``/``stale`` from snapshot age;
    invalid config entries surface as ``invalid`` with their issue codes only.
    """

    summaries = [
        _summarize_valid_device(device, snapshots, now_monotonic, stale_after_seconds)
        for device in devices
    ]
    summaries.extend(_summarize_invalid_device(invalid) for invalid in invalid_devices)
    return summaries


def _cloud_runtime_ready(config) -> bool:
    """True when a cloud broker has every runtime credential field it needs.

    Cloud telemetry is subscribed as ``<app_key>/#``, so a record missing or
    blank in any of username/password/client_id/app_key would connect but never
    subscribe. The per-source completeness contract lives in the EMS Core, so
    this reuses it rather than re-listing the four cloud fields.
    """

    from ems.mqtt_credentials import missing_mqtt_credential_fields

    return not missing_mqtt_credential_fields(
        config, source=SOURCE_ZENDURE_CLOUD_MQTT
    )


class _BrokerRuntime:
    """Owns one read-only telemetry service scoped to a single broker profile.

    ``snapshots`` are filtered to the broker's configured device identifiers so a
    device can only ever be satisfied by its own broker's telemetry. There is no
    publish/write path.
    """

    def __init__(
        self,
        config,
        devices,
        *,
        service=None,
        service_factory=None,
        owns_service=True,
    ):
        self._config = config
        self._devices = tuple(devices)
        self._stale_after_seconds = config.stale_after_seconds
        self._identifiers = frozenset(
            device.identifier for device in self._devices if device.identifier
        )
        factory = service_factory or ZendureMqttService
        self._service = service if service is not None else factory(config)
        # A borrowed service (shared with the control runtime) is owned by that
        # runtime: this telemetry broker only reads its snapshots and must not
        # start or stop it, so the shared connection lifecycle stays single.
        self._owns_service = owns_service

    @property
    def broker_ref(self) -> str:
        return self._config.broker_ref

    @property
    def source(self) -> str | None:
        return self._config.source

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def configured(self) -> bool:
        return bool(self._config.host)

    @property
    def running(self) -> bool:
        return bool(getattr(self._service, "running", False))

    @property
    def connected(self) -> bool:
        return bool(getattr(self._service, "connected", False))

    @property
    def device_count(self) -> int:
        return len(self._devices)

    def start(self) -> None:
        # A borrowed service is started by its owning control runtime; a broker
        # with no configured devices is never started either — it would only open
        # a connection with nothing to observe. An unusable profile (disabled,
        # incomplete, or a cloud broker whose credential cannot be resolved) is
        # never started so it cannot dial the broker anonymously or sit in a
        # reconnect loop; the sanitized issue stays visible in status instead.
        if self._owns_service and self._devices and self.profile_issue() is None:
            self._service.start()

    def stop(self) -> None:
        if self._owns_service:
            self._service.stop()

    def snapshots(self) -> dict:
        snapshots = self._service.snapshots()
        if not self._identifiers:
            return snapshots
        return {
            key: snap
            for key, snap in snapshots.items()
            if key in self._identifiers
            or getattr(snap, "serial_number", None) in self._identifiers
        }

    def device_summaries(self, now_monotonic, invalid_devices=()):
        return summarize_zendure_mqtt_devices(
            self._devices,
            invalid_devices,
            self.snapshots(),
            now_monotonic=now_monotonic,
            stale_after_seconds=self._stale_after_seconds,
        )

    def endpoint(self) -> str | None:
        host = self._config.host
        return f"{host}:{self._config.port}" if host else None

    def subscription_count(self) -> int:
        if not self._config.host:
            return 0
        try:
            return len(self._config.client_config().resolved_subscriptions())
        except ZendureMqttConfigError:
            return 0

    def _last_error(self):
        status_fn = getattr(self._service, "status", None)
        if callable(status_fn):
            return status_fn().get("last_error")
        return None

    def profile_issue(self) -> str | None:
        """Sanitized broker-profile problem code, or ``None`` when usable.

        A broker that carries devices but has no endpoint, is disabled via its
        per-profile flag, or is a cloud broker without any credential material
        cannot serve telemetry. The missing-host check comes first: a host-less
        profile is incomplete, not disabled, because the feature itself has no
        opt-out. Only a stable code leaves here; no host or credential is
        exposed.
        """

        if self.device_count == 0:
            return None
        if not self._config.host:
            return "broker_profile_incomplete"
        if not self.enabled:
            return "broker_profile_disabled"
        if self.source == SOURCE_ZENDURE_CLOUD_MQTT and not _cloud_runtime_ready(
            self._config
        ):
            return "broker_auth_missing"
        return None

    def summary(self) -> dict:
        return {
            "broker_ref": self.broker_ref,
            "source": self.source,
            "endpoint": self.endpoint(),
            "enabled": self.enabled,
            "running": self.running,
            "connected": self.connected,
            "device_count": self.device_count,
            "snapshot_count": len(self.snapshots()),
            "issue": self.profile_issue(),
            "last_error": self._last_error(),
        }


class ZendureMqttTelemetryRuntime:
    """Owns one read-only telemetry service per configured broker profile.

    ``start``/``stop`` fan out to the per-broker services and stay idempotent.
    ``snapshots`` are scoped per broker so a device is never satisfied by the
    wrong broker, and ``status`` is credential-free. There is no publish/write
    path.
    """

    def __init__(
        self,
        runtime_config: ZendureMqttRuntimeConfig | None = None,
        devices=(),
        *,
        invalid_devices=(),
        config_error: str | None = None,
        service=None,
        service_factory=None,
        broker_runtimes=None,
        stale_after_seconds: float | None = None,
    ):
        self._invalid_devices = tuple(invalid_devices)
        self._config_error = config_error
        if broker_runtimes is not None:
            self._brokers = list(broker_runtimes)
            self._stale_after_seconds = (
                stale_after_seconds if stale_after_seconds is not None else 60.0
            )
        else:
            config = runtime_config or ZendureMqttRuntimeConfig()
            self._brokers = [
                _BrokerRuntime(
                    config,
                    devices,
                    service=service,
                    service_factory=service_factory,
                )
            ]
            self._stale_after_seconds = config.stale_after_seconds

    @property
    def enabled(self) -> bool:
        return any(broker.enabled for broker in self._brokers)

    @property
    def broker_configured(self) -> bool:
        return any(broker.configured for broker in self._brokers)

    @property
    def broker_count(self) -> int:
        return len(self._brokers)

    @property
    def configured_device_count(self) -> int:
        return sum(broker.device_count for broker in self._brokers)

    @property
    def invalid_device_count(self) -> int:
        return len(self._invalid_devices)

    def start(self) -> None:
        for broker in self._brokers:
            broker.start()

    def stop(self) -> None:
        for broker in self._brokers:
            broker.stop()

    def snapshots(self) -> dict:
        merged: dict = {}
        for broker in self._brokers:
            merged.update(broker.snapshots())
        return merged

    def device_summaries(self, *, now_monotonic=None) -> list:
        """Per-device snapshot summaries (unseen/online/stale/invalid)."""

        if now_monotonic is None:
            now_monotonic = time.monotonic()
        summaries: list = []
        for broker in self._brokers:
            summaries.extend(broker.device_summaries(now_monotonic))
        summaries.extend(
            _summarize_invalid_device(invalid) for invalid in self._invalid_devices
        )
        return summaries

    def _primary_endpoint(self) -> str | None:
        for broker in self._brokers:
            if broker.broker_ref == DEFAULT_BROKER_REF and broker.endpoint():
                return broker.endpoint()
        for broker in self._brokers:
            if broker.endpoint():
                return broker.endpoint()
        return None

    def _snapshot_count(self) -> int:
        return sum(len(broker.snapshots()) for broker in self._brokers)

    def status(self) -> dict:
        """Credential-free status summary safe to log or surface."""

        status = {
            "enabled": self.enabled,
            "running": any(broker.running for broker in self._brokers),
            "connected": any(broker.connected for broker in self._brokers),
            "configured_device_count": self.configured_device_count,
            "invalid_device_count": self.invalid_device_count,
            "broker_configured": self.broker_configured,
            "broker_count": self.broker_count,
            "endpoint": self._primary_endpoint(),
            "stale_after_seconds": self._stale_after_seconds,
            "subscription_count": sum(
                broker.subscription_count() for broker in self._brokers
            ),
            "snapshot_count": self._snapshot_count(),
            "brokers": [broker.summary() for broker in self._brokers],
            "devices": self.device_summaries(),
        }
        if self._config_error:
            status["config_error"] = self._config_error
        return status

    def write_status_file(self, path, *, control_status=None) -> bool:
        """Persist a credential-free status snapshot for out-of-process readers.

        Admin prefers this live snapshot over a config-derived fallback. Writing
        is best-effort and atomic; it never publishes to the broker and any I/O
        error is swallowed so status persistence can't disturb the runtime.
        ``control_status`` (from the MQTT control runtime) is merged under a
        ``control`` block so control devices appear in runtime status too.
        """

        status = self.status()
        if control_status is not None:
            status["control"] = control_status
        payload = {"written_at": time.time(), "status": status}
        try:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(target)
        except OSError:
            return False
        return True


def _include_broker(config, device_count, explicit_refs) -> bool:
    # A broker appears in status when it carries devices or was explicitly
    # declared under ``zendure_mqtt.brokers``; a synthesized empty default is
    # hidden so ``broker_count`` reflects the brokers actually in use.
    return device_count > 0 or config.broker_ref in explicit_refs


def build_zendure_mqtt_runtime(
    config,
    *,
    service=None,
    service_factory=None,
    shared_services=None,
    credential_resolver=None,
) -> ZendureMqttTelemetryRuntime:
    """Build a read-only telemetry runtime from a full EMS config dict.

    ``shared_services`` maps ``broker_ref`` to an already-built control service
    (owned by the control runtime). When a broker matches one, the telemetry
    runtime borrows that service instead of opening a second connection, so a
    broker shared with control keeps a single connection and snapshot cache. A
    borrowed service is never started or stopped by the telemetry runtime.
    """

    config = config if isinstance(config, dict) else {}
    raw = config.get("zendure_mqtt")
    brokers, errors, stale_after = load_zendure_mqtt_broker_configs(
        raw, credential_resolver=credential_resolver
    )
    explicit_refs = set()
    if isinstance(raw, dict) and isinstance(raw.get("brokers"), dict):
        explicit_refs = {str(ref) for ref in raw["brokers"].keys()}
    brokers_defined = bool(explicit_refs)

    known_refs = set(brokers) | {DEFAULT_BROKER_REF}
    valid, invalid = classify_zendure_mqtt_devices(
        config.get("devices"),
        known_broker_refs=known_refs,
        brokers_defined=brokers_defined,
    )

    devices_by_ref: dict[str, list] = {}
    for device in valid:
        devices_by_ref.setdefault(device.broker_ref, []).append(device)

    # A device may reference the implicit default broker even when no top-level
    # broker block exists; synthesize a disabled default so it stays configured.
    if DEFAULT_BROKER_REF in devices_by_ref and DEFAULT_BROKER_REF not in brokers:
        brokers[DEFAULT_BROKER_REF] = ZendureMqttRuntimeConfig(
            broker_ref=DEFAULT_BROKER_REF,
            source=SOURCE_LOCAL_MQTT,
            stale_after_seconds=stale_after,
        )

    shared = shared_services if isinstance(shared_services, dict) else {}
    broker_runtimes = []
    for ref, broker_config in brokers.items():
        broker_devices = devices_by_ref.get(ref, [])
        if not _include_broker(broker_config, len(broker_devices), explicit_refs):
            continue
        borrowed = shared.get(ref)
        if borrowed is not None:
            broker_runtimes.append(
                _BrokerRuntime(
                    broker_config,
                    broker_devices,
                    service=borrowed,
                    owns_service=False,
                )
            )
            continue
        broker_service = service if (service is not None and len(brokers) == 1) else None
        # An owned cloud broker mirrors the control path: ACL-scoped cloud
        # sessions only deliver per-device trees, never the broad local
        # wildcards. An explicit profile subscription list always wins.
        if (
            broker_config.source == SOURCE_ZENDURE_CLOUD_MQTT
            and broker_config.subscriptions is None
        ):
            derived = zendure_cloud_device_subscriptions(config.get("devices"), ref)
            if derived:
                broker_config = dataclasses.replace(
                    broker_config, subscriptions=derived
                )
        broker_runtimes.append(
            _BrokerRuntime(
                broker_config,
                broker_devices,
                service=broker_service,
                service_factory=service_factory,
            )
        )

    config_error = "; ".join(f"{ref}: {msg}" for ref, msg in sorted(errors.items())) or None

    return ZendureMqttTelemetryRuntime(
        broker_runtimes=broker_runtimes,
        invalid_devices=invalid,
        config_error=config_error,
        stale_after_seconds=stale_after,
    )
