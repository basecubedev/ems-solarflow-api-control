# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import copy
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from ems import config as cfg
from ems.config import (
    CONFIG_UPGRADE_DEFAULTS,
    DASHBOARD_DEFAULTS,
    ENERGY_SAVINGS_DEFAULTS,
    OUTPUT_CONTROL_DEFAULTS,
    WINTER_DEFAULTS,
)

ROOT = Path(__file__).resolve().parents[1]


def without_comment_keys(values):
    return {
        key: value
        for key, value in values.items()
        if not key.startswith("_comment")
    }


def snapshot_config_module():
    names = [
        name for name in dir(cfg)
        if name.isupper() or name in ("ARGS", "BASE_DIR", "CONFIG")
    ]
    return {name: getattr(cfg, name) for name in names}


def restore_config_module(snapshot):
    for name, value in snapshot.items():
        setattr(cfg, name, value)


def test_config_template_output_control_matches_code_defaults():
    template = json.loads(Path("config.template.json").read_text())

    assert (
        without_comment_keys(template["system"]["output_control"])
        == OUTPUT_CONTROL_DEFAULTS
    )


def test_config_template_winter_matches_code_defaults():
    template = json.loads(Path("config.template.json").read_text())

    assert without_comment_keys(template["winter"]) == WINTER_DEFAULTS


def test_config_template_dashboard_matches_code_defaults():
    template = json.loads(Path("config.template.json").read_text())

    assert without_comment_keys(template["dashboard"]) == DASHBOARD_DEFAULTS


def test_dashboard_defaults_include_session_and_log_settings():
    assert DASHBOARD_DEFAULTS["session_idle_timeout_seconds"] == 1800
    assert DASHBOARD_DEFAULTS["session_absolute_max_seconds"] == 43200
    assert DASHBOARD_DEFAULTS["log_buffer_lines"] == 5000
    assert DASHBOARD_DEFAULTS["log_redaction"] is False


def test_dashboard_animation_mode_default_and_normalization():
    assert DASHBOARD_DEFAULTS["animation_mode"] == "normal"
    # Valid values pass through (case/space-insensitive); invalid -> normal.
    assert cfg.normalize_dashboard_config({"animation_mode": "reduced"})["animation_mode"] == "reduced"
    assert cfg.normalize_dashboard_config({"animation_mode": " OFF "})["animation_mode"] == "off"
    assert cfg.normalize_dashboard_config({"animation_mode": "bogus"})["animation_mode"] == "normal"
    assert cfg.normalize_dashboard_config({})["animation_mode"] == "normal"


def test_config_template_energy_savings_matches_code_defaults():
    template = json.loads(Path("config.template.json").read_text())

    assert without_comment_keys(template["energy_savings"]) == ENERGY_SAVINGS_DEFAULTS


def test_config_template_standalone_live_control_defaults():
    template = json.loads(Path("config.template.json").read_text())

    assert template["ha"]["enabled"] is False
    assert template["ha"]["control_enabled"] is False
    assert template["grid_meter"]["type"] == "shelly"
    assert isinstance(template["grid_meter"]["ip"], str)
    assert template["grid_meter"]["ip"]
    # The deprecated top-level "shelly" block has been removed; the grid meter
    # is configured solely under grid_meter.
    assert "shelly" not in template
    removed_key = "chan" + "nel"
    assert removed_key not in template["grid_meter"]
    assert template["system"]["dry_run"] is False
    assert template["system"]["allow_hardware_writes"] is True
    assert template["system"]["allow_state_reconciliation_writes"] is True
    assert template["system"]["reconcile_ac_mode_on_start"] is True
    assert template["system"]["reconcile_smart_mode"] is True


def test_config_template_uses_persisted_data_paths():
    template = json.loads(Path("config.template.json").read_text())

    docker_only_flag = "_config" + "_initialized"
    assert docker_only_flag not in json.dumps(template)
    assert template["system"]["runtime_state_path"] == "data/runtime-state.json"
    assert template["dashboard"]["database_path"] == "data/ems_dashboard.sqlite"


def test_config_template_contains_startup_upgrade_defaults():
    template = json.loads(Path("config.template.json").read_text())

    assert without_comment_keys(template["config_upgrade"]) == CONFIG_UPGRADE_DEFAULTS


