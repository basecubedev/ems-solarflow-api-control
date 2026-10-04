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


def _zensdk_device(hardware_profile="solarflow_800_pro_2", **kwargs):
    """No-ack ZenSDK device (worst case: holds the slot until confirmation)."""

    return ZendureMqttDeviceClient(
        "WR",
        _FakeService(),
        device_id="DEVICE_ID",
        topic_family=FAMILY_LEGACY_JSON,
        source="local_mqtt",
        product_key="PK",
        hardware_profile=hardware_profile,
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
    # A model whose AC charge path has not been measured rejects a charge.
    dev = _zensdk_device("solarflow_800")
    result = dev.dispatch_output_limit(-500)
    assert result.status is WriteDispatchStatus.REJECTED
    assert result.reason
    assert bool(result) is False


def test_write_output_limit_wrapper_stays_boolean():
    dev = _zensdk_device("solarflow_800")
    assert dev.write_output_limit(600) is True
    assert dev.write_output_limit(-500) is False


# --- direction changes -------------------------------------------------------


def test_leaving_a_charge_preempts_the_in_flight_charge():
    """The way back may not wait behind the charge it ends.

    On an ack profile only a 0 W target preempted, so an exit to the standby
    floor queued behind the in-flight charge: the device drew for another
    thirty seconds, until the charge command timed out.
    """

    dev = _ack_device()
    dev.write_output_limit(-600)
    old = dev._active_command

    result = dev.dispatch_output_limit(35)

    assert result.status is WriteDispatchStatus.PUBLISHED
    assert old.state == "superseded"
    assert dev._active_command.target_w == 35


def test_entering_a_charge_preempts_an_in_flight_discharge():
    dev = _ack_device()
    dev.write_output_limit(600)

    result = dev.dispatch_output_limit(-500)

    assert result.status is WriteDispatchStatus.PUBLISHED
    assert dev._active_command.target_w == -500


def test_a_power_change_inside_the_charge_still_waits():
    """Only a direction change jumps the queue; anything else would spam."""

    dev = _ack_device()
    dev.write_output_limit(-600)

    result = dev.dispatch_output_limit(-400)

    assert result.status is WriteDispatchStatus.QUEUED_LATEST
    assert dev._pending_target == -400


# --- a charge on an invoke profile is confirmable ----------------------------


def _acknowledged_charge(watts=600):
    dev = _ack_device()
    dev.write_output_limit(-watts)
    record = dev._active_command
    dev.handle_reply(_reply(record))
    assert record.state == "acknowledged"
    return dev, record


def _report(dev, record, metrics):
    dev._service.set_snapshot(
        metrics, last_seen_monotonic=record.published_monotonic + 1.0
    )
    dev.fetch()


@pytest.mark.parametrize(
    "metrics",
    [
        {"outputLimit": 0, "acMode": 1, "inputLimit": 600},
        {"outputLimit": 0, "acMode": 1, "gridInputPower": 590},
    ],
)
def test_an_invoke_charge_confirms_from_what_the_device_reports_charging(metrics):
    """A charging Hyper reports outputLimit 0, never the negative target.

    Confirming against outputLimit therefore never succeeded, and every charge
    command ended in confirmation_timed_out. The charge proves itself in the
    AC-input direction and its power: the limit where the device reports one,
    else the AC input it measures.
    """

    dev, record = _acknowledged_charge()

    _report(dev, record, metrics)

    assert record.state == "telemetry_confirmed"


@pytest.mark.parametrize(
    "metrics",
    [
        {"outputLimit": 0},
        {"outputLimit": 0, "acMode": 1},
        {"outputLimit": 0, "acMode": 2, "gridInputPower": 0},
        {"outputLimit": 0, "acMode": 1, "gridInputPower": 0},
    ],
)
def test_an_invoke_charge_is_not_confirmed_without_a_charge(metrics):
    dev, record = _acknowledged_charge()

    _report(dev, record, metrics)

    assert record.state == "acknowledged"


# --- what the client last put on the wire ------------------------------------


def test_the_client_remembers_a_charge_it_published_until_the_device_leaves_it():
    """The controller's record of its own charge, the one a reset cannot erase.

    A queued target was not published and changes nothing. Neither does the
    way back going out: the broker accepting it says nothing about the device.
    Only a report that shows the device out of the charge ends the record.
    """

    dev = _ack_device()
    assert dev.charge_commanded is False

    dev.write_output_limit(-600)
    assert dev.charge_commanded is True

    dev.dispatch_output_limit(-400)
    assert dev._pending_target == -400
    assert dev.charge_commanded is True

    exit_command = dev.dispatch_output_limit(35)
    assert exit_command.published
    assert dev.charge_commanded is True

    record = dev._last_command
    _report(dev, record, {"outputLimit": 0, "acMode": 1, "inputLimit": 600})
    assert dev.charge_commanded is True

    _report(dev, record, {"outputLimit": 35, "acMode": 2, "inputLimit": 0})
    assert dev.charge_commanded is False


class _Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


def test_an_exit_the_device_rejected_keeps_the_charge_and_is_asked_again_later():
    """A rejected exit leaves the device charging, and the EMS knowing it.

    The record ended when the exit was published, so a Hyper that rejected it
    went on drawing from the grid with the EMS's record clear. A rejection also
    frees the command slot at once, so the exit is held to the resend window
    rather than published again every cycle.
    """

    from unittest.mock import patch

    clock = _Clock()
    with patch("time.monotonic", clock):
        dev = _ack_device()
        dev.write_output_limit(-600)
        dev.handle_reply(_reply(dev._active_command))

        dev.dispatch_output_limit(0)
        exit_record = dev._active_command
        dev.handle_reply(_reply(exit_record, success=0, output="failed"))
        assert exit_record.state == "rejected"
        assert dev.charge_commanded is True
        published = len(dev._service.published)

        clock.now += 10
        assert not dev.dispatch_output_limit(0).published
        assert dev.charge_exit_due() is False
        assert len(dev._service.published) == published

        clock.now += 20
        assert dev.charge_exit_due() is True
        assert dev.dispatch_output_limit(0).published
        assert len(dev._service.published) == published + 1
