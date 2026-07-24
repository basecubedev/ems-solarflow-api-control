# SPDX-License-Identifier: AGPL-3.0-or-later
"""System Build verification is done once, at the right time, and reused.

These tests lock the pull economics of the verification lifecycle:

* an explicit verification pulls each missing image at most once;
* an exact digest-pinned image already present locally is reused without a pull;
* a resolved tag is reused (Continue / Update Admin Server / re-render) without a
  second pull, and concurrent verifications of one tag coalesce into one pull;
* changing the selection (or invalidating / expiring the cache) forces a fresh
  verification;
* a GitHub Container Registry pull-rate-limit maps to a typed error and never
  produces a cached / verified result.

No real Docker daemon: pulls/inspects are faked, counting every pull.
"""

import threading
import time

import pytest

from admin.admin_update import ADMIN_IMAGE_REPO, EMS_IMAGE_REPO
from admin.deployment import _docker_pull_error
from admin.system_alignment import SystemAlignmentService
from admin.system_build import (
    CachingBuildResolver,
    SystemBuildError,
    SystemBuildResolver,
)

pytestmark = [pytest.mark.simulation, pytest.mark.system_build]

REVISION = "f7265fc747c2223f126f0ee7801e030c6226edf4"
DEV_TAG = "dev-feature-zendure-mqtt-device-support-f7265fc-123456789-1"
ADMIN_DIGEST = "sha256:" + "a" * 64
EMS_DIGEST = "sha256:" + "b" * 64


def _labels(*, revision=REVISION, build_id="v0.8.0-f7265fc", channel="stable",
            release_tag=None, version="v0.8.0"):
    labels = {
        "org.opencontainers.image.version": version,
        "org.opencontainers.image.revision": revision,
        "de.basecubedev.ems.build_id": build_id,
        "de.basecubedev.ems.channel": channel,
    }
    if release_tag:
        labels["de.basecubedev.ems.release_tag"] = release_tag
    return labels


def _pair(tag, *, channel="stable"):
    labels = _labels(channel=channel, release_tag=tag, version=tag)
    return {
        f"{ADMIN_IMAGE_REPO}:{tag}": {"digest": "sha256:admin", "labels": labels},
        f"{EMS_IMAGE_REPO}:{tag}": {"digest": "sha256:ems", "labels": labels},
    }


class RecordingDocker:
    """DockerCli-shaped double keyed by tag *and* digest ref, counting pulls.

    ``pull`` records every ref and makes that exact image locally inspectable
    afterwards (mirroring a real ``docker pull``). ``present`` pre-seeds
    locally-present images so a digest-pinned inspect can hit before any pull.
    A ref in ``rate_limit_refs`` raises a registry throttle on pull; ``pull_delay``
    widens the concurrency window.
    """

    def __init__(self, images, *, present=(), rate_limit_refs=(), pull_delay=0.0):
        self._images = dict(images)
        self._local = set(present)
        self._rate_limit = set(rate_limit_refs)
        self._pull_delay = pull_delay
        self.pulled = []
        self._lock = threading.Lock()

    def pull(self, ref, on_progress=None):
        with self._lock:
            self.pulled.append(ref)
        if self._pull_delay:
            time.sleep(self._pull_delay)
        if ref in self._rate_limit:
            raise RuntimeError(
                "toomanyrequests: You have reached your pull rate limit."
            )
        if ref not in self._images:
            raise RuntimeError(f"pull failed: {ref} not found")
        with self._lock:
            self._local.add(ref)

    def inspect_image(self, ref):
        if ref not in self._images or ref not in self._local:
            return None
        entry = self._images[ref]
        return {"image_ref": ref, "digest": entry["digest"], "labels": entry["labels"]}


# --- digest-pinned local reuse (development descriptor path) -----------------


def _dev_images(tag=DEV_TAG):
    admin_ref = f"{ADMIN_IMAGE_REPO}@{ADMIN_DIGEST}"
    ems_ref = f"{EMS_IMAGE_REPO}@{EMS_DIGEST}"
    labels = _labels(build_id=tag, channel="development", release_tag=tag, version=tag)
    images = {
        admin_ref: {"digest": ADMIN_DIGEST, "labels": labels},
        ems_ref: {"digest": EMS_DIGEST, "labels": labels},
    }
    return images, admin_ref, ems_ref


