# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 2: safety preemption and structured write-dispatch results.

A safety-relevant lower target must not wait behind an in-flight command for the
full command timeout. A full stop (0 W) always preempts; a substantial downward
reduction (>= the safety margin) preempts too. Preemption retires the in-flight
command as terminal ``superseded`` and publishes the safer target immediately, so
a late reply or late telemetry for the retired command can never confirm the
replacement. Correlation for acknowledged profiles is never weakened.

``dispatch_output_limit`` returns a structured :class:`WriteDispatchResult` that
distinguishes published / coalesced / queued / rejected — the boolean
``write_output_limit`` wrapper stays compatible.
"""

import pytest

from ems.mqtt_control.dispatch import WriteDispatchStatus
from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient
from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.power_control,
    pytest.mark.unit,
    pytest.mark.simulation,
]


class _FakeSnapshot:
    def __init__(self, metrics, last_seen_monotonic):
        self.metrics = metrics
        self.last_seen_monotonic = last_seen_monotonic
        self.metric_monotonic = {
            key: last_seen_monotonic for key in metrics
        }


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
    """No-ack ZenSDK device (worst case: holds the slot until confirmation)."""

    return ZendureMqttDeviceClient(
        "WR",
        _FakeService(),
        device_id="DEVICE_ID",
        topic_family=FAMILY_LEGACY_JSON,
        source="local_mqtt",
        product_key="PK",
        hardware_profile="solarflow_800_pro_2",
        max_power=2000,
        confirmation_timeout_seconds=30.0,
        **kwargs,
    )


def _ack_device(**kwargs):
    return ZendureMqttDeviceClient(
        "WR",
        _FakeService(),
        device_id="DEVICE_ID",
        topic_family=FAMILY_LEGACY_JSON,
        source="local_mqtt",
        product_key="PK",
        hardware_profile="hyper_2000",
        max_power=2000,
        **kwargs,
    )


def _reply(record, *, success=1, output="success"):
    return {
        "messageId": record.message_id,
        "deviceId": record.device_id,
        "function": "deviceAutomation",
        "output": output,
        "success": success,
    }


# --- safety preemption -------------------------------------------------------


def test_zero_target_preempts_in_flight_no_ack_command_immediately():
    dev = _zensdk_device()
    dev.write_output_limit(600)
    old = dev._active_command
    assert old.target_w == 600
    # A 0 W safety stop must publish within this cycle, not wait for the old
    # no-ack command's 30 s confirmation deadline.
    result = dev.dispatch_output_limit(0)
    assert result.status is WriteDispatchStatus.PUBLISHED
    assert len(dev._service.published) == 2
    assert dev._active_command.target_w == 0
    assert old.state == "superseded"
    assert old.is_terminal


def test_substantial_reduction_preempts():
    dev = _zensdk_device()
    dev.write_output_limit(600)
    old = dev._active_command
    # 600 -> 100 is a 500 W reduction (>= 300 W margin): preempt and publish now.
    result = dev.dispatch_output_limit(100)
    assert result.status is WriteDispatchStatus.PUBLISHED
    assert len(dev._service.published) == 2
    assert dev._active_command.target_w == 100
    assert old.state == "superseded"


def test_no_ack_changed_target_supersedes_and_publishes_now():
    """A no-ack command settles only via slow telemetry, so the latest target
    replaces it immediately instead of stalling behind the confirmation window.
    """

    dev = _zensdk_device()
    dev.write_output_limit(600)
    old = dev._active_command
    result = dev.dispatch_output_limit(500)
    assert result.status is WriteDispatchStatus.PUBLISHED
    assert len(dev._service.published) == 2
    assert dev._active_command.target_w == 500
    assert old.state == "superseded"


def test_no_ack_increase_supersedes_and_publishes_now():
    dev = _zensdk_device()
    dev.write_output_limit(600)
    result = dev.dispatch_output_limit(900)
    assert result.status is WriteDispatchStatus.PUBLISHED
    assert len(dev._service.published) == 2
    assert dev._active_command.target_w == 900


def test_ack_profile_small_reduction_queues_not_preempts():
    dev = _ack_device()
    dev.write_output_limit(600)
    result = dev.dispatch_output_limit(500)
    assert result.status is WriteDispatchStatus.QUEUED_LATEST
    assert len(dev._service.published) == 1
    assert dev._pending_target == 500


def test_ack_profile_increase_never_preempts():
    dev = _ack_device()
    dev.write_output_limit(600)
    result = dev.dispatch_output_limit(900)
    assert result.status is WriteDispatchStatus.QUEUED_LATEST
    assert len(dev._service.published) == 1
    assert dev._pending_target == 900


def test_superseded_target_telemetry_cannot_confirm_replacement():
    dev = _zensdk_device()
    dev.write_output_limit(600)
    old = dev._active_command
    dev.dispatch_output_limit(0)
    new = dev._active_command
    assert new.target_w == 0
    # Late telemetry reflecting the OLD (600 W) target must not confirm the new
    # 0 W command; only telemetry matching the new target may.
    dev._service.set_snapshot(
        {"outputLimit": 600}, last_seen_monotonic=new.published_monotonic + 1.0
    )
    dev.fetch()
    assert new.state == "published"
    assert old.state == "superseded"
    dev._service.set_snapshot(
        {"outputLimit": 0, "acMode": 2, "smartMode": 1, "inputLimit": 0},
        last_seen_monotonic=new.published_monotonic + 2.0,
    )
    dev.fetch()
    assert new.state == "telemetry_confirmed"


def test_preemption_supersedes_ack_command_without_weakening_correlation():
    dev = _ack_device()
    dev.write_output_limit(600)
    old = dev._active_command
    # A 0 W stop preempts even an ack profile's in-flight command.
    dev.dispatch_output_limit(0)
    new = dev._active_command
    assert new is not old
    assert new.target_w == 0
    assert old.state == "superseded"
    # A late reply for the OLD command must never acknowledge the NEW command.
    assert dev.handle_reply(_reply(old)) is False
    assert new.state == "published"


def test_zero_stop_preempts_an_active_charge_command():
    # "A full stop always preempts" must hold for an active charge (negative)
    # command too, not only an active discharge.
    dev = _ack_device()  # hyper_2000 supports charge
    dev.write_output_limit(-500)
    old = dev._active_command
    assert old.target_w == -500
    result = dev.dispatch_output_limit(0)
    assert result.status is WriteDispatchStatus.PUBLISHED
    assert old.state == "superseded"
    assert dev._active_command.target_w == 0


def test_configurable_preempt_margin():
    dev = _zensdk_device(safety_preempt_margin_w=50)
    dev.write_output_limit(600)
    # With a 50 W margin, a 100 W reduction now preempts.
    result = dev.dispatch_output_limit(500)
    assert result.status is WriteDispatchStatus.PUBLISHED
    assert dev._active_command.target_w == 500


def test_ack_profile_sub_tolerance_reduction_queues_zero_stop_preempts():
    dev = _ack_device(safety_preempt_margin_w=5, confirmation_tolerance_w=25)
    dev.write_output_limit(600)
    result = dev.dispatch_output_limit(585)
    assert result.status is WriteDispatchStatus.QUEUED_LATEST
    assert dev._pending_target == 585
    result2 = dev.dispatch_output_limit(0)
    assert result2.status is WriteDispatchStatus.PUBLISHED


# --- structured dispatch result ----------------------------------------------


def test_dispatch_published_result_carries_correlation():
    dev = _zensdk_device()
    result = dev.dispatch_output_limit(600)
    assert result.status is WriteDispatchStatus.PUBLISHED
    assert result.target_w == 600
    assert result.message_id == dev._active_command.message_id
    assert result.command_state == "published"
    assert bool(result) is True


def test_dispatch_coalesced_result():
    dev = _zensdk_device()
    dev.write_output_limit(600)
    result = dev.dispatch_output_limit(600)
    assert result.status is WriteDispatchStatus.COALESCED_ACTIVE
    assert len(dev._service.published) == 1
    assert bool(result) is True


def test_dispatch_queued_result():
    dev = _ack_device()
    dev.write_output_limit(600)
    result = dev.dispatch_output_limit(500)
    assert result.status is WriteDispatchStatus.QUEUED_LATEST
    assert result.target_w == 500
    assert bool(result) is True


def test_dispatch_rejected_result_is_falsey():
    dev = _zensdk_device()
    # ZenSDK does not support charge; a negative target is rejected.
    result = dev.dispatch_output_limit(-500)
    assert result.status is WriteDispatchStatus.REJECTED
    assert result.reason
    assert bool(result) is False


def test_write_output_limit_wrapper_stays_boolean():
    dev = _zensdk_device()
    assert dev.write_output_limit(600) is True
    assert dev.write_output_limit(-500) is False
