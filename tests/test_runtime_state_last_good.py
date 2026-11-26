# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The runtime state keeps a last good copy and falls back to it, or starts with control off.

Backlog 71: since the operating settings live in ``runtime-state.json`` alone,
a missing or unreadable file made the EMS run on the built-in defaults --
control on, every device allowed to discharge. The owner's call (2026-11-25):
the EMS keeps a last good copy and falls back to it by itself; where that copy
is unusable too, it starts with control off and says so.
"""

import hashlib
import json
import logging
import os

import pytest

from ems.runtime_state import (
    RuntimeState,
    last_good_runtime_state_path,
    request_runtime_reset,
    start_runtime_state,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.config,
    pytest.mark.power_control,
]

OPERATOR_STATE = {
    "system": {"enabled": True, "max_total_power": 600, "loop_interval": 5},
    "winter": {"enabled": False},
    "devices": {"INV_1": {"identity": "SN1", "enabled": False, "max_power": 300}},
}


def _defaults():
    return {
        "system": {"enabled": True, "max_total_power": 800, "loop_interval": 5},
        "winter": {"enabled": True},
        "ac_charge_control": {"enabled": True},
        "devices": {"INV_1": {"identity": "SN1", "enabled": True, "max_power": 800}},
    }


def _config(tmp_path, text='{"installed": true}\n'):
    config = tmp_path / "config.json"
    config.write_text(text, encoding="utf-8")
    return config, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _saved(tmp_path):
    """An installation whose runtime state the operator has saved once."""

    path = tmp_path / "runtime-state.json"
    state = RuntimeState(str(path), _defaults())
    state.load_or_create()
    state.data = json.loads(json.dumps(OPERATOR_STATE))
    state.save_atomic()
    return path


def _start(tmp_path, path, **kwargs):
    config, _digest = _config(tmp_path)
    return start_runtime_state(str(path), _defaults(), str(config), **kwargs)


def _unreadable(tmp_path):
    return sorted(tmp_path.glob("runtime-state.json.unreadable-*"))


def test_every_save_leaves_a_last_good_copy_of_what_it_wrote(tmp_path):
    path = _saved(tmp_path)

    copy = last_good_runtime_state_path(str(path))
    assert os.path.exists(copy)
    assert json.loads(open(copy).read()) == json.loads(path.read_text())


@pytest.mark.parametrize("broken", ["{not valid json", "", "[]", '"text"'])
def test_an_unreadable_file_falls_back_to_the_last_good_copy(tmp_path, caplog, broken):
    path = _saved(tmp_path)
    path.write_text(broken)
    caplog.set_level(logging.INFO)

    state = _start(tmp_path, path)

    assert state.load_failed is False
    assert state.data["system"]["max_total_power"] == 600
    assert state.data["devices"]["INV_1"]["enabled"] is False
    assert json.loads(path.read_text())["system"]["max_total_power"] == 600
    (aside,) = _unreadable(tmp_path)
    assert aside.read_text() == broken
    assert "event=runtime_state_restored" in caplog.text
    assert "source=last_good" in caplog.text


@pytest.mark.parametrize("copy_text", [None, "{not valid json", "[]"])
def test_with_no_usable_copy_an_unreadable_file_starts_with_control_off(tmp_path, caplog, copy_text):
    path = _saved(tmp_path)
    copy = last_good_runtime_state_path(str(path))
    if copy_text is None:
        os.remove(copy)
    else:
        open(copy, "w").write(copy_text)
    path.write_text("{not valid json")
    caplog.set_level(logging.WARNING)

    state = _start(tmp_path, path)

    assert state.load_failed is True
    assert state.data["system"]["enabled"] is False
    assert json.loads(path.read_text())["system"]["enabled"] is False
    assert len(_unreadable(tmp_path)) == 1
    assert "event=runtime_state_unusable" in caplog.text


def test_a_file_that_cannot_be_set_aside_is_not_overwritten(tmp_path, monkeypatch):
    path = _saved(tmp_path)
    os.remove(last_good_runtime_state_path(str(path)))
    path.write_text("{not valid json")
    import ems.runtime_state as runtime_state_module

    def refuse(*args, **kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(runtime_state_module.os, "link", refuse)
    monkeypatch.setattr(runtime_state_module.shutil, "copy2", refuse)

    state = _start(tmp_path, path)

    assert state.data["system"]["enabled"] is False
    assert path.read_text() == "{not valid json"


def test_a_removed_file_comes_back_from_the_last_good_copy(tmp_path, caplog):
    path = _saved(tmp_path)
    os.remove(path)
    caplog.set_level(logging.INFO)

    state = _start(tmp_path, path)

    assert state.data["system"]["max_total_power"] == 600
    assert json.loads(path.read_text())["devices"]["INV_1"]["enabled"] is False
    assert "event=runtime_state_restored" in caplog.text
    assert "source=last_good" in caplog.text


def test_a_file_removed_while_the_ems_runs_comes_back_with_its_values(tmp_path):
    path = _saved(tmp_path)
    state = RuntimeState(str(path), _defaults())
    state.load_or_create()
    os.remove(path)

    data = state.load_if_changed()

    assert data["system"]["max_total_power"] == 600


def test_a_first_start_without_file_or_copy_starts_from_the_defaults(tmp_path, caplog):
    path = tmp_path / "runtime-state.json"
    caplog.set_level(logging.WARNING)

    state = _start(tmp_path, path)

    assert state.data["system"]["enabled"] is True
    assert state.load_failed is False
    assert "runtime_state_unusable" not in caplog.text
    assert os.path.exists(last_good_runtime_state_path(str(path)))


def test_a_start_refreshes_the_copy_from_a_file_edited_by_hand(tmp_path):
    path = _saved(tmp_path)
    edited = json.loads(path.read_text())
    edited["system"]["max_total_power"] = 700
    path.write_text(json.dumps(edited))

    _start(tmp_path, path)

    copy = last_good_runtime_state_path(str(path))
    assert json.loads(open(copy).read())["system"]["max_total_power"] == 700


def test_a_setup_reset_takes_the_copy_with_it(tmp_path):
    path = _saved(tmp_path)
    config, digest = _config(tmp_path, '{"set up": "fresh"}\n')
    request_runtime_reset(str(path), digest)

    state = start_runtime_state(str(path), _defaults(), str(config))

    assert state.data["system"]["max_total_power"] == 800
    assert json.loads(open(last_good_runtime_state_path(str(path))).read())["system"]["max_total_power"] == 800


def test_a_restored_copy_is_no_placeholder_seed(tmp_path):
    path = _saved(tmp_path)
    os.remove(path)

    state = _start(tmp_path, path, placeholder_start=True)

    assert "seeded_from_placeholders" not in state.data
    assert state.data["system"]["max_total_power"] == 600


def test_a_copy_that_cannot_be_written_does_not_fail_the_save(tmp_path, caplog):
    path = tmp_path / "runtime-state.json"
    state = RuntimeState(str(path), _defaults())
    state.load_or_create()
    copy = last_good_runtime_state_path(str(path))
    os.remove(copy)
    os.mkdir(copy)
    caplog.set_level(logging.WARNING)

    state.update_section("system", {"max_total_power": 700})

    assert json.loads(path.read_text())["system"]["max_total_power"] == 700
    assert "event=runtime_state_last_good_failed" in caplog.text


def test_emsctl_puts_a_removed_file_back_from_the_copy_rather_than_defaults(tmp_path):
    import emsctl

    path = _saved(tmp_path)
    os.remove(path)

    config = {"system": {}, "devices": [{"name": "INV_1", "sn": "SN1", "ip": "192.0.2.5"}]}

    runtime_state, created = emsctl.open_runtime_state(str(path), config)

    assert created is True
    assert runtime_state.snapshot()["system"]["max_total_power"] == 600
    runtime_state.save_atomic()
    assert json.loads(path.read_text())["devices"]["INV_1"]["enabled"] is False


def test_a_file_that_stays_unreadable_after_the_restore_ends_with_control_off(tmp_path, monkeypatch):
    import ems.runtime_state as runtime_state_module

    path = _saved(tmp_path)
    path.write_text("{not valid json")
    real_read = runtime_state_module.RuntimeState.read_file

    def unreadable_main(file_path):
        if str(file_path) == str(path):
            raise PermissionError("unreadable")
        return real_read(file_path)

    monkeypatch.setattr(runtime_state_module.RuntimeState, "read_file", staticmethod(unreadable_main))

    state = _start(tmp_path, path)

    assert state.data["system"]["enabled"] is False
    assert state.load_failed is True


def test_a_removed_file_whose_copy_cannot_be_used_starts_with_control_off(tmp_path, caplog):
    path = _saved(tmp_path)
    os.remove(path)
    copy = last_good_runtime_state_path(str(path))
    open(copy, "w").write("{broken")
    caplog.set_level(logging.WARNING)

    state = _start(tmp_path, path)

    assert state.data["system"]["enabled"] is False
    assert state.load_failed is True and state.created is False
    assert json.loads(path.read_text())["system"]["enabled"] is False
    assert open(copy).read() == "{broken"
    assert "event=runtime_state_unusable" in caplog.text


def test_emsctl_refuses_a_removed_file_whose_copy_cannot_be_used(tmp_path):
    import emsctl

    path = _saved(tmp_path)
    os.remove(path)
    open(last_good_runtime_state_path(str(path)), "w").write("{broken")

    with pytest.raises(ValueError, match="last good copy cannot be read"):
        emsctl.open_runtime_state(str(path), {"system": {}, "devices": []})
    assert not path.exists()


def test_a_replacement_that_cannot_be_written_leaves_the_unreadable_file_in_place(tmp_path, monkeypatch):
    """Interleaving: the card is full between the set-aside and the write; the file must not go missing."""

    import ems.runtime_state as runtime_state_module

    path = _saved(tmp_path)
    path.write_text("{not valid json")

    def card_full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runtime_state_module, "atomic_write_bytes", card_full)

    state = _start(tmp_path, path)
    data = state.load_if_changed()

    assert path.read_text() == "{not valid json"
    assert data["system"]["max_total_power"] == 600
    assert state.created is False and state.load_failed is True


def test_a_writer_that_never_read_the_file_does_not_overwrite_it_or_its_copy(tmp_path):
    """The Admin opens its own instance; over a hand-edit typo it holds no operator's state."""

    from ems.runtime_commands import RuntimeWriteError, apply_system_update

    path = _saved(tmp_path)
    copy_before = open(last_good_runtime_state_path(str(path))).read()
    path.write_text("{typo")
    writer = RuntimeState(str(path), {"devices": {}})
    writer.load_if_changed(force=True)

    with pytest.raises((ValueError, RuntimeWriteError)):
        apply_system_update(writer, {"max_total_power": 500}, None, source="admin")

    assert path.read_text() == "{typo"
    assert open(last_good_runtime_state_path(str(path))).read() == copy_before


