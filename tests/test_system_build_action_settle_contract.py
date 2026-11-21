# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static contract: the browser gate waits on the server's answer, not on prose.

``expectValidSystemBuildAction`` is the shared assertion behind the System
Build browser journeys. Before asserting which action a build authorizes it has
to know the action has settled. Two things can still be moving: the console's
own request (its status line names it, ``buildMessage`` in
``frontend/admin-modern/src/lib/setup.js``) and the server's transition, whose
busy stages the console never shows in words. The helper waits for the first by
the console's own texts and for the second on ``action_state.busy``, which the
server derives from ``_ACTION_BUSY_STAGES``. This module keeps both waits, their
order, and the list of the console's transient messages true.
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


def _console_messages():
    """The status keys ``buildMessage`` returns, and the English text of each."""

    source = (ROOT / "frontend" / "admin-modern" / "src" / "lib" / "setup.js").read_text(encoding="utf-8")
    body = source[source.index("export function buildMessage("):]
    body = body[: body.index("\n}\n")]
    keys = set(re.findall(r'return "([a-zA-Z]+)"', body)) | set(re.findall(r'"([a-zA-Z]+)" : "', body)) | set(re.findall(r': "([a-zA-Z]+)";', body))
    strings = (ROOT / "frontend" / "admin-modern" / "src" / "i18n" / "strings.js").read_text(encoding="utf-8")
    texts = dict(re.findall(r'"setup\.build\.([a-zA-Z]+)": s\("([^"]*)"', strings))
    return keys, texts


def test_the_guard_names_only_the_console_s_own_requests_under_way():
    """The console's transient messages, and none that says where a build stands.

    A settled message in the list would make the helper wait for a state that
    stays; a transient one missing from it would let the assertions read a
    frame the console is still changing.
    """

    listed = re.search(r"CONSOLE_TRANSIENT_MESSAGES = \[([^\]]*)\]", helper())
    assert listed, "the helper no longer names the console's transient messages"
    transient = set(re.findall(r'"([a-zA-Z]+)"', listed.group(1)))
    keys, texts = _console_messages()
    assert transient <= keys, sorted(transient - keys)
    assert transient <= set(texts), sorted(transient - set(texts))
    assert transient == {"validating", "confirming", "updating", "reconnecting", "busy", "inProgress"}
    settled = keys - transient
    assert {"aligned", "updateRequired", "terminal", "legacyReady"} <= settled
    shared = {key for key in transient if any(texts[key] == texts[other] for other in settled if other in texts)}
    assert shared == set(), shared


def test_every_busy_stage_can_name_itself_when_it_fails_to_settle():
    """The helper's diagnostic branch asserts ``progress_message`` is truthy, so a
    busy stage without one turns a useful timeout into an empty assertion."""

    missing = sorted(busy_stages() - set(progress_messages()))

    assert missing == [], (
        f"{missing} are reported busy with no progress message; the browser "
        "gate's 'did not settle' failure would then name nothing"
    )
