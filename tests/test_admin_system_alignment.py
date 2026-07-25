# SPDX-License-Identifier: AGPL-3.0-or-later
"""One orchestration service that aligns Admin to a verified SystemBuild first.

Fresh Setup, Automated Setup, Guided Upgrade and align-existing all funnel through
:class:`SystemAlignmentService`: resolve+verify the Admin/EMS pair, compare the
running Admin and the persistent Compose ref, and only when Admin is aligned (and
the embedded resources verify) allow the EMS step. Known-good is written only
after Admin + EMS are verified and health checks pass.
"""

import dataclasses
import threading
import time
from datetime import datetime, timezone

import pytest

from admin.admin_update import (
    ADMIN_IMAGE_REPO,
    EMS_IMAGE_REPO,
    PendingTransitionStore,
    TransitionStateError,
    make_transition_record,
)
from admin.guided_upgrade import UpgradeJob, UpgradeJobRegistry
from admin.operation_coordinator import (
    OperationCoordinator,
    OperationWorkerStatusUnavailable,
)
from admin.image_identity import ImageIdentity
from admin.known_good import KnownGoodStore
from admin.system_alignment import (
    SystemAlignmentError,
    SystemAlignmentService,
    terminal_system_build_action_state,
)
from admin.system_build import SystemBuild, SystemBuildError
from admin.system_health import validate_system_health_result

pytestmark = [pytest.mark.simulation, pytest.mark.system_build]

REVISION = "f7265fc747c2223f126f0ee7801e030c6226edf4"
T0 = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)


def _no_worker(_operation_id):
    return False

STAGE_ADMIN_UPDATE_PENDING = "admin_update_pending"
STAGE_ADMIN_RECONNECT_PENDING = "admin_reconnect_pending"
STAGE_ADMIN_ALIGNED = "admin_aligned"
STAGE_RESOURCES_VERIFIED = "resources_verified"
STAGE_EMS_OPERATION_PENDING = "ems_operation_pending"
STAGE_EMS_OPERATION_RUNNING = "ems_operation_running"
STAGE_HEALTHCHECK_PENDING = "healthcheck_pending"
STAGE_FAILED_RECOVERABLE = "failed_recoverable"
STAGE_COMPLETED = "completed"
STAGE_CANCELLED = "cancelled"
DEV_TAG = "dev-feature-risk-1234567890-f7265fc-42-1"


def _build(tag="v0.8.0", admin_digest="sha256:v080admin", channel="stable"):
    return SystemBuild(
        requested_tag=tag,
        canonical_tag=tag,
        channel=channel,
        revision=REVISION,
        build_id=tag if channel == "development" else "v0.8.0-f7265fc",
        admin_image=f"{ADMIN_IMAGE_REPO}:{tag}",
        admin_digest=admin_digest,
        ems_image=f"{EMS_IMAGE_REPO}:{tag}",
        ems_digest="sha256:v080ems",
        release_tag=tag,
    )


class FakeResolver:
    def __init__(self, build=None, error=None):
        self._build = build
        self._error = error
        self.resolved = []

    def resolve(self, tag):
        self.resolved.append(tag)
        if self._error is not None:
            raise self._error
        return self._build


class FakeEmbedded:
    def __init__(self, fail=False):
        self._fail = fail
        self.verified = []
        self.imported = []

    def verify(self, *, running_build):
        if self._fail:
            from admin.embedded_resources import EmbeddedResourcesError

            raise EmbeddedResourcesError("bad bundle")
        self.verified.append(running_build)
        return running_build

    def import_into_cache(self, *, running_build):
        if self._fail:
            from admin.embedded_resources import EmbeddedResourcesError

            raise EmbeddedResourcesError("bad bundle")
        self.imported.append(running_build)
        return "v0.8.0"


class FakeReleaseArchive:
    """Stand-in for the exact-historical-release resource preparer."""

    def __init__(self, fail=False):
        self._fail = fail
        self.prepared = []

    def import_into_cache(self, *, running_build):
        if self._fail:
            from admin.releases import ReleaseError

            raise ReleaseError("release resources could not be verified", 422)
        self.prepared.append(running_build)
        return running_build.get("canonical_tag")


def _service(tmp_path, *, build=None, resolver_error=None, running=None,
             persistent_ref=None, embedded=None, launched=None, known_good=None,
             running_ems=None, release_archive=None):
    running = running or ImageIdentity(
        image_ref=f"{ADMIN_IMAGE_REPO}:latest", digest="sha256:latest", revision="old",
        build_id="v0.7.0-old",
    )
    transitions = PendingTransitionStore(tmp_path / "state")
    known_good = known_good or KnownGoodStore(tmp_path / "state")
    launched = [] if launched is None else launched
    target = build or _build()
    running_ems = running_ems or ImageIdentity(
        image_ref=target.ems_image,
        digest=target.ems_digest,
        revision=target.revision,
        channel=target.channel,
        build_id=target.build_id,
        release_tag=target.release_tag,
    )
    return (
        SystemAlignmentService(
            resolver=FakeResolver(build=build, error=resolver_error),
            transition_store=transitions,
            embedded_resources=embedded or FakeEmbedded(),
            release_archive_resources=release_archive or FakeReleaseArchive(),
            known_good_store=known_good,
            current_identity=lambda: running,
            current_ems_identity=lambda: running_ems,
            persistent_ref=lambda: persistent_ref or f"{ADMIN_IMAGE_REPO}:latest",
            launcher=lambda record: launched.append(record),
            now=lambda: T0,
        ),
        transitions,
        known_good,
        launched,
    )


# --- validate: server-authoritative alignment + button state ----------------


def _validate(tmp_path, *, running, persistent_ref, build=None, tag="v0.8.0"):
    build = build or _build()
    service, *_ = _service(
        tmp_path, build=build, running=running, persistent_ref=persistent_ref
    )
    return service.validate(requested_tag=tag)


def _aligned_running():
    return ImageIdentity(
        image_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0", digest="sha256:aligned",
        revision=REVISION, build_id="v0.8.0-f7265fc",
    )


def _development_build(tag=DEV_TAG):
    return _build(tag=tag, admin_digest="sha256:dev-admin", channel="development")


def _running_for(build):
    return ImageIdentity(
        image_ref=build.admin_image,
        digest=build.admin_digest,
        revision=build.revision,
        build_id=build.build_id,
    )


def test_validate_aligned_bundle_is_read_only_and_allows_confirmation(tmp_path):
    build = _build(admin_digest="sha256:aligned")
    embedded = FakeEmbedded()
    service, transitions, *_ = _service(
        tmp_path,
        build=build,
        running=_aligned_running(),
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
        embedded=embedded,
    )
    first = service.validate(requested_tag="v0.8.0")
    result = service.validate(requested_tag="v0.8.0")

    assert first == result
    assert result["valid"] is True
    assert result["validation_state"] == "valid"
    assert result["alignment"] == "aligned"
    assert result["admin_update_required"] is False
    assert result["embedded_resources_valid"] is True
    assert result["resources_verified"] is False
    assert result["next_allowed"] is True
    assert result["confirmation_allowed"] is True
    assert result["action_state"] == {
        "selected_build": {
            "tag": "v0.8.0",
            "channel": "stable",
            "revision": REVISION,
            "build_id": "v0.8.0-f7265fc",
        },
        "selection_fingerprint": (
            f"v0.8.0:stable:{REVISION}:v0.8.0-f7265fc:sha256:aligned:sha256:v080ems"
        ),
        "compatibility_mode": "modern_paired",
        "alignment_state": "aligned",
        "resource_strategy": "embedded",
        "resource_state": "ready",
        "admin_update_required": False,
        "admin_update_allowed": False,
        "continue_allowed": True,
        "terminal_error": None,
        "busy": False,
        "progress_message": None,
        "polling_required": False,
        "transition_stage": None,
        "operation_id": None,
    }
    assert result["transition_in_progress"] is False
    assert result["transition_stage"] is None
    assert result["selected_tag"] == "v0.8.0"
    assert transitions.read() is None
    assert embedded.imported == []
    assert embedded.verified == [build.as_dict(), build.as_dict()]
    assert result["summary"] == {
        "channel": "stable",
        "revision": REVISION,
        "build_id": "v0.8.0-f7265fc",
        "admin_image": f"{ADMIN_IMAGE_REPO}:v0.8.0",
        "ems_image": f"{EMS_IMAGE_REPO}:v0.8.0",
    }


def test_validate_aligned_with_verified_resources_enables_next(tmp_path):
    # Once resources are verified for exactly this build, the combined status is
    # green and Next is enabled.
    build = _build(admin_digest="sha256:aligned")
    service, *_ = _service(
        tmp_path, build=build, running=_aligned_running(),
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
    )
    started = service.start(requested_tag="v0.8.0", mode="fresh_install")
    service.verify_resources(operation_id=started["operation_id"])

    result = service.validate(requested_tag="v0.8.0")
    assert result["alignment"] == "aligned"
    assert result["resources_verified"] is True
    assert result["admin_update_required"] is False
    assert result["next_allowed"] is True
    assert result["confirmation_allowed"] is False
    assert result["transition_in_progress"] is True
    assert result["transition_stage"] == STAGE_RESOURCES_VERIFIED
    assert result["operation_id"] == started["operation_id"]
    assert service.status()["transition"]["admin_alignment_required"] is False


@pytest.mark.parametrize(
    "stage,extra",
    [
        (STAGE_ADMIN_UPDATE_PENDING, {}),
        (STAGE_ADMIN_RECONNECT_PENDING, {}),
        (STAGE_ADMIN_ALIGNED, {}),
        (STAGE_ADMIN_ALIGNED, {"resources_claimed_at": "2026-07-14T12:00:00Z"}),
        (
            STAGE_FAILED_RECOVERABLE,
            {
                "failed_stage": STAGE_ADMIN_ALIGNED,
                "resume_stage": STAGE_ADMIN_ALIGNED,
                "error_code": "resource_failed",
                "error_message": "resource verification failed",
            },
        ),
    ],
)
def test_validate_blocks_next_during_every_unfinished_transition(tmp_path, stage, extra):
    build = _build(admin_digest="sha256:aligned")
    service, transitions, *_ = _service(
        tmp_path,
        build=build,
        running=_aligned_running(),
        persistent_ref=build.admin_image,
    )
    transitions.begin(
        make_transition_record(
            mode="fresh_install",
            system_tag=build.canonical_tag,
            build_id=build.build_id,
            revision=build.revision,
            admin_image=build.admin_image,
            admin_digest=build.admin_digest,
            ems_image=build.ems_image,
            ems_digest=build.ems_digest,
            stage=stage,
            now=T0,
            **extra,
        )
    )

    result = service.validate(requested_tag=build.canonical_tag)

    assert result["next_allowed"] is False
    assert result["confirmation_allowed"] is False
    assert result["transition_in_progress"] is True
    assert result["transition_stage"] == stage
    assert result["recovery_required"] is (stage == STAGE_FAILED_RECOVERABLE)


def test_validate_blocks_transition_for_another_tag(tmp_path):
    selected = _build(admin_digest="sha256:aligned")
    other = _build(tag="v0.9.0", admin_digest="sha256:other")
    service, transitions, *_ = _service(
        tmp_path,
        build=selected,
        running=_aligned_running(),
        persistent_ref=selected.admin_image,
    )
    transitions.begin(
        make_transition_record(
            mode="fresh_install",
            system_tag=other.canonical_tag,
            build_id=other.build_id,
            revision=other.revision,
            admin_image=other.admin_image,
            admin_digest=other.admin_digest,
            ems_image=other.ems_image,
            ems_digest=other.ems_digest,
            stage=STAGE_RESOURCES_VERIFIED,
            now=T0,
        )
    )

    result = service.validate(requested_tag=selected.canonical_tag)

    assert result["next_allowed"] is False
    assert result["transition_in_progress"] is True
    assert result["active_transition_tag"] == "v0.9.0"


@pytest.mark.parametrize(
    ("stage", "previous_tag"),
    [
        (STAGE_COMPLETED, "v0.8.0"),
        (STAGE_COMPLETED, "v0.9.0"),
        (STAGE_CANCELLED, "v0.9.0"),
    ],
)
def test_validate_allows_new_confirmation_after_terminal_transition(
    tmp_path, stage, previous_tag
):
    selected = _build(admin_digest="sha256:aligned")
    previous = _build(tag=previous_tag, admin_digest="sha256:previous")
    service, transitions, *_ = _service(
        tmp_path,
        build=selected,
        running=_aligned_running(),
        persistent_ref=selected.admin_image,
    )
    transitions.begin(
        make_transition_record(
            mode="fresh_install",
            system_tag=previous.canonical_tag,
            build_id=previous.build_id,
            revision=previous.revision,
            admin_image=previous.admin_image,
            admin_digest=previous.admin_digest,
            ems_image=previous.ems_image,
            ems_digest=previous.ems_digest,
            stage=stage,
            now=T0,
        )
    )
    existing_operation_id = transitions.read().operation_id

    result = service.validate(requested_tag=selected.canonical_tag)

    assert result["next_allowed"] is True
    assert result["confirmation_allowed"] is True
    assert result["transition_in_progress"] is False
    assert result["transition_stage"] == stage
    assert result["active_transition_tag"] is None
    assert transitions.read().operation_id == existing_operation_id


