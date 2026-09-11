# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a theme has to be, so that picking one cannot half-work.

A theme is a block of design tokens under `:root[data-theme="name"]`. The CSS
cascade makes a forgotten token invisible rather than loud: it simply keeps the
value from `:root`, so a palette that omits `--danger` shows the *previous*
theme's red on its own background and nobody sees a failure, only something
slightly wrong. Every theme therefore has to redefine the whole set.

The first pass is deliberately dark-only. Light grounds are not a token swap:
the neutral veils (`rgba(255,255,255,.035)` to lift a surface, dark rgba to sink
one) assume something dark underneath and invert their meaning on paper. That is
its own piece of work, and this test is what keeps it from being started by
accident.

`signal` is today's Admin. It is a theme like the others so the switcher has a
name for "unchanged", and it must equal the base `:root` -- otherwise choosing
the default would visibly alter the page.
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

pytestmark = [pytest.mark.contract, pytest.mark.admin]

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "admin" / "static" / "admin.css").read_text(encoding="utf-8")

DEFAULT_THEME = "signal"


def base_tokens():
    return measure.base_tokens(CSS)


def themes():
    found = measure.themes(CSS)
    assert found, "admin.css declares no :root[data-theme=…] blocks"
    return found


def derived_tokens():
    return measure.derived_tokens(CSS)


def palette_tokens():
    return measure.palette_tokens(CSS)


def shape_tokens():
    return measure.shape_tokens(CSS)


def test_every_theme_redefines_the_whole_token_set():
    expected = set(palette_tokens())
    incomplete = {name: sorted(expected - set(tokens)) for name, tokens in themes().items()}
    incomplete = {name: missing for name, missing in incomplete.items() if missing}
    assert incomplete == {}, f"themes that would inherit a token from :root: {incomplete}"


def test_no_theme_invents_a_token_the_admin_never_reads():
    """A token nothing reads is a value to maintain for nothing."""

    expected = set(palette_tokens())
    extra = {name: sorted(set(tokens) - expected) for name, tokens in themes().items()}
    extra = {name: names for name, names in extra.items() if names}
    assert extra == {}, f"themes declaring tokens :root does not: {extra}"


def test_every_theme_is_dark():
    """The first pass is dark-only; a light ground is its own piece of work."""

    bright = {
        name: (tokens["--bg"], round(luma(tokens["--bg"])))
        for name, tokens in themes().items()
        if luma(tokens["--bg"]) >= DARK_MAX_LUMA
    }
    assert bright == {}, f"themes that are not dark: {bright}"


def test_the_default_theme_changes_nothing():
    """Choosing "signal" must look exactly like choosing nothing."""

    base = palette_tokens()
    signal = themes().get(DEFAULT_THEME)
    assert signal, f"the default theme {DEFAULT_THEME!r} is not declared"

    def normalise(value):
        return value.replace(" ", "").replace("0.", ".")

    differing = {
        name: (base[name], signal[name])
        for name in palette_tokens()
        if normalise(base[name]) != normalise(signal.get(name, ""))
    }
    assert differing == {}, f"{DEFAULT_THEME} differs from :root: {differing}"


# --- the switcher ----------------------------------------------------------

HTML = (ROOT / "admin" / "static" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "admin" / "static" / "admin.js").read_text(encoding="utf-8")
EARLY = ROOT / "admin" / "static" / "admin-theme.js"

STORAGE_KEY = "ems-admin-theme"


def offered():
    block = re.search(r"const THEMES = \[(.*?)\];", JS, re.S)
    assert block, "admin.js declares no THEMES table"
    return dict(re.findall(r'id:\s*"([a-z0-9-]+)",\s*label:\s*"([^"]+)"', block.group(1)))


def test_the_switcher_offers_exactly_the_themes_the_stylesheet_has():
    """Two lists that must not drift.

    The stylesheet cannot hand JavaScript its labels, so the table is written
    twice and this keeps it honest. A theme in the CSS that nobody can pick is
    dead weight; one in the menu with no CSS silently does nothing.
    """

    styled, listed = set(themes()), set(offered())
    assert styled == listed, (
        f"only in the stylesheet: {sorted(styled - listed)}; "
        f"only in the menu: {sorted(listed - styled)}"
    )


