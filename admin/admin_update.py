# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plan Admin Console self-updates and own the pending-update state.

Before a Guided EMS Upgrade the Admin Console may need to replace *itself*. This
module derives the target Admin image server-side from a trusted release *tag*
(the browser never sends an image ref), decides whether an update is required by
digest/build identity (never by tag name alone), and persists a small pending
record so the replacement Admin can resume the upgrade after it restarts.

The disruptive pull/recreate work lives in ``admin.update_apply`` and runs out of
the HTTP request. Nothing here stops the running Admin or touches the EMS
container/config/data.
"""

import fcntl
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from admin.image_identity import ImageIdentity, identify_image
from admin.models import utc_now_iso
from admin.releases import TAG_PATTERN
from admin.system_build_id import SystemBuildIdKind, parse_system_build_id

# Trusted image repositories. The browser never supplies an image ref; the target
# is always ``<repo>:<validated-tag>`` derived here.
ADMIN_IMAGE_REPO = "ghcr.io/basecubedev/ems-solarflow-admin"
EMS_IMAGE_REPO = "ghcr.io/basecubedev/ems-solarflow-api-control"

PENDING_ADMIN_UPDATE_FILE = "pending-admin-update.json"
PENDING_SCHEMA_VERSION = 1

# Canonical Admin identifiers. The compose *service* name (used by
# ``docker compose up <service>``) and the *container* name (used for Docker
# inspect identity) default to the same value but are configured separately.
DEFAULT_ADMIN_CONTAINER = "ems-solarflow-admin"
DEFAULT_ADMIN_COMPOSE_SERVICE = "ems-solarflow-admin"
DEFAULT_ADMIN_COMPOSE_FILE = "docker-compose.admin.yml"

# The Docker control socket. Fixed and validated: the updater sidecar mounts
# exactly this canonical host path (never a browser- or env-supplied path) so it
# can drive the daemon to recreate the Admin container.
DOCKER_SOCKET_PATH = "/var/run/docker.sock"

# Lifecycle stages for one pending update. The happy path is
# planned -> admin_update_started -> admin_update_succeeded, with
# admin_update_failed as the recoverable failure branch. resume_ready/completed/
# cancelled are terminal housekeeping states.
STAGE_PLANNED = "planned"
STAGE_STARTED = "admin_update_started"
STAGE_FAILED = "admin_update_failed"
STAGE_SUCCEEDED = "admin_update_succeeded"
STAGE_RESUME_READY = "resume_ready"
STAGE_COMPLETED = "completed"
STAGE_CANCELLED = "cancelled"

VALID_STAGES = frozenset(
    {
        STAGE_PLANNED,
        STAGE_STARTED,
        STAGE_FAILED,
        STAGE_SUCCEEDED,
        STAGE_RESUME_READY,
        STAGE_COMPLETED,
        STAGE_CANCELLED,
    }
)

# Stages that let the browser resume the EMS upgrade after the Admin restart.
RESUMABLE_STAGES = frozenset({STAGE_SUCCEEDED, STAGE_RESUME_READY})

NEXT_STEP_RESUME_EMS = "resume_ems_upgrade"

# Server-side EMS-upgrade gate outcomes. Admin version/self-update status is a
# compatibility *warning*, not a hard block — a local/dev Admin build must still
# be able to upgrade EMS. The only remaining hard block is a genuinely in-flight
# Admin self-update, which cannot safely run concurrently with an EMS upgrade.
GATE_NOT_REQUIRED = "admin_update_not_required"
GATE_COMPLETED = "admin_update_completed"
GATE_RECOMMENDED = "admin_update_recommended"
GATE_UNAVAILABLE = "admin_update_unavailable"
GATE_IN_PROGRESS = "admin_update_in_progress"

SEVERITY_OK = "ok"
SEVERITY_WARNING = "warning"

_EMS_WARN_RECOMMENDED = (
    "This Admin Console may not match the selected EMS release. You can still "
    "continue, but if the upgrade fails, update or restart the Admin Console and retry."
)
_EMS_WARN_UNAVAILABLE = (
    "The Admin Console cannot update itself in this environment. EMS upgrade can "
    "continue, but verify the result after the upgrade."
)
_EMS_IN_PROGRESS_MESSAGE = (
    "An Admin Console update is currently running. Wait for it to finish before "
    "starting the EMS upgrade."
)

# Decision reason codes (stable; surfaced to the UI and tests).
REASON_DIGEST_MATCH = "digest_match"
REASON_DIGEST_CHANGED = "digest_changed"
REASON_DIGEST_UNKNOWN = "digest_unknown"
REASON_CURRENT_UNKNOWN = "current_identity_unknown"

_CURRENT_UNKNOWN_WARNING = (
    "Current Admin image could not be detected. The update will be attempted "
    "after explicit confirmation."
)
_TARGET_DIGEST_UNKNOWN_WARNING = (
    "Target Admin image digest could not be verified. Confirm to update the "
    "Admin Console before the EMS upgrade."
)


def validate_release_tag(release_tag: str) -> str:
    """Return the trimmed tag or raise ``ValueError`` for anything unsafe.

    Enforces the same strict pattern as the release catalogue
    (:data:`admin.releases.TAG_PATTERN`), so shell/metacharacter strings
    (``v0.7.0;rm``, ``v0.7.0$(x)``, ``../v0.7.0``) are rejected even without
    whitespace, and the browser can never smuggle an image ref through a tag.
    """

    tag = str(release_tag or "").strip()
    if not tag:
        raise ValueError("release tag is required")
    if not TAG_PATTERN.fullmatch(tag):
        raise ValueError("release tag is invalid")
    return tag


def target_admin_image_for_release(release_tag: str) -> str:
    """Derive the target Admin image ref ``<ADMIN_IMAGE_REPO>:<validated-tag>``."""

    return f"{ADMIN_IMAGE_REPO}:{validate_release_tag(release_tag)}"


def target_ems_image_for_release(release_tag: str) -> str:
    """Derive the EMS image ref for the same tag (metadata only)."""

    return f"{EMS_IMAGE_REPO}:{validate_release_tag(release_tag)}"


@dataclass(frozen=True)
class AdminImageTarget:
    """The Admin image a release resolves to, with any known build identity."""

    release_tag: str
    image_ref: str
    digest: str | None = None
    revision: str | None = None
    build_serial: int | None = None

    def as_dict(self) -> dict:
        return {
            "release_tag": self.release_tag,
            "image_ref": self.image_ref,
            "digest": self.digest,
            "revision": self.revision,
            "build_serial": self.build_serial,
        }


@dataclass(frozen=True)
class AdminUpdateDecision:
    """Whether the running Admin must update before the EMS upgrade, and why."""

    update_required: bool
    reason: str
    current: dict
    target: dict
    warning: str | None = None

    def as_dict(self) -> dict:
        return {
            "update_required": self.update_required,
            "reason": self.reason,
            "current": dict(self.current),
            "target": dict(self.target),
            "warning": self.warning,
        }


def _clean(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _identity_view(identity: ImageIdentity) -> dict:
    """Compact, JSON-safe view of a build identity for state/decision output."""

    identity = identity or ImageIdentity()
    return {
        "image_ref": identity.image_ref,
        "digest": identity.digest,
        "revision": identity.revision,
        "build_serial": identity.build_serial,
    }


def _plan_summary(pending: dict) -> dict:
    """Compact view of a pending record for the EMS-upgrade gate response."""

    return {
        "target_release": pending.get("target_release"),
        "stage": pending.get("stage"),
        "update_required": pending.get("update_required"),
        "reason": pending.get("reason"),
    }


def admin_image_ref_from_env(environ=None) -> str | None:
    """Build the current Admin image ref from ``EMS_ADMIN_IMAGE``/``EMS_ADMIN_TAG``.

    The installer/compose stamp these non-secret values, so this is the reliable
    fallback when Docker cannot be inspected. If ``EMS_ADMIN_IMAGE`` already
    carries a tag it is used verbatim; otherwise ``EMS_ADMIN_TAG`` (default
    ``latest``) is appended.
    """

    environ = os.environ if environ is None else environ
    image = _clean(environ.get("EMS_ADMIN_IMAGE"))
    if not image:
        return None
    # A tag on the final path segment (after the last '/') means it is complete.
    if ":" in image.rsplit("/", 1)[-1]:
        return image
    tag = _clean(environ.get("EMS_ADMIN_TAG")) or "latest"
    return f"{image}:{tag}"


def _safe_inspect_container(docker, name):
    inspect = getattr(docker, "inspect_container", None)
    if not callable(inspect):
        return None
    try:
        return inspect(name)
    except Exception:  # inspection must never crash identity detection
        return None


def detect_current_admin_identity(docker=None, environ=None, hostname=None):
    """Detect the running Admin image identity and how it was found.

    Tries Docker inspect (container name, then hostname), then the
    ``EMS_ADMIN_IMAGE``/``EMS_ADMIN_TAG`` env fallback, enriching the ref with the
    local image digest/labels. Never raises — an undetectable identity is
    all-``None`` with source ``unknown``.
    """

    environ = os.environ if environ is None else environ
    image_ref = None
    source = "unknown"

    container_name = _clean(environ.get("EMS_ADMIN_CONTAINER_NAME"))
    if docker is not None and container_name:
        info = _safe_inspect_container(docker, container_name)
        ref = _clean((info or {}).get("image"))
        if ref:
            image_ref, source = ref, "docker_inspect"

    if image_ref is None and docker is not None:
        host = _clean(hostname) or socket.gethostname()
        info = _safe_inspect_container(docker, host)
        ref = _clean((info or {}).get("image"))
        if ref:
            image_ref, source = ref, "docker_inspect"

    if image_ref is None:
        ref = admin_image_ref_from_env(environ)
        if ref:
            image_ref, source = ref, "env"

    if image_ref is None:
        return ImageIdentity(), source
    if docker is not None:
        return identify_image(docker, image_ref), source
    return ImageIdentity(image_ref=image_ref), source


def resolve_admin_image_target(release_tag, docker=None) -> AdminImageTarget:
    """Derive the target Admin image and read any locally-known build identity.

    The digest is only populated when the target image is already present
    locally (``inspect_image`` never pulls); a not-yet-pulled target keeps a
    ``None`` digest, which the decision treats as "uncertain".
    """

    image_ref = target_admin_image_for_release(release_tag)
    identity = ImageIdentity(image_ref=image_ref)
    if docker is not None:
        identity = identify_image(docker, image_ref)
    return AdminImageTarget(
        release_tag=str(release_tag).strip(),
        image_ref=image_ref,
        digest=identity.digest,
        revision=identity.revision,
        build_serial=identity.build_serial,
    )


def decide_admin_update(current: ImageIdentity, target: ImageIdentity) -> AdminUpdateDecision:
    """Decide whether the Admin Console must update, preferring digest identity.

    Policy, in order:

    1. Current identity undetectable -> update required, uncertain (warning).
    2. Both digests known and equal -> no update needed (the release retagged an
       unchanged Admin image).
    3. Both digests known and different -> update required.
    4. Either digest unknown -> update required but uncertain (warning); the
       caller must require explicit confirmation. Tag names alone never settle
       it.
    """

    current = current or ImageIdentity()
    target = target or ImageIdentity()
    current_view = _identity_view(current)
    target_view = _identity_view(target)

    if not current.image_ref and not current.digest:
        return AdminUpdateDecision(
            True, REASON_CURRENT_UNKNOWN, current_view, target_view,
            warning=_CURRENT_UNKNOWN_WARNING,
        )

    if current.digest and target.digest:
        if current.digest == target.digest:
            return AdminUpdateDecision(
                False, REASON_DIGEST_MATCH, current_view, target_view
            )
        return AdminUpdateDecision(
            True, REASON_DIGEST_CHANGED, current_view, target_view
        )

    return AdminUpdateDecision(
        True, REASON_DIGEST_UNKNOWN, current_view, target_view,
        warning=_TARGET_DIGEST_UNKNOWN_WARNING,
    )


class PendingUpdateStateError(Exception):
    """A pending-state file exists but cannot be used (unreadable/corrupt)."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason
        self.message = message


