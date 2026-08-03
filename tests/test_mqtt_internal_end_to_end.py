# SPDX-License-Identifier: AGPL-3.0-or-later
"""True internal end-to-end MQTT flows: payload -> DeviceState -> controller -> write.

Unlike the synthetic-state scenarios, these never patch ``fetch_all_devices``.
An MQTT report is injected into the fake broker, the production telemetry runtime
turns it into a snapshot, the production device client's ``fetch()`` converts it
to a ``DeviceState``, the real controller allocates, and the resulting write is
captured on the correct transport. Only the broker/HTTP boundary is fake.

Cases A-F below map to the task's mandatory internal E2E matrix; see
``tests/MQTT_MATRIX_COVERAGE.md``.
"""

import pytest

from tests.helpers import payloads
from tests.helpers.controller import run_installation_cycle
from tests.helpers.fake_mqtt import FakeMqttNetwork
from tests.helpers.mqtt_scenarios import (
    PAYLOAD_HTTP_ZENSDK,
    PAYLOAD_LEGACY_JSON,
    PAYLOAD_LEGACY_JSON_ALT,
    PAYLOAD_ZENSDK_HA_SCALAR,
    ROLE_INVERTER_CONTROL,
    ROLE_INVERTER_TELEMETRY_ONLY,
    TRANSPORT_API_HTTP,
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

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.e2e,
    pytest.mark.simulation,
    pytest.mark.power_control,
]

_LOAD_W = 2000.0
LOCAL_A = BrokerSpec(ref="local_a", source="local_mqtt", host="10.0.0.10")
LOCAL_B = BrokerSpec(ref="local_b", source="local_mqtt", host="10.0.0.20")
HTTP_METER = GridMeterSpec(
    meter_type="shelly", transport=TRANSPORT_GRID_METER_HTTP, power_w=_LOAD_W
)


def _api(name="API", serial=None):
    return DeviceSpec(name, ROLE_INVERTER_CONTROL, TRANSPORT_API_HTTP,
                      PAYLOAD_HTTP_ZENSDK, serial=serial or name)


def _legacy(name, ref, transport, *, alt=False):
    return DeviceSpec(
        name, ROLE_INVERTER_CONTROL, transport,
        PAYLOAD_LEGACY_JSON_ALT if alt else PAYLOAD_LEGACY_JSON,
        broker_ref=ref, device_id=f"DEV{name}", product_key=f"PK{name}",
        control_enabled=True,
    )


def _writes(network, ref):
    return network.broker(ref).write_topics


# --- Case A: Legacy JSON local MQTT -----------------------------------------
def test_case_a_legacy_json_local_mqtt():
    scenario = Scenario(
        name="e2e_a_legacy_local",
        brokers=(LOCAL_A,),
        devices=(_legacy("LA", "local_a", TRANSPORT_LOCAL_MQTT_A),),
        grid_meter=HTTP_METER,
    )
    network = FakeMqttNetwork()
    installation = build_installation(scenario, network)
    try:
        controller = run_installation_cycle(installation)
        # The injected report became a DeviceState through the production path.
        assert "LA" in controller.last_control_explanation.devices
        # The control write reached broker A on the canonical properties/write topic.
        assert _writes(network, "local_a") == ["iot/PKLA/DEVLA/properties/write"]
    finally:
        installation.stop()


# --- Case B: Legacy JSON alt local MQTT -------------------------------------
def test_case_b_legacy_json_alt_local_mqtt():
    scenario = Scenario(
        name="e2e_b_legacy_alt",
        brokers=(LOCAL_A,),
        devices=(_legacy("LA", "local_a", TRANSPORT_LOCAL_MQTT_A, alt=True),),
        grid_meter=HTTP_METER,
    )
    network = FakeMqttNetwork()
    installation = build_installation(scenario, network)
    try:
        controller = run_installation_cycle(installation)
        assert "LA" in controller.last_control_explanation.devices
        # Commands stay on the iot/… tree even for the leading-slash report
        # family: devices publish reports on /… but accept writes on iot/… only.
        assert _writes(network, "local_a") == ["iot/PKLA/DEVLA/properties/write"]
    finally:
        installation.stop()


# --- Case C: ZenSDK scalar stays read-only ----------------------------------
def test_case_c_zensdk_scalar_is_read_only():
    scenario = Scenario(
        name="e2e_c_scalar_readonly",
        brokers=(LOCAL_A,),
        devices=(DeviceSpec("Scal", ROLE_INVERTER_TELEMETRY_ONLY,
                            TRANSPORT_LOCAL_MQTT_A, PAYLOAD_ZENSDK_HA_SCALAR,
                            broker_ref="local_a", serial="SNScal"),),
        grid_meter=HTTP_METER,
    )
    network = FakeMqttNetwork()
    installation = build_installation(scenario, network)
    try:
        controller = run_installation_cycle(installation)
        # Multiple scalar metrics were ingested into a snapshot...
        summaries = installation.telemetry_runtime.device_summaries()
        assert any(s["name"] == "Scal" for s in summaries)
        # ...but a telemetry-only device never enters control or publishes.
        assert installation.control_runtime.devices == []
        assert "Scal" not in controller.last_control_explanation.devices
        assert network.broker("local_a").publish_calls == []
    finally:
        installation.stop()


