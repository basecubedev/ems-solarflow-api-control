# SPDX-License-Identifier: AGPL-3.0-or-later
"""Persist one Guided Upgrade's execution context across an Admin restart.

The durable ``pending-transition.json`` binds an operation to a System Build and
a *fingerprint* of its options, but it cannot reconstruct the actual options —
so a resume after the Admin process is replaced would otherwise have to ask the
browser for them again. This store keeps the reproducible, secret-free context
(the confirmed option flags plus backup status) keyed by ``operation_id`` beside
the transition, so an automatic resume can rebuild the run without the browser.

It is fail-closed: a load only succeeds when the requested operation id and
System Build tag match AND the persisted fingerprint reproduces from the stored
options. Any manual edit (a flipped option, an unknown option, a rewritten
fingerprint) makes the fingerprint irreproducible, so the context is refused.
Only boolean option flags, the tag, a reference string and the reproducible
fingerprint are stored — never config content or credentials.
"""

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from admin.guided_upgrade import (
    ALL_OPTIONS,
    _normalized_options,
    guided_upgrade_request_fingerprint,
)

GUIDED_UPGRADE_CONTEXT_FILE = "guided-upgrade-context.json"
# v3 also binds the reviewed/completed MQTT migration across Admin replacement.
GUIDED_UPGRADE_CONTEXT_VERSION = 3


