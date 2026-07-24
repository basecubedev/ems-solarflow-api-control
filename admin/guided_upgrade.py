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
import hashlib
import json
import re
import threading
from collections import namedtuple
from pathlib import Path

from ems import config as ems_config

from admin.deployment import DockerCli, DockerCompose, DockerError
from admin.ems_cli import EmsCliDiagnostics
from admin.ems_tool import EmsToolRunner, mode_detail
from admin.install_context import detect_install_context
from admin.models import utc_now_iso
from admin.releases import DOCKER_IMAGE_REPOSITORY, ReleaseManager
from admin.system_build import digest_pinned_ref
from admin.zendure_mqtt_migration_review import (
    prepare_migration_apply,
    zendure_mqtt_migration_review,
)

EMS_SERVICE = "ems"

# Target-image verification copy for the resolved-System-Build check.
_VERIFY_OK_DETAIL = "Target image identity verified."
_VERIFY_UNVERIFIABLE = (
    "Target image identity could not be verified. Guided Upgrade is blocked "
    "before making changes."
)
# The pulled runtime image's content digest did not equal the verified digest.
# A matching tag is never accepted as proof; the run stops before Compose/recreate.
_PULL_DIGEST_MISMATCH = (
    "The pulled EMS image digest does not match the verified System Build. "
    "No Compose change was made."
)
# Matches the published EMS image ref in either the mutable ``:tag`` form or the
# digest-pinned ``@sha256:...`` form; the bundled InfluxDB image never matches.
EMS_IMAGE_RE = re.compile(
    r"ghcr\.io/basecubedev/ems-solarflow-api-control[@:][^\s\"'{}]+"
)
# The created backup archive path printed by ``backup create`` (``.tar.gz`` or
# its encrypted ``.tar.gz.enc`` variant).
_BACKUP_ARCHIVE_RE = re.compile(r"(\S+\.tar\.gz(?:\.enc)?)")

ALL_OPTIONS = (
    "backup",
    "config_check",
    "config_add_keys",
    "config_comments",
    "pull_image",
    "recreate",
    "diagnostics",
)

# Deploying the System Build is binding: pulling the target image and recreating
# the EMS container are mandatory whenever the image changes. A deactivatable
# recreate could leave Compose pointing at the new build while the old container
# keeps running, so these are forced on and are never operator-toggleable.
MANDATORY_OPTIONS = ("pull_image", "recreate")


def _normalized_options(options):
    """Bool-normalize the option flags, forcing the mandatory deploy steps on."""

    source = options if isinstance(options, dict) else {}
    normalized = {key: bool(source.get(key)) for key in ALL_OPTIONS}
    for key in MANDATORY_OPTIONS:
        normalized[key] = True
    return normalized

_RunContext = namedtuple(
    "_RunContext",
    "context release_dir target_release target_image target_digest "
    "target_runtime_image options system_build migration",
)


def _system_build_field(system_build, name):
    """Read one field from a resolved SystemBuild (object or plain dict)."""

    if system_build is None:
        return None
    if isinstance(system_build, dict):
        return system_build.get(name)
    return getattr(system_build, name, None)
_AlignmentPreparation = namedtuple(
    "_AlignmentPreparation",
    "run_context request_fingerprint steps warnings current_config",
)

