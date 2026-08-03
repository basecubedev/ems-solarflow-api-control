# SPDX-License-Identifier: AGPL-3.0-or-later
"""Embedded Admin-image Setup resources, cryptographically bound to the build.

The Admin image ships ``/app/release-resources/`` with the Setup resources plus a
``system-build.json`` and a ``resource-manifest.json``. This service verifies the
bundle against the running Admin build and every file hash, then imports it into
the existing ReleaseManager cache so Setup works with no GitHub access. A cache
is trusted only when its manifest matches the canonical tag, revision, build id
and file hashes — never because a tag directory happens to exist.
"""

import json

import pytest

from admin.embedded_resources import (
    EmbeddedReleaseResources,
    EmbeddedResourcesError,
    ReleaseArchiveResources,
    sha256_bytes,
    write_release_resources,
)
from admin.releases import ReleaseManager

pytestmark = [
    pytest.mark.admin,
    pytest.mark.contract,
    pytest.mark.simulation,
]


REVISION = "f7265fc747c2223f126f0ee7801e030c6226edf4"
BUILD_ID = "v0.8.0-f7265fc"
SYSTEM_TAG = "v0.8.0"
CHANNEL = "stable"
RELEASE_TAG = SYSTEM_TAG
ADMIN_IMAGE = "ghcr.io/basecubedev/ems-solarflow-admin:v0.8.0"
EMS_IMAGE = "ghcr.io/basecubedev/ems-solarflow-api-control:v0.8.0"

TEMPLATE_BYTES = json.dumps({"schema": 1, "devices": []}).encode("utf-8")


def _running(
    revision=REVISION,
    build_id=BUILD_ID,
    *,
    tag=SYSTEM_TAG,
    channel=CHANNEL,
    release_tag=None,
    admin_image=None,
    ems_image=None,
):
    """Complete non-recursive identity expected from the embedded bundle."""

    return {
        "system_tag": tag,
        "canonical_tag": tag,
        "channel": channel,
        "revision": revision,
        "build_id": build_id,
        "release_tag": tag if release_tag is None else release_tag,
        "admin_image": admin_image
        or f"ghcr.io/basecubedev/ems-solarflow-admin:{tag}",
        "ems_image": ems_image
        or f"ghcr.io/basecubedev/ems-solarflow-api-control:{tag}",
    }


def test_legacy_archive_requires_an_explicit_ems_image_revision():
    class _Manager:
        def prepare(self, *_args, **_kwargs):
            raise AssertionError("unverified legacy resources must not be prepared")

    service = ReleaseArchiveResources(release_manager=_Manager())
    build = _running(revision=None, build_id="123456789-1", tag="v0.7.0")

    with pytest.raises(EmbeddedResourcesError, match="revision.*unverified"):
        service.import_into_cache(running_build=build)


def _write_bundle(
    root,
    *,
    tag=SYSTEM_TAG,
    channel=CHANNEL,
    revision=REVISION,
    build_id=BUILD_ID,
    release_tag=None,
    admin_image=None,
    ems_image=None,
):
    """Write a valid embedded resource bundle and return its directory."""

    release_tag = tag if release_tag is None else release_tag
    admin_image = admin_image or f"ghcr.io/basecubedev/ems-solarflow-admin:{tag}"
    ems_image = ems_image or f"ghcr.io/basecubedev/ems-solarflow-api-control:{tag}"

    files = {
        "config.template.json": TEMPLATE_BYTES,
        "docker-compose.example.yml": b"services:\n  ems:\n    image: x\n",
        "install-docker.sh": b"#!/bin/sh\necho install\n",
        "install-docker.ps1": b"Write-Host install\n",
        "deploy/docker/compose.influxdb.yml": b"services: {}\n",
    }
    for rel, content in files.items():
        path = root.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    manifest = {
        "format_version": 1,
        "system_tag": tag,
        "channel": channel,
        "build_id": build_id,
        "revision": revision,
        "release_tag": release_tag,
        "admin_image": admin_image,
        "ems_image": ems_image,
        "files": {rel: sha256_bytes(content) for rel, content in files.items()},
    }
    (root / "resource-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    system_build = {
        "format_version": 1,
        "system_tag": tag,
        "channel": channel,
        "revision": revision,
        "build_id": build_id,
        "release_tag": release_tag,
        "admin_image": admin_image,
        "ems_image": ems_image,
    }
    (root / "system-build.json").write_text(json.dumps(system_build), encoding="utf-8")
    return root


def _service(tmp_path, resources_dir):
    manager = ReleaseManager(data_dir=str(tmp_path / "admin"))
    return EmbeddedReleaseResources(resources_dir=resources_dir, release_manager=manager), manager


# --- happy path: verify + import ------------------------------------------


