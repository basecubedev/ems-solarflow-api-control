# SPDX-License-Identifier: AGPL-3.0-or-later
"""Image build-identity label parsing and ``docker image inspect`` handling.

No Docker daemon, network, or real images are required: ``DockerCli`` runs
against an injected ``run`` callable, and the parsing helpers are pure.
"""

import json
from types import SimpleNamespace

import pytest

from admin.deployment import DockerCli
from admin.image_identity import (
    ALREADY_CURRENT,
    DOWNGRADE_BLOCKED,
    IDENTITY_UNKNOWN,
    LEGACY_UNVERIFIED,
    OLDER_THAN_RUNNING_BUILD,
    UPGRADE_AVAILABLE,
    ImageIdentity,
    assess_upgrade,
    from_inspect,
    identify_image,
    parse_labels,
)

pytestmark = pytest.mark.simulation


IMAGE_REF = "ghcr.io/basecubedev/ems-solarflow-api-control:latest"
DIGEST = "sha256:" + "a" * 64


def _full_labels():
    return {
        "org.opencontainers.image.version": "latest",
        "org.opencontainers.image.revision": "cafef00d",
        "de.basecubedev.ems.channel": "latest",
        "de.basecubedev.ems.build_serial": "128",
        "de.basecubedev.ems.build_id": "42-1",
        "de.basecubedev.ems.release_tag": "latest",
    }


def _inspect_object(labels=None, repo_digests=None, repo_tags=None):
    return {
        "Id": DIGEST,
        "RepoTags": repo_tags if repo_tags is not None else [IMAGE_REF],
        "RepoDigests": (
            repo_digests
            if repo_digests is not None
            else [f"ghcr.io/basecubedev/ems-solarflow-api-control@{DIGEST}"]
        ),
        "Config": {"Labels": _full_labels() if labels is None else labels},
    }


# --- label parsing -------------------------------------------------------


def test_parse_labels_reads_every_build_identity_field():
    identity = parse_labels(_full_labels(), image_ref=IMAGE_REF, digest=DIGEST)

    assert identity.image_ref == IMAGE_REF
    assert identity.digest == DIGEST
    assert identity.version_label == "latest"
    assert identity.revision == "cafef00d"
    assert identity.channel == "latest"
    assert identity.build_serial == 128
    assert identity.build_id == "42-1"
    assert identity.release_tag == "latest"
    assert identity.labels["de.basecubedev.ems.channel"] == "latest"


def test_parse_labels_missing_labels_yield_none_not_traceback():
    identity = parse_labels({}, image_ref=IMAGE_REF)

    assert identity.image_ref == IMAGE_REF
    assert identity.version_label is None
    assert identity.channel is None
    assert identity.build_serial is None
    assert identity.build_id is None
    assert identity.release_tag is None
    assert identity.labels == {}


def test_parse_labels_non_mapping_input_is_all_unknown():
    identity = parse_labels(None)

    assert identity == ImageIdentity()
    assert identity.build_serial is None


@pytest.mark.parametrize("bad_serial", ["", "  ", "not-a-number", "12.5", "1_000x"])
def test_parse_labels_bad_build_serial_is_unknown_not_crash(bad_serial):
    labels = dict(_full_labels())
    labels["de.basecubedev.ems.build_serial"] = bad_serial

    identity = parse_labels(labels)

    assert identity.build_serial is None
    # A bad serial must not poison the other fields.
    assert identity.channel == "latest"


def test_parse_labels_blank_values_and_whitespace_are_trimmed():
    labels = {
        "org.opencontainers.image.version": "  v0.6.1  ",
        "de.basecubedev.ems.channel": "   ",
    }

    identity = parse_labels(labels)

    assert identity.version_label == "v0.6.1"
    assert identity.channel is None


def test_as_dict_is_json_serializable_and_copies_labels():
    identity = parse_labels(_full_labels(), image_ref=IMAGE_REF, digest=DIGEST)

    view = identity.as_dict()
    json.dumps(view)  # must not raise
    view["labels"]["mutated"] = "x"
    assert "mutated" not in identity.labels


# --- docker image inspect result parsing ---------------------------------


def _docker_returning(stdout, returncode=0):
    calls = []

    def _run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    return DockerCli(run=_run), calls


def test_inspect_image_returns_sanitized_labels_and_digest():
    docker, calls = _docker_returning(json.dumps([_inspect_object()]))

    result = docker.inspect_image(IMAGE_REF)

    assert calls == [["docker", "image", "inspect", IMAGE_REF]]
    assert result["image_ref"] == IMAGE_REF
    assert result["digest"] == DIGEST
    assert result["repo_digests"] == [
        f"ghcr.io/basecubedev/ems-solarflow-api-control@{DIGEST}"
    ]
    assert result["labels"]["de.basecubedev.ems.build_serial"] == "128"

    identity = from_inspect(result)
    assert identity.build_serial == 128
    assert identity.channel == "latest"
    assert identity.digest == DIGEST


