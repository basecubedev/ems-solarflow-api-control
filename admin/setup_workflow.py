# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ownership and abandonment for the artifacts Guided Setup leaves on disk.

The single owner of Guided Setup's generated config, its metadata sidecar and
the deployment marker: the only place those paths are resolved, inspected and
removed. Artifacts are workflow-scoped (``workflows/guided-setup/<id>/``) with
the deployment marker remaining at its install-state-contract location. It never
touches the live config, the running EMS, or resources shared with Guided
Upgrade.

Ownership is decided in two separate stages. *Claim authority* comes from the
durable workflow record: only an artifact the record says this workflow created
is part of its cleanup plan, so a file that merely sits at a known path is
observed, never adopted. *Deletion proof* then decides whether a claimed
artifact is safe to remove: the workflow directory only when its normalized path
*is* that workflow's own directory, a global artifact only when its validated
content names that same workflow as owner. A claimed artifact whose owner cannot
be proven — a foreign workflow, a malformed sidecar, a marker from an install
that predates workflow ownership — is kept and reported as review-required.
Cleanup is best-effort per owned artifact; ownership never is.

See ``docs/technical/admin-workflow-state.md``.
"""

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from admin.admin_update import SETUP_TRANSITION_MODES
from admin.guided_setup_workflow import (
    ARTIFACT_ABSENT,
    ARTIFACT_FAILED,
    ARTIFACT_REMOVED,
    ARTIFACT_REVIEW_REQUIRED,
    CLEANUP_COMPLETE,
    CLEANUP_PENDING,
    CLEANUP_REVIEW_REQUIRED,
    SETUP_WORKFLOW_NOT_ACTIVE,
    SETUP_WORKFLOW_REQUIRED,
    STATUS_ABANDONED,
    STATUS_ACTIVE,
    WORKFLOW_NOT_ACTIVE_MESSAGE,
    WORKFLOW_REQUIRED_MESSAGE,
)
from admin.models import utc_now_iso

GENERATED_CONFIG_META_FILE = "config.meta.json"
GENERATED_CONFIG_OWNER = "guided_setup"
GENERATED_CONFIG_META_VERSION = 2

TERMINAL_STAGES = frozenset({"completed", "cancelled"})

CLEANUP_INCOMPLETE = "abandon_cleanup_incomplete"
ARTIFACT_REVIEW_REQUIRED_ERROR = "setup_artifact_review_required"

REASON_GENERATED_CONFIG_REVIEW = "generated_config_review_required"
REASON_LEGACY_ARTIFACT_REVIEW = "legacy_artifact_review_required"
REASON_OWNER_MISMATCH = "setup_artifact_owner_mismatch"

MAX_META_BYTES = 64 * 1024


@dataclass(frozen=True)
class SetupArtifactClaims:
    """What a workflow record says this workflow itself created.

    Relative paths exactly as the durable record stores them, or ``None`` where
    the workflow never claimed the artifact. ``generated_preview_id`` is
    authority metadata, not a filesystem target, so it is not represented here.
    """

    generated_config: str | None = None
    generated_metadata: str | None = None
    deployment_marker: str | None = None

    def claims_anything(self):
        return any(
            (self.generated_config, self.generated_metadata, self.deployment_marker)
        )


# The global locations a workflow can observe but never owns without a claim.
# A review state naming only these was produced by cleanup that inferred
# ownership from a path; nothing about it needs an operator.
UNCLAIMABLE_ARTIFACT_KINDS = frozenset(
    {"legacy_generated_config", "legacy_generated_metadata", "deployment_marker"}
)


def stale_unclaimed_review(record):
    """True when a stored review state blames artifacts the workflow never claimed.

    Reconciling such a record changes only the record: the files it named belong
    to the installed system and stay exactly as they are. A review state that
    names a claimed artifact, or this workflow's own directory, is a genuine
    ownership question and is never stale.
    """

    if not isinstance(record, dict) or record.get("status") == STATUS_ACTIVE:
        return False
    cleanup = record.get("cleanup")
    if not isinstance(cleanup, dict):
        return False
    if cleanup.get("state") != CLEANUP_REVIEW_REQUIRED:
        return False
    claims = setup_artifact_claims(record)
    if claims is None or claims.claims_anything():
        return False
    unresolved = {
        entry.get("kind")
        for entry in cleanup.get("artifacts") or []
        if isinstance(entry, dict)
        and entry.get("status") in {ARTIFACT_REVIEW_REQUIRED, ARTIFACT_FAILED}
    }
    return bool(unresolved) and unresolved <= UNCLAIMABLE_ARTIFACT_KINDS


def reconcile_unclaimed_review(workflows):
    """Persist ``complete`` for a record stranded by path-inferred ownership.

    Idempotent and filesystem-free; returns the reconciled record or ``None``
    when there was nothing to reconcile.
    """

    if workflows is None:
        return None
    record = workflows.load()
    if not stale_unclaimed_review(record):
        return None
    return workflows.finish(
        record["workflow_id"],
        status=record["status"],
        cleanup=cleanup_state_from_results([]),
    )


def setup_artifact_claims(record):
    """Read the artifact claims out of a validated workflow record.

    Returns ``None`` for a missing record: without one there is no claim
    evidence at all, and cleanup has to stay in its conservative legacy mode.
    """

    if not isinstance(record, dict):
        return None
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, dict):
        return SetupArtifactClaims()
    return SetupArtifactClaims(
        generated_config=artifacts.get("generated_config"),
        generated_metadata=artifacts.get("generated_metadata"),
        deployment_marker=artifacts.get("deployment_marker"),
    )


class SetupWorkflowArtifacts:
    """The single owner of Guided Setup's durable temporary artifacts.

    With a ``workflow_id`` the generated config and its metadata live in that
    workflow's own directory; without one this is the legacy singleton view
    used to inspect and clean up pre-workflow installs. The deployment marker
    path is part of the install-state contract
    (``<admin_data>/state/.admin-deployment.json``) and stays global — its
    ownership is recorded in its content and in the workflow record instead.
    """

    def __init__(self, admin_data_dir, *, workflow_id=None):
        base = Path(admin_data_dir)
        self.admin_data_dir = base
        self.workflow_id = workflow_id
        self.legacy_generated_config_path = base / "generated" / "config.json"
        self.legacy_generated_meta_path = (
            base / "generated" / GENERATED_CONFIG_META_FILE
        )
        self.deployment_marker_path = base / "state" / ".admin-deployment.json"
        if workflow_id:
            self.workflow_dir = base / "workflows" / "guided-setup" / workflow_id
            self.generated_config_path = (
                self.workflow_dir / "generated" / "config.json"
            )
            self.generated_meta_path = (
                self.workflow_dir / "generated" / GENERATED_CONFIG_META_FILE
            )
        else:
            self.workflow_dir = None
            self.generated_config_path = self.legacy_generated_config_path
            self.generated_meta_path = self.legacy_generated_meta_path

    def record_generated(
        self,
        *,
        workflow_id,
        preview_id,
        draft_fingerprint,
        base_config_revision,
        prepared_config_sha256,
    ):
        """Bind the generated config to its workflow, preview and live baseline.

        The sidecar is the artifact's ownership proof: deployment accepts a
        generated config only when this metadata names the active workflow's
        identity, its exact preview, the payload hash of the generated bytes
        and the live-config state the draft was reviewed against.
        """

        _atomic_write_json(
            self.generated_meta_path,
            {
                "format_version": GENERATED_CONFIG_META_VERSION,
                "owner": GENERATED_CONFIG_OWNER,
                "workflow_id": workflow_id,
                "preview_id": preview_id,
                "draft_fingerprint": draft_fingerprint,
                "base_config_revision": dict(base_config_revision),
                "prepared_config_sha256": prepared_config_sha256,
                "recorded_at": utc_now_iso(),
            },
        )

    def generated_metadata(self):
        return read_generated_metadata(self.generated_config_path)

    def state(self):
        return {
            "generated_config": {
                "path": str(self.generated_config_path),
                "exists": self.generated_config_path.is_file(),
            },
            "deployment_marker": {
                "path": str(self.deployment_marker_path),
                "exists": self.deployment_marker_path.is_file(),
            },
        }

    def clear(self, claims=None):
        """Remove only what this workflow can prove it owns; attempt all of it.

        With ``claims`` — the durable record's artifact claims — only artifacts
        this workflow recorded are inspected at all: files it never created are
        another owner's business, so they are neither read, removed nor reported.
        Without claims (no record exists) every known location stays in scope,
        because nothing can prove which of them this cleanup is responsible for.

        One failure must not skip the rest, and nothing whose owner cannot be
        proven is deleted. Returns a per-artifact
        ``{kind, path, status[, error][, reason]}`` list so the caller can report
        partial cleanup and unknown ownership truthfully instead of claiming an
        all-or-nothing result it did not deliver. ``path`` is server-side detail;
        the durable record keeps kind and status only.
        """

        if claims is None:
            results = []
            if self.workflow_dir is not None:
                results.extend(self._clear_workflow_directory())
            results.extend(self._clear_legacy_generated())
            results.append(self._clear_deployment_marker())
            return results
        return self._clear_claimed(claims)

    # --- claim-scoped cleanup planning -------------------------------------

    def _relative_claim(self, path):
        try:
            return Path(path).relative_to(self.admin_data_dir).as_posix()
        except ValueError:  # pragma: no cover - paths are built from the base
            return None

    def _generated_claim_scope(self, claims):
        """Which generated pair a record's claims address, if any.

        ``"unrecognized"`` is fail-closed on purpose: a claim that names no
        canonical generated path is not resolved into a deletion target.
        """

        claimed = {
            value
            for value in (claims.generated_config, claims.generated_metadata)
            if value
        }
        if not claimed:
            return None
        legacy = {
            self._relative_claim(self.legacy_generated_config_path),
            self._relative_claim(self.legacy_generated_meta_path),
        }
        if self.workflow_dir is not None:
            scoped = {
                self._relative_claim(self.generated_config_path),
                self._relative_claim(self.generated_meta_path),
            }
            if claimed <= scoped:
                return "scoped"
        if claimed <= legacy:
            return "legacy"
        return "unrecognized"

    def _unrecognized_claim(self, kind, path):
        return {
            "kind": kind,
            "path": str(path),
            "status": ARTIFACT_REVIEW_REQUIRED,
            "reason": REASON_OWNER_MISMATCH,
        }

    def _clear_claimed(self, claims):
        results = []
        # The workflow's own directory is namespaced by its id and proven by path
        # identity, so it needs no claim: it can hold nothing but this workflow's
        # files, including one written in the crash window before the record
        # claim was persisted.
        if self.workflow_dir is not None:
            results.extend(self._clear_workflow_directory())
        scope = self._generated_claim_scope(claims)
        # An artifact whose own sidecar/content names this workflow is in scope
        # even without a record claim: it can only be this workflow's, and the
        # claim may be missing because the process died between writing the file
        # and persisting the claim. Content that does not name this workflow is
        # someone else's file and stays out of scope entirely.
        if scope == "legacy" or (scope is None and self._legacy_owner_proven()):
            results.extend(self._clear_legacy_generated())
        elif scope == "unrecognized" or (scope == "scoped" and self.workflow_dir is None):
            results.append(
                self._unrecognized_claim("generated_config", self.generated_config_path)
            )
        marker_claim = claims.deployment_marker
        if marker_claim and marker_claim != self._relative_claim(
            self.deployment_marker_path
        ):
            results.append(
                self._unrecognized_claim(
                    "deployment_marker", self.deployment_marker_path
                )
            )
        elif marker_claim or self._marker_owner_proven():
            results.append(self._clear_deployment_marker())
        return results

    # --- ownership-proving removals ----------------------------------------

    def _owns_workflow_directory(self):
        """True only when ``workflow_dir`` *is* this workflow's own directory.

        Compares realpaths so a traversal-shaped or symlinked id cannot address a
        directory outside ``<admin_data>/workflows/guided-setup/<workflow_id>``.
        """

        if not self.workflow_id or self.workflow_dir is None:
            return False
        if self.workflow_dir.is_symlink():
            return False
        root = self.admin_data_dir / "workflows" / "guided-setup"
        expected = os.path.realpath(root / self.workflow_id)
        return (
            os.path.realpath(self.workflow_dir) == expected
            and os.path.dirname(expected) == os.path.realpath(root)
            and os.path.basename(expected) == self.workflow_id
        )

    def _clear_workflow_directory(self):
        scoped = (
            ("generated_config", self.generated_config_path),
            ("generated_metadata", self.generated_meta_path),
        )
        if not self._owns_workflow_directory():
            return [
                {
                    "kind": "workflow_directory",
                    "path": str(self.workflow_dir),
                    "status": ARTIFACT_REVIEW_REQUIRED,
                    "reason": REASON_OWNER_MISMATCH,
                }
            ]
        existed = {path: path.exists() for _kind, path in scoped}
        error = None
        directory_existed = self.workflow_dir.exists()
        if directory_existed:
            try:
                shutil.rmtree(self.workflow_dir)
            except OSError as exc:
                error = exc.strerror or type(exc).__name__
        results = []
        for kind, path in scoped:
            entry = {"kind": kind, "path": str(path)}
            if path.exists():
                entry["status"] = ARTIFACT_FAILED
                entry["error"] = error or "not removed"
            elif not existed[path]:
                entry["status"] = ARTIFACT_ABSENT
            else:
                entry["status"] = ARTIFACT_REMOVED
            results.append(entry)
        if directory_existed and self.workflow_dir.exists():
            results.append(
                {
                    "kind": "workflow_directory",
                    "path": str(self.workflow_dir),
                    "status": ARTIFACT_FAILED,
                    "error": error or "not removed",
                }
            )
        return results

    def _legacy_owner_proven(self):
        """True when the legacy sidecar proves this exact workflow's ownership."""

        if not self.workflow_id:
            return False
        meta = read_generated_metadata(self.legacy_generated_config_path)
        return bool(
            isinstance(meta, dict)
            and meta.get("owner") == GENERATED_CONFIG_OWNER
            and isinstance(meta.get("workflow_id"), str)
            and meta.get("workflow_id") == self.workflow_id
        )

    def _clear_legacy_generated(self):
        """The pre-workflow singleton pair: never deleted without owner proof."""

        pairs = (
            ("legacy_generated_config", self.legacy_generated_config_path,
             REASON_GENERATED_CONFIG_REVIEW),
            ("legacy_generated_metadata", self.legacy_generated_meta_path,
             REASON_LEGACY_ARTIFACT_REVIEW),
        )
        if self.workflow_dir is None:
            # No workflow scope: these are the same paths the scoped block would
            # own, but nothing here can prove which workflow wrote them.
            pairs = (
                ("generated_config", self.legacy_generated_config_path,
                 REASON_GENERATED_CONFIG_REVIEW),
                ("generated_metadata", self.legacy_generated_meta_path,
                 REASON_LEGACY_ARTIFACT_REVIEW),
            )
        proven = self._legacy_owner_proven()
        results = []
        for kind, path, reason in pairs:
            entry = {"kind": kind, "path": str(path)}
            if not path.exists():
                entry["status"] = ARTIFACT_ABSENT
            elif not proven:
                entry["status"] = ARTIFACT_REVIEW_REQUIRED
                entry["reason"] = reason
            else:
                try:
                    path.unlink()
                except FileNotFoundError:
                    entry["status"] = ARTIFACT_ABSENT
                except OSError as exc:
                    entry["status"] = ARTIFACT_FAILED
                    entry["error"] = exc.strerror or type(exc).__name__
                else:
                    entry["status"] = ARTIFACT_REMOVED
            results.append(entry)
        return results

    def _marker_owner_proven(self):
        """True when the marker's own content names this exact workflow."""

        if not self.workflow_id:
            return False
        marker = _read_json_document(self.deployment_marker_path)
        return bool(
            isinstance(marker, dict)
            and marker.get("owner") == GENERATED_CONFIG_OWNER
            and isinstance(marker.get("workflow_id"), str)
            and marker.get("workflow_id") == self.workflow_id
        )

    def _clear_deployment_marker(self):
        """Remove the global marker only when its content names this owner."""

        path = self.deployment_marker_path
        entry = {"kind": "deployment_marker", "path": str(path)}
        if not path.exists():
            entry["status"] = ARTIFACT_ABSENT
            return entry
        owned = self._marker_owner_proven()
        if not owned:
            entry["status"] = ARTIFACT_REVIEW_REQUIRED
            entry["reason"] = REASON_OWNER_MISMATCH
            return entry
        try:
            path.unlink()
        except FileNotFoundError:
            entry["status"] = ARTIFACT_ABSENT
        except OSError as exc:
            entry["status"] = ARTIFACT_FAILED
            entry["error"] = exc.strerror or type(exc).__name__
        else:
            entry["status"] = ARTIFACT_REMOVED
        return entry


