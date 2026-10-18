# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import logging
import os

import pytest

from ems.runtime_state import RuntimeState, merge_runtime_defaults

pytestmark = [
    pytest.mark.contract,
]


DEFAULTS = {
    "system": {
        "enabled": True,
        "max_total_power": 800,
        "loop_interval": 5,
    },
    "winter": {
        "enabled": False,
    },
    "devices": {
        "WR1": {
            "enabled": True,
            "max_power": 800,
            "offgrid_socket_mode": "off",
            "pv_priority_factor": 1.0,
        }
    },
}


def test_merge_runtime_defaults_preserves_unknown_keys_and_removes_legacy_socket():
    merged = merge_runtime_defaults(
        {
            "unknown": {"keep": True},
            "system": {
                "enabled": False,
                "extra": "preserved",
            },
            "devices": {
                "WR1": {
                    "max_power": 600,
                    "ac_charge_power_w": 200,
                    "offgrid_socket": True,
                },
                "WR2": {
                    "enabled": False,
                    "offgrid_socket": True,
                },
            },
        },
        DEFAULTS,
    )

    assert merged["unknown"] == {"keep": True}
    assert merged["system"]["enabled"] is False
    assert merged["system"]["loop_interval"] == 5
    assert merged["system"]["extra"] == "preserved"
    assert merged["devices"]["WR1"]["max_power"] == 600
    assert merged["devices"]["WR1"]["ac_charge_power_w"] == 200
    assert "offgrid_socket" not in merged["devices"]["WR1"]
    assert "offgrid_socket" not in merged["devices"]["WR2"]
    assert merged["devices"]["WR2"]["enabled"] is False


def test_runtime_state_creates_default_file_and_saves_atomically(tmp_path):
    path = tmp_path / "runtime-state.json"
    state = RuntimeState(str(path), DEFAULTS)

    data = state.load_or_create()

    assert path.exists()
    assert not list(tmp_path.glob("*.tmp"))
    assert data["system"]["enabled"] is True
    assert data["devices"]["WR1"]["max_power"] == 800

    state.update_section("system", {"enabled": False})
    state.update_section("winter", {"enabled": True})
    assert state.set_device("WR1", "max_power", 650) is True
    assert state.set_device("WR1", "max_power", 650) is False
    state.save_atomic()

    saved = json.loads(path.read_text())
    assert saved["system"]["enabled"] is False
    assert saved["winter"]["enabled"] is True
    assert saved["devices"]["WR1"]["max_power"] == 650


def test_runtime_state_recreates_configured_data_path_without_root_migration(tmp_path):
    data_path = tmp_path / "data" / "runtime-state.json"
    old_root_path = tmp_path / "runtime-state.json"
    old_root_payload = {
        "system": {
            "enabled": False,
            "max_total_power": 123,
        }
    }
    old_root_path.write_text(json.dumps(old_root_payload))

    state = RuntimeState(str(data_path), DEFAULTS)
    data = state.load_or_create()

    assert data_path.exists()
    assert json.loads(old_root_path.read_text()) == old_root_payload
    assert data["system"]["enabled"] is True
    assert data["system"]["max_total_power"] == 800
    assert data["devices"]["WR1"]["enabled"] is True


def test_runtime_state_load_if_changed_merges_external_updates(tmp_path):
    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({
        "system": {"enabled": False},
        "devices": {"WR1": {"max_power": 700}},
    }))
    state = RuntimeState(str(path), DEFAULTS)

    data = state.load_or_create()

    assert data["system"]["enabled"] is False
    assert data["system"]["loop_interval"] == 5
    assert data["devices"]["WR1"]["max_power"] == 700
    assert data["devices"]["WR1"]["pv_priority_factor"] == 1.0

    path.write_text(json.dumps({
        "system": {"enabled": True},
        "devices": {"WR1": {"max_power": 500}},
    }))

    data = state.load_if_changed(force=True)

    assert data["system"]["enabled"] is True
    assert data["devices"]["WR1"]["max_power"] == 500


