import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ems import config as target_cfg
from ems.controller import EMSController
from ems.models import DeviceCapabilities, DeviceState
from ems.target_control import calculate_targets


pytestmark = [
    pytest.mark.simulation,
    pytest.mark.power_control,
    pytest.mark.regression,
]


BASE_OUTPUT_CONTROL = {
    "load_deadband_w": 5,
    "target_deadband_w": 10,
    "filter_enabled": True,
    "filter_method": "median_ema",
    "median_window": 3,
    "ema_alpha": 0.65,
    "sign_change_fast_response_enabled": True,
    "sign_change_threshold_w": 50,
    "sign_change_filter_reset_factor": 1.0,
    "ramp_enabled": True,
    "ramp_up_w_per_cycle": 500,
    "ramp_down_w_per_cycle": 500,
    "device_ramp_enabled": True,
    "device_ramp_up_w_per_cycle": 400,
    "device_ramp_down_w_per_cycle": 400,
    "large_import_bypass_w": 600,
    "large_export_bypass_w": 600,
    "bypass_ramp_multiplier": 1.5,
    "telemetry_max_age_seconds": 10,
    "stale_telemetry_ramp_factor": 0.5,
}


TARGET_CONTROL_DEFAULTS = {
    "REDISTRIBUTE_CLAMPED_POWER": True,
    "PV_KWP_WEIGHTING": True,
    "BATTERY_KWH_WEIGHTING": True,
    "PV_CHARGE_BALANCE_ENABLED": False,
    "PV_CHARGE_BALANCE_DEADBAND_PERCENT": 5.0,
    "PV_CHARGE_BALANCE_FULL_BIAS_PERCENT": 15.0,
    "PV_CHARGE_BALANCE_STRENGTH": 1.0,
}


class ShellyStub:
    def get_power(self):
        return 0


def device(
    name="WR1",
    max_power=800,
    pv_kwp=1.0,
    pv_priority_factor=1.0,
    battery_kwh=1.0,
    min_soc=15,
    max_soc=100,
):
    return SimpleNamespace(
        name=name,
        max_power=max_power,
        pv_kwp=pv_kwp,
        pv_priority_factor=pv_priority_factor,
        battery_kwh=battery_kwh,
        min_soc=min_soc,
        max_soc=max_soc,
    )


def state(
    soc=70,
    min_soc=15,
    max_soc=100,
    solar=500,
    output=0,
    output_limit=0,
    pack_in=0,
    pack_out=0,
    soc_limit=0,
    pack_state=2,
    ac_status=1,
    dc_status=1,
):
    return DeviceState(
        soc=soc,
        min_soc=min_soc,
        max_soc=max_soc,
        solar=solar,
        output=output,
        pack_in=pack_in,
        pack_out=pack_out,
        temp=20,
        voltage=48,
        rssi=0,
        remain_minutes=0,
        solar1=0,
        solar2=0,
        solar3=0,
        solar4=0,
        output_limit=output_limit,
        soc_limit=soc_limit,
        pack_state=pack_state,
        fault_level=0,
        smart_mode=1,
        grid_off_mode=0,
        ac_mode=2,
        ac_status=ac_status,
        dc_status=dc_status,
        grid_state=1,
    )


def controller(monkeypatch, devices=None, output_control=None):
    config = dict(BASE_OUTPUT_CONTROL)
    if output_control:
        config.update(output_control)

    monkeypatch.setattr(target_cfg, "OUTPUT_CONTROL_CONFIG", config)

    return EMSController(
        devices=devices or [device()],
        shelly=ShellyStub(),
        sleep_enabled=False,
    )


def calculate_targets_with_config(
    states,
    devices=None,
    capabilities=None,
    requested_total=None,
    max_power=1200,
    load=0,
    **target_config,
):
    devices = devices or [
        device(f"WR{i + 1}")
        for i in range(len(states))
    ]

    monkeypatches = []
    try:
        for name, value in {
            **TARGET_CONTROL_DEFAULTS,
            **target_config,
        }.items():
            patch = pytest.MonkeyPatch()
            patch.setattr(target_cfg, name, value)
            monkeypatches.append(patch)

        return calculate_targets(
            load=load,
            devices=states,
            max_power=max_power,
            device_configs=devices,
            capabilities=capabilities,
            requested_total=requested_total,
        )
    finally:
        for patch in reversed(monkeypatches):
            patch.undo()


