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
    DENSITY_SCALAR,
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


# --- nesting ---------------------------------------------------------------
#
# A container that sits inside a card and holds things is a well, not a second
# card. Lifting it lays a film on a surface that already carries one, and the
# deeper it sits the brighter it gets -- which is backwards, and is what made
# the cockpit read as milky on a laptop LCD whatever palette was chosen.
#
# Measured on the Control view in copper. A card is --panel over --bg, L24.8.
# The containers nested inside one rendered at:
#
#     .flow-wrap            39.9 -> 24.1
#     .control-context-rail 42.5 -> 18.1
#     .flow-view-tabs       38.0 -> 31.0
#
# and the tiles they hold came down with them: .control-context-item 46.7 ->
# 22.2. The ground is 15.6 and the one deliberately bright thing on the view,
# the result tile, is 58.2 with a chroma of 41 -- an accent, not a veil, which
# is why it is not in this list.
#
# The list is written out because the stylesheet has no way of saying "this is
# inside a card". A fifth container added later will not be caught here; it
# will be caught by looking at the page, the way these four were.
NESTED_CONTAINERS = {
    ".flow-wrap": "the Live Flow diagram's well, inside .flow-panel",
    ".control-context-rail": "the Control view's context strip, inside a stage card",
    ".energy-context-rail": "the same strip on the Energy view",
    ".flow-view-tabs": "the view switcher, a track holding its buttons",
}


def test_a_container_inside_a_card_does_not_lift():
    """A container holds objects; it is not one.

    The first version of this asked for a fill built from --bg, which was the
    right idea and one level too specific: three of the four now paint nothing
    at all, which recesses them further than any percentage of --bg could. What
    is still forbidden is the thing that was actually wrong -- reaching for
    --veil and coming out brighter than the card the container sits in, so the
    page grows a tone for something that is not an object.
    """

    blocks = {}
    for selector, body in measure.rules(CSS):
        # A selector can carry more than one rule -- the media queries restate
        # several of these to change their layout -- so collect them all rather
        # than letting the last one win, which is how this first read nothing.
        blocks.setdefault(" ".join(selector.split()), []).append(body)
    allowed = {"transparent", "none", "var(--tone-well)"}
    lifting = {}
    for selector, what in NESTED_CONTAINERS.items():
        bodies = blocks.get(selector)
        assert bodies, f"{selector} is gone, so this contract reads nothing: {what}"
        painted = re.findall(r"(?<![a-z-])background:\s*([^;]+)", " ".join(bodies))
        assert painted, f"{selector} declares no background any more: {what}"
        fill = " ".join(" ".join(painted).split())
        # What is left once the object-style wrapper and its `none` layer are
        # taken off is the fill this surface answers for.
        own = re.sub(r"var\(--o-layers,\s*none\),\s*", "", fill)
        own = re.sub(r"^var\(--o-fill,\s*(.*)\)$", r"\1", own.strip())
        if own not in allowed:
            lifting[selector] = own[:90]
    assert lifting == {}, f"containers that are surfaces of their own: {lifting}"


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


# --- density, the third axis ------------------------------------------------
#
# Colour says what things are made of, an object style what shape they are, and
# a density how much room they take. The third axis is a single unitless number
# rather than a table of distances, and that is deliberate: the spacings in this
# stylesheet were tuned against one another, so a density free to redefine them
# one at a time would be free to break that relationship. Multiplying them all
# keeps it, and `calc(8px * var(--d))` still says 8px to whoever reads the rule.

DENSITY_STORAGE_KEY = "ems-dashboard-density"
DEFAULT_DENSITY = "normal"

# What the density does not reach, and why. Nothing is on this list for being
# awkward to convert; each entry is measured against something other than the
# rhythm of the page.
UNSCALED = set()


def densities():
    found = measure.densities(CSS)
    assert found, "styles.css declares no :root[data-density=…] blocks"
    return found


def densities_offered():
    block = re.search(r"const DENSITIES = \[(.*?)\];", JS, re.S)
    assert block, "app.js declares no DENSITIES table"
    return dict(re.findall(r'id:\s*"([a-z0-9-]+)",\s*label:\s*"([^"]+)"', block.group(1)))


def test_every_spacing_answers_to_the_density_axis():
    """A density that reached half the distances would not read as denser, only
    as broken: the card would tighten while the gap between cards held, and the
    page would lose its rhythm instead of its slack.

    Zero, `auto` and negative values are excluded because they are not
    distances -- `auto` is a centring instruction and a negative margin is a
    hairline or hanging-indent trick that has to keep matching the border or
    the icon column it was measured against.
    """

    deaf = [
        (selector, prop, value)
        for selector, prop, value in measure.spacings(CSS)
        if DENSITY_SCALAR not in value and (selector, prop) not in UNSCALED
    ]
    assert deaf == [], f"{len(deaf)} spacings ignore the density, e.g. {deaf[:6]}"


def test_the_default_density_changes_nothing():
    """Choosing "normal" must look exactly like choosing nothing, for the same
    reason `signal` and `glass` must: a default you can see is a default that
    was never really the default."""

    assert densities().get(DEFAULT_DENSITY) == {DENSITY_SCALAR: "1"}


def test_a_density_sets_nothing_but_the_scalar():
    """One number or it is not a density. A block that also set a colour or a
    corner would make "compact, but square" depend on the order they were
    chosen in, which is exactly what three separate axes exist to prevent."""

    trespassing = {
        name: sorted(token for token in tokens if token != DENSITY_SCALAR)
        for name, tokens in densities().items()
    }
    trespassing = {name: found for name, found in trespassing.items() if found}
    assert trespassing == {}, f"densities setting more than the scalar: {trespassing}"


def test_neither_of_the_other_axes_sets_the_density():
    """The mirror of the rule above, and the one that actually gets broken:
    a palette or a style is the natural place to sneak a little more air in."""

    trespassing = {
        f"{kind}:{name}": tokens[DENSITY_SCALAR]
        for kind, axis in (("theme", measure.themes(CSS)), ("style", measure.object_styles(CSS)))
        for name, tokens in axis.items()
        if DENSITY_SCALAR in tokens
    }
    assert trespassing == {}, f"a palette or style setting the density: {trespassing}"


def test_the_density_switcher_offers_exactly_the_densities_the_stylesheet_has():
    listed, declared = set(densities_offered()), set(densities())
    assert declared == listed, (
        f"only in the stylesheet: {sorted(declared - listed)}; "
        f"only in the menu: {sorted(listed - declared)}"
    )


def test_the_stored_density_is_applied_before_the_first_paint():
    """All three axes or none. A page that painted the right colours at the
    wrong spacing and reflowed a moment later would be worse than one that
    waited, because a reflow moves what the reader is already looking at."""

    early = EARLY.read_text(encoding="utf-8")
    assert "data-density" in early, "the early script never sets data-density"
    assert DENSITY_STORAGE_KEY in early, f"the early script does not read {DENSITY_STORAGE_KEY!r}"


def test_both_halves_agree_on_where_the_density_is_stored():
    assert DENSITY_STORAGE_KEY in JS, f"app.js does not write {DENSITY_STORAGE_KEY!r}"


def test_the_three_axes_are_stored_apart():
    """Three keys, because the three choices are independent. One key for all
    of them would make "void, square, compact" unrepresentable -- the thing the
    whole arrangement exists for."""

    assert len({STORAGE_KEY, STYLE_STORAGE_KEY, DENSITY_STORAGE_KEY}) == 3
