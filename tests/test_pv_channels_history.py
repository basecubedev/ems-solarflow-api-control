# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every device type records the PV inputs it reports, and only those (#382).

The history wrote ``solar1``-``solar4`` for every device, so a device whose
report carries fewer recorded the rest as 0 and an E3/DC four inputs the EMS
does not read, while an inverter added through the MQTT mapping could not map a
single string. A state now says which inputs its report carried; the history
and the dashboard leave out the ones it did not, and keep a reported 0. An
external device records only the values it still sends.
"""

from types import SimpleNamespace

import pytest

from dashboard.telemetry import _state_telemetry_fields
from ems.clients import parse_device, zero_device_state
from ems.e3dc_runtime import e3dc_device_state
from ems.history.influx_writer import build_telemetry_lines
from ems.zendure_mqtt.external_catalog import EMS_SOLARFLOW

pytestmark = [
    pytest.mark.contract,
]


def _report(**properties):
    return {"properties": {"electricLevel": 50, "solarInputPower": 300, **properties}}


def _device_line(state, name="WR1"):
    lines = build_telemetry_lines([SimpleNamespace(name=name)], [state], {name: True}, 0, timestamp_ns=1)
    return next(line for line in lines if line.startswith("zendure_device"))


def _fields(line):
    return dict(item.split("=", 1) for item in line.split(" ")[1].split(","))


def test_a_report_says_which_pv_inputs_it_carried():
    assert parse_device(_report(solarPower1=180, solarPower2=0)).pv_inputs_reported == (True, True, False, False)
    assert parse_device(_report()).pv_inputs_reported == (False, False, False, False)


def test_the_history_records_the_inputs_a_device_reports_and_a_reported_zero():
    fields = _fields(_device_line(parse_device(_report(solarPower1=180, solarPower2=0))))

    assert (fields["solar1"], fields["solar2"]) == ("180.0", "0.0")
    assert "solar3" not in fields and "solar4" not in fields
    assert fields["solar"] == "300.0"


def test_a_state_that_does_not_say_records_all_four_as_before():
    state = zero_device_state()

    assert state.pv_inputs_reported is None
    assert {"solar1", "solar2", "solar3", "solar4"} <= set(_fields(_device_line(state)))


def test_the_dashboard_leaves_an_input_the_device_does_not_report_empty():
    fields = _state_telemetry_fields(parse_device(_report(solarPower1=180, solarPower2=0)))

    assert fields["pv_inputs_w"] == [180.0, 0.0, None, None]
    assert _state_telemetry_fields(zero_device_state())["pv_inputs_w"] == [0.0, 0.0, 0.0, 0.0]


def test_an_e3dc_records_no_pv_input_it_does_not_have():
    state = e3dc_device_state(None)
    lines = build_telemetry_lines(
        [], [], {}, 0, timestamp_ns=1,
        read_only_tiles=[{"name": "E3DC", "source": "e3dc", "online": True, "state": state}],
    )

    assert state.pv_inputs_reported == (False, False, False, False)
    assert not any(f"solar{n}=" in lines[0] for n in range(1, 5))


def test_an_external_device_maps_its_strings_beside_the_total():
    readings = EMS_SOLARFLOW.readings("state", '{"inverterPower": 400, "solarPower": 420, "solarPower1": 220, "solarPower2": 200}')
    state = parse_device({"properties": readings})

    assert (state.solar, state.solar1, state.solar2) == (420, 220, 200)
    assert state.pv_inputs_reported == (True, True, False, False)
    assert EMS_SOLARFLOW.readings("solarPower2", "-5") is None


# --- an external device that stops sending one value ----------------------------------


def test_a_string_an_external_device_stopped_sending_leaves_its_tile_and_the_rest_stays(monkeypatch):
    from dashboard.telemetry import build_dashboard_snapshot, telemetry_only_device_tiles
    from ems.zendure_mqtt.snapshot import ZendureMqttAggregator

    clock = {"now": 1000.0}
    aggregator = ZendureMqttAggregator(monotonic=lambda: clock["now"])
    aggregator.observe("ems-solarflow/tsun/state", b'{"inverterPower": 500, "solarPower": 520, "solarPower1": 260, "solarPower2": 260}')
    clock["now"] += 90
    aggregator.observe("ems-solarflow/tsun/state", b'{"inverterPower": 480, "solarPower": 500, "solarPower2": 250}')
    snapshots = {snap.device_id: snap for snap in aggregator.snapshots()}

    class Runtime:
        stale_after_seconds = 60.0

        def device_summaries(self):
            return [{"name": "TSUN", "identifier": "ems-solarflow/tsun", "status": "online"}]

        def snapshots(self):
            return snapshots

    controller = SimpleNamespace(
        devices=[], runtime_state=None, device_online={}, commanded_total_w=0, filtered_load_w=0,
        _dashboard_capabilities=[], zendure_mqtt_runtime=Runtime(),
    )
    import dashboard.telemetry as telemetry

    monkeypatch.setattr(telemetry.time, "monotonic", lambda: clock["now"])
    tile = build_dashboard_snapshot(
        controller, load_w=0, states=[], targets=[], effective_targets=[], allocated_total_w=0,
        effective_total_w=0, enabled=True, max_total_power=1600, min_output_limit=35,
    )["devices"]["TSUN"]
    [history] = telemetry_only_device_tiles(controller)

    assert tile["output_w"] == 480
    assert tile["pv_inputs_w"] == [None, 250.0, None, None]
    line = build_telemetry_lines([], [], {}, None, timestamp_ns=1, read_only_tiles=[history])[0]
    assert "solar2=250" in line and "solar1=" not in line


def test_an_external_device_whose_required_value_went_stale_is_offline_not_a_measured_zero(monkeypatch):
    """A single-topic bridge that keeps sending its charge level but stopped
    sending its inverter power must not read as 0 W measured."""

    from dashboard.telemetry import build_dashboard_snapshot, telemetry_only_device_tiles
    from ems.zendure_mqtt.snapshot import ZendureMqttAggregator
    import dashboard.telemetry as telemetry

    clock = {"now": 1000.0}
    aggregator = ZendureMqttAggregator(monotonic=lambda: clock["now"])
    aggregator.observe("ems-solarflow/garage/inverterPower", b"400")
    clock["now"] += 90
    aggregator.observe("ems-solarflow/garage/batterySoc", b"55")
    snapshots = {snap.device_id: snap for snap in aggregator.snapshots()}

    class Runtime:
        stale_after_seconds = 60.0

        def device_summaries(self):
            return [{"name": "Garage", "identifier": "ems-solarflow/garage", "status": "online"}]

        def snapshots(self):
            return snapshots

    controller = SimpleNamespace(
        devices=[], runtime_state=None, device_online={}, commanded_total_w=0, filtered_load_w=0,
        _dashboard_capabilities=[], zendure_mqtt_runtime=Runtime(),
    )
    monkeypatch.setattr(telemetry.time, "monotonic", lambda: clock["now"])
    snapshot = build_dashboard_snapshot(
        controller, load_w=0, states=[], targets=[], effective_targets=[], allocated_total_w=0,
        effective_total_w=0, enabled=True, max_total_power=1600, min_output_limit=35,
    )
    [history] = telemetry_only_device_tiles(controller)

    assert snapshot["devices"]["Garage"]["online"] is False
    assert snapshot["device_power_valid"] is False
    assert build_telemetry_lines([], [], {}, None, timestamp_ns=1, read_only_tiles=[history]) == [
        "zendure_device,device=Garage,source=read_only available=false 1"
    ]


def test_an_external_device_records_only_the_values_it_sends():
    from dashboard.telemetry import EXTERNAL_METRIC_FIELDS

    metrics = {metric for key in EMS_SOLARFLOW.keys.values() for metric in key.metrics}
    assert set(EXTERNAL_METRIC_FIELDS) == metrics

    state = parse_device({"properties": {"outputHomePower": 400, "solarPower2": 250}})
    tile = {
        "name": "TSUN", "source": "read_only", "online": True, "state": state,
        "history_fields": ("output", "solar2"),
    }
    [line] = build_telemetry_lines([], [], {}, None, timestamp_ns=1, read_only_tiles=[tile])
    assert line == "zendure_device,device=TSUN,source=read_only available=true,output=400.0,solar2=250.0 1"


def test_strings_without_a_total_are_the_device_s_pv():
    from dashboard.telemetry import _with_pv_total

    assert _with_pv_total({"outputHomePower": 400, "solarPower1": 220, "solarPower2": 200})["solarInputPower"] == 420
    assert _with_pv_total({"solarInputPower": 430, "solarPower1": 220})["solarInputPower"] == 430
    assert "solarInputPower" not in _with_pv_total({"outputHomePower": 400})
