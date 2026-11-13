# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A value still at a default a later release replaced takes the new default.

Owner 2026-11-13: most installations never change their defaults, so when a
default turns out not to be good, the update should carry the new one to them;
a value someone changed stays. A file that still holds an old default was most
likely never touched, so the EMS refreshes it once, in config.json at start-up
and in runtime-state.json when it loads it, and the file then records the
revision: a value set afterwards, an old default among them, stays.
"""

import json
import logging
import shutil
from pathlib import Path

import pytest

import ems.config as cfg
from ems import config_catalog
from ems.retired_defaults import (
    CONFIG_REVISION_KEY,
    DEFAULTS_REVISION,
    RETIRED_DEFAULTS,
    RUNTIME_REVISION_KEY,
    refresh_retired_defaults,
    same_value,
)
from ems.runtime_state import RuntimeState

pytestmark = [
    pytest.mark.unit,
    pytest.mark.config,
]

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = json.loads((ROOT / "config" / "config.template.json").read_text())


def _at(data, path):
    for key in path.split("."):
        data = data[key]
    return data


def test_every_retired_default_is_a_tuning_value_the_template_has_replaced():
    for entry in RETIRED_DEFAULTS:
        current = _at(TEMPLATE, entry.path)
        assert entry.values and entry.shipped, entry.path
        assert 1 <= entry.revision <= DEFAULTS_REVISION, entry.path
        for retired in entry.values:
            assert not isinstance(retired, (bool, str)), entry.path
            assert not same_value(current, retired), entry.path
    assert TEMPLATE[CONFIG_REVISION_KEY] == DEFAULTS_REVISION
    assert config_catalog.build_default_template()[CONFIG_REVISION_KEY] == DEFAULTS_REVISION


def test_an_old_default_moves_and_a_changed_value_stays():
    data = {
        "ac_charge_control": {"max_total_charge_power_w": 1200.0},
        "battery_care": {"summer_full_points": 5, "winter_full_points": 4},
        "winter": {"months": [10, 11, 12, 1, 2], "ramp_step_percent": 4},
        "system": {"loop_interval": True},
    }

    refreshed, changes = refresh_retired_defaults(data, TEMPLATE, 0)

    assert refreshed["ac_charge_control"]["max_total_charge_power_w"] is None
    assert refreshed["battery_care"] == {"summer_full_points": 3, "winter_full_points": 4}
    assert refreshed["winter"] == {"months": [10, 11, 12, 1, 2, 3], "ramp_step_percent": 4}
    assert refreshed["system"]["loop_interval"] is True
    assert [change["path"] for change in changes] == [
        "winter.months",
        "ac_charge_control.max_total_charge_power_w",
        "battery_care.summer_full_points",
    ]
    assert data["ac_charge_control"]["max_total_charge_power_w"] == 1200.0
    assert refresh_retired_defaults(data, TEMPLATE, DEFAULTS_REVISION) == (data, [])


def _startup(tmp_path, values, **kwargs):
    shutil.copy(ROOT / "config" / "config.template.json", tmp_path / "config.template.json")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values))
    messages, backups = [], []

    def backup(raw, config_path, base_dir):
        backups.append(raw)
        return str(tmp_path / "backup.tar.gz")

    raw = cfg.perform_startup_config_upgrade(
        json.loads(path.read_text()),
        str(path),
        str(tmp_path),
        backup_factory=kwargs.pop("backup_factory", backup),
        emit_message=lambda level, message: messages.append((level, message)),
    )
    return raw, path, messages, backups


def _old_config(**extra):
    return {
        "config_schema_version": 3,
        "system": {"loop_interval": 3, "deadband": 10, "output_control": {"median_window": 4}},
        "battery_care": {"summer_full_points": 5, "winter_full_points": 10, "pv_only_points": 0},
        "ac_charge_control": {"max_total_charge_power_w": 1200},
        "devices": [],
        **extra,
    }


def test_the_start_refreshes_old_defaults_once_behind_a_backup_and_adds_no_key(tmp_path):
    raw, path, messages, backups = _startup(tmp_path, _old_config())

    written = json.loads(path.read_text())
    assert raw == written
    assert written[CONFIG_REVISION_KEY] == DEFAULTS_REVISION
    assert written["system"] == {"loop_interval": 5, "deadband": 2, "output_control": {"median_window": 4}}
    assert written["battery_care"] == {"summer_full_points": 3, "winter_full_points": 4, "pv_only_points": 1}
    assert written["ac_charge_control"] == {"max_total_charge_power_w": None}
    assert "dashboard" not in written
    assert len(backups) == 1
    refreshed = next(message for level, message in messages if "Config defaults refreshed" in message)
    assert "ac_charge_control.max_total_charge_power_w: 1200 -> null" in refreshed

    again = cfg.perform_startup_config_upgrade(
        written, str(path), str(tmp_path), emit_message=lambda level, message: messages.append(message)
    )
    assert again == written and json.loads(path.read_text()) == written


def test_a_value_set_after_the_refresh_stays_even_at_an_old_default(tmp_path):
    values = _old_config(**{CONFIG_REVISION_KEY: DEFAULTS_REVISION})
    raw, path, messages, backups = _startup(tmp_path, values)

    assert json.loads(path.read_text())["ac_charge_control"]["max_total_charge_power_w"] == 1200
    assert raw["battery_care"]["summer_full_points"] == 5
    assert backups == []


def test_a_config_without_an_old_default_only_records_the_revision(tmp_path):
    values = {"config_schema_version": 3, "system": {"loop_interval": 7}, "devices": []}
    raw, path, messages, backups = _startup(tmp_path, values)

    assert json.loads(path.read_text()) == {**values, CONFIG_REVISION_KEY: DEFAULTS_REVISION}
    assert backups == []
    assert not any("Config defaults refreshed" in message for _, message in messages)


def test_a_disabled_startup_check_and_a_newer_revision_leave_the_file_alone(tmp_path):
    disabled = _old_config(config_upgrade={"on_startup": "disabled"})
    raw, path, _, backups = _startup(tmp_path, disabled)
    assert json.loads(path.read_text()) == disabled == raw and backups == []

    newer = _old_config(**{CONFIG_REVISION_KEY: DEFAULTS_REVISION + 1})
    raw, path, _, backups = _startup(tmp_path, newer)
    assert json.loads(path.read_text())["ac_charge_control"]["max_total_charge_power_w"] == 1200


def test_a_failed_backup_or_write_starts_with_the_file_as_it_was(tmp_path, monkeypatch):
    def refused(*args, **kwargs):
        raise OSError("disk full")

    raw, path, messages, _ = _startup(tmp_path, _old_config(), backup_factory=refused)
    assert raw == _old_config() == json.loads(path.read_text())
    assert any(level == logging.WARNING and "backup failed" in message for level, message in messages)

    monkeypatch.setattr(cfg, "write_config_json_atomic", refused)
    raw, path, messages, _ = _startup(tmp_path, _old_config())
    assert raw == _old_config() == json.loads(path.read_text())
    assert any(level == logging.INFO and "could not be written" in message for level, message in messages)

    applying = _old_config(config_upgrade={"on_startup": "apply"})
    raw, path, messages, _ = _startup(tmp_path, applying)
    assert raw == applying == json.loads(path.read_text())


def test_the_upgrade_plan_lists_the_refresh_and_records_the_revision_without_calling_it_missing(tmp_path):
    shutil.copy(ROOT / "config" / "config.template.json", tmp_path / "config.template.json")

    plan = cfg.build_config_upgrade_plan(_old_config(), str(tmp_path))

    assert {"path": "system.loop_interval", "from": 3, "to": 5} in plan["refresh_defaults"]
    assert plan["upgraded_config"][CONFIG_REVISION_KEY] == DEFAULTS_REVISION
    assert plan["upgraded_config"]["system"]["output_control"]["median_window"] == 4
    assert CONFIG_REVISION_KEY not in [item["path"] for item in plan["add"]]


RUNTIME_DEFAULTS = {
    "system": {"enabled": True, "loop_interval": 5},
    "battery_care": {"enabled": True, "summer_full_points": 3, "winter_full_points": 4, "pv_only_points": 1,
                     "min_discharge_w": 50},
    "devices": {},
}
OLD_RUNTIME = {
    "system": {"enabled": False, "loop_interval": 3},
    "battery_care": {"enabled": True, "summer_full_points": 5, "winter_full_points": 4, "pv_only_points": 0},
    "devices": {},
}


def _runtime(tmp_path, stored, revision, defaults=RUNTIME_DEFAULTS):
    path = tmp_path / "runtime-state.json"
    if stored is not None:
        path.write_text(json.dumps(stored))
    state = RuntimeState(str(path), defaults, revision)
    state.load_or_create()
    return state, json.loads(path.read_text()) if path.exists() else None


def test_the_ems_moves_runtime_values_still_at_an_old_default_and_records_it(tmp_path, caplog):
    caplog.set_level(logging.INFO)

    state, saved = _runtime(tmp_path, OLD_RUNTIME, DEFAULTS_REVISION)

    assert saved[RUNTIME_REVISION_KEY] == DEFAULTS_REVISION
    assert saved["system"] == {"enabled": False, "loop_interval": 5}
    assert saved["battery_care"] == {**RUNTIME_DEFAULTS["battery_care"], "winter_full_points": 4}
    assert state.data == saved
    assert "event=runtime_default_refreshed" in caplog.text


@pytest.mark.parametrize(
    "stored, revision",
    [
        (OLD_RUNTIME, None),
        (OLD_RUNTIME, 0),
        ({**OLD_RUNTIME, RUNTIME_REVISION_KEY: DEFAULTS_REVISION}, DEFAULTS_REVISION),
    ],
    ids=["not the EMS start", "config not refreshed", "already refreshed"],
)
def test_a_runtime_state_is_refreshed_only_at_an_ems_start_for_a_refreshed_config_and_only_once(
    tmp_path, stored, revision
):
    _, saved = _runtime(tmp_path, stored, revision)

    assert saved == stored


def test_a_runtime_value_the_config_keeps_at_an_old_default_stays(tmp_path):
    kept = {**RUNTIME_DEFAULTS, "battery_care": {**RUNTIME_DEFAULTS["battery_care"], "summer_full_points": 5}}

    _, saved = _runtime(tmp_path, OLD_RUNTIME, DEFAULTS_REVISION, defaults=kept)

    assert saved["battery_care"]["summer_full_points"] == 5
    assert saved["battery_care"]["pv_only_points"] == 1


def test_a_new_runtime_state_records_the_config_s_revision(tmp_path):
    _, saved = _runtime(tmp_path, None, DEFAULTS_REVISION)

    assert saved[RUNTIME_REVISION_KEY] == DEFAULTS_REVISION


def test_the_dashboard_preview_names_the_old_defaults(tmp_path):
    from dashboard import maintenance

    shutil.copy(ROOT / "config" / "config.template.json", tmp_path / "config.template.json")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_old_config()))

    preview = maintenance.config_upgrade_plan(_old_config(), str(tmp_path), str(path))

    assert preview["refresh_default_count"] == 6
    assert {
        "kind": "refresh_default",
        "path": "ac_charge_control.max_total_charge_power_w",
        "old_value": 1200,
        "value": None,
    } in preview["items"]


def test_the_refresh_keeps_the_file_s_own_order_and_spacing(tmp_path):
    shutil.copy(ROOT / "config" / "config.template.json", tmp_path / "config.template.json")
    path = tmp_path / "config.json"
    path.write_text(
        '{\n  "devices": [],\n\n  "config_schema_version": 3,\n  "system": { "loop_interval": 3, "deadband": 7 }\n}\n'
    )

    cfg.perform_startup_config_upgrade(
        json.loads(path.read_text()), str(path), str(tmp_path),
        backup_factory=lambda *args: "backup", emit_message=lambda level, message: None,
    )

    assert path.read_text() == (
        '{\n  "devices": [],\n\n  "config_schema_version": 3,\n  "config_defaults_revision": 1,\n'
        '  "system": { "loop_interval": 5, "deadband": 7 }\n}\n'
    )


def _current_config_without_revision():
    current = json.loads((ROOT / "config" / "config.template.json").read_text())
    current.pop(CONFIG_REVISION_KEY)
    return current


@pytest.mark.parametrize("mode", ["check", "apply"])
def test_a_config_that_cannot_be_written_lacking_only_the_revision_starts_quietly(tmp_path, monkeypatch, mode):
    def refused(*args, **kwargs):
        raise OSError("read-only file system")

    values = _current_config_without_revision()
    values["config_upgrade"]["on_startup"] = mode
    monkeypatch.setattr(cfg, "write_config_json_atomic", refused)

    raw, path, messages, _ = _startup(tmp_path, values)

    assert raw == values
    assert not any("Config upgrade available" in message for _, message in messages)
    assert not any(level == logging.WARNING for level, _ in messages)
    assert cfg.build_config_upgrade_plan(values, str(tmp_path))["changed"] is False


def _loaded(tmp_path, values, **flags):
    from types import SimpleNamespace

    shutil.copy(ROOT / "config" / "config.template.json", tmp_path / "config.template.json")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values))
    args = SimpleNamespace(config=str(path), simulate=False, replay=None, self_test=False, dry_run=False, preflight=False)
    for key, value in flags.items():
        setattr(args, key, value)
    cfg.load_config(args, str(tmp_path))
    return json.loads(path.read_text())


def _live_config():
    values = _old_config()
    values["system"]["dry_run"] = False
    return values


def test_a_live_start_refreshes_the_config(tmp_path):
    written = _loaded(tmp_path, _live_config())

    assert written[CONFIG_REVISION_KEY] == DEFAULTS_REVISION
    assert written["ac_charge_control"]["max_total_charge_power_w"] is None


@pytest.mark.parametrize(
    "flags, system",
    [
        ({"simulate": True}, {}),
        ({"replay": "trace.jsonl"}, {}),
        ({"self_test": True}, {}),
        ({"dry_run": True}, {}),
        ({"preflight": True}, {}),
        ({}, {"dry_run": True}),
        ({}, {"dry_run": None}),
        ({}, {"dry_run": "garbage"}),
        ({}, {"simulation_mode": True}),
        ({}, {"simulation_mode": "maybe"}),
    ],
)
def test_a_check_or_test_start_leaves_the_config_alone(tmp_path, flags, system):
    values = _live_config()
    values["system"].update(system)

    assert _loaded(tmp_path, values, **flags) == values


def test_runtime_state_follows_the_config_s_revision_only_in_a_live_start(monkeypatch):
    monkeypatch.setattr(cfg, "CONFIG", {CONFIG_REVISION_KEY: DEFAULTS_REVISION})
    monkeypatch.setattr(cfg, "SIMULATION_MODE", False)
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    assert cfg.runtime_defaults_revision() == DEFAULTS_REVISION

    monkeypatch.setattr(cfg, "DRY_RUN", True)
    assert cfg.runtime_defaults_revision() is None


def test_an_apply_start_refreshes_old_defaults_and_adds_missing_keys(tmp_path):
    raw, path, messages, backups = _startup(tmp_path, _old_config(config_upgrade={"on_startup": "apply"}))

    written = json.loads(path.read_text())
    assert raw == written
    assert written[CONFIG_REVISION_KEY] == DEFAULTS_REVISION
    assert written["system"]["loop_interval"] == 5
    assert written["system"]["output_control"]["median_window"] == 4
    assert written["dashboard"]["port"] == TEMPLATE["dashboard"]["port"]
    assert len(backups) == 2


def test_a_plan_without_the_refresh_keeps_old_defaults_and_records_no_revision(tmp_path):
    shutil.copy(ROOT / "config" / "config.template.json", tmp_path / "config.template.json")

    plan = cfg.build_config_upgrade_plan(_old_config(), str(tmp_path), refresh_defaults=False)

    assert plan["refresh_defaults"] == []
    assert plan["upgraded_config"]["ac_charge_control"]["max_total_charge_power_w"] == 1200
    assert CONFIG_REVISION_KEY not in plan["upgraded_config"]


def test_the_runtime_refresh_covers_the_runtime_state_the_ems_builds(monkeypatch):
    from ems.retired_defaults import _lookup
    from ems.runtime_state import build_runtime_defaults

    monkeypatch.setattr(cfg, "CONFIG", {})
    runtime = build_runtime_defaults([])

    assert {entry.path for entry in RETIRED_DEFAULTS if _lookup(runtime, entry.path.split("."))[0]} == {
        "system.loop_interval",
        "battery_care.summer_full_points",
        "battery_care.winter_full_points",
        "battery_care.pv_only_points",
    }


def test_the_ems_start_hands_the_revision_to_its_runtime_state():
    main = (ROOT / "ems-solarflow-api-control.py").read_text(encoding="utf-8").split("def main():", 1)[1]
    call = main.split("start_runtime_state(", 1)[1].split("\n    )", 1)[0]

    assert "cfg.runtime_defaults_revision()" in call


def test_a_runtime_state_records_no_revision_beyond_what_this_release_knows(tmp_path):
    _, saved = _runtime(tmp_path, OLD_RUNTIME, DEFAULTS_REVISION + 1)

    assert saved[RUNTIME_REVISION_KEY] == DEFAULTS_REVISION
    assert saved["battery_care"]["summer_full_points"] == 3


def test_diagnose_does_not_miss_the_revision():
    from ems.diagnostics import diagnose_missing_template_keys

    without = {key: value for key, value in TEMPLATE.items() if key != CONFIG_REVISION_KEY}

    assert diagnose_missing_template_keys(without, TEMPLATE) == []


def test_a_config_the_ems_may_not_write_is_left_without_a_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "_config_writable", lambda path: False)

    raw, path, messages, backups = _startup(tmp_path, _old_config())

    assert raw == _old_config() == json.loads(path.read_text())
    assert backups == []
    assert any(level == logging.INFO and "not writable" in message for level, message in messages)


def test_a_config_written_by_someone_else_while_the_ems_starts_is_not_overwritten(tmp_path):
    admin = {"config_schema_version": 3, "system": {"loop_interval": 4}, "devices": []}

    def admin_writes_during_backup(raw, config_path, base_dir):
        Path(config_path).write_text(json.dumps(admin))
        return "backup"

    raw, path, messages, _ = _startup(tmp_path, _old_config(), backup_factory=admin_writes_during_backup)

    assert json.loads(path.read_text()) == admin
    assert raw == _old_config()
    assert any("changed while the EMS started" in message for _, message in messages)


def test_a_config_with_setup_placeholders_runs_dry_and_is_left_alone(tmp_path):
    values = _live_config()
    values["devices"] = [{"name": "WR1", "ip": "192.168.1.20", "sn": "YOUR_SN"}]

    assert _loaded(tmp_path, values) == values


def test_a_live_ems_reads_the_revision_its_config_records(tmp_path):
    from types import SimpleNamespace

    config = cfg.default_runtime_config()
    config["system"].update({"dry_run": False, "simulation_mode": False})
    config[CONFIG_REVISION_KEY] = DEFAULTS_REVISION
    shutil.copy(ROOT / "config" / "config.template.json", tmp_path / "config.template.json")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    args = SimpleNamespace(config=str(path), dry_run=False, simulate=False, replay=None, self_test=False)
    saved = {name: getattr(cfg, name) for name in dir(cfg) if name.isupper()}
    try:
        cfg.initialize(args, str(tmp_path))
        revision, dry = cfg.runtime_defaults_revision(), cfg.DRY_RUN
    finally:
        for name, value in saved.items():
            setattr(cfg, name, value)

    assert (dry, revision) == (False, DEFAULTS_REVISION)


def test_emsctl_records_the_config_s_revision_in_a_file_it_creates_and_refreshes_nothing(tmp_path):
    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps(OLD_RUNTIME))
    RuntimeState(str(path), RUNTIME_DEFAULTS, DEFAULTS_REVISION, refresh_defaults=False).load_or_create()
    assert json.loads(path.read_text()) == OLD_RUNTIME

    fresh = RuntimeState(str(tmp_path / "new.json"), RUNTIME_DEFAULTS, DEFAULTS_REVISION, refresh_defaults=False)
    assert fresh.data[RUNTIME_REVISION_KEY] == DEFAULTS_REVISION


def test_the_cleared_charge_limit_means_no_limit_now_and_the_old_default_after_a_rollback():
    assert cfg.installation_charge_limit_w(None) is None
    assert cfg.safe_int(None, 1200, minimum=0) == 1200


def test_a_template_older_than_this_ems_refreshes_and_records_nothing(tmp_path):
    stale = dict(TEMPLATE)
    stale.pop(CONFIG_REVISION_KEY)
    stale["ac_charge_control"] = {**TEMPLATE["ac_charge_control"], "max_total_charge_power_w": 1200}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_old_config()))
    (tmp_path / "config.template.json").write_text(json.dumps(stale))
    messages = []

    raw = cfg.perform_startup_config_upgrade(
        _old_config(), str(path), str(tmp_path), emit_message=lambda level, message: messages.append((level, message))
    )

    assert raw == _old_config() == json.loads(path.read_text())
    assert any(level == logging.WARNING and "older than this EMS" in message for level, message in messages)
    assert cfg.build_config_upgrade_plan(_old_config(), str(tmp_path))["refresh_defaults"] == []


def test_a_runtime_refresh_backs_the_file_up_first(tmp_path):
    _runtime(tmp_path, OLD_RUNTIME, DEFAULTS_REVISION)

    assert json.loads((tmp_path / "runtime-state.json.bak").read_text()) == OLD_RUNTIME


def test_a_revision_written_as_a_whole_float_counts():
    from ems.retired_defaults import stored_revision

    assert stored_revision({CONFIG_REVISION_KEY: 1.0}, CONFIG_REVISION_KEY) == 1
    assert stored_revision({CONFIG_REVISION_KEY: 1.5}, CONFIG_REVISION_KEY) == 0


def test_the_refresh_keeps_each_device_s_own_layout(tmp_path):
    text = (
        '{\n  "config_schema_version": 3,\n  "system": { "loop_interval": 3 },\n  "devices": [\n'
        '    { "name": "A", "sn": "X" },\n    {\n      "sn": "Y",\n\n      "name": "B"\n    }\n  ]\n}\n'
    )
    shutil.copy(ROOT / "config" / "config.template.json", tmp_path / "config.template.json")
    path = tmp_path / "config.json"
    path.write_text(text)

    cfg.perform_startup_config_upgrade(
        json.loads(text), str(path), str(tmp_path),
        backup_factory=lambda *args: "backup", emit_message=lambda level, message: None,
    )

    assert path.read_text() == (
        '{\n  "config_schema_version": 3,\n  "config_defaults_revision": 1,\n  "system": { "loop_interval": 5 },\n'
        '  "devices": [\n    { "name": "A", "sn": "X" },\n    {\n      "sn": "Y",\n\n      "name": "B"\n    }\n  ]\n}\n'
    )