def _read_json_document(path):
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if len(raw) > MAX_META_BYTES:
        return None
    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return document if isinstance(document, dict) else None


def cleanup_state_from_results(results):
    """The durable cleanup state a cleanup attempt earned.

    ``pending`` means a removal this workflow owned failed and a retry can
    converge; ``review_required`` means files remain whose owner could not be
    proven, so a retry will not help and an operator has to decide. Only the
    unresolved artifacts are recorded, as ``{kind, status}`` — no paths, no OS
    errors.
    """

    failed = [entry for entry in results if entry["status"] == ARTIFACT_FAILED]
    review = [
        entry for entry in results if entry["status"] == ARTIFACT_REVIEW_REQUIRED
    ]
    if failed:
        state = CLEANUP_PENDING
    elif review:
        state = CLEANUP_REVIEW_REQUIRED
    else:
        state = CLEANUP_COMPLETE
    return {
        "state": state,
        "attempted_at": utc_now_iso(),
        "failed_count": len(failed),
        "review_count": len(review),
        "artifacts": [
            {"kind": entry["kind"], "status": entry["status"]}
            for entry in failed + review
        ],
    }


def read_generated_metadata(generated_config_path):
    """Return the metadata recorded beside a generated config, else ``None``.

    Shape validation is the consumer's job: deployment treats anything that
    cannot prove workflow ownership as requiring regeneration.
    """

    path = Path(generated_config_path).parent / GENERATED_CONFIG_META_FILE
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > MAX_META_BYTES:
        return None
    try:
        meta = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return meta if isinstance(meta, dict) else None