def test_valid_embedded_resources_import(tmp_path):
    bundle = _write_bundle(tmp_path / "res")
    service, manager = _service(tmp_path, bundle)
    tag = service.import_into_cache(running_build=_running())
    assert tag == SYSTEM_TAG
    # Imported into the ReleaseManager cache: config_template works with no network.
    prepared = manager.config_template()
    assert prepared["tag"] == SYSTEM_TAG
    assert prepared["template"] == {"schema": 1, "devices": []}


def test_setup_works_without_github_after_import(tmp_path):
    bundle = _write_bundle(tmp_path / "res")
    service, manager = _service(tmp_path, bundle)
    service.import_into_cache(running_build=_running())
    # The prepared release is discoverable purely from the local cache.
    assert manager.prepared_release() == SYSTEM_TAG


# --- verification failures -> system_build_resources_invalid --------------


def test_missing_system_build_json_rejected(tmp_path):
    bundle = _write_bundle(tmp_path / "res")
    (bundle / "system-build.json").unlink()
    service, _ = _service(tmp_path, bundle)
    with pytest.raises(EmbeddedResourcesError) as exc:
        service.import_into_cache(running_build=_running())
    assert exc.value.code == "system_build_resources_invalid"


def test_missing_required_file_rejected(tmp_path):
    bundle = _write_bundle(tmp_path / "res")
    (bundle / "install-docker.sh").unlink()
    service, _ = _service(tmp_path, bundle)
    with pytest.raises(EmbeddedResourcesError) as exc:
        service.import_into_cache(running_build=_running())
    assert exc.value.code == "system_build_resources_invalid"


def test_missing_deployment_resource_tree_rejected(tmp_path):
    bundle = _write_bundle(tmp_path / "res")
    deploy_file = bundle / "deploy" / "docker" / "compose.influxdb.yml"
    deploy_file.unlink()
    manifest_path = bundle / "resource-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["files"]["deploy/docker/compose.influxdb.yml"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    service, _ = _service(tmp_path, bundle)

    with pytest.raises(EmbeddedResourcesError) as exc:
        service.import_into_cache(running_build=_running())

    assert exc.value.code == "system_build_resources_invalid"


def test_invalid_file_hash_rejected(tmp_path):
    bundle = _write_bundle(tmp_path / "res")
    (bundle / "install-docker.sh").write_bytes(b"#!/bin/sh\nrm -rf /\n")  # tampered
    service, _ = _service(tmp_path, bundle)
    with pytest.raises(EmbeddedResourcesError) as exc:
        service.import_into_cache(running_build=_running())
    assert exc.value.code == "system_build_resources_invalid"


def test_revision_mismatch_rejected(tmp_path):
    bundle = _write_bundle(tmp_path / "res")
    service, _ = _service(tmp_path, bundle)
    with pytest.raises(EmbeddedResourcesError) as exc:
        service.import_into_cache(running_build=_running(revision="0000000other"))
    assert exc.value.code == "system_build_resources_invalid"


def test_build_id_mismatch_rejected(tmp_path):
    bundle = _write_bundle(tmp_path / "res")
    service, _ = _service(tmp_path, bundle)
    with pytest.raises(EmbeddedResourcesError) as exc:
        service.import_into_cache(running_build=_running(build_id="v0.7.0-deadbee"))
    assert exc.value.code == "system_build_resources_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("system_tag", "v0.8.1"),
        ("channel", "rc"),
        ("release_tag", "v0.8.1"),
        (
            "admin_image",
            "ghcr.io/basecubedev/ems-solarflow-admin:v0.8.1",
        ),
        (
            "ems_image",
            "ghcr.io/basecubedev/ems-solarflow-api-control:v0.8.1",
        ),
    ),
)
def test_complete_system_build_identity_is_bound_to_expected_pair(
    tmp_path, field, value
):
    """Equal revision/build_id must not hide a different paired build."""

    bundle = _write_bundle(tmp_path / "res")
    descriptor_path = bundle / "system-build.json"
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    descriptor[field] = value
    descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")

    service, _ = _service(tmp_path, bundle)
    with pytest.raises(EmbeddedResourcesError) as exc:
        service.import_into_cache(running_build=_running())
    assert exc.value.code == "system_build_resources_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("system_tag", "v0.8.1"),
        ("channel", "rc"),
        ("release_tag", "v0.8.1"),
        (
            "admin_image",
            "ghcr.io/basecubedev/ems-solarflow-admin:v0.8.1",
        ),
        (
            "ems_image",
            "ghcr.io/basecubedev/ems-solarflow-api-control:v0.8.1",
        ),
    ),
)
def test_resource_manifest_is_bound_to_complete_system_build_identity(
    tmp_path, field, value
):
    bundle = _write_bundle(tmp_path / "res")
    manifest_path = bundle / "resource-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    service, _ = _service(tmp_path, bundle)
    with pytest.raises(EmbeddedResourcesError) as exc:
        service.import_into_cache(running_build=_running())
    assert exc.value.code == "system_build_resources_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("system_tag", "v0.8.1"),
        ("channel", "rc"),
        ("release_tag", "v0.8.1"),
        (
            "admin_image",
            "ghcr.io/basecubedev/ems-solarflow-admin:v0.8.1",
        ),
        (
            "ems_image",
            "ghcr.io/basecubedev/ems-solarflow-api-control:v0.8.1",
        ),
    ),
)
def test_matching_descriptors_cannot_override_expected_system_build(
    tmp_path, field, value
):
    """Both bundle descriptors may agree and still describe the wrong target."""

    bundle = _write_bundle(tmp_path / "res")
    for name in ("system-build.json", "resource-manifest.json"):
        path = bundle / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload[field] = value
        path.write_text(json.dumps(payload), encoding="utf-8")

    service, _ = _service(tmp_path, bundle)
    with pytest.raises(EmbeddedResourcesError) as exc:
        service.import_into_cache(running_build=_running())
    assert exc.value.code == "system_build_resources_invalid"