def test_the_stored_theme_is_applied_before_the_first_paint():
    """Otherwise the page paints the default and then visibly changes.

    The Admin CSP is `script-src 'self'`, so this cannot be three lines inline
    in <head>; it is its own file, and it has to be requested before the
    stylesheet it affects.
    """

    assert EARLY.exists(), "admin/static/admin-theme.js is missing"
    head = HTML.split("</head>", 1)[0]
    assert "admin-theme.js" in head, "the early theme script is not in <head>"
    assert head.index("admin-theme.js") < head.index("admin.css"), (
        "the theme script must run before the stylesheet it affects"
    )


def test_both_halves_agree_on_where_the_choice_is_stored():
    """The early script reads it and the console writes it; one key, or the
    choice is applied from a slot nothing ever fills."""

    early = EARLY.read_text(encoding="utf-8")
    assert STORAGE_KEY in early, f"the early script does not read {STORAGE_KEY!r}"
    assert STORAGE_KEY in JS, f"admin.js does not write {STORAGE_KEY!r}"


def test_an_unreadable_store_does_not_break_the_page():
    """Private mode throws on localStorage; the default theme is the right
    answer there, not a stack trace before the first paint."""

    early = EARLY.read_text(encoding="utf-8")
    assert "try" in early and "catch" in early, "the early script does not guard localStorage"


# --- the shape axis --------------------------------------------------------


def test_a_theme_never_touches_the_shape_axis():
    """A palette that also set a radius would make "void, but square" impossible.

    The two axes are what the demo proved out: fifteen palettes times thirteen
    object styles, on identical markup. That only works while neither axis
    reaches into the other.
    """

    trespassing = {
        name: sorted(token for token in tokens if token.startswith(SHAPE_PREFIX))
        for name, tokens in themes().items()
    }
    trespassing = {name: found for name, found in trespassing.items() if found}
    assert trespassing == {}, f"themes setting shape tokens: {trespassing}"


def test_no_pill_carries_its_own_radius():
    """Eighteen rules said 999px. An object style can only round or square the
    pills of a page if one value decides it for all of them."""

    body = re.sub(r':root(\[data-theme="[a-z0-9-]+"\])?\s*\{.*?\}', "", CSS, flags=re.S)
    literal = re.findall(r"border-radius:\s*999px", body)
    assert literal == [], f"{len(literal)} rules still round themselves to 999px"
    assert "--o-pill-radius" in shape_tokens(), "the pill radius is not declared in :root"


# Two corners are not roles. A spinner is a circle because it turns, and an
# object style that squared it would break the animation rather than restyle it;
# `code` is a run of text, not a component with edges.
LITERAL_CORNERS = {"50%", ".25rem"}


def corners():
    return measure.corners(CSS)


def test_every_corner_reads_a_role():
    """Radii drifted into values nobody chose: cards at 12, 14 and 16px, rows at
    7, 8, 9 and 10px. Naming the value would freeze the drift -- `--o-r10` says
    nothing an object style can act on. Naming the *role* is what lets one say
    "square everything", because it can then answer for cards and rows
    separately, which is the whole point of the second axis.
    """

    strays = [
        (selector, value)
        for selector, value in corners()
        if not value.startswith("var(--o-") and value not in LITERAL_CORNERS
    ]
    assert strays == [], f"{len(strays)} rules still choose their own corner: {strays[:8]}"


def test_the_shape_axis_names_every_role_it_uses():
    """A role read but never declared resolves to nothing, and the corner
    silently becomes square."""

    declared = set(shape_tokens())
    used = {
        match
        for _, value in corners()
        for match in re.findall(r"var\((--o-[a-z0-9-]+)\)", value)
    }
    assert used <= declared, f"corners reading tokens :root does not declare: {sorted(used - declared)}"


def test_badges_of_the_same_size_share_one_padding():
    """Badges drifted between `1px 7px`, `1px 8px` and `2px 8px` for the same
    kind of label. Height follows font size, so the small ones are one group."""

    body = re.sub(r':root(\[data-theme="[a-z0-9-]+"\])?\s*\{.*?\}', "", CSS, flags=re.S)
    paddings = set()
    for selector, block in re.findall(r"([^{}]+)\{([^{}]*)\}", body):
        size = re.search(r"(?<![a-z-])font-size:\s*(\d+)px", block)
        rounded = "--o-pill-radius" in block or "999px" in block
        if not rounded or not size or int(size.group(1)) > 10:
            continue
        padding = re.search(r"(?<![a-z-])padding:\s*([^;]+);", block)
        if padding:
            paddings.add(" ".join(padding.group(1).split()))
    assert len(paddings) <= 1, f"small badges still use several paddings: {sorted(paddings)}"


