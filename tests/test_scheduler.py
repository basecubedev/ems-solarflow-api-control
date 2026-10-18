# SPDX-License-Identifier: AGPL-3.0-or-later
"""The scheduler fires each switch point once, as emsctl would.

Every case drives ``Scheduler.tick`` with an explicit time, so each
interleaving -- a restart in a window, a person's change between two switch
points, a clock that jumps -- is named by the times it passes and needs no
sleep.
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import emsctl
from ems.runtime_commands import build_validation_context, run_command
from ems.runtime_state import RuntimeState
from ems.schedule_recurrence import localize
from ems.schedule_store import ScheduleStore
from ems.scheduler import (
    CLOCK_FLOOR,
    RELOAD_EVERY,
    Scheduler,
    local_zone,
    window_clash,
)
from tests._emsctl_test_helpers import write_config

pytestmark = [pytest.mark.contract, pytest.mark.config]

BERLIN = ZoneInfo("Europe/Berlin")
DEVICES = {"WR1": "SN-TEST-0001"}


def at(text):
    """A Berlin wall-clock time as the aware UTC time the scheduler is given."""

    return localize(datetime.fromisoformat(text), BERLIN).astimezone(timezone.utc)


class Installation:
    def __init__(self, root, *, max_power=800):
        self.root = Path(root)
        write_config(self.root / "config.json")
        config = json.loads((self.root / "config.json").read_text())
        config["devices"][0]["max_power"] = max_power
        (self.root / "config.json").write_text(json.dumps(config))
        self.config = config
        assert emsctl.main(self._args("status")) == 0
        self.runtime = RuntimeState(str(self.root / "runtime-state.json"), {"devices": {}})
        self.runtime.load_if_changed(force=True)
        self.edited_at = at("2026-10-19T08:00")
        self.store = ScheduleStore(self.root / "schedules.json", clock=lambda: self.edited_at)
        self.scheduler = self.restart()

    def _args(self, *argv):
        return ["--config", str(self.root / "config.json"), "--runtime-state", str(self.root / "runtime-state.json"), *argv]

    def restart(self):
        self.scheduler = Scheduler(
            self.store,
            self.runtime,
            zone=BERLIN,
            validation_context=lambda: build_validation_context(self.config),
        )
        return self.scheduler

    def tick(self, text, devices=DEVICES):
        self.scheduler.tick(at(text), devices=devices)

    def device(self, key):
        return json.loads((self.root / "runtime-state.json").read_text())["devices"]["WR1"].get(key)

    def system(self, key):
        return json.loads((self.root / "runtime-state.json").read_text())["system"].get(key)


def socket_window(start="2026-10-19T18:00", end="2026-10-19T23:00", value="standard", end_value="off", **repeat):
    rule = {
        "name": "Evening socket",
        "target": {"scope": "device", "device": "WR1", "identity": "SN-TEST-0001"},
        "action": {"id": "offgrid_socket", "value": value, "end_value": end_value},
        "when": {"start": start, "end": end, "repeat": repeat or {"freq": "daily"}},
    }
    return rule


def total_limit_at(start, value, name="Limit", **repeat):
    rule = {
        "name": name,
        "target": {"scope": "system"},
        "action": {"id": "total_output_limit", "value": value},
        "when": {"start": start},
    }
    if repeat:
        rule["when"]["repeat"] = repeat
    return rule


@pytest.fixture
def home(tmp_path):
    return Installation(tmp_path)


def _events(caplog, name):
    return [record.getMessage() for record in caplog.records if f"event={name}" in record.getMessage()]


def test_a_window_sets_its_value_at_the_start_and_its_end_value_at_the_end_once_each(home, caplog):
    home.store.create(socket_window(), by="dashboard")

    with caplog.at_level(logging.INFO):
        home.tick("2026-10-19T17:59")
        assert home.device("offgrid_socket_mode") == "off"
        home.tick("2026-10-19T18:00")
        assert home.device("offgrid_socket_mode") == "standard"
        home.tick("2026-10-19T18:00:05")
        home.tick("2026-10-19T22:59")
        home.tick("2026-10-19T23:00")

    assert home.device("offgrid_socket_mode") == "off"
    assert len(_events(caplog, "schedule_switch_applied")) == 2
    assert len([line for line in _events(caplog, "runtime_values_updated") if "source=schedule" in line]) == 2


def test_a_persons_change_holds_until_the_next_switch_point(home):
    home.store.create(socket_window(), by="dashboard")
    home.tick("2026-10-19T18:00")

    run_command(home.runtime, "device", "offgrid", "eco", device="WR1",
                validation_context=build_validation_context(home.config), source="emsctl")
    home.tick("2026-10-19T19:31")
    home.tick("2026-10-19T20:31")
    assert home.device("offgrid_socket_mode") == "eco"

    home.tick("2026-10-19T23:00")
    assert home.device("offgrid_socket_mode") == "off"


def test_after_a_restart_in_the_window_the_start_is_caught_up_once(home, caplog):
    home.store.create(socket_window(), by="dashboard")
    home.tick("2026-10-19T17:00")
    home.restart()

    with caplog.at_level(logging.INFO):
        home.tick("2026-10-19T20:00")
        home.restart()
        home.tick("2026-10-19T20:05")

    assert home.device("offgrid_socket_mode") == "standard"
    assert len(_events(caplog, "schedule_catch_up")) == 1


def test_a_window_missed_entirely_fires_only_its_end(home):
    home.store.create(socket_window(), by="dashboard")
    run_command(home.runtime, "device", "offgrid", "eco", device="WR1",
                validation_context=build_validation_context(home.config), source="emsctl")

    home.tick("2026-10-19T23:30")

    assert home.device("offgrid_socket_mode") == "off"
    assert home.store.load().applied["devices.WR1.offgrid_socket_mode"]["at"] == at("2026-10-19T23:00")


def test_a_rule_saved_inside_its_window_first_acts_at_the_end(home):
    home.edited_at = at("2026-10-19T19:00")
    home.store.create(socket_window(), by="dashboard")

    home.tick("2026-10-19T19:01")
    assert home.device("offgrid_socket_mode") == "off"
    run_command(home.runtime, "device", "offgrid", "eco", device="WR1",
                validation_context=build_validation_context(home.config), source="emsctl")
    home.tick("2026-10-19T23:00")

    assert home.device("offgrid_socket_mode") == "off"


def test_at_one_instant_the_next_windows_start_beats_the_last_ones_end(home):
    home.store.create(socket_window(), by="dashboard")
    home.store.create(
        socket_window(start="2026-10-19T23:00", end="2026-10-20T06:00", value="eco", end_value="off"),
        by="dashboard",
    )

    home.tick("2026-10-19T18:00")
    home.tick("2026-10-19T23:00")
    assert home.device("offgrid_socket_mode") == "eco"
    home.tick("2026-10-20T06:00")
    assert home.device("offgrid_socket_mode") == "off"


def test_two_switch_points_at_one_instant_go_to_the_higher_rule(home):
    home.store.create(total_limit_at("2026-10-19T09:00", 1000, name="Top"), by="dashboard")
    home.store.create(total_limit_at("2026-10-19T09:00", 300, name="Below"), by="dashboard")
    home.config["system"]["max_total_power_limit"] = 1200

    home.tick("2026-10-19T09:00")

    assert home.system("max_total_power") == 1000


def test_a_pause_fires_nothing_and_a_resume_fires_nothing_from_the_pause(home):
    home.store.create(total_limit_at("2026-10-19T06:00", 600, freq="daily"), by="dashboard")
    home.edited_at = at("2026-10-19T05:00")
    home.store.set_enabled(False)

    home.tick("2026-10-19T06:00")
    assert home.system("max_total_power") == 900

    home.edited_at = at("2026-10-19T07:00")
    home.store.set_enabled(True)
    home.tick("2026-10-19T07:00:05")
    assert home.system("max_total_power") == 900
    home.tick("2026-10-20T06:00")
    assert home.system("max_total_power") == 600


def test_a_value_the_installation_no_longer_takes_is_not_written(tmp_path, caplog):
    home = Installation(tmp_path, max_power=400)
    rule = {
        "name": "Midday",
        "target": {"scope": "device", "device": "WR1"},
        "action": {"id": "device_output_limit", "value": 500},
        "when": {"start": "2026-10-19T12:00"},
    }
    home.store.create(rule, by="dashboard")

    with caplog.at_level(logging.INFO):
        home.tick("2026-10-19T12:00")
        home.restart()
        home.tick("2026-10-19T12:01")

    assert home.device("max_power") == 400
    assert home.store.load().applied == {}
    assert len(_events(caplog, "schedule_switch_refused")) == 2


def test_nothing_fires_on_a_clock_that_cannot_be_right_and_catches_up_once_it_is(home, caplog):
    home.store.create(total_limit_at("2026-10-20T06:00", 600), by="dashboard")

    with caplog.at_level(logging.INFO):
        home.scheduler.tick(CLOCK_FLOOR - timedelta(days=1), devices=DEVICES)
        home.tick("2026-10-18T06:30")
        assert home.system("max_total_power") == 900
        home.tick("2026-10-20T06:30")

    assert home.system("max_total_power") == 600
    assert len(_events(caplog, "schedule_clock_untrusted")) == 1


def test_a_clock_back_before_an_applied_switch_point_fires_nothing(home):
    home.store.create(socket_window(), by="dashboard")
    home.tick("2026-10-19T18:00")
    run_command(home.runtime, "device", "offgrid", "eco", device="WR1",
                validation_context=build_validation_context(home.config), source="emsctl")

    home.tick("2026-10-19T17:00")
    assert home.device("offgrid_socket_mode") == "eco"
    home.tick("2026-10-19T18:30")
    assert home.device("offgrid_socket_mode") == "eco"


def test_a_rule_whose_device_is_gone_applies_nothing(home, caplog):
    rule = socket_window()
    rule["target"] = {"scope": "device", "device": "WR9"}
    home.store.create(rule, by="dashboard")

    with caplog.at_level(logging.INFO):
        home.tick("2026-10-19T18:00")
        home.tick("2026-10-19T23:00")

    assert home.device("offgrid_socket_mode") == "off"
    assert home.store.load().applied == {}
    assert len(_events(caplog, "schedule_rule_orphaned")) == 1


def test_a_device_renamed_in_config_is_followed_by_its_identity(home):
    rule = socket_window()
    rule["target"]["device"] = "Garage"
    created = home.store.create(rule, by="dashboard")

    home.tick("2026-10-19T18:00")

    assert home.store.load().rule(created.id).device == "WR1"
    assert home.device("offgrid_socket_mode") == "standard"


def test_an_edit_the_files_timestamp_hides_is_seen_within_a_minute(home, monkeypatch):
    monkeypatch.setattr("ems.scheduler._file_signature", lambda path: "unchanged")
    home.tick("2026-10-19T09:00:30")
    other = ScheduleStore(home.store.path, clock=lambda: at("2026-10-19T09:00:40"))
    other.create(total_limit_at("2026-10-19T09:01", 600), by="emsctl")

    home.tick("2026-10-19T09:01:10")
    assert home.system("max_total_power") == 900
    home.scheduler.tick(at("2026-10-19T09:00:30") + RELOAD_EVERY, devices=DEVICES)
    assert home.system("max_total_power") == 600


def test_charging_from_the_grid_for_a_window_starts_and_stops_the_charge(home):
    rule = {
        "name": "Cheap night",
        "target": {"scope": "device", "device": "WR1"},
        "action": {"id": "charge_from_grid", "value": 600, "end_value": 0},
        "when": {"start": "2026-10-20T02:00", "end": "2026-10-20T05:00", "repeat": {"freq": "daily", "interval": 2}},
    }
    home.store.create(rule, by="dashboard")

    home.tick("2026-10-20T02:00")
    assert (home.device("runtime_role"), home.device("ac_charge_power_w")) == ("ac_input", 600)
    assert home.device("runtime_role_reason") == "schedule"
    home.tick("2026-10-20T05:00")
    assert home.device("runtime_role") == "ac_output"
    home.tick("2026-10-21T02:00")
    assert home.device("runtime_role") == "ac_output"


def test_the_status_names_what_comes_next_and_what_was_applied(home):
    created = home.store.create(socket_window(), by="dashboard")
    home.tick("2026-10-19T18:00")

    status = home.scheduler.status(at("2026-10-19T18:01"))

    assert status["enabled"] is True and status["clock_trusted"] is True
    assert status["zone"] == "Europe/Berlin"
    assert status["next"][0] == {
        "at": "2026-10-19T23:00:00+02:00",
        "slot": "devices.WR1.offgrid_socket_mode",
        "rule": created.id,
        "name": "Evening socket",
        "kind": "end",
        "value": "off",
        "text": "off",
    }
    assert status["applied"]["devices.WR1.offgrid_socket_mode"]["value"] == "standard"


def test_an_unreadable_store_applies_nothing_and_says_so_once(home, caplog):
    Path(home.store.path).write_text("{broken")

    with caplog.at_level(logging.INFO):
        home.tick("2026-10-19T18:00")
        home.tick("2026-10-19T18:00:05")

    assert home.scheduler.status(at("2026-10-19T18:00")) == {"loaded": False}
    assert len(_events(caplog, "schedule_store_load_error")) == 1


def test_windows_on_one_key_that_overlap_are_found_and_adjacent_ones_are_not(home):
    first = home.store.create(socket_window(), by="dashboard")
    book = home.store.load()
    clashing = home.store.create(socket_window(start="2026-10-19T22:00", end="2026-10-19T23:30"), by="dashboard")
    adjacent = home.store.create(socket_window(start="2026-10-19T23:00", end="2026-10-20T06:00"), by="dashboard")

    other, start = window_clash(clashing, book, BERLIN, at("2026-10-19T08:00"))
    assert other.id == first.id
    assert start == at("2026-10-19T22:00")
    assert window_clash(adjacent, book, BERLIN, at("2026-10-19T08:00")) is None


@pytest.mark.parametrize(
    "environ,expected",
    [
        ({"TZ": "Europe/Berlin"}, "Europe/Berlin"),
        ({"TZ": ":Europe/Berlin"}, "Europe/Berlin"),
        ({"TZ": "/usr/share/zoneinfo/Europe/Berlin"}, "Europe/Berlin"),
    ],
)
def test_the_zone_is_the_one_tz_names(environ, expected):
    assert getattr(local_zone(environ, localtime="/nonexistent"), "key", None) == expected


def test_without_tz_the_zone_is_the_mounted_localtime(tmp_path):
    localtime = tmp_path / "localtime"
    localtime.write_bytes(Path("/usr/share/zoneinfo/Europe/Berlin").read_bytes())

    zone = local_zone({}, localtime=str(localtime))

    assert zone.utcoffset(datetime(2026, 7, 1)) == timedelta(hours=2)


def test_a_localtime_link_into_the_zone_database_names_its_zone(tmp_path):
    zoneinfo = tmp_path / "zoneinfo" / "Europe"
    zoneinfo.mkdir(parents=True)
    (zoneinfo / "Berlin").write_bytes(Path("/usr/share/zoneinfo/Europe/Berlin").read_bytes())
    (tmp_path / "localtime").symlink_to(zoneinfo / "Berlin")

    assert local_zone({}, localtime=str(tmp_path / "localtime")).key == "Europe/Berlin"


def test_without_tz_and_localtime_the_zone_is_utc():
    assert local_zone({}, localtime="/nonexistent") is timezone.utc


def test_a_tz_without_a_zone_file_is_read_as_utc_as_the_c_library_reads_it():
    assert local_zone({"TZ": "Not/A_Zone"}, localtime="/nonexistent") is timezone.utc


def test_a_posix_rule_string_is_no_zone_the_scheduler_can_follow():
    assert local_zone({"TZ": "CET-1CEST,M3.5.0,M10.5.0/3"}, localtime="/nonexistent") is None


def test_without_a_zone_it_can_follow_nothing_fires_and_it_says_so_once(home, caplog):
    home.store.create(total_limit_at("2026-10-19T09:00", 600), by="dashboard")
    scheduler = Scheduler(home.store, home.runtime, zone=None,
                          validation_context=lambda: build_validation_context(home.config))

    with caplog.at_level(logging.INFO):
        scheduler.tick(at("2026-10-19T09:00"), devices=DEVICES)
        scheduler.tick(at("2026-10-19T09:01"), devices=DEVICES)

    assert home.system("max_total_power") == 900
    assert scheduler.status(at("2026-10-19T09:01")) == {"loaded": False, "zone": "unsupported"}
    assert len(_events(caplog, "schedule_zone_unsupported")) == 1


def _check(home, payload, now="2026-10-19T08:00"):
    from ems.scheduler import check_rule

    return check_rule(
        payload,
        home.store.load(),
        validation_context=build_validation_context(home.config),
        devices=DEVICES,
        zone=BERLIN,
        now=at(now),
    )


def test_a_rule_this_installation_can_carry_out_passes_the_save_check(home):
    rule = _check(home, socket_window())

    assert rule.device == "WR1" and rule.is_window
    assert _check(home, {**total_limit_at("2026-10-19T09:00", 900), "name": "Winter"}).action.id == "total_output_limit"
    _check(home, {"name": "Winter", "target": {"scope": "system"},
                  "action": {"id": "winter_mode", "value": True}, "when": {"start": "2026-10-19T09:00"}})


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda rule: rule["target"].update(device="WR9"), "unknown device WR9; known devices: WR1"),
        (
            lambda rule: rule.update(action={"id": "device_output_limit", "value": 801, "end_value": 0}),
            "max_power must be between 0 and 800",
        ),
        (
            lambda rule: rule.update(action={"id": "device_output_limit", "value": 400, "end_value": 900}),
            "max_power must be between 0 and 800",
        ),
        (
            lambda rule: rule["when"].update(start="2026-10-18T18:00", end="2026-10-18T23:00", repeat=None),
            "lies in the past and would never fire",
        ),
    ],
)
def test_a_rule_the_installation_cannot_carry_out_is_refused_before_it_is_saved(home, change, message):
    from ems.schedule_model import ScheduleRuleError

    rule = socket_window()
    change(rule)
    if rule["when"].get("repeat") is None:
        rule["when"].pop("repeat", None)

    with pytest.raises(ScheduleRuleError, match=message):
        _check(home, rule)


def test_a_window_that_overlaps_another_on_the_same_setting_is_refused_with_its_name(home):
    from ems.schedule_model import ScheduleRuleError

    home.store.create(socket_window(), by="dashboard")

    with pytest.raises(ScheduleRuleError, match="overlaps 'Evening socket' on Mon 19 Oct 2026 22:00"):
        _check(home, {**socket_window(start="2026-10-19T22:00", end="2026-10-19T23:30"), "name": "Late"})
    _check(home, {**socket_window(start="2026-10-19T23:00", end="2026-10-20T06:00"), "name": "Night"})


def test_a_switch_point_stamped_by_a_clock_that_ran_ahead_blocks_nothing(home):
    """The clock read March 2027 for a while and the applied record says so;
    once it is right again the setting's next switch point still fires."""

    home.store.create(total_limit_at("2026-10-19T09:00", 600, freq="daily"), by="dashboard")
    home.store.record_applied("system.max_total_power", rule_id="r_00000000",
                              at=at("2027-03-01T09:00"), value=700)

    home.tick("2026-10-19T09:00")

    assert home.system("max_total_power") == 600


