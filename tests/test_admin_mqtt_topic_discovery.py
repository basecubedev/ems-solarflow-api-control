# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only MQTT topic discovery tests (no real broker or network)."""

import json
import ssl
import sys
import types

import pytest

from admin.mqtt_discovery import _normalize_attempt_result
from admin.mqtt_topic_discovery import (
    DEFAULT_SUBSCRIPTIONS,
    MAX_METRICS_PER_CANDIDATE,
    MAX_TOPICS_PER_CANDIDATE,
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
from admin.zendure_mqtt_config_proposals import build_proposals
from ems.clients import DEVICE_STATE_PROPERTIES
from ems.zendure_mqtt.snapshot import ZendureMqttAggregator
from tests.helpers import payloads

pytestmark = [
    pytest.mark.admin,
    pytest.mark.mqtt,
    pytest.mark.integration,
    pytest.mark.simulation,
]


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
        (f"Zendure/sensor/DEV{index}/electricLevel", None) for index in range(500)
    ]
    aggregator = MqttTopicAggregator(_broker(), max_topics=50, max_candidates=10)
    for topic, payload in messages:
        aggregator.observe(topic, payload)
    assert aggregator.topics_seen_count == 50
    assert len(aggregator.results()) == 10


_ESPHOME_NODE_TOPICS = (
    ("Pool/sensor/pool_temperature/state", b"24.5"),
    ("Pool/switch/pump/state", b"ON"),
    ("Pool/switch/pump/command", b"ON"),
    ("Pool/binary_sensor/flow/state", b"OFF"),
    ("Pool/number/setpoint/state", b"28"),
    ("homeassistant/sensor/pool/pool_temperature/config", b'{"name": "Pool temperature"}'),
    ("homeassistant/switch/pump/config", b'{"name": "Pump"}'),
)

_ESPHOME_UNDER_ZENDURE_TOPICS = (
    ("Zendure/sensor/pool_temperature/state", b"24.5"),
    ("Zendure/switch/pump/state", b"ON"),
    ("Zendure/binary_sensor/flow/state", b"OFF"),
    ("Zendure/number/setpoint/state", b"28"),
    ("Zendure/sensor/pool_temperature/availability", b"online"),
    ("Zendure/Pool/sensor/pool_temperature/state", b"24.5"),
)


@pytest.mark.parametrize(
    ("topic", "payload"), _ESPHOME_NODE_TOPICS + _ESPHOME_UNDER_ZENDURE_TOPICS
)
def test_an_esphome_or_home_assistant_topic_is_no_zendure_device(topic, payload):
    """AL20: an entity's ``state``/``command``/``config`` topic is no Zendure property.

    ``Pool/...`` matches the cloud scalar shape and ``Zendure/...`` the ZenSDK
    one; only the second is on a subscription local discovery makes.
    """

    aggregator = MqttTopicAggregator(_broker())
    aggregator.observe(topic, payload)

    assert aggregator.results() == []


def test_an_esphome_node_takes_no_candidate_slot_from_a_zendure_device():
    """Topics that are not offered must not use up the bounded candidate list first."""

    aggregator = MqttTopicAggregator(_broker(), max_candidates=1)
    for topic, payload in _ESPHOME_NODE_TOPICS + _ESPHOME_UNDER_ZENDURE_TOPICS:
        aggregator.observe(topic, payload)
    for topic, payload in payloads.scalar_messages():
        aggregator.observe(topic, payload)

    assert [item["device_id"] for item in aggregator.results()] == [payloads.SCALAR_SERIAL]


def test_a_broker_with_only_an_esphome_node_reports_no_hardware_topics():
    listener = FakeMqttListener(_ESPHOME_NODE_TOPICS + _ESPHOME_UNDER_ZENDURE_TOPICS)

    devices = discover_broker_topics(_broker(), listener_factory=lambda *a, **k: listener)

    assert devices == []
    assert _normalize_attempt_result(devices)["status"] == "mqtt_listened_no_topics"


def test_zendure_devices_beside_an_esphome_node_are_found_with_their_metrics():
    esphome = list(_ESPHOME_NODE_TOPICS + _ESPHOME_UNDER_ZENDURE_TOPICS)
    messages = [
        *esphome[:4],
        *payloads.scalar_messages(),
        *esphome[4:],
        (payloads.d0_topic(), payloads.d0_total_power()),
        *payloads.cloud_scalar_messages(),
    ]

    devices = discover_broker_topics(
        _broker(), listener_factory=lambda *a, **k: FakeMqttListener(messages)
    )

    by_device = {(item["topic_family"], item["device_id"]): item for item in devices}
    assert set(by_device) == {
        (FAMILY_ZENSDK_HA_SCALAR, payloads.SCALAR_SERIAL),
        (FAMILY_ZENSDK_HA_SCALAR, payloads.D0_SERIAL),
        (FAMILY_ZENDURE_CLOUD_SCALAR, payloads.CLOUD_SERIAL),
    }
    scalar = by_device[(FAMILY_ZENSDK_HA_SCALAR, payloads.SCALAR_SERIAL)]
    assert scalar["metrics_seen"] == list(payloads.SCALAR_METRICS)
    assert scalar["topics_seen"] == [topic for topic, _ in payloads.scalar_messages()]
    assert by_device[(FAMILY_ZENSDK_HA_SCALAR, payloads.D0_SERIAL)]["metrics_seen"] == [
        "totalPower"
    ]
    cloud = by_device[(FAMILY_ZENDURE_CLOUD_SCALAR, payloads.CLOUD_SERIAL)]
    assert cloud["metrics_seen"] == list(payloads.SCALAR_METRICS)


