# SPDX-License-Identifier: AGPL-3.0-or-later
"""A stop the EMS reaches by itself ends the full-charge assist's charge too.

AR-1 in docs/developer/review-coverage.md. ``--once``, ``--max-cycles``,
``--duration`` and an error outside the guarded cycle return the regulator's
charge on the way out, but the release read only the transport's record of
it, and no transport has the assist's charge on record -- the assist starts it
through the state reconciler. That charge ran on to a full battery with no EMS
left to watch it. Since OFF-1 the controller keeps the assist's charge on a
:class:`~ems.charge_record.ChargeRecord` of its own, the record switching
control off ends it by; the release reads that record too.

A restart that switched the assist's AC mode off left the device in the AC
input the assist had put it in until the assist completed: nothing claimed
the way out.
"""

import json
import logging
import signal
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ems import config as cfg
from ems.charge_record import EXIT_RESEND_SECONDS, FOUND_EXIT_ATTEMPTS
from ems.config import AC_CHARGE_CONTROL_DEFAULTS
from ems.controller import EMSController
from ems.state_store import BatteryFullChargeStateStore
from tests.test_ac_charge_regulator import (
    IDLE_EXIT,
    FollowingHardware,
    SteppedClock,
    following_http_device,
)
from tests.test_battery_full_charge_assist import configure_assist, state
from tests.test_off_means_off import Runtime
from tests.test_stop_signal_unwinds import entry_module
from tests.test_write_gates import ShellyStub

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
    pytest.mark.simulation,
]

DUE = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
POWER = 200


def assist_due(path):
    store = BatteryFullChargeStateStore(str(path))
    store.set_full_charge_feature_enabled_state(True, DUE)
    store.update_device_state(
        "WR1", DUE, has_battery=True, next_due_at=DUE.isoformat()
    )
    return store


def assist_running(path, ac_charge_mode=True):
    """The store of an EMS that started the assist before this process did.

    With its AC mode, that EMS put the device into AC input and owes the way back.
    """

    store = assist_due(path)
    store.mark_assist_started("WR1", DUE, ac_charge_mode)
    store.update_device_state(
        "WR1",
        DUE,
        max_soc_request_pending=False,
        ac_input_request_pending=False,
        ac_mode_restore_pending=bool(ac_charge_mode),
    )
    return store


def charging_item(limit=POWER):
    item = state(soc=88, max_soc=100, ac_mode=1, ac_status=2, input_limit_w=limit)
    item.grid_input = limit
    return item


@contextmanager
def gates(
    dry_run=False,
    simulation=False,
    replay=None,
    hardware_writes=True,
    state_writes=True,
):
    """The write gates as a config sets them, each one on its own."""

    with (
        patch.object(cfg, "DRY_RUN", dry_run),
        patch.object(cfg, "SIMULATION_MODE", simulation),
        patch.object(cfg, "ARGS", SimpleNamespace(replay=replay)),
        patch.object(cfg, "ALLOW_HARDWARE_WRITES", hardware_writes),
        patch.object(cfg, "ALLOW_STATE_RECONCILIATION_WRITES", state_writes),
    ):
        yield


SHUT = {
    "dry_run": ({"dry_run": True}, "dry_run"),
    "simulation": ({"simulation": True}, "simulation_mode"),
    "replay": ({"replay": "trace.jsonl"}, "replay"),
    "transport gate": ({"hardware_writes": False}, "allow_hardware_writes"),
}


