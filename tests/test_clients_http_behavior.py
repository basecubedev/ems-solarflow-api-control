import logging
from types import SimpleNamespace

import pytest

from ems.clients import (
    HAClient,
    ShellyClient,
    ZendureClient,
    create_session,
    _parse_shelly_power,
    zendure_write_succeeded,
)


class ResponseStub:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self.payload = payload
        self.text = text

    def json(self):
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


class SessionStub:
    def __init__(self, get_response=None, post_response=None):
        self.get_response = get_response or ResponseStub(payload={})
        self.post_response = post_response or ResponseStub(payload={})
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        if isinstance(self.get_response, BaseException):
            raise self.get_response
        return self.get_response

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        if isinstance(self.post_response, BaseException):
            raise self.post_response
        return self.post_response


def test_create_session_configures_retrying_http_adapters():
    session = create_session()

    retry = session.get_adapter("http://example.test").max_retries
    assert retry.total == 3
    assert retry.backoff_factor == 0.3
    assert set(retry.status_forcelist) == {500, 502, 503, 504}
    assert set(retry.allowed_methods) == {"GET", "POST"}

    assert session.get_adapter("https://example.test").max_retries.total == 3


def test_zendure_write_success_handles_success_and_logs_failure(caplog):
    dev = SimpleNamespace(name="WR1")

    assert zendure_write_succeeded(
        "zendure_write_error",
        dev,
        ResponseStub(status_code=204),
    ) is True

    caplog.set_level(logging.WARNING)
    result = zendure_write_succeeded(
        "zendure_write_error",
        dev,
        ResponseStub(status_code=500, text="x" * 250),
        command="set_output",
    )

    assert result is False
    assert "event=zendure_write_error" in caplog.text
    assert "device=WR1" in caplog.text
    assert "command=set_output" in caplog.text
    assert "status_code=500" in caplog.text


def test_ha_client_posts_state_payload_and_headers():
    session = SessionStub(post_response=ResponseStub(status_code=200))
    client = HAClient("http://ha.local/", "TOKEN", session)

    client.set_state(
        "sensor.ems",
        42,
        unit="W",
        device_class="power",
        state_class="measurement",
        icon="mdi:solar-power",
        extra_attributes={"source": "test"},
    )

    method, url, kwargs = session.calls[0]
    assert method == "post"
    assert url == "http://ha.local/api/states/sensor.ems"
    assert kwargs["headers"]["Authorization"] == "Bearer TOKEN"
    assert kwargs["timeout"] == 2
    assert kwargs["json"] == {
        "state": 42,
        "attributes": {
            "unit_of_measurement": "W",
            "device_class": "power",
            "state_class": "measurement",
            "icon": "mdi:solar-power",
            "source": "test",
        },
    }


def test_ha_client_read_helpers_tolerate_bad_or_unavailable_responses():
    client = HAClient(
        "http://ha.local",
        "TOKEN",
        SessionStub(get_response=ResponseStub(status_code=404)),
    )
    assert client.get_state("sensor.missing") is None
    assert client.get_float("sensor.missing", 12.5) == 12.5
    assert client.ping() is False

    client = HAClient(
        "http://ha.local",
        "TOKEN",
        SessionStub(get_response=ResponseStub(status_code=200, payload=[])),
    )
    assert client.get_state("sensor.bad") is None

    client = HAClient(
        "http://ha.local",
        "TOKEN",
        SessionStub(get_response=ConnectionError("offline")),
    )
    assert client.get_state("sensor.offline") is None
    assert client.ping() is False


def test_zendure_and_shelly_clients_parse_and_preserve_http_values():
    zendure_session = SessionStub(
        get_response=ResponseStub(
            payload={
                "properties": {
                    "electricLevel": 65,
                    "solarInputPower": 900,
                    "outputHomePower": 450,
                    "packInputPower": 120,
                    "outputLimit": 800,
                }
            }
        )
    )
    zendure = ZendureClient(
        "WR1",
        "192.0.2.10",
        "SN",
        zendure_session,
        min_soc=10,
        max_soc=100,
        smart_mode=1,
        grid_off_mode=None,
    )

    state = zendure.fetch()

    assert state.soc == 65
    assert state.solar == 900
    assert state.output == 450
    assert state.pack_in == 120
    assert state.output_limit == 800

    shelly = ShellyClient(
        "192.0.2.20",
        SessionStub(
            get_response=ResponseStub(
                payload={"em:0": {"total_act_power": 123.456}}
            )
        ),
    )
    assert shelly.get_power() == 123.5

    shelly.session = SessionStub(get_response=ValueError("offline"))
    assert shelly.get_power() == 123.5


def test_parse_shelly_power_supports_triphase_payload():
    assert _parse_shelly_power({"em:0": {"total_act_power": 321.5}}) == 321.5


def test_parse_shelly_power_supports_monophase_three_channels():
    assert _parse_shelly_power(
        {
            "em1:0": {"act_power": 100.0},
            "em1:1": {"act_power": 20.0},
            "em1:2": {"act_power": -5.0},
        }
    ) == 115.0


def test_parse_shelly_power_supports_monophase_single_channel():
    assert _parse_shelly_power({"em1:0": {"act_power": 42.0}}) == 42.0


def test_parse_shelly_power_rejects_unsupported_payload():
    with pytest.raises(ValueError, match="Unsupported Shelly status payload"):
        _parse_shelly_power({"wifi": {"sta_ip": "192.168.1.10"}})


def test_parse_shelly_power_rejects_payload_without_numeric_power_values():
    with pytest.raises(ValueError, match="Unsupported Shelly status payload"):
        _parse_shelly_power(
            {
                "em1:0": {"act_power": "42.0"},
                "em1:1": {"act_power": None},
            }
        )


def test_shelly_client_logs_parse_errors_and_keeps_last_value(caplog):
    client = ShellyClient(
        "192.0.2.20",
        SessionStub(
            get_response=ResponseStub(
                payload={"em:0": {"total_act_power": 123.456}}
            )
        ),
    )
    assert client.get_power() == 123.5

    caplog.set_level(logging.WARNING)
    client.session = SessionStub(
        get_response=ResponseStub(payload={"wifi": {"sta_ip": "192.168.1.10"}})
    )

    assert client.get_power() == 123.5
    assert "event=shelly_read_error" in caplog.text
    assert "stale_value=123.5" in caplog.text
