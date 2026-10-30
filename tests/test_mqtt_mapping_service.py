# SPDX-License-Identifier: AGPL-3.0-or-later
"""The service loop with fake connections: reloads, reconnects and the status file."""

import json
import os

import pytest

from mqtt_mapping import __main__ as entry
from mqtt_mapping.brokers import BrokerEndpoint, load_broker_file
from mqtt_mapping.service import MappingService, ServicePaths
from mqtt_mapping.status import read_status, status_is_fresh

pytestmark = [
    pytest.mark.unit,
    pytest.mark.mqtt,
]


class _FakeConnection:
    instances = []

    def __init__(self, endpoint, topics, on_message):
        self.endpoint = endpoint
        self.topics = tuple(topics)
        self.on_message = on_message
        self.started = self.stopped = False
        self.published = []
        _FakeConnection.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def publish(self, topic, payload, expiry):
        self.published.append((topic, json.loads(payload), expiry))
        return True

    def state(self):
        return {"connected": self.started and not self.stopped, "mqtt5": True, "error": None}


class _Clock:
    now = 50.0

    def __call__(self):
        return self.now


@pytest.fixture
def service(tmp_path):
    _FakeConnection.instances = []
    paths = ServicePaths(
        str(tmp_path / "config" / "mqtt" / "mappings.json"),
        str(tmp_path / "mqtt" / "mapping-credentials.json"),
        str(tmp_path / "data" / "mqtt-mapping-status.json"),
    )
    for path in (paths.mappings, paths.brokers):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    clock = _Clock()
    return MappingService(paths, connection_factory=_FakeConnection, clock=clock), paths, clock


def _write(path, document):
    tmp = f"{path}.new"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    os.replace(tmp, path)


BROKERS = {
    "schema_version": 1,
    "brokers": {"ems-mqtt": {"host": "ems-mqtt-broker", "port": 1883, "username": "ems-mapping", "password": "pw"}},
}


def _mappings(device_id="balkon", topic="plug"):
    return {
        "schema_version": 1,
        "devices": [
            {
                "id": device_id,
                "broker_ref": "ems-mqtt",
                "values": {"inverterPower": {"sources": [{"topic": topic, "format": "number"}]}},
            }
        ],
    }


def test_the_service_connects_where_the_mappings_read_and_publishes_what_arrives(service):
    svc, paths, clock = service
    _write(paths.brokers, BROKERS)
    _write(paths.mappings, _mappings())

    assert svc.reload_if_changed() is True
    (connection,) = _FakeConnection.instances
    assert connection.started and connection.topics == ("plug",)
    assert connection.endpoint == BrokerEndpoint("ems-mqtt", "ems-mqtt-broker", 1883, "ems-mapping", "pw")

    connection.on_message("plug", b"612")
    svc.tick()

    assert connection.published == [("ems-solarflow/balkon/state", {"inverterPower": 612}, 45)]


def test_an_unchanged_file_is_not_read_again(service):
    svc, paths, _clock = service
    _write(paths.brokers, BROKERS)
    _write(paths.mappings, _mappings())
    svc.reload_if_changed()

    assert svc.reload_if_changed() is False
    assert len(_FakeConnection.instances) == 1


def test_a_new_topic_replaces_the_connection_and_a_rename_keeps_it(service):
    svc, paths, _clock = service
    _write(paths.brokers, BROKERS)
    _write(paths.mappings, _mappings())
    svc.reload_if_changed()
    first = _FakeConnection.instances[0]

    _write(paths.mappings, _mappings(device_id="terrasse"))
    svc.reload_if_changed()
    assert _FakeConnection.instances == [first] and not first.stopped

    _write(paths.mappings, _mappings(topic="plug2"))
    svc.reload_if_changed()
    assert first.stopped
    assert _FakeConnection.instances[-1].topics == ("plug2",)


def test_a_file_that_does_not_validate_leaves_the_last_good_mappings_running(service):
    svc, paths, _clock = service
    _write(paths.brokers, BROKERS)
    _write(paths.mappings, _mappings())
    svc.reload_if_changed()

    broken = _mappings()
    broken["devices"][0]["values"]["inverterPower"]["sources"][0]["factor"] = 0
    _write(paths.mappings, broken)

    assert svc.reload_if_changed() is False
    assert [device.id for device in svc.publisher.mappings.devices] == ["balkon"]
    assert [issue["code"] for issue in svc.status()["issues"]] == ["mqtt_mapping_option_invalid"]


def test_a_broker_the_credentials_file_does_not_name_is_refused(service):
    svc, paths, _clock = service
    _write(paths.brokers, {"schema_version": 1, "brokers": {}})
    _write(paths.mappings, _mappings())

    svc.reload_if_changed()

    assert _FakeConnection.instances == []
    assert [issue["code"] for issue in svc.status()["issues"]] == ["mqtt_mapping_broker_invalid"]


def test_the_status_file_says_what_each_device_does_and_holds_no_password(service):
    svc, paths, _clock = service
    _write(paths.brokers, BROKERS)
    _write(paths.mappings, _mappings())
    svc.reload_if_changed()
    _FakeConnection.instances[0].on_message("plug", b"612")
    svc.tick()

    svc.write_status()
    status = read_status(paths.status)

    assert status["schema_version"] == 1
    assert status["brokers"] == {"ems-mqtt": {"connected": True, "mqtt5": True, "error": None}}
    (device,) = status["devices"]
    assert device["publishing"] is True and device["state"] == {"inverterPower": 612}
    assert device["sources"][0]["payload_preview"] == "612"
    assert device["last_published_at"]
    assert "pw" not in json.dumps(status)
    assert status_is_fresh(paths.status)


def test_the_health_check_follows_the_status_file(service):
    _svc, paths, _clock = service

    assert entry.main(["--healthcheck", "--status", paths.status]) == 1
    os.makedirs(os.path.dirname(paths.status), exist_ok=True)
    with open(paths.status, "w", encoding="utf-8") as handle:
        handle.write("{}")
    assert entry.main(["--healthcheck", "--status", paths.status]) == 0


@pytest.mark.parametrize(
    "entry_document",
    [
        {"host": "", "port": 1883},
        {"host": "h", "port": 0},
        {"host": "h", "port": True},
        {"host": "h", "port": 1883, "username": "u"},
        {"host": "h", "port": 1883, "tls": False, "tls_insecure": True},
        "h:1883",
    ],
)
def test_an_incomplete_broker_entry_is_left_out_and_reported(tmp_path, entry_document):
    path = tmp_path / "brokers.json"
    path.write_text(json.dumps({"schema_version": 1, "brokers": {"x": entry_document}}), encoding="utf-8")

    endpoints, issues = load_broker_file(path)

    assert endpoints == {}
    assert [issue["code"] for issue in issues] == ["mqtt_mapping_brokers_unreadable"]


def test_a_state_the_broker_did_not_take_is_not_reported_as_published(service):
    """The broker connection dropped after the last state went out; the next ones are refused."""

    svc, paths, _clock = service
    _write(paths.brokers, BROKERS)
    _write(paths.mappings, _mappings())
    svc.reload_if_changed()
    (connection,) = _FakeConnection.instances
    connection.on_message("plug", b"612")
    connection.stopped = True

    svc.tick()
    (device,) = svc.status()["devices"]

    assert device["state"] == {"inverterPower": 612}
    assert device["publishing"] is False
    assert device["reason"] == "not_delivered"