def test_a_rule_saved_through_the_dashboard_follows_its_device_through_a_rename(home):
    from dashboard.schedule_api import ScheduleApi

    api = ScheduleApi(home.store, runtime_state=home.runtime, devices=lambda: {"WR1": "SN-TEST-0001"},
                      validation_context=lambda: build_validation_context(home.config), zone=BERLIN,
                      clock=lambda: at("2026-10-19T08:00"))
    rule = socket_window()
    rule["target"] = {"scope": "device", "device": "WR1"}
    status, created = api.handle("POST", "/api/schedule/rules", {"rule": rule})
    assert status == 201 and created["target"]["identity"] == "SN-TEST-0001"

    home.tick("2026-10-19T18:00", devices={"Terrasse": "SN-TEST-0001"})

    assert home.store.load().rule(created["id"]).device == "Terrasse"


def _charge_window(**when):
    return {
        "name": "Cheap night",
        "target": {"scope": "device", "device": "WR1"},
        "action": {"id": "charge_from_grid", "value": 600, "end_value": 0},
        "when": {"start": "2026-10-20T02:00", "end": "2026-10-20T05:00", "repeat": {"freq": "daily"}, **when},
    }


@pytest.mark.parametrize(
    "end_it",
    [
        lambda home, rule: home.store.delete(rule.id, revision=rule.revision),
        lambda home, rule: home.store.replace(rule.id, {**_charge_window(), "enabled": False},
                                              revision=rule.revision, by="dashboard"),
        lambda home, rule: home.store.skip(rule.id, date(2026, 10, 20), revision=rule.revision, by="dashboard"),
        lambda home, rule: home.store.set_enabled(False),
    ],
    ids=["deleted", "switched_off", "skipped", "paused"],
)
def test_a_window_whose_rule_goes_away_ends_at_once(home, caplog, end_it):
    """Interleaving: a charge window starts at 02:00, the operator takes its
    rule away at 03:00. Its 05:00 end will not come, so the charge would run
    on; the window ends at the next tick instead."""

    rule = home.store.create(_charge_window(), by="dashboard")
    home.tick("2026-10-20T02:00")
    assert home.device("runtime_role") == "ac_input"

    end_it(home, home.store.load().rule(rule.id))
    with caplog.at_level(logging.INFO):
        home.tick("2026-10-20T03:00")

    assert home.device("runtime_role") == "ac_output"
    assert len(_events(caplog, "schedule_window_closed")) == 1
    assert "window" not in home.store.load().applied["devices.WR1.runtime_role,devices.WR1.ac_charge_power_w"]