def _dev_source(tag=DEV_TAG):
    def source(requested):
        assert requested == tag
        return {
            "tag": tag,
            "channel": "development",
            "installable": True,
            "build_id": tag,
            "admin_image": f"{ADMIN_IMAGE_REPO}:{tag}",
            "ems_image": f"{EMS_IMAGE_REPO}:{tag}",
            "admin_digest": ADMIN_DIGEST,
            "ems_digest": EMS_DIGEST,
            "revision": REVISION,
        }

    return source


def test_exact_digest_pinned_images_present_locally_are_reused_without_pull():
    images, admin_ref, ems_ref = _dev_images()
    docker = RecordingDocker(images, present=[admin_ref, ems_ref])
    build = SystemBuildResolver(
        docker=docker, development_build_source=_dev_source()
    ).resolve(DEV_TAG)
    # Both images were already local: identity is proven by inspection, no pull.
    assert docker.pulled == []
    assert build.admin_digest == ADMIN_DIGEST
    assert build.ems_digest == EMS_DIGEST
    assert build.revision == REVISION


def test_missing_digest_pinned_image_pulls_only_the_absent_one():
    images, admin_ref, ems_ref = _dev_images()
    # Admin digest is local; EMS digest is missing.
    docker = RecordingDocker(images, present=[admin_ref])
    build = SystemBuildResolver(
        docker=docker, development_build_source=_dev_source()
    ).resolve(DEV_TAG)
    assert docker.pulled == [ems_ref]  # zero admin pulls, one ems pull
    assert build.admin_digest == ADMIN_DIGEST
    assert build.ems_digest == EMS_DIGEST


def test_mutable_tag_is_always_pulled_even_when_locally_present():
    # A matching *tag* alone is never proof: a stable/latest tag ref resolves
    # through a pull even if a same-named image already exists locally.
    tag = "v0.8.0"
    images = _pair(tag)
    docker = RecordingDocker(
        images, present=[f"{ADMIN_IMAGE_REPO}:{tag}", f"{EMS_IMAGE_REPO}:{tag}"]
    )
    SystemBuildResolver(docker=docker).resolve(tag)
    assert docker.pulled == [f"{ADMIN_IMAGE_REPO}:{tag}", f"{EMS_IMAGE_REPO}:{tag}"]


# --- verified-resolution reuse (CachingBuildResolver) ------------------------


def test_verification_pulls_once_and_is_reused_without_a_second_pull():
    docker = RecordingDocker(_pair("v0.8.0"))
    resolver = CachingBuildResolver(SystemBuildResolver(docker=docker))
    first = resolver.resolve("v0.8.0")
    second = resolver.resolve("v0.8.0")  # Continue / Update / re-render
    assert first.admin_digest == second.admin_digest
    # One pull each — the reuse pulls nothing more.
    assert docker.pulled == [f"{ADMIN_IMAGE_REPO}:v0.8.0", f"{EMS_IMAGE_REPO}:v0.8.0"]


def test_different_selection_is_verified_independently():
    images = {**_pair("v0.8.0"), **_pair("v0.8.1")}
    docker = RecordingDocker(images)
    resolver = CachingBuildResolver(SystemBuildResolver(docker=docker))
    resolver.resolve("v0.8.0")
    resolver.resolve("v0.8.1")
    # Each distinct tag pulled its own pair exactly once.
    assert docker.pulled == [
        f"{ADMIN_IMAGE_REPO}:v0.8.0",
        f"{EMS_IMAGE_REPO}:v0.8.0",
        f"{ADMIN_IMAGE_REPO}:v0.8.1",
        f"{EMS_IMAGE_REPO}:v0.8.1",
    ]


def test_invalidate_forces_a_fresh_verification():
    docker = RecordingDocker(_pair("v0.8.0"))
    resolver = CachingBuildResolver(SystemBuildResolver(docker=docker))
    resolver.resolve("v0.8.0")
    resolver.invalidate("v0.8.0")
    resolver.resolve("v0.8.0")
    assert docker.pulled == [
        f"{ADMIN_IMAGE_REPO}:v0.8.0",
        f"{EMS_IMAGE_REPO}:v0.8.0",
    ] * 2


