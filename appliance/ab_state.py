# SPDX-License-Identifier: AGPL-3.0-or-later
"""The A/B state that has to survive the reboot in the middle of the update.

An A/B update is the one appliance operation that crosses a reboot, and the slot
it reboots into is a different root filesystem. So the facts both slots need —
which trial is in flight, which slot is known good, what a fallback observed —
live on the shared persistent partition rather than in either slot.

This is not a second operation database. The operation record in
``appliance/operations.py`` stays the authority for what the operator asked for
and how far it got; this store carries only what the *other* slot has to be able
to read, and every entry names the operation it belongs to.

Every write is atomic and flushed before the step it authorises, because the
whole point is to be correct across a power loss.
"""

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

AB_STATE_SCHEMA_VERSION = 1

PENDING_NAME = "pending-trial.json"
SLOTS_NAME = "slots.json"
FALLBACKS_NAME = "fallbacks.json"
STAGING_NAME = "staging"

MAX_FALLBACKS = 20
STATE_MODE = 0o600
DIR_MODE = 0o700


class AbStateError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class PendingTrial:
    """What the target slot must be able to prove about itself after the reboot."""

    operation_id: str
    source_slot: str
    target_slot: str
    target_release: str
    target_build_id: str
    artifact_digest: str
    expected_boot_partition: int
    expected_root_partuuid: str
    trial_requested_at: float
    schema_version: int = AB_STATE_SCHEMA_VERSION
    attempt: int = 1
    committed: bool = False
    kind: str = "update"
    boot_digest: str = ""
    rootfs_digest: str = ""
    release_version: str = ""
    # The canonical hash of the EMS deployment authority this trial was planned
    # against. Verified again in the target slot; never refreshed in place.
    deployment_fingerprint: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass
class SlotRecord:
    """A slot whose exact build is recorded as known good."""

    slot: str
    release_version: str = ""
    build_id: str = ""
    artifact_digest: str = ""
    boot_digest: str = ""
    rootfs_digest: str = ""
    committed_at: float = 0.0
    health: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass
class SlotHistory:
    known_good_slot: str = ""
    previous_slot: str = ""
    schema_version: int = AB_STATE_SCHEMA_VERSION
    slots: dict = field(default_factory=dict)

    def record(self, name):
        entry = self.slots.get(name)
        return SlotRecord(**entry) if entry else None

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "known_good_slot": self.known_good_slot,
            "previous_slot": self.previous_slot,
            "slots": {name: dict(entry) for name, entry in sorted(self.slots.items())},
        }


@dataclass
class FallbackRecord:
    """A trial that never committed, seen from the slot that booted instead."""

    operation_id: str
    source_slot: str
    target_slot: str
    target_release: str
    target_build_id: str
    observed_at: float
    attempt: int = 1
    known_good_slot: str = ""
    last_health: dict = field(default_factory=dict)
    acknowledged: bool = False

    def to_dict(self):
        return asdict(self)