def test_control_off_never_replaces_the_copy_an_operator_saved(tmp_path, monkeypatch):
    import ems.runtime_state as runtime_state_module

    path = _saved(tmp_path)
    copy_before = open(last_good_runtime_state_path(str(path))).read()
    path.write_text("{not valid json")
    real_read = runtime_state_module.RuntimeState.read_file

    def unreadable_main(file_path):
        if str(file_path) == str(path):
            raise PermissionError("unreadable")
        return real_read(file_path)

    monkeypatch.setattr(runtime_state_module.RuntimeState, "read_file", staticmethod(unreadable_main))

    _start(tmp_path, path)

    assert open(last_good_runtime_state_path(str(path))).read() == copy_before


def test_a_start_sweeps_temp_files_a_killed_copy_write_left(tmp_path):
    import time

    path = _saved(tmp_path)
    stale = tmp_path / "runtime-state.json.last-good.123.456.0123abcd.tmp"
    stale.write_text("{}")
    old = time.time() - 7200
    os.utime(stale, (old, old))

    _start(tmp_path, path)

    assert not stale.exists()


def _refuse_set_aside(monkeypatch):
    import ems.runtime_state as runtime_state_module

    def refuse(*args, **kwargs):
        raise PermissionError("hard links refused")

    monkeypatch.setattr(runtime_state_module.os, "link", refuse)
    monkeypatch.setattr(runtime_state_module.shutil, "copy2", refuse)


