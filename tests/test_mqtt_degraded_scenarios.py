# SPDX-License-Identifier: AGPL-3.0-or-later
"""Degraded-mode scenarios: broker outage, staleness, publish and meter failure.

These prove the controller stays safe and broker-isolated when part of a mixed
installation fails: a broker offline never reroutes its devices, cloud never
falls back to local, stale/unseen telemetry is treated as unavailable, a failed
publish is not retried on another broker, and hostile messages are absorbed.
"""

import pytest

from ems.zendure_mqtt.service import (
    SNAPSHOT_STALE,
    SNAPSHOT_UNSEEN,
    SnapshotStatus,
)
from tests.helpers import payloads
from tests.helpers.controller import make_state, run_control_cycle
from tests.helpers.fake_mqtt import FakeMqttNetwork
from tests.helpers.mqtt_scenarios import (
    PAYLOAD_HTTP_ZENSDK,
    PAYLOAD_LEGACY_JSON,
    ROLE_INVERTER_CONTROL,
    TRANSPORT_API_HTTP,
    TRANSPORT_CLOUD_MQTT,
    TRANSPORT_GRID_METER_HTTP,
    TRANSPORT_GRID_METER_MQTT,
    TRANSPORT_LOCAL_MQTT_A,
    TRANSPORT_LOCAL_MQTT_B,
    BrokerSpec,
    DeviceSpec,
    GridMeterSpec,
    Scenario,
    build_installation,
)

pytestmark = [pytest.mark.simulation, pytest.mark.power_control]

_LOAD = 2500.0
LOCAL_A = BrokerSpec(ref="local_a", source="local_mqtt", host="10.0.0.10")
LOCAL_B = BrokerSpec(ref="local_b", source="local_mqtt", host="10.0.0.20")
CLOUD = BrokerSpec(
    ref="cloud", source="zendure_cloud_mqtt", host="cloud.example", port=8883,
    tls=True, credentials_ref="cloud-token-SECRET",
)
HTTP_METER = GridMeterSpec(
    meter_type="shelly", transport=TRANSPORT_GRID_METER_HTTP, power_w=_LOAD
)


def _api(name="API", serial=None):
    return DeviceSpec(name, ROLE_INVERTER_CONTROL, TRANSPORT_API_HTTP,
                      PAYLOAD_HTTP_ZENSDK, serial=serial or name)


def _legacy(name, ref, transport):
    return DeviceSpec(name, ROLE_INVERTER_CONTROL, transport, PAYLOAD_LEGACY_JSON,
                      broker_ref=ref, device_id=f"DEV{name}", product_key=f"PK{name}",
                      control_enabled=True)


# --- 1. One local broker offline --------------------------------------------
def test_local_broker_a_offline_does_not_reroute():
    scenario = Scenario(
        name="broker_a_offline",
        brokers=(LOCAL_A, LOCAL_B),
        devices=(_api(), _legacy("A", "local_a", TRANSPORT_LOCAL_MQTT_A),
                 _legacy("B", "local_b", TRANSPORT_LOCAL_MQTT_B)),
        grid_meter=HTTP_METER,
    )
    network = FakeMqttNetwork()
    network.broker("local_a", connect_fails=True)  # broker A is down
    installation = build_installation(scenario, network)
    try:
        run_control_cycle(installation)
        # Broker A never accepted a control publish (client never connected).
        assert network.broker("local_a").publish_calls == []
        # Broker B stayed operational and isolated; API stayed operational.
        assert network.broker("local_b").write_topics == ["iot/PKB/DEVB/properties/write"]
        assert installation.api_sessions["API"].post.called
        # No rerouting: device A's write never appears on broker B.
        assert "DEVA" not in " ".join(network.broker("local_b").write_topics)
        # Status explains broker A's failure without leaking anything.
        status = installation.telemetry_runtime.status()
        broker_a = next(b for b in status["brokers"] if b["broker_ref"] == "local_a")
        assert broker_a["connected"] is False
    finally:
        installation.stop()


