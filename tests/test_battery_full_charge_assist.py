# SPDX-License-Identifier: AGPL-3.0-or-later
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ems import config as cfg
from ems.clients import parse_device
from ems.controller import (
    EMSController,
)
from ems.models import DeviceState
from ems.state_store import BatteryFullChargeStateStore


def device(name="WR1", max_soc=90):
    return SimpleNamespace(
        name=name,
        ip="127.0.0.1",
        sn=f"{name}-SN",
        session=Mock(),
        max_power=800,
        pv_kwp=1.0,
        pv_priority_factor=1.0,
        battery_kwh=1.0,
        min_soc=15,
        max_soc=max_soc,
        smart_mode=1,
        grid_off_mode=None,
    )


def state(
    soc=85,
    min_soc=15,
    max_soc=90,
    soc_limit=0,
    ac_mode=2,
    ac_status=1,
    input_limit_w=0,
    pack_num=1,
    soc_status=0,
    battery_calibration_time=1234,
):
    return DeviceState(
        soc=soc,
        min_soc=min_soc,
        max_soc=max_soc,
        solar=500,
        output=0,
        pack_in=0,
        pack_out=0,
        temp=20,
        voltage=48,
        rssi=0,
        remain_minutes=0,
        solar1=0,
        solar2=0,
        solar3=0,
        solar4=0,
        output_limit=0,
        soc_limit=soc_limit,
        pack_state=2,
        fault_level=0,
        smart_mode=1,
        grid_off_mode=0,
        ac_mode=ac_mode,
        ac_status=ac_status,
        dc_status=1,
        grid_state=1,
        input_limit_w=input_limit_w,
        pack_num=pack_num,
        soc_status=soc_status,
        battery_calibration_time=battery_calibration_time,
    )


class ShellyStub:
    def __init__(self, power=500):
        self.power = power

    def get_power(self):
        return self.power


def configure_assist(**overrides):
    config = {
        **cfg.BATTERY_FULL_CHARGE_ASSIST_DEFAULTS,
        "enabled": True,
        **overrides,
    }
    return patch.object(cfg, "BATTERY_FULL_CHARGE_ASSIST_CONFIG", config)


def controller_for(dev, store):
    controller = EMSController(
        devices=[dev],
        shelly=ShellyStub(),
        sleep_enabled=False,
        runtime_state=None,
        battery_full_charge_store=store,
    )
    controller.set_output_limit = Mock()
    return controller


def run_controller_once(controller, telemetry, writes_allowed=True):
    with patch(
        "ems.controller.fetch_all_devices",
        return_value=[telemetry],
    ), patch(
        "ems.controller.cfg.SYSTEM_ENABLED",
        True,
    ), patch(
        "ems.controller.cfg.MAX_TOTAL_POWER",
        800,
    ), patch(
        "ems.controller.cfg.MAX_DEVICE_POWER",
        800,
    ), patch(
        "ems.controller.cfg.MIN_OUTPUT_LIMIT",
        0,
    ), patch(
        "ems.controller.cfg.LOOP_INTERVAL",
        5,
    ), patch(
        "ems.controller.cfg.DEADBAND",
        10,
    ), patch(
        "ems.controller.cfg.SOC_RECONCILE_INTERVAL",
        10,
    ), patch(
        "ems.controller.cfg.SIMULATION_MODE",
        False,
    ), patch(
        "ems.controller.cfg.ARGS",
        SimpleNamespace(replay=None),
    ), patch(
        "ems.controller.cfg.state_reconciliation_writes_allowed",
        return_value=writes_allowed,
    ):
        controller.run_once()


def posted_properties(dev):
    return [
        call.kwargs["json"]["properties"]
        for call in dev.session.post.call_args_list
    ]


def event_types(store):
    with store.connect() as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT event_type FROM battery_full_charge_events ORDER BY id"
            ).fetchall()
        ]


def test_config_defaults_enable_full_charge_assist():
    safe = cfg.default_safe_config()
    assist = safe["battery_full_charge_assist"]

    assert assist["enabled"] is True
    assert assist["interval_days"] == 28
    assert assist["state_database_path"] == "data/ems_state.sqlite"


def test_config_normalization_rejects_invalid_force_time():
    with pytest.raises(ValueError):
        cfg.normalize_battery_full_charge_assist_config({
            "enabled": True,
            "force_time": "25:99",
        })