def test_a_window_edited_to_end_later_keeps_running(home):
    rule = home.store.create(_charge_window(), by="dashboard")
    home.tick("2026-10-20T02:00")

    home.store.replace(rule.id, _charge_window(end="2026-10-20T06:00"), revision=1, by="dashboard")
    home.tick("2026-10-20T03:00")
    assert home.device("runtime_role") == "ac_input"
    home.tick("2026-10-20T05:00")
    assert home.device("runtime_role") == "ac_input"
    home.tick("2026-10-20T06:00")

    assert home.device("runtime_role") == "ac_output"


def test_a_charge_is_one_write_and_all_or_nothing(home, monkeypatch):
    rule = home.store.create(_charge_window(), by="dashboard")
    writes = []
    real = home.runtime.update_device

    def counting(name, values):
        writes.append(dict(values))
        return real(name, values)

    monkeypatch.setattr(home.runtime, "update_device", counting)

    home.tick("2026-10-20T02:00")

    assert [sorted(write) for write in writes] == [["ac_charge_power_w", "runtime_role", "runtime_role_reason"]]
    assert rule.id


def test_a_runtime_write_that_fails_is_reported_once_and_does_not_stop_the_schedule(home, monkeypatch, caplog):
    home.store.create(total_limit_at("2026-10-19T09:00", 600), by="dashboard")
    home.store.create(total_limit_at("2026-10-19T10:00", 700, name="Later"), by="dashboard")
    real = home.runtime.update_section
    calls = []

    def full_disk(name, values):
        calls.append(values)
        if len(calls) == 1:
            raise OSError(28, "No space left on device")
        return real(name, values)

    monkeypatch.setattr(home.runtime, "update_section", full_disk)

    with caplog.at_level(logging.INFO):
        home.tick("2026-10-19T09:00")
        home.tick("2026-10-19T09:00:05")
        home.tick("2026-10-19T10:00")

    assert home.system("max_total_power") == 700
    assert len(_events(caplog, "schedule_switch_failed")) == 1