class Assist:
    """The real loop over one local-API device whose hardware follows its commands.

    Power and state writes both reach the hardware double through the device's
    own client, behind the real write gates.
    """

    def __init__(self, tmp_path, *, item=None, store=None, runtime=None):
        self.store = store or assist_due(tmp_path / "ems_state.sqlite")
        self.item = item or state(soc=85, max_soc=90, ac_mode=2, input_limit_w=0)
        self.dev, self.hardware = following_http_device(
            self.item, hardware_profile="solarflow_800_pro_2"
        )
        self.runtime = runtime or Runtime()
        self.controller = EMSController(
            devices=[self.dev],
            shelly=ShellyStub(0),
            sleep_enabled=False,
            runtime_state=self.runtime,
            battery_full_charge_store=self.store,
        )
        self.clock = SteppedClock()

    def fetch(self, _devices):
        self.dev.fetch()
        return [self.item]

    @contextmanager
    def loop(self, ac_mode=True, **gate_options):
        with (
            gates(**gate_options),
            patch("ems.controller.fetch_all_devices", side_effect=self.fetch),
            patch.object(cfg, "SYSTEM_ENABLED", True),
            patch.object(cfg, "MAX_TOTAL_POWER", 800),
            patch.object(cfg, "MAX_DEVICE_POWER", 800),
            patch.object(cfg, "MIN_OUTPUT_LIMIT", 0),
            patch.object(cfg, "DEADBAND", 10),
            patch.object(cfg, "SOC_RECONCILE_INTERVAL", 0),
            patch.object(cfg, "WINTER_CONFIG", {**cfg.WINTER_DEFAULTS, "enabled": False}),
            patch.object(
                cfg,
                "AC_CHARGE_CONTROL_CONFIG",
                {**AC_CHARGE_CONTROL_DEFAULTS, "enabled": False},
            ),
            patch("time.monotonic", self.clock),
            configure_assist(
                assist_start_soc=80,
                enable_ac_charge_mode=ac_mode,
                ac_charge_power=POWER,
            ),
        ):
            yield

    def run(self, cycles=1, **options):
        with self.loop(**options):
            for _ in range(cycles):
                self.controller.run_once()
                self.clock.now += 5

    def release(self, **gate_options):
        with gates(**gate_options), patch("time.monotonic", self.clock):
            self.controller.release_charging_devices()

    def written_since(self, before):
        return self.hardware.writes[before:]


def assist_charging(tmp_path, **options):
    """The assist started, and the device sits in its charge at ``POWER``."""

    rig = Assist(tmp_path, **options)
    rig.run()
    assert rig.item.ac_mode == 1, "the assist never put the device into AC input"
    assert rig.item.grid_input == POWER
    rig.run()
    return rig


def test_a_stop_the_ems_reaches_by_itself_ends_the_assist_s_charge(tmp_path, caplog):
    rig = assist_charging(tmp_path)
    before = len(rig.hardware.writes)

    with caplog.at_level(logging.INFO):
        rig.release()

    assert rig.written_since(before) == [IDLE_EXIT]
    assert rig.item.ac_mode == 2
    assert rig.item.grid_input == 0
    released = [
        line
        for line in caplog.text.splitlines()
        if "event=ac_charge_released_on_shutdown" in line
    ]
    assert len(released) == 1, caplog.text
    assert "charge=battery_full_charge_assist" in released[0]


@pytest.mark.parametrize("shut", SHUT.values(), ids=SHUT.keys())
def test_the_assist_s_release_waits_on_the_power_write_gate(tmp_path, caplog, shut):
    options, blocked_by = shut
    rig = assist_charging(tmp_path)
    before = len(rig.hardware.writes)

    with caplog.at_level(logging.INFO):
        rig.release(**options)

    assert rig.written_since(before) == []
    assert rig.item.ac_mode == 1
    withheld = [
        line
        for line in caplog.text.splitlines()
        if "event=ac_charge_release_withheld" in line
    ]
    assert len(withheld) == 1, caplog.text
    assert blocked_by in withheld[0]
    assert "event=ac_charge_released_on_shutdown" not in caplog.text


def test_the_end_of_the_assist_s_charge_needs_no_state_reconciliation(tmp_path):
    """A power command on the transport's gate, as every end of a charge is."""

    rig = assist_charging(tmp_path)
    before = len(rig.hardware.writes)

    rig.release(state_writes=False)

    assert rig.written_since(before) == [IDLE_EXIT]
    assert rig.item.ac_mode == 2


