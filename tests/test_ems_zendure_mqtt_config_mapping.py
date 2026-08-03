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

pytestmark = [
    pytest.mark.config,
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]


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
    proposal = map_snapshot_to_proposal(snap, source="local_mqtt")
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
    proposal = map_snapshot_to_proposal(snap, source="local_mqtt")
    assert proposal.topic_family == FAMILY_LEGACY_JSON
    assert proposal.base_topic == "iot"
    assert proposal.device_id == "DEV"
    assert proposal.product_key == "PK"
    assert proposal.serial_number == "SN-REAL"


def test_legacy_slash_json_becomes_proposal():
    snap = _snapshot_from(
        [("/PK/DEV/properties/report", json.dumps(_legacy_payload()))]
    )
    proposal = map_snapshot_to_proposal(snap, source="local_mqtt")
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
    proposal = map_snapshot_to_proposal(snap, source="local_mqtt")
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
    proposal = map_snapshot_to_proposal(snap, source="local_mqtt")
    assert proposal.role_hint == ROLE_BATTERY_INVERTER


def test_grid_like_metrics_become_grid_meter_only_when_clear():
    snap = ZendureMqttSnapshot(
        device_id="METER1",
        serial_number="METER1",
        topic_families={FAMILY_ZENSDK_HA_SCALAR},
        metrics={"gridInputPower": -120, "total_power": -120},
        capabilities=set(),
    )
    proposal = map_snapshot_to_proposal(snap, source="local_mqtt")
    assert proposal.role_hint == ROLE_GRID_METER


def test_battery_device_with_grid_metric_is_not_grid_meter():
    snap = _snapshot_from(
        [
            ("Zendure/sensor/SN1/electricLevel", "43"),
            ("Zendure/sensor/SN1/gridInputPower", "-120"),
        ]
    )
    proposal = map_snapshot_to_proposal(snap, source="local_mqtt")
    assert proposal.role_hint != ROLE_GRID_METER


def test_telemetry_without_output_control_is_telemetry_only():
    snap = _snapshot_from(
        [
            ("Zendure/sensor/SN1/electricLevel", "43"),
            ("Zendure/sensor/SN1/solarInputPower", "620"),
        ]
    )
    assert "output_control" not in snap.capabilities
    proposal = map_snapshot_to_proposal(snap, source="local_mqtt")
    assert proposal.role_hint == ROLE_TELEMETRY_ONLY


def test_incomplete_snapshot_is_unknown_and_low_confidence():
    snap = ZendureMqttSnapshot(
        device_id="SN9",
        serial_number="SN9",
        topic_families={FAMILY_ZENSDK_HA_SCALAR},
        metrics={},
        capabilities=set(),
    )
    proposal = map_snapshot_to_proposal(snap, source="local_mqtt")
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
    proposal = map_snapshot_to_proposal(snap, source="local_mqtt")
    assert proposal.confidence == "medium"


# --- config fragment --------------------------------------------------------


def test_legacy_inverter_fragment_enables_output_control():
    # A legacy JSON inverter whose product resolves to a writable model (Hyper
    # 2000) and that reports outputLimit is proposed as a controllable EMS
    # inverter with its hardware identity pinned — never inferred from the family.
    snap = _snapshot_from(
        [("iot/PK/DEV/properties/report", json.dumps(_legacy_payload(product="Hyper 2000")))]
    )
    proposal = map_snapshot_to_proposal(snap, source="local_mqtt")
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
    proposal = map_snapshot_to_proposal(snap, source="local_mqtt")
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
    proposal = map_snapshot_to_proposal(snap, source="local_mqtt")
    fragment = proposal.config_fragment
    assert fragment["capabilities"]["write_output_limit"] is False
    assert "write_protocol" not in fragment["mqtt"]
    assert proposal.output_control_supported is False
    # A scalar topic identifies neither the model nor a product key, so no write
    # method resolves — the family itself is not the blocker.
    assert proposal.output_control_reason == "write_method_missing"


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
    proposal = map_snapshot_to_proposal(snap, source="local_mqtt")
    fragment = proposal.config_fragment
    assert fragment["capabilities"]["write_output_limit"] is True
    assert proposal.output_control_supported is True
    assert proposal.output_control_reason == "legacy_object_device_automation"


# --- deduplication ----------------------------------------------------------