def test_a_file_that_cannot_be_set_aside_runs_on_the_copy_in_memory(tmp_path, monkeypatch):
    from ems.runtime_commands import apply_system_update

    path = _saved(tmp_path)
    copy_before = open(last_good_runtime_state_path(str(path))).read()
    path.write_text("{not valid json")
    _refuse_set_aside(monkeypatch)

    state = _start(tmp_path, path)

    assert state.data["system"]["max_total_power"] == 600
    assert state.load_failed is True
    assert path.read_text() == "{not valid json"
    assert open(last_good_runtime_state_path(str(path))).read() == copy_before

    apply_system_update(state, {"loop_interval": 7}, None, source="dashboard")

    stored = json.loads(path.read_text())
    assert (stored["system"]["max_total_power"], stored["system"]["loop_interval"]) == (600, 7)


def test_saves_after_a_control_off_start_never_replace_the_copy(tmp_path, monkeypatch):
    """Interleaving: control off kept the unreadable file; a schedule's later save replaces it, not the copy."""

    from ems.runtime_commands import apply_system_update

    path = _saved(tmp_path)
    copy = last_good_runtime_state_path(str(path))
    open(copy, "w").write("{broken copy")
    path.write_text("{not valid json")
    _refuse_set_aside(monkeypatch)

    state = _start(tmp_path, path)
    apply_system_update(state, {"loop_interval": 7}, None, source="schedule")

    assert open(copy).read() == "{broken copy"
    assert json.loads(path.read_text())["system"]["enabled"] is False


