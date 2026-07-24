# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the read-only Zendure MQTT config-proposal mapper (no broker)."""

import copy
import json

import pytest

from ems.zendure_mqtt import (
    ZendureMqttAggregator,
    ZendureMqttConfigProposal,
    map_snapshot_to_proposal,
    map_snapshots_to_proposals,
)
from ems.zendure_mqtt.config_mapping import (
    ROLE_BATTERY_INVERTER,
    ROLE_GRID_METER,
    ROLE_TELEMETRY_ONLY,
    ROLE_UNKNOWN,
)
from ems.zendure_mqtt.snapshot import ZendureMqttSnapshot
from ems.zendure_mqtt.topics import (
    FAMILY_LEGACY_JSON,
    FAMILY_LEGACY_JSON_ALT,
    FAMILY_ZENDURE_CLOUD_SCALAR,
    FAMILY_ZENSDK_HA_SCALAR,
)

pytestmark = pytest.mark.simulation


def _snapshot_from(messages):
    agg = ZendureMqttAggregator()
    for topic, payload in messages:
        agg.observe(topic, payload)
    snaps = agg.snapshots()
    assert len(snaps) == 1
    return snaps[0]


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
    }
    payload.update(overrides)
    return payload


# --- basic mapping per topic family -----------------------------------------


def test_zensdk_scalar_becomes_proposal():
    snap = _snapshot_from(
        [
            ("Zendure/sensor/SN1/electricLevel", "43"),
            ("Zendure/sensor/SN1/solarInputPower", "620"),
            ("Zendure/sensor/SN1/acMode", "2"),
        ]
    )
    proposal = map_snapshot_to_proposal(snap)
    assert isinstance(proposal, ZendureMqttConfigProposal)
    assert proposal.source == "zendure_mqtt"
    assert proposal.topic_family == FAMILY_ZENSDK_HA_SCALAR
    assert proposal.base_topic == "Zendure"
    assert proposal.serial_number == "SN1"
    assert proposal.confidence == "high"
    assert "electricLevel" in proposal.metrics


def test_legacy_iot_json_becomes_proposal_with_product_key_and_device_id():
    snap = _snapshot_from(
        [("iot/PK/DEV/properties/report", json.dumps(_legacy_payload()))]
    )
    proposal = map_snapshot_to_proposal(snap)
    assert proposal.topic_family == FAMILY_LEGACY_JSON
    assert proposal.base_topic == "iot"
    assert proposal.device_id == "DEV"
    assert proposal.product_key == "PK"
    assert proposal.serial_number == "SN-REAL"


def test_legacy_slash_json_becomes_proposal():
    snap = _snapshot_from(
        [("/PK/DEV/properties/report", json.dumps(_legacy_payload()))]
    )
    proposal = map_snapshot_to_proposal(snap)
    assert proposal.topic_family == FAMILY_LEGACY_JSON_ALT
    # The alt prefix is empty, so no base topic is proposed.
    assert proposal.base_topic is None
    assert proposal.device_id == "DEV"


def test_cloud_scalar_keeps_base_topic_none_and_exposes_no_secret():
    secret = "SECRET_APP_KEY"
    snap = _snapshot_from(
        [
            (f"{secret}/sensor/DEV1/electricLevel", "43"),
            (f"{secret}/sensor/DEV1/solarInputPower", "620"),
        ]
    )
    proposal = map_snapshot_to_proposal(snap)
    assert proposal.topic_family == FAMILY_ZENDURE_CLOUD_SCALAR
    assert proposal.base_topic is None
    assert proposal.config_fragment["mqtt"]["app_key"] is None
    assert "cloud_base_topic_hidden" in proposal.warnings
    # The app key must never appear anywhere in the proposal.
    blob = json.dumps(
        {
            "repr": repr(proposal),
            "fragment": proposal.config_fragment,
            "metrics": proposal.metrics,
            "warnings": proposal.warnings,
        }
    )
    assert secret not in blob


