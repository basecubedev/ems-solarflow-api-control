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

import re
from pathlib import Path

import pytest

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
    shared = set.intersection(*(set(tokens) for tokens in declared.values()))
    drift = {
        name: {surface: declared[surface][name] for surface in SURFACES}
        for name in sorted(shared)
        if len({declared[surface][name] for surface in SURFACES}) > 1
    }
    assert drift == {}, f"shared tokens with more than one value: {drift}"
