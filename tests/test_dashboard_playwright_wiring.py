# SPDX-License-Identifier: AGPL-3.0-or-later
"""The cockpit browser suite has to be reachable by something.

The same rule the Appliance Manager suite already lives under, and for the same
reason: a suite nobody runs does not fail, it just stops being evidence and the
specs rot against a UI that moved on. The Manager's version of this file exists
because its config and fifty specs sat unrun for a while; this one exists so
the cockpit never earns that history.

What is different here is the server. The Manager brings a scripted test host;
the cockpit runs against scripts/serve_dashboard_preview.py, which serves the
real dashboard/static/ assets with synthetic payloads. That server is therefore
part of the suite, and a change to it has to trigger the suite.
"""

import re
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.simulation]

ROOT = Path(__file__).resolve().parents[1]
CONFIG = "playwright.dashboard.config.ts"
SPECS = ROOT / "tests" / "e2e-dashboard"
WORKFLOWS = ROOT / ".github" / "workflows"
PREVIEW = "scripts/serve_dashboard_preview.py"


def workflows_running_it():
    return [path for path in WORKFLOWS.glob("*.yml") if CONFIG in path.read_text(encoding="utf-8")]


def test_the_dashboard_config_and_its_specs_exist():
    assert (ROOT / CONFIG).is_file()
    assert list(SPECS.glob("*.spec.ts"))


def test_a_ci_job_runs_the_cockpit_browser_suite():
    assert workflows_running_it(), f"no workflow runs {CONFIG}"


def triggering_paths(document):
    """The paths a workflow's pull_request trigger watches.

    Read from the parsed document rather than from the file's text, because the
    text also contains the comments -- and a comment naming the preview server
    makes a substring check pass while the trigger ignores it. PyYAML resolves
    the bare key `on` to the boolean True (YAML 1.1), which is why both spellings
    are looked up.
    """

    trigger = document.get("on", document.get(True)) or {}
    return set((trigger.get("pull_request") or {}).get("paths") or [])


def test_the_workflow_reacts_to_the_code_those_specs_cover():
    """A job that never triggers is the same as no job."""

    for path in workflows_running_it():
        watched = triggering_paths(yaml.safe_load(path.read_text(encoding="utf-8")))
        missing = {"dashboard/**", "tests/e2e-dashboard/**", PREVIEW, CONFIG} - watched
        assert missing == set(), f"{path.name} does not trigger on: {sorted(missing)}"


def test_the_gate_refuses_a_run_in_which_the_cockpit_job_failed():
    """A job outside the gate's `needs` can fail without blocking anything,
    which is the one way to have a suite and still not have evidence."""

    for path in workflows_running_it():
        jobs = yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]
        if "gate" not in jobs:
            continue
        needed = set(jobs["gate"].get("needs") or [])
        declared = set(jobs) - {"gate"}
        assert declared <= needed, (
            f"{path.name}: outside the gate: {sorted(declared - needed)}"
        )


def test_a_local_tier_runs_it_too():
    """CI is not the only place a maintainer needs to reach it, and on a branch
    that never opens a pull request CI is not a place at all."""

    scripts = list((ROOT / "scripts").glob("test-*.sh"))
    running = [path.name for path in scripts if CONFIG in path.read_text(encoding="utf-8")]

    assert running, f"no scripts/test-*.sh runs {CONFIG}"


def test_the_cockpit_specs_carry_the_tags_the_runner_selects_on():
    tagged = [
        path.name
        for path in SPECS.glob("*.spec.ts")
        if re.search(r"test\.describe\([^)]*@(smoke|authority)", path.read_text(encoding="utf-8"))
    ]

    assert tagged, "no cockpit spec carries @smoke or @authority"


def test_every_project_the_job_runs_is_a_browser_it_installed():
    """WebKit in the projects list and not in the install step is several
    hundred "Executable doesn't exist" failures, which is how this was learned
    locally rather than in CI."""

    config = (ROOT / CONFIG).read_text(encoding="utf-8")
    projects = set(re.findall(r'name:\s*"([a-z]+)"', config))
    assert projects, "the config declares no projects"

    checked = 0
    for path in workflows_running_it():
        for name, job in yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"].items():
            steps = job.get("steps") or []
            runs = " ".join(str(step.get("run", "")) for step in steps)
            if CONFIG not in runs:
                continue
            installed = re.search(r"playwright install --with-deps ([a-z ]+)", runs)
            assert installed, f"{path.name}:{name} installs no browser"
            assert projects <= set(installed.group(1).split()), (
                f"{path.name}:{name} runs {sorted(projects)} "
                f"but installs {installed.group(1).split()}"
            )
            checked += 1
    assert checked, f"no job actually runs {CONFIG}"


def test_the_suite_does_not_share_a_port_with_another_one():
    """Playwright's `reuseExistingServer` adopts whatever already answers on the
    port instead of failing. A collision therefore does not look like a
    collision: the suite runs happily against the wrong application and reports
    that the elements it wants are missing, which on that page is true. That is
    what a stray development server on 8134 did to this suite -- sixteen tests
    red, none of them about the cockpit.

    This can only compare the configs in the repository to one another; nothing
    can defend a port against a process somebody left running. What it does
    catch is the version of that mistake which ships.
    """

    ports = {}
    for path in sorted(ROOT.glob("playwright*.config.ts")):
        # The variable names carry digits (EMS_ADMIN_E2E_PORT), so a [A-Z_]
        # class silently matches nothing and the whole check passes on an empty
        # table. It did, until a perturbation said so.
        found = re.search(
            r"PORT = Number\(process\.env\.[A-Z0-9_]+ \?\? (\d+)\)",
            path.read_text(encoding="utf-8"),
        )
        if found:
            ports.setdefault(found.group(1), []).append(path.name)

    # One pair predates this suite and is left as it stands rather than fixed
    # blind: the packaged Admin runner pins 8124 in its shell script as well as
    # its config, and it builds a container this machine cannot exercise. It is
    # named here so it stays a known debt instead of becoming invisible.
    KNOWN = {("playwright.appliance.config.ts", "playwright.packaged.config.ts")}

    shared = {
        port: names
        for port, names in ports.items()
        if len(names) > 1 and tuple(sorted(names)) not in KNOWN
    }
    assert shared == {}, f"two suites on one port: {shared}"


def test_the_preview_server_the_suite_depends_on_exists():
    """The config names it in a shell command, where nothing checks the path."""

    config = (ROOT / CONFIG).read_text(encoding="utf-8")
    assert PREVIEW in config
    assert (ROOT / PREVIEW).is_file()
