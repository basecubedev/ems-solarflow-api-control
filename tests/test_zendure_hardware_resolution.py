# SPDX-License-Identifier: AGPL-3.0-or-later
"""Strict, evidence-first hardware-model resolution.

Hardware identity is resolved only from an exact (normalized) alias or canonical
name. A bare brand/family word never authorizes a writable model, and a
future/unknown model in a known line (Hyper 3000, AIO 3000) resolves to nothing.
Real product strings are camelCase / glued / punctuated, so normalization splits
camelCase and letter/digit boundaries before matching. ``resolve_hardware_profile
_detail`` returns structured resolution metadata whose confidence gates writes.
"""

import pytest

from ems.mqtt_control.zendure_profiles import (
    HardwareProfileResolution,
    resolve_hardware_profile,
    resolve_hardware_profile_detail,
)

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]


# --- future / over-broad models never inherit a family write profile --------


def test_future_hyper_model_does_not_inherit_hyper_2000():
    assert resolve_hardware_profile("Hyper 3000") is None


def test_future_aio_model_does_not_inherit_aio_2400():
    assert resolve_hardware_profile("AIO 3000") is None


@pytest.mark.parametrize("word", ["Hyper", "AIO", "Hub", "SolarFlow", "SolarFlow Mystery"])
def test_bare_family_or_brand_words_do_not_resolve_to_a_writable_model(word):
    assert resolve_hardware_profile(word) is None


# --- normalization: real camelCase / glued / punctuated product strings ------


def test_camelcase_glued_solarflow_resolves_via_alias():
    prof = resolve_hardware_profile("solarFlow800Pro")
    assert prof is not None
    assert prof.canonical_name == "solarflow_800_pro"


def test_glued_hyper_resolves_via_alias():
    prof = resolve_hardware_profile("Hyper2000")
    assert prof is not None
    assert prof.canonical_name == "hyper_2000"


def test_punctuated_aio_resolves_via_alias():
    prof = resolve_hardware_profile("SolarFlow-AIO-ZY")
    assert prof is not None
    assert prof.canonical_name == "aio_2400"


def test_hyper_with_underscore_and_dotted_revision_resolves():
    prof = resolve_hardware_profile("Hyper2000_3.0")
    assert prof is not None
    assert prof.canonical_name == "hyper_2000"


# --- structured resolution metadata -----------------------------------------


def test_resolution_detail_exact_alias_is_writable():
    r = resolve_hardware_profile_detail("Hyper 2000")
    assert isinstance(r, HardwareProfileResolution)
    assert r.profile_id == "hyper_2000"
    assert r.confidence == "exact"
    assert r.matched_alias == "Hyper 2000"
    assert r.source_value == "Hyper 2000"
    assert r.writable is True


def test_resolution_detail_canonical_name_is_writable():
    r = resolve_hardware_profile_detail("hyper_2000")
    assert r.profile_id == "hyper_2000"
    assert r.confidence == "canonical"
    assert r.writable is True


def test_resolution_detail_bare_family_word_is_ambiguous_and_not_writable():
    r = resolve_hardware_profile_detail("Hyper")
    assert r.profile_id is None
    assert r.confidence == "ambiguous"
    assert r.writable is False


def test_resolution_detail_unknown_is_not_writable():
    r = resolve_hardware_profile_detail("Totally Unknown Widget")
    assert r.profile_id is None
    assert r.confidence == "unknown"
    assert r.writable is False


def test_resolution_detail_empty_is_unknown():
    for value in (None, "", "   "):
        r = resolve_hardware_profile_detail(value)
        assert r.profile_id is None
        assert r.confidence == "unknown"
        assert r.writable is False


def test_resolution_detail_preserves_camelcase_source_value():
    r = resolve_hardware_profile_detail("solarFlow800Pro")
    assert r.profile_id == "solarflow_800_pro"
    assert r.confidence == "exact"
    assert r.source_value == "solarFlow800Pro"


# --- writable reflects actual implemented write support, not just confidence --
#
# A telemetry-only model (ACE 1500, SuperBase) resolves with EXACT confidence but
# carries no implemented power_write_profile: `writable` must stay False so it can
# never authorize a hardware write. Confidence alone is not write authority.


@pytest.mark.parametrize(
    "product,profile_id",
    [
        ("Hyper 2000", "hyper_2000"),
        ("Hub 2000", "hub_2000"),
        ("AIO 2400", "aio_2400"),
        ("Hub 1200", "hub_1200"),
        ("SolarFlow 800 Pro", "solarflow_800_pro"),
    ],
)
def test_implemented_models_resolve_writable(product, profile_id):
    r = resolve_hardware_profile_detail(product)
    assert r.profile_id == profile_id
    assert r.confidence in ("exact", "canonical")
    assert r.writable is True


@pytest.mark.parametrize(
    "product,profile_id",
    [
        ("ACE 1500", "ace_1500"),
        ("SuperBase V4600", "superbase_v4600"),
        ("SuperBase V6400", "superbase_v6400"),
    ],
)
def test_telemetry_only_models_are_identified_but_not_writable(product, profile_id):
    r = resolve_hardware_profile_detail(product)
    # Identity is preserved (so it can be persisted) ...
    assert r.profile_id == profile_id
    assert r.confidence == "exact"
    # ... but the model is telemetry-only, so it must never be writable.
    assert r.writable is False


def test_ambiguous_and_unknown_resolutions_are_not_writable():
    assert resolve_hardware_profile_detail("Hyper").writable is False
    assert resolve_hardware_profile_detail("Totally Unknown").writable is False
