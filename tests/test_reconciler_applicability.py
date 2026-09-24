# SPDX-License-Identifier: AGPL-3.0-or-later
"""A state is reconciled when the device actually has it.

Every state reconciler writes only when telemetry disagrees with the intended
value. That is not enough on its own: if the device never adopts the value, the
disagreement is permanent and the write repeats for as long as the EMS runs.
Measured on a battery-less device over one night at a five-second loop, that is
7200 `acMode` writes -- and `acMode` moves a relay.

The answer is not to give up after a while, which would leave the device in a
state nobody has described. It is to ask whether the state exists on this device
at all. A device without a battery has no SoC window to manage, and a device
that never reports an AC mode offers nothing to reconcile against. Both are
fully describable situations, unlike "we tried and stopped".

An *applicable* state that disagrees is still written, every time. That case is
a real fault and looking away from it would be the dangerous choice.
"""

import logging

import pytest

from ems.runtime_intents import ac_output_intent
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


# --- AC mode: a device that never reports one offers nothing to reconcile ----


def _reconcile(controller, dev, item):
    return _writes(
        controller,
        lambda: controller.reconcile_ac_mode_intent(
            dev, item, ac_output_intent(dev.name)
        ),
    )


def test_ac_mode_is_not_written_repeatedly_to_a_device_that_never_reports_one():
    """One probe settles whether the field is there; after that, silence."""

    dev = _device()
    controller = _controller(dev)

    written = [_reconcile(controller, dev, _state(0, ac_mode=0)) for _ in range(5)]

    assert written[0] == [{"acMode": 2}]
    assert written[1:] == [[]] * 4


def test_ac_mode_zero_is_written_once_a_usable_mode_has_been_seen():
    """A blip is not the same as a device that has no such field.

    Once the device has shown a real AC mode, a later zero is a transient and
    the reconciler behaves as it always did.
    """

    dev = _device()
    controller = _controller(dev)

    assert _reconcile(controller, dev, _state(2, ac_mode=1)) == [{"acMode": 2}]
    assert _reconcile(controller, dev, _state(2, ac_mode=0)) == [{"acMode": 2}]


def test_a_matching_ac_mode_is_silent():
    dev = _device()
    controller = _controller(dev)

    assert _reconcile(controller, dev, _state(2, ac_mode=2)) == []


def test_an_unusable_ac_mode_is_still_refused():
    """Values outside the known set were already refused; that does not change."""

    dev = _device()
    controller = _controller(dev)

    assert _reconcile(controller, dev, _state(2, ac_mode=7)) == []


def test_unreported_ac_mode_is_logged_once_it_is_judged_unobservable(caplog):
    dev = _device()
    controller = _controller(dev)

    _reconcile(controller, dev, _state(0, ac_mode=0))
    with caplog.at_level(logging.WARNING):
        _reconcile(controller, dev, _state(0, ac_mode=0))

    assert any("ac_mode_never_reported" in message for message in caplog.messages)


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


def test_an_explicit_operator_intent_writes_even_into_an_unreported_ac_mode():
    """Silence suppresses the routine reconcile, never an instruction.

    The rule exists to stop an unprompted loop. An operator who sets the runtime
    role has said what they want, and the device may well accept it -- refusing
    would turn a safety measure for relays into a refusal to follow orders.
    """

    from ems.runtime_intents import ac_output_intent as intent_for

    dev = _device()
    controller = _controller(dev)
    operator_intent = intent_for(dev.name, "emsctl")

    written = _writes(
        controller,
        lambda: controller.reconcile_ac_mode_intent(
            dev, _state(0, ac_mode=0), operator_intent
        ),
    )

    assert written == [{"acMode": 2}]


def test_a_persisted_operator_reason_does_not_defeat_the_guard():
    """`emsctl device ... ac-mode` writes `runtime_role_reason` into
    runtime-state.json, and the controller replays it every cycle. Keying the
    exception on "this looks like an instruction" therefore never expired: the
    write resumed every loop, which is the very thing the guard exists to stop.

    An instruction is honoured once. What settles it after that is the device:
    if it still reports nothing, the field is not there to reconcile.
    """

    dev = _device()
    controller = _controller(dev)
    operator_intent = ac_output_intent(dev.name, "emsctl")

    attempts = [
        _writes(
            controller,
            lambda: controller.reconcile_ac_mode_intent(
                dev, _state(0, ac_mode=0), operator_intent
            ),
        )
        for _ in range(6)
    ]

    assert attempts[0] == [{"acMode": 2}]
    assert attempts[1:] == [[]] * 5


def test_the_routine_reconcile_also_probes_once_and_then_stops():
    dev = _device()
    controller = _controller(dev)

    attempts = [_reconcile(controller, dev, _state(0, ac_mode=0)) for _ in range(6)]

    assert attempts[0] == [{"acMode": 2}]
    assert attempts[1:] == [[]] * 5


def test_a_device_that_accepts_the_probe_is_reconciled_normally_afterwards():
    """The probe is how observability is decided, not a one-shot surrender."""

    dev = _device()
    controller = _controller(dev)

    assert _reconcile(controller, dev, _state(0, ac_mode=0)) == [{"acMode": 2}]
    # The device now reports a real mode, and drifts back later.
    assert _reconcile(controller, dev, _state(0, ac_mode=2)) == []
    assert _reconcile(controller, dev, _state(0, ac_mode=1)) == [{"acMode": 2}]
