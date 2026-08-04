# SPDX-License-Identifier: AGPL-3.0-or-later
"""The observed telemetry family must not decide MQTT write capability.

A Zendure power write is addressed on ``iot/<productKey>/<deviceId>/…`` for every
topic family — ``ems/zendure_mqtt/write_protocols.py`` and
``ems/mqtt_control/zendure_commands.py`` build that route without reading the
telemetry family at all. The telemetry family only names how reports are parsed.

Until this contract existed, ``resolve_power_write_capability`` refused a
writable model on a scalar family with ``transport_incompatible``. That verdict
made a SolarFlow 800 Pro 2 telemetry-only on the very family its own generation
publishes by default (``solarflow_zensdk`` → ``zensdk_ha_scalar``), while the
same physical device stayed controllable when discovery happened to classify it
as ``legacy_zendure_json_alt``. Same device, same write topic, opposite verdict.

Write capability is therefore decided from the pinned model plus an implemented
write route. Route completeness (product key / route device id) stays a separate
axis owned by ``zendure_mqtt_control_addressability``.

The broker source is held constant at the Zendure cloud broker throughout this
file, which carries the write route on every telemetry family: that isolates the
family axis these contracts are about. The broker-source axis itself is covered
by ``test_zendure_mqtt_broker_source_capability.py``.
"""

import pytest

from ems.mqtt_control.power_capability import (
    BLOCK_HARDWARE_PROFILE_DEFERRED,
    BLOCK_HARDWARE_PROFILE_MISSING,
    BLOCK_HARDWARE_PROFILE_UNKNOWN,
    BROKER_SOURCE_ZENDURE_CLOUD_MQTT,
    resolve_power_write_capability,
)
from ems.mqtt_control.zendure_commands import build_power_command
from ems.mqtt_control.zendure_profiles import (
    HARDWARE_PROFILES,
    OPERATION_DISCHARGE,
    WRITE_PROFILE_LEGACY_HUB,
    WRITE_PROFILE_LEGACY_OBJECT,
    WRITE_PROFILE_ZENSDK_PROPERTIES,
)
from ems.zendure_mqtt.capability import (
    CAPABILITY_SUPPORTED,
    mqtt_output_control_capability,
)
from ems.zendure_mqtt.topics import (
    FAMILY_LEGACY_JSON,
    FAMILY_LEGACY_JSON_ALT,
    FAMILY_UNKNOWN,
    FAMILY_ZENDURE_CLOUD_SCALAR,
    FAMILY_ZENSDK_HA_SCALAR,
)
from ems.zendure_mqtt.write_protocols import (
    PROTOCOL_LEGACY_PROPERTIES_WRITE,
    build_output_limit_message,
)

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]

TELEMETRY_FAMILIES = (
    FAMILY_LEGACY_JSON,
    FAMILY_LEGACY_JSON_ALT,
    FAMILY_ZENSDK_HA_SCALAR,
    FAMILY_ZENDURE_CLOUD_SCALAR,
    FAMILY_UNKNOWN,
)

WRITABLE_MODELS = tuple(
    sorted(name for name, prof in HARDWARE_PROFILES.items() if prof.writable)
)
TELEMETRY_ONLY_MODELS = tuple(
    sorted(name for name, prof in HARDWARE_PROFILES.items() if not prof.writable)
)

PRODUCT_KEY = "TESTPK0001"
ROUTE_DEVICE_ID = "TESTROUTE01"

# Where a command is published, independent of the telemetry family.
WRITE_FAMILY_PROPERTIES_WRITE = "iot_properties_write"
WRITE_FAMILY_FUNCTION_INVOKE = "iot_function_invoke"
BLOCK_TRANSPORT_WRITE_NOT_IMPLEMENTED = "transport_write_not_implemented"


def _shared_capability(**kwargs):
    from ems.zendure_mqtt.capability import resolve_output_control_capability

    return resolve_output_control_capability(**kwargs, broker_source=BROKER_SOURCE_ZENDURE_CLOUD_MQTT)


# --- evidence: the write route does not read the telemetry family -------------


@pytest.mark.parametrize("family", TELEMETRY_FAMILIES)
def test_properties_write_route_is_identical_on_every_telemetry_family(family):
    """A ZenSDK properties/write always targets iot/<pk>/<dev>/properties/write."""

    message = build_output_limit_message(
        PROTOCOL_LEGACY_PROPERTIES_WRITE,
        topic_family=family,
        product_key=PRODUCT_KEY,
        device_id=ROUTE_DEVICE_ID,
        output_limit_w=300,
    )

    assert message is not None
    assert message.topic == f"iot/{PRODUCT_KEY}/{ROUTE_DEVICE_ID}/properties/write"


