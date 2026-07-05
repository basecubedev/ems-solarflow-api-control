# SPDX-License-Identifier: AGPL-3.0-or-later
"""Release discovery and setup-resource cache tests."""

import io
import json
import urllib.error
import zipfile

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
        if "api.github.com" in url:
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
    assert [item["tag"] for item in result["releases"]] == [
        "v0.6.1",
        "v0.6.0",
        "latest",
        "v0.6.1-rc1",
        "v0.5.9",
    ]
    assert result["releases"][0]["stable"] is True
    by_tag = {item["tag"]: item for item in result["releases"]}
    assert by_tag["v0.6.1-rc1"]["prerelease"] is True
    assert by_tag["v0.6.1-rc1"]["channel"] == "rc"
    assert by_tag["v0.6.1-rc1"]["admin_supported"] is True
    assert by_tag["latest"]["stable"] is False
    assert by_tag["latest"]["selectable"] is True
    assert by_tag["v0.5.9"]["admin_supported"] is False
    assert by_tag["v0.5.9"]["selectable"] is False


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

    assert result["docker_image"].startswith("ghcr.io/")
    assert all("ghcr.io" not in url for url in urls)


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

    assert all(
        item["selectable"]
        for item in result["releases"]
        if item["tag"] != "v0.5.9"
    )
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
