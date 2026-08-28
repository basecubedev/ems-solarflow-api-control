# SPDX-License-Identifier: AGPL-3.0-or-later
"""Release discovery and setup-resource cache tests."""

import io
import json
import os
import urllib.error
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from admin.development_catalogue import (
    development_catalogue_source,
    load_development_builds,
)
from admin.releases import (
    REPO,
    ReleaseError,
    ReleaseManager,
    legacy_release_resource_ref,
    legacy_release_resource_url,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.system_build,
    pytest.mark.integration,
    pytest.mark.simulation,
]

LEGACY_REVISION = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
OTHER_LEGACY_REVISION = "b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _archive(extra=None):
    content = {
        "repo/config.template.json": b'{"devices": []}\n',
        "repo/docker-compose.example.yml": b"services: {}\n",
        "repo/install-docker.sh": b"#!/bin/sh\n",
        "repo/install-docker.ps1": b"Write-Host install\n",
        "repo/deploy/docker/compose.influxdb.yml": b"services: {}\n",
        "repo/README.md": b"must not be extracted\n",
    }
    content.update(extra or {})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as handle:
        for name, value in content.items():
            handle.writestr(name, value)
    return output.getvalue()


def _github_payload():
    return [
        {
            "tag_name": "v0.6.1-rc1",
            "name": "0.6.1 release candidate",
            "published_at": "2026-06-20T10:00:00Z",
            "prerelease": False,
            "draft": False,
            "zipball_url": "https://example.test/v0.6.1-rc1.zip",
        },
        {
            "tag_name": "v0.6.1",
            "name": "v0.6.1",
            "published_at": "2026-06-21T10:00:00Z",
            "prerelease": False,
            "draft": False,
            "zipball_url": "https://example.test/v0.6.1.zip",
        },
        {
            "tag_name": "v0.6.0",
            "name": "v0.6.0",
            "published_at": "2026-06-01T10:00:00Z",
            "prerelease": False,
            "draft": False,
            "zipball_url": "https://example.test/v0.6.0.zip",
        },
        {
            "tag_name": "v0.5.9",
            "name": "v0.5.9",
            "published_at": "2026-05-01T10:00:00Z",
            "prerelease": False,
            "draft": False,
            "zipball_url": "https://example.test/v0.5.9.zip",
        },
    ]


def _is_github_api_url(url):
    return urlparse(url).hostname == "api.github.com"


def _opener(archive=None, payload=None):
    def open_url(request, timeout=None):
        url = request.full_url
        if "/git/trees/" in url:
            paths = list(
                {
                    "config.template.json",
                    "docker-compose.example.yml",
                    "install-docker.sh",
                    "install-docker.ps1",
                    "deploy/docker/compose.influxdb.yml",
                }
            )
            return _Response(
                json.dumps(
                    {"tree": [{"path": path, "type": "blob"} for path in paths]}
                ).encode()
            )
        if _is_github_api_url(url):
            return _Response(
                json.dumps(_github_payload() if payload is None else payload).encode()
            )
        return _Response(archive or _archive())

    return open_url


