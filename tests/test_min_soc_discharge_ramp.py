# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Battery care: a battery close to its minSoc discharges gently.

The SolarFlow 800 Pro 2 lifts its discharge floor (``socLimit 2``) one point
above minSoc. The EMS saw a dischargeable device and gave it 600-800 W at once;
1 % of the pack is about 29 Wh, so it was back on the floor within 25 s, and
re-entering the floor made the firmware draw about 1.2 kW from the grid for a
few seconds. A few watts of PV, or the SoC estimate relaxing at rest, lifted it
to minSoc + 1 again and the loop repeated every 10-13 minutes (server EMS,
2026-10-05 and 2026-10-23).

The point dropped after a few watt-hours, not the 29 Wh it holds: the cell
voltage sags under a high current near the floor and the SoC estimate follows
it. So the battery share grows with the square of the distance from minSoc
(owner 2026-11-12, "Akkuschonung"). Since the live test (owner 2026-11-13) a
battery by default rests one point above minSoc and passes only its PV, may
give 50 W on the next point (the minimum discharge, the foot of the curve), and
full power from 3 points outside winter mode and from 4 points in it. The cycle
tests run on a longer curve, no PV-only point and full power at 5 and 10
points, which holds a device across more points.
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ems.config import (
    BATTERY_CARE_DEFAULTS,
    BATTERY_CARE_MAX_POINTS,
    BATTERY_CARE_MAX_WATTS,
    normalize_battery_care_config,
)
from ems.controller import EMSController
from ems.models import DeviceState
from ems.target_control import (
    MinSocRamp,
    apply_battery_topup_after_pv_first,
    calculate_targets,
    detect_capabilities,
    min_soc_ramp_cap_w,
    min_soc_ramp_share,
)

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.simulation,
    pytest.mark.unit,
]

RAMP = MinSocRamp(pv_only_points=0, full_points=5, min_discharge_w=50)
LONG_RAMP_CARE = {
    **BATTERY_CARE_DEFAULTS,
    "summer_full_points": 5,
    "winter_full_points": 10,
    "pv_only_points": 0,
}


def device(name="WR1", max_power=800):
    return SimpleNamespace(
        name=name,
        max_power=max_power,
        pv_kwp=1.0,
        pv_priority_factor=1.0,
        battery_kwh=1.0,
        min_soc=15,
        max_soc=100,
        smart_mode=1,
        grid_off_mode=None,
    )


def state(soc, solar=0, min_soc=15, max_soc=100, **overrides):
    values = dict(
        soc=soc,
        min_soc=min_soc,
        max_soc=max_soc,
        solar=solar,
        output=0,
        pack_in=0,
        pack_out=0,
        temp=20,
        voltage=48,
        rssi=0,
        remain_minutes=0,
        solar1=0,
        solar2=0,
        solar3=0,
        solar4=0,
        output_limit=0,
        soc_limit=0,
        pack_state=2,
        fault_level=0,
        smart_mode=1,
        grid_off_mode=0,
        ac_mode=2,
        ac_status=1,
        dc_status=1,
        grid_state=1,
        pack_num=1,
    )
    values.update(overrides)
    return DeviceState(**values)


def caps_for(states, devices, ramp=RAMP):
    return [min_soc_ramp_cap_w(s, d, ramp) for s, d in zip(states, devices)]


def targets_for(states, devices, requested_total, ramp=RAMP, explain=False):
    return calculate_targets(
        0,
        states,
        sum(d.max_power for d in devices),
        device_configs=devices,
        capabilities=[detect_capabilities(s) for s in states],
        requested_total=requested_total,
        explain=explain,
        discharge_caps=caps_for(states, devices, ramp),
    )


@pytest.mark.parametrize(
    "points, share",
    [(-3, 0.0), (0, 0.0), (1, 0.0), (2, 0.0625), (3, 0.25), (4, 0.5625), (5, 1.0), (40, 1.0)],
)
def test_the_share_grows_with_the_square_of_the_distance_from_the_first_point(points, share):
    assert min_soc_ramp_share(points, RAMP) == pytest.approx(share)


