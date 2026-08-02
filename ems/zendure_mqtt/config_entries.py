# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recognize and validate Zendure MQTT device config entries.

Pure and dependency-light: no paho/MQTT client, no network, no file writes.
These helpers classify a ``config.json`` ``devices[]`` entry as a Zendure MQTT
device and validate its shape. They never read, echo or require secret MQTT
fields (broker password, tokens, cloud app key).

A Zendure MQTT entry without an explicit capability is telemetry-only.
Discovery enables supported, addressable inverters by default by writing
``capabilities.write_output_limit=true``; such a control entry validates through
:func:`validate_zendure_mqtt_control_device_config` and still writes only behind
its broker profile's MQTT write gate (an operational safety control, on by
default in the release template).
"""

import hashlib
import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ems.device_identity import (
    normalize_mqtt_route_segment,
    resolve_inverter_identity,
)

ZENDURE_MQTT_TYPE = "zendure_mqtt"

# Stable identity of the implicit broker used by old single-broker configs. A
# device without an explicit ``mqtt.broker_ref`` maps to it.
DEFAULT_BROKER_REF = "default"

# ``default`` is owned exclusively by the implicit legacy top-level broker: a
# named ``zendure_mqtt.brokers.default`` profile would collide with that identity
# and could silently overwrite it, so the ref is reserved for named profiles.
RESERVED_MQTT_BROKER_REFS = frozenset({DEFAULT_BROKER_REF})

# Backward-compatible ref for a single locally discovered broker and the prefix
# for the deterministic per-endpoint refs minted for every discovered broker.
LOCAL_BROKER_REF = "local_mqtt"

# Connection kinds a broker profile may declare. Kept here (not imported from
# the paho-backed service module) so this validator stays dependency-light.
SOURCE_LOCAL_MQTT = "local_mqtt"
SOURCE_ZENDURE_CLOUD_MQTT = "zendure_cloud_mqtt"
SUPPORTED_BROKER_SOURCES = frozenset({SOURCE_LOCAL_MQTT, SOURCE_ZENDURE_CLOUD_MQTT})

# Non-secret fields that mark a broker's credentials as living outside
# config.json (``credentials_ref``) or, for backward-compatible single-broker
# configs, still inline. Their presence is checked, never their value.
_INLINE_AUTH_KEYS = ("app_key", "password", "username", "token")

# Identifier fields, in order of preference, that make an entry addressable.
_DEVICE_IDENTIFIER_PATHS = (
    ("mqtt", "device_id"),
    ("serial_number",),
    ("device_id",),
)


def _issue(severity: str, code: str, message: str) -> dict[str, Any]:
    return {"severity": severity, "code": code, "message": message}


def _entry_type(item: Mapping[str, Any]) -> str:
    return str(item.get("type", "")).strip().lower()


def is_zendure_mqtt_device_config(item: Any) -> bool:
    """True if ``item`` is a ``devices[]`` entry of type ``zendure_mqtt``."""

    return isinstance(item, Mapping) and _entry_type(item) == ZENDURE_MQTT_TYPE


def _write_output_limit_requested(item: Mapping[str, Any]) -> bool:
    capabilities = item.get("capabilities")
    if not isinstance(capabilities, Mapping):
        return False
    return capabilities.get("write_output_limit") is True


def is_telemetry_only_zendure_mqtt_device_config(item: Any) -> bool:
    """True for a Zendure MQTT entry that does not request output writes."""

    return is_zendure_mqtt_device_config(item) and not _write_output_limit_requested(
        item
    )


def config_entry_enabled(item: Any, *, default: bool = True) -> bool:
    """Strictly resolve a device/config entry's ``enabled`` flag.

    A non-boolean ``enabled`` (e.g. the string ``"false"`` or ``0``) is never
    trusted as enabled: it resolves to ``False`` so a mistyped flag cannot
    silently keep a device active. A missing flag uses ``default``.
    """

    if not isinstance(item, Mapping):
        return default
    from ems.config import optional_json_bool

    try:
        return optional_json_bool(item.get("enabled"), "enabled", default=default)
    except ValueError:
        return False


def is_control_zendure_mqtt_device_config(item: Any) -> bool:
    """True for a Zendure MQTT entry that opts in to output control writes."""

    return is_zendure_mqtt_device_config(item) and _write_output_limit_requested(item)


def has_enabled_mqtt_control_device(config: Any) -> bool:
    """True if ``config.devices[]`` holds any enabled MQTT control entry.

    Used at startup to decide whether a control-runtime build failure is fatal:
    dropping a configured control device from the loop is unsafe.
    """

    if not isinstance(config, Mapping):
        return False
    devices = config.get("devices")
    if not isinstance(devices, list):
        return False
    return any(
        is_control_zendure_mqtt_device_config(item)
        and config_entry_enabled(item)
        for item in devices
    )


def has_runtime_control_device(config: Any) -> bool:
    """True when startup can build at least one control-loop device.

    Every enabled non-MQTT entry is an HTTP/API control device, matching
    :func:`ems.config.http_control_device_configs`. An MQTT entry participates
    only when it explicitly requests output control. Telemetry-only MQTT entries
    and disabled entries of either transport therefore do not make an otherwise
    empty EMS config bootable.
    """

    if not isinstance(config, Mapping):
        return False
    devices = config.get("devices")
    if not isinstance(devices, list):
        return False
    return any(
        isinstance(item, Mapping)
        and config_entry_enabled(item)
        and (
            not is_zendure_mqtt_device_config(item)
            or is_control_zendure_mqtt_device_config(item)
        )
        for item in devices
    )


def _mqtt_raw(item: Any, key: str) -> str | None:
    """Case-preserving ``mqtt.<key>`` string (topics/keys are case-sensitive)."""

    if isinstance(item, Mapping):
        mqtt = item.get("mqtt")
        if isinstance(mqtt, Mapping):
            value = mqtt.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def zendure_mqtt_write_topic(item: Any) -> str | None:
    """Explicit ``mqtt.write_topic`` override for a control entry, or ``None``."""

    return _mqtt_raw(item, "write_topic")


def zendure_mqtt_product_key(item: Any) -> str | None:
    """Configured ``mqtt.product_key`` for a control entry, or ``None``."""

    return _mqtt_raw(item, "product_key")


def zendure_mqtt_topic_family(item: Any) -> str | None:
    """Configured ``mqtt.topic_family`` for an entry, or ``None``."""

    return _mqtt_raw(item, "topic_family")


def zendure_mqtt_write_protocol(item: Any) -> str | None:
    """Explicit ``mqtt.write_protocol`` for a control entry, or ``None``."""

    return _mqtt_raw(item, "write_protocol")


def zendure_mqtt_hardware_profile(item: Any) -> str | None:
    """Pinned hardware profile for an entry (top-level or ``mqtt``), or ``None``.

    The hardware profile selects the verified write adapter at runtime; it is
    never derived from the topic family. A top-level ``hardware_profile`` is
    preferred, with ``mqtt.hardware_profile`` accepted as a fallback.
    """

    if isinstance(item, Mapping):
        value = item.get("hardware_profile")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _mqtt_raw(item, "hardware_profile")


def zendure_mqtt_power_write_profile(item: Any) -> str | None:
    """Stored informational ``power_write_profile`` for an entry, or ``None``.

    This is metadata mirroring the registry's write profile for the pinned
    hardware model. The registry stays authoritative; the stored value only ever
    has to *agree* with it (see :func:`_power_write_profile_metadata_issues`).
    """

    if isinstance(item, Mapping):
        value = item.get("power_write_profile")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _mqtt_raw(item, "power_write_profile")


def _power_write_profile_metadata_issues(
    item: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Reject a stored ``power_write_profile`` that contradicts the model.

    A missing value is fine (the registry re-derives it). A present value must
    equal the pinned model's registry write profile — including ``telemetry_only``
    for a read-only model — otherwise the config is contradictory and rejected so
    the runtime never trusts tampered write metadata.
    """

    from ems.mqtt_control.zendure_profiles import hardware_profile_by_name

    hardware_profile = zendure_mqtt_hardware_profile(item)
    stored = zendure_mqtt_power_write_profile(item)
    if hardware_profile is None or stored is None:
        return []
    profile = hardware_profile_by_name(hardware_profile)
    if profile is None:
        return []
    expected = profile.power_write_profile
    if stored == expected:
        return []
    return [
        _issue(
            "error",
            "power_write_profile_mismatch",
            f"configured power_write_profile {stored!r} does not match hardware "
            f"profile {profile.canonical_name!r}; expected {expected!r}",
        )
    ]


