# SPDX-License-Identifier: AGPL-3.0-or-later
"""The E3/DC grid meter and the session it reads through.

The control loop drives every read: the session performs one Modbus request
per cycle, whichever role asks first, and backs off without blocking the loop
while the device is gone. Reads run against the probe's loopback E3/DC, so
framing, offset detection and the word-swapped registers are exercised without
hardware. Time is an injected clock; nothing waits on the wall clock.
"""

import importlib.util
import os
import re
import struct

import pytest

from ems import config as cfg
from ems import e3dc_modbus as modbus
from ems.clients import E3dcModbusGridMeterClient, create_grid_meter_client
from ems.e3dc_runtime import E3dcModbusSession, E3dcSessions

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
]

_PROBE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "scripts", "e3dc_modbus_probe.py"
)
_spec = importlib.util.spec_from_file_location("e3dc_modbus_probe_for_meter", _PROBE_PATH)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class RecordingClient(modbus.ReadOnlyModbusTcpClient):
    """The real client, recording every read it puts on the wire."""

    reads = []

    def read_registers(self, address, count, function_code=3, unit_id=None):
        RecordingClient.reads.append((address, count, function_code))
        return super().read_registers(address, count, function_code, unit_id)


class ScriptedClient:
    """A Modbus client whose reads answer from a script, for failure shapes."""

    def __init__(self, host, port, unit_id=1, timeout=3.0):
        self.reads = 0
        ScriptedClient.instance = self

    def connect(self):
        pass

    def close(self):
        pass

    def read_registers(self, address, count, function_code=3, unit_id=None):
        self.reads += 1
        return type(self).answer(self)


def _put_int32(words, manual_address, value, offset=-1):
    raw = value & 0xFFFFFFFF
    words[manual_address + offset] = raw & 0xFFFF
    words[manual_address + offset + 1] = (raw >> 16) & 0xFFFF


@pytest.fixture
def device():
    with probe._LoopbackE3dcServer(probe.build_self_test_words()) as server:
        yield server


def _session(server, clock=None, **kwargs):
    return E3dcModbusSession(
        "127.0.0.1", port=server.port, unit_id=kwargs.pop("unit_id", 1),
        clock=clock or FakeClock(), **kwargs,
    )


def _scripted(answer, clock=None):
    client_class = type("Scripted", (ScriptedClient,), {"answer": answer})
    return E3dcModbusSession(
        "192.0.2.10", port=502, unit_id=1, modbus_client_factory=client_class,
        clock=clock or FakeClock(),
    )


def _dead(client):
    raise modbus.ModbusError("timeout waiting for the Modbus response")


# --- the session: one consistent reading per cycle ----------------------------


def test_one_reading_carries_grid_pv_battery_and_soc_with_their_signs(device):
    session = _session(device)
    try:
        reading, error = session.reading_for_cycle()
        assert session.mapping.address_offset == -1
    finally:
        session.close()
    assert error is None
    assert reading.grid_w == -600.0
    assert reading.pv_w == 4321.0
    assert reading.battery_w == -1500.0
    assert reading.soc == 87.0
    assert reading.inverter_w is None


def test_import_stays_positive(device):
    _put_int32(device.words, 40074, 2345)
    session = _session(device)
    try:
        reading, _ = session.reading_for_cycle()
    finally:
        session.close()
    assert reading.grid_w == 2345.0


def test_the_inverter_is_read_only_when_a_device_needs_it(device):
    session = _session(device)
    session.read_inverter = True
    try:
        reading, _ = session.reading_for_cycle()
    finally:
        session.close()
    assert reading.inverter_w == 1234 + 1200 + 1190


@pytest.mark.parametrize("offset", [0, -2])
def test_offset_is_detected_rather_than_assumed(offset):
    words = probe.build_self_test_words(address_offset=offset)
    with probe._LoopbackE3dcServer(words) as server:
        session = _session(server)
        try:
            reading, _ = session.reading_for_cycle()
            assert session.mapping.address_offset == offset
        finally:
            session.close()
    assert reading.grid_w == -600.0