def test_the_winter_curve_runs_from_the_minimum_discharge_to_full_power_at_ten_points():
    winter = MinSocRamp(pv_only_points=0, full_points=10, min_discharge_w=50)
    watts = [min_soc_ramp_cap_w(state(15 + points), device(), winter) for points in (1, 2, 3, 4, 5, 7, 10)]
    assert watts == [50, 59, 87, 133, 198, 383, None]


def test_the_first_point_gives_exactly_the_minimum_discharge_and_never_more_than_the_device():
    assert min_soc_ramp_cap_w(state(16), device(), RAMP) == 50
    assert min_soc_ramp_cap_w(state(16, solar=100), device(), MinSocRamp(0, 5, 0)) == 90
    assert min_soc_ramp_cap_w(state(16), device(), MinSocRamp(0, 5, 900)) is None
    assert min_soc_ramp_cap_w(state(15), device(), RAMP) == 0


def test_full_points_zero_switches_the_ramp_off():
    off = MinSocRamp(pv_only_points=0, full_points=0)
    assert min_soc_ramp_share(0, off) == 1.0
    assert min_soc_ramp_cap_w(state(16), device(), off) is None


def test_full_points_at_or_below_pv_only_is_a_step():
    step = MinSocRamp(pv_only_points=3, full_points=2)
    assert [min_soc_ramp_share(points, step) for points in (2, 3, 4)] == [0.0, 0.0, 1.0]


def test_a_narrow_window_reaches_full_power_by_its_middle():
    assert min_soc_ramp_share(2, RAMP, window=6) == pytest.approx(0.25)
    assert min_soc_ramp_share(3, RAMP, window=6) == 1.0
    assert min_soc_ramp_share(2, RAMP, window=60) == pytest.approx(0.0625)


def test_a_window_too_narrow_for_the_ramp_still_holds_the_first_point_to_its_minimum():
    assert min_soc_ramp_share(1, RAMP, window=2) == 0.0
    assert min_soc_ramp_share(2, RAMP, window=2) == 1.0
    assert min_soc_ramp_cap_w(state(41, min_soc=40, max_soc=42), device(), RAMP) == 50


def test_pv_only_points_hold_the_battery_before_the_curve_starts():
    held = MinSocRamp(pv_only_points=1, full_points=5, min_discharge_w=50)
    assert [min_soc_ramp_cap_w(state(15 + points), device(), held) for points in (1, 2, 3, 5)] == [
        0,
        50,
        133,
        None,
    ]


def test_the_cap_passes_pv_through_and_adds_the_battery_share():
    assert min_soc_ramp_cap_w(state(16, solar=100), device(), RAMP) == 140
    assert min_soc_ramp_cap_w(state(18, solar=0), device(), RAMP) == 238
    assert min_soc_ramp_cap_w(state(19, solar=600), device(), RAMP) is None
    assert min_soc_ramp_cap_w(state(20, solar=0), device(), RAMP) is None


def test_a_raised_min_soc_above_the_battery_means_pv_only():
    assert min_soc_ramp_cap_w(state(30, solar=50, min_soc=33), device(), RAMP) == 45


@pytest.mark.parametrize("overrides", [{"pack_num": 0}, {"max_soc": 0}])
def test_a_device_without_a_battery_is_never_held(overrides):
    assert min_soc_ramp_cap_w(state(16, **overrides), device(), RAMP) is None


def test_one_point_above_min_soc_the_house_load_does_not_drain_the_battery():
    """The incident: SoC minSoc + 1, floor lifted, the house asks 800 W."""

    targets, _current, _new = targets_for([state(16, solar=40)], [device()], 800)
    assert targets == [86]