@pytest.mark.parametrize(
    "field",
    ("system_tag", "channel", "release_tag", "admin_image", "ems_image"),
)
def test_resource_manifest_requires_every_system_build_identity_field(
    tmp_path, field
):
    bundle = _write_bundle(tmp_path / "res")
    manifest_path = bundle / "resource-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop(field)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    service, _ = _service(tmp_path, bundle)
    with pytest.raises(EmbeddedResourcesError) as exc:
        service.import_into_cache(running_build=_running())
    assert exc.value.code == "system_build_resources_invalid"


def test_path_traversal_in_manifest_rejected(tmp_path):
    bundle = _write_bundle(tmp_path / "res")
    manifest = json.loads((bundle / "resource-manifest.json").read_text())
    manifest["files"]["../../etc/passwd"] = sha256_bytes(b"x")
    (bundle / "resource-manifest.json").write_text(json.dumps(manifest))
    service, _ = _service(tmp_path, bundle)
    with pytest.raises(EmbeddedResourcesError) as exc:
        service.import_into_cache(running_build=_running())
    assert exc.value.code == "system_build_resources_invalid"


def test_symlink_escape_rejected(tmp_path):
    bundle = _write_bundle(tmp_path / "res")
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"top secret\n")
    link = bundle / "config.template.json"
    link.unlink()
    link.symlink_to(secret)
    # Point the manifest hash at the symlink target's content so only the
    # symlink-escape guard (not the hash check) can reject it.
    manifest = json.loads((bundle / "resource-manifest.json").read_text())
    manifest["files"]["config.template.json"] = sha256_bytes(b"top secret\n")
    (bundle / "resource-manifest.json").write_text(json.dumps(manifest))
    service, _ = _service(tmp_path, bundle)
    with pytest.raises(EmbeddedResourcesError) as exc:
        service.import_into_cache(running_build=_running())
    assert exc.value.code == "system_build_resources_invalid"


# --- cache identity binding (stale caches rejected) -----------------------


def test_cache_from_older_latest_build_rejected(tmp_path):
    # A cached "latest" from a previous build must not be trusted for a new one.
    bundle = _write_bundle(
        tmp_path / "res", tag="latest", channel="latest",
        revision="a" * 40, build_id="latest-aaaaaaa",
    )
    service, manager = _service(tmp_path, bundle)
    service.import_into_cache(
        running_build=_running(
            revision="a" * 40,
            build_id="latest-aaaaaaa",
            tag="latest",
            channel="latest",
        )
    )
    # Now the running Admin is a different (newer) build; the stale cache is invalid.
    stale = manager._cached_manifests().get("latest")
    assert stale is not None
    assert service.is_cache_valid_for_build(stale, running_build=_running()) is False


def test_cache_from_other_dev_run_rejected(tmp_path):
    dev_tag = "dev-feature-x-aaaaaaa-111-1"
    bundle = _write_bundle(
        tmp_path / "res", tag=dev_tag, channel="development",
        revision="a" * 40, build_id=dev_tag,
    )
    service, manager = _service(tmp_path, bundle)
    service.import_into_cache(
        running_build=_running(
            revision="a" * 40,
            build_id=dev_tag,
            tag=dev_tag,
            channel="development",
        )
    )
    cached = manager._cached_manifests().get(dev_tag)
    # A different dev run (different build id) must not reuse this cache.
    assert service.is_cache_valid_for_build(
        cached,
        running_build=_running(
            revision="b" * 40,
            build_id="dev-feature-x-bbbbbbb-222-1",
            tag="dev-feature-x-bbbbbbb-222-1",
            channel="development",
        ),
    ) is False


