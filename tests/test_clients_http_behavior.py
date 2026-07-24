# SPDX-License-Identifier: AGPL-3.0-or-later
import logging
from types import SimpleNamespace

import pytest

from ems.clients import (
    EcoTrackerClient,
    HAClient,
    MqttGridMeterClient,
    ShellyClient,
    Shelly3EMGen1Client,
    TasmotaHttpClient,
    ZendureClient,
    ZendureGridMeterHttpClient,
    ZendureSmartMeter3CTHttpClient,
    create_grid_meter_client,
    create_session,
    _parse_ecotracker_power,
    _parse_shelly_3em_gen1_power,
    _parse_shelly_power,
    _parse_mqtt_grid_power_payload,
    _parse_tasmota_http_power,
    _parse_zendure_smartmeter_3ct_power,
    zendure_write,
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


class FakeMqttClient:
    def __init__(self):
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self.username_password = None
        self.connect_calls = []
        self.subscriptions = []
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.tls_set_called = False
        self.tls_set_calls = []
        self.tls_insecure = None
        self.published = []
        # TLS must be configured before connect; record ordering to prove it.
        self._connected = False
        self.tls_before_connect = None

    def username_pw_set(self, username, password=None):
        self.username_password = (username, password)

    def tls_set(self, *args, **kwargs):
        self.tls_set_called = True
        self.tls_set_calls.append((args, kwargs))

    def tls_insecure_set(self, value):
        self.tls_insecure = bool(value)

    def connect_async(self, host, port, keepalive=60):
        self.tls_before_connect = self.tls_set_called
        self.connect_calls.append((host, port, keepalive))
        self._connected = True

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self):
        self.loop_stopped = True

    def disconnect(self):
        self.disconnected = True

    def subscribe(self, topic):
        self.subscriptions.append(topic)

    def publish(self, *args, **kwargs):
        self.published.append((args, kwargs))


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
                    "inputLimit": 200,
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
    assert state.input_limit_w == 200

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


def test_zendure_client_defaults_missing_input_limit_to_zero():
    zendure = ZendureClient(
        "WR1",
        "192.0.2.10",
        "SN",
        SessionStub(
            get_response=ResponseStub(
                payload={"properties": {"electricLevel": 65}}
            )
        ),
        min_soc=10,
        max_soc=100,
        smart_mode=1,
        grid_off_mode=None,
    )

    state = zendure.fetch()

    assert state.input_limit_w == 0


def test_parse_shelly_power_supports_triphase_payload():
    assert _parse_shelly_power({"em:0": {"total_act_power": 321.5}}) == 321.5


def test_parse_shelly_power_default_prefers_aggregate():
    assert _parse_shelly_power(
        {
            "em:0": {"total_act_power": 321.5},
            "em1:0": {"act_power": 100.0},
            "em1:1": {"act_power": 20.0},
            "em1:2": {"act_power": -5.0},
        }
    ) == 321.5


def test_parse_shelly_power_default_falls_back_to_em1_sum():
    assert _parse_shelly_power(
        {
            "em1:0": {"act_power": 100.0},
            "em1:1": {"act_power": 20.0},
            "em1:2": {"act_power": -5.0},
        }
    ) == 115.0


def test_parse_shelly_power_supports_monophase_single_channel():
    assert _parse_shelly_power({"em1:0": {"act_power": 42.0}}) == 42.0


def test_parse_shelly_power_supports_single_selected_channel_list():
    assert _parse_shelly_power(
        {
            "em:0": {"total_act_power": 999.0},
            "em1:0": {"act_power": 100.0},
            "em1:1": {"act_power": 20.0},
            "em1:2": {"act_power": -5.0},
        },
        channels=["c"],
    ) == -5.0


def test_parse_shelly_power_supports_selected_channels_list():
    assert _parse_shelly_power(
        {
            "em:0": {"total_act_power": 999.0},
            "em1:0": {"act_power": 100.0},
            "em1:1": {"act_power": 20.0},
            "em1:2": {"act_power": -5.0},
        },
        channels=["a", "c"],
    ) == 95.0


def test_parse_shelly_power_supports_direct_em1_channels_list():
    assert _parse_shelly_power(
        {
            "em1:0": {"act_power": 100.0},
            "em1:1": {"act_power": 20.0},
            "em1:2": {"act_power": -5.0},
        },
        channels=["em1:0", "em1:2"],
    ) == 95.0


def test_parse_shelly_power_rejects_channels_string():
    with pytest.raises(ValueError, match="Shelly channels must be a list"):
        _parse_shelly_power({"em1:2": {"act_power": 42.0}}, channels="c")


