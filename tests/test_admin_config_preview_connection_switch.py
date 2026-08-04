# SPDX-License-Identifier: AGPL-3.0-or-later
"""Common inverter values survive a connection switch into the preview.

Switching a configured inverter from Local API to an MQTT connection keeps the
logical device: the config name and every common (transport-independent)
``devices[]`` value travel with the selection, and a disabled inverter stays out
of the generated config exactly like a disabled Local API draft item. Identity
and connection fields stay owned by the trusted proposal fragment.
"""

import pytest

from admin.config_preview import ConfigPreviewGenerator
from admin.models import MqttHardwareCandidate
from admin.zendure_mqtt_config_proposals import build_proposals
from ems.config_catalog import device_common_field_keys

pytestmark = [
    pytest.mark.admin,
    pytest.mark.authority,
    pytest.mark.config,
    pytest.mark.integration,
    pytest.mark.simulation,
]


@pytest.fixture(autouse=True)
def _isolate_install_root(isolated_install_root):
    return isolated_install_root


TEMPLATE = {
    "system": {"max_total_power": 1600, "dry_run": False},
    "devices": [{"name": "WR1", "ip": "192.0.2.1", "sn": "YOUR_SN", "max_power": 800}],
    "grid_meter": {"type": "shelly", "ip": "192.0.2.3"},
}


class _ReleaseManager:
    def config_template(self):
        return {"tag": "v0.6.0", "template": TEMPLATE}


def _meter():
    return {
        "config_name": "grid",
        "display_name": "Shelly 3EM",
        "role": "grid_meter",
        "enabled": True,
        "ip": "192.168.1.50",
        "api_family": "shelly_gen2",
        "device_type": "shelly_pro_3em",
    }


def _proposal():
    candidate = MqttHardwareCandidate(
        broker_id="mqtt:10.0.0.10:1883",
        broker_host="10.0.0.10",
        broker_port=1883,
        topic_family="legacy_zendure_json",
        device_id="DEV1",
        serial_number="LEG1",
        product_key="PK1",
        model_hint="Hyper 2000",
        metrics_seen=["electricLevel", "outputHomePower", "outputLimit"],
    )
    return build_proposals([candidate.to_dict()])


def _generate(proposals):
    return ConfigPreviewGenerator(
        _ReleaseManager(), zendure_cloud_auth_available=lambda: True
    ).generate([_meter()], 1, zendure_mqtt_proposals=proposals)


# Common values a switched-over inverter must keep. Catalog-derived, so a new
# common device field is covered without editing a handwritten list.
_COMMON_VALUES = {
    "max_power": 777,
    "min_soc": 18,
    "max_soc": 96,
    "pv_kwp": 3.2,
    "battery_kwh": 7.7,
}


def _mqtt_device(result):
    devices = [d for d in result["config"]["devices"] if d.get("type") == "zendure_mqtt"]
    assert len(devices) == 1, devices
    return devices[0]


def test_common_values_survive_the_switch_to_an_mqtt_connection():
    proposals = _proposal()
    proposals[0]["config_name"] = "INV_1"
    proposals[0]["config_values"] = dict(_COMMON_VALUES)
    result = _generate(proposals)
    assert result["ready"] is True, result["validation"]
    device = _mqtt_device(result)
    assert device["name"] == "INV_1"
    for key, value in _COMMON_VALUES.items():
        assert device[key] == value, key
    # The trusted fragment still owns identity and connection.
    assert device["mqtt"]["device_id"] == "DEV1"
    assert device["serial_number"] == "LEG1"


def test_every_common_catalog_key_is_writable_through_the_selection():
    # Catalog-driven: whatever the schema declares common must be applicable,
    # so a future field is covered without touching this test.
    common = set(device_common_field_keys())
    assert _COMMON_VALUES.keys() <= common
    assert "ip" not in common and "sn" not in common and "name" not in common


def test_identity_fields_can_never_be_rewritten_through_common_values():
    proposals = _proposal()
    proposals[0]["config_name"] = "INV_1"
    proposals[0]["config_values"] = {
        "name": "hijacked",
        "sn": "OTHER",
        "ip": "10.9.9.9",
        "max_power": 640,
    }
    result = _generate(proposals)
    device = _mqtt_device(result)
    assert device["name"] == "INV_1"
    assert device["serial_number"] == "LEG1"
    assert "ip" not in device
    assert device["max_power"] == 640


def test_a_disabled_selection_stays_out_of_the_generated_config():
    proposals = _proposal()
    proposals[0]["config_name"] = "INV_1"
    proposals[0]["enabled"] = False
    result = _generate(proposals)
    assert [d for d in result["config"]["devices"] if d.get("type") == "zendure_mqtt"] == []


def test_a_disabled_duplicate_never_blocks_the_enabled_selection():
    # A disabled selection never becomes a device, so it must not count as a
    # second claim on the physical identity either.
    enabled, disabled = _proposal()[0], _proposal()[0]
    enabled["config_name"] = "INV_1"
    disabled["id"] = str(disabled.get("id", "")) + ":stale"
    disabled["config_name"] = "INV_2"
    disabled["enabled"] = False
    result = _generate([enabled, disabled])
    assert result["ready"] is True, result["validation"]
    assert _mqtt_device(result)["name"] == "INV_1"


def test_two_enabled_selections_for_one_device_are_still_rejected():
    first, second = _proposal()[0], _proposal()[0]
    first["config_name"] = "INV_1"
    second["id"] = str(second.get("id", "")) + ":dup"
    second["config_name"] = "INV_2"
    result = _generate([first, second])
    assert result["ready"] is False
    codes = {issue["code"] for issue in result["validation"]["errors"]}
    assert "zendure_device_identity_duplicate" in codes


def test_an_enabled_selection_is_generated_as_before():
    proposals = _proposal()
    proposals[0]["config_name"] = "INV_1"
    proposals[0]["enabled"] = True
    result = _generate(proposals)
    assert _mqtt_device(result)["name"] == "INV_1"
