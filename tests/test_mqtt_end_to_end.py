# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end mixed-transport control: API + Zendure cloud MQTT + 2 local brokers.

Deterministic and broker-free. Four inverters share one control loop:

  1. API (local HTTP)         -> fake requests session captures the POST
  2. Zendure cloud MQTT       -> fake broker captures the publish (virtual)
  3. Local MQTT broker A      -> fake broker captures the publish
  4. Local MQTT broker B      -> fake broker captures the publish

All three write gates are enabled and simulation is off, but every transport is
a fake, so the writes are virtual. The test asserts power is allocated to all
four devices and that each device's outputLimit reaches its own transport only
(broker isolation).
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ems.clients import ZendureClient
from ems.controller import EMSController
from ems.models import DeviceState
from ems.zendure_mqtt.control import (
    ZendureMqttControlClient,
    ZendureMqttControlService,
)
from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.e2e,
    pytest.mark.simulation,
    pytest.mark.power_control,
]


class FakeMqttClient:
    def __init__(self):
        self.publish_calls = []
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None

    def tls_set(self, *a, **k):
        pass

    def tls_insecure_set(self, v):
        pass

    def username_pw_set(self, u, p=None):
        pass

    def connect(self, host, port, keepalive=0):
        pass

    def loop_start(self):
        if self.on_connect is not None:
            self.on_connect(self, None, None, 0)

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, topic, qos=0):
        pass

    def publish(self, topic, payload, qos=0, retain=False):
        self.publish_calls.append((topic, payload, qos))
        return None


class RuntimeStateStub:
    def __init__(self):
        self.system = {}
        self.devices = {}

    def load_if_changed(self):
        return None

    def get_system(self, key, default=None):
        return self.system.get(key, default)

    def get_device(self, device_name, key, default=None):
        return self.devices.get(device_name, {}).get(key, default)


class ShellyStub:
    def __init__(self, power):
        self.power = power

    def get_power(self):
        return self.power


def _state():
    return DeviceState(
        soc=80, min_soc=15, max_soc=100, solar=800, output=0,
        pack_in=0, pack_out=0, temp=20, voltage=48, rssi=0, remain_minutes=0,
        solar1=0, solar2=0, solar3=0, solar4=0, output_limit=0, soc_limit=0,
        pack_state=2, fault_level=0, smart_mode=1, grid_off_mode=0,
        ac_mode=2, ac_status=1, dc_status=1, grid_state=1, input_limit_w=0,
    )


def _mqtt_device_config(name, broker_ref, device_id, product_key):
    return {
        "type": "zendure_mqtt",
        "name": name,
        "hardware_profile": "solarflow_800_pro_2",
        "mqtt": {
            "broker_ref": broker_ref,
            "topic_family": "legacy_zendure_json",
            "device_id": device_id,
            "product_key": product_key,
        },
        "capabilities": {"write_output_limit": True},
        "max_power": 800,
    }


def _config():
    return {
        "zendure_mqtt": {
            "enabled": True,
            "brokers": {
                "cloud": {
                    "enabled": True, "source": "zendure_cloud_mqtt",
                    "host": "cloud", "port": 8883, "credentials_ref": "tok",
                },
                "local_a": {
                    "enabled": True, "source": "local_mqtt", "host": "a", "port": 1883,
                },
                "local_b": {
                    "enabled": True, "source": "local_mqtt", "host": "b", "port": 1883,
                },
            },
        },
        "devices": [
            _mqtt_device_config("Cloud", "cloud", "DEVC", "PKC"),
            _mqtt_device_config("LocalA", "local_a", "DEVA", "PKA"),
            _mqtt_device_config("LocalB", "local_b", "DEVB", "PKB"),
        ],
    }


def _build_control_runtime():
    from ems.mqtt_credentials import MqttCredentials

    fakes = {}

    def factory(broker_config):
        fake = FakeMqttClient()
        fakes[broker_config.broker_ref] = fake
        return ZendureMqttControlService(
            broker_config,
            read_client_factory=lambda cfg: ZendureMqttControlClient(
                cfg, client_factory=lambda c: fake
            ),
        )

    class Resolver:
        def resolve(self, credentials_ref):
            assert credentials_ref == "tok"
            return MqttCredentials(
                username="cloud-user",
                password="cloud-password",
                client_id="cloud-client",
                app_key="cloud-app-key",
            )

    runtime = build_zendure_mqtt_control_runtime(
        _config(), service_factory=factory, credential_resolver=Resolver()
    )
    runtime.start()
    return runtime, fakes


