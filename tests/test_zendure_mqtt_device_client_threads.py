# SPDX-License-Identifier: AGPL-3.0-or-later
"""One MQTT device client, three threads, one command state at a time.

The control loop dispatches and describes, the fetch executor fetches and the
MQTT network thread delivers device replies to the same
``ZendureMqttDeviceClient``. A reply that frees the single command slot
publishes the queued target from the network thread, so these tests hold that
thread inside its publish and run the control thread against it.

No sleeps: the network thread is parked on an Event inside the broker double,
and the control thread's progress is observed either through its own completion
or through the device client's lock reporting that it has to wait.
"""

import json
import threading

import pytest

from ems.mqtt_control.dispatch import WriteDispatchStatus
from ems.zendure_mqtt.control import PublishSubmission
from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient
from ems.zendure_mqtt.service import classify_snapshot
from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
    pytest.mark.power_control,
]

_WAIT_S = 10.0


class _HeldPublishService:
    """Broker service double whose publish from one chosen thread waits."""

    def __init__(self):
        self.connected = True
        self.published = []
        self.held_thread = None
        self.publish_held = threading.Event()
        self.release_publish = threading.Event()

    def snapshot_status(self, device_id, *, now_monotonic=None):
        return classify_snapshot(None, 60.0, now_monotonic=0.0)

    def publish_message(self, message):
        if threading.current_thread() is self.held_thread:
            self.publish_held.set()
            if not self.release_publish.wait(_WAIT_S):
                raise AssertionError("held publish was never released")
        payload = json.loads(message.payload)
        self.published.append(payload["arguments"][0]["autoModelValue"]["outPower"])
        return PublishSubmission(True)


class _ObservedLock:
    """Reentrant lock that reports when a second thread has to wait for it."""

    def __init__(self, on_wait):
        self._lock = threading.RLock()
        self._on_wait = on_wait

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            self._on_wait()
            self._lock.acquire()
        return self

    def __exit__(self, *_exc):
        self._lock.release()
        return False


def _device(service):
    return ZendureMqttDeviceClient(
        "WR",
        service,
        device_id="DEVICE_ID",
        topic_family=FAMILY_LEGACY_JSON,
        source="local_mqtt",
        product_key="PK",
        hardware_profile="hyper_2000",
        max_power=2000,
    )


def _rejection(record):
    return {
        "messageId": record.message_id,
        "deviceId": record.device_id,
        "function": "deviceAutomation",
        "output": "error",
        "success": 0,
    }


def _queued_behind_rejected_command(service):
    """An 800 W command in flight, 700 W queued, and a rejection reply ready."""

    dev = _device(service)
    assert dev.dispatch_output_limit(800).status is WriteDispatchStatus.PUBLISHED
    in_flight = dev._active_command
    assert dev.dispatch_output_limit(700).status is WriteDispatchStatus.QUEUED_LATEST
    return dev, _rejection(in_flight)


def _run_against_held_flush(dev, service, reply, control_call):
    """Run ``control_call`` while the network thread is inside its flush publish.

    Returns once the control call has either finished or is waiting for the
    device client's lock; then the network thread is released and both joined.
    """

    progressed = threading.Event()
    dev._lock = _ObservedLock(progressed.set)
    results = []

    def network_thread():
        dev.handle_reply(reply)

    def control_thread():
        results.append(control_call())
        progressed.set()

    network = threading.Thread(target=network_thread, name="mqtt-network")
    service.held_thread = network
    network.start()
    assert service.publish_held.wait(_WAIT_S)
    control = threading.Thread(target=control_thread, name="control-loop")
    control.start()
    assert progressed.wait(_WAIT_S)
    service.release_publish.set()
    network.join(_WAIT_S)
    control.join(_WAIT_S)
    assert not network.is_alive() and not control.is_alive()
    return results[0]


def test_stop_dispatched_during_a_reply_flush_reaches_the_broker_last():
    """Interleaving: a rejection reply freed the slot and the network thread is
    publishing the queued 700 W target when the control thread dispatches a
    0 W stop. The stop must follow the 700 W command to the broker and own the
    active slot; the reverse order leaves the device on the older target while
    the stop's record is lost.
    """

    service = _HeldPublishService()
    dev, reply = _queued_behind_rejected_command(service)

    result = _run_against_held_flush(
        dev, service, reply, lambda: dev.dispatch_output_limit(0)
    )

    assert result.status is WriteDispatchStatus.PUBLISHED
    assert service.published == [800, 700, 0]
    assert dev._active_command.target_w == 0
    assert dev._last_command.target_w == 0


def test_describe_during_a_reply_flush_reports_the_flushed_command():
    """Interleaving: the control thread describes the device while the network
    thread is publishing the queued 700 W target. The status must show the
    700 W command in the slot, not a moment where it is neither pending nor
    active.
    """

    service = _HeldPublishService()
    dev, reply = _queued_behind_rejected_command(service)

    described = _run_against_held_flush(dev, service, reply, dev.describe)

    assert described["pending_target"] is None
    assert described["active_command"]["target_power_w"] == 700
    assert service.published == [800, 700]


class _CountingLock:
    """Reentrant lock that counts how often it is taken."""

    def __init__(self):
        self._lock = threading.RLock()
        self.taken = 0

    def __enter__(self):
        self._lock.acquire()
        self.taken += 1
        return self

    def __exit__(self, *_exc):
        self._lock.release()
        return False


@pytest.mark.parametrize(
    "call",
    [
        lambda dev: dev.fetch(),
        lambda dev: dev.set_dispatch_observer(None),
        lambda dev: dev.cancel_pending_output_limit("test"),
        lambda dev: dev.dispatch_output_limit(500),
        lambda dev: dev.write_properties({"minSoc": 100}, reason="test"),
        lambda dev: dev.handle_reply(b"{}"),
        lambda dev: dev.describe(),
    ],
    ids=["fetch", "observer", "cancel", "dispatch", "write", "reply", "describe"],
)
def test_every_entry_to_the_command_state_takes_the_lock(call):
    dev = _device(_HeldPublishService())
    dev._lock = _CountingLock()

    call(dev)

    assert dev._lock.taken >= 1


def test_the_command_state_lock_can_be_taken_again_by_its_holder():
    """The dispatch observer is called with the lock held, so an observer that
    calls back into the client must find the lock reentrant."""

    dev = _device(_HeldPublishService())

    assert type(dev._lock) is type(threading.RLock())
