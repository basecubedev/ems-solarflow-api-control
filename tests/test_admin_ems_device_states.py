# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whether the running EMS reads each mapped device as saved, told from its control snapshot."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from admin.config_runtime_overlap import resolve_runtime_state_path
from admin.ems_device_states import EMS_DEVICE_CHANGES, EMS_DEVICE_STATES, compare_external_devices, ems_device_states
from admin.test_support import write_started_ems_snapshot
from ems.control_status import CONTROL_STATUS_SCHEMA_VERSION
from ems.device_fingerprints import FINGERPRINT_VERSION, device_fingerprints, loaded_devices_record
from ems.paths import resolve_control_status_path

pytestmark = [pytest.mark.unit, pytest.mark.admin, pytest.mark.mqtt]


def external(device_id, name, **extra):
    return {"type": "external_mqtt", "name": name, "enabled": True, "mqtt": {"broker_ref": "ems-mqtt", "device_id": device_id}, **extra}


def test_each_mapped_device_is_running_waiting_or_leaving_by_what_ems_read():
    read = {"devices": [external("balkon", "Balkon"), external("garage", "Garage"), external("alt", "Alt"), external("aus", "Aus", enabled=False), {"name": "WR1", "ip": "192.0.2.1"}]}
    saved = {"devices": [external("balkon", "Balkon"), external("garage", "Garage Nord"), external("neu", "Neu"), external("aus", "Aus", enabled=False), {"name": "WR1", "ip": "192.0.2.9"}]}
    moved = external("balkon", "Balkon")
    moved["mqtt"]["broker_ref"] = "other"

    states = compare_external_devices(saved, device_fingerprints(read))

    assert states == {
        "alt": {"state": "leaving", "change": "removed", "name": "Alt", "ran": "Alt"},
        "aus": {"state": "off", "change": None, "name": "Aus", "ran": "Aus"},
        "balkon": {"state": "running", "change": None, "name": "Balkon", "ran": "Balkon"},
        "garage": {"state": "waiting", "change": "renamed", "name": "Garage Nord", "ran": "Garage"},
        "neu": {"state": "waiting", "change": "added", "name": "Neu", "ran": None},
    }
    assert compare_external_devices({"devices": [moved]}, device_fingerprints(read))["balkon"]["change"] == "changed"
    assert compare_external_devices({"devices": [external("balkon", "Balkon", max_power=600)]}, device_fingerprints(read))["balkon"]["change"] == "changed"
    assert {item["state"] for item in states.values()} <= set(EMS_DEVICE_STATES)
    assert {item["change"] for item in states.values()} - {None} <= set(EMS_DEVICE_CHANGES)


def test_switching_a_device_off_or_on_waits_for_a_restart_as_ems_builds_it_from_the_file():
    read_on = device_fingerprints({"devices": [external("balkon", "Balkon")]})
    read_off = device_fingerprints({"devices": [external("balkon", "Balkon", enabled=False)]})
    saved_off = {"devices": [external("balkon", "Balkon", enabled=False)]}
    saved_on = {"devices": [external("balkon", "Balkon")]}

    assert compare_external_devices(saved_off, read_on)["balkon"] == {"state": "waiting", "change": "changed", "name": "Balkon", "ran": "Balkon"}
    assert compare_external_devices(saved_on, read_off)["balkon"] == {"state": "waiting", "change": "changed", "name": "Balkon", "ran": "Balkon"}
    assert compare_external_devices(saved_off, read_off)["balkon"]["state"] == "off"


def test_entries_sharing_an_id_are_in_effect_only_when_ems_read_each_one_as_saved():
    def pair(on_x, on_y):
        first, second = external("balkon", "Balkon", enabled=on_x), external("balkon", "Balkon Y", enabled=on_y)
        second["mqtt"]["broker_ref"] = "other"
        return {"devices": [first, second]}

    swapped = compare_external_devices(pair(True, False), device_fingerprints(pair(False, True)))
    same = compare_external_devices(pair(True, False), device_fingerprints(pair(True, False)))
    one_left = compare_external_devices({"devices": pair(True, False)["devices"][:1]}, device_fingerprints(pair(True, False)))

    assert swapped["balkon"] == {"state": "waiting", "change": "renamed", "name": "Balkon", "ran": "Balkon Y"}
    assert same["balkon"] == {"state": "running", "change": None, "name": "Balkon", "ran": "Balkon"}
    assert one_left["balkon"]["state"] == "waiting"


