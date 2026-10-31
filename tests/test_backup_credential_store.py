# SPDX-License-Identifier: AGPL-3.0-or-later
"""Config backups carry the credential store, and a restore keeps each record with its key.

``config/secrets/`` holds the MQTT passwords -- the built-in broker's accounts
among them -- encrypted with one key the installation generates for itself. A
restore that left them out made every device connected to the built-in broker
need a new password. A record decrypts with its own key only, so a backup from
another installation brings its key along, and keeping the key in place keeps
out every record it could not read.
"""

import json
import os
import stat

import pytest

from admin.credential_store import CredentialStore
from admin.backup_restore import BackupRestoreService
from admin.install_context import detect_install_context
from ems import backup
from ems.mqtt_credentials import SECRET_KEY_FILENAME, FileMqttCredentialResolver

pytestmark = [
    pytest.mark.backup_restore,
    pytest.mark.integration,
    pytest.mark.mqtt,
]

DEVICES = "mqtt-broker-devices"
METER = "mqtt-broker-meter"


def _install(tmp_path, name):
    root = tmp_path / name
    (root / "config").mkdir(parents=True)
    (root / "data" / "backups").mkdir(parents=True)
    config = {"system": {"runtime_state_path": "data/runtime-state.json"}, "devices": []}
    (root / "config" / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return root


def _store(root):
    return CredentialStore(root / "config")


def _secrets(root):
    return root / "config" / "secrets"


def _backup(root):
    config = json.loads((root / "config" / "config.json").read_text(encoding="utf-8"))
    return backup.create_config_backup(
        config,
        base_dir=str(root),
        config_path=str(root / "config" / "config.json"),
        backup_dir=str(root / "data" / "backups"),
    )


def _password(root, ref):
    secret = _store(root).load_mqtt_broker_secret(ref)
    return secret.password if secret is not None else None


def _actions(result):
    return {os.path.basename(action["path"]): action["action"] for action in result["actions"]}


@pytest.fixture
def source(tmp_path):
    root = _install(tmp_path, "source")
    store = _store(root)
    store.save_mqtt_broker_secret(DEVICES, "devices", "devices-password")
    store.save_mqtt_broker_secret(METER, "meter", "meter-password")
    store.save_mqtt_discovery_secret("ems-mqtt", "ems", "ems-password", label="Built-in MQTT broker")
    (root / "config" / "discovery-connections.json").write_text('{"schema_version": 1}', encoding="utf-8")
    return root


def test_a_config_backup_carries_the_key_every_record_and_the_discovery_list(source):
    (_secrets(source) / ".mqtt-x.json.1234.tmp").write_text("half written", encoding="utf-8")
    (_secrets(source) / "notes.txt").write_text("not a record", encoding="utf-8")

    manifest = backup.inspect_backup(_backup(source))["manifest"]

    files = {entry["path"]: entry for entry in manifest["files"]}
    stored = sorted(
        name for name in os.listdir(_secrets(source))
        if name == SECRET_KEY_FILENAME or name.endswith(".json") and not name.startswith(".")
    )
    assert len(stored) == 4
    for name in stored:
        entry = files[f"config/secrets/{name}"]
        assert entry["kind"] == backup.CREDENTIAL_STORE_KIND and entry["sensitive"] is True
    assert "config/secrets/notes.txt" not in files
    assert not any(path.endswith(".tmp") for path in files)
    assert files["config/discovery-connections.json"]["kind"] == "discovery_connections"


def test_a_fresh_installation_gets_every_password_back(source, tmp_path):
    archive = _backup(source)
    fresh = _install(tmp_path, "fresh")

    backup.restore_backup(archive, base_dir=str(fresh), on_conflict="replace")

    assert _password(fresh, DEVICES) == "devices-password"
    assert FileMqttCredentialResolver(_secrets(fresh)).resolve(METER).password == "meter-password"
    assert _store(fresh).load_mqtt_discovery_secret("ems-mqtt").password == "ems-password"
    assert stat.S_IMODE(os.stat(_secrets(fresh)).st_mode) == 0o700
    assert {stat.S_IMODE(path.stat().st_mode) for path in _secrets(fresh).iterdir()} == {0o600}


def test_a_restore_onto_its_own_installation_puts_a_changed_password_back(source):
    archive = _backup(source)
    _store(source).save_mqtt_broker_secret(DEVICES, "devices", "changed-since")

    result = backup.restore_backup(archive, base_dir=str(source), on_conflict="replace")

    assert _password(source, DEVICES) == "devices-password"
    assert _actions(result)[SECRET_KEY_FILENAME] == "skip_identical"


def test_a_backup_from_another_key_brings_its_key_along_with_its_records(source, tmp_path):
    archive = _backup(source)
    fresh = _install(tmp_path, "fresh")
    _store(fresh).save_mqtt_broker_secret(DEVICES, "devices", "fresh-password")

    backup.restore_backup(archive, base_dir=str(fresh), on_conflict="replace")

    assert (_secrets(fresh) / SECRET_KEY_FILENAME).read_bytes() == (_secrets(source) / SECRET_KEY_FILENAME).read_bytes()
    assert _password(fresh, DEVICES) == "devices-password"
    assert _password(fresh, METER) == "meter-password"


def test_keeping_the_key_in_place_keeps_out_every_record_it_could_not_read(source, tmp_path):
    archive = _backup(source)
    fresh = _install(tmp_path, "fresh")
    _store(fresh).save_mqtt_broker_secret(DEVICES, "devices", "fresh-password")

    result = backup.restore_backup(archive, base_dir=str(fresh), on_conflict="keep")

    actions = _actions(result)
    assert actions[SECRET_KEY_FILENAME] == "kept_current"
    assert actions[f"mqtt-{DEVICES}.json"] == "kept_current"
    assert actions[f"mqtt-{METER}.json"] == "skipped_other_key"
    assert _password(fresh, DEVICES) == "fresh-password"
    assert not (_secrets(fresh) / f"mqtt-{METER}.json").exists()


@pytest.mark.parametrize("decision", ["keep", "replace"])
def test_the_key_is_asked_about_once_and_its_records_follow(source, tmp_path, decision):
    archive = _backup(source)
    fresh = _install(tmp_path, "fresh")
    _store(fresh).save_mqtt_broker_secret(DEVICES, "devices", "fresh-password")
    asked = []

    def resolver(entry):
        asked.append(os.path.basename(entry["path"]))
        return decision

    backup.restore_backup(archive, base_dir=str(fresh), conflict_resolver=resolver)

    assert [name for name in asked if name.startswith(("mqtt-", "."))] == [SECRET_KEY_FILENAME]
    assert _password(fresh, DEVICES) == ("fresh-password" if decision == "keep" else "devices-password")


def test_the_admin_preview_says_which_records_a_kept_key_keeps_out(source, tmp_path):
    archive = _backup(source)
    fresh = _install(tmp_path, "fresh")
    os.replace(archive, fresh / "data" / "backups" / os.path.basename(archive))
    _store(fresh).save_mqtt_broker_secret(DEVICES, "devices", "fresh-password")
    service = BackupRestoreService(context_provider=lambda: detect_install_context(base_dir=str(fresh)))
    backup_id = service.list_backups()["backups"][0]["id"]

    plan = service.create_restore_plan({"id": backup_id, "scope": "config", "conflict_policy": "keep"})

    actions = {os.path.basename(row["path"]): row["action"] for row in plan["files"]}
    assert actions[SECRET_KEY_FILENAME] == "would_keep_current"
    assert actions[f"mqtt-{DEVICES}.json"] == "would_keep_current"
    assert actions[f"mqtt-{METER}.json"] == "would_skip_other_key"
