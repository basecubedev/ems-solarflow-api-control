# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backend safeguards for the unified transport selection (Guided Setup).

The frontend reconciler configures each physical device over exactly one
transport, but the backend must not rely on that: a config-preview request that
still submits the SAME serial as both a Local-API draft inverter and a selected
Zendure MQTT proposal must be rejected, not silently merged. These tests also
pin that selected proposals are included, unselected ones are omitted, Local-API
only stays supported, and discovery/selection can never forge write capability.
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

pytestmark = [
    pytest.mark.admin,
    pytest.mark.authority,
    pytest.mark.config,
    pytest.mark.mqtt,
    pytest.mark.integration,
    pytest.mark.simulation,
]

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


def _local_ref(host):
    return stable_local_broker_ref(normalized_broker_identity({
        "source": "local_mqtt", "host": host, "port": 1883, "tls": False,
        "tls_insecure": False, "credentials_ref": None,
    }))


def _base_config(brokers):
    return {
        "system": {"max_total_power": 1600},
        "devices": [],
        "grid_meter": {"type": "shelly", "ip": "10.0.0.9"},
        "zendure_mqtt": {"enabled": True, "brokers": brokers},
    }


def _write(tmp_path, config):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return str(path)


def _http_inverter(serial, *, name="inverter_1"):
    return {
        "config_name": name, "display_name": f"SolarFlow {serial}", "role": "inverter",
        "enabled": True, "ip": "192.168.1.5", "serial_number": serial,
        "device_type": "zendure_solarflow_800_pro", "api_family": "zendure_local_http",
    }


def _meter():
    return {
        "config_name": "grid_meter", "display_name": "Shelly Pro 3EM", "role": "grid_meter",
        "enabled": True, "ip": "192.168.1.9", "api_family": "shelly_gen2",
        "device_type": "shelly_pro_3em",
    }


def _proposal(serial, broker_ref, *, broker_host, write_output_limit=False):
    return {
        "id": f"zendure-mqtt:{serial}",
        "config_fragment": {
            "type": "zendure_mqtt", "enabled": True, "name": f"Zendure {serial}",
            "serial_number": serial,
            "mqtt": {"broker_ref": broker_ref, "topic_family": "zensdk_ha_scalar",
                     "base_topic": "Zendure", "device_id": serial},
            "capabilities": {"read_power": True, "read_soc": True,
                             "write_output_limit": write_output_limit},
        },
        "broker_host": broker_host, "broker_port": 1883, "connection_source": "local_mqtt",
    }


def _mqtt_devices(result):
    return [d for d in result["config"]["devices"] if d.get("type") == "zendure_mqtt"]


def _http_devices(result):
    return [d for d in result["config"]["devices"] if d.get("type") != "zendure_mqtt"]


def test_same_serial_local_api_and_mqtt_is_rejected(tmp_path):
    ref = _local_ref("10.0.0.10")
    path = _write(tmp_path, _base_config({ref: {
        "enabled": True, "source": "local_mqtt", "host": "10.0.0.10", "port": 1883, "tls": False,
    }}))
    result = _generator(path).generate(
        [_http_inverter("EOD1AAA"), _meter()], 1,
        zendure_mqtt_proposals=[_proposal("EOD1AAA", ref, broker_host="10.0.0.10")],
    )
    assert result["ready"] is False
    codes = {e["code"] for e in result["validation"]["errors"]}
    assert "zendure_device_identity_duplicate" in codes


def test_selected_mqtt_proposal_is_included(tmp_path):
    ref = _local_ref("10.0.0.10")
    path = _write(tmp_path, _base_config({ref: {
        "enabled": True, "source": "local_mqtt", "host": "10.0.0.10", "port": 1883, "tls": False,
    }}))
    # A controllable Local-API inverter satisfies the control requirement; the
    # MQTT proposal is a different serial and must appear in the generated config.
    result = _generator(path).generate(
        [_http_inverter("SNCTRL"), _meter()], 1,
        zendure_mqtt_proposals=[_proposal("EOD1BBB", ref, broker_host="10.0.0.10")],
    )
    serials = {d.get("serial_number") for d in _mqtt_devices(result)}
    assert "EOD1BBB" in serials


def test_unselected_mqtt_proposal_is_omitted(tmp_path):
    path = _write(tmp_path, _base_config({}))
    result = _generator(path).generate([_http_inverter("SNCTRL"), _meter()], 1)
    assert _mqtt_devices(result) == []
    # The silent-HTTP guard warns that cloud MQTT is connected but nothing selected.
    codes = {w["code"] for w in result["validation"]["warnings"]}
    assert "zendure_mqtt_cloud_devices_not_selected" in codes


def test_local_api_only_setup_is_ready(tmp_path):
    path = _write(tmp_path, _base_config({}))
    result = _generator(path).generate([_http_inverter("SN1"), _meter()], 1)
    assert result["ready"] is True
    assert {d.get("sn") for d in _http_devices(result)} == {"SN1"}


def test_selection_cannot_forge_write_capability(tmp_path):
    ref = _local_ref("10.0.0.10")
    path = _write(tmp_path, _base_config({ref: {
        "enabled": True, "source": "local_mqtt", "host": "10.0.0.10", "port": 1883, "tls": False,
    }}))
    # A proposal claiming write_output_limit for a topic family with no verified
    # write protocol is downgraded to telemetry-only; priority/selection never
    # authorizes a write.
    result = _generator(path).generate(
        [_http_inverter("SNCTRL"), _meter()], 1,
        zendure_mqtt_proposals=[
            _proposal("EOD1CCC", ref, broker_host="10.0.0.10", write_output_limit=True)
        ],
    )
    devices = [d for d in _mqtt_devices(result) if d.get("serial_number") == "EOD1CCC"]
    assert devices, "the MQTT device should be present"
    assert devices[0]["capabilities"]["write_output_limit"] is False
