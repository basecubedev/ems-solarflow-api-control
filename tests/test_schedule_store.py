# SPDX-License-Identifier: AGPL-3.0-or-later
"""schedules.json has one writer, and an edit never lands on a newer rule.

The dashboard and emsctl both edit the rules, from two processes; the
scheduler records what it applied. Each change re-reads the file, and an edit
made against an older revision is refused instead of saved over the newer one.
"""

import json
from datetime import date, datetime, timezone

import pytest

from ems.schedule_model import ScheduleRuleError, parse_rule, rule_payload
from ems.schedule_store import (
    MAX_RULES,
    BrokenRule,
    ScheduleConflict,
    ScheduleNotFound,
    ScheduleStore,
    ScheduleStoreError,
)

pytestmark = [pytest.mark.contract, pytest.mark.config]

NOW = datetime(2026, 10, 18, 14, 2, 11, tzinfo=timezone.utc)


def _store(tmp_path):
    return ScheduleStore(tmp_path / "schedules.json", clock=lambda: NOW)


def _socket_rule(**overrides):
    rule = {
        "name": "Evening socket",
        "target": {"scope": "device", "device": "WR1", "identity": "SN-TEST-0001"},
        "action": {"id": "offgrid_socket", "value": "standard", "end_value": "off"},
        "when": {
            "start": "2026-10-19T18:00",
            "end": "2026-10-19T23:00",
            "repeat": {"freq": "weekly", "by_weekday": ["mo", "tu", "we", "th", "fr"]},
        },
    }
    rule.update(overrides)
    return rule


def test_a_new_rule_gets_its_id_its_first_revision_and_who_made_it(tmp_path):
    store = _store(tmp_path)

    rule = store.create(_socket_rule(), by="dashboard")

    assert rule.id.startswith("r_") and len(rule.id) == 10
    assert (rule.revision, rule.updated_at, rule.updated_by) == (1, NOW, "dashboard")
    on_disk = json.loads((tmp_path / "schedules.json").read_text())
    assert on_disk["schema_version"] == 1
    assert on_disk["rules"][0]["id"] == rule.id
    assert store.load().rule(rule.id) == rule


def test_an_edit_against_an_older_revision_is_refused_and_changes_nothing(tmp_path):
    store = _store(tmp_path)
    rule = store.create(_socket_rule(), by="dashboard")
    store.replace(rule.id, _socket_rule(name="Evening"), revision=1, by="emsctl")
    before = (tmp_path / "schedules.json").read_bytes()

    with pytest.raises(ScheduleConflict, match="revision 2, not 1"):
        store.replace(rule.id, _socket_rule(name="Late"), revision=1, by="dashboard")
    with pytest.raises(ScheduleConflict):
        store.delete(rule.id, revision=1)

    assert (tmp_path / "schedules.json").read_bytes() == before
    assert store.load().rule(rule.id).name == "Evening"


def test_replacing_and_deleting_at_the_current_revision(tmp_path):
    store = _store(tmp_path)
    rule = store.create(_socket_rule(), by="dashboard")

    replaced = store.replace(rule.id, _socket_rule(name="Evening"), revision=1, by="emsctl")
    assert (replaced.revision, replaced.updated_by, replaced.name) == (2, "emsctl", "Evening")

    store.delete(rule.id, revision=2)
    assert store.load().rules == ()
    with pytest.raises(ScheduleNotFound):
        store.delete(rule.id, revision=2)


def test_two_writers_each_keep_the_other_ones_rule(tmp_path):
    """Interleaving: emsctl creates a rule, then the dashboard's store, which
    loaded before that, creates one too."""

    dashboard = _store(tmp_path)
    dashboard.load()
    emsctl_store = _store(tmp_path)

    first = emsctl_store.create(_socket_rule(name="From the shell"), by="emsctl")
    second = dashboard.create(_socket_rule(name="From the page"), by="dashboard")

    assert [rule.id for rule in dashboard.load().rules] == [first.id, second.id]


