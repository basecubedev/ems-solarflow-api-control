# SPDX-License-Identifier: AGPL-3.0-or-later
"""The overview answers three questions before it shows a single fact.

It used to open with six tiles of raw readings and end with a bullet list whose
first word was a machine code. Whether an entry was serious was decided in the
browser by a regular expression over that code -- a second severity judgement
next to the one the health check had already made.

Now the page says what this appliance is (one verdict), what needs doing (the
findings, worst first, each with a way to get there), and only then what the
box currently reads. The severity comes from the backend; this layer maps it
onto a sentence and never re-derives it.
"""

import json
import os
import shutil
import subprocess

import pytest

pytestmark = [
    pytest.mark.appliance,
    pytest.mark.contract,
    pytest.mark.simulation,
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "tests", "js", "appliance_overview_runner.js")


def _render(status, view="overview"):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, RUNNER],
        input=json.dumps({"status": status, "view": view}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _finding(code="admin_unhealthy", severity="error", section="admin", **overrides):
    payload = {
        "code": code,
        "severity": severity,
        "section": section,
        "title": "EMS Admin is not answering",
        "message": "the EMS Admin container is not healthy",
        "next_step": "Open Admin and restart it.",
    }
    payload.update(overrides)
    return payload


def _status(level="healthy", findings=(), **overrides):
    payload = {"health": {"level": level, "warnings": list(findings)}}
    payload.update(overrides)
    return payload


# --- the verdict -----------------------------------------------------------


def test_a_healthy_appliance_is_told_so_in_one_sentence():
    view = _render(_status())["verdict"]
    assert view["tone"] == "ok"
    assert view["text"].endswith(".")
    assert "healthy" in view["text"].lower()


def test_a_degraded_appliance_leads_with_the_bad_news():
    view = _render(_status("degraded", [_finding()]))["verdict"]
    assert view["tone"] == "bad"


def test_an_appliance_that_needs_attention_is_not_reported_as_broken():
    view = _render(_status("attention", [_finding(severity="warning")]))["verdict"]
    assert view["tone"] == "warn"


def test_a_status_that_could_not_be_read_is_never_reported_as_healthy():
    """The old page printed the tiles anyway, every value an em dash, and said
    underneath them that the appliance looked healthy.

    A status call that failed is itself the finding, so the page reports an
    appliance nobody could ask rather than an empty one.
    """

    view = _render({"error": "agent_unavailable"})
    assert view["verdict"]["tone"] == "bad"
    assert "could not be read" in view["verdict"]["text"].lower()
    assert "healthy" not in view["verdict"]["text"].lower()
    assert view["findings"]["hidden"] is False
    assert view["findings"]["findings"][0]["severity"] == "error"
    assert "agent_unavailable" in view["findings"]["findings"][0]["message"]


# --- the findings ----------------------------------------------------------


def test_findings_are_ranked_worst_first():
    view = _render(
        _status(
            "degraded",
            [
                _finding(code="security_updates_pending", severity="warning", section="updates"),
                _finding(code="admin_unhealthy", severity="error", section="admin"),
                _finding(code="reboot_required", severity="info", section="overview"),
            ],
        )
    )
    assert [item["severity"] for item in view["findings"]["findings"]] == [
        "error",
        "warning",
        "info",
    ]


def test_the_severity_is_the_one_the_backend_sent():
    """app.js used to re-derive it with /unhealthy|not_running|.../ over the code.

    A backend that downgrades a finding has to be able to say so, and a code it
    has never seen must not be silently ranked by its spelling.
    """

    view = _render(_status("attention", [_finding(code="admin_unhealthy", severity="info")]))
    assert view["findings"]["findings"][0]["severity"] == "info"
    assert view["findings"]["status"] == "info"


def test_the_headline_counts_what_is_waiting():
    one = _render(_status("attention", [_finding(severity="warning")]))
    two = _render(_status("attention", [_finding(severity="warning"), _finding()]))
    assert "1 thing" in one["findings"]["headline"]
    assert "2 things" in two["findings"]["headline"]


def test_a_clean_appliance_hides_the_findings_panel():
    view = _render(_status())["findings"]
    assert view["hidden"] is True
    assert view["findings"] == []


def test_no_finding_is_shown_as_its_code():
    view = _render(_status("degraded", [_finding()]))["findings"]["findings"][0]
    assert view["title"] == "EMS Admin is not answering"
    assert "admin_unhealthy" not in json.dumps(
        {"title": view["title"], "message": view["message"], "next_step": view["next_step"]}
    )


# --- getting there ---------------------------------------------------------


def test_a_finding_offers_the_page_that_can_act_on_it():
    action = _render(_status("degraded", [_finding(section="admin")]))["actions"][0]
    assert action["view"] == "admin"
    assert action["label"] == "Open Admin"


def test_a_finding_does_not_offer_the_page_it_is_already_on():
    """An "Open Overview" button on the overview is a button that does nothing."""

    action = _render(
        _status("attention", [_finding(code="reboot_required", section="overview")]),
        view="overview",
    )["actions"][0]
    assert action is None


def test_an_unknown_destination_is_dropped_rather_than_guessed():
    action = _render(_status("degraded", [_finding(section="does-not-exist")]))["actions"][0]
    assert action is None


def test_the_navigation_marks_the_sections_that_need_attention():
    view = _render(
        _status(
            "degraded",
            [
                _finding(section="admin"),
                _finding(section="updates", severity="warning"),
                _finding(section="updates", severity="warning"),
            ],
        )
    )
    assert view["attention"] == {"admin": "error", "updates": "warning"}