def test_inspect_image_without_repo_digests_reports_none_digest():
    docker, _ = _docker_returning(
        json.dumps([_inspect_object(repo_digests=[])])
    )

    result = docker.inspect_image(IMAGE_REF)

    assert result["digest"] is None
    assert result["repo_digests"] == []
    assert result["id"] == DIGEST


def test_inspect_image_missing_labels_section_yields_empty_labels():
    entry = _inspect_object()
    del entry["Config"]["Labels"]
    docker, _ = _docker_returning(json.dumps([entry]))

    result = docker.inspect_image(IMAGE_REF)

    assert result["labels"] == {}
    assert from_inspect(result).channel is None


def test_inspect_image_missing_image_returns_none_on_nonzero_exit():
    docker, _ = _docker_returning("[]", returncode=1)

    assert docker.inspect_image(IMAGE_REF) is None


def test_inspect_image_empty_array_returns_none():
    docker, _ = _docker_returning("[]")

    assert docker.inspect_image(IMAGE_REF) is None


def test_inspect_image_invalid_json_returns_none():
    docker, _ = _docker_returning("not json")

    assert docker.inspect_image(IMAGE_REF) is None


def test_inspect_image_blank_ref_does_not_shell_out():
    calls = []

    def _run(command, **_kwargs):  # pragma: no cover - must not be called
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    assert DockerCli(run=_run).inspect_image("  ") is None
    assert calls == []


def test_inspect_image_missing_docker_cli_returns_none_not_raise():
    def _run(*_args, **_kwargs):
        raise FileNotFoundError("docker")

    assert DockerCli(run=_run).inspect_image(IMAGE_REF) is None


def test_inspect_image_daemon_error_returns_none_not_raise():
    def _run(*_args, **_kwargs):
        raise OSError("cannot connect to the docker daemon")

    assert DockerCli(run=_run).inspect_image(IMAGE_REF) is None


# --- identify_image glue -------------------------------------------------


def test_identify_image_inspects_via_docker_and_parses():
    docker, calls = _docker_returning(json.dumps([_inspect_object()]))

    identity = identify_image(docker, IMAGE_REF)

    assert calls == [["docker", "image", "inspect", IMAGE_REF]]
    assert identity.channel == "latest"
    assert identity.build_serial == 128


def test_identify_image_unavailable_keeps_ref_and_stays_unknown():
    class _Unavailable:
        def inspect_image(self, _ref):
            return None

    identity = identify_image(_Unavailable(), IMAGE_REF)

    assert identity.image_ref == IMAGE_REF
    assert identity.channel is None
    assert identity.build_serial is None


def test_identify_image_without_inspect_capability_is_safe():
    identity = identify_image(object(), IMAGE_REF)

    assert identity == ImageIdentity(image_ref=IMAGE_REF)


# --- upgrade assessment --------------------------------------------------


def _v(*parts):
    """A comparable SemVer key: core triple plus the release-marker suffix."""

    return (*parts, (1,))


def test_assess_same_digest_is_already_current_regardless_of_versions():
    current = ImageIdentity(digest=DIGEST, build_serial=1000)
    target = ImageIdentity(digest=DIGEST, build_serial=2000)

    result = assess_upgrade(
        current, target, current_version=_v(0, 6, 0), target_version=_v(0, 7, 0)
    )

    assert result.state == ALREADY_CURRENT
    assert result.basis == "digest"
    assert result.is_noop and not result.blocked


def test_assess_higher_semver_is_upgrade():
    result = assess_upgrade(
        ImageIdentity(), ImageIdentity(),
        current_version=_v(0, 6, 0), target_version=_v(0, 6, 1),
    )

    assert result.state == UPGRADE_AVAILABLE
    assert result.is_upgrade and result.basis == "semver"


def test_assess_equal_semver_is_already_current():
    result = assess_upgrade(
        ImageIdentity(), ImageIdentity(),
        current_version=_v(0, 6, 1), target_version=_v(0, 6, 1),
    )

    assert result.state == ALREADY_CURRENT


def test_assess_semver_downgrade_beats_newer_build_serial():
    # v0.7.0 -> v0.6.9 stays a downgrade even though the target build is newer.
    current = ImageIdentity(build_serial=1)
    target = ImageIdentity(build_serial=9999)

    result = assess_upgrade(
        current, target, current_version=_v(0, 7, 0), target_version=_v(0, 6, 9)
    )

    assert result.state == DOWNGRADE_BLOCKED
    assert result.blocked and result.basis == "semver"


