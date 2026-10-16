# SPDX-License-Identifier: AGPL-3.0-or-later
"""A full-charge assist behind a shut state gate holds no output control (K25).

With ``allow_state_reconciliation_writes`` off nothing the assist needs can be
written: neither its ``socSet=100`` nor its ``acMode=1`` and ``inputLimit``.
It started all the same and claimed the device, out of output control, so
every ``outputLimit`` write was skipped and the device kept its last limit,
discharging into an export, until the firmware reported ``socLimit == 1``,
which that frozen limit could keep from happening. A due assist now waits for
the gate to start, and one already running claims nothing while the gate is
shut: it stays active in the store and takes the device again once the gate
opens. Its restore behind a shut gate is
``test_full_charge_assist_restore_behind_a_shut_gate.py``'s (K22).

An MQTT control device carries no state writes at all, every gate open or
not: the assist started there all the same, claimed the device, and held its
last limit until the firmware reported ``socLimit == 1`` by itself. It does not
start there, and one running from before the device moved claims nothing. With
control switched off a running assist claims nothing either: its claim, its
start and its dashboard card read one predicate, ``full_charge_assist_held_by``.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from dashboard.telemetry import _battery_full_charge_assist_payload
from ems import config as cfg
from ems.controller import FULL_CHARGE_ASSIST_REASON
from test_ac_charge_regulator import IDLE_EXIT, following_mqtt_device
from test_shutdown_ends_the_full_charge_assist import (
    DUE,
    POWER,
    Assist,
    assist_due,
    assist_running,
    charging_item,
)
from test_write_gates import ShellyStub

pytestmark = [
    pytest.mark.unit,
    pytest.mark.simulation,
    pytest.mark.power_control,
]

STATE_FIELDS = {"minSoc", "socSet", "acMode", "inputLimit", "smartMode", "gridOffMode"}
FEEDING = 400
EXPORT = -300
NOT_DUE = "2099-01-01T00:00:00+00:00"


def _state_writes(writes):
    return [written for written in writes if STATE_FIELDS.intersection(written)]


def _output_limits(writes):
    return [written["outputLimit"] for written in writes if set(written) == {"outputLimit"}]


def _record(rig):
    return rig.store.get_device_state("WR1")


class MqttAssist(Assist):
    """The same loop over a real MQTT control client, its write gate open."""

    def device(self, item):
        return following_mqtt_device(
            item, client_options={"telemetry_confirmation_supported": False}
        )

    @contextmanager
    def loop(self, ac_mode=True, **gate_options):
        with (
            patch.object(cfg, "ALLOW_MQTT_LOCAL_CONTROL_WRITES", True),
            super().loop(ac_mode=ac_mode, **gate_options),
        ):
            yield


def _feeding_the_house(tmp_path, rig_class=Assist, state_writes=False):
    """A device the EMS has raised to the house's import, its assist not due yet."""

    store = assist_due(tmp_path / "ems_state.sqlite")
    store.update_device_state("WR1", DUE, next_due_at=NOT_DUE)
    rig = rig_class(tmp_path, store=store)
    rig.controller.shelly = ShellyStub(FEEDING)
    rig.run(cycles=12, state_writes=state_writes)
    assert rig.item.output_limit >= FEEDING
    rig.controller.shelly = ShellyStub(EXPORT)
    return rig


def test_a_due_assist_waits_for_the_state_gate_and_holds_no_output_control(tmp_path):
    rig = _feeding_the_house(tmp_path)
    fed = rig.item.output_limit
    rig.store.update_device_state("WR1", DUE, next_due_at=DUE.isoformat())
    before = len(rig.hardware.writes)

    rig.run(cycles=60, state_writes=False)

    written = rig.written_since(before)
    assert _state_writes(written) == []
    assert _output_limits(written), "the device kept its limit into the export"
    assert rig.item.output_limit < fed
    assert rig.controller.device_output_control_allowed(rig.dev) is True
    record = _record(rig)
    assert record["full_charge_assist_active"] is False
    assert record["next_due_at"] == DUE.isoformat()


