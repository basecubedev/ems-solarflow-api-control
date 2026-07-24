# SPDX-License-Identifier: AGPL-3.0-or-later
"""Caching must not let a local validation image diverge from the published one.

For every EMS/Admin x release/feature pair the local single-platform validation
build and the pushed multi-platform build must share the same Dockerfile,
context, and build args (revision, build_id, channel, release/system tag). The
only permitted differences are platform, load/push, tags and cache wiring, so a
cache setting can never become part of image identity.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / ".github" / "workflows" / "docker-publish.yml"
FEATURE = ROOT / ".github" / "workflows" / "docker-feature-publish.yml"
PACKAGED_SCRIPT = ROOT / "tests" / "e2e" / "run-packaged-admin.sh"

# Inputs that legitimately differ between a loaded single-platform validation
# build and the pushed multi-platform build. Everything else is image identity.
NON_IDENTITY_KEYS = {"platforms", "load", "push", "tags", "cache-from", "cache-to", "labels", "annotations"}

IDENTITY_LABELS = (
    "org.opencontainers.image.version",
    "org.opencontainers.image.revision",
    "de.basecubedev.ems.channel",
    "de.basecubedev.ems.build_id",
    "de.basecubedev.ems.release_tag",
)

# (workflow, job, local validation step, pushed build step)
PAIRS = (
    (RELEASE, "publish-ghcr", "Build local Docker image for content validation", "Build and push Docker image"),
    (RELEASE, "publish-ghcr", "Build local Admin image for content validation", "Build and push Admin Docker image"),
    (FEATURE, "publish-feature-ghcr", "Build local EMS image for content validation", "Build and push EMS Docker image"),
    (FEATURE, "publish-feature-ghcr", "Build local Admin image for startup validation", "Build and push Admin Docker image"),
)

PAIR_IDS = ["release-ems", "release-admin", "feature-ems", "feature-admin"]


def _load(workflow: Path) -> dict:
    return yaml.safe_load(workflow.read_text(encoding="utf-8"))


def _named(workflow: Path, job: str, step_name: str) -> dict:
    for step in _load(workflow)["jobs"][job]["steps"]:
        if step.get("name") == step_name:
            return step
    raise AssertionError(f"{workflow.name}:{job} has no step named {step_name!r}")


def _kv_block(with_block: dict, key: str) -> dict:
    values = {}
    for line in str(with_block.get(key, "")).splitlines():
        line = line.strip()
        if "=" in line:
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip()
    return values


@pytest.mark.parametrize(("workflow", "job", "local_name", "pushed_name"), PAIRS, ids=PAIR_IDS)
def test_local_validation_and_pushed_build_share_identity(workflow, job, local_name, pushed_name):
    local = _named(workflow, job, local_name)
    pushed = _named(workflow, job, pushed_name)
    local_with = local.get("with") or {}
    pushed_with = pushed.get("with") or {}

    assert local["uses"].startswith("docker/build-push-action")
    assert pushed["uses"].startswith("docker/build-push-action")
    assert local_with.get("file") == pushed_with.get("file")
    assert local_with.get("context") == pushed_with.get("context")
    assert _kv_block(local_with, "build-args") == _kv_block(pushed_with, "build-args"), (
        f"{PAIR_IDS} build args diverge between local and pushed"
    )


@pytest.mark.parametrize(("workflow", "job", "local_name", "pushed_name"), PAIRS, ids=PAIR_IDS)
def test_cache_settings_are_not_part_of_image_identity(workflow, job, local_name, pushed_name):
    local_with = _named(workflow, job, local_name).get("with") or {}
    pushed_with = _named(workflow, job, pushed_name).get("with") or {}
    differing = {k for k in set(local_with) | set(pushed_with) if local_with.get(k) != pushed_with.get(k)}
    # Whatever differs between the two builds must be non-identity wiring only.
    assert differing <= NON_IDENTITY_KEYS, f"identity-affecting keys differ: {differing - NON_IDENTITY_KEYS}"
    # The local build imports but never exports; the pushed build is the exporter.
    assert "cache-to" not in local_with
    assert "cache-to" in pushed_with


def test_release_ems_local_labels_match_published_identity():
    # The EMS local build overrides the Dockerfile's empty-on-latest version
    # label; those identity labels must equal what metadata-action publishes.
    local_labels = _kv_block(_named(RELEASE, "publish-ghcr", "Build local Docker image for content validation").get("with") or {}, "labels")
    meta_labels = _kv_block(_named(RELEASE, "publish-ghcr", "Generate Docker metadata").get("with") or {}, "labels")
    for label in IDENTITY_LABELS:
        assert local_labels.get(label) == meta_labels.get(label), (
            f"{label}: local={local_labels.get(label)!r} published={meta_labels.get(label)!r}"
        )


def _script_const(name: str) -> str:
    match = re.search(rf'^{name}="([^"]+)"', PACKAGED_SCRIPT.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"{name} not found in {PACKAGED_SCRIPT.name}"
    return match.group(1)


def _script_build_arg(name: str) -> str:
    match = re.search(rf"--build-arg {name}=(\S+)", PACKAGED_SCRIPT.read_text(encoding="utf-8"))
    assert match, f"--build-arg {name} not found in {PACKAGED_SCRIPT.name}"
    return match.group(1)


def test_packaged_browser_gate_prebuild_matches_script_identity():
    # The CI prebuild replaces the script's own build, so its tag and fixed
    # development identity must stay in lockstep with run-packaged-admin.sh.
    prebuild = _named(FEATURE, "packaged-system-build-smoke", "Prebuild packaged Admin browser-gate image")
    with_ = prebuild.get("with") or {}
    assert with_.get("load") is True
    assert with_.get("push") is not True
    assert "scope=${{ env.ADMIN_FEATURE_CACHE_SCOPE }}" in str(with_.get("cache-from", ""))

    tag = _script_const("IMAGE_NAME")
    development_tag = _script_const("DEVELOPMENT_TAG")
    revision = _script_build_arg("EMS_REVISION")

    assert str(with_.get("tags")).strip() == tag
    args = _kv_block(with_, "build-args")
    assert args.get("EMS_REVISION") == revision
    assert args.get("EMS_CHANNEL") == "development"
    for key in ("EMS_BUILD_ID", "EMS_RELEASE_TAG", "EMS_SYSTEM_TAG"):
        assert args.get(key) == development_tag, f"{key} drifted from the script"


def test_packaged_browser_gate_script_skips_build_when_prebuilt():
    # Local runs still build with plain Docker; CI opts into the prebuilt image.
    script = PACKAGED_SCRIPT.read_text(encoding="utf-8")
    assert "EMS_ADMIN_PACKAGED_SKIP_BUILD" in script
    assert "docker build -f deploy/admin/Dockerfile" in script
