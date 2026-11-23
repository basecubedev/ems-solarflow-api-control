# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Defaults a release shipped and a later release replaced.

A value in ``config.json`` or ``runtime-state.json`` that still equals one of
them was most likely never changed by hand: the file got it from the template,
or from the defaults, of its day. A refresh moves such a value to today's
default once, and the file then carries ``DEFAULTS_REVISION``, so a value set
afterwards stays as it is, an old default among them. A value someone set to
exactly an old default before the refresh cannot be told apart and moves too.

Only tuning values are listed. A switch, a write gate, a path or an identity is
never refreshed, and neither is ``system.min_output_limit`` 0, which switches
the standby floor off and is as likely chosen. Two switches are moved, the
owner's call (2026-11-23), each with its reason in ``switch_reason`` and only
while its own settings show it was left as shipped (``shipped_with``): the
per-device ramp is switched off, and a load filter that main's template had
switched off for one day is switched on again. The shipped filter passes every
reading unchanged (a window of 1, an EMA of 1.0) rather than being off, because
a release before 2026-11-23 reads a filter switched off as no surplus charge,
so a rollback keeps charging.
Each entry was the template's value in the releases it names. Two are the
owner's call (2026-11-13): battery
care's first points, which main carried that day only, its PV-only 0 among
them, and the 1200 W installation charge limit. That one is cleared to null
rather than set to today's 0: both mean no limit now, but a release before
2026-11-13 reads 0 as no charging at all and null as its own 1200 W, so a
rollback keeps charging.

