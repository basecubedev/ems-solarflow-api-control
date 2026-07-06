# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin backup / restore orchestration.

Admin is UI and orchestration only; the EMS core (:mod:`ems.backup`) stays the
source of truth for the archive format, checksums, manifests and restore logic.
This module never invents a new archive format. It resolves the real EMS install
layout, lists/inspects/verifies the normal EMS backup archives, previews restores
(dry-run first) and applies them behind a rollback backup, and stores small Admin
grouping metadata for "backup sets" that only reference EMS-owned archives.

The module is import-side-effect-free and must not import ``emsctl``.
"""

import copy
import fnmatch
import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ems import backup as backup_mod

from admin.install_context import detect_install_context
from admin.models import utc_now_iso

# Only these archive names are ever offered; everything else in the directory is
# ignored so a stray file can never be listed, inspected, restored or deleted.
ALLOWED_ARCHIVE_PATTERNS = ("ems-*.tar.gz", "ems-*.tar.gz.enc")

# A restore must be applied from a preview generated shortly before; expired
# plans are rejected so a stale plan cannot be replayed against a changed archive.
PLAN_TTL_SECONDS = 600

# Diff output is response-limited so a large text file cannot flood the UI.
DIFF_MAX_BYTES = 200_000

CREATE_SCOPES = ("config", "databases", "influxdb", "system")
RESTORE_SCOPES = ("config", "databases", "influxdb", "system")
CONFLICT_POLICIES = ("abort", "keep", "replace")

# InfluxDB has a dedicated EMS/CLI restore flow; its archives must never go
# through the generic EMS file restore path (backup_mod.restore_backup). Admin
# orchestrates the EMS CLI restore instead (see docs/user/admin-backup-restore.md).
INFLUXDB_BACKUP_TYPE = "influxdb"

# Executor step status -> live job step state (mirrors admin.guided_upgrade).
_STEP_STATE = {"ok": "done", "skipped": "skipped", "warning": "done", "error": "failed"}


class BackupRestoreError(Exception):
    """Raised for expected backup/restore failures that are safe to show a user."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _safe_id(name):
    """Stable opaque ID for an archive basename.

    A sha256 of the basename is never turned back into a path: IDs are only ever
    matched against basenames discovered by listing the backup directory, so a
    crafted ID can never escape it.
    """

    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def _iso_from_epoch(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_config(path):
    try:
        with open(path, encoding="utf-8") as handle:
            parsed = json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_archive_name(name):
    """Best-effort ``(backup_type, backup_purpose)`` from an EMS archive name."""

    stem = name
    for suffix in (".tar.gz.enc", ".tar.gz"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    parts = stem.split("-")
    backup_type = parts[1] if len(parts) > 1 else None
    backup_purpose = parts[2] if len(parts) > 2 else None
    return backup_type, backup_purpose


# Placeholders older/foreign manifests used for "no value".
_ABSENT_BUILD_META = {"", "unknown", "none", "null", "-"}


def _clean_build_meta(value):
    """Treat empty and placeholder build fields (``unknown``/``none``/...) as absent."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if text and text.lower() not in _ABSENT_BUILD_META else None


def _source_build(source):
    """Compact build label: prefer the honest build label, then describe/commit."""

    return (_clean_build_meta(source.get("build_label"))
            or _clean_build_meta(source.get("git_describe"))
            or _clean_build_meta(source.get("git_commit_short")))


def _step(key, status, label, detail=None):
    return {"key": key, "status": status, "label": label, "detail": detail}


def resolve_ems_cli_backup_path(env, archive_path, mode):
    """Container-visible path for an Admin-resolved backup archive.

    Only archives inside Admin's backup directory are mapped and only their
    basename is reused, so no caller-supplied path can reach the EMS CLI. Both
    EMS contexts (``container`` exec, ``compose`` one-off) run the published EMS
    image with host ``data/`` bind-mounted at ``/app/data``.
    """

    from admin.ems_tool import CONTAINER_DATA_DIR

    backup_dir = os.path.realpath(env.backup_dir)
    resolved = os.path.realpath(archive_path)
    if os.path.dirname(resolved) != backup_dir:
        raise BackupRestoreError(
            "the backup archive is outside the Admin backup directory"
        )
    if mode not in ("container", "compose"):
        raise BackupRestoreError(
            "no EMS context is available to run the InfluxDB restore"
        )
    return f"{CONTAINER_DATA_DIR}/backups/{os.path.basename(resolved)}"


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BackupEnv:
    """Resolved paths + config for one backup/restore operation.

    ``base_dir`` is the EMS install root so archived paths (``config/config.json``,
    ``data/runtime-state.json``) map back to their real locations on restore.
    """

    base_dir: str
    config_path: str
    backup_dir: str
    sets_dir: str
    config: dict
    influx: dict

    @property
    def safe_location(self):
        # True only means the backup directory is inside the EMS install root, so
        # archived paths stay resolvable. It does NOT mean the backups survive a
        # manual reset: deleting data/ deletes local backups too (export first).
        base = os.path.abspath(self.base_dir)
        backup = os.path.abspath(self.backup_dir)
        return backup == base or backup.startswith(base + os.sep)

    def display_backup_dir(self):
        base = os.path.abspath(self.base_dir)
        backup = os.path.abspath(self.backup_dir)
        if backup == base or backup.startswith(base + os.sep):
            return os.path.relpath(backup, base).replace(os.sep, "/")
        return backup


def resolve_env(context):
    """Build a :class:`BackupEnv` from an Admin install context."""

    base_dir = str(context.install_root)
    config_path = str(context.config_path)
    data_dir = str(context.data_dir)
    config = _load_config(config_path)
    return BackupEnv(
        base_dir=base_dir,
        config_path=config_path,
        backup_dir=os.path.join(data_dir, "backups"),
        sets_dir=os.path.join(data_dir, "admin", "backups", "sets"),
        config=config,
        influx=backup_mod.evaluate_influxdb_backup(config),
    )


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class BackupRecord:
    id: str
    name: str
    path: str
    size_bytes: int = 0
    mtime: str = ""
    created_at: str = ""
    backup_type: str = None
    backup_purpose: str = None
    encrypted: bool = False
    locked: bool = False
    valid: bool = True
    manifest_available: bool = False
    source_version: str = None
    source_commit: str = None
    source_build: str = None
    files_count: int = 0
    sensitive_count: int = 0
    privacy_count: int = 0
    warnings: list = field(default_factory=list)
    error: str = None

    def to_dict(self):
        # ``path`` is intentionally omitted: the frontend only ever uses ``id``.
        return {
            "id": self.id,
            "name": self.name,
            "size_bytes": self.size_bytes,
            "mtime": self.mtime,
            "created_at": self.created_at,
            "backup_type": self.backup_type,
            "backup_purpose": self.backup_purpose,
            "encrypted": self.encrypted,
            "locked": self.locked,
            "valid": self.valid,
            "manifest_available": self.manifest_available,
            "source_version": self.source_version,
            "source_commit": self.source_commit,
            "source_build": self.source_build,
            "files_count": self.files_count,
            "sensitive_count": self.sensitive_count,
            "privacy_count": self.privacy_count,
            "warnings": list(self.warnings),
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class BackupStore:
    """List, resolve and delete EMS backup archives + Admin backup-set metadata.

    All resolution goes through a server-side listing of the backup directory, so
    frontend IDs never become paths: traversal, absolute paths, symlinks and
    unknown files are rejected because they never appear in the listing.
    """

    def __init__(self, env):
        self.env = env

    # --- archives --------------------------------------------------------

    def _iter_names(self):
        try:
            entries = os.listdir(self.env.backup_dir)
        except OSError:
            return []
        names = []
        for name in entries:
            if os.sep in name or (os.altsep and os.altsep in name):
                continue
            if not any(fnmatch.fnmatch(name, pat) for pat in ALLOWED_ARCHIVE_PATTERNS):
                continue
            full = os.path.join(self.env.backup_dir, name)
            # A symlink is never treated as a backup archive.
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            names.append(name)
        return sorted(names)

    def resolve(self, backup_id):
        """Return the absolute path for a safe archive ID, else raise."""

        if not isinstance(backup_id, str) or not backup_id:
            raise BackupRestoreError("a backup id is required")
        for name in self._iter_names():
            if _safe_id(name) == backup_id:
                return os.path.join(self.env.backup_dir, name)
        raise BackupRestoreError("unknown backup id")

    def record_for(self, path):
        name = os.path.basename(path)
        stat = os.stat(path)
        parsed_type, parsed_purpose = _parse_archive_name(name)
        record = BackupRecord(
            id=_safe_id(name),
            name=name,
            path=path,
            size_bytes=stat.st_size,
            mtime=_iso_from_epoch(stat.st_mtime),
            created_at=_iso_from_epoch(stat.st_mtime),
            backup_type=parsed_type,
            backup_purpose=parsed_purpose,
        )
        if stat.st_size == 0:
            record.valid = False
            record.error = "archive is empty"
            record.warnings.append("archive is empty")
            return record

        if backup_mod.is_encrypted(path):
            record.encrypted = True
            record.locked = True
            return record

        try:
            info = backup_mod.inspect_backup(path)
            manifest = info.get("manifest") or {}
        except backup_mod.BackupError as exc:
            record.valid = False
            record.error = str(exc)
            record.warnings.append("manifest could not be read")
            return record

        self._apply_manifest(record, manifest)
        return record

    @staticmethod
    def _apply_manifest(record, manifest):
        record.manifest_available = True
        record.backup_type = manifest.get("backup_type") or record.backup_type
        record.backup_purpose = manifest.get("backup_purpose") or record.backup_purpose
        record.created_at = manifest.get("created_at") or record.created_at
        source = manifest.get("source") or {}
        record.source_version = source.get("ems_version")
        record.source_commit = _clean_build_meta(source.get("git_commit_short"))
        record.source_build = _source_build(source)
        files = manifest.get("files") or []
        record.files_count = len(files)
        record.sensitive_count = sum(1 for f in files if f.get("sensitive"))
        record.privacy_count = sum(1 for f in files if f.get("privacy_relevant"))

    def list_records(self):
        return [self.record_for(os.path.join(self.env.backup_dir, name))
                for name in self._iter_names()]

    def delete_archive(self, backup_id):
        """Delete one archive resolved through the safe listing."""

        path = self.resolve(backup_id)
        os.remove(path)
        return os.path.basename(path)

    # --- backup sets -----------------------------------------------------

    def _set_path(self, set_id):
        # Set IDs are Admin-generated and must resolve to a plain file in sets_dir.
        if not isinstance(set_id, str) or not set_id:
            raise BackupRestoreError("a backup set id is required")
        if set_id != os.path.basename(set_id) or set_id in (".", ".."):
            raise BackupRestoreError("unsafe backup set id")
        return os.path.join(self.env.sets_dir, set_id + ".json")

    def read_set(self, set_id):
        path = self._set_path(set_id)
        if os.path.islink(path) or not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                parsed = json.load(handle)
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def write_set(self, record):
        os.makedirs(self.env.sets_dir, exist_ok=True)
        path = self._set_path(record["id"])
        tmp = path + ".part"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return path

    def list_sets(self):
        try:
            entries = os.listdir(self.env.sets_dir)
        except OSError:
            return []
        sets = []
        for name in sorted(entries):
            if not name.endswith(".json") or os.sep in name:
                continue
            record = self.read_set(name[: -len(".json")])
            if record is not None:
                sets.append(self._decorate_set(record))
        return sets

    def _decorate_set(self, record):
        names = set(self._iter_names())
        archives = []
        for entry in record.get("archives", []):
            name = entry.get("name")
            present = bool(name) and name in names
            archives.append({
                "type": entry.get("type"),
                "name": name,
                "optional": bool(entry.get("optional")),
                "present": present,
                "id": _safe_id(name) if present else None,
            })
        return {
            "id": record.get("id"),
            "created_at": record.get("created_at"),
            "purpose": record.get("purpose"),
            "label": record.get("label"),
            "status": record.get("status"),
            "warnings": list(record.get("warnings", [])),
            "archives": archives,
        }

    def delete_set(self, set_id, mode):
        record = self.read_set(set_id)
        if record is None:
            raise BackupRestoreError("unknown backup set id")
        removed = []
        if mode == "metadata_and_archives":
            names = set(self._iter_names())
            for entry in record.get("archives", []):
                name = entry.get("name")
                if name and name in names:
                    os.remove(os.path.join(self.env.backup_dir, name))
                    removed.append(name)
        os.remove(self._set_path(set_id))
        return {"set_id": set_id, "mode": mode, "removed_archives": removed}


# ---------------------------------------------------------------------------
# Inspector
# ---------------------------------------------------------------------------

class BackupInspector:
    """Read-only manifest / file-list / diff access on top of EMS core helpers."""

    def __init__(self, env):
        self.env = env

    def inspect(self, path, password=None):
        name = os.path.basename(path)
        encrypted = backup_mod.is_encrypted(path)
        if encrypted and not password:
            return {
                "ok": True,
                "id": _safe_id(name),
                "name": name,
                "encrypted": True,
                "locked": True,
                "manifest": None,
            }
        try:
            info = backup_mod.inspect_backup(path, password=password)
        except backup_mod.BackupPasswordError:
            raise BackupRestoreError("the backup password is incorrect")
        except backup_mod.BackupError as exc:
            raise BackupRestoreError(str(exc))
        manifest = info.get("manifest") or {}
        return {
            "ok": True,
            "id": _safe_id(name),
            "name": name,
            "encrypted": bool(encrypted),
            "locked": False,
            "manifest": self._summarize(manifest),
        }

    @staticmethod
    def _summarize(manifest):
        source = manifest.get("source") or {}
        files = [
            {
                # Content is never returned — only metadata about each file.
                "path": entry.get("path"),
                "kind": entry.get("kind"),
                "sensitive": bool(entry.get("sensitive")),
                "privacy_relevant": bool(entry.get("privacy_relevant")),
                "size_bytes": entry.get("size_bytes"),
                "has_checksum": bool(entry.get("sha256")),
            }
            for entry in manifest.get("files") or []
        ]
        return {
            "backup_type": manifest.get("backup_type"),
            "backup_purpose": manifest.get("backup_purpose"),
            "created_at": manifest.get("created_at"),
            "encryption": manifest.get("encryption") or {"enabled": False},
            "source": {
                "ems_version": source.get("ems_version"),
                "build_label": source.get("build_label"),
                "git_commit_short": source.get("git_commit_short"),
                "git_branch": source.get("git_branch"),
                "git_describe": source.get("git_describe"),
                "git_dirty": source.get("git_dirty"),
            },
            "files": files,
            "files_count": len(files),
            "sensitive_count": sum(1 for f in files if f["sensitive"]),
            "privacy_count": sum(1 for f in files if f["privacy_relevant"]),
            "skipped": list(manifest.get("skipped") or []),
            "databases": list(manifest.get("databases") or []),
            "influxdb": manifest.get("influxdb"),
        }

    def diff(self, path, file_name, password=None):
        try:
            result = backup_mod.diff_backup_file(
                path, file_name, base_dir=self.env.base_dir, password=password
            )
        except backup_mod.BackupPasswordError:
            raise BackupRestoreError("the backup password is incorrect")
        except backup_mod.BackupError as exc:
            raise BackupRestoreError(str(exc))
        if result.get("binary"):
            return {"ok": True, "binary": True, "text": result.get("text"),
                    "path": result.get("path")}
        text = result.get("text") or ""
        truncated = False
        if len(text) > DIFF_MAX_BYTES:
            text = text[:DIFF_MAX_BYTES]
            truncated = True
        return {"ok": True, "binary": False, "text": text,
                "path": result.get("path"), "truncated": truncated}


# ---------------------------------------------------------------------------
# Restore plans
# ---------------------------------------------------------------------------

@dataclass
class RestorePlanTarget:
    archive_id: str
    name: str
    backup_type: str
    archive_sha256: str


@dataclass
class RestorePlan:
    plan_id: str
    kind: str
    backup_id: str
    scope: str
    conflict_policy: str
    rollback_enabled: bool
    auto_rollback_enabled: bool
    password: str
    created_at: str
    expires_at: str
    targets: list
    manifest_summary: dict
    files: list
    summary: dict
    warnings: list
    blocked: bool
    block_reason: str

    def public(self):
        # The password is never serialized back to the client.
        return {
            "plan_id": self.plan_id,
            "kind": self.kind,
            "backup_id": self.backup_id,
            "scope": self.scope,
            "conflict_policy": self.conflict_policy,
            "rollback_enabled": self.rollback_enabled,
            "auto_rollback_enabled": self.auto_rollback_enabled,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "archive_sha256": self.targets[0].archive_sha256 if self.targets else None,
            "targets": [
                {"name": t.name, "backup_type": t.backup_type,
                 "archive_sha256": t.archive_sha256}
                for t in self.targets
            ],
            "manifest_summary": self.manifest_summary,
            "files": self.files,
            "summary": self.summary,
            "warnings": list(self.warnings),
            "blocked": self.blocked,
            "block_reason": self.block_reason,
        }


class RestorePlanRegistry:
    """Bounded in-memory store of short-lived restore plans."""

    def __init__(self, max_plans=32, ttl_seconds=PLAN_TTL_SECONDS):
        self._lock = threading.Lock()
        self._plans = {}
        self._order = []
        self._max = max_plans
        self._ttl = ttl_seconds

    def put(self, plan):
        with self._lock:
            self._plans[plan.plan_id] = plan
            self._order.append(plan.plan_id)
            while len(self._order) > self._max:
                self._plans.pop(self._order.pop(0), None)

    def get(self, plan_id):
        with self._lock:
            plan = self._plans.get(plan_id)
        if plan is None:
            return None
        if datetime.now(timezone.utc) > datetime.strptime(
            plan.expires_at, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc):
            return None
        return plan


# ---------------------------------------------------------------------------
# Jobs (mirror admin.guided_upgrade job machinery)
# ---------------------------------------------------------------------------

class BackupJob:
    """Thread-safe progress record for one backup or restore run."""

    def __init__(self, job_id, planned_steps):
        self._lock = threading.Lock()
        steps = [
            {"key": s["key"], "label": s["label"], "state": "pending", "message": None}
            for s in planned_steps
        ]
        if steps:
            steps[0]["state"] = "running"
        self._state = {
            "job_id": job_id,
            "status": "running",
            "steps": steps,
            "result": None,
            "error": None,
            "started_at": utc_now_iso(),
            "finished_at": None,
        }

    @property
    def job_id(self):
        return self._state["job_id"]

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def record_step(self, step):
        with self._lock:
            steps = self._state["steps"]
            idx = next(
                (i for i, s in enumerate(steps) if s["key"] == step.get("key")), None
            )
            if idx is None:
                return
            state = _STEP_STATE.get(step.get("status"), "done")
            steps[idx]["state"] = state
            steps[idx]["message"] = step.get("detail")
            if state != "failed":
                for nxt in steps[idx + 1:]:
                    if nxt["state"] == "pending":
                        nxt["state"] = "running"
                        break

    def finish(self, result):
        with self._lock:
            self._state["result"] = result
            self._state["finished_at"] = utc_now_iso()
            self._state["status"] = result.get("status", "failed") if not result.get(
                "ok"
            ) else "succeeded"
            if not result.get("ok"):
                self._state["error"] = {"message": result.get("message")}
                for step in self._state["steps"]:
                    if step["state"] == "running":
                        step["state"] = "failed"
        return result


class BackupJobRegistry:
    """In-memory registry of backup/restore jobs on bounded daemon threads."""

    def __init__(self, max_jobs=8):
        self._lock = threading.Lock()
        self._jobs = {}
        self._order = []
        self._max_jobs = max_jobs

    def submit(self, job, runner):
        with self._lock:
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            while len(self._order) > self._max_jobs:
                self._jobs.pop(self._order.pop(0), None)
        thread = threading.Thread(target=self._run, args=(job, runner), daemon=True)
        thread.start()
        return job

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
        return job.snapshot() if job is not None else None

    @staticmethod
    def _run(job, runner):
        try:
            runner(job)
        except Exception:  # never leak a traceback; surface a failed job
            job.finish({
                "ok": False,
                "status": "failed",
                "message": "The backup job failed unexpectedly.",
            })


class _ProgressSteps(list):
    """Step list that notifies a live job as each step is appended."""

    def __init__(self, progress=None):
        super().__init__()
        self._progress = progress

    def append(self, step):
        super().append(step)
        if self._progress is not None:
            self._progress.record_step(step)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class BackupRestoreService:
    """Facade used by ``admin/server.py`` for all backup/restore actions."""

    def __init__(self, context_provider=detect_install_context, plans=None,
                 ems_tool=None):
        self._context_provider = context_provider
        self.plans = plans or RestorePlanRegistry()
        self._ems_tool = ems_tool

    # --- environment -----------------------------------------------------

    def _env(self):
        return resolve_env(self._context_provider())

    def _store(self, env=None):
        return BackupStore(env or self._env())

    # --- list / inspect / diff ------------------------------------------

    def list_backups(self):
        env = self._env()
        store = BackupStore(env)
        records = store.list_records()
        sets = store.list_sets()
        encrypted = sum(1 for r in records if r.encrypted)
        invalid = sum(1 for r in records if not r.valid)
        created = [r.created_at for r in records if r.created_at]
        warnings = []
        if not env.safe_location:
            warnings.append("The backup directory is outside the EMS install root.")
        if any(r.locked for r in records):
            warnings.append("Some backups are encrypted and locked until unlocked.")
        if invalid:
            warnings.append("Some archives could not be read and are marked invalid.")
        return {
            "ok": True,
            "backup_dir": env.display_backup_dir(),
            "safe_location": env.safe_location,
            "influxdb": {
                "supported": bool(env.influx.get("supported")),
                "reason": env.influx.get("reason"),
                "message": env.influx.get("message"),
            },
            "summary": {
                "total": len(records),
                "encrypted": encrypted,
                "invalid": invalid,
                "latest_created_at": max(created) if created else None,
            },
            "sets": sets,
            "backups": [r.to_dict() for r in records],
            "warnings": warnings,
        }

    def inspect_backup(self, backup_id, password=None):
        store = self._store()
        path = store.resolve(backup_id)
        return BackupInspector(store.env).inspect(path, password=password)

    def diff_backup_file(self, backup_id, file_name, password=None):
        if not isinstance(file_name, str) or not file_name:
            raise BackupRestoreError("a file name is required")
        store = self._store()
        path = store.resolve(backup_id)
        return BackupInspector(store.env).diff(path, file_name, password=password)

    # --- delete ----------------------------------------------------------

    def delete_backup(self, backup_id, confirm, mode="archive"):
        if confirm is not True:
            raise BackupRestoreError("delete requires confirm=true")
        env = self._env()
        store = BackupStore(env)
        # A set ID resolves to Admin metadata; anything else is an archive ID.
        if store.read_set(backup_id) is not None:
            if mode not in ("metadata_only", "metadata_and_archives"):
                raise BackupRestoreError(
                    "backup set deletion requires mode "
                    "'metadata_only' or 'metadata_and_archives'"
                )
            result = store.delete_set(backup_id, mode)
            result["ok"] = True
            return result
        name = store.delete_archive(backup_id)
        return {"ok": True, "deleted": name, "mode": "archive"}

    # --- create ----------------------------------------------------------

    def plan_create_steps(self, scope):
        if scope == "system":
            keys = [
                ("preflight", "Preflight"),
                ("create_config", "Create config backup"),
                ("verify_config", "Verify config backup"),
                ("create_databases", "Create database backup"),
                ("verify_databases", "Verify database backup"),
                ("create_influxdb", "Create InfluxDB backup"),
                ("verify_influxdb", "Verify InfluxDB backup"),
                ("write_set", "Write backup set metadata"),
                ("refresh", "Refresh backup list"),
            ]
        else:
            keys = [
                ("preflight", "Preflight"),
                (f"create_{scope}", f"Create {scope} backup"),
                (f"verify_{scope}", f"Verify {scope} backup"),
                ("refresh", "Refresh backup list"),
            ]
        return [{"key": key, "label": label} for key, label in keys]

    def create_backup(self, request, progress=None):
        scope = (request or {}).get("scope")
        if scope not in CREATE_SCOPES:
            raise BackupRestoreError("unsupported backup scope")
        password = self._create_password(request)
        env = self._env()
        steps = _ProgressSteps(progress)
        steps.append(_step("preflight", "ok", "Preflight",
                           detail=f"Backup directory: {env.display_backup_dir()}"))
        try:
            if scope == "system":
                return self._create_system(env, steps, password)
            return self._create_single(env, steps, scope, password)
        except BackupRestoreError as exc:
            return {"ok": False, "status": "failed", "message": str(exc),
                    "steps": list(steps), "archives": []}

    @staticmethod
    def _create_password(request):
        request = request or {}
        if not request.get("encrypt"):
            return None
        password = request.get("password")
        if not isinstance(password, str) or not password:
            raise BackupRestoreError("an encryption password is required")
        return password

    def _create_single(self, env, steps, scope, password):
        record = self._create_one(env, steps, scope, password)
        steps.append(_step("refresh", "ok", "Refresh backup list"))
        return {"ok": True, "status": "completed", "steps": list(steps),
                "archives": [record], "backup_set": None}

    def _create_system(self, env, steps, password):
        archives = []
        warnings = []
        # Config and databases are the required members of a system set.
        archives.append(self._create_one(env, steps, "config", password))
        archives.append(self._create_one(env, steps, "databases", password))

        influx_record = self._create_influx_member(env, steps, password, warnings)
        if influx_record is not None:
            archives.append(influx_record)

        set_status = "complete" if influx_record is not None or env.influx.get(
            "reason"
        ) else "partial"
        set_record = self._write_set(env, archives, warnings, set_status)
        steps.append(_step("write_set", "ok", "Write backup set metadata",
                           detail=set_record["id"]))
        steps.append(_step("refresh", "ok", "Refresh backup list"))
        return {"ok": True, "status": "completed", "steps": list(steps),
                "archives": archives, "backup_set": set_record, "warnings": warnings}

    def _create_influx_member(self, env, steps, password, warnings):
        evaluation = env.influx
        if not evaluation.get("supported"):
            message = evaluation.get("message") or "InfluxDB backup is not available."
            steps.append(_step("create_influxdb", "skipped",
                               "Create InfluxDB backup", detail=message))
            steps.append(_step("verify_influxdb", "skipped",
                               "Verify InfluxDB backup", detail="Skipped."))
            warnings.append(message)
            return None
        try:
            record = self._create_one(env, steps, "influxdb", password)
        except BackupRestoreError as exc:
            steps.append(_step("verify_influxdb", "warning",
                               "Verify InfluxDB backup", detail=str(exc)))
            warnings.append(f"InfluxDB backup could not be created: {exc}")
            return None
        return record

    def _create_one(self, env, steps, scope, password):
        create_key = f"create_{scope}"
        verify_key = f"verify_{scope}"
        try:
            path = self._create_archive(env, scope, password)
        except backup_mod.BackupError as exc:
            steps.append(_step(create_key, "error", f"Create {scope} backup",
                               detail=str(exc)))
            raise BackupRestoreError(str(exc))
        steps.append(_step(create_key, "ok", f"Create {scope} backup",
                           detail=os.path.basename(path)))
        record = self._verify_archive(env, path, scope, password)
        if not record["verified"]:
            steps.append(_step(verify_key, "error", f"Verify {scope} backup",
                               detail=record.get("error")))
            raise BackupRestoreError(
                record.get("error") or f"{scope} backup could not be verified"
            )
        steps.append(_step(verify_key, "ok", f"Verify {scope} backup",
                           detail="Checksums verified."))
        return record

    def _create_archive(self, env, scope, password):
        if scope == "config":
            return backup_mod.create_config_backup(
                env.config, base_dir=env.base_dir, config_path=env.config_path,
                backup_dir=env.backup_dir, password=password,
            )
        if scope == "databases":
            return backup_mod.create_database_backup(
                env.config, base_dir=env.base_dir, backup_dir=env.backup_dir,
                password=password,
            )
        if scope == "influxdb":
            return self._create_influxdb_archive(env, password)
        raise BackupRestoreError("unsupported backup scope")

    def _create_influxdb_archive(self, env, password):
        # Bundled InfluxDB data lives in a container volume, so its backup is the
        # one operation that needs the running EMS/InfluxDB context; it is run
        # through the EMS tool and the resulting archive lands in the shared
        # backup directory. Kept isolated + injectable so tests never need Docker.
        before = set(os.listdir(env.backup_dir)) if os.path.isdir(env.backup_dir) else set()
        self._run_influx_backup_tool()
        after = set(os.listdir(env.backup_dir)) if os.path.isdir(env.backup_dir) else set()
        new = sorted(
            name for name in after - before
            if name.startswith("ems-influxdb-")
        )
        if not new:
            raise BackupRestoreError(
                "InfluxDB backup produced no archive in the backup directory"
            )
        return os.path.join(env.backup_dir, new[-1])

    def _run_influx_backup_tool(self):
        from admin.ems_tool import EmsToolRunner
        tool = self._ems_tool or EmsToolRunner()
        result = tool.run(self._context_provider(), ("backup", "create", "--type", "influxdb"))
        if getattr(result, "blocked", False):
            raise BackupRestoreError(result.message or "no EMS context for InfluxDB backup")
        if getattr(result, "returncode", 1) != 0:
            raise BackupRestoreError(result.detail or "InfluxDB backup failed")

    def _verify_archive(self, env, path, expected_type, password=None):
        record = {"id": _safe_id(os.path.basename(path)),
                  "name": os.path.basename(path), "type": expected_type,
                  "verified": False, "error": None, "size_bytes": 0}
        try:
            if not os.path.isfile(path):
                raise BackupRestoreError("archive does not exist")
            size = os.path.getsize(path)
            record["size_bytes"] = size
            if size <= 0:
                raise BackupRestoreError("archive is empty")
            info = backup_mod.inspect_backup(path, password=password)
            manifest = info.get("manifest")
            if manifest is None:
                raise BackupRestoreError("manifest could not be read")
            if manifest.get("backup_type") != expected_type:
                raise BackupRestoreError("manifest backup type does not match")
            plan = backup_mod.plan_restore(path, base_dir=env.base_dir, password=password)
            if any(not entry.get("checksum_ok") for entry in plan["files"]):
                raise BackupRestoreError("archive checksums are invalid")
        except (backup_mod.BackupError, BackupRestoreError) as exc:
            record["error"] = str(exc)
            return record
        record["verified"] = True
        return record

    def _write_set(self, env, archives, warnings, status):
        set_id = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S") + "-system"
        record = {
            "id": set_id,
            "created_at": utc_now_iso(),
            "purpose": "manual",
            "label": "System backup",
            "archives": [
                {"type": a["type"], "name": a["name"],
                 "optional": a["type"] == "influxdb"}
                for a in archives
            ],
            "status": status,
            "warnings": list(warnings),
        }
        BackupStore(env).write_set(record)
        return record

    # --- restore preview -------------------------------------------------

    def create_restore_plan(self, request):
        request = request or {}
        scope = request.get("scope", "config")
        if scope not in RESTORE_SCOPES:
            raise BackupRestoreError("unsupported restore scope")
        # Admin restore always previews before writing and requires an explicit
        # confirm, so differing files default to replace rather than blocking.
        conflict_policy = request.get("conflict_policy", "replace")
        if conflict_policy not in CONFLICT_POLICIES:
            raise BackupRestoreError("unsupported conflict policy")
        backup_id = request.get("id")
        password = request.get("password")
        rollback = request.get("rollback", True)
        auto_rollback = request.get("auto_rollback", True)

        env = self._env()
        store = BackupStore(env)
        if store.read_set(backup_id) is not None:
            targets, kind = self._set_targets(store, backup_id, scope), "set"
        else:
            targets, kind = [self._archive_target(store, backup_id)], "archive"

        # Apply generic (config/database) members before the InfluxDB member so a
        # failed InfluxDB restore can be reconciled by rolling back the generic
        # members Admin already applied (InfluxDB rollback stays owned by EMS CLI).
        targets.sort(key=lambda t: t.backup_type == INFLUXDB_BACKUP_TYPE)

        files, summary, warnings, blocked, block_reason, manifest_summary = \
            self._preview_targets(env, store, targets, conflict_policy, password)

        plan = RestorePlan(
            plan_id=uuid.uuid4().hex,
            kind=kind,
            backup_id=backup_id,
            scope=scope,
            conflict_policy=conflict_policy,
            rollback_enabled=bool(rollback),
            auto_rollback_enabled=bool(auto_rollback),
            password=password,
            created_at=utc_now_iso(),
            expires_at=(datetime.now(timezone.utc) + timedelta(
                seconds=PLAN_TTL_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            targets=targets,
            manifest_summary=manifest_summary,
            files=files,
            summary=summary,
            warnings=warnings,
            blocked=blocked,
            block_reason=block_reason,
        )
        self.plans.put(plan)
        return {"ok": True, **plan.public()}

    def _archive_target(self, store, backup_id):
        path = store.resolve(backup_id)
        name = os.path.basename(path)
        backup_type, _ = _parse_archive_name(name)
        return RestorePlanTarget(
            archive_id=backup_id, name=name, backup_type=backup_type,
            archive_sha256=backup_mod._sha256_file(path),
        )

    def _set_targets(self, store, set_id, scope):
        record = store.read_set(set_id)
        decorated = store._decorate_set(record)
        wanted = None if scope in ("system", None) else {scope}
        targets = []
        for entry in decorated["archives"]:
            if not entry["present"]:
                continue
            if wanted is not None and entry["type"] not in wanted:
                continue
            path = os.path.join(store.env.backup_dir, entry["name"])
            targets.append(RestorePlanTarget(
                archive_id=entry["id"], name=entry["name"],
                backup_type=entry["type"],
                archive_sha256=backup_mod._sha256_file(path),
            ))
        if not targets:
            raise BackupRestoreError("no matching archives in the backup set")
        return targets

    def _preview_targets(self, env, store, targets, conflict_policy, password):
        files = []
        would_restore = would_replace = would_skip = 0
        warnings = []
        blocked = False
        block_reason = None
        manifest_summary = None
        has_conflict = False

        for target in targets:
            path = store.resolve(target.archive_id)
            if backup_mod.is_encrypted(path) and not password:
                raise BackupRestoreError(
                    "this backup is encrypted; a password is required to preview it"
                )
            if target.backup_type == INFLUXDB_BACKUP_TYPE:
                ok, message = self._run_influx_restore_cli(
                    env, store, target, password, dry_run=True
                )
                files.append({
                    "path": "bundled InfluxDB data",
                    "action": "would_restore_influxdb",
                    "kind": INFLUXDB_BACKUP_TYPE,
                    "archive": target.name,
                })
                if ok:
                    would_restore += 1
                    warnings.append(
                        "Bundled InfluxDB analytics data will be replaced with "
                        "this backup."
                    )
                else:
                    blocked = True
                    block_reason = message or "influxdb_preview_failed"
                    warnings.append(message or "InfluxDB restore preview failed.")
                continue
            try:
                plan = backup_mod.plan_restore(path, base_dir=env.base_dir,
                                               password=password)
            except backup_mod.BackupPasswordError:
                raise BackupRestoreError("the backup password is incorrect")
            except backup_mod.BackupError as exc:
                raise BackupRestoreError(str(exc))
            if manifest_summary is None:
                manifest_summary = BackupInspector._summarize(plan["manifest"])

            for entry in plan["files"]:
                action, counters = self._preview_action(entry, conflict_policy)
                if counters == "restore":
                    would_restore += 1
                elif counters == "replace":
                    would_replace += 1
                elif counters == "skip":
                    would_skip += 1
                if entry["status"] == "conflict":
                    has_conflict = True
                if not entry.get("checksum_ok"):
                    blocked = True
                    block_reason = "checksum_invalid"
                files.append({
                    "path": entry["path"], "action": action,
                    "kind": entry.get("kind"), "archive": target.name,
                })

        if has_conflict and conflict_policy == "abort":
            blocked = True
            block_reason = block_reason or "conflicts_require_policy"
            warnings.append(
                "Conflicting files were found; choose keep or replace to continue."
            )
        summary = {"would_restore": would_restore, "would_replace": would_replace,
                   "would_skip": would_skip}
        return files, summary, warnings, blocked, block_reason, manifest_summary

    @staticmethod
    def _preview_action(entry, conflict_policy):
        if not entry.get("checksum_ok"):
            return "checksum_invalid", None
        status = entry["status"]
        if status == "identical":
            return "would_skip_identical", "skip"
        if status == "new":
            return "would_restore_new", "restore"
        # conflict
        if conflict_policy == "replace":
            return "would_replace_conflict", "replace"
        if conflict_policy == "keep":
            return "would_keep_current", "skip"
        return "conflict_requires_policy", "skip"

    # --- restore execute -------------------------------------------------

    def plan_restore_steps(self, plan):
        steps = [{"key": "verify_plan", "label": "Verify restore plan"}]
        for idx, target in enumerate(plan.targets):
            suffix = f"_{idx}" if len(plan.targets) > 1 else ""
            label = target.backup_type or "archive"
            # EMS CLI owns the InfluxDB rollback + restore, so it is one step.
            if target.backup_type == INFLUXDB_BACKUP_TYPE:
                steps.append({"key": f"apply{suffix}",
                              "label": "Restore InfluxDB (EMS CLI)"})
                continue
            if plan.rollback_enabled:
                steps.append({"key": f"rollback{suffix}",
                              "label": f"Create {label} rollback backup"})
            steps.append({"key": f"apply{suffix}", "label": f"Restore {label}"})
            steps.append({"key": f"postcheck{suffix}",
                          "label": f"Verify restored {label}"})
        steps.append({"key": "done", "label": "Finish restore"})
        return steps

    def restore_from_plan(self, plan_id, confirm, progress=None):
        if confirm is not True:
            raise BackupRestoreError("restore requires confirm=true")
        plan = self.plans.get(plan_id)
        if plan is None:
            raise BackupRestoreError("unknown or expired restore plan")
        if plan.blocked:
            raise BackupRestoreError(
                plan.block_reason or "the restore plan is blocked"
            )

        env = self._env()
        store = BackupStore(env)
        # Re-verify each archive is unchanged since the preview.
        for target in plan.targets:
            path = store.resolve(target.archive_id)
            if backup_mod._sha256_file(path) != target.archive_sha256:
                raise BackupRestoreError(
                    "the backup archive changed after the preview; re-run the preview"
                )

        steps = _ProgressSteps(progress)
        steps.append(_step("verify_plan", "ok", "Verify restore plan"))
        return self._execute_restore(env, store, plan, steps)

    def _execute_restore(self, env, store, plan, steps):
        rollbacks = []
        # Create every generic rollback backup before any file is written so a
        # failure to capture the current state aborts the restore before it
        # starts. InfluxDB rollback is owned by EMS CLI, not created here.
        if plan.rollback_enabled:
            for idx, target in enumerate(plan.targets):
                if target.backup_type == INFLUXDB_BACKUP_TYPE:
                    rollbacks.append((target, None))
                    continue
                suffix = f"_{idx}" if len(plan.targets) > 1 else ""
                try:
                    rollback_path = self._create_rollback(env, target)
                except (backup_mod.BackupError, BackupRestoreError) as exc:
                    steps.append(_step(f"rollback{suffix}", "error",
                                       "Create rollback backup", detail=str(exc)))
                    return {"ok": False, "status": "failed",
                            "message": f"Rollback backup failed; restore not started: {exc}",
                            "steps": list(steps)}
                rollbacks.append((target, rollback_path))
                steps.append(_step(f"rollback{suffix}", "ok",
                                   "Create rollback backup",
                                   detail=os.path.basename(rollback_path)))
        else:
            rollbacks = [(target, None) for target in plan.targets]

        actions = []
        for idx, target in enumerate(plan.targets):
            suffix = f"_{idx}" if len(plan.targets) > 1 else ""
            if target.backup_type == INFLUXDB_BACKUP_TYPE:
                outcome = self._apply_influx_target(
                    env, store, plan, target, steps, suffix, rollbacks, idx, actions
                )
                if outcome is not None:
                    return outcome
                continue
            path = store.resolve(target.archive_id)
            try:
                result = self._apply_archive(env, path, plan, target)
            except backup_mod.BackupError as exc:
                steps.append(_step(f"apply{suffix}", "error", "Restore",
                                   detail=str(exc)))
                return self._maybe_auto_rollback(
                    env, plan, steps, rollbacks, applied_index=idx,
                    message=f"Restore failed: {exc}", actions=actions)
            actions.extend(result.get("actions", []))
            steps.append(_step(f"apply{suffix}", "ok", "Restore",
                               detail=target.name))

            check_ok, detail = self._post_restore_check(env, target)
            if not check_ok:
                steps.append(_step(f"postcheck{suffix}", "error",
                                   "Verify restored files", detail=detail))
                return self._maybe_auto_rollback(
                    env, plan, steps, rollbacks, applied_index=idx,
                    message=f"Post-restore check failed: {detail}", actions=actions)
            steps.append(_step(f"postcheck{suffix}", "ok", "Verify restored files",
                               detail=detail))

        steps.append(_step("done", "ok", "Finish restore"))
        rollback_name = os.path.basename(rollbacks[0][1]) if rollbacks and rollbacks[0][1] else None
        return {"ok": True, "status": "restored", "steps": list(steps),
                "actions": actions, "rollback_backup": rollback_name}

    def _apply_influx_target(self, env, store, plan, target, steps, suffix,
                             rollbacks, idx, actions):
        """Restore one bundled InfluxDB member through the EMS CLI.

        Returns a failure result dict, or ``None`` on success. EMS CLI creates
        and applies the InfluxDB rollback; on failure Admin only rolls back the
        generic members it applied before this one.
        """

        ok, message = self._run_influx_restore_cli(
            env, store, target, plan.password,
            dry_run=False, rollback_enabled=plan.rollback_enabled,
        )
        if ok:
            steps.append(_step(f"apply{suffix}", "ok", "Restore InfluxDB",
                               detail=target.name))
            return None
        steps.append(_step(f"apply{suffix}", "error", "Restore InfluxDB",
                           detail=message))
        message = f"InfluxDB restore failed: {message}"
        if any(t.backup_type != INFLUXDB_BACKUP_TYPE for t in plan.targets[:idx]):
            return self._maybe_auto_rollback(
                env, plan, steps, rollbacks, applied_index=idx,
                message=message, actions=actions)
        return {"ok": False, "status": "failed", "message": message,
                "steps": list(steps), "actions": actions,
                "rollback_backup": None}

    def _run_influx_restore_cli(self, env, store, target, password, *,
                                dry_run, rollback_enabled=True):
        """Run ``emsctl backup restore`` for a bundled InfluxDB archive.

        EMS CLI owns the InfluxDB restore + rollback; Admin only orchestrates it
        and never pushes InfluxDB through the generic file restore path. Returns
        ``(ok, message)``. The password, when set, is piped via stdin and never
        placed in argv or logged.
        """

        from admin.ems_tool import BLOCKED_MESSAGE, EmsToolRunner

        tool = self._ems_tool or EmsToolRunner()
        context = self._context_provider()
        mode = tool.resolve_mode(context)["mode"]
        if mode == "blocked":
            return False, BLOCKED_MESSAGE
        ems_path = resolve_ems_cli_backup_path(
            env, store.resolve(target.archive_id), mode
        )
        args = ["backup", "restore", ems_path]
        if dry_run:
            args.append("--dry-run")
        else:
            args += ["--on-conflict", "replace",
                     "--rollback" if rollback_enabled else "--no-rollback"]
        input_text = f"{password}\n" if password else None
        result = tool.run(context, tuple(args), input_text=input_text)
        if getattr(result, "blocked", False):
            return False, result.message or BLOCKED_MESSAGE
        if getattr(result, "returncode", 1) != 0:
            return False, result.detail or "the InfluxDB restore command failed"
        return True, None

    def _maybe_auto_rollback(self, env, plan, steps, rollbacks, applied_index,
                             message, actions=None):
        actions = actions or []
        rollback_name = None
        if rollbacks and rollbacks[0][1]:
            rollback_name = os.path.basename(rollbacks[0][1])
        if not plan.auto_rollback_enabled:
            return {"ok": False, "status": "failed", "message": message,
                    "steps": list(steps), "actions": actions,
                    "rollback_backup": rollback_name}
        # Roll back every generic archive that was (or was being) applied, newest
        # first. InfluxDB rollback is owned by EMS CLI, so it is skipped here.
        try:
            for idx in range(applied_index, -1, -1):
                _target, rollback_path = rollbacks[idx]
                if _target.backup_type == INFLUXDB_BACKUP_TYPE:
                    continue
                if rollback_path is None:
                    raise BackupRestoreError("no rollback backup was created")
                self._apply_rollback(env, rollback_path, plan.password)
        except (backup_mod.BackupError, BackupRestoreError) as exc:
            steps.append(_step("done", "error", "Automatic rollback", detail=str(exc)))
            return {
                "ok": False, "status": "rollback_failed",
                "message": ("Restore failed and automatic rollback also failed. "
                            "Manual recovery is required."),
                "steps": list(steps), "actions": actions,
                "rollback_backup": rollback_name,
            }
        steps.append(_step("done", "ok", "Automatic rollback",
                           detail="Rolled back to the pre-restore state."))
        return {
            "ok": False, "status": "rolled_back",
            "message": ("Restore failed after applying changes. Rollback was "
                        "restored successfully."),
            "steps": list(steps), "actions": actions,
            "rollback_backup": rollback_name,
        }

    def _create_rollback(self, env, target):
        if target.backup_type == "config":
            return backup_mod.create_rollback_backup(
                env.config, target.name, base_dir=env.base_dir,
                config_path=env.config_path, backup_dir=env.backup_dir,
            )
        if target.backup_type == "databases":
            return backup_mod.create_database_rollback_backup(
                env.config, target.name, base_dir=env.base_dir,
                backup_dir=env.backup_dir,
            )
        raise BackupRestoreError(
            f"automatic rollback is not supported for {target.backup_type} backups"
        )

    def _apply_archive(self, env, path, plan, target):
        # Final guard: never push an InfluxDB archive through generic file
        # restore; InfluxDB is restored only through the EMS CLI path.
        if target.backup_type == INFLUXDB_BACKUP_TYPE:
            raise BackupRestoreError(
                "InfluxDB archives must be restored through the EMS CLI, not the "
                "generic file restore path"
            )
        return backup_mod.restore_backup(
            path, base_dir=env.base_dir, password=plan.password,
            on_conflict=plan.conflict_policy,
        )

    def _apply_rollback(self, env, rollback_path, password):
        # Rollback always replaces so it fully returns to the captured state.
        return backup_mod.restore_backup(
            rollback_path, base_dir=env.base_dir, password=password,
            on_conflict="replace",
        )

    def _post_restore_check(self, env, target):
        # Read-only: never touches hardware, never restarts containers. Config
        # backups must leave a parseable config.json behind.
        if target.backup_type == "config":
            config_file = os.path.join(env.base_dir, "config", "config.json")
            if not os.path.isfile(config_file):
                config_file = env.config_path
            try:
                with open(config_file, encoding="utf-8") as handle:
                    json.load(handle)
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                return False, f"config.json is not valid JSON: {exc}"
            return True, "config.json parses."
        return True, "Restored files are present."
