# SPDX-License-Identifier: AGPL-3.0-or-later
"""Core-owned MQTT credential resolution.

Only non-secret references are persisted in config. This module reads the EMS
secret store directly and deliberately has no dependency on ``admin``.
"""

import base64
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ems.zendure_mqtt.config_entries import (
    SOURCE_LOCAL_MQTT,
    SOURCE_ZENDURE_CLOUD_MQTT,
)

_REF_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# A canonical ref also bounds its length: it becomes a secrets-dir filename
# (``mqtt-<ref>.json``), so an unbounded value could not resolve to a real
# managed file. No legitimate reference approaches this.
_MAX_REF_LEN = 128


class MqttCredentialError(ValueError):
    """Sanitized credential resolution failure safe for operator output."""


# The one authoritative per-source runtime credential contract, owned by the
# EMS Core (not Admin). The Core resolver only proves a record decodes; a broker
# that references it additionally needs every field listed here. A local broker
# authenticates with a username/password pair; a Zendure cloud broker also builds
# its session from ``client_id`` and subscribes on ``app_key`` (see
# ems/zendure_mqtt/runtime.py). An anonymous broker carries no ``credentials_ref``
# and never reaches this contract.
MQTT_CREDENTIAL_REQUIRED_FIELDS = {
    SOURCE_LOCAL_MQTT: ("username", "password"),
    SOURCE_ZENDURE_CLOUD_MQTT: ("username", "password", "client_id", "app_key"),
}


def _credential_field(credentials, field):
    if isinstance(credentials, Mapping):
        return credentials.get(field)
    return getattr(credentials, field, None)


def missing_mqtt_credential_fields(credentials, *, source):
    """Return the required fields a resolved record leaves blank for ``source``.

    A field is complete only when it is a non-whitespace string. The stored value
    is never trimmed — only tested for emptiness after stripping — so a genuine
    password with surrounding spaces counts as complete and stays byte-for-byte
    intact. ``credentials`` may be a :class:`MqttCredentials` or a plain mapping
    with the same field names. An unknown source falls back to the local pair.
    """

    required = MQTT_CREDENTIAL_REQUIRED_FIELDS.get(
        source, MQTT_CREDENTIAL_REQUIRED_FIELDS[SOURCE_LOCAL_MQTT]
    )
    return tuple(
        field
        for field in required
        if not (
            isinstance(_credential_field(credentials, field), str)
            and _credential_field(credentials, field).strip()
        )
    )


def require_complete_mqtt_credentials(credentials, *, source, credentials_ref):
    """Raise when a referenced record misses a required field for ``source``.

    A configured ``credentials_ref`` stands for real authentication, so an
    incomplete record must never silently degrade to an anonymous connection.
    The error names the reference and the missing field names only — never a
    credential value.
    """

    missing = missing_mqtt_credential_fields(credentials, source=source)
    if missing:
        raise MqttCredentialError(
            f"MQTT credential reference '{credentials_ref}' is incomplete for "
            f"{source}: missing {', '.join(missing)}"
        )


def validate_mqtt_credentials_ref(value):
    """Return ``value`` unchanged when it is a canonical credentials_ref.

    A configured reference is an immutable identifier: it must already use the
    canonical syntax (a lowercase alphanumeric first character, then lowercase
    alphanumerics, ``-`` or ``_``) so it resolves to exactly one credential
    file and stays byte-identical from validation through runtime loading.
    This is the one authoritative syntax contract shared by the Core runtime
    resolver and the Admin credential-staging gate that both Setup and
    Maintenance apply run before writing config — a value already in the config
    is never silently normalized to another identifier. Raises
    :class:`MqttCredentialError` (secret-free) otherwise.
    """

    if not (
        isinstance(value, str)
        and len(value) <= _MAX_REF_LEN
        and _REF_RE.fullmatch(value)
    ):
        raise MqttCredentialError("MQTT credentials_ref is not a canonical reference")
    return value


@dataclass(frozen=True)
class MqttCredentials:
    username: str | None
    password: str | None
    # Cloud runtime records also carry the MQTT client id and account app key;
    # local broker records leave both ``None``.
    client_id: str | None = None
    app_key: str | None = None


class MqttCredentialResolver(Protocol):
    def resolve(self, credentials_ref: str) -> MqttCredentials: ...


def default_mqtt_secrets_dir() -> Path:
    configured = os.environ.get("EMS_CONFIG_DIR")
    if configured:
        return Path(configured) / "secrets"
    install_root = os.environ.get("EMS_INSTALL_DIR")
    if install_root:
        return Path(install_root) / "config" / "secrets"
    from ems import paths

    return Path(paths.BASE_DIR) / "config" / "secrets"


