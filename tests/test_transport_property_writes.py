# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transport-neutral property writes and transport-aware state-write gates.

State/mode reconciliation must route each write through the device's own
transport: HTTP devices POST to their local API, MQTT ZenSDK devices publish to
their own properties/write topic, and a device with neither capability fails
closed. The gate policy is a single resolver: the transport's write gate plus
``allow_state_reconciliation_writes`` — MQTT reconciliation must never depend
on the HTTP ``allow_hardware_writes`` gate.
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ems import config as cfg
from ems.controller import EMSController
from ems.property_writes import write_device_properties
from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient
from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
    pytest.mark.power_control,
]


class _FakeService:
    def __init__(self):
        self.published = []
        self.connected = True

    def snapshot_status(self, device_id, *, now_monotonic=None):
        from ems.zendure_mqtt.service import classify_snapshot

        return classify_snapshot(None, 60.0, now_monotonic=now_monotonic or 0.0)

    def publish_output_limit(self, topic, payload):
        self.published.append((topic, payload))
        return True


def _mqtt_device(hardware_profile="solarflow_800_pro_2", **kwargs):
    return ZendureMqttDeviceClient(
        "INV-MQTT",
        _FakeService(),
        device_id="DEV",
        topic_family=FAMILY_LEGACY_JSON,
        source="zendure_cloud_mqtt",
        product_key="PK",
        hardware_profile=hardware_profile,
        max_power=800,
        **kwargs,
    )


# --- gate resolver -----------------------------------------------------------


def _with_gates(**overrides):
    values = {
        "DRY_RUN": False,
        "SIMULATION_MODE": False,
        "ALLOW_HARDWARE_WRITES": True,
        "ALLOW_MQTT_LOCAL_CONTROL_WRITES": True,
        "ALLOW_MQTT_ZENDURE_CONTROL_WRITES": True,
        "ALLOW_STATE_RECONCILIATION_WRITES": True,
    }
    values.update(overrides)
    return patch.multiple(cfg, **values)


def test_state_write_gate_uses_the_transport_gate_not_the_http_gate():
    with _with_gates(ALLOW_HARDWARE_WRITES=False):
        assert cfg.state_reconciliation_writes_allowed("mqtt_zendure") is True
        assert cfg.state_reconciliation_writes_allowed("mqtt_local") is True
        assert cfg.state_reconciliation_writes_allowed("api") is False
        decision = cfg.resolve_state_write_gate("api")
        assert decision.blocked_by == ("allow_hardware_writes",)


def test_state_write_gate_requires_the_cloud_gate_for_cloud_devices():
    with _with_gates(ALLOW_MQTT_ZENDURE_CONTROL_WRITES=False):
        decision = cfg.resolve_state_write_gate("mqtt_zendure")
        assert decision.allowed is False
        assert decision.blocked_by == ("allow_mqtt_zendure_control_writes",)
        assert cfg.state_reconciliation_writes_allowed("api") is True


def test_state_write_gate_requires_the_global_state_gate():
    with _with_gates(ALLOW_STATE_RECONCILIATION_WRITES=False):
        for gate in ("api", "mqtt_local", "mqtt_zendure"):
            decision = cfg.resolve_state_write_gate(gate)
            assert decision.allowed is False
            assert decision.blocked_by == ("allow_state_reconciliation_writes",)


@pytest.mark.parametrize("blocker", ["DRY_RUN", "SIMULATION_MODE"])
def test_state_write_gate_blocks_on_dry_run_and_simulation(blocker):
    with _with_gates(**{blocker: True}):
        decision = cfg.resolve_state_write_gate("mqtt_zendure")
        assert decision.allowed is False
        assert blocker.lower() in decision.blocked_by


# --- transport dispatch ------------------------------------------------------


def test_dispatch_uses_http_for_session_devices():
    dev = SimpleNamespace(name="WR1", ip="10.0.0.1", sn="SN1", session=Mock())
    dev.session.post.return_value = SimpleNamespace(status_code=200, text="")
    result = write_device_properties(
        dev, {"acMode": 2}, reason="ac_mode_intent", field="acMode"
    )
    assert bool(result) is True
    dev.session.post.assert_called_once()
    assert dev.session.post.call_args.kwargs["json"]["properties"] == {"acMode": 2}


