# SPDX-License-Identifier: AGPL-3.0-or-later
"""Apply a planned Admin Console self-update, out of the HTTP request.

Pulls the target Admin image, repoints the Admin compose/env tag, and recreates
only the Admin service. Runs as a sidecar container (default) or a local worker
(dev/tests). It only ever touches the Admin image/compose/service — never the EMS
container, config, or data. See ``docs/technical/admin-architecture.md``.
"""

import argparse
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from admin.admin_update import (
    DEFAULT_ADMIN_COMPOSE_FILE,
    DEFAULT_ADMIN_COMPOSE_SERVICE,
    NEXT_STEP_RESUME_EMS,
    STAGE_FAILED,
    STAGE_STARTED,
    STAGE_SUCCEEDED,
    PendingAdminUpdateStore,
    PendingTransitionStore,
    PendingUpdateStateError,
    TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
    TransitionStateError,
    target_admin_image_for_release,
)
from admin.image_identity import identify_image
from admin.models import utc_now_iso

# Give the HTTP response time to flush to the browser before the local worker
# starts the disruptive pull/recreate. The sidecar path passes 0 (the parent
# already responded).
DEFAULT_DELAY_SECONDS = 2.0

# One published Admin image ref in a compose file. The variable form
# (``${EMS_ADMIN_TAG...}``) is handled separately via the env file.
ADMIN_IMAGE_RE = re.compile(r"ghcr\.io/basecubedev/ems-solarflow-admin:[^\s\"'{}$]+")

# Cap the per-plan updater log so a stuck/retried updater cannot grow unbounded.
_MAX_LOG_BYTES = 64 * 1024


class AdminUpdateApplyError(Exception):
    """A failure while applying the Admin update (pull/compose/recreate)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _atomic_write_bytes(path, raw):
    """Restore ``raw`` bytes to ``path`` atomically (temp file + rename).

    Used only for rollback, so the restored file is exactly the original bytes
    (never a reserialized/normalized form).
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".admin-rollback.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


class ComposeEnvTransaction:
    """Byte-for-byte snapshot/rollback for the files an Admin update rewrites.

    Snapshots each path's raw bytes and whether it existed *before* any change.
    ``rollback`` restores existed files to their exact original bytes and removes
    files that did not exist before, so a failed pull/recreate/verify can never
    leave the persistent Admin compose/env pointing at a target that is not
    actually running. Rollback never raises: each failure is collected as a
    ``{"path", "error"}`` record (paths and OS error text only — no file
    contents, so no secret env values leak).
    """

    def __init__(self, paths):
        self._entries = []
        seen = set()
        for raw_path in paths:
            if raw_path is None:
                continue
            path = Path(raw_path)
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            existed = path.exists()
            data = path.read_bytes() if existed else None
            self._entries.append((path, existed, data))

    def rollback(self):
        failures = []
        for path, existed, data in self._entries:
            try:
                if existed:
                    _atomic_write_bytes(path, data)
                elif path.exists():
                    path.unlink()
            except OSError as exc:
                failures.append({"path": str(path), "error": str(exc)})
        return failures