@pytest.mark.parametrize(
    ("stage", "previous_tag"),
    [
        (STAGE_COMPLETED, "v0.8.0"),
        (STAGE_COMPLETED, "v0.9.0"),
        (STAGE_CANCELLED, "v0.9.0"),
    ],
)
def test_confirm_replaces_terminal_history_with_new_setup_operation(
    tmp_path, stage, previous_tag
):
    selected = _build(admin_digest="sha256:aligned")
    previous = _build(tag=previous_tag, admin_digest="sha256:previous")
    service, transitions, *_ = _service(
        tmp_path,
        build=selected,
        running=_aligned_running(),
        persistent_ref=selected.admin_image,
    )
    transitions.begin(
        make_transition_record(
            mode="fresh_install",
            system_tag=previous.canonical_tag,
            build_id=previous.build_id,
            revision=previous.revision,
            admin_image=previous.admin_image,
            admin_digest=previous.admin_digest,
            ems_image=previous.ems_image,
            ems_digest=previous.ems_digest,
            stage=stage,
            now=T0,
        )
    )
    previous_operation_id = transitions.read().operation_id

    result = service.confirm_setup_build(
        requested_tag=selected.canonical_tag,
        mode="fresh_install",
    )

    current = transitions.read()
    assert result["resources_verified"] is True
    assert result["operation_id"] != previous_operation_id
    assert current.operation_id == result["operation_id"]
    assert current.system_tag == selected.canonical_tag
    assert current.stage == STAGE_RESOURCES_VERIFIED


@pytest.mark.parametrize(
    "stage,extra",
    [
        (STAGE_ADMIN_UPDATE_PENDING, {}),
        (STAGE_RESOURCES_VERIFIED, {}),
        (
            STAGE_FAILED_RECOVERABLE,
            {
                "failed_stage": STAGE_ADMIN_ALIGNED,
                "resume_stage": STAGE_ADMIN_ALIGNED,
                "error_code": "resource_failed",
                "error_message": "resource verification failed",
            },
        ),
    ],
)
def test_validate_keeps_nonterminal_other_build_blocking(tmp_path, stage, extra):
    selected = _build(admin_digest="sha256:aligned")
    other = _build(tag="v0.9.0", admin_digest="sha256:other")
    service, transitions, *_ = _service(
        tmp_path,
        build=selected,
        running=_aligned_running(),
        persistent_ref=selected.admin_image,
    )
    transitions.begin(
        make_transition_record(
            mode="fresh_install",
            system_tag=other.canonical_tag,
            build_id=other.build_id,
            revision=other.revision,
            admin_image=other.admin_image,
            admin_digest=other.admin_digest,
            ems_image=other.ems_image,
            ems_digest=other.ems_digest,
            stage=stage,
            now=T0,
            **extra,
        )
    )

    result = service.validate(requested_tag=selected.canonical_tag)

    assert result["next_allowed"] is False
    assert result["confirmation_allowed"] is False
    assert result["transition_in_progress"] is True
    assert result["active_transition_tag"] == other.canonical_tag


def test_development_update_persists_acknowledgement_for_exact_tag(tmp_path):
    build = _development_build()
    service, transitions, *_ = _service(tmp_path, build=build)

    result = service.start(
        requested_tag=build.canonical_tag,
        mode="fresh_install",
        development_risk_acknowledged=True,
    )

    record = transitions.read()
    assert result["stage"] == STAGE_ADMIN_RECONNECT_PENDING
    assert record.development_risk_acknowledged is True
    assert record.development_risk_acknowledged_for_tag == build.canonical_tag


def test_development_transition_requires_new_acknowledgement(tmp_path):
    build = _development_build()
    service, transitions, *_ = _service(tmp_path, build=build)

    with pytest.raises(SystemAlignmentError) as excinfo:
        service.start(requested_tag=build.canonical_tag, mode="fresh_install")

    assert excinfo.value.code == "acknowledgement_required"
    assert transitions.read() is None


def test_development_reconnect_uses_durable_acknowledgement(tmp_path):
    build = _development_build()
    service, transitions, *_ = _service(
        tmp_path, build=build, persistent_ref=build.admin_image
    )
    started = service.start(
        requested_tag=build.canonical_tag,
        mode="fresh_install",
        development_risk_acknowledged=True,
    )
    service._current_identity = lambda: _running_for(build)

    resumed = service.resume(operation_id=started["operation_id"])
    verified = service.verify_resources(operation_id=started["operation_id"])

    assert resumed["stage"] == STAGE_ADMIN_ALIGNED
    assert verified["stage"] == STAGE_RESOURCES_VERIFIED
    assert transitions.read().development_risk_acknowledged_for_tag == build.canonical_tag
    assert service.development_acknowledgement_allows_automatic_resume(
        requested_tag=build.canonical_tag
    ) is True
    validation = service.validate(requested_tag=build.canonical_tag)
    assert validation["development_risk_acknowledged"] is True
    assert validation["development_risk_acknowledged_for_tag"] == build.canonical_tag
    assert validation["next_allowed"] is True


def test_development_reconnect_rejects_acknowledgement_for_changed_resolved_build(tmp_path):
    build = _development_build()
    service, *_ = _service(tmp_path, build=build)
    started = service.start(
        requested_tag=build.canonical_tag,
        mode="fresh_install",
        development_risk_acknowledged=True,
    )
    changed = _development_build("dev-feature-other-abcdef1234-f7265fc-43-1")
    service._resolver._build = changed
    service._current_identity = lambda: _running_for(build)

    with pytest.raises(SystemAlignmentError) as excinfo:
        service.resume(operation_id=started["operation_id"])

    assert excinfo.value.code == "transition_context_mismatch"


def test_development_manual_retry_requires_fresh_acknowledgement(tmp_path):
    build = _development_build()
    service, transitions, *_ = _service(tmp_path, build=build)
    started = service.start(
        requested_tag=build.canonical_tag,
        mode="fresh_install",
        development_risk_acknowledged=True,
    )
    transitions.mark_failed(
        started["operation_id"],
        error_code="admin_update_failed",
        error_message="update failed",
        resume_stage=STAGE_ADMIN_UPDATE_PENDING,
        now=T0,
    )

    with pytest.raises(SystemAlignmentError) as excinfo:
        service.retry(operation_id=started["operation_id"])
    assert excinfo.value.code == "acknowledgement_required"

    retried = service.retry(
        operation_id=started["operation_id"],
        development_risk_acknowledged=True,
    )
    assert retried["stage"] == STAGE_ADMIN_RECONNECT_PENDING


@pytest.mark.parametrize("tag,channel", [("v0.8.0", "stable"), ("v0.9.0-rc.1", "rc")])
def test_non_development_transitions_need_no_risk_acknowledgement(tmp_path, tag, channel):
    build = _build(tag=tag, channel=channel)
    service, transitions, *_ = _service(tmp_path, build=build)

    service.start(requested_tag=tag, mode="fresh_install")

    assert transitions.read().development_risk_acknowledged is False


def test_validate_resources_verified_only_for_the_matching_build(tmp_path):
    # A verified transition for one build must not report the *selected* build's
    # resources as verified when the build identities differ.
    build = _build(admin_digest="sha256:aligned")
    other = _build(tag="v0.9.0", admin_digest="sha256:aligned")
    service, transitions, *_ = _service(
        tmp_path, build=build, running=_aligned_running(),
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
    )
    started = service.start(requested_tag="v0.8.0", mode="fresh_install")
    service.verify_resources(operation_id=started["operation_id"])
    # Repoint the resolver at a different build than the verified transition.
    service._resolver._build = other

    result = service.validate(requested_tag="v0.9.0")
    assert result["resources_verified"] is False
    assert result["next_allowed"] is False
    assert result["active_transition_tag"] == "v0.8.0"


def test_validate_retag_required_enables_update_and_blocks_next(tmp_path):
    # Example 3: same digest, but the persistent compose ref still says latest.
    build = _build(admin_digest="sha256:aligned")
    running = ImageIdentity(
        image_ref=f"{ADMIN_IMAGE_REPO}:latest", digest="sha256:aligned",
        revision=REVISION, build_id="v0.8.0-f7265fc",
    )
    result = _validate(
        tmp_path, build=build, running=running,
        persistent_ref=f"{ADMIN_IMAGE_REPO}:latest",
    )
    assert result["alignment"] == "retag_required"
    assert result["admin_update_required"] is True
    assert result["next_allowed"] is False


def test_validate_admin_update_required_when_running_content_differs(tmp_path):
    build = _build(admin_digest="sha256:new")
    running = ImageIdentity(
        image_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0", digest="sha256:old",
        revision="old", build_id="v0.7.0-old",
    )
    result = _validate(
        tmp_path, build=build, running=running,
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
    )
    assert result["alignment"] == "admin_update_required"
    assert result["admin_update_required"] is True
    assert result["next_allowed"] is False


def test_validate_latest_same_digest_is_aligned(tmp_path):
    # Example 1: selected latest, persistent latest, digest is the current latest.
    build = _build(tag="latest", admin_digest="sha256:currentlatest")
    running = ImageIdentity(
        image_ref=f"{ADMIN_IMAGE_REPO}:latest", digest="sha256:currentlatest",
        revision=REVISION, build_id="v0.8.0-f7265fc",
    )
    result = _validate(
        tmp_path, build=build, running=running, tag="latest",
        persistent_ref=f"{ADMIN_IMAGE_REPO}:latest",
    )
    assert result["alignment"] == "aligned"
    assert result["embedded_resources_valid"] is True
    assert result["resources_verified"] is False
    assert result["next_allowed"] is True
    assert result["admin_update_required"] is False


def test_switching_builds_before_confirmation_never_persists_selection(tmp_path):
    first = _build(tag="v0.8.0", admin_digest="sha256:aligned")
    second = _build(tag="v0.9.0", admin_digest="sha256:aligned")
    embedded = FakeEmbedded()
    service, transitions, *_ = _service(
        tmp_path,
        build=first,
        running=_aligned_running(),
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
        embedded=embedded,
    )

    assert service.validate(requested_tag="v0.8.0")["next_allowed"] is True
    service._resolver._build = second
    service.validate(requested_tag="v0.9.0")

    assert transitions.read() is None
    assert embedded.imported == []


def test_confirm_setup_build_persists_and_imports_exactly_once(tmp_path):
    build = _build(admin_digest="sha256:aligned")
    embedded = FakeEmbedded()
    service, transitions, *_ = _service(
        tmp_path,
        build=build,
        running=_aligned_running(),
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
        embedded=embedded,
    )

    first = service.confirm_setup_build(
        requested_tag="v0.8.0", mode="fresh_install"
    )
    second = service.confirm_setup_build(
        requested_tag="v0.8.0", mode="fresh_install"
    )

    assert first["operation_id"] == second["operation_id"]
    assert first["resources_verified"] is True
    assert second["resources_verified"] is True
    assert transitions.read().stage == STAGE_RESOURCES_VERIFIED
    assert embedded.imported == [build.as_dict()]


def test_modern_admin_update_flow_unchanged_by_legacy_support(tmp_path):
    # Regression: a modern paired build that needs an Admin update still launches
    # the replacement, records no orchestrator override (the selected Admin
    # becomes the running Admin), and keeps the embedded strategy end to end.
    build = _build(admin_digest="sha256:v080admin")  # running != target -> update
    service, transitions, _, launched = _service(tmp_path, build=build)

    validated = service.validate(requested_tag="v0.8.0")
    assert validated["compatibility_mode"] == "modern_paired"
    assert validated["resource_strategy"] == "embedded"
    assert validated["admin_update_required"] is True
    assert validated["next_allowed"] is False

    started = service.start(requested_tag="v0.8.0", mode="fresh_install")
    assert started["reconnect"] is True
    assert len(launched) == 1  # the Admin replacement is launched

    record = transitions.read()
    assert record.admin_alignment_required is True
    assert record.compatibility_mode == "modern_paired"
    assert record.resource_strategy == "embedded"
    assert record._orchestrator_override() is None

    # After the reconnect the running Admin is the target build; resume aligns.
    aligned = ImageIdentity(
        image_ref=build.admin_image, digest=build.admin_digest,
        revision=build.revision, build_id=build.build_id,
    )
    resumed = service.resume(
        operation_id=started["operation_id"], running_admin=aligned
    )
    assert resumed["stage"] == STAGE_ADMIN_ALIGNED


def test_validate_then_confirm_legacy_release_agree(tmp_path):
    # Phase 3: validation and confirmation must use the same effective decision.
    # A legacy release that validate() reports Continue-able must confirm without
    # an Admin alignment step and without replacing the running modern Admin.
    build = _legacy_build()
    service, transitions, _, launched = _service(tmp_path, build=build)

    validated = service.validate(requested_tag="v0.7.0")
    assert validated["next_allowed"] is True
    assert validated["confirmation_allowed"] is True

    confirmed = service.confirm_setup_build(
        requested_tag="v0.7.0", mode="fresh_install"
    )

    assert confirmed["resources_verified"] is True
    assert confirmed["next_allowed"] is True
    assert launched == []
    assert transitions.read().stage == STAGE_RESOURCES_VERIFIED
    assert transitions.read().admin_alignment_required is False