class PendingAdminUpdateStore:
    """Atomic reader/writer for the single pending Admin-update record.

    One JSON object under ``<admin-data>/state/pending-admin-update.json``. Writes
    are atomic (temp file + fsync + rename); a missing file reads as ``None``; a
    corrupt file raises :class:`PendingUpdateStateError` rather than crashing the
    Admin server. It never stores passwords or secrets.
    """

    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / PENDING_ADMIN_UPDATE_FILE
        self._lock = threading.Lock()

    def read(self) -> dict | None:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PendingUpdateStateError(
                "state_unreadable",
                f"The pending Admin update state could not be read: {exc}",
            )
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise PendingUpdateStateError(
                "state_corrupt",
                "The pending Admin update state file is corrupt. Delete "
                f"{self.path} to recover.",
            )
        if not isinstance(data, dict):
            raise PendingUpdateStateError(
                "state_corrupt",
                "The pending Admin update state file is not a JSON object. Delete "
                f"{self.path} to recover.",
            )
        return data

    def write(self, state: dict) -> dict:
        if not isinstance(state, dict):
            raise ValueError("pending Admin update state must be a dict")
        payload = json.dumps(state, indent=2, sort_keys=True).encode("utf-8")
        with self._lock:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=".pending-admin-update.", suffix=".tmp", dir=self.state_dir
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
        return state

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


# --- staged system-build transition record (schema v2) -------------------
#
# v1 (the pending-admin-update plan above) is the legacy per-release Admin
# self-update record; the v2 transition record below is the single-use form the
# paired-system-build flows persist for the whole Admin+EMS operation. It reuses
# the same state directory and atomic-write discipline, but commits each
# externally-visible stage so reconnect and job polling never consume or
# prematurely finish the operation.

TRANSITION_SCHEMA_VERSION = 2
PENDING_TRANSITION_FILE = "pending-transition.json"

# One hour: long enough for an Admin pull+recreate+reconnect, short enough that a
# stale transition never lingers as a live resume target.
DEFAULT_TRANSITION_TTL_SECONDS = 3600

TRANSITION_MODE_AUTOMATED_SETUP = "automated_setup"
TRANSITION_MODE_FRESH_INSTALL = "fresh_install"
TRANSITION_MODE_GUIDED_UPGRADE = "guided_upgrade"
TRANSITION_MODE_ALIGN_EXISTING = "align_existing_install"
SUPPORTED_TRANSITION_MODES = frozenset(
    {
        TRANSITION_MODE_AUTOMATED_SETUP,
        TRANSITION_MODE_FRESH_INSTALL,
        TRANSITION_MODE_GUIDED_UPGRADE,
        TRANSITION_MODE_ALIGN_EXISTING,
    }
)
# The modes a Guided Setup workflow can own. Defined here with the modes
# themselves so every consumer classifies a transition the same way.
SETUP_TRANSITION_MODES = frozenset(
    {TRANSITION_MODE_FRESH_INSTALL, TRANSITION_MODE_AUTOMATED_SETUP}
)

TRANSITION_STAGE_ADMIN_UPDATE_PENDING = "admin_update_pending"
TRANSITION_STAGE_ADMIN_RECONNECT_PENDING = "admin_reconnect_pending"
TRANSITION_STAGE_ADMIN_ALIGNED = "admin_aligned"
TRANSITION_STAGE_RESOURCES_VERIFIED = "resources_verified"
TRANSITION_STAGE_EMS_OPERATION_PENDING = "ems_operation_pending"
TRANSITION_STAGE_EMS_OPERATION_RUNNING = "ems_operation_running"
TRANSITION_STAGE_HEALTHCHECK_PENDING = "healthcheck_pending"
TRANSITION_STAGE_COMPLETED = "completed"
TRANSITION_STAGE_FAILED_RECOVERABLE = "failed_recoverable"
TRANSITION_STAGE_CANCELLED = "cancelled"
VALID_TRANSITION_STAGES = frozenset(
    {
        TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
        TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
        TRANSITION_STAGE_ADMIN_ALIGNED,
        TRANSITION_STAGE_RESOURCES_VERIFIED,
        TRANSITION_STAGE_EMS_OPERATION_PENDING,
        TRANSITION_STAGE_EMS_OPERATION_RUNNING,
        TRANSITION_STAGE_HEALTHCHECK_PENDING,
        TRANSITION_STAGE_COMPLETED,
        TRANSITION_STAGE_FAILED_RECOVERABLE,
        TRANSITION_STAGE_CANCELLED,
    }
)
TERMINAL_TRANSITION_STAGES = frozenset(
    {TRANSITION_STAGE_COMPLETED, TRANSITION_STAGE_CANCELLED}
)
CANCELLABLE_TRANSITION_STAGES = frozenset(
    {
        TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
        TRANSITION_STAGE_ADMIN_ALIGNED,
        TRANSITION_STAGE_RESOURCES_VERIFIED,
        TRANSITION_STAGE_EMS_OPERATION_PENDING,
        TRANSITION_STAGE_FAILED_RECOVERABLE,
    }
)


def transition_resource_verification_active(record) -> bool:
    """True while a claimed resource import may still write the shared cache.

    The claim is taken before the import and the stage only advances after it
    returns, so ``admin_aligned`` plus a claim is externally mutating even though
    the visible stage has not moved. At ``resources_verified`` the same claim is
    history, and at ``failed_recoverable`` the attempt is over.
    """

    return bool(
        getattr(record, "stage", None) == TRANSITION_STAGE_ADMIN_ALIGNED
        and getattr(record, "resources_claimed_at", None)
    )


_TRANSITION_EDGES = {
    TRANSITION_STAGE_ADMIN_UPDATE_PENDING: {
        TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
        TRANSITION_STAGE_FAILED_RECOVERABLE,
    },
    TRANSITION_STAGE_ADMIN_RECONNECT_PENDING: {
        TRANSITION_STAGE_ADMIN_ALIGNED,
        TRANSITION_STAGE_FAILED_RECOVERABLE,
    },
    TRANSITION_STAGE_ADMIN_ALIGNED: {
        TRANSITION_STAGE_RESOURCES_VERIFIED,
        TRANSITION_STAGE_FAILED_RECOVERABLE,
    },
    TRANSITION_STAGE_RESOURCES_VERIFIED: {
        TRANSITION_STAGE_EMS_OPERATION_PENDING,
        TRANSITION_STAGE_FAILED_RECOVERABLE,
    },
    TRANSITION_STAGE_EMS_OPERATION_PENDING: {
        TRANSITION_STAGE_EMS_OPERATION_RUNNING,
        TRANSITION_STAGE_FAILED_RECOVERABLE,
    },
    TRANSITION_STAGE_EMS_OPERATION_RUNNING: {
        TRANSITION_STAGE_HEALTHCHECK_PENDING,
        TRANSITION_STAGE_FAILED_RECOVERABLE,
    },
    TRANSITION_STAGE_HEALTHCHECK_PENDING: {
        TRANSITION_STAGE_COMPLETED,
        TRANSITION_STAGE_FAILED_RECOVERABLE,
    },
    TRANSITION_STAGE_FAILED_RECOVERABLE: set(),
    TRANSITION_STAGE_COMPLETED: set(),
    TRANSITION_STAGE_CANCELLED: set(),
}

