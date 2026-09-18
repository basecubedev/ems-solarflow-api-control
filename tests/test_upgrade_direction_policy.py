# SPDX-License-Identifier: AGPL-3.0-or-later
"""The upgrade-direction policy, as a table that runs.

``assess_upgrade`` states its rules as six numbered steps in a docstring, and
the order of those steps is the policy: a digest beats a channel, a channel
beats SemVer, SemVer beats a declared release, a declared release beats a build
serial. Reordering any two of them changes what an operator may install without
changing a single test that names only one rule.

So every rule gets a row, written from the docstring rather than from the code,
and every row names which signal is expected to have decided it. A verdict that
comes out right for the wrong reason is a rule that has stopped applying and
has not been noticed yet.
"""

import pytest

from admin.image_identity import (
    ALREADY_CURRENT,
    DOWNGRADE_BLOCKED,
    IDENTITY_UNKNOWN,
    OLDER_THAN_RUNNING_BUILD,
    ROLLBACK_AVAILABLE,
    UPGRADE_AVAILABLE,
    ImageIdentity,
    assess_upgrade,
)
from admin.releases import _version

pytestmark = [
    pytest.mark.admin,
    pytest.mark.system_build,
    pytest.mark.contract,
    pytest.mark.simulation,
]


def _build(*, channel, tag=None, declares=None, serial=None, digest="sha256:x"):
    """One side of a move, described the way an image describes itself."""

    return ImageIdentity(
        digest=digest,
        channel=channel,
        version_label=tag,
        release_tag=tag,
        build_serial=serial,
        contains_release=declares,
    )


def _move(running, target, *, rolling=False, allow_unverified=False):
    def own_version(side):
        tag = side.release_tag or side.version_label
        return _version(tag) if tag else None

    return assess_upgrade(
        running,
        target,
        current_version=own_version(running),
        target_version=None if rolling else own_version(target),
        allow_unverified=allow_unverified,
        target_rolling=rolling,
        target_contains_version=(
            _version(target.contains_release) if target.contains_release else None
        ),
        current_contains_version=(
            _version(running.contains_release) if running.contains_release else None
        ),
    )


STABLE = dict(channel="stable")
DEV = dict(channel="development")
LATEST = dict(channel="latest")

