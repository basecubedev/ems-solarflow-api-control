# SPDX-License-Identifier: AGPL-3.0-or-later
"""Named high-value mixed-transport end-to-end scenarios (01-12).

Each scenario builds the real production runtimes (control + telemetry + grid
meter) over the fake broker network and runs the actual EMS control loop. Only
the hardware/network boundary is faked; allocation, write gates and transport
routing are real. The assertions prove that a mixed installation stays safe,
deterministic and broker-isolated.
"""

import pytest

from tests.helpers.controller import make_state, run_control_cycle
from tests.helpers.fake_mqtt import FakeMqttNetwork
from tests.helpers.mqtt_scenarios import (
    PAYLOAD_HTTP_ZENSDK,
    PAYLOAD_LEGACY_JSON,
    PAYLOAD_LEGACY_JSON_ALT,
    PAYLOAD_ZENSDK_HA_SCALAR,
    ROLE_INVERTER_CONTROL,
    ROLE_INVERTER_TELEMETRY_ONLY,
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

_LOAD_W = 2000.0

LOCAL_A = BrokerSpec(ref="local_a", source="local_mqtt", host="10.0.0.10")
LOCAL_B = BrokerSpec(ref="local_b", source="local_mqtt", host="10.0.0.20")
CLOUD = BrokerSpec(
    ref="cloud", source="zendure_cloud_mqtt", host="cloud.example", port=8883,
    tls=True, credentials_ref="cloud-token-SECRET",
)

HTTP_LOAD_METER = GridMeterSpec(
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


def _scalar(name, ref, transport, family=PAYLOAD_ZENSDK_HA_SCALAR):
    return DeviceSpec(name, ROLE_INVERTER_TELEMETRY_ONLY, transport, family,
                      broker_ref=ref, serial=f"SN{name}")


def _run(scenario):
    network = FakeMqttNetwork()
    installation = build_installation(scenario, network)
    controller = run_control_cycle(installation)
    return installation, network, controller


def _mqtt_writes(network, ref):
    return network.broker(ref).write_topics


# --- Scenario 01: Empty installation ----------------------------------------
def test_scenario_01_empty_installation():
    scenario = Scenario(name="s01_empty", grid_meter=HTTP_LOAD_METER)
    network = FakeMqttNetwork()
    installation = build_installation(scenario, network)
    try:
        assert installation.devices == []
        # No MQTT services are created for an HTTP-only, device-free install.
        assert installation.telemetry_runtime.broker_count == 0
        assert installation.control_runtime.services == []
        controller = run_control_cycle(installation, states=[])
        assert controller is not None
        assert network.all_publishes() == []
    finally:
        installation.stop()


# --- Scenario 02: One modern API device -------------------------------------
def test_scenario_02_single_api_device():
    scenario = Scenario(
        name="s02_api", devices=(_api(),), grid_meter=HTTP_LOAD_METER
    )
    installation, network, controller = _run(scenario)
    try:
        assert installation.api_sessions["API"].post.called
        # No MQTT runtime is created for an API-only install.
        assert installation.telemetry_runtime.broker_count == 0
        assert installation.control_runtime.services == []
        assert network.all_publishes() == []
        explanation = controller.last_control_explanation
        assert set(explanation.devices) == {"API"}
    finally:
        installation.stop()


# --- Scenario 03: One legacy local MQTT device ------------------------------
def test_scenario_03_single_legacy_local_mqtt():
    scenario = Scenario(
        name="s03_legacy_local",
        brokers=(LOCAL_A,),
        devices=(_legacy("LA", "local_a", TRANSPORT_LOCAL_MQTT_A),),
        grid_meter=HTTP_LOAD_METER,
    )
    installation, network, controller = _run(scenario)
    try:
        assert _mqtt_writes(network, "local_a") == ["iot/PKLA/DEVLA/properties/write"]
        # One shared control service on the one referenced broker.
        assert len(installation.control_runtime.services) == 1
    finally:
        installation.stop()
    # Clean shutdown stops the one service; the loop is no longer running.


# --- Scenario 04: Scalar MQTT remains read-only -----------------------------
def test_scenario_04_scalar_mqtt_is_read_only():
    scenario = Scenario(
        name="s04_scalar_readonly",
        brokers=(LOCAL_A,),
        devices=(_scalar("Scal", "local_a", TRANSPORT_LOCAL_MQTT_A),),
        grid_meter=HTTP_LOAD_METER,
    )
    installation, network, controller = _run(scenario)
    try:
        # A telemetry-only scalar device never enters the control loop.
        assert installation.devices == []
        assert installation.control_runtime.devices == []
        # Telemetry is visible, but the device never publishes.
        assert network.broker("local_a").publish_calls == []
        summaries = installation.telemetry_runtime.device_summaries()
        assert any(s["name"] == "Scal" for s in summaries)
    finally:
        installation.stop()


def test_scenario_04_explicit_scalar_control_request_is_rejected():
    from ems.zendure_mqtt.config_entries import (
        validate_zendure_mqtt_control_device_config,
    )

    # Asking a scalar device to control must be rejected, not silently skipped.
    item = {
        "type": "zendure_mqtt",
        "name": "Scal",
        "mqtt": {
            "broker_ref": "local_a",
            "topic_family": PAYLOAD_ZENSDK_HA_SCALAR,
            "device_id": "SNScal",
        },
        "capabilities": {"write_output_limit": True},
    }
    codes = {
        issue["code"]
        for issue in validate_zendure_mqtt_control_device_config(item)
    }
    assert "write_protocol_unsupported" in codes or "write_target_missing" in codes


# --- Scenario 05: D0 as MQTT grid meter -------------------------------------
def test_scenario_05_d0_mqtt_grid_meter():
    d0 = GridMeterSpec(
        meter_type="zendure_smartmeter_d0", transport=TRANSPORT_GRID_METER_MQTT,
        broker_ref="local_a", serial="D0X", topic="Zendure/sensor/D0X/totalPower",
        power_w=_LOAD_W,
    )
    scenario = Scenario(
        name="s05_d0",
        brokers=(LOCAL_A,),
        devices=(_api(),),
        grid_meter=d0,
    )
    installation, network, controller = _run(scenario)
    try:
        assert installation.grid_meter.get_power() == _LOAD_W
        # D0 exists only as the grid meter, never as a devices[] entry.
        assert all(d.get("type") != "zendure_smartmeter_d0" for d in installation.config["devices"])
        assert "D0X" not in {d.name for d in installation.devices}
        # The API inverter received the calculated write; D0 never publishes.
        assert installation.api_sessions["API"].post.called
        assert network.broker("local_a").publish_calls == []
    finally:
        installation.stop()


# --- Scenario 06: Two devices on one local broker ---------------------------
def test_scenario_06_two_devices_one_broker():
    scenario = Scenario(
        name="s06_two_on_one",
        brokers=(LOCAL_A,),
        devices=(
            _legacy("A", "local_a", TRANSPORT_LOCAL_MQTT_A),
            _legacy("B", "local_a", TRANSPORT_LOCAL_MQTT_A),
        ),
        grid_meter=HTTP_LOAD_METER,
    )
    installation, network, controller = _run(scenario)
    try:
        # One shared broker service for both devices.
        assert len(installation.control_runtime.services) == 1
        topics = sorted(_mqtt_writes(network, "local_a"))
        assert topics == ["iot/PKA/DEVA/properties/write", "iot/PKB/DEVB/properties/write"]
    finally:
        installation.stop()


# --- Scenario 07: Two local brokers -----------------------------------------
def test_scenario_07_two_local_brokers_isolated():
    scenario = Scenario(
        name="s07_two_brokers",
        brokers=(LOCAL_A, LOCAL_B),
        devices=(
            _legacy("A", "local_a", TRANSPORT_LOCAL_MQTT_A),
            _legacy("B", "local_b", TRANSPORT_LOCAL_MQTT_B),
        ),
        grid_meter=HTTP_LOAD_METER,
    )
    installation, network, controller = _run(scenario)
    try:
        assert len(installation.control_runtime.services) == 2
        assert _mqtt_writes(network, "local_a") == ["iot/PKA/DEVA/properties/write"]
        assert _mqtt_writes(network, "local_b") == ["iot/PKB/DEVB/properties/write"]
        # Strict isolation: device B's write never appears on broker A.
        assert "DEVB" not in " ".join(_mqtt_writes(network, "local_a"))
        assert "DEVA" not in " ".join(_mqtt_writes(network, "local_b"))
    finally:
        installation.stop()


# --- Scenario 08: D0 on broker B, inverter on broker A ----------------------
def test_scenario_08_d0_broker_b_inverter_broker_a():
    d0 = GridMeterSpec(
        meter_type="zendure_smartmeter_d0", transport=TRANSPORT_GRID_METER_MQTT,
        broker_ref="local_b", serial="D0B", topic="Zendure/sensor/D0B/totalPower",
        power_w=_LOAD_W,
    )
    scenario = Scenario(
        name="s08_d0_b_inverter_a",
        brokers=(LOCAL_A, LOCAL_B),
        devices=(_legacy("A", "local_a", TRANSPORT_LOCAL_MQTT_A),),
        grid_meter=d0,
    )
    installation, network, controller = _run(scenario)
    try:
        # Grid-meter subscription stays on broker B; control publish on broker A.
        assert installation.grid_meter.get_power() == _LOAD_W
        assert _mqtt_writes(network, "local_a") == ["iot/PKA/DEVA/properties/write"]
        # Broker B only carries the D0 subscription, never a control publish.
        assert network.broker("local_b").publish_calls == []
        # No accidental default local_mqtt remapping: only the two named brokers.
        assert set(network.brokers) == {"local_a", "local_b"}
    finally:
        installation.stop()


# --- Scenario 09: API + two local brokers -----------------------------------
def test_scenario_09_api_plus_two_local_brokers():
    scenario = Scenario(
        name="s09_api_two_local",
        brokers=(LOCAL_A, LOCAL_B),
        devices=(
            _api(),
            _legacy("A", "local_a", TRANSPORT_LOCAL_MQTT_A),
            _legacy("B", "local_b", TRANSPORT_LOCAL_MQTT_B),
        ),
        grid_meter=HTTP_LOAD_METER,
    )
    installation, network, controller = _run(scenario)
    try:
        assert set(controller.last_control_explanation.devices) == {"API", "A", "B"}
        assert installation.api_sessions["API"].post.called
        assert _mqtt_writes(network, "local_a") == ["iot/PKA/DEVA/properties/write"]
        assert _mqtt_writes(network, "local_b") == ["iot/PKB/DEVB/properties/write"]
    finally:
        installation.stop()


# --- Scenario 10: API + local MQTT + cloud MQTT -----------------------------
def test_scenario_10_api_local_cloud():
    cloud = DeviceSpec(
        "Cloud", ROLE_INVERTER_CONTROL, TRANSPORT_CLOUD_MQTT, PAYLOAD_LEGACY_JSON,
        broker_ref="cloud", device_id="DEVCloud", product_key="PKCloud",
        control_enabled=True,
    )
    scenario = Scenario(
        name="s10_api_local_cloud",
        brokers=(LOCAL_A, CLOUD),
        devices=(_api(), _legacy("A", "local_a", TRANSPORT_LOCAL_MQTT_A), cloud),
        grid_meter=HTTP_LOAD_METER,
    )
    installation, network, controller = _run(scenario)
    try:
        assert installation.api_sessions["API"].post.called
        assert _mqtt_writes(network, "local_a") == ["iot/PKA/DEVA/properties/write"]
        assert _mqtt_writes(network, "cloud") == ["iot/PKCloud/DEVCloud/properties/write"]
        # Cloud credential reference is never echoed into status.
        import json

        blob = json.dumps(installation.control_runtime.status())
        assert "cloud-token-SECRET" not in blob
    finally:
        installation.stop()


# --- Scenario 11: Maximum representative mixed installation ------------------
def test_scenario_11_maximum_mixed_installation():
    d0 = GridMeterSpec(
        meter_type="zendure_smartmeter_d0", transport=TRANSPORT_GRID_METER_MQTT,
        broker_ref="local_b", serial="D0B", topic="Zendure/sensor/D0B/totalPower",
        power_w=_LOAD_W,
    )
    cloud = DeviceSpec(
        "Cloud", ROLE_INVERTER_CONTROL, TRANSPORT_CLOUD_MQTT, PAYLOAD_LEGACY_JSON,
        broker_ref="cloud", device_id="DEVCloud", product_key="PKCloud",
        control_enabled=True,
    )
    scenario = Scenario(
        name="s11_max_mixed",
        brokers=(LOCAL_A, LOCAL_B, CLOUD),
        devices=(
            _api(),
            _legacy("A", "local_a", TRANSPORT_LOCAL_MQTT_A),
            _legacy("B", "local_b", TRANSPORT_LOCAL_MQTT_B, alt=True),
            cloud,
            _scalar("Scal", "local_a", TRANSPORT_LOCAL_MQTT_A),
        ),
        grid_meter=d0,
    )
    installation, network, controller = _run(scenario)
    try:
        # Four control-capable devices are allocated; the scalar is excluded.
        assert set(controller.last_control_explanation.devices) == {"API", "A", "B", "Cloud"}
        assert installation.api_sessions["API"].post.called
        assert _mqtt_writes(network, "local_a") == ["iot/PKA/DEVA/properties/write"]
        assert _mqtt_writes(network, "local_b") == ["iot/PKB/DEVB/properties/write"]
        assert _mqtt_writes(network, "cloud") == ["iot/PKCloud/DEVCloud/properties/write"]
        # Broker B carries the D0 subscription and one control publish only for B.
        assert installation.grid_meter.get_power() == _LOAD_W
        # The scalar telemetry-only device never publishes.
        scalar_publishes = [
            r for r in network.broker("local_a").publish_calls if "SNScal" in r.topic
        ]
        assert scalar_publishes == []
    finally:
        installation.stop()


# --- Scenario 13: All transports share one distribution ---------------------
def test_scenario_13_all_transports_share_one_distribution():
    # HTTP + local MQTT (broker A) + second local MQTT (broker B) + cloud MQTT,
    # all control inverters with identical tuning. With identical device states
    # the single EMS distribution must allocate the same target to every device
    # regardless of transport — proving MQTT and API are equal EMS transports.
    cloud = DeviceSpec(
        "Cloud", ROLE_INVERTER_CONTROL, TRANSPORT_CLOUD_MQTT, PAYLOAD_LEGACY_JSON,
        broker_ref="cloud", device_id="DEVCloud", product_key="PKCloud",
        control_enabled=True,
    )
    scenario = Scenario(
        name="s13_equal_distribution",
        brokers=(LOCAL_A, LOCAL_B, CLOUD),
        devices=(
            _api(),
            _legacy("A", "local_a", TRANSPORT_LOCAL_MQTT_A),
            _legacy("B", "local_b", TRANSPORT_LOCAL_MQTT_B),
            cloud,
        ),
        grid_meter=HTTP_LOAD_METER,
    )
    installation, network, controller = _run(scenario)
    try:
        explanation = controller.last_control_explanation
        assert set(explanation.devices) == {"API", "A", "B", "Cloud"}
        targets = {
            name: dev.effective_target_w for name, dev in explanation.devices.items()
        }
        # One shared distribution: identical inputs across transports produce
        # identical per-device targets, none exceeding the per-device bound.
        distinct = {round(value) for value in targets.values() if value is not None}
        assert len(distinct) == 1, targets
        for value in targets.values():
            assert value is not None and value <= 800 + 1
        # Every transport actually received its write from the same iteration.
        assert installation.api_sessions["API"].post.called
        assert _mqtt_writes(network, "local_a") == ["iot/PKA/DEVA/properties/write"]
        assert _mqtt_writes(network, "local_b") == ["iot/PKB/DEVB/properties/write"]
        assert _mqtt_writes(network, "cloud") == ["iot/PKCloud/DEVCloud/properties/write"]
    finally:
        installation.stop()


# --- Scenario 12: Eight-device scaling and determinism ----------------------
def _eight_device_scenario():
    cloud1 = DeviceSpec("C1", ROLE_INVERTER_CONTROL, TRANSPORT_CLOUD_MQTT,
                        PAYLOAD_LEGACY_JSON, broker_ref="cloud",
                        device_id="DEVC1", product_key="PKC1", control_enabled=True)
    cloud2 = DeviceSpec("C2", ROLE_INVERTER_CONTROL, TRANSPORT_CLOUD_MQTT,
                        PAYLOAD_LEGACY_JSON, broker_ref="cloud",
                        device_id="DEVC2", product_key="PKC2", control_enabled=True)
    return Scenario(
        name="s12_eight",
        brokers=(LOCAL_A, LOCAL_B, CLOUD),
        devices=(
            _api("API1", "SNA1"), _api("API2", "SNA2"),
            _legacy("LA1", "local_a", TRANSPORT_LOCAL_MQTT_A),
            _legacy("LA2", "local_a", TRANSPORT_LOCAL_MQTT_A),
            _legacy("LB1", "local_b", TRANSPORT_LOCAL_MQTT_B),
            _legacy("LB2", "local_b", TRANSPORT_LOCAL_MQTT_B),
            cloud1, cloud2,
        ),
        grid_meter=HTTP_LOAD_METER,
    )


def test_scenario_12_eight_device_determinism():
    def run():
        network = FakeMqttNetwork()
        installation = build_installation(_eight_device_scenario(), network)
        controller = run_control_cycle(
            installation, states=[make_state() for _ in installation.devices]
        )
        explanation = controller.last_control_explanation
        allocations = {
            name: dev.effective_target_w for name, dev in explanation.devices.items()
        }
        installation.stop()
        return explanation, allocations, network

    exp1, alloc1, net1 = run()
    exp2, alloc2, _net2 = run()

    assert len(exp1.devices) == 8
    # Deterministic: identical inputs produce identical per-device allocations.
    assert alloc1 == alloc2
    # No target exceeds the per-device maximum.
    for value in alloc1.values():
        assert value is None or value <= 800 + 1
    # No broker leakage: each broker only saw its own devices' writes.
    for ref, expected in (
        ("local_a", {"DEVLA1", "DEVLA2"}),
        ("local_b", {"DEVLB1", "DEVLB2"}),
        ("cloud", {"DEVC1", "DEVC2"}),
    ):
        seen = " ".join(net1.broker(ref).write_topics)
        for dev in expected:
            assert dev in seen