def zendure_mqtt_device_identifier(item: Any) -> str | None:
    """Return the addressable identifier of a Zendure MQTT entry, or ``None``.

    Prefers ``mqtt.device_id``, then ``serial_number``, then ``device_id``.
    """

    if not isinstance(item, Mapping):
        return None
    for path in _DEVICE_IDENTIFIER_PATHS:
        cursor: Any = item
        for key in path:
            cursor = cursor.get(key) if isinstance(cursor, Mapping) else None
        if isinstance(cursor, str) and cursor.strip():
            return cursor.strip()
    return None


def _has_device_identifier(item: Mapping[str, Any]) -> bool:
    return zendure_mqtt_device_identifier(item) is not None


def zendure_mqtt_route_device_id(item: Any) -> str | None:
    """Return the explicit MQTT route/payload device id, or ``None``.

    Reads only ``mqtt.device_id`` — the exact MQTT topic segment and payload
    ``deviceId`` a control write targets — and never falls back to the physical
    ``serial_number`` or a top-level ``device_id``. The value is trimmed and
    case-preserved (route segments are case-sensitive); masked, redacted and
    placeholder values resolve to ``None``. This is the sole authority for
    addressing a control write, distinct from
    :func:`zendure_mqtt_device_identifier` (telemetry matching, legacy fallback):
    a physical serial identifies the inverter, never its MQTT route.
    """

    if not isinstance(item, Mapping):
        return None
    mqtt = item.get("mqtt")
    if not isinstance(mqtt, Mapping):
        return None
    return normalize_mqtt_route_segment(mqtt.get("device_id"))


ADDRESSABILITY_READY = "ready"
ADDRESSABILITY_MISSING_DEVICE_ID = "missing_device_id"
ADDRESSABILITY_MISSING_PRODUCT_KEY = "missing_product_key"
ADDRESSABILITY_MISSING_WRITE_TOPIC = "missing_write_topic"
ADDRESSABILITY_INVALID_WRITE_TOPIC = "invalid_write_topic"


@dataclass(frozen=True)
class ControlAddressability:
    """Whether a Zendure MQTT control entry has a complete, explicit write route."""

    ready: bool
    reason: str
    profile_backed: bool
    device_id: str | None
    product_key: str | None
    write_topic: str | None


def _profile_backed_control(item: Any) -> bool:
    """True when a pinned, known, writable profile with a publish route applies.

    Such a device publishes to its canonical ``iot/<productKey>/<deviceId>``
    topic, so its write route needs a product key; a device without a pinned
    writable profile is addressed by the explicit custom ``mqtt.write_topic``.
    This is the route *shape* only and stays broker-source independent — a device
    blocked by its broker source is still canonically addressed, and must report
    that block rather than a missing custom write topic.
    """

    from ems.mqtt_control.power_capability import profile_write_route_implemented

    return profile_write_route_implemented(zendure_mqtt_hardware_profile(item))