def test_duplicate_snapshots_for_same_device_are_deduplicated():
    snap = _snapshot_from(
        [("iot/PK/DEV/properties/report", json.dumps(_legacy_payload()))]
    )
    proposals = map_snapshots_to_proposals([snap, snap], source="local_mqtt")
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
    proposals = map_snapshots_to_proposals([scalar, report], source="local_mqtt")
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
    proposals = map_snapshots_to_proposals([a, b], source="local_mqtt")
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
    proposal = map_snapshot_to_proposal(snap, source="local_mqtt")
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
    proposals = map_snapshots_to_proposals([a, b], source="local_mqtt")
    assert {p.proposal_id for p in proposals} == {
        "zendure-mqtt:SN1",
        "zendure-mqtt:SN2",
    }


# --- identity-alias grouping (route enrichment) -----------------------------


def _route_only_snapshot(device_id="ROUTE-1", product_key="PK"):
    return ZendureMqttSnapshot(
        device_id=device_id,
        serial_number=None,
        product_key=product_key,
        product="Hyper 2000",
        topic_families={FAMILY_LEGACY_JSON},
        metrics={"outputLimit": 100},
        capabilities={"output_control"},
    )


def _serial_bearing_snapshot(device_id="ROUTE-1", serial="SERIAL-1", product_key="PK"):
    return ZendureMqttSnapshot(
        device_id=device_id,
        serial_number=serial,
        product_key=product_key,
        product="Hyper 2000",
        topic_families={FAMILY_LEGACY_JSON},
        metrics={"electricLevel": 50},
        capabilities={"battery_storage"},
    )


def test_route_only_and_serial_bearing_same_route_merge_into_one_proposal():
    # A serial-less route observation and a later serial-bearing observation of
    # the same scoped route are the same physical inverter (identity enrichment),
    # so they must never split into two proposals.
    proposals = map_snapshots_to_proposals(
        [_route_only_snapshot(), _serial_bearing_snapshot()],
        source="zendure_cloud_mqtt",
        broker_ref="zendure_cloud",
    )
    assert len(proposals) == 1
    merged = proposals[0]
    assert merged.serial_number == "SERIAL-1"
    assert merged.device_id == "ROUTE-1"
    # Telemetry, metrics and capabilities from both observations are preserved.
    assert "outputLimit" in merged.metrics
    assert "electricLevel" in merged.metrics
    assert "output_control" in merged.capabilities
    assert "battery_storage" in merged.capabilities
    assert "identity_route_serial_conflict" not in merged.warnings


def test_serial_then_route_only_order_still_merges():
    proposals = map_snapshots_to_proposals(
        [_serial_bearing_snapshot(), _route_only_snapshot()],
        source="zendure_cloud_mqtt",
        broker_ref="zendure_cloud",
    )
    assert len(proposals) == 1
    assert proposals[0].serial_number == "SERIAL-1"


def test_same_route_claiming_two_serials_is_blocked_not_merged():
    # Two contradictory physical serials on one scoped route must never be
    # silently merged; each stays a distinct proposal, flagged as an identity
    # conflict, and control is blocked because the write target is ambiguous.
    proposals = map_snapshots_to_proposals(
        [
            _serial_bearing_snapshot(serial="SERIAL-1"),
            _serial_bearing_snapshot(serial="SERIAL-2"),
        ],
        source="zendure_cloud_mqtt",
        broker_ref="zendure_cloud",
    )
    assert len(proposals) == 2
    assert {p.serial_number for p in proposals} == {"SERIAL-1", "SERIAL-2"}
    for proposal in proposals:
        assert "identity_route_serial_conflict" in proposal.warnings
        assert proposal.output_control_supported is False
        assert proposal.control_block_reason == "identity_route_serial_conflict"


def test_route_only_shared_with_conflicting_serials_does_not_bridge_merge():
    # A route-only observation that shares a contested route with two different
    # serials must not act as a bridge that merges the two serials together.
    proposals = map_snapshots_to_proposals(
        [
            _serial_bearing_snapshot(serial="SERIAL-1"),
            _serial_bearing_snapshot(serial="SERIAL-2"),
            _route_only_snapshot(),
        ],
        source="zendure_cloud_mqtt",
        broker_ref="zendure_cloud",
    )
    serials = {p.serial_number for p in proposals}
    assert "SERIAL-1" in serials
    assert "SERIAL-2" in serials
    # The two serial proposals are never collapsed into one.
    assert len([p for p in proposals if p.serial_number in {"SERIAL-1", "SERIAL-2"}]) == 2


