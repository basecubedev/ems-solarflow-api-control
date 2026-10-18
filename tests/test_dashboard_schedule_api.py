# SPDX-License-Identifier: AGPL-3.0-or-later
"""The dashboard's schedule routes answer from the EMS's scheduler modules.

The browser shows what these answers say -- whether a draft can be saved,
the sentence it reads as, its next switch points, which window it clashes
with -- and decides none of it.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import emsctl
from dashboard.schedule_api import RULE_STATUSES, ScheduleApi, _emsctl_line
from ems.runtime_commands import build_validation_context
from ems.schedule_actions import ACTIONS, CHARGE, FACTOR, GROUPS, SWITCH, WATTS, action_commands
from ems.runtime_state import RuntimeState
from ems.schedule_recurrence import PRESETS, localize
from ems.schedule_store import ScheduleStore
from tests._emsctl_test_helpers import write_config

pytestmark = [pytest.mark.contract, pytest.mark.config]

BERLIN = ZoneInfo("Europe/Berlin")


def at(text):
    return localize(datetime.fromisoformat(text), BERLIN).astimezone(timezone.utc)


@pytest.fixture
def api(tmp_path):
    write_config(tmp_path / "config.json")
    config = json.loads((tmp_path / "config.json").read_text())
    assert emsctl.main(["--config", str(tmp_path / "config.json"), "--runtime-state",
                        str(tmp_path / "runtime-state.json"), "status"]) == 0
    runtime = RuntimeState(str(tmp_path / "runtime-state.json"), {"devices": {}})
    runtime.load_if_changed(force=True)
    clock = {"now": at("2026-10-19T08:00")}
    store = ScheduleStore(tmp_path / "schedules.json", clock=lambda: clock["now"])
    api = ScheduleApi(
        store,
        runtime_state=runtime,
        devices=lambda: {"WR1": "SN-TEST-0001"},
        validation_context=lambda: build_validation_context(config),
        zone=BERLIN,
        clock=lambda: clock["now"],
    )
    api.clock_state = clock
    api.runtime_path = tmp_path / "runtime-state.json"
    api.config = config
    return api


def socket_window(**overrides):
    rule = {
        "name": "Evening socket",
        "target": {"scope": "device", "device": "WR1"},
        "action": {"id": "offgrid_socket", "value": "standard", "end_value": "off"},
        "when": {"start": "2026-10-19T18:00", "end": "2026-10-19T23:00",
                 "repeat": {"freq": "weekly", "by_weekday": ["mo", "tu", "we", "th", "fr"]}},
    }
    rule.update(overrides)
    return rule


def test_a_rule_is_created_and_listed_with_its_sentence_and_next_switch_point(api):
    status, created = api.handle("POST", "/api/schedule/rules", {"rule": socket_window()})

    assert status == 201
    assert created["summary"] == "Every weekday, 18:00–23:00, from Mon 19 Oct 2026"
    assert created["action_summary"] == "WR1 · Off-grid socket standard, then off"
    assert created["status"] == "waiting"
    assert created["next"]["at"] == "2026-10-19T18:00:00+02:00"
    status, overview = api.handle("GET", "/api/schedule")
    assert status == 200
    assert overview["zone"] == "Europe/Berlin" and overview["enabled"] is True
    assert overview["zone_offset_minutes"] == 120
    assert [rule["id"] for rule in overview["rules"]] == [created["id"]]
    assert overview["next"][0]["text"] == "standard"
    assert {action["id"] for action in overview["actions"]} >= {"offgrid_socket", "charge_from_grid"}
    assert [group["id"] for group in overview["groups"]] == list(GROUPS)
    assert {action["group"] for action in overview["actions"]} <= set(GROUPS)


def test_an_edit_against_an_older_revision_is_answered_409(api):
    _, created = api.handle("POST", "/api/schedule/rules", {"rule": socket_window()})
    rule_id = created["id"]
    assert api.handle("PUT", f"/api/schedule/rules/{rule_id}",
                      {"revision": 1, "rule": socket_window(name="Evening")})[0] == 200

    status, answer = api.handle("PUT", f"/api/schedule/rules/{rule_id}", {"revision": 1, "rule": socket_window()})

    assert status == 409 and answer["error"] == "schedule_conflict"
    assert api.handle("DELETE", f"/api/schedule/rules/{rule_id}", query="revision=1")[0] == 409
    assert api.handle("DELETE", f"/api/schedule/rules/{rule_id}", query="revision=2")[0] == 200


@pytest.mark.parametrize(
    "rule,message",
    [
        (socket_window(target={"scope": "device", "device": "WR9"}), "unknown device WR9"),
        (socket_window(action={"id": "device_output_limit", "value": 900, "end_value": 0}), "between 0 and 800"),
        (socket_window(when={"start": "2026-10-18T18:00", "end": "2026-10-18T23:00"}), "lies in the past"),
        ({"name": "x"}, "when must be an object"),
    ],
)
def test_a_rule_the_installation_cannot_carry_out_is_answered_400_and_not_saved(api, rule, message):
    status, answer = api.handle("POST", "/api/schedule/rules", {"rule": rule})

    assert status == 400 and answer["error"] == "invalid_schedule_rule"
    assert message in answer["message"]
    assert api.store.load().rules == ()


def test_a_preview_says_what_a_draft_would_do_and_writes_nothing(api):
    answer = api.handle("POST", "/api/schedule/preview", {"rule": socket_window()})[1]

    assert answer["ok"] is True
    assert answer["equals"] == ["emsctl device WR1 offgrid standard", "emsctl device WR1 offgrid off"]
    assert [point["at"] for point in answer["next"][:2]] == ["2026-10-19T18:00:00+02:00", "2026-10-19T23:00:00+02:00"]
    assert answer["first_action"] == "2026-10-19T18:00:00+02:00"
    assert api.store.load().rules == ()


def test_a_preview_of_a_clashing_window_names_the_other_rule(api):
    api.handle("POST", "/api/schedule/rules", {"rule": socket_window()})

    answer = api.handle("POST", "/api/schedule/preview", {
        "rule": socket_window(name="Late", when={"start": "2026-10-19T22:00", "end": "2026-10-19T23:30"})})[1]

    assert answer == {"ok": False, "message": answer["message"]}
    assert "overlaps 'Evening socket' on Mon 19 Oct 2026 22:00" in answer["message"]


def test_skip_move_pause_and_apply_now(api):
    _, first = api.handle("POST", "/api/schedule/rules", {"rule": socket_window()})
    _, second = api.handle("POST", "/api/schedule/rules", {"rule": {
        "name": "Winter on", "target": {"scope": "system"}, "action": {"id": "winter_mode", "value": True},
        "when": {"start": "2026-11-01T00:00"}}})

    skipped = api.handle("POST", f"/api/schedule/rules/{first['id']}/skip", {"revision": 1})[1]
    assert skipped["when"]["exceptions"] == ["2026-10-19"]
    assert skipped["next"]["at"] == "2026-10-20T18:00:00+02:00"
    moved = api.handle("POST", f"/api/schedule/rules/{second['id']}/move", {"direction": "up"})[1]
    assert [rule["id"] for rule in moved["rules"]] == [second["id"], first["id"]]
    paused = api.handle("PATCH", "/api/schedule", {"enabled": False})[1]
    assert paused["enabled"] is False and {rule["status"] for rule in paused["rules"]} == {"paused"}

    api.clock_state["now"] = at("2026-10-20T19:00")
    status, refused = api.handle("POST", f"/api/schedule/rules/{first['id']}/apply-now", {})
    assert (status, refused["message"]) == (400, "switch the rule on, and the schedule, to apply it now")
    api.handle("PATCH", "/api/schedule", {"enabled": True})
    applied = api.handle("POST", f"/api/schedule/rules/{first['id']}/apply-now", {})[1]
    assert applied == {"applied": first["id"], "value": "standard", "text": "standard"}
    device = json.loads(api.runtime_path.read_text())["devices"]["WR1"]
    assert device["offgrid_socket_mode"] == "standard"


def test_a_charge_is_not_offered_for_a_device_on_mqtt(api):
    api.config["devices"][0].update(
        {"type": "zendure_mqtt", "capabilities": {"write_output_limit": True},
         "mqtt": {"broker_ref": "home", "device_id": "D1"}}
    )

    actions = {action["id"]: action for action in api.handle("GET", "/api/schedule")[1]["actions"]}

    assert actions["charge_from_grid"]["devices"] == []
    assert actions["offgrid_socket"]["devices"] == ["WR1"]


def test_an_unknown_route_rule_or_unreadable_store(api):
    assert api.handle("GET", "/api/schedule/nothing")[0] == 404
    assert api.handle("POST", "/api/schedule/rules/r_00000000/skip", {"revision": 1})[0] == 404
    assert api.handle("PATCH", "/api/schedule", {"enabled": "yes"})[0] == 400
    with open(api.store.path, "w") as handle:
        handle.write("{broken")
    assert api.handle("GET", "/api/schedule")[0] == 503


@pytest.mark.parametrize(
    "spoil",
    [
        lambda api: api.config["devices"][0].update(max_power=400),
        lambda api: api.config["devices"].clear(),
    ],
)
def test_a_rule_the_installation_no_longer_takes_can_still_be_switched_off(api, spoil):
    rule = socket_window(action={"id": "device_output_limit", "value": 600, "end_value": 0})
    _, created = api.handle("POST", "/api/schedule/rules", {"rule": rule})
    spoil(api)
    if not api.config["devices"]:
        api.devices = lambda: {}

    status, switched = api.handle("PUT", f"/api/schedule/rules/{created['id']}",
                                  {"revision": 1, "rule": {**rule, "enabled": False}})

    assert status == 200 and switched["enabled"] is False
    status, answer = api.handle("PUT", f"/api/schedule/rules/{created['id']}",
                                {"revision": 2, "rule": {**rule, "enabled": True}})
    assert status == 400


def test_the_serial_is_taken_from_the_installation_not_from_the_page(api):
    rule = socket_window()
    rule["target"] = {"scope": "device", "device": "WR1", "identity": "SN-FROM-THE-PAGE"}

    created = api.handle("POST", "/api/schedule/rules", {"rule": rule})[1]

    assert created["target"]["identity"] == "SN-TEST-0001"


def test_a_body_of_the_wrong_shape_is_answered_400_not_dropped(api):
    rule = socket_window()
    rule["target"] = {"scope": "device", "device": ["WR1"]}

    status, answer = api.handle("POST", "/api/schedule/rules", {"rule": rule})

    assert status == 400 and answer["error"] == "invalid_schedule_rule"


def test_a_store_that_cannot_be_written_is_answered_503(api, monkeypatch):
    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(api.store, "_write", full_disk)

    status, answer = api.handle("POST", "/api/schedule/rules", {"rule": socket_window()})

    assert (status, answer["error"]) == (503, "schedule_store_unwritable")


def test_without_a_zone_the_scheduler_can_follow_rules_can_be_listed_paused_and_deleted_only(api):
    _, created = api.handle("POST", "/api/schedule/rules", {"rule": socket_window()})
    api.zone = None

    status, overview = api.handle("GET", "/api/schedule")
    assert status == 200 and overview["zone"] == "unsupported" and "POSIX" in overview["zone_message"]
    assert overview["zone_offset_minutes"] is None
    assert overview["rules"][0]["status"] == "paused" and overview["next"] == []
    assert api.handle("PATCH", "/api/schedule", {"enabled": False})[0] == 200
    for method, path in (("POST", "/api/schedule/preview"), ("POST", "/api/schedule/rules"),
                         ("POST", f"/api/schedule/rules/{created['id']}/apply-now"),
                         ("POST", f"/api/schedule/rules/{created['id']}/skip")):
        status, answer = api.handle(method, path, {"rule": socket_window(), "revision": 1})
        assert (status, answer["error"]) == (503, "schedule_zone_unsupported"), path
    assert api.handle("DELETE", f"/api/schedule/rules/{created['id']}", None, "revision=1")[0] == 200


def test_a_preview_keeps_the_edited_rule_at_its_place_in_the_list(api):
    top = {"name": "Top", "target": {"scope": "system"}, "action": {"id": "total_output_limit", "value": 900},
           "when": {"start": "2026-10-19T09:00", "repeat": {"freq": "daily"}}}
    below = {**top, "name": "Below", "action": {"id": "total_output_limit", "value": 300}}
    _, first = api.handle("POST", "/api/schedule/rules", {"rule": top})
    api.handle("POST", "/api/schedule/rules", {"rule": below})

    answer = api.handle("POST", "/api/schedule/preview", {"rule": top, "id": first["id"]})[1]

    assert [point["name"] for point in answer["timeline"][:2]] == ["Below", "Top"]


def test_a_broken_entry_with_an_odd_id_can_be_deleted_by_its_encoded_id(api):
    import json as json_module

    api.store.create(socket_window(), by="dashboard")
    data = json_module.loads(open(api.store.path).read())
    data["rules"].append({"id": "Abend Regel", "name": "hand edited"})
    open(api.store.path, "w").write(json_module.dumps(data))

    status, _ = api.handle("DELETE", "/api/schedule/rules/Abend%20Regel", query="revision=")

    assert status == 200
    assert [rule.id for rule in api.store.load().rules if rule.id == "Abend Regel"] == []


def test_the_save_check_runs_on_the_rules_read_under_the_lock(api, monkeypatch):
    """Interleaving: emsctl adds an overlapping window after the dashboard
    last read the rules; the dashboard's save is checked against it."""

    import fcntl

    from ems.schedule_store import ScheduleStore

    ScheduleStore(api.store.path, clock=lambda: at("2026-10-19T08:00")).create(socket_window(name="Shell"), by="emsctl")
    under_lock = []
    real = api.store.create

    def create(payload, *, by, check=None):
        def checking(book):
            with open(f"{api.store.path}.lock") as other_process:
                try:
                    fcntl.flock(other_process, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    under_lock.append(True)
            check(book)

        return real(payload, by=by, check=checking)

    monkeypatch.setattr(api.store, "create", create)

    status, answer = api.handle("POST", "/api/schedule/rules", {"rule": socket_window(
        name="Page", when={"start": "2026-10-19T22:00", "end": "2026-10-19T23:30"})})

    assert under_lock == [True]
    assert status == 400 and "overlaps 'Shell'" in answer["message"]


def test_a_paused_schedule_lists_nothing_next_while_the_editor_still_previews(api):
    _, created = api.handle("POST", "/api/schedule/rules", {"rule": socket_window()})
    api.handle("PATCH", "/api/schedule", {"enabled": False})

    overview = api.handle("GET", "/api/schedule")[1]
    preview = api.handle("POST", "/api/schedule/preview", {"id": created["id"], "rule": socket_window()})[1]

    assert overview["enabled"] is False and overview["next"] == []
    assert preview["timeline"][0]["at"] == "2026-10-19T18:00:00+02:00"


SAMPLE_VALUES = {SWITCH: (True, False), WATTS: (0, 600), FACTOR: (1.5,), CHARGE: (0, 600)}


@pytest.mark.parametrize("action", ACTIONS, ids=lambda action: action.id)
def test_every_emsctl_line_the_editor_shows_runs_the_same_commands_in_emsctl(action):
    parser = emsctl.build_parser()
    device = "WR1" if action.per_device else None
    for value in SAMPLE_VALUES.get(action.value_type, action.choices):
        rule = SimpleNamespace(action=action, device=device)
        lines = _emsctl_line(rule, value).split(" && ")
        commands = action_commands(action, value)
        assert len(lines) == len(commands)
        for line, (command, argument) in zip(lines, commands):
            words = line.split()
            assert words[0] == "emsctl", line
            args = parser.parse_args(words[1:])
            assert emsctl.RUNTIME_COMMAND_SCOPES[args.command] == action.scope, line
            assert getattr(args, "name", None) == device, line
            assert (args.action, getattr(args, "value", None)) == (
                command, None if argument is None else str(argument)), line


def test_apply_now_while_another_process_holds_the_runtime_state_says_busy(api, monkeypatch):
    import fcntl

    monkeypatch.setattr("ems.runtime_state.LOCK_TIMEOUT_SECONDS", 0.05)
    _, created = api.handle("POST", "/api/schedule/rules", {"rule": socket_window()})
    with open(f"{api.runtime_path}.lock", "a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        status, answer = api.handle("POST", f"/api/schedule/rules/{created['id']}/apply-now", {})

    assert (status, answer["error"]) == (503, "schedule_busy")


def test_apply_now_that_cannot_save_the_runtime_state_names_the_runtime_state(api, monkeypatch):
    _, created = api.handle("POST", "/api/schedule/rules", {"rule": socket_window()})

    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(api.runtime_state, "update_device", full_disk)
    status, answer = api.handle("POST", f"/api/schedule/rules/{created['id']}/apply-now", {})

    assert (status, answer["error"]) == (503, "runtime_state_unwritable")


def test_a_rule_the_scheduler_could_not_write_reads_refused_with_the_reason(api):
    _, created = api.handle("POST", "/api/schedule/rules", {"rule": socket_window()})
    api.scheduler = SimpleNamespace(status=lambda now: {"refused": {
        created["id"]: {"revision": created["revision"], "message": "offgrid_socket_mode is not supported"}}})

    card = api.handle("GET", "/api/schedule")[1]["rules"][0]

    assert card["status"] == "refused"
    assert card["message"] == "Not written: offgrid_socket_mode is not supported"
    api.handle("PUT", f"/api/schedule/rules/{created['id']}", {"revision": 1, "rule": socket_window(name="Fixed")})
    assert api.handle("GET", "/api/schedule")[1]["rules"][0]["status"] == "waiting"


def test_an_edit_of_a_rule_on_a_device_not_in_config_keeps_its_serial(api):
    rule = api.store.create({
        "name": "Garage socket",
        "target": {"scope": "device", "device": "Garage", "identity": "SN-GARAGE-0001"},
        "action": {"id": "offgrid_socket", "value": "standard"},
        "when": {"start": "2026-10-19T18:00"},
    }, by="emsctl")
    payload = {**socket_window(), "target": {"scope": "device", "device": "Garage"}, "enabled": False}

    status, _ = api.handle("PUT", f"/api/schedule/rules/{rule.id}", {"revision": 1, "rule": payload})

    assert status == 200
    assert api.store.load().rule(rule.id).identity == "SN-GARAGE-0001"


def test_a_rule_years_apart_is_waiting_not_finished_and_can_skip_its_next_date(api):
    _, created = api.handle("POST", "/api/schedule/rules", {"rule": {
        "name": "Every other year", "target": {"scope": "system"},
        "action": {"id": "total_output_limit", "value": 600},
        "when": {"start": "2026-10-01T06:00", "repeat": {"freq": "yearly", "interval": 2}}}})

    assert created["status"] == "waiting"
    assert created["next"]["at"].startswith("2028-10-01T06:00")
    skipped = api.handle("POST", f"/api/schedule/rules/{created['id']}/skip", {"revision": 1})[1]
    assert skipped["when"]["exceptions"] == ["2028-10-01"]


def _js_object_keys(name):
    source = (Path(__file__).resolve().parent.parent / "dashboard" / "static" / "app.js").read_text()
    body = re.search(rf"const {name} = \{{(.*?)\n\}};", source, re.S).group(1)
    return set(re.findall(r"^\s*(\w+):", body, re.M))


def test_the_editor_names_every_preset_the_ems_offers_and_custom():
    assert _js_object_keys("SCHEDULE_PRESET_LABELS") == set(PRESETS) | {"custom"}


def test_the_rule_card_styles_every_status_a_rule_can_have():
    assert _js_object_keys("SCHEDULE_STATUS_TONE") == set(RULE_STATUSES)


def test_apply_now_inside_a_window_is_ended_when_the_rule_goes_away(api):
    """Interleaving: at 19:00 a person presses Apply now on an 18:00-23:00
    window, then deletes the rule at 20:00. Recorded as the window's start,
    the window still ends, at once, instead of staying on without its end."""

    from ems.scheduler import Scheduler

    _, created = api.handle("POST", "/api/schedule/rules", {"rule": socket_window()})
    api.clock_state["now"] = at("2026-10-19T19:00")
    api.handle("POST", f"/api/schedule/rules/{created['id']}/apply-now", {})
    record = api.store.load().applied["devices.WR1.offgrid_socket_mode"]
    assert record["window"]["ends_at"] == at("2026-10-19T23:00")

    api.handle("DELETE", f"/api/schedule/rules/{created['id']}", None, "revision=1")
    scheduler = Scheduler(api.store, api.runtime_state, zone=BERLIN,
                          validation_context=lambda: build_validation_context(api.config))
    scheduler.tick(at("2026-10-19T20:00"))

    assert json.loads(api.runtime_path.read_text())["devices"]["WR1"]["offgrid_socket_mode"] == "off"


def test_apply_now_on_a_rule_switched_off_is_refused_not_undone_a_cycle_later(api):
    _, created = api.handle("POST", "/api/schedule/rules", {"rule": {**socket_window(), "enabled": False}})
    api.clock_state["now"] = at("2026-10-19T19:00")

    status, answer = api.handle("POST", f"/api/schedule/rules/{created['id']}/apply-now", {})

    assert status == 400 and "switch the rule on" in answer["message"]
    assert json.loads(api.runtime_path.read_text())["devices"]["WR1"]["offgrid_socket_mode"] == "off"


def test_apply_now_that_cannot_be_recorded_writes_nothing(api, monkeypatch):
    from ems.file_io import FileLockTimeout
    from ems.schedule_store import ScheduleStoreError

    _, created = api.handle("POST", "/api/schedule/rules", {"rule": socket_window()})
    api.clock_state["now"] = at("2026-10-19T19:00")

    def busy(*args, **kwargs):
        raise ScheduleStoreError("schedules are busy") from FileLockTimeout("held")

    monkeypatch.setattr(api.store, "record_applied", busy)
    status, answer = api.handle("POST", f"/api/schedule/rules/{created['id']}/apply-now", {})

    assert (status, answer["error"]) == (503, "schedule_busy")
    assert json.loads(api.runtime_path.read_text())["devices"]["WR1"]["offgrid_socket_mode"] == "off"


def test_skip_next_while_a_window_runs_leaves_out_the_running_one(api):
    _, created = api.handle("POST", "/api/schedule/rules", {"rule": socket_window()})
    api.clock_state["now"] = at("2026-10-19T19:00")

    skipped = api.handle("POST", f"/api/schedule/rules/{created['id']}/skip", {"revision": 1})[1]

    assert skipped["when"]["exceptions"] == ["2026-10-19"]


def test_without_a_zone_a_rule_can_still_be_switched_off(api):
    _, created = api.handle("POST", "/api/schedule/rules", {"rule": socket_window()})
    api.zone = None

    status, _ = api.handle("PUT", f"/api/schedule/rules/{created['id']}",
                           {"revision": 1, "rule": {**socket_window(), "enabled": False}})

    assert status == 200 and api.store.load().rule(created["id"]).enabled is False


def test_apply_now_under_a_clock_stepped_back_records_nothing_earlier_than_the_last_record(api):
    _, created = api.handle("POST", "/api/schedule/rules", {"rule": socket_window()})
    api.store.record_applied("devices.WR1.offgrid_socket_mode", rule_id=created["id"], at=at("2026-10-19T19:00"),
                             value="standard")
    api.clock_state["now"] = at("2026-10-19T18:30")

    api.handle("POST", f"/api/schedule/rules/{created['id']}/apply-now", {})

    assert api.store.load().applied["devices.WR1.offgrid_socket_mode"]["at"] == at("2026-10-19T19:00")