def test_what_a_held_device_cannot_deliver_goes_to_the_others_by_weight():
    devices = [device("Low"), device("High"), device("Mid")]
    states = [state(16), state(80), state(40)]
    targets, _current, _new = targets_for(states, devices, 1200, ramp=MinSocRamp(0, 5, 20))
    assert targets[0] == 20
    assert targets[1] > targets[2] > 0
    assert sum(targets) == 1200


def test_what_no_device_can_deliver_comes_from_the_grid():
    devices = [device("Low"), device("High")]
    targets, _current, _new = targets_for([state(17), state(80)], devices, 1600)
    assert targets == [97, 800]


def test_redistribution_never_lifts_a_held_device_above_its_cap():
    devices = [device("Low"), device("Small", max_power=300)]
    targets, _current, _new = targets_for([state(16, solar=100), state(80)], devices, 1000)
    assert targets[0] <= 140
    assert targets[1] == 300


def test_the_pv_first_top_up_stops_at_the_cap():
    devices = [device("Low"), device("High")]
    states = [state(17), state(80)]
    targets, used = apply_battery_topup_after_pv_first(
        [0, 0],
        states,
        devices,
        [detect_capabilities(s) for s in states],
        1200,
        caps=caps_for(states, devices),
    )
    assert used is True
    assert targets[0] <= 97
    assert targets[1] <= 800


def test_without_caps_the_allocation_is_unchanged():
    devices = [device("A"), device("B")]
    states = [state(16, solar=120), state(55, solar=300)]
    capabilities = [detect_capabilities(s) for s in states]
    for requested in (0, 150, 420, 900, 1500):
        plain = calculate_targets(
            0, states, 1600, device_configs=devices, capabilities=capabilities,
            requested_total=requested,
        )
        uncapped = calculate_targets(
            0, states, 1600, device_configs=devices, capabilities=capabilities,
            requested_total=requested, discharge_caps=[None, None],
        )
        assert plain == uncapped


def test_the_explanation_names_the_ramp():
    targets, _current, _new, explanation = targets_for(
        [state(16, solar=40), state(80)], [device("Low"), device("High")], 900, explain=True
    )
    assert explanation.devices["Low"].limiting_reason == "battery_care"
    limit = next(item for item in explanation.limits if item.name == "battery_care")
    assert limit.active is True
    assert limit.value == "[86, null]"


class RuntimeStateStub:
    def __init__(self, system=None, winter=None, devices=None, care=None):
        self.system = system or {}
        self.devices = devices or {}
        self.data = {
            "system": self.system,
            "winter": winter or {"enabled": False},
            "battery_care": {**LONG_RAMP_CARE, **(care or {})},
        }

    def load_if_changed(self):
        return None

    def get_system(self, key, default=None):
        return self.system.get(key, default)

    def get_device(self, device_name, key, default=None):
        return self.devices.get(device_name, {}).get(key, default)


class ShellyStub:
    def __init__(self, power=0):
        self.power = power

    def get_power(self):
        return self.power


def controller(system=None, devices=None, load=0, winter=None, runtime_devices=None, care=None):
    return EMSController(
        devices=devices or [device()],
        shelly=ShellyStub(load),
        sleep_enabled=False,
        runtime_state=RuntimeStateStub(system, winter, runtime_devices, care),
    )


def test_the_controller_reads_battery_care_from_the_runtime_state():
    states = [state(16, solar=100)]
    assert controller().min_soc_ramp_caps(states) == [140]
    assert controller(care={"min_discharge_w": 20}).min_soc_ramp_caps(states) == [110]
    assert controller(care={"enabled": False}).min_soc_ramp_caps(states) == [None]
    assert controller(care={"summer_full_points": 0}).min_soc_ramp_caps(states) == [None]
    wider = controller(care={"pv_only_points": 2, "summer_full_points": 10})
    assert wider.min_soc_ramp_caps([state(21)]) == [188]