def test_device_identity_is_read_without_the_serial_number(device):
    session = _session(device)
    try:
        session.reading_for_cycle()
    finally:
        session.close()
    assert session.device_info == {
        "modbus_firmware": "1.4",
        "manufacturer": "HagerEnergy GmbH",
        "model": "S10 E AIO",
        "firmware_release": "S10_2021_04",
    }


def test_only_read_function_codes_reach_the_wire(device):
    RecordingClient.reads = []
    clock = FakeClock()
    session = _session(device, clock=clock, modbus_client_factory=RecordingClient)
    session.read_inverter = True
    try:
        session.reading_for_cycle()
        clock.now += 5
        session.reading_for_cycle()
    finally:
        session.close()
    assert RecordingClient.reads
    assert {code for _, _, code in RecordingClient.reads} <= modbus.READ_ONLY_FUNCTION_CODES


def test_the_session_and_the_meter_offer_no_write_path():
    for owner in (E3dcModbusSession, E3dcModbusGridMeterClient):
        public = {name for name in dir(owner) if not name.startswith("_")}
        assert not {name for name in public if "write" in name or "set" in name.split("_")}
    assert not hasattr(modbus.ReadOnlyModbusTcpClient, "write_registers")
    with pytest.raises(modbus.ModbusError, match="not a read"):
        modbus.ReadOnlyModbusTcpClient("127.0.0.1").read_registers(40000, 1, function_code=16)


def test_both_roles_in_one_cycle_cost_one_request_and_the_next_cycle_reads_again(device):
    RecordingClient.reads = []
    clock = FakeClock()
    session = _session(device, clock=clock, modbus_client_factory=RecordingClient)

    def power_reads():
        return sum(1 for _, count, _ in RecordingClient.reads if count == modbus.POWER_COUNT)

    try:
        first, _ = session.reading_for_cycle(role="grid_meter")
        clock.now += 0.1
        second, _ = session.reading_for_cycle(role="device:E3DC")
        assert second is first
        assert power_reads() == 1
        clock.now += 1.0
        session.reading_for_cycle(role="grid_meter")
        assert power_reads() == 2
    finally:
        session.close()


# --- the session: failures, back-off and recovery -----------------------------


def test_a_lost_device_answers_the_cycle_with_its_reason(device):
    clock = FakeClock()
    session = _session(device, clock=clock)
    try:
        assert session.reading_for_cycle()[0] is not None
        device.close()
        clock.now += 5
        reading, error = session.reading_for_cycle()
    finally:
        session.close()
    assert reading is None
    assert "device closed the connection" in error or "cannot connect" in error


def test_a_backing_off_session_sends_nothing_and_does_not_block_the_cycle():
    clock = FakeClock()
    session = _scripted(_dead, clock)
    session.reading_for_cycle()
    client = ScriptedClient.instance
    assert client.reads == 1
    clock.now += 0.5
    reading, error = session.reading_for_cycle()
    assert reading is None
    assert "timeout" in error
    assert client.reads == 1
    clock.now += 0.6
    session.reading_for_cycle()
    assert client.reads == 2


def test_retries_back_off_and_are_capped():
    clock = FakeClock()
    session = _scripted(_dead, clock)
    waits = []
    for _ in range(8):
        session.reading_for_cycle()
        waits.append(session._retry_at - clock.now)
        clock.now = session._retry_at
    assert waits[:4] == [1.0, 2.0, 4.0, 8.0]
    assert waits[-1] == E3dcModbusSession.MAXIMUM_RETRY_SECONDS


def test_a_long_outage_keeps_the_retry_capped_instead_of_overflowing():
    clock = FakeClock()
    session = _scripted(_dead, clock)
    session.consecutive_failures = 5000
    session.reading_for_cycle()
    assert session._retry_at - clock.now == E3dcModbusSession.MAXIMUM_RETRY_SECONDS


