# SPDX-License-Identifier: AGPL-3.0-or-later
"""Validated download and safe generated-config output for Admin Setup."""

import json
import os
import tempfile
import threading
from pathlib import Path

from admin.models import utc_now_iso


class ConfigExportValidationError(Exception):
    def __init__(self, preview):
        super().__init__("Generated config validation failed.")
        self.preview = preview


class ConfigExportService:
    def __init__(self, preview_generator, admin_data_dir):
        self.preview_generator = preview_generator
        self.target_path = Path(admin_data_dir) / "generated" / "config.json"
        self._write_lock = threading.Lock()

    def status(self):
        target = self.target_path
        return {"path": str(target), "exists": target.is_file()}

    def serialize(self, draft, supported_grid_meter_count=None):
        preview = self.preview_generator.generate(draft, supported_grid_meter_count)
        if not preview["ready"]:
            raise ConfigExportValidationError(preview)
        payload = json.dumps(
            preview["config"],
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
        return payload, preview

    def write(self, draft, supported_grid_meter_count=None, overwrite=False):
        payload, preview = self.serialize(draft, supported_grid_meter_count)
        target = self.target_path
        with self._write_lock:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(
                prefix=".config.", suffix=".tmp", dir=target.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                if overwrite:
                    os.replace(temporary, target)
                else:
                    try:
                        os.link(temporary, target)
                    except FileExistsError:
                        return {
                            "ok": False,
                            "reason": "target_exists",
                            "path": str(target),
                            "message": (
                                "A generated config already exists. "
                                "Confirm overwrite to replace it."
                            ),
                        }
                self._fsync_directory(target.parent)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        return {
            "ok": True,
            "path": str(target),
            "written_at": utc_now_iso(),
            "release": preview["release"],
        }

    @staticmethod
    def _fsync_directory(directory):
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
