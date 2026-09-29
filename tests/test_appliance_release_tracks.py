# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which track a version is on, and which one an operator is offered first.

The Updates page groups every version into Stable, Unstable and Experimental --
the same three words the Admin console uses -- and offers the newest stable one
as a single button. Both answers come from the backend: a browser that parsed
version strings itself would be a second reader of the same string, and a
second reader is how ``0.1.0~rc1`` once compared equal to ``0.1.0``.
"""

import pytest

from appliance import version
from appliance.releases import ReleaseTarget, parse_release_index
from tests.helpers.appliance import (
    ADMIN_CONTAINER,
    ADMIN_REPOSITORY,
    StaticCatalogue,
    build_test_services,
)
from tests.test_appliance_manager_update import (
    BASE,
    RELEASE_ID,
    build,
    index_payload,
    plan,
)

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]


@pytest.mark.parametrize(
    "text,track",
    [
        ("0.3.8", version.TRACK_STABLE),
        ("v1.1.0", version.TRACK_STABLE),
        ("v1.2.0-rc1", version.TRACK_UNSTABLE),
        ("0.4.0~rc2", version.TRACK_UNSTABLE),
        ("1.0.0-beta.1", version.TRACK_UNSTABLE),
        ("0.3.8~test1", version.TRACK_UNSTABLE),
        ("v1.2.0-dev.7", version.TRACK_UNSTABLE),
        ("0.4.0~rc1+b2", version.TRACK_UNSTABLE),
        ("0.0.0~dev", version.TRACK_EXPERIMENTAL),
        ("0.0.0~dev.abc123", version.TRACK_EXPERIMENTAL),
    ],
)
def test_every_version_lands_on_exactly_one_track(text, track):
    assert version.release_track(text) == track


@pytest.mark.parametrize(
    "text",
    ["", None, "garbage", "latest", "0.3.8+dev1", "0.3.8+local", "0.4.0~", "1.0.0-", "0.3.8.1", "1.2",
     "V0.3.8"],
)
def test_a_version_nobody_can_read_is_never_stable(text):
    """version_key degrades an unparseable string to 0.0.0 -- a release -- and
    drops what it does not read. The button that installs "the latest stable",
    and the image that bakes one in, must not be pointed at either."""

    assert version.release_track(text) == version.TRACK_EXPERIMENTAL
    assert version.is_stable(text) is False


@pytest.mark.parametrize(
    "offered,installed,moving",
    [
        ("0.2.0", "0.1.0", version.DIRECTION_UPGRADE),
        ("0.1.0", "0.2.0", version.DIRECTION_DOWNGRADE),
        ("v1.0.0", "1.0.0", version.DIRECTION_REINSTALL),
        ("1.0.0", "1.0.0-rc1", version.DIRECTION_UPGRADE),
        ("0.2.0", "", version.DIRECTION_UNKNOWN),
        ("0.3.8+dev1", "0.3.8", version.DIRECTION_UNKNOWN),
        ("", "0.3.8", version.DIRECTION_UNKNOWN),
        ("0.3.8.1", "0.3.8", version.DIRECTION_UNKNOWN),
        ("0.3.8+dev1", "0.3.8+dev1", version.DIRECTION_UNKNOWN),
        ("garbage", "garbage", version.DIRECTION_UNKNOWN),
        ("0.0.0~dev.abc1234", "0.0.0~dev.def5678", version.DIRECTION_UNKNOWN),
        ("0.0.0~dev.9999999", "0.0.0~dev.abc1234", version.DIRECTION_UNKNOWN),
        ("0.0.0~dev", "0.0.0~dev", version.DIRECTION_UNKNOWN),
        ("0.0.0~dev.abc1234", "0.0.0~dev.abc1234", version.DIRECTION_UNKNOWN),
        ("0.3.8", "0.0.0~dev.abc1234", version.DIRECTION_UNKNOWN),
    ],
)
def test_the_direction_is_read_by_the_one_comparator(offered, installed, moving):
    assert version.direction(offered=offered, installed=installed) == moving


@pytest.mark.parametrize("installed", ["", None, "dev-feat-x-abc1234-1-1", "latest"])
def test_an_installed_build_nobody_can_read_is_not_compared(installed):
    """Read as 0.0.0, it would make every listed version -- the running one
    included -- an upgrade, and the page would offer one."""

    moving = version.direction(offered="v1.1.0", installed=installed)

    assert moving == version.DIRECTION_UNKNOWN


def test_the_manager_keeps_answering_with_the_same_direction_names():
    from appliance import manager_update

    assert manager_update.direction is version.direction
    assert manager_update.DIRECTION_UPGRADE == version.DIRECTION_UPGRADE
    assert "DIRECTION_UNKNOWN" in manager_update.__all__


# --- EMS Admin ---------------------------------------------------------------


def test_an_admin_release_carries_its_track():
    listed = {item.tag: item.to_dict()["track"] for item in parse_release_index(
        ["v1.1.0", "v1.2.0-rc1", "v1.2.0-dev.3"]
    )}

    assert listed == {
        "v1.1.0": "stable",
        "v1.2.0-rc1": "unstable",
        "v1.2.0-dev.3": "unstable",
    }


@pytest.mark.parametrize(
    "tag",
    ["v1.1.0", "v1.2.0-rc1", "v1.2.0-rc.2", "v1.2.0-beta1", "v1.2.0-dev.3", "v1.2.0-local", "1.0.0"],
)
def test_an_admin_tag_is_grouped_as_the_admin_console_groups_it(tag):
    """Two surfaces, one word for the same tag. The Admin console calls every
    version-shaped prerelease a candidate (Unstable); Experimental is its word
    for development builds, which are not version-shaped and never listed here."""

    from admin.releases import _is_release_candidate

    appliance = parse_release_index([tag])[0].track
    admin = "unstable" if _is_release_candidate(tag) else "stable"

    assert appliance == admin


def test_an_index_that_flags_a_plain_tag_as_prerelease_is_not_shown_as_stable():
    target = ReleaseTarget(tag="v1.3.0", channel="exact", prerelease=True)

    assert target.to_dict()["track"] == version.TRACK_UNSTABLE


def _admin_services(tmp_path, tags):
    services = build_test_services(
        tmp_path, catalogue=StaticCatalogue(tags, allow_prerelease=True)
    )
    host = services.host
    host.write_deployment(tag="v1.0.0")
    for tag in ("v1.0.0", "v1.1.0"):
        host.publish_image(tag)
    host.pull_local(f"{ADMIN_REPOSITORY}:v1.0.0")
    host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:v1.0.0")
    return services


def test_the_admin_list_names_the_latest_stable_and_which_way_it_moves(tmp_path):
    services = _admin_services(tmp_path, ["v1.2.0-rc1", "v1.1.0", "v1.0.0"])

    listing = services.admin.releases()

    assert listing["latest_stable"]["tag"] == "v1.1.0"
    assert listing["latest_stable"]["direction"] == "upgrade"
    directions = {item["tag"]: item["direction"] for item in listing["available"]}
    assert directions == {"v1.2.0-rc1": "upgrade", "v1.1.0": "upgrade", "v1.0.0": "reinstall"}


def test_an_admin_without_a_readable_version_is_offered_no_upgrade(tmp_path):
    services = build_test_services(
        tmp_path, catalogue=StaticCatalogue(["v1.1.0"], allow_prerelease=True)
    )

    listing = services.admin.releases()

    assert listing["available"][0]["direction"] == "unknown"
    assert listing["latest_stable"]["direction"] == "unknown"


def test_a_candidate_newer_than_every_release_is_not_the_latest_stable(tmp_path):
    services = _admin_services(tmp_path, ["v2.0.0-rc1"])

    assert services.admin.releases()["latest_stable"] is None


# --- Appliance Manager -------------------------------------------------------


def _entry(release_id, release_version):
    return {
        "release_id": release_id,
        "manifest_url": f"{BASE}/{release_id}.manifest.json",
        "signature_url": f"{BASE}/{release_id}.manifest.json.asc",
        "archive_url": f"{BASE}/{release_id}.deb",
        "release_version": release_version,
    }


def test_the_manager_list_carries_tracks_and_names_the_latest_stable(tmp_path):
    index = index_payload(extra=[
        _entry("ems-appliance-manager-0.0.0-dev.abc1234-arm64", "0.0.0~dev.abc1234"),
        _entry("ems-appliance-manager-0.1.5-arm64", "0.1.5"),
    ])
    _, service = build(tmp_path, index=index, installed_version="0.1.0")

    listing = service.sources()

    tracks = {entry["release_id"]: entry["track"] for entry in listing["releases"]}
    assert tracks == {
        "ems-appliance-manager-0.0.0-dev.abc1234-arm64": "experimental",
        "ems-appliance-manager-0.2.0-arm64": "stable",
        "ems-appliance-manager-0.1.5-arm64": "stable",
    }
    assert listing["latest_stable"]["release_id"] == "ems-appliance-manager-0.2.0-arm64"
    assert listing["latest_stable"] in listing["releases"]


def test_an_index_that_says_nothing_about_a_version_offers_no_latest_stable(tmp_path):
    entry = _entry("ems-appliance-manager-0.9.0-arm64", "")
    _, service = build(tmp_path, index={"format_version": 1, "releases": [entry]})

    listing = service.sources()

    assert listing["releases"][0]["track"] == version.TRACK_EXPERIMENTAL
    assert listing["latest_stable"] is None


def test_an_unconfigured_index_offers_no_latest_stable(tmp_path):
    _, service = build(tmp_path, configured=False)

    assert service.sources()["latest_stable"] is None


def test_a_manager_whose_own_version_is_unreadable_offers_no_upgrade(tmp_path):
    _, service = build(tmp_path, installed_version="")

    listing = service.sources()

    assert {entry["direction"] for entry in listing["releases"]} == {"unknown"}


def test_a_signed_package_that_is_not_the_version_the_index_named_is_blocked(tmp_path):
    """The page offers "Update to <the index's version>"; confirming it must not
    install a different signed version."""

    index = index_payload()
    index["releases"][0]["release_version"] = "0.9.0"
    services, _ = build(tmp_path, index=index)

    planned = plan(services, "manager.plan_update", release_id=RELEASE_ID)

    codes = [blocker["code"] for blocker in planned["plan"]["blockers"]]
    assert "release_index_mismatch" in codes


def test_a_signed_package_the_index_described_truthfully_is_not_blocked(tmp_path):
    services, _ = build(tmp_path)

    planned = plan(services, "manager.plan_update", release_id=RELEASE_ID)

    assert "release_index_mismatch" not in [b["code"] for b in planned["plan"]["blockers"]]


def test_the_plan_and_the_list_agree_on_an_unreadable_installed_version(tmp_path):
    services, service = build(tmp_path, installed_version="")

    planned = plan(services, "manager.plan_update", release_id=RELEASE_ID)

    assert planned["plan"]["direction"] == version.DIRECTION_UNKNOWN
    assert service.sources()["releases"][0]["direction"] == version.DIRECTION_UNKNOWN


def test_the_latest_stable_channel_and_the_summary_name_the_same_release():
    catalogue = StaticCatalogue(["v1.2.0-rc1", "v1.1.0", "v1.0.0"], allow_prerelease=True)
    listed = [item.to_dict() for item in catalogue.available()]

    summary = version.newest_stable(
        listed, version_of=lambda item: item["tag"], stable=lambda item: item["track"] == "stable"
    )

    assert catalogue.latest_stable().tag == summary["tag"] == "v1.1.0"


def test_the_newest_stable_is_the_highest_version_not_the_first_listed():
    """The image build, the package fetch and the Updates page ask the same
    question; list order is the index's claim, the version is the answer."""

    picked = version.newest_stable(["0.2.0", "0.3.0~rc1", "0.10.0", "0.9.0"], version_of=str)

    assert picked == "0.10.0"
    assert version.latest_stable(["appliance-manager-v0.2.0", "appliance-manager-v0.10.0"]) == "0.10.0"