def install(tmp_path, saved, *, loaded=None, age_seconds=0, schema=CONTROL_STATUS_SCHEMA_VERSION, record=None, config_text=None, runtime=None):
    config_path = tmp_path / "config.json"
    config_path.write_text(config_text if config_text is not None else json.dumps(saved))
    context = SimpleNamespace(install_root=tmp_path, config_path=config_path, config_exists=True)
    if runtime is not None:
        runtime_path = Path(resolve_runtime_state_path(context, saved))
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_path.write_text(json.dumps(runtime))
    if loaded is not None or record is not None:
        written = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        status = {"schema_version": schema, "written_at": written.isoformat(), "loop_interval_s": 5, "started_at": "2026-11-06T06:00:00+00:00"}
        status["loaded_devices"] = record if record is not None else loaded_devices_record(loaded)
        path = Path(resolve_control_status_path(resolve_runtime_state_path(context, saved)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(status))
    return context


def test_a_fresh_snapshot_tells_the_states_of_the_install(tmp_path):
    saved = {"system": {"runtime_state_path": "data/runtime-state.json"}, "devices": [external("balkon", "Balkon")]}

    answer = ems_device_states(install(tmp_path, saved, loaded=device_fingerprints({"devices": []})))

    assert answer == {
        "known": True,
        "reason": None,
        "started_at": "2026-11-06T06:00:00+00:00",
        "devices": {"balkon": {"state": "waiting", "change": "added", "name": "Balkon", "ran": None}},
    }


def test_a_switch_in_runtime_state_says_nothing_of_an_external_device_ems_keeps_no_entry_for(tmp_path):
    saved = {"devices": [external("balkon", "Balkon")]}
    context = install(tmp_path, saved, loaded=device_fingerprints(saved), runtime={"devices": {"Balkon": {"enabled": False}}})

    assert ems_device_states(context)["devices"]["balkon"]["state"] == "running"


@pytest.mark.parametrize("snapshot", [
    {"loaded": None},
    {"loaded": [], "age_seconds": 3600},
    {"loaded": [], "config_text": "{ not json"},
    {"loaded": [], "schema": 99},
])
def test_without_a_fresh_snapshot_this_can_read_or_a_readable_config_nothing_is_known_and_ems_counts_as_stopped(tmp_path, snapshot):
    saved = {"system": {}, "devices": [external("balkon", "Balkon")]}

    assert ems_device_states(install(tmp_path, saved, **snapshot)) == {"known": False, "reason": "stopped", "started_at": None, "devices": {}}


@pytest.mark.parametrize("record", [
    {"version": FINGERPRINT_VERSION + 1, "devices": []},
    [],
    {"version": FINGERPRINT_VERSION},
])
def test_a_fresh_snapshot_whose_devices_this_cannot_compare_knows_nothing(tmp_path, record):
    saved = {"devices": [external("balkon", "Balkon")]}

    assert ems_device_states(install(tmp_path, saved, record=record)) == {"known": False, "reason": "unnamed", "started_at": "2026-11-06T06:00:00+00:00", "devices": {}}


def test_a_fresh_snapshot_from_an_ems_that_does_not_name_its_devices_knows_nothing(tmp_path):
    saved = {"devices": [external("balkon", "Balkon")]}
    context = install(tmp_path, saved, loaded=[])
    path = Path(resolve_control_status_path(resolve_runtime_state_path(context, saved)))
    status = json.loads(path.read_text())
    del status["loaded_devices"]
    path.write_text(json.dumps(status))

    assert ems_device_states(context) == {"known": False, "reason": "unnamed", "started_at": "2026-11-06T06:00:00+00:00", "devices": {}}


@pytest.mark.parametrize(("release", "known", "reason", "started"), [
    ("this", True, None, True),
    ("earlier", False, "unnamed", False),
    ("later", False, "unnamed", True),
])
def test_the_test_admin_seeds_an_ems_of_each_release_as_it_answers(tmp_path, release, known, reason, started):
    saved = {"devices": [external("balkon", "Balkon")]}
    context = install(tmp_path, saved)
    write_started_ems_snapshot(context, release=release)

    answer = ems_device_states(context)

    assert (answer["known"], answer["reason"], answer["started_at"] is not None) == (known, reason, started)
    assert answer["devices"] == ({"balkon": {"state": "running", "change": None, "name": "Balkon", "ran": "Balkon"}} if known else {})
