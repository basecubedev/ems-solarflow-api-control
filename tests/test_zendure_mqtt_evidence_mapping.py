# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discovery-mapper hardware identity: persist telemetry-only, block conflicts.

A resolved hardware model is pinned into the proposed config even when the model
is read-only (ACE 1500, SuperBase), so a future firmware/support upgrade never
has to rediscover it. When two snapshots for the same logical device report
different exact models, the proposal must not silently pick one — it resolves to
a read-only conflict that carries no writable profile through serialization.
"""

import pytest

from admin.zendure_mqtt_config_proposals import _proposal_to_dict
from ems.zendure_mqtt import map_snapshots_to_proposals
from ems.zendure_mqtt.snapshot import ZendureMqttSnapshot
from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]


def _legacy_snapshot(serial, product):
    return ZendureMqttSnapshot(
        device_id=serial,
        serial_number=serial,
        product_key="PK",
        product=product,
        topic_families={FAMILY_LEGACY_JSON},
        metrics={"outputLimit": 300, "electricLevel": 50},
        capabilities={"battery_storage", "output_control"},
    )


def _proposal_for(serial, product):
    return map_snapshots_to_proposals([_legacy_snapshot(serial, product)], source="local_mqtt")[0]


# --- Phase 6: telemetry-only identity is persisted --------------------------


@pytest.mark.parametrize(
    "product,profile_id",
    [
        ("ACE 1500", "ace_1500"),
        ("SuperBase V4600", "superbase_v4600"),
        ("SuperBase V6400", "superbase_v6400"),
    ],
)
def test_telemetry_only_profile_is_persisted_without_control(product, profile_id):
    proposal = _proposal_for("SNT", product)
    fragment = proposal.config_fragment
    assert fragment["hardware_profile"] == profile_id
    assert fragment["power_write_profile"] == "telemetry_only"
    assert fragment["capabilities"]["write_output_limit"] is False
    assert proposal.output_control_supported is False


def test_unknown_model_persists_no_hardware_profile():
    proposal = _proposal_for("SNU", "Totally Unknown Widget")
    fragment = proposal.config_fragment
    assert "hardware_profile" not in fragment
    assert fragment["capabilities"]["write_output_limit"] is False


def test_telemetry_only_profile_survives_serialization_roundtrip():
    proposal = _proposal_for("SNT", "ACE 1500")
    data = _proposal_to_dict(proposal)
    # The serialized fragment keeps the pinned model so a later upgrade re-uses it.
    assert data["config_fragment"]["hardware_profile"] == "ace_1500"
    assert data["hardware_model"] == "ace_1500"


# --- Phase 5: conflicting exact evidence is a read-only conflict -------------


def test_conflicting_models_produce_read_only_conflict():
    proposals = map_snapshots_to_proposals(
        [
            _legacy_snapshot("SNC", "Hyper 2000"),
            _legacy_snapshot("SNC", "AIO 2400"),
        ],
        source="local_mqtt",
    )
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.hardware_profile_confidence == "conflict"
    assert proposal.control_block_reason == "hardware_profile_conflict"
    assert proposal.output_control_supported is False
    fragment = proposal.config_fragment
    assert "hardware_profile" not in fragment
    assert fragment["capabilities"]["write_output_limit"] is False


def test_conflict_is_visible_in_admin_review_dict():
    proposals = map_snapshots_to_proposals(
        [
            _legacy_snapshot("SNC", "Hyper 2000"),
            _legacy_snapshot("SNC", "AIO 2400"),
        ],
        source="local_mqtt",
    )
    data = _proposal_to_dict(proposals[0])
    assert data["hardware_profile_confidence"] == "conflict"
    assert data["control_block_reason"] == "hardware_profile_conflict"
    assert data["output_control_supported"] is False
    assert data["hardware_model"] is None


def test_agreeing_models_are_not_a_conflict():
    proposals = map_snapshots_to_proposals(
        [
            _legacy_snapshot("SNA", "Hyper 2000"),
            _legacy_snapshot("SNA", "Hyper2000"),
        ],
        source="local_mqtt",
    )
    proposal = proposals[0]
    assert proposal.hardware_profile_confidence in ("exact", "canonical")
    assert proposal.config_fragment["hardware_profile"] == "hyper_2000"
    assert proposal.control_block_reason is None
