# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Move the bundled InfluxDB to the version this Admin build ships, behind a verified backup.

InfluxDB 2.x upgrades in place: the new ``influxd`` converts the metadata it
finds and keeps the time series as they are. The step is one way. From 2.8 on
the conversion hashes the API tokens, and an older ``influxd`` refuses the
converted data outright, so the way back is not the old image but the backup
this upgrade takes first: the converted directory is set aside (never
deleted), the previous version starts empty from ``config/influxdb.env``, and
the backup is restored into it.

An upgrade runs only on an operator's confirmation, and it stops before
changing anything when InfluxDB is not running, cannot list its buckets and
tasks with the EMS token, the new image cannot be downloaded, or the backup
cannot be verified. Its record (``influxdb-upgrade.json`` in the Admin data
directory) names both versions, the backup and the buckets and tasks InfluxDB
held; it is what offers the way back and what that way back checks against.
Nothing rolls back by itself. A first start of the new version converts the
data at a speed nobody has measured on every board, so time alone never fails
it: past SLOW_START_SECONDS the job notes that InfluxDB is still starting and
waits on until InfluxDB answers or its container does not keep running, and
the operator may stop the wait, which ends the run as not completed and offers
the way back. The way back checks everything it needs before it changes anything,
and when a later step fails it puts InfluxDB back as it was before it; only
when that fails too does it leave a record that offers nothing more until the
operator has looked.

An upgrade is offered only where the memory is there for it. The new
version's first start loads every shard again, and in a large-data
measurement on a Pi-sized machine it peaked about 15 % above the process it
replaced, while the backup runs beside the old one. So the free memory, RAM
and swap together, has to cover MEMORY_GROWTH_SHARE of what InfluxDB holds now
plus MEMORY_RESERVE_MB; with less the plan says ``low_memory`` and the upgrade
does not start. What InfluxDB holds is read from its own process, so a
database grown far past the defaults is measured, not guessed. Where either
figure cannot be read the check gives way. A container Docker stopped for
lack of memory is named as such in the failure.

