# SPDX-License-Identifier: AGPL-3.0-or-later
"""Manual backup and restore helpers.

Supports config, local SQLite databases, bundled InfluxDB data, optional
encryption, manifests, and rollback backups. See ``docs/cli.md`` for the
operator-facing contract and ``docs/influxdb.md`` for the InfluxDB details.

This module is import-side-effect-free.
"""

import difflib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone

from ems import backup_crypto
from ems import influx_setup
from ems.build_info import collect_build_info
from ems.config import normalize_influxdb_config, safe_bool
from ems.diagnostics import DIAGNOSE_SCHEMA_VERSION, SUPPORT_BUNDLE_VERSION
from ems.paths import BASE_DIR

APP_NAME = "ems-solarflow-api-control"
BACKUP_FORMAT_VERSION = 1
CONFIG_BACKUP_FORMAT_VERSION = 1
MANIFEST_NAME = "backup-manifest.json"
DEFAULT_BACKUP_DIR_NAME = os.path.join("data", "backups")
CONTAINER_BACKUP_DIR = "/app/data/backups"
OFFICIAL_APP_DIR = "/app"
DEFAULT_COMPRESSION_LEVEL = 3

BACKUP_TYPES = ("config", "databases", "influxdb")
BACKUP_PURPOSES = ("manual", "auto", "rollback")

# Bundled InfluxDB data backup.
INFLUX_DISABLED_MESSAGE = "InfluxDB analytics is disabled. Nothing to back up."
INFLUX_EXTERNAL_MESSAGE = (
    "External InfluxDB detected.\n"
    "Automatic InfluxDB backup/restore is not supported for external mode.\n"
    "Use your external InfluxDB backup strategy."
)
INFLUX_ARCHIVE_SUBDIR = "influxdb"

# Defaults mirror config.py / the project layout.
DEFAULT_RUNTIME_STATE_PATH = "data/runtime-state.json"
DEFAULT_DASHBOARD_AUTH_PATH = "config/dashboard-auth.json"
DEFAULT_DASHBOARD_CERT_PATH = "config/dashboard.crt"
DEFAULT_DASHBOARD_KEY_PATH = "config/dashboard.key"
DEFAULT_INFLUX_SECRET_PATH = "deploy/docker/influxdb.env"

# Database backup defaults mirror config.py.
DEFAULT_DASHBOARD_DB_PATH = "data/ems_dashboard.sqlite"
DEFAULT_STATE_DB_PATH = "data/ems_state.sqlite"

# Config backups do not include SQLite databases; use the dedicated type.
SKIP_DATABASE_REASON = "use_databases_backup_type"
MISSING_DATABASE_REASON = "missing"
# Database backups only record InfluxDB status; data uses the influxdb type.
INFLUX_SKIP_REASON = "use_influxdb_backup_type"


class BackupError(Exception):
    """Raised for backup/restore failures that are safe to show the user."""


# Re-export so callers only need to import ems.backup for password handling.
BackupPasswordError = backup_crypto.BackupPasswordError
BackupFormatError = backup_crypto.BackupFormatError

DEFAULT_ENCRYPTION_ALGORITHM = backup_crypto.DEFAULT_ALGORITHM
SUPPORTED_ENCRYPTION_ALGORITHMS = tuple(backup_crypto.SUPPORTED_AEAD_ALGORITHMS)


def build_encryption_options(
    algorithm=None, chunk_size=None, kdf_iterations=None
):
    """Validate optional encryption parameters and return engine kwargs.

    Raises :class:`BackupError` for any out-of-range or unknown value so CLI
    misuse fails with a clear message instead of a traceback.
    """

    algorithm = algorithm or DEFAULT_ENCRYPTION_ALGORITHM
    if algorithm not in backup_crypto.SUPPORTED_AEAD_ALGORITHMS:
        raise BackupError(
            f"unsupported encryption algorithm: {algorithm!r} "
            f"(choose from {', '.join(SUPPORTED_ENCRYPTION_ALGORITHMS)})"
        )

    chunk = backup_crypto.DEFAULT_CHUNK_SIZE if chunk_size is None else int(chunk_size)
    if not backup_crypto.MIN_CHUNK_SIZE <= chunk <= backup_crypto.MAX_CHUNK_SIZE:
        raise BackupError(
            f"chunk size out of range: {chunk} bytes (allowed "
            f"{backup_crypto.MIN_CHUNK_SIZE}..{backup_crypto.MAX_CHUNK_SIZE})"
        )

    iterations = (
        backup_crypto.DEFAULT_KDF_ITERATIONS
        if kdf_iterations is None
        else int(kdf_iterations)
    )
    if not (
        backup_crypto.MIN_KDF_ITERATIONS
        <= iterations
        <= backup_crypto.MAX_KDF_ITERATIONS
    ):
        raise BackupError(
            f"KDF iterations out of range: {iterations} (allowed "
            f"{backup_crypto.MIN_KDF_ITERATIONS}..{backup_crypto.MAX_KDF_ITERATIONS})"
        )

    return {
        "algorithm": algorithm,
        "chunk_size": chunk,
        "iterations": iterations,
    }


def _encryption_method(encryption_options, encrypted):
    if not encrypted:
        return None
    algorithm = (encryption_options or {}).get(
        "algorithm", DEFAULT_ENCRYPTION_ALGORITHM
    )
    return backup_crypto.encryption_method_label(algorithm)


def parse_chunk_size(text):
    """Parse a chunk size like ``4M``/``512K``/``1048576`` into bytes."""

    if text is None:
        return None
    value = str(text).strip().upper()
    multiplier = 1
    if value.endswith("K"):
        multiplier, value = 1024, value[:-1]
    elif value.endswith("M"):
        multiplier, value = 1024 * 1024, value[:-1]
    try:
        number = int(value)
    except ValueError as exc:
        raise BackupError(f"invalid chunk size: {text!r}") from exc
    if number <= 0:
        raise BackupError(f"invalid chunk size: {text!r}")
    return number * multiplier


