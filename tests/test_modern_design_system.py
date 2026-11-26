# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The design system both Modern front ends are built from (``frontend/shared/``).

The Modern dashboard and the Modern Admin console read one ``tokens.css``, so a
colour scheme is defined once and holds for both. These tests hold that file to
its own rules: every scheme defines every role, and every ink stays readable on
every ground it is written on.
"""

import re

import pytest

from tests.modern_frontend import ROOT, SHARED, TOKENS_CSS, root_block, themes

GROUNDS = ("--m-bg", "--m-surface-1", "--m-surface-2", "--m-surface-3")

pytestmark = [
    pytest.mark.contract,
]


def _scheme(css, scheme):
    tokens = root_block(css)
    if scheme != themes()[0]:
        tokens.update(root_block(css, f':root[data-m-theme="{scheme}"]'))
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
    css = TOKENS_CSS.read_text(encoding="utf-8")
    base = {key for key in root_block(css) if key.startswith("--m-") and not key.startswith(("--m-font", "--m-mono", "--m-radius"))}
    schemes = re.findall(r':root\[data-m-theme="([a-z]+)"\]', css)
    assert schemes == themes()[1:]
    for scheme in schemes:
        block = root_block(css, f':root[data-m-theme="{scheme}"]')
        themed = {key for key in base if key not in ("--m-ok", "--m-warn", "--m-fault", "--m-white", "--m-black")}
        assert themed <= set(block), f"{scheme} leaves roles to another scheme: {sorted(themed - set(block))}"


@pytest.mark.parametrize(
    ("ink", "grounds"),
    [
        ("--m-text", ("--m-bg", "--m-surface-1", "--m-surface-2", "--m-surface-3")),
        ("--m-muted", ("--m-bg", "--m-surface-1", "--m-surface-2", "--m-surface-3")),
        ("--m-muted-2", ("--m-bg", "--m-surface-1", "--m-surface-2", "--m-surface-3")),
        ("--soc-ink", ("--m-bg", "--m-surface-1", "--m-surface-2", "--m-surface-3")),
        ("--m-accent-ink", ("--m-accent",)),
        ("--m-selected-ink", ("--m-selected-bg",)),
        ("--m-text", ("--m-quiet-bg",)),
        ("--m-warn", ("--m-quiet-bg",)),
    ],
)
def test_every_scheme_keeps_its_text_readable(ink, grounds):
    """WCAG AA for normal text, 4.5:1, for every ink on every ground it is written on."""

    css = TOKENS_CSS.read_text(encoding="utf-8")
    for scheme in themes():
        tokens = _scheme(css, scheme)
        for ground in grounds:
            ratio = _contrast(tokens[ink], tokens[ground])
            assert ratio >= 4.5, f"{scheme}: {ink} on {ground} is {ratio:.2f}:1"


def _rules(css):
    """``[(selectors, declarations)]`` of the plain rules in ``css``; an at-rule's own block is left out, its rules are kept."""

    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules = []
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        declarations = {}
        for part in body.split(";"):
            name, _, value = part.partition(":")
            if value.strip():
                declarations[name.strip()] = value.strip()
        rules.append(([selector.strip() for selector in selectors.split(",")], declarations))
    return rules


def _token(value):
    match = re.fullmatch(r"var\((--[\w-]+)(?:,.*)?\)", value or "")
    return match.group(1) if match else None


def _last_compound(selector):
    """The compound a selector styles: ``.row .badge.x`` and ``:global(.badge.x)`` style ``.badge.x``."""

    selector = re.sub(r":global\(([^()]*)\)", r"\1", selector)
    return re.split(r"[\s>+~]+", selector.strip())[-1]


def _mix(colour, ground, weight):
    channels = [
        round(weight * int(colour[i : i + 2], 16) + (1 - weight) * int(ground[i : i + 2], 16))
        for i in (1, 3, 5)
    ]
    return "#" + "".join(f"{channel:02x}" for channel in channels)


def _badge_variants(markup):
    """Each static class list a ``badge`` element of ``markup`` carries beside ``badge``."""

    return {tuple(extra.split()) for extra in re.findall(r'class="badge((?: [\w-]+)*)', markup)}