def test_a_second_start_after_a_control_off_start_keeps_the_copy(tmp_path):
    path = _saved(tmp_path)
    copy = last_good_runtime_state_path(str(path))
    open(copy, "w").write("{broken copy")
    path.write_text("{not valid json")

    _start(tmp_path, path)
    again = _start(tmp_path, path)

    assert again.data["system"]["enabled"] is False
    assert open(copy).read() == "{broken copy"


def test_control_switched_on_makes_the_state_the_copy_in_the_same_save(tmp_path):
    """Interleaving: the marker leaves with the switch in one save, read from the file under its lock."""

    from ems.runtime_commands import apply_system_update

    path = _saved(tmp_path)
    other = RuntimeState(str(path), _defaults())
    other.load_or_create()
    os.remove(last_good_runtime_state_path(str(path)))
    path.write_text("{not valid json")
    state = _start(tmp_path, path)
    assert "started_control_off" not in other.data

    apply_system_update(other, {"enabled": True}, None, source="dashboard")

    stored = json.loads(path.read_text())
    copy = json.loads(open(last_good_runtime_state_path(str(path))).read())
    assert stored["system"]["enabled"] is True and "started_control_off" not in stored
    assert stored["system"]["max_total_power"] == _defaults()["system"]["max_total_power"]
    assert copy == stored
    assert "started_control_off" not in state.load_if_changed()


def test_a_control_off_start_switched_on_by_hand_becomes_the_copy_again(tmp_path):
    path = _saved(tmp_path)
    os.remove(last_good_runtime_state_path(str(path)))
    path.write_text("{not valid json")
    _start(tmp_path, path)
    edited = json.loads(path.read_text())
    edited["system"]["enabled"] = True
    path.write_text(json.dumps(edited))

    _start(tmp_path, path)

    copy = json.loads(open(last_good_runtime_state_path(str(path))).read())
    assert copy["system"]["enabled"] is True


def _admin_write(tmp_path, path):
    from admin.install_context import detect_install_context
    from admin.runtime_convergence import write_targets

    config = {"system": {"max_total_power": 1600}, "devices": []}
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "config.json").write_text(json.dumps(config))
    target = ("system.loop_interval", "system", None, "loop_interval", 9)
    return write_targets(detect_install_context(base_dir=str(tmp_path)), config, [target], create=True)


def test_an_admin_save_puts_a_removed_file_back_from_its_copy(tmp_path):
    path = _saved(tmp_path)
    os.remove(path)

    result = _admin_write(tmp_path, path)

    stored = json.loads(path.read_text())
    assert result["applied"] == ["system.loop_interval"]
    assert (stored["system"]["max_total_power"], stored["system"]["loop_interval"]) == (600, 9)


