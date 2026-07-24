# SPDX-License-Identifier: AGPL-3.0-or-later
"""Multi-source hardware-model evidence resolution and conflict detection.

A device's model may be observed from several sources (an explicit reviewed user
selection, an existing persisted profile, the cloud device list, a full report,
retained metadata, a product-key mapping). Repeated *agreeing* exact evidence
stays exact; a weaker signal never overrides an exact one; but two *conflicting*
exact signals for different models must never silently pick one — the result is a
``conflict`` that stays read-only, so a misidentified device can never authorize
a write.
"""

import pytest

from ems.mqtt_control.zendure_profiles import (
    CONFIDENCE_CONFLICT,
    EVIDENCE_CLOUD_DEVICE_LIST,
    EVIDENCE_EXISTING_CONFIG,
    EVIDENCE_FULL_REPORT,
    EVIDENCE_RETAINED_METADATA,
    EVIDENCE_USER_SELECTION,
    HardwareProfileEvidence,
    make_hardware_profile_evidence,
    resolve_hardware_profile_evidence,
)

pytestmark = pytest.mark.simulation


def _ev(source, value, observed_at=None):
    return make_hardware_profile_evidence(source, value, observed_at=observed_at)


def test_evidence_record_resolves_profile_and_confidence():
    ev = _ev(EVIDENCE_CLOUD_DEVICE_LIST, "Hyper 2000")
    assert isinstance(ev, HardwareProfileEvidence)
    assert ev.source == EVIDENCE_CLOUD_DEVICE_LIST
    assert ev.value == "Hyper 2000"
    assert ev.resolved_profile == "hyper_2000"
    assert ev.confidence == "exact"


def test_single_exact_evidence_resolves_exact():
    r = resolve_hardware_profile_evidence([_ev(EVIDENCE_CLOUD_DEVICE_LIST, "Hyper 2000")])
    assert r.profile_id == "hyper_2000"
    assert r.confidence == "exact"
    assert r.writable is True


def test_same_profile_from_two_sources_remains_exact():
    r = resolve_hardware_profile_evidence(
        [
            _ev(EVIDENCE_CLOUD_DEVICE_LIST, "Hyper 2000"),
            _ev(EVIDENCE_FULL_REPORT, "Hyper2000"),
        ]
    )
    assert r.profile_id == "hyper_2000"
    assert r.confidence == "exact"


def test_weak_evidence_cannot_override_exact_evidence():
    r = resolve_hardware_profile_evidence(
        [
            _ev(EVIDENCE_CLOUD_DEVICE_LIST, "Hyper 2000"),
            _ev(EVIDENCE_FULL_REPORT, "Hyper"),  # bare brand word -> ambiguous
        ]
    )
    assert r.profile_id == "hyper_2000"
    assert r.confidence == "exact"


def test_explicit_user_selection_overrides_weaker_matching_evidence():
    r = resolve_hardware_profile_evidence(
        [
            _ev(EVIDENCE_CLOUD_DEVICE_LIST, "Hyper"),  # ambiguous
            _ev(EVIDENCE_USER_SELECTION, "hyper_2000"),
        ]
    )
    assert r.profile_id == "hyper_2000"
    assert r.confidence in ("exact", "canonical")
    assert r.writable is True


def test_explicit_user_selection_wins_over_conflicting_discovery():
    # The operator reviewed the evidence and decided: their choice is decisive.
    r = resolve_hardware_profile_evidence(
        [
            _ev(EVIDENCE_CLOUD_DEVICE_LIST, "AIO 2400"),
            _ev(EVIDENCE_FULL_REPORT, "Hub 2000"),
            _ev(EVIDENCE_USER_SELECTION, "hyper_2000"),
        ]
    )
    assert r.profile_id == "hyper_2000"
    assert r.confidence != CONFIDENCE_CONFLICT


def test_conflicting_exact_discovery_evidence_is_conflict():
    r = resolve_hardware_profile_evidence(
        [
            _ev(EVIDENCE_CLOUD_DEVICE_LIST, "Hyper 2000"),
            _ev(EVIDENCE_FULL_REPORT, "AIO 2400"),
        ]
    )
    assert r.profile_id is None
    assert r.confidence == CONFIDENCE_CONFLICT
    assert r.writable is False


def test_conflict_between_telemetry_only_and_writable_is_still_conflict():
    r = resolve_hardware_profile_evidence(
        [
            _ev(EVIDENCE_CLOUD_DEVICE_LIST, "Hyper 2000"),
            _ev(EVIDENCE_FULL_REPORT, "ACE 1500"),
        ]
    )
    assert r.profile_id is None
    assert r.confidence == CONFIDENCE_CONFLICT
    assert r.writable is False


def test_existing_persisted_profile_beats_conflicting_weaker_discovery():
    # A persisted exact profile is authoritative over lower-tier discovery.
    r = resolve_hardware_profile_evidence(
        [
            _ev(EVIDENCE_EXISTING_CONFIG, "hub_2000"),
            _ev(EVIDENCE_RETAINED_METADATA, "AIO 2400"),
        ]
    )
    assert r.profile_id == "hub_2000"
    assert r.confidence != CONFIDENCE_CONFLICT


def test_no_evidence_is_unknown_and_not_writable():
    r = resolve_hardware_profile_evidence([])
    assert r.profile_id is None
    assert r.confidence == "unknown"
    assert r.writable is False


def test_only_weak_evidence_stays_ambiguous_or_unknown():
    r = resolve_hardware_profile_evidence([_ev(EVIDENCE_CLOUD_DEVICE_LIST, "Hyper")])
    assert r.profile_id is None
    assert r.confidence in ("ambiguous", "unknown")
    assert r.writable is False
