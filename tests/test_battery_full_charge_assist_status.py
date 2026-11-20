# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from dashboard.telemetry import build_dashboard_snapshot
from ems.diagnostics import (
    diagnose_battery_full_charge_assist_report,
    diagnose_control_snapshot,
)
from ems.state_store import BatteryFullChargeStateStore, describe_full_charge_assist_status

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.contract,
]


NOW = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
CONFIG = {"assist_window_days": 7}
HELD_BY_GATE = "state_writes_not_allowed"
HELD_BY_TRANSPORT = "transport_state_reconciliation_unsupported"


def test_disabled_status_when_feature_not_enabled():
    result = describe_full_charge_assist_status(CONFIG, False, True, None, NOW)
    assert result["status"] == "disabled"
    assert result["inside_assist_window"] is False


def test_ignored_no_battery_status():
    result = describe_full_charge_assist_status(CONFIG, True, False, None, NOW)
    assert result["status"] == "ignored_no_battery"


def test_unknown_status_when_record_missing():
    result = describe_full_charge_assist_status(CONFIG, True, True, None, NOW)
    assert result["status"] == "unknown"


def test_active_status():
    record = {
        "full_charge_assist_active": True,
        "restore_pending": True,
        "ac_mode_restore_pending": True,
        "next_due_at": (NOW + timedelta(days=2)).isoformat(),
        "last_seen_soc_limit": 0,
    }
    result = describe_full_charge_assist_status(CONFIG, True, True, record, NOW)
    assert result["status"] == "active"
    assert result["inside_assist_window"] is True
    assert result["days_until_due"] == 2


def test_restore_pending_status_takes_priority_over_window():
    record = {
        "full_charge_assist_active": False,
        "restore_pending": True,
        "ac_mode_restore_pending": False,
        "next_due_at": (NOW + timedelta(days=2)).isoformat(),
        "last_seen_soc_limit": 0,
    }
    result = describe_full_charge_assist_status(CONFIG, True, True, record, NOW)
    assert result["status"] == "restore_pending"


def test_completed_status_from_last_seen_soc_limit():
    record = {
        "full_charge_assist_active": False,
        "restore_pending": False,
        "ac_mode_restore_pending": False,
        "next_due_at": (NOW + timedelta(days=20)).isoformat(),
        "last_seen_soc_limit": 1,
    }
    result = describe_full_charge_assist_status(CONFIG, True, True, record, NOW)
    assert result["status"] == "completed"


def test_overdue_status_when_due_date_in_past():
    record = {
        "full_charge_assist_active": False,
        "restore_pending": False,
        "ac_mode_restore_pending": False,
        "next_due_at": (NOW - timedelta(days=1)).isoformat(),
        "last_seen_soc_limit": 0,
    }
    result = describe_full_charge_assist_status(CONFIG, True, True, record, NOW)
    assert result["status"] == "overdue"


def test_inside_assist_window_true_within_window_days():
    record = {
        "full_charge_assist_active": False,
        "restore_pending": False,
        "ac_mode_restore_pending": False,
        "next_due_at": (NOW + timedelta(days=3)).isoformat(),
        "last_seen_soc_limit": 0,
    }
    result = describe_full_charge_assist_status(CONFIG, True, True, record, NOW)
    assert result["status"] == "window"
    assert result["inside_assist_window"] is True
    assert result["days_until_due"] == 3


def test_inside_assist_window_false_outside_window_days():
    record = {
        "full_charge_assist_active": False,
        "restore_pending": False,
        "ac_mode_restore_pending": False,
        "next_due_at": (NOW + timedelta(days=20)).isoformat(),
        "last_seen_soc_limit": 0,
    }
    result = describe_full_charge_assist_status(CONFIG, True, True, record, NOW)
    assert result["status"] == "ok"
    assert result["inside_assist_window"] is False
    assert result["days_until_due"] == 20


def _record(active, next_due_at):
    return {
        "full_charge_assist_active": active,
        "restore_pending": active,
        "ac_mode_restore_pending": False,
        "next_due_at": next_due_at.isoformat(),
        "last_seen_soc_limit": 0,
    }


def test_an_active_assist_that_can_write_nothing_is_waiting_not_active():
    """Behind a shut state gate it claims nothing (K25), so it is not running."""

    record = _record(True, NOW + timedelta(days=2))

    result = describe_full_charge_assist_status(
        CONFIG, True, True, record, NOW, held_by=HELD_BY_GATE
    )

    assert result["status"] == "waiting"
    assert "state writes" in result["message"]


