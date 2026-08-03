# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin discovery -> generated config -> EMS Core runtime contract + lifecycle.

Two things must hold across the Admin/Core boundary:

* a config generated from Admin discovery proposals is accepted by the *same*
  Core resolver/runtime builder EMS uses at startup, broker_ref/topic_family and
  stable identity survive, and no secret survives into the preview; and
* the MQTT runtimes obey their lifecycle contract: each unique broker service
  starts once, a shared service is reused, stop is idempotent, and grid-meter
  clients are closed.
"""

import json

import pytest

import ems.config as cfg
from admin.config_preview import ConfigPreviewGenerator
from admin.models import MqttHardwareCandidate
from admin.zendure_mqtt_config_proposals import build_proposals
from ems.zendure_mqtt.config_entries import find_duplicate_zendure_device_identities
from ems.zendure_mqtt.control_runtime import (
    MqttControlStartupError,
    build_zendure_mqtt_control_runtime,
    build_zendure_mqtt_control_runtime_or_abort,
)
from ems.zendure_mqtt.runtime import build_zendure_mqtt_runtime
from tests.helpers.fake_mqtt import FakeMqttNetwork
from tests.helpers.mqtt_scenarios import (
    PAYLOAD_LEGACY_JSON,
    ROLE_INVERTER_CONTROL,
    TRANSPORT_LOCAL_MQTT_A,
    TRANSPORT_LOCAL_MQTT_B,
    BrokerSpec,
    DeviceSpec,
    Scenario,
    build_config,
    build_installation,
)

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.contract,
    pytest.mark.simulation,
    pytest.mark.power_control,
]


class _ReleaseManager:
    def config_template(self):
        return {
            "tag": "v0.7.0",
            "template": {
                "system": {"max_total_power": 1600, "dry_run": False},
                "devices": [{"name": "WR1", "ip": "192.0.2.1", "sn": "YOUR_SN",
                             "max_power": 800}],
                "grid_meter": {"type": "shelly", "ip": "192.0.2.3"},
                "zendure_mqtt": {"enabled": True, "brokers": {}},
            },
        }


def _inverter_item():
    return {
        "config_name": "WR1", "display_name": "SolarFlow 800", "role": "inverter",
        "enabled": True, "ip": "192.168.1.10", "serial_number": "SN1",
        "device_type": "zendure_solarflow_800_pro", "api_family": "zendure_local_http",
    }


def _d0_candidate(broker_id, host, serial):
    return MqttHardwareCandidate(
        broker_id=broker_id, broker_host=host, broker_port=1883,
        topic_family="zensdk_ha_scalar", device_id=serial, serial_number=serial,
        metrics_seen=["totalPower"], topics_seen=[f"Zendure/sensor/{serial}/totalPower"],
        source_type="local_mqtt",
    )


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


# --- Admin -> Core contract -------------------------------------------------
def test_single_local_d0_survives_into_core_resolver():
    proposals = build_proposals([_d0_candidate("b1", "10.0.0.9", "D0X").to_dict()])
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_inverter_item()], 1, zendure_mqtt_proposals=proposals
    )
    config = result["config"]
    grid = config["grid_meter"]
    assert grid["type"] == "zendure_smartmeter_d0"
    assert grid["mqtt"]["broker_ref"] == proposals[0]["broker_ref"]
    assert grid["mqtt"]["broker_ref"].startswith("local_mqtt_")
    assert grid["mqtt"]["topic"] == "Zendure/sensor/D0X/totalPower"
    # D0 is the grid meter only, never a devices[] entry.
    assert all(d.get("type") != "zendure_smartmeter_d0" for d in config["devices"])
    # The exact resolver EMS runs at startup accepts it.
    resolved = cfg.resolve_grid_meter_mqtt_settings(config)
    assert resolved["host"] == "10.0.0.9"


def test_d0_on_broker_b_keeps_distinct_broker_ref():
    inverter = MqttHardwareCandidate(
        broker_id="bA", broker_host="10.0.0.10", broker_port=1883,
        topic_family="zensdk_ha_scalar", device_id="INV1", serial_number="INV1",
        metrics_seen=["outputLimit"], topics_seen=["Zendure/sensor/INV1/electricLevel"],
        source_type="local_mqtt",
    ).to_dict()
    d0 = _d0_candidate("bB", "10.0.0.20", "D0B").to_dict()
    proposals = build_proposals([inverter, d0])
    by_serial = {p["serial_number"]: p for p in proposals}
    assert by_serial["INV1"]["broker_ref"] != by_serial["D0B"]["broker_ref"]
    # Each broker keeps its own endpoint; hosts never cross.
    assert by_serial["INV1"]["broker_host"] == "10.0.0.10"
    assert by_serial["D0B"]["broker_host"] == "10.0.0.20"


def test_generated_multi_broker_config_builds_a_runtime_without_leaking_secrets():
    scenario = Scenario(
        name="multi_broker_auth_tls",
        brokers=(
            BrokerSpec(ref="local_a", source="local_mqtt", host="10.0.0.10",
                       username="u", password="pw-SECRET"),
            BrokerSpec(ref="tls_b", source="local_mqtt", host="10.0.0.20",
                       port=8883, tls=True),
        ),
        devices=(
            DeviceSpec("A", ROLE_INVERTER_CONTROL, TRANSPORT_LOCAL_MQTT_A,
                       PAYLOAD_LEGACY_JSON, broker_ref="local_a",
                       device_id="DEVA", product_key="PKA", control_enabled=True),
            DeviceSpec("B", ROLE_INVERTER_CONTROL, TRANSPORT_LOCAL_MQTT_B,
                       PAYLOAD_LEGACY_JSON, broker_ref="tls_b",
                       device_id="DEVB", product_key="PKB", control_enabled=True),
        ),
    )
    config = build_config(scenario)
    # The same builders EMS uses at startup accept the generated config.
    control = build_zendure_mqtt_control_runtime(config)
    telemetry = build_zendure_mqtt_runtime(config)
    try:
        status = json.dumps({"c": control.status(), "t": telemetry.status()})
        assert "pw-SECRET" not in status
        assert control.rejected == []
        # broker_ref survives to both control devices.
        refs = {d.broker_ref for d in control.devices}
        assert refs == {"local_a", "tls_b"}
    finally:
        control.stop()
        telemetry.stop()


def test_same_device_two_transports_is_rejected_by_core():
    # A local MQTT entry and a cloud MQTT entry for the same serial must collide.
    devices = [
        {"type": "zendure_mqtt", "name": "Local", "serial_number": "DUP1",
         "mqtt": {"broker_ref": "local_a", "topic_family": "legacy_zendure_json",
                  "device_id": "DEV1", "product_key": "PK1"},
         "capabilities": {"write_output_limit": True}},
        {"type": "zendure_mqtt", "name": "Cloud", "serial_number": "DUP1",
         "mqtt": {"broker_ref": "cloud", "topic_family": "legacy_zendure_json",
                  "device_id": "DEV1", "product_key": "PK1"},
         "capabilities": {"write_output_limit": True}},
    ]
    issues = find_duplicate_zendure_device_identities(devices)
    assert len(issues) == 1
    assert "DUP1" not in issues[0]["message"]


# --- Runtime lifecycle contract ---------------------------------------------
def _control_scenario():
    return Scenario(
        name="lifecycle",
        brokers=(
            BrokerSpec(ref="local_a", source="local_mqtt", host="10.0.0.10"),
            BrokerSpec(ref="local_b", source="local_mqtt", host="10.0.0.20"),
        ),
        devices=(
            DeviceSpec("A", ROLE_INVERTER_CONTROL, TRANSPORT_LOCAL_MQTT_A,
                       PAYLOAD_LEGACY_JSON, broker_ref="local_a",
                       device_id="DEVA", product_key="PKA", control_enabled=True),
            DeviceSpec("B", ROLE_INVERTER_CONTROL, TRANSPORT_LOCAL_MQTT_B,
                       PAYLOAD_LEGACY_JSON, broker_ref="local_b",
                       device_id="DEVB", product_key="PKB", control_enabled=True),
        ),
    )


def test_each_unique_broker_service_starts_once_and_stops_once():
    network = FakeMqttNetwork()
    installation = build_installation(_control_scenario(), network)
    # Each broker opened exactly one connection (one client, one loop_start).
    for ref in ("local_a", "local_b"):
        broker = network.broker(ref)
        assert len(broker.clients) == 1
        assert broker.clients[0].loop_start_count == 1
    installation.stop()
    for ref in ("local_a", "local_b"):
        assert network.broker(ref).clients[0].loop_stop_count == 1


def test_stop_is_idempotent():
    network = FakeMqttNetwork()
    installation = build_installation(_control_scenario(), network)
    installation.control_runtime.stop()
    installation.control_runtime.stop()  # second stop is a no-op
    installation.telemetry_runtime.stop()
    # loop_stop was called exactly once per client despite repeated stops.
    for ref in ("local_a", "local_b"):
        assert network.broker(ref).clients[0].loop_stop_count == 1


def test_shared_broker_service_is_reused_across_control_and_telemetry():
    # A broker carrying both a control device and a telemetry-only device opens
    # a single connection: the telemetry runtime borrows the control service.
    scenario = Scenario(
        name="shared_service",
        brokers=(BrokerSpec(ref="local_a", source="local_mqtt", host="10.0.0.10"),),
        devices=(
            DeviceSpec("Ctrl", ROLE_INVERTER_CONTROL, TRANSPORT_LOCAL_MQTT_A,
                       PAYLOAD_LEGACY_JSON, broker_ref="local_a",
                       device_id="DEVCtrl", product_key="PKCtrl", control_enabled=True),
            DeviceSpec("Tel", "inverter_telemetry_only", TRANSPORT_LOCAL_MQTT_A,
                       "zensdk_ha_scalar", broker_ref="local_a", serial="SNTel"),
        ),
    )
    network = FakeMqttNetwork()
    installation = build_installation(scenario, network)
    try:
        assert len(network.broker("local_a").clients) == 1
    finally:
        installation.stop()


def test_control_build_failure_with_enabled_device_aborts_startup():
    config = build_config(_control_scenario())

    def boom(_config):
        raise RuntimeError("service construction failed")

    with pytest.raises(MqttControlStartupError):
        build_zendure_mqtt_control_runtime_or_abort(config, service_factory=boom)


def test_grid_meter_client_is_closed_on_stop():
    from tests.helpers.mqtt_scenarios import (
        GridMeterSpec,
        TRANSPORT_GRID_METER_MQTT,
    )

    scenario = Scenario(
        name="grid_close",
        brokers=(BrokerSpec(ref="local_a", source="local_mqtt", host="10.0.0.10"),),
        devices=(),
        grid_meter=GridMeterSpec(
            meter_type="zendure_smartmeter_d0", transport=TRANSPORT_GRID_METER_MQTT,
            broker_ref="local_a", serial="D0X",
            topic="Zendure/sensor/D0X/totalPower", power_w=-50.0,
        ),
    )
    network = FakeMqttNetwork()
    installation = build_installation(scenario, network)
    grid_client = network.broker("local_a").clients[-1]
    installation.stop()
    # close() stops the loop and disconnects the grid-meter's own client.
    assert grid_client.loop_stop_count >= 1
    assert grid_client.disconnect_count >= 1