# --- role hints -------------------------------------------------------------


def test_battery_storage_plus_output_control_is_battery_inverter():
    snap = _snapshot_from(
        [("iot/PK/DEV/properties/report", json.dumps(_legacy_payload()))]
    )
    assert {"battery_storage", "output_control"} <= snap.capabilities
    proposal = map_snapshot_to_proposal(snap)
    assert proposal.role_hint == ROLE_BATTERY_INVERTER


def test_grid_like_metrics_become_grid_meter_only_when_clear():
    snap = ZendureMqttSnapshot(
        device_id="METER1",
        serial_number="METER1",
        topic_families={FAMILY_ZENSDK_HA_SCALAR},
        metrics={"gridInputPower": -120, "total_power": -120},
        capabilities=set(),
    )
    proposal = map_snapshot_to_proposal(snap)
    assert proposal.role_hint == ROLE_GRID_METER


def test_battery_device_with_grid_metric_is_not_grid_meter():
    snap = _snapshot_from(
        [
            ("Zendure/sensor/SN1/electricLevel", "43"),
            ("Zendure/sensor/SN1/gridInputPower", "-120"),
        ]
    )
    proposal = map_snapshot_to_proposal(snap)
    assert proposal.role_hint != ROLE_GRID_METER


def test_telemetry_without_output_control_is_telemetry_only():
    snap = _snapshot_from(
        [
            ("Zendure/sensor/SN1/electricLevel", "43"),
            ("Zendure/sensor/SN1/solarInputPower", "620"),
        ]
    )
    assert "output_control" not in snap.capabilities
    proposal = map_snapshot_to_proposal(snap)
    assert proposal.role_hint == ROLE_TELEMETRY_ONLY


def test_incomplete_snapshot_is_unknown_and_low_confidence():
    snap = ZendureMqttSnapshot(
        device_id="SN9",
        serial_number="SN9",
        topic_families={FAMILY_ZENSDK_HA_SCALAR},
        metrics={},
        capabilities=set(),
    )
    proposal = map_snapshot_to_proposal(snap)
    assert proposal.role_hint == ROLE_UNKNOWN
    assert proposal.confidence == "low"
    assert "insufficient_telemetry" in proposal.warnings


# --- confidence -------------------------------------------------------------


def test_limited_metrics_are_medium_confidence():
    snap = ZendureMqttSnapshot(
        device_id="SN2",
        serial_number="SN2",
        topic_families={FAMILY_ZENSDK_HA_SCALAR},
        metrics={"solarInputPower": 100},
        capabilities={"pv_input"},
    )
    proposal = map_snapshot_to_proposal(snap)
    assert proposal.confidence == "medium"


# --- config fragment --------------------------------------------------------


def test_legacy_inverter_fragment_enables_output_control():
    # A legacy JSON inverter whose product resolves to a writable model (Hyper
    # 2000) and that reports outputLimit is proposed as a controllable EMS
    # inverter with its hardware identity pinned — never inferred from the family.
    snap = _snapshot_from(
        [("iot/PK/DEV/properties/report", json.dumps(_legacy_payload(product="Hyper 2000")))]
    )
    proposal = map_snapshot_to_proposal(snap)
    fragment = proposal.config_fragment
    assert fragment["type"] == "zendure_mqtt"
    assert fragment["enabled"] is True
    assert fragment["capabilities"]["write_output_limit"] is True
    assert fragment["capabilities"]["read_power"] is True
    assert fragment["capabilities"]["read_soc"] is True
    # The resolved hardware identity is pinned into config, not a write protocol.
    assert fragment["hardware_profile"] == "hyper_2000"
    assert fragment["power_write_profile"] == "legacy_object_device_automation"
    assert "write_protocol" not in fragment["mqtt"]
    assert proposal.output_control_supported is True
    assert proposal.output_control_reason == "legacy_object_device_automation"


