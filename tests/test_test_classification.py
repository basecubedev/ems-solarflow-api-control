# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contracts for the test-selection interface itself.

Marker names, tier expressions and CI groups are a public developer contract:
a renamed module or a dropped ``pytestmark`` must fail here instead of silently
shrinking a tier. Everything below is a *collection* check, so the cost stays
independent of how long the selected tests take to run.

See ``docs/developer/testing.md``.
"""
import configparser
import functools
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.contract,
    pytest.mark.simulation,
    # Each check spawns its own collection, so the module costs about a minute:
    # too slow for the fast developer loop, still enforced by the PR groups.
    pytest.mark.slow,
]

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
PYTEST_INI = ROOT / "pytest.ini"

LEVELS = frozenset({"unit", "contract", "integration", "e2e"})
EXECUTION = frozenset({"docker", "browser", "slow"})
AREAS = frozenset({
    "admin", "setup", "maintenance", "workflow", "authority", "config",
    "mqtt", "power_control", "backup_restore", "system_build",
})
LEGACY = frozenset({"simulation", "regression", "mqtt_release"})

# The pull-request groups from scripts/test-pr.sh. `core` is deliberately the
# complement of the functional groups so that an unclassified module still runs.
PR_GROUPS = {
    "core": "not docker and not admin and not mqtt and not power_control",
    "admin": "admin and not docker",
    "mqtt": "mqtt and not docker",
    "power-control": "power_control and not docker",
    "docker": "docker",
}


@functools.cache
def collect(expression=None):
    """Node IDs pytest selects for one marker expression (all when None)."""
    selection = ["-m", expression] if expression else []
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "--collect-only", "-q",
            "-p", "no:cacheprovider", *selection,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in (0, 5), result.stdout + result.stderr
    return frozenset(
        line.strip()
        for line in result.stdout.splitlines()
        if "::" in line and not line.startswith(" ")
    )


def registered_markers():
    parser = configparser.ConfigParser()
    parser.read(PYTEST_INI, encoding="utf-8")
    entries = parser["pytest"]["markers"].strip().splitlines()
    return {line.split(":", 1)[0].strip() for line in entries if line.strip()}


def module_of(node_id):
    return Path(node_id.split("::", 1)[0]).stem


# --- marker registry -------------------------------------------------------


def test_registered_markers_cover_both_dimensions():
    markers = registered_markers()
    assert LEVELS <= markers
    assert EXECUTION <= markers
    assert AREAS <= markers
    assert LEGACY <= markers, "existing markers must not be removed without migration"


def test_registered_marker_names_are_stable_identifiers():
    for name in registered_markers():
        assert re.fullmatch(r"[a-z][a-z0-9_]*", name), name


def test_unknown_markers_are_rejected():
    # --strict-markers is what turns a marker typo into a failure instead of a
    # silently empty selection.
    assert "--strict-markers" in PYTEST_INI.read_text(encoding="utf-8")


# --- documented selectors --------------------------------------------------


DOCUMENTED_SELECTORS = [
    "(unit or contract) and not docker and not browser and not slow",
    "admin and authority and not slow",
    "admin and setup and not slow",
    "admin and maintenance and not slow",
    "admin and workflow and not slow",
    "mqtt and not docker and not slow",
    "power_control and not slow",
    "system_build and not docker and not slow",
    "backup_restore",
    "config",
    "docker",
    "authority",
]


@pytest.mark.parametrize("expression", DOCUMENTED_SELECTORS)
def test_documented_selector_collects_tests(expression):
    assert collect(expression), expression


# --- representative membership ---------------------------------------------


def assert_modules_selected(expression, expected):
    modules = {module_of(node) for node in collect(expression)}
    missing = sorted(set(expected) - modules)
    assert not missing, f"{expression!r} no longer selects: {missing}"


def test_admin_authority_selection_covers_device_plan_modules():
    assert_modules_selected("admin and authority", {
        "test_admin_setup_plan_binding",
        "test_admin_setup_plan_draft_authority",
        "test_admin_setup_device_plan_api",
        "test_admin_setup_preview_authority",
        "test_admin_setup_draft_field_authority",
        "test_admin_config_apply",
        "test_admin_config_preview",
    })


def test_admin_setup_selection_covers_guided_setup_modules():
    assert_modules_selected("admin and setup", {
        "test_admin_setup_intent",
        "test_admin_setup_catalog",
        "test_admin_discovery_run",
        "test_admin_fresh_install_discovery_contract",
    })


def test_mqtt_selection_covers_broker_and_device_modules():
    assert_modules_selected("mqtt", {
        "test_zendure_mqtt_command_lifecycle",
        "test_mqtt_credential_resolver",
        "test_mqtt_effective_broker_profiles",
        "test_ems_zendure_mqtt_control",
        "test_admin_mqtt_control_use_case",
    })


def test_power_control_selection_covers_allocation_and_write_gates():
    assert_modules_selected("power_control", {
        "test_energy_allocation",
        "test_pv_first_charge_balance",
        "test_write_gates",
        "test_control_write_gates",
        "test_simulated_power_control_regression",
        "test_controller_write_dispatch",
    })


def test_system_build_selection_covers_build_and_alignment_modules():
    assert_modules_selected("system_build", {
        "test_admin_system_build",
        "test_admin_system_alignment",
        "test_admin_operation_coordinator",
    })


def test_docker_selection_only_holds_real_docker_suites():
    modules = {module_of(node) for node in collect("docker")}
    assert modules, "the docker tier must not be empty"
    for module in modules:
        text = (TESTS / f"{module}.py").read_text(encoding="utf-8")
        assert "pytest.mark.docker" in text, module
        assert "docker" in text.lower(), module


# --- RC tier ---------------------------------------------------------------


def test_rc_tier_includes_device_plan_authority_regressions():
    # The RC tier runs the full non-Docker suite and the focused authority
    # gate; these regressions must be reachable through the focused gate too.
    assert_modules_selected("authority", {
        "test_admin_setup_plan_binding",
        "test_admin_setup_plan_draft_authority",
        "test_admin_setup_workflow_plan_authority",
        "test_admin_setup_continuation_authority",
        "test_admin_setup_transition_authority",
    })


def test_rc_tier_includes_docker_first_quickstarts():
    nodes = collect("docker")
    assert "tests/test_docker_first_e2e.py::test_ems_only_quickstart" in nodes
    assert "tests/test_docker_first_e2e.py::test_analytics_quickstart" in nodes


def test_rc_security_selection_collects_tests():
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "--collect-only", "-q",
            "-p", "no:cacheprovider", "-m", "not docker",
            "-k", "auth or secret or csrf or xss or privilege or redaction or hardening",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert any("::" in line for line in result.stdout.splitlines())


# --- group completeness ----------------------------------------------------


def test_pull_request_groups_cover_every_collected_test():
    everything = collect()
    covered = set()
    for expression in PR_GROUPS.values():
        covered |= collect(expression)
    missing = sorted(everything - covered)
    assert not missing, f"tests outside every PR group: {missing[:10]}"


def test_every_test_module_declares_exactly_one_level():
    unclassified = []
    conflicting = []
    for path in sorted(TESTS.glob("test_*.py")):
        marks = set(re.findall(r"pytest\.mark\.([a-z0-9_]+)", path.read_text(encoding="utf-8")))
        levels = marks & LEVELS
        if not levels:
            unclassified.append(path.name)
        elif len(levels) > 1:
            conflicting.append((path.name, sorted(levels)))
    assert not unclassified, f"missing a level marker: {unclassified}"
    assert not conflicting, f"more than one level marker: {conflicting}"


def test_mqtt_release_bridge_only_names_existing_modules():
    # The mqtt_release allowlist in conftest is the one remaining name-based
    # bridge; a renamed module must fail here instead of shrinking the gate.
    from conftest import MQTT_RELEASE_MODULES

    missing = sorted(
        name for name in MQTT_RELEASE_MODULES if not (TESTS / f"{name}.py").is_file()
    )
    assert not missing, missing
    assert collect("mqtt_release")


# --- Playwright groups -----------------------------------------------------


def playwright_available():
    return (ROOT / "node_modules" / ".bin" / "playwright").exists() and shutil.which("npx")


@pytest.mark.skipif(not playwright_available(), reason="Playwright is not installed (npm ci)")
@pytest.mark.parametrize("tag", ["@smoke", "@authority"])
def test_playwright_group_collects_specs(tag):
    result = subprocess.run(
        ["npx", "playwright", "test", "--list", "--project=chromium", "--grep", tag],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert re.search(r"Total: [1-9]\d* test", result.stdout), result.stdout
