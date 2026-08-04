# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guided Upgrade omits no-op MQTT migration detail from plan and confirmation."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.system_build,
    pytest.mark.contract,
    pytest.mark.simulation,
]

ROOT = Path(__file__).resolve().parents[1]
ADMIN_JS = ROOT / "admin" / "static" / "admin.js"


def _extract_function(source, name):
    marker = f"function {name}"
    start = source.find(marker)
    assert start >= 0, f"{name} is missing from admin.js"
    brace = source.find(") {", start) + 2
    assert brace >= 2, f"function body for {name} is missing"
    depth = 0
    quote = None
    escaped = False
    for index in range(brace, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated function {name}")


def _summarize(review):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for upgrade-confirmation frontend tests")
    source = ADMIN_JS.read_text(encoding="utf-8")
    fn = _extract_function(source, "summarizeMqttMigration")
    setup = (
        "console.log(JSON.stringify(summarizeMqttMigration("
        + json.dumps(review)
        + ")));"
    )
    result = subprocess.run(
        [node, "-e", fn + "\n" + setup],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _change(disables_control=False):
    return {"serial": "S", "disables_control": disables_control}


def test_no_migration_empty_changes_is_not_relevant():
    result = _summarize({"needs_migration": False, "changes": []})
    assert result["relevant"] is False
    assert result["text"] == ""
    assert result["affected"] == 0
    assert result["losingControl"] == 0


def test_missing_migration_fields_are_not_relevant_without_error():
    for review in ({}, None, {"needs_migration": False}, {"changes": []}):
        result = _summarize(review)
        assert result["relevant"] is False
        assert result["text"] == ""


def test_two_affected_without_control_loss_shows_no_lose_control():
    result = _summarize(
        {"needs_migration": True, "changes": [_change(), _change()]}
    )
    assert result["relevant"] is True
    assert result["text"] == "MQTT configuration migration required for 2 devices."
    assert "lose" not in result["text"]


def test_control_loss_is_shown_only_when_a_device_is_affected():
    result = _summarize(
        {
            "needs_migration": True,
            "changes": [_change(disables_control=True), _change()],
        }
    )
    assert result["relevant"] is True
    assert result["text"] == (
        "MQTT configuration migration required for 2 devices; "
        "1 device will lose output control."
    )


def test_inconsistent_false_flag_still_shows_real_changes():
    result = _summarize({"needs_migration": False, "changes": [_change()]})
    assert result["relevant"] is True
    assert result["text"] == "MQTT configuration migration required for 1 device."


@pytest.mark.parametrize(
    "review",
    [
        {"needs_migration": False, "changes": []},
        {},
        None,
        {"needs_migration": True, "changes": [_change(), _change()]},
        {"needs_migration": True, "changes": [_change(disables_control=True)]},
        {"needs_migration": True, "changes": []},
    ],
)
def test_zero_counts_are_never_rendered(review):
    text = _summarize(review)["text"]
    assert "0 affected" not in text
    assert "0 lose control" not in text
    assert "0 device" not in text
