# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 4 of docs/developer/settings-operation-store.md: an operating power never runs above its limit.

The operating values (``system.max_total_power``, a device's ``max_power``)
live in the runtime state, the hard limits in ``config.json``. The EMS holds
each operating value at the limit the live validation context names now, so a
runtime value written by hand, or kept from before an Admin save lowered the
limit, cannot drive the devices past it.
"""

import logging
from unittest.mock import Mock, patch

import pytest

from ems.controller import EMSController
from tests.test_write_gates import RuntimeStateStub, ShellyStub, device, state

pytestmark = [pytest.mark.unit, pytest.mark.power_control]


def _run(runtime_state, power_limits, load=3000):
    devices = [device("WR1"), device("WR2")]
    controller = EMSController(
        devices=devices,
        shelly=ShellyStub(load),
        sleep_enabled=False,
        runtime_state=runtime_state,
        power_limits=power_limits,
    )
    controller.set_output_limit = Mock()
    with patch("ems.controller.fetch_all_devices", return_value=[state(), state()]), patch(
        "ems.controller.cfg.SYSTEM_ENABLED", True
    ), patch("ems.controller.cfg.MAX_TOTAL_POWER", 1600), patch(
        "ems.controller.cfg.MAX_DEVICE_POWER", 800
    ), patch("ems.controller.cfg.MIN_OUTPUT_LIMIT", 0), patch(
        "ems.controller.cfg.LOOP_INTERVAL", 5
    ), patch("ems.controller.cfg.SOC_RECONCILE_INTERVAL", 0):
        controller.run_once()
        controller.run_once()
    written = {}
    for call in controller.set_output_limit.call_args_list:
        written[call.args[0].name] = call.args[1]
    return controller, devices, written


def test_an_operating_value_above_its_limit_runs_at_the_limit_and_is_named_once(caplog):
    caplog.set_level(logging.WARNING)
    runtime_state = RuntimeStateStub(
        system={"max_total_power": 1600},
        devices={"WR1": {"max_power": 800}, "WR2": {"max_power": 800}},
    )

    _, devices, written = _run(
        runtime_state,
        lambda: {"system_max_total_power": 900, "device_max_power": {"WR1": 300, "WR2": 800}},
    )

    assert devices[0].max_power == 300 and devices[1].max_power == 800
    assert written["WR1"] <= 300
    assert sum(written.values()) <= 900
    said = [record.getMessage() for record in caplog.records if "operating_power_above_limit" in record.getMessage()]
    assert len(said) == 2
    assert any("devices.WR1.max_power" in line for line in said)
    assert any("system.max_total_power" in line for line in said)


def test_an_operating_value_within_its_limit_runs_as_set():
    runtime_state = RuntimeStateStub(system={"max_total_power": 700}, devices={"WR1": {"max_power": 400}})

    _, devices, written = _run(
        runtime_state,
        lambda: {"system_max_total_power": 1600, "device_max_power": {"WR1": 800, "WR2": 800}},
    )

    assert devices[0].max_power == 400
    assert sum(written.values()) <= 700


def test_without_live_limits_the_operating_value_runs_as_before():
    runtime_state = RuntimeStateStub(system={"max_total_power": 1000}, devices={"WR1": {"max_power": 500}})

    _, devices, _ = _run(runtime_state, None)

    assert devices[0].max_power == 500
