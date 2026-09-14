# SPDX-License-Identifier: AGPL-3.0-or-later
"""The measured charge power, and the four places that read it.

Charging is the only direction the EMS commands that produces no ``output``.
Every consumer that derives something from summed outputs is therefore wrong
about a charging device unless it subtracts the draw again, which is what these
tests pin down.
"""

from types import SimpleNamespace

import pytest

from dashboard.telemetry import build_dashboard_snapshot
from ems.clients import parse_device, zero_device_state
from ems.history.influx_provider import INFLUX_SERIES
from ems.history.influx_writer import build_telemetry_lines
from ems.history.provider import SERIES_CATALOG, SERIES_DEVICE_FIELD
from ems.power_direction import derive_house_load_w

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
    pytest.mark.simulation,
]


def _state(output=0, grid_input=0, **extra):
    return SimpleNamespace(
        solar=0,
        output=output,
        grid_input=grid_input,
        pack_in=0,
        pack_out=0,
        soc=50,
        output_limit=0,
        **extra,
    )


def _controller(names):
    return SimpleNamespace(
        devices=[SimpleNamespace(name=name) for name in names],
        runtime_state=None,
        device_online={name: True for name in names},
        _dashboard_capabilities=[],
        commanded_total_w=0,
        filtered_load_w=0,
        last_control_explanation=None,
    )


def test_the_measured_charge_power_is_read_from_telemetry():
    """Both transports build DeviceState through parse_device, so one read covers both."""

    state = parse_device({"properties": {"gridInputPower": 600, "inputLimit": 800}})

    assert state.grid_input == 600
    # What was asked for and what flows are different quantities, kept apart.
    assert state.input_limit_w == 800
    assert zero_device_state().grid_input == 0


def test_house_load_does_not_bill_the_household_for_a_charging_device():
    """The meter cannot tell a charging device from an appliance; the EMS can.

    900 W leaves one inverter, 800 W is drawn back in by a charging device, and
    the meter nets that to 100 W of import. Summing outputs alone reports a
    1000 W household; the house actually draws 200 W.
    """

    assert derive_house_load_w(400, 600) == 1000
    assert derive_house_load_w(900, 100, 800) == 200
    # Never negative, as before.
    assert derive_house_load_w(0, -900, 800) == 0


def test_the_dashboard_snapshot_carries_the_charge_and_corrects_the_home_load():
    controller = _controller(["EXPORTER", "CHARGER"])
    states = [_state(output=900), _state(grid_input=800)]

    snapshot = build_dashboard_snapshot(
        controller,
        0,
        states,
        [900, -800],
        [900, -800],
        100,
        100,
        enabled=True,
        max_total_power=1600,
        min_output_limit=35,
    )
    snapshot_grid = build_dashboard_snapshot(
        controller,
        100,
        states,
        [900, -800],
        [900, -800],
        100,
        100,
        enabled=True,
        max_total_power=1600,
        min_output_limit=35,
    )

    assert snapshot["devices"]["CHARGER"]["ac_charge_w"] == 800
    assert snapshot["devices"]["CHARGER"]["output_w"] == 0
    assert snapshot["devices"]["EXPORTER"]["ac_charge_w"] == 0
    assert snapshot["inverter_charge_w"] == 800
    assert snapshot["inverter_output_w"] == 900
    assert snapshot_grid["home_load_w"] == 200


def test_the_influx_writer_records_the_charge_and_uses_the_same_house_load():
    devices = [SimpleNamespace(name="EXPORTER"), SimpleNamespace(name="CHARGER")]
    states = [_state(output=900), _state(grid_input=800)]

    lines = build_telemetry_lines(
        devices, states, {"EXPORTER": True, "CHARGER": True}, 100, timestamp_ns=1
    )
    charger = next(line for line in lines if "device=CHARGER" in line)
    meter = next(line for line in lines if line.startswith("shelly_meter"))

    assert "grid_input=800" in charger
    assert "house_load=200" in meter


def test_both_history_catalogs_offer_the_same_series():
    """A series present in one provider and absent from the other silently
    disappears when the operator switches analytics source."""

    assert sorted(SERIES_CATALOG) == sorted(INFLUX_SERIES)
    assert "ac_charge" in SERIES_CATALOG
    assert SERIES_DEVICE_FIELD["ac_charge"] == "ac_charge_w"
