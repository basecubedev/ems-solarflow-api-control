# SPDX-License-Identifier: AGPL-3.0-or-later
"""The status page answers its own headline instead of leaving seven cards open.

"What is installed, what is running, and what is wrong" was answered by a red
banner, two cards tinted WARNING and a third card explaining that diagnostics
had never been run — three descriptions of one cause, none of them a next step,
and nothing sorted by how bad it was.

The synthesis here is a projection, never a second authority: it reads the
overview payload the Maintenance page already assembles and states what that
payload proves. It invents no diagnosis vocabulary either — the severities are
the EMS diagnostics ones (``info``/``warning``/``error``).
"""

import pytest

from admin.maintenance import (
    EMS_RUNNING_IDENTITY_UNKNOWN_WARNING,
    PARTIAL_INSTALL_WARNING,
    run_maintenance_overview,
)
from admin.maintenance_health import SEVERITIES, build_maintenance_health

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.contract,
    pytest.mark.simulation,
]


def _overview(**overrides):
    payload = {
        "install_state": {
            "state": "standard_install",
            "label": "Standard installation",
            "message": "A standard EMS installation was found.",
            "reasons": [],
        },
        "paths": {
            "config": {"path": "/srv/config/config.json", "exists": True,
                       "modified_at": "2026-09-01T10:00:00Z"},
            "data": {"path": "/srv/data", "exists": True},
            "compose": {"path": "/srv/docker-compose.yml", "exists": True},
        },
        "docker": {"available": True, "state": "ready", "error": None},
        "containers": {
            "ems": {
                "found": True,
                "running": True,
                "name": "ems",
                "tag": "v0.8.0",
                "status": "running",
                "started_at": "2026-09-01T12:00:00Z",
            }
        },
        "warnings": [],
    }
    payload.update(overrides)
    return payload


def _codes(health):
    return [finding["code"] for finding in health["findings"]]


def _by_code(health, code):
    return next(f for f in health["findings"] if f["code"] == code)


# --- the shape ------------------------------------------------------------


def test_a_healthy_installation_reports_nothing_to_do():
    health = build_maintenance_health(_overview())
    assert health["status"] == "ok"
    assert health["findings"] == []


def test_every_finding_carries_a_next_step():
    """A finding that names a problem and stops is the banner all over again."""

    health = build_maintenance_health(
        _overview(docker={"available": False, "state": "unavailable", "error": "no socket"})
    )
    assert health["findings"]
    for finding in health["findings"]:
        assert finding["severity"] in SEVERITIES
        assert finding["code"] and finding["code"] == finding["code"].lower()
        assert finding["title"]
        assert finding["message"]
        assert finding["next_step"]


def test_the_worst_finding_decides_the_page_status():
    health = build_maintenance_health(
        _overview(warnings=[EMS_RUNNING_IDENTITY_UNKNOWN_WARNING])
    )
    assert health["status"] == "warning"
    health = build_maintenance_health(
        _overview(docker={"available": False, "state": "unavailable", "error": None})
    )
    assert health["status"] == "error"


def test_findings_are_ordered_worst_first():
    health = build_maintenance_health(
        _overview(
            docker={"available": False, "state": "unavailable", "error": None},
            warnings=[EMS_RUNNING_IDENTITY_UNKNOWN_WARNING],
        )
    )
    severities = [SEVERITIES.index(f["severity"]) for f in health["findings"]]
    assert severities == sorted(severities)


# --- what it can prove ----------------------------------------------------


def test_an_unreachable_docker_is_the_first_thing_reported():
    health = build_maintenance_health(
        _overview(docker={"available": False, "state": "unavailable", "error": "x"})
    )
    assert _codes(health)[0] == "docker_unavailable"
    assert _by_code(health, "docker_unavailable")["severity"] == "error"


