# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ask before a consumer joins or leaves a recorded history.

A consumer's energy and power history hang on its history key
(``consumer_history_key``): a rename or a move to another broker keeps it, so
the history follows the device by default. Maintenance asks the operator
whenever a saved change touches a history the dashboard recorded:

* ``renamed`` / ``moved``: a consumer with values gets another name or broker.
  ``keep`` carries them along; ``new`` gives it a key of its own.
* ``reused``: a consumer new to the config would continue values recorded
  before, under its own device id or under a removed consumer of its name.
  ``keep`` takes them over; ``new`` begins apart.

The answers travel in the draft (``consumer_history``: ``name -> keep|new``)
and become the consumer's ``history_id``, so what is applied is what was
reviewed. A question without an answer keeps. Values a consumer begins apart
from stay recorded under their key and show as a former consumer.
"""

import os

from dashboard.sqlite_store import consumer_histories
from ems.backup import collect_database_backup_files
from ems.zendure_mqtt.config_entries import (
    consumer_history_key,
    consumer_history_keys,
    is_consumer_device_config,
    zendure_mqtt_broker_ref,
    zendure_mqtt_route_device_id,
)
from ems.zendure_mqtt.external_catalog import valid_device_id

DRAFT_FIELD = "consumer_history"
KEEP = "keep"
NEW = "new"
ANSWERS = (KEEP, NEW)

RENAMED = "renamed"
MOVED = "moved"
REUSED = "reused"


def recorded_histories(config, base_dir):
    """The consumer histories of the dashboard database ``config`` names, or none."""

    present, _missing = collect_database_backup_files(config, base_dir=base_dir)
    path = next((item["abs_path"] for item in present if item["role"] == "dashboard_history"), None)
    return consumer_histories(path) if path and os.path.isfile(path) else {}


def _consumers(config):
    devices = config.get("devices") if isinstance(config, dict) else None
    items = [item for item in devices if is_consumer_device_config(item)] if isinstance(devices, list) else []
    keys = consumer_history_keys(items)
    return [(item, keys[item["name"]]) for item in items if item.get("name") in keys]


def _fresh_key(item, taken):
    base = (zendure_mqtt_route_device_id(item) or "consumer")[:60]
    number = 2
    while f"{base}-{number}" in taken or not valid_device_id(f"{base}-{number}"):
        number += 1
    return f"{base}-{number}"


def _question(name, kind, key, history, previous=None, adopt=None):
    return {
        "name": name,
        "kind": kind,
        "previous_name": previous,
        "history_name": history["name"],
        "last_date": history["last_date"],
        "kwh": history["kwh"],
        "key": key,
        "adopt": adopt,
    }


def _questions(current, merged, histories):
    before = {key: item for item, key in _consumers(current)}
    after = _consumers(merged)
    in_use = {key for _item, key in after}
    questions = []
    for item, key in after:
        name = item["name"]
        previous = before.get(key)
        if previous is not None:
            if key not in histories:
                continue
            if previous.get("name") != name:
                questions.append(_question(name, RENAMED, key, histories[key], previous=previous.get("name")))
            elif zendure_mqtt_broker_ref(previous) != zendure_mqtt_broker_ref(item):
                questions.append(_question(name, MOVED, key, histories[key], previous=name))
            continue
        if key in histories:
            questions.append(_question(name, REUSED, key, histories[key]))
            continue
        adopt = next(
            (
                other
                for other, history in sorted(histories.items(), key=lambda pair: pair[1]["last_date"] or "", reverse=True)
                if history["name"] == name and other not in in_use and valid_device_id(other)
            ),
            None,
        )
        if adopt is not None:
            questions.append(_question(name, REUSED, key, histories[adopt], adopt=adopt))
    return questions


def _apart_from_others(current, merged, histories):
    """Give a consumer new to the config a key of its own when another holds its key.

    Enabled consumers never share a device id, but one can take over a key
    that is another's device id; the newcomer then begins apart rather than
    recording into that one's values. A consumer is known by its device id,
    which a rename, a move and its place in the list all keep.
    """

    known = {zendure_mqtt_route_device_id(item) for item, _key in _consumers(current)}
    after = [item for item, _key in _consumers(merged)]
    held = {consumer_history_key(item) for item in after if zendure_mqtt_route_device_id(item) in known}
    taken = set(histories) | {consumer_history_key(item) for item in after}
    for item in after:
        if zendure_mqtt_route_device_id(item) in known:
            continue
        key = consumer_history_key(item)
        if key in held and "history_id" not in item:
            key = item["history_id"] = _fresh_key(item, taken)
            taken.add(key)
        held.add(key)


def resolve(current, merged, histories, answers):
    """The history questions ``merged`` raises, each with its answer; ``merged`` carries the answers.

    ``keep`` is the answer a question without one gets. Only the consumers a
    question names are touched: a ``history_id`` is written to begin apart,
    or to take over a removed consumer's key. A consumer new to the config
    whose key another holds gets one of its own either way.
    """

    answers = answers if isinstance(answers, dict) else {}
    _apart_from_others(current, merged, histories)
    questions = _questions(current, merged, histories)
    if not questions:
        return []
    items = {item["name"]: item for item, _key in _consumers(merged)}
    taken = set(histories) | {key for _item, key in _consumers(merged)} | {key for _item, key in _consumers(current)}
    for question in questions:
        answer = answers.get(question["name"])
        question["answer"] = answer if answer in ANSWERS else KEEP
        question["answered"] = answer in ANSWERS
        item = items[question["name"]]
        if question["adopt"] is not None:
            if question["answer"] == KEEP:
                item["history_id"] = question["adopt"]
        elif question["answer"] == NEW:
            item["history_id"] = _fresh_key(item, taken)
            taken.add(item["history_id"])
    return questions