def test_partial_cache_not_treated_as_complete(tmp_path):
    bundle = _write_bundle(tmp_path / "res")
    service, manager = _service(tmp_path, bundle)
    service.import_into_cache(running_build=_running())
    # Corrupt the cache: remove a required file. It must no longer verify.
    (manager.releases_dir / SYSTEM_TAG / "install-docker.sh").unlink()
    manifest = json.loads(
        (manager.releases_dir / SYSTEM_TAG / "manifest.json").read_text()
    )
    assert service.is_cache_valid_for_build(manifest, running_build=_running()) is False


# --- generator round-trip -------------------------------------------------


def test_generator_produces_verifiable_bundle(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.template.json").write_bytes(TEMPLATE_BYTES)
    (source / "docker-compose.example.yml").write_bytes(b"services: {}\n")
    (source / "install-docker.sh").write_bytes(b"#!/bin/sh\n")
    (source / "install-docker.ps1").write_bytes(b"Write-Host x\n")
    deploy = source / "deploy" / "docker"
    deploy.mkdir(parents=True)
    (deploy / "compose.influxdb.yml").write_bytes(b"services: {}\n")

    out = tmp_path / "release-resources"
    write_release_resources(
        out,
        source_root=source,
        system_tag=SYSTEM_TAG,
        channel="stable",
        revision=REVISION,
        build_id=BUILD_ID,
        release_tag=RELEASE_TAG,
        admin_image=ADMIN_IMAGE,
        ems_image=EMS_IMAGE,
    )
    assert (out / "system-build.json").is_file()
    assert (out / "resource-manifest.json").is_file()

    system_build = json.loads(
        (out / "system-build.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (out / "resource-manifest.json").read_text(encoding="utf-8")
    )
    expected_identity = {
        "system_tag": SYSTEM_TAG,
        "channel": CHANNEL,
        "revision": REVISION,
        "build_id": BUILD_ID,
        "release_tag": RELEASE_TAG,
        "admin_image": ADMIN_IMAGE,
        "ems_image": EMS_IMAGE,
    }
    for field, expected in expected_identity.items():
        assert system_build[field] == expected
        assert manifest[field] == expected

    service, manager = _service(tmp_path, out)
    tag = service.import_into_cache(running_build=_running())
    assert tag == SYSTEM_TAG
    assert manager.config_template()["template"] == {"schema": 1, "devices": []}


def test_generator_rejects_source_without_deployment_resources(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.template.json").write_bytes(TEMPLATE_BYTES)
    (source / "docker-compose.example.yml").write_bytes(b"services: {}\n")
    (source / "install-docker.sh").write_bytes(b"#!/bin/sh\n")
    (source / "install-docker.ps1").write_bytes(b"Write-Host x\n")

    with pytest.raises(EmbeddedResourcesError) as exc:
        write_release_resources(
            tmp_path / "release-resources",
            source_root=source,
            system_tag=SYSTEM_TAG,
            channel=CHANNEL,
            revision=REVISION,
            build_id=BUILD_ID,
            release_tag=RELEASE_TAG,
            admin_image=ADMIN_IMAGE,
            ems_image=EMS_IMAGE,
        )

    assert exc.value.code == "system_build_resources_invalid"


@pytest.mark.parametrize(
    "override",
    (
        {"revision": "not-a-full-git-sha"},
        {"channel": "development"},
        {"release_tag": "v0.7.0"},
        {"admin_image": "ghcr.io/example/other:v0.8.0"},
        {"ems_image": "ghcr.io/example/other:v0.8.0"},
    ),
)
def test_generator_rejects_inconsistent_complete_identity(tmp_path, override):
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.template.json").write_bytes(TEMPLATE_BYTES)
    (source / "docker-compose.example.yml").write_bytes(b"services: {}\n")
    (source / "install-docker.sh").write_bytes(b"#!/bin/sh\n")
    (source / "install-docker.ps1").write_bytes(b"Write-Host x\n")
    deploy = source / "deploy" / "docker"
    deploy.mkdir(parents=True)
    (deploy / "compose.influxdb.yml").write_bytes(b"services: {}\n")
    identity = {
        "system_tag": SYSTEM_TAG,
        "channel": CHANNEL,
        "revision": REVISION,
        "build_id": BUILD_ID,
        "release_tag": RELEASE_TAG,
        "admin_image": ADMIN_IMAGE,
        "ems_image": EMS_IMAGE,
    }
    identity.update(override)

    with pytest.raises(EmbeddedResourcesError) as exc:
        write_release_resources(
            tmp_path / "release-resources",
            source_root=source,
            **identity,
        )

    assert exc.value.code == "system_build_resources_invalid"