Import-side-effect-free.
"""

import json
import math
import re
import secrets
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ems.config import resolve_influxdb_config
from ems.file_io import atomic_write_bytes
from ems.influx_image import compose_image, is_upgrade, parse_image, pinned_image, replace_image
from ems.influx_setup import DOCKER_FIRST_SECRET_FILE, parse_env_file

from admin.container_actions import ANALYTICS_PROFILE, INFLUX_SERVICE
from admin.deployment import DockerCli, DockerCompose, DockerError
from admin.embedded_resources import DEFAULT_RESOURCES_DIR
from admin.install_context import detect_install_context
from admin.maintenance import run_maintenance_overview
from admin.models import utc_now_iso

RECORD_FILE = "influxdb-upgrade.json"
RECORD_VERSION = 1

# The Admin image ships the release resources of its own build; a source
# checkout has the pin at its root.
_PIN_ROOTS = (Path(DEFAULT_RESOURCES_DIR), Path(__file__).resolve().parents[1])

# A first start of a newer version converts the metadata and loads every shard,
# at a speed nobody has measured on a Pi with years of history. Past this mark
# the job says it is still starting and keeps waiting: only a container that
# does not keep running or keeps restarting, or the operator stopping the wait, ends it.
SLOW_START_SECONDS = 900
READY_POLL_SECONDS = 5
STILL_STARTING = "still_starting"
PUTTING_BACK = "putting_back"
# A container Docker reports as not running this often in a row is not starting, it is failing.
NOT_RUNNING_POLLS = 6
RESTARTS_ALLOWED = 2
MEMORY_GROWTH_SHARE = 0.2
MEMORY_RESERVE_MB = 64
CHECK_TIMEOUT_SECONDS = 120
INFLUX_URL = "http://localhost:8086"

RUNNING = "running"
UPGRADED = "upgraded"
FAILED = "failed"
ROLLING_BACK = "rolling_back"
ROLLED_BACK = "rolled_back"
ROLLBACK_FAILED = "rollback_failed"
RECORD_STATES = (RUNNING, UPGRADED, FAILED, ROLLING_BACK, ROLLED_BACK, ROLLBACK_FAILED)
# An upgrade that did not finish counts as a failed one: the way back is
# offered while docker-compose.yml names its image, and a later upgrade may
# run. A way back that did not finish, or failed and could not put InfluxDB
# back, may have left it empty, so nothing is offered until the operator has
# looked.
ROLLBACK_FROM_STATES = (RUNNING, UPGRADED, FAILED)
BLOCKING_STATES = (ROLLING_BACK, ROLLBACK_FAILED)
# The way back holds the restored data beside the archive and the staging the
# restore unpacks it into; the converted data is only renamed.
RESTORE_SPACE_FACTOR = 2
_MOUNT_RE = re.compile(r"^\s*-\s*[\"']?\./data/influxdb:/var/lib/influxdb2[\"']?\s*$", re.MULTILINE)

UPGRADE_STEPS = (
    ("preflight", "Check the installation"),
    ("inventory", "List buckets and tasks"),
    ("pull", "Download the new InfluxDB image"),
    ("backup", "Back up InfluxDB and verify the backup"),
    ("update_compose", "Name the new image in docker-compose.yml"),
    ("recreate", "Start InfluxDB on the new image"),
    ("verify", "Check InfluxDB on the new image"),
)
ROLLBACK_STEPS = (
    ("preflight", "Check the backup and the installation"),
    ("pull", "Make the previous InfluxDB image available"),
    ("stop", "Stop InfluxDB"),
    ("set_aside", "Set the converted data aside"),
    ("update_compose", "Name the previous image in docker-compose.yml"),
    ("start", "Start the previous InfluxDB empty"),
    ("restore", "Restore the backup"),
    ("verify", "Check InfluxDB after the restore"),
)

_INIT_KEYS = (
    "DOCKER_INFLUXDB_INIT_USERNAME",
    "DOCKER_INFLUXDB_INIT_PASSWORD",
    "DOCKER_INFLUXDB_INIT_ORG",
    "DOCKER_INFLUXDB_INIT_BUCKET",
    "DOCKER_INFLUXDB_INIT_ADMIN_TOKEN",
)
_VERSION_RE = re.compile(r"\bv?(\d+\.\d+\.\d+)\b")
# ``$0`` is the organisation; the token never leaves the container's environment.
_LIST_SCRIPT = (
    'exec influx {what} list --json --host ' + INFLUX_URL + ' '
    '--org "$0" --token "$INFLUXDB_TOKEN"'
)
_PING = ("influx", "ping", "--host", INFLUX_URL)


class InfluxUpgradeError(Exception):
    """A step failed; the message is safe to show."""


def default_target_image():
    """The InfluxDB image this Admin build ships, or None when it names none."""

    for root in _PIN_ROOTS:
        try:
            return pinned_image(root)
        except (OSError, ValueError):
            continue
    return None


def _stamp(now):
    return now.strftime("%Y%m%dT%H%M%SZ")


class UpgradeRecordStore:
    """The one record of the last InfluxDB upgrade, in the Admin data directory."""

    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        """The record, None when there is none; ValueError when it cannot be trusted."""

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise ValueError(f"{self.path} cannot be read: {exc}") from exc
        if not isinstance(data, dict) or data.get("version") != RECORD_VERSION:
            raise ValueError(f"{self.path} is not a record this Admin knows")
        if data.get("state") not in RECORD_STATES:
            raise ValueError(f"{self.path} names an unknown state")
        for key in ("from_image", "to_image"):
            if parse_image(data.get(key)) is None:
                raise ValueError(f"{self.path} names no InfluxDB image as {key}")
        for key in ("buckets", "tasks"):
            values = data.get(key)
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise ValueError(f"{self.path} holds no {key} list")
        if not isinstance(data.get("backup"), str) or not data["backup"]:
            raise ValueError(f"{self.path} names no backup")
        kept = data.get("kept") or []
        if not isinstance(kept, list) or not all(isinstance(name, str) for name in kept):
            raise ValueError(f"{self.path} holds no list of kept directories")
        return data

    def write(self, record):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": RECORD_VERSION, **record}, indent=2, sort_keys=True)
        atomic_write_bytes(str(self.path), (payload + "\n").encode("utf-8"))


class InfluxUpgradeService:
    """Plan, run and undo the upgrade of the bundled InfluxDB.

    Every collaborator is injectable so tests drive it without Docker; the
    caller (admin/server.py) runs one job at a time and holds the
    container-action and restore locks while it does.
    """

    def __init__(
        self,
        record_path,
        backups,
        context_provider=detect_install_context,
        docker=None,
        compose=None,
        overview_provider=run_maintenance_overview,
        target_image=default_target_image,
        clock=time.monotonic,
        sleep=time.sleep,
        now=lambda: datetime.now(timezone.utc),
        disk_free=lambda path: shutil.disk_usage(path).free,
        on_image_changed=lambda _image: None,
        meminfo=None,
        swaps=None,
    ):
        self.store = UpgradeRecordStore(record_path)
        self._backups = backups
        self._context_provider = context_provider
        self._docker = docker or DockerCli()
        self._compose = compose or DockerCompose()
        self._overview_provider = overview_provider
        self._target_image = target_image
        self._clock = clock
        self._sleep = sleep
        self._now = now
        self._disk_free = disk_free
        self._on_image_changed = on_image_changed
        self._meminfo = meminfo or read_meminfo
        self._swaps = swaps or read_swaps
        self.active = None
        self.live_key = None
        self._wait_lock = threading.Lock()
        self._waiting = False
        self._stoppable = False
        self.slow = False
        self._stop_waiting = threading.Event()

    # --- read-only plan --------------------------------------------------

    def plan(self):
        """What the Updates page shows and offers; reads, never changes anything."""

        facts = self._facts()
        view = {
            "ok": True,
            "state": facts["state"],
            "message": facts["message"],
            "current_image": facts.get("current"),
            "target_image": facts.get("target"),
            "container": facts.get("container"),
            "last": _public_record(facts.get("record")),
            "rollback": self._rollback_view(facts),
            "recheck": self._recheck_view(facts),
            "slow": bool(facts.get("slow")),
            "live": bool(facts.get("live")),
            "memory": facts.get("memory"),
        }
        for key in ("current", "target"):
            version = parse_image(facts.get(key))
            view[f"{key}_version"] = str(version) if version else None
        return view

    def _facts(self, own_job=False):
        facts = {"state": "unavailable", "message": None}
        try:
            facts["record"] = self.store.read()
        except ValueError as exc:
            facts["message"] = f"{exc}. Remove it to upgrade InfluxDB here again."
            return facts
        context = self._context_provider()
        facts["context"] = context
        if not (getattr(context, "config_exists", False) and getattr(context, "compose_exists", False)):
            return {**facts, "state": "not_installed", "message": "Nothing is installed here yet."}
        config = _read_config(context)
        if config is None:
            facts["message"] = "config/config.json cannot be read, so InfluxDB is left as it is."
            return facts
        influx = resolve_influxdb_config(config)
        facts["org"] = influx["org"]
        if not influx["enabled"]:
            return {**facts, "state": "disabled", "message": "Analytics is switched off."}
        if influx["mode"] != "bundled":
            return {**facts, "state": "external", "message": "Analytics uses an InfluxDB you manage yourself."}
        text = _read_compose(context)
        current = compose_image(text) if text is not None else None
        facts["current"] = current
        facts["target"] = self._target_image()
        if text is None:
            facts["message"] = "docker-compose.yml cannot be read."
            return facts
        if parse_image(current) is None:
            return {
                **facts,
                "state": "unmanaged",
                "message": "docker-compose.yml names no InfluxDB 2.x image the Admin can read; it is left to you.",
            }
        if parse_image(facts["target"]) is None:
            facts["message"] = "This Admin build names no InfluxDB version."
            return facts
        overview = self._overview_provider()
        if not (overview.get("docker") or {}).get("available"):
            facts["message"] = "Docker is not available."
            return facts
        container = (overview.get("containers") or {}).get(INFLUX_SERVICE) or {}
        facts["container"] = {
            "name": container.get("name"),
            "running": bool(container.get("running")),
            "found": bool(container.get("found", container.get("running"))),
            "image": container.get("image"),
        }
        record = facts["record"]
        if self.active is not None and not own_job:
            return {**facts, "state": "busy", "message": "An InfluxDB upgrade or its way back is running.",
                    "slow": self.slow,
                    "live": record is not None and self.live_key == _record_key(record)}
        if record is not None and record["state"] in BLOCKING_STATES:
            return {
                **facts,
                "state": "blocked",
                "message": (
                    f"The way back to {record['from_image']} did not complete. {_where(record)} "
                    + (
                        "If the Admin restarted during it, a data/influxdb-…-set-aside-… directory may hold "
                        "the data instead. " if record["state"] == ROLLING_BACK else ""
                    )
                    + f"The backup {record['backup']} is under Backups. Once InfluxDB holds what it should, "
                    f"remove {self.store.path.name} from the Admin data directory to upgrade here again."
                ),
            }
        # A stopped container counts too: the next start would put the compose
        # file's image on the data that container's image wrote.
        container_image = facts["container"]["image"]
        if facts["container"]["found"] and container_image != current:
            return {
                **facts,
                "state": "mismatch",
                "message": (
                    f"InfluxDB's container was made from {container_image or 'an image that cannot be read'}, "
                    f"but docker-compose.yml names {current}; no upgrade is offered until they agree."
                ),
            }
        if is_upgrade(current, facts["target"]):
            problem = _way_back_problem(context)
            if problem:
                return {
                    **facts,
                    "state": "no_way_back",
                    "message": f"The way back would not work here: {problem} Upgrade by hand, or put that right first.",
                }
            if not facts["container"]["running"]:
                return {
                    **facts,
                    "state": "not_running",
                    "message": "InfluxDB is not running; Restart EMS now on Services starts it, then it can be upgraded.",
                }
            memory = self._memory(facts["container"]["name"])
            facts["memory"] = memory
            if memory is not None and memory["available_mb"] < memory["needed_mb"]:
                return {
                    **facts,
                    "state": "low_memory",
                    "message": (
                        f"InfluxDB holds {memory['influx_mb']} MB; its upgrade needs about {memory['needed_mb']} MB "
                        f"free beside that, and {memory['available_mb']} MB of memory and swap are free. Switch swap "
                        "on, shorten the retention under Settings or move to a device with more memory first."
                    ),
                }
            return {**facts, "state": "available", "message": None}
        if is_upgrade(facts["target"], current):
            return {**facts, "state": "newer", "message": "InfluxDB runs a newer version than this Admin build ships."}
        return {**facts, "state": "current", "message": None}

    def _rollback_view(self, facts):
        record = facts.get("record")
        if record is None or record["state"] not in ROLLBACK_FROM_STATES:
            return None
        view = {
            "available": False,
            "to_image": record["from_image"],
            "backup": record["backup"],
            "since": record.get("started_at"),
            "reason": None,
        }
        if facts["state"] in ("unavailable", "not_installed", "disabled", "external", "busy"):
            view["reason"] = facts["message"]
        elif facts.get("current") != record["to_image"]:
            view["reason"] = f"docker-compose.yml no longer names {record['to_image']}."
        elif _way_back_problem(facts["context"]):
            view["reason"] = _way_back_problem(facts["context"])
        elif not self._backups.has_archive(record["backup"]):
            view["reason"] = f"The backup {record['backup']} is no longer in the backup directory."
        else:
            view["available"] = True
        return view

    # --- the upgrade -------------------------------------------------------

    def upgrade(self, target_image, progress=None):
        """Upgrade to ``target_image``, the image the operator confirmed."""

        run = _Run(UPGRADE_STEPS, progress)
        self.active = "upgrade"
        try:
            return self._upgrade(run, target_image)
        finally:
            self.active = None
            self.live_key = None

    def _upgrade(self, run, target_image):
        facts = self._facts(own_job=True)
        if facts["state"] != "available" or facts["target"] != target_image:
            return run.refused(facts["message"] or f"InfluxDB cannot be upgraded to {target_image} now.")
        context, current, target = facts["context"], facts["current"], facts["target"]
        container = facts["container"]["name"]
        run.done("preflight", f"{current} to {target}")
        try:
            listed = self._listing(container, facts["org"])
        except InfluxUpgradeError as exc:
            return run.failed("inventory", str(exc), changed=False)
        buckets, tasks = sorted(listed["bucket"]), sorted(listed["task"])
        ids = {"bucket_ids": listed["bucket"], "task_ids": listed["task"]}
        run.done("inventory", f"{len(buckets)} buckets, {len(tasks)} tasks")
        try:
            self._docker.pull(target)
        except DockerError as exc:
            return run.failed("pull", _docker_text(exc), changed=False)
        run.done("pull", target)
        try:
            result = self._backups.create_backup({"scope": "influxdb"})
        except Exception as exc:  # the backup's own message, never a traceback
            return run.failed("backup", str(exc) or "The backup failed.", changed=False)
        archives = result.get("archives") or []
        if not result.get("ok") or not archives or not archives[0].get("verified"):
            return run.failed("backup", result.get("message") or "The backup could not be verified.", changed=False)
        backup = archives[0]["name"]
        run.done("backup", backup)
        record = {
            "state": RUNNING,
            "from_image": current,
            "to_image": target,
            "backup": backup,
            "buckets": buckets,
            "tasks": tasks,
            **ids,
            "started_at": utc_now_iso(),
            "finished_at": None,
            "message": None,
            "set_aside": None,
            "kept": _kept_by(facts.get("record")),
        }
        try:
            self.store.write(record)
            self.live_key = _record_key(record)
            self._rewrite_compose(context, current, target)
        except (OSError, ValueError, InfluxUpgradeError) as exc:
            self._finish(record, FAILED, f"docker-compose.yml was not changed: {exc}")
            return run.failed("update_compose", str(exc), changed=False, backup=backup)
        run.done("update_compose", target)
        try:
            self._compose.up(
                context.install_root, profiles=(ANALYTICS_PROFILE,), services=(INFLUX_SERVICE,), force_recreate=True
            )
        except Exception as exc:  # recorded as the failure it is, never a traceback
            return self._upgrade_failed(run, record, "recreate", _docker_text(exc))
        run.done("recreate")
        try:
            problem = self._check(container, target, buckets, tasks, facts["org"], run.noting("verify"), ids=ids)
        except Exception as exc:  # recorded as the failure it is, never a traceback
            problem = _docker_text(exc) or "The check failed unexpectedly."
        if problem:
            return self._upgrade_failed(run, record, "verify", problem)
        self._finish(record, UPGRADED, None)
        run.done("verify", f"{target} answers with every bucket and task")
        return run.completed(
            f"InfluxDB runs {target}. The backup {backup} stays under Backups.", backup=backup
        )

    def _upgrade_failed(self, run, record, key, problem):
        message = (
            f"{problem} docker-compose.yml names {record['to_image']}; InfluxDB is left as it is. "
            f"Roll back restores {record['backup']} on {record['from_image']}."
        )
        self._finish(record, FAILED, problem)
        return run.failed(key, problem, changed=True, backup=record["backup"], message=message)

    def _recheck_view(self, facts):
        """Whether an unfinished upgrade can be checked again, and why not; None without one."""

        record = facts.get("record")
        if record is None or record["state"] not in (RUNNING, FAILED):
            return None
        if facts["state"] == "busy":
            return {"available": False, "reason": facts["message"]}
        if "container" not in facts:
            return {"available": False, "reason": facts["message"] or "InfluxDB cannot be read here."}
        if facts.get("current") != record["to_image"]:
            return {"available": False,
                    "reason": f"docker-compose.yml names {facts.get('current')}; the upgrade did not change it."}
        if facts["container"]["image"] != record["to_image"]:
            return {"available": False,
                    "reason": (f"InfluxDB's container runs {facts['container']['image'] or 'another image'}; "
                               "Restart EMS now on Services starts the version docker-compose.yml names, then check again.")}
        if not facts["container"]["running"]:
            return {"available": False,
                    "reason": "InfluxDB is not running; Restart EMS now on Services starts it, then check again."}
        return {"available": True, "reason": None}

    def recheck(self):
        """Check again an upgrade that did not finish: an InfluxDB that answers on the new
        version with every bucket and task it held is recorded as upgraded. Changes no container.
        """

        facts = self._facts(own_job=True)
        view = self._recheck_view(facts)
        if view is None:
            return {"ok": False, "status": "refused", "message": "There is no unfinished InfluxDB upgrade to check."}
        if not view["available"]:
            return {"ok": False, "status": "refused", "message": view["reason"]}
        record = facts["record"]
        container = facts["container"]["name"]
        if not self._answers(container):
            return {"ok": False, "status": "failed", "message": "InfluxDB does not answer yet; check again later."}
        ids = {key: record[key] for key in ("bucket_ids", "task_ids") if isinstance(record.get(key), dict)}
        problem = self._verify(container, record["to_image"], record["buckets"], record["tasks"], facts["org"], ids)
        if problem:
            return {"ok": False, "status": "failed", "message": problem}
        self._finish(record, UPGRADED, None)
        return {"ok": True, "status": "completed",
                "message": f"InfluxDB runs {record['to_image']} with every bucket and task."}

    # --- the way back --------------------------------------------------------

    def rollback(self, image, progress=None):
        """Restore the upgrade's backup on ``image``, the previous version the operator confirmed."""

        run = _Run(ROLLBACK_STEPS, progress)
        self.active = "rollback"
        try:
            return self._rollback(run, image)
        finally:
            self.active = None
            self.live_key = None

    def _rollback(self, run, image):
        facts = self._facts(own_job=True)
        view = self._rollback_view(facts)
        if view is None or not view["available"] or view["to_image"] != image:
            reason = (view or {}).get("reason") or "There is no InfluxDB upgrade to roll back."
            return run.refused(reason)
        record, context = facts["record"], facts["context"]
        container = facts["container"]["name"]
        problem = self._rollback_preflight(context, record)
        if problem:
            return run.refused(problem)
        run.done("preflight", f"{record['to_image']} back to {image} from {record['backup']}")
        try:
            if self._docker.inspect_image(image) is None:
                self._docker.pull(image)
        except DockerError as exc:
            return run.failed("pull", _docker_text(exc), changed=False)
        run.done("pull", image)
        before = dict(record)
        answered = self._answers(container)
        record = {**record, "state": ROLLING_BACK, "message": None, "set_aside": None, "attempt": None,
                  "rollback_started_at": utc_now_iso()}
        try:
            self.store.write(record)
        except OSError as exc:
            return run.failed("stop", f"The upgrade record cannot be written: {exc}", changed=False)
        self.live_key = _record_key(record)
        try:
            self._roll_back(run, record, context, container, image, facts["org"])
        except _StepFailed as failure:
            return self._put_back(run, record, before, failure, context, container, answered)
        except Exception as exc:  # recorded as the failure it is, never a traceback
            failure = _StepFailed(run.pending(), _docker_text(exc) or "The way back failed unexpectedly.")
            return self._put_back(run, record, before, failure, context, container, answered)
        record["kept"] = list(record.get("kept") or []) + [record["set_aside"]]
        self._finish(record, ROLLED_BACK, None)
        run.done("verify")
        return run.completed(
            f"InfluxDB runs {image} again with the data of {record['backup']}. "
            f"What it recorded after that backup is in data/{record['set_aside']}.",
            backup=record["backup"],
        )

    def _rollback_preflight(self, context, record):
        """Why the way back cannot run to its end here, checked before it changes anything."""

        problem = _way_back_problem(context)
        if problem:
            return problem
        root = Path(context.install_root)
        try:
            size = self._backups.check_influx_restore(record["backup"])
        except Exception as exc:  # the backup service's own reason, never a traceback
            return f"The backup {record['backup']} cannot be restored: {exc}"
        free = self._disk_free(root / "data")
        if free < RESTORE_SPACE_FACTOR * size:
            return (
                f"The way back needs about {RESTORE_SPACE_FACTOR * size // 2**20} MiB free beside "
                f"data/, and {free // 2**20} MiB are."
            )
        return None

    def _roll_back(self, run, record, context, container, image, org):
        """Every step that changes something on the way back; raises _StepFailed."""

        root = context.install_root
        # A name that did not exist before is what proves, after a failed
        # rename, that the directory there is the one this way back moved.
        name = self._fresh_dir(root, f"influxdb-{parse_image(record['to_image'])}-set-aside")
        try:
            self._compose.stop(root, services=(INFLUX_SERVICE,), profiles=(ANALYTICS_PROFILE,))
        except DockerError as exc:
            raise _StepFailed("stop", _docker_text(exc)) from exc
        run.done("stop")
        try:
            self._docker.set_aside_influx_data(root, image, name)
        except DockerError as exc:
            # The rename may have happened although the command failed after it.
            if (Path(root) / "data" / name).exists():
                record["set_aside"] = name
                self.store.write(record)
            elif not (Path(root) / "data" / "influxdb").is_dir():
                raise _Unproven("set_aside", _docker_text(exc), name) from exc
            raise _StepFailed("set_aside", _docker_text(exc)) from exc
        record["set_aside"] = name
        self.store.write(record)
        run.done("set_aside", f"data/{name}")
        try:
            self._rewrite_compose(context, record["to_image"], image)
        except (OSError, ValueError, InfluxUpgradeError) as exc:
            raise _StepFailed("update_compose", str(exc)) from exc
        run.done("update_compose", image)
        try:
            # The image keeps /etc/influxdb2 in an anonymous volume, which a
            # recreate carries over; an empty InfluxDB that finds the old CLI
            # configuration there refuses to set itself up.
            self._compose.up(
                root, profiles=(ANALYTICS_PROFILE,), services=(INFLUX_SERVICE,),
                force_recreate=True, renew_anon_volumes=True,
            )
        except DockerError as exc:
            raise _StepFailed("start", _docker_text(exc)) from exc
        problem = self._wait_until_answering(container, run.noting("start"))
        if problem:
            raise _StepFailed("start", problem)
        run.done("start", image)
        try:
            self._backups.restore_influx_archive(record["backup"])
        except Exception as exc:  # the restore's own message, never a traceback
            raise _StepFailed("restore", str(exc) or "The restore failed.") from exc
        run.done("restore", record["backup"])
        problem = self._check(container, image, record["buckets"], record["tasks"], org, run.noting("verify"))
        if problem:
            raise _StepFailed("verify", problem)

    def _put_back(self, run, record, before, failure, context, container, answered):
        """Return InfluxDB, and the record (``before``), to what they were before the way back.

        Nothing is deleted: what the attempt started is set aside beside the
        data it puts back. It counts as put back once the data, the compose
        file and the container are as before; InfluxDB has to answer again
        only if it answered before (``answered``). Only when that fails is the
        record left as a failed way back, which offers nothing more until the
        operator has looked.
        """

        run.note(failure.key, PUTTING_BACK)
        if isinstance(failure, _Unproven):
            problem = f"Neither data/influxdb nor data/{failure.name} is there to put back."
            return self._rollback_failed(run, record, failure, problem)
        root = context.install_root
        aside = record.get("set_aside")
        attempt = None
        try:
            if aside:
                attempt = self._fresh_dir(root, f"influxdb-{parse_image(record['from_image'])}-failed-way-back")
                self._compose.stop(root, services=(INFLUX_SERVICE,), profiles=(ANALYTICS_PROFILE,))
                try:
                    self._docker.put_back_influx_data(root, record["from_image"], aside, attempt)
                except DockerError:
                    # The moves may have happened although the command failed
                    # after them; the record says what the disk shows.
                    data = Path(root) / "data"
                    record.update({
                        "set_aside": aside if (data / aside).exists() else None,
                        "attempt": attempt if (data / attempt).exists() else None,
                    })
                    self.store.write(record)
                    raise
                # An attempt that never created data/influxdb left nothing to move.
                attempt = attempt if (Path(root) / "data" / attempt).exists() else None
                record.update({"set_aside": None, "attempt": attempt})
                self.store.write(record)
                if compose_image(_read_compose(context) or "") != record["to_image"]:
                    self._rewrite_compose(context, record["from_image"], record["to_image"])
                # Fresh anonymous volumes: those of the attempt hold the
                # previous image's set-up, not the upgraded one's.
                self._compose.up(
                    root, profiles=(ANALYTICS_PROFILE,), services=(INFLUX_SERVICE,),
                    force_recreate=True, renew_anon_volumes=True,
                )
            else:
                self._compose.up(root, profiles=(ANALYTICS_PROFILE,), services=(INFLUX_SERVICE,))
            problem = self._wait_until_answering(container, limit=SLOW_START_SECONDS) if answered else None
        except Exception as exc:  # the way back could not be undone; say where everything is
            problem = _docker_text(exc)
        if problem:
            return self._rollback_failed(run, record, failure, problem)
        left = f" What the attempt started is in data/{attempt}." if attempt else ""
        state = "answers" if answered else "does not answer, as it did not before"
        kept = list(before.get("kept") or []) + ([attempt] if attempt else [])
        try:
            self.store.write({**before, "kept": kept, "message": f"The way back failed at {failure.key}: {failure.problem}"})
        except OSError:
            pass
        message = (
            f"{failure.problem} InfluxDB is back on {record['to_image']} with its own data, as before the "
            f"way back, and {state}.{left} Roll back can be tried again."
        )
        return run.failed(failure.key, failure.problem, changed=bool(attempt), backup=record["backup"], message=message)

    def _rollback_failed(self, run, record, failure, undo_problem):
        message = (
            f"{failure.problem} Putting InfluxDB back failed too: {undo_problem} {_where(record)} "
            f"The backup {record['backup']} is under Backups. Nothing more is offered here until "
            "you have looked; docs/technical/influxdb.md says how to put InfluxDB back by hand."
        )
        self._finish(record, ROLLBACK_FAILED, f"{failure.problem} {undo_problem}")
        return run.failed(failure.key, failure.problem, changed=True, backup=record["backup"], message=message)

    # --- helpers ----------------------------------------------------------------

    def _finish(self, record, state, message):
        record.update({"state": state, "message": message, "finished_at": utc_now_iso()})
        try:
            self.store.write(record)
        except OSError:
            pass

    def _rewrite_compose(self, context, expected, image):
        """Name ``image`` instead of ``expected``; the file is replaced whole or not at all."""

        path = Path(context.compose_path)
        text = path.read_text(encoding="utf-8")
        if compose_image(text) != expected:
            raise InfluxUpgradeError(f"docker-compose.yml no longer names {expected}.")
        mode = path.stat().st_mode & 0o777
        atomic_write_bytes(str(path), replace_image(text, image).encode("utf-8"), mode=mode)
        try:
            self._on_image_changed(image)
        except Exception:  # the Setup marker is a projection; the compose file is the authority
            pass

    def _check(self, container, image, buckets, tasks, org, on_slow=None, ids=None):
        """Why InfluxDB in ``container`` is not ``image`` holding ``buckets`` and ``tasks``, or None."""

        problem = self._wait_until_answering(container, on_slow)
        return problem or self._verify(container, image, buckets, tasks, org, ids)

    def _verify(self, container, image, buckets, tasks, org, ids=None):
        """Why the answering InfluxDB in ``container`` is not ``image`` holding ``buckets`` and ``tasks``, or None.

        Each command has CHECK_TIMEOUT_SECONDS: right after a start with years
        of history, a board short of memory can take longer than Docker's
        usual half minute to run one, and that is a slow answer, not a failed
        upgrade.

        With ``ids`` (``bucket_ids``/``task_ids`` by name, as listed before the
        upgrade) a bucket or task made anew under an old name is not the one
        InfluxDB held, so it counts as missing.
        """

        began = self._started_at(container)
        try:
            state = self._docker.inspect_container(container) or {}
            code, out, _detail = self._docker.exec_capture(
                container, ("influxd", "version"), timeout=CHECK_TIMEOUT_SECONDS
            )
            listed = self._listing(container, org)
        except (DockerError, InfluxUpgradeError) as exc:
            if self._stopped_for_memory(container):
                return "InfluxDB was stopped for lack of memory while it was checked."
            now_began = self._started_at(container)
            if began and now_began and now_began != began:
                return "InfluxDB restarted while it was checked."
            return _docker_text(exc) if isinstance(exc, DockerError) else str(exc)
        if state.get("image") != image:
            return f"The InfluxDB container runs {state.get('image') or 'another image'}, not {image}."
        wanted = parse_image(image)
        found = _VERSION_RE.search(out) if code == 0 else None
        if found is None or not _same_release(found.group(1), wanted):
            return f"InfluxDB reports {found.group(1) if found else 'no version'}, not {wanted}."
        missing = sorted(set(buckets) - set(listed["bucket"])) + sorted(set(tasks) - set(listed["task"]))
        if missing:
            return f"InfluxDB no longer lists {', '.join(missing)}."
        for what, key in (("bucket", "bucket_ids"), ("task", "task_ids")):
            before = (ids or {}).get(key) or {}
            renewed = sorted(
                name for name, held in before.items()
                if isinstance(held, list) and set(held) - set(listed[what].get(name) or ())
            )
            if renewed:
                return f"InfluxDB lists {', '.join(renewed)} made anew, not the {what}s it held."
        return None

    def _memory(self, container):
        """What InfluxDB holds, what its upgrade needs free and what is free, in MB; None where unreadable.

        InfluxDB is the process ``influxd`` runs as, PID 1 of its container in
        every image this Admin manages; its anonymous memory and what of it
        sits in swap are what a new start has to hold again. The free memory
        is the host's: MemAvailable plus the free swap, where a zram device
        counts half -- the pages it takes are compressed into the very memory
        MemAvailable already counts. Without /proc/swaps all free swap counts
        half.
        """

        host = self._meminfo()
        if not host or "MemAvailable" not in host:
            return None
        try:
            code, out, _detail = self._docker.exec_capture(container, ("cat", "/proc/1/status"))
        except DockerError:
            return None
        held = _process_memory_mb(out) if code == 0 else None
        if held is None:
            return None
        swaps = self._swaps()
        if swaps is None:
            swap_free = host.get("SwapFree", 0) // 2
        else:
            swap_free = sum(
                (size - used) // 2 if name.startswith("/dev/zram") else size - used
                for name, size, used in swaps
            )
        return {
            "influx_mb": held,
            "needed_mb": math.ceil(held * MEMORY_GROWTH_SHARE) + MEMORY_RESERVE_MB,
            "available_mb": host["MemAvailable"] + max(swap_free, 0),
        }

    def _stopped_for_memory(self, container):
        """Whether Docker reports the container's last run as ended for lack of memory.

        Docker learns that from the memory cgroup, which Raspberry Pi kernels
        switch off; there the kernel's own OOM killer leaves exit code 137.
        """

        reader = getattr(self._docker, "inspect_container_oom_killed", None)
        if reader is None:
            return False
        try:
            return reader(container) is True
        except DockerError:
            return False

    def _fresh_dir(self, root, prefix):
        """A ``data/`` directory name that does not exist yet."""

        for _attempt in range(8):
            name = f"{prefix}-{_stamp(self._now()).lower()}-{secrets.token_hex(3)}"
            if not (Path(root) / "data" / name).exists():
                return name
        raise _StepFailed("set_aside", "No free name for a set-aside directory was found.")

    def _answers(self, container):
        """Whether InfluxDB in ``container`` answers now, asked once."""

        try:
            state = self._docker.inspect_container(container) or {}
            if state.get("status") != "running":
                return False
            return self._docker.exec_capture(container, _PING)[0] == 0
        except DockerError:
            return False

    def _wait_until_answering(self, container, on_slow=None, limit=None):
        """Why InfluxDB in ``container`` will not answer, or None once it does.

        Without ``limit``, time alone never ends the wait: ``on_slow`` is told
        once that the start takes longer than SLOW_START_SECONDS, and the wait
        goes on until InfluxDB answers, its container does not keep running or
        keeps restarting, or ``stop_waiting`` ends it. A wait with ``limit``,
        the one that puts InfluxDB back after a failed way back, cannot be
        stopped and gives up after ``limit`` seconds.

        InfluxDB is asked at its own address, never through the CLI
        configuration: an image setting itself up on empty data, as on the way
        back, first runs a temporary server on another port, writes that port
        into the configuration, stops it and only then starts the server that
        stays. Asked through the configuration, the temporary server answers
        for it, and a restore started on that answer finds nobody listening.
        """

        with self._wait_lock:
            self._stop_waiting.clear()
            self._waiting = True
            self._stoppable = limit is None
            self.slow = False
        try:
            return self._wait_for_answer(container, on_slow, limit)
        finally:
            with self._wait_lock:
                self._waiting = False
                self._stoppable = False
                self.slow = False
                self._stop_waiting.clear()

    def stop_waiting(self):
        """End a wait past SLOW_START_SECONDS that may be stopped; whether one was."""

        with self._wait_lock:
            if not (self._waiting and self._stoppable and self.slow):
                return False
            self._stop_waiting.set()
            return True

    def _started_at(self, container):
        reader = getattr(self._docker, "inspect_container_started_at", None)
        if reader is None:
            return None
        try:
            return reader(container)
        except DockerError:
            return None

    def _wait_for_answer(self, container, on_slow, limit):
        started = self._clock()
        not_running = 0
        restarts = 0
        began = self._started_at(container)
        while True:
            if self._stop_waiting.is_set():
                return "Waiting was stopped on request; InfluxDB may still be starting."
            try:
                state = self._docker.inspect_container(container) or {}
            except DockerError:
                state = {}
            if state.get("status") != "running":
                not_running += 1
                if not_running >= NOT_RUNNING_POLLS:
                    detail = state.get("status_detail") or state.get("status") or "gone"
                    if self._stopped_for_memory(container):
                        return f"InfluxDB was stopped for lack of memory ({detail})."
                    if "(137)" in detail:
                        return f"InfluxDB was killed ({detail}); on a device short of memory that is the kernel making room."
                    return f"InfluxDB does not stay running ({detail})."
            else:
                not_running = 0
                now_began = self._started_at(container)
                if began and now_began and now_began != began:
                    restarts += 1
                    if restarts > RESTARTS_ALLOWED:
                        return f"InfluxDB keeps restarting ({restarts} times while it was waited for)."
                began = now_began or began
                try:
                    code, _out, _detail = self._docker.exec_capture(container, _PING)
                except DockerError:
                    code = None
                if code == 0:
                    return None
            elapsed = self._clock() - started
            if limit is not None and elapsed >= limit:
                return f"InfluxDB has not answered within {limit // 60} minutes; it may still be converting its data."
            if limit is None and not self.slow and elapsed >= SLOW_START_SECONDS:
                with self._wait_lock:
                    self.slow = True
                if on_slow is not None:
                    on_slow()
            self._sleep(READY_POLL_SECONDS)

    def _listing(self, container, org):
        """Bucket and task IDs by name, as InfluxDB lists them with the EMS token; a task name may repeat."""

        names = {}
        for what in ("bucket", "task"):
            try:
                code, out, detail = self._docker.exec_capture(
                    container, ("sh", "-c", _LIST_SCRIPT.format(what=what), org), timeout=CHECK_TIMEOUT_SECONDS
                )
            except DockerError as exc:
                raise InfluxUpgradeError(_docker_text(exc)) from exc
            if code != 0:
                raise InfluxUpgradeError(
                    f"InfluxDB does not list its {what}s with the EMS token" + (f": {detail}" if detail else ".")
                )
            try:
                listed = json.loads(out)
            except ValueError as exc:
                raise InfluxUpgradeError(f"InfluxDB's {what} list cannot be read.") from exc
            if not isinstance(listed, list):
                raise InfluxUpgradeError(f"InfluxDB's {what} list cannot be read.")
            ids = {}
            for entry in listed:
                if isinstance(entry, dict) and entry.get("name"):
                    ids.setdefault(str(entry["name"]), set()).add(str(entry.get("id") or ""))
            names[what] = {name: sorted(found - {""}) for name, found in ids.items()}
        return names


