# SPDX-License-Identifier: AGPL-3.0-or-later
"""Broker source is an independent output-control capability axis.

Before this contract the capability decision knew the pinned model, the write
profile and the write route, but never *which broker* the command would travel
through. A device observed only through scalar telemetry on a **local** broker
therefore resolved as write-capable and built
``iot/<productKey>/<deviceId>/properties/write`` on a transport for which no
hardware evidence exists that the route is relayed at all.

The Zendure cloud broker keeps the Archive 112 fix: its scalar telemetry is a
report-shape detail and the canonical write route is confirmed there. The local
broker keeps only the write paths that are actually exercised — the JSON report
families — and the operator-verified custom escape hatch.
"""

import pytest

from ems.mqtt_control.power_capability import (
    BLOCK_BROKER_SOURCE_UNKNOWN,
    BLOCK_BROKER_SOURCE_WRITE_UNVERIFIED,
    BLOCK_HARDWARE_PROFILE_DEFERRED,
    BLOCK_HARDWARE_PROFILE_UNKNOWN,
    BROKER_SOURCE_LOCAL_MQTT,
    BROKER_SOURCE_ZENDURE_CLOUD_MQTT,
    OPERATION_DISCHARGE,
    WRITE_FAMILY_FUNCTION_INVOKE,
    WRITE_FAMILY_PROPERTIES_WRITE,
    profile_write_route_implemented,
    resolve_broker_source_write_support,
    resolve_power_write_capability,
)
from ems.mqtt_control.topic_families import (
    FAMILY_LEGACY_JSON,
    FAMILY_LEGACY_JSON_ALT,
    FAMILY_UNKNOWN,
    FAMILY_ZENDURE_CLOUD_SCALAR,
    FAMILY_ZENSDK_HA_SCALAR,
)
from ems.zendure_mqtt.capability import (
    CAPABILITY_UNSUPPORTED,
    mqtt_output_control_capability,
    proposal_output_control,
    resolve_output_control_capability,
)

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
]

ZENSDK_MODEL = "solarflow_800_pro_2"
LEGACY_MODEL = "hyper_2000"
PRODUCT_KEY = "73bkTV"
ROUTE_DEVICE_ID = "ABCD1234567890"


def _core(**kwargs):
    params = {
        "topic_family": FAMILY_ZENSDK_HA_SCALAR,
        "hardware_profile": ZENSDK_MODEL,
        "broker_source": BROKER_SOURCE_ZENDURE_CLOUD_MQTT,
        "product_key": PRODUCT_KEY,
        "device_id": ROUTE_DEVICE_ID,
    }
    params.update(kwargs)
    return resolve_output_control_capability(**params)


def test_local_scalar_control_was_over_authorized():
    """The exact reproduction: a complete route on an unverified broker source.

    Every other axis is green — pinned supported model, implemented write
    profile, product key and MQTT route id present — so only the broker source
    can refuse this, and it must.
    """

    capability = _core(broker_source=BROKER_SOURCE_LOCAL_MQTT)

    assert capability.supported is False
    assert capability.block_reason == BLOCK_BROKER_SOURCE_WRITE_UNVERIFIED
    assert capability.broker_source_supported is False
    # The other axes stay honest: nothing about the model or the route is wrong.
    assert capability.model_supported is True
    assert capability.transport_supported is True
    assert capability.write_route_ready is True


def test_cloud_scalar_keeps_output_control():
    capability = _core()

    assert capability.supported is True
    assert capability.block_reason is None
    assert capability.broker_source_supported is True
    assert capability.write_family == WRITE_FAMILY_PROPERTIES_WRITE


@pytest.mark.parametrize(
    "family",
    [
        FAMILY_ZENSDK_HA_SCALAR,
        FAMILY_ZENDURE_CLOUD_SCALAR,
        FAMILY_LEGACY_JSON,
        FAMILY_LEGACY_JSON_ALT,
    ],
)
def test_cloud_source_is_write_capable_on_every_telemetry_family(family):
    capability = _core(topic_family=family)

    assert capability.supported is True
    assert capability.broker_source_supported is True


