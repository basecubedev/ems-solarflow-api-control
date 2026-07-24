# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static contract: release and feature image builds reuse isolated BuildKit caches.

Caching is a performance optimization only. These contracts prove the cache is
scoped, stable, and owned so a cache hit can never leak identity between images
or pipelines, and never replaces a validation or publication gate. Steps are
isolated from the parsed workflow and their effective ``with:`` inputs are
checked, not an accidental block of YAML text.

Cache ownership (why the scopes are isolated):
* EMS and Admin never share a scope, so an EMS layer can never satisfy an Admin
  build or vice versa.
* release and feature pipelines never share a scope, so a testing-only feature
  build can never write into the release cache.
* the ``-v1`` suffix is a manual reset lever; scopes carry no SHA/run/tag, so a
  scope stays stable across commits and workflow attempts and actually reuses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / ".github" / "workflows" / "docker-publish.yml"
FEATURE = ROOT / ".github" / "workflows" / "docker-feature-publish.yml"

BUILD_PUSH_ACTION = "docker/build-push-action"

# The four owned scopes. Names are literal so a rename is a deliberate reset.
EMS_RELEASE_SCOPE = "ems-release-v1"
ADMIN_RELEASE_SCOPE = "admin-release-v1"
EMS_FEATURE_SCOPE = "ems-feature-v1"
ADMIN_FEATURE_SCOPE = "admin-feature-v1"

RELEASE_SCOPE_ENV = {
    "EMS_RELEASE_CACHE_SCOPE": EMS_RELEASE_SCOPE,
    "ADMIN_RELEASE_CACHE_SCOPE": ADMIN_RELEASE_SCOPE,
}
FEATURE_SCOPE_ENV = {
    "EMS_FEATURE_CACHE_SCOPE": EMS_FEATURE_SCOPE,
    "ADMIN_FEATURE_CACHE_SCOPE": ADMIN_FEATURE_SCOPE,
}

# Values that must never appear inside a cache scope: they would mint a fresh,
# empty cache for every build and defeat reuse entirely.
VOLATILE_TOKENS = (
    "github.sha",
    "github.ref",
    "github.run_id",
    "github.run_number",
    "github.run_attempt",
    "github.head_ref",
    "steps.build_identity",
    "steps.smoke_identity",
    "GITHUB_SHA",
    "GITHUB_RUN_ID",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_RUN_NUMBER",
)


def _load(workflow: Path) -> dict:
    return yaml.safe_load(workflow.read_text(encoding="utf-8"))


def _steps(workflow: Path, job: str) -> list[dict]:
    return _load(workflow)["jobs"][job]["steps"]


def _iter_jobs(workflow: Path):
    for job, definition in _load(workflow)["jobs"].items():
        yield job, definition.get("steps", [])


def _named(workflow: Path, job: str, step_name: str) -> dict:
    for step in _steps(workflow, job):
        if step.get("name") == step_name:
            return step
    raise AssertionError(f"{workflow.name}:{job} has no step named {step_name!r}")


def _build_steps(workflow: Path, job: str) -> list[dict]:
    return [
        step
        for step in _steps(workflow, job)
        if isinstance(step.get("uses"), str) and step["uses"].startswith(BUILD_PUSH_ACTION)
    ]


def _with(step: dict) -> dict:
    return step.get("with") or {}


def _cache_from(step: dict) -> str:
    return str(_with(step).get("cache-from", ""))


def _cache_to(step: dict) -> str:
    return str(_with(step).get("cache-to", ""))


# --- scope ownership -------------------------------------------------------


def test_release_workflow_defines_stable_release_scopes():
    env = _load(RELEASE).get("env") or {}
    for key, value in RELEASE_SCOPE_ENV.items():
        assert env.get(key) == value, f"{key} must be {value!r}, got {env.get(key)!r}"


def test_feature_workflow_defines_stable_feature_scopes():
    env = _load(FEATURE).get("env") or {}
    for key, value in FEATURE_SCOPE_ENV.items():
        assert env.get(key) == value, f"{key} must be {value!r}, got {env.get(key)!r}"