def test_an_admin_save_over_a_removed_file_with_a_broken_copy_writes_control_off(tmp_path):
    path = _saved(tmp_path)
    os.remove(path)
    open(last_good_runtime_state_path(str(path)), "w").write("{broken")

    _admin_write(tmp_path, path)

    stored = json.loads(path.read_text())
    assert stored["system"]["enabled"] is False and stored["system"]["loop_interval"] == 9


def test_control_switched_off_after_a_hand_edit_switched_it_on_reaches_the_copy(tmp_path):
    path = _saved(tmp_path)
    copy = last_good_runtime_state_path(str(path))
    open(copy, "w").write("{broken copy")
    path.write_text("{not valid json")
    _start(tmp_path, path)
    edited = json.loads(path.read_text())
    edited["system"]["enabled"] = True
    path.write_text(json.dumps(edited))
    state = _start(tmp_path, path)

    state.update_section("system", {"enabled": False, "max_total_power": 100})
    os.remove(path)
    again = _start(tmp_path, path)

    assert (again.data["system"]["enabled"], again.data["system"]["max_total_power"]) == (False, 100)


def test_without_any_copy_the_control_off_state_becomes_the_copy(tmp_path):
    path = _saved(tmp_path)
    os.remove(last_good_runtime_state_path(str(path)))
    path.write_text("{not valid json")
    _start(tmp_path, path)
    restarted = _start(tmp_path, path)
    assert restarted.load_failed is False

    os.remove(path)
    data = restarted.load_if_changed()

    assert data["system"]["enabled"] is False


def test_a_file_lost_after_a_control_off_start_whose_copy_was_never_written_stays_off(tmp_path, monkeypatch):
    import ems.runtime_state as runtime_state_module

    path = _saved(tmp_path)
    os.remove(last_good_runtime_state_path(str(path)))
    path.write_text("{not valid json")
    real_write = runtime_state_module.atomic_write_bytes

    def no_copy(target, raw, **kwargs):
        if str(target).endswith(".last-good"):
            raise OSError(28, "No space left on device")
        return real_write(target, raw, **kwargs)

    monkeypatch.setattr(runtime_state_module, "atomic_write_bytes", no_copy)
    state = _start(tmp_path, path)

    os.remove(path)
    data = state.load_if_changed()

    assert data["system"]["enabled"] is False and state.created is False


def test_a_setup_reset_builds_afresh_even_where_the_copy_cannot_be_removed(tmp_path, monkeypatch):
    import ems.runtime_state as runtime_state_module

    path = _saved(tmp_path)
    config, digest = _config(tmp_path, '{"set up": "fresh"}\n')
    request_runtime_reset(str(path), digest)
    real_remove = runtime_state_module.os.remove

    def refuse_copy(target):
        if str(target).endswith(".last-good"):
            raise PermissionError("busy")
        return real_remove(target)

    monkeypatch.setattr(runtime_state_module.os, "remove", refuse_copy)

    state = start_runtime_state(str(path), _defaults(), str(config))

    assert state.data["system"]["max_total_power"] == 800
    assert json.loads(open(last_good_runtime_state_path(str(path))).read())["system"]["max_total_power"] == 800


def test_a_control_off_start_without_any_copy_comes_back_off_after_a_restart(tmp_path):
    path = _saved(tmp_path)
    os.remove(last_good_runtime_state_path(str(path)))
    path.write_text("{not valid json")
    _start(tmp_path, path)
    os.remove(path)

    restarted = _start(tmp_path, path)

    assert restarted.data["system"]["enabled"] is False


def _copy_writes_fail(monkeypatch):
    import ems.runtime_state as runtime_state_module

    real_write = runtime_state_module.atomic_write_bytes

    def no_copy(target, raw, **kwargs):
        if str(target).endswith(".last-good"):
            raise OSError(28, "No space left on device")
        return real_write(target, raw, **kwargs)

    monkeypatch.setattr(runtime_state_module, "atomic_write_bytes", no_copy)


def test_a_restart_on_a_marked_file_whose_copy_was_never_written_stays_off_when_it_is_lost(tmp_path, monkeypatch):
    path = _saved(tmp_path)
    os.remove(last_good_runtime_state_path(str(path)))
    path.write_text("{not valid json")
    _copy_writes_fail(monkeypatch)
    _start(tmp_path, path)
    restarted = _start(tmp_path, path)
    assert restarted.load_failed is False

    os.remove(path)

    assert restarted.load_if_changed()["system"]["enabled"] is False


