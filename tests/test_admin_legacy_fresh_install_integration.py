# SPDX-License-Identifier: AGPL-3.0-or-later
"""Service-level integration for a legacy v0.7.0 Fresh Install on a modern Admin.

This crosses the real Admin service boundaries — the real
:class:`SystemAlignmentService`, :class:`PendingTransitionStore`,
:class:`KnownGoodStore`, :class:`ReleaseManager` and the release-archive resource
preparer — mocking only the two external effects: Docker image inspection (the
resolver) and network (the release download / GitHub metadata). It proves the
whole golden path from validation through resource preparation, discovery
authorization and completion, plus the resource-verification failure path.
"""

import io
import json
import zipfile
from urllib.parse import urlparse

import pytest

from admin.admin_update import ADMIN_IMAGE_REPO, EMS_IMAGE_REPO, PendingTransitionStore
from admin.embedded_resources import EmbeddedReleaseResources, ReleaseArchiveResources
from admin.image_identity import ImageIdentity
from admin.known_good import KnownGoodStore
from admin.releases import ReleaseManager
from admin.system_alignment import SystemAlignmentError, SystemAlignmentService
from admin.system_build import SystemBuild

pytestmark = [
    pytest.mark.admin,
    pytest.mark.setup,
    pytest.mark.e2e,
    pytest.mark.simulation,
    pytest.mark.system_build,
]

LEGACY_TAG = "v0.7.0"
LEGACY_REVISION = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
LEGACY_BUILD_ID = "123456789-1"
MODERN_REVISION = "f7265fc747c2223f126f0ee7801e030c6226edf4"


def _legacy_build():
    return SystemBuild(
        requested_tag=LEGACY_TAG, canonical_tag=LEGACY_TAG, channel="stable",
        revision=LEGACY_REVISION, build_id=LEGACY_BUILD_ID,
        admin_image=f"{ADMIN_IMAGE_REPO}:{LEGACY_TAG}", admin_digest="sha256:v070-admin",
        ems_image=f"{EMS_IMAGE_REPO}:{LEGACY_TAG}", ems_digest="sha256:v070-ems",
        release_tag=LEGACY_TAG,
    )


def _modern_admin_identity():
    return ImageIdentity(
        image_ref=f"{ADMIN_IMAGE_REPO}:v0.9.0", digest="sha256:modern-admin",
        version_label="v0.9.0", revision=MODERN_REVISION, channel="stable",
        build_id="v0.9.0-f7265fc", release_tag="v0.9.0",
    )


def _running_v070_ems():
    return ImageIdentity(
        image_ref=f"{EMS_IMAGE_REPO}:{LEGACY_TAG}", digest="sha256:v070-ems",
        version_label=LEGACY_TAG, revision=LEGACY_REVISION, channel="stable",
        build_id=LEGACY_BUILD_ID, release_tag=LEGACY_TAG,
    )


