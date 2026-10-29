# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contracts of the Modern dashboard, at ``/`` beside the deprecated Classic at ``/classic/``.

Modern is a build output committed under ``dashboard/static/modern/``. These
tests hold that output to the rules the hand-written Classic page follows by
construction: the server's CSP (no inline script or style), the shared energy
colours, the flow renderer's no-CSS-animation invariant, and a licence
inventory that names every package the bundle carries.
"""

import http.client
import re
from pathlib import Path

import pytest

from dashboard.server import start_dashboard_server
from dashboard.static_files import SHARED_ROOT_FILES, build_static_asset_index, lookup_static_asset, modern_asset_path, modern_redirect
from tests.modern_frontend import (
    TOKENS_CSS,
    assert_build_matches_source,
    assert_bundled_packages_inventoried,
    assert_font_and_icons_carry_their_notices,
    assert_font_and_icons_inventoried,
    assert_no_colour_literal_outside_the_token_blocks,
    assert_no_inline_script_or_style,
    assert_tab_icon_is_the_mark,
    js_export,
    root_block,
    run_npm_script,
)

pytestmark = [
    pytest.mark.contract,
]

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "dashboard" / "static"
MODERN = STATIC / "modern"
SOURCE = ROOT / "frontend" / "dashboard-modern"
CLASSIC_CSS = STATIC / "styles.css"
MODERN_CSS = MODERN / "assets" / "modern.css"
MODERN_JS = MODERN / "assets" / "modern.js"

SHARED_ENERGY_TOKENS = ("--pv", "--battery", "--output", "--grid", "--danger")


class StoreStub:
    def latest(self):
        return {"timestamp": "2026-10-19T12:00:00+00:00", "pv_total_w": 1200}

    def history(self, _range_name):
        return []


def _request(server, path):
    host, port = server.server_address
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def _served(assets, path):
    asset_path = modern_asset_path(path, SHARED_ROOT_FILES)
    return lookup_static_asset(assets, asset_path) if asset_path else None


def test_modern_answers_at_the_root_and_classic_under_classic():
    assets = build_static_asset_index(str(STATIC))
    for path in ("/", "/index.html"):
        full_path, content_type = _served(assets, path)
        assert full_path.endswith("modern/index.html")
        assert content_type.startswith("text/html")
    assert _served(assets, "/classic/")[0].endswith("static/index.html")
    assert _served(assets, "/classic/app.js")[0].endswith("static/app.js")
    assert _served(assets, "/uPlot.iife.min.js")[0].endswith("static/uPlot.iife.min.js")
    assert _served(assets, "/app.js") is None
    assert _served(assets, "/classic/modern/index.html") is None
    assert _served(assets, "/classic/./modern/index.html") is None
    assert _served(assets, "/classic//modern/index.html") is None
    assert _served(assets, "/classic/./app.js")[0].endswith("static/app.js")
    assert _served(assets, "/../../server.py") is None
    assert _served(assets, "/classic/../../server.py") is None
    assert _served(assets, "/nowhere/") is None


def test_the_old_addresses_move_to_the_new_ones():
    assert modern_redirect("/modern") == "/"
    assert modern_redirect("/modern/") == "/"
    assert modern_redirect("/modern/assets/modern.js") == "/assets/modern.js"
    assert modern_redirect("/classic") == "/classic/"
    assert modern_redirect("/modernity") is None
    assert modern_redirect("/") is None
    assert modern_redirect("/modern/", "demo=1") == "/?demo=1"
    assert modern_redirect("/modern/a\r\nSet-Cookie: x", "b=\n") == "/a%0D%0ASet-Cookie: x?b=%0A"
    assert modern_redirect("/modern/\\evil.example") == "/evil.example"
    assert modern_redirect("/modern//\\evil.example") == "/evil.example"
    assert modern_redirect("/modern/a\\b", "c=\\") == "/a%5Cb?c=%5C"


def test_the_bundle_is_served_with_types_nosniff_accepts():
    assets = build_static_asset_index(str(STATIC))
    assert _served(assets, "/assets/modern.js")[1].startswith("application/javascript")
    assert _served(assets, "/assets/modern.css")[1].startswith("text/css")
    assert _served(assets, "/assets/inter-latin-wght-normal.woff2")[1] == "font/woff2"
    assert _served(assets, "/licenses.json")[1].startswith("application/json")
    assert _served(assets, "/fallback.js")[1].startswith("application/javascript")


def test_the_server_serves_modern_under_the_same_security_headers():
    try:
        server = start_dashboard_server(StoreStub(), host="127.0.0.1", port=0)
    except PermissionError as exc:
        pytest.skip(f"local socket creation is not permitted: {exc}")
    try:
        status, headers, _ = _request(server, "/modern/?demo=1")
        assert status == 302
        assert headers["Location"] == "/?demo=1"
        assert headers["Cache-Control"] == "no-store"
        status, headers, body = _request(server, "/")
        assert status == 200
        assert b'<div id="app">' in body
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "script-src 'self'" in headers["Content-Security-Policy"]
        assert "style-src 'self'" in headers["Content-Security-Policy"]
        assert headers.get("ETag")
        status, headers, _ = _request(server, "/assets/inter-latin-wght-normal.woff2")
        assert status == 200
        assert headers["Content-Type"] == "font/woff2"
    finally:
        server.shutdown()
        server.server_close()


def test_the_page_carries_no_inline_script_or_style():
    assert_no_inline_script_or_style(
        (MODERN / "index.html").read_text(encoding="utf-8"), MODERN_JS.read_text(encoding="utf-8")
    )


def test_the_tab_icon_is_the_mark_in_the_token_colours():
    assert_tab_icon_is_the_mark((MODERN / "index.html").read_text(encoding="utf-8"))


def test_the_shared_energy_colours_have_the_contract_values():
    classic = root_block(CLASSIC_CSS.read_text(encoding="utf-8"))
    modern = root_block(TOKENS_CSS.read_text(encoding="utf-8"))
    for token in SHARED_ENERGY_TOKENS:
        assert modern[token].strip().lower() == classic[token].strip().lower(), token


def test_the_soc_is_drawn_in_the_colour_classic_draws_it_in():
    classic_token = re.search(r'soc:\s*\{[^}]*colorVar:\s*"([^"]+)"', (STATIC / "app.js").read_text(encoding="utf-8")).group(1)
    classic = root_block(CLASSIC_CSS.read_text(encoding="utf-8"))
    modern = root_block(TOKENS_CSS.read_text(encoding="utf-8"))
    assert re.search(r'soc: \{ token: "--soc"', (SOURCE / "src" / "lib" / "analytics.js").read_text(encoding="utf-8"))
    assert modern["--soc"].strip().lower() == classic[classic_token].strip().lower()


def test_no_colour_literal_outside_the_token_blocks():
    assert_no_colour_literal_outside_the_token_blocks(MODERN_CSS.read_text(encoding="utf-8"))


def test_the_flow_layer_has_no_css_animation_and_no_filter():
    """The flow moves with element.animate() and literal keyframes, and its glow is wider strokes, never a filter (reports/dashboard-perf)."""

    css = MODERN_CSS.read_text(encoding="utf-8")
    assert "@keyframes" not in css
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if any(name in selector for name in (".flow-stage", ".wires", ".wire-", ".dash-layer", ".run", ".dashes", ".hub", ".node")):
            assert "animation" not in body, selector
            assert "filter" not in body, selector


def test_every_bundled_package_is_in_the_licence_inventory():
    assert_bundled_packages_inventoried(MODERN)


def test_the_font_and_icons_shipped_beside_the_bundle_are_inventoried():
    assert_font_and_icons_inventoried(MODERN)


def test_the_font_and_icons_ship_with_their_licence_texts():
    assert_font_and_icons_carry_their_notices(MODERN)


def test_analytics_ranges_are_the_backend_ranges():
    from ems.history.provider import HISTORY_RANGE_SECONDS

    source = (SOURCE / "src" / "lib" / "analytics.js").read_text(encoding="utf-8")
    ranges = re.search(r"ANALYTICS_RANGES = Object\.freeze\(\[([^\]]*)\]\)", source).group(1)
    assert [item.strip().strip('"') for item in ranges.split(",")] == list(HISTORY_RANGE_SECONDS)


def _strings():
    strings = js_export(SOURCE / "src" / "i18n" / "strings.js", "STRINGS")
    if strings is None:
        pytest.skip("Node is required to read the Modern string table")
    return strings


def test_every_device_setting_and_value_has_its_words_in_every_language():
    from ems.device_settings import SETTINGS

    strings = _strings()
    for setting in SETTINGS:
        assert strings[f"setting.{setting.key}"]["en"] == setting.label
        assert strings[f"setting.{setting.key}.help"]["en"] == setting.help
        for value in setting.values:
            assert strings[f"settingValue.{value.word}"]["en"] == value.label


def test_the_rule_reasons_modern_translates_are_the_dashboards_own():
    from dashboard.telemetry import RULE_REASONS

    assert js_export(SOURCE / "src" / "lib" / "status.js", "RULE_REASONS") == RULE_REASONS
    strings = _strings()
    for key, reason in RULE_REASONS.items():
        assert strings[f"rule.reason.{key}"]["en"] == reason


def _limit_reasons_the_ems_gives():
    """Every string a limit's reason can be: the fourth argument of add_limit and ControlLimitExplanation."""

    import ast

    reasons = set()
    for source in (ROOT / "ems" / "target_control.py", ROOT / "ems" / "controller.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or len(node.args) < 4:
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if name not in ("add_limit", "ControlLimitExplanation"):
                continue
            reasons.update(item.value for item in ast.walk(node.args[3]) if isinstance(item, ast.Constant) and isinstance(item.value, str))
    return reasons


def test_the_limit_reasons_modern_translates_are_the_ems_own():
    registry = js_export(SOURCE / "src" / "lib" / "control.js", "LIMIT_REASONS")
    if registry is None:
        pytest.skip("Node is required to read the Modern limit reasons")
    assert set(registry) == _limit_reasons_the_ems_gives()
    strings = _strings()
    for reason, key in registry.items():
        assert strings[f"limit.reason.{key}"]["en"] == reason


def _explanation_notes_the_ems_writes():
    """The notes of a control explanation: notes=[...] and explanation_notes.append(...); f-strings apart."""

    import ast

    fixed, formatted = set(), []
    for source in (ROOT / "ems" / "target_control.py", ROOT / "ems" / "controller.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            values = []
            if isinstance(node, ast.keyword) and node.arg == "notes" and isinstance(node.value, ast.List):
                values = node.value.elts
            elif isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "append" and getattr(node.func.value, "id", "") == "explanation_notes":
                values = node.args
            for value in values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    fixed.add(value.value)
                elif isinstance(value, ast.JoinedStr):
                    formatted.append("".join(part.value for part in value.values if isinstance(part, ast.Constant)))
    return fixed, formatted


def test_the_explanation_notes_modern_translates_are_the_ems_own():
    registry = js_export(SOURCE / "src" / "lib" / "control.js", "EXPLAIN_NOTES")
    if registry is None:
        pytest.skip("Node is required to read the Modern explanation notes")
    fixed, formatted = _explanation_notes_the_ems_writes()
    assert set(registry) == fixed
    assert formatted == [" W could not be distributed"]
    strings = _strings()
    for note, key in registry.items():
        assert strings[f"note.{key}"]["en"] == note


def test_the_control_explanation_carries_the_controllers_device_order():
    """A limit's per-device values follow the controller's order; the devices mapping is sent sorted by name."""

    from types import SimpleNamespace

    from dashboard.telemetry import _control_explain_payload

    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="Terasse"), SimpleNamespace(name="Garage")],
        last_control_explanation={"devices": {"Garage": {}, "Terasse": {}}, "limits": []},
    )
    assert _control_explain_payload(controller)["device_order"] == ["Terasse", "Garage"]
    assert _control_explain_payload(SimpleNamespace(devices=[], last_control_explanation=None)) is None


def test_the_unit_tests_of_the_modern_frontend_pass():
    assert len(list((SOURCE / "tests").glob("*.test.js"))) >= 10
    for script in ("test:frontend-shared", "test:dashboard-modern"):
        if run_npm_script(script) is None:
            pytest.skip("npm is required for the Modern frontend unit tests")


def test_the_committed_build_is_the_build_of_the_committed_source(tmp_path):
    if not assert_build_matches_source(
        SOURCE / "vite.config.js", MODERN, tmp_path / "modern", "npm run build:dashboard-modern"
    ):
        pytest.skip("npm ci has not been run; CI runs the same check in its own job")