def test_config_template_comments_are_readable_values():
    template = json.loads(Path("config.template.json").read_text())
    failures = []

    def walk(value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                child_path = f"{path}.{key}" if path else key
                if key.startswith("_comment"):
                    if isinstance(item, str):
                        comment_lines = [item]
                    elif (
                        isinstance(item, list)
                        and item
                        and all(isinstance(line, str) for line in item)
                    ):
                        comment_lines = item
                    else:
                        failures.append(f"{child_path}: invalid comment value")
                        continue
                    too_long = [line for line in comment_lines if len(line) > 88]
                    if too_long:
                        failures.append(f"{child_path}: line too long")
                walk(item, child_path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(template, "")
    assert failures == []


def test_config_renderer_keeps_comment_arrays_multiline():
    layout = cfg._extract_template_layout(
        '{\n'
        '  "_comment": [\n'
        '    "Short sentence one.",\n'
        '    "Short sentence two."\n'
        '  ],\n'
        '  "months": [1, 2, 3]\n'
        '}'
    )
    text = cfg.render_config_json(
        {
            "_comment": ["Short sentence one.", "Short sentence two."],
            "months": [1, 2, 3],
        },
        layout,
    )

    assert (
        '  "_comment": [\n'
        '    "Short sentence one.",\n'
        '    "Short sentence two."\n'
        '  ],'
    ) in text
    assert '  "months": [1, 2, 3]' in text


def base_minimal_config():
    return {
        "ha": {
            "enabled": False,
            "control_enabled": False,
            "url": "",
            "token": "",
        },
        "system": {
            "enabled": True,
            "dry_run": False,
            "simulation_mode": False,
            "allow_hardware_writes": True,
            "allow_state_reconciliation_writes": True,
            "reconcile_ac_mode_on_start": True,
            "reconcile_smart_mode": True,
            "max_total_power": 800,
            "max_device_power": 800,
            "deadband": 10,
            "loop_interval": 5,
        },
        "devices": [],
        "shelly": {
            "ip": "192.168.1.50",
        },
    }


def minimal_upgrade_config():
    return {
        "_comment": "keep user top comment",
        "ha": {
            "_comment": "keep user ha comment",
            "enabled": True,
            "custom_ha_key": "kept",
        },
        "system": {
            "enabled": True,
            "dry_run": False,
            "max_total_power": 1234,
            "max_device_power": 900,
            "deadband": 10,
            "loop_interval": 5,
        },
        "devices": [
            {
                "name": "USER",
                "ip": "192.0.2.20",
                "sn": "USER_SN",
            }
        ],
        "shelly": {
            "ip": "192.168.1.50",
        },
        "unknown_top": {
            "nested": True,
        },
    }


def upgrade(values):
    return cfg.build_config_upgrade_plan(values)["upgraded_config"]


def test_config_template_upgrade_preserves_existing_comments():
    user = minimal_upgrade_config()

    result = upgrade(user)

    assert result["_comment"] == "keep user top comment"
    assert result["ha"]["_comment"] == "keep user ha comment"


def test_config_template_upgrade_adds_missing_template_comments():
    result = upgrade(minimal_upgrade_config())

    assert "_comment_docs" in result
    assert "_comment" in result["dashboard"]
    assert "_comment_animation_mode" in result["dashboard"]


def test_config_template_upgrade_does_not_add_sample_devices():
    user = minimal_upgrade_config()
    user.pop("devices")

    result = upgrade(user)

    assert result["devices"] == []


def test_config_template_upgrade_preserves_user_devices():
    user = minimal_upgrade_config()

    result = upgrade(user)

    assert result["devices"][0]["name"] == user["devices"][0]["name"]
    assert result["devices"][0]["ip"] == user["devices"][0]["ip"]
    assert result["devices"][0]["sn"] == user["devices"][0]["sn"]


def test_config_template_upgrade_preserves_user_values():
    result = upgrade(minimal_upgrade_config())

    assert result["ha"]["enabled"] is True
    assert result["system"]["max_total_power"] == 1234
    assert result["system"]["dry_run"] is False


def test_config_template_upgrade_preserves_unknown_keys():
    result = upgrade(minimal_upgrade_config())

    assert result["unknown_top"] == {"nested": True}
    assert result["ha"]["custom_ha_key"] == "kept"


def test_config_template_upgrade_adds_missing_template_keys():
    result = upgrade(minimal_upgrade_config())

    assert result["dashboard"]["animation_mode"] == "normal"
    assert result["influxdb"]["raw_write_interval_seconds"] == 0
    assert result["battery_full_charge_assist"]["ac_charge_power"] == 600


def test_config_template_upgrade_uses_legacy_shelly_ip_for_grid_meter():
    result = upgrade(minimal_upgrade_config())

    assert result["grid_meter"]["type"] == "shelly"
    assert result["grid_meter"]["ip"] == "192.168.1.50"
    assert "_comment" in result["grid_meter"]


def test_config_template_upgrade_sets_schema_version():
    result = upgrade(minimal_upgrade_config())

    assert result["config_schema_version"] == cfg.LATEST_CONFIG_SCHEMA_VERSION


def test_config_template_upgrade_is_idempotent():
    result = upgrade(minimal_upgrade_config())
    second_plan = cfg.build_config_upgrade_plan(result)

    assert second_plan["changed"] is False
    assert second_plan["upgraded_config"] == result


def test_config_template_upgrade_adds_device_defaults():
    user = minimal_upgrade_config()
    user["devices"][0].pop("max_power", None)

    plan = cfg.build_config_upgrade_plan(user)
    result = plan["upgraded_config"]

    assert result["devices"][0]["max_power"] == 800
    assert any(item["path"] == "devices[0].max_power" for item in plan["add"])


def test_config_template_upgrade_adds_device_comments():
    user = minimal_upgrade_config()
    user["devices"][0].pop("_comment_smart_mode", None)
    user["devices"][0].pop("_comment_soc", None)

    plan = cfg.build_config_upgrade_plan(user)
    result = plan["upgraded_config"]

    assert "_comment_smart_mode" in result["devices"][0]
    assert "_comment_soc" in result["devices"][0]
    paths = {item["path"] for item in plan["comment_add"]}
    assert "devices[0]._comment_smart_mode" in paths
    assert "devices[0]._comment_soc" in paths


def test_config_template_upgrade_never_overwrites_device_identity():
    user = minimal_upgrade_config()
    user["devices"][0].update({
        "name": "REAL",
        "ip": "192.0.2.44",
        "sn": "REAL_SN",
    })

    result = upgrade(user)

    assert result["devices"][0]["name"] == "REAL"
    assert result["devices"][0]["ip"] == "192.0.2.44"
    assert result["devices"][0]["sn"] == "REAL_SN"


def test_config_template_upgrade_does_not_fill_missing_device_identity():
    user = minimal_upgrade_config()
    user["devices"] = [{"max_power": 500}]

    result = upgrade(user)

    assert "name" not in result["devices"][0]
    assert "ip" not in result["devices"][0]
    assert "sn" not in result["devices"][0]
    assert result["devices"][0]["max_power"] == 500


def test_config_template_upgrade_preserves_unknown_device_keys():
    user = minimal_upgrade_config()
    user["devices"][0]["custom_device_key"] = "kept"

    result = upgrade(user)

    assert result["devices"][0]["custom_device_key"] == "kept"


def test_config_template_upgrade_preserves_invalid_device_items():
    user = minimal_upgrade_config()
    user["devices"] = ["invalid"]

    result = upgrade(user)

    assert result["devices"] == ["invalid"]


def test_config_upgrade_render_uses_device_template_layout():
    user = minimal_upgrade_config()
    user["devices"][0].update({
        "pv_kwp": 2.5,
        "battery_kwh": 1.92,
        "custom_device_key": "kept",
    })
    user["devices"][0].pop("_comment_smart_mode", None)
    user["devices"][0].pop("_comment_soc", None)

    plan = cfg.build_config_upgrade_plan(user)
    text = cfg.render_config_json(
        plan["upgraded_config"],
        plan["template_layout"],
    )
    parsed = json.loads(text)
    device = parsed["devices"][0]

    assert device["pv_kwp"] == 2.5
    assert device["battery_kwh"] == 1.92
    assert device["custom_device_key"] == "kept"
    assert (
        '      "sn": "USER_SN",\n\n'
        '      "_comment_smart_mode": [\n'
        '        "Use smart_mode=1 for runtime/RAM mode.",'
    ) in text
    assert (
        '      "battery_kwh": 1.92,\n\n'
        '      "_comment_soc": [\n'
        '        "Battery SOC limits in percent.",'
    ) in text
    assert text.index('"custom_device_key"') > text.index('"max_soc"')


def test_config_upgrade_render_does_not_fill_missing_device_identity():
    user = minimal_upgrade_config()
    user["devices"] = [{"max_power": 500}]

    plan = cfg.build_config_upgrade_plan(user)
    text = cfg.render_config_json(
        plan["upgraded_config"],
        plan["template_layout"],
    )
    device = json.loads(text)["devices"][0]

    assert "name" not in device
    assert "ip" not in device
    assert "sn" not in device
    assert "YOUR_SN" not in text


def test_config_upgrade_render_blank_lines_are_template_driven(tmp_path):
    template_text = Path("config.template.json").read_text()
    template_text = template_text.replace(
        '      "max_power": 800,\n'
        '      "pv_kwp": 1.0,',
        '      "max_power": 800,\n\n'
        '      "pv_kwp": 1.0,',
        1,
    )
    (tmp_path / "config.template.json").write_text(template_text)
    user = minimal_upgrade_config()
    user["devices"][0]["pv_kwp"] = 2.5

    plan = cfg.build_config_upgrade_plan(user, base_dir=str(tmp_path))
    text = cfg.render_config_json(
        plan["upgraded_config"],
        plan["template_layout"],
    )

    assert '      "max_power": 800,\n\n      "pv_kwp": 2.5,' in text
    assert json.loads(text)["devices"][0]["pv_kwp"] == 2.5


def test_config_upgrade_render_uses_first_template_device_shape(tmp_path):
    needle = (
        '      "max_power": 800,\n'
        '      "pv_kwp": 1.0,'
    )
    template_text = Path("config.template.json").read_text()
    first = template_text.index(needle)
    second = template_text.index(needle, first + len(needle))
    template_text = (
        template_text[:second]
        + '      "max_power": 800,\n\n'
        '      "pv_kwp": 1.0,'
        + template_text[second + len(needle):]
    )
    (tmp_path / "config.template.json").write_text(template_text)
    user = minimal_upgrade_config()
    user["devices"] = [
        {"name": "A", "ip": "192.0.2.1", "sn": "A_SN", "pv_kwp": 1.5},
        {"name": "B", "ip": "192.0.2.2", "sn": "B_SN", "pv_kwp": 2.5},
    ]

    plan = cfg.build_config_upgrade_plan(user, base_dir=str(tmp_path))
    text = cfg.render_config_json(
        plan["upgraded_config"],
        plan["template_layout"],
    )

    assert '      "max_power": 800,\n      "pv_kwp": 2.5,' in text
    assert '      "max_power": 800,\n\n      "pv_kwp": 2.5,' not in text


def test_missing_config_schema_version_is_treated_as_schema_1():
    assert cfg.read_config_schema_version(minimal_upgrade_config()) == 1


def test_config_schema_migrations_run_serially():
    calls = []

    def one_to_two(config, changes):
        calls.append((1, 2))
        config["one"] = True
        return config

    def two_to_three(config, changes):
        calls.append((2, 3))
        config["two"] = config["one"]
        return config

    result, steps = cfg.run_config_schema_migrations(
        {"system": {}, "devices": []},
        migrations={
            (1, 2): ("one", one_to_two),
            (2, 3): ("two", two_to_three),
        },
        latest_schema=3,
    )

    assert calls == [(1, 2), (2, 3)]
    assert [(step["from"], step["to"]) for step in steps] == [(1, 2), (2, 3)]
    assert result["config_schema_version"] == 3
    assert result["two"] is True


def test_config_schema_migration_starts_at_existing_schema():
    calls = []

    def two_to_three(config, changes):
        calls.append((2, 3))
        return config

    result, steps = cfg.run_config_schema_migrations(
        {"config_schema_version": 2},
        migrations={(2, 3): ("two", two_to_three)},
        latest_schema=3,
    )

    assert calls == [(2, 3)]
    assert [(step["from"], step["to"]) for step in steps] == [(2, 3)]
    assert result["config_schema_version"] == 3


def test_config_schema_latest_runs_no_schema_migrations():
    result, steps = cfg.run_config_schema_migrations(
        {"config_schema_version": 3, "user": "kept"},
        migrations={},
        latest_schema=3,
    )

    assert steps == []
    assert result["user"] == "kept"
    assert result["config_schema_version"] == 3


def test_config_schema_future_version_aborts():
    with pytest.raises(cfg.ConfigUpgradeError, match="newer EMS version"):
        cfg.run_config_schema_migrations(
            {"config_schema_version": 999},
            latest_schema=3,
        )


def test_config_schema_missing_intermediate_migration_aborts():
    with pytest.raises(cfg.ConfigUpgradeError, match="missing config schema migration"):
        cfg.run_config_schema_migrations(
            {"config_schema_version": 1},
            migrations={(2, 3): ("two", lambda config, changes: config)},
            latest_schema=3,
        )


def test_existing_values_change_only_with_explicit_migration():
    def two_to_three(config, changes):
        old = config["battery_full_charge_assist"]["ac_charge_power"]
        config["battery_full_charge_assist"]["ac_charge_power"] = 600
        changes.append({
            "path": "battery_full_charge_assist.ac_charge_power",
            "old_value": old,
            "value": 600,
        })
        return config

    user = minimal_upgrade_config()
    user["config_schema_version"] = 2
    user["battery_full_charge_assist"] = {"ac_charge_power": 200}

    plan = cfg.build_config_upgrade_plan(
        user,
        migrations={(2, 3): ("battery assist", two_to_three)},
        latest_schema=3,
    )

    assert plan["upgraded_config"]["battery_full_charge_assist"]["ac_charge_power"] == 600
    assert plan["schema_migrations"][0]["changes"] == [{
        "path": "battery_full_charge_assist.ac_charge_power",
        "old_value": 200,
        "value": 600,
    }]


def test_write_config_json_atomic_preserves_existing_permissions(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{}")
    path.chmod(0o600)

    cfg.write_config_json_atomic(str(path), {"ok": True})

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"ok": True}


def test_write_config_json_atomic_creates_restrictive_file(tmp_path):
    path = tmp_path / "config.json"

    cfg.write_config_json_atomic(str(path), {"ok": True})

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"ok": True}


def initialize_config_from_dict(tmp_path, values):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(values))
    args = SimpleNamespace(
        config=str(config_path),
        dry_run=False,
        simulate=False,
        replay=None,
        self_test=False,
        no_ha=False,
    )
    cfg.initialize(args, str(tmp_path))


