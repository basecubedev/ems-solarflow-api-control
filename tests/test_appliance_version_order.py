# SPDX-License-Identifier: AGPL-3.0-or-later
"""One comparator, because two callers used to answer the same question apart.

Both discarded everything after the first hyphen, so a release candidate and
the release it precedes compared equal — in either direction, for both the OS
release gates and the container release index.
"""

import shutil
import subprocess

import pytest

from appliance.version import version_key

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]


@pytest.mark.parametrize(
    "lower,higher",
    [
        ("0.1.0-rc1", "0.1.0"),
        ("1.0.0-rc2", "1.0.0-rc10"),
        ("1.0.0-beta", "1.0.0-rc1"),
        ("0.9.0", "0.10.0"),
        ("0.1.0", "0.2.0"),
        ("v1.0.0-rc1", "v1.0.0"),
        ("1.0.0-rc1", "1.0.1-rc1"),
    ],
)
def test_the_order_is_the_one_a_release_gate_needs(lower, higher):
    assert version_key(lower) < version_key(higher)


def test_a_candidate_is_never_equal_to_the_release_it_precedes():
    """The defect: a downgrade guard could not see an rc-to-final step at all."""

    assert version_key("0.1.0-rc1") != version_key("0.1.0")


def test_a_leading_v_is_not_part_of_the_version():
    assert version_key("v1.2.3") == version_key("1.2.3")


@pytest.mark.parametrize("text", ["", None, "not-a-version", "..", "1.2.3.4.5"])
def test_an_unparseable_version_degrades_rather_than_raising(text):
    """These keys gate installs; a refusal must come from a rule, not a crash."""

    assert isinstance(version_key(text), tuple)


def test_build_metadata_does_not_change_precedence():
    """Semver's rule, and the one this keeps: build metadata is not a version.

    Two artifacts of the same version built twice are the same version. What
    tells them apart is the build id, which the gates compare separately.
    """

    assert version_key("1.0.0+build7") == version_key("1.0.0")
    assert version_key("1.0.0-rc1+build7") < version_key("1.0.0")


# --- the packaging's authority, and ours, must agree -------------------------


DEBIAN_ORDER = (
    "0.1.0~rc1",
    "0.1.0~rc2",
    "0.1.0",
    "0.1.1~rc1",
    "0.1.1",
    "0.2.0",
    "1.0.0~rc1",
    "1.0.0",
)


def test_the_tilde_form_ranks_below_the_release_it_precedes():
    """Splitting only on the hyphen made this form invisible.

    `0.1.0~rc1` parsed as the release itself and compared equal — the same
    defect this comparator was written to fix, in the spelling Debian uses.
    """

    assert version_key("0.1.0~rc1") < version_key("0.1.0")
    assert version_key("0.1.0~rc1") < version_key("0.1.0~rc2")


@pytest.mark.skipif(shutil.which("dpkg") is None, reason="dpkg orders the packaged form")
@pytest.mark.parametrize(
    "lower,higher", [(DEBIAN_ORDER[i], DEBIAN_ORDER[i + 1]) for i in range(len(DEBIAN_ORDER) - 1)]
)
def test_dpkg_and_this_comparator_order_a_release_the_same_way(lower, higher):
    """Two authorities on one question, and the appliance obeys both.

    dpkg decides whether an install is an upgrade; this comparator decides
    whether the appliance calls it a downgrade. A disagreement means one of them
    offers a step the other refuses to name correctly — and for the hyphen form
    they *do* disagree: dpkg reads `-rc1` as a revision above the release.
    """

    assert version_key(lower) < version_key(higher)
    assert subprocess.run(
        ["dpkg", "--compare-versions", lower, "lt", higher], check=False
    ).returncode == 0


@pytest.mark.skipif(shutil.which("dpkg") is None, reason="dpkg orders the packaged form")
def test_the_hyphen_form_is_the_one_that_disagrees():
    """Why the packaging must use tildes, asserted rather than remembered."""

    assert version_key("0.1.0-rc1") < version_key("0.1.0")
    assert subprocess.run(
        ["dpkg", "--compare-versions", "0.1.0-rc1", "gt", "0.1.0"], check=False
    ).returncode == 0, "dpkg no longer sorts a hyphen revision above the release"


