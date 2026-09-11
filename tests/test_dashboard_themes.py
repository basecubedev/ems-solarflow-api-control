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


# A battery glyph is a drawing rather than a component: its 3px body and the
# terminal nub beside it are geometry, not a shape anybody would restyle.
# `inherit` is already a role reference -- the parent's -- and is what the two
# travelling-border children want.
LITERAL_CORNERS = {"inherit", "3px", "0 2px 2px 0"}


def test_every_corner_reads_a_role():
    """Fourteen values for five roles, and a second set of them in a media
    query.

    The narrow-screen shrink is the part worth naming: the panels came down a
    pixel, the metric tiles two and the icons two, in six separate rules. That
    is one decision copied six times, so it is now one override of the tokens.

    A value may also be derived from a role -- an overlay drawn a pixel inside
    its parent is that parent's corner minus a pixel -- which is why this looks
    for a role anywhere in the value rather than at the front of it.
    """

    strays = [
        (selector, value)
        for selector, value in measure.corners(CSS)
        if "var(--o-" not in value and value not in LITERAL_CORNERS
    ]
    assert strays == [], f"{len(strays)} rules still choose their own corner: {strays[:8]}"


def test_no_pill_carries_its_own_radius():
    """Twenty-three rules said 999px. An object style can only round or square
    the pills of a page if one value decides it for all of them."""

    literal = re.findall(r"border-radius:\s*999px", measure.body(CSS))
    assert literal == [], f"{len(literal)} rules still round themselves to 999px"
    assert "--o-pill-radius" in measure.shape_tokens(CSS), "the pill radius is not in :root"


# Three rules wear a pill radius without being a pill: the charge bar and its
# fill are a rounded track, the stage dot is a circle with a width and a height,
# and the two tab strips are the groove a row of pills sits in -- their 3px is
# the groove's inset, not a pill's height.
NOT_PILLS = {".soc-bar", ".soc-fill", ".control-stage-dot", ".flow-view-tabs", ".analytics-tabs"}

MEASURE = re.compile(r"(?<![a-z-])(padding|height|min-height):\s*([^;}]+)")


def pill_rules():
    for selector, block in measure.rules(CSS):
        if "--o-pill-radius" not in block:
            continue
        selector = " ".join(selector.split())
        if any(name in selector for name in NOT_PILLS):
            continue
        yield selector, block


def test_every_pill_measures_itself_with_a_role():
    """Eleven paddings and five heights for four kinds of pill.

    A status pill sat at 22px with `0 8px`, a fact tile at 32px with `5px 7px`,
    `5px 6px` or `6px 8px` depending on which one, and the clickable ones at 24,
    25 and 26px in three different strips. None of it was decided; it accreted.

    The heights matter more here than the corners do. This is the densest of the
    three surfaces -- a control card packs a dozen of these -- so two pixels of
    pill height is not two pixels, it is two pixels times twelve rows.
    """

    strays = []
    for selector, block in pill_rules():
        for prop, value in MEASURE.findall(block):
            value = " ".join(value.split())
            if not value.startswith("var(--o-"):
                strays.append((selector[:44], f"{prop}: {value}"))
    assert strays == [], f"{len(strays)} pills still size themselves: {strays[:8]}"


def test_a_pill_role_has_one_measurement():
    """Naming the roles is only half of it -- two tokens for the same role would
    put the drift back with better names on it."""

    declared = measure.shape_tokens(CSS)
    heights = {name for name in declared if name.endswith("-height")}
    paddings = {name for name in declared if name.endswith(("-pad", "-inset"))}
    assert heights, "no pill height is declared"
    assert paddings, "no pill padding is declared"
    duplicates = {
        value: sorted(name for name in group if declared[name] == value)
        for group in (heights, paddings)
        for value in {declared[name] for name in group}
    }
    duplicates = {value: names for value, names in duplicates.items() if len(names) > 1}
    assert duplicates == {}, f"two roles with the same measurement: {duplicates}"


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


# --- the object style ------------------------------------------------------

STYLE_STORAGE_KEY = "ems-dashboard-style"


def styles_offered():
    block = re.search(r"const STYLES = \[(.*?)\];", JS, re.S)
    assert block, "app.js declares no STYLES table"
    return dict(re.findall(r'id:\s*"([a-z0-9-]+)",\s*label:\s*"([^"]+)"', block.group(1)))


def test_the_style_switcher_offers_exactly_the_styles_the_stylesheet_has():
    """The same two lists that cannot see each other as the palettes have, and
    the same reason to keep them honest: a style in the CSS nobody can pick is
    dead weight, one in the menu with no CSS silently does nothing."""

    styled, listed = set(measure.object_styles(CSS)), set(styles_offered())
    assert styled == listed, (
        f"only in the stylesheet: {sorted(styled - listed)}; "
        f"only in the menu: {sorted(listed - styled)}"
    )


def test_the_stored_style_is_applied_before_the_first_paint():
    """Both axes or neither: a page that painted the right colours on the wrong
    corners and then corrected itself would be worse than one that waited."""

    early = EARLY.read_text(encoding="utf-8")
    assert "data-style" in early, "the early script never sets data-style"
    assert STYLE_STORAGE_KEY in early, f"the early script does not read {STYLE_STORAGE_KEY!r}"


def test_both_halves_agree_on_where_the_style_is_stored():
    assert STYLE_STORAGE_KEY in JS, f"app.js does not write {STYLE_STORAGE_KEY!r}"


def test_the_two_axes_are_stored_apart():
    """One key for both would make "void, but square" unrepresentable -- the
    thing the whole arrangement exists for."""

    assert STYLE_STORAGE_KEY != STORAGE_KEY