class FileMqttCredentialResolver:
    def __init__(self, secrets_dir=None):
        self.secrets_dir = Path(secrets_dir) if secrets_dir else default_mqtt_secrets_dir()

    def resolve(self, credentials_ref: str) -> MqttCredentials:
        # Validate the configured reference exactly as given: normalizing here
        # (lowercasing/stripping) would silently resolve a different identifier
        # than the config declares.
        ref = validate_mqtt_credentials_ref(credentials_ref)
        path = self.secrets_dir / f"mqtt-{ref}.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise MqttCredentialError(f"MQTT credential reference '{ref}' was not found") from exc
        except (OSError, ValueError) as exc:
            raise MqttCredentialError(f"MQTT credential reference '{ref}' is unavailable or malformed") from exc
        if not isinstance(record, dict) or record.get("ref") not in (None, ref):
            raise MqttCredentialError(f"MQTT credential reference '{ref}' is malformed")
        username = self._field(record, "username", ref)
        password = self._field(record, "password", ref)
        if username == "":
            raise MqttCredentialError(f"MQTT credential reference '{ref}' has an empty username")
        if username is not None and password in (None, ""):
            raise MqttCredentialError(
                f"MQTT credential reference '{ref}' has no password"
            )
        if password is not None and username in (None, ""):
            raise MqttCredentialError(
                f"MQTT credential reference '{ref}' has incomplete authentication"
            )
        client_id = self._field(record, "client_id", ref)
        app_key = self._field(record, "app_key", ref)
        return MqttCredentials(username, password, client_id, app_key)

    def _field(self, record, field, ref):
        blob = record.get(field)
        if blob is None:
            return None
        if not isinstance(blob, str) or not blob:
            raise MqttCredentialError(f"MQTT credential reference '{ref}' is malformed")
        try:
            if record.get(f"{field}_encrypted") is True:
                from cryptography.fernet import Fernet

                key = (self.secrets_dir / ".secret-key").read_bytes()
                return Fernet(key).decrypt(blob.encode("ascii")).decode("utf-8")
            if record.get(f"{field}_encrypted") is False:
                return base64.b64decode(blob.encode("ascii"), validate=True).decode("utf-8")
        except Exception as exc:
            raise MqttCredentialError(f"MQTT credential reference '{ref}' could not be decrypted") from exc
        raise MqttCredentialError(f"MQTT credential reference '{ref}' is malformed")


def resolve_mqtt_profile_credentials(profile, resolver=None):
    """Return a copy with ``credentials_ref`` resolved to in-memory fields."""

    if not isinstance(profile, dict):
        return profile
    resolved = dict(profile)
    ref = resolved.get("credentials_ref")
    if not (isinstance(ref, str) and ref.strip()):
        return resolved
    if resolved.get("username") not in (None, "") or resolved.get("password") not in (None, ""):
        raise MqttCredentialError(
            f"MQTT credential reference '{ref.strip()}' conflicts with inline credentials"
        )
    credentials = (resolver or FileMqttCredentialResolver()).resolve(ref)
    require_complete_mqtt_credentials(
        credentials, source=SOURCE_LOCAL_MQTT, credentials_ref=ref.strip()
    )
    resolved["username"] = credentials.username
    resolved["password"] = credentials.password
    return resolved


# --- global MQTT credential consumer model ---------------------------------
# Credential integrity is a whole-config contract, not a per-feature one. One
# extractor discovers every component that references a runtime credential —
# Zendure broker profiles and MQTT grid meters alike — so validation, staging
# and final verification all reason about the same set instead of each growing
# its own scanner that could miss a consumer.

COMPONENT_ZENDURE_MQTT_BROKER = "zendure_mqtt_broker"
COMPONENT_GRID_METER = "grid_meter"


@dataclass(frozen=True)
class MqttCredentialConsumer:
    """One configured component that references a runtime MQTT credential.

    ``credentials_ref`` is the reference exactly as configured (never
    normalized); ``source`` is the compatible credential source
    (``local_mqtt``/``zendure_cloud_mqtt``); ``component`` names the feature
    (``zendure_mqtt_broker``/``grid_meter``); ``config_path`` locates the field
    for operator-facing messages; ``broker_ref`` is set when the consumer reaches
    the credential through a named broker profile (a grid meter using
    ``mqtt.broker_ref``) rather than owning the reference directly.
    """

    credentials_ref: str
    source: str
    component: str
    config_path: str
    broker_ref: str | None = None


def _mqtt_grid_meter_type(grid_meter):
    """Return the lowercased grid-meter type when it is MQTT-backed, else None."""

    from collections.abc import Mapping

    from ems.config import MQTT_GRID_METER_TYPES

    if not isinstance(grid_meter, Mapping):
        return None
    meter_type = str(grid_meter.get("type") or "").strip().lower()
    return meter_type if meter_type in MQTT_GRID_METER_TYPES else None


