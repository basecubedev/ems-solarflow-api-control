# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What an Admin save hands the running EMS through the runtime state (Tier 2).

docs/developer/settings-operation-store.md: ``config.json`` holds the
installation, ``runtime-state.json`` the operation. An Admin save writes the
operating settings it changed through the runtime service
(admin/operation_settings.py) and, here, what a change of the installation
means for the operation: a device switched on or off takes its control along,
and an operating power above a limit the save lowered is held at that limit.
Both go through the validated writers the dashboard uses, so they take effect
within one EMS control loop.

A device's keys reach the runtime entry the device owns by the rule the EMS
starts with (``ems.runtime_state.owned_runtime_device``): the entry stamped with
its serial, so a rename or a name swap that the running EMS has not loaded yet
still reaches the device it means, and the EMS carries that entry, values
included, over to the new name at its next start.

Best-effort: config.json is already saved when this runs, so any runtime write
problem (a device not yet in runtime, a read-only file) is recorded as a
per-key skip or warning and never fails the apply. A new device has no runtime
entry and is skipped: the EMS seeds its values on its next start.
"""

import os
import re

from admin.config_runtime_overlap import (
    device_group,
    device_runtime_plan,
    device_state,
    read_runtime_state,
    resolve_runtime_state_path,
)
from ems.runtime_commands import (
    DEVICE_FIELDS,
    SECTION_FIELDS,
    SYSTEM_FIELDS,
    RuntimeWriteError,
    apply_configured_device_update,
    apply_section_update,
    apply_system_update,
    build_validation_context,
)
from ems.config import recorded_defaults_revision
from ems.read_only_devices import is_read_only_device_config
from ems.runtime_state import RuntimeState, owned_runtime_device

_DEVICE_PATH = re.compile(r"^devices\[(\d+)\]\.([A-Za-z0-9_]+)$")
_SCALAR_PATH = re.compile(r"^([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)$")

RUNTIME_ABSENT_REASON = "runtime_state_absent"


def _device_name(config, index):
    devices = config.get("devices") if isinstance(config.get("devices"), list) else []
    if index < 0 or index >= len(devices):
        return None
    device = devices[index]
    # A read-only device never gets a runtime-state entry: the EMS neither
    # reads nor honours one for a device it does not control.
    if not isinstance(device, dict) or is_read_only_device_config(device):
        return None
    return str(device.get("name") or "").strip() or None


def _classify(path, config):
    device_match = _DEVICE_PATH.match(path)
    if device_match:
        key = device_match.group(2)
        if key not in DEVICE_FIELDS:
            return None
        name = _device_name(config, int(device_match.group(1)))
        if not name:
            return None
        return ("device", name, key)
    scalar_match = _SCALAR_PATH.match(path)
    if not scalar_match:
        return None
    head, key = scalar_match.group(1), scalar_match.group(2)
    if head == "system" and key in SYSTEM_FIELDS:
        return ("system", None, key)
    if head in SECTION_FIELDS and key in SECTION_FIELDS[head]:
        return ("section", head, key)
    return None


def _devices(config):
    devices = config.get("devices") if isinstance(config, dict) else None
    return devices if isinstance(devices, list) else []


def _named_device(config, name):
    for device in _devices(config):
        if isinstance(device, dict) and str(device.get("name") or "").strip() == name:
            return device
    return None


def _apply_one(runtime_state, validation_context, kind, holder, key, value):
    if kind == "parked":
        name, identity = holder
        if not runtime_state.update_parked(name, identity, {key: value}):
            raise RuntimeWriteError(f"no parked values for device {name}")
        return
    if kind == "system":
        apply_system_update(runtime_state, {key: value}, validation_context, source="admin")
    elif kind == "section":
        apply_section_update(
            runtime_state, holder, {key: value}, validation_context, source="admin"
        )
    else:
        name, identity, refusal = holder
        if refusal is not None:
            raise RuntimeWriteError(refusal)
        apply_configured_device_update(
            runtime_state, name, identity, {key: value}, validation_context, source="admin"
        )


def _runs_as_another(runtime_state, holder):
    """A device switched on under another entry than the running EMS drives it as keeps that entry until the EMS restarts."""

    name, identity, _refusal = holder
    devices = runtime_state.snapshot().get("devices")
    entry = owned_runtime_device(devices, name, identity) if isinstance(devices, dict) else None
    if entry is None or entry == name:
        return []
    return [f"device {name} runs as {entry} in the EMS until it restarts"]


def write_targets(context, config, targets, *, create=False):
    """Write ``targets`` through the runtime service; ``create`` makes a missing runtime state first.

    An operating setting lives only in the runtime state, so a change of one
    creates the file where the EMS has not yet, recording the config's
    defaults revision as the EMS would, so its first start takes no value set
    here for an old default; a mirror of a config value leaves a missing file
    to the EMS, which puts it back from its last good copy or seeds it from
    the config.
    """

    result = {"applied": [], "skipped": [], "warnings": []}
    if not targets:
        return result
    path = resolve_runtime_state_path(context, config)
    if create and not os.path.exists(path):
        try:
            RuntimeState(path, {"devices": {}}, recorded_defaults_revision(config), refresh_defaults=False).load_or_create()
        except OSError as exc:
            for display_path, *_ in targets:
                result["skipped"].append({"path": display_path, "reason": f"runtime_write_failed: {exc}"})
            return result
    if not os.path.exists(path):
        for display_path, *_ in targets:
            result["skipped"].append({"path": display_path, "reason": RUNTIME_ABSENT_REASON})
        result["warnings"].append(
            "runtime-state file not present; the EMS restores it from its last good copy, or seeds these values "
            "from config on its next start"
        )
        return result
    runtime_state = RuntimeState(path, {"devices": {}})
    runtime_state.load_if_changed(force=True)
    validation_context = build_validation_context(config, None)
    for display_path, kind, holder, key, value in targets:
        try:
            _apply_one(runtime_state, validation_context, kind, holder, key, value)
            result["applied"].append(display_path)
            if kind == "device" and key == "enabled" and value is True:
                result["warnings"].extend(_runs_as_another(runtime_state, holder))
        except RuntimeWriteError as exc:
            result["skipped"].append({"path": display_path, "reason": str(exc)})
        except OSError as exc:
            result["skipped"].append(
                {"path": display_path, "reason": f"runtime_write_failed: {exc}"}
            )
    return result


def mirror_changed_keys_to_runtime(context, *, previous, merged):
    """Hand an Admin save to the running EMS through the runtime state.

    ``previous`` is the config the apply replaced and ``merged`` the one it
    wrote. Two things follow from a save (docs/developer/settings-operation-store.md):
    a device switched on or off takes its control along, and an operating
    power above a limit the save lowered is held at that limit. A limit raised
    leaves the operating value where it is: it is changed on the dashboard, up
    to the limit. Device values come from device_runtime_plan, which names the
    runtime entry each device owns, by name and serial, and leaves alone a
    device it cannot tie to one.
    """

    path = resolve_runtime_state_path(context, merged)
    runtime_data = read_runtime_state(path)
    runtime_devices = runtime_data.get("devices") if isinstance(runtime_data.get("devices"), dict) else {}
    targets = []
    for display_path, name, identity, key, value in device_runtime_plan(previous, merged):
        if not _classify(display_path, merged):
            continue
        if key == "max_power":
            entry = owned_runtime_device(runtime_devices, name, identity)
            held = runtime_devices.get(entry) if entry is not None else None
            if not isinstance(held, dict) or not _above(held.get("max_power"), value):
                continue
        elif key != "enabled":
            continue
        targets.append((display_path, "device", (name, identity, None), key, value))
    system = runtime_data.get("system") if isinstance(runtime_data.get("system"), dict) else {}
    limit = build_validation_context(merged, None)["system_max_total_power"]
    if _above(system.get("max_total_power"), limit):
        targets.append(("system.max_total_power", "system", None, "max_total_power", limit))
    return write_targets(context, merged, targets)


def _above(value, limit):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > limit


def device_holder(config, name, key, value):
    """The holder and value a write of ``key`` takes for the config device ``name``.

    Only the entry that speaks for its device (device_state) is reset, and
    ``enabled`` is whether the EMS builds that device, so a telemetry entry
    never switches control back on.
    """

    entry = _named_device(config, name)
    group = device_group(entry)
    state = device_state(config, group) if group is not None else None
    if state is None or state[0] is not entry:
        return (name, None, f"device {name} does not speak for a device with a runtime entry"), value
    identity = group[1] if group[0] == "serial" else None
    return (name, identity, None), state[1] if key == "enabled" else value
