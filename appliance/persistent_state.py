# SPDX-License-Identifier: AGPL-3.0-or-later
"""What formats the state on the persistent partition is written in.

Every other schema number is stamped into an image and compared against a
constant compiled into that same image, so it can only agree with itself. This
record lives outside ``/persistent/shared`` and ``/persistent/slots``, where
nothing re-seeds it, and is therefore the one operand that does not travel with
the slot.

It names every format independently: the shared-path count is not the only thing
a step back can cross. Values only rise, and an unknown axis is carried through,
so an older manager cannot erase a newer one's claim.
"""

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from appliance import (
    ab_bootstrap,
    ab_layout,
    ab_persistence,
    ab_state,
    backup_ownership,
    manager_retention,
    operation_schema,
)

STAMP_SCHEMA_VERSION = 1
READABLE_STAMP_VERSIONS = (1,)

# Deliberately a sibling of upstream's own directories rather than a child of
# any of them: /persistent/shared is re-seeded from the slot on every boot,
# /persistent/slots is per-slot by definition, and /persistent/common belongs to
# upstream's machine-id handling.
STAMP_DIRECTORY = "ems-appliance"
STAMP_NAME = "state-schema.json"

# Every versioned format the appliance keeps on a shared path, read from the
# owning module rather than restated. A schema that moves without an axis moving
# with it is a failing test, not a silent gap.


def implemented_schemas():
    """What the running Appliance Manager implements, by axis.

    ``os_update`` is imported here rather than at module scope: it is a consumer
    of this module, and the update planner has to be able to ask what this
    partition holds without the two importing each other.
    """

    from appliance.os_update import CONFIRMED_AUTHORITY_SCHEMA_VERSION

    return {
        "persistent_paths": ab_persistence.PERSISTENT_SCHEMA_VERSION,
        "slot_layout": ab_layout.LAYOUT_SCHEMA_VERSION,
        "ab_state": ab_state.AB_STATE_SCHEMA_VERSION,
        "runtime_record": ab_bootstrap.RECORD_VERSION,
        "backup_ownership": backup_ownership.RECORD_SCHEMA_VERSION,
        "backup_account_origin": backup_ownership.ACCOUNT_ORIGIN_SCHEMA_VERSION,
        "backup_acl_manifest": backup_ownership.ACL_MANIFEST_SCHEMA_VERSION,
        "backup_home_marker": backup_ownership.HOME_MARKER_SCHEMA_VERSION,
        "operations": operation_schema.OPERATION_SCHEMA_VERSION,
        "operation_authority": operation_schema.AUTHORITY_SCHEMA_VERSION,
        "operation_recovery": operation_schema.RECOVERY_SCHEMA_VERSION,
        "confirmed_authority": CONFIRMED_AUTHORITY_SCHEMA_VERSION,
        "manager_retention": manager_retention.RECORD_SCHEMA_VERSION,
    }


def readable_floors():
    """The oldest schema this manager can still read, by axis.

    Most formats are read with strict equality, so their floor is what they
    implement. Two carry a window and say so themselves: the runtime record
    (``READABLE_RECORD_VERSIONS``) and the backup-ownership record (its legacy
    version). Restating either here would let the window and the declaration
    drift apart.
    """

    floors = implemented_schemas()

    # Two record formats carry a real window and declare it themselves.
    # Restating either here would let the window and the declaration drift.
    floors["runtime_record"] = min(ab_bootstrap.READABLE_RECORD_VERSIONS)
    floors["backup_ownership"] = backup_ownership.LEGACY_RECORD_SCHEMA_VERSION

    # Two axes are structural rather than a stored format, and newer code copes
    # with every older value by construction: a larger set of shared paths
    # subsumes a smaller one (the missing directory is created), and the layout
    # descriptor is re-seeded from the running slot at every boot, so a manager
    # always meets its own. Declaring these as strictly as a record format would
    # refuse the forward update that ships the very schema bump -- which is the
    # defect the one-directional gate had, reintroduced from the other side.
    floors["persistent_paths"] = 1
    floors["slot_layout"] = 1
    floors["manager_retention"] = min(manager_retention.READABLE_RECORD_VERSIONS)

    # Everything else is read with strict equality by the module that owns it.
    # The floor is what it implements, and that is deliberate: a release whose
    # manager cannot read the pending-trial record this appliance would write
    # could never complete its own trial, so refusing it at plan time is the
    # honest answer. Bumping one of those schemas means adding a read window
    # first, not lowering this.
    return floors


STATE_ADOPTED = "adopted"
STATE_MATCHED = "matched"
STATE_RAISED = "raised"
STATE_BEHIND = "behind"
STATE_UNREADABLE = "unreadable"

FILE_MODE = 0o644
DIRECTORY_MODE = 0o755


class PersistentStateError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class StateStamp:
    """The record as it stands on the partition."""

    present: bool
    schemas: dict = field(default_factory=dict)
    stamp_schema_version: int = STAMP_SCHEMA_VERSION
    written_by: dict = field(default_factory=dict)
    written_at: str = ""
    unreadable: str = ""

    def to_dict(self):
        return {
            "present": self.present,
            "schemas": dict(self.schemas),
            "stamp_schema_version": self.stamp_schema_version,
            "written_by": dict(self.written_by),
            "written_at": self.written_at,
            "unreadable": self.unreadable,
        }


