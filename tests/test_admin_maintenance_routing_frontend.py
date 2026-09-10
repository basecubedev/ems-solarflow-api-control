# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance splits into a status page and a settings page, and old links live.

``#maintenance-manual`` was the published address of the manual maintenance page.
It appears in bookmarks and in click paths the documentation prints, so it stays
a permanent alias for the status page rather than becoming a dead link. The
settings page additionally answers a per-tab deep link so a "change these
settings" pointer can land on the right tab.
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
RUNNER = os.path.join(ROOT, "tests", "js", "maintenance_route_runner.js")
STATIC_DIR = os.path.join(ROOT, "admin", "static")


def _read(name):
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def _route(hash_value):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, RUNNER],
        input=json.dumps({"hash": hash_value}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# --- the two doors --------------------------------------------------------


@pytest.mark.parametrize(
    "hash_value, path",
    [
        ("maintenance", "hub"),
        ("maintenance-status", "status"),
        ("maintenance-settings", "settings"),
        ("maintenance-upgrade", "upgrade"),
        ("maintenance-backup", "backup"),
    ],
)
def test_every_maintenance_page_has_its_own_address(hash_value, path):
    resolved = _route(hash_value)
    assert resolved["path"] == path
    assert resolved["known"] is True


def test_the_old_manual_address_still_lands_on_the_status_page():
    """A published link must not break because the page was split in two."""

    resolved = _route("maintenance-manual")
    assert resolved["path"] == "status"
    assert resolved["known"] is True


def test_an_unknown_maintenance_address_falls_back_to_the_hub():
    assert _route("maintenance-nonsense")["known"] is False


# --- settings tabs --------------------------------------------------------


@pytest.mark.parametrize("tab", ["devices", "features", "safety", "expert"])
def test_a_settings_tab_can_be_linked_to_directly(tab):
    resolved = _route("maintenance-settings-" + tab)
    assert resolved["path"] == "settings"
    assert resolved["tab"] == tab


def test_a_settings_address_without_a_tab_picks_none():
    assert _route("maintenance-settings")["tab"] is None


def test_an_unknown_tab_does_not_become_a_tab():
    resolved = _route("maintenance-settings-nonsense")
    assert resolved["path"] == "settings"
    assert resolved["tab"] is None


def test_a_tab_suffix_on_another_page_is_not_a_settings_tab():
    assert _route("maintenance-status-safety")["tab"] is None


# --- panels ---------------------------------------------------------------


def test_each_path_maps_to_exactly_one_panel():
    js = _read("admin.js")
    registry = js.split("const MAINTENANCE_PANEL_IDS = {", 1)[1].split("};", 1)[0]
    for key, panel in (
        ("hub", "maintenance-hub"),
        ("status", "maintenance-status-panel"),
        ("settings", "maintenance-settings-panel"),
        ("upgrade", "maintenance-upgrade-panel"),
        ("backup", "maintenance-backup-panel"),
    ):
        assert key + ': "' + panel + '"' in registry
    assert "maintenance-manual-panel" not in registry


def test_the_new_panels_are_hidden_against_the_hub_flex_layout():
    """.maintenance-view sets display:flex, which beats the UA [hidden] rule."""

    css = _read("admin.css")
    for selector in (
        "#maintenance-status-panel[hidden]",
        "#maintenance-settings-panel[hidden]",
    ):
        assert selector in css
    block = css.split(".maintenance-hub[hidden]", 1)[1].split("}", 1)[0]
    assert "display: none" in block


def test_leaving_the_settings_page_returns_its_borrowed_forms():
    """The credential forms are singletons; a hidden panel must not keep them.

    parkInlineConfigs() refuses to reclaim a node that is mounted somewhere
    else, so a settings panel that goes hidden while still holding
    #mqtt-credential-form strands it for the rest of the session.
    """

    js = _read("admin.js")
    body = js.split("function setMaintenancePath", 1)[1].split("\nfunction ", 1)[0]
    assert "parkMaintenanceSourceConfigs()" in body
    parked = body.index("parkMaintenanceSourceConfigs()")
    panels = body.index("MAINTENANCE_PANEL_IDS")
    assert parked < panels, "park before the panels are hidden, not after"


# --- cold deep links and focus --------------------------------------------


def test_a_bookmarked_maintenance_address_reveals_the_workspace():
    """A cold load fires no hashchange, so the router has to reveal itself."""

    js = _read("admin.js")
    body = js.split("function applyHashRoute", 1)[1].split("\nfunction ", 1)[0]
    assert "if (!workspaceRevealed) {" in body
    assert "revealWorkspace()" in body


def test_only_maintenance_opens_from_an_address():
    """Guided Setup resumes from its durable transition, never from a bookmark.

    Revealing the wizard for a "#setup" address would resurrect an unconfirmed
    selection that no server-side transition backs.
    """

    js = _read("admin.js")
    body = js.split("function applyHashRoute", 1)[1].split("\nfunction ", 1)[0]
    guard = body.split("if (!workspaceRevealed) {", 1)[1].split("}", 1)[0]
    assert 'if (view !== "maintenance") return;' in guard


def test_the_authenticated_app_asks_the_router_before_showing_the_gate():
    js = _read("admin.js")
    body = js.split("function showAuthenticatedApp", 1)[1].split("\nfunction ", 1)[0]
    routed = body.index("applyHashRoute()")
    gate = body.index("startEls.gate.hidden = false")
    assert routed < gate, "route first; the gate is the fallback"


def test_every_maintenance_page_heading_can_take_focus():
    """Panel switches move focus, so the headings must be focus targets."""

    html = _read("index.html")
    # Five maintenance pages plus the landing and Guided Setup, which share the
    # same header and the same focus helper.
    assert html.count('<h2 class="maintenance-title" tabindex="-1">') == 7
    assert '<h2 class="maintenance-title">' not in html


def test_switching_pages_moves_focus_to_the_new_heading():
    js = _read("admin.js")
    body = js.split("function setMaintenancePath", 1)[1].split("\nfunction ", 1)[0]
    assert "focusMaintenanceHeading(next)" in body
    helper = js.split("function focusMaintenanceHeading", 1)[1].split("\nfunction ", 1)[0]
    assert "MAINTENANCE_PANEL_IDS[path]" in helper
    # One focus helper for every Admin page that can be navigated to.
    shared = js.split("function focusPageHeading", 1)[1].split("\nfunction ", 1)[0]
    assert ".maintenance-title" in shared
    assert "heading.focus()" in shared
