"""Static contract: real-Docker e2e tests run in their own CI job, and the
pull-request workflow runs partitioned functional groups instead of one
monolithic suite. This guards against Docker-marked tests either running twice
or silently disappearing, and against a group quietly dropping out of CI."""

from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.contract,
]

ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "simulated-regression-tests.yml"
NIGHTLY_WORKFLOW = ROOT / ".github" / "workflows" / "nightly-full-suite.yml"
PLAYWRIGHT_WORKFLOW = ROOT / ".github" / "workflows" / "playwright-e2e.yml"
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "docker-publish.yml"
CANARY_WORKFLOW = ROOT / ".github" / "workflows" / "admin-replacement-canary.yml"

# Mirrors scripts/test-pr.sh; ownership is exclusive, see
# tests/test_test_classification.py.
PR_GROUPS = ("core", "admin", "mqtt", "power-control")


def _stripped_lines(path):
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]


def test_pull_request_workflow_runs_every_functional_group():
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    for group in PR_GROUPS:
        assert f"- {group}\n" in text, group
    assert "./scripts/test-pr.sh ${{ matrix.group }}" in text


def test_pull_request_workflow_does_not_run_the_whole_suite_twice():
    lines = _stripped_lines(CI_WORKFLOW)
    assert "pytest -q tests/" not in lines
    assert 'pytest -q -m "not docker" tests/' not in lines


def test_nightly_workflow_runs_the_full_non_docker_suite():
    text = NIGHTLY_WORKFLOW.read_text(encoding="utf-8")
    assert 'pytest -q -m "not docker" tests/' in text
    assert "pytest -q tests/" not in _stripped_lines(NIGHTLY_WORKFLOW)


def test_publish_full_suite_excludes_docker_marked_tests():
    text = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
    assert 'pytest -q -m "not docker" tests/' in text
    assert "pytest -q tests/" not in _stripped_lines(PUBLISH_WORKFLOW)


def test_dedicated_job_runs_docker_marked_tests():
    assert "pytest -q -rs -m docker tests/" in CI_WORKFLOW.read_text(encoding="utf-8")


def test_nightly_keeps_a_docker_first_job():
    assert "pytest -q -rs -m docker tests/" in NIGHTLY_WORKFLOW.read_text(encoding="utf-8")


def test_browser_groups_are_split_between_pull_request_and_nightly():
    pr_text = PLAYWRIGHT_WORKFLOW.read_text(encoding="utf-8")
    nightly_text = NIGHTLY_WORKFLOW.read_text(encoding="utf-8")

    assert 'npx playwright test --project=chromium --grep "@smoke|@authority"' in pr_text
    assert 'npx playwright test --project=firefox --grep "@smoke"' in pr_text
    assert "npx playwright test --project=${{ matrix.browser }}" in nightly_text


def test_no_job_suppresses_failures_unconditionally():
    for workflow in (CI_WORKFLOW, NIGHTLY_WORKFLOW, PLAYWRIGHT_WORKFLOW, CANARY_WORKFLOW):
        for line in _stripped_lines(workflow):
            assert line != "continue-on-error: true", workflow.name


def test_only_the_canary_workflow_runs_the_admin_replacement_config():
    # The replacement journey may only run against immutable digests resolved
    # from the Development catalogue, so exactly one workflow owns it.
    marker = "playwright.admin-replacement.config.ts"
    owners = [
        path.name
        for path in ROOT.glob(".github/workflows/*.yml")
        if marker in path.read_text(encoding="utf-8")
    ]
    assert owners == ["admin-replacement-canary.yml"], owners


def test_canary_resolves_immutable_digests_for_both_replacement_sides():
    text = CANARY_WORKFLOW.read_text(encoding="utf-8")
    assert "CANARY_SOURCE_ADMIN_DIGEST: ${{ steps.pair.outputs.source_admin_digest }}" in text
    assert "CANARY_ADMIN_DIGEST: ${{ steps.pair.outputs.target_admin_digest }}" in text
    assert "CANARY_EMS_DIGEST: ${{ steps.pair.outputs.target_ems_digest }}" in text
    # An absent pair, a mutable digest or one image replacing itself are blocked
    # preconditions inside the resolver, never a skip.
    assert "scripts/resolve_canary_builds.py" in text
    assert text.count("verify_development_catalogue.py") == 2
    assert "continue-on-error" not in text


def _runs_pytest(step):
    for line in step.get("run", "").splitlines():
        stripped = line.strip()
        if stripped.startswith(("pytest ", "python -m pytest", "python3 -m pytest")):
            return True
    return False


def _reports_selection(step):
    run = step.get("run", "")
    return "scripts/test-pr.sh" in run or "printf" in run and "pytest" in run


def test_every_pytest_job_reports_its_selection():
    import yaml

    for workflow in (CI_WORKFLOW, NIGHTLY_WORKFLOW):
        jobs = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]
        for name, job in jobs.items():
            steps = job.get("steps", [])
            if not any(_runs_pytest(step) for step in steps):
                continue
            assert any(_reports_selection(step) for step in steps), (
                f"{workflow.name}:{name} runs pytest without reporting its selection"
            )
