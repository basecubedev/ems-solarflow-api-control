# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 1: no-ack (properties/write) command completion.

A ZenSDK ``properties/write`` has no verified acknowledgement reply contract, so
a published command can never be *acknowledged*. It must instead:

* be confirmed directly from fresh, target-matching telemetry
  (``published`` -> ``telemetry_confirmed``), releasing the slot and letting the
  next pending target publish immediately, without any acknowledgement;
* reject telemetry older than the publish (retained/stale) or outside the
  policy-defined tolerance;
* reach a bounded, *honest* ``confirmation_timed_out`` (never an acknowledgement
  ``timed_out``) when no matching telemetry ever arrives;
* complete immediately as ``completed_unconfirmed`` when the profile is neither
  acknowledgeable nor telemetry-confirmable, rather than occupying the active
  slot until a meaningless acknowledgement timeout.

The confirmation deadline for a no-ack command starts at successful publish time.
"""

import pytest

from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient
from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON

pytestmark = pytest.mark.simulation


class _FakeSnapshot:
    def __init__(self, metrics, last_seen_monotonic):
        self.metrics = metrics
        self.last_seen_monotonic = last_seen_monotonic


class _FakeService:
    def __init__(self):
        self.published = []
        self._snapshot = None
        self.connected = True

    def set_snapshot(self, metrics, last_seen_monotonic):
        self._snapshot = _FakeSnapshot(metrics, last_seen_monotonic)

    def snapshot_status(self, device_id, *, now_monotonic=None):
        from ems.zendure_mqtt.service import classify_snapshot

        return classify_snapshot(
            self._snapshot, 60.0, now_monotonic=now_monotonic or 0.0
        )

    def publish_output_limit(self, topic, payload):
        self.published.append((topic, payload))
        return True


def _zensdk_device(**kwargs):
    """A ZenSDK properties/write device: no ack contract, telemetry-confirmable."""

    return ZendureMqttDeviceClient(
        "WR",
        _FakeService(),
        device_id="DEVICE_ID",
        topic_family=FAMILY_LEGACY_JSON,
        source="local_mqtt",
        product_key="PK",
        hardware_profile="solarflow_800_pro_2",
        max_power=2000,
        **kwargs,
    )


def test_no_ack_profile_has_no_acknowledgement_contract():
    dev = _zensdk_device()
    assert dev._reply_contract().supports_acknowledgement is False
    # ...but ZenSDK output is observable, so telemetry confirmation IS available.
    assert dev._confirmation_policy().telemetry_confirmation_supported is True


def test_no_ack_write_confirms_directly_from_fresh_telemetry():
    dev = _zensdk_device()
    assert dev.write_output_limit(500) is True
    rec = dev._active_command
    assert rec.state == "published"
    # No reply is ever sent (no ack contract). Fresh telemetry matching the
    # target confirms the published command directly.
    dev._service.set_snapshot(
        {"outputLimit": 500, "acMode": 2},
        last_seen_monotonic=rec.published_monotonic + 1.0,
    )
    dev.fetch()
    assert rec.state == "telemetry_confirmed"
    assert dev._active_command is None


def test_no_ack_changed_target_supersedes_in_flight_command():
    """The latest target replaces an unconfirmable in-flight command at once
    instead of waiting out its telemetry-confirmation window.
    """

    dev = _zensdk_device()
    dev.write_output_limit(500)
    old = dev._active_command
    dev.write_output_limit(300)
    assert old.state == "superseded"
    assert len(dev._service.published) == 2
    assert dev._active_command.target_w == 300
    assert dev._pending_target is None


def test_no_ack_stale_telemetry_does_not_confirm():
    dev = _zensdk_device()
    dev.write_output_limit(500)
    rec = dev._active_command
    # Retained telemetry that predates the publish must never confirm it.
    dev._service.set_snapshot(
        {"outputLimit": 500}, last_seen_monotonic=rec.published_monotonic - 5.0
    )
    dev.fetch()
    assert rec.state == "published"
    assert dev._active_command is rec


def test_no_ack_value_mismatch_does_not_confirm():
    dev = _zensdk_device()
    dev.write_output_limit(500)
    rec = dev._active_command
    dev._service.set_snapshot(
        {"outputLimit": 50}, last_seen_monotonic=rec.published_monotonic + 1.0
    )
    dev.fetch()
    assert rec.state == "published"


def test_no_ack_confirmation_timeout_is_honest_not_ack_timeout():
    dev = _zensdk_device(
        command_ack_timeout_seconds=10.0, confirmation_timeout_seconds=5.0
    )
    dev.write_output_limit(500)
    rec = dev._active_command
    published_at = rec.published_monotonic
    # Past the ack timeout but before the confirmation deadline: still published,
    # never dishonestly reported as an acknowledgement timeout.
    dev.describe(now_monotonic=published_at + 6.0)
    assert rec.state == "confirmation_timed_out"
    assert dev._active_command is None


def test_no_ack_is_not_classified_as_ack_timeout():
    dev = _zensdk_device(
        command_ack_timeout_seconds=1.0, confirmation_timeout_seconds=30.0
    )
    dev.write_output_limit(500)
    rec = dev._active_command
    # Well past the 1s ack timeout: a no-ack command must NOT be timed_out, it has
    # no acknowledgement to wait for. It stays published pending confirmation.
    dev.describe(now_monotonic=rec.published_monotonic + 5.0)
    assert rec.state == "published"


def test_no_ack_confirmation_deadline_starts_at_publish():
    dev = _zensdk_device(confirmation_timeout_seconds=7.0)
    dev.write_output_limit(500)
    rec = dev._active_command
    described = dev.describe(now_monotonic=rec.published_monotonic + 0.5)
    assert described["confirmation_deadline"] == pytest.approx(
        rec.published_monotonic + 7.0
    )


def test_no_ack_uses_policy_tolerance_not_hardcoded_default():
    # A generous policy tolerance must actually flow into confirmation: an 80 W
    # deviation is outside the 25 W default but inside a 100 W policy tolerance.
    dev = _zensdk_device(confirmation_tolerance_w=100)
    assert dev._confirmation_policy().confirmation_tolerance_w == 100
    dev.write_output_limit(500)
    rec = dev._active_command
    dev._service.set_snapshot(
        {"outputLimit": 580, "acMode": 2},
        last_seen_monotonic=rec.published_monotonic + 1.0,
    )
    dev.fetch()
    assert rec.state == "telemetry_confirmed"


def test_non_confirmable_no_ack_completes_immediately():
    # No ack contract AND no reliable telemetry confirmation: publishing must not
    # occupy the slot until a meaningless timeout — complete honestly at once.
    dev = _zensdk_device(telemetry_confirmation_supported=False)
    assert dev.write_output_limit(500) is True
    rec = dev._last_command
    assert rec.state == "completed_unconfirmed"
    assert dev._active_command is None
    # A subsequent write is free to publish immediately (slot never held).
    dev.write_output_limit(300)
    assert dev._last_command.target_w == 300


def test_absent_output_telemetry_does_not_confirm_a_stop():
    # A telemetry snapshot that does not carry outputLimit must never confirm a
    # command — a missing field must not be read as an observed 0 W (which would
    # falsely confirm a 0 W stop from telemetry that never reported the output).
    dev = _zensdk_device()
    dev.write_output_limit(0)
    rec = dev._active_command
    assert rec.target_w == 0
    dev._service.set_snapshot(
        {"electricLevel": 55}, last_seen_monotonic=rec.published_monotonic + 1.0
    )
    dev.fetch()
    assert rec.state == "published"


def test_confirming_telemetry_wins_over_elapsed_confirmation_deadline():
    # A fetch that carries confirming telemetry must confirm even if the
    # confirmation deadline has just elapsed: confirmation is attempted before the
    # timeout, so real observed output wins over a bare deadline.
    dev = _zensdk_device(confirmation_timeout_seconds=0.0)
    dev.write_output_limit(500)
    rec = dev._active_command
    dev._service.set_snapshot(
        {"outputLimit": 500, "acMode": 2},
        last_seen_monotonic=rec.published_monotonic + 0.001,
    )
    dev.fetch()
    assert rec.state == "telemetry_confirmed"


def test_ack_profile_published_command_not_confirmed_from_telemetry():
    # Correlation stays strict for profiles that DO have an ack contract: fresh
    # telemetry must not confirm a still-published (unacknowledged) command.
    dev = ZendureMqttDeviceClient(
        "WR",
        _FakeService(),
        device_id="DEVICE_ID",
        topic_family=FAMILY_LEGACY_JSON,
        source="local_mqtt",
        product_key="PK",
        hardware_profile="hyper_2000",
        max_power=2000,
    )
    assert dev._reply_contract().supports_acknowledgement is True
    dev.write_output_limit(500)
    rec = dev._active_command
    dev._service.set_snapshot(
        {"outputLimit": 500}, last_seen_monotonic=rec.published_monotonic + 1.0
    )
    dev.fetch()
    assert rec.state == "published"