# Live-step labels, kept in sync with the steps emitted in _run below.
_PLAN_LABELS = {
    "verify_image": "Verify target image",
    "preflight": "Preflight checks",
    "migration_review": "Review Zendure MQTT migration",
    "backup": "Create backup",
    "mqtt_migration": "Apply Zendure MQTT migration",
    "target_resources": "Verify target resources",
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


def guided_upgrade_request_fingerprint(target_release, options):
    """Return a stable identity for one confirmed Guided Upgrade request.

    Only executor options that affect the operation are included. Unknown JSON
    fields and mapping insertion order therefore cannot change the identity,
    while any effective option change does. The prefixed representation is
    suitable for persisting with a paired-system transition.
    """
    if not isinstance(target_release, str) or not target_release.strip():
        raise ValueError("A target release is required for request fingerprinting.")
    normalized_options = _normalized_options(options)
    payload = json.dumps(
        {
            "options": normalized_options,
            "target_release": target_release.strip(),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def plan_upgrade_steps(options):
    """Ordered ``{key, label}`` steps a run with these options will emit.

    Mirrors the emit order in ``GuidedUpgradeExecutor._run`` so the live job can
    seed pending steps before execution begins.
    """
    options = _normalized_options(options)
    want_config_write = options["config_add_keys"] or options["config_comments"]
    keys = ["verify_image", "preflight", "migration_review"]
    if options["backup"]:
        keys.append("backup")
    keys.append("mqtt_migration")
    # The target resources are imported by the aligned Admin and verified here,
    # after alignment, before any config or EMS change.
    keys.append("target_resources")
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
        self._by_operation = {}
        self._max_jobs = max_jobs

    def _register_locked(self, job, operation_id=None):
        self._jobs[job.job_id] = job
        self._order.append(job.job_id)
        if operation_id is not None:
            self._by_operation[operation_id] = job.job_id
        while len(self._order) > self._max_jobs:
            evicted = self._order.pop(0)
            self._jobs.pop(evicted, None)
            self._by_operation = {
                op: jid for op, jid in self._by_operation.items() if jid != evicted
            }

    def submit(self, job, runner):
        with self._lock:
            self._register_locked(job)
        thread = threading.Thread(target=self._run, args=(job, runner), daemon=True)
        thread.start()
        return job

    def get_or_submit(self, operation_id, job, runner):
        """Submit ``job`` for ``operation_id`` once; return ``(job, created)``.

        A repeated call for the same operation returns the existing live job and
        ``created=False`` without starting a second worker, so several resume
        requests can never spawn two EMS upgrade jobs.
        """

        with self._lock:
            existing_id = self._by_operation.get(operation_id)
            if existing_id is not None and existing_id in self._jobs:
                return self._jobs[existing_id], False
            self._register_locked(job, operation_id=operation_id)
        thread = threading.Thread(target=self._run, args=(job, runner), daemon=True)
        thread.start()
        return job, True

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
        config_apply=None,
        install_context_provider=detect_install_context,
    ):
        self.release_manager = release_manager or ReleaseManager()
        self.compose = compose or DockerCompose()
        self.docker_cli = docker_cli or DockerCli()
        self.ems_cli = ems_cli or EmsCliDiagnostics()
        self.config_apply = config_apply
        # EMS-owned commands (backup) run through the best available EMS tool
        # context: a running EMS container, else a one-off compose container.
        self.ems_tool = ems_tool or EmsToolRunner(
            docker=self.docker_cli, compose=self.compose
        )
        self._install_context_provider = install_context_provider

    def execute(self, target_release, options, *, confirm=False, progress=None,
                system_build=None):
        rejection, run_context = self.preflight(
            target_release, options, confirm=confirm, system_build=system_build
        )
        if rejection is not None:
            return rejection
        return self.run(run_context, progress=progress)

    @staticmethod
    def request_fingerprint(target_release, options):
        """Return the durable identity for effective Guided Upgrade inputs."""
        return guided_upgrade_request_fingerprint(target_release, options)

    def preflight(self, target_release, options, *, confirm=False, system_build=None):
        """Return ``(rejection, run_context)``; exactly one is non-``None``.

        Current-state preflight: fast, side-effect-free guard checks on the
        *current* installation only. It deliberately does NOT require the target
        release cache — those resources are imported from the aligned target
        Admin after Admin alignment and are verified later by :meth:`run`. An
        unprepared, not-yet-cached target therefore still reaches Admin
        alignment.

        ``system_build`` is the resolved, verified Admin/EMS pair. Its EMS image
        and digest are the sole build-identity source for the run — the compose
        ref and pull always use ``system_build.ems_image``, so a request tag can
        never smuggle a different EMS image/repository.
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

        context = self._install_context_provider()
        if not context.config_exists:
            return self._rejected(
                "config_missing", "config/config.json was not found.", target_release
            ), None
        if not context.compose_exists:
            return self._rejected(
                "compose_missing", "docker-compose.yml was not found.", target_release
            ), None

        # The same EMS-owned dry-run used by Maintenance is part of the visible
        # upgrade plan. Its exact config fingerprint is carried into the run so a
        # changed migration can never be applied from a stale confirmation.
        migration = self._mqtt_migration_review(context)

        release_dir = self.release_manager.releases_dir / target_release
        # The resolved System Build's EMS image is authoritative; only fall back
        # to the fixed official repository ref for a legacy direct execution that
        # did not resolve a pair (the repository is still server-fixed).
        target_image = (
            _system_build_field(system_build, "ems_image")
            or f"{DOCKER_IMAGE_REPOSITORY}:{target_release}"
        )
        target_digest = _system_build_field(system_build, "ems_digest")
        # target_image / target_release stay the readable display identity (the
        # release tag). The runtime identity that Docker pulls and Compose
        # persists is the exact verified digest, so a later registry tag move can
        # never drift the installed or restarted EMS image.
        target_runtime_image = self._runtime_image(target_image, target_digest)
        return None, _RunContext(
            context, release_dir, target_release, target_image, target_digest,
            target_runtime_image, options, system_build, migration,
        )

    @staticmethod
    def _runtime_image(target_image, target_digest):
        """Return the immutable digest-pinned EMS runtime ref for the deploy.

        With a verified digest the runtime ref is ``repository@sha256:<digest>``
        (the mutable tag stripped, the official repository required). A legacy
        direct execution without a resolved digest keeps the tag ref — it has no
        verified digest to pin and is gated elsewhere.
        """

        if not target_digest:
            return target_image
        return digest_pinned_ref(
            target_image, target_digest, require_repo=DOCKER_IMAGE_REPOSITORY
        )

    def prepare_alignment(self, run_context):
        """Perform all fallible work required before paired Admin alignment.

        Target verification and the optional EMS backup must complete before an
        Admin replacement is requested. The returned preparation is reusable by
        :meth:`run`; passing it there replays the completed steps to live job
        progress without executing their work again.

        Returns ``(failure, preparation)`` like :meth:`preflight`: exactly one
        item is non-``None``. Unexpected exceptions intentionally propagate so
        the durable transition owner can record an operational failure.
        """
        steps = []
        warnings = []

        def failed():
            return self._failed(
                run_context.target_release,
                run_context.target_image,
                list(steps),
                warnings,
                target_digest=run_context.target_digest,
                runtime_image=run_context.target_runtime_image,
            )

        verify = self._verify_system_build(run_context)
        steps.append(verify)
        if verify["status"] == "error":
            return failed(), None

        current_config = self._load_config(Path(run_context.context.config_path))
        steps.append(_step("preflight", "ok", "Preflight checks"))

        migration = run_context.migration
        review = migration.get("review") or {}
        changes = review.get("changes") if isinstance(review.get("changes"), list) else []
        losing_control = sum(1 for change in changes if change.get("disables_control"))
        steps.append(
            _step(
                "migration_review",
                "ok",
                "Review Zendure MQTT migration",
                detail=(
                    f"{len(changes)} affected device(s); "
                    f"{losing_control} lose control."
                    if migration.get("required")
                    else "No MQTT migration is required."
                ),
                required=bool(migration.get("required")),
                affected_devices=len(changes),
                devices_losing_control=losing_control,
                revision=migration.get("revision"),
            )
        )

        if run_context.options["backup"]:
            backup = self._run_backup(run_context.context)
            steps.append(backup)
            if backup["status"] == "error":
                return failed(), None

        migration_step = self._apply_mqtt_migration(run_context)
        steps.append(migration_step)
        if migration_step["status"] == "error":
            return failed(), None
        # Generic config upgrade must start from the migrated config, never the
        # stale snapshot taken before the Core-owned migration.
        current_config = self._load_config(Path(run_context.context.config_path))

        preparation = _AlignmentPreparation(
            run_context=run_context,
            request_fingerprint=self.request_fingerprint(
                run_context.target_release, run_context.options
            ),
            steps=tuple(copy.deepcopy(steps)),
            warnings=tuple(warnings),
            current_config=copy.deepcopy(current_config),
        )
        return None, preparation

    def resume_alignment(self, run_context, *, backup=None, migration=None):
        """Reconstruct successful pre-alignment work after Admin replacement.

        The paired transition's persisted request fingerprint is the authority
        for using this method. It deliberately performs no image verification,
        pull, or backup; it only recreates their completed progress entries and
        reloads the non-mutating config snapshot needed by the remaining run.

        ``backup`` is the durable backup state consumed from the persisted
        context (``{"completed", "verified", "reference"}``). The backup step is
        rebuilt from that recorded state — its exact verified archive — rather
        than re-asserted merely because the backup option was enabled.
        """
        steps = [
            _step(
                "verify_image",
                "ok",
                "Verify target image",
                detail="Completed before Admin alignment.",
            ),
            _step(
                "preflight",
                "ok",
                "Preflight checks",
                detail="Completed before Admin alignment.",
            ),
        ]
        migration = migration or {}
        required = bool(migration.get("required"))
        steps.append(
            _step(
                "migration_review",
                "ok",
                "Review Zendure MQTT migration",
                detail="Completed before Admin alignment.",
                required=required,
                revision=migration.get("revision"),
            )
        )
        if run_context.options["backup"]:
            backup = backup or {}
            reference = backup.get("reference")
            steps.append(
                _step(
                    "backup",
                    "ok",
                    "Create backup",
                    detail=reference or "Completed before Admin alignment.",
                    archive=reference,
                    verified=bool(backup.get("verified")),
                )
            )
        steps.append(
            _step(
                "mqtt_migration",
                "ok" if required else "skipped",
                "Apply Zendure MQTT migration",
                detail=(
                    "Completed before Admin alignment."
                    if required
                    else "No MQTT migration was required."
                ),
            )
        )
        return _AlignmentPreparation(
            run_context=run_context,
            request_fingerprint=self.request_fingerprint(
                run_context.target_release, run_context.options
            ),
            steps=tuple(steps),
            warnings=(),
            current_config=self._load_config(Path(run_context.context.config_path)),
        )

    def run(self, run_context, *, pre_alignment=None, progress=None):
        if pre_alignment is None:
            failure, pre_alignment = self.prepare_alignment(run_context)
            if failure is not None:
                return failure
        expected_fingerprint = self.request_fingerprint(
            run_context.target_release, run_context.options
        )
        if pre_alignment.request_fingerprint != expected_fingerprint:
            raise ValueError("Guided Upgrade pre-alignment context does not match request.")
        return self._run(
            run_context.context,
            run_context.release_dir,
            run_context.target_release,
            run_context.target_image,
            run_context.target_digest,
            run_context.target_runtime_image,
            run_context.options,
            pre_alignment=pre_alignment,
            progress=progress,
        )

    def _run(self, context, release_dir, target_release, target_image, target_digest,
             target_runtime_image, options, *, pre_alignment, progress=None):
        steps = _ProgressSteps(progress)
        for step in pre_alignment.steps:
            steps.append(copy.deepcopy(step))
        warnings = list(pre_alignment.warnings)
        config_path = Path(context.config_path)
        current_config = copy.deepcopy(pre_alignment.current_config)

        def failed(reason=None, message=None):
            return self._failed(
                target_release, target_image, list(steps), warnings,
                target_digest=target_digest, runtime_image=target_runtime_image,
                reason=reason, message=message,
            )

        # target_resources: the aligned target Admin imports its embedded
        # bundle into the release cache before this runs. Verify the cache is
        # present and complete now, before any config or EMS change. A missing
        # or incomplete import stops the run here.
        resources = self._verify_target_resources(release_dir, target_release)
        steps.append(resources)
        if resources["status"] == "error":
            return failed()

        want_config_write = options["config_add_keys"] or options["config_comments"]

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

        # 05 pull_image — ensure the exact verified digest is locally available.
        # Verification already pulled it, so an exact digest already present is
        # reused without any registry request; only a missing digest is pulled.
        # A matching mutable tag is never accepted as proof.
        if options["pull_image"]:
            if self._verified_local_runtime_image(target_runtime_image, target_digest):
                steps.append(
                    _step("pull_image", "skipped", "Pull image",
                          image=target_runtime_image,
                          detail="Verified image already available locally.")
                )
            else:
                try:
                    self.docker_cli.pull(target_runtime_image)
                except DockerError as exc:
                    steps.append(_step("pull_image", "error", "Pull image",
                                       detail=exc.message, code=exc.code))
                    return failed(reason=exc.code, message=exc.message)
                # A matching tag is never proof: confirm the pulled content digest
                # equals the verified one before persisting or recreating. A
                # mismatch (a moved/re-pushed image) fails closed with a typed reason.
                mismatch = self._verify_pulled_digest(target_runtime_image, target_digest)
                if mismatch is not None:
                    steps.append(mismatch)
                    return failed(reason="target_digest_mismatch")
                steps.append(
                    _step("pull_image", "ok", "Pull image", image=target_runtime_image)
                )

        # 06 update_compose — persist the digest-pinned runtime ref so a later
        # tag move cannot drift a pull/recreate/recovery to a newer image.
        step = self._update_compose(Path(context.compose_path), target_runtime_image)
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
            "target_digest": target_digest,
            # The immutable digest-pinned ref actually pulled and persisted; the
            # readable tag stays in target_release / target_image.
            "runtime_image": target_runtime_image,
            "steps": list(steps),
            "warnings": warnings,
            "diagnostics": diagnostics,
        }

    def _verified_local_runtime_image(self, runtime_image, target_digest):
        """True when the exact digest-pinned image is already present and matches.

        A verified digest can be reused without a registry request. Only an exact
        ``repository@sha256:<digest>`` ref whose inspected content digest equals
        the verified digest counts — a matching mutable tag is never proof, and a
        divergent local digest is never reused.
        """

        if not target_digest or "@sha256:" not in str(runtime_image):
            return False
        return self._inspect_digest(runtime_image) == target_digest

    def _verify_pulled_digest(self, runtime_image, target_digest):
        """Return a failed pull step when the pulled content digest is wrong.

        With no verified digest (a legacy direct execution) there is nothing to
        confirm and ``None`` is returned. Otherwise the just-pulled ref is
        inspected and its content digest must equal ``target_digest``; a missing
        or divergent digest fails closed before Compose is updated.
        """

        if not target_digest:
            return None
        actual = self._inspect_digest(runtime_image)
        if actual == target_digest:
            return None
        return _step(
            "pull_image", "error", "Pull image",
            detail=_PULL_DIGEST_MISMATCH,
            reason="target_digest_mismatch",
            expected=target_digest,
            actual=actual,
        )

    def _inspect_digest(self, image_ref):
        """Return the local content digest of ``image_ref``, or ``None``."""

        inspect = getattr(self.docker_cli, "inspect_image", None)
        if not callable(inspect):
            return None
        try:
            result = inspect(image_ref)
        except Exception:
            return None
        if isinstance(result, dict):
            return result.get("digest")
        return None

    def _verify_system_build(self, run_context):
        """Confirm the run targets the resolved System Build's EMS identity.

        The resolved :class:`~admin.system_build.SystemBuild` is the single
        build-identity source: its EMS image ref and digest are already verified
        by the ``SystemBuildResolver`` (repository fixed server-side, pair
        cross-checked). This step never re-runs a second, independent release
        assessment, so a divergent legacy ``verify_upgrade_target`` verdict can
        no longer block a valid System Build. A missing ems image/digest (a
        legacy direct execution without a resolved pair) is unverifiable.
        """
        system_build = run_context.system_build
        if system_build is None:
            # Legacy direct execution without a resolved pair. The upgrade
            # direction is gated at validation time; never re-assess here.
            return _step("verify_image", "ok", "Verify target image",
                         detail=_VERIFY_OK_DETAIL)
        ems_image = _system_build_field(system_build, "ems_image")
        ems_digest = _system_build_field(system_build, "ems_digest")
        if not ems_image or not ems_digest:
            return _step("verify_image", "error", "Verify target image",
                         detail=_VERIFY_UNVERIFIABLE)
        return _step(
            "verify_image", "ok", "Verify target image",
            detail="Resolved System Build image identity verified.",
            image=ems_image, digest=ems_digest,
        )

    def _verify_target_resources(self, release_dir, target_release):
        """Confirm the aligned target Admin imported the target release cache.

        Post-alignment gate: the selected release must be the currently prepared
        (imported) release and its ``config.template.json`` must be present. A
        not-yet-imported or partial cache aborts before any config/EMS change.
        """
        try:
            prepared = self.release_manager.prepared_release()
        except Exception:
            prepared = None
        if prepared != target_release:
            return _step(
                "target_resources", "error", "Verify target resources",
                detail="The target System Build resources have not been imported yet.",
            )
        if not (release_dir / "config.template.json").is_file():
            return _step(
                "target_resources", "error", "Verify target resources",
                detail="Imported target resources are incomplete for this build.",
            )
        return _step(
            "target_resources", "ok", "Verify target resources",
            detail="Target System Build resources verified.",
        )

    def _run_backup(self, context):
        # EMS Core creates AND verifies the archive in one command: it validates
        # the manifest and every member checksum (and decrypts when applicable)
        # and exits non-zero if verification fails. Only a verified archive with
        # an identifiable path is accepted, so a bad backup stops the run before
        # any Admin replacement.
        try:
            result = self.ems_tool.run(context, ("backup", "create", "--verify"))
        except Exception as exc:  # never leak a traceback to the UI
            return _step("backup", "error", "Create backup", detail=str(exc))
        if result.blocked:
            return _step("backup", "error", "Create backup", detail=result.message)
        if result.returncode != 0:
            return _step(
                "backup", "error", "Create backup",
                detail=result.detail or "Backup verification failed.",
            )
        archive = self._parse_backup_archive(result.detail)
        if not archive:
            return _step(
                "backup", "error", "Create backup",
                detail="The created backup archive could not be identified.",
            )
        # ``detail`` is the user-safe execution context; ``archive`` is the exact
        # verified archive reference persisted for a durable resume.
        return _step(
            "backup", "ok", "Create backup",
            detail=mode_detail(result.mode), archive=archive, verified=True,
        )

    def _apply_mqtt_migration(self, run_context):
        """Apply the reviewed Core migration after backup, before alignment."""

        migration = run_context.migration
        if not migration.get("required"):
            return _step(
                "mqtt_migration",
                "skipped",
                "Apply Zendure MQTT migration",
                detail="No MQTT migration was required.",
            )
        expected_revision = migration.get("revision")
        prepared = prepare_migration_apply(
            expected_revision,
            base_dir=str(run_context.context.install_root),
        )
        if prepared.get("status") != "ok":
            detail = prepared.get("message") or (
                "The MQTT migration review is stale; review the plan again."
                if prepared.get("status") == "conflict"
                else "The MQTT migration could not be validated."
            )
            return _step(
                "mqtt_migration",
                "error",
                "Apply Zendure MQTT migration",
                detail=detail,
                reason=prepared.get("status"),
            )
        if not prepared.get("changed"):
            return _step(
                "mqtt_migration",
                "skipped",
                "Apply Zendure MQTT migration",
                detail="The config was already migrated.",
            )
        try:
            if self.config_apply is not None:
                self.config_apply.apply_maintenance(
                    prepared["payload"],
                    expected_revision,
                    create_backup=False,
                )
            else:
                migrated = json.loads(prepared["payload"].decode("utf-8"))
                ems_config.write_config_json_atomic(
                    str(run_context.context.config_path), migrated
                )
        except Exception as exc:
            return _step(
                "mqtt_migration",
                "error",
                "Apply Zendure MQTT migration",
                detail=f"MQTT migration write failed: {exc}",
                reason="write_failed",
            )
        return _step(
            "mqtt_migration",
            "ok",
            "Apply Zendure MQTT migration",
            detail="EMS/Core migration applied and validated.",
            warnings=list(prepared.get("warnings") or []),
        )

    @staticmethod
    def _parse_backup_archive(detail):
        """Extract the created archive path from ``backup create`` output."""

        if not detail:
            return None
        match = _BACKUP_ARCHIVE_RE.search(detail)
        return match.group(1) if match else None

    def _update_compose(self, compose_path, runtime_image):
        try:
            compose_text = compose_path.read_text(encoding="utf-8")
        except OSError as exc:
            return _step("update_compose", "error", "Update compose", detail=str(exc))
        # Matches either the mutable ``:tag`` form or an existing digest-pinned
        # ``@sha256:...`` ref, so a re-upgrade of an already-pinned compose works.
        matches = EMS_IMAGE_RE.findall(compose_text)
        if not matches:
            return _step(
                "update_compose", "error", "Update compose",
                detail="No EMS image reference found in docker-compose.yml.",
            )
        if matches[0] == runtime_image:
            return _step(
                "update_compose", "skipped", "Update compose",
                detail="Compose already targets the verified runtime image.",
            )
        updated_text, _ = EMS_IMAGE_RE.subn(lambda _m: runtime_image, compose_text)
        try:
            compose_path.with_name(compose_path.name + ".bak").write_text(
                compose_text, encoding="utf-8"
            )
            compose_path.write_text(updated_text, encoding="utf-8")
        except OSError as exc:
            return _step("update_compose", "error", "Update compose", detail=str(exc))
        return _step("update_compose", "ok", "Update compose", image=runtime_image)

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
        return _normalized_options(options)

    @staticmethod
    def _load_config(config_path):
        try:
            parsed = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _mqtt_migration_review(self, context):
        """Return the EMS-owned review bound to the exact current config bytes."""

        path = Path(context.config_path)
        raw = path.read_bytes()
        config = self._load_config(path)
        review = zendure_mqtt_migration_review(config)
        return {
            "required": bool(review.get("needs_migration")),
            "revision": hashlib.sha256(raw).hexdigest(),
            "review": review,
        }

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
    def _failed(target_release, target_image, steps, warnings, *, target_digest=None,
                runtime_image=None, reason=None, message=None):
        result = {
            "ok": False,
            "status": "failed",
            "target_release": target_release,
            "target_image": target_image,
            "target_digest": target_digest,
            "runtime_image": runtime_image,
            "steps": steps,
            "warnings": warnings,
            "diagnostics": None,
        }
        if reason is not None:
            result["reason"] = reason
        if message is not None:
            result["message"] = message
        return result
