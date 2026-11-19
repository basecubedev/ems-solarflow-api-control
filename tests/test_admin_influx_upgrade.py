# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The bundled InfluxDB upgrade and its way back, driven against fakes.

The fakes model the one fact the design rests on: once a newer InfluxDB has
converted the data directory, the previous image refuses to open it. So a
way back that started the previous image on the converted data would leave
InfluxDB down -- the tests hold the rollback to setting the data aside and
restoring the upgrade's own backup instead. Real images prove the same fact
in tests/test_influx_upgrade_real_images.py.
"""

import json
import math
from dataclasses import dataclass
from pathlib import Path

import pytest

from admin.deployment import DockerError
from admin.influx_upgrade import (
    FAILED,
    NOT_RUNNING_POLLS,
    READY_POLL_SECONDS,
    RESTARTS_ALLOWED,
    ROLLBACK_FAILED,
    ROLLBACK_STEPS,
    ROLLED_BACK,
    RUNNING,
    SLOW_START_SECONDS,
    PUTTING_BACK,
    STILL_STARTING,
    UPGRADE_STEPS,
    UPGRADED,
    MEMORY_GROWTH_SHARE,
    MEMORY_RESERVE_MB,
    InfluxUpgradeService,
    UpgradeRecordStore,
    _process_memory_mb,
    read_meminfo,
    read_swaps,
)

pytestmark = [pytest.mark.unit, pytest.mark.admin, pytest.mark.maintenance]

OLD = "influxdb:2.7"
NEW = "influxdb:2.9.1"
BACKUP = "ems-influxdb-manual-20261118T010000Z.tar.gz"
BUCKETS = ["_monitoring", "_tasks", "ems_1h", "ems_1m", "ems_5m", "ems_raw"]
TASKS = ["ems-downsample-1m", "ems-downsample-5m"]
ENV = (
    "INFLUXDB_ORG=ems\nINFLUXDB_TOKEN=tok\nDOCKER_INFLUXDB_INIT_MODE=setup\n"
    "DOCKER_INFLUXDB_INIT_USERNAME=admin\nDOCKER_INFLUXDB_INIT_PASSWORD=pw-123456\n"
    "DOCKER_INFLUXDB_INIT_ORG=ems\nDOCKER_INFLUXDB_INIT_BUCKET=ems_raw\n"
    "DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=tok\n"
)


@dataclass
class Context:
    install_root: Path

    @property
    def config_path(self):
        return self.install_root / "config" / "config.json"

    @property
    def config_exists(self):
        return self.config_path.exists()

    @property
    def compose_path(self):
        return self.install_root / "docker-compose.yml"

    @property
    def compose_exists(self):
        return self.compose_path.exists()

    @property
    def data_dir(self):
        return self.install_root / "data"


def compose_text(image):
    return (
        "services:\n  ems:\n    image: ghcr.io/basecubedev/ems-solarflow-api-control:v1\n"
        f"  influxdb:\n    image: {image}\n    container_name: ems-influxdb\n"
        "    volumes:\n      - ./data/influxdb:/var/lib/influxdb2\n"
    )


class Influx:
    """One InfluxDB container and the data directory it runs on."""

    def __init__(self):
        self.image = OLD
        self.status = "running"
        self.data = {"format": "2.7", "buckets": list(BUCKETS), "tasks": list(TASKS)}
        self.answers_after = 0
        self.token_ok = True
        self.version_override = None
        self.started = 0
        self.renewed = set()
        self.held_kb = 300 * 1024
        self.swap_kb = 0
        self.oom_killed = False
        self.status_detail = None
        self.setup_polls = 0
        self.setting_up = 0
        self.timeouts = {}

    @property
    def version(self):
        return {OLD: "2.7.12", NEW: "2.9.1"}.get(self.image, "0.0.0")

    def start(self, image):
        """Start ``image`` on the data; an older image refuses converted data."""

        self.image = image
        self.started += 1
        if self.data is None:
            self.data = {"format": "2.7" if image == OLD else "2.9", "buckets": ["_monitoring", "_tasks", "ems_raw"], "tasks": []}
            self.setting_up = self.setup_polls
        if image == NEW:
            self.data["format"] = "2.9"
        refuses = image == OLD and self.data["format"] != "2.7"
        self.status = "restarting" if refuses else "running"


class FakeDocker:
    def __init__(self, influx, calls):
        self.influx = influx
        self.calls = calls
        self.pull_error = None
        self.local_images = {OLD}
        self.on_pull = None

    def pull(self, image):
        self.calls.append(("pull", image))
        if self.on_pull:
            self.on_pull()
        if self.pull_error:
            raise self.pull_error
        self.local_images.add(image)

    def inspect_image(self, image):
        return {"ref": image} if image in self.local_images else None

    def inspect_container(self, name):
        return {"container_name": name, "image": self.influx.image, "status": self.influx.status,
                "status_detail": self.influx.status_detail}

    def inspect_container_started_at(self, name):
        return f"2026-11-18T10:00:{self.influx.started:02d}Z"

    def inspect_container_oom_killed(self, name):
        return self.influx.oom_killed

    def exec_capture(self, name, command, timeout=30):
        self.calls.append(("exec", tuple(command[:2])))
        influx = self.influx
        influx.timeouts[tuple(command[:2])] = timeout
        if influx.status != "running":
            return 1, "", "not running"
        if tuple(command[:2]) == ("influx", "ping"):
            if influx.setting_up > 0:
                if tuple(command[2:]) == ("--host", "http://localhost:8086"):
                    influx.setting_up -= 1
                    return 1, "", "connection refused"
                return 0, "OK\n", None
            if influx.answers_after > 0:
                influx.answers_after -= 1
                return 1, "", "connection refused"
            return 0, "OK\n", None
        if tuple(command) == ("influxd", "version"):
            return 0, f"InfluxDB v{influx.version_override or influx.version} (git: abc)\n", None
        if tuple(command) == ("cat", "/proc/1/status"):
            if influx.held_kb is None:
                return 0, "Name:\tinfluxd\n", None
            return 0, f"Name:\tinfluxd\nVmRSS:\t{influx.held_kb + 9000} kB\nRssAnon:\t{influx.held_kb} kB\nVmSwap:\t{influx.swap_kb} kB\n", None
        assert command[0] == "sh" and command[-1] == "ems", command
        if not influx.token_ok:
            return 1, "", "Error: unauthorized access"
        what = "bucket" if "bucket list" in command[2] else "task"
        names = influx.data["buckets" if what == "bucket" else "tasks"]
        return 0, json.dumps([{"name": name, "id": f"{name}-{'new' if name in influx.renewed else 'held'}"} for name in names]), None

    def set_aside_influx_data(self, root, image, name):
        self.calls.append(("set_aside", image, name))
        data = Path(root) / "data"
        (data / "influxdb").rename(data / name)
        (data / "influxdb").mkdir()
        self.influx.set_aside = self.influx.data
        self.influx.data = None

    def put_back_influx_data(self, root, image, aside, attempt):
        self.calls.append(("put_back", image, aside, attempt))
        data = Path(root) / "data"
        if (data / "influxdb").exists():
            (data / "influxdb").rename(data / attempt)
        (data / aside).rename(data / "influxdb")
        self.influx.attempt = self.influx.data
        self.influx.data = self.influx.set_aside
        self.influx.set_aside = None


class FakeCompose:
    def __init__(self, influx, calls):
        self.influx = influx
        self.calls = calls
        self.up_error = None

    def up(self, root, profiles=(), services=(), force_recreate=False, renew_anon_volumes=False):
        self.calls.append(("up", tuple(services), force_recreate, renew_anon_volumes))
        if self.up_error:
            raise self.up_error
        image = (Path(root) / "docker-compose.yml").read_text(encoding="utf-8").split("influxdb:\n    image: ")[1].split()[0]
        self.influx.start(image)

    def stop(self, root, services=(), profiles=()):
        self.calls.append(("stop", tuple(services)))
        self.influx.status = "exited"


class FakeBackups:
    def __init__(self, influx, calls):
        self.influx = influx
        self.calls = calls
        self.result = {"ok": True, "archives": [{"name": BACKUP, "verified": True}]}
        self.archives = set()
        self.restore_error = None
        self.check_error = None
        self.size = 10 * 2**20

    def create_backup(self, request):
        self.calls.append(("backup", request["scope"]))
        if self.result.get("ok"):
            self.archives.add(self.result["archives"][0]["name"])
            self.saved = json.loads(json.dumps(self.influx.data))
        return self.result

    def has_archive(self, name):
        return name in self.archives

    def check_influx_restore(self, name):
        if self.check_error:
            raise self.check_error
        assert name in self.archives
        return self.size

    def restore_influx_archive(self, name):
        self.calls.append(("restore", name))
        if self.restore_error:
            raise self.restore_error
        if self.influx.setting_up:
            raise RuntimeError('Error: API compatibility check failed: Get "http://localhost:8086/health": connection refused')
        self.influx.data = {**json.loads(json.dumps(self.saved)), "format": self.influx.data["format"]}


class Clock:
    def __init__(self):
        self.now = 0.0
        self.slept = []
        self.after_sleep = None

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
        if self.after_sleep is not None:
            self.after_sleep()


@pytest.fixture
def world(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "data" / "influxdb").mkdir(parents=True)
    (tmp_path / "config" / "config.json").write_text(
        json.dumps({"influxdb": {"enabled": True, "mode": "bundled", "org": "ems"}}), encoding="utf-8"
    )
    (tmp_path / "config" / "influxdb.env").write_text(ENV, encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text(compose_text(OLD), encoding="utf-8")
    influx = Influx()
    calls = []
    clock = Clock()
    docker = FakeDocker(influx, calls)
    compose = FakeCompose(influx, calls)
    backups = FakeBackups(influx, calls)
    docker_state = {"available": True}
    disk = {"free": 10 * 2**30}
    memory = {"MemAvailable": 4096, "SwapFree": 0}
    swaps = []

    def overview():
        return {
            "docker": dict(docker_state),
            "containers": {
                "influxdb": {"name": "ems-influxdb", "found": True, "running": influx.status == "running",
                             "image": influx.image}
            },
        }

    service = InfluxUpgradeService(
        record_path=tmp_path / "admin" / "influxdb-upgrade.json",
        backups=backups,
        context_provider=lambda: Context(tmp_path),
        docker=docker,
        compose=compose,
        overview_provider=overview,
        target_image=lambda: NEW,
        clock=clock,
        sleep=clock.sleep,
        disk_free=lambda _path: disk["free"],
        meminfo=lambda: dict(memory) if memory else None,
        swaps=lambda: list(swaps) if swaps is not None else None,
    )

    class World:
        pass

    w = World()
    w.root, w.influx, w.calls, w.clock = tmp_path, influx, calls, clock
    w.docker, w.compose, w.backups, w.service, w.docker_state = docker, compose, backups, service, docker_state
    w.disk = disk
    w.memory = memory
    w.swaps = swaps
    w.compose_image = lambda: (tmp_path / "docker-compose.yml").read_text(encoding="utf-8").split("influxdb:\n    image: ")[1].split()[0]
    w.record = lambda: service.store.read()
    return w


class Progress:
    def __init__(self):
        self.steps = []
        self.notes = []

    def record_step(self, step):
        self.steps.append(step)

    def note_step(self, key, note):
        self.notes.append((key, note, len(self.steps)))


MEMORY_READ = ("exec", ("cat", "/proc/1/status"))


def changes(calls):
    """The calls that do something: the memory check only reads."""

    return [call for call in calls if call != MEMORY_READ]


def keys(result, status="ok"):
    return [step["key"] for step in result["steps"] if step["status"] == status]


# --- plan --------------------------------------------------------------------


def _short_of_memory(world, held_mb=372, free_mb=70, swap_mb=0, zram_mb=0):
    world.influx.held_kb = held_mb * 1024
    world.memory.update(MemAvailable=free_mb, SwapFree=swap_mb + zram_mb)
    world.swaps[:] = [("/var/lib/ems-appliance-swap/swapfile", swap_mb, 0)] if swap_mb else []
    if zram_mb:
        world.swaps.append(("/dev/zram0", zram_mb, 0))


def test_the_plan_offers_the_version_this_build_ships(world):
    plan = world.service.plan()

    assert plan["state"] == "available"
    assert (plan["current_image"], plan["target_image"]) == (OLD, NEW)
    assert (plan["current_version"], plan["target_version"]) == ("2.7", "2.9.1")
    assert plan["container"] == {"name": "ems-influxdb", "running": True, "found": True, "image": OLD}
    assert plan["last"] is None and plan["rollback"] is None
    assert plan["memory"] == {"influx_mb": 300, "needed_mb": 124, "available_mb": 4096}
    assert world.calls == [MEMORY_READ]


@pytest.mark.parametrize(
    ("config", "image", "state"),
    [
        ({"enabled": False}, OLD, "disabled"),
        ({"enabled": True, "mode": "external", "url": "http://influx.lan:8086"}, OLD, "external"),
        ({"enabled": True, "mode": "bundled"}, "influxdb:latest", "unmanaged"),
        ({"enabled": True, "mode": "bundled"}, "influxdb:2.7-alpine", "unmanaged"),
        ({"enabled": True, "mode": "bundled"}, NEW, "current"),
        ({"enabled": True, "mode": "bundled"}, "influxdb:2.9", "current"),
        ({"enabled": True, "mode": "bundled"}, "influxdb:2.10.1", "newer"),
    ],
)
def test_the_plan_offers_nothing_it_should_not(world, config, image, state):
    (world.root / "config" / "config.json").write_text(json.dumps({"influxdb": config}), encoding="utf-8")
    (world.root / "docker-compose.yml").write_text(compose_text(image), encoding="utf-8")
    world.influx.image = image

    assert world.service.plan()["state"] == state


def test_a_stopped_influxdb_is_started_before_it_is_upgraded(world):
    world.influx.status = "exited"

    assert world.service.plan()["state"] == "not_running"
    assert world.service.upgrade(NEW)["status"] == "refused"
    assert changes(world.calls) == []


def test_without_docker_nothing_is_offered(world):
    world.docker_state["available"] = False

    plan = world.service.plan()

    assert plan["state"] == "unavailable" and plan["message"] == "Docker is not available."


def test_a_record_that_cannot_be_trusted_stops_both_directions(world):
    path = world.root / "admin" / "influxdb-upgrade.json"
    path.parent.mkdir()
    path.write_text("{not json", encoding="utf-8")

    plan = world.service.plan()

    assert plan["state"] == "unavailable" and "Remove it" in plan["message"]
    assert world.service.upgrade(NEW)["status"] == "refused"
    assert world.service.rollback(OLD)["status"] == "refused"


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"state": "done"},
        {"from_image": "influxdb:latest"},
        {"buckets": "ems_raw"},
        {"tasks": [1]},
        {"backup": ""},
    ],
)
def test_the_record_store_refuses_a_record_it_does_not_know(tmp_path, change):
    store = UpgradeRecordStore(tmp_path / "record.json")
    record = {
        "state": UPGRADED, "from_image": OLD, "to_image": NEW, "backup": BACKUP,
        "buckets": BUCKETS, "tasks": TASKS,
    }
    store.write(record)
    assert store.read()["state"] == UPGRADED
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    raw.update(change)
    store.path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError):
        store.read()


# --- upgrade ---------------------------------------------------------------------


def test_the_upgrade_backs_up_before_it_changes_anything(world):
    progress = Progress()

    result = world.service.upgrade(NEW, progress=progress)

    assert result["ok"] is True and result["backup"] == BACKUP
    assert keys(result) == [key for key, _label in UPGRADE_STEPS]
    assert [step["key"] for step in progress.steps] == keys(result)
    order = [call[0] for call in world.calls if call[0] in ("pull", "backup", "up")]
    assert order == ["pull", "backup", "up"]
    assert ("up", ("influxdb",), True, False) in world.calls
    assert world.compose_image() == NEW
    assert (world.influx.image, world.influx.status) == (NEW, "running")
    record = world.record()
    assert record["state"] == UPGRADED
    assert (record["from_image"], record["to_image"], record["backup"]) == (OLD, NEW, BACKUP)
    assert record["buckets"] == BUCKETS and record["tasks"] == TASKS


def test_after_the_upgrade_the_way_back_is_offered_with_its_backup(world):
    world.service.upgrade(NEW)

    plan = world.service.plan()

    assert plan["state"] == "current"
    assert plan["last"]["state"] == UPGRADED
    assert plan["rollback"]["available"] is True
    assert (plan["rollback"]["to_image"], plan["rollback"]["backup"]) == (OLD, BACKUP)


def test_the_upgrade_runs_only_to_the_image_the_operator_confirmed(world):
    result = world.service.upgrade("influxdb:2.10.1")

    assert result["status"] == "refused" and result["changed"] is False
    assert changes(world.calls) == [] and world.compose_image() == OLD and world.record() is None


def test_a_token_influxdb_refuses_stops_before_anything_changes(world):
    world.influx.token_ok = False

    result = world.service.upgrade(NEW)

    assert result["status"] == "failed" and result["changed"] is False
    assert keys(result, "error") == ["inventory"]
    assert "unauthorized" in result["message"] and "Nothing was changed." in result["message"]
    assert not any(call[0] in ("pull", "backup", "up") for call in world.calls)
    assert world.compose_image() == OLD and world.record() is None


def test_a_failed_download_changes_nothing(world):
    world.docker.pull_error = DockerError("image_pull_failed", "The image could not be downloaded.")

    result = world.service.upgrade(NEW)

    assert result["changed"] is False and keys(result, "error") == ["pull"]
    assert not any(call[0] in ("backup", "up") for call in world.calls)
    assert world.compose_image() == OLD and world.record() is None


def test_a_backup_that_is_not_verified_changes_nothing(world):
    world.backups.result = {"ok": True, "archives": [{"name": BACKUP, "verified": False}]}

    result = world.service.upgrade(NEW)

    assert result["changed"] is False and keys(result, "error") == ["backup"]
    assert not any(call[0] == "up" for call in world.calls)
    assert world.compose_image() == OLD and world.record() is None


def test_a_failed_backup_changes_nothing(world):
    world.backups.result = {"ok": False, "message": "No space left on device."}

    result = world.service.upgrade(NEW)

    assert result["changed"] is False and "No space left" in result["message"]
    assert world.compose_image() == OLD and world.record() is None


def test_a_compose_file_changed_meanwhile_is_not_overwritten(world):
    """Interleaving: the operator edits docker-compose.yml while the image downloads."""

    edited = compose_text("influxdb:2.7.11")

    def edit():
        (world.root / "docker-compose.yml").write_text(edited, encoding="utf-8")

    world.docker.on_pull = edit

    result = world.service.upgrade(NEW)

    assert result["changed"] is False and keys(result, "error") == ["update_compose"]
    assert (world.root / "docker-compose.yml").read_text(encoding="utf-8") == edited
    assert not any(call[0] == "up" for call in world.calls)
    assert world.record()["state"] == FAILED
    assert world.service.plan()["rollback"]["available"] is False


def test_a_failed_start_leaves_the_way_back_open(world):
    world.compose.up_error = DockerError("compose_up_failed", "Could not start the containers.")

    result = world.service.upgrade(NEW)

    assert result["changed"] is True and keys(result, "error") == ["recreate"]
    assert "Roll back restores" in result["message"] and BACKUP in result["message"]
    assert world.record()["state"] == FAILED
    assert world.service.plan()["rollback"]["available"] is True


def test_a_new_version_that_keeps_crashing_fails_quickly(world):
    def crash(image):
        world.influx.image = image
        world.influx.status = "restarting"

    world.influx.start = crash

    result = world.service.upgrade(NEW)

    assert keys(result, "error") == ["verify"] and "does not stay running" in result["message"]
    assert len(world.clock.slept) == NOT_RUNNING_POLLS - 1
    assert world.record()["state"] == FAILED


def test_a_slow_first_start_is_waited_for(world):
    world.influx.answers_after = 20

    result = world.service.upgrade(NEW)

    assert result["ok"] is True
    assert world.clock.slept == [READY_POLL_SECONDS] * 20


def test_a_start_longer_than_the_slow_mark_is_noted_once_and_waited_for(world):
    polls = SLOW_START_SECONDS // READY_POLL_SECONDS + 40
    world.influx.answers_after = polls
    progress = Progress()

    result = world.service.upgrade(NEW, progress=progress)

    assert result["ok"] is True and keys(result, "error") == []
    assert world.clock.slept == [READY_POLL_SECONDS] * polls
    verify_at = [step["key"] for step in progress.steps].index("recreate") + 1
    assert progress.notes == [("verify", STILL_STARTING, verify_at)]
    assert world.record()["state"] == UPGRADED


def test_the_job_shows_the_note_on_its_running_step_until_that_step_ends(world):
    from admin.backup_restore import BackupJob

    job = BackupJob("job", [{"key": key, "label": label} for key, label in UPGRADE_STEPS])
    job.note_step("verify", STILL_STARTING)
    assert all("note" not in step for step in job.snapshot()["steps"])
    seen = []
    world.influx.answers_after = SLOW_START_SECONDS // READY_POLL_SECONDS + 2
    world.clock.after_sleep = lambda: seen.append(
        next(step for step in job.snapshot()["steps"] if step["key"] == "verify")
    )

    world.service.upgrade(NEW, progress=job)

    assert "note" not in seen[0] and seen[0]["state"] == "running"
    assert seen[-1]["note"] == STILL_STARTING and seen[-1]["state"] == "running"
    assert all("note" not in step for step in job.snapshot()["steps"])


def test_a_start_that_stops_after_the_slow_mark_fails_without_rolling_back(world):
    polls = SLOW_START_SECONDS // READY_POLL_SECONDS + 3

    def stop_after_the_mark():
        if len(world.clock.slept) >= polls:
            world.influx.status = "exited"

    world.influx.answers_after = 10**6
    world.clock.after_sleep = stop_after_the_mark
    progress = Progress()

    result = world.service.upgrade(NEW, progress=progress)

    assert keys(result, "error") == ["verify"] and "does not stay running" in result["message"]
    assert [note[:2] for note in progress.notes] == [("verify", STILL_STARTING)]
    assert not any(call[0] in ("stop", "set_aside", "restore") for call in world.calls)
    assert world.compose_image() == NEW and world.influx.data["buckets"] == BUCKETS
    assert world.record()["state"] == FAILED


def test_an_operator_stops_a_long_wait_and_the_run_ends_as_not_completed_with_the_way_back(world):
    world.influx.answers_after = 10**6
    seen = []

    def stop_once_slow():
        if world.service.slow and not seen:
            seen.append(world.service.plan())
            seen.append(world.service.stop_waiting())

    world.clock.after_sleep = stop_once_slow

    result = world.service.upgrade(NEW, progress=Progress())

    assert [seen[0]["state"], seen[0]["slow"], seen[1]] == ["busy", True, True]
    assert keys(result, "error") == ["verify"] and "Waiting was stopped" in result["message"]
    assert "Roll back restores" in result["message"]
    assert world.record()["state"] == FAILED
    assert not any(call[0] in ("stop", "set_aside", "restore") for call in world.calls)
    plan = world.service.plan()
    assert plan["rollback"]["available"] is True and plan["slow"] is False
    assert world.service.stop_waiting() is False


def test_not_running_polls_fail_a_start_only_in_a_row(world):
    seen = []

    def flicker():
        seen.append(world.influx.status)
        world.influx.status = "restarting" if world.influx.status == "running" else "running"

    world.influx.answers_after = NOT_RUNNING_POLLS + 2
    world.clock.after_sleep = flicker

    result = world.service.upgrade(NEW)

    assert result["ok"] is True
    assert seen.count("running") >= NOT_RUNNING_POLLS


def test_a_stop_from_the_request_thread_ends_only_a_wait_past_the_slow_mark(world):
    """Interleaving: the request thread asks to stop while the job thread is held in its
    first poll before SLOW_START_SECONDS (refused), then again once the job thread has
    noted the mark (taken); the job then ends as not completed."""

    import threading

    first_poll = threading.Event()
    go_on = threading.Event()
    noted = threading.Event()

    class NotingProgress(Progress):
        def note_step(self, key, note):
            super().note_step(key, note)
            if note:
                noted.set()

    def hold_the_first_poll():
        if not first_poll.is_set():
            first_poll.set()
            go_on.wait(30)

    world.influx.answers_after = 10**9
    world.clock.after_sleep = hold_the_first_poll
    results = []
    job = threading.Thread(target=lambda: results.append(world.service.upgrade(NEW, progress=NotingProgress())))
    job.start()
    assert first_poll.wait(30)
    early = world.service.stop_waiting()
    go_on.set()
    assert noted.wait(30)
    late = world.service.stop_waiting()
    job.join(30)

    assert (early, late) == (False, True) and not job.is_alive()
    assert keys(results[0], "error") == ["verify"] and "Waiting was stopped" in results[0]["message"]
    assert world.service.stop_waiting() is False


def test_a_stop_during_the_way_back_puts_influxdb_back_and_no_second_stop_reaches_that(world):
    world.service.upgrade(NEW)
    real_start = world.influx.start

    def slow_previous(image):
        real_start(image)
        world.influx.answers_after = 10**6 if image == OLD else 0

    world.influx.start = slow_previous
    stops = []

    def stop_when_slow():
        if world.service.slow or stops:
            stops.append(world.service.stop_waiting())

    world.clock.after_sleep = stop_when_slow
    progress = Progress()

    result = world.service.rollback(OLD, progress=progress)

    assert stops[0] is True and not any(stops[1:])
    assert keys(result, "error") == ["start"] and "Waiting was stopped" in result["message"]
    assert f"InfluxDB is back on {NEW}" in result["message"]
    assert [note[:2] for note in progress.notes] == [("start", STILL_STARTING), ("start", PUTTING_BACK)]
    assert world.record()["state"] == UPGRADED and world.influx.image == NEW
    assert world.service.plan()["rollback"]["available"] is True


def test_putting_back_after_a_failed_way_back_gives_up_after_the_slow_mark_and_cannot_be_stopped(world):
    world.service.upgrade(NEW)
    world.backups.restore_error = RuntimeError("influx restore failed")
    real_start = world.influx.start

    def upgraded_never_answers(image):
        real_start(image)
        if image == NEW:
            world.influx.answers_after = 10**6

    world.influx.start = upgraded_never_answers
    stops = []
    world.clock.after_sleep = lambda: stops.append(world.service.stop_waiting())

    result = world.service.rollback(OLD)

    assert keys(result, "error") == ["restore"] and "has not answered within 15 minutes" in result["message"]
    assert world.record()["state"] == ROLLBACK_FAILED and not any(stops)
    assert len(world.clock.slept) == SLOW_START_SECONDS // READY_POLL_SECONDS


def test_an_influxdb_that_keeps_restarting_ends_the_wait(world):
    def crash_and_restart():
        world.influx.started += 1

    world.influx.answers_after = 10**6
    world.clock.after_sleep = crash_and_restart

    result = world.service.upgrade(NEW)

    assert keys(result, "error") == ["verify"] and "keeps restarting" in result["message"]
    assert len(world.clock.slept) == RESTARTS_ALLOWED + 1
    assert world.record()["state"] == FAILED


def test_a_check_that_raises_without_words_fails_the_upgrade(world, monkeypatch):
    def broken(*_args):
        raise RuntimeError()

    monkeypatch.setattr(world.service, "_verify", broken)

    result = world.service.upgrade(NEW)

    assert result["ok"] is False and keys(result, "error") == ["verify"]
    assert "failed unexpectedly" in result["message"] and world.record()["state"] == FAILED


def test_a_recheck_records_a_stopped_upgrade_as_upgraded_only_with_the_buckets_it_held(world):
    world.influx.answers_after = 10**6
    world.clock.after_sleep = lambda: world.service.slow and world.service.stop_waiting()
    world.service.upgrade(NEW)
    world.clock.after_sleep = None
    assert world.record()["state"] == FAILED

    waiting = world.service.recheck()
    assert waiting["ok"] is False and "does not answer yet" in waiting["message"]
    assert world.record()["state"] == FAILED
    world.influx.answers_after = 0
    held = list(world.influx.data["buckets"])
    world.influx.data["buckets"].remove("ems_1h")
    missing = world.service.recheck()
    assert missing["ok"] is False and "no longer lists ems_1h" in missing["message"]
    world.influx.data["buckets"] = held
    world.influx.renewed.add("ems_1h")
    anew = world.service.recheck()
    assert anew["ok"] is False and "lists ems_1h made anew" in anew["message"] and world.record()["state"] == FAILED
    world.influx.renewed.clear()

    done = world.service.recheck()

    assert done["ok"] is True and world.record()["state"] == UPGRADED
    assert world.service.recheck()["status"] == "refused"
    assert not any(call[0] in ("stop", "up", "set_aside", "restore") for call in world.calls[-6:])


def test_a_recheck_without_an_unfinished_upgrade_is_refused(world):
    assert world.service.recheck()["status"] == "refused"
    world.service.upgrade(NEW)
    assert world.service.recheck()["status"] == "refused"


def test_a_way_back_records_when_it_started_beside_the_upgrade_it_undoes(world):
    world.service.upgrade(NEW)
    upgraded = world.record()

    world.service.rollback(OLD)

    last = world.service.plan()["last"]
    assert last["state"] == ROLLED_BACK and isinstance(last["rollback_started_at"], str)
    assert last["started_at"] == upgraded["started_at"]


def test_a_bucket_made_anew_under_its_old_name_fails_the_upgrade(world):
    real_start = world.influx.start

    def renew_a_bucket(image):
        real_start(image)
        world.influx.renewed.add("ems_1h")

    world.influx.start = renew_a_bucket

    result = world.service.upgrade(NEW)

    assert keys(result, "error") == ["verify"] and "lists ems_1h made anew, not the buckets" in result["message"]
    assert world.record()["state"] == FAILED


def test_a_recheck_is_offered_by_the_plan_only_where_it_can_succeed(world):
    assert world.service.plan()["recheck"] is None
    world.compose.up_error = DockerError("compose_failed", "up failed")
    world.service.upgrade(NEW)
    world.compose.up_error = None
    view = world.service.plan()["recheck"]
    assert view["available"] is False and "Restart EMS now on Services starts the version" in view["reason"]
    assert world.service.recheck()["status"] == "refused"
    world.influx.start(NEW)
    assert world.service.plan()["recheck"] == {"available": True, "reason": None}
    world.influx.status = "exited"
    stopped = world.service.plan()["recheck"]
    assert stopped["available"] is False and "InfluxDB is not running" in stopped["reason"]
    world.influx.status = "running"
    (world.root / "docker-compose.yml").write_text(
        (world.root / "docker-compose.yml").read_text(encoding="utf-8").replace(NEW, OLD), encoding="utf-8"
    )
    view = world.service.plan()["recheck"]
    assert view["available"] is False and "the upgrade did not change it" in view["reason"]


def test_a_record_written_before_ids_were_kept_is_checked_again_and_rolled_back_by_name(world):
    world.influx.answers_after = 10**6
    world.clock.after_sleep = lambda: world.service.slow and world.service.stop_waiting()
    world.service.upgrade(NEW)
    world.clock.after_sleep = None
    record = world.record()
    for key in ("bucket_ids", "task_ids"):
        record.pop(key)
    world.service.store.write({key: value for key, value in record.items() if key != "version"})
    world.influx.answers_after = 0
    world.influx.renewed.add("ems_1h")

    rechecked = world.service.recheck()
    rolled = world.service.rollback(OLD)

    assert rechecked["ok"] is True and world.service.plan()["last"]["state"] == ROLLED_BACK
    assert rolled["ok"] is True and world.influx.image == OLD


def test_only_the_record_of_the_running_job_is_said_to_be_running_now(world):
    """Interleaving: a way back starts from an upgrade record a restarted Admin left
    running; the plan read before the way back writes its own record names that old
    record not live, the plan read after it names the way back live."""

    world.influx.answers_after = 10**6
    world.clock.after_sleep = lambda: world.service.slow and world.service.stop_waiting()
    world.service.upgrade(NEW)
    world.clock.after_sleep = None
    world.influx.answers_after = 0
    left = world.record()
    left["state"] = RUNNING
    world.service.store.write({key: value for key, value in left.items() if key != "version"})
    seen = []
    real_check, real_set_aside = world.backups.check_influx_restore, world.docker.set_aside_influx_data

    def before_its_record(name):
        seen.append(world.service.plan())
        return real_check(name)

    def after_its_record(*args):
        seen.append(world.service.plan())
        return real_set_aside(*args)

    world.backups.check_influx_restore = before_its_record
    world.docker.set_aside_influx_data = after_its_record

    world.service.rollback(OLD)

    assert [(plan["state"], plan["last"]["state"], plan["live"]) for plan in seen] == [
        ("busy", RUNNING, False),
        ("busy", "rolling_back", True),
    ]
    assert world.service.plan()["live"] is False


def test_a_task_name_listed_twice_keeps_its_ids_whatever_the_order(world):
    real_exec = world.docker.exec_capture
    order = {"reverse": False}

    def two_tasks_of_one_name(name, command, timeout=30):
        code, out, detail = real_exec(name, command, timeout)
        if command[0] == "sh" and "task list" in command[2] and code == 0:
            listed = json.loads(out) + [{"name": TASKS[0], "id": "duplicate-kept-off"}]
            out = json.dumps(listed[::-1] if order["reverse"] else listed)
        return code, out, detail

    world.docker.exec_capture = two_tasks_of_one_name
    world.influx.start = (lambda real: lambda image: (real(image), order.update(reverse=True)))(world.influx.start)

    result = world.service.upgrade(NEW)

    assert result["ok"] is True
    assert world.record()["task_ids"][TASKS[0]] == sorted([f"{TASKS[0]}-held", "duplicate-kept-off"])


def test_a_bucket_the_new_version_lost_fails_the_upgrade(world):
    real_start = world.influx.start

    def lose_a_bucket(image):
        real_start(image)
        world.influx.data["buckets"].remove("ems_1h")

    world.influx.start = lose_a_bucket

    result = world.service.upgrade(NEW)

    assert keys(result, "error") == ["verify"] and "ems_1h" in result["message"]
    assert world.record()["state"] == FAILED


def test_a_container_that_reports_another_version_fails_the_upgrade(world):
    world.influx.version_override = "2.7.12"

    result = world.service.upgrade(NEW)

    assert keys(result, "error") == ["verify"] and "reports 2.7.12" in result["message"]


def test_the_plan_says_busy_while_a_job_runs(world):
    seen = []
    world.docker.on_pull = lambda: seen.append(world.service.plan()["state"])

    world.service.upgrade(NEW)

    assert seen == ["busy"]
    assert world.service.plan()["state"] == "current"


def _rewrite_record(world, **changes):
    record = world.record()
    record.update(changes)
    world.service.store.write({key: value for key, value in record.items() if key != "version"})


def test_an_upgrade_the_admin_did_not_finish_offers_the_way_back_and_blocks_nothing(world):
    """Interleaving: the Admin restarts after starting the new version, before it settled the record."""

    world.service.upgrade(NEW)
    _rewrite_record(world, state=RUNNING, finished_at=None)

    plan = world.service.plan()

    assert plan["state"] == "current" and plan["last"]["state"] == RUNNING
    assert plan["rollback"]["available"] is True
    (world.root / "docker-compose.yml").write_text(compose_text(NEW), encoding="utf-8")
    world.service._target_image = lambda: "influxdb:2.9.2"
    assert world.service.plan()["state"] == "available"


def test_an_upgrade_stopped_before_it_changed_compose_can_run_again(world):
    """Interleaving: the Admin restarts between writing the record and docker-compose.yml."""

    world.service.upgrade(NEW)
    _rewrite_record(world, state=RUNNING, finished_at=None)
    (world.root / "docker-compose.yml").write_text(compose_text(OLD), encoding="utf-8")
    world.influx.image = OLD

    plan = world.service.plan()

    assert plan["state"] == "available" and plan["rollback"]["available"] is False
    assert world.service.upgrade(NEW)["ok"] is True


def test_a_way_back_that_did_not_finish_offers_nothing_until_looked_at(world):
    world.service.upgrade(NEW)
    _rewrite_record(world, state="rolling_back")

    plan = world.service.plan()

    assert plan["state"] == "blocked" and BACKUP in plan["message"]
    assert "influxdb-upgrade.json" in plan["message"]
    assert plan["rollback"] is None
    assert world.service.upgrade(NEW)["status"] == "refused"


# --- the way back -------------------------------------------------------------------


def test_the_previous_image_refuses_converted_data():
    """The premise: starting the old image on what the new one converted fails."""

    influx = Influx()
    influx.start(NEW)
    influx.start(OLD)

    assert influx.status == "restarting"


def test_the_way_back_restores_the_backup_on_the_previous_version(world):
    world.service.upgrade(NEW)
    world.influx.data["buckets"].append("written_after_the_upgrade")
    world.calls.clear()
    progress = Progress()

    result = world.service.rollback(OLD, progress=progress)

    assert result["ok"] is True, result
    assert keys(result) == [key for key, _label in ROLLBACK_STEPS]
    assert [call[0] for call in world.calls if call[0] in ("stop", "set_aside", "up", "restore")] == [
        "stop", "set_aside", "up", "restore",
    ]
    assert [call for call in world.calls if call[0] == "up"] == [("up", ("influxdb",), True, True)]
    _kind, image, name = next(call for call in world.calls if call[0] == "set_aside")
    assert image == OLD and name.startswith("influxdb-2.9.1-set-aside-")
    assert world.influx.set_aside["buckets"][-1] == "written_after_the_upgrade"
    assert world.compose_image() == OLD
    assert (world.influx.image, world.influx.status) == (OLD, "running")
    assert world.influx.data["buckets"] == BUCKETS
    record = world.record()
    assert record["state"] == ROLLED_BACK and record["set_aside"] == name
    assert name in result["message"]
    plan = world.service.plan()
    assert plan["state"] == "available" and plan["rollback"] is None


def test_the_way_back_restores_only_once_the_server_that_stays_answers(world):
    """An empty InfluxDB sets itself up on a temporary server first and points
    the CLI configuration at it; on a slow board that server answered for the
    real one and the restore found nobody listening (VM shaped like a Pi 3B+,
    2026-11-19, three of three ways back)."""

    world.service.upgrade(NEW)
    world.influx.setup_polls = 3
    world.calls.clear()

    result = world.service.rollback(OLD, progress=Progress())

    assert result["ok"] is True, result
    pings = [index for index, call in enumerate(world.calls) if call == ("exec", ("influx", "ping"))]
    restore = world.calls.index(("restore", world.record()["backup"]))
    assert len([index for index in pings if index < restore]) >= 4
    assert world.influx.setting_up == 0
    assert world.record()["state"] == ROLLED_BACK


def test_the_checks_give_a_board_under_load_two_minutes_per_command(world):
    world.service.upgrade(NEW)

    assert world.influx.timeouts[("influxd", "version")] >= 120
    assert world.influx.timeouts[("sh", "-c")] >= 120


def test_the_way_back_downloads_the_previous_image_only_when_it_is_gone(world):
    world.service.upgrade(NEW)
    world.docker.local_images.discard(OLD)
    world.calls.clear()

    world.service.rollback(OLD)

    assert ("pull", OLD) in world.calls


@pytest.mark.parametrize("image", [NEW, "influxdb:2.7.12"])
def test_the_way_back_runs_only_to_the_version_the_operator_confirmed(world, image):
    world.service.upgrade(NEW)
    world.calls.clear()

    result = world.service.rollback(image)

    assert result["status"] == "refused" and changes(world.calls) == []


def test_the_way_back_needs_the_backup_it_restores(world):
    world.service.upgrade(NEW)
    world.backups.archives.clear()
    world.calls.clear()

    plan = world.service.plan()
    result = world.service.rollback(OLD)

    assert plan["rollback"]["available"] is False and BACKUP in plan["rollback"]["reason"]
    assert result["status"] == "refused" and changes(world.calls) == []


def test_the_way_back_needs_the_setup_values_an_empty_influxdb_starts_from(world):
    world.service.upgrade(NEW)
    (world.root / "config" / "influxdb.env").write_text(ENV.replace("DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=tok", "DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=other"), encoding="utf-8")
    world.calls.clear()

    result = world.service.rollback(OLD)

    assert result["status"] == "refused" and "EMS token" in result["message"]
    assert changes(world.calls) == [] and world.compose_image() == NEW


def test_no_way_back_without_an_upgrade(world):
    result = world.service.rollback(OLD)

    assert result["status"] == "refused" and changes(world.calls) == []


def test_a_failed_restore_puts_influxdb_back_as_it_was(world):
    world.service.upgrade(NEW)
    upgraded = world.record()
    world.influx.data["buckets"].append("written_after_the_upgrade")
    world.backups.restore_error = RuntimeError("influx restore failed: checksum mismatch")

    result = world.service.rollback(OLD)

    assert keys(result, "error") == ["restore"] and "checksum mismatch" in result["message"]
    assert "Roll back can be tried again" in result["message"]
    _kind, _image, aside, attempt = next(call for call in world.calls if call[0] == "put_back")
    assert aside.startswith("influxdb-2.9.1-set-aside-") and attempt.startswith("influxdb-2.7-failed-way-back-")
    assert attempt in result["message"]
    assert world.compose_image() == NEW
    assert (world.influx.image, world.influx.status) == (NEW, "running")
    assert world.influx.data["buckets"][-1] == "written_after_the_upgrade"
    record = world.record()
    assert record["state"] == UPGRADED and record["finished_at"] == upgraded["finished_at"]
    assert "restore" in record["message"]
    plan = world.service.plan()
    assert plan["state"] == "current" and plan["rollback"]["available"] is True


def test_a_failed_set_aside_starts_influxdb_again_and_changes_nothing(world):
    world.service.upgrade(NEW)

    def refuse(*_args):
        raise DockerError("influx_set_aside_failed", "Could not move InfluxDB's data directory.")

    world.docker.set_aside_influx_data = refuse

    result = world.service.rollback(OLD)

    assert keys(result, "error") == ["set_aside"] and result["changed"] is False
    assert world.influx.status == "running" and world.compose_image() == NEW
    assert not any(call[0] == "put_back" for call in world.calls)
    assert world.record()["state"] == UPGRADED
    assert world.service.plan()["rollback"]["available"] is True


def test_a_way_back_that_cannot_be_undone_blocks_both_directions(world):
    """Interleaving: the restore fails, and Docker refuses the move that would undo the way back."""

    world.service.upgrade(NEW)
    world.backups.restore_error = RuntimeError("restore timed out")

    def refuse(*_args):
        raise DockerError("influx_set_aside_failed", "Could not move InfluxDB's data directory.")

    world.docker.put_back_influx_data = refuse

    result = world.service.rollback(OLD)

    record = world.record()
    assert record["state"] == ROLLBACK_FAILED
    assert f"data/{record['set_aside']}" in result["message"] and BACKUP in result["message"]
    assert world.influx.set_aside["format"] == "2.9"
    plan = world.service.plan()
    assert plan["state"] == "blocked" and plan["rollback"] is None
    assert record["set_aside"] in plan["message"] and "influxdb-upgrade.json" in plan["message"]
    world.calls.clear()
    assert world.service.upgrade(NEW)["status"] == "refused"
    assert world.service.upgrade(plan["target_image"] or NEW)["status"] == "refused"
    assert changes(world.calls) == []


def test_an_unexpected_error_on_the_way_back_is_undone_too(world):
    world.service.upgrade(NEW)

    def boom(*_args, **_kwargs):
        raise RuntimeError("socket closed")

    world.backups.restore_influx_archive = boom

    result = world.service.rollback(OLD)

    assert keys(result, "error") == ["restore"] and "socket closed" in result["message"]
    assert world.record()["state"] == UPGRADED
    assert world.compose_image() == NEW


@pytest.mark.parametrize(
    ("arrange", "fragment"),
    [
        (lambda w: (w.root / "data" / "influxdb").rmdir(), "data/influxdb/ is not a plain directory"),
        (lambda w: (w.root / "docker-compose.yml").write_text(
            (w.root / "docker-compose.yml").read_text(encoding="utf-8").replace("./data/influxdb:", "./influx:"),
            encoding="utf-8"), "does not mount ./data/influxdb"),
        (lambda w: setattr(w.backups, "check_error", RuntimeError("archive checksums are invalid")), "checksums are invalid"),
        (lambda w: w.disk.update(free=15 * 2**20), "needs about 20 MiB free"),
    ],
)
def test_the_way_back_checks_what_it_needs_before_it_changes_anything(world, arrange, fragment):
    world.service.upgrade(NEW)
    world.calls.clear()
    arrange(world)

    result = world.service.rollback(OLD)

    assert result["status"] == "refused" and fragment in result["message"]
    assert changes(world.calls) == [] and world.record()["state"] == UPGRADED


def test_a_symlinked_data_directory_is_refused_before_anything_moves(world, tmp_path):
    world.service.upgrade(NEW)
    real = tmp_path / "ssd-data"
    (world.root / "data").rename(real)
    (world.root / "data").symlink_to(real, target_is_directory=True)
    world.calls.clear()

    result = world.service.rollback(OLD)

    assert result["status"] == "refused" and "data/ is not a plain directory" in result["message"]
    assert changes(world.calls) == []


def test_a_running_container_that_is_not_the_compose_image_is_not_upgraded(world):
    world.influx.image = "influxdb:2.9.1"

    plan = world.service.plan()

    assert plan["state"] == "mismatch" and "influxdb:2.9.1" in plan["message"]
    assert world.service.upgrade(NEW)["status"] == "refused"


def test_an_installation_without_a_compose_file_shows_nothing(world):
    (world.root / "docker-compose.yml").unlink()

    assert world.service.plan()["state"] == "not_installed"


def test_a_compose_write_that_fails_leaves_the_file_whole(world, monkeypatch):
    from admin import influx_upgrade

    original = (world.root / "docker-compose.yml").read_text(encoding="utf-8")

    def disk_full(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(influx_upgrade, "atomic_write_bytes", disk_full)

    result = world.service.upgrade(NEW)

    assert keys(result, "error") == ["update_compose"] and result["changed"] is False
    assert (world.root / "docker-compose.yml").read_text(encoding="utf-8") == original
    assert not any(call[0] == "up" for call in world.calls)


def test_the_compose_file_keeps_its_mode(world):
    path = world.root / "docker-compose.yml"
    path.chmod(0o640)

    world.service.upgrade(NEW)

    assert path.stat().st_mode & 0o777 == 0o640


def test_putting_back_an_upgrade_that_never_answered_does_not_block(world):
    """The way back is most wanted when the new version keeps crashing; putting
    that state back is a success even though InfluxDB does not answer, as before."""

    def crash(image):
        world.influx.image = image
        world.influx.status = "restarting" if image == NEW else "running"
        if world.influx.data is None:
            world.influx.data = {"format": "2.7", "buckets": ["_monitoring", "_tasks"], "tasks": []}

    world.influx.start = crash
    world.service.upgrade(NEW)
    assert world.record()["state"] == FAILED
    world.backups.restore_error = RuntimeError("restore timed out")

    result = world.service.rollback(OLD)

    assert "does not answer, as it did not before" in result["message"]
    assert world.record()["state"] == FAILED
    assert world.compose_image() == NEW and world.influx.data["buckets"] == BUCKETS
    assert world.service.plan()["rollback"]["available"] is True


def test_putting_back_an_upgrade_that_answered_and_no_longer_does_blocks(world):
    world.service.upgrade(NEW)
    world.backups.restore_error = RuntimeError("restore timed out")
    real_start = world.influx.start

    def dead_after_put_back(image):
        real_start(image)
        if world.influx.data and world.influx.data.get("format") == "2.9" and image == NEW:
            world.influx.status = "restarting"

    world.influx.start = dead_after_put_back

    result = world.service.rollback(OLD)

    record = world.record()
    assert record["state"] == ROLLBACK_FAILED
    assert "Putting InfluxDB back failed too" in result["message"]
    assert record["set_aside"] is None and record["attempt"].startswith("influxdb-2.7-failed-way-back-")
    where = f"back in data/influxdb; what the way back started is in data/{record['attempt']}"
    assert where in result["message"]
    plan = world.service.plan()
    assert plan["state"] == "blocked" and where in plan["message"]


def test_a_put_back_that_cannot_stop_influxdb_says_the_data_is_still_set_aside(world):
    world.service.upgrade(NEW)
    world.backups.restore_error = RuntimeError("restore timed out")
    stops = []

    def stop(root, services=(), profiles=()):
        stops.append(services)
        if len(stops) > 1:
            raise DockerError("compose_stop_failed", "Could not stop the optional feature container.")
        world.influx.status = "exited"

    world.compose.stop = stop

    result = world.service.rollback(OLD)

    record = world.record()
    assert record["state"] == ROLLBACK_FAILED and record["set_aside"]
    assert f"whole in data/{record['set_aside']}; data/influxdb holds what the way back started" in result["message"]
    assert not any(call[0] == "put_back" for call in world.calls)


def test_a_rename_that_happened_although_the_command_failed_is_put_back(world):
    """Interleaving: the move succeeds, the mkdir after it fails, Docker reports an error."""

    world.service.upgrade(NEW)
    real = world.docker.set_aside_influx_data

    def moved_then_failed(root, image, name):
        real(root, image, name)
        (world.root / "data" / "influxdb").rmdir()
        raise DockerError("influx_set_aside_failed", "Could not move InfluxDB's data directory.")

    world.docker.set_aside_influx_data = moved_then_failed

    result = world.service.rollback(OLD)

    assert keys(result, "error") == ["set_aside"]
    assert any(call[0] == "put_back" for call in world.calls)
    assert (world.root / "data" / "influxdb").is_dir()
    assert world.record()["state"] == UPGRADED and world.influx.data["format"] == "2.9"
    assert sorted(path.name for path in (world.root / "data").iterdir()) == ["influxdb"]
    assert "failed-way-back" not in result["message"] and world.record().get("kept", []) == []


def test_data_no_check_can_find_after_a_failed_rename_blocks(world):
    world.service.upgrade(NEW)

    def vanished(root, image, name):
        (world.root / "data" / "influxdb").rmdir()
        raise DockerError("influx_set_aside_failed", "Could not move InfluxDB's data directory.")

    world.docker.set_aside_influx_data = vanished
    world.calls.clear()

    result = world.service.rollback(OLD)

    assert world.record()["state"] == ROLLBACK_FAILED
    assert "Neither data/influxdb nor data/influxdb-2.9.1-set-aside-" in result["message"]
    assert not any(call[0] == "up" for call in world.calls)
    assert world.service.plan()["state"] == "blocked"


def test_the_directories_a_way_back_set_aside_stay_named_in_every_later_record(world):
    world.service.upgrade(NEW)
    world.service.rollback(OLD)
    aside = world.record()["set_aside"]

    assert world.service.plan()["last"]["kept"] == [aside]
    assert world.service.upgrade(NEW)["ok"] is True
    assert world.record()["kept"] == [aside]
    assert world.service.plan()["last"]["kept"] == [aside]


def test_a_set_aside_name_that_cannot_be_made_fresh_moves_nothing(world, monkeypatch):
    from admin import influx_upgrade

    world.service.upgrade(NEW)
    monkeypatch.setattr(influx_upgrade.secrets, "token_hex", lambda _n: "abcdef")
    monkeypatch.setattr(influx_upgrade, "_stamp", lambda _now: "20261118T020000Z")
    (world.root / "data" / "influxdb-2.9.1-set-aside-20261118t020000z-abcdef").mkdir()
    world.calls.clear()

    result = world.service.rollback(OLD)

    assert keys(result, "error") == ["set_aside"] and "No free name" in result["message"]
    assert not any(call[0] in ("stop", "set_aside", "put_back") for call in world.calls)
    assert world.record()["state"] == UPGRADED and world.influx.status == "running"


def test_a_put_back_whose_moves_happened_before_its_command_failed_says_so(world):
    """Interleaving: both renames of the put-back complete, then Docker reports an error."""

    world.service.upgrade(NEW)
    world.backups.restore_error = RuntimeError("restore timed out")
    real = world.docker.put_back_influx_data

    def moved_then_failed(root, image, aside, attempt):
        real(root, image, aside, attempt)
        raise DockerError("influx_set_aside_failed", "Could not move InfluxDB's data directory.")

    world.docker.put_back_influx_data = moved_then_failed

    result = world.service.rollback(OLD)

    record = world.record()
    assert record["state"] == ROLLBACK_FAILED
    assert record["set_aside"] is None and (world.root / "data" / record["attempt"]).is_dir()
    assert f"back in data/influxdb; what the way back started is in data/{record['attempt']}" in result["message"]


def test_without_a_working_way_back_no_upgrade_is_offered(world):
    env = world.root / "config" / "influxdb.env"
    env.write_text(env.read_text(encoding="utf-8").replace("DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=tok", "DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=distinct"), encoding="utf-8")

    plan = world.service.plan()

    assert plan["state"] == "no_way_back" and "EMS token" in plan["message"]
    assert world.service.upgrade(NEW)["status"] == "refused" and changes(world.calls) == []


def test_every_compose_change_is_reported_to_the_setup_marker(world):
    seen = []
    world.service._on_image_changed = seen.append

    world.service.upgrade(NEW)
    world.service.rollback(OLD)

    assert seen == [NEW, OLD]


def test_a_stopped_container_of_another_version_is_a_mismatch_too(world):
    """A failed Setup can leave docker-compose.yml naming 2.9.1 beside a stopped 2.7 container;
    the next start would convert the data without a backup, so nothing is offered."""

    (world.root / "docker-compose.yml").write_text(compose_text(NEW), encoding="utf-8")
    world.influx.status = "exited"

    plan = world.service.plan()

    assert plan["state"] == "mismatch" and OLD in plan["message"]


@pytest.mark.parametrize(
    "image",
    ["influxdb:latest", "influxdb:2", "influxdb@sha256:" + "a" * 64, "docker.io/library/influxdb:2.10.0", None],
)
def test_a_container_of_an_image_the_admin_cannot_compare_is_a_mismatch(world, image):
    """Whatever the container was made from, unless it is what docker-compose.yml names, nothing is upgraded."""

    world.influx.image = image

    plan = world.service.plan()

    assert plan["state"] == "mismatch"
    assert world.service.upgrade(NEW)["status"] == "refused"


def test_a_container_left_on_another_image_after_the_recreate_fails_the_upgrade(world):
    real_start = world.influx.start

    def stale(image):
        real_start(image)
        world.influx.image = "influxdb:2.9.0"

    world.influx.start = stale
    world.influx.version_override = "2.9.1"

    result = world.service.upgrade(NEW)

    assert keys(result, "error") == ["verify"] and "runs influxdb:2.9.0, not influxdb:2.9.1" in result["message"]



# --- memory ------------------------------------------------------------------


def test_an_upgrade_is_not_offered_without_the_memory_its_first_start_needs(world):
    """The Pi-3-sized VM run without swap: 70 MB free beside 372 MB InfluxDB went through with 18 MB left."""

    _short_of_memory(world)

    plan = world.service.plan()

    needed = math.ceil(372 * MEMORY_GROWTH_SHARE) + MEMORY_RESERVE_MB
    assert plan["state"] == "low_memory"
    assert plan["memory"] == {"influx_mb": 372, "needed_mb": needed, "available_mb": 70}
    assert f"{needed} MB" in plan["message"] and "70 MB" in plan["message"]
    assert "swap" in plan["message"] and "retention" in plan["message"]


def test_swap_counts_as_free_memory(world):
    _short_of_memory(world, swap_mb=285)

    assert world.service.plan()["state"] == "available"


@pytest.mark.parametrize(("zram_mb", "state"), [(150, "available"), (100, "low_memory")])
def test_free_compressed_swap_counts_half(world, zram_mb, state):
    """A zram page is compressed into the memory MemAvailable already counts."""

    _short_of_memory(world, zram_mb=zram_mb)

    plan = world.service.plan()

    assert plan["memory"]["available_mb"] == 70 + zram_mb // 2
    assert plan["state"] == state


def test_without_the_swap_list_free_swap_counts_half(world):
    _short_of_memory(world, swap_mb=285)
    world.service._swaps = lambda: None

    assert world.service.plan()["memory"]["available_mb"] == 70 + 285 // 2


def test_what_influxdb_has_in_swap_counts_as_what_it_holds(world):
    _short_of_memory(world, held_mb=200, free_mb=150)
    world.influx.swap_kb = 300 * 1024

    plan = world.service.plan()

    assert plan["memory"]["influx_mb"] == 500
    assert plan["state"] == "low_memory"


def test_a_database_grown_past_the_defaults_is_measured_not_guessed(world):
    _short_of_memory(world, held_mb=3000, free_mb=500)

    plan = world.service.plan()

    assert plan["state"] == "low_memory" and plan["memory"]["needed_mb"] == 664


def test_short_of_memory_the_upgrade_stops_before_anything_changes(world):
    _short_of_memory(world)

    result = world.service.upgrade(NEW)

    assert result["ok"] is False and result["changed"] is False
    assert keys(result, "error") == ["preflight"] and "MB" in result["message"]
    assert ("backup", "influxdb") not in world.calls and world.record() is None
    assert world.compose_image() == OLD


@pytest.mark.parametrize("unreadable", ["meminfo", "status", "exec"])
def test_memory_that_cannot_be_read_does_not_hold_the_upgrade_back(world, unreadable):
    _short_of_memory(world)
    if unreadable == "meminfo":
        world.memory.clear()
    elif unreadable == "status":
        world.influx.held_kb = None
    else:
        world.docker.exec_capture = lambda *_args, **_kwargs: (_ for _ in ()).throw(DockerError("exec_failed", "no"))

    plan = world.service.plan()

    assert plan["state"] == "available" and plan["memory"] is None


def test_process_memory_is_anonymous_memory_plus_swap():
    assert _process_memory_mb("RssAnon:\t 409600 kB\nVmSwap:\t102400 kB\n") == 500
    assert _process_memory_mb("VmRSS:\t204800 kB\n") == 200
    assert _process_memory_mb("Name:\tinfluxd\n") is None
    assert _process_memory_mb(None) is None
    assert _process_memory_mb("Name:\ttini\nRssAnon:\t1024 kB\n") is None
    assert _process_memory_mb("Name:\tinfluxd\nRssAnon:\t1024 kB\n") == 1


def test_the_swap_list_is_read_in_megabytes(tmp_path):
    path = tmp_path / "swaps"
    path.write_text(
        "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
        "/dev/zram0                              partition\t461820\t\t172904\t\t100\n"
        "/var/lib/ems-appliance-swap/swapfile    file\t\t2097148\t\t0\t\t10\n"
    )

    assert read_swaps(path) == [("/dev/zram0", 450, 168), ("/var/lib/ems-appliance-swap/swapfile", 2047, 0)]
    assert read_swaps(tmp_path / "missing") is None


def test_meminfo_is_read_in_megabytes(tmp_path):
    path = tmp_path / "meminfo"
    path.write_text("MemTotal:  924804 kB\nMemAvailable:  71680 kB\nSwapFree:  0 kB\nHugePages_Total: 0\n")

    assert read_meminfo(path) == {"MemTotal": 903, "MemAvailable": 70, "SwapFree": 0, "HugePages_Total": 0}
    assert read_meminfo(tmp_path / "missing") is None


def test_a_new_version_the_system_stops_for_lack_of_memory_says_so(world):
    def killed(image):
        world.influx.image = image
        world.influx.status = "exited"
        world.influx.oom_killed = True

    world.influx.start = killed

    result = world.service.upgrade(NEW)

    assert keys(result, "error") == ["verify"]
    assert "stopped for lack of memory" in result["message"] and "Roll back restores" in result["message"]


def test_a_new_version_the_kernel_kills_is_named_as_killed(world):
    """Where the memory cgroup is off, as on a Raspberry Pi, Docker reports no OOM, only exit code 137."""

    def killed(image):
        world.influx.image = image
        world.influx.status = "exited"
        world.influx.status_detail = "Exited (137) 3 seconds ago"

    world.influx.start = killed

    result = world.service.upgrade(NEW)

    assert keys(result, "error") == ["verify"]
    assert "InfluxDB was killed (Exited (137)" in result["message"] and "short of memory" in result["message"]


def test_a_restart_during_the_check_is_named_instead_of_the_listing_it_broke(world):
    original = world.docker.exec_capture

    def restart_while_listing(name, command, timeout=30):
        if command[0] == "sh" and world.influx.image == NEW and world.influx.started == 1:
            world.influx.started += 1
            return 1, "", "connection refused"
        return original(name, command, timeout)

    world.docker.exec_capture = restart_while_listing

    result = world.service.upgrade(NEW)

    assert keys(result, "error") == ["verify"]
    assert "restarted while it was checked" in result["message"]
