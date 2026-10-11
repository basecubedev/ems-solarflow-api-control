# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ambiguous Zendure MQTT proposal-selection conflicts.

Selecting two trusted proposals for the *same physical device* is ambiguous: the
merge rejects both rather than letting the last proposal win, and provisions no
broker for either. Two distinct devices sharing one config name fail the shared
name-uniqueness gate.
"""

import copy

import pytest

from admin.config_preview import ConfigPreviewGenerator
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


class _ReleaseManager:
    def config_template(self):
        return {"template": copy.deepcopy(TEMPLATE), "tag": "v-test"}


def _generator():
    return ConfigPreviewGenerator(
        _ReleaseManager(),
        install_context_provider=lambda: None,
        zendure_cloud_auth_available=lambda: True,
    )


def _local_ref(host, *, credentials_ref=None):
    return stable_local_broker_ref(normalized_broker_identity({
        "source": "local_mqtt", "host": host, "port": 1883, "tls": False,
        "tls_insecure": False, "credentials_ref": credentials_ref,
    }))


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


def _mqtt_devices(result):
    return [d for d in result["config"]["devices"] if d.get("type") == "zendure_mqtt"]


# --- F4: two proposals share one physical identity -------------------------


def test_two_proposals_for_one_device_are_both_rejected():
    first = _proposal(
        "DEV1", broker_host="10.0.0.20",
        broker_ref=_local_ref("10.0.0.20", credentials_ref="mqtt-a"),
        credentials_ref="mqtt-a",
    )
    second = _proposal(
        "DEV1", broker_host="10.0.0.30",
        broker_ref=_local_ref("10.0.0.30", credentials_ref="mqtt-a"),
        credentials_ref="mqtt-a",
    )
    result = _generator().generate([], zendure_mqtt_proposals=[first, second])
    assert result["ready"] is False
    codes = {e["code"] for e in result["validation"]["errors"]}
    assert "zendure_device_identity_duplicate" in codes
    assert _mqtt_devices(result) == []
    assert result["config"]["zendure_mqtt"]["brokers"] == {}


# --- two distinct devices sharing one config name --------------------------


def test_two_proposals_with_identical_names_are_rejected():
    # Two different physical devices whose proposals carry the same config name
    # must fail the shared name-uniqueness gate (the name is the EMS runtime
    # identity key), exactly like API inverters and the grid meter.
    ref_a = _local_ref("10.0.0.10", credentials_ref="mqtt-a")
    p1 = _proposal("DEV1", broker_host="10.0.0.10", broker_ref=ref_a, credentials_ref="mqtt-a")
    p2 = _proposal("DEV2", broker_host="10.0.0.10", broker_ref=ref_a, credentials_ref="mqtt-a")
    p1["config_name"] = "Garage inverter"
    p2["config_name"] = "Garage inverter"
    result = _generator().generate([], zendure_mqtt_proposals=[p1, p2])
    assert result["ready"] is False
    codes = {e["code"] for e in result["validation"]["errors"]}
    assert "config_name_duplicate" in codes
