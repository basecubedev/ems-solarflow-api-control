# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contracts of the Modern dashboard, the second entry page at ``/modern/``.

Modern is a build output committed under ``dashboard/static/modern/``. These
tests hold that output to the rules the hand-written Classic page follows by
construction: the server's CSP (no inline script or style), the shared energy
colours, the flow renderer's no-CSS-animation invariant, and a licence
inventory that names every package the bundle carries.
"""

import http.client
import json
import re
import shutil
import subprocess
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

import pytest

from dashboard.server import start_dashboard_server
from dashboard.static_files import build_static_asset_index, lookup_static_asset

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
INVENTORY = ROOT / "THIRD_PARTY_LICENSES.md"

SHARED_ENERGY_TOKENS = ("--pv", "--battery", "--output", "--grid", "--danger")
PERMISSIVE = {"MIT", "ISC", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "OFL-1.1", "0BSD"}


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


def _root_block(css, selector=":root"):
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, f"{selector} block not found"
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", match.group(1)))


def test_the_modern_entry_page_resolves_for_both_spellings():
    assets = build_static_asset_index(str(STATIC))
    for path in ("/modern/", "/modern"):
        full_path, content_type = lookup_static_asset(assets, path)
        assert full_path.endswith("modern/index.html")
        assert content_type.startswith("text/html")
    assert lookup_static_asset(assets, "/")[0].endswith("static/index.html")
    assert lookup_static_asset(assets, "/modern/../../server.py") is None
    assert lookup_static_asset(assets, "/nowhere/") is None


def test_the_bundle_is_served_with_types_nosniff_accepts():
    assets = build_static_asset_index(str(STATIC))
    assert lookup_static_asset(assets, "/modern/assets/modern.js")[1].startswith("application/javascript")
    assert lookup_static_asset(assets, "/modern/assets/modern.css")[1].startswith("text/css")
    assert lookup_static_asset(assets, "/modern/assets/inter-latin-wght-normal.woff2")[1] == "font/woff2"
    assert lookup_static_asset(assets, "/modern/licenses.json")[1].startswith("application/json")


def test_the_server_serves_modern_under_the_same_security_headers():
    try:
        server = start_dashboard_server(StoreStub(), host="127.0.0.1", port=0)
    except PermissionError as exc:
        pytest.skip(f"local socket creation is not permitted: {exc}")
    try:
        status, headers, body = _request(server, "/modern/")
        assert status == 200
        assert b'<div id="app">' in body
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "script-src 'self'" in headers["Content-Security-Policy"]
        assert "style-src 'self'" in headers["Content-Security-Policy"]
        assert headers.get("ETag")
        status, headers, _ = _request(server, "/modern/assets/inter-latin-wght-normal.woff2")
        assert status == 200
        assert headers["Content-Type"] == "font/woff2"
    finally:
        server.shutdown()
        server.server_close()


class _PageElements(HTMLParser):
    """What the CSP decides on: every script, every style element, every style attribute."""

    def __init__(self):
        super().__init__()
        self.scripts = []
        self.styles = 0
        self.style_attributes = []

    def handle_starttag(self, tag, attrs):
        names = dict(attrs)
        if tag == "script":
            self.scripts.append(names)
        elif tag == "style":
            self.styles += 1
        if "style" in names:
            self.style_attributes.append(tag)


def test_the_page_carries_no_inline_script_or_style():
    """The CSP allows neither; an inline chunk would break the page with no visible error."""

    page = _PageElements()
    page.feed((MODERN / "index.html").read_text(encoding="utf-8"))
    page.close()
    assert page.scripts and all(script.get("src") for script in page.scripts), page.scripts
    assert page.styles == 0
    assert page.style_attributes == []
    bundle = MODERN_JS.read_text(encoding="utf-8")
    assert not re.search(r'style=\\?"', bundle), "a template would set a style attribute the CSP blocks"


def test_the_tab_icon_is_the_mark_in_the_token_colours():
    """The favicon is built from src/brand/mark.js at build time; its colours must be the scheme's own."""

    page = (MODERN / "index.html").read_text(encoding="utf-8")
    links = re.findall(r'<link rel="icon" type="image/svg\+xml" href="data:image/svg\+xml,([^"]+)">', page)
    assert len(links) == 1, links
    svg = urllib.parse.unquote(links[0])
    root = _root_block((SOURCE / "src" / "styles" / "tokens.css").read_text(encoding="utf-8"))
    for token in ("--pv", "--battery", "--output", "--grid", "--m-surface-2", "--m-line-strong", "--m-text"):
        assert f'"{root[token].strip()}"' in svg, token
    assert "var(" not in svg


