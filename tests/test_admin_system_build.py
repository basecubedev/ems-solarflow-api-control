# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verified Admin/EMS SystemBuild pair resolution and alignment decisions.

No real Docker daemon: image pull/inspect is faked, returning the OCI build
labels CI stamps onto each image. The resolver derives the two fixed official
image names server-side and never accepts a browser-supplied image ref.
"""

import pytest

from admin.admin_update import ADMIN_IMAGE_REPO, EMS_IMAGE_REPO
from admin.image_identity import ImageIdentity
from admin.system_build import (
    ALIGN_ADMIN_UPDATE_REQUIRED,
    ALIGN_ALIGNED,
    ALIGN_RETAG_REQUIRED,
    DigestReferenceError,
    SystemBuild,
    SystemBuildError,
    SystemBuildResolver,
    decide_alignment,
    digest_pinned_ref,
)

pytestmark = [pytest.mark.simulation, pytest.mark.system_build]


REVISION = "f7265fc747c2223f126f0ee7801e030c6226edf4"
DEV_TAG = "dev-feature-zendure-mqtt-device-support-f7265fc-123456789-1"
OLD_RETRY_MUTABLE_DEV_TAG = "dev-feature-zendure-mqtt-device-support-f7265fc-123456789"
DEV_ALIAS = "dev-feature-zendure-mqtt-device-support"
DEV_SHA_ALIAS = "dev-feature-zendure-mqtt-device-support-f7265fc"


def _labels(
    revision=REVISION,
    build_id="v0.8.0-f7265fc",
    channel="stable",
    release_tag=None,
    version="v0.8.0",
):
    labels = {
        "org.opencontainers.image.version": version,
        "org.opencontainers.image.revision": revision,
        "de.basecubedev.ems.build_id": build_id,
        "de.basecubedev.ems.channel": channel,
    }
    if release_tag:
        labels["de.basecubedev.ems.release_tag"] = release_tag
    return labels


class FakeDocker:
    """DockerCli-shaped double: pull records + validates, inspect returns labels."""

    def __init__(self, images):
        self._images = dict(images)
        self.pulled = []

    def pull(self, ref, on_progress=None):
        self.pulled.append(ref)
        if ref not in self._images:
            raise RuntimeError(f"pull failed: {ref} not found")

    def inspect_image(self, ref):
        entry = self._images.get(ref)
        if entry is None:
            return None
        return {"image_ref": ref, "digest": entry["digest"], "labels": entry["labels"]}


def _pair(tag, *, channel="stable", build_id="v0.8.0-f7265fc", revision=REVISION,
          release_tag=None, admin_digest="sha256:admin", ems_digest="sha256:ems",
          admin_labels=None, ems_labels=None):
    admin_ref = f"{ADMIN_IMAGE_REPO}:{tag}"
    ems_ref = f"{EMS_IMAGE_REPO}:{tag}"
    images = {
        admin_ref: {
            "digest": admin_digest,
            "labels": admin_labels
            or _labels(
                revision=revision,
                build_id=build_id,
                channel=channel,
                release_tag=release_tag,
                version=tag,
            ),
        },
        ems_ref: {
            "digest": ems_digest,
            "labels": ems_labels
            or _labels(
                revision=revision,
                build_id=build_id,
                channel=channel,
                release_tag=release_tag,
                version=tag,
            ),
        },
    }
    return images


# --- pair resolution: happy paths ----------------------------------------


def test_resolves_matching_stable_pair():
    docker = FakeDocker(_pair("v0.8.0", channel="stable", release_tag="v0.8.0"))
    build = SystemBuildResolver(docker=docker).resolve("v0.8.0")
    assert isinstance(build, SystemBuild)
    assert build.canonical_tag == "v0.8.0"
    assert build.channel == "stable"
    assert build.revision == REVISION
    assert build.build_id == "v0.8.0-f7265fc"
    assert build.admin_image == f"{ADMIN_IMAGE_REPO}:v0.8.0"
    assert build.ems_image == f"{EMS_IMAGE_REPO}:v0.8.0"
    assert build.admin_digest == "sha256:admin"
    assert build.ems_digest == "sha256:ems"
    # Both images were resolved (pulled) before returning.
    assert docker.pulled == [f"{ADMIN_IMAGE_REPO}:v0.8.0", f"{EMS_IMAGE_REPO}:v0.8.0"]


def test_resolves_matching_rc_pair():
    docker = FakeDocker(_pair("v0.8.0-RC1", channel="rc", release_tag="v0.8.0-RC1"))
    build = SystemBuildResolver(docker=docker).resolve("v0.8.0-RC1")
    assert build.channel == "rc"
    assert build.canonical_tag == "v0.8.0-RC1"


def test_resolves_matching_latest_pair():
    docker = FakeDocker(_pair("latest", channel="latest", release_tag="latest"))
    build = SystemBuildResolver(docker=docker).resolve("latest")
    assert build.channel == "latest"
    assert build.canonical_tag == "latest"
    assert build.release_tag == "latest"


def test_rejects_latest_pair_without_canonical_release_tag():
    docker = FakeDocker(_pair("latest", channel="latest"))

    with pytest.raises(SystemBuildError) as exc:
        SystemBuildResolver(docker=docker).resolve("latest")

    assert exc.value.code == "system_build_mismatch"


def test_resolves_immutable_development_pair():
    docker = FakeDocker(
        _pair(
            DEV_TAG,
            channel="development",
            build_id=DEV_TAG,
            release_tag=DEV_TAG,
        )
    )
    build = SystemBuildResolver(docker=docker).resolve(DEV_TAG)
    assert build.channel == "development"
    assert build.canonical_tag == DEV_TAG


@pytest.mark.parametrize("build_id", ("local-f7265fc", "local-f7265fc-dirty"))
def test_resolves_repository_local_pair(build_id):
    docker = FakeDocker(
        _pair(
            "local",
            channel="development",
            build_id=build_id,
            release_tag="local",
        )
    )

    build = SystemBuildResolver(docker=docker).resolve("local")

    assert build.canonical_tag == "local"
    assert build.channel == "development"
    assert build.build_id == build_id
    assert build.release_tag == "local"


def test_resolves_repository_local_pair_without_registry_pull():
    class LocalDocker(FakeDocker):
        def pull(self, ref, on_progress=None):
            raise AssertionError(f"local pair must not pull {ref}")

    docker = LocalDocker(
        _pair(
            "local",
            channel="development",
            build_id="local-f7265fc",
            release_tag="local",
        )
    )

    build = SystemBuildResolver(docker=docker).resolve("local")

    assert build.build_id == "local-f7265fc"


def test_rejects_local_pair_whose_build_id_does_not_match_revision():
    docker = FakeDocker(
        _pair(
            "local",
            channel="development",
            build_id="local-deadbee",
            release_tag="local",
        )
    )

    with pytest.raises(SystemBuildError) as exc:
        SystemBuildResolver(docker=docker).resolve("local")

    assert exc.value.code == "system_build_mismatch"


def test_rejects_development_pair_whose_canonical_metadata_names_another_tag():
    other = "dev-feature-other-f7265fc-987654321-2"
    labels = _labels(
        channel="development", build_id=other, release_tag=other
    )
    docker = FakeDocker(
        _pair(DEV_TAG, admin_labels=labels, ems_labels=labels)
    )
    with pytest.raises(SystemBuildError) as exc:
        SystemBuildResolver(docker=docker).resolve(DEV_TAG)
    assert exc.value.code == "system_build_mismatch"


@pytest.mark.parametrize("version", (None, "v0.7.0"))
def test_rejects_pair_without_matching_oci_version(version):
    labels = _labels(release_tag="v0.8.0", version=version)
    docker = FakeDocker(
        _pair("v0.8.0", admin_labels=labels, ems_labels=labels)
    )

    with pytest.raises(SystemBuildError) as exc:
        SystemBuildResolver(docker=docker).resolve("v0.8.0")

    assert exc.value.code == "system_build_mismatch"


# --- pair resolution: rejections -----------------------------------------


def test_rejects_missing_admin_image():
    images = _pair("v0.8.0", release_tag="v0.8.0")
    del images[f"{ADMIN_IMAGE_REPO}:v0.8.0"]
    with pytest.raises(SystemBuildError) as exc:
        SystemBuildResolver(docker=FakeDocker(images)).resolve("v0.8.0")
    assert "admin" in exc.value.code


def test_rejects_missing_ems_image():
    images = _pair("v0.8.0", release_tag="v0.8.0")
    del images[f"{EMS_IMAGE_REPO}:v0.8.0"]
    with pytest.raises(SystemBuildError) as exc:
        SystemBuildResolver(docker=FakeDocker(images)).resolve("v0.8.0")
    assert "ems" in exc.value.code


def test_rejects_revision_mismatch():
    admin_labels = _labels(revision=REVISION, release_tag="v0.8.0")
    ems_labels = _labels(revision="0000000different", release_tag="v0.8.0")
    docker = FakeDocker(
        _pair("v0.8.0", admin_labels=admin_labels, ems_labels=ems_labels)
    )
    with pytest.raises(SystemBuildError) as exc:
        SystemBuildResolver(docker=docker).resolve("v0.8.0")
    assert exc.value.code == "system_build_mismatch"


def test_rejects_build_id_mismatch():
    admin_labels = _labels(build_id="v0.8.0-f7265fc", release_tag="v0.8.0")
    ems_labels = _labels(build_id="v0.8.0-deadbee", release_tag="v0.8.0")
    docker = FakeDocker(
        _pair("v0.8.0", admin_labels=admin_labels, ems_labels=ems_labels)
    )
    with pytest.raises(SystemBuildError) as exc:
        SystemBuildResolver(docker=docker).resolve("v0.8.0")
    assert exc.value.code == "system_build_mismatch"


def test_rejects_channel_mismatch():
    admin_labels = _labels(channel="stable", release_tag="v0.8.0")
    ems_labels = _labels(channel="rc", release_tag="v0.8.0")
    docker = FakeDocker(
        _pair("v0.8.0", admin_labels=admin_labels, ems_labels=ems_labels)
    )
    with pytest.raises(SystemBuildError) as exc:
        SystemBuildResolver(docker=docker).resolve("v0.8.0")
    assert exc.value.code == "system_build_mismatch"


def test_rejects_missing_required_metadata():
    admin_labels = {"de.basecubedev.ems.channel": "stable"}  # no revision/build_id
    docker = FakeDocker(
        _pair("v0.8.0", admin_labels=admin_labels, ems_labels=_labels(release_tag="v0.8.0"))
    )
    with pytest.raises(SystemBuildError) as exc:
        SystemBuildResolver(docker=docker).resolve("v0.8.0")
    assert exc.value.code == "system_build_mismatch"


def test_rejects_stable_release_tag_mismatch():
    # Stable/RC builds must also match the expected release tag label.
    labels = _labels(channel="stable", release_tag="v0.7.0")  # wrong release tag
    docker = FakeDocker(_pair("v0.8.0", admin_labels=labels, ems_labels=labels))
    with pytest.raises(SystemBuildError) as exc:
        SystemBuildResolver(docker=docker).resolve("v0.8.0")
    assert exc.value.code == "system_build_mismatch"


@pytest.mark.parametrize(
    "bad",
    [
        "ghcr.io/evil/admin:latest",
        "v0.8.0; rm -rf /",
        "../v0.8.0",
        "repo/image",
        "v0.8.0$(id)",
        "",
    ],
)
def test_rejects_invalid_or_malicious_tag(bad):
    docker = FakeDocker({})
    with pytest.raises(SystemBuildError) as exc:
        SystemBuildResolver(docker=docker).resolve(bad)
    assert exc.value.code == "system_build_invalid_tag"
    assert docker.pulled == []  # nothing pulled before validation


def test_rejects_floating_development_alias():
    docker = FakeDocker(_pair(DEV_ALIAS, channel="dev"))
    with pytest.raises(SystemBuildError) as exc:
        SystemBuildResolver(docker=docker).resolve(DEV_ALIAS)
    assert exc.value.code == "system_build_dev_floating"
    assert docker.pulled == []  # rejected before any pull


@pytest.mark.parametrize("mutable_alias", (OLD_RETRY_MUTABLE_DEV_TAG, DEV_SHA_ALIAS))
def test_rejects_development_aliases_without_run_attempt(mutable_alias):
    docker = FakeDocker(_pair(mutable_alias, channel="development", build_id=mutable_alias))
    with pytest.raises(SystemBuildError) as exc:
        SystemBuildResolver(docker=docker).resolve(mutable_alias)
    assert exc.value.code == "system_build_dev_floating"
    assert docker.pulled == []


# --- alignment decision ---------------------------------------------------


def _target(canonical_tag="v0.8.0", admin_digest="sha256:v080"):
    return SystemBuild(
        requested_tag=canonical_tag,
        canonical_tag=canonical_tag,
        channel="stable",
        revision=REVISION,
        build_id="v0.8.0-f7265fc",
        admin_image=f"{ADMIN_IMAGE_REPO}:{canonical_tag}",
        admin_digest=admin_digest,
        ems_image=f"{EMS_IMAGE_REPO}:{canonical_tag}",
        ems_digest="sha256:ems",
    )


def test_alignment_same_tag_same_digest_is_aligned():
    running = ImageIdentity(image_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0", digest="sha256:v080")
    decision = decide_alignment(running, _target(), persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0")
    assert decision.decision == ALIGN_ALIGNED


def test_alignment_different_tag_same_digest_needs_retag():
    # admin:latest currently resolves to the same digest as v0.8.0, but the
    # persistent compose ref still says latest: not fully aligned.
    running = ImageIdentity(image_ref=f"{ADMIN_IMAGE_REPO}:latest", digest="sha256:v080")
    decision = decide_alignment(running, _target(), persistent_ref=f"{ADMIN_IMAGE_REPO}:latest")
    assert decision.decision == ALIGN_RETAG_REQUIRED


def test_alignment_same_tag_different_digest_needs_update():
    running = ImageIdentity(image_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0", digest="sha256:old")
    decision = decide_alignment(running, _target(), persistent_ref=f"{ADMIN_IMAGE_REPO}:v0.8.0")
    assert decision.decision == ALIGN_ADMIN_UPDATE_REQUIRED


def test_alignment_different_tag_different_digest_needs_update():
    running = ImageIdentity(image_ref=f"{ADMIN_IMAGE_REPO}:latest", digest="sha256:old")
    decision = decide_alignment(running, _target(), persistent_ref=f"{ADMIN_IMAGE_REPO}:latest")
    assert decision.decision == ALIGN_ADMIN_UPDATE_REQUIRED


# --- historical (legacy CI) build identity --------------------------------
#
# Images published before the modern build-ID contract stamp the original CI
# ``<GITHUB_RUN_ID>-<GITHUB_RUN_ATTEMPT>`` build id (e.g. ``123456789-1``). These
# are recognized as their own kind and accepted only for a strictly matched
# non-development pair; they are never treated as modern build ids.

from admin.system_build_id import (  # noqa: E402
    SystemBuildIdKind,
    parse_system_build_id,
    validate_system_build_id,
)

LEGACY_REVISION = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
LEGACY_BUILD_ID = "123456789-1"


def test_parse_recognizes_legacy_ci_build_id():
    parsed = parse_system_build_id("123456789-1")
    assert parsed.kind is SystemBuildIdKind.LEGACY_CI
    assert parsed.value == "123456789-1"
    assert parsed.is_legacy is True
    assert parsed.is_modern is False


def test_parse_distinguishes_modern_kinds_from_legacy():
    assert (
        parse_system_build_id("v0.8.0-f7265fc").kind
        is SystemBuildIdKind.MODERN_RELEASE
    )
    assert (
        parse_system_build_id("latest-f7265fc").kind is SystemBuildIdKind.MODERN_LATEST
    )
    assert parse_system_build_id(DEV_TAG).kind is SystemBuildIdKind.DEVELOPMENT
    assert parse_system_build_id("local-f7265fc").kind is SystemBuildIdKind.LOCAL


@pytest.mark.parametrize(
    "bad", ("0-1", "1-0", "01-1", "1-01", "12-", "-1", "1-2-3", "abc-1", "123456789")
)
def test_parse_rejects_malformed_legacy_numeric_ids(bad):
    with pytest.raises(ValueError):
        parse_system_build_id(bad)


def test_legacy_ci_build_id_passes_format_validation():
    assert validate_system_build_id("123456789-1") == "123456789-1"


def test_resolves_legacy_ci_pair_when_all_labels_match():
    docker = FakeDocker(
        _pair(
            "v0.7.0",
            channel="stable",
            build_id=LEGACY_BUILD_ID,
            revision=LEGACY_REVISION,
            release_tag="v0.7.0",
        )
    )
    build = SystemBuildResolver(docker=docker).resolve("v0.7.0")
    assert build.canonical_tag == "v0.7.0"
    assert build.build_id == LEGACY_BUILD_ID
    assert build.revision == LEGACY_REVISION
    assert build.channel == "stable"


def test_rejects_legacy_pair_with_different_build_ids():
    admin_labels = _labels(
        build_id="123456789-1", revision=LEGACY_REVISION,
        release_tag="v0.7.0", version="v0.7.0",
    )
    ems_labels = _labels(
        build_id="123456789-2", revision=LEGACY_REVISION,
        release_tag="v0.7.0", version="v0.7.0",
    )
    docker = FakeDocker(
        _pair("v0.7.0", admin_labels=admin_labels, ems_labels=ems_labels)
    )
    with pytest.raises(SystemBuildError) as exc:
        SystemBuildResolver(docker=docker).resolve("v0.7.0")
    assert exc.value.code == "system_build_mismatch"


def test_rejects_legacy_pair_with_different_revisions():
    admin_labels = _labels(
        build_id=LEGACY_BUILD_ID, revision=LEGACY_REVISION,
        release_tag="v0.7.0", version="v0.7.0",
    )
    ems_labels = _labels(
        build_id=LEGACY_BUILD_ID,
        revision="0000000ffff0000000000000000000000000aaaa",
        release_tag="v0.7.0", version="v0.7.0",
    )
    docker = FakeDocker(
        _pair("v0.7.0", admin_labels=admin_labels, ems_labels=ems_labels)
    )
    with pytest.raises(SystemBuildError) as exc:
        SystemBuildResolver(docker=docker).resolve("v0.7.0")
    assert exc.value.code == "system_build_mismatch"


def test_rejects_legacy_pair_with_wrong_tag():
    labels = _labels(
        build_id=LEGACY_BUILD_ID, revision=LEGACY_REVISION,
        release_tag="v0.7.0", version="v0.7.1",
    )
    docker = FakeDocker(_pair("v0.7.0", admin_labels=labels, ems_labels=labels))
    with pytest.raises(SystemBuildError) as exc:
        SystemBuildResolver(docker=docker).resolve("v0.7.0")
    assert exc.value.code == "system_build_mismatch"


def test_rejects_legacy_pair_with_wrong_release_tag():
    labels = _labels(
        build_id=LEGACY_BUILD_ID, revision=LEGACY_REVISION,
        release_tag="v0.7.1", version="v0.7.0",
    )
    docker = FakeDocker(_pair("v0.7.0", admin_labels=labels, ems_labels=labels))
    with pytest.raises(SystemBuildError) as exc:
        SystemBuildResolver(docker=docker).resolve("v0.7.0")
    assert exc.value.code == "system_build_mismatch"


def test_legacy_build_id_not_accepted_for_development_build():
    # A development identity requires build_id == tag, so a bare legacy numeric
    # id can never stand in for a development build.
    labels = _labels(
        channel="development", build_id="123456789-1",
        release_tag=DEV_TAG, version=DEV_TAG,
    )
    docker = FakeDocker(_pair(DEV_TAG, admin_labels=labels, ems_labels=labels))
    with pytest.raises(SystemBuildError) as exc:
        SystemBuildResolver(docker=docker).resolve(DEV_TAG)
    assert exc.value.code == "system_build_mismatch"


# --- compatibility mode ---------------------------------------------------

from admin.system_build import (  # noqa: E402
    COMPAT_LEGACY_RELEASE,
    COMPAT_LOCAL,
    COMPAT_MODERN_PAIRED,
    system_build_compatibility,
    system_build_keeps_current_admin,
)


def _legacy_system_build():
    return SystemBuild(
        requested_tag="v0.7.0", canonical_tag="v0.7.0", channel="stable",
        revision=LEGACY_REVISION, build_id=LEGACY_BUILD_ID,
        admin_image=f"{ADMIN_IMAGE_REPO}:v0.7.0", admin_digest="sha256:oldadmin",
        ems_image=f"{EMS_IMAGE_REPO}:v0.7.0", ems_digest="sha256:oldems",
        release_tag="v0.7.0",
    )


def _local_system_build():
    return SystemBuild(
        requested_tag="local", canonical_tag="local", channel="development",
        revision=REVISION, build_id="local-f7265fc",
        admin_image=f"{ADMIN_IMAGE_REPO}:local", admin_digest="sha256:localadmin",
        ems_image=f"{EMS_IMAGE_REPO}:local", ems_digest="sha256:localems",
        release_tag="local",
    )


def test_compatibility_mode_classifies_legacy_release():
    assert system_build_compatibility(_legacy_system_build()) == COMPAT_LEGACY_RELEASE


def test_compatibility_mode_classifies_modern_paired():
    assert system_build_compatibility(_target()) == COMPAT_MODERN_PAIRED


def test_compatibility_mode_classifies_local():
    assert system_build_compatibility(_local_system_build()) == COMPAT_LOCAL


def test_compatibility_mode_accepts_plain_dict():
    assert (
        system_build_compatibility(_legacy_system_build().as_dict())
        == COMPAT_LEGACY_RELEASE
    )


def test_legacy_release_keeps_current_admin_but_modern_does_not():
    assert system_build_keeps_current_admin(_legacy_system_build()) is True
    assert system_build_keeps_current_admin(_target()) is False
    assert system_build_keeps_current_admin(_local_system_build()) is False


# --- resource strategy ----------------------------------------------------

from admin.system_build import (  # noqa: E402
    BuildResourceStrategy,
    resource_strategy_for_compatibility,
    system_build_resource_strategy,
)


def test_resource_strategy_modern_paired_uses_embedded():
    assert (
        system_build_resource_strategy(_target())
        == BuildResourceStrategy.EMBEDDED.value
    )


def test_resource_strategy_local_uses_embedded():
    # A local checkout bakes its own embedded bundle, so it verifies embedded.
    assert (
        system_build_resource_strategy(_local_system_build())
        == BuildResourceStrategy.EMBEDDED.value
    )


def test_resource_strategy_legacy_release_uses_release_archive():
    # A legacy release predates the embedded bundle: its resources come from the
    # exact historical release archive, never the running Admin's embedded copy.
    assert (
        system_build_resource_strategy(_legacy_system_build())
        == BuildResourceStrategy.RELEASE_ARCHIVE.value
    )


def test_resource_strategy_accepts_plain_dict():
    assert (
        system_build_resource_strategy(_legacy_system_build().as_dict())
        == BuildResourceStrategy.RELEASE_ARCHIVE.value
    )


def test_resource_strategy_unknown_compatibility_fails_closed():
    with pytest.raises(SystemBuildError) as exc:
        resource_strategy_for_compatibility("not_a_real_compatibility_mode")
    assert exc.value.code == "system_build_resource_strategy_unknown"


def test_latest_tagged_local_image_is_rejected_with_actionable_error():
    # A local image (local build id) published under the rolling ``latest`` tag
    # must not be silently reinterpreted as a registry latest build.
    labels = _labels(
        channel="development", build_id="local-f7265fc",
        release_tag="latest", version="latest",
    )
    docker = FakeDocker(_pair("latest", admin_labels=labels, ems_labels=labels))
    with pytest.raises(SystemBuildError) as exc:
        SystemBuildResolver(docker=docker).resolve("latest")
    assert exc.value.code == "system_build_local_mistagged_latest"
    assert "start-admin-setup.sh" in exc.value.message


# --- digest-pinned reference helper --------------------------------------
#
# The single shared builder that combines a repository with a verified content
# digest for the immutable runtime deployment reference.


def test_digest_pinned_ref_strips_tag_and_pins_digest():
    digest = "sha256:" + "a" * 64
    assert (
        digest_pinned_ref(f"{EMS_IMAGE_REPO}:latest", digest)
        == f"{EMS_IMAGE_REPO}@{digest}"
    )


def test_digest_pinned_ref_preserves_registry_host_port():
    digest = "sha256:" + "b" * 64
    assert (
        digest_pinned_ref("registry.example:5000/project/image:tag", digest)
        == f"registry.example:5000/project/image@{digest}"
    )
    assert (
        digest_pinned_ref("registry.example:5000/project/image", digest)
        == f"registry.example:5000/project/image@{digest}"
    )


def test_digest_pinned_ref_accepts_matching_already_pinned_ref():
    digest = "sha256:" + "c" * 64
    ref = f"{EMS_IMAGE_REPO}@{digest}"
    assert digest_pinned_ref(ref, digest) == ref


def test_digest_pinned_ref_rejects_conflicting_pinned_digest():
    with pytest.raises(DigestReferenceError):
        digest_pinned_ref(
            f"{EMS_IMAGE_REPO}@sha256:" + "a" * 64, "sha256:" + "d" * 64
        )


@pytest.mark.parametrize("bad", ["", "notadigest", "sha256:", "sha256:ab/cd", "sha256:zz zz"])
def test_digest_pinned_ref_rejects_malformed_digest(bad):
    with pytest.raises(DigestReferenceError):
        digest_pinned_ref(f"{EMS_IMAGE_REPO}:latest", bad)


def test_digest_pinned_ref_requires_expected_repository():
    digest = "sha256:" + "a" * 64
    # The official repository is accepted.
    assert digest_pinned_ref(
        f"{EMS_IMAGE_REPO}:v1.0.0", digest, require_repo=EMS_IMAGE_REPO
    ) == f"{EMS_IMAGE_REPO}@{digest}"
    # An unexpected repository fails closed.
    with pytest.raises(DigestReferenceError):
        digest_pinned_ref("evil.example/x/y:tag", digest, require_repo=EMS_IMAGE_REPO)


def test_digest_pinned_ref_rejects_empty_reference():
    with pytest.raises(DigestReferenceError):
        digest_pinned_ref("", "sha256:" + "a" * 64)