def test_an_active_assist_over_a_report_without_its_fields_waits_for_one():
    """It claims nothing while the report lacks a field it still has to write."""

    record = _record(True, NOW + timedelta(days=2))

    result = describe_full_charge_assist_status(
        CONFIG, True, True, record, NOW, held_by="fields_not_reported"
    )

    assert result["status"] == "waiting"
    assert "report" in result["message"]
    assert "state writes" not in result["message"]


def test_a_due_assist_behind_a_shut_gate_stays_overdue():
    """It starts in the first cycle after the gate opens."""

    record = _record(False, NOW - timedelta(days=1))

    result = describe_full_charge_assist_status(
        CONFIG, True, True, record, NOW, held_by=HELD_BY_GATE
    )

    assert result["status"] == "overdue"


@pytest.mark.parametrize("active", [False, True], ids=["due", "from before the move"])
def test_an_assist_on_a_transport_without_state_writes_is_unavailable(active):
    """Over MQTT control it never starts, and one from before claims nothing."""

    record = _record(active, NOW - timedelta(days=3))

    result = describe_full_charge_assist_status(
        CONFIG, True, True, record, NOW, held_by=HELD_BY_TRANSPORT
    )

    assert result["status"] == "unavailable"
    assert "MQTT" in result["message"]


class AssistStoreStub:
    def __init__(self, record):
        self.record = record

    def get_device_state(self, device, now=None):
        return self.record


def _controller(*, enabled, has_battery, record, raise_error=False):
    if raise_error:
        def boom(*args, **kwargs):
            raise RuntimeError("legacy deployment without assist support")

        return SimpleNamespace(
            devices=[SimpleNamespace(name="WR1")],
            runtime_state=None,
            device_online={"WR1": True},
            commanded_total_w=0,
            filtered_load_w=0,
            _dashboard_capabilities=[],
            full_charge_assist_config=boom,
        )

    return SimpleNamespace(
        devices=[SimpleNamespace(name="WR1")],
        runtime_state=None,
        device_online={"WR1": True},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
        full_charge_assist_config=lambda: {"assist_window_days": 7},
        full_charge_assist_enabled=lambda: enabled,
        full_charge_assist_has_battery=lambda dev, state: has_battery,
        full_charge_assist_held_by=lambda dev: None,
        battery_full_charge_store=AssistStoreStub(record),
    )


def _state(**overrides):
    defaults = dict(
        solar=500,
        output=700,
        pack_out=220,
        pack_in=20,
        soc=64,
        soc_limit=0,
        ac_mode=2,
        ac_status=1,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _snapshot_assist(controller, state):
    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=[state],
        targets=[700],
        effective_targets=[700],
        allocated_total_w=700,
        effective_total_w=700,
        enabled=True,
        max_total_power=700,
        min_output_limit=35,
    )
    return snapshot["devices"]["WR1"]["battery_full_charge_assist"]


def test_api_includes_battery_full_charge_assist_when_available():
    controller = _controller(
        enabled=True,
        has_battery=True,
        record={
            "full_charge_assist_active": True,
            "restore_pending": True,
            "ac_mode_restore_pending": True,
            "assist_started_at": "2026-06-14T10:00:00+02:00",
            "last_full_charge_at": None,
            "next_due_at": "2026-06-16T14:00:00+02:00",
            "last_seen_soc_limit": 0,
        },
    )
    assist = _snapshot_assist(controller, _state(soc_limit=0, ac_mode=1, ac_status=2))

    assert assist["enabled"] is True
    assert assist["has_battery"] is True
    assert assist["status"] == "active"
    assert assist["restore_pending"] is True
    assert assist["ac_mode_restore_pending"] is True
    assert assist["soc_limit"] == 0
    assert assist["ac_mode"] == 1
    assert assist["ac_status"] == 2
    assert assist["assist_started_at"] == "2026-06-14T10:00:00+02:00"


def test_devices_without_battery_are_marked_ignored():
    controller = _controller(enabled=True, has_battery=False, record=None)
    assist = _snapshot_assist(controller, _state())

    assert assist["status"] == "ignored_no_battery"
    assert assist["has_battery"] is False


def test_missing_assist_state_does_not_break_device_status_api(caplog):
    controller = _controller(
        enabled=True, has_battery=True, record=None, raise_error=True
    )
    with caplog.at_level(logging.DEBUG, logger="root"):
        assist = _snapshot_assist(controller, _state())

    assert assist["status"] == "unknown"
    assert assist["enabled"] is False
    # The real cause must be recorded at DEBUG instead of vanishing silently.
    assert any(
        "event=dashboard_assist_payload_unavailable" in record.message
        and record.levelno == logging.DEBUG
        for record in caplog.records
    )