def test_the_shared_energy_colours_have_the_contract_values():
    classic = _root_block(CLASSIC_CSS.read_text(encoding="utf-8"))
    modern = _root_block((SOURCE / "src" / "styles" / "tokens.css").read_text(encoding="utf-8"))
    for token in SHARED_ENERGY_TOKENS:
        assert modern[token].strip().lower() == classic[token].strip().lower(), token


def _themes():
    """The scheme ids the Appearance popover offers, read from ``THEMES``; the first is ``:root``."""

    source = (SOURCE / "src" / "state" / "prefs.svelte.js").read_text(encoding="utf-8")
    match = re.search(r"export const THEMES = Object\.freeze\(\[([^\]]*)\]\)", source)
    assert match, "THEMES not found"
    return re.findall(r'"([a-z]+)"', match.group(1))


def _scheme(css, scheme):
    tokens = _root_block(css)
    if scheme != _themes()[0]:
        tokens.update(_root_block(css, f':root[data-m-theme="{scheme}"]'))
    return tokens


def _luminance(hex_colour):
    value = hex_colour.strip().lstrip("#")
    channels = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a, b):
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_every_colour_scheme_defines_every_role():
    css = (SOURCE / "src" / "styles" / "tokens.css").read_text(encoding="utf-8")
    base = {key for key in _root_block(css) if key.startswith("--m-") and not key.startswith(("--m-font", "--m-mono", "--m-radius"))}
    schemes = re.findall(r':root\[data-m-theme="([a-z]+)"\]', css)
    assert schemes == _themes()[1:]
    for scheme in schemes:
        block = _root_block(css, f':root[data-m-theme="{scheme}"]')
        themed = {key for key in base if key not in ("--m-ok", "--m-warn", "--m-fault", "--m-white", "--m-black")}
        assert themed <= set(block), f"{scheme} leaves roles to another scheme: {sorted(themed - set(block))}"


@pytest.mark.parametrize(
    ("ink", "grounds"),
    [
        ("--m-text", ("--m-bg", "--m-surface-1", "--m-surface-2", "--m-surface-3")),
        ("--m-muted", ("--m-bg", "--m-surface-1", "--m-surface-2", "--m-surface-3")),
        ("--m-muted-2", ("--m-bg", "--m-surface-1", "--m-surface-2", "--m-surface-3")),
        ("--m-accent-ink", ("--m-accent",)),
        ("--m-selected-ink", ("--m-selected-bg",)),
    ],
)
def test_every_scheme_keeps_its_text_readable(ink, grounds):
    """WCAG AA for normal text, 4.5:1, for every ink on every ground it is written on."""

    css = (SOURCE / "src" / "styles" / "tokens.css").read_text(encoding="utf-8")
    for scheme in _themes():
        tokens = _scheme(css, scheme)
        for ground in grounds:
            ratio = _contrast(tokens[ink], tokens[ground])
            assert ratio >= 4.5, f"{scheme}: {ink} on {ground} is {ratio:.2f}:1"


def test_no_colour_literal_outside_the_token_blocks():
    """A colour belongs to a role. ``#0000`` is how the minifier spells ``transparent``.

    ``::backdrop`` inherits custom properties only from Chrome 122 and Safari
    17.4, so the scrim names the default scheme's value as its fallback; that
    one literal must be the token's own value.
    """

    css = MODERN_CSS.read_text(encoding="utf-8")
    scrim = _root_block(css)["--m-scrim"].strip()
    fallbacks = re.findall(r"var\(--m-scrim,([^)]+)\)", css)
    assert fallbacks and all(value.strip() == scrim for value in fallbacks), (fallbacks, scrim)
    css = re.sub(r"var\(--m-scrim,[^)]+\)", "var(--m-scrim)", css)
    without_tokens = re.sub(r":root(\[[^\]]*\])?\{[^}]*\}", "", css)
    literals = set(re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)", without_tokens))
    assert literals <= {"#0000"}, sorted(literals)


