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
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from admin.config_export import PreparedConfigChange
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
        # Reentrant: apply()/apply_maintenance() take the same lock inside an
        # open apply_transaction().
        self._write_lock = threading.RLock()

    @contextmanager
    def apply_transaction(self):
        """The one apply transaction shared by Setup and Maintenance.

        Serializes credential staging, the config write and any rollback as a
        single unit, so a parallel Apply can never interleave its staging with
        another request's commit or roll a snapshot back over another
        request's successful result.
        """

        with self._write_lock:
            yield

    def apply(
        self,
        draft,
        supported_grid_meter_count=None,
        features=None,
        zendure_mqtt_proposals=None,
        zendure_mqtt_broker=None,
        zendure_mqtt_manual_devices=None,
        *,
        prepared=None,
    ):
        """Apply a Setup config; serializes once unless a prepared change is given.

        The server passes the ``prepared`` change it already staged credentials
        against so the exact staged bytes are written; a direct caller without
        one falls back to serializing here.
        """

        change = prepared or self.config_export.prepare(
            draft,
            supported_grid_meter_count,
            features,
            zendure_mqtt_proposals,
            zendure_mqtt_broker,
            zendure_mqtt_manual_devices,
        )
        return self.apply_prepared(change)

    def capture_config_revision(self):
        """Return the target config's ``(expected_revision, expect_absent)`` now.

        Read once through the same install context the write resolves, so a
        fresh Setup apply commits against the exact filesystem state observed
        when the draft was prepared. Expected absence is a revision state in its
        own right: a config appearing before the commit is a conflict, not
        something to overwrite silently.
        """

        context = self.install_context_provider()
        target = Path(context.config_path)
        with self._write_lock:
            if target.exists():
                return hashlib.sha256(target.read_bytes()).hexdigest(), False
            return None, True

    def apply_maintenance(self, payload, expected_revision, create_backup=True):
        """Atomically apply an already validated Maintenance config payload."""

        return self.apply_prepared(
            PreparedConfigChange(
                payload=payload,
                parsed_config=None,
                preview=None,
                expected_revision=expected_revision,
            ),
            create_backup=create_backup,
        )

    def apply_prepared(self, change, *, create_backup=True):
        """Write one prepared payload atomically — the single exact-payload writer.

        Never re-serializes: the exact ``change.payload`` the caller staged
        credentials for is what is written. The current config is re-read under
        the lock and matched against the state captured when the change was
        prepared: ``change.expected_revision`` set (a config existed) requires an
        unchanged hash; ``change.expect_absent`` (no config existed) requires the
        config to still be absent. Either mismatch raises
        :class:`ConfigChangedError` before any write, so a config edited or
        created externally during staging is never overwritten; a no-op is
        reported ``changed=False`` and skips the backup/write. A legacy call with
        neither state set writes unconditionally.
        """

        context = self.install_context_provider()
        target = Path(context.config_path)
        with self._write_lock:
            existed = target.exists()
            current = target.read_bytes() if existed else b""
            if change.expect_absent:
                if existed:
                    raise ConfigChangedError(
                        "config/config.json was created while the draft was being reviewed"
                    )
                changed = True
            elif change.expected_revision is not None:
                revision = hashlib.sha256(current).hexdigest()
                if revision != change.expected_revision:
                    raise ConfigChangedError(
                        "config/config.json changed while the draft was being reviewed"
                    )
                changed = current != change.payload
            else:
                # A legacy apply with no captured filesystem state writes the
                # generated config unconditionally.
                changed = True
            backup_path = None
            if changed:
                # Back up before touching the target: a failing backup must abort
                # the apply so an existing config is never lost.
                backup_path = (
                    self._backup(target) if existed and create_backup else None
                )
                _atomic_write(target, change.payload)
        result = {
            "ok": True,
            "created": not existed,
            "changed": changed,
            "path": str(target),
            "config_source": context.config_source,
            "backup_path": str(backup_path) if backup_path else None,
            "applied_at": utc_now_iso(),
        }
        if isinstance(change.preview, dict) and "release" in change.preview:
            result["release"] = change.preview["release"]
        return result

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
