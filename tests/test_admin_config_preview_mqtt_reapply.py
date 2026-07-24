# SPDX-License-Identifier: AGPL-3.0-or-later
"""Setup reapply / trusted MQTT device rebind contract.

Re-applying a Zendure MQTT proposal over an existing config must be an explicit
upsert, not an unconditional skip: an exact repeat is idempotent, and selecting
the *same physical device* from a different trusted broker rebinds the existing
device to the new resolved broker profile without appending a duplicate device
or leaving a newly-created broker profile unreferenced. Genuine conflicts
(HTTP-vs-MQTT serial clash, two distinct proposals sharing an identity) still
reject.
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
        config_path=config_path,
        config_exists=True,
        config_source="canonical",
        template_path="/app/config.template.json",
        template_exists=True,
        template_source="legacy",
        data_dir="/data",
        data_dir_exists=True,
        compose_path="/app/docker-compose.yml",
        compose_exists=True,
        config_layout_state="standard_only",
    )


class _ReleaseManager:
    def __init__(self, template=None):
        self._template = copy.deepcopy(template or TEMPLATE)

    def config_template(self):
        return {"template": copy.deepcopy(self._template), "tag": "v-test"}


def _generator(config_path):
    return ConfigPreviewGenerator(
        _ReleaseManager(),
        install_context_provider=lambda: _context(config_path),
        zendure_cloud_auth_available=lambda: True,
    )


def _local_ref(host, *, port=1883, credentials_ref=None, tls=False, tls_insecure=False):
    identity = normalized_broker_identity(
        {
            "source": "local_mqtt",
            "host": host,
            "port": port,
            "tls": tls,
            "tls_insecure": tls_insecure,
            "credentials_ref": credentials_ref,
        }
    )
    return stable_local_broker_ref(identity)


def _local_profile(host, *, port=1883, credentials_ref=None, tls=False, tls_insecure=False):
    profile = {
        "enabled": True,
        "source": "local_mqtt",
        "host": host,
        "port": port,
        "tls": tls,
    }
    if tls:
        profile["tls_insecure"] = tls_insecure
    if credentials_ref is not None:
        profile["credentials_ref"] = credentials_ref
    return profile


def _mqtt_device(serial, broker_ref, *, device_id=None):
    return {
        "type": "zendure_mqtt",
        "name": f"Zendure {serial}",
        "enabled": True,
        "serial_number": serial,
        "mqtt": {
            "broker_ref": broker_ref,
            "topic_family": "zensdk_ha_scalar",
            "base_topic": "Zendure",
            "device_id": device_id or serial,
        },
        "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": False},
    }


def _proposal(serial, *, broker_host, broker_ref, broker_port=1883,
              connection_source="local_mqtt", credentials_ref=None,
              tls=False, tls_insecure=False, device_id=None):
    fragment = {
        "type": "zendure_mqtt",
        "enabled": True,
        "name": f"Zendure {serial}",
        "serial_number": serial,
        "mqtt": {
            "broker_ref": broker_ref,
            "topic_family": "zensdk_ha_scalar",
            "base_topic": "Zendure",
            "device_id": device_id or serial,
        },
        "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": False},
    }
    proposal = {
        "id": f"zendure-mqtt:{serial}",
        "config_fragment": fragment,
        "broker_host": broker_host,
        "broker_port": broker_port,
        "connection_source": connection_source,
    }
    if credentials_ref is not None:
        proposal["credentials_ref"] = credentials_ref
    if tls:
        proposal["broker_tls"] = True
        proposal["broker_tls_insecure"] = tls_insecure
        proposal["broker_port"] = 8883 if broker_port == 1883 else broker_port
    return proposal


def _base_config(brokers, devices):
    config = {
        "system": {"max_total_power": 1600},
        "devices": [
            {"name": "inverter_1", "ip": "10.0.0.1", "sn": "REAL1", "max_power": 800},
            *devices,
        ],
        "grid_meter": {"type": "shelly", "ip": "10.0.0.9"},
        "zendure_mqtt": {"enabled": True, "brokers": brokers},
    }
    return config


def _write(tmp_path, config):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return str(path)


def _mqtt_devices(result):
    return [d for d in result["config"]["devices"] if d.get("type") == "zendure_mqtt"]


def _brokers(result):
    return result["config"]["zendure_mqtt"]["brokers"]


# --- Case A: exact repeated apply is idempotent --------------------------


def test_case_a_exact_reapply_idempotent(tmp_path):
    ref_a = _local_ref("10.0.0.10", credentials_ref="mqtt-a")
    base = _base_config(
        {ref_a: _local_profile("10.0.0.10", credentials_ref="mqtt-a")},
        [_mqtt_device("DEV1", ref_a)],
    )
    path = _write(tmp_path, base)
    proposal = _proposal(
        "DEV1", broker_host="10.0.0.10", broker_ref=ref_a, credentials_ref="mqtt-a"
    )
    result = _generator(path).generate([], zendure_mqtt_proposals=[proposal])
    assert result["ready"] is True, result["validation"]
    devices = _mqtt_devices(result)
    assert len(devices) == 1
    assert devices[0]["mqtt"]["broker_ref"] == ref_a
    assert set(_brokers(result)) == {ref_a}


# --- Case B: same device, changed trusted endpoint -> rebind -------------


def test_case_b_changed_endpoint_rebinds_existing_device(tmp_path):
    ref_a = _local_ref("10.0.0.10", credentials_ref="mqtt-a")
    ref_b = _local_ref("10.0.0.20", credentials_ref="mqtt-a")
    base = _base_config(
        {ref_a: _local_profile("10.0.0.10", credentials_ref="mqtt-a")},
        [_mqtt_device("DEV1", ref_a)],
    )
    path = _write(tmp_path, base)
    proposal = _proposal(
        "DEV1", broker_host="10.0.0.20", broker_ref=ref_b, credentials_ref="mqtt-a"
    )
    result = _generator(path).generate([], zendure_mqtt_proposals=[proposal])
    assert result["ready"] is True, result["validation"]
    devices = _mqtt_devices(result)
    assert len(devices) == 1, "no duplicate device may be appended"
    assert devices[0]["mqtt"]["broker_ref"] == ref_b, "device rebound to new broker"
    brokers = _brokers(result)
    # The new profile is referenced (kept); no newly-created unused profile.
    assert ref_b in brokers
    assert brokers[ref_b]["host"] == "10.0.0.20"


def test_case_b_no_newly_created_unused_profile(tmp_path):
    ref_a = _local_ref("10.0.0.10", credentials_ref="mqtt-a")
    ref_b = _local_ref("10.0.0.20", credentials_ref="mqtt-a")
    base = _base_config(
        {ref_a: _local_profile("10.0.0.10", credentials_ref="mqtt-a")},
        [_mqtt_device("DEV1", ref_a)],
    )
    path = _write(tmp_path, base)
    proposal = _proposal(
        "DEV1", broker_host="10.0.0.20", broker_ref=ref_b, credentials_ref="mqtt-a"
    )
    result = _generator(path).generate([], zendure_mqtt_proposals=[proposal])
    brokers = _brokers(result)
    referenced = {d["mqtt"]["broker_ref"] for d in _mqtt_devices(result)}
    # Every newly-created broker profile must be referenced by a device.
    newly_created = set(brokers) - {ref_a}
    assert newly_created <= referenced


# --- Case C: same endpoint, changed credentials_ref ----------------------


def test_case_c_changed_credentials_ref_rebinds_and_isolates(tmp_path):
    ref_a = _local_ref("10.0.0.10", credentials_ref="mqtt-a")
    ref_c = _local_ref("10.0.0.10", credentials_ref="mqtt-c")
    other_ref = _local_ref("10.0.0.30", credentials_ref="mqtt-a")
    base = _base_config(
        {
            ref_a: _local_profile("10.0.0.10", credentials_ref="mqtt-a"),
            other_ref: _local_profile("10.0.0.30", credentials_ref="mqtt-a"),
        },
        [_mqtt_device("DEV1", ref_a), _mqtt_device("DEV2", other_ref)],
    )
    path = _write(tmp_path, base)
    proposal = _proposal(
        "DEV1", broker_host="10.0.0.10", broker_ref=ref_c, credentials_ref="mqtt-c"
    )
    result = _generator(path).generate([], zendure_mqtt_proposals=[proposal])
    assert result["ready"] is True, result["validation"]
    devices = {d["serial_number"]: d for d in _mqtt_devices(result)}
    assert len(devices) == 2
    assert devices["DEV1"]["mqtt"]["broker_ref"] == ref_c
    # The unrelated device keeps its original credential profile.
    assert devices["DEV2"]["mqtt"]["broker_ref"] == other_ref
    assert _brokers(result)[ref_c]["credentials_ref"] == "mqtt-c"


# --- Case D: same endpoint, changed TLS identity -------------------------


def test_case_d_changed_tls_identity_rebinds_no_downgrade(tmp_path):
    ref_plain = _local_ref("10.0.0.10", credentials_ref="mqtt-a")
    ref_tls = _local_ref("10.0.0.10", port=8883, credentials_ref="mqtt-a", tls=True)
    base = _base_config(
        {ref_plain: _local_profile("10.0.0.10", credentials_ref="mqtt-a")},
        [_mqtt_device("DEV1", ref_plain)],
    )
    path = _write(tmp_path, base)
    proposal = _proposal(
        "DEV1", broker_host="10.0.0.10", broker_ref=ref_tls,
        credentials_ref="mqtt-a", tls=True,
    )
    result = _generator(path).generate([], zendure_mqtt_proposals=[proposal])
    assert result["ready"] is True, result["validation"]
    devices = _mqtt_devices(result)
    assert len(devices) == 1
    assert devices[0]["mqtt"]["broker_ref"] == ref_tls
    assert _brokers(result)[ref_tls]["tls"] is True


# --- Case E: local API and MQTT share a serial -> conflict ---------------


def test_case_e_local_api_and_mqtt_share_serial_conflicts(tmp_path):
    ref_a = _local_ref("10.0.0.10", credentials_ref="mqtt-a")
    base = _base_config(
        {ref_a: _local_profile("10.0.0.10", credentials_ref="mqtt-a")},
        [],
    )
    path = _write(tmp_path, base)
    # Proposal serial equals the existing local-API inverter serial (REAL1).
    proposal = _proposal(
        "REAL1", broker_host="10.0.0.20",
        broker_ref=_local_ref("10.0.0.20", credentials_ref="mqtt-a"),
        credentials_ref="mqtt-a",
    )
    result = _generator(path).generate([], zendure_mqtt_proposals=[proposal])
    assert result["ready"] is False
    codes = {e["code"] for e in result["validation"]["errors"]}
    assert "zendure_device_identity_duplicate" in codes


# --- Case F: two distinct proposals share an identity -> conflict --------


def test_case_f_two_proposals_same_identity_conflict(tmp_path):
    ref_a = _local_ref("10.0.0.10", credentials_ref="mqtt-a")
    base = _base_config(
        {ref_a: _local_profile("10.0.0.10", credentials_ref="mqtt-a")},
        [],
    )
    path = _write(tmp_path, base)
    first = _proposal(
        "DEV9", broker_host="10.0.0.10", broker_ref=ref_a, credentials_ref="mqtt-a"
    )
    second = _proposal(
        "DEV9", broker_host="10.0.0.20",
        broker_ref=_local_ref("10.0.0.20", credentials_ref="mqtt-a"),
        credentials_ref="mqtt-a",
    )
    result = _generator(path).generate(
        [], zendure_mqtt_proposals=[first, second]
    )
    assert result["ready"] is False
    codes = {e["code"] for e in result["validation"]["errors"]}
    assert "zendure_device_identity_duplicate" in codes


# --- Case G: D0 and inverter share a broker; rebinding one leaves other ---


def test_case_g_rebinding_inverter_does_not_touch_d0(tmp_path):
    ref_a = _local_ref("10.0.0.10", credentials_ref="mqtt-a")
    ref_b = _local_ref("10.0.0.20", credentials_ref="mqtt-a")
    base = _base_config(
        {ref_a: _local_profile("10.0.0.10", credentials_ref="mqtt-a")},
        [_mqtt_device("DEV1", ref_a)],
    )
    base["grid_meter"] = {
        "type": "zendure_smartmeter_d0",
        "mqtt": {
            "broker_ref": ref_a,
            "topic": "Zendure/sensor/D0SN/totalPower",
            "payload_format": "number",
        },
    }
    path = _write(tmp_path, base)
    proposal = _proposal(
        "DEV1", broker_host="10.0.0.20", broker_ref=ref_b, credentials_ref="mqtt-a"
    )
    result = _generator(path).generate([], zendure_mqtt_proposals=[proposal])
    assert result["ready"] is True, result["validation"]
    devices = _mqtt_devices(result)
    assert len(devices) == 1
    assert devices[0]["mqtt"]["broker_ref"] == ref_b
    # The D0 grid meter still references the original broker; it is not rebound
    # and its (still-referenced) profile is preserved.
    assert result["config"]["grid_meter"]["mqtt"]["broker_ref"] == ref_a
    assert ref_a in _brokers(result)