@pytest.mark.parametrize("entry", ["total", "sum", "phase_d"])
def test_parse_shelly_power_rejects_unsupported_channels_entry(entry):
    with pytest.raises(ValueError, match="Unsupported Shelly channel in channels"):
        _parse_shelly_power({"em1:2": {"act_power": 42.0}}, channels=[entry])


def test_parse_shelly_power_rejects_empty_channels_entry():
    with pytest.raises(
        ValueError,
        match="Shelly channels must not contain empty values",
    ):
        _parse_shelly_power({"em1:2": {"act_power": 42.0}}, channels=[""])


def test_parse_shelly_power_rejects_missing_selected_channel():
    with pytest.raises(ValueError, match="Unsupported Shelly status payload"):
        _parse_shelly_power(
            {
                "em1:0": {"act_power": 100.0},
                "em1:1": {"act_power": 20.0},
            },
            channels=["c"],
        )


def test_parse_shelly_power_rejects_non_numeric_selected_channel():
    with pytest.raises(ValueError, match="Unsupported Shelly status payload"):
        _parse_shelly_power({"em1:2": {"act_power": "42.0"}}, channels=["c"])


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


def test_parse_shelly_3em_gen1_power_prefers_total_power():
    assert _parse_shelly_3em_gen1_power(
        {
            "total_power": 321.5,
            "emeters": [
                {"power": 100.0},
                {"power": 20.0},
                {"power": -5.0},
            ],
        }
    ) == 321.5


def test_parse_shelly_3em_gen1_power_sums_emeters_without_total():
    assert _parse_shelly_3em_gen1_power(
        {
            "emeters": [
                {"power": 100.0},
                {"power": 20.0},
                {"power": -5.0},
            ]
        }
    ) == 115.0


def test_parse_shelly_3em_gen1_power_supports_em_two_channel_payload():
    assert _parse_shelly_3em_gen1_power(
        {
            "emeters": [
                {"power": 40.0},
                {"power": 2.5},
            ]
        }
    ) == 42.5


def test_parse_shelly_3em_gen1_power_supports_named_channels():
    data = {
        "total_power": 999.0,
        "emeters": [
            {"power": 100.0},
            {"power": 20.0},
            {"power": -5.0},
        ],
    }
    assert _parse_shelly_3em_gen1_power(data, channels=["a"]) == 100.0
    assert _parse_shelly_3em_gen1_power(data, channels=["b"]) == 20.0
    assert _parse_shelly_3em_gen1_power(data, channels=["c"]) == -5.0
    assert _parse_shelly_3em_gen1_power(data, channels=["a", "c"]) == 95.0


def test_parse_shelly_3em_gen1_power_normalizes_uppercase_channels():
    data = {
        "emeters": [
            {"power": 100.0},
            {"power": 20.0},
            {"power": -5.0},
        ]
    }
    assert _parse_shelly_3em_gen1_power(data, channels=["A"]) == 100.0
    assert _parse_shelly_3em_gen1_power(data, channels=["B"]) == 20.0
    assert _parse_shelly_3em_gen1_power(data, channels=["C"]) == -5.0
    assert _parse_shelly_3em_gen1_power(data, channels=["A", "C"]) == 95.0


def test_parse_shelly_3em_gen1_power_ignores_total_power_when_channels_given():
    data = {
        "total_power": 999.0,
        "emeters": [
            {"power": 100.0},
            {"power": 20.0},
            {"power": -5.0},
        ],
    }
    # Selecting a single phase must not return the all-clamp total_power.
    assert _parse_shelly_3em_gen1_power(data, channels=["a"]) == 100.0
    assert _parse_shelly_3em_gen1_power(data, channels=["a", "b"]) == 120.0


def test_parse_shelly_3em_gen1_power_supports_emeter_index_channels():
    data = {
        "emeters": [
            {"power": 100.0},
            {"power": 20.0},
            {"power": -5.0},
        ]
    }
    assert _parse_shelly_3em_gen1_power(data, channels=["emeter:0"]) == 100.0
    assert _parse_shelly_3em_gen1_power(data, channels=["emeter:1"]) == 20.0
    assert _parse_shelly_3em_gen1_power(data, channels=["emeter:2"]) == -5.0


def test_parse_shelly_3em_gen1_power_supports_numeric_channels():
    data = {
        "emeters": [
            {"power": 100.0},
            {"power": 20.0},
            {"power": -5.0},
        ]
    }
    assert _parse_shelly_3em_gen1_power(data, channels=["0"]) == 100.0
    assert _parse_shelly_3em_gen1_power(data, channels=["1"]) == 20.0
    assert _parse_shelly_3em_gen1_power(data, channels=["2"]) == -5.0


