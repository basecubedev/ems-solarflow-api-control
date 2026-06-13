# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ems.config import TOPOLOGY_DEFAULTS
from ems.topology import TopologyValidationError, resolve_topology, topology_to_dict


ROOT = Path(__file__).resolve().parents[1]
EMSCTL = ROOT / "emsctl.py"
DEVICE_IDS = ("inverter_1", "inverter_2", "inverter_3", "inverter_4", "inverter_5", "inverter_6")


def resolve(config, device_ids=DEVICE_IDS):
    return resolve_topology(config, device_ids, TOPOLOGY_DEFAULTS)


def enabled_topology(**updates):
    config = {
        "enabled": True,
        "root_mode": "parallel",
        "root_devices": ["inverter_1"],
        "links": [],
    }
    config.update(updates)
    return config


def test_missing_topology_keeps_backward_compatible_defaults():
    topology = resolve({})

    assert topology.enabled is False
    assert topology.root_devices == ()
    assert topology.links == ()
    assert topology.root_nodes == ()
    assert topology.branch_members == {}


def test_disabled_topology_does_not_validate_strict_fields():
    topology = resolve({
        "enabled": False,
        "root_mode": "serial",
        "root_devices": ["unknown"],
        "links": "invalid",
    })

    assert topology.enabled is False


def test_valid_flat_root_only_topology():
    topology = resolve(enabled_topology(root_devices=["inverter_1", "inverter_5"]))

    assert topology.enabled is True
    assert topology.root_devices == ("inverter_1", "inverter_5")
    assert [node.device_id for node in topology.root_nodes] == ["inverter_1", "inverter_5"]
    assert topology.branch_members == {
        "root": ("inverter_1", "inverter_5"),
    }


def test_valid_nested_topology_resolves_tree_and_branches():
    topology = resolve(enabled_topology(
        root_devices=["inverter_1", "inverter_5", "inverter_6"],
        links=[
            {
                "sources": ["inverter_2", "inverter_3"],
                "target": "inverter_1",
                "mode": "parallel",
            },
            {
                "sources": ["inverter_4"],
                "target": "inverter_2",
                "mode": "single",
            },
        ],
    ))

    assert topology.branch_members["inverter_1"] == (
        "inverter_1",
        "inverter_2",
        "inverter_4",
        "inverter_3",
    )
    assert topology.branch_members["inverter_2"] == ("inverter_2", "inverter_4")
    assert topology.branch_members["root"] == ("inverter_1", "inverter_5", "inverter_6")
    assert topology_to_dict(topology)["resolved_tree"][0] == {
        "device_id": "inverter_1",
        "source_mode": "parallel",
        "sources": [
            {
                "device_id": "inverter_2",
                "source_mode": "single",
                "sources": [
                    {
                        "device_id": "inverter_4",
                        "source_mode": None,
                        "sources": [],
                    }
                ],
            },
            {
                "device_id": "inverter_3",
                "source_mode": None,
                "sources": [],
            },
        ],
    }


@pytest.mark.parametrize(
    "config, message",
    [
        (
            enabled_topology(root_devices=["missing"]),
            "unknown device",
        ),
        (
            enabled_topology(links=[
                {"sources": ["inverter_2"], "target": "inverter_1", "mode": "single"},
                {"sources": ["inverter_2"], "target": "inverter_3", "mode": "single"},
            ]),
            "more than one link",
        ),
        (
            enabled_topology(root_devices=["inverter_1", "inverter_2"], links=[
                {"sources": ["inverter_2"], "target": "inverter_1", "mode": "single"},
            ]),
            "root device",
        ),
        (
            enabled_topology(links=[
                {"sources": ["inverter_1"], "target": "inverter_1", "mode": "single"},
            ]),
            "itself",
        ),
        (
            enabled_topology(root_devices=["inverter_1"], links=[
                {"sources": ["inverter_2"], "target": "inverter_3", "mode": "single"},
                {"sources": ["inverter_3"], "target": "inverter_2", "mode": "single"},
            ]),
            "cycle",
        ),
        (
            enabled_topology(links=[
                {"sources": ["inverter_2", "inverter_3"], "target": "inverter_1", "mode": "single"},
            ]),
            "exactly one source",
        ),
        (
            enabled_topology(root_devices=["inverter_1", "inverter_1"]),
            "duplicate device id",
        ),
        (
            enabled_topology(links=[
                {"sources": ["inverter_2", "inverter_2"], "target": "inverter_1", "mode": "parallel"},
            ]),
            "duplicate device id",
        ),
    ],
)
def test_invalid_topologies_are_rejected(config, message):
    with pytest.raises(TopologyValidationError, match=message):
        resolve(config)