def test_ems_and_admin_scopes_are_distinct():
    assert EMS_RELEASE_SCOPE != ADMIN_RELEASE_SCOPE
    assert EMS_FEATURE_SCOPE != ADMIN_FEATURE_SCOPE


def test_release_and_feature_scopes_are_distinct():
    release = {EMS_RELEASE_SCOPE, ADMIN_RELEASE_SCOPE}
    feature = {EMS_FEATURE_SCOPE, ADMIN_FEATURE_SCOPE}
    assert release.isdisjoint(feature)
    assert len(release | feature) == 4


@pytest.mark.parametrize("scope", (EMS_RELEASE_SCOPE, ADMIN_RELEASE_SCOPE, EMS_FEATURE_SCOPE, ADMIN_FEATURE_SCOPE))
def test_scope_values_carry_no_volatile_interpolation(scope):
    assert "${{" not in scope
    for token in VOLATILE_TOKENS:
        assert token not in scope, f"scope {scope!r} must not embed {token}"


@pytest.mark.parametrize("workflow", (RELEASE, FEATURE))
def test_cache_scope_references_use_workflow_env_not_volatile_values(workflow):
    for job, steps in _iter_jobs(workflow):
        for step in steps:
            for value in (_cache_from(step), _cache_to(step)):
                if not value:
                    continue
                assert "type=gha" in value, f"{workflow.name}:{job} must use the gha backend"
                for token in VOLATILE_TOKENS:
                    assert token not in value, f"{workflow.name}:{job} cache embeds {token}"


# --- release pipeline (docker-publish.yml) ---------------------------------

RELEASE_JOB = "publish-ghcr"


def test_release_ems_local_validation_imports_release_scope_only():
    step = _named(RELEASE, RELEASE_JOB, "Build local Docker image for content validation")
    assert step["uses"].startswith(BUILD_PUSH_ACTION)
    with_ = _with(step)
    assert with_.get("load") is True
    assert with_.get("push") is False
    assert with_.get("platforms") == "linux/amd64"
    assert "scope=${{ env.EMS_RELEASE_CACHE_SCOPE }}" in _cache_from(step)
    assert not _cache_to(step), "local validation must import but never export"


def test_release_ems_publish_imports_and_exports_release_scope_max():
    step = _named(RELEASE, RELEASE_JOB, "Build and push Docker image")
    with_ = _with(step)
    assert with_.get("push") is True
    assert with_.get("platforms") == "linux/amd64,linux/arm64"
    assert "scope=${{ env.EMS_RELEASE_CACHE_SCOPE }}" in _cache_from(step)
    assert "scope=${{ env.EMS_RELEASE_CACHE_SCOPE }}" in _cache_to(step)
    assert "mode=max" in _cache_to(step)


def test_release_admin_local_validation_imports_release_scope_only():
    step = _named(RELEASE, RELEASE_JOB, "Build local Admin image for content validation")
    assert step["uses"].startswith(BUILD_PUSH_ACTION)
    with_ = _with(step)
    assert with_.get("load") is True
    assert with_.get("push") is False
    assert with_.get("platforms") == "linux/amd64"
    assert "scope=${{ env.ADMIN_RELEASE_CACHE_SCOPE }}" in _cache_from(step)
    assert not _cache_to(step)


def test_release_admin_publish_imports_and_exports_release_scope_max():
    step = _named(RELEASE, RELEASE_JOB, "Build and push Admin Docker image")
    with_ = _with(step)
    assert with_.get("push") is True
    assert with_.get("platforms") == "linux/amd64,linux/arm64"
    assert "scope=${{ env.ADMIN_RELEASE_CACHE_SCOPE }}" in _cache_from(step)
    assert "scope=${{ env.ADMIN_RELEASE_CACHE_SCOPE }}" in _cache_to(step)
    assert "mode=max" in _cache_to(step)


