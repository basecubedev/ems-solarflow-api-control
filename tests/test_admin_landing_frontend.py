# SPDX-License-Identifier: AGPL-3.0-or-later
"""The landing page answers the same three questions the maintenance hub does.

Maintenance states one verdict about this host, lists what needs attention in
severity order, and marks exactly one recommended door. The landing page did
none of that: it printed "Recommended: <path>" over an unranked bullet list
built with innerHTML, and highlighted a door before the install state had been
read. The two pages now share the verdict line, the findings list and the path
cards, so an operator meets one console rather than two.

install_state.py stays the only classifier: the view function maps its verdict
onto one sentence and one badge and never re-derives the recommendation.
"""

import json
import os
import shutil
import subprocess

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.setup,
    pytest.mark.contract,
    pytest.mark.simulation,
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "tests", "js", "start_gate_runner.js")
STATIC_DIR = os.path.join(ROOT, "admin", "static")


def _read(name):
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def _gate():
    html = _read("index.html")
    gate = html.split('id="view-start"', 1)[1].split('id="view-setup"', 1)[0]
    assert gate, "start gate slice is empty"
    return gate


def _render(state):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, RUNNER],
        input=json.dumps({"state": state}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _state(**overrides):
    payload = {
        "state": "standard_install",
        "recommended_path": "manage_existing",
        "reasons": [],
        "warnings": [],
    }
    payload.update(overrides)
    return payload


# --- markup: the landing wears the maintenance page furniture ---------------


def test_landing_carries_the_shared_page_header():
    gate = _gate()
    assert 'class="maintenance-head"' in gate
    assert '<h2 class="maintenance-title" tabindex="-1">' in gate
    assert 'class="maintenance-subtitle"' in gate
    # The landing is the root of the console, so it has no "back" control.
    assert "data-back=" not in gate


def test_landing_has_one_verdict_line_like_the_hub():
    gate = _gate()
    assert 'class="maintenance-hub-verdict" id="start-verdict"' in gate
    assert 'role="status"' in gate
    assert 'aria-live="polite"' in gate
    # The old recommendation box and its bullet list are gone.
    assert "start-gate-recommend" not in gate
    assert "start-recommend-notes" not in gate
    assert "start-choices-legend" not in gate


def test_landing_findings_reuse_the_maintenance_findings_list():
    gate = _gate()
    assert 'class="maintenance-findings" id="start-findings"' in gate
    assert 'id="start-findings-headline"' in gate
    assert 'id="start-findings-list"' in gate
    # Nothing to report is the normal case, so the section starts hidden.
    findings = gate.split('id="start-findings"', 1)[1].split(">", 1)[0]
    assert "hidden" in findings


def test_landing_paths_are_maintenance_style_cards():
    gate = _gate()
    assert gate.count("data-start-path=") == 2
    assert gate.count("maintenance-path-card maintenance-path-nav") == 2
    assert gate.count('class="control-stage-title"') == 2
    assert gate.count('class="maintenance-path-arrow"') == 2
    assert "Guided setup" in gate and "Maintenance" in gate
    assert gate.index('data-start-path="setup_new"') < gate.index(
        'data-start-path="manage_existing"'
    )
    # Docker bootstrap / developer setup stay documentation-only paths.
    assert "Docker bootstrap" not in gate
    assert "Developer setup" not in gate
    assert "See the setup paths in the documentation." in gate


def test_landing_recommendation_badges_start_hidden():
    gate = _gate()
    assert gate.count("maintenance-recommended-badge") == 2
    assert gate.count('class="maintenance-recommended-badge" id="start-setup-badge" hidden') == 1
    assert (
        gate.count('class="maintenance-recommended-badge" id="start-maintenance-badge" hidden')
        == 1
    )
    # No door is marked before install-state has been read.
    assert "is-primary" not in gate
    assert "is-recommended" not in gate


def test_landing_opens_each_path_from_its_card_without_a_next_button():
    gate = _gate()
    # The card is the action; there is no separate submit between the cards and
    # the docs hint, and both paths are real keyboard-accessible buttons.
    assert 'id="start-continue"' not in gate
    assert ">Next<" not in gate
    assert gate.count('<button type="button" class="control-pipeline-stage ') == 2


def test_landing_keeps_the_browser_level_entry_point():
    gate = _gate()
    assert 'data-testid="start-fresh-install"' in gate
    assert 'id="start-path-error"' in gate


# --- the verdict: what the landing may say about this host ------------------


def test_a_finished_installation_reads_as_installed_and_recommends_maintenance():
    view = _render(_state())["view"]
    assert view["verdict"] == "An EMS installation was found on this host."
    assert view["tone"] == "ok"
    assert view["recommended"] == "manage_existing"
    assert view["installState"] == {"text": "Installed", "tone": "ok"}
    assert view["findings"] == []


def test_an_empty_host_reads_as_empty_and_recommends_guided_setup():
    view = _render(
        _state(state="none", recommended_path="setup_new")
    )["view"]
    assert view["verdict"] == "No EMS installation was found on this host."
    assert view["tone"] == "ok"
    assert view["recommended"] == "setup_new"
    assert view["installState"]["text"] == "Nothing installed"


def test_a_partial_install_is_never_reported_as_healthy():
    view = _render(
        _state(
            state="partial_install",
            reasons=["The standard config/config.json needs repair."],
        )
    )["view"]
    assert view["tone"] == "warn"
    assert view["installState"]["tone"] == "warn"
    assert "incomplete" in view["verdict"].lower()


def test_an_unreadable_install_state_recommends_nothing():
    view = _render(None)["view"]
    assert view["recommended"] is None
    assert view["tone"] == "warn"
    assert "could not be read" in view["verdict"]


def test_an_unknown_state_name_is_reported_as_unknown_not_as_installed():
    view = _render(_state(state="something_new"))["view"]
    assert view["tone"] == "warn"
    assert view["installState"]["text"] == "Unknown"
    assert "could not be classified" in view["verdict"]


def test_an_unknown_recommended_path_marks_no_door():
    view = _render(_state(recommended_path="reinstall_everything"))["view"]
    assert view["recommended"] is None


# --- findings: warnings outrank the classification reasons ------------------


def test_warnings_rank_above_reasons_and_carry_their_severity():
    result = _render(
        _state(
            state="legacy_root_config",
            reasons=["A legacy root config.json was found."],
            warnings=["Both configs exist and differ."],
        )
    )
    assert [item["severity"] for item in result["findings"]["items"]] == [
        "warning",
        "info",
    ]
    assert result["findings"]["status"] == "warning"
    assert result["findings"]["headline"] == "2 things need your attention."
    assert result["findings"]["hidden"] is False


def test_a_clean_host_hides_the_findings_list_entirely():
    result = _render(_state())
    assert result["findings"]["hidden"] is True
    assert result["findings"]["items"] == []


def test_a_landing_finding_renders_only_the_lines_it_has():
    result = _render(_state(warnings=["Both configs exist and differ."]))
    lines = result["findings"]["items"][0]["lines"]
    assert [line["className"] for line in lines] == ["maintenance-finding-message"]
    assert lines[0]["text"] == "Both configs exist and differ."


def test_server_text_reaches_the_findings_list_as_text_not_markup():
    result = _render(_state(warnings=["<img src=x onerror=alert(1)>"]))
    assert "<img" not in result["findings"]["html"]
    assert "&lt;img" in result["findings"]["html"]