# rule, running side, target side, expected state, expected deciding signal.
POLICY = [
    (
        "1 the same image is the same build, whatever either side is called",
        _build(**STABLE, tag="v0.8.4", serial=1300, digest="sha256:same"),
        _build(**DEV, tag="dev-x-aaaaaaa-9-1", declares="v0.7.0", serial=9,
               digest="sha256:same"),
        ALREADY_CURRENT,
        "digest",
    ),
    (
        "2 the rolling channel is always a forward move",
        _build(**STABLE, tag="v0.9.0", serial=1400),
        _build(**LATEST, tag="latest", serial=1, digest="sha256:rolling"),
        UPGRADE_AVAILABLE,
        "channel",
    ),
    (
        "3 a higher release is an upgrade",
        _build(**STABLE, tag="v0.8.4", serial=1300),
        _build(**STABLE, tag="v0.8.7", serial=1450, digest="sha256:t"),
        UPGRADE_AVAILABLE,
        "semver",
    ),
    (
        "3 the same release is no move",
        _build(**STABLE, tag="v0.8.4", serial=1300),
        _build(**STABLE, tag="v0.8.4", serial=1300, digest="sha256:t"),
        ALREADY_CURRENT,
        "semver",
    ),
    (
        "3 an earlier patch of the running line is a rollback",
        _build(**STABLE, tag="v0.8.7", serial=1450),
        _build(**STABLE, tag="v0.8.4", serial=1300, digest="sha256:t"),
        ROLLBACK_AVAILABLE,
        "semver",
    ),
    (
        "3 leaving the line downwards is refused",
        _build(**STABLE, tag="v0.8.4", serial=1300),
        _build(**STABLE, tag="v0.7.0", serial=900, digest="sha256:t"),
        DOWNGRADE_BLOCKED,
        "semver",
    ),
    (
        "4 a development build on a higher release is an upgrade",
        _build(**STABLE, tag="v0.8.4", serial=1300),
        _build(**DEV, tag="dev-x-aaaaaaa-9-1", declares="v0.8.7", serial=9,
               digest="sha256:t"),
        UPGRADE_AVAILABLE,
        "contains_release",
    ),
    (
        "4 a development build on the running release sits on top of it",
        _build(**STABLE, tag="v0.8.4", serial=1300),
        _build(**DEV, tag="dev-x-aaaaaaa-9-1", declares="v0.8.4", serial=9,
               digest="sha256:t"),
        UPGRADE_AVAILABLE,
        "contains_release",
    ),
    (
        "4 going back to the release a development build declares drops commits",
        _build(**DEV, tag="dev-x-aaaaaaa-9-1", declares="v0.8.4", serial=9),
        _build(**STABLE, tag="v0.8.4", serial=1300, digest="sha256:t"),
        ROLLBACK_AVAILABLE,
        "contains_release",
    ),
    (
        "4 a development build from an older line is refused",
        _build(**STABLE, tag="v0.8.4", serial=1300),
        _build(**DEV, tag="dev-x-aaaaaaa-9-1", declares="v0.7.0", serial=9,
               digest="sha256:t"),
        DOWNGRADE_BLOCKED,
        "contains_release",
    ),
    (
        "4 a rolling build reaches an earlier patch of the line it declares",
        _build(**LATEST, tag="latest", declares="v0.8.7", serial=1200),
        _build(**STABLE, tag="v0.8.4", serial=1300, digest="sha256:t"),
        ROLLBACK_AVAILABLE,
        "contains_release",
    ),
    (
        "4 a rolling build reaches a release above the one it declares",
        _build(**LATEST, tag="latest", declares="v0.8.4", serial=1200),
        _build(**STABLE, tag="v0.8.7", serial=1450, digest="sha256:t"),
        UPGRADE_AVAILABLE,
        "contains_release",
    ),
    (
        "5 one counter, a higher serial is an upgrade",
        _build(**DEV, tag="dev-x-aaaaaaa-9-1", declares="v0.8.4", serial=9),
        _build(**DEV, tag="dev-x-bbbbbbb-11-1", declares="v0.8.4", serial=11,
               digest="sha256:t"),
        UPGRADE_AVAILABLE,
        "build_serial",
    ),
    (
        "5 one counter, the same serial is no move",
        _build(**DEV, tag="dev-x-aaaaaaa-9-1", declares="v0.8.4", serial=9),
        _build(**DEV, tag="dev-x-aaaaaaa-9-1", declares="v0.8.4", serial=9,
               digest="sha256:t"),
        ALREADY_CURRENT,
        "build_serial",
    ),
    (
        "5 one counter, a lower serial of the same branch on the declared line is a rollback",
        _build(**DEV, tag="dev-x-bbbbbbb-11-1", declares="v0.8.4", serial=11),
        _build(**DEV, tag="dev-x-aaaaaaa-9-1", declares="v0.8.4", serial=9,
               digest="sha256:t"),
        ROLLBACK_AVAILABLE,
        "build_serial",
    ),
    (
        "5 one counter, a lower serial of another branch on the same line is refused",
        _build(**DEV, tag="dev-x-bbbbbbb-11-1", declares="v0.8.4", serial=11),
        _build(**DEV, tag="dev-y-aaaaaaa-9-1", declares="v0.8.4", serial=9,
               digest="sha256:t"),
        OLDER_THAN_RUNNING_BUILD,
        "build_serial",
    ),
    (
        "5 one counter, a lower serial that cannot say its branch is refused",
        _build(**DEV, tag="dev-x-bbbbbbb-11-1", declares="v0.8.4", serial=11),
        _build(**DEV, declares="v0.8.4", serial=9, digest="sha256:t"),
        OLDER_THAN_RUNNING_BUILD,
        "build_serial",
    ),
    (
        "5 one counter, a lower serial across lines is refused",
        _build(**DEV, tag="dev-x-bbbbbbb-11-1", declares="v0.8.4", serial=11),
        _build(**DEV, tag="dev-y-aaaaaaa-9-1", declares="v0.7.0", serial=9,
               digest="sha256:t"),
        OLDER_THAN_RUNNING_BUILD,
        "build_serial",
    ),
    (
        "5 one counter, a lower serial declaring nothing is refused",
        _build(**DEV, tag="dev-x-bbbbbbb-11-1", declares="v0.8.4", serial=11),
        _build(**DEV, tag="dev-y-aaaaaaa-9-1", serial=9, digest="sha256:t"),
        OLDER_THAN_RUNNING_BUILD,
        "build_serial",
    ),
    (
        "6 nothing can prove a move, so none is offered",
        _build(channel=None, digest="sha256:r"),
        _build(channel=None, digest="sha256:t"),
        IDENTITY_UNKNOWN,
        "none",
    ),
]


@pytest.mark.parametrize(
    "rule, running, target, state, basis",
    POLICY,
    ids=[row[0] for row in POLICY],
)
def test_the_policy_decides_this_and_says_why(rule, running, target, state, basis):
    verdict = _move(running, target, rolling=target.channel == "latest")
    assert (verdict.state, verdict.basis) == (state, basis), rule


@pytest.mark.parametrize(
    "rule, running, target, state, basis",
    POLICY,
    ids=[row[0] for row in POLICY],
)
def test_only_a_refused_move_is_a_blocked_one(rule, running, target, state, basis):
    """The three refusals and nothing else: a rollback is a move, not a block."""

    verdict = _move(running, target, rolling=target.channel == "latest")
    expected_block = state in (
        DOWNGRADE_BLOCKED,
        OLDER_THAN_RUNNING_BUILD,
        IDENTITY_UNKNOWN,
    )
    assert verdict.blocked is expected_block, rule


def test_the_test_override_never_turns_a_downgrade_into_an_upgrade():
    """Step 6's escape hatch reaches only what no rule above it decided."""

    running = _build(**STABLE, tag="v0.8.4", serial=1300)
    lower_line = _build(**STABLE, tag="v0.7.0", serial=900, digest="sha256:t")
    assert _move(running, lower_line, allow_unverified=True).state == DOWNGRADE_BLOCKED

    unknowable = _build(channel=None, digest="sha256:t")
    assert (
        _move(_build(channel=None, digest="sha256:r"), unknowable,
              allow_unverified=True).state
        == UPGRADE_AVAILABLE
    )