# --- Case D: D0 MQTT grid meter + API inverter ------------------------------
def test_case_d_d0_grid_meter_plus_api_inverter():
    d0 = GridMeterSpec(
        meter_type="zendure_smartmeter_d0", transport=TRANSPORT_GRID_METER_MQTT,
        broker_ref="local_a", serial="D0X", topic="Zendure/sensor/D0X/totalPower",
        power_w=_LOAD_W,
    )
    scenario = Scenario(
        name="e2e_d_d0_plus_api",
        brokers=(LOCAL_A,),
        devices=(_api(),),
        grid_meter=d0,
    )
    network = FakeMqttNetwork()
    installation = build_installation(scenario, network)
    try:
        # The controller reads real D0 power injected over MQTT.
        assert installation.grid_meter.get_power() == _LOAD_W
        controller = run_installation_cycle(installation)
        assert "API" in controller.last_control_explanation.devices
        # The API inverter received the write; the D0 broker never published.
        assert installation.api_sessions["API"].post.called
        assert network.broker("local_a").publish_calls == []
    finally:
        installation.stop()


# --- Case E: Mixed API + local MQTT -----------------------------------------
def test_case_e_mixed_api_and_local_mqtt():
    scenario = Scenario(
        name="e2e_e_mixed",
        brokers=(LOCAL_A,),
        devices=(_api(), _legacy("LA", "local_a", TRANSPORT_LOCAL_MQTT_A)),
        grid_meter=HTTP_METER,
    )
    network = FakeMqttNetwork()
    installation = build_installation(scenario, network)
    try:
        controller = run_installation_cycle(installation)
        # Both states entered the same real controller cycle.
        assert set(controller.last_control_explanation.devices) == {"API", "LA"}
        # HTTP write only to API; MQTT write only to broker A.
        assert installation.api_sessions["API"].post.called
        assert _writes(network, "local_a") == ["iot/PKLA/DEVLA/properties/write"]
    finally:
        installation.stop()


def test_case_e_allocation_is_deterministic():
    def run():
        network = FakeMqttNetwork()
        scenario = Scenario(
            name="e2e_e_determinism",
            brokers=(LOCAL_A,),
            devices=(_api(), _legacy("LA", "local_a", TRANSPORT_LOCAL_MQTT_A)),
            grid_meter=HTTP_METER,
        )
        installation = build_installation(scenario, network)
        try:
            controller = run_installation_cycle(installation)
            return {
                name: dev.effective_target_w
                for name, dev in controller.last_control_explanation.devices.items()
            }
        finally:
            installation.stop()

    assert run() == run()


# --- Case F: Two local brokers, isolated writes -----------------------------
def test_case_f_two_local_brokers_isolated():
    scenario = Scenario(
        name="e2e_f_two_brokers",
        brokers=(LOCAL_A, LOCAL_B),
        devices=(
            _legacy("A", "local_a", TRANSPORT_LOCAL_MQTT_A),
            _legacy("B", "local_b", TRANSPORT_LOCAL_MQTT_B),
        ),
        grid_meter=HTTP_METER,
    )
    network = FakeMqttNetwork()
    installation = build_installation(scenario, network)
    try:
        controller = run_installation_cycle(installation)
        assert set(controller.last_control_explanation.devices) == {"A", "B"}
        # Each device's payload became a DeviceState and its write stayed isolated.
        assert _writes(network, "local_a") == ["iot/PKA/DEVA/properties/write"]
        assert _writes(network, "local_b") == ["iot/PKB/DEVB/properties/write"]
        assert "DEVB" not in " ".join(_writes(network, "local_a"))
        assert "DEVA" not in " ".join(_writes(network, "local_b"))
    finally:
        installation.stop()


def test_unmapped_report_topic_never_creates_a_device():
    # A production-path sanity check: an unrelated topic on the broker does not
    # fabricate a controllable device.
    scenario = Scenario(
        name="e2e_unmapped_topic",
        brokers=(LOCAL_A,),
        devices=(_legacy("LA", "local_a", TRANSPORT_LOCAL_MQTT_A),),
        grid_meter=HTTP_METER,
    )
    network = FakeMqttNetwork()
    installation = build_installation(scenario, network)
    try:
        network.broker("local_a").inject(payloads.UNKNOWN_TOPIC, b"{}")
        controller = run_installation_cycle(installation)
        assert set(controller.last_control_explanation.devices) == {"LA"}
    finally:
        installation.stop()