def test_a_save_leaves_another_writers_temp_file_alone(tmp_path):
    store = _store(tmp_path)
    foreign = tmp_path / "schedules.json.tmp"
    foreign.write_text('{"half": ')

    store.create(_socket_rule(), by="dashboard")

    assert foreign.read_text() == '{"half": '
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [
        "schedules.json",
        "schedules.json.lock",
        "schedules.json.tmp",
    ]


def test_moving_a_rule_changes_only_the_order(tmp_path):
    store = _store(tmp_path)
    ids = [store.create(_socket_rule(name=name), by="dashboard").id for name in ("A", "B", "C")]

    store.move(ids[2], "up")
    store.move(ids[0], "up")
    store.move(ids[0], "down")

    assert [rule.id for rule in store.load().rules] == [ids[2], ids[0], ids[1]]
    with pytest.raises(ScheduleRuleError, match="up or down"):
        store.move(ids[0], "left")


def test_skipping_a_day_adds_it_to_the_rule_and_bumps_the_revision(tmp_path):
    store = _store(tmp_path)
    rule = store.create(_socket_rule(), by="dashboard")

    skipped = store.skip(rule.id, date(2026, 12, 24), revision=1, by="dashboard")

    assert skipped.revision == 2
    assert date(2026, 12, 24) in skipped.when.exceptions


def test_the_pause_switch_and_the_applied_record_are_kept(tmp_path):
    store = _store(tmp_path)
    rule = store.create(_socket_rule(), by="dashboard")

    store.set_enabled(False)
    store.record_applied(
        "devices.WR1.offgrid_socket_mode",
        rule_id=rule.id,
        at=datetime(2026, 10, 19, 16, 0, tzinfo=timezone.utc),
        value="standard",
    )

    book = store.load()
    assert book.enabled is False
    assert book.resumed_at is None
    assert book.applied == {
        "devices.WR1.offgrid_socket_mode": {
            "rule": rule.id,
            "at": datetime(2026, 10, 19, 16, 0, tzinfo=timezone.utc),
            "value": "standard",
        }
    }


def test_a_device_renamed_in_config_is_followed_by_its_identity(tmp_path):
    store = _store(tmp_path)
    rule = store.create(_socket_rule(), by="dashboard")

    assert store.rename_devices({"SN-TEST-0001": "Garage"}) is True
    assert store.rename_devices({"SN-TEST-0001": "Garage"}) is False

    assert store.load().rule(rule.id).device == "Garage"


@pytest.mark.parametrize(
    "content,message",
    [
        ("{not json", "cannot read schedules"),
        (json.dumps({"schema_version": 2, "rules": []}), "schema_version 1"),
        (json.dumps({"schema_version": 1, "rules": {}}), "rules must be a list"),
        (json.dumps({"schema_version": 1, "enabled": "yes"}), "enabled must be true or false"),
    ],
)
def test_a_file_that_is_not_schedules_is_refused_and_never_overwritten(tmp_path, content, message):
    path = tmp_path / "schedules.json"
    path.write_text(content)
    store = _store(tmp_path)

    with pytest.raises(ScheduleStoreError, match=message):
        store.load()
    with pytest.raises(ScheduleStoreError, match=message):
        store.create(_socket_rule(), by="dashboard")

    assert path.read_text() == content


def test_a_broken_entry_is_shown_as_broken_and_the_others_still_load(tmp_path):
    store = _store(tmp_path)
    good = store.create(_socket_rule(), by="dashboard")
    data = json.loads((tmp_path / "schedules.json").read_text())
    data["rules"].append({"id": "r_deadbeef", "name": "hand edited"})
    (tmp_path / "schedules.json").write_text(json.dumps(data))

    rules = store.load().rules

    assert rules[0] == good
    assert isinstance(rules[1], BrokenRule) and rules[1].id == "r_deadbeef"
    store.delete("r_deadbeef", revision=None)
    assert store.load().rules == (good,)


