# SPDX-License-Identifier: AGPL-3.0-or-later
"""The real Admin Docker context must contain every repository COPY source."""

from pathlib import Path

import pytest

from docker_build_context_contract import (
    DockerIgnore,
    repository_copy_sources,
    validate_repository_copy_sources,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.contract,
    pytest.mark.simulation,
]

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "deploy" / "admin" / "Dockerfile"
EMS_DOCKERFILE = ROOT / "Dockerfile"

# Local secret / runtime / generated artifacts a real working-directory build
# must never leak into the Docker context.
EXCLUDED_LOCAL_PATHS = (
    ".env",
    ".env.local",
    "config/config.json",
    "config/secrets/.secret-key",
    "config/secrets/zendure-cloud.json",
    "config/secrets/mqtt-zendure-cloud.json",
    "config/dashboard-auth.json",
    "config/influxdb.env",
    "data/ems_dashboard.sqlite",
    "data/runtime-state.json",
    "data/admin/state/zendure-cloud-token.json",
    "data/admin/state/.admin-secret-key",
    "runtime-state.json",
    "backup/ems-config-manual-2026-06-20.tar.gz",
    "deploy/docker/influxdb.env",
    "node_modules/playwright/index.js",
    ".venv/bin/python",
    "venv/bin/python",
    "reports/test-coverage-overview.md",
    "playwright-report/index.html",
    "test-results/results.xml",
    ".ruff_cache/CACHEDIR.TAG",
    ".mypy_cache/x.json",
    ".coverage",
    "coverage.xml",
    "htmlcov/index.html",
    ".claude/settings.json",
)

# Representative required build-context sources that must survive .dockerignore
# whether or not the working directory is dirty.
REQUIRED_CONTEXT_PATHS = (
    "config/config.template.json",
    "scripts/influx_utils.py",
    "scripts/generate_release_resources.py",
    "docker-compose.example.yml",
    "install-docker.sh",
    "deploy/docker/compose.influxdb.yml",
    "deploy/docker/influxdb.env.example",
    "ems/controller.py",
    "admin/server.py",
    "dashboard/auth.py",
    "requirements.txt",
    "docker-entrypoint.sh",
)


def _ignore():
    return DockerIgnore(ROOT / ".dockerignore")


def test_admin_dockerfile_copy_sources_exist_in_effective_build_context():
    issues = validate_repository_copy_sources(
        context_root=ROOT,
        dockerfile=DOCKERFILE,
        dockerignore=ROOT / ".dockerignore",
    )

    assert not issues, "\n".join(str(issue) for issue in issues)


def test_embedded_resource_generator_is_a_verified_context_source():
    sources = {source for _, source in repository_copy_sources(DOCKERFILE)}
    generator = "scripts/generate_release_resources.py"
    assert generator in sources

    issues = validate_repository_copy_sources(
        context_root=ROOT,
        dockerfile=DOCKERFILE,
        dockerignore=ROOT / ".dockerignore",
    )
    generator_issues = [issue for issue in issues if issue.source == generator]
    assert not generator_issues, "\n".join(str(issue) for issue in generator_issues)


def test_admin_dockerfile_binds_release_tag_into_embedded_descriptor():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert '--release-tag "${EMS_RELEASE_TAG}"' in dockerfile


def test_ems_dockerfile_copy_sources_exist_in_effective_build_context():
    issues = validate_repository_copy_sources(
        context_root=ROOT,
        dockerfile=EMS_DOCKERFILE,
        dockerignore=ROOT / ".dockerignore",
    )
    assert not issues, "\n".join(str(issue) for issue in issues)


def test_local_secret_and_runtime_paths_are_excluded_from_docker_context():
    ignore = _ignore()
    leaked = [path for path in EXCLUDED_LOCAL_PATHS if not ignore.is_ignored(path)]
    assert not leaked, "these local paths would leak into the Docker context: " + ", ".join(
        leaked
    )


def test_required_build_sources_survive_dockerignore():
    ignore = _ignore()
    dropped = [path for path in REQUIRED_CONTEXT_PATHS if ignore.is_ignored(path)]
    assert not dropped, "these required build sources are excluded: " + ", ".join(dropped)


def test_context_resolves_the_same_required_sources_clean_or_dirty():
    # The required-source set is invariant: every real file COPY source of both
    # images survives .dockerignore, and every representative local artifact is
    # excluded — so a clean checkout and a dirty working directory (which only
    # adds excluded artifacts) resolve to the same required build context.
    ignore = _ignore()
    for dockerfile in (EMS_DOCKERFILE, DOCKERFILE):
        for _, source in repository_copy_sources(dockerfile):
            if source.startswith("$") or any(character in source for character in "*?["):
                continue
            if (ROOT / source).is_file():
                assert not ignore.is_ignored(source), source
    for path in EXCLUDED_LOCAL_PATHS:
        assert ignore.is_ignored(path), path
