# SPDX-License-Identifier: AGPL-3.0-or-later
"""Durable, server-owned identity and exact preview authority for Guided Setup.

One record file holds the current Guided Setup workflow: an opaque server-issued
``workflow_id``, its lifecycle status, the exact preview authority (an opaque
``preview_id`` plus the fingerprint of the full mutation input, the live-config
baseline it was reviewed against and the prepared payload hash), and where the
workflow's temporary artifacts live. Setup mutations must present the active
workflow and preview IDs; a preview for draft A can never authorize draft B, an
old tab can never mutate a newer workflow, and an Admin restart reads the same
record back.

Deliberately NOT here: transition stage and worker state (System Alignment owns
those — the record links an ``operation_id`` only as a reference), raw drafts,
passwords, API keys or tokens. Credential-affecting values contribute only a
SHA-256 digest to the fingerprint; the canonical fingerprint body is hashed
immediately and never persisted or logged.

See ``docs/technical/admin-workflow-state.md``.
"""

import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
from pathlib import Path

from admin.models import utc_now_iso

GUIDED_SETUP_WORKFLOW_FILE = "guided-setup-workflow.json"
# 2 adds the validated ``cleanup`` state. A version-1 record reads as absent
# (fail closed), so an in-progress Setup has to be started again rather than
# continuing without a cleanup-ownership state.
GUIDED_SETUP_WORKFLOW_VERSION = 2
GUIDED_SETUP_WORKFLOW_TYPE = "guided_setup"

STATUS_ACTIVE = "active"
STATUS_ABANDONED = "abandoned"
STATUS_SUPERSEDED = "superseded"
STATUS_COMPLETED = "completed"
TERMINAL_STATUSES = frozenset(
    {STATUS_ABANDONED, STATUS_SUPERSEDED, STATUS_COMPLETED}
)

CLEANUP_NOT_REQUIRED = "not_required"
CLEANUP_PENDING = "pending"
CLEANUP_COMPLETE = "complete"
CLEANUP_REVIEW_REQUIRED = "review_required"
CLEANUP_STATES = frozenset(
    {
        CLEANUP_NOT_REQUIRED,
        CLEANUP_PENDING,
        CLEANUP_COMPLETE,
        CLEANUP_REVIEW_REQUIRED,
    }
)
# Cleanup that has not converged keeps the workflow the only owner of the files
# it left behind, so no replacement workflow and no Guided Upgrade may start.
CLEANUP_BLOCKING_STATES = frozenset({CLEANUP_PENDING, CLEANUP_REVIEW_REQUIRED})

ARTIFACT_KINDS = frozenset(
    {
        "workflow_directory",
        "generated_config",
        "generated_metadata",
        "legacy_generated_config",
        "legacy_generated_metadata",
        "deployment_marker",
    }
)
ARTIFACT_REMOVED = "removed"
ARTIFACT_ABSENT = "absent"
ARTIFACT_FAILED = "failed"
ARTIFACT_REVIEW_REQUIRED = "review_required"
ARTIFACT_STATUSES = frozenset(
    {
        ARTIFACT_REMOVED,
        ARTIFACT_ABSENT,
        ARTIFACT_FAILED,
        ARTIFACT_REVIEW_REQUIRED,
    }
)

SETUP_WORKFLOW_REQUIRED = "setup_workflow_required"
SETUP_WORKFLOW_NOT_ACTIVE = "setup_workflow_not_active"
SETUP_PREVIEW_REQUIRED = "setup_preview_required"
SETUP_PREVIEW_MISMATCH = "setup_preview_mismatch"
SETUP_CLEANUP_REQUIRED = "setup_cleanup_required"

