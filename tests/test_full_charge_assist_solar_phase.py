# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The full-charge assist takes the day's PV into the battery before it charges from AC.

Owner 2026-11-15: feeding the PV out and charging the battery back from the
grid at ``force_time`` loses the conversion both ways. The assist now starts
at midnight of its due day, or early in its window, and until ``force_time``
on the day it started the device feeds out only its PV above the charge
ceiling it reports; it charges from AC only from then on, if the battery is
not full yet.
Without its AC mode it charges from PV alone for its whole run. The device
stays in output control meanwhile, held by a ceiling, not by a claim.
"""

import logging
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ems import config as cfg
from ems.controller import FULL_CHARGE_ASSIST_REASON, EMSController
from ems.diagnostics import diagnose_battery_full_charge_status
from ems.state_store import (
    BatteryFullChargeStateStore,
    describe_full_charge_assist_status,
    full_charge_assist_ac_from,
)
from ems.target_control import calculate_targets, detect_capabilities
from test_ac_charge_regulator import following_http_device
from test_battery_full_charge_assist import state
from test_full_charge_assist_behind_a_shut_gate import _state_writes
from test_shutdown_ends_the_full_charge_assist import POWER, Assist
from test_write_gates import ShellyStub

pytestmark = [
    pytest.mark.unit,
    pytest.mark.simulation,
    pytest.mark.power_control,
]

HOUSE = 400
DUE = datetime(2026, 11, 20, 9, 0).astimezone()
EVENING_BEFORE = DUE.replace(day=19, hour=22, minute=0)
MIDNIGHT = DUE.replace(hour=0, minute=0)
MORNING = DUE.replace(hour=10, minute=0)
FORCE = DUE.replace(hour=14, minute=0)


def due_on(path, due=DUE):
    """A battery last full 28 days before ``due``, the feature on since then."""

    store = BatteryFullChargeStateStore(str(path))
    last = due - timedelta(days=28)
    store.set_full_charge_feature_enabled_state(True, last)
    store.update_device_state(
        "WR1",
        last,
        has_battery=True,
        last_full_charge_at=last.isoformat(),
        next_due_at=due.isoformat(),
    )
    return store


class Day(Assist):
    """The real loop on a wall clock the test sets, with ``force_time`` at 14:00."""

    at = EVENING_BEFORE
    force_time = "14:00"
    min_output_limit = 0

    def __init__(self, tmp_path, **options):
        options.setdefault("store", due_on(tmp_path / "ems_state.sqlite"))
        options.setdefault(
            "item", state(soc=50, max_soc=90, ac_mode=2, input_limit_w=0)
        )
        super().__init__(tmp_path, **options)
        self.controller.shelly = ShellyStub(HOUSE)

    @contextmanager
    def loop(self, ac_mode=True, **gate_options):
        rig = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(rig.at.timestamp(), tz)

        with (
            super().loop(ac_mode=ac_mode, **gate_options),
            patch("ems.controller.datetime", Clock),
            patch.object(cfg, "MIN_OUTPUT_LIMIT", self.min_output_limit),
            patch.object(
                cfg,
                "BATTERY_FULL_CHARGE_ASSIST_CONFIG",
                {
                    **cfg.BATTERY_FULL_CHARGE_ASSIST_CONFIG,
                    "force_time": self.force_time,
                },
            ),
        ):
            yield

    def explained(self):
        return self.controller.last_control_explanation.devices["WR1"]

    def record(self):
        return self.store.get_device_state("WR1")


def acmode_writes(writes):
    return [written for written in writes if "acMode" in written]


def test_the_due_day_takes_the_pv_into_the_battery_until_force_time(tmp_path):
    rig = Day(tmp_path)
    rig.run(cycles=12)
    assert rig.item.output_limit > 0, "the device never fed the house"
    assert rig.record()["full_charge_assist_active"] is False

    rig.at = MIDNIGHT
    before = len(rig.hardware.writes)
    rig.run(cycles=12)

    written = rig.written_since(before)
    assert rig.record()["full_charge_assist_active"] is True
    assert {"socSet": 1000} in written
    assert acmode_writes(written) == []
    assert rig.item.output_limit == 0
    assert rig.explained().limiting_reason == "full_charge_assist_solar"
    limits = {limit.name: limit for limit in rig.controller.last_control_explanation.limits}
    assert limits["full_charge_assist_solar"].active is True
    assert limits["full_charge_assist_solar"].value == "[0]"
    assert "battery_care" not in limits or limits["battery_care"].active is False
    assert rig.controller.device_output_control_allowed(rig.dev) is True
    assert rig.controller.runtime_intents["WR1"].reason != FULL_CHARGE_ASSIST_REASON
    with rig.loop():
        assert rig.controller.full_charge_assist_takes_ac_input() is False

    rig.at = FORCE - timedelta(minutes=1)
    before = len(rig.hardware.writes)
    rig.run(cycles=3)
    assert acmode_writes(rig.written_since(before)) == []
    assert rig.item.output_limit == 0

    rig.at = FORCE
    rig.run(cycles=2)

    assert rig.item.ac_mode == 1
    assert rig.item.input_limit_w == POWER
    assert rig.controller.runtime_intents["WR1"].reason == FULL_CHARGE_ASSIST_REASON
    with rig.loop():
        assert rig.controller.full_charge_assist_takes_ac_input() is True


@pytest.mark.parametrize(
    "pv,ceiling,fed",
    [
        (1300, 1000, 300),
        (500, 1000, 0),
        (300, 0, 300),
        (1300, None, 0),
    ],
    ids=["PV above the ceiling", "PV below it", "charge refused", "no ceiling reported"],
)
def test_the_device_feeds_out_only_the_pv_its_battery_cannot_take(
    tmp_path, pv, ceiling, fed
):
    rig = Day(tmp_path)
    rig.item.solar = pv
    rig.item.charge_max_limit_w = ceiling
    rig.at = MORNING

    rig.run(cycles=12)

    assert rig.record()["full_charge_assist_active"] is True
    assert rig.item.output_limit == fed
    assert rig.explained().limiting_reason == "full_charge_assist_solar"


def test_a_ceiling_that_does_not_bind_names_no_assist(tmp_path):
    rig = Day(tmp_path)
    rig.item.solar = 1300
    rig.item.charge_max_limit_w = 1000
    rig.controller.shelly = ShellyStub(-200)
    rig.at = MORNING

    rig.run(cycles=12)

    assert rig.record()["full_charge_assist_active"] is True
    assert rig.item.output_limit < 300
    assert rig.explained().limiting_reason != "full_charge_assist_solar"


def test_the_cycle_that_crosses_force_time_reads_one_clock(tmp_path):
    """Its claim and its ceiling agree: no cycle hands the house load to the battery."""

    rig = Day(tmp_path)
    rig.item.solar = 300
    rig.at = FORCE - timedelta(seconds=1)
    rig.run(cycles=12)
    assert rig.item.output_limit == 0
    original = EMSController.full_charge_assist_caps

    def crossing(self, states, now=None):
        rig.at = FORCE + timedelta(seconds=1)
        return original(self, states, now)

    before = len(rig.hardware.writes)
    with patch.object(EMSController, "full_charge_assist_caps", crossing):
        rig.run(cycles=1)

    assert all(
        written.get("outputLimit", 0) == 0 for written in rig.written_since(before)
    )
    assert rig.item.output_limit == 0
    rig.run(cycles=1)
    assert rig.item.ac_mode == 1
    assert rig.item.output_limit == 0


@pytest.mark.parametrize("soc", [50, 15], ids=["above minSoc", "at minSoc"])
def test_the_standby_floor_takes_no_pv_from_the_battery(tmp_path, soc):
    """Nor does the integrator wind up against the held device, at its minSoc either."""

    rig = Day(tmp_path, item=state(soc=soc, min_soc=15, max_soc=90, ac_mode=2))
    rig.min_output_limit = 35
    rig.at = MORNING

    rig.run(cycles=12)

    assert rig.record()["full_charge_assist_active"] is True
    assert rig.item.output_limit == 0
    assert rig.controller.commanded_total_w == 0


def test_an_early_start_takes_pv_until_force_time_and_ac_after(tmp_path):
    store = due_on(tmp_path / "ems_state.sqlite", due=DUE + timedelta(days=3))
    rig = Day(tmp_path, store=store, item=state(soc=85, max_soc=90, ac_mode=2))
    rig.at = MORNING

    rig.run(cycles=12)

    assert rig.record()["full_charge_assist_active"] is True
    assert acmode_writes(rig.hardware.writes) == []
    assert rig.item.output_limit == 0

    rig.at = FORCE
    rig.run(cycles=2)

    assert rig.item.ac_mode == 1


def test_an_assist_that_starts_after_force_time_charges_from_ac_at_once(tmp_path):
    store = due_on(tmp_path / "ems_state.sqlite", due=DUE + timedelta(days=3))
    rig = Day(tmp_path, store=store, item=state(soc=85, max_soc=90, ac_mode=2))
    rig.at = FORCE + timedelta(hours=1)

    rig.run(cycles=2)

    assert rig.record()["full_charge_assist_active"] is True
    assert rig.item.ac_mode == 1
    assert rig.item.input_limit_w == POWER


def test_without_its_ac_mode_the_assist_takes_pv_alone_until_the_battery_is_full(
    tmp_path,
):
    rig = Day(tmp_path)
    rig.at = FORCE + timedelta(hours=2)

    rig.run(cycles=12, ac_mode=False)

    assert rig.record()["full_charge_assist_active"] is True
    assert acmode_writes(rig.hardware.writes) == []
    assert rig.item.output_limit == 0
    assert rig.explained().limiting_reason == "full_charge_assist_solar"
    assert rig.controller.device_output_control_allowed(rig.dev) is True

    rig.item.soc_limit = 1
    rig.run(cycles=2, ac_mode=False)

    assert rig.record()["full_charge_assist_active"] is False
    assert rig.record()["restore_pending"] is False
    assert rig.item.output_limit > 0


def test_an_assist_that_cannot_write_holds_back_no_pv(tmp_path):
    """Behind a shut state gate it raises no socSet, so it takes nothing from the house."""

    rig = Day(tmp_path)
    rig.store.mark_assist_started("WR1", MIDNIGHT, True)
    rig.at = MORNING

    rig.run(cycles=12, state_writes=False)

    assert _state_writes(rig.hardware.writes) == []
    assert rig.item.output_limit > 0
    assert rig.explained().limiting_reason != "full_charge_assist_solar"


def test_an_assist_that_owes_the_ac_output_back_keeps_its_claim(tmp_path):
    """Without its AC mode, a device still in the assist's AC input is claimed back first."""

    rig = Day(
        tmp_path,
        item=state(soc=50, max_soc=90, ac_mode=1, ac_status=2, input_limit_w=POWER),
    )
    rig.item.grid_input = POWER
    rig.store.mark_assist_started("WR1", MIDNIGHT, True)
    rig.store.update_device_state("WR1", MIDNIGHT, ac_mode_restore_pending=True)
    rig.at = MORNING
    config = {**cfg.BATTERY_FULL_CHARGE_ASSIST_DEFAULTS, "enable_ac_charge_mode": False}
    status = describe_full_charge_assist_status(config, True, True, rig.record(), MORNING)
    assert (status["status"], status["phase"]) == ("active", "ac")
    assert diagnose_battery_full_charge_status(rig.record(), config, MORNING) == "active"

    rig.run(cycles=1, ac_mode=False)

    assert rig.controller.runtime_intents["WR1"].reason == FULL_CHARGE_ASSIST_REASON
    assert {"acMode": 2} in rig.hardware.writes