def test_the_number_of_rules_is_capped(tmp_path):
    store = _store(tmp_path)
    data = {"schema_version": 1, "enabled": True, "applied": {}, "rules": []}
    template = rule_payload(store.create(_socket_rule(), by="dashboard"))
    for index in range(MAX_RULES):
        data["rules"].append({**template, "id": f"r_{index:08x}"})
    (tmp_path / "schedules.json").write_text(json.dumps(data))

    with pytest.raises(ScheduleRuleError, match=f"at most {MAX_RULES}"):
        store.create(_socket_rule(), by="dashboard")


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"name": ""}, "name must be 1 to 80"),
        ({"name": "x" * 81}, "name must be 1 to 80"),
        ({"enabled": "yes"}, "enabled must be true or false"),
        ({"colour": "red"}, "unknown rule field: colour"),
        ({"action": {"id": "offgrid_socket", "value": "standard"}}, "needs the end_value"),
        ({"action": {"id": "reboot", "value": 1}}, "unknown schedule action reboot"),
        ({"action": {"id": "offgrid_socket", "value": "always", "end_value": "off"}}, "takes one of"),
        ({"target": {"scope": "system"}}, "offgrid_socket applies to the device"),
        ({"target": {"scope": "device", "device": ""}}, "device must be 1 to 64"),
        ({"when": {"start": "tomorrow"}}, "start must be a local time"),
    ],
)
def test_a_rule_the_store_cannot_keep_is_refused_with_its_reason(tmp_path, overrides, message):
    with pytest.raises(ScheduleRuleError, match=message):
        _store(tmp_path).create(_socket_rule(**overrides), by="dashboard")
    assert not (tmp_path / "schedules.json").exists()


def test_a_single_switch_point_has_no_end_value():
    rule = _socket_rule(
        action={"id": "total_output_limit", "value": 1000, "end_value": 800},
        target={"scope": "system"},
        when={"start": "2026-10-19T06:00", "repeat": {"freq": "weekly", "by_weekday": ["mo"]}},
    )

    with pytest.raises(ScheduleRuleError, match="single switch point has no end_value"):
        ScheduleStore("/nonexistent/schedules.json").create(rule, by="dashboard")


def test_only_the_dashboard_and_emsctl_edit_rules(tmp_path):
    with pytest.raises(ScheduleRuleError, match="an edit comes from one of: dashboard, emsctl"):
        _store(tmp_path).create(_socket_rule(), by="schedule")


def test_a_stored_rule_reads_back_as_written(tmp_path):
    rule = _store(tmp_path).create(_socket_rule(), by="dashboard")

    assert parse_rule(rule_payload(rule)) == rule


def test_a_resume_records_when_it_happened_so_the_pause_fires_nothing_late(tmp_path):
    store = _store(tmp_path)
    store.set_enabled(True)
    assert store.load().resumed_at is None

    store.set_enabled(False)
    store.set_enabled(True)

    assert store.load().resumed_at == NOW