_REAL_DEVICE_METRICS = {
    "electricLevel": 55,
    "outputHomePower": 300,
    "outputLimit": 800,
    "solarInputPower": 400,
    "inverseMaxPower": 800,
    "phaseSwitch": 0,
    "gridOffPower": 0,
    "acMode": 2,
}


@pytest.mark.parametrize("reads_first", [True, False], ids=["read_first", "read_last"])
def test_discovery_infers_the_capabilities_the_runtime_infers(reads_first):
    """R38-1: a found device takes every metric it reports, also those sent before."""

    names = sorted(
        _REAL_DEVICE_METRICS, key=lambda name: (name in DEVICE_STATE_PROPERTIES) != reads_first
    )
    messages = [
        (payloads.scalar_topic(name), str(_REAL_DEVICE_METRICS[name]).encode()) for name in names
    ]
    discovery = MqttTopicAggregator(_broker())
    runtime = ZendureMqttAggregator()
    for topic, payload in messages:
        discovery.observe(topic, payload)
        runtime.observe(topic, payload)

    (candidate,) = discovery.results()
    (proposal,) = build_proposals([candidate])
    (snapshot,) = runtime.snapshots()
    assert candidate["metrics_seen"] == names
    assert candidate["topics_seen"] == [topic for topic, _ in messages]
    assert set(proposal["capabilities"]) == snapshot.capabilities == {
        "ac_input_control",
        "battery_storage",
        "multi_mppt",
        "offgrid_output",
        "output_control",
        "pv_input",
    }


def test_a_metric_under_a_key_without_a_found_device_finds_none():
    """The same id under the cloud prefix is a device key of its own."""

    aggregator = MqttTopicAggregator(_broker())
    aggregator.observe("Zendure/sensor/SN1/electricLevel", b"43")
    aggregator.observe("Zendure/sensor/SN1/availability", b"online")
    aggregator.observe("appkeyAAAA/sensor/SN1/config", b"{}")

    assert [
        (item["topic_family"], item["device_id"], item["metrics_seen"])
        for item in aggregator.results()
    ] == [(FAMILY_ZENSDK_HA_SCALAR, "SN1", ["electricLevel", "availability"])]


def test_what_a_device_sent_before_it_was_found_is_bounded_like_its_metrics():
    aggregator = MqttTopicAggregator(_broker())
    for index in range(MAX_METRICS_PER_CANDIDATE + 10):
        aggregator.observe(f"Zendure/sensor/SN1/extra{index}", b"1")
    aggregator.observe("Zendure/sensor/SN1/electricLevel", b"43")

    (item,) = aggregator.results()
    assert item["metrics_seen"] == [f"extra{index}" for index in range(MAX_METRICS_PER_CANDIDATE)]
    assert len(item["topics_seen"]) == MAX_TOPICS_PER_CANDIDATE


@pytest.mark.parametrize(
    "topic",
    [
        "Zendure/number/D0SN/totalPower",
        "Zendure/select/D0SN/totalPower",
        "Zendure/sensors/D0SN/totalPower",
    ],
)
def test_total_power_finds_a_device_only_on_the_exact_d0_topic(topic):
    """R38-2: ``totalPower`` is read on ``Zendure/sensor/<serial>/totalPower`` only."""

    aggregator = MqttTopicAggregator(_broker())
    aggregator.observe(topic, b"-40")

    assert aggregator.results() == []


@pytest.mark.parametrize("family_topic", ["Zendure/sensor/SN1/{}", "appkeyAAAA/sensor/SN1/{}"])
def test_every_property_parse_device_reads_finds_a_zendure_device(family_topic):
    """The discovery reads the parser's own list, not a copy of it."""

    for prop in sorted(DEVICE_STATE_PROPERTIES):
        aggregator = MqttTopicAggregator(_broker())
        aggregator.observe(family_topic.format(prop), b"1")
        assert [item["metrics_seen"] for item in aggregator.results()] == [[prop]], prop


def test_a_topic_published_into_the_zendure_tree_by_hand_still_classifies():
    """A user's own ``Zendure/sensors/<x>/<property>`` topic keeps its reading."""

    match = classify_topic("Zendure/sensors/X1/solarInputPower")
    assert (match.family, match.device_id, match.metric) == (
        FAMILY_ZENSDK_HA_SCALAR,
        "X1",
        "solarInputPower",
    )
    aggregator = MqttTopicAggregator(_broker())
    aggregator.observe("Zendure/sensors/X1/solarInputPower", b"300")
    assert [item["metrics_seen"] for item in aggregator.results()] == [["solarInputPower"]]


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
