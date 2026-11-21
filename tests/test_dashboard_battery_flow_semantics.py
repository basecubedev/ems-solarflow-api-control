# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
import json
from types import SimpleNamespace

import pytest

from dashboard.telemetry import build_dashboard_snapshot
from ems.target_control import (
    ControlExplanation,
    ControlLimitExplanation,
    DeviceControlExplanation,
)

pytestmark = [
    pytest.mark.contract,
]


def test_dashboard_backend_keeps_positive_battery_power_for_charging():
    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="WR1")],
        runtime_state=None,
        device_online={"WR1": True},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
    )
    state = SimpleNamespace(
        solar=500,
        output=700,
        pack_out=220,
        pack_in=20,
        soc=64,
    )

    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=[state],
        targets=[700],
        effective_targets=[700],
        allocated_total_w=700,
        effective_total_w=700,
        enabled=True,
        max_total_power=700,
        min_output_limit=35,
    )

    assert snapshot["battery_power_w"] == 200
    assert snapshot["devices"]["WR1"]["battery_power_w"] == 200
    assert snapshot["devices"]["WR1"]["pack_output_w"] == 220
    assert snapshot["devices"]["WR1"]["pack_input_w"] == 20
    assert snapshot["control_explain"] is None


def test_dashboard_backend_keeps_negative_battery_power_for_discharging():
    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="WR1")],
        runtime_state=None,
        device_online={"WR1": True},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
    )
    state = SimpleNamespace(
        solar=1400,
        output=700,
        pack_out=10,
        pack_in=710,
        soc=64,
    )

    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=[state],
        targets=[700],
        effective_targets=[700],
        allocated_total_w=700,
        effective_total_w=700,
        enabled=True,
        max_total_power=700,
        min_output_limit=35,
    )

    assert snapshot["battery_power_w"] == -700
    assert snapshot["devices"]["WR1"]["battery_power_w"] == -700


def test_dashboard_backend_snapshot_keeps_two_device_values_and_totals():
    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="WR1"), SimpleNamespace(name="WR2")],
        runtime_state=None,
        device_online={"WR1": True, "WR2": True},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
    )
    states = [
        SimpleNamespace(
            solar=1400,
            output=700,
            pack_out=710,
            pack_in=10,
            soc=63,
        ),
        SimpleNamespace(
            solar=600,
            output=300,
            pack_out=20,
            pack_in=220,
            soc=58,
        ),
    ]

    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=states,
        targets=[700, 300],
        effective_targets=[700, 300],
        allocated_total_w=1000,
        effective_total_w=1000,
        enabled=True,
        max_total_power=1400,
        min_output_limit=35,
    )

    assert set(snapshot["devices"]) == {"WR1", "WR2"}
    assert snapshot["devices"]["WR1"]["pv_input_w"] == 1400
    assert snapshot["devices"]["WR1"]["output_w"] == 700
    assert snapshot["devices"]["WR1"]["battery_power_w"] == 700
    assert snapshot["devices"]["WR1"]["soc"] == 63
    assert snapshot["devices"]["WR2"]["pv_input_w"] == 600
    assert snapshot["devices"]["WR2"]["output_w"] == 300
    assert snapshot["devices"]["WR2"]["battery_power_w"] == -200
    assert snapshot["devices"]["WR2"]["soc"] == 58
    assert snapshot["pv_total_w"] == 2000
    assert snapshot["inverter_output_w"] == 1000
    assert snapshot["battery_power_w"] == 500
    assert snapshot["average_soc"] == 60.5


