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
import os

from admin.config_runtime_overlap import (
    compute_overlap_provenance,
    device_group,
    device_state,
    overlap_provenance_for_context,
    parked_for,
    parked_identity,
    read_runtime_state,
    resolve_runtime_state_path,
    runtime_entry_for,
    values_differ,
)
from admin.runtime_convergence import device_holder, write_targets
from ems.read_only_devices import is_read_only_device_config
from ems.config import apply_runtime_config_defaults, template_placeholder_paths
from ems.runtime_state import PARKED_DEVICES_KEY, PLACEHOLDER_SEEDED_KEY, owned_runtime_device, parked_key, runtime_reset_pending
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

    return operating_from(operating_provenance(context, config))


def operating_provenance(context, config):
    """The overlap provenance the page shows: the config's values alone while a fresh setup's reset is pending.

    That reset sets the present runtime state aside at the next EMS start, so
    the values it holds are not the ones the EMS will run.
    """

    if held_by_config(context, config):
        return compute_overlap_provenance(config, {})
    return overlap_provenance_for_context(context, config)


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


def operating_powers(provenance, limits=None, unbuilt=frozenset()):
    """The operating values below the hard limits, as the EMS takes them.

    ``{"system.max_total_power": W, "devices": {name: {"max_power": W, "control": bool}}}``:
    the total and each device's power run in the runtime state, below the
    limits ``config.json`` holds, which the Admin edits (Safety tab, Devices);
    ``control`` is a device's control switch, a setting apart from its
    activation in ``config.json``. Shown beside the limit and the activation,
    changed on the dashboard. ``limits``, the validation context of the
    saved config, holds each power at its limit, as the EMS runs it. A device
    the running EMS does not build (``unbuilt``) has no control switch to name.
    """

    limits = limits if isinstance(limits, dict) else {}
    device_limits = limits.get("device_max_power") if isinstance(limits.get("device_max_power"), dict) else {}
    total = provenance.get("system.max_total_power")
    powers = {
        "system.max_total_power": _held(total.get("effective_value") if isinstance(total, dict) else None, limits.get("system_max_total_power")),
    }
    devices = {}
    for name, entry in (provenance.get("devices") or {}).items():
        if not isinstance(entry, dict):
            continue
        power = entry.get("max_power") if isinstance(entry.get("max_power"), dict) else {}
        control = entry.get("enabled") if isinstance(entry.get("enabled"), dict) else {}
        devices[name] = {
            "max_power": _held(power.get("effective_value"), device_limits.get(name)),
            "control": None if name in unbuilt else control.get("effective_value"),
        }
    powers["devices"] = devices
    return powers


OPERATING_BASE_KEY = "operating_base"


def adopt_operating_values(draft, features, devices):
    """Put the operating values into an editable draft in place of the config's.

    The draft also carries them as they were loaded (``operating_base``, on the
    draft for the settings and on each device for its values), so a save
    writes only what this page changed: a value the dashboard, the scheduler
    or ``emsctl`` set after the page loaded is not put back.
    """

    draft.setdefault("features", {}).update(copy.deepcopy(features))
    draft[OPERATING_BASE_KEY] = copy.deepcopy(features)
    for item in draft.get("devices") or []:
        if not isinstance(item, dict):
            continue
        values = devices.get(_device_name(item))
        if values:
            item.update(copy.deepcopy(values))
            item[OPERATING_BASE_KEY] = copy.deepcopy(values)
    return draft


def held_by_config(context, config):
    """Whether every operating value a save changes goes into ``config.json``.

    So while the next EMS start builds the runtime state from the config: a
    fresh setup's reset request still names the installed config, the runtime
    state was first written by a start whose config still held template
    placeholders (``seeded_from_placeholders``), or there is none yet and the
    config still holds them. A live start after it moves the values again.
    """

    try:
        path = resolve_runtime_state_path(context, config)
        if runtime_reset_pending(path, str(context.config_path)):
            return True
        if not os.path.exists(path):
            return bool(template_placeholder_paths(apply_runtime_config_defaults(config)))
        return bool(read_runtime_state(path).get(PLACEHOLDER_SEEDED_KEY))
    except (OSError, AttributeError, TypeError, ValueError):
        return False


def config_held_device_names(context, config):
    """The config devices whose operating values stay in their config entry.

    A device the running EMS keeps no runtime entry for, and no parked values
    (a new device, one that never ran), has nowhere else to keep them: its
    config entry holds them until a live start with the device built moves
    them (ems.config.move_startup_operating_values). While a fresh setup's
    reset is pending, every device's do.
    """

    return unbuilt_devices(context, config)[0]


