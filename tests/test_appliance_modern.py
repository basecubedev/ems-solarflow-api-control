# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contracts of the Modern Appliance Manager, the second entry page at ``/modern/``.

Modern is a build output committed under ``appliance/static/modern/``, made from
``frontend/appliance-modern/`` and the shared design system in
``frontend/shared/``. These tests hold the output to the Manager's CSP, the
shared colours and the licence inventory, the web service to serving it and
nothing beside it, and the console's vocabulary to the backend's: the words it
has for a code are only right while the backend still sends that code.
"""

import http.client
import re
import threading
from pathlib import Path

import pytest

from appliance import (
    admin_lifecycle,
    backup_access,
    docker_backend,
    export_state,
    manager_install,
    manager_verify,
    operations,
    ssh_policy,
    validation,
    web_audit,
)
from appliance.web import ApplianceWebApp, ApplianceWebServer
from tests.helpers.appliance import build_test_services
from tests.modern_frontend import (
    assert_build_matches_source,
    assert_bundled_packages_inventoried,
    assert_font_and_icons_carry_their_notices,
    assert_font_and_icons_inventoried,
    assert_no_colour_literal_outside_the_token_blocks,
    assert_no_inline_script_or_style,
    assert_tab_icon_is_the_mark,
    run_npm_script,
)

pytestmark = [pytest.mark.contract, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "appliance" / "static"
MODERN = STATIC / "modern"
SOURCE = ROOT / "frontend" / "appliance-modern"
MODERN_JS = MODERN / "assets" / "appliance.js"
MODERN_CSS = MODERN / "assets" / "appliance.css"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)

# Built in agent.py as f"system.{action}", so they are not TYPE_ constants.
DYNAMIC_TYPES = ("system.reboot", "system.shutdown")
# Raised by the console itself, about its own session and its own read.
BROWSER_FINDINGS = {"status_unavailable", "security_audit_degraded", "password_unconfirmed"}


def js_list(relative, name):
    """The string items of ``export const NAME = Object.freeze([...])`` in a source module."""

    source = (SOURCE / "src" / relative).read_text(encoding="utf-8")
    match = re.search(rf"export const {name} = Object\.freeze\(\[(.*?)\]\)", source, re.S)
    assert match, f"{name} not found in {relative}"
    return re.findall(r'"([^"]+)"', match.group(1))


def js_keys(relative, name):
    """The keys of ``export const NAME = Object.freeze({...})`` in a source module."""

    source = (SOURCE / "src" / relative).read_text(encoding="utf-8")
    match = re.search(rf"export const {name} = Object\.freeze\(\{{(.*?)\n\}}\)", source, re.S)
    assert match, f"{name} not found in {relative}"
    return re.findall(r"^  (?:\"([^\"]+)\"|([a-z_]+)):", match.group(1), re.M)


def keys(relative, name):
    return {quoted or bare for quoted, bare in js_keys(relative, name)}


def constants(module, prefix):
    return {value for key, value in vars(module).items() if key.startswith(prefix) and isinstance(value, str)}


@pytest.fixture()
def server(tmp_path):
    services = build_test_services(tmp_path)
    app = ApplianceWebApp(paths=services.paths, config=services.config, agent=None)
    srv = ApplianceWebServer(app, ("127.0.0.1", 0))
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


def fetch(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("GET", path)
    response = connection.getresponse()
    body = response.read()
    headers = dict(response.getheaders())
    connection.close()
    return response.status, headers, body


# --- serving -----------------------------------------------------------------


def test_the_manager_serves_modern_beside_classic_under_its_own_headers(server):
    for path in ("/modern/", "/modern", "/modern/index.html"):
        status, headers, body = fetch(server, path)
        assert status == 200, path
        assert headers["Content-Type"].startswith("text/html")
        assert headers["Content-Security-Policy"] == CSP
        assert headers["Cache-Control"] == "no-cache"
        assert b'<div id="app">' in body
    expected = {
        "/modern/assets/appliance.js": "application/javascript",
        "/modern/assets/appliance.css": "text/css",
        "/modern/assets/inter-latin-wght-normal.woff2": "font/woff2",
        "/modern/licenses.json": "application/json",
        "/modern/Inter.LICENSE": "text/plain",
    }
    for path, content_type in expected.items():
        status, headers, _ = fetch(server, path)
        assert status == 200, path
        assert headers["Content-Type"].startswith(content_type), path
    assert b"Appliance Manager" in fetch(server, "/")[2]


def test_nothing_outside_the_build_or_of_another_kind_is_served(server, tmp_path, monkeypatch):
    """Planted files, so each guard of _serve_modern is what answers 404."""

    from appliance import web

    static = tmp_path / "planted-static"
    assets = static / "modern" / "assets"
    assets.mkdir(parents=True)
    (assets / "ok.js").write_text("ok", encoding="utf-8")
    (static / "modern" / ".hidden.js").write_text("hidden", encoding="utf-8")
    (static / "modern" / "notes.py").write_text("source", encoding="utf-8")
    secret = tmp_path / "secret.js"
    secret.write_text("secret", encoding="utf-8")
    (assets / "leak.js").symlink_to(secret)
    monkeypatch.setattr(web, "STATIC_DIR", str(static))

    assert fetch(server, "/modern/assets/ok.js")[0] == 200
    for path in ("/modern/.hidden.js", "/modern/notes.py", "/modern/assets/leak.js", "/modern/assets", "/modern/assets/missing.js"):
        assert fetch(server, path)[0] == 404, path


def test_the_modern_page_needs_no_session_but_every_api_still_does(server):
    assert fetch(server, "/modern/")[0] == 200
    assert fetch(server, "/api/status")[0] == 401


# --- the build ---------------------------------------------------------------


def test_the_page_carries_no_inline_script_or_style():
    assert_no_inline_script_or_style((MODERN / "index.html").read_text(encoding="utf-8"), MODERN_JS.read_text(encoding="utf-8"))


def test_the_tab_icon_is_the_mark_in_the_token_colours():
    assert_tab_icon_is_the_mark((MODERN / "index.html").read_text(encoding="utf-8"))


def test_no_colour_literal_outside_the_token_blocks():
    assert_no_colour_literal_outside_the_token_blocks(MODERN_CSS.read_text(encoding="utf-8"))


def test_the_bundle_fits_under_the_repository_size_limit():
    assert MODERN_JS.stat().st_size < 512 * 1024


def test_every_bundled_package_is_in_the_licence_inventory():
    assert_bundled_packages_inventoried(MODERN)


def test_the_inventory_claims_no_package_the_bundle_does_not_carry():
    import json

    bundled = {entry["name"] for entry in json.loads((MODERN / "licenses.json").read_text(encoding="utf-8"))}
    inventory = (ROOT / "THIRD_PARTY_LICENSES.md").read_text(encoding="utf-8")
    vendored = inventory.split("## Vendored Components", 1)[1].split("\n## ", 1)[0]
    claimed = {
        re.match(r"\| `([^`]+)`", row).group(1)
        for row in vendored.splitlines()
        if row.startswith("| `") and "`appliance/static/modern/assets/appliance.js`" in row
    }
    assert claimed and claimed <= bundled, (claimed, bundled)


def test_the_font_and_icons_shipped_beside_the_bundle_are_inventoried():
    assert_font_and_icons_inventoried(MODERN)


def test_the_font_and_icons_ship_with_their_licence_texts():
    assert_font_and_icons_carry_their_notices(MODERN)


def test_the_package_ships_the_modern_build():
    script = (ROOT / "packaging" / "appliance" / "build-deb.sh").read_text(encoding="utf-8")
    assert "find modern -type f" in script
    assert 'appliance/static/modern/index.html" ] ||' in script


def test_the_classic_page_offers_the_switch_and_decides_before_it_paints():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'href="/modern/"' in page
    early = (STATIC / "theme.js").read_text(encoding="utf-8")
    assert 'var UI_KEY = "ems-appliance-ui";' in early
    assert 'var DEFAULT_UI = "modern";' in early
    assert 'window.location.replace("/modern/" + window.location.hash)' in early
    prefs = (SOURCE / "src" / "state" / "prefs.svelte.js").read_text(encoding="utf-8")
    assert 'UI_CHOICE_KEY = "ems-appliance-ui"' in prefs


# --- the vocabulary is the backend's ------------------------------------------


def test_every_operation_type_the_agent_runs_has_a_title():
    declared = set(DYNAMIC_TYPES)
    for path in sorted((ROOT / "appliance").glob("*.py")):
        declared |= set(re.findall(r'^TYPE_[A-Z_]+ = "([a-z][a-z._]+)"', path.read_text(encoding="utf-8"), re.M))
    assert set(js_list("lib/operations.js", "OPERATION_TYPES")) == declared


def test_every_operation_state_has_words():
    assert keys("lib/operations.js", "OPERATION_STATES") == constants(operations, "STATE_")


def test_the_verification_failures_are_the_lifecycle_s_own():
    assert set(js_list("lib/operations.js", "VERIFICATION_FAILURES")) == set(admin_lifecycle.VERIFICATION_MESSAGES)


def test_every_finding_the_backend_raises_is_known_and_no_other():
    source = (ROOT / "appliance" / "status.py").read_text(encoding="utf-8")
    raised = set(re.findall(r'finding\(\s*"([a-z_]+)"', source))
    raised |= set(re.findall(r'^\s*"([a-z_]+_low)",$', source, re.M))
    sections = set(js_list("lib/findings.js", "STATUS_SECTIONS"))
    assert 'f"{name}_unavailable"' in source
    from appliance import status

    assert sections == set(status.SECTION_LABELS)
    raised |= {f"{name}_unavailable" for name in sections}
    known = keys("lib/findings.js", "FINDINGS") - {"...Object.fromEntries"}
    known |= {f"{name}_unavailable" for name in sections}
    assert known - BROWSER_FINDINGS == raised


def test_every_error_the_console_translates_is_one_the_appliance_raises():
    source = "".join(path.read_text(encoding="utf-8") for path in sorted((ROOT / "appliance").glob("*.py")))
    for code in js_list("lib/errors.js", "KNOWN_ERRORS"):
        assert f'"{code}"' in source, f"the appliance no longer raises {code!r}"


def test_the_log_sources_with_names_are_the_declared_ones():
    assert tuple(js_list("lib/logs.js", "KNOWN_LOG_SOURCES")) == tuple(validation.LOG_SOURCES)
    assert set(js_list("lib/logs.js", "BASIC_LOG_SOURCES")) <= set(validation.LOG_SOURCES)


def test_the_manager_vocabulary_is_the_manager_s():
    assert set(js_list("lib/manager.js", "MANAGER_OUTCOMES")) == constants(manager_install, "OUTCOME_")
    assert keys("lib/manager.js", "MANAGER_VERDICTS") == constants(manager_verify, "VERDICT_") - {manager_verify.VERDICT_PENDING, manager_verify.VERDICT_NAME}


def test_the_access_vocabulary_is_the_access_code_s():
    assert set(js_list("lib/access.js", "EXPORT_ACCESS_STATUSES")) == constants(backup_access, "STATUS_")
    assert set(js_list("lib/access.js", "EXPORT_STATES")) == constants(export_state, "STATE_")
    assert set(js_list("lib/access.js", "RESCUE_SSH_STATES")) == constants(ssh_policy, "REFUSAL_")


def test_the_daemon_states_are_docker_backend_s():
    assert set(js_list("lib/words.js", "DAEMON_STATES")) == constants(docker_backend, "DAEMON_")


def test_the_audit_finding_states_only_what_the_backend_says():
    """The translated finding names the cause and the reassurance of the
    backend's own sentence; it must stop doing so when that sentence does."""

    assert "appliance agent was unreachable" in web_audit.DEGRADED_MESSAGE
    assert "Authentication itself still works" in web_audit.DEGRADED_MESSAGE