def test_a_silent_device_costs_one_read_per_cycle_not_one_per_offset():
    session = _scripted(_dead)
    reading, error = session.reading_for_cycle()
    assert reading is None
    assert ScriptedClient.instance.reads == 1
    assert error == "no Modbus answer from 192.0.2.10:502: timeout waiting for the Modbus response"
    assert "unit id" not in error


def test_a_device_that_answered_is_not_reported_as_silent():
    def answered_then_dropped(client):
        if client.reads <= 2:
            return (0x1234,)
        raise modbus.ModbusError("device closed the connection")

    session = _scripted(answered_then_dropped)
    _, error = session.reading_for_cycle()
    assert error.startswith("connection to 192.0.2.10:502 lost after it answered")
    assert "last answer: 0x1234" in error


def test_an_unexpected_error_is_a_failed_read_not_a_crash():
    def broken(client):
        raise IndexError("index out of range")

    reading, error = _scripted(broken).reading_for_cycle()
    assert reading is None
    assert error == "index out of range"


def test_recovers_after_the_device_comes_back():
    words = probe.build_self_test_words()
    clock = FakeClock()
    first = probe._LoopbackE3dcServer(words)
    session = _session(first, clock=clock)
    second = None
    try:
        session.reading_for_cycle()
        first.close()
        clock.now += 5
        assert session.reading_for_cycle()[0] is None
        second = probe._LoopbackE3dcServer(words)
        session._client.port = second.port
        clock.now += 5
        reading, error = session.reading_for_cycle()
        assert error is None
        assert reading.grid_w == -600.0
        assert session.consecutive_failures == 0
    finally:
        session.close()
        if second is not None:
            second.close()


def test_sunspec_mode_is_named_as_the_cause():
    words = probe.build_self_test_words(magic=modbus.SUNSPEC_FIRST_WORD)
    with probe._LoopbackE3dcServer(words) as server:
        session = _session(server)
        try:
            _, error = session.reading_for_cycle()
        finally:
            session.close()
    assert "SunSpec mode" in error


def test_wrong_unit_id_is_refused_rather_than_guessed(device):
    session = _session(device, unit_id=7)
    try:
        reading, error = session.reading_for_cycle()
    finally:
        session.close()
    assert reading is None
    assert "magic word 0xE3DC not found at unit id 7" in error


def test_a_truncated_response_is_a_modbus_error():
    import socket as socket_module

    ours, device_end = socket_module.socketpair()
    client = modbus.ReadOnlyModbusTcpClient("127.0.0.1")
    client._socket = ours
    device_end.sendall(struct.pack(">HHHBB", 1, 0, 2, 1, 3))
    try:
        with pytest.raises(modbus.ModbusError, match="truncated"):
            client.read_registers(40000, 1)
    finally:
        client.close()
        device_end.close()


def test_one_session_per_endpoint_whatever_the_spelling_of_the_host():
    sessions = E3dcSessions()
    try:
        a = sessions.session("E3DC.lan", 502, 1)
        b = sessions.session(" e3dc.lan", 502, 1)
        c = sessions.session("e3dc.lan", 502, 2)
    finally:
        sessions.close()
    assert a is b
    assert a is not c


# --- the grid meter: the loop's reading, or the last good value ---------------


def test_the_meter_returns_this_cycles_grid_power(device):
    meter = E3dcModbusGridMeterClient(_session(device))
    try:
        assert meter.get_power() == -600.0
    finally:
        meter.close()
    assert meter.health.success_count == 1
    assert meter.health.last_latency_ms > 0


def test_a_failed_cycle_keeps_the_last_value_and_reports_at_once(device):
    clock = FakeClock()
    meter = E3dcModbusGridMeterClient(_session(device, clock=clock))
    try:
        assert meter.get_power() == -600.0
        measured = meter.health.last_latency_ms
        device.close()
        clock.now += 5
        assert meter.get_power() == -600.0
    finally:
        meter.close()
    assert meter.health.consecutive_failures == 1
    assert meter.health.stale_used is True
    assert meter.health.last_latency_ms == measured


