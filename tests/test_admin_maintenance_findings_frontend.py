# SPDX-License-Identifier: AGPL-3.0-or-later
"""The status page leads with what is wrong, ranked, with a next step.

Before this the page opened with a red banner that named a problem and said in
the same sentence that nothing could be done about it, followed by seven cards
whose coloured chips the owner had to compare by eye. The findings list is one
ordered answer, rendered from the server-side synthesis; the cards below stay
the detail view.
"""

import json
import os
import shutil
import subprocess

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.contract,
    pytest.mark.simulation,
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "tests", "js", "maintenance_findings_runner.js")
STATIC_DIR = os.path.join(ROOT, "admin", "static")


def _read(name):
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def _status_panel():
    html = _read("index.html")
    panel = html.split('id="maintenance-status-panel"', 1)[1].split(
        'id="maintenance-settings-panel"', 1
    )[0]
    assert panel, "status panel slice is empty"
    return panel


def _render(health):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, RUNNER],
        input=json.dumps({"health": health}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _finding(code="ems_stopped", severity="error"):
    return {
        "code": code,
        "severity": severity,
        "title": "EMS is not running",
        "message": "The EMS container exists but is exited.",
        "next_step": "Open EMS services below and start it.",
    }


def test_the_findings_list_leads_the_status_page():
    panel = _status_panel()
    assert 'id="maintenance-findings"' in panel
    assert panel.index('id="maintenance-findings"') < panel.index(
        'id="maintenance-control-state"'
    ), "what is wrong comes before what is allowed"


def test_the_old_free_text_warning_paragraph_is_gone():
    """Its content is a finding now, with a severity and a next step."""

    assert 'id="maintenance-warnings"' not in _status_panel()


def test_a_healthy_system_says_so_instead_of_showing_an_empty_box():
    view = _render({"status": "ok", "findings": []})
    assert view["hidden"] is False
    assert view["items"] == []
    assert "nothing" in view["headline"].lower()


def test_each_finding_renders_its_title_message_and_next_step():
    view = _render({"status": "error", "findings": [_finding()]})
    item = view["items"][0]
    assert item["title"] == "EMS is not running"
    assert item["message"] == "The EMS container exists but is exited."
    assert item["nextStep"] == "Open EMS services below and start it."
    assert item["severity"] == "error"


def test_the_headline_counts_what_needs_attention():
    view = _render(
        {
            "status": "error",
            "findings": [_finding(), _finding("ems_image_unverifiable", "warning")],
        }
    )
    assert "2" in view["headline"]


def test_the_server_order_is_kept_rather_than_re_sorted():
    """Ranking is the server's answer; a second sort here would be a second rule."""

    findings = [
        _finding("docker_unavailable", "error"),
        _finding("installation_warning", "warning"),
        _finding("settings_await_restart", "info"),
    ]
    view = _render({"status": "error", "findings": findings})
    assert [item["code"] for item in view["items"]] == [
        "docker_unavailable",
        "installation_warning",
        "settings_await_restart",
    ]


def test_a_missing_health_block_is_not_reported_as_healthy():
    for health in (None, {}, {"status": "ok"}):
        view = _render(health)
        assert view["hidden"] is False
        assert "could not" in view["headline"].lower() or view["items"]


def test_finding_text_is_escaped_rather_than_written_as_markup():
    view = _render(
        {
            "status": "warning",
            "findings": [
                {
                    "code": "installation_warning",
                    "severity": "warning",
                    "title": "<img src=x onerror=alert(1)>",
                    "message": "<b>bold</b>",
                    "next_step": "ok",
                }
            ],
        }
    )
    assert view["items"][0]["title"] == "<img src=x onerror=alert(1)>"
    assert "<img" not in view["html"]
    assert "&lt;img" in view["html"]


def test_a_quote_in_server_data_cannot_forge_an_attribute_in_the_serialized_html():
    """The serialized html is the only view these tests have of the DOM.

    `finding.code` is server data and lands in an attribute. A serializer that
    escapes `<` but leaves `"` alone lets that data close the attribute and
    invent markup the real DOM never held, so every escaping assertion made
    against `html` would be reading a forgery rather than the rendered tree.
    """

    view = _render(
        {
            "status": "warning",
            "findings": [_finding(code='" onerror="alert(1)', severity="warning")],
        }
    )
    assert view["items"][0]["code"] == '" onerror="alert(1)'
    assert 'onerror="alert(1)"' not in view["html"]
    assert "&quot; onerror=&quot;alert(1)" in view["html"]
