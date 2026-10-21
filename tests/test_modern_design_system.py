# SPDX-License-Identifier: AGPL-3.0-or-later
"""The design system both Modern front ends are built from (``frontend/shared/``).

The Modern dashboard and the Modern Admin console read one ``tokens.css``, so a
colour scheme is defined once and holds for both. These tests hold that file to
its own rules: every scheme defines every role, and every ink stays readable on
every ground it is written on.
"""

import re

import pytest

from tests.modern_frontend import TOKENS_CSS, root_block, themes

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
        ("--m-accent-ink", ("--m-accent",)),
        ("--m-selected-ink", ("--m-selected-bg",)),
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