OWNERSHIP_NONE = "none"
OWNERSHIP_OWNED = "owned"
OWNERSHIP_UNPROVEN = "unproven"
OWNERSHIP_MISMATCH = "mismatch"

SETUP_TRANSITION_OWNER_UNPROVEN = "setup_transition_owner_unproven"
SETUP_TRANSITION_CONTEXT_MISMATCH = "setup_transition_context_mismatch"

OWNER_UNPROVEN_MESSAGE = (
    "This setup cannot prove it owns the active System Build transition. Nothing "
    "was cancelled or removed."
)
CONTEXT_MISMATCH_MESSAGE = (
    "The active System Build transition belongs to a different setup operation. "
    "Nothing was cancelled or removed."
)


def transition_ownership(record, transition):
    """How strongly ``record`` can prove it owns ``transition``.

    ``none`` — there is no non-terminal transition, so there is nothing to own,
    cancel or adopt. ``owned`` — the workflow's stored ``operation_id`` *is* this
    Setup-mode transition's operation id. ``unproven`` — the workflow names no
    operation id, or the transition is not Setup-owned at all. ``mismatch`` — the
    workflow names a different operation id.

    A Setup-owned *mode* is never proof of ownership: it only classifies the
    transition. Only the exact operation id proves which workflow started it, so
    one workflow can never terminate or adopt the transition another one created.
    A missing record is the documented pre-workflow path (see
    ``abandon_setup_workflow``) and is answered ``owned`` for a Setup-mode
    transition: there is no workflow that could be named, so there is also no
    newer workflow whose state could be lost.
    """

    transition = transition or {}
    operation_id = transition.get("operation_id")
    if not operation_id or transition.get("stage") in TERMINAL_STAGES:
        return OWNERSHIP_NONE
    if transition.get("mode") not in SETUP_TRANSITION_MODES:
        return OWNERSHIP_UNPROVEN
    if record is None:
        return OWNERSHIP_OWNED
    linked = record.get("operation_id")
    if not linked:
        return OWNERSHIP_UNPROVEN
    return OWNERSHIP_OWNED if linked == operation_id else OWNERSHIP_MISMATCH


