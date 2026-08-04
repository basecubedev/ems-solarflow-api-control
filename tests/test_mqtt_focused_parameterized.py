# SPDX-License-Identifier: AGPL-3.0-or-later
"""Focused parameterized coverage across the MQTT integration boundary.

Complements the named scenarios with tight, high-signal parameterized cases:
device counts, per-family parsing/normalization, read-only vs writable
enforcement, broker profile validity, cross-transport duplicate identity and
the write gates crossed with the global safety modes.
"""

from types import SimpleNamespace

import pytest

import ems.config as cfg
from ems.zendure_mqtt.config_entries import (
    find_duplicate_zendure_device_identities,
    validate_zendure_mqtt_control_device_config,
    validate_zendure_mqtt_device_config,
    zendure_mqtt_broker_profile_views,
)
from ems.zendure_mqtt.snapshot import ZendureMqttAggregator
from ems.zendure_mqtt.topics import (
    FAMILY_LEGACY_JSON,
    FAMILY_LEGACY_JSON_ALT,
    FAMILY_UNKNOWN,
    FAMILY_ZENDURE_CLOUD_SCALAR,
    FAMILY_ZENSDK_HA_SCALAR,
    classify_topic,
)
from ems.zendure_mqtt.write_protocols import resolve_write_protocol
from tests.helpers import payloads
from tests.helpers.controller import make_state, run_control_cycle
from tests.helpers.fake_mqtt import FakeMqttNetwork
from tests.helpers.mqtt_scenarios import (
    PAYLOAD_HTTP_ZENSDK,
    PAYLOAD_LEGACY_JSON,
    ROLE_INVERTER_CONTROL,
    TRANSPORT_API_HTTP,
    TRANSPORT_GRID_METER_HTTP,
    TRANSPORT_LOCAL_MQTT_A,
    BrokerSpec,
    DeviceSpec,
    GridMeterSpec,
    Scenario,
    build_installation,
)

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
    pytest.mark.power_control,
]

_HTTP_METER = GridMeterSpec(
    meter_type="shelly", transport=TRANSPORT_GRID_METER_HTTP, power_w=3000.0
)
_BROKER = BrokerSpec(ref="local_a", source="local_mqtt", host="10.0.0.10")


# --- 1. Device counts -------------------------------------------------------
def _count_scenario(count):
    devices = []
    for index in range(count):
        if index % 3 == 2:
            devices.append(
                DeviceSpec(
                    f"M{index}", ROLE_INVERTER_CONTROL, TRANSPORT_LOCAL_MQTT_A,
                    PAYLOAD_LEGACY_JSON, broker_ref="local_a",
                    device_id=f"DEV{index}", product_key=f"PK{index}",
                    control_enabled=True,
                )
            )
        else:
            devices.append(
                DeviceSpec(
                    f"A{index}", ROLE_INVERTER_CONTROL, TRANSPORT_API_HTTP,
                    PAYLOAD_HTTP_ZENSDK, serial=f"SN{index}",
                )
            )
    brokers = (_BROKER,) if any(d.is_mqtt for d in devices) else ()
    return Scenario(
        name=f"count_{count}", brokers=brokers, devices=tuple(devices),
        grid_meter=_HTTP_METER,
    )


@pytest.mark.parametrize("count", [0, 1, 2, 3, 4, 8])
def test_device_count_runtime_is_bounded_and_ordered(count):
    network = FakeMqttNetwork()
    installation = build_installation(_count_scenario(count), network)
    try:
        assert len(installation.devices) == count
        # Stable ordering: API devices first, then control-runtime devices.
        names = [d.name for d in installation.devices]
        assert names == sorted(names, key=lambda n: (n[0] != "A", n))
        controller = run_control_cycle(
            installation, states=[make_state() for _ in range(count)]
        )
        if count == 0:
            assert controller.last_control_explanation is None or (
                len(controller.last_control_explanation.devices) == 0
            )
        else:
            assert len(controller.last_control_explanation.devices) == count
    finally:
        installation.stop()
    # Cleanup: each unique broker service was stopped (no exception on stop).


# --- 2. Payload-family parsing and normalization ----------------------------
def test_scalar_metrics_arrive_out_of_order_and_merge():
    agg = ZendureMqttAggregator()
    messages = list(payloads.scalar_messages())
    for topic, payload in reversed(messages):  # reverse order must not matter
        agg.observe(topic, payload)
    snap = agg.snapshots()[0]
    assert snap.metrics["electricLevel"] == 82
    assert snap.metrics["solarInputPower"] == 640
    assert "battery_storage" in snap.capabilities
    assert "pv_input" in snap.capabilities


def test_repeated_metric_takes_latest_value():
    agg = ZendureMqttAggregator()
    topic = payloads.scalar_topic("outputHomePower")
    agg.observe(topic, b"100")
    agg.observe(topic, b"250")
    snap = agg.snapshots()[0]
    assert snap.metrics["outputHomePower"] == 250


