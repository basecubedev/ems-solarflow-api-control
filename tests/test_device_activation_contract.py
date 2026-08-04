# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transport-independent device activation.

A device entry's ``enabled`` flag decides whether the EMS controls it, for
HTTP/local-API entries exactly as for Zendure MQTT control entries. Before this
contract, ``enabled: false`` was honored only on the MQTT path, so an operator
who disabled an API device in Admin kept an inverter under EMS control.
"""

import pytest

from ems.config import http_control_device_configs, mqtt_control_device_configs
from ems.zendure_mqtt.config_entries import has_runtime_control_device

pytestmark = [
    pytest.mark.authority,
    pytest.mark.config,
    pytest.mark.setup,
    pytest.mark.contract,
    pytest.mark.simulation,
    pytest.mark.power_control,
]


def _api_device(name, **overrides):
    device = {"name": name, "ip": "192.168.1.10", "sn": "SN-" + name}
    device.update(overrides)
    return device


def _mqtt_control_device(name, *, enabled=True):
    device = {
        "name": name,
        "type": "zendure_mqtt",
        "mqtt": {
            "broker_ref": "local",
            "device_id": "dev-" + name,
            "product_key": "pk",
            "topic_family": "legacy_zendure_json",
        },
        "capabilities": {"read_power": True, "write_output_limit": True},
    }
    if enabled is not None:
        device["enabled"] = enabled
    return device


def test_api_device_without_enabled_key_is_controlled():
    devices = [_api_device("INV_1")]

    assert [d["name"] for d in http_control_device_configs(devices)] == ["INV_1"]


def test_enabled_api_device_is_controlled():
    devices = [_api_device("INV_1", enabled=True)]

    assert [d["name"] for d in http_control_device_configs(devices)] == ["INV_1"]


def test_disabled_api_device_is_not_controlled():
    devices = [_api_device("INV_1", enabled=False)]

    assert http_control_device_configs(devices) == []


@pytest.mark.parametrize("value", ["false", "true", 0, 1, ""])
def test_non_boolean_enabled_never_controls_an_api_device(value):
    devices = [_api_device("INV_1", enabled=value)]

    assert http_control_device_configs(devices) == []


def test_null_enabled_reads_like_a_missing_flag():
    devices = [_api_device("INV_1", enabled=None)]

    assert [d["name"] for d in http_control_device_configs(devices)] == ["INV_1"]


def test_disabled_api_device_leaves_the_mqtt_control_device_untouched():
    devices = [_api_device("INV_1", enabled=False), _mqtt_control_device("INV_2")]

    assert http_control_device_configs(devices) == []
    assert [d["name"] for d in mqtt_control_device_configs(devices)] == ["INV_2"]


def test_disabled_api_device_alone_is_not_a_bootable_control_config():
    config = {"devices": [_api_device("INV_1", enabled=False)]}

    assert has_runtime_control_device(config) is False


def test_enabled_api_device_is_a_bootable_control_config():
    config = {"devices": [_api_device("INV_1")]}

    assert has_runtime_control_device(config) is True


def test_disabled_api_device_beside_an_enabled_mqtt_device_stays_bootable():
    config = {
        "devices": [_api_device("INV_1", enabled=False), _mqtt_control_device("INV_2")],
    }

    assert has_runtime_control_device(config) is True