def test_runtime_state_invalid_json_keeps_current_data_and_logs_warning(tmp_path, caplog):
    path = tmp_path / "runtime-state.json"
    state = RuntimeState(str(path), DEFAULTS)
    state.load_or_create()
    state.update_section("system", {"enabled": False})

    path.write_text("{not valid json")

    caplog.set_level(logging.WARNING)
    data = state.load_if_changed(force=True)

    assert data["system"]["enabled"] is False
    assert "event=runtime_state_load_error" in caplog.text


def _write_externally(path, mutate):
    """Rewrite the file the way emsctl does, with a distinct mtime."""

    data = json.loads(path.read_text())
    mutate(data)
    stat = path.stat()
    path.write_text(json.dumps(data))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


@pytest.mark.parametrize(
    "save",
    [
        lambda state: state.update_section("system", {"max_total_power": 600}),
        lambda state: state.update_device("WR1", {"max_power": 600}),
        lambda state: (state.set_device("WR1", "max_power", 600), state.save_atomic()),
    ],
    ids=["update_section", "update_device", "set_then_save"],
)
def test_a_save_keeps_a_change_another_process_wrote_since_the_last_load(
    tmp_path, save
):
    """Interleaving: EMS loads, emsctl disables, the dashboard saves a field."""

    path = tmp_path / "runtime-state.json"
    state = RuntimeState(str(path), DEFAULTS)
    state.load_or_create()

    def disable(data):
        data["system"]["enabled"] = False
        data["devices"]["WR1"]["runtime_role"] = "ac_input"

    _write_externally(path, disable)
    save(state)

    on_disk = json.loads(path.read_text())
    assert on_disk["system"]["enabled"] is False
    assert on_disk["devices"]["WR1"]["runtime_role"] == "ac_input"


@pytest.mark.parametrize(
    "save",
    [
        lambda state: state.update_section("system", {"max_total_power": 600}),
        lambda state: state.update_device("WR1", {"max_power": 600}),
    ],
    ids=["update_section", "update_device"],
)
def test_a_save_keeps_a_change_another_writer_saved_within_the_same_timestamp(
    tmp_path, save
):
    """Interleaving: EMS loads, emsctl saves within the same filesystem tick,
    the dashboard saves a field.

    File timestamps come from a coarse clock, a few milliseconds per tick, so
    two writes close together carry one mtime. A re-read that waits for the
    mtime to move skips the second writer's change and saves over it.
    """

    path = tmp_path / "runtime-state.json"
    state = RuntimeState(str(path), DEFAULTS)
    state.load_or_create()
    stamp = path.stat()

    data = json.loads(path.read_text())
    data["system"]["enabled"] = False
    path.write_text(json.dumps(data))
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))

    save(state)

    assert json.loads(path.read_text())["system"]["enabled"] is False


def test_a_save_leaves_another_writers_temp_file_alone(tmp_path):
    """The EMS, the Admin container and emsctl each save from a process of
    their own; a temp file shared by name is truncated by whichever starts
    second, and its owner then renames the other one's half-written file."""

    path = tmp_path / "runtime-state.json"
    state = RuntimeState(str(path), DEFAULTS)
    state.load_or_create()
    foreign = tmp_path / "runtime-state.json.tmp"
    foreign.write_text('{"half": ')

    state.update_section("system", {"enabled": False})

    assert foreign.read_text() == '{"half": '
    assert json.loads(path.read_text())["system"]["enabled"] is False
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [
        "runtime-state.json",
        "runtime-state.json.lock",
        "runtime-state.json.tmp",
    ]


def test_a_save_that_fails_leaves_no_temp_file_and_the_file_unchanged(tmp_path):
    path = tmp_path / "runtime-state.json"
    state = RuntimeState(str(path), DEFAULTS)
    state.load_or_create()
    before = path.read_bytes()
    state.data["system"]["unserialisable"] = object()

    with pytest.raises(TypeError):
        state.save_atomic()

    assert path.read_bytes() == before
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["runtime-state.json", "runtime-state.json.lock"]