def test_legacy_verify_resources_uses_release_archive_not_embedded(tmp_path):
    # Phase 4: a legacy release prepares its resources from the exact historical
    # archive; the running Admin's embedded bundle is never substituted.
    build = _legacy_build()
    embedded = FakeEmbedded()
    archive = FakeReleaseArchive()
    service, transitions, *_ = _service(
        tmp_path, build=build, running=_modern_running_admin(),
        embedded=embedded, release_archive=archive,
    )

    service.confirm_setup_build(requested_tag="v0.7.0", mode="fresh_install")

    assert [b.get("canonical_tag") for b in archive.prepared] == ["v0.7.0"]
    assert [b.get("revision") for b in archive.prepared] == [build.revision]
    assert embedded.imported == []
    assert transitions.read().stage == STAGE_RESOURCES_VERIFIED


def test_modern_verify_resources_uses_embedded_not_release_archive(tmp_path):
    build = _build(admin_digest="sha256:aligned")
    embedded = FakeEmbedded()
    archive = FakeReleaseArchive()
    service, *_ = _service(
        tmp_path, build=build, running=_aligned_running(),
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
        embedded=embedded, release_archive=archive,
    )

    service.confirm_setup_build(requested_tag="v0.8.0", mode="fresh_install")

    assert embedded.imported == [build.as_dict()]
    assert archive.prepared == []


def test_legacy_release_archive_failure_fails_closed_before_ems(tmp_path):
    # A release that cannot be verified stops at failed_recoverable; it never
    # advances to resource-verified, so no config/EMS mutation can follow.
    build = _legacy_build()
    archive = FakeReleaseArchive(fail=True)
    service, transitions, *_ = _service(
        tmp_path, build=build, running=_modern_running_admin(),
        release_archive=archive,
    )

    with pytest.raises(SystemAlignmentError) as excinfo:
        service.confirm_setup_build(requested_tag="v0.7.0", mode="fresh_install")

    assert excinfo.value.code == "system_build_resources_invalid"
    record = transitions.read()
    assert record.stage == STAGE_FAILED_RECOVERABLE
    assert record.error_code == "system_build_resources_invalid"


def test_legacy_release_archive_unavailable_fails_closed(tmp_path):
    # No configured release-archive provider must fail closed rather than fall
    # back to the embedded bundle (which would substitute main-branch resources).
    build = _legacy_build()
    service = SystemAlignmentService(
        resolver=FakeResolver(build=build),
        transition_store=PendingTransitionStore(tmp_path / "state"),
        embedded_resources=FakeEmbedded(),
        release_archive_resources=None,
        known_good_store=KnownGoodStore(tmp_path / "state"),
        current_identity=_modern_running_admin,
        current_ems_identity=lambda: _running_ems_identity(
            tag="v0.7.0", digest="sha256:oldems"
        ),
        persistent_ref=lambda: f"{ADMIN_IMAGE_REPO}:latest",
        launcher=lambda record: None,
        now=lambda: T0,
    )

    with pytest.raises(SystemAlignmentError) as excinfo:
        service.confirm_setup_build(requested_tag="v0.7.0", mode="fresh_install")
    assert excinfo.value.code == "system_build_resources_invalid"


def test_confirm_setup_build_refuses_admin_mismatch_without_transition(tmp_path):
    build = _build(admin_digest="sha256:new")
    service, transitions, *_ = _service(
        tmp_path,
        build=build,
        running=_aligned_running(),
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
    )

    with pytest.raises(SystemAlignmentError) as excinfo:
        service.confirm_setup_build(
            requested_tag="v0.8.0", mode="fresh_install"
        )

    assert excinfo.value.code == "system_build_alignment_required"
    assert transitions.read() is None


def _service_with_mutable_admin(tmp_path, *, build, running):
    """A service whose running Admin identity can be swapped after confirmation."""

    holder = {"admin": running}
    transitions = PendingTransitionStore(tmp_path / "state")
    known_good = KnownGoodStore(tmp_path / "state")
    running_ems = ImageIdentity(
        image_ref=build.ems_image, digest=build.ems_digest, revision=build.revision,
        channel=build.channel, build_id=build.build_id, release_tag=build.release_tag,
    )
    service = SystemAlignmentService(
        resolver=FakeResolver(build=build),
        transition_store=transitions,
        embedded_resources=FakeEmbedded(),
        release_archive_resources=FakeReleaseArchive(),
        known_good_store=known_good,
        current_identity=lambda: holder["admin"],
        current_ems_identity=lambda: running_ems,
        persistent_ref=lambda: f"{ADMIN_IMAGE_REPO}:latest",
        launcher=lambda record: None,
        now=lambda: T0,
    )
    return service, transitions, holder


def test_legacy_discovery_authorized_for_current_orchestrator_admin(tmp_path):
    # Phase 7: discovery is authorised for a confirmed legacy transition against
    # the running modern orchestrator Admin — never against the historical build.
    build = _legacy_build()
    service, transitions, holder = _service_with_mutable_admin(
        tmp_path, build=build, running=_modern_running_admin()
    )
    confirmed = service.confirm_setup_build(
        requested_tag="v0.7.0", mode="fresh_install"
    )

    result = service.validate_setup_discovery_operation(
        operation_id=confirmed["operation_id"]
    )

    assert result["operation_id"] == confirmed["operation_id"]
    assert result["system_tag"] == "v0.7.0"


def test_legacy_discovery_rejects_a_different_running_admin(tmp_path):
    build = _legacy_build()
    service, transitions, holder = _service_with_mutable_admin(
        tmp_path, build=build, running=_modern_running_admin()
    )
    confirmed = service.confirm_setup_build(
        requested_tag="v0.7.0", mode="fresh_install"
    )
    # The Admin now reports a different modern identity than the authorised one.
    holder["admin"] = ImageIdentity(
        image_ref=f"{ADMIN_IMAGE_REPO}:v0.9.0", digest="sha256:other-admin",
        version_label="v0.9.0", revision="b" * 40, build_id="v0.9.0-bbbbbbb",
        release_tag="v0.9.0",
    )

    with pytest.raises(SystemAlignmentError) as excinfo:
        service.validate_setup_discovery_operation(
            operation_id=confirmed["operation_id"]
        )
    assert excinfo.value.code == "system_build_mismatch"


def test_legacy_discovery_rejects_a_different_selected_ems_build(tmp_path):
    build = _legacy_build()
    service, transitions, holder = _service_with_mutable_admin(
        tmp_path, build=build, running=_modern_running_admin()
    )
    confirmed = service.confirm_setup_build(
        requested_tag="v0.7.0", mode="fresh_install"
    )
    # The selected release now resolves to a different EMS image/digest.
    service._resolver._build = dataclasses.replace(
        _legacy_build(), ems_digest="sha256:tampered"
    )

    with pytest.raises(SystemAlignmentError) as excinfo:
        service.validate_setup_discovery_operation(
            operation_id=confirmed["operation_id"]
        )
    assert excinfo.value.code == "system_build_mismatch"


def test_legacy_discovery_rejects_a_stale_transition_stage(tmp_path):
    build = _legacy_build()
    service, transitions, holder = _service_with_mutable_admin(
        tmp_path, build=build, running=_modern_running_admin()
    )
    confirmed = service.confirm_setup_build(
        requested_tag="v0.7.0", mode="fresh_install"
    )
    # Advancing past resources_verified makes the operation ineligible for a
    # fresh discovery authorization.
    service.begin_ems_operation(operation_id=confirmed["operation_id"])

    with pytest.raises(SystemAlignmentError) as excinfo:
        service.validate_setup_discovery_operation(
            operation_id=confirmed["operation_id"]
        )
    assert excinfo.value.code == "system_alignment_incomplete"


def test_setup_discovery_operation_requires_matching_verified_transition(tmp_path):
    build = _build(admin_digest="sha256:aligned")
    service, *_ = _service(
        tmp_path,
        build=build,
        running=_aligned_running(),
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
    )
    confirmed = service.confirm_setup_build(
        requested_tag="v0.8.0", mode="fresh_install"
    )

    result = service.validate_setup_discovery_operation(
        operation_id=confirmed["operation_id"]
    )

    assert result["operation_id"] == confirmed["operation_id"]
    assert result["stage"] == STAGE_RESOURCES_VERIFIED
    assert result["system_tag"] == "v0.8.0"


@pytest.mark.parametrize("operation_id", [None, "wrong-operation"])
def test_setup_discovery_operation_rejects_missing_or_wrong_id(tmp_path, operation_id):
    build = _build(admin_digest="sha256:aligned")
    service, *_ = _service(
        tmp_path,
        build=build,
        running=_aligned_running(),
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
    )
    service.confirm_setup_build(requested_tag="v0.8.0", mode="fresh_install")

    with pytest.raises(SystemAlignmentError) as excinfo:
        service.validate_setup_discovery_operation(operation_id=operation_id)

    assert excinfo.value.code in {"setup_operation_required", "operation_mismatch"}


def test_setup_discovery_operation_rejects_failed_recoverable(tmp_path):
    build = _build(admin_digest="sha256:aligned")
    service, transitions, *_ = _service(
        tmp_path,
        build=build,
        running=_aligned_running(),
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
    )
    confirmed = service.confirm_setup_build(
        requested_tag="v0.8.0", mode="fresh_install"
    )
    transitions.mark_failed(
        confirmed["operation_id"],
        error_code="resource_cache_failed",
        error_message="cache failed",
        resume_stage=STAGE_RESOURCES_VERIFIED,
        now=T0,
    )

    with pytest.raises(SystemAlignmentError) as excinfo:
        service.validate_setup_discovery_operation(
            operation_id=confirmed["operation_id"]
        )

    assert excinfo.value.code == "system_alignment_incomplete"


def test_setup_discovery_operation_rechecks_build_identity(tmp_path):
    build = _build(admin_digest="sha256:aligned")
    service, *_ = _service(
        tmp_path,
        build=build,
        running=_aligned_running(),
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
    )
    confirmed = service.confirm_setup_build(
        requested_tag="v0.8.0", mode="fresh_install"
    )
    service._resolver._build = _build(tag="v0.8.0", admin_digest="sha256:changed")

    with pytest.raises(SystemAlignmentError) as excinfo:
        service.validate_setup_discovery_operation(
            operation_id=confirmed["operation_id"]
        )

    assert excinfo.value.code == "system_build_mismatch"


def test_validate_latest_newer_digest_requires_update(tmp_path):
    # Example 2: selected latest, persistent latest, running digest is older.
    build = _build(tag="latest", admin_digest="sha256:newlatest")
    running = ImageIdentity(
        image_ref=f"{ADMIN_IMAGE_REPO}:latest", digest="sha256:oldlatest",
        revision="old", build_id="v0.7.0-old",
    )
    result = _validate(
        tmp_path, build=build, running=running, tag="latest",
        persistent_ref=f"{ADMIN_IMAGE_REPO}:latest",
    )
    assert result["alignment"] == "admin_update_required"
    assert result["admin_update_required"] is True
    assert result["next_allowed"] is False


# --- Admin and embedded-resource alignment are separate committed stages ---


def test_already_aligned_admin_still_persists_and_verifies_resources_separately(tmp_path):
    build = _build(admin_digest="sha256:aligned")
    running = ImageIdentity(
        image_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0", digest="sha256:aligned",
        revision=REVISION, build_id="v0.8.0-f7265fc",
    )
    embedded = FakeEmbedded()
    service, transitions, _, launched = _service(
        tmp_path, build=build, running=running,
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0", embedded=embedded,
    )
    result = service.start(requested_tag="v0.8.0", mode="fresh_install")
    operation_id = result["operation_id"]
    assert result["status"] == STAGE_ADMIN_ALIGNED
    assert result["config_written"] is False
    assert embedded.imported == []
    assert launched == []  # no updater needed
    assert transitions.read().stage == STAGE_ADMIN_ALIGNED
    assert service.is_transition_pending() is True

    verified = service.verify_resources(operation_id=operation_id)
    assert verified["status"] == STAGE_RESOURCES_VERIFIED
    assert transitions.read().stage == STAGE_RESOURCES_VERIFIED
    assert embedded.imported == [build.as_dict()]

    # Verification is idempotent; polling/retry must not rewrite the cache.
    assert service.verify_resources(operation_id=operation_id) == verified
    assert len(embedded.imported) == 1


# --- bootstrap latest -> v0.8.0: alignment starts, EMS not touched --------


def test_bootstrap_latest_starts_alignment_without_touching_ems(tmp_path):
    build = _build(admin_digest="sha256:v080admin")
    service, transitions, _, launched = _service(tmp_path, build=build)
    result = service.start(requested_tag="v0.8.0", mode="fresh_install")
    assert result["status"] == "admin_alignment_started"
    assert result["reconnect"] is True
    assert result["config_written"] is False
    assert result["ems_started"] is False
    # The launcher receives the pending target; after it is launched, the
    # persisted operation waits for the replacement Admin to reconnect.
    assert launched and launched[0].stage == STAGE_ADMIN_UPDATE_PENDING
    assert transitions.read().system_tag == "v0.8.0"
    assert transitions.read().stage == STAGE_ADMIN_RECONNECT_PENDING


def test_resolution_failure_blocks_before_any_transition(tmp_path):
    service, transitions, _, launched = _service(
        tmp_path, resolver_error=SystemBuildError("system_build_mismatch", "bad pair")
    )
    with pytest.raises(SystemBuildError):
        service.start(requested_tag="v0.8.0", mode="fresh_install")
    assert launched == []
    assert transitions.read() is None  # nothing persisted, no EMS work


# --- prepare_setup_resources: aligned-only, never updates Admin -------------


