# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every workflow that pushes the EMS and Admin images pushes their source beside them.

The source image is collected from the images as pushed (their digests, not a
tag that may have moved), tagged like them, checked to be pullable without
credentials, and removed with them when a development build is pruned or its
branch is deleted.
"""

from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.contract, pytest.mark.documentation]

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
SOURCES = "ghcr.io/basecubedev/ems-solarflow-sources"
PUBLISHERS = {
    "docker-publish.yml": ("publish-ghcr", "build_id"),
    "docker-feature-publish.yml": ("publish-feature-ghcr", "immutable_tag"),
}


def steps(name):
    workflow = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return workflow["jobs"][PUBLISHERS[name][0]]["steps"]


def index(all_steps, name):
    matches = [position for position, step in enumerate(all_steps) if step.get("name") == name]
    assert len(matches) == 1, (name, [step.get("name") for step in all_steps])
    return matches[0]


@pytest.mark.parametrize("name", sorted(PUBLISHERS))
def test_the_source_is_collected_from_the_pushed_digests_and_pushed_after_both_images(name):
    all_steps = steps(name)
    ems = index(all_steps, next(step["name"] for step in all_steps if step.get("id") == "push_ems"))
    admin = index(all_steps, next(step["name"] for step in all_steps if step.get("id") == "push_admin"))
    collect = index(all_steps, "Collect the source of the images' third-party components")
    push = index(all_steps, "Push the source image beside the images")
    verify = index(all_steps, "Verify anyone can pull the source image")
    assert max(ems, admin) < collect < push < verify
    step = all_steps[collect]
    assert step["env"]["EMS_DIGEST"] == "${{ steps.push_ems.outputs.digest }}"
    assert step["env"]["ADMIN_DIGEST"] == "${{ steps.push_admin.outputs.digest }}"
    assert step["env"]["REVISION"] == "${{ steps.build_identity.outputs.git_commit }}"
    assert step["env"]["BUILD_ID"] == "${{ steps.build_identity.outputs." + PUBLISHERS[name][1] + " }}"
    run = step["run"]
    assert "scripts/image_sources.py collect" in run
    assert '--image "ghcr.io/basecubedev/ems-solarflow-api-control@${EMS_DIGEST}"' in run
    assert '--image "ghcr.io/basecubedev/ems-solarflow-admin@${ADMIN_DIGEST}"' in run
    assert '--revision "${REVISION}"' in run and '--output "${RUNNER_TEMP}/image-sources"' in run


@pytest.mark.parametrize("name", sorted(PUBLISHERS))
def test_the_source_image_carries_the_images_tags_and_its_build_identity(name):
    all_steps = steps(name)
    meta = all_steps[index(all_steps, "Generate source image metadata")]["with"]
    assert meta["images"] == SOURCES

    def lines(block):
        return {line.strip() for line in str(block or "").strip().splitlines() if line.strip()}

    for image_meta in ("meta", "meta_admin"):
        image = next(step["with"] for step in all_steps if step.get("id") == image_meta)
        assert lines(image["tags"]) <= lines(meta["tags"]), image_meta
        assert lines(image.get("flavor")) == lines(meta.get("flavor")), image_meta
    identity = PUBLISHERS[name][1]
    assert "${{ steps.build_identity.outputs." + identity + " }}" in meta["tags"]
    assert "org.opencontainers.image.revision=${{ steps.build_identity.outputs.git_commit }}" in meta["labels"]
    assert "org.opencontainers.image.source=https://github.com/basecubedev/ems-solarflow-api-control" in meta["labels"]
    push = all_steps[index(all_steps, "Push the source image beside the images")]
    assert push["id"] == "push_sources" and push["with"]["context"] == "${{ runner.temp }}/image-sources"
    verify = all_steps[index(all_steps, "Verify anyone can pull the source image")]
    assert '--image "ghcr.io/basecubedev/ems-solarflow-sources@${SOURCES_DIGEST}"' in verify["run"]
    assert verify["env"]["SOURCES_DIGEST"] == "${{ steps.push_sources.outputs.digest }}"


@pytest.mark.parametrize("name", sorted(PUBLISHERS))
def test_no_public_tag_names_an_image_before_its_source_is_pushed_and_verified(name):
    """The three images are pushed by digest only; one last step tags the source, then the images."""

    all_steps = steps(name)
    for step_id, image in (("push_ems", "ems-solarflow-api-control"), ("push_admin", "ems-solarflow-admin"), ("push_sources", "ems-solarflow-sources")):
        with_ = next(step for step in all_steps if step.get("id") == step_id)["with"]
        assert "tags" not in with_ and "push" not in with_, step_id
        assert with_["outputs"] == f"type=image,name=ghcr.io/basecubedev/{image},push-by-digest=true,name-canonical=true,push=true"
    tagging = index(all_steps, "Tag the images and their source")
    assert tagging > index(all_steps, "Verify anyone can pull the source image")
    assert not any("imagetools create" in str(step.get("run", "")) for step in all_steps[:tagging])
    step = all_steps[tagging]
    for key, value in (("EMS", "push_ems"), ("ADMIN", "push_admin"), ("SOURCES", "push_sources")):
        assert step["env"][f"{key}_DIGEST"] == "${{ steps." + value + ".outputs.digest }}"
    for key, meta in (("EMS", "meta"), ("ADMIN", "meta_admin"), ("SOURCES", "meta_sources")):
        assert step["env"][f"{key}_TAGS"] == "${{ steps." + meta + ".outputs.tags }}"
    assert step["env"]["OWN_TAG"] == "${{ steps.build_identity.outputs." + PUBLISHERS[name][1] + " }}"
    run = step["run"]
    assert "for which in own shared; do" in run
    order = [run.index(f'tag "${{{key}_DIGEST}}" "${{{key}_TAGS}}" "${{which}}"') for key in ("SOURCES", "EMS", "ADMIN")]
    assert order == sorted(order)
    assert 'docker buildx imagetools create "${args[@]}" "${image}@${digest}"' in run
    assert 'python3 scripts/image_sources.py verify --image "${line}" --digest "${digest}"' in run
    assert "::error::no tag to give" in run


def test_the_feature_pair_check_reads_the_pushed_digests_not_a_tag_that_does_not_exist_yet():
    all_steps = steps("docker-feature-publish.yml")
    pair = all_steps[index(all_steps, "Verify published Admin/EMS image pair")]
    assert pair["env"]["ADMIN_IMAGE"] == "ghcr.io/basecubedev/ems-solarflow-admin@${{ steps.push_admin.outputs.digest }}"
    assert pair["env"]["EMS_IMAGE"] == "ghcr.io/basecubedev/ems-solarflow-api-control@${{ steps.push_ems.outputs.digest }}"
    assert index(all_steps, "Verify published Admin/EMS image pair") < index(all_steps, "Collect the source of the images' third-party components")


def test_a_development_build_s_source_is_pruned_and_cleaned_up_with_its_images():
    workflow = yaml.safe_load((WORKFLOWS / "docker-feature-publish.yml").read_text(encoding="utf-8"))
    prune = next(step for step in workflow["jobs"]["prune-old-development-builds"]["steps"]
                 if step.get("name") == "Keep only the newest builds per feature branch")["run"]
    for package in ("ems-solarflow-admin", "ems-solarflow-api-control", "ems-solarflow-sources"):
        assert f"--package {package}" in prune
    assert "--keep 2" in prune and "--protect-tag" in prune and "--digest" not in prune
    cleanup = (WORKFLOWS / "docker-feature-cleanup.yml").read_text(encoding="utf-8")
    assert 'delete_matching_versions "ems-solarflow-sources"' in cleanup
    assert 'verify_no_matching_versions "ems-solarflow-sources"' in cleanup


def test_release_runs_of_one_ref_queue_so_their_tags_move_in_run_order():
    workflow = yaml.safe_load((WORKFLOWS / "docker-publish.yml").read_text(encoding="utf-8"))
    concurrency = workflow["jobs"]["publish-ghcr"]["concurrency"]
    assert concurrency == {"group": "ems-release-publish-${{ github.ref }}", "cancel-in-progress": False}


def test_a_failed_development_run_removes_the_untagged_images_it_pushed():
    all_steps = steps("docker-feature-publish.yml")
    cleanup = index(all_steps, "Remove what a failed run pushed")
    assert cleanup > index(all_steps, "Tag the images and their source")
    step = all_steps[cleanup]
    assert step["if"] == "failure() || cancelled()"
    assert step["env"]["IMMUTABLE_TAG"] == "${{ steps.build_identity.outputs.immutable_tag }}"
    for key, value in (("EMS", "push_ems"), ("ADMIN", "push_admin"), ("SOURCES", "push_sources")):
        assert step["env"][f"{key}_DIGEST"] == "${{ steps." + value + ".outputs.digest }}"
    run = step["run"]
    assert "python3 scripts/prune_development_builds.py" in run and '"${digests[@]}"' in run
    for package in ("ems-solarflow-admin", "ems-solarflow-api-control", "ems-solarflow-sources"):
        assert f"--package {package}" in run
    assert '--prefix "${FEATURE_TAG_PREFIX}"' in run and '--own-tag "${IMMUTABLE_TAG}"' in run


def test_the_branch_cleanup_keeps_a_package_s_last_tagged_version_instead_of_failing():
    cleanup = (WORKFLOWS / "docker-feature-cleanup.yml").read_text(encoding="utf-8")
    assert "grep -qi 'last tagged version'" in cleanup
    assert 'echo "${id}" >> "${package}-kept.txt"' in cleanup
    assert "select((.id as $id | $kept | index($id)) == null)" in cleanup