def test_a_rename_carries_what_was_applied_so_nothing_fires_twice(home):
    rule = socket_window()
    created = home.store.create(rule, by="dashboard")
    home.tick("2026-10-19T18:00")
    run_command(home.runtime, "device", "offgrid", "eco", device="WR1",
                validation_context=build_validation_context(home.config), source="emsctl")

    home.store.rename_devices({"SN-TEST-0001": "WR1"})
    renamed = home.store.load()
    assert renamed.applied["devices.WR1.offgrid_socket_mode"]["rule"] == created.id

    data = json.loads(Path(home.store.path).read_text())
    data["rules"][0]["target"]["device"] = "Old"
    data["applied"] = {"devices.Old.offgrid_socket_mode": data["applied"]["devices.WR1.offgrid_socket_mode"]}
    data["applied"]["devices.Old.offgrid_socket_mode"]["window"]["device"] = "Old"
    Path(home.store.path).write_text(json.dumps(data))
    home.restart()
    home.tick("2026-10-19T18:30")

    book = home.store.load()
    assert set(book.applied) == {"devices.WR1.offgrid_socket_mode"}
    assert book.applied["devices.WR1.offgrid_socket_mode"]["window"]["device"] == "WR1"
    assert home.device("offgrid_socket_mode") == "eco"


def test_the_status_list_catches_up_with_time_without_a_switch_point(home):
    home.store.create(total_limit_at("2026-10-29T09:00", 600), by="dashboard")
    home.tick("2026-10-19T09:00")
    assert home.scheduler.status(at("2026-10-19T09:00"))["next"] == []

    assert len(home.scheduler.status(at("2026-10-23T09:00"))["next"]) == 1


@pytest.mark.parametrize(
    "edit",
    [
        lambda rule: {**rule, "target": {"scope": "device", "device": "WR2"}},
        lambda rule: {**rule, "when": {**rule["when"], "end": "2026-10-20T02:30"}},
    ],
    ids=["moved_to_another_device", "cut_to_an_end_already_passed"],
)
def test_a_running_window_edited_so_its_own_end_will_not_come_ends_at_once(home, edit):
    home.config["devices"].append({"name": "WR2", "ip": "192.0.2.21", "sn": "SN-TEST-0002", "max_power": 800})
    rule = home.store.create(_charge_window(), by="dashboard")
    home.tick("2026-10-20T02:00")
    assert home.device("runtime_role") == "ac_input"

    home.edited_at = at("2026-10-20T03:00")
    home.store.replace(rule.id, edit(_charge_window()), revision=1, by="dashboard")
    home.tick("2026-10-20T03:00:05", devices={"WR1": "SN-TEST-0001", "WR2": "SN-TEST-0002"})

    assert home.device("runtime_role") == "ac_output"


