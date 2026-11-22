# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An inverter an OpenDTU reads, taken from its web API, shown and never controlled.

#390: an OpenDTU reached the EMS only through the MQTT mapping, which needs a
broker. Its ``/api/livedata/status?inv=<serial>`` answers one inverter in one
request. The entry is read once per control cycle, sends only ``GET``, counts
as offline when the OpenDTU does not reach the inverter or its last answer is
older than ``max_age_seconds``, and writes the history under
``source=read_only`` with the fields a series written through the mapping
already has (output, solar, solar1, solar2, output_limit).
"""

from types import SimpleNamespace

import pytest
import requests
import threading

from dashboard.telemetry import build_dashboard_snapshot
from ems import config as cfg
from ems.history.influx_writer import build_telemetry_lines
from ems.opendtu_http import (
    OpenDtuAnswerError,
    OpenDtuDevice,
    OpenDtuDeviceRuntime,
    build_opendtu_device_runtime,
    parse_inverter,
    select_inverter,
)
from ems.read_only_devices import (
    READ_ONLY_DEVICE_TYPES,
    combine_read_only_devices,
    is_read_only_device_config,
    read_only_runtime_builders,
)
from ems.zendure_mqtt.config_entries import has_runtime_control_device

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
]

ZENDURE = {"name": "WR1", "ip": "192.0.2.1", "sn": "SN1", "max_power": 800}
SERIAL = "116491234567"


def _entry(**values):
    return {"name": "TSUN OpenDTU", "type": "opendtu_http", "ip": "192.0.2.70", "serial": SERIAL, **values}


def _power(value):
    return {"Power": {"v": value, "u": "W", "d": 1}}


def _inverter(serial=SERIAL, *, reachable=True, data_age=3, ac=412.5, dc=(220.4, 205.1), limit=600):
    return {
        "serial": serial,
        "name": "TSUN TSOL-M800(DE)",
        "order": 0,
        "data_age": data_age,
        "data_age_ms": data_age * 1000,
        "poll_enabled": True,
        "reachable": reachable,
        "producing": ac > 0,
        "limit_relative": 75,
        "limit_absolute": limit,
        "AC": {"0": {**_power(ac), "Voltage": {"v": 231.2, "u": "V", "d": 1}}},
        "DC": {str(index): {"name": {"u": f"Panel {index + 1}"}, **_power(value)} for index, value in enumerate(dc)},
        "INV": {"0": {"Temperature": {"v": 31.0, "u": "°C", "d": 1}}},
        "events": 0,
    }


def _answer(*inverters):
    return {"inverters": list(inverters), "total": {"Power": {"v": 0, "u": "W", "d": 0}}, "hints": {}}


class _Response:
    def __init__(self, payload=None, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return self.payload


class _Session:
    """Answers each GET with the next item; records every request, and refuses all but GET."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append(("GET", url, params, timeout))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def close(self):
        self.closed = True

    def __getattr__(self, name):
        raise AssertionError(f"the OpenDTU reader sent {name}")


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _device(session, clock=None, **values):
    return OpenDtuDevice(cfg.opendtu_http_device_settings(_entry(**values)), session=session, clock=clock or _Clock())


# --- never controlled -----------------------------------------------------------


def test_an_opendtu_entry_is_read_only_and_never_a_control_device():
    assert is_read_only_device_config(_entry()) is True
    assert is_read_only_device_config({"type": " OpenDTU_HTTP "}) is True
    assert cfg.http_control_device_configs([ZENDURE, _entry()]) == [ZENDURE]
    grouped = cfg.config_control_devices_by_gate({"devices": [ZENDURE, _entry()]})
    assert [item["name"] for items in grouped.values() for item in items] == ["WR1"]
    assert has_runtime_control_device({"devices": [_entry()]}) is False


def test_the_config_upgrade_invents_no_zendure_values_for_an_opendtu():
    upgraded = cfg._merge_template_upgrade_view({"devices": []}, {"devices": [_entry()]}, {"smart_mode": 1, "min_soc": 15})
    assert upgraded["devices"][0] == _entry()


# --- settings -------------------------------------------------------------------