def test_the_standby_floor_never_lifts_a_device_above_its_cap():
    ems = controller(devices=[device("A"), device("B"), device("C")])
    ems.device_commandable = Mock(return_value=True)
    ems.device_charge_floor_w = Mock(return_value=0)
    ems.device_may_discharge = Mock(return_value=True)
    assert ems.effective_control_targets([0, 0, 0], True, 35, [0, 20, None]) == [0, 20, 35]
    assert ems.effective_control_targets([0, 0, 0], True, 35) == [35, 35, 35]


def test_a_charging_device_is_not_held_by_the_ramp():
    ems = controller()
    ems.device_commandable = Mock(return_value=True)
    ems.device_charge_floor_w = Mock(return_value=-600)
    ems.device_may_discharge = Mock(return_value=True)
    assert ems.effective_control_targets([-600], True, 35, [0]) == [-600]


def test_a_charge_the_ems_commanded_ends_on_the_full_floor():
    """A zero against telemetry that does not show the charge yet is never written."""

    ems = controller(devices=[device("Charging"), device("Idle")])
    ems.charge_commanded_by_ems = lambda dev: dev.name == "Charging"
    assert ems.standby_floor_caps([0, 0], [state(16), state(16)]) == [None, 0]


def test_one_point_above_min_soc_the_loop_writes_only_the_minimum_discharge():
    """The whole cycle. Without battery care this loop writes 785 W, then 800 W."""

    ems = controller(load=800)
    ems.set_output_limit = Mock()
    with patch(
        "ems.controller.fetch_all_devices",
        return_value=[state(16, solar=0, output=35, output_limit=35)],
    ), patch("ems.controller.cfg.SOC_RECONCILE_INTERVAL", 0), patch(
        "ems.controller.cfg.MAX_TOTAL_POWER", 800
    ), patch(
        "ems.controller.cfg.MIN_OUTPUT_LIMIT", 35
    ):
        for _cycle in range(6):
            ems.run_once()
    written = [call.args[1] for call in ems.set_output_limit.call_args_list]
    assert written and max(written) <= 50


def test_winter_mode_takes_the_winter_full_point():
    from datetime import datetime

    states = [state(17, solar=100)]
    winter = controller(winter={"enabled": True})
    assert winter.min_soc_ramp_caps(states, now=datetime(2027, 1, 15)) == [149]
    assert winter.min_soc_ramp_caps(states, now=datetime(2026, 7, 15)) == [187]
    off = controller(winter={"enabled": False})
    assert off.min_soc_ramp_caps(states, now=datetime(2027, 1, 15)) == [187]
    no_winter_ramp = controller(winter={"enabled": True}, care={"winter_full_points": 0})
    assert no_winter_ramp.min_soc_ramp_caps(states, now=datetime(2027, 1, 15)) == [None]


def test_the_defaults_rest_one_point_above_min_soc_and_reach_full_power_at_three_or_four():
    from datetime import datetime

    shipped = controller(winter={"enabled": True}, care=BATTERY_CARE_DEFAULTS)
    summer, winter = datetime(2026, 7, 15), datetime(2027, 1, 15)
    assert shipped.min_soc_ramp_caps([state(16, solar=100)], now=summer) == [90]
    assert [shipped.min_soc_ramp_caps([state(soc)], now=summer) for soc in (16, 17, 18)] == [[0], [50], [None]]
    assert [shipped.min_soc_ramp_caps([state(soc)], now=winter) for soc in (16, 17, 18, 19)] == [
        [0],
        [50],
        [238],
        [None],
    ]


def test_a_soc_of_zero_without_a_confirmed_pack_is_not_held():
    assert min_soc_ramp_cap_w(state(0, solar=100, pack_num=None), device(), RAMP) is None
    assert min_soc_ramp_cap_w(state(0, solar=100, min_soc=0), device(), RAMP) == 90


def test_the_explanation_names_the_ramp_only_where_it_holds():
    _targets, _current, _new, explanation = targets_for(
        [state(16, solar=40), state(80)], [device("Low"), device("High")], 0, explain=True
    )
    assert "battery_care" not in {item.name for item in explanation.limits}
    assert explanation.devices["Low"].limiting_reason != "battery_care"