def test_a_change_holds_a_lock_another_process_has_to_wait_for(tmp_path):
    """Interleaving: emsctl changes the rules while the EMS records what it
    applied. Each reads, changes and replaces the whole file, so each holds a
    lock on schedules.json.lock that another process cannot take meanwhile."""

    import fcntl

    store = _store(tmp_path)
    store.create(_socket_rule(), by="dashboard")
    with store._exclusive():
        with open(tmp_path / "schedules.json.lock", "a") as other_process:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other_process, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with open(tmp_path / "schedules.json.lock", "a") as other_process:
        fcntl.flock(other_process, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize(
    "change",
    [
        lambda store, rule: store.create(_socket_rule(name="Second"), by="emsctl"),
        lambda store, rule: store.replace(rule.id, _socket_rule(name="B"), revision=1, by="emsctl"),
        lambda store, rule: store.delete(rule.id, revision=1),
        lambda store, rule: store.move(rule.id, "up"),
        lambda store, rule: store.skip(rule.id, date(2026, 12, 24), revision=1, by="emsctl"),
        lambda store, rule: store.set_enabled(False),
        lambda store, rule: store.record_applied("system.max_total_power", rule_id=rule.id, at=NOW, value=1),
        lambda store, rule: store.rename_devices({"SN-TEST-0001": "Garage"}),
    ],
)
def test_every_change_takes_the_lock(tmp_path, monkeypatch, change):
    store = _store(tmp_path)
    rule = store.create(_socket_rule(), by="dashboard")
    taken = []
    real = store._exclusive

    def recording():
        taken.append(True)
        return real()

    monkeypatch.setattr(store, "_exclusive", recording)

    change(store, rule)

    assert taken == [True]


def test_an_entry_without_an_id_can_still_be_deleted(tmp_path):
    store = _store(tmp_path)
    good = store.create(_socket_rule(), by="dashboard")
    data = json.loads((tmp_path / "schedules.json").read_text())
    data["rules"].append({"name": "no id at all"})
    (tmp_path / "schedules.json").write_text(json.dumps(data))

    broken = store.load().rules[1]
    assert isinstance(broken, BrokenRule) and broken.id.startswith("x_")

    store.delete(broken.id, revision=None)
    assert store.load().rules == (good,)


@pytest.mark.parametrize(
    "broken",
    [
        {"target": {"identity": "SN-TEST-0001", "device": ["WR1"]}},
        {"target": {"identity": "SN-TEST-0001"}},
        {"target": {"identity": ["SN-TEST-0001"], "device": "WR1"}},
        {"target": {"identity": "SN-TEST-0001", "device": "WR1"}, "action": "offgrid_socket"},
        {"target": {"identity": "SN-TEST-0001", "device": "WR1"}, "action": {"id": ["offgrid_socket"]}},
    ],
    ids=["device_list", "no_device", "identity_list", "action_text", "action_id_list"],
)
def test_a_rename_passes_over_a_broken_entry_and_follows_the_good_ones(tmp_path, broken):
    store = _store(tmp_path)
    good = store.create(_socket_rule(), by="dashboard")
    data = json.loads((tmp_path / "schedules.json").read_text())
    data["rules"].append({"id": "r_0000beef", **broken})
    (tmp_path / "schedules.json").write_text(json.dumps(data))

    assert store.rename_devices({"SN-TEST-0001": "Terrasse"}) is True

    assert store.load().rule(good.id).device == "Terrasse"


def test_an_edit_of_an_entry_that_is_not_an_object_is_a_conflict_not_a_crash(tmp_path):
    store = _store(tmp_path)
    data = {"schema_version": 1, "enabled": True, "rules": ["not a rule"], "applied": {}}
    (tmp_path / "schedules.json").write_text(json.dumps(data))
    broken = store.load().rules[0]

    with pytest.raises(ScheduleConflict, match="revision None, not 1"):
        store.delete(broken.id, revision=1)


def test_a_rule_stored_without_its_enabled_flag_can_skip_a_date(tmp_path):
    store = _store(tmp_path)
    rule = store.create(_socket_rule(), by="dashboard")
    data = json.loads((tmp_path / "schedules.json").read_text())
    del data["rules"][0]["enabled"]
    (tmp_path / "schedules.json").write_text(json.dumps(data))

    skipped = store.skip(rule.id, date(2026, 10, 20), revision=1, by="emsctl")

    assert date(2026, 10, 20) in skipped.when.exceptions
    assert skipped.enabled is True


def test_recording_waits_a_bounded_time_behind_a_save_in_progress(tmp_path, monkeypatch):
    """Interleaving: a dashboard save holds the lock while its check runs;
    the control loop records a switch point. The loop must give up after the
    lock's wait, not queue behind the save for as long as it takes."""

    import threading

    monkeypatch.setattr("ems.file_io.LOCK_TIMEOUT_SECONDS", 0.05)
    store = _store(tmp_path)
    inside, release = threading.Event(), threading.Event()

    def slow_check(book):
        inside.set()
        release.wait(5)

    saver = threading.Thread(target=lambda: store.create(_socket_rule(), by="dashboard", check=slow_check))
    saver.start()
    assert inside.wait(5)
    outcome = []

    def record():
        try:
            store.record_applied("system.max_total_power", rule_id="r_0000beef", at=NOW, value=600)
        except ScheduleStoreError as exc:
            outcome.append(str(exc))

    recorder = threading.Thread(target=record)
    recorder.start()
    recorder.join(2)
    finished = not recorder.is_alive()
    release.set()
    saver.join(5)
    recorder.join(5)

    assert finished
    assert outcome and "busy" in outcome[0]


def test_dates_skipped_in_the_past_do_not_count_towards_the_limit(tmp_path):
    store = _store(tmp_path)
    rule = store.create(_socket_rule(), by="dashboard")
    data = json.loads((tmp_path / "schedules.json").read_text())
    data["rules"][0]["when"]["exceptions"] = [f"2025-01-{day:02d}" for day in range(1, 32)] + [
        f"2025-02-{day:02d}" for day in range(1, 19)] + ["2026-10-17"]
    assert len(data["rules"][0]["when"]["exceptions"]) == 50
    (tmp_path / "schedules.json").write_text(json.dumps(data))

    skipped = store.skip(rule.id, date(2026, 10, 20), revision=1, by="dashboard")

    assert sorted(skipped.when.exceptions) == [date(2026, 10, 17), date(2026, 10, 20)]


def test_a_rename_onto_a_name_a_removed_device_left_records_under_takes_the_renamed_ones(tmp_path):
    store = _store(tmp_path)
    rule = store.create(_socket_rule(target={"scope": "device", "device": "A", "identity": "SN-TEST-0001"}),
                        by="dashboard")
    data = json.loads((tmp_path / "schedules.json").read_text())
    data["applied"] = {
        "devices.B.offgrid_socket_mode": {"rule": "r_0000dead", "at": "2026-10-18T13:00:00Z", "value": "eco"},
        "devices.A.offgrid_socket_mode": {"rule": rule.id, "at": "2026-10-17T16:00:00Z", "value": "standard"},
    }
    (tmp_path / "schedules.json").write_text(json.dumps(data))

    store.rename_devices({"SN-TEST-0001": "B"})

    assert store.load().applied["devices.B.offgrid_socket_mode"]["rule"] == rule.id


def test_a_restore_that_resumes_a_paused_schedule_fires_nothing_from_the_pause(tmp_path):
    store = _store(tmp_path)
    store.create(_socket_rule(), by="dashboard")
    backed_up = (tmp_path / "schedules.json").read_bytes()
    store.set_enabled(False)

    store.restore(backed_up)

    book = store.load()
    assert book.enabled is True and book.resumed_at == NOW


def test_restored_rules_fire_nothing_from_before_the_restore(tmp_path):
    """Interleaving: a backup holds a nightly charge window saved a month
    ago; the rule was deleted since, and a charge started by hand at 10:00.
    Restored at 14:02, today's 05:00 end must not fire as a missed one."""

    from zoneinfo import ZoneInfo

    from ems.scheduler import due_switch_points

    month_ago = ScheduleStore(tmp_path / "backup.json", clock=lambda: datetime(2026, 9, 18, tzinfo=timezone.utc))
    month_ago.create({
        "name": "Cheap night", "target": {"scope": "device", "device": "WR1"},
        "action": {"id": "charge_from_grid", "value": 600, "end_value": 0},
        "when": {"start": "2026-09-19T02:00", "end": "2026-09-19T05:00", "repeat": {"freq": "daily"}},
    }, by="dashboard")
    store = _store(tmp_path)
    store.set_enabled(True)

    store.restore((tmp_path / "backup.json").read_bytes())

    assert due_switch_points(store.load(), ZoneInfo("Europe/Berlin"), NOW) == {}


def test_a_restore_over_an_unreadable_file_takes_no_applied_records_from_the_backup(tmp_path):
    store = _store(tmp_path)
    store.create(_socket_rule(), by="dashboard")
    store.record_applied("devices.WR1.offgrid_socket_mode", rule_id="r_0000beef", at=NOW, value="standard",
                         window={"action": "offgrid_socket", "device": "WR1", "end_value": "off", "ends_at": NOW})
    backed_up = (tmp_path / "schedules.json").read_bytes()
    (tmp_path / "schedules.json").write_text("{broken")

    store.restore(backed_up)

    assert store.load().applied == {}
