# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The operating settings in the Admin console.

An operating setting is one an operator changes while the EMS runs; it lives
in ``runtime-state.json``, which the dashboard, ``emsctl`` and the scheduler
write too (docs/developer/settings-operation-store.md). The Admin shows each
as the EMS takes it and writes a change through the same runtime service,
never into ``config.json``. Which settings these are is named once, in
``ems.runtime_commands`` (``OPERATION_SETTING_PATHS``, ``OPERATION_DEVICE_KEYS``).
"""

import copy

from admin.config_runtime_overlap import overlap_provenance_for_context
from admin.runtime_convergence import device_holder, write_targets
from ems.runtime_commands import (
    OPERATION_DEVICE_KEYS,
    OPERATION_SETTING_PATHS,
    RuntimeWriteError,
    build_validation_context,
    validate_device_values,
    validate_section_values,
    validate_system_values,
)


def operating_values(context, config):
    """Each operating setting as the EMS takes it: ``(features, devices)``.

    ``features`` maps a setting path to its value; ``devices`` maps a config
    device name to its operating device values.
    """

    return operating_from(overlap_provenance_for_context(context, config))


def operating_from(provenance):
    """The operating values of an overlap provenance map, as ``operating_values`` answers."""

    features = {
        path: provenance[path]["effective_value"]
        for path in OPERATION_SETTING_PATHS
        if path in provenance
    }
    devices = {
        name: {key: entry[key]["effective_value"] for key in OPERATION_DEVICE_KEYS if key in entry}
        for name, entry in (provenance.get("devices") or {}).items()
        if isinstance(entry, dict)
    }
    return features, devices


def installation_provenance(provenance):
    """The provenance map without the operating settings, which have no second value to compare."""

    kept = {path: entry for path, entry in provenance.items() if path not in OPERATION_SETTING_PATHS and path != "devices"}
    kept["devices"] = {
        name: {key: value for key, value in entry.items() if key not in OPERATION_DEVICE_KEYS}
        for name, entry in (provenance.get("devices") or {}).items()
        if isinstance(entry, dict)
    }
    return kept


def adopt_operating_values(draft, features, devices):
    """Put the operating values into an editable draft in place of the config's."""

    draft.setdefault("features", {}).update(copy.deepcopy(features))
    for item in draft.get("devices") or []:
        if not isinstance(item, dict):
            continue
        values = devices.get(_device_name(item))
        if values:
            item.update(copy.deepcopy(values))
    return draft


def operation_changes(context, current, draft):
    """What a draft changes of the operating settings: ``(changes, targets, issues)``.

    ``changes`` are diff rows that take effect at once, ``targets`` what
    ``write_operation_changes`` writes, ``issues`` the values the runtime
    service refuses, as validation errors.
    """

    features, devices = operating_values(context, current)
    validation_context = build_validation_context(current, None)
    changes, targets, issues = [], [], []
    drafted = draft.get("features") if isinstance(draft.get("features"), dict) else {}
    for path in sorted(OPERATION_SETTING_PATHS & set(drafted)):
        before, after = features.get(path), drafted[path]
        if after == before:
            continue
        section, _, key = path.partition(".")
        try:
            if section == "system":
                value = validate_system_values({key: after}, validation_context)[key]
                target = (path, "system", None, key, value)
            else:
                value = validate_section_values(section, {key: after})[key]
                target = (path, "section", section, key, value)
        except RuntimeWriteError as exc:
            issues.append({"code": "operating_value_invalid", "message": f"{path}: {exc}"})
            continue
        changes.append({"path": path, "before": before, "after": value, "applies_live": True})
        targets.append(target)
    positions = _config_positions(current)
    for item in draft.get("devices") or []:
        if not isinstance(item, dict):
            continue
        name = _device_name(item)
        known = devices.get(name)
        if known is None or name not in positions:
            continue
        for key in sorted(OPERATION_DEVICE_KEYS & set(item)):
            before, after = known.get(key), item[key]
            if after == before:
                continue
            path = f"devices[{positions[name]}].{key}"
            try:
                value = validate_device_values(name, {key: after}, validation_context)[key]
            except RuntimeWriteError as exc:
                issues.append({"code": "operating_value_invalid", "message": f"{path}: {exc}"})
                continue
            holder, value = device_holder(current, name, key, value)
            changes.append({"path": path, "before": before, "after": value, "applies_live": True})
            targets.append((path, "device", holder, key, value))
    return changes, targets, issues


def write_operation_changes(context, config, targets):
    """Write the operating changes of an apply through the runtime service."""

    return write_targets(context, config, targets, create=True)


def _device_name(item):
    return str(item.get("original_name") or item.get("name") or "").strip()


def _config_positions(config):
    devices = config.get("devices") if isinstance(config.get("devices"), list) else []
    return {
        str(device.get("name") or "").strip(): index
        for index, device in enumerate(devices)
        if isinstance(device, dict) and str(device.get("name") or "").strip()
    }
