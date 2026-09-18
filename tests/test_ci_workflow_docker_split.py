"""Static contract: real-Docker e2e tests run in their own CI job, and the
pull-request workflow runs partitioned functional groups instead of one
monolithic suite. This guards against Docker-marked tests either running twice
or silently disappearing, and against a group quietly dropping out of CI."""

from pathlib import Path

import pytest
import yaml

pytestmark = [
    pytest.mark.contract,
]

ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "simulated-regression-tests.yml"
PLAYWRIGHT_WORKFLOW = ROOT / ".github" / "workflows" / "playwright-e2e.yml"
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "docker-publish.yml"
FEATURE_WORKFLOW = ROOT / ".github" / "workflows" / "docker-feature-publish.yml"
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


def test_a_pull_request_never_runs_the_whole_suite_twice():
    """The groups partition the collection, so an unpartitioned run alongside
    them is the same tests a second time. It exists -- see the test below -- but
    a pull request must not reach it."""

    assert "pytest -q tests/" not in _stripped_lines(CI_WORKFLOW)

    jobs = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    unpartitioned = [
        name
        for name, job in jobs.items()
        if any(
            'pytest -q -m "not docker" tests/' in step.get("run", "")
            for step in job.get("steps", [])
        )
    ]
    assert unpartitioned, "nothing runs the suite unpartitioned any more"
    for name in unpartitioned:
        assert jobs[name].get("if") == "github.event_name == 'push'", (
            f"{name} runs the whole suite and is reachable from a pull request, "
            "which is the same tests the five groups already ran"
        )


def test_the_full_non_docker_suite_still_runs_somewhere():
    """It moved out of a nightly schedule and onto the merge. One process rather
    than five is the only place an interaction between two tests shows up, so
    losing it would cost a class of failure the groups cannot see."""

    text = CI_WORKFLOW.read_text(encoding="utf-8")
    assert 'pytest -q -m "not docker" tests/' in text
    assert (
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -W error::DeprecationWarning "
        "-m pytest -q -m \"not docker\" tests/" in text
    ), "the strict deprecation run is not duplicated anywhere else"


def test_publish_full_suite_excludes_docker_marked_tests():
    text = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
    assert 'pytest -q -m "not docker" tests/' in text
    assert "pytest -q tests/" not in _stripped_lines(PUBLISH_WORKFLOW)


def test_dedicated_job_runs_docker_marked_tests():
    assert "pytest -q -rs -m docker tests/" in CI_WORKFLOW.read_text(encoding="utf-8")


def test_browser_groups_are_split_between_the_pull_request_and_the_merge():
    text = PLAYWRIGHT_WORKFLOW.read_text(encoding="utf-8")

    assert 'npx playwright test --project=chromium --grep "@smoke|@authority"' in text
    assert 'npx playwright test --project=firefox --grep "@smoke"' in text
    assert "npx playwright test --project=${{ matrix.browser }}" in text

    jobs = yaml.safe_load(text)["jobs"]
    ungrepped = [
        name
        for name, job in jobs.items()
        if any(
            step.get("run", "").strip() == "npx playwright test --project=${{ matrix.browser }}"
            for step in job.get("steps", [])
        )
    ]
    assert ungrepped, "no job runs the browser projects in full any more"
    for name in ungrepped:
        assert jobs[name].get("if") == "github.event_name == 'push'", (
            f"{name} runs the full browser projects on a pull request, which is "
            "what the critical and smoke slices exist to avoid"
        )


def test_no_job_suppresses_failures_unconditionally():
    for workflow in (CI_WORKFLOW, PLAYWRIGHT_WORKFLOW, CANARY_WORKFLOW):
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
    for workflow in (CI_WORKFLOW,):
        jobs = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]
        for name, job in jobs.items():
            steps = job.get("steps", [])
            if not any(_runs_pytest(step) for step in steps):
                continue
            assert any(_reports_selection(step) for step in steps), (
                f"{workflow.name}:{name} runs pytest without reporting its selection"
            )