def test_grid_meter_defaults_to_shelly_compatible_safe_config():
    safe_config = cfg.default_safe_config()

    assert safe_config["grid_meter"] == {"type": "shelly", "ip": ""}
    assert safe_config["shelly"] == {"ip": ""}


def test_legacy_shelly_ip_fallback_populates_grid_meter(tmp_path):
    snapshot = snapshot_config_module()
    values = base_minimal_config()
    values.pop("grid_meter", None)
    values["shelly"]["ip"] = "192.168.1.51"

    try:
        initialize_config_from_dict(tmp_path, values)

        assert cfg.GRID_METER_CONFIG == {
            "type": "shelly",
            "ip": "192.168.1.51",
        }
        assert cfg.SHELLY_IP == "192.168.1.51"
    finally:
        restore_config_module(snapshot)


def test_runtime_load_applies_conservative_missing_defaults_in_memory(tmp_path):
    snapshot = snapshot_config_module()
    values = {
        "system": {
            "enabled": True,
            "max_total_power": 800,
            "max_device_power": 800,
            "deadband": 10,
        },
        "devices": [],
        "shelly": {"ip": "192.168.1.77"},
    }

    try:
        initialize_config_from_dict(tmp_path, values)

        assert cfg.DRY_RUN is True
        assert cfg.SIMULATION_MODE is False
        assert cfg.ALLOW_HARDWARE_WRITES is False
        assert cfg.ALLOW_STATE_RECONCILIATION_WRITES is False
        assert cfg.LOOP_INTERVAL == 5
        assert cfg.GRID_METER_CONFIG == {
            "type": "shelly",
            "ip": "192.168.1.77",
        }
    finally:
        restore_config_module(snapshot)


