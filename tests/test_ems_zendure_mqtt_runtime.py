# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the config-driven Zendure MQTT telemetry runtime holder.

Broker-free: a fake service stands in for the real read-only service so config
selection, snapshot filtering and sanitized status are exercised without a
network broker.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ems.zendure_mqtt import (
    ZendureMqttRuntimeConfig,
    build_zendure_mqtt_runtime,
    classify_zendure_mqtt_devices,
    load_zendure_mqtt_runtime_config,
)
from ems.zendure_mqtt.runtime import ZendureMqttTelemetryRuntime

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.contract,
]


class _Snap:
    def __init__(self, device_id, serial_number=None):
        self.device_id = device_id
        self.serial_number = serial_number


class FakeService:
    def __init__(self, config):
        self.config = config
        self.started = False
        self.stop_calls = 0
        self._snapshots = {}

    def set_snapshots(self, snapshots):
        self._snapshots = dict(snapshots)

    def start(self):
        if self.config.enabled:
            self.started = True

    def stop(self):
        self.stop_calls += 1
        self.started = False

    @property
    def running(self):
        return self.started

    @property
    def connected(self):
        return self.started

    def snapshots(self):
        return dict(self._snapshots)

    def status(self):
        return {
            "enabled": self.config.enabled,
            "running": self.running,
            "connected": self.connected,
            "host": self.config.host,
            "port": self.config.port,
            "snapshot_count": len(self._snapshots),
            "last_error": None,
        }


def _telemetry_device(name="Zendure Battery", device_id="DEV1", topic_family="zensdk_ha_scalar"):
    return {
        "type": "zendure_mqtt",
        "name": name,
        "mqtt": {"topic_family": topic_family, "device_id": device_id},
    }