def test_parse_shelly_3em_gen1_power_rejects_missing_selected_channel():
    with pytest.raises(ValueError, match="missing numeric emeters"):
        _parse_shelly_3em_gen1_power(
            {"emeters": [{"power": 100.0}]},
            channels=["c"],
        )


def test_parse_shelly_3em_gen1_power_rejects_non_numeric_channel():
    with pytest.raises(ValueError, match="missing numeric emeters"):
        _parse_shelly_3em_gen1_power(
            {"emeters": [{"power": "100.0"}]},
            channels=["a"],
        )


def test_parse_shelly_3em_gen1_power_rejects_boolean_power():
    with pytest.raises(ValueError, match="Unsupported Shelly 3EM Gen1 status payload"):
        _parse_shelly_3em_gen1_power(
            {"emeters": [{"power": True}, {"power": False}]}
        )


def test_parse_shelly_3em_gen1_power_rejects_unsupported_channel_entry():
    with pytest.raises(ValueError, match="Unsupported Shelly 3EM Gen1 channel"):
        _parse_shelly_3em_gen1_power(
            {"emeters": [{"power": 100.0}]},
            channels=["total"],
        )


def test_parse_shelly_3em_gen1_power_rejects_invalid_payload_type():
    with pytest.raises(ValueError, match="expected object"):
        _parse_shelly_3em_gen1_power([{"power": 100.0}])


def test_shelly_3em_gen1_client_reads_status_and_preserves_last_value(caplog):
    client = Shelly3EMGen1Client(
        "192.0.2.30",
        SessionStub(
            get_response=ResponseStub(payload={"total_power": 123.456})
        ),
    )
    assert client.get_power() == 123.5
    assert client.session.calls[0][1] == "http://192.0.2.30/status"

    caplog.set_level(logging.WARNING)
    client.session = SessionStub(
        get_response=ResponseStub(payload={"wifi": {"sta_ip": "192.168.1.10"}})
    )

    assert client.get_power() == 123.5
    assert "event=shelly_3em_gen1_read_error" in caplog.text
    assert "stale_value=123.5" in caplog.text


def test_shelly_3em_gen1_client_passes_configured_channels_to_parser():
    client = Shelly3EMGen1Client(
        "192.0.2.30",
        SessionStub(
            get_response=ResponseStub(
                payload={
                    "total_power": 999.0,
                    "emeters": [
                        {"power": 100.0},
                        {"power": 20.0},
                        {"power": -5.0},
                    ],
                }
            )
        ),
        channels=["a", "c"],
    )
    assert client.channels == ["a", "c"]
    # total_power (999) is ignored because channels are configured.
    assert client.get_power() == 95.0
    assert client.session.calls[0][1] == "http://192.0.2.30/status"


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


def test_shelly_client_uses_configured_channels_single_item_and_keeps_last_value(
    caplog,
):
    client = ShellyClient(
        "192.0.2.20",
        SessionStub(
            get_response=ResponseStub(
                payload={
                    "em:0": {"total_act_power": 321.5},
                    "em1:0": {"act_power": 100.0},
                    "em1:1": {"act_power": 20.0},
                    "em1:2": {"act_power": -5.0},
                }
            )
        ),
        channels=["c"],
    )
    assert client.get_power() == -5.0

    caplog.set_level(logging.WARNING)
    client.session = SessionStub(
        get_response=ResponseStub(payload={"em1:0": {"act_power": 100.0}})
    )

    assert client.get_power() == -5.0
    assert "event=shelly_read_error" in caplog.text
    assert "channels=['c']" in caplog.text
    assert "stale_value=-5.0" in caplog.text


def test_shelly_client_uses_configured_channels_and_keeps_last_value(caplog):
    client = ShellyClient(
        "192.0.2.20",
        SessionStub(
            get_response=ResponseStub(
                payload={
                    "em:0": {"total_act_power": 321.5},
                    "em1:0": {"act_power": 100.0},
                    "em1:1": {"act_power": 20.0},
                    "em1:2": {"act_power": -5.0},
                }
            )
        ),
        channels=["a", "c"],
    )
    assert client.get_power() == 95.0

    caplog.set_level(logging.WARNING)
    client.session = SessionStub(
        get_response=ResponseStub(payload={"em1:0": {"act_power": 100.0}})
    )

    assert client.get_power() == 95.0
    assert "event=shelly_read_error" in caplog.text
    assert "channels=['a', 'c']" in caplog.text
    assert "stale_value=95.0" in caplog.text


def test_parse_ecotracker_power_supports_minimal_payload():
    assert _parse_ecotracker_power({"power": 830}) == 830.0