def test_dashboard_snapshot_publishes_control_explanation_data():
    explanation = ControlExplanation(
        mode="pv_first",
        requested_total_w=1000,
        effective_target_total_w=1000,
        allocated_target_total_w=1000,
        commanded_total_w=1000,
        devices={
            "WR1": DeviceControlExplanation(
                device="WR1",
                online=True,
                pv_input_w=1400,
                output_w=700,
                soc=63,
                min_soc=15,
                max_soc=100,
                pv_priority_factor=1.2,
                allocated_target_w=700,
                effective_target_w=700,
                decision_reason="pv_first_allocation",
            ),
            "WR2": DeviceControlExplanation(
                device="WR2",
                online=True,
                pv_input_w=600,
                output_w=300,
                soc=58,
                min_soc=15,
                max_soc=100,
                pv_priority_factor=1.0,
                allocated_target_w=300,
                effective_target_w=300,
                decision_reason="pv_first_allocation",
            ),
        },
        limits=[
            ControlLimitExplanation(
                name="pv_surplus_available",
                active=True,
                value=2000,
                reason="total PV can cover the requested output",
            )
        ],
    )
    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="WR1"), SimpleNamespace(name="WR2")],
        runtime_state=None,
        device_online={"WR1": True, "WR2": True},
        commanded_total_w=1000,
        filtered_load_w=0,
        _dashboard_capabilities=[],
        last_control_explanation=explanation,
    )
    states = [
        SimpleNamespace(solar=1400, output=700, pack_out=710, pack_in=10, soc=63),
        SimpleNamespace(solar=600, output=300, pack_out=20, pack_in=220, soc=58),
    ]

    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=states,
        targets=[700, 300],
        effective_targets=[700, 300],
        allocated_total_w=1000,
        effective_total_w=1000,
        enabled=True,
        max_total_power=1400,
        min_output_limit=35,
    )
    payload = json.loads(json.dumps(snapshot))

    assert payload["pv_total_w"] == 2000
    assert payload["devices"]["WR1"]["target_w"] == 700
    assert payload["control_explain"]["mode"] == "pv_first"
    assert payload["control_explain"]["requested_total_w"] == 1000
    assert set(payload["control_explain"]["devices"]) == {"WR1", "WR2"}
    assert payload["control_explain"]["devices"]["WR1"]["pv_input_w"] == 1400
    assert payload["control_explain"]["devices"]["WR1"]["soc"] == 63
    assert (
        payload["control_explain"]["devices"]["WR1"]["pv_priority_factor"]
        == 1.2
    )
    assert payload["control_explain"]["devices"]["WR1"]["allocated_target_w"] == 700
    assert payload["control_explain"]["devices"]["WR2"]["effective_target_w"] == 300
    assert payload["control_explain"]["limits"][0]["name"] == "pv_surplus_available"


def _grid_controller(health):
    return SimpleNamespace(
        devices=[SimpleNamespace(name="WR1")],
        runtime_state=None,
        device_online={"WR1": True},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
        shelly=SimpleNamespace(health=health),
    )


def _snapshot_for(controller):
    return build_dashboard_snapshot(
        controller,
        load_w=240,
        states=[SimpleNamespace(solar=0, output=0, pack_out=0, pack_in=0, soc=50)],
        targets=[0],
        effective_targets=[0],
        allocated_total_w=0,
        effective_total_w=0,
        enabled=True,
        max_total_power=800,
        min_output_limit=35,
    )


def test_grid_power_is_marked_valid_after_a_successful_read():
    controller = _grid_controller(
        SimpleNamespace(success_count=3, stale_used=False)
    )

    assert _snapshot_for(controller)["grid_power_valid"] is True


def test_grid_power_is_marked_invalid_while_a_stale_value_is_used():
    """An unreachable meter keeps publishing its last value, or its initial 0.

    Reported as valid, the statistics would integrate it and claim no grid
    import for an installation whose meter is simply down.
    """

    never_answered = _grid_controller(
        SimpleNamespace(success_count=0, stale_used=False)
    )
    stale = _grid_controller(SimpleNamespace(success_count=5, stale_used=True))

    assert _snapshot_for(never_answered)["grid_power_valid"] is False
    assert _snapshot_for(stale)["grid_power_valid"] is False


def test_grid_power_counts_as_valid_without_health_data():
    """Simulation and replay clients have no health; their readings are real."""

    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="WR1")],
        runtime_state=None,
        device_online={"WR1": True},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
    )

    assert _snapshot_for(controller)["grid_power_valid"] is True


def _two_device_controller(fresh, *, runtime_state=None):
    """Two devices, the second one offline, freshness answered by ``fresh``.

    The names are checked here because the snapshot builder treats a raising
    freshness lookup as "measured": a typo in ``fresh`` would otherwise make a
    test pass without ever asking the question it claims to ask.
    """

    assert set(fresh) == {"WR1", "WR2"}, fresh

    return SimpleNamespace(
        devices=[SimpleNamespace(name="WR1"), SimpleNamespace(name="WR2")],
        runtime_state=runtime_state,
        device_online={"WR1": True, "WR2": False},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
        telemetry_fresh=lambda name: fresh[name],
    )


