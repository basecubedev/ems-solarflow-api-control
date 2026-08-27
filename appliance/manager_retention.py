# SPDX-License-Identifier: AGPL-3.0-or-later
"""The package that was running, kept so a revert has a target.

dpkg keeps no copy of the archive it unpacked, so retention happens at install
time or not at all. ``current.deb`` is what runs, ``previous.deb`` what ran
before; installing rotates the first into the second.

Keyed on digest and build id, never on the version string: two commits can build
one version, and a revert picking by name could reinstall what it is leaving.
"""

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

RECORD_NAME = "retention.json"
RECORD_SCHEMA_VERSION = 2
READABLE_RECORD_VERSIONS = (1, 2)

CURRENT_NAME = "current.deb"
PREVIOUS_NAME = "previous.deb"

FILE_MODE = 0o600
DIRECTORY_MODE = 0o700


class RetentionError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RetainedPackage:
    """One archive this appliance kept, and what it is."""

    path: str = ""
    sha256: str = ""
    version: str = ""
    build_id: str = ""
    retained_at: str = ""
    architecture: str = ""
    # What this package's manager writes and the oldest it can read, copied from
    # its verified manifest. A revert has no manifest left to consult, so a
    # package that was not asked this question when it arrived cannot be asked
    # it later.
    state_implements: dict = field(default_factory=dict)
    state_reads: dict = field(default_factory=dict)

    @property
    def present(self):
        return bool(self.path) and Path(self.path).is_file()

    def to_dict(self):
        return {
            "path": self.path,
            "sha256": self.sha256,
            "version": self.version,
            "build_id": self.build_id,
            "retained_at": self.retained_at,
            "architecture": self.architecture,
            "state_schemas": {
                "implements": dict(self.state_implements),
                "reads": dict(self.state_reads),
            },
        }


@dataclass(frozen=True)
class Retention:
    """What is kept, and therefore what a revert can offer."""

    current: RetainedPackage = RetainedPackage()
    previous: RetainedPackage = RetainedPackage()
    unreadable: str = ""

    @property
    def can_revert(self):
        """A revert needs a file on disk, not merely a record of one."""

        return self.previous.present

    def to_dict(self):
        return {
            "schema_version": RECORD_SCHEMA_VERSION,
            "current": self.current.to_dict(),
            "previous": self.previous.to_dict(),
            "can_revert": self.can_revert,
            "unreadable": self.unreadable,
        }


def _record_path(paths):
    return Path(paths.packages_dir) / RECORD_NAME


def _schemas(block):
    if not isinstance(block, dict):
        return {}
    return {
        str(name): int(value)
        for name, value in block.items()
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1
    }


def _package(payload):
    if not isinstance(payload, dict):
        return RetainedPackage()
    schemas = payload.get("state_schemas")
    schemas = schemas if isinstance(schemas, dict) else {}
    return RetainedPackage(
        path=str(payload.get("path") or ""),
        sha256=str(payload.get("sha256") or ""),
        version=str(payload.get("version") or ""),
        build_id=str(payload.get("build_id") or ""),
        retained_at=str(payload.get("retained_at") or ""),
        architecture=str(payload.get("architecture") or ""),
        state_implements=_schemas(schemas.get("implements")),
        state_reads=_schemas(schemas.get("reads")),
    )


def read(paths):
    """What this appliance has kept. Absence is not an error."""

    target = _record_path(paths)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Retention()
    except (OSError, ValueError) as exc:
        return Retention(unreadable=str(exc)[:200])
    if not isinstance(payload, dict):
        return Retention(unreadable="the retention record is not an object")
    version = payload.get("schema_version")
    if version not in READABLE_RECORD_VERSIONS:
        return Retention(
            unreadable=f"retention record version {version!r} cannot be read by this manager"
        )
    return Retention(
        current=_package(payload.get("current")),
        previous=_package(payload.get("previous")),
    )


def _write_record(paths, retention):
    target = _record_path(paths)
    payload = json.dumps(
        {
            "schema_version": RECORD_SCHEMA_VERSION,
            "current": retention.current.to_dict(),
            "previous": retention.previous.to_dict(),
        },
        indent=2,
        sort_keys=True,
    )
    handle, staging = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(staging, FILE_MODE)
        os.replace(staging, target)
    except OSError as exc:
        try:
            os.unlink(staging)
        except OSError:
            pass
        raise RetentionError("retention_not_writable", f"{target} could not be written: {exc}")


def _copy(source, target):
    handle, staging = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.")
    os.close(handle)
    try:
        shutil.copyfile(source, staging)
        os.chmod(staging, FILE_MODE)
        os.replace(staging, target)
    except OSError as exc:
        try:
            os.unlink(staging)
        except OSError:
            pass
        raise RetentionError("retention_not_writable", f"{target} could not be written: {exc}")


def retain(
    paths,
    archive,
    *,
    sha256,
    version,
    build_id="",
    retained_at="",
    architecture="",
    state_implements=None,
    state_reads=None,
    rotate=True,
):
    """Keep ``archive`` as current, moving what was current to previous.

    ``rotate=False`` seeds the current entry without displacing anything -- what an
    image build does for a manager that was never installed through this path.
    """

    directory = Path(paths.packages_dir)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, DIRECTORY_MODE)
    except OSError:
        pass

    source = Path(archive)
    if not source.is_file():
        raise RetentionError("retention_source_missing", f"{source} is not a file")

    existing = read(paths)
    current_path = directory / CURRENT_NAME
    previous_path = directory / PREVIOUS_NAME

    previous = existing.previous
    if rotate and existing.current.present:
        # The archive itself moves, not just the record: a record naming a file
        # that is no longer there is exactly the shape rollback-manager has been
        # refusing on since it was written.
        _copy(current_path, previous_path)
        previous = RetainedPackage(
            path=str(previous_path),
            sha256=existing.current.sha256,
            version=existing.current.version,
            build_id=existing.current.build_id,
            retained_at=existing.current.retained_at,
            architecture=existing.current.architecture,
            state_implements=dict(existing.current.state_implements),
            state_reads=dict(existing.current.state_reads),
        )

    _copy(source, current_path)
    current = RetainedPackage(
        path=str(current_path),
        sha256=sha256,
        version=version,
        build_id=build_id,
        retained_at=retained_at,
        architecture=architecture,
        state_implements=dict(state_implements or {}),
        state_reads=dict(state_reads or {}),
    )
    retention = Retention(current=current, previous=previous)
    _write_record(paths, retention)
    return retention


def revert_target(paths):
    """The archive a revert would install, or why there is none."""

    retention = read(paths)
    if retention.unreadable:
        raise RetentionError("retention_unreadable", retention.unreadable)
    if not retention.previous.path:
        raise RetentionError(
            "no_previous_package",
            "this appliance has kept no earlier Appliance Manager package, so there "
            "is nothing to go back to",
        )
    if not retention.previous.present:
        raise RetentionError(
            "previous_package_missing",
            f"{retention.previous.path} is recorded but not on disk",
        )
    return retention.previous