WORKFLOW_REQUIRED_MESSAGE = (
    "Start Guided Setup before changing setup state."
)
WORKFLOW_NOT_ACTIVE_MESSAGE = (
    "This browser tab belongs to an older setup session and can no longer "
    "change the current workflow."
)
CLEANUP_REQUIRED_MESSAGE = (
    "Guided Setup has stopped, but temporary setup files remain. Retry the "
    "cleanup before starting a new setup or upgrade."
)
LEGACY_REVIEW_REQUIRED_MESSAGE = (
    "Guided Setup has stopped and left files it cannot prove it owns. Review "
    "them before starting a new setup or upgrade."
)
PREVIEW_REQUIRED_MESSAGE = (
    "Review the current configuration before saving or applying this setup."
)
PREVIEW_MISMATCH_MESSAGE = (
    "This setup changed after the displayed preview was created. Review the "
    "current configuration again before saving or applying it."
)

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ALLOWED_TRANSITION_MODES = frozenset({"fresh_install", "automated_setup"})

MAX_RECORD_BYTES = 64 * 1024

# Keys whose values are (or may carry) secrets. They contribute presence and a
# value digest to the fingerprint, never the raw value.
_SECRET_KEY_MARKERS = ("password", "token", "secret", "api_key", "apikey")


class GuidedSetupWorkflowError(Exception):
    """A Setup mutation presented no, a stale, or a foreign workflow identity."""

    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _is_secret_key(key):
    lowered = key.lower()
    return any(marker in lowered for marker in _SECRET_KEY_MARKERS)