class _StepFailed(Exception):
    def __init__(self, key, problem):
        super().__init__(problem)
        self.key = key
        self.problem = problem


class _Unproven(_StepFailed):
    """A step failed and left the data where no check can prove it is."""

    def __init__(self, key, problem, name):
        super().__init__(key, problem)
        self.name = name


class _Run:
    """The steps of one run, handed to the job as each one ends."""

    def __init__(self, steps, progress):
        self.labels = dict(steps)
        self.progress = progress
        self.steps = []

    def _add(self, key, status, detail=None):
        step = {"key": key, "status": status, "label": self.labels[key], "detail": detail}
        self.steps.append(step)
        if self.progress is not None:
            self.progress.record_step(step)

    def done(self, key, detail=None):
        self._add(key, "ok", detail)

    def note(self, key, note):
        """Mark the running step ``key`` with ``note`` while it goes on."""

        noted = getattr(self.progress, "note_step", None)
        if noted is not None:
            noted(key, note)

    def noting(self, key):
        """A callback that marks ``key`` as still starting."""

        return lambda: self.note(key, STILL_STARTING)

    def pending(self):
        finished = {step["key"] for step in self.steps}
        return next((key for key in self.labels if key not in finished), "verify")

    def refused(self, message):
        self._add("preflight", "error", message)
        return {"ok": False, "status": "refused", "changed": False, "message": message, "steps": list(self.steps)}

    def failed(self, key, problem, changed, backup=None, message=None):
        self._add(key, "error", problem)
        if message is None:
            message = f"{problem} Nothing was changed." if not changed else problem
        result = {"ok": False, "status": "failed", "changed": changed, "message": message, "steps": list(self.steps)}
        if backup:
            result["backup"] = backup
        return result

    def completed(self, message, backup=None):
        return {"ok": True, "status": "completed", "changed": True, "message": message,
                "backup": backup, "steps": list(self.steps)}