def test_a_control_off_start_fixed_by_hand_writes_the_fixed_state_back_when_the_file_is_lost(tmp_path, monkeypatch):
    path = _saved(tmp_path)
    os.remove(last_good_runtime_state_path(str(path)))
    path.write_text("{not valid json")
    _copy_writes_fail(monkeypatch)
    state = _start(tmp_path, path)
    path.write_text(json.dumps(OPERATOR_STATE))
    assert "started_control_off" not in state.load_if_changed()

    os.remove(path)

    assert state.load_if_changed()["system"]["max_total_power"] == 600
    assert json.loads(path.read_text())["system"]["max_total_power"] == 600


def test_a_control_off_start_under_template_placeholders_is_marked_as_their_seed(tmp_path):
    path = _saved(tmp_path)
    os.remove(last_good_runtime_state_path(str(path)))
    path.write_text("{not valid json")

    state = _start(tmp_path, path, placeholder_start=True)

    assert state.data.get("seeded_from_placeholders") is True
    assert json.loads(path.read_text()).get("seeded_from_placeholders") is True


def test_a_removed_file_whose_restore_cannot_be_written_runs_on_the_copy(tmp_path, monkeypatch):
    """A usable copy that cannot be put in place still runs; control off would lose it at the next save."""

    import ems.runtime_state as runtime_state_module

    path = _saved(tmp_path)
    os.remove(path)
    real_write = runtime_state_module.atomic_write_bytes
    attempts = []

    def main_refused(target, raw, **kwargs):
        if str(target) == str(path) and not attempts:
            attempts.append(target)
            raise OSError(28, "No space left on device")
        return real_write(target, raw, **kwargs)

    monkeypatch.setattr(runtime_state_module, "atomic_write_bytes", main_refused)

    state = _start(tmp_path, path)

    assert state.data["system"]["max_total_power"] == 600 and state.data["devices"]["INV_1"]["enabled"] is False
    assert state.created is False and state.load_failed is True
    assert "started_control_off" not in state.data


def test_emsctl_may_save_the_file_it_read(tmp_path):
    import emsctl

    path = _saved(tmp_path)
    config = {"system": {}, "devices": [{"name": "INV_1", "sn": "SN1", "ip": "192.0.2.5"}]}

    runtime_state, created = emsctl.open_runtime_state(str(path), config)
    runtime_state.data["system"]["loop_interval"] = 9
    runtime_state.save_atomic()

    assert created is False and json.loads(path.read_text())["system"]["loop_interval"] == 9


def test_the_admin_reads_a_removed_file_from_the_copy_that_will_run(tmp_path):
    from admin.config_runtime_overlap import read_runtime_state
    from admin.install_context import detect_install_context
    from admin.operation_settings import held_by_config

    path = _saved(tmp_path)
    seeded = json.loads(path.read_text())
    seeded["seeded_from_placeholders"] = True
    open(last_good_runtime_state_path(str(path)), "w").write(json.dumps(seeded))
    os.remove(path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.json").write_text(json.dumps({"system": {}, "devices": []}))
    context = detect_install_context(base_dir=str(tmp_path))

    assert read_runtime_state(path)["system"]["max_total_power"] == 600
    assert held_by_config(context, {"system": {}, "devices": []}) is True


def test_the_admin_reads_an_unreadable_file_from_the_copy_that_will_run(tmp_path):
    from admin.config_runtime_overlap import read_runtime_state

    path = _saved(tmp_path)
    path.write_text("{typo")

    assert read_runtime_state(path)["devices"]["INV_1"]["enabled"] is False


def test_a_placeholder_start_over_a_file_that_cannot_be_set_aside_writes_nothing(tmp_path, monkeypatch):
    path = _saved(tmp_path)
    os.remove(last_good_runtime_state_path(str(path)))
    path.write_text("{not valid json")
    _refuse_set_aside(monkeypatch)

    _start(tmp_path, path, placeholder_start=True)

    assert path.read_text() == "{not valid json"


def test_a_running_ems_without_a_copy_writes_back_the_state_it_runs_when_the_file_is_lost(tmp_path, monkeypatch):
    path = _saved(tmp_path)
    os.remove(last_good_runtime_state_path(str(path)))
    _copy_writes_fail(monkeypatch)
    state = _start(tmp_path, path)

    os.remove(path)
    data = state.load_if_changed()

    assert (data["system"]["max_total_power"], data["devices"]["INV_1"]["enabled"]) == (600, False)
    assert state.created is False
    assert json.loads(path.read_text())["system"]["max_total_power"] == 600


def test_an_edit_made_while_the_card_is_full_survives_the_next_cycles(tmp_path, monkeypatch):
    import ems.runtime_state as runtime_state_module

    path = _saved(tmp_path)
    os.remove(path)

    def card_full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runtime_state_module, "atomic_write_bytes", card_full)
    state = _start(tmp_path, path)
    with pytest.raises(OSError):
        state.update_section("system", {"enabled": False})

    data = state.load_if_changed()

    assert data["system"]["enabled"] is False and data["system"]["max_total_power"] == 600