def test_the_settings_need_a_name_and_an_address_and_default_the_rest():
    assert cfg.opendtu_http_device_settings(_entry(serial=None)) == {
        "name": "TSUN OpenDTU",
        "host": "192.0.2.70",
        "port": 80,
        "serial": None,
        "max_age_seconds": 60,
    }
    assert cfg.opendtu_http_device_settings(_entry(port=8080, max_age_seconds=30))["port"] == 8080
    assert cfg.opendtu_http_device_settings(_entry(ip="opendtu.local"))["host"] == "opendtu.local"


@pytest.mark.parametrize(
    "values, message",
    [
        ({"name": ""}, "requires a name"),
        ({"ip": " "}, "requires ip"),
        ({"ip": "opendtu.local:8080"}, "without a scheme, port or path"),
        ({"ip": "http://192.0.2.70"}, "without a scheme, port or path"),
        ({"ip": "192.0.2.70/x?"}, "without a scheme, port or path"),
        ({"ip": "fe80::1"}, "without a scheme, port or path"),
        ({"serial": "12/34"}, "serial must be"),
        ({"serial": "x" * 21}, "serial must be"),
        ({"port": 0}, "port must be in"),
        ({"max_age_seconds": 1}, "max_age_seconds must be in"),
        ({"max_age_seconds": True}, "not a boolean"),
    ],
)
def test_an_invalid_entry_is_refused(values, message):
    with pytest.raises(ValueError, match=message):
        cfg.opendtu_http_device_settings(_entry(**values))


def test_an_invalid_entry_is_refused_at_startup_and_a_disabled_one_builds_nothing():
    with pytest.raises(ValueError, match="port must be in"):
        build_opendtu_device_runtime([_entry(port=70000)])
    assert build_opendtu_device_runtime([ZENDURE, _entry(enabled=False)]) is None


# --- the answer -----------------------------------------------------------------


def test_an_answer_becomes_output_strings_their_sum_and_the_limit():
    reading = parse_inverter(select_inverter(_answer(_inverter()), SERIAL))

    assert reading.output_w == 412.5
    assert reading.pv_inputs_w == (220.4, 205.1)
    assert reading.solar_w == pytest.approx(425.5)
    assert reading.limit_w == 600
    assert (reading.reachable, reading.data_age_seconds) == (True, 3.0)


def test_the_inverter_is_chosen_by_its_serial_or_as_the_only_one():
    other = _inverter("114100000001", ac=99)
    assert select_inverter(_answer(other, _inverter()), SERIAL.upper())["serial"] == SERIAL
    assert select_inverter(_answer(_inverter()), None)["serial"] == SERIAL
    with pytest.raises(OpenDtuAnswerError, match="lists 2 inverters; set serial"):
        select_inverter(_answer(other, _inverter()), None)
    with pytest.raises(OpenDtuAnswerError, match="no inverter 999999"):
        select_inverter(_answer(_inverter()), "999999")


def test_a_summary_without_channels_is_no_reading():
    """The answer without ``inv``, as the reporter in #380 got it, lists no power at all."""

    summary = {key: value for key, value in _inverter().items() if key not in ("AC", "DC", "INV")}
    with pytest.raises(OpenDtuAnswerError, match="no AC power"):
        parse_inverter(summary)


def test_an_unknown_limit_or_a_string_without_power_is_left_out_and_the_others_keep_their_number():
    from ems.opendtu_http import history_fields

    inverter = _inverter(limit=-1, dc=(220.4, 205.1, 180.0))
    inverter["DC"]["1"]["Power"]["v"] = None
    reading = parse_inverter(inverter)
    assert reading.limit_w is None
    assert reading.pv_inputs_w == (220.4, None, 180.0)
    assert reading.solar_w == pytest.approx(400.4)
    assert history_fields(reading) == ("output", "solar", "solar1", "solar3")


# --- reading it -------------------------------------------------------------------


def test_one_get_per_read_names_the_inverter_and_waits_briefly():
    session = _Session(_Response(_answer(_inverter())))
    device = _device(session)

    assert device.poll_once() == 5.0

    assert session.requests == [("GET", "http://192.0.2.70:80/api/livedata/status", {"inv": SERIAL}, (1.0, 2.0))]
    assert device.status().online is True
    assert device.status().error is None