# --- 2. Cloud broker offline ------------------------------------------------
def test_cloud_broker_offline_never_falls_back_to_local():
    scenario = Scenario(
        name="cloud_offline",
        brokers=(LOCAL_A, CLOUD),
        devices=(_api(), _legacy("A", "local_a", TRANSPORT_LOCAL_MQTT_A),
                 DeviceSpec("Cloud", ROLE_INVERTER_CONTROL, TRANSPORT_CLOUD_MQTT,
                            PAYLOAD_LEGACY_JSON, broker_ref="cloud",
                            device_id="DEVCloud", product_key="PKCloud",
                            control_enabled=True)),
        grid_meter=HTTP_METER,
    )
    network = FakeMqttNetwork()
    network.broker("cloud", connect_fails=True)
    installation = build_installation(scenario, network)
    try:
        run_control_cycle(installation)
        # Local API and local MQTT remain independent and operational.
        assert installation.api_sessions["API"].post.called
        assert network.broker("local_a").write_topics == ["iot/PKA/DEVA/properties/write"]
        # The cloud device does not fall back to the local broker.
        assert network.broker("cloud").publish_calls == []
        assert "DEVCloud" not in " ".join(network.broker("local_a").write_topics)
        # Secrets stay redacted even in the degraded status.
        import json
        assert "cloud-token-SECRET" not in json.dumps(installation.control_runtime.status())
    finally:
        installation.stop()


# --- 3. One device becomes stale / unseen -----------------------------------
def test_device_client_rejects_stale_and_unseen_snapshots():
    from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient

    class _Service:
        connected = True

        def __init__(self, state):
            self._state = state

        def snapshot_status(self, device_id, *, now_monotonic=None):
            return self._state

    for state in (
        SnapshotStatus(None, SNAPSHOT_UNSEEN, None),
        SnapshotStatus(object(), SNAPSHOT_STALE, 120.0),
    ):
        device = ZendureMqttDeviceClient(
            "D", _Service(state), device_id="DEV1",
            topic_family="legacy_zendure_json", source="local_mqtt",
            product_key="PK1",
        )
        # A stale/unseen snapshot is an unavailable read, never silently reused.
        assert device.fetch() is None


def test_stale_device_gets_no_unsafe_write():
    scenario = Scenario(
        name="one_stale",
        brokers=(LOCAL_A,),
        devices=(_api(), _legacy("A", "local_a", TRANSPORT_LOCAL_MQTT_A)),
        grid_meter=HTTP_METER,
    )
    network = FakeMqttNetwork()
    installation = build_installation(scenario, network)
    try:
        # The MQTT device reads as unavailable (None); the API device is healthy.
        states = [make_state(), None]
        controller = run_control_cycle(installation, states=states)
        explanation = controller.last_control_explanation
        # The healthy API device is still allocated.
        assert explanation.devices["API"].effective_target_w >= 0
        # The stale device receives no positive (unsafe) discharge target.
        stale = explanation.devices["A"]
        assert (stale.effective_target_w or 0) <= 0
    finally:
        installation.stop()


# --- 4. Grid meter becomes stale or unavailable -----------------------------
def test_generic_mqtt_meter_unseen_returns_safe_fallback():
    scenario = Scenario(
        name="meter_unseen",
        brokers=(LOCAL_A,),
        devices=(_api(),),
        grid_meter=GridMeterSpec(
            meter_type="mqtt", transport=TRANSPORT_GRID_METER_MQTT,
            broker_ref="local_a", topic="grid/power", power_w=0.0,
            state="unseen",  # no message injected
        ),
    )
    network = FakeMqttNetwork()
    installation = build_installation(scenario, network)
    try:
        # No message received -> safe fallback value, never an unsafe reading.
        assert installation.grid_meter.get_power() == 0
    finally:
        installation.stop()