@pytest.mark.parametrize("family", [FAMILY_LEGACY_JSON, FAMILY_LEGACY_JSON_ALT])
def test_local_json_control_path_is_retained(family):
    """The local JSON write paths are implemented and exercised; keep them."""

    capability = _core(
        topic_family=family,
        hardware_profile=LEGACY_MODEL,
        broker_source=BROKER_SOURCE_LOCAL_MQTT,
    )

    assert capability.supported is True
    assert capability.broker_source_supported is True
    assert capability.write_family == WRITE_FAMILY_FUNCTION_INVOKE


@pytest.mark.parametrize("source", [None, "", "   ", "cloud", "mqtt", "zendure_mqtt"])
def test_unknown_or_missing_broker_source_fails_closed(source):
    capability = _core(broker_source=source)

    assert capability.supported is False
    assert capability.block_reason == BLOCK_BROKER_SOURCE_UNKNOWN
    assert capability.broker_source_supported is False


@pytest.mark.parametrize("family", [FAMILY_UNKNOWN, "something_else"])
def test_local_unclassified_family_fails_closed(family):
    """Only the observed JSON report families are proven on a local broker."""

    capability = _core(topic_family=family, broker_source=BROKER_SOURCE_LOCAL_MQTT)

    assert capability.supported is False
    assert capability.block_reason == BLOCK_BROKER_SOURCE_WRITE_UNVERIFIED


def test_model_and_route_axes_still_precede_the_source_axis():
    """A source problem never masks a missing model or an incomplete route."""

    unknown_model = _core(
        hardware_profile="hyper_3000", broker_source=BROKER_SOURCE_LOCAL_MQTT
    )
    assert unknown_model.block_reason == BLOCK_HARDWARE_PROFILE_UNKNOWN

    deferred = _core(hardware_profile="ace_1500")
    assert deferred.block_reason == BLOCK_HARDWARE_PROFILE_DEFERRED

    no_product_key = _core(product_key=None)
    assert no_product_key.block_reason == "missing_product_key"

    no_device_id = _core(device_id=None)
    assert no_device_id.block_reason == "missing_device_id"


def test_broker_source_axis_resolves_independently():
    cloud = resolve_broker_source_write_support(
        BROKER_SOURCE_ZENDURE_CLOUD_MQTT, FAMILY_ZENSDK_HA_SCALAR
    )
    assert cloud.supported is True
    assert cloud.block_reason is None
    assert cloud.broker_source == BROKER_SOURCE_ZENDURE_CLOUD_MQTT

    local_scalar = resolve_broker_source_write_support(
        "  Local_MQTT  ", FAMILY_ZENSDK_HA_SCALAR
    )
    assert local_scalar.supported is False
    assert local_scalar.block_reason == BLOCK_BROKER_SOURCE_WRITE_UNVERIFIED
    assert local_scalar.broker_source == BROKER_SOURCE_LOCAL_MQTT

    unknown = resolve_broker_source_write_support(None, FAMILY_LEGACY_JSON)
    assert unknown.supported is False
    assert unknown.block_reason == BLOCK_BROKER_SOURCE_UNKNOWN
    assert unknown.broker_source is None


def test_write_route_shape_is_source_independent():
    """Route shape (canonical vs custom topic) must not follow the broker source.

    Otherwise a blocked local device would be re-classified as a custom-topic
    device and report a missing write topic instead of the real cause.
    """

    assert profile_write_route_implemented(ZENSDK_MODEL) is True
    assert profile_write_route_implemented(LEGACY_MODEL) is True
    assert profile_write_route_implemented("ace_1500") is False
    assert profile_write_route_implemented(None) is False