def _canonical(value):
    if isinstance(value, dict):
        canonical = {}
        for key, item in value.items():
            key = str(key)
            if (
                _is_secret_key(key)
                and isinstance(item, str)
                and item
            ):
                canonical[key] = (
                    "digest:" + hashlib.sha256(item.encode("utf-8")).hexdigest()
                )
            else:
                canonical[key] = _canonical(item)
        return canonical
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def setup_mutation_fingerprint(
    *,
    draft,
    supported_grid_meter_count,
    features,
    zendure_mqtt_proposals,
    zendure_mqtt_broker,
    zendure_mqtt_manual_devices,
):
    """Deterministic digest of every input that can change the generated config
    bytes or the credential-staging decisions.

    Called with the *resolved* trusted proposal set (never raw browser proposal
    content), so a discovery-state change between preview and mutation changes
    the fingerprint and forces a re-review. Transport-only fields (``overwrite``,
    the workflow/preview IDs, the legacy ``config_revision``) stay out.
    """

    body = {
        "fingerprint_version": 1,
        "draft": _canonical(draft),
        "supported_grid_meter_count": supported_grid_meter_count,
        "features": _canonical(features),
        "zendure_mqtt_proposals": _canonical(zendure_mqtt_proposals),
        "zendure_mqtt_broker": _canonical(zendure_mqtt_broker),
        "zendure_mqtt_manual_devices": _canonical(zendure_mqtt_manual_devices),
    }
    encoded = json.dumps(
        body, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _valid_base_revision(value):
    if not isinstance(value, dict) or set(value) != {
        "expected_revision",
        "expect_absent",
    }:
        return False
    revision = value["expected_revision"]
    absent = value["expect_absent"]
    if not isinstance(absent, bool):
        return False
    if revision is not None and not (
        isinstance(revision, str) and _SHA256_RE.match(revision)
    ):
        return False
    return absent == (revision is None)


def _valid_preview(preview):
    if preview is None:
        return True
    if not isinstance(preview, dict) or set(preview) != {
        "preview_id",
        "draft_fingerprint",
        "base_config_revision",
        "prepared_config_sha256",
        "issued_at",
    }:
        return False
    if not (
        isinstance(preview["preview_id"], str)
        and _ID_RE.match(preview["preview_id"])
    ):
        return False
    if not (
        isinstance(preview["draft_fingerprint"], str)
        and _FINGERPRINT_RE.match(preview["draft_fingerprint"])
    ):
        return False
    if not _valid_base_revision(preview["base_config_revision"]):
        return False
    prepared = preview["prepared_config_sha256"]
    if prepared is not None and not (
        isinstance(prepared, str) and _SHA256_RE.match(prepared)
    ):
        return False
    return isinstance(preview["issued_at"], str)


def _valid_artifact_path(value, workflow_id):
    """Only relative paths inside this workflow's own directory or the known
    legacy singleton locations may be claimed as owned artifacts."""

    if value is None:
        return True
    if not isinstance(value, str) or not value:
        return False
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        return False
    allowed_roots = (
        ("workflows", "guided-setup", workflow_id),
        ("generated",),
        ("state",),
    )
    return any(path.parts[: len(root)] == root for root in allowed_roots)


def _valid_cleanup(cleanup):
    """Validate the durable cleanup state fail-closed.

    Deliberately narrow: an explicit state, a count and per-artifact
    ``{kind, status}`` pairs only. Absolute paths and raw OS errors stay out of
    the record — they belong in the server log, not in a browser-facing view.
    """

    if not isinstance(cleanup, dict) or set(cleanup) != {
        "state",
        "attempted_at",
        "failed_count",
        "review_count",
        "artifacts",
    }:
        return False
    if cleanup["state"] not in CLEANUP_STATES:
        return False
    attempted = cleanup["attempted_at"]
    if attempted is not None and not isinstance(attempted, str):
        return False
    for key in ("failed_count", "review_count"):
        count = cleanup[key]
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return False
    artifacts = cleanup["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) > 32:
        return False
    for entry in artifacts:
        if not isinstance(entry, dict) or set(entry) != {"kind", "status"}:
            return False
        if entry["kind"] not in ARTIFACT_KINDS:
            return False
        if entry["status"] not in ARTIFACT_STATUSES:
            return False
    return True


def empty_cleanup_state():
    return {
        "state": CLEANUP_NOT_REQUIRED,
        "attempted_at": None,
        "failed_count": 0,
        "review_count": 0,
        "artifacts": [],
    }


def cleanup_state(record):
    """The cleanup state of a (possibly missing) workflow record."""

    if not isinstance(record, dict):
        return CLEANUP_NOT_REQUIRED
    cleanup = record.get("cleanup")
    if not isinstance(cleanup, dict):
        return CLEANUP_NOT_REQUIRED
    state = cleanup.get("state")
    return state if state in CLEANUP_STATES else CLEANUP_NOT_REQUIRED


def cleanup_blocks(record):
    """True while ``record``'s unfinished cleanup owns files on disk."""

    return cleanup_state(record) in CLEANUP_BLOCKING_STATES


def cleanup_conflict_error(record):
    """The fail-closed conflict a blocked follow-up action must return."""

    state = cleanup_state(record)
    message = (
        LEGACY_REVIEW_REQUIRED_MESSAGE
        if state == CLEANUP_REVIEW_REQUIRED
        else CLEANUP_REQUIRED_MESSAGE
    )
    return GuidedSetupWorkflowError(SETUP_CLEANUP_REQUIRED, message)


class GuidedSetupWorkflowStore:
    """Atomic reader/writer for the single Guided Setup workflow record.

    Reads fail closed: a corrupt, oversized or foreign-shaped record reads as
    ``None`` so no mutation authority can be derived from untrusted state.
    """

    def __init__(self, admin_data_dir, *, id_factory=None):
        base = Path(admin_data_dir)
        self.admin_data_dir = base
        self.path = base / "state" / GUIDED_SETUP_WORKFLOW_FILE
        self.workflows_root = base / "workflows" / "guided-setup"
        self._lock = threading.RLock()
        self._id_factory = id_factory or (lambda: secrets.token_urlsafe(32))

    # --- paths ------------------------------------------------------------

    def workflow_dir(self, workflow_id):
        if not (isinstance(workflow_id, str) and _ID_RE.match(workflow_id)):
            raise ValueError("invalid setup workflow id")
        return self.workflows_root / workflow_id

    def generated_config_path(self, workflow_id):
        return self.workflow_dir(workflow_id) / "generated" / "config.json"

    def active_generated_config_path(self):
        """The active workflow's generated-config target, else ``None``."""

        record = self.active()
        if record is None:
            return None
        return self.generated_config_path(record["workflow_id"])

    # --- reading ----------------------------------------------------------

    def load(self):
        try:
            raw = self.path.read_bytes()
        except OSError:
            return None
        if len(raw) > MAX_RECORD_BYTES:
            return None
        try:
            record = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return record if self._valid_record(record) else None

    def active(self):
        record = self.load()
        if record is None or record["status"] != STATUS_ACTIVE:
            return None
        return record

    def require_active(self, workflow_id):
        """Resolve ``workflow_id`` to the active record, or raise fail-closed."""

        if not isinstance(workflow_id, str) or not workflow_id:
            raise GuidedSetupWorkflowError(
                SETUP_WORKFLOW_REQUIRED, WORKFLOW_REQUIRED_MESSAGE
            )
        record = self.active()
        if record is None or record["workflow_id"] != workflow_id:
            raise GuidedSetupWorkflowError(
                SETUP_WORKFLOW_NOT_ACTIVE, WORKFLOW_NOT_ACTIVE_MESSAGE
            )
        return record

    def verify_preview_authority(self, record, *, preview_id, draft_fingerprint):
        """Check a mutation's exact preview authority against ``record``.

        Raises with a stable code before any staging: no stored preview or no
        submitted preview ID → ``setup_preview_required``; a submitted ID or a
        recomputed fingerprint that differs from the stored preview →
        ``setup_preview_mismatch``. Live-baseline staleness stays with the
        caller — it needs the serialized apply transaction's config read.
        """

        preview = record.get("preview")
        if preview is None or not isinstance(preview_id, str) or not preview_id:
            raise GuidedSetupWorkflowError(
                SETUP_PREVIEW_REQUIRED, PREVIEW_REQUIRED_MESSAGE
            )
        if preview_id != preview["preview_id"]:
            raise GuidedSetupWorkflowError(
                SETUP_PREVIEW_MISMATCH, PREVIEW_MISMATCH_MESSAGE
            )
        if draft_fingerprint != preview["draft_fingerprint"]:
            raise GuidedSetupWorkflowError(
                SETUP_PREVIEW_MISMATCH, PREVIEW_MISMATCH_MESSAGE
            )
        return preview

    def redacted_view(self):
        """Compact workflow state for authenticated UI diagnostics.

        Contains identifiers, lifecycle and artifact existence only — no
        fingerprints, no secrets, no absolute private paths.
        """

        record = self.load()
        if record is None:
            return None
        preview = record.get("preview")
        return {
            "workflow_id": record["workflow_id"],
            "status": record["status"],
            "transition_mode": record.get("transition_mode"),
            "operation_id": record.get("operation_id"),
            "selected_system_tag": record.get("selected_system_tag"),
            "preview": (
                {
                    "preview_id": preview["preview_id"],
                    "issued_at": preview["issued_at"],
                    "base_config_revision": dict(
                        preview["base_config_revision"]
                    ),
                }
                if preview
                else None
            ),
            # Existence only — the artifact paths themselves stay server-side.
            "artifacts": {
                key: bool((record.get("artifacts") or {}).get(key))
                for key in (
                    "generated_config",
                    "generated_metadata",
                    "deployment_marker",
                )
            },
            # Kinds, statuses and counts only: enough for the recovery UI to
            # name the right action, never a server path or an OS error.
            "cleanup": {
                "state": cleanup_state(record),
                "attempted_at": (record.get("cleanup") or {}).get("attempted_at"),
                "failed_count": (record.get("cleanup") or {}).get("failed_count", 0),
                "review_count": (record.get("cleanup") or {}).get("review_count", 0),
                "artifacts": [
                    {"kind": entry["kind"], "status": entry["status"]}
                    for entry in (record.get("cleanup") or {}).get("artifacts", [])
                ],
                "blocking": cleanup_blocks(record),
            },
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
        }

    # --- mutations ----------------------------------------------------------

    def ensure_active(self, *, transition_mode=None, selected_system_tag=None):
        """Return the active workflow, creating a fresh one when none is active.

        Refuses to replace a terminal workflow whose cleanup has not converged:
        that record is the only ownership proof for the files it left behind, so
        overwriting it would orphan them. The caller must retry cleanup with the
        same workflow ID first.
        """

        with self._lock:
            record = self.load()
            if record is not None and record["status"] == STATUS_ACTIVE:
                return record
            if cleanup_blocks(record):
                raise cleanup_conflict_error(record)
            return self._persist(
                self._new_record(
                    transition_mode=transition_mode,
                    selected_system_tag=selected_system_tag,
                )
            )

    def start_replacement(self, *, selected_system_tag=None):
        """Create a fresh active workflow, replacing a converged terminal one."""

        with self._lock:
            record = self.load()
            if cleanup_blocks(record):
                raise cleanup_conflict_error(record)
            return self._persist(
                self._new_record(selected_system_tag=selected_system_tag)
            )

    def record_preview(
        self,
        workflow_id,
        *,
        draft_fingerprint,
        base_config_revision,
        prepared_config_sha256,
    ):
        if not (
            isinstance(draft_fingerprint, str)
            and _FINGERPRINT_RE.match(draft_fingerprint)
        ):
            raise ValueError("invalid draft fingerprint")
        if not _valid_base_revision(base_config_revision):
            raise ValueError("invalid base config revision")
        if prepared_config_sha256 is not None and not (
            isinstance(prepared_config_sha256, str)
            and _SHA256_RE.match(prepared_config_sha256)
        ):
            raise ValueError("invalid prepared config hash")
        with self._lock:
            record = self.require_active(workflow_id)
            record["preview"] = {
                "preview_id": self._id_factory(),
                "draft_fingerprint": draft_fingerprint,
                "base_config_revision": dict(base_config_revision),
                "prepared_config_sha256": prepared_config_sha256,
                "issued_at": utc_now_iso(),
            }
            return self._persist(record)

    def clear_preview(self, workflow_id):
        """Invalidate the stored preview; tolerant of a missing/foreign record."""

        with self._lock:
            record = self.active()
            if record is None or record["workflow_id"] != workflow_id:
                return None
            if record.get("preview") is None:
                return record
            record["preview"] = None
            return self._persist(record)

    def record_transition(
        self,
        workflow_id,
        *,
        operation_id,
        transition_mode,
        selected_system_tag=None,
    ):
        with self._lock:
            record = self.active()
            if record is None or record["workflow_id"] != workflow_id:
                return None
            if operation_id is not None:
                record["operation_id"] = str(operation_id)
            if transition_mode in _ALLOWED_TRANSITION_MODES:
                record["transition_mode"] = transition_mode
            if selected_system_tag:
                record["selected_system_tag"] = str(selected_system_tag)
            return self._persist(record)

    def bind_generated_artifacts(self, workflow_id, *, preview_id):
        """Record the workflow-owned generated config/metadata and its preview.

        The binding is durable on purpose: it names the preview the artifact was
        produced from, not whichever preview happens to be current later. A user
        who revisits Config Preview after generating a config issues newer
        previews; that must not retroactively disown a legitimate artifact.
        """

        with self._lock:
            record = self.require_active(workflow_id)
            base = ("workflows", "guided-setup", workflow_id, "generated")
            record["artifacts"]["generated_config"] = "/".join(
                base + ("config.json",)
            )
            record["artifacts"]["generated_metadata"] = "/".join(
                base + ("config.meta.json",)
            )
            record["artifacts"]["generated_preview_id"] = preview_id
            return self._persist(record)

    def record_deployment_marker(self, workflow_id):
        with self._lock:
            record = self.active()
            if record is None or record["workflow_id"] != workflow_id:
                return None
            record["artifacts"]["deployment_marker"] = "state/.admin-deployment.json"
            return self._persist(record)

    def finish(self, workflow_id, *, status, cleanup=None):
        """Mark the stored workflow terminal; idempotent for retried cleanup.

        Terminalization revokes mutation and preview authority immediately, and
        never changes the workflow ID: a failed cleanup stays owned by exactly
        this workflow so its retry can be addressed.
        """

        if status not in TERMINAL_STATUSES:
            raise ValueError(f"not a terminal workflow status: {status}")
        if cleanup is not None and not _valid_cleanup(cleanup):
            raise ValueError("invalid cleanup state")
        with self._lock:
            record = self.load()
            if record is None or record["workflow_id"] != workflow_id:
                return None
            record["status"] = status
            record["preview"] = None
            if cleanup is not None:
                record["cleanup"] = dict(cleanup)
            return self._persist(record)

    # --- internals ----------------------------------------------------------

    def _new_record(self, *, transition_mode=None, selected_system_tag=None):
        now = utc_now_iso()
        return {
            "format_version": GUIDED_SETUP_WORKFLOW_VERSION,
            "workflow_id": self._id_factory(),
            "type": GUIDED_SETUP_WORKFLOW_TYPE,
            "status": STATUS_ACTIVE,
            "operation_id": None,
            "transition_mode": (
                transition_mode
                if transition_mode in _ALLOWED_TRANSITION_MODES
                else None
            ),
            "selected_system_tag": (
                str(selected_system_tag) if selected_system_tag else None
            ),
            "preview": None,
            "artifacts": {
                "generated_config": None,
                "generated_metadata": None,
                "generated_preview_id": None,
                "deployment_marker": None,
            },
            "cleanup": empty_cleanup_state(),
            "created_at": now,
            "updated_at": now,
        }

    def _valid_record(self, record):
        if not isinstance(record, dict):
            return False
        if record.get("format_version") != GUIDED_SETUP_WORKFLOW_VERSION:
            return False
        if record.get("type") != GUIDED_SETUP_WORKFLOW_TYPE:
            return False
        workflow_id = record.get("workflow_id")
        if not (isinstance(workflow_id, str) and _ID_RE.match(workflow_id)):
            return False
        if record.get("status") not in TERMINAL_STATUSES | {STATUS_ACTIVE}:
            return False
        operation_id = record.get("operation_id")
        if operation_id is not None and not isinstance(operation_id, str):
            return False
        mode = record.get("transition_mode")
        if mode is not None and mode not in _ALLOWED_TRANSITION_MODES:
            return False
        tag = record.get("selected_system_tag")
        if tag is not None and not isinstance(tag, str):
            return False
        if not _valid_preview(record.get("preview")):
            return False
        artifacts = record.get("artifacts")
        if not isinstance(artifacts, dict) or set(artifacts) != {
            "generated_config",
            "generated_metadata",
            "generated_preview_id",
            "deployment_marker",
        }:
            return False
        generated_preview_id = artifacts["generated_preview_id"]
        if generated_preview_id is not None and not (
            isinstance(generated_preview_id, str)
            and _ID_RE.match(generated_preview_id)
        ):
            return False
        if not all(
            _valid_artifact_path(value, workflow_id)
            for key, value in artifacts.items()
            if key != "generated_preview_id"
        ):
            return False
        if not _valid_cleanup(record.get("cleanup")):
            return False
        if not isinstance(record.get("created_at"), str):
            return False
        if not isinstance(record.get("updated_at"), str):
            return False
        return True

    def _persist(self, record):
        record["updated_at"] = utc_now_iso()
        payload = (
            json.dumps(record, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=directory
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return record


__all__ = [
    "ARTIFACT_ABSENT",
    "ARTIFACT_FAILED",
    "ARTIFACT_KINDS",
    "ARTIFACT_REMOVED",
    "ARTIFACT_REVIEW_REQUIRED",
    "CLEANUP_COMPLETE",
    "CLEANUP_NOT_REQUIRED",
    "CLEANUP_PENDING",
    "CLEANUP_REVIEW_REQUIRED",
    "GUIDED_SETUP_WORKFLOW_FILE",
    "GuidedSetupWorkflowError",
    "GuidedSetupWorkflowStore",
    "SETUP_CLEANUP_REQUIRED",
    "SETUP_PREVIEW_MISMATCH",
    "SETUP_PREVIEW_REQUIRED",
    "SETUP_WORKFLOW_NOT_ACTIVE",
    "SETUP_WORKFLOW_REQUIRED",
    "STATUS_ABANDONED",
    "STATUS_ACTIVE",
    "STATUS_COMPLETED",
    "STATUS_SUPERSEDED",
    "cleanup_blocks",
    "cleanup_conflict_error",
    "cleanup_state",
    "empty_cleanup_state",
    "setup_mutation_fingerprint",
]
