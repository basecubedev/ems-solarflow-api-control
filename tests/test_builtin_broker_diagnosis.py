# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the EMS says about the built-in broker it does not own, and about the mapping.

The EMS only reads: ``config/mqtt/broker.json`` for the switch and the mapping's
status file for its devices. Each finding is a warning with a stable code.
"""

import configparser
import json
import os
from pathlib import Path

import pytest

from appliance.config import (
    DEFAULT_MQTT_BROKER_CONTAINER,
    DEFAULT_MQTT_MAPPING_CONTAINER,
    ApplianceConfig,
)
from admin.mqtt_broker import BROKER_SERVICE, MAPPING_SERVICE
from ems.backup import collect_config_backup_files
from ems.diagnostics import diagnose_builtin_mqtt
from ems.zendure_mqtt.builtin_broker import (
    BUILTIN_PROFILE_REF,
    broker_enabled,
    builtin_broker_root_causes,
    builtin_profile_users,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.mqtt,
]

ROOT = Path(__file__).resolve().parents[1]
USES_BROKER = {
    "devices": [
        {"name": "Balkon", "type": "external_mqtt", "mqtt": {"broker_ref": BUILTIN_PROFILE_REF}},
        {"name": "Off", "enabled": False, "type": "external_mqtt", "mqtt": {"broker_ref": BUILTIN_PROFILE_REF}},
        {"name": "Elsewhere", "type": "zendure_mqtt", "mqtt": {"broker_ref": "local"}},
    ],
    "grid_meter": {"type": "mqtt", "mqtt": {"broker_ref": BUILTIN_PROFILE_REF, "topic": "meter/power"}},
}


@pytest.fixture
def dirs(tmp_path):
    config_dir, data_dir = tmp_path / "config", tmp_path / "data"
    config_dir.mkdir()
    data_dir.mkdir()
    return config_dir, data_dir


def _broker(config_dir, **document):
    path = config_dir / "mqtt" / "broker.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, **document}), encoding="utf-8")


def _status(data_dir, devices, age=0):
    path = data_dir / "mqtt-mapping-status.json"
    path.write_text(json.dumps({"schema_version": 1, "devices": devices}), encoding="utf-8")
    if age:
        stamp = path.stat().st_mtime - age
        os.utime(path, (stamp, stamp))


def _codes(config, dirs):
    return [cause["code"] for cause in builtin_broker_root_causes(config, *dirs)]


def test_no_broker_file_is_off_and_an_unreadable_one_is_unknown(dirs):
    config_dir, _ = dirs
    assert broker_enabled(config_dir) is False

    (config_dir / "mqtt").mkdir()
    (config_dir / "mqtt" / "broker.json").write_text("{", encoding="utf-8")
    assert broker_enabled(config_dir) is None


def test_the_users_of_the_built_in_profile_are_named():
    assert builtin_profile_users(USES_BROKER) == ["Balkon", "grid meter"]


def test_a_stopped_broker_that_devices_use_is_a_warning(dirs):
    _broker(dirs[0], enabled=False)

    (cause,) = builtin_broker_root_causes(USES_BROKER, *dirs)

    assert cause["code"] == "mqtt_broker_stopped_in_use"
    assert cause["severity"] == "warning"
    assert "Balkon" in cause["message"] and "grid meter" in cause["message"]


def test_a_broker_nobody_uses_is_no_finding_on_or_off(dirs):
    assert _codes({}, dirs) == []
    _broker(dirs[0], enabled=True)
    assert _codes({}, dirs) == []


def test_a_silent_mapped_device_is_named_with_its_reason(dirs):
    _status(dirs[1], [
        {"id": "balkon", "enabled": True, "publishing": False, "reason": "stale"},
        {"id": "keller", "enabled": True, "publishing": True, "broker_ref": "iobroker"},
        {"id": "aus", "enabled": False, "publishing": False, "reason": "disabled"},
    ])

    (cause,) = builtin_broker_root_causes({}, *dirs)

    assert cause["code"] == "mqtt_mapping_device_silent"
    assert cause["message"] == "Not published: balkon (stale)."


def test_a_mapping_that_stopped_reporting_is_one_finding(dirs):
    _status(dirs[1], [{"id": "balkon", "enabled": True, "publishing": True}], age=600)

    (cause,) = builtin_broker_root_causes({}, *dirs)

    assert cause["code"] == "mqtt_mapping_device_silent"
    assert "10 minutes" in cause["message"]


def test_published_devices_the_ems_has_no_profile_for_are_a_warning(dirs):
    _broker(dirs[0], enabled=True)
    _status(dirs[1], [{"id": "balkon", "enabled": True, "publishing": True, "broker_ref": BUILTIN_PROFILE_REF}])

    assert _codes({}, dirs) == ["mqtt_broker_profile_missing"]
    with_profile = {"zendure_mqtt": {"brokers": {BUILTIN_PROFILE_REF: {"host": "192.0.2.1"}}}}
    assert _codes(with_profile, dirs) == []


def test_the_diagnosis_turns_each_finding_into_a_warning_and_a_root_cause(dirs):
    config_dir, data_dir = dirs
    _broker(config_dir, enabled=False)
    checks = []

    report = diagnose_builtin_mqtt(
        checks, USES_BROKER, str(config_dir / "config.json"), str(data_dir / "runtime-state.json")
    )

    assert report["enabled"] is False
    assert [cause["code"] for cause in report["root_causes"]] == ["mqtt_broker_stopped_in_use"]
    assert [(check["section"], check["level"], check["code"]) for check in checks] == [
        ("config", "warning", "mqtt_broker_stopped_in_use")
    ]


def test_a_config_backup_carries_the_broker_and_mapping_files(dirs):
    config_dir, _ = dirs
    (config_dir / "config.json").write_text("{}", encoding="utf-8")
    _broker(config_dir, enabled=True)
    (config_dir / "mqtt" / "mappings.json").write_text('{"schema_version": 1, "devices": []}', encoding="utf-8")

    included, _skipped = collect_config_backup_files(
        {}, base_dir=str(config_dir.parent), config_path=str(config_dir / "config.json")
    )

    kinds = {entry["kind"]: entry for entry in included}
    assert kinds["mqtt_broker_config"]["arcname"] == "config/mqtt/broker.json"
    assert kinds["mqtt_mapping_config"]["arcname"] == "config/mqtt/mappings.json"
    assert not kinds["mqtt_broker_config"]["sensitive"]


def test_the_appliance_watches_the_containers_the_admin_starts():
    shipped = configparser.ConfigParser()
    shipped.read(ROOT / "packaging" / "appliance" / "config" / "appliance.conf", encoding="utf-8")

    assert (DEFAULT_MQTT_BROKER_CONTAINER, DEFAULT_MQTT_MAPPING_CONTAINER) == (BROKER_SERVICE, MAPPING_SERVICE)
    assert ApplianceConfig().managed_containers[-2:] == (BROKER_SERVICE, MAPPING_SERVICE)
    assert shipped["appliance"]["mqtt_broker_container"] == BROKER_SERVICE
    assert shipped["appliance"]["mqtt_mapping_container"] == MAPPING_SERVICE


def test_a_profile_discovery_minted_counts_as_the_built_in_one_by_its_account():
    config = {
        "zendure_mqtt": {"brokers": {"local_mqtt_x": {"host": "192.0.2.10", "credentials_ref": "ems-mqtt"}}},
        "devices": [{"name": "Balkon", "type": "external_mqtt", "mqtt": {"broker_ref": "local_mqtt_x"}}],
    }

    assert builtin_profile_users(config) == ["Balkon"]
