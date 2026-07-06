# SPDX-License-Identifier: AGPL-3.0-or-later
"""Execute a guided EMS upgrade after the planning page.

Conservative single-shot executor. It only ever: pulls the target image, bumps
the EMS image reference in the standard ``docker-compose.yml`` and force-recreates
the ``ems`` service. Optional config add-keys / comment refresh run against the
*prepared target release* template and backup/config writes reuse the EMS Core
helpers. It never removes containers, volumes or data and never uses
``down``/``rm``/``down -v``.
"""

import copy
import json
import re
import threading
from collections import namedtuple
from pathlib import Path

from ems import config as ems_config

from admin.deployment import DockerCli, DockerCompose, DockerError
from admin.ems_cli import EmsCliDiagnostics
from admin.ems_tool import EmsToolRunner, mode_detail
from admin.image_identity import (
    ALREADY_CURRENT,
    DOWNGRADE_BLOCKED,
    IDENTITY_UNKNOWN,
    OLDER_THAN_RUNNING_BUILD,
)
from admin.install_context import detect_install_context
from admin.models import utc_now_iso
from admin.releases import DOCKER_IMAGE_REPOSITORY, ReleaseManager

EMS_SERVICE = "ems"

# Target-image verification copy, keyed by the assessment state that blocks the
# run before any mutating step.
_VERIFY_OK_DETAIL = "Target image identity verified."
_VERIFY_UNVERIFIABLE = (
    "Target image identity could not be verified. Guided Upgrade is blocked "
    "before making changes."
)
_VERIFY_BLOCK_MESSAGES = {
    ALREADY_CURRENT: "This EMS build is already installed; there is nothing to upgrade.",
    OLDER_THAN_RUNNING_BUILD: (
        "This target is older than the running EMS build and cannot be "
        "installed through Guided Upgrade."
    ),
    DOWNGRADE_BLOCKED: (
        "This target is older than the running EMS build and cannot be "
        "installed through Guided Upgrade."
    ),
    IDENTITY_UNKNOWN: _VERIFY_UNVERIFIABLE,
}
# Matches the published EMS image ref; the bundled InfluxDB image never matches.
EMS_IMAGE_RE = re.compile(
    r"ghcr\.io/basecubedev/ems-solarflow-api-control:[^\s\"'{}]+"
)

ALL_OPTIONS = (
    "backup",
    "config_check",
    "config_add_keys",
    "config_comments",
    "pull_image",
    "recreate",
    "diagnostics",
)

_RunContext = namedtuple(
    "_RunContext", "context release_dir target_release target_image options"
)

# Live-step labels, kept in sync with the steps emitted in _run below.
_PLAN_LABELS = {
    "verify_image": "Verify target image",
    "preflight": "Preflight checks",
    "backup": "Create backup",
    "config_check": "Check config",
    "config_write": "Update config",
    "pull_image": "Pull image",
    "update_compose": "Update compose",
    "recreate_ems": "Recreate EMS",
    "diagnostics": "Run diagnostics",
}

# Executor step status -> live job step state.
_STEP_STATE = {"ok": "done", "skipped": "skipped", "warning": "done", "error": "failed"}


def _step(step_id, status, label, **extra):
    step = {"id": step_id, "status": status, "label": label}
    step.update(extra)
    return step


def plan_upgrade_steps(options):
    """Ordered ``{key, label}`` steps a run with these options will emit.

    Mirrors the emit order in ``GuidedUpgradeExecutor._run`` so the live job can
    seed pending steps before execution begins.
    """
    options = {key: bool(options.get(key)) for key in ALL_OPTIONS}
    want_config_write = options["config_add_keys"] or options["config_comments"]
    keys = ["verify_image", "preflight"]
    if options["backup"]:
        keys.append("backup")
    if options["config_check"] or want_config_write:
        keys.append("config_check")
    if want_config_write:
        keys.append("config_write")
    if options["pull_image"]:
        keys.append("pull_image")
    keys.append("update_compose")
    if options["recreate"]:
        keys.append("recreate_ems")
    if options["diagnostics"]:
        keys.append("diagnostics")
    return [{"key": key, "label": _PLAN_LABELS[key]} for key in keys]


class _ProgressSteps(list):
    """Step list that notifies a live job as each step is appended."""

    def __init__(self, progress=None):
        super().__init__()
        self._progress = progress

    def append(self, step):
        super().append(step)
        if self._progress is not None:
            self._progress.record_step(step)