def test_parse_ecotracker_power_supports_full_payload_with_optional_fields():
    assert _parse_ecotracker_power(
        {
            "power": -125,
            "powerAvg": -100,
            "powerPhase1": -50,
            "powerPhase2": -50,
            "powerPhase3": -25,
            "energyCounterIn": 145000,
            "energyCounterOut": 4500,
        }
    ) == -125.0


def test_parse_ecotracker_power_rejects_missing_numeric_power():
    with pytest.raises(ValueError, match="Unsupported EcoTracker payload"):
        _parse_ecotracker_power({"power": "125"})


def test_ecotracker_client_reads_v1_json_and_preserves_last_value():
    client = EcoTrackerClient(
        "192.0.2.30",
        SessionStub(get_response=ResponseStub(payload={"power": 123.456})),
    )

    assert client.get_power() == 123.5
    assert client.session.calls[0][1] == "http://192.0.2.30/v1/json"

    client.session = SessionStub(get_response=ValueError("offline"))
    assert client.get_power() == 123.5


def test_create_grid_meter_client_supports_shelly_and_ecotracker():
    shelly = create_grid_meter_client(
        {"type": "shelly", "ip": "192.0.2.1"},
        SessionStub(),
    )
    assert isinstance(shelly, ShellyClient)
    assert shelly.channels is None

    assert isinstance(
        create_grid_meter_client(
            {"type": "ecotracker", "ip": "192.0.2.2"},
            SessionStub(),
        ),
        EcoTrackerClient,
    )


def test_create_grid_meter_client_supports_shelly_channels_list():
    shelly = create_grid_meter_client(
        {"type": "shelly", "ip": "192.0.2.1", "channels": ["a", "c"]},
        SessionStub(),
    )
    assert isinstance(shelly, ShellyClient)
    assert shelly.channels == ["a", "c"]


def test_create_grid_meter_client_supports_shelly_3em_gen1():
    gen1 = create_grid_meter_client(
        {"type": "shelly_3em_gen1", "ip": "192.0.2.50"},
        SessionStub(),
    )
    assert isinstance(gen1, Shelly3EMGen1Client)
    assert not isinstance(gen1, ShellyClient)
    assert gen1.channels is None


def test_create_grid_meter_client_shelly_still_returns_pro_client():
    shelly = create_grid_meter_client(
        {"type": "shelly", "ip": "192.0.2.1"},
        SessionStub(),
    )
    assert isinstance(shelly, ShellyClient)
    assert not isinstance(shelly, Shelly3EMGen1Client)


def test_create_grid_meter_client_supports_shelly_3em_gen1_channels_list():
    gen1 = create_grid_meter_client(
        {"type": "shelly_3em_gen1", "ip": "192.0.2.50", "channels": ["a", "c"]},
        SessionStub(),
    )
    assert isinstance(gen1, Shelly3EMGen1Client)
    assert gen1.channels == ["a", "c"]


def test_parse_tasmota_http_power_sml():
    data = {
        "StatusSNS": {
            "SML": {
                "Power_curr": 538
            }
        }
    }

    assert _parse_tasmota_http_power(data, "StatusSNS.SML.Power_curr") == 538.0


def test_parse_tasmota_http_power_obis_key():
    data = {
        "StatusSNS": {
            "SM": {
                "16_7_0": -99.43
            }
        }
    }

    assert _parse_tasmota_http_power(data, "StatusSNS.SM.16_7_0") == -99.43


def test_parse_tasmota_http_power_missing_path():
    data = {"StatusSNS": {"SML": {}}}

    with pytest.raises(ValueError, match="Missing JSON path"):
        _parse_tasmota_http_power(data, "StatusSNS.SML.Power_curr")


def test_parse_tasmota_http_power_rejects_non_numeric():
    data = {"StatusSNS": {"SML": {"Power_curr": "538"}}}

    with pytest.raises(ValueError, match="Tasmota power path is not numeric"):
        _parse_tasmota_http_power(data, "StatusSNS.SML.Power_curr")


def test_parse_tasmota_http_power_rejects_boolean():
    data = {"StatusSNS": {"SML": {"Power_curr": True}}}

    with pytest.raises(ValueError, match="Tasmota power path is not numeric"):
        _parse_tasmota_http_power(data, "StatusSNS.SML.Power_curr")


def test_tasmota_http_client_reads_json_and_preserves_last_value():
    client = TasmotaHttpClient(
        "http://192.0.2.40/cm?cmnd=Status%2010",
        "StatusSNS.SML.Power_curr",
        SessionStub(
            get_response=ResponseStub(
                payload={"StatusSNS": {"SML": {"Power_curr": 538.44}}}
            )
        ),
    )

    assert client.get_power() == 538.4
    assert client.session.calls[0][1] == "http://192.0.2.40/cm?cmnd=Status%2010"

    client.session = SessionStub(
        get_response=ResponseStub(payload={"StatusSNS": {"SML": {}}})
    )
    assert client.get_power() == 538.4


