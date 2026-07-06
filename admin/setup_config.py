# SPDX-License-Identifier: AGPL-3.0-or-later
"""Setup-facing view of the central config catalog for the Admin wizard.

Admin stays an orchestration/UI layer: the reference for possible options is the
central catalog in :mod:`ems.config_catalog`. This module turns the catalog into
a setup-only payload the frontend can render, and applies user-selected feature
values on top of an EMS config preview. It never exposes secret values and never
duplicates EMS runtime logic.
"""

import copy

from ems.config_catalog import (
    GRID_METER_VARIANTS,
    INVERTER_CONNECTION_VARIANTS,
    SETUP_GROUPS,
    get_config_feature_field_index,
    get_config_feature_sections,
)


def _is_secret(field):
    return field.get("risk") == "secret" or field.get("type") == "password"


def _annotate_secret(field):
    secret = _is_secret(field)
    field["secret"] = secret
    if secret:
        # Never surface a secret default value to the setup UI.
        field.pop("default", None)
    return field


def build_setup_catalog():
    """Return the setup-mode catalog payload for the Admin UI.

    Only sections/fields in the ``setup`` flow are included. Deprecated/legacy
    integrations (e.g. Home Assistant control) stay out of the recommended setup
    list because the catalog scopes them to maintenance. Secret fields are
    marked and never carry a value.
    """

    sections = get_config_feature_sections(mode="setup")
    for section in sections:
        for field in section["fields"]:
            _annotate_secret(field)

    variants = {}
    for key, variant in GRID_METER_VARIANTS.items():
        if variant.get("scope") == "maintenance" or variant.get("level") == "deprecated":
            continue
        variants[key] = {
            "id": key,
            "label": variant["label"],
            "description": variant["description"],
            "fields": list(variant.get("fields", ())),
            "level": variant.get("level", "normal"),
        }

    hardware_variants = {
        "inverter": [
            {
                "id": key,
                "label": variant["label"],
                "description": variant["description"],
                "default_port": variant.get("default_port"),
                "required_fields": list(variant.get("required_fields", ())),
                "default": bool(variant.get("default_manual")),
            }
            for key, variant in INVERTER_CONNECTION_VARIANTS.items()
            if variant.get("manual_setup")
        ],
        "grid_meter": [
            {
                "id": key,
                "label": variant["label"],
                "description": variant["description"],
                "default_port": variant.get("default_port"),
                "required_fields": ["host", "port"],
                "default": bool(variant.get("default_manual")),
            }
            for key, variant in GRID_METER_VARIANTS.items()
            if variant.get("manual_setup")
            and variant.get("scope") != "maintenance"
            and variant.get("level") != "deprecated"
        ],
    }

    groups = [copy.deepcopy(group) for group in SETUP_GROUPS]

    return {
        "mode": "setup",
        "groups": groups,
        "sections": sections,
        "grid_meter_variants": variants,
        "hardware_variants": hardware_variants,
    }


def _is_setup_writable_field(field):
    """True for catalog fields the guided setup flow is allowed to write.

    Maintenance-only, deprecated, hidden and read-only fields stay out of the
    setup scope even if a value for them is posted manually.
    """

    if field.get("scope") not in ("setup", "both"):
        return False
    if field.get("level") == "deprecated":
        return False
    if field.get("editable") is False:
        return False
    return True


def _setup_field_index():
    """Setup-writable catalog fields keyed by config path."""

    return {
        path: field
        for path, field in get_config_feature_field_index().items()
        if _is_setup_writable_field(field)
    }


_DEVICE_FIELD_PREFIX = "devices[]."
# name/ip/sn have dedicated draft-item mapping in config_preview; keeping them
# out of the config-values index guarantees per-device overrides can never
# rewrite device identity fields.
_DEVICE_MAPPED_KEYS = frozenset({"name", "ip", "sn"})


def _device_field_index():
    """Flat ``devices[]`` catalog fields keyed by their per-device key.

    Only setup-writable, single-segment device keys are included, so applying
    values can never reach nested structures or non-device config paths.
    """

    index = {}
    for path, field in get_config_feature_field_index().items():
        if not path.startswith(_DEVICE_FIELD_PREFIX):
            continue
        if not _is_setup_writable_field(field):
            continue
        key = path[len(_DEVICE_FIELD_PREFIX):]
        if not key or "." in key or "[" in key:
            continue
        if key in _DEVICE_MAPPED_KEYS:
            continue
        index[key] = field
    return index


def _coerce(field, value):
    if value is None:
        return None
    field_type = field.get("type")
    if field_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if field_type == "integer":
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if field_type == "number":
        try:
            number = float(value)
        except (TypeError, ValueError):
            return value
        return int(number) if number.is_integer() else number
    if field_type in ("month_list", "integer_list"):
        return _coerce_int_list(value)
    if field_type == "string_list":
        return _coerce_string_list(value)
    return value


def _coerce_int_list(value):
    items = value
    if isinstance(value, str):
        items = [part for part in value.replace(";", ",").split(",")]
    if not isinstance(items, (list, tuple)):
        return value
    result = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        try:
            result.append(int(float(text)))
        except (TypeError, ValueError):
            return value
    return result


def _coerce_string_list(value):
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


def _set_path(config, path, value):
    parts = path.split(".")
    cursor = config
    for part in parts[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            child = {}
            cursor[part] = child
        cursor = child
    cursor[parts[-1]] = value


def apply_setup_features(config, features):
    """Apply catalog-driven setup feature values onto a config preview.

    ``features`` maps stable catalog config paths to user-entered values. Unknown
    paths and hidden/read-only fields are ignored so the UI cannot write outside
    the catalog. Device entries are owned by the device draft, not by features.
    Returns the list of applied paths.
    """

    if not isinstance(features, dict) or not features:
        return []

    index = _setup_field_index()
    applied = []
    for path, raw_value in features.items():
        if not isinstance(path, str):
            continue
        field = index.get(path)
        if field is None:
            continue
        if "[]" in path:
            # Repeated device entries flow through the device draft, not here.
            continue
        _set_path(config, path, _coerce(field, raw_value))
        applied.append(path)
    return applied


def apply_device_config_values(device, values):
    """Apply catalog-backed per-device setup values onto a generated device.

    ``values`` maps flat device field keys (e.g. ``max_power``) to user-entered
    values from the inverter draft row. Only known ``devices[]`` catalog fields
    are written, coerced to the catalog field type; unknown keys and identity
    fields (name/ip/sn) are ignored so the UI can never write outside the device
    object. Returns the list of applied keys.
    """

    if not isinstance(device, dict) or not isinstance(values, dict) or not values:
        return []

    index = _device_field_index()
    applied = []
    for key, raw_value in values.items():
        if not isinstance(key, str):
            continue
        field = index.get(key)
        if field is None:
            continue
        device[key] = _coerce(field, raw_value)
        applied.append(key)
    return applied


__all__ = [
    "build_setup_catalog",
    "apply_setup_features",
    "apply_device_config_values",
]