def _same_release(found, wanted):
    parts = [int(part) for part in found.split(".")]
    if wanted.patch is None:
        return parts[:2] == [wanted.major, wanted.minor]
    return parts == [wanted.major, wanted.minor, wanted.patch]


def read_meminfo(path="/proc/meminfo"):
    """The host's memory figures in MB by /proc/meminfo name, or None where the file cannot be read.

    Inside a container /proc/meminfo is the host's, so the Admin reads the
    memory the InfluxDB beside it competes for.
    """

    try:
        text = Path(path).read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    values = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            values[key.strip()] = int(fields[0]) // 1024
    return values or None


def read_swaps(path="/proc/swaps"):
    """The host's swap devices as (name, size MB, used MB), or None where /proc/swaps cannot be read."""

    try:
        lines = Path(path).read_text(encoding="ascii", errors="replace").splitlines()[1:]
    except OSError:
        return None
    swaps = []
    for line in lines:
        fields = line.split()
        if len(fields) >= 4 and fields[2].isdigit() and fields[3].isdigit():
            swaps.append((fields[0], int(fields[2]) // 1024, int(fields[3]) // 1024))
    return swaps


def _process_memory_mb(status):
    """Anonymous memory plus swap of influxd, in MB, from its /proc/<pid>/status text; None otherwise."""

    found = {}
    for line in str(status or "").splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if key == "Name" and fields and fields[0] != "influxd":
            return None
        if key in ("RssAnon", "VmRSS", "VmSwap") and fields and fields[0].isdigit():
            found[key] = int(fields[0])
    held = found.get("RssAnon", found.get("VmRSS"))
    if held is None:
        return None
    return (held + found.get("VmSwap", 0)) // 1024


def _docker_text(exc):
    detail = getattr(exc, "detail", None)
    message = getattr(exc, "message", None) or str(exc)
    return f"{message} {detail}" if detail else message


def _read_config(context):
    if not getattr(context, "config_exists", False):
        return None
    try:
        parsed = json.loads(Path(context.config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _read_compose(context):
    if not getattr(context, "compose_exists", False):
        return None
    try:
        return Path(context.compose_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _way_back_problem(context):
    """What would stop the way back on this installation, as far as it can be told without a backup."""

    problem = _init_secrets_problem(context)
    if problem:
        return problem
    root = Path(context.install_root)
    for name in ("config", "data", "data/influxdb"):
        path = root / name
        if path.is_symlink() or not path.is_dir():
            return f"{name}/ is not a plain directory, so InfluxDB's data cannot be set aside here."
    if not _MOUNT_RE.search(_read_compose(context) or ""):
        return "docker-compose.yml does not mount ./data/influxdb as InfluxDB's data, so it is left to you."
    return None


def _init_secrets_problem(context):
    """Why an empty InfluxDB would not set itself up with the EMS token, or None."""

    path = Path(context.install_root) / DOCKER_FIRST_SECRET_FILE
    try:
        values = parse_env_file(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return f"{DOCKER_FIRST_SECRET_FILE} cannot be read, so an empty InfluxDB could not set itself up."
    missing = [key for key in _INIT_KEYS if not values.get(key)]
    if values.get("DOCKER_INFLUXDB_INIT_MODE") != "setup" or missing:
        return f"{DOCKER_FIRST_SECRET_FILE} lacks the setup values an empty InfluxDB needs ({', '.join(missing) or 'mode'})."
    if values.get("DOCKER_INFLUXDB_INIT_ADMIN_TOKEN") != values.get("INFLUXDB_TOKEN"):
        return f"The EMS token in {DOCKER_FIRST_SECRET_FILE} is not the one an empty InfluxDB would be set up with."
    return None


def _kept_by(record):
    """Every directory an earlier run set aside, as a record names them, each once."""

    if not record:
        return []
    names = list(record.get("kept") or []) + [record.get("set_aside"), record.get("attempt")]
    return list(dict.fromkeys(name for name in names if name))


def _where(record):
    """Where InfluxDB's data from before the way back is, as the record knows it."""

    if record.get("set_aside") and record.get("attempt"):
        return (
            f"InfluxDB's data from before the way back is whole in data/{record['set_aside']}, and what "
            f"the way back started is in data/{record['attempt']}; data/influxdb may be missing."
        )
    if record.get("set_aside"):
        return (
            f"InfluxDB's data from before the way back is whole in data/{record['set_aside']}; "
            "data/influxdb holds what the way back started."
        )
    if record.get("attempt"):
        return (
            "InfluxDB's data from before the way back is back in data/influxdb; what the way back "
            f"started is in data/{record['attempt']}."
        )
    return "InfluxDB's data is where it was, in data/influxdb."


def _record_key(record):
    return (record.get("state"), record.get("started_at"), record.get("rollback_started_at"))


def _public_record(record):
    if record is None:
        return None
    keys = ("state", "from_image", "to_image", "backup", "started_at", "finished_at", "rollback_started_at",
            "message", "set_aside", "kept")
    return {key: record.get(key) for key in keys}
