# SPDX-License-Identifier: AGPL-3.0-or-later
"""The hardware registry is the single power-write capability authority.

``resolve_power_write_capability`` decides writability from the pinned hardware
profile and its compatibility with the telemetry transport — never from the
topic family alone. This is the authoritative transport/profile compatibility
matrix: a known writable model on a compatible transport is supported for the
operations its model allows; everything else is blocked with a stable reason.
"""

import pytest

from ems.mqtt_control.power_capability import (
    BLOCK_HARDWARE_PROFILE_DEFERRED,
    BLOCK_HARDWARE_PROFILE_MISSING,
    BLOCK_HARDWARE_PROFILE_UNKNOWN,
    BLOCK_OPERATION_UNSUPPORTED,
    BLOCK_TRANSPORT_INCOMPATIBLE,
    resolve_power_write_capability,
)
from ems.mqtt_control.zendure_profiles import (
    WRITE_PROFILE_LEGACY_HUB,
    WRITE_PROFILE_LEGACY_OBJECT,
    WRITE_PROFILE_ZENSDK_PROPERTIES,
)
from ems.zendure_mqtt.topics import (
    FAMILY_LEGACY_JSON,
    FAMILY_LEGACY_JSON_ALT,
    FAMILY_UNKNOWN,
    FAMILY_ZENSDK_HA_SCALAR,
)

pytestmark = pytest.mark.simulation


# --- writable models on a compatible transport ------------------------------


def test_hyper_on_legacy_json_supports_all_operations():
    for op in ("discharge", "idle", "charge"):
        cap = resolve_power_write_capability(
            topic_family=FAMILY_LEGACY_JSON, hardware_profile="hyper_2000", operation=op
        )
        assert cap.supported is True, op
        assert cap.profile_id == "hyper_2000"
        assert cap.write_profile == WRITE_PROFILE_LEGACY_OBJECT
        assert cap.block_reason is None
    base = resolve_power_write_capability(
        topic_family=FAMILY_LEGACY_JSON_ALT, hardware_profile="hyper_2000"
    )
    assert base.supported is True
    assert base.supported_operations == {"discharge", "idle", "charge"}


def test_aio_supports_discharge_and_idle_but_not_charge():
    ok = resolve_power_write_capability(
        topic_family=FAMILY_LEGACY_JSON, hardware_profile="aio_2400", operation="discharge"
    )
    assert ok.supported is True
    assert ok.write_profile == WRITE_PROFILE_LEGACY_OBJECT
    charge = resolve_power_write_capability(
        topic_family=FAMILY_LEGACY_JSON, hardware_profile="aio_2400", operation="charge"
    )
    assert charge.supported is False
    assert charge.block_reason == BLOCK_OPERATION_UNSUPPORTED


@pytest.mark.parametrize("profile", ["hub_1200", "hub_2000"])
def test_hub_uses_scalar_automation_and_rejects_charge(profile):
    ok = resolve_power_write_capability(
        topic_family=FAMILY_LEGACY_JSON, hardware_profile=profile, operation="discharge"
    )
    assert ok.supported is True
    assert ok.write_profile == WRITE_PROFILE_LEGACY_HUB
    charge = resolve_power_write_capability(
        topic_family=FAMILY_LEGACY_JSON, hardware_profile=profile, operation="charge"
    )
    assert charge.supported is False
    assert charge.block_reason == BLOCK_OPERATION_UNSUPPORTED


def test_zensdk_supports_discharge_idle_not_charge():
    ok = resolve_power_write_capability(
        topic_family=FAMILY_LEGACY_JSON,
        hardware_profile="solarflow_800_pro_2",
        operation="discharge",
    )
    assert ok.supported is True
    assert ok.write_profile == WRITE_PROFILE_ZENSDK_PROPERTIES
    charge = resolve_power_write_capability(
        topic_family=FAMILY_LEGACY_JSON,
        hardware_profile="solarflow_800_pro_2",
        operation="charge",
    )
    assert charge.supported is False
    assert charge.block_reason == BLOCK_OPERATION_UNSUPPORTED


# --- transport incompatibility ----------------------------------------------


def test_legacy_automation_profile_incompatible_with_scalar_transport():
    cap = resolve_power_write_capability(
        topic_family=FAMILY_ZENSDK_HA_SCALAR, hardware_profile="hyper_2000"
    )
    assert cap.supported is False
    assert cap.block_reason == BLOCK_TRANSPORT_INCOMPATIBLE


def test_unknown_transport_is_incompatible_for_writable_profile():
    cap = resolve_power_write_capability(
        topic_family=FAMILY_UNKNOWN, hardware_profile="hyper_2000"
    )
    assert cap.supported is False
    assert cap.block_reason == BLOCK_TRANSPORT_INCOMPATIBLE


# --- deferred / unknown / missing profiles ----------------------------------


@pytest.mark.parametrize("profile", ["ace_1500", "superbase_v4600", "superbase_v6400"])
def test_deferred_profiles_are_never_writable(profile):
    cap = resolve_power_write_capability(
        topic_family=FAMILY_LEGACY_JSON, hardware_profile=profile
    )
    assert cap.supported is False
    assert cap.block_reason == BLOCK_HARDWARE_PROFILE_DEFERRED
    assert cap.supported_operations == frozenset()


def test_missing_profile_blocks_with_missing_reason():
    for value in (None, "", "   "):
        cap = resolve_power_write_capability(
            topic_family=FAMILY_LEGACY_JSON, hardware_profile=value
        )
        assert cap.supported is False
        assert cap.block_reason == BLOCK_HARDWARE_PROFILE_MISSING


def test_unknown_pinned_profile_blocks_with_unknown_reason():
    cap = resolve_power_write_capability(
        topic_family=FAMILY_LEGACY_JSON, hardware_profile="typo_profile"
    )
    assert cap.supported is False
    assert cap.block_reason == BLOCK_HARDWARE_PROFILE_UNKNOWN


def test_topic_family_alone_never_authorizes_a_write():
    # No hardware profile: a legacy JSON transport must NOT be writable.
    cap = resolve_power_write_capability(
        topic_family=FAMILY_LEGACY_JSON, hardware_profile=None
    )
    assert cap.supported is False
