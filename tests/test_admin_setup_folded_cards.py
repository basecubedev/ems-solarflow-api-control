# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The lines Guided Setup's folded Settings cards show, against the real setup catalogue.

``safetySummary`` names the Control and safety fields by path; a path the
catalogue no longer carries would drop its part from the line without a sound.
``departedFields`` decides which folded card opens on its own. Both are drawn
in the browser (``frontend/admin-modern/src/lib/setupDraft.js``); these tests
feed them the catalogue the Setup route serves.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from admin.setup_config import build_setup_catalog

pytestmark = [
    pytest.mark.admin,
    pytest.mark.setup,
    pytest.mark.contract,
]

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "frontend" / "admin-modern" / "src" / "lib"


def _run(tmp_path, features, expression):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to evaluate the setup draft's summaries")
    payload = tmp_path / "input.json"
    payload.write_text(json.dumps({"catalog": build_setup_catalog(), "features": features}), encoding="utf-8")
    script = (
        "const fs = await import('node:fs');"
        f"const draft = await import({json.dumps((LIB / 'setupDraft.js').as_uri())});"
        f"const input = JSON.parse(fs.readFileSync({json.dumps(str(payload))}, 'utf8'));"
        "const { safety, advanced } = draft.setupSettingsFields(input.catalog);"
        f"process.stdout.write(JSON.stringify({expression}));"
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout)


def test_the_safety_line_names_every_part_from_the_catalogue_defaults(tmp_path):
    parts = _run(tmp_path, {}, "draft.safetySummary(input.features, safety)")

    assert parts == [
        {"key": "controlOn"},
        {"key": "writesOn"},
        {"key": "max", "watts": 800},
        {"key": "device", "watts": 800},
        {"key": "min", "watts": 35},
    ]


def test_the_safety_line_says_what_holds_ems_back(tmp_path):
    features = {
        "system.enabled": False,
        "system.allow_hardware_writes": False,
        "system.allow_mqtt_local_control_writes": False,
        "system.allow_mqtt_zendure_control_writes": False,
        "system.dry_run": True,
        "system.simulation_mode": True,
        "system.max_total_power": 1600,
    }

    parts = _run(tmp_path, features, "draft.safetySummary(input.features, safety)")

    assert [part["key"] for part in parts] == ["controlOff", "writesOff", "dryRun", "simulation", "max", "device", "min"]
    assert parts[4] == {"key": "max", "watts": 1600}


@pytest.mark.parametrize(
    ("closed", "still_open"),
    [
        (["system.allow_hardware_writes"], ["local_mqtt", "zendure_mqtt"]),
        (["system.allow_mqtt_local_control_writes", "system.allow_mqtt_zendure_control_writes"], ["local_api"]),
    ],
)
def test_the_safety_line_names_the_transports_that_still_write(tmp_path, closed, still_open):
    parts = _run(tmp_path, {path: False for path in closed}, "draft.safetySummary(input.features, safety)")

    assert parts[1] == {"key": "writesSome", "sources": still_open}


def test_every_output_write_gate_of_the_catalogue_is_one_the_line_names(tmp_path):
    named, gates = _run(
        tmp_path,
        {},
        "[draft.WRITE_GATES.map(([path]) => path), safety.map((f) => f.path)"
        ".filter((path) => /^system\\.allow_.*_writes$/.test(path) && path !== 'system.allow_state_reconciliation_writes')]",
    )

    assert gates
    assert sorted(named) == sorted(gates)


def test_departed_fields_name_each_value_away_from_its_default_in_its_own_card(tmp_path):
    untouched = _run(
        tmp_path,
        {},
        "[draft.departedFields(input.features, safety).length, draft.departedFields(input.features, advanced).length]",
    )
    changed = _run(
        tmp_path,
        {"system.dry_run": True, "system.max_device_power": 400, "system.loop_interval": 7},
        "[draft.departedFields(input.features, safety).map((f) => f.path), draft.departedFields(input.features, advanced).map((f) => f.path)]",
    )

    assert untouched == [0, 0]
    assert sorted(changed[0]) == ["system.dry_run", "system.max_device_power"]
    assert changed[1] == ["system.loop_interval"]