def _run_once(controller, states):
    controller.run_startup_ac_mode_reconcile_once = Mock()
    with patch("ems.controller.fetch_all_devices", return_value=states), patch.multiple(
        "ems.controller.cfg",
        SYSTEM_ENABLED=True,
        MAX_TOTAL_POWER=3200,
        MAX_DEVICE_POWER=800,
        MIN_OUTPUT_LIMIT=0,
        LOOP_INTERVAL=5,
        DEADBAND=10,
        SOC_RECONCILE_INTERVAL=0,
        REDISTRIBUTE_CLAMPED_POWER=True,
        PV_KWP_WEIGHTING=True,
        BATTERY_KWH_WEIGHTING=True,
        DRY_RUN=False,
        SIMULATION_MODE=False,
        ARGS=SimpleNamespace(replay=False),
        ALLOW_HARDWARE_WRITES=True,
        ALLOW_MQTT_LOCAL_CONTROL_WRITES=True,
        ALLOW_MQTT_ZENDURE_CONTROL_WRITES=True,
        ALLOW_STATE_RECONCILIATION_WRITES=False,
    ):
        controller.run_once()


def test_mixed_four_inverter_control_writes_reach_every_transport():
    http_session = Mock()
    http_session.post.return_value = SimpleNamespace(status_code=200)
    http = ZendureClient(
        "API", "1.2.3.4", "SN-API", http_session,
        15, 100, 1, None, 800, 1.0, 1.0, 1.0,
    )

    runtime, fakes = _build_control_runtime()
    try:
        devices = [http] + runtime.devices
        assert len(devices) == 4

        controller = EMSController(
            devices, ShellyStub(2000), sleep_enabled=False,
            runtime_state=RuntimeStateStub(),
        )
        _run_once(controller, [_state() for _ in devices])

        # (a) Power is allocated across all four devices.
        explanation = controller.last_control_explanation
        assert explanation is not None
        assert set(explanation.devices) == {"API", "Cloud", "LocalA", "LocalB"}
        assert explanation.effective_target_total_w > 0

        # (b) The API device received an HTTP outputLimit write.
        assert http_session.post.called
        posted = http_session.post.call_args.kwargs["json"]["properties"]
        assert "outputLimit" in posted and posted["outputLimit"] > 0

        # (c) Each MQTT device wrote to its own broker only (isolation).
        def writes(ref):
            return [c for c in fakes[ref].publish_calls if c[0].endswith("properties/write")]

        cloud_writes = writes("cloud")
        a_writes = writes("local_a")
        b_writes = writes("local_b")
        assert [w[0] for w in cloud_writes] == ["iot/PKC/DEVC/properties/write"]
        assert [w[0] for w in a_writes] == ["iot/PKA/DEVA/properties/write"]
        assert [w[0] for w in b_writes] == ["iot/PKB/DEVB/properties/write"]
    finally:
        runtime.stop()


def test_mqtt_gates_block_writes_when_disabled():
    """With the MQTT gates off, MQTT devices get no publish while API still writes."""

    http_session = Mock()
    http_session.post.return_value = SimpleNamespace(status_code=200)
    http = ZendureClient(
        "API", "1.2.3.4", "SN-API", http_session,
        15, 100, 1, None, 800, 1.0, 1.0, 1.0,
    )
    runtime, fakes = _build_control_runtime()
    try:
        controller = EMSController(
            [http] + runtime.devices, ShellyStub(2000), sleep_enabled=False,
            runtime_state=RuntimeStateStub(),
        )
        controller.run_startup_ac_mode_reconcile_once = Mock()
        with patch("ems.controller.fetch_all_devices", return_value=[_state() for _ in range(4)]), patch.multiple(
            "ems.controller.cfg",
            SYSTEM_ENABLED=True, MAX_TOTAL_POWER=3200, MAX_DEVICE_POWER=800,
            MIN_OUTPUT_LIMIT=0, LOOP_INTERVAL=5, DEADBAND=10, SOC_RECONCILE_INTERVAL=0,
            REDISTRIBUTE_CLAMPED_POWER=True, PV_KWP_WEIGHTING=True, BATTERY_KWH_WEIGHTING=True,
            DRY_RUN=False, SIMULATION_MODE=False, ARGS=SimpleNamespace(replay=False),
            ALLOW_HARDWARE_WRITES=True,
            ALLOW_MQTT_LOCAL_CONTROL_WRITES=False,
            ALLOW_MQTT_ZENDURE_CONTROL_WRITES=False,
            ALLOW_STATE_RECONCILIATION_WRITES=False,
        ):
            controller.run_once()

        assert http_session.post.called  # API gate on -> writes
        for ref in ("cloud", "local_a", "local_b"):
            assert [c for c in fakes[ref].publish_calls if c[0].endswith("write")] == []
    finally:
        runtime.stop()


def test_four_device_install_splits_into_one_api_and_three_control():
    import ems.config as cfg

    devices = [{"name": "API", "ip": "1.2.3.4", "sn": "SN-API"}] + _config()["devices"]
    http = cfg.http_control_device_configs(devices)
    control = cfg.mqtt_control_device_configs(devices)
    assert [d["name"] for d in http] == ["API"]
    assert [d["name"] for d in control] == ["Cloud", "LocalA", "LocalB"]
