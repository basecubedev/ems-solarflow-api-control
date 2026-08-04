# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for the central Zendure hardware/write-profile registry.

The registry is the single authority that separates telemetry transport
(``topic_family``) from hardware identity (``hardware_profile``) and the verified
write protocol (``power_write_profile``). Hardware is resolved from an explicit
product/model identity — never from a topic family or a substring guess.
"""

import pytest

from ems.mqtt_control.zendure_profiles import (
    IMPLEMENTED_WRITE_PROFILES,
    OPERATION_CHARGE,
    OPERATION_DISCHARGE,
    OPERATION_IDLE,
    WRITE_PROFILE_LEGACY_HUB,
    WRITE_PROFILE_LEGACY_OBJECT,
    WRITE_PROFILE_TELEMETRY_ONLY,
    WRITE_PROFILE_ZENSDK_PROPERTIES,
    ZendureHardwareProfile,
    operation_for_target,
    resolve_hardware_profile,
)

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]


# --- object-automation profiles (Hyper 2000, AIO 2400) ----------------------


def test_hyper2000_resolves_object_automation_profile():
    prof = resolve_hardware_profile("Hyper 2000")
    assert isinstance(prof, ZendureHardwareProfile)
    assert prof.canonical_name == "hyper_2000"
    assert prof.power_write_profile == WRITE_PROFILE_LEGACY_OBJECT
    assert prof.supports_discharge is True
    assert prof.supports_idle is True
    assert prof.supports_charge is True
    assert prof.writable is True


def test_hyper2000_3_0_resolves_object_automation_profile():
    prof = resolve_hardware_profile("Hyper 2000 3.0")
    assert prof is not None
    assert prof.canonical_name == "hyper_2000"
    assert prof.power_write_profile == WRITE_PROFILE_LEGACY_OBJECT
    assert prof.supports_charge is True


def test_aio2400_resolves_object_automation_profile():
    prof = resolve_hardware_profile("AIO 2400")
    assert prof is not None
    assert prof.canonical_name == "aio_2400"
    assert prof.power_write_profile == WRITE_PROFILE_LEGACY_OBJECT
    assert prof.supports_discharge is True
    assert prof.supports_idle is True
    # AIO must reject AC charging.
    assert prof.supports_charge is False


def test_solarflow_aio_zy_resolves_object_automation_profile():
    prof = resolve_hardware_profile("SolarFlow AIO ZY")
    assert prof is not None
    assert prof.canonical_name == "aio_2400"
    assert prof.power_write_profile == WRITE_PROFILE_LEGACY_OBJECT
    assert prof.supports_charge is False


# --- scalar-automation profiles (Hub 1200, Hub 2000) ------------------------


def test_hub1200_resolves_scalar_automation_profile():
    prof = resolve_hardware_profile("Hub 1200")
    assert prof is not None
    assert prof.canonical_name == "hub_1200"
    assert prof.power_write_profile == WRITE_PROFILE_LEGACY_HUB
    assert prof.supports_discharge is True
    assert prof.supports_idle is True
    # Hub devices are output-only: no AC charging.
    assert prof.supports_charge is False


def test_solarflow_2_0_resolves_scalar_automation_profile():
    prof = resolve_hardware_profile("SolarFlow 2.0")
    assert prof is not None
    assert prof.canonical_name == "hub_1200"
    assert prof.power_write_profile == WRITE_PROFILE_LEGACY_HUB
    assert prof.supports_charge is False


def test_hub2000_resolves_scalar_automation_profile():
    prof = resolve_hardware_profile("Hub 2000")
    assert prof is not None
    assert prof.canonical_name == "hub_2000"
    assert prof.power_write_profile == WRITE_PROFILE_LEGACY_HUB
    assert prof.supports_charge is False


def test_solarflow_hub2000_resolves_scalar_automation_profile():
    prof = resolve_hardware_profile("SolarFlow Hub 2000")
    assert prof is not None
    assert prof.canonical_name == "hub_2000"
    assert prof.power_write_profile == WRITE_PROFILE_LEGACY_HUB


# --- ZenSDK stays on the existing property-write path -----------------------


def test_zensdk_device_keeps_existing_properties_write_profile():
    prof = resolve_hardware_profile("SolarFlow 800 Pro 2")
    assert prof is not None
    assert prof.canonical_name == "solarflow_800_pro_2"
    assert prof.power_write_profile == WRITE_PROFILE_ZENSDK_PROPERTIES
    assert prof.writable is True


def test_zensdk_brand_model_resolves_without_explicit_alias():
    # A SolarFlow-brand model not spelled out in the alias list still resolves to
    # the ZenSDK profile via the brand token; it never falls through to unknown.
    prof = resolve_hardware_profile("SolarFlow 2400 AC")
    assert prof is not None
    assert prof.canonical_name == "solarflow_2400_ac"
    assert prof.power_write_profile == WRITE_PROFILE_ZENSDK_PROPERTIES


# --- deferred / conditional / unknown hardware ------------------------------


def test_ace1500_remains_telemetry_only():
    prof = resolve_hardware_profile("ACE 1500")
    assert prof is not None
    assert prof.canonical_name == "ace_1500"
    assert prof.power_write_profile == WRITE_PROFILE_TELEMETRY_ONLY
    assert prof.writable is False
    assert prof.supports_discharge is False
    assert prof.supports_idle is False
    assert prof.supports_charge is False


def test_superbase_v4600_is_enabled_only_with_verified_shared_contract():
    prof = resolve_hardware_profile("SuperBase V4600")
    assert prof is not None
    assert prof.canonical_name == "superbase_v4600"
    # The shared object contract is not verified against fixtures in this release,
    # so the profile stays telemetry-only and is never writable.
    assert prof.power_write_profile == WRITE_PROFILE_TELEMETRY_ONLY
    assert prof.writable is False


def test_superbase_v6400_is_enabled_only_with_verified_shared_contract():
    prof = resolve_hardware_profile("SuperBase V6400")
    assert prof is not None
    assert prof.canonical_name == "superbase_v6400"
    assert prof.power_write_profile == WRITE_PROFILE_TELEMETRY_ONLY
    assert prof.writable is False


def test_unknown_product_name_returns_no_profile():
    assert resolve_hardware_profile("Totally Unknown Widget") is None
    assert resolve_hardware_profile(None) is None
    assert resolve_hardware_profile("") is None


def test_unknown_product_name_does_not_match_by_substring():
    # 'superhub' contains 'hub', 'hyperion' contains 'hyper', but neither is a
    # whole-token/alias match, so neither may be classified as writable hardware.
    assert resolve_hardware_profile("SuperHub 9000") is None
    assert resolve_hardware_profile("Hyperion X") is None
    assert resolve_hardware_profile("Superbased Battery") is None


# --- registry invariants ----------------------------------------------------


def test_telemetry_only_profile_is_not_an_implemented_write_profile():
    assert WRITE_PROFILE_TELEMETRY_ONLY not in IMPLEMENTED_WRITE_PROFILES
    assert WRITE_PROFILE_ZENSDK_PROPERTIES in IMPLEMENTED_WRITE_PROFILES
    assert WRITE_PROFILE_LEGACY_HUB in IMPLEMENTED_WRITE_PROFILES
    assert WRITE_PROFILE_LEGACY_OBJECT in IMPLEMENTED_WRITE_PROFILES


def test_operation_for_target_maps_sign_to_operation():
    assert operation_for_target(500) == OPERATION_DISCHARGE
    assert operation_for_target(1) == OPERATION_DISCHARGE
    assert operation_for_target(0) == OPERATION_IDLE
    assert operation_for_target(-500) == OPERATION_CHARGE


def test_supported_operations_reflect_capability_flags():
    hyper = resolve_hardware_profile("Hyper 2000")
    assert set(hyper.supported_operations) == {
        OPERATION_DISCHARGE,
        OPERATION_IDLE,
        OPERATION_CHARGE,
    }
    hub = resolve_hardware_profile("Hub 2000")
    assert set(hub.supported_operations) == {OPERATION_DISCHARGE, OPERATION_IDLE}
    ace = resolve_hardware_profile("ACE 1500")
    assert ace.supported_operations == ()