def test_packdata_multiple_batteries_preserved():
    agg = ZendureMqttAggregator()
    agg.observe(payloads.legacy_json_topic(), payloads.packdata_report(packs=3))
    snap = agg.snapshots()[0]
    assert len(snap.battery_packs) == 3


@pytest.mark.parametrize("raw,expected", [
    (b"-798", -798.0), (b"0", 0), (b"800", 800), (b"1234567", 1234567),
])
def test_scalar_numeric_coercion(raw, expected):
    agg = ZendureMqttAggregator()
    agg.observe(payloads.scalar_topic("totalPower"), raw)
    snap = agg.snapshots()[0]
    assert snap.metrics["totalPower"] == expected


@pytest.mark.parametrize("payload", [
    payloads.MALFORMED_JSON, payloads.EMPTY_PAYLOAD, payloads.PARTIAL_REPORT,
])
def test_malformed_report_is_absorbed(payload):
    agg = ZendureMqttAggregator()
    agg.observe(payloads.legacy_json_topic(), payload)
    # A device row is created from the topic, but no crash and no junk metrics.
    snap = agg.snapshots()[0]
    assert isinstance(snap.metrics, dict)


@pytest.mark.parametrize("topic,family", [
    (payloads.scalar_topic("electricLevel"), FAMILY_ZENSDK_HA_SCALAR),
    (payloads.cloud_scalar_topic("electricLevel"), FAMILY_ZENDURE_CLOUD_SCALAR),
    (payloads.legacy_json_topic(), FAMILY_LEGACY_JSON),
    (payloads.legacy_json_alt_topic(), FAMILY_LEGACY_JSON_ALT),
    (payloads.UNKNOWN_TOPIC, FAMILY_UNKNOWN),
    (payloads.WRITE_RESPONSE_TOPIC, FAMILY_UNKNOWN),
    (payloads.FOREIGN_WILDCARD_TOPIC, FAMILY_UNKNOWN),
])
def test_topic_classification(topic, family):
    assert classify_topic(topic).family == family


def test_cloud_scalar_topic_never_exposes_app_key():
    match = classify_topic(payloads.cloud_scalar_topic("electricLevel"))
    # The account app-key prefix must never be stored on the match.
    assert payloads.CLOUD_APP_KEY not in (match.product_key or "")
    assert match.serial_number == payloads.CLOUD_SERIAL


# --- 3. Read-only vs writable behavior --------------------------------------
@pytest.mark.parametrize("family", [
    "zensdk_ha_scalar", "zendure_cloud_scalar",
])
def test_scalar_family_has_no_inferred_write_protocol(family):
    assert resolve_write_protocol(family) is None


@pytest.mark.parametrize("family", [
    FAMILY_LEGACY_JSON, FAMILY_LEGACY_JSON_ALT,
])
def test_legacy_family_has_no_inferred_write_protocol(family):
    # Topic-family write inference was removed: a bare legacy family no longer
    # yields a write protocol. Writability comes from the pinned hardware profile.
    assert resolve_write_protocol(family) is None


def test_scalar_control_entry_is_rejected():
    item = {
        "type": "zendure_mqtt", "name": "Scal",
        "mqtt": {"topic_family": "zensdk_ha_scalar", "device_id": "SN1",
                 "product_key": "PK1"},
        "capabilities": {"write_output_limit": True},
    }
    codes = {i["code"] for i in validate_zendure_mqtt_control_device_config(item)}
    assert "write_protocol_unsupported" in codes


def test_telemetry_only_entry_requesting_write_is_flagged():
    item = {
        "type": "zendure_mqtt", "name": "Dev",
        "mqtt": {"topic_family": "legacy_zendure_json", "device_id": "DEV1"},
        "capabilities": {"write_output_limit": True},
    }
    codes = {i["code"] for i in validate_zendure_mqtt_device_config(item)}
    assert "write_output_limit_unsupported" in codes


# --- 4. Broker profile validation -------------------------------------------
@pytest.mark.parametrize("port,usable", [
    (1, True), (1883, True), (8883, True), (65535, True),
    (0, False), (-1, False), (65536, False), ("nan", False), (True, False),
])
def test_broker_profile_port_validity(port, usable):
    raw = {
        "enabled": True,
        "brokers": {
            "b": {"enabled": True, "source": "local_mqtt", "host": "h", "port": port},
        },
    }
    view = zendure_mqtt_broker_profile_views(raw)["b"]
    if usable:
        assert view.usable, f"expected usable for port {port!r}"
    else:
        assert not view.usable


def test_disabled_broker_is_unusable():
    raw = {"enabled": True, "brokers": {
        "b": {"enabled": False, "source": "local_mqtt", "host": "h", "port": 1883}}}
    assert not zendure_mqtt_broker_profile_views(raw)["b"].usable


def test_cloud_broker_without_auth_is_unusable():
    raw = {"enabled": True, "brokers": {
        "b": {"enabled": True, "source": "zendure_cloud_mqtt", "host": "h", "port": 8883}}}
    view = zendure_mqtt_broker_profile_views(raw)["b"]
    assert view.usability_issue() == "zendure_mqtt_broker_auth_missing"


