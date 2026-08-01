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
    GRID_METER_KNOWN_MQTT_KEYS,
    GRID_METER_KNOWN_TOP_KEYS,
    GRID_METER_VARIANTS,
    INVERTER_CONNECTION_VARIANTS,
    SETUP_GROUPS,
    ZENDURE_MQTT_BROKER_FIELDS,
    ZENDURE_MQTT_BROKER_HELP,
    ZENDURE_MQTT_GENERATIONS,
    get_config_feature_field_index,
    get_config_feature_sections,
    grid_meter_variant_field_spec,
)
from ems.mqtt_control.zendure_profiles import hardware_profile_selector_options
from admin.device_common_fields import coerce_field_value
from admin.zendure_mqtt_config_draft import generation_supports_output_control


def _is_secret(field):
    return field.get("risk") == "secret" or field.get("type") == "password"


def _annotate_secret(field):
    secret = _is_secret(field)
    field["secret"] = secret
    if secret:
        # Never surface a secret default value to the setup UI.
        field.pop("default", None)
    return field


def grid_meter_variant_catalog():
    """Serializable grid-meter variant map shared by Setup and Maintenance.

    The per-variant ``fields`` list is the field-visibility contract the
    hardware editors switch on. Deprecated/legacy variants stay out.
    """

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
    return variants


def mqtt_grid_meter_keys():
    """Every ``grid_meter.mqtt`` key any known variant may carry.

    Derived, so a field added to ``GRID_METER_VARIANTS`` is known to both Admin
    flows at once instead of waiting for a hand-maintained mirror list.
    """

    return frozenset(GRID_METER_KNOWN_MQTT_KEYS)


def strip_incompatible_grid_meter_fields(grid, grid_type):
    """Drop grid-meter fields that belong to a different variant.

    The allowed keys come from the EMS-owned catalog
    (:func:`grid_meter_variant_field_spec`), so both Admin flows clean up a
    variant switch the same way — including a switch *inside* the MQTT family,
    where a coarse MQTT/non-MQTT split leaves keys the target variant cannot
    carry. Only keys belonging to some known variant are eligible for removal;
    operator-defined custom keys survive untouched.
    """

    spec = grid_meter_variant_field_spec(grid_type)
    if spec is None:
        # Unknown type: fall back to the coarse HTTP<->MQTT split.
        grid.pop("mqtt", None)
        return

    allowed = spec["keys"]
    for key in list(grid.keys()):
        if key in GRID_METER_KNOWN_TOP_KEYS and key not in allowed:
            grid.pop(key, None)

    mqtt = grid.get("mqtt")
    if isinstance(mqtt, dict):
        allowed_mqtt = spec["mqtt_keys"]
        if not allowed_mqtt:
            grid.pop("mqtt", None)
        else:
            for key in list(mqtt.keys()):
                if key in GRID_METER_KNOWN_MQTT_KEYS and key not in allowed_mqtt:
                    mqtt.pop(key, None)


def hardware_section_catalog(mode):
    """Hardware (grid_meter/devices) catalog sections for an Admin flow.

    Secret fields are marked and never carry a value, mirroring the setup
    catalog annotation, so no flow can surface a stored secret.
    """

    sections = []
    for section in get_config_feature_sections(mode=mode):
        if section.get("setup_group") != "hardware":
            continue
        for field in section["fields"]:
            _annotate_secret(field)
        sections.append(section)
    return sections


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

    variants = grid_meter_variant_catalog()

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

    broker_fields = [_annotate_secret(dict(field)) for field in ZENDURE_MQTT_BROKER_FIELDS]
    generations = [
        {
            "id": key,
            "label": profile["label"],
            "description": profile["description"],
            "product_key": profile["product_key"],
            "default": profile["default"],
            "supports_output_control": generation_supports_output_control(key),
        }
        for key, profile in ZENDURE_MQTT_GENERATIONS.items()
    ]

    return {
        "mode": "setup",
        "groups": groups,
        "sections": sections,
        "grid_meter_variants": variants,
        "hardware_variants": hardware_variants,
        "zendure_mqtt_broker": {"help": ZENDURE_MQTT_BROKER_HELP, "fields": broker_fields},
        "zendure_mqtt_generations": generations,
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
            for option in hardware_profile_selector_options()
        ],
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


# Catalog field coercion is shared with the Zendure MQTT draft path; it lives
# in admin.device_common_fields (below this module in the import graph).
_coerce = coerce_field_value


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