Import-side-effect-free.
"""

import copy
from dataclasses import dataclass

DEFAULTS_REVISION = 4
CONFIG_REVISION_KEY = "config_defaults_revision"
RUNTIME_REVISION_KEY = "defaults_revision"


TODAYS_DEFAULT = object()


@dataclass(frozen=True)
class RetiredDefault:
    """A dotted path, the defaults it had, the releases that shipped them.

    ``replacement`` is what the value becomes; by default today's default.
    ``switch_reason`` says why a switch is moved at all: an old setting that was
    the template's rather than a choice. ``shipped_with`` names settings that
    tell a value left as shipped from a tuned one: the entry moves a value only
    while each of them is absent or at one of the values listed for it. They are
    read as the earlier revisions' entries left them and before the entries of
    the entry's own revision move them: an older default of theirs has already
    become the one this revision starts from. ``shipped_from_revision`` names
    the first revision whose template carried the value; a file recording an
    earlier one got it by hand.
    """

    path: str
    values: tuple
    shipped: str
    revision: int = 1
    replacement: object = TODAYS_DEFAULT
    switch_reason: str | None = None
    shipped_with: tuple = ()
    shipped_from_revision: int = 0


RETIRED_DEFAULTS = (
    RetiredDefault("system.loop_interval", (3,), "v0.4.2-v0.7.0"),
    RetiredDefault("system.deadband", (10,), "v0.1.0-v0.7.0"),
    RetiredDefault("system.output_control.target_deadband_w", (10,), "v0.4.0-v0.7.0"),
    RetiredDefault("system.output_control.median_window", (3,), "v0.4.0-v0.4.2", replacement=2),
    RetiredDefault("system.output_control.ema_alpha", (0.65,), "v0.4.0", replacement=0.85),
    RetiredDefault("system.output_control.ramp_up_w_per_cycle", (300,), "v0.4.0-v0.4.2"),
    RetiredDefault("system.output_control.ramp_down_w_per_cycle", (500,), "v0.4.0-v0.7.0"),
    RetiredDefault("system.output_control.device_ramp_up_w_per_cycle", (250,), "v0.4.0-v0.4.2"),
    RetiredDefault("system.output_control.device_ramp_down_w_per_cycle", (400,), "v0.4.0-v0.7.0"),
    RetiredDefault("system.output_control.large_export_bypass_w", (500,), "v0.4.0-v0.4.2"),
    RetiredDefault("winter.months", ([10, 11, 12, 1, 2],), "v0.4.0-v0.4.1"),
    RetiredDefault("winter.ramp_step_percent", (5,), "v0.4.0-v0.8.12"),
    RetiredDefault("energy_savings.max_sample_delta_seconds", (60,), "v0.5.2-v0.5.3"),
    RetiredDefault(
        "ac_charge_control.max_total_charge_power_w", (1200,), "v0.8.13-v0.10.0", replacement=None
    ),
    RetiredDefault("battery_care.summer_full_points", (5,), "main 2026-11-13"),
    RetiredDefault("battery_care.winter_full_points", (10,), "main 2026-11-13"),
    RetiredDefault("battery_care.pv_only_points", (0,), "main 2026-11-13"),
    RetiredDefault(
        "system.output_control.median_window", (2,), "v0.5.0-v0.10.7", revision=2,
        shipped_with=(("system.output_control.ema_alpha", (0.85,)),),
    ),
    RetiredDefault(
        "system.output_control.ema_alpha", (0.85,), "v0.4.1-v0.10.7", revision=2,
        shipped_with=(("system.output_control.median_window", (2,)),),
    ),
    RetiredDefault(
        "system.output_control.device_ramp_enabled", (True,), "v0.4.0-v0.10.7", revision=3,
        switch_reason=(
            "The template ramped every device below the total's ramp; measured on a SolarFlow "
            "800 Pro 2 it held a single device back while the total counted it (issue #341)."
        ),
        shipped_with=(
            ("system.output_control.ramp_enabled", (True,)),
            ("system.output_control.device_ramp_up_w_per_cycle", (400,)),
            ("system.output_control.device_ramp_down_w_per_cycle", (200,)),
        ),
    ),
    RetiredDefault(
        "system.output_control.filter_enabled", (False,), "main 2026-11-23", revision=4,
        shipped_from_revision=2,
        switch_reason=(
            "Main's template switched the load filter off (issue #341); a release before that reads "
            "a filter switched off as no surplus charge, so the shipped filter is on again and "
            "passes every reading unchanged."
        ),
        shipped_with=(
            ("system.output_control.median_window", (2,)),
            ("system.output_control.ema_alpha", (0.85,)),
        ),
    ),
    RetiredDefault(
        "system.output_control.median_window", (2,), "main 2026-11-23", revision=4, shipped_from_revision=2,
        shipped_with=(
            ("system.output_control.filter_enabled", (False,)),
            ("system.output_control.ema_alpha", (0.85,)),
        ),
    ),
    RetiredDefault(
        "system.output_control.ema_alpha", (0.85,), "main 2026-11-23", revision=4, shipped_from_revision=2,
        shipped_with=(
            ("system.output_control.filter_enabled", (False,)),
            ("system.output_control.median_window", (2,)),
        ),
    ),
)


def stored_revision(data, key):
    """The defaults revision a file records, 0 when it records none."""

    value = data.get(key) if isinstance(data, dict) else None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def same_value(stored, retired):
    """Whether a stored value is a retired default: equal and of the same kind.

    A number equals a number of either type, so 1200.0 is 1200; a boolean never
    equals a number.
    """

    if isinstance(stored, bool) or isinstance(retired, bool):
        return type(stored) is type(retired) and stored == retired
    if isinstance(stored, (int, float)) and isinstance(retired, (int, float)):
        return stored == retired
    return type(stored) is type(retired) and stored == retired


def _lookup(data, keys):
    node = data
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return False, None
        node = node[key]
    return True, node


def _left_as_shipped(data, companions):
    for path, values in companions:
        found, stored = _lookup(data, path.split("."))
        if found and not any(same_value(stored, value) for value in values):
            return False
    return True


def refresh_retired_defaults(data, current, after_revision, up_to_revision=DEFAULTS_REVISION):
    """``data`` with every value still at a retired default moved to ``current``'s.

    Only entries retired after ``after_revision`` and up to ``up_to_revision``
    count, in the order of their revisions. A path ``data`` lacks, one
    ``current`` has no value for, and one whose current value is the same are
    left alone. Returns a deep copy and the changes as ``{"path", "from",
    "to"}``, one per path: a value two revisions move goes from where it stood
    to where the last one leaves it.
    """

    refreshed = copy.deepcopy(data)
    changes = {}
    baseline, baseline_revision = refreshed, None
    for entry in RETIRED_DEFAULTS:
        if entry.revision != baseline_revision:
            baseline, baseline_revision = copy.deepcopy(refreshed), entry.revision
        if not after_revision < entry.revision <= up_to_revision:
            continue
        if after_revision < entry.shipped_from_revision:
            continue
        keys = entry.path.split(".")
        found, stored = _lookup(refreshed, keys)
        has_default, default = _lookup(current, keys)
        if not found or not has_default:
            continue
        if not any(same_value(stored, retired) for retired in entry.values):
            continue
        if not _left_as_shipped(baseline, entry.shipped_with):
            continue
        new = default if entry.replacement is TODAYS_DEFAULT else entry.replacement
        if same_value(stored, new) or same_value(stored, default):
            continue
        _lookup(refreshed, keys[:-1])[1][keys[-1]] = copy.deepcopy(new)
        first = changes.get(entry.path, {"from": stored})["from"]
        changes[entry.path] = {"path": entry.path, "from": first, "to": copy.deepcopy(new)}
    return refreshed, list(changes.values())
