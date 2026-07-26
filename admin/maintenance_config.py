# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance draft, preview and apply preparation for an existing EMS config.

Loads the real resolved ``config/config.json``, exposes a normalized draft the
Admin UI can edit, and prepares a validated merged config with a bounded diff.
This module never writes: applying the prepared payload is owned by the shared
atomic config apply service.
"""

import copy
import hashlib
import hmac
import json

from admin.config_preview import _GRID_TYPE_CHOICES, _valid_host
from admin.config_runtime_overlap import overlap_provenance_for_context
from admin.device_common_fields import (
    coerce_field_value as _coerce,
    common_device_value_fields,
)
from admin.inverter_names import next_compact_inverter_name
from admin.install_context import detect_install_context
from admin.setup_config import (
    _set_path,
    grid_meter_variant_catalog,
    hardware_section_catalog,
)
from admin.zendure_mqtt_broker_profiles import (
    BrokerEndpointError,
    broker_endpoint,
    default_zendure_cloud_auth_available,
    resolve_broker_ref,
)
from admin.zendure_mqtt_config_draft import (
    apply_zendure_mqtt_draft_fields,
    generation_catalog,
    zendure_hardware_profile_options,
    zendure_mqtt_device_draft,
)
from ems.config import (
    MQTT_GRID_METER_TYPES,
    MqttBrokerReferenceAmbiguousError,
    grid_meter_mqtt_settings,
    normalize_mqtt_grid_meter_settings,
    resolve_grid_meter_mqtt_settings,
)
from ems.config_catalog import (
    ZENDURE_MQTT_BROKER_HELP,
    device_common_defaults,
    device_common_field_keys,
    get_config_feature_field_index,
    get_config_feature_sections,
    http_grid_meter_types,
)
from ems.mqtt_credentials import find_mqtt_credential_consumer_issues
from ems.device_identity import (
    PHYSICAL_IDENTITY_ALIAS_TOKENS_FIELD,
    PHYSICAL_IDENTITY_TOKEN_FIELD,
    broker_sources_from_config,
    identity_evidence_conflict,
    resolve_inverter_identity,
    resolve_inverter_identity_evidence,
    supplied_identity_token,
)
from ems.external_status import (
    mask_external_mqtt_string,
    mask_mqtt_topic,
    sanitize_external_mqtt_status,
)
from ems.zendure_mqtt.config_entries import (
    find_duplicate_zendure_device_identities,
    find_reserved_mqtt_broker_ref_issues,
    find_zendure_mqtt_broker_profile_issues,
    has_runtime_control_device,
    is_control_zendure_mqtt_device_config,
    is_zendure_mqtt_device_config,
    validate_zendure_mqtt_control_device_config,
    validate_zendure_mqtt_device_config,
    zendure_mqtt_broker_profile_views,
)

# HTTP/IP and MQTT grid-meter type sets both come from the central catalog/config
# (via http_grid_meter_types / MQTT_GRID_METER_TYPES) so a new variant is
# validated here without a duplicated maintenance-only list.
_HOST_GRID_METER_TYPES = http_grid_meter_types()
_MQTT_GRID_METER_TYPES = tuple(MQTT_GRID_METER_TYPES)
# Flat MQTT keys that may linger under grid_meter from a legacy/other meter type.
_MQTT_GRID_METER_FLAT_KEYS = (
    "host",
    "port",
    "username",
    "password",
    "topic",
    "payload_format",
    "value_path",
    "max_age_seconds",
)
# Non-secret MQTT grid-meter values the editor may change. The password is
# handled separately (keep/clear/set) and never round-trips through the draft.
_MQTT_GRID_METER_EDIT_KEYS = (
    "broker_ref",
    "host",
    "port",
    "username",
    "topic",
    "payload_format",
    "value_path",
    "max_age_seconds",
)

_SERIAL_PLACEHOLDER = "YOUR_SN"
_MAX_STRING_LEN = 160

# Leaf key fragments whose value is a secret and must never be surfaced to the
# browser (draft, diff or preview JSON). Username is not a secret and is kept.
_SECRET_LEAF_FRAGMENTS = (
    "password",
    "passphrase",
    "token",
    "secret",
    "credential",
    "app_key",
    "apikey",
    "product_key",
)
# Reference keys that merely *name* an external secret record; the reference is
# not a secret and the setup preview shows it, so maintenance must too.
_NON_SECRET_LEAF_KEYS = (
    "credentials_ref",
    # A keyed, non-reversible equality helper deliberately issued to the
    # browser; it is neither a bearer token nor config source-of-truth.
    PHYSICAL_IDENTITY_TOKEN_FIELD,
    PHYSICAL_IDENTITY_ALIAS_TOKENS_FIELD,
    # Boolean presence metadata used by the browser to render keep/clear/set
    # controls. It carries no credential value and must remain a boolean.
    "has_password",
)
_REDACTED = "••••"


def _issue(code, message):
    return {"code": code, "message": message}


def _is_secret_leaf(key):
    lowered = str(key).lower()
    if lowered in _NON_SECRET_LEAF_KEYS:
        return False
    return any(fragment in lowered for fragment in _SECRET_LEAF_FRAGMENTS)


def _redact_secrets(value):
    """Replace secret leaf values in-place with a placeholder for display only."""

    if isinstance(value, dict):
        for key, item in value.items():
            if _is_secret_leaf(key) and item not in (None, "", False):
                value[key] = _REDACTED
            else:
                _redact_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _redact_secrets(item)
    return value


def redact_config_for_browser(
    value, *, identity_token_key=None, broker_sources=None
):
    """Redact config/draft secrets in a browser-facing copy in-place."""

    if identity_token_key is not None:
        _attach_physical_identity_tokens(
            value, identity_token_key, broker_sources=broker_sources
        )
    safe = sanitize_external_mqtt_status(
        value,
        sensitive_context=value,
        drop_secrets=False,
    )
    if isinstance(value, dict) and isinstance(safe, dict):
        value.clear()
        value.update(safe)
    elif isinstance(value, list) and isinstance(safe, list):
        value[:] = safe
    else:
        value = safe
    _redact_cloud_mqtt_route_ids(value, broker_sources=broker_sources)
    _redact_secrets(value)
    _redact_mqtt_device_id_diff(value)
    return value


def _attach_physical_identity_tokens(value, token_key, *, broker_sources=None):
    """Attach derived equality tokens to each browser-visible devices list."""

    def walk(node, inherited_sources=None):
        if not isinstance(node, dict):
            if isinstance(node, list):
                for child in node:
                    walk(child, inherited_sources)
            return
        sources = broker_sources_from_config(node) or inherited_sources or {}
        devices = node.get("devices")
        if isinstance(devices, list):
            for device in devices:
                if not isinstance(device, dict):
                    continue
                evidence = resolve_inverter_identity_evidence(
                    device, broker_sources=sources, token_key=token_key
                )
                if evidence is None:
                    continue
                if evidence.primary.opaque_token is not None:
                    device[PHYSICAL_IDENTITY_TOKEN_FIELD] = (
                        evidence.primary.opaque_token
                    )
                alias_tokens = list(evidence.opaque_tokens)
                if alias_tokens:
                    device[PHYSICAL_IDENTITY_ALIAS_TOKENS_FIELD] = alias_tokens
        for child in node.values():
            walk(child, sources)

    walk(value, broker_sources or {})


def _redact_cloud_mqtt_route_ids(value, *, broker_sources=None):
    """Hide account-scoped cloud route ids while preserving physical serials."""

    sources = dict(broker_sources or {})
    if isinstance(value, dict):
        sources.update(broker_sources_from_config(value))

    def mask_strings(node, sensitive):
        if isinstance(node, dict):
            for child_key in list(node):
                child = node[child_key]
                if str(child_key).lower() in {
                    "sn",
                    "serial_number",
                    PHYSICAL_IDENTITY_TOKEN_FIELD,
                }:
                    continue
                safe_key = (
                    mask_external_mqtt_string(
                        child_key,
                        sensitive_values=frozenset(sensitive),
                        cloud_scoped=True,
                    )
                    if isinstance(child_key, str)
                    else child_key
                )
                safe_child = mask_strings(child, sensitive)
                if safe_key != child_key:
                    del node[child_key]
                node[safe_key] = safe_child
            return node
        if isinstance(node, list):
            return [mask_strings(child, sensitive) for child in node]
        if isinstance(node, str):
            return mask_external_mqtt_string(
                node, sensitive_values=frozenset(sensitive), cloud_scoped=True
            )
        return node

    all_sensitive = set()

    def walk(node):
        if isinstance(node, dict):
            mqtt = node.get("mqtt")
            if isinstance(mqtt, dict):
                broker_ref = str(mqtt.get("broker_ref") or "default").strip()
                source = str(mqtt.get("source") or sources.get(broker_ref) or "")
                if source == "zendure_cloud_mqtt":
                    sensitive = {
                        raw.strip()
                        for raw in (
                            mqtt.get("device_id"),
                            mqtt.get("product_key"),
                            node.get("device_id"),
                            node.get("product_key"),
                        )
                        if isinstance(raw, str) and raw.strip()
                    }
                    all_sensitive.update(sensitive)
                    mask_strings(node, sensitive)
                    for target in (mqtt, node):
                        for key in ("device_id", "product_key"):
                            if target.get(key) not in (None, ""):
                                target[key] = _REDACTED
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    if all_sensitive:
        mask_strings(value, all_sensitive)


def _redact_mqtt_device_id_diff(value):
    """Diff entries lack their parent device, so mask every MQTT routing id."""

    if isinstance(value, dict):
        path = value.get("path")
        if isinstance(path, str) and path.endswith(
            (".mqtt.device_id", ".mqtt.product_key")
        ):
            for key in ("before", "after"):
                if value.get(key) not in (None, ""):
                    value[key] = _REDACTED
        elif isinstance(path, str) and "topic" in path.lower():
            # A diff entry has lost its parent device and cannot prove local vs
            # Cloud scope, so it masks every routing topic fail-safe.
            for key in ("before", "after"):
                if isinstance(value.get(key), str):
                    value[key] = mask_mqtt_topic(value[key], cloud_scoped=True)
        for item in value.values():
            _redact_mqtt_device_id_diff(item)
    elif isinstance(value, list):
        for item in value:
            _redact_mqtt_device_id_diff(item)


def _is_maintenance_field(field):
    """True for catalog feature fields the maintenance editor may write.

    Device and grid-meter paths are owned by the dedicated hardware editors;
    deprecated, read-only and secret fields stay out so the editor can never
    surface a secret value or write outside the catalog.
    """

    if field.get("scope") not in ("maintenance", "both"):
        return False
    if field.get("level") == "deprecated":
        return False
    if field.get("editable") is False:
        return False
    if field.get("risk") == "secret" or field.get("type") == "password":
        return False
    return True


def _maintenance_field_index():
    index = {}
    for path, field in get_config_feature_field_index().items():
        if "[]" in path or path.startswith("devices") or path.startswith("grid_meter"):
            continue
        if not _is_maintenance_field(field):
            continue
        index[path] = field
    return index


# Catalog device fields (beyond name/ip/sn identity) the editor may write.
# Shared with the Zendure MQTT draft path via admin.device_common_fields so
# both transports project the identical common value set.
_DEVICE_VALUE_FIELDS = common_device_value_fields()


def _get_path(config, path):
    cursor = config
    for part in path.split("."):
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(part)
    return cursor


def _bounded(value):
    """Bound a scalar for safe UI rendering; lists/dicts collapse to a summary."""

    if isinstance(value, str):
        return value if len(value) <= _MAX_STRING_LEN else value[:_MAX_STRING_LEN] + "…"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return f"[{len(value)} items]"
    if isinstance(value, dict):
        return "{…}"
    text = str(value)
    return text if len(text) <= _MAX_STRING_LEN else text[:_MAX_STRING_LEN] + "…"


# --- load / draft --------------------------------------------------------


def load_maintenance_config(base_dir=None, *, identity_token_key=None):
    """Load the resolved EMS config and return a normalized maintenance view.

    Missing or invalid config degrades to a clear status (never an exception),
    so the Admin route can return a non-500 result the UI can render.
    """

    context = detect_install_context(base_dir=base_dir)
    config_path = str(context.config_path)
    if not context.config_exists:
        return {
            "status": "missing",
            "config_path": config_path,
            "source": context.config_source,
            "message": "No config.json was found at the resolved install path.",
        }
    try:
        raw = context.config_path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        return {
            "status": "invalid",
            "config_path": config_path,
            "source": context.config_source,
            "message": f"The config file could not be read: {exc}.",
        }
    try:
        config = json.loads(text)
    except ValueError:
        return {
            "status": "invalid",
            "config_path": config_path,
            "source": context.config_source,
            "message": "The config file is not valid JSON.",
        }
    if not isinstance(config, dict):
        return {
            "status": "invalid",
            "config_path": config_path,
            "source": context.config_source,
            "message": "The config file is not a JSON object.",
        }

    draft = build_maintenance_draft(config)
    overrides = overlap_provenance_for_context(context, config)
    _attach_runtime_override_identity_tokens(
        overrides,
        config,
        identity_token_key=identity_token_key,
    )
    browser_fields = {
        "draft": copy.deepcopy(draft),
        "overrides": overrides,
    }
    redact_config_for_browser(
        browser_fields,
        identity_token_key=identity_token_key,
        broker_sources=broker_sources_from_config(config),
    )
    browser_fields = sanitize_external_mqtt_status(
        browser_fields,
        sensitive_context=config,
        drop_secrets=False,
    )
    return {
        "status": "ok",
        "config_path": config_path,
        "source": context.config_source,
        "revision": hashlib.sha256(raw).hexdigest(),
        "summary": _summary(config, draft),
        "draft": browser_fields["draft"],
        "overrides": browser_fields["overrides"],
        "catalog": _catalog(),
        "warnings": [],
    }


def _attach_runtime_override_identity_tokens(
    overrides, config, *, identity_token_key
):
    """Bind device override rows to raw config identities without exposing names.

    Cloud route values can occur in a configured display name, so the browser's
    override map is masked along with the draft.  Each field row carries the
    same server-issued equality token as its device draft; reset requests can
    therefore resolve the installed device without trusting or reconstructing
    the masked display name.
    """

    if identity_token_key is None or not isinstance(overrides, dict):
        return
    device_overrides = overrides.get("devices")
    if not isinstance(device_overrides, dict):
        return
    broker_sources = broker_sources_from_config(config)
    for device in _config_devices(config):
        name = str(device.get("name") or "").strip()
        fields = device_overrides.get(name)
        if not name or not isinstance(fields, dict):
            continue
        identity = resolve_inverter_identity(
            device,
            broker_sources=broker_sources,
            token_key=identity_token_key,
        )
        if identity is None or identity.opaque_token is None:
            continue
        for entry in fields.values():
            if isinstance(entry, dict):
                entry[PHYSICAL_IDENTITY_TOKEN_FIELD] = identity.opaque_token


def build_maintenance_draft(config):
    """Build an editable in-memory draft from the current config values."""

    return {
        "devices": [_device_draft(device) for device in _config_devices(config)],
        "grid_meter": _grid_meter_draft(config.get("grid_meter")),
        "zendure_mqtt": _zendure_mqtt_broker_draft(config.get("zendure_mqtt")),
        "features": _feature_draft(config),
    }


def _config_devices(config):
    devices = config.get("devices")
    if not isinstance(devices, list):
        return []
    return [device for device in devices if isinstance(device, dict)]


def _device_draft(device):
    if is_zendure_mqtt_device_config(device):
        return zendure_mqtt_device_draft(device)
    name = str(device.get("name") or "").strip()
    draft = {
        "kind": "local_api",
        "original_name": name,
        "name": name,
        "ip": str(device.get("ip") or "").strip(),
        "sn": str(device.get("sn") or "").strip(),
        "enabled": bool(device.get("enabled", True)),
        "has_enabled_key": "enabled" in device,
        "connection_type": str(device.get("connection_type") or "").strip() or "local API",
    }
    for key in _DEVICE_VALUE_FIELDS:
        if key in device:
            draft[key] = device[key]
    return draft


def _broker_uses_tls(broker):
    if broker.get("tls") is True:
        return True
    return str(broker.get("security") or "").strip().lower() in ("tls", "mqtts", "ssl")


def _has_named_brokers(broker):
    return (
        isinstance(broker, dict)
        and isinstance(broker.get("brokers"), dict)
        and bool(broker["brokers"])
    )


def _zendure_mqtt_broker_draft(broker):
    """Editable, secret-free view of the top-level Zendure MQTT broker.

    The password is never returned; only whether one is stored is surfaced so the
    UI can offer keep/clear without ever displaying the secret. ``managed``
    records how the broker block is owned: ``legacy`` for an editable top-level
    single broker, ``named`` for a config whose connections live under
    ``brokers`` (owned by the setup/proposal flow — never edited via this form),
    or ``none`` when no broker exists yet.
    """

    if not isinstance(broker, dict):
        return {
            "present": False,
            "managed": "none",
            "enabled": False,
            "host": "",
            "port": None,
            "tls": False,
            "username": "",
            "has_password": False,
        }
    top_host = str(broker.get("host") or "").strip()
    if _has_named_brokers(broker) and not top_host:
        # Named broker profiles are the modern connection model. The maintenance
        # top-level broker form must not surface (or later inject) legacy
        # host/port/tls fields beside them, so it is presented as read-only.
        return {
            "present": False,
            "managed": "named",
            # The feature is always on; named profiles carry their own flags.
            "enabled": True,
            "host": "",
            "port": None,
            "tls": False,
            "username": "",
            "has_password": False,
        }
    return {
        "present": True,
        "managed": "legacy",
        # Always-on feature: a legacy broker is active exactly when it has a
        # host; the stored legacy ``enabled`` key is ignored.
        "enabled": bool(top_host),
        "host": top_host,
        "port": broker.get("port"),
        "tls": _broker_uses_tls(broker),
        "username": str(broker.get("username") or "").strip(),
        "has_password": bool(broker.get("password")),
    }


def _grid_meter_draft(grid_meter):
    if not isinstance(grid_meter, dict):
        return {"type": "", "ip": "", "present": False}
    draft = {
        "present": True,
        "type": str(grid_meter.get("type") or "").strip(),
        "ip": str(grid_meter.get("ip") or "").strip(),
    }
    if grid_meter.get("port") is not None:
        draft["port"] = grid_meter.get("port")
    if grid_meter.get("url"):
        draft["url"] = str(grid_meter.get("url")).strip()
    if grid_meter.get("power_path"):
        draft["power_path"] = str(grid_meter.get("power_path")).strip()
    channels = grid_meter.get("channels")
    if isinstance(channels, list):
        draft["channels"] = [str(item) for item in channels]
    if draft["type"] in _MQTT_GRID_METER_TYPES:
        draft["mqtt"] = _grid_meter_mqtt_draft(grid_meter)
    return draft


def _grid_meter_mqtt_draft(grid_meter):
    """Secret-free editable view of the MQTT grid-meter settings.

    Works for nested (``grid_meter.mqtt``) and legacy flat configs alike; the
    password is only surfaced as a ``has_password`` flag so the UI can offer
    keep/clear without ever seeing the stored secret.
    """

    settings = grid_meter_mqtt_settings(grid_meter)
    draft = {}
    for key in _MQTT_GRID_METER_EDIT_KEYS:
        if settings.get(key) not in (None, ""):
            draft[key] = settings[key]
    draft["has_password"] = bool(settings.get("password"))
    return draft


def _feature_draft(config):
    draft = {}
    for path in _maintenance_field_index():
        value = _get_path(config, path)
        if value is not None:
            draft[path] = value
    return draft


def _summary(config, draft):
    devices = draft["devices"]
    grid_meter = config.get("grid_meter") if isinstance(config.get("grid_meter"), dict) else {}
    dashboard = config.get("dashboard") if isinstance(config.get("dashboard"), dict) else {}
    influx = config.get("influxdb") if isinstance(config.get("influxdb"), dict) else {}
    return {
        "device_count": len(devices),
        "enabled_device_count": sum(1 for device in devices if device.get("enabled", True)),
        "grid_meter_type": str(grid_meter.get("type") or "").strip() or None,
        "dashboard_enabled": bool(dashboard.get("enabled", False)),
        "influx_enabled": bool(influx.get("enabled", False)),
        "influx_mode": str(influx.get("mode") or "").strip() or None,
    }


def _catalog():
    """Serializable metadata the UI needs to render editors and options."""

    index = _maintenance_field_index()
    sections = []
    for section in get_config_feature_sections(mode="maintenance"):
        if section.get("setup_group") == "hardware" or section["id"] == "ha":
            continue
        fields = [field for field in section["fields"] if field["path"] in index]
        if not fields:
            continue
        section = dict(section)
        section["fields"] = fields
        sections.append(section)

    return {
        "feature_sections": sections,
        # Hardware metadata shared with the setup catalog (single source:
        # ems.config_catalog) so both hardware editors render the same fields.
        "hardware_sections": hardware_section_catalog("maintenance"),
        # Central default common values for a new inverter, from the same
        # catalog/template source the config template is generated from. The
        # browser initializes new devices from this payload; the merge path
        # re-materializes the same defaults, so a buggy client cannot produce
        # an incomplete device.
        "default_device": {"common": device_common_defaults()},
        "grid_meter_variants": grid_meter_variant_catalog(),
        "zendure_mqtt_generations": generation_catalog(),
        "zendure_mqtt_hardware_models": [
            {
                key: value
                for key, value in option.items()
                if key
                in {
                    "id",
                    "label",
                    "generation",
                    "compatible_generations",
                    "control_supported",
                    "supported_operations",
                    "power_write_profile",
                    "validation_maturity",
                }
            }
            for option in zendure_hardware_profile_options()
        ],
        "zendure_mqtt_broker": {"help": ZENDURE_MQTT_BROKER_HELP},
    }


# --- preview / merge -----------------------------------------------------


def _load_current(base_dir):
    """Read the resolved config, returning ``(error, raw_bytes, config)``."""

    context = detect_install_context(base_dir=base_dir)
    config_path = str(context.config_path)
    if not context.config_exists:
        return {
            "status": "missing",
            "config_path": config_path,
            "message": "No config.json was found at the resolved install path.",
        }, None, None
    try:
        raw = context.config_path.read_bytes()
        current = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return {
            "status": "invalid",
            "config_path": config_path,
            "message": "The current config could not be read as JSON.",
        }, None, None
    if not isinstance(current, dict):
        return {
            "status": "invalid",
            "config_path": config_path,
            "message": "The current config is not a JSON object.",
        }, None, None
    return None, raw, current


def preview_maintenance_config(draft, base_dir=None, *, identity_token_key=None):
    """Merge a draft with the current config and return validation + diff.

    Never writes: the resolved config is only ever read, and the merged result
    stays in memory. The returned ``preview``/``diff`` are redacted so no stored
    or newly entered secret (broker password, tokens) reaches the browser.
    Missing/invalid config degrades to a clear status.
    """

    context = detect_install_context(base_dir=base_dir)
    config_path = str(context.config_path)
    error, raw, current = _load_current(base_dir)
    if error is not None:
        return error

    draft = draft if isinstance(draft, dict) else {}
    merge_issues = []
    merged = _merge_draft(
        current, draft, merge_issues, identity_token_key=identity_token_key
    )
    validation = _validate(merged, merge_issues)
    # Read-only preview surfaces the global MQTT credentials_ref contract early
    # (canonical refs, single-source ownership) so the UI shows a bad reference
    # before an apply is attempted; the apply itself defers to the shared
    # credential-staging layer, which rejects the same references with a stable
    # structured code and a byte-exact rollback.
    _append_mqtt_credential_consumer_issues(validation, merged)
    diff = summarize_config_changes(current, merged)
    response = {
        "status": "ok",
        "config_path": config_path,
        "revision": hashlib.sha256(raw).hexdigest(),
        "changed": diff["changed"],
        "diff": redact_config_for_browser(
            diff,
            identity_token_key=identity_token_key,
            broker_sources=broker_sources_from_config(merged),
        ),
        "validation": validation,
        "preview": redact_config_for_browser(
            copy.deepcopy(merged),
            identity_token_key=identity_token_key,
            broker_sources=broker_sources_from_config(merged),
        ),
    }
    return sanitize_external_mqtt_status(
        response,
        sensitive_context=merged,
        drop_secrets=False,
    )


def prepare_maintenance_config_apply(
    draft, expected_revision, base_dir=None, *, identity_token_key=None
):
    """Validate a reviewed draft and serialize it without writing anything.

    The serialized ``payload`` carries the true merged config (secrets intact);
    only the browser-facing ``preview``/``diff`` are redacted.
    """

    error, raw, current = _load_current(base_dir)
    if error is not None:
        return error
    revision = hashlib.sha256(raw).hexdigest()

    draft = draft if isinstance(draft, dict) else {}
    merge_issues = []
    merged = _merge_draft(
        current, draft, merge_issues, identity_token_key=identity_token_key
    )
    validation = _validate(merged, merge_issues)
    diff = summarize_config_changes(current, merged)
    result = sanitize_external_mqtt_status(
        {
            "status": "ok",
            "config_path": str(
                detect_install_context(base_dir=base_dir).config_path
            ),
            "revision": revision,
            "changed": diff["changed"],
            "diff": redact_config_for_browser(
                diff,
                identity_token_key=identity_token_key,
                broker_sources=broker_sources_from_config(merged),
            ),
            "validation": validation,
            "preview": redact_config_for_browser(
                copy.deepcopy(merged),
                identity_token_key=identity_token_key,
                broker_sources=broker_sources_from_config(merged),
            ),
        },
        sensitive_context=merged,
        drop_secrets=False,
    )
    if not expected_revision or expected_revision != revision:
        return {
            "status": "conflict",
            "message": (
                "config/config.json changed after this workflow was opened. "
                "Reload the current config and review the draft again."
            ),
            "revision": revision,
        }
    if not validation["ok"]:
        result["status"] = "invalid"
        result["message"] = "The draft is not valid and was not applied."
        return result
    result["payload"] = (
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    return result


def _merge_draft(current, draft, issues, *, identity_token_key=None):
    """Merge the draft onto a copy of the current config.

    ``issues`` collects actionable ``{code, message}`` errors raised while
    merging (currently broker-profile resolution); the validator surfaces them
    so an apply can never proceed past a failed merge step.
    """

    merged = copy.deepcopy(current)
    _merge_devices(
        merged,
        draft.get("devices"),
        issues,
        identity_token_key=identity_token_key,
    )
    _merge_grid_meter(merged, draft.get("grid_meter"))
    _merge_zendure_mqtt_broker(merged, draft.get("zendure_mqtt"))
    _merge_features(merged, draft.get("features"))
    return merged


def _is_mqtt_draft_item(item):
    return item.get("kind") == "zendure_mqtt" or item.get("type") == "zendure_mqtt"


# Connection/identity keys owned by exactly one transport. A transport switch
# removes the old transport's keys and nothing else, so common tuning values
# and unknown custom keys survive the switch untouched.
_LOCAL_API_ONLY_DEVICE_KEYS = ("ip", "sn", "port", "connection_type")
_ZENDURE_MQTT_ONLY_DEVICE_KEYS = (
    "type",
    "serial_number",
    "device_id",
    "product",
    "product_key",
    "mqtt",
    "capabilities",
    "hardware_profile",
    "power_write_profile",
)


def _strip_stale_transport_keys(device, transport):
    stale = (
        _ZENDURE_MQTT_ONLY_DEVICE_KEYS
        if transport == "local_api"
        else _LOCAL_API_ONLY_DEVICE_KEYS
    )
    for key in stale:
        device.pop(key, None)
    return device


def materialize_maintenance_device(*, existing_device, draft_item, transport, defaults):
    """Materialize one maintenance draft entry into a config device.

    Value precedence: existing explicit device values, then explicit draft
    edits, then the central catalog defaults. A transport switch keeps the
    device's common/custom values and replaces only the connection: the old
    transport's keys are dropped before the new transport's fields apply.
    Missing common defaults materialize for brand-new devices and transport
    switches only — an untouched existing device keeps its byte-exact stored
    shape so a no-op apply never rewrites config.
    """

    is_mqtt = transport == "zendure_mqtt"
    new_device = existing_device is None
    switched = not new_device and is_zendure_mqtt_device_config(existing_device) != is_mqtt
    if new_device:
        device = {}
    elif switched:
        device = _strip_stale_transport_keys(copy.deepcopy(existing_device), transport)
    else:
        device = copy.deepcopy(existing_device)
    if is_mqtt:
        apply_zendure_mqtt_draft_fields(device, draft_item)
    else:
        _apply_device_fields(device, draft_item)
    if new_device or switched:
        for key, value in defaults.items():
            if key not in device:
                device[key] = copy.deepcopy(value)
    return device


def _resolve_original_device(
    item,
    by_name,
    identity_index,
    token_index,
    evidence_by_id,
    claimed_ids,
    issues,
    *,
    broker_sources,
    identity_token_key,
):
    """Find the config entry a draft item edits, failing closed on conflicts.

    ``original_name`` is authoritative when it resolves. Otherwise the item's
    trusted identity aliases (serial, scoped route, endpoint) are intersected
    against the configured devices: a route-only entry still matches a later
    serial-bearing observation of the same route (enrichment), while a shared
    route that claims a different physical serial is a contradiction and refuses
    to guess. When name and identity resolve to different originals, or one alias
    is ambiguous across entries, the merge fails closed.
    """

    named = by_name.get(str(item.get("original_name") or ""))
    evidence = resolve_inverter_identity_evidence(
        item,
        broker_sources=broker_sources,
        token_key=identity_token_key,
    )
    supplied_token = supplied_identity_token(item)
    token_match = token_index.get(supplied_token) if supplied_token else None
    label = str(item.get("name") or item.get("original_name") or "device").strip()

    matched = None
    if evidence is not None:
        hits = []
        for key in evidence.comparison_keys:
            if key in identity_index:
                found = identity_index[key]
                if found is None:
                    # A trusted alias shared by two configured entries is
                    # ambiguous; refuse to guess which one is edited.
                    issues.append(
                        _issue(
                            "device_identity_conflict",
                            f"{label}: a trusted identity alias matches more than "
                            "one configured device. Reload the current config and "
                            "review the draft.",
                        )
                    )
                    return None
                if all(found is not hit for hit in hits):
                    hits.append(found)
        if len(hits) > 1:
            issues.append(
                _issue(
                    "device_identity_conflict",
                    f"{label}: the draft's identity aliases refer to different "
                    "configured devices. Reload the current config and review the "
                    "draft.",
                )
            )
            return None
        if hits:
            matched = hits[0]

    if (
        supplied_token
        and evidence is not None
        and evidence.opaque_tokens
        and not any(
            hmac.compare_digest(supplied_token, token)
            for token in evidence.opaque_tokens
        )
    ):
        issues.append(
            _issue(
                "device_identity_conflict",
                f"{label}: the server-issued device identity does not match the "
                "draft's physical or scoped route identity. Reload the current "
                "config and review the draft.",
            )
        )
        return None

    candidates = [
        candidate for candidate in (named, matched, token_match) if candidate is not None
    ]
    if candidates and any(candidate is not candidates[0] for candidate in candidates[1:]):
        issues.append(
            _issue(
                "device_identity_conflict",
                f"{label}: the draft name, physical identity and scoped route "
                "identity refer to different configured devices. Reload the "
                "current config and review the draft.",
            )
        )
        return None
    if supplied_token and evidence is None and named is None and token_match is None:
        issues.append(
            _issue(
                "device_identity_conflict",
                f"{label}: the server-issued device identity is unknown or stale. "
                "Reload the current config and review the draft.",
            )
        )
        return None

    resolved = named or token_match or matched
    # A shared route with a different serial is a conflict only for an
    # identity/token match — a device the draft did not name. When the draft
    # authoritatively names the entry (``original_name``), the user is editing
    # that one device, so correcting/changing its serial on the same route is a
    # legitimate edit, not a contradiction between two devices.
    if (
        named is None
        and resolved is not None
        and evidence is not None
        and identity_evidence_conflict(evidence, evidence_by_id.get(id(resolved)))
    ):
        issues.append(
            _issue(
                "device_identity_conflict",
                f"{label}: this Cloud route is already bound to a different "
                "physical serial. Reload the current config and review the draft.",
            )
        )
        return None

    if named is not None:
        return named
    fallback = token_match or matched
    if fallback is not None and id(fallback) not in claimed_ids:
        return fallback
    return None


def _restore_unchanged_cloud_display_name(original, item, broker_sources):
    """Keep a redacted no-op name byte-exact without trusting browser input."""

    if not isinstance(original, dict) or not isinstance(item, dict):
        return item
    mqtt = original.get("mqtt")
    if not isinstance(mqtt, dict):
        return item
    broker_ref = str(mqtt.get("broker_ref") or "default").strip()
    source = str(mqtt.get("source") or broker_sources.get(broker_ref) or "")
    if source != "zendure_cloud_mqtt":
        return item
    original_name = str(original.get("name") or "").strip()
    sensitive = frozenset(
        raw.strip()
        for raw in (mqtt.get("device_id"), mqtt.get("product_key"))
        if isinstance(raw, str) and raw.strip()
    )
    safe_name = mask_external_mqtt_string(
        original_name, sensitive_values=sensitive, cloud_scoped=True
    )
    if safe_name == original_name or str(item.get("name") or "").strip() != safe_name:
        return item
    restored = copy.deepcopy(item)
    restored["name"] = original_name
    return restored


def _merge_devices(merged, devices, issues, *, identity_token_key=None):
    if not isinstance(devices, list):
        return
    originals = _config_devices(merged)
    by_name = {}
    for device in originals:
        key = str(device.get("name") or "")
        if key and key not in by_name:
            by_name[key] = device
    broker_sources = broker_sources_from_config(merged)
    identity_index = {}
    token_index = {}
    evidence_by_id = {}
    for device in originals:
        evidence = resolve_inverter_identity_evidence(
            device,
            broker_sources=broker_sources,
            token_key=identity_token_key,
        )
        if evidence is None:
            continue
        evidence_by_id[id(device)] = evidence
        # Index every trusted alias (serial AND scoped route AND endpoint), not
        # only the strongest one, so a route-only entry still matches a later
        # serial-bearing observation of the same route. A key shared by two
        # entries maps to None (ambiguous) and fails closed on match.
        for key in evidence.comparison_keys:
            identity_index[key] = None if key in identity_index else device
        for token in evidence.opaque_tokens:
            token_index[token] = None if token in token_index else device
    claimed_ids = {
        id(by_name[str(item.get("original_name") or "")])
        for item in devices
        if isinstance(item, dict) and str(item.get("original_name") or "") in by_name
    }
    allocation_names = [str(device.get("name") or "").strip() for device in originals]
    allocation_count = len(originals)
    defaults = device_common_defaults()

    result = []
    for item in devices:
        if not isinstance(item, dict) or item.get("removed") is True:
            continue
        original = _resolve_original_device(
            item,
            by_name,
            identity_index,
            token_index,
            evidence_by_id,
            claimed_ids,
            issues,
            broker_sources=broker_sources,
            identity_token_key=identity_token_key,
        )
        item = _restore_unchanged_cloud_display_name(
            original, item, broker_sources
        )
        if original is not None:
            claimed_ids.add(id(original))
        if original is None and "name" not in item:
            item = copy.deepcopy(item)
            item["name"] = next_compact_inverter_name(
                allocation_names, allocation_count
            )
        if original is None:
            allocation_count += 1
            name = str(item.get("name") or "").strip()
            if name:
                allocation_names.append(name)
        if _is_mqtt_draft_item(item):
            device = materialize_maintenance_device(
                existing_device=original,
                draft_item=item,
                transport="zendure_mqtt",
                defaults=defaults,
            )
            if not is_zendure_mqtt_device_config(original):
                _resolve_new_device_broker(merged, device, item, issues)
        else:
            device = materialize_maintenance_device(
                existing_device=original,
                draft_item=item,
                transport="local_api",
                defaults=defaults,
            )
        result.append(device)
    merged["devices"] = result


def _resolve_new_device_broker(merged, device, item, issues):
    """Persist the broker profile a newly added Zendure MQTT device references.

    Runs the same shared resolver as Fresh Setup on the endpoint the browser
    passed through from the trusted discovery proposal: a matching existing
    profile (any ref) is reused, a new endpoint provisions its own profile, and
    a ref that already exists with different connection data is rejected with an
    actionable conflict instead of being silently replaced. An existing device
    edit never reaches this path, so operator-declared profiles are never
    rewritten by the device editor.
    """

    broker = item.get("broker")
    if not isinstance(broker, dict) or not broker:
        return
    label = str(device.get("name") or "Zendure MQTT device").strip()
    validation = {"errors": issues}
    try:
        endpoint = broker_endpoint(
            {
                "broker_host": broker.get("host"),
                "broker_port": broker.get("port"),
                "broker_tls": broker.get("tls"),
                "broker_tls_insecure": broker.get("tls_insecure"),
                "broker_tls_mode": broker.get("tls_mode"),
                "credentials_ref": broker.get("credentials_ref"),
                "connection_source": broker.get("source"),
            }
        )
    except BrokerEndpointError as exc:
        issues.append(_issue("zendure_mqtt_broker_endpoint_invalid", f"{label}: {exc}."))
        return
    mqtt = device.get("mqtt") if isinstance(device.get("mqtt"), dict) else {}
    ref = str(broker.get("ref") or "").strip() or str(mqtt.get("broker_ref") or "").strip()
    if not ref:
        return
    resolved = resolve_broker_ref(
        merged,
        ref,
        endpoint,
        label,
        validation,
        default_zendure_cloud_auth_available,
        ref_conflict="reject",
    )
    if resolved is None:
        return
    if isinstance(device.get("mqtt"), dict):
        device["mqtt"]["broker_ref"] = resolved


def _merge_zendure_mqtt_broker(merged, broker):
    """Write the top-level Zendure MQTT broker from its draft, secrets preserved.

    An empty password field keeps the stored password; an explicit
    ``clear_password`` removes it. The ``brokers`` sub-profiles are never touched
    here.
    """

    if not isinstance(broker, dict) or not broker:
        return
    existing = merged.get("zendure_mqtt")
    existing_is_dict = isinstance(existing, dict)
    top_host_present = existing_is_dict and bool(str(existing.get("host") or "").strip())
    # Named-broker configs are owned by the setup/proposal flow. The maintenance
    # top-level broker form never writes legacy top-level host/port/tls beside
    # named profiles, so an unchanged draft is a strict no-op for them.
    if str(broker.get("managed") or "").strip() == "named" or (
        _has_named_brokers(existing) and not top_host_present
    ):
        return
    host = str(broker.get("host") or "").strip()
    if broker.get("present") is False and not host and not existing_is_dict:
        return
    creating = not existing_is_dict
    target = existing if existing_is_dict else {}
    if creating:
        merged["zendure_mqtt"] = target

    # The telemetry feature is always on, so the removed top-level ``enabled``
    # toggle is never written; an existing legacy key round-trips untouched
    # (the runtime ignores it). Optional keys are only written when the profile
    # already tracks them or the user supplies a value, so a minimal legacy
    # broker is never migrated with injected port/tls fields on a no-op apply.
    if host:
        target["host"] = host
    elif "host" in target:
        target["host"] = host
    if broker.get("port") not in (None, ""):
        target["port"] = _coerce_number(broker.get("port"))
    elif creating and host:
        target["port"] = 1883
    if broker.get("tls"):
        target["tls"] = True
    elif "tls" in target:
        target["tls"] = bool(broker.get("tls"))
    username = str(broker.get("username") or "").strip()
    if username:
        target["username"] = username
    elif "username" in target:
        target.pop("username", None)
    if broker.get("clear_password"):
        target.pop("password", None)
    else:
        password = broker.get("password")
        if isinstance(password, str) and password:
            target["password"] = password


def _apply_device_fields(device, item):
    if "name" in item:
        device["name"] = str(item.get("name") or "").strip()
    if "ip" in item:
        device["ip"] = str(item.get("ip") or "").strip()
    if "sn" in item:
        device["sn"] = str(item.get("sn") or "").strip()
    for key, field in _DEVICE_VALUE_FIELDS.items():
        if key in item:
            device[key] = _coerce(field, item[key])
    # Keep unknown (non-catalog) device keys untouched; only surface an
    # explicit enabled flag so a disabled draft device reads as a real change.
    enabled = bool(item.get("enabled", True))
    if "enabled" in device or item.get("has_enabled_key") or not enabled:
        device["enabled"] = enabled


def _coerce_number(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if not text:
        return value
    try:
        number = float(text)
    except ValueError:
        return value
    return int(number) if number.is_integer() else number


def _merge_grid_meter(merged, grid_meter):
    if not isinstance(grid_meter, dict):
        return
    if grid_meter.get("present") is False:
        merged.pop("grid_meter", None)
        return
    target = merged.get("grid_meter")
    if not isinstance(target, dict):
        target = {}
        merged["grid_meter"] = target
    original_type = str(target.get("type") or "").strip().lower()
    if grid_meter.get("type"):
        target["type"] = str(grid_meter["type"]).strip().lower()
    new_type = str(target.get("type") or "").strip().lower()
    if "ip" in grid_meter:
        ip_value = str(grid_meter.get("ip") or "").strip()
        # Never introduce an empty ip on a meter that never had one: a flat MQTT
        # meter keeps its connection in flat/nested MQTT fields, not in ip.
        if ip_value or "ip" in target:
            target["ip"] = ip_value
    if grid_meter.get("port") is not None:
        target["port"] = _coerce_number(grid_meter["port"])
    for key in ("url", "power_path"):
        if grid_meter.get(key):
            target[key] = str(grid_meter[key]).strip()
    channels = grid_meter.get("channels")
    if isinstance(channels, list):
        target["channels"] = [str(item).strip() for item in channels if str(item).strip()]
    # Stale-key cleanup belongs only to an explicit type change. A no-op apply
    # must not migrate a Core-supported legacy flat MQTT meter by deleting its
    # connection fields (host/port/topic/…), which would pass Maintenance
    # validation yet be rejected by EMS Core at startup.
    if new_type != original_type:
        _strip_stale_grid_meter_keys(target)
    if new_type in _MQTT_GRID_METER_TYPES:
        _merge_grid_meter_mqtt(target, grid_meter.get("mqtt"))


def _merge_grid_meter_mqtt(target, draft_mqtt):
    """Write edited MQTT grid-meter values, preserving the stored representation.

    A value is only written when it differs from the currently stored one, so
    an unchanged draft stays a byte-level no-op for both nested and legacy flat
    configs; edits keep the representation the config already uses (a flat
    meter stays flat, a nested one stays nested). The password follows the
    broker rules: blank keeps the stored secret, ``clear_password`` removes it,
    a non-empty value replaces it.
    """

    if not isinstance(draft_mqtt, dict) or not draft_mqtt:
        return
    current = grid_meter_mqtt_settings(target)
    nested = target.get("mqtt")
    has_nested = isinstance(nested, dict)
    has_flat = any(key in target for key in _MQTT_GRID_METER_FLAT_KEYS)

    def container():
        nonlocal nested, has_nested
        if has_nested:
            return nested
        if has_flat:
            return target
        nested = {}
        target["mqtt"] = nested
        has_nested = True
        return nested

    for key in _MQTT_GRID_METER_EDIT_KEYS:
        if key not in draft_mqtt:
            continue
        value = draft_mqtt.get(key)
        if value == current.get(key):
            continue
        if isinstance(value, str):
            value = value.strip()
        if key in ("port", "max_age_seconds") and value not in (None, ""):
            value = _coerce_number(value)
        if value == current.get(key):
            continue
        dest = container()
        if value in (None, ""):
            dest.pop(key, None)
        else:
            dest[key] = value

    secret_container = nested if has_nested else (target if has_flat else None)
    if draft_mqtt.get("clear_password"):
        if secret_container is not None:
            secret_container.pop("password", None)
    else:
        password = draft_mqtt.get("password")
        if isinstance(password, str) and password:
            container()["password"] = password


def _strip_stale_grid_meter_keys(target):
    """Drop keys that do not belong to the selected grid meter type.

    Keeps a type switch clean, e.g. moving from Tasmota/MQTT to a plain HTTP/IP
    meter must not leave ``url``/``power_path``/``mqtt`` (or flat MQTT keys) behind.
    """
    meter_type = str(target.get("type") or "").strip().lower()
    if meter_type in _MQTT_GRID_METER_TYPES:
        for key in ("ip", "url", "power_path", "channels", *_MQTT_GRID_METER_FLAT_KEYS):
            target.pop(key, None)
        return
    target.pop("mqtt", None)
    for key in _MQTT_GRID_METER_FLAT_KEYS:
        target.pop(key, None)
    if meter_type != "tasmota_http":
        target.pop("url", None)
        target.pop("power_path", None)
    if meter_type not in ("shelly", "shelly_3em_gen1"):
        target.pop("channels", None)


def _merge_features(merged, features):
    if not isinstance(features, dict):
        return
    index = _maintenance_field_index()
    for path, raw_value in features.items():
        field = index.get(path)
        if field is None:
            continue
        _set_path(merged, path, _coerce(field, raw_value))


# --- validation ----------------------------------------------------------


def _has_enabled_cloud_control_device(config):
    brokers = (config.get("zendure_mqtt") or {}).get("brokers")
    if not isinstance(brokers, dict):
        return False
    cloud_refs = {
        ref
        for ref, broker in brokers.items()
        if isinstance(broker, dict)
        and broker.get("source") == "zendure_cloud_mqtt"
    }
    if not cloud_refs:
        return False
    for device in config.get("devices") or []:
        if not isinstance(device, dict) or device.get("enabled") is False:
            continue
        if not is_control_zendure_mqtt_device_config(device):
            continue
        if (device.get("mqtt") or {}).get("broker_ref") in cloud_refs:
            return True
    return False


_COMMON_DEVICE_VALUE_KEYS = device_common_field_keys()


def _append_common_value_warning(validation, device, label, transport_label):
    """Flag an enabled control device that lacks common tuning values.

    A warning, not an error: legacy configs relying on runtime fallbacks stay
    applyable byte-for-byte, but an incomplete control device can no longer
    pass preview as silently fully configured.
    """

    if device.get("enabled") is False:
        return
    missing = [key for key in _COMMON_DEVICE_VALUE_KEYS if key not in device]
    if not missing:
        return
    validation["warnings"].append(
        _issue(
            "device_common_values_missing",
            f"{label} ({transport_label}): no configured value for "
            f"{', '.join(missing)}; the EMS runtime falls back to built-in "
            "defaults. Central defaults are added per device only when it is "
            "newly created or its transport is switched; existing entries are "
            "not changed retroactively.",
        )
    )


def _validate(config, merge_issues=()):
    validation = {"errors": list(merge_issues), "warnings": [], "info": []}
    # A reserved named broker ref (``default``) is rejected regardless of devices:
    # it collides with the implicit legacy top-level broker's identity.
    for issue in find_reserved_mqtt_broker_ref_issues(config):
        validation["errors"].append(_issue(issue["code"], issue["message"]))
    devices = config.get("devices")
    if not isinstance(devices, list) or not devices:
        validation["errors"].append(_issue("no_devices", "At least one inverter is required."))
    else:
        if not has_runtime_control_device(config):
            validation["errors"].append(
                _issue(
                    "no_control_devices",
                    "At least one enabled API or MQTT output-control inverter is required; telemetry-only MQTT devices cannot run the EMS control loop.",
                )
            )
        broker_views = zendure_mqtt_broker_profile_views(config.get("zendure_mqtt"))
        known_refs = set(broker_views)
        zmqtt = config.get("zendure_mqtt")
        brokers_defined = isinstance(zmqtt, dict) and bool(zmqtt.get("brokers"))
        names = []
        for index, device in enumerate(devices, 1):
            if not isinstance(device, dict):
                validation["errors"].append(
                    _issue("device_invalid", f"Inverter {index} is not a valid entry.")
                )
                continue
            name = str(device.get("name") or "").strip()
            names.append(name)
            label = name or f"inverter {index}"
            if not name:
                validation["errors"].append(
                    _issue("device_name_empty", f"Inverter {index} needs a config name.")
                )
            if is_zendure_mqtt_device_config(device):
                # Zendure MQTT devices never carry ip/sn. Pick the Core validator
                # by capability so an existing control (write-capable) device is
                # not forced off to satisfy the telemetry-only shape check.
                validator = (
                    validate_zendure_mqtt_control_device_config
                    if is_control_zendure_mqtt_device_config(device)
                    else validate_zendure_mqtt_device_config
                )
                for issue in validator(
                    device,
                    known_broker_refs=known_refs,
                    brokers_defined=brokers_defined,
                ):
                    if issue.get("severity") == "error":
                        validation["errors"].append(
                            _issue(issue["code"], f"{label}: {issue['message']}.")
                        )
                # Telemetry-only MQTT devices never need control tuning values.
                if is_control_zendure_mqtt_device_config(device):
                    _append_common_value_warning(
                        validation, device, label, "Zendure MQTT"
                    )
                continue
            if not _valid_host(device.get("ip")):
                validation["errors"].append(
                    _issue("device_host_invalid", f"{label} has an invalid IP address or hostname.")
                )
            serial = str(device.get("sn") or "").strip()
            if not serial or serial == _SERIAL_PLACEHOLDER:
                validation["errors"].append(
                    _issue("device_serial_missing", f"{label} requires a serial number.")
                )
            _append_common_value_warning(validation, device, label, "Local API")
        duplicates = sorted({name for name in names if name and names.count(name) > 1})
        if duplicates:
            validation["errors"].append(
                _issue(
                    "device_name_duplicate",
                    f"Config names must be unique: {', '.join(duplicates)}.",
                )
            )
        for issue in find_duplicate_zendure_device_identities(
            devices, broker_sources=broker_sources_from_config(config)
        ):
            validation["errors"].append(_issue(issue["code"], issue["message"]))
        for issue in find_zendure_mqtt_broker_profile_issues(config):
            validation["errors"].append(_issue(issue["code"], issue["message"]))

    grid_meter = config.get("grid_meter")
    if isinstance(grid_meter, dict):
        meter_type = str(grid_meter.get("type") or "").strip().lower()
        if meter_type and meter_type not in _GRID_TYPE_CHOICES:
            validation["errors"].append(
                _issue("grid_meter_type_invalid", f"Unknown grid meter type: {meter_type}.")
            )
        if meter_type in _HOST_GRID_METER_TYPES and not _valid_host(grid_meter.get("ip")):
            if not (meter_type == "tasmota_http" and grid_meter.get("url")):
                validation["errors"].append(
                    _issue("grid_meter_host_invalid", "The grid meter has an invalid IP or hostname.")
                )
        # Parity with the Core resolver EMS runs at startup: an MQTT grid meter
        # that Core rejects (missing host/topic, unknown/disabled broker ref,
        # invalid port, conflicting inline settings) must fail Maintenance before
        # a payload is written, not silently pass and abort EMS boot. Core
        # messages carry no secrets, so they are safe to surface.
        if meter_type in _MQTT_GRID_METER_TYPES:
            try:
                resolved = resolve_grid_meter_mqtt_settings(config)
                normalize_mqtt_grid_meter_settings(
                    {"type": meter_type, "mqtt": resolved}, meter_type=meter_type
                )
            except MqttBrokerReferenceAmbiguousError as exc:
                validation["errors"].append(_issue(exc.code, str(exc)))
            except ValueError as exc:
                validation["errors"].append(
                    _issue("grid_meter_mqtt_invalid", str(exc))
                )
    else:
        validation["warnings"].append(_issue("grid_meter_missing", "No grid meter is configured."))

    if _has_enabled_cloud_control_device(config):
        validation["warnings"].append(
            _issue(
                "zendure_cloud_mqtt_single_controller",
                "Zendure Cloud MQTT output control is enabled: use only one "
                "active controller. Disable Zendure HEMS, Smart Matching, "
                "Zendure schedules and any other system that writes inverter "
                "power.",
            )
        )

    try:
        json.dumps(config, allow_nan=False)
    except (TypeError, ValueError):
        validation["errors"].append(
            _issue("config_not_serializable", "The resulting config is not valid JSON data.")
        )

    validation["ok"] = not validation["errors"]
    return validation


def _append_mqtt_credential_consumer_issues(validation, config):
    """Add the global MQTT credential contract issues to a validation result.

    Used by the read-only preview only: it enforces the same canonical/
    single-source contract as Core config validation and Admin Apply, keeping
    ``validation["ok"]`` in sync when a bad reference is present.
    """

    for issue in find_mqtt_credential_consumer_issues(config):
        validation["errors"].append(_issue(issue["code"], issue["message"]))
    validation["ok"] = not validation["errors"]


# --- diff ----------------------------------------------------------------


def summarize_config_changes(before, after):
    """Return a compact, bounded diff of leaf-value changes between two configs.

    Dict keys flatten with dots and list indices with ``[i]`` (so per-device
    fields read as ``devices[0].max_power``). Scalar lists compare as a single
    leaf; comment keys are skipped. Values are bounded for safe UI rendering.
    """

    before_leaves = {}
    after_leaves = {}
    _flatten(before, "", before_leaves)
    _flatten(after, "", after_leaves)

    def _bound(path, value):
        # A secret leaf (broker password, token, app key) is never surfaced in a
        # diff, even when its value changed.
        return _REDACTED if _is_secret_leaf(path) else _bounded(value)

    changes, added, removed = [], [], []
    for path in sorted(set(before_leaves) | set(after_leaves)):
        in_before = path in before_leaves
        in_after = path in after_leaves
        if in_before and in_after:
            if before_leaves[path] != after_leaves[path]:
                changes.append(
                    {
                        "path": path,
                        "before": _bound(path, before_leaves[path]),
                        "after": _bound(path, after_leaves[path]),
                    }
                )
        elif in_after:
            added.append({"path": path, "after": _bound(path, after_leaves[path])})
        else:
            removed.append({"path": path, "before": _bound(path, before_leaves[path])})

    return {
        "changed": bool(changes or added or removed),
        "changes": changes,
        "added": added,
        "removed": removed,
    }


def _flatten(value, prefix, out):
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key.startswith("_"):
                continue
            path = f"{prefix}.{key}" if prefix else str(key)
            _flatten(child, path, out)
        return
    if isinstance(value, list) and any(isinstance(item, dict) for item in value):
        for index, child in enumerate(value):
            _flatten(child, f"{prefix}[{index}]", out)
        return
    out[prefix] = value


__all__ = [
    "load_maintenance_config",
    "build_maintenance_draft",
    "materialize_maintenance_device",
    "preview_maintenance_config",
    "prepare_maintenance_config_apply",
    "summarize_config_changes",
    "redact_config_for_browser",
]