def test_an_incomplete_installation_is_reported_once_not_three_times():
    """The banner, the state label and the paths card all said the same thing."""

    health = build_maintenance_health(
        _overview(
            install_state={
                "state": "standard_config_only",
                "label": "Config without compose",
                "message": "A standard config/config.json exists but "
                "docker-compose.yml is missing.",
                "reasons": ["No docker-compose.yml"],
            },
            paths={
                "config": {"path": "/srv/config/config.json", "exists": True,
                           "modified_at": None},
                "data": {"path": "/srv/data", "exists": True},
                "compose": {"path": "/srv/docker-compose.yml", "exists": False},
            },
            warnings=[PARTIAL_INSTALL_WARNING],
            containers={"ems": {"found": False, "running": False}},
        )
    )
    assert _codes(health).count("installation_incomplete") == 1
    assert "installation_warning" not in _codes(health), (
        "the partial-install warning is already the structured finding"
    )


def test_a_stopped_ems_is_reported_with_the_page_that_can_start_it():
    health = build_maintenance_health(
        _overview(
            containers={
                "ems": {"found": True, "running": False, "name": "ems",
                        "tag": "v0.8.0", "status": "exited"}
            }
        )
    )
    finding = _by_code(health, "ems_stopped")
    assert finding["severity"] == "error"
    assert "ems" in finding["message"].lower()


def test_a_missing_ems_container_is_not_the_same_as_a_stopped_one():
    health = build_maintenance_health(
        _overview(containers={"ems": {"found": False, "running": False}})
    )
    assert "ems_missing" in _codes(health)
    assert "ems_stopped" not in _codes(health)


def test_saved_settings_newer_than_the_running_ems_ask_for_a_restart():
    """Config → runtime convergence is not automatic; nothing said so here."""

    health = build_maintenance_health(
        _overview(
            paths={
                "config": {"path": "/srv/config/config.json", "exists": True,
                           "modified_at": "2026-09-01T13:00:00Z"},
                "data": {"path": "/srv/data", "exists": True},
                "compose": {"path": "/srv/docker-compose.yml", "exists": True},
            }
        )
    )
    finding = _by_code(health, "settings_await_restart")
    assert finding["severity"] == "info"
    assert "restart" in finding["next_step"].lower()


def test_settings_older_than_the_running_ems_ask_for_nothing():
    assert build_maintenance_health(_overview())["findings"] == []


def test_an_unreadable_timestamp_never_invents_a_restart_finding():
    for modified in (None, "", "not-a-date"):
        health = build_maintenance_health(
            _overview(
                paths={
                    "config": {"path": "/c", "exists": True, "modified_at": modified},
                    "data": {"path": "/d", "exists": True},
                    "compose": {"path": "/y", "exists": True},
                }
            )
        )
        assert "settings_await_restart" not in _codes(health)


def test_a_warning_with_no_structured_finding_keeps_the_servers_own_words():
    health = build_maintenance_health(
        _overview(warnings=[EMS_RUNNING_IDENTITY_UNKNOWN_WARNING])
    )
    finding = _by_code(health, "ems_image_unverifiable")
    assert finding["severity"] == "warning"

    health = build_maintenance_health(_overview(warnings=["Something else is odd."]))
    finding = _by_code(health, "installation_warning")
    assert finding["message"] == "Something else is odd."


def test_an_unreadable_overview_fails_closed():
    """A synthesis that cannot read its input must not report a healthy system."""

    for payload in (None, "broken", {}, []):
        health = build_maintenance_health(payload)
        assert health["status"] == "error"
        assert _codes(health) == ["overview_unreadable"]


# --- it is wired to the real payload --------------------------------------


def test_the_overview_carries_its_own_health_block(tmp_path):
    overview = run_maintenance_overview(base_dir=str(tmp_path))
    assert "health" in overview
    health = overview["health"]
    assert health["status"] in ("ok", "info", "warning", "error")
    assert isinstance(health["findings"], list)
    # An empty directory is not an installation, and the block says so.
    assert "installation_incomplete" in [f["code"] for f in health["findings"]]


def test_the_health_block_is_rebuildable_from_the_payload(tmp_path):
    """It is a projection: dropping it and recomputing must give the same thing."""

    overview = run_maintenance_overview(base_dir=str(tmp_path))
    recomputed = build_maintenance_health(
        {key: value for key, value in overview.items() if key != "health"}
    )
    assert recomputed == overview["health"]
