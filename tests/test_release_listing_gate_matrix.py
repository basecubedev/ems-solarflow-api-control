# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the catalogue offers and what the upgrade gate allows must be one answer.

Every release-selection defect reported from a live console has been a
disagreement between these two. The listing placed a rolling build in the wrong
line and refused releases the gate called upgrades; the catalogue offered every
development build while the gate refused the older ones. Both were found one
scenario at a time, by an operator.

So both are asked the same question about the same builds here. The listing may
be more permissive -- a target it cannot inspect stays selectable on purpose,
and preparing it verifies before anything changes -- but it may never be
*stricter*: a row the console refuses to offer is a build the operator cannot
reach at all, and no gate verdict can undo that.
"""

import json
from urllib.parse import urlparse

import pytest

from admin.image_identity import ALREADY_CURRENT, BLOCKING_UPGRADE_STATES
from admin.releases import ReleaseManager

pytestmark = [
    pytest.mark.admin,
    pytest.mark.system_build,
    pytest.mark.contract,
    pytest.mark.simulation,
]

REPOSITORY = "ghcr.io/basecubedev/ems-solarflow-api-control"
CATALOGUE = ("v0.9.0", "v0.8.7", "v0.8.4", "v0.7.0")
PUBLISHED_AT = {
    "v0.9.0": "2026-09-16T21:00:00Z",
    "v0.8.7": "2026-09-17T09:00:00Z",
    "v0.8.4": "2026-09-13T00:00:00Z",
    "v0.7.0": "2026-07-07T00:00:00Z",
}
# Serials from the release workflow's counter; the order matches publication.
SERIALS = {"v0.7.0": 900, "v0.8.4": 1300, "v0.8.7": 1450, "v0.9.0": 1400}


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self, size=None):
        return self._payload if size is None else self._payload[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _opener():
    releases = [
        {
            "tag_name": tag,
            "name": tag,
            "published_at": PUBLISHED_AT[tag],
            "prerelease": False,
            "draft": False,
            "zipball_url": f"https://example.test/{tag}.zip",
        }
        for tag in CATALOGUE
    ]
    tree = {
        "tree": [
            {"path": path, "type": "blob"}
            for path in (
                "config.template.json",
                "docker-compose.example.yml",
                "install-docker.sh",
                "install-docker.ps1",
                "deploy/docker/compose.influxdb.yml",
            )
        ]
    }

    def open_url(request, timeout=None):
        url = request.full_url
        if "/git/trees/" in url:
            return _Response(json.dumps(tree).encode())
        if urlparse(url).hostname == "api.github.com":
            return _Response(json.dumps(releases).encode())
        return _Response(b"")

    return open_url


def _labels(*, channel, release_tag, serial, contains):
    labels = {
        "org.opencontainers.image.revision": "d" * 40,
        "org.opencontainers.image.version": release_tag,
        "de.basecubedev.ems.channel": channel,
        "de.basecubedev.ems.release_tag": release_tag,
        "de.basecubedev.ems.build_id": f"{release_tag}-ddddddd",
    }
    if serial is not None:
        labels["de.basecubedev.ems.build_serial"] = str(serial)
    if contains is not None:
        labels["de.basecubedev.ems.contains_release"] = contains
    return labels


class _Docker:
    def __init__(self, running_ref, images):
        self._running_ref = running_ref
        self._images = images

    def inspect_container(self, _name):
        return {
            "container_name": "ems-solarflow-api-control",
            "image": self._running_ref,
            "status": "running",
        }

    def inspect_image(self, ref):
        found = self._images.get(ref)
        return dict(found) if found else None


# Every running build a console can be in when it opens the upgrade page, as
# (channel, the release its image declares, its own release tag).
RUNNING_BUILDS = {
    "stable v0.8.4": ("stable", "v0.8.4", "v0.8.4", 1300),
    "stable v0.9.0, the newest line": ("stable", "v0.9.0", "v0.9.0", 1400),
    "rolling latest built past v0.8.4": ("latest", "v0.8.4", "latest", 1200),
    "rolling latest built past v0.9.0": ("latest", "v0.9.0", "latest", 1500),
    "development build off v0.8.4": ("development", "v0.8.4", "dev-x-ddddddd-99-1", 45),
    "development build off v0.7.0": ("development", "v0.7.0", "dev-y-ddddddd-98-1", 44),
}


def _manager(tmp_path, scenario):
    channel, contains, release_tag, serial = RUNNING_BUILDS[scenario]
    running_ref = f"{REPOSITORY}:running"
    images = {
        running_ref: {
            "digest": "sha256:" + "e" * 64,
            "labels": _labels(
                channel=channel,
                release_tag=release_tag,
                serial=serial,
                contains=contains,
            ),
        }
    }
    for index, tag in enumerate(CATALOGUE):
        images[f"{REPOSITORY}:{tag}"] = {
            "digest": "sha256:" + str(index + 1) * 64,
            "labels": _labels(
                channel="stable", release_tag=tag, serial=SERIALS[tag], contains=tag
            ),
        }
    project = tmp_path / "project"
    project.mkdir()
    (project / "docker-compose.yml").write_text(
        f"services:\n  ems:\n    image: {running_ref}\n"
        "    container_name: ems-solarflow-api-control\n",
        encoding="utf-8",
    )
    return ReleaseManager(
        data_dir=tmp_path / "data",
        project_dir=project,
        urlopen=_opener(),
        docker=_Docker(running_ref, images),
    )


@pytest.mark.parametrize("scenario", sorted(RUNNING_BUILDS))
def test_the_catalogue_never_refuses_what_the_gate_allows(tmp_path, scenario):
    """The listing may be more permissive than the gate, never stricter.

    A build the console does not offer cannot be selected at all, whatever the
    gate would have said about it -- which is how an installation running
    `latest` came to have the newest patch of its own line, an upgrade by every
    measure the gate applies, shown as a refused downgrade.
    """

    manager = _manager(tmp_path, scenario)
    rows = {item["tag"]: item for item in manager.list_releases()["releases"]}

    for tag in CATALOGUE:
        row = rows[tag]
        verdict = manager.verify_upgrade_target(tag)
        if verdict.state in BLOCKING_UPGRADE_STATES:
            continue
        if verdict.state == ALREADY_CURRENT:
            # Not a move at all: refusing to offer it is the listing's job.
            assert row["selectable"] is False
            continue
        assert row["selectable"] is True, (
            f"{scenario}: the gate allows {tag} ({verdict.state}) but the "
            f"catalogue refuses it ({row['upgrade_state']}: {row['reason']})"
        )


@pytest.mark.parametrize("scenario", sorted(RUNNING_BUILDS))
def test_the_catalogue_and_the_gate_name_the_same_verdict(tmp_path, scenario):
    """Where both can see the images, they must not merely agree on the answer.

    Every image here is inspectable, so neither side has to fall back on an
    unverifiable target. With nothing left unknown, the two must report the
    same state for the same build, not just compatible ones.
    """

    manager = _manager(tmp_path, scenario)
    rows = {item["tag"]: item for item in manager.list_releases()["releases"]}

    for tag in CATALOGUE:
        listed = rows[tag]["upgrade_state"]
        gate = manager.verify_upgrade_target(tag).state
        assert listed == gate, (
            f"{scenario}: the catalogue says {listed} for {tag}, the gate says {gate}"
        )


@pytest.mark.parametrize("scenario", sorted(RUNNING_BUILDS))
def test_a_build_can_always_reach_its_own_declared_release(tmp_path, scenario):
    """The release a build was built past is inside its line by definition.

    Only asked of a build with no version of its own: a release build *is* the
    release it declares, and offering an installation itself is not a move.
    """

    channel, contains, _release_tag, _serial = RUNNING_BUILDS[scenario]
    if channel not in ("latest", "development"):
        pytest.skip("a release build is the release it declares")
    manager = _manager(tmp_path, scenario)
    rows = {item["tag"]: item for item in manager.list_releases()["releases"]}

    assert rows[contains]["selectable"] is True, (
        f"{scenario} cannot reach {contains}, the release it declares"
    )


@pytest.mark.parametrize("scenario", sorted(RUNNING_BUILDS))
def test_the_rolling_channel_is_reachable_from_everywhere(tmp_path, scenario):
    """`latest` is a channel, not a version: no build may be locked out of it."""

    manager = _manager(tmp_path, scenario)
    rows = {item["tag"]: item for item in manager.list_releases()["releases"]}

    assert rows["latest"]["selectable"] is True
