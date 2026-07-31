# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared Admin-side mapping for Zendure MQTT device drafts.

Single source of truth for both Fresh Install and Maintenance: it maps the
user-facing Zendure hardware generation to the internal ``topic_family`` /
``base_topic``, builds device config fragments, sanitizes untrusted proposal
fragments, and validates entries with the EMS-owned validators.

Secret-free by construction: no broker password/token/app key is ever copied
into a device fragment, and raw topic-family names never leave this module as
user-facing labels. Output control is capability-gated by the shared EMS helper
(:func:`ems.zendure_mqtt.capability.mqtt_output_control_capability`): it is
enabled only for a topic family with a verified write method, so a forged or
unsupported control request is downgraded to telemetry-only.
"""

import copy
import re
from collections.abc import Mapping

from admin.device_common_fields import (
    apply_common_device_values,
    common_device_draft_values,
)
from ems.config_catalog import ZENDURE_MQTT_GENERATIONS
from ems.zendure_mqtt.capability import mqtt_output_control_capability
from ems.zendure_mqtt.config_entries import (
    is_control_zendure_mqtt_device_config,
    validate_zendure_mqtt_control_device_config,
    validate_zendure_mqtt_device_config,
    zendure_mqtt_broker_ref,
    zendure_mqtt_hardware_profile,
    zendure_mqtt_source,
)
from ems.zendure_mqtt.topics import (
    FAMILY_LEGACY_JSON,
    FAMILY_LEGACY_JSON_ALT,
    FAMILY_ZENDURE_CLOUD_SCALAR,
    FAMILY_ZENSDK_HA_SCALAR,
)

# Server-only marker set once a draft entry's MQTT connection resolved against
# current trusted discovery state. Never accepted from browser JSON.
TRUSTED_CONNECTION_SELECTION_FIELD = "trusted_connection_selection"


def _draft_hardware_model(item):
    """Concrete registry hardware model pinned on a draft entry, or ``""``.

    A generation never authorizes control; only a concrete registry model does.
    The established concrete-model field is ``power_hardware_profile``. Newer
    normalized drafts carry the exact model in ``hardware_profile`` and signal
    that format with an explicit ``hardware_generation`` key — only then is
    ``hardware_profile`` read as a model, because older drafts overload
    ``hardware_profile`` as the display *generation* (and the generation id
    ``solarflow_zensdk`` collides with a registry model name).
    """

    from ems.mqtt_control.zendure_profiles import hardware_profile_by_name

    if not isinstance(item, dict):
        return ""
    candidates = [item.get("hardware_model"), item.get("power_hardware_profile")]
    # Compatibility with the short-lived normalized draft format which used
    # hardware_profile for the model only when hardware_generation was present.
    if "hardware_generation" in item:
        candidates.append(item.get("hardware_profile"))
    # Older config-shaped drafts may carry the canonical model directly.
    candidates.append(item.get("hardware_profile"))
    for value in candidates:
        text = str(value or "").strip()
        if text and hardware_profile_by_name(text) is not None:
            return text
    return ""


def _draft_hardware_generation(item):
    """Display/telemetry generation from normalized or legacy draft fields."""

    if not isinstance(item, dict):
        return ""
    # A legacy browser posts its edited generation in hardware_profile. Prefer
    # that recognized generation even when a newer server also supplied a
    # hardware_generation field in the loaded draft.
    legacy = str(item.get("hardware_profile") or "").strip()
    if generation_profile(legacy) is not None:
        return legacy
    for key in ("hardware_generation", "generation"):
        value = str(item.get(key) or "").strip()
        if generation_profile(value) is not None:
            return value
    model = _draft_hardware_model(item)
    if model:
        from ems.mqtt_control.zendure_profiles import hardware_profile_by_name

        profile = hardware_profile_by_name(model)
        if profile is not None:
            return profile.hardware_generation
    return ""


def normalize_zendure_mqtt_draft(item):
    """Return the normalized, secret-free browser/API hardware DTO.

    Legacy overloaded fields remain readable, but new drafts always separate the
    display generation from the exact registry model. The write profile is
    derived from the Core registry and cannot be supplied by the browser.
    """

    normalized = copy.deepcopy(item) if isinstance(item, dict) else {}
    generation = _draft_hardware_generation(normalized)
    model = _draft_hardware_model(normalized)
    from ems.mqtt_control.zendure_profiles import (
        hardware_profile_by_name,
        hardware_profile_matches_generation,
    )

    profile = hardware_profile_by_name(model) if model else None
    if profile is None or not hardware_profile_matches_generation(profile, generation):
        model = ""
        profile = None
    normalized.pop("hardware_profile", None)
    normalized.pop("power_hardware_profile", None)
    normalized.pop("generation", None)
    normalized["hardware_generation"] = generation
    normalized["hardware_model"] = model
    normalized["power_write_profile"] = (
        profile.power_write_profile if profile is not None else None
    )
    return normalized


# Secret key fragments stripped from any untrusted proposal fragment, and the
# identifier keys that must never carry a display-masked value into config.
SECRET_KEY_FRAGMENTS = (
    "password",
    "passphrase",
    "token",
    "secret",
    "credential",
    "app_key",
    "apikey",
    "username",
)
MASKED_IDENTITY_KEYS = ("product_key", "device_key", "device_id", "serial_number")

# Display-mask markers (…abcd / ••••). A value carrying either is not a usable
# identifier and must never reach config.
_MASK_MARKERS = ("•", "…")

# Reverse map: an internal topic family resolves back to the user-facing
# generation id so an existing device renders with a friendly label instead of a
# raw family name. The leading-slash "alt" JSON layout is generation-ambiguous
# (new ZenSDK devices publish it via the cloud broker too) and is only flagged,
# never resolved to a generation on its own.
_TOPIC_FAMILY_GENERATION = {
    profile["topic_family"]: gen_id
    for gen_id, profile in ZENDURE_MQTT_GENERATIONS.items()
}
_ALT_TOPIC_FAMILY = FAMILY_LEGACY_JSON_ALT

# Model-name matching order: the hub/hyper line tokens are unambiguous, while
# "solarflow" is the family brand that also prefixes legacy hub names
# ("SolarFlow Hub 2000"), so the legacy generation must be checked first.
_MODEL_MATCH_ORDER = ("hub_hyper_legacy", "solarflow_zensdk", "zendure_cloud")

# Neutral, display-only telemetry-schema names per internal topic family. The
# schema (which parser reads the payload) is deliberately a separate derivation
# from the hardware generation; stored configs keep the internal family values.
TELEMETRY_SCHEMAS = {
    FAMILY_ZENSDK_HA_SCALAR: "zendure_scalar_topics",
    FAMILY_LEGACY_JSON: "zendure_json_report",
    FAMILY_LEGACY_JSON_ALT: "zendure_json_report_leading_slash",
    FAMILY_ZENDURE_CLOUD_SCALAR: "zendure_cloud_scalar_topics",
}


def _issue(code, message):
    return {"code": code, "message": message}


def _is_masked_identifier(value):
    return isinstance(value, str) and any(marker in value for marker in _MASK_MARKERS)


# Draft states of an editable, non-secret field. A key the browser never sent is
# not an edit, while a key it sent empty is an explicit clear.
_KEEP = "keep"
_SET = "set"
_CLEAR = "clear"


def _editable_draft_value(key, *containers):
    """Classify an editable draft field as keep/set/clear.

    A masked display placeholder resolves to keep: the browser is never given a
    redacted cloud identifier, so it can never resubmit one and must not be able
    to erase the stored value by echoing the mask back.
    """

    present = False
    for container in containers:
        if not isinstance(container, dict) or key not in container:
            continue
        present = True
        value = str(container.get(key) or "").strip()
        if _is_masked_identifier(value):
            return _KEEP, ""
        if value:
            return _SET, value
    return (_CLEAR, "") if present else (_KEEP, "")


def _store_editable_value(target, key, state, value):
    """Write a keep/set/clear decision onto a config mapping."""

    if state == _SET:
        target[key] = value
    elif state == _CLEAR and str(target.get(key) or "").strip():
        target.pop(key, None)


def generation_catalog():
    """User-facing Zendure hardware generations for the UI (no internal names)."""

    return [
        {
            "id": gen_id,
            "label": profile["label"],
            "description": profile["description"],
            "product_key": profile["product_key"],
            "default": profile["default"],
            # Whether output control can be enabled for this generation's topic
            # family. The UI offers the control option only when this is true.
            "supports_output_control": generation_supports_output_control(gen_id),
        }
        for gen_id, profile in ZENDURE_MQTT_GENERATIONS.items()
    ]


def zendure_hardware_profile_options():
    """Concrete hardware-model selector options for Admin (registry-derived).

    ``hardware_generation`` is a telemetry/display grouping (``generation_catalog``
    above); this is the separate, exact registry model selector — the authority
    for control. Both are normalized, distinct fields.
    """

    from ems.mqtt_control.zendure_profiles import hardware_profile_selector_options

    return hardware_profile_selector_options()


def generation_profile(generation_id):
    return ZENDURE_MQTT_GENERATIONS.get(str(generation_id or "").strip())


def generation_label(generation_id):
    profile = generation_profile(generation_id)
    return profile["label"] if profile is not None else ""


def hardware_profile_for_topic_family(topic_family):
    """Return ``(generation_id, alternative_layout)`` for an internal family.

    A topic family names the observed telemetry schema, never the hardware
    generation. ``generation_id`` is ``None`` for an unrecognized family and for
    the internal leading-slash JSON layout: that layout is published by new
    ZenSDK devices via the cloud broker as well as by older hardware, so it is
    only flagged as ``alternative_layout`` — callers with model evidence use
    :func:`resolve_hardware_generation` instead.
    """

    family = str(topic_family or "").strip()
    if family == _ALT_TOPIC_FAMILY:
        return None, True
    return _TOPIC_FAMILY_GENERATION.get(family), False


def hardware_generation_for_model(model_hint):
    """Generation id inferred from a product model name; ``None`` when unknown.

    Matches whole name tokens (never substrings) against the catalog's
    per-generation ``model_keywords`` so e.g. "SolarFlow Hub 2000" resolves to
    the hub generation, not to the SolarFlow brand.
    """

    tokens = set(re.split(r"[^a-z0-9]+", str(model_hint or "").strip().lower()))
    tokens.discard("")
    if not tokens:
        return None
    for gen_id in _MODEL_MATCH_ORDER:
        profile = ZENDURE_MQTT_GENERATIONS.get(gen_id)
        if profile is None:
            continue
        if any(keyword in tokens for keyword in profile.get("model_keywords", ())):
            return gen_id
    return None


def resolve_hardware_generation(topic_family, model_hint=None):
    """Return ``(generation_id, alternative_layout)`` with model evidence first.

    The product model identifies the hardware generation whenever it is known;
    the topic family stays a schema-only fallback and the ambiguous leading-slash
    JSON layout alone never resolves to a generation.
    """

    family = str(topic_family or "").strip()
    alternative_layout = family == _ALT_TOPIC_FAMILY
    generation = hardware_generation_for_model(model_hint)
    if generation is None:
        generation, alternative_layout = hardware_profile_for_topic_family(family)
    return generation, alternative_layout


def telemetry_schema_for_topic_family(topic_family):
    """Neutral display-only schema name for an internal family (or ``None``)."""

    return TELEMETRY_SCHEMAS.get(str(topic_family or "").strip())


def _is_secret_key(key):
    lowered = str(key).lower()
    return any(fragment in lowered for fragment in SECRET_KEY_FRAGMENTS)


def _strip_secrets(value):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if _is_secret_key(key):
                continue
            if key in MASKED_IDENTITY_KEYS and _is_masked_identifier(item):
                continue
            cleaned[key] = _strip_secrets(item)
        return cleaned
    if isinstance(value, list):
        return [_strip_secrets(item) for item in value]
    return value


def _enforce_output_control_capability(entry):
    """Downgrade output control unless the topic family has a verified write method.

    The device may request ``write_output_limit`` only when its topic family (and
    any explicit write protocol) resolves to a supported write method; otherwise
    it is forced telemetry-only and the write protocol is dropped. This is the
    capability-based enforcement point for untrusted/forged fragments.
    """

    if not isinstance(entry, dict):
        return
    capabilities = entry.get("capabilities")
    mqtt = entry.get("mqtt") if isinstance(entry.get("mqtt"), dict) else {}
    requested = (
        isinstance(capabilities, dict)
        and capabilities.get("write_output_limit") is True
    )
    capability = mqtt_output_control_capability(
        topic_family=mqtt.get("topic_family"),
        hardware_profile=zendure_mqtt_hardware_profile(entry),
        write_protocol=mqtt.get("write_protocol"),
    )
    control = requested and capability.supported
    if isinstance(capabilities, dict):
        capabilities["write_output_limit"] = control
    if isinstance(entry.get("mqtt"), dict):
        # A profile-based device pins ``hardware_profile`` (not a write protocol);
        # the explicit custom escape hatch keeps its write protocol.
        if control and capability.write_protocol:
            entry["mqtt"]["write_protocol"] = capability.write_protocol
        elif not control:
            entry["mqtt"].pop("write_protocol", None)


def sanitize_zendure_mqtt_fragment(value):
    """Deep-copy an untrusted fragment, dropping secrets and capability-gating writes.

    Any secret-looking key is removed and a masked identifier value is dropped.
    Output control is preserved only when the fragment's topic family has a
    verified write method (see the shared EMS capability rule); an unsupported or
    forged control request is downgraded to telemetry-only so a hostile fragment
    can never enable writes on a family that cannot safely accept them.
    """

    cleaned = _strip_secrets(value)
    _enforce_output_control_capability(cleaned)
    return cleaned


def validate_zendure_mqtt_fragment(entry):
    """Validation issues as ``{code, message}`` (errors only).

    Control entries (``write_output_limit=true``) are validated with the control
    validator; telemetry-only entries with the telemetry validator.
    """

    if is_control_zendure_mqtt_device_config(entry):
        issues = validate_zendure_mqtt_control_device_config(entry)
    else:
        issues = validate_zendure_mqtt_device_config(entry)
    return [
        _issue(issue["code"], issue["message"])
        for issue in issues
        if issue.get("severity") == "error"
    ]


def _requested_output_control(item):
    """Explicit draft control choice as a bool, or ``None`` when unmentioned."""

    if not isinstance(item, dict):
        return None
    if isinstance(item.get("output_control"), bool):
        return item["output_control"]
    capabilities = item.get("capabilities")
    if isinstance(capabilities, dict) and isinstance(
        capabilities.get("write_output_limit"), bool
    ):
        return capabilities["write_output_limit"]
    return None


def _draft_requests_output_control(item):
    """True if a manual/maintenance draft entry asks for output control."""

    return _requested_output_control(item) is True


def generation_supports_output_control(generation_id):
    """Whether a concrete model on this generation's transport could be controllable.

    Informational for the UI only: a hardware generation never authorizes a write
    (see the module contract). It reports whether the generation's *transport* is a
    JSON-report family that a concrete registry model could write over; the actual
    write is always authorized by the pinned concrete model, never the generation.
    """

    from ems.zendure_mqtt.topics import JSON_FAMILIES

    profile = generation_profile(generation_id)
    if profile is None:
        return False
    return profile["topic_family"] in JSON_FAMILIES


def build_manual_zendure_mqtt_fragment(item, broker_ref):
    """Build a device fragment from a manual UI entry.

    The user picks a friendly hardware generation, never a raw topic family; the
    generation resolves to its internal ``topic_family``/``base_topic`` here.
    Output control is enabled only when the user requests it *and* the resolved
    topic family has a verified write method (capability-based) — an unsupported
    family stays telemetry-only. Returns ``(fragment, issues)`` where ``fragment``
    is ``None`` when the entry is unusable and ``issues`` is ``{code, message}``.
    """

    if not isinstance(item, dict):
        return None, [
            _issue("zendure_mqtt_device_invalid", "Zendure MQTT device is not valid.")
        ]
    name = str(item.get("name") or "").strip()
    label = name or "Zendure MQTT device"
    # The physical serial and the MQTT route/payload device id are independent
    # identities: the serial is read only from serial_number and the route id
    # only from an explicit mqtt.device_id (or the top-level mqtt_device_id draft
    # field). Neither is ever derived from the other.
    serial = str(item.get("serial_number") or "").strip()
    item_mqtt = item.get("mqtt") if isinstance(item.get("mqtt"), dict) else {}
    route_device_id = str(
        item_mqtt.get("device_id") or item.get("mqtt_device_id") or ""
    ).strip()
    normalized = normalize_zendure_mqtt_draft(item)
    profile = generation_profile(normalized.get("hardware_generation"))
    if profile is None:
        return None, [
            _issue(
                "zendure_mqtt_generation_unknown",
                f"{label}: choose a Zendure hardware generation.",
            )
        ]
    if not serial and not route_device_id:
        return None, [
            _issue(
                "zendure_mqtt_device_identifier_missing",
                f"{label}: enter a serial number or MQTT device ID.",
            )
        ]

    mqtt = {
        "broker_ref": broker_ref,
        "topic_family": profile["topic_family"],
        "base_topic": profile["base_topic"],
    }
    if route_device_id:
        mqtt["device_id"] = route_device_id
    product_key = ""
    if profile["product_key"]:
        product_key = str(item.get("product_key") or "").strip()
        if product_key:
            mqtt["product_key"] = product_key

    wants_control = _draft_requests_output_control(item)
    # Control is authorized only by a concrete registry hardware model, never by
    # the generation. Without a model the device is added telemetry-only.
    model = normalized.get("hardware_model") or ""
    capability = (
        mqtt_output_control_capability(
            topic_family=profile["topic_family"], hardware_profile=model
        )
        if model
        else None
    )
    issues = []
    output_control = False
    hardware_profile = None
    power_write_profile = None
    if wants_control and not model:
        issues.append(
            _issue(
                "zendure_mqtt_control_requires_model",
                f"{label}: select the exact Zendure hardware model to enable output "
                "control; adding it as telemetry only.",
            )
        )
    elif wants_control and not capability.supported:
        issues.append(
            _issue(
                "zendure_mqtt_control_unavailable",
                f"{label}: output control is not available for this hardware model; "
                "adding it as telemetry only.",
            )
        )
    elif wants_control and not route_device_id:
        # A control write is addressed by the explicit MQTT route id, never the
        # physical serial; without it the entry cannot be made write-capable.
        return None, [
            _issue(
                "mqtt_device_id_missing",
                f"{label}: enter the MQTT device ID to enable output control; the "
                "physical serial number is not an MQTT route identifier.",
            )
        ]
    elif wants_control:
        # A control device must also be addressable to derive its write topic.
        if not product_key and not str(item.get("write_topic") or "").strip():
            return None, [
                _issue(
                    "zendure_mqtt_control_write_target_missing",
                    f"{label}: enter the device product key to enable output control.",
                )
            ]
        output_control = True
        hardware_profile = capability.hardware_profile
        power_write_profile = capability.power_write_profile

    fragment = {
        "type": "zendure_mqtt",
        "enabled": True,
        "name": name if "name" in item else "INV_1",
        "serial_number": serial,
        "mqtt": mqtt,
        "capabilities": {
            "read_power": True,
            "read_soc": True,
            "write_output_limit": output_control,
        },
    }
    if hardware_profile:
        fragment["hardware_profile"] = hardware_profile
        if power_write_profile:
            fragment["power_write_profile"] = power_write_profile
    return fragment, issues


def _mqtt_str(mqtt, key):
    if isinstance(mqtt, dict):
        value = mqtt.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _effective_mqtt_source(device, broker_sources):
    """Transport source a configured MQTT device uses, or ``""`` when unresolved.

    A config may omit ``mqtt.source`` because the referenced broker profile is
    the authority for the transport (and thus for the write gate). Resolving it
    here keeps the browser from having to guess from whichever discovery
    proposals happen to exist; an unresolved source stays empty so no caller can
    read a concrete transport that was never proven.
    """

    stated = zendure_mqtt_source(device)
    if stated:
        return stated
    if not isinstance(broker_sources, Mapping):
        return ""
    return str(broker_sources.get(zendure_mqtt_broker_ref(device)) or "")


def zendure_mqtt_untrusted_connection_block(item):
    """True when a draft entry carries a broker endpoint the server never resolved.

    The endpoint block is the only draft field that can provision a broker
    profile and re-home a device, and the browser can craft every byte of it.
    Callers use this to refuse it on an entry bound to a stored device.
    """

    if item.get(TRUSTED_CONNECTION_SELECTION_FIELD) is True:
        return False
    broker = item.get("broker")
    return isinstance(broker, dict) and bool(broker)


def _selected_mqtt_connection(item):
    """Concrete MQTT connection a server-resolved draft entry selects.

    ``None`` unless the server marked the entry as backed by a current trusted
    proposal, so neither an ordinary field edit nor a crafted broker block can
    re-home a stored connection: a submitted ``broker``/``mqtt`` pair is not
    proof that the connection it names was ever discovered. On a resolved entry
    the broker block is proposal-owned (the resolver rewrote it from current
    discovery) and therefore outranks the browser-editable ``mqtt`` values it
    duplicates.
    """

    if item.get(TRUSTED_CONNECTION_SELECTION_FIELD) is not True:
        return None
    mqtt = item.get("mqtt") if isinstance(item.get("mqtt"), dict) else {}
    broker = item.get("broker") if isinstance(item.get("broker"), dict) else {}
    ref = str(broker.get("ref") or mqtt.get("broker_ref") or "").strip()
    source = str(broker.get("source") or mqtt.get("source") or "").strip().lower()
    if not ref and not source:
        return None
    return (source, ref, str(mqtt.get("topic_family") or "").strip())


def zendure_mqtt_connection_switched(device, item, broker_sources=None):
    """True when a draft entry selects a different concrete MQTT connection.

    Same config type is not the same connection: an already configured MQTT
    inverter can be moved to another broker, another account or another
    transport. The trusted proposal selection — never a raw field comparison —
    is the switch signal, so route enrichment on the unchanged connection stays
    an ordinary edit. A stored config may omit ``mqtt.source`` because its
    broker profile is the authority; resolving it here keeps a reselection of
    the same transport from reading as a cloud/local change.
    """

    selected = _selected_mqtt_connection(item)
    if selected is None:
        return False
    mqtt = device.get("mqtt") if isinstance(device.get("mqtt"), dict) else {}
    stored = (
        _effective_mqtt_source(device, broker_sources).strip().lower(),
        str(mqtt.get("broker_ref") or "").strip(),
        str(mqtt.get("topic_family") or "").strip(),
    )
    return selected != stored


def zendure_mqtt_device_draft(device, *, broker_sources=None):
    """Editable maintenance draft view of an existing ``zendure_mqtt`` device.

    Preserves the MQTT topic identity and device/product identifiers, surfaces
    the stored output-control opt-in as an editable field and carries a resolved
    user-facing hardware generation. No broker secret is ever read here.
    """

    name = str(device.get("name") or "").strip()
    mqtt = device.get("mqtt") if isinstance(device.get("mqtt"), dict) else {}
    topic_family = _mqtt_str(mqtt, "topic_family")
    write_protocol = _mqtt_str(mqtt, "write_protocol")
    write_topic = _mqtt_str(mqtt, "write_topic")
    generation, alt_layout = hardware_profile_for_topic_family(topic_family)
    model = zendure_mqtt_hardware_profile(device) or ""
    from ems.mqtt_control.zendure_profiles import hardware_profile_by_name

    model_profile = hardware_profile_by_name(model) if model else None
    if generation is None and model_profile is not None:
        generation = model_profile.hardware_generation
    capabilities = device.get("capabilities")
    read_power = bool(capabilities.get("read_power", True)) if isinstance(
        capabilities, dict
    ) else True
    read_soc = bool(capabilities.get("read_soc", True)) if isinstance(
        capabilities, dict
    ) else True
    write_output_limit = (
        bool(capabilities.get("write_output_limit", False))
        if isinstance(capabilities, dict)
        else False
    )
    control_capability = mqtt_output_control_capability(
        topic_family=topic_family,
        hardware_profile=zendure_mqtt_hardware_profile(device),
        write_protocol=write_protocol or None,
    )
    # Route addressability requires an explicit mqtt.device_id (never the physical
    # serial) plus the mode's target (product_key for a profile canonical topic, or
    # a valid write_topic for a custom write).
    from ems.zendure_mqtt.config_entries import zendure_mqtt_control_addressability

    addressability = zendure_mqtt_control_addressability(device)
    has_write_target = addressability.ready
    # A pinned model always publishes to its canonical topic; a stored write_topic
    # is then obsolete residue the UI shows read-only (never an editable field) so
    # Maintenance/Setup never reintroduce it. The custom escape hatch keeps its
    # explicit topic as the effective one.
    if model_profile is not None:
        from ems.zendure_mqtt.write_protocols import canonical_profile_write_topic

        effective_write_topic = canonical_profile_write_topic(
            _mqtt_str(mqtt, "product_key"), _mqtt_str(mqtt, "device_id")
        )
        effective_write_topic_source = "canonical_profile"
        write_topic_obsolete = bool(write_topic)
    else:
        effective_write_topic = write_topic or None
        effective_write_topic_source = "custom_explicit"
        write_topic_obsolete = False
    draft = common_device_draft_values(device)
    draft.update({
        "kind": "zendure_mqtt",
        "original_name": name,
        "name": name,
        "enabled": bool(device.get("enabled", True)),
        "has_enabled_key": "enabled" in device,
        "serial_number": str(device.get("serial_number") or "").strip(),
        # Display device id: the MQTT route id (mqtt.device_id), falling back to a
        # legacy top-level device_id. Redacted for cloud devices before it reaches
        # the browser. This is display only — apply never reads the route id from
        # it (that comes exclusively from mqtt.device_id below), so a top-level
        # device_id can never be promoted into the write route.
        "device_id": _mqtt_str(mqtt, "device_id") or str(device.get("device_id") or "").strip(),
        "product_key": _mqtt_str(mqtt, "product_key"),
        "hardware_generation": generation or "",
        "hardware_model": model if model_profile is not None else "",
        "power_write_profile": (
            model_profile.power_write_profile
            if model_profile is not None
            else write_protocol or None
        ),
        "validation_maturity": (
            model_profile.validation_status if model_profile is not None else None
        ),
        "supported_operations": (
            list(model_profile.supported_operations) if model_profile is not None else []
        ),
        "control_readiness": {
            "ready": control_capability.supported and has_write_target,
            "reason": (
                "write_target_missing"
                if control_capability.supported and not has_write_target
                else control_capability.reason
            ),
        },
        "alternative_layout": alt_layout,
        "write_output_limit": write_output_limit,
        # Editable control intent for the maintenance UI (mirrors the stored
        # capability); whether this device *can* be controlled at all, resolved
        # from its actual configured values: a valid explicit custom write
        # protocol counts, never the topic family alone.
        "output_control": write_output_limit,
        "supports_output_control": control_capability.supported,
        "mqtt": {
            "broker_ref": _mqtt_str(mqtt, "broker_ref"),
            "source": _mqtt_str(mqtt, "source"),
            # Display-only resolution of the stored source; apply never reads it,
            # so an untouched draft still writes back byte-identical config.
            "effective_source": _effective_mqtt_source(device, broker_sources),
            "topic_family": topic_family,
            "base_topic": mqtt.get("base_topic") if isinstance(mqtt, dict) else None,
            "device_id": _mqtt_str(mqtt, "device_id"),
            "write_protocol": write_protocol,
            "write_topic": write_topic,
            "effective_write_topic": effective_write_topic,
            "effective_write_topic_source": effective_write_topic_source,
            "write_topic_obsolete": write_topic_obsolete,
        },
        "capabilities": {
            "read_power": read_power,
            "read_soc": read_soc,
            "write_output_limit": write_output_limit,
        },
    })
    return draft


def _has_addressable_write_target(device):
    """True when control writes for ``device`` could actually be addressed.

    Same rule the control validation applies: a route device id plus either a
    product key for the canonical profile topic or an explicit write topic.
    """

    from ems.zendure_mqtt.config_entries import (
        zendure_mqtt_product_key,
        zendure_mqtt_route_device_id,
        zendure_mqtt_write_topic,
    )

    if zendure_mqtt_route_device_id(device) is None:
        return False
    return (
        zendure_mqtt_product_key(device) is not None
        or zendure_mqtt_write_topic(device) is not None
    )


def _apply_output_control(device, item, *, new_device, model_changed=False):
    """Resolve a draft entry's output-control choice onto a config device.

    Control is authorized only by the pinned concrete hardware model (or the
    isolated custom write protocol) and needs an addressable write target
    (product key or explicit write topic). An existing control entry is never
    *silently* downgraded: on a genuine no-op the stored request is preserved so
    an invalid config is surfaced by validation, not quietly turned off. But an
    explicit change — enabling control, or changing the pinned model (e.g. a
    writable model cleared to a telemetry-only one) — re-evaluates capability and
    removes stale control and write metadata.
    """

    from ems.zendure_mqtt.write_protocols import PROTOCOL_CUSTOM_PROPERTIES_WRITE

    capabilities = device.get("capabilities")
    stored = (
        bool(capabilities.get("write_output_limit", False))
        if isinstance(capabilities, dict)
        else False
    )
    mqtt = device.get("mqtt") if isinstance(device.get("mqtt"), dict) else {}
    capability = mqtt_output_control_capability(
        topic_family=mqtt.get("topic_family"),
        hardware_profile=zendure_mqtt_hardware_profile(device),
        write_protocol=mqtt.get("write_protocol"),
    )
    requested = _requested_output_control(item)
    if requested is None:
        # A new entry — including one whose transport just changed, which reaches
        # this projection with the stale block stripped — has no stored decision
        # to keep. A device that can control does, so an added inverter is never
        # silently telemetry-only just because the draft stayed quiet. The
        # implicit default also requires an addressable write target: capability
        # without an address would write a control entry validation must reject.
        requested = (
            capability.supported and _has_addressable_write_target(device)
            if new_device
            else stored
        )
    # A genuine no-op on an existing device (intent unchanged, model unchanged)
    # preserves the stored config so validation — not this projection — decides
    # whether an unchanged control entry is valid.
    if not new_device and not model_changed and requested == stored:
        return
    # Preserve an explicit operator request even when its target/capability is
    # incomplete. Validation then returns the actionable error (for example
    # ``write_target_missing``) instead of silently changing the checkbox back
    # to telemetry-only. Untrusted proposal fragments remain capability-gated by
    # ``sanitize_zendure_mqtt_fragment`` before they reach this projection.
    control = bool(requested) and capability.supported
    device.setdefault("capabilities", {})["write_output_limit"] = control
    if isinstance(device.get("mqtt"), dict):
        # A profile-authorized device carries no mqtt.write_protocol (the model
        # selects the adapter). Only the explicit custom escape hatch keeps one.
        if control and capability.write_protocol == PROTOCOL_CUSTOM_PROPERTIES_WRITE:
            device["mqtt"]["write_protocol"] = capability.write_protocol
        elif device["mqtt"].get("write_protocol") != PROTOCOL_CUSTOM_PROPERTIES_WRITE:
            # Drop any stale built-in write method; keep an explicit custom one.
            device["mqtt"].pop("write_protocol", None)


def apply_zendure_mqtt_draft_fields(device, item):
    """Write editable Zendure MQTT fields from a draft entry onto a config device.

    Identity (topic family/base topic) follows the selected hardware generation
    so the UI never edits raw internal names. An unknown generation keeps the
    device's existing mqtt identity untouched. Output control follows the
    draft's explicit choice, capability-gated by the shared EMS rule.
    """

    if "name" in item:
        device["name"] = str(item.get("name") or "").strip()
    device["type"] = "zendure_mqtt"

    # serial_number and mqtt.device_id are distinct identity fields: the serial is
    # the physical/API identity, mqtt.device_id is the MQTT routing identity. A
    # config may legitimately carry different values, so they are patched
    # independently and never collapsed into one input.
    _store_editable_value(
        device, "serial_number", *_editable_draft_value("serial_number", item)
    )

    # A concrete registry model pins the runtime write adapter into config; it is
    # separate from the display-only hardware generation. power_write_profile is
    # re-derived from the registry (authoritative), never trusted from the draft.
    from ems.mqtt_control.zendure_profiles import hardware_profile_by_name

    normalized = normalize_zendure_mqtt_draft(item)
    previous_model = zendure_mqtt_hardware_profile(device) or ""
    model = normalized.get("hardware_model") or ""
    model_selection_present = any(
        key in item
        for key in ("hardware_model", "power_hardware_profile", "hardware_generation")
    )
    profile = hardware_profile_by_name(model) if model else None
    if profile is not None:
        device["hardware_profile"] = model
        device["power_write_profile"] = profile.power_write_profile
    elif model_selection_present:
        device.pop("hardware_profile", None)
        device.pop("power_write_profile", None)
    model_changed = model_selection_present and model != previous_model

    mqtt = device.get("mqtt")
    new_device = not isinstance(mqtt, dict)
    if new_device:
        mqtt = {}
        device["mqtt"] = mqtt
        # A brand-new device (e.g. added from a discovery proposal) seeds its
        # topic identity and connection metadata from the draft's mqtt block, so
        # a detected alternative legacy layout is preserved rather than
        # normalized and the connection source stays explicit (parity with the
        # Fresh Setup fragment path).
        item_mqtt = item.get("mqtt")
        if isinstance(item_mqtt, dict):
            for key in (
                "broker_ref",
                "source",
                "topic_family",
                "device_id",
                "product_key",
                "write_protocol",
            ):
                value = item_mqtt.get(key)
                if isinstance(value, str) and value.strip():
                    mqtt[key] = value.strip()
            # A draft that carries no base topic (absent, or an explicit null
            # from a proposal whose observation never reported one) must not
            # persist a null: the selected family's canonical topic keeps the
            # seeded connection complete.
            if item_mqtt.get("base_topic") is not None:
                mqtt["base_topic"] = item_mqtt["base_topic"]
            elif mqtt.get("topic_family"):
                # No explicit base topic: derive the observed family's canonical
                # one so the seeded entry stays complete.
                family_generation, _ = hardware_profile_for_topic_family(
                    mqtt["topic_family"]
                )
                family_profile = generation_profile(family_generation)
                if family_profile is not None:
                    mqtt["base_topic"] = family_profile["base_topic"]
        item_caps = (
            item.get("capabilities") if isinstance(item.get("capabilities"), dict) else {}
        )
        device["capabilities"] = {
            "read_power": bool(item_caps.get("read_power", True)),
            "read_soc": bool(item_caps.get("read_soc", True)),
        }
    profile = generation_profile(normalized.get("hardware_generation"))
    # Re-home the topic identity only where the generation actually determines
    # it: a brand-new manual device (no observed family), or an existing device
    # whose operator explicitly changes the hardware generation. A discovery
    # proposal seeds the observed topic family, and that observed schema is
    # authoritative — a model-resolved hardware label (e.g. a hub-line product
    # publishing scalar topics) must never rewrite it. The draft's
    # ``hardware_profile`` is derived from the device's current topic family, so
    # comparing it to that family's generation detects a real change; an
    # unchanged generation must never normalize a custom or absent ``base_topic``
    # on a no-op apply. The internal "alt" legacy layout is likewise preserved.
    current_generation, _ = hardware_profile_for_topic_family(mqtt.get("topic_family"))
    if new_device:
        generation_changed = not mqtt.get("topic_family")
    else:
        generation_changed = str(normalized.get("hardware_generation") or "") != str(
            current_generation or ""
        )
    if profile is not None and generation_changed and not (
        item.get("alternative_layout") and mqtt.get("topic_family") == _ALT_TOPIC_FAMILY
    ):
        mqtt["topic_family"] = profile["topic_family"]
        mqtt["base_topic"] = profile["base_topic"]
    item_mqtt = item.get("mqtt") if isinstance(item.get("mqtt"), dict) else {}
    # The MQTT route id comes only from mqtt.device_id; a legacy top-level
    # device_id is never promoted into the write route.
    _store_editable_value(
        mqtt, "device_id", *_editable_draft_value("device_id", item_mqtt)
    )
    # Product-key addressing is a runtime write target, not a display-generation
    # property. Cloud JSON devices may use a generation whose manual form does
    # not normally request a key, so persist any explicit draft value regardless
    # of generation.
    _store_editable_value(
        mqtt, "product_key", *_editable_draft_value("product_key", item, item_mqtt)
    )

    _apply_output_control(
        device, item, new_device=new_device, model_changed=model_changed
    )

    # Common (transport-independent) tuning values round-trip through the same
    # catalog-derived projection as Local API devices; keys absent from the
    # draft stay untouched so a no-op apply remains byte-exact.
    apply_common_device_values(device, item)

    enabled = bool(item.get("enabled", True))
    if "enabled" in device or item.get("has_enabled_key") or not enabled:
        device["enabled"] = enabled


__all__ = [
    "SECRET_KEY_FRAGMENTS",
    "TRUSTED_CONNECTION_SELECTION_FIELD",
    "zendure_mqtt_untrusted_connection_block",
    "MASKED_IDENTITY_KEYS",
    "TELEMETRY_SCHEMAS",
    "generation_catalog",
    "zendure_hardware_profile_options",
    "normalize_zendure_mqtt_draft",
    "generation_profile",
    "generation_label",
    "generation_supports_output_control",
    "hardware_profile_for_topic_family",
    "hardware_generation_for_model",
    "resolve_hardware_generation",
    "telemetry_schema_for_topic_family",
    "zendure_mqtt_connection_switched",
    "sanitize_zendure_mqtt_fragment",
    "validate_zendure_mqtt_fragment",
    "build_manual_zendure_mqtt_fragment",
    "zendure_mqtt_device_draft",
    "apply_zendure_mqtt_draft_fields",
]
