# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ambiguous Zendure MQTT proposal-selection conflicts.

Selecting two trusted proposals for the *same physical device* is ambiguous even
when that device already exists in the base config: the merge must reject it
rather than silently rebinding the same device twice (last proposal wins). A
proposal that matches a *disabled* existing device is equally ambiguous: it must
be rejected, not appended as an enabled duplicate that later trips
duplicate-identity validation.
"""

import copy
import json

import pytest

from admin.config_preview import ConfigPreviewGenerator
from admin.install_context import AdminInstallContext
from ems.zendure_mqtt.config_entries import (
    normalized_broker_identity,
    stable_local_broker_ref,
)

pytestmark = pytest.mark.simulation

TEMPLATE = {
    "system": {"max_total_power": 1600},
    "devices": [{"name": "inverter_1", "ip": "192.0.2.1", "sn": "YOUR_SN", "max_power": 800}],
    "grid_meter": {"type": "shelly", "ip": "192.0.2.3"},
    "zendure_mqtt": {"enabled": True, "brokers": {}},
}


def _context(config_path):
    return AdminInstallContext(
        config_path=config_path, config_exists=True, config_source="canonical",
        template_path="/app/config.template.json", template_exists=True,
        template_source="legacy", data_dir="/data", data_dir_exists=True,
        compose_path="/app/docker-compose.yml", compose_exists=True,
        config_layout_state="standard_only",
    )


class _ReleaseManager:
    def config_template(self):
        return {"template": copy.deepcopy(TEMPLATE), "tag": "v-test"}


def _generator(config_path):
    return ConfigPreviewGenerator(
        _ReleaseManager(),
        install_context_provider=lambda: _context(config_path),
        zendure_cloud_auth_available=lambda: True,
    )


def _local_ref(host, *, credentials_ref=None):
    return stable_local_broker_ref(normalized_broker_identity({
        "source": "local_mqtt", "host": host, "port": 1883, "tls": False,
        "tls_insecure": False, "credentials_ref": credentials_ref,
    }))


def _local_profile(host, *, credentials_ref=None):
    p = {"enabled": True, "source": "local_mqtt", "host": host, "port": 1883, "tls": False}
    if credentials_ref:
        p["credentials_ref"] = credentials_ref
    return p


def _mqtt_device(serial, broker_ref, *, enabled=True):
    return {
        "type": "zendure_mqtt", "name": f"Zendure {serial}", "enabled": enabled,
        "serial_number": serial,
        "mqtt": {"broker_ref": broker_ref, "topic_family": "zensdk_ha_scalar",
                 "base_topic": "Zendure", "device_id": serial},
        "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": False},
    }


def _proposal(serial, *, broker_host, broker_ref, credentials_ref=None):
    fragment = {
        "type": "zendure_mqtt", "enabled": True, "name": f"Zendure {serial}",
        "serial_number": serial,
        "mqtt": {"broker_ref": broker_ref, "topic_family": "zensdk_ha_scalar",
                 "base_topic": "Zendure", "device_id": serial},
        "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": False},
    }
    p = {"id": f"zendure-mqtt:{serial}", "config_fragment": fragment,
         "broker_host": broker_host, "broker_port": 1883, "connection_source": "local_mqtt"}
    if credentials_ref:
        p["credentials_ref"] = credentials_ref
    return p


def _base_config(brokers, devices):
    return {
        "system": {"max_total_power": 1600},
        "devices": [
            {"name": "inverter_1", "ip": "10.0.0.1", "sn": "REAL1", "max_power": 800},
            *devices,
        ],
        "grid_meter": {"type": "shelly", "ip": "10.0.0.9"},
        "zendure_mqtt": {"enabled": True, "brokers": brokers},
    }


def _write(tmp_path, config):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return str(path)


def _mqtt_devices(result):
    return [d for d in result["config"]["devices"] if d.get("type") == "zendure_mqtt"]


# --- F4: two proposals share an identity already present in the base -------


def test_two_proposals_same_base_identity_conflict_no_rebind(tmp_path):
    ref_a = _local_ref("10.0.0.10", credentials_ref="mqtt-a")
    base = _base_config(
        {ref_a: _local_profile("10.0.0.10", credentials_ref="mqtt-a")},
        [_mqtt_device("DEV1", ref_a)],
    )
    path = _write(tmp_path, base)
    ref_b = _local_ref("10.0.0.20", credentials_ref="mqtt-a")
    ref_c = _local_ref("10.0.0.30", credentials_ref="mqtt-a")
    result = _generator(path).generate([], zendure_mqtt_proposals=[
        _proposal("DEV1", broker_host="10.0.0.20", broker_ref=ref_b, credentials_ref="mqtt-a"),
        _proposal("DEV1", broker_host="10.0.0.30", broker_ref=ref_c, credentials_ref="mqtt-a"),
    ])
    assert result["ready"] is False
    codes = {e["code"] for e in result["validation"]["errors"]}
    assert "zendure_device_identity_duplicate" in codes
    # The base device is not rebound and no new broker profile was provisioned.
    devices = _mqtt_devices(result)
    assert len(devices) == 1
    assert devices[0]["mqtt"]["broker_ref"] == ref_a
    assert set(result["config"]["zendure_mqtt"]["brokers"]) == {ref_a}


# --- two distinct devices sharing one config name --------------------------


def test_two_proposals_with_identical_names_are_rejected(tmp_path):
    # Two different physical devices whose proposals carry the same config name
    # must fail the shared name-uniqueness gate (the name is the EMS runtime
    # identity key), exactly like API inverters and the grid meter.
    ref_a = _local_ref("10.0.0.10", credentials_ref="mqtt-a")
    base = _base_config(
        {ref_a: _local_profile("10.0.0.10", credentials_ref="mqtt-a")}, []
    )
    path = _write(tmp_path, base)
    p1 = _proposal("DEV1", broker_host="10.0.0.10", broker_ref=ref_a, credentials_ref="mqtt-a")
    p2 = _proposal("DEV2", broker_host="10.0.0.10", broker_ref=ref_a, credentials_ref="mqtt-a")
    p1["config_name"] = "Garage inverter"
    p2["config_name"] = "Garage inverter"
    result = _generator(path).generate([], zendure_mqtt_proposals=[p1, p2])
    assert result["ready"] is False
    codes = {e["code"] for e in result["validation"]["errors"]}
    assert "config_name_duplicate" in codes


# --- F7: proposal matches a disabled existing device ----------------------


def test_proposal_matching_disabled_device_is_rejected(tmp_path):
    ref_a = _local_ref("10.0.0.10", credentials_ref="mqtt-a")
    base = _base_config(
        {ref_a: _local_profile("10.0.0.10", credentials_ref="mqtt-a")},
        [_mqtt_device("DEV1", ref_a, enabled=False)],
    )
    path = _write(tmp_path, base)
    ref_b = _local_ref("10.0.0.20", credentials_ref="mqtt-a")
    result = _generator(path).generate([], zendure_mqtt_proposals=[
        _proposal("DEV1", broker_host="10.0.0.20", broker_ref=ref_b, credentials_ref="mqtt-a"),
    ])
    assert result["ready"] is False
    codes = {e["code"] for e in result["validation"]["errors"]}
    assert "zendure_device_identity_disabled" in codes
    # No enabled duplicate is appended; the single existing entry stays disabled.
    devices = _mqtt_devices(result)
    assert len(devices) == 1
    assert devices[0].get("enabled") is False
    assert devices[0]["mqtt"]["broker_ref"] == ref_a