class AbStateStore:
    """Single-writer durable state on the shared persistent partition."""

    def __init__(self, directory, *, time_fn=None):
        self.directory = Path(directory)
        self._time = time_fn or time.time

    # --- persistence -----------------------------------------------------

    def ensure(self):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            os.chmod(self.directory, DIR_MODE)
            if os.geteuid() == 0:
                os.chown(self.directory, 0, 0)
        except OSError as exc:
            raise AbStateError("ab_state_unusable", f"{self.directory} is unusable: {exc}")
        return self.directory

    @property
    def staging_dir(self):
        return self.directory / STAGING_NAME

    def _path(self, name):
        return self.directory / name

    def _read(self, name):
        try:
            return json.loads(self._path(name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise AbStateError("ab_state_corrupt", f"{name} could not be read as A/B state")

    def _write(self, name, payload):
        """Atomic and flushed. This runs before the step it authorises.

        A pending trial that reached the medium after the reboot request would
        leave the target slot unable to prove what it is, which is precisely the
        state that must become manual_action_required rather than a guess.
        """

        self.ensure()
        target = self._path(name)
        # A fixed staging name is a shared mutable path: two writers -- the
        # agent and a deliberate root CLI invocation -- would truncate each
        # other's half-written file and one of them would rename the other's
        # bytes into place. The pid makes the staging file this writer's own,
        # the way paths.atomic_write already does.
        staged = target.with_name(f".{target.name}.{os.getpid()}.staged")
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        try:
            handle = os.open(str(staged), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, STATE_MODE)
            try:
                os.write(handle, text.encode("utf-8"))
                os.fsync(handle)
            finally:
                os.close(handle)
            os.replace(staged, target)
        except OSError as exc:
            try:
                os.unlink(staged)
            except OSError:
                pass
            raise AbStateError("ab_state_write_failed", f"{name} could not be written: {exc}")
        # The rename made the new content visible; only the directory flush
        # makes the entry survive a power loss. A discarded result here is the
        # difference between "the trial is recorded" and "the trial was
        # recorded in a cache", which is the exact state this store exists for.
        if not self._sync_directory():
            raise AbStateError(
                "ab_state_write_failed",
                f"{name} was replaced but {self.directory} could not be flushed",
            )
        return target

    def _remove(self, name):
        try:
            os.unlink(self._path(name))
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise AbStateError("ab_state_write_failed", f"{name} could not be removed: {exc}")
        if not self._sync_directory():
            raise AbStateError(
                "ab_state_write_failed",
                f"{name} was removed but {self.directory} could not be flushed",
            )
        return True

    def _sync_directory(self):
        try:
            handle = os.open(str(self.directory), os.O_RDONLY)
        except OSError:
            return False
        try:
            os.fsync(handle)
        except OSError:
            return False
        finally:
            os.close(handle)
        return True

    # --- pending trial ---------------------------------------------------

    def pending(self):
        payload = self._read(PENDING_NAME)
        if payload is None:
            return None
        if payload.get("schema_version") != AB_STATE_SCHEMA_VERSION:
            raise AbStateError(
                "ab_state_unsupported",
                f"pending A/B state schema {payload.get('schema_version')!r} is not supported",
            )
        known = set(PendingTrial.__dataclass_fields__)
        return PendingTrial(**{key: value for key, value in payload.items() if key in known})

    def set_pending(self, trial):
        trial.schema_version = AB_STATE_SCHEMA_VERSION
        self._write(PENDING_NAME, trial.to_dict())
        return trial

    def clear_pending(self):
        return self._remove(PENDING_NAME)

    def mark_committed(self, operation_id):
        trial = self.pending()
        if trial is None or trial.operation_id != operation_id:
            raise AbStateError(
                "pending_trial_mismatch", "no pending trial belongs to this operation"
            )
        trial.committed = True
        return self.set_pending(trial)

    # --- slot history ----------------------------------------------------

    def slots(self):
        payload = self._read(SLOTS_NAME)
        if payload is None:
            return SlotHistory()
        if payload.get("schema_version") != AB_STATE_SCHEMA_VERSION:
            raise AbStateError(
                "ab_state_unsupported",
                f"slot history schema {payload.get('schema_version')!r} is not supported",
            )
        return SlotHistory(
            known_good_slot=str(payload.get("known_good_slot") or ""),
            previous_slot=str(payload.get("previous_slot") or ""),
            slots={
                name: dict(entry)
                for name, entry in (payload.get("slots") or {}).items()
                if isinstance(entry, dict)
            },
        )

    def record_known_good(self, record, *, previous_slot=""):
        """Promote a slot to known-good and demote the one it replaced.

        The demoted slot keeps its recorded build, because that is what makes it
        a rollback candidate rather than "the other partition".
        """

        history = self.slots()
        history.slots[record.slot] = record.to_dict()
        history.previous_slot = previous_slot or history.known_good_slot
        history.known_good_slot = record.slot
        self._write(SLOTS_NAME, history.to_dict())
        return history

    def invalidate_slot(self, name):
        """A slot that was written into is no longer the build it was.

        Called before the first destructive byte: if the write is interrupted,
        nothing may later present that slot as a known-good rollback target.
        """

        history = self.slots()
        history.slots.pop(name, None)
        if history.previous_slot == name:
            history.previous_slot = ""
        self._write(SLOTS_NAME, history.to_dict())
        return history

    # --- fallbacks -------------------------------------------------------

    def fallbacks(self):
        payload = self._read(FALLBACKS_NAME) or {}
        entries = payload.get("fallbacks") or []
        known = set(FallbackRecord.__dataclass_fields__)
        return [
            FallbackRecord(**{key: value for key, value in entry.items() if key in known})
            for entry in entries
            if isinstance(entry, dict)
        ]

    def record_fallback(self, record):
        entries = [item.to_dict() for item in self.fallbacks()]
        entries.append(record.to_dict())
        del entries[:-MAX_FALLBACKS]
        self._write(
            FALLBACKS_NAME,
            {"schema_version": AB_STATE_SCHEMA_VERSION, "fallbacks": entries},
        )
        return record

    def last_fallback(self):
        for entry in reversed(self.fallbacks()):
            if not entry.acknowledged:
                return entry
        return None

    def acknowledge_fallback(self, operation_id):
        """Stop presenting a fallback as current. It is never retried by this.

        Acknowledging is what lets an operator plan the next attempt after the
        inactive slot has been staged again; it does not re-arm anything.
        """

        entries = self.fallbacks()
        matched = False
        for entry in entries:
            if entry.operation_id == str(operation_id):
                entry.acknowledged = True
                matched = True
        if not matched:
            return False
        self._write(
            FALLBACKS_NAME,
            {
                "schema_version": AB_STATE_SCHEMA_VERSION,
                "fallbacks": [item.to_dict() for item in entries],
            },
        )
        return True

    # --- projection ------------------------------------------------------

    def summary(self):
        trial = self.pending()
        history = self.slots()
        last = self.last_fallback()
        return {
            "schema_version": AB_STATE_SCHEMA_VERSION,
            "pending_trial": trial.to_dict() if trial else None,
            "known_good_slot": history.known_good_slot,
            "previous_slot": history.previous_slot,
            "slots": history.to_dict()["slots"],
            "last_fallback": last.to_dict() if last else None,
        }