def _context_integrity(
    *,
    format_version,
    operation_id,
    target_system_tag,
    options,
    request_fingerprint,
    pre_alignment_completed,
    backup_completed,
    backup_reference,
    backup_verified,
    mqtt_migration_required,
    mqtt_migration_completed,
    mqtt_migration_revision,
) -> str:
    """Fingerprint the complete context, including its completion/backup fields.

    Unlike ``request_fingerprint`` (which binds only the tag and options to the
    paired transition), this covers every persisted decision, so flipping a
    completion or backup flag on disk makes the context irreproducible.
    """

    payload = json.dumps(
        {
            "format_version": format_version,
            "operation_id": operation_id,
            "target_system_tag": target_system_tag,
            "options": options,
            "request_fingerprint": request_fingerprint,
            "pre_alignment_completed": bool(pre_alignment_completed),
            "backup_completed": bool(backup_completed),
            "backup_reference": backup_reference,
            "backup_verified": bool(backup_verified),
            "mqtt_migration_required": bool(mqtt_migration_required),
            "mqtt_migration_completed": bool(mqtt_migration_completed),
            "mqtt_migration_revision": mqtt_migration_revision,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _context_semantics_error(
    *, options, pre_alignment_completed, backup_completed, backup_verified,
    backup_reference, mqtt_migration_required=False,
    mqtt_migration_completed=False, mqtt_migration_revision=None,
):
    """Return a reason string if the completion/backup state is inconsistent.

    Pre-alignment must have completed. When backup was enabled it must be
    completed, verified, and identify an exact archive; when it was disabled a
    verification must never be invented.
    """

    if not pre_alignment_completed:
        return "pre_alignment_completed must be true"
    if options.get("backup"):
        if not backup_completed:
            return "backup enabled but not completed"
        if not backup_verified:
            return "backup enabled but not verified"
        if not (isinstance(backup_reference, str) and backup_reference.strip()):
            return "backup enabled but no exact archive reference"
    elif backup_verified:
        return "backup disabled but verification is claimed"
    if mqtt_migration_required:
        if not mqtt_migration_completed:
            return "MQTT migration required but not completed"
        if not (
            isinstance(mqtt_migration_revision, str)
            and len(mqtt_migration_revision) == 64
        ):
            return "MQTT migration required but review revision is invalid"
    elif mqtt_migration_completed:
        return "MQTT migration not required but completion is claimed"
    return None


class GuidedUpgradeContextPersistenceError(Exception):
    """The durable Guided Upgrade context could not be saved.

    Raised so the caller fails closed — no Admin replacement may start when the
    context that a later automatic resume depends on is not durable.
    """


@dataclass(frozen=True)
class GuidedUpgradeContext:
    """The reproducible, secret-free execution context of one Guided Upgrade."""

    operation_id: str
    target_system_tag: str
    options: dict
    request_fingerprint: str
    pre_alignment_completed: bool
    backup_completed: bool
    backup_reference: str | None
    backup_verified: bool
    mqtt_migration_required: bool
    mqtt_migration_completed: bool
    mqtt_migration_revision: str | None


class GuidedUpgradeContextStore:
    """Atomic reader/writer for a single Guided Upgrade context record."""

    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / GUIDED_UPGRADE_CONTEXT_FILE
        self._lock = threading.Lock()

    @staticmethod
    def _normalize_options(options):
        return _normalized_options(options)

    def save(
        self,
        *,
        operation_id,
        target_system_tag,
        options,
        request_fingerprint,
        pre_alignment_completed=True,
        backup_completed=False,
        backup_reference=None,
        backup_verified=False,
        mqtt_migration_required=False,
        mqtt_migration_completed=False,
        mqtt_migration_revision=None,
    ) -> GuidedUpgradeContext:
        """Persist the context; refuse a fingerprint that the options can't produce."""

        if not operation_id or not isinstance(operation_id, str):
            raise ValueError("a guided upgrade operation id is required")
        if not target_system_tag or not isinstance(target_system_tag, str):
            raise ValueError("a guided upgrade target System Build tag is required")
        normalized = self._normalize_options(options)
        expected = guided_upgrade_request_fingerprint(target_system_tag, normalized)
        if request_fingerprint != expected:
            raise ValueError(
                "the request fingerprint does not match the guided upgrade options"
            )
        semantics_error = _context_semantics_error(
            options=normalized,
            pre_alignment_completed=pre_alignment_completed,
            backup_completed=backup_completed,
            backup_verified=backup_verified,
            backup_reference=backup_reference,
            mqtt_migration_required=mqtt_migration_required,
            mqtt_migration_completed=mqtt_migration_completed,
            mqtt_migration_revision=mqtt_migration_revision,
        )
        if semantics_error is not None:
            raise ValueError(f"inconsistent guided upgrade context: {semantics_error}")
        record = {
            "format_version": GUIDED_UPGRADE_CONTEXT_VERSION,
            "operation_id": operation_id,
            "target_system_tag": target_system_tag,
            "options": normalized,
            "request_fingerprint": request_fingerprint,
            "pre_alignment_completed": bool(pre_alignment_completed),
            "backup_completed": bool(backup_completed),
            "backup_reference": backup_reference,
            "backup_verified": bool(backup_verified),
            "mqtt_migration_required": bool(mqtt_migration_required),
            "mqtt_migration_completed": bool(mqtt_migration_completed),
            "mqtt_migration_revision": mqtt_migration_revision,
        }
        record["integrity"] = _context_integrity(**record)
        payload = json.dumps(record, indent=2, sort_keys=True).encode("utf-8")
        with self._lock:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=".guided-upgrade-context.", suffix=".tmp", dir=self.state_dir
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
        return self._to_context(record)

    def load(self, *, operation_id, target_system_tag) -> GuidedUpgradeContext | None:
        """Return the bound context, or ``None`` (fail-closed) if it cannot be trusted."""

        try:
            raw = self.path.read_bytes()
        except (FileNotFoundError, OSError):
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("format_version") != GUIDED_UPGRADE_CONTEXT_VERSION:
            return None
        if data.get("operation_id") != operation_id:
            return None
        if data.get("target_system_tag") != target_system_tag:
            return None
        options = data.get("options")
        if not isinstance(options, dict) or set(options) != set(ALL_OPTIONS):
            return None
        if not all(isinstance(value, bool) for value in options.values()):
            return None
        normalized = {key: bool(options[key]) for key in ALL_OPTIONS}
        expected = guided_upgrade_request_fingerprint(target_system_tag, normalized)
        if data.get("request_fingerprint") != expected:
            return None
        pre_alignment_completed = data.get("pre_alignment_completed")
        backup_completed = data.get("backup_completed")
        backup_verified = data.get("backup_verified")
        backup_reference = data.get("backup_reference")
        mqtt_migration_required = data.get("mqtt_migration_required")
        mqtt_migration_completed = data.get("mqtt_migration_completed")
        mqtt_migration_revision = data.get("mqtt_migration_revision")
        if not all(
            isinstance(value, bool)
            for value in (pre_alignment_completed, backup_completed, backup_verified)
        ):
            return None
        if not all(
            isinstance(value, bool)
            for value in (mqtt_migration_required, mqtt_migration_completed)
        ):
            return None
        if backup_reference is not None and not isinstance(backup_reference, str):
            return None
        if mqtt_migration_revision is not None and not isinstance(
            mqtt_migration_revision, str
        ):
            return None
        expected_integrity = _context_integrity(
            format_version=GUIDED_UPGRADE_CONTEXT_VERSION,
            operation_id=operation_id,
            target_system_tag=target_system_tag,
            options=normalized,
            request_fingerprint=data["request_fingerprint"],
            pre_alignment_completed=pre_alignment_completed,
            backup_completed=backup_completed,
            backup_reference=backup_reference,
            backup_verified=backup_verified,
            mqtt_migration_required=mqtt_migration_required,
            mqtt_migration_completed=mqtt_migration_completed,
            mqtt_migration_revision=mqtt_migration_revision,
        )
        if data.get("integrity") != expected_integrity:
            return None
        if _context_semantics_error(
            options=normalized,
            pre_alignment_completed=pre_alignment_completed,
            backup_completed=backup_completed,
            backup_verified=backup_verified,
            backup_reference=backup_reference,
            mqtt_migration_required=mqtt_migration_required,
            mqtt_migration_completed=mqtt_migration_completed,
            mqtt_migration_revision=mqtt_migration_revision,
        ) is not None:
            return None
        return self._to_context({**data, "options": normalized})

    def describe(self) -> dict:
        """Report the stored context's identity and its domain validity apart.

        Two different facts, and conflating them is what let an unusable context
        be treated as an ordinary orphan: *identity-readable* means the file
        names an operation and a target, *domain-valid* means :meth:`load` — the
        one validation authority — accepts it for that exact pair. Only the
        identity fields are returned; they carry no secrets.
        """

        absent = {
            "present": False,
            "identity_readable": True,
            "domain_valid": True,
            "operation_id": None,
            "target_system_tag": None,
            "reason": None,
        }

        def unusable(reason):
            return {
                "present": True,
                "identity_readable": False,
                "domain_valid": False,
                "operation_id": None,
                "target_system_tag": None,
                "reason": reason,
            }

        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return absent
        except OSError:
            return unusable("unreadable_context_file")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return unusable("corrupt_context_file")
        if not isinstance(data, dict):
            return unusable("corrupt_context_file")
        operation_id = data.get("operation_id")
        target_system_tag = data.get("target_system_tag")
        if not (isinstance(operation_id, str) and operation_id):
            return unusable("missing_context_identity")
        if not (isinstance(target_system_tag, str) and target_system_tag):
            return unusable("missing_context_identity")
        # The authoritative loader decides usability; nothing is re-validated
        # here, so the two answers can never drift apart.
        domain_valid = (
            self.load(
                operation_id=operation_id, target_system_tag=target_system_tag
            )
            is not None
        )
        return {
            "present": True,
            "identity_readable": True,
            "domain_valid": domain_valid,
            "operation_id": operation_id,
            "target_system_tag": target_system_tag,
            "reason": None if domain_valid else "unreproducible_context",
        }

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def clear_for_operation(self, operation_id) -> bool:
        """Remove the stored context only when it belongs to ``operation_id``.

        Terminal lifecycle events (Cancel upgrade, completed upgrade) are bound
        to their operation, so an old cancellation can never clear a newer
        upgrade's context. An unreadable file is left untouched — fail closed
        rather than guess ownership. Returns whether the context was removed.
        """

        if not operation_id or not isinstance(operation_id, str):
            return False
        with self._lock:
            try:
                raw = self.path.read_bytes()
            except (FileNotFoundError, OSError):
                return False
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return False
            if not isinstance(data, dict) or data.get("operation_id") != operation_id:
                return False
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return True

    @staticmethod
    def _to_context(record) -> GuidedUpgradeContext:
        return GuidedUpgradeContext(
            operation_id=record["operation_id"],
            target_system_tag=record["target_system_tag"],
            options=dict(record["options"]),
            request_fingerprint=record["request_fingerprint"],
            pre_alignment_completed=bool(record.get("pre_alignment_completed")),
            backup_completed=bool(record.get("backup_completed")),
            backup_reference=record.get("backup_reference"),
            backup_verified=bool(record.get("backup_verified")),
            mqtt_migration_required=bool(record.get("mqtt_migration_required")),
            mqtt_migration_completed=bool(record.get("mqtt_migration_completed")),
            mqtt_migration_revision=record.get("mqtt_migration_revision"),
        )