class _Resolver:
    """Mocks the Docker image-inspection boundary only."""

    def __init__(self, build):
        self._build = build

    def resolve(self, requested_tag):
        assert requested_tag == self._build.canonical_tag
        return self._build


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _release_archive():
    files = {
        f"{LEGACY_TAG}/config.template.json": b'{"devices": [], "legacy": true}\n',
        f"{LEGACY_TAG}/docker-compose.example.yml": b"services: {}\n",
        f"{LEGACY_TAG}/install-docker.sh": b"#!/bin/sh\n",
        f"{LEGACY_TAG}/install-docker.ps1": b"Write-Host install\n",
        f"{LEGACY_TAG}/deploy/docker/compose.influxdb.yml": b"services: {}\n",
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as handle:
        for name, value in files.items():
            handle.writestr(name, value)
    return output.getvalue()


def _opener(*, download_fails=False):
    def open_url(request, timeout=None):
        url = request.full_url
        if "/git/trees/" in url:
            paths = [
                "config.template.json",
                "docker-compose.example.yml",
                "install-docker.sh",
                "install-docker.ps1",
                "deploy/docker/compose.influxdb.yml",
            ]
            return _Response(
                json.dumps(
                    {"tree": [{"path": path, "type": "blob"} for path in paths]}
                ).encode()
            )
        if "/commits/" in url:
            return _Response(json.dumps({"sha": LEGACY_REVISION}).encode())
        if urlparse(url).hostname == "api.github.com":
            payload = [
                {
                    "tag_name": LEGACY_TAG,
                    "name": LEGACY_TAG,
                    "published_at": "2026-01-01T10:00:00Z",
                    "prerelease": False,
                    "draft": False,
                    "zipball_url": "https://example.test/v0.7.0.zip",
                }
            ]
            return _Response(json.dumps(payload).encode())
        if download_fails:
            import urllib.error

            raise urllib.error.URLError("network down")
        return _Response(_release_archive())

    return open_url


def _service(tmp_path, *, download_fails=False):
    data_dir = tmp_path / "admin"
    release_manager = ReleaseManager(
        data_dir=data_dir, urlopen=_opener(download_fails=download_fails)
    )
    state_dir = data_dir / "state"
    transitions = PendingTransitionStore(state_dir)
    known_good = KnownGoodStore(state_dir)
    service = SystemAlignmentService(
        resolver=_Resolver(_legacy_build()),
        transition_store=transitions,
        embedded_resources=EmbeddedReleaseResources(release_manager=release_manager),
        release_archive_resources=ReleaseArchiveResources(
            release_manager=release_manager
        ),
        known_good_store=known_good,
        current_identity=_modern_admin_identity,
        current_ems_identity=_running_v070_ems,
        persistent_ref=lambda: f"{ADMIN_IMAGE_REPO}:v0.9.0",
        launcher=lambda record: None,
    )
    return service, transitions, known_good, release_manager


def test_legacy_v070_fresh_install_golden_path(tmp_path):
    service, transitions, known_good, release_manager = _service(tmp_path)

    # 04 Validate: Continue open, Admin update not required, embedded n/a.
    validated = service.validate(requested_tag=LEGACY_TAG)
    assert validated["compatibility_mode"] == "legacy_release"
    assert validated["resource_strategy"] == "release_archive"
    assert validated["admin_update_required"] is False
    assert validated["embedded_resources_valid"] is None
    assert validated["next_allowed"] is True
    assert validated["confirmation_allowed"] is True

    # 07-08 Confirm + prepare the exact v0.7.0 release resources.
    confirmed = service.confirm_setup_build(
        requested_tag=LEGACY_TAG, mode="fresh_install"
    )
    operation_id = confirmed["operation_id"]
    assert confirmed["resources_verified"] is True

    # 08 The resources were prepared from the exact release into the cache, and
    # the config template is loadable from that historical release (not main).
    prepared = release_manager.prepared_release()
    assert prepared == LEGACY_TAG
    template = release_manager.config_template()
    assert template["tag"] == LEGACY_TAG
    assert template["template"] == {"devices": [], "legacy": True}

    # 09 The transition separates the modern orchestrator Admin from the EMS build.
    record = transitions.read()
    assert record.stage == "resources_verified"
    assert record.compatibility_mode == "legacy_release"
    assert record.orchestrator_admin["digest"] == "sha256:modern-admin"
    assert record.selected_ems_build["digest"] == "sha256:v070-ems"

    # 10 Discovery is authorised against the running modern orchestrator Admin.
    authorized = service.validate_setup_discovery_operation(operation_id=operation_id)
    assert authorized["system_tag"] == LEGACY_TAG

    # 11-13 Deploy EMS and pass health checks (Docker execution mocked away).
    service.begin_ems_operation(operation_id=operation_id)
    assert service.claim_ems_operation(operation_id=operation_id) is True
    service.finish_ems_operation(operation_id=operation_id, succeeded=True)
    completed = service.finish_healthcheck(operation_id=operation_id, passed=True)
    assert completed["status"] == "completed"

    # 14-15 Known-good records the modern Admin plus v0.7.0 EMS correctly.
    kg = known_good.current()
    assert kg["system_tag"] == LEGACY_TAG
    assert kg["ems_digest"] == "sha256:v070-ems"
    assert kg["admin_digest"] == "sha256:modern-admin"
    assert kg["admin_digest"] != _legacy_build().admin_digest
    assert kg["compatibility_mode"] == "legacy_release"


def test_legacy_release_download_failure_fails_before_any_mutation(tmp_path):
    service, transitions, known_good, release_manager = _service(
        tmp_path, download_fails=True
    )

    validated = service.validate(requested_tag=LEGACY_TAG)
    assert validated["next_allowed"] is True

    with pytest.raises(SystemAlignmentError) as excinfo:
        service.confirm_setup_build(requested_tag=LEGACY_TAG, mode="fresh_install")
    assert excinfo.value.code == "system_build_resources_invalid"

    # The transition fails closed before resources verify, so no config template
    # is prepared, no discovery is authorised, and no known-good is written.
    record = transitions.read()
    assert record.stage == "failed_recoverable"
    assert release_manager.prepared_release() is None
    assert known_good.current() is None
    with pytest.raises(SystemAlignmentError):
        service.validate_setup_discovery_operation(operation_id=record.operation_id)


def test_legacy_discovery_blocked_until_resources_prepared(tmp_path):
    # Discovery can never be authorised straight from a browser selection: it
    # requires the confirmed, resource-verified, persisted transition.
    service, transitions, known_good, release_manager = _service(tmp_path)
    service.validate(requested_tag=LEGACY_TAG)
    assert transitions.read() is None
    with pytest.raises(SystemAlignmentError) as excinfo:
        service.validate_setup_discovery_operation(operation_id="not-a-real-op")
    assert excinfo.value.code in {"no_transition", "setup_operation_required"}
