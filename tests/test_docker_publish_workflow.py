"""Static contract: published Docker images carry a monotonic build identity.

`latest` is a channel, not a version. Guided Upgrade must be able to compare
image identity (channel + monotonic serial), not tag name alone, so every
published image needs build-identity labels. These text assertions guard the
required labels and the channel-resolution rules against silent regressions.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "docker-publish.yml"


def _text():
    return PUBLISH_WORKFLOW.read_text(encoding="utf-8")


def test_build_identity_step_resolves_channel_serial_and_id():
    text = _text()
    assert "id: build_identity" in text
    # Monotonic, human-readable serial + unique build id.
    assert "build_serial=${GITHUB_RUN_NUMBER}" in text
    assert "build_id=${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" in text


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
        "org.opencontainers.image.revision=${{ github.sha }}",
        "de.basecubedev.ems.channel=${{ steps.build_identity.outputs.channel }}",
        "de.basecubedev.ems.build_serial=${{ steps.build_identity.outputs.build_serial }}",
        "de.basecubedev.ems.build_id=${{ steps.build_identity.outputs.build_id }}",
        "de.basecubedev.ems.release_tag=${{ steps.build_identity.outputs.release_tag }}",
    )
    for label in required_labels:
        assert label in text, f"missing required label: {label}"


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