class UpgradeJob:
    """Thread-safe progress record for one guided upgrade run."""

    def __init__(self, job_id, planned_steps):
        self._lock = threading.Lock()
        steps = [
            {"key": s["key"], "label": s["label"], "state": "pending", "message": None}
            for s in planned_steps
        ]
        if steps:
            steps[0]["state"] = "running"
        self._state = {
            "job_id": job_id,
            "status": "running",
            "steps": steps,
            "result": None,
            "error": None,
            "started_at": utc_now_iso(),
            "finished_at": None,
        }

    @property
    def job_id(self):
        return self._state["job_id"]

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def record_step(self, step):
        with self._lock:
            steps = self._state["steps"]
            idx = next(
                (i for i, s in enumerate(steps) if s["key"] == step.get("id")), None
            )
            if idx is None:
                return
            state = _STEP_STATE.get(step.get("status"), "done")
            steps[idx]["state"] = state
            steps[idx]["message"] = step.get("detail")
            if state != "failed":
                for nxt in steps[idx + 1:]:
                    if nxt["state"] == "pending":
                        nxt["state"] = "running"
                        break

    def finish(self, result):
        with self._lock:
            self._state["result"] = result
            self._state["finished_at"] = utc_now_iso()
            if result.get("ok"):
                self._state["status"] = "succeeded"
            else:
                self._state["status"] = "failed"
                self._state["error"] = {
                    "reason": result.get("reason"),
                    "message": result.get("message"),
                }
                for step in self._state["steps"]:
                    if step["state"] == "running":
                        step["state"] = "failed"
        return result


class UpgradeJobRegistry:
    """In-memory registry of guided-upgrade jobs on bounded daemon threads."""

    def __init__(self, max_jobs=8):
        self._lock = threading.Lock()
        self._jobs = {}
        self._order = []
        self._max_jobs = max_jobs

    def submit(self, job, runner):
        with self._lock:
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            while len(self._order) > self._max_jobs:
                self._jobs.pop(self._order.pop(0), None)
        thread = threading.Thread(target=self._run, args=(job, runner), daemon=True)
        thread.start()
        return job

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
        return job.snapshot() if job is not None else None

    @staticmethod
    def _run(job, runner):
        try:
            runner(job)
        except Exception:  # never leak a traceback; surface a failed job
            job.finish(
                {
                    "ok": False,
                    "status": "failed",
                    "message": "Upgrade failed unexpectedly.",
                    "steps": [],
                    "warnings": [],
                }
            )