def test_prepare_setup_resources_verifies_aligned_admin_without_launcher(tmp_path):
    build = _build(admin_digest="sha256:aligned")
    embedded = FakeEmbedded()
    service, transitions, _, launched = _service(
        tmp_path, build=build, running=_aligned_running(),
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0", embedded=embedded,
    )
    result = service.prepare_setup_resources(
        requested_tag="v0.8.0", mode="fresh_install"
    )
    assert result["resources_verified"] is True
    assert result["next_allowed"] is True
    assert transitions.read().stage == STAGE_RESOURCES_VERIFIED
    assert embedded.imported == [build.as_dict()]
    # Preparing resources must never launch the hardened Admin updater.
    assert launched == []


def test_prepare_setup_resources_imports_when_resources_missing(tmp_path):
    # Aligned + resources not yet in the cache: preparation imports them itself.
    build = _build(admin_digest="sha256:aligned")
    embedded = FakeEmbedded()
    service, _, _, launched = _service(
        tmp_path, build=build, running=_aligned_running(),
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0", embedded=embedded,
    )
    assert embedded.imported == []
    service.prepare_setup_resources(requested_tag="v0.8.0", mode="fresh_install")
    assert embedded.imported == [build.as_dict()]
    assert launched == []


def test_prepare_setup_resources_refuses_unaligned_admin(tmp_path):
    # Running content differs from the target: the Admin must be updated first,
    # and preparation must refuse (never create a transition or launch a change).
    build = _build(admin_digest="sha256:new")
    running = ImageIdentity(
        image_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0", digest="sha256:old",
        revision="old", build_id="v0.7.0-old",
    )
    service, transitions, _, launched = _service(
        tmp_path, build=build, running=running,
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
    )
    with pytest.raises(SystemAlignmentError) as excinfo:
        service.prepare_setup_resources(requested_tag="v0.8.0", mode="fresh_install")
    assert excinfo.value.code == "system_build_alignment_required"
    assert launched == []
    assert transitions.read() is None


def test_prepare_setup_resources_is_idempotent(tmp_path):
    build = _build(admin_digest="sha256:aligned")
    embedded = FakeEmbedded()
    service, _, _, launched = _service(
        tmp_path, build=build, running=_aligned_running(),
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0", embedded=embedded,
    )
    first = service.prepare_setup_resources(requested_tag="v0.8.0", mode="fresh_install")
    second = service.prepare_setup_resources(requested_tag="v0.8.0", mode="fresh_install")
    assert first["resources_verified"] is True
    assert second["resources_verified"] is True
    assert len(embedded.imported) == 1
    assert launched == []


# --- reconnect verifies Admin only; resources are a separate hard gate -----


def test_resume_verifies_admin_without_completing_or_importing_resources(tmp_path):
    build = _build()
    embedded = FakeEmbedded()
    service, transitions, _, _ = _service(tmp_path, build=build, embedded=embedded)
    started = service.start(requested_tag="v0.8.0", mode="guided_upgrade")
    operation_id = started["operation_id"]
    # After the Admin restart the running Admin now IS the target build.
    running = {"revision": REVISION, "build_id": "v0.8.0-f7265fc", "digest": "sha256:v080admin"}
    resumed = service.resume(operation_id=operation_id, running_admin=running)
    assert resumed["status"] == STAGE_ADMIN_ALIGNED
    assert resumed["mode"] == "guided_upgrade"
    assert transitions.read().stage == STAGE_ADMIN_ALIGNED
    assert service.is_transition_pending() is True
    assert embedded.imported == []

    # Reconnect polling is idempotent and cannot consume or finish the operation.
    assert service.resume(operation_id=operation_id, running_admin=running) == resumed
    assert transitions.read().stage == STAGE_ADMIN_ALIGNED

    verified = service.verify_resources(operation_id=operation_id)
    assert verified["status"] == STAGE_RESOURCES_VERIFIED
    assert transitions.read().stage == STAGE_RESOURCES_VERIFIED
    assert len(embedded.imported) == 1


def test_failed_alignment_blocks_ems_on_wrong_running_admin(tmp_path):
    build = _build()
    service, transitions, _, _ = _service(tmp_path, build=build)
    started = service.start(requested_tag="v0.8.0", mode="guided_upgrade")
    # The Admin update failed: the running Admin is still the OLD build.
    still_old = {"revision": "old", "build_id": "v0.7.0-old", "digest": "sha256:latest"}
    with pytest.raises(SystemAlignmentError):
        service.resume(operation_id=started["operation_id"], running_admin=still_old)
    assert transitions.read().stage == STAGE_ADMIN_RECONNECT_PENDING


def test_resource_failure_is_recoverable_and_can_retry_from_admin_aligned(tmp_path):
    build = _build()
    embedded = FakeEmbedded(fail=True)
    service, transitions, _, _ = _service(tmp_path, build=build, embedded=embedded)
    started = service.start(requested_tag="v0.8.0", mode="guided_upgrade")
    operation_id = started["operation_id"]
    running = {"revision": REVISION, "build_id": "v0.8.0-f7265fc", "digest": "sha256:v080admin"}
    service.resume(operation_id=operation_id, running_admin=running)
    with pytest.raises(SystemAlignmentError):
        service.verify_resources(operation_id=operation_id)

    failed = transitions.read()
    assert failed.stage == STAGE_FAILED_RECOVERABLE
    assert failed.failed_stage == STAGE_ADMIN_ALIGNED
    assert failed.resume_stage == STAGE_ADMIN_ALIGNED
    assert failed.error_code == "system_build_resources_invalid"
    assert service.is_transition_pending() is True

    retried = service.retry(operation_id=operation_id)
    assert retried["status"] == STAGE_ADMIN_ALIGNED
    embedded._fail = False
    assert service.verify_resources(operation_id=operation_id)["status"] == STAGE_RESOURCES_VERIFIED


def test_expired_alignment_cannot_import_embedded_resources(tmp_path):
    build = _build(admin_digest="sha256:aligned")
    running = ImageIdentity(
        image_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
        digest="sha256:aligned",
        revision=REVISION,
        build_id="v0.8.0-f7265fc",
    )
    embedded = FakeEmbedded()
    service, transitions, _, _ = _service(
        tmp_path,
        build=build,
        running=running,
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
        embedded=embedded,
    )
    started = service.start(requested_tag="v0.8.0", mode="fresh_install")
    service._now = lambda: datetime(
        2026, 7, 14, 14, 0, 0, tzinfo=timezone.utc
    )

    with pytest.raises(SystemAlignmentError) as exc:
        service.verify_resources(operation_id=started["operation_id"])

    assert exc.value.code == "expired"
    assert embedded.imported == []
    assert transitions.read().stage == STAGE_ADMIN_ALIGNED


def test_selected_build_change_during_transition_is_rejected(tmp_path):
    build = _build()
    service, transitions, _, _ = _service(tmp_path, build=build)
    service.start(requested_tag="v0.8.0", mode="guided_upgrade")
    # A second, different plan must not silently overwrite the active transition.
    service._resolver = FakeResolver(build=_build(tag="v0.9.0"))
    with pytest.raises(SystemAlignmentError):
        service.start(requested_tag="v0.9.0", mode="guided_upgrade")


def test_same_build_and_mode_reuse_active_transition_after_admin_reconnect(tmp_path):
    build = _build()
    launched = []
    service, transitions, _, _ = _service(
        tmp_path, build=build, launched=launched
    )
    started = service.start(requested_tag="v0.8.0", mode="guided_upgrade")
    running = {
        "revision": REVISION,
        "build_id": "v0.8.0-f7265fc",
        "digest": "sha256:v080admin",
    }
    service.resume(operation_id=started["operation_id"], running_admin=running)

    resumed_request = service.start(
        requested_tag="v0.8.0", mode="guided_upgrade"
    )

    assert resumed_request["operation_id"] == started["operation_id"]
    assert resumed_request["stage"] == STAGE_ADMIN_ALIGNED
    assert transitions.read().stage == STAGE_ADMIN_ALIGNED
    assert len(launched) == 1


def test_transition_pending_flag_blocks_ems_mutations(tmp_path):
    build = _build()
    service, transitions, _, _ = _service(tmp_path, build=build)
    assert service.is_transition_pending() is False
    service.start(requested_tag="v0.8.0", mode="guided_upgrade")
    assert service.is_transition_pending() is True


def test_matching_transition_rejects_changed_guided_request_fingerprint(tmp_path):
    build = _build(admin_digest="sha256:aligned")
    running = ImageIdentity(
        image_ref=build.admin_image,
        digest=build.admin_digest,
        revision=build.revision,
        channel=build.channel,
        build_id=build.build_id,
        release_tag=build.release_tag,
    )
    service, transitions, _, _ = _service(
        tmp_path,
        build=build,
        running=running,
        persistent_ref=build.admin_image,
    )
    resolved = service.resolve("v0.8.0")
    first = service.start_resolved(
        system_build=resolved,
        mode="guided_upgrade",
        request_fingerprint="sha256:" + "a" * 64,
    )
    assert first["stage"] == STAGE_ADMIN_ALIGNED
    assert transitions.read().request_fingerprint == "sha256:" + "a" * 64

    with pytest.raises(SystemAlignmentError) as exc:
        service.start_resolved(
            system_build=resolved,
            mode="guided_upgrade",
            request_fingerprint="sha256:" + "b" * 64,
        )

    assert exc.value.code == "transition_context_mismatch"


def test_status_polling_is_read_only_and_keeps_target_and_known_good_visible(tmp_path):
    service, transitions, _, _ = _service(tmp_path, build=_build())
    started = service.start(requested_tag="v0.8.0", mode="guided_upgrade")
    before = transitions.path.read_bytes()

    first = service.status()
    second = service.status()
    assert first == second
    assert first["active"] is True
    assert first["transition"]["operation_id"] == started["operation_id"]
    assert first["transition"]["stage"] == STAGE_ADMIN_RECONNECT_PENDING
    assert first["transition"]["system_tag"] == "v0.8.0"
    assert first["known_good"] is None
    assert transitions.path.read_bytes() == before


def _resources_verified_service(tmp_path):
    build = _build(admin_digest="sha256:aligned")
    running = ImageIdentity(
        image_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
        digest="sha256:aligned",
        revision=REVISION,
        build_id="v0.8.0-f7265fc",
    )
    service, transitions, known_good, _ = _service(
        tmp_path,
        build=build,
        running=running,
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
    )
    started = service.start(requested_tag="v0.8.0", mode="guided_upgrade")
    operation_id = started["operation_id"]
    service.verify_resources(operation_id=operation_id)
    return service, transitions, known_good, build, operation_id


def _healthcheck_pending_service(tmp_path):
    service, transitions, known_good, build, operation_id = _resources_verified_service(tmp_path)
    pending = service.begin_ems_operation(operation_id=operation_id)
    assert pending["status"] == STAGE_EMS_OPERATION_PENDING
    assert service.claim_ems_operation(operation_id=operation_id) is True
    service.finish_ems_operation(operation_id=operation_id, succeeded=True)
    assert transitions.read().stage == STAGE_HEALTHCHECK_PENDING
    return service, transitions, known_good, build, operation_id


# --- EMS execution has one atomic claim and recoverable failure ------------


def test_duplicate_ems_claim_cannot_execute_deployment_twice(tmp_path):
    service, transitions, _, _, operation_id = _resources_verified_service(tmp_path)
    service.begin_ems_operation(operation_id=operation_id)
    deployments = []

    if service.claim_ems_operation(operation_id=operation_id):
        deployments.append(operation_id)
    if service.claim_ems_operation(operation_id=operation_id):
        deployments.append(operation_id)

    assert deployments == [operation_id]
    assert transitions.read().stage == STAGE_EMS_OPERATION_RUNNING


def test_ems_deployment_failure_remains_recoverable(tmp_path):
    service, transitions, known_good, _, operation_id = _resources_verified_service(tmp_path)
    service.begin_ems_operation(operation_id=operation_id)
    assert service.claim_ems_operation(operation_id=operation_id) is True

    failed = service.finish_ems_operation(
        operation_id=operation_id,
        succeeded=False,
        error_code="ems_deployment_failed",
        error_message="compose up failed",
    )
    assert failed["status"] == STAGE_FAILED_RECOVERABLE
    record = transitions.read()
    assert record.failed_stage == STAGE_EMS_OPERATION_RUNNING
    assert record.resume_stage == STAGE_EMS_OPERATION_PENDING
    assert record.error_code == "ems_deployment_failed"
    assert known_good.current() is None
    assert service.status()["active"] is True


# --- healthcheck is the only gateway to known-good and completion ---------


def test_healthcheck_failure_does_not_write_known_good(tmp_path):
    service, transitions, known_good, build, operation_id = _healthcheck_pending_service(tmp_path)
    failed = service.finish_healthcheck(
        operation_id=operation_id,
        system_build=build,
        passed=False,
        error_code="healthcheck_failed",
        error_message="EMS did not become healthy",
    )
    assert failed["status"] == STAGE_FAILED_RECOVERABLE
    record = transitions.read()
    assert record.failed_stage == STAGE_HEALTHCHECK_PENDING
    assert record.resume_stage == STAGE_HEALTHCHECK_PENDING
    assert known_good.current() is None


