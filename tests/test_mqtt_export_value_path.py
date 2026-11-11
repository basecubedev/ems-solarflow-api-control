# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An MQTT meter whose JSON carries export as a value of its own.

A French Linky read by a ZLinky_TIC through Zigbee2MQTT publishes the apparent
power drawn (``apparent_power``, SINSTS) and injected (``injected_v_a``,
SINSTI) side by side, both positive and in VA; the field names are those of the
Zigbee2MQTT device page. The generic MQTT meter subtracts the export path, and
refuses a message without it until one has carried it.
"""

import pytest

from ems import config as cfg
from ems.clients import _parse_mqtt_grid_power_payload, create_grid_meter_client

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.power_control,
    pytest.mark.unit,
]

ZLINKY = {
    "type": "mqtt",
    "mqtt": {
        "host": "192.0.2.70",
        "topic": "zigbee2mqtt/linky",
        "payload_format": "json",
        "value_path": "apparent_power",
        "export_value_path": "injected_v_a",
    },
}


class FakeMqttClient:
    def username_pw_set(self, username, password):
        self.auth = (username, password)

    def connect_async(self, *_args, **_kwargs):
        return None

    def loop_start(self):
        return None


class Message:
    topic = "zigbee2mqtt/linky"

    def __init__(self, payload):
        self.payload = payload


@pytest.mark.parametrize(
    "payload, power",
    [
        (b'{"apparent_power": 0, "injected_v_a": 450, "linkquality": 120}', -450.0),
        (b'{"apparent_power": 1200, "injected_v_a": 0}', 1200.0),
        (b'{"apparent_power": "830", "injected_v_a": "30"}', 800.0),
    ],
)
def test_export_is_subtracted(payload, power):
    assert _parse_mqtt_grid_power_payload(payload, payload_format="json", value_path="apparent_power", export_value_path="injected_v_a") == power


def test_a_message_without_the_export_value_counts_as_no_export_only_once_one_had_it():
    payload = b'{"apparent_power": 1200}'
    with pytest.raises(ValueError, match="no export value at injected_v_a yet"):
        _parse_mqtt_grid_power_payload(payload, payload_format="json", value_path="apparent_power", export_value_path="injected_v_a")

    assert _parse_mqtt_grid_power_payload(payload, payload_format="json", value_path="apparent_power", export_value_path="injected_v_a", missing_export_is_zero=True) == 1200.0


def test_a_nested_export_path_and_a_broken_export_value():
    payload = b'{"meter": {"in": 100, "out": 40}}'
    assert _parse_mqtt_grid_power_payload(payload, payload_format="json", value_path="meter.in", export_value_path="meter.out") == 60.0
    with pytest.raises(ValueError, match="not numeric"):
        _parse_mqtt_grid_power_payload(b'{"in": 1, "out": "n/a"}', payload_format="json", value_path="in", export_value_path="out")


def test_a_number_payload_has_no_export_path():
    settings = cfg.normalize_mqtt_grid_meter_settings({"type": "mqtt", "mqtt": {**ZLINKY["mqtt"], "payload_format": "number"}})

    assert "value_path" not in settings and "export_value_path" not in settings
    assert cfg.normalize_mqtt_grid_meter_settings(ZLINKY)["export_value_path"] == "injected_v_a"


def test_the_meter_reads_a_zlinky_message_as_grid_power():
    config = {**ZLINKY, "mqtt": {**ZLINKY["mqtt"], "_mqtt_client_factory": FakeMqttClient}}
    client = create_grid_meter_client(config, None)

    client._on_message(None, None, Message(b'{"apparent_power": 900}'))
    assert client.last_message_monotonic is None
    assert "injected_v_a" in client.health.last_error

    client._on_message(None, None, Message(b'{"apparent_power": 0, "injected_v_a": 310}'))
    assert client.last_value == -310.0
    client._on_message(None, None, Message(b'{"apparent_power": 700}'))
    assert client.last_value == 700.0


def test_config_init_asks_for_an_optional_export_path(monkeypatch):
    from ems import config_init

    menu = str([choice for choice, _ in config_init.GRID_METER_CHOICES].index("mqtt") + 1)
    answers = iter([menu, "192.0.2.70", "", "", "zigbee2mqtt/linky", "json", "apparent_power", "injected_v_a", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr(config_init.getpass, "getpass", lambda _prompt="": "")

    result = config_init.ask_grid_meter({"type": "shelly", "ip": "192.0.2.9"})

    mqtt = cfg.grid_meter_mqtt_settings(result)
    assert (mqtt["value_path"], mqtt["export_value_path"]) == ("apparent_power", "injected_v_a")