def unbuilt_devices(context, config):
    """``(held in config, parked)``: the names of the config devices the running EMS keeps no runtime entry for.

    A device that ran keeps its operating values parked in the runtime state
    (``parked_devices``) and gets them back when it is built again, so the
    Admin shows and changes them there; any other keeps them in its entry.
    """

    devices = config.get("devices") if isinstance(config.get("devices"), list) else []
    named = [
        (str(entry.get("name") or "").strip(), entry)
        for entry in devices
        if isinstance(entry, dict) and not is_read_only_device_config(entry) and str(entry.get("name") or "").strip()
    ]
    if held_by_config(context, config):
        return frozenset(name for name, _entry in named), frozenset()
    try:
        runtime_data = read_runtime_state(resolve_runtime_state_path(context, config))
    except (OSError, AttributeError, TypeError, ValueError):
        runtime_data = {}
    runtime_devices = runtime_data.get("devices") if isinstance(runtime_data.get("devices"), dict) else {}
    unbuilt = [(name, entry) for name, entry in named if runtime_entry_for(entry, config, runtime_devices) is None]
    parked = frozenset(name for name, entry in unbuilt if parked_for(entry, runtime_data))
    return frozenset(name for name, _entry in unbuilt) - parked, parked


def operating_feature_edits(draft):
    """The operating settings a draft changes from the values its page loaded, for a save that writes them into the config."""

    features = draft.get("features") if isinstance(draft.get("features"), dict) else {}
    base = draft.get(OPERATING_BASE_KEY)
    return {
        path: value
        for path, value in features.items()
        if path in OPERATION_SETTING_PATHS and isinstance(base, dict) and _edited(base, path, value)
    }


def _edited(base, key, value):
    """Whether this page changed ``key`` from the value it loaded; a draft that carries none changed nothing.

    A page that loaded a value names it in ``base``; a value it does not name
    there (a key the page added) counts as changed.
    """

    return isinstance(base, dict) and (key not in base or values_differ(base[key], value))


def operation_changes(context, current, draft, *, merged=None, resolved=None):
    """What a draft changes of the operating settings: ``(changes, targets, issues)``.

    ``changes`` are diff rows that take effect at once, ``targets`` what
    ``write_operation_changes`` writes, ``issues`` the values the runtime
    service refuses, as validation errors. Only a value this page changed
    from the one it loaded counts (an existing device the page sent without
    the values it loaded changes none), against the value the EMS runs now, and
    within the limits of ``merged``, the config the apply writes. A device is
    the config entry the merge took it for (``resolved``, by draft position),
    so a name the browser saw masked still finds it and a new device under a
    removed one's name finds none, while one the page added that the merge
    matched to an existing device by its serial brings all of its values;
    one whose values its config entry holds (config_held_device_names) is
    left to the merge, and one that keeps them parked is changed there, for
    when it runs again.
    """

    features, devices = operating_values(context, current)
    held_in_config, parked = unbuilt_devices(context, current)
    everything_in_config = held_by_config(context, current)
    if everything_in_config:
        draft = {**draft, "features": {}}
    validation_context = build_validation_context(merged if isinstance(merged, dict) else current, None)
    changes, targets, issues = [], [], []
    drafted = draft.get("features") if isinstance(draft.get("features"), dict) else {}
    base = draft.get(OPERATING_BASE_KEY)
    for path in sorted(OPERATION_SETTING_PATHS & set(drafted)):
        before, after = features.get(path), drafted[path]
        if not _edited(base, path, after) or not values_differ(before, after):
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
    config_devices = current.get("devices") if isinstance(current.get("devices"), list) else []
    names = resolved.get("names", {}) if isinstance(resolved, dict) else None
    placed = resolved.get("placed", {}) if isinstance(resolved, dict) else {}
    runtime_data = _runtime_data(context, current)
    for position, item in enumerate(draft.get("devices") or []):
        if not isinstance(item, dict) or item.get("removed") is True:
            continue
        name = names.get(position) if names is not None else _device_name(item)
        if names is not None and not name:
            if not everything_in_config and _speaks_for_its_serial(merged, placed.get(position)):
                _returning_device(item, merged["devices"][placed[position]], placed[position], runtime_data, validation_context, changes, targets, issues)
            continue
        if item.get("original_name") and not isinstance(item.get(OPERATING_BASE_KEY), dict):
            continue
        known = devices.get(name)
        if not name or known is None or name not in positions or name in held_in_config:
            continue
        added_here = not item.get("original_name")
        for key in sorted(OPERATION_DEVICE_KEYS & set(item)):
            before, after = known.get(key), item[key]
            if not (added_here or _edited(item.get(OPERATING_BASE_KEY), key, after)) or not values_differ(before, after):
                continue
            path = f"devices[{placed.get(position, positions[name])}].{key}"
            try:
                value = validate_device_values(name, {key: after}, validation_context)[key]
            except RuntimeWriteError as exc:
                issues.append({"code": "operating_value_invalid", "message": f"{path}: {exc}"})
                continue
            if name in parked:
                changes.append({"path": path, "before": before, "after": value, "applies_live": False})
                targets.append((path, "parked", (name, parked_identity(config_devices[positions[name]])), key, value))
                continue
            holder, value = device_holder(current, name, key, value)
            changes.append({"path": path, "before": before, "after": value, "applies_live": True})
            targets.append((path, "device", holder, key, value))
    return changes, targets, issues


