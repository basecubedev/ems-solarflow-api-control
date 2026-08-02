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
    ZENDURE_MQTT_BROKER_FIELDS,
    ZENDURE_MQTT_BROKER_HELP,
    config_field_index,
    get_config_feature_sections,
    is_editable_catalog_field,
)
from ems.config_mutation import (
    GRID_METER_PREFIX,
    SETUP_POLICY,
    ConfigChange,
    apply_common_values,
    apply_config_changes,
    apply_grid_meter_changes,
)
from ems.mqtt_control.zendure_profiles import hardware_profile_selector_options
from admin.secret_policy import is_secret_catalog_field
from admin.zendure_mqtt_config_draft import generation_catalog


def _is_secret(field):
    return is_secret_catalog_field(field)


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
    # One generation projection for Setup and Maintenance: it carries the
    # Core-derived broker sources that can control each generation, which the
    # browser must never restate.
    generations = generation_catalog()

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
    """True for catalog fields the guided setup flow is allowed to write."""

    return is_editable_catalog_field(field, scope="setup")


def _setup_field_index():
    """Setup-writable catalog fields keyed by config path."""

    return config_field_index(scope="setup")


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

    return config_field_index(
        scope="setup",
        prefix=_DEVICE_FIELD_PREFIX,
        flat_keys=True,
        exclude_keys=_DEVICE_MAPPED_KEYS,
    )


def apply_setup_features(config, features):
    """Apply catalog-driven setup feature values onto a config preview.

    ``features`` maps stable catalog config paths to user-entered values. Unknown
    paths and hidden/read-only fields are ignored so the UI cannot write outside
    the catalog. Device entries are owned by the device draft, not by features.
    Grid-meter paths go through the canonical grid-meter mutation so a variant
    switch, a cleared field and a padded value read the same here as they do in
    Maintenance. Returns the list of applied paths.
    """

    if not isinstance(config, dict) or not isinstance(features, dict) or not features:
        return []

    index = _setup_field_index()
    grid_changes, other_changes = [], []
    for path, raw_value in features.items():
        if not isinstance(path, str):
            continue
        if path.startswith(GRID_METER_PREFIX):
            grid_changes.append(ConfigChange(path[len(GRID_METER_PREFIX):], raw_value))
        else:
            other_changes.append(ConfigChange(path, raw_value))

    applied = list(apply_config_changes(config, other_changes, SETUP_POLICY, field_index=index).applied_paths)
    if grid_changes:
        grid = config.get("grid_meter")
        if not isinstance(grid, dict):
            grid = {}
            config["grid_meter"] = grid
        applied.extend(
            apply_grid_meter_changes(
                grid, grid_changes, SETUP_POLICY, field_index=index
            ).applied_paths
        )
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

    applied = apply_common_values(device, values, _device_field_index())
    return [change.path for change in applied]


__all__ = [
    "build_setup_catalog",
    "apply_setup_features",
    "apply_device_config_values",
]