def test_config_normalization_clamps_safe_numeric_values():
    assist = cfg.normalize_battery_full_charge_assist_config({
        "interval_days": -5,
        "assist_window_days": -1,
        "assist_start_soc": 150,
        "ac_charge_power": -10,
    })

    assert assist["enabled"] is True
    assert assist["interval_days"] == 1
    assert assist["assist_window_days"] == 0
    assert assist["assist_start_soc"] == 100
    assert assist["ac_charge_power"] == 0


def test_parse_device_reads_battery_full_charge_diagnostics():
    parsed = parse_device({
        "properties": {
            "electricLevel": 88,
            "minSoc": 150,
            "socSet": 900,
            "socLimit": 1,
            "inputLimit": 200,
            "packNum": 2,
            "socStatus": 3,
            "batCalTime": 1234,
        }
    })

    assert parsed.soc == 88
    assert parsed.max_soc == 90
    assert parsed.soc_limit == 1
    assert parsed.input_limit_w == 200
    assert parsed.pack_num == 2
    assert parsed.soc_status == 3
    assert parsed.battery_calibration_time == 1234


def test_passive_tracking_updates_last_full_charge_from_soc_limit(tmp_path):
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    now = datetime(2026, 6, 1, 13, 42, tzinfo=timezone.utc)

    record = store.record_observation(
        "WR1",
        state(soc=99, max_soc=90, soc_limit=1),
        True,
        now,
        interval_days=28,
    )

    assert record["has_battery"] is True
    assert record["last_full_charge_at"] == now.isoformat()
    assert record["next_due_at"] == (now + timedelta(days=28)).isoformat()
    assert record["last_seen_pack_num"] == 1
    assert record["last_seen_soc_status"] == 0
    assert record["last_seen_battery_calibration_time"] == 1234


def test_pack_num_zero_is_ignored_and_does_not_write(tmp_path):
    dev = device()
    dev.session.post.return_value = SimpleNamespace(status_code=200)
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    controller = controller_for(dev, store)

    with configure_assist(enabled=True):
        run_controller_once(controller, state(pack_num=0, soc=100), writes_allowed=True)

    dev.session.post.assert_not_called()
    record = store.get_device_state("WR1")
    assert record["has_battery"] is False
    assert record["full_charge_assist_active"] is False


def test_disabled_feature_tracks_passively_but_does_not_write(tmp_path):
    dev = device()
    dev.session.post.return_value = SimpleNamespace(status_code=200)
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    controller = controller_for(dev, store)

    with configure_assist(enabled=False):
        run_controller_once(controller, state(soc=95, soc_limit=1), writes_allowed=True)

    dev.session.post.assert_not_called()
    record = store.get_device_state("WR1")
    assert record["last_full_charge_at"] is not None
    assert record["full_charge_assist_active"] is False


def test_first_enable_seeds_schedule_and_does_not_charge(tmp_path):
    dev = device(max_soc=90)
    dev.session.post.return_value = SimpleNamespace(status_code=200)
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    controller = controller_for(dev, store)

    with configure_assist(enabled=True, assist_start_soc=80):
        run_controller_once(
            controller,
            state(soc=95, max_soc=90, soc_limit=0, ac_mode=2),
            writes_allowed=True,
        )

    dev.session.post.assert_not_called()
    record = store.get_device_state("WR1")
    assert record["full_charge_assist_active"] is False
    assert record["restore_pending"] is False
    assert record["ac_mode_restore_pending"] is False
    assert record["last_full_charge_at"] is not None
    assert record["next_due_at"] is not None
    assert (
        datetime.fromisoformat(record["next_due_at"])
        - datetime.fromisoformat(record["last_full_charge_at"])
    ) == timedelta(days=28)
    assert "full_charge_assist_initial_state_seeded" in event_types(store)


def test_reenable_with_old_overdue_schedule_seeds_today_and_does_not_charge(tmp_path):
    dev = device(max_soc=90)
    dev.session.post.return_value = SimpleNamespace(status_code=200)
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    old = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
    store.set_full_charge_feature_enabled_state(False, old)
    store.update_device_state(
        "WR1",
        old,
        has_battery=True,
        last_full_charge_at=old.isoformat(),
        next_due_at=(old + timedelta(days=28)).isoformat(),
    )
    controller = controller_for(dev, store)

    with configure_assist(enabled=True, assist_start_soc=80):
        run_controller_once(
            controller,
            state(soc=95, max_soc=90, soc_limit=0, ac_mode=2),
            writes_allowed=True,
        )

    dev.session.post.assert_not_called()
    record = store.get_device_state("WR1")
    assert datetime.fromisoformat(record["last_full_charge_at"]) > old
    assert (
        datetime.fromisoformat(record["next_due_at"])
        - datetime.fromisoformat(record["last_full_charge_at"])
    ) == timedelta(days=28)
    assert record["full_charge_assist_active"] is False
    assert "full_charge_assist_reenabled_schedule_seeded" in event_types(store)


