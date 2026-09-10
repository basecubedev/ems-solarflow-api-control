# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the appliance tells an operator is wrong, and where to go about it.

The overview used to hand the browser a flat list of ``{code, message}`` and let
it sort out the rest: app.js decided how bad each entry was with a regular
expression over the code string, printed the code itself as the headline, and
offered no destination. That put the severity judgement in two places -- the
health level here and the regex there -- and showed ``admin_unhealthy`` to a
person who wanted to know what to do about it.

The judgement belongs here, once. Every finding carries its own severity, the
level is the worst of them rather than a second opinion, and each one names the
page of the manager that can act on it.
"""

import pytest

from appliance.status import HEALTH_ATTENTION, HEALTH_DEGRADED, HEALTH_HEALTHY
from tests.helpers.appliance import build_test_services

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

SEVERITIES = ("error", "warning", "info")


def health_for(tmp_path, sections):
    return build_test_services(tmp_path).status._health(sections)


def docker_down():
    return {"docker": {"status": "ok", "daemon": {"state": "stopped"}}}


def updates_pending():
    return {
        "updates": {
            "status": "ok",
            "security_count": 3,
            "reboot_required": True,
            "package_manager": {"healthy": True},
        }
    }


def every_finding(tmp_path):
    """One of every finding the health check can produce."""

    sections = {
        "docker": {"status": "ok", "daemon": {"state": "stopped"}},
        "admin": {"status": "ok", "installed": False},
        "updates": {
            "status": "ok",
            "security_count": 2,
            "reboot_required": True,
            "package_manager": {"healthy": False},
        },
        "system": {
            "status": "ok",
            "storage": {
                "root": {"available": True, "used_percent": 95},
                "ems_data": {"available": True, "used_percent": 97},
            },
        },
        "network": {"status": "unavailable"},
    }
    findings = health_for(tmp_path, sections)["warnings"]
    assert len(findings) >= 7, "the fixture no longer covers most of the findings"
    return findings


def test_every_finding_says_how_bad_it_is_and_what_to_do_next(tmp_path):
    for finding in every_finding(tmp_path):
        assert finding["severity"] in SEVERITIES, finding
        assert finding["title"].strip(), finding
        assert finding["message"].strip(), finding
        assert finding["next_step"].strip(), finding


def test_the_health_level_is_the_worst_finding_rather_than_a_second_judgement(tmp_path):
    """One judgement, read twice.

    The level and the per-finding severity used to be written side by side at
    every branch, so a new finding could raise one and forget the other.
    """

    degraded = health_for(tmp_path, docker_down())
    assert degraded["level"] == HEALTH_DEGRADED
    assert {item["severity"] for item in degraded["warnings"]} == {"error"}

    attention = health_for(tmp_path, updates_pending())
    assert attention["level"] == HEALTH_ATTENTION
    assert {item["severity"] for item in attention["warnings"]} == {"warning"}

    assert health_for(tmp_path, {})["level"] == HEALTH_HEALTHY


def test_every_finding_points_at_a_page_the_manager_actually_has(tmp_path):
    """A destination the navigation does not have is not a next step.

    The section is a routing hint, so its vocabulary is the manager's own view
    ids; app.js is where those are declared and therefore what this reads.
    """

    import re
    from pathlib import Path

    app = (Path(__file__).resolve().parents[1] / "appliance" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    declared = app.split("var VIEWS = [", 1)[1].split("];", 1)[0]
    views = set(re.findall(r'id:\s*"([a-z-]+)"', declared))
    assert views, "the VIEWS table changed shape; this test can no longer read it"

    for finding in every_finding(tmp_path):
        assert finding["section"] in views, f"{finding['code']} points at {finding['section']}"


def test_no_finding_shows_the_operator_its_own_code(tmp_path):
    """``admin_unhealthy`` is a log line, not a sentence for a person."""

    for finding in every_finding(tmp_path):
        assert finding["code"] not in finding["title"], finding
        assert "_" not in finding["title"], finding


def test_a_healthy_appliance_has_nothing_to_report(tmp_path):
    health = health_for(tmp_path, {"docker": {"status": "ok", "daemon": {"state": "running"}}})
    assert health["warnings"] == []
    assert health["level"] == HEALTH_HEALTHY
