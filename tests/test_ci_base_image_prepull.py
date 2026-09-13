# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static contract: a registry hiccup is not a red branch.

Both base images of this repository's Dockerfiles are pulled anonymously from
Docker Hub, and on 2026-09-12 the token endpoint reset the connection twice in
a row. Both runs died in "Run System Build compatibility gate" with "failed to
resolve source metadata for docker.io/library/python:3.14-slim"; the third run
passed with nothing changed. The PR gate refuses a run in which anything did
not succeed, so the whole branch went red for a network event.

The fix is a split rather than a retry. ``scripts/prepull_base_images.py``
takes the registry work into a step of its own that may retry, and the build
that follows keeps failing on its first try -- a broken Dockerfile must not be
attempted three times before it is reported. What holds the split together is
that ``docker build`` without ``--pull`` resolves a tag already in the local
image store without contacting the registry, so once the pull step is green
the build has no registry left to fail against.

Two ways that decays, and what is pinned here against each:

a job that builds and forgets to pull
    A fourth job is not on anyone's list. So the jobs are not listed: a step is
    read as building when it runs ``docker build``, or when it runs pytest with
    a marker expression that actually selects one of the Docker-marked modules.
    That second half is why ``-m system_build`` counts -- the paired-image
    Docker contract carries both markers -- and why ``-m "simulation and
    power_control"`` does not.

the pull list drifting from the Dockerfiles
    A bump to ``python:3.15-slim`` that leaves a pre-pull of ``3.14-slim``
    behind is a pre-pull that still passes and covers nothing. The script reads
    the ``FROM`` lines instead of keeping a list, and that is checked here
    against the Dockerfiles rather than against a copy of the same list.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.prepull_base_images import (
    DOCKERFILES,
    UnresolvedBaseImage,
    base_images,
    collect,
    pull,
    resolve_dockerfiles,
)

pytestmark = [pytest.mark.contract]

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

PREPULL = "scripts/prepull_base_images.py"

# A ``-m`` on the same line as a pytest invocation, once continuations are
# joined. ``python -m pip`` has no pytest on its line and is not matched.
PYTEST_MARKERS = re.compile(r"pytest\b[^\n]*?-m\s+(?:\"([^\"]*)\"|'([^']*)'|(\S+))")
PRINTS_ONLY = re.compile(r"\s*(printf|echo)\b")
FROM_REFERENCE = re.compile(r"^\s*FROM\s+(.+)$", re.IGNORECASE | re.MULTILINE)


# --- reading the workflows -------------------------------------------------


def triggers(document):
    """``on:`` is the YAML 1.1 boolean ``True`` unless it was quoted."""

    return document.get("on") or document.get(True) or {}


def pull_request_workflows():
    """Every workflow whose failure turns a pull request red."""

    found = []
    for path in WORKFLOWS:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if "pull_request" in (triggers(document) or {}):
            found.append((path, document))
    return found


def step_script(step):
    """One step's shell, with continuations joined and pure output lines
    dropped -- a step that only prints ``pytest ... -m docker`` runs nothing."""

    script = (step.get("run") or "").replace("\\\n", " ")
    return "\n".join(
        line for line in script.splitlines() if not PRINTS_ONLY.match(line)
    )


# --- which marker expressions reach a Docker-marked module -----------------


def module_markers(path):
    """Marker names in a module's ``pytestmark``. Module level only: that is
    where tests/test_test_classification.py requires classification to live."""

    names = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in node.targets
        ):
            continue
        for sub in ast.walk(node.value):
            if (
                isinstance(sub, ast.Attribute)
                and isinstance(sub.value, ast.Attribute)
                and isinstance(sub.value.value, ast.Name)
                and sub.value.value.id == "pytest"
                and sub.value.attr == "mark"
            ):
                names.add(sub.attr)
    return names


def docker_marked_modules():
    found = {}
    for path in sorted(TESTS.glob("test_*.py")):
        markers = module_markers(path)
        if "docker" in markers:
            found[path.name] = markers
    return found


def evaluate(expression, markers):
    """Evaluate a pytest marker expression against one module's markers.

    Only the shapes pytest's own ``-m`` grammar uses are understood. Anything
    else raises rather than resolving to something convenient: an expression
    this cannot read is an expression this cannot vouch for."""

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Name):
            return node.id in markers
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not walk(node.operand)
        if isinstance(node, ast.BoolOp):
            results = [walk(value) for value in node.values]
            return all(results) if isinstance(node.op, ast.And) else any(results)
        raise AssertionError(
            f"marker expression {expression!r} uses a form this contract "
            "cannot evaluate; teach it the form or the check is decorative"
        )

    return walk(ast.parse(expression, mode="eval"))


def selects_a_docker_module(expression, modules):
    return any(evaluate(expression, markers) for markers in modules.values())


def building_steps(job, modules):
    """Indices of the steps in one job that can reach a ``docker build``."""

    found = []
    for index, step in enumerate(job.get("steps") or []):
        script = step_script(step)
        if "docker build" in script:
            found.append(index)
            continue
        expressions = [
            next(group for group in match.groups() if group is not None)
            for match in PYTEST_MARKERS.finditer(script)
        ]
        if any(selects_a_docker_module(expr, modules) for expr in expressions):
            found.append(index)
    return found


# --- the workflow contract -------------------------------------------------


def test_the_docker_marked_modules_are_found():
    """The check below compares a marker expression against this set. An empty
    set would make every expression innocent and every assertion vacuous."""

    modules = docker_marked_modules()
    assert "test_system_build_docker_contract.py" in modules, (
        "the paired-image Docker contract is the module that made "
        "-m system_build build images; losing it here would silence the check"
    )
    assert modules["test_system_build_docker_contract.py"] >= {"docker", "system_build"}