def test_an_assist_that_could_not_write_its_charge_is_released_nothing(tmp_path):
    """Behind a shut state gate the assist never put the device into AC input.

    Someone else's AC input on the device is not the assist's to end.
    """

    rig = Assist(tmp_path)
    rig.run(state_writes=False)
    assert rig.store.get_device_state("WR1")["full_charge_assist_active"] is True
    assert rig.hardware.writes == []
    rig.item.ac_mode = 1
    rig.item.input_limit_w = POWER
    rig.run(state_writes=False)

    rig.release()

    assert rig.hardware.writes == []


def test_an_assist_charge_left_to_the_device_after_its_exits_is_not_released(
    tmp_path,
):
    """The release keeps the cap the owner kept for a switch-off (OFF-1b)."""

    rig = assist_charging(tmp_path)
    rig.runtime.system["enabled"] = False
    rig.hardware.ignore(lambda properties: properties.get("acMode") == 2, times=99)
    for _ in range(FOUND_EXIT_ATTEMPTS + 1):
        rig.run()
        rig.clock.now += EXIT_RESEND_SECONDS
    assert rig.hardware.unapplied == [IDLE_EXIT] * FOUND_EXIT_ATTEMPTS
    before = len(rig.hardware.writes)

    rig.release()

    assert rig.written_since(before) == []


def test_a_stop_signal_names_the_assist_s_charge_it_keeps(tmp_path):
    rig = assist_charging(tmp_path)

    assert rig.controller.charges_held_by_ems() == {"WR1": POWER}


def test_the_assist_s_charge_taken_back_by_a_restart_is_released(tmp_path):
    """An assist from the process before is the EMS's charge once it holds it."""

    rig = Assist(
        tmp_path,
        item=charging_item(),
        store=assist_running(tmp_path / "ems_state.sqlite"),
    )
    rig.run()
    before = len(rig.hardware.writes)

    rig.release()

    assert rig.written_since(before) == [IDLE_EXIT]


def test_switching_the_ac_mode_off_ends_the_assist_s_ac_input(tmp_path, caplog):
    rig = Assist(
        tmp_path,
        item=charging_item(),
        store=assist_running(tmp_path / "ems_state.sqlite"),
    )

    with caplog.at_level(logging.INFO):
        rig.run(cycles=3, ac_mode=False)

    assert rig.hardware.writes == [{"acMode": 2}]
    assert rig.item.ac_mode == 2
    assert rig.item.grid_input == 0
    record = rig.store.get_device_state("WR1")
    assert record["full_charge_assist_active"] is True
    assert record["ac_mode_restore_pending"] is False
    assert rig.controller.full_charge_assist_charges == {}


def test_the_assist_without_its_ac_mode_leaves_the_direction_alone_after(
    tmp_path,
):
    """One end, then nothing: a later AC input is not the assist's to fight."""

    rig = Assist(
        tmp_path,
        item=charging_item(),
        store=assist_running(tmp_path / "ems_state.sqlite"),
    )
    rig.run(cycles=2, ac_mode=False)
    rig.item.ac_mode = 1

    rig.run(cycles=3, ac_mode=False)

    assert rig.hardware.writes == [{"acMode": 2}]


def test_the_assist_s_ac_input_is_ended_only_on_the_state_gate(tmp_path):
    rig = Assist(
        tmp_path,
        item=charging_item(),
        store=assist_running(tmp_path / "ems_state.sqlite"),
    )

    rig.run(cycles=3, ac_mode=False, state_writes=False)

    assert rig.hardware.writes == []
    assert rig.item.ac_mode == 1
    assert rig.store.get_device_state("WR1")["ac_mode_restore_pending"] is True


