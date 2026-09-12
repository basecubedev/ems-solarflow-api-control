# SPDX-License-Identifier: AGPL-3.0-or-later
"""A design token that several surfaces read has one value, not three.

Admin, the EMS Dashboard and the Appliance Manager are separate deployables with
separate stylesheets, and sixteen token names appear in all three `:root` blocks
-- the shared vocabulary already exists, copied. Copies drift silently: `--border`
was `rgba(120, 140, 170, 0.18)` in Admin and `rgba(148, 163, 184, 0.16)` in the
other two, and nobody noticed, because a difference that small looks like
nothing on one screen at a time.

It stops looking like nothing when there is more than one theme. Then three
copies are three places to maintain, and the drift is three bugs instead of a
shade. agent-rules §2 lists shared UI design tokens in the single-source table
for exactly this reason; this test is what makes the rule enforceable rather
than advisory.

The test compares only the *shared* subset. A token a single surface defines for
itself -- the Dashboard's `--pipe-speed`, Admin's `--card-lift` -- is that
surface's business.
"""

import itertools
import re
from pathlib import Path

import pytest

from tests import theming_contracts
from tests.theming_contracts import SHAPE_PREFIX

pytestmark = [pytest.mark.contract]

ROOT = Path(__file__).resolve().parents[1]

SURFACES = {
    "admin": "admin/static/admin.css",
    "appliance": "appliance/static/styles.css",
    "dashboard": "dashboard/static/styles.css",
}


def root_tokens(relative):
    css = (ROOT / relative).read_text(encoding="utf-8")
    block = re.search(r":root\s*\{(.*?)\}", css, re.S)
    assert block, f"{relative} has no :root block; this test can no longer read it"
    tokens = dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", block.group(1)))
    assert tokens, f"{relative} declares no tokens in :root"
    return {name: " ".join(value.split()) for name, value in tokens.items()}


@pytest.fixture(scope="module")
def declared():
    return {surface: root_tokens(path) for surface, path in SURFACES.items()}


def test_the_three_surfaces_share_a_token_vocabulary(declared):
    """If this shrinks, the surfaces are drifting apart by name as well."""

    shared = set.intersection(*(set(tokens) for tokens in declared.values()))
    assert len(shared) >= 16, f"only {len(shared)} token names are shared: {sorted(shared)}"


def test_a_shared_token_has_the_same_value_everywhere(declared):
    """Two surfaces are enough for drift.

    The comparison is pairwise rather than over the three-way intersection: a
    token Admin and the Manager both read is shared between them whether or not
    the Dashboard has heard of it, and `--surface-sunken` is exactly that -- the
    derived tokens reached the Manager with the palettes and the Dashboard has
    none of them yet.
    """

    drift = {}
    for left, right in itertools.combinations(sorted(SURFACES), 2):
        for name in sorted(set(declared[left]) & set(declared[right])):
            if declared[left][name] != declared[right][name]:
                drift[name] = {left: declared[left][name], right: declared[right][name]}
    assert drift == {}, f"shared tokens with more than one value: {drift}"


# --- the palettes ----------------------------------------------------------
#
# A surface offers palettes or it does not. The ones that do offer the same
# twelve, because a theme is a product-wide choice: an owner who picked
# "graphite" and then opens the Appliance Manager has not changed their mind
# about how the product should look. The stylesheets are separate deployables
# and cannot link one another, so the blocks are a copy -- and a copy of a
# twelve-by-seventeen table is exactly the kind of thing that drifts by one
# value and is never noticed again.


def themed(declared):
    found = {
        surface: theming_contracts.themes((ROOT / path).read_text(encoding="utf-8"))
        for surface, path in SURFACES.items()
    }
    return {surface: palettes for surface, palettes in found.items() if palettes}


def test_the_surfaces_that_offer_palettes_offer_the_same_ones(declared):
    offered = {surface: sorted(palettes) for surface, palettes in themed(declared).items()}
    assert len(offered) >= 2, f"only {sorted(offered)} declares palettes; nothing to compare"
    assert len(set(map(tuple, offered.values()))) == 1, f"the palette lists differ: {offered}"


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