def test_reenable_does_not_overwrite_pending_restore(tmp_path):
    dev = device(max_soc=95)
    dev.session.post.return_value = SimpleNamespace(status_code=200)
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    old = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
    store.set_full_charge_feature_enabled_state(False, old)
    store.update_device_state(
        "WR1",
        old,
        has_battery=True,
        last_full_charge_at=old.isoformat(),
        next_due_at=(old + timedelta(days=28)).isoformat(),
        restore_pending=True,
        max_soc_request_pending=True,
    )
    controller = controller_for(dev, store)

    with configure_assist(enabled=True):
        run_controller_once(
            controller,
            state(soc=95, max_soc=100, soc_limit=0, ac_mode=2),
            writes_allowed=True,
        )

    assert {"minSoc": 150, "socSet": 950} in posted_properties(dev)
    record = store.get_device_state("WR1")
    assert record["last_full_charge_at"] == old.isoformat()
    assert "full_charge_assist_reenabled_schedule_seeded" not in event_types(store)


def test_disabled_state_is_persisted_and_does_not_start_new_assist(tmp_path):
    dev = device(max_soc=90)
    dev.session.post.return_value = SimpleNamespace(status_code=200)
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    old = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
    store.set_full_charge_feature_enabled_state(True, old)
    store.update_device_state(
        "WR1",
        old,
        has_battery=True,
        next_due_at=old.isoformat(),
    )
    controller = controller_for(dev, store)

    with configure_assist(enabled=False):
        run_controller_once(
            controller,
            state(soc=95, max_soc=90, soc_limit=0, ac_mode=2),
            writes_allowed=True,
        )

    dev.session.post.assert_not_called()
    assert store.get_full_charge_feature_enabled_state() is False
    assert store.get_device_state("WR1")["full_charge_assist_active"] is False


def test_second_loop_after_first_enable_seed_does_not_start_before_window(tmp_path):
    dev = device(max_soc=90)
    dev.session.post.return_value = SimpleNamespace(status_code=200)
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    controller = controller_for(dev, store)

    with configure_assist(enabled=True, assist_start_soc=80):
        run_controller_once(
            controller,
            state(soc=95, max_soc=90, soc_limit=0, ac_mode=2),
            writes_allowed=True,
        )
        run_controller_once(
            controller,
            state(soc=95, max_soc=90, soc_limit=0, ac_mode=2),
            writes_allowed=True,
        )

    dev.session.post.assert_not_called()
    assert store.get_device_state("WR1")["full_charge_assist_active"] is False


def test_assist_window_start_writes_socset_acmode_and_input_limit_same_loop(tmp_path):
    dev = device(max_soc=90)
    dev.session.post.return_value = SimpleNamespace(status_code=200)
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    store.set_full_charge_feature_enabled_state(
        True,
        datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    )
    store.update_device_state(
        "WR1",
        datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        has_battery=True,
        next_due_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc).isoformat(),
    )
    controller = controller_for(dev, store)

    with configure_assist(
        enabled=True,
        assist_start_soc=80,
        enable_ac_charge_mode=True,
        ac_charge_power=200,
    ):
        run_controller_once(
            controller,
            state(soc=85, max_soc=90, ac_mode=2, input_limit_w=0),
            writes_allowed=True,
        )

    properties = posted_properties(dev)
    assert {"minSoc": 150, "socSet": 1000} in properties
    assert {"acMode": 1} in properties
    assert {"inputLimit": 200} in properties
    assert controller.set_output_limit.call_count == 0

    record = store.get_device_state("WR1")
    assert record["full_charge_assist_active"] is True
    assert record["restore_pending"] is True
    assert record["max_soc_request_pending"] is False
    assert record["ac_input_request_pending"] is False
    assert record["ac_mode_restore_pending"] is True


def test_assist_window_below_start_soc_does_not_start(tmp_path):
    dev = device(max_soc=90)
    dev.session.post.return_value = SimpleNamespace(status_code=200)
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    store.set_full_charge_feature_enabled_state(
        True,
        datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    )
    controller = controller_for(dev, store)

    with configure_assist(enabled=True, assist_start_soc=80):
        run_controller_once(controller, state(soc=70), writes_allowed=True)

    dev.session.post.assert_not_called()
    assert store.get_device_state("WR1")["full_charge_assist_active"] is False


