# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live topics: every topic shown, recognised devices grouped and named, all bounded.

A label is a name, never an assignment; what the EMS reads directly and the
mapping's own outputs are labelled but not offered for mapping.
"""

import json

import pytest

from admin.mqtt_live_topics import (
    LABELS,
    MAX_PAYLOAD_BYTES,
    MAX_TOPICS,
    PAYLOAD_PREVIEW_CHARS,
    REAP_INTERVAL_SECONDS,
    SESSION_IDLE_SECONDS,
    LiveTopicSessions,
    TopicTree,
    recognize,
    topic_values,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.admin,
    pytest.mark.mqtt,
]


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


@pytest.mark.parametrize(
    ("topic", "label", "group"),
    [
        ("shellies/plug1/relay/0/power", "shelly_gen1", "plug1"),
        ("shellyplusplugs-abc/status/switch:0", "shelly_gen2", "shellyplusplugs-abc"),
        ("shellyplusplugs-abc/online", "shelly_gen2", "shellyplusplugs-abc"),
        ("shellypro3em-xyz/events/rpc", "shelly_gen2", "shellypro3em-xyz"),
        ("tele/garage/SENSOR", "tasmota", "garage"),
        ("stat/garage/POWER", "tasmota", "garage"),
        ("solar/114181234567/0/power", "opendtu", "114181234567"),
        ("ahoy/wr1/ch0/P_AC", "ahoydtu", "wr1"),
        ("iot/pk/dev123/properties/report", "zendure_native", "dev123"),
        ("Zendure/sensor/SN1/totalPower", "zendure_meter_native", "SN1"),
        ("ems-solarflow/balkon/state", "ems_output", "balkon"),
        ("heizung/vorlauf", "unknown", "heizung"),
        ("tele/x", "unknown", "tele/x"),
    ],
)
def test_a_topic_is_recognised_or_grouped_by_its_tree(topic, label, group):
    recognized = recognize(topic)

    assert (recognized.label, recognized.group) == (label, group)


def test_every_label_is_listed():
    topics = [
        "shellies/a/relay/0/power", "s/status/switch:0", "tele/a/SENSOR", "p/1234567890/0/power",
        "p/w/ch0/P_AC", "iot/pk/d/properties/report", "Zendure/sensor/S/totalPower",
        "ems-solarflow/x/state", "x/y",
    ]
    assert {recognize(topic).label for topic in topics} == set(LABELS)


def test_unknown_topics_of_a_recognised_device_join_its_group():
    tree = TopicTree(clock=Clock())
    tree.observe("shellyplusplugs-abc/status/switch:0", b'{"apower": 5}')
    tree.observe("shellyplusplugs-abc/status/sys", b'{"uptime": 9}')
    tree.observe("heizung/vorlauf", b"41.5")

    groups = tree.groups()

    assert [(g["label"], g["key"], len(g["topics"])) for g in groups] == [
        ("shelly_gen2", "shellyplusplugs-abc", 2),
        ("unknown", "heizung", 1),
    ]


def test_direct_devices_and_outputs_are_shown_but_not_offered():
    tree = TopicTree(clock=Clock())
    tree.observe("iot/pk/dev/properties/report", b'{"properties": {"electricLevel": 50}}')
    tree.observe("ems-solarflow/balkon/state", b'{"inverterPower": 5}')

    assert {g["label"]: g["mappable"] for g in tree.groups()} == {"zendure_native": False, "ems_output": False}


def test_values_are_every_number_with_its_path_power_and_charge_first():
    payload = json.dumps({"voltage": 231, "apower": -612.3, "aenergy": {"total": 5}, "on": True, "id": "x",
                          "emeters": [{"power": 1}], "soc": 80}).encode()

    payload_format, values = topic_values(payload)

    assert payload_format == "json"
    assert [(v["path"], v["likely"]) for v in values][:3] == [("apower", "power"), ("emeters.0.power", "power"), ("soc", "soc")]
    assert {v["path"] for v in values} == {"voltage", "apower", "aenergy.total", "emeters.0.power", "soc"}
    assert topic_values(b"612") == ("number", [{"path": None, "value": 612, "likely": None}])
    assert topic_values(b"on") == (None, [])


def test_a_value_shows_where_it_is_already_mapped():
    tree = TopicTree(clock=Clock())
    tree.observe("plug", b'{"apower": 5}')

    (group,) = tree.groups({("plug", "apower"): {"balkon"}})

    assert group["topics"][0]["values"][0]["mapped_to"] == ["balkon"]


def test_the_record_is_bounded_and_says_so():
    tree = TopicTree(clock=Clock())
    for index in range(MAX_TOPICS + 5):
        tree.observe(f"t/{index}", b"1")
    tree.observe("big", b"x" * (MAX_PAYLOAD_BYTES * 3))
    tree.observe("$SYS/broker/uptime", b"5")

    assert len(tree.topics) == MAX_TOPICS and tree.truncated
    assert "$SYS/broker/uptime" not in tree.topics


def test_rate_change_and_retention_are_counted():
    clock = Clock()
    tree = TopicTree(clock=clock)
    tree.observe("p", b"1", True)
    clock.now += 60
    tree.observe("p", b"2")
    clock.now += 60
    tree.observe("p", b"2")

    (topic,) = tree.groups()[0]["topics"]

    assert topic["retained"] is True and topic["changing"] is True
    assert topic["messages_per_minute"] == 1.5


class FakeWatcher:
    def __init__(self, endpoint, tree):
        self.endpoint, self.tree, self.stopped, self.state = endpoint, tree, False, "listening"

    def start(self):
        pass

    def stop(self):
        self.stopped = True


def test_one_session_per_broker_and_an_unpolled_one_ends():
    clock = Clock()
    sessions = LiveTopicSessions(watcher_factory=FakeWatcher, clock=clock, spawn=lambda target: None)
    first = sessions.start("ems-mqtt", {"host": "127.0.0.1", "port": 1883})
    first_watcher = sessions.get(first).watcher

    second = sessions.start("ems-mqtt", {"host": "127.0.0.1", "port": 1883})
    assert first_watcher.stopped and sessions.get(first) is None

    clock.now += SESSION_IDLE_SECONDS - 1
    assert sessions.snapshot(second)["state"] == "listening"
    clock.now += SESSION_IDLE_SECONDS + 1
    assert sessions.get(second) is None


def test_a_payload_larger_than_its_preview_is_still_read():
    payload = json.dumps({"apower": -612.3, "pad": "x" * 4000}).encode()
    tree = TopicTree(clock=Clock())
    tree.observe("shellyplusplugs-balkon/status/switch:0", payload)

    (topic,) = tree.groups()[0]["topics"]

    assert {"path": "apower", "value": -612.3} in [
        {"path": value["path"], "value": value["value"]} for value in topic["values"]
    ]
    assert len(topic["payload_preview"]) == PAYLOAD_PREVIEW_CHARS


def test_a_session_nobody_polls_any_more_ends_without_another_request():
    """The dialog closed after one poll and no further request reaches the Admin.

    The reaper is the only thing left to end the ``#`` subscription; it runs here
    in the test thread, and each of its waits moves the clock one interval on.
    """

    clock = Clock()
    spawned = []

    def wait(seconds):
        clock.now += seconds

    sessions = LiveTopicSessions(watcher_factory=FakeWatcher, clock=clock, wait=wait, spawn=spawned.append)
    session_id = sessions.start("ems-mqtt", {"host": "127.0.0.1", "port": 1883})
    watcher = sessions.get(session_id).watcher
    (reaper,) = spawned
    started = clock.now

    reaper()

    assert watcher.stopped
    assert SESSION_IDLE_SECONDS < clock.now - started <= SESSION_IDLE_SECONDS + REAP_INTERVAL_SECONDS
    sessions.start("ems-mqtt", {"host": "127.0.0.1", "port": 1883})
    assert len(spawned) == 2


def test_the_interval_is_measured_once_a_topic_has_arrived_three_times():
    clock = Clock()
    tree = TopicTree(clock=clock)
    tree.observe("tele/plug/SENSOR", b"1")
    clock.now += 300
    tree.observe("tele/plug/SENSOR", b"2")

    assert tree.groups()[0]["topics"][0]["interval_seconds"] is None

    clock.now += 300
    tree.observe("tele/plug/SENSOR", b"3")

    assert tree.groups()[0]["topics"][0]["interval_seconds"] == 300


def test_a_plain_number_is_named_by_its_topic():
    assert topic_values(b"410", "solar/1161234567/0/power")[1][0]["likely"] == "power"
    assert topic_values(b"81", "akku/soc")[1][0]["likely"] == "soc"
    assert topic_values(b"5", "heating/flow")[1][0]["likely"] is None