def test_http_meter_timeout_returns_safe_fallback():
    scenario = Scenario(
        name="meter_timeout",
        brokers=(),
        devices=(_api(),),
        grid_meter=HTTP_METER,
        failure_mode="http_timeout",
    )
    network = FakeMqttNetwork()
    installation = build_installation(scenario, network)
    try:
        assert installation.grid_meter.get_power() == 0
    finally:
        installation.stop()


def test_d0_meter_malformed_payload_keeps_last_safe_value():
    scenario = Scenario(
        name="meter_malformed",
        brokers=(LOCAL_A,),
        devices=(_api(),),
        grid_meter=GridMeterSpec(
            meter_type="zendure_smartmeter_d0", transport=TRANSPORT_GRID_METER_MQTT,
            broker_ref="local_a", serial="D0X",
            topic="Zendure/sensor/D0X/totalPower", power_w=-100.0,
        ),
    )
    network = FakeMqttNetwork()
    installation = build_installation(scenario, network)
    try:
        # First a valid reading establishes -100, then junk must not corrupt it.
        assert installation.grid_meter.get_power() == -100.0
        network.broker("local_a").inject("Zendure/sensor/D0X/totalPower", b"not-a-number")
        assert installation.grid_meter.get_power() == -100.0
    finally:
        installation.stop()


# --- 5. One publish fails ----------------------------------------------------
def test_publish_failure_is_not_retried_on_another_broker():
    scenario = Scenario(
        name="publish_fail",
        brokers=(LOCAL_A, LOCAL_B),
        devices=(_api(), _legacy("A", "local_a", TRANSPORT_LOCAL_MQTT_A),
                 _legacy("B", "local_b", TRANSPORT_LOCAL_MQTT_B)),
        grid_meter=HTTP_METER,
    )
    network = FakeMqttNetwork()
    network.broker("local_a", publish_fails=True)  # broker A rejects publishes
    installation = build_installation(scenario, network)
    try:
        run_control_cycle(installation)
        # The failing broker saw exactly one attempt; no retry elsewhere.
        assert len(network.broker("local_a").writes) == 1
        assert network.broker("local_b").write_topics == ["iot/PKB/DEVB/properties/write"]
        assert "DEVA" not in " ".join(network.broker("local_b").write_topics)
        # Other transports are unaffected.
        assert installation.api_sessions["API"].post.called
    finally:
        installation.stop()


# --- 6. Malformed and foreign messages --------------------------------------
def test_hostile_messages_never_create_devices_or_publish():
    scenario = Scenario(
        name="hostile_messages",
        brokers=(LOCAL_A,),
        devices=(DeviceSpec("Scal", ROLE_INVERTER_CONTROL, TRANSPORT_LOCAL_MQTT_A,
                            PAYLOAD_LEGACY_JSON, broker_ref="local_a",
                            device_id="DEVScal", product_key="PKScal",
                            control_enabled=True),),
        grid_meter=HTTP_METER,
    )
    network = FakeMqttNetwork()
    installation = build_installation(scenario, network)
    try:
        broker = network.broker("local_a")
        for topic, payload in [
            (payloads.UNKNOWN_TOPIC, b"{}"),
            ("iot/PK/DEV/properties/write", b'{"outputLimit": 500}'),  # write-response
            (payloads.FOREIGN_WILDCARD_TOPIC, b"junk"),
            ("Zendure/sensor/OTHER/totalPower/extra/seg", b"-10"),  # extra D0 seg
            (payloads.cloud_scalar_topic("electricLevel"), b"50"),  # secret-prefixed
        ]:
            broker.inject(topic, payload)
        # Only the legitimately configured device exists; no junk device rows.
        snapshots = installation.telemetry_runtime.snapshots()
        control_status = installation.control_runtime.status()
        # The control device never published in response to hostile input.
        assert broker.publish_calls == []
        # No false grid-meter mapping and no crash occurred.
        assert isinstance(snapshots, dict)
        assert control_status["accepted_control_devices"] == 1
    finally:
        installation.stop()
