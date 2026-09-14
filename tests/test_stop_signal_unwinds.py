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
    assert 'self.commanded_device_targets.get(dev.name, 0) >= 0' in source
    assert "continue" in source