# --- readability -----------------------------------------------------------


def test_every_theme_stays_readable():
    """A palette is a set of colours until someone has to read text on it.

    Twelve themes clear this today, the weakest at 7.0 for muted copy. The
    thirteenth is the one this exists for: a palette can be picked for how it
    looks in a swatch and turn out unreadable in a sentence, and nothing else in
    the suite would notice.
    """

    failures = {}
    for name, tokens in themes().items():
        background = channels(tokens["--bg"])
        text = contrast(channels(tokens["--text"]), background)
        muted = contrast(channels(tokens["--muted"]), background)
        if text < TEXT_MIN_CONTRAST or muted < MUTED_MIN_CONTRAST:
            failures[name] = {"text": round(text, 1), "muted": round(muted, 1)}

    assert failures == {}, (
        f"themes below WCAG (text >= {TEXT_MIN_CONTRAST}, muted >= {MUTED_MIN_CONTRAST}): {failures}"
    )


def test_pill_shaped_controls_share_one_padding():
    """A pill you can click is a third thing, next to the badge and the status
    pill: bigger, because it has to be hit, and sized by its role rather than
    by the label inside it.

    The back button and the theme menu sit side by side in the header, so two
    paddings there were visible as two heights. `cursor: pointer` is what tells
    them apart from a label -- it is in the stylesheet because these are the
    rules that respond to a click.
    """

    body = re.sub(r':root(\[data-theme="[a-z0-9-]+"\])?\s*\{.*?\}', "", CSS, flags=re.S)
    paddings = set()
    for _, block in re.findall(r"([^{}]+)\{([^{}]*)\}", body):
        if "--o-pill-radius" not in block or "cursor: pointer" not in block:
            continue
        padding = re.search(r"(?<![a-z-])padding:\s*([^;]+);", block)
        if padding:
            paddings.add(" ".join(padding.group(1).split()))
    assert len(paddings) <= 1, f"pill-shaped controls still differ: {sorted(paddings)}"


# --- reach -----------------------------------------------------------------

# White and black with an alpha are ground-agnostic on a dark theme: a 3.5%
# white veil lifts any dark surface and means the same on all twelve. Anything
# with a hue does not -- it was mixed for one particular background, and on the
# next theme it is simply the wrong colour sitting there.
def hued_literals():
    return measure.hued_literals(CSS)


def test_no_rule_mixes_its_own_colour():
    """A theme reaches what reads tokens, and nothing else.

    Forty-three rules carried a hue of their own -- a sunken input at
    `rgba(8, 12, 20, 0.7)`, an accent tint at `rgba(34, 211, 238, 0.16)`. Every
    one was chosen against the one background that existed at the time, so on a
    near-black theme the sunken surface is lighter than the thing it sinks into,
    and on a warm one the tint is the wrong colour entirely. None of it fails
    loudly; it just quietly is not themed.
    """

    remaining = hued_literals()
    assert remaining == [], (
        f"{len(remaining)} rules still carry their own hue: {sorted(set(remaining))[:8]}"
    )


def test_a_theme_never_redefines_a_derived_token():
    """A derived token is the mechanism that spares twelve hand-written values.

    Overriding one in a palette does not extend the system, it opts that palette
    out of it -- and silently, because the result still renders.
    """

    trespassing = {
        name: sorted(set(tokens) & set(derived_tokens())) for name, tokens in themes().items()
    }
    trespassing = {name: found for name, found in trespassing.items() if found}
    assert trespassing == {}, f"themes redefining a derived token: {trespassing}"


# --- the object style ------------------------------------------------------

STYLE_STORAGE_KEY = "ems-admin-style"


def styles_offered():
    block = re.search(r"const STYLES = \[(.*?)\];", JS, re.S)
    assert block, "admin.js declares no STYLES table"
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
    assert STYLE_STORAGE_KEY in JS, f"admin.js does not write {STYLE_STORAGE_KEY!r}"


def test_the_two_axes_are_stored_apart():
    """One key for both would make "void, but square" unrepresentable -- the
    thing the whole arrangement exists for."""

    assert STYLE_STORAGE_KEY != STORAGE_KEY