def test_legacy_alt_layout_inverter_enables_output_control():
    # The leading-slash legacy JSON layout is transport-compatible with the same
    # model, so a known writable model stays controllable there too.
    snap = _snapshot_from(
        [("/PK/DEV/properties/report", json.dumps(_legacy_payload(product="Hyper 2000")))]
    )
    proposal = map_snapshot_to_proposal(snap)
    fragment = proposal.config_fragment
    assert fragment["mqtt"]["topic_family"] == FAMILY_LEGACY_JSON_ALT
    assert fragment["capabilities"]["write_output_limit"] is True
    assert fragment["hardware_profile"] == "hyper_2000"
    assert proposal.output_control_supported is True


def test_scalar_inverter_fragment_stays_telemetry_only():
    # A scalar (ZenSDK/HA) inverter has no verified output-write topic, so it is
    # capability-based telemetry-only even though output_control is observed.
    snap = _snapshot_from(
        [
            ("Zendure/sensor/SN1/electricLevel", "43"),
            ("Zendure/sensor/SN1/outputLimit", "300"),
        ]
    )
    assert "output_control" in snap.capabilities
    proposal = map_snapshot_to_proposal(snap)
    fragment = proposal.config_fragment
    assert fragment["capabilities"]["write_output_limit"] is False
    assert "write_protocol" not in fragment["mqtt"]
    assert proposal.output_control_supported is False
    assert proposal.output_control_reason == "scalar_write_not_verified"


def test_write_addressable_legacy_model_defaults_to_output_control():
    # A writable, addressable model does not require a separate outputLimit
    # observation before it joins the control loop.
    snap = ZendureMqttSnapshot(
        device_id="DEV",
        serial_number="SNX",
        product_key="PK",
        product="Hyper 2000",
        topic_families={FAMILY_LEGACY_JSON},
        metrics={"inputLimit": 0, "electricLevel": 50},
        capabilities={"battery_storage"},
    )
    proposal = map_snapshot_to_proposal(snap)
    fragment = proposal.config_fragment
    assert fragment["capabilities"]["write_output_limit"] is True
    assert proposal.output_control_supported is True
    assert proposal.output_control_reason == "legacy_object_device_automation"


# --- deduplication ----------------------------------------------------------


def test_duplicate_snapshots_for_same_device_are_deduplicated():
    snap = _snapshot_from(
        [("iot/PK/DEV/properties/report", json.dumps(_legacy_payload()))]
    )
    proposals = map_snapshots_to_proposals([snap, snap])
    assert len(proposals) == 1


def test_snapshots_with_same_serial_merge_into_one_proposal():
    scalar = ZendureMqttSnapshot(
        device_id="SN1",
        serial_number="SN1",
        topic_families={FAMILY_ZENSDK_HA_SCALAR},
        metrics={"electricLevel": 43},
        capabilities={"battery_storage"},
    )
    report = ZendureMqttSnapshot(
        device_id="DEV",
        serial_number="SN1",
        product_key="PK",
        topic_families={FAMILY_LEGACY_JSON},
        metrics={"outputLimit": 300},
        capabilities={"output_control"},
    )
    proposals = map_snapshots_to_proposals([scalar, report])
    assert len(proposals) == 1
    merged = proposals[0]
    assert merged.role_hint == ROLE_BATTERY_INVERTER
    assert "electricLevel" in merged.metrics
    assert "outputLimit" in merged.metrics