def test_a_falling_ceiling_applies_in_the_same_cycle_not_at_the_ramp_pace():
    ems = controller()
    ems.commanded_device_targets["WR1"] = 800
    assert ems.apply_device_ramp([0], 0, [0]) == [0]
    assert ems.commanded_device_targets["WR1"] == 0
    ems.commanded_device_targets["WR1"] = 800
    assert ems.apply_device_ramp([0], 0) == [600]


def test_a_battery_on_its_floor_keeps_the_full_standby_floor():
    ems = controller(devices=[device("Floor"), device("Above")])
    ems.charge_commanded_by_ems = lambda dev: False
    assert ems.standby_floor_caps([0, 0], [state(15), state(16)]) == [None, 0]


def test_the_integrator_is_held_to_what_the_held_devices_can_deliver():
    """A wound-up total would land on the device the moment its ceiling rises."""

    ems = controller(load=400)
    ems.set_output_limit = Mock()
    held = [state(17, solar=50, output=245, output_limit=245)]
    with patch("ems.controller.fetch_all_devices", return_value=held), patch(
        "ems.controller.cfg.SOC_RECONCILE_INTERVAL", 0
    ), patch("ems.controller.cfg.MAX_TOTAL_POWER", 800), patch(
        "ems.controller.cfg.MIN_OUTPUT_LIMIT", 35
    ):
        for _cycle in range(10):
            ems.run_once()
    assert ems.commanded_total_w <= 245


def _ceiling(states, caps, max_power=1600, capabilities=None):
    ems = controller(devices=[device(f"D{i}") for i in range(len(states))])
    capabilities = capabilities or [detect_capabilities(s) for s in states]
    return ems.discharge_ceiling_w(max_power, caps, states, capabilities)


def test_the_ceiling_counts_what_each_device_can_deliver_this_cycle():
    from ems.models import DeviceCapabilities

    held, full = state(16), state(80)
    assert _ceiling([held, full], [0, None]) == 800
    assert _ceiling([held, full], [0, None], max_power=600) == 600
    assert _ceiling([held, full], [None, None]) == 1600
    assert _ceiling([held, state(0, solar=0, pack_num=0)], [0, None]) == 0
    assert _ceiling([held, state(0, solar=120, pack_num=0)], [0, None]) == 120
    floored = state(15, solar=60, soc_limit=2)
    assert _ceiling([held, floored], [0, 54]) == 60
    assert _ceiling([floored, full], [54, None]) == 1600
    assert _ceiling([state(16, output=245, output_limit=245), full], [0, None]) == 1045
    blocked = DeviceCapabilities(False, False, False, False, "ac_discharge_disabled")
    capabilities = [detect_capabilities(held), blocked]
    assert _ceiling([held, full], [0, None], capabilities=capabilities) == 0


def test_a_held_device_beside_one_that_cannot_deliver_does_not_wind_up():
    """Without the second device's real capacity the total climbed to 864 W at night."""

    ems = controller(devices=[device("Held"), device("NoBattery")], load=150)
    ems.set_output_limit = Mock()
    night = [state(16), state(0, solar=0, pack_num=0)]
    with patch("ems.controller.fetch_all_devices", return_value=night), patch(
        "ems.controller.cfg.SOC_RECONCILE_INTERVAL", 0
    ), patch("ems.controller.cfg.MAX_TOTAL_POWER", 1600), patch(
        "ems.controller.cfg.MIN_OUTPUT_LIMIT", 35
    ):
        for _cycle in range(10):
            ems.run_once()
    assert ems.commanded_total_w <= 70


def test_a_battery_on_its_floor_is_not_named_as_held_by_the_ramp():
    _targets, _current, _new, explanation = targets_for(
        [state(15, soc_limit=2), state(80)], [device("Floor"), device("High")], 500, explain=True
    )
    assert explanation.devices["Floor"].limiting_reason != "battery_care"
    assert "battery_care" not in {item.name for item in explanation.limits}