def test_a_stop_while_the_ac_input_is_still_ending_releases_it(tmp_path):
    rig = Assist(
        tmp_path,
        item=charging_item(),
        store=assist_running(tmp_path / "ems_state.sqlite"),
    )
    rig.hardware.ignore(lambda properties: properties == {"acMode": 2})
    rig.run(ac_mode=False)
    assert rig.item.ac_mode == 1
    rig.clock.now += EXIT_RESEND_SECONDS
    before = len(rig.hardware.writes)

    rig.release()

    assert rig.written_since(before) == [IDLE_EXIT]
    assert rig.item.ac_mode == 2


def test_an_assist_charging_again_after_its_ac_mode_was_off_is_restored(tmp_path):
    """The way back is owed again once the assist put the device back into AC input."""

    rig = Assist(
        tmp_path,
        item=charging_item(),
        store=assist_running(tmp_path / "ems_state.sqlite"),
    )
    rig.run(cycles=2, ac_mode=False)
    assert rig.store.get_device_state("WR1")["ac_mode_restore_pending"] is False

    rig.run(cycles=2)
    assert rig.item.ac_mode == 1
    assert rig.store.get_device_state("WR1")["ac_mode_restore_pending"] is True

    rig.item.soc = 100
    rig.item.soc_limit = 1
    rig.run(cycles=2)

    assert rig.item.ac_mode == 2
    assert rig.store.get_device_state("WR1")["full_charge_assist_active"] is False


class FullReport(FollowingHardware):
    """The hardware double, reporting what the live loop reads off a device."""

    def report(self):
        item = self.state
        return {
            **super().report(),
            "electricLevel": item.soc,
            "packNum": item.pack_num,
            "socSet": int(item.max_soc * 10),
            "minSoc": int(item.min_soc * 10),
            "socLimit": item.soc_limit,
            "gridOffMode": item.grid_off_mode,
        }


class QuietMqttRuntime:
    """A telemetry runtime with no broker; ``on_loop`` runs where the loop restarts it."""

    def __init__(self, on_loop=None):
        self.on_loop = on_loop
        self.starts = 0

    def start(self):
        self.starts += 1
        if self.starts > 1 and self.on_loop is not None:
            self.on_loop()

    def stop(self):
        return None

    def status(self):
        return {
            "enabled": False,
            "broker_configured": False,
            "running": False,
            "configured_device_count": 0,
            "invalid_device_count": 0,
        }

    def write_status_file(self, *_args, **_kwargs):
        return None


