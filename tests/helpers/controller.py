# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic controller-run helpers for the mixed-transport scenarios.

Drives ``EMSController.run_once`` with the real allocation math while patching
only the config globals (write gates, limits) and the telemetry fetch boundary.
No sleeps, no network.
"""

import contextlib
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ems.controller import EMSController
from ems.models import DeviceState

# cfg globals shared by both the synthetic-state and true-fetch control runs.
_CFG_CONTROL_GLOBALS = dict(
    SYSTEM_ENABLED=True,
    MIN_OUTPUT_LIMIT=0,
    LOOP_INTERVAL=5,
    DEADBAND=10,
    SOC_RECONCILE_INTERVAL=0,
    REDISTRIBUTE_CLAMPED_POWER=True,
    PV_KWP_WEIGHTING=True,
    BATTERY_KWH_WEIGHTING=True,
    DRY_RUN=False,
    SIMULATION_MODE=False,
    ARGS=SimpleNamespace(replay=False),
    ALLOW_STATE_RECONCILIATION_WRITES=False,
)


def _gate_globals(gates):
    return dict(
        ALLOW_HARDWARE_WRITES=gates.get("allow_hardware_writes", False),
        ALLOW_MQTT_LOCAL_CONTROL_WRITES=gates.get("allow_mqtt_local_control_writes", False),
        ALLOW_MQTT_ZENDURE_CONTROL_WRITES=gates.get("allow_mqtt_zendure_control_writes", False),
    )


@contextlib.contextmanager
def patch_snapshot_clock(clock):
    """Point the MQTT staleness math at ``clock`` for the duration of the block.

    The telemetry aggregators already stamp ``last_seen`` with the same clock (via
    the network), so ``clock.advance`` deterministically ages a snapshot without a
    real sleep. ``None`` is a no-op so callers without a clock stay simple.
    """

    if clock is None:
        yield
        return
    fake_time = SimpleNamespace(monotonic=clock.monotonic)
    with patch("ems.zendure_mqtt.service.time", fake_time):
        yield


class RuntimeStateStub:
    """Minimal runtime-state that returns defaults and never touches disk."""

    def __init__(self):
        self.system = {}
        self.devices = {}

    def load_if_changed(self):
        return None

    def get_system(self, key, default=None):
        return self.system.get(key, default)

    def get_device(self, device_name, key, default=None):
        return self.devices.get(device_name, {}).get(key, default)


def make_state(**overrides) -> DeviceState:
    """A healthy, discharge-capable device state; override any field."""

    base = dict(
        soc=80, min_soc=15, max_soc=100, solar=800, output=0,
        pack_in=0, pack_out=0, temp=20, voltage=48, rssi=0, remain_minutes=0,
        solar1=0, solar2=0, solar3=0, solar4=0, output_limit=0, soc_limit=0,
        pack_state=2, fault_level=0, smart_mode=1, grid_off_mode=0,
        ac_mode=2, ac_status=1, dc_status=1, grid_state=1, input_limit_w=0,
    )
    base.update(overrides)
    return DeviceState(**base)


def run_control_cycle(
    installation,
    *,
    states=None,
    gates=None,
    max_total_power=6400,
    max_device_power=800,
):
    """Run one control cycle over an installation's devices.

    ``states`` defaults to a fresh state per device (real allocation math runs
    over it). ``gates`` defaults to the scenario's write-gate flags.
    """

    devices = installation.devices
    if states is None:
        states = [make_state() for _ in devices]
    if gates is None:
        gates = installation.scenario.write_gates

    controller = EMSController(
        devices,
        installation.grid_meter,
        sleep_enabled=False,
        runtime_state=RuntimeStateStub(),
    )
    controller.run_startup_ac_mode_reconcile_once = Mock()
    with patch(
        "ems.controller.fetch_all_devices", return_value=states
    ), patch.multiple(
        "ems.controller.cfg",
        MAX_TOTAL_POWER=max_total_power,
        MAX_DEVICE_POWER=max_device_power,
        **_CFG_CONTROL_GLOBALS,
        **_gate_globals(gates),
    ):
        controller.run_once()
    return controller


def run_installation_cycle(
    installation,
    *,
    gates=None,
    max_total_power=6400,
    max_device_power=800,
    clock=None,
):
    """Run one real control cycle over an installation via the production fetch path.

    Unlike :func:`run_control_cycle`, this does NOT patch ``fetch_all_devices``:
    each device's own ``fetch()`` runs, so the full
    MQTT-payload -> snapshot -> production-client -> DeviceState -> controller ->
    allocation -> transport-write chain is exercised. Only cfg globals (write
    gates/limits) and, optionally, the MQTT snapshot clock are patched; the broker
    boundary stays the fake in-process network.
    """

    if gates is None:
        gates = installation.scenario.write_gates
    controller = EMSController(
        installation.devices,
        installation.grid_meter,
        sleep_enabled=False,
        runtime_state=RuntimeStateStub(),
    )
    controller.run_startup_ac_mode_reconcile_once = Mock()
    with patch.multiple(
        "ems.controller.cfg",
        MAX_TOTAL_POWER=max_total_power,
        MAX_DEVICE_POWER=max_device_power,
        **_CFG_CONTROL_GLOBALS,
        **_gate_globals(gates),
    ), patch_snapshot_clock(clock):
        controller.run_once()
    return controller