def _grid_meter_broker_ref(grid_meter):
    from collections.abc import Mapping

    mqtt = grid_meter.get("mqtt") if isinstance(grid_meter, Mapping) else None
    raw = mqtt.get("broker_ref") if isinstance(mqtt, Mapping) else None
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def _referenced_broker_refs(config):
    """Broker refs an enabled MQTT device or the MQTT grid meter references."""

    from ems.zendure_mqtt.config_entries import (
        config_entry_enabled,
        is_zendure_mqtt_device_config,
        zendure_mqtt_broker_ref,
    )

    refs = set()
    devices = config.get("devices")
    if isinstance(devices, list):
        for device in devices:
            if is_zendure_mqtt_device_config(device) and config_entry_enabled(device):
                refs.add(zendure_mqtt_broker_ref(device))
    grid = config.get("grid_meter")
    if _mqtt_grid_meter_type(grid) and config_entry_enabled(grid):
        broker_ref = _grid_meter_broker_ref(grid)
        if broker_ref:
            refs.add(broker_ref)
    return refs


def _grid_meter_consumer(config):
    from collections.abc import Mapping

    from ems.zendure_mqtt.config_entries import (
        SOURCE_LOCAL_MQTT,
        config_entry_enabled,
        effective_broker_enabled,
        get_effective_mqtt_broker_profile,
    )

    grid = config.get("grid_meter")
    if not _mqtt_grid_meter_type(grid) or not config_entry_enabled(grid):
        return None
    mqtt = grid.get("mqtt")
    mqtt = mqtt if isinstance(mqtt, Mapping) else {}
    broker_ref = _grid_meter_broker_ref(grid)
    if broker_ref:
        # A named-broker grid meter reaches its credential through the effective
        # broker profile the runtime resolves — including the implicit ``default``
        # top-level broker — so the scanner and runtime always agree on which
        # broker a ref names and never drop the legacy default's credential.
        effective = get_effective_mqtt_broker_profile(config, broker_ref)
        if effective is None or not effective_broker_enabled(effective):
            return None
        profile = effective.config
        cred_ref = profile.get("credentials_ref")
        if cred_ref is None:
            return None
        # The runtime grid-meter resolver defaults a source-less broker to
        # local_mqtt and rejects any non-local broker, so a grid meter's broker is
        # always a local consumer; a missing source must not drop its credential
        # from local staging.
        return MqttCredentialConsumer(
            credentials_ref=cred_ref,
            source=str(profile.get("source") or SOURCE_LOCAL_MQTT).strip().lower(),
            component=COMPONENT_GRID_METER,
            config_path="grid_meter.mqtt.broker_ref",
            broker_ref=broker_ref,
        )
    cred_ref = mqtt.get("credentials_ref")
    if cred_ref is None:
        return None
    # A direct MQTT grid meter always authenticates against a local broker.
    return MqttCredentialConsumer(
        credentials_ref=cred_ref,
        source=SOURCE_LOCAL_MQTT,
        component=COMPONENT_GRID_METER,
        config_path="grid_meter.mqtt.credentials_ref",
        broker_ref=None,
    )


def _has_host(profile):
    host = profile.get("host")
    return isinstance(host, str) and bool(host.strip())


def collect_mqtt_credential_consumers(config):
    """Return every MQTT credential consumer discovered in the complete config.

    Broker profiles are resolved once through the shared Core resolver
    (:func:`iter_effective_mqtt_broker_profiles`) so this scanner and the EMS
    runtime always agree on which brokers exist. Covers the implicit legacy
    ``default`` broker (top-level ``zendure_mqtt`` fields), enabled and referenced
    named ``zendure_mqtt.brokers`` profiles (local and cloud), a direct MQTT grid
    meter and a grid meter that references a named broker profile. Disabled or
    unreferenced named profiles follow the existing runtime semantics and yield
    no consumer, so a value never becomes a false credential requirement. The
    returned references are verbatim so a later canonical check rejects exactly
    what the config declared.
    """

    from collections.abc import Mapping

    from ems.zendure_mqtt.config_entries import (
        ORIGIN_LEGACY_DEFAULT,
        SOURCE_LOCAL_MQTT,
        config_entry_enabled,
        iter_effective_mqtt_broker_profiles,
    )

    if not isinstance(config, Mapping):
        return ()

    referenced = _referenced_broker_refs(config)
    consumers = []
    for profile in iter_effective_mqtt_broker_profiles(config):
        prof = profile.config
        cred_ref = prof.get("credentials_ref")
        if cred_ref is None:
            continue
        if profile.origin == ORIGIN_LEGACY_DEFAULT:
            # The legacy top-level broker is the single-broker install's own
            # broker: the runtime resolves its credential whenever it has a host,
            # so it consumes independently of device references. A source-less
            # top-level broker resolves as local_mqtt, matching the runtime.
            if not _has_host(prof):
                continue
            source = str(prof.get("source") or SOURCE_LOCAL_MQTT).strip().lower()
            config_path = "zendure_mqtt.credentials_ref"
        else:
            if profile.broker_ref not in referenced or not config_entry_enabled(prof):
                continue
            source = str(prof.get("source") or "").strip().lower()
            config_path = (
                f"zendure_mqtt.brokers.{profile.broker_ref}.credentials_ref"
            )
        consumers.append(
            MqttCredentialConsumer(
                credentials_ref=cred_ref,
                source=source,
                component=COMPONENT_ZENDURE_MQTT_BROKER,
                config_path=config_path,
                broker_ref=None,
            )
        )

    grid_consumer = _grid_meter_consumer(config)
    if grid_consumer is not None:
        consumers.append(grid_consumer)
    return tuple(consumers)