def test_non_root_source_must_resolve_to_root():
    config = enabled_topology(root_devices=["inverter_1"], links=[
        {"sources": ["inverter_2"], "target": "inverter_3", "mode": "single"},
    ])

    with pytest.raises(TopologyValidationError, match="resolve into a root device"):
        resolve(config)


def test_duplicate_target_links_are_rejected():
    config = enabled_topology(links=[
        {"sources": ["inverter_2"], "target": "inverter_1", "mode": "single"},
        {"sources": ["inverter_3"], "target": "inverter_1", "mode": "single"},
    ])

    with pytest.raises(
        TopologyValidationError,
        match="topology target may only appear once in links: inverter_1",
    ):
        resolve(config)


def write_diagnose_config(tmp_path, topology):
    config_path = tmp_path / "config.json"
    runtime_path = tmp_path / "runtime-state.json"
    config_path.write_text(json.dumps({
        "system": {
            "enabled": True,
            "dry_run": False,
            "max_total_power": 900,
            "max_device_power": 800,
            "loop_interval": 5,
            "min_output_limit": 35,
            "runtime_state_path": str(runtime_path),
        },
        "ha": {
            "enabled": False,
            "control_enabled": False,
            "url": "",
            "token": "",
        },
        "dashboard": {"enabled": False},
        "winter": {"enabled": False},
        "devices": [
            {"name": "inverter_1", "max_power": 800},
            {"name": "inverter_2", "max_power": 800},
        ],
        "grid_meter": {"type": "ha"},
        "topology": topology,
    }))
    runtime_path.write_text(json.dumps({
        "system": {"enabled": True},
        "devices": {},
    }))
    return config_path, runtime_path


def run_diagnose(tmp_path, *args):
    config_path = tmp_path / "config.json"
    runtime_path = tmp_path / "runtime-state.json"
    return subprocess.run(
        [
            sys.executable,
            str(EMSCTL),
            "--config",
            str(config_path),
            "--runtime-state",
            str(runtime_path),
            "diagnose",
            *args,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_emsctl_diagnose_json_contains_topology_section(tmp_path):
    write_diagnose_config(
        tmp_path,
        {
            "enabled": True,
            "root_mode": "parallel",
            "root_devices": ["inverter_1"],
            "links": [
                {
                    "sources": ["inverter_2"],
                    "target": "inverter_1",
                    "mode": "single",
                }
            ],
        },
    )
    result = run_diagnose(tmp_path, "--json")
    payload = json.loads(result.stdout)

    assert result.returncode == 0, result.stderr
    assert payload["topology"]["enabled"] is True
    assert payload["topology"]["valid"] is True
    assert payload["topology"]["root_devices"] == ["inverter_1"]
    assert payload["topology"]["branch_members"]["inverter_1"] == [
        "inverter_1",
        "inverter_2",
    ]
    assert payload["topology"]["branch_members"]["root"] == ["inverter_1"]


def test_emsctl_diagnose_warns_for_non_object_topology(tmp_path):
    write_diagnose_config(tmp_path, True)

    result = run_diagnose(tmp_path, "--json")
    payload = json.loads(result.stdout)

    assert result.returncode == 0, result.stderr
    assert payload["status"] == "warning"
    assert payload["topology"]["enabled"] is False
    assert payload["topology"]["valid"] is True
    assert payload["topology"]["warnings"] == [
        "topology section is not an object and was ignored"
    ]
    assert any(
        check["code"] == "topology_ignored_non_object"
        for check in payload["checks"]
    )

    result = run_diagnose(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "Topology: disabled" in result.stdout
    assert "Warning: topology section is not an object and was ignored" in result.stdout
