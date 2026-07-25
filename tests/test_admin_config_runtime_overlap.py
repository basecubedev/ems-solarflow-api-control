# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only config/runtime overlap provenance for Admin maintenance (Tier 1)."""

import json
from types import SimpleNamespace

import pytest

from admin.config_runtime_overlap import (
    compute_overlap_provenance,
    read_runtime_state,
    resolve_runtime_state_path,
)

pytestmark = pytest.mark.simulation


def _config():
    return {
        "system": {
            "enabled": True,
            "max_total_power": 1600,
            "loop_interval": 3,
            "min_output_limit": 35,
        },
        "winter": {"enabled": False},
        "devices": [
            {"name": "WR1", "max_power": 800, "pv_priority_factor": 1.0, "enabled": True},
        ],
    }


def test_no_runtime_data_is_all_config():
    prov = compute_overlap_provenance(_config(), {})
    assert prov["system.loop_interval"] == {
        "config_value": 3,
        "effective_value": 3,
        "source": "config",
    }
    assert prov["devices"]["WR1"]["max_power"]["source"] == "config"
    assert prov["winter.enabled"]["source"] == "config"


def test_runtime_override_is_detected():
    runtime = {"system": {"loop_interval": 5}, "devices": {"WR1": {"max_power": 600}}}
    prov = compute_overlap_provenance(_config(), runtime)
    loop = prov["system.loop_interval"]
    assert loop["config_value"] == 3
    assert loop["effective_value"] == 5
    assert loop["source"] == "dashboard_override"
    device = prov["devices"]["WR1"]["max_power"]
    assert device["effective_value"] == 600
    assert device["source"] == "dashboard_override"


def test_runtime_equal_to_config_is_not_override():
    prov = compute_overlap_provenance(_config(), {"system": {"loop_interval": 3}})
    assert prov["system.loop_interval"]["source"] == "config"
    assert prov["system.loop_interval"]["effective_value"] == 3


def test_winter_enabled_override():
    prov = compute_overlap_provenance(_config(), {"winter": {"enabled": True}})
    assert prov["winter.enabled"]["effective_value"] is True
    assert prov["winter.enabled"]["source"] == "dashboard_override"


def test_missing_device_in_runtime_is_config():
    prov = compute_overlap_provenance(_config(), {"devices": {}})
    assert prov["devices"]["WR1"]["enabled"]["source"] == "config"


def test_resolve_path_honors_configured_relative_path(tmp_path):
    context = SimpleNamespace(install_root=tmp_path)
    path = resolve_runtime_state_path(
        context, {"system": {"runtime_state_path": "data/rt.json"}}
    )
    assert path == str(tmp_path / "data" / "rt.json")


def test_resolve_path_defaults_when_config_silent(tmp_path):
    context = SimpleNamespace(install_root=tmp_path)
    path = resolve_runtime_state_path(context, {})
    assert path.endswith("runtime-state.json")


def test_resolve_path_absolute_is_used_verbatim(tmp_path):
    context = SimpleNamespace(install_root=tmp_path)
    absolute = str(tmp_path / "elsewhere" / "rt.json")
    path = resolve_runtime_state_path(context, {"system": {"runtime_state_path": absolute}})
    assert path == absolute


def test_read_missing_file_returns_empty(tmp_path):
    assert read_runtime_state(str(tmp_path / "nope.json")) == {}


def test_read_malformed_file_returns_empty(tmp_path):
    path = tmp_path / "rt.json"
    path.write_text("{not json", encoding="utf-8")
    assert read_runtime_state(str(path)) == {}


def test_read_valid_file(tmp_path):
    path = tmp_path / "rt.json"
    path.write_text(json.dumps({"system": {"loop_interval": 9}}), encoding="utf-8")
    assert read_runtime_state(str(path)) == {"system": {"loop_interval": 9}}
