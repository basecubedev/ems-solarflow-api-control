# SPDX-License-Identifier: AGPL-3.0-or-later
"""The full-charge state store is written on change, not every cycle (K8b).

``record_observation`` rewrote the device row on every control cycle, the
feature switch was upserted on every cycle, and an active assist cleared an
already-clear request flag on every cycle: about two SQLite writes per device
and cycle, 17,280 a day per device at a 5 s loop, on an appliance's SD card.
The store now writes when a value changes, and refreshes ``last_seen_at``
once per ``LAST_SEEN_REFRESH``; the time a battery was last seen full is kept
in memory while it stays full and written the moment it leaves full, so no
decision reads a coarser value.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from ems.state_store import LAST_SEEN_REFRESH, BatteryFullChargeStateStore
from test_battery_full_charge_assist import (
    configure_assist,
    controller_for,
    device,
    run_controller_once,
    state,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.power_control,
]

LOOP = timedelta(seconds=5)
START = datetime(2026, 6, 1, 13, 0, tzinfo=timezone.utc)


class CountingConnection(sqlite3.Connection):
    """Every connection the store opens, so their ``total_changes`` can be summed."""

    opened = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        CountingConnection.opened.append(self)


def counted_store(path):
    store = BatteryFullChargeStateStore(str(path))
    CountingConnection.opened = []
    store.connect = lambda: sqlite3.connect(store.path, factory=CountingConnection)
    return store


def rows_written():
    return sum(conn.total_changes for conn in CountingConnection.opened)


def stored_row(store, device_name="WR1"):
    """The row as an out-of-process reader (diagnose) sees it."""

    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        return dict(
            conn.execute(
                "SELECT * FROM battery_full_charge_state WHERE device = ?",
                (device_name,),
            ).fetchone()
        )


def cycle(controller, dev, telemetry, now):
    transition = controller.full_charge_assist_feature_transition(now)
    controller.process_battery_full_charge_assist(dev, telemetry, now, transition)
    controller.full_charge_assist_intent(dev)


def test_steady_cycles_write_only_the_last_seen_refresh(tmp_path):
    dev = device()
    store = counted_store(tmp_path / "ems_state.sqlite")
    controller = controller_for(dev, store)
    telemetry = state(soc=85, max_soc=90)
    cycles = 360

    with configure_assist(enabled=True):
        cycle(controller, dev, telemetry, START)
        after_first = rows_written()
        for index in range(1, cycles):
            cycle(controller, dev, telemetry, START + index * LOOP)

    refreshes = (cycles - 1) * LOOP // LAST_SEEN_REFRESH
    assert refreshes == 1
    assert rows_written() - after_first == refreshes


def test_an_active_assist_held_steady_writes_nothing(tmp_path):
    """Through whole control cycles: the request flag, the AC pending flags and the observation."""

    dev = device()
    store = counted_store(tmp_path / "ems_state.sqlite")
    controller = controller_for(dev, store)
    store.update_device_state(
        "WR1",
        START,
        has_battery=True,
        next_due_at=(START + timedelta(days=20)).isoformat(),
        last_full_charge_at=(START - timedelta(days=8)).isoformat(),
    )
    store.mark_assist_started("WR1", START, ac_charge_mode=False)
    store.set_full_charge_feature_enabled_state(True, START)
    telemetry = state(soc=90, max_soc=100)

    with configure_assist(enabled=True, enable_ac_charge_mode=False):
        run_controller_once(controller, telemetry)
        run_controller_once(controller, telemetry)
        after_settled = rows_written()
        for _ in range(100):
            run_controller_once(controller, telemetry)

    assert store.get_device_state("WR1")["full_charge_assist_active"] is True
    assert store.get_device_state("WR1")["max_soc_request_pending"] is False
    assert rows_written() == after_settled


def test_a_changed_observation_is_written_at_once(tmp_path):
    dev = device()
    store = counted_store(tmp_path / "ems_state.sqlite")
    controller = controller_for(dev, store)

    with configure_assist(enabled=True):
        cycle(controller, dev, state(soc=85, max_soc=90), START)
        before = rows_written()
        cycle(controller, dev, state(soc=86, max_soc=90), START + LOOP)

    assert rows_written() == before + 1
    assert stored_row(store)["last_seen_soc"] == 86
    assert stored_row(store)["last_seen_at"] == (START + LOOP).isoformat()


def test_the_feature_switch_is_written_when_it_changes(tmp_path):
    store = counted_store(tmp_path / "ems_state.sqlite")

    for index in range(5):
        store.set_full_charge_feature_enabled_state(True, START + index * LOOP)
    once = rows_written()
    store.set_full_charge_feature_enabled_state(False, START + 6 * LOOP)

    assert once == 1
    assert rows_written() == 2
    assert store.get_full_charge_feature_enabled_state() is False


def test_the_last_full_charge_is_exact_when_the_battery_leaves_full(tmp_path):
    """Held in memory while full, written as the last full cycle once it leaves."""

    dev = device()
    store = counted_store(tmp_path / "ems_state.sqlite")
    controller = controller_for(dev, store)
    full = state(soc=100, max_soc=100, soc_limit=1)
    last_full = START + 100 * LOOP

    with configure_assist(enabled=True):
        cycle(controller, dev, full, START)
        written_while_full = rows_written()
        for index in range(1, 101):
            cycle(controller, dev, full, START + index * LOOP)
        assert rows_written() == written_while_full
        assert store.get_device_state("WR1")["last_full_charge_at"] == last_full.isoformat()
        assert stored_row(store)["last_full_charge_at"] == START.isoformat()

        cycle(controller, dev, state(soc=99, max_soc=100, soc_limit=0), last_full + LOOP)

    row = stored_row(store)
    assert row["last_full_charge_at"] == last_full.isoformat()
    assert row["next_due_at"] == (last_full + timedelta(days=28)).isoformat()
    assert row["last_seen_soc_limit"] == 0


def test_a_flag_write_carries_the_observation_held_in_memory(tmp_path):
    store = counted_store(tmp_path / "ems_state.sqlite")
    full = state(soc=100, max_soc=100, soc_limit=1)
    store.record_observation("WR1", full, True, START, 28)
    store.record_observation("WR1", full, True, START + LOOP, 28)

    store.update_device_state("WR1", START + 2 * LOOP, restore_pending=True)

    row = stored_row(store)
    assert row["restore_pending"] == 1
    assert row["last_seen_at"] == (START + LOOP).isoformat()
    assert row["last_full_charge_at"] == (START + LOOP).isoformat()
