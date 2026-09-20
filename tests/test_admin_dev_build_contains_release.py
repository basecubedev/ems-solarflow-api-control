# SPDX-License-Identifier: AGPL-3.0-or-later
"""A development build is ordered by the release it contains, not by a run number.

`build_serial` is `GITHUB_RUN_NUMBER`, and a development build is numbered by
`docker-feature-publish.yml` while a release is numbered by
`docker-publish.yml`. The two counters are independent, so comparing them is
not an ordering: run 45 of one workflow was refused as "older" than run 167 of
the other, while its revision was fourteen commits ahead of that release.

The build now declares the newest `v*` tag it descends from, and that decides.
A build whose declared release is at least the running one is a forward move --
it contains that release plus its own commits. A branch cut before the running
release stays refused, which a run-number comparison could not tell apart.
"""

import pytest

from admin.image_identity import (
    DOWNGRADE_BLOCKED,
    IDENTITY_UNKNOWN,
    OLDER_THAN_RUNNING_BUILD,
    ROLLBACK_AVAILABLE,
    UPGRADE_AVAILABLE,
    ImageIdentity,
    assess_upgrade,
    parse_labels,
)
from admin.releases import _version

pytestmark = [
    pytest.mark.admin,
    pytest.mark.contract,
    pytest.mark.simulation,
]


def _running(version="v0.8.4", serial=167):
    return ImageIdentity(
        digest="sha256:running",
        version_label=version,
        release_tag=version,
        channel="stable",
        build_serial=serial,
    )


def _dev(contains, serial=45):
    return ImageIdentity(
        digest="sha256:dev",
        version_label="dev-feat-x-aaaaaaaaaa-1234567-99-1",
        release_tag="dev-feat-x-aaaaaaaaaa-1234567-99-1",
        channel="development",
        build_serial=serial,
        contains_release=contains,
    )


def _assess(running, target):
    return assess_upgrade(
        running,
        target,
        current_version=_version(running.release_tag),
        target_version=None,
        allow_unverified=False,
        target_contains_version=(
            _version(target.contains_release) if target.contains_release else None
        ),
    )


def test_a_development_build_on_the_running_release_is_a_forward_move():
    assessment = _assess(_running(), _dev("v0.8.4"))

    assert assessment.state == UPGRADE_AVAILABLE
    assert assessment.basis == "contains_release"
    assert not assessment.blocked


def test_a_lower_run_number_no_longer_decides_anything():
    """The exact case that was refused: serial 45 against a running 167."""

    assert _assess(_running(serial=167), _dev("v0.8.4", serial=45)).state == (
        UPGRADE_AVAILABLE
    )
    assert _assess(_running(serial=1), _dev("v0.8.4", serial=9999)).state == (
        UPGRADE_AVAILABLE
    )


def test_a_branch_cut_earlier_in_the_running_line_is_a_rollback():
    """The case a run-number comparison cannot see.

    A stale branch rebuilt today carries a high run number and old code. Its
    declared release is what gives it away -- and inside one `major.minor` that
    makes the move a rollback, selectable and named as one, the same answer a
    released patch of that line gets.
    """

    assessment = _assess(_running("v0.8.4"), _dev("v0.8.2", serial=9999))

    assert assessment.state == ROLLBACK_AVAILABLE
    assert assessment.basis == "contains_release"
    assert not assessment.blocked


def test_a_branch_from_an_older_line_is_refused():
    """Across a minor or major boundary a downgrade is blocked, not offered."""

    assessment = _assess(_running("v0.8.4"), _dev("v0.7.9", serial=9999))

    assert assessment.state == DOWNGRADE_BLOCKED
    assert assessment.blocked


def test_a_newer_declared_release_is_also_forward():
    assert _assess(_running("v0.8.2"), _dev("v0.8.4")).state == UPGRADE_AVAILABLE


def test_a_build_without_the_label_is_never_ordered_by_a_run_number():
    """Built before the label existed, so nothing can prove the direction.

    The run numbers must not stand in for one. What used to refuse this move
    was that meaningless comparison, not a judgement, so the honest verdict is
    that the identity is unknown -- and the listing path refuses it on exactly
    that ground.
    """

    assessment = _assess(_running(serial=167), _dev(None, serial=45))

    assert assessment.state == IDENTITY_UNKNOWN
    assert assessment.basis == "none"
    assert assessment.blocked


def test_an_unlabelled_development_target_is_refused_rather_than_waved_through():
    """`decide_upgrade_direction` no longer asks for an unverified pass.

    That flag existed because a development target could not be judged at all.
    It can be now, and where the label is missing the direction is genuinely
    unknown -- so the move is refused instead of proceeding on a warning that
    the plan view never renders and that names a test override which is not in
    play. Nothing is allowed here that would need warning about.
    """

    assessment = _move(_running(serial=167), _dev(None, serial=45))

    assert assessment.state == IDENTITY_UNKNOWN
    assert assessment.blocked
    assert not assessment.warning