def test_a_plan_of_unknown_direction_says_so(tmp_path):
    services, _ = build(tmp_path, installed_version="")

    planned = plan(services, "manager.plan_update", release_id=RELEASE_ID)

    assert "running Appliance Manager's version cannot be compared" in planned["plan"]["warning"]


def test_the_index_cuts_a_long_claim_so_it_can_never_match_a_long_signed_version():
    from appliance import release_fetch

    long_version = "0.2.0~" + "x" * 80
    claimed = release_fetch._description({"release_version": long_version})["release_version"]

    assert claimed == long_version[: release_fetch.MAX_DESCRIPTION]


@pytest.mark.parametrize("tag", ["v1.1.0", "v1.2.0-rc1", "v1.2.0-dev.3", "v1.2.0-test1"])
def test_admin_and_manager_versions_land_on_the_same_track(tag):
    """One page, one rule: the two lists on Updates may not group the same
    kind of build apart."""

    admin = parse_release_index([tag])[0].track

    assert admin == version.release_track(tag)


def test_an_index_claim_spelled_with_a_v_is_the_signed_version(tmp_path):
    index = index_payload()
    index["releases"][0]["release_version"] = "v0.2.0"
    services, _ = build(tmp_path, index=index)

    planned = plan(services, "manager.plan_update", release_id=RELEASE_ID)

    assert "release_index_mismatch" not in [b["code"] for b in planned["plan"]["blockers"]]


