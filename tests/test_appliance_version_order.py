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


def test_every_version_this_tree_spells_itself_is_read_alike_by_both():
    """The two versions this repository writes rather than receives: what a
    build with no tag calls itself, and the placeholder in ``debian/control``
    that the build overwrites. A release version arrives from a tag and is
    checked by ``is_stable`` in the release chain instead."""

    from pathlib import Path

    from appliance.version import DEVELOPMENT_VERSION_PREFIX

    control = (
        Path(__file__).resolve().parents[1]
        / "packaging" / "appliance" / "debian" / "control"
    ).read_text(encoding="utf-8")
    placeholder = next(
        line.split(":", 1)[1].strip()
        for line in control.splitlines()
        if line.startswith("Version:")
    )

    for spelling in (DEVELOPMENT_VERSION_PREFIX, placeholder):
        assert "-" not in spelling, (
            f"{spelling!r} uses a hyphen, which makes dpkg and version_key "
            "disagree about its order; spell a pre-release with ~"
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


def test_the_source_records_no_version_at_all():
    """The inversion of the bug this file used to guard against.

    There used to be a hand-edited ``APPLIANCE_VERSION`` literal, and one test
    here compared it against the newest released tag -- because tagging a release
    without bumping it produced a second package carrying the first one's
    version, two different artefacts that ``version_key`` read as equal.

    A version is now the tag, and only the tag: the build is told which one and
    stamps it into the package, the way an EMS image takes its version from the
    tag CI was invoked with and carries it as an OCI label. The forgotten bump is
    not caught any more, it is unrepresentable -- and this is what keeps it that
    way, because re-introducing the literal would look like a tidy-up.
    """

    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "appliance" / "version.py"
    text = source.read_text(encoding="utf-8")

    assert "APPLIANCE_VERSION" not in text, (
        "a version literal is back in the source; the tag is the only version"
    )

    from appliance.version import DEVELOPMENT_VERSION_PREFIX, is_stable

    assert not is_stable(DEVELOPMENT_VERSION_PREFIX), (
        "a build with no tag behind it would be publishable, and the release "
        "chain refuses only what is_stable rejects"
    )


def test_a_development_build_sorts_below_every_release():
    """The tilde is the load-bearing character, and both comparators have to
    agree about it: ``version_key`` gates which package an appliance installs,
    and ``dpkg`` gates whether it will install at all. ``0.0.0+dev`` looks
    equivalent and is not -- version_key scores it equal to 0.0.0 while dpkg
    sorts it above."""

    from appliance.version import development_version, version_key

    development = development_version("abc123def456")

    for release in ("0.0.0", "0.0.1", "0.1.0", "1.0.0", "0.1.0~rc1"):
        assert version_key(development) < version_key(release), (
            f"{development} does not sort below {release}"
        )