def test_pv_around_the_ceiling_does_not_move_the_logged_pv_claim(tmp_path, caplog):
    rig = Day(tmp_path)
    rig.item.charge_max_limit_w = 1000
    rig.controller.shelly = ShellyStub(300)
    rig.at = MORNING

    with caplog.at_level(logging.INFO):
        for pv in [990, 1020] * 10:
            rig.item.solar = pv
            rig.run(cycles=1)

    moves = [
        record
        for record in caplog.records
        if "pv_first_priority_changed" in record.getMessage()
    ]
    assert len(moves) <= 1


def _config(name):
    return SimpleNamespace(
        name=name, max_power=800, pv_kwp=1.0, pv_priority_factor=1.0, battery_kwh=2.0
    )


@pytest.mark.parametrize("requested", [80, 300, 450, 500])
def test_the_split_hands_a_held_device_s_pv_to_the_others(requested):
    """Its PV above the ceiling is not on offer, so another battery covers the rest."""

    held = state(soc=80, max_soc=90)
    held.solar = 400
    other = state(soc=80, max_soc=90)
    other.solar = 100
    states = [held, other]

    targets, _current, _new, explanation = calculate_targets(
        0,
        states,
        1600,
        device_configs=[_config("WR1"), _config("WR2")],
        capabilities=[detect_capabilities(report) for report in states],
        requested_total=requested,
        explain=True,
        discharge_caps=[0, None],
    )

    assert targets[0] == 0
    assert sum(targets) == requested
    assert explanation.undistributed_target_w == 0


