# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stale MQTT telemetry must be rejected in the control read path.

Uses an injected monotonic clock (no real sleeps) so the controller can never
act on a device whose broker disconnected or stopped publishing.
"""

from types import SimpleNamespace

from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient
from ems.zendure_mqtt.service import (
    SNAPSHOT_FRESH,
    SNAPSHOT_STALE,
    SNAPSHOT_UNSEEN,
    classify_snapshot,
)
from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON


def _snapshot(last_seen_monotonic, metrics=None):
    return SimpleNamespace(
        metrics=metrics or {"electricLevel": 55, "outputLimit": 300},
        last_seen_monotonic=last_seen_monotonic,
    )


# --- classifier -------------------------------------------------------------


def test_classify_unseen_when_snapshot_missing():
    status = classify_snapshot(None, 60.0, now_monotonic=100.0)
    assert status.state == SNAPSHOT_UNSEEN
    assert status.is_fresh is False


def test_classify_fresh_within_window():
    status = classify_snapshot(_snapshot(100.0), 60.0, now_monotonic=130.0)
    assert status.state == SNAPSHOT_FRESH
    assert status.age_seconds == 30.0
    assert status.is_fresh is True


def test_classify_stale_beyond_window():
    status = classify_snapshot(_snapshot(100.0), 60.0, now_monotonic=200.0)
    assert status.state == SNAPSHOT_STALE
    assert status.age_seconds == 100.0
    assert status.is_fresh is False


# --- device adapter fetch ---------------------------------------------------


class ClockService:
    """Service double whose freshness is driven by an injectable clock."""

    def __init__(self, snapshot, stale_after=60.0):
        self._snapshot = snapshot
        self._stale_after = stale_after
        self.now = 100.0

    def set_snapshot(self, snapshot):
        self._snapshot = snapshot

    def snapshot_status(self, device_id, *, now_monotonic=None):
        snap = self._snapshot if device_id == "DEV" else None
        return classify_snapshot(snap, self._stale_after, now_monotonic=self.now)


def _device(service):
    return ZendureMqttDeviceClient(
        "WR-MQTT",
        service,
        device_id="DEV",
        topic_family=FAMILY_LEGACY_JSON,
        source="local_mqtt",
        product_key="PK",
        max_power=800,
    )


def test_fresh_snapshot_is_accepted():
    service = ClockService(_snapshot(100.0))
    service.now = 130.0
    dev = _device(service)
    state = dev.fetch()
    assert state is not None
    assert state.soc == 55


def test_missing_snapshot_is_rejected():
    dev = _device(ClockService(None))
    assert dev.fetch() is None
    assert dev.read_health.last_error == "no_snapshot"


def test_expired_snapshot_is_rejected():
    service = ClockService(_snapshot(100.0))
    service.now = 400.0
    dev = _device(service)
    assert dev.fetch() is None
    assert dev.read_health.last_error == "mqtt_snapshot_stale"


def test_snapshot_becomes_stale_after_updates_stop():
    service = ClockService(_snapshot(100.0))
    dev = _device(service)
    service.now = 130.0
    assert dev.fetch() is not None
    # Broker stops publishing: the snapshot ages past the window.
    service.now = 300.0
    assert dev.fetch() is None
    assert dev.read_health.last_error == "mqtt_snapshot_stale"


def test_fresh_telemetry_after_stale_restores_device():
    service = ClockService(_snapshot(100.0))
    dev = _device(service)
    service.now = 400.0
    assert dev.fetch() is None
    # A new report arrives; last_seen advances and control resumes.
    service.set_snapshot(_snapshot(390.0))
    assert dev.fetch() is not None


def test_stale_device_is_not_treated_as_healthy():
    service = ClockService(_snapshot(100.0))
    service.now = 400.0
    dev = _device(service)
    for _ in range(3):
        assert dev.fetch() is None
    assert dev.read_health.classify() == "failed"
