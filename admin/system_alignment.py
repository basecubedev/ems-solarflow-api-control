# SPDX-License-Identifier: AGPL-3.0-or-later
"""Align paired System Builds and own their durable transition lifecycle."""

from admin.admin_update import (
    CANCELLABLE_TRANSITION_STAGES,
    TERMINAL_TRANSITION_STAGES,
    TRANSITION_MODE_ALIGN_EXISTING,
    TRANSITION_MODE_AUTOMATED_SETUP,
    TRANSITION_MODE_FRESH_INSTALL,
    TRANSITION_MODE_GUIDED_UPGRADE,
    TRANSITION_STAGE_ADMIN_ALIGNED,
    TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
    TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
    TRANSITION_STAGE_COMPLETED,
    TRANSITION_STAGE_EMS_OPERATION_PENDING,
    TRANSITION_STAGE_EMS_OPERATION_RUNNING,
    TRANSITION_STAGE_FAILED_RECOVERABLE,
    TRANSITION_STAGE_HEALTHCHECK_PENDING,
    TRANSITION_STAGE_RESOURCES_VERIFIED,
    TransitionStateError,
    make_transition_record,
)
from admin.system_build import (
    ALIGN_ADMIN_RECREATE_REQUIRED,
    ALIGN_ALIGNED,
    ALIGN_SYSTEM_BUILD_MISMATCH,
    ALIGNMENT_UPDATE_DECISIONS,
    BuildResourceStrategy,
    SystemBuild,
    SystemBuildError,
    classify_channel,
    decide_alignment,
    decide_upgrade_direction,
    system_build_compatibility,
    system_build_keeps_current_admin,
    system_build_resource_strategy,
)
from admin.system_build_id import validate_system_build_id

SUPPORTED_ALIGNMENT_MODES = frozenset(
    {
        TRANSITION_MODE_AUTOMATED_SETUP,
        TRANSITION_MODE_FRESH_INSTALL,
        TRANSITION_MODE_GUIDED_UPGRADE,
        TRANSITION_MODE_ALIGN_EXISTING,
    }
)

_STRATEGY_EMBEDDED = BuildResourceStrategy.EMBEDDED.value
_STRATEGY_RELEASE_ARCHIVE = BuildResourceStrategy.RELEASE_ARCHIVE.value

_ACTION_BUSY_STAGES = {
    TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
    TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
    TRANSITION_STAGE_ADMIN_ALIGNED,
}

_ACTION_PROGRESS_MESSAGES = {
    TRANSITION_STAGE_ADMIN_UPDATE_PENDING: "Preparing the Admin Server update…",
    TRANSITION_STAGE_ADMIN_RECONNECT_PENDING: (
        "Waiting for the updated Admin Server to reconnect…"
    ),
    TRANSITION_STAGE_ADMIN_ALIGNED: "Verifying selected System Build resources…",
}


def terminal_system_build_action_state(requested_tag, code, message) -> dict:
    """Return the normalized fail-closed action contract for validation errors."""

    tag = str(requested_tag or "").strip() or None
    return {
        "selected_build": {
            "tag": tag,
            "channel": None,
            "revision": None,
            "build_id": None,
        },
        "selection_fingerprint": None,
        "compatibility_mode": None,
        "alignment_state": "error",
        "resource_strategy": None,
        "resource_state": "error",
        "admin_update_required": False,
        "admin_update_allowed": False,
        "continue_allowed": False,
        "terminal_error": {"code": code, "message": message},
        "busy": False,
        "progress_message": None,
        "polling_required": False,
        "transition_stage": "validation_failed",
        "operation_id": None,
    }


class EffectiveBuildDecision:
    """One server-authoritative compatibility/resource verdict for a build.

    Computed once from the resolved build + running Admin and reused by
    validate, confirm, resource preparation and discovery authorization so those
    steps can never disagree about alignment, resource strategy or whether the
    modern Admin is kept. It never opens a transition or touches Docker state.
    """

    def __init__(self, *, build, running, decision, effective_alignment,
                 embedded_state):
        self.build = build
        self.running = running
        self.decision = decision
        self.effective_alignment = effective_alignment
        self.embedded_state = embedded_state
        self.compatibility_mode = system_build_compatibility(build)
        self.resource_strategy = system_build_resource_strategy(build)
        self.keeps_current_admin = system_build_keeps_current_admin(build)

    @property
    def is_mismatch(self) -> bool:
        return self.decision == ALIGN_SYSTEM_BUILD_MISMATCH

    @property
    def admin_update_required(self) -> bool:
        return self.effective_alignment in ALIGNMENT_UPDATE_DECISIONS

    @property
    def alignment_ready(self) -> bool:
        return self.effective_alignment == ALIGN_ALIGNED

    @property
    def embedded_applicable(self) -> bool:
        return self.resource_strategy == _STRATEGY_EMBEDDED

    @property
    def embedded_valid(self):
        """``True``/``False`` for an embedded build; ``None`` when not applicable."""

        if not self.embedded_applicable:
            return None
        return self.embedded_state == "match"

    @property
    def resources_ready(self) -> bool:
        """True when resources are present (embedded) or preparable (archive)."""

        if self.embedded_applicable:
            return self.embedded_state == "match"
        # A legacy release always has a verifiable historical archive to prepare.
        return True

    def resource_status(self, *, verified=False) -> str:
        if verified:
            return "prepared"
        if not self.embedded_applicable:
            return "ready"
        return {
            "match": "ready",
            "mismatch": "stale",
        }.get(self.embedded_state, "unknown")


