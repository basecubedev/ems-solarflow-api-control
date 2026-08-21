# SPDX-License-Identifier: AGPL-3.0-or-later
"""The appliance browser suite has to be reachable by something.

playwright.appliance.config.ts and about fifty specs under tests/e2e-appliance
existed while no CI job and no local tier ever ran them. A suite nobody runs
does not fail -- it just stops being evidence, quietly, and the specs rot
against a UI that moved on.
"""

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.simulation]

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
        and "SystemdContainer" in path.read_text(encoding="utf-8")
    ]

    assert container_tiers, "no container tier was found to check"

    offenders = []
    for path in container_tiers:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "pytest.skip(" not in line:
                continue
            # A prerequisite the host does not have. Anything else is this tier
            # failing, and a skip reports that as passing.
            if not any(token in line for token in ("docker", "str(exc)")):
                offenders.append(f"{path.name}: {line.strip()[:90]}")

    assert not offenders, offenders


def test_a_missing_package_archive_names_itself():
    """apt failing left the container booted but useless, and every check above
    it then failed for reasons that never mentioned apt."""

    helper = (ROOT / "tests/helpers/appliance_systemd.py").read_text(encoding="utf-8")

    assert "PREREQUISITE_MARKER" in helper
    assert "no package archive" in helper
