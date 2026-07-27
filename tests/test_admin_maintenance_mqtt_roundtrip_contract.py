# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance no-op roundtrip contract for Zendure MQTT configs.

An unchanged Maintenance draft must be a *semantic no-op*: loading the current
config into an editable draft and applying it without edits must not mutate the
stored config. These tests pin the operational fields that must survive a
roundtrip — control capability, distinct identifiers, named broker profiles, D0
grid-meter references, unknown extension keys — and separately assert that
intentional single-field edits change only the selected field.
"""

import copy
import json

import pytest

from admin.maintenance_config import (
    load_maintenance_config,
    prepare_maintenance_config_apply,
    preview_maintenance_config,
)

pytestmark = pytest.mark.simulation


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _write_config(base_dir, data):
    config_dir = base_dir / "config"
    config_dir.mkdir(exist_ok=True)
    path = config_dir / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def assert_semantic_noop_roundtrip(config, tmp_path):
    """Load ``config`` as a maintenance draft and apply it unchanged.

    Asserts the diff is empty (``changed is False``) and that the true merged
    config (secrets intact, via the apply payload) equals the input exactly.
    """

    original = copy.deepcopy(config)
    _write_config(tmp_path, config)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    assert loaded["status"] == "ok"
    draft = loaded["draft"]

    result = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert result["status"] == "ok"
    assert result["changed"] is False, result["diff"]

    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok", prepared
    merged = json.loads(prepared["payload"])
    assert merged == original
    return loaded, merged


# --- config builders -----------------------------------------------------


def _base_config():
    return {
        "system": {"max_total_power": 1600},
        "devices": [
            {"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800},
        ],
        "grid_meter": {"type": "shelly", "ip": "192.168.1.50"},
    }


def _named_local_broker(ref="local_mqtt_10_0_0_10_aabbccdd", host="10.0.0.10",
                        credentials_ref=None, tls=False):
    profile = {
        "enabled": True,
        "source": "local_mqtt",
        "host": host,
        "port": 8883 if tls else 1883,
        "tls": tls,
    }
    if tls:
        profile["tls_insecure"] = False
    if credentials_ref:
        profile["credentials_ref"] = credentials_ref
    return ref, profile


def _telemetry_device(ref, name="Zendure MQTT", serial="SERIAL-1", device_id="SERIAL-1"):
    return {
        "type": "zendure_mqtt",
        "name": name,
        "enabled": True,
        "serial_number": serial,
        "mqtt": {
            "broker_ref": ref,
            "topic_family": "zensdk_ha_scalar",
            "base_topic": "Zendure",
            "device_id": device_id,
        },
        "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": False},
    }


# --- Case A: named local broker, telemetry device ------------------------


def test_case_a_named_local_broker_telemetry_noop(tmp_path):
    ref, profile = _named_local_broker(credentials_ref="mqtt-broker-1")
    config = _base_config()
    config["zendure_mqtt"] = {"enabled": True, "brokers": {ref: profile}}
    device = _telemetry_device(ref)
    device["mqtt"]["product_key"] = "PK-XYZ"
    device["ext_unknown"] = {"keep": True}
    config["devices"].append(device)
    loaded, merged = assert_semantic_noop_roundtrip(config, tmp_path)
    assert "PK-XYZ" not in json.dumps(loaded)
    # No obsolete top-level broker fields beside the named profile.
    assert set(merged["zendure_mqtt"]) == {"enabled", "brokers"}
    assert merged["zendure_mqtt"]["brokers"][ref] == profile


# --- Case B: named local broker, control device --------------------------


def test_case_b_control_device_survives_noop(tmp_path):
    ref, profile = _named_local_broker()
    config = _base_config()
    config["zendure_mqtt"] = {"enabled": True, "brokers": {ref: profile}}
    control = _telemetry_device(ref, name="Zendure Control")
    control["capabilities"]["write_output_limit"] = True
    control["mqtt"]["product_key"] = "PK-CTL"
    control["mqtt"]["write_protocol"] = "custom_properties_write"
    control["mqtt"]["write_topic"] = "iot/PK-CTL/DEVICE/properties/write"
    config["devices"].append(control)
    _, merged = assert_semantic_noop_roundtrip(config, tmp_path)
    dev = [d for d in merged["devices"] if d.get("type") == "zendure_mqtt"][0]
    assert dev["capabilities"]["write_output_limit"] is True
    assert dev["mqtt"]["write_protocol"] == "custom_properties_write"
    assert dev["mqtt"]["write_topic"] == "iot/PK-CTL/DEVICE/properties/write"


# --- Case C: distinct serial and MQTT device ID --------------------------


def test_case_c_distinct_serial_and_device_id_survive(tmp_path):
    ref, profile = _named_local_broker()
    config = _base_config()
    config["zendure_mqtt"] = {"enabled": True, "brokers": {ref: profile}}
    config["devices"].append(
        _telemetry_device(ref, serial="SERIAL-1", device_id="DEVICE-1")
    )
    _, merged = assert_semantic_noop_roundtrip(config, tmp_path)
    dev = [d for d in merged["devices"] if d.get("type") == "zendure_mqtt"][0]
    assert dev["serial_number"] == "SERIAL-1"
    assert dev["mqtt"]["device_id"] == "DEVICE-1"


# --- Case D: alternative legacy leading-slash layout ---------------------


def test_case_d_legacy_alt_layout_noop(tmp_path):
    ref, profile = _named_local_broker()
    config = _base_config()
    config["zendure_mqtt"] = {"enabled": True, "brokers": {ref: profile}}
    config["devices"].append(
        {
            "type": "zendure_mqtt",
            "name": "Zendure Legacy Alt",
            "enabled": True,
            "serial_number": "LEG-1",
            "mqtt": {
                "broker_ref": ref,
                "topic_family": "legacy_zendure_json_alt",
                "base_topic": "/iot",
                "device_id": "LEG-1",
                "product_key": "PK-LEG",
            },
            "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": False},
        }
    )
    _, merged = assert_semantic_noop_roundtrip(config, tmp_path)
    dev = [d for d in merged["devices"] if d.get("type") == "zendure_mqtt"][0]
    assert dev["mqtt"]["topic_family"] == "legacy_zendure_json_alt"
    assert dev["mqtt"]["base_topic"] == "/iot"
    assert dev["mqtt"]["product_key"] == "PK-LEG"


# --- Case E: D0 grid meter on a named local broker -----------------------


def test_case_e_d0_grid_meter_named_broker_noop(tmp_path):
    ref, profile = _named_local_broker(credentials_ref="mqtt-broker-1")
    config = _base_config()
    config["zendure_mqtt"] = {"enabled": True, "brokers": {ref: profile}}
    config["grid_meter"] = {
        "type": "zendure_smartmeter_d0",
        "mqtt": {
            "broker_ref": ref,
            "topic": "Zendure/D0SN/properties/report",
            # D0 requires payload_format "number" (the canonical setup value); the
            # config must be Core-valid so Maintenance parity validation accepts it.
            "payload_format": "number",
            "max_age_seconds": 30,
        },
    }
    assert_semantic_noop_roundtrip(config, tmp_path)


# --- Case F: multiple named brokers --------------------------------------


def test_case_f_multiple_named_brokers_noop(tmp_path):
    ref_a, prof_a = _named_local_broker(ref="local_mqtt_a", host="10.0.0.10")
    ref_b, prof_b = _named_local_broker(ref="local_mqtt_b", host="10.0.0.11")
    config = _base_config()
    config["zendure_mqtt"] = {
        "enabled": True,
        "brokers": {
            ref_a: prof_a,
            ref_b: prof_b,
            "zendure_cloud": {
                "enabled": True,
                "source": "zendure_cloud_mqtt",
                "host": "mqtteu.zen-iot.com",
                "port": 8883,
                "tls": True,
                "tls_insecure": True,
                "credentials_ref": "zendure-cloud",
            },
        },
    }
    config["devices"].append(_telemetry_device(ref_a, name="Dev A", serial="A-1", device_id="A-1"))
    config["devices"].append(_telemetry_device(ref_b, name="Dev B", serial="B-1", device_id="B-1"))
    _, merged = assert_semantic_noop_roundtrip(config, tmp_path)
    assert set(merged["zendure_mqtt"]["brokers"]) == {ref_a, ref_b, "zendure_cloud"}
    devs = [d for d in merged["devices"] if d.get("type") == "zendure_mqtt"]
    assert {d["mqtt"]["broker_ref"] for d in devs} == {ref_a, ref_b}


# --- Case G: disabled broker/device --------------------------------------


def test_case_g_disabled_broker_and_device_stay_disabled(tmp_path):
    ref, profile = _named_local_broker()
    profile["enabled"] = False
    config = _base_config()
    config["zendure_mqtt"] = {"enabled": True, "brokers": {ref: profile}}
    device = _telemetry_device(ref)
    device["enabled"] = False
    config["devices"].append(device)
    _, merged = assert_semantic_noop_roundtrip(config, tmp_path)
    assert merged["zendure_mqtt"]["brokers"][ref]["enabled"] is False
    dev = [d for d in merged["devices"] if d.get("type") == "zendure_mqtt"][0]
    assert dev["enabled"] is False


# --- Case H: old single-broker configuration -----------------------------


def test_case_h_legacy_single_broker_full_noop(tmp_path):
    config = _base_config()
    config["zendure_mqtt"] = {
        "enabled": True,
        "host": "192.168.1.20",
        "port": 1883,
        "tls": False,
        "username": "mqttuser",
        "password": "BROKER_SECRET",
    }
    config["devices"].append(
        {
            "type": "zendure_mqtt",
            "name": "Legacy Single",
            "enabled": True,
            "serial_number": "LSB-1",
            "mqtt": {
                "topic_family": "zensdk_ha_scalar",
                "base_topic": "Zendure",
                "device_id": "LSB-1",
            },
            "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": False},
        }
    )
    assert_semantic_noop_roundtrip(config, tmp_path)


def test_case_h_legacy_single_broker_without_tls_key_noop(tmp_path):
    # A minimal legacy broker lacking tls/port keys must not be migrated: a no-op
    # apply must not inject tls:false / port:1883 beside the stored fields.
    config = _base_config()
    config["zendure_mqtt"] = {
        "enabled": True,
        "host": "192.168.1.20",
        "username": "mqttuser",
        "password": "BROKER_SECRET",
    }
    config["devices"].append(
        {
            "type": "zendure_mqtt",
            "name": "Legacy Minimal",
            "enabled": True,
            "serial_number": "LMB-1",
            "mqtt": {
                "topic_family": "zensdk_ha_scalar",
                "base_topic": "Zendure",
                "device_id": "LMB-1",
            },
            "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": False},
        }
    )
    assert_semantic_noop_roundtrip(config, tmp_path)


# --- Case I: unknown keys ------------------------------------------------


def test_case_i_unknown_nonsecret_keys_survive(tmp_path):
    ref, profile = _named_local_broker()
    profile["vendor_extension"] = {"nested": [1, 2, 3]}
    config = _base_config()
    config["zendure_mqtt"] = {"enabled": True, "brokers": {ref: profile}, "extra_top": "keep"}
    device = _telemetry_device(ref)
    device["custom_note"] = "operator comment"
    config["devices"].append(device)
    config["some_other_feature"] = {"a": 1, "b": [True, False]}
    _, merged = assert_semantic_noop_roundtrip(config, tmp_path)
    assert merged["zendure_mqtt"]["extra_top"] == "keep"
    assert merged["zendure_mqtt"]["brokers"][ref]["vendor_extension"] == {"nested": [1, 2, 3]}
    dev = [d for d in merged["devices"] if d.get("type") == "zendure_mqtt"][0]
    assert dev["custom_note"] == "operator comment"


def test_case_i_unknown_secret_like_key_not_leaked_but_preserved(tmp_path):
    # A hidden secret-like broker field must not reach the browser, but must not
    # be deleted from the stored config just because the UI cannot display it.
    ref, profile = _named_local_broker()
    profile["legacy_app_key"] = "SECRET-APP-KEY"
    config = _base_config()
    config["zendure_mqtt"] = {"enabled": True, "brokers": {ref: profile}}
    config["devices"].append(_telemetry_device(ref))
    _write_config(tmp_path, config)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    assert "SECRET-APP-KEY" not in json.dumps(loaded)
    draft = loaded["draft"]
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert "SECRET-APP-KEY" not in json.dumps(preview)
    prepared = prepare_maintenance_config_apply(draft, loaded["revision"], base_dir=str(tmp_path))
    assert prepared["status"] == "ok"
    assert json.loads(prepared["payload"])["zendure_mqtt"]["brokers"][ref]["legacy_app_key"] == "SECRET-APP-KEY"


# --- Step 1.2: explicit edits change only the selected field --------------


def _load_edit_apply(config, tmp_path, mutate):
    _write_config(tmp_path, config)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    mutate(draft)
    prepared = prepare_maintenance_config_apply(draft, loaded["revision"], base_dir=str(tmp_path))
    assert prepared["status"] == "ok", prepared
    return json.loads(prepared["payload"])


def test_edit_rename_only(tmp_path):
    ref, profile = _named_local_broker()
    config = _base_config()
    config["zendure_mqtt"] = {"enabled": True, "brokers": {ref: profile}}
    config["devices"].append(_telemetry_device(ref, serial="SERIAL-1", device_id="DEVICE-1"))

    def mutate(draft):
        dev = [d for d in draft["devices"] if d.get("kind") == "zendure_mqtt"][0]
        dev["name"] = "Renamed"

    merged = _load_edit_apply(config, tmp_path, mutate)
    dev = [d for d in merged["devices"] if d.get("type") == "zendure_mqtt"][0]
    assert dev["name"] == "Renamed"
    assert dev["serial_number"] == "SERIAL-1"
    assert dev["mqtt"]["device_id"] == "DEVICE-1"


def test_edit_serial_only_keeps_device_id(tmp_path):
    ref, profile = _named_local_broker()
    config = _base_config()
    config["zendure_mqtt"] = {"enabled": True, "brokers": {ref: profile}}
    config["devices"].append(_telemetry_device(ref, serial="SERIAL-1", device_id="DEVICE-1"))

    def mutate(draft):
        dev = [d for d in draft["devices"] if d.get("kind") == "zendure_mqtt"][0]
        dev["serial_number"] = "SERIAL-2"

    merged = _load_edit_apply(config, tmp_path, mutate)
    dev = [d for d in merged["devices"] if d.get("type") == "zendure_mqtt"][0]
    assert dev["serial_number"] == "SERIAL-2"
    assert dev["mqtt"]["device_id"] == "DEVICE-1"


def test_edit_device_id_only_keeps_serial(tmp_path):
    ref, profile = _named_local_broker()
    config = _base_config()
    config["zendure_mqtt"] = {"enabled": True, "brokers": {ref: profile}}
    config["devices"].append(_telemetry_device(ref, serial="SERIAL-1", device_id="DEVICE-1"))

    def mutate(draft):
        dev = [d for d in draft["devices"] if d.get("kind") == "zendure_mqtt"][0]
        dev["mqtt"]["device_id"] = "DEVICE-2"
        dev["device_id"] = "DEVICE-2"

    merged = _load_edit_apply(config, tmp_path, mutate)
    dev = [d for d in merged["devices"] if d.get("type") == "zendure_mqtt"][0]
    assert dev["serial_number"] == "SERIAL-1"
    assert dev["mqtt"]["device_id"] == "DEVICE-2"


def test_edit_disable_only(tmp_path):
    ref, profile = _named_local_broker()
    config = _base_config()
    config["zendure_mqtt"] = {"enabled": True, "brokers": {ref: profile}}
    config["devices"].append(_telemetry_device(ref))

    def mutate(draft):
        dev = [d for d in draft["devices"] if d.get("kind") == "zendure_mqtt"][0]
        dev["enabled"] = False

    merged = _load_edit_apply(config, tmp_path, mutate)
    dev = [d for d in merged["devices"] if d.get("type") == "zendure_mqtt"][0]
    assert dev["enabled"] is False
    assert dev["capabilities"]["write_output_limit"] is False


# --- unsafe legacy control entry: top-level device_id is not an MQTT route -----


def _unsafe_control_config(ref="local_mqtt_home", *, top_level_device_id="TOPLEVEL-ID"):
    """A control device carrying a top-level device_id but no mqtt.device_id.

    This is the invalid-but-loadable shape a legacy config can hold: output
    control is requested, yet the entry has no explicit MQTT route id, so the
    top-level device_id is the only device identifier present.
    """

    _, profile = _named_local_broker(ref=ref)
    config = _base_config()
    config["zendure_mqtt"] = {"enabled": True, "brokers": {ref: profile}}
    config["devices"].append(
        {
            "type": "zendure_mqtt",
            "name": "Unsafe Control",
            "enabled": True,
            "serial_number": "PHYSICAL-SERIAL",
            "device_id": top_level_device_id,
            "hardware_profile": "hyper_2000",
            "power_write_profile": "legacy_object_device_automation",
            "mqtt": {
                "broker_ref": ref,
                "topic_family": "legacy_zendure_json",
                "base_topic": "iot",
                "product_key": "PK-A",
            },
            "capabilities": {
                "read_power": True,
                "read_soc": True,
                "write_output_limit": True,
            },
        }
    )
    return config


def test_noop_roundtrip_of_unsafe_legacy_entry_stays_mqtt_device_id_missing(tmp_path):
    config = _unsafe_control_config()
    _write_config(tmp_path, config)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    assert loaded["status"] == "ok"
    draft = loaded["draft"]
    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    # The unchanged unsafe entry stays invalid: the top-level device_id is never
    # promoted into the MQTT route, so the control validator still rejects it.
    assert prepared["status"] == "invalid", prepared
    codes = {issue["code"] for issue in prepared["validation"]["errors"]}
    assert "mqtt_device_id_missing" in codes


def test_entering_explicit_route_device_id_repairs_the_unsafe_entry(tmp_path):
    config = _unsafe_control_config()
    _write_config(tmp_path, config)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    dev = [d for d in draft["devices"] if d.get("kind") == "zendure_mqtt"][0]
    dev["mqtt"]["device_id"] = "ROUTE-DEV-ID"
    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok", prepared
    merged = json.loads(prepared["payload"])
    device = [d for d in merged["devices"] if d.get("type") == "zendure_mqtt"][0]
    assert device["mqtt"]["device_id"] == "ROUTE-DEV-ID"
    assert device["capabilities"]["write_output_limit"] is True
    # The physical serial and the legacy top-level device_id are left untouched.
    assert device["serial_number"] == "PHYSICAL-SERIAL"
