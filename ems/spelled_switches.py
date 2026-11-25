# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Switches a config spells as something other than true or false.

A hand-edited ``"false"`` is read by the EMS differently from how it looks:
a control flag that is not a real boolean is read on its blocking side, a
text ``simulation_mode`` also forces a dry run, and the other switches go
through readers that agree only on the plain words. The Admin console shows
what the file says. So the file says it plainly: each such switch is written
as the boolean the EMS already read from it, and the EMS does after the
rewrite exactly what it did before.

Rewritten are a control flag holding anything but a boolean, and a switch of
``WORD_SWITCH_PATHS``, each of which the EMS reads by its words, holding one
of the words all those readers agree on (true/false, yes/no, on/off, 1/0, any
case) or the number 0 or 1. Anything else stays as written, and so does every
other switch: ``zendure_mqtt.tls`` and ``tls_insecure``, which the EMS
refuses unless they are booleans, and a switch the catalogue gains later
until it is placed here.

Import-side-effect-free.
"""

import copy

from ems.config import CONTROL_FLAG_BLOCKING_SIDE, CONTROL_FLAGS_INVALID_FORCE_DRY_RUN

WORD_SWITCH_PATHS = frozenset({
    "ac_charge_control.enabled",
    "battery_care.enabled",
    "battery_full_charge_assist.enable_ac_charge_mode",
    "battery_full_charge_assist.enabled",
    "config_upgrade.backup_before_apply",
    "dashboard.enabled",
    "dashboard.log_redaction",
    "dashboard.signpost",
    "dashboard.ssl_auto_generate",
    "dashboard.ssl_enabled",
    "energy_savings.enabled",
    "influxdb.auto_init",
    "influxdb.auto_sync",
    "influxdb.enabled",
    "system.battery_kwh_weighting",
    "system.output_control.device_ramp_enabled",
    "system.output_control.filter_enabled",
    "system.output_control.ramp_enabled",
    "system.output_control.sign_change_fast_response_enabled",
    "system.pv_charge_balance_enabled",
    "system.pv_kwp_weighting",
    "system.redistribute_clamped_power",
    "winter.enabled",
})
STRICT_SWITCH_PATHS = frozenset({"zendure_mqtt.tls", "zendure_mqtt.tls_insecure"})
AGREED_WORDS = {
    "true": True, "yes": True, "on": True, "1": True,
    "false": False, "no": False, "off": False, "0": False,
}


def switch_paths():
    """Every on/off setting of the catalogue outside the device and grid-meter entries.

    Each is a control flag, one of ``WORD_SWITCH_PATHS`` or one of
    ``STRICT_SWITCH_PATHS``; a test holds the catalogue to that.
    """

    from ems.config_catalog import get_config_feature_field_index

    return frozenset(
        path
        for path, field in get_config_feature_field_index().items()
        if field.get("type") == "boolean"
        and "[]" not in path
        and not path.startswith(("devices", "grid_meter"))
    )


def normalize_spelled_switches(config):
    """``config`` with its spelled switches as booleans, and the changes made.

    Each change is ``{"path", "from", "to"}``; a text ``simulation_mode`` adds
    the ``system.dry_run`` it forced. The input is not changed.
    """

    normalized = copy.deepcopy(config)
    changes = []
    for path in sorted(WORD_SWITCH_PATHS | {f"system.{name}" for name in CONTROL_FLAG_BLOCKING_SIDE}):
        section, _, key = path.rpartition(".")
        block = _block(normalized, section)
        if block is None or key not in block or isinstance(block[key], bool):
            continue
        read = _read(path, block[key])
        if read is None:
            continue
        changes.append({"path": path, "from": block[key], "to": read})
        block[key] = read
        if section == "system" and key in CONTROL_FLAGS_INVALID_FORCE_DRY_RUN and block.get("dry_run") is not True:
            changes.append({"path": "system.dry_run", "from": block.get("dry_run"), "to": True})
            block["dry_run"] = True
    return normalized, changes


def _block(config, section):
    cursor = config
    for part in section.split("."):
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(part)
    return cursor if isinstance(cursor, dict) else None


def _read(path, value):
    section, _, key = path.rpartition(".")
    if section == "system" and key in CONTROL_FLAG_BLOCKING_SIDE:
        return CONTROL_FLAG_BLOCKING_SIDE[key]
    if path not in WORD_SWITCH_PATHS:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        return AGREED_WORDS.get(value.strip().lower())
    return None
