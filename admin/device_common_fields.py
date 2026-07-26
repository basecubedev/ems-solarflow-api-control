# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared transport-independent inverter value handling for Admin flows.

One catalog-derived definition of the common (transport-independent) device
value set, its coercion and its default materialization. Both the Local API
and the Zendure MQTT maintenance paths project these values through this
module, so a future common ``devices[]`` catalog field reaches every editor
and merge path without a second transport-specific implementation.
Identity/connection fields (``name``/``ip``/``sn``/``mqtt.*``) stay explicitly
owned by each transport.
"""

import copy

from ems.config_catalog import (
    DEVICE_IDENTITY_FIELD_KEYS,
    device_common_defaults,
    get_config_feature_field_index,
)


def coerce_field_value(field, value):
    """Coerce a browser-supplied value by its catalog field type."""

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


def is_editable_maintenance_field(field):
    """True for catalog fields an Admin editor may write.

    Deprecated, read-only and secret fields stay out so no editor can surface
    a secret value or write outside the catalog.
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


def common_device_value_fields():
    """Editable common device catalog fields keyed by their device key.

    Derived from the central catalog so the editable per-device value set is
    never a flow-local copy; identity keys keep dedicated handling.
    """

    fields = {}
    for path, field in get_config_feature_field_index().items():
        if not path.startswith("devices[]."):
            continue
        key = path[len("devices[].") :]
        if key in DEVICE_IDENTITY_FIELD_KEYS:
            continue
        if not is_editable_maintenance_field(field):
            continue
        fields[key] = field
    return fields


def apply_common_device_values(device, item, fields=None):
    """Write coerced common values present in a draft entry onto a device."""

    if fields is None:
        fields = common_device_value_fields()
    for key, field in fields.items():
        if key in item:
            device[key] = coerce_field_value(field, item[key])


def common_device_draft_values(device):
    """Common values stored on a config device, for the editable draft."""

    values = {}
    for key in common_device_value_fields():
        if key in device:
            values[key] = copy.deepcopy(device[key])
    return values


def materialize_common_device_defaults(device):
    """Fill missing common values with the central defaults.

    Existing explicit values (including explicit ``0``) are never replaced;
    only absent keys inherit the catalog/template default. Callers apply this
    to newly created devices and transport switches — never to an untouched
    existing device, whose byte-exact shape must survive a no-op apply.
    """

    for key, value in device_common_defaults().items():
        if key not in device:
            device[key] = copy.deepcopy(value)
    return device


__all__ = [
    "coerce_field_value",
    "is_editable_maintenance_field",
    "common_device_value_fields",
    "apply_common_device_values",
    "common_device_draft_values",
    "materialize_common_device_defaults",
]