def test_runtime_load_does_not_rewrite_config_file(tmp_path):
    snapshot = snapshot_config_module()
    values = {
        "system": {
            "enabled": True,
            "max_total_power": 800,
            "max_device_power": 800,
            "deadband": 10,
        },
        "devices": [],
    }
    config_path = tmp_path / "config.json"
    original_text = json.dumps(values)
    config_path.write_text(original_text)
    args = SimpleNamespace(
        config=str(config_path),
        dry_run=False,
        simulate=False,
        replay=None,
        self_test=False,
        no_ha=False,
    )

    try:
        cfg.initialize(args, str(tmp_path))

        assert config_path.read_text() == original_text
    finally:
        restore_config_module(snapshot)


def prepare_startup_upgrade_fixture(tmp_path, values=None):
    shutil.copy(ROOT / "config.template.json", tmp_path / "config.template.json")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(values or minimal_upgrade_config()))
    return config_path


def test_startup_config_upgrade_check_reports_without_writing(tmp_path):
    config_path = prepare_startup_upgrade_fixture(tmp_path)
    original_text = config_path.read_text()
    messages = []

    raw = cfg.perform_startup_config_upgrade(
        json.loads(original_text),
        str(config_path),
        str(tmp_path),
        emit_message=lambda level, message: messages.append((level, message)),
    )

    assert raw == json.loads(original_text)
    assert config_path.read_text() == original_text
    assert any("Config upgrade available:" in message for _, message in messages)
    assert any(
        "emsctl.py config upgrade --dry-run" in message
        for _, message in messages
    )


