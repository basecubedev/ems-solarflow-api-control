# SPDX-License-Identifier: AGPL-3.0-or-later
"""CI must require the real paired-image Docker/startup smoke contract."""

from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.system_build,
    pytest.mark.contract,
    pytest.mark.simulation,
]

ROOT = Path(__file__).resolve().parents[1]
DOCKER_CONTRACT = ROOT / "tests" / "test_system_build_docker_contract.py"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "simulated-regression-tests.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "docker-publish.yml"
FEATURE_WORKFLOW = ROOT / ".github" / "workflows" / "docker-feature-publish.yml"


def test_real_paired_system_build_docker_contract_exists():
    text = DOCKER_CONTRACT.read_text(encoding="utf-8")
    assert '"docker", "build"' in text
    assert 'str(ADMIN_DOCKERFILE)' in text
    assert 'str(ROOT / "Dockerfile")' in text
    assert "org.opencontainers.image.revision" in text
    assert "/app/release-resources/system-build.json" in text
    assert "/app/release-resources/resource-manifest.json" in text
    assert "start-admin-setup.sh" in text
    assert "/api/admin/auth/status" in text


def test_pull_request_ci_requires_all_docker_marked_contracts():
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    assert 'pytest -q -rs -m docker tests/' in text


@pytest.mark.parametrize("workflow", (RELEASE_WORKFLOW, FEATURE_WORKFLOW))
def test_publish_workflows_require_paired_startup_smoke(workflow):
    text = workflow.read_text(encoding="utf-8")
    assert "test_system_build_docker_contract.py" in text, (
        f"{workflow.name} can publish images without running the paired Docker/startup contract"
    )
    assert "-rs" in text, f"{workflow.name} must report Docker-unavailable skips explicitly"
