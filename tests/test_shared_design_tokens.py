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
