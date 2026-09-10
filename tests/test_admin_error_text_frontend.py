# SPDX-License-Identifier: AGPL-3.0-or-later
"""Error messages are written for the owner, not copied from the wire.

The client's habit was ``data.error || "a readable sentence"``. The server's
machine code therefore *won* and the readable sentence only appeared when the
server sent nothing at all — so a home owner met ``system_transition_in_progress``
or ``confirmation_required`` on screen, while the sentence the server had
written alongside the code was thrown away.

The resolver keeps the precedence ``authMessage`` always had: the server's own
sentence first, then a code we have words for, then a plain fallback.
"""

import json
import os
import shutil
import subprocess

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.contract,
    pytest.mark.simulation,
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "tests", "js", "error_text_runner.js")
STATIC_DIR = os.path.join(ROOT, "admin", "static")


def _read(name):
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def _text(payload, fallback="Something went wrong."):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, RUNNER],
        input=json.dumps({"payload": payload, "fallback": fallback}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["text"]


def test_the_servers_own_sentence_wins():
    assert (
        _text({"error": "system_build_mismatch", "message": "Pick the build again."})
        == "Pick the build again."
    )


def test_a_known_code_becomes_a_sentence_instead_of_the_code():
    text = _text({"error": "system_transition_in_progress"})
    assert text != "system_transition_in_progress"
    assert " " in text and text.endswith(".")


def test_an_unknown_code_falls_back_rather_than_printing_itself():
    assert _text({"error": "some_internal_code"}) == "Something went wrong."


def test_prose_sent_in_the_error_key_passes_through():
    """A third of the server's "error" values are already English sentences."""

    assert _text({"error": "unknown or expired restore plan"}) == (
        "unknown or expired restore plan"
    )


def test_the_reason_key_is_resolved_too():
    """LegacyMigrationError and the deployment permission path use "reason"."""

    assert _text({"reason": "target_exists", "message": "It is already there."}) == (
        "It is already there."
    )


def test_an_object_shaped_error_does_not_become_object_Object():
    """A job status carries {code, message}; the old code stringified it."""

    text = _text({"error": {"code": "pull_failed", "message": "The pull failed."}})
    assert text == "The pull failed."
    assert "[object" not in _text({"error": {"code": "pull_failed"}})


@pytest.mark.parametrize("payload", [None, {}, [], "boom", {"error": None}])
def test_a_payload_with_nothing_usable_gives_the_fallback(payload):
    assert _text(payload) == "Something went wrong."


def test_no_render_path_prints_a_bare_error_code_any_more():
    js = _read("admin.js")
    # The comment in the resolver quotes the old pattern; the code must not use it.
    body = "\n".join(
        line for line in js.splitlines() if not line.lstrip().startswith("//")
    )
    assert 'data.error || "' not in body
    assert 'job.error || "' not in body


def test_the_two_message_maps_do_not_overlap():
    """Two maps that answer for the same code are two sources of truth."""

    js = _read("admin.js")

    def keys(name):
        block = js.split("const " + name + " = {", 1)[1].split("\n};", 1)[0]
        return {
            line.split(":", 1)[0].strip()
            for line in block.splitlines()
            if line.strip() and ":" in line and not line.lstrip().startswith("//")
        }

    general = keys("ADMIN_ERROR_MESSAGES")
    setup = keys("SETUP_CONFLICT_MESSAGES")
    auth = keys("AUTH_ERROR_MESSAGES")
    assert general and setup and auth
    assert not general & setup
    assert not general & auth