def test_dispatch_uses_mqtt_capability_for_mqtt_devices():
    dev = _mqtt_device()
    result = write_device_properties(dev, {"acMode": 2}, reason="ac_mode_intent")
    assert bool(result) is True
    topic, payload = dev._service.published[-1]
    assert topic == "iot/PK/DEV/properties/write"
    assert json.loads(payload)["properties"] == {"acMode": 2}


def test_dispatch_fails_closed_without_any_transport():
    bare = SimpleNamespace(name="ghost", session=None)
    result = write_device_properties(bare, {"acMode": 2}, reason="ac_mode_intent")
    assert bool(result) is False
    assert result.reason == "transport_property_writes_unsupported"


# --- MQTT property-write validation ------------------------------------------


def test_mqtt_property_write_rejects_undeclared_property():
    dev = _mqtt_device()
    result = dev.write_properties({"minSoc": 200}, reason="soc_limits")
    assert bool(result) is False
    assert result.reason == "property_write_unsupported"
    assert dev._service.published == []


def test_mqtt_property_write_rejects_legacy_automation_profile():
    # Legacy hub/hyper models keep their function/invoke power protocol and
    # API-only state reconciliation; arbitrary properties/write is unverified.
    dev = _mqtt_device(hardware_profile="hyper_2000")
    result = dev.write_properties({"acMode": 2}, reason="ac_mode_intent")
    assert bool(result) is False
    assert result.reason == "property_write_unsupported"
    assert dev._service.published == []


@pytest.mark.parametrize(
    "properties",
    [
        {"acMode": 3},
        {"acMode": True},
        {"smartMode": 2},
        {"outputLimit": -1},
        {"inputLimit": "200"},
        {},
    ],
)
def test_mqtt_property_write_rejects_unsafe_values(properties):
    dev = _mqtt_device()
    result = dev.write_properties(properties, reason="test")
    assert bool(result) is False
    assert dev._service.published == []


def test_mqtt_property_write_enforces_max_power_bound():
    dev = _mqtt_device()
    result = dev.write_properties({"outputLimit": 5000}, reason="restore")
    assert bool(result) is False
    assert result.reason == "target_above_maximum"
    assert dev._service.published == []


def test_mqtt_property_write_does_not_touch_the_power_command_slot():
    dev = _mqtt_device()
    dev.write_output_limit(300)
    active = dev._active_command
    dev.write_properties({"acMode": 2}, reason="restore")
    assert dev._active_command is active


# --- controller reconciliation skips MQTT devices explicitly -----------------


class _RuntimeStateStub:
    def load_if_changed(self):
        return None

    def get_system(self, key, default=None):
        return default

    def get_device(self, device_name, key, default=None):
        return default


class _ShellyStub:
    def get_power(self):
        return 0


def _controller_with(dev):
    controller = EMSController(
        devices=[dev],
        shelly=_ShellyStub(),
        sleep_enabled=False,
        runtime_state=_RuntimeStateStub(),
    )
    return controller


def test_reconciliation_paths_skip_mqtt_devices_with_explicit_reason(caplog):
    from ems.models import DeviceState
    from ems.runtime_intents import ac_output_intent

    dev = _mqtt_device()
    controller = _controller_with(dev)
    state = DeviceState(
        soc=60, min_soc=15, max_soc=100, solar=0, output=0, pack_in=0, pack_out=0,
        temp=0, voltage=0, rssi=0, remain_minutes=0, solar1=0, solar2=0, solar3=0,
        solar4=0, output_limit=0, soc_limit=0, pack_state=0, fault_level=0,
        smart_mode=0, grid_off_mode=0, ac_mode=1, ac_status=0, dc_status=0,
        grid_state=0, input_limit_w=0, pack_num=0, soc_status=0,
        battery_calibration_time=None,
    )
    with caplog.at_level(logging.DEBUG):
        assert controller.reconcile_ac_mode_intent(
            dev, state, ac_output_intent(dev.name)
        ) is True
        controller.apply_device_modes(dev, state)
        controller.apply_winter_ac_charge_limit(dev)
        controller.apply_runtime_device_state(dev, state)
        assert controller.apply_soc_limits(dev, state) is True
    skips = [r for r in caplog.messages if "state_reconciliation_skipped" in r]
    assert len(skips) == 5
    assert all("transport_state_reconciliation_unsupported" in r for r in skips)
    assert dev._service.published == []