def test_function_invoke_route_never_reads_a_telemetry_family():
    """The legacy automation builder takes no topic-family argument at all."""

    command = build_power_command(
        hardware_profile="hyper_2000",
        target_w=300,
        product_key=PRODUCT_KEY,
        device_id=ROUTE_DEVICE_ID,
        message_id=1,
        timestamp=1,
    )

    assert command.topic == f"iot/{PRODUCT_KEY}/{ROUTE_DEVICE_ID}/function/invoke"


# --- the reported false classification ----------------------------------------


def test_a_zensdk_model_is_controllable_on_its_own_default_telemetry_family():
    """The reproduction: 800 Pro 2 on zensdk_ha_scalar was transport_incompatible.

    ``zensdk_ha_scalar`` is the default family of the ``solarflow_zensdk``
    generation, so the previous rule declared every current SolarFlow model
    telemetry-only on its own native transport.
    """

    cap = resolve_power_write_capability(
        topic_family=FAMILY_ZENSDK_HA_SCALAR, hardware_profile="solarflow_800_pro_2",
        broker_source=BROKER_SOURCE_ZENDURE_CLOUD_MQTT,
    )

    assert cap.supported is True
    assert cap.block_reason is None
    assert cap.write_family == WRITE_FAMILY_PROPERTIES_WRITE
    assert cap.telemetry_family == FAMILY_ZENSDK_HA_SCALAR


def test_a_zensdk_model_is_controllable_on_the_cloud_scalar_family():
    cap = resolve_power_write_capability(
        topic_family=FAMILY_ZENDURE_CLOUD_SCALAR, hardware_profile="solarflow_800_pro_2",
        broker_source=BROKER_SOURCE_ZENDURE_CLOUD_MQTT,
    )

    assert cap.supported is True
    assert cap.write_family == WRITE_FAMILY_PROPERTIES_WRITE


# --- the authoritative matrix -------------------------------------------------


@pytest.mark.parametrize("model", WRITABLE_MODELS)
@pytest.mark.parametrize("family", TELEMETRY_FAMILIES)
def test_every_writable_model_is_supported_on_every_telemetry_family(model, family):
    """No API-capable registry model is telemetry-only over MQTT."""

    cap = resolve_power_write_capability(topic_family=family, hardware_profile=model, broker_source=BROKER_SOURCE_ZENDURE_CLOUD_MQTT)

    assert cap.supported is True, (model, family, cap.block_reason)
    assert cap.model_supported is True
    assert cap.transport_supported is True
    assert cap.block_reason is None


@pytest.mark.parametrize("model", WRITABLE_MODELS)
def test_the_write_family_follows_the_write_profile(model):
    expected = {
        WRITE_PROFILE_ZENSDK_PROPERTIES: WRITE_FAMILY_PROPERTIES_WRITE,
        WRITE_PROFILE_LEGACY_HUB: WRITE_FAMILY_FUNCTION_INVOKE,
        WRITE_PROFILE_LEGACY_OBJECT: WRITE_FAMILY_FUNCTION_INVOKE,
    }[HARDWARE_PROFILES[model].power_write_profile]
    cap = resolve_power_write_capability(
        topic_family=FAMILY_LEGACY_JSON, hardware_profile=model,
        broker_source=BROKER_SOURCE_ZENDURE_CLOUD_MQTT,
    )

    assert cap.write_family == expected


@pytest.mark.parametrize("model", TELEMETRY_ONLY_MODELS)
@pytest.mark.parametrize("family", TELEMETRY_FAMILIES)
def test_a_telemetry_only_model_stays_blocked_on_every_family(model, family):
    """Fail-closed direction: widening the transport rule lifts no model."""

    cap = resolve_power_write_capability(topic_family=family, hardware_profile=model, broker_source=BROKER_SOURCE_ZENDURE_CLOUD_MQTT)

    assert cap.supported is False
    assert cap.model_supported is False
    assert cap.block_reason in {
        BLOCK_HARDWARE_PROFILE_DEFERRED,
        BLOCK_HARDWARE_PROFILE_UNKNOWN,
    }


@pytest.mark.parametrize("family", TELEMETRY_FAMILIES)
def test_an_unpinned_model_never_becomes_controllable(family):
    for profile in (None, "", "   "):
        cap = resolve_power_write_capability(
            topic_family=family, hardware_profile=profile,
            broker_source=BROKER_SOURCE_ZENDURE_CLOUD_MQTT,
        )
        assert cap.supported is False
        assert cap.block_reason == BLOCK_HARDWARE_PROFILE_MISSING


@pytest.mark.parametrize("family", TELEMETRY_FAMILIES)
def test_an_unknown_model_never_becomes_controllable(family):
    cap = resolve_power_write_capability(
        topic_family=family, hardware_profile="solarflow_9999_imaginary",
        broker_source=BROKER_SOURCE_ZENDURE_CLOUD_MQTT,
    )

    assert cap.supported is False
    assert cap.block_reason == BLOCK_HARDWARE_PROFILE_UNKNOWN


