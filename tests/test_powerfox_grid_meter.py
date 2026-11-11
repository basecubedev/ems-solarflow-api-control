# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A powerfox poweropti read over its local API.

Payloads follow the local API as the python-powerfox library models it
(``GET /value`` with ``X-API-KEY``, OBIS values in W); no test reaches a real
poweropti. Whether the poweropti reports export as a negative ``1.7.0`` or as a
separate ``2.7.0`` is not confirmed on hardware, so both forms are read.
"""

import json
import logging

import pytest
import requests

from ems import config as cfg
from ems import diagnostics
from ems.clients import (
    METER_FROZEN_AFTER_S,
    PowerfoxClient,
    _parse_powerfox_value,
    create_grid_meter_client,
)

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
]

KEY = "1a2b3c4d5e6f"
METER = {"type": "powerfox_poweropti", "ip": "192.0.2.50", "token": KEY}


def _value(power, timestamp=1762916400, **more):
    values = [{"obis": "1.8.0", "value": 1234567}, {"obis": "1.7.0", "value": power}]
    values += [{"obis": obis, "value": value} for obis, value in more.items()]
    return {"timestamp": timestamp, "values": values}


class Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


class Session:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def close(self):
        self.closed = True

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers, timeout))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# --- settings ----------------------------------------------------------------------


def test_settings_need_an_address_and_the_api_key():
    assert cfg.powerfox_grid_meter_settings(METER) == {"host": "192.0.2.50", "port": 80, "token": KEY}
    assert cfg.powerfox_grid_meter_settings({**METER, "port": 8080})["port"] == 8080


@pytest.mark.parametrize(
    "change, message",
    [
        ({"ip": ""}, "grid_meter.ip"),
        ({"token": ""}, "grid_meter.token"),
        ({"token": "with space"}, "grid_meter.token"),
        ({"port": 0}, "grid_meter.port"),
    ],
)
def test_settings_refuse_what_the_poweropti_cannot_answer(change, message):
    with pytest.raises(ValueError, match=message):
        cfg.powerfox_grid_meter_settings({**METER, **change})


def test_the_meter_is_named_alike_everywhere():
    assert cfg.grid_meter_model_transport("powerfox_poweropti") == ("powerfox poweropti", "local HTTP API")
    assert diagnostics.GRID_METER_PROVIDERS["powerfox_poweropti"] == PowerfoxClient.provider


# --- the payload -------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload, power",
    [
        (_value(456), 456.0),
        (_value(-300), -300.0),
        (_value(0, **{"2.7.0": 300}), -300.0),
        (_value(120, **{"2.7.0": 20}), 100.0),
        (_value(-300, **{"2.7.0": 300}), -300.0),
    ],
)
def test_export_reads_negative_whichever_way_the_poweropti_reports_it(payload, power):
    assert _parse_powerfox_value(payload) == (power, 1762916400)


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"values": {}}, {"values": [{"obis": "1.8.0", "value": 1}]}, _value("456"), _value(float("nan"))],
)
def test_a_payload_without_numeric_power_is_refused(payload):
    with pytest.raises(ValueError, match="powerfox payload"):
        _parse_powerfox_value(payload)


def test_a_payload_without_a_time_stamp_still_reads():
    assert _parse_powerfox_value({"values": [{"obis": "1.7.0", "value": 5}]}) == (5.0, None)


# --- the reader --------------------------------------------------------------------


def test_the_reader_asks_with_the_api_key_and_returns_the_power():
    session = Session(Response(_value(-678)))
    client = create_grid_meter_client(METER, session)

    assert isinstance(client, PowerfoxClient)
    assert client.get_power() == -678.0
    assert session.calls == [("http://192.0.2.50/value", {"X-API-KEY": KEY}, 3)]
    assert client.health.success_count == 1


def test_a_port_other_than_80_is_part_of_the_address():
    session = Session(Response(_value(1)))
    create_grid_meter_client({**METER, "port": 8080}, session).get_power()

    assert session.calls[0][0] == "http://192.0.2.50:8080/value"


def test_the_reader_is_not_built_without_the_api_key():
    with pytest.raises(ValueError, match="grid_meter.token"):
        create_grid_meter_client({**METER, "token": ""}, Session())


def test_a_refused_key_keeps_the_last_value_and_says_why(caplog):
    session = Session(Response(_value(250)), Response({}, status_code=401))
    client = PowerfoxClient("192.0.2.50", session, KEY, clock=Clock())
    client.get_power()

    with caplog.at_level(logging.WARNING):
        assert client.get_power() == 250.0

    assert client.health.consecutive_failures == 1
    assert "PRO service" in client.health.last_error
    assert "powerfox_read_error" in caplog.text
    assert KEY not in caplog.text


def test_a_time_stamp_that_stops_moving_fails_the_reading():
    clock = Clock()
    session = Session(Response(_value(100, 5)), Response(_value(100, 5)), Response(_value(100, 5)), Response(_value(90, 6)))
    client = PowerfoxClient("192.0.2.50", session, KEY, clock=clock)

    client.get_power()
    clock.now += METER_FROZEN_AFTER_S
    client.get_power()
    assert client.health.consecutive_failures == 0
    clock.now += 1
    client.get_power()
    assert client.health.consecutive_failures == 1
    assert client.get_power() == 90.0
    assert client.health.consecutive_failures == 0


# --- the CLI setup assistant -------------------------------------------------------


def _menu_number(config_init):
    return str([choice for choice, _ in config_init.GRID_METER_CHOICES].index("powerfox_poweropti") + 1)


def test_config_init_asks_address_and_key_and_drops_foreign_keys(monkeypatch):
    from ems import config_init

    answers = iter([_menu_number(config_init), "192.0.2.50"])
    keys = iter(["", KEY])
    prompts = []
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr(config_init.getpass, "getpass", lambda prompt="": prompts.append(prompt) or next(keys))

    result = config_init.ask_grid_meter(
        {"type": "homewizard_p1", "ip": "192.0.2.9", "port": 443, "serial": "0a1b2c3d4e5f", "token": "0" * 32, "pairing_name": "local/x"}
    )

    assert result == METER
    assert "not set" in prompts[0] and len(prompts) == 2
    assert config_init._meter_summary(result) == "powerfox poweropti at 192.0.2.50, API key set"


def test_config_init_without_questions_keeps_a_complete_meter_and_refuses_one_without_key():
    from ems import config_init

    assert config_init.ask_grid_meter(METER, noninteractive=True) == METER
    with pytest.raises(config_init.ConfigInitError):
        config_init.ask_grid_meter({**METER, "token": ""}, noninteractive=True)


# --- diagnostics -------------------------------------------------------------------


def _codes(checks):
    return {check["code"]: check for check in checks}


@pytest.mark.parametrize(
    "grid_meter, level",
    [(METER, "ok"), ({**METER, "token": ""}, "error"), ({**METER, "ip": ""}, "error")],
)
def test_diagnose_config_uses_the_readers_own_check(grid_meter, level):
    checks = []
    diagnostics.diagnose_grid_meter_config(checks, {"grid_meter": grid_meter})

    codes = _codes(checks)
    code = "grid_meter_settings_complete" if level == "ok" else "grid_meter_settings_invalid"
    assert codes[code]["level"] == level
    assert codes["grid_meter_type"]["details"]["model"] == "powerfox poweropti"


@pytest.mark.parametrize(
    "answer, code",
    [(Response(_value(-678)), "grid_meter_read_ok"), (Response({}, status_code=401), "grid_meter_read_failed")],
)
def test_diagnose_reads_the_meter_once_with_the_ems_reader(monkeypatch, answer, code):
    session = Session(answer)
    monkeypatch.setattr("ems.clients.create_session", lambda: session)
    checks = []

    health = diagnostics.diagnose_hardware(checks, {"grid_meter": METER, "devices": []})

    codes = _codes(checks)
    assert code in codes
    assert len(session.calls) == 1
    assert session.closed
    assert KEY not in json.dumps(codes)
    assert health["grid_meter"]["provider"] == "powerfox poweropti"