@pytest.mark.parametrize(
    "claimed,signed,matches",
    [
        ("0.2.0", "0.2.0", True),
        ("v0.2.0", "0.2.0", True),
        ("0.9.0", "0.2.0", False),
        ("0.2.0~" + "x" * 58, "0.2.0~" + "x" * 80, False),
    ],
)
def test_one_rule_says_whether_an_index_claim_is_the_signed_version(claimed, signed, matches):
    from appliance import release_fetch

    assert release_fetch.claim_matches(claimed, signed) is matches


def test_the_image_build_checks_the_claim_with_the_same_rule():
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "appliance-fetch-manager-package.py"

    assert "release_fetch.claim_matches(" in script.read_text(encoding="utf-8")


def test_the_image_build_filters_a_wanted_version_with_the_same_rule():
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "appliance-fetch-manager-package.py"
    text = script.read_text(encoding="utf-8")

    assert "release_fetch.claim_matches(described_version(entry), wanted)" in text


def test_a_development_build_running_is_blamed_on_the_running_side(tmp_path):
    services, _ = build(tmp_path, installed_version="0.0.0~dev.abc1234")

    planned = plan(services, "manager.plan_update", release_id=RELEASE_ID)

    assert planned["plan"]["direction"] == version.DIRECTION_UNKNOWN
    assert "running Appliance Manager's version" in planned["plan"]["warning"]


