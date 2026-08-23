# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the two-second poll is allowed to do to the page.

The manager polls so an operation's progress stays live. It rebuilt the whole
view each time, which destroyed any field the operator was typing into and the
focus with it -- a WLAN passphrase or a release tag could not be entered on a
slow connection at all. Progress must still update; the view must not be torn
out from under the person using it.
"""

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "appliance" / "static" / "app.js").read_text(encoding="utf-8")


def body(name):
    start = APP.index(f"function {name}(")
    depth = 0
    for index in range(start, len(APP)):
        if APP[index] == "{":
            depth += 1
        elif APP[index] == "}":
            depth -= 1
            if depth == 0:
                return APP[start : index + 1]
    raise AssertionError(f"{name} has no closing brace")


def test_the_poll_does_not_rebuild_the_view_unconditionally():
    """startPolling used to call the full render on every tick."""

    polling = body("startPolling")

    assert "renderPolled" in polling
    assert re.search(r"\brender\(\)", polling) is None


def test_the_poll_leaves_a_field_alone_while_it_has_focus():
    polled = body("renderPolled")

    assert "isEditing()" in polled


def test_editing_is_decided_by_the_focused_element_not_by_a_flag():
    """A flag would have to be cleared by every code path that ever set it."""

    editing = body("isEditing")

    assert "document.activeElement" in editing
    assert re.search(r"input|select|textarea", editing, re.I)


def test_progress_still_updates_while_a_field_has_focus():
    """Otherwise a long operation looks frozen whenever a field is focused."""

    polled = body("renderPolled")

    assert "refreshBanner()" in polled


def test_the_banner_is_replaced_in_place_rather_than_by_a_full_render():
    refresh = body("refreshBanner")

    assert "operation-banner" in refresh
    assert "replaceChild" in refresh


def test_cancel_is_only_offered_where_it_is_a_legal_transition():
    """RUNNING -> CANCELLED is not a transition the store allows, so the button
    produced an alert carrying an internal state name instead of cancelling."""

    assert "CANCELLABLE_STATES.indexOf(operation.state)" in APP
    assert '"operation-uninterruptible"' in APP


def test_a_failed_status_call_does_not_report_the_appliance_healthy():
    """Every card renders as an em dash when /api/status failed; saying the
    appliance looks healthy underneath them is the opposite of the truth."""

    block = APP.split('main.appendChild(el("h2", { class: "section-title", text: "Warnings" }))')[1]
    healthy = block.index("looks healthy")
    guard = block.index("status.error")

    assert guard < healthy, "the healthy empty-state is not guarded by the error check"