def _write_cached(data_dir, tag="v0.6.0"):
    root = data_dir / "releases" / tag
    (root / "deploy" / "docker").mkdir(parents=True)
    (root / "config.template.json").write_text('{"devices": []}', encoding="utf-8")
    (root / "docker-compose.example.yml").write_text("services: {}", encoding="utf-8")
    (root / "install-docker.sh").write_text("#!/bin/sh", encoding="utf-8")
    (root / "install-docker.ps1").write_text("Write-Host install", encoding="utf-8")
    (root / "deploy" / "docker" / "compose.yml").write_text(
        "services: {}", encoding="utf-8"
    )
    manifest = {
        "tag": tag,
        "source": "github",
        "downloaded_at": "2026-06-01T10:00:00Z",
        "repo": REPO,
        "resources": {},
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_release_list_maps_stable_and_prerelease_metadata(tmp_path):
    manager = ReleaseManager(data_dir=tmp_path, urlopen=_opener())
    result = manager.list_releases()

    assert result["latest"] == "latest"
    assert result["latest_stable"] == "v0.6.1"
    assert result["default_release"] == "v0.6.1"
    # ``latest`` sorts first; pre-v0.6.0 releases (v0.5.9) are hidden entirely.
    assert [item["tag"] for item in result["releases"]] == [
        "latest",
        "v0.6.1",
        "v0.6.0",
        "v0.6.1-rc1",
    ]
    assert result["releases"][0]["tag"] == "latest"
    by_tag = {item["tag"]: item for item in result["releases"]}
    assert by_tag["v0.6.1"]["stable"] is True
    assert by_tag["v0.6.1-rc1"]["prerelease"] is True
    assert by_tag["v0.6.1-rc1"]["channel"] == "rc"
    assert by_tag["v0.6.1-rc1"]["admin_supported"] is True
    assert by_tag["latest"]["stable"] is False
    assert by_tag["latest"]["selectable"] is True
    assert "v0.5.9" not in by_tag


def test_every_channel_group_is_ordered_newest_first(tmp_path):
    """Prereleases must order like versions, not like strings.

    The sort key dropped the prerelease part, so the RC number fell through to
    the tag-string tiebreaker and ran the opposite way from the versions above
    it: ``v0.8.0`` before ``v0.7.0``, but ``v0.8.0-RC1`` before ``v0.8.0-RC2``.
    """

    payload = [
        {
            "tag_name": tag,
            "name": tag,
            "published_at": "2026-07-01T10:00:00Z",
            "prerelease": False,
            "draft": False,
            "zipball_url": f"https://example.test/{tag}.zip",
        }
        for tag in ("v0.7.0-RC1", "v0.8.0-RC2", "v0.8.0-RC1", "v0.7.0", "v0.8.0")
    ]
    manager = ReleaseManager(data_dir=tmp_path, urlopen=_opener(payload=payload))

    tags = [item["tag"] for item in manager.list_releases()["releases"]]

    assert tags == [
        "latest",
        "v0.8.0",
        "v0.7.0",
        "v0.8.0-RC2",
        "v0.8.0-RC1",
        "v0.7.0-RC1",
    ]


def test_release_default_prefers_stable_over_rolling_latest(tmp_path):
    # When a versioned stable release exists it is the default, never the rolling
    # ``latest`` channel — even though ``latest`` sorts first in the catalogue.
    manager = ReleaseManager(data_dir=tmp_path, urlopen=_opener())
    result = manager.list_releases()

    assert result["releases"][0]["tag"] == "latest"
    assert result["default_release"] == "v0.6.1"
    assert result["default_release"] != "latest"


def test_release_default_falls_back_to_latest_without_a_stable_release(tmp_path):
    # No final versioned release is published (only a release candidate). The
    # rolling ``latest`` channel is then the fallback default.
    payload = [
        {
            "tag_name": "v0.6.1-rc1",
            "name": "0.6.1 release candidate",
            "published_at": "2026-06-20T10:00:00Z",
            "prerelease": True,
            "draft": False,
            "zipball_url": "https://example.test/v0.6.1-rc1.zip",
        }
    ]
    manager = ReleaseManager(data_dir=tmp_path, urlopen=_opener(payload=payload))
    result = manager.list_releases()

    by_tag = {item["tag"]: item for item in result["releases"]}
    assert by_tag["latest"]["channel"] == "latest"
    assert by_tag["latest"]["selectable"] is True
    assert result["latest_stable"] is None
    assert result["default_release"] == "latest"


def test_offline_release_list_still_returns_cached_release(tmp_path):
    _write_cached(tmp_path)

    def offline(_request, timeout=None):
        raise urllib.error.URLError("offline")

    result = ReleaseManager(data_dir=tmp_path, urlopen=offline).list_releases()

    assert result["prepared_release"] == "v0.6.0"
    assert next(
        item for item in result["releases"] if item["tag"] == "v0.6.0"
    )["prepared"] is True
    latest = next(item for item in result["releases"] if item["tag"] == "latest")
    assert latest["selectable"] is False
    assert "unavailable" in result["warnings"][0]


def test_prepare_extracts_only_whitelisted_resources_and_writes_manifest(tmp_path):
    manager = ReleaseManager(data_dir=tmp_path, urlopen=_opener())
    manager.list_releases()
    result = manager.prepare("v0.6.0")
    root = tmp_path / "releases" / "v0.6.0"

    assert result["status"] == "ready"
    assert result["config_template_loaded"] is True
    assert result["docker_image"].endswith(":v0.6.0")
    assert result["resources"]["config_template_available"] is True
    assert (root / "install-docker.sh").is_file()
    assert (root / "deploy" / "docker" / "compose.influxdb.yml").is_file()
    assert not (root / "README.md").exists()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["tag"] == "v0.6.0"
    assert manifest["release"] == "v0.6.0"
    assert manifest["prepared_at"]
    assert manifest["config_template"] == "config.template.json"
    assert manifest["docker_compose_template"] == "docker-compose.example.yml"
    assert manifest["docker_image"].endswith(":v0.6.0")
    assert manifest["resources"]["config_template"] == "config.template.json"
    selected = json.loads(
        (tmp_path / "state" / "selected-release.json").read_text(encoding="utf-8")
    )
    assert selected["tag"] == "v0.6.0"
    assert not (tmp_path / "selected-release.json").exists()


def test_prepare_accepts_config_directory_template(tmp_path):
    # Newer release archives ship the template at config/config.template.json;
    # it is flattened into the cache as config.template.json.
    content = {
        "repo/config/config.template.json": b'{"devices": []}\n',
        "repo/docker-compose.example.yml": b"services: {}\n",
        "repo/install-docker.sh": b"#!/bin/sh\n",
        "repo/install-docker.ps1": b"Write-Host install\n",
        "repo/deploy/docker/compose.influxdb.yml": b"services: {}\n",
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as handle:
        for name, value in content.items():
            handle.writestr(name, value)

    manager = ReleaseManager(data_dir=tmp_path, urlopen=_opener(output.getvalue()))
    manager.list_releases()
    result = manager.prepare("v0.6.0")
    root = tmp_path / "releases" / "v0.6.0"

    assert result["status"] == "ready"
    assert result["config_template_loaded"] is True
    assert (root / "config.template.json").is_file()
    assert not (root / "config").exists()


def test_archive_path_traversal_is_rejected(tmp_path):
    archive = _archive({"repo/../../outside.txt": b"unsafe"})
    manager = ReleaseManager(data_dir=tmp_path, urlopen=_opener(archive))
    manager.list_releases()

    with pytest.raises(ReleaseError, match="unsafe path"):
        manager.prepare("v0.6.0")
    assert not (tmp_path / "outside.txt").exists()


def test_reprepare_reuses_complete_cache_without_download(tmp_path):
    root = _write_cached(tmp_path)

    def no_network(_request, timeout=None):
        raise AssertionError("complete cache must not use the network")

    result = ReleaseManager(data_dir=tmp_path, urlopen=no_network).prepare("v0.6.0")
    assert result["reused"] is True
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["release"] == "v0.6.0"
    assert manifest["config_template"] == "config.template.json"
    assert manifest["docker_image"].endswith(":v0.6.0")


def test_archive_download_error_becomes_structured_release_error(tmp_path):
    calls = 0

    def failing_download(request, timeout=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _Response(json.dumps(_github_payload()).encode())
        raise urllib.error.HTTPError(
            request.full_url, 503, "unavailable", hdrs=None, fp=None
        )

    manager = ReleaseManager(
        data_dir=tmp_path,
        urlopen=failing_download,
        resource_checker=lambda _ref: True,
    )
    manager.list_releases()
    with pytest.raises(ReleaseError, match="Could not download") as raised:
        manager.prepare("v0.6.0")
    assert raised.value.status == 502


def test_active_release_detected_from_compose_image(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "docker-compose.yml").write_text(
        "services:\n  ems:\n"
        "    image: ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.0\n",
        encoding="utf-8",
    )
    manager = ReleaseManager(data_dir=tmp_path / "data", project_dir=project)
    assert manager.detect_active_release() == "v0.6.0"


def test_latest_compose_image_is_not_claimed_as_concrete_active_release(tmp_path):
    (tmp_path / "docker-compose.yml").write_text(
        "image: ghcr.io/basecubedev/ems-solarflow-api-control:latest\n",
        encoding="utf-8",
    )
    manager = ReleaseManager(data_dir=tmp_path / "data", project_dir=tmp_path)
    assert manager.detect_active_release() is None


def test_prepare_blocks_downgrade_from_newer_active_release(tmp_path):
    _write_cached(tmp_path, "v0.5.0")
    project = tmp_path / "project"
    project.mkdir()
    (project / "docker-compose.yml").write_text(
        "image: ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.0\n",
        encoding="utf-8",
    )
    manager = ReleaseManager(data_dir=tmp_path, project_dir=project)

    with pytest.raises(ReleaseError, match="Downgrade") as raised:
        manager.prepare("v0.5.0")
    assert raised.value.status == 409


def test_prepare_rejects_pre_v060_release_as_unsupported(tmp_path):
    manager = ReleaseManager(data_dir=tmp_path, urlopen=_opener())

    with pytest.raises(ReleaseError, match="from v0.6.0 onward") as raised:
        manager.prepare("v0.5.9")
    assert raised.value.status == 400


def test_missing_docker_resources_disable_supported_release(tmp_path):
    manager = ReleaseManager(
        data_dir=tmp_path,
        urlopen=_opener(),
        resource_checker=lambda ref: ref != "v0.6.1",
    )
    result = manager.list_releases()
    by_tag = {item["tag"]: item for item in result["releases"]}

    assert by_tag["v0.6.1"]["admin_supported"] is True
    assert by_tag["v0.6.1"]["docker_supported"] is False
    assert by_tag["v0.6.1"]["selectable"] is False
    assert "resources are missing" in by_tag["v0.6.1"]["reason"]
    assert result["default_release"] == "v0.6.0"


def test_latest_is_default_fallback_when_no_stable_resources_verify(tmp_path):
    manager = ReleaseManager(
        data_dir=tmp_path,
        urlopen=_opener(),
        resource_checker=lambda ref: ref == "main",
    )
    result = manager.list_releases()

    assert result["latest_stable"] is None
    assert result["default_release"] == "latest"


def test_latest_is_synthetic_selectable_rolling_channel(tmp_path):
    result = ReleaseManager(data_dir=tmp_path, urlopen=_opener()).list_releases()
    latest = next(item for item in result["releases"] if item["tag"] == "latest")

    assert latest["kind"] == "tag"
    assert latest["version"] is None
    assert latest["channel"] == "latest"
    assert latest["stable"] is False
    assert latest["docker_supported"] is True
    assert latest["selectable"] is True


class _LocalOnlyDocker:
    """Docker inspector double: inspects local images, never pulls."""

    def __init__(self, images):
        self._images = images

    def inspect_image(self, ref):
        return self._images.get(ref)

    def pull(self, ref, on_progress=None):
        raise AssertionError(f"local discovery must not pull {ref}")


def _local_images(build_id="local-f7265fc"):
    from admin.admin_update import ADMIN_IMAGE_REPO, EMS_IMAGE_REPO

    rev = "f7265fc747c2223f126f0ee7801e030c6226edf4"
    labels = {
        "org.opencontainers.image.version": "local",
        "org.opencontainers.image.revision": rev,
        "de.basecubedev.ems.build_id": build_id,
        "de.basecubedev.ems.channel": "development",
        "de.basecubedev.ems.release_tag": "local",
    }
    return {
        f"{ADMIN_IMAGE_REPO}:local": {
            "image_ref": f"{ADMIN_IMAGE_REPO}:local",
            "digest": "sha256:localadmin",
            "labels": labels,
        },
        f"{EMS_IMAGE_REPO}:local": {
            "image_ref": f"{EMS_IMAGE_REPO}:local",
            "digest": "sha256:localems",
            "labels": labels,
        },
    }


def test_valid_local_pair_appears_under_experimental(tmp_path):
    docker = _LocalOnlyDocker(_local_images())
    manager = ReleaseManager(data_dir=tmp_path, urlopen=_opener(), docker=docker)

    result = manager.list_releases(for_upgrade=False)

    local = next((r for r in result["releases"] if r["tag"] == "local"), None)
    assert local is not None
    assert local["channel"] == "development"  # frontend maps this to Experimental
    assert local["selectable"] is True
    assert "checkout" in local["name"]


def test_local_pair_is_not_presented_as_latest(tmp_path):
    docker = _LocalOnlyDocker(_local_images())
    manager = ReleaseManager(data_dir=tmp_path, urlopen=_opener(), docker=docker)

    result = manager.list_releases(for_upgrade=False)

    local = next(r for r in result["releases"] if r["tag"] == "local")
    assert local["tag"] == "local"
    latest = next((r for r in result["releases"] if r["tag"] == "latest"), None)
    if latest is not None:
        assert latest["channel"] == "latest"


def test_dirty_local_build_remains_explicitly_marked(tmp_path):
    docker = _LocalOnlyDocker(_local_images(build_id="local-f7265fc-dirty"))
    manager = ReleaseManager(data_dir=tmp_path, urlopen=_opener(), docker=docker)

    result = manager.list_releases(for_upgrade=False)

    local = next(r for r in result["releases"] if r["tag"] == "local")
    assert local["dirty"] is True
    assert local["build_id"] == "local-f7265fc-dirty"


def test_no_local_images_means_no_local_entry(tmp_path):
    manager = ReleaseManager(data_dir=tmp_path, urlopen=_opener(), docker=None)

    result = manager.list_releases(for_upgrade=False)

    assert all(r["tag"] != "local" for r in result["releases"])


def test_legacy_versioned_resources_come_from_exact_tag():
    ref = legacy_release_resource_ref(
        tag="v0.7.0", channel="stable", revision=LEGACY_REVISION
    )
    assert ref == "v0.7.0"
    url = legacy_release_resource_url(
        tag="v0.7.0", channel="stable", revision=LEGACY_REVISION
    )
    assert url == f"https://codeload.github.com/{REPO}/zip/refs/tags/v0.7.0"


def test_legacy_latest_resources_come_from_exact_revision():
    ref = legacy_release_resource_ref(
        tag="latest", channel="latest", revision=LEGACY_REVISION
    )
    assert ref == LEGACY_REVISION
    url = legacy_release_resource_url(
        tag="latest", channel="latest", revision=LEGACY_REVISION
    )
    assert url == f"https://codeload.github.com/{REPO}/zip/{LEGACY_REVISION}"
    assert "refs/heads/main" not in url


def test_legacy_latest_without_pinned_revision_is_rejected():
    with pytest.raises(ReleaseError):
        legacy_release_resource_ref(tag="latest", channel="latest", revision=None)


def test_legacy_latest_preparation_pins_to_revision_never_main(tmp_path):
    urls = []
    base_opener = _opener()

    def tracking_opener(request, timeout=None):
        urls.append(request.full_url)
        return base_opener(request, timeout=timeout)

    manager = ReleaseManager(data_dir=tmp_path, urlopen=tracking_opener)
    manager.list_releases()
    result = manager.prepare("latest", revision=LEGACY_REVISION)

    assert result["status"] == "ready"
    assert result["manifest"]["resource_ref"] == LEGACY_REVISION
    assert result["manifest"]["resolved_revision"] == LEGACY_REVISION
    assert result["manifest"]["expected_ems_revision"] == LEGACY_REVISION
    assert any(f"/zip/{LEGACY_REVISION}" in url for url in urls)
    assert all("/zip/refs/heads/main" not in url for url in urls)


def test_legacy_tag_revision_matches_image_and_is_recorded(tmp_path):
    manager = ReleaseManager(
        data_dir=tmp_path,
        urlopen=_opener(),
        revision_resolver=lambda _ref: LEGACY_REVISION,
    )
    manager.list_releases()

    result = manager.prepare("v0.6.0", revision=LEGACY_REVISION)

    manifest = result["manifest"]
    assert manifest["resource_ref"] == "refs/tags/v0.6.0"
    assert manifest["resolved_revision"] == LEGACY_REVISION
    assert manifest["expected_ems_revision"] == LEGACY_REVISION
    assert len(manifest["archive_sha256"]) == 64
    assert manifest["resource_hashes"]["config.template.json"]
    assert result["legacy_identity_verified"] is True


def test_legacy_tag_revision_mismatch_fails_before_download(tmp_path):
    downloads = []

    def opener(request, timeout=None):
        if "/zip/" in request.full_url:
            downloads.append(request.full_url)
        return _opener()(request, timeout=timeout)

    manager = ReleaseManager(
        data_dir=tmp_path,
        urlopen=opener,
        revision_resolver=lambda _ref: OTHER_LEGACY_REVISION,
    )
    manager.list_releases()

    with pytest.raises(ReleaseError, match="revision") as raised:
        manager.prepare("v0.6.0", revision=LEGACY_REVISION)

    assert raised.value.status == 409
    assert downloads == []


def test_moved_legacy_tag_invalidates_verified_cache(tmp_path):
    first = ReleaseManager(
        data_dir=tmp_path,
        urlopen=_opener(),
        revision_resolver=lambda _ref: LEGACY_REVISION,
    )
    first.list_releases()
    first.prepare("v0.6.0", revision=LEGACY_REVISION)

    moved = ReleaseManager(
        data_dir=tmp_path,
        urlopen=_opener(),
        revision_resolver=lambda _ref: OTHER_LEGACY_REVISION,
    )
    with pytest.raises(ReleaseError, match="revision"):
        moved.prepare("v0.6.0", revision=LEGACY_REVISION)


def test_verified_cache_from_another_ems_revision_is_rejected_offline(tmp_path):
    first = ReleaseManager(
        data_dir=tmp_path,
        urlopen=_opener(),
        revision_resolver=lambda _ref: LEGACY_REVISION,
    )
    first.list_releases()
    first.prepare("v0.6.0", revision=LEGACY_REVISION)

    def offline(*_args, **_kwargs):
        raise urllib.error.URLError("offline")

    second = ReleaseManager(
        data_dir=tmp_path,
        urlopen=offline,
        revision_resolver=offline,
    )
    with pytest.raises(ReleaseError):
        second.prepare("v0.6.0", revision=OTHER_LEGACY_REVISION)


def test_archive_hash_mismatch_rejects_offline_cache_reuse(tmp_path):
    first = ReleaseManager(
        data_dir=tmp_path,
        urlopen=_opener(),
        revision_resolver=lambda _ref: LEGACY_REVISION,
    )
    first.list_releases()
    first.prepare("v0.6.0", revision=LEGACY_REVISION)
    (tmp_path / "releases" / "v0.6.0" / ".source-archive").write_bytes(
        b"tampered archive"
    )

    def offline(*_args, **_kwargs):
        raise urllib.error.URLError("offline")

    second = ReleaseManager(
        data_dir=tmp_path,
        urlopen=offline,
        revision_resolver=offline,
    )
    with pytest.raises(ReleaseError):
        second.prepare("v0.6.0", revision=LEGACY_REVISION)


def test_fully_verified_legacy_cache_is_reusable_offline(tmp_path):
    first = ReleaseManager(
        data_dir=tmp_path,
        urlopen=_opener(),
        revision_resolver=lambda _ref: LEGACY_REVISION,
    )
    first.list_releases()
    first.prepare("v0.6.0", revision=LEGACY_REVISION)

    def offline(*_args, **_kwargs):
        raise urllib.error.URLError("offline")

    second = ReleaseManager(
        data_dir=tmp_path,
        urlopen=offline,
        revision_resolver=offline,
    )
    result = second.prepare("v0.6.0", revision=LEGACY_REVISION)

    assert result["reused"] is True
    assert result["legacy_identity_verified"] is True


def test_latest_preparation_downloads_main_branch_resources(tmp_path):
    urls = []
    base_opener = _opener()

    def tracking_opener(request, timeout=None):
        urls.append(request.full_url)
        return base_opener(request, timeout=timeout)

    manager = ReleaseManager(data_dir=tmp_path, urlopen=tracking_opener)
    manager.list_releases()
    result = manager.prepare("latest")

    assert result["status"] == "ready"
    assert result["legacy_identity_verified"] is False
    assert any("unverified" in warning for warning in result["warnings"])
    assert any("/zip/refs/heads/main" in url for url in urls)


def test_release_preparation_does_not_pull_docker_image(tmp_path):
    urls = []
    base_opener = _opener()

    def tracking_opener(request, timeout=None):
        urls.append(request.full_url)
        return base_opener(request, timeout=timeout)

    manager = ReleaseManager(data_dir=tmp_path, urlopen=tracking_opener)
    manager.list_releases()
    result = manager.prepare("v0.6.0")

    assert result["docker_image"] == "ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.0"
    assert all(urlparse(url).hostname != "ghcr.io" for url in urls)


def test_prepared_newer_release_also_blocks_downgrade(tmp_path):
    _write_cached(tmp_path, "v0.7.0")
    manager = ReleaseManager(data_dir=tmp_path)

    with pytest.raises(ReleaseError, match="Downgrades") as raised:
        manager.prepare("v0.6.0")
    assert raised.value.status == 409


def test_incomparable_active_tag_warns_without_blocking_release(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "docker-compose.yml").write_text(
        "image: ghcr.io/basecubedev/ems-solarflow-api-control:nightly\n",
        encoding="utf-8",
    )
    result = ReleaseManager(
        data_dir=tmp_path / "data", project_dir=project, urlopen=_opener()
    ).list_releases()

    # v0.5.9 is filtered out; every remaining release stays selectable.
    assert all(item["selectable"] for item in result["releases"])
    assert any("cannot be compared safely" in warning for warning in result["warnings"])


def test_config_template_comes_from_selected_cached_release(tmp_path):
    _write_cached(tmp_path)
    manager = ReleaseManager(data_dir=tmp_path)
    manager.prepare("v0.6.0")
    result = manager.config_template()

    assert result["tag"] == "v0.6.0"
    assert result["template"] == {"devices": []}
    assert result["source"].endswith("v0.6.0/config.template.json")
    assert result["docker_image"].endswith(":v0.6.0")


def test_config_template_requires_prepared_release(tmp_path):
    manager = ReleaseManager(data_dir=tmp_path)

    with pytest.raises(ReleaseError, match="No release resources prepared yet") as raised:
        manager.config_template()

    assert raised.value.status == 404


@pytest.mark.parametrize(
    ("content", "message"),
    (
        (None, "missing config.template.json"),
        ("not-json", "invalid config.template.json"),
        ("[]", "invalid config.template.json"),
    ),
)
def test_config_template_reports_selected_release_resource_error(
    tmp_path, content, message
):
    state = tmp_path / "state"
    state.mkdir()
    (state / "selected-release.json").write_text(
        json.dumps({"tag": "v0.6.0"}), encoding="utf-8"
    )
    release = tmp_path / "releases" / "v0.6.0"
    release.mkdir(parents=True)
    if content is not None:
        (release / "config.template.json").write_text(content, encoding="utf-8")

    with pytest.raises(ReleaseError, match=message) as raised:
        ReleaseManager(data_dir=tmp_path).config_template()

    assert raised.value.status == 500


# --- build-identity aware release selection ------------------------------

DOCKER_IMAGE = "ghcr.io/basecubedev/ems-solarflow-api-control"


def _ref(tag):
    return f"{DOCKER_IMAGE}:{tag}"


def _image(digest=None, build_serial=None, channel=None):
    labels = {}
    if channel is not None:
        labels["de.basecubedev.ems.channel"] = channel
    if build_serial is not None:
        labels["de.basecubedev.ems.build_serial"] = str(build_serial)
    return {"digest": digest, "labels": labels}


class _FakeDocker:
    """Read-only Docker double exposing inspect_container/inspect_image."""

    def __init__(self, container=None, images=None):
        self._container = container
        self._images = images or {}

    def inspect_container(self, _name):
        return dict(self._container) if self._container else None

    def inspect_image(self, image_ref):
        found = self._images.get(image_ref)
        return dict(found) if found else None


def _running_container(tag):
    return {
        "container_name": "ems-solarflow-api-control",
        "image": _ref(tag),
        "status": "running",
    }


def _project_compose(tmp_path, tag):
    project = tmp_path / "project"
    project.mkdir()
    (project / "docker-compose.yml").write_text(
        "services:\n  ems:\n"
        f"    image: {_ref(tag)}\n"
        "    container_name: ems-solarflow-api-control\n",
        encoding="utf-8",
    )
    return project


def _identity_release(tmp_path, running_tag, images):
    return ReleaseManager(
        data_dir=tmp_path / "data",
        project_dir=_project_compose(tmp_path, running_tag),
        urlopen=_opener(),
        docker=_FakeDocker(container=_running_container(running_tag), images=images),
    )


def test_running_latest_blocks_older_build_serial_stable_target(tmp_path):
    manager = _identity_release(
        tmp_path,
        "latest",
        {
            _ref("latest"): _image(digest="sha256:l", build_serial=1200, channel="latest"),
            _ref("v0.6.0"): _image(digest="sha256:s", build_serial=1180),
        },
    )
    by_tag = {item["tag"]: item for item in manager.list_releases()["releases"]}

    assert by_tag["v0.6.0"]["upgrade_state"] == "older_than_running_build"
    assert by_tag["v0.6.0"]["selectable"] is False
    assert "older than the running EMS build" in by_tag["v0.6.0"]["reason"]


def test_running_latest_allows_newer_build_serial_stable_target(tmp_path):
    manager = _identity_release(
        tmp_path,
        "latest",
        {
            _ref("latest"): _image(digest="sha256:l", build_serial=1200, channel="latest"),
            _ref("v0.6.0"): _image(digest="sha256:s", build_serial=1300),
        },
    )
    by_tag = {item["tag"]: item for item in manager.list_releases()["releases"]}

    assert by_tag["v0.6.0"]["upgrade_state"] == "upgrade_available"
    assert by_tag["v0.6.0"]["selectable"] is True


def test_same_digest_target_is_reported_already_current(tmp_path):
    shared = "sha256:" + "d" * 64
    manager = _identity_release(
        tmp_path,
        "latest",
        {
            _ref("latest"): _image(digest=shared, build_serial=1200, channel="latest"),
            _ref("v0.6.0"): _image(digest=shared, build_serial=1200),
        },
    )
    by_tag = {item["tag"]: item for item in manager.list_releases()["releases"]}

    assert by_tag["v0.6.0"]["upgrade_state"] == "already_current"
    assert by_tag["v0.6.0"]["selectable"] is False
    assert by_tag["v0.6.0"]["reason"] == "Already running this EMS build."


# --- release list must never dead-end / latest is a rolling channel ------


def test_running_newest_stable_keeps_latest_selectable_when_same_build(tmp_path):
    # Regression: right after cutting v0.6.1 the ``latest`` image is the same
    # build as the running release. It must still be selectable (rolling
    # channel) so the upgrade list is never empty -> "No EMS releases available".
    shared = "sha256:" + "1" * 64
    manager = _identity_release(
        tmp_path,
        "v0.6.1",
        {
            _ref("v0.6.1"): _image(digest=shared, build_serial=1300, channel="stable"),
            _ref("latest"): _image(digest=shared, build_serial=1300, channel="latest"),
        },
    )
    result = manager.list_releases()
    by_tag = {item["tag"]: item for item in result["releases"]}

    assert by_tag["latest"]["selectable"] is True
    assert by_tag["v0.6.1"]["selectable"] is False  # already current
    assert result["default_release"] == "latest"
    assert any(item["selectable"] for item in result["releases"])


def test_running_newest_stable_keeps_latest_selectable_when_latest_is_older(tmp_path):
    # Regression: a locally cached ``latest`` with a lower build serial than the
    # freshly-released running stable must not be blocked as older-than-running.
    manager = _identity_release(
        tmp_path,
        "v0.6.1",
        {
            _ref("v0.6.1"): _image(digest="sha256:aaa", build_serial=1300, channel="stable"),
            _ref("latest"): _image(digest="sha256:bbb", build_serial=1250, channel="latest"),
        },
    )
    by_tag = {item["tag"]: item for item in manager.list_releases()["releases"]}

    assert by_tag["latest"]["upgrade_state"] == "upgrade_available"
    assert by_tag["latest"]["selectable"] is True


def test_verify_target_latest_older_serial_is_allowed_channel_switch(tmp_path):
    # The upgrade executor must match the list: switching to ``latest`` is
    # allowed even when its build serial is lower than the running release.
    manager = _verify_manager(
        tmp_path,
        "v0.6.1",
        images={_ref("v0.6.1"): _image(digest="sha256:aaa", build_serial=1300, channel="stable")},
        pullable={_ref("latest"): _image(digest="sha256:bbb", build_serial=1250, channel="latest")},
    )

    assessment = manager.verify_upgrade_target("latest", pull=manager._docker.pull)

    assert assessment.state == "upgrade_available"
    assert assessment.basis == "channel"
    assert assessment.is_upgrade


# --- Guided Setup lists every supported release (fresh install) ----------


def test_setup_flow_offers_every_supported_release_ignoring_running_build(tmp_path):
    # Regression: a fresh install (for_upgrade=False) must offer every supported
    # release even while a newer EMS is running -- including legacy v0.6.x images
    # without build-identity labels -- and must not gate on the running build.
    manager = _identity_release(
        tmp_path,
        "v0.6.1",
        {_ref("v0.6.1"): _image(digest="sha256:aaa", build_serial=1300, channel="stable")},
    )
    result = manager.list_releases(for_upgrade=False)
    by_tag = {item["tag"]: item for item in result["releases"]}

    assert by_tag["latest"]["selectable"] is True
    assert by_tag["v0.6.1"]["selectable"] is True
    assert by_tag["v0.6.0"]["selectable"] is True  # legacy, older than running
    assert "v0.5.9" not in by_tag  # pre-v0.6.0 still hidden
    assert result["releases"][0]["tag"] == "latest"  # latest still first


def test_setup_flow_does_not_inspect_the_running_container(tmp_path):
    # Setup must not touch Docker for the running build: a daemon that raises on
    # inspection would otherwise break a fresh install.
    class _ExplodingDocker:
        def inspect_container(self, _name):
            raise AssertionError("setup must not inspect the running container")

        def inspect_image(self, _ref):
            raise AssertionError("setup must not inspect the running image")

    manager = ReleaseManager(
        data_dir=tmp_path / "data",
        project_dir=_project_compose(tmp_path, "v0.6.1"),
        urlopen=_opener(),
        docker=_ExplodingDocker(),
    )
    result = manager.list_releases(for_upgrade=False)

    assert all(
        item["selectable"] for item in result["releases"] if item["docker_supported"]
    )
    assert {"latest", "v0.6.1", "v0.6.0"}.issubset(
        {item["tag"] for item in result["releases"]}
    )


def test_prepare_blocks_semver_downgrade_even_when_target_build_is_newer(tmp_path):
    # v0.7.0 -> v0.6.9 stays blocked even though the target build serial is higher.
    manager = ReleaseManager(
        data_dir=tmp_path / "data",
        project_dir=_project_compose(tmp_path, "v0.7.0"),
        docker=_FakeDocker(
            container=_running_container("v0.7.0"),
            images={
                _ref("v0.7.0"): _image(build_serial=1),
                _ref("v0.6.9"): _image(build_serial=9999),
            },
        ),
    )
    with pytest.raises(ReleaseError, match="Downgrade") as raised:
        manager.prepare("v0.6.9")
    assert raised.value.status == 409


def test_prepare_blocks_when_upgrade_cannot_be_proven(tmp_path):
    # Running latest with a known build, but the target image is not present
    # locally, so Guided Upgrade cannot prove the move is an upgrade.
    manager = ReleaseManager(
        data_dir=tmp_path / "data",
        project_dir=_project_compose(tmp_path, "latest"),
        docker=_FakeDocker(
            container=_running_container("latest"),
            images={_ref("latest"): _image(build_serial=1200, channel="latest")},
        ),
    )
    with pytest.raises(ReleaseError, match="cannot verify") as raised:
        manager.prepare("v0.6.0")
    assert raised.value.status == 409


def test_running_stable_allows_unlabeled_semver_upgrade_with_warning(tmp_path):
    # Legacy v0.6.0 -> v0.6.1 with no build labels: SemVer proves the upgrade and
    # the release stays selectable, carrying the SemVer-fallback warning.
    manager = _identity_release(
        tmp_path,
        "v0.6.0",
        {_ref("v0.6.0"): _image(digest="sha256:a")},
    )
    by_tag = {item["tag"]: item for item in manager.list_releases()["releases"]}

    assert by_tag["v0.6.1"]["upgrade_state"] == "upgrade_available"
    assert by_tag["v0.6.1"]["selectable"] is True
    assert "SemVer" in by_tag["v0.6.1"]["upgrade_warning"]
    assert by_tag["v0.6.1"]["reason"] == by_tag["v0.6.1"]["upgrade_warning"]


def test_running_stable_blocks_unlabeled_semver_downgrade(tmp_path):
    # v0.6.1 -> v0.6.0 with no labels stays a downgrade via the SemVer fallback.
    manager = _identity_release(
        tmp_path,
        "v0.6.1",
        {_ref("v0.6.1"): _image(digest="sha256:a")},
    )
    by_tag = {item["tag"]: item for item in manager.list_releases()["releases"]}

    assert by_tag["v0.6.0"]["upgrade_state"] == "downgrade_blocked"
    assert by_tag["v0.6.0"]["selectable"] is False


def test_verify_unlabeled_semver_downgrade_blocks_even_with_override(tmp_path, monkeypatch):
    # The override must never turn a SemVer-proven downgrade into an upgrade.
    monkeypatch.setenv("ADMIN_ALLOW_LEGACY_UNVERIFIED_UPGRADES", "true")
    manager = _verify_manager(
        tmp_path,
        "v0.6.1",
        images={_ref("v0.6.1"): _image(digest="sha256:a")},
    )

    assessment = manager.verify_upgrade_target("v0.6.0", pull=manager._docker.pull)

    assert assessment.state == "downgrade_blocked"
    assert assessment.blocked


def test_verify_override_allows_unlabeled_legacy_with_warning(tmp_path, monkeypatch):
    # Running ``latest`` (known build) to an unlabeled legacy stable: blocked by
    # default (see ``test_verify_target_unknown_when_pulled_image_has_no_labels``),
    # but the test override allows it and returns a clear warning.
    monkeypatch.setenv("ADMIN_ALLOW_LEGACY_UNVERIFIED_UPGRADES", "true")
    manager = _verify_manager(
        tmp_path,
        "latest",
        images={_ref("latest"): _image(digest="sha256:l", build_serial=1200, channel="latest")},
        pullable={_ref("v0.6.1"): {"digest": "sha256:s", "labels": {}}},
    )

    assessment = manager.verify_upgrade_target("v0.6.1", pull=manager._docker.pull)

    assert assessment.state == "upgrade_available"
    assert assessment.basis == "legacy_unverified"
    assert assessment.is_upgrade and assessment.warning
    assert manager._docker.pulled == [_ref("v0.6.1")]


def test_prepare_override_allows_unverifiable_legacy(tmp_path, monkeypatch):
    # Same latest->legacy case the default blocks in
    # ``test_prepare_blocks_when_upgrade_cannot_be_proven`` now prepares.
    monkeypatch.setenv("ADMIN_ALLOW_LEGACY_UNVERIFIED_UPGRADES", "true")
    manager = ReleaseManager(
        data_dir=tmp_path / "data",
        project_dir=_project_compose(tmp_path, "latest"),
        urlopen=_opener(),
        docker=_FakeDocker(
            container=_running_container("latest"),
            images={_ref("latest"): _image(build_serial=1200, channel="latest")},
        ),
    )
    manager.list_releases()

    result = manager.prepare("v0.6.0")

    assert result["status"] == "ready"


def test_no_docker_inspector_keeps_semver_only_behaviour(tmp_path):
    # Without a Docker inspector, no new identity blocks appear: a plain stable
    # listing stays selectable and reports a SemVer-based state.
    by_tag = {
        item["tag"]: item
        for item in ReleaseManager(data_dir=tmp_path, urlopen=_opener())
        .list_releases()["releases"]
    }

    assert by_tag["v0.6.1"]["selectable"] is True
    assert by_tag["v0.6.1"]["upgrade_state"] in ("upgrade_available", "identity_unknown")


# --- target-image verification (Guided Upgrade gate) ---------------------


class _PullingDocker(_FakeDocker):
    """`_FakeDocker` whose `pullable` images become inspectable after `pull`."""

    def __init__(self, container=None, images=None, pullable=None):
        super().__init__(container=container, images=images)
        self._pullable = dict(pullable or {})
        self.pulled = []

    def pull(self, image, on_progress=None):
        self.pulled.append(image)
        if image in self._pullable:
            self._images[image] = self._pullable.pop(image)


def _verify_manager(tmp_path, running_tag, images, pullable=None):
    return ReleaseManager(
        data_dir=tmp_path / "data",
        project_dir=_project_compose(tmp_path, running_tag),
        urlopen=_opener(),
        docker=_PullingDocker(
            container=_running_container(running_tag), images=images, pullable=pullable
        ),
    )


def test_verify_target_pulls_missing_image_then_allows_newer_serial(tmp_path):
    manager = _verify_manager(
        tmp_path,
        "latest",
        images={_ref("latest"): _image(digest="sha256:l", build_serial=1200, channel="latest")},
        pullable={_ref("v0.6.1"): _image(digest="sha256:s", build_serial=1300)},
    )

    assessment = manager.verify_upgrade_target("v0.6.1", pull=manager._docker.pull)

    assert assessment.state == "upgrade_available"
    assert assessment.basis == "build_serial"
    assert manager._docker.pulled == [_ref("v0.6.1")]


def test_verify_target_blocks_lower_build_serial(tmp_path):
    manager = _verify_manager(
        tmp_path,
        "latest",
        images={_ref("latest"): _image(digest="sha256:l", build_serial=1200, channel="latest")},
        pullable={_ref("v0.6.1"): _image(digest="sha256:s", build_serial=1100)},
    )

    assessment = manager.verify_upgrade_target("v0.6.1", pull=manager._docker.pull)

    assert assessment.state == "older_than_running_build"
    assert assessment.blocked


def test_verify_target_same_digest_is_noop(tmp_path):
    shared = "sha256:" + "d" * 64
    manager = _verify_manager(
        tmp_path,
        "latest",
        images={_ref("latest"): _image(digest=shared, build_serial=1200, channel="latest")},
        pullable={_ref("v0.6.1"): _image(digest=shared, build_serial=1200)},
    )

    assessment = manager.verify_upgrade_target("v0.6.1", pull=manager._docker.pull)

    assert assessment.state == "already_current"
    assert assessment.is_noop


def test_verify_target_unknown_when_pulled_image_has_no_labels(tmp_path):
    manager = _verify_manager(
        tmp_path,
        "latest",
        images={_ref("latest"): _image(digest="sha256:l", build_serial=1200, channel="latest")},
        pullable={_ref("v0.6.1"): {"digest": None, "labels": {}}},
    )

    assessment = manager.verify_upgrade_target("v0.6.1", pull=manager._docker.pull)

    assert assessment.state == "identity_unknown"
    assert assessment.blocked
    assert manager._docker.pulled == [_ref("v0.6.1")]


def test_list_keeps_not_local_target_selectable_and_latest_first(tmp_path):
    # Running ``latest`` with a known build; the stable target is not local yet,
    # so its identity is unverifiable at list time. Selection must stay open
    # (verified later) while filtering is unchanged: latest first, < v0.6.0 gone.
    manager = _verify_manager(
        tmp_path,
        "latest",
        images={_ref("latest"): _image(digest="sha256:l", build_serial=1200, channel="latest")},
    )
    result = manager.list_releases()
    by_tag = {item["tag"]: item for item in result["releases"]}

    assert result["releases"][0]["tag"] == "latest"
    assert "v0.5.9" not in by_tag
    assert by_tag["v0.6.1"]["upgrade_state"] == "identity_unknown"
    assert by_tag["v0.6.1"]["selectable"] is True


# --- unified catalogue: development builds --------------------------------

_DEV_REVISION = "95a135fa838a144db2bae7d45cd8c0fb2d453f81"
_DEV_TAG = "dev-feature-zendure-mqtt-device-support-95a135f-123456789-1"
_DEV_TAG_2 = "dev-firefox-performance-fix-a21bc94-987654321-1"
_DEV_FLOATING = "dev-feature-zendure-mqtt-device-support"


def _development_source(entries):
    return lambda: list(entries)


def _dev_entry(
    tag=_DEV_TAG,
    display_name="MQTT device support",
    revision=_DEV_REVISION,
    created_at="2026-07-14T09:00:00Z",
    installable=True,
):
    return {
        "tag": tag,
        "display_name": display_name,
        "revision": revision,
        "created_at": created_at,
        "installable": installable,
    }


def test_catalogue_without_development_source_has_no_development_channel(tmp_path):
    result = ReleaseManager(data_dir=tmp_path, urlopen=_opener()).list_releases()
    assert all(item["channel"] != "development" for item in result["releases"])


def test_development_builds_appear_after_stable_and_release_candidates(tmp_path):
    manager = ReleaseManager(
        data_dir=tmp_path,
        urlopen=_opener(),
        development_source=_development_source([_dev_entry()]),
    )
    result = manager.list_releases()
    channels = [item["channel"] for item in result["releases"]]

    # Stable (incl. rolling latest) first, then rc, then development last.
    assert channels[0] in ("latest", "stable")
    last_stable = max(i for i, c in enumerate(channels) if c in ("latest", "stable"))
    first_rc = min(i for i, c in enumerate(channels) if c == "rc")
    dev_indexes = [i for i, c in enumerate(channels) if c == "development"]
    assert last_stable < first_rc
    assert dev_indexes and min(dev_indexes) > first_rc
    assert dev_indexes == list(range(len(channels) - len(dev_indexes), len(channels)))


def test_development_catalogue_item_exposes_normalized_metadata(tmp_path):
    manager = ReleaseManager(
        data_dir=tmp_path,
        urlopen=_opener(),
        development_source=_development_source([_dev_entry()]),
    )
    by_tag = {item["tag"]: item for item in manager.list_releases()["releases"]}

    item = by_tag[_DEV_TAG]
    assert item["channel"] == "development"
    assert item["display_name"] == "MQTT device support"
    assert item["revision_short"] == "95a135f"
    assert item["created_at"] == "2026-07-14T09:00:00Z"
    assert item["installable"] is True
    assert item["selectable"] is True
    assert item["stable"] is False
    assert item["prerelease"] is False
    assert item["version"] is None


def test_pruned_development_build_in_local_cache_is_not_listed(tmp_path):
    _write_cached(tmp_path, _DEV_TAG_2)
    manager = ReleaseManager(
        data_dir=tmp_path,
        urlopen=_opener(),
        development_source=_development_source([_dev_entry(tag=_DEV_TAG)]),
    )
    tags = [item["tag"] for item in manager.list_releases()["releases"]]
    assert _DEV_TAG in tags
    assert _DEV_TAG_2 not in tags


def test_cached_and_catalogued_development_build_is_listed_once(tmp_path):
    _write_cached(tmp_path, _DEV_TAG)
    manager = ReleaseManager(
        data_dir=tmp_path,
        urlopen=_opener(),
        development_source=_development_source([_dev_entry(tag=_DEV_TAG)]),
    )
    items = [i for i in manager.list_releases()["releases"] if i["tag"] == _DEV_TAG]
    assert len(items) == 1
    assert items[0]["channel"] == "development"


def test_development_builds_are_sorted_newest_first(tmp_path):
    older = _dev_entry(tag=_DEV_TAG_2, created_at="2026-07-10T09:00:00Z")
    newer = _dev_entry(tag=_DEV_TAG, created_at="2026-07-14T09:00:00Z")
    manager = ReleaseManager(
        data_dir=tmp_path,
        urlopen=_opener(),
        development_source=_development_source([older, newer]),
    )
    dev_tags = [
        item["tag"]
        for item in manager.list_releases()["releases"]
        if item["channel"] == "development"
    ]
    assert dev_tags == [_DEV_TAG, _DEV_TAG_2]


def test_floating_development_aliases_are_excluded_from_catalogue(tmp_path):
    manager = ReleaseManager(
        data_dir=tmp_path,
        urlopen=_opener(),
        development_source=_development_source(
            [_dev_entry(tag=_DEV_FLOATING)]
        ),
    )
    assert all(
        item["channel"] != "development"
        for item in manager.list_releases()["releases"]
    )


def test_non_installable_development_builds_are_excluded_from_catalogue(tmp_path):
    manager = ReleaseManager(
        data_dir=tmp_path,
        urlopen=_opener(),
        development_source=_development_source(
            [_dev_entry(installable=False)]
        ),
    )
    assert all(
        item["channel"] != "development"
        for item in manager.list_releases()["releases"]
    )


def test_development_source_failure_never_breaks_the_catalogue(tmp_path):
    def broken():
        raise RuntimeError("registry unavailable")

    result = ReleaseManager(
        data_dir=tmp_path, urlopen=_opener(), development_source=broken
    ).list_releases()

    assert [item["tag"] for item in result["releases"]][:1] == ["latest"]
    assert all(item["channel"] != "development" for item in result["releases"])


# --- production development build catalogue -------------------------------

_CATALOGUE_TAG = _DEV_TAG
_CATALOGUE_REVISION = _DEV_REVISION
_ADMIN_REPO = "ghcr.io/basecubedev/ems-solarflow-admin"
_EMS_REPO = "ghcr.io/basecubedev/ems-solarflow-api-control"


def _catalogue_entry(tag=_CATALOGUE_TAG, **overrides):
    entry = {
        "tag": tag,
        "display_name": "MQTT device support",
        "channel": "development",
        "revision": _CATALOGUE_REVISION,
        "build_id": tag,
        "revision_short": _CATALOGUE_REVISION[:7],
        "run_id": "123456789",
        "run_attempt": 1,
        "created_at": "2026-07-14T09:00:00Z",
        "admin_image": f"{_ADMIN_REPO}:{tag}",
        "admin_digest": "sha256:" + "a" * 64,
        "ems_image": f"{_EMS_REPO}:{tag}",
        "ems_digest": "sha256:" + "b" * 64,
        "installable": True,
    }
    entry.update(overrides)
    return entry


def _write_catalogue(tmp_path, entries):
    path = tmp_path / "development-builds.json"
    path.write_text(json.dumps({"builds": entries}), encoding="utf-8")
    return path


def test_missing_catalogue_yields_empty_development_group(tmp_path):
    assert load_development_builds(tmp_path / "does-not-exist.json") == []


def test_invalid_catalogue_json_is_fail_closed(tmp_path):
    path = tmp_path / "development-builds.json"
    path.write_text("{ not valid json", encoding="utf-8")
    assert load_development_builds(path) == []


def test_catalogue_lists_installable_canonical_pair(tmp_path):
    path = _write_catalogue(tmp_path, [_catalogue_entry()])
    builds = load_development_builds(path)

    assert [b["tag"] for b in builds] == [_CATALOGUE_TAG]
    assert builds[0]["display_name"] == "MQTT device support"
    assert builds[0]["revision"] == _CATALOGUE_REVISION
    assert builds[0]["created_at"] == "2026-07-14T09:00:00Z"
    assert builds[0]["installable"] is True


def test_catalogue_excludes_floating_aliases(tmp_path):
    path = _write_catalogue(tmp_path, [_catalogue_entry(tag=_DEV_FLOATING)])
    assert load_development_builds(path) == []


def test_catalogue_excludes_incomplete_pairs(tmp_path):
    entry = _catalogue_entry()
    del entry["ems_image"]
    path = _write_catalogue(tmp_path, [entry])
    assert load_development_builds(path) == []


def test_catalogue_excludes_non_installable_builds(tmp_path):
    path = _write_catalogue(tmp_path, [_catalogue_entry(installable=False)])
    assert load_development_builds(path) == []


def test_catalogue_excludes_mismatched_image_repositories(tmp_path):
    path = _write_catalogue(
        tmp_path, [_catalogue_entry(admin_image=f"ghcr.io/evil/admin:{_CATALOGUE_TAG}")]
    )
    assert load_development_builds(path) == []


def test_catalogue_excludes_revision_mismatched_with_tag(tmp_path):
    # The immutable tag embeds the short revision; a build whose revision does not
    # match is a mislabelled pair and must not be offered.
    path = _write_catalogue(
        tmp_path, [_catalogue_entry(revision="0" * 40)]
    )
    assert load_development_builds(path) == []


@pytest.mark.parametrize(
    ("overrides"),
    (
        {"channel": "stable"},
        {"build_id": "another-build"},
        {"run_id": "999"},
        {"run_attempt": 2},
        {"admin_digest": "sha256:short"},
        {"ems_digest": None},
        {"created_at": "yesterday"},
    ),
)
def test_catalogue_rejects_incomplete_or_mismatched_identity(overrides, tmp_path):
    path = _write_catalogue(tmp_path, [_catalogue_entry(**overrides)])
    assert load_development_builds(path) == []


def test_catalogue_rejects_duplicate_canonical_tags(tmp_path):
    path = _write_catalogue(
        tmp_path,
        [_catalogue_entry(), _catalogue_entry(display_name="duplicate")],
    )
    assert load_development_builds(path) == []


def test_dynamic_catalogue_fetch_is_bounded_and_cached(tmp_path):
    payload = json.dumps({"builds": [_catalogue_entry()]}).encode()
    calls = []

    def opener(request, timeout):
        calls.append((request.full_url, timeout))
        return _Response(payload)

    cache = tmp_path / "development-catalogue-cache.json"
    builds = load_development_builds(
        "https://catalogue.example/development-builds.json",
        cache_path=cache,
        urlopen=opener,
    )

    assert [item["tag"] for item in builds] == [_CATALOGUE_TAG]
    assert calls == [("https://catalogue.example/development-builds.json", 3)]
    assert json.loads(cache.read_text(encoding="utf-8"))["builds"][0]["tag"] == _CATALOGUE_TAG


def test_catalogue_outage_uses_fresh_valid_cache(tmp_path):
    cache = _write_catalogue(tmp_path, [_catalogue_entry()])

    def unavailable(_request, timeout):
        assert timeout == 3
        raise urllib.error.URLError("offline")

    builds = load_development_builds(
        "https://catalogue.example/development-builds.json",
        cache_path=cache,
        urlopen=unavailable,
        now=lambda: datetime.now(timezone.utc),
    )
    assert [item["tag"] for item in builds] == [_CATALOGUE_TAG]


def test_catalogue_outage_rejects_stale_cache(tmp_path):
    cache = _write_catalogue(tmp_path, [_catalogue_entry()])
    stale = datetime.now(timezone.utc) - timedelta(days=3)
    os.utime(cache, (stale.timestamp(), stale.timestamp()))

    def unavailable(_request, timeout):
        raise urllib.error.URLError("offline")

    assert load_development_builds(
        "https://catalogue.example/development-builds.json",
        cache_path=cache,
        urlopen=unavailable,
        now=lambda: datetime.now(timezone.utc),
    ) == []


def test_oversized_remote_catalogue_is_fail_closed_without_cache_fallback(tmp_path):
    cache = _write_catalogue(tmp_path, [_catalogue_entry()])

    def oversized(_request, timeout):
        return _Response(b" " * (512 * 1024 + 1))

    assert load_development_builds(
        "https://catalogue.example/development-builds.json",
        cache_path=cache,
        urlopen=oversized,
    ) == []


def test_stable_admin_source_can_observe_a_later_catalogue_publication(tmp_path):
    catalogue = {"builds": []}

    def opener(_request, timeout):
        return _Response(json.dumps(catalogue).encode())

    source = development_catalogue_source(
        "https://catalogue.example/development-builds.json",
        cache_path=tmp_path / "cache.json",
        urlopen=opener,
    )
    assert source() == []

    catalogue["builds"].append(_catalogue_entry())
    assert [entry["tag"] for entry in source()] == [_CATALOGUE_TAG]


def test_catalogue_source_feeds_release_manager_without_fake_items(tmp_path):
    # The real loader (not an injected item list) surfaces a development build in
    # the catalogue, sorted after stable/latest and release candidates.
    catalogue = _write_catalogue(tmp_path, [_catalogue_entry()])
    manager = ReleaseManager(
        data_dir=tmp_path,
        urlopen=_opener(),
        development_source=development_catalogue_source(catalogue),
    )
    channels = [item["channel"] for item in manager.list_releases()["releases"]]
    dev_tags = [
        item["tag"]
        for item in manager.list_releases()["releases"]
        if item["channel"] == "development"
    ]

    assert dev_tags == [_CATALOGUE_TAG]
    non_dev = [i for i, c in enumerate(channels) if c in ("stable", "latest", "rc")]
    assert min(i for i, c in enumerate(channels) if c == "development") > max(non_dev)


def test_catalogue_source_preserves_exact_pair_metadata_in_release_item(tmp_path):
    entry = _catalogue_entry()
    catalogue = _write_catalogue(tmp_path, [entry])
    manager = ReleaseManager(
        data_dir=tmp_path,
        urlopen=_opener(),
        development_source=development_catalogue_source(catalogue),
    )

    item = next(
        release
        for release in manager.list_releases()["releases"]
        if release["tag"] == entry["tag"]
    )

    for field in (
        "build_id",
        "run_id",
        "run_attempt",
        "admin_image",
        "admin_digest",
        "ems_image",
        "ems_digest",
    ):
        assert item[field] == entry[field]

    descriptor = manager.development_build(entry["tag"])
    assert descriptor is not None
    assert descriptor["tag"] == entry["tag"]
    assert descriptor["admin_digest"] == entry["admin_digest"]
    assert descriptor["ems_digest"] == entry["ems_digest"]
    assert manager.development_build("dev-missing-aaaaaaa-1-1") is None


# --- mocked opener host matching -----------------------------------------


def test_github_api_url_matching_uses_parsed_host_not_substring():
    # Only the real API host counts; a look-alike path or subdomain must not.
    assert _is_github_api_url("https://api.github.com/repos/x/y/releases")
    assert not _is_github_api_url("https://evil.example/api.github.com/releases")
    assert not _is_github_api_url("https://api.github.com.evil.example/releases")


# --- installed release identity for a digest-pinned Compose ref ------------
#
# A digest-pinned Compose image (repository@sha256:…) carries no readable tag,
# so the installed release is recovered from authoritative OCI labels or a
# digest-matching known-good record — never derived from the digest text, and a
# prepared (downloaded, not installed) release never becomes the baseline.

_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64


def _digest_ref(digest):
    return f"{DOCKER_IMAGE}@{digest}"


def _labeled(digest, *, release_tag=None, version=None,
             build_id="v0.8.0-f7265fc", revision="f" * 40, channel="stable"):
    labels = {
        "org.opencontainers.image.revision": revision,
        "de.basecubedev.ems.build_id": build_id,
        "de.basecubedev.ems.channel": channel,
    }
    if version is not None:
        labels["org.opencontainers.image.version"] = version
    if release_tag is not None:
        labels["de.basecubedev.ems.release_tag"] = release_tag
    return {"digest": digest, "labels": labels}


def _digest_project(tmp_path, digest):
    project = tmp_path / "project"
    project.mkdir()
    (project / "docker-compose.yml").write_text(
        "services:\n  ems:\n"
        f"    image: {_digest_ref(digest)}\n"
        "    container_name: ems-solarflow-api-control\n",
        encoding="utf-8",
    )
    return project


class _StubKnownGood:
    def __init__(self, record=None):
        self._record = record

    def current(self):
        return dict(self._record) if self._record else None


def _digest_manager(tmp_path, *, compose_digest, docker, known_good=None):
    return ReleaseManager(
        data_dir=tmp_path / "data",
        project_dir=_digest_project(tmp_path, compose_digest),
        urlopen=_opener(),
        docker=docker,
        known_good=known_good,
    )


def test_digest_pinned_compose_active_release_from_compose_oci_release_tag(tmp_path):
    # EMS not running; the digest-pinned Compose image is locally inspectable.
    docker = _FakeDocker(
        container=None,
        images={_digest_ref(_DIGEST_A): _labeled(_DIGEST_A, release_tag="v0.8.0",
                                                 version="v0.8.0")},
    )
    manager = _digest_manager(tmp_path, compose_digest=_DIGEST_A, docker=docker)
    assert manager.detect_active_release() == "v0.8.0"


def test_running_container_release_tag_wins_over_stale_compose(tmp_path):
    # The Compose digest is stale; the running container's OCI release_tag wins.
    running_ref = _digest_ref(_DIGEST_A)
    docker = _FakeDocker(
        container={"container_name": "ems-solarflow-api-control",
                   "image": running_ref, "status": "running"},
        images={running_ref: _labeled(_DIGEST_A, release_tag="v0.9.0",
                                       version="v0.9.0")},
    )
    manager = _digest_manager(tmp_path, compose_digest=_DIGEST_B, docker=docker)
    assert manager.detect_active_release() == "v0.9.0"


def test_known_good_used_when_compose_digest_not_inspectable(tmp_path):
    # EMS stopped and the Compose digest cannot be inspected, but a known-good
    # record matches that exact digest.
    docker = _FakeDocker(container=None, images={})
    kg = _StubKnownGood(
        {"ems_digest": _DIGEST_A, "system_tag": "v0.8.0", "build_id": "v0.8.0-f7265fc"}
    )
    manager = _digest_manager(tmp_path, compose_digest=_DIGEST_A, docker=docker,
                              known_good=kg)
    assert manager.detect_active_release() == "v0.8.0"


def test_known_good_ignored_when_its_digest_differs_from_compose(tmp_path):
    docker = _FakeDocker(container=None, images={})
    kg = _StubKnownGood(
        {"ems_digest": _DIGEST_B, "system_tag": "v0.8.0", "build_id": "v0.8.0-f7265fc"}
    )
    manager = _digest_manager(tmp_path, compose_digest=_DIGEST_A, docker=docker,
                              known_good=kg)
    assert manager.detect_active_release() is None


def test_malformed_oci_release_tag_leaves_active_unknown(tmp_path):
    docker = _FakeDocker(
        container=None,
        images={_digest_ref(_DIGEST_A): _labeled(_DIGEST_A,
                                                 release_tag="not a tag!!",
                                                 version=None)},
    )
    manager = _digest_manager(tmp_path, compose_digest=_DIGEST_A, docker=docker)
    assert manager.detect_active_release() is None


class _NameImageDocker:
    def __init__(self, *, containers, image_id=None, images=None):
        self._containers = containers
        self._image_id = image_id
        self._images = images or {}

    def inspect_container(self, name):
        found = self._containers.get(name)
        return dict(found) if found else None

    def inspect_container_image_id(self, _name):
        return self._image_id

    def inspect_image(self, ref):
        found = self._images.get(ref)
        return dict(found) if found else None


def test_configured_ems_container_name_used_for_active_release(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_CONTAINER_NAME", "custom-ems")
    running_ref = _digest_ref(_DIGEST_A)
    docker = _NameImageDocker(
        containers={
            "custom-ems": {
                "container_name": "custom-ems",
                "image": running_ref,
                "status": "running",
            }
        },
        images={running_ref: _labeled(_DIGEST_A, release_tag="v0.8.0", version="v0.8.0")},
    )
    manager = _digest_manager(tmp_path, compose_digest=_DIGEST_B, docker=docker)
    assert manager.detect_active_release() == "v0.8.0"


def test_default_canonical_container_name_still_resolves(tmp_path, monkeypatch):
    monkeypatch.delenv("EMS_CONTAINER_NAME", raising=False)
    running_ref = _digest_ref(_DIGEST_A)
    docker = _NameImageDocker(
        containers={
            "ems-solarflow-api-control": {
                "container_name": "ems-solarflow-api-control",
                "image": running_ref,
                "status": "running",
            }
        },
        images={running_ref: _labeled(_DIGEST_A, release_tag="v0.8.0", version="v0.8.0")},
    )
    manager = _digest_manager(tmp_path, compose_digest=_DIGEST_B, docker=docker)
    assert manager.detect_active_release() == "v0.8.0"


def test_running_identity_and_active_release_use_same_immutable_image(tmp_path, monkeypatch):
    monkeypatch.delenv("EMS_CONTAINER_NAME", raising=False)
    running_tag_ref = _ref("v0.9.0")
    image_id = "sha256:" + "c" * 64
    docker = _NameImageDocker(
        containers={
            "ems-solarflow-api-control": {
                "container_name": "ems-solarflow-api-control",
                "image": running_tag_ref,
                "status": "running",
            }
        },
        image_id=image_id,
        images={
            image_id: _labeled(_DIGEST_A, release_tag="v0.8.0", version="v0.8.0"),
            running_tag_ref: _labeled(_DIGEST_B, release_tag="v0.9.0", version="v0.9.0"),
        },
    )
    manager = _digest_manager(tmp_path, compose_digest=_DIGEST_A, docker=docker)

    assert manager.detect_active_release() == "v0.8.0"
    running = manager._running_identity()
    assert running.release_tag == "v0.8.0"
    assert running.digest == _DIGEST_A


def _versioned_payload(*tags):
    return [
        {
            "tag_name": tag,
            "name": tag,
            "published_at": "2026-06-01T10:00:00Z",
            "prerelease": False,
            "draft": False,
            "zipball_url": f"https://example.test/{tag}.zip",
        }
        for tag in tags
    ]


def test_prepared_release_never_replaces_installed_digest_baseline(tmp_path):
    # Installed v0.8.0 by digest, v0.9.0 only prepared (downloaded, not installed).
    _write_cached(tmp_path / "data", "v0.9.0")
    docker = _FakeDocker(
        container=None,
        images={_digest_ref(_DIGEST_A): _labeled(_DIGEST_A, release_tag="v0.8.0",
                                                 version="v0.8.0")},
    )
    manager = ReleaseManager(
        data_dir=tmp_path / "data",
        project_dir=_digest_project(tmp_path, _DIGEST_A),
        urlopen=_opener(payload=_versioned_payload("v0.9.0", "v0.8.1", "v0.8.0")),
        resource_checker=lambda _ref: True,
        docker=docker,
    )
    result = manager.list_releases()
    by_tag = {item["tag"]: item for item in result["releases"]}

    assert result["active_release"] == "v0.8.0"
    assert by_tag["v0.8.0"]["active"] is True
    # The merely-prepared v0.9.0 is downloaded, not installed: never marked active.
    assert by_tag["v0.9.0"]["active"] is False
    assert result["prepared_release"] == "v0.9.0"
    # v0.8.1 is a forward move from the installed v0.8.0 — never a downgrade from
    # the merely-prepared v0.9.0.
    assert by_tag["v0.8.1"]["upgrade_state"] != "downgrade_blocked"
    assert by_tag["v0.8.1"]["selectable"] is True


# --- two products, one release list ------------------------------------------


def _appliance_entries(count, *, start=1):
    """What a weekly image build leaves behind, newest first."""

    return [
        {
            "tag_name": f"appliance-image-ci-{number}",
            "name": f"Appliance image 0.1.0 (CI build {number}, unsigned)",
            "published_at": "2026-08-27T21:00:00Z",
            "prerelease": True,
            "draft": False,
        }
        for number in range(start + count - 1, start - 1, -1)
    ]


def _paged_opener(pages):
    """An opener that answers /releases the way GitHub does: one page at a time."""

    def open_url(request, timeout=None):
        url = request.full_url
        if _is_github_api_url(url) and "/releases" in url:
            number = int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
            page = pages[number - 1] if number <= len(pages) else []
            return _Response(json.dumps(page).encode())
        return _opener()(request, timeout)

    return open_url


def test_an_appliance_release_is_not_offered_as_an_ems_system_build(tmp_path):
    """The appliance publishes an image build and a Manager release from this
    same repository, tagged outside the ``v*`` namespace on purpose. Offering
    one would mean offering a build whose container images do not exist."""

    payload = _appliance_entries(3) + _github_payload()
    manager = ReleaseManager(data_dir=tmp_path, urlopen=_opener(payload=payload))

    tags = {item["tag"] for item in manager.list_releases()["releases"]}

    assert not any(tag.startswith("appliance-") for tag in tags), tags
    assert "v0.6.1" in tags


def test_a_year_of_weekly_image_builds_does_not_hide_every_ems_release(tmp_path):
    """The failure this pagination exists for.

    One fixed page of 50 and a weekly image build meant no ``v*`` release was
    inside the window in under a year. ``latest_stable`` then became None and
    the console fell back to the rolling ``latest`` channel with no warning,
    because the fetch itself had succeeded.
    """

    pages = [_appliance_entries(100), _appliance_entries(52, start=101) + _github_payload()]
    manager = ReleaseManager(data_dir=tmp_path, urlopen=_paged_opener(pages))

    result = manager.list_releases()
    tags = {item["tag"] for item in result["releases"]}

    assert "v0.6.1" in tags, "an EMS release fell out of reach behind appliance builds"
    assert result["latest_stable"], "no stable release could be resolved"


def test_one_oversized_page_does_not_take_the_whole_catalogue_down(tmp_path):
    """A release with assets is ~40 KB of JSON and the appliance publishes a
    nineteen-asset image build every week, so a page can outgrow the 2 MiB this
    will read. That used to raise out of the whole fetch: the console reported
    "GitHub releases are unavailable" and fell back to the rolling latest
    channel, for a repository that was answering perfectly well.
    """

    heavy = _appliance_entries(1)
    heavy[0]["body"] = "x" * (2 * 1024 * 1024 + 64)
    pages = [heavy, _github_payload()]
    manager = ReleaseManager(data_dir=tmp_path, urlopen=_paged_opener(pages))

    result = manager.list_releases()
    tags = {item["tag"] for item in result["releases"]}

    assert "v0.6.1" in tags, "one unreadable page emptied the catalogue"
    assert result["latest_stable"]
    assert any("could not be read" in warning for warning in result.get("warnings", [])), (
        "a page was silently dropped; a missing release must not look like an absent one"
    )


def test_the_window_did_not_shrink_when_the_pages_got_smaller(tmp_path):
    """Smaller pages are only safe if there are correspondingly more of them:
    the point of paginating at all was that a year of weekly image builds must
    not push every EMS release out of reach.
    """

    from admin.releases import (
        GITHUB_RELEASE_PAGES,
        GITHUB_RELEASES_PER_PAGE,
        GITHUB_RELEASES_URL,
    )

    assert GITHUB_RELEASES_PER_PAGE * GITHUB_RELEASE_PAGES >= 500
    assert f"per_page={GITHUB_RELEASES_PER_PAGE}" in GITHUB_RELEASES_URL


def test_the_paging_stops_at_the_end_rather_than_asking_forever(tmp_path):
    """A short page is the last page. Asking anyway spends a request per refresh
    on a repository that will never answer with anything."""

    asked = []

    def counting(request, timeout=None):
        url = request.full_url
        if _is_github_api_url(url) and "/releases" in url:
            asked.append(url)
            return _Response(json.dumps(_github_payload()).encode())
        return _opener()(request, timeout)

    ReleaseManager(data_dir=tmp_path, urlopen=counting).list_releases()

    assert len(asked) == 1, asked
