# SPDX-License-Identifier: AGPL-3.0-or-later
"""A config reload never discards settings the operator has not saved.

The Maintenance settings editor is a browser-held draft: nothing reaches
config.json until it is reviewed and applied. Every reload of the panel used to
replace that draft unconditionally, so "Refresh" silently destroyed work that
existed nowhere else. These contracts pin the decision the shipped code makes,
including the direction it fails in when the answer cannot be computed.
"""

import json
import os
import shutil
import subprocess

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.contract,
    pytest.mark.simulation,
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "tests", "js", "maintenance_draft_guard_runner.js")
ADMIN_JS = os.path.join(ROOT, "admin", "static", "admin.js")


def _keep(state, options=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, RUNNER],
        input=json.dumps({"state": state, "options": options}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["keep"]


def _loaded(draft, pristine):
    return {"loaded": True, "draft": draft, "pristine": pristine}


def test_an_edited_draft_survives_a_reload():
    assert _keep(_loaded({"a": 2}, {"a": 1})) is True


def test_an_untouched_draft_is_replaced_by_the_saved_settings():
    assert _keep(_loaded({"a": 1}, {"a": 1})) is False


def test_an_explicit_discard_replaces_even_an_edited_draft():
    """Applying writes the draft; the reload after it loads the new saved state."""

    assert _keep(_loaded({"a": 2}, {"a": 1}), {"discardDraft": True}) is False


def test_the_first_load_has_no_draft_to_keep():
    assert _keep({"loaded": False, "draft": None, "pristine": None}) is False
    assert _keep(None) is False


@pytest.mark.parametrize(
    "state",
    [
        {"loaded": True, "draft": {"a": 1}, "pristine": None},
        {"loaded": True, "draft": None, "pristine": {"a": 1}},
    ],
)
def test_a_half_loaded_editor_has_nothing_to_protect(state):
    assert _keep(state) is False


@pytest.mark.parametrize("key", ["draft", "pristine"])
def test_a_draft_that_cannot_be_compared_counts_as_unsaved(key):
    """Fail closed: an unreadable answer must not authorise throwing work away."""

    state = _loaded({"a": 1}, {"a": 1})
    state[key] = "__circular__"
    assert _keep(state) is True


def test_the_reload_path_asks_before_it_replaces_the_draft():
    with open(ADMIN_JS, encoding="utf-8") as handle:
        source = handle.read()
    body = source.split("function renderMaintenanceConfig(", 1)[1]
    assert body, "renderMaintenanceConfig is missing"
    guard = body.index("mconfigShouldKeepDraft(mconfigState, options)")
    replacement = body.index("mconfigState.draft = mconfigClone(data.draft")
    assert guard < replacement
