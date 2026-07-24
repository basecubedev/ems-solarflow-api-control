# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only MQTT topic discovery tests (no real broker or network)."""

import json
import ssl
import sys
import types

import pytest

from admin.mqtt_topic_discovery import (
    DEFAULT_SUBSCRIPTIONS,
    FAMILY_LEGACY_JSON,
    FAMILY_LEGACY_JSON_ALT,
    FAMILY_LEGACY_JSON_WRITE,
    FAMILY_UNKNOWN,
    FAMILY_ZENDURE_CLOUD_SCALAR,
    FAMILY_ZENSDK_HA_SCALAR,
    FakeMqttListener,
    MqttTopicAggregator,
    PahoMqttListener,
    classify_topic,
    discover_broker_topics,
    parse_report_payload,
)

pytestmark = pytest.mark.simulation


def _broker(host="192.168.1.20", port=1883):
    return {"id": f"mqtt:{host}:{port}", "host": host, "port": port}


def test_classifies_zendure_scalar_topic():
    match = classify_topic("Zendure/sensor/EOD123/packInputPower")
    assert match.family == FAMILY_ZENSDK_HA_SCALAR
    assert match.device_id == "EOD123"
    assert match.serial_number == "EOD123"
    assert match.metric == "packInputPower"


def test_classifies_legacy_iot_report_topic():
    report = classify_topic("iot/productKey/EOD123/properties/report")
    assert report.family == FAMILY_LEGACY_JSON
    assert report.device_id == "EOD123"
    assert report.product_key == "productKey"

    write = classify_topic("iot/productKey/EOD123/properties/write")
    assert write.family == FAMILY_LEGACY_JSON_WRITE

    alt = classify_topic("/productKey/EOD123/properties/report")
    assert alt.family == FAMILY_LEGACY_JSON_ALT
    assert alt.device_id == "EOD123"


def test_groups_scalar_topics_by_broker_and_device():
    aggregator = MqttTopicAggregator(_broker())
    aggregator.observe("Zendure/sensor/EOD123/packInputPower")
    aggregator.observe("Zendure/sensor/EOD123/outputHomePower")
    aggregator.observe("Zendure/sensor/EOD999/solarInputPower")

    results = aggregator.results()
    assert len(results) == 2
    by_device = {item["device_id"]: item for item in results}

    first = by_device["EOD123"]
    assert (
        first["id"]
        == "mqtt-device:local_mqtt:mqtt:192.168.1.20:1883:zensdk_ha_scalar:EOD123"
    )
    assert first["source_type"] == "local_mqtt"
    assert first["broker_id"] == "mqtt:192.168.1.20:1883"
    assert set(first["metrics_seen"]) == {"packInputPower", "outputHomePower"}
    assert by_device["EOD999"]["metrics_seen"] == ["solarInputPower"]


def test_same_device_id_on_two_brokers_is_not_merged():
    first = discover_broker_topics(
        _broker("192.168.1.20"),
        listener_factory=lambda *a, **k: FakeMqttListener(
            [("Zendure/sensor/EOD123/packInputPower", None)]
        ),
    )
    second = discover_broker_topics(
        _broker("192.168.1.30"),
        listener_factory=lambda *a, **k: FakeMqttListener(
            [("Zendure/sensor/EOD123/packInputPower", None)]
        ),
    )
    assert first[0]["serial_number"] == second[0]["serial_number"] == "EOD123"
    assert first[0]["broker_id"] != second[0]["broker_id"]
    assert first[0]["id"] != second[0]["id"]


def test_default_subscriptions_include_slash_prefixed_alt_tree():
    # legacy_zendure_json_alt topics (`/<pk>/<dev>/properties/report`) are now
    # both classified and subscribed to, not only classified.
    assert "/+/+/#" in DEFAULT_SUBSCRIPTIONS
    assert classify_topic("/productKey/EOD123/properties/report").family == (
        FAMILY_LEGACY_JSON_ALT
    )


def test_classifies_cloud_prefixed_scalar_topic():
    match = classify_topic("appkey123/sensor/EOD123/electricLevel")
    assert match.family == FAMILY_ZENDURE_CLOUD_SCALAR
    assert match.device_id == "EOD123"
    assert match.serial_number == "EOD123"
    assert match.metric == "electricLevel"
    for component in ("number", "switch", "select", "binary_sensor"):
        alt = classify_topic(f"appkey123/{component}/EOD123/state")
        assert alt.family == FAMILY_ZENDURE_CLOUD_SCALAR


def test_cloud_prefix_does_not_shadow_local_families():
    # `Zendure/...` and `iot/...` keep their own families even though they also
    # have four+ segments.
    assert classify_topic("Zendure/sensor/EOD123/x").family == FAMILY_ZENSDK_HA_SCALAR
    assert (
        classify_topic("iot/productKey/EOD123/properties/report").family
        == FAMILY_LEGACY_JSON
    )


def test_unknown_topic_does_not_crash():
    aggregator = MqttTopicAggregator(_broker())
    assert classify_topic("home/livingroom/temperature").family == FAMILY_UNKNOWN
    assert classify_topic("appkey123/unknowncomp/dev/metric").family == FAMILY_UNKNOWN
    assert classify_topic("").family == FAMILY_UNKNOWN
    assert classify_topic(None).family == FAMILY_UNKNOWN
    aggregator.observe("home/livingroom/temperature")
    aggregator.observe(None, b"not json")
    assert aggregator.results() == []