def test_status_marks_a_recoverable_transition_as_cancellable(tmp_path):
    # A guided_upgrade that fails its healthcheck (EMS already recreated, so
    # return_available is false) would wedge the console if resume were the only
    # action. status() must report the transition as cancellable so the recovery
    # UI can offer an abandon escape, and cancel() must then succeed.
    service, transitions, _, build, operation_id = _healthcheck_pending_service(tmp_path)

    # While the healthcheck is still pending the transition is running, not an
    # escape-hatch candidate — even with the worker proven inactive.
    assert (
        service.status(operation_active=_no_worker)["transition"]["cancel_available"]
        is False
    )

    service.finish_healthcheck(
        operation_id=operation_id,
        system_build=build,
        passed=False,
        error_code="healthcheck_unavailable",
        error_message="EMS diagnostics reported unavailable",
    )
    status = service.status(operation_active=_no_worker)
    assert status["transition"]["stage"] == STAGE_FAILED_RECOVERABLE
    assert status["transition"]["cancel_available"] is True

    cancelled = service.cancel(operation_id=operation_id)
    assert cancelled["stage"] == "cancelled"
    assert service.status()["active"] is False


def test_status_offers_cancel_for_an_expired_transition(tmp_path):
    """An expired transition's only remaining action is the abandon escape.

    The live wedge: a guided_upgrade whose Admin container was replaced but
    whose reconnect resume never landed sits at admin_reconnect_pending until
    the TTL runs out. Every resume then fails with ``expired`` while status()
    reported neither the expiry nor any available action. status() must expose
    ``expired`` and report the transition as cancellable so the recovery UI
    can offer the escape, and cancel() must then succeed.
    """

    build = _build()
    service, transitions, *_ = _service(
        tmp_path,
        build=build,
        running=_aligned_running(),
        persistent_ref=build.admin_image,
    )
    record = transitions.begin(
        make_transition_record(
            mode="guided_upgrade",
            system_tag=build.canonical_tag,
            build_id=build.build_id,
            revision=build.revision,
            admin_image=build.admin_image,
            admin_digest=build.admin_digest,
            ems_image=build.ems_image,
            ems_digest=build.ems_digest,
            stage=STAGE_ADMIN_RECONNECT_PENDING,
            ttl_seconds=60,
            now=T0,
        )
    )

    fresh = service.status(operation_active=_no_worker)["transition"]
    assert fresh["expired"] is False
    assert fresh["cancel_available"] is False

    service._now = lambda: datetime(2026, 7, 14, 14, 0, 0, tzinfo=timezone.utc)
    status = service.status(operation_active=_no_worker)
    assert status["active"] is True
    expired = status["transition"]
    assert expired["stage"] == STAGE_ADMIN_RECONNECT_PENDING
    assert expired["expired"] is True
    assert expired["cancel_available"] is True
    assert expired["resume_available"] is False

    cancelled = service.cancel(operation_id=record.operation_id)
    assert cancelled["stage"] == STAGE_CANCELLED
    assert service.status()["active"] is False


@pytest.mark.parametrize(
    "diagnostics, expected_error_code",
    [
        ({}, "healthcheck_result_invalid"),
        ({"available": False}, "healthcheck_unavailable"),
        ({"available": True, "summary": {"status": "failed"}}, "healthcheck_failed"),
        ({"available": True, "summary": {"status": "banana"}}, "healthcheck_result_invalid"),
    ],
)
def test_recover_preserves_specific_healthcheck_error(
    tmp_path, diagnostics, expected_error_code
):
    service, transitions, known_good, build, operation_id = _healthcheck_pending_service(
        tmp_path
    )
    prior = _build(tag="v0.7.0", admin_digest="sha256:prior")
    known_good.record(prior)
    preserved = known_good.current()

    health = validate_system_health_result(diagnostics)
    assert health.success is False
    result = service.recover_ems_operation(
        operation_id=operation_id,
        healthcheck_passed=health.success,
        healthcheck_error_code=health.error_code,
        healthcheck_error_message=health.message,
    )

    assert result["status"] == STAGE_FAILED_RECOVERABLE
    record = transitions.read()
    assert record.stage == STAGE_FAILED_RECOVERABLE
    assert record.error_code == expected_error_code
    # No raw diagnostics value leaks into the persisted message.
    assert "banana" not in (record.error_message or "")
    # A failed resume never touches the previous known-good build.
    assert known_good.current() == preserved

    # The normal apply path derives the same code from the same diagnosis.
    apply_service, apply_transitions, _, apply_build, apply_op = (
        _healthcheck_pending_service(tmp_path / "apply")
    )
    apply_service.finish_healthcheck(
        operation_id=apply_op,
        system_build=apply_build,
        passed=health.success,
        error_code=health.error_code or "healthcheck_failed",
        error_message=health.message,
    )
    assert apply_transitions.read().error_code == expected_error_code


def test_recover_successful_healthcheck_still_completes(tmp_path):
    service, transitions, known_good, build, operation_id = _healthcheck_pending_service(
        tmp_path
    )
    health = validate_system_health_result(
        {"available": True, "summary": {"status": "ok"}}
    )
    result = service.recover_ems_operation(
        operation_id=operation_id,
        healthcheck_passed=health.success,
        healthcheck_error_code=health.error_code,
        healthcheck_error_message=health.message,
    )
    assert result["status"] == STAGE_COMPLETED
    assert transitions.read().error_code is None
    assert known_good.current()["system_tag"] == "v0.8.0"


def test_known_good_cannot_be_written_before_healthcheck_stage(tmp_path):
    service, _, known_good, build, _ = _resources_verified_service(tmp_path)
    with pytest.raises(SystemAlignmentError) as exc:
        service.mark_known_good(system_build=build, healthcheck_passed=True)
    assert exc.value.code == "healthcheck_required"
    assert known_good.current() is None


# --- known-good state -----------------------------------------------------


def test_known_good_requires_healthcheck(tmp_path):
    service, _, known_good, _ = _service(tmp_path, build=_build())
    with pytest.raises(SystemAlignmentError):
        service.mark_known_good(system_build=_build(), healthcheck_passed=False)
    assert known_good.current() is None


def test_successful_healthcheck_writes_known_good_then_completes_once(tmp_path):
    service, transitions, known_good, build, operation_id = _healthcheck_pending_service(tmp_path)
    persisted_at_stages = []
    persist_known_good = known_good.record

    def record_with_stage_check(system_build, **kwargs):
        persisted_at_stages.append(transitions.read().stage)
        return persist_known_good(system_build, **kwargs)

    known_good.record = record_with_stage_check
    completed = service.finish_healthcheck(
        operation_id=operation_id,
        system_build=build,
        passed=True,
    )
    # Persist known-good while the transition is still healthcheck_pending, and
    # only then commit the terminal completed stage.
    assert persisted_at_stages == [STAGE_HEALTHCHECK_PENDING]
    assert completed["status"] == STAGE_COMPLETED
    assert transitions.read().stage == STAGE_COMPLETED
    assert service.is_transition_pending() is False
    persisted = known_good.current()
    assert persisted["system_tag"] == "v0.8.0"
    assert persisted["revision"] == REVISION

    # A terminal operation cannot restart EMS or repeat finalization.
    with pytest.raises(SystemAlignmentError) as exc:
        service.begin_ems_operation(operation_id=operation_id)
    assert exc.value.code == "not_resumable"
    with pytest.raises(SystemAlignmentError) as exc:
        service.finish_healthcheck(
            operation_id=operation_id,
            system_build=build,
            passed=True,
        )
    assert exc.value.code == "not_resumable"
    assert persisted_at_stages == [STAGE_HEALTHCHECK_PENDING]
    assert known_good.current() == persisted


# --- known-good separates the running Admin from the installed EMS (Phase 9) --


def _legacy_completed_service(tmp_path):
    build = _legacy_build()
    service, transitions, known_good, launched = _service(
        tmp_path, build=build, running=_modern_running_admin()
    )
    service.confirm_setup_build(requested_tag="v0.7.0", mode="fresh_install")
    operation_id = transitions.read().operation_id
    service.begin_ems_operation(operation_id=operation_id)
    assert service.claim_ems_operation(operation_id=operation_id) is True
    service.finish_ems_operation(operation_id=operation_id, succeeded=True)
    service.finish_healthcheck(operation_id=operation_id, passed=True)
    return service, transitions, known_good, build, operation_id


def test_legacy_known_good_records_modern_admin_and_legacy_ems(tmp_path):
    service, transitions, known_good, build, _ = _legacy_completed_service(tmp_path)

    kg = known_good.current()
    modern = _modern_running_admin()
    assert kg["system_tag"] == "v0.7.0"
    assert kg["ems_digest"] == build.ems_digest
    assert kg["compatibility_mode"] == "legacy_release"
    assert kg["resource_strategy"] == "release_archive"
    # The recorded running Admin is the modern orchestrator, never v0.7.0's Admin.
    assert kg["admin_digest"] == modern.digest
    assert kg["admin_build_id"] == modern.build_id
    assert kg["admin_digest"] != build.admin_digest
    assert transitions.read().stage == STAGE_COMPLETED


def test_modern_known_good_records_selected_admin_as_running(tmp_path):
    service, transitions, known_good, build, operation_id = (
        _healthcheck_pending_service(tmp_path)
    )
    service.finish_healthcheck(operation_id=operation_id, passed=True)

    kg = known_good.current()
    assert kg["admin_digest"] == build.admin_digest
    assert kg["ems_digest"] == build.ems_digest
    assert kg["compatibility_mode"] == "modern_paired"


def test_failed_legacy_install_writes_no_known_good_and_keeps_modern_admin(tmp_path):
    build = _legacy_build()
    service, transitions, known_good, _ = _service(
        tmp_path, build=build, running=_modern_running_admin()
    )
    service.confirm_setup_build(requested_tag="v0.7.0", mode="fresh_install")
    operation_id = transitions.read().operation_id
    service.begin_ems_operation(operation_id=operation_id)
    assert service.claim_ems_operation(operation_id=operation_id) is True
    service.finish_ems_operation(
        operation_id=operation_id, succeeded=False,
        error_code="ems_deployment_failed", error_message="compose failed",
    )

    record = transitions.read()
    assert record.stage == STAGE_FAILED_RECOVERABLE
    assert known_good.current() is None
    # The orchestrator Admin identity is preserved and never downgraded.
    assert record.orchestrator_admin["digest"] == _modern_running_admin().digest


def test_legacy_recovery_realign_never_downgrades_admin(tmp_path):
    # A recovery re-alignment for a legacy build keeps the modern Admin: no
    # Admin-replacement launcher runs.
    build = _legacy_build()
    service, transitions, _, launched = _service(
        tmp_path, build=build, running=_modern_running_admin()
    )

    started = service.start(requested_tag="v0.7.0", mode="align_existing_install")

    assert launched == []
    assert started["stage"] == STAGE_ADMIN_ALIGNED


def test_healthcheck_reconstructs_target_build_from_transition(tmp_path):
    service, transitions, known_good, _, operation_id = _healthcheck_pending_service(tmp_path)
    completed = service.finish_healthcheck(
        operation_id=operation_id,
        passed=True,
    )
    assert completed["status"] == STAGE_COMPLETED
    assert transitions.read().stage == STAGE_COMPLETED
    assert known_good.current()["build_id"] == "v0.8.0-f7265fc"


def test_cancelled_transition_is_terminal(tmp_path):
    service, transitions, known_good, _, operation_id = _resources_verified_service(tmp_path)
    cancelled = service.cancel(operation_id=operation_id)
    assert cancelled["status"] == STAGE_CANCELLED
    assert transitions.read().stage == STAGE_CANCELLED
    assert known_good.current() is None
    with pytest.raises(SystemAlignmentError) as exc:
        service.begin_ems_operation(operation_id=operation_id)
    assert exc.value.code == "not_resumable"


def _return_recovery_service(tmp_path, *, running_ems):
    old = SystemBuild(
        requested_tag="v0.7.0",
        canonical_tag="v0.7.0",
        channel="stable",
        revision="a" * 40,
        build_id="v0.7.0-aaaaaaa",
        admin_image=f"{ADMIN_IMAGE_REPO}:v0.7.0",
        admin_digest="sha256:old-admin",
        ems_image=f"{EMS_IMAGE_REPO}:v0.7.0",
        ems_digest="sha256:old-ems",
        release_tag="v0.7.0",
    )
    target = _build(admin_digest="sha256:new-admin")

    class Resolver:
        def __init__(self):
            self.builds = {"v0.7.0": old, "v0.8.0": target}
            self.resolved = []

        def resolve(self, tag):
            self.resolved.append(tag)
            return self.builds[tag]

    transitions = PendingTransitionStore(tmp_path / "state")
    known_good = KnownGoodStore(tmp_path / "state")
    known_good.record(old)
    launched = []
    resolver = Resolver()
    service = SystemAlignmentService(
        resolver=resolver,
        transition_store=transitions,
        embedded_resources=FakeEmbedded(),
        known_good_store=known_good,
        current_identity=lambda: ImageIdentity(
            image_ref=target.admin_image,
            digest=target.admin_digest,
            revision=target.revision,
            build_id=target.build_id,
        ),
        current_ems_identity=lambda: running_ems,
        persistent_ref=lambda: target.admin_image,
        launcher=launched.append,
        now=lambda: T0,
    )
    started = service.start(requested_tag="v0.8.0", mode="guided_upgrade")
    operation_id = started["operation_id"]
    service.verify_resources(operation_id=operation_id)
    service.begin_ems_operation(operation_id=operation_id)
    assert service.claim_ems_operation(operation_id=operation_id)
    service.finish_ems_operation(operation_id=operation_id, succeeded=True)
    service.finish_healthcheck(operation_id=operation_id, passed=False)
    return service, transitions, resolver, launched, old, target, operation_id


