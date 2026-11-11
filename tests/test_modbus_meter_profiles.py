# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Grid meters read over Modbus TCP by their manufacturers' register maps.

Each register map follows the manufacturer's Modbus document: Huawei's Solar
Inverter Modbus Interface Definitions (37113 I32, >0 feeding into the grid,
meter status 37100), Sungrow's hybrid inverter protocol (input register 5601,
address 5600, S32 low word first, >0 buying from the grid), the Carlo Gavazzi
EM24 Ethernet protocol (0x0028 INT32 W*10, LSW first, imported positive),
Victron's GX register list (820 to 822 int16, power supplied by the grid) and
the Wibeee Modbus TCP specification (register 6 S32 LSB first). The Modbus
client is a fake; no test reaches a device.
"""

import struct

import pytest

from ems import config as cfg
from ems import diagnostics, modbus_meters
from ems.clients import ModbusMeterClient, create_grid_meter_client
from ems.e3dc_modbus import READ_HOLDING_REGISTERS, READ_INPUT_REGISTERS, ModbusError
from ems.modbus_meters import PROFILES_BY_TYPE, ModbusMeterError, ProfileMeter, read_profile_power

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
]


def int32(value, *, low_word_first):
    high, low = struct.unpack(">HH", struct.pack(">i", value))
    return (low, high) if low_word_first else (high, low)


class FakeModbus:
    def __init__(self, regs, *, fail=False):
        self.regs = regs
        self.fail = fail
        self.reads = []
        self.connected = False
        self.closed = False

    def connect(self):
        self.connected = True

    def close(self):
        self.closed = True

    def read_registers(self, address, count, function_code=READ_HOLDING_REGISTERS, unit_id=None):
        self.reads.append((function_code, address, count))
        if self.fail:
            raise ModbusError("timeout waiting for the Modbus response")
        return tuple(self.regs.get(item, 0) for item in range(address, address + count))


def huawei(power, status=1):
    return {37100: status, **dict(zip((37113, 37114), int32(power, low_word_first=False)))}


@pytest.mark.parametrize(
    "meter_type, regs, power, sent",
    [
        ("huawei_sun2000_modbus", huawei(500), -500.0, (READ_HOLDING_REGISTERS, 37100, 15)),
        ("huawei_sun2000_modbus", huawei(-1200), 1200.0, (READ_HOLDING_REGISTERS, 37100, 15)),
        ("sungrow_sh_modbus", dict(zip((5600, 5601), int32(-800, low_word_first=True))), -800.0, (READ_INPUT_REGISTERS, 5600, 2)),
        ("sungrow_sh_modbus", dict(zip((5600, 5601), int32(70000, low_word_first=True))), 70000.0, (READ_INPUT_REGISTERS, 5600, 2)),
        ("carlo_gavazzi_em24_modbus", dict(zip((0x28, 0x29), int32(12345, low_word_first=True))), 1234.5, (READ_HOLDING_REGISTERS, 0x28, 2)),
        ("carlo_gavazzi_em24_modbus", dict(zip((0x28, 0x29), int32(-4567, low_word_first=True))), -456.7, (READ_HOLDING_REGISTERS, 0x28, 2)),
        ("victron_gx_modbus", {820: 100, 821: 0x10000 - 50, 822: 25}, 75.0, (READ_HOLDING_REGISTERS, 820, 3)),
        ("wibeee_modbus", dict(zip((6, 7), int32(-300, low_word_first=True))), -300.0, (READ_HOLDING_REGISTERS, 6, 2)),
    ],
)
def test_each_profile_reads_grid_power_import_positive_in_one_request(meter_type, regs, power, sent):
    client = FakeModbus(regs)

    assert read_profile_power(client, PROFILES_BY_TYPE[meter_type]) == pytest.approx(power)
    assert client.reads == [sent]


def test_a_huawei_meter_that_reports_itself_offline_is_refused():
    with pytest.raises(ModbusMeterError, match="offline"):
        read_profile_power(FakeModbus(huawei(0, status=0)), PROFILES_BY_TYPE["huawei_sun2000_modbus"])


def test_the_meter_connects_once_at_the_profile_defaults_and_starts_over_after_a_failure():
    built = []
    clients = [FakeModbus({820: 10}), FakeModbus({820: 20})]

    def factory(host, *, port, unit_id, timeout):
        built.append((host, port, unit_id))
        return clients[len(built) - 1]

    meter = ProfileMeter("192.0.2.95", PROFILES_BY_TYPE["victron_gx_modbus"], client_factory=factory)

    assert meter.read_power() == 10.0
    clients[0].fail = True
    with pytest.raises(ModbusError):
        meter.read_power()
    assert clients[0].closed
    assert meter.read_power() == 20.0
    assert built == [("192.0.2.95", 502, 100), ("192.0.2.95", 502, 100)]
    assert meter.endpoint == "192.0.2.95:502 unit 100"


def test_the_factory_reads_the_configured_device_and_names_it(caplog):
    client = create_grid_meter_client({"type": "huawei_sun2000_modbus", "ip": "192.0.2.96", "port": 6607}, None)

    assert isinstance(client, ModbusMeterClient)
    assert (client.provider, client.endpoint) == ("Huawei SUN2000 + DTSU666", "192.0.2.96:6607 unit 1")
    client.meter = type("Failing", (), {"endpoint": client.endpoint, "read_power": lambda self: (_ for _ in ()).throw(ModbusMeterError("offline")), "close": lambda self: None})()
    assert client.get_power() == 0
    assert "modbus_meter_read_error" in caplog.text


@pytest.mark.parametrize("meter_type", sorted(PROFILES_BY_TYPE))
def test_every_profile_is_named_alike_everywhere(meter_type):
    label = PROFILES_BY_TYPE[meter_type].label

    assert cfg.grid_meter_model_transport(meter_type) == (label, "Modbus TCP")
    assert diagnostics.GRID_METER_PROVIDERS[meter_type] == label
    assert meter_type in cfg.GRID_METER_READER_SETTINGS


def test_settings_default_to_the_profile_and_refuse_what_modbus_cannot_address():
    assert cfg.modbus_profile_grid_meter_settings({"ip": "192.0.2.95"}, "victron_gx_modbus") == {"host": "192.0.2.95", "port": 502, "unit_id": 100}
    with pytest.raises(ValueError, match="grid_meter.unit_id"):
        cfg.modbus_profile_grid_meter_settings({"ip": "192.0.2.95", "unit_id": 300}, "victron_gx_modbus")
    with pytest.raises(ValueError, match="grid_meter.ip"):
        cfg.modbus_profile_grid_meter_settings({}, "wibeee_modbus")


def test_config_init_asks_address_port_and_unit_with_the_profile_defaults(monkeypatch):
    from ems import config_init

    menu = str([choice for choice, _ in config_init.GRID_METER_CHOICES].index("victron_gx_modbus") + 1)
    answers = iter([menu, "192.0.2.95", "", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    result = config_init.ask_grid_meter({"type": "sunspec_modbus", "ip": "192.0.2.9", "unit_id": 200})

    assert result == {"type": "victron_gx_modbus", "ip": "192.0.2.95", "port": 502, "unit_id": 100}
    assert config_init._meter_summary(result) == "Victron GX Modbus TCP at 192.0.2.95:502 unit 100"


def test_diagnose_reads_the_device_once_with_the_ems_reader(monkeypatch):
    built = []

    class Probe:
        def __init__(self, host, profile, port=None, unit_id=None):
            self.endpoint = f"{host}:{port} unit {unit_id}"
            self.closed = False
            built.append(self)

        def read_power(self):
            return -678.0

        def close(self):
            self.closed = True

    monkeypatch.setattr(modbus_meters, "ProfileMeter", Probe)
    checks = []

    health = diagnostics.diagnose_hardware(checks, {"grid_meter": {"type": "sungrow_sh_modbus", "ip": "192.0.2.97"}, "devices": []})

    codes = {check["code"]: check for check in checks}
    assert codes["grid_meter_read_ok"]["details"]["power_w"] == -678.0
    assert built[0].closed
    assert health["grid_meter"]["provider"] == "Sungrow SH series"
