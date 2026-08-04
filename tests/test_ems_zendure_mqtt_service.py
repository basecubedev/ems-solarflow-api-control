# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the read-only Zendure MQTT runtime telemetry service.

Broker-free: a fake paho-style client is injected through a real read client so
the aggregator path is exercised end-to-end without a network broker.
"""

import json

import pytest

from ems.zendure_mqtt import (
    ZendureMqttClientConfig,
    ZendureMqttConfigError,
    ZendureMqttReadClient,
    ZendureMqttRuntimeConfig,
    ZendureMqttService,
)

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
]


class FakeMessage:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload


class FakeMqttClient:
    """Minimal paho-style stand-in that records calls and delivers messages."""

    def __init__(self, *, fail_connect=False):
        self.fail_connect = fail_connect
        self.connect_timeout = None
        self.subscriptions = []
        self.username_pw = None
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.publish_calls = []
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None

    def tls_set(self, *args, **kwargs):
        pass

    def tls_insecure_set(self, value):
        pass

    def username_pw_set(self, username, password=None):
        self.username_pw = (username, password)

    def connect(self, host, port, keepalive=0):
        if self.fail_connect:
            raise OSError("connection refused")

    def loop_start(self):
        self.loop_started = True
        if self.on_connect is not None:
            self.on_connect(self, None, None, 0)

    def loop_stop(self):
        self.loop_stopped = True

    def disconnect(self):
        self.disconnected = True

    def subscribe(self, topic, qos=0):
        self.subscriptions.append(topic)

    def publish(self, *args, **kwargs):  # pragma: no cover - must never be called
        self.publish_calls.append((args, kwargs))

    def deliver(self, topic, payload):
        self.on_message(self, None, FakeMessage(topic, payload))


def _service(config, *, fake=None):
    """Build a service whose read client is backed by an injectable fake broker."""

    fake = fake if fake is not None else FakeMqttClient()
    captured = {}

    def read_client_factory(client_config):
        captured["client_config"] = client_config
        return ZendureMqttReadClient(client_config, client_factory=lambda _cfg: fake)

    service = ZendureMqttService(config, read_client_factory=read_client_factory)
    return service, fake, captured


def _enabled_config(**kwargs):
    data = {"enabled": True, "host": "broker.local"}
    data.update(kwargs)
    return ZendureMqttRuntimeConfig.from_dict(data)


def test_disabled_service_does_not_create_or_connect_client():
    created = []

    def factory(_client_config):
        created.append(_client_config)
        raise AssertionError("disabled service must not build a client")

    config = ZendureMqttRuntimeConfig.from_dict({"enabled": False})
    service = ZendureMqttService(config, read_client_factory=factory)
    service.start()
    assert created == []
    assert service.running is False
    assert service.connected is False
    assert service.snapshots() == {}


def test_enabled_service_starts_read_client_with_expected_config():
    config = _enabled_config(port=8883, app_key="secretAppKey", tls=True)
    service, fake, captured = _service(config)
    service.start()
    assert service.running is True
    assert service.connected is True
    client_config = captured["client_config"]
    assert isinstance(client_config, ZendureMqttClientConfig)
    assert client_config.host == "broker.local"
    assert client_config.port == 8883
    assert client_config.app_key == "secretAppKey"
    assert "secretAppKey/#" in fake.subscriptions
    assert "#" not in fake.subscriptions


def test_stop_is_safe_when_never_started():
    service, _fake, _captured = _service(_enabled_config())
    service.stop()  # must not raise
    assert service.running is False


def test_repeated_start_and_stop_are_safe():
    service, fake, _captured = _service(_enabled_config())
    service.start()
    service.start()  # idempotent: no second client, no re-subscribe storm
    assert fake.subscriptions.count("Zendure/#") == 1
    service.stop()
    service.stop()
    assert fake.loop_stopped and fake.disconnected
    assert service.running is False


def test_failed_start_is_retried_only_after_cooldown(monkeypatch):
    # A broker that is down at EMS boot must not stay dead until a process
    # restart: start() may be re-invoked from the control loop and retries —
    # but throttled, so a 5s loop cannot hammer the broker with blocking
    # connect attempts every cycle.
    from ems.zendure_mqtt import service as service_module

    clock = {"now": 1000.0}
    monkeypatch.setattr(service_module.time, "monotonic", lambda: clock["now"])

    attempts = []

    def factory(client_config):
        fake = FakeMqttClient(fail_connect=len(attempts) == 0)
        attempts.append(fake)
        return ZendureMqttReadClient(client_config, client_factory=lambda _cfg: fake)

    service = ZendureMqttService(_enabled_config(), read_client_factory=factory)
    service.start()
    assert len(attempts) == 1
    assert service.running is False

    clock["now"] += 1.0
    service.start()  # within cooldown: no new connect attempt
    assert len(attempts) == 1

    clock["now"] += service_module.START_RETRY_COOLDOWN_SECONDS
    service.start()
    assert len(attempts) == 2
    assert service.running is True


def test_stop_then_start_is_not_throttled():
    # The cooldown only applies to failed connect attempts; an operator
    # stop/start cycle restarts immediately.
    fakes = []

    def factory(client_config):
        fake = FakeMqttClient()
        fakes.append(fake)
        return ZendureMqttReadClient(client_config, client_factory=lambda _cfg: fake)

    service = ZendureMqttService(_enabled_config(), read_client_factory=factory)
    service.start()
    assert service.running is True
    service.stop()
    service.start()
    assert service.running is True
    assert len(fakes) == 2


def test_incoming_messages_update_snapshots_through_aggregator():
    service, fake, _captured = _service(_enabled_config())
    service.start()
    fake.deliver("Zendure/sensor/DEV123/electricLevel", "43")
    payload = json.dumps({"sn": "SN9", "properties": {"outputLimit": 301}})
    fake.deliver("iot/prodKey/DEVLEG/properties/report", payload)
    snapshots = service.snapshots()
    assert snapshots["DEV123"].metrics["electricLevel"] == 43
    assert snapshots["DEVLEG"].metrics["outputLimit"] == 301


def test_status_reports_running_connected_and_snapshot_count():
    service, fake, _captured = _service(_enabled_config())
    service.start()
    fake.deliver("Zendure/sensor/DEV123/electricLevel", "43")
    status = service.status()
    assert status["enabled"] is True
    assert status["running"] is True
    assert status["connected"] is True
    assert status["snapshot_count"] == 1


def test_status_and_repr_never_expose_credentials():
    config = _enabled_config(username="secretuser", password="sup3r-secret-pw")
    service, _fake, _captured = _service(config)
    service.start()
    status = service.status()
    flattened = json.dumps(status)
    assert "secretuser" not in flattened
    assert "sup3r-secret-pw" not in flattened
    assert "sup3r-secret-pw" not in repr(config)
    assert "secretuser" not in json.dumps(list(status.keys()))


def test_start_catches_client_error_with_client_module_absent():
    """The client-error identity must not depend on the client module's life.

    A re-imported ems.zendure_mqtt.client (e.g. after a sys.modules pop in a
    status-only context) must not create a second error-class identity that
    slips past service.start()'s except clause — and start() must not need the
    client module at all when the factory is injected.
    """

    import sys

    from ems.zendure_mqtt.client import ZendureMqttClientError

    class _FailingClient:
        def start(self):
            raise ZendureMqttClientError("failed to connect")

        def stop(self):
            pass

    service = ZendureMqttService(
        _enabled_config(), read_client_factory=lambda _cfg: _FailingClient()
    )
    saved = sys.modules.pop("ems.zendure_mqtt.client")
    try:
        service.start()  # must swallow the error, never re-import the client
        assert "ems.zendure_mqtt.client" not in sys.modules
    finally:
        sys.modules["ems.zendure_mqtt.client"] = saved
    assert service.running is False


def test_config_without_host_is_inactive_without_error():
    config = ZendureMqttRuntimeConfig.from_dict({"password": "sup3r-secret-pw"})
    assert config.enabled is False


def test_config_with_host_is_active_without_enabled_key():
    config = ZendureMqttRuntimeConfig.from_dict({"host": "broker.local"})
    assert config.enabled is True


def test_legacy_top_level_enabled_false_is_ignored():
    config = ZendureMqttRuntimeConfig.from_dict(
        {"enabled": False, "host": "broker.local"}
    )
    assert config.enabled is True


def test_legacy_top_level_enabled_junk_is_ignored():
    config = ZendureMqttRuntimeConfig.from_dict(
        {"enabled": "junk", "host": "broker.local"}
    )
    assert config.enabled is True


def test_custom_subscriptions_accepted_and_bare_wildcard_rejected():
    config = ZendureMqttRuntimeConfig.from_dict(
        {
            "enabled": True,
            "host": "broker.local",
            "subscriptions": ["myapp/#", "#", "  ", "myapp/#"],
        }
    )
    assert config.subscriptions == ("myapp/#",)
    service, fake, _captured = _service(config)
    service.start()
    assert "myapp/#" in fake.subscriptions
    assert "#" not in fake.subscriptions


def test_only_bare_wildcard_falls_back_to_known_families():
    config = ZendureMqttRuntimeConfig.from_dict(
        {"enabled": True, "host": "broker.local", "subscriptions": ["#"]}
    )
    assert config.subscriptions is None
    service, fake, _captured = _service(config)
    service.start()
    assert "#" not in fake.subscriptions
    assert "Zendure/#" in fake.subscriptions


def test_string_subscriptions_are_rejected():
    with pytest.raises(ZendureMqttConfigError) as excinfo:
        ZendureMqttRuntimeConfig.from_dict(
            {
                "enabled": True,
                "host": "broker.local",
                "password": "sup3r-secret-pw",
                "username": "secretuser",
                "subscriptions": "Zendure/#",
            }
        )
    message = str(excinfo.value)
    assert "topic filters" in message
    assert "sup3r-secret-pw" not in message
    assert "secretuser" not in message


def test_non_iterable_subscriptions_are_rejected():
    with pytest.raises(ZendureMqttConfigError):
        ZendureMqttRuntimeConfig.from_dict(
            {"enabled": True, "host": "broker.local", "subscriptions": 5}
        )


def test_non_string_entries_are_rejected():
    with pytest.raises(ZendureMqttConfigError):
        ZendureMqttRuntimeConfig.from_dict(
            {
                "enabled": True,
                "host": "broker.local",
                "subscriptions": ["Zendure/#", 42],
            }
        )
    with pytest.raises(ZendureMqttConfigError):
        ZendureMqttRuntimeConfig.from_dict(
            {
                "enabled": True,
                "host": "broker.local",
                "subscriptions": ["Zendure/#", {"topic": "x"}],
            }
        )


def test_list_subscriptions_still_work():
    config = ZendureMqttRuntimeConfig.from_dict(
        {
            "enabled": True,
            "host": "broker.local",
            "subscriptions": ["myapp/#", "myapp/#", "  ", "other/#"],
        }
    )
    assert config.subscriptions == ("myapp/#", "other/#")


def test_bare_wildcard_only_list_falls_back_to_defaults():
    config = ZendureMqttRuntimeConfig.from_dict(
        {"enabled": True, "host": "broker.local", "subscriptions": ["#"]}
    )
    assert config.subscriptions is None
    service, fake, _captured = _service(config)
    service.start()
    assert "#" not in fake.subscriptions
    assert "Zendure/#" in fake.subscriptions


def test_start_records_sanitized_last_error_on_connect_failure():
    config = _enabled_config(username="secretuser", password="sup3r-secret-pw")
    fake = FakeMqttClient(fail_connect=True)
    service, _fake, _captured = _service(config, fake=fake)
    service.start()  # connection failure must never propagate to the caller
    assert service.running is False
    status = service.status()
    assert status["last_error"]
    assert "sup3r-secret-pw" not in status["last_error"]
    assert "secretuser" not in status["last_error"]


def test_service_exposes_no_publish_path():
    service, fake, _captured = _service(_enabled_config())
    service.start()
    fake.deliver("Zendure/sensor/DEV123/electricLevel", "43")
    service.stop()
    assert fake.publish_calls == []
    assert not hasattr(service, "publish")