def test_transport_write_not_implemented_is_the_reason_for_a_missing_write_route():
    """The block reason names the real cause: no implemented publish route.

    ``transport_incompatible`` used to carry two meanings at once — "this
    telemetry family is scalar" and "there is no write path". Only the latter is
    a genuine transport problem, and it is reported under its own name.
    """

    from ems.mqtt_control import power_capability

    assert (
        power_capability.BLOCK_TRANSPORT_WRITE_NOT_IMPLEMENTED
        == BLOCK_TRANSPORT_WRITE_NOT_IMPLEMENTED
    )


# --- the composed capability contract used by Admin ---------------------------


def test_the_shared_helper_reports_a_ready_write_route():
    cap = _shared_capability(
        topic_family=FAMILY_ZENSDK_HA_SCALAR,
        hardware_profile="solarflow_800_pro_2",
        product_key=PRODUCT_KEY,
        device_id=ROUTE_DEVICE_ID,
    )

    assert cap.supported is True
    assert cap.model_supported is True
    assert cap.transport_supported is True
    assert cap.write_route_ready is True
    assert cap.reason is None
    assert cap.write_family == WRITE_FAMILY_PROPERTIES_WRITE
    assert cap.telemetry_family == FAMILY_ZENSDK_HA_SCALAR


def test_the_shared_helper_names_the_missing_product_key():
    cap = _shared_capability(
        topic_family=FAMILY_ZENSDK_HA_SCALAR,
        hardware_profile="solarflow_800_pro_2",
        product_key="",
        device_id=ROUTE_DEVICE_ID,
    )

    assert cap.supported is False
    assert cap.model_supported is True
    assert cap.transport_supported is True
    assert cap.write_route_ready is False
    assert cap.reason == "missing_product_key"


def test_the_shared_helper_names_the_missing_route_device_id():
    cap = _shared_capability(
        topic_family=FAMILY_ZENSDK_HA_SCALAR,
        hardware_profile="solarflow_800_pro_2",
        product_key=PRODUCT_KEY,
        device_id="",
    )

    assert cap.supported is False
    assert cap.write_route_ready is False
    assert cap.reason == "missing_device_id"


def test_the_shared_helper_fails_closed_without_a_model():
    cap = _shared_capability(
        topic_family=FAMILY_ZENSDK_HA_SCALAR,
        hardware_profile="",
        product_key=PRODUCT_KEY,
        device_id=ROUTE_DEVICE_ID,
    )

    assert cap.supported is False
    assert cap.model_supported is False
    assert cap.reason == BLOCK_HARDWARE_PROFILE_MISSING


def test_the_shared_helper_keeps_a_telemetry_only_model_blocked():
    cap = _shared_capability(
        topic_family=FAMILY_LEGACY_JSON,
        hardware_profile="ace_1500",
        product_key=PRODUCT_KEY,
        device_id=ROUTE_DEVICE_ID,
    )

    assert cap.supported is False
    assert cap.reason == BLOCK_HARDWARE_PROFILE_DEFERRED


# --- the existing capability facade stays consistent --------------------------


@pytest.mark.parametrize("family", TELEMETRY_FAMILIES)
def test_the_mqtt_capability_facade_agrees_with_the_core_verdict(family):
    cap = mqtt_output_control_capability(
        topic_family=family, hardware_profile="solarflow_800_pro_2",
        broker_source=BROKER_SOURCE_ZENDURE_CLOUD_MQTT,
    )

    assert cap.status == CAPABILITY_SUPPORTED
    assert cap.supported is True
    assert cap.power_write_profile == WRITE_PROFILE_ZENSDK_PROPERTIES
    assert cap.write_family == WRITE_FAMILY_PROPERTIES_WRITE


def test_a_scalar_family_without_a_model_reports_the_missing_model_not_the_family():
    """``scalar_write_not_verified`` blamed the transport for a missing model."""

    cap = mqtt_output_control_capability(topic_family=FAMILY_ZENSDK_HA_SCALAR, broker_source=BROKER_SOURCE_ZENDURE_CLOUD_MQTT)

    assert cap.supported is False
    assert cap.reason == "write_method_missing"


def test_supported_operations_survive_the_transport_change():
    cap = resolve_power_write_capability(
        topic_family=FAMILY_ZENSDK_HA_SCALAR,
        hardware_profile="solarflow_800_pro_2",
        operation=OPERATION_DISCHARGE,
        broker_source=BROKER_SOURCE_ZENDURE_CLOUD_MQTT,
    )

    assert cap.supported is True
    assert OPERATION_DISCHARGE in cap.supported_operations
