# SPDX-License-Identifier: AGPL-3.0-or-later
"""Validated download and safe generated-config output for Admin Setup."""

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from admin.models import utc_now_iso


class ConfigExportValidationError(Exception):
    def __init__(self, preview):
        super().__init__("Generated config validation failed.")
        self.preview = preview


def config_payload_bytes(config):
    """The one serialization of a generated config to target bytes.

    Preview hashing and export/apply must agree byte-for-byte, so the exact
    prepared-config hash bound into a Setup preview provably matches what a
    later write/apply serializes.
    """

    return (
        json.dumps(config, indent=2, ensure_ascii=False, allow_nan=False).encode(
            "utf-8"
        )
        + b"\n"
    )


@dataclass(frozen=True)
class PreparedConfigChange:
    """The single serialized target config used by one apply transaction.

    The config is serialized exactly once into ``payload``; ``parsed_config`` is
    ``json.loads(payload)`` for credential-requirement extraction, so credential
    staging and the config write can never diverge. ``preview`` carries the
    generation metadata (release), and ``expected_revision`` the pre-read config
    hash for the revision check (``None`` when no config existed at preparation
    time). ``expect_absent`` records that no config existed when the change was
    prepared: expected absence is a revision state in its own right, so a config
    that appears before the commit is a conflict, not something to overwrite.
    """

    payload: bytes
    parsed_config: dict
    preview: dict | None = None
    expected_revision: str | None = None
    expect_absent: bool = False


class ConfigExportService:
    def __init__(self, preview_generator, admin_data_dir, target_path_provider=None):
        self.preview_generator = preview_generator
        self._default_target_path = Path(admin_data_dir) / "generated" / "config.json"
        # Resolves the generated-config target per call (the active Guided Setup
        # workflow's own directory); the legacy singleton path stays the
        # fallback so pre-workflow artifacts remain inspectable.
        self._target_path_provider = target_path_provider
        self._write_lock = threading.Lock()

    @property
    def target_path(self):
        if self._target_path_provider is not None:
            provided = self._target_path_provider()
            if provided:
                return Path(provided)
        return self._default_target_path

    def status(self):
        target = self.target_path
        return {"path": str(target), "exists": target.is_file()}

    def serialize(
        self,
        draft,
        supported_grid_meter_count=None,
        features=None,
        zendure_mqtt_proposals=None,
        zendure_mqtt_broker=None,
        zendure_mqtt_manual_devices=None,
    ):
        preview = self.preview_generator.generate(
            draft,
            supported_grid_meter_count,
            features,
            zendure_mqtt_proposals,
            zendure_mqtt_broker,
            zendure_mqtt_manual_devices,
        )
        if not preview["ready"]:
            raise ConfigExportValidationError(preview)
        return config_payload_bytes(preview["config"]), preview

    def prepare(
        self,
        draft,
        supported_grid_meter_count=None,
        features=None,
        zendure_mqtt_proposals=None,
        zendure_mqtt_broker=None,
        zendure_mqtt_manual_devices=None,
        *,
        expected_revision=None,
        expect_absent=False,
    ):
        """Serialize the target config exactly once into a prepared change.

        The one place the config builder runs for an apply/write: the returned
        payload is what credential staging is derived from and what the commit
        writes, so no re-serialization can make the staged and written configs
        diverge. ``expected_revision``/``expect_absent`` carry the filesystem
        state observed when the change was prepared so the commit can refuse to
        overwrite a config edited or created externally in the meantime.
        """

        payload, preview = self.serialize(
            draft,
            supported_grid_meter_count,
            features,
            zendure_mqtt_proposals,
            zendure_mqtt_broker,
            zendure_mqtt_manual_devices,
        )
        return PreparedConfigChange(
            payload=payload,
            parsed_config=json.loads(payload),
            preview=preview,
            expected_revision=expected_revision,
            expect_absent=expect_absent,
        )

    def write(
        self,
        draft,
        supported_grid_meter_count=None,
        overwrite=False,
        features=None,
        zendure_mqtt_proposals=None,
        zendure_mqtt_broker=None,
        zendure_mqtt_manual_devices=None,
        *,
        prepared=None,
    ):
        change = prepared or self.prepare(
            draft,
            supported_grid_meter_count,
            features,
            zendure_mqtt_proposals,
            zendure_mqtt_broker,
            zendure_mqtt_manual_devices,
        )
        return self.write_prepared(change, overwrite=overwrite)

    def write_prepared(self, change, overwrite=False):
        """Write an already-prepared payload to the generated config path.

        Never re-serializes: the exact ``change.payload`` bytes credentials were
        staged for are the bytes written.
        """

        payload = change.payload
        preview = change.preview
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
