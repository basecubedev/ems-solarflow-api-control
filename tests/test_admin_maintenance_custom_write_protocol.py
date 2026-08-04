# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance must preserve explicit custom MQTT write configurations.

A manually configured device may carry a valid custom write protocol
(``custom_properties_write`` plus an explicit ``mqtt.write_topic``) on a topic
family that has no built-in write method. Maintenance derives capability from
the actual configured values — never from the topic family alone — so a
harmless roundtrip keeps the device writable, an explicit disable keeps the
protocol metadata for later re-enabling, and an invalid custom configuration is
rejected instead of silently downgraded. Discovery-derived devices keep using
the shared known-capability rule: an unsupported scalar topic can not become
writable without a valid explicit write configuration.
"""

import json

import pytest

from admin.maintenance_config import (
    load_maintenance_config,
    prepare_maintenance_config_apply,
    preview_maintenance_config,
)
from admin.zendure_mqtt_config_draft import zendure_mqtt_device_draft

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.mqtt,
    pytest.mark.integration,
    pytest.mark.simulation,
]


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


WRITE_TOPIC = "Zendure/number/CUST1/outputLimit"


def _custom_device(write_output_limit=True, write_topic=WRITE_TOPIC):
    mqtt = {
        "broker_ref": "home_broker",
        "topic_family": "zensdk_ha_scalar",
        "base_topic": "Zendure",
        "device_id": "CUST1",
        "write_protocol": "custom_properties_write",
        "vendor_extension": {"keep": True},
    }
    if write_topic:
        mqtt["write_topic"] = write_topic
    return {
        "type": "zendure_mqtt",
        "name": "Custom Writable",
        "enabled": True,
        "serial_number": "CUST1",
        "mqtt": mqtt,
        "capabilities": {
            "read_power": True,
            "read_soc": True,
            "write_output_limit": write_output_limit,
        },
    }


def _config(device):
    return {
        "system": {"max_total_power": 1600},
        "devices": [
            {"name": "WR1", "ip": "10.0.0.1", "sn": "REAL1", "max_power": 800},
            device,
        ],
        "grid_meter": {"type": "shelly", "ip": "10.0.0.9"},
        "zendure_mqtt": {
            "brokers": {
                "home_broker": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "10.0.0.10",
                    "port": 1883,
                }
            }
        },
    }


def _write_config(base_dir, data):
    config_dir = base_dir / "config"
    config_dir.mkdir(exist_ok=True)
    path = config_dir / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _mqtt_device(config):
    return [d for d in config["devices"] if d.get("type") == "zendure_mqtt"][0]


def _roundtrip(tmp_path, config, mutate=None):
    _write_config(tmp_path, config)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    assert loaded["status"] == "ok"
    draft = loaded["draft"]
    if mutate is not None:
        entry = next(d for d in draft["devices"] if d.get("kind") == "zendure_mqtt")
        mutate(entry)
    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok", prepared
    return json.loads(prepared["payload"])


# --- draft capability reflects the explicit write configuration -----------


def test_custom_writable_device_draft_reports_control_supported():
    draft = zendure_mqtt_device_draft(_custom_device())
    # Capability follows the configured values (valid explicit custom write
    # configuration), never the topic family alone.
    assert draft["supports_output_control"] is True
    assert draft["output_control"] is True
    assert draft["mqtt"]["write_protocol"] == "custom_properties_write"
    assert draft["mqtt"]["write_topic"] == WRITE_TOPIC
    # The custom escape hatch's explicit topic IS the effective one.
    assert draft["mqtt"]["effective_write_topic"] == WRITE_TOPIC
    assert draft["mqtt"]["effective_write_topic_source"] == "custom_explicit"
    assert draft["mqtt"]["write_topic_obsolete"] is False


def test_profile_backed_draft_shows_canonical_effective_topic():
    device = {
        "type": "zendure_mqtt",
        "name": "Pro2",
        "enabled": True,
        "serial_number": "SN1",
        "hardware_profile": "solarflow_800_pro_2",
        "power_write_profile": "zensdk_properties_write",
        "mqtt": {
            "broker_ref": "home_broker",
            "topic_family": "legacy_zendure_json",
            "device_id": "DEV",
            "product_key": "PK",
            # Obsolete residue: a pinned model ignores this.
            "write_topic": "/PK/DEV/properties/report",
        },
        "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": True},
    }
    draft = zendure_mqtt_device_draft(device)
    assert draft["mqtt"]["effective_write_topic"] == "iot/PK/DEV/properties/write"
    assert draft["mqtt"]["effective_write_topic_source"] == "canonical_profile"
    assert draft["mqtt"]["write_topic_obsolete"] is True


def test_scalar_device_without_custom_protocol_stays_unsupported():
    device = _custom_device(write_output_limit=False)
    del device["mqtt"]["write_protocol"]
    del device["mqtt"]["write_topic"]
    draft = zendure_mqtt_device_draft(device)
    assert draft["supports_output_control"] is False


# --- roundtrip and explicit edits ------------------------------------------


def test_custom_write_config_survives_noop_roundtrip(tmp_path):
    merged = _roundtrip(tmp_path, _config(_custom_device()))
    device = _mqtt_device(merged)
    assert device["capabilities"]["write_output_limit"] is True
    assert device["mqtt"]["write_protocol"] == "custom_properties_write"
    assert device["mqtt"]["write_topic"] == WRITE_TOPIC
    assert device["mqtt"]["vendor_extension"] == {"keep": True}


def test_explicit_disable_keeps_custom_write_metadata(tmp_path):
    def _disable(entry):
        entry["output_control"] = False
        entry["capabilities"]["write_output_limit"] = False

    merged = _roundtrip(tmp_path, _config(_custom_device()), mutate=_disable)
    device = _mqtt_device(merged)
    assert device["capabilities"]["write_output_limit"] is False
    # The custom write metadata is not re-derivable from the topic family, so
    # it stays for future re-enabling.
    assert device["mqtt"]["write_protocol"] == "custom_properties_write"
    assert device["mqtt"]["write_topic"] == WRITE_TOPIC


def test_explicit_enable_uses_existing_custom_write_protocol(tmp_path):
    def _enable(entry):
        entry["output_control"] = True
        entry["capabilities"]["write_output_limit"] = True

    merged = _roundtrip(
        tmp_path, _config(_custom_device(write_output_limit=False)), mutate=_enable
    )
    device = _mqtt_device(merged)
    assert device["capabilities"]["write_output_limit"] is True
    assert device["mqtt"]["write_protocol"] == "custom_properties_write"
    assert device["mqtt"]["write_topic"] == WRITE_TOPIC


def test_disable_still_drops_reinferable_legacy_protocol(tmp_path):
    # A legacy-family protocol is re-inferred on enable, so dropping it on
    # disable keeps configs minimal (existing behavior stays intact).
    device = {
        "type": "zendure_mqtt",
        "name": "Legacy Control",
        "enabled": True,
        "serial_number": "LEG1",
        "mqtt": {
            "broker_ref": "home_broker",
            "topic_family": "legacy_zendure_json",
            "base_topic": "iot",
            "device_id": "LEG1",
            "product_key": "PK1",
            "write_protocol": "legacy_properties_write",
        },
        "capabilities": {
            "read_power": True,
            "read_soc": True,
            "write_output_limit": True,
        },
    }

    def _disable(entry):
        entry["output_control"] = False
        entry["capabilities"]["write_output_limit"] = False

    merged = _roundtrip(tmp_path, _config(device), mutate=_disable)
    result = _mqtt_device(merged)
    assert result["capabilities"]["write_output_limit"] is False
    assert "write_protocol" not in result["mqtt"]


# --- invalid custom configuration is rejected, never downgraded -------------


def test_custom_writable_without_write_topic_is_a_validation_error(tmp_path):
    _write_config(tmp_path, _config(_custom_device(write_topic=None)))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is False
    codes = {e["code"] for e in preview["validation"]["errors"]}
    assert "write_topic_required" in codes
    # No silent downgrade: the previewed device still requests control.
    device = _mqtt_device(preview["preview"])
    assert device["capabilities"]["write_output_limit"] is True

    prepared = prepare_maintenance_config_apply(
        loaded["draft"], loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "invalid"
    assert "payload" not in prepared


def test_scalar_control_without_protocol_is_a_validation_error(tmp_path):
    device = _custom_device()
    del device["mqtt"]["write_protocol"]
    del device["mqtt"]["write_topic"]
    _write_config(tmp_path, _config(device))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is False
    codes = {e["code"] for e in preview["validation"]["errors"]}
    assert "write_protocol_unsupported" in codes


# --- discovery-derived devices keep the shared capability rule --------------


def test_forged_scalar_draft_cannot_become_writable(tmp_path):
    # A new draft entry (discovery projection shape) claiming a custom write
    # protocol on a scalar family has no trusted write topic. The explicit
    # operator intent remains visible and validation blocks the draft rather
    # than silently changing the checkbox back to telemetry-only.
    _write_config(tmp_path, _config(_custom_device()))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["devices"].append(
        {
            "kind": "zendure_mqtt",
            "original_name": None,
            "name": "Forged Scalar",
            "enabled": True,
            "has_enabled_key": True,
            "serial_number": "FORGE1",
            "device_id": "FORGE1",
            "hardware_profile": "solarflow_zensdk",
            "output_control": True,
            "mqtt": {
                "broker_ref": "home_broker",
                "topic_family": "zensdk_ha_scalar",
                "base_topic": "Zendure",
                "device_id": "FORGE1",
                "write_protocol": "custom_properties_write",
            },
            "capabilities": {
                "read_power": True,
                "read_soc": True,
                "write_output_limit": True,
            },
        }
    )
    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "invalid", prepared
    assert "write_target_missing" in {
        issue["code"] for issue in prepared["validation"]["errors"]
    }
