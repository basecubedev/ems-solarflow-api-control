# SPDX-License-Identifier: AGPL-3.0-or-later
"""A stop signal must unwind the loop, and must not reset the devices.

Python's default SIGTERM action terminates the process without raising, so
``finally`` never runs -- verified, not recalled. That block closes the
grid-meter client and stops the InfluxDB writer and both MQTT runtimes, and
`docker stop`, `docker compose down` and `systemctl stop` all send SIGTERM, so
none of it happened except on Ctrl-C.

What it must *not* do is return a charging device. An operator stop is usually a
restart, and the instruction is that the last state survives one: discharging
devices already keep their outputLimit, and charging is the only state that
block ever touched. A stop the EMS reached by itself -- --once, --max-cycles, an
unhandled error -- is the opposite case, because nothing is coming back to
supervise the charge.
"""

import importlib.util
import signal
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
    pytest.mark.simulation,
]

ROOT = Path(__file__).resolve().parents[1]


def entry_module():
    """Load the entry script, which is not importable by name (it has dashes)."""

    spec = importlib.util.spec_from_file_location(
        "ems_entry", ROOT / "ems-solarflow-api-control.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def restored_signals():
    previous = {
        name: signal.getsignal(getattr(signal, name))
        for name in ("SIGTERM", "SIGHUP")
        if hasattr(signal, name)
    }
    yield
    for name, handler in previous.items():
        signal.signal(getattr(signal, name), handler)


def test_a_stop_signal_raises_instead_of_terminating(restored_signals):
    module = entry_module()
    module.install_stop_signal_handlers()

    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler), "SIGTERM was left on its default, which skips finally"

    # SystemExit rather than Exception on purpose: an `except Exception` around
    # the control loop must not be able to swallow a stop.
    with pytest.raises(SystemExit):
        handler(signal.SIGTERM, None)


def test_the_unwind_reaches_a_finally_block(restored_signals):
    """What the handler is for, stated as the property that matters."""

    module = entry_module()
    module.install_stop_signal_handlers()
    handler = signal.getsignal(signal.SIGTERM)

    released = []
    try:
        try:
            handler(signal.SIGTERM, None)
        finally:
            released.append("charge returned")
    except SystemExit:
        pass

    assert released == ["charge returned"]


def test_sighup_is_handled_too_where_the_platform_has_it(restored_signals):
    if not hasattr(signal, "SIGHUP"):
        pytest.skip("platform has no SIGHUP")

    module = entry_module()
    module.install_stop_signal_handlers()

    assert callable(signal.getsignal(signal.SIGHUP))


def test_a_signal_stop_is_told_apart_from_the_ems_stopping_itself(restored_signals):
    """The `finally` needs to know which kind of stop this was.

    An operator stop preserves a running charge because a restart is coming; a
    stop the EMS reached on its own returns the devices because nothing is.
    """

    module = entry_module()
    stopped_by = module.install_stop_signal_handlers()

    assert stopped_by["signal"] is None, "nothing has stopped us yet"

    handler = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit):
        handler(signal.SIGTERM, None)

    assert stopped_by["signal"] == "SIGTERM"


def test_the_shutdown_release_only_ever_touched_charging_devices():
    """Which is why honouring "keep the last state" costs the discharge side
    nothing: it was never reset here in the first place."""

    import inspect

    from ems.controller import EMSController

    source = inspect.getsource(EMSController.release_charging_devices)
    assert "if not self.charge_commanded_by_ems(dev):" in source
    assert "continue" in source


def test_the_kept_across_stop_line_names_what_is_still_drawing():
    """A line saying "a restart is coming" without saying what is drawing while
    it does leaves the operator nothing to act on.

    Pinned because the documentation promises the device names, and the first
    version of the event carried only the signal -- the doc would have been a
    false claim about the EMS's own output.
    """

    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "ems-solarflow-api-control.py"
    text = source.read_text(encoding="utf-8")
    start = text.index('"ac_charge_kept_across_stop"')
    block = text[start - 600 : start + 400]

    assert "devices=" in block
    assert "charging_w=" in block
    # Silent when nothing was charging: the normal stop must not gain a line.
    assert "if charging:" in block
    assert "ems.charges_held_by_ems()" in block
    assert "commanded_device_targets" not in block


def test_a_charge_kept_across_a_stop_is_named_by_the_ems_s_own_record(monkeypatch):
    """The regulator's target is not the only record of a running charge.

    Switching control back on resets the targets, and an unreachable device is
    given zero, while the transport still knows it last put a charge on the
    wire. Naming devices by the target alone left such a charge out of the line
    that says what is still drawing after the stop.
    """

    from types import SimpleNamespace

    from ems.controller import EMSController
    from tests.test_write_gates import ShellyStub, device, state

    reset, unreachable, idle = device("RESET"), device("GONE"), device("IDLE")
    reset.charge_commanded = True
    unreachable.charge_commanded = True
    idle.charge_commanded = False
    ems = EMSController(
        devices=[reset, unreachable, idle], shelly=ShellyStub(0), sleep_enabled=False
    )
    ems.commanded_device_targets = {"RESET": 0, "GONE": 0, "IDLE": 0}
    drawing = state(solar=0)
    drawing.grid_input = 640
    ems.last_states = {"RESET": drawing, "GONE": SimpleNamespace(), "IDLE": drawing}

    assert ems.charges_held_by_ems() == {"RESET": 640, "GONE": 0}

    _open_the_write_gate(monkeypatch)
    ems.commanded_device_targets["IDLE"] = -300
    assert ems.charges_held_by_ems()["IDLE"] == 300


def test_a_dry_run_names_no_charge_kept_across_a_stop(monkeypatch):
    """With the write gate shut nothing was put on the wire to keep.

    The regulator still holds a charge target in a dry run, and the line after
    a signal stop named it as drawing although no device was ever told to.
    """

    from ems import config as cfg
    from ems.controller import EMSController
    from tests.test_write_gates import ShellyStub, device

    dry = device("DRY")
    dry.charge_commanded = False
    ems = EMSController(devices=[dry], shelly=ShellyStub(0), sleep_enabled=False)
    ems.commanded_device_targets = {"DRY": -300}

    _open_the_write_gate(monkeypatch)
    monkeypatch.setattr(cfg, "DRY_RUN", True)
    assert ems.charges_held_by_ems() == {}

    monkeypatch.setattr(cfg, "DRY_RUN", False)
    assert ems.charges_held_by_ems() == {"DRY": 300}


def _open_the_write_gate(monkeypatch):
    from types import SimpleNamespace

    from ems import config as cfg

    monkeypatch.setattr(cfg, "DRY_RUN", False)
    monkeypatch.setattr(cfg, "SIMULATION_MODE", False)
    monkeypatch.setattr(cfg, "ALLOW_HARDWARE_WRITES", True)
    monkeypatch.setattr(cfg, "ARGS", SimpleNamespace(replay=False))
