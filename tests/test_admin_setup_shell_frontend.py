# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guided Setup wears the same page furniture as Maintenance.

Setup opened with a thin nav bar carrying a bare span, no statement of where
the installer stood, and four step chips reading "Locked". Maintenance answers
"where am I, what is this page for, how is it going" in a header and one
verdict line; Setup now answers them the same way, in the same order, with the
same classes.

The verdict reads the active step's own status text rather than recomputing it,
so the sentence and the chip cannot drift apart.
"""

import json
import os
import re
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
RUNNER = os.path.join(ROOT, "tests", "js", "setup_progress_runner.js")
STATIC_DIR = os.path.join(ROOT, "admin", "static")


def _read(name):
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def _setup_panel():
    html = _read("index.html")
    panel = html.split('id="view-setup"', 1)[1].split('id="view-maintenance"', 1)[0]
    assert panel, "setup panel slice is empty"
    return panel


def _shell():
    """Everything above the first step panel: header, verdict, stepper."""

    return _setup_panel().split('class="setup-step-panel"', 1)[0]


def _release_stage():
    return _setup_panel().split('aria-label="Release"', 1)[1].split(
        'aria-label="Devices"', 1
    )[0]


def _progress(step, status_text="", tone=""):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, RUNNER],
        input=json.dumps({"step": step, "statusText": status_text, "tone": tone}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# --- the shared page header -------------------------------------------------


def test_guided_setup_uses_the_shared_page_header():
    shell = _shell()
    assert 'class="maintenance-head"' in shell
    assert '<h2 class="maintenance-title" tabindex="-1">Guided setup</h2>' in shell
    assert 'class="maintenance-subtitle"' in shell
    assert 'class="workspace-back" data-back="landing"' in shell
    # The old nav bar carried a bare span, which no screen reader announced as
    # a heading and no keyboard could land on.
    assert "workspace-nav" not in shell
    assert "workspace-view-title" not in shell


def test_the_dead_nav_bar_styles_are_gone_with_it():
    css = _read("admin.css")
    assert ".workspace-nav" not in css
    assert ".workspace-view-title" not in css


def test_guided_setup_states_its_progress_where_the_hub_states_its_verdict():
    shell = _shell()
    assert 'class="maintenance-hub-verdict" id="setup-verdict"' in shell
    verdict = shell.split('id="setup-verdict"', 1)[1].split(">", 1)[0]
    assert 'role="status"' in verdict
    assert 'aria-live="polite"' in verdict
    # The verdict is the first thing under the header, above the stepper.
    assert shell.index('id="setup-verdict"') < shell.index("setup-stepper")


def test_locked_steps_no_longer_repeat_the_word_locked():
    shell = _shell()
    # A disabled, dimmed chip already says "not yet"; four "Locked" labels
    # under it were noise the hub had already dropped from its own state pills.
    assert ">Locked<" not in shell
    for step in ("release", "devices", "config", "deployment", "start"):
        assert f'id="step-status-{step}"' in shell


def test_the_stepper_still_names_and_numbers_every_step():
    shell = _shell()
    for index, label in (
        ("01", "Release"),
        ("02", "Devices"),
        ("03", "Config"),
        ("04", "Prepare deployment"),
        ("05", "Start EMS"),
    ):
        assert f'<span class="setup-step-index">{index}</span>' in shell
        assert f'<span class="setup-step-label">{label}</span>' in shell


# --- the progress sentence --------------------------------------------------


def test_the_progress_sentence_names_the_step_its_number_and_its_status():
    result = _progress("release", "Ready")
    assert result["verdict"] == "Step 01 of 05 · Choose a System Build · Ready."


def test_a_step_without_a_status_still_says_where_the_installer_stands():
    assert _progress("devices")["verdict"] == "Step 02 of 05 · Find your devices."


def test_the_last_step_is_numbered_against_the_real_step_count():
    result = _progress("start", "Running", "ok")
    assert result["verdict"].startswith("Step 05 of 05 ·")
    assert result["verdict"].endswith("Start EMS · Running.")
    assert result["tone"] == "ok"


def test_every_step_has_a_title_so_the_sentence_is_never_half_written():
    steps = _progress("release")["steps"]
    for step in steps:
        verdict = _progress(step)["verdict"]
        assert "undefined" not in verdict
        assert re.match(r"^Step \d\d of \d\d · .+\.$", verdict), verdict


def test_an_unknown_step_reports_a_starting_wizard_not_a_step_number():
    result = _progress("not_a_step")
    assert result["verdict"] == "Guided setup is starting…"
    assert result["tone"] == ""


def test_the_caller_owns_the_tone_so_it_matches_the_state_it_came_from():
    assert _progress("config", "Needs attention", "warn")["tone"] == "warn"
    assert _progress("config", "Draft ready")["tone"] == ""


# --- step 01 copy -----------------------------------------------------------


def test_step_one_no_longer_repeats_the_page_subtitle_or_its_own_heading():
    release = _release_stage()
    text = re.sub(r"\s+", " ", release)
    # The header says what Guided Setup is; the block heading says to choose a
    # build. Both sentences below said it a second time.
    assert "This assistant prepares Docker-based EMS installations." not in text
    assert "Choose the System Build you want to install." not in text
    # The reference detail they carried is kept, next to the selector.
    assert "v0.6.0 onward" in text