class GuidedUpgradeExecutor:
    """Run the confirmed guided EMS upgrade and return a compact step result.

    Injectable so tests drive it with fake docker/compose/release/config paths
    and never touch a real daemon.
    """

    def __init__(
        self,
        release_manager=None,
        compose=None,
        docker_cli=None,
        ems_cli=None,
        ems_tool=None,
        install_context_provider=detect_install_context,
    ):
        self.release_manager = release_manager or ReleaseManager()
        self.compose = compose or DockerCompose()
        self.docker_cli = docker_cli or DockerCli()
        self.ems_cli = ems_cli or EmsCliDiagnostics()
        # EMS-owned commands (backup) run through the best available EMS tool
        # context: a running EMS container, else a one-off compose container.
        self.ems_tool = ems_tool or EmsToolRunner(
            docker=self.docker_cli, compose=self.compose
        )
        self._install_context_provider = install_context_provider

    def execute(self, target_release, options, *, confirm=False, progress=None):
        rejection, run_context = self.preflight(
            target_release, options, confirm=confirm
        )
        if rejection is not None:
            return rejection
        return self.run(run_context, progress=progress)

    def preflight(self, target_release, options, *, confirm=False):
        """Return ``(rejection, run_context)``; exactly one is non-``None``.

        Fast, side-effect-free guard checks so the endpoint can reject before a
        job is spawned. The returned ``run_context`` feeds :meth:`run`.
        """
        options = self._normalize_options(options)
        if confirm is not True:
            return self._rejected(
                "confirm_required",
                "Explicit upgrade confirmation is required.",
                target_release,
            ), None
        if not isinstance(target_release, str) or not target_release.strip():
            return self._rejected(
                "target_required", "A target release is required.", target_release
            ), None
        target_release = target_release.strip()

        if self.release_manager.prepared_release() != target_release:
            return self._rejected(
                "target_not_prepared",
                "The target release is not the currently prepared release.",
                target_release,
            ), None
        release_dir = self.release_manager.releases_dir / target_release
        if not (release_dir / "config.template.json").is_file():
            return self._rejected(
                "target_not_prepared",
                "Prepared release resources are missing for the target.",
                target_release,
            ), None

        context = self._install_context_provider()
        if not context.config_exists:
            return self._rejected(
                "config_missing", "config/config.json was not found.", target_release
            ), None
        if not context.compose_exists:
            return self._rejected(
                "compose_missing", "docker-compose.yml was not found.", target_release
            ), None

        target_image = f"{DOCKER_IMAGE_REPOSITORY}:{target_release}"
        return None, _RunContext(
            context, release_dir, target_release, target_image, options
        )

    def run(self, run_context, *, progress=None):
        return self._run(
            run_context.context,
            run_context.release_dir,
            run_context.target_release,
            run_context.target_image,
            run_context.options,
            progress=progress,
        )

    def _run(self, context, release_dir, target_release, target_image, options,
             progress=None):
        steps = _ProgressSteps(progress)
        warnings = []
        config_path = Path(context.config_path)

        def failed():
            return self._failed(target_release, target_image, list(steps), warnings)

        # 01 verify target image — pull/prepare then inspect labels so a
        # downgrade / no-op / unverifiable target aborts before any mutation.
        verify = self._verify_target(target_release)
        steps.append(verify)
        if verify["status"] == "error":
            return failed()
        if verify.get("warning"):
            warnings.append(verify["warning"])

        steps.append(_step("preflight", "ok", "Preflight checks"))
        current_config = self._load_config(config_path)
        want_config_write = options["config_add_keys"] or options["config_comments"]

        # 02 backup — run the EMS Core backup through the best EMS tool context
        # (running container, else one-off compose container) so the real
        # (encryptable) EMS backup format is used, not an admin-side copy.
        if options["backup"]:
            step = self._run_backup(context)
            steps.append(step)
            if step["status"] == "error":
                return failed()

        # 03 config_check (also required whenever a config write is requested)
        plan = None
        if options["config_check"] or want_config_write:
            try:
                plan = ems_config.build_config_upgrade_plan(
                    current_config, base_dir=str(release_dir)
                )
            except (ValueError, OSError, ems_config.ConfigUpgradeError) as exc:
                steps.append(_step("config_check", "error", "Check config", detail=str(exc)))
                return failed()
            add_count = len(plan.get("add", []))
            comment_count = len(plan.get("comment_add", [])) + len(
                plan.get("comment_refresh", [])
            )
            steps.append(
                _step(
                    "config_check",
                    "ok",
                    "Check config",
                    detail=f"{add_count} missing keys, {comment_count} comment updates",
                )
            )

        # 04 config_write
        if want_config_write:
            final_config = (
                copy.deepcopy(plan["upgraded_config"])
                if options["config_add_keys"]
                else copy.deepcopy(current_config)
            )
            if options["config_comments"]:
                final_config, _ = ems_config.refresh_template_comments(
                    final_config, base_dir=str(release_dir)
                )
            if final_config == current_config:
                steps.append(
                    _step("config_write", "skipped", "Update config",
                          detail="No config changes were needed.")
                )
            else:
                try:
                    ems_config.write_config_json_atomic(
                        str(config_path), final_config, layout=plan.get("template_layout")
                    )
                except OSError as exc:
                    steps.append(_step("config_write", "error", "Update config", detail=str(exc)))
                    return failed()
                steps.append(_step("config_write", "ok", "Update config"))
                if not options["backup"]:
                    warnings.append("Config was changed without a pre-upgrade backup.")

        # 05 pull_image
        if options["pull_image"]:
            try:
                self.docker_cli.pull(target_image)
            except DockerError as exc:
                steps.append(_step("pull_image", "error", "Pull image", detail=exc.message))
                return failed()
            steps.append(_step("pull_image", "ok", "Pull image", image=target_image))

        # 06 update_compose (always before recreate when the target image differs)
        step = self._update_compose(Path(context.compose_path), target_image)
        steps.append(step)
        if step["status"] == "error":
            return failed()

        # 07 recreate_ems
        if options["recreate"]:
            try:
                self.compose.up(
                    str(context.install_root), services=(EMS_SERVICE,), force_recreate=True
                )
            except DockerError as exc:
                steps.append(_step("recreate_ems", "error", "Recreate EMS", detail=exc.message))
                return failed()
            steps.append(_step("recreate_ems", "ok", "Recreate EMS"))

        # 08 diagnostics
        diagnostics = None
        if options["diagnostics"]:
            diagnostics = self._run_diagnostics(steps, warnings)

        return {
            "ok": True,
            "status": "completed",
            "target_release": target_release,
            "target_image": target_image,
            "steps": list(steps),
            "warnings": warnings,
            "diagnostics": diagnostics,
        }

    def _verify_target(self, target_release):
        """Verify the target image identity before any mutating step.

        Reuses the release manager's build-identity assessment, pulling a
        not-yet-local target so ``latest`` vs stable is decided by build serial.
        Only a proven upgrade passes; older / already-current / unverifiable
        targets return an error step that aborts the run.
        """
        verify = getattr(self.release_manager, "verify_upgrade_target", None)
        if not callable(verify):
            return _step("verify_image", "ok", "Verify target image",
                         detail=_VERIFY_OK_DETAIL)
        try:
            assessment = verify(target_release, pull=self._pull_target)
        except Exception:  # never leak a traceback; treat as unverifiable
            return _step("verify_image", "error", "Verify target image",
                         detail=_VERIFY_UNVERIFIABLE)
        if getattr(assessment, "is_upgrade", False):
            # A legacy metadata fallback (SemVer or the unverified test override)
            # is allowed but carries a warning the run surfaces to the operator.
            warning = getattr(assessment, "warning", None)
            step = _step("verify_image", "ok", "Verify target image",
                         detail=warning or _VERIFY_OK_DETAIL)
            if warning:
                step["warning"] = warning
            return step
        detail = _VERIFY_BLOCK_MESSAGES.get(
            getattr(assessment, "state", None), _VERIFY_UNVERIFIABLE
        )
        return _step("verify_image", "error", "Verify target image", detail=detail)

    def _pull_target(self, image):
        self.docker_cli.pull(image)

    def _run_backup(self, context):
        try:
            result = self.ems_tool.run(context, ("backup", "create"))
        except Exception as exc:  # never leak a traceback to the UI
            return _step("backup", "error", "Create backup", detail=str(exc))
        if result.blocked:
            return _step("backup", "error", "Create backup", detail=result.message)
        if result.returncode != 0:
            return _step("backup", "error", "Create backup", detail=result.detail)
        # Report the EMS tool context (running container vs. compose one-off)
        # as a short, user-safe detail — never the raw command line.
        return _step("backup", "ok", "Create backup", detail=mode_detail(result.mode))

    def _update_compose(self, compose_path, target_image):
        try:
            compose_text = compose_path.read_text(encoding="utf-8")
        except OSError as exc:
            return _step("update_compose", "error", "Update compose", detail=str(exc))
        matches = EMS_IMAGE_RE.findall(compose_text)
        if not matches:
            return _step(
                "update_compose", "error", "Update compose",
                detail="No EMS image reference found in docker-compose.yml.",
            )
        if matches[0] == target_image:
            return _step(
                "update_compose", "skipped", "Update compose",
                detail="Compose already targets the release image.",
            )
        updated_text, _ = EMS_IMAGE_RE.subn(lambda _m: target_image, compose_text)
        try:
            compose_path.with_name(compose_path.name + ".bak").write_text(
                compose_text, encoding="utf-8"
            )
            compose_path.write_text(updated_text, encoding="utf-8")
        except OSError as exc:
            return _step("update_compose", "error", "Update compose", detail=str(exc))
        return _step("update_compose", "ok", "Update compose", image=target_image)

    def _run_diagnostics(self, steps, warnings):
        try:
            diagnostics = self.ems_cli.run()
        except Exception as exc:  # diagnostics must never fail the applied upgrade
            steps.append(_step("diagnostics", "warning", "Run diagnostics", detail=str(exc)))
            warnings.append("Diagnostics could not be run after the upgrade.")
            return None
        status = (diagnostics or {}).get("summary", {}).get("status")
        if status == "failed":
            steps.append(
                _step("diagnostics", "warning", "Run diagnostics",
                      detail="Diagnostics reported failures.")
            )
            warnings.append("Post-upgrade diagnostics reported failures.")
        elif status == "warning":
            steps.append(_step("diagnostics", "warning", "Run diagnostics"))
        else:
            steps.append(_step("diagnostics", "ok", "Run diagnostics"))
        return diagnostics

    @staticmethod
    def _normalize_options(options):
        options = options if isinstance(options, dict) else {}
        return {key: bool(options.get(key)) for key in ALL_OPTIONS}

    @staticmethod
    def _load_config(config_path):
        try:
            parsed = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _rejected(reason, message, target_release):
        return {
            "ok": False,
            "status": "rejected",
            "reason": reason,
            "message": message,
            "target_release": target_release,
            "target_image": None,
            "steps": [],
            "warnings": [],
            "diagnostics": None,
        }

    @staticmethod
    def _failed(target_release, target_image, steps, warnings):
        return {
            "ok": False,
            "status": "failed",
            "target_release": target_release,
            "target_image": target_image,
            "steps": steps,
            "warnings": warnings,
            "diagnostics": None,
        }