def overview_rule(ems, states, later=None):
    """The Overview's battery care rule after ``ems`` ran three cycles.

    Three, because the first cycle's split hands a battery one point above its
    minSoc less than its ceiling, so battery care holds it only from the second.
    The first cycle reads ``states``, the other two ``later`` if it is given.
    """

    from dashboard.telemetry import build_dashboard_snapshot

    ems.set_output_limit = Mock()
    reads = [states, later or states, later or states]
    with patch("ems.controller.fetch_all_devices", side_effect=reads), patch(
        "ems.controller.cfg.SOC_RECONCILE_INTERVAL", 0
    ), patch("ems.controller.cfg.MAX_TOTAL_POWER", 1600), patch(
        "ems.controller.cfg.MIN_OUTPUT_LIMIT", 35
    ):
        for _cycle in range(3):
            ems.run_once()
    count = len(states)
    snapshot = build_dashboard_snapshot(
        ems, 900, states, [0] * count, [0] * count, 0, 0,
        enabled=True, max_total_power=1600, min_output_limit=35,
    )
    return snapshot["rules"]["battery_care_active"]


def test_the_overview_names_the_batteries_battery_care_holds_back():
    ems = controller(load=900, devices=[device("Low"), device("High")])
    states = [state(16, output=35, output_limit=35), state(80, output=35, output_limit=35)]
    assert overview_rule(ems, states) == {"active": True, "reason": "Low"}


def test_the_overview_rule_is_off_while_battery_care_holds_no_battery():
    from dashboard.telemetry import RULE_REASONS

    states = [state(16, output=35, output_limit=35), state(80, output=35, output_limit=35)]
    inactive = {"active": False, "reason": RULE_REASONS["battery_care_active"]}
    switched_off = controller(load=900, devices=[device("Low"), device("High")], care={"enabled": False})
    assert overview_rule(switched_off, states) == inactive
    far_from_floor = [state(60, output=35, output_limit=35), state(80, output=35, output_limit=35)]
    assert overview_rule(controller(load=900, devices=[device("Mid"), device("High")]), far_from_floor) == inactive


def test_the_overview_does_not_name_a_battery_the_ems_does_not_command():
    """Its share stays in the split, but nothing is written to it, so nothing holds it back."""

    from dashboard.telemetry import RULE_REASONS

    inactive = {"active": False, "reason": RULE_REASONS["battery_care_active"]}
    states = [state(16, output=35, output_limit=35), state(80, output=35, output_limit=35)]
    control_off = controller(system={"enabled": False}, load=900, devices=[device("Low"), device("High")])
    assert overview_rule(control_off, states) == inactive
    switched_off = controller(
        load=900, devices=[device("Low"), device("High")], runtime_devices={"Low": {"enabled": False}}
    )
    assert overview_rule(switched_off, states) == inactive
    offline = controller(load=900, devices=[device("Low"), device("High")])
    assert overview_rule(offline, states, later=[None, states[1]]) == inactive


def test_the_admin_reads_a_hand_edited_distance_as_the_ems_does():
    from admin.config_runtime_overlap import compute_overlap_provenance, config_effective_value

    config = {"battery_care": {"winter_full_points": 80}}
    provenance = compute_overlap_provenance(config, {"battery_care": {"winter_full_points": 50}})
    assert provenance["battery_care.winter_full_points"]["source"] == "config"
    assert config_effective_value(config, "section", "battery_care", "winter_full_points") == 50


def test_the_admin_reads_a_hand_edited_minimum_discharge_as_the_ems_does():
    from admin.config_runtime_overlap import config_effective_value

    for written, read in ((9000, BATTERY_CARE_MAX_WATTS), (-20, 0), ("x", 50), (80, 80)):
        config = {"battery_care": {"min_discharge_w": written}}
        assert config_effective_value(config, "section", "battery_care", "min_discharge_w") == read
        assert normalize_battery_care_config(config["battery_care"])["min_discharge_w"] == read


