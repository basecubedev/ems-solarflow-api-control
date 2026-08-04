# SPDX-License-Identifier: AGPL-3.0-or-later
"""Persist the *known-good* system build — the installed, verified baseline.

A build moves through distinct states — selected, resolved, Admin aligned, EMS
deployed, health checked — and only becomes *known good* after Admin AND EMS are
verified and health checks pass. A merely selected or downloaded build must never
become the installed baseline automatically. This store owns only that final,
verified record; the transition/pending state is elsewhere.
"""

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from admin.models import utc_now_iso
from admin.system_build_id import validate_system_build_id

KNOWN_GOOD_FILE = "known-good-system-build.json"
KNOWN_GOOD_FORMAT_VERSION = 1


@dataclass(frozen=True)
class KnownGoodRecord:
    """The last installed build proven good by health checks.

    ``admin_image``/``admin_digest``/``admin_build_id``/``admin_revision`` record
    the *running* orchestrator Admin. For a modern paired build that is the
    selected Admin; for a legacy release it is the running modern Admin, never
    the historical Admin image that is never run. ``build_id``/``revision`` and
    ``ems_*`` identify the installed EMS System Build.
    """

    system_tag: str
    build_id: str
    revision: str
    admin_image: str
    admin_digest: str
    ems_image: str
    ems_digest: str
    aligned_at: str
    healthcheck_at: str
    admin_build_id: str | None = None
    admin_revision: str | None = None
    compatibility_mode: str | None = None
    resource_strategy: str | None = None
    format_version: int = KNOWN_GOOD_FORMAT_VERSION

    def as_dict(self) -> dict:
        return {
            "format_version": self.format_version,
            "system_tag": self.system_tag,
            "build_id": self.build_id,
            "revision": self.revision,
            "admin_image": self.admin_image,
            "admin_digest": self.admin_digest,
            "admin_build_id": self.admin_build_id or self.build_id,
            "admin_revision": self.admin_revision or self.revision,
            "ems_image": self.ems_image,
            "ems_digest": self.ems_digest,
            "compatibility_mode": self.compatibility_mode,
            "resource_strategy": self.resource_strategy,
            "aligned_at": self.aligned_at,
            "healthcheck_at": self.healthcheck_at,
        }


class KnownGoodStore:
    """Atomic reader/writer for the single known-good system-build record."""

    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / KNOWN_GOOD_FILE
        self._lock = threading.Lock()

    def current(self) -> dict | None:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            validate_system_build_id(data.get("build_id"))
        except (TypeError, ValueError):
            return None
        return data

    def record(self, system_build, *, orchestrator_admin=None,
               compatibility_mode=None, resource_strategy=None,
               aligned_at=None, healthcheck_at=None) -> dict:
        """Persist ``system_build`` as known-good; return the stored record.

        ``system_build`` is a :class:`admin.system_build.SystemBuild` (or an
        object exposing the same fields), the installed EMS build.
        ``orchestrator_admin`` is the *running* Admin identity (build_id,
        revision, image, digest); when omitted it defaults to the selected build's
        Admin — correct for a modern paired build. Timestamps default to now.
        """

        build_id = validate_system_build_id(system_build.build_id)
        admin = orchestrator_admin or {}
        admin_image = admin.get("image") or system_build.admin_image
        admin_digest = admin.get("digest") or system_build.admin_digest
        admin_build_id = admin.get("build_id") or system_build.build_id
        admin_revision = admin.get("revision") or system_build.revision
        now = utc_now_iso()
        record = KnownGoodRecord(
            system_tag=system_build.canonical_tag,
            build_id=build_id,
            revision=system_build.revision,
            admin_image=admin_image,
            admin_digest=admin_digest,
            admin_build_id=admin_build_id,
            admin_revision=admin_revision,
            ems_image=system_build.ems_image,
            ems_digest=system_build.ems_digest,
            compatibility_mode=compatibility_mode,
            resource_strategy=resource_strategy,
            aligned_at=aligned_at or now,
            healthcheck_at=healthcheck_at or now,
        )
        payload = json.dumps(record.as_dict(), indent=2, sort_keys=True).encode("utf-8")
        with self._lock:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=".known-good.", suffix=".tmp", dir=self.state_dir
            )
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.path)
            finally:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
        return record.as_dict()

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