def zendure_mqtt_control_addressability(
    item: Any, *, profile_backed: bool | None = None
) -> ControlAddressability:
    """Resolve the write-route addressability of a control entry.

    Every control write needs an explicit ``mqtt.device_id`` route id (the topic
    segment and the payload ``deviceId``); a physical serial is never
    substituted. A profile-backed device additionally needs a ``mqtt.product_key``
    for its canonical topic; a custom device needs a valid explicit
    ``mqtt.write_topic``. ``profile_backed`` overrides mode detection for callers
    (migration) that resolve the intended writable model separately. Single source
    of truth for config validation, migration, Maintenance readiness and
    diagnostics so route checks never drift apart.
    """

    from ems.zendure_mqtt.write_protocols import publish_topic_error

    device_id = zendure_mqtt_route_device_id(item)
    product_key = zendure_mqtt_product_key(item)
    write_topic = zendure_mqtt_write_topic(item)
    if profile_backed is None:
        profile_backed = _profile_backed_control(item)
    if profile_backed:
        if product_key is None:
            reason = ADDRESSABILITY_MISSING_PRODUCT_KEY
        elif device_id is None:
            reason = ADDRESSABILITY_MISSING_DEVICE_ID
        else:
            reason = ADDRESSABILITY_READY
    elif write_topic is None:
        reason = ADDRESSABILITY_MISSING_WRITE_TOPIC
    elif publish_topic_error(write_topic) is not None:
        reason = ADDRESSABILITY_INVALID_WRITE_TOPIC
    elif device_id is None:
        reason = ADDRESSABILITY_MISSING_DEVICE_ID
    else:
        reason = ADDRESSABILITY_READY
    return ControlAddressability(
        reason == ADDRESSABILITY_READY,
        reason,
        profile_backed,
        device_id,
        product_key,
        write_topic,
    )


def zendure_cloud_device_subscriptions(devices: Any, broker_ref: str) -> tuple[str, ...]:
    """Device-scoped cloud topic filters for entries bound to ``broker_ref``.

    The Zendure cloud broker serves ACL-scoped account sessions that never
    deliver the broad local wildcard families; telemetry only arrives on the
    per-device trees. Cloud services therefore subscribe exactly those (parity
    with Admin cloud discovery); the account ``<app_key>/#`` tree is appended by
    the client config. Disabled entries and entries without a product key or
    route device id contribute nothing.

    The device-scoped route is ``<productKey>/<mqtt.device_id>``: both are
    case-sensitive MQTT segments and the physical ``serial_number`` is never
    substituted for the route id, so an entry without an explicit ``mqtt.device_id``
    contributes no subscription rather than subscribing to a wrong topic.
    """

    if not isinstance(devices, list):
        return ()
    topics: list[str] = []
    for item in devices:
        if not is_zendure_mqtt_device_config(item) or not config_entry_enabled(item):
            continue
        if zendure_mqtt_broker_ref(item) != broker_ref:
            continue
        product_key = zendure_mqtt_product_key(item)
        identifier = zendure_mqtt_route_device_id(item)
        if not product_key or not identifier:
            continue
        for topic in (
            f"/{product_key}/{identifier}/#",
            f"iot/{product_key}/{identifier}/#",
        ):
            if topic not in topics:
                topics.append(topic)
    return tuple(topics)


def zendure_mqtt_broker_ref(item: Any) -> str:
    """Configured broker ref for a Zendure MQTT entry, defaulting to ``default``."""

    if isinstance(item, Mapping):
        mqtt = item.get("mqtt")
        if isinstance(mqtt, Mapping):
            ref = mqtt.get("broker_ref")
            if isinstance(ref, str) and ref.strip():
                return ref.strip()
    return DEFAULT_BROKER_REF


def zendure_mqtt_source(item: Any) -> str | None:
    """Configured connection source for a Zendure MQTT entry, or ``None``."""

    if isinstance(item, Mapping):
        mqtt = item.get("mqtt")
        if isinstance(mqtt, Mapping):
            source = mqtt.get("source")
            if isinstance(source, str) and source.strip():
                return source.strip()
    return None


def zendure_mqtt_effective_broker_source(
    item: Any, broker_sources: Any = None
) -> str | None:
    """Canonical broker source a Zendure MQTT entry writes through, or ``None``.

    Single source of truth for the broker-source capability axis. The broker
    profile is authoritative (a device that contradicts it is rejected by
    validation); an entry's own ``mqtt.source`` is read only when no profile map
    is available, which is the case for the Admin fragment paths that work on a
    single detached entry. An unresolvable source stays ``None`` so the
    capability layer fails closed rather than assuming a transport.
    """

    from ems.mqtt_control.power_capability import normalize_broker_source

    if isinstance(broker_sources, Mapping):
        resolved = normalize_broker_source(
            broker_sources.get(zendure_mqtt_broker_ref(item))
        )
        if resolved is not None:
            return resolved
    return normalize_broker_source(zendure_mqtt_source(item))


def _normalized(value: Any) -> str | None:
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed:
            return trimmed.lower()
    return None


def zendure_config_device_identity(
    item: Any, *, broker_sources: Mapping[str, str] | None = None
) -> tuple[str, ...] | None:
    """Return a sanitized, comparable identity for a Zendure config entry.

    A serial number is preferred because it is the strongest cross-adapter
    identity: a local/API device and an MQTT entry for the same physical unit
    collide on it. MQTT entries without a serial use a source-, broker-, and
    product/topic-scoped route; Local API entries without a serial use their
    endpoint. The identity never includes credentials or a broker host.
    Returns ``None`` when no meaningful identity exists.
    """

    identity = resolve_inverter_identity(item, broker_sources=broker_sources)
    return identity.comparison_key if identity is not None else None


def zendure_physical_identity(item: Any) -> str | None:
    """Normalized physical identity of a config/draft device entry, or ``None``.

    Only an explicit physical serial qualifies here (local API ``sn`` or MQTT
    ``serial_number``). Account-scoped MQTT routing ids are deliberately never
    treated as physical serials. Placeholder (``YOUR_...``) and display-masked
    values resolve to ``None``. Normalization is for comparison only; stored
    identifiers are never rewritten.
    """

    identity = resolve_inverter_identity(item)
    if identity is None or identity.kind != "physical_serial":
        return None
    return identity.normalized_components[0]


