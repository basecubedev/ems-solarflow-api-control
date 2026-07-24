# SPDX-License-Identifier: AGPL-3.0-or-later
"""One build-ID contract must survive every paired-build persistence layer."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from admin.admin_update import (
    ADMIN_IMAGE_REPO,
    EMS_IMAGE_REPO,
    PendingTransitionStore,
    TransitionStateError,
    make_transition_record,
    validate_transition_for_resume,
)
from admin.embedded_resources import (
    EmbeddedReleaseResources,
    EmbeddedResourcesError,
    write_release_resources,
)
from admin.known_good import KnownGoodStore
from admin.releases import ReleaseManager
from admin.system_build import SystemBuild, SystemBuildError, SystemBuildResolver
from workflow_contract import run_output_step

pytestmark = pytest.mark.simulation

ROOT = Path(__file__).resolve().parents[1]
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "docker-publish.yml"
REVISION = "c7b2f136c5cc7d0a1a00002fd183baa21869799f"
SHORT_REVISION = REVISION[:7]
T0 = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)

try:
    from admin.system_build_id import validate_system_build_id
except ModuleNotFoundError:
    validate_system_build_id = None


class FakeDocker:
    def __init__(self, images, present=None):
        self.images = images
        self.pulled = []
        # ``present is None`` keeps the legacy "every catalogue image is already
        # inspectable" behaviour. A set models real local presence: only those
        # refs (plus any pulled) inspect, so a digest-pinned image that is absent
        # must be pulled before it resolves.
        self._present = None if present is None else set(present)

    def pull(self, ref, on_progress=None):
        self.pulled.append(ref)
        if ref not in self.images:
            raise RuntimeError("image unavailable")
        if self._present is not None:
            self._present.add(ref)

    def inspect_image(self, ref):
        if self._present is not None and ref not in self._present:
            return None
        return self.images.get(ref)


def _pair(tag, *, revision=REVISION, build_id, channel="stable"):
    labels = {
        "org.opencontainers.image.version": tag,
        "org.opencontainers.image.revision": revision,
        "de.basecubedev.ems.build_id": build_id,
        "de.basecubedev.ems.channel": channel,
        "de.basecubedev.ems.release_tag": tag,
    }
    return {
        f"{ADMIN_IMAGE_REPO}:{tag}": {
            "image_ref": f"{ADMIN_IMAGE_REPO}:{tag}",
            "digest": "sha256:admin",
            "labels": labels,
        },
        f"{EMS_IMAGE_REPO}:{tag}": {
            "image_ref": f"{EMS_IMAGE_REPO}:{tag}",
            "digest": "sha256:ems",
            "labels": labels,
        },
    }


def _release_source(root: Path):
    files = {
        "config.template.json": "{}\n",
        "docker-compose.example.yml": "services: {}\n",
        "install-docker.sh": "#!/bin/sh\n",
        "install-docker.ps1": "Write-Host install\n",
        "deploy/docker/compose.influxdb.yml": "services: {}\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def test_authoritative_system_build_id_validator_exists():
    assert callable(validate_system_build_id), (
        "admin.system_build_id.validate_system_build_id must be the shared identity contract"
    )


@pytest.mark.skipif(validate_system_build_id is None, reason="shared validator not implemented yet")
@pytest.mark.parametrize(
    "value",
    (
        f"v0.8.0-{SHORT_REVISION}",
        f"v0.8.0-{SHORT_REVISION}-123456789-1",
        f"v0.8.0-RC1-{SHORT_REVISION}",
        f"v0.8.0-RC1-{SHORT_REVISION}-123456789-2",
        f"latest-{SHORT_REVISION}-123456789-3",
        f"dev-feature-zendure-mqtt-device-support-{SHORT_REVISION}-123456789-1",
        f"local-{SHORT_REVISION}",
        f"local-{SHORT_REVISION}-dirty",
    ),
)
def test_supported_system_build_id_formats(value):
    assert validate_system_build_id(value) == value


@pytest.mark.skipif(validate_system_build_id is None, reason="shared validator not implemented yet")
@pytest.mark.parametrize(
    "value",
    (
        "",
        f" {SHORT_REVISION}",
        f"local-{SHORT_REVISION} ",
        f"local/{SHORT_REVISION}",
        f"local-{SHORT_REVISION};id",
        f"local-{SHORT_REVISION}$(id)",
        "x" * 129,
        f"LOCAL-{SHORT_REVISION}",
        f"dev-feature-x-{SHORT_REVISION}-123456789",  # old tag lacks attempt
        "dev-feature-x-nothex-123456789-1",
        f"dev-Feature-X-{SHORT_REVISION}-123456789-1",
    ),
)
def test_unsafe_or_malformed_system_build_ids_are_rejected(value):
    with pytest.raises(ValueError):
        validate_system_build_id(value)


def test_real_ci_build_identity_round_trips_every_layer(tmp_path):
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=True
    ).stdout.strip()
    result, outputs = run_output_step(
        PUBLISH_WORKFLOW,
        "Resolve build identity",
        cwd=ROOT,
        tmp_path=tmp_path,
        environ={
            "GITHUB_REF": "refs/tags/v0.8.0",
            "GITHUB_SHA": revision,
            "GITHUB_RUN_ID": "123456789",
            "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_RUN_NUMBER": "314",
        },
    )
    assert result.returncode == 0, result.stderr
    build_id = outputs["build_id"]

    resolver = SystemBuildResolver(
        docker=FakeDocker(_pair("v0.8.0", revision=revision, build_id=build_id))
    )
    build = resolver.resolve("v0.8.0")

    record = make_transition_record(
        mode="guided_upgrade",
        system_tag=build.canonical_tag,
        build_id=build.build_id,
        revision=build.revision,
        admin_image=build.admin_image,
        admin_digest=build.admin_digest,
        ems_image=build.ems_image,
        ems_digest=build.ems_digest,
        now=T0,
    )
    transitions = PendingTransitionStore(tmp_path / "state")
    transitions.begin(record, now=T0)
    persisted = transitions.read()
    assert persisted.build_id == build_id
    validate_transition_for_resume(
        persisted,
        now=T0,
        running_admin={
            "digest": build.admin_digest,
            "revision": revision,
            "build_id": build_id,
        },
    )

    source = _release_source(tmp_path / "release-source")
    bundle = tmp_path / "release-resources"
    write_release_resources(
        bundle,
        source_root=source,
        system_tag=build.canonical_tag,
        channel=build.channel,
        revision=revision,
        build_id=build_id,
        release_tag=build.release_tag,
        admin_image=build.admin_image,
        ems_image=build.ems_image,
    )
    manager = ReleaseManager(data_dir=tmp_path / "admin-data")
    embedded = EmbeddedReleaseResources(release_manager=manager, resources_dir=bundle)
    verified = embedded.verify(running_build=build.as_dict())
    assert verified["build_id"] == build_id

    known_good = KnownGoodStore(tmp_path / "known-good")
    stored = known_good.record(build)
    assert stored["build_id"] == build_id
    assert known_good.current()["build_id"] == build_id


def test_resolver_rejects_unsafe_build_id_even_when_pair_matches():
    unsafe = f"v0.8.0-{SHORT_REVISION}-123-1;touch"
    resolver = SystemBuildResolver(docker=FakeDocker(_pair("v0.8.0", build_id=unsafe)))
    with pytest.raises((SystemBuildError, ValueError)):
        resolver.resolve("v0.8.0")


def _development_descriptor(tag, revision=REVISION, **overrides):
    descriptor = {
        "tag": tag,
        "display_name": "digest canary",
        "channel": "development",
        "revision": revision,
        "build_id": tag,
        "run_id": "123456789",
        "run_attempt": 1,
        "created_at": "2026-07-17T12:00:00Z",
        "admin_image": f"{ADMIN_IMAGE_REPO}:{tag}",
        "admin_digest": "sha256:" + "a" * 64,
        "ems_image": f"{EMS_IMAGE_REPO}:{tag}",
        "ems_digest": "sha256:" + "b" * 64,
        "installable": True,
    }
    descriptor.update(overrides)
    return descriptor


def _digest_pair(descriptor, *, admin_digest=None, ems_digest=None, revision=None):
    labels = {
        "org.opencontainers.image.version": descriptor["tag"],
        "org.opencontainers.image.revision": revision or descriptor["revision"],
        "de.basecubedev.ems.build_id": descriptor["build_id"],
        "de.basecubedev.ems.channel": "development",
        "de.basecubedev.ems.release_tag": descriptor["tag"],
    }
    admin_ref = f'{ADMIN_IMAGE_REPO}@{descriptor["admin_digest"]}'
    ems_ref = f'{EMS_IMAGE_REPO}@{descriptor["ems_digest"]}'
    return {
        admin_ref: {
            "image_ref": admin_ref,
            "digest": admin_digest or descriptor["admin_digest"],
            "labels": labels,
        },
        ems_ref: {
            "image_ref": ems_ref,
            "digest": ems_digest or descriptor["ems_digest"],
            "labels": labels,
        },
    }


def test_development_resolver_pulls_catalogue_pair_by_immutable_digest():
    tag = f"dev-digest-canary-{SHORT_REVISION}-123456789-1"
    descriptor = _development_descriptor(tag)
    # The digest-pinned pair is not yet present locally, so it is pulled by its
    # immutable digest (never by the floating tag).
    docker = FakeDocker(_digest_pair(descriptor), present=set())
    resolver = SystemBuildResolver(
        docker=docker,
        development_build_source=lambda requested: (
            descriptor if requested == tag else None
        ),
    )

    build = resolver.resolve(tag)

    assert docker.pulled == [
        f'{ADMIN_IMAGE_REPO}@{descriptor["admin_digest"]}',
        f'{EMS_IMAGE_REPO}@{descriptor["ems_digest"]}',
    ]
    assert build.admin_image == descriptor["admin_image"]
    assert build.admin_digest == descriptor["admin_digest"]
    assert build.ems_image == descriptor["ems_image"]
    assert build.ems_digest == descriptor["ems_digest"]


def test_development_resolver_reuses_present_immutable_digest_without_pull():
    # An exact digest-pinned pair already present locally is reused as-is: its
    # identity is proven by inspection, so no registry pull is made.
    tag = f"dev-digest-canary-{SHORT_REVISION}-123456789-1"
    descriptor = _development_descriptor(tag)
    docker = FakeDocker(_digest_pair(descriptor))  # every image already local
    resolver = SystemBuildResolver(
        docker=docker,
        development_build_source=lambda requested: (
            descriptor if requested == tag else None
        ),
    )

    build = resolver.resolve(tag)

    assert docker.pulled == []
    assert build.admin_digest == descriptor["admin_digest"]
    assert build.ems_digest == descriptor["ems_digest"]


@pytest.mark.parametrize(
    ("descriptor_overrides", "image_overrides"),
    (
        ({"admin_image": f"{ADMIN_IMAGE_REPO}:another-tag"}, {}),
        ({"build_id": "dev-another-c7b2f13-123456789-1"}, {}),
        ({}, {"admin_digest": "sha256:" + "c" * 64}),
        ({}, {"ems_digest": "sha256:" + "d" * 64}),
        ({}, {"revision": "d" * 40}),
    ),
)
def test_development_resolver_rejects_catalogue_or_pulled_pair_mismatch(
    descriptor_overrides, image_overrides
):
    tag = f"dev-digest-canary-{SHORT_REVISION}-123456789-1"
    descriptor = _development_descriptor(tag, **descriptor_overrides)
    docker = FakeDocker(_digest_pair(descriptor, **image_overrides))
    resolver = SystemBuildResolver(
        docker=docker,
        development_build_source=lambda _requested: descriptor,
    )

    with pytest.raises(SystemBuildError) as exc:
        resolver.resolve(tag)

    assert exc.value.code == "system_build_mismatch"


def test_transition_rejects_unsafe_build_id_even_when_revision_is_embedded():
    unsafe = f"v0.8.0-{SHORT_REVISION}-123-1;touch"
    with pytest.raises((TransitionStateError, ValueError)):
        make_transition_record(
            mode="guided_upgrade",
            system_tag="v0.8.0",
            build_id=unsafe,
            revision=REVISION,
            admin_image=f"{ADMIN_IMAGE_REPO}:v0.8.0",
            admin_digest="sha256:admin",
            ems_image=f"{EMS_IMAGE_REPO}:v0.8.0",
            ems_digest="sha256:ems",
            now=T0,
        )


def test_embedded_resources_reject_unsafe_build_id(tmp_path):
    unsafe = f"v0.8.0-{SHORT_REVISION}-123-1;touch"
    source = _release_source(tmp_path / "source")
    bundle = tmp_path / "bundle"
    with pytest.raises((EmbeddedResourcesError, ValueError)):
        write_release_resources(
            bundle,
            source_root=source,
            system_tag="v0.8.0",
            channel="stable",
            revision=REVISION,
            build_id=unsafe,
            release_tag="v0.8.0",
            admin_image=f"{ADMIN_IMAGE_REPO}:v0.8.0",
            ems_image=f"{EMS_IMAGE_REPO}:v0.8.0",
        )


def test_known_good_rejects_unsafe_build_id(tmp_path):
    unsafe = f"v0.8.0-{SHORT_REVISION}-123-1;touch"
    build = SystemBuild(
        requested_tag="v0.8.0",
        canonical_tag="v0.8.0",
        channel="stable",
        revision=REVISION,
        build_id=unsafe,
        admin_image=f"{ADMIN_IMAGE_REPO}:v0.8.0",
        admin_digest="sha256:admin",
        ems_image=f"{EMS_IMAGE_REPO}:v0.8.0",
        ems_digest="sha256:ems",
        release_tag="v0.8.0",
    )
    with pytest.raises(ValueError):
        KnownGoodStore(tmp_path).record(build)