def _returning_device(item, device, index, runtime_data, validation_context, changes, targets, issues):
    """A device the draft adds whose serial the runtime state still holds values for.

    Removed and added back without an EMS start in between, its serial still
    owns a runtime entry, which the next start keeps; parked, the next start
    restores those values. Either way they would win over what was typed for
    it, so the typed values go there too. The device is the entry the merge
    wrote for ``item`` (``device``), so its name and serial are the ones the
    next start reads, whatever the browser saw masked.
    """

    group = device_group(device)
    if group is None or group[0] != "serial" or index is None:
        return
    parked = runtime_data.get(PARKED_DEVICES_KEY)
    name, identity = str(device.get("name") or "").strip(), group[1]
    runtime_devices = runtime_data.get("devices") if isinstance(runtime_data.get("devices"), dict) else {}
    entry = owned_runtime_device(runtime_devices, name, identity)
    if entry is not None and (runtime_devices.get(entry) or {}).get("identity") != identity:
        entry = None
    held = runtime_devices.get(entry) if entry is not None else (parked or {}).get(parked_key(name, identity))
    if not isinstance(held, dict) or not held:
        return
    for key in sorted(OPERATION_DEVICE_KEYS & set(item)):
        before, after = held.get(key), item[key]
        if not values_differ(before, after):
            continue
        path = f"devices[{index}].{key}"
        try:
            value = validate_device_values(name, {key: after}, validation_context)[key]
        except RuntimeWriteError as exc:
            issues.append({"code": "operating_value_invalid", "message": f"{path}: {exc}"})
            continue
        if entry is not None:
            changes.append({"path": path, "before": before, "after": value, "applies_live": True})
            targets.append((path, "device", (name, identity, None), key, value))
        else:
            changes.append({"path": path, "before": before, "after": value, "applies_live": False})
            targets.append((path, "parked", (name, identity), key, value))


def _speaks_for_its_serial(merged, index):
    """Whether the device the merge placed at ``index`` is the one the next EMS start builds for its serial.

    A switched-off copy of a running device's entry is not: its values never
    reach the runtime entry of the device it copies.
    """

    devices = merged.get("devices") if isinstance(merged, dict) and isinstance(merged.get("devices"), list) else []
    if index is None or not 0 <= index < len(devices) or not isinstance(devices[index], dict):
        return False
    group = device_group(devices[index])
    state = device_state(merged, group) if group is not None else None
    return state is not None and state[0] is devices[index]


def _runtime_data(context, config):
    try:
        return read_runtime_state(resolve_runtime_state_path(context, config))
    except (OSError, AttributeError, TypeError, ValueError):
        return {}


def write_operation_changes(context, config, targets):
    """Write the operating changes of an apply through the runtime service."""

    return write_targets(context, config, targets, create=True)


def _held(value, limit):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or isinstance(limit, bool) or not isinstance(limit, (int, float)):
        return value
    return min(value, limit)


def _device_name(item):
    return str(item.get("original_name") or item.get("name") or "").strip()


def _config_positions(config):
    devices = config.get("devices") if isinstance(config.get("devices"), list) else []
    return {
        str(device.get("name") or "").strip(): index
        for index, device in enumerate(devices)
        if isinstance(device, dict) and str(device.get("name") or "").strip()
    }