def test_broker_profile_view_never_carries_secret():
    raw = {"enabled": True, "brokers": {
        "b": {"enabled": True, "source": "local_mqtt", "host": "h", "port": 1883,
              "password": "s3cret", "username": "u"}}}
    view = zendure_mqtt_broker_profile_views(raw)["b"]
    assert "s3cret" not in repr(view)
    assert view.has_auth is True


# --- 5. Duplicate physical-device identity ----------------------------------
def test_same_serial_across_transports_is_duplicate():
    devices = [
        {"name": "HTTP", "sn": "SHARED1", "ip": "1.2.3.4"},
        {"type": "zendure_mqtt", "name": "MQTT", "serial_number": "SHARED1",
         "mqtt": {"topic_family": "legacy_zendure_json", "device_id": "DEV1"}},
    ]
    issues = find_duplicate_zendure_device_identities(devices)
    assert len(issues) == 1
    assert issues[0]["code"] == "zendure_device_identity_duplicate"
    # The message names indices only, never the serial.
    assert "SHARED1" not in issues[0]["message"]


def test_disabled_duplicate_is_ignored():
    devices = [
        {"name": "HTTP", "sn": "SHARED1"},
        {"type": "zendure_mqtt", "name": "MQTT", "serial_number": "SHARED1",
         "enabled": False,
         "mqtt": {"topic_family": "legacy_zendure_json", "device_id": "DEV1"}},
    ]
    assert find_duplicate_zendure_device_identities(devices) == []


def test_different_display_names_same_serial_still_collide():
    devices = [
        {"name": "First", "sn": "SHARED2"},
        {"name": "Second", "sn": "SHARED2"},
    ]
    assert len(find_duplicate_zendure_device_identities(devices)) == 1


def test_distinct_identifiers_are_not_merged():
    devices = [
        {"name": "SameName", "sn": "SNA"},
        {"name": "SameName", "sn": "SNB"},
    ]
    assert find_duplicate_zendure_device_identities(devices) == []


# --- 6. Write gates crossed with global safety modes ------------------------
_GATE_CASES = [
    # (control_gate, gate_flags, safety_flags, expected_allowed)
    ("api", {"ALLOW_HARDWARE_WRITES": True}, {}, True),
    ("api", {"ALLOW_HARDWARE_WRITES": False}, {}, False),
    ("mqtt_local", {"ALLOW_MQTT_LOCAL_CONTROL_WRITES": True}, {}, True),
    ("mqtt_local", {"ALLOW_MQTT_LOCAL_CONTROL_WRITES": False}, {}, False),
    ("mqtt_zendure", {"ALLOW_MQTT_ZENDURE_CONTROL_WRITES": True}, {}, True),
    ("api", {"ALLOW_HARDWARE_WRITES": True}, {"DRY_RUN": True}, False),
    ("api", {"ALLOW_HARDWARE_WRITES": True}, {"SIMULATION_MODE": True}, False),
    ("mqtt_local", {"ALLOW_MQTT_LOCAL_CONTROL_WRITES": True}, {"_replay": True}, False),
]


@pytest.mark.parametrize("gate,gate_flags,safety,expected", _GATE_CASES)
def test_write_gate_decisions(gate, gate_flags, safety, expected, monkeypatch):
    for name in (
        "ALLOW_HARDWARE_WRITES",
        "ALLOW_MQTT_LOCAL_CONTROL_WRITES",
        "ALLOW_MQTT_ZENDURE_CONTROL_WRITES",
    ):
        monkeypatch.setattr(cfg, name, gate_flags.get(name, False))
    monkeypatch.setattr(cfg, "DRY_RUN", safety.get("DRY_RUN", False))
    monkeypatch.setattr(cfg, "SIMULATION_MODE", safety.get("SIMULATION_MODE", False))
    monkeypatch.setattr(cfg, "ARGS", SimpleNamespace(replay=safety.get("_replay", False)))

    decision = cfg.resolve_write_gate(gate)
    assert decision.allowed is expected
    assert cfg.control_writes_allowed(gate) is expected


def test_one_disabled_gate_does_not_block_others(monkeypatch):
    monkeypatch.setattr(cfg, "ALLOW_HARDWARE_WRITES", True)
    monkeypatch.setattr(cfg, "ALLOW_MQTT_LOCAL_CONTROL_WRITES", False)
    monkeypatch.setattr(cfg, "ALLOW_MQTT_ZENDURE_CONTROL_WRITES", True)
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    monkeypatch.setattr(cfg, "SIMULATION_MODE", False)
    monkeypatch.setattr(cfg, "ARGS", SimpleNamespace(replay=False))
    assert cfg.resolve_write_gate("api").allowed is True
    assert cfg.resolve_write_gate("mqtt_local").allowed is False
    assert cfg.resolve_write_gate("mqtt_zendure").allowed is True