def test_the_shipped_version_is_a_form_both_authorities_read_alike():
    from appliance.version import APPLIANCE_VERSION

    assert "-" not in APPLIANCE_VERSION, (
        "a hyphen makes dpkg and version_key disagree; spell a pre-release with ~"
    )


# --- the Manager's own tag namespace -----------------------------------------


def test_a_manager_tag_is_not_readable_as_an_ems_version():
    """The reason the prefix exists, and the reason it looks the way it does.

    ``admin/releases.py`` offers every non-draft release of this repository as
    an EMS system-build target and decides eligibility by parsing the tag. A
    Manager tag that parsed as a version would offer operators an EMS build
    whose container images do not exist. Run through the pattern that decides,
    not eyeballed.
    """

    from admin.releases import VERSION_PATTERN
    from appliance.version import tag_for

    for version in ("0.1.0", "1.0.0", "0.2.0~rc1", "10.20.30"):
        assert not VERSION_PATTERN.fullmatch(tag_for(version)), version


def test_a_tag_round_trips_through_the_version_it_names():
    from appliance.version import tag_for, version_from_tag

    for version in ("0.1.0", "1.2.3", "0.2.0~rc1"):
        assert version_from_tag(tag_for(version)) == version


def test_an_ems_tag_is_not_mistaken_for_a_manager_one():
    """Both products tag in one namespace, so reading the wrong one is the
    mistake available here. An EMS tag is not an error, it is someone else's."""

    from appliance.version import version_from_tag

    for tag in ("v0.8.0-RC2", "v0.6.0", "archive-before-timestamp-rewrite", ""):
        assert version_from_tag(tag) == ""


@pytest.mark.parametrize(
    "text,stable",
    [
        ("0.1.0", True),
        ("1.0.0", True),
        ("0.1.0~rc1", False),
        ("0.1.0-rc1", False),
        ("0.1.0~beta.2", False),
    ],
)
def test_a_candidate_is_not_stable(text, stable):
    from appliance.version import is_stable

    assert is_stable(text) is stable


def test_the_newest_stable_wins_and_a_candidate_never_does():
    """What the image bakes in. "Latest" answering with a release candidate is
    the difference between shipping a candidate to every card and shipping
    nothing until a release exists."""

    from appliance.version import latest_stable

    tags = [
        "appliance-manager-v0.1.0",
        "appliance-manager-v0.2.0",
        "appliance-manager-v0.3.0~rc1",
        "v0.8.0-RC2",
        "backup/admin-mqtt-before-cleanup",
    ]

    assert latest_stable(tags) == "0.2.0"


def test_no_stable_tag_yields_no_answer_rather_than_a_guess():
    """Today's state: the namespace exists and holds nothing. A caller must be
    able to tell "none published yet" from "here is one"."""

    from appliance.version import latest_stable

    assert latest_stable(["appliance-manager-v0.1.0~rc1", "v0.8.0-RC2"]) == ""
    assert latest_stable([]) == ""


@pytest.mark.skipif(shutil.which("git") is None, reason="the tags live in git")
def test_the_source_is_never_behind_the_newest_released_tag():
    """The forgotten bump, which nothing caught before.

    ``APPLIANCE_VERSION`` is a hand-edited literal and the only thing the
    package version comes from. Tagging a release without bumping it produces a
    second release carrying the first one's version -- two different packages
    that ``version_key`` reads as the same, which is precisely what the whole
    comparator exists to prevent one layer down.
    """

    from pathlib import Path

    from appliance.version import APPLIANCE_VERSION, latest_stable, version_key

    root = Path(__file__).resolve().parents[1]
    listed = subprocess.run(
        ["git", "tag", "--list"],
        capture_output=True, text=True, check=False, cwd=root, timeout=60,
    )
    if listed.returncode != 0:
        pytest.skip("no git checkout to read tags from")
    released = latest_stable(listed.stdout.split())
    if not released:
        pytest.skip("no Manager release has been tagged yet")

    assert version_key(APPLIANCE_VERSION) >= version_key(released), (
        f"appliance/version.py says {APPLIANCE_VERSION}, "
        f"but {released} is already tagged"
    )