def test_before_any_reading_the_placeholder_is_reported_as_a_failed_read():
    meter = E3dcModbusGridMeterClient(_scripted(_dead))
    assert meter.get_power() == 0
    assert meter.health.success_count == 0
    assert "no Modbus answer" in meter.health.last_error


def test_the_factory_shares_the_session_it_is_given(device):
    sessions = E3dcSessions()
    config = {"type": "e3dc_modbus", "ip": "127.0.0.1", "port": device.port}
    try:
        meter = create_grid_meter_client(config, session=None, e3dc_sessions=sessions)
        assert meter.session is sessions.session("127.0.0.1", device.port, 1)
        assert meter.get_power() == -600.0
        meter.close()
        assert meter.session.mapping is not None
    finally:
        sessions.close()


def test_the_factory_without_sessions_builds_one_the_meter_owns(device):
    meter = create_grid_meter_client(
        {"type": "e3dc_modbus", "ip": "127.0.0.1", "port": device.port, "unit_id": 1},
        session=None,
    )
    try:
        assert isinstance(meter, E3dcModbusGridMeterClient)
        assert meter.provider == "E3/DC"
        assert meter.transport == "modbus_tcp"
        assert meter.get_power() == -600.0
    finally:
        meter.close()
    assert meter.session.mapping is None


# --- configuration ------------------------------------------------------------


def test_config_default_port_is_the_modbus_tcp_port():
    assert cfg.E3DC_MODBUS_DEFAULT_PORT == modbus.MODBUS_TCP_PORT


def test_catalog_default_port_is_the_config_default():
    from ems.config_catalog import GRID_METER_VARIANTS

    assert GRID_METER_VARIANTS["e3dc_modbus"]["default_port"] == cfg.E3DC_MODBUS_DEFAULT_PORT