def test_a_runtime_writer_takes_the_minimum_discharge_in_watts_within_its_bounds():
    from ems.runtime_commands import RuntimeWriteError, SECTION_COMMAND_TABLES, validate_section_values

    assert SECTION_COMMAND_TABLES["battery_care"]["min-discharge"].key == "min_discharge_w"
    assert validate_section_values("battery_care", {"min_discharge_w": "60"}) == {"min_discharge_w": 60}
    with pytest.raises(RuntimeWriteError):
        validate_section_values("battery_care", {"min_discharge_w": BATTERY_CARE_MAX_WATTS + 1})


def test_the_editors_offer_the_runtime_bounds():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    modern = (root / "frontend/dashboard-modern/src/widgets/RuntimeEditor.svelte").read_text(encoding="utf-8")
    classic = (root / "dashboard/static/app.js").read_text(encoding="utf-8")
    assert modern.count(f"min: 0, max: {BATTERY_CARE_MAX_POINTS}, unit") == 3
    assert f'"min_discharge_w", type: "number", label: t("runtime.batteryCareMinDischarge"), min: 0, max: {BATTERY_CARE_MAX_WATTS}, unit: "W"' in modern
    assert classic.count(f', 0, {BATTERY_CARE_MAX_POINTS}, "%", "1")') == 3


def test_a_device_the_allocation_keeps_counts_as_the_allocation_counts_it():
    """Offline or switched off, a device keeps its share of the split, which only it delivers."""

    from ems.models import DeviceCapabilities

    ems = controller(devices=[device("Held"), device("Off"), device("Ok")])
    states = [state(16), state(80, output=300, output_limit=300), state(80)]
    capabilities = [detect_capabilities(s) for s in states]
    assert ems.discharge_ceiling_w(2400, [0, None, None], states, capabilities) == 1600
    capabilities[1] = DeviceCapabilities(True, False, False, False, "runtime_role_ac_input")
    assert ems.discharge_ceiling_w(2400, [0, None, None], states, capabilities) == 800
    no_battery = [state(16), state(0, solar=0, pack_num=0), state(80)]
    capabilities = [detect_capabilities(s) for s in no_battery]
    assert ems.discharge_ceiling_w(2400, [0, None, None], no_battery, capabilities) == 800


class LoopMeter:
    """The grid meter of a closed loop: the house less what the devices deliver."""

    def __init__(self, house, delivering):
        self.house = house
        self.delivering = dict(delivering)
        self.written = {}

    def get_power(self):
        return self.house - sum({**self.delivering, **self.written}.values())

    def write(self, dev, target, *args, **kwargs):
        self.written[dev.name] = max(0, target)
        return True


def test_a_switched_off_device_beside_a_held_one_does_not_cost_import():
    """The healthy device must cover what the house needs; a pinned total left 250 W on the grid."""

    meter = LoopMeter(1100, {"Off": 300})
    ems = EMSController(
        devices=[device("Held"), device("Off"), device("Ok")],
        shelly=meter,
        sleep_enabled=False,
        runtime_state=RuntimeStateStub(devices={"Off": {"enabled": False}}),
    )
    ems.set_output_limit = Mock(side_effect=meter.write)
    plant = [state(16), state(80, output=300, output_limit=300), state(80)]
    with patch("ems.controller.fetch_all_devices", return_value=plant), patch(
        "ems.controller.cfg.SOC_RECONCILE_INTERVAL", 0
    ), patch("ems.controller.cfg.MAX_TOTAL_POWER", 2400), patch(
        "ems.controller.cfg.MIN_OUTPUT_LIMIT", 35
    ):
        for _cycle in range(25):
            ems.run_once()
    assert meter.written["Held"] <= 50
    assert meter.written["Ok"] >= 750
    assert abs(meter.get_power()) <= 60