@dataclass(frozen=True)
class StampReconciliation:
    """What the running manager found, and what it did about it."""

    outcome: str
    implemented: dict = field(default_factory=dict)
    recorded: dict = field(default_factory=dict)
    behind: tuple = ()
    detail: str = ""

    @property
    def compatible(self):
        """Whether this manager may write state in this partition's formats.

        ``behind`` is the only incompatible outcome: at least one axis on the
        partition was written by a manager newer than this one, so this code
        cannot claim to understand what is there.
        """

        return self.outcome != STATE_BEHIND

    def to_dict(self):
        return {
            "outcome": self.outcome,
            "compatible": self.compatible,
            "implemented": dict(self.implemented),
            "recorded": dict(self.recorded),
            "behind": list(self.behind),
            "detail": self.detail,
        }


def stamp_path(mountpoint):
    return Path(mountpoint) / STAMP_DIRECTORY / STAMP_NAME


def resolve(root, mountpoint):
    """The partition's directory as this process can reach it.

    ``root`` is "/" on an appliance and a fixture tree in a test, exactly as
    every other probe-backed reader in this package treats it.
    """

    return Path(root or "/") / str(mountpoint).lstrip("/")


def _schema_number(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def read_stamp(mountpoint):
    """The record, or an honest account of why there is none."""

    target = stamp_path(mountpoint)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return StateStamp(present=False)
    except (OSError, ValueError) as exc:
        return StateStamp(present=True, unreadable=str(exc)[:200])
    if not isinstance(payload, dict):
        return StateStamp(present=True, unreadable="the record is not an object")

    envelope = payload.get("stamp_schema_version")
    if envelope not in READABLE_STAMP_VERSIONS:
        return StateStamp(
            present=True,
            unreadable=f"state schema record version {envelope!r} cannot be read by this manager",
        )
    schemas = payload.get("schemas")
    if not isinstance(schemas, dict) or not schemas:
        return StateStamp(present=True, unreadable="the record names no schemas")
    bad = sorted(name for name, value in schemas.items() if not _schema_number(value))
    if bad:
        return StateStamp(
            present=True,
            unreadable=f"{', '.join(bad)} is not a schema number",
        )
    written_by = payload.get("written_by")
    return StateStamp(
        present=True,
        schemas={str(name): int(value) for name, value in schemas.items()},
        stamp_schema_version=envelope,
        written_by=written_by if isinstance(written_by, dict) else {},
        written_at=str(payload.get("written_at") or ""),
    )


def write_stamp(mountpoint, *, schemas, written_by=None, written_at=""):
    """Claim this partition's state formats, atomically."""

    target = stamp_path(mountpoint)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(target.parent, DIRECTORY_MODE)
    except OSError as exc:
        raise PersistentStateError(
            "persistent_state_not_writable", f"{target.parent} could not be created: {exc}"
        ) from exc

    payload = json.dumps(
        {
            "stamp_schema_version": STAMP_SCHEMA_VERSION,
            "schemas": {str(name): int(value) for name, value in sorted(schemas.items())},
            "written_by": dict(written_by or {}),
            "written_at": written_at,
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
        raise PersistentStateError(
            "persistent_state_not_writable", f"{target} could not be written: {exc}"
        ) from exc
    return read_stamp(mountpoint)


def compare(stamp, *, implemented=None):
    """What the record means for a manager implementing ``implemented``."""

    implemented = dict(implemented if implemented is not None else implemented_schemas())
    if stamp.unreadable:
        return StampReconciliation(
            outcome=STATE_UNREADABLE,
            implemented=implemented,
            detail=stamp.unreadable,
        )
    if not stamp.present:
        return StampReconciliation(
            outcome=STATE_ADOPTED,
            implemented=implemented,
            detail="this partition carried no state schema record",
        )

    recorded = dict(stamp.schemas)
    behind = tuple(
        sorted(name for name, value in recorded.items() if value > implemented.get(name, 0))
    )
    if behind:
        named = ", ".join(f"{name} {recorded[name]}>{implemented.get(name, 0)}" for name in behind)
        return StampReconciliation(
            outcome=STATE_BEHIND,
            implemented=implemented,
            recorded=recorded,
            behind=behind,
            detail=(
                f"this partition holds state written by a newer Appliance Manager ({named}); "
                "the running one cannot prove it understands that format"
            ),
        )
    raised = sorted(
        name for name, value in implemented.items() if value > recorded.get(name, 0)
    )
    if raised:
        return StampReconciliation(
            outcome=STATE_RAISED,
            implemented=implemented,
            recorded=recorded,
            detail=f"now owned at a newer schema: {', '.join(raised)}",
        )
    return StampReconciliation(
        outcome=STATE_MATCHED, implemented=implemented, recorded=recorded
    )


def merge(recorded, implemented):
    """The record after this manager claims what it may.

    Monotonic by axis, and an axis this manager does not know is carried
    through untouched — a step back must not erase what a newer one recorded.
    """

    merged = dict(recorded)
    for name, value in implemented.items():
        merged[name] = max(int(value), int(merged.get(name, 0)))
    return merged


def reconcile(mountpoint, *, implemented=None, written_by=None, written_at="", write=True):
    """Read the record, and claim the partition when this manager may."""

    implemented = dict(implemented if implemented is not None else implemented_schemas())
    stamp = read_stamp(mountpoint)
    verdict = compare(stamp, implemented=implemented)
    if not write or verdict.outcome in (STATE_MATCHED, STATE_BEHIND, STATE_UNREADABLE):
        return verdict, stamp
    stamp = write_stamp(
        mountpoint,
        schemas=merge(stamp.schemas, implemented),
        written_by=written_by,
        written_at=written_at,
    )
    return verdict, stamp
