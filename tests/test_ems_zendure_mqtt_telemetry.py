# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the EMS Core Zendure MQTT telemetry parser (no broker required)."""

import json

import pytest

from ems.zendure_mqtt import (
    FAMILY_LEGACY_JSON,
    FAMILY_LEGACY_JSON_ALT,
    FAMILY_UNKNOWN,
    FAMILY_ZENDURE_CLOUD_SCALAR,
    FAMILY_ZENSDK_HA_SCALAR,
    ZendureMqttAggregator,
    classify_topic,
    coerce_scalar,
    parse_report_payload,
)
from tools import zendure_mqtt_mock_service as mock

pytestmark = pytest.mark.simulation


# --- topic classification ---------------------------------------------------


def test_classifies_zensdk_ha_scalar():
    match = classify_topic("Zendure/sensor/SN1/electricLevel")
    assert match.family == FAMILY_ZENSDK_HA_SCALAR
    assert match.device_id == "SN1"
    assert match.serial_number == "SN1"
    assert match.metric == "electricLevel"


def test_classifies_legacy_iot_json():
    match = classify_topic("iot/PK/DEV/properties/report")
    assert match.family == FAMILY_LEGACY_JSON
    assert match.device_id == "DEV"
    assert match.product_key == "PK"


def test_classifies_legacy_slash_json_alt():
    match = classify_topic("/PK/DEV/properties/report")
    assert match.family == FAMILY_LEGACY_JSON_ALT
    assert match.device_id == "DEV"
    assert match.product_key == "PK"


def test_classifies_cloud_scalar_without_exposing_appkey():
    topic = "SECRET_APP_KEY/sensor/DEV1/electricLevel"
    match = classify_topic(topic)
    assert match.family == FAMILY_ZENDURE_CLOUD_SCALAR
    assert match.device_id == "DEV1"
    assert match.metric == "electricLevel"
    # The appKey prefix must never surface as a field on the match.
    for value in (match.device_id, match.serial_number, match.metric, match.product_key):
        assert value != "SECRET_APP_KEY"


def test_write_topic_is_not_treated_as_telemetry():
    assert classify_topic("iot/PK/DEV/properties/write").family == FAMILY_UNKNOWN


@pytest.mark.parametrize("topic", [None, "", "a", "a/b", "/", 123, b"bytes"])
def test_unknown_topics_do_not_raise(topic):
    assert classify_topic(topic).family == FAMILY_UNKNOWN


# --- scalar payloads --------------------------------------------------------


def test_scalar_numeric_payloads_become_numbers():
    assert coerce_scalar("430") == 430
    assert isinstance(coerce_scalar("430"), int)
    assert coerce_scalar("43.5") == 43.5
    assert isinstance(coerce_scalar("43.5"), float)
    assert coerce_scalar("-70") == -70


def test_scalar_non_numeric_payloads_remain_strings():
    assert coerce_scalar("online") == "online"
    assert coerce_scalar("") == ""
    assert coerce_scalar("nan") == "nan"
    assert coerce_scalar(b"offline") == "offline"


# --- JSON report payloads ---------------------------------------------------


def _legacy_payload(**overrides):
    payload = {
        "sn": "SN-REAL",
        "product": "solarFlow800Pro",
        "properties": {
            "electricLevel": 43,
            "solarInputPower": 620,
            "outputLimit": 301,
            "inputLimit": 0,
            "acMode": 2,
        },
        "packData": [{"sn": "SN-REAL-PACK1", "socLevel": 43}],
        "unknownTopLevel": 99,
    }
    payload.update(overrides)
    return payload


def test_legacy_json_extracts_product_serial_properties_and_packdata():
    report = parse_report_payload(json.dumps(_legacy_payload()))
    assert report.serial_number == "SN-REAL"
    assert report.product == "solarFlow800Pro"
    assert report.properties["electricLevel"] == 43
    assert report.battery_packs == [{"sn": "SN-REAL-PACK1", "socLevel": 43}]
    assert report.extra.get("unknownTopLevel") == 99


def test_parse_accepts_bytes_and_dict():
    as_dict = parse_report_payload(_legacy_payload())
    as_bytes = parse_report_payload(json.dumps(_legacy_payload()).encode())
    assert as_dict.serial_number == as_bytes.serial_number == "SN-REAL"


def test_malformed_json_does_not_raise():
    report = parse_report_payload("{not valid json")
    assert report.serial_number is None
    assert report.properties == {}
    assert report.battery_packs == []


# --- capability inference ---------------------------------------------------


def _snapshot_for(messages):
    agg = ZendureMqttAggregator()
    for topic, payload in messages:
        agg.observe(topic, payload)
    snaps = agg.snapshots()
    assert len(snaps) == 1
    return snaps[0]