def test_return_action_refuses_to_misalign_admin_when_target_ems_is_running(tmp_path):
    target = _build(admin_digest="sha256:new-admin")
    running_target = {
        "digest": target.ems_digest,
        "revision": target.revision,
        "channel": target.channel,
        "build_id": target.build_id,
        "release_tag": target.release_tag,
    }
    service, transitions, _, launched, _, _, operation_id = _return_recovery_service(
        tmp_path, running_ems=running_target
    )

    with pytest.raises(SystemAlignmentError) as exc:
        service.return_to_running_build(operation_id=operation_id, confirm=True)

    assert exc.value.code == "admin_already_matches_running_ems"
    assert transitions.read().stage == STAGE_FAILED_RECOVERABLE
    assert launched == []


def test_return_action_aligns_admin_to_verified_running_known_good_ems(tmp_path):
    running_old = {
        "digest": "sha256:old-ems",
        "revision": "a" * 40,
        "channel": "stable",
        "build_id": "v0.7.0-aaaaaaa",
        "release_tag": "v0.7.0",
    }
    service, transitions, resolver, launched, old, _, operation_id = (
        _return_recovery_service(tmp_path, running_ems=running_old)
    )

    result = service.return_to_running_build(
        operation_id=operation_id, confirm=True
    )

    assert result["status"] == "admin_return_started"
    assert result["target_system_tag"] == old.canonical_tag
    assert transitions.read().system_tag == old.canonical_tag
    assert transitions.read().stage == STAGE_ADMIN_RECONNECT_PENDING
    assert len(launched) == 1
    assert resolver.resolved.count("v0.7.0") == 1


# --- read-only Guided Upgrade validation (no transition, no resources) -------


def _running_ems_identity(*, tag, digest, revision="a" * 40, build_id=None):
    return ImageIdentity(
        image_ref=f"{EMS_IMAGE_REPO}:{tag}",
        digest=digest,
        version_label=tag,
        revision=revision,
        channel="stable",
        build_id=build_id or f"{tag}-aaaaaaa",
        release_tag=tag,
    )


def _legacy_build(tag="v0.7.0"):
    return SystemBuild(
        requested_tag=tag, canonical_tag=tag, channel="stable",
        revision="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
        build_id="123456789-1",
        admin_image=f"{ADMIN_IMAGE_REPO}:{tag}", admin_digest="sha256:oldadmin",
        ems_image=f"{EMS_IMAGE_REPO}:{tag}", ems_digest="sha256:oldems",
        release_tag=tag,
    )


def _modern_running_admin():
    return ImageIdentity(
        image_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
        digest="sha256:modern-admin",
        version_label="v0.8.0",
        revision=REVISION,
        channel="stable",
        build_id="v0.8.0-f7265fc",
        release_tag="v0.8.0",
    )


def test_confirm_legacy_records_modern_orchestrator_and_selected_ems(tmp_path):
    # Phase 6: the transition separates the running modern orchestrator Admin from
    # the selected historical EMS build; the historical Admin image is never run.
    build = _legacy_build()
    running_admin = _modern_running_admin()
    service, transitions, _, launched = _service(
        tmp_path, build=build, running=running_admin
    )

    service.confirm_setup_build(requested_tag="v0.7.0", mode="fresh_install")

    record = transitions.read()
    assert record.compatibility_mode == "legacy_release"
    assert record.resource_strategy == "release_archive"
    assert record.orchestrator_admin["digest"] == running_admin.digest
    assert record.orchestrator_admin["build_id"] == running_admin.build_id
    assert record.orchestrator_admin["digest"] != build.admin_digest
    assert record.selected_ems_build["digest"] == build.ems_digest
    assert record.selected_ems_build["version"] == "v0.7.0"
    assert launched == []


def test_confirm_modern_records_selected_admin_as_orchestrator(tmp_path):
    build = _build(admin_digest="sha256:aligned")
    service, transitions, *_ = _service(
        tmp_path,
        build=build,
        running=_aligned_running(),
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
    )

    service.confirm_setup_build(requested_tag="v0.8.0", mode="fresh_install")

    record = transitions.read()
    assert record.compatibility_mode == "modern_paired"
    assert record.resource_strategy == "embedded"
    # No override: the orchestrator is the selected Admin.
    assert record._orchestrator_override() is None
    assert record.orchestrator_admin["digest"] == build.admin_digest


def test_validate_upgrade_target_allows_unprepared_forward_build(tmp_path):
    build = _build()  # v0.8.0 target
    running_ems = _running_ems_identity(tag="v0.7.0", digest="sha256:old-ems")
    service, transitions, *_ = _service(
        tmp_path, build=build, running_ems=running_ems
    )

    result = service.validate_upgrade_target(requested_tag="v0.8.0")

    assert result["ok"] is True
    assert result["valid"] is True
    assert result["upgrade_allowed"] is True
    assert result["system_build"]["ems_image"] == build.ems_image
    assert result["system_build"]["ems_digest"] == build.ems_digest
    assert result["compatibility_mode"] == "modern_paired"
    # Read-only: a mere validation never opens a durable transition or imports.
    assert transitions.read() is None


