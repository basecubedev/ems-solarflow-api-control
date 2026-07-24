# SPDX-License-Identifier: AGPL-3.0-or-later
"""Finalization contract for the actually running EMS System Build.

A successful Compose job and a reachable dashboard are necessary but not
sufficient: the container answering that health check can still be the previous
EMS image. Known-good may be written only after the running container identity
matches the target persisted in the active transition.
"""

import pytest

from admin.admin_update import PendingTransitionStore
from admin.image_identity import ImageIdentity
from admin.known_good import KnownGoodStore
from admin.server import _running_ems_identity
from admin.system_alignment import SystemAlignmentService
from admin.system_build import SystemBuild

pytestmark = pytest.mark.simulation


REVISION = "f7265fc747c2223f126f0ee7801e030c6226edf4"
TAG = "v0.8.0"
BUILD_ID = "v0.8.0-f7265fc"


def _target_build():
    return SystemBuild(
        requested_tag=TAG,
        canonical_tag=TAG,
        channel="stable",
        revision=REVISION,
        build_id=BUILD_ID,
        admin_image="ghcr.io/basecubedev/ems-solarflow-admin:v0.8.0",
        admin_digest="sha256:target-admin",
        ems_image="ghcr.io/basecubedev/ems-solarflow-api-control:v0.8.0",
        ems_digest="sha256:target-ems",
        release_tag=TAG,
    )


class _Resolver:
    def __init__(self, build):
        self.build = build

    def resolve(self, requested_tag):
        assert requested_tag == self.build.canonical_tag
        return self.build


class _Embedded:
    def import_into_cache(self, **_identity):
        return TAG


def _healthcheck_pending_service(tmp_path, current_ems_identity):
    build = _target_build()
    transitions = PendingTransitionStore(tmp_path / "state")
    known_good = KnownGoodStore(tmp_path / "state")
    service = SystemAlignmentService(
        resolver=_Resolver(build),
        transition_store=transitions,
        embedded_resources=_Embedded(),
        known_good_store=known_good,
        current_identity=lambda: ImageIdentity(
            image_ref=build.admin_image,
            digest=build.admin_digest,
            revision=build.revision,
            channel=build.channel,
            build_id=build.build_id,
            release_tag=build.release_tag,
        ),
        current_ems_identity=current_ems_identity,
        persistent_ref=lambda: build.admin_image,
        launcher=lambda _record: None,
    )
    started = service.start(requested_tag=TAG, mode="guided_upgrade")
    operation_id = started["operation_id"]
    service.verify_resources(operation_id=operation_id)
    service.begin_ems_operation(operation_id=operation_id)
    assert service.claim_ems_operation(operation_id=operation_id) is True
    service.finish_ems_operation(operation_id=operation_id, succeeded=True)
    assert transitions.read().stage == "healthcheck_pending"
    return service, transitions, known_good, operation_id


def test_healthy_previous_ems_cannot_become_target_known_good(tmp_path):
    previous = ImageIdentity(
        image_ref="ghcr.io/basecubedev/ems-solarflow-api-control:v0.7.0",
        digest="sha256:previous-ems",
        revision="a" * 40,
        channel="stable",
        build_id="v0.7.0-aaaaaaa",
        release_tag="v0.7.0",
    )
    service, transitions, known_good, operation_id = _healthcheck_pending_service(
        tmp_path, lambda: previous
    )

    result = service.finish_healthcheck(operation_id=operation_id, passed=True)

    assert result["status"] == "failed_recoverable"
    record = transitions.read()
    assert record.stage == "failed_recoverable"
    assert record.failed_stage == "healthcheck_pending"
    # A positively identified *different* EMS needs one new deployment claim;
    # retrying only the health probe could never align it to the target.
    assert record.resume_stage == "ems_operation_pending"
    assert record.error_code == "ems_identity_mismatch"
    assert known_good.current() is None