def transition_ownership_error(ownership):
    """The fail-closed ``(code, message)`` an unprovable ownership must return."""

    if ownership == OWNERSHIP_MISMATCH:
        return SETUP_TRANSITION_CONTEXT_MISMATCH, CONTEXT_MISMATCH_MESSAGE
    return SETUP_TRANSITION_OWNER_UNPROVEN, OWNER_UNPROVEN_MESSAGE


class SetupWorkflowAbandonError(Exception):
    """Abandonment could not run safely; nothing was changed."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


SETUP_TRANSITION_LINK_FAILED = "setup_transition_link_failed"
SETUP_TRANSITION_LINK_UNRECONCILED = "setup_transition_link_unreconciled"


class SetupTransitionLinkError(Exception):
    """The workflow could not be made the durable owner of its transition.

    Raised inside the System Alignment pre-commit boundary, so the transition is
    never committed and no Admin replacement is launched: an unownable transition
    must not exist at all.
    """

    code = SETUP_TRANSITION_LINK_FAILED

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class SetupTransitionLinkRollbackError(SetupTransitionLinkError):
    """The link was written, the transition commit failed, and so did the undo.

    Both stores are readable but no longer agree: the workflow names an
    operation that never became a transition. Nothing was launched, but the
    inconsistency is durable, so it is reported as its own reconciliation error
    rather than as the commit failure that triggered it.
    """

    code = SETUP_TRANSITION_LINK_UNRECONCILED


def abandon_setup_workflow(
    *,
    alignment,
    artifacts=None,
    coordinator=None,
    status=None,
    workflows=None,
    workflow_id=None,
    terminal_status=STATUS_ABANDONED,
):
    """Abandon Guided Setup and return the resulting authoritative state.

    Idempotent. With a ``workflows`` store this is workflow-verified: a
    ``workflow_id`` that does not match the stored record is refused before
    anything changes, so an old tab can never discard a newer workflow. A
    non-terminal transition is cancelled only when this workflow can prove it
    owns its exact ``operation_id``; an unproven or mismatched owner fails closed
    with nothing cancelled and nothing cleaned, because a workflow that cannot
    name the transition also cannot know which artifacts belong to it. A refused
    cancel propagates before any removal, so a running operation never loses
    state it still depends on. ``status`` is the caller's worker-aware read; an
    unreadable transition fails closed.

    Cleanup after a successful cancel is best-effort per *owned* artifact: a
    failed removal yields ``ok: False`` with ``abandon_cleanup_incomplete`` and
    retrying converges, while an artifact whose owner cannot be proven is kept
    and reported as ``setup_artifact_review_required``. The stored workflow is
    marked terminal either way — its transition is gone, so it must never regain
    mutation authority; only its cleanup may be retried, under the same ID.
    """

    record = workflows.load() if workflows is not None else None
    if workflow_id:
        if record is None or record.get("workflow_id") != workflow_id:
            raise SetupWorkflowAbandonError(
                SETUP_WORKFLOW_NOT_ACTIVE, WORKFLOW_NOT_ACTIVE_MESSAGE
            )
    elif record is not None:
        # A stored record makes the ID mandatory: the legacy no-ID path exists
        # only for installs that predate workflow ownership and must never be
        # able to adopt or discard the workflow currently on record.
        raise SetupWorkflowAbandonError(
            SETUP_WORKFLOW_REQUIRED, WORKFLOW_REQUIRED_MESSAGE
        )
    if artifacts is None:
        if workflows is None:
            raise ValueError("either artifacts or a workflow store is required")
        artifacts = SetupWorkflowArtifacts(
            workflows.admin_data_dir,
            workflow_id=record.get("workflow_id") if record else None,
        )

    status = status if status is not None else (alignment.status() or {})
    if status.get("ok") is False:
        raise SetupWorkflowAbandonError(
            "transition_status_unavailable",
            status.get("message")
            or "The System Build transition state could not be read.",
        )
    transition = dict(status.get("transition") or {})
    ownership = transition_ownership(record, transition)
    if ownership in {OWNERSHIP_UNPROVEN, OWNERSHIP_MISMATCH}:
        raise SetupWorkflowAbandonError(*transition_ownership_error(ownership))
    if ownership == OWNERSHIP_OWNED:
        cancelled = alignment.cancel(
            operation_id=transition["operation_id"], coordinator=coordinator
        )
        transition["stage"] = (cancelled or {}).get("stage", "cancelled")
    cleanup = artifacts.clear(claims=setup_artifact_claims(record))
    failed = [entry for entry in cleanup if entry["status"] == ARTIFACT_FAILED]
    review = [
        entry for entry in cleanup if entry["status"] == ARTIFACT_REVIEW_REQUIRED
    ]
    cleanup_state = cleanup_state_from_results(cleanup)
    workflow_state = None
    if workflows is not None and record is not None:
        finished = workflows.finish(
            record["workflow_id"], status=terminal_status, cleanup=cleanup_state
        )
        if finished is not None:
            workflow_state = {
                "workflow_id": finished["workflow_id"],
                "status": finished["status"],
                "cleanup": cleanup_state["state"],
            }
    result = {
        "ok": not failed and not review,
        "removed": [
            entry["path"] for entry in cleanup if entry["status"] == ARTIFACT_REMOVED
        ],
        "cleanup": cleanup,
        "cleanup_state": cleanup_state["state"],
        "transition": transition or None,
        "workflow": workflow_state,
        **artifacts.state(),
    }
    if failed:
        result["error"] = CLEANUP_INCOMPLETE
        result["status"] = 500
        result["message"] = (
            f"Guided Setup has stopped, but {len(failed)} setup file(s) could not "
            "be removed. Retry the cleanup to finish clearing them — the live "
            "config and the running EMS were not changed."
        )
    elif review:
        result["error"] = ARTIFACT_REVIEW_REQUIRED_ERROR
        result["status"] = 409
        result["message"] = (
            f"Guided Setup has stopped, but {len(review)} setup file(s) could not "
            "be proven to belong to this setup and were kept for review. The "
            "live config and the running EMS were not changed."
        )
    return result


def _atomic_write_json(path, payload):
    path = Path(path)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=directory
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