@pytest.mark.parametrize("requested", [100, 250, 400, 700, 701, 800])
def test_a_held_device_does_not_hold_back_the_others_pv_for_its_battery(requested):
    """The emptier device at its minSoc still feeds the house: no gap in the split.

    The charge balance would hand the emptier battery's PV to the fuller one,
    but the held one takes it all for itself, and the split gave the house
    nothing up to the summed PV, then everything.
    """

    held = state(soc=60, max_soc=90)
    held.solar = 300
    other = state(soc=15, min_soc=15, max_soc=90)
    other.solar = 400
    states = [held, other]

    targets, _current, _new = calculate_targets(
        0,
        states,
        1600,
        device_configs=[_config("WR1"), _config("WR2")],
        capabilities=[detect_capabilities(report) for report in states],
        requested_total=requested,
        discharge_caps=[0, None],
    )

    assert targets == [0, min(requested, 400)]


@pytest.mark.parametrize("held_soc,other_soc", [(40, 80), (80, 40)])
def test_pv_above_the_ceiling_goes_first(held_soc, other_soc):
    """The battery cannot take it, so it feeds the house before the other device's PV."""

    held = state(soc=held_soc, max_soc=90)
    held.solar = 1300
    other = state(soc=other_soc, max_soc=90)
    other.solar = 600
    states = [held, other]

    def split(requested):
        return calculate_targets(
            0,
            states,
            1600,
            device_configs=[_config("WR1"), _config("WR2")],
            capabilities=[detect_capabilities(report) for report in states],
            requested_total=requested,
            discharge_caps=[300, None],
        )[0]

    assert split(250) == [250, 0]
    assert split(500) == [300, 200]