def test_validate_upgrade_target_reports_the_verified_build_fingerprint(tmp_path):
    # The Guided Upgrade validate emits the same verified-build fingerprint the
    # Fresh Install validate does, so a plan can be bound to the exact resolved
    # pair (tag:channel:revision:build_id:admin_digest:ems_digest).
    build = _build()
    running_ems = _running_ems_identity(tag="v0.7.0", digest="sha256:old-ems")
    service, *_ = _service(tmp_path, build=build, running_ems=running_ems)

    result = service.validate_upgrade_target(requested_tag="v0.8.0")

    assert result["selection_fingerprint"] == ":".join(
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


def test_validate_upgrade_target_recreates_on_invalid_embedded_resources(tmp_path):
    # A matching Admin identity whose embedded resources verify as stale is an
    # admin_recreate_required, not a finished alignment. Validation must reach
    # the same effective decision as execution — never preview "ready" and then
    # unexpectedly recreate.
    build = _build()
    running = _running_for(build)  # identity + digest match the build
    service, transitions, *_ = _service(
        tmp_path,
        build=build,
        running=running,
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
        embedded=FakeEmbedded(fail=True),  # embedded resources invalid
    )

    result = service.validate_upgrade_target(requested_tag="v0.8.0")

    assert result["alignment"] == "admin_recreate_required"
    assert result["admin_update_required"] is True
    # Validation stays read-only: no import, no transition, no Docker state change.
    assert transitions.read() is None


def test_validate_upgrade_target_reports_aligned_when_embedded_valid(tmp_path):
    build = _build()
    running = _running_for(build)
    service, transitions, *_ = _service(
        tmp_path,
        build=build,
        running=running,
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
        embedded=FakeEmbedded(),  # embedded resources valid
    )

    result = service.validate_upgrade_target(requested_tag="v0.8.0")

    assert result["alignment"] == "aligned"
    assert result["admin_update_required"] is False


def test_validate_upgrade_target_legacy_release_keeps_modern_admin(tmp_path):
    build = _legacy_build()
    running_ems = _running_ems_identity(tag="v0.6.0", digest="sha256:older-ems")
    service, transitions, *_ = _service(
        tmp_path, build=build, running_ems=running_ems
    )

    result = service.validate_upgrade_target(requested_tag="v0.7.0")

    assert result["compatibility_mode"] == "legacy_release"
    # The modern Admin stays the orchestrator; it is never downgraded to the
    # historical Admin image.
    assert result["admin_update_required"] is False
    # The historical EMS image remains the selected deployment target.
    assert result["system_build"]["ems_image"] == build.ems_image
    assert result["system_build"]["ems_digest"] == build.ems_digest
    assert transitions.read() is None


def test_validate_legacy_release_reports_compat_and_keeps_admin(tmp_path):
    build = _legacy_build()
    # The default running Admin (modern, different digest) would otherwise force
    # an admin update; the legacy compatibility mode keeps it in place.
    service, transitions, _, launched = _service(tmp_path, build=build)

    result = service.validate(requested_tag="v0.7.0")

    assert result["compatibility_mode"] == "legacy_release"
    assert result["admin_update_required"] is False
    assert launched == []


def test_validate_legacy_release_does_not_deadlock_step1(tmp_path):
    # Regression: selecting v0.7.0 must never leave both Update Admin Server and
    # Continue disabled while the status reports the Admin ready. The historical
    # release uses the release-archive strategy, so an embedded-resource mismatch
    # can never gate it.
    build = _legacy_build()
    service, transitions, _, launched = _service(tmp_path, build=build)

    result = service.validate(requested_tag="v0.7.0")

    assert result["compatibility_mode"] == "legacy_release"
    assert result["resource_strategy"] == "release_archive"
    # Not-applicable is represented explicitly, never as a failed embedded check.
    assert result["embedded_resources_applicable"] is False
    assert result["embedded_resources_valid"] is None
    assert result["resource_status"] == "ready"
    # The modern Admin stays; no downgrade to the historical Admin is offered.
    assert result["admin_update_required"] is False
    # The user can proceed straight to confirmation.
    assert result["next_allowed"] is True
    assert result["confirmation_allowed"] is True
    # The deadlock contract: never both actions disabled while reporting ready.
    assert not (
        result["admin_update_required"] is False and result["next_allowed"] is False
    )
    assert launched == []


def test_validate_modern_paired_reports_embedded_strategy(tmp_path):
    build = _build(admin_digest="sha256:aligned")
    service, *_ = _service(
        tmp_path,
        build=build,
        running=_aligned_running(),
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
    )

    result = service.validate(requested_tag="v0.8.0")

    assert result["compatibility_mode"] == "modern_paired"
    assert result["resource_strategy"] == "embedded"
    assert result["embedded_resources_applicable"] is True
    assert result["embedded_resources_valid"] is True
    assert result["resource_status"] == "ready"
    assert result["next_allowed"] is True


def test_validate_modern_embedded_mismatch_still_blocks_and_is_not_applicable_free(
    tmp_path,
):
    # An embedded build whose bundle is stale is a real failure (recreate), not a
    # not-applicable case: embedded_resources_valid stays False, not None.
    build = _build(admin_digest="sha256:aligned")
    service, transitions, *_ = _service(
        tmp_path,
        build=build,
        running=_aligned_running(),
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
        embedded=FakeEmbedded(fail=True),
    )

    result = service.validate(requested_tag="v0.8.0")

    assert result["resource_strategy"] == "embedded"
    assert result["embedded_resources_applicable"] is True
    assert result["embedded_resources_valid"] is False
    assert result["admin_update_required"] is True
    assert result["next_allowed"] is False
    assert result["confirmation_allowed"] is False


# --- persist-before-launch (guided upgrade context) -----------------------

_FP = "sha256:" + "a" * 64


def test_start_resolved_runs_pre_launch_before_launcher(tmp_path):
    build = _build(admin_digest="sha256:v080admin")  # not aligned -> launches
    service, transitions, _, launched = _service(tmp_path, build=build)

    def pre_launch(record):
        launched.append(("persist", record.operation_id))

    result = service.start_resolved(
        system_build=build, mode="guided_upgrade",
        request_fingerprint=_FP, pre_launch=pre_launch,
    )

    assert result["reconnect"] is True
    # The durable context persist ran before the Admin-replacement launcher.
    assert launched[0][0] == "persist"
    assert launched[0][1] == result["operation_id"]
    assert launched[1].operation_id == result["operation_id"]


def test_start_resolved_pre_launch_failure_blocks_launcher_and_transition(tmp_path):
    build = _build(admin_digest="sha256:v080admin")
    service, transitions, _, launched = _service(tmp_path, build=build)

    def failing(record):
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        service.start_resolved(
            system_build=build, mode="guided_upgrade",
            request_fingerprint=_FP, pre_launch=failing,
        )

    # No Admin replacement launched, and no unusable active transition remains.
    assert launched == []
    assert transitions.read() is None


def test_validate_upgrade_target_reports_admin_alignment_decision(tmp_path):
    build = _build()
    running_ems = _running_ems_identity(tag="v0.7.0", digest="sha256:old-ems")
    # The running Admin is an old floating latest, so alignment is required.
    service, *_ = _service(tmp_path, build=build, running_ems=running_ems)

    result = service.validate_upgrade_target(requested_tag="v0.8.0")

    assert result["alignment"] == "admin_update_required"
    assert result["admin_update_required"] is True


def test_validate_upgrade_target_blocks_a_downgrade(tmp_path):
    target = _build(tag="v0.7.0", admin_digest="sha256:v070admin")
    running_ems = _running_ems_identity(
        tag="v0.8.0", digest="sha256:running-v080ems", build_id="v0.8.0-f7265fc"
    )
    service, transitions, *_ = _service(
        tmp_path, build=target, running_ems=running_ems
    )

    result = service.validate_upgrade_target(requested_tag="v0.7.0")

    assert result["valid"] is True
    assert result["upgrade_allowed"] is False
    assert transitions.read() is None


def test_validate_upgrade_target_reports_running_admin_identity(tmp_path):
    build = _build()  # v0.8.0 target
    running_admin = ImageIdentity(
        image_ref=f"{ADMIN_IMAGE_REPO}:v0.7.0",
        digest="sha256:running-admin",
        version_label="v0.7.0",
        revision="a" * 40,
        build_id="v0.7.0-aaaaaaa",
    )
    running_ems = _running_ems_identity(tag="v0.7.0", digest="sha256:old-ems")
    service, *_ = _service(
        tmp_path, build=build, running=running_admin, running_ems=running_ems
    )

    result = service.validate_upgrade_target(requested_tag="v0.8.0")

    # Current Admin reflects the Admin's own image, never the EMS/target build.
    assert result["current_admin"]["system_tag"] == "v0.7.0"
    assert result["current_admin"]["digest"] == "sha256:running-admin"
    assert result["current_admin"]["digest"] != build.ems_digest
    assert result["current_admin"]["digest"] != build.admin_digest


def test_validate_upgrade_target_does_not_import_embedded_resources(tmp_path):
    build = _build()
    embedded = FakeEmbedded()
    running_ems = _running_ems_identity(tag="v0.7.0", digest="sha256:old-ems")
    service, *_ = _service(
        tmp_path, build=build, embedded=embedded, running_ems=running_ems
    )

    service.validate_upgrade_target(requested_tag="v0.8.0")

    assert embedded.imported == []
    assert embedded.verified == []


# --- embedded resource mismatch: shared effective-alignment decision --------


def test_validate_aligned_identity_with_bad_resources_requires_recreate(tmp_path):
    # Admin identity matches, but the embedded resources fail to verify. The
    # combined verdict must not read as a finished "aligned" state; it becomes a
    # recreate so Align Admin activates instead of stranding both actions.
    build = _build(admin_digest="sha256:aligned")
    service, transitions, *_ = _service(
        tmp_path,
        build=build,
        running=_aligned_running(),
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
        embedded=FakeEmbedded(fail=True),
    )
    result = service.validate(requested_tag="v0.8.0")
    assert result["alignment"] == "admin_recreate_required"
    assert result["admin_update_required"] is True
    assert result["embedded_resources_valid"] is False
    assert result["next_allowed"] is False
    assert result["confirmation_allowed"] is False
    assert transitions.read() is None


def test_start_recreates_admin_when_resources_mismatch(tmp_path):
    # Starting alignment for an identity-aligned build with stale resources must
    # launch a controlled recreate (reconnect), not silently accept "aligned".
    build = _build(admin_digest="sha256:aligned")
    service, transitions, _known, launched = _service(
        tmp_path,
        build=build,
        running=_aligned_running(),
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
        embedded=FakeEmbedded(fail=True),
    )
    result = service.start(requested_tag="v0.8.0", mode="fresh_install")
    assert result["status"] == "admin_alignment_started"
    assert result["reconnect"] is True
    assert result["decision"] == "admin_recreate_required"
    assert len(launched) == 1
    assert transitions.read().stage == STAGE_ADMIN_RECONNECT_PENDING


def test_recreate_with_still_bad_resources_fails_recoverable(tmp_path):
    # After the recreate reconnects, one resource-verification attempt runs. If it
    # still fails, the transition lands in failed_recoverable with no auto-retry.
    build = _build(admin_digest="sha256:aligned")
    service, transitions, _known, _launched = _service(
        tmp_path,
        build=build,
        running=_aligned_running(),
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
        embedded=FakeEmbedded(fail=True),
    )
    started = service.start(requested_tag="v0.8.0", mode="fresh_install")
    operation_id = started["operation_id"]
    service.resume(operation_id=operation_id)
    with pytest.raises(SystemAlignmentError) as excinfo:
        service.verify_resources(operation_id=operation_id)
    assert excinfo.value.code == "system_build_resources_invalid"
    record = transitions.read()
    assert record.stage == STAGE_FAILED_RECOVERABLE
    assert record.error_code == "system_build_resources_invalid"


def test_recreate_completed_with_valid_resources_enables_next(tmp_path):
    # Once the recreate reconnects and resources verify, validate reports a green
    # aligned build with Next enabled.
    build = _build(admin_digest="sha256:aligned")
    embedded = FakeEmbedded()
    service, *_ = _service(
        tmp_path,
        build=build,
        running=_aligned_running(),
        persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0",
        embedded=embedded,
    )
    started = service.start(requested_tag="v0.8.0", mode="fresh_install")
    service.verify_resources(operation_id=started["operation_id"])
    result = service.validate(requested_tag="v0.8.0")
    assert result["alignment"] == "aligned"
    assert result["embedded_resources_valid"] is True
    assert result["resources_verified"] is True
    assert result["next_allowed"] is True


# --- normalized Step 1 action-state contract -------------------------------

def _assert_action_state_invariant(result):
    action = result["action_state"]
    enabled = int(action["admin_update_allowed"]) + int(action["continue_allowed"])
    if action["terminal_error"] is not None:
        assert enabled == 0
        assert action["busy"] is False
    elif action["busy"]:
        assert enabled == 0
        assert action["polling_required"] is True
        assert action["progress_message"]
    else:
        assert enabled == 1
        assert action["polling_required"] is False
        assert action["progress_message"] is None
    assert result["next_allowed"] is action["continue_allowed"]
    assert result["admin_update_allowed"] is action["admin_update_allowed"]


def test_action_state_matrix_latest_development_legacy_and_update(tmp_path):
    latest = dataclasses.replace(
        _build(tag="latest", admin_digest="sha256:latest"),
        channel="latest",
        build_id="latest-f7265fc",
        release_tag="latest",
    )
    development = _development_build()
    legacy = _legacy_build()
    cases = (
        (
            "latest-aligned",
            latest,
            _running_for(latest),
            latest.admin_image,
            "continue",
        ),
        (
            "latest-update",
            latest,
            _aligned_running(),
            latest.admin_image,
            "admin_update",
        ),
        (
            "development-aligned",
            development,
            _running_for(development),
            development.admin_image,
            "continue",
        ),
        (
            "development-update",
            development,
            _aligned_running(),
            development.admin_image,
            "admin_update",
        ),
        (
            "v070-legacy",
            legacy,
            _aligned_running(),
            f"{ADMIN_IMAGE_REPO}:latest",
            "continue",
        ),
    )

    for name, build, running, persistent_ref, expected in cases:
        service, *_ = _service(
            tmp_path / name,
            build=build,
            running=running,
            persistent_ref=persistent_ref,
        )
        result = service.validate(requested_tag=build.canonical_tag)
        _assert_action_state_invariant(result)
        action = result["action_state"]
        assert action[f"{expected}_allowed"] is True
        assert action["selected_build"]["tag"] == build.canonical_tag
        assert action["selected_build"]["channel"] == build.channel
        assert action["selection_fingerprint"]


@pytest.mark.parametrize(
    "stage",
    (
        STAGE_ADMIN_UPDATE_PENDING,
        STAGE_ADMIN_RECONNECT_PENDING,
        STAGE_ADMIN_ALIGNED,
    ),
)
def test_action_state_busy_has_progress_and_polling(tmp_path, stage):
    build = _build(admin_digest="sha256:aligned")
    service, transitions, *_ = _service(
        tmp_path,
        build=build,
        running=_aligned_running(),
        persistent_ref=build.admin_image,
    )
    transitions.begin(
        make_transition_record(
            mode="fresh_install",
            system_tag=build.canonical_tag,
            build_id=build.build_id,
            revision=build.revision,
            admin_image=build.admin_image,
            admin_digest=build.admin_digest,
            ems_image=build.ems_image,
            ems_digest=build.ems_digest,
            stage=stage,
            now=T0,
        )
    )

    result = service.validate(requested_tag=build.canonical_tag)

    _assert_action_state_invariant(result)
    assert result["action_state"]["transition_stage"] == stage
    assert result["action_state"]["operation_id"] == transitions.read().operation_id


def test_action_state_resource_verification_claim_is_busy(tmp_path):
    build = _build(admin_digest="sha256:aligned")
    service, transitions, *_ = _service(
        tmp_path,
        build=build,
        running=_aligned_running(),
        persistent_ref=build.admin_image,
    )
    started = service.start(requested_tag=build.canonical_tag, mode="fresh_install")
    assert transitions.claim_resource_verification(started["operation_id"], now=T0)

    result = service.validate(requested_tag=build.canonical_tag)

    _assert_action_state_invariant(result)
    assert result["action_state"]["busy"] is True
    assert result["action_state"]["resource_state"] == "ready"


@pytest.mark.parametrize(
    "stage",
    (
        STAGE_RESOURCES_VERIFIED,
        STAGE_EMS_OPERATION_PENDING,
        STAGE_EMS_OPERATION_RUNNING,
        STAGE_HEALTHCHECK_PENDING,
    ),
)
def test_action_state_verified_forward_stages_allow_continue(tmp_path, stage):
    build = _build(admin_digest="sha256:aligned")
    service, transitions, *_ = _service(
        tmp_path,
        build=build,
        running=_aligned_running(),
        persistent_ref=build.admin_image,
    )
    transitions.begin(
        make_transition_record(
            mode="fresh_install",
            system_tag=build.canonical_tag,
            build_id=build.build_id,
            revision=build.revision,
            admin_image=build.admin_image,
            admin_digest=build.admin_digest,
            ems_image=build.ems_image,
            ems_digest=build.ems_digest,
            stage=stage,
            now=T0,
        )
    )

    result = service.validate(requested_tag=build.canonical_tag)

    _assert_action_state_invariant(result)
    assert result["action_state"]["continue_allowed"] is True


def test_action_state_other_build_is_actionable_terminal_error(tmp_path):
    selected = _legacy_build()
    other = _build(tag="v0.9.0", admin_digest="sha256:other")
    service, transitions, *_ = _service(tmp_path, build=selected)
    transitions.begin(
        make_transition_record(
            mode="fresh_install",
            system_tag=other.canonical_tag,
            build_id=other.build_id,
            revision=other.revision,
            admin_image=other.admin_image,
            admin_digest=other.admin_digest,
            ems_image=other.ems_image,
            ems_digest=other.ems_digest,
            stage=STAGE_RESOURCES_VERIFIED,
            now=T0,
        )
    )

    result = service.validate(requested_tag=selected.canonical_tag)

    _assert_action_state_invariant(result)
    error = result["action_state"]["terminal_error"]
    assert error["code"] == "transition_active_for_another_build"
    assert other.canonical_tag in error["message"]
    assert selected.canonical_tag in error["message"]


def test_action_state_stale_build_fingerprint_is_actionable_terminal_error(tmp_path):
    selected = _build(admin_digest="sha256:new-admin")
    service, transitions, *_ = _service(tmp_path, build=selected)
    transitions.begin(
        make_transition_record(
            mode="fresh_install",
            system_tag=selected.canonical_tag,
            build_id=selected.build_id,
            revision=selected.revision,
            admin_image=selected.admin_image,
            admin_digest="sha256:old-admin",
            ems_image=selected.ems_image,
            ems_digest=selected.ems_digest,
            stage=STAGE_RESOURCES_VERIFIED,
            now=T0,
        )
    )

    result = service.validate(requested_tag=selected.canonical_tag)

    _assert_action_state_invariant(result)
    error = result["action_state"]["terminal_error"]
    assert error["code"] == "transition_stale_for_selected_build"
    assert selected.canonical_tag in error["message"]
    assert result["resources_verified"] is False


@pytest.mark.parametrize(
    "code",
    (
        "system_build_admin_unavailable",
        "system_build_ems_unavailable",
        "system_build_mismatch",
        "system_build_invalid_tag",
    ),
)
def test_terminal_validation_action_state_disables_both_actions(code):
    action = terminal_system_build_action_state(
        "v0.7.0", code, "Correct the selected image or catalogue entry and retry."
    )

    assert action["admin_update_allowed"] is False
    assert action["continue_allowed"] is False
    assert action["busy"] is False
    assert action["terminal_error"]["code"] == code
    assert action["terminal_error"]["message"].startswith("Correct")


# --- expired transitions must be worker-aware before abandonment ------------
#
# TTL expiry proves every forward path is closed, not that the mutating worker
# for the operation has stopped. Abandon and Resume therefore consult live
# worker state, not the clock alone.

LATER = datetime(2026, 7, 14, 14, 0, 0, tzinfo=timezone.utc)


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _running_ems_service(tmp_path):
    """A guided_upgrade transition parked at ems_operation_running."""

    service, transitions, known_good, build, operation_id = _resources_verified_service(
        tmp_path
    )
    service.begin_ems_operation(operation_id=operation_id)
    assert service.claim_ems_operation(operation_id=operation_id) is True
    assert transitions.read().stage == STAGE_EMS_OPERATION_RUNNING
    return service, transitions, known_good, build, operation_id


def _blocking_upgrade_worker(registry, coordinator, operation_id):
    """Submit a real upgrade worker that claims the coordinator until released.

    Exercises the production path: the registry claims the operation through the
    coordinator before starting the worker thread and releases it when the
    worker finishes, so ``coordinator.is_active`` mirrors the worker lifecycle.
    """

    release = threading.Event()
    job = UpgradeJob(operation_id + "-job", [])

    def runner(handle):
        release.wait(5)
        handle.finish(
            {"ok": True, "status": "succeeded", "steps": [], "warnings": []}
        )

    submitted, created = registry.get_or_submit(
        operation_id, job, runner, coordinator=coordinator
    )
    assert created is True and submitted is not None
    assert _wait_until(lambda: coordinator.is_active(operation_id))
    return release


def _new_build_record(build, *, now):
    return make_transition_record(
        mode="guided_upgrade",
        system_tag=build.canonical_tag,
        build_id=build.build_id,
        revision=build.revision,
        admin_image=build.admin_image,
        admin_digest=build.admin_digest,
        ems_image=build.ems_image,
        ems_digest=build.ems_digest,
        stage=STAGE_RESOURCES_VERIFIED,
        now=now,
    )


def test_expired_running_transition_blocks_abandon_while_upgrade_worker_alive(tmp_path):
    service, transitions, _, build, operation_id = _running_ems_service(tmp_path)
    coordinator = OperationCoordinator()
    registry = UpgradeJobRegistry()
    release = _blocking_upgrade_worker(registry, coordinator, operation_id)
    try:
        service._now = lambda: LATER

        status = service.status(operation_active=coordinator.is_active)
        transition = status["transition"]
        assert transition["expired"] is True
        assert transition["worker_active"] is True
        assert transition["worker_status_available"] is True
        assert transition["resume_available"] is False
        assert transition["cancel_available"] is False

        with pytest.raises(SystemAlignmentError) as exc:
            service.cancel(operation_id=operation_id, coordinator=coordinator)
        assert exc.value.code == "transition_worker_active"
        assert transitions.read().stage == STAGE_EMS_OPERATION_RUNNING

        # A new operation cannot begin while the abandoned record is still
        # non-terminal and the old worker keeps mutating.
        with pytest.raises(TransitionStateError) as begin_exc:
            transitions.begin(_new_build_record(build, now=LATER))
        assert begin_exc.value.reason == "transition_active"
    finally:
        release.set()

    assert _wait_until(lambda: not coordinator.is_active(operation_id))

    status = service.status(operation_active=coordinator.is_active)
    assert status["transition"]["worker_active"] is False
    assert status["transition"]["cancel_available"] is True

    cancelled = service.cancel(operation_id=operation_id, coordinator=coordinator)
    assert cancelled["stage"] == STAGE_CANCELLED
    # A worker that tries to register after abandonment is refused.
    assert coordinator.claim(operation_id) is None

    # With the worker gone and the transition terminal, a fresh operation begins.
    restarted = transitions.begin(_new_build_record(build, now=LATER))
    assert restarted.operation_id != operation_id


def test_expired_healthcheck_transition_blocks_abandon_while_worker_alive(tmp_path):
    service, transitions, _, build, operation_id = _healthcheck_pending_service(tmp_path)
    coordinator = OperationCoordinator()
    registry = UpgradeJobRegistry()
    release = _blocking_upgrade_worker(registry, coordinator, operation_id)
    try:
        service._now = lambda: LATER

        transition = service.status(operation_active=coordinator.is_active)[
            "transition"
        ]
        assert transition["stage"] == STAGE_HEALTHCHECK_PENDING
        assert transition["expired"] is True
        assert transition["worker_active"] is True
        assert transition["resume_available"] is False
        assert transition["cancel_available"] is False

        with pytest.raises(SystemAlignmentError) as exc:
            service.cancel(operation_id=operation_id, coordinator=coordinator)
        assert exc.value.code == "transition_worker_active"
        assert transitions.read().stage == STAGE_HEALTHCHECK_PENDING
    finally:
        release.set()

    assert _wait_until(lambda: not coordinator.is_active(operation_id))
    cancelled = service.cancel(operation_id=operation_id, coordinator=coordinator)
    assert cancelled["stage"] == STAGE_CANCELLED


def test_expired_transition_without_matching_worker_stays_escapable(tmp_path):
    # A live worker for a *different* operation must not block this abandon,
    # and an expired orphan with no worker (the Admin-restart case) is escapable.
    service, transitions, _, build, operation_id = _running_ems_service(tmp_path)
    coordinator = OperationCoordinator()
    service._now = lambda: LATER
    other_token = coordinator.claim("some-other-operation")
    assert other_token is not None

    status = service.status(operation_active=coordinator.is_active)
    transition = status["transition"]
    assert transition["expired"] is True
    assert transition["worker_active"] is False
    assert transition["worker_status_available"] is True
    assert transition["cancel_available"] is True
    assert transition["resume_available"] is False

    cancelled = service.cancel(operation_id=operation_id, coordinator=coordinator)
    assert cancelled["stage"] == STAGE_CANCELLED


@pytest.mark.parametrize(
    "stage,extra",
    [
        (
            STAGE_FAILED_RECOVERABLE,
            {
                "failed_stage": STAGE_ADMIN_ALIGNED,
                "resume_stage": STAGE_ADMIN_ALIGNED,
                "error_code": "resource_failed",
                "error_message": "resource verification failed",
            },
        ),
        (STAGE_EMS_OPERATION_PENDING, {}),
        (STAGE_EMS_OPERATION_RUNNING, {}),
        (STAGE_HEALTHCHECK_PENDING, {}),
    ],
)
def test_expired_resumable_stage_never_offers_resume(tmp_path, stage, extra):
    build = _build()
    service, transitions, *_ = _service(
        tmp_path,
        build=build,
        running=_aligned_running(),
        persistent_ref=build.admin_image,
    )
    transitions.begin(
        make_transition_record(
            mode="guided_upgrade",
            system_tag=build.canonical_tag,
            build_id=build.build_id,
            revision=build.revision,
            admin_image=build.admin_image,
            admin_digest=build.admin_digest,
            ems_image=build.ems_image,
            ems_digest=build.ems_digest,
            stage=stage,
            ttl_seconds=60,
            now=T0,
            **extra,
        )
    )

    fresh = service.status()["transition"]
    assert fresh["expired"] is False
    assert fresh["resume_available"] is True

    service._now = lambda: LATER
    expired = service.status()["transition"]
    assert expired["expired"] is True
    assert expired["resume_available"] is False


def test_stale_worker_completion_cannot_finish_a_cancelled_transition(tmp_path):
    # The old worker may keep running its own process after the user abandons
    # the transition, but its completion callbacks must never revive a terminal
    # record or write known-good behind the operator's back.
    service, transitions, known_good, build, operation_id = _healthcheck_pending_service(
        tmp_path
    )
    service._now = lambda: LATER
    cancelled = service.cancel(operation_id=operation_id)
    assert cancelled["stage"] == STAGE_CANCELLED

    with pytest.raises(SystemAlignmentError) as health_exc:
        service.finish_healthcheck(
            operation_id=operation_id, passed=True, system_build=build
        )
    assert health_exc.value.code == "not_resumable"

    with pytest.raises(SystemAlignmentError) as ems_exc:
        service.finish_ems_operation(operation_id=operation_id, succeeded=True)
    assert ems_exc.value.code == "not_resumable"

    assert known_good.current() is None
    assert transitions.read().stage == STAGE_CANCELLED


def test_stale_worker_completion_cannot_touch_a_newer_operation(tmp_path):
    # After abandonment a fresh operation may own the store. The old worker's
    # completion, keyed by its own (now-terminal) operation id, is rejected as
    # a mismatch and never mutates the newer transition.
    service, transitions, _, build, operation_id = _running_ems_service(tmp_path)
    service._now = lambda: LATER
    service.cancel(operation_id=operation_id)
    newer = transitions.begin(_new_build_record(build, now=LATER))
    assert newer.operation_id != operation_id

    with pytest.raises(SystemAlignmentError) as exc:
        service.finish_ems_operation(operation_id=operation_id, succeeded=True)
    assert exc.value.code == "operation_mismatch"
    assert transitions.read().operation_id == newer.operation_id
    assert transitions.read().stage == STAGE_RESOURCES_VERIFIED


def test_stale_worker_cannot_mark_a_newer_build_known_good(tmp_path):
    # A worker that lost the abandonment race must not write known-good for the
    # newer transition it never owned: its healthcheck completion is rejected by
    # the operation-id guard before any known-good is recorded.
    service, transitions, known_good, build, operation_id = _running_ems_service(
        tmp_path
    )
    service._now = lambda: LATER
    service.cancel(operation_id=operation_id)
    newer = transitions.begin(_new_build_record(build, now=LATER))
    assert newer.operation_id != operation_id

    with pytest.raises(SystemAlignmentError) as exc:
        service.finish_healthcheck(
            operation_id=operation_id, passed=True, system_build=build
        )
    assert exc.value.code == "operation_mismatch"
    assert known_good.current() is None
    assert transitions.read().operation_id == newer.operation_id


# --- worker-liveness lookup failures fail closed ----------------------------
#
# The original bug: a liveness probe that raised was swallowed to "inactive",
# so a registry/lock fault silently enabled Abandon. Unknown worker state must
# instead fail closed for both status and cancel.


def _raising_probe(_operation_id):
    raise RuntimeError("registry unavailable")


def test_status_fails_closed_when_worker_liveness_lookup_raises(tmp_path):
    service, transitions, _, build, operation_id = _running_ems_service(tmp_path)
    service._now = lambda: LATER

    transition = service.status(operation_active=_raising_probe)["transition"]
    assert transition["expired"] is True
    assert transition["worker_active"] is None
    assert transition["worker_status_available"] is False
    assert transition["resume_available"] is False
    assert transition["cancel_available"] is False
    # A read-only status never mutates the durable record.
    assert transitions.read().stage == STAGE_EMS_OPERATION_RUNNING


def test_status_without_liveness_provider_reports_worker_state_unavailable(tmp_path):
    # No provider means worker liveness cannot be observed at all. That is not
    # proof of inactivity: status must report the state unknown and keep the
    # abandon escape closed until a real probe is supplied.
    service, transitions, _, build, operation_id = _running_ems_service(tmp_path)
    service._now = lambda: LATER

    transition = service.status()["transition"]
    assert transition["expired"] is True
    assert transition["worker_active"] is None
    assert transition["worker_status_available"] is False
    assert transition["resume_available"] is False
    assert transition["cancel_available"] is False
    assert transitions.read().stage == STAGE_EMS_OPERATION_RUNNING


def test_status_with_invalid_liveness_provider_fails_closed(tmp_path):
    service, transitions, _, build, operation_id = _running_ems_service(tmp_path)
    service._now = lambda: LATER

    transition = service.status(operation_active=object())["transition"]
    assert transition["worker_active"] is None
    assert transition["worker_status_available"] is False
    assert transition["resume_available"] is False
    assert transition["cancel_available"] is False


def test_status_with_proven_inactive_provider_offers_abandon(tmp_path):
    # A valid provider that answers False is a successful liveness lookup: the
    # worker is proven gone and the expired transition stays escapable. This is
    # the Admin-restart orphan contract (empty coordinator, valid probe).
    service, transitions, _, build, operation_id = _running_ems_service(tmp_path)
    service._now = lambda: LATER

    transition = service.status(operation_active=lambda _operation_id: False)[
        "transition"
    ]
    assert transition["worker_active"] is False
    assert transition["worker_status_available"] is True
    assert transition["resume_available"] is False
    assert transition["cancel_available"] is True

    cancelled = service.cancel(operation_id=operation_id)
    assert cancelled["stage"] == STAGE_CANCELLED


def test_cancel_fails_closed_when_worker_liveness_cannot_be_verified(tmp_path):
    service, transitions, _, build, operation_id = _running_ems_service(tmp_path)
    service._now = lambda: LATER

    class _UnavailableCoordinator:
        def abandon(self, operation_id, cancel):
            raise OperationWorkerStatusUnavailable(operation_id)

    with pytest.raises(SystemAlignmentError) as exc:
        service.cancel(
            operation_id=operation_id, coordinator=_UnavailableCoordinator()
        )
    assert exc.value.code == "transition_worker_status_unavailable"
    # Fail closed: the transition is untouched and a new operation stays blocked.
    assert transitions.read().stage == STAGE_EMS_OPERATION_RUNNING
    with pytest.raises(TransitionStateError) as begin_exc:
        transitions.begin(_new_build_record(build, now=LATER))
    assert begin_exc.value.reason == "transition_active"


def test_abandon_and_worker_claim_never_both_win_through_the_service(tmp_path):
    # The service abandon commits its durable cancel while holding the
    # coordinator lock, so a worker that tries to claim the same operation
    # mid-cancel is rejected and can never be active against a cancelled record.
    service, transitions, _, build, operation_id = _running_ems_service(tmp_path)
    coordinator = OperationCoordinator()
    service._now = lambda: LATER

    entered = threading.Event()
    finish = threading.Event()
    original_commit = service._commit_cancel
    outcome = {}

    def slow_commit(op):
        entered.set()
        assert finish.wait(2)
        return original_commit(op)

    service._commit_cancel = slow_commit

    def abandon():
        outcome["cancelled"] = service.cancel(
            operation_id=operation_id, coordinator=coordinator
        )

    def claim():
        assert entered.wait(2)
        outcome["token"] = coordinator.claim(operation_id)

    ta = threading.Thread(target=abandon)
    tc = threading.Thread(target=claim)
    ta.start()
    tc.start()
    assert entered.wait(2)
    finish.set()
    ta.join(2)
    tc.join(2)

    assert outcome["cancelled"]["stage"] == STAGE_CANCELLED
    assert outcome["token"] is None
    assert transitions.read().stage == STAGE_CANCELLED
    assert coordinator.is_active(operation_id) is False