def test_the_plan_reads_in_classic_s_order():
    classic = (STATIC / "app.js").read_text(encoding="utf-8")
    block = classic.split("var PLAN_FIELD_ORDER = [", 1)[1].split("];", 1)[0]
    assert js_list("lib/plan.js", "PLAN_FIELD_ORDER") == re.findall(r'"([a-z_]+)"', block)
    block = classic.split("var PLAN_EXPERT_FIELDS = [", 1)[1].split("];", 1)[0]
    assert js_list("lib/plan.js", "PLAN_EXPERT_FIELDS") == re.findall(r'"([a-z_]+)"', block)


def test_every_page_is_a_section_the_backend_names_or_the_settings():
    source = (ROOT / "appliance" / "status.py").read_text(encoding="utf-8")
    sections = set(re.findall(r'^VIEW_[A-Z_]+ = "([a-z_]+)"', source, re.M))
    pages = set(re.findall(r'\{ id: "([a-z]+)", icon:', (SOURCE / "src" / "app" / "pages.js").read_text(encoding="utf-8")))
    assert sections <= pages
    assert pages - sections <= {"network", "settings"}


# --- the source ----------------------------------------------------------------


def test_the_unit_tests_of_the_appliance_frontend_pass():
    assert len(list((SOURCE / "tests").glob("*.test.js"))) >= 6
    for script in ("test:frontend-shared", "test:appliance-modern"):
        if run_npm_script(script) is None:
            pytest.skip("npm is required for the Modern frontend unit tests")


def test_the_committed_build_is_the_build_of_the_committed_source(tmp_path):
    if not assert_build_matches_source(SOURCE / "vite.config.js", MODERN, tmp_path / "modern", "npm run build:appliance-modern"):
        pytest.skip("npm ci has not been run; CI runs the same check in its own job")
