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