def test_startup_config_upgrade_disabled_skips_check_and_write(tmp_path):
    values = minimal_upgrade_config()
    values["config_upgrade"] = {"on_startup": "disabled"}
    config_path = prepare_startup_upgrade_fixture(tmp_path, values)
    original_text = config_path.read_text()
    messages = []

    raw = cfg.perform_startup_config_upgrade(
        values,
        str(config_path),
        str(tmp_path),
        emit_message=lambda level, message: messages.append((level, message)),
    )

    assert raw == values
    assert config_path.read_text() == original_text
    assert messages == []


def test_startup_config_upgrade_apply_backs_up_writes_and_reloads(tmp_path):
    values = minimal_upgrade_config()
    values["config_upgrade"] = {
        "on_startup": "apply",
        "backup_before_apply": True,
        "backup_failure_policy": "continue_without_upgrade",
    }
    config_path = prepare_startup_upgrade_fixture(tmp_path, values)
    backups = []
    original_write = cfg.write_config_json_atomic

    def fake_backup(raw_config, path, base_dir):
        backups.append((copy.deepcopy(raw_config), path, base_dir))
        return str(tmp_path / "backup.tar.gz")

    def write_with_disk_marker(path, data, *, layout=None):
        marked = copy.deepcopy(data)
        marked["disk_reload_marker"] = "from-disk"
        original_write(path, marked, layout=layout)

    try:
        cfg.write_config_json_atomic = write_with_disk_marker
        raw = cfg.perform_startup_config_upgrade(
            values,
            str(config_path),
            str(tmp_path),
            backup_factory=fake_backup,
            emit_message=lambda level, message: None,
        )
    finally:
        cfg.write_config_json_atomic = original_write

    written = json.loads(config_path.read_text())
    assert backups
    assert written["config_schema_version"] == cfg.LATEST_CONFIG_SCHEMA_VERSION
    assert written["config_upgrade"]["on_startup"] == "apply"
    assert written["disk_reload_marker"] == "from-disk"
    assert raw["disk_reload_marker"] == "from-disk"


