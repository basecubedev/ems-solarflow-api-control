# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The readers that carry meters of Switzerland, Austria and the UK.

A Swiss smart meter reader in the CH-SMR format (VSE recommendation, chapter 6)
publishes ``{"meter": {"1.7.0": kW, "2.7.0": kW}}``: keys with dots, reached by
a JSON Pointer, and kW, turned into W by the MQTT meter's scale. An Austrian or
Swiss Tasmota reader that decrypts the customer interface reports import and
export apart, so the Tasmota reader subtracts an export path. smart-me Telstar
(register 0x2004 at Modbus address 0x2003, int32 big-endian, mW) and IAMMETER
WEM3080T (address 32, int32 high word first, W) follow their vendors' Modbus
maps. No test reaches a device.
"""

import struct

import pytest

from ems import config as cfg
from ems.clients import TasmotaHttpClient, _get_json_path, _parse_mqtt_grid_power_payload, _parse_tasmota_http_power, create_grid_meter_client
from ems.e3dc_modbus import READ_HOLDING_REGISTERS
from ems.modbus_meters import PROFILES_BY_TYPE, read_profile_power

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
]

CH_SMR = b'{"meter":{"ts":1762916400,"1.7.0":0.25,"2.7.0":0.0,"1.8.0":1234.5}}'
CH_SMR_EXPORT = b'{"meter":{"ts":1762916401,"1.7.0":0.0,"2.7.0":0.48}}'


# --- JSON paths ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path, value",
    [
        ("StatusSNS.SML.Power", 7),
        ("/meter/1.7.0", 0.25),
        ("/odd~1key/a~0b", 3),
    ],
)
def test_a_path_is_dotted_or_a_json_pointer(path, value):
    data = {"StatusSNS": {"SML": {"Power": 7}}, "meter": {"1.7.0": 0.25}, "odd/key": {"a~b": 3}}

    assert _get_json_path(data, path) == value


def test_a_missing_pointer_target_is_refused():
    with pytest.raises(ValueError, match="Missing JSON path"):
        _get_json_path({"meter": {}}, "/meter/1.7.0")


# --- MQTT scale (CH-SMR) -------------------------------------------------------------


@pytest.mark.parametrize("payload, power", [(CH_SMR, 250.0), (CH_SMR_EXPORT, -480.0)])
def test_a_ch_smr_message_reads_in_watts(payload, power):
    assert _parse_mqtt_grid_power_payload(
        payload, payload_format="json", value_path="/meter/1.7.0", export_value_path="/meter/2.7.0", value_scale=1000
    ) == pytest.approx(power)


def test_a_scale_of_minus_one_turns_the_sign_of_a_plain_number():
    assert _parse_mqtt_grid_power_payload(b"350", value_scale=-1) == -350.0


@pytest.mark.parametrize("scale, expected", [(None, 1.0), ("1000", 1000.0), (-1, -1.0), (0.5, 0.5)])
def test_the_scale_defaults_to_one_and_takes_any_number_but_zero(scale, expected):
    settings = {} if scale is None else {"value_scale": scale}

    assert cfg.mqtt_grid_meter_value_scale(settings) == expected


@pytest.mark.parametrize("scale", [0, "kW", True, float("nan")])
def test_a_scale_that_cannot_turn_values_into_watts_is_refused(scale):
    with pytest.raises(ValueError, match="value_scale"):
        cfg.mqtt_grid_meter_value_scale({"value_scale": scale})


class FakeMqttClient:
    def username_pw_set(self, username, password):
        self.auth = (username, password)

    def connect_async(self, *_args, **_kwargs):
        return None

    def loop_start(self):
        return None


class Message:
    topic = "dt/home/12345678/ds"

    def __init__(self, payload):
        self.payload = payload


def test_the_mqtt_meter_reads_a_ch_smr_reader_and_a_d0_keeps_its_watts():
    mqtt = {"host": "192.0.2.70", "topic": "dt/home/12345678/ds", "payload_format": "json", "value_path": "/meter/1.7.0", "export_value_path": "/meter/2.7.0", "value_scale": 1000, "_mqtt_client_factory": FakeMqttClient}
    client = create_grid_meter_client({"type": "mqtt", "mqtt": mqtt}, None)
    client._on_message(None, None, Message(CH_SMR_EXPORT))

    assert client.last_value == -480.0
    d0 = create_grid_meter_client({"type": "zendure_smartmeter_d0", "mqtt": {"host": "192.0.2.70", "topic": "Zendure/sensor/ABC/totalPower", "value_scale": 1000, "_mqtt_client_factory": FakeMqttClient}}, None)
    assert d0.value_scale == 1.0


def test_normalized_settings_carry_the_scale():
    settings = cfg.normalize_mqtt_grid_meter_settings({"type": "mqtt", "mqtt": {"host": "h", "topic": "t", "value_scale": "1000"}})

    assert settings["value_scale"] == 1000.0
    with pytest.raises(ValueError, match="value_scale"):
        cfg.normalize_mqtt_grid_meter_settings({"type": "mqtt", "mqtt": {"host": "h", "topic": "t", "value_scale": 0}})


# --- Tasmota export path -------------------------------------------------------------


AT_READER = {"StatusSNS": {"Time": "2026-11-11T19:00:00", "SM": {"1_7_0": 0, "2_7_0": 512, "1_8_0": 1234.5}}}


def test_a_tasmota_reader_with_separate_export_reads_negative_on_export():
    assert _parse_tasmota_http_power(AT_READER, "StatusSNS.SM.1_7_0", "StatusSNS.SM.2_7_0") == -512.0
    assert _parse_tasmota_http_power(AT_READER, "StatusSNS.SM.1_7_0") == 0.0
    with pytest.raises(ValueError, match="2_8_0"):
        _parse_tasmota_http_power(AT_READER, "StatusSNS.SM.1_7_0", "StatusSNS.SM.2_8_0")


class Response:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class Session:
    def get(self, url, timeout=None):
        self.url = url
        return Response(AT_READER)


def test_the_factory_hands_the_export_path_to_the_reader():
    session = Session()
    client = create_grid_meter_client({"type": "tasmota_http", "ip": "192.0.2.30", "power_path": "StatusSNS.SM.1_7_0", "export_path": " StatusSNS.SM.2_7_0 "}, session)

    assert isinstance(client, TasmotaHttpClient)
    assert client.get_power() == -512.0
    assert session.url == "http://192.0.2.30/cm?cmnd=Status%2010"


def test_config_init_asks_for_an_optional_tasmota_export_path(monkeypatch):
    from ems import config_init

    menu = str([choice for choice, _ in config_init.GRID_METER_CHOICES].index("tasmota_http") + 1)
    answers = iter([menu, "192.0.2.30", "StatusSNS.SM.1_7_0", "StatusSNS.SM.2_7_0"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    result = config_init.ask_grid_meter({"type": "shelly", "ip": "192.0.2.9"})

    assert result == {"type": "tasmota_http", "ip": "192.0.2.30", "power_path": "StatusSNS.SM.1_7_0", "export_path": "StatusSNS.SM.2_7_0"}


# --- Modbus profiles -----------------------------------------------------------------


class FakeModbus:
    def __init__(self, regs):
        self.regs = regs
        self.reads = []

    def read_registers(self, address, count, function_code=READ_HOLDING_REGISTERS, unit_id=None):
        self.reads.append((function_code, address, count))
        return tuple(self.regs.get(item, 0) for item in range(address, address + count))


def _int32(address, value):
    return dict(zip((address, address + 1), struct.unpack(">HH", struct.pack(">i", value))))


@pytest.mark.parametrize(
    "meter_type, regs, power, sent",
    [
        ("smart_me_modbus", _int32(0x2003, 1234567), 1234.567, (READ_HOLDING_REGISTERS, 0x2003, 2)),
        ("smart_me_modbus", _int32(0x2003, -500000), -500.0, (READ_HOLDING_REGISTERS, 0x2003, 2)),
        ("iammeter_wem3080t_modbus", _int32(32, -2750), -2750.0, (READ_HOLDING_REGISTERS, 32, 2)),
    ],
)
def test_the_new_profiles_read_their_vendors_registers(meter_type, regs, power, sent):
    client = FakeModbus(regs)

    assert read_profile_power(client, PROFILES_BY_TYPE[meter_type]) == pytest.approx(power)
    assert client.reads == [sent]


# --- review fixes --------------------------------------------------------------------


def test_a_pointer_indexes_into_a_list_and_a_dotted_path_does_not():
    data = {"Datas": [[230.1, 1.2, -276]]}

    assert _get_json_path(data, "/Datas/0/2") == -276
    with pytest.raises(ValueError, match="Missing JSON path"):
        _get_json_path(data, "/Datas/1/2")
    with pytest.raises(ValueError, match="Missing JSON path"):
        _get_json_path(data, "Datas.0.2")
    for token in ("01", "\u0661", "-1"):
        with pytest.raises(ValueError, match="Missing JSON path"):
            _get_json_path({"a": [5, 6]}, f"/a/{token}")


def test_a_tasmota_reader_without_a_power_path_is_refused_even_with_an_export_path():
    with pytest.raises(ValueError, match="non-empty"):
        _parse_tasmota_http_power(AT_READER, "", "StatusSNS.SM.2_7_0")


def test_diagnose_reads_the_tasmota_export_path_too(monkeypatch):
    from ems import diagnostics

    monkeypatch.setattr(diagnostics, "diagnose_http_json", lambda url: (200, AT_READER))
    checks = []
    diagnostics.diagnose_hardware(checks, {"grid_meter": {"type": "tasmota_http", "ip": "192.0.2.30", "power_path": "StatusSNS.SM.1_7_0", "export_path": "StatusSNS.SM.2_7_0"}, "devices": []})

    assert {check["code"]: check for check in checks}["tasmota_read_ok"]["details"]["power_w"] == -512.0


def test_diagnose_config_refuses_a_scale_the_ems_would_refuse():
    from ems import diagnostics

    checks = []
    diagnostics.diagnose_grid_meter_config(checks, {"grid_meter": {"type": "mqtt", "mqtt": {"host": "h", "topic": "t", "value_scale": 0}}})

    assert {check["code"]: check["level"] for check in checks}["grid_meter_mqtt_value_scale_invalid"] == "error"


def test_a_d0_meter_ignores_a_scale_it_cannot_show():
    settings = cfg.normalize_mqtt_grid_meter_settings({"type": "zendure_smartmeter_d0", "mqtt": {"host": "h", "topic": "Zendure/sensor/ABC/totalPower", "value_scale": 0}})

    assert "value_scale" not in settings


def test_config_init_asks_again_for_a_scale_that_is_no_number(monkeypatch, capsys):
    from ems import config_init

    menu = str([choice for choice, _ in config_init.GRID_METER_CHOICES].index("mqtt") + 1)
    answers = iter([menu, "192.0.2.70", "", "", "dt/home/1/ds", "json", "/meter/1.7.0", "/meter/2.7.0", "kW", "1000", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr(config_init.getpass, "getpass", lambda _prompt="": "")

    result = config_init.ask_grid_meter({"type": "shelly", "ip": "192.0.2.9"})

    assert cfg.grid_meter_mqtt_settings(result)["value_scale"] == 1000.0
    assert "grid_meter.mqtt.value_scale" in capsys.readouterr().out


def test_a_profile_with_a_minimum_interval_is_not_asked_again_sooner():
    from ems.modbus_meters import ProfileMeter

    now = [100.0]
    client = FakeModbus(_int32(0x2003, 1500000))
    client.connect = lambda: None
    client.close = lambda: None
    meter = ProfileMeter("192.0.2.98", PROFILES_BY_TYPE["smart_me_modbus"], client_factory=lambda *a, **k: client, clock=lambda: now[0])

    assert meter.read_power() == 1500.0
    now[0] += 1.0
    assert meter.read_power() == 1500.0
    assert len(client.reads) == 1
    now[0] += 0.998
    meter.read_power()
    assert len(client.reads) == 2