def test_a_placeholder_start_reading_a_control_off_state_does_not_mark_it_as_their_seed(tmp_path):
    path = _saved(tmp_path)
    os.remove(last_good_runtime_state_path(str(path)))
    path.write_text("{not valid json")
    _start(tmp_path, path)

    again = _start(tmp_path, path, placeholder_start=True)

    assert "seeded_from_placeholders" not in again.data
    assert "seeded_from_placeholders" not in json.loads(path.read_text())


def test_a_running_ems_keeps_the_state_it_runs_over_an_older_copy_when_the_file_is_lost(tmp_path, monkeypatch):
    import ems.runtime_state as runtime_state_module

    path = _saved(tmp_path)
    os.remove(path)
    real_write = runtime_state_module.atomic_write_bytes
    full = [True]

    def card(target, raw, **kwargs):
        if full[0]:
            raise OSError(28, "No space left on device")
        return real_write(target, raw, **kwargs)

    monkeypatch.setattr(runtime_state_module, "atomic_write_bytes", card)
    state = _start(tmp_path, path)
    with pytest.raises(OSError):
        state.update_section("system", {"enabled": False})
    full[0] = False

    data = state.load_if_changed()

    assert data["system"]["enabled"] is False
    assert json.loads(path.read_text())["system"]["enabled"] is False


def test_a_placeholder_start_on_a_full_card_still_comes_up(tmp_path, monkeypatch):
    import ems.runtime_state as runtime_state_module

    path = _saved(tmp_path)
    os.remove(last_good_runtime_state_path(str(path)))
    path.write_text("{not valid json")

    def card_full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runtime_state_module, "atomic_write_bytes", card_full)

    state = _start(tmp_path, path, placeholder_start=True)

    assert state.data["system"]["enabled"] is False


@pytest.mark.parametrize("hollow", ["[]", "null", '"x"'])
def test_a_running_ems_and_an_admin_writer_treat_json_that_is_no_object_as_unreadable(tmp_path, hollow):
    from ems.runtime_commands import RuntimeWriteError, apply_system_update

    path = _saved(tmp_path)
    running = RuntimeState(str(path), _defaults())
    running.load_or_create()
    path.write_text(hollow)

    assert running.load_if_changed()["system"]["max_total_power"] == 600

    writer = RuntimeState(str(path), {"devices": {}})
    writer.load_if_changed(force=True)
    with pytest.raises((ValueError, RuntimeWriteError)):
        apply_system_update(writer, {"max_total_power": 500}, None, source="admin")
    assert path.read_text() == hollow


def _control_off_start(tmp_path):
    path = _saved(tmp_path)
    os.remove(last_good_runtime_state_path(str(path)))
    path.write_text("{not valid json")
    state = _start(tmp_path, path)
    assert state.started_control_off
    return path, state


def _diagnosed(tmp_path, runtime):
    from ems import diagnostics
    from tests._emsctl_test_helpers import diagnose_args

    args = diagnose_args(tmp_path)
    with open(args.runtime_state, "w", encoding="utf-8") as handle:
        json.dump(runtime, handle)
    return diagnostics.run_install_diagnosis(args)