@pytest.mark.parametrize(
    "save",
    [
        lambda state: state.update_section("system", {"max_total_power": 600}),
        lambda state: state.update_device("WR1", {"max_power": 600}),
    ],
    ids=["update_section", "update_device"],
)
def test_a_save_holds_a_lock_another_process_has_to_wait_for(tmp_path, monkeypatch, save):
    """Interleaving: emsctl and the EMS re-read and replace the file; while one
    does, another process cannot take runtime-state.json.lock."""

    import fcntl

    path = tmp_path / "runtime-state.json"
    state = RuntimeState(str(path), DEFAULTS)
    state.load_or_create()
    held = []
    real = state.save_atomic

    def save_while_checking():
        with open(tmp_path / "runtime-state.json.lock") as other_process:
            try:
                fcntl.flock(other_process, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(True)
        real()

    monkeypatch.setattr(state, "save_atomic", save_while_checking)

    save(state)

    assert held == [True]


def test_a_writer_that_cannot_create_the_lock_still_saves(tmp_path, monkeypatch):
    path = tmp_path / "runtime-state.json"
    state = RuntimeState(str(path), DEFAULTS)
    state.load_or_create()
    real_open = os.open

    def no_lock(name, *args, **kwargs):
        if str(name).endswith(".lock"):
            raise PermissionError(13, "Permission denied", name)
        return real_open(name, *args, **kwargs)

    monkeypatch.setattr("ems.runtime_state.os.open", no_lock)

    state.update_section("system", {"enabled": False})

    assert json.loads(path.read_text())["system"]["enabled"] is False


def test_a_change_waits_a_bounded_time_for_a_stalled_writer_then_says_it_is_busy(tmp_path, monkeypatch):
    """A suspended emsctl holding the lock must not stall the control loop."""

    import fcntl

    monkeypatch.setattr("ems.runtime_state.LOCK_TIMEOUT_SECONDS", 0.05)
    path = tmp_path / "runtime-state.json"
    state = RuntimeState(str(path), DEFAULTS)
    state.load_or_create()
    before = path.read_bytes()
    with open(tmp_path / "runtime-state.json.lock", "a") as stalled:
        fcntl.flock(stalled, fcntl.LOCK_EX)
        with pytest.raises(ValueError, match="busy: another process holds its lock"):
            state.update_section("system", {"enabled": False})

    assert path.read_bytes() == before


def test_the_startup_reconcile_holds_the_lock_from_read_to_save(tmp_path, monkeypatch):
    """Interleaving: the EMS reconciles on start while emsctl would save; the
    reconcile holds the lock from its read to its save."""

    import fcntl

    path = tmp_path / "runtime-state.json"
    RuntimeState(str(path), DEFAULTS).load_or_create()
    state = RuntimeState(str(path), DEFAULTS)
    held = []
    real = state.save_atomic

    def save_while_checking():
        with open(tmp_path / "runtime-state.json.lock") as other_process:
            try:
                fcntl.flock(other_process, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(True)
        real()

    data = json.loads(path.read_text())
    data["devices"]["GONE"] = {"enabled": True}
    path.write_text(json.dumps(data))
    monkeypatch.setattr(state, "save_atomic", save_while_checking)

    state.load_or_create()

    assert held and all(held)


def test_a_writer_waiting_for_another_process_keeps_no_reader_waiting(tmp_path, monkeypatch):
    """Interleaving: emsctl holds runtime-state.json.lock; a dashboard thread
    starts an edit and waits for it; the control loop reads a value meanwhile.
    The waiting writer holds no in-process lock yet, so the read returns."""

    import fcntl
    import threading

    path = tmp_path / "runtime-state.json"
    state = RuntimeState(str(path), DEFAULTS)
    state.load_or_create()
    waiting = threading.Event()
    release = threading.Event()

    def polling_sleep(_seconds):
        waiting.set()
        release.wait()

    monkeypatch.setattr("ems.runtime_state.time.sleep", polling_sleep)
    holder = open(tmp_path / "runtime-state.json.lock", "a")
    fcntl.flock(holder, fcntl.LOCK_EX)
    writer = threading.Thread(target=lambda: state.update_section("system", {"enabled": False}))
    writer.start()
    assert waiting.wait(10)

    reader = threading.Thread(target=lambda: state.get_system("enabled"))
    reader.start()
    reader.join(10)
    reader_finished = not reader.is_alive()

    fcntl.flock(holder, fcntl.LOCK_UN)
    holder.close()
    release.set()
    writer.join(10)

    assert reader_finished
    assert json.loads(path.read_text())["system"]["enabled"] is False


def test_the_startup_save_waits_and_then_goes_on_without_a_stalled_lock(tmp_path, monkeypatch, caplog):
    import fcntl

    monkeypatch.setattr("ems.runtime_state.STARTUP_LOCK_TIMEOUT_SECONDS", 0.05)
    path = tmp_path / "runtime-state.json"
    with open(tmp_path / "runtime-state.json.lock", "a") as stalled:
        fcntl.flock(stalled, fcntl.LOCK_EX)
        with caplog.at_level(logging.WARNING):
            RuntimeState(str(path), DEFAULTS).load_or_create()

    assert path.exists()
    assert any("event=runtime_state_lock_busy" in record.getMessage() for record in caplog.records)


def test_a_lock_file_that_cannot_be_opened_is_reported_once(tmp_path, monkeypatch, caplog):
    import ems.file_io as file_io_module

    monkeypatch.setattr(file_io_module, "_LOCKS_UNAVAILABLE", set())
    real_open = os.open

    def no_lock(name, *args, **kwargs):
        if str(name).endswith(".lock"):
            raise PermissionError(13, "Permission denied", name)
        return real_open(name, *args, **kwargs)

    monkeypatch.setattr("ems.runtime_state.os.open", no_lock)
    state = RuntimeState(str(tmp_path / "runtime-state.json"), DEFAULTS)
    with caplog.at_level(logging.WARNING):
        state.load_or_create()
        state.update_section("system", {"enabled": False})

    assert len([r for r in caplog.records if "event=file_lock_unavailable" in r.getMessage()]) == 1


def test_temp_files_a_killed_writer_left_are_removed_at_start_and_nothing_else(tmp_path):
    from ems.runtime_state import remove_stale_temp_files

    path = tmp_path / "runtime-state.json"
    old = tmp_path / "runtime-state.json.4242.139.tmp"
    fresh = tmp_path / "runtime-state.json.4243.139.tmp"
    other = tmp_path / "runtime-state.json.backup.tmp"
    for item in (old, fresh, other):
        item.write_text("{")
    os.utime(old, (1_000_000_000, 1_000_000_000))

    removed = remove_stale_temp_files(str(path), now=1_000_000_000 + 7200)

    assert removed == ["runtime-state.json.4242.139.tmp"]
    assert not old.exists() and fresh.exists() and other.exists()


def test_a_missing_file_is_created_without_holding_the_in_process_lock_while_it_waits(tmp_path, monkeypatch):
    """Interleaving: a dashboard thread holds the flock and next takes
    ``self.lock``; the control loop finds the file gone and creates it. Were
    the loop to hold ``self.lock`` while it waits for the flock, each would
    wait for the other until the startup timeout."""

    import contextlib
    import threading

    import ems.runtime_state as runtime_state_module

    state = RuntimeState(str(tmp_path / "runtime-state.json"), DEFAULTS)
    real = runtime_state_module.exclusive_file_lock
    free = []

    def another_thread_can_take(lock):
        taken = []

        def probe():
            if lock.acquire(blocking=False):
                taken.append(True)
                lock.release()

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join()
        return bool(taken)

    @contextlib.contextmanager
    def watching(path, timeout=None):
        free.append(another_thread_can_take(state.lock))
        with real(path, timeout):
            yield

    monkeypatch.setattr(runtime_state_module, "exclusive_file_lock", watching)
    state.load_if_changed()

    assert free and all(free)
    assert (tmp_path / "runtime-state.json").exists()


def test_a_file_removed_while_the_ems_runs_is_recreated_within_an_edits_wait(tmp_path, monkeypatch, caplog):
    """The control loop calls ``load_if_changed`` every cycle: re-creating a
    removed file behind a stalled writer waits as long as an edit would, not
    as long as the start-up reconcile."""

    import fcntl
    import threading

    monkeypatch.setattr("ems.runtime_state.LOCK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr("ems.runtime_state.STARTUP_LOCK_TIMEOUT_SECONDS", 5)
    state = RuntimeState(str(tmp_path / "runtime-state.json"), DEFAULTS)
    with open(tmp_path / "runtime-state.json.lock", "a") as stalled:
        fcntl.flock(stalled, fcntl.LOCK_EX)
        loader = threading.Thread(target=state.load_if_changed)
        with caplog.at_level(logging.WARNING):
            loader.start()
            loader.join(2)
        finished = not loader.is_alive()
    loader.join()

    assert finished
    assert any("event=runtime_state_lock_busy" in record.getMessage() for record in caplog.records)


def test_a_replace_within_one_tick_of_the_clock_is_still_seen(tmp_path):
    """Interleaving: the EMS saves; within the same tick of the filesystem's
    coarse clock emsctl replaces the file. The mtime is the same, the inode
    is not."""

    path = tmp_path / "runtime-state.json"
    state = RuntimeState(str(path), DEFAULTS)
    state.load_or_create()
    before = os.stat(path)
    other = RuntimeState(str(path), DEFAULTS)
    other.load_if_changed(force=True)
    other.update_section("system", {"max_total_power": 321})
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    state.load_if_changed()

    assert state.data["system"]["max_total_power"] == 321


def test_a_writers_re_read_of_a_file_nobody_else_changed_logs_no_load(tmp_path, caplog):
    state = RuntimeState(str(tmp_path / "runtime-state.json"), DEFAULTS)
    state.load_or_create()

    with caplog.at_level(logging.INFO):
        state.update_section("system", {"max_total_power": 700})
        state.update_section("system", {"max_total_power": 650})

    messages = [record.getMessage() for record in caplog.records]
    assert not any("event=runtime_state_loaded" in message for message in messages)
    assert len([message for message in messages if "event=runtime_state_saved" in message]) == 2


def test_a_temp_file_of_another_container_with_the_same_pid_is_left_alone(tmp_path):
    """The EMS and the Admin both run as PID 1 in their containers and write
    the same bind-mounted directory; a write must not reuse the other's
    in-flight temp file."""

    import threading

    from ems.file_io import atomic_write_bytes

    path = tmp_path / "runtime-state.json"
    theirs = tmp_path / f"runtime-state.json.{os.getpid()}.{threading.get_ident()}.tmp"
    theirs.write_bytes(b"their half-written state")

    atomic_write_bytes(str(path), b"{}\n")

    assert theirs.read_bytes() == b"their half-written state"
    assert path.read_bytes() == b"{}\n"


def test_a_filesystem_without_flock_saves_without_the_lock_and_says_so_once(tmp_path, monkeypatch, caplog):
    """An NFS mount without a lock daemon opens the lock file but refuses
    flock with ENOLCK; the EMS must still start and save, as before locks."""

    import errno
    import fcntl

    import ems.file_io as file_io_module

    monkeypatch.setattr(file_io_module, "_LOCKS_UNAVAILABLE", set())
    real = fcntl.flock

    def no_locks(fd, operation):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(file_io_module.fcntl, "flock", no_locks)
    state = RuntimeState(str(tmp_path / "runtime-state.json"), DEFAULTS)
    with caplog.at_level(logging.WARNING):
        state.load_or_create()
        state.update_section("system", {"enabled": False})
    monkeypatch.setattr(file_io_module.fcntl, "flock", real)

    assert json.loads((tmp_path / "runtime-state.json").read_text())["system"]["enabled"] is False
    assert len([r for r in caplog.records if "event=file_lock_unavailable" in r.getMessage()]) == 1