def test_disabled_feature_serializes_disabled_status():
    controller = _controller(enabled=False, has_battery=True, record=None)
    assist = _snapshot_assist(controller, _state())

    assert assist["status"] == "disabled"


class MultiAssistStoreStub:
    def __init__(self, records):
        self.records = records

    def get_device_state(self, device, now=None):
        return self.records.get(device)


def _multi_controller(records, held_by=None):
    names = list(records)
    held_by = held_by or {}
    return SimpleNamespace(
        devices=[SimpleNamespace(name=name) for name in names],
        runtime_state=None,
        device_online={name: True for name in names},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
        full_charge_assist_config=lambda: {"assist_window_days": 7},
        full_charge_assist_enabled=lambda: True,
        full_charge_assist_has_battery=lambda dev, state: True,
        full_charge_assist_held_by=lambda dev: held_by.get(dev.name),
        battery_full_charge_store=MultiAssistStoreStub(records),
    )


def _full_snapshot(controller, states):
    count = len(states)
    return build_dashboard_snapshot(
        controller,
        load_w=0,
        states=states,
        targets=[0] * count,
        effective_targets=[0] * count,
        allocated_total_w=0,
        effective_total_w=0,
        enabled=True,
        max_total_power=700,
        min_output_limit=35,
    )


def _active_assist_record():
    return {
        "full_charge_assist_active": True,
        "restore_pending": True,
        "ac_mode_restore_pending": True,
        "next_due_at": (NOW + timedelta(days=2)).isoformat(),
        "last_seen_soc_limit": 0,
    }


def _idle_assist_record():
    return {
        "full_charge_assist_active": False,
        "restore_pending": False,
        "ac_mode_restore_pending": False,
        "next_due_at": (NOW + timedelta(days=20)).isoformat(),
        "last_seen_soc_limit": 0,
    }


def test_snapshot_exposes_firmware_status_fields():
    controller = _multi_controller({"WR1": _idle_assist_record()})
    snapshot = _full_snapshot(
        controller,
        [_state(soc_status=1, pack_num=2, input_limit_w=300)],
    )

    device = snapshot["devices"]["WR1"]
    assert device["soc_status"] == 1
    assert device["pack_num"] == 2
    assert device["input_limit_w"] == 300


def test_full_charge_assist_rule_active_when_device_assisting():
    controller = _multi_controller({"WR1": _active_assist_record()})
    snapshot = _full_snapshot(controller, [_state()])

    rule = snapshot["rules"]["full_charge_assist_active"]
    assert rule["active"] is True
    assert "WR1" in rule["reason"]


def test_full_charge_assist_rule_inactive_without_active_device():
    controller = _multi_controller({"WR1": _idle_assist_record()})
    snapshot = _full_snapshot(controller, [_state()])

    rule = snapshot["rules"]["full_charge_assist_active"]
    assert rule["active"] is False
    assert rule["reason"] == "no device currently in full-charge assist"


def test_full_charge_assist_rule_lists_all_active_devices():
    controller = _multi_controller(
        {
            "WR1": _active_assist_record(),
            "WR2": _idle_assist_record(),
            "WR3": _active_assist_record(),
        }
    )
    snapshot = _full_snapshot(controller, [_state(), _state(), _state()])

    rule = snapshot["rules"]["full_charge_assist_active"]
    assert rule["active"] is True
    assert "WR1" in rule["reason"]
    assert "WR3" in rule["reason"]
    assert "WR2" not in rule["reason"]


def test_full_charge_assist_rule_leaves_out_an_assist_that_can_write_nothing():
    controller = _multi_controller(
        {"WR1": _active_assist_record(), "WR2": _active_assist_record()},
        held_by={"WR1": HELD_BY_GATE},
    )
    snapshot = _full_snapshot(controller, [_state(), _state()])

    assert snapshot["devices"]["WR1"]["battery_full_charge_assist"]["status"] == "waiting"
    rule = snapshot["rules"]["full_charge_assist_active"]
    assert rule["active"] is True
    assert "WR2" in rule["reason"]
    assert "WR1" not in rule["reason"]


def _diagnose_config(tmp_path, system, *devices):
    return {
        "system": system,
        "devices": list(devices),
        "grid_meter": {"type": "shelly", "ip": "10.1.2.4"},
        "battery_full_charge_assist": {
            "enabled": True,
            "assist_window_days": 7,
            "state_database_path": str(tmp_path / "ems_state.sqlite"),
        },
    }


