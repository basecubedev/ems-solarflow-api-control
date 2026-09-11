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

pytestmark = [pytest.mark.contract, pytest.mark.admin]

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "admin" / "static" / "admin.css").read_text(encoding="utf-8")

DEFAULT_THEME = "signal"
# Rec. 709 luma; a ground this dark cannot be mistaken for a light theme.
DARK_MAX_LUMA = 90


def declarations(block):
    return {
        name: " ".join(value.split())
        for name, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", block)
    }


def base_tokens():
    block = re.search(r":root\s*\{(.*?)\}", CSS, re.S)
    assert block, "admin.css has no :root block"
    return declarations(block.group(1))


def themes():
    found = {
        name: declarations(body)
        for name, body in re.findall(r':root\[data-theme="([a-z0-9-]+)"\]\s*\{(.*?)\}', CSS, re.S)
    }
    assert found, "admin.css declares no :root[data-theme=…] blocks"
    return found


def luma(value):
    match = re.fullmatch(r"#([0-9a-fA-F]{6})", value.strip())
    assert match, f"a theme background must be a plain hex colour, got {value!r}"
    red, green, blue = (int(match.group(1)[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


# Colour and shape are independent axes: a palette says what things are made of,
# an object style says what shape they are. Keeping them apart is what makes
# "void palette, square corners" possible at all, so shape tokens carry their own
# prefix and no theme is allowed to set one.
#
# The prefix is the demo's (`--o-`, for object), so the object styles worked out
# there transfer without renaming anything later.
SHAPE_PREFIX = "--o-"


def derived_tokens():
    """Tokens whose value is built from another token.

    `--surface-sunken` is `--bg` at half strength, so it follows every palette
    without being written twelve times. A theme that redefined one would break
    exactly the derivation that makes it work.
    """

    return {
        name: value
        for name, value in base_tokens().items()
        if "var(--" in value and not name.startswith(SHAPE_PREFIX)
    }


def palette_tokens():
    return {
        name: value
        for name, value in base_tokens().items()
        if not name.startswith(SHAPE_PREFIX) and "var(--" not in value
    }


def shape_tokens():
    return {name: value for name, value in base_tokens().items() if name.startswith(SHAPE_PREFIX)}


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

# WCAG 2.1: 4.5:1 is the floor for normal text, 7:1 is the enhanced level. Body
# copy is held to the enhanced one because every palette already clears it by a
# wide margin; muted copy is held to the floor, which is what it is for.
TEXT_MIN_CONTRAST = 7.0
MUTED_MIN_CONTRAST = 4.5


def channels(value):
    match = re.fullmatch(r"#([0-9a-fA-F]{6})", value.strip())
    assert match, f"expected a plain hex colour, got {value!r}"
    return tuple(int(match.group(1)[index : index + 2], 16) for index in (0, 2, 4))


def relative_luminance(rgb):
    def linear(channel):
        ratio = channel / 255
        return ratio / 12.92 if ratio <= 0.03928 else ((ratio + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(foreground, background):
    lighter = max(relative_luminance(foreground), relative_luminance(background))
    darker = min(relative_luminance(foreground), relative_luminance(background))
    return (lighter + 0.05) / (darker + 0.05)


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
NEUTRAL = re.compile(r"rgba?\(\s*(?:255,\s*255,\s*255|0,\s*0,\s*0)\b|#000\b|#fff\b", re.I)
COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)")


def hued_literals():
    body = re.sub(r':root(\[data-theme="[a-z0-9-]+"\])?\s*\{.*?\}', "", CSS, flags=re.S)
    found = []
    for selector, block in re.findall(r"([^{}]+)\{([^{}]*)\}", body):
        for declaration in block.split(";"):
            if ":" not in declaration:
                continue
            _, _, value = declaration.partition(":")
            for literal in COLOUR.findall(value):
                if not NEUTRAL.search(literal):
                    found.append((" ".join(selector.split())[:44], literal.strip()))
    return found


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