@pytest.mark.parametrize("requested", [800, 850, 900, 1000, 1005, 1100])
def test_three_devices_leave_no_gap_in_the_split(requested):
    """The emptiest at its minSoc feeds the house once the fuller one is at its rating."""

    held = state(soc=60, max_soc=90)
    held.solar = 600
    fuller = state(soc=80, max_soc=90)
    fuller.solar = 100
    empty = state(soc=15, min_soc=15, max_soc=90)
    empty.solar = 300
    states = [held, fuller, empty]

    targets, _current, _new = calculate_targets(
        0,
        states,
        2400,
        device_configs=[_config("WR1"), _config("WR2"), _config("WR3")],
        capabilities=[detect_capabilities(report) for report in states],
        requested_total=requested,
        discharge_caps=[0, None, None],
    )

    assert targets[0] == 0
    assert sum(targets) == requested


class Answering:
    """A grid meter that sees what the devices feed out."""

    def __init__(self, house, items):
        self.house = house
        self.items = items

    def get_power(self):
        return self.house - sum(item.output_limit for item in self.items)


class Fleet(Day):
    """The loop over a device in the PV phase and others that cover the house."""

    def __init__(self, tmp_path, held, others, house):
        super().__init__(tmp_path, item=held)
        self.others = others
        self.more = [
            following_http_device(
                other, name=f"WR{index}", hardware_profile="solarflow_800_pro_2"
            )[0]
            for index, other in enumerate(others, start=2)
        ]
        self.controller = EMSController(
            devices=[self.dev, *self.more],
            shelly=Answering(house, [self.item, *others]),
            sleep_enabled=False,
            runtime_state=self.runtime,
            battery_full_charge_store=self.store,
        )

    @contextmanager
    def loop(self, ac_mode=True, **gate_options):
        with (
            super().loop(ac_mode=ac_mode, **gate_options),
            patch.object(cfg, "MAX_TOTAL_POWER", 800 * (1 + len(self.others))),
        ):
            yield

    def fetch(self, _devices):
        self.dev.fetch()
        for dev in self.more:
            dev.fetch()
        items = [self.item, *self.others]
        for item in items:
            item.output = item.output_limit
        return items

    def grid(self, cycles):
        grid = []
        with self.loop():
            for _ in range(cycles):
                self.controller.run_once()
                self.clock.now += 5
                grid.append(round(self.controller.shelly.get_power()))
        return grid


