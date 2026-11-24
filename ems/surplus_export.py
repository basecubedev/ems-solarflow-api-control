# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A full device hands its PV to another device's AC charge.

A Zendure device whose battery is full and whose ``gridReverse`` is 1 (the
Zendure app's "Export" switch) passes all its PV to its AC output and ignores
the ``outputLimit`` it is given: the firmware's bypass. With ``gridReverse`` 2
the same device curtails its panels to the limit instead. Measured on a
SolarFlow Pro (Zendure-HA #1667, 2026-09-25): ``outputLimit`` 204 W, output
635 W at 674 W of PV, ``socLimit`` 1. The app switch writes nothing but
``gridReverse`` (SolarFlow 800 Pro 2, 2026-11-24).

The EMS uses the bypass only while another device can take the PV in: it
allows export (``gridReverse`` 1) on at most ``devices`` full devices while
some other device may charge from AC, and refuses it (2) on every device it
manages otherwise -- every device full, AC charging off, nothing to take it.
What the bypass passes beyond the house shows on the meter as surplus, and
the AC charge regulator charges it into the other device.

A device in bypass is no actuator: whatever it is given, it delivers its PV.
It is told its share of the house load, never below ``min_output_limit`` (a
written 0 W can end the bypass): when the bypass ends -- the battery leaves
full, or the export is refused -- it falls to the share the house needs, and
only the charge it fed drops out.

A device is taken for one in bypass when its export is allowed, its battery is
full and it delivers more than its limit; it stays one until either of the
first two ends. ``pass`` and ``reverseState`` are recorded, not relied on: what
they mean is documented nowhere, and ``pass`` 2 was seen on a device at its
``minSoc`` floor whose limit still held (2026-10-05).

Import-side-effect-free.
"""

from dataclasses import dataclass, field

from ems.config import AC_CHARGE_CONTROL_DEFAULTS, safe_float, safe_int
from ems.device_settings import DeviceSetting, SettingValue, model_settings

GRID_REVERSE_PROPERTY = "gridReverse"
EXPORT_ALLOWED = 1
EXPORT_REFUSED = 2
OVER_LIMIT_MARGIN_W = 25
START_GRACE_SECONDS = 120.0
NO_CHARGE_EXPORT_W = 25
KEEP_ALLOWED_POINTS = 2
PV_START_W = 50
TAKER_LOSS_SECONDS = 60.0

GRID_EXPORT = DeviceSetting(
    "grid_export",
    "grid-export",
    "Grid export",
    "Whether a full battery passes all its PV out (the Zendure app's export switch).",
    (
        SettingValue("allow", "Allowed", ((GRID_REVERSE_PROPERTY, EXPORT_ALLOWED),)),
        SettingValue("refuse", "Refused", ((GRID_REVERSE_PROPERTY, EXPORT_REFUSED),)),
    ),
)

SETTING_KEYS = (
    "surplus_export_devices",
    "surplus_export_min_headroom_percent",
    "surplus_export_limit_w",
    "surplus_export_limit_seconds",
    "surplus_export_hold_minutes",
)
DEFAULTS = {key: AC_CHARGE_CONTROL_DEFAULTS[key] for key in SETTING_KEYS}

REASON_OFF = "off"
REASON_HELD = "export_limit_hold"
REASON_NO_TAKER = "no_device_can_charge"
REASON_NO_FULL_DEVICE = "no_full_device"
REASON_NO_CHARGE = "no_charge_followed"
REASON_PASSING = "passing"


@dataclass(frozen=True)
class SurplusExportSettings:
    """The ``surplus_export_*`` keys of ``ac_charge_control``, read."""

    devices: int = DEFAULTS["surplus_export_devices"]
    min_headroom_percent: float = DEFAULTS["surplus_export_min_headroom_percent"]
    export_limit_w: float = DEFAULTS["surplus_export_limit_w"]
    export_limit_seconds: float = DEFAULTS["surplus_export_limit_seconds"]
    hold_seconds: float = DEFAULTS["surplus_export_hold_minutes"] * 60.0
    grace_seconds: float = START_GRACE_SECONDS

    @property
    def managed(self):
        """Whether the EMS sets ``gridReverse`` at all; 0 devices leaves it to the app."""

        return self.devices > 0


def settings_from_config(section):
    """Read the settings from an ``ac_charge_control`` section; a bad value is its default."""

    section = section if isinstance(section, dict) else {}

    def number(key, reader):
        return reader(section.get(key, DEFAULTS[key]), DEFAULTS[key], minimum=0)

    return SurplusExportSettings(
        devices=number("surplus_export_devices", safe_int),
        min_headroom_percent=number("surplus_export_min_headroom_percent", safe_float),
        export_limit_w=number("surplus_export_limit_w", safe_float),
        export_limit_seconds=number("surplus_export_limit_seconds", safe_float),
        hold_seconds=number("surplus_export_hold_minutes", safe_float) * 60.0,
    )


@dataclass(frozen=True)
class DeviceView:
    """What the plan reads of one device this cycle.

    ``managed`` says the EMS may set this device's ``gridReverse``;
    ``chargeable`` is the controller's answer to whether it may take an AC
    charge now, every permission axis included; ``may_output`` whether it may
    supply the house at all, which a bypass would; ``may_take`` whether the
    operator and the feature let it charge at all, the standing part of that
    answer.
    """

    name: str
    online: bool
    managed: bool
    may_output: bool
    may_take: bool
    full: bool
    chargeable: bool
    headroom_percent: float
    solar_w: float
    output_w: float
    output_limit_w: float
    grid_reverse: int | None


@dataclass(frozen=True)
class SurplusExportState:
    """What one cycle hands the next: who may export, who does, the export limit's clock."""

    allowed: tuple = ()
    exporting: tuple = ()
    export_since: float | None = None
    held_until: float | None = None
    waiting_since: float | None = None
    held_reason: str | None = None
    taker_lost_since: float | None = None


@dataclass(frozen=True)
class SurplusExportPlan:
    """One cycle's decision.

    ``grid_export`` maps each managed device to ``"allow"`` or ``"refuse"``;
    it is empty while the feature is off. ``exporting`` names the devices in
    bypass now, managed or not.
    """

    state: SurplusExportState
    grid_export: dict = field(default_factory=dict)
    exporting: frozenset = frozenset()
    reason: str = REASON_OFF


def in_bypass(view, was_in_bypass):
    """Whether ``view`` passes its PV past its limit.

    It enters on evidence -- export allowed, battery full, more delivered than
    the limit -- and stays while export is allowed and the battery full: once
    its limit is its share, a device that delivers no more than the house takes
    is still in bypass, and treating it as an actuator again would hand it a
    claim it does not follow.
    """

    if not view.online or view.grid_reverse != EXPORT_ALLOWED or not view.full:
        return False
    return was_in_bypass or view.output_w > view.output_limit_w + OVER_LIMIT_MARGIN_W


def plan_surplus_export(views, previous, settings, *, grid_power_w, charging, now, beyond_target_w=None):
    """Decide who may export this cycle and who is in bypass.

    A device that takes the PV in is one that may charge from AC and is not
    full; before the first device is allowed it also needs
    ``min_headroom_percent`` of room, so a taker about to fill up does not
    switch the export on for a moment. Devices already allowed keep it while
    they stay full or within ``KEEP_ALLOWED_POINTS`` of their ceiling and have
    any PV, so a SoC or a PV reading flickering at an edge does not switch it
    with every report, and are no taker meanwhile; a device not yet allowed
    needs more than ``PV_START_W`` of PV. Free places go to the full devices
    with the most PV that may supply the house. A taker lost while the export
    is allowed ends it only after ``TAKER_LOSS_SECONDS`` without one when a
    device that may take PV in and has room is still there -- its charge
    eligibility can flicker; a taker gone because AC charging, the device or
    its permission is off, or because it is full, ends it at once.

    A taker that may charge is not one that does: a surplus below the charge
    threshold, or the hourly entry cap, starts no charge. ``charging`` is
    whether the AC charge runs; an export the meter shows, also beyond the grid
    target regulation settles on (``beyond_target_w``, the reading less that
    target; more than ``NO_CHARGE_EXPORT_W`` both ways, or a meter without a
    reading) for
    ``settings.grace_seconds`` without one is refused for the hold, as the export
    limit refuses it. While the house takes all the PV nothing leaves, and the
    export stays allowed. A device without PV passes nothing and is no
    candidate.
    """

    previous = previous or SurplusExportState()
    exporting = frozenset(
        view.name for view in views if in_bypass(view, view.name in previous.exporting)
    )
    if not settings.managed:
        return SurplusExportPlan(
            state=SurplusExportState(exporting=tuple(sorted(exporting))),
            exporting=exporting,
        )

    export_since, held_until = _export_limit_clock(
        previous, settings, exporting, grid_power_w, now
    )
    held_reason = previous.held_reason if held_until is not None else None
    if held_until is not None and held_until != previous.held_until:
        held_reason = REASON_HELD
    waiting_since = None
    beyond = grid_power_w if beyond_target_w is None else beyond_target_w
    exported = grid_power_w is None or (
        grid_power_w < -NO_CHARGE_EXPORT_W and beyond < -NO_CHARGE_EXPORT_W
    )
    if held_until is None and previous.allowed and not charging and exported:
        waiting_since = previous.waiting_since if previous.waiting_since is not None else now
        if now - waiting_since >= settings.grace_seconds:
            held_until, held_reason, waiting_since = now + settings.hold_seconds, REASON_NO_CHARGE, None
    room = 0.0 if previous.allowed else settings.min_headroom_percent
    takers = [
        view
        for view in views
        if view.online
        and view.chargeable
        and not view.full
        and view.name not in previous.allowed
        and view.headroom_percent > 0
        and view.headroom_percent >= room
    ]
    candidates = [
        view
        for view in views
        if view.online
        and view.managed
        and view.may_output
        and view.solar_w > (0 if view.name in previous.allowed else PV_START_W)
        and (
            view.full
            or view.name in previous.allowed
            and view.headroom_percent <= KEEP_ALLOWED_POINTS
        )
    ]

    taker_lost_since = None
    kept = tuple(name for name in previous.allowed if name in {view.name for view in candidates})
    flickering = [
        view
        for view in views
        if view.online
        and view.may_take
        and not view.full
        and view.headroom_percent > 0
        and view.name not in previous.allowed
    ]
    if held_until is None and not takers and kept and flickering:
        taker_lost_since = previous.taker_lost_since if previous.taker_lost_since is not None else now
    if held_until is not None:
        allowed, reason = (), held_reason or REASON_HELD
    elif not takers and taker_lost_since is not None and now - taker_lost_since < TAKER_LOSS_SECONDS:
        allowed, reason = kept, REASON_PASSING
    elif not takers:
        allowed, reason = (), REASON_NO_TAKER
    elif not candidates:
        allowed, reason = (), REASON_NO_FULL_DEVICE
    else:
        allowed, reason = _choose(candidates, previous.allowed, settings.devices), REASON_PASSING

    grid_export = {
        view.name: "allow" if view.name in allowed else "refuse"
        for view in views
        if view.online and view.managed
    }
    return SurplusExportPlan(
        state=SurplusExportState(
            allowed=allowed,
            exporting=tuple(sorted(exporting)),
            export_since=export_since,
            held_until=held_until,
            waiting_since=waiting_since if allowed else None,
            held_reason=held_reason,
            taker_lost_since=taker_lost_since if allowed else None,
        ),
        grid_export=grid_export,
        exporting=exporting,
        reason=reason,
    )


def _choose(candidates, kept, places):
    names = [view.name for view in candidates]
    chosen = [name for name in kept if name in names][:places]
    for view in sorted(candidates, key=lambda view: (-view.solar_w, view.name)):
        if len(chosen) >= places:
            break
        if view.name not in chosen:
            chosen.append(view.name)
    return tuple(chosen)


def _export_limit_clock(previous, settings, exporting, grid_power_w, now):
    """``(export_since, held_until)``: how long the meter has exported past the limit, and the hold it tripped."""

    if previous.held_until is not None and now < previous.held_until:
        return None, previous.held_until
    if settings.export_limit_w <= 0 or not exporting or grid_power_w is None:
        return None, None
    if grid_power_w >= -settings.export_limit_w:
        return None, None
    since = previous.export_since if previous.export_since is not None else now
    if now - since >= settings.export_limit_seconds:
        return None, now + settings.hold_seconds
    return since, None


def house_shares(views, exporting, house_load_w):
    """The limit each device in bypass is told: its part of ``house_load_w``,
    what the house draws beyond the other devices' targets.

    The house load is split by what each delivers, evenly while none delivers
    anything. Not capped at what a device delivers now: told less, a device
    that follows its limit after all -- no PV left, or a firmware that lets the
    battery add to it -- could never be told more again.
    """

    members = [view for view in views if view.name in exporting]
    delivered = sum(max(0.0, view.output_w) for view in members)
    house = max(0.0, house_load_w or 0.0)
    shares = {}
    for view in members:
        weight = max(0.0, view.output_w) / delivered if delivered > 0 else 1.0 / len(members)
        shares[view.name] = int(house * weight)
    return shares


def grid_export_listed(dev):
    """Whether ``dev``'s model is one whose local API takes ``gridReverse``: an identified zenSDK model."""

    return (GRID_EXPORT.key,) if model_settings(dev) else ()


def grid_export_offered(dev, reported):
    """The export setting, for a device whose state the EMS writes and whose report carries it."""

    if not getattr(dev, "supports_state_reconciliation", True):
        return ()
    if not grid_export_listed(dev) or GRID_REVERSE_PROPERTY not in reported:
        return ()
    return (GRID_EXPORT,)


def reported_grid_export(state):
    """The report's ``gridReverse`` as the reconciler reads it; empty when it carries none."""

    if state is None or not getattr(state, "grid_reverse_reported", False):
        return {}
    return {GRID_REVERSE_PROPERTY: int(state.grid_reverse)}