@pytest.fixture
def entry(tmp_path, monkeypatch):
    """Run the entry script's ``main`` against the hardware double, on a config of its own."""

    from ems.zendure_mqtt import control_runtime, runtime

    module = entry_module()
    item = charging_item()
    item.grid_off_mode = 2
    hardware = FullReport(item)
    assist_running(tmp_path / "ems_state.sqlite")
    config = cfg.default_runtime_config()
    config["system"].update(
        dry_run=False,
        allow_hardware_writes=True,
        allow_state_reconciliation_writes=True,
        runtime_state_path=str(tmp_path / "runtime-state.json"),
    )
    config["dashboard"]["enabled"] = False
    config["influxdb"]["enabled"] = False
    config["winter"]["enabled"] = False
    config["config_upgrade"] = {"on_startup": "disabled"}
    config["ac_charge_control"]["enabled"] = False
    config["battery_full_charge_assist"].update(
        state_database_path=str(tmp_path / "ems_state.sqlite"),
        ac_charge_power=POWER,
    )
    config["grid_meter"] = {"type": "shelly", "ip": "192.0.2.20"}
    config["devices"] = [
        {
            "name": "WR1",
            "ip": "192.0.2.10",
            "sn": "WR1-SN",
            "min_soc": 15,
            "max_soc": 100,
            "hardware_profile": "solarflow_800_pro_2",
        }
    ]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    monkeypatch.setattr(module, "create_session", lambda: hardware)
    monkeypatch.setattr(
        module, "create_grid_meter_client", lambda *_args, **_kwargs: ShellyStub(0)
    )
    monkeypatch.setattr(module, "close_grid_meter_client", lambda _client: None)
    monkeypatch.setattr(module, "setup_logging", lambda _level: None)
    monkeypatch.setattr(module, "build_winter_store", lambda: None)
    monkeypatch.setattr(
        control_runtime,
        "build_zendure_mqtt_control_runtime_or_abort",
        lambda _config: None,
    )
    monkeypatch.setattr("ems.controller.time.sleep", lambda _seconds: None)
    previous_signals = {
        name: signal.getsignal(getattr(signal, name))
        for name in ("SIGTERM", "SIGHUP")
        if hasattr(signal, name)
    }
    snapshot = {name: getattr(cfg, name) for name in dir(cfg) if name.isupper()}

    def run(*args, on_loop=None):
        mqtt = QuietMqttRuntime(on_loop)
        monkeypatch.setattr(
            runtime,
            "build_zendure_mqtt_runtime",
            lambda _config, shared_services=None: mqtt,
        )
        monkeypatch.setattr(
            sys, "argv", ["ems-solarflow-api-control.py", "--config", str(path), *args]
        )
        module.main()

    yield SimpleNamespace(run=run, item=item, hardware=hardware)

    for name, value in snapshot.items():
        setattr(cfg, name, value)
    for name, handler in previous_signals.items():
        signal.signal(getattr(signal, name), handler)


BOUNDED = {
    "--once": ["--once"],
    "--max-cycles": ["--max-cycles", "2"],
    "--duration": ["--duration", "0.000001"],
}


@pytest.mark.parametrize("bound", BOUNDED.values(), ids=BOUNDED.keys())
def test_a_bounded_run_returns_the_assist_s_charge_on_its_way_out(entry, bound):
    entry.run(*bound)

    assert entry.hardware.writes == [IDLE_EXIT]
    assert entry.item.ac_mode == 2
    assert entry.item.grid_input == 0


def test_an_error_outside_the_guarded_cycle_returns_the_assist_s_charge(entry):
    def broken():
        raise RuntimeError("telemetry runtime broke")

    with pytest.raises(RuntimeError, match="telemetry runtime broke"):
        entry.run(on_loop=broken)

    assert entry.hardware.writes == [IDLE_EXIT]
    assert entry.item.ac_mode == 2


def test_a_stop_signal_keeps_the_assist_s_charge_and_names_it(entry, caplog):
    def stopped():
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

    with caplog.at_level(logging.INFO), pytest.raises(SystemExit):
        entry.run(on_loop=stopped)

    assert entry.hardware.writes == []
    assert entry.item.ac_mode == 1
    kept = [
        line
        for line in caplog.text.splitlines()
        if "event=ac_charge_kept_across_stop" in line
    ]
    assert len(kept) == 1, caplog.text
    assert "devices=WR1" in kept[0]
    assert f"charging_w={POWER}" in kept[0]


def test_ctrl_c_returns_the_assist_s_charge(entry, caplog):
    """Ctrl-C is someone at the console, not a service manager stopping for a restart.

    Only SIGTERM and SIGHUP -- ``docker stop``, ``docker compose down``,
    ``systemctl stop`` -- keep a charge; KeyboardInterrupt returns it, as a
    stop the EMS reaches by itself does.
    """

    def interrupted():
        raise KeyboardInterrupt()

    with caplog.at_level(logging.INFO), pytest.raises(KeyboardInterrupt):
        entry.run(on_loop=interrupted)

    assert entry.hardware.writes == [IDLE_EXIT]
    assert entry.item.ac_mode == 2
    assert "event=ac_charge_kept_across_stop" not in caplog.text
    assert "event=ac_charge_released_on_shutdown" in caplog.text