def test_without_a_serial_the_only_inverter_is_asked_for_by_the_serial_the_list_names():
    """The list without ``inv`` carries no channels on current firmware (#380's sample)."""

    summary = {key: value for key, value in _inverter().items() if key not in ("AC", "DC", "INV")}
    session = _Session(_Response(_answer(summary)), _Response(_answer(_inverter())))
    device = _device(session, serial=None)

    device.poll_once()
    device.poll_once()

    assert [request[2] for request in session.requests] == [None, {"inv": SERIAL}, {"inv": SERIAL}]
    assert device.status().online is True


def test_without_a_serial_a_list_that_carries_channels_is_read_as_it_is():
    session = _Session(_Response(_answer(_inverter())))
    device = _device(session, serial=None)

    device.poll_once()

    assert [request[2] for request in session.requests] == [None]
    assert device.status().reading.output_w == 412.5


def test_a_serial_learned_from_the_list_is_asked_for_again_after_a_failed_read():
    summary = {key: value for key, value in _inverter().items() if key not in ("AC", "DC", "INV")}
    session = _Session(
        _Response(_answer(summary)), requests.ConnectionError("gone"), _Response(_answer(summary)), _Response(_answer(_inverter()))
    )
    device = _device(session, serial=None)

    device.poll_once()
    device.poll_once()

    assert [request[2] for request in session.requests] == [None, {"inv": SERIAL}, None, {"inv": SERIAL}]
    assert device.status().online is True


def test_an_inverter_the_opendtu_does_not_reach_is_offline_and_its_zero_measured():
    device = _device(_Session(_Response(_answer(_inverter(reachable=False)))))
    device.poll_once()
    status = device.status()

    assert (status.online, status.reading, status.measured_zero) == (False, None, True)
    assert "does not reach" in status.error


def test_an_inverter_lost_while_it_produced_is_unmeasured_not_a_measured_zero():
    """reachable=false also means a radio link gone in daylight, while it feeds."""

    session = _Session(_Response(_answer(_inverter(ac=412.5))), _Response(_answer(_inverter(reachable=False, ac=412.5))))
    device = _device(session)
    device.poll_once()
    device.poll_once()

    assert (device.status().online, device.status().measured_zero) == (False, False)


def test_an_inverter_lost_at_dusk_is_a_measured_zero():
    session = _Session(_Response(_answer(_inverter(ac=12.0))), _Response(_answer(_inverter(reachable=False, ac=12.0))))
    device = _device(session)
    device.poll_once()
    device.poll_once()

    assert (device.status().online, device.status().measured_zero) == (False, True)


def test_a_reader_whose_read_raises_logs_it_and_goes_on():
    """Interleaving: the first poll raises past poll_once's own handling, the
    thread logs it and waits; close() then ends the wait at once."""

    raised = threading.Event()
    device = _device(_Session(_Response(_answer(_inverter()))))

    def boom():
        raised.set()
        raise RuntimeError("unexpected")

    device.poll_once = boom
    device.start()
    try:
        assert raised.wait(timeout=10)
    finally:
        device.close()
    assert not device._thread.is_alive()


def test_an_answer_heard_long_ago_is_offline_and_unmeasured():
    device = _device(_Session(_Response(_answer(_inverter(data_age=61)))))
    device.poll_once()
    status = device.status()

    assert (status.online, status.reading, status.measured_zero) == (False, None, False)
    assert "61.0 s old" in status.error


def test_a_failed_read_backs_off_and_a_result_nobody_renewed_goes_stale():
    clock = _Clock()
    session = _Session(requests.ConnectionError("refused"), requests.ConnectionError("refused"), _Response(_answer(_inverter())))
    device = _device(session, clock)

    assert [device.poll_once(), device.poll_once(), device.poll_once()] == [1, 2, 5.0]
    assert device.status().online is True

    clock.now += 31
    status = device.status()
    assert (status.online, status.measured_zero) == (False, False)


def test_a_refusal_names_the_setting_that_allows_reading():
    device = _device(_Session(_Response(status=401)))
    device.poll_once()
    assert "allow read-only access" in device.status().error


