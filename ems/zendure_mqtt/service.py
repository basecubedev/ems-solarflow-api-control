# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only runtime telemetry service wrapping the Zendure MQTT read client.

Owns a single ``ZendureMqttReadClient``, starts/stops it idempotently and
exposes the latest snapshots plus lightweight status. Read-only by construction:
it offers no publish/write path, and status/errors never carry credentials. A
disabled config never connects. The write-capable ``ZendureMqttControlService``
subclass adds the gated publish path for control devices.
"""

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

# The paho-backed read client is imported lazily (factory only): the Admin
# container ships only the parsing/status subset of this package and must be
# able to import service/runtime and build config-derived status without
# ems.zendure_mqtt.client or paho-mqtt being present. The client-error class
# lives in the paho-free config module so its identity is stable here.
from ems.zendure_mqtt.config import ZendureMqttClientConfig, ZendureMqttClientError

logger = logging.getLogger(__name__)

# Cooldown between failed broker connect attempts. start() may be called every
# control-loop cycle to self-heal a broker that was down at boot; the cooldown
# keeps that retry from turning into a blocking connect attempt per cycle.
START_RETRY_COOLDOWN_SECONDS = 30.0

# Freshness states shared by telemetry status and the control read path.
SNAPSHOT_FRESH = "fresh"
SNAPSHOT_STALE = "stale"
SNAPSHOT_UNSEEN = "unseen"


@dataclass(frozen=True)
class SnapshotStatus:
    """Freshness verdict for one device snapshot.

    ``fresh`` snapshots may drive control; ``stale``/``unseen`` must be treated
    as an unavailable read so the controller never acts on disconnected devices.
    """

    snapshot: object | None
    state: str
    age_seconds: float | None

    @property
    def is_fresh(self) -> bool:
        return self.state == SNAPSHOT_FRESH


def snapshot_age_seconds(snapshot, now_monotonic) -> float | None:
    last = getattr(snapshot, "last_seen_monotonic", None)
    if last is None or now_monotonic is None:
        return None
    return max(0.0, now_monotonic - last)


def classify_snapshot(snapshot, stale_after_seconds, *, now_monotonic) -> SnapshotStatus:
    """Classify a snapshot as fresh/stale/unseen from its last-seen age."""

    if snapshot is None:
        return SnapshotStatus(None, SNAPSHOT_UNSEEN, None)
    age = snapshot_age_seconds(snapshot, now_monotonic)
    if age is not None and stale_after_seconds and age > stale_after_seconds:
        return SnapshotStatus(snapshot, SNAPSHOT_STALE, age)
    return SnapshotStatus(snapshot, SNAPSHOT_FRESH, age)

# A bare ``#`` would vacuum an entire shared broker; it is never accepted as a
# custom subscription.
_GLOBAL_WILDCARD = "#"

# Stable identity of the implicit broker used by old single-broker configs.
DEFAULT_BROKER_REF = "default"

# Known connection sources for a broker profile. A profile is either a plain
# local/LAN broker or the encrypted Zendure cloud broker.
SOURCE_LOCAL_MQTT = "local_mqtt"
SOURCE_ZENDURE_CLOUD_MQTT = "zendure_cloud_mqtt"


class ZendureMqttConfigError(ValueError):
    """Invalid runtime config; never carries credential detail."""


def _clean_str(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_subscriptions(raw) -> tuple[str, ...] | None:
    if raw is None:
        return None
    # A bare string is iterable but means a single filter, not a set of chars.
    if isinstance(raw, str) or not isinstance(raw, Iterable):
        raise ZendureMqttConfigError(
            "subscriptions must be a list of MQTT topic filters"
        )
    items = []
    for entry in raw:
        if not isinstance(entry, str):
            raise ZendureMqttConfigError(
                "subscriptions must be a list of MQTT topic filters"
            )
        text = entry.strip()
        if text and text != _GLOBAL_WILDCARD and text not in items:
            items.append(text)
    return tuple(items) if items else None


@dataclass(frozen=True)
class ZendureMqttRuntimeConfig:
    """Config for the runtime telemetry service, buildable from a plain dict.

    The password is held but excluded from ``repr`` so it cannot leak into logs
    or status output.
    """

    enabled: bool = False
    host: str | None = None
    port: int = 1883
    tls: bool = False
    tls_insecure: bool = False
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    client_id: str | None = None
    app_key: str | None = None
    subscriptions: tuple[str, ...] | None = None
    connect_timeout_seconds: float = 10.0
    keepalive_seconds: int = 30
    # A device snapshot older than this is reported as stale in status output.
    stale_after_seconds: float = 60.0
    # Broker profile identity. ``broker_ref`` is the key devices reference and
    # ``source`` records the connection kind (local vs Zendure cloud).
    broker_ref: str = DEFAULT_BROKER_REF
    source: str | None = None

    @classmethod
    def from_dict(
        cls,
        data: dict | None,
        *,
        broker_ref: str = DEFAULT_BROKER_REF,
        source: str | None = None,
        enabled: bool | None = None,
        stale_after_seconds: float | None = None,
    ) -> "ZendureMqttRuntimeConfig":
        data = data or {}
        from ems.config import (
            default_mqtt_port,
            parse_mqtt_port,
            require_json_bool,
            resolve_mqtt_tls_metadata,
        )

        # The telemetry feature has no opt-out: a profile is active exactly
        # when it has a broker host (and the loader's per-profile flag, when
        # one is passed). The legacy top-level ``enabled`` key in ``data`` is
        # ignored entirely, so a profile without a host is inactive — never a
        # config error.
        resolved_flag = (
            True if enabled is None else require_json_bool(enabled, "enabled")
        )
        host = _clean_str(data.get("host"))
        resolved_enabled = resolved_flag and bool(host)
        resolved_stale = (
            float(data.get("stale_after_seconds", 60.0))
            if stale_after_seconds is None
            else float(stale_after_seconds)
        )
        try:
            tls, tls_insecure = resolve_mqtt_tls_metadata(
                tls_mode=data.get("tls_mode"),
                tls=data.get("tls"),
                tls_insecure=data.get("tls_insecure"),
            )
            port = parse_mqtt_port(
                data.get("port"), default=default_mqtt_port(tls)
            )
        except ValueError as exc:
            raise ZendureMqttConfigError(str(exc)) from exc
        return cls(
            enabled=resolved_enabled,
            host=host,
            port=port,
            tls=tls,
            tls_insecure=tls_insecure,
            username=_clean_str(data.get("username")),
            password=data.get("password") or None,
            client_id=_clean_str(data.get("client_id")),
            app_key=_clean_str(data.get("app_key")),
            subscriptions=_clean_subscriptions(data.get("subscriptions")),
            connect_timeout_seconds=float(data.get("connect_timeout_seconds", 10.0)),
            keepalive_seconds=int(data.get("keepalive_seconds", 30)),
            stale_after_seconds=resolved_stale,
            broker_ref=str(broker_ref),
            source=_clean_str(source) or _clean_str(data.get("source")),
        )

    def client_config(self) -> ZendureMqttClientConfig:
        if not self.host:
            raise ZendureMqttConfigError("no broker host is configured")
        return ZendureMqttClientConfig(
            host=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            tls=self.tls,
            tls_insecure=self.tls_insecure,
            client_id=self.client_id,
            app_key=self.app_key,
            subscriptions=self.subscriptions,
            connect_timeout_seconds=self.connect_timeout_seconds,
            keepalive_seconds=self.keepalive_seconds,
        )

    def redacted(self) -> "ZendureMqttRuntimeConfig":
        return replace(self, username=None, password=None)


def _default_read_client_factory(config: ZendureMqttClientConfig):
    from ems.zendure_mqtt.client import ZendureMqttReadClient

    return ZendureMqttReadClient(config)


class ZendureMqttService:
    """Start/stop wrapper around one read-only Zendure MQTT client.

    ``start`` and ``stop`` are idempotent and never raise on connection failure:
    a broker problem is captured as sanitized ``last_error`` so it cannot affect
    the EMS control loop. A disabled config never constructs or connects a client.
    """

    def __init__(self, config: ZendureMqttRuntimeConfig, *, read_client_factory=None):
        self._config = config
        self._read_client_factory = read_client_factory or _default_read_client_factory
        self._client = None
        self._last_error: str | None = None
        self._next_start_retry_monotonic = 0.0

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def running(self) -> bool:
        return self._client is not None

    @property
    def connected(self) -> bool:
        return bool(self._client is not None and self._client.connected)

    def start(self) -> None:
        if not self._config.enabled or self._client is not None:
            return
        # Failed attempts are throttled so the control loop can call start()
        # every cycle to self-heal a broker that was down at boot, without
        # issuing a blocking connect attempt each cycle.
        if time.monotonic() < self._next_start_retry_monotonic:
            return
        client = self._read_client_factory(self._config.client_config())
        try:
            client.start()
        except ZendureMqttClientError as exc:
            self._last_error = str(exc)
            self._next_start_retry_monotonic = (
                time.monotonic() + START_RETRY_COOLDOWN_SECONDS
            )
            self._safe_stop(client)
            logger.warning("event=zendure_mqtt_service_start_failed error=%s", exc)
            return
        self._client = client
        self._last_error = None
        self._next_start_retry_monotonic = 0.0

    def stop(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            self._safe_stop(client)

    def snapshots(self) -> dict:
        """Latest per-device snapshots keyed by device id (empty when stopped)."""

        if self._client is None:
            return {}
        return self._client.snapshots()

    def snapshot_status(self, device_id, *, now_monotonic=None) -> SnapshotStatus:
        """Freshness verdict for one device, honouring ``stale_after_seconds``."""

        if now_monotonic is None:
            now_monotonic = time.monotonic()
        snapshot = self.snapshots().get(device_id)
        return classify_snapshot(
            snapshot,
            self._config.stale_after_seconds,
            now_monotonic=now_monotonic,
        )

    def status(self) -> dict:
        """Credential-free status summary safe to log or surface."""

        return {
            "enabled": self._config.enabled,
            "running": self.running,
            "connected": self.connected,
            "host": self._config.host,
            "port": self._config.port,
            "snapshot_count": len(self.snapshots()),
            "last_error": self._last_error,
        }

    @staticmethod
    def _safe_stop(client) -> None:
        try:
            client.stop()
        except Exception:
            pass
