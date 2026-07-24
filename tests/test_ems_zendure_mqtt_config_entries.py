# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for EMS-side Zendure MQTT config-entry helpers (no broker, no paho)."""

import copy
import sys

import pytest

from ems.zendure_mqtt.config_entries import (
    ZENDURE_MQTT_TYPE,
    duplicate_device_name_startup_error,
    duplicate_zendure_identity_startup_error,
    find_duplicate_device_names,
    zendure_cloud_device_subscriptions,
    find_duplicate_zendure_device_identities,
    find_zendure_mqtt_broker_profile_issues,
    has_enabled_mqtt_control_device,
    has_runtime_control_device,
    is_telemetry_only_zendure_mqtt_device_config,
    is_zendure_mqtt_device_config,
    validate_zendure_mqtt_control_device_config,
    validate_zendure_mqtt_device_config,
    zendure_config_device_identity,
    zendure_mqtt_broker_profile_views,
)

pytestmark = pytest.mark.simulation


def _telemetry_only_entry():
    return {
        "type": ZENDURE_MQTT_TYPE,
        "enabled": True,
        "name": "Zendure MQTT SolarFlow 800",
        "mqtt": {
            "topic_family": "zensdk_ha_scalar",
            "base_topic": "Zendure",
            "device_id": "ABC123",
            "product_key": "PK1",
            "app_key": None,
        },
        "capabilities": {
            "read_power": True,
            "read_soc": True,
            "write_output_limit": False,
        },
    }


def test_valid_telemetry_only_entry_is_recognized():
    entry = _telemetry_only_entry()
    assert is_zendure_mqtt_device_config(entry) is True
    assert is_telemetry_only_zendure_mqtt_device_config(entry) is True
    assert validate_zendure_mqtt_device_config(entry) == []


def test_http_device_is_not_zendure_mqtt():
    http_device = {"name": "WR1", "ip": "192.168.1.50", "sn": "SER1"}
    assert is_zendure_mqtt_device_config(http_device) is False
    assert is_telemetry_only_zendure_mqtt_device_config(http_device) is False


def test_non_dict_is_not_recognized():
    assert is_zendure_mqtt_device_config("nope") is False
    assert is_zendure_mqtt_device_config(None) is False


def test_missing_mqtt_object_is_invalid():
    entry = _telemetry_only_entry()
    del entry["mqtt"]
    codes = {i["code"] for i in validate_zendure_mqtt_device_config(entry)}
    assert "mqtt_missing" in codes
    assert "device_identifier_missing" in codes


def test_missing_topic_family_is_invalid():
    entry = _telemetry_only_entry()
    del entry["mqtt"]["topic_family"]
    codes = {i["code"] for i in validate_zendure_mqtt_device_config(entry)}
    assert "topic_family_missing" in codes


def test_missing_device_identifier_is_invalid():
    entry = _telemetry_only_entry()
    entry["mqtt"].pop("device_id")
    entry.pop("serial_number", None)
    entry.pop("device_id", None)
    codes = {i["code"] for i in validate_zendure_mqtt_device_config(entry)}
    assert "device_identifier_missing" in codes


def test_serial_number_satisfies_device_identifier():
    entry = _telemetry_only_entry()
    entry["mqtt"].pop("device_id")
    entry["serial_number"] = "SN-9"
    assert validate_zendure_mqtt_device_config(entry) == []


def test_missing_name_is_invalid():
    entry = _telemetry_only_entry()
    entry["name"] = "  "
    codes = {i["code"] for i in validate_zendure_mqtt_device_config(entry)}
    assert "name_missing" in codes


def test_write_output_limit_true_is_rejected():
    entry = _telemetry_only_entry()
    entry["capabilities"]["write_output_limit"] = True
    assert is_telemetry_only_zendure_mqtt_device_config(entry) is False
    issues = validate_zendure_mqtt_device_config(entry)
    codes = {i["code"] for i in issues}
    assert "write_output_limit_unsupported" in codes
    assert all(i["severity"] == "error" for i in issues if i["code"].startswith("write"))