@pytest.mark.parametrize(
    ("source", "family", "model", "expected_supported", "expected_reason"),
    [
        (
            BROKER_SOURCE_ZENDURE_CLOUD_MQTT,
            FAMILY_ZENSDK_HA_SCALAR,
            ZENSDK_MODEL,
            True,
            None,
        ),
        (
            BROKER_SOURCE_ZENDURE_CLOUD_MQTT,
            FAMILY_LEGACY_JSON_ALT,
            ZENSDK_MODEL,
            True,
            None,
        ),
        (
            BROKER_SOURCE_LOCAL_MQTT,
            FAMILY_ZENSDK_HA_SCALAR,
            ZENSDK_MODEL,
            False,
            BLOCK_BROKER_SOURCE_WRITE_UNVERIFIED,
        ),
        (
            BROKER_SOURCE_LOCAL_MQTT,
            FAMILY_LEGACY_JSON,
            LEGACY_MODEL,
            True,
            None,
        ),
        (
            "unknown_broker",
            FAMILY_LEGACY_JSON,
            LEGACY_MODEL,
            False,
            BLOCK_BROKER_SOURCE_UNKNOWN,
        ),
        (None, FAMILY_LEGACY_JSON, LEGACY_MODEL, False, BLOCK_BROKER_SOURCE_UNKNOWN),
        (
            BROKER_SOURCE_ZENDURE_CLOUD_MQTT,
            FAMILY_ZENSDK_HA_SCALAR,
            "not_a_model",
            False,
            BLOCK_HARDWARE_PROFILE_UNKNOWN,
        ),
        (
            BROKER_SOURCE_ZENDURE_CLOUD_MQTT,
            FAMILY_ZENSDK_HA_SCALAR,
            "superbase_v4600",
            False,
            BLOCK_HARDWARE_PROFILE_DEFERRED,
        ),
    ],
)
def test_power_write_capability_matrix(
    source, family, model, expected_supported, expected_reason
):
    cap = resolve_power_write_capability(
        topic_family=family,
        hardware_profile=model,
        broker_source=source,
        operation=OPERATION_DISCHARGE,
    )

    assert cap.supported is expected_supported
    assert cap.block_reason == expected_reason
    if expected_supported:
        assert cap.broker_source_supported is True
        assert cap.write_family is not None


def test_unsupported_operation_still_reported_on_a_verified_source():
    cap = resolve_power_write_capability(
        topic_family=FAMILY_ZENSDK_HA_SCALAR,
        hardware_profile=ZENSDK_MODEL,
        broker_source=BROKER_SOURCE_ZENDURE_CLOUD_MQTT,
        operation="charge",
    )

    assert cap.supported is False
    assert cap.block_reason == "operation_unsupported"
    assert cap.broker_source_supported is True


def test_proposal_capability_follows_the_broker_source():
    local = mqtt_output_control_capability(
        topic_family=FAMILY_ZENSDK_HA_SCALAR,
        hardware_profile=ZENSDK_MODEL,
        broker_source=BROKER_SOURCE_LOCAL_MQTT,
    )
    assert local.status == CAPABILITY_UNSUPPORTED
    assert local.block_reason == BLOCK_BROKER_SOURCE_WRITE_UNVERIFIED
    assert proposal_output_control(local) == (
        False,
        BLOCK_BROKER_SOURCE_WRITE_UNVERIFIED,
    )

    cloud = mqtt_output_control_capability(
        topic_family=FAMILY_ZENSDK_HA_SCALAR,
        hardware_profile=ZENSDK_MODEL,
        broker_source=BROKER_SOURCE_ZENDURE_CLOUD_MQTT,
    )
    assert cloud.supported is True
    assert proposal_output_control(cloud)[0] is True


def test_custom_escape_hatch_stays_operator_verified():
    """An explicit operator write topic is not a source-derived route."""

    capability = resolve_output_control_capability(
        topic_family=FAMILY_ZENSDK_HA_SCALAR,
        hardware_profile=None,
        broker_source=BROKER_SOURCE_LOCAL_MQTT,
        device_id=ROUTE_DEVICE_ID,
        write_protocol="custom_properties_write",
        write_topic="iot/custom/route/properties/write",
    )

    assert capability.supported is True
    assert capability.broker_source_supported is True
