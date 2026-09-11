# SPDX-License-Identifier: AGPL-3.0-or-later
"""A page never uses one name for two different things.

The Network page stacked a card called "Hostname" (what the hostname is) on a
card called "Hostname" (where you change it), and SSH & Backup Access did the
same with "SSH service". Reading down the page, the second heading looks like a
repeat of the first until you notice one has buttons in it.

The two kinds of card answer different questions -- a status card says what is
true, an action card says what you can do -- so they must not answer with the
same words.
"""

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "appliance" / "static" / "app.js").read_text(encoding="utf-8")

# A view is what an operator sees at once, which is not always one function:
# System Updates renders the manager block and the OS block into one page, and
# the Admin page swaps in the bootstrap branch when nothing is installed.
VIEWS = {
    "Overview": ["renderOverview"],
    "EMS Admin": ["renderAdmin"],
    "EMS Admin (nothing installed)": ["renderAdminBootstrap"],
    "System updates": ["renderManagerUpdates", "renderPackageUpdates"],
    "Network": ["renderNetwork"],
    "SSH & backup access": ["renderAccess"],
    "Diagnostics": ["renderDiagnostics"],
    "Settings": ["renderSettings"],
}


def body(name):
    marker = f"function {name}("
    start = APP.index(marker)
    depth = 0
    for index in range(APP.index("{", start), len(APP)):
        if APP[index] == "{":
            depth += 1
        elif APP[index] == "}":
            depth -= 1
            if depth == 0:
                return APP[start : index + 1]
    raise AssertionError(f"{name} has no closing brace")


def titles(names, call):
    found = []
    for name in names:
        found += re.findall(rf'{call}\(\s*\n?\s*"([^"]+)"', body(name))
    return found


def test_no_view_gives_a_status_card_and_an_action_card_the_same_name():
    clashes = {}
    for view, names in VIEWS.items():
        shared = set(titles(names, "card")) & set(titles(names, "actionCard"))
        if shared:
            clashes[view] = sorted(shared)

    assert clashes == {}, f"one name for two different things: {clashes}"


def test_the_admin_page_does_not_offer_install_version_next_to_installed_version():
    """Two headings one letter apart, one a reading and one a control."""

    admin = body("renderAdmin")
    assert '"Installed version"' in admin, "the status card was renamed; revisit this pairing"
    assert '"Install version"' not in admin
