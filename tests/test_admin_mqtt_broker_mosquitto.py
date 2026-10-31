# SPDX-License-Identifier: AGPL-3.0-or-later
"""The rendered broker files on the pinned Mosquitto image: what each client may do.

The Admin renders ``mosquitto.conf``, ``acl`` and ``passwd`` exactly as it would
for an installation, and the pinned image runs them as the install user. Each
forbidden publish is followed by an allowed one from the same connection; the
watcher sees the allowed one, so not seeing the forbidden one before it proves
the broker dropped it rather than that it was slow. The broker counts as up
once it answers a CONNECT, never when its port merely accepts: Docker's port
proxy accepts before Mosquitto listens, and the first client then lost its
connection -- the failure this module showed now and then. Every marker is a topic of
its own, and so is every topic a check publishes (``{n}`` is replaced by a
counter), so a late copy of an earlier message can never stand in for one or
look like a forbidden one; an allowed publish is waited for by name rather than
judged by its order.
"""

import json
import os
import subprocess
import uuid

import pytest

from tests.helpers.mosquitto import (
    free_port,
    require_real_broker_environment,
    wait_until,
)

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.e2e,
    pytest.mark.docker,
]

require_real_broker_environment()


class FakeCompose:
    def up(self, *args, **kwargs):
        pass

    def stop(self, *args, **kwargs):
        pass

    def reload(self, *args, **kwargs):
        pass

    def ps(self, workspace):
        return []


def _client(port, username=None, password=None):
    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if username:
        client.username_pw_set(username, password)
    result = {}
    client.on_connect = lambda _c, _u, _f, reason, _p=None: result.setdefault("refused", reason.is_failure)
    client.on_disconnect = lambda *_args: result.setdefault("refused", True)
    client.connect("127.0.0.1", port)
    client.loop_start()
    wait_until(lambda: "refused" in result, message="neither CONNACK nor disconnect")
    return client, result["refused"]


class Watcher:
    def __init__(self, port, password):
        self.messages = []
        self.markers = 0
        self.client, refused = _client(port, "ems-admin", password)
        assert not refused
        subscribed = []
        self.client.on_message = lambda _c, _u, message: self.messages.append(message.topic)
        self.client.on_subscribe = lambda *_args: subscribed.append(True)
        self.client.subscribe("#", qos=1)
        wait_until(lambda: subscribed, message="watcher not subscribed")

    def _unique(self, topic):
        self.markers += 1
        return topic.replace("{n}", str(self.markers))

    def delivered(self, client, forbidden, allowed):
        """Whether ``forbidden`` reached the watcher before ``allowed`` did."""

        forbidden = self._unique(forbidden)
        marker = self._unique(allowed.replace("/ok", "/ok-{n}", 1))
        client.publish(forbidden, b"1", qos=1).wait_for_publish(5)
        client.publish(marker, b"1", qos=1).wait_for_publish(5)
        wait_until(lambda: marker in self.messages, message=f"{marker} never arrived")
        seen = self.messages[: self.messages.index(marker)]
        self.messages.clear()
        return forbidden in seen

    def arrives(self, client, topic):
        """Whether ``topic`` from ``client`` reaches the watcher; raises when it never does."""

        topic = self._unique(topic)
        client.publish(topic, b"1", qos=1).wait_for_publish(5)
        wait_until(lambda: topic in self.messages, message=f"{topic} never arrived")
        self.messages.clear()
        return True

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()


