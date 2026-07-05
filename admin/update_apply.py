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
    PendingUpdateStateError,
    target_admin_image_for_release,
)
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
        if text != original:
            compose_file.with_name(compose_file.name + ".bak").write_text(
                original, encoding="utf-8"
            )
            compose_file.write_text(text, encoding="utf-8")

    # Keep the recorded env tag correct when the env file is provided/exists.
    target_env = env_file or (default_env if default_env.exists() else None)
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


def apply_admin_update(
    plan_id,
    *,
    store=None,
    state_dir=None,
    docker=None,
    environ=None,
    release_manager=None,
    compose_recreate=None,
    sleep=None,
    delay_seconds=DEFAULT_DELAY_SECONDS,
    log=None,
):
    """Pull, repoint, and recreate the Admin service for one pending plan.

    Every failure before the recreate is captured in the pending state (stage
    ``admin_update_failed``) and the per-plan log, leaving the current Admin
    running. The recreate replaces this process in a real deployment; the new
    Admin resumes from the pending state.
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

    def fail(code, message):
        pending["stage"] = STAGE_FAILED
        pending["updated_at"] = utc_now_iso()
        pending["message"] = message
        store.write(pending)
        logger.write(f"FAILED[{code}]: {message}")
        return {"ok": False, "error": code, "message": message}

    pending["stage"] = STAGE_STARTED
    pending["updated_at"] = utc_now_iso()
    store.write(pending)
    logger.write(f"pulling {target_ref}")

    try:
        docker.pull(target_ref)
    except Exception as exc:
        return fail("pull_failed", f"Could not pull the target Admin image: {exc}")
    logger.write("pull complete; updating compose/env tag")

    try:
        located = update_admin_image_reference(
            compose_file, target_ref, env_file=env_file
        )
    except OSError as exc:
        return fail("compose_update_failed", f"Could not update the Admin compose/env: {exc}")
    if not located:
        return fail(
            "image_reference_missing",
            "Could not locate the Admin image reference to update.",
        )
    logger.write(f"recreating {service} via {compose_file}")

    try:
        compose_recreate(compose_file, service)
    except AdminUpdateApplyError as exc:
        return fail(exc.code, exc.message)
    except Exception as exc:  # never leak a traceback into the pending state
        return fail("recreate_failed", f"Could not recreate the Admin Console: {exc}")

    # In production the recreate already replaced this process; reaching here
    # (tests / a fast daemon) means the swap returned, so record success.
    pending["stage"] = STAGE_SUCCEEDED
    pending["updated_at"] = utc_now_iso()
    pending["message"] = "Admin Console updated."
    pending["next_step"] = NEXT_STEP_RESUME_EMS
    store.write(pending)
    logger.write("SUCCESS: admin update applied")
    return {"ok": True, "status": STAGE_SUCCEEDED}


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
    parser.add_argument("--plan-id", required=True)
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=0.0,
        help="Delay before applying (sidecar mode passes 0).",
    )
    args = parser.parse_args(argv)
    result = apply_admin_update(args.plan_id, delay_seconds=args.delay_seconds)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