def load_fixture_cases():
    path = (
        Path(__file__).parent
        / "fixtures"
        / "power_control_regression_cases.json"
    )
    return json.loads(path.read_text())


@pytest.mark.parametrize(
    "case",
    load_fixture_cases(),
    ids=lambda case: case["name"],
)
def test_total_target_fixture_regressions(monkeypatch, case):
    ems = controller(
        monkeypatch,
        output_control=case["config"],
    )
    initial = case["initial_state"]
    states = [
        state(
            solar=500,
            output=initial.get("current_output_w", 0),
            output_limit=initial.get("output_limit_w", 0),
        )
    ]

    for step in case["steps"]:
        target = ems.stabilized_total_target(
            step["shelly_power_w"],
            states,
            case["max_power_w"],
            has_export_capacity=True,
        )

        assert step["expected_target_min_w"] <= target
        assert target <= step["expected_target_max_w"]


def test_output_filter_first_cycle_median_and_ema(monkeypatch):
    ems = controller(
        monkeypatch,
        output_control={
            "median_window": 3,
            "ema_alpha": 0.5,
            "sign_change_fast_response_enabled": False,
        },
    )

    assert ems.filter_output_control_load(100) == pytest.approx(100)
    assert ems.filter_output_control_load(900) == pytest.approx(300)
    assert ems.filter_output_control_load(100) == pytest.approx(200)


def test_sign_change_fast_response_resets_stale_filter_direction(monkeypatch):
    fast = controller(
        monkeypatch,
        output_control={
            "median_window": 3,
            "ema_alpha": 0.2,
            "sign_change_fast_response_enabled": True,
            "sign_change_threshold_w": 50,
            "sign_change_filter_reset_factor": 1.0,
        },
    )
    fast.filter_output_control_load(200)
    fast.filter_output_control_load(200)

    assert fast.filter_output_control_load(-200) == pytest.approx(-200)

    slow = controller(
        monkeypatch,
        output_control={
            "median_window": 3,
            "ema_alpha": 0.2,
            "sign_change_fast_response_enabled": False,
            "sign_change_threshold_w": 50,
        },
    )
    slow.filter_output_control_load(200)
    slow.filter_output_control_load(200)

    assert slow.filter_output_control_load(-200) > 0


def test_repeated_small_sign_changes_around_zero_stay_bounded(monkeypatch):
    ems = controller(
        monkeypatch,
        output_control={
            "median_window": 3,
            "ema_alpha": 0.65,
            "sign_change_fast_response_enabled": True,
            "sign_change_threshold_w": 50,
        },
    )

    filtered = [
        ems.filter_output_control_load(value)
        for value in [30, -30, 40, -40, 20, -20]
    ]

    assert max(abs(value) for value in filtered) <= 40


def test_total_ramp_limits_large_load_step_and_drop(monkeypatch):
    ems = controller(
        monkeypatch,
        output_control={
            "filter_enabled": False,
            "load_deadband_w": 0,
            "target_deadband_w": 0,
            "ramp_enabled": True,
            "ramp_up_w_per_cycle": 200,
            "ramp_down_w_per_cycle": 300,
            "large_import_bypass_w": 10000,
            "large_export_bypass_w": 10000,
        },
    )
    states = [state(output_limit=400)]

    assert ems.stabilized_total_target(1000, states, 1200) == 600
    assert ems.stabilized_total_target(-1000, states, 1200) == 300