def styled():
    found = {
        surface: theming_contracts.object_styles((ROOT / path).read_text(encoding="utf-8"))
        for surface, path in SURFACES.items()
    }
    return {surface: styles for surface, styles in found.items() if styles}


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
    "admin": "tests/e2e/admin-theme.spec.ts",
    "appliance": "tests/e2e-appliance/theme.spec.ts",
    "dashboard": "tests/e2e-dashboard/theme.spec.ts",
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
        # Two select-id spellings, because the surfaces do not share one: the
        # Admin and the Manager use `#theme-select`, the cockpit `#themeSelect`.
        used[axis] |= set(re.findall(rf'"#{axis}-select",\s*"([a-z0-9-]+)"', spec))
        used[axis] |= set(re.findall(rf'"#{axis}Select",\s*"([a-z0-9-]+)"', spec))
        used[axis] |= set(re.findall(rf'"data-{axis}",\s*"([a-z0-9-]+)"', spec))
    # A name the build is meant not to know is the point of one of the tests.
    used["theme"] -= {"harlequin"}
    unknown = {axis: sorted(names - known[axis]) for axis, names in used.items()}
    unknown = {axis: names for axis, names in unknown.items() if names}
    assert unknown == {}, f"{surface} browser tests name what the stylesheet has not: {unknown}"


def test_the_surfaces_that_offer_styles_offer_the_same_ones():
    offered = {surface: sorted(styles) for surface, styles in styled().items()}
    assert len(offered) == len(SURFACES), f"only {sorted(offered)} declares object styles"
    assert len(set(map(tuple, offered.values()))) == 1, f"the style lists differ: {offered}"


def test_a_style_has_one_value_for_a_corner_everywhere():
    """Three copies of a five-by-four table is exactly the kind of thing that
    drifts by one value and is never noticed again."""

    styles = styled()
    drift = {}
    for left, right in itertools.combinations(sorted(styles), 2):
        for name in sorted(set(styles[left]) & set(styles[right])):
            one, other = styles[left][name], styles[right][name]
            for token in sorted(set(one) & set(other)):
                if one[token] != other[token]:
                    drift[f"{name}.{token}"] = {left: one[token], right: other[token]}
    assert drift == {}, f"object styles that drifted between surfaces: {drift}"


def test_a_palette_has_one_value_for_a_shared_token(declared):
    """The token sets are not identical -- Admin has a raised-surface treatment
    the Manager has no use for -- so the comparison is over what both declare."""

    palettes = themed(declared)
    drift = {}
    for left, right in itertools.combinations(sorted(palettes), 2):
        for name in sorted(set(palettes[left]) & set(palettes[right])):
            one, other = palettes[left][name], palettes[right][name]
            for token in sorted(set(one) & set(other)):
                if one[token] != other[token]:
                    drift[f"{name}.{token}"] = {left: one[token], right: other[token]}
    assert drift == {}, f"palettes that drifted between surfaces: {drift}"


# --- density, the third axis ------------------------------------------------

DEFAULT_DENSITY = "normal"


def dense():
    found = {
        surface: theming_contracts.densities((ROOT / path).read_text(encoding="utf-8"))
        for surface, path in SURFACES.items()
    }
    return {surface: densities for surface, densities in found.items() if densities}


def test_the_surfaces_that_offer_densities_offer_the_same_ones():
    """Three separate deployables on three origins cannot share a stylesheet,
    so they share a vocabulary instead and this is what keeps the copies
    honest. A density the Manager has and the cockpit has not is not a smaller
    feature; it is the same word meaning two things."""

    offered = {surface: sorted(densities) for surface, densities in dense().items()}
    assert len(offered) == len(SURFACES), f"only {sorted(offered)} declares densities"
    assert len(set(map(tuple, offered.values()))) == 1, f"the density lists differ: {offered}"


def test_a_density_means_the_same_number_everywhere():
    """"Compact" has to be one amount of compact. Three copies of three numbers
    is small enough to look safe and exactly the kind of thing that drifts by a
    hundredth and is never noticed again."""

    densities = dense()
    drift = {}
    for left, right in itertools.combinations(sorted(densities), 2):
        for name in sorted(set(densities[left]) & set(densities[right])):
            one, other = densities[left][name], densities[right][name]
            for token in sorted(set(one) & set(other)):
                if one[token] != other[token]:
                    drift[f"{name}.{token}"] = {left: one[token], right: other[token]}
    assert drift == {}, f"densities that drifted between surfaces: {drift}"


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_the_default_density_changes_nothing(surface):
    """The same rule the default palette and the default object style live
    under: a default anyone can see was never the default."""

    css = (ROOT / SURFACES[surface]).read_text(encoding="utf-8")
    declared = theming_contracts.densities(css)
    assert declared.get(DEFAULT_DENSITY) == {theming_contracts.DENSITY_SCALAR: "1"}, (
        f"{surface}: {DEFAULT_DENSITY} is not the scalar's own value"
    )
