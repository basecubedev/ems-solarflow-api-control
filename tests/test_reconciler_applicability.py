# SPDX-License-Identifier: AGPL-3.0-or-later
"""A state is reconciled when the device actually has it.

A reconciler writes only when telemetry disagrees with the intended value, which
is not enough on its own: if the device never adopts the value, the disagreement
is permanent and the write repeats for as long as the EMS runs.

Giving up after a while would leave the device in a state nobody has described.
Asking whether the state exists on this device at all does not, and a device
with no battery is the clear case -- it has no SoC window to manage, so the
window is not reconciled rather than reconciled and ignored.

An *applicable* state that disagrees is still written, every time. That case is
a real fault and looking away from it would be the dangerous choice.

The same question for `acMode` is open and deliberately not answered here: see
`docs/develop/control-architecture-plan.md`. Two designs were tried and both
stranded a device that reports no AC mode -- the field has to be reconcilable in
both directions, and nothing has yet observed hardware that reports none.
"""

import pytest

from tests.test_write_gates import device, state as base_state

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
]


def _controller(dev):
    from tests.test_write_gates import ShellyStub
    from ems.controller import EMSController

    controller = EMSController(
        devices=[dev], shelly=ShellyStub(0), sleep_enabled=False, runtime_state=None
    )
    controller.device_state_writes_allowed = lambda _dev: True
    return controller


def _device(min_soc=15, max_soc=100):
    dev = device("WR1")
    dev.min_soc = min_soc
    dev.max_soc = max_soc
    dev.smart_mode = 1
    dev.grid_off_mode = None
    return dev


def _state(pack_num, **overrides):
    item = base_state(soc=50, solar=0, output=0, soc_limit=0)
    item.pack_num = pack_num
    item.min_soc = overrides.pop("tele_min_soc", 0)
    item.max_soc = overrides.pop("tele_max_soc", 0)
    for key, value in overrides.items():
        setattr(item, key, value)
    return item


def _writes(controller, call):
    from unittest.mock import patch

    recorded = []
    with patch(
        "ems.controller.write_device_properties",
        side_effect=lambda dev, props, **kw: recorded.append(dict(props)) or True,
    ):
        call()
    return recorded


# --- SoC window: a device without a battery has none -------------------------


def test_soc_window_is_not_reconciled_without_a_battery():
    dev = _device()
    controller = _controller(dev)
    item = _state(0)

    written = _writes(
        controller, lambda: controller.apply_soc_limits(dev, item)
    )

    assert written == []


def test_soc_window_is_reconciled_with_a_battery():
    dev = _device()
    controller = _controller(dev)
    item = _state(2)

    written = _writes(
        controller, lambda: controller.apply_soc_limits(dev, item)
    )

    assert written == [{"minSoc": 150, "socSet": 1000}]


def test_soc_window_is_reconciled_while_battery_presence_is_unknown():
    """Silence must not unlock the new behaviour: unknown keeps today's."""

    dev = _device()
    controller = _controller(dev)
    item = _state(None)

    written = _writes(
        controller, lambda: controller.apply_soc_limits(dev, item)
    )

    assert written == [{"minSoc": 150, "socSet": 1000}]


def test_a_matching_soc_window_is_still_silent_with_a_battery():
    dev = _device()
    controller = _controller(dev)
    item = _state(2, tele_min_soc=15, tele_max_soc=100)

    written = _writes(
        controller, lambda: controller.apply_soc_limits(dev, item)
    )

    assert written == []


def test_skipping_the_soc_window_reports_success():
    """Nothing to do is success, exactly as the existing "unmanaged" path is."""

    dev = _device()
    controller = _controller(dev)

    assert controller.apply_soc_limits(dev, _state(0)) is True


# --- the reconcilers keep writing when the state IS applicable ---------------


def test_an_applicable_state_that_disagrees_is_written_every_time():
    """No convergence limit: an applicable mismatch stays a fault worth fixing."""

    dev = _device()
    controller = _controller(dev)
    item = _state(2)

    for _ in range(5):
        assert _writes(
            controller, lambda: controller.apply_soc_limits(dev, item)
        ) == [{"minSoc": 150, "socSet": 1000}]
