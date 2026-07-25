# SPDX-License-Identifier: AGPL-3.0-or-later
"""Effectiveness-based telemetry confirmation and foreign-writer detection."""

import logging

import pytest

from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient
from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON

pytestmark = [pytest.mark.simulation, pytest.mark.power_control]

APPLIED = {"outputLimit": 300, "acMode": 2, "smartMode": 1, "inputLimit": 0}


class _FakeSnapshot:
    def __init__(self, metrics, last_seen_monotonic, metric_monotonic=None):
        self.metrics = metrics
        self.last_seen_monotonic = last_seen_monotonic
        self.metric_monotonic = metric_monotonic or {
            key: last_seen_monotonic for key in metrics
        }


class _FakeService:
    def __init__(self):
        self.published = []
        self.connected = True
        self._snapshot = None

    def set_snapshot(self, metrics, last_seen_monotonic, metric_monotonic=None):
        self._snapshot = _FakeSnapshot(
            dict(metrics), last_seen_monotonic, metric_monotonic
        )

    def snapshot_status(self, device_id, *, now_monotonic=None):
        from ems.zendure_mqtt.service import classify_snapshot

        return classify_snapshot(
            self._snapshot, 60.0, now_monotonic=now_monotonic or 0.0
        )

    def publish_output_limit(self, topic, payload):
        self.published.append((topic, payload))
        return True


def _device(**kwargs):
    return ZendureMqttDeviceClient(
        "INV",
        _FakeService(),
        device_id="DEV",
        topic_family=FAMILY_LEGACY_JSON,
        source="zendure_cloud_mqtt",
        product_key="PK",
        hardware_profile="solarflow_800_pro_2",
        max_power=2000,
        **kwargs,
    )


def _published(dev, target=300):
    dev.write_output_limit(target)
    return dev._active_command


def _snapshot_after(record, metrics, offset=1.0, **kwargs):
    return dict(metrics), record.published_monotonic + offset


# --- expected-property confirmation ------------------------------------------


def test_full_applied_state_confirms():
    dev = _device()
    rec = _published(dev)
    metrics, ts = _snapshot_after(rec, APPLIED)
    dev._service.set_snapshot(metrics, ts)
    dev.fetch()
    assert rec.state == "telemetry_confirmed"


def test_missing_ac_mode_blocks_confirmation():
    dev = _device()
    rec = _published(dev)
    metrics = {"outputLimit": 300, "smartMode": 1}
    dev._service.set_snapshot(metrics, rec.published_monotonic + 1.0)
    dev.fetch()
    assert rec.state == "published"


def test_wrong_ac_mode_blocks_confirmation():
    dev = _device()
    rec = _published(dev)
    metrics = dict(APPLIED, acMode=1)
    dev._service.set_snapshot(metrics, rec.published_monotonic + 1.0)
    dev.fetch()
    assert rec.state == "published"


def test_wrong_smart_mode_blocks_but_absent_smart_mode_is_tolerated():
    dev = _device()
    rec = _published(dev)
    dev._service.set_snapshot(
        dict(APPLIED, smartMode=0), rec.published_monotonic + 1.0
    )
    dev.fetch()
    assert rec.state == "published"

    absent = {"outputLimit": 300, "acMode": 2, "inputLimit": 0}
    dev._service.set_snapshot(absent, rec.published_monotonic + 2.0)
    dev.fetch()
    assert rec.state == "telemetry_confirmed"


def test_incompatible_input_limit_blocks_confirmation():
    dev = _device()
    rec = _published(dev)
    dev._service.set_snapshot(
        dict(APPLIED, inputLimit=400), rec.published_monotonic + 1.0
    )
    dev.fetch()
    assert rec.state == "published"


def test_stale_output_metric_in_fresh_snapshot_cannot_confirm():
    """A merged snapshot refreshed by an unrelated message must not confirm a
    command from an outputLimit value that predates the publish.
    """

    dev = _device()
    rec = _published(dev)
    now = rec.published_monotonic
    metric_times = {key: now + 1.0 for key in APPLIED}
    metric_times["outputLimit"] = now - 5.0
    dev._service.set_snapshot(dict(APPLIED), now + 1.0, metric_times)
    dev.fetch()
    assert rec.state == "published"


# --- lifecycle events --------------------------------------------------------


def test_confirmation_timeout_and_confirmation_emit_events(caplog):
    dev = _device(confirmation_timeout_seconds=5.0)
    rec = _published(dev)
    with caplog.at_level(logging.INFO):
        dev.describe(now_monotonic=rec.published_monotonic + 6.0)
    assert any("event=confirmation_timed_out" in m for m in caplog.messages)

    caplog.clear()
    rec2 = _published(dev, 250)
    metrics = dict(APPLIED, outputLimit=250)
    dev._service.set_snapshot(metrics, rec2.published_monotonic + 1.0)
    with caplog.at_level(logging.INFO):
        dev.fetch()
    assert any("event=telemetry_confirmed" in m for m in caplog.messages)


# --- foreign-writer detection ------------------------------------------------


def _confirmed_device():
    dev = _device()
    rec = _published(dev)
    dev._service.set_snapshot(dict(APPLIED), rec.published_monotonic + 1.0)
    dev.fetch()
    assert rec.state == "telemetry_confirmed"
    return dev, rec


def test_two_newer_deviating_reports_raise_suspicion(caplog):
    dev, rec = _confirmed_device()
    base = rec.published_monotonic
    with caplog.at_level(logging.WARNING):
        dev._service.set_snapshot(dict(APPLIED, outputLimit=800), base + 10.0)
        dev.fetch()
        assert dev.describe()["external_control_suspected"] is False
        dev._service.set_snapshot(dict(APPLIED, outputLimit=800), base + 20.0)
        dev.fetch()
    described = dev.describe()
    assert described["external_control_suspected"] is True
    assert described["external_control_detail"]["expected_w"] == 300
    assert described["external_control_detail"]["observed_w"] == 800
    assert any("event=external_control_suspected" in m for m in caplog.messages)


def test_unchanged_stale_report_never_inflates_the_streak():
    dev, rec = _confirmed_device()
    base = rec.published_monotonic
    dev._service.set_snapshot(dict(APPLIED, outputLimit=800), base + 10.0)
    dev.fetch()
    dev.fetch()
    dev.fetch()
    assert dev.describe()["external_control_suspected"] is False


def test_matching_report_resets_the_streak():
    dev, rec = _confirmed_device()
    base = rec.published_monotonic
    dev._service.set_snapshot(dict(APPLIED, outputLimit=800), base + 10.0)
    dev.fetch()
    dev._service.set_snapshot(dict(APPLIED), base + 20.0)
    dev.fetch()
    dev._service.set_snapshot(dict(APPLIED, outputLimit=800), base + 30.0)
    dev.fetch()
    assert dev.describe()["external_control_suspected"] is False


def test_new_local_confirmation_clears_suspicion():
    dev, rec = _confirmed_device()
    base = rec.published_monotonic
    dev._service.set_snapshot(dict(APPLIED, outputLimit=800), base + 10.0)
    dev.fetch()
    dev._service.set_snapshot(dict(APPLIED, outputLimit=800), base + 20.0)
    dev.fetch()
    assert dev.describe()["external_control_suspected"] is True

    rec2 = _published(dev, 500)
    dev._service.set_snapshot(
        dict(APPLIED, outputLimit=500), rec2.published_monotonic + 1.0
    )
    dev.fetch()
    assert rec2.state == "telemetry_confirmed"
    assert dev.describe()["external_control_suspected"] is False