# --- the pair check must see what the upgrade policy reads -----------------
#
# `de.basecubedev.ems.contains_release` and `.build_serial` decide every
# upgrade and rollback verdict, and SystemBuildResolver refuses a pair whose
# two images disagree about them. The publish workflow builds a local pair
# precisely to catch a stamping mistake before anything is pushed -- but the
# two validation images were not given those labels, so the one guard that
# could catch it at build time was blind to exactly them, and a mistake would
# surface on an operator's machine as system_build_mismatch instead.

POLICY_LABELS = (
    "de.basecubedev.ems.contains_release",
    "de.basecubedev.ems.build_serial",
)

# Every workflow that builds a local pair to validate before it pushes, and the
# step in it that compares the two images.
PAIR_CHECKS = [
    (PUBLISH_WORKFLOW, "Verify Admin/EMS system-build pair metadata"),
    (FEATURE_WORKFLOW, "Verify Admin/EMS feature-build pair metadata"),
]


def _workflow_steps(path):
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        yield from job.get("steps", [])


def _metadata_labels(path):
    """Every ``docker/metadata-action`` step's label block, by step id."""

    return {
        step["id"]: step.get("with", {}).get("labels", "")
        for step in _workflow_steps(path)
        if "docker/metadata-action" in str(step.get("uses", "")) and step.get("id")
    }


def _image_builds(path):
    """Every image that ships or stands in for one that ships.

    ``(step name, pushed, raw labels, the labels it stamps)`` for each pushed
    build and for each local ``:ci`` build the pair check inspects. Other
    local builds are fixtures with an identity of their own -- the packaged
    browser gate -- and belong to no pair.
    """

    metadata = _metadata_labels(path)
    builds = []
    for step in _workflow_steps(path):
        if "docker/build-push-action" not in str(step.get("uses", "")):
            continue
        with_ = step.get("with", {})
        pushed = bool(with_.get("push", False))
        if not pushed and not str(with_.get("tags", "")).strip().endswith(":ci"):
            continue
        raw = str(with_.get("labels", ""))
        labels = raw
        for step_id, block in metadata.items():
            labels = labels.replace(
                "${{ steps." + step_id + ".outputs.labels }}", str(block)
            )
        builds.append((step.get("name", "<unnamed>"), pushed, raw, labels))
    return builds


@pytest.mark.parametrize("path", [PUBLISH_WORKFLOW, FEATURE_WORKFLOW], ids=lambda p: p.name)
def test_every_published_image_stamps_the_labels_the_policy_reads(path):
    builds = _image_builds(path)
    assert builds, f"{path.name} builds no images"
    for name, _pushed, _raw, labels in builds:
        for label in POLICY_LABELS:
            assert label in labels, f"{path.name}: {name} does not stamp {label}"


@pytest.mark.parametrize("path", [PUBLISH_WORKFLOW, FEATURE_WORKFLOW], ids=lambda p: p.name)
def test_a_validation_image_is_labelled_exactly_as_the_pushed_one(path):
    """The pair check reads what the validation image was stamped with.

    A label block copied by hand into the validation build can differ from
    the block the pushed image gets, and then the check passes an image that
    never ships and misses the one that does. One source: the metadata step.
    """

    metadata = _metadata_labels(path)
    sources = {"${{ steps." + step_id + ".outputs.labels }}" for step_id in metadata}
    for name, pushed, raw, _labels in _image_builds(path):
        if pushed:
            continue
        assert raw.strip() in sources, (
            f"{path.name}: {name} stamps a label block of its own instead of "
            "the metadata step's"
        )


@pytest.mark.parametrize("path, step_name", PAIR_CHECKS, ids=lambda v: getattr(v, "name", v))
def test_the_pair_check_compares_the_labels_the_policy_reads(path, step_name):
    text = path.read_text(encoding="utf-8")
    assert step_name in text, f"{path.name} has no step {step_name!r}"
    verify = text.split(step_name, 1)[1]
    verify = verify.split("\n      - name:", 1)[0]
    for label in POLICY_LABELS:
        assert label in verify, f"{path.name}: the pair check never reads {label}"
