# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Switches the config spells as text become what the EMS already read from them.

Backlog 51: ``"allow_hardware_writes": "false"`` closed the gate in the EMS
while the Admin's editor showed it switched on. The owner's call (2026-11-25):
no reading in the Admin, the file itself holds true or false. The EMS rewrites
such a switch at start, so that it does exactly what it did before: a control
flag on its blocking side, a text simulation_mode with the dry run it forced,
the plain words every reader agrees on; anything else stays as written.
"""

import json
import logging
from types import SimpleNamespace

import pytest

from ems import config as cfg
from ems.spelled_switches import STRICT_SWITCH_PATHS, WORD_SWITCH_PATHS, normalize_spelled_switches, switch_paths

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.config,
    pytest.mark.unit,
]


def _system(**values):
    return {"system": dict(values)}


@pytest.mark.parametrize(
    ("name", "stored", "written"),
    [
        ("allow_hardware_writes", "false", False),
        ("allow_hardware_writes", "true", False),
        ("allow_mqtt_local_control_writes", "on", False),
        ("allow_state_reconciliation_writes", 1, False),
        ("enabled", "true", False),
        ("dry_run", "false", True),
        ("reconcile_smart_mode", None, False),
    ],
)
def test_a_control_flag_spelled_otherwise_is_written_on_its_blocking_side(name, stored, written):
    config, changes = normalize_spelled_switches(_system(**{name: stored}))

    assert config["system"][name] is written
    assert changes == [{"path": f"system.{name}", "from": stored, "to": written}]


def test_a_text_simulation_mode_keeps_the_dry_run_it_forced():
    config, changes = normalize_spelled_switches(_system(simulation_mode="true", dry_run=False))

    assert (config["system"]["simulation_mode"], config["system"]["dry_run"]) == (False, True)
    assert changes == [
        {"path": "system.simulation_mode", "from": "true", "to": False},
        {"path": "system.dry_run", "from": False, "to": True},
    ]


@pytest.mark.parametrize(
    ("stored", "written"),
    [("false", False), (" OFF ", False), ("no", False), ("0", False), (0, False),
     ("true", True), ("On", True), ("yes", True), ("1", True), (1, True)],
)
def test_the_words_every_reader_agrees_on_become_booleans(stored, written):
    config, changes = normalize_spelled_switches({"winter": {"enabled": stored}, "energy_savings": {"enabled": stored}})

    assert config["winter"]["enabled"] is written
    assert config["energy_savings"]["enabled"] is written
    assert len(changes) == 2


@pytest.mark.parametrize("stored", ["enabled", "disabled", "maybe", "", 2, 1.0, [], {}])
def test_a_value_the_readers_read_differently_stays_as_written(stored):
    original = {"winter": {"enabled": stored}, "zendure_mqtt": {"tls": "false", "tls_insecure": "false"}}

    config, changes = normalize_spelled_switches(original)

    assert config == original
    assert changes == []


def test_booleans_missing_switches_and_other_values_are_left_alone():
    original = {
        "system": {"allow_hardware_writes": True, "dry_run": False, "loop_interval": "5"},
        "winter": {"months": "10,11"},
        "devices": [{"name": "INV_1", "enabled": True}, {"name": "INV_2"}, {"name": "INV_3", "enabled": None}, "x"],
    }

    config, changes = normalize_spelled_switches(original)

    assert config == original and changes == []


@pytest.mark.parametrize("stored", ["false", "true", "on", 1, 0, "", [], {}])
def test_a_device_switched_on_or_off_as_anything_but_a_boolean_is_written_off(stored):
    """Backlog 59: the EMS builds a device only for ``"enabled": true``, the Admin showed ``"true"`` and ``1`` on."""

    from ems.zendure_mqtt.config_entries import config_entry_enabled

    original = {"devices": [{"name": "INV_1", "enabled": True}, {"name": "INV_2", "enabled": stored}]}

    config, changes = normalize_spelled_switches(original)

    assert config_entry_enabled(original["devices"][1]) is False
    assert config["devices"] == [{"name": "INV_1", "enabled": True}, {"name": "INV_2", "enabled": False}]
    assert changes == [{"path": "devices[1].enabled", "from": stored, "to": False}]


def test_the_input_is_never_changed_in_place():
    original = _system(allow_hardware_writes="false")
    normalize_spelled_switches(original)
    assert original == _system(allow_hardware_writes="false")


def test_every_catalogue_switch_is_placed_once():
    """A switch the catalogue gains stays as written until it is placed here."""

    flags = {f"system.{name}" for name in cfg.CONTROL_FLAG_BLOCKING_SIDE}
    paths = switch_paths()
    assert not any("[]" in path or path.startswith(("devices", "grid_meter")) for path in paths)
    assert not (flags & WORD_SWITCH_PATHS) and not (flags & STRICT_SWITCH_PATHS)
    assert not (WORD_SWITCH_PATHS & STRICT_SWITCH_PATHS)
    assert paths == flags | WORD_SWITCH_PATHS | STRICT_SWITCH_PATHS


def test_a_switch_the_ems_reads_strictly_stays_so_a_refused_broker_stays_refused():
    from ems.config import resolve_mqtt_tls_metadata

    for stored in ("false", "off", 0, 1):
        with pytest.raises(ValueError):
            resolve_mqtt_tls_metadata(tls=stored)
        config, changes = normalize_spelled_switches({"zendure_mqtt": {"tls": stored, "tls_insecure": stored}})
        assert config["zendure_mqtt"] == {"tls": stored, "tls_insecure": stored} and changes == []


def _loaded(tmp_path, config):
    data = cfg.default_runtime_config()
    for section, values in config.items():
        if isinstance(values, list):
            data[section] = values
        else:
            data.setdefault(section, {}).update(values)
    data["config_upgrade"] = {"on_startup": "disabled"}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    args = SimpleNamespace(config=str(path), dry_run=False, simulate=False, replay=None, self_test=False)
    snapshot = {name: getattr(cfg, name) for name in dir(cfg) if name.isupper()}
    try:
        cfg.initialize(args, str(tmp_path))
        return {
            "write": cfg.resolve_write_gate("api").allowed,
            "dry_run": cfg.DRY_RUN,
            "simulation": cfg.SIMULATION_MODE,
            "enabled": cfg.SYSTEM_ENABLED,
            "kwp": cfg.PV_KWP_WEIGHTING,
            "redistribute": cfg.REDISTRIBUTE_CLAMPED_POWER,
            "devices": [item["name"] for item in cfg.http_control_device_configs()],
        }
    finally:
        for name, value in snapshot.items():
            setattr(cfg, name, value)


@pytest.mark.parametrize(
    "spelled",
    [
        {"system": {"allow_hardware_writes": "false", "dry_run": False}},
        {"system": {"allow_hardware_writes": "true", "dry_run": False}},
        {"system": {"simulation_mode": "true", "dry_run": False}},
        {"system": {"simulation_mode": "false", "dry_run": False}},
        {"system": {"dry_run": "false"}},
        {"system": {"enabled": "true", "dry_run": False}},
        {"system": {"pv_kwp_weighting": "off", "redistribute_clamped_power": "no", "dry_run": False}},
        {"system": {"dry_run": False}, "devices": [{"name": "INV_1", "ip": "192.0.2.5", "sn": "A", "enabled": "true"}]},
    ],
)
def test_the_ems_reads_the_rewritten_config_as_it_read_the_spelled_one(tmp_path, spelled):
    before = _loaded(tmp_path, spelled)
    normalized, changes = normalize_spelled_switches(spelled)

    assert changes
    assert _loaded(tmp_path, normalized) == before


def _startup(tmp_path, system, backups):
    data = cfg.default_runtime_config()
    data["system"].update(system)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data, indent=2))
    messages = []
    written = cfg.normalize_startup_config_switches(
        data,
        str(path),
        str(tmp_path),
        backup=True,
        backup_factory=lambda *args, **kwargs: backups.append(args) or str(tmp_path / "backup.tar.gz"),
        emit_message=lambda level, text: messages.append((level, text)),
    )
    return path, written, messages


def test_the_ems_writes_the_booleans_at_start_behind_a_backup_and_says_so(tmp_path):
    backups = []
    path, written, messages = _startup(tmp_path, {"allow_hardware_writes": "false", "simulation_mode": "true"}, backups)

    stored = json.loads(path.read_text())
    assert (stored["system"]["allow_hardware_writes"], stored["system"]["simulation_mode"], stored["system"]["dry_run"]) == (
        False, False, True,
    )
    assert written["system"]["allow_hardware_writes"] is False
    assert len(backups) == 1
    assert any(level == logging.WARNING and 'system.allow_hardware_writes: "false" -> false' in text for level, text in messages)


def test_a_device_switched_on_as_text_is_written_off_at_start(tmp_path):
    data = cfg.default_runtime_config()
    data["devices"] = [{"name": "INV_1", "ip": "192.0.2.5", "sn": "A", "enabled": "true"}]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data, indent=2))
    messages = []

    written = cfg.normalize_startup_config_switches(
        data, str(path), str(tmp_path), backup=False, backup_factory=None,
        emit_message=lambda level, text: messages.append(text),
    )

    assert json.loads(path.read_text())["devices"][0]["enabled"] is False
    assert written["devices"][0]["enabled"] is False
    assert any('devices[0].enabled: "true" -> false' in text for text in messages)


def test_a_config_with_only_booleans_is_not_written(tmp_path):
    backups = []
    data = cfg.default_runtime_config()
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data, indent=2))
    before = path.stat().st_mtime_ns, path.read_text()
    messages = []

    cfg.normalize_startup_config_switches(
        data, str(path), str(tmp_path), backup=True,
        backup_factory=lambda *args, **kwargs: backups.append(args),
        emit_message=lambda level, text: messages.append(text),
    )

    assert (path.stat().st_mtime_ns, path.read_text()) == before
    assert backups == [] and messages == []


def test_a_file_that_is_no_longer_what_the_ems_read_is_left_alone(tmp_path):
    data = cfg.default_runtime_config()
    data["system"]["allow_hardware_writes"] = "false"
    path = tmp_path / "config.json"
    path.write_text(json.dumps({**data, "system": {**data["system"], "loop_interval": 7}}, indent=2))
    before = path.read_text()
    messages = []

    returned = cfg.normalize_startup_config_switches(
        data, str(path), str(tmp_path), backup=False, backup_factory=None,
        emit_message=lambda level, text: messages.append(text),
    )

    assert returned is data and path.read_text() == before
    assert any("changed while the EMS started" in text for text in messages)


def test_a_file_written_while_the_backup_runs_is_left_alone(tmp_path):
    data = cfg.default_runtime_config()
    data["system"]["allow_hardware_writes"] = "false"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data, indent=2))
    meanwhile = json.dumps({**data, "system": {**data["system"], "loop_interval": 7}}, indent=2)

    def backup_while_someone_writes(*args):
        path.write_text(meanwhile)
        return str(tmp_path / "backup.tar.gz")

    cfg.normalize_startup_config_switches(
        data, str(path), str(tmp_path), backup=True, backup_factory=backup_while_someone_writes,
        emit_message=lambda level, text: None,
    )

    assert path.read_text() == meanwhile


def test_the_rewrite_keeps_the_file_s_own_layout(tmp_path):
    data = cfg.default_runtime_config()
    data["system"]["allow_hardware_writes"] = "false"
    data["devices"] = [{"name": "INV_1", "ip": "192.0.2.5", "sn": "A"}, {"sn": "B", "name": "INV_2", "ip": "192.0.2.6"}]
    path = tmp_path / "config.json"
    text = json.dumps(data, indent=2).replace('  "devices"', '\n  "devices"', 1)
    path.write_text(text)

    cfg.normalize_startup_config_switches(
        data, str(path), str(tmp_path), backup=False, backup_factory=None, emit_message=lambda level, text: None,
    )

    rewritten = path.read_text()
    ordered = json.loads(rewritten, object_pairs_hook=lambda pairs: pairs)
    devices = dict(ordered)["devices"]
    assert [key for key, _ in devices[1]] == ["sn", "name", "ip"]
    assert [key for key, _ in ordered] == list(data)
    assert '\n\n  "devices"' in rewritten
    assert json.loads(rewritten)["system"]["allow_hardware_writes"] is False


def _with_template(tmp_path):
    import shutil
    from pathlib import Path

    shutil.copy(Path(__file__).resolve().parents[1] / "config" / "config.template.json", tmp_path / "config.template.json")


def test_a_live_start_rewrites_and_a_dry_or_test_start_leaves_the_file_alone(tmp_path):
    _with_template(tmp_path)
    data = cfg.default_runtime_config()
    data["system"].update({"dry_run": False, "allow_hardware_writes": "false"})
    data["config_upgrade"] = {"on_startup": "check", "backup_before_apply": False}
    path = tmp_path / "config.json"
    text = json.dumps(data, indent=2)
    messages = []

    for refresh in (False, True):
        path.write_text(text)
        cfg.perform_startup_config_upgrade(
            json.loads(text), str(path), str(tmp_path), refresh_defaults=refresh,
            emit_message=lambda level, message: messages.append(message),
        )
        stored = json.loads(path.read_text())["system"]["allow_hardware_writes"]
        assert stored == (False if refresh else "false")


def test_a_start_whose_defaults_refresh_did_not_run_rewrites_nothing(tmp_path):
    """The refresh reads a text "true" as set by hand; rewritten first, a later
    refresh would take the boolean for the shipped default and move it."""

    data = cfg.default_runtime_config()
    data.pop(cfg.CONFIG_REVISION_KEY, None)
    data["system"]["dry_run"] = False
    data["system"]["output_control"].update(
        {"device_ramp_enabled": "true", "ramp_enabled": True, "device_ramp_up_w": 400, "device_ramp_down_w": 200}
    )
    data["config_upgrade"] = {"on_startup": "check", "backup_before_apply": False}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data, indent=2))

    def start():
        cfg.perform_startup_config_upgrade(
            json.loads(path.read_text()), str(path), str(tmp_path), refresh_defaults=True,
            emit_message=lambda level, message: None,
        )
        return json.loads(path.read_text())

    without_template = start()
    assert without_template["system"]["output_control"]["device_ramp_enabled"] == "true"
    assert cfg.CONFIG_REVISION_KEY not in without_template

    _with_template(tmp_path)
    with_template = start()
    assert with_template["system"]["output_control"]["device_ramp_enabled"] is True
    assert with_template[cfg.CONFIG_REVISION_KEY] == cfg.DEFAULTS_REVISION