def test_assess_uses_build_serial_when_a_side_is_latest():
    # Running ``latest`` has no comparable SemVer, so the build serial decides.
    current = ImageIdentity(channel="latest", build_serial=1200)

    newer = assess_upgrade(
        current, ImageIdentity(build_serial=1300),
        current_version=None, target_version=_v(0, 7, 0),
    )
    older = assess_upgrade(
        current, ImageIdentity(build_serial=1180),
        current_version=None, target_version=_v(0, 6, 9),
    )
    same = assess_upgrade(
        current, ImageIdentity(build_serial=1200),
        current_version=None, target_version=_v(0, 6, 9),
    )

    assert newer.state == UPGRADE_AVAILABLE and newer.basis == "build_serial"
    assert older.state == OLDER_THAN_RUNNING_BUILD and older.blocked
    assert same.state == ALREADY_CURRENT


def test_assess_unknown_when_nothing_can_prove_an_upgrade():
    # Running ``latest`` (no SemVer) and a target whose build serial is unknown.
    result = assess_upgrade(
        ImageIdentity(channel="latest", build_serial=1200),
        ImageIdentity(),
        current_version=None, target_version=_v(0, 6, 0),
    )

    assert result.state == IDENTITY_UNKNOWN
    assert result.blocked and result.basis == "none"


# --- legacy metadata fallbacks ------------------------------------------


def test_assess_legacy_semver_upgrade_carries_a_warning():
    # Neither side has a build serial (pre-labels images), so SemVer settles it
    # and the verdict flags that the check fell back to SemVer.
    result = assess_upgrade(
        ImageIdentity(digest="sha256:a"), ImageIdentity(),
        current_version=_v(0, 6, 0), target_version=_v(0, 6, 1),
    )

    assert result.state == UPGRADE_AVAILABLE
    assert result.basis == "semver"
    assert result.is_upgrade
    assert result.warning  # "Legacy image metadata missing ... SemVer fallback."


def test_assess_override_allows_unverifiable_legacy_with_warning():
    # Running ``latest`` (known serial) to a legacy stable with no labels: the
    # override turns the otherwise-unknown verdict into an allowed upgrade.
    result = assess_upgrade(
        ImageIdentity(channel="latest", build_serial=1200),
        ImageIdentity(digest="sha256:s"),
        current_version=None, target_version=_v(0, 6, 0),
        allow_unverified=True,
    )

    assert result.state == UPGRADE_AVAILABLE
    assert result.basis == LEGACY_UNVERIFIED
    assert result.is_upgrade and result.warning


def test_assess_override_is_ignored_without_the_flag():
    result = assess_upgrade(
        ImageIdentity(channel="latest", build_serial=1200),
        ImageIdentity(digest="sha256:s"),
        current_version=None, target_version=_v(0, 6, 0),
        allow_unverified=False,
    )

    assert result.state == IDENTITY_UNKNOWN
    assert result.blocked


def test_assess_override_never_relaxes_a_semver_downgrade():
    # A SemVer-proven downgrade stays blocked even with the override enabled.
    result = assess_upgrade(
        ImageIdentity(digest="sha256:a"), ImageIdentity(digest="sha256:b"),
        current_version=_v(0, 6, 1), target_version=_v(0, 6, 0),
        allow_unverified=True,
    )

    assert result.state == DOWNGRADE_BLOCKED
    assert result.blocked and result.warning is None


def test_assess_target_latest_is_always_a_forward_channel_move():
    # ``latest`` is a rolling channel: moving to it is a forward move even when
    # its local build serial is lower than the running release (regression: the
    # only forward target must never dead-end the release list).
    result = assess_upgrade(
        ImageIdentity(digest="sha256:run", build_serial=1300),
        ImageIdentity(digest="sha256:lat", build_serial=1250, channel="latest"),
        current_version=_v(0, 6, 1), target_version=None,
        target_rolling=True,
    )

    assert result.state == UPGRADE_AVAILABLE
    assert result.basis == "channel"
    assert result.is_upgrade and not result.blocked


def test_assess_target_latest_same_digest_is_still_already_current():
    # Identical bits are a no-op even for the rolling channel (digest wins).
    shared = "sha256:" + "e" * 64
    result = assess_upgrade(
        ImageIdentity(digest=shared, build_serial=1300),
        ImageIdentity(digest=shared, build_serial=1300, channel="latest"),
        current_version=_v(0, 6, 1), target_version=None,
        target_rolling=True,
    )

    assert result.state == ALREADY_CURRENT
    assert result.basis == "digest"


def test_assess_build_serial_wins_over_override_when_labels_present():
    # With serials on both sides the serial decides; the override never applies.
    result = assess_upgrade(
        ImageIdentity(channel="latest", build_serial=1200),
        ImageIdentity(build_serial=1100),
        current_version=None, target_version=_v(0, 6, 0),
        allow_unverified=True,
    )

    assert result.state == OLDER_THAN_RUNNING_BUILD
    assert result.basis == "build_serial"
    assert result.blocked