def test_serial_less_observations_of_one_route_merge_into_one_proposal():
    # Two serial-less observations of the same device route (differing product key
    # / topic family) are one physical device and must merge into one proposal —
    # never split into two with a colliding id and name.
    a = ZendureMqttSnapshot(
        device_id="DEVICE-X",
        serial_number=None,
        product_key=None,
        product="Hyper 2000",
        topic_families={FAMILY_ZENSDK_HA_SCALAR},
        metrics={"electricLevel": 50},
        capabilities={"battery_storage"},
    )
    b = ZendureMqttSnapshot(
        device_id="DEVICE-X",
        serial_number=None,
        product_key="PK",
        product="Hyper 2000",
        topic_families={FAMILY_LEGACY_JSON},
        metrics={"outputLimit": 100},
        capabilities={"output_control"},
    )
    proposals = map_snapshots_to_proposals(
        [a, b], source="local_mqtt", broker_ref="local_mqtt"
    )
    assert len(proposals) == 1
    merged = proposals[0]
    assert merged.role_hint == ROLE_BATTERY_INVERTER
    assert "electricLevel" in merged.metrics
    assert "outputLimit" in merged.metrics


def _serialless_pk_snapshot(device_id="DEVICE-X", product_key="PK-A", metric="outputLimit"):
    return ZendureMqttSnapshot(
        device_id=device_id,
        serial_number=None,
        product_key=product_key,
        product="Hyper 2000",
        topic_families={FAMILY_LEGACY_JSON},
        metrics={metric: 100},
        capabilities={"output_control"},
    )


def test_two_known_product_keys_on_one_device_id_are_blocked_not_merged():
    # Defect 3: two serial-less observations sharing a device id but carrying
    # different known product keys are two distinct precise routes. They must not
    # merge (which would mix metrics onto one control target); each stays a
    # separate blocked proposal and control is off (ambiguous write address).
    proposals = map_snapshots_to_proposals(
        [
            _serialless_pk_snapshot(product_key="PK-A", metric="outputLimit"),
            _serialless_pk_snapshot(product_key="PK-B", metric="inputLimit"),
        ],
        source="zendure_cloud_mqtt",
        broker_ref="zendure_cloud",
    )
    assert len(proposals) == 2
    assert {p.product_key for p in proposals} == {"PK-A", "PK-B"}
    for proposal in proposals:
        assert "identity_route_product_conflict" in proposal.warnings
        assert proposal.output_control_supported is False
        assert proposal.control_block_reason == "identity_route_product_conflict"
    # Metrics are never mixed across the two conflicting routes.
    by_pk = {p.product_key: p for p in proposals}
    assert "outputLimit" in by_pk["PK-A"].metrics
    assert "outputLimit" not in by_pk["PK-B"].metrics
    assert "inputLimit" in by_pk["PK-B"].metrics


def test_missing_product_observation_does_not_bridge_two_product_routes():
    # Defect 3: an unknown-product observation of a contested device id must not
    # bridge the two known product routes. It becomes its own blocked observation.
    missing = ZendureMqttSnapshot(
        device_id="DEVICE-X",
        serial_number=None,
        product_key=None,
        product="Hyper 2000",
        topic_families={FAMILY_LEGACY_JSON},
        metrics={"solarInputPower": 5},
        capabilities=set(),
    )
    proposals = map_snapshots_to_proposals(
        [
            _serialless_pk_snapshot(product_key="PK-A", metric="outputLimit"),
            _serialless_pk_snapshot(product_key="PK-B", metric="inputLimit"),
            missing,
        ],
        source="zendure_cloud_mqtt",
        broker_ref="zendure_cloud",
    )
    # PK-A, PK-B, and the ambiguous missing-product observation stay separate.
    assert len(proposals) == 3
    known = [p for p in proposals if p.product_key in {"PK-A", "PK-B"}]
    assert len(known) == 2
    # The missing-product observation never absorbed a known route's metrics.
    for proposal in known:
        assert "solarInputPower" not in proposal.metrics


def test_same_serial_gains_additional_route_is_one_proposal():
    a = _serial_bearing_snapshot(device_id="ROUTE-A", serial="SERIAL-1", product_key="PK")
    b = _serial_bearing_snapshot(device_id="ROUTE-B", serial="SERIAL-1", product_key="PK")
    proposals = map_snapshots_to_proposals(
        [a, b], source="zendure_cloud_mqtt", broker_ref="zendure_cloud"
    )
    assert len(proposals) == 1
    assert proposals[0].serial_number == "SERIAL-1"


def _serialless_route_snapshot(device_id, product_key, metric):
    return ZendureMqttSnapshot(
        device_id=device_id,
        serial_number=None,
        product_key=product_key,
        product="Hyper 2000",
        topic_families={FAMILY_LEGACY_JSON},
        metrics={metric: 100},
        capabilities={"output_control"},
    )