class AdminComposeRunner:
    """Recreate only the Admin service from its own compose file.

    Uses ``docker compose -f <file> up -d --no-deps --force-recreate <service>``
    so bundled/EMS services referenced by the same project are never touched.
    Injectable ``run`` keeps it testable without a daemon.
    """

    def __init__(self, run=None):
        self._run = run or subprocess.run

    def recreate(self, compose_file, service):
        compose_file = Path(compose_file)
        argv = [
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            str(service),
        ]
        try:
            result = self._run(
                argv,
                cwd=str(compose_file.parent),
                capture_output=True,
                text=True,
                timeout=180,
            )
        except FileNotFoundError as exc:
            raise AdminUpdateApplyError(
                "docker_cli_missing", "The docker CLI is not available."
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise AdminUpdateApplyError(
                "recreate_failed", "Could not recreate the Admin Console container."
            ) from exc
        if result.returncode != 0:
            raise AdminUpdateApplyError(
                "recreate_failed",
                "Could not recreate the Admin Console container.",
            )


class _FileLogger:
    """Append-only, size-bounded plain-text log for one update run."""

    def __init__(self, path):
        self.path = Path(path)

    def write(self, message):
        line = f"{utc_now_iso()} {message}\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > _MAX_LOG_BYTES:
                # Bounded: drop the old log rather than grow without limit.
                self.path.write_text("", encoding="utf-8")
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass


def _write_env_tag(env_file, tag):
    """Set ``EMS_ADMIN_TAG=<tag>`` in ``env_file`` (create/replace the line)."""

    path = Path(env_file)
    lines = []
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
    replaced = False
    for index, line in enumerate(lines):
        if line.startswith("EMS_ADMIN_TAG="):
            lines[index] = f"EMS_ADMIN_TAG={tag}"
            replaced = True
            break
    if not replaced:
        lines.append(f"EMS_ADMIN_TAG={tag}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# A literal ``EMS_ADMIN_TAG: "value"`` line in the compose environment. Anchored
# to the line start so the ``${EMS_ADMIN_TAG:-latest}`` inside an image ref (a
# variable substitution, not a key) is never mistaken for it.
COMPOSE_ENV_TAG_RE = re.compile(r'(?m)^(\s*EMS_ADMIN_TAG:\s*")([^"${}]+)(")')


def _sync_compose_env_tag(text, target_tag):
    """Update a literal compose ``EMS_ADMIN_TAG`` value; return (text, changed)."""

    if not COMPOSE_ENV_TAG_RE.search(text):
        return text, False
    updated = COMPOSE_ENV_TAG_RE.sub(
        lambda m: f"{m.group(1)}{target_tag}{m.group(3)}", text
    )
    return updated, updated != text


def update_admin_image_reference(compose_file, target_ref, *, env_file=None):
    """Repoint the Admin image to ``target_ref`` and keep tag metadata in sync.

    Updates, where present: a literal ``image:`` ref, a literal compose
    ``EMS_ADMIN_TAG`` value, and the ``EMS_ADMIN_TAG`` line in the env file
    (``.env.admin``). A variable-driven image (``${EMS_ADMIN_TAG...}``) is
    repointed via the env file. Compose edits are backed up (``.bak``). Returns
    ``True`` when at least one reference was located, ``False`` otherwise.
    """

    compose_file = Path(compose_file)
    target_tag = target_ref.rsplit(":", 1)[-1]
    default_env = compose_file.parent / ".env.admin"
    located = False
    variable_driven = False

    try:
        original = compose_file.read_text(encoding="utf-8")
    except OSError:
        original = None

    if original is not None:
        text = original
        if ADMIN_IMAGE_RE.search(text):
            located = True
            text = ADMIN_IMAGE_RE.sub(lambda _m: target_ref, text)
        text, tag_synced = _sync_compose_env_tag(text, target_tag)
        located = located or tag_synced
        if "${EMS_ADMIN_TAG" in original:
            # Variable-driven image tag: resolved from the env file below.
            located = True
            variable_driven = True
        if text != original:
            compose_file.with_name(compose_file.name + ".bak").write_text(
                original, encoding="utf-8"
            )
            compose_file.write_text(text, encoding="utf-8")

    # Keep the recorded env tag correct. A variable-driven compose needs the env
    # file created when it is absent, or Compose keeps using the default tag and
    # nothing actually changes despite located=True.
    target_env = env_file or (default_env if default_env.exists() else None)
    if target_env is None and variable_driven:
        target_env = default_env
    if target_env is not None:
        _write_env_tag(target_env, target_tag)
        located = True

    return located


def _resolve_compose_file(environ):
    configured = (environ.get("EMS_ADMIN_COMPOSE_FILE") or "").strip()
    if configured:
        return Path(configured)
    # Fall back to the standard Admin compose next to the EMS install root.
    from admin.install_context import detect_install_context

    context = detect_install_context()
    return Path(context.install_root) / DEFAULT_ADMIN_COMPOSE_FILE


def _resolve_compose_service(environ):
    # The compose *service* name for ``docker compose up <service>`` — not the
    # container name (``EMS_ADMIN_CONTAINER_NAME``), which is inspect-only.
    return (
        environ.get("EMS_ADMIN_COMPOSE_SERVICE") or ""
    ).strip() or DEFAULT_ADMIN_COMPOSE_SERVICE


def _resolve_env_file(environ, compose_file):
    candidate = compose_file.parent / ".env.admin"
    return candidate if candidate.exists() else None


def _log_path(store, plan_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(plan_id))[:64] or "unknown"
    return Path(store.state_dir).parent / "logs" / f"admin-update-{safe}.log"


def _derive_target_ref(pending):
    target = pending.get("target_admin") or {}
    ref = target.get("image_ref")
    if ref:
        return ref
    release = pending.get("target_release")
    return target_admin_image_for_release(release)


class _SystemTransitionApplyStore:
    """Present a v2 transition through the narrow store shape used by v1 apply.

    The file/config/Docker transaction is shared unchanged. Stage writes are
    translated: updater failure becomes ``failed_recoverable``; updater success
    deliberately remains ``admin_reconnect_pending`` until the new Admin verifies
    its running identity through :class:`SystemAlignmentService`.
    """

    def __init__(self, store, transition_id):
        self._store = store
        self.transition_id = transition_id
        self.state_dir = store.state_dir

    def read(self):
        record = self._store.read()
        if (
            record is None
            or record.operation_id != self.transition_id
            or record.stage != "admin_reconnect_pending"
            or not record.admin_update_claimed_at
        ):
            return None
        return {
            "id": record.operation_id,
            "stage": STAGE_STARTED,
            "target_release": record.system_tag,
            "target_admin": {"image_ref": record.admin_image},
            "message": "Admin System Build alignment running.",
        }

    def write(self, pending):
        stage = pending.get("stage")
        if stage == STAGE_FAILED:
            self._store.mark_failed(
                self.transition_id,
                error_code=pending.get("error_code") or "admin_update_failed",
                error_message=pending.get("message") or "Admin update failed",
                resume_stage=TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
            )
        # STARTED/SUCCEEDED do not consume the reconnect stage.
        return pending


def apply_admin_update(
    plan_id,
    *,
    store=None,
    state_dir=None,
    docker=None,
    environ=None,
    release_manager=None,
    compose_recreate=None,
    verify=None,
    sleep=None,
    delay_seconds=DEFAULT_DELAY_SECONDS,
    log=None,
    expected_target_digest=None,
):
    """Pull, repoint, and recreate the Admin service for one pending plan.

    The target image is pulled *before* any persistent file changes, so a pull
    failure leaves compose/env untouched. The compose/env rewrite, recreate and
    optional ``verify`` then run inside a byte-for-byte transaction: any failure
    restores the original compose/env exactly (removing files that did not exist
    before), leaving the old Admin usable and reporting rollback failures
    explicitly in the result (paths only — no secret env contents). The recreate
    replaces this process in a real deployment; the new Admin resumes from the
    pending state.
    """

    environ = os.environ if environ is None else environ
    sleep = sleep or time.sleep
    if store is None:
        resolved_state_dir = state_dir or _default_state_dir(release_manager, environ)
        store = PendingAdminUpdateStore(resolved_state_dir)
    if docker is None:
        from admin.deployment import DockerCli

        docker = DockerCli()
    if compose_recreate is None:
        compose_recreate = AdminComposeRunner().recreate
    logger = log or _FileLogger(_log_path(store, plan_id))

    if delay_seconds and delay_seconds > 0:
        sleep(delay_seconds)

    try:
        pending = store.read()
    except PendingUpdateStateError as exc:
        logger.write(f"cannot read pending state: {exc.message}")
        return {"ok": False, "error": exc.reason}
    if pending is None or pending.get("id") != plan_id:
        logger.write("no matching pending plan; nothing to do")
        return {"ok": False, "error": "unknown_plan"}

    target_ref = _derive_target_ref(pending)
    compose_file = _resolve_compose_file(environ)
    service = _resolve_compose_service(environ)
    env_file = _resolve_env_file(environ, compose_file)

    def fail(code, message, *, rollback=None):
        pending["stage"] = STAGE_FAILED
        pending["updated_at"] = utc_now_iso()
        pending["error_code"] = code
        pending["message"] = message
        store.write(pending)
        logger.write(f"FAILED[{code}]: {message}")
        result = {"ok": False, "error": code, "message": message}
        if rollback:
            # Paths + OS error text only; never file contents (no secret leak).
            result["rollback"] = rollback
            logger.write(
                "ROLLBACK incomplete for: "
                + ", ".join(entry["path"] for entry in rollback)
            )
        return result

    pending["stage"] = STAGE_STARTED
    pending["updated_at"] = utc_now_iso()
    store.write(pending)
    logger.write(f"pulling {target_ref}")

    # Pull first: a pull failure must leave compose/env completely untouched.
    try:
        docker.pull(target_ref)
    except Exception as exc:
        return fail("pull_failed", f"Could not pull the target Admin image: {exc}")
    if expected_target_digest:
        pulled = identify_image(docker, target_ref)
        if pulled.digest != expected_target_digest:
            return fail(
                "target_digest_mismatch",
                "The pulled Admin image no longer matches the resolved System Build.",
            )
    logger.write("pull complete; updating compose/env tag")

    # Everything that mutates persistent files runs inside a byte-for-byte
    # transaction. Snapshot the compose file, its env file (explicit or the
    # default that a variable-driven compose may create), and the ``.bak`` the
    # rewrite leaves behind, so rollback can restore/remove each exactly.
    default_env = compose_file.parent / ".env.admin"
    bak_file = compose_file.with_name(compose_file.name + ".bak")
    txn = ComposeEnvTransaction([compose_file, env_file or default_env, bak_file])

    def rollback_fail(code, message):
        rollback_failures = txn.rollback()
        return fail(code, message, rollback=rollback_failures or None)

    try:
        located = update_admin_image_reference(
            compose_file, target_ref, env_file=env_file
        )
    except OSError as exc:
        return rollback_fail(
            "compose_update_failed", f"Could not update the Admin compose/env: {exc}"
        )
    if not located:
        return rollback_fail(
            "image_reference_missing",
            "Could not locate the Admin image reference to update.",
        )
    logger.write(f"recreating {service} via {compose_file}")

    try:
        compose_recreate(compose_file, service)
    except AdminUpdateApplyError as exc:
        return rollback_fail(exc.code, exc.message)
    except Exception as exc:  # never leak a traceback into the pending state
        return rollback_fail("recreate_failed", f"Could not recreate the Admin Console: {exc}")

    # Optionally verify the replacement Admin (the sidecar survives the recreate
    # and can confirm the new build is actually running). A failed verification
    # rolls the persistent files back to the previous Admin build.
    if verify is not None:
        logger.write("verifying replacement Admin")
        try:
            verified = verify()
        except Exception as exc:
            return rollback_fail("verify_failed", f"Admin verification failed: {exc}")
        if verified is False:
            return rollback_fail(
                "verify_failed", "The replacement Admin Console failed verification."
            )

    # In production the recreate already replaced this process; reaching here
    # (tests / a fast daemon) means the swap returned, so record success.
    pending["stage"] = STAGE_SUCCEEDED
    pending["updated_at"] = utc_now_iso()
    pending["message"] = "Admin Console updated."
    pending["next_step"] = NEXT_STEP_RESUME_EMS
    store.write(pending)
    logger.write("SUCCESS: admin update applied")
    return {"ok": True, "status": STAGE_SUCCEEDED}


def apply_system_transition_admin_update(
    transition_id,
    *,
    store=None,
    state_dir=None,
    docker=None,
    environ=None,
    release_manager=None,
    compose_recreate=None,
    verify=None,
    sleep=None,
    delay_seconds=DEFAULT_DELAY_SECONDS,
    log=None,
    now=None,
):
    """Apply the Admin-image step for a persisted v2 System Build transition."""

    environ = os.environ if environ is None else environ
    if store is None:
        resolved_state_dir = state_dir or _default_state_dir(release_manager, environ)
        store = PendingTransitionStore(resolved_state_dir)
    try:
        record = store.read()
    except TransitionStateError as exc:
        return {"ok": False, "error": exc.reason, "message": exc.message}
    if record is None or record.operation_id != transition_id:
        return {"ok": False, "error": "unknown_transition"}
    try:
        claimed = store.claim_admin_update(transition_id, now=now)
    except TransitionStateError as exc:
        return {"ok": False, "error": exc.reason, "message": exc.message}
    if not claimed:
        return {"ok": False, "error": "admin_update_already_claimed"}
    adapter = _SystemTransitionApplyStore(store, transition_id)
    return apply_admin_update(
        transition_id,
        store=adapter,
        docker=docker,
        environ=environ,
        release_manager=release_manager,
        compose_recreate=compose_recreate,
        verify=verify,
        sleep=sleep,
        delay_seconds=delay_seconds,
        log=log,
        expected_target_digest=record.admin_digest,
    )


def _default_state_dir(release_manager, environ):
    data_dir = getattr(release_manager, "data_dir", None)
    if data_dir is not None:
        return Path(data_dir) / "state"
    configured = (environ.get("EMS_ADMIN_DATA_DIR") or "").strip()
    if configured:
        return Path(configured) / "state"
    from admin.releases import default_admin_data_dir

    return default_admin_data_dir() / "state"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Apply a pending Admin Console update.")
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--plan-id")
    operation.add_argument("--transition-id")
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=0.0,
        help="Delay before applying (sidecar mode passes 0).",
    )
    args = parser.parse_args(argv)
    if args.transition_id:
        result = apply_system_transition_admin_update(
            args.transition_id, delay_seconds=args.delay_seconds
        )
    else:
        result = apply_admin_update(args.plan_id, delay_seconds=args.delay_seconds)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