def test_capability_inference_core_set():
    snap = _snapshot_for(
        [("iot/PK/DEV/properties/report", json.dumps(_legacy_payload()))]
    )
    assert {
        "battery_storage",
        "pv_input",
        "output_control",
        "ac_input_control",
    } <= snap.capabilities


def test_solarflow2400_like_data_detects_multi_mppt():
    payload = _legacy_payload(
        properties={"solarPower5": 120, "solarPower6": 90, "chargeMaxLimit": 2400}
    )
    snap = _snapshot_for([("iot/PK/DEV/properties/report", json.dumps(payload))])
    assert "multi_mppt" in snap.capabilities


# --- aggregation ------------------------------------------------------------


def test_aggregator_merges_multiple_scalar_metrics_into_one_snapshot():
    snap = _snapshot_for(
        [
            ("Zendure/sensor/SN1/electricLevel", "43"),
            ("Zendure/sensor/SN1/solarInputPower", "620"),
            ("Zendure/sensor/SN1/acMode", "2"),
        ]
    )
    assert snap.device_id == "SN1"
    assert snap.metrics["electricLevel"] == 43
    assert snap.metrics["solarInputPower"] == 620
    assert snap.metrics["acMode"] == 2


def test_aggregator_records_last_seen_from_injected_clocks():
    agg = ZendureMqttAggregator(monotonic=lambda: 123.0, wall_clock=lambda: 1_700_000_000.0)
    agg.observe("Zendure/sensor/SN1/electricLevel", "43")
    snap = agg.snapshots()[0]
    assert snap.last_seen_monotonic == 123.0
    assert snap.last_seen_epoch == 1_700_000_000.0


def test_merged_snapshot_preserves_each_metric_observation_provenance():
    monotonic_values = iter((100.0, 200.0))
    agg = ZendureMqttAggregator(
        monotonic=lambda: next(monotonic_values), wall_clock=lambda: 1.0
    )
    agg.observe(
        "iot/PK/DEV/properties/report",
        json.dumps(
            {
                "properties": {"outputLimit": 300, "acMode": 2},
            }
        ),
    )
    agg.observe("Zendure/sensor/DEV/electricLevel", "50")
    snap = agg.snapshots()[0]

    assert snap.last_seen_monotonic == 200.0
    assert snap.metric_monotonic["outputLimit"] == 100.0
    assert snap.metric_monotonic["electricLevel"] == 200.0
    assert snap.observed_metrics == {"electricLevel"}


def test_aggregator_preserves_packdata_as_battery_packs():
    snap = _snapshot_for(
        [("iot/PK/DEV/properties/report", json.dumps(_legacy_payload()))]
    )
    assert snap.battery_packs == [{"sn": "SN-REAL-PACK1", "socLevel": 43}]


def test_scalar_and_json_payload_serial_number_kept():
    snap = _snapshot_for(
        [("iot/PK/DEV/properties/report", json.dumps(_legacy_payload()))]
    )
    assert snap.device_id == "DEV"  # grouped by topic device id
    assert snap.serial_number == "SN-REAL"  # sn from payload


def test_unknown_topic_is_ignored_by_aggregator():
    agg = ZendureMqttAggregator()
    agg.observe("iot/PK/DEV/properties/write", "{}")
    agg.observe(None, None)
    assert agg.snapshots() == []


# --- normalization ----------------------------------------------------------


def test_normalization_adds_aliases_without_losing_raw():
    snap = _snapshot_for(
        [
            ("Zendure/sensor/SN1/fanSwitch", "1"),
            ("Zendure/sensor/SN1/socSet", "1000"),
            ("Zendure/sensor/SN1/minSoc", "10"),
            ("Zendure/sensor/SN1/BatVolt", "5320"),
        ]
    )
    assert snap.metrics["fanSwitch"] == 1  # raw preserved
    assert snap.metrics["fan_enabled"] is True
    assert snap.metrics["soc_set_percent"] == 100  # 1000 / 10
    assert snap.metrics["min_soc_percent"] == 10  # already <= 100
    assert snap.metrics["battery_voltage_raw"] == 5320


# --- mock service round-trip ------------------------------------------------


def test_mock_service_all_schema_round_trips_through_parser():
    args = mock._finalize_args(mock.build_arg_parser().parse_args(["--schema", "all"]))
    agg = ZendureMqttAggregator()
    for record in mock.build_batch(args, 0):
        agg.observe(record["topic"], record["payload"])
    snaps = {snap.device_id: snap for snap in agg.snapshots()}

    scalar = snaps[args.device_sn]
    assert {FAMILY_ZENSDK_HA_SCALAR, FAMILY_ZENDURE_CLOUD_SCALAR} <= scalar.topic_families
    assert scalar.metrics["electricLevel"] == 43

    legacy = snaps[args.device_id]
    assert legacy.product == args.product
    assert legacy.serial_number == args.device_sn
    assert legacy.metrics["electricLevel"] == 43
    assert legacy.battery_packs
    assert "battery_storage" in legacy.capabilities