def test_startup_config_upgrade_apply_skips_when_backup_fails(tmp_path):
    values = minimal_upgrade_config()
    values["config_upgrade"] = {"on_startup": "apply"}
    config_path = prepare_startup_upgrade_fixture(tmp_path, values)
    original_text = config_path.read_text()
    messages = []

    def fail_backup(raw_config, path, base_dir):
        raise RuntimeError("backup boom")

    raw = cfg.perform_startup_config_upgrade(
        values,
        str(config_path),
        str(tmp_path),
        backup_factory=fail_backup,
        emit_message=lambda level, message: messages.append((level, message)),
    )

    assert raw == values
    assert config_path.read_text() == original_text
    assert any(
        "Config auto-upgrade skipped because backup failed" in message
        for _, message in messages
    )


def test_startup_config_upgrade_invalid_mode_falls_back_to_check(tmp_path):
    values = minimal_upgrade_config()
    values["config_upgrade"] = {"on_startup": "something-else"}
    config_path = prepare_startup_upgrade_fixture(tmp_path, values)
    original_text = config_path.read_text()
    messages = []

    raw = cfg.perform_startup_config_upgrade(
        values,
        str(config_path),
        str(tmp_path),
        emit_message=lambda level, message: messages.append((level, message)),
    )

    assert raw == values
    assert config_path.read_text() == original_text
    assert any(
        "Invalid config_upgrade.on_startup" in message
        for _, message in messages
    )
    assert any("Config upgrade available:" in message for _, message in messages)


def test_startup_config_upgrade_invalid_backup_policy_falls_back(tmp_path):
    values = minimal_upgrade_config()
    values["config_upgrade"] = {
        "on_startup": "apply",
        "backup_before_apply": True,
        "backup_failure_policy": "fail_startup",
    }
    config_path = prepare_startup_upgrade_fixture(tmp_path, values)
    messages = []

    raw = cfg.perform_startup_config_upgrade(
        values,
        str(config_path),
        str(tmp_path),
        backup_factory=lambda raw_config, path, base_dir: str(
            tmp_path / "backup.tar.gz"
        ),
        emit_message=lambda level, message: messages.append((level, message)),
    )

    assert raw["config_upgrade"]["backup_failure_policy"] == "fail_startup"
    assert json.loads(config_path.read_text())["config_schema_version"] == 3
    assert any(
        "Invalid config_upgrade.backup_failure_policy" in message
        for _, message in messages
    )


def test_runtime_load_preserves_user_values(tmp_path):
    snapshot = snapshot_config_module()
    values = base_minimal_config()
    values["system"]["dry_run"] = False
    values["system"]["allow_hardware_writes"] = True
    values["system"]["allow_state_reconciliation_writes"] = True

    try:
        initialize_config_from_dict(tmp_path, values)

        assert cfg.DRY_RUN is False
        assert cfg.ALLOW_HARDWARE_WRITES is True
        assert cfg.ALLOW_STATE_RECONCILIATION_WRITES is True
    finally:
        restore_config_module(snapshot)


def test_runtime_load_forces_safe_mode_for_template_placeholders(tmp_path, caplog):
    snapshot = snapshot_config_module()
    shutil.copy(ROOT / "config.template.json", tmp_path / "config.template.json")
    values = json.loads((ROOT / "config.template.json").read_text())

    try:
        initialize_config_from_dict(tmp_path, values)

        assert cfg.SYSTEM_ENABLED is False
        assert cfg.DRY_RUN is True
        assert cfg.ALLOW_HARDWARE_WRITES is False
        assert cfg.ALLOW_STATE_RECONCILIATION_WRITES is False
        assert "Config still contains template placeholder values" in caplog.text
        assert "devices[0].sn" in caplog.text
    finally:
        restore_config_module(snapshot)


