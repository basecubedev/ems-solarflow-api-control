# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Appliance Manager answers to the same two axes as the Admin Console.

The Manager is its own deployable with its own stylesheet -- it cannot link
Admin's -- so the palettes are a second copy of the same table. Copies drift,
which is why `test_shared_design_tokens.py` compares them across surfaces; this
module asks the questions that are local to this one stylesheet: does every
palette redefine the whole set, does any rule still carry a hue of its own, and
does every corner read a role rather than a number somebody once typed.

The Manager is the smaller surface and the more exposed one: it is what an
owner sees when the EMS is down and the host needs attention. A palette that
renders its warnings unreadable is worse here than anywhere else, which is what
the contrast contract is for.
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

pytestmark = [pytest.mark.contract, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "appliance" / "static"
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
EARLY = STATIC / "theme.js"

DEFAULT_THEME = "signal"
STORAGE_KEY = "ems-appliance-theme"


def base_tokens():
    return measure.base_tokens(CSS)


def themes():
    found = measure.themes(CSS)
    assert found, "styles.css declares no :root[data-theme=…] blocks"
    return found


def palette_tokens():
    return measure.palette_tokens(CSS)


def shape_tokens():
    return measure.shape_tokens(CSS)


def derived_tokens():
    return measure.derived_tokens(CSS)


def test_every_theme_redefines_the_whole_token_set():
    """A token a palette leaves out keeps its `:root` value, which shows the
    previous theme's colour on the new ground and reads as slightly wrong
    rather than as a fault."""

    expected = set(palette_tokens())
    incomplete = {name: sorted(expected - set(tokens)) for name, tokens in themes().items()}
    incomplete = {name: missing for name, missing in incomplete.items() if missing}
    assert incomplete == {}, f"themes that would inherit a token from :root: {incomplete}"


def test_no_theme_invents_a_token_the_manager_never_reads():
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
    """An appliance console is read when something is already wrong."""

    failures = {}
    for name, tokens in themes().items():
        background = channels(tokens["--bg"])
        text = contrast(channels(tokens["--text"]), background)
        muted = contrast(channels(tokens["--muted"]), background)
        if text < TEXT_MIN_CONTRAST or muted < MUTED_MIN_CONTRAST:
            failures[name] = {"text": round(text, 1), "muted": round(muted, 1)}
    assert failures == {}, f"themes that are hard to read: {failures}"


def test_a_theme_never_redefines_a_derived_token():
    trespassing = {
        name: sorted(set(tokens) & set(derived_tokens())) for name, tokens in themes().items()
    }
    trespassing = {name: found for name, found in trespassing.items() if found}
    assert trespassing == {}, f"themes overriding a derived token: {trespassing}"


# --- the switcher ----------------------------------------------------------


def offered():
    block = re.search(r"var THEMES = \[(.*?)\];", JS, re.S)
    assert block, "app.js declares no THEMES table"
    return dict(re.findall(r'id:\s*"([a-z0-9-]+)",\s*label:\s*"([^"]+)"', block.group(1)))


def test_the_switcher_offers_exactly_the_themes_the_stylesheet_has():
    styled, listed = set(themes()), set(offered())
    assert styled == listed, (
        f"only in the stylesheet: {sorted(styled - listed)}; "
        f"only in the menu: {sorted(listed - styled)}"
    )


def test_the_stored_theme_is_applied_before_the_first_paint():
    """The Manager's CSP is `script-src 'self'` like Admin's, so this cannot be
    three lines inline in <head>; it is its own file, requested before the
    stylesheet it affects."""

    assert EARLY.exists(), "appliance/static/theme.js is missing"
    head = HTML.split("</head>", 1)[0]
    assert "theme.js" in head, "the early theme script is not in <head>"
    assert head.index("theme.js") < head.index("styles.css"), (
        "the theme script must run before the stylesheet it affects"
    )


def test_the_early_script_is_served_at_all():
    """`STATIC_FILES` is an allowlist: a file in the directory that is not in it
    is a 404, and the console would paint the default theme every time."""

    web = (ROOT / "appliance" / "web.py").read_text(encoding="utf-8")
    allowlist = re.search(r"STATIC_FILES = \{(.*?)\}", web, re.S)
    assert allowlist, "appliance/web.py declares no STATIC_FILES allowlist"
    assert '"theme.js"' in allowlist.group(1), "theme.js is not in the static allowlist"


def test_both_halves_agree_on_where_the_choice_is_stored():
    early = EARLY.read_text(encoding="utf-8")
    assert STORAGE_KEY in early, f"the early script does not read {STORAGE_KEY!r}"
    assert STORAGE_KEY in JS, f"app.js does not write {STORAGE_KEY!r}"


def test_the_manager_keeps_its_own_stored_choice():
    """Admin and the Manager are different origins, so one localStorage cannot
    serve both. Sharing the key name would only suggest otherwise."""

    assert STORAGE_KEY != "ems-admin-theme"


def test_an_unreadable_store_does_not_break_the_page():
    early = EARLY.read_text(encoding="utf-8")
    assert "try" in early and "catch" in early, "the early script does not guard localStorage"


# --- the shape axis --------------------------------------------------------

# Two corners are not roles: both circles are 7px status dots, and a circle is a
# geometric necessity rather than a shape choice an object style may argue with.
LITERAL_CORNERS = {"50%"}


def test_a_theme_never_touches_the_shape_axis():
    trespassing = {
        name: sorted(token for token in tokens if token.startswith(SHAPE_PREFIX))
        for name, tokens in themes().items()
    }
    trespassing = {name: found for name, found in trespassing.items() if found}
    assert trespassing == {}, f"themes setting shape tokens: {trespassing}"


def test_every_corner_reads_a_role():
    """The Manager's corners drifted the same way Admin's did, and against
    Admin as well: its controls sat at 8px where Admin's sit at 10. One
    vocabulary is only one vocabulary if the values follow it."""

    strays = [
        (selector, value)
        for selector, value in measure.corners(CSS)
        if not value.startswith("var(--o-") and value not in LITERAL_CORNERS
    ]
    assert strays == [], f"{len(strays)} rules still choose their own corner: {strays[:8]}"


def test_no_pill_carries_its_own_radius():
    literal = re.findall(r"border-radius:\s*999px", measure.body(CSS))
    assert literal == [], f"{len(literal)} rules still round themselves to 999px"


def test_badges_of_the_same_size_share_one_padding():
    """Height follows font size, so the small labels are one group."""

    paddings = set()
    for _, block in measure.rules(CSS):
        size = re.search(r"(?<![a-z-])font-size:\s*(\d+)px", block)
        rounded = "--o-pill-radius" in block or "999px" in block
        if not rounded or not size or int(size.group(1)) > 10:
            continue
        padding = re.search(r"(?<![a-z-])padding:\s*([^;]+);", block)
        if padding:
            paddings.add(" ".join(padding.group(1).split()))
    assert len(paddings) <= 1, f"small badges still use several paddings: {sorted(paddings)}"


def test_pill_shaped_controls_share_one_padding():
    """A pill you can click is a third thing next to the badge and the status
    pill: bigger, because it has to be hit. `cursor: pointer` is what tells
    them apart from a label."""

    paddings = set()
    for _, block in measure.rules(CSS):
        if "--o-pill-radius" not in block or "cursor: pointer" not in block:
            continue
        padding = re.search(r"(?<![a-z-])padding:\s*([^;]+);", block)
        if padding:
            paddings.add(" ".join(padding.group(1).split()))
    assert len(paddings) <= 1, f"pill-shaped controls still differ: {sorted(paddings)}"


# --- reach -----------------------------------------------------------------


def test_no_rule_mixes_its_own_colour():
    """A theme reaches what reads tokens, and nothing else.

    Seventeen rules carried a hue of their own -- the accent tint behind the
    primary button, the three border colours of the status pills, the near-black
    ground of the log view. Each was mixed against the one background that
    existed at the time; on a warm palette it is simply the wrong colour sitting
    there, and nothing fails, it just quietly is not themed.
    """

    remaining = measure.hued_literals(CSS)
    assert remaining == [], (
        f"{len(remaining)} rules still carry their own hue: {sorted(set(remaining))[:8]}"
    )
