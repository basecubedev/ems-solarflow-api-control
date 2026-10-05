# SPDX-License-Identifier: AGPL-3.0-or-later
"""In winter, PV refills a battery winter mode raised above its SoC.

Winter mode raises minSoc a few points above the SoC each morning. Measured on
2026-10-05 on two SolarFlow 800 Pro 2 at minSoc 23/24 % and SoC 20/21 %: the
firmware did not discharge, but it also did not hold PV back for the battery.
Once a water heater drew power, the EMS raised ``outputLimit`` up to the PV and
the battery received nothing, so the raise was never reached. The EMS therefore
keeps such a device from exporting until the battery holds its minSoc -- for a
bounded window after the day's first PV and after the step, so a pack that
cannot take the charge does not curtail its PV all day.
"""

from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ems import config as cfg
from ems.controller import EMSController
from ems.target_control import calculate_targets, detect_capabilities
from ems.winter_reserve import HOLD_WINDOW, SOLAR_CHARGE_REASON
from tests.test_battery_less_allocation import allocate, device, state

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
]

MORNING = datetime(2026, 11, 2, 9, 0)


@pytest.fixture(autouse=True)
def _no_configured_devices():
    """Policies resolve from the device type alone unless a test configures one."""

    with patch.object(cfg, "ZENDURE_CONFIG", []), patch.object(
        cfg, "state_reconciliation_writes_allowed", lambda gate: True
    ):
        yield


def _controller(*devices, first_pv=MORNING):
    controller = EMSController(devices=list(devices), shelly=None, sleep_enabled=False)
    if first_pv is not None:
        for dev in devices:
            item = controller.winter.device(dev.name)
            item.pv_date = first_pv.date().isoformat()
            item.pv_start_at = first_pv
    return controller


def _cycle(controller, states, now=MORNING, *, winter_active=True):
    capabilities = [detect_capabilities(item) for item in states]
    with patch.object(cfg, "winter_mode_active", lambda now, runtime=None: winter_active):
        return controller.winter_filtered_capabilities(states, capabilities, now=now)


def _held(capabilities, index=0):
    return capabilities[index].reason == SOLAR_CHARGE_REASON


def _plant():
    """Terasse below its raised minSoc with PV; Grill above its minSoc."""

    return [
        state(soc=21, solar=470, pack_num=1, min_soc=24),
        state(soc=30, solar=200, pack_num=1, min_soc=20),
    ]


def _devices():
    return [device("Terasse"), device("Grill")]


def test_a_device_below_its_min_soc_exports_nothing_while_pv_charges_it():
    devices = _devices()
    states = _plant()
    capabilities = _cycle(_controller(*devices), states)

    targets = allocate(states, 820, devices=devices, capabilities=capabilities)

    assert _held(capabilities)
    assert targets[0] == 0
    assert targets[1] > 0


def test_without_winter_mode_the_device_still_feeds_the_house():
    """The baseline the hold changes: PV-first hands the raised device its PV."""

    devices = _devices()
    states = _plant()
    capabilities = _cycle(_controller(*devices), states, winter_active=False)

    assert allocate(states, 820, devices=devices, capabilities=capabilities)[0] > 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"soc": 24},
        {"soc": 30},
        {"solar": 0},
        {"min_soc": 0},
        {"soc": 0},
        {"pack_num": 0},
    ],
    ids=["holds-min-soc", "above-min-soc", "no-pv", "min-soc-unmanaged", "no-soc-reading", "no-battery"],
)
def test_the_hold_applies_only_below_min_soc_with_pv(overrides):
    values = {"soc": 21, "solar": 470, "pack_num": 1, "min_soc": 24, **overrides}
    dev = device("Terasse")

    assert not _controller(dev).winter.solar_charge_candidate(dev, state(**values))


def test_a_device_winter_mode_does_not_manage_is_not_held():
    """MQTT control devices are output-only; winter mode never raised their minSoc."""

    dev = SimpleNamespace(**vars(device("Terasse")), supports_state_reconciliation=False)

    assert not _held(_cycle(_controller(dev), [_plant()[0]]))


def test_without_state_reconciliation_writes_a_low_battery_is_not_held():
    """Winter mode did not set that minSoc and may not manage it; the app may have."""

    with patch.object(cfg, "state_reconciliation_writes_allowed", lambda gate: False):
        capabilities = _cycle(_controller(*_devices()), _plant())

    assert not _held(capabilities)
    assert capabilities[0].can_export


def test_a_device_another_rule_keeps_from_exporting_keeps_its_reason():
    """A runtime ac_input reservation is the real reason, not winter mode."""

    controller = _controller(*_devices())
    states = _plant()
    capabilities = [detect_capabilities(item) for item in states]
    capabilities[0] = replace(capabilities[0], can_export=False, can_discharge=False, reason="runtime_role_ac_input")
    with patch.object(cfg, "winter_mode_active", lambda now, runtime=None: True):
        filtered = controller.winter_filtered_capabilities(states, capabilities, now=MORNING)

    assert filtered[0].reason == "runtime_role_ac_input"