def test_serial_backed_conflicting_serialless_route_blocks_writes():
    # Defect 3 (repro A): a serial-bearing observation and a serial-less
    # observation share a device id but carry different product keys — one physical
    # inverter, two precise write routes. Control is blocked and no product key is
    # pinned into a writable config.
    a = _serial_bearing_snapshot(device_id="DEV", serial="SERIAL-1", product_key="PK-A")
    b = _serialless_route_snapshot("DEV", "PK-B", "inputLimit")
    proposals = map_snapshots_to_proposals(
        [a, b], source="zendure_cloud_mqtt", broker_ref="zendure_cloud"
    )
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.serial_number == "SERIAL-1"
    assert proposal.output_control_supported is False
    assert proposal.control_block_reason == "identity_route_product_conflict"
    assert "identity_route_product_conflict" in proposal.warnings
    assert "product_key" not in proposal.config_fragment["mqtt"]
    assert proposal.config_fragment["capabilities"]["write_output_limit"] is False
    assert proposal.product_key is None


def test_serial_no_product_plus_two_serialless_routes_blocks():
    # Defect 3 (repro B): a serial with no product key plus two serial-less
    # observations carrying two product keys on one device id. One inverter, two
    # routes → blocked, no pin.
    a = _serial_bearing_snapshot(device_id="DEV", serial="SERIAL-1", product_key=None)
    b = _serialless_route_snapshot("DEV", "PK-A", "outputLimit")
    c = _serialless_route_snapshot("DEV", "PK-B", "inputLimit")
    proposals = map_snapshots_to_proposals(
        [a, b, c], source="zendure_cloud_mqtt", broker_ref="zendure_cloud"
    )
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.serial_number == "SERIAL-1"
    assert proposal.control_block_reason == "identity_route_product_conflict"
    assert "product_key" not in proposal.config_fragment["mqtt"]


def test_same_serial_two_product_keys_blocks_not_first_wins():
    # Defect 3 (repro C): the same serial reports two different product keys on one
    # device id. Never silently pick the first; block control and drop the pin.
    a = _serial_bearing_snapshot(device_id="DEV", serial="SERIAL-1", product_key="PK-A")
    b = _serial_bearing_snapshot(device_id="DEV", serial="SERIAL-1", product_key="PK-B")
    proposals = map_snapshots_to_proposals(
        [a, b], source="zendure_cloud_mqtt", broker_ref="zendure_cloud"
    )
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.serial_number == "SERIAL-1"
    assert proposal.control_block_reason == "identity_route_product_conflict"
    assert "identity_route_product_conflict" in proposal.warnings
    assert "product_key" not in proposal.config_fragment["mqtt"]


def test_route_conflict_does_not_make_serial_a_second_inverter():
    # Defect 3: route ambiguity blocks control but never turns one physical serial
    # into two inverters.
    a = _serial_bearing_snapshot(device_id="DEV", serial="SERIAL-1", product_key="PK-A")
    b = _serial_bearing_snapshot(device_id="DEV", serial="SERIAL-1", product_key="PK-B")
    proposals = map_snapshots_to_proposals(
        [a, b], source="zendure_cloud_mqtt", broker_ref="zendure_cloud"
    )
    assert len({p.serial_number for p in proposals}) == 1


def test_case_distinct_device_ids_do_not_merge():
    # Defect 2: two device ids differing only in case are distinct MQTT routes and
    # never collapse into one proposal.
    a = _serialless_route_snapshot("DEV", "PK", "outputLimit")
    b = _serialless_route_snapshot("dev", "PK", "electricLevel")
    proposals = map_snapshots_to_proposals(
        [a, b], source="zendure_cloud_mqtt", broker_ref="zendure_cloud"
    )
    assert len(proposals) == 2


def test_case_distinct_product_keys_are_distinct_routes():
    # Defect 2 + 3: "PK" and "pk" on one device id are two distinct routes, blocked
    # and never merged.
    a = _serialless_route_snapshot("DEV", "PK", "outputLimit")
    b = _serialless_route_snapshot("DEV", "pk", "inputLimit")
    proposals = map_snapshots_to_proposals(
        [a, b], source="zendure_cloud_mqtt", broker_ref="zendure_cloud"
    )
    assert len(proposals) == 2
    assert {p.product_key for p in proposals} == {"PK", "pk"}
    for proposal in proposals:
        assert proposal.control_block_reason == "identity_route_product_conflict"


# --- purity -----------------------------------------------------------------


def test_mapper_does_not_mutate_input_snapshots():
    snap = _snapshot_from(
        [("iot/PK/DEV/properties/report", json.dumps(_legacy_payload()))]
    )
    before = copy.deepcopy(
        (snap.metrics, snap.capabilities, snap.topic_families, snap.battery_packs)
    )
    map_snapshots_to_proposals([snap], source="local_mqtt")
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