def test_a_due_assist_starts_once_the_gate_opens(tmp_path):
    rig = _feeding_the_house(tmp_path)
    rig.store.update_device_state("WR1", DUE, next_due_at=DUE.isoformat())
    rig.run(cycles=6, state_writes=False)
    before = len(rig.hardware.writes)

    rig.run(cycles=2)

    written = rig.written_since(before)
    assert {"socSet": 1000} in written
    assert {"acMode": 1} in written
    assert {"inputLimit": POWER} in written
    assert _record(rig)["full_charge_assist_active"] is True
    assert rig.item.grid_input == POWER


def test_a_running_assist_claims_nothing_behind_a_shut_state_gate(tmp_path):
    """Active in the store, it claims the device again once the gate opens."""

    rig = _feeding_the_house(tmp_path)
    fed = rig.item.output_limit
    rig.store.mark_assist_started("WR1", DUE, True)
    before = len(rig.hardware.writes)

    rig.run(cycles=60, state_writes=False)

    written = rig.written_since(before)
    assert _state_writes(written) == []
    assert rig.item.output_limit < fed
    assert rig.controller.device_output_control_allowed(rig.dev) is True
    assert _record(rig)["full_charge_assist_active"] is True

    before = len(rig.hardware.writes)
    rig.run(cycles=2)

    written = rig.written_since(before)
    assert {"socSet": 1000} in written
    assert {"acMode": 1} in written
    assert rig.controller.device_output_control_allowed(rig.dev) is False


@pytest.mark.parametrize(
    "ac_charge_control", [False, True], ids=["AC charging off", "AC charging on"]
)
def test_a_device_in_the_running_assist_s_ac_input_behind_a_shut_gate(
    tmp_path, ac_charge_control
):
    """From the process before: charging on, or the one exit found after a start.

    Neither is the assist's: behind the shut gate it writes nothing.
    """

    rig = Assist(
        tmp_path,
        item=charging_item(),
        store=assist_running(tmp_path / "ems_state.sqlite"),
    )
    rig.ac_charge_control = ac_charge_control

    rig.run(cycles=24, state_writes=False)

    assert rig.controller.device_output_control_allowed(rig.dev) is True
    if ac_charge_control:
        assert _state_writes(rig.hardware.writes) == [IDLE_EXIT]
        assert rig.item.grid_input == 0
    else:
        assert _state_writes(rig.hardware.writes) == []
        assert rig.item.grid_input == POWER
    record = _record(rig)
    assert record["full_charge_assist_active"] is True
    assert record["ac_mode_restore_pending"] is True


@pytest.mark.parametrize("switch", ["system", "device"])
def test_a_running_assist_claims_nothing_with_control_switched_off(tmp_path, switch):
    """Off is off: its claim reads what its start and its card read."""

    rig = _feeding_the_house(tmp_path, state_writes=True)
    rig.store.mark_assist_started("WR1", DUE, True)
    if switch == "system":
        rig.runtime.system["enabled"] = False
    else:
        rig.runtime.devices["WR1"] = {"enabled": False}
    before = len(rig.hardware.writes)

    rig.run(cycles=3)

    assert _state_writes(rig.written_since(before)) == []
    assert rig.controller.runtime_intents["WR1"].reason != FULL_CHARGE_ASSIST_REASON
    assert _record(rig)["full_charge_assist_active"] is True
    assert _dashboard_assist(rig)["status"] == "waiting"


def _switch(rig, switch, enabled):
    if switch == "system":
        rig.runtime.system["enabled"] = enabled
    else:
        rig.runtime.devices["WR1"] = {"enabled": enabled}


