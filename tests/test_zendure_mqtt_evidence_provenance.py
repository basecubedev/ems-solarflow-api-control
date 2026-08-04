# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real model-evidence provenance is preserved through discovery and review.

A model observation is not blindly labelled ``full_report``: each observation
carries its actual source (telemetry full report, an existing config, a reviewed
user selection, ...). Sanitized evidence sources are carried into the proposal
and the Admin review DTO, a decisive reviewed source resolves a conflict, and no
raw secret product key is ever exposed as evidence.
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


def _snapshot(serial, product, *, product_key="SECRETPK"):
    return ZendureMqttSnapshot(
        device_id=serial,
        serial_number=serial,
        product_key=product_key,
        product=product,
        topic_families={FAMILY_LEGACY_JSON},
        metrics={"outputLimit": 300, "electricLevel": 50},
        capabilities={"battery_storage", "output_control"},
    )


def _sources(proposal):
    return {(e["source"], e["value"]) for e in proposal.hardware_profile_evidence_sources}


def test_telemetry_evidence_source_is_full_report():
    proposal = map_snapshots_to_proposals([_snapshot("SN1", "Hyper 2000")])[0]
    assert ("full_report", "Hyper 2000") in _sources(proposal)


def test_seeded_existing_config_evidence_source_is_preserved():
    proposal = map_snapshots_to_proposals(
        [_snapshot("SN2", "Hyper 2000")],
        seed_evidence={"SN2": [("existing_config", "Hyper 2000")]},
    )[0]
    sources = {s for s, _ in _sources(proposal)}
    assert "existing_config" in sources
    assert "full_report" in sources


def test_reviewed_user_selection_resolves_a_conflict():
    # Two disagreeing telemetry reports conflict...
    conflict = map_snapshots_to_proposals(
        [_snapshot("SN3", "Hyper 2000"), _snapshot("SN3", "AIO 2400")]
    )[0]
    assert conflict.hardware_profile_confidence == "conflict"
    # ...until a reviewed user selection decisively resolves it.
    resolved = map_snapshots_to_proposals(
        [_snapshot("SN3", "Hyper 2000"), _snapshot("SN3", "AIO 2400")],
        seed_evidence={"SN3": [("user_selection", "AIO 2400")]},
    )[0]
    assert resolved.hardware_profile == "aio_2400"
    assert resolved.hardware_profile_confidence in ("exact", "canonical")


def test_evidence_sources_never_leak_the_product_key():
    proposal = map_snapshots_to_proposals(
        [_snapshot("SN4", "Hyper 2000", product_key="SUPERSECRETKEY")]
    )[0]
    for entry in proposal.hardware_profile_evidence_sources:
        assert "SUPERSECRETKEY" not in str(entry.get("value"))


def test_admin_review_dict_exposes_evidence_sources():
    proposal = map_snapshots_to_proposals([_snapshot("SN5", "Hyper 2000")])[0]
    data = _proposal_to_dict(proposal)
    assert any(
        e["source"] == "full_report" for e in data["hardware_profile_evidence_sources"]
    )