@pytest.mark.parametrize("failure", ("missing", "inspection_error"))
def test_uninspectable_ems_cannot_become_known_good(tmp_path, failure):
    def current_ems_identity():
        if failure == "inspection_error":
            raise RuntimeError("docker inspection unavailable")
        return ImageIdentity()

    service, transitions, known_good, operation_id = _healthcheck_pending_service(
        tmp_path, current_ems_identity
    )

    result = service.finish_healthcheck(operation_id=operation_id, passed=True)

    assert result["status"] == "failed_recoverable"
    record = transitions.read()
    assert record.error_code == "ems_identity_unverifiable"
    assert record.resume_stage == "healthcheck_pending"
    assert known_good.current() is None


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("revision", "a" * 40),
        ("build_id", "v0.7.0-aaaaaaa"),
        ("channel", "rc"),
        ("release_tag", "v0.8.0-RC1"),
    ),
)
def test_matching_digest_with_wrong_ems_metadata_cannot_become_known_good(
    tmp_path, field, value
):
    build = _target_build()
    identity = {
        "image_ref": build.ems_image,
        "digest": build.ems_digest,
        "revision": build.revision,
        "channel": build.channel,
        "build_id": build.build_id,
        "release_tag": build.release_tag,
    }
    identity[field] = value
    running = ImageIdentity(**identity)
    service, transitions, known_good, operation_id = _healthcheck_pending_service(
        tmp_path, lambda: running
    )

    result = service.finish_healthcheck(operation_id=operation_id, passed=True)

    assert result["status"] == "failed_recoverable"
    record = transitions.read()
    assert record.error_code == "ems_identity_mismatch"
    assert record.resume_stage == "ems_operation_pending"
    assert known_good.current() is None


def test_matching_running_ems_allows_known_good_and_completion(tmp_path):
    build = _target_build()
    running = ImageIdentity(
        image_ref=build.ems_image,
        digest=build.ems_digest,
        revision=build.revision,
        channel=build.channel,
        build_id=build.build_id,
        release_tag=build.release_tag,
    )
    service, transitions, known_good, operation_id = _healthcheck_pending_service(
        tmp_path, lambda: running
    )

    result = service.finish_healthcheck(operation_id=operation_id, passed=True)

    assert result["status"] == "completed"
    assert transitions.read().stage == "completed"
    assert known_good.current()["ems_digest"] == build.ems_digest


def test_running_ems_identity_uses_immutable_container_image_id_not_moved_tag():
    build = _target_build()

    class Docker:
        def __init__(self):
            self.inspected_images = []

        def inspect_container(self, _name):
            return {
                "image": build.ems_image,
                "container_id": "container-1",
                "status": "running",
            }

        def inspect_container_image_id(self, _name):
            return "sha256:immutable-target-image-id"

        def inspect_image(self, image_ref):
            self.inspected_images.append(image_ref)
            if image_ref == "sha256:immutable-target-image-id":
                return {
                    "image_ref": image_ref,
                    "digest": build.ems_digest,
                    "labels": {
                        "org.opencontainers.image.revision": build.revision,
                        "de.basecubedev.ems.channel": build.channel,
                        "de.basecubedev.ems.build_id": build.build_id,
                        "de.basecubedev.ems.release_tag": build.release_tag,
                    },
                }
            # The mutable tag was moved after this container started.
            return {
                "image_ref": image_ref,
                "digest": "sha256:moved-tag",
                "labels": {
                    "org.opencontainers.image.revision": "b" * 40,
                    "de.basecubedev.ems.channel": "stable",
                    "de.basecubedev.ems.build_id": "v0.8.1-bbbbbbb",
                    "de.basecubedev.ems.release_tag": "v0.8.1",
                },
            }

    docker = Docker()
    identity = _running_ems_identity(docker)

    assert docker.inspected_images == ["sha256:immutable-target-image-id"]
    assert identity.digest == build.ems_digest
    assert identity.build_id == build.build_id


@pytest.mark.parametrize("container_status", ("exited", "created", "paused"))
def test_non_running_ems_container_cannot_satisfy_runtime_identity(container_status):
    build = _target_build()

    class Docker:
        def __init__(self):
            self.inspected_images = []

        def inspect_container(self, _name):
            return {"image": build.ems_image, "status": container_status}

        def inspect_container_image_id(self, _name):
            raise AssertionError("a non-running container must not be identified")

        def inspect_image(self, image_ref):
            self.inspected_images.append(image_ref)
            return None

    docker = Docker()
    identity = _running_ems_identity(docker)

    assert identity.digest is None
    assert docker.inspected_images == [""]


def test_running_ems_identity_never_falls_back_to_a_mutable_tag():
    build = _target_build()

    class Docker:
        def __init__(self):
            self.inspected_images = []

        def inspect_container(self, _name):
            return {"image": build.ems_image, "status": "running"}

        def inspect_container_image_id(self, _name):
            return None

        def inspect_image(self, image_ref):
            self.inspected_images.append(image_ref)
            return None

    docker = Docker()
    identity = _running_ems_identity(docker)

    assert identity.digest is None
    assert docker.inspected_images == [""]