# ---------------------------------------------------------------------------
# File selection
# ---------------------------------------------------------------------------

def _resolve(path, base_dir):
    """Resolve ``path`` relative to ``base_dir`` (absolute paths pass through)."""

    if not path:
        return path
    if os.path.isabs(path):
        return path
    return os.path.join(base_dir, path)


def _archive_rel(abs_path, base_dir):
    """Return a safe project-relative archive path for ``abs_path``.

    Paths inside ``base_dir`` keep their relative layout; anything that would
    escape the project root falls back to its basename so restore stays inside
    the project.
    """

    rel = os.path.relpath(abs_path, base_dir)
    if rel.startswith("..") or os.path.isabs(rel):
        rel = os.path.basename(abs_path)
    return rel.replace(os.sep, "/")


def collect_config_backup_files(
    config,
    base_dir=None,
    config_path=None,
    runtime_state_path=None,
    dashboard_auth_path=None,
):
    """Plan which files go into a config backup.

    Returns ``(included, skipped)`` where ``included`` entries are dicts with
    ``abs_path``, ``arcname``, ``kind`` and ``sensitive`` and ``skipped`` entries
    are dicts with ``path`` and ``reason``.
    """

    base_dir = base_dir or BASE_DIR
    config = config if isinstance(config, dict) else {}
    system = config.get("system", {}) if isinstance(config.get("system"), dict) else {}
    dashboard = (
        config.get("dashboard", {})
        if isinstance(config.get("dashboard"), dict)
        else {}
    )

    candidates = []

    # config.json — always when present.
    cfg_path = config_path or _resolve("config.json", base_dir)
    candidates.append((cfg_path, "config", True))

    # runtime-state.json
    if runtime_state_path:
        rt_path = runtime_state_path
    else:
        rt_path = _resolve(
            system.get("runtime_state_path", DEFAULT_RUNTIME_STATE_PATH),
            base_dir,
        )
    candidates.append((rt_path, "runtime_state", False))

    # dashboard auth / cert / key — included only when present.
    if dashboard_auth_path:
        auth_path = dashboard_auth_path
    else:
        auth_path = _resolve(
            dashboard.get("auth_file", DEFAULT_DASHBOARD_AUTH_PATH),
            base_dir,
        )
    candidates.append((auth_path, "dashboard_auth", True))
    candidates.append((
        _resolve(
            dashboard.get("ssl_cert_file", DEFAULT_DASHBOARD_CERT_PATH),
            base_dir,
        ),
        "dashboard_cert",
        False,
    ))
    candidates.append((
        _resolve(
            dashboard.get("ssl_key_file", DEFAULT_DASHBOARD_KEY_PATH),
            base_dir,
        ),
        "dashboard_key",
        True,
    ))

    included = []
    skipped = []

    for abs_path, kind, sensitive in candidates:
        if abs_path and os.path.isfile(abs_path):
            included.append({
                "abs_path": abs_path,
                "arcname": _archive_rel(abs_path, base_dir),
                "kind": kind,
                "sensitive": sensitive,
            })

    # Bundled InfluxDB secret — only when bundled analytics is enabled.
    influx = normalize_influxdb_config(config.get("influxdb"))
    if influx.get("enabled") and influx.get("mode") == "bundled":
        secret_path = _resolve(
            influx.get("secret_file", DEFAULT_INFLUX_SECRET_PATH),
            base_dir,
        )
        if secret_path and os.path.isfile(secret_path):
            included.append({
                "abs_path": secret_path,
                "arcname": _archive_rel(secret_path, base_dir),
                "kind": "influxdb_secret",
                "sensitive": True,
            })

    # Record databases as skipped here; they have their own backup type.
    for db_default, cfg_key, section in (
        ("data/ems_dashboard.sqlite", "database_path", dashboard),
        ("data/ems_state.sqlite", "state_database_path",
         config.get("battery_full_charge_assist", {})),
    ):
        section = section if isinstance(section, dict) else {}
        db_path = _resolve(section.get(cfg_key, db_default), base_dir)
        if db_path and os.path.isfile(db_path):
            skipped.append({
                "path": _archive_rel(db_path, base_dir),
                "reason": SKIP_DATABASE_REASON,
            })

    return included, skipped


def collect_database_backup_files(
    config,
    base_dir=None,
    dashboard_db_path=None,
    state_db_path=None,
):
    """Plan which local SQLite databases go into a database backup.

    Returns ``(present, missing)``. ``present`` entries are dicts with
    ``abs_path``, ``arcname``, ``kind`` (``"sqlite"``), ``role`` and
    ``sensitive``; ``missing`` entries are dicts with ``path``, ``kind``,
    ``role`` and ``reason``. Missing databases never fail the backup.
    """

    base_dir = base_dir or BASE_DIR
    config = config if isinstance(config, dict) else {}
    dashboard = (
        config.get("dashboard", {})
        if isinstance(config.get("dashboard"), dict)
        else {}
    )
    assist = (
        config.get("battery_full_charge_assist", {})
        if isinstance(config.get("battery_full_charge_assist"), dict)
        else {}
    )

    if dashboard_db_path:
        dash_path = dashboard_db_path
    else:
        dash_path = _resolve(
            dashboard.get("database_path", DEFAULT_DASHBOARD_DB_PATH), base_dir
        )
    if state_db_path:
        st_path = state_db_path
    else:
        st_path = _resolve(
            assist.get("state_database_path", DEFAULT_STATE_DB_PATH), base_dir
        )

    candidates = (
        (dash_path, "dashboard_history"),
        (st_path, "ems_state"),
    )

    present = []
    missing = []
    for abs_path, role in candidates:
        if abs_path and os.path.isfile(abs_path):
            present.append({
                "abs_path": abs_path,
                "arcname": _archive_rel(abs_path, base_dir),
                "kind": "sqlite",
                "role": role,
                # Not a classic secret, but local energy/runtime history.
                "sensitive": False,
                "privacy_relevant": True,
            })
        else:
            rel = _archive_rel(abs_path, base_dir) if abs_path else role
            missing.append({
                "path": rel,
                "kind": "sqlite",
                "role": role,
                "reason": MISSING_DATABASE_REASON,
            })
    return present, missing