# The jobs known to build one of this repository's images. Named so that a
# detector that quietly stops recognising a build fails here instead of
# checking nothing and passing. A new building job is caught by the loop, not
# by this set, so adding one does not mean editing this list.
KNOWN_BUILDING_JOBS = {
    "system-build-compatibility",
    "docker-smoke-test",
    "docker-first-e2e",
}


def test_every_pull_request_job_that_can_build_an_image_pulls_first():
    modules = docker_marked_modules()
    workflows = pull_request_workflows()
    assert workflows, "no workflow runs on pull_request any more"

    checked = set()
    for path, document in workflows:
        for name, job in (document.get("jobs") or {}).items():
            steps = job.get("steps") or []
            builds = building_steps(job, modules)
            if not builds:
                continue
            checked.add(name)
            pulls = [
                index
                for index, step in enumerate(steps)
                if PREPULL in step_script(step)
            ]
            assert pulls, (
                f"{path.name}: job {name!r} builds a repository image without "
                f"pulling its base images first; add a step running {PREPULL}"
            )
            assert min(pulls) < min(builds), (
                f"{path.name}: job {name!r} pulls the base images in step "
                f"{min(pulls)}, after the build in step {min(builds)}"
            )

    assert KNOWN_BUILDING_JOBS <= checked, (
        "these jobs build an image and were not recognised as building one, so "
        "nothing above was proved about them: "
        f"{sorted(KNOWN_BUILDING_JOBS - checked)}"
    )


def test_the_build_itself_is_never_retried():
    """The pull is retried because pulling is all it does. A loop around a
    build would turn a broken Dockerfile into a slow flake instead of a fast
    failure, which is the outcome this whole change exists to avoid."""

    for path, document in pull_request_workflows():
        for name, job in (document.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                script = step_script(step)
                if "docker build" not in script and "pytest" not in script:
                    continue
                assert "sleep" not in script, (
                    f"{path.name}: job {name!r} step {step.get('name')!r} "
                    "builds or tests inside something that waits and repeats"
                )


# --- the script's list of images -------------------------------------------


def tracked_dockerfiles():
    listed = subprocess.run(
        ["git", "ls-files", "--", "*Dockerfile", "*Dockerfile.*", "*/Dockerfile"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return {
        line
        for line in listed.stdout.split()
        if line and not line.startswith("tests/fixtures/")
    }


def test_the_prepull_covers_every_dockerfile_this_repository_builds():
    assert tracked_dockerfiles() == set(DOCKERFILES)


def test_the_base_images_are_read_out_of_the_dockerfiles():
    """Not compared against a second copy of the same list: the Dockerfiles are
    re-read here, so a base image bump that misses the pre-pull cannot pass."""

    expected = set()
    for name in DOCKERFILES:
        text = (ROOT / name).read_text(encoding="utf-8")
        for words in FROM_REFERENCE.findall(text):
            reference = next(
                (word for word in words.split() if not word.startswith("--")), ""
            )
            if reference and reference.lower() != "scratch":
                expected.add(reference)

    assert expected, "no FROM lines were read out of the Dockerfiles"
    assert set(collect(resolve_dockerfiles(DOCKERFILES))) == expected


def test_a_stage_reference_is_not_mistaken_for_a_registry_read(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM influxdb:2.9 AS influxdb-cli\n"
        "FROM scratch AS empty\n"
        "FROM influxdb-cli AS again\n"
        "FROM --platform=linux/amd64 python:3.14-slim\n",
        encoding="utf-8",
    )
    assert base_images(dockerfile) == ["influxdb:2.9", "python:3.14-slim"]


def test_a_from_built_from_an_argument_is_an_error_not_a_skip(tmp_path):
    """Skipping it would leave a base image uncovered while the step stays
    green, which is indistinguishable from the fix working."""

    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM ${BASE_IMAGE}\n", encoding="utf-8")
    with pytest.raises(UnresolvedBaseImage):
        base_images(dockerfile)


# --- the retry itself ------------------------------------------------------


class FakeDocker:
    """Fails the first ``failures`` pulls, then succeeds."""

    def __init__(self, failures):
        self.failures = failures
        self.calls = []
        self.waited = []

    def run(self, argv):
        self.calls.append(argv)
        return 1 if len(self.calls) <= self.failures else 0

    def sleep(self, seconds):
        self.waited.append(seconds)


def test_a_transient_registry_failure_is_retried():
    docker = FakeDocker(failures=2)
    assert pull("python:3.14-slim", run=docker.run, sleep=docker.sleep, log=lambda *_: None)
    assert len(docker.calls) == 3
    assert docker.calls[0] == ["docker", "pull", "python:3.14-slim"]
    assert docker.waited == [3, 6], "the wait must grow between attempts"


def test_a_registry_that_stays_down_gives_up_and_says_so():
    docker = FakeDocker(failures=99)
    assert not pull(
        "python:3.14-slim", attempts=4, run=docker.run, sleep=docker.sleep,
        log=lambda *_: None,
    )
    assert len(docker.calls) == 4, "the retry is bounded"
    assert len(docker.waited) == 3, "nothing waits after the last attempt"


def test_a_pull_that_works_first_time_does_not_wait():
    docker = FakeDocker(failures=0)
    assert pull("python:3.14-slim", run=docker.run, sleep=docker.sleep)
    assert len(docker.calls) == 1
    assert docker.waited == []