def test_the_reader_runs_on_its_own_thread_until_it_is_closed():
    """Interleaving: the control loop's refresh starts the reader and returns at
    once; the reader's first GET happens on its thread, which close() ends."""

    asked = threading.Event()

    class Session(_Session):
        def get(self, url, params=None, timeout=None):
            answer = super().get(url, params=params, timeout=timeout)
            asked.set()
            return answer

    runtime = OpenDtuDeviceRuntime([_device(Session(_Response(_answer(_inverter()))))])
    runtime.refresh()
    try:
        assert asked.wait(timeout=10)
    finally:
        runtime.close()

    assert not runtime._devices[0]._thread.is_alive()


# --- the tile, the dashboard and the history ---------------------------------------


def _runtime(*answers):
    device = _device(_Session(*answers))
    device.poll_once()
    return OpenDtuDeviceRuntime([device])


def test_the_tile_is_a_pv_inverter_without_a_battery():
    [tile] = _runtime(_Response(_answer(_inverter()))).tiles()

    assert tile["online"] is True
    assert tile["source"] == "read_only"
    assert tile["hardware"] == {"pv": True, "battery": False}
    assert tile["soc_reported"] is False
    assert (tile["state"].output, tile["state"].solar1, tile["state"].solar2, tile["state"].solar3) == (412.5, 220.4, 205.1, 0)
    assert tile["fields"]["pv_inputs_w"] == [220.4, 205.1]
    assert tile["history_fields"] == ("output", "solar", "solar1", "solar2", "output_limit")


def test_an_offline_tile_is_zero_so_no_stale_power_reaches_a_total():
    [tile] = _runtime(_Response(_answer(_inverter(reachable=False, ac=300)))).tiles()

    assert tile["online"] is False
    assert tile["measured_offline"] is True
    assert (tile["state"].output, tile["state"].solar) == (0, 0)
    assert "history_fields" not in tile


def test_the_history_goes_on_under_the_mapping_s_name_and_fields():
    lines = build_telemetry_lines(
        [SimpleNamespace(name="WR1")],
        [SimpleNamespace(solar=0, output=100, pack_out=0, pack_in=0, soc=50)],
        {"WR1": True},
        200,
        timestamp_ns=1,
        read_only_tiles=_runtime(_Response(_answer(_inverter()))).tiles(),
    )

    [line] = [line for line in lines if "device=TSUN\\ OpenDTU" in line]
    assert line == (
        "zendure_device,device=TSUN\\ OpenDTU,source=read_only "
        "available=true,output=412.5,output_limit=600.0,solar=425.5,solar1=220.4,solar2=205.1 1"
    )
    meter = next(line for line in lines if line.startswith("shelly_meter"))
    assert "house_load=712.5" in meter


def test_an_offline_inverter_writes_only_that_it_is_unavailable():
    lines = build_telemetry_lines([], [], {}, None, timestamp_ns=1, read_only_tiles=_runtime(requests.ConnectionError("x")).tiles())
    assert lines == ["zendure_device,device=TSUN\\ OpenDTU,source=read_only available=false 1"]


def _snapshot(runtime):
    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="WR1")],
        runtime_state=None,
        device_online={"WR1": True},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
        zendure_mqtt_runtime=None,
        read_only_devices=runtime,
    )
    return build_dashboard_snapshot(
        controller,
        load_w=100,
        states=[SimpleNamespace(solar=400, output=300, pack_out=110, pack_in=10, soc=60)],
        targets=[300],
        effective_targets=[300],
        allocated_total_w=300,
        effective_total_w=300,
        enabled=True,
        max_total_power=1600,
        min_output_limit=35,
    )


def test_the_dashboard_counts_the_inverter_and_no_state_of_charge():
    snapshot = _snapshot(_runtime(_Response(_answer(_inverter()))))

    tile = snapshot["devices"]["TSUN OpenDTU"]
    assert (tile["read_only"], tile["online"], tile["soc_reported"]) == (True, True, False)
    assert tile["pv_inputs_w"] == [220.4, 205.1]
    assert tile["output_limit_w"] == 600
    assert tile["output_limit_applicable"] is True
    assert snapshot["pv_total_w"] == pytest.approx(400 + 425.5)
    assert snapshot["inverter_output_w"] == pytest.approx(300 + 412.5)
    assert snapshot["average_soc"] == 60.0


