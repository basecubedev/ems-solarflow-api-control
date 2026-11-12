# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A SunSpec meter model read over Modbus TCP.

The register maps follow the SunSpec model definitions (sunspec/models): the
``SunS`` marker, model 1 (common, 66 registers), then a meter model whose total
real power ``W`` sits at offset 18 with its scale factor at 22 (201 to 204), or
as a float32 at offset 28 (211 to 214). The Modbus client is a fake; no test
reaches a device.
"""

import struct

import pytest

from ems import config as cfg
from ems import diagnostics
from ems import sunspec
from ems.clients import SunSpecMeterClient, create_grid_meter_client
from ems.e3dc_modbus import ModbusError, ModbusExceptionResponse
from ems.sunspec import MeterModel, SunSpecError, SunSpecMeter, locate_meter, read_meter_power

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
]

METER = {"type": "sunspec_modbus", "ip": "192.0.2.90", "unit_id": 200}


def registers(model_id=203, power=-1234, scale=-1, *, base=40000, length=105):
    regs = {base: 0x5375, base + 1: 0x6E53, base + 2: 1, base + 3: 66}
    meter = base + 2 + 68
    regs[meter], regs[meter + 1] = model_id, length
    if model_id in sunspec.FLOAT_METER_MODELS:
        regs[meter + 28], regs[meter + 29] = struct.unpack(">HH", struct.pack(">f", power))
    else:
        regs[meter + 18] = power & 0xFFFF
        regs[meter + 22] = scale & 0xFFFF
    regs[meter + 2 + length] = 0xFFFF
    return regs


class FakeModbus:
    def __init__(self, regs, *, refused=(), fail_after=None):
        self.regs = regs
        self.refused = set(refused)
        self.fail_after = fail_after
        self.reads = []
        self.connected = False
        self.closed = False

    def connect(self):
        self.connected = True

    def close(self):
        self.closed = True

    def read_registers(self, address, count, function_code=3, unit_id=None):
        self.reads.append((address, count))
        if self.fail_after is not None and len(self.reads) > self.fail_after:
            raise ModbusError("timeout waiting for the Modbus response")
        if address in self.refused:
            raise ModbusExceptionResponse(function_code, 2)
        return tuple(self.regs.get(item, 0) for item in range(address, address + count))


# --- the register map ----------------------------------------------------------------


@pytest.mark.parametrize(
    "regs, model, power",
    [
        (registers(203, -1234, -1), MeterModel(203, 40070), -123.4),
        (registers(201, 450, 1), MeterModel(201, 40070), 4500.0),
        (registers(204, 800, 0), MeterModel(204, 40070), 800.0),
        (registers(213, -678.5), MeterModel(213, 40070), -678.5),
        (registers(203, 300, 0, base=0), MeterModel(203, 70), 300.0),
    ],
)
def test_the_meter_model_is_found_in_the_chain_and_its_power_read(regs, model, power):
    client = FakeModbus(regs)

    assert locate_meter(client) == model
    assert read_meter_power(client, model) == pytest.approx(power)


def test_a_base_that_answers_with_an_exception_is_skipped():
    client = FakeModbus(registers(base=50000), refused={40000, 0})

    assert locate_meter(client) == MeterModel(203, 50070)


@pytest.mark.parametrize(
    "regs, message",
    [
        ({}, "no SunSpec device"),
        ({40000: 0x5375, 40001: 0x6E53, 40002: 1, 40003: 66, 40070: 0xFFFF}, "no meter model"),
    ],
)
def test_a_device_without_a_meter_model_is_refused(regs, message):
    with pytest.raises(SunSpecError, match=message):
        locate_meter(FakeModbus(regs))


@pytest.mark.parametrize(
    "regs, message",
    [
        (registers(203, 0x8000, 0), "does not report"),
        (registers(203, 100, 0x8000), "does not report"),
        (registers(203, 100, 20), "scale factor"),
        (registers(213, float("nan")), "does not report"),
    ],
)
def test_a_power_the_meter_does_not_report_is_refused(regs, message):
    client = FakeModbus(regs)

    with pytest.raises(SunSpecError, match=message):
        read_meter_power(client, locate_meter(client))


# --- the connection ------------------------------------------------------------------


def test_the_meter_is_located_once_and_read_every_cycle():
    built = []

    def factory(host, *, port, unit_id, timeout):
        built.append((host, port, unit_id, timeout))
        return FakeModbus(registers())

    meter = SunSpecMeter("192.0.2.90", 502, 200, client_factory=factory)

    assert meter.read_power() == -123.4
    assert meter.read_power() == -123.4
    assert built == [("192.0.2.90", 502, 200, 3.0)]
    assert meter.model == MeterModel(203, 40070)


def test_a_failed_read_drops_the_connection_and_the_next_one_starts_over():
    clients = [FakeModbus(registers(), fail_after=4), FakeModbus(registers(213, 50.0))]
    meter = SunSpecMeter("192.0.2.90", client_factory=lambda *_args, **_kwargs: clients[0] if not clients[0].connected else clients[1])

    meter.read_power()
    with pytest.raises(ModbusError):
        meter.read_power()
    assert clients[0].closed and meter.model is None
    assert meter.read_power() == 50.0


# --- the meter -----------------------------------------------------------------------


class Meter:
    endpoint = "192.0.2.90:502 unit 200"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.closed = False

    def read_power(self):
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def close(self):
        self.closed = True


def test_a_failed_read_keeps_the_last_value(caplog):
    client = SunSpecMeterClient(Meter(-250.0, SunSpecError("meter model 203 does not report its total real power")))

    assert client.get_power() == -250.0
    assert client.get_power() == -250.0
    assert client.health.consecutive_failures == 1
    assert "sunspec_meter_read_error" in caplog.text


def test_the_factory_reads_the_configured_unit():
    client = create_grid_meter_client(METER, None)

    assert isinstance(client, SunSpecMeterClient)
    assert client.endpoint == "192.0.2.90:502 unit 200"
    assert cfg.sunspec_modbus_grid_meter_settings({"ip": "192.0.2.90"}) == {"host": "192.0.2.90", "port": 502, "unit_id": 1}
    with pytest.raises(ValueError, match="unit_id"):
        cfg.sunspec_modbus_grid_meter_settings({**METER, "unit_id": 300})


def test_the_meter_is_named_alike_everywhere():
    assert cfg.grid_meter_model_transport("sunspec_modbus") == ("SunSpec meter", "Modbus TCP")
    assert diagnostics.GRID_METER_PROVIDERS["sunspec_modbus"] == SunSpecMeterClient.provider


# --- the CLI setup assistant and the diagnosis ---------------------------------------


def test_config_init_asks_address_port_and_unit(monkeypatch):
    from ems import config_init

    menu = str([choice for choice, _ in config_init.GRID_METER_CHOICES].index("sunspec_modbus") + 1)
    answers = iter([menu, "192.0.2.90", "", "200"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    result = config_init.ask_grid_meter({"type": "dsmr_p1_tcp", "ip": "192.0.2.80", "port": 23})

    assert result == {**METER, "port": 502}
    assert config_init._meter_summary(result) == "SunSpec meter Modbus TCP at 192.0.2.90:502 unit 200"


def test_diagnose_reads_the_meter_once_and_lets_go(monkeypatch):
    built = []

    class ProbeMeter(Meter):
        def __init__(self, host, *, port, unit_id):
            super().__init__(-678.0)
            built.append(self)

    monkeypatch.setattr(sunspec, "SunSpecMeter", ProbeMeter)
    checks = []

    health = diagnostics.diagnose_hardware(checks, {"grid_meter": METER, "devices": []})

    codes = {check["code"]: check for check in checks}
    assert codes["grid_meter_read_ok"]["details"]["power_w"] == -678.0
    assert built[0].closed
    assert health["grid_meter"]["provider"] == "SunSpec meter"