def test_create_grid_meter_client_supports_tasmota_http_url_and_ip_shorthand():
    from_url = create_grid_meter_client(
        {
            "type": "tasmota_http",
            "url": "http://192.0.2.40/cm?cmnd=Status%2010",
            "power_path": "StatusSNS.SML.Power_curr",
        },
        SessionStub(),
    )
    assert isinstance(from_url, TasmotaHttpClient)
    assert from_url.url == "http://192.0.2.40/cm?cmnd=Status%2010"
    assert from_url.power_path == "StatusSNS.SML.Power_curr"

    from_ip = create_grid_meter_client(
        {
            "type": "tasmota_http",
            "ip": "192.0.2.41",
            "power_path": "StatusSNS.SM.16_7_0",
        },
        SessionStub(),
    )
    assert isinstance(from_ip, TasmotaHttpClient)
    assert from_ip.url == "http://192.0.2.41/cm?cmnd=Status%2010"
    assert from_ip.power_path == "StatusSNS.SM.16_7_0"


def test_create_grid_meter_client_rejects_tasmota_http_missing_config():
    with pytest.raises(ValueError, match="requires power_path"):
        create_grid_meter_client(
            {"type": "tasmota_http", "ip": "192.0.2.41"},
            SessionStub(),
        )

    with pytest.raises(ValueError, match="requires url or ip"):
        create_grid_meter_client(
            {"type": "tasmota_http", "power_path": "StatusSNS.SML.Power_curr"},
            SessionStub(),
        )


def test_parse_zendure_smartmeter_3ct_power_accepts_example_payload():
    data = {
        "timestamp": 1783163312,
        "messageId": 12,
        "deviceId": "rhRkw909",
        "a_aprt_power": 0,
        "b_aprt_power": 0,
        "c_aprt_power": -798,
        "total_power": -798,
    }

    assert _parse_zendure_smartmeter_3ct_power(data) == -798.0


def test_parse_zendure_smartmeter_3ct_power_rejects_missing_total_power():
    with pytest.raises(ValueError, match="missing numeric total_power"):
        _parse_zendure_smartmeter_3ct_power({"a_aprt_power": 10})


def test_parse_zendure_smartmeter_3ct_power_rejects_non_numeric():
    with pytest.raises(ValueError, match="missing numeric total_power"):
        _parse_zendure_smartmeter_3ct_power({"total_power": "-798"})


def test_parse_zendure_smartmeter_3ct_power_rejects_boolean():
    with pytest.raises(ValueError, match="missing numeric total_power"):
        _parse_zendure_smartmeter_3ct_power({"total_power": True})


def test_parse_zendure_smartmeter_3ct_power_rejects_non_object():
    with pytest.raises(ValueError, match="expected object"):
        _parse_zendure_smartmeter_3ct_power([{"total_power": -798}])


def test_zendure_grid_meter_http_client_reads_report_and_preserves_last_value():
    client = ZendureGridMeterHttpClient(
        "192.0.2.80",
        SessionStub(
            get_response=ResponseStub(payload={"total_power": -798.44})
        ),
    )

    assert client.get_power() == -798.4
    assert client.session.calls[0][1] == "http://192.0.2.80:80/properties/report"

    client.session = SessionStub(get_response=ValueError("offline"))
    assert client.get_power() == -798.4


def test_zendure_grid_meter_http_client_uses_discovered_port():
    client = ZendureGridMeterHttpClient(
        "192.0.2.80",
        SessionStub(get_response=ResponseStub(payload={"total_power": -43})),
        port=8080,
    )
    assert client.get_power() == -43.0
    assert client.session.calls[0][1] == "http://192.0.2.80:8080/properties/report"


def test_zendure_grid_meter_http_client_logs_read_error(caplog):
    client = ZendureGridMeterHttpClient(
        "192.0.2.80",
        SessionStub(get_response=ResponseStub(payload={"total_power": 120.0})),
    )
    assert client.get_power() == 120.0

    caplog.set_level(logging.WARNING)
    client.session = SessionStub(get_response=ResponseStub(payload={"foo": 1}))

    assert client.get_power() == 120.0
    assert "event=zendure_grid_meter_http_read_error" in caplog.text
    assert "stale_value=120.0" in caplog.text


def test_zendure_smartmeter_3ct_http_client_is_a_backward_compatible_alias():
    assert ZendureSmartMeter3CTHttpClient is ZendureGridMeterHttpClient