_RECOVERY_STAGES_BY_FAILED_STAGE = {
    TRANSITION_STAGE_ADMIN_UPDATE_PENDING: {TRANSITION_STAGE_ADMIN_UPDATE_PENDING},
    TRANSITION_STAGE_ADMIN_RECONNECT_PENDING: {TRANSITION_STAGE_ADMIN_UPDATE_PENDING},
    TRANSITION_STAGE_ADMIN_ALIGNED: {TRANSITION_STAGE_ADMIN_ALIGNED},
    TRANSITION_STAGE_RESOURCES_VERIFIED: {TRANSITION_STAGE_RESOURCES_VERIFIED},
    TRANSITION_STAGE_EMS_OPERATION_PENDING: {TRANSITION_STAGE_EMS_OPERATION_PENDING},
    TRANSITION_STAGE_EMS_OPERATION_RUNNING: {TRANSITION_STAGE_EMS_OPERATION_PENDING},
    # A health probe may be retried in place when identity inspection was
    # temporarily unavailable. A positively identified wrong EMS must instead
    # return to the one-shot deployment claim before health is trusted again.
    TRANSITION_STAGE_HEALTHCHECK_PENDING: {
        TRANSITION_STAGE_HEALTHCHECK_PENDING,
        TRANSITION_STAGE_EMS_OPERATION_PENDING,
    },
}

_REQUEST_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_TRANSITION_REQUIRED_STR_FIELDS = (
    "operation_id",
    "mode",
    "stage",
    "system_tag",
    "build_id",
    "revision",
    "admin_image",
    "admin_digest",
    "ems_image",
    "ems_digest",
    "created_at",
    "expires_at",
)


def _short_revision(revision) -> str:
    text = str(revision or "").strip()
    return text[:7]


def _build_id_embeds_revision(build_id, kind, revision) -> bool:
    """True when ``build_id`` is consistent with ``revision`` for its kind.

    A modern build id embeds the short revision; a legacy CI build id
    (``<run>-<attempt>``) predates that convention and carries no revision, so
    only the modern kinds are held to the embedding rule.
    """

    if kind is SystemBuildIdKind.LEGACY_CI:
        return True
    return _short_revision(revision) in build_id