def _two_device_snapshot(controller):
    state = SimpleNamespace(solar=500, output=300, pack_out=0, pack_in=0, soc=50)

    return build_dashboard_snapshot(
        controller,
        load_w=100,
        states=[state, state],
        targets=[300, 300],
        effective_targets=[300, 300],
        allocated_total_w=600,
        effective_total_w=600,
        enabled=True,
        max_total_power=800,
        min_output_limit=35,
    )


def test_device_power_stays_valid_through_a_single_failed_read():
    """A failed read is not a gap while the cached state is still current.

    The controller keeps publishing that state and the control loop keeps
    regulating on it, so the statistics integrate it over the same window.
    Otherwise one network hiccup would mark the day, the month and the year.
    """

    controller = _two_device_controller({"WR1": True, "WR2": True})
    snapshot = _two_device_snapshot(controller)

    assert snapshot["device_power_valid"] is True
    # The tile still says what happened, which is what the Overview shows.
    assert snapshot["devices"]["WR2"]["online"] is False


def test_device_power_is_invalid_once_telemetry_ages_out():
    """Past the staleness window the last value is no longer a reading.

    Integrating it would let a device that dropped off the network keep
    contributing PV and battery energy for as long as it is gone.
    """

    controller = _two_device_controller({"WR1": True, "WR2": False})
    snapshot = _two_device_snapshot(controller)

    assert snapshot["device_power_valid"] is False
    assert snapshot["grid_power_valid"] is True


def test_a_controller_that_cannot_date_its_telemetry_is_taken_at_its_word():
    """Simulation, replay and test doubles have no timestamps; they measure.

    The same choice the grid reading makes for a client without health data,
    and the same one ``sample_is_measured`` makes for a snapshot without the
    flags: absent evidence is not evidence of a gap.
    """

    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="WR1"), SimpleNamespace(name="WR2")],
        runtime_state=None,
        device_online={"WR1": True, "WR2": False},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
    )

    assert _two_device_snapshot(controller)["device_power_valid"] is True


def test_device_power_is_valid_while_every_device_reports():
    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="WR1")],
        runtime_state=None,
        device_online={"WR1": True},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
    )

    assert _snapshot_for(controller)["device_power_valid"] is True


def test_a_disabled_device_does_not_stop_the_statistics():
    """Switching a unit off for the season is a decision, not a gap.

    An enabled device whose telemetry has aged out halts the figures, because
    its last reading would otherwise be integrated as a measurement. A disabled
    one is not being run at all.
    """

    controller = _two_device_controller(
        {"WR1": True, "WR2": False},
        runtime_state=SimpleNamespace(
            get_device=lambda name, key, default: (
                False if (name, key) == ("WR2", "enabled") else default
            ),
        ),
    )
    snapshot = _two_device_snapshot(controller)

    assert snapshot["devices"]["WR2"]["enabled"] is False
    assert snapshot["device_power_valid"] is True
    # It is still reported as offline, which is what the Overview shows.
    assert snapshot["rules"]["offline_devices"]["active"] is True


def _disabled_second_device():
    return SimpleNamespace(
        get_device=lambda name, key, default: (
            False if (name, key) == ("WR2", "enabled") else default
        ),
    )


def test_a_disabled_device_that_went_quiet_contributes_nothing():
    """The exemption from holding the statistics is paid for by not counting.

    A unit switched off for the season keeps publishing its last reading until
    the EMS restarts. It does not hold the figures -- switching it off was a
    decision -- so its frozen PV would otherwise be integrated as measured
    energy for as long as the process runs.
    """

    controller = _two_device_controller(
        {"WR1": True, "WR2": False}, runtime_state=_disabled_second_device()
    )
    snapshot = _two_device_snapshot(controller)

    assert snapshot["device_power_valid"] is True
    assert snapshot["pv_total_w"] == 500
    assert snapshot["inverter_output_w"] == 300
    # The tile still carries the device's own last values.
    assert snapshot["devices"]["WR2"]["pv_input_w"] == 500


