# SPDX-License-Identifier: AGPL-3.0-or-later
"""Release discovery and setup-resource cache tests."""

import io
import json
import urllib.error
import zipfile
from urllib.parse import urlparse

import pytest

from admin.releases import REPO, ReleaseError, ReleaseManager

pytestmark = pytest.mark.simulation


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


def _opener(archive=None):
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
            return _Response(json.dumps(_github_payload()).encode())
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


# --- mocked opener host matching -----------------------------------------


def test_github_api_url_matching_uses_parsed_host_not_substring():
    # Only the real API host counts; a look-alike path or subdomain must not.
    assert _is_github_api_url("https://api.github.com/repos/x/y/releases")
    assert not _is_github_api_url("https://evil.example/api.github.com/releases")
    assert not _is_github_api_url("https://api.github.com.evil.example/releases")
