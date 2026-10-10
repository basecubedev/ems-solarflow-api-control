# SPDX-License-Identifier: AGPL-3.0-or-later
"""Effectiveness-based telemetry confirmation and foreign-writer detection."""

import logging
import math

import pytest

from ems.mqtt_control.confirmation import DEFAULT_CONFIRMATION_TIMEOUT_SECONDS
from ems.zendure_mqtt.device_client import (
    DEFAULT_COMMAND_ACK_TIMEOUT_SECONDS,
    DEFAULT_SAFETY_PREEMPT_MARGIN_W,
    MAX_COMMAND_ACK_TIMEOUT_SECONDS,
    MAX_CONFIRMATION_TIMEOUT_SECONDS,
    MAX_CONFIRMATION_TOLERANCE_W,
    MAX_SAFETY_PREEMPT_MARGIN_W,
    ZendureMqttDeviceClient,
)
from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
    pytest.mark.power_control,
]

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


@pytest.mark.parametrize("stale_key", ["acMode", "smartMode", "inputLimit"])
def test_stale_mode_metric_in_fresh_snapshot_cannot_confirm(stale_key):
    """A fresh outputLimit must not confirm a command while a matching-but-stale
    acMode/smartMode/inputLimit predates the publish: the atomic ZenSDK command
    changes all four together, so a cached mode value is not evidence it applied.
    """

    dev = _device()
    rec = _published(dev)
    now = rec.published_monotonic
    metric_times = {key: now + 1.0 for key in APPLIED}
    metric_times[stale_key] = now - 5.0
    dev._service.set_snapshot(dict(APPLIED), now + 1.0, metric_times)
    dev.fetch()
    assert rec.state == "published"


def test_all_expected_properties_fresh_and_matching_confirms():
    dev = _device()
    rec = _published(dev)
    now = rec.published_monotonic
    metric_times = {key: now + 1.0 for key in APPLIED}
    dev._service.set_snapshot(dict(APPLIED), now + 1.0, metric_times)
    dev.fetch()
    assert rec.state == "telemetry_confirmed"


def test_metric_timestamp_exactly_equal_to_publish_is_fresh():
    dev = _device()
    rec = _published(dev)
    now = rec.published_monotonic
    metric_times = {key: now for key in APPLIED}
    dev._service.set_snapshot(dict(APPLIED), now, metric_times)
    dev.fetch()
    assert rec.state == "telemetry_confirmed"


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


def test_own_timed_out_target_applied_late_is_not_external_control():
    dev, rec = _confirmed_device()
    dev._confirmation_timeout_s = 5.0
    rec2 = _published(dev, 500)
    dev.describe(now_monotonic=rec2.published_monotonic + 6.0)
    assert rec2.state == "confirmation_timed_out"
    base = rec2.published_monotonic
    dev._service.set_snapshot(dict(APPLIED, outputLimit=500), base + 10.0)
    dev.fetch()
    dev._service.set_snapshot(dict(APPLIED, outputLimit=500), base + 20.0)
    dev.fetch()
    assert dev.describe()["external_control_suspected"] is False


def test_foreign_value_after_own_timeout_is_still_detected():
    dev, rec = _confirmed_device()
    dev._confirmation_timeout_s = 5.0
    rec2 = _published(dev, 500)
    dev.describe(now_monotonic=rec2.published_monotonic + 6.0)
    base = rec2.published_monotonic
    dev._service.set_snapshot(dict(APPLIED, outputLimit=900), base + 10.0)
    dev.fetch()
    dev._service.set_snapshot(dict(APPLIED, outputLimit=900), base + 20.0)
    dev.fetch()
    assert dev.describe()["external_control_suspected"] is True


def _time_out_unconfirmed(dev, targets):
    dev._confirmation_timeout_s = 5.0
    record = None
    for target in targets:
        record = _published(dev, target)
        dev.describe(now_monotonic=record.published_monotonic + 6.0)
        assert record.state == "confirmation_timed_out"
    return record


def _report_twice(dev, output_limit, after):
    for offset in (10.0, 20.0):
        dev._service.set_snapshot(
            dict(APPLIED, outputLimit=output_limit),
            after.published_monotonic + offset,
        )
        dev.fetch()


def test_unconfirmed_own_targets_keep_only_the_last_two():
    dev, _rec = _confirmed_device()

    _time_out_unconfirmed(dev, range(400, 1400, 50))

    assert list(dev._unconfirmed_own_targets) == [1300, 1350]


