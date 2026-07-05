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

    return {
        "mode": "setup",
        "sections": sections,
        "grid_meter_variants": variants,
    }


def _setup_field_index():
    """Editable, non-hidden catalog fields keyed by config path."""

    index = {}
    for path, field in get_config_feature_field_index().items():
        if field.get("editable") is False:
            continue
        if field.get("scope") == "hidden":
            continue
        index[path] = field
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


__all__ = ["build_setup_catalog", "apply_setup_features"]
