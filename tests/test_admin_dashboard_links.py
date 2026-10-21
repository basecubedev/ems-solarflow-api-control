# SPDX-License-Identifier: AGPL-3.0-or-later
"""Classic's dashboard links and the early view switch: every link goes to the
dashboard where this browser reaches it, and a stored ?ui= choice leaves the
address. Split from test_admin_frontend.py, which is at the size limit."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.contract,
    pytest.mark.simulation,
]

STATIC = Path(__file__).resolve().parents[1] / "admin" / "static"


def _read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def _run_node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to run the dashboard link functions")
    result = subprocess.run([node, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _js_function_body(js, signature):
    assert js.count(signature) == 1, signature
    return js.split(signature, 1)[1].split("\n}\n", 1)[0]


def _maintenance_section(html):
    return html.split('id="view-maintenance"', 1)[1].split("</main>", 1)[0]


def test_maintenance_dashboard_link_is_a_real_anchor():
    html = _read("index.html")
    js = _read("admin.js")
    maintenance = _maintenance_section(html)
    assert 'id="maintenance-dashboard-link"' in maintenance
    assert 'id="maintenance-containers-dashboard-link"' in maintenance
    assert 'target="_blank"' in maintenance
    assert 'rel="noopener"' in maintenance
    # href is set through the DOM property, never via innerHTML/markup.
    fn = js.split("function renderMaintenanceDashboard", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "link.href = href" in fn
    assert "maintenanceEls.containersDashboardLink" in fn
    assert "innerHTML" not in fn
    render = _js_function_body(js, "function renderMaintenance(")
    assert "dashboardHrefForBrowser(data.links && data.links.dashboard_url)" in render


def test_no_dashboard_link_points_at_the_admin_console_itself():
    """An "Open dashboard" link with href="/" on the Admin port opened the
    Admin itself -- now its Modern view. Every dashboard link takes its
    address from the server's dashboard URL instead."""

    html = _read("index.html")
    anchors = re.findall(r"<a\b[^>]*>\s*Open (?:EMS )?[Dd]ashboard\s*</a>", html)
    assert len(anchors) >= 3
    assert not [anchor for anchor in anchors if 'href="/"' in anchor]


def test_the_dashboard_links_are_wired_and_hide_when_the_address_is_unknown():
    """The container restart's link is an a.secondary-button, whose display
    beats the browser's [hidden] rule unless the stylesheet restores it; the
    setup success link hides as the Maintenance ones do."""

    js = _read("admin.js")
    css = _read("admin.css")
    assert 'containersDashboardLink: document.getElementById("maintenance-containers-dashboard-link")' in js
    assert "a.secondary-button[hidden] { display: none; }" in css
    start = _js_function_body(js, "function startDashboardHref(")
    assert "dashboardHrefForBrowser(" in start
    success = _js_function_body(js, "function renderStartSuccess(")
    assert "link.hidden = !href;" in success


def test_a_stored_ui_choice_leaves_the_address_and_keeps_the_rest():
    early = _read("admin-theme.js")
    start = early.index("  function forgetAsked() {")
    fn = early[start:early.index("\n  }\n", start) + 4]
    script = fn + """
const cases = ["?ui=classic", "?ui=classic&screen=x", "?screen=x&ui=modern", "?a=1&ui=classic&b=2", "?screen=x"];
console.log(JSON.stringify(cases.map((search) => {
  let written = null;
  globalThis.window = {
    location: { search, pathname: "/", hash: "#maintenance" },
    history: { state: null, replaceState: (state, title, url) => { written = url; } },
  };
  forgetAsked();
  return written;
})));
"""
    assert _run_node(script) == [
        "/#maintenance",
        "/?screen=x#maintenance",
        "/?screen=x#maintenance",
        "/?a=1&b=2#maintenance",
        "/?screen=x#maintenance",
    ]


def test_dashboard_links_point_where_this_browser_reaches_the_dashboard():
    js = _read("admin.js")
    fn = "function dashboardHrefForBrowser(" + _js_function_body(js, "function dashboardHrefForBrowser(") + "\n}\n"
    script = fn + """
const cases = [
  ["192.168.1.5", "http://localhost:8080"],
  ["ems.local", "https://localhost:8443"],
  ["127.0.0.1", "http://localhost:8080/somewhere?x=1#y"],
  ["192.168.1.5", "javascript:alert(1)"],
  ["192.168.1.5", "not a url"],
  ["192.168.1.5", ""],
  ["192.168.1.5", null],
  ["192.168.1.5", "http://u:p@localhost:8080"],
  ["fe80::1", "http://localhost:8080"],
  ["", "http://localhost:8080"],
];
console.log(JSON.stringify(cases.map(([hostname, url]) => {
  globalThis.window = { location: { hostname } };
  return dashboardHrefForBrowser(url);
})));
"""
    assert _run_node(script) == [
        "http://192.168.1.5:8080/",
        "https://ems.local:8443/",
        "http://127.0.0.1:8080/",
        None,
        None,
        None,
        None,
        "http://192.168.1.5:8080/",
        "http://[fe80::1]:8080/",
        None,
    ]