def test_a_new_confirmation_forgets_the_unconfirmed_targets_before_it():
    """Excused only until a newer target is confirmed: after that the device has
    shown it holds the newer one, so an older target arriving is someone else's."""

    dev, _rec = _confirmed_device()
    _time_out_unconfirmed(dev, (500, 600))
    confirmed = _published(dev, 400)
    dev._service.set_snapshot(dict(APPLIED, outputLimit=400), confirmed.published_monotonic + 1.0)
    dev.fetch()
    assert confirmed.state == "telemetry_confirmed"

    _report_twice(dev, 600, confirmed)

    assert dev.describe()["external_control_suspected"] is True


def test_foreign_value_equal_to_an_older_own_target_is_detected():
    """Own 500, 600 and 700 W all time out unconfirmed; then the device holds
    500 W. Only the last two released targets can still be landing late, so
    500 W is evidence of another writer, not a late own command.
    """

    dev, _rec = _confirmed_device()
    last = _time_out_unconfirmed(dev, (500, 600, 700))

    _report_twice(dev, 500, last)

    assert dev.describe()["external_control_suspected"] is True


def test_own_target_before_the_latest_unconfirmed_one_is_still_ours():
    """The latest own command (600 W) never landed; the one before it (500 W)
    landed late. That is the device following this EMS, not a foreign writer.
    """

    dev, _rec = _confirmed_device()
    last = _time_out_unconfirmed(dev, (500, 600))

    _report_twice(dev, 500, last)

    assert dev.describe()["external_control_suspected"] is False


@pytest.mark.parametrize("replacement", [600, 0], ids=["superseded", "preempted"])
def test_an_own_target_that_left_the_slot_for_a_newer_one_is_still_ours(replacement):
    """On a profile without acknowledgements every changed target supersedes the
    published one, and a stop preempts it. The device may still apply the one
    that left; that is the device following this EMS, not a foreign writer.
    """

    dev, _rec = _confirmed_device()
    dev._confirmation_timeout_s = 5.0
    first = _published(dev, 500)
    second = _published(dev, replacement)
    assert first.state == "superseded"
    dev.describe(now_monotonic=second.published_monotonic + 6.0)

    for offset in (10.0, 20.0):
        dev._service.set_snapshot(dict(APPLIED, outputLimit=500), second.published_monotonic + offset)
        dev.fetch()

    assert dev.describe()["external_control_suspected"] is False


def test_a_target_sent_again_counts_again_in_the_last_two():
    """Re-sending one target is the controller's answer to a device that does
    not follow; each send is a command. Counted once, an older own target stayed
    excused for as long as the new one kept being lost."""

    dev, _rec = _confirmed_device()
    last = _time_out_unconfirmed(dev, (500, 600, 600, 600))

    _report_twice(dev, 500, last)

    assert list(dev._unconfirmed_own_targets) == [600, 600]
    assert dev.describe()["external_control_suspected"] is True


def test_a_device_that_ignores_every_stop_is_flagged():
    dev, _rec = _confirmed_device()
    dev._confirmation_timeout_s = 5.0
    _published(dev, 800)

    last = _time_out_unconfirmed(dev, (0, 0, 0))
    _report_twice(dev, 800, last)

    assert dev.describe()["external_control_suspected"] is True


def _ack_profile_device():
    dev = ZendureMqttDeviceClient(
        "WR",
        _FakeService(),
        device_id="DEV",
        topic_family=FAMILY_LEGACY_JSON,
        source="local_mqtt",
        product_key="PK",
        hardware_profile="hyper_2000",
        max_power=1200,
    )
    dev._command_ack_timeout_s = 3.0
    dev._confirmation_timeout_s = 5.0
    return dev


def _reply(dev, record, output):
    assert dev.handle_reply(
        {
            "messageId": record.message_id,
            "deviceId": "DEV",
            "function": "deviceAutomation",
            "output": output,
            "success": 1 if output == "success" else 0,
        }
    )


def _report_on(dev, value, at):
    dev._service.set_snapshot({"outputLimit": value}, at)
    dev.fetch()


def _confirmed_on_ack_profile(dev, target=300):
    record = _published(dev, target)
    _reply(dev, record, "success")
    _report_on(dev, target, record.published_monotonic + 1.0)
    assert record.state == "telemetry_confirmed", record.state


def test_an_ack_profile_remembers_both_kinds_of_timeout():
    dev = _ack_profile_device()
    _confirmed_on_ack_profile(dev)
    unanswered = _published(dev, 500)
    dev.describe(now_monotonic=unanswered.published_monotonic + 4.0)
    unconfirmed = _published(dev, 600)
    _reply(dev, unconfirmed, "success")
    dev.describe(now_monotonic=unconfirmed.published_monotonic + 20.0)

    assert list(dev._unconfirmed_own_targets) == [500, 600]
    _report_on(dev, 500, unconfirmed.published_monotonic + 30.0)
    _report_on(dev, 500, unconfirmed.published_monotonic + 40.0)
    assert dev.describe()["external_control_suspected"] is False
    _report_on(dev, 400, unconfirmed.published_monotonic + 50.0)
    _report_on(dev, 400, unconfirmed.published_monotonic + 60.0)
    assert dev.describe()["external_control_suspected"] is True


