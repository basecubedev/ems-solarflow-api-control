# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every transition stage, under every liveness combination, has a way out.

The wedges this matrix exists for were all found one cell at a time: a stage
that refuses cancellation, a worker nobody could prove had stopped, a sidecar
that died without writing a failure. Each was fixed against the case that was
reported, and the next one showed up somewhere else in the same table.

So the table is the contract. Two things are asserted for every cell: the exact
pair of capabilities the console offers, and the invariant underneath them --
where every mutation that could own the stage is *proven* stopped, an escape
must exist that does not require waiting out the deadline. Where anything is
still running, or cannot be read, no escape is required and none is offered:
that is fail-closed, and it is the answer in roughly half of this table.
"""

from datetime import datetime, timedelta, timezone

import pytest

from admin.admin_update import PendingTransitionStore, make_transition_record
from admin.image_identity import ImageIdentity
from admin.known_good import KnownGoodStore
from admin.system_alignment import SystemAlignmentService
from admin.system_build import SystemBuild

pytestmark = [
    pytest.mark.admin,
    pytest.mark.system_build,
    pytest.mark.workflow,
    pytest.mark.contract,
    pytest.mark.simulation,
]

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
PAST_THE_DEADLINE = NOW + timedelta(seconds=7200)
REVISION = "f" * 40

BUILD = SystemBuild(
    requested_tag="v0.8.6",
    canonical_tag="v0.8.6",
    channel="stable",
    revision=REVISION,
    build_id="v0.8.6-fffffff",
    admin_image="ghcr.io/basecubedev/ems-solarflow-admin:v0.8.6",
    admin_digest="sha256:" + "a" * 64,
    ems_image="ghcr.io/basecubedev/ems-solarflow-api-control:v0.8.6",
    ems_digest="sha256:" + "b" * 64,
    release_tag="v0.8.6",
)

# The stage a transition can sit in, and whether it is one an operator can be
# left in. `completed`/`cancelled` are terminal and out of scope here.
STAGES = (
    "admin_update_pending",
    "admin_reconnect_pending",
    "admin_aligned",
    "resources_verified",
    "ems_operation_pending",
    "ems_operation_running",
    "healthcheck_pending",
    "failed_recoverable",
)

# What might still be mutating, and whether it can be proven either way. The
# two owners are independent: the coordinator sees workers in this process, the
# container probe sees the Admin replacement, and neither can answer for the
# other.
LIVENESS = {
    # label: (worker_active, replacement_probe, admin_is_target, expired)
    "worker running": (True, None, True, False),
    "worker proven stopped": (False, None, True, False),
    "worker state unreadable": (None, None, True, False),
    "replacement running": (False, "active", False, False),
    "replacement gone, Admin not the target": (False, "inactive", False, False),
    "replacement gone, Admin is the target": (False, "inactive", True, False),
    "replacement unprovable": (False, "unknown", False, False),
    "deadline passed": (False, None, True, True),
}

# Hand-written from the rules, never from the implementation: (cancel, resume).
#
#   - a stage outside the externally-owned one is cancellable whenever no
#     worker is proven running;
#   - `admin_reconnect_pending` is owned by the replacement, so only a
#     replacement proven gone whose Admin is not the target opens it;
#   - `ems_operation_running` and `healthcheck_pending` are owned by a worker
#     that the resume path can reconcile, so they resume rather than cancel;
#   - expiry closes every forward path, so it opens cancel and closes resume.
EXPECTED = {
    "admin_update_pending": {
        "worker running": (False, False),
        "worker proven stopped": (True, False),
        "worker state unreadable": (False, False),
        "replacement running": (True, False),
        "replacement gone, Admin not the target": (True, False),
        "replacement gone, Admin is the target": (True, False),
        "replacement unprovable": (True, False),
        "deadline passed": (True, False),
    },
    "admin_reconnect_pending": {
        "worker running": (False, False),
        "worker proven stopped": (False, False),
        "worker state unreadable": (False, False),
        "replacement running": (False, False),
        "replacement gone, Admin not the target": (True, False),
        "replacement gone, Admin is the target": (False, False),
        "replacement unprovable": (False, False),
        "deadline passed": (True, False),
    },
    "admin_aligned": {
        "worker running": (False, False),
        "worker proven stopped": (True, False),
        "worker state unreadable": (False, False),
        "replacement running": (True, False),
        "replacement gone, Admin not the target": (True, False),
        "replacement gone, Admin is the target": (True, False),
        "replacement unprovable": (True, False),
        "deadline passed": (True, False),
    },
    "resources_verified": {
        "worker running": (False, False),
        "worker proven stopped": (True, False),
        "worker state unreadable": (False, False),
        "replacement running": (True, False),
        "replacement gone, Admin not the target": (True, False),
        "replacement gone, Admin is the target": (True, False),
        "replacement unprovable": (True, False),
        "deadline passed": (True, False),
    },
    "ems_operation_pending": {
        "worker running": (False, True),
        "worker proven stopped": (True, True),
        "worker state unreadable": (False, True),
        "replacement running": (True, True),
        "replacement gone, Admin not the target": (True, True),
        "replacement gone, Admin is the target": (True, True),
        "replacement unprovable": (True, True),
        "deadline passed": (True, False),
    },
    "ems_operation_running": {
        "worker running": (False, True),
        "worker proven stopped": (False, True),
        "worker state unreadable": (False, True),
        "replacement running": (False, True),
        "replacement gone, Admin not the target": (False, True),
        "replacement gone, Admin is the target": (False, True),
        "replacement unprovable": (False, True),
        "deadline passed": (True, False),
    },
    "healthcheck_pending": {
        "worker running": (False, True),
        "worker proven stopped": (False, True),
        "worker state unreadable": (False, True),
        "replacement running": (False, True),
        "replacement gone, Admin not the target": (False, True),
        "replacement gone, Admin is the target": (False, True),
        "replacement unprovable": (False, True),
        "deadline passed": (True, False),
    },
    "failed_recoverable": {
        "worker running": (False, True),
        "worker proven stopped": (True, True),
        "worker state unreadable": (False, True),
        "replacement running": (True, True),
        "replacement gone, Admin not the target": (True, True),
        "replacement gone, Admin is the target": (True, True),
        "replacement unprovable": (True, True),
        "deadline passed": (True, False),
    },
}


def _identity(role):
    return ImageIdentity(
        image_ref=BUILD.admin_image if role == "admin" else BUILD.ems_image,
        digest=BUILD.admin_digest if role == "admin" else BUILD.ems_digest,
        revision=REVISION,
        channel="stable",
        build_id=BUILD.build_id,
        release_tag="v0.8.6",
    )


_STRANGER = ImageIdentity(
    image_ref=BUILD.admin_image,
    digest="sha256:" + "9" * 64,
    revision="a" * 40,
    channel="stable",
    build_id="v0.0.1-aaaaaaa",
    release_tag="v0.0.1",
)


class _Embedded:
    def verify(self, *args, **kwargs):
        return {}

    def import_into_cache(self, *args, **kwargs):
        return {}


class _Resolver:
    def resolve(self, requested_tag):
        return BUILD


def _seed(state_dir, stage):
    store = PendingTransitionStore(state_dir)
    record = store.begin(
        make_transition_record(
            mode="guided_upgrade",
            system_tag=BUILD.canonical_tag,
            build_id=BUILD.build_id,
            revision=REVISION,
            admin_image=BUILD.admin_image,
            admin_digest=BUILD.admin_digest,
            ems_image=BUILD.ems_image,
            ems_digest=BUILD.ems_digest,
            stage="ems_operation_pending" if stage == "failed_recoverable" else stage,
            now=NOW,
        ),
        now=NOW,
    )
    if stage == "failed_recoverable":
        store.mark_failed(
            record.operation_id,
            error_code="ems_deployment_failed",
            error_message="the deployment failed",
            resume_stage="ems_operation_pending",
            now=NOW,
        )
    return record


def _service(state_dir, *, replacement, admin_is_target, now):
    return SystemAlignmentService(
        resolver=_Resolver(),
        transition_store=PendingTransitionStore(state_dir),
        embedded_resources=_Embedded(),
        known_good_store=KnownGoodStore(state_dir),
        current_identity=lambda: _identity("admin") if admin_is_target else _STRANGER,
        current_ems_identity=lambda: _identity("ems"),
        persistent_ref=lambda: BUILD.admin_image,
        launcher=lambda _record: pytest.fail("no launch belongs in this matrix"),
        replacement_activity=(lambda _op: replacement) if replacement else None,
        now=lambda: now,
    )


def _cell(tmp_path, stage, situation):
    worker, replacement, admin_is_target, expired = LIVENESS[situation]
    state_dir = tmp_path / "state"
    record = _seed(state_dir, stage)
    service = _service(
        state_dir,
        replacement=replacement,
        admin_is_target=admin_is_target,
        now=PAST_THE_DEADLINE if expired else NOW,
    )
    probe = None if worker is None else (lambda _op: worker)
    transition = service.status(operation_active=probe)["transition"]
    return service, record, transition


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("situation", sorted(LIVENESS))
def test_the_console_offers_exactly_these_ways_out(tmp_path, stage, situation):
    _service_unused, _record, transition = _cell(tmp_path, stage, situation)
    offered = (
        transition["cancel_available"] is True,
        transition["resume_available"] is True,
    )
    assert offered == EXPECTED[stage][situation], (
        f"{stage} / {situation}: offered cancel/resume {offered}, "
        f"expected {EXPECTED[stage][situation]}"
    )


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("situation", sorted(LIVENESS))
def test_a_stage_with_nothing_running_is_never_a_dead_end(tmp_path, stage, situation):
    """The invariant the table exists to protect.

    Where the mutation that owns *this* stage is proven stopped, some way out
    must be offered without waiting out the deadline. Which mutation that is
    differs by stage, and neither owner can answer for the other: the
    coordinator sees workers in this process, the container probe sees the
    Admin replacement. An owner that is running, or that nothing can read --
    including one with no probe at all -- requires no escape, and refusing to
    act on a state that cannot be read is the point rather than a gap in it.
    """

    worker, replacement, admin_is_target, expired = LIVENESS[situation]
    if stage == "admin_reconnect_pending":
        # Owned by the out-of-process replacement. An Admin that *is* the
        # target is that replacement having succeeded: the operation is being
        # continued, not stranded.
        owner_proven_stopped = replacement == "inactive" and not admin_is_target
    else:
        owner_proven_stopped = worker is False
    if expired or not owner_proven_stopped:
        pytest.skip("this stage's owner is running, unreadable, or continuing")

    _service_unused, _record, transition = _cell(tmp_path, stage, situation)
    assert (
        transition["cancel_available"] is True
        or transition["resume_available"] is True
    ), f"{stage} / {situation} offers no way out"


@pytest.mark.parametrize("stage", STAGES)
def test_every_stage_becomes_escapable_once_the_deadline_passes(tmp_path, stage):
    """The floor under the whole table: waiting always eventually works."""

    service, record, transition = _cell(tmp_path, stage, "deadline passed")
    assert transition["cancel_available"] is True
    cancelled = service.cancel(operation_id=record.operation_id)
    assert cancelled["stage"] == "cancelled"


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("situation", sorted(LIVENESS))
def test_the_store_refuses_exactly_what_the_console_does_not_offer(
    tmp_path, stage, situation
):
    """The console must not offer an action the durable store will refuse.

    The two used to be derived separately, which is how a stage came to show an
    enabled button behind a refusal. A console that offers nothing may still be
    refused -- that is the coordinator's word, not the store's -- so only the
    positive direction is a contract.
    """

    service, record, transition = _cell(tmp_path, stage, situation)
    if transition["cancel_available"] is not True:
        return
    cancelled = service.cancel(operation_id=record.operation_id)
    assert cancelled["stage"] == "cancelled"