class SystemAlignmentError(Exception):
    """An alignment step could not proceed. ``code`` is a stable machine string."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class SystemAlignmentService:
    """Own the durable Admin/resources/EMS/healthcheck transition lifecycle."""

    def __init__(self, *, resolver, transition_store, embedded_resources,
                 known_good_store, current_identity, persistent_ref, launcher,
                 current_ems_identity=None, release_archive_resources=None,
                 now=None):
        self._resolver = resolver
        self._transitions = transition_store
        self._embedded = embedded_resources
        self._release_archive = release_archive_resources
        self._known_good = known_good_store
        self._current_identity = current_identity
        self._current_ems_identity = current_ems_identity or (lambda: {})
        self._persistent_ref = persistent_ref
        self._launcher = launcher
        self._now = now

    # --- helpers ---------------------------------------------------------

    def _now_value(self):
        return self._now() if callable(self._now) else None

    def _require_mode(self, mode):
        if mode not in SUPPORTED_ALIGNMENT_MODES:
            raise SystemAlignmentError("unsupported_mode", f"unsupported alignment mode: {mode!r}")

    @staticmethod
    def _raise_store(exc):
        raise SystemAlignmentError(exc.reason, exc.message) from exc

    def _current_record(self, operation_id=None):
        try:
            record = self._transitions.read()
        except TransitionStateError as exc:
            self._raise_store(exc)
        if record is None:
            raise SystemAlignmentError(
                "no_transition", "there is no system-build transition"
            )
        if operation_id is not None and record.operation_id != operation_id:
            raise SystemAlignmentError(
                "operation_mismatch", "the transition operation id does not match"
            )
        return record

    @staticmethod
    def _result(record, **extra):
        result = {
            "ok": True,
            "status": record.stage,
            "stage": record.stage,
            "operation_id": record.operation_id,
            "mode": record.mode,
            "system_tag": record.system_tag,
            "build_id": record.build_id,
            "admin_alignment_required": record.admin_alignment_required,
            "next_step": record.next_step,
            "development_risk_acknowledged": (
                record.development_risk_acknowledged
            ),
            "development_risk_acknowledged_for_tag": (
                record.development_risk_acknowledged_for_tag
            ),
        }
        result.update(extra)
        return result

    @staticmethod
    def _identity_dict(identity):
        if isinstance(identity, dict):
            return identity
        as_dict = getattr(identity, "as_dict", None)
        if callable(as_dict):
            return as_dict()
        return {
            "digest": getattr(identity, "digest", None),
            "build_id": getattr(identity, "build_id", None),
            "revision": getattr(identity, "revision", None),
        }

    def is_transition_pending(self) -> bool:
        """True when a non-terminal transition is persisted (blocks EMS mutations)."""

        try:
            record = self._transitions.read()
        except TransitionStateError:
            return True  # an unreadable/tampered record is treated as blocking
        return bool(record is not None and record.stage not in TERMINAL_TRANSITION_STAGES)

    def resources_verified(self) -> bool:
        """True once resources passed and while the operation remains forward-safe."""

        try:
            record = self._transitions.read()
        except TransitionStateError:
            return False
        if record is None:
            return False
        return record.stage in {
            TRANSITION_STAGE_RESOURCES_VERIFIED,
            TRANSITION_STAGE_EMS_OPERATION_PENDING,
            TRANSITION_STAGE_EMS_OPERATION_RUNNING,
            TRANSITION_STAGE_HEALTHCHECK_PENDING,
            TRANSITION_STAGE_COMPLETED,
        }

    def _resources_verified_for_build(self, build) -> bool:
        """True only when the verified transition is for exactly *this* build.

        A verified transition for a different build (a stale/other selection)
        never reports the currently-selected build's resources as ready, so Next
        cannot open on another build's verification.
        """

        if not self.resources_verified():
            return False
        try:
            record = self._transitions.read()
        except TransitionStateError:
            return False
        return bool(
            record is not None
            and record.system_tag == build.canonical_tag
            and record.build_id == build.build_id
            and record.revision == build.revision
            and record.admin_digest == build.admin_digest
            and record.ems_digest == build.ems_digest
        )

    def _embedded_resources_state(self, build, running) -> str:
        """Return ``"match"``, ``"mismatch"`` or ``"unknown"`` for the bundle.

        ``"unknown"`` when verification is not applicable (no verifier, or the
        running identity is not the build's) — that is not a definitive failure
        and must never force a recreate on its own.
        """

        verify = getattr(self._embedded, "verify", None)
        if not (
            callable(verify)
            and running.get("revision") == build.revision
            and running.get("build_id") == build.build_id
        ):
            return "unknown"
        try:
            verify(running_build=build.as_dict())
            return "match"
        except Exception:
            return "mismatch"

    def _effective_alignment(self, decision, embedded_state, build=None) -> str:
        """One alignment verdict shared by validate(), validate_upgrade_target()
        and start().

        A legacy release keeps the running modern Admin as the orchestration
        layer — it is never downgraded to the historical Admin image, so its
        alignment is a no-op regardless of the raw digest decision. Otherwise a
        matching Admin identity whose embedded resources verify as stale is a
        recreate, not a finished alignment: report it so Update Admin Server
        stays actionable. An ``unknown`` embedded state leaves the decision
        untouched.
        """

        if build is not None and system_build_keeps_current_admin(build):
            return ALIGN_ALIGNED
        if decision == ALIGN_ALIGNED and embedded_state == "mismatch":
            return ALIGN_ADMIN_RECREATE_REQUIRED
        return decision

    def _orchestrator_admin_identity(self, verdict) -> dict | None:
        """The orchestrator Admin override to persist, or ``None`` (modern paired).

        A legacy release keeps the running modern Admin as the orchestration
        layer, so that identity is recorded separately from the selected build's
        (historical, never-run) Admin image. A modern build has no override: the
        selected Admin becomes the running Admin.
        """

        if not verdict.keeps_current_admin:
            return None
        running = verdict.running
        return {
            "build_id": running.get("build_id"),
            "revision": running.get("revision"),
            "image": running.get("image_ref") or running.get("image"),
            "digest": running.get("digest"),
        }

    def _effective_decision(self, build, running_identity) -> EffectiveBuildDecision:
        """Compute the one shared compatibility/resource verdict for ``build``.

        Read-only: resolves alignment and the embedded-resource state but never
        opens a transition, imports resources or inspects Docker beyond the
        already-provided running identity.
        """

        running = self._identity_dict(running_identity)
        embedded_state = self._embedded_resources_state(build, running)
        decision = decide_alignment(
            running_identity, build, persistent_ref=self._persistent_ref()
        )
        effective_alignment = self._effective_alignment(
            decision.decision, embedded_state, build
        )
        return EffectiveBuildDecision(
            build=build,
            running=running,
            decision=decision.decision,
            effective_alignment=effective_alignment,
            embedded_state=embedded_state,
        )

    def status(self) -> dict:
        """Return a side-effect-free transition/known-good snapshot for polling."""

        try:
            record = self._transitions.read()
        except TransitionStateError as exc:
            return {
                "ok": False,
                "active": True,
                "error": exc.reason,
                "message": exc.message,
                "transition": None,
                "known_good": self._known_good.current(),
            }
        transition = None
        known_good = self._known_good.current()
        if record is not None:
            transition = record.as_dict()
            transition["resume_available"] = (
                record.stage
                in {
                    TRANSITION_STAGE_FAILED_RECOVERABLE,
                    TRANSITION_STAGE_EMS_OPERATION_PENDING,
                    TRANSITION_STAGE_EMS_OPERATION_RUNNING,
                    TRANSITION_STAGE_HEALTHCHECK_PENDING,
                }
            )
            try:
                running_ems = self._identity_dict(self._current_ems_identity())
            except Exception:
                running_ems = {}
            transition["return_available"] = bool(
                record.stage == TRANSITION_STAGE_FAILED_RECOVERABLE
                and isinstance(known_good, dict)
                and self._ems_identity_matches_known_good(running_ems, known_good)
            )
            # A recoverable transition whose only forward action (resume) keeps
            # failing must not wedge the console: abandoning it is the escape
            # when neither resume nor a known-good return can complete. Cancel is
            # the store's own cancellability authority, so surface it here so the
            # recovery UI can offer it for a guided_upgrade the same way the
            # backend already accepts it.
            transition["cancel_available"] = record.stage in CANCELLABLE_TRANSITION_STAGES
        return {
            "ok": True,
            "active": bool(
                record is not None and record.stage not in TERMINAL_TRANSITION_STAGES
            ),
            "transition": transition,
            "known_good": known_good,
        }

    @staticmethod
    def _selection_fingerprint(build) -> str:
        return ":".join(
            str(value or "")
            for value in (
                build.canonical_tag,
                build.channel,
                build.revision,
                build.build_id,
                build.admin_digest,
                build.ems_digest,
            )
        )

    def selection_fingerprint(self, build) -> str:
        """Public accessor for the verified-pair selection fingerprint.

        Both the verify (validate) path and the execute-time enforcement compute
        the fingerprint through this single helper, so their formats can never
        diverge.
        """

        return self._selection_fingerprint(build)

    def _validated_action_state(
        self,
        *,
        build,
        verdict,
        active,
        active_matches,
        resources_verified,
        base_ready,
        transition_error=None,
    ) -> dict:
        stage = active.stage if active not in {None, False} else None
        terminal_error = None
        busy = False
        polling_required = False
        progress_message = None
        admin_update_allowed = False
        continue_allowed = False

        if transition_error is not None:
            terminal_error = {
                "code": getattr(transition_error, "reason", "transition_state_invalid"),
                "message": getattr(
                    transition_error,
                    "message",
                    "The saved System Build transition cannot be read. "
                    "Review or clear the transition before retrying.",
                ),
            }
        elif active not in {None, False} and stage not in TERMINAL_TRANSITION_STAGES:
            if not active_matches:
                if active.system_tag == build.canonical_tag:
                    terminal_error = {
                        "code": "transition_stale_for_selected_build",
                        "message": (
                            f"System Build {build.canonical_tag} has changed since the "
                            "saved operation started. Cancel the saved operation, then "
                            "select and validate the build again."
                        ),
                    }
                else:
                    terminal_error = {
                        "code": "transition_active_for_another_build",
                        "message": (
                            f"System Build {active.system_tag} already owns the active "
                            "operation. Select that build again and finish its setup, or "
                            f"cancel the saved operation before selecting {build.canonical_tag}."
                        ),
                    }
            elif stage == TRANSITION_STAGE_FAILED_RECOVERABLE:
                terminal_error = {
                    "code": active.error_code or "system_build_recovery_required",
                    "message": active.error_message or (
                        "The System Build operation failed. Use the recovery actions "
                        "before continuing."
                    ),
                }
            elif resources_verified:
                continue_allowed = True
            elif stage in _ACTION_BUSY_STAGES:
                busy = True
                polling_required = True
                progress_message = _ACTION_PROGRESS_MESSAGES[stage]
            else:
                terminal_error = {
                    "code": "system_build_transition_blocked",
                    "message": (
                        f"The System Build operation is paused at {stage}. "
                        "Resume or cancel the saved operation before continuing."
                    ),
                }
        elif verdict.admin_update_required:
            admin_update_allowed = True
        elif base_ready:
            continue_allowed = True
        else:
            terminal_error = {
                "code": "system_build_resources_unavailable",
                "message": (
                    "The selected System Build resources are unavailable. "
                    "Verify the packaged resources or release archive and try again."
                ),
            }

        action_state = {
            "selected_build": {
                "tag": build.canonical_tag,
                "channel": build.channel,
                "revision": build.revision,
                "build_id": build.build_id,
            },
            "selection_fingerprint": self._selection_fingerprint(build),
            "compatibility_mode": verdict.compatibility_mode,
            "alignment_state": verdict.effective_alignment,
            "resource_strategy": verdict.resource_strategy,
            "resource_state": verdict.resource_status(verified=resources_verified),
            "admin_update_required": verdict.admin_update_required,
            "admin_update_allowed": admin_update_allowed,
            "continue_allowed": continue_allowed,
            "terminal_error": terminal_error,
            "busy": busy,
            "progress_message": progress_message,
            "polling_required": polling_required,
            "transition_stage": stage,
            "operation_id": (
                active.operation_id
                if active not in {None, False} and active_matches
                else None
            ),
        }
        actionable = int(admin_update_allowed) + int(continue_allowed)
        if terminal_error is None and not busy and actionable != 1:
            raise SystemAlignmentError(
                "system_build_action_state_invalid",
                "the selected System Build did not produce exactly one next action",
            )
        return action_state

    def validate(self, *, requested_tag) -> dict:
        """Return the authoritative alignment and Step 1 action gates."""

        build = self._resolver.resolve(requested_tag)
        running_identity = self._current_identity()
        verdict = self._effective_decision(build, running_identity)
        if verdict.is_mismatch:
            raise SystemAlignmentError(
                "system_build_mismatch", "the running Admin cannot be aligned to this build"
            )
        effective_alignment = verdict.effective_alignment
        embedded_matches = verdict.embedded_state == "match"
        resources_verified = self._resources_verified_for_build(build)
        transition_error = None
        try:
            active = self._transitions.read()
        except TransitionStateError as exc:
            active = False
            transition_error = exc
        active_matches = bool(
            active
            and active.system_tag == build.canonical_tag
            and active.build_id == build.build_id
            and active.revision == build.revision
            and active.admin_digest == build.admin_digest
            and active.ems_digest == build.ems_digest
        )
        # Readiness gates on the *selected resource strategy*, not on embedded
        # resources unconditionally: a legacy release is ready to prepare from its
        # historical archive even though the running Admin's embedded bundle can
        # never match it.
        base_ready = verdict.alignment_ready and verdict.resources_ready
        # Terminal records remain as history but own no active operation; only a
        # non-terminal record may reserve its build against another selection.
        no_active_transition = bool(
            active is None
            or (
                active is not False
                and active.stage in TERMINAL_TRANSITION_STAGES
            )
        )
        prepared_transition = bool(
            active not in {None, False}
            and active_matches
            and active.stage not in TERMINAL_TRANSITION_STAGES
            and resources_verified
        )
        confirmation_allowed = base_ready and no_active_transition
        next_allowed = base_ready and (
            confirmation_allowed or prepared_transition
        )
        transition_in_progress = bool(
            active is False
            or (
                active is not None
                and not no_active_transition
            )
        )
        action_state = self._validated_action_state(
            build=build,
            verdict=verdict,
            active=active,
            active_matches=active_matches,
            resources_verified=resources_verified,
            base_ready=base_ready,
            transition_error=transition_error,
        )
        # Compatibility fields mirror the normalized decision while older API
        # consumers migrate. The nested action_state remains the sole button gate.
        next_allowed = action_state["continue_allowed"]
        return {
            "ok": True,
            "status": "validated",
            "valid": True,
            "validation_state": "valid",
            "selected_tag": build.requested_tag,
            "system_build": build.as_dict(),
            "compatibility_mode": verdict.compatibility_mode,
            "resource_strategy": verdict.resource_strategy,
            "resource_status": verdict.resource_status(verified=resources_verified),
            "embedded_resources_applicable": verdict.embedded_applicable,
            "alignment": effective_alignment,
            "admin_update_required": verdict.admin_update_required,
            "admin_update_allowed": action_state["admin_update_allowed"],
            # ``None`` when embedded resources do not apply (a legacy release), so
            # a not-applicable check is never rendered as a failure.
            "embedded_resources_valid": verdict.embedded_valid,
            "resources_verified": resources_verified,
            "next_allowed": next_allowed,
            "confirmation_allowed": confirmation_allowed,
            "transition_in_progress": transition_in_progress,
            "transition_stage": (
                active.stage if active not in {None, False} else None
            ),
            "recovery_required": bool(
                active not in {None, False}
                and active.stage == TRANSITION_STAGE_FAILED_RECOVERABLE
            ),
            "operation_id": (
                active.operation_id if prepared_transition else None
            ),
            "development_risk_acknowledged": bool(
                active not in {None, False}
                and active_matches
                and active.stage not in TERMINAL_TRANSITION_STAGES
                and active.stage != TRANSITION_STAGE_FAILED_RECOVERABLE
                and active.development_risk_acknowledged
                and active.development_risk_acknowledged_for_tag
                == build.canonical_tag
            ),
            "development_risk_acknowledged_for_tag": (
                active.development_risk_acknowledged_for_tag
                if active not in {None, False}
                and active_matches
                and active.stage not in TERMINAL_TRANSITION_STAGES
                and active.stage != TRANSITION_STAGE_FAILED_RECOVERABLE
                else None
            ),
            "active_transition_tag": (
                active.system_tag
                if active not in {None, False} and transition_in_progress
                else None
            ),
            "action_state": action_state,
            "summary": {
                "channel": build.channel,
                "revision": build.revision,
                "build_id": build.build_id,
                "admin_image": build.admin_image,
                "ems_image": build.ems_image,
            },
            "checks": {
                "admin_image_available": True,
                "ems_image_available": True,
                "revision_matches": True,
                "build_id_matches": True,
                "channel_matches": True,
                "embedded_resources_match": embedded_matches,
            },
        }

    def validate_upgrade_target(self, *, requested_tag) -> dict:
        """Read-only Guided Upgrade validation: resolve, align, check direction.

        Resolves the requested tag into a verified Admin/EMS pair, decides how
        the running Admin aligns to it, and assesses the upgrade/downgrade
        direction against the running EMS identity. It never opens a transition,
        imports resources, writes config or touches containers.
        """

        build = self._resolver.resolve(requested_tag)
        running_identity = self._current_identity()
        # One effective decision, identical to execution and validate(): a
        # matching Admin with stale embedded resources previews as
        # admin_recreate_required instead of "ready", and a legacy release keeps
        # the modern Admin (never downgraded). Read-only: no import, no Docker.
        verdict = self._effective_decision(build, running_identity)
        if verdict.is_mismatch:
            raise SystemAlignmentError(
                "system_build_mismatch",
                "the running Admin cannot be aligned to this build",
            )
        try:
            running_ems = self._current_ems_identity()
        except Exception:
            running_ems = None
        direction = decide_upgrade_direction(running_ems, build)
        effective_alignment = verdict.effective_alignment
        return {
            "ok": True,
            "status": "validated",
            "valid": True,
            "selected_tag": build.requested_tag,
            "system_build": build.as_dict(),
            # Same verified-build fingerprint the Fresh Install validate emits, so
            # a Guided Upgrade plan can be bound to the exact resolved pair and
            # invalidated when the tag, revision, build id, channel or a digest
            # changes.
            "selection_fingerprint": self._selection_fingerprint(build),
            "compatibility_mode": verdict.compatibility_mode,
            "resource_strategy": verdict.resource_strategy,
            "current_admin": self._current_admin_summary(running_identity),
            "alignment": effective_alignment,
            "admin_update_required": verdict.admin_update_required,
            "upgrade_allowed": direction.allowed,
            "upgrade_state": direction.state,
            "upgrade_direction": direction.as_dict(),
        }

    def _current_admin_summary(self, running_identity) -> dict:
        """Summarize the running Admin's own identity (never the EMS build)."""

        running = self._identity_dict(running_identity)
        return {
            "system_tag": running.get("version_label") or running.get("release_tag"),
            "image": running.get("image_ref"),
            "digest": running.get("digest"),
            "build_id": running.get("build_id"),
            "revision": running.get("revision"),
        }

    def return_to_running_build(self, *, operation_id, confirm) -> dict:
        """Align Admin to the verified build of the EMS container now running."""

        if confirm is not True:
            raise SystemAlignmentError(
                "confirmation_required", "returning Admin requires confirmation"
            )
        current = self._current_record(operation_id)
        if current.stage != TRANSITION_STAGE_FAILED_RECOVERABLE:
            raise SystemAlignmentError(
                "invalid_transition",
                "Admin can return to the running EMS build only after a recoverable failure",
            )
        known_good = self._known_good.current()
        if not isinstance(known_good, dict):
            raise SystemAlignmentError(
                "known_good_unavailable", "there is no known-good System Build"
            )
        try:
            validate_system_build_id(known_good.get("build_id"))
            target_tag = known_good["system_tag"]
        except (KeyError, TypeError, ValueError) as exc:
            raise SystemAlignmentError(
                "known_good_invalid", "the known-good System Build is invalid"
            ) from exc

        try:
            running_ems = self._identity_dict(self._current_ems_identity())
        except Exception as exc:
            raise SystemAlignmentError(
                "running_ems_unverifiable",
                "the running EMS build could not be inspected",
            ) from exc
        if self._ems_identity_matches_transition(running_ems, current):
            raise SystemAlignmentError(
                "admin_already_matches_running_ems",
                "Admin already matches the EMS build currently running",
            )
        if not self._ems_identity_matches_known_good(running_ems, known_good):
            raise SystemAlignmentError(
                "running_ems_mismatch",
                "the running EMS build does not match the verified known-good build",
            )

        # Resolve and compare before cancelling the partial transition, so an
        # unavailable/changed rollback target never removes the recovery gate.
        target = self._resolver.resolve(target_tag)
        if not self._known_good_matches_build(known_good, target):
            raise SystemAlignmentError(
                "known_good_mismatch",
                "the available rollback images no longer match known-good",
            )
        try:
            self._transitions.cancel(
                operation_id=operation_id, now=self._now_value()
            )
        except TransitionStateError as exc:
            self._raise_store(exc)
        result = self._start_resolved(
            build=target,
            mode=TRANSITION_MODE_ALIGN_EXISTING,
        )
        result["target_system_tag"] = target_tag
        result["status"] = "admin_return_started"
        return result

    # --- start -----------------------------------------------------------

    def start(
        self,
        *,
        requested_tag,
        mode,
        development_risk_acknowledged=False,
    ) -> dict:
        """Resolve a requested pair, then enter/reuse its durable transition."""

        self._require_mode(mode)
        build = self.resolve(requested_tag)
        return self.start_resolved(
            system_build=build,
            mode=mode,
            development_risk_acknowledged=development_risk_acknowledged,
        )

    def resolve(self, requested_tag):
        """Resolve one immutable pair without persisting or launching work."""

        return self._resolver.resolve(requested_tag)

    def transition_build(self, *, operation_id):
        """Return the verified pair bound to the durable transition.

        A resume must never re-resolve the mutable tag: a replacement Admin has
        a fresh (empty) resolver cache, so a tag moved since verification would
        pull a different digest. The durable transition already pins the verified
        Admin/EMS pair, so the runtime identity is reconstructed from it and stays
        digest-bound across the Admin replacement.
        """

        return self._build_from_record(self._current_record(operation_id))

    def prepare_setup_resources(
        self,
        *,
        requested_tag,
        mode,
        development_risk_acknowledged=False,
    ) -> dict:
        """Compatibility wrapper for explicit resource preparation."""

        return self.confirm_setup_build(
            requested_tag=requested_tag,
            mode=mode,
            development_risk_acknowledged=development_risk_acknowledged,
        )

    def confirm_setup_build(
        self,
        *,
        requested_tag,
        mode,
        development_risk_acknowledged=False,
    ) -> dict:
        """Commit an aligned Setup build and verify/import its resources."""

        self._require_mode(mode)
        build = self.resolve(requested_tag)
        # Confirmation reuses the exact effective decision validate() showed: a
        # legacy release keeps the running modern Admin, so it is aligned here
        # without an Admin update; a modern build that is not yet aligned is
        # refused before any transition is opened.
        verdict = self._effective_decision(build, self._current_identity())
        if verdict.is_mismatch:
            raise SystemAlignmentError(
                "system_build_mismatch",
                "the running Admin cannot be aligned to this build",
            )
        if not verdict.alignment_ready:
            raise SystemAlignmentError(
                "system_build_alignment_required",
                "align the Admin to the selected System Build before preparing resources",
            )
        result = self._start_resolved(
            build=build,
            mode=mode,
            development_risk_acknowledged=development_risk_acknowledged,
        )
        operation_id = result.get("operation_id")
        if result.get("stage") == TRANSITION_STAGE_ADMIN_ALIGNED and operation_id:
            verified = self.verify_resources(operation_id=operation_id)
            result = {**result, **verified, "system_build": build.as_dict()}
        result.setdefault("system_build", build.as_dict())
        result["resources_verified"] = self._resources_verified_for_build(build)
        result["next_allowed"] = result["resources_verified"]
        return result

    def validate_setup_discovery_operation(self, *, operation_id) -> dict:
        """Authorize one Guided Setup discovery mutation."""

        if not operation_id:
            raise SystemAlignmentError(
                "setup_operation_required", "a confirmed Setup operation id is required"
            )
        record = self._current_record(operation_id)
        if (
            record.mode
            not in {TRANSITION_MODE_FRESH_INSTALL, TRANSITION_MODE_AUTOMATED_SETUP}
            or record.stage != TRANSITION_STAGE_RESOURCES_VERIFIED
        ):
            raise SystemAlignmentError(
                "system_alignment_incomplete",
                "Setup discovery requires a resource-verified Setup operation",
            )
        build = self.resolve(record.system_tag)
        if not self._record_matches_build(record, build):
            raise SystemAlignmentError(
                "system_build_mismatch",
                "the confirmed Setup operation no longer matches its System Build",
            )
        # The running Admin must match the authorised *orchestrator* identity, not
        # the selected EMS build: for a legacy release the orchestrator is the
        # running modern Admin, which can never equal the historical build id.
        running = self._identity_dict(self._current_identity())
        orchestrator = record.orchestrator_admin
        if (
            running.get("build_id") != orchestrator["build_id"]
            or running.get("revision") != orchestrator["revision"]
            or running.get("digest") != orchestrator["digest"]
        ):
            raise SystemAlignmentError(
                "system_build_mismatch",
                "the running Admin no longer matches the confirmed Setup operation",
            )
        return record.as_dict()

    @staticmethod
    def _record_matches_build(record, build):
        return all(
            (
                record.system_tag == build.canonical_tag,
                record.build_id == build.build_id,
                record.revision == build.revision,
                record.admin_image == build.admin_image,
                record.admin_digest == build.admin_digest,
                record.ems_image == build.ems_image,
                record.ems_digest == build.ems_digest,
            )
        )

    @staticmethod
    def _development_build(build):
        return classify_channel(build.canonical_tag) == "development"

    def _require_explicit_development_acknowledgement(self, build, acknowledged):
        if self._development_build(build) and acknowledged is not True:
            raise SystemAlignmentError(
                "acknowledgement_required",
                "Development System Builds require explicit risk acknowledgement",
            )

    def _resolved_transition_build(self, record):
        build = self.resolve(record.system_tag)
        if not self._record_matches_build(record, build):
            raise SystemAlignmentError(
                "transition_context_mismatch",
                "the active transition no longer matches its resolved System Build",
            )
        return build

    def _require_stored_development_acknowledgement(self, record, build):
        if not self._development_build(build):
            return
        if not (
            record.development_risk_acknowledged is True
            and record.development_risk_acknowledged_for_tag
            == record.system_tag
            == build.canonical_tag
        ):
            raise SystemAlignmentError(
                "acknowledgement_required",
                "Development System Build acknowledgement is missing or mismatched",
            )

    def development_acknowledgement_allows_automatic_resume(
        self, *, requested_tag
    ) -> bool:
        try:
            record = self._transitions.read()
            if (
                record is None
                or record.stage in TERMINAL_TRANSITION_STAGES
                or record.stage == TRANSITION_STAGE_FAILED_RECOVERABLE
            ):
                return False
            build = self.resolve(requested_tag)
            if not self._record_matches_build(record, build):
                return False
            self._require_stored_development_acknowledgement(record, build)
        except (SystemBuildError, SystemAlignmentError, TransitionStateError):
            return False
        return True

    def start_resolved(
        self,
        *,
        system_build,
        mode,
        request_fingerprint=None,
        development_risk_acknowledged=False,
        pre_launch=None,
    ) -> dict:
        """Start/reuse a transition for an already-resolved verified pair.

        ``pre_launch`` is an optional callback invoked with the new transition
        record after the operation id is minted but *before* the transition is
        committed and any Admin-replacement launcher runs. If it raises, the
        transition is never committed and nothing is launched — the caller can
        persist durable context and fail closed on a persistence error.
        """

        self._require_mode(mode)
        return self._start_resolved(
            build=system_build,
            mode=mode,
            request_fingerprint=request_fingerprint,
            development_risk_acknowledged=development_risk_acknowledged,
            pre_launch=pre_launch,
        )

    def _start_resolved(
        self,
        *,
        build,
        mode,
        request_fingerprint=None,
        development_risk_acknowledged=False,
        pre_launch=None,
    ) -> dict:
        """Proceed with an aligned pair or start Admin alignment."""

        self._require_explicit_development_acknowledgement(
            build, development_risk_acknowledged
        )
        try:
            existing = self._transitions.read()
        except TransitionStateError as exc:
            self._raise_store(exc)
        if existing is not None and existing.stage not in TERMINAL_TRANSITION_STAGES:
            same_operation = (
                existing.mode == mode
                and existing.system_tag == build.canonical_tag
                and existing.build_id == build.build_id
                and existing.revision == build.revision
                and existing.admin_image == build.admin_image
                and existing.admin_digest == build.admin_digest
                and existing.ems_image == build.ems_image
                and existing.ems_digest == build.ems_digest
            )
            if not same_operation:
                raise SystemAlignmentError(
                    "transition_active",
                    "another system-build transition is already in progress",
                )
            if existing.request_fingerprint != request_fingerprint:
                raise SystemAlignmentError(
                    "transition_context_mismatch",
                    "the resumed operation options differ from the prepared transition",
                )
            return self._result(
                existing,
                system_build=build.as_dict(),
                reconnect=existing.stage in {
                    TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
                    TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
                },
                config_written=False,
                ems_started=False,
            )

        running = self._current_identity()
        verdict = self._effective_decision(build, running)
        if verdict.is_mismatch:
            raise SystemAlignmentError(
                "system_build_mismatch", "running Admin cannot be aligned to this build"
            )
        effective_alignment = verdict.effective_alignment

        # Every operation is persisted, including an already-aligned Admin, so
        # resource/config/EMS/health work is covered by the same durable gate.
        initial_stage = (
            TRANSITION_STAGE_ADMIN_ALIGNED
            if effective_alignment == ALIGN_ALIGNED
            else TRANSITION_STAGE_ADMIN_UPDATE_PENDING
        )
        record = make_transition_record(
            mode=mode,
            system_tag=build.canonical_tag,
            build_id=build.build_id,
            revision=build.revision,
            admin_image=build.admin_image,
            admin_digest=build.admin_digest,
            ems_image=build.ems_image,
            ems_digest=build.ems_digest,
            stage=initial_stage,
            admin_alignment_required=effective_alignment != ALIGN_ALIGNED,
            compatibility_mode=verdict.compatibility_mode,
            resource_strategy=verdict.resource_strategy,
            orchestrator_admin=self._orchestrator_admin_identity(verdict),
            request_fingerprint=request_fingerprint,
            development_risk_acknowledged=(
                self._development_build(build)
                and development_risk_acknowledged is True
            ),
            development_risk_acknowledged_for_tag=(
                build.canonical_tag
                if self._development_build(build)
                and development_risk_acknowledged is True
                else None
            ),
            now=self._now_value(),
        )
        # Persist any durable execution context before committing the transition
        # or launching an Admin replacement. A failure here leaves no active
        # transition and never launches a replacement.
        if pre_launch is not None:
            pre_launch(record)
        try:
            self._transitions.begin(record, now=self._now_value())
        except TransitionStateError as exc:
            self._raise_store(exc)

        if effective_alignment == ALIGN_ALIGNED:
            return self._result(
                record,
                decision=effective_alignment,
                system_build=build.as_dict(),
                config_written=False,
                ems_started=False,
            )

        # Commit the reconnect wait before launching. The sidecar may begin
        # immediately, and must always see a state it can fail recoverably.
        try:
            waiting = self._transitions.advance(
                record.operation_id,
                expected_stage=TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
                new_stage=TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
                now=self._now_value(),
            )
        except TransitionStateError as exc:
            self._raise_store(exc)
        try:
            self._launcher(record)
        except Exception as exc:
            try:
                self._transitions.mark_failed(
                    record.operation_id,
                    error_code="admin_update_launch_failed",
                    error_message=str(exc) or "Admin update could not be launched",
                    resume_stage=TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
                    now=self._now_value(),
                )
            except TransitionStateError:
                pass
            raise SystemAlignmentError(
                "admin_update_launch_failed",
                f"Admin update could not be launched: {exc}",
            ) from exc
        return {
            "ok": True,
            "status": "admin_alignment_started",
            "stage": waiting.stage,
            "decision": effective_alignment,
            "reconnect": True,
            "system_build": build.as_dict(),
            "operation_id": record.operation_id,
            "config_written": False,
            "ems_started": False,
        }

    # --- resume ----------------------------------------------------------

    def resume(self, operation_id=None, running_admin=None) -> dict:
        """Verify the reconnected Admin and stop at ``admin_aligned``.

        ``running_admin`` is injectable for tests. Productive HTTP callers pass
        only the operation id; identity is detected by the bound dependency.
        """

        if operation_id is None:
            operation_id = self._current_record().operation_id
        current = self._current_record(operation_id)
        build = self._resolved_transition_build(current)
        self._require_stored_development_acknowledgement(current, build)
        if running_admin is None:
            running_admin = self._current_identity()
        try:
            record = self._transitions.resume_after_admin_reconnect(
                operation_id,
                running_admin=self._identity_dict(running_admin),
                now=self._now_value(),
            )
        except TransitionStateError as exc:
            self._raise_store(exc)
        return self._result(record)

    def _resource_provider_for(self, build):
        """Return the resource provider for ``build``'s strategy, or fail closed.

        An embedded build verifies the running Admin's bundle; a legacy release
        prepares its resources from the exact historical archive. A strategy with
        no configured provider is refused rather than falling back — main-branch
        resources are never substituted for a historical release.
        """

        strategy = system_build_resource_strategy(build)
        if strategy == _STRATEGY_RELEASE_ARCHIVE:
            if self._release_archive is None:
                raise SystemAlignmentError(
                    "system_build_resources_invalid",
                    "release-archive resource preparation is unavailable",
                )
            return self._release_archive
        return self._embedded

    def verify_resources(self, *, operation_id) -> dict:
        """Verify/prepare the selected build's resources once, after Admin aligns.

        The resource *source* follows the build's compatibility mode: a modern
        paired build verifies the embedded bundle, a legacy release prepares the
        exact historical release archive.
        """

        record = self._current_record(operation_id)
        build = self._resolved_transition_build(record)
        self._require_stored_development_acknowledgement(record, build)
        if record.stage == TRANSITION_STAGE_RESOURCES_VERIFIED:
            return self._result(record)
        if record.stage != TRANSITION_STAGE_ADMIN_ALIGNED:
            raise SystemAlignmentError(
                "invalid_transition",
                f"resources cannot be verified while transition is {record.stage}",
            )
        try:
            claimed = self._transitions.claim_resource_verification(
                operation_id, now=self._now_value()
            )
        except TransitionStateError as exc:
            self._raise_store(exc)
        if not claimed:
            current = self._current_record(operation_id)
            if current.stage == TRANSITION_STAGE_RESOURCES_VERIFIED:
                return self._result(current)
            raise SystemAlignmentError(
                "resource_verification_in_progress",
                "embedded resources are already being verified",
            )
        try:
            provider = self._resource_provider_for(build)
            provider.import_into_cache(
                running_build=self._build_from_record(record).as_dict()
            )
        except Exception as exc:
            message = f"System Build resources could not be prepared: {exc}"
            try:
                self._transitions.mark_failed(
                    operation_id,
                    error_code="system_build_resources_invalid",
                    error_message=message,
                    resume_stage=TRANSITION_STAGE_ADMIN_ALIGNED,
                    now=self._now_value(),
                )
            except TransitionStateError as state_exc:
                self._raise_store(state_exc)
            raise SystemAlignmentError(
                "system_build_resources_invalid", message
            ) from exc
        try:
            verified = self._transitions.advance(
                operation_id,
                expected_stage=TRANSITION_STAGE_ADMIN_ALIGNED,
                new_stage=TRANSITION_STAGE_RESOURCES_VERIFIED,
                now=self._now_value(),
            )
        except TransitionStateError as exc:
            self._raise_store(exc)
        return self._result(verified)

    def retry(
        self,
        *,
        operation_id,
        development_risk_acknowledged=False,
    ) -> dict:
        current = self._current_record(operation_id)
        build = self._resolved_transition_build(current)
        self._require_explicit_development_acknowledgement(
            build, development_risk_acknowledged
        )
        self._require_stored_development_acknowledgement(current, build)
        try:
            record = self._transitions.retry(operation_id, now=self._now_value())
        except TransitionStateError as exc:
            self._raise_store(exc)
        if record.stage == TRANSITION_STAGE_ADMIN_UPDATE_PENDING:
            try:
                waiting = self._transitions.advance(
                    operation_id,
                    expected_stage=TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
                    new_stage=TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
                    now=self._now_value(),
                )
                self._launcher(record)
            except Exception as exc:
                try:
                    self._transitions.mark_failed(
                        operation_id,
                        error_code="admin_update_launch_failed",
                        error_message=str(exc) or "Admin update could not be launched",
                        resume_stage=TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
                        now=self._now_value(),
                    )
                except TransitionStateError:
                    pass
                raise SystemAlignmentError(
                    "admin_update_launch_failed",
                    f"Admin update could not be launched: {exc}",
                ) from exc
            return self._result(waiting, reconnect=True)
        return self._result(record)

    def begin_ems_operation(self, *, operation_id) -> dict:
        try:
            self._transitions.assert_fresh(
                operation_id, now=self._now_value()
            )
        except TransitionStateError as exc:
            self._raise_store(exc)
        try:
            record = self._transitions.advance(
                operation_id,
                expected_stage=TRANSITION_STAGE_RESOURCES_VERIFIED,
                new_stage=TRANSITION_STAGE_EMS_OPERATION_PENDING,
                now=self._now_value(),
            )
        except TransitionStateError as exc:
            self._raise_store(exc)
        return self._result(record)

    def claim_ems_operation(self, *, operation_id) -> bool:
        try:
            return self._transitions.claim(
                operation_id,
                expected_stage=TRANSITION_STAGE_EMS_OPERATION_PENDING,
                new_stage=TRANSITION_STAGE_EMS_OPERATION_RUNNING,
                now=self._now_value(),
            )
        except TransitionStateError as exc:
            self._raise_store(exc)

    def finish_ems_operation(
        self,
        *,
        operation_id,
        succeeded,
        error_code=None,
        error_message=None,
    ) -> dict:
        if not succeeded:
            try:
                record = self._transitions.mark_failed(
                    operation_id,
                    error_code=error_code or "ems_deployment_failed",
                    error_message=error_message or "EMS deployment failed",
                    resume_stage=TRANSITION_STAGE_EMS_OPERATION_PENDING,
                    now=self._now_value(),
                )
            except TransitionStateError as exc:
                self._raise_store(exc)
            return self._result(record)
        try:
            record = self._transitions.advance(
                operation_id,
                expected_stage=TRANSITION_STAGE_EMS_OPERATION_RUNNING,
                new_stage=TRANSITION_STAGE_HEALTHCHECK_PENDING,
                now=self._now_value(),
            )
        except TransitionStateError as exc:
            self._raise_store(exc)
        return self._result(record)

    def recover_ems_operation(
        self,
        *,
        operation_id,
        healthcheck_passed=None,
        healthcheck_error_code=None,
        healthcheck_error_message=None,
    ) -> dict:
        """Reconcile durable EMS stages after the Admin process restarts.

        A committed pending intent is simply returned for the normal route to
        claim. An abandoned running claim is never executed twice: an exact
        target container advances to health checks, while an old/unknown EMS is
        made explicitly recoverable at ``ems_operation_pending``. Health-only
        retries never reclaim or redeploy EMS.
        """

        record = self._current_record(operation_id)
        if record.stage == TRANSITION_STAGE_FAILED_RECOVERABLE:
            try:
                record = self._transitions.retry(
                    operation_id, now=self._now_value()
                )
            except TransitionStateError as exc:
                self._raise_store(exc)

        if record.stage in {
            TRANSITION_STAGE_RESOURCES_VERIFIED,
            TRANSITION_STAGE_EMS_OPERATION_PENDING,
        }:
            return self._result(record)

        if record.stage == TRANSITION_STAGE_EMS_OPERATION_RUNNING:
            identity_state = self._running_ems_identity_state(record)
            if identity_state == "match":
                return self.finish_ems_operation(
                    operation_id=operation_id, succeeded=True
                )
            try:
                failed = self._transitions.mark_failed(
                    operation_id,
                    error_code="ems_operation_interrupted",
                    error_message=(
                        "The interrupted EMS operation did not leave the exact "
                        "target System Build running. Retry deployment explicitly."
                    ),
                    resume_stage=TRANSITION_STAGE_EMS_OPERATION_PENDING,
                    now=self._now_value(),
                )
            except TransitionStateError as exc:
                self._raise_store(exc)
            return self._result(failed)

        if record.stage == TRANSITION_STAGE_HEALTHCHECK_PENDING:
            if healthcheck_passed is None:
                return self._result(record)
            return self.finish_healthcheck(
                operation_id=operation_id,
                passed=bool(healthcheck_passed),
                error_code=healthcheck_error_code,
                error_message=healthcheck_error_message,
            )

        raise SystemAlignmentError(
            "invalid_transition",
            f"EMS recovery cannot continue while transition is {record.stage}",
        )

    def finish_healthcheck(
        self,
        *,
        operation_id,
        passed,
        system_build=None,
        error_code=None,
        error_message=None,
    ) -> dict:
        record = self._current_record(operation_id)
        if record.stage in TERMINAL_TRANSITION_STAGES:
            raise SystemAlignmentError(
                "not_resumable", f"transition is {record.stage}; it cannot restart"
            )
        if not passed:
            try:
                failed = self._transitions.mark_failed(
                    operation_id,
                    error_code=error_code or "healthcheck_failed",
                    error_message=error_message or "EMS health checks failed",
                    resume_stage=TRANSITION_STAGE_HEALTHCHECK_PENDING,
                    now=self._now_value(),
                )
            except TransitionStateError as exc:
                self._raise_store(exc)
            return self._result(failed)
        if record.stage != TRANSITION_STAGE_HEALTHCHECK_PENDING:
            raise SystemAlignmentError(
                "healthcheck_required", "known-good requires pending health checks"
            )
        identity_state = self._running_ems_identity_state(record)
        if identity_state != "match":
            code = (
                "ems_identity_unverifiable"
                if identity_state == "unverifiable"
                else "ems_identity_mismatch"
            )
            resume_stage = (
                TRANSITION_STAGE_HEALTHCHECK_PENDING
                if identity_state == "unverifiable"
                else TRANSITION_STAGE_EMS_OPERATION_PENDING
            )
            message = (
                "The running EMS identity could not be inspected."
                if identity_state == "unverifiable"
                else "The running EMS does not match the target System Build."
            )
            try:
                failed = self._transitions.mark_failed(
                    operation_id,
                    error_code=code,
                    error_message=message,
                    resume_stage=resume_stage,
                    now=self._now_value(),
                )
            except TransitionStateError as exc:
                self._raise_store(exc)
            return self._result(failed)
        if system_build is None:
            system_build = self._build_from_record(record)
        self._require_matching_build(record, system_build)

        # Cross-file ordering is deliberate: if the process stops between these
        # writes, retry sees the matching known-good and only commits completed.
        known_good = self._known_good.current()
        if not self._known_good_matches(known_good, record):
            self._record_known_good(system_build, record)
        try:
            completed = self._transitions.advance(
                operation_id,
                expected_stage=TRANSITION_STAGE_HEALTHCHECK_PENDING,
                new_stage=TRANSITION_STAGE_COMPLETED,
                now=self._now_value(),
            )
        except TransitionStateError as exc:
            self._raise_store(exc)
        return self._result(completed)

    # --- known-good ------------------------------------------------------

    def mark_known_good(self, *, system_build, healthcheck_passed) -> dict:
        """Persist ``system_build`` as known-good only after health checks pass."""

        if not healthcheck_passed:
            raise SystemAlignmentError(
                "healthcheck_required", "known-good requires passing health checks"
            )
        record = self._current_record()
        if record.stage != TRANSITION_STAGE_HEALTHCHECK_PENDING:
            raise SystemAlignmentError(
                "healthcheck_required",
                "known-good can only be written after EMS deployment and health checks",
            )
        identity_state = self._running_ems_identity_state(record)
        if identity_state != "match":
            raise SystemAlignmentError(
                (
                    "ems_identity_unverifiable"
                    if identity_state == "unverifiable"
                    else "ems_identity_mismatch"
                ),
                "known-good requires the exact running EMS System Build",
            )
        self._require_matching_build(record, system_build)
        return self._record_known_good(system_build, record)

    def _record_known_good(self, system_build, record) -> dict:
        """Persist known-good with the running orchestrator Admin separated out.

        The installed EMS build comes from ``system_build``; the running Admin
        identity comes from the transition's orchestrator, so a legacy install
        records the real modern Admin, never the historical Admin image.
        """

        return self._known_good.record(
            system_build,
            orchestrator_admin=record.orchestrator_admin,
            compatibility_mode=record.compatibility_mode,
            resource_strategy=record.resource_strategy,
        )

    def cancel(self, *, operation_id) -> dict:
        try:
            record = self._transitions.cancel(
                operation_id=operation_id, now=self._now_value()
            )
        except TransitionStateError as exc:
            self._raise_store(exc)
        if record is None:
            raise SystemAlignmentError(
                "no_transition", "there is no system-build transition"
            )
        return self._result(record)

    # --- internals -------------------------------------------------------

    @staticmethod
    def _build_from_record(record):
        channel = classify_channel(record.system_tag)
        return SystemBuild(
            requested_tag=record.system_tag,
            canonical_tag=record.system_tag,
            channel=channel,
            revision=record.revision,
            build_id=record.build_id,
            admin_image=record.admin_image,
            admin_digest=record.admin_digest,
            ems_image=record.ems_image,
            ems_digest=record.ems_digest,
            # OCI release_tag identifies the canonical install target for every
            # paired artifact (stable, RC, latest, development and local).
            release_tag=record.system_tag,
        )

    @staticmethod
    def _require_matching_build(record, system_build):
        try:
            build_id = validate_system_build_id(system_build.build_id)
        except (AttributeError, TypeError, ValueError) as exc:
            raise SystemAlignmentError(
                "system_build_mismatch", "healthchecked System Build is invalid"
            ) from exc
        fields = (
            (record.system_tag, getattr(system_build, "canonical_tag", None)),
            (record.build_id, build_id),
            (record.revision, getattr(system_build, "revision", None)),
            (record.admin_image, getattr(system_build, "admin_image", None)),
            (record.admin_digest, getattr(system_build, "admin_digest", None)),
            (record.ems_image, getattr(system_build, "ems_image", None)),
            (record.ems_digest, getattr(system_build, "ems_digest", None)),
        )
        if any(expected != actual for expected, actual in fields):
            raise SystemAlignmentError(
                "system_build_mismatch",
                "healthchecked System Build does not match the active transition",
            )

    @staticmethod
    def _known_good_matches(known_good, record):
        # Bind on the installed EMS System Build identity (ems digest + selected
        # build id/revision/tag). The running Admin is the orchestrator, which
        # differs from the selected build's Admin for a legacy release, so it is
        # compared separately rather than folded into this build-identity check.
        if not isinstance(known_good, dict):
            return False
        if known_good.get("admin_digest") != record.orchestrator_admin["digest"]:
            return False
        return all(
            known_good.get(field) == getattr(record, field)
            for field in (
                "build_id",
                "revision",
                "ems_image",
                "ems_digest",
            )
        ) and known_good.get("system_tag") == record.system_tag

    @staticmethod
    def _known_good_matches_build(known_good, build):
        # The rollback target's EMS System Build must equal known-good. The
        # historical Admin image of a legacy build is never run, so it is not
        # compared here (known-good records the running orchestrator Admin).
        return all(
            known_good.get(field) == getattr(build, field)
            for field in (
                "build_id",
                "revision",
                "ems_image",
                "ems_digest",
            )
        ) and known_good.get("system_tag") == build.canonical_tag

    @staticmethod
    def _ems_identity_matches_transition(identity, record):
        return bool(
            identity.get("digest")
            and identity.get("digest") == record.ems_digest
            and identity.get("build_id") == record.build_id
            and identity.get("revision") == record.revision
            and identity.get("channel") == classify_channel(record.system_tag)
            and identity.get("release_tag") == record.system_tag
        )

    def _running_ems_identity_state(self, record):
        """Return ``match``, ``mismatch`` or ``unverifiable`` for live EMS."""

        try:
            identity = self._identity_dict(self._current_ems_identity())
        except Exception:
            return "unverifiable"
        required = (
            "digest",
            "revision",
            "build_id",
            "channel",
            "release_tag",
        )
        if any(not identity.get(field) for field in required):
            return "unverifiable"
        return (
            "match"
            if self._ems_identity_matches_transition(identity, record)
            else "mismatch"
        )

    @staticmethod
    def _ems_identity_matches_known_good(identity, known_good):
        tag = known_good.get("system_tag")
        return bool(
            identity.get("digest")
            and identity.get("digest") == known_good.get("ems_digest")
            and identity.get("build_id") == known_good.get("build_id")
            and identity.get("revision") == known_good.get("revision")
            and identity.get("channel") == classify_channel(tag)
            and identity.get("release_tag") == tag
        )
