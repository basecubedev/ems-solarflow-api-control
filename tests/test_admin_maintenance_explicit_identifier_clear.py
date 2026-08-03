# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance explicit-value semantics for editable Zendure MQTT identifiers.

Maintenance is an editor for the real EMS config, so a visible field must not
claim to be cleared while Apply silently keeps its previous value. Every
editable, non-secret identifier distinguishes three draft states:

* key absent from the draft -> preserve the stored value,
* key present with a value  -> replace the stored value,
* key present and empty     -> remove the stored value.

A masked cloud placeholder is a display artefact, never an explicit clear: the
browser is never given the real identifier, so it can never resubmit it.
"""

import copy
import json

import pytest

from admin.maintenance_config import (
    load_maintenance_config,
    prepare_maintenance_config_apply,
    preview_maintenance_config,
)
from admin.zendure_mqtt_config_draft import apply_zendure_mqtt_draft_fields
from ems.zendure_mqtt.config_entries import (
    validate_zendure_mqtt_control_device_config,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.integration,
    pytest.mark.simulation,
]

MASK = "••••"


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _control_device():
    return {
        "type": "zendure_mqtt",
        "name": "INV_1",
        "serial_number": "PHYSICAL-SERIAL",
        "hardware_profile": "hyper_2000",
        "power_write_profile": "legacy_object_device_automation",
        "mqtt": {
            "broker_ref": "local_mixed",
            "topic_family": "legacy_zendure_json",
            "device_id": "ROUTE-ID",
            "product_key": "PK-A",
        },
        "capabilities": {
            "read_power": True,
            "read_soc": True,
            "write_output_limit": True,
        },
    }


def _draft_item(**overrides):
    item = {
        "name": "INV_1",
        "kind": "zendure_mqtt",
        "serial_number": "PHYSICAL-SERIAL",
        "hardware_generation": "hub_hyper_legacy",
        "hardware_model": "hyper_2000",
        "mqtt": {"device_id": "ROUTE-ID"},
    }
    mqtt = overrides.pop("mqtt", None)
    item.update(overrides)
    if mqtt is not None:
        item["mqtt"] = mqtt
    return item


# --- physical serial --------------------------------------------------------


def test_explicit_empty_serial_removes_the_stored_serial():
    device = _control_device()
    apply_zendure_mqtt_draft_fields(device, _draft_item(serial_number=""))
    assert "serial_number" not in device


def test_missing_serial_key_preserves_the_stored_serial():
    device = _control_device()
    item = _draft_item()
    item.pop("serial_number")
    apply_zendure_mqtt_draft_fields(device, item)
    assert device["serial_number"] == "PHYSICAL-SERIAL"


def test_explicit_serial_replaces_the_stored_serial_and_is_stripped():
    device = _control_device()
    apply_zendure_mqtt_draft_fields(device, _draft_item(serial_number=" NEW-SERIAL "))
    assert device["serial_number"] == "NEW-SERIAL"


def test_clearing_the_serial_never_changes_the_route_device_id():
    device = _control_device()
    apply_zendure_mqtt_draft_fields(device, _draft_item(serial_number=""))
    assert device["mqtt"]["device_id"] == "ROUTE-ID"


def test_clearing_the_serial_never_infers_it_from_the_route_device_id():
    device = _control_device()
    apply_zendure_mqtt_draft_fields(device, _draft_item(serial_number=""))
    assert device.get("serial_number") != "ROUTE-ID"
    assert "serial_number" not in device


def test_blank_stored_serial_stays_byte_identical_on_a_no_op_apply():
    # Fresh Setup persists an empty serial_number for a route-only manual entry,
    # so clearing an already-blank value must not rewrite the stored config.
    device = _control_device()
    device["serial_number"] = ""
    before = copy.deepcopy(device)
    apply_zendure_mqtt_draft_fields(device, _draft_item(serial_number=""))
    assert device == before


# --- MQTT route device id ---------------------------------------------------


def test_explicit_empty_route_device_id_removes_the_stored_route_id():
    device = _control_device()
    apply_zendure_mqtt_draft_fields(device, _draft_item(mqtt={"device_id": ""}))
    assert "device_id" not in device["mqtt"]


def test_missing_route_device_id_key_preserves_the_stored_route_id():
    device = _control_device()
    apply_zendure_mqtt_draft_fields(device, _draft_item(mqtt={}))
    assert device["mqtt"]["device_id"] == "ROUTE-ID"


def test_masked_route_device_id_preserves_the_real_stored_route_id():
    device = _control_device()
    apply_zendure_mqtt_draft_fields(device, _draft_item(mqtt={"device_id": MASK}))
    assert device["mqtt"]["device_id"] == "ROUTE-ID"


def test_explicit_route_device_id_is_stored_with_case_preserved():
    device = _control_device()
    apply_zendure_mqtt_draft_fields(
        device, _draft_item(mqtt={"device_id": " NewRoute "})
    )
    assert device["mqtt"]["device_id"] == "NewRoute"


def test_clearing_the_route_device_id_never_changes_the_serial():
    device = _control_device()
    apply_zendure_mqtt_draft_fields(device, _draft_item(mqtt={"device_id": ""}))
    assert device["serial_number"] == "PHYSICAL-SERIAL"


def test_clearing_the_route_device_id_never_infers_it_from_other_identities():
    device = _control_device()
    apply_zendure_mqtt_draft_fields(
        device,
        _draft_item(
            serial_number="PHYSICAL-SERIAL",
            sn="PHYSICAL-SERIAL",
            device_id="LEGACY-TOP-LEVEL",
            mqtt={"device_id": ""},
        ),
    )
    assert "device_id" not in device["mqtt"]


def test_clearing_the_route_device_id_blocks_a_write_capable_entry():
    device = _control_device()
    apply_zendure_mqtt_draft_fields(device, _draft_item(mqtt={"device_id": ""}))
    codes = {
        issue["code"]
        for issue in validate_zendure_mqtt_control_device_config(device)
        if issue.get("severity") == "error"
    }
    assert device["capabilities"]["write_output_limit"] is False or (
        "mqtt_device_id_missing" in codes
    )


def test_clearing_the_route_device_id_with_control_off_disables_writes():
    device = _control_device()
    apply_zendure_mqtt_draft_fields(
        device, _draft_item(mqtt={"device_id": ""}, output_control=False)
    )
    assert device["capabilities"]["write_output_limit"] is False
    assert "device_id" not in device["mqtt"]


# --- other editable addressing fields --------------------------------------


def test_explicit_empty_product_key_removes_the_stored_product_key():
    device = _control_device()
    apply_zendure_mqtt_draft_fields(device, _draft_item(product_key=""))
    assert "product_key" not in device["mqtt"]


def test_missing_product_key_preserves_the_stored_product_key():
    device = _control_device()
    apply_zendure_mqtt_draft_fields(device, _draft_item())
    assert device["mqtt"]["product_key"] == "PK-A"


def test_masked_product_key_preserves_the_stored_product_key():
    device = _control_device()
    apply_zendure_mqtt_draft_fields(device, _draft_item(product_key=MASK))
    assert device["mqtt"]["product_key"] == "PK-A"


def test_write_topic_is_not_editable_through_the_draft():
    # The pinned model publishes to its canonical topic; a stored write_topic is
    # read-only residue the draft must neither introduce nor clear.
    device = _control_device()
    device["mqtt"]["write_topic"] = "custom/topic"
    apply_zendure_mqtt_draft_fields(
        device, _draft_item(mqtt={"device_id": "ROUTE-ID", "write_topic": ""})
    )
    assert device["mqtt"]["write_topic"] == "custom/topic"


# --- full preview / apply / reload round trip -------------------------------


def _mixed_config():
    return {
        "config_schema_version": 3,
        "system": {"max_total_power": 2000},
        "grid_meter": {"type": "shelly", "ip": "192.168.50.2"},
        "zendure_mqtt": {
            "brokers": {
                "local_mixed": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "192.168.50.10",
                    "port": 1883,
                    "tls": False,
                    "username": "local-user",
                    "password": "local-secret",
                }
            }
        },
        "devices": [
            {"name": "WR1", "ip": "192.168.50.20", "sn": "API-SERIAL", "max_power": 800},
            _control_device(),
        ],
    }


def _rewrite_config(path, payload):
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(payload, encoding="utf-8")


def _write_config(base_dir, config):
    config_dir = base_dir / "config"
    config_dir.mkdir(exist_ok=True)
    path = config_dir / "config.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def test_preview_apply_and_reload_keep_a_cleared_route_field_empty(tmp_path):
    path = _write_config(tmp_path, _mixed_config())
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    assert loaded["status"] == "ok"

    draft = copy.deepcopy(loaded["draft"])
    device = draft["devices"][1]
    device["mqtt"]["device_id"] = ""
    device["output_control"] = False
    device["capabilities"]["write_output_limit"] = False

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["status"] == "ok", preview
    assert preview["validation"]["ok"] is True, preview["validation"]

    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok", prepared
    merged = json.loads(prepared["payload"])
    assert "device_id" not in merged["devices"][1]["mqtt"]
    assert merged["devices"][1]["serial_number"] == "PHYSICAL-SERIAL"
    assert merged["devices"][1]["capabilities"]["write_output_limit"] is False

    _rewrite_config(path, prepared["payload"])
    reloaded = load_maintenance_config(base_dir=str(tmp_path))
    assert reloaded["status"] == "ok"
    reloaded_device = reloaded["draft"]["devices"][1]
    assert reloaded_device["mqtt"]["device_id"] == ""
    assert reloaded_device["serial_number"] == "PHYSICAL-SERIAL"
    assert reloaded_device["control_readiness"]["ready"] is False


def test_preview_apply_and_reload_keep_a_cleared_serial_removed(tmp_path):
    path = _write_config(tmp_path, _mixed_config())
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = copy.deepcopy(loaded["draft"])
    draft["devices"][1]["serial_number"] = ""

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["status"] == "ok", preview

    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    if prepared["status"] != "ok":
        # A telemetry contract that requires the serial must say so, not restore it.
        assert prepared["status"] == "invalid", prepared
        return
    merged = json.loads(prepared["payload"])
    assert "serial_number" not in merged["devices"][1]
    assert merged["devices"][1]["mqtt"]["device_id"] == "ROUTE-ID"

    _rewrite_config(path, prepared["payload"])
    reloaded = load_maintenance_config(base_dir=str(tmp_path))
    assert reloaded["draft"]["devices"][1]["serial_number"] == ""


def test_control_intent_left_on_after_clearing_the_route_blocks_apply(tmp_path):
    _write_config(tmp_path, _mixed_config())
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = copy.deepcopy(loaded["draft"])
    draft["devices"][1]["mqtt"]["device_id"] = ""

    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    if prepared["status"] == "ok":
        merged = json.loads(prepared["payload"])
        assert merged["devices"][1]["capabilities"]["write_output_limit"] is False
    else:
        assert prepared["status"] == "invalid", prepared