def find_mqtt_credential_consumer_issues(config):
    """Sanitized config-integrity issues for every MQTT credential consumer.

    Core owns configuration-level credential integrity, so Preview, the Core
    validator and Apply all report the same two stable codes for the same
    config:

    * ``mqtt_credentials_ref_invalid`` — a configured ``credentials_ref`` is not
      canonical;
    * ``mqtt_credential_source_conflict`` — one reference is claimed by
      incompatible credential sources (a reference resolves to a single file,
      so cross-source sharing would have one source overwrite the other).

    Each issue carries ``{severity, code, message, path, credentials_ref}`` (and
    ``sources``/``consumers`` for a conflict). A ``credentials_ref`` is a
    non-secret config identifier; no secret value appears in an issue.
    """

    from ems.zendure_mqtt.config_entries import SUPPORTED_BROKER_SOURCES

    issues = []
    sources_by_ref = {}
    components_by_ref = {}
    path_by_ref = {}
    for consumer in collect_mqtt_credential_consumers(config):
        try:
            validate_mqtt_credentials_ref(consumer.credentials_ref)
        except MqttCredentialError:
            issues.append(
                {
                    "severity": "error",
                    "code": "mqtt_credentials_ref_invalid",
                    "message": (
                        f"{consumer.config_path} is not a canonical MQTT "
                        "credentials_ref"
                    ),
                    "path": consumer.config_path,
                    "credentials_ref": consumer.credentials_ref,
                }
            )
            continue
        if consumer.source in SUPPORTED_BROKER_SOURCES:
            sources_by_ref.setdefault(consumer.credentials_ref, set()).add(
                consumer.source
            )
            components_by_ref.setdefault(consumer.credentials_ref, set()).add(
                consumer.component
            )
            path_by_ref.setdefault(consumer.credentials_ref, consumer.config_path)
    for ref in sorted(sources_by_ref):
        sources = sources_by_ref[ref]
        if len(sources) > 1:
            issues.append(
                {
                    "severity": "error",
                    "code": "mqtt_credential_source_conflict",
                    "message": (
                        f"credentials_ref '{ref}' is claimed by incompatible "
                        f"credential sources ({', '.join(sorted(sources))}); one "
                        "reference can back only one source"
                    ),
                    "path": path_by_ref[ref],
                    "credentials_ref": ref,
                    "sources": sorted(sources),
                    "consumers": sorted(components_by_ref[ref]),
                }
            )
    return issues


def resolve_mqtt_cloud_profile_credentials(profile, resolver=None):
    """Return a copy of a cloud broker profile with its runtime secret resolved.

    Unlike :func:`resolve_mqtt_profile_credentials`, this fills the cloud-only
    ``client_id`` and ``app_key`` in addition to ``username``/``password`` from
    the Core-owned runtime credential record referenced by ``credentials_ref``.
    A profile without a ``credentials_ref`` is returned unchanged; a reference
    that cannot be resolved raises :class:`MqttCredentialError` for the caller to
    handle (the runtime treats a missing cloud record as unusable, not fatal).
    """

    if not isinstance(profile, dict):
        return profile
    resolved = dict(profile)
    ref = resolved.get("credentials_ref")
    if not (isinstance(ref, str) and ref.strip()):
        return resolved
    credentials = (resolver or FileMqttCredentialResolver()).resolve(ref)
    require_complete_mqtt_credentials(
        credentials, source=SOURCE_ZENDURE_CLOUD_MQTT, credentials_ref=ref.strip()
    )
    resolved["username"] = credentials.username
    resolved["password"] = credentials.password
    resolved["client_id"] = credentials.client_id
    resolved["app_key"] = credentials.app_key
    return resolved