def test_the_label_is_read_off_the_image():
    identity = parse_labels(
        {
            "de.basecubedev.ems.channel": "development",
            "de.basecubedev.ems.contains_release": "v0.8.4",
        }
    )

    assert identity.contains_release == "v0.8.4"
    assert identity.as_dict()["contains_release"] == "v0.8.4"


def test_a_missing_label_is_none_not_empty():
    assert parse_labels({"de.basecubedev.ems.channel": "stable"}).contains_release is None


def _rolling(contains, serial=168):
    return ImageIdentity(
        digest="sha256:rolling",
        version_label="latest",
        release_tag="latest",
        channel="latest",
        build_serial=serial,
        contains_release=contains,
    )


def _assess_rolling(running, target):
    return assess_upgrade(
        running,
        target,
        current_version=None,
        target_version=None,
        allow_unverified=False,
        target_contains_version=(
            _version(target.contains_release) if target.contains_release else None
        ),
        current_contains_version=(
            _version(running.contains_release) if running.contains_release else None
        ),
    )


def test_a_rolling_install_states_a_version_through_the_release_it_contains():
    """`latest` has no SemVer of its own, which is what left it uncomparable.

    An installation that has followed the rolling channel for months could only
    say "latest" and a commit hash. The release it was built past is the one
    version such an image can state, and it is enough to order a development
    build against it.
    """

    assessment = _assess_rolling(_rolling("v0.8.4"), _dev("v0.8.4"))

    assert assessment.state == UPGRADE_AVAILABLE
    assert assessment.basis == "contains_release"


def test_a_rolling_install_applies_the_same_downgrade_policy():
    assert _assess_rolling(
        _rolling("v0.8.4"), _dev("v0.8.2", serial=9999)
    ).state == ROLLBACK_AVAILABLE

    blocked = _assess_rolling(_rolling("v0.8.4"), _dev("v0.7.9", serial=9999))
    assert blocked.state == DOWNGRADE_BLOCKED
    assert blocked.blocked


def test_the_running_side_prefers_its_own_semver_over_the_declared_release():
    """A tagged release states its version directly; the label never overrides it."""

    running = ImageIdentity(
        digest="sha256:running",
        version_label="v0.8.4",
        release_tag="v0.8.4",
        channel="stable",
        build_serial=167,
        contains_release="v0.1.0",
    )

    assert _assess(running, _dev("v0.8.4")).state == UPGRADE_AVAILABLE


def _release(version, serial, digest="sha256:release"):
    return ImageIdentity(
        digest=digest,
        version_label=version,
        release_tag=version,
        channel="stable",
        build_serial=serial,
    )


def _move(current, target):
    """Assess a move the way both production callers assemble it."""

    def version_of(identity):
        tag = identity.release_tag
        return _version(tag) if tag and tag != "latest" else None

    def declared(identity):
        return _version(identity.contains_release) if identity.contains_release else None

    return assess_upgrade(
        current,
        target,
        current_version=version_of(current),
        target_version=version_of(target),
        target_contains_version=declared(target),
        current_contains_version=declared(current),
        target_rolling=target.release_tag == "latest",
    )


def test_two_development_builds_are_still_ordered_by_their_run_numbers():
    """Both come from the same workflow, so the serial is a real ordering here.

    Deciding these two by the release they declare would call every move
    between them an upgrade: they usually name the same release, and the one
    that differs is the count of commits on top, which the label cannot show.
    So the serial keeps saying which of the two is newer -- it only stops
    deciding, on its own, whether the backwards move is allowed.
    """

    newer = _dev("v0.8.4", serial=50)
    # Yesterday's build of the same branch: the tag names the branch, because
    # the declared release and the counter are shared with every other branch.
    older = ImageIdentity(
        digest="sha256:older-dev",
        version_label="dev-feat-x-aaaaaaaaaa-1234566-98-1",
        release_tag="dev-feat-x-aaaaaaaaaa-1234566-98-1",
        channel="development",
        build_serial=45,
        contains_release="v0.8.4",
    )

    back = _move(newer, older)
    assert back.state == ROLLBACK_AVAILABLE
    assert back.basis == "build_serial"
    assert not back.blocked

    forward = _move(older, newer)
    assert forward.state == UPGRADE_AVAILABLE
    assert forward.basis == "build_serial"


def test_an_older_development_build_outside_the_line_stays_refused():
    """The serial says "older"; the declarations say it is another line too."""

    running = _dev("v0.8.4", serial=50)
    older_line = ImageIdentity(
        digest="sha256:older-line",
        channel="development",
        build_serial=45,
        contains_release="v0.7.0",
    )

    back = _move(running, older_line)
    assert back.state == OLDER_THAN_RUNNING_BUILD
    assert back.blocked


def test_an_older_development_build_that_declares_nothing_stays_refused():
    """Without a declaration on both sides nothing places the two in one line."""

    running = _dev("v0.8.4", serial=50)
    undeclared = ImageIdentity(
        digest="sha256:undeclared",
        channel="development",
        build_serial=45,
    )

    back = _move(running, undeclared)
    assert back.state == OLDER_THAN_RUNNING_BUILD
    assert back.blocked