def test_diagnose_names_a_control_off_start_as_an_error_with_what_to_do(tmp_path):
    path, _state = _control_off_start(tmp_path / "ems")
    runtime = json.loads(path.read_text())

    report = _diagnosed(tmp_path, runtime)

    causes = [cause for cause in report["root_causes"] if cause["code"] == "runtime_state_unusable"]
    assert len(causes) == 1
    assert causes[0]["severity"] == "error"
    assert "switch control on" in causes[0]["suggested_next_check"]
    assert report["diagnosis"]["root_causes"] == report["root_causes"]
    assert ("runtime_state_unusable", "error") in [(check["code"], check["level"]) for check in report["checks"]]
    assert report["status"] == "error"


@pytest.mark.parametrize("marked, enabled", [(False, False), (True, True)])
def test_diagnose_names_no_control_off_start_without_the_mark_or_once_control_is_on(tmp_path, marked, enabled):
    runtime = json.loads(json.dumps(OPERATOR_STATE))
    runtime["system"]["enabled"] = enabled
    if marked:
        runtime["started_control_off"] = True

    report = _diagnosed(tmp_path, runtime)

    assert "runtime_state_unusable" not in [cause["code"] for cause in report["root_causes"]]
    assert "runtime_state_unusable" not in [check["code"] for check in report["checks"]]


def test_the_live_snapshot_says_control_started_off_until_control_is_switched_on(tmp_path):
    from types import SimpleNamespace

    from dashboard.telemetry import build_dashboard_snapshot
    from ems.runtime_commands import apply_system_update

    _path, state = _control_off_start(tmp_path)
    controller = SimpleNamespace(
        devices=[],
        runtime_state=state,
        device_online={},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
        zendure_mqtt_runtime=None,
    )

    def snapshot():
        return build_dashboard_snapshot(
            controller,
            load_w=0,
            states=[],
            targets=[],
            effective_targets=[],
            allocated_total_w=0,
            effective_total_w=0,
            enabled=False,
            max_total_power=800,
            min_output_limit=0,
        )["controller"]

    assert snapshot()["started_control_off"] is True

    apply_system_update(state, {"enabled": True}, None, source="dashboard")

    assert snapshot()["started_control_off"] is False


def test_control_switched_on_by_a_hand_edit_drops_the_mark_on_the_live_reload(tmp_path):
    """Review: the mark outlived a hand edit's switch-on in memory, so a later pause read as an unreadable start."""

    from types import SimpleNamespace

    from dashboard.telemetry import build_dashboard_snapshot
    from ems.runtime_commands import apply_system_update

    path, state = _control_off_start(tmp_path)
    edited = json.loads(path.read_text())
    edited["system"]["enabled"] = True
    path.write_text(json.dumps(edited))

    state.load_if_changed()
    assert "started_control_off" not in state.data
    apply_system_update(state, {"enabled": False}, None, source="dashboard")

    assert "started_control_off" not in json.loads(path.read_text())
    controller = SimpleNamespace(devices=[], runtime_state=state, device_online={}, commanded_total_w=0, filtered_load_w=0, _dashboard_capabilities=[], zendure_mqtt_runtime=None)
    snapshot = build_dashboard_snapshot(
        controller, load_w=0, states=[], targets=[], effective_targets=[], allocated_total_w=0, effective_total_w=0,
        enabled=False, max_total_power=800, min_output_limit=0,
    )
    assert snapshot["controller"]["started_control_off"] is False


def test_the_live_flag_is_off_while_control_reads_on_even_with_the_mark(tmp_path):
    from types import SimpleNamespace

    from dashboard.telemetry import _started_control_off

    marked = {"started_control_off": True, "system": {"enabled": True}}

    assert _started_control_off(SimpleNamespace(runtime_state=SimpleNamespace(data=marked))) is False


def test_diagnose_control_mode_names_the_control_off_start_too(tmp_path):
    from ems import diagnostics
    from tests._emsctl_test_helpers import diagnose_args

    path, _state = _control_off_start(tmp_path / "ems")
    args = diagnose_args(tmp_path, control=True)
    with open(args.runtime_state, "w", encoding="utf-8") as handle:
        handle.write(path.read_text())

    report = diagnostics.run_control_diagnosis(args)

    assert "runtime_state_unusable" in [cause["code"] for cause in report["root_causes"]]