def test_payload_json_report_extracts_serial_product_and_metrics_when_present():
    payload = json.dumps(
        {
            "sn": "SN-EOD123",
            "product": "SolarFlow 800",
            "properties": {
                "electricLevel": 55,
                "outputHomePower": 120,
                "packData": [{"sn": "PACK1"}],
            },
        }
    )
    parsed = parse_report_payload(payload)
    assert parsed["serial_number"] == "SN-EOD123"
    assert parsed["model_hint"] == "SolarFlow 800"
    assert set(parsed["metrics"]) == {"electricLevel", "outputHomePower", "packData"}
    assert parsed["pack_data"] is True

    aggregator = MqttTopicAggregator(_broker())
    aggregator.observe("iot/productKey/EOD123/properties/report", payload)
    result = aggregator.results()[0]
    assert result["topic_family"] == FAMILY_LEGACY_JSON
    assert result["serial_number"] == "SN-EOD123"
    assert result["model_hint"] == "SolarFlow 800"
    assert "electricLevel" in result["metrics_seen"]


def test_malformed_payload_is_ignored():
    assert parse_report_payload(b"\xff\xfe not utf8") == {}
    assert parse_report_payload("not json") == {}
    assert parse_report_payload(json.dumps([1, 2, 3])) == {}

    aggregator = MqttTopicAggregator(_broker())
    aggregator.observe("iot/productKey/EOD123/properties/report", b"\xff\xfe")
    result = aggregator.results()[0]
    assert result["device_id"] == "EOD123"
    assert result["serial_number"] is None


def test_topic_discovery_does_not_publish():
    listener = FakeMqttListener(
        [
            ("Zendure/sensor/EOD123/packInputPower", None),
            ("iot/productKey/EOD123/properties/report", json.dumps({"sn": "S1"})),
        ]
    )
    devices = discover_broker_topics(
        _broker(), listener_factory=lambda *a, **k: listener
    )
    assert listener.published == []
    assert len(devices) == 2


def test_discovery_output_is_bounded():
    messages = [
        (f"Zendure/sensor/DEV{index}/metric", None) for index in range(500)
    ]
    aggregator = MqttTopicAggregator(_broker(), max_topics=50, max_candidates=10)
    for topic, payload in messages:
        aggregator.observe(topic, payload)
    assert aggregator.topics_seen_count == 50
    assert len(aggregator.results()) <= 10


def _install_fake_paho(monkeypatch):
    holder = {}

    class _FakeClient:
        def __init__(self, *_args, **_kwargs):
            self.ops = []
            holder["client"] = self

        def tls_set(self, *a, **k):
            self.ops.append(("tls_set", a, k))

        def tls_insecure_set(self, value):
            self.ops.append(("tls_insecure_set", value))

        def username_pw_set(self, username, password=None):
            self.ops.append(("username_pw_set", username, password))

        def subscribe(self, *a, **k):
            self.ops.append(("subscribe", a, k))

        def connect(self, host, port, keepalive=0):
            self.ops.append(("connect", host, port))

        def loop_start(self):
            self.ops.append(("loop_start",))

        def loop_stop(self):
            self.ops.append(("loop_stop",))

        def disconnect(self):
            self.ops.append(("disconnect",))

    fake_client = types.ModuleType("paho.mqtt.client")
    fake_client.Client = _FakeClient
    fake_client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    monkeypatch.setitem(sys.modules, "paho", types.ModuleType("paho"))
    monkeypatch.setitem(sys.modules, "paho.mqtt", types.ModuleType("paho.mqtt"))
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", fake_client)
    return holder


def test_paho_listener_plaintext_anonymous_skips_tls_and_auth(monkeypatch):
    holder = _install_fake_paho(monkeypatch)
    PahoMqttListener("192.168.1.20", 1883).listen(0, lambda topic, payload: None)
    names = [op[0] for op in holder["client"].ops]
    assert "tls_set" not in names
    assert "username_pw_set" not in names
    assert "connect" in names


def test_paho_listener_sets_tls_and_credentials_before_connect(monkeypatch):
    holder = _install_fake_paho(monkeypatch)
    PahoMqttListener(
        "192.168.1.20",
        8883,
        tls=True,
        tls_mode="insecure_no_verify",
        username="mqtt-user",
        password="mqtt-secret",
    ).listen(0, lambda topic, payload: None)
    ops = holder["client"].ops
    names = [op[0] for op in ops]
    assert "tls_set" in names
    # insecure_no_verify must skip chain verification (self-signed brokers).
    assert ("tls_set", (), {"cert_reqs": ssl.CERT_NONE}) in ops
    assert ("tls_insecure_set", True) in ops
    assert ("username_pw_set", "mqtt-user", "mqtt-secret") in ops
    assert names.index("tls_set") < names.index("connect")
    assert names.index("username_pw_set") < names.index("connect")


def test_paho_listener_tls_with_system_ca_stays_verified(monkeypatch):
    holder = _install_fake_paho(monkeypatch)
    PahoMqttListener("192.168.1.20", 8883, tls=True, tls_mode="system_ca").listen(
        0, lambda topic, payload: None
    )
    ops = holder["client"].ops
    names = [op[0] for op in ops]
    assert "tls_set" in names
    assert ("tls_set", (), {}) in ops
    assert "tls_insecure_set" not in names