def _parse_orchestrator_admin(raw, *, selected):
    """Validate an optional orchestrator-Admin block; return override fields or None.

    ``selected`` is the selected-build Admin identity. When the block is absent
    or identical to it there is no override (a modern paired build, or an older
    record). Otherwise it is a legacy orchestrator override, held to the same
    build-id integrity rules as the selected build.
    """

    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise TransitionStateError(
            "state_malformed", "transition orchestrator_admin is not an object"
        )
    fields = {}
    for key in ("build_id", "revision", "image", "digest"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise TransitionStateError(
                "state_malformed",
                f"transition orchestrator_admin field '{key}' is missing or invalid",
            )
        fields[key] = value.strip()
    try:
        kind = parse_system_build_id(fields["build_id"]).kind
    except (TypeError, ValueError) as exc:
        raise TransitionStateError(
            "state_malformed", "transition orchestrator_admin build_id is invalid"
        ) from exc
    if not _build_id_embeds_revision(fields["build_id"], kind, fields["revision"]):
        raise TransitionStateError(
            "state_tampered",
            "transition orchestrator_admin build_id does not match its revision",
        )
    if all(fields[key] == selected[key] for key in fields):
        return None
    return fields


def _parse_iso(value):
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _now_utc():
    return datetime.now(timezone.utc)


class TransitionStateError(Exception):
    """A transition record is present but cannot be used/resumed.

    ``reason`` is a stable machine code (surfaced to the UI/tests):
    ``state_malformed``, ``unsupported_state_version``, ``unsupported_mode``,
    ``state_tampered``, ``expired``, ``not_resumable``, ``admin_identity_mismatch``,
    ``admin_unverifiable``, ``transition_active``, ``no_transition``.
    """

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class TransitionRecord:
    """One staged paired-system-build operation (Admin realign -> healthy EMS)."""

    operation_id: str
    mode: str
    stage: str
    system_tag: str
    build_id: str
    revision: str
    admin_image: str
    admin_digest: str
    ems_image: str
    ems_digest: str
    created_at: str
    expires_at: str
    state_version: int = TRANSITION_SCHEMA_VERSION
    resume_path: str | None = None
    next_step: str = NEXT_STEP_RESUME_EMS
    updated_at: str | None = None
    failed_stage: str | None = None
    resume_stage: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    admin_update_claimed_at: str | None = None
    # Persist whether this transition actually required replacing/retagging the
    # Admin.  Later stages cannot reconstruct that historical decision from the
    # now-aligned running image, but the progress UI must distinguish executed
    # Admin work from an honest "not required" skip after a reload.
    admin_alignment_required: bool | None = None
    resources_claimed_at: str | None = None
    request_fingerprint: str | None = None
    development_risk_acknowledged: bool = False
    development_risk_acknowledged_for_tag: str | None = None
    # Compatibility-aware identity separation. For a modern paired build the
    # running Admin *becomes* the selected Admin, so these stay ``None`` and the
    # orchestrator identity falls back to the selected Admin fields. For a legacy
    # release the running modern Admin orchestrates the historical EMS install,
    # so it is recorded here — never collapsed into the selected build's Admin
    # image, which is never run.
    compatibility_mode: str | None = None
    resource_strategy: str | None = None
    orchestrator_build_id: str | None = None
    orchestrator_revision: str | None = None
    orchestrator_admin_image: str | None = None
    orchestrator_admin_digest: str | None = None

    def _orchestrator_override(self) -> dict | None:
        """The persisted legacy orchestrator override, or ``None`` (modern paired).

        Only a genuine override is persisted; a modern record stores nothing here
        so a tampered ``admin_digest`` can never be masked by a duplicate copy.
        """

        if self.orchestrator_admin_digest is None:
            return None
        return {
            "build_id": self.orchestrator_build_id,
            "revision": self.orchestrator_revision,
            "image": self.orchestrator_admin_image,
            "digest": self.orchestrator_admin_digest,
        }

    @property
    def orchestrator_admin(self) -> dict:
        """The effective Admin identity that orchestrates this transition.

        Falls back to the selected Admin fields, so a record persisted before the
        orchestrator block existed keeps the correct (modern paired) meaning.
        """

        override = self._orchestrator_override()
        if override is not None:
            return dict(override)
        return {
            "build_id": self.build_id,
            "revision": self.revision,
            "image": self.admin_image,
            "digest": self.admin_digest,
        }

    @property
    def selected_ems_build(self) -> dict:
        """The EMS System Build this transition installs (the selected build)."""

        return {
            "version": self.system_tag,
            "image": self.ems_image,
            "digest": self.ems_digest,
            "build_id": self.build_id,
            "revision": self.revision,
        }

    def is_expired(self, now=None) -> bool:
        """True once ``now`` (default: current UTC time) has passed the TTL."""

        return _transition_is_expired(self, now or _now_utc())

    def as_dict(self) -> dict:
        return {
            "state_version": self.state_version,
            "operation_id": self.operation_id,
            "mode": self.mode,
            "stage": self.stage,
            "system_tag": self.system_tag,
            "build_id": self.build_id,
            "revision": self.revision,
            "admin_image": self.admin_image,
            "admin_digest": self.admin_digest,
            "ems_image": self.ems_image,
            "ems_digest": self.ems_digest,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "resume_path": self.resume_path,
            "next_step": self.next_step,
            "updated_at": self.updated_at or self.created_at,
            "failed_stage": self.failed_stage,
            "resume_stage": self.resume_stage,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "admin_update_claimed_at": self.admin_update_claimed_at,
            "admin_alignment_required": self.admin_alignment_required,
            "resources_claimed_at": self.resources_claimed_at,
            "request_fingerprint": self.request_fingerprint,
            "development_risk_acknowledged": self.development_risk_acknowledged,
            "development_risk_acknowledged_for_tag": (
                self.development_risk_acknowledged_for_tag
            ),
            "compatibility_mode": self.compatibility_mode,
            "resource_strategy": self.resource_strategy,
            # The persisted orchestrator override (``None`` for a modern paired
            # build, where the orchestrator is the selected Admin) and the derived
            # selected EMS build. Persisting only a genuine override keeps a
            # tampered admin_digest from hiding behind a duplicate copy.
            "orchestrator_admin": self._orchestrator_override(),
            "selected_ems_build": self.selected_ems_build,
        }


TRANSITION_MUTABLE_FIELDS = frozenset(
    {
        "stage",
        "updated_at",
        "failed_stage",
        "resume_stage",
        "error_code",
        "error_message",
        "admin_update_claimed_at",
        "resources_claimed_at",
    }
)

# Everything a record carries that is *not* lifecycle state: the fields
# ``make_transition_record`` fixes when the operation is created and no store
# mutation may ever change. Derived by exclusion, so a field added to the record
# is immutable identity unless it is declared mutable above.
TRANSITION_IDENTITY_FIELDS = tuple(
    field.name
    for field in fields(TransitionRecord)
    if field.name not in TRANSITION_MUTABLE_FIELDS
)


def transition_identity(record) -> tuple:
    """The immutable projection that defines *which* operation a record is."""

    return tuple(getattr(record, name, None) for name in TRANSITION_IDENTITY_FIELDS)


def _require_transition_str(data, key):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TransitionStateError(
            "state_malformed", f"transition field '{key}' is missing or invalid"
        )
    return value.strip()


def _optional_transition_str(data, key):
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise TransitionStateError(
            "state_malformed", f"transition field '{key}' is invalid"
        )
    return value.strip()


def parse_transition_record(data) -> TransitionRecord:
    """Structurally validate a persisted transition dict into a record.

    Rejects (in order) a non-object, an unknown ``state_version``, an
    unsupported ``mode``, any missing required field, and a ``build_id`` that no
    longer embeds the short revision (structural tampering). Time-based and
    running-Admin checks are separate (see :func:`validate_transition_for_resume`).
    """

    if not isinstance(data, dict):
        raise TransitionStateError(
            "state_malformed", "transition state is not a JSON object"
        )
    version = data.get("state_version")
    if version != TRANSITION_SCHEMA_VERSION:
        raise TransitionStateError(
            "unsupported_state_version",
            f"unsupported transition state_version: {version!r}",
        )
    values = {
        key: _require_transition_str(data, key)
        for key in _TRANSITION_REQUIRED_STR_FIELDS
        if key != "build_id"
    }
    try:
        parsed_build_id = parse_system_build_id(data.get("build_id"))
    except (TypeError, ValueError) as exc:
        raise TransitionStateError(
            "state_malformed", "transition field 'build_id' is invalid"
        ) from exc
    values["build_id"] = parsed_build_id.value
    build_id_kind = parsed_build_id.kind
    if values["mode"] not in SUPPORTED_TRANSITION_MODES:
        raise TransitionStateError(
            "unsupported_mode", f"unsupported transition mode: {values['mode']!r}"
        )
    if values["stage"] not in VALID_TRANSITION_STAGES:
        raise TransitionStateError(
            "invalid_stage", f"unsupported transition stage: {values['stage']!r}"
        )
    # Integrity: a modern build id embeds the short revision, so a tampered
    # build_id that no longer matches the revision is rejected before resume. A
    # legacy CI build id (``<run>-<attempt>``) predates that convention and does
    # not embed a revision; its format is validated above and its release
    # identity is verified upstream, so the embedding rule is not applied to it.
    if not _build_id_embeds_revision(
        values["build_id"], build_id_kind, values["revision"]
    ):
        raise TransitionStateError(
            "state_tampered", "transition build_id does not match its revision"
        )
    orchestrator = _parse_orchestrator_admin(
        data.get("orchestrator_admin"),
        selected={
            "build_id": values["build_id"],
            "revision": values["revision"],
            "image": values["admin_image"],
            "digest": values["admin_digest"],
        },
    )
    compatibility_mode = _optional_transition_str(data, "compatibility_mode")
    resource_strategy = _optional_transition_str(data, "resource_strategy")
    next_step = data.get("next_step")
    resume_path = data.get("resume_path")
    failed_stage = _optional_transition_str(data, "failed_stage")
    resume_stage = _optional_transition_str(data, "resume_stage")
    error_code = _optional_transition_str(data, "error_code")
    error_message = _optional_transition_str(data, "error_message")
    admin_update_claimed_at = _optional_transition_str(
        data, "admin_update_claimed_at"
    )
    admin_alignment_required = data.get("admin_alignment_required")
    if admin_alignment_required is not None and not isinstance(
        admin_alignment_required, bool
    ):
        raise TransitionStateError(
            "state_malformed",
            "transition Admin alignment requirement is invalid",
        )
    resources_claimed_at = _optional_transition_str(data, "resources_claimed_at")
    request_fingerprint = _optional_transition_str(data, "request_fingerprint")
    development_risk_acknowledged = data.get(
        "development_risk_acknowledged", False
    )
    if not isinstance(development_risk_acknowledged, bool):
        raise TransitionStateError(
            "state_malformed",
            "transition development acknowledgement is invalid",
        )
    development_risk_acknowledged_for_tag = _optional_transition_str(
        data, "development_risk_acknowledged_for_tag"
    )
    if development_risk_acknowledged:
        if development_risk_acknowledged_for_tag != values["system_tag"]:
            raise TransitionStateError(
                "state_tampered",
                "transition development acknowledgement targets another System Build",
            )
    elif development_risk_acknowledged_for_tag is not None:
        raise TransitionStateError(
            "state_tampered",
            "transition has a development acknowledgement tag without acknowledgement",
        )
    if request_fingerprint and not _REQUEST_FINGERPRINT_RE.fullmatch(
        request_fingerprint
    ):
        raise TransitionStateError(
            "state_malformed", "transition request_fingerprint is invalid"
        )
    if values["stage"] == TRANSITION_STAGE_FAILED_RECOVERABLE:
        allowed_resume = _RECOVERY_STAGES_BY_FAILED_STAGE.get(failed_stage, set())
        if resume_stage not in allowed_resume:
            raise TransitionStateError(
                "state_tampered",
                "recoverable transition has an invalid recovery path",
            )
        if not error_code or not error_message:
            raise TransitionStateError(
                "state_malformed", "recoverable transition is missing failure details"
            )
    elif any((failed_stage, resume_stage, error_code, error_message)):
        raise TransitionStateError(
            "state_tampered",
            "non-failed transition contains recovery metadata",
        )
    return TransitionRecord(
        operation_id=values["operation_id"],
        mode=values["mode"],
        stage=values["stage"],
        system_tag=values["system_tag"],
        build_id=values["build_id"],
        revision=values["revision"],
        admin_image=values["admin_image"],
        admin_digest=values["admin_digest"],
        ems_image=values["ems_image"],
        ems_digest=values["ems_digest"],
        created_at=values["created_at"],
        expires_at=values["expires_at"],
        state_version=TRANSITION_SCHEMA_VERSION,
        resume_path=resume_path if isinstance(resume_path, str) else None,
        next_step=next_step if isinstance(next_step, str) and next_step else NEXT_STEP_RESUME_EMS,
        updated_at=_optional_transition_str(data, "updated_at") or values["created_at"],
        failed_stage=failed_stage,
        resume_stage=resume_stage,
        error_code=error_code,
        error_message=error_message,
        admin_update_claimed_at=admin_update_claimed_at,
        admin_alignment_required=admin_alignment_required,
        resources_claimed_at=resources_claimed_at,
        request_fingerprint=request_fingerprint,
        development_risk_acknowledged=development_risk_acknowledged,
        development_risk_acknowledged_for_tag=development_risk_acknowledged_for_tag,
        compatibility_mode=compatibility_mode,
        resource_strategy=resource_strategy,
        orchestrator_build_id=orchestrator["build_id"] if orchestrator else None,
        orchestrator_revision=orchestrator["revision"] if orchestrator else None,
        orchestrator_admin_image=orchestrator["image"] if orchestrator else None,
        orchestrator_admin_digest=orchestrator["digest"] if orchestrator else None,
    )


def _transition_is_expired(record: TransitionRecord, now) -> bool:
    expires = _parse_iso(record.expires_at)
    if expires is None:
        return True  # an unparseable expiry is treated as already expired
    return now >= expires


def _running_admin_matches(record: TransitionRecord, running_admin) -> None:
    """Raise unless the running Admin identity matches the record's target.

    Catches a tampered ``admin_digest``/``build_id``/``revision`` and a wrong
    running Admin build in one place. All three immutable dimensions are
    required: accepting revision alone is unsafe because retried workflows may
    produce more than one artifact from the same checkout.
    """

    running = running_admin or {}
    # Match the *orchestrator* Admin — the Admin that actually runs. For a modern
    # paired build this is the selected Admin; for a legacy release it is the
    # running modern Admin, never the historical Admin image that is never run.
    orchestrator = record.orchestrator_admin
    checks = (
        ("digest", orchestrator["digest"], running.get("digest")),
        ("build_id", orchestrator["build_id"], running.get("build_id")),
        ("revision", orchestrator["revision"], running.get("revision")),
    )
    if any(not actual for _name, _expected, actual in checks):
        raise TransitionStateError(
            "admin_unverifiable",
            "running Admin build could not be verified against the transition target",
        )
    if any(
        str(expected).strip() != str(actual).strip()
        for _name, expected, actual in checks
    ):
        raise TransitionStateError(
            "admin_identity_mismatch",
            "running Admin build does not match the transition target",
        )


def validate_transition_for_resume(record: TransitionRecord, *, now, running_admin) -> TransitionRecord:
    """Raise :class:`TransitionStateError` unless ``record`` may resume now."""

    if record.stage in TERMINAL_TRANSITION_STAGES:
        raise TransitionStateError(
            "not_resumable", f"transition is {record.stage}; it cannot resume"
        )
    if _transition_is_expired(record, now):
        raise TransitionStateError("expired", "the pending transition has expired")
    _running_admin_matches(record, running_admin)
    return record


def make_transition_record(
    *,
    mode,
    system_tag,
    build_id,
    revision,
    admin_image,
    admin_digest,
    ems_image,
    ems_digest,
    operation_id=None,
    stage=TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
    resume_path=None,
    next_step=NEXT_STEP_RESUME_EMS,
    updated_at=None,
    failed_stage=None,
    resume_stage=None,
    error_code=None,
    error_message=None,
    admin_update_claimed_at=None,
    admin_alignment_required=None,
    resources_claimed_at=None,
    request_fingerprint=None,
    development_risk_acknowledged=False,
    development_risk_acknowledged_for_tag=None,
    compatibility_mode=None,
    resource_strategy=None,
    orchestrator_admin=None,
    ttl_seconds=DEFAULT_TRANSITION_TTL_SECONDS,
    now=None,
) -> TransitionRecord:
    """Build a validated, TTL-stamped transition record.

    Raises :class:`TransitionStateError` (``unsupported_mode``/``state_malformed``/
    ``state_tampered``) for inconsistent inputs so a bad record is never persisted.
    """

    now = now or _now_utc()
    try:
        ttl = int(ttl_seconds)
    except (TypeError, ValueError):
        ttl = DEFAULT_TRANSITION_TTL_SECONDS
    if ttl <= 0:
        ttl = DEFAULT_TRANSITION_TTL_SECONDS
    created = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    expires = (now.astimezone(timezone.utc) + timedelta(seconds=ttl)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    candidate = {
        "state_version": TRANSITION_SCHEMA_VERSION,
        "operation_id": operation_id or uuid.uuid4().hex,
        "mode": mode,
        "stage": stage,
        "system_tag": system_tag,
        "build_id": build_id,
        "revision": revision,
        "admin_image": admin_image,
        "admin_digest": admin_digest,
        "ems_image": ems_image,
        "ems_digest": ems_digest,
        "created_at": created,
        "expires_at": expires,
        "resume_path": resume_path,
        "next_step": next_step,
        "updated_at": updated_at or created,
        "failed_stage": failed_stage,
        "resume_stage": resume_stage,
        "error_code": error_code,
        "error_message": error_message,
        "admin_update_claimed_at": admin_update_claimed_at,
        "admin_alignment_required": admin_alignment_required,
        "resources_claimed_at": resources_claimed_at,
        "request_fingerprint": request_fingerprint,
        "development_risk_acknowledged": development_risk_acknowledged,
        "development_risk_acknowledged_for_tag": (
            development_risk_acknowledged_for_tag
        ),
        "compatibility_mode": compatibility_mode,
        "resource_strategy": resource_strategy,
        "orchestrator_admin": orchestrator_admin,
    }
    # Reuse the structural validator so make/parse stay consistent.
    return parse_transition_record(candidate)


class PendingTransitionStore:
    """Atomic store and legal state-machine guard for one system operation."""

    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / PENDING_TRANSITION_FILE
        self.lock_path = self.state_dir / ".pending-transition.lock"
        self._lock = threading.Lock()

    @contextmanager
    def _locked(self):
        """Serialize read/modify/write cycles across threads and processes."""

        with self._lock:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with open(self.lock_path, "a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_raw(self):
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise TransitionStateError(
                "state_unreadable", f"the transition state could not be read: {exc}"
            )
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise TransitionStateError(
                "state_malformed", "the transition state file is corrupt"
            )
        return data

    def _write_raw(self, data):
        payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".pending-transition.", suffix=".tmp", dir=self.state_dir
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

    def read(self) -> TransitionRecord | None:
        raw = self._read_raw()
        if raw is None:
            return None
        return parse_transition_record(raw)

    @staticmethod
    def _updated_at(now) -> str:
        current = now or _now_utc()
        return current.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _record_locked(self, operation_id=None) -> TransitionRecord:
        raw = self._read_raw()
        if raw is None:
            raise TransitionStateError(
                "no_transition", "there is no pending transition"
            )
        record = parse_transition_record(raw)
        if operation_id is not None and record.operation_id != operation_id:
            raise TransitionStateError(
                "operation_mismatch", "the transition operation id does not match"
            )
        return record

    def _write_record_locked(self, record) -> TransitionRecord:
        validated = parse_transition_record(record.as_dict())
        self._write_raw(validated.as_dict())
        return validated

    @staticmethod
    def _require_mutable(record):
        if record.stage in TERMINAL_TRANSITION_STAGES:
            raise TransitionStateError(
                "not_resumable", f"transition is {record.stage}; it cannot restart"
            )

    @staticmethod
    def _require_fresh(record, now):
        current_time = now or _now_utc()
        if _transition_is_expired(record, current_time):
            raise TransitionStateError(
                "expired", "the pending transition has expired"
            )

    def assert_fresh(self, operation_id, *, now=None) -> TransitionRecord:
        """Return the current record only when new mutation may still begin."""

        with self._locked():
            record = self._record_locked(operation_id)
            self._require_mutable(record)
            self._require_fresh(record, now)
            return record

    def begin(self, record: TransitionRecord, *, now=None) -> TransitionRecord:
        """Persist a validated operation, refusing to clobber any active state."""

        validated = parse_transition_record(record.as_dict())
        with self._locked():
            existing = self._read_raw()
            if existing is not None:
                current = parse_transition_record(existing)
                if current.stage not in TERMINAL_TRANSITION_STAGES:
                    raise TransitionStateError(
                        "transition_active",
                        "another system-build transition is already in progress",
                    )
            self._write_raw(validated.as_dict())
        return validated

    def advance(self, operation_id, *, expected_stage, new_stage, now=None) -> TransitionRecord:
        """Commit one legal stage edge; repeating an already-committed edge is safe."""

        if expected_stage not in VALID_TRANSITION_STAGES or new_stage not in VALID_TRANSITION_STAGES:
            raise TransitionStateError("invalid_stage", "transition stage is invalid")
        with self._locked():
            record = self._record_locked(operation_id)
            self._require_mutable(record)
            if new_stage not in _TRANSITION_EDGES.get(expected_stage, set()):
                raise TransitionStateError(
                    "invalid_transition",
                    f"transition edge {expected_stage} -> {new_stage} is invalid",
                )
            if record.stage == new_stage:
                return record
            if record.stage != expected_stage:
                raise TransitionStateError(
                    "invalid_transition",
                    f"transition cannot advance from {record.stage} to {new_stage}",
                )
            advanced = replace(
                record,
                stage=new_stage,
                updated_at=self._updated_at(now),
            )
            return self._write_record_locked(advanced)

    def claim(self, operation_id, *, expected_stage, new_stage, now=None) -> bool:
        """Atomically claim a mutating stage; return ``False`` to duplicate claimers."""

        with self._locked():
            record = self._record_locked(operation_id)
            self._require_mutable(record)
            if new_stage not in _TRANSITION_EDGES.get(expected_stage, set()):
                raise TransitionStateError(
                    "invalid_transition",
                    f"transition edge {expected_stage} -> {new_stage} is invalid",
                )
            if record.stage == new_stage:
                return False
            self._require_fresh(record, now)
            if record.stage != expected_stage:
                raise TransitionStateError(
                    "invalid_transition",
                    f"transition cannot be claimed from {record.stage}",
                )
            claimed = replace(
                record,
                stage=new_stage,
                updated_at=self._updated_at(now),
            )
            self._write_record_locked(claimed)
            return True

    def claim_admin_update(self, operation_id, *, now=None) -> bool:
        """Atomically claim the one v2 Admin updater execution.

        The transition already waits at ``admin_reconnect_pending`` before the
        sidecar starts. A separate durable claim distinguishes the one updater
        allowed to pull/rewrite/recreate from duplicate sidecars or CLI replay.
        """

        with self._locked():
            record = self._record_locked(operation_id)
            self._require_mutable(record)
            self._require_fresh(record, now)
            if record.stage != TRANSITION_STAGE_ADMIN_RECONNECT_PENDING:
                raise TransitionStateError(
                    "admin_update_not_runnable",
                    f"Admin update cannot run while transition is {record.stage}",
                )
            if record.admin_update_claimed_at:
                return False
            claimed = replace(
                record,
                admin_update_claimed_at=self._updated_at(now),
                updated_at=self._updated_at(now),
            )
            self._write_record_locked(claimed)
            return True

    def claim_resource_verification(self, operation_id, *, now=None) -> bool:
        """Claim the embedded-resource import before its filesystem mutation."""

        with self._locked():
            record = self._record_locked(operation_id)
            self._require_mutable(record)
            self._require_fresh(record, now)
            if record.stage == TRANSITION_STAGE_RESOURCES_VERIFIED:
                return False
            if record.stage != TRANSITION_STAGE_ADMIN_ALIGNED:
                raise TransitionStateError(
                    "invalid_transition",
                    f"resources cannot be claimed while transition is {record.stage}",
                )
            if record.resources_claimed_at:
                return False
            claimed = replace(
                record,
                resources_claimed_at=self._updated_at(now),
                updated_at=self._updated_at(now),
            )
            self._write_record_locked(claimed)
            return True

    def resume_after_admin_reconnect(
        self, operation_id, *, running_admin, now=None
    ) -> TransitionRecord:
        """Verify the replacement Admin and stop at ``admin_aligned``.

        Repeated reconnect polling after this edge is idempotent. It never imports
        resources or consumes/finalizes the surrounding system operation.
        """

        current_time = now or _now_utc()
        with self._locked():
            record = self._record_locked(operation_id)
            self._require_mutable(record)
            if _transition_is_expired(record, current_time):
                raise TransitionStateError("expired", "the pending transition has expired")
            _running_admin_matches(record, running_admin)
            if record.stage == TRANSITION_STAGE_ADMIN_ALIGNED:
                return record
            if record.stage != TRANSITION_STAGE_ADMIN_RECONNECT_PENDING:
                raise TransitionStateError(
                    "invalid_transition",
                    f"Admin reconnect cannot resume transition at {record.stage}",
                )
            aligned = replace(
                record,
                stage=TRANSITION_STAGE_ADMIN_ALIGNED,
                updated_at=self._updated_at(current_time),
            )
            return self._write_record_locked(aligned)

    def mark_failed(
        self,
        operation_id,
        *,
        error_code,
        error_message,
        resume_stage,
        now=None,
    ) -> TransitionRecord:
        """Persist a recoverable failure and the exact safe retry stage."""

        code = str(error_code or "").strip()
        message = str(error_message or "").strip()
        if not code or not message:
            raise TransitionStateError(
                "state_malformed", "recoverable failure requires code and message"
            )
        with self._locked():
            record = self._record_locked(operation_id)
            self._require_mutable(record)
            if record.stage == TRANSITION_STAGE_FAILED_RECOVERABLE:
                if (
                    record.error_code == code
                    and record.error_message == message
                    and record.resume_stage == resume_stage
                ):
                    return record
                raise TransitionStateError(
                    "invalid_transition", "transition already has a recoverable failure"
                )
            allowed_resume = _RECOVERY_STAGES_BY_FAILED_STAGE.get(record.stage, set())
            if resume_stage not in allowed_resume:
                raise TransitionStateError(
                    "invalid_transition",
                    f"{record.stage} cannot recover from {resume_stage}",
                )
            failed = replace(
                record,
                stage=TRANSITION_STAGE_FAILED_RECOVERABLE,
                updated_at=self._updated_at(now),
                failed_stage=record.stage,
                resume_stage=resume_stage,
                error_code=code,
                error_message=message,
            )
            return self._write_record_locked(failed)

    def retry(self, operation_id, *, now=None) -> TransitionRecord:
        """Explicitly leave recoverable failure at its recorded safe stage."""

        with self._locked():
            record = self._record_locked(operation_id)
            self._require_mutable(record)
            self._require_fresh(record, now)
            if record.stage != TRANSITION_STAGE_FAILED_RECOVERABLE:
                raise TransitionStateError(
                    "invalid_transition", "only a recoverable failure can be retried"
                )
            retried = replace(
                record,
                stage=record.resume_stage,
                updated_at=self._updated_at(now),
                failed_stage=None,
                resume_stage=None,
                error_code=None,
                error_message=None,
                admin_update_claimed_at=(
                    None
                    if record.resume_stage == TRANSITION_STAGE_ADMIN_UPDATE_PENDING
                    else record.admin_update_claimed_at
                ),
                resources_claimed_at=(
                    None
                    if record.resume_stage == TRANSITION_STAGE_ADMIN_ALIGNED
                    else record.resources_claimed_at
                ),
            )
            return self._write_record_locked(retried)

    def consume_for_resume(
        self, *, running_admin, now=None, operation_id=None
    ) -> TransitionRecord:
        """Compatibility name for the non-consuming staged reconnect operation."""

        if operation_id is None:
            current = self.read()
            if current is None:
                raise TransitionStateError(
                    "no_transition", "there is no pending transition to resume"
                )
            operation_id = current.operation_id
        return self.resume_after_admin_reconnect(
            operation_id, running_admin=running_admin, now=now
        )

    def cancel(self, *, operation_id=None, now=None) -> TransitionRecord | None:
        """Mark the current transition cancelled (terminal, not resumable).

        A fresh transition may only be cancelled outside externally-mutating
        stages — including a claimed resource verification, whose visible stage
        is still the cancellable ``admin_aligned``. An expired one may be
        cancelled from any non-terminal stage: expiry already refuses every
        forward path, so without cancel the record would wedge the store
        permanently (``begin`` never replaces a non-terminal record).
        """

        with self._locked():
            raw = self._read_raw()
            if raw is None:
                return None
            record = parse_transition_record(raw)
            if operation_id is not None and record.operation_id != operation_id:
                raise TransitionStateError(
                    "operation_mismatch", "the transition operation id does not match"
                )
            if record.stage == TRANSITION_STAGE_CANCELLED:
                return record
            self._require_mutable(record)
            if not _transition_is_expired(record, now or _now_utc()):
                if record.stage not in CANCELLABLE_TRANSITION_STAGES:
                    raise TransitionStateError(
                        "mutation_in_progress",
                        f"transition cannot be cancelled while {record.stage} is running",
                    )
                if transition_resource_verification_active(record):
                    raise TransitionStateError(
                        "mutation_in_progress",
                        "transition cannot be cancelled while System Build "
                        "resources are being prepared",
                    )
            cancelled = replace(
                record,
                stage=TRANSITION_STAGE_CANCELLED,
                updated_at=self._updated_at(now),
                failed_stage=None,
                resume_stage=None,
                error_code=None,
                error_message=None,
            )
            return self._write_record_locked(cancelled)

    def clear_locked(self):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def clear(self) -> None:
        with self._locked():
            self.clear_locked()


class AdminUpdateService:
    """Plan/execute/resume Admin Console self-updates behind the HTTP API.

    Read-only status and planning are always safe; execution only writes the
    pending record and hands the disruptive pull/recreate to an out-of-request
    worker (``admin.update_apply``). Injectable ``docker``/``store``/
    ``worker_launcher`` keep it fully testable without a real daemon.
    """

    def __init__(
        self,
        *,
        docker=None,
        release_manager=None,
        store=None,
        state_dir=None,
        environ=None,
        worker_launcher=None,
    ):
        self._docker = docker
        self._release_manager = release_manager
        self._environ = environ
        if store is not None:
            self._store = store
        else:
            resolved_state_dir = state_dir or self._default_state_dir()
            self._store = PendingAdminUpdateStore(resolved_state_dir)
        self._worker_launcher = worker_launcher or self._default_launch

    # --- helpers ---------------------------------------------------------

    def _env(self):
        return os.environ if self._environ is None else self._environ

    def _default_state_dir(self):
        data_dir = getattr(self._release_manager, "data_dir", None)
        if data_dir is not None:
            return Path(data_dir) / "state"
        from admin.releases import default_admin_data_dir

        return default_admin_data_dir() / "state"

    def _docker_supported(self):
        if self._docker is None:
            return False
        probe = getattr(self._docker, "probe", None)
        if not callable(probe):
            # No probe available (a bare test double): assume usable.
            return True
        try:
            return probe().get("state") == "ready"
        except Exception:
            return False

    def _current_identity(self):
        return detect_current_admin_identity(docker=self._docker, environ=self._env())

    def _validate_target_release(self, target_release):
        """Return the trimmed tag or raise ``ValueError`` for an unsafe target.

        Syntax is always enforced. When a release catalogue is available it is
        authoritative: an unknown or non-selectable tag is rejected. A transient
        catalogue failure falls back to syntax-only so a GitHub outage never
        blocks a legitimate update.
        """

        tag = validate_release_tag(target_release)
        if self._release_manager is None:
            return tag
        list_releases = getattr(self._release_manager, "list_releases", None)
        if not callable(list_releases):
            return tag
        try:
            listing = list_releases(for_upgrade=True)
            releases = listing.get("releases") if isinstance(listing, dict) else None
        except Exception:
            return tag  # catalogue unavailable: rely on syntax + digest decision
        if not releases:
            return tag
        match = next((item for item in releases if item.get("tag") == tag), None)
        if match is None:
            raise ValueError(f"release tag is not an available release: {tag}")
        if match.get("selectable") is False:
            raise ValueError(f"release tag is not a selectable release: {tag}")
        return tag

    def _ems_target_view(self, release_tag):
        try:
            image_ref = target_ems_image_for_release(release_tag)
        except ValueError:
            return {"image_ref": None, "digest": None}
        digest = None
        if self._docker is not None:
            digest = identify_image(self._docker, image_ref).digest
        return {"image_ref": image_ref, "digest": digest}

    # --- read APIs -------------------------------------------------------

    def status(self):
        if not self._docker_supported():
            return {
                "ok": True,
                "supported": False,
                "reason": "docker_unavailable",
                "message": "Admin update requires Docker access.",
                "current_admin": None,
                "pending": None,
            }
        current, source = self._current_identity()
        try:
            pending = self._reconcile_pending(self._store.read())
        except PendingUpdateStateError as exc:
            return {
                "ok": False,
                "supported": True,
                "error": exc.reason,
                "message": exc.message,
                "current_admin": {**_identity_view(current), "source": source},
                "pending": None,
            }
        return {
            "ok": True,
            "supported": True,
            "current_admin": {**_identity_view(current), "source": source},
            "pending": pending,
        }

    def resume(self):
        try:
            pending = self._reconcile_pending(self._store.read())
        except PendingUpdateStateError as exc:
            return {
                "ok": False,
                "error": exc.reason,
                "message": exc.message,
                "pending": None,
                "resume_available": False,
            }
        if pending is None:
            return {"ok": True, "pending": None, "resume_available": False}
        stage = pending.get("stage")
        return {
            "ok": True,
            "pending": pending,
            "resume_available": stage in RESUMABLE_STAGES,
            "next_step": pending.get("next_step", NEXT_STEP_RESUME_EMS),
        }

    # --- EMS-upgrade gate ------------------------------------------------

    def ems_upgrade_allowed(self, target_release):
        """Server-side compatibility signal for the EMS upgrade of ``target_release``.

        Admin image/version status is advisory: a mismatched, unknown, or
        self-update-incapable Admin returns ``allowed: True`` with a
        ``severity: "warning"`` so a local/dev Admin build can still upgrade EMS.
        The only hard block (``allowed: False``) is a genuinely in-flight Admin
        self-update, which must not run concurrently with an EMS upgrade. Never
        raises.
        """

        try:
            tag = validate_release_tag(target_release)
        except ValueError as exc:
            return {"allowed": False, "error": "invalid_release", "message": str(exc)}

        # A pending update for THIS release settles the signal directly.
        try:
            pending = self._reconcile_pending(self._store.read())
        except PendingUpdateStateError:
            pending = None
        if isinstance(pending, dict) and pending.get("target_release") == tag:
            if pending.get("update_required") is False:
                return {"allowed": True, "severity": SEVERITY_OK, "reason": GATE_NOT_REQUIRED}
            if pending.get("stage") in RESUMABLE_STAGES:
                return {"allowed": True, "severity": SEVERITY_OK, "reason": GATE_COMPLETED}
            if pending.get("stage") == STAGE_STARTED:
                # An Admin self-update is actively running: block concurrency.
                return {
                    "allowed": False,
                    "error": GATE_IN_PROGRESS,
                    "reason": GATE_IN_PROGRESS,
                    "message": _EMS_IN_PROGRESS_MESSAGE,
                    "plan": _plan_summary(pending),
                }
            # planned / failed for this release: recommend, do not block.
            return {
                "allowed": True,
                "severity": SEVERITY_WARNING,
                "reason": GATE_RECOMMENDED,
                "message": _EMS_WARN_RECOMMENDED,
                "plan": _plan_summary(pending),
            }

        # No settled pending state for this release: decide from image identity.
        if not self._docker_supported():
            return {
                "allowed": True,
                "severity": SEVERITY_WARNING,
                "reason": GATE_UNAVAILABLE,
                "message": _EMS_WARN_UNAVAILABLE,
            }
        try:
            target = resolve_admin_image_target(tag, docker=self._docker)
            current, _source = self._current_identity()
        except Exception:
            return {
                "allowed": True,
                "severity": SEVERITY_WARNING,
                "reason": GATE_UNAVAILABLE,
                "message": _EMS_WARN_UNAVAILABLE,
            }
        decision = decide_admin_update(
            current,
            ImageIdentity(
                image_ref=target.image_ref,
                digest=target.digest,
                revision=target.revision,
                build_serial=target.build_serial,
            ),
        )
        if not decision.update_required:
            return {"allowed": True, "severity": SEVERITY_OK, "reason": GATE_NOT_REQUIRED}
        # update_required (including the uncertain digest/current-unknown cases):
        # recommend an Admin update but let the EMS upgrade proceed.
        return {
            "allowed": True,
            "severity": SEVERITY_WARNING,
            "reason": GATE_RECOMMENDED,
            "message": _EMS_WARN_RECOMMENDED,
            "plan": decision.as_dict(),
        }

    # --- planning --------------------------------------------------------

    def plan(self, target_release):
        """Create a pending plan for ``target_release`` and return its summary.

        Raises ``ValueError`` for an invalid/absent/unknown release tag (the
        caller maps that to a 400). The tag is validated against the release
        catalogue when available; the target image is always derived server-side.
        """

        target_release = self._validate_target_release(target_release)
        target = resolve_admin_image_target(target_release, docker=self._docker)
        current, source = self._current_identity()
        target_identity = ImageIdentity(
            image_ref=target.image_ref,
            digest=target.digest,
            revision=target.revision,
            build_serial=target.build_serial,
        )
        decision = decide_admin_update(current, target_identity)

        plan_id = uuid.uuid4().hex
        now = utc_now_iso()
        state = {
            "schema_version": PENDING_SCHEMA_VERSION,
            "id": plan_id,
            "stage": STAGE_PLANNED,
            "created_at": now,
            "updated_at": now,
            "target_release": str(target_release).strip(),
            "current_admin": {**_identity_view(current), "source": source},
            "target_admin": target.as_dict(),
            "target_ems": self._ems_target_view(target_release),
            "update_required": decision.update_required,
            "reason": decision.reason,
            "warning": decision.warning,
            "next_step": NEXT_STEP_RESUME_EMS,
            "message": "Admin update planned.",
        }
        self._store.write(state)
        return {
            "ok": True,
            "plan_id": plan_id,
            "target_release": state["target_release"],
            "current_admin": state["current_admin"],
            "target_admin": state["target_admin"],
            "update_required": decision.update_required,
            "reason": decision.reason,
            "warning": decision.warning,
        }

    # --- execution -------------------------------------------------------

    def execute(self, plan_id, confirm):
        """Mark the plan started and launch the out-of-request updater.

        Returns before any image is pulled or container replaced: the browser
        gets a reconnect response and polls auth/resume while the Admin restarts.
        """

        if confirm is not True:
            return {
                "ok": False,
                "error": "confirm_required",
                "message": "Explicit confirmation is required to update the Admin Console.",
            }
        plan_id = _clean(plan_id)
        if not plan_id:
            return {
                "ok": False,
                "error": "plan_required",
                "message": "A plan id is required.",
            }
        try:
            pending = self._store.read()
        except PendingUpdateStateError as exc:
            return {"ok": False, "error": exc.reason, "message": exc.message}
        if pending is None or pending.get("id") != plan_id:
            return {
                "ok": False,
                "error": "unknown_plan",
                "message": "No matching Admin update plan was found. Plan again.",
            }
        # A no-op plan (image unchanged for this release) must not launch the
        # updater, even if the button was bypassed and confirm was posted.
        if pending.get("update_required") is False:
            return {
                "ok": False,
                "error": "admin_update_not_required",
                "message": "Admin Console image is unchanged for this release.",
            }

        pending["stage"] = STAGE_STARTED
        pending["updated_at"] = utc_now_iso()
        pending["message"] = "Admin Console update started."
        self._store.write(pending)

        try:
            self._worker_launcher(plan_id)
        except Exception as exc:  # updater could not even start; keep old Admin
            pending["stage"] = STAGE_FAILED
            pending["updated_at"] = utc_now_iso()
            pending["message"] = f"Admin Console update could not start: {exc}"
            self._store.write(pending)
            return {
                "ok": False,
                "error": "updater_start_failed",
                "message": "The Admin Console updater could not be started. "
                "The current Admin Console is still running.",
            }

        return {
            "ok": True,
            "status": STAGE_STARTED,
            "reconnect": True,
            "poll_url": "/api/admin/auth/status",
            "resume_url": "/api/admin/maintenance/admin-update/resume",
            "message": "Admin Console update started. This page will reconnect "
            "automatically.",
        }

    # --- reconcile / launch ---------------------------------------------

    def _reconcile_pending(self, pending):
        """Promote a started update to succeeded once the new Admin is running.

        Success is proven by the running image matching the plan target (by
        digest when known, else image ref) — never by tag name alone.
        """

        if not isinstance(pending, dict) or pending.get("stage") != STAGE_STARTED:
            return pending
        current, source = self._current_identity()
        target = pending.get("target_admin") or {}
        target_digest = target.get("digest")
        target_ref = target.get("image_ref")
        matched = False
        if target_digest and current.digest and target_digest == current.digest:
            matched = True
        elif target_ref and current.image_ref and target_ref == current.image_ref:
            matched = True
        if not matched:
            return pending
        pending["stage"] = STAGE_SUCCEEDED
        pending["updated_at"] = utc_now_iso()
        pending["message"] = "Admin Console updated."
        pending["current_admin"] = {**_identity_view(current), "source": source}
        try:
            self._store.write(pending)
        except OSError:
            pass
        return pending

    def _default_launch(self, plan_id):
        """Launch the updater out of the request, preferring a sidecar container."""

        AdminUpdateLauncher(
            store=self._store,
            docker=self._docker,
            release_manager=self._release_manager,
            environ=self._env(),
        ).launch(plan_id)


def _flag(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _safe_container_suffix(plan_id) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(plan_id))[:64] or "unknown"


def _positive_id(value):
    """Return an all-digit, strictly-positive id, else ``None``.

    Mirrors the installer's ``is_positive_id`` shell check so the updater refuses
    to run the sidecar as root or with a smuggled/malformed id — a non-root
    numeric PUID/PGID is required, exactly like the normal Admin container.
    """

    text = str(value or "").strip()
    if not text or not text.isdigit():
        return None
    number = int(text)
    return str(number) if number > 0 else None


def _nonnegative_id(value):
    """Return an all-digit, non-negative id, else ``None`` (socket group)."""

    text = str(value or "").strip()
    if not text or not text.isdigit():
        return None
    return str(int(text))


def _resolve_host_ids(env):
    """Return ``(PUID, PGID)`` validated strictly-positive, or raise ``RuntimeError``.

    The values come from the same bootstrap env the normal Admin container uses
    (``.env.admin`` -> ``PUID``/``PGID``). Missing or invalid metadata fails
    before the container is ever launched rather than silently defaulting.
    """

    puid = _positive_id(env.get("PUID"))
    if puid is None:
        raise RuntimeError(
            "PUID must be a non-root numeric id for the Admin update sidecar"
        )
    pgid = _positive_id(env.get("PGID"))
    if pgid is None:
        raise RuntimeError(
            "PGID must be a non-root numeric id for the Admin update sidecar"
        )
    return puid, pgid


def _resolve_docker_socket_gid(env, socket_stat, *, socket_path=DOCKER_SOCKET_PATH):
    """Return the Docker socket group id for ``--group-add``.

    Prefers the bootstrap ``DOCKER_GID`` (installer-discovered), else stats the
    fixed Docker socket path — the very source the installer used. Raises
    ``RuntimeError`` when neither yields a usable group.
    """

    from_env = _nonnegative_id(env.get("DOCKER_GID"))
    if from_env is not None:
        return from_env
    try:
        gid = socket_stat(socket_path).st_gid
    except OSError as exc:
        raise RuntimeError(
            f"Docker socket group could not be determined from {socket_path}: {exc}"
        ) from exc
    gid = _nonnegative_id(gid)
    if gid is None:
        raise RuntimeError(
            f"Docker socket group at {socket_path} is not a valid group id"
        )
    return gid


class AdminUpdateLauncher:
    """Start the out-of-request Admin updater, preferring a sidecar container.

    The default runs a short-lived ``docker run --rm`` sidecar (detached) that
    survives the Admin recreate, so the process running ``docker compose up`` is
    never the container being replaced. The target image comes from the pending
    state (created server-side), never the browser. The same-process thread
    worker is only used when explicitly enabled via
    ``EMS_ADMIN_UPDATE_LOCAL_WORKER=1`` (dev/tests), where recreating the running
    container is acceptable.
    """

    def __init__(self, *, store, docker=None, release_manager=None,
                 environ=None, run=None, socket_stat=None):
        self._store = store
        self._docker = docker
        self._release_manager = release_manager
        self._environ = environ
        self._run = run or subprocess.run
        self._socket_stat = socket_stat or os.stat

    def _env(self):
        return os.environ if self._environ is None else self._environ

    def launch(self, plan_id):
        if _flag(self._env().get("EMS_ADMIN_UPDATE_LOCAL_WORKER")):
            self._launch_local(plan_id)
            return
        self._launch_sidecar(plan_id)

    def _pending_target_ref(self, plan_id):
        pending = self._store.read()
        if not isinstance(pending, dict) or pending.get("id") != plan_id:
            raise ValueError("no matching pending Admin update plan")
        target = pending.get("target_admin") or {}
        image_ref = _clean(target.get("image_ref"))
        if not image_ref:
            # Derive from the trusted release tag as a fallback (never the browser).
            image_ref = target_admin_image_for_release(pending.get("target_release"))
        return image_ref

    def _current_admin_image(self, env):
        """The running Admin image ref; the sidecar runs from THIS build.

        Running the updater from the current Admin build (not the target tag)
        guarantees it understands the pending-state format it was handed, and
        avoids ever executing a stale locally-cached target tag before it is
        pulled/verified.
        """

        ref = admin_image_ref_from_env(env)
        if not ref:
            raise RuntimeError(
                "Current Admin image could not be determined for the update sidecar"
            )
        return ref

    def build_sidecar_argv(self, plan_id):
        """Build the ``docker run`` argv for the updater sidecar (no shell).

        The sidecar runs from the *current* Admin image with the same effective
        host permissions as the normal Admin container (``--user PUID:PGID`` and
        ``--group-add DOCKER_GID``), so it can write the host compose/env, the
        pending state, and drive the Docker socket to recreate the Admin service.
        """

        env = self._env()
        base_image = self._current_admin_image(env)
        install_root = self._install_root(env)
        admin_data_dir = self._admin_data_dir(env)
        # The bind mounts use same-path semantics, so the Docker daemon needs
        # host-valid absolute paths; a relative path would produce an unsafe or
        # invalid -v argument.
        if not install_root.is_absolute():
            raise RuntimeError(
                "EMS_INSTALL_DIR must be an absolute host path for Admin update"
            )
        if not admin_data_dir.is_absolute():
            raise RuntimeError(
                "EMS_ADMIN_DATA_DIR must be an absolute host path for Admin update"
            )
        puid, pgid = _resolve_host_ids(env)
        docker_gid = _resolve_docker_socket_gid(env, self._socket_stat)
        compose_file = _clean(env.get("EMS_ADMIN_COMPOSE_FILE")) or str(
            install_root / DEFAULT_ADMIN_COMPOSE_FILE
        )
        service = _clean(env.get("EMS_ADMIN_COMPOSE_SERVICE")) or DEFAULT_ADMIN_COMPOSE_SERVICE
        container = _clean(env.get("EMS_ADMIN_CONTAINER_NAME")) or DEFAULT_ADMIN_CONTAINER
        return [
            "docker", "run", "--rm", "-d",
            "--user", f"{puid}:{pgid}",
            "--group-add", docker_gid,
            "--name", f"ems-admin-updater-{_safe_container_suffix(plan_id)}",
            "-v", f"{DOCKER_SOCKET_PATH}:{DOCKER_SOCKET_PATH}",
            "-v", f"{install_root}:{install_root}",
            "-v", f"{admin_data_dir}:{admin_data_dir}",
            "-e", f"EMS_INSTALL_DIR={install_root}",
            "-e", f"EMS_ADMIN_DATA_DIR={admin_data_dir}",
            "-e", f"EMS_ADMIN_COMPOSE_FILE={compose_file}",
            "-e", f"EMS_ADMIN_COMPOSE_SERVICE={service}",
            "-e", f"EMS_ADMIN_CONTAINER_NAME={container}",
            base_image,
            "python", "-m", "admin.update_apply",
            "--plan-id", str(plan_id), "--delay-seconds", "0",
        ]

    def _install_root(self, env):
        configured = _clean(env.get("EMS_INSTALL_DIR"))
        if configured:
            return Path(configured)
        from admin.install_context import detect_install_context

        return Path(detect_install_context().install_root)

    def _admin_data_dir(self, env):
        configured = _clean(env.get("EMS_ADMIN_DATA_DIR"))
        if configured:
            return Path(configured)
        data_dir = getattr(self._release_manager, "data_dir", None)
        if data_dir is not None:
            return Path(data_dir)
        from admin.releases import default_admin_data_dir

        return default_admin_data_dir()

    def _launch_sidecar(self, plan_id):
        # Validate the pending plan resolves to a target (never launch a blind
        # updater), then run the updater from the CURRENT Admin build.
        self._pending_target_ref(plan_id)  # raises if target missing
        argv = self.build_sidecar_argv(plan_id)
        try:
            result = self._run(argv, capture_output=True, text=True, timeout=60)
        except FileNotFoundError as exc:
            raise RuntimeError("the docker CLI is not available") from exc
        if getattr(result, "returncode", 0) != 0:
            raise RuntimeError(
                f"docker run failed: {getattr(result, 'stderr', '') or 'unknown error'}"
            )

    def _launch_local(self, plan_id):
        """Same-process daemon-thread worker (dev/tests only).

        Recreates the very container it runs in, which is why it is opt-in.
        """

        from admin import update_apply

        threading.Thread(
            target=update_apply.apply_admin_update,
            kwargs={
                "plan_id": plan_id,
                "store": self._store,
                "docker": self._docker,
                "environ": self._env(),
                "release_manager": self._release_manager,
            },
            daemon=True,
        ).start()


class SystemTransitionLauncher(AdminUpdateLauncher):
    """Launch the real Admin updater for a v2 :class:`TransitionRecord`.

    It deliberately reuses the hardened v1 sidecar construction (current image,
    non-root ids, fixed Docker socket and same-path host mounts) while selecting
    the v2 CLI entrypoint. The object is directly callable so it can be injected
    into :class:`admin.system_alignment.SystemAlignmentService`.
    """

    def __call__(self, record):
        self.launch(record)

    def _matching_transition(self, record):
        if not isinstance(record, TransitionRecord):
            raise ValueError("a System Build transition record is required")
        current = self._store.read()
        if current is None or current.operation_id != record.operation_id:
            raise ValueError("no matching System Build transition")
        if current.admin_image != record.admin_image:
            raise ValueError("transition Admin target does not match persisted state")
        return current

    def launch(self, record):
        self._matching_transition(record)
        if _flag(self._env().get("EMS_ADMIN_UPDATE_LOCAL_WORKER")):
            self._launch_transition_local(record)
            return
        self._launch_transition_sidecar(record)

    def build_transition_sidecar_argv(self, record):
        self._matching_transition(record)
        argv = super().build_sidecar_argv(record.operation_id)
        option = argv.index("--plan-id")
        argv[option] = "--transition-id"
        return argv

    def _launch_transition_sidecar(self, record):
        argv = self.build_transition_sidecar_argv(record)
        try:
            result = self._run(argv, capture_output=True, text=True, timeout=60)
        except FileNotFoundError as exc:
            raise RuntimeError("the docker CLI is not available") from exc
        if getattr(result, "returncode", 0) != 0:
            raise RuntimeError(
                f"docker run failed: {getattr(result, 'stderr', '') or 'unknown error'}"
            )

    def _launch_transition_local(self, record):
        from admin import update_apply

        threading.Thread(
            target=update_apply.apply_system_transition_admin_update,
            kwargs={
                "transition_id": record.operation_id,
                "store": self._store,
                "docker": self._docker,
                "environ": self._env(),
                "release_manager": self._release_manager,
            },
            daemon=True,
        ).start()