def test_wrong_type_is_rejected():
    entry = _telemetry_only_entry()
    entry["type"] = "shelly"
    issues = validate_zendure_mqtt_device_config(entry)
    assert {i["code"] for i in issues} == {"not_zendure_mqtt"}


def test_validation_messages_do_not_leak_secret_values():
    entry = _telemetry_only_entry()
    entry["mqtt"]["app_key"] = "SUPER-SECRET-APP-KEY"
    entry["mqtt"]["password"] = "broker-pw"
    del entry["mqtt"]["topic_family"]
    text = " ".join(i["message"] for i in validate_zendure_mqtt_device_config(entry))
    assert "SUPER-SECRET-APP-KEY" not in text
    assert "broker-pw" not in text


def test_helper_module_does_not_import_paho_or_runtime_client():
    # Run in a clean interpreter so unrelated tests that import the runtime
    # MQTT client/paho earlier in the session cannot mask a real dependency.
    import subprocess

    code = (
        "import sys\n"
        "import ems.zendure_mqtt.config_entries\n"
        "assert 'paho' not in sys.modules\n"
        "assert 'ems.zendure_mqtt.client' not in sys.modules\n"
        "assert 'ems.zendure_mqtt.service' not in sys.modules\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_input_entry_is_not_mutated():
    entry = _telemetry_only_entry()
    before = copy.deepcopy(entry)
    validate_zendure_mqtt_device_config(entry)
    is_telemetry_only_zendure_mqtt_device_config(entry)
    assert entry == before


# --- device identity -----------------------------------------------------


def test_identity_from_local_api_sn():
    assert zendure_config_device_identity(
        {"name": "WR1", "ip": "192.168.1.50", "sn": "SER1"}
    ) == ("serial", "ser1")


def test_identity_from_top_level_serial_number():
    assert zendure_config_device_identity(
        {"name": "WR1", "serial_number": "Abc-9"}
    ) == ("serial", "abc-9")


def test_mqtt_identity_prefers_serial_number_when_present():
    entry = _telemetry_only_entry()
    entry["serial_number"] = "SN-9"
    assert zendure_config_device_identity(entry) == ("serial", "sn-9")


def test_mqtt_identity_from_product_key_and_device_id():
    entry = _telemetry_only_entry()
    entry.pop("serial_number", None)
    assert zendure_config_device_identity(entry) == ("mqtt_pk", "pk1", "abc123")


def test_mqtt_identity_from_topic_family_and_device_id():
    entry = _telemetry_only_entry()
    entry["mqtt"].pop("product_key")
    assert zendure_config_device_identity(entry) == (
        "mqtt_tf",
        "zensdk_ha_scalar",
        "abc123",
    )


def test_mqtt_identity_from_device_id_only():
    entry = _telemetry_only_entry()
    entry["mqtt"].pop("product_key")
    entry["mqtt"].pop("topic_family")
    assert zendure_config_device_identity(entry) == ("mqtt_dev", "abc123")


def test_identity_is_none_without_meaningful_fields():
    assert zendure_config_device_identity({"name": "WR1"}) is None
    assert zendure_config_device_identity("nope") is None


def test_template_placeholders_are_not_physical_device_identities():
    devices = [
        {"name": "WR1", "sn": "YOUR_SN"},
        {"name": "WR2", "sn": "YOUR_SN"},
    ]
    assert zendure_config_device_identity(devices[0]) is None
    assert find_duplicate_zendure_device_identities(devices) == []


def test_mqtt_device_without_serial_is_valid_when_device_id_unique():
    a = _telemetry_only_entry()
    a.pop("serial_number", None)
    b = _telemetry_only_entry()
    b["mqtt"]["device_id"] = "DIFFERENT"
    b.pop("serial_number", None)
    assert find_duplicate_zendure_device_identities([a, b]) == []


def test_duplicate_mqtt_device_id_is_reported():
    a = _telemetry_only_entry()
    a.pop("serial_number", None)
    b = _telemetry_only_entry()
    b.pop("serial_number", None)
    issues = find_duplicate_zendure_device_identities([a, b])
    assert [i["code"] for i in issues] == ["zendure_device_identity_duplicate"]
    assert issues[0]["severity"] == "error"
    assert "devices.0" in issues[0]["message"]
    assert "devices.1" in issues[0]["message"]


def test_local_sn_and_mqtt_serial_number_collide():
    local = {"name": "WR1", "ip": "10.0.0.1", "sn": "SHARED"}
    mqtt = _telemetry_only_entry()
    mqtt["serial_number"] = "shared"
    issues = find_duplicate_zendure_device_identities([local, mqtt])
    assert [i["code"] for i in issues] == ["zendure_device_identity_duplicate"]


def test_disabled_duplicate_is_ignored():
    a = {"name": "WR1", "sn": "SER1"}
    b = {"name": "WR2", "sn": "SER1", "enabled": False}
    assert find_duplicate_zendure_device_identities([a, b]) == []


def test_cloud_device_subscriptions_are_device_scoped_and_deduped():
    # The Zendure cloud broker never delivers the broad local wildcards; cloud
    # services subscribe exactly the per-device trees (Admin-discovery parity).
    def _dev(name, ref, product_key=None, device_id=None, serial=None, enabled=True):
        entry = {
            "type": ZENDURE_MQTT_TYPE,
            "enabled": enabled,
            "name": name,
            "mqtt": {"broker_ref": ref, "topic_family": "legacy_zendure_json"},
        }
        if product_key:
            entry["mqtt"]["product_key"] = product_key
        if device_id:
            entry["mqtt"]["device_id"] = device_id
        if serial:
            entry["serial_number"] = serial
        return entry

    devices = [
        _dev("A", "cloud", product_key="PK", device_id="D1"),
        _dev("B", "cloud", product_key="PK", serial="D2"),  # identifier from serial
        _dev("C", "other", product_key="PK", device_id="D3"),  # other broker
        _dev("D", "cloud", product_key="PK", device_id="D4", enabled=False),
        _dev("E", "cloud", device_id="D5"),  # no product key -> unaddressable
        {"name": "HTTP", "ip": "1.2.3.4", "sn": "S"},  # non-MQTT entry
    ]
    assert zendure_cloud_device_subscriptions(devices, "cloud") == (
        "/PK/D1/#",
        "iot/PK/D1/#",
        "/PK/D2/#",
        "iot/PK/D2/#",
    )
    assert zendure_cloud_device_subscriptions(devices, "missing") == ()
    assert zendure_cloud_device_subscriptions(None, "cloud") == ()


def test_duplicate_device_names_reported_across_transports():
    # The device name is the EMS runtime identity key: an API inverter and an
    # MQTT device (or two MQTT devices) must never share one.
    local = {"name": "Zendure MQTT SolarFlow 800 Pro2", "ip": "10.0.0.1", "sn": "S1"}
    mqtt = _telemetry_only_entry()
    mqtt["name"] = "Zendure MQTT SolarFlow 800 Pro2"
    issues = find_duplicate_device_names([local, mqtt])
    assert [i["code"] for i in issues] == ["device_name_duplicate"]
    assert issues[0]["severity"] == "error"
    assert "devices.0" in issues[0]["message"]
    assert "devices.1" in issues[0]["message"]


def test_unique_or_disabled_duplicate_names_produce_no_issues():
    a = {"name": "WR1", "sn": "S1"}
    b = {"name": "WR2", "sn": "S2"}
    disabled = {"name": "WR1", "sn": "S3", "enabled": False}
    assert find_duplicate_device_names([a, b, disabled]) == []
    assert duplicate_device_name_startup_error([a, b, disabled]) is None


def test_duplicate_name_startup_error_reports_count_only():
    a = {"name": "WR1", "sn": "S1"}
    b = {"name": "WR1", "sn": "S2"}
    assert duplicate_device_name_startup_error([a, b]) == {"duplicate_count": 1}


def test_duplicate_issue_output_has_no_secret_or_identity_values():
    a = _telemetry_only_entry()
    a["mqtt"]["password"] = "broker-pw"
    a["mqtt"]["app_key"] = "SECRET-APP-KEY"
    a.pop("serial_number", None)
    b = copy.deepcopy(a)
    text = " ".join(
        i["message"] for i in find_duplicate_zendure_device_identities([a, b])
    )
    assert "broker-pw" not in text
    assert "SECRET-APP-KEY" not in text
    assert "ABC123" not in text
    assert "abc123" not in text


def test_find_duplicates_ignores_non_object_and_non_list():
    assert find_duplicate_zendure_device_identities("nope") == []
    assert find_duplicate_zendure_device_identities([None, 3, {"name": "x"}]) == []


# --- startup abort -------------------------------------------------------


def test_startup_error_is_none_without_duplicates():
    devices = [{"name": "WR1", "sn": "A"}, {"name": "WR2", "sn": "B"}]
    assert duplicate_zendure_identity_startup_error(devices) is None


def test_startup_error_reports_count_on_duplicate():
    devices = [{"name": "WR1", "sn": "SHARED"}, {"name": "WR2", "sn": "shared"}]
    assert duplicate_zendure_identity_startup_error(devices) == {"duplicate_count": 1}


def test_startup_error_ignores_disabled_duplicate():
    devices = [
        {"name": "WR1", "sn": "SHARED"},
        {"name": "WR2", "sn": "SHARED", "enabled": False},
    ]
    assert duplicate_zendure_identity_startup_error(devices) is None


def test_startup_error_leaks_no_identifiers_or_secrets():
    a = _telemetry_only_entry()
    a["mqtt"]["password"] = "broker-pw"
    a["mqtt"]["app_key"] = "SECRET-APP-KEY"
    a.pop("serial_number", None)
    b = copy.deepcopy(a)
    error = duplicate_zendure_identity_startup_error([a, b])
    assert error == {"duplicate_count": 1}
    text = " ".join(str(value) for value in error.values())
    for leaked in ("broker-pw", "SECRET-APP-KEY", "ABC123", "abc123", "PK1"):
        assert leaked not in text


def test_duplicate_identity_still_blocked_across_different_broker_refs():
    # The same physical device configured against two brokers is still a
    # duplicate: identity is by serial, independent of broker_ref.
    devices = [
        {
            "type": ZENDURE_MQTT_TYPE,
            "name": "Cloud copy",
            "serial_number": "SHARED-SN",
            "mqtt": {
                "broker_ref": "zendure_cloud",
                "topic_family": "zensdk_ha_scalar",
                "device_id": "DEVA",
            },
        },
        {
            "type": ZENDURE_MQTT_TYPE,
            "name": "Local copy",
            "serial_number": "SHARED-SN",
            "mqtt": {
                "broker_ref": "local_mqtt",
                "topic_family": "zensdk_ha_scalar",
                "device_id": "DEVB",
            },
        },
    ]
    issues = find_duplicate_zendure_device_identities(devices)
    assert any(i["code"] == "zendure_device_identity_duplicate" for i in issues)


def test_unknown_broker_ref_is_a_validation_error():
    entry = _telemetry_only_entry()
    entry["mqtt"]["broker_ref"] = "ghost"
    issues = validate_zendure_mqtt_device_config(
        entry, known_broker_refs={"default", "local_mqtt"}
    )
    assert any(i["code"] == "broker_ref_unknown" and i["severity"] == "error" for i in issues)


def test_known_broker_ref_passes_validation():
    entry = _telemetry_only_entry()
    entry["mqtt"]["broker_ref"] = "local_mqtt"
    issues = validate_zendure_mqtt_device_config(
        entry, known_broker_refs={"default", "local_mqtt"}
    )
    assert issues == []


def test_missing_broker_ref_warns_when_brokers_defined():
    entry = _telemetry_only_entry()
    entry["mqtt"].pop("broker_ref", None)
    issues = validate_zendure_mqtt_device_config(entry, brokers_defined=True)
    assert any(i["code"] == "broker_ref_missing" and i["severity"] == "warning" for i in issues)


# --- broker-profile usability -------------------------------------------


def _local_broker_config(**overrides):
    profile = {"enabled": True, "source": "local_mqtt", "host": "10.0.0.5", "port": 1883}
    profile.update(overrides)
    return {"zendure_mqtt": {"enabled": True, "brokers": {"local_mqtt": profile}}}


def _mqtt_device(broker_ref="local_mqtt", **overrides):
    entry = _telemetry_only_entry()
    entry["mqtt"]["broker_ref"] = broker_ref
    entry.update(overrides)
    return entry


def _codes(issues):
    return {i["code"] for i in issues}


def test_enabled_device_with_usable_local_broker_has_no_issue():
    config = _local_broker_config()
    config["devices"] = [_mqtt_device("local_mqtt")]
    assert find_zendure_mqtt_broker_profile_issues(config) == []


def test_enabled_device_with_unknown_broker_ref_is_rejected():
    config = _local_broker_config()
    config["devices"] = [_mqtt_device("ghost")]
    issues = find_zendure_mqtt_broker_profile_issues(config)
    assert "zendure_mqtt_broker_ref_unknown" in _codes(issues)


def test_enabled_device_with_disabled_broker_ref_is_rejected():
    config = _local_broker_config(enabled=False)
    config["devices"] = [_mqtt_device("local_mqtt")]
    issues = find_zendure_mqtt_broker_profile_issues(config)
    assert "zendure_mqtt_broker_ref_disabled" in _codes(issues)


def test_enabled_device_with_blank_local_broker_host_is_rejected():
    config = _local_broker_config(host="")
    config["devices"] = [_mqtt_device("local_mqtt")]
    issues = find_zendure_mqtt_broker_profile_issues(config)
    assert "zendure_mqtt_broker_ref_incomplete" in _codes(issues)


def test_cloud_broker_without_auth_reference_is_rejected():
    config = {
        "zendure_mqtt": {
            "enabled": True,
            "brokers": {
                "zendure_cloud": {
                    "enabled": True,
                    "source": "zendure_cloud_mqtt",
                    "host": "mqtteu.zen-iot.com",
                    "port": 8883,
                }
            },
        },
        "devices": [_mqtt_device("zendure_cloud")],
    }
    issues = find_zendure_mqtt_broker_profile_issues(config)
    assert "zendure_mqtt_broker_auth_missing" in _codes(issues)


def test_cloud_broker_with_credentials_ref_is_accepted():
    config = {
        "zendure_mqtt": {
            "enabled": True,
            "brokers": {
                "zendure_cloud": {
                    "enabled": True,
                    "source": "zendure_cloud_mqtt",
                    "host": "mqtteu.zen-iot.com",
                    "port": 8883,
                    "credentials_ref": "zendure-cloud",
                }
            },
        },
        "devices": [_mqtt_device("zendure_cloud")],
    }
    assert find_zendure_mqtt_broker_profile_issues(config) == []


def test_disabled_broker_profile_without_referencing_device_is_allowed():
    config = _local_broker_config(enabled=False)
    config["devices"] = [{"name": "WR1", "ip": "10.0.0.1", "sn": "SER1"}]
    assert find_zendure_mqtt_broker_profile_issues(config) == []


def test_disabled_device_does_not_require_usable_broker():
    config = _local_broker_config(enabled=False)
    device = _mqtt_device("local_mqtt")
    device["enabled"] = False
    config["devices"] = [device]
    assert find_zendure_mqtt_broker_profile_issues(config) == []


def test_api_only_config_has_no_broker_issues():
    config = {"devices": [{"name": "WR1", "ip": "10.0.0.1", "sn": "SER1"}]}
    assert find_zendure_mqtt_broker_profile_issues(config) == []


def test_old_single_broker_default_config_is_compatible():
    config = {
        "zendure_mqtt": {"enabled": True, "host": "broker.local", "port": 1883},
        "devices": [_telemetry_only_entry()],  # no broker_ref -> implicit default
    }
    assert find_zendure_mqtt_broker_profile_issues(config) == []


def test_broker_issue_messages_leak_no_identifiers_or_secrets():
    device = _mqtt_device("ghost")
    device["mqtt"]["password"] = "broker-pw"
    device["mqtt"]["app_key"] = "SECRET-APP-KEY"
    config = _local_broker_config()
    config["devices"] = [device]
    text = " ".join(i["message"] for i in find_zendure_mqtt_broker_profile_issues(config))
    for leaked in ("broker-pw", "SECRET-APP-KEY", "ABC123", "10.0.0.5"):
        assert leaked not in text


def test_broker_profile_views_synthesize_default_for_single_broker():
    views = zendure_mqtt_broker_profile_views(
        {"enabled": True, "host": "broker.local", "port": 1883}
    )
    assert "default" in views
    assert views["default"].usable is True


def _control_entry(broker_ref="cloud", **mqtt):
    base = {
        "broker_ref": broker_ref,
        "topic_family": "legacy_zendure_json",
        "device_id": "DEV1",
        "product_key": "PK1",
    }
    base.update(mqtt)
    return {
        "type": ZENDURE_MQTT_TYPE,
        "name": "Ctrl",
        "mqtt": base,
        "capabilities": {"write_output_limit": True},
    }


def test_control_device_source_matching_broker_is_valid():
    sources = {"cloud": "zendure_cloud_mqtt"}
    entry = _control_entry("cloud", source="zendure_cloud_mqtt")
    issues = validate_zendure_mqtt_control_device_config(
        entry, known_broker_refs=sources.keys(), broker_sources=sources
    )
    assert "mqtt_source_mismatch" not in {i["code"] for i in issues}


def test_control_device_source_overriding_broker_is_rejected():
    sources = {"cloud": "zendure_cloud_mqtt"}
    entry = _control_entry("cloud", source="local_mqtt")
    issues = validate_zendure_mqtt_control_device_config(
        entry, known_broker_refs=sources.keys(), broker_sources=sources
    )
    mismatch = next(i for i in issues if i["code"] == "mqtt_source_mismatch")
    # The sanitized message names neither a host nor a credential.
    assert "cloud" in mismatch["message"]


def test_missing_device_source_never_mismatches():
    sources = {"cloud": "zendure_cloud_mqtt"}
    entry = _control_entry("cloud")  # no mqtt.source
    issues = validate_zendure_mqtt_control_device_config(
        entry, known_broker_refs=sources.keys(), broker_sources=sources
    )
    assert "mqtt_source_mismatch" not in {i["code"] for i in issues}


def test_source_mismatch_check_is_skipped_without_broker_sources():
    entry = _control_entry("cloud", source="local_mqtt")
    issues = validate_zendure_mqtt_control_device_config(entry)
    assert "mqtt_source_mismatch" not in {i["code"] for i in issues}


def test_has_enabled_mqtt_control_device_detects_active_entry():
    config = {"devices": [_control_entry("cloud")]}
    assert has_enabled_mqtt_control_device(config) is True


def test_has_enabled_mqtt_control_device_ignores_disabled_and_telemetry():
    disabled = _control_entry("cloud")
    disabled["enabled"] = False
    config = {"devices": [disabled, _telemetry_only_entry()]}
    assert has_enabled_mqtt_control_device(config) is False


def test_runtime_control_device_matches_api_and_enabled_mqtt_startup_inputs():
    assert has_runtime_control_device(
        {"devices": [{"name": "WR1", "ip": "10.0.0.1", "sn": "SN1"}]}
    ) is True
    assert has_runtime_control_device(
        {"devices": [_control_entry("cloud")]}
    ) is True


def test_runtime_control_device_rejects_telemetry_only_or_disabled_mqtt():
    disabled = _control_entry("cloud")
    disabled["enabled"] = False
    assert has_runtime_control_device(
        {"devices": [_telemetry_only_entry(), disabled]}
    ) is False