@pytest.mark.parametrize(
    "held_pv,ceiling,house,other_pv,other_soc",
    [
        (400, 1000, 300, 100, 80),
        (1300, 1000, 600, 100, 80),
        (300, 1000, 250, 400, 15),
    ],
    ids=[
        "winter: all PV into the battery",
        "summer: PV above the ceiling",
        "the other at its minSoc",
    ],
)
def test_the_other_device_covers_the_house(
    tmp_path, held_pv, ceiling, house, other_pv, other_soc
):
    held = state(soc=60, max_soc=90, ac_mode=2)
    held.solar = held_pv
    held.charge_max_limit_w = ceiling
    other = state(soc=other_soc, min_soc=15, max_soc=90, ac_mode=2)
    other.solar = other_pv
    rig = Fleet(tmp_path, held, [other], house)
    rig.at = MORNING

    grid = rig.grid(60)

    assert rig.record()["full_charge_assist_active"] is True
    assert rig.item.output_limit == max(0, held_pv - ceiling)
    assert max(abs(value) for value in grid[-20:]) <= 30, grid[-20:]


@pytest.mark.parametrize(
    "house,held_pv,ceiling",
    [(900, 600, 1000), (900, 900, None), (1200, 1300, 1000)],
)
def test_three_devices_settle_with_the_emptiest_at_its_minsoc(
    tmp_path, house, held_pv, ceiling
):
    held = state(soc=60, min_soc=15, max_soc=90, ac_mode=2)
    held.solar = held_pv
    held.charge_max_limit_w = ceiling
    fuller = state(soc=80, min_soc=15, max_soc=90, ac_mode=2)
    fuller.solar = 100
    empty = state(soc=15, min_soc=15, max_soc=90, ac_mode=2)
    empty.solar = 300
    rig = Fleet(tmp_path, held, [fuller, empty], house)
    rig.at = MORNING

    grid = rig.grid(100)

    assert rig.record()["full_charge_assist_active"] is True
    assert max(grid[-30:]) - min(grid[-30:]) <= 40, grid[-30:]
    assert max(abs(value) for value in grid[-10:]) <= 30, grid[-10:]


def test_the_ac_charge_may_begin_at_force_time_on_the_day_the_assist_started():
    started = {"assist_started_at": MIDNIGHT.isoformat()}
    late = {"assist_started_at": (FORCE + timedelta(hours=3)).isoformat()}

    assert full_charge_assist_ac_from(started, "14:00") == FORCE
    assert full_charge_assist_ac_from(late, "14:00") == FORCE
    assert full_charge_assist_ac_from({}, "14:00") is None
    assert full_charge_assist_ac_from(started, "nonsense") is None


def test_force_time_is_a_local_hour_across_a_dst_change(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    try:
        started = datetime(2026, 3, 29, 0, 0, tzinfo=timezone(timedelta(hours=1)))
        ac_from = full_charge_assist_ac_from(
            {"assist_started_at": started.isoformat()}, "14:00"
        )
        assert ac_from.hour == 14
        assert ac_from.utcoffset() == timedelta(hours=2)
    finally:
        monkeypatch.undo()
        time.tzset()


def test_the_card_and_diagnose_name_the_pv_phase():
    config = {**cfg.BATTERY_FULL_CHARGE_ASSIST_DEFAULTS, "force_time": "14:00"}
    record = {
        "full_charge_assist_active": True,
        "assist_started_at": MIDNIGHT.isoformat(),
        "next_due_at": DUE.isoformat(),
        "has_battery": True,
    }

    morning = describe_full_charge_assist_status(config, True, True, record, MORNING)
    afternoon = describe_full_charge_assist_status(config, True, True, record, FORCE)
    without_ac = describe_full_charge_assist_status(
        {**config, "enable_ac_charge_mode": False}, True, True, record, FORCE
    )

    assert (morning["status"], morning["phase"]) == ("active", "solar")
    assert morning["ac_from"] == FORCE.isoformat()
    assert (afternoon["phase"], afternoon["ac_from"]) == ("ac", None)
    assert (without_ac["phase"], without_ac["ac_from"]) == ("solar", None)
    assert diagnose_battery_full_charge_status(record, config, MORNING) == (
        "active, charging from PV until 14:00"
    )
    assert diagnose_battery_full_charge_status(record, config, FORCE) == "active"
    assert diagnose_battery_full_charge_status(
        record, {**config, "enable_ac_charge_mode": False}, FORCE
    ) == "active, charging from PV"
