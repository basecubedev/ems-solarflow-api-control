# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every API path a Classic front end calls is called by Modern or listed.

The Modern Admin console once listed the release versions and could not
install one, and no test noticed. docs/developer/modern-parity.md lists what
each front end can do; this holds its registry of Classic-only API paths to
the code in both directions: a new Classic request Modern does not make fails
until Modern makes it or the registry names it, and a registry entry Modern
has since taken over fails until it is removed.
"""

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.admin]

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "developer" / "modern-parity.md"
API_PATH = re.compile(r"[\"'`](/api/[A-Za-z0-9_\-/.?=&]*)")

PRODUCTS = {
    "admin": (
        ["admin/static/admin.js"],
        ["frontend/admin-modern/src", "frontend/shared"],
    ),
    "dashboard": (
        ["dashboard/static/app.js", "dashboard/static/index.html"],
        ["frontend/dashboard-modern/src", "frontend/shared"],
    ),
    "appliance": (
        ["appliance/static/app.js"],
        ["frontend/appliance-modern/src", "frontend/shared"],
    ),
}


def _classic_paths(files):
    paths = set()
    for name in files:
        paths |= set(API_PATH.findall((ROOT / name).read_text(encoding="utf-8")))
    return paths


def _modern_text(roots):
    return "".join(
        path.read_text(encoding="utf-8")
        for root in roots
        for path in sorted((ROOT / root).rglob("*"))
        if path.suffix in {".js", ".svelte"}
    )


def _registry(product):
    match = re.search(
        rf"```text classic-only {product}\n(.*?)```", DOC.read_text(encoding="utf-8"), re.S
    )
    if match is None:
        return set()
    return {line.strip() for line in match.group(1).splitlines() if line.strip()}


@pytest.mark.parametrize("product", sorted(PRODUCTS))
def test_every_classic_api_path_is_called_by_modern_or_listed(product):
    classic, modern = PRODUCTS[product]
    text = _modern_text(modern)
    classic_only = {
        path for path in _classic_paths(classic) if path.split("?")[0] not in text
    }
    registry = _registry(product)
    assert sorted(classic_only - registry) == [], (
        "Classic calls these and Modern does not: build them in Modern, or list "
        "them in docs/developer/modern-parity.md"
    )
    assert sorted(registry - classic_only) == [], (
        "Modern calls these now, or Classic no longer does: remove them from the "
        "registry in docs/developer/modern-parity.md"
    )


def test_the_registry_is_found_for_the_console_that_has_gaps():
    assert _registry("admin"), "the admin registry block is missing from the parity page"