def test_expired_entry_is_re_verified():
    clock = {"t": 1000.0}
    docker = RecordingDocker(_pair("v0.8.0"))
    resolver = CachingBuildResolver(
        SystemBuildResolver(docker=docker),
        ttl_seconds=300,
        clock=lambda: clock["t"],
    )
    resolver.resolve("v0.8.0")
    clock["t"] += 301  # past the TTL
    resolver.resolve("v0.8.0")
    assert len(docker.pulled) == 4  # two full pairs


def test_concurrent_verification_of_one_tag_coalesces_into_one_pull():
    docker = RecordingDocker(_pair("v0.8.0"), pull_delay=0.05)
    resolver = CachingBuildResolver(SystemBuildResolver(docker=docker))
    start = threading.Barrier(2)
    results = []

    def worker():
        start.wait()
        results.append(resolver.resolve("v0.8.0"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 2
    assert results[0].admin_digest == results[1].admin_digest
    # Exactly one pair pulled despite two simultaneous requests.
    assert docker.pulled == [f"{ADMIN_IMAGE_REPO}:v0.8.0", f"{EMS_IMAGE_REPO}:v0.8.0"]


# --- registry rate-limit ------------------------------------------------------


def test_pull_rate_limit_maps_to_typed_docker_error():
    err = _docker_pull_error(
        "toomanyrequests: You have reached your pull rate limit. "
        "You may increase the limit by authenticating and upgrading."
    )
    assert err.code == "image_pull_rate_limited"
    assert "rate limit" in err.message.lower()


def test_resolver_maps_rate_limit_to_typed_system_build_error():
    images = _pair("v0.8.0")
    docker = RecordingDocker(
        images, rate_limit_refs=[f"{ADMIN_IMAGE_REPO}:v0.8.0"]
    )
    with pytest.raises(SystemBuildError) as excinfo:
        SystemBuildResolver(docker=docker).resolve("v0.8.0")
    assert excinfo.value.code == "system_build_registry_rate_limited"
    assert "rate limit" in excinfo.value.message.lower()


def test_rate_limited_resolution_is_not_cached():
    images = _pair("v0.8.0")
    docker = RecordingDocker(
        images, rate_limit_refs=[f"{ADMIN_IMAGE_REPO}:v0.8.0"]
    )
    resolver = CachingBuildResolver(SystemBuildResolver(docker=docker))
    with pytest.raises(SystemBuildError):
        resolver.resolve("v0.8.0")
    # A failed verification leaves no cached result; a retry attempts the pull.
    with pytest.raises(SystemBuildError):
        resolver.resolve("v0.8.0")
    assert docker.pulled == [f"{ADMIN_IMAGE_REPO}:v0.8.0"] * 2


# --- service reuse: Continue does not re-pull the verified build --------------


def _aligned_service(tmp_path, docker, tag="v0.8.0"):
    from admin.embedded_resources import EmbeddedReleaseResources  # noqa: F401
    from admin.image_identity import ImageIdentity
    from admin.known_good import KnownGoodStore
    from admin.admin_update import PendingTransitionStore

    class _Embedded:
        def verify(self, *, running_build):
            return running_build

        def import_into_cache(self, *, running_build):
            return running_build.get("canonical_tag")

    running = ImageIdentity(
        image_ref=f"{ADMIN_IMAGE_REPO}:{tag}",
        digest="sha256:admin",
        revision=REVISION,
        build_id="v0.8.0-f7265fc",
        channel="stable",
        release_tag=tag,
        version_label=tag,
    )
    running_ems = ImageIdentity(
        image_ref=f"{EMS_IMAGE_REPO}:{tag}",
        digest="sha256:ems",
        revision=REVISION,
        build_id="v0.8.0-f7265fc",
        channel="stable",
        release_tag=tag,
    )
    return SystemAlignmentService(
        resolver=CachingBuildResolver(SystemBuildResolver(docker=docker)),
        transition_store=PendingTransitionStore(tmp_path / "state"),
        embedded_resources=_Embedded(),
        known_good_store=KnownGoodStore(tmp_path / "state"),
        current_identity=lambda: running,
        current_ems_identity=lambda: running_ems,
        persistent_ref=lambda: f"{ADMIN_IMAGE_REPO}:{tag}",
        launcher=lambda record: None,
    )


def test_continue_after_verification_reuses_build_without_pulling(tmp_path):
    from admin.admin_update import TRANSITION_MODE_FRESH_INSTALL

    docker = RecordingDocker(_pair("v0.8.0"))
    service = _aligned_service(tmp_path, docker)

    result = service.validate(requested_tag="v0.8.0")  # explicit verification
    assert result["valid"] is True
    pulled_after_verify = list(docker.pulled)
    assert pulled_after_verify == [
        f"{ADMIN_IMAGE_REPO}:v0.8.0",
        f"{EMS_IMAGE_REPO}:v0.8.0",
    ]

    # Continue (confirm the aligned Setup build) reuses the verified resolution.
    service.confirm_setup_build(
        requested_tag="v0.8.0", mode=TRANSITION_MODE_FRESH_INSTALL
    )
    assert docker.pulled == pulled_after_verify  # zero additional pulls


# --- Guided Upgrade shares the same verified-resolution cache -----------------


def _upgrade_service(tmp_path, docker, *, running_tag="v0.7.0"):
    from admin.image_identity import ImageIdentity
    from admin.known_good import KnownGoodStore
    from admin.admin_update import PendingTransitionStore

    class _Embedded:
        def verify(self, *, running_build):
            return running_build

        def import_into_cache(self, *, running_build):
            return running_build.get("canonical_tag")

    running_admin = ImageIdentity(
        image_ref=f"{ADMIN_IMAGE_REPO}:{running_tag}",
        digest="sha256:old-admin",
        revision="old",
        build_id="v0.7.0-old",
        channel="stable",
        release_tag=running_tag,
        version_label=running_tag,
    )
    running_ems = ImageIdentity(
        image_ref=f"{EMS_IMAGE_REPO}:{running_tag}",
        digest="sha256:old-ems",
        revision="old",
        build_id="v0.7.0-old",
        channel="stable",
        release_tag=running_tag,
    )
    return SystemAlignmentService(
        resolver=CachingBuildResolver(SystemBuildResolver(docker=docker)),
        transition_store=PendingTransitionStore(tmp_path / "state"),
        embedded_resources=_Embedded(),
        known_good_store=KnownGoodStore(tmp_path / "state"),
        current_identity=lambda: running_admin,
        current_ems_identity=lambda: running_ems,
        persistent_ref=lambda: f"{ADMIN_IMAGE_REPO}:{running_tag}",
        launcher=lambda record: None,
    )


def test_upgrade_validate_then_execute_reuses_build_without_pulling(tmp_path):
    docker = RecordingDocker(_pair("v0.8.0"))
    service = _upgrade_service(tmp_path, docker)

    # Guided Upgrade explicit verification pulls the pair once.
    result = service.validate_upgrade_target(requested_tag="v0.8.0")
    assert result["valid"] is True
    assert result["upgrade_allowed"] is True
    pulled_after_verify = list(docker.pulled)
    assert pulled_after_verify == [
        f"{ADMIN_IMAGE_REPO}:v0.8.0",
        f"{EMS_IMAGE_REPO}:v0.8.0",
    ]

    # Execute re-resolves the target (server.py:_handle_maintenance_upgrade_execute
    # → system_alignment.resolve). The cached, digest-pinned resolution is reused.
    build = service.resolve("v0.8.0")
    assert build.ems_image == f"{EMS_IMAGE_REPO}:v0.8.0"
    assert docker.pulled == pulled_after_verify  # zero additional verification pulls


def test_upgrade_validation_rate_limit_is_typed_and_not_cached(tmp_path):
    docker = RecordingDocker(
        _pair("v0.8.0"), rate_limit_refs=[f"{ADMIN_IMAGE_REPO}:v0.8.0"]
    )
    service = _upgrade_service(tmp_path, docker)

    with pytest.raises(SystemBuildError) as excinfo:
        service.validate_upgrade_target(requested_tag="v0.8.0")
    assert excinfo.value.code == "system_build_registry_rate_limited"
    # A rate-limited verification is never cached and never opens a transition.
    assert service._transitions.read() is None
    with pytest.raises(SystemBuildError):
        service.validate_upgrade_target(requested_tag="v0.8.0")
    assert docker.pulled == [f"{ADMIN_IMAGE_REPO}:v0.8.0"] * 2