def test_runtime_load_keeps_live_mode_after_required_values_are_configured(tmp_path):
    snapshot = snapshot_config_module()
    shutil.copy(ROOT / "config.template.json", tmp_path / "config.template.json")
    values = json.loads((ROOT / "config.template.json").read_text())
    values["grid_meter"]["ip"] = "192.0.2.50"
    values["devices"] = [{
        **values["devices"][0],
        "ip": "192.0.2.100",
        "sn": "REAL_SN",
    }]
    values["system"]["enabled"] = True
    values["system"]["dry_run"] = False
    values["system"]["allow_hardware_writes"] = True
    values["system"]["allow_state_reconciliation_writes"] = True

    try:
        initialize_config_from_dict(tmp_path, values)

        assert cfg.SYSTEM_ENABLED is True
        assert cfg.DRY_RUN is False
        assert cfg.ALLOW_HARDWARE_WRITES is True
        assert cfg.ALLOW_STATE_RECONCILIATION_WRITES is True
    finally:
        restore_config_module(snapshot)


def test_explicit_grid_meter_overrides_legacy_shelly(tmp_path):
    snapshot = snapshot_config_module()
    values = base_minimal_config()
    values["shelly"]["ip"] = "192.168.1.51"
    values["grid_meter"] = {
        "type": "ecotracker",
        "ip": "192.168.1.60",
    }

    try:
        initialize_config_from_dict(tmp_path, values)

        assert cfg.GRID_METER_CONFIG == {
            "type": "ecotracker",
            "ip": "192.168.1.60",
        }
        assert cfg.SHELLY_IP == "192.168.1.51"
    finally:
        restore_config_module(snapshot)


def test_shelly_grid_meter_config_preserves_channels(tmp_path):
    snapshot = snapshot_config_module()
    values = base_minimal_config()
    values["grid_meter"] = {
        "type": "shelly",
        "ip": "192.168.1.50",
        "channels": ["A", "C"],
    }

    try:
        initialize_config_from_dict(tmp_path, values)

        assert cfg.GRID_METER_CONFIG == {
            "type": "shelly",
            "ip": "192.168.1.50",
            "channels": ["a", "c"],
        }
    finally:
        restore_config_module(snapshot)


def test_shelly_3em_gen1_grid_meter_config_preserves_channels(tmp_path):
    snapshot = snapshot_config_module()
    values = base_minimal_config()
    values["grid_meter"] = {
        "type": "shelly_3em_gen1",
        "ip": "192.168.1.50",
        "channels": ["A", "C"],
    }

    try:
        initialize_config_from_dict(tmp_path, values)

        assert cfg.GRID_METER_CONFIG == {
            "type": "shelly_3em_gen1",
            "ip": "192.168.1.50",
            "channels": ["a", "c"],
        }
    finally:
        restore_config_module(snapshot)


def test_shelly_grid_meter_config_rejects_channels_string(tmp_path):
    snapshot = snapshot_config_module()
    values = base_minimal_config()
    values["grid_meter"] = {
        "type": "shelly",
        "ip": "192.168.1.50",
        "channels": "c",
    }

    try:
        with pytest.raises(ValueError, match="grid_meter.channels must be a list"):
            initialize_config_from_dict(tmp_path, values)
    finally:
        restore_config_module(snapshot)


def test_shelly_grid_meter_config_rejects_empty_channel_entry(tmp_path):
    snapshot = snapshot_config_module()
    values = base_minimal_config()
    values["grid_meter"] = {
        "type": "shelly",
        "ip": "192.168.1.50",
        "channels": ["c", ""],
    }

    try:
        with pytest.raises(
            ValueError,
            match="grid_meter.channels must not contain empty values",
        ):
            initialize_config_from_dict(tmp_path, values)
    finally:
        restore_config_module(snapshot)


def test_tasmota_grid_meter_config_preserves_url_ip_and_power_path(tmp_path):
    snapshot = snapshot_config_module()
    values = base_minimal_config()
    values["grid_meter"] = {
        "type": "tasmota_http",
        "url": "http://192.168.1.70/cm?cmnd=Status%2010",
        "ip": "192.168.1.71",
        "power_path": "StatusSNS.SM.16_7_0",
    }

    try:
        initialize_config_from_dict(tmp_path, values)

        assert cfg.GRID_METER_CONFIG == {
            "type": "tasmota_http",
            "url": "http://192.168.1.70/cm?cmnd=Status%2010",
            "ip": "192.168.1.71",
            "power_path": "StatusSNS.SM.16_7_0",
        }
    finally:
        restore_config_module(snapshot)


