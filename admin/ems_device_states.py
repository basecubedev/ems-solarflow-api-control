# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whether the running EMS reads each mapped device as saved, by what it read when it started.

EMS reads ``config.json`` only when it starts, and its control snapshot names
the device entries it read (``loaded_devices``, ems/device_fingerprints.py).
This compares them with the file as saved now, for each external MQTT device
by the id it is published as (the id a mapping gives it), so a page says what
is true, in any tab and after a reload, rather than what it last did itself:

* ``running``: EMS read the entry as saved, switched on;
* ``off``: EMS read it as saved, switched off;
* ``waiting``: the saved entry is not one EMS read, a switch on or off
  included, as EMS builds an external MQTT device from the file only when it
  starts and keeps no runtime-state entry for it; it takes effect when EMS
  restarts, and ``change`` says how: ``added``, ``renamed`` or ``changed``;
* ``leaving``: only EMS has it (``change`` ``removed``); it goes on a restart.

Nothing is ``known`` without a readable saved config and a fresh snapshot this
can read (``reason`` ``stopped``; a snapshot counts as fresh as long as
diagnose takes it to be, at least a minute and three loop intervals, so an EMS
just stopped is still taken as running), or with a fresh one that names no
devices this can compare, written by an EMS of another release (``unnamed``).
``started_at`` is when the EMS that wrote the fresh snapshot started, so a
page tells one EMS from the next.
"""

import json

from admin.config_runtime_overlap import resolve_runtime_state_path
from admin.install_context import detect_install_context
from ems.device_fingerprints import FINGERPRINT_VERSION, device_fingerprints
from ems.diagnostics import diagnose_live_snapshot, diagnose_loop_interval
from ems.paths import resolve_control_status_path

EMS_DEVICE_STATES = ("running", "off", "waiting", "leaving")
EMS_DEVICE_CHANGES = ("added", "renamed", "changed", "removed")
EMS_UNKNOWN_REASONS = ("stopped", "unnamed")


def _by_id(fingerprints):
    grouped = {}
    for item in fingerprints if isinstance(fingerprints, list) else []:
        if isinstance(item, dict) and item.get("device_id"):
            grouped.setdefault(str(item["device_id"]), []).append(item)
    return grouped


def _digests(items):
    return sorted(str(item.get("digest")) for item in items)


def _named(items):
    switched_on = [item for item in items if item.get("enabled")]
    return str((switched_on or items)[0].get("name") or "") if items else None


def compare_external_devices(saved_config, loaded):
    """``{device_id: {state, change, name, ran}}`` for each mapping id saved or read.

    An id is in effect as saved when EMS read its entries exactly as they are
    saved, entries sharing it included. ``name`` is the saved name, or the one
    EMS reads for a device that is only there; ``ran`` is the name EMS reads it
    under, ``None`` when it has none; of entries sharing an id, the first
    switched on names it, as EMS reads one switched off not at all.
    """

    saved, running = _by_id(device_fingerprints(saved_config)), _by_id(loaded)
    states = {}
    for device_id in sorted(set(saved) | set(running)):
        mine, theirs = saved.get(device_id, []), running.get(device_id, [])
        ran = _named(theirs)
        name = _named(mine) if mine else ran
        if mine and _digests(mine) == _digests(theirs):
            state, change = ("running" if any(item["enabled"] for item in mine) else "off"), None
        elif mine:
            state, change = "waiting", "added" if not theirs else "renamed" if ran != name else "changed"
        else:
            state, change = "leaving", "removed"
        states[device_id] = {"state": state, "change": change, "name": name, "ran": ran}
    return states


def saved_config(context):
    """The install's ``config.json`` as saved now; ``None`` when it is missing or cannot be read."""

    try:
        config = json.loads(context.config_path.read_text(encoding="utf-8")) if context.config_exists else None
    except (OSError, ValueError):
        return None
    return config if isinstance(config, dict) else None


def control_status_path(context, config):
    """Where an EMS of this install writes its control snapshot, beside the runtime-state file it reads."""

    return resolve_control_status_path(resolve_runtime_state_path(context, config))


def read_snapshot(context, config):
    """What a control snapshot of this install says: ``{fingerprints, reason, started_at}``, fingerprints ``None`` with a reason when it names none this can compare."""

    try:
        runtime_path = resolve_runtime_state_path(context, config)
    except (OSError, TypeError, ValueError):
        return {"fingerprints": None, "reason": "stopped", "started_at": None}
    live = diagnose_live_snapshot(str(runtime_path), diagnose_loop_interval(config, {}))
    if live["status"] != "fresh":
        return {"fingerprints": None, "reason": "stopped", "started_at": None}
    started_at = live["data"].get("started_at")
    record = live["data"].get("loaded_devices")
    if not isinstance(record, dict) or record.get("version") != FINGERPRINT_VERSION or not isinstance(record.get("devices"), list):
        return {"fingerprints": None, "reason": "unnamed", "started_at": started_at}
    return {"fingerprints": record["devices"], "reason": None, "started_at": started_at}


def ems_device_states(context=None):
    """``{known, reason, started_at, devices}`` for this install, ``devices`` as compare_external_devices answers."""

    context = context or detect_install_context()
    config = saved_config(context)
    if config is None:
        return {"known": False, "reason": "stopped", "started_at": None, "devices": {}}
    snapshot = read_snapshot(context, config)
    if snapshot["fingerprints"] is None:
        return {"known": False, "reason": snapshot["reason"], "started_at": snapshot["started_at"], "devices": {}}
    return {"known": True, "reason": None, "started_at": snapshot["started_at"], "devices": compare_external_devices(config, snapshot["fingerprints"])}