def test_an_edit_landing_while_the_scheduler_records_is_still_seen(home, monkeypatch):
    """Interleaving: the scheduler applies 09:00; between its runtime write
    and its record, emsctl adds a rule for 09:01. The record keeps that rule,
    and the scheduler parses it instead of taking the file for its own."""

    home.store.create(total_limit_at("2026-10-19T09:00", 600), by="dashboard")
    real = home.store.record_applied
    other = ScheduleStore(home.store.path, clock=lambda: at("2026-10-19T09:00:01"))

    def with_an_edit_first(*args, **kwargs):
        other.create(total_limit_at("2026-10-19T09:01", 700, name="From the shell"), by="emsctl")
        return real(*args, **kwargs)

    monkeypatch.setattr(home.store, "record_applied", with_an_edit_first)
    home.tick("2026-10-19T09:00")
    monkeypatch.setattr(home.store, "record_applied", real)

    home.tick("2026-10-19T09:01")

    assert home.system("max_total_power") == 700


def test_a_runtime_write_that_fails_once_is_tried_again_on_the_next_cycle(home, monkeypatch):
    home.store.create(total_limit_at("2026-10-19T09:00", 600), by="dashboard")
    real = home.runtime.update_section
    calls = []

    def fails_once(name, values):
        calls.append(values)
        if len(calls) == 1:
            raise OSError(5, "Input/output error")
        return real(name, values)

    monkeypatch.setattr(home.runtime, "update_section", fails_once)

    home.tick("2026-10-19T09:00")
    assert home.system("max_total_power") == 900
    home.tick("2026-10-19T09:00:05")

    assert home.system("max_total_power") == 600


def test_a_device_name_with_dots_keeps_its_applied_record_through_a_rename(home):
    home.config["devices"][0]["name"] = "WR.1"
    data = json.loads((home.root / "runtime-state.json").read_text())
    data["devices"]["WR.1"] = data["devices"].pop("WR1")
    (home.root / "runtime-state.json").write_text(json.dumps(data))
    home.runtime.load_if_changed(force=True)
    rule = socket_window()
    rule["target"] = {"scope": "device", "device": "WR.1", "identity": "SN-TEST-0001"}
    home.store.create(rule, by="dashboard")
    home.tick("2026-10-19T18:00", devices={"WR.1": "SN-TEST-0001"})

    home.store.rename_devices({"SN-TEST-0001": "WR.East"})

    assert set(home.store.load().applied) == {"devices.WR.East.offgrid_socket_mode"}


def test_the_status_and_the_tick_never_run_at_once(home):
    """The dashboard asks for the status from its own threads."""

    entered = []

    class RecordingLock:
        def __enter__(self):
            entered.append(True)

        def __exit__(self, *exc):
            return False

    home.scheduler._lock = RecordingLock()
    home.tick("2026-10-19T09:00")
    home.scheduler.status(at("2026-10-19T09:00"))

    assert entered == [True, True]


def test_a_clock_stepped_back_an_hour_fires_nothing_twice(home):
    """A daily window saved two days ago started at 18:00; the clock is then
    set back to 17:00. Yesterday's 23:00 end must not fire, nor 18:00 again."""

    home.edited_at = at("2026-10-17T08:00")
    home.store.create(socket_window(start="2026-10-17T18:00", end="2026-10-17T23:00"), by="dashboard")
    home.tick("2026-10-19T18:05")
    assert home.device("offgrid_socket_mode") == "standard"
    run_command(home.runtime, "device", "offgrid", "eco", device="WR1",
                validation_context=build_validation_context(home.config), source="emsctl")

    home.tick("2026-10-19T17:00")
    home.tick("2026-10-19T18:00")
    home.tick("2026-10-19T18:10")

    assert home.device("offgrid_socket_mode") == "eco"


def test_a_switch_point_that_could_not_be_recorded_does_not_fire_again(home, monkeypatch, caplog):
    home.store.create(total_limit_at("2026-10-19T09:00", 600), by="dashboard")
    real = home.store.record_applied
    failing = {"on": True}

    def full_disk(*args, **kwargs):
        if failing["on"]:
            raise OSError(28, "No space left on device")
        return real(*args, **kwargs)

    monkeypatch.setattr(home.store, "record_applied", full_disk)
    with caplog.at_level(logging.INFO):
        home.tick("2026-10-19T09:00")
        run_command(home.runtime, "system", "max-power", "400",
                    validation_context=build_validation_context(home.config), source="emsctl")
        home.tick("2026-10-19T09:00:05")
        home.tick("2026-10-19T09:01:10")
    assert home.system("max_total_power") == 400
    assert len([line for line in _events(caplog, "runtime_values_updated") if "source=schedule" in line]) == 1

    failing["on"] = False
    home.tick("2026-10-19T09:01:15")

    assert home.store.load().applied["system.max_total_power"]["at"] == at("2026-10-19T09:00")


