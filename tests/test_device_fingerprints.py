# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The fingerprints the EMS keeps of the device entries it read, and writes into its snapshot."""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from admin.ems_device_states import control_status_path, ems_device_states
from ems import config as cfg
from ems.control_status import ControlStatusWriter
from ems.device_fingerprints import FINGERPRINT_VERSION, device_fingerprints, loaded_devices_record

pytestmark = [pytest.mark.unit, pytest.mark.mqtt, pytest.mark.admin]

REPO = Path(__file__).resolve().parents[1]
BALKON = {"type": "external_mqtt", "name": "Balkon", "enabled": True, "mqtt": {"broker_ref": "ems-mqtt", "topic_family": "ems_solarflow", "device_id": "balkon"}}
ZENDURE = {"name": "WR1", "ip": "192.0.2.10", "sn": "SN1"}


def test_each_entry_gets_its_name_its_mapping_id_and_a_digest_of_the_whole_entry():
    prints = device_fingerprints({"devices": [BALKON, ZENDURE, "not a device", {**ZENDURE, "name": "WR2", "enabled": False}]})

    assert [(item["name"], item["device_id"], item["enabled"]) for item in prints] == [("Balkon", "balkon", True), ("WR1", None, True), ("WR2", None, False)]
    assert all(len(item["digest"]) == 16 for item in prints)


def test_the_digest_ignores_key_order_and_changes_with_any_value_a_new_name_and_broker_included():
    digest = device_fingerprints({"devices": [BALKON]})[0]["digest"]
    reordered = dict(reversed(list(BALKON.items())))

    assert device_fingerprints({"devices": [reordered]})[0]["digest"] == digest
    assert device_fingerprints({"devices": [{**BALKON, "name": "Balkon Süd"}]})[0]["digest"] != digest
    assert device_fingerprints({"devices": [{**BALKON, "mqtt": {**BALKON["mqtt"], "broker_ref": "other"}}]})[0]["digest"] != digest


def test_switched_on_and_external_are_read_as_the_ems_reads_them():
    prints = device_fingerprints({"devices": [{**BALKON, "enabled": "false"}, {**BALKON, "enabled": 0}, {**BALKON, "type": " External_MQTT "}]})

    assert [item["enabled"] for item in prints] == [False, False, True]
    assert prints[2]["device_id"] == "balkon"


def test_any_change_to_an_entry_gives_another_digest_a_switch_included():
    digest = device_fingerprints({"devices": [BALKON]})[0]["digest"]

    for changed in ({**BALKON, "enabled": False}, {**BALKON, "max_power": 600}, {**BALKON, "name": "Balkon Nord"}):
        assert device_fingerprints({"devices": [changed]})[0]["digest"] != digest


def test_the_digest_rule_is_the_one_its_version_names():
    """A reader of another release compares only fingerprints of the same version, so a new rule needs a new FINGERPRINT_VERSION."""

    assert FINGERPRINT_VERSION == 1
    assert device_fingerprints({"devices": [BALKON]})[0]["digest"] == "9582f8435e53cdd8"


def test_any_text_in_an_entry_gives_a_digest():
    assert len(device_fingerprints({"devices": [{**BALKON, "name": "Balkon \ud800 Süd"}]})[0]["digest"]) == 16


def test_a_config_without_devices_has_no_fingerprints():
    assert device_fingerprints({}) == []
    assert device_fingerprints({"devices": "none"}) == []
    assert device_fingerprints(None) == []


def test_loading_the_config_keeps_the_fingerprints_of_the_file_as_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "LOADED_DEVICES", [])
    path = tmp_path / "config.json"
    saved = {"config_upgrade": {"on_startup": "disabled"}, "system": {}, "devices": [BALKON, ZENDURE]}
    path.write_text(json.dumps(saved))
    args = SimpleNamespace(config=str(path), dry_run=False, simulate=False, replay=None, self_test=False)

    cfg.load_config(args, str(REPO))

    assert cfg.LOADED_DEVICES == device_fingerprints(saved)


def test_loading_keeps_the_fingerprints_of_the_file_the_startup_upgrade_wrote(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "LOADED_DEVICES", [])
    shutil.copy(REPO / "config" / "config.template.json", tmp_path / "config.template.json")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"config_upgrade": {"on_startup": "apply"}, "system": {}, "devices": [BALKON, ZENDURE]}))
    args = SimpleNamespace(config=str(path), dry_run=False, simulate=False, replay=None, self_test=False)

    cfg.load_config(args, str(tmp_path))

    upgraded = json.loads(path.read_text())
    assert set(upgraded) > {"config_upgrade", "system", "devices"}
    assert cfg.LOADED_DEVICES == device_fingerprints(upgraded)


def test_an_ems_that_loaded_the_file_as_saved_reads_every_device_as_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "LOADED_DEVICES", [])
    path = tmp_path / "config.json"
    saved = {"config_upgrade": {"on_startup": "disabled"}, "system": {"runtime_state_path": "data/runtime-state.json"}, "devices": [BALKON, ZENDURE]}
    path.write_text(json.dumps(saved))
    cfg.load_config(SimpleNamespace(config=str(path), dry_run=False, simulate=False, replay=None, self_test=False), str(REPO))
    context = SimpleNamespace(install_root=tmp_path, config_path=path, config_exists=True)
    writer = ControlStatusWriter(control_status_path(context, saved), loaded_devices=cfg.LOADED_DEVICES)
    controller = SimpleNamespace(
        last_control_explanation=None,
        runtime_system_int=lambda name, default, minimum=None: 5,
        last_fetch_at=None,
        cycle_failures=0,
        shelly=None,
        devices=[],
    )

    assert writer.write(controller)
    answer = ems_device_states(context)

    assert answer["devices"] == {"balkon": {"state": "running", "change": None, "name": "Balkon", "ran": "Balkon"}}
    assert (answer["known"], answer["reason"]) == (True, None)
    assert answer["started_at"] == writer.started_at


def test_the_snapshot_carries_the_fingerprints_the_writer_was_given(tmp_path):
    prints = device_fingerprints({"devices": [BALKON]})
    writer = ControlStatusWriter(tmp_path / "control-status.json", loaded_devices=prints)
    controller = SimpleNamespace(
        last_control_explanation=None,
        runtime_system_int=lambda name, default, minimum=None: 5,
        last_fetch_at=None,
        cycle_failures=0,
        shelly=None,
        devices=[],
    )

    assert writer.write(controller, now=1_800_000_000.0, now_monotonic=100.0)
    snapshot = json.loads(writer.path.read_text())
    assert snapshot["loaded_devices"] == loaded_devices_record(prints)
    assert snapshot["started_at"] == writer.started_at


def test_a_writer_given_no_fingerprints_names_no_devices_rather_than_none(tmp_path):
    writer = ControlStatusWriter(tmp_path / "control-status.json")
    controller = SimpleNamespace(
        last_control_explanation=None,
        runtime_system_int=lambda name, default, minimum=None: 5,
        last_fetch_at=None,
        cycle_failures=0,
        shelly=None,
        devices=[],
    )

    assert writer.write(controller, now=1_800_000_000.0, now_monotonic=100.0)
    assert "loaded_devices" not in json.loads(writer.path.read_text())