def test_the_release_chain_names_an_unreadable_version_as_unreadable():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "appliance-manager-release.yml").read_text(
        encoding="utf-8"
    )
    script = (root / "scripts" / "appliance-fetch-manager-package.py").read_text(encoding="utf-8")

    assert "is not a version this chain can read" in workflow
    assert '"not a readable version"' in script


def test_a_debian_revision_offered_to_the_manager_is_not_compared(tmp_path):
    """dpkg reads 0.3.8-1 above 0.3.8, version_key below; the list must not
    call it an older candidate."""

    index = index_payload(extra=[_entry("ems-appliance-manager-0.3.8-1-arm64", "0.3.8-1")])
    _, service = build(tmp_path, index=index, installed_version="0.3.8")

    entry = [e for e in service.sources()["releases"] if e["described"]["release_version"] == "0.3.8-1"][0]

    assert entry["direction"] == version.DIRECTION_UNKNOWN
    assert entry["track"] == version.TRACK_EXPERIMENTAL


def test_an_uppercase_prefix_is_not_offered_as_the_latest_stable(tmp_path):
    """normalize_version strips only a lowercase v, so a V0.3.8 claim could never
    pass the plan's index check; it is not offered as one click."""

    index = {"format_version": 1, "releases": [_entry("ems-appliance-manager-0.3.8-arm64", "V0.3.8")]}
    _, service = build(tmp_path, index=index, installed_version="0.1.0")

    assert service.sources()["latest_stable"] is None


