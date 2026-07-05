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

import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from admin.image_identity import ImageIdentity, identify_image
from admin.models import utc_now_iso
from admin.releases import TAG_PATTERN

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

# Server-side EMS-upgrade gate outcomes.
GATE_NOT_REQUIRED = "admin_update_not_required"
GATE_COMPLETED = "admin_update_completed"
GATE_BLOCKED = "admin_update_required"
_EMS_BLOCK_MESSAGE = "Update the Admin Console before running this EMS upgrade."
_EMS_BLOCK_UNKNOWN = (
    "The Admin update status could not be determined. Update the Admin Console "
    "before running this EMS upgrade."
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
        """Server-side gate: may the EMS upgrade for ``target_release`` proceed?

        Frontend blocking is not enough — a direct authenticated POST must also
        be refused while a required Admin update for this release is not
        complete. Never raises and never proceeds on uncertainty.
        """

        try:
            tag = validate_release_tag(target_release)
        except ValueError as exc:
            return {"allowed": False, "error": "invalid_release", "message": str(exc)}

        # A pending update for THIS release settles the gate directly.
        try:
            pending = self._reconcile_pending(self._store.read())
        except PendingUpdateStateError:
            pending = None
        if isinstance(pending, dict) and pending.get("target_release") == tag:
            # A no-op plan (image unchanged for this release) never blocks.
            if pending.get("update_required") is False:
                return {"allowed": True, "reason": GATE_NOT_REQUIRED}
            if pending.get("stage") in RESUMABLE_STAGES:
                return {"allowed": True, "reason": GATE_COMPLETED}
            # planned / started / failed for this release: not done yet.
            return {
                "allowed": False,
                "error": GATE_BLOCKED,
                "message": _EMS_BLOCK_MESSAGE,
                "plan": _plan_summary(pending),
            }

        # No settled pending state for this release: decide from image identity.
        if not self._docker_supported():
            return {
                "allowed": False,
                "error": GATE_BLOCKED,
                "message": _EMS_BLOCK_UNKNOWN,
            }
        try:
            target = resolve_admin_image_target(tag, docker=self._docker)
            current, _source = self._current_identity()
        except Exception:
            return {
                "allowed": False,
                "error": GATE_BLOCKED,
                "message": _EMS_BLOCK_UNKNOWN,
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
            return {"allowed": True, "reason": GATE_NOT_REQUIRED}
        # update_required (including the uncertain digest/current-unknown cases)
        # blocks: do not silently proceed with a possibly-incompatible Admin.
        return {
            "allowed": False,
            "error": GATE_BLOCKED,
            "message": _EMS_BLOCK_MESSAGE,
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
                 environ=None, run=None):
        self._store = store
        self._docker = docker
        self._release_manager = release_manager
        self._environ = environ
        self._run = run or subprocess.run

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

    def build_sidecar_argv(self, plan_id, *, image_ref):
        """Build the ``docker run`` argv for the updater sidecar (no shell)."""

        env = self._env()
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
        compose_file = _clean(env.get("EMS_ADMIN_COMPOSE_FILE")) or str(
            install_root / DEFAULT_ADMIN_COMPOSE_FILE
        )
        service = _clean(env.get("EMS_ADMIN_COMPOSE_SERVICE")) or DEFAULT_ADMIN_COMPOSE_SERVICE
        container = _clean(env.get("EMS_ADMIN_CONTAINER_NAME")) or DEFAULT_ADMIN_CONTAINER
        return [
            "docker", "run", "--rm", "-d",
            "--name", f"ems-admin-updater-{_safe_container_suffix(plan_id)}",
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
            "-v", f"{install_root}:{install_root}",
            "-v", f"{admin_data_dir}:{admin_data_dir}",
            "-e", f"EMS_INSTALL_DIR={install_root}",
            "-e", f"EMS_ADMIN_DATA_DIR={admin_data_dir}",
            "-e", f"EMS_ADMIN_COMPOSE_FILE={compose_file}",
            "-e", f"EMS_ADMIN_COMPOSE_SERVICE={service}",
            "-e", f"EMS_ADMIN_CONTAINER_NAME={container}",
            image_ref,
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
        image_ref = self._pending_target_ref(plan_id)  # raises if target missing
        argv = self.build_sidecar_argv(plan_id, image_ref=image_ref)
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
