# SPDX-License-Identifier: AGPL-3.0-or-later
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from dashboard.runtime_write import (
    RuntimeWriteError,
    apply_device_update,
    apply_system_update,
    build_validation_context,
    effective_limits,
)
from ems.runtime_state import RuntimeState


def test_runtime_write_uses_config_aware_power_limits(tmp_path):
    runtime_state = RuntimeState(
        str(tmp_path / "runtime-state.json"),
        {
            "system": {
                "enabled": True,
                "max_total_power": 1200,
                "loop_interval": 5,
                "min_output_limit": 35,
            },
            "ha": {"enabled": False, "control_enabled": False},
            "winter": {"enabled": False},
            "devices": {
                "WR1": {
                    "enabled": True,
                    "max_power": 800,
                    "offgrid_socket_mode": "off",
                    "pv_priority_factor": 1.0,
                }
            },
        },
    )
    runtime_state.load_or_create()
    context = build_validation_context(
        {
            "system": {
                "max_total_power": 1200,
                "max_total_power_limit": 1200,
                "max_device_power": 900,
            },
            "devices": [
                {
                    "name": "WR1",
                    "max_power": 800,
                }
            ],
        },
        runtime_state,
    )

    with pytest.raises(RuntimeWriteError, match="max_total_power must be between 0 and 1200"):
        apply_system_update(runtime_state, {"max_total_power": 5000}, context)

    with pytest.raises(RuntimeWriteError, match="max_power must be between 0 and 800"):
        apply_device_update(runtime_state, "WR1", {"max_power": 1200}, context)

    apply_system_update(runtime_state, {"max_total_power": 1000}, context)
    apply_device_update(runtime_state, "WR1", {"max_power": 700}, context)
    assert runtime_state.snapshot()["system"]["max_total_power"] == 1000
    assert runtime_state.snapshot()["devices"]["WR1"]["max_power"] == 700


def test_lowering_system_power_does_not_lower_later_edit_ceiling(tmp_path):
    runtime_state = RuntimeState(
        str(tmp_path / "runtime-state.json"),
        {
            "system": {
                "enabled": True,
                "max_total_power": 1600,
                "loop_interval": 5,
                "min_output_limit": 35,
            },
            "ha": {"enabled": False, "control_enabled": False},
            "winter": {"enabled": False},
            "devices": {},
        },
    )
    runtime_state.load_or_create()
    context = build_validation_context(runtime_state=runtime_state)

    apply_system_update(runtime_state, {"max_total_power": 800}, context)
    apply_system_update(runtime_state, {"max_total_power": 1600}, context)

    assert runtime_state.snapshot()["system"]["max_total_power"] == 1600
    assert effective_limits(context)["system"]["max_total_power"] == 5000


def test_lowering_system_power_does_not_lower_min_output_limit_ceiling(tmp_path):
    runtime_state = RuntimeState(
        str(tmp_path / "runtime-state.json"),
        {
            "system": {
                "enabled": True,
                "max_total_power": 1600,
                "loop_interval": 5,
                "min_output_limit": 35,
            },
            "ha": {"enabled": False, "control_enabled": False},
            "winter": {"enabled": False},
            "devices": {},
        },
    )
    runtime_state.load_or_create()
    context = build_validation_context(runtime_state=runtime_state)

    apply_system_update(runtime_state, {"max_total_power": 800}, context)
    apply_system_update(runtime_state, {"min_output_limit": 1600}, context)

    snapshot = runtime_state.snapshot()
    assert snapshot["system"]["max_total_power"] == 800
    assert snapshot["system"]["min_output_limit"] == 1600
    assert effective_limits(context)["system"]["min_output_limit"] == 5000


def test_lowering_device_power_does_not_lower_device_ceiling(tmp_path):
    runtime_state = RuntimeState(
        str(tmp_path / "runtime-state.json"),
        {
            "system": {
                "enabled": True,
                "max_total_power": 1600,
                "loop_interval": 5,
                "min_output_limit": 35,
            },
            "ha": {"enabled": False, "control_enabled": False},
            "winter": {"enabled": False},
            "devices": {
                "WR1": {
                    "enabled": True,
                    "max_power": 800,
                    "offgrid_socket_mode": "off",
                    "pv_priority_factor": 1.0,
                }
            },
        },
    )
    runtime_state.load_or_create()
    context = build_validation_context(
        {
            "system": {"max_device_power": 800},
            "devices": [{"name": "WR1", "max_power": 800}],
        },
        runtime_state,
    )

    apply_device_update(runtime_state, "WR1", {"max_power": 400}, context)
    apply_device_update(runtime_state, "WR1", {"max_power": 800}, context)

    assert runtime_state.snapshot()["devices"]["WR1"]["max_power"] == 800
    assert effective_limits(context)["devices"]["WR1"] == 800


def test_runtime_numeric_limits_reject_invalid_high_and_low_values(tmp_path):
    runtime_state = RuntimeState(
        str(tmp_path / "runtime-state.json"),
        {
            "system": {
                "enabled": True,
                "max_total_power": 1600,
                "loop_interval": 5,
                "min_output_limit": 35,
            },
            "ha": {"enabled": False, "control_enabled": False},
            "winter": {"enabled": False},
            "devices": {
                "WR1": {
                    "enabled": True,
                    "max_power": 800,
                    "offgrid_socket_mode": "off",
                    "pv_priority_factor": 1.0,
                }
            },
        },
    )
    runtime_state.load_or_create()
    context = build_validation_context(
        {
            "system": {
                "max_total_power_limit": 5000,
                "max_device_power": 800,
            },
            "devices": [{"name": "WR1", "max_power": 800}],
        },
        runtime_state,
    )

    invalid_system_updates = [
        ({"max_total_power": 5001}, "max_total_power must be between 0 and 5000"),
        ({"loop_interval": 0}, "loop_interval must be between 1 and 3600"),
        ({"loop_interval": 3601}, "loop_interval must be between 1 and 3600"),
    ]
    for payload, message in invalid_system_updates:
        with pytest.raises(RuntimeWriteError, match=message):
            apply_system_update(runtime_state, payload, context)

    invalid_device_updates = [
        ({"max_power": 801}, "max_power must be between 0 and 800"),
        ({"pv_priority_factor": 0}, "pv_priority_factor must be between 0.01 and 100.0"),
        ({"pv_priority_factor": 101}, "pv_priority_factor must be between 0.01 and 100.0"),
    ]
    for payload, message in invalid_device_updates:
        with pytest.raises(RuntimeWriteError, match=message):
            apply_device_update(runtime_state, "WR1", payload, context)


def test_runtime_write_limit_fallback_is_conservative():
    limits = effective_limits(build_validation_context())
    assert limits["system"]["max_total_power"] == 5000
    assert limits["fallback_device_max_power"] == 5000


def test_runtime_state_dashboard_updates_are_thread_safe(tmp_path):
    path = tmp_path / "runtime-state.json"
    runtime_state = RuntimeState(
        str(path),
        {
            "system": {
                "enabled": True,
                "max_total_power": 1200,
                "loop_interval": 5,
                "min_output_limit": 0,
            },
            "ha": {"enabled": False, "control_enabled": False},
            "winter": {"enabled": False},
            "devices": {},
        },
    )
    runtime_state.load_or_create()
    context = build_validation_context(runtime_state=runtime_state)

    def update(value):
        apply_system_update(runtime_state, {"max_total_power": value}, context)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(update, range(100, 900, 10)))

    payload = json.loads(path.read_text())
    assert isinstance(payload["system"]["max_total_power"], int)
    assert 100 <= payload["system"]["max_total_power"] < 900
