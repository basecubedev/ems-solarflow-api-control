# SPDX-License-Identifier: AGPL-3.0-or-later
"""SoC-limit reconciliation writes only the bounds the operator manages.

``min_soc``/``max_soc`` of ``0`` mean "leave this value to the device". A
payload that always carried both properties wrote ``minSoc=0`` or ``socSet=0``
to the inverter whenever only one bound was managed.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ems import config as cfg
from ems.controller import EMSController

pytestmark = [
    pytest.mark.unit,
    pytest.mark.power_control,
]


def _controller():
    return EMSController(devices=[], shelly=None, sleep_enabled=False)


def _device(min_soc, max_soc):
    return SimpleNamespace(name="WR1", min_soc=min_soc, max_soc=max_soc)


def _state(min_soc=15, max_soc=95):
    return SimpleNamespace(min_soc=min_soc, max_soc=max_soc, pack_num=1)


def _reconcile(dev, state):
    writes = []

    def capture(device, payload, **kwargs):
        writes.append(dict(payload))
        return True

    with patch("ems.controller.write_device_properties", capture), patch.object(
        cfg, "state_reconciliation_writes_allowed", lambda gate: True
    ):
        result = _controller().apply_soc_limits(dev, state)
    return result, writes


@pytest.mark.parametrize(
    "min_soc, max_soc, expected",
    [
        (20, 90, {"minSoc": 200, "socSet": 900}),
        (0, 90, {"socSet": 900}),
        (20, 0, {"minSoc": 200}),
    ],
)
def test_only_managed_bounds_are_written(min_soc, max_soc, expected):
    result, writes = _reconcile(_device(min_soc, max_soc), _state())
    assert result is True
    assert writes == [expected]


def test_both_bounds_unmanaged_writes_nothing():
    assert _reconcile(_device(0, 0), _state()) == (True, [])


@pytest.mark.parametrize("min_soc, max_soc", [(0, 95), (15, 0)])
def test_an_unmanaged_bound_never_counts_as_a_difference(min_soc, max_soc):
    result, writes = _reconcile(_device(min_soc, max_soc), _state(15, 95))
    assert result is True
    assert writes == []


class _AssistStore:
    def __init__(self, active):
        self.active = active

    def get_device_state(self, device, now=None):
        return {"full_charge_assist_active": self.active}


@pytest.mark.parametrize("active, socset", [(True, 1000), (False, 900)])
def test_periodic_reconcile_leaves_an_active_full_charge_assist_at_100(
    active, socset
):
    controller = EMSController(
        devices=[],
        shelly=None,
        sleep_enabled=False,
        battery_full_charge_store=_AssistStore(active),
    )
    dev = _device(15, 90)
    writes = []

    def capture(device, payload, **kwargs):
        writes.append(dict(payload))
        return True

    with patch("ems.controller.write_device_properties", capture), patch.object(
        cfg, "state_reconciliation_writes_allowed", lambda gate: True
    ), patch.object(controller, "apply_device_modes", lambda dev, state: None):
        controller.reconcile_device_state_limits(
            dev, _state(15, 50), winter_active=False, winter_adjust_today=False
        )

    assert writes == [{"minSoc": 150, "socSet": socset}]


@pytest.mark.parametrize(
    "device_min_soc, expected", [(30, 30), (10, 15), (70, 40)]
)
def test_a_restart_in_winter_keeps_the_ramped_min_soc(device_min_soc, expected):
    """No in-memory ramp target after a restart; the device's value is adopted."""

    controller = _controller()
    dev = _device(15, 95)
    with patch.object(cfg, "winter_feature_enabled", lambda runtime: True):
        target, adjusted = controller.winter_reconciliation_target(
            dev,
            _state(device_min_soc, 95),
            winter_active=True,
            adjust_today=False,
        )

    assert (target, adjusted) == (expected, False)
    assert controller.winter_min_soc_targets["WR1"] == expected