@pytest.mark.parametrize(
    "meter_type",
    [
        "zendure_grid_meter_http",
        "zendure_smartmeter_3ct_http",
        "zendure_smartmeter_d0_http",
    ],
)
def test_create_grid_meter_client_supports_zendure_http_types(meter_type):
    client = create_grid_meter_client(
        {"type": meter_type, "ip": "192.0.2.80"},
        SessionStub(),
    )
    assert isinstance(client, ZendureGridMeterHttpClient)
    assert client.ip == "192.0.2.80"
    assert client.port == 80
    assert client.provider == "Zendure Grid Meter (HTTP)"


def test_d0_http_and_3ct_http_use_the_same_shared_reader():
    # The D0 local-API meter must reuse the shared Zendure local-HTTP reader
    # rather than gain a second near-identical client. Both types build the same
    # class from the same factory and parse total_power identically.
    from ems.clients import _parse_zendure_grid_meter_http_power

    d0 = create_grid_meter_client(
        {"type": "zendure_smartmeter_d0_http", "ip": "192.0.2.81"},
        SessionStub(get_response=ResponseStub(payload={"total_power": 512.0})),
    )
    ct = create_grid_meter_client(
        {"type": "zendure_smartmeter_3ct_http", "ip": "192.0.2.82"},
        SessionStub(get_response=ResponseStub(payload={"total_power": 512.0})),
    )
    assert type(d0) is type(ct) is ZendureGridMeterHttpClient
    # total_power is the single value both read at /properties/report.
    assert _parse_zendure_grid_meter_http_power({"total_power": 512.0}) == 512.0


def test_d0_http_reader_preserves_grid_power_sign_semantics():
    # Positive total_power stays import (grid draw); negative stays export
    # (feed-in). The D0 local-API meter is read-only regardless of transport.
    imp = create_grid_meter_client(
        {"type": "zendure_smartmeter_d0_http", "ip": "192.0.2.83"},
        SessionStub(get_response=ResponseStub(payload={"total_power": 240.0})),
    )
    assert imp.get_power() == 240.0

    exp = create_grid_meter_client(
        {"type": "zendure_smartmeter_d0_http", "ip": "192.0.2.83"},
        SessionStub(get_response=ResponseStub(payload={"total_power": -180.0})),
    )
    assert exp.get_power() == -180.0
    # The shared reader carries no writer surface.
    assert not hasattr(exp, "write_output_limit")


def test_create_grid_meter_client_preserves_zendure_http_port():
    client = create_grid_meter_client(
        {"type": "zendure_grid_meter_http", "ip": "192.0.2.80", "port": 8080},
        SessionStub(),
    )
    assert client.port == 8080


def test_create_grid_meter_client_rejects_zendure_http_missing_ip():
    with pytest.raises(ValueError, match="requires ip"):
        create_grid_meter_client(
            {"type": "zendure_grid_meter_http"},
            SessionStub(),
        )


def test_parse_mqtt_grid_power_payload_accepts_number_and_json():
    assert _parse_mqtt_grid_power_payload(b"-6") == -6.0
    assert _parse_mqtt_grid_power_payload("1.5") == 1.5
    assert _parse_mqtt_grid_power_payload(
        b'{"power": {"total": 42}}',
        payload_format="json",
        value_path="power.total",
    ) == 42.0


def test_parse_mqtt_grid_power_payload_rejects_invalid_values():
    with pytest.raises(ValueError, match="not numeric"):
        _parse_mqtt_grid_power_payload(b"OFF")

    with pytest.raises(ValueError, match="Missing JSON path"):
        _parse_mqtt_grid_power_payload(
            b'{"power": {}}',
            payload_format="json",
            value_path="power.total",
        )

    with pytest.raises(ValueError, match="Unsupported MQTT payload_format"):
        _parse_mqtt_grid_power_payload(b"1", payload_format="xml")


def test_mqtt_grid_meter_client_subscribes_and_returns_latest_value():
    fake = FakeMqttClient()
    client = MqttGridMeterClient(
        "mqtt.local",
        1883,
        "Zendure/sensor/SN/totalPower",
        username="user",
        password="secret",
        client_factory=lambda: fake,
    )

    assert fake.username_password == ("user", "secret")
    assert fake.connect_calls == [("mqtt.local", 1883, 30)]
    assert fake.loop_started is True

    fake.on_connect(fake, None, None, 0)
    assert fake.subscriptions == ["Zendure/sensor/SN/totalPower"]

    fake.on_message(
        fake,
        None,
        SimpleNamespace(topic="Zendure/sensor/SN/totalPower", payload=b"-6"),
    )

    assert client.get_power() == -6.0
    assert client.health.success_count == 1

    client.close()
    assert fake.loop_stopped is True
    assert fake.disconnected is True