def test_device_ramp_limits_per_inverter_changes(monkeypatch):
    devices = [device("WR1", max_power=800)]
    ems = controller(
        monkeypatch,
        devices=devices,
        output_control={
            "device_ramp_enabled": True,
            "device_ramp_up_w_per_cycle": 250,
            "device_ramp_down_w_per_cycle": 300,
            "large_import_bypass_w": 10000,
            "large_export_bypass_w": 10000,
        },
    )

    assert ems.apply_device_ramp([100], raw_load=0) == [100]
    assert ems.apply_device_ramp([800], raw_load=0) == [350]
    assert ems.apply_device_ramp([0], raw_load=0) == [50]


def test_large_import_export_bypass_still_affects_ramp_only(monkeypatch):
    ems = controller(
        monkeypatch,
        output_control={
            "filter_enabled": False,
            "load_deadband_w": 0,
            "target_deadband_w": 0,
            "ramp_enabled": True,
            "ramp_up_w_per_cycle": 200,
            "ramp_down_w_per_cycle": 200,
            "large_import_bypass_w": 600,
            "large_export_bypass_w": 600,
            "bypass_ramp_multiplier": 2.0,
        },
    )
    states = [state(output_limit=300)]

    assert ems.stabilized_total_target(700, states, 1200) == 700
    assert ems.stabilized_total_target(-700, states, 1200) == 300


def test_load_derived_target_uses_current_output_but_not_home_value():
    states = [state(solar=500, output=250)]

    targets, current_total, new_total = calculate_targets_with_config(
        states,
        requested_total=None,
        max_power=800,
        load=100,
    )

    assert current_total == 250
    assert new_total == 350
    assert sum(targets) == 350


def test_multi_device_distribution_skips_unavailable_and_respects_caps():
    states = [
        state(solar=500),
        state(solar=500),
    ]
    devices = [
        device("WR1", max_power=300),
        device("WR2", max_power=800),
    ]
    capabilities = [
        DeviceCapabilities(
            can_charge=True,
            can_discharge=False,
            can_export=False,
            can_ac_charge=False,
            reason="simulated_unavailable",
        ),
        DeviceCapabilities(
            can_charge=True,
            can_discharge=True,
            can_export=True,
            can_ac_charge=False,
            reason="simulated_available",
        ),
    ]

    targets, _, new_total = calculate_targets_with_config(
        states,
        devices=devices,
        capabilities=capabilities,
        requested_total=600,
        max_power=1200,
    )

    assert new_total == 600
    assert targets == [0, 600]
    assert all(target >= 0 for target in targets)
    assert all(target <= dev.max_power for target, dev in zip(targets, devices))


def test_device_caps_limit_total_to_achievable_output():
    states = [
        state(solar=500),
        state(solar=500),
    ]
    devices = [
        device("WR1", max_power=250),
        device("WR2", max_power=800),
    ]

    targets, _, new_total = calculate_targets_with_config(
        states,
        devices=devices,
        requested_total=900,
        max_power=1200,
    )

    assert new_total == 900
    assert targets == [250, 500]
    assert sum(targets) <= new_total
    assert all(target >= 0 for target in targets)


def test_soc_charge_balance_strength_changes_pv_first_distribution():
    states = [
        state(soc=70, solar=500),
        state(soc=50, solar=500),
    ]

    balanced, _, _ = calculate_targets_with_config(
        states,
        requested_total=600,
        PV_CHARGE_BALANCE_ENABLED=True,
        PV_CHARGE_BALANCE_STRENGTH=0.5,
    )
    unbalanced, _, _ = calculate_targets_with_config(
        states,
        requested_total=600,
        PV_CHARGE_BALANCE_ENABLED=False,
    )

    assert unbalanced == [300, 300]
    assert balanced == [450, 150]
    assert all(0 <= target <= 500 for target in balanced)
    assert sum(balanced) == 600


def test_min_soc_prevents_battery_topup_from_protected_device():
    states = [
        state(soc=20, min_soc=20, solar=100),
        state(soc=70, min_soc=20, solar=100),
    ]

    targets, _, _ = calculate_targets_with_config(
        states,
        requested_total=500,
        max_power=800,
    )

    assert targets[0] == 100
    assert targets[1] == 400
    assert sum(targets) == 500