def test_a_runtime_state_another_process_keeps_locked_is_tried_again(home, monkeypatch):
    import fcntl

    monkeypatch.setattr("ems.runtime_state.LOCK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr("ems.file_io.LOCK_TIMEOUT_SECONDS", 0.05)
    home.store.create(total_limit_at("2026-10-19T09:00", 600), by="dashboard")
    holder = open(home.root / "runtime-state.json.lock", "a")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        home.tick("2026-10-19T09:00")
        assert home.system("max_total_power") == 900
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    home.tick("2026-10-19T09:00:05")

    assert home.system("max_total_power") == 600


def test_a_rule_switched_off_still_follows_its_device_through_a_rename(home):
    rule = socket_window()
    rule["enabled"] = False
    created = home.store.create(rule, by="dashboard")

    home.tick("2026-10-19T09:00", devices={"Terrasse": "SN-TEST-0001"})

    assert home.store.load().rule(created.id).device == "Terrasse"


def test_the_status_shows_times_in_the_ems_zone(home):
    home.store.create(socket_window(), by="dashboard")
    home.tick("2026-10-19T09:00")

    assert home.scheduler.status(at("2026-10-19T09:00"))["next"][0]["at"] == "2026-10-19T18:00:00+02:00"


@pytest.mark.parametrize(
    "window",
    [
        {"action": "an_action_of_a_newer_release", "device": "WR1", "end_value": 0, "ends_at": "2026-10-20T03:00:00Z"},
        {"action": "winter_mode", "device": None, "end_value": None, "ends_at": "2026-10-20T03:00:00Z"},
    ],
    ids=["unknown_action", "invalid_end_value"],
)
def test_a_recorded_window_this_build_cannot_close_stalls_nothing(home, window):
    home.store.create(total_limit_at("2026-10-20T02:00", 600), by="dashboard")
    data = json.loads(Path(home.store.path).read_text())
    data["applied"]["devices.WR1.max_power"] = {
        "rule": "r_0000beef", "at": "2026-10-20T01:00:00Z", "value": 1, "window": window,
    }
    Path(home.store.path).write_text(json.dumps(data))

    home.tick("2026-10-20T02:00")

    assert home.system("max_total_power") == 600


def test_a_window_cut_to_a_past_end_closes_with_the_end_value_just_saved(home):
    rule = home.store.create(socket_window(start="2026-10-19T18:00", end="2026-10-19T23:00",
                                           value="standard", end_value="off"), by="dashboard")
    home.tick("2026-10-19T18:00")

    home.edited_at = at("2026-10-19T20:00")
    home.store.replace(rule.id, socket_window(start="2026-10-19T18:00", end="2026-10-19T19:30",
                                              value="standard", end_value="eco"), revision=1, by="dashboard")
    home.tick("2026-10-19T20:00:05")

    assert home.device("offgrid_socket_mode") == "eco"


def test_a_busy_runtime_state_holds_the_rest_of_the_cycles_switch_points(home, monkeypatch):
    home.store.create(total_limit_at("2026-10-19T09:00", 600), by="dashboard")
    rule = socket_window(start="2026-10-19T09:00", end="2026-10-19T10:00")
    home.store.create(rule, by="dashboard")
    attempts = []

    def busy(*args, **kwargs):
        attempts.append(args)
        from ems.runtime_commands import RuntimeWriteError
        from ems.runtime_state import FileLockTimeout

        try:
            raise ValueError("runtime state is busy") from FileLockTimeout("held")
        except ValueError as exc:
            raise RuntimeWriteError(str(exc)) from exc

    monkeypatch.setattr("ems.scheduler.run_commands", busy)

    home.tick("2026-10-19T09:00")

    assert len(attempts) == 1


CHARGE_SLOT = "devices.WR1.runtime_role,devices.WR1.ac_charge_power_w"


def test_a_window_running_when_the_store_turns_unreadable_still_ends_at_its_time(home, caplog):
    """Interleaving: a charge window starts at 02:00, at 03:00 schedules.json
    is broken by hand. Nothing starts while it cannot be read, but the
    window's 05:00 end still comes, or the charge would run on until the file
    is mended; once it is, the end is recorded and fires nothing again."""

    home.store.create(_charge_window(), by="dashboard")
    home.tick("2026-10-20T02:00")
    assert home.device("runtime_role") == "ac_input"
    good = Path(home.store.path).read_bytes()

    Path(home.store.path).write_text("{broken")
    home.tick("2026-10-20T03:00")
    assert home.device("runtime_role") == "ac_input"
    with caplog.at_level(logging.INFO):
        home.tick("2026-10-20T05:00")
        home.tick("2026-10-20T05:00:05")
    assert home.device("runtime_role") == "ac_output"
    assert len(_events(caplog, "schedule_window_closed")) == 1

    Path(home.store.path).write_bytes(good)
    home.tick("2026-10-20T05:01")

    record = home.store.load().applied[CHARGE_SLOT]
    assert record["at"] == at("2026-10-20T05:00") and "window" not in record


def test_a_later_switch_point_on_a_lost_windows_setting_replaces_the_window(home):
    """Interleaving: a charge window starts at 02:00, the EMS stops; a single
    switch point of another rule sets 300 W at 03:30; the window's rule is
    deleted. At 04:00 the latest switch point wins, as it would have had the
    EMS run through: the window does not end over it."""

    window = home.store.create(_charge_window(), by="dashboard")
    home.tick("2026-10-20T02:00")
    single = home.store.create({
        "name": "Top up",
        "target": {"scope": "device", "device": "WR1"},
        "action": {"id": "charge_from_grid", "value": 300},
        "when": {"start": "2026-10-20T03:30"},
    }, by="dashboard")
    home.store.delete(window.id, revision=window.revision)

    home.restart()
    home.tick("2026-10-20T04:00")

    assert (home.device("runtime_role"), home.device("ac_charge_power_w")) == ("ac_input", 300)
    assert home.store.load().applied[CHARGE_SLOT]["rule"] == single.id


def test_a_busy_schedules_lock_is_waited_for_once_per_cycle(home, monkeypatch):
    """Each record of an applied switch point waits for the schedules lock;
    with another process holding it, a cycle with several due settings must
    not wait once per setting -- the control loop is the caller."""

    from ems.file_io import FileLockTimeout
    from ems.schedule_store import ScheduleStoreError

    home.store.create(total_limit_at("2026-10-19T09:00", 600), by="dashboard")
    home.store.create(socket_window(start="2026-10-19T09:00", end="2026-10-19T10:00"), by="dashboard")
    real = home.store.record_applied
    attempts = []

    def busy(*args, **kwargs):
        attempts.append(args)
        raise ScheduleStoreError("schedules are busy") from FileLockTimeout("held")

    monkeypatch.setattr(home.store, "record_applied", busy)
    home.tick("2026-10-19T09:00")

    assert len(attempts) == 1
    assert (home.system("max_total_power"), home.device("offgrid_socket_mode")) == (600, "standard")

    monkeypatch.setattr(home.store, "record_applied", real)
    home.tick("2026-10-19T09:00:05")

    assert set(home.store.load().applied) == {"system.max_total_power", "devices.WR1.offgrid_socket_mode"}


def test_a_paused_schedule_names_nothing_that_comes_next(home):
    home.store.create(socket_window(), by="dashboard")
    home.store.set_enabled(False)
    home.tick("2026-10-19T08:00")

    assert home.scheduler.status(at("2026-10-19T08:00"))["next"] == []


@pytest.mark.parametrize(
    "tz,offset",
    [("UTC0", timedelta(0)), ("CET-1", timedelta(hours=1)), ("<+03>-3", timedelta(hours=3)),
     ("EST5", timedelta(hours=-5)), ("IST-5:30", timedelta(hours=5, minutes=30))],
)
def test_a_posix_tz_without_daylight_saving_is_the_fixed_offset_it_names(tz, offset):
    zone = local_zone({"TZ": tz}, localtime="/nonexistent")

    assert zone.utcoffset(datetime(2026, 7, 1)) == zone.utcoffset(datetime(2026, 1, 1)) == offset
    assert str(zone) == tz


def test_a_schedules_file_removed_after_a_switch_point_fires_nothing_more(home):
    home.store.create(total_limit_at("2026-10-19T09:00", 600, freq="daily"), by="dashboard")
    home.tick("2026-10-19T09:00")
    assert home.system("max_total_power") == 600

    Path(home.store.path).unlink()
    run_command(home.runtime, "system", "max-power", "400",
                validation_context=build_validation_context(home.config), source="emsctl")
    home.tick("2026-10-19T09:00:05")
    home.tick("2026-10-20T09:00")

    assert home.system("max_total_power") == 400


@pytest.mark.parametrize(
    "replace_file,still_running_at_3",
    [
        (lambda home, before, empty: Path(home.store.path).unlink(), False),
        (lambda home, before, empty: Path(home.store.path).write_bytes(empty), False),
        (lambda home, before, empty: Path(home.store.path).write_bytes(before), True),
    ],
    ids=["removed", "restored_without_the_rule", "restored_with_the_rule"],
)
def test_a_file_replaced_under_a_running_window_does_not_forget_it(home, replace_file, still_running_at_3):
    """Interleaving: a charge window starts at 02:00; at 03:00 a backup is
    restored, or the file removed. The new file has no record that the
    window runs, but the scheduler still ends it: at once when its rule went
    with the old file, at 05:00 when the restored file still has it."""

    home.store.set_enabled(True)
    empty = Path(home.store.path).read_bytes()
    home.store.create(_charge_window(), by="dashboard")
    before = Path(home.store.path).read_bytes()
    home.tick("2026-10-20T02:00")
    assert home.device("runtime_role") == "ac_input"

    replace_file(home, before, empty)
    home.tick("2026-10-20T03:00")
    assert (home.device("runtime_role") == "ac_input") is still_running_at_3
    home.tick("2026-10-20T05:00")

    assert home.device("runtime_role") == "ac_output"
    assert "window" not in home.store.load().applied[CHARGE_SLOT]


def test_a_rename_that_meets_a_busy_lock_waits_and_the_other_rules_still_fire(home, monkeypatch, caplog):
    from ems.file_io import FileLockTimeout
    from ems.schedule_store import ScheduleStoreError

    renamed = socket_window()
    renamed["target"]["device"] = "Garage"
    moved = home.store.create(renamed, by="dashboard")
    home.store.create(total_limit_at("2026-10-19T09:00", 600), by="dashboard")
    real = home.store.rename_devices

    def busy(*args, **kwargs):
        raise ScheduleStoreError("schedules are busy") from FileLockTimeout("held")

    monkeypatch.setattr(home.store, "rename_devices", busy)
    with caplog.at_level(logging.INFO):
        home.tick("2026-10-19T09:00")
        home.tick("2026-10-19T09:00:05")

    assert home.system("max_total_power") == 600
    assert _events(caplog, "schedule_store_load_error") == []
    assert len(_events(caplog, "schedule_rename_deferred")) == 1

    monkeypatch.setattr(home.store, "rename_devices", real)
    home.tick("2026-10-19T09:01:10")

    assert home.store.load().rule(moved.id).device == "WR1"


def test_a_lost_window_whose_end_is_refused_says_so_once(home, caplog):
    rule = home.store.create({
        "name": "Low noon",
        "target": {"scope": "device", "device": "WR1"},
        "action": {"id": "device_output_limit", "value": 300, "end_value": 700},
        "when": {"start": "2026-10-19T11:00", "end": "2026-10-19T14:00"},
    }, by="dashboard")
    home.tick("2026-10-19T11:00")
    home.config["devices"][0]["max_power"] = 400
    home.store.delete(rule.id, revision=rule.revision)

    with caplog.at_level(logging.INFO):
        for minute, start in enumerate(("2026-10-20T09:00", "2026-10-20T10:00", "2026-10-20T11:00"), start=1):
            home.store.create(total_limit_at(start, 600, name=f"Other {minute}"), by="dashboard")
            home.tick(f"2026-10-19T12:0{minute}")

    assert home.device("max_power") == 300
    assert len(_events(caplog, "schedule_switch_refused")) == 1


def test_a_window_end_the_runtime_could_not_save_while_the_store_is_unreadable_is_tried_again(home, monkeypatch):
    home.store.create(_charge_window(), by="dashboard")
    home.tick("2026-10-20T02:00")
    Path(home.store.path).write_text("{broken")
    home.tick("2026-10-20T03:00")
    real = home.runtime.update_device
    calls = []

    def full_disk(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise OSError(28, "No space left on device")
        return real(*args, **kwargs)

    monkeypatch.setattr(home.runtime, "update_device", full_disk)
    home.tick("2026-10-20T05:00")
    assert json.loads((home.root / "runtime-state.json").read_text())["devices"]["WR1"]["runtime_role"] == "ac_input"
    home.tick("2026-10-20T05:00:05")

    assert home.device("runtime_role") == "ac_output"


def test_a_broken_store_is_read_again_only_when_it_changes(home, monkeypatch):
    Path(home.store.path).write_text("{broken")
    real = home.store.read_bytes
    reads = []

    def counting():
        reads.append(1)
        return real()

    monkeypatch.setattr(home.store, "read_bytes", counting)
    for second in range(0, 50, 5):
        home.tick(f"2026-10-19T09:00:{second:02d}")

    assert len(reads) == 1


def test_a_rename_behind_a_busy_lock_spares_the_cycle_a_second_wait(home, monkeypatch):
    from ems.file_io import FileLockTimeout
    from ems.schedule_store import ScheduleStoreError

    renamed = socket_window()
    renamed["target"]["device"] = "Garage"
    home.store.create(renamed, by="dashboard")
    home.store.create(total_limit_at("2026-10-19T09:00", 600), by="dashboard")
    waits = []

    def busy(*args, **kwargs):
        waits.append(args)
        raise ScheduleStoreError("schedules are busy") from FileLockTimeout("held")

    monkeypatch.setattr(home.store, "rename_devices", busy)
    monkeypatch.setattr(home.store, "record_applied", busy)
    home.tick("2026-10-19T09:00")

    assert len(waits) == 1
    assert home.system("max_total_power") == 600


def test_a_resume_stamped_by_a_clock_far_ahead_does_not_close_every_window_as_it_opens(home):
    """The clock ran two days ahead when the schedule was resumed, then NTP
    put it right. The resume time bounds nothing; it must not cut a window's
    own end off either, or every window would stop in the cycle it starts."""

    home.store.create(_charge_window(), by="dashboard")
    data = json.loads(Path(home.store.path).read_text())
    data["resumed_at"] = "2026-10-22T02:00:00Z"
    Path(home.store.path).write_text(json.dumps(data))

    home.tick("2026-10-20T02:00")
    assert home.device("runtime_role") == "ac_input"
    home.tick("2026-10-20T03:00")
    assert home.device("runtime_role") == "ac_input"
    home.tick("2026-10-20T05:00")

    assert home.device("runtime_role") == "ac_output"


def test_a_rule_whose_switch_point_was_refused_says_so_until_it_is_written(home):
    rule = home.store.create({
        "name": "Midday",
        "target": {"scope": "device", "device": "WR1"},
        "action": {"id": "device_output_limit", "value": 500},
        "when": {"start": "2026-10-19T12:00", "repeat": {"freq": "daily"}},
    }, by="dashboard")
    home.config["devices"][0]["max_power"] = 400
    home.tick("2026-10-19T12:00")

    refusal = home.scheduler.status(at("2026-10-19T12:00"))["refused"][rule.id]
    assert refusal["revision"] == rule.revision and "400" in refusal["message"]

    home.config["devices"][0]["max_power"] = 800
    home.tick("2026-10-20T12:00")

    assert home.scheduler.status(at("2026-10-20T12:00"))["refused"] == {}


def test_a_rule_every_second_year_fires_again_two_years_on(home):
    home.store.create(total_limit_at("2026-11-02T06:00", 600, freq="yearly", interval=2), by="dashboard")
    home.tick("2026-11-02T06:00")
    assert home.system("max_total_power") == 600
    run_command(home.runtime, "system", "max-power", "800",
                validation_context=build_validation_context(home.config), source="emsctl")

    for day in ("2027-06-01", "2028-01-01", "2028-11-01"):
        home.tick(f"{day}T12:00")
    assert home.system("max_total_power") == 800
    home.tick("2028-11-02T06:00")

    assert home.system("max_total_power") == 600


def test_moving_a_running_windows_start_keeps_it_running(home):
    """Interleaving: a charge window runs from 02:00; at 03:00 its start is
    moved to 02:30. Now is still inside the window, so it runs on to 05:00
    instead of stopping at once and never starting again that night."""

    rule = home.store.create(_charge_window(), by="dashboard")
    home.tick("2026-10-20T02:00")
    home.edited_at = at("2026-10-20T03:00")
    home.store.replace(rule.id, _charge_window(start="2026-10-20T02:30"), revision=1, by="dashboard")

    home.tick("2026-10-20T03:00")
    assert home.device("runtime_role") == "ac_input"
    home.tick("2026-10-20T05:00")

    assert home.device("runtime_role") == "ac_output"


def test_with_no_controlled_devices_a_device_rule_is_orphaned_not_refused(home, caplog):
    home.store.create(socket_window(), by="dashboard")

    with caplog.at_level(logging.INFO):
        home.tick("2026-10-19T18:00", devices={})

    assert len(_events(caplog, "schedule_rule_orphaned")) == 1
    assert _events(caplog, "schedule_switch_refused") == []


def test_the_coming_switch_points_at_one_instant_list_the_one_that_wins_last(home):
    home.store.create(socket_window(start="2026-10-19T18:00", end="2026-10-19T20:00"), by="dashboard")
    home.store.create(socket_window(start="2026-10-19T20:00", end="2026-10-19T22:00", value="eco"), by="dashboard")
    home.tick("2026-10-19T08:00")

    at_eight = [point for point in home.scheduler.status(at("2026-10-19T08:00"))["next"]
                if point["at"].startswith("2026-10-19T20:00")]

    assert [point["kind"] for point in at_eight] == ["end", "start"]


def test_a_window_that_overlaps_another_years_ahead_is_still_found(home):
    daily = home.store.create({
        "name": "Mornings", "target": {"scope": "system"},
        "action": {"id": "total_output_limit", "value": 400, "end_value": 800},
        "when": {"start": "2026-10-20T00:00", "end": "2026-10-20T12:00", "repeat": {"freq": "daily"}},
    }, by="dashboard")
    trip = home.store.create({
        "name": "Trip", "target": {"scope": "system"},
        "action": {"id": "total_output_limit", "value": 100, "end_value": 800},
        "when": {"start": "2028-01-01T06:00", "end": "2028-01-10T00:00"},
    }, by="dashboard")

    other, start = window_clash(trip, home.store.load(), BERLIN, at("2026-10-19T08:00"))

    assert other.id == daily.id and start == at("2028-01-01T06:00")


def test_a_yearly_window_meets_a_weekly_one_when_its_date_falls_on_that_weekday(home):
    sundays = home.store.create({
        "name": "Sundays", "target": {"scope": "system"},
        "action": {"id": "total_output_limit", "value": 400, "end_value": 800},
        "when": {"start": "2026-10-25T11:00", "end": "2026-10-25T13:00",
                 "repeat": {"freq": "weekly", "by_weekday": ["su"]}},
    }, by="dashboard")
    first_of_february = home.store.create({
        "name": "1 February", "target": {"scope": "system"},
        "action": {"id": "total_output_limit", "value": 100, "end_value": 800},
        "when": {"start": "2027-02-01T10:00", "end": "2027-02-01T12:00", "repeat": {"freq": "yearly"}},
    }, by="dashboard")

    other, start = window_clash(first_of_february, home.store.load(), BERLIN, at("2026-10-19T08:00"))

    assert other.id == sundays.id and start == at("2032-02-01T11:00")


def test_a_serial_two_configured_devices_share_moves_no_rule_between_them(home):
    rule = socket_window()
    rule["target"]["device"] = "Garage"
    created = home.store.create(rule, by="dashboard")

    home.tick("2026-10-19T18:00", devices={"WR1": "SN-TEST-0001", "WR2": "SN-TEST-0001"})

    assert home.store.load().rule(created.id).device == "Garage"


def test_a_window_end_refused_while_the_store_is_unreadable_is_tried_again(home, monkeypatch):
    import ems.scheduler as scheduler_module
    from ems.runtime_commands import RuntimeWriteError

    home.store.create(socket_window(start="2026-10-19T18:00", end="2026-10-19T20:00"), by="dashboard")
    home.tick("2026-10-19T18:00")
    Path(home.store.path).write_text("{broken")
    home.tick("2026-10-19T19:00")
    real = scheduler_module.run_commands
    refusals = []

    def refused_twice(*args, **kwargs):
        if len(refusals) < 2:
            refusals.append(args)
            raise RuntimeWriteError("offgrid_socket_mode is not supported")
        return real(*args, **kwargs)

    monkeypatch.setattr("ems.scheduler.run_commands", refused_twice)
    for moment in ("2026-10-19T20:00", "2026-10-19T20:00:05", "2026-10-19T20:00:10"):
        home.tick(moment)

    assert len(refusals) == 2
    assert home.device("offgrid_socket_mode") == "off"


def test_a_clash_hidden_by_a_skipped_date_in_the_first_cycle_is_still_found(home):
    daily = home.store.create({
        "name": "Evenings", "target": {"scope": "system"},
        "action": {"id": "total_output_limit", "value": 400, "end_value": 800},
        "when": {"start": "2026-10-19T18:00", "end": "2026-10-19T20:00", "repeat": {"freq": "daily"},
                 "exceptions": ["2026-10-24"]},
    }, by="dashboard")
    saturdays = home.store.create({
        "name": "Saturdays", "target": {"scope": "system"},
        "action": {"id": "total_output_limit", "value": 100, "end_value": 800},
        "when": {"start": "2026-10-19T19:00", "end": "2026-10-19T21:00",
                 "repeat": {"freq": "weekly", "by_weekday": ["sa"]}},
    }, by="dashboard")

    other, start = window_clash(saturdays, home.store.load(), BERLIN, at("2026-10-19T08:00"))

    assert other.id == daily.id and start == at("2026-10-31T19:00")


def test_a_repeat_whose_until_has_passed_is_refused_as_never_firing(home):
    from ems.schedule_model import ScheduleRuleError

    with pytest.raises(ScheduleRuleError, match="this repeat has ended"):
        _check(home, total_limit_at("2026-10-01T06:00", 600, freq="daily", until="2026-10-05"))
