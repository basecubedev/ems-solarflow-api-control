# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dashboard maintenance service layer for backup, restore and config upgrade."""
import hashlib
import json
import os
import re
from dataclasses import dataclass

from ems import backup as backup_mod
from ems import config as config_mod
from ems.diagnostics import diagnose_redact_key, diagnose_redact_text


BACKUP_TYPES = ("config", "databases", "influxdb")
REDACTED = "<redacted>"

# Only archives produced by the backup module are ever listed/inspected.
_STRICT_ARCHIVE_NAME_RE = re.compile(
    r"\Aems-[A-Za-z0-9._-]+\.tar\.gz(?:\.enc)?\Z"
)
_ARCHIVE_SUFFIXES = (".tar.gz.enc", ".tar.gz")


class MaintenanceError(Exception):
    """User-facing maintenance failure carrying an HTTP-ish status hint."""

    def __init__(self, message, *, status=400, code="maintenance_error"):
        super().__init__(message)
        self.status = status
        self.code = code


def _relpath(path, base_dir):
    if not path:
        return None
    try:
        return os.path.relpath(path, base_dir)
    except ValueError:
        return path


def load_config(config_path):
    if not (config_path and os.path.exists(config_path)):
        raise MaintenanceError(
            "config file not found", status=500, code="config_unavailable"
        )
    try:
        with open(config_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise MaintenanceError(
            f"could not read config: {exc}", status=500, code="config_unreadable"
        ) from exc
    if not isinstance(data, dict):
        raise MaintenanceError(
            "config must be a JSON object", status=500, code="config_unreadable"
        )
    return data


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def maintenance_status(config_path, base_dir):
    config = load_config(config_path)
    evaluation = backup_mod.evaluate_influxdb_backup(config)
    influx = config_mod.normalize_influxdb_config(config.get("influxdb"))
    return {
        "config_path": _relpath(config_path, base_dir),
        "backup_dir": _relpath(backup_mod.default_backup_dir(base_dir), base_dir),
        "backup_types": list(BACKUP_TYPES),
        "influxdb": {
            "enabled": bool(influx.get("enabled")),
            "mode": influx.get("mode"),
            "backup_supported": bool(evaluation["supported"]),
            "restore_supported": bool(evaluation["supported"]),
        },
        "restore_available_in_dashboard": True,
    }


# ---------------------------------------------------------------------------
# Backup listing / inspection
# ---------------------------------------------------------------------------

def _is_backup_name(name):
    return bool(
        isinstance(name, str)
        and _STRICT_ARCHIVE_NAME_RE.fullmatch(name)
    )


def _strip_suffix(name):
    for suffix in _ARCHIVE_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _parse_backup_name(name):
    """Best-effort ``backup_type``/``backup_purpose`` from the file name."""
    stem = _strip_suffix(name)
    parts = stem.split("-")
    backup_type = parts[1] if len(parts) > 1 else None
    backup_purpose = parts[2] if len(parts) > 2 else None
    return backup_type, backup_purpose


@dataclass(frozen=True)
class BackupArchiveRef:
    """A validated, filesystem-resolved reference to a backup archive.

    ``path`` is canonicalized from a directory-scan entry (not from the raw
    request name), so it is safe to hand to the core backup helpers.
    """

    name: str
    path: str
    stat: os.stat_result


def _validate_backup_file_name(file_name):
    """Validate a request-supplied backup name and return its bare basename."""
    if not isinstance(file_name, str) or not file_name:
        raise MaintenanceError("invalid backup file name", code="invalid_backup_name")
    safe_name = os.path.basename(file_name)
    if (
        safe_name != file_name
        or "/" in file_name
        or "\\" in file_name
        or ".." in file_name
        or os.path.isabs(file_name)
        or not _STRICT_ARCHIVE_NAME_RE.fullmatch(file_name)
    ):
        raise MaintenanceError("invalid backup file name", code="invalid_backup_name")
    return safe_name


def _confined_inside(backup_dir, path):
    try:
        if os.path.commonpath([backup_dir, path]) != backup_dir:
            return False
    except ValueError:
        return False
    return os.path.dirname(path) == backup_dir


def _safe_backup_path(base_dir, file_name):
    """Resolve ``file_name`` to a path strictly inside the backup directory.

    Rejects symlinks even when they resolve back into the backup directory.
    """
    safe_name = _validate_backup_file_name(file_name)
    backup_dir = os.path.realpath(backup_mod.default_backup_dir(base_dir))
    # backup_dir is already canonical and safe_name is a single validated
    # component, so once the final component is proven not to be a symlink the
    # joined path is canonical; realpath() here would only re-join needlessly.
    joined = os.path.join(backup_dir, safe_name)
    if os.path.islink(joined):
        raise MaintenanceError("invalid backup file name", code="invalid_backup_name")
    candidate = os.path.normpath(joined)
    if not _confined_inside(backup_dir, candidate):
        raise MaintenanceError("invalid backup file name", code="invalid_backup_name")
    return candidate


def _ref_from_entry(backup_dir, entry):
    """Build a :class:`BackupArchiveRef` from a scandir entry, or ``None``.

    The archive path is derived from the scan entry, not from request input,
    and symlinks are rejected so a planted link cannot escape the directory.
    """
    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
        return None
    stat = entry.stat(follow_symlinks=False)
    path = os.path.realpath(entry.path)
    if not _confined_inside(backup_dir, path):
        return None
    return BackupArchiveRef(name=entry.name, path=path, stat=stat)


def _find_backup_archive(base_dir, file_name):
    """Locate a validated backup archive by scanning the backup directory.

    Matching by scan-entry name keeps the resolved path off the request-tainted
    flow, so the core backup helpers never open a request-derived path.
    """
    safe_name = _validate_backup_file_name(file_name)
    backup_dir = os.path.realpath(backup_mod.default_backup_dir(base_dir))
    try:
        scan = os.scandir(backup_dir)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise MaintenanceError(
            "backup not found", status=404, code="backup_not_found"
        ) from exc
    with scan:
        for entry in scan:
            if entry.name != safe_name:
                continue
            ref = _ref_from_entry(backup_dir, entry)
            if ref is None:
                raise MaintenanceError(
                    "invalid backup file name", code="invalid_backup_name"
                )
            return ref
    raise MaintenanceError("backup not found", status=404, code="backup_not_found")


def _manifest_summary(manifest):
    if not isinstance(manifest, dict):
        return None
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    files = []
    for entry in manifest.get("files", []) or []:
        if not isinstance(entry, dict):
            continue
        summary = {
            "path": entry.get("path"),
            "kind": entry.get("kind"),
            "sensitive": bool(entry.get("sensitive")),
        }
        if entry.get("privacy_relevant"):
            summary["privacy_relevant"] = True
        files.append(summary)
    encryption = manifest.get("encryption") or {}
    return {
        "backup_type": manifest.get("backup_type"),
        "backup_purpose": manifest.get("backup_purpose"),
        "backup_format": manifest.get("backup_format"),
        "created_at": manifest.get("created_at"),
        "ems_version": source.get("ems_version"),
        "git_commit_short": source.get("git_commit_short"),
        "git_branch": source.get("git_branch"),
        "encrypted": bool(encryption.get("enabled")),
        "encryption_method": encryption.get("method") if encryption.get("enabled") else None,
        "rollback_for": manifest.get("rollback_for"),
        "skipped_count": len(manifest.get("skipped", []) or []),
        "files": files,
    }


def _backup_item(backup_dir, ref):
    from datetime import datetime, timezone

    stat = ref.stat
    encrypted = ref.name.endswith(".enc")
    backup_type, backup_purpose = _parse_backup_name(ref.name)
    item = {
        "name": ref.name,
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z"),
        "encrypted": encrypted,
        "manifest_available": False,
        "backup_type": backup_type,
        "backup_purpose": backup_purpose,
        "ems_version": None,
    }
    if encrypted:
        # Encrypted backups need a password we never hold here.
        return item
    try:
        inspected = backup_mod.inspect_backup(ref.path, allowed_root=backup_dir)
        manifest = inspected.get("manifest")
    except backup_mod.BackupError:
        return item
    if isinstance(manifest, dict):
        item["manifest_available"] = True
        item["backup_type"] = manifest.get("backup_type") or backup_type
        item["backup_purpose"] = manifest.get("backup_purpose") or backup_purpose
        source = manifest.get("source") or {}
        item["ems_version"] = source.get("ems_version")
    return item


def list_backups(base_dir):
    backup_dir = os.path.realpath(backup_mod.default_backup_dir(base_dir))
    if not os.path.isdir(backup_dir):
        return {"items": []}
    items = []
    try:
        scan = os.scandir(backup_dir)
    except OSError:
        return {"items": []}
    with scan:
        for entry in scan:
            if not _is_backup_name(entry.name):
                continue
            try:
                ref = _ref_from_entry(backup_dir, entry)
                if ref is None:
                    continue
                items.append(_backup_item(backup_dir, ref))
            except (MaintenanceError, OSError):
                continue
    items.sort(key=lambda entry: entry["modified_at"], reverse=True)
    return {"items": items}


def inspect_backup(base_dir, file_name, password=None):
    ref = _find_backup_archive(base_dir, file_name)
    backup_dir = os.path.dirname(ref.path)
    encrypted = backup_mod.is_encrypted(ref.path, allowed_root=backup_dir)
    if encrypted and not password:
        return {
            "name": file_name,
            "encrypted": True,
            "manifest_available": False,
        }
    try:
        inspected = backup_mod.inspect_backup(
            ref.path, password=password, allowed_root=backup_dir
        )
    except backup_mod.BackupError as exc:
        raise MaintenanceError(
            f"could not inspect backup: {exc}", code="backup_unreadable"
        ) from exc
    summary = _manifest_summary(inspected.get("manifest"))
    return {
        "name": file_name,
        "encrypted": encrypted,
        "manifest_available": summary is not None,
        "manifest": summary,
    }


# ---------------------------------------------------------------------------
# Backup creation
# ---------------------------------------------------------------------------

def _created_response(path, backup_type, base_dir):
    name = os.path.basename(path)
    return {
        "created": True,
        "backup": {
            "name": name,
            "path": _relpath(path, base_dir),
            "backup_type": backup_type,
        },
    }


def create_backup(
    backup_type,
    config,
    *,
    base_dir,
    config_path=None,
    runtime_state_path=None,
    dashboard_auth_path=None,
):
    if backup_type not in BACKUP_TYPES:
        raise MaintenanceError(
            f"unknown backup type: {backup_type}", code="unknown_backup_type"
        )

    if backup_type == "config":
        path = backup_mod.create_config_backup(
            config,
            base_dir=base_dir,
            config_path=config_path,
            runtime_state_path=runtime_state_path,
            dashboard_auth_path=dashboard_auth_path,
            backup_purpose="manual",
        )
        return _created_response(path, "config", base_dir)

    if backup_type == "databases":
        path = backup_mod.create_database_backup(
            config,
            base_dir=base_dir,
            backup_purpose="manual",
        )
        return _created_response(path, "databases", base_dir)

    return _create_influxdb_backup(config, base_dir=base_dir)


def _create_influxdb_backup(config, *, base_dir):
    evaluation = backup_mod.evaluate_influxdb_backup(config)
    if not evaluation["supported"]:
        return {
            "created": False,
            "reason": f"influxdb_{evaluation['reason']}",
            "message": evaluation["message"],
        }
    try:
        # Reuse the CLI's bundled InfluxDB backup runner (Docker/CLI orchestration).
        # emsctl is import-side-effect-free, so a lazy import keeps the server lean.
        import emsctl

        runner = emsctl.make_influx_backup_runner(config, json_output=True)
        path = backup_mod.create_influxdb_backup(
            config,
            base_dir=base_dir,
            backup_purpose="manual",
            backup_runner=runner,
        )
    except backup_mod.BackupError as exc:
        raise MaintenanceError(
            f"InfluxDB backup failed: {exc}", status=500, code="influxdb_backup_failed"
        ) from exc
    return _created_response(path, "influxdb", base_dir)


# ---------------------------------------------------------------------------
# Restore (wraps the same ems.backup core the CLI uses)
# ---------------------------------------------------------------------------

def _restore_archive_path(path, base_dir):
    if not path:
        return None
    absolute = path if os.path.isabs(path) else os.path.join(base_dir, path)
    relative = os.path.relpath(absolute, base_dir)
    if relative.startswith("..") or os.path.isabs(relative):
        relative = os.path.basename(absolute)
    return relative.replace(os.sep, "/")


def _config_restore_paths(
    config,
    base_dir,
    *,
    config_path=None,
    runtime_state_path=None,
    dashboard_auth_path=None,
):
    system = config.get("system") if isinstance(config.get("system"), dict) else {}
    dashboard = (
        config.get("dashboard") if isinstance(config.get("dashboard"), dict) else {}
    )
    influx = config_mod.normalize_influxdb_config(config.get("influxdb"))
    paths = {
        _restore_archive_path(config_path or "config.json", base_dir),
        _restore_archive_path(
            runtime_state_path
            or system.get("runtime_state_path", backup_mod.DEFAULT_RUNTIME_STATE_PATH),
            base_dir,
        ),
        _restore_archive_path(
            dashboard_auth_path
            or dashboard.get("auth_file", backup_mod.DEFAULT_DASHBOARD_AUTH_PATH),
            base_dir,
        ),
        _restore_archive_path(
            dashboard.get("ssl_cert_file", backup_mod.DEFAULT_DASHBOARD_CERT_PATH),
            base_dir,
        ),
        _restore_archive_path(
            dashboard.get("ssl_key_file", backup_mod.DEFAULT_DASHBOARD_KEY_PATH),
            base_dir,
        ),
    }
    if influx.get("enabled") and influx.get("mode") == "bundled":
        paths.add(
            _restore_archive_path(
                influx.get("secret_file", backup_mod.DEFAULT_INFLUX_SECRET_PATH),
                base_dir,
            )
        )
    return paths - {None}


def _database_restore_paths(config, base_dir):
    dashboard = (
        config.get("dashboard") if isinstance(config.get("dashboard"), dict) else {}
    )
    assist = (
        config.get("battery_full_charge_assist")
        if isinstance(config.get("battery_full_charge_assist"), dict)
        else {}
    )
    return {
        _restore_archive_path(
            dashboard.get("database_path", backup_mod.DEFAULT_DASHBOARD_DB_PATH),
            base_dir,
        ),
        _restore_archive_path(
            assist.get("state_database_path", backup_mod.DEFAULT_STATE_DB_PATH),
            base_dir,
        ),
    }


def _validate_restore_manifest(
    manifest,
    config,
    base_dir,
    *,
    config_path=None,
    runtime_state_path=None,
    dashboard_auth_path=None,
):
    backup_type = manifest.get("backup_type")
    files = manifest.get("files", [])
    if backup_type not in BACKUP_TYPES:
        raise MaintenanceError(
            f"unknown backup type: {backup_type}", code="unknown_backup_type"
        )

    if backup_type == "config":
        allowed = _config_restore_paths(
            config,
            base_dir,
            config_path=config_path,
            runtime_state_path=runtime_state_path,
            dashboard_auth_path=dashboard_auth_path,
        )
        valid = all(entry.get("path") in allowed for entry in files)
    elif backup_type == "databases":
        allowed = _database_restore_paths(config, base_dir)
        valid = all(entry.get("path") in allowed for entry in files)
    elif backup_type == "influxdb":
        valid = all(
            isinstance(entry.get("path"), str)
            and entry["path"].startswith(f"{backup_mod.INFLUX_ARCHIVE_SUBDIR}/")
            for entry in files
        )

    if not valid:
        raise MaintenanceError(
            "backup contains a path that cannot be restored from the dashboard",
            code="unsupported_restore_path",
        )


def _resolve_restore_target(
    base_dir,
    file_name,
    password,
    config,
    *,
    config_path=None,
    runtime_state_path=None,
    dashboard_auth_path=None,
):
    """Validate the file and resolve its backup type from the manifest.

    Returns ``(ref, backup_type)``. Raises ``MaintenanceError`` when the file
    is missing, encrypted without a password, or unreadable.
    """
    ref = _find_backup_archive(base_dir, file_name)
    backup_dir = os.path.dirname(ref.path)
    if backup_mod.is_encrypted(ref.path, allowed_root=backup_dir) and not password:
        raise MaintenanceError(
            "password required for encrypted backup",
            code="password_required",
        )
    try:
        inspected = backup_mod.inspect_backup(
            ref.path, password=password, allowed_root=backup_dir
        )
    except backup_mod.BackupError as exc:
        raise MaintenanceError(
            f"could not read backup: {exc}", code="backup_unreadable"
        ) from exc
    manifest = inspected.get("manifest")
    if not isinstance(manifest, dict) or not manifest.get("backup_type"):
        raise MaintenanceError(
            "backup manifest unavailable", code="manifest_unavailable"
        )
    _validate_restore_manifest(
        manifest,
        config,
        base_dir,
        config_path=config_path,
        runtime_state_path=runtime_state_path,
        dashboard_auth_path=dashboard_auth_path,
    )
    return ref, manifest["backup_type"]


def _sanitize_actions(actions):
    out = []
    for action in actions or []:
        if not isinstance(action, dict):
            continue
        out.append(
            {
                "path": action.get("path"),
                "action": action.get("action"),
                "status": action.get("status"),
            }
        )
    return out


_RESTORE_WARNINGS = {
    "config": [
        "Config restore changes files on disk. Restart EMS for restored "
        "settings to take effect.",
        "Config restore may replace dashboard auth files; you may need to "
        "re-login after restart.",
    ],
    "databases": [
        "Database restore replaces local dashboard/history data immediately. "
        "Restart EMS if the dashboard still shows cached values.",
    ],
    "influxdb": [
        "InfluxDB restore replaces bundled analytics data (replace-style "
        "influx restore --full).",
    ],
}


def restore_plan(
    base_dir,
    file_name,
    password,
    config,
    *,
    config_path=None,
    runtime_state_path=None,
    dashboard_auth_path=None,
):
    ref, backup_type = _resolve_restore_target(
        base_dir,
        file_name,
        password,
        config,
        config_path=config_path,
        runtime_state_path=runtime_state_path,
        dashboard_auth_path=dashboard_auth_path,
    )
    backup_dir = os.path.dirname(ref.path)
    encrypted = backup_mod.is_encrypted(ref.path, allowed_root=backup_dir)
    if backup_type == "influxdb":
        try:
            result = backup_mod.restore_influxdb_backup(
                ref.path,
                config,
                base_dir=base_dir,
                password=password,
                dry_run=True,
                allowed_root=backup_dir,
            )
        except backup_mod.BackupError as exc:
            raise MaintenanceError(
                f"InfluxDB restore unavailable: {exc}",
                code="influxdb_restore_unsupported",
            ) from exc
        actions = result.get("actions", [])
    else:
        try:
            result = backup_mod.restore_backup(
                ref.path,
                base_dir=base_dir,
                password=password,
                dry_run=True,
                allowed_root=backup_dir,
            )
        except backup_mod.BackupError as exc:
            raise MaintenanceError(
                f"restore preview failed: {exc}", code="restore_plan_failed"
            ) from exc
        actions = result.get("actions", [])
    return {
        "file": file_name,
        "backup_type": backup_type,
        "encrypted": encrypted,
        "actions": _sanitize_actions(actions),
        "requires_restart": backup_type == "config",
        "requires_relogin": backup_type == "config",
        "warnings": list(_RESTORE_WARNINGS.get(backup_type, [])),
    }


def restore(
    base_dir,
    file_name,
    password,
    config,
    *,
    confirm_preview,
    confirm_restore,
    confirm_replace,
    config_path=None,
    runtime_state_path=None,
    dashboard_auth_path=None,
    store=None,
):
    if not (confirm_preview and confirm_restore and confirm_replace):
        raise MaintenanceError(
            "restore requires preview, restore and replace confirmation",
            code="confirmation_required",
        )

    ref, backup_type = _resolve_restore_target(
        base_dir,
        file_name,
        password,
        config,
        config_path=config_path,
        runtime_state_path=runtime_state_path,
        dashboard_auth_path=dashboard_auth_path,
    )

    if backup_type == "influxdb":
        return _restore_influxdb(ref, file_name, password, config, base_dir)

    if backup_type not in ("config", "databases"):
        raise MaintenanceError(
            f"unknown backup type: {backup_type}", code="unknown_backup_type"
        )

    # Rollback first: if it fails, the restore must never start.
    try:
        if backup_type == "databases":
            rollback_path = backup_mod.create_database_rollback_backup(
                config, file_name, base_dir=base_dir
            )
        else:
            rollback_path = backup_mod.create_rollback_backup(
                config,
                file_name,
                base_dir=base_dir,
                config_path=config_path,
                runtime_state_path=runtime_state_path,
                dashboard_auth_path=dashboard_auth_path,
            )
    except backup_mod.BackupError as exc:
        raise MaintenanceError(
            f"rollback backup failed; restore not started: {exc}",
            status=500,
            code="rollback_failed",
        ) from exc

    try:
        backup_dir = os.path.dirname(ref.path)
        if backup_type == "databases" and store is not None and hasattr(
            store, "maintenance_pause"
        ):
            with store.maintenance_pause():
                result = backup_mod.restore_backup(
                    ref.path,
                    base_dir=base_dir,
                    password=password,
                    on_conflict="replace",
                    allowed_root=backup_dir,
                )
        else:
            result = backup_mod.restore_backup(
                ref.path,
                base_dir=base_dir,
                password=password,
                on_conflict="replace",
                allowed_root=backup_dir,
            )
    except backup_mod.BackupError as exc:
        raise MaintenanceError(
            f"restore failed: {exc}", status=500, code="restore_failed"
        ) from exc

    requires_restart = backup_type == "config"
    requires_relogin = backup_type == "config"
    if backup_type == "config":
        message = (
            "Restore completed. Restart EMS for restored settings to take "
            "effect; you may need to re-login."
        )
    else:
        message = (
            "Restore completed. Restart EMS if the dashboard still shows "
            "cached values."
        )
    return {
        "restored": True,
        "backup_type": backup_type,
        "rollback_backup": _relpath(rollback_path, base_dir),
        "actions": _sanitize_actions(result.get("actions")),
        "requires_restart": requires_restart,
        "requires_relogin": requires_relogin,
        "message": message,
    }


def _restore_influxdb(ref, file_name, password, config, base_dir):
    evaluation = backup_mod.evaluate_influxdb_backup(config)
    if not evaluation["supported"]:
        raise MaintenanceError(
            evaluation["message"],
            code=f"influxdb_{evaluation['reason']}",
        )
    try:
        import emsctl

        backup_runner = emsctl.make_influx_backup_runner(config, json_output=True)
        rollback_path = backup_mod.create_influxdb_rollback_backup(
            config, file_name, base_dir=base_dir, backup_runner=backup_runner
        )
    except backup_mod.BackupError as exc:
        raise MaintenanceError(
            f"InfluxDB rollback backup failed; restore not started: {exc}",
            status=500,
            code="rollback_failed",
        ) from exc

    try:
        restore_runner = emsctl.make_influx_restore_runner(config, json_output=True)
        backup_dir = os.path.dirname(ref.path)
        result = backup_mod.restore_influxdb_backup(
            ref.path,
            config,
            base_dir=base_dir,
            password=password,
            restore_runner=restore_runner,
            allowed_root=backup_dir,
        )
    except backup_mod.BackupError as exc:
        raise MaintenanceError(
            f"InfluxDB restore failed: {exc}", status=500, code="restore_failed"
        ) from exc

    return {
        "restored": True,
        "backup_type": "influxdb",
        "rollback_backup": _relpath(rollback_path, base_dir),
        "actions": _sanitize_actions(result.get("actions")),
        "requires_restart": False,
        "requires_relogin": False,
        "message": "InfluxDB restore completed (bundled analytics data replaced).",
    }


# ---------------------------------------------------------------------------
# Config upgrade
# ---------------------------------------------------------------------------

_URL_CRED_RE = re.compile(r"https?://[^/\s:@]+:[^@\s/]+@", re.IGNORECASE)


def _redact_upgrade_value(path, value):
    last_segment = str(path).rsplit(".", 1)[-1]
    if diagnose_redact_key(last_segment):
        return REDACTED
    if isinstance(value, str):
        if _URL_CRED_RE.search(value):
            return diagnose_redact_text(value)
        if diagnose_redact_key(value):
            return REDACTED
    return value


def _upgrade_items(plan):
    items = []
    for entry in plan.get("add", []) or []:
        items.append(
            {
                "kind": "add",
                "path": entry["path"],
                "value": _redact_upgrade_value(entry["path"], entry.get("value")),
            }
        )
    for change in plan.get("migrate", []) or []:
        items.append(
            {
                "kind": "migrate",
                "path": change.get("path"),
                "old_value": _redact_upgrade_value(
                    change.get("path"), change.get("old_value")
                ),
                "value": _redact_upgrade_value(
                    change.get("path"), change.get("value")
                ),
            }
        )
    for entry in plan.get("comment_add", []) or []:
        items.append(
            {
                "kind": "comment_add",
                "path": entry.get("path"),
            }
        )
    for entry in plan.get("comment_refresh", []) or []:
        items.append(
            {
                "kind": "comment_refresh",
                "path": entry.get("path"),
            }
        )
    return items


def _build_plan(config, base_dir, config_path):
    plan = config_mod.build_config_upgrade_plan(config, base_dir)
    try:
        with open(config_path, encoding="utf-8") as handle:
            current_text = handle.read()
    except OSError:
        current_text = None
    current_rendered = config_mod.render_config_json(
        config, plan.get("template_layout")
    )
    upgraded_rendered = config_mod.render_config_json(
        plan["upgraded_config"], plan.get("template_layout")
    )
    format_changed = current_text is not None and current_text != current_rendered
    if format_changed:
        plan["changed"] = True
    plan_id_source = current_text
    if plan_id_source is None:
        plan_id_source = json.dumps(config, sort_keys=True, separators=(",", ":"))
    refresh_source = json.dumps(
        plan.get("comment_refresh", []),
        sort_keys=True,
        separators=(",", ":"),
    )
    fingerprint = "\0".join((plan_id_source, upgraded_rendered, refresh_source))
    plan_id = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return plan, format_changed, plan_id


def config_upgrade_plan(config, base_dir, config_path):
    try:
        plan, format_changed, plan_id = _build_plan(config, base_dir, config_path)
    except config_mod.ConfigUpgradeError as exc:
        raise MaintenanceError(
            f"config upgrade unavailable: {exc}",
            status=500,
            code="config_upgrade_unavailable",
        ) from exc
    template_path = plan.get("template_path")
    return {
        "changed": bool(plan.get("changed")),
        "format_changed": format_changed,
        "add_count": len(plan.get("add", []) or []),
        "comment_add_count": len(plan.get("comment_add", []) or []),
        "comment_refresh_count": len(plan.get("comment_refresh", []) or []),
        "migration_count": len(plan.get("migrate", []) or []),
        "template": os.path.basename(template_path) if template_path else None,
        "items": _upgrade_items(plan),
        "plan_id": plan_id,
        "apply_available": bool(
            plan.get("changed") or plan.get("comment_refresh")
        ),
        "requires_restart": True,
    }


def apply_config_upgrade(
    config,
    *,
    base_dir,
    config_path,
    runtime_state_path=None,
    dashboard_auth_path=None,
    confirm_apply=False,
    refresh_comments=True,
    expected_plan_id=None,
):
    """Apply the previewed upgrade after creating a mandatory backup."""
    if not confirm_apply:
        raise MaintenanceError(
            "config upgrade apply requires confirmation",
            code="confirmation_required",
        )
    try:
        plan, format_changed, plan_id = _build_plan(config, base_dir, config_path)
    except config_mod.ConfigUpgradeError as exc:
        raise MaintenanceError(
            f"config upgrade unavailable: {exc}",
            status=500,
            code="config_upgrade_unavailable",
        ) from exc

    if not expected_plan_id or expected_plan_id != plan_id:
        raise MaintenanceError(
            "config changed since preview; check the upgrade plan again",
            status=409,
            code="config_upgrade_plan_changed",
        )

    final_config = plan["upgraded_config"]
    refreshed_items = []
    if refresh_comments:
        final_config, refreshed_items = config_mod.refresh_template_comments(
            final_config,
            base_dir,
        )
    rendered = config_mod.render_config_json(
        final_config,
        plan.get("template_layout"),
    )
    try:
        with open(config_path, encoding="utf-8") as handle:
            current_text = handle.read()
    except OSError:
        current_text = None

    if current_text == rendered:
        return {
            "changed": False,
            "requires_restart": False,
            "requires_relogin": False,
            "message": "Config is already up to date.",
        }

    try:
        backup_path = backup_mod.create_config_backup(
            config,
            base_dir=base_dir,
            config_path=config_path,
            runtime_state_path=runtime_state_path,
            dashboard_auth_path=dashboard_auth_path,
            backup_purpose="manual",
        )
    except backup_mod.BackupError as exc:
        raise MaintenanceError(
            f"backup failed; config.json not changed: {exc}",
            status=500,
            code="backup_failed",
        ) from exc

    config_mod.write_config_json_atomic(
        config_path,
        final_config,
        layout=plan.get("template_layout"),
    )

    applied = {
        "keys_added": len(plan.get("add", []) or []),
        "values_migrated": len(plan.get("migrate", []) or []),
        "comments_added": len(plan.get("comment_add", []) or []),
        "comments_refreshed": len(refreshed_items),
        "format_changed": bool(format_changed),
    }
    return {
        "changed": True,
        "backup": _relpath(backup_path, base_dir),
        "backup_name": os.path.basename(backup_path),
        "requires_restart": True,
        "requires_relogin": False,
        "applied": applied,
        "applied_count": (
            applied["keys_added"]
            + applied["values_migrated"]
            + applied["comments_added"]
            + applied["comments_refreshed"]
            + int(applied["format_changed"])
        ),
        "message": "Config upgraded. Restart EMS for changed settings to take effect.",
    }