@pytest.mark.parametrize(
    "offered,installed,moving",
    [
        ("0.3.9~rc1+b2", "0.3.9~rc1.b2", version.DIRECTION_UNKNOWN),
        ("v0.8.0-RC1", "0.8.0-rc1", version.DIRECTION_REINSTALL),
    ],
)
def test_equal_scores_are_not_a_reinstall_unless_the_version_is_the_same(offered, installed, moving):
    assert version.direction(offered=offered, installed=installed) == moving


@pytest.mark.parametrize(
    "offered,installed",
    [("0.3.9~rc-1", "0.3.8"), ("0.3.9", "0.3.8-1"), ("0.3.8-1", "0.3.8")],
)
def test_a_package_revision_is_not_ordered_anywhere(offered, installed):
    """The listing, the plan and newest_stable read the same rule."""

    assert version.direction(offered=offered, installed=installed, package=True) == "unknown"


def test_a_package_revision_is_experimental_wherever_it_is_grouped():
    assert version.release_track("0.3.9~rc-1", package=True) == version.TRACK_EXPERIMENTAL
    assert version.release_track("0.3.9~rc1", package=True) == version.TRACK_UNSTABLE


def test_the_manager_plan_does_not_order_a_revision_it_is_running(tmp_path):
    services, _ = build(tmp_path, installed_version="0.1.0-1")

    planned = plan(services, "manager.plan_update", release_id=RELEASE_ID)

    assert planned["plan"]["direction"] == version.DIRECTION_UNKNOWN


def test_the_release_chain_names_a_hyphen_as_the_problem():
    from pathlib import Path

    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "appliance-manager-release.yml"
    ).read_text(encoding="utf-8")

    assert workflow.index('if "-" in wanted:') < workflow.index("if not is_readable(wanted):")


def test_a_package_version_is_the_same_only_when_spelled_the_same():
    assert version.direction(offered="0.3.9~RC1", installed="0.3.9~rc1", package=True) != "reinstall"
    assert version.direction(offered="v0.8.0-RC1", installed="0.8.0-rc1") == "reinstall"


def test_a_tie_between_two_readable_spellings_is_warned_as_a_tie():
    from appliance.manager_update import DIRECTION_UNKNOWN, ManagerUpdateService

    text = ManagerUpdateService._warning(DIRECTION_UNKNOWN, True, "tie")
    assert version.compare(offered="0.3.9~rc1+b2", installed="0.3.9~rc1.b2", package=True) == (
        "unknown", "tie"
    )

    assert "score the same" in text
    assert "development build" not in text


@pytest.mark.parametrize(
    "bootstrap,daemon,expected",
    [
        (True, "running", "Open Admin and install it"),
        (False, "running", "Open Admin and repair it"),
        (False, None, "Open Admin and repair it"),
        (False, "stopped", None),
        (True, "unavailable", None),
    ],
)
def test_a_missing_admin_is_sent_where_that_state_is_acted_on(tmp_path, bootstrap, daemon, expected):
    """A stopped daemon hides every container: docker_not_running says so, and
    "no Admin installed" would be false."""

    services = build_test_services(tmp_path)
    admin = {"status": "ok", "installed": False, "bootstrap_required": bootstrap}
    if daemon:
        admin["docker"] = {"state": daemon}

    warnings = services.status._health({"admin": admin})["warnings"]
    found = [item for item in warnings if item["code"] == "admin_not_installed"]

    if expected is None:
        assert found == []
    else:
        assert found[0]["next_step"].startswith(expected)


@pytest.mark.parametrize(
    "offered,installed,side",
    [("0.2.0", "", "running"), ("", "0.1.0", "offered"), ("0.3.9~rc1+b2", "0.3.9~rc1.b2", "tie")],
)
def test_the_reason_for_an_unknown_direction_comes_from_the_comparator(offered, installed, side):
    assert version.direction(offered=offered, installed=installed, package=True) == "unknown"
    assert version.compare(offered=offered, installed=installed, package=True)[1] == side


def test_one_helper_says_whether_two_package_versions_are_the_same():
    from appliance import release_fetch

    assert release_fetch.claim_matches("v0.2.0", "0.2.0") is version.same_version(
        "v0.2.0", "0.2.0", package=True
    )
    assert "same_version(" in __import__("inspect").getsource(release_fetch.claim_matches)
