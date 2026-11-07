# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance asks before a consumer joins or leaves a recorded history.

A consumer's energy and power history hang on its device id, so a rename or a
move carries them along unless the operator begins anew; a consumer new to the
config that would continue values recorded before -- under its own device id,
or under a removed consumer of its name -- is asked about too. The answer
becomes the consumer's ``history_id``; a question without one keeps.
"""

import copy
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from admin.consumer_history import KEEP, MOVED, NEW, RENAMED, REUSED, recorded_histories, resolve
from admin.maintenance_config import load_maintenance_config, preview_maintenance_config
from dashboard.sqlite_store import DashboardStore

pytestmark = [
    pytest.mark.unit,
    pytest.mark.admin,
    pytest.mark.maintenance,
]

IDENTITY_KEY = b"consumer-history-identity-key-32"
HISTORY = {
    "washer": {"name": "Washer", "last_date": "2026-11-03", "kwh": 12.4},
    "old-plug": {"name": "Dryer", "last_date": "2026-05-01", "kwh": 40.0},
}


def _consumer(name, device_id, broker="house", **extra):
    return {
        "name": name,
        "type": "external_mqtt",
        "mqtt": {"broker_ref": broker, "topic_family": "ems_consumer", "device_id": device_id},
        **extra,
    }


def _config(*devices):
    return {"devices": [{"name": "WR1", "ip": "192.0.2.10", "sn": "SN1"}, *devices]}


def _resolve(current, merged, answers=None, histories=HISTORY):
    merged = copy.deepcopy(merged)
    return resolve(current, merged, histories, answers), merged["devices"][1:]


def test_a_rename_carries_the_values_unless_the_operator_begins_anew():
    current = _config(_consumer("Washer", "washer"))
    renamed = _config(_consumer("Waschmaschine", "washer"))

    (question,), devices = _resolve(current, renamed)
    assert question["kind"] == "renamed" and question["previous_name"] == "Washer"
    assert (question["answer"], question["answered"], question["kwh"]) == (KEEP, False, 12.4)
    assert "history_id" not in devices[0]

    (question,), devices = _resolve(current, renamed, {"Waschmaschine": NEW})
    assert (question["answer"], question["answered"]) == (NEW, True)
    assert devices[0]["history_id"] == "washer-2"


def test_a_move_to_another_broker_is_asked_about_too():
    (question,), _devices = _resolve(_config(_consumer("Washer", "washer")), _config(_consumer("Washer", "washer", broker="cellar")))

    assert question["kind"] == "moved"


def test_a_consumer_without_values_or_unchanged_is_not_asked_about():
    assert _resolve(_config(_consumer("Office", "office")), _config(_consumer("Desk", "office")))[0] == []
    assert _resolve(_config(_consumer("Washer", "washer")), _config(_consumer("Washer", "washer")))[0] == []
    assert _resolve(_config(), _config(_consumer("Office", "office")))[0] == []


def test_a_new_consumer_on_a_recorded_device_id_continues_unless_it_begins_anew():
    (question,), devices = _resolve(_config(), _config(_consumer("Trockner", "washer")))
    assert (question["kind"], question["history_name"], question["adopt"]) == ("reused", "Washer", None)
    assert "history_id" not in devices[0]

    _questions, devices = _resolve(_config(), _config(_consumer("Trockner", "washer")), {"Trockner": NEW})
    assert devices[0]["history_id"] == "washer-2"


def test_a_new_consumer_takes_over_the_values_of_a_removed_one_of_its_name():
    current = _config(_consumer("Dryer", "old-plug"))
    replaced = _config(_consumer("Dryer", "new-plug"))

    (question,), devices = _resolve(current, replaced)
    assert (question["kind"], question["adopt"], question["last_date"]) == ("reused", "old-plug", "2026-05-01")
    assert devices[0]["history_id"] == "old-plug"

    _questions, devices = _resolve(current, replaced, {"Dryer": NEW})
    assert "history_id" not in devices[0]


def test_values_another_consumer_still_records_are_never_taken_over():
    current = _config(_consumer("Dryer", "old-plug"))
    both = _config(_consumer("Dryer", "old-plug"), _consumer("Dryer 2", "new-plug"))

    assert _resolve(current, both)[0] == []


def test_a_newcomer_whose_key_another_took_over_begins_apart():
    current = _config(_consumer("Dryer", "new-plug", history_id="old-plug"))
    readded = _config(_consumer("Dryer", "new-plug", history_id="old-plug"), _consumer("Old dryer", "old-plug"))

    questions, devices = _resolve(current, readded)

    assert questions == []
    assert devices[0]["history_id"] == "old-plug"
    assert devices[1]["history_id"] == "old-plug-2"

    renamed_after_it = _config(_consumer("Old dryer", "old-plug"), _consumer("Laundry", "new-plug", history_id="old-plug"))
    histories = {"old-plug": HISTORY["old-plug"]}
    (question,), devices = _resolve(current, renamed_after_it, histories=histories)
    assert devices[0]["history_id"] == "old-plug-2"
    assert (question["kind"], question["name"], question["previous_name"]) == ("renamed", "Laundry", "Dryer")


def test_a_re_enabled_consumer_is_asked_whether_it_continues_its_values():
    current = _config(_consumer("Washer", "washer", enabled=False))
    enabled = _config(_consumer("Washer", "washer"))

    (question,), devices = _resolve(current, enabled)

    assert (question["kind"], question["history_name"]) == ("reused", "Washer")
    assert "history_id" not in devices[0]


def test_values_recorded_under_a_name_are_never_offered_as_a_key():
    histories = {"Washing machine": {"name": "Washing machine", "last_date": "2026-11-01", "kwh": 3.0}}

    assert _resolve(_config(), _config(_consumer("Washing machine", "washer")), histories=histories)[0] == []


def test_a_fresh_key_is_unused_and_a_valid_device_id():
    histories = {**HISTORY, "washer-2": {"name": "Washer", "last_date": "2026-01-01", "kwh": 1.0}}
    long_id = "w" * 64
    current = _config(_consumer("Washer", "washer"), _consumer("Long", long_id))
    renamed = _config(_consumer("Waschmaschine", "washer"), _consumer("Longer", long_id))

    _questions, devices = _resolve(
        current, renamed, {"Waschmaschine": NEW, "Longer": NEW}, {**histories, long_id: HISTORY["washer"]}
    )

    assert devices[0]["history_id"] == "washer-3"
    assert devices[1]["history_id"] == "w" * 60 + "-2"


def _install(base_dir, config):
    (base_dir / "config").mkdir(exist_ok=True)
    (base_dir / "config" / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    (base_dir / "data").mkdir(exist_ok=True)
    store = DashboardStore(base_dir / "data" / "ems_dashboard.sqlite", energy_savings={"enabled": True})
    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)
    for second in (0, 10):
        store.record({
            "timestamp": (start + timedelta(seconds=second)).isoformat(),
            "devices": {},
            "grid_power_w": 400,
            "home_load_w": 1000,
            "consumers": {"washer": {"name": "Washer", "online": True, "power_w": 600}},
        })


def test_the_maintenance_preview_asks_and_writes_the_answer(tmp_path):
    config = _config(_consumer("Washer", "washer"))
    config["zendure_mqtt"] = {"brokers": {"house": {"enabled": True, "source": "local_mqtt", "host": "10.0.0.5"}}}
    _install(tmp_path, config)
    assert set(recorded_histories(config, str(tmp_path))) == {"washer"}

    loaded = load_maintenance_config(base_dir=str(tmp_path), identity_token_key=IDENTITY_KEY)
    draft = copy.deepcopy(loaded["draft"])
    row = next(item for item in draft["devices"] if item.get("name") == "Washer")
    row["name"] = "Waschmaschine"

    asked = preview_maintenance_config(draft, base_dir=str(tmp_path), identity_token_key=IDENTITY_KEY)
    answered = preview_maintenance_config(
        {**draft, "consumer_history": {"Waschmaschine": NEW}}, base_dir=str(tmp_path), identity_token_key=IDENTITY_KEY
    )

    assert asked["validation"]["ok"], asked["validation"]
    assert [(q["name"], q["kind"], q["answered"]) for q in asked["consumer_history"]] == [("Waschmaschine", "renamed", False)]
    renamed = next(item for item in answered["preview"]["devices"] if item.get("name") == "Waschmaschine")
    assert renamed["history_id"] == "washer-2"
    assert answered["validation"]["ok"], answered["validation"]


def test_the_console_has_words_for_every_kind_of_question():
    source = (Path(__file__).resolve().parents[1] / "frontend/admin-modern/src/lib/settings.js").read_text(encoding="utf-8")
    listed = re.search(r"HISTORY_KINDS = Object\.freeze\((\[[^\]]*\])\)", source)

    assert json.loads(listed.group(1)) == [RENAMED, MOVED, REUSED]