def _stored(tmp_path, active, *names):
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    for name in names:
        store.update_device_state(
            name,
            NOW,
            has_battery=True,
            next_due_at=(NOW - timedelta(days=3)).isoformat(),
        )
        if active:
            store.mark_assist_started(name, NOW, True)


def _diagnose_status(config, runtime=None):
    report = diagnose_battery_full_charge_assist_report(config, runtime or {})
    return {item["device"]: item["status"] for item in report["devices"]}


OPEN_GATES = {
    "enabled": True,
    "dry_run": False,
    "allow_state_reconciliation_writes": True,
}
API_DEVICE = {"name": "WR1", "ip": "10.1.2.3", "sn": "HOA1NAH9A123456"}
MQTT_DEVICE = {
    "name": "WR2",
    "type": "zendure_mqtt",
    "capabilities": {"write_output_limit": True},
}


@pytest.mark.parametrize("active", [False, True], ids=["due", "from before the move"])
def test_diagnose_names_an_mqtt_device_the_assist_never_runs_on(tmp_path, active):
    _stored(tmp_path, active, "WR1", "WR2")

    status = _diagnose_status(
        _diagnose_config(tmp_path, OPEN_GATES, API_DEVICE, MQTT_DEVICE)
    )

    assert status["WR1"] == ("active" if active else "overdue")
    assert status["WR2"] == "not available over MQTT"


@pytest.mark.parametrize(
    "shut",
    [
        {"allow_state_reconciliation_writes": False},
        {"dry_run": True},
        {"simulation_mode": True},
        {"allow_hardware_writes": False},
        {"enabled": False},
    ],
    ids=["state writes off", "dry run", "simulation", "transport gate", "control off"],
)
def test_diagnose_names_an_active_assist_its_config_keeps_from_writing(tmp_path, shut):
    """The gates the EMS's state write gate reads, and control switched off (off is off)."""

    _stored(tmp_path, True, "WR1")

    status = _diagnose_status(
        _diagnose_config(tmp_path, {**OPEN_GATES, **shut}, API_DEVICE)
    )

    assert status["WR1"] == "active, waiting for state writes"


SWITCHES = {
    "stored off, runtime on": ({"enabled": False}, {"system": {"enabled": True}}, True, True),
    "runtime off": ({}, {"system": {"enabled": False}}, False, False),
    "runtime off as text": ({}, {"system": {"enabled": "off"}}, False, False),
    "device switched off": ({}, {"devices": {"WR1": {"enabled": False}}}, True, False),
    "device switched on": ({}, {"devices": {"WR1": {"enabled": True}}}, True, True),
}


@pytest.mark.parametrize(
    "stored,runtime,system_on,writes", SWITCHES.values(), ids=SWITCHES.keys()
)
def test_diagnose_reads_control_switched_off_as_the_ems_does(
    tmp_path, stored, runtime, system_on, writes
):
    """The system's and the device's ``enabled``, runtime-state over the stored config (R14-2).

    Read from the stored config alone, an assist the EMS was running read as
    waiting, and one it held behind control switched off at runtime as active.
    The control snapshot reads ``system.enabled`` the same way.
    """

    _stored(tmp_path, True, "WR1")
    config = _diagnose_config(tmp_path, {**OPEN_GATES, **stored}, API_DEVICE)

    status = _diagnose_status(config, runtime)

    assert status["WR1"] == ("active" if writes else "active, waiting for state writes")
    snapshot = diagnose_control_snapshot(config, runtime, {}, "runtime-state.json")
    assert snapshot["control_enabled"] is system_on


def test_dashboard_rules_contract_includes_expected_keys():
    controller = _multi_controller({"WR1": _idle_assist_record()})
    snapshot = _full_snapshot(controller, [_state()])

    # These keys back the fixed rule list the dashboard renders (RULES in the
    # Modern status.js).
    # A backend rename/removal would silently degrade the GUI to "inactive",
    # so keep the contract explicit here.
    expected = {
        "ems_enabled",
        "soc_limit_active",
        "output_limit_active",
        "winter_soc_mode",
        "battery_care_active",
        "full_charge_assist_active",
        "pv_priority_balancing",
        "battery_balancing",
        "night_min_soc_idle",
        "offline_devices",
    }
    rules = snapshot["rules"]
    assert expected <= set(rules)
    for key in expected:
        rule = rules[key]
        assert set(rule) == {"active", "reason"}
        assert isinstance(rule["active"], bool)
        assert isinstance(rule["reason"], str)
