"""Static contract: real-Docker e2e tests run in their own CI job, not the
normal full test suite. This guards against Docker-marked tests either running
twice (once in the full suite, once in the dedicated job) or silently
disappearing from CI."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "simulated-regression-tests.yml"
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "docker-publish.yml"


def _stripped_lines(path):
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]


def test_full_suite_excludes_docker_marked_tests():
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    assert 'pytest -q -m "not docker" tests/' in text
    assert "pytest -q tests/" not in _stripped_lines(CI_WORKFLOW)


def test_publish_workflow_delegates_testing_to_ci():
    # The release publish workflow no longer re-runs the Python suite: every
    # image it builds is a commit on main (tags are verified against main),
    # already gated by the CI workflow. Guard that no pytest run creeps back in
    # and that the main-only safety gate the delegation relies on stays.
    text = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
    assert "pytest" not in text
    assert "Verify tag commit is on main" in text


def test_dedicated_job_runs_docker_marked_tests():
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "pytest -q -rs -m docker tests/" in text
