# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the sanitized Zendure MQTT runtime status/snapshot summary.

Broker-free and clock-injected so unseen/online/stale/invalid states are
exercised deterministically without a network broker and without sleeping.
"""

import json
import sys

import pytest

from ems.zendure_mqtt import (
    build_zendure_mqtt_runtime,
    summarize_zendure_mqtt_devices,
)
from ems.zendure_mqtt.runtime import (
    ZendureMqttTelemetryRuntime,
    classify_zendure_mqtt_devices,
)
from ems.zendure_mqtt.service import ZendureMqttRuntimeConfig
from ems.zendure_mqtt.snapshot import ZendureMqttSnapshot

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
]


class FakeService:
    def __init__(self, config):
        self.config = config
        self.started = False
        self._snapshots = {}

    def set_snapshots(self, snapshots):
        self._snapshots = dict(snapshots)

    def start(self):
        if self.config.enabled:
            self.started = True

    def stop(self):
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


def _telemetry_device(name="Battery", device_id="DEV1", topic_family="zensdk_ha_scalar"):
    return {
        "type": "zendure_mqtt",
        "name": name,
        "mqtt": {"topic_family": topic_family, "device_id": device_id},
    }


def _snapshot(device_id="DEV1", *, metrics=None, capabilities=None, last_seen_monotonic=100.0):
    return ZendureMqttSnapshot(
        device_id=device_id,
        metrics=dict(metrics or {}),
        capabilities=set(capabilities or set()),
        last_seen_epoch=1_700_000_000.0,
        last_seen_monotonic=last_seen_monotonic,
    )


def _runtime(devices_cfg, *, enabled=True, host="broker.local", stale_after_seconds=60.0):
    config = ZendureMqttRuntimeConfig.from_dict(
        {"enabled": enabled, "host": host, "stale_after_seconds": stale_after_seconds}
    )
    service = FakeService(config)
    valid, invalid = classify_zendure_mqtt_devices(devices_cfg)
    runtime = ZendureMqttTelemetryRuntime(
        config, valid, invalid_devices=invalid, service=service
    )
    return runtime, service


def test_unseen_device_reports_unseen():
    runtime, _service = _runtime([_telemetry_device(device_id="DEV1")])
    runtime.start()
    summaries = runtime.device_summaries(now_monotonic=200.0)
    assert len(summaries) == 1
    device = summaries[0]
    assert device["status"] == "unseen"
    assert device["last_seen"] is None
    assert device["age_seconds"] is None
    assert device["metric_count"] == 0
    assert device["metrics"] == []
    assert device["write_output_limit"] is False


def test_matching_snapshot_reports_online_with_metrics():
    runtime, service = _runtime([_telemetry_device(device_id="DEV1")])
    runtime.start()
    service.set_snapshots(
        {
            "DEV1": _snapshot(
                "DEV1",
                metrics={"electricLevel": 55, "outputHomePower": 120, "solarInputPower": 30},
                capabilities={"battery_storage", "pv_input"},
                last_seen_monotonic=150.0,
            )
        }
    )
    device = runtime.device_summaries(now_monotonic=155.0)[0]
    assert device["status"] == "online"
    assert device["age_seconds"] == 5.0
    assert device["metric_count"] == 3
    assert device["metrics"] == ["electricLevel", "outputHomePower", "solarInputPower"]
    assert device["capabilities"] == ["battery_storage", "pv_input"]
    assert device["last_seen"] is not None


def test_old_snapshot_reports_stale_without_sleeping():
    runtime, service = _runtime(
        [_telemetry_device(device_id="DEV1")], stale_after_seconds=60.0
    )
    runtime.start()
    service.set_snapshots(
        {"DEV1": _snapshot("DEV1", metrics={"electricLevel": 10}, last_seen_monotonic=100.0)}
    )
    device = runtime.device_summaries(now_monotonic=1000.0)[0]
    assert device["status"] == "stale"
    assert device["age_seconds"] == 900.0


def test_invalid_device_appears_as_invalid():
    # A telemetry entry missing its device identifier is genuinely invalid.
    invalid_cfg = {
        "type": "zendure_mqtt",
        "name": "BadTelemetry",
        "mqtt": {"topic_family": "zensdk_ha_scalar"},
    }
    runtime, _service = _runtime([_telemetry_device(device_id="DEV1"), invalid_cfg])
    assert runtime.invalid_device_count == 1
    summaries = runtime.device_summaries(now_monotonic=1.0)
    invalid = [d for d in summaries if d["status"] == "invalid"]
    assert len(invalid) == 1
    assert invalid[0]["name"] == "BadTelemetry"
    assert "device_identifier_missing" in invalid[0]["issues"]
    assert invalid[0]["write_output_limit"] is False


def test_status_includes_endpoint_and_counts():
    runtime, _service = _runtime([_telemetry_device()])
    runtime.start()
    status = runtime.status()
    assert status["endpoint"] == "broker.local:1883"
    assert status["configured_device_count"] == 1
    assert status["invalid_device_count"] == 0
    assert status["stale_after_seconds"] == 60.0
    assert isinstance(status["devices"], list)


def test_write_output_limit_false_for_every_summary():
    runtime, service = _runtime([_telemetry_device(device_id="DEV1")])
    runtime.start()
    service.set_snapshots({"DEV1": _snapshot("DEV1", metrics={"outputLimit": 100})})
    for device in runtime.device_summaries(now_monotonic=101.0):
        assert device["write_output_limit"] is False


def test_missing_broker_config_does_not_crash_status():
    runtime = build_zendure_mqtt_runtime(
        {"zendure_mqtt": {"enabled": True}, "devices": [_telemetry_device()]},
        service_factory=FakeService,
    )
    status = runtime.status()
    assert status["enabled"] is False
    assert status["endpoint"] is None
    assert status["broker_configured"] is False
    # No broker host is not a config error: the feature is always on and simply
    # inactive until a broker is configured.
    assert "config_error" not in status
    assert isinstance(status["devices"], list)


def test_status_summary_is_credential_free():
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
    for secret in ("secretuser", "sup3r-secret-pw", "secretAppKey"):
        assert secret not in flattened


def test_summary_builder_needs_no_mqtt_client_module():
    """The status path must not require the paho-backed read client module."""

    sys.modules.pop("ems.zendure_mqtt.client", None)
    summaries = summarize_zendure_mqtt_devices(
        classify_zendure_mqtt_devices([_telemetry_device()])[0],
        [],
        {},
        now_monotonic=1.0,
        stale_after_seconds=60.0,
    )
    assert summaries[0]["status"] == "unseen"
    assert "ems.zendure_mqtt.client" not in sys.modules
