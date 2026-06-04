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