def test_same_model_devices_get_unique_names():
    # Two physical units of the same model must never propose the identical
    # config name: device names are the EMS runtime identity key (controller
    # state, runtime-state.json, dashboard, history), so a collision silently
    # merges two real devices into one logical device.
    a = ZendureMqttSnapshot(
        device_id="EOD1NLN9P010902",
        serial_number="EOD1NLN9P010902",
        product_key="nVyeqM",
        product="SolarFlow 800 Pro2",
        topic_families={FAMILY_LEGACY_JSON_ALT},
        metrics={"electricLevel": 43, "outputLimit": 301},
    )
    b = ZendureMqttSnapshot(
        device_id="EOD1NLN9P010611",
        serial_number="EOD1NLN9P010611",
        product_key="nVyeqM",
        product="SolarFlow 800 Pro2",
        topic_families={FAMILY_LEGACY_JSON_ALT},
        metrics={"electricLevel": 51, "outputLimit": 120},
    )
    proposals = map_snapshots_to_proposals([a, b])
    names = [p.config_fragment["name"] for p in proposals]
    display_names = [p.display_name for p in proposals]
    assert names == [
        "Zendure MQTT SolarFlow 800 Pro2 (EOD1NLN9P010902)",
        "Zendure MQTT SolarFlow 800 Pro2 (EOD1NLN9P010611)",
    ]
    assert display_names == names


def test_single_device_keeps_plain_product_name():
    snap = ZendureMqttSnapshot(
        device_id="EOD1NLN9P010902",
        serial_number="EOD1NLN9P010902",
        product_key="nVyeqM",
        product="SolarFlow 800 Pro2",
        topic_families={FAMILY_LEGACY_JSON_ALT},
        metrics={"electricLevel": 43},
    )
    proposal = map_snapshot_to_proposal(snap)
    assert proposal.config_fragment["name"] == "Zendure MQTT SolarFlow 800 Pro2"
    assert proposal.display_name == "Zendure MQTT SolarFlow 800 Pro2"


def test_distinct_devices_produce_distinct_proposals():
    a = ZendureMqttSnapshot(
        device_id="SN1",
        serial_number="SN1",
        topic_families={FAMILY_ZENSDK_HA_SCALAR},
        metrics={"electricLevel": 43},
    )
    b = ZendureMqttSnapshot(
        device_id="SN2",
        serial_number="SN2",
        topic_families={FAMILY_ZENSDK_HA_SCALAR},
        metrics={"electricLevel": 51},
    )
    proposals = map_snapshots_to_proposals([a, b])
    assert {p.proposal_id for p in proposals} == {
        "zendure-mqtt:SN1",
        "zendure-mqtt:SN2",
    }


# --- purity -----------------------------------------------------------------


def test_mapper_does_not_mutate_input_snapshots():
    snap = _snapshot_from(
        [("iot/PK/DEV/properties/report", json.dumps(_legacy_payload()))]
    )
    before = copy.deepcopy(
        (snap.metrics, snap.capabilities, snap.topic_families, snap.battery_packs)
    )
    map_snapshots_to_proposals([snap])
    assert (
        snap.metrics,
        snap.capabilities,
        snap.topic_families,
        snap.battery_packs,
    ) == before


# --- D0 grid-meter mapping (local totalPower) -------------------------------


def _d0_proposal(messages, *, source="local_mqtt", broker_ref="local_mqtt"):
    return map_snapshot_to_proposal(
        _snapshot_from(messages), source=source, broker_ref=broker_ref
    )


def test_local_d0_total_power_maps_to_grid_meter_target():
    proposal = _d0_proposal([("Zendure/sensor/D0SN/totalPower", "-43")])
    assert proposal.role_hint == ROLE_GRID_METER
    assert proposal.target == "grid_meter"
    # The device fragment stays read-only.
    assert proposal.config_fragment["capabilities"]["read_power"] is True
    assert proposal.config_fragment["capabilities"]["write_output_limit"] is False
    fragment = proposal.grid_meter_fragment
    assert fragment is not None
    assert fragment["type"] == "zendure_smartmeter_d0"
    assert fragment["mqtt"]["broker_ref"] == "local_mqtt"
    assert fragment["mqtt"]["topic"] == "Zendure/sensor/D0SN/totalPower"
    assert fragment["mqtt"]["payload_format"] == "number"
    assert "password" not in fragment["mqtt"]
    assert "Zendure/sensor/D0SN/totalPower" in proposal.seen_topics


