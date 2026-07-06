# SPDX-License-Identifier: AGPL-3.0-or-later
"""Apply a validated Admin setup config to the real EMS installation.

Preview and export stay non-destructive; this is the explicit step where Admin
writes the generated config to the resolved EMS config path
(``<install>/config/config.json``). The EMS config layout remains the source of
truth: the target is resolved through the shared install context, never
hardcoded, so Admin cannot introduce a second runtime layout. Setup apply always
backs up an existing config; Maintenance apply can skip that backup only after
the UI's explicit warning and confirmation.
"""

import hashlib
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from admin.install_context import detect_install_context
from admin.models import utc_now_iso


class ConfigApplyService:
    def __init__(
        self,
        config_export,
        admin_data_dir,
        install_context_provider=detect_install_context,
    ):
        self.config_export = config_export
        self.backup_dir = Path(admin_data_dir) / "backups" / "config"
        self.install_context_provider = install_context_provider
        self._write_lock = threading.Lock()

    def apply(self, draft, supported_grid_meter_count=None, features=None):
        payload, preview = self.config_export.serialize(
            draft, supported_grid_meter_count, features
        )
        context = self.install_context_provider()
        target = Path(context.config_path)
        with self._write_lock:
            existed = target.exists()
            # Back up before touching the target: a failing backup must abort the
            # apply so an existing config is never lost.
            backup_path = self._backup(target) if existed else None
            _atomic_write(target, payload)
        return {
            "ok": True,
            "created": not existed,
            "path": str(target),
            "config_source": context.config_source,
            "backup_path": str(backup_path) if backup_path else None,
            "release": preview["release"],
            "applied_at": utc_now_iso(),
        }

    def apply_maintenance(self, payload, expected_revision, create_backup=True):
        """Atomically apply an already validated Maintenance config payload."""

        context = self.install_context_provider()
        target = Path(context.config_path)
        with self._write_lock:
            current = target.read_bytes()
            revision = hashlib.sha256(current).hexdigest()
            if revision != expected_revision:
                raise ConfigChangedError(
                    "config/config.json changed while the draft was being reviewed"
                )
            # A no-op apply must report changed=False so the UI does not push a
            # container restart the user does not need. Skip the backup/write too:
            # backing up identical content only adds noise.
            changed = current != payload
            backup_path = None
            if changed:
                backup_path = self._backup(target) if create_backup else None
                _atomic_write(target, payload)
        return {
            "ok": True,
            "created": False,
            "changed": changed,
            "path": str(target),
            "config_source": context.config_source,
            "backup_path": str(backup_path) if backup_path else None,
            "applied_at": utc_now_iso(),
        }

    def _backup(self, target):
        self.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = Path(target).read_bytes()
        backup_path = self._unique_backup_path()
        _atomic_write(backup_path, data)
        return backup_path

    def _unique_backup_path(self):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        candidate = self.backup_dir / f"config-before-admin-apply-{stamp}.json"
        counter = 1
        while candidate.exists():
            candidate = (
                self.backup_dir / f"config-before-admin-apply-{stamp}-{counter}.json"
            )
            counter += 1
        return candidate


def _atomic_write(path, payload):
    path = Path(path)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=".config.", suffix=".tmp", dir=directory
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(directory):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ConfigChangedError(RuntimeError):
    pass
