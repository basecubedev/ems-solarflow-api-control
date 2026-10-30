# SPDX-License-Identifier: AGPL-3.0-or-later
"""When the mapping publishes: interval, change, silence and expiry, on a fake clock.

No sleeps: the clock is a variable each test moves, so every interleaving
named below is the one that runs.
"""

import json

import pytest

from mqtt_mapping.config import parse_mappings
from mqtt_mapping.publisher import (
    MIN_EXPIRY_SECONDS,
    MappingPublisher,
    expiry_seconds,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.mqtt,
]


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _mappings(interval=15, max_age=60, devices=None):
    devices = devices or [
        {
            "id": "balkon",
            "broker_ref": "ems-mqtt",
            "values": {
                "inverterPower": {
                    "sources": [{"topic": "plug", "format": "number", "max_age_seconds": max_age}]
                }
            },
        }
    ]
    mappings, issues = parse_mappings(
        {"schema_version": 1, "publish_interval_seconds": interval, "devices": devices}
    )
    assert issues == []
    return mappings


def _publisher(mappings, accept=True):
    clock = _Clock()
    sent = []

    def publish(broker_ref, topic, payload, expiry):
        sent.append((clock.now, broker_ref, topic, json.loads(payload), expiry))
        return accept

    return MappingPublisher(mappings, publish, clock=clock, wall_clock=lambda: 1.7e9), clock, sent


def test_the_first_reading_is_published_at_once_retained_with_its_expiry():
    publisher, clock, sent = _publisher(_mappings())

    publisher.observe("ems-mqtt", "plug", b"612")
    assert publisher.tick() == ["balkon"]

    assert sent == [(1000.0, "ems-mqtt", "ems-solarflow/balkon/state", {"inverterPower": 612}, 45)]


def test_an_unchanged_value_is_repeated_at_the_interval_and_not_before():
    publisher, clock, sent = _publisher(_mappings(interval=15, max_age=600))
    publisher.observe("ems-mqtt", "plug", b"612")
    publisher.tick()

    clock.now += 14.9
    assert publisher.tick() == []
    clock.now += 0.1
    assert publisher.tick() == ["balkon"]
    assert len(sent) == 2


def test_a_change_goes_out_at_once_but_at_most_once_a_second():
    publisher, clock, sent = _publisher(_mappings())
    publisher.observe("ems-mqtt", "plug", b"612")
    publisher.tick()

    clock.now += 0.5
    publisher.observe("ems-mqtt", "plug", b"700")
    assert publisher.tick() == []
    clock.now += 0.5
    assert publisher.tick() == ["balkon"]
    assert sent[-1][3] == {"inverterPower": 700}


def test_a_source_that_falls_silent_stops_the_device_and_a_new_reading_restarts_it():
    publisher, clock, sent = _publisher(_mappings(interval=15, max_age=20))
    publisher.observe("ems-mqtt", "plug", b"612")
    publisher.tick()

    clock.now += 21
    assert publisher.tick() == []
    clock.now += 30
    assert publisher.tick() == []
    assert publisher.evaluations()["balkon"].reason == "stale"

    publisher.observe("ems-mqtt", "plug", b"500")
    assert publisher.tick() == ["balkon"]
    assert len(sent) == 2


def test_a_publish_the_client_refused_is_tried_again_on_the_next_tick():
    publisher, clock, sent = _publisher(_mappings(), accept=False)
    publisher.observe("ems-mqtt", "plug", b"612")

    assert publisher.tick() == []
    assert publisher.tick() == []
    assert len(sent) == 2
    assert publisher.last_published_wall("balkon") is None


def test_a_reading_from_another_broker_does_not_feed_the_device():
    publisher, clock, sent = _publisher(_mappings())

    publisher.observe("iobroker", "plug", b"612")

    assert publisher.tick() == []
    assert publisher.evaluations()["balkon"].reason == "source_missing"


@pytest.mark.parametrize(("interval", "expiry"), [(5, MIN_EXPIRY_SECONDS), (15, 45), (30, 90)])
def test_a_retained_state_expires_after_three_intervals_and_never_sooner_than_45_s(interval, expiry):
    assert expiry_seconds(interval) == expiry


def test_new_mappings_keep_what_was_received_on_topics_still_read():
    publisher, clock, sent = _publisher(_mappings())
    publisher.observe("ems-mqtt", "plug", b"612")
    publisher.observe("ems-mqtt", "gone", b"1")
    publisher.tick()

    renamed = _mappings(
        devices=[
            {
                "id": "terrasse",
                "broker_ref": "ems-mqtt",
                "values": {"inverterPower": {"sources": [{"topic": "plug", "format": "number"}]}},
            }
        ]
    )
    publisher.replace_mappings(renamed)

    assert publisher.tick() == ["terrasse"]
    assert sent[-1][2] == "ems-solarflow/terrasse/state"
    assert publisher.last_published_wall("balkon") is None