def test_every_badge_keeps_its_text_readable_on_its_fill():
    """A badge variant that changes its ink stays readable in every scheme.

    The shared ``.badge`` writes ``--m-selected-ink`` on ``--m-selected-bg``. A
    variant that only swaps the ink keeps that fill, which in Midnight is the
    accent blue: the accent itself vanished on it and the warning amber fell
    to 2:1. A transparent variant is read on every surface.
    """

    base = _rules((SHARED / "styles" / "base.css").read_text(encoding="utf-8"))
    css = TOKENS_CSS.read_text(encoding="utf-8")
    schemes = {scheme: _scheme(css, scheme) for scheme in themes()}
    sources = sorted((ROOT / "frontend").glob("*/src/**/*.svelte")) + sorted((SHARED).glob("**/*.svelte"))
    failures = []
    checked = 0
    for source in sources:
        text = source.read_text(encoding="utf-8")
        styles = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", text, flags=re.S))
        own = _rules(styles)
        for selectors, declarations in own:
            if any(_last_compound(selector) == ".badge" for selector in selectors) and {"color", "background", "background-color"} & set(declarations):
                failures.append(f"{source.relative_to(ROOT)}: a bare .badge rule sets its colours and outranks every variant in base.css")
        rules = base + own
        for extras in _badge_variants(text):
            style = {}
            for classes in [("badge",)] + [(extra,) for extra in extras] + [("badge", extra) for extra in extras]:
                wanted = {"." + ".".join(classes), "." + ".".join(reversed(classes))}
                for selectors, declarations in rules:
                    if wanted & {_last_compound(selector) for selector in selectors}:
                        if "background-color" in declarations:
                            style.pop("background", None)
                        if "background" in declarations:
                            style.pop("background-color", None)
                        style.update(declarations)
            label = " ".join(("badge",) + extras)
            ink = _token(style.get("color"))
            fill = style.get("background", style.get("background-color"))
            grounds = GROUNDS if fill == "transparent" else (_token(fill),)
            if ink is None or None in grounds:
                failures.append(f"{source.relative_to(ROOT)} .{label}: ink {style.get('color')!r} on {fill!r} is no token pair this test can check")
                continue
            checked += 1
            for scheme, tokens in schemes.items():
                for ground in grounds:
                    if not (tokens.get(ink, "").startswith("#") and tokens.get(ground, "").startswith("#")):
                        failures.append(f"{source.relative_to(ROOT)} .{label}: {scheme} {ink} or {ground} is no hex colour")
                        continue
                    ratio = _contrast(tokens[ink], tokens[ground])
                    if ratio < 4.5:
                        failures.append(f"{source.relative_to(ROOT)} .{label}: {scheme} {ink} on {ground} is {ratio:.2f}:1")
    assert checked > 10
    assert not failures, "\n".join(sorted(set(failures)))


STATE_TONES = ("ok", "warn", "fault", "acc", "idle")


def test_every_state_pill_keeps_its_text_readable_on_its_tint():
    """A ``.status`` pill writes its tone on 12 % of that tone over a card; the accent and idle tones bring their own ink.

    Midnight's accent blue on its own tint was 3.5:1. The energy tones mark,
    they carry no text (docs/developer/dashboard-modern-style-guide.md).
    """

    base = _rules((SHARED / "styles" / "base.css").read_text(encoding="utf-8"))
    css = TOKENS_CSS.read_text(encoding="utf-8")
    styles = {}
    for selectors, declarations in base:
        for selector in selectors:
            styles.setdefault(selector, {}).update(declarations)
    status = styles[".status"]
    assert "12%" in status["background"]
    failures = []
    for tone in STATE_TONES:
        colour = _token(styles[f".c-{tone}"]["--c"])
        ink = _token(styles.get(f".status.c-{tone}", {}).get("color", f"var({colour})"))
        for scheme in themes():
            tokens = _scheme(css, scheme)
            for ground in ("--m-surface-1", "--m-surface-2"):
                ratio = _contrast(tokens[ink], _mix(tokens[colour], tokens[ground], 0.12))
                if ratio < 4.5:
                    failures.append(f".status.c-{tone}: {scheme} {ink} on its tint over {ground} is {ratio:.2f}:1")
    assert not failures, "\n".join(failures)