def test_force_time_starts_even_below_start_soc(tmp_path):
    dev = device()
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    controller = controller_for(dev, store)
    now = datetime(2026, 6, 14, 14, 0, tzinfo=timezone.utc)
    record = store.update_device_state(
        "WR1",
        now,
        has_battery=True,
        next_due_at=now.isoformat(),
    )

    with configure_assist(enabled=True, assist_start_soc=80, force_time="14:00"):
        start, event_type = controller.should_start_full_charge_assist(
            record,
            state(soc=40),
            now,
        )

    assert start is True
    assert event_type == "full_charge_assist_forced"


def test_existing_due_schedule_starts_when_already_enabled(tmp_path):
    dev = device(max_soc=90)
    dev.session.post.return_value = SimpleNamespace(status_code=200)
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    old = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
    store.set_full_charge_feature_enabled_state(True, old)
    store.update_device_state(
        "WR1",
        old,
        has_battery=True,
        next_due_at=old.isoformat(),
    )
    controller = controller_for(dev, store)

    with configure_assist(enabled=True, assist_start_soc=80):
        run_controller_once(
            controller,
            state(soc=95, max_soc=90, soc_limit=0, ac_mode=2),
            writes_allowed=True,
        )

    assert {"minSoc": 150, "socSet": 1000} in posted_properties(dev)
    assert store.get_device_state("WR1")["full_charge_assist_active"] is True


def test_force_start_works_after_seeded_due_date(tmp_path):
    dev = device(max_soc=90)
    dev.session.post.return_value = SimpleNamespace(status_code=200)
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    due = datetime.now().astimezone().replace(
        hour=13,
        minute=0,
        second=0,
        microsecond=0
    )
    store.set_full_charge_feature_enabled_state(True, due - timedelta(days=28))
    store.update_device_state(
        "WR1",
        due,
        has_battery=True,
        last_full_charge_at=(due - timedelta(days=28)).isoformat(),
        next_due_at=due.isoformat(),
    )
    controller = controller_for(dev, store)

    with configure_assist(enabled=True, assist_start_soc=80, force_time="00:00"):
        run_controller_once(
            controller,
            state(soc=40, max_soc=90, soc_limit=0, ac_mode=2),
            writes_allowed=True,
        )

    assert {"minSoc": 150, "socSet": 1000} in posted_properties(dev)
    assert store.get_device_state("WR1")["full_charge_assist_active"] is True


def test_active_assist_completion_uses_only_soc_limit_and_restores_config_max_soc(tmp_path):
    dev = device(max_soc=95)
    dev.session.post.return_value = SimpleNamespace(status_code=200)
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    store.mark_assist_started("WR1", now, ac_charge_mode=False)
    controller = controller_for(dev, store)

    with configure_assist(enabled=True, enable_ac_charge_mode=False):
        run_controller_once(
            controller,
            state(soc=97, max_soc=100, soc_limit=1),
            writes_allowed=True,
        )

    properties = posted_properties(dev)
    assert {"minSoc": 150, "socSet": 950} in properties
    assert all("batCalTime" not in item for item in properties)
    record = store.get_device_state("WR1")
    assert record["full_charge_assist_active"] is False
    assert record["restore_pending"] is False
    assert record["last_full_charge_at"] is not None


def test_active_assist_completion_still_runs_when_feature_disabled(tmp_path):
    dev = device(max_soc=95)
    dev.session.post.return_value = SimpleNamespace(status_code=200)
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    store.mark_assist_started("WR1", now, ac_charge_mode=False)
    controller = controller_for(dev, store)

    with configure_assist(enabled=False, enable_ac_charge_mode=False):
        run_controller_once(
            controller,
            state(soc=50, max_soc=80, soc_limit=1),
            writes_allowed=True,
        )

    assert {"minSoc": 150, "socSet": 950} in posted_properties(dev)
    record = store.get_device_state("WR1")
    assert record["full_charge_assist_active"] is False
    assert record["restore_pending"] is False
    assert record["last_full_charge_at"] is not None


def test_restore_ac_output_uses_runtime_intent_and_blocks_output_until_safe(tmp_path):
    dev = device(max_soc=90)
    dev.session.post.return_value = SimpleNamespace(status_code=200)
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    store.update_device_state(
        "WR1",
        now,
        has_battery=True,
        restore_pending=False,
        ac_mode_restore_pending=True,
    )
    controller = controller_for(dev, store)

    with configure_assist(enabled=True):
        run_controller_once(
            controller,
            state(soc=80, max_soc=90, ac_mode=1, ac_status=2),
            writes_allowed=True,
        )

    assert {"acMode": 2} in posted_properties(dev)
    assert controller.set_output_limit.call_count == 0
    record = store.get_device_state("WR1")
    assert record["ac_mode_restore_pending"] is True