def test_release_ems_and_admin_never_share_a_cache_scope():
    ems_local = _cache_from(_named(RELEASE, RELEASE_JOB, "Build local Docker image for content validation"))
    ems_push = _cache_to(_named(RELEASE, RELEASE_JOB, "Build and push Docker image"))
    admin_local = _cache_from(_named(RELEASE, RELEASE_JOB, "Build local Admin image for content validation"))
    admin_push = _cache_to(_named(RELEASE, RELEASE_JOB, "Build and push Admin Docker image"))
    assert "EMS_RELEASE_CACHE_SCOPE" in ems_local and "EMS_RELEASE_CACHE_SCOPE" in ems_push
    assert "ADMIN_RELEASE_CACHE_SCOPE" in admin_local and "ADMIN_RELEASE_CACHE_SCOPE" in admin_push
    assert "ADMIN" not in ems_local and "ADMIN" not in ems_push
    assert "EMS_RELEASE" not in admin_local and "EMS_RELEASE" not in admin_push


def test_release_uses_one_buildx_builder():
    uses = [s.get("uses", "") for s in _steps(RELEASE, RELEASE_JOB)]
    assert sum(u.startswith("docker/setup-buildx-action") for u in uses) == 1


# --- feature package-smoke warms the feature caches ------------------------

PACKAGE_SMOKE_JOB = "package-smoke"


def test_package_smoke_sets_up_buildx():
    uses = [s.get("uses", "") for s in _steps(FEATURE, PACKAGE_SMOKE_JOB)]
    assert sum(u.startswith("docker/setup-buildx-action") for u in uses) == 1


def test_package_smoke_ems_build_imports_and_exports_feature_scope():
    step = _named(FEATURE, PACKAGE_SMOKE_JOB, "Build EMS package image")
    with_ = _with(step)
    assert with_.get("load") is True
    assert with_.get("platforms") == "linux/amd64"
    assert "scope=${{ env.EMS_FEATURE_CACHE_SCOPE }}" in _cache_from(step)
    assert "scope=${{ env.EMS_FEATURE_CACHE_SCOPE }}" in _cache_to(step)
    assert "mode=max" in _cache_to(step)


def test_package_smoke_admin_build_imports_and_exports_feature_scope():
    step = _named(FEATURE, PACKAGE_SMOKE_JOB, "Build Admin package image")
    with_ = _with(step)
    assert with_.get("load") is True
    assert with_.get("platforms") == "linux/amd64"
    assert "scope=${{ env.ADMIN_FEATURE_CACHE_SCOPE }}" in _cache_from(step)
    assert "scope=${{ env.ADMIN_FEATURE_CACHE_SCOPE }}" in _cache_to(step)
    assert "mode=max" in _cache_to(step)


# --- feature publishing job (publish-feature-ghcr) -------------------------

PUBLISH_JOB = "publish-feature-ghcr"


def test_feature_ems_local_validation_imports_feature_scope_only():
    step = _named(FEATURE, PUBLISH_JOB, "Build local EMS image for content validation")
    assert step["uses"].startswith(BUILD_PUSH_ACTION)
    with_ = _with(step)
    assert with_.get("load") is True
    assert with_.get("push") is False
    assert with_.get("platforms") == "linux/amd64"
    assert "scope=${{ env.EMS_FEATURE_CACHE_SCOPE }}" in _cache_from(step)
    assert not _cache_to(step)


def test_feature_ems_publish_imports_and_exports_feature_scope_max():
    step = _named(FEATURE, PUBLISH_JOB, "Build and push EMS Docker image")
    with_ = _with(step)
    assert with_.get("push") is True
    assert with_.get("platforms") == "linux/amd64,linux/arm64"
    assert "scope=${{ env.EMS_FEATURE_CACHE_SCOPE }}" in _cache_from(step)
    assert "scope=${{ env.EMS_FEATURE_CACHE_SCOPE }}" in _cache_to(step)
    assert "mode=max" in _cache_to(step)