def test_the_flow_layer_has_no_css_animation_and_no_filter():
    """The flow moves with element.animate() and literal keyframes, and its glow is wider strokes, never a filter (reports/dashboard-perf)."""

    css = MODERN_CSS.read_text(encoding="utf-8")
    assert "@keyframes" not in css
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if any(name in selector for name in (".flow-stage", ".wires", ".wire-", ".dash-layer", ".run", ".dashes", ".hub", ".node")):
            assert "animation" not in body, selector
            assert "filter" not in body, selector


def test_every_bundled_package_is_in_the_licence_inventory():
    bundled = json.loads((MODERN / "licenses.json").read_text(encoding="utf-8"))
    names = {entry["name"] for entry in bundled}
    assert {"svelte"} <= names
    inventory = INVENTORY.read_text(encoding="utf-8")
    vendored = inventory.split("## Vendored Components", 1)[1].split("\n## ", 1)[0]
    for entry in bundled:
        assert f"`{entry['name']}`" in vendored, f"{entry['name']} is bundled but not inventoried"
        assert entry.get("identifier") in PERMISSIVE, f"{entry['name']}: {entry.get('identifier')}"


def test_the_font_and_icons_shipped_beside_the_bundle_are_inventoried():
    inventory = INVENTORY.read_text(encoding="utf-8")
    for name in ("`@fontsource-variable/inter`", "`lucide-static`", "dashboard/static/modern/"):
        assert name in inventory, name


def test_the_font_and_icons_ship_with_their_licence_texts():
    """Copied artwork carries its notice: the OFL text, and Lucide's ISC and Feather's MIT notices."""

    inter = (MODERN / "Inter.LICENSE").read_text(encoding="utf-8")
    assert "SIL Open Font License" in inter and "PERMISSION & CONDITIONS" in inter
    lucide = (MODERN / "Lucide.LICENSE").read_text(encoding="utf-8")
    assert "ISC License" in lucide and "Cole Bemis" in lucide


def test_analytics_ranges_are_the_backend_ranges():
    from ems.history.provider import HISTORY_RANGE_SECONDS

    source = (SOURCE / "src" / "lib" / "analytics.js").read_text(encoding="utf-8")
    ranges = re.search(r"ANALYTICS_RANGES = Object\.freeze\(\[([^\]]*)\]\)", source).group(1)
    assert [item.strip().strip('"') for item in ranges.split(",")] == list(HISTORY_RANGE_SECONDS)


def test_the_unit_tests_of_the_modern_frontend_pass():
    """Through the npm script CI runs: Node 22 stopped reading a directory argument as "every test in it"."""

    npm = shutil.which("npm")
    if not npm:
        pytest.skip("npm is required for the Modern frontend unit tests")
    assert len(list((SOURCE / "tests").glob("*.test.js"))) >= 10
    result = subprocess.run(
        [npm, "run", "--silent", "test:dashboard-modern"], cwd=ROOT, capture_output=True, text=True, timeout=300
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert re.search(r"\bpass [1-9]", result.stdout), result.stdout[-2000:]


def test_the_committed_build_is_the_build_of_the_committed_source(tmp_path):
    """The bundle is committed so no install path needs Node; this keeps it honest."""

    vite = ROOT / "node_modules" / ".bin" / "vite"
    if not vite.exists():
        pytest.skip("npm ci has not been run; CI runs the same check in its own job")
    out = tmp_path / "modern"
    result = subprocess.run(
        [str(vite), "build", "--config", str(SOURCE / "vite.config.js"), "--outDir", str(out), "--logLevel", "error"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    committed = {p.relative_to(MODERN): p.read_bytes() for p in MODERN.rglob("*") if p.is_file()}
    rebuilt = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    assert sorted(rebuilt) == sorted(committed)
    stale = [str(name) for name in committed if committed[name] != rebuilt[name]]
    assert not stale, f"rebuild dashboard/static/modern with npm run build:dashboard-modern: {stale}"