def test_a_night_with_the_inverter_off_keeps_the_energy_statistics_measured():
    """Hoymiles inverters switch off at night; their 0 W is a reading, not a gap."""

    night = _snapshot(_runtime(_Response(_answer(_inverter(reachable=False)))))
    failed = _snapshot(_runtime(requests.ConnectionError("x")))

    assert night["devices"]["TSUN OpenDTU"]["online"] is False
    assert night["device_power_valid"] is True
    assert failed["device_power_valid"] is False


# --- several kinds at once ------------------------------------------------------------


def test_a_kind_that_fails_to_refresh_leaves_the_others_read():
    class Broken:
        names = ["E3DC"]

        def refresh(self):
            raise RuntimeError("modbus gone")

        def tiles(self):
            return [{"name": "E3DC"}]

    device = _device(_Session(_Response(_answer(_inverter()))))
    device.poll_once()
    opendtu = OpenDtuDeviceRuntime([device])
    combined = combine_read_only_devices(Broken(), None, opendtu)

    combined.refresh()
    combined.close()

    assert combined.names == ["E3DC", "TSUN OpenDTU"]
    assert [tile["name"] for tile in combined.tiles()] == ["E3DC", "TSUN OpenDTU"]
    assert combined.tiles()[1]["online"] is True
    assert combine_read_only_devices(None, None) is None


# --- diagnostics ---------------------------------------------------------------------


def test_diagnostics_name_an_opendtu_entry_read_only_or_invalid():
    from ems.diagnostics import diagnose_config_plausibility

    checks = []
    diagnose_config_plausibility(checks, None, {"system": {}, "devices": [ZENDURE, _entry(), _entry(name="Bad", port=0)]})

    by_code = {check["code"]: check for check in checks}
    assert by_code["opendtu_http_device_read_only"]["level"] == "ok"
    assert by_code["opendtu_http_device_invalid"]["level"] == "error"
    assert "devices.2" in by_code["opendtu_http_device_invalid"]["message"]


def test_a_hardware_diagnosis_reads_an_opendtu_and_never_takes_it_for_a_zendure(monkeypatch):
    import ems.opendtu_http as opendtu
    from ems.diagnostics import diagnose_hardware

    monkeypatch.setattr(opendtu.requests, "Session", lambda: _Session(_Response(_answer(_inverter(reachable=False)))))
    checks = []
    diagnose_hardware(checks, {"grid_meter": {"type": "none"}, "devices": [_entry(sn="SN-LEFT-OVER")]})

    codes = {check["code"] for check in checks}
    assert "opendtu_http_inverter_unreachable" in codes
    assert not any(code.startswith("zendure_") for code in codes)


# --- every read-only type has each of its parts ----------------------------------------


def test_every_read_only_type_is_built_probed_checked_named_and_drawn():
    from pathlib import Path

    from ems.config_init import READ_ONLY_DEVICE_LABELS
    from ems.diagnostics import READ_ONLY_HARDWARE_PROBES, diagnose_config_plausibility

    assert set(read_only_runtime_builders(None)) == set(READ_ONLY_DEVICE_TYPES)
    assert set(READ_ONLY_HARDWARE_PROBES) == set(READ_ONLY_DEVICE_TYPES)
    assert set(READ_ONLY_DEVICE_LABELS) == set(READ_ONLY_DEVICE_TYPES)
    editor = (Path(__file__).resolve().parents[1] / "frontend/admin-modern/src/widgets/DevicesEditor.svelte").read_text(encoding="utf-8")
    for kind in READ_ONLY_DEVICE_TYPES:
        checks = []
        diagnose_config_plausibility(checks, None, {"system": {}, "devices": [ZENDURE, {"name": "R", "type": kind, "ip": "192.0.2.9"}]})
        assert f"{kind}_device_read_only" in {check["code"] for check in checks}, kind
        assert f'kind === "{kind}"' in editor, kind