def test_omitted_ha_keys_default_to_disabled(tmp_path):
    snapshot = snapshot_config_module()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "ha": {
            "url": "http://homeassistant.local:8123",
            "token": "TOKEN"
        },
        "system": {
            "enabled": True,
            "dry_run": False,
            "simulation_mode": False,
            "allow_hardware_writes": True,
            "allow_state_reconciliation_writes": True,
            "reconcile_ac_mode_on_start": True,
            "reconcile_smart_mode": True,
            "max_total_power": 800,
            "max_device_power": 800,
            "deadband": 10,
            "loop_interval": 5
        },
        "devices": [],
        "shelly": {
            "ip": "192.168.1.50"
        }
    }))
    args = SimpleNamespace(
        config=str(config_path),
        dry_run=False,
        simulate=False,
        replay=None,
        self_test=False,
        no_ha=False
    )

    try:
        cfg.initialize(args, str(tmp_path))

        assert cfg.HA_ENABLED is False
        assert cfg.HA_CONTROL_ENABLED is False
        assert cfg.RECONCILE_AC_MODE_ON_START is True
        assert cfg.RECONCILE_SMART_MODE is True
    finally:
        restore_config_module(snapshot)


def test_omitted_ha_section_defaults_to_disabled(tmp_path):
    snapshot = snapshot_config_module()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "system": {
            "enabled": True,
            "dry_run": False,
            "simulation_mode": False,
            "allow_hardware_writes": True,
            "allow_state_reconciliation_writes": True,
            "reconcile_ac_mode_on_start": True,
            "reconcile_smart_mode": True,
            "max_total_power": 800,
            "max_device_power": 800,
            "deadband": 10,
            "loop_interval": 5
        },
        "devices": [],
        "shelly": {
            "ip": "192.168.1.50"
        }
    }))
    args = SimpleNamespace(
        config=str(config_path),
        dry_run=False,
        simulate=False,
        replay=None,
        self_test=False,
        no_ha=False
    )

    try:
        cfg.initialize(args, str(tmp_path))

        assert cfg.HA_ENABLED is False
        assert cfg.HA_CONTROL_ENABLED is False
        assert cfg.HA_URL == ""
        assert cfg.HA_TOKEN == ""
    finally:
        restore_config_module(snapshot)


def test_safe_session_timeout_parsing():
    # explicit positive value preserved
    assert cfg.safe_session_timeout(900, 1800) == 900
    assert cfg.safe_session_timeout("600", 1800) == 600
    # 0 is a deliberate "disabled / infinite" opt-in and is preserved
    assert cfg.safe_session_timeout(0, 1800) == 0
    # negative typo must fall back to the secure default, never silently disable
    assert cfg.safe_session_timeout(-5, 1800) == 1800
    # invalid / missing values fall back to the default
    assert cfg.safe_session_timeout("nope", 1800) == 1800
    assert cfg.safe_session_timeout(None, 43200) == 43200


def test_dashboard_config_missing_keys_fall_back_to_defaults(tmp_path):
    snapshot = snapshot_config_module()
    values = base_minimal_config()
    values["dashboard"] = {
        "enabled": True,
        "host": "127.0.0.1",
    }

    try:
        initialize_config_from_dict(tmp_path, values)

        assert cfg.DASHBOARD_CONFIG["host"] == "127.0.0.1"
        assert cfg.DASHBOARD_CONFIG["session_idle_timeout_seconds"] == 1800
        assert cfg.DASHBOARD_CONFIG["session_absolute_max_seconds"] == 43200
        assert cfg.DASHBOARD_CONFIG["log_buffer_lines"] == 5000
        assert cfg.DASHBOARD_CONFIG["log_redaction"] is False
    finally:
        restore_config_module(snapshot)


def test_dashboard_session_timeout_zero_is_accepted(tmp_path):
    snapshot = snapshot_config_module()
    values = base_minimal_config()
    values["dashboard"] = {
        "session_idle_timeout_seconds": 0,
        "session_absolute_max_seconds": 0,
    }

    try:
        initialize_config_from_dict(tmp_path, values)

        assert cfg.DASHBOARD_CONFIG["session_idle_timeout_seconds"] == 0
        assert cfg.DASHBOARD_CONFIG["session_absolute_max_seconds"] == 0
    finally:
        restore_config_module(snapshot)


def test_dashboard_negative_session_timeout_falls_back_to_defaults(tmp_path):
    snapshot = snapshot_config_module()
    values = base_minimal_config()
    values["dashboard"] = {
        "session_idle_timeout_seconds": -1,
        "session_absolute_max_seconds": -20,
    }

    try:
        initialize_config_from_dict(tmp_path, values)

        assert cfg.DASHBOARD_CONFIG["session_idle_timeout_seconds"] == 1800
        assert cfg.DASHBOARD_CONFIG["session_absolute_max_seconds"] == 43200
    finally:
        restore_config_module(snapshot)