def test_d0_grid_meter_confidence_is_not_low_from_total_power_alone():
    proposal = _d0_proposal([("Zendure/sensor/D0SN/totalPower", "-43")])
    assert proposal.confidence in ("medium", "high")


def test_snapshot_d0_grid_meter_from_task_shape():
    snap = ZendureMqttSnapshot(
        device_id="D0SN",
        serial_number="D0SN",
        topic_families={FAMILY_ZENSDK_HA_SCALAR},
        metrics={"totalPower": -43},
        seen_topics={"Zendure/sensor/D0SN/totalPower"},
    )
    proposal = map_snapshot_to_proposal(
        snap, source="local_mqtt", broker_ref="local_mqtt"
    )
    assert proposal.role_hint == ROLE_GRID_METER
    assert proposal.target == "grid_meter"
    assert proposal.grid_meter_fragment["type"] == "zendure_smartmeter_d0"
    assert proposal.grid_meter_fragment["mqtt"]["broker_ref"] == "local_mqtt"
    assert (
        proposal.grid_meter_fragment["mqtt"]["topic"]
        == "Zendure/sensor/D0SN/totalPower"
    )
    assert proposal.grid_meter_fragment["mqtt"]["payload_format"] == "number"


@pytest.mark.parametrize(
    "topic",
    [
        "Zendure/sensor/D0SN/total_power",
        "Zendure/number/D0SN/totalPower",
        "Zendure/sensor/D0SN/totalPower/extra",
    ],
)
def test_unsafe_or_wrong_topics_do_not_create_d0_fragment(topic):
    proposal = _d0_proposal([(topic, "-43")])
    assert proposal.target == "device"
    assert proposal.grid_meter_fragment is None


@pytest.mark.parametrize(
    "topic",
    [
        "Zendure/sensor//totalPower",
        "custom/sensor/D0SN/totalPower",
        "SECRET_APP_KEY/sensor/D0SN/totalPower",
    ],
)
def test_empty_device_or_foreign_prefix_topics_are_not_d0(topic):
    snap = ZendureMqttSnapshot(
        device_id="D0SN",
        serial_number="D0SN",
        topic_families={FAMILY_ZENSDK_HA_SCALAR},
        metrics={"totalPower": -43},
        seen_topics={topic},
    )
    proposal = map_snapshot_to_proposal(
        snap, source="local_mqtt", broker_ref="local_mqtt"
    )
    assert proposal.target == "device"
    assert proposal.grid_meter_fragment is None
    # A foreign/secret-prefixed topic is never echoed back on the proposal.
    assert topic not in proposal.seen_topics or topic.startswith("Zendure/")


def test_grid_metric_without_exact_topic_emits_warning():
    # total_power metric with no exact totalPower topic: grid-meter hint stays,
    # but no auto-applicable D0 fragment.
    proposal = _d0_proposal([("Zendure/sensor/D0SN/total_power", "-43")])
    assert proposal.role_hint == ROLE_GRID_METER
    assert proposal.target == "device"
    assert "grid_power_metric_seen_but_topic_unavailable" in proposal.warnings


def test_cloud_source_does_not_auto_map_d0_grid_meter():
    proposal = _d0_proposal(
        [("Zendure/sensor/D0SN/totalPower", "-43")],
        source="zendure_cloud_mqtt",
        broker_ref="zendure_cloud",
    )
    assert proposal.target == "device"
    assert proposal.grid_meter_fragment is None


def test_mixed_inverter_metrics_do_not_become_grid_meter():
    proposal = _d0_proposal(
        [
            ("Zendure/sensor/D0SN/totalPower", "-43"),
            ("Zendure/sensor/D0SN/outputLimit", "100"),
            ("Zendure/sensor/D0SN/electricLevel", "55"),
            ("Zendure/sensor/D0SN/solarInputPower", "120"),
        ]
    )
    assert proposal.role_hint == ROLE_BATTERY_INVERTER
    assert proposal.target == "device"
    assert proposal.grid_meter_fragment is None