@pytest.fixture
def broker(tmp_path):
    from admin.credential_store import CredentialStore
    from admin.mqtt_broker import MOSQUITTO_IMAGE, BuiltinBroker, mqtt_answers

    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "config" / "config.json").write_text(
        json.dumps({"grid_meter": {"type": "mqtt", "mqtt": {"broker_ref": "ems-mqtt", "topic": "meter/power"}}}),
        encoding="utf-8",
    )
    admin = BuiltinBroker(
        install_root=tmp_path,
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        credential_store=CredentialStore(tmp_path / "config"),
        compose=FakeCompose(),
        runtime_identity=(os.getuid(), os.getgid()),
        port_free=lambda port: True,
        port_answers=lambda port: True,
    )
    assert admin.enable()["ok"]
    admin.update(
        {
            "anonymous": True,
            "accounts": [{"name": "devices"}, {"name": "zendure", "zendure": True}, {"name": "meter"}],
            "grid_meter_account": "meter",
        }
    )
    workspace = tmp_path / "mqtt"
    port = free_port()
    name = f"ems-mqtt-test-{uuid.uuid4().hex[:8]}"
    run = [
        "docker", "run", "-d", "--rm", "--name", name,
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-p", f"{port}:1883",
        "-v", f"{workspace / 'broker'}:/mosquitto/config:ro",
        "-v", f"{workspace / 'data'}:/mosquitto/data",
        MOSQUITTO_IMAGE,
    ]
    subprocess.run(run, check=True, capture_output=True, text=True)
    try:
        try:
            wait_until(lambda: mqtt_answers(port), timeout=30.0, message="the broker never sent a CONNACK")
        except AssertionError as exc:
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True).stderr
            raise AssertionError(f"{exc}: {logs}") from exc
        yield admin, port, name
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_each_client_reaches_only_what_its_mode_allows(broker):
    admin, port, _name = broker
    watcher = Watcher(port, admin.password("ems-admin"))
    clients = []
    try:
        anonymous, refused = _client(port)
        assert not refused
        devices, _ = _client(port, "devices", admin.password("devices"))
        zendure, _ = _client(port, "zendure", admin.password("zendure"))
        meter, _ = _client(port, "meter", admin.password("meter"))
        mapping, _ = _client(port, "ems-mapping", admin.password("ems-mapping"))
        ems, _ = _client(port, "ems", admin.password("ems"))
        clients = [anonymous, devices, zendure, meter, mapping, ems]

        for client in (anonymous, devices):
            assert not watcher.delivered(client, "ems-solarflow/x{n}/state", "shellies/ok")
            assert not watcher.delivered(client, "iot/pk/dev{n}/properties/report", "shellies/ok")
            assert not watcher.delivered(client, "Zendure/sensor/x{n}/totalPower", "shellies/ok")
            assert not watcher.delivered(client, "/pk/dev{n}/properties/report", "shellies/ok")
            assert not watcher.delivered(client, "meter/power", "shellies/ok")
        assert watcher.arrives(zendure, "iot/pk/dev{n}/properties/report")
        assert not watcher.delivered(zendure, "ems-solarflow/x{n}/state", "shellies/ok")
        assert watcher.arrives(meter, "meter/power")
        assert watcher.arrives(mapping, "ems-solarflow/balkon{n}/state")
        assert not watcher.delivered(mapping, "iot/pk/dev{n}/properties/write", "ems-solarflow/ok/state")
        assert watcher.arrives(ems, "iot/pk/dev{n}/properties/write")
        assert not watcher.delivered(ems, "iot/pk/dev{n}/properties/report", "iot/pk/ok/properties/write")
    finally:
        for client in clients:
            client.loop_stop()
            client.disconnect()
        watcher.stop()


def test_a_wrong_password_is_refused_and_anonymous_off_refuses_anonymous(broker):
    admin, port, name = broker

    client, refused = _client(port, "devices", "not-the-password")
    client.loop_stop()
    assert refused

    admin.update({"anonymous": False})
    subprocess.run(["docker", "kill", "-s", "HUP", name], check=True, capture_output=True)
    def anonymous_refused():
        client, refused = _client(port)
        client.loop_stop()
        client.disconnect()
        return refused

    wait_until(anonymous_refused, message="anonymous still accepted")


def test_a_new_account_works_after_a_reload_without_a_restart(broker):
    admin, port, name = broker
    watcher = Watcher(port, admin.password("ems-admin"))
    try:
        admin.update(
            {
                "accounts": [
                    {"name": "devices"}, {"name": "zendure", "zendure": True}, {"name": "meter"}, {"name": "plug"}
                ]
            }
        )
        subprocess.run(["docker", "kill", "-s", "HUP", name], check=True, capture_output=True)

        def connects():
            client, refused = _client(port, "plug", admin.password("plug"))
            client.loop_stop()
            client.disconnect()
            return not refused

        wait_until(connects, message="the new account was not loaded")
        assert watcher.client.is_connected()
    finally:
        watcher.stop()