def test_mqtt_grid_meter_client_reports_missing_and_stale_values():
    fake = FakeMqttClient()
    client = MqttGridMeterClient(
        "mqtt.local",
        1883,
        "meter/grid",
        max_age_seconds=1,
        client_factory=lambda: fake,
    )

    assert client.get_power() == 0
    assert client.health.failure_count == 1

    fake.on_message(
        fake,
        None,
        SimpleNamespace(topic="meter/grid", payload=b"12"),
    )
    client.last_message_monotonic = 0

    assert client.get_power() == 12.0
    assert client.health.failure_count == 2


def test_mqtt_grid_meter_client_configures_tls_before_connect():
    fake = FakeMqttClient()
    MqttGridMeterClient(
        "mqtt.local",
        8883,
        "Zendure/sensor/SN/totalPower",
        tls=True,
        client_factory=lambda: fake,
    )
    assert fake.tls_set_called is True
    # Certificate verification stays on unless explicitly disabled.
    assert fake.tls_set_calls == [((), {})]
    assert fake.tls_insecure is None
    # TLS is applied before the connection is opened.
    assert fake.tls_before_connect is True


def test_mqtt_grid_meter_client_tls_insecure_only_when_enabled():
    import ssl

    fake = FakeMqttClient()
    MqttGridMeterClient(
        "mqtt.local",
        8883,
        "Zendure/sensor/SN/totalPower",
        tls=True,
        tls_insecure=True,
        client_factory=lambda: fake,
    )
    assert fake.tls_set_called is True
    assert fake.tls_insecure is True
    # Insecure must skip chain verification too (self-signed broker chains),
    # not only the hostname check.
    assert fake.tls_set_calls == [((), {"cert_reqs": ssl.CERT_NONE})]


def test_mqtt_grid_meter_client_without_tls_does_not_configure_tls():
    fake = FakeMqttClient()
    MqttGridMeterClient(
        "mqtt.local",
        1883,
        "Zendure/sensor/SN/totalPower",
        client_factory=lambda: fake,
    )
    assert fake.tls_set_called is False
    assert fake.tls_insecure is None


def test_mqtt_grid_meter_client_never_publishes():
    fake = FakeMqttClient()
    client = MqttGridMeterClient(
        "mqtt.local",
        1883,
        "Zendure/sensor/SN/totalPower",
        client_factory=lambda: fake,
    )
    fake.on_connect(fake, None, None, 0)
    fake.on_message(
        fake,
        None,
        SimpleNamespace(topic="Zendure/sensor/SN/totalPower", payload=b"-6"),
    )
    client.get_power()
    client.close()
    assert fake.published == []


def test_mqtt_grid_meter_client_malformed_payload_keeps_previous_value():
    fake = FakeMqttClient()
    client = MqttGridMeterClient(
        "mqtt.local",
        1883,
        "Zendure/sensor/SN/totalPower",
        client_factory=lambda: fake,
    )
    fake.on_connect(fake, None, None, 0)
    fake.on_message(
        fake,
        None,
        SimpleNamespace(topic="Zendure/sensor/SN/totalPower", payload=b"-6"),
    )
    assert client.get_power() == -6.0
    before = client.health.failure_count
    fake.on_message(
        fake,
        None,
        SimpleNamespace(topic="Zendure/sensor/SN/totalPower", payload=b"OFF"),
    )
    # The last good value is preserved and a parse failure is recorded.
    assert client.last_value == -6.0
    assert client.health.failure_count == before + 1


def test_create_grid_meter_client_applies_tls_from_config():
    fake = FakeMqttClient()
    create_grid_meter_client(
        {
            "type": "zendure_smartmeter_d0",
            "mqtt": {
                "host": "mqtt.local",
                "port": 8883,
                "topic": "Zendure/sensor/SN/totalPower",
                "payload_format": "number",
                "tls": True,
                "tls_insecure": False,
                "_mqtt_client_factory": lambda: fake,
            },
        },
        SessionStub(),
    )
    assert fake.tls_set_called is True
    assert fake.tls_insecure is None


def test_create_grid_meter_client_supports_mqtt_with_factory():
    fake = FakeMqttClient()
    client = create_grid_meter_client(
        {
            "type": "mqtt",
            "mqtt": {
                "host": "mqtt.local",
                "port": 1883,
                "topic": "meter/grid",
                "payload_format": "number",
                "max_age_seconds": 15,
                "_mqtt_client_factory": lambda: fake,
            },
        },
        SessionStub(),
    )

    assert isinstance(client, MqttGridMeterClient)
    assert client.endpoint == "mqtt.local:1883 meter/grid"
    assert client.provider == "MQTT"


