# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the two-second poll is allowed to do to the page.

The manager polls so an operation's progress stays live. It rebuilt the whole
view each time, which destroyed any field the operator was typing into and the
focus with it -- a WLAN passphrase or a release tag could not be entered on a
slow connection at all. Progress must still update; the view must not be torn
out from under the person using it.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_appliance_manager_frontend import node, requires_node

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


def test_a_rebuild_draws_a_waiting_control_as_waiting():
    """The poll rebuilds the view while a request is out; a rebuilt control
    that came back enabled could send the request a second time."""

    assert "reapplyHolds()" in body("render")
    assert "reapplyHolds()" in body("refreshBanner")


@pytest.mark.parametrize(
    "sender",
    ["planOperation", "confirmDialog", "acknowledgeOperation", "cancelOperation", "submitGate", "logout"],
)
def test_every_request_a_press_sends_holds_its_control(sender):
    assert "holdControl(" in body(sender), sender


def test_cancel_and_escape_wait_while_a_confirmation_is_out():
    """Cancelling the plan whose confirmation is on its way would withdraw the
    operation the appliance is starting."""

    assert "dialog-cancel" in body("confirmDialog")
    assert 'isHeld(document.getElementById("dialog-cancel"))' in body("dismissDialog")


def _load_into_with_focus_in(tag):
    script = (
        "var calls = [];\n"
        "var state = { viewGeneration: 1, listingGenerations: {}, data: {} };\n"
        "function render() { calls.push('render'); }\n"
        "function refreshBanner() { calls.push('banner'); }\n"
        f"var focused = {{ tagName: {json.dumps(tag)} }};\n"
        "var main = { contains: function (candidate) { return candidate === focused; } };\n"
        "var document = { activeElement: focused, getElementById: function () { return main; } };\n"
        "function api() { return Promise.resolve({ appliance_version: '0.3.13' }); }\n"
        + "\n".join(body(name) for name in ("isEditing", "renderPolled", "listingGeneration", "loadInto"))
        + "\nloadInto('settings', '/api/settings').then(function () {\n"
        "  console.log(JSON.stringify({ calls: calls, settings: state.data.settings }));\n"
        "});\n"
    )
    result = subprocess.run([node, "-"], input=script, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@requires_node
def test_a_view_read_arriving_while_a_field_is_typed_in_leaves_the_field_alone():
    """The Settings read rebuilt the page under a password being typed.

    A view fetches its data the first time it is drawn and rebuilt the whole
    page when the answer came. On a slow appliance the answer arrived after the
    operator had started typing, and a password field -- which is never kept
    across a rebuild -- came back empty: the change was sent without the
    current password and refused. The read now waits for the field like the
    poll does; only the banner is refreshed.
    """

    result = _load_into_with_focus_in("INPUT")

    assert result["calls"] == ["banner"]
    assert result["settings"] == {"appliance_version": "0.3.13"}


@requires_node
def test_a_view_read_still_draws_the_page_when_no_field_is_in_use():
    result = _load_into_with_focus_in("BUTTON")

    assert result["calls"] == ["render"]
    assert result["settings"] == {"appliance_version": "0.3.13"}


@requires_node
def test_a_rescue_account_without_a_password_is_said_to_have_none():
    """An emptied password field came out as "unknown".

    The appliance reports it as ``password_set`` false with
    ``password_is_default`` None -- there is no shipped password to compare --
    and the check for an unread comparison ran first, so the state the console
    has words for could never show.
    """
    from appliance.rescue_account import RescueState

    states = [
        RescueState(present=True, password_set=False, shell="/bin/bash", uid=1001).to_dict(),
        RescueState(present=True, unreadable="shadow unreadable").to_dict(),
        RescueState(present=True, password_set=True, password_is_default=None, shell="/bin/bash", uid=1001).to_dict(),
    ]
    script = body("rescueState") + f"\nconsole.log(JSON.stringify({json.dumps(states)}.map(function (rescue) {{ return rescueState(rescue).label; }})));\n"
    result = subprocess.run([node, "-"], input=script, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == ["no password", "unknown", "unknown"]
