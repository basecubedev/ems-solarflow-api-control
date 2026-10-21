# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checks every Modern front end's committed build has to pass.

The Modern dashboard and the Modern Admin console are built from one design
system (``frontend/shared/``) and committed as build output, so no install
path needs Node. Each app's contract module calls these on its own output; the
design system's own rules (colour schemes, contrast) live in
``test_modern_design_system.py``.
"""

import json
import re
import shutil
import subprocess
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT / "frontend" / "shared"
TOKENS_CSS = SHARED / "styles" / "tokens.css"
PREFS = SHARED / "state" / "prefs.svelte.js"
INVENTORY = ROOT / "THIRD_PARTY_LICENSES.md"
PERMISSIVE = {"MIT", "ISC", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "OFL-1.1", "0BSD"}


def root_block(css, selector=":root"):
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, f"{selector} block not found"
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", match.group(1)))


def themes():
    """The scheme ids the Appearance popover offers, read from ``THEMES``; the first is ``:root``."""

    source = PREFS.read_text(encoding="utf-8")
    match = re.search(r"export const THEMES = Object\.freeze\(\[([^\]]*)\]\)", source)
    assert match, "THEMES not found"
    return re.findall(r'"([a-z]+)"', match.group(1))


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


def assert_no_inline_script_or_style(page_html, bundle_js):
    """The CSP allows neither; an inline chunk would break the page with no visible error."""

    page = _PageElements()
    page.feed(page_html)
    page.close()
    assert page.scripts and all(script.get("src") for script in page.scripts), page.scripts
    assert page.styles == 0
    assert page.style_attributes == []
    assert not re.search(r'style=\\?"', bundle_js), "a template would set a style attribute the CSP blocks"


def assert_tab_icon_is_the_mark(page_html):
    """The favicon is built from frontend/shared/brand/mark.js; its colours must be the scheme's own."""

    links = re.findall(r'<link rel="icon" type="image/svg\+xml" href="data:image/svg\+xml,([^"]+)">', page_html)
    assert len(links) == 1, links
    svg = urllib.parse.unquote(links[0])
    root = root_block(TOKENS_CSS.read_text(encoding="utf-8"))
    for token in ("--pv", "--battery", "--output", "--grid", "--m-surface-2", "--m-line-strong", "--m-text"):
        assert f'"{root[token].strip()}"' in svg, token
    assert "var(" not in svg


def assert_no_colour_literal_outside_the_token_blocks(css):
    """A colour belongs to a role. ``#0000`` is how the minifier spells ``transparent``.

    ``::backdrop`` inherits custom properties only from Chrome 122 and Safari
    17.4, so the scrim names the default scheme's value as its fallback; that
    one literal must be the token's own value.
    """

    scrim = root_block(css)["--m-scrim"].strip()
    fallbacks = re.findall(r"var\(--m-scrim,([^)]+)\)", css)
    assert fallbacks and all(value.strip() == scrim for value in fallbacks), (fallbacks, scrim)
    css = re.sub(r"var\(--m-scrim,[^)]+\)", "var(--m-scrim)", css)
    without_tokens = re.sub(r":root(\[[^\]]*\])?\{[^}]*\}", "", css)
    literals = set(re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)", without_tokens))
    assert literals <= {"#0000"}, sorted(literals)


def assert_bundled_packages_inventoried(build_dir, required=("svelte",)):
    bundled = json.loads((build_dir / "licenses.json").read_text(encoding="utf-8"))
    names = {entry["name"] for entry in bundled}
    assert set(required) <= names
    inventory = INVENTORY.read_text(encoding="utf-8")
    vendored = inventory.split("## Vendored Components", 1)[1].split("\n## ", 1)[0]
    for entry in bundled:
        assert f"`{entry['name']}`" in vendored, f"{entry['name']} is bundled but not inventoried"
        assert entry.get("identifier") in PERMISSIVE, f"{entry['name']}: {entry.get('identifier')}"


def assert_font_and_icons_inventoried(build_dir):
    inventory = INVENTORY.read_text(encoding="utf-8")
    shipped = build_dir.relative_to(ROOT).as_posix() + "/"
    for name in ("`@fontsource-variable/inter`", "`lucide-static`", shipped):
        assert name in inventory, name


def assert_font_and_icons_carry_their_notices(build_dir):
    """Copied artwork carries its notice: the OFL text, and Lucide's ISC and Feather's MIT notices."""

    inter = (build_dir / "Inter.LICENSE").read_text(encoding="utf-8")
    assert "SIL Open Font License" in inter and "PERMISSION & CONDITIONS" in inter
    lucide = (build_dir / "Lucide.LICENSE").read_text(encoding="utf-8")
    assert "ISC License" in lucide and "Cole Bemis" in lucide


def run_npm_script(script):
    """Through the npm script CI runs: Node 22 stopped reading a directory argument as "every test in it"."""

    npm = shutil.which("npm")
    if not npm:
        return None
    result = subprocess.run(
        [npm, "run", "--silent", script], cwd=ROOT, capture_output=True, text=True, timeout=300
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert re.search(r"\bpass [1-9]", result.stdout), result.stdout[-2000:]
    return result


def assert_build_matches_source(config, committed, out, hint):
    """The bundle is committed so no install path needs Node; this keeps it honest.

    Returns ``False`` when Vite is not installed, so the caller can skip.
    """

    vite = ROOT / "node_modules" / ".bin" / "vite"
    if not vite.exists():
        return False
    result = subprocess.run(
        [str(vite), "build", "--config", str(config), "--outDir", str(out), "--logLevel", "error"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    built = {p.relative_to(committed): p.read_bytes() for p in committed.rglob("*") if p.is_file()}
    rebuilt = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    assert sorted(rebuilt) == sorted(built)
    stale = [str(name) for name in built if built[name] != rebuilt[name]]
    assert not stale, f"rebuild with {hint}: {stale}"
    return True