def test_a_disabled_device_that_still_answers_is_still_counted():
    """Excluded from control is not the same as absent: its PV is real."""

    controller = _two_device_controller(
        {"WR1": True, "WR2": True}, runtime_state=_disabled_second_device()
    )
    snapshot = _two_device_snapshot(controller)

    assert snapshot["pv_total_w"] == 1000
    assert snapshot["inverter_output_w"] == 600


def test_an_enabled_device_that_went_quiet_stays_in_the_aggregates():
    """Deliberately the other way round, and the reason is the gate.

    Its sample is dropped whole, so nothing it contributes is ever integrated.
    Leaving the cached value in the totals keeps the Overview showing what the
    control loop is still steering with.
    """

    controller = _two_device_controller({"WR1": True, "WR2": False})
    snapshot = _two_device_snapshot(controller)

    assert snapshot["device_power_valid"] is False
    assert snapshot["pv_total_w"] == 1000


def test_every_validity_flag_the_gate_reads_is_one_the_snapshot_writes():
    """Two lists that have to agree, walked.

    ``sample_is_measured`` treats a flag missing from the snapshot as measured,
    so renaming a key on the publishing side would disable the whole gate in
    silence -- every figure would look measured again. The registry is the
    authority; this test holds the publisher to it.
    """

    from ems.energy_channels import SAMPLE_VALIDITY_FLAGS

    snapshot = _two_device_snapshot(
        _two_device_controller({"WR1": True, "WR2": True})
    )

    missing = [flag for flag in SAMPLE_VALIDITY_FLAGS if flag not in snapshot]

    assert not missing, f"snapshot does not publish {missing}"
    assert all(isinstance(snapshot[flag], bool) for flag in SAMPLE_VALIDITY_FLAGS)


def test_a_freshness_lookup_that_raises_never_breaks_the_snapshot():
    """Building the snapshot is what keeps the whole dashboard alive.

    A controller that answers the freshness question with an exception must not
    take the dashboard down with it, and the sample counts as measured -- the
    same direction as every other missing-evidence fallback here.
    """

    def boom(name):
        raise RuntimeError("no clock for you")

    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="WR1"), SimpleNamespace(name="WR2")],
        runtime_state=None,
        device_online={"WR1": True, "WR2": True},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
        telemetry_fresh=boom,
    )

    assert _two_device_snapshot(controller)["device_power_valid"] is True


def test_an_inverter_without_packs_is_left_out_of_the_average_soc():
    """A SolarFlow 800 beside an 800 Pro reports 0 %: counting it halves the total."""

    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="Pro"), SimpleNamespace(name="SF800")],
        runtime_state=None,
        device_online={"Pro": True, "SF800": True},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
    )
    states = [
        SimpleNamespace(solar=400, output=300, pack_out=100, pack_in=0, soc=80, pack_num=2),
        SimpleNamespace(solar=600, output=600, pack_out=0, pack_in=0, soc=0, pack_num=0),
    ]

    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=states,
        targets=[300, 600],
        effective_targets=[300, 600],
        allocated_total_w=900,
        effective_total_w=900,
        enabled=True,
        max_total_power=1600,
        min_output_limit=35,
    )

    assert snapshot["average_soc"] == 80
    assert snapshot["devices"]["Pro"]["soc_reported"] is True
    assert snapshot["devices"]["SF800"]["soc_reported"] is False
    assert snapshot["pv_total_w"] == 1000
    assert snapshot["inverter_output_w"] == 900


def test_a_device_whose_packs_were_never_reported_still_counts_in_the_average_soc():
    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="WR1"), SimpleNamespace(name="WR2")],
        runtime_state=None,
        device_online={"WR1": True, "WR2": True},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
    )
    states = [
        SimpleNamespace(solar=0, output=0, pack_out=0, pack_in=0, soc=80, pack_num=1),
        SimpleNamespace(solar=0, output=0, pack_out=0, pack_in=0, soc=40, pack_num=None),
    ]

    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=states,
        targets=[0, 0],
        effective_targets=[0, 0],
        allocated_total_w=0,
        effective_total_w=0,
        enabled=True,
        max_total_power=1600,
        min_output_limit=35,
    )

    assert snapshot["average_soc"] == 60
    assert snapshot["devices"]["WR2"]["soc_reported"] is True