def test_an_offline_device_is_not_held_on_its_cached_state():
    controller = _controller(device("Terasse"))
    controller.device_online["Terasse"] = False

    assert not _held(_cycle(controller, [_plant()[0]]))


def test_outside_winter_no_policy_is_resolved():
    """The control loop runs all year; summer cycles stop at the cheap checks."""

    controller = _controller(*_devices())
    with patch.object(controller.winter, "policy", side_effect=AssertionError("resolved")):
        _cycle(controller, _plant(), winter_active=False)


def test_the_hold_is_named_in_the_control_explanation():
    devices = _devices()
    states = _plant()
    capabilities = _cycle(_controller(*devices), states)

    with patch.multiple("ems.target_control.cfg", REDISTRIBUTE_CLAMPED_POWER=True):
        _, _, _, explanation = calculate_targets(
            load=0,
            devices=states,
            max_power=1600,
            device_configs=devices,
            capabilities=capabilities,
            requested_total=820,
            explain=True,
        )

    assert explanation.devices["Terasse"].capability_reason == SOLAR_CHARGE_REASON


def test_a_held_device_is_no_export_capacity():
    """Counted as one, it let the commanded total wind up to max_power while held.

    When the hold lifted, the allocation jumped from min_output_limit towards
    that total and overshot into the grid.
    """

    controller = _controller(device("Terasse"))
    states = [_plant()[0]]

    assert not controller.has_output_control_export_capacity(states, _cycle(controller, states), [0])


def test_an_idle_device_with_pv_only_in_its_string_fields_is_still_export_capacity():
    """Only the winter hold is taken out; detection reads solarInputPower alone."""

    controller = _controller(device("Terasse"))
    idle = state(soc=50, solar=0, solar1=300, pack_num=1, min_soc=20, dc_status=0, ac_status=0)
    capabilities = [detect_capabilities(idle)]

    assert not capabilities[0].can_export
    assert controller.has_output_control_export_capacity([idle], capabilities, [0])


@pytest.mark.parametrize(
    "soc, holding, held",
    [(23, False, False), (22, False, True), (23, True, True), (24, True, False)],
    ids=["one-below-not-entered", "two-below-entered", "one-below-kept", "reached-released"],
)
def test_the_hold_has_a_one_point_hysteresis(soc, holding, held):
    """After minSoc followed the SoC, a one-point dip must not flip the output."""

    controller = _controller(device("Terasse"))
    controller.winter.device("Terasse").holding = holding
    capabilities = _cycle(controller, [state(soc=soc, solar=470, pack_num=1, min_soc=24)])

    assert _held(capabilities) is held


def test_the_hold_lasts_three_hours_after_the_days_first_pv():
    """A pack that cannot take the charge curtails its PV for no longer than that."""

    controller = _controller(device("Terasse"))
    low = [_plant()[0]]

    assert _held(_cycle(controller, low, MORNING + HOLD_WINDOW))
    assert not _held(_cycle(controller, low, MORNING + HOLD_WINDOW + timedelta(minutes=1)))


def test_the_step_opens_its_own_three_hour_window():
    controller = _controller(device("Terasse"))
    item = controller.winter.device("Terasse")
    item.step_date = MORNING.date().isoformat()
    item.step_at = MORNING + timedelta(hours=2)
    low = [_plant()[0]]

    assert _held(_cycle(controller, low, MORNING + timedelta(hours=4)))
    assert not _held(_cycle(controller, low, MORNING + timedelta(hours=5, minutes=1)))


def test_without_pv_seen_today_there_is_no_hold():
    """The window opens with the day's first PV a reconcile saw."""

    controller = _controller(device("Terasse"), first_pv=None)

    assert not _held(_cycle(controller, [_plant()[0]]))


def test_yesterdays_window_does_not_hold_today():
    controller = _controller(device("Terasse"), first_pv=MORNING - timedelta(days=1))

    assert not _held(_cycle(controller, [_plant()[0]]))


def test_a_restart_in_daylight_opens_no_new_hold_window():
    """The window opens at the first PV after a dark reading this process saw."""

    controller = _controller(device("Terasse"), first_pv=None)
    item = controller.winter.device("Terasse")
    controller.winter.observe_pv(device("Terasse"), _plant()[0], item, MORNING.date().isoformat(), MORNING)

    assert item.pv_start_at is None
    assert not _held(_cycle(controller, [_plant()[0]]))


def test_the_first_pv_after_a_dark_reading_opens_the_window():
    controller = _controller(device("Terasse"), first_pv=None)
    item = controller.winter.device("Terasse")
    today = MORNING.date().isoformat()
    dark = state(soc=21, solar=0, pack_num=1, min_soc=24)
    controller.winter.observe_pv(device("Terasse"), dark, item, today, MORNING - timedelta(hours=2))
    controller.winter.observe_pv(device("Terasse"), _plant()[0], item, today, MORNING)

    assert item.pv_start_at == MORNING
    assert _held(_cycle(controller, [_plant()[0]]))


def test_a_held_capability_says_so_itself():
    capabilities = _cycle(_controller(device("Terasse")), [_plant()[0]])

    assert capabilities[0].export_held
    assert not capabilities[0].can_export
