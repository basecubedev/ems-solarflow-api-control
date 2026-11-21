# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A stylesheet that offers palettes, object styles and densities keeps their contract.

The Modern dashboard and the Modern Admin console read their tokens from one
file, ``frontend/shared/styles/tokens.css``, which ``test_modern_design_system.py``
holds. The Appliance Manager's page carries its own stylesheet, and this module
holds it to the theming contract: it parses, its ground is the palette's, a rule
names one of four tones instead of inventing a surface level, its copy stays
readable on a tile, an object style sets corners and nothing else, a density sets
its scalar, and the browser tests name only what the stylesheet has.
"""

import re
from pathlib import Path

import pytest

from tests import theming_contracts
from tests.theming_contracts import MUTED_MIN_CONTRAST, SHAPE_PREFIX, TEXT_MIN_CONTRAST

pytestmark = [pytest.mark.contract]

ROOT = Path(__file__).resolve().parents[1]

SURFACES = {
    "appliance": "appliance/static/styles.css",
}


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_the_stylesheet_parses(surface):
    """Every other test in this file reads the stylesheet with regular
    expressions, which is why they all stayed green while a stylesheet was
    missing a closing brace and the page rendered as unstyled HTML. Balanced
    braces is the cheapest thing that would have caught it, and the only
    structural claim these tests can make without a real parser.
    """

    css = (ROOT / SURFACES[surface]).read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    depth, opened = 0, 0
    for character in css:
        if character == "{":
            depth += 1
            opened += 1
        elif character == "}":
            depth -= 1
            assert depth >= 0, f"{surface}: a rule closes that was never opened"
    assert depth == 0, f"{surface}: {depth} of {opened} rules are never closed"


# --- the ground ------------------------------------------------------------
#
# The one surface every palette is judged against, and the one that used to be
# nobody's: each stylesheet blended --bg into --bg2 and laid its own accent
# washes over that, so the colour a palette names as its ground was never the
# colour on screen.

GROUND = "var(--glow), var(--bg)"


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_the_ground_is_the_palette_and_a_wash_the_palette_chose(surface):
    """`body` paints --bg, and over it only what the palette asked for.

    Nothing about the old ground failed loudly. Measured on the cockpit in
    copper, it lifted the darkest tone on the page to L17.6 where --bg is
    L12.1, and left `void` -- a palette whose whole idea is #000 -- without a
    single black pixel. The wash moved into --glow, which a palette sets like
    any other colour, so a palette that wants a flat ground can have one; eight
    of the twelve do.
    """

    css = re.sub(r"/\*.*?\*/", "", (ROOT / SURFACES[surface]).read_text(encoding="utf-8"), flags=re.S)
    rule = re.search(r"^body \{(.*?)^\}", css, re.S | re.M)
    assert rule, f"{surface}: no body rule to read"
    painted = [
        " ".join(value.split())
        for value in re.findall(r"(?<![a-z-])background:\s*([^;]+);", rule.group(1))
    ]
    assert painted == [GROUND], f"{surface}: body paints {painted}, not [{GROUND!r}]"


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_a_wash_is_a_wash_and_not_a_second_ground(surface):
    """--glow is laid over --bg, so it has to be something you can see through.

    A palette that put a solid colour there would cover the ground instead of
    tinting it, and --bg would quietly stop meaning anything on that palette --
    while still passing every contract that reads --bg as a value.
    """

    css = (ROOT / SURFACES[surface]).read_text(encoding="utf-8")
    palettes = theming_contracts.themes(css)
    assert palettes, f"{surface} declares no palettes"
    solid = {
        name: tokens["--glow"]
        for name, tokens in palettes.items()
        if tokens["--glow"] != "none" and not tokens["--glow"].startswith("radial-gradient(")
    }
    assert solid == {}, f"{surface}: palettes whose wash is not a gradient: {solid}"


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_no_rule_lays_plain_white_on_a_palette(surface):
    """A film takes its colour from the palette it is lying on.

    `hued_literals` let a rule keep a literal as long as it was neutral, and
    the reasoning was half right: black sinks a surface toward every palette's
    own ground, because every ground here is dark. White lifts it toward a
    colour no palette has. There were 106 of them -- hovers, inset highlights,
    the inside of a fact tile -- and on copper they measured grey while the
    rest of the page was warm. They read `--veil` now, which is the palette's
    own light and is tinted in eleven of the twelve.
    """

    css = (ROOT / SURFACES[surface]).read_text(encoding="utf-8")
    remaining = theming_contracts.white_films(css)
    assert remaining == [], (
        f"{surface}: {len(remaining)} rules still paint plain white: "
        f"{sorted(set(remaining))[:6]}"
    )


# --- the object styles -----------------------------------------------------
#
# The second axis. A palette says what things are made of; an object style says
# what shape they are. Every combination of the two has to render, which is only
# true while neither reaches into the other -- so a style sets corner roles and
# nothing else, and a palette sets colours and nothing else.
#
# Only the corner roles vary. The pill measurements and the paddings are density
# rather than shape, and a style that moved the height of every fact tile would
# be moving the layout rather than restyling it.

DEFAULT_STYLE = "glass"


# A rule may draw on a surface -- a sheen, a hairline grid, an accent tint --
# but the surface itself is one of four named tones. What is on this list is not
# a surface.
NOT_A_SURFACE = {
    "appliance": set(),
}


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_a_rule_names_a_tone_instead_of_inventing_one(surface):
    """Four tones, and nothing else decides how far from the ground a surface is.

    This is the contract the appearance work was actually missing. Every rule
    was free to mix its own fill, and they did: forty-one different neutral
    lifts in the Dashboard alone, at 2.2%, 2.5%, 2.6%, 3%, 3.2%, 3.5%, 3.6%,
    3.8%, 4%, 4.5%, 5%, 5.5% and 16%. No two of those are distinguishable and
    all of them together were, measured on the Control view in copper, twenty
    painted surfaces resolving to thirteen tones two units apart -- which is
    what "it is not clear" looks like when you count it.

    Tinting a tone is still allowed and is how a card says something about
    itself: `color-mix(in srgb, var(--grid) 5%, var(--tone-card))` is a card
    that is about the grid meter. What is forbidden is deciding the level.
    """

    css = (ROOT / SURFACES[surface]).read_text(encoding="utf-8")
    invented = {
        selector: base
        for selector, base in theming_contracts.surface_fills(css)
        if theming_contracts.invents_a_tone(base) and selector not in NOT_A_SURFACE[surface]
    }
    assert invented == {}, f"{surface} rules mixing their own surface level: {invented}"


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_the_four_tones_are_the_same_four_everywhere(surface):
    """A surface cannot quietly stop declaring a tone and fall back to
    whatever `var()` does with an unknown name, which is nothing."""

    css = (ROOT / SURFACES[surface]).read_text(encoding="utf-8")
    declared = theming_contracts.base_tokens(css)
    missing = [name for name in theming_contracts.TONES if name not in declared]
    assert missing == [], f"{surface} declares no {missing}"



@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_muted_copy_stays_readable_on_the_surface_it_sits_on(surface):
    """The ground is not where most text lands.

    `test_every_theme_stays_readable` holds --text and --muted against --bg,
    which is the darkest thing on the page and therefore the easy case. Text
    sits on a card and, more often, on a tile inside one -- and raising a tone
    is now a single edit that would quietly take the contrast down with it on
    every palette at once. So the check is against --tone-inner, the brightest
    surface ordinary copy sits on.

    --muted2 is deliberately not held to this. It is the faintest of the three
    tiers, used for a tiny uppercase label beside the value it labels, and it
    has never cleared 4.5 on a tile in any palette -- 3.0 to 7.4 before the
    tones and 2.8 to 7.2 after. Writing a floor it does not meet would be a
    failing test rather than a contract; what this pins is that the tier
    ordinary copy uses does not join it.
    """

    css = (ROOT / SURFACES[surface]).read_text(encoding="utf-8")
    base = theming_contracts.base_tokens(css)
    palettes = theming_contracts.themes(css)
    if not palettes:
        pytest.skip(f"{surface} offers no palettes")
    # Read the strength out of the token rather than repeating it here. Writing
    # 0.11 into this test would have made it blind to the one edit it exists to
    # catch -- raising --tone-inner to 26% left it passing.
    share = theming_contracts.mix_share(base["--tone-inner"])
    failures = {}
    for name, tokens in palettes.items():

        def read(token, tokens=tokens):
            return tokens.get(token, base.get(token))

        ground = theming_contracts.channels(read("--bg"))
        light = theming_contracts.channels(read("--text"))
        inner = tuple(light[i] * share + ground[i] * (1 - share) for i in range(3))
        text = theming_contracts.contrast(light, inner)
        muted = theming_contracts.contrast(theming_contracts.channels(read("--muted")), inner)
        if text < TEXT_MIN_CONTRAST or muted < MUTED_MIN_CONTRAST:
            failures[name] = {"text": round(text, 2), "muted": round(muted, 2)}
    assert failures == {}, f"{surface} palettes that are hard to read on a tile: {failures}"


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_every_object_style_sets_every_corner_role(surface):
    """A role a style leaves out keeps its :root value, so "edge" would render
    with one rounded corner somewhere and look like a bug rather than a choice.
    """

    css = (ROOT / SURFACES[surface]).read_text(encoding="utf-8")
    expected = set(theming_contracts.radius_tokens(css))
    assert expected, f"{surface} declares no corner roles"
    incomplete = {
        name: sorted(expected - set(tokens))
        for name, tokens in theming_contracts.object_styles(css).items()
    }
    incomplete = {name: missing for name, missing in incomplete.items() if missing}
    assert incomplete == {}, f"{surface} styles that would inherit a corner: {incomplete}"


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_no_object_style_reaches_into_the_palette(surface):
    css = (ROOT / SURFACES[surface]).read_text(encoding="utf-8")
    trespassing = {
        name: sorted(token for token in tokens if not token.startswith(SHAPE_PREFIX))
        for name, tokens in theming_contracts.object_styles(css).items()
    }
    trespassing = {name: extra for name, extra in trespassing.items() if extra}
    assert trespassing == {}, f"{surface} styles setting something else: {trespassing}"


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_the_default_object_style_changes_nothing(surface):
    """Choosing "rounded" must look exactly like choosing nothing."""

    css = (ROOT / SURFACES[surface]).read_text(encoding="utf-8")
    base = theming_contracts.radius_tokens(css)
    default = theming_contracts.object_styles(css).get(DEFAULT_STYLE)
    assert default, f"{surface} does not declare the default style {DEFAULT_STYLE!r}"
    differing = {
        name: (base[name], default.get(name))
        for name in base
        if base[name] != default.get(name)
    }
    assert differing == {}, f"{surface}: {DEFAULT_STYLE} differs from :root: {differing}"


SPECS = {
    "appliance": "tests/e2e-appliance/theme.spec.ts",
}


@pytest.mark.parametrize("surface", sorted(SPECS))
def test_the_browser_tests_name_palettes_styles_and_densities_that_exist(surface):
    """Renaming the four first styles to the demo's thirteen left two browser
    tests selecting `crisp` and `edge`, which no longer existed. Playwright
    reported it as "did not find some options" after six minutes of browser
    time; this says the same thing in a tenth of a second.
    """

    spec = (ROOT / SPECS[surface]).read_text(encoding="utf-8")
    css = (ROOT / SURFACES[surface]).read_text(encoding="utf-8")
    known = {
        "style": set(theming_contracts.object_styles(css)),
        "theme": set(theming_contracts.themes(css)),
        "density": set(theming_contracts.densities(css)),
    }
    used = {"style": set(), "theme": set(), "density": set()}
    for axis in used:
        used[axis] |= set(re.findall(rf'"#{axis}-select",\s*"([a-z0-9-]+)"', spec))
        used[axis] |= set(re.findall(rf'"data-{axis}",\s*"([a-z0-9-]+)"', spec))
    # A name the build is meant not to know is the point of one of the tests.
    used["theme"] -= {"harlequin"}
    unknown = {axis: sorted(names - known[axis]) for axis, names in used.items()}
    unknown = {axis: names for axis, names in unknown.items() if names}
    assert unknown == {}, f"{surface} browser tests name what the stylesheet has not: {unknown}"


# --- density, the third axis ------------------------------------------------

DEFAULT_DENSITY = "normal"


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_the_default_density_changes_nothing(surface):
    """The same rule the default palette and the default object style live
    under: a default anyone can see was never the default."""

    css = (ROOT / SURFACES[surface]).read_text(encoding="utf-8")
    declared = theming_contracts.densities(css)
    assert declared.get(DEFAULT_DENSITY) == {theming_contracts.DENSITY_SCALAR: "1"}, (
        f"{surface}: {DEFAULT_DENSITY} is not the scalar's own value"
    )
