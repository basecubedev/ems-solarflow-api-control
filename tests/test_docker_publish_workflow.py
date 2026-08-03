"""Static contract: published Docker images carry a monotonic build identity.

`latest` is a channel, not a version. Guided Upgrade must be able to compare
image identity (channel + monotonic serial), not tag name alone, so every
published image needs build-identity labels. These text assertions guard the
required labels and the channel-resolution rules against silent regressions.
"""

from pathlib import Path

import yaml

import pytest

pytestmark = [
    pytest.mark.contract,
]

ROOT = Path(__file__).resolve().parents[1]
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "docker-publish.yml"
DOCKERFILE = ROOT / "Dockerfile"


def _publish_steps():
    workflow = yaml.safe_load(PUBLISH_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["publish-ghcr"]["steps"]


def _step(step_name):
    for step in _publish_steps():
        if step.get("name") == step_name:
            return step
    raise AssertionError(f"publish-ghcr has no step named {step_name!r}")


def _build_args(step):
    raw = (step.get("with") or {}).get("build-args", "")
    args = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        args[key.strip()] = value.strip()
    return args

# Runtime-visible build identity that CI passes into the image build so
# ems.build_info can read it without depending on OCI labels.
BUILD_IDENTITY_ARGS = (
    "EMS_RELEASE_TAG",
    "EMS_GIT_COMMIT",
    "EMS_GIT_COMMIT_SHORT",
    "EMS_GIT_DESCRIBE",
    "EMS_GIT_BRANCH",
    "EMS_GIT_DIRTY",
    "EMS_BUILD_ID",
    "EMS_BUILD_SERIAL",
    "EMS_CHANNEL",
)


def _text():
    return PUBLISH_WORKFLOW.read_text(encoding="utf-8")


def _dockerfile_text():
    return DOCKERFILE.read_text(encoding="utf-8")


def test_build_identity_step_resolves_channel_serial_and_id():
    text = _text()
    assert "id: build_identity" in text
    # Monotonic, human-readable serial + unique build id.
    assert "build_serial=${GITHUB_RUN_NUMBER}" in text
    assert (
        'echo "build_id=${release_tag}-${git_commit_short}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"'
        in text
    )


def test_channel_rules_are_deterministic():
    text = _text()
    # Tag builds split into stable vs. rc; everything else is the latest channel.
    assert 'if [[ "${GITHUB_REF}" == refs/tags/* ]]; then' in text
    assert 'if [[ "${release_tag}" == *-* ]]; then' in text
    assert 'channel="rc"' in text
    assert 'channel="stable"' in text
    assert 'channel="latest"' in text


def test_required_build_identity_labels_are_published():
    text = _text()
    required_labels = (
        "org.opencontainers.image.version=${{ steps.build_identity.outputs.release_tag }}",
        "org.opencontainers.image.revision=${{ steps.build_identity.outputs.git_commit }}",
        "de.basecubedev.ems.channel=${{ steps.build_identity.outputs.channel }}",
        "de.basecubedev.ems.build_serial=${{ steps.build_identity.outputs.build_serial }}",
        "de.basecubedev.ems.build_id=${{ steps.build_identity.outputs.build_id }}",
        "de.basecubedev.ems.release_tag=${{ steps.build_identity.outputs.release_tag }}",
    )
    for label in required_labels:
        assert label in text, f"missing required label: {label}"


def test_release_identity_uses_the_checked_out_commit_everywhere():
    text = _text()
    assert 'git_commit="$(git rev-parse --verify HEAD)"' in text
    assert 'echo "git_commit=${git_commit}"' in text
    assert "${{ github.sha }}" not in text


def test_created_timestamp_comes_from_metadata_action():
    # org.opencontainers.image.created is emitted automatically by
    # docker/metadata-action; the build-push step must consume its labels.
    text = _text()
    assert "uses: docker/metadata-action@" in text
    assert "labels: ${{ steps.meta.outputs.labels }}" in text


def test_existing_image_tags_are_unchanged():
    text = _text()
    assert "type=ref,event=tag" in text
    assert (
        "type=raw,value=latest,enable=${{ github.ref == 'refs/heads/main' }}" in text
    )


def test_dockerfile_declares_runtime_build_identity_args_and_env():
    text = _dockerfile_text()
    for name in BUILD_IDENTITY_ARGS:
        assert f"ARG {name}=" in text, f"missing ARG {name}"
        assert f"ENV {name}=${name}" in text, f"missing ENV {name}"


def test_release_tag_env_is_empty_for_non_tag_builds():
    text = _text()
    # `latest`/main/schedule builds must not pass a channel name as the runtime
    # release version; the ems_release_tag output stays empty for them.
    assert 'ems_release_tag=""' in text
    assert 'echo "ems_release_tag=${ems_release_tag}"' in text
    # Tag builds still carry the real tag.
    assert 'ems_release_tag="${release_tag}"' in text


def test_workflow_forwards_build_identity_to_local_and_pushed_images():
    # Both the local content-validation build and the pushed multi-platform
    # build are Buildx action steps; caching them must not let their runtime
    # identity drift apart. Compare the semantic build-arg inputs, not the raw
    # shell that either step happens to use.
    local = _build_args(_step("Build local Docker image for content validation"))
    pushed = _build_args(_step("Build and push Docker image"))
    for name in BUILD_IDENTITY_ARGS:
        assert name in local, f"local build missing {name}"
        assert name in pushed, f"pushed build missing {name}"
        assert local[name] == pushed[name], (
            f"{name} differs: local={local[name]!r} pushed={pushed[name]!r}"
        )
    assert local["EMS_RELEASE_TAG"] == "${{ steps.build_identity.outputs.ems_release_tag }}"


def test_release_pair_verification_checks_complete_oci_and_bundle_identity():
    text = _text()
    start = text.index("      - name: Verify Admin/EMS system-build pair metadata")
    end = text.index("      - name: Build and push Admin Docker image", start)
    verification = text[start:end]

    for label in (
        "org.opencontainers.image.version",
        "org.opencontainers.image.revision",
        "de.basecubedev.ems.channel",
        "de.basecubedev.ems.build_id",
        "de.basecubedev.ems.release_tag",
    ):
        assert verification.count(label) >= 2, label
    assert "/app/release-resources/system-build.json" in verification
    assert "/app/release-resources/resource-manifest.json" in verification
    for field in (
        "system_tag",
        "channel",
        "revision",
        "build_id",
        "release_tag",
        "admin_image",
        "ems_image",
    ):
        assert verification.count(f"['{field}']") >= 2, field
