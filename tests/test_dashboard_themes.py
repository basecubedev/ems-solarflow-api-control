# SPDX-License-Identifier: AGPL-3.0-or-later
"""The cockpit answers to the same twelve palettes as the other two surfaces.

The Dashboard is the largest of the three stylesheets and the one a household
actually leaves on a screen, which makes the palette a comfort question rather
than a preference: a cockpit that sits in a living room at night is a different
object from one on a desk at noon.

This module asks what is local to this stylesheet. What keeps its copy of the
palettes from drifting against Admin's and the Manager's is
`test_shared_design_tokens.py`, which compares them across surfaces.

The shape axis is deliberately only half here: the pills read the shared radius,
because that costs nothing and finishes the second step across all three
surfaces. The remaining corners and the pill *heights* are their own pass --
this is the densest surface there is, and moving a pill's height by two pixels
moves the vertical rhythm of every packed control card with it.
"""

import re
from pathlib import Path

import pytest

from tests import theming_contracts as measure
from tests.theming_contracts import (
    DARK_MAX_LUMA,
    MUTED_MIN_CONTRAST,
    SHAPE_PREFIX,
    TEXT_MIN_CONTRAST,
    channels,
    contrast,
    luma,
)

pytestmark = [pytest.mark.contract]

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "dashboard" / "static"
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
EARLY = STATIC / "theme.js"

DEFAULT_THEME = "signal"
STORAGE_KEY = "ems-dashboard-theme"


def themes():
    found = measure.themes(CSS)
    assert found, "styles.css declares no :root[data-theme=…] blocks"
    return found


def palette_tokens():
    return measure.palette_tokens(CSS)


def test_every_theme_redefines_the_whole_token_set():
    expected = set(palette_tokens())
    incomplete = {name: sorted(expected - set(tokens)) for name, tokens in themes().items()}
    incomplete = {name: missing for name, missing in incomplete.items() if missing}
    assert incomplete == {}, f"themes that would inherit a token from :root: {incomplete}"


def test_no_theme_invents_a_token_the_cockpit_never_reads():
    expected = set(palette_tokens())
    extra = {name: sorted(set(tokens) - expected) for name, tokens in themes().items()}
    extra = {name: names for name, names in extra.items() if names}
    assert extra == {}, f"themes declaring tokens :root does not: {extra}"


def test_every_theme_is_dark():
    bright = {
        name: (tokens["--bg"], round(luma(tokens["--bg"])))
        for name, tokens in themes().items()
        if luma(tokens["--bg"]) >= DARK_MAX_LUMA
    }
    assert bright == {}, f"themes that are not dark: {bright}"


def test_the_default_theme_changes_nothing():
    base = palette_tokens()
    signal = themes().get(DEFAULT_THEME)
    assert signal, f"the default theme {DEFAULT_THEME!r} is not declared"

    def normalise(value):
        return value.replace(" ", "").replace("0.", ".")

    differing = {
        name: (base[name], signal[name])
        for name in base
        if normalise(base[name]) != normalise(signal.get(name, ""))
    }
    assert differing == {}, f"{DEFAULT_THEME} differs from :root: {differing}"


def test_every_theme_stays_readable():
    failures = {}
    for name, tokens in themes().items():
        background = channels(tokens["--bg"])
        text = contrast(channels(tokens["--text"]), background)
        muted = contrast(channels(tokens["--muted"]), background)
        if text < TEXT_MIN_CONTRAST or muted < MUTED_MIN_CONTRAST:
            failures[name] = {"text": round(text, 1), "muted": round(muted, 1)}
    assert failures == {}, f"themes that are hard to read: {failures}"


def test_a_theme_never_redefines_a_derived_token():
    """Eight of this stylesheet's tokens are built from others -- the five tinted
    text colours and the three cool highlights. A palette that redefined one
    would opt itself out of the derivation, silently, because it still renders.
    """

    derived = measure.derived_tokens(CSS)
    assert len(derived) >= 8, f"the derived tokens are gone or renamed: {sorted(derived)}"
    trespassing = {name: sorted(set(tokens) & set(derived)) for name, tokens in themes().items()}
    trespassing = {name: found for name, found in trespassing.items() if found}
    assert trespassing == {}, f"themes overriding a derived token: {trespassing}"


# --- the switcher ----------------------------------------------------------


def offered():
    block = re.search(r"const THEMES = \[(.*?)\];", JS, re.S)
    assert block, "app.js declares no THEMES table"
    return dict(re.findall(r'id:\s*"([a-z0-9-]+)",\s*label:\s*"([^"]+)"', block.group(1)))


def test_the_switcher_offers_exactly_the_themes_the_stylesheet_has():
    styled, listed = set(themes()), set(offered())
    assert styled == listed, (
        f"only in the stylesheet: {sorted(styled - listed)}; "
        f"only in the menu: {sorted(listed - styled)}"
    )


def test_the_stored_theme_is_applied_before_the_first_paint():
    """The cockpit's CSP is `script-src 'self'`, so this cannot be three lines
    inline in <head>; it is its own file, requested before the stylesheet."""

    assert EARLY.exists(), "dashboard/static/theme.js is missing"
    head = HTML.split("</head>", 1)[0]
    assert "theme.js" in head, "the early theme script is not in <head>"
    assert head.index("theme.js") < head.index("styles.css"), (
        "the theme script must run before the stylesheet it affects"
    )


def test_both_halves_agree_on_where_the_choice_is_stored():
    early = EARLY.read_text(encoding="utf-8")
    assert STORAGE_KEY in early, f"the early script does not read {STORAGE_KEY!r}"
    assert STORAGE_KEY in JS, f"app.js does not write {STORAGE_KEY!r}"


def test_each_surface_keeps_its_own_stored_choice():
    """Three origins, three localStorages. A shared key name would only suggest
    a choice carries over when it cannot."""

    assert STORAGE_KEY not in ("ems-admin-theme", "ems-appliance-theme")


def test_an_unreadable_store_does_not_break_the_page():
    early = EARLY.read_text(encoding="utf-8")
    assert "try" in early and "catch" in early, "the early script does not guard localStorage"


# --- the shape axis, as far as it goes here --------------------------------


def test_a_theme_never_touches_the_shape_axis():
    trespassing = {
        name: sorted(token for token in tokens if token.startswith(SHAPE_PREFIX))
        for name, tokens in themes().items()
    }
    trespassing = {name: found for name, found in trespassing.items() if found}
    assert trespassing == {}, f"themes setting shape tokens: {trespassing}"


def test_no_pill_carries_its_own_radius():
    """Twenty-three rules said 999px. An object style can only round or square
    the pills of a page if one value decides it for all of them."""

    literal = re.findall(r"border-radius:\s*999px", measure.body(CSS))
    assert literal == [], f"{len(literal)} rules still round themselves to 999px"
    assert "--o-pill-radius" in measure.shape_tokens(CSS), "the pill radius is not in :root"


# --- reach -----------------------------------------------------------------


def test_no_rule_mixes_its_own_colour():
    """A palette reaches what reads tokens, and nothing else.

    Two hundred and eighty rules carried a colour of their own. None of it
    failed loudly; it simply was not themed, and on any palette but the one
    they were mixed against they would be the wrong colour sitting there.
    """

    remaining = measure.hued_literals(CSS)
    assert remaining == [], (
        f"{len(remaining)} rules still carry their own hue: {sorted(set(remaining))[:8]}"
    )
