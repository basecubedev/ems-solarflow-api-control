# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the shared MQTT output-control capability decision."""

import pytest

from ems.zendure_mqtt.capability import (
    CAPABILITY_SUPPORTED,
    CAPABILITY_UNKNOWN,
    CAPABILITY_UNSUPPORTED,
    mqtt_output_control_capability,
    proposal_output_control,
)
from ems.zendure_mqtt.topics import (
    FAMILY_LEGACY_JSON,
    FAMILY_LEGACY_JSON_ALT,
    FAMILY_UNKNOWN,
    FAMILY_ZENDURE_CLOUD_SCALAR,
    FAMILY_ZENSDK_HA_SCALAR,
)

pytestmark = pytest.mark.simulation


def test_legacy_json_family_with_writable_profile_is_supported():
    cap = mqtt_output_control_capability(
        topic_family=FAMILY_LEGACY_JSON,
        hardware_profile="hyper_2000",
        observed_capabilities={"output_control"},
    )
    assert cap.status == CAPABILITY_SUPPORTED
    assert cap.supported is True
    assert cap.hardware_profile == "hyper_2000"
    assert cap.power_write_profile == "legacy_object_device_automation"


def test_legacy_alt_layout_with_writable_profile_is_supported():
    cap = mqtt_output_control_capability(
        topic_family=FAMILY_LEGACY_JSON_ALT, hardware_profile="hyper_2000"
    )
    assert cap.supported is True
    assert cap.power_write_profile == "legacy_object_device_automation"


def test_legacy_family_alone_is_not_supported():
    # No pinned hardware profile: a bare legacy family never authorizes control.
    cap = mqtt_output_control_capability(
        topic_family=FAMILY_LEGACY_JSON, observed_capabilities={"output_control"}
    )
    assert cap.supported is False
    assert cap.block_reason == "hardware_profile_missing"


@pytest.mark.parametrize(
    "family", [FAMILY_ZENSDK_HA_SCALAR, FAMILY_ZENDURE_CLOUD_SCALAR]
)
def test_scalar_families_are_unsupported(family):
    cap = mqtt_output_control_capability(
        topic_family=family, observed_capabilities={"output_control"}
    )
    assert cap.status == CAPABILITY_UNSUPPORTED
    assert cap.supported is False
    assert cap.reason == "scalar_write_not_verified"
    assert cap.write_protocol is None


@pytest.mark.parametrize("family", [FAMILY_UNKNOWN, "", None, "something_else"])
def test_unknown_family_is_unknown(family):
    cap = mqtt_output_control_capability(topic_family=family)
    assert cap.status == CAPABILITY_UNKNOWN
    assert cap.reason == "write_method_missing"


def test_explicit_custom_protocol_makes_scalar_supported():
    # An explicit, supported write method overrides family inference so an
    # operator-verified writable device is not forced telemetry-only.
    cap = mqtt_output_control_capability(
        topic_family=FAMILY_ZENSDK_HA_SCALAR,
        write_protocol="custom_properties_write",
    )
    assert cap.supported is True
    assert cap.write_protocol == "custom_properties_write"


def test_proposal_supported_model_defaults_to_output_control_without_observation():
    # A supported transport/model is immediately controllable; telemetry
    # observation is evidence, not an extra operator gate.
    cap = mqtt_output_control_capability(
        topic_family=FAMILY_LEGACY_JSON,
        hardware_profile="hyper_2000",
        observed_capabilities=set(),
    )
    enabled, reason = proposal_output_control(cap)
    assert enabled is True
    assert reason == "legacy_object_device_automation"


def test_proposal_enables_when_supported_and_observed():
    cap = mqtt_output_control_capability(
        topic_family=FAMILY_LEGACY_JSON,
        hardware_profile="hyper_2000",
        observed_capabilities={"output_control"},
    )
    enabled, reason = proposal_output_control(cap)
    assert enabled is True
    assert reason == "legacy_object_device_automation"


def test_proposal_scalar_reason_is_capability_based():
    cap = mqtt_output_control_capability(
        topic_family=FAMILY_ZENSDK_HA_SCALAR, observed_capabilities={"output_control"}
    )
    enabled, reason = proposal_output_control(cap)
    assert enabled is False
    assert reason == "scalar_write_not_verified"
