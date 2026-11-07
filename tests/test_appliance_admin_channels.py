# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Manager offers the EMS Admin channels the Admin console offers.

The Manager installed only versioned tags, so the rolling ``latest`` an Admin
console can move itself to could neither be offered nor restored from the
Manager, and its tracks were named Unstable and Experimental where the console
says RC and Development. The pull stays fail-closed: what changes is which
tags may be asked for, not how a pulled image is proven.
"""

import pytest

from appliance import validation
from appliance.validation import ValidationError
from appliance.version import (
    TRACK_DEVELOPMENT,
    TRACK_LATEST,
    TRACK_RC,
    TRACK_STABLE,
    release_track,
)

pytestmark = [pytest.mark.unit, pytest.mark.appliance]


@pytest.mark.parametrize("tag", ["latest", "dev-feature-x-1a2b3c4-17-1", "dev_branch-0123abcd-3-2"])
def test_rolling_and_development_tags_may_be_asked_for(tag):
    assert validation.validate_release_tag(tag) == tag
    assert validation.is_prerelease_tag(tag) is True


@pytest.mark.parametrize(
    "tag", ["main", "local", "local-x", "dev-", "dev-feature-x", "Latest", "latest2", "dev x"]
)
def test_other_names_stay_refused(tag):
    with pytest.raises(ValidationError):
        validation.validate_release_tag(tag)


def test_the_host_ceiling_still_refuses_them_when_prereleases_are_off():
    for tag in ("latest", "dev-x-1a2b3c4-1-1", "v1.2.0-rc1"):
        with pytest.raises(ValidationError) as excinfo:
            validation.validate_installable_tag(tag, allow_prerelease=False)
        assert excinfo.value.code == "prerelease_not_allowed"


@pytest.mark.parametrize(
    "tag, track",
    [
        ("v1.2.0", TRACK_STABLE),
        ("v1.2.0-rc1", TRACK_RC),
        ("latest", TRACK_LATEST),
        ("dev-feature-1a2b3c4", TRACK_DEVELOPMENT),
    ],
)
def test_the_tracks_are_the_admin_consoles_channels(tag, track):
    assert release_track(tag) == track


def test_the_track_names_are_the_ones_the_admin_console_uses():
    from admin import system_build

    admin = {system_build.CHANNEL_STABLE, system_build.CHANNEL_RC, system_build.CHANNEL_LATEST, system_build.CHANNEL_DEV}

    assert {TRACK_STABLE, TRACK_RC, TRACK_LATEST, TRACK_DEVELOPMENT} == admin


def test_a_package_prerelease_and_development_build_use_the_same_words():
    assert release_track("0.5.0~rc1", package=True) == TRACK_RC
    assert release_track("0.0.0~dev.1a2b3c", package=True) == TRACK_DEVELOPMENT
    assert release_track("0.5.0", package=True) == TRACK_STABLE


def test_a_latest_image_is_proven_by_its_labels_like_any_release():
    labels = {
        "org.opencontainers.image.source": "https://github.com/x/y",
        "org.opencontainers.image.version": "latest",
        "org.opencontainers.image.revision": "a" * 40,
        "org.opencontainers.image.created": "2026-11-01T00:00:00Z",
    }
    labels = {name: labels.get(name, "x") for name in validation.REQUIRED_OCI_LABELS}
    labels["org.opencontainers.image.version"] = "latest"
    labels["org.opencontainers.image.source"] = "https://github.com/x/y"

    assert validation.validate_oci_labels(labels, requested_tag="latest", expected_source="https://github.com/x/y")
    labels["org.opencontainers.image.version"] = "v1.2.0"
    with pytest.raises(ValidationError):
        validation.validate_oci_labels(labels, requested_tag="latest", expected_source="https://github.com/x/y")


def test_an_admin_install_plan_names_its_track(tmp_path):
    from appliance.agent import AgentHandlers
    from tests.test_appliance_admin_lifecycle import healthy_appliance

    services = healthy_appliance(tmp_path)
    services.host.publish_image("v1.1.0")
    plan = AgentHandlers(services, executor=lambda target: target()).dispatch(
        {"operation": "admin.plan_install", "channel": "exact", "tag": "v1.1.0"}
    )["plan"]

    assert plan["track"] == TRACK_STABLE


def test_a_rolling_admin_is_planned_by_its_digest(tmp_path):
    """The Admin console moves itself to latest; the Manager can now install
    and restore that, pinned by the digest it pulled."""

    import dataclasses

    from appliance.agent import AgentHandlers
    from tests.test_appliance_admin_lifecycle import healthy_appliance

    services = healthy_appliance(tmp_path)
    services.host.publish_image("latest", version="latest")
    images = dataclasses.replace(services.config.images, allow_prerelease=True)
    services.config = dataclasses.replace(services.config, images=images)
    services.admin.config = dataclasses.replace(services.admin.config, images=images)
    plan = AgentHandlers(services, executor=lambda target: target()).dispatch(
        {"operation": "admin.plan_install", "channel": "exact", "tag": "latest"}
    )["plan"]

    assert plan["track"] == TRACK_LATEST
    assert plan["target_reference"].endswith(plan["target_digest"])


@pytest.mark.parametrize(
    "tag",
    [
        "dev-feature-x-1a2b3c4-17-1",
        "dev-feature-x",
        "dev-x-ABCDEF0-1-1",
        "dev-x-1a2b3c4-0-1",
        "dev_y-0123abcd-3-2",
        "dev-fix-appliance-manager-review-0123456789-abcdef0-19123456789-1",
        "dev-" + "a" * 80 + "-0123456789-abcdef0-19123456789-1",
    ],
)
def test_a_development_tag_is_immutable_by_the_admin_consoles_rule(tag):
    from admin import system_build

    accepted = bool(validation.DEVELOPMENT_TAG_RE.match(tag))

    assert accepted == (system_build.is_development_build_tag(tag) and system_build.is_immutable_dev_tag(tag))


# --- one grammar and one order with the Admin console --------------------------

PUBLISHED_SHAPES = [
    "v0.8.0", "v0.8.1-rc1", "v0.8.1-rc2", "v0.8.1-rc6", "v0.8.1", "v0.9.0-RC", "v0.9.0-rc1",
    "v0.9.0-RC2", "v0.9.0", "v1.0.0-rc-1", "v1.0.0-beta.2", "v1.0.0",
]


def test_the_tags_the_admin_console_reads_are_the_ones_this_appliance_reads():
    """The console accepts -rc-1; the Manager dropped it from its list."""

    from admin.releases import _version

    for tag in PUBLISHED_SHAPES:
        assert bool(_version(tag)) == bool(validation.RELEASE_TAG_RE.match(tag)), tag


def test_the_admin_console_and_this_appliance_order_published_tags_alike():
    """Case did not matter to the console and did here: -RC1 sorted below -rc1."""

    from admin.releases import _version

    from appliance.version import version_key

    assert sorted(PUBLISHED_SHAPES, key=version_key) == sorted(PUBLISHED_SHAPES, key=_version)


def test_the_one_known_difference_is_the_tenth_candidate():
    """The console compares rc10 and rc2 as text; here the number counts, so
    the tenth candidate is never read as older than the second."""

    from appliance.version import version_key

    assert version_key("v1.0.0-rc10") > version_key("v1.0.0-rc2")


@pytest.mark.parametrize("channel", ["current", "previous_known_good"])
def test_a_rolling_tag_is_never_reinstalled_or_returned_to_by_its_name(channel):
    """latest moves: pulling it again for "current" or "previous known-good"
    installs today's build, possibly the very one being escaped."""

    from appliance.releases import ReleaseResolutionError, resolve_channel

    with pytest.raises(ReleaseResolutionError) as excinfo:
        resolve_channel(
            channel,
            catalogue=None,
            current_tag="latest",
            previous_known_good={"admin_version": "latest", "admin_digest": "sha256:" + "a" * 64},
        )

    assert excinfo.value.code == "rolling_tag_not_by_name"


def test_a_development_tag_as_long_as_the_publishing_workflow_makes_it_is_valid():
    """docker-feature-publish builds dev-<ref up to 80>-<ref hash>-<sha>-<run>-<attempt>
    and guards it only at Docker's 128 characters."""

    tag = "dev-" + "a" * 80 + "-0123456789-abcdef0-19123456789-1"

    assert len(tag) <= 128
    assert validation.validate_release_tag(tag) == tag
