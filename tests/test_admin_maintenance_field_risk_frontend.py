# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings rows carry the risk the catalog already ships.

``ems.config_catalog`` classifies every field's level and risk and serializes
both to the browser, where nothing rendered them: an owner could not see before
editing whether a value destabilizes the control loop, discards stored history
or is a secret. These contracts pin the markers to the shipped renderer.
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
RUNNER = os.path.join(ROOT, "tests", "js", "maintenance_field_row_runner.js")


def _field(**overrides):
    field = {
        "path": "system.loop_interval",
        "label": "Loop interval",
        "description": "Time between two EMS control cycles.",
        "type": "integer",
        "level": "normal",
        "risk": "restart_required",
    }
    field.update(overrides)
    return field


def _row(field, value=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, RUNNER],
        input=json.dumps({"field": field, "value": value}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_row_carries_its_catalog_classification():
    row = _row(_field(level="expert", risk="control_stability"))
    assert row["dataset"]["path"] == "system.loop_interval"
    assert row["dataset"]["level"] == "expert"
    assert row["dataset"]["risk"] == "control_stability"


def test_a_field_without_a_level_still_says_which_path_it_writes():
    row = _row({"path": "system.enabled", "label": "EMS control enabled"})
    assert row["dataset"]["path"] == "system.enabled"
    assert "level" not in row["dataset"]
    assert row["badges"] == []


@pytest.mark.parametrize(
    "risk, text",
    [
        ("control_stability", "affects control stability"),
        ("data_loss", "can discard stored data"),
        ("secret", "secret"),
        ("deprecated", "deprecated"),
    ],
)
def test_a_consequential_risk_is_named_before_the_edit(risk, text):
    badges = _row(_field(risk=risk))["badges"]
    assert [badge["text"] for badge in badges] == [text]
    assert badges[0]["title"]


def test_the_ordinary_restart_risk_is_not_badged_as_a_warning():
    """Most fields need a restart; a badge on all of them would mean nothing."""

    assert _row(_field(risk="restart_required"))["badges"] == []


def test_an_unknown_risk_is_not_invented_into_a_warning():
    assert _row(_field(risk="something_new"))["badges"] == []
