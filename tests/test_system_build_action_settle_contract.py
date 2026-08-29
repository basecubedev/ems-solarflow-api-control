# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static contract: the browser gate waits on the server's answer, not on prose.

``expectValidSystemBuildAction`` is the shared assertion behind every System
Build browser test. Before asserting which action a build authorizes it has to
know the action has settled, and for a long time the only thing it waited on was
the status line not matching ``/checking|confirming|updating|reconnecting/i``.

Those words belong to admin.js, which renders them while a request of its own is
in flight. The *server* has its own busy stages, and none of their progress
messages contains any of those words:

    admin_update_pending      "Preparing the Admin Server update…"
    admin_reconnect_pending   "Waiting for the updated Admin Server to reconnect…"
    admin_aligned             "Verifying selected System Build resources…"

So the guard passed while a transition was still running. The helper then read a
validation snapshot that said ``busy: true`` and compared its progress message
against a status line that had already moved on to the settled text --
"The Admin Server is ready for the selected System Build." Firefox lost that race
on main twice on 2026-08-28 (`f504b246` and `00e50f7d`); Chromium landed after
the settle and passed, which is why it read as a merge breaking something.

The fix is to wait on ``action_state.busy``, which the server derives from
``_ACTION_BUSY_STAGES``. This module keeps that true, and keeps the message table
complete so the helper's "did not settle" diagnostic can still name the stage it
was stuck in.
"""

import re

import pytest

from pathlib import Path

pytestmark = [
    pytest.mark.admin,
    pytest.mark.system_build,
    pytest.mark.contract,
]

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tests" / "e2e" / "helpers" / "system-build-action.ts"
ALIGNMENT = ROOT / "admin" / "system_alignment.py"


def helper():
    return HELPER.read_text(encoding="utf-8")


def busy_stages():
    """The stages the server reports as busy, read from the module that owns them."""

    from admin.system_alignment import _ACTION_BUSY_STAGES

    return set(_ACTION_BUSY_STAGES)


def progress_messages():
    from admin.system_alignment import _ACTION_PROGRESS_MESSAGES

    return dict(_ACTION_PROGRESS_MESSAGES)


def test_the_helper_waits_for_the_server_to_report_it_is_no_longer_busy():
    """The one control. Everything else here explains why it is needed."""

    text = helper()

    assert "state.busy === true" in text and "expect\n      .poll(" in text, (
        "expectValidSystemBuildAction no longer polls action_state.busy. Without "
        "it the helper asserts against whatever frame it happens to catch, and "
        "the failure surfaces as a status-text mismatch in whichever browser is "
        "slowest that day"
    )


def test_the_wait_comes_before_the_snapshot_it_protects():
    """Order is the property, not presence: a poll after ``latestValidation()``
    would leave the snapshot as racy as it was."""

    text = helper()
    poll = text.index("state.busy === true")
    snapshot = text.index("const validation = await setup.latestValidation();")

    assert poll < snapshot, (
        "the busy wait must run before the validation snapshot the assertions read"
    )


def test_no_busy_stage_message_is_matched_by_the_client_side_guard():
    """The defect itself, stated so it cannot come back as a false sense of cover.

    If someone ever writes a progress message containing one of these words, the
    text guard would appear to work for that stage and not for its siblings --
    which is exactly the half-covered state that made this flake so hard to read.
    """

    guard = re.search(r"/([a-z|]+)/i", helper())
    assert guard, "the client-side transient guard is gone or changed shape"
    words = re.compile(guard.group(1), re.IGNORECASE)

    covered = {
        stage: message
        for stage, message in progress_messages().items()
        if words.search(message)
    }

    assert covered == {}, (
        f"{covered} would now be matched by the client-side guard. That guard is "
        "for admin.js transients; the server's busy stages are waited on through "
        "action_state.busy, and a message that matches both invites the next "
        "reader to think the text guard is what covers them"
    )


def test_every_busy_stage_can_name_itself_when_it_fails_to_settle():
    """The helper's diagnostic branch asserts ``progress_message`` is truthy, so a
    busy stage without one turns a useful timeout into an empty assertion."""

    missing = sorted(busy_stages() - set(progress_messages()))

    assert missing == [], (
        f"{missing} are reported busy with no progress message; the browser "
        "gate's 'did not settle' failure would then name nothing"
    )


def test_the_dom_is_given_the_same_chance_to_catch_up():
    """A validation response is recorded by a listener that can run before the
    page renders it. The button reads after the snapshot are single shots, so the
    helper waits for the status line to stop showing a progress message too."""

    text = helper()

    assert "validationHistory()" in text and "progressMessages" in text, (
        "the helper no longer waits for the rendered status to leave its busy "
        "message; the button assertions can then read the busy frame"
    )