def detect_influxdb_status(config):
    """Detect InfluxDB analytics status for the database-backup manifest.

    A *database* backup never contains InfluxDB data; this only records whether
    it is enabled and in which mode so the manifest and CLI can point the user
    at the dedicated ``influxdb`` backup type (:func:`create_influxdb_backup`).
    """

    config = config if isinstance(config, dict) else {}
    influx = normalize_influxdb_config(config.get("influxdb"))
    enabled = bool(influx.get("enabled"))
    if not enabled:
        return {
            "detected": False,
            "enabled": False,
            "mode": None,
            "included": False,
            "reason": "not_enabled",
        }
    return {
        "detected": True,
        "enabled": True,
        "mode": influx.get("mode"),
        "included": False,
        "reason": INFLUX_SKIP_REASON,
    }


def _sqlite_backup_copy(src_path, dst_path):
    """Snapshot a SQLite database into ``dst_path`` via the online backup API.

    This never copies the live file directly, so the staged copy stays
    internally consistent even if the EMS is writing to the source.
    """

    try:
        src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise BackupError(
            f"cannot open SQLite database {src_path}: {exc}"
        ) from exc
    try:
        dst = sqlite3.connect(dst_path)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    except sqlite3.Error as exc:
        raise BackupError(
            f"SQLite backup failed for {src_path}: {exc}"
        ) from exc
    finally:
        src.close()


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _utc_now_iso(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_timestamp(now=None):
    now = now or datetime.now()
    return now.strftime("%Y-%m-%d-%H%M%S")


def _manifest_file_entry(entry):
    file_entry = {
        "path": entry["arcname"],
        "kind": entry["kind"],
        "sensitive": bool(entry["sensitive"]),
        "size_bytes": os.path.getsize(entry["abs_path"]),
        "sha256": _sha256_file(entry["abs_path"]),
    }
    if entry.get("privacy_relevant"):
        file_entry["privacy_relevant"] = True
    return file_entry


def build_manifest(
    included,
    skipped,
    *,
    backup_type="config",
    backup_purpose="manual",
    encrypted=False,
    encryption_method=None,
    rollback_for=None,
    created_at=None,
    databases=None,
    influxdb=None,
):
    build = collect_build_info()
    manifest = {
        "backup_format": BACKUP_FORMAT_VERSION,
        "backup_type": backup_type,
        "backup_purpose": backup_purpose,
        "created_at": created_at or _utc_now_iso(),
        "app": APP_NAME,
        "source": {
            "ems_version": build["ems_version"],
            "build_label": build["build_label"],
            "git_commit": build["git_commit"],
            "git_commit_short": build["git_commit_short"],
            "git_branch": build["git_branch"],
            "git_describe": build["git_describe"],
            "git_dirty": build["git_dirty"],
            "build_id": build["build_id"],
            "build_serial": build["build_serial"],
            "channel": build["channel"],
        },
        "contracts": {
            "config_backup_format_version": CONFIG_BACKUP_FORMAT_VERSION,
            "diagnose_schema_version": DIAGNOSE_SCHEMA_VERSION,
            "support_bundle_version": SUPPORT_BUNDLE_VERSION,
        },
        "encryption": {
            "enabled": bool(encrypted),
            "method": encryption_method if encrypted else None,
        },
        "files": [
            _manifest_file_entry(entry)
            for entry in included
        ],
        "skipped": list(skipped),
    }
    if databases is not None:
        manifest["databases"] = list(databases)
    if influxdb is not None:
        manifest["influxdb"] = dict(influxdb)
    if backup_purpose == "rollback" and rollback_for:
        manifest["rollback_for"] = rollback_for
    return manifest


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

def _in_official_app_layout(
    base_dir=None, app_dir=OFFICIAL_APP_DIR, path_exists=os.path.exists
):
    # The /.dockerenv marker also exists in CI/devcontainers, so it alone is too
    # broad. Only the official image layout (app at /app with mounted config/data)
    # should report container mode; native and container defaults both live
    # under the persistent data directory.
    base_dir = base_dir or BASE_DIR
    return (
        os.path.abspath(base_dir) == app_dir
        and path_exists(os.path.join(app_dir, "config"))
        and path_exists(os.path.join(app_dir, "data"))
    )


def running_in_container(
    environ=None, docker_env_path="/.dockerenv", base_dir=None,
    app_dir=OFFICIAL_APP_DIR,
):
    # An explicit EMS_IN_CONTAINER (truthy or falsy) always wins so the compose
    # overlay and tests can pin the answer. The Docker marker only forces the
    # container backup default when the official /app layout is present, so
    # native tests in temp dirs keep their project-local backup directory.
    environ = os.environ if environ is None else environ
    flag = str(environ.get("EMS_IN_CONTAINER", "")).strip()
    if flag:
        return safe_bool(flag)
    return os.path.exists(docker_env_path) and _in_official_app_layout(
        base_dir, app_dir
    )


def container_detection_source(
    environ=None, docker_env_path="/.dockerenv", base_dir=None,
    app_dir=OFFICIAL_APP_DIR,
):
    """Name the signal that put backups in container mode (None if native)."""
    environ = os.environ if environ is None else environ
    flag = str(environ.get("EMS_IN_CONTAINER", "")).strip()
    if flag:
        return "EMS_IN_CONTAINER" if safe_bool(flag) else None
    if os.path.exists(docker_env_path) and _in_official_app_layout(
        base_dir, app_dir
    ):
        return docker_env_path
    return None


def default_backup_dir(base_dir=None, environ=None):
    if running_in_container(environ, base_dir=base_dir):
        return CONTAINER_BACKUP_DIR
    return os.path.join(base_dir or BASE_DIR, DEFAULT_BACKUP_DIR_NAME)


def backup_filename(backup_type, backup_purpose, encrypted, timestamp):
    name = f"ems-{backup_type}-{backup_purpose}-{timestamp}.tar.gz"
    return name + ".enc" if encrypted else name


_BACKUP_SUFFIXES = (".tar.gz.enc", ".tar.gz")


def _split_backup_suffix(path):
    for suffix in _BACKUP_SUFFIXES:
        if path.endswith(suffix):
            return path[: -len(suffix)], suffix
    stem, ext = os.path.splitext(path)
    return stem, ext


def _link_unique(source, preferred_path):
    """Hard-link ``source`` to ``preferred_path`` without ever overwriting.

    Uses ``os.link`` (atomic, fails if the target exists) so two backups made in
    the same second resolve to distinct ``...-2``/``...-3`` paths instead of one
    silently clobbering the other. Returns the path that was created.
    """

    stem, suffix = _split_backup_suffix(preferred_path)
    candidate = preferred_path
    index = 2
    while True:
        try:
            os.link(source, candidate)
            return candidate
        except FileExistsError:
            candidate = f"{stem}-{index}{suffix}"
            index += 1


def _reject_symlink_sources(included):
    for entry in included:
        if os.path.islink(entry["abs_path"]):
            raise BackupError(
                f"Refusing to back up symlink path: {entry['arcname']}"
            )


def _write_tar(archive_path, manifest, included, compression_level):
    with tarfile.open(
        archive_path, "w:gz", compresslevel=compression_level
    ) as tar:
        manifest_bytes = json.dumps(
            manifest, indent=2, sort_keys=True
        ).encode("utf-8")
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(manifest_bytes)
        info.mtime = 0
        tar.addfile(info, io.BytesIO(manifest_bytes))

        for entry in included:
            tar.add(entry["abs_path"], arcname=entry["arcname"])


def _emit_archive(
    backup_type,
    backup_purpose,
    manifest,
    included,
    backup_dir,
    encrypted,
    password,
    compression_level,
    now,
    *,
    encryption_options=None,
):
    """Write (and optionally encrypt) the final archive; return its path.

    The archive is built into a temp file and then atomically linked to a unique
    destination, so an existing backup of the same type/purpose/second is never
    overwritten. Temp files are always cleaned up, including on failure.
    """

    _reject_symlink_sources(included)

    if encrypted:
        try:
            backup_crypto.validate_encryption_params(**(encryption_options or {}))
        except (ValueError, TypeError) as exc:
            raise BackupError(f"invalid encryption options: {exc}") from exc

    os.makedirs(backup_dir, exist_ok=True)
    timestamp = _local_timestamp(now if isinstance(now, datetime) else None)
    preferred_path = os.path.join(
        backup_dir, backup_filename(backup_type, backup_purpose, encrypted, timestamp)
    )

    fd, tmp_archive = tempfile.mkstemp(suffix=".part", dir=backup_dir)
    os.close(fd)
    tmp_plain = None
    try:
        if not encrypted:
            _write_tar(tmp_archive, manifest, included, compression_level)
        else:
            fd_plain, tmp_plain = tempfile.mkstemp(suffix=".tar.gz", dir=backup_dir)
            os.close(fd_plain)
            _write_tar(tmp_plain, manifest, included, compression_level)
            backup_crypto.encrypt_file(
                tmp_plain, tmp_archive, password, **(encryption_options or {})
            )
        return _link_unique(tmp_archive, preferred_path)
    finally:
        for path in (tmp_archive, tmp_plain):
            if path and os.path.exists(path):
                os.remove(path)


def create_config_backup(
    config,
    *,
    base_dir=None,
    config_path=None,
    runtime_state_path=None,
    dashboard_auth_path=None,
    backup_dir=None,
    backup_purpose="manual",
    rollback_for=None,
    password=None,
    encryption_options=None,
    compression_level=DEFAULT_COMPRESSION_LEVEL,
    now=None,
):
    """Create a config backup and return the path to the created archive."""

    if backup_purpose not in BACKUP_PURPOSES:
        raise BackupError(f"invalid backup purpose: {backup_purpose}")

    base_dir = base_dir or BASE_DIR
    backup_dir = backup_dir or default_backup_dir(base_dir)
    encrypted = bool(password)
    encryption_options = encryption_options if encrypted else None

    included, skipped = collect_config_backup_files(
        config,
        base_dir=base_dir,
        config_path=config_path,
        runtime_state_path=runtime_state_path,
        dashboard_auth_path=dashboard_auth_path,
    )
    if not included:
        raise BackupError(
            "no config files found to back up (config.json missing?)"
        )

    manifest = build_manifest(
        included,
        skipped,
        backup_type="config",
        backup_purpose=backup_purpose,
        encrypted=encrypted,
        encryption_method=_encryption_method(encryption_options, encrypted),
        rollback_for=rollback_for,
        created_at=_utc_now_iso(now if isinstance(now, datetime) else None),
    )

    return _emit_archive(
        "config",
        backup_purpose,
        manifest,
        included,
        backup_dir,
        encrypted,
        password,
        compression_level,
        now,
        encryption_options=encryption_options,
    )


def create_rollback_backup(config, rollback_for, *, password=None, **kwargs):
    """Create a config rollback backup tagged for ``rollback_for``."""

    return create_config_backup(
        config,
        backup_purpose="rollback",
        rollback_for=rollback_for,
        password=password,
        **kwargs,
    )


def create_database_backup(
    config,
    *,
    base_dir=None,
    dashboard_db_path=None,
    state_db_path=None,
    backup_dir=None,
    backup_purpose="manual",
    rollback_for=None,
    password=None,
    encryption_options=None,
    compression_level=DEFAULT_COMPRESSION_LEVEL,
    now=None,
):
    """Create a database backup and return the path to the created archive.

    Local SQLite databases are snapshotted consistently into a temporary
    staging directory (never a live file copy) before being archived. Missing
    databases are recorded in the manifest rather than failing the backup.
    InfluxDB data is detected but not included in this task.
    """

    if backup_purpose not in BACKUP_PURPOSES:
        raise BackupError(f"invalid backup purpose: {backup_purpose}")

    base_dir = base_dir or BASE_DIR
    backup_dir = backup_dir or default_backup_dir(base_dir)
    encrypted = bool(password)
    encryption_options = encryption_options if encrypted else None

    present, missing = collect_database_backup_files(
        config,
        base_dir=base_dir,
        dashboard_db_path=dashboard_db_path,
        state_db_path=state_db_path,
    )
    influx = detect_influxdb_status(config)

    os.makedirs(backup_dir, exist_ok=True)
    staging = tempfile.mkdtemp(prefix="ems-db-backup-")
    try:
        staged = []
        for entry in present:
            staged_path = os.path.join(staging, *entry["arcname"].split("/"))
            os.makedirs(os.path.dirname(staged_path) or staging, exist_ok=True)
            _sqlite_backup_copy(entry["abs_path"], staged_path)
            staged.append({
                "abs_path": staged_path,
                "arcname": entry["arcname"],
                "kind": entry["kind"],
                "role": entry["role"],
                "sensitive": entry["sensitive"],
                "privacy_relevant": entry.get("privacy_relevant", False),
            })

        databases = [
            {
                "path": entry["arcname"],
                "included": True,
                "kind": entry["kind"],
                "role": entry["role"],
                "size_bytes": os.path.getsize(entry["abs_path"]),
            }
            for entry in staged
        ]
        databases.extend(
            {
                "path": entry["path"],
                "included": False,
                "kind": entry["kind"],
                "role": entry["role"],
                "reason": entry["reason"],
            }
            for entry in missing
        )

        manifest = build_manifest(
            staged,
            [],
            backup_type="databases",
            backup_purpose=backup_purpose,
            encrypted=encrypted,
            encryption_method=_encryption_method(encryption_options, encrypted),
            rollback_for=rollback_for,
            created_at=_utc_now_iso(now if isinstance(now, datetime) else None),
            databases=databases,
            influxdb=influx,
        )

        return _emit_archive(
            "databases",
            backup_purpose,
            manifest,
            staged,
            backup_dir,
            encrypted,
            password,
            compression_level,
            now,
            encryption_options=encryption_options,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def create_database_rollback_backup(
    config, rollback_for, *, password=None, **kwargs
):
    """Create a database rollback backup tagged for ``rollback_for``."""

    return create_database_backup(
        config,
        backup_purpose="rollback",
        rollback_for=rollback_for,
        password=password,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Bundled InfluxDB backup / restore
# ---------------------------------------------------------------------------

def evaluate_influxdb_backup(config):
    """Decide whether bundled InfluxDB backup/restore applies to ``config``.

    Returns ``{supported, reason, mode, message}``. ``reason`` is ``None`` when
    supported, otherwise ``"disabled"`` or ``"external"`` with a user-facing
    ``message``.
    """

    influx = normalize_influxdb_config(
        config.get("influxdb") if isinstance(config, dict) else None
    )
    if not influx.get("enabled"):
        return {
            "supported": False,
            "reason": "disabled",
            "mode": influx.get("mode"),
            "message": INFLUX_DISABLED_MESSAGE,
        }
    if influx.get("mode") != "bundled":
        return {
            "supported": False,
            "reason": "external",
            "mode": influx.get("mode"),
            "message": INFLUX_EXTERNAL_MESSAGE,
        }
    return {
        "supported": True,
        "reason": None,
        "mode": "bundled",
        "message": None,
    }


def influxdb_manifest_block(influx, *, included=True):
    """Build the manifest ``influxdb`` block for a bundled InfluxDB backup."""

    return {
        "included": bool(included),
        "mode": influx.get("mode"),
        "service": influx_setup.INFLUX_SERVICE,
        "container_name": influx_setup.INFLUX_CONTAINER_NAME,
        "org": influx.get("org"),
        "bucket_prefix": influx.get("bucket_prefix"),
        "backup_method": influx_setup.INFLUX_BACKUP_METHOD,
        "data_dir": influx_setup.DEFAULT_DATA_DIR,
    }


def _stage_influxdb_files(influx_out_dir, staging):
    """Collect official backup output under ``influx_out_dir`` as archive entries."""

    included = []
    for root, _dirs, names in os.walk(influx_out_dir):
        for name in sorted(names):
            abs_path = os.path.join(root, name)
            rel = os.path.relpath(abs_path, staging).replace(os.sep, "/")
            included.append({
                "abs_path": abs_path,
                "arcname": rel,
                "kind": "influxdb_backup",
                "sensitive": True,
                "privacy_relevant": True,
            })
    included.sort(key=lambda entry: entry["arcname"])
    return included


def create_influxdb_backup(
    config,
    *,
    base_dir=None,
    backup_dir=None,
    backup_purpose="manual",
    rollback_for=None,
    password=None,
    encryption_options=None,
    compression_level=DEFAULT_COMPRESSION_LEVEL,
    now=None,
    backup_runner=None,
):
    """Create a bundled InfluxDB data backup; return the created archive path.

    ``backup_runner(influx_out_dir)`` must populate ``influx_out_dir`` with the
    output of the official ``influx backup`` command (the Docker orchestration
    lives in the caller so this stays testable). Raises :class:`BackupError`
    when InfluxDB is disabled or in external mode.
    """

    if backup_purpose not in BACKUP_PURPOSES:
        raise BackupError(f"invalid backup purpose: {backup_purpose}")
    if backup_runner is None:
        raise BackupError("InfluxDB backup requires a backup_runner")

    evaluation = evaluate_influxdb_backup(config)
    if not evaluation["supported"]:
        raise BackupError(evaluation["message"])

    base_dir = base_dir or BASE_DIR
    backup_dir = backup_dir or default_backup_dir(base_dir)
    encrypted = bool(password)
    encryption_options = encryption_options if encrypted else None
    influx = normalize_influxdb_config(config.get("influxdb"))

    os.makedirs(backup_dir, exist_ok=True)
    staging = tempfile.mkdtemp(prefix="ems-influx-backup-")
    try:
        influx_out = os.path.join(staging, INFLUX_ARCHIVE_SUBDIR)
        os.makedirs(influx_out, exist_ok=True)
        backup_runner(influx_out)

        included = _stage_influxdb_files(influx_out, staging)
        if not included:
            raise BackupError(
                "InfluxDB backup produced no output files; nothing to archive"
            )

        manifest = build_manifest(
            included,
            [],
            backup_type="influxdb",
            backup_purpose=backup_purpose,
            encrypted=encrypted,
            encryption_method=_encryption_method(encryption_options, encrypted),
            rollback_for=rollback_for,
            created_at=_utc_now_iso(now if isinstance(now, datetime) else None),
            influxdb=influxdb_manifest_block(influx),
        )

        return _emit_archive(
            "influxdb",
            backup_purpose,
            manifest,
            included,
            backup_dir,
            encrypted,
            password,
            compression_level,
            now,
            encryption_options=encryption_options,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def create_influxdb_rollback_backup(
    config, rollback_for, *, password=None, backup_runner=None, **kwargs
):
    """Create an InfluxDB rollback backup tagged for ``rollback_for``."""

    return create_influxdb_backup(
        config,
        backup_purpose="rollback",
        rollback_for=rollback_for,
        password=password,
        backup_runner=backup_runner,
        **kwargs,
    )


def extract_influxdb_payload(
    archive_path, dest_dir, *, password=None, allowed_root=None
):
    """Validate and extract the ``influxdb/`` payload of an InfluxDB backup.

    Returns ``(manifest, influx_dir)`` where ``influx_dir`` holds the official
    backup files ready to feed to ``influx restore``. Every member checksum is
    verified before anything is written.
    """

    archive_path = _validated_existing_archive_path(
        archive_path, allowed_root=allowed_root
    )
    with open_backup_archive(
        archive_path, password=password, allowed_root=allowed_root
    ) as tar:
        manifest = read_manifest(tar)
        if manifest.get("backup_type") != "influxdb":
            raise BackupError(
                "archive is not an InfluxDB backup "
                f"(backup_type={manifest.get('backup_type')!r})"
            )
        entries = []
        for file_entry in manifest["files"]:
            data = _read_member_bytes(tar, file_entry["path"])
            manifest_sha = file_entry.get("sha256")
            if manifest_sha is not None and _sha256_bytes(data) != manifest_sha:
                raise BackupError(
                    f"checksum mismatch for {file_entry['path']}; "
                    "refusing to restore"
                )
            entries.append((file_entry["path"], data))

    for arcname, data in entries:
        target = os.path.join(dest_dir, *arcname.split("/"))
        _atomic_write(target, data)

    influx_dir = os.path.join(dest_dir, INFLUX_ARCHIVE_SUBDIR)
    return manifest, influx_dir


def restore_influxdb_backup(
    archive_path,
    config,
    *,
    base_dir=None,
    password=None,
    restore_runner=None,
    dry_run=False,
    allowed_root=None,
):
    """Restore a bundled InfluxDB backup (replace strategy).

    ``restore_runner(influx_dir)`` must feed the extracted official backup
    output to ``influx restore`` inside the bundled container. Raises
    :class:`BackupError` when InfluxDB is disabled or in external mode, or when
    the archive is not an InfluxDB backup. With ``dry_run=True`` the archive is
    validated (structure + checksums) but nothing is restored.
    """

    archive_path = _validated_existing_archive_path(
        archive_path, allowed_root=allowed_root
    )
    if not dry_run and restore_runner is None:
        raise BackupError("InfluxDB restore requires a restore_runner")

    evaluation = evaluate_influxdb_backup(config)
    if not evaluation["supported"]:
        raise BackupError(evaluation["message"])

    base_dir = base_dir or BASE_DIR
    staging = tempfile.mkdtemp(prefix="ems-influx-restore-")
    try:
        manifest, influx_dir = extract_influxdb_payload(
            archive_path,
            staging,
            password=password,
            allowed_root=allowed_root,
        )
        if dry_run:
            return {
                "manifest": manifest,
                "strategy": "replace",
                "dry_run": True,
                "actions": [{"path": INFLUX_ARCHIVE_SUBDIR,
                             "action": "would_restore_influxdb"}],
            }
        restore_runner(influx_dir)
        return {"manifest": manifest, "strategy": "replace", "dry_run": False}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# Archive access / validation
# ---------------------------------------------------------------------------

def _validated_existing_archive_path(archive_path, allowed_root=None):
    """Return a canonical, existing regular-file archive path."""

    if not isinstance(archive_path, (str, os.PathLike)):
        raise BackupError("invalid backup path")
    try:
        archive_path = os.fspath(archive_path)
    except TypeError as exc:
        raise BackupError("invalid backup path") from exc
    if (
        not isinstance(archive_path, str)
        or not archive_path
        or "\x00" in archive_path
    ):
        raise BackupError("invalid backup path")

    # Reject the symlink identity before realpath() canonicalizes it away, so a
    # symlinked archive can never be followed (O_NOFOLLOW only guards the final
    # component).
    if os.path.islink(archive_path):
        raise BackupError("backup path must not be a symlink")
    archive_path = os.path.realpath(archive_path)
    if allowed_root is not None:
        if not isinstance(allowed_root, (str, os.PathLike)):
            raise BackupError("invalid allowed backup directory")
        try:
            allowed_root = os.fspath(allowed_root)
        except TypeError as exc:
            raise BackupError("invalid allowed backup directory") from exc
        if (
            not isinstance(allowed_root, str)
            or not allowed_root
            or "\x00" in allowed_root
            or os.path.islink(allowed_root)
        ):
            raise BackupError("invalid allowed backup directory")
        allowed_root = os.path.realpath(allowed_root)
        if not os.path.isdir(allowed_root):
            raise BackupError("allowed backup directory not found")
        try:
            inside_allowed_root = (
                os.path.commonpath([allowed_root, archive_path]) == allowed_root
            )
        except ValueError:
            inside_allowed_root = False
        if not inside_allowed_root:
            raise BackupError("backup path is outside allowed directory")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(archive_path, flags)
    except OSError as exc:
        raise BackupError(f"backup not found: {archive_path}") from exc
    try:
        st = os.fstat(fd)
    finally:
        os.close(fd)
    if not os.path.stat.S_ISREG(st.st_mode):
        raise BackupError(f"backup not found: {archive_path}")
    return archive_path


def is_encrypted(path, allowed_root=None):
    return backup_crypto.is_encrypted_backup(path, allowed_root=allowed_root)


@contextmanager
def open_backup_archive(archive_path, password=None, allowed_root=None):
    """Yield an open ``tarfile`` for ``archive_path``, decrypting if needed."""

    archive_path = _validated_existing_archive_path(
        archive_path, allowed_root=allowed_root
    )

    tmp_path = None
    encrypted = backup_crypto.is_encrypted_backup(
        archive_path, allowed_root=allowed_root
    )
    try:
        if encrypted:
            if not password:
                raise BackupError(
                    "backup is password protected; a password is required"
                )
            try:
                tmp_path = backup_crypto.decrypt_file_to_temp(
                    archive_path, password, allowed_root=allowed_root
                )
            except backup_crypto.BackupFormatError as exc:
                raise BackupError(f"cannot read encrypted backup: {exc}") from exc
            tar_path = tmp_path
        else:
            tar_path = archive_path

        try:
            tar = tarfile.open(tar_path, "r:gz")
        except tarfile.TarError as exc:
            raise BackupError(f"cannot read backup archive: {exc}") from exc
        try:
            yield tar
        finally:
            tar.close()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def _is_safe_member_path(name):
    if not name or name.startswith("/") or os.path.isabs(name):
        return False
    normalized = os.path.normpath(name)
    if normalized.startswith("..") or normalized.startswith("/"):
        return False
    parts = normalized.replace("\\", "/").split("/")
    return ".." not in parts


def read_manifest(tar):
    """Read and validate the manifest from an open backup tarfile."""

    try:
        member = tar.getmember(MANIFEST_NAME)
    except KeyError as exc:
        raise BackupError("backup is missing backup-manifest.json") from exc

    handle = tar.extractfile(member)
    if handle is None:
        raise BackupError("backup-manifest.json is not a regular file")

    try:
        manifest = json.loads(handle.read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise BackupError(f"invalid backup-manifest.json: {exc}") from exc

    if not isinstance(manifest, dict):
        raise BackupError("backup-manifest.json must be a JSON object")
    if "backup_format" not in manifest or "files" not in manifest:
        raise BackupError("backup-manifest.json is missing required fields")
    if not isinstance(manifest.get("files"), list):
        raise BackupError("backup-manifest.json 'files' must be a list")

    for entry in manifest["files"]:
        if not isinstance(entry, dict) or "path" not in entry:
            raise BackupError("backup-manifest.json has an invalid file entry")
        if not _is_safe_member_path(entry["path"]):
            raise BackupError(
                f"unsafe path in manifest rejected: {entry.get('path')!r}"
            )

    return manifest


def _read_member_bytes(tar, name):
    if not _is_safe_member_path(name):
        raise BackupError(f"unsafe archive path rejected: {name!r}")
    try:
        member = tar.getmember(name)
    except KeyError as exc:
        raise BackupError(f"manifest lists missing file: {name}") from exc
    if not member.isfile():
        raise BackupError(f"manifest entry is not a regular file: {name}")
    handle = tar.extractfile(member)
    if handle is None:
        raise BackupError(f"cannot read archived file: {name}")
    return handle.read()


# ---------------------------------------------------------------------------
# Inspect
# ---------------------------------------------------------------------------

def inspect_backup(archive_path, password=None, base_dir=None, allowed_root=None):
    """Return a description of a backup.

    ``{"encrypted": bool, "manifest": dict|None, "path": str}``. For an
    encrypted backup with no password, ``manifest`` is ``None``.

    Confinement to a directory only happens when the caller passes
    ``allowed_root`` explicitly (the dashboard does); ``base_dir`` is the
    restore target, not the archive-source allowlist.
    """

    archive_path = _validated_existing_archive_path(
        archive_path, allowed_root=allowed_root
    )

    encrypted = backup_crypto.is_encrypted_backup(
        archive_path, allowed_root=allowed_root
    )
    if encrypted and not password:
        return {"encrypted": True, "manifest": None, "path": archive_path}

    with open_backup_archive(
        archive_path, password=password, allowed_root=allowed_root
    ) as tar:
        manifest = read_manifest(tar)
    return {"encrypted": encrypted, "manifest": manifest, "path": archive_path}


# ---------------------------------------------------------------------------
# Restore planning / applying
# ---------------------------------------------------------------------------

def _looks_binary(data):
    if b"\x00" in data:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _build_restore_entries(tar, manifest, base_dir):
    entries = []
    for file_entry in manifest["files"]:
        arcname = file_entry["path"]
        data = _read_member_bytes(tar, arcname)
        actual_sha = _sha256_bytes(data)
        manifest_sha = file_entry.get("sha256")
        checksum_ok = manifest_sha is None or actual_sha == manifest_sha

        target = os.path.join(base_dir, *arcname.split("/"))
        if os.path.isfile(target):
            current_sha = _sha256_file(target)
            status = "identical" if current_sha == actual_sha else "conflict"
        else:
            status = "new"

        entries.append({
            "path": arcname,
            "kind": file_entry.get("kind"),
            "sensitive": bool(file_entry.get("sensitive")),
            "target": target,
            "status": status,
            "checksum_ok": checksum_ok,
            "manifest_sha256": manifest_sha,
            "actual_sha256": actual_sha,
            "is_text": not _looks_binary(data),
            "_data": data,
        })
    return entries


def plan_restore(
    archive_path, base_dir=None, password=None, allowed_root=None
):
    """Return restore plan entries (without file payloads) for display."""

    archive_path = _validated_existing_archive_path(
        archive_path, allowed_root=allowed_root
    )
    base_dir = base_dir or BASE_DIR
    with open_backup_archive(
        archive_path, password=password, allowed_root=allowed_root
    ) as tar:
        manifest = read_manifest(tar)
        entries = _build_restore_entries(tar, manifest, base_dir)

    plan = []
    for entry in entries:
        public = {key: value for key, value in entry.items() if key != "_data"}
        plan.append(public)
    return {"manifest": manifest, "files": plan}


def _atomic_write(target, data):
    parent = os.path.dirname(target) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".restore-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _dry_run_action(entry):
    status = entry["status"]
    if status == "identical":
        action = "would_skip_identical"
    elif status == "new":
        action = "would_restore_new"
    else:
        action = "would_replace_conflict"
    return {"path": entry["path"], "action": action, "status": status}


def restore_backup(
    archive_path,
    base_dir=None,
    *,
    password=None,
    on_conflict="abort",
    conflict_resolver=None,
    dry_run=False,
    allowed_root=None,
):
    """Restore files from a backup archive.

    ``on_conflict`` is one of ``abort`` / ``keep`` / ``replace`` and is used
    when ``conflict_resolver`` is ``None``. ``conflict_resolver(entry)`` may
    return ``keep`` / ``replace`` / ``abort`` for interactive callers.

    Returns a result dict with per-file ``actions``.
    """

    archive_path = _validated_existing_archive_path(
        archive_path, allowed_root=allowed_root
    )
    if on_conflict not in ("abort", "keep", "replace"):
        raise BackupError(f"invalid on_conflict policy: {on_conflict}")

    base_dir = base_dir or BASE_DIR
    actions = []

    with open_backup_archive(
        archive_path, password=password, allowed_root=allowed_root
    ) as tar:
        manifest = read_manifest(tar)
        entries = _build_restore_entries(tar, manifest, base_dir)

        # Validate every checksum before writing anything.
        for entry in entries:
            if not entry["checksum_ok"]:
                raise BackupError(
                    f"checksum mismatch for {entry['path']}; refusing to restore"
                )

        # Dry-run reports a plan only: it never resolves conflicts, never
        # aborts on a differing file, and never writes anything.
        if dry_run:
            for entry in entries:
                actions.append(_dry_run_action(entry))
            return {"manifest": manifest, "actions": actions, "dry_run": True}

        for entry in entries:
            status = entry["status"]
            if status == "identical":
                actions.append({"path": entry["path"], "action": "skip_identical"})
                continue

            if status == "conflict":
                if conflict_resolver is not None:
                    decision = conflict_resolver(entry)
                else:
                    decision = on_conflict
                if decision not in ("abort", "keep", "replace"):
                    raise BackupError(
                        f"invalid conflict resolver decision: {decision!r}"
                    )
                if decision == "abort":
                    raise BackupError(
                        f"restore aborted at conflicting file: {entry['path']}"
                    )
                if decision == "keep":
                    actions.append({"path": entry["path"], "action": "kept_current"})
                    continue
                # decision == "replace" falls through to write.

            _atomic_write(entry["target"], entry["_data"])
            actions.append({
                "path": entry["path"],
                "action": "restored",
                "status": status,
            })

    return {"manifest": manifest, "actions": actions, "dry_run": dry_run}


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

def diff_backup_file(
    archive_path,
    filename,
    base_dir=None,
    password=None,
    allowed_root=None,
):
    """Return a unified diff between the current file and its backup version.

    Returns ``{"binary": bool, "text": str, "path": str}``. For binary files
    ``binary`` is ``True`` and ``text`` holds an explanatory message.
    """

    archive_path = _validated_existing_archive_path(
        archive_path, allowed_root=allowed_root
    )
    base_dir = base_dir or BASE_DIR
    wanted = filename.replace(os.sep, "/")

    with open_backup_archive(
        archive_path, password=password, allowed_root=allowed_root
    ) as tar:
        manifest = read_manifest(tar)
        match = None
        for entry in manifest["files"]:
            if entry["path"] == wanted or os.path.basename(entry["path"]) == wanted:
                match = entry
                break
        if match is None:
            raise BackupError(f"{filename} is not present in this backup")
        backup_data = _read_member_bytes(tar, match["path"])

    target = os.path.join(base_dir, *match["path"].split("/"))
    current_data = b""
    if os.path.isfile(target):
        with open(target, "rb") as handle:
            current_data = handle.read()

    if _looks_binary(backup_data) or _looks_binary(current_data):
        return {
            "binary": True,
            "text": f"{match['path']} is a binary file; not showing a diff.",
            "path": match["path"],
        }

    current_lines = current_data.decode("utf-8").splitlines(keepends=True)
    backup_lines = backup_data.decode("utf-8").splitlines(keepends=True)
    diff = difflib.unified_diff(
        current_lines,
        backup_lines,
        fromfile=f"current/{match['path']}",
        tofile=f"backup/{match['path']}",
    )
    return {"binary": False, "text": "".join(diff), "path": match["path"]}