def test_a_rolling_install_rolls_back_inside_the_line_it_declares():
    """The owner's policy, applied where a serial could only say "older".

    A `latest` built from v0.8.4 plus a handful of commits was refusing v0.8.4
    itself, because 170 > 167. Once the running image declares the release it
    was built past, a release inside that line is a rollback -- selectable, and
    never proposed -- exactly as it is for a tagged release.
    """

    running = _rolling("v0.8.4", serial=170)

    assert _move(running, _release("v0.8.4", 167)).state == ROLLBACK_AVAILABLE
    assert _move(running, _release("v0.8.3", 160)).state == ROLLBACK_AVAILABLE
    assert _move(running, _release("v0.9.0", 200)).state == UPGRADE_AVAILABLE
    blocked = _move(running, _release("v0.7.0", 120))
    assert blocked.state == DOWNGRADE_BLOCKED and blocked.blocked


def test_a_rolling_install_without_the_label_is_still_ordered_by_run_number():
    """Nothing declared, nothing to place by: the serial decides as before."""

    running = ImageIdentity(
        digest="sha256:rolling", version_label="latest", release_tag="latest",
        channel="latest", build_serial=168,
    )

    assessment = _move(running, _release("v0.8.2", 158))

    assert assessment.state == OLDER_THAN_RUNNING_BUILD
    assert assessment.basis == "build_serial"


def test_the_way_back_to_the_release_the_build_contains_stays_open():
    """A development build must not be a one-way door.

    Returning to the release it was built on removes the branch commits, so it
    is a rollback and is named one -- not refused, and not dressed up as an
    upgrade the way a run-number comparison happened to do.
    """

    assessment = _move(_dev("v0.8.4"), _release("v0.8.4", 167))

    assert assessment.state == ROLLBACK_AVAILABLE
    assert assessment.basis == "contains_release"
    assert not assessment.blocked


def test_the_way_back_to_a_newer_release_is_an_upgrade():
    assessment = _move(_dev("v0.8.4"), _release("v0.9.0", 200))

    assert assessment.state == UPGRADE_AVAILABLE
    assert not assessment.blocked


def test_leaving_a_development_build_for_another_release_line_is_refused():
    assessment = _move(_dev("v0.8.4"), _release("v0.7.0", 100))

    assert assessment.state == DOWNGRADE_BLOCKED
    assert assessment.blocked


def test_the_rolling_channel_is_always_reachable_from_a_development_build():
    assessment = _move(_dev("v0.8.4"), _rolling("v0.8.4"))

    assert assessment.state == UPGRADE_AVAILABLE
    assert assessment.basis == "channel"
    assert not assessment.blocked


def test_a_prerelease_the_branch_was_cut_after_is_still_forward():
    assert _assess(_running("v0.8.4"), _dev("v0.9.0-RC1")).state == UPGRADE_AVAILABLE


def test_a_branch_cut_at_a_prerelease_is_a_rollback_once_that_release_is_out():
    """`v0.9.0-RC1` precedes `v0.9.0`, and both sit in the same `0.9` line."""

    assessment = _assess(_running("v0.9.0"), _dev("v0.9.0-RC1"))

    assert assessment.state == ROLLBACK_AVAILABLE
    assert not assessment.blocked


def test_a_label_that_is_not_a_release_version_decides_nothing():
    """The `v*` restriction is the guard; this is what happens if it ever slips.

    This repository tags appliance images and Manager releases too. A tag from
    one of those parses as no version at all, so the move falls through to the
    paths that admit they cannot prove it -- never to a comparison against it.
    """

    assessment = _assess(_running(serial=167), _dev("appliance-image-v0.1.0", serial=45))

    assert assessment.state == IDENTITY_UNKNOWN
    assert assessment.blocked


def test_an_empty_label_is_the_same_as_no_label():
    """`git describe` prints nothing when no release is an ancestor."""

    assert parse_labels(
        {"de.basecubedev.ems.contains_release": ""}
    ).contains_release is None


def test_the_declared_release_never_decides_inside_one_counter():
    """Two development builds declare the same release; it separates nothing.

    What distinguishes them is the commits each carries on top, which the label
    cannot show. Their serials count runs of one workflow and do order them --
    and where a serial is missing the honest answer is that the direction is
    unknown, not that the declared release makes it an upgrade.
    """

    with_serial = _dev("v0.8.4", serial=50)
    without_serial = ImageIdentity(
        digest="sha256:no-serial",
        channel="development",
        contains_release="v0.8.4",
    )

    assert _move(with_serial, without_serial).state == IDENTITY_UNKNOWN
    assert _move(without_serial, with_serial).state == IDENTITY_UNKNOWN


def test_an_unreadable_image_decides_nothing_and_crashes_nothing():
    assert _move(ImageIdentity(), ImageIdentity()).state == IDENTITY_UNKNOWN