@pytest.mark.parametrize(
    "ac_charge_control", [False, True], ids=["AC charging off", "AC charging on"]
)
@pytest.mark.parametrize("switch", ["system", "device"])
def test_a_device_in_the_running_assist_s_ac_input_after_a_restart_with_control_off(
    tmp_path, switch, ac_charge_control
):
    """As any AC input found after a start: the one exit, or left to the device (R14-1).

    The assist claims nothing while control is off, nor does its restore
    (``test_full_charge_assist_restore_behind_a_shut_gate.py``). Once control
    is back it claims the device again, and its charge is on record.
    """

    rig = Assist(
        tmp_path,
        item=charging_item(),
        store=assist_running(tmp_path / "ems_state.sqlite"),
    )
    rig.ac_charge_control = ac_charge_control
    _switch(rig, switch, False)

    rig.run(cycles=24)

    if ac_charge_control:
        assert _state_writes(rig.hardware.writes) == [IDLE_EXIT]
        assert rig.item.grid_input == 0
    else:
        assert rig.hardware.writes == []
        assert rig.item.grid_input == POWER
    assert _record(rig)["full_charge_assist_active"] is True

    _switch(rig, switch, True)
    rig.run(cycles=2)

    assert rig.item.ac_mode == 1
    assert rig.item.grid_input == POWER
    assert rig.controller.device_output_control_allowed(rig.dev) is False
    with rig.loop():
        assert rig.controller.charges_held_by_ems() == {"WR1": POWER}


def _dashboard_assist(rig, **gate_options):
    with rig.loop(**gate_options):
        return _battery_full_charge_assist_payload(
            rig.controller, rig.dev, rig.item, datetime.now(timezone.utc)
        )


def test_the_dashboard_shows_a_running_assist_behind_a_shut_gate_as_waiting(tmp_path):
    """It claims nothing there; the card said it was helping the device."""

    rig = _feeding_the_house(tmp_path)
    rig.store.mark_assist_started("WR1", DUE, True)
    rig.run(cycles=10, state_writes=False)
    assert rig.controller.device_output_control_allowed(rig.dev) is True

    assert _dashboard_assist(rig, state_writes=False)["status"] == "waiting"
    assert _dashboard_assist(rig)["status"] == "active"


@pytest.mark.parametrize("ac_mode", [True, False], ids=["AC mode on", "AC mode off"])
def test_a_due_assist_does_not_start_on_an_mqtt_control_device(tmp_path, ac_mode):
    """Every gate open: nothing the assist needs can go out over MQTT."""

    rig = _feeding_the_house(tmp_path, MqttAssist, state_writes=True)
    fed = rig.item.output_limit
    rig.store.update_device_state("WR1", DUE, next_due_at=DUE.isoformat())

    rig.run(cycles=60, ac_mode=ac_mode)

    assert rig.item.output_limit < fed, "the device kept its limit into the export"
    assert rig.controller.device_output_control_allowed(rig.dev) is True
    record = _record(rig)
    assert record["full_charge_assist_active"] is False
    assert record["next_due_at"] == DUE.isoformat()
    assert _dashboard_assist(rig)["status"] == "unavailable"


@pytest.mark.parametrize("ac_mode", [True, False], ids=["AC mode on", "AC mode off"])
def test_a_running_assist_claims_nothing_on_an_mqtt_control_device(tmp_path, ac_mode):
    """One started before the device moved to MQTT stays active and holds nothing."""

    rig = _feeding_the_house(tmp_path, MqttAssist, state_writes=True)
    fed = rig.item.output_limit
    rig.store.mark_assist_started("WR1", DUE, ac_mode)

    rig.run(cycles=60, ac_mode=ac_mode)

    assert rig.item.output_limit < fed, "the device kept its limit into the export"
    assert rig.controller.device_output_control_allowed(rig.dev) is True
    assert _record(rig)["full_charge_assist_active"] is True
    assert _dashboard_assist(rig)["status"] == "unavailable"