def find_duplicate_zendure_device_identities(
    devices: Any, *, broker_sources: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    """Report duplicate physical-device identities among active ``devices[]``.

    A physical Zendure device must be configured only once. Entries are matched
    by :func:`zendure_config_device_identity`; ``enabled=false`` entries are
    ignored (missing ``enabled`` counts as enabled). Messages expose only the
    device index, never serials, device ids or credentials.
    """

    if not isinstance(devices, list):
        return []

    seen: dict[tuple[str, ...], int] = {}
    issues: list[dict[str, Any]] = []
    for index, item in enumerate(devices):
        if not isinstance(item, Mapping) or not config_entry_enabled(item):
            continue
        identity = zendure_config_device_identity(
            item, broker_sources=broker_sources
        )
        if identity is None:
            continue
        first = seen.get(identity)
        if first is None:
            seen[identity] = index
            continue
        issues.append(
            _issue(
                "error",
                "zendure_device_identity_duplicate",
                f"Duplicate configured Zendure device identity between "
                f"devices.{first} and devices.{index}. "
                "Configure each physical device only once.",
            )
        )
    return issues


def find_duplicate_device_names(devices: Any) -> list[dict[str, Any]]:
    """Report duplicate config names among active ``devices[]`` entries.

    The device name is the EMS runtime identity key (controller state,
    runtime-state.json, dashboard snapshot, history series), so two active
    entries — regardless of transport — must never share one: they would
    silently merge into one logical device. ``enabled=false`` entries are
    ignored like in the identity check. A name is a display label, never a
    secret, so it may appear in the message.
    """

    if not isinstance(devices, list):
        return []

    seen: dict[str, int] = {}
    issues: list[dict[str, Any]] = []
    for index, item in enumerate(devices):
        if not isinstance(item, Mapping) or not config_entry_enabled(item):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        key = name.strip()
        first = seen.get(key)
        if first is None:
            seen[key] = index
            continue
        issues.append(
            _issue(
                "error",
                "device_name_duplicate",
                f"Duplicate device name between devices.{first} and "
                f"devices.{index}: {key}. Device names must be unique.",
            )
        )
    return issues


def duplicate_device_name_startup_error(devices: Any) -> dict[str, Any] | None:
    """Sanitized startup-abort fields when active device names collide.

    Returns ``{"duplicate_count": N}`` when two active entries share a name,
    else ``None`` — mirroring the identity startup guard.
    """

    duplicates = find_duplicate_device_names(devices)
    if not duplicates:
        return None
    return {"duplicate_count": len(duplicates)}


def duplicate_zendure_identity_startup_error(
    devices: Any, *, broker_sources: Mapping[str, str] | None = None
) -> dict[str, Any] | None:
    """Sanitized startup-abort fields when active Zendure identities collide.

    Returns ``{"duplicate_count": N}`` when a physical device is configured more
    than once, else ``None``. Only the count leaves this function, so a startup
    abort can be logged without exposing serials, device ids, product keys or
    credentials.
    """

    duplicates = find_duplicate_zendure_device_identities(
        devices, broker_sources=broker_sources
    )
    if not duplicates:
        return None
    return {"duplicate_count": len(duplicates)}


def _source_mismatch_issue(
    item: Mapping[str, Any], broker_sources: Any
) -> dict[str, Any] | None:
    """Reject a device that names a transport source other than its broker's.

    The broker profile is the sole authority for the transport (and thus the
    write gate). A device may omit ``mqtt.source`` and inherit the broker's, but
    it must never override it: that would let device config pick a different
    write gate than the broker profile. ``broker_sources`` maps each known
    ``broker_ref`` to its profile source; when it is absent the check is skipped.
    """

    if not isinstance(broker_sources, Mapping):
        return None
    device_source = _normalized(zendure_mqtt_source(item))
    if device_source is None:
        return None
    ref = zendure_mqtt_broker_ref(item)
    broker_source = _normalized(broker_sources.get(ref))
    if broker_source is None or device_source == broker_source:
        return None
    return _issue(
        "error",
        "mqtt_source_mismatch",
        f"mqtt.source '{device_source}' differs from broker profile '{ref}' "
        "source; the broker profile is authoritative — omit mqtt.source",
    )


def _structural_issues(
    item: Mapping[str, Any],
    *,
    known_broker_refs: Any,
    brokers_defined: bool,
    broker_sources: Any = None,
) -> list[dict[str, Any]]:
    """Shape checks shared by the telemetry and control validators."""

    issues: list[dict[str, Any]] = []

    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        issues.append(
            _issue("error", "name_missing", "name must be a non-empty string")
        )

    mqtt = item.get("mqtt")
    if not isinstance(mqtt, Mapping):
        issues.append(_issue("error", "mqtt_missing", "mqtt must be an object"))
    else:
        topic_family = mqtt.get("topic_family")
        if not isinstance(topic_family, str) or not topic_family.strip():
            issues.append(
                _issue(
                    "error",
                    "topic_family_missing",
                    "mqtt.topic_family must be a non-empty string",
                )
            )

    if not _has_device_identifier(item):
        issues.append(
            _issue(
                "error",
                "device_identifier_missing",
                "a device identifier is required "
                "(mqtt.device_id, serial_number or device_id)",
            )
        )

    explicit_ref = None
    if isinstance(mqtt, Mapping):
        ref = mqtt.get("broker_ref")
        if isinstance(ref, str) and ref.strip():
            explicit_ref = ref.strip()
    if explicit_ref is not None and known_broker_refs is not None:
        if explicit_ref not in set(known_broker_refs):
            issues.append(
                _issue(
                    "error",
                    "broker_ref_unknown",
                    f"mqtt.broker_ref '{explicit_ref}' is not a configured "
                    "zendure_mqtt.brokers profile",
                )
            )
    elif explicit_ref is None and brokers_defined:
        issues.append(
            _issue(
                "warning",
                "broker_ref_missing",
                "mqtt.broker_ref should be set when zendure_mqtt.brokers is "
                "configured so the connection method is explicit",
            )
        )

    mismatch = _source_mismatch_issue(item, broker_sources)
    if mismatch is not None:
        issues.append(mismatch)

    issues.extend(_power_write_profile_metadata_issues(item))

    return issues


def _entry_type_issue(item: Any) -> list[dict[str, Any]] | None:
    if not isinstance(item, Mapping):
        return [_issue("error", "device_not_object", "device entry must be an object")]
    if _entry_type(item) != ZENDURE_MQTT_TYPE:
        return [
            _issue(
                "error",
                "not_zendure_mqtt",
                f"device type must be '{ZENDURE_MQTT_TYPE}'",
            )
        ]
    return None


def validate_zendure_mqtt_device_config(
    item: Any,
    *,
    known_broker_refs: Any = None,
    brokers_defined: bool = False,
    broker_sources: Any = None,
) -> list[dict[str, Any]]:
    """Validate a Zendure MQTT entry without output control; empty means valid.

    Each issue is ``{"severity", "code", "message"}`` with ``severity`` in
    ``error``/``warning``. Messages never contain secret MQTT field values. A
    ``capabilities.write_output_limit=true`` entry is a *control* entry, not a
    telemetry one, and is reported here as ``write_output_limit_unsupported``;
    use :func:`validate_zendure_mqtt_control_device_config` for control entries.
    """

    type_issue = _entry_type_issue(item)
    if type_issue is not None:
        return type_issue

    issues = _structural_issues(
        item,
        known_broker_refs=known_broker_refs,
        brokers_defined=brokers_defined,
        broker_sources=broker_sources,
    )

    if _write_output_limit_requested(item):
        issues.append(
            _issue(
                "error",
                "write_output_limit_unsupported",
                "capabilities.write_output_limit=true is a control entry, not "
                "telemetry-only",
            )
        )

    return issues


def validate_zendure_mqtt_control_device_config(
    item: Any,
    *,
    known_broker_refs: Any = None,
    brokers_defined: bool = False,
    broker_sources: Any = None,
) -> list[dict[str, Any]]:
    """Validate a control (write-capable) Zendure MQTT entry; empty means valid.

    Shares the structural checks with the telemetry validator and additionally
    requires the entry to actually opt in to control and be write-addressable: an
    explicit ``mqtt.device_id`` route id (never the physical serial), plus a
    ``mqtt.product_key`` for the canonical profile topic or an explicit
    ``mqtt.write_topic`` for a custom write. When ``broker_sources`` is supplied a
    device that overrides its broker profile's transport source is rejected so
    device config can never select a different write gate than the broker profile;
    the resolved source is also the broker-source capability axis, and an entry
    whose source cannot be resolved is refused rather than assumed controllable.
    """

    type_issue = _entry_type_issue(item)
    if type_issue is not None:
        return type_issue

    issues = _structural_issues(
        item,
        known_broker_refs=known_broker_refs,
        brokers_defined=brokers_defined,
        broker_sources=broker_sources,
    )

    if not _write_output_limit_requested(item):
        issues.append(
            _issue(
                "error",
                "control_not_requested",
                "capabilities.write_output_limit=true is required for a control "
                "device",
            )
        )

    if zendure_mqtt_route_device_id(item) is None:
        issues.append(
            _issue(
                "error",
                "mqtt_device_id_missing",
                "mqtt.device_id is required for output-control writes; the physical "
                "serial_number is not an MQTT route identifier",
            )
        )

    product_key = zendure_mqtt_product_key(item)
    if product_key is None and zendure_mqtt_write_topic(item) is None:
        issues.append(
            _issue(
                "error",
                "write_target_missing",
                "mqtt.product_key or mqtt.write_topic is required to address "
                "output-control writes",
            )
        )
    elif product_key is None and _profile_backed_control(item):
        issues.append(
            _issue(
                "error",
                "write_target_missing",
                "mqtt.product_key is required to address the canonical profile "
                "write topic",
            )
        )

    issues.extend(
        _control_write_capability_issues(
            item, zendure_mqtt_effective_broker_source(item, broker_sources)
        )
    )

    return issues


# Stable messages for the machine-readable capability block reasons.
_BLOCK_REASON_MESSAGES = {
    "hardware_profile_deferred": (
        "this Zendure model is not yet validated for power control and stays "
        "telemetry-only"
    ),
    "transport_write_not_implemented": (
        "the pinned hardware_profile has no implemented MQTT write route"
    ),
    # Retired reason, kept so an upgraded config carrying it still explains itself.
    "transport_incompatible": (
        "the pinned hardware_profile is not compatible with this device's "
        "topic_family transport"
    ),
    "broker_source_unknown": (
        "the broker profile this control device references declares no known "
        "connection source, so no write route can be authorized"
    ),
    "broker_source_write_unverified": (
        "output control over this MQTT broker source is not verified for the "
        "telemetry this device reports; it stays telemetry-only"
    ),
}


def _control_write_capability_issues(
    item: Mapping[str, Any], broker_source: str | None
) -> list[dict[str, Any]]:
    """Require a verified write method before a control device may publish.

    A pinned ``hardware_profile`` is the primary authority: it must be known,
    writable and carry an implemented write route, and ``broker_source`` must be
    a proven carrier for that route. Without a profile, only an explicit
    supported ``mqtt.write_protocol`` (the operator-verified custom escape hatch)
    authorizes a write — a bare topic family never does.
    """

    from ems.mqtt_control.power_capability import resolve_power_write_capability
    from ems.mqtt_control.zendure_profiles import hardware_profile_by_name
    from ems.zendure_mqtt.write_protocols import (
        PROTOCOL_CUSTOM_PROPERTIES_WRITE,
        resolve_write_protocol,
    )

    topic_family = zendure_mqtt_topic_family(item)
    hardware_profile = zendure_mqtt_hardware_profile(item)
    if hardware_profile is not None:
        if hardware_profile_by_name(hardware_profile) is None:
            return [
                _issue(
                    "error",
                    "hardware_profile_unknown",
                    f"unknown Zendure hardware_profile {hardware_profile!r}: choose "
                    "a supported model profile",
                )
            ]
        cap = resolve_power_write_capability(
            topic_family=topic_family,
            hardware_profile=hardware_profile,
            broker_source=broker_source,
        )
        if cap.supported:
            # A pinned model publishes to its canonical topic; a stored
            # mqtt.write_topic is dead config. Warn (never block: the device is
            # writable) so Admin/migration can normalize it away.
            if (
                zendure_mqtt_write_topic(item) is not None
                and zendure_mqtt_product_key(item) is not None
            ):
                return [
                    _issue(
                        "warning",
                        "profile_write_topic_obsolete",
                        "mqtt.write_topic is ignored for a pinned hardware_profile: "
                        "control uses the canonical "
                        "iot/<productKey>/<deviceId>/properties/write topic. Remove "
                        "the obsolete override.",
                    )
                ]
            return []
        code = cap.block_reason or "write_protocol_unsupported"
        return [_issue("error", code, _BLOCK_REASON_MESSAGES.get(code, code))]

    explicit = zendure_mqtt_write_protocol(item)
    protocol = resolve_write_protocol(topic_family, explicit)
    if protocol is None:
        return [
            _issue(
                "error",
                "write_protocol_unsupported",
                "no supported MQTT output write method: pin a supported "
                "hardware_profile or set a supported mqtt.write_protocol",
            )
        ]
    if protocol == PROTOCOL_CUSTOM_PROPERTIES_WRITE:
        write_topic = zendure_mqtt_write_topic(item)
        if write_topic is None:
            return [
                _issue(
                    "error",
                    "write_topic_required",
                    "mqtt.write_topic is required for the custom_properties_write "
                    "protocol",
                )
            ]
        from ems.zendure_mqtt.write_protocols import publish_topic_error

        topic_error = publish_topic_error(write_topic)
        if topic_error is not None:
            return [
                _issue(
                    "error",
                    "write_topic_invalid",
                    "mqtt.write_topic must be a valid publish topic (no MQTT "
                    f"wildcards, NUL or empty value): {topic_error}",
                )
            ]
    return []


@dataclass(frozen=True)
class ZendureMqttBrokerProfileView:
    """Sanitized usability view of one broker profile, safe to log/surface.

    Carries no credential value: ``has_auth`` only records whether an external
    credential reference (or a legacy inline secret) is present, never the
    secret itself.
    """

    ref: str
    enabled: bool
    host: str | None
    port: int | None
    source: str | None
    has_auth: bool

    @property
    def endpoint(self) -> str | None:
        return f"{self.host}:{self.port}" if self.host and self.port else None

    @property
    def usable(self) -> bool:
        return self.usability_issue() is None

    def usability_issue(self) -> str | None:
        """Sanitized issue code when this profile cannot back a device, else None.

        The incomplete check comes first: a profile without an endpoint is
        incomplete, not disabled, because the telemetry feature has no opt-out.
        """

        if not (self.host and self.port):
            return "zendure_mqtt_broker_ref_incomplete"
        if not self.enabled:
            return "zendure_mqtt_broker_ref_disabled"
        if self.source not in SUPPORTED_BROKER_SOURCES:
            return "zendure_mqtt_broker_ref_incomplete"
        if self.source == SOURCE_ZENDURE_CLOUD_MQTT and not self.has_auth:
            return "zendure_mqtt_broker_auth_missing"
        return None


def _profile_has_auth(profile: Mapping[str, Any]) -> bool:
    ref = profile.get("credentials_ref")
    if isinstance(ref, str) and ref.strip():
        return True
    return any(profile.get(key) for key in _INLINE_AUTH_KEYS)


def _profile_port(profile: Mapping[str, Any], tls: bool | None = None) -> int:
    """Return the canonical broker port or raise for malformed metadata."""

    from ems.config import (
        default_mqtt_port,
        parse_mqtt_port,
        resolve_mqtt_tls_metadata,
    )

    if tls is None:
        tls, _ = resolve_mqtt_tls_metadata(
            tls_mode=profile.get("tls_mode"),
            tls=profile.get("tls"),
            tls_insecure=profile.get("tls_insecure"),
        )

    return parse_mqtt_port(profile.get("port"), default=default_mqtt_port(tls))


def _profile_view_port(profile: Mapping[str, Any]) -> int | None:
    try:
        return _profile_port(profile)
    except ValueError:
        return None


def normalized_broker_identity(profile: Any) -> tuple | None:
    """Secret-free connection-profile identity of a broker endpoint.

    Two brokers with the same identity address the same physical broker and may
    share one profile; differing identities must never be merged onto one ref.
    The identity covers source, normalized host, resolved port, TLS mode and the
    non-secret credential reference. It never includes credential contents, so
    it is safe to hash into a display ref. Returns ``None`` when
    no host is known, since identity cannot then be established. Shared by Admin
    proposal building, config preview and Core so one normalization rule governs
    broker equality everywhere.
    """

    if not isinstance(profile, Mapping):
        return None
    host = _normalized_host(profile.get("host"))
    if host is None:
        return None
    from ems.config import resolve_mqtt_tls_metadata

    tls, tls_insecure = resolve_mqtt_tls_metadata(
        tls_mode=profile.get("tls_mode"),
        tls=profile.get("tls"),
        tls_insecure=profile.get("tls_insecure"),
    )
    return (
        _normalized(profile.get("source")),
        host.lower(),
        _profile_port(profile, tls),
        tls,
        tls_insecure,
        str(profile.get("credentials_ref") or "").strip().lower() or None,
    )


def _broker_ref_slug(value: Any) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return slug or "broker"


def stable_local_broker_ref(identity: tuple) -> str:
    """Deterministic local broker ref for a secret-free connection identity.

    ``identity`` is a :func:`normalized_broker_identity` tuple. The same identity
    always yields the same ref and different endpoints yield different refs,
    independent of how many brokers were discovered or in what order — so a
    broker's ref never changes merely because another broker appears. The hash is
    taken over the full structured identity, so two hosts that slug-collide after
    normalization still get distinct refs. No credential is part of ``identity``,
    so a ref can never leak a secret.
    """

    raw = "\x1f".join("" if part is None else str(part) for part in identity)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    host = identity[1] if len(identity) > 1 else None
    return f"{LOCAL_BROKER_REF}_{_broker_ref_slug(host)}_{digest}"


def _safe_profile_enabled(value: Any, *, default: bool) -> bool:
    """Strict enable-flag read for a diagnostic broker view.

    Invalid types resolve to ``False`` (never trusted as enabled); the runtime
    path is responsible for surfacing the same value as a sanitized config error.
    """

    from ems.config import optional_json_bool

    try:
        return optional_json_bool(value, "enabled", default=default)
    except ValueError:
        return False


# Where an effective broker profile came from: the legacy top-level single-broker
# block (mapped to the implicit ``default`` ref) or a named ``brokers`` entry.
ORIGIN_LEGACY_DEFAULT = "legacy_default"
ORIGIN_NAMED_PROFILE = "named_profile"


@dataclass(frozen=True)
class EffectiveMqttBrokerProfile:
    """One broker profile the EMS runtime would build from ``zendure_mqtt``.

    ``config`` is the raw profile mapping (the top-level block for the legacy
    default, a ``brokers`` entry for a named profile), never a copy, so callers
    read credential/source fields exactly as configured.
    """

    broker_ref: str
    config: Mapping[str, Any]
    origin: str


def _has_named_brokers(zmqtt: Any) -> bool:
    brokers = zmqtt.get("brokers") if isinstance(zmqtt, Mapping) else None
    return isinstance(brokers, Mapping) and bool(brokers)


def legacy_default_broker_present(zmqtt: Any) -> bool:
    """True when the implicit ``default`` broker exists for a ``zendure_mqtt`` block.

    The one rule the runtime, the effective-profile resolver and the diagnostic
    views all share: a legacy single-broker install configures its broker in the
    top-level fields, so the ``default`` profile exists whenever the block has a
    host or declares no named brokers at all.
    """

    if not isinstance(zmqtt, Mapping):
        return False
    host = zmqtt.get("host")
    return (isinstance(host, str) and bool(host.strip())) or not _has_named_brokers(
        zmqtt
    )


def iter_effective_mqtt_broker_profiles(config: Any):
    """Yield every broker profile the runtime resolves from a full EMS config.

    The single Core resolver shared by the runtime, the credential-consumer
    scanner and config validation so broker resolution is defined once: the
    implicit legacy ``default`` profile (see :func:`legacy_default_broker_present`)
    followed by each named ``brokers`` entry.
    """

    zmqtt = config.get("zendure_mqtt") if isinstance(config, Mapping) else None
    if not isinstance(zmqtt, Mapping):
        return
    if legacy_default_broker_present(zmqtt):
        yield EffectiveMqttBrokerProfile(DEFAULT_BROKER_REF, zmqtt, ORIGIN_LEGACY_DEFAULT)
    if _has_named_brokers(zmqtt):
        for ref, profile in zmqtt["brokers"].items():
            # A named profile using a reserved ref (``default``) is invalid and is
            # never resolved: skipping it here guarantees the legacy default is
            # never silently overwritten by a same-named named profile.
            if str(ref) in RESERVED_MQTT_BROKER_REFS:
                continue
            if isinstance(profile, Mapping):
                yield EffectiveMqttBrokerProfile(
                    str(ref), profile, ORIGIN_NAMED_PROFILE
                )


def effective_broker_enabled(profile: EffectiveMqttBrokerProfile) -> bool:
    """Whether an effective broker profile is active, matching the runtime.

    The telemetry feature has no opt-out, so the legacy default is active exactly
    when it has a broker host; a named profile honours its own ``enabled`` flag.
    Shared by the runtime grid-meter resolver and the credential scanner so both
    agree on which brokers a grid meter may bind to.
    """

    prof = profile.config
    if profile.origin == ORIGIN_LEGACY_DEFAULT:
        host = prof.get("host")
        return isinstance(host, str) and bool(host.strip())
    return _safe_profile_enabled(prof.get("enabled"), default=True)


def get_effective_mqtt_broker_profile(config, broker_ref):
    """Return the effective broker profile for ``broker_ref``, or ``None``.

    The single lookup shared by the runtime grid-meter resolver, the credential
    scanner and diagnostics so a ref always resolves to the same profile
    everywhere. ``default`` resolves to the implicit legacy top-level broker (see
    :func:`iter_effective_mqtt_broker_profiles`), independent of whether other
    named brokers exist.
    """

    for profile in iter_effective_mqtt_broker_profiles(config):
        if profile.broker_ref == broker_ref:
            return profile
    return None


def effective_mqtt_broker_profile_map(config):
    """Return ``{broker_ref: EffectiveMqttBrokerProfile}`` with unique refs.

    The reserved-ref skip in :func:`iter_effective_mqtt_broker_profiles` keeps the
    mapping collision-free: the legacy ``default`` is the only holder of that ref,
    so a named ``default`` can never overwrite it here either.
    """

    return {
        profile.broker_ref: profile
        for profile in iter_effective_mqtt_broker_profiles(config)
    }


def find_reserved_mqtt_broker_ref_issues(config):
    """Report named ``zendure_mqtt.brokers`` profiles using a reserved ref.

    Each issue is ``{severity, code, message, path}`` with the stable code
    ``mqtt_broker_ref_reserved`` and the offending config path. Only the reserved
    ref name leaves this function — never a host, username or credential value.
    """

    zmqtt = config.get("zendure_mqtt") if isinstance(config, Mapping) else None
    brokers = zmqtt.get("brokers") if isinstance(zmqtt, Mapping) else None
    if not isinstance(brokers, Mapping):
        return []
    issues = []
    for ref in brokers:
        if str(ref) in RESERVED_MQTT_BROKER_REFS:
            issues.append(
                {
                    "severity": "error",
                    "code": "mqtt_broker_ref_reserved",
                    "message": (
                        f"zendure_mqtt.brokers.{ref} uses the reserved broker ref "
                        f"'{ref}'; it belongs to the implicit legacy top-level "
                        "broker. Rename the named profile (e.g. home, local, "
                        "secondary, cloud)."
                    ),
                    "path": f"zendure_mqtt.brokers.{ref}",
                }
            )
    return issues


def broker_profile_view(
    profile: EffectiveMqttBrokerProfile,
) -> ZendureMqttBrokerProfileView:
    """Sanitized usability view of one effective broker profile.

    The single mapping from a Core :class:`EffectiveMqttBrokerProfile` to a
    secret-free diagnostic view. Enablement comes from
    :func:`effective_broker_enabled` so views agree with the runtime; the legacy
    default's source falls back to ``local_mqtt`` when unset (a named profile
    keeps its own source, which the runtime validates).
    """

    prof = profile.config
    source = _normalized(prof.get("source"))
    if profile.origin == ORIGIN_LEGACY_DEFAULT and source is None:
        source = SOURCE_LOCAL_MQTT
    return ZendureMqttBrokerProfileView(
        ref=profile.broker_ref,
        enabled=effective_broker_enabled(profile),
        host=_normalized_host(prof.get("host")),
        port=_profile_view_port(prof),
        source=source,
        has_auth=_profile_has_auth(prof),
    )


def zendure_mqtt_broker_profile_views(
    raw: Any,
) -> dict[str, ZendureMqttBrokerProfileView]:
    """Return usability views per broker ref from the ``zendure_mqtt`` block.

    Derived directly from the one Core resolver
    (:func:`iter_effective_mqtt_broker_profiles`) so diagnostics see exactly the
    broker profiles — same refs, same hosts — that the runtime, the credential
    scanner and the grid-meter resolver see. There is no second iteration over
    ``zendure_mqtt.brokers`` here: a named profile using the reserved ``default``
    ref is dropped by the resolver, so it can never overwrite the legacy
    top-level default in these views. Old single-broker configs still resolve to
    an implicit ``default`` view from their top-level fields.
    """

    return {
        profile.broker_ref: broker_profile_view(profile)
        for profile in iter_effective_mqtt_broker_profiles({"zendure_mqtt": raw})
    }


def _normalized_host(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
# URL structure that must never appear in a bare broker host: userinfo, a
# scheme separator, a path, a query or a fragment.
_HOST_DISALLOWED_CHARS = frozenset("@/\\?#")


def is_valid_mqtt_broker_host(value: Any) -> bool:
    """True only for a bare hostname or IP literal (v4/v6, optionally bracketed).

    This is the authoritative broker-host contract for the EMS Core, reused by
    diagnostics so a credential or URL can never be smuggled through the
    ``host`` field into an endpoint string that a report or log echoes. Any URL
    structure — userinfo (``user:pass@host``), a scheme (``mqtt://host``), a
    path/query/fragment, embedded whitespace or a control character — is
    rejected. A plain hostname, ``192.168.20.10`` and a bracketed ``[::1]`` IPv6
    literal stay valid.
    """

    if not isinstance(value, str) or not value or len(value) > 253:
        return False
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return False
    if any(ch in _HOST_DISALLOWED_CHARS for ch in value):
        return False
    literal = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    try:
        ipaddress.ip_address(literal)
        return True
    except ValueError:
        pass
    # A colon is legitimate only inside an IP literal, which already failed to
    # parse above; a hostname never contains one.
    if ":" in value:
        return False
    hostname = value[:-1] if value.endswith(".") else value
    return bool(hostname) and all(
        _HOST_LABEL_RE.fullmatch(label) for label in hostname.split(".")
    )


def format_mqtt_endpoint(host: Any, port: Any) -> str | None:
    """Render a ``host:port`` endpoint, bracketing a bare IPv6 literal.

    Returns ``None`` when the host is not a valid bare hostname/IP so a caller
    never emits an endpoint built from a credential-bearing host.
    """

    if not is_valid_mqtt_broker_host(host):
        return None
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{port}"


def find_zendure_mqtt_broker_profile_issues(config: Any) -> list[dict[str, Any]]:
    """Report enabled Zendure MQTT devices whose broker profile is unusable.

    Each issue is ``{"severity", "code", "message"}`` using the sanitized codes
    ``zendure_mqtt_broker_ref_unknown``/``_disabled``/``_incomplete`` and
    ``zendure_mqtt_broker_auth_missing``. Messages carry only the device index
    and the broker ref label; never serials, device ids, hosts or credentials.
    """

    if not isinstance(config, Mapping):
        return []
    devices = config.get("devices")
    if not isinstance(devices, list):
        return []

    views = zendure_mqtt_broker_profile_views(config.get("zendure_mqtt"))
    issues: list[dict[str, Any]] = []
    for index, item in enumerate(devices):
        if not is_zendure_mqtt_device_config(item):
            continue
        if not config_entry_enabled(item):
            continue
        ref = zendure_mqtt_broker_ref(item)
        # The implicit ``default`` broker is the legacy single-broker path; its
        # enablement/host is validated by the runtime-config diagnostics, so it
        # is not subjected to the strict explicit-broker usability rules here.
        if ref == DEFAULT_BROKER_REF:
            continue
        view = views.get(ref)
        if view is None:
            issues.append(
                _issue(
                    "error",
                    "zendure_mqtt_broker_ref_unknown",
                    f"devices.{index} references broker profile '{ref}', "
                    "which is not configured",
                )
            )
            continue
        code = view.usability_issue()
        if code is not None:
            issues.append(
                _issue("error", code, _broker_issue_message(index, ref, code))
            )
    return issues


_BROKER_ISSUE_REASONS = {
    "zendure_mqtt_broker_ref_disabled": "is disabled",
    "zendure_mqtt_broker_ref_incomplete": "is incomplete (missing host/port or source)",
    "zendure_mqtt_broker_auth_missing": "has no external credential reference",
}


def _broker_issue_message(index: int, ref: str, code: str) -> str:
    reason = _BROKER_ISSUE_REASONS.get(code, "is not usable")
    return f"devices.{index} references broker profile '{ref}', which {reason}"