def test_meter_settings_defaults_match_the_measured_installation():
    assert cfg.e3dc_modbus_grid_meter_settings({"ip": " 192.0.2.10 "}) == {
        "host": "192.0.2.10",
        "port": 502,
        "unit_id": 1,
    }


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"ip": ""}, "requires grid_meter.ip"),
        ({"port": 0}, "grid_meter.port must be in 1..65535"),
        ({"port": 70000}, "grid_meter.port must be in 1..65535"),
        ({"port": "502.5"}, "grid_meter.port must be a whole number"),
        ({"port": True}, "grid_meter.port must be a number, not a boolean"),
        ({"port": "nan"}, "grid_meter.port must be a finite number"),
        ({"unit_id": 256}, "grid_meter.unit_id must be in 0..255"),
        ({"unit_id": "one"}, "grid_meter.unit_id must be a number"),
    ],
)
def test_invalid_meter_settings_are_refused_not_defaulted(overrides, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        cfg.e3dc_modbus_grid_meter_settings({"ip": "192.0.2.10", **overrides})


@pytest.mark.parametrize(
    "device, message",
    [
        ({"ip": "192.0.2.10"}, "E3/DC Modbus device requires a name"),
        ({"name": "E3DC"}, "E3/DC Modbus device E3DC requires ip"),
        ({"name": "E3DC", "ip": "192.0.2.10", "unit_id": 300}, "unit_id must be in 0..255"),
    ],
)
def test_invalid_device_settings_are_refused(device, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        cfg.e3dc_modbus_device_settings({"type": "e3dc_modbus", **device})


def test_the_grid_register_is_the_word_swapped_int32_at_40074():
    assert modbus.GRID_POWER_FIELD.address == 40074
    assert modbus.GRID_POWER_FIELD.datatype == "int32sw"
    raw = (-4) & 0xFFFFFFFF
    assert modbus.GRID_POWER_FIELD.decode((raw & 0xFFFF, raw >> 16)) == -4


def test_model_and_transport_labels_name_modbus():
    assert cfg.grid_meter_model_transport("e3dc_modbus") == (
        "E3/DC energy storage system",
        "Modbus TCP",
    )


def test_template_placeholder_meter_ip_keeps_the_ems_in_no_write_mode():
    config = {
        "devices": [{"name": "WR1", "ip": "192.0.2.1", "sn": "SN1"}],
        "grid_meter": {"type": "e3dc_modbus", "ip": "192.168.1.50"},
    }
    assert "grid_meter.ip" in cfg.template_placeholder_paths(config)


@pytest.mark.parametrize(
    "from_type, to_type, carries",
    [
        ("shelly", "ecotracker", True),
        ("zendure_grid_meter_http", "e3dc_modbus", False),
        ("e3dc_modbus", "tasmota_http", False),
        ("e3dc_modbus", "e3dc_modbus", True),
        ("mqtt", "shelly", False),
        ("unknown", "shelly", False),
    ],
)
def test_a_port_only_carries_over_between_variants_that_share_it(from_type, to_type, carries):
    from ems.config_catalog import grid_meter_port_carries_over

    assert grid_meter_port_carries_over(from_type, to_type) is carries


def test_a_legacy_flat_mqtt_max_age_survives_the_variant_cleanup():
    from ems.config_mutation import strip_incompatible_grid_meter_fields

    grid = {"type": "mqtt", "host": "broker", "topic": "meter/power", "max_age_seconds": 30}
    strip_incompatible_grid_meter_fields(grid, "mqtt")
    assert grid["max_age_seconds"] == 30


# --- diagnostics ----------------------------------------------------------------


def test_diagnostics_names_the_same_provider_as_the_meter():
    from ems.diagnostics import GRID_METER_PROVIDERS

    assert GRID_METER_PROVIDERS["e3dc_modbus"] == E3dcModbusGridMeterClient.provider


def _diagnose_checks(grid_meter, section, devices=()):
    from ems.diagnostics import diagnose_grid_meter_config, diagnose_hardware

    checks = []
    if section == "config":
        diagnose_grid_meter_config(checks, {"grid_meter": grid_meter})
        return checks, None
    health = diagnose_hardware(checks, {"grid_meter": grid_meter, "devices": list(devices)})
    return checks, health


def _codes(checks):
    return {check["code"]: check for check in checks}


def test_diagnose_config_accepts_a_complete_e3dc_meter():
    checks, _ = _diagnose_checks({"type": "e3dc_modbus", "ip": "192.0.2.10"}, "config")
    codes = _codes(checks)
    assert "grid_meter_type_unknown" not in codes
    assert codes["grid_meter_type"]["details"]["transport"] == "Modbus TCP"
    assert codes["grid_meter_ip_present"]["level"] == "ok"


@pytest.mark.parametrize(
    "grid_meter, code",
    [
        ({"type": "e3dc_modbus"}, "grid_meter_ip_missing"),
        ({"type": "e3dc_modbus", "ip": "192.0.2.10", "unit_id": 300}, "grid_meter_e3dc_modbus_invalid"),
    ],
)
def test_diagnose_config_reports_an_incomplete_e3dc_meter_as_error(grid_meter, code):
    checks, _ = _diagnose_checks(grid_meter, "config")
    assert _codes(checks)[code]["level"] == "error"


def test_diagnose_hardware_reads_the_meter_through_the_ems_session(device):
    checks, health = _diagnose_checks(
        {"type": "e3dc_modbus", "ip": "127.0.0.1", "port": device.port}, "hardware"
    )
    check = _codes(checks)["e3dc_modbus_read_ok"]
    assert check["details"]["power_w"] == -600.0
    assert check["details"]["model"] == "S10 E AIO"
    assert "S10-12345678912" not in str(checks)
    assert health["grid_meter"]["provider"] == "E3/DC"
    assert health["grid_meter"]["success_count"] == 1


def test_diagnose_hardware_explains_the_two_switches_when_unreachable(device):
    device.close()
    checks, health = _diagnose_checks(
        {"type": "e3dc_modbus", "ip": "127.0.0.1", "port": device.port}, "hardware"
    )
    check = _codes(checks)["e3dc_modbus_read_failed"]
    assert check["level"] == "warning"
    assert "ModBus TCP" in check["hint"]
    assert health["grid_meter"]["consecutive_failures"] == 1


def test_diagnose_hardware_reads_an_e3dc_device_and_never_its_http_endpoint(device, monkeypatch):
    import ems.diagnostics as diagnostics

    def no_http(*args, **kwargs):
        raise AssertionError("an E3/DC device must not be probed over HTTP")

    monkeypatch.setattr(diagnostics, "diagnose_http_json", no_http)
    checks, health = _diagnose_checks(
        {"type": "ha"},
        "hardware",
        devices=[{"name": "E3DC", "type": "e3dc_modbus", "ip": "127.0.0.1", "port": device.port}],
    )
    check = _codes(checks)["e3dc_modbus_device_read_ok"]
    assert check["details"]["pv_w"] == 4321.0
    assert check["details"]["battery_w"] == -1500.0
    assert check["details"]["inverter_w"] == 1234 + 1200 + 1190
    assert check["details"]["grid_w"] == -600.0
    assert health["devices"] == [
        {"name": "E3DC", "read": health["devices"][0]["read"], "write": None}
    ]
    assert health["devices"][0]["read"]["success_count"] == 1


# --- the CLI setup assistant and emsctl -----------------------------------------


def test_config_init_asks_for_ip_port_and_unit_id_and_drops_foreign_keys():
    from ems import config_init

    existing = {
        "type": "e3dc_modbus",
        "ip": "192.0.2.10",
        "unit_id": 3,
        "url": "http://stale",
        "power_path": "StatusSNS.Power",
        "mqtt": {"host": "broker"},
    }
    result = config_init.ask_grid_meter(existing, noninteractive=True)
    assert result == {"type": "e3dc_modbus", "ip": "192.0.2.10", "port": 502, "unit_id": 3}
    assert config_init._meter_summary(result) == "E3/DC Modbus TCP at 192.0.2.10:502 unit 3"


def test_config_init_refuses_an_out_of_range_unit_id():
    from ems import config_init

    with pytest.raises(config_init.ConfigInitError, match="unit ID must be <= 255"):
        config_init.ask_grid_meter(
            {"type": "e3dc_modbus", "ip": "192.0.2.10", "unit_id": 256},
            noninteractive=True,
        )


def test_config_init_asks_again_for_an_out_of_range_unit_id(monkeypatch):
    from ems import config_init

    answers = iter([str(len(config_init.GRID_METER_CHOICES)), "192.0.2.10", "", "300", "7"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    result = config_init.ask_grid_meter({"type": "shelly", "ip": "192.0.2.10"})

    assert result["unit_id"] == 7


def test_config_init_accepts_a_stored_whole_number_float_port():
    from ems import config_init

    result = config_init.ask_grid_meter(
        {"type": "e3dc_modbus", "ip": "192.0.2.10", "port": 1502.0}, noninteractive=True
    )
    assert result["port"] == 1502


def test_config_init_does_not_carry_an_http_port_to_the_e3dc(monkeypatch):
    from ems import config_init

    answers = iter([str(len(config_init.GRID_METER_CHOICES)), "", "", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    result = config_init.ask_grid_meter(
        {"type": "zendure_grid_meter_http", "ip": "192.0.2.10", "port": 80}
    )
    assert result == {"type": "e3dc_modbus", "ip": "192.0.2.10", "port": 502, "unit_id": 1}


def test_config_init_switch_away_from_e3dc_drops_modbus_keys(monkeypatch):
    from ems import config_init

    answers = iter(["1", "192.0.2.20"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    result = config_init.ask_grid_meter(
        {"type": "e3dc_modbus", "ip": "192.0.2.10", "port": 502, "unit_id": 1}
    )
    assert result == {"type": "shelly", "ip": "192.0.2.20"}


def test_emsctl_grid_meter_test_reads_the_e3dc_every_interval(device, capsys):
    from types import SimpleNamespace

    import emsctl

    rc = emsctl.handle_grid_meter_command(
        SimpleNamespace(action="test", duration=1, interval=0.6),
        {"grid_meter": {"type": "e3dc_modbus", "ip": "127.0.0.1", "port": device.port}},
    )
    out = capsys.readouterr().out
    assert f"Grid meter read test: E3/DC 127.0.0.1:{device.port} unit 1" in out
    assert "Failed: 0" in out
    assert "Latest power: -600.0 W" in out
    assert rc == 0


class _InverterTimesOut(modbus.ReadOnlyModbusTcpClient):
    """The real client, except that the inverter block times out."""

    connects = 0
    inverter_reads = 0

    def connect(self):
        _InverterTimesOut.connects += 1
        super().connect()

    def read_registers(self, address, count, function_code=3, unit_id=None):
        if count == modbus.INVERTER_COUNT:
            _InverterTimesOut.inverter_reads += 1
            raise modbus.ModbusError("timeout waiting for the Modbus response")
        return super().read_registers(address, count, function_code, unit_id)


def test_a_timing_out_inverter_block_backs_off_without_costing_the_grid(device):
    _InverterTimesOut.connects = 0
    _InverterTimesOut.inverter_reads = 0
    clock = FakeClock()
    session = _session(device, clock=clock, modbus_client_factory=_InverterTimesOut)
    session.read_inverter = True
    try:
        readings = []
        for _ in range(5):
            readings.append(session.reading_for_cycle()[0])
            clock.now += 0.6
    finally:
        session.close()
    assert all(reading.grid_w == -600.0 for reading in readings)
    assert all(reading.inverter_w is None for reading in readings)
    assert _InverterTimesOut.inverter_reads == 2
    assert _InverterTimesOut.connects == 3
    assert session.consecutive_failures == 0


def test_diagnose_warns_when_the_device_answers_without_its_inverter_block():
    words = probe.build_self_test_words()
    refused = set(range(41000 - 1, 41000 - 1 + modbus.INVERTER_COUNT))
    with probe._LoopbackE3dcServer(words, unsupported=refused) as server:
        checks, health = _diagnose_checks(
            {"type": "ha"},
            "hardware",
            devices=[{"name": "E3DC", "type": "e3dc_modbus", "ip": "127.0.0.1", "port": server.port}],
        )
    codes = _codes(checks)
    assert "e3dc_modbus_device_read_ok" not in codes
    assert codes["e3dc_modbus_device_inverter_unavailable"]["level"] == "warning"
    assert health["devices"][0]["read"]["success_count"] == 0


def test_a_device_lost_during_identification_costs_one_read_not_a_retry_per_field():
    def magic_then_silence(client):
        if client.reads == 1:
            return (modbus.MAGIC_VALUE,)
        raise modbus.ModbusError("timeout waiting for the Modbus response")

    session = _scripted(magic_then_silence)
    reading, error = session.reading_for_cycle()
    assert reading is None
    assert ScriptedClient.instance.reads == 2
    assert "timeout" in error


def test_back_off_starts_when_the_failed_attempt_ends():
    clock = FakeClock()

    def slow_timeout(client):
        clock.now += 2.0
        raise modbus.ModbusError("timeout waiting for the Modbus response")

    session = _scripted(slow_timeout, clock)
    session.reading_for_cycle()
    assert session._retry_at == clock.now + 1.0
    clock.now += 0.5
    session.reading_for_cycle()
    assert ScriptedClient.instance.reads == 1


def test_diagnose_reports_the_detected_offset(device):
    checks, _ = _diagnose_checks(
        {"type": "e3dc_modbus", "ip": "127.0.0.1", "port": device.port}, "hardware"
    )
    assert _codes(checks)["e3dc_modbus_read_ok"]["details"]["address_offset"] == -1