def test_feature_admin_local_validation_imports_feature_scope_only():
    step = _named(FEATURE, PUBLISH_JOB, "Build local Admin image for startup validation")
    assert step["uses"].startswith(BUILD_PUSH_ACTION)
    with_ = _with(step)
    assert with_.get("load") is True
    assert with_.get("push") is False
    assert with_.get("platforms") == "linux/amd64"
    assert "scope=${{ env.ADMIN_FEATURE_CACHE_SCOPE }}" in _cache_from(step)
    assert not _cache_to(step)


def test_feature_admin_publish_imports_and_exports_feature_scope_max():
    step = _named(FEATURE, PUBLISH_JOB, "Build and push Admin Docker image")
    with_ = _with(step)
    assert with_.get("push") is True
    assert with_.get("platforms") == "linux/amd64,linux/arm64"
    assert "scope=${{ env.ADMIN_FEATURE_CACHE_SCOPE }}" in _cache_from(step)
    assert "scope=${{ env.ADMIN_FEATURE_CACHE_SCOPE }}" in _cache_to(step)
    assert "mode=max" in _cache_to(step)


def test_feature_ems_and_admin_never_share_a_cache_scope():
    ems = _cache_to(_named(FEATURE, PUBLISH_JOB, "Build and push EMS Docker image"))
    admin = _cache_to(_named(FEATURE, PUBLISH_JOB, "Build and push Admin Docker image"))
    assert "EMS_FEATURE_CACHE_SCOPE" in ems and "ADMIN" not in ems
    assert "ADMIN_FEATURE_CACHE_SCOPE" in admin and "EMS_FEATURE" not in admin


# --- general invariants ----------------------------------------------------


@pytest.mark.parametrize("workflow", (RELEASE, FEATURE))
def test_no_docker_layer_directory_is_cached_with_actions_cache(workflow):
    for job, steps in _iter_jobs(workflow):
        for step in steps:
            assert not str(step.get("uses", "")).startswith("actions/cache"), (
                f"{workflow.name}:{job} must not archive Docker state with actions/cache"
            )


@pytest.mark.parametrize("workflow", (RELEASE, FEATURE))
def test_every_build_push_action_step_uses_the_gha_backend(workflow):
    for job, steps in _iter_jobs(workflow):
        for step in steps:
            if not str(step.get("uses", "")).startswith(BUILD_PUSH_ACTION):
                continue
            cache_from = _cache_from(step)
            assert cache_from, f"{workflow.name}:{job} build has no cache-from"
            assert "type=gha" in cache_from
            if _cache_to(step):
                assert "type=gha" in _cache_to(step)


@pytest.mark.parametrize("workflow", (RELEASE, FEATURE))
def test_pushed_builds_are_multiplatform_and_local_builds_single_platform(workflow):
    for job, steps in _iter_jobs(workflow):
        for step in steps:
            if not str(step.get("uses", "")).startswith(BUILD_PUSH_ACTION):
                continue
            with_ = _with(step)
            if with_.get("push") is True:
                assert with_.get("platforms") == "linux/amd64,linux/arm64", (
                    f"{workflow.name}:{job} pushed build must stay multi-platform"
                )
                assert with_.get("load") is not True
            else:
                assert with_.get("load") is True, f"{workflow.name}:{job} local build must load"
                assert with_.get("platforms") == "linux/amd64", (
                    f"{workflow.name}:{job} local build must stay single-platform"
                )


@pytest.mark.parametrize("workflow", (RELEASE, FEATURE))
def test_only_final_multiplatform_builds_export_cache(workflow):
    for job, steps in _iter_jobs(workflow):
        for step in steps:
            if not str(step.get("uses", "")).startswith(BUILD_PUSH_ACTION):
                continue
            with_ = _with(step)
            if not _cache_to(step):
                continue
            # A cache exporter is either a pushed multi-platform build or the
            # package-smoke warmers that deliberately seed the feature caches.
            is_publish = with_.get("push") is True
            is_smoke_warmer = job == PACKAGE_SMOKE_JOB
            assert is_publish or is_smoke_warmer, (
                f"{workflow.name}:{job} local validation build must not export cache"
            )
