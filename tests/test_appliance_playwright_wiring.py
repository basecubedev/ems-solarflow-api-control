# SPDX-License-Identifier: AGPL-3.0-or-later
"""The appliance browser suite has to be reachable by something.

playwright.appliance.config.ts and about fifty specs under tests/e2e-appliance
existed while no CI job and no local tier ever ran them. A suite nobody runs
does not fail -- it just stops being evidence, quietly, and the specs rot
against a UI that moved on.
"""

import ast
import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
CONFIG = "playwright.appliance.config.ts"
WORKFLOWS = ROOT / ".github" / "workflows"


def test_the_appliance_config_and_its_specs_exist():
    assert (ROOT / CONFIG).is_file()
    assert list((ROOT / "tests" / "e2e-appliance").glob("*.spec.ts"))


def test_a_ci_job_runs_the_appliance_browser_suite():
    referencing = [
        path.name
        for path in WORKFLOWS.glob("*.yml")
        if CONFIG in path.read_text(encoding="utf-8")
    ]

    assert referencing, f"no workflow runs {CONFIG}"


def test_the_workflow_reacts_to_the_code_those_specs_cover():
    """A job that never triggers is the same as no job."""

    referencing = [
        path for path in WORKFLOWS.glob("*.yml") if CONFIG in path.read_text(encoding="utf-8")
    ]
    watched = "\n".join(path.read_text(encoding="utf-8") for path in referencing)

    assert "appliance/**" in watched
    assert "tests/e2e-appliance/**" in watched


def test_a_local_tier_runs_it_too():
    """CI is not the only place a maintainer needs to reach it."""

    scripts = list((ROOT / "scripts").glob("test-*.sh"))
    running = [path.name for path in scripts if CONFIG in path.read_text(encoding="utf-8")]

    assert running, f"no scripts/test-*.sh runs {CONFIG}"


def test_the_appliance_specs_carry_the_tags_the_runner_selects_on():
    """The critical subsets are chosen by tag, so untagged specs run nowhere."""

    specs = (ROOT / "tests" / "e2e-appliance").glob("*.spec.ts")
    tagged = [
        path.name
        for path in specs
        if re.search(r"test\.describe\([^)]*@(smoke|authority)", path.read_text(encoding="utf-8"))
    ]

    assert tagged, "no appliance spec carries @smoke or @authority"


# --- a prerequisite is a skip; a failure is not -------------------------------


def test_the_container_tiers_skip_only_on_a_real_prerequisite():
    """A container that booted and then failed to start its units is this tier
    failing, not the environment lacking something. Reporting that as a skip is
    how a security tier disappears from a release without anyone noticing."""

    container_tiers = [
        path
        for path in (ROOT / "tests").glob("test_appliance_*.py")
        if path.name != Path(__file__).name
        and "appliance_systemd" in path.read_text(encoding="utf-8")
    ]

    assert container_tiers, "no container tier was found to check"

    offenders = []
    for path in container_tiers:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for handler in ast.walk(tree):
            if not isinstance(handler, ast.ExceptHandler):
                continue
            if not any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "skip"
                for node in ast.walk(handler)
            ):
                continue
            caught = ast.unparse(handler.type) if handler.type else "everything"
            # A prerequisite the host does not have is a skip. A tier that
            # booted and then failed is not, and a skip reports it as passing.
            if caught != "SystemdUnavailable":
                offenders.append(f"{path.name}: skips on {caught}")

    assert not offenders, offenders


def test_a_tier_failure_is_a_different_exception_from_a_missing_prerequisite():
    """The split is what keeps `except ...: pytest.skip()` from swallowing it."""

    from tests.helpers.appliance_systemd import SystemdTierFailure, SystemdUnavailable

    assert not issubclass(SystemdTierFailure, SystemdUnavailable)
    assert not issubclass(SystemdUnavailable, SystemdTierFailure)


def test_a_job_can_declare_the_container_tier_load_bearing(monkeypatch):
    """Where a green result is meant to mean these ran, a skip is a failure."""

    from tests.helpers import appliance_systemd

    monkeypatch.setenv("EMS_REQUIRE_APPLIANCE_CONTAINER_TESTS", "1")
    with pytest.raises(appliance_systemd.SystemdTierFailure):
        appliance_systemd.unavailable("docker is not available")

    monkeypatch.delenv("EMS_REQUIRE_APPLIANCE_CONTAINER_TESTS")
    with pytest.raises(appliance_systemd.SystemdUnavailable):
        appliance_systemd.unavailable("docker is not available")


def test_a_missing_package_archive_names_itself():
    """apt failing left the container booted but useless, and every check above
    it then failed for reasons that never mentioned apt."""

    helper = (ROOT / "tests/helpers/appliance_systemd.py").read_text(encoding="utf-8")

    assert "PREREQUISITE_MARKER" in helper
    assert "no package archive" in helper


# --- the job has to be able to run what it collects ---------------------------


def appliance_job():
    text = (WORKFLOWS / "playwright-e2e.yml").read_text(encoding="utf-8")
    return text.split("playwright-appliance:")[1].split("\n  playwright-")[0]


def test_every_project_the_job_runs_is_a_browser_it_installed():
    """A project that was never downloaded fails at launch, not at assertion."""

    job = appliance_job()
    config = (ROOT / CONFIG).read_text(encoding="utf-8")
    declared = set(re.findall(r'name:\s*"(\w+)"', config))
    run = [line for line in job.splitlines() if "playwright test" in line][0]
    install = [line for line in job.splitlines() if "playwright install" in line][0]

    selected = set(re.findall(r"--project=(\w+)", run)) or declared

    assert declared, "the appliance config declares no projects"
    for project in selected:
        assert project in install, f"the job runs {project} without installing it"


def test_a_spec_that_writes_into_the_repository_is_not_collected_by_default():
    """The RC tier ends in a clean-tree check; a capture run would fail it."""

    config = (ROOT / CONFIG).read_text(encoding="utf-8")
    writing = [
        path.name
        for path in (ROOT / "tests" / "e2e-appliance").glob("*.spec.ts")
        if "docs/assets" in path.read_text(encoding="utf-8")
    ]

    assert writing, "no capture spec found; this guard has lost its subject"
    for name in writing:
        assert name in config, f"{name} writes into the repository but is not excluded"
    assert "testIgnore" in config