def test_an_ack_profile_remembers_a_target_a_stop_preempted():
    dev = _ack_profile_device()
    _confirmed_on_ack_profile(dev)
    rise = _published(dev, 800)
    _reply(dev, rise, "success")
    stop = _published(dev, 0)
    _reply(dev, stop, "success")
    dev.describe(now_monotonic=stop.published_monotonic + 20.0)

    assert rise.state == "superseded"
    assert list(dev._unconfirmed_own_targets) == [800, 0]
    _report_on(dev, 800, stop.published_monotonic + 30.0)
    _report_on(dev, 800, stop.published_monotonic + 40.0)
    assert dev.describe()["external_control_suspected"] is False


def test_a_target_the_device_rejected_is_not_excused():
    """The device said no; holding that value afterwards is not following this EMS."""

    dev = _ack_profile_device()
    _confirmed_on_ack_profile(dev)
    rejected = _published(dev, 500)
    _reply(dev, rejected, "error")

    _report_on(dev, 500, rejected.published_monotonic + 10.0)
    _report_on(dev, 500, rejected.published_monotonic + 20.0)

    assert 500 not in dev._unconfirmed_own_targets
    assert dev.describe()["external_control_suspected"] is True


def test_unbounded_timeout_tuning_is_capped():
    dev = _device(command_ack_timeout_seconds=1e12, confirmation_timeout_seconds=1e12)
    assert dev._command_ack_timeout_s == MAX_COMMAND_ACK_TIMEOUT_SECONDS
    assert dev._confirmation_timeout_s == MAX_CONFIRMATION_TIMEOUT_SECONDS


def test_non_finite_tuning_falls_back_to_the_default():
    dev = _device(
        command_ack_timeout_seconds=math.inf,
        confirmation_timeout_seconds=math.nan,
        confirmation_tolerance_w=math.nan,
        safety_preempt_margin_w=math.inf,
    )
    assert dev._command_ack_timeout_s == DEFAULT_COMMAND_ACK_TIMEOUT_SECONDS
    assert dev._confirmation_timeout_s == DEFAULT_CONFIRMATION_TIMEOUT_SECONDS
    assert dev._confirmation_tolerance_w is None
    assert dev._safety_preempt_margin_w == DEFAULT_SAFETY_PREEMPT_MARGIN_W


def test_oversized_tolerance_and_margin_are_capped():
    dev = _device(confirmation_tolerance_w=100000, safety_preempt_margin_w=100000)
    assert dev._confirmation_tolerance_w == MAX_CONFIRMATION_TOLERANCE_W
    assert dev._safety_preempt_margin_w == MAX_SAFETY_PREEMPT_MARGIN_W


# --- the EMS's own charge ----------------------------------------------------

CHARGING = {"outputLimit": 0, "acMode": 1, "smartMode": 1, "inputLimit": 800}


def _charging_device():
    dev = _device()
    rec = _published(dev, -800)
    dev._service.set_snapshot(dict(CHARGING), rec.published_monotonic + 1.0)
    dev.fetch()
    assert rec.state == "telemetry_confirmed"
    return dev, rec


def test_a_steady_charge_of_its_own_is_not_external_control():
    """A charging device reports outputLimit 0, never the negative target.

    Comparing that 0 with the confirmed -800 W read every steady charge as a
    foreign writer two reports in, and the flag stayed raised.
    """

    dev, rec = _charging_device()
    base = rec.published_monotonic
    dev._service.set_snapshot(dict(CHARGING), base + 10.0)
    dev.fetch()
    dev._service.set_snapshot(dict(CHARGING), base + 20.0)
    dev.fetch()

    assert dev.describe()["external_control_suspected"] is False


@pytest.mark.parametrize(
    "foreign",
    [
        {"inputLimit": 300},
        {"outputLimit": 600, "acMode": 2, "inputLimit": 0},
    ],
)
def test_a_foreign_change_to_an_own_charge_is_still_detected(foreign):
    dev, rec = _charging_device()
    base = rec.published_monotonic
    dev._service.set_snapshot(dict(CHARGING, **foreign), base + 10.0)
    dev.fetch()
    dev._service.set_snapshot(dict(CHARGING, **foreign), base + 20.0)
    dev.fetch()

    assert dev.describe()["external_control_suspected"] is True