def test_runtime_status_needs_no_mqtt_client_module():
    """Building a runtime and reading status must not import the paho client.

    Status-only consumers (the Admin container ships no ems.zendure_mqtt.client
    and no paho-mqtt) must be able to import runtime/service and build the
    config-derived offline status. Only start() may touch the client module.
    """

    script = """
import sys

class _BlockClientModule:
    def find_spec(self, name, path=None, target=None):
        if name == "ems.zendure_mqtt.client":
            raise ImportError("blocked for the status-only import chain test")

sys.meta_path.insert(0, _BlockClientModule())

from ems.zendure_mqtt import build_zendure_mqtt_runtime

status = build_zendure_mqtt_runtime({
    "zendure_mqtt": {"host": "broker.local", "port": 1883},
    "devices": [{
        "type": "zendure_mqtt",
        "name": "Battery",
        "mqtt": {"topic_family": "zensdk_ha_scalar", "device_id": "DEV1"},
    }],
}).status()
assert status["enabled"] is True
assert status["broker_configured"] is True
assert "ems.zendure_mqtt.client" not in sys.modules
print("OK")
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


def test_load_runtime_config_absent_is_disabled_without_error():
    config, error = load_zendure_mqtt_runtime_config(None)
    assert config.enabled is False
    assert error is None


def test_load_runtime_config_non_dict_reports_error():
    config, error = load_zendure_mqtt_runtime_config([1, 2])
    assert config.enabled is False
    assert error


def test_load_runtime_config_without_host_is_inactive_without_error():
    # The legacy top-level ``enabled`` key is ignored; a block without a broker
    # host is simply inactive, never a config error.
    config, error = load_zendure_mqtt_runtime_config(
        {"enabled": True, "password": "sup3r-secret-pw"}
    )
    assert config.enabled is False
    assert error is None


def test_classify_ignores_http_and_control_devices():
    valid, invalid = classify_zendure_mqtt_devices(
        [
            {"name": "WR1", "ip": "192.168.1.10", "sn": "SN1"},
            _telemetry_device(name="Good", device_id="DEV1"),
            # Control (write-capable) entries belong to the control path and are
            # excluded from the read-only telemetry classification entirely.
            {
                "type": "zendure_mqtt",
                "name": "Control",
                "mqtt": {"topic_family": "zensdk_ha_scalar", "device_id": "DEV2"},
                "capabilities": {"write_output_limit": True},
            },
        ]
    )
    assert [d.name for d in valid] == ["Good"]
    assert valid[0].identifier == "DEV1"
    assert invalid == []


def test_runtime_without_broker_host_is_inactive_noop():
    # A legacy ``enabled`` key may still be present; it is ignored, and without
    # a broker host the runtime stays inactive.
    runtime = build_zendure_mqtt_runtime(
        {"zendure_mqtt": {"enabled": False}, "devices": [_telemetry_device()]},
        service_factory=FakeService,
    )
    runtime.start()
    status = runtime.status()
    assert status["enabled"] is False
    assert status["broker_configured"] is False
    assert status["running"] is False
    assert status["configured_device_count"] == 1


def test_legacy_enabled_false_with_host_still_starts():
    runtime = build_zendure_mqtt_runtime(
        {
            "zendure_mqtt": {"enabled": False, "host": "broker.local", "port": 1883},
            "devices": [_telemetry_device()],
        },
        service_factory=FakeService,
    )
    runtime.start()
    status = runtime.status()
    assert status["enabled"] is True
    assert status["running"] is True


def test_enabled_runtime_starts_service_and_reports_status():
    runtime = build_zendure_mqtt_runtime(
        {
            "zendure_mqtt": {"enabled": True, "host": "broker.local", "port": 1883},
            "devices": [_telemetry_device()],
        },
        service_factory=FakeService,
    )
    runtime.start()
    status = runtime.status()
    assert status["enabled"] is True
    assert status["broker_configured"] is True
    assert status["running"] is True
    assert status["subscription_count"] >= 1


def test_snapshots_are_filtered_to_configured_identifiers():
    config = ZendureMqttRuntimeConfig.from_dict({"enabled": True, "host": "broker.local"})
    service = FakeService(config)
    runtime = ZendureMqttTelemetryRuntime(
        config,
        classify_zendure_mqtt_devices(
            [
                _telemetry_device(name="A", device_id="DEV1"),
                _telemetry_device(name="B", device_id="SNX"),
            ]
        )[0],
        service=service,
    )
    service.set_snapshots(
        {
            "DEV1": _Snap("DEV1"),
            "OTHER": _Snap("OTHER", serial_number="SNX"),
            "STRANGER": _Snap("STRANGER"),
        }
    )
    snapshots = runtime.snapshots()
    assert set(snapshots) == {"DEV1", "OTHER"}
    assert runtime.status()["snapshot_count"] == 2


def test_no_configured_identifiers_returns_all_snapshots():
    config = ZendureMqttRuntimeConfig.from_dict({"enabled": True, "host": "broker.local"})
    service = FakeService(config)
    runtime = ZendureMqttTelemetryRuntime(config, [], service=service)
    service.set_snapshots({"DEV1": _Snap("DEV1"), "DEV2": _Snap("DEV2")})
    assert set(runtime.snapshots()) == {"DEV1", "DEV2"}


def test_stop_is_idempotent():
    config = ZendureMqttRuntimeConfig.from_dict({"enabled": True, "host": "broker.local"})
    service = FakeService(config)
    runtime = ZendureMqttTelemetryRuntime(config, [], service=service)
    runtime.start()
    runtime.stop()
    runtime.stop()
    assert service.stop_calls == 2
    assert runtime.status()["running"] is False


def test_status_never_exposes_credentials():
    runtime = build_zendure_mqtt_runtime(
        {
            "zendure_mqtt": {
                "enabled": True,
                "host": "broker.local",
                "username": "secretuser",
                "password": "sup3r-secret-pw",
                "app_key": "secretAppKey",
            },
            "devices": [_telemetry_device()],
        },
        service_factory=FakeService,
    )
    runtime.start()
    flattened = json.dumps(runtime.status())
    assert "secretuser" not in flattened
    assert "sup3r-secret-pw" not in flattened
    assert "secretAppKey" not in flattened


def test_runtime_has_no_publish_path():
    runtime = build_zendure_mqtt_runtime(
        {"zendure_mqtt": {"enabled": False}, "devices": []},
        service_factory=FakeService,
    )
    assert not hasattr(runtime, "publish")


def test_disabled_broker_with_devices_reports_sanitized_issue_code():
    runtime = build_zendure_mqtt_runtime(
        {
            "zendure_mqtt": {
                "enabled": True,
                "brokers": {
                    "local_mqtt": {
                        "enabled": False,
                        "source": "local_mqtt",
                        "host": "broker.local",
                        "port": 1883,
                    }
                },
            },
            "devices": [
                {
                    "type": "zendure_mqtt",
                    "name": "Battery",
                    "mqtt": {
                        "broker_ref": "local_mqtt",
                        "topic_family": "zensdk_ha_scalar",
                        "device_id": "DEV1",
                    },
                }
            ],
        },
        service_factory=FakeService,
    )
    broker = next(
        b for b in runtime.status()["brokers"] if b["broker_ref"] == "local_mqtt"
    )
    assert broker["issue"] == "broker_profile_disabled"


def test_hostless_broker_with_devices_reports_incomplete_issue_code():
    runtime = build_zendure_mqtt_runtime(
        {
            "zendure_mqtt": {
                "brokers": {
                    "local_mqtt": {
                        "enabled": True,
                        "source": "local_mqtt",
                    }
                },
            },
            "devices": [
                {
                    "type": "zendure_mqtt",
                    "name": "Battery",
                    "mqtt": {
                        "broker_ref": "local_mqtt",
                        "topic_family": "zensdk_ha_scalar",
                        "device_id": "DEV1",
                    },
                }
            ],
        },
        service_factory=FakeService,
    )
    broker = next(
        b for b in runtime.status()["brokers"] if b["broker_ref"] == "local_mqtt"
    )
    assert broker["issue"] == "broker_profile_incomplete"


def test_usable_broker_reports_no_issue():
    runtime = build_zendure_mqtt_runtime(
        {
            "zendure_mqtt": {"enabled": True, "host": "broker.local", "port": 1883},
            "devices": [_telemetry_device()],
        },
        service_factory=FakeService,
    )
    assert all(broker["issue"] is None for broker in runtime.status()["brokers"])


# --- a configured local credentials_ref never falls back to anonymous --------
# The EMS runtime resolves a broker's credentials_ref through its own Core
# contract: a required field that is present but only whitespace is incomplete,
# so the broker is not built and never dials the broker anonymously.


class _FixedCredentialResolver:
    def __init__(self, credentials):
        self._credentials = credentials

    def resolve(self, ref):
        return self._credentials


@pytest.mark.parametrize(
    "username,password",
    [
        (" ", "sup3r-secret-pw"),
        ("\t", "sup3r-secret-pw"),
        ("\n", "sup3r-secret-pw"),
        ("real-user", " "),
        ("real-user", "\t"),
        ("real-user", "\n"),
    ],
)
def test_local_default_broker_with_whitespace_credential_is_not_built(
    username, password
):
    from ems.mqtt_credentials import MqttCredentials

    runtime = build_zendure_mqtt_runtime(
        {
            "zendure_mqtt": {
                "host": "broker.local",
                "port": 1883,
                "credentials_ref": "home",
            },
            "devices": [_telemetry_device()],
        },
        service_factory=FakeService,
        credential_resolver=_FixedCredentialResolver(
            MqttCredentials(username, password)
        ),
    )
    status = runtime.status()
    # The broker was not built with credentials; the incomplete reference surfaces
    # as a config error naming the default ref, never a credential value.
    assert status.get("config_error")
    assert "default" in status["config_error"]
    # No anonymous fallback: the runtime is not enabled and nothing connects.
    assert status["enabled"] is False
    assert all(
        not broker["connected"] and not broker["running"]
        for broker in status["brokers"]
    )
    assert "sup3r-secret-pw" not in json.dumps(status)


def test_named_local_broker_with_whitespace_credential_lands_in_errors():
    from ems.mqtt_credentials import MqttCredentials
    from ems.zendure_mqtt.runtime import load_zendure_mqtt_broker_configs

    brokers, errors, _stale = load_zendure_mqtt_broker_configs(
        {
            "brokers": {
                "home": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "broker.local",
                    "port": 1883,
                    "credentials_ref": "home",
                }
            }
        },
        credential_resolver=_FixedCredentialResolver(
            MqttCredentials("real-user", "   ")
        ),
    )
    assert "home" not in brokers
    assert "home" in errors
    assert "real-user" not in errors["home"]


def test_config_error_surfaced_in_status():
    runtime = build_zendure_mqtt_runtime(
        {
            "zendure_mqtt": {
                "host": "broker.local",
                "port": "not-a-port",
                "password": "sup3r-secret-pw",
            },
            "devices": [],
        },
        service_factory=FakeService,
    )
    status = runtime.status()
    assert status["enabled"] is False
    assert status["config_error"]
    assert "sup3r-secret-pw" not in json.dumps(status)


def test_hostless_block_reports_no_config_error():
    runtime = build_zendure_mqtt_runtime(
        {
            "zendure_mqtt": {"enabled": True, "password": "sup3r-secret-pw"},
            "devices": [],
        },
        service_factory=FakeService,
    )
    status = runtime.status()
    assert status["enabled"] is False
    assert "config_error" not in status
    assert "sup3r-secret-pw" not in json.dumps(status)


def test_write_status_file_round_trips_sanitized_status(tmp_path):
    runtime = build_zendure_mqtt_runtime(
        {
            "zendure_mqtt": {
                "enabled": True,
                "host": "broker.local",
                "port": 1883,
                "password": "sup3r-secret-pw",
            },
            "devices": [_telemetry_device()],
        },
        service_factory=FakeService,
    )
    path = tmp_path / "data" / "zendure-mqtt-status.json"
    assert runtime.write_status_file(path) is True
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload["written_at"], (int, float))
    assert payload["status"] == runtime.status()
    assert "sup3r-secret-pw" not in path.read_text(encoding="utf-8")


def test_write_status_file_merges_control_status(tmp_path):
    runtime = build_zendure_mqtt_runtime(
        {
            "zendure_mqtt": {"enabled": True, "host": "broker.local"},
            "devices": [_telemetry_device()],
        },
        service_factory=FakeService,
    )
    control_status = {
        "accepted_control_devices": 1,
        "devices": [
            {
                "name": "Ctrl",
                "broker_ref": "default",
                "source": "local_mqtt",
                "control_enabled": True,
                "write_gate": "allow_mqtt_local_control_writes",
                "state": "unseen",
            }
        ],
    }
    path = tmp_path / "data" / "zendure-mqtt-status.json"
    assert runtime.write_status_file(path, control_status=control_status) is True
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"]["control"] == control_status
    # Telemetry-only writes still omit the control block entirely.
    assert runtime.write_status_file(path) is True
    assert "control" not in json.loads(path.read_text(encoding="utf-8"))["status"]


def test_write_status_file_masks_cloud_route_and_command_topic(tmp_path):
    runtime = build_zendure_mqtt_runtime(
        {"zendure_mqtt": {"host": "broker.local"}, "devices": []},
        service_factory=FakeService,
    )
    route = "ACCOUNT_ROUTE_1234"
    topic = f"iot/PRODUCT_SECRET/{route}/properties/write"
    control_status = {
        "devices": [
            {
                "name": "Cloud battery",
                "broker_ref": "cloud_a",
                "source": "zendure_cloud_mqtt",
                "identifier": route,
                "product_key": "PRODUCT_SECRET",
                "effective_write_topic": topic,
                "last_command": {
                    "device_id": route,
                    "topic": topic,
                    "correlation_id": "safe-correlation-id",
                },
                "password": "BROKER_PASSWORD",
                "app_key": "APP_KEY_SECRET",
            }
        ],
        "authorization_code": "AUTHORIZATION_SECRET",
    }
    path = tmp_path / "data" / "zendure-mqtt-status.json"

    assert runtime.write_status_file(path, control_status=control_status) is True
    payload = json.loads(path.read_text(encoding="utf-8"))
    flattened = json.dumps(payload)

    for secret in (
        route,
        topic,
        "PRODUCT_SECRET",
        "BROKER_PASSWORD",
        "APP_KEY_SECRET",
        "AUTHORIZATION_SECRET",
    ):
        assert secret not in flattened
    device = payload["status"]["control"]["devices"][0]
    assert device["identifier"] == "…1234"
    assert device["effective_write_topic"] == "iot/…/…/properties/write"
    assert device["last_command"]["correlation_id"] == "safe-correlation-id"


def test_write_status_file_masks_route_left_only_in_invalid_device_name(tmp_path):
    route = "ACCOUNT_ROUTE_7501"
    product = "PRODUCT_KEY_7501"
    config = {
        "zendure_mqtt": {
            "brokers": {
                "cloud_a": {
                    "enabled": False,
                    "source": "zendure_cloud_mqtt",
                    "host": "mqtt.example.invalid",
                    "port": 8883,
                }
            }
        },
        "devices": [
            {
                "type": "zendure_mqtt",
                "name": f"Rejected Cloud {route}",
                "mqtt": {
                    "broker_ref": "cloud_a",
                    # Deliberately invalid: classification drops the route/source
                    # fields but retains the operator-facing display name.
                    "device_id": route,
                    "product_key": product,
                },
                "capabilities": {"write_output_limit": False},
            }
        ],
    }
    runtime = build_zendure_mqtt_runtime(config, service_factory=FakeService)
    assert runtime.status()["invalid_device_count"] == 1
    path = tmp_path / "data" / "zendure-mqtt-status.json"

    assert runtime.write_status_file(path) is True

    flattened = path.read_text(encoding="utf-8")
    assert route not in flattened
    assert product not in flattened
    payload = json.loads(flattened)
    assert payload["status"]["devices"][0]["name"] != f"Rejected Cloud {route}"


def test_write_status_file_masks_name_only_route_in_incomplete_cloud_config(tmp_path):
    route = "SECRET_CLOUD_ROUTE_7501"
    config = {
        "zendure_mqtt": {
            "brokers": {
                "cloud_a": {
                    "source": "zendure_cloud_mqtt",
                    "host": "mqtt.example.invalid",
                }
            }
        },
        "devices": [
            {
                "type": "zendure_mqtt",
                "name": f"Rejected Cloud {route}",
                "mqtt": {"broker_ref": "cloud_a"},
            }
        ],
    }
    runtime = build_zendure_mqtt_runtime(config, service_factory=FakeService)
    path = tmp_path / "data" / "zendure-mqtt-status.json"

    assert runtime.write_status_file(path) is True
    assert route not in path.read_text(encoding="utf-8")


def test_write_status_file_swallows_io_errors(tmp_path):
    runtime = build_zendure_mqtt_runtime(
        {"zendure_mqtt": {"enabled": False}, "devices": []},
        service_factory=FakeService,
    )
    # Target parent is a file, so mkdir/replace cannot succeed.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    assert runtime.write_status_file(blocker / "status.json") is False
