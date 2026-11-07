# SPDX-License-Identifier: AGPL-3.0-or-later
"""Config -> runtime-state convergence for Admin maintenance (Tier 2).

When Admin applies a maintenance config, the whitelisted overlapping keys it
changed are mirrored into runtime-state through the same validated writers the
Dashboard uses, so the change goes live within one EMS control loop instead of
waiting for a restart. Convergence is one-directional (config/Admin -> runtime);
runtime is never merged back into config.

A device's keys reach the runtime entry the device owns by the rule the EMS
starts with (``ems.runtime_state.owned_runtime_device``): the entry stamped with
its serial, so a rename or a name swap that the running EMS has not loaded yet
still reaches the device it means, and the EMS carries that entry, values
included, over to the new name at its next start.

Best-effort: config.json is already the durable source of truth when this runs,
so any runtime write problem (a value above the runtime power ceiling, a device
not yet in runtime, a read-only file) is recorded as a per-key skip/warning and
never fails the apply. A new device has no runtime entry and is skipped: the EMS
seeds its values from config on its next start.
"""

import os
import re

from admin.config_runtime_overlap import (
    config_effective_value,
    device_group,
    device_runtime_plan,
    device_state,
    resolve_runtime_state_path,
    values_differ,
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


def _changed_scalars(previous, merged):
    """``(path, value)`` for each system and section key the saved config changed."""

    for head, fields in (("system", SYSTEM_FIELDS), *SECTION_FIELDS.items()):
        before = previous.get(head) if isinstance(previous, dict) else None
        after = merged.get(head)
        before = before if isinstance(before, dict) else {}
        after = after if isinstance(after, dict) else {}
        for key in fields:
            value = after.get(key)
            if value is not None and values_differ(before.get(key), value):
                yield f"{head}.{key}", value


def _named_device(config, name):
    for device in _devices(config):
        if isinstance(device, dict) and str(device.get("name") or "").strip() == name:
            return device
    return None


def _apply_one(runtime_state, validation_context, kind, holder, key, value):
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


def _write_targets(context, config, targets):
    result = {"applied": [], "skipped": [], "warnings": []}
    if not targets:
        return result
    path = resolve_runtime_state_path(context, config)
    if not os.path.exists(path):
        for display_path, *_ in targets:
            result["skipped"].append({"path": display_path, "reason": RUNTIME_ABSENT_REASON})
        result["warnings"].append(
            "runtime-state file not present; the EMS will seed these values "
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
    """Mirror the overlapping whitelisted keys the apply changed into runtime-state.

    ``previous`` is the config the apply replaced and ``merged`` the one it
    wrote. Device values come from device_runtime_plan and go to the entry the
    device owns, by name and serial (``apply_configured_device_update``).
    """

    targets = []
    for path, value in _changed_scalars(previous, merged):
        classified = _classify(path, merged)
        if classified:
            kind, holder, key = classified
            targets.append((path, kind, holder, key, value))
    for path, name, identity, key, value in device_runtime_plan(previous, merged):
        if _classify(path, merged):
            targets.append((path, "device", (name, identity, None), key, value))
    return _write_targets(context, merged, targets)


def reset_targets_to_config(context, config, requests):
    """Write installed config values back into runtime-state for the given keys.

    ``requests`` is a list of ``{"scope","key",...}`` targets: ``system``/
    ``section`` (with ``section``) / ``device`` (with ``name``). Reset always
    *writes* the effective config value, never clears the override, because the
    EMS loads config once per process and a cleared key would fall back to a
    stale value.
    """

    targets = []
    for item in requests:
        if not isinstance(item, dict):
            continue
        scope = item.get("scope")
        key = item.get("key")
        if scope == "system":
            kind, holder, display = "system", None, "system." + str(key)
        elif scope == "section":
            holder = item.get("section")
            kind, display = "section", str(holder) + "." + str(key)
        elif scope == "device":
            holder = item.get("name")
            kind, display = "device", "devices." + str(holder) + "." + str(key)
        else:
            continue
        value = config_effective_value(config, kind, holder, key)
        if value is None:
            continue
        if kind == "device":
            holder, value = _device_reset(config, holder, key, value)
        targets.append((display, kind, holder, key, value))
    return _write_targets(context, config, targets)


def _device_reset(config, name, key, value):
    """The holder and value a reset of ``key`` writes for the config device ``name``.

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
