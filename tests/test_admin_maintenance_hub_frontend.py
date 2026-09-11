# SPDX-License-Identifier: AGPL-3.0-or-later
"""The maintenance hub says what it can prove, and no more.

Four doors that all look alike answer nothing, so each card carries the one fact
that decides whether to open it. The danger in that is a pill: a card that reads
"EMS running" while it is not is worse than a card that says nothing, so every
state here is derived from the read-only overview and an unreadable overview
renders as unknown, never as the optimistic branch.

The hub deliberately does not say whether EMS is *controlling*. That verdict
needs the saved config and the transport gates, and it is stated once, on the
status page — two derivations of one truth are how the two start to disagree.
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
RUNNER = os.path.join(ROOT, "tests", "js", "maintenance_hub_runner.js")
STATIC_DIR = os.path.join(ROOT, "admin", "static")


def _read(name):
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def _view(overview):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, RUNNER],
        input=json.dumps({"overview": overview}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _overview(state="standard_install", running=True, tag="v0.8.0", warnings=()):
    return {
        "install_state": {"state": state, "label": "Standard installation"},
        "containers": {"ems": {"running": running, "tag": tag}},
        "components": {"ems": {"tag": tag}},
        "warnings": list(warnings),
    }


def test_a_healthy_installation_names_the_running_version():
    view = _view(_overview())
    assert view["tone"] == "ok"
    assert view["state"] == "EMS running"
    assert "v0.8.0" in view["verdict"]


def test_a_stopped_ems_is_not_reported_as_fine():
    view = _view(_overview(running=False))
    assert view["tone"] == "warn"
    assert view["state"] == "EMS stopped"


def test_an_incomplete_installation_says_so_before_anything_else():
    view = _view(_overview(state="compose_only", running=True))
    assert view["tone"] == "warn"
    assert view["state"] == "Needs setup"


def test_a_warning_keeps_the_card_from_reading_all_clear():
    view = _view(_overview(warnings=["EMS is running an unknown image"]))
    assert view["tone"] == "warn"
    assert view["state"] == "Needs a look"


@pytest.mark.parametrize("overview", [None, "broken", {}])
def test_an_unreadable_overview_is_unknown_not_optimistic(overview):
    view = _view(overview)
    assert view["tone"] == "warn"
    assert view["state"] in ("Unknown", "Needs setup")
    assert "running" not in view["verdict"].lower()


def test_the_hub_never_claims_control():
    """Control is a config verdict; the hub only sees the container."""

    js = _read("admin.js")
    body = js.split("function maintenanceHubView", 1)[1].split("\n}", 1)[0]
    for forbidden in ("controlling", "allow_hardware_writes", "may_control", "control"):
        assert forbidden not in body.lower()


def test_each_hub_card_carries_its_own_state_element():
    html = _read("index.html")
    hub = html.split('id="maintenance-hub"', 1)[1].split(
        'id="maintenance-status-panel"', 1
    )[0]
    assert hub, "hub slice is empty"
    for marker in (
        'id="maintenance-hub-verdict"',
        'id="maintenance-hub-status-state"',
        'id="maintenance-hub-settings-state"',
        'id="maintenance-hub-backup-state"',
    ):
        assert marker in hub


def test_the_unsaved_pill_stays_quiet_until_there_is_a_draft():
    """An editor that was never opened has nothing to report."""

    html = _read("index.html")
    pill = html.split('id="maintenance-hub-settings-state"', 1)[1].split(">", 1)[0]
    assert "hidden" in pill
    js = _read("admin.js")
    body = js.split("function renderMaintenanceHubDraftState", 1)[1].split("\n}", 1)[0]
    assert "mconfigDraftChangeCount" in body
    assert "pill.hidden = count === 0" in body


def test_opening_the_hub_reads_state_without_mutating_anything():
    js = _read("admin.js")
    body = js.split("async function loadMaintenanceHubState", 1)[1].split("\n}", 1)[0]
    assert '"/api/admin/maintenance/overview"' in body
    assert '"/api/admin/maintenance/backups"' in body
    assert "method:" not in body, "the hub must only read"


# --- which door to open first ---------------------------------------------


def test_an_admin_deployed_installation_is_healthy():
    """A successful Admin deployment writes the marker and is standard.

    Reading only "standard_install" reported every Admin-deployed system as
    "Needs setup" — the one shape the console produces itself.
    """

    view = _view(_overview(state="admin_prepared_install"))
    assert view["tone"] == "ok"
    assert view["state"] == "EMS running"


@pytest.mark.parametrize(
    "overview",
    [
        _overview(state="compose_only"),
        _overview(running=False),
        _overview(warnings=["EMS is running an unknown image"]),
        None,
    ],
)
def test_a_system_that_needs_a_look_points_at_the_status_page(overview):
    assert _view(overview)["recommended"] == "status"


def test_a_healthy_system_points_at_the_guided_upgrade():
    assert _view(_overview())["recommended"] == "upgrade"


def test_the_recommendation_is_not_hard_coded_in_the_markup():
    """The badge used to sit on the upgrade card whatever the system said."""

    html = _read("index.html")
    hub = html.split('id="maintenance-hub"', 1)[1].split(
        'id="maintenance-status-panel"', 1
    )[0]
    assert hub, "hub slice is empty"
    for marker in (
        'id="maintenance-hub-upgrade-badge"',
        'id="maintenance-hub-status-badge"',
    ):
        assert marker in hub
    badges = [
        hub.split(marker, 1)[1].split(">", 1)[0]
        for marker in (
            'id="maintenance-hub-upgrade-badge"',
            'id="maintenance-hub-status-badge"',
        )
    ]
    assert all("hidden" in badge for badge in badges), "no badge shows before a verdict"
    assert "is-primary" not in hub, "the highlight follows the verdict too"


def test_the_hub_moves_the_badge_and_the_highlight_together():
    js = _read("admin.js")
    body = js.split("function renderMaintenanceHubState", 1)[1].split("\n}", 1)[0]
    assert "view.recommended" in body
    assert "is-primary" in body