def test_create_grid_meter_client_supports_zendure_smartmeter_d0_preset():
    fake = FakeMqttClient()
    client = create_grid_meter_client(
        {
            "type": "zendure_smartmeter_d0",
            "mqtt": {
                "host": "mqtt.local",
                "port": 1883,
                "topic": "Zendure/sensor/SN/totalPower",
                "payload_format": "number",
                "_mqtt_client_factory": lambda: fake,
            },
        },
        SessionStub(),
    )

    assert isinstance(client, MqttGridMeterClient)
    assert client.provider == "Zendure SmartMeter D0"
    assert client.transport == "mqtt"
    assert client.endpoint == "mqtt.local:1883 Zendure/sensor/SN/totalPower"
    fake.on_message(
        fake,
        None,
        SimpleNamespace(topic="Zendure/sensor/SN/totalPower", payload=b"-6"),
    )
    assert client.get_power() == -6.0


def test_create_grid_meter_client_rejects_mqtt_missing_config():
    with pytest.raises(ValueError, match="requires host"):
        create_grid_meter_client({"type": "mqtt", "topic": "meter/grid"}, SessionStub())

    with pytest.raises(ValueError, match="requires topic"):
        create_grid_meter_client({"type": "mqtt", "host": "mqtt.local"}, SessionStub())


def test_create_grid_meter_client_rejects_unknown_type():
    with pytest.raises(ValueError, match="Unsupported grid meter type"):
        create_grid_meter_client({"type": "unknown", "ip": "192.0.2.3"}, SessionStub())


# =====================
# COMMUNICATION HEALTH
# =====================


def _zendure_client(session):
    return ZendureClient(
        "WR1",
        "192.0.2.20",
        "SN-WR1",
        session,
        10,
        90,
        1,
        0,
    )


def test_shelly_read_success_updates_health():
    client = ShellyClient(
        "192.0.2.10",
        SessionStub(
            get_response=ResponseStub(payload={"em:0": {"total_act_power": 120.0}})
        ),
    )

    assert client.get_power() == 120.0
    assert client.health.success_count == 1
    assert client.health.consecutive_failures == 0
    assert client.health.last_latency_ms is not None
    assert client.health.classify() == "ok"


def test_shelly_read_failure_keeps_stale_value_and_tracks_health():
    client = ShellyClient("192.0.2.10", SessionStub(get_response=ConnectionError("offline")))
    client.last_value = 55.0

    assert client.get_power() == 55.0
    assert client.health.failure_count == 1
    assert client.health.consecutive_failures == 1
    assert client.health.stale_used is True

    # A later success resets the consecutive-failure counter.
    client.session = SessionStub(
        get_response=ResponseStub(payload={"em:0": {"total_act_power": 10.0}})
    )
    assert client.get_power() == 10.0
    assert client.health.consecutive_failures == 0
    assert client.health.stale_used is False


def test_zendure_fetch_updates_read_health_on_success_and_failure():
    client = _zendure_client(
        SessionStub(
            get_response=ResponseStub(payload={"properties": {"electricLevel": 50}})
        )
    )
    assert client.fetch() is not None
    assert client.read_health.success_count == 1

    client.session = SessionStub(get_response=ConnectionError("down"))
    assert client.fetch() is None
    assert client.read_health.failure_count == 1
    assert client.read_health.consecutive_failures == 1


def test_zendure_write_updates_write_health_without_touching_read_health():
    client = _zendure_client(SessionStub(post_response=ResponseStub(status_code=200)))

    assert zendure_write(
        client,
        "outputLimit",
        {"outputLimit": 100},
        "write_output_limit_error",
        target_w=100,
    ) is True
    assert client.write_health.success_count == 1
    assert client.write_health.last_field == "outputLimit"
    assert client.read_health.attempted is False


def test_zendure_write_failure_increments_write_health():
    client = _zendure_client(
        SessionStub(post_response=ResponseStub(status_code=500, text="boom"))
    )

    assert zendure_write(
        client,
        "outputLimit",
        {"outputLimit": 100},
        "write_output_limit_error",
        target_w=100,
    ) is False
    assert client.write_health.failure_count == 1
    assert client.write_health.consecutive_failures == 1


def test_zendure_write_transport_error_records_failure_and_reraises():
    client = _zendure_client(SessionStub(post_response=ConnectionError("down")))

    with pytest.raises(ConnectionError):
        zendure_write(
            client,
            "outputLimit",
            {"outputLimit": 100},
            "write_output_limit_error",
        )
    assert client.write_health.failure_count == 1
    assert client.write_health.consecutive_failures == 1