def test_restore_pending_runs_when_feature_disabled(tmp_path):
    dev = device(max_soc=95)
    dev.session.post.return_value = SimpleNamespace(status_code=200)
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    store.update_device_state(
        "WR1",
        now,
        has_battery=True,
        restore_pending=True,
        max_soc_request_pending=True,
    )
    controller = controller_for(dev, store)

    with configure_assist(enabled=False):
        run_controller_once(
            controller,
            state(soc=80, max_soc=100, ac_mode=2),
            writes_allowed=True,
        )

    assert {"minSoc": 150, "socSet": 950} in posted_properties(dev)
    record = store.get_device_state("WR1")
    assert record["restore_pending"] is False
    assert record["max_soc_request_pending"] is False


def test_active_assist_disabled_mid_run_aborts_and_restores(tmp_path):
    dev = device(max_soc=90)
    dev.session.post.return_value = SimpleNamespace(status_code=200)
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    store.mark_assist_started("WR1", now, ac_charge_mode=True)
    controller = controller_for(dev, store)

    with configure_assist(enabled=False):
        run_controller_once(
            controller,
            state(soc=85, max_soc=100, ac_mode=1, ac_status=2),
            writes_allowed=True,
        )

    properties = posted_properties(dev)
    assert {"minSoc": 150, "socSet": 900} in properties
    assert {"acMode": 2} in properties
    record = store.get_device_state("WR1")
    assert record["full_charge_assist_active"] is False
    assert record["restore_pending"] is False
    assert record["ac_input_request_pending"] is False
    assert record["ac_mode_restore_pending"] is True
    assert controller.set_output_limit.call_count == 0


def test_ac_restore_completes_only_after_telemetry_confirms_output_mode(tmp_path):
    dev = device(max_soc=90)
    dev.session.post.return_value = SimpleNamespace(status_code=200)
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    store.update_device_state(
        "WR1",
        now,
        has_battery=True,
        ac_mode_restore_pending=True,
    )
    controller = controller_for(dev, store)

    with configure_assist(enabled=False):
        run_controller_once(
            controller,
            state(soc=80, max_soc=90, ac_mode=2),
            writes_allowed=True,
        )

    record = store.get_device_state("WR1")
    assert record["ac_mode_restore_pending"] is False

    dev.session.post.reset_mock()
    controller.set_output_limit.reset_mock()
    with configure_assist(enabled=False):
        run_controller_once(
            controller,
            state(soc=80, max_soc=90, ac_mode=2),
            writes_allowed=True,
        )

    assert {"acMode": 2} not in posted_properties(dev)
    assert controller.set_output_limit.call_count == 1


def test_ac_restore_retries_until_telemetry_confirms_output_mode(tmp_path):
    dev = device(max_soc=90)
    dev.session.post.return_value = SimpleNamespace(status_code=200)
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    store.update_device_state(
        "WR1",
        now,
        has_battery=True,
        ac_mode_restore_pending=True,
    )
    controller = controller_for(dev, store)

    with configure_assist(enabled=False):
        run_controller_once(
            controller,
            state(soc=80, max_soc=90, ac_mode=1, ac_status=2),
            writes_allowed=True,
        )
        run_controller_once(
            controller,
            state(soc=80, max_soc=90, ac_mode=1, ac_status=2),
            writes_allowed=True,
        )

    assert posted_properties(dev).count({"acMode": 2}) == 2
    record = store.get_device_state("WR1")
    assert record["ac_mode_restore_pending"] is True


def test_blocked_writes_keep_pending_flags(tmp_path):
    dev = device(max_soc=90)
    dev.session.post.return_value = SimpleNamespace(status_code=200)
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    store.set_full_charge_feature_enabled_state(
        True,
        datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    )
    store.update_device_state(
        "WR1",
        datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        has_battery=True,
        next_due_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc).isoformat(),
    )
    controller = controller_for(dev, store)

    with configure_assist(enabled=True, assist_start_soc=80):
        run_controller_once(
            controller,
            state(soc=85, max_soc=90, ac_mode=2),
            writes_allowed=False,
        )

    dev.session.post.assert_not_called()
    record = store.get_device_state("WR1")
    assert record["full_charge_assist_active"] is True
    assert record["max_soc_request_pending"] is True
    assert record["ac_input_request_pending"] is True
    assert record["ac_mode_restore_pending"] is True


if __name__ == "__main__":
    unittest.main()
