# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic tests for the MQTT hardware control probe (no hardware/network).

The write/observe loop is driven by an injected clock and a simulated inverter so
the measured latency is exact and reproducible. The fake device mirrors the
production device-client surface the probe uses (``_build_write`` returning a
prepared message with QoS/retain, ``_publish_message``, ``write_properties``).
"""

import importlib.util
import json
import os
from types import SimpleNamespace

import pytest

_PROBE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "scripts",
    "mqtt_write_latency_probe.py",
)
_spec = importlib.util.spec_from_file_location("mqtt_write_latency_probe", _PROBE_PATH)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

_UNSET = object()


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


class SimInverter:
    """A device whose HTTP-visible state lands ``latency`` seconds after a write."""

    def __init__(self, clock, latency, initial, state=None):
        self.clock = clock
        self.latency = latency
        self.state = {
            "smartMode": 1,
            "acMode": 2,
            "outputLimit": initial,
            "inputLimit": 0,
            "outputHomePower": 0,
            "soc": 60.0,
            "minSoc": 15.0,
            "solarInputPower": 0.0,
            "gridState": 0,
        }
        if state:
            self.state.update(state)
        self._pending = None
        self._land_at = None

    def command(self, properties):
        self._pending = dict(properties)
        self._land_at = self.clock.now() + self.latency

    def read(self):
        if self._pending is not None and self.clock.now() >= self._land_at:
            self.state.update(self._pending)
            self._pending = None
        return dict(self.state)


class FakeMqttDevice:
    name = "inv-mqtt"
    sn = "SN123"
    physical_serial = "SN123"
    source = "zendure_cloud_mqtt"
    control_gate = "mqtt_zendure"
    broker_ref = "zendure_cloud"
    hardware_profile = "solarflow_800_pro_2"
    max_power = 800

    def __init__(self, sim):
        self.sim = sim
        self.property_writes = []
        self.published_messages = []
        self.publish_accepted = True

    def _build_write(self, target):
        properties = {"smartMode": 1, "acMode": 2, "outputLimit": target, "inputLimit": 0}
        payload = json.dumps({"properties": properties}).encode()
        message = SimpleNamespace(
            topic=f"iot/PK/{self.sn}/properties/write",
            payload=payload,
            qos=1,
            retain=False,
        )
        return message, 1, "discharge", dict(properties)

    def _publish_message(self, message):
        self.published_messages.append(message)
        if self.publish_accepted:
            self.sim.command(json.loads(message.payload)["properties"])
        return SimpleNamespace(accepted=self.publish_accepted, mid=1)

    def write_properties(self, properties, *, reason, **_kwargs):
        self.property_writes.append((dict(properties), reason))
        self.sim.command(dict(properties))
        return True

    def check_property_writes(self, properties):
        # All captured properties are restorable in the default fake.
        return {}

    def describe(self):
        return {
            "power_write_profile": "zensdk_properties_write",
            "effective_write_topic": f"iot/PK/{self.sn}/properties/write",
            "effective_write_topic_source": "canonical_profile",
            "write_topic_obsolete": False,
        }


class FakeReader:
    ip = "192.168.1.50"
    name = "inv-http"
    sn = "SN123"

    def __init__(self, sim):
        self.sim = sim

    def fetch(self):
        state = self.sim.read()
        return SimpleNamespace(
            smart_mode=state["smartMode"],
            ac_mode=state["acMode"],
            output_limit=state["outputLimit"],
            input_limit_w=state["inputLimit"],
            output=state["outputHomePower"],
            soc=state["soc"],
            min_soc=state["minSoc"],
            solar=state["solarInputPower"],
            grid_state=state["gridState"],
        )


def _opts(**overrides):
    base = dict(
        samples=4,
        poll_interval=0.25,
        timeout=20.0,
        settle=1.0,
        match_tolerance=5,
        connect_timeout=15.0,
        contention_check=True,
        verify_output=False,
        output_timeout=5.0,
        output_tolerance=50,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_pick_target_alternates_to_largest_delta():
    assert probe.pick_target(200, [200, 500]) == 500
    assert probe.pick_target(500, [200, 500]) == 200
    assert probe.pick_target(None, [200, 500]) == 200


def test_moved_respects_tolerance():
    assert probe.moved(500, 200, 5) is True
    assert probe.moved(203, 200, 5) is False
    assert probe.moved(None, 200, 5) is False


def test_percentile_interpolates():
    assert probe.percentile([], 0.5) is None
    assert probe.percentile([10], 0.9) == 10
    assert probe.percentile([0, 100], 0.5) == 50
    assert probe.percentile([0, 10, 20, 30], 0.5) == 15


def test_run_probe_measures_latency_at_poll_resolution():
    clock = FakeClock()
    sim = SimInverter(clock, latency=0.4, initial=200)
    dev = FakeMqttDevice(sim)
    reader = FakeReader(sim)

    samples = probe.run_probe(
        dev, reader, [200, 500], _opts(samples=3),
        sleep=clock.sleep, now=clock.now,
    )

    assert len(samples) == 3
    assert all(s.locally_accepted and not s.timed_out for s in samples)
    for s in samples:
        assert s.matched_target
        assert s.mode_ok is True
        assert s.setpoint_match_from_submit_ms == pytest.approx(500.0)

    stats = probe.summarize(samples, _opts(samples=3))
    assert stats["matched"] == 3
    assert stats["landed"] == 3
    assert stats["movement_only"] == 0
    assert stats["setpoint_match_from_submit_p50_ms"] == pytest.approx(500.0)
    assert stats["poll_resolution_ms"] == pytest.approx(250.0)


def test_run_probe_emits_progress_lines():
    clock = FakeClock()
    sim = SimInverter(clock, latency=0.4, initial=200)
    dev = FakeMqttDevice(sim)
    reader = FakeReader(sim)
    lines = []
    probe.run_probe(
        dev, reader, [200, 500], _opts(samples=1),
        sleep=clock.sleep, now=clock.now, progress=lines.append,
    )
    assert any("submitted 500W" in line for line in lines)
    assert any("matched target" in line for line in lines)


def test_run_probe_times_out_when_value_never_lands():
    clock = FakeClock()
    sim = SimInverter(clock, latency=999.0, initial=200)
    dev = FakeMqttDevice(sim)
    reader = FakeReader(sim)

    samples = probe.run_probe(
        dev, reader, [200, 500], _opts(samples=1, timeout=1.0),
        sleep=clock.sleep, now=clock.now,
    )

    assert len(samples) == 1
    assert samples[0].timed_out is True
    assert samples[0].matched_target is False
    assert samples[0].setpoint_match_from_submit_ms is None


def test_run_probe_intermediate_movement_is_not_a_match():
    """Movement toward the target (200 -> 250, target 500) is not setpoint landing.

    The sample must not be counted as matched/landed, must record movement, and
    must not contribute to the latency percentiles.
    """

    clock = FakeClock()

    class SteppedInverter(SimInverter):
        def read(self):
            # The device only ever moves partway to the target, never matching it.
            if self._pending is not None and self.clock.now() >= self._land_at:
                stepped = dict(self._pending)
                stepped["outputLimit"] = 250
                self.state.update(stepped)
                self._pending = None
            return dict(self.state)

    sim = SteppedInverter(clock, latency=0.4, initial=200)
    dev = FakeMqttDevice(sim)
    reader = FakeReader(sim)
    samples = probe.run_probe(
        dev, reader, [200, 500], _opts(samples=1, timeout=2.0),
        sleep=clock.sleep, now=clock.now,
    )
    assert samples[0].matched_target is False
    assert samples[0].timed_out is True
    assert samples[0].movement_observed is True
    assert samples[0].last_observed_output_limit == 250
    assert samples[0].setpoint_match_from_submit_ms is None

    stats = probe.summarize(samples, _opts(samples=1))
    assert stats["matched"] == 0
    assert stats["landed"] == 0
    assert stats["movement_only"] == 1
    assert stats["setpoint_match_from_submit_p50_ms"] is None


def test_run_probe_aborts_on_foreign_writer():
    clock = FakeClock()
    sim = SimInverter(clock, latency=0.4, initial=200)
    dev = FakeMqttDevice(sim)

    class DriftingReader(FakeReader):
        """After the first sample lands (500 W), later reads report a foreign
        value so the next sample's baseline check must abort."""

        def __init__(self, sim):
            super().__init__(sim)
            self.landed_reads = 0

        def fetch(self):
            state = super().fetch()
            if state.output_limit == 500 and self.sim._pending is None:
                self.landed_reads += 1
                if self.landed_reads > 2:
                    state.output_limit = 137
            return state

    reader = DriftingReader(sim)
    with pytest.raises(RuntimeError, match="foreign writer"):
        probe.run_probe(
            dev, reader, [200, 500], _opts(samples=4),
            sleep=clock.sleep, now=clock.now,
        )


def test_probe_refuses_a_retained_command():
    clock = FakeClock()
    sim = SimInverter(clock, latency=0.1, initial=200)
    dev = FakeMqttDevice(sim)

    def retained_build(target):
        message, mid, op, expected = FakeMqttDevice._build_write(dev, target)
        message.retain = True
        return message, mid, op, expected

    dev._build_write = retained_build
    with pytest.raises(RuntimeError, match="retained"):
        probe.publish_target(dev, 500)


# --- physical-effect classification ------------------------------------------


def test_classify_physical_distinguishes_reaction_and_conditions():
    reacted = {"outputHomePower": 480, "soc": 60.0, "minSoc": 15.0}
    blocked = {"outputHomePower": 0, "soc": 15.0, "minSoc": 15.0}
    silent = {"outputHomePower": 0, "soc": 60.0, "minSoc": 15.0}
    assert probe.classify_physical(reacted, 500, 50) == "output_reacted"
    assert probe.classify_physical(blocked, 500, 50) == "no_output_possible_soc_at_minimum"
    assert probe.classify_physical(silent, 500, 50) == "not_reacted"
    assert probe.classify_physical({"outputHomePower": 20, "soc": 60.0, "minSoc": 15.0}, 0, 50) == "output_reacted"
    assert probe.classify_physical(None, 500, 50) == "unknown"


def test_verify_output_reports_reaction_when_it_lands():
    clock = FakeClock()
    sim = _TimelineInverter(
        clock, setpoint_at=0.1, physical_at=0.2, initial=200
    )
    dev = FakeMqttDevice(sim)
    reader = FakeReader(sim)
    samples = probe.run_probe(
        dev, reader, [200, 500], _opts(samples=1, verify_output=True),
        sleep=clock.sleep, now=clock.now,
    )
    assert samples[0].physical == "output_reacted"


# --- mode recovery test ------------------------------------------------------


def test_mode_test_not_applicable_in_output_mode():
    clock = FakeClock()
    sim = SimInverter(clock, latency=0.1, initial=200)
    dev = FakeMqttDevice(sim)
    reader = FakeReader(sim)
    result = probe.run_mode_recovery_test(
        dev, reader, 200, _opts(), sleep=clock.sleep, now=clock.now
    )
    assert result["result"] == "not_applicable"


def test_mode_test_lands_mode_and_setpoint_from_inactive_state():
    clock = FakeClock()
    sim = SimInverter(
        clock, latency=0.4, initial=0,
        state={"smartMode": 0, "acMode": 1, "outputLimit": 0},
    )
    dev = FakeMqttDevice(sim)
    reader = FakeReader(sim)
    result = probe.run_mode_recovery_test(
        dev, reader, 200, _opts(), sleep=clock.sleep, now=clock.now
    )
    assert result["result"] == "mode_and_setpoint_verified"
    assert sim.state["acMode"] == 2
    assert sim.state["outputLimit"] == 200


def test_mode_test_smart_mode_mismatch_is_not_full_success():
    """The atomic setpoint lands but smartMode stays 0: the mode test must NOT
    report full success — every expected property is verified exactly.
    """

    clock = FakeClock()

    class NoSmartModeInverter(SimInverter):
        def command(self, properties):
            props = dict(properties)
            props["smartMode"] = 0  # device never leaves standby smartMode
            super().command(props)

    sim = NoSmartModeInverter(
        clock, latency=0.4, initial=0,
        state={"smartMode": 0, "acMode": 1, "outputLimit": 0},
    )
    dev = FakeMqttDevice(sim)
    reader = FakeReader(sim)
    result = probe.run_mode_recovery_test(
        dev, reader, 200, _opts(), sleep=clock.sleep, now=clock.now
    )
    assert result["result"] == "mode_mismatch"


def test_mode_test_incomplete_when_property_not_reported():
    clock = FakeClock()
    sim = SimInverter(
        clock, latency=0.4, initial=0,
        state={"smartMode": 0, "acMode": 1, "outputLimit": 0},
    )
    dev = FakeMqttDevice(sim)

    class PartialReader(FakeReader):
        def fetch(self):
            state = super().fetch()
            state.smart_mode = None  # HTTP report omits smartMode
            return state

    reader = PartialReader(sim)
    result = probe.run_mode_recovery_test(
        dev, reader, 200, _opts(), sleep=clock.sleep, now=clock.now
    )
    assert result["result"] == "setpoint_verified_mode_incomplete"


# --- full-state restore ------------------------------------------------------


def test_restore_writes_and_verifies_the_complete_initial_state():
    clock = FakeClock()
    sim = SimInverter(clock, latency=0.2, initial=500)
    dev = FakeMqttDevice(sim)
    reader = FakeReader(sim)
    initial = {"smartMode": 0, "acMode": 1, "outputLimit": 0, "inputLimit": 0}
    contracts = probe.build_operation_contracts(dev, [200, 500])
    report = probe.restore_initial_state(
        dev, reader, initial, contracts, _opts(), sleep=clock.sleep, now=clock.now
    )
    assert report["restored"] is True
    assert report["restore_verified"] is True
    assert report["restore_partial"] is False
    assert dev.property_writes == [(initial, "probe_restore")]
    assert sim.state["smartMode"] == 0
    assert sim.state["acMode"] == 1


@pytest.mark.parametrize("missing", ["smartMode", "acMode", "inputLimit"])
def test_preflight_requires_every_property_modified_by_the_exact_command(missing):
    clock = FakeClock()
    sim = SimInverter(clock, latency=0.2, initial=500)
    dev = FakeMqttDevice(sim)
    contracts = probe.build_operation_contracts(dev, [200, 500])
    initial = {
        "smartMode": 1,
        "acMode": 2,
        "outputLimit": 500,
        "inputLimit": 0,
    }
    del initial[missing]

    issues = probe.preflight_restorable(dev, initial, contracts)

    assert issues == {missing: "initial_value_missing"}
    assert dev.published_messages == []


def test_preflight_contract_comes_from_the_real_zensdk_production_builder():
    from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient
    from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON

    dev = ZendureMqttDeviceClient(
        "INV",
        SimpleNamespace(),
        device_id="ROUTE-ID",
        topic_family=FAMILY_LEGACY_JSON,
        source="zendure_cloud_mqtt",
        broker_ref="cloud",
        product_key="PRODUCT-KEY",
        hardware_profile="solarflow_800_pro_2",
        max_power=800,
    )

    contracts = probe.build_operation_contracts(dev, [0, 500])

    assert probe.required_restore_properties(contracts) == (
        "acMode",
        "inputLimit",
        "outputLimit",
        "smartMode",
    )
    assert contracts[0].modified_properties == {
        "smartMode": 1,
        "acMode": 2,
        "outputLimit": 0,
        "inputLimit": 0,
    }
    assert contracts[1].modified_properties["outputLimit"] == 500


def test_rejected_local_submission_does_not_trigger_restore():
    clock = FakeClock()
    sim = SimInverter(clock, latency=0.2, initial=500)
    dev = FakeMqttDevice(sim)
    dev.publish_accepted = False
    reader = FakeReader(sim)
    contracts = probe.build_operation_contracts(dev, [200])
    initial = {
        "smartMode": 1,
        "acMode": 2,
        "outputLimit": 500,
        "inputLimit": 0,
    }
    activity = probe.WriteActivity()

    submission = probe.publish_target(
        dev, 200, activity=activity, now=clock.now
    )
    report = probe.finalize_restoration(
        dev,
        reader,
        initial,
        contracts,
        _opts(),
        activity,
        sleep=clock.sleep,
        now=clock.now,
    )

    assert submission.locally_accepted is False
    assert activity.attempted is True
    assert activity.locally_accepted is False
    assert activity.accepted_command_ids == []
    assert report["restore_status"] == "not_attempted"
    assert report["detail"] == "no state-changing command was locally accepted"
    assert dev.property_writes == []


def test_builder_rejection_is_not_a_local_submission_and_needs_no_restore():
    clock = FakeClock()
    sim = SimInverter(clock, latency=0.2, initial=500)
    dev = FakeMqttDevice(sim)
    reader = FakeReader(sim)
    contracts = probe.build_operation_contracts(dev, [200])
    initial = {
        "smartMode": 1,
        "acMode": 2,
        "outputLimit": 500,
        "inputLimit": 0,
    }
    activity = probe.WriteActivity()

    def reject_builder(_target):
        raise RuntimeError("builder rejected")

    dev._build_write = reject_builder
    with pytest.raises(RuntimeError, match="builder rejected"):
        probe.publish_target(dev, 200, activity=activity, now=clock.now)
    report = probe.finalize_restoration(
        dev,
        reader,
        initial,
        contracts,
        _opts(),
        activity,
        sleep=clock.sleep,
        now=clock.now,
    )

    assert activity.attempted is False
    assert activity.locally_accepted is False
    assert report["restore_status"] == "not_attempted"
    assert dev.property_writes == []


def test_one_accepted_submission_requires_restore_after_a_later_rejection():
    clock = FakeClock()
    sim = SimInverter(clock, latency=0.0, initial=500)
    dev = FakeMqttDevice(sim)
    reader = FakeReader(sim)
    contracts = probe.build_operation_contracts(dev, [200, 500])
    initial = {
        "smartMode": 1,
        "acMode": 2,
        "outputLimit": 500,
        "inputLimit": 0,
    }
    activity = probe.WriteActivity()

    accepted = probe.publish_target(dev, 200, activity=activity, now=clock.now)
    dev.publish_accepted = False
    rejected = probe.publish_target(dev, 500, activity=activity, now=clock.now)
    report = probe.finalize_restoration(
        dev,
        reader,
        initial,
        contracts,
        _opts(),
        activity,
        sleep=clock.sleep,
        now=clock.now,
    )

    assert accepted.locally_accepted is True
    assert rejected.locally_accepted is False
    assert activity.locally_accepted is True
    assert activity.accepted_command_ids == ["1"]
    assert report["restore_status"] == "restore_verified"
    assert report["restore_verified"] is True


def test_restore_does_not_verify_unchanged_state_before_delayed_target_lands():
    clock = FakeClock()

    class QueuedInverter(SimInverter):
        def __init__(self):
            super().__init__(clock, latency=0.0, initial=500)
            self.queue = []

        def command(self, properties):
            delay = 0.75 if not self.queue else 1.5
            self.queue.append((self.clock.now() + delay, dict(properties)))

        def read(self):
            while self.queue and self.clock.now() >= self.queue[0][0]:
                _, properties = self.queue.pop(0)
                self.state.update(properties)
            return dict(self.state)

    sim = QueuedInverter()
    dev = FakeMqttDevice(sim)
    reader = FakeReader(sim)
    initial = {
        "smartMode": 1,
        "acMode": 2,
        "outputLimit": 500,
        "inputLimit": 0,
    }
    contracts = probe.build_operation_contracts(dev, [200])
    activity = probe.WriteActivity()

    assert probe.publish_target(
        dev, 200, activity=activity, now=clock.now
    ).locally_accepted
    report = probe.finalize_restoration(
        dev,
        reader,
        initial,
        contracts,
        _opts(timeout=3.0),
        activity,
        sleep=clock.sleep,
        now=clock.now,
    )

    assert report["restore_verified"] is True
    assert clock.now() >= 1001.5
    assert sim.queue == []
    assert sim.state["outputLimit"] == 500


def test_restore_fails_when_no_post_command_transition_is_observed():
    clock = FakeClock()

    class VerySlowQueuedInverter(SimInverter):
        def __init__(self):
            super().__init__(clock, latency=0.0, initial=500)
            self.queue = []

        def command(self, properties):
            self.queue.append(
                (self.clock.now() + 10.0 + len(self.queue), dict(properties))
            )

        def read(self):
            return dict(self.state)

    sim = VerySlowQueuedInverter()
    dev = FakeMqttDevice(sim)
    initial = {
        "smartMode": 1,
        "acMode": 2,
        "outputLimit": 500,
        "inputLimit": 0,
    }
    contracts = probe.build_operation_contracts(dev, [200])
    activity = probe.WriteActivity()
    probe.publish_target(dev, 200, activity=activity, now=clock.now)

    report = probe.finalize_restoration(
        dev,
        FakeReader(sim),
        initial,
        contracts,
        _opts(timeout=1.0),
        activity,
        sleep=clock.sleep,
        now=clock.now,
    )

    assert report["restore_status"] == "restore_failed"
    assert report["state_transition_observed"] is False
    assert "no post-command state transition" in report["detail"]
    assert probe.exit_code_after_restoration(0, activity, report) == 1


def test_restore_read_exception_still_submits_full_restore_and_fails_closed():
    clock = FakeClock()
    sim = SimInverter(clock, latency=0.0, initial=200)
    dev = FakeMqttDevice(sim)
    initial = {
        "smartMode": 1,
        "acMode": 2,
        "outputLimit": 200,
        "inputLimit": 0,
    }
    contracts = probe.build_operation_contracts(dev, [500])
    activity = probe.WriteActivity(
        attempted=True,
        locally_accepted=True,
        accepted_command_ids=["command-1"],
        accepted_modified_properties=list(initial),
    )

    class RaisingReader:
        def fetch(self):
            raise RuntimeError("HTTP read failed during restore")

    report = probe.finalize_restoration(
        dev,
        RaisingReader(),
        initial,
        contracts,
        _opts(timeout=0.5),
        activity,
        sleep=clock.sleep,
        now=clock.now,
    )

    assert dev.property_writes == [(initial, "probe_restore")]
    assert report["restore_status"] == "restore_failed"
    assert report["restore_submitted"] is True
    assert report["restore_verified"] is False
    assert probe.exit_code_after_restoration(0, activity, report) == 1


def test_restore_uses_latest_command_http_match_when_probe_ends_at_initial_state():
    clock = FakeClock()
    sim = SimInverter(clock, latency=0.0, initial=200)
    dev = FakeMqttDevice(sim)
    reader = FakeReader(sim)
    opts = _opts(samples=2, settle=0.0, timeout=1.0)
    activity = probe.WriteActivity()
    initial = {
        "smartMode": 1,
        "acMode": 2,
        "outputLimit": 200,
        "inputLimit": 0,
    }

    samples = probe.run_probe(
        dev,
        reader,
        [200, 500],
        opts,
        activity=activity,
        sleep=clock.sleep,
        now=clock.now,
    )
    report = probe.finalize_restoration(
        dev,
        reader,
        initial,
        probe.build_operation_contracts(dev, [200, 500]),
        opts,
        activity,
        sleep=clock.sleep,
        now=clock.now,
    )

    assert [(sample.target, sample.matched_target) for sample in samples] == [
        (500, True),
        (200, True),
    ]
    assert activity.latest_accepted_state_observed is True
    assert report["restore_status"] == "restore_verified"
    assert report["state_transition_observed"] is True


def test_later_accepted_submission_resets_prior_transition_evidence():
    clock = FakeClock()
    sim = SimInverter(clock, latency=0.0, initial=200)
    dev = FakeMqttDevice(sim)
    activity = probe.WriteActivity()

    probe.publish_target(dev, 500, activity=activity, now=clock.now)
    activity.record_latest_state_observed()
    assert activity.latest_accepted_state_observed is True

    probe.publish_target(dev, 200, activity=activity, now=clock.now)
    assert activity.latest_accepted_state_observed is False


def test_unverified_required_restore_forces_nonzero_exit_status():
    activity = probe.WriteActivity(
        attempted=True,
        locally_accepted=True,
        accepted_command_ids=["command-1"],
    )
    failed = {"restore_status": "restore_failed", "restore_verified": False}
    verified = {"restore_status": "restore_verified", "restore_verified": True}

    assert probe.exit_code_after_restoration(0, activity, failed) == 1
    assert probe.exit_code_after_restoration(130, activity, failed) == 130
    assert probe.exit_code_after_restoration(0, activity, verified) == 0
    assert probe.exit_code_after_restoration(
        0, probe.WriteActivity(), {"restore_verified": False}
    ) == 0


def test_incomplete_captured_state_can_never_be_reported_as_restored():
    clock = FakeClock()
    sim = SimInverter(clock, latency=0.0, initial=500)
    dev = FakeMqttDevice(sim)
    contracts = probe.build_operation_contracts(dev, [200])
    activity = probe.WriteActivity(
        attempted=True,
        locally_accepted=True,
        accepted_command_ids=["1"],
        accepted_modified_properties=[
            "smartMode", "acMode", "outputLimit", "inputLimit"
        ],
    )

    report = probe.finalize_restoration(
        dev,
        FakeReader(sim),
        {"smartMode": 1, "acMode": 2, "outputLimit": 500},
        contracts,
        _opts(),
        activity,
        sleep=clock.sleep,
        now=clock.now,
    )

    assert report["restore_status"] == "restore_failed"
    assert report["restore_attempted"] is True
    assert report["restore_submitted"] is False
    assert report["restore_verified"] is False
    assert report["unknown_properties"] == ["inputLimit"]
    assert dev.property_writes == []
    assert probe.exit_code_after_restoration(0, activity, report) == 1


def test_restore_reports_failed_verification_honestly():
    clock = FakeClock()
    sim = SimInverter(clock, latency=999.0, initial=500)
    dev = FakeMqttDevice(sim)
    reader = FakeReader(sim)
    contracts = probe.build_operation_contracts(dev, [200, 500])
    report = probe.restore_initial_state(
        dev,
        reader,
        {"smartMode": 0, "acMode": 1, "outputLimit": 0, "inputLimit": 0},
        contracts,
        _opts(timeout=1.0),
        sleep=clock.sleep, now=clock.now,
    )
    assert report["restored"] is False
    assert report["restore_verified"] is False
    assert report["restore_status"] == "restore_failed"


def test_restore_verification_fails_on_enum_mode_within_watt_tolerance():
    """A restored acMode that differs by 1 must FAIL verification: mode/enum
    properties compare exactly, never within the watt tolerance.
    """

    clock = FakeClock()
    sim = SimInverter(clock, latency=0.2, initial=500)
    dev = FakeMqttDevice(sim)

    class WrongModeReader(FakeReader):
        def fetch(self):
            state = super().fetch()
            state.ac_mode = 2  # desired was 1; off by one, within watt tolerance
            return state

    reader = WrongModeReader(sim)
    contracts = probe.build_operation_contracts(dev, [200, 500])
    report = probe.restore_initial_state(
        dev, reader, {"smartMode": 0, "acMode": 1, "outputLimit": 0, "inputLimit": 0},
        contracts, _opts(timeout=1.0, match_tolerance=5),
        sleep=clock.sleep, now=clock.now,
    )
    assert report["restore_verified"] is False


def test_rejected_full_restore_is_failed_without_partial_fallback():
    """A rejected atomic restore fails; it never emits another power command."""

    clock = FakeClock()
    sim = SimInverter(clock, latency=0.2, initial=500)

    class RejectPropertiesDevice(FakeMqttDevice):
        def write_properties(self, properties, *, reason, **_kwargs):
            return False  # atomic property write rejected

    dev = RejectPropertiesDevice(sim)
    reader = FakeReader(sim)
    contracts = probe.build_operation_contracts(dev, [200, 500])
    report = probe.restore_initial_state(
        dev, reader, {"smartMode": 0, "acMode": 1, "outputLimit": 0, "inputLimit": 0},
        contracts, _opts(timeout=2.0), sleep=clock.sleep, now=clock.now,
    )
    assert report["restore_status"] == "restore_failed"
    assert report["restore_partial"] is False
    assert report["restore_verified"] is False
    assert dev.published_messages == []


def test_failed_full_restore_never_falls_back_to_atomic_normal_power_command():
    clock = FakeClock()
    sim = SimInverter(clock, latency=0.2, initial=500)

    class RejectPropertiesDevice(FakeMqttDevice):
        def write_properties(self, properties, *, reason, **_kwargs):
            self.property_writes.append((dict(properties), reason))
            return False

    dev = RejectPropertiesDevice(sim)
    reader = FakeReader(sim)
    contracts = probe.build_operation_contracts(dev, [200, 500])
    initial = {
        "smartMode": 0,
        "acMode": 1,
        "outputLimit": 0,
        "inputLimit": 0,
    }

    report = probe.restore_initial_state(
        dev,
        reader,
        initial,
        contracts,
        _opts(timeout=2.0),
        sleep=clock.sleep,
        now=clock.now,
    )

    assert report["restore_status"] == "restore_failed"
    assert report["restore_verified"] is False
    assert report["restore_partial"] is False
    assert report["captured_properties"] == [
        "acMode", "inputLimit", "outputLimit", "smartMode"
    ]
    assert report["changed_properties"] == [
        "acMode", "inputLimit", "outputLimit", "smartMode"
    ]
    assert report["submitted_properties"] == []
    assert set(report["matched_properties"]) | set(report["mismatched_properties"]) == {
        "smartMode", "acMode", "outputLimit", "inputLimit"
    }
    # The old fallback called `_publish_message` with a ZenSDK normal-power
    # command, silently overwriting all four fields while calling it output-only.
    assert dev.published_messages == []


def test_preflight_rejects_unrestorable_initial_state():
    clock = FakeClock()
    sim = SimInverter(clock, latency=0.2, initial=500)

    class UnrestorableDevice(FakeMqttDevice):
        def check_property_writes(self, properties):
            return {"acMode": "invalid_property_value"}

    dev = UnrestorableDevice(sim)
    contracts = probe.build_operation_contracts(dev, [200, 500])
    unrestorable = probe.preflight_restorable(
        dev,
        {"smartMode": 1, "acMode": 0, "outputLimit": 100, "inputLimit": 0},
        contracts,
    )
    assert "acMode" in unrestorable


def test_initial_restore_state_captures_only_reported_numeric_properties():
    session = FakeSession(
        report={"properties": {"smartMode": 0, "acMode": 1, "outputLimit": 100, "electricLevel": 60}}
    )
    captured = probe.initial_restore_state(
        "10.0.0.5",
        session,
        ("smartMode", "acMode", "outputLimit", "inputLimit"),
    )
    assert captured == {"smartMode": 0, "acMode": 1, "outputLimit": 100}


# --- preview -----------------------------------------------------------------


def test_preview_shows_topic_qos_properties_and_no_secrets(capsys):
    clock = FakeClock()
    sim = SimInverter(clock, latency=0.1, initial=200)
    dev = FakeMqttDevice(sim)
    reader = FakeReader(sim)
    gate = SimpleNamespace(gate_name="allow_mqtt_zendure_control_writes", gate_enabled=True, blocked_by=())
    initial = {"smartMode": 1, "acMode": 2, "outputLimit": 200, "inputLimit": 0}
    contracts = probe.build_operation_contracts(dev, [200, 500])
    probe.print_preview(dev, reader, [200, 500], gate, initial, contracts)
    out = capsys.readouterr().out
    assert "qos=1" in out
    assert "retain=False" in out
    assert "iot/…/…/properties/write" in out
    assert "iot/PK/SN123/properties/write" not in out
    assert "'smartMode': 1" in out
    assert "restorable initial properties" in out
    assert "effective write topic" in out
    assert "single-writer advisory" in out
    assert "password" not in out.lower()


def test_preview_redacts_unknown_cloud_topic_shapes(capsys):
    clock = FakeClock()
    sim = SimInverter(clock, latency=0.1, initial=200)
    dev = FakeMqttDevice(sim)
    reader = FakeReader(sim)
    raw_topic = "private/account-route/device-route/properties/write"
    original_build = dev._build_write

    def custom_topic_build(target):
        message, message_id, operation, expected = original_build(target)
        message.topic = raw_topic
        return message, message_id, operation, expected

    dev._build_write = custom_topic_build
    dev.describe = lambda: {
        "power_write_profile": "zensdk_properties_write",
        "effective_write_topic": raw_topic,
        "effective_write_topic_source": "custom",
        "write_topic_obsolete": False,
    }
    contracts = probe.build_operation_contracts(dev, [200, 500])
    gate = SimpleNamespace(gate_name="mqtt_zendure", gate_enabled=True, blocked_by=())

    probe.print_preview(
        dev,
        reader,
        [200, 500],
        gate,
        {"smartMode": 1, "acMode": 2, "outputLimit": 200, "inputLimit": 0},
        contracts,
    )

    out = capsys.readouterr().out
    assert raw_topic not in out
    assert "<redacted-cloud-topic>" in out


# --- top-level restoration orchestration ------------------------------------


class _MainRuntime:
    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def _run_main_harness(monkeypatch, *, action=None, initial=None, argv=None,
                      physical_serial=_UNSET, reader_sn=_UNSET):
    from ems import clients
    from ems.zendure_mqtt import control_runtime

    clock = FakeClock()
    sim = SimInverter(clock, latency=0.0, initial=500)
    dev = FakeMqttDevice(sim)
    if physical_serial is not _UNSET:
        dev.physical_serial = physical_serial
    reader = FakeReader(sim)
    if reader_sn is not _UNSET:
        reader.sn = reader_sn
    runtime = _MainRuntime()
    gate = SimpleNamespace(
        allowed=True,
        gate_name="mqtt_zendure",
        gate_enabled=True,
        blocked_by=(),
    )
    cfg = SimpleNamespace(
        MAX_DEVICE_POWER=800,
        resolve_write_gate=lambda _name: gate,
    )
    configured_initial = dict(
        initial
        or {
            "smartMode": 1,
            "acMode": 2,
            "outputLimit": 500,
            "inputLimit": 0,
        }
    )

    monkeypatch.setattr(
        probe,
        "bootstrap_config",
        lambda _path: (cfg, {"devices": []}, "/config/config.json"),
    )
    monkeypatch.setattr(clients, "create_session", lambda: SimpleNamespace())
    monkeypatch.setattr(
        control_runtime,
        "build_zendure_mqtt_control_runtime",
        lambda _config: runtime,
    )
    monkeypatch.setattr(
        probe, "resolve_mqtt_device", lambda *_args, **_kwargs: (dev, None)
    )
    monkeypatch.setattr(
        probe, "resolve_http_reader", lambda *_args, **_kwargs: (reader, None)
    )
    monkeypatch.setattr(probe, "wait_for_broker", lambda *_args: True)
    monkeypatch.setattr(
        probe,
        "initial_restore_state",
        lambda _ip, _session, _required: dict(configured_initial),
    )
    if action is not None:
        monkeypatch.setattr(
            probe,
            "run_probe",
            lambda current_dev, _reader, _values, _opts, *, activity, **_kwargs: action(
                current_dev, activity
            ),
        )

    code = probe.main(
        argv
        or [
            "--confirm-writes",
            "--samples",
            "1",
            "--poll-interval",
            "0.01",
            "--settle",
            "0",
        ]
    )
    return code, dev, runtime


def test_main_exception_before_submit_does_not_restore(monkeypatch, capsys):
    def fail_before_submit(_dev, _activity):
        raise RuntimeError("before submit")

    code, dev, runtime = _run_main_harness(
        monkeypatch, action=fail_before_submit
    )

    output = capsys.readouterr()
    assert code == 1
    assert dev.published_messages == []
    assert dev.property_writes == []
    assert runtime.started is True and runtime.stopped is True
    assert "'restore_status': 'not_attempted'" in output.out


def test_main_exception_after_accepted_submit_restores(monkeypatch, capsys):
    initial = {
        "smartMode": 1,
        "acMode": 2,
        "outputLimit": 500,
        "inputLimit": 0,
    }

    def fail_after_submit(dev, activity):
        assert probe.publish_target(dev, 200, activity=activity).locally_accepted
        raise RuntimeError("after submit")

    code, dev, runtime = _run_main_harness(
        monkeypatch,
        action=fail_after_submit,
        initial=initial,
        argv=[
            "--confirm-writes",
            "--samples",
            "1",
            "--poll-interval",
            "0.01",
            "--timeout",
            "0.05",
            "--settle",
            "0",
        ],
    )

    output = capsys.readouterr()
    assert code == 1
    assert dev.property_writes == [(initial, "probe_restore")]
    assert runtime.stopped is True
    assert "'restore_status': 'restore_verified'" in output.out


def test_main_keyboard_interrupt_after_accepted_submit_restores(monkeypatch, capsys):
    initial = {
        "smartMode": 1,
        "acMode": 2,
        "outputLimit": 500,
        "inputLimit": 0,
    }

    def interrupt_after_submit(dev, activity):
        assert probe.publish_target(dev, 200, activity=activity).locally_accepted
        raise KeyboardInterrupt

    code, dev, runtime = _run_main_harness(
        monkeypatch, action=interrupt_after_submit, initial=initial
    )

    output = capsys.readouterr()
    assert code == 130
    assert dev.property_writes == [(initial, "probe_restore")]
    assert runtime.stopped is True
    assert "'restore_status': 'restore_verified'" in output.out


def test_main_restore_read_exception_still_stops_runtime(monkeypatch, capsys):
    initial = {
        "smartMode": 1,
        "acMode": 2,
        "outputLimit": 500,
        "inputLimit": 0,
    }

    def fail_after_submit(dev, activity):
        assert probe.publish_target(dev, 200, activity=activity).locally_accepted

        def raise_read(_reader):
            raise RuntimeError("restore reader failed")

        monkeypatch.setattr(probe, "read_power_state", raise_read)
        raise RuntimeError("after submit")

    code, dev, runtime = _run_main_harness(
        monkeypatch,
        action=fail_after_submit,
        initial=initial,
        argv=[
            "--confirm-writes",
            "--samples",
            "1",
            "--poll-interval",
            "0.01",
            "--timeout",
            "0.05",
            "--settle",
            "0",
        ],
    )

    output = capsys.readouterr()
    assert code == 1
    assert dev.property_writes == [(initial, "probe_restore")]
    assert runtime.stopped is True
    assert "'restore_status': 'restore_failed'" in output.out


def test_dry_preview_incomplete_restore_preflight_exits_nonzero(
    monkeypatch, capsys
):
    code, dev, runtime = _run_main_harness(
        monkeypatch,
        initial={"smartMode": 1, "acMode": 2, "outputLimit": 500},
        argv=["--dry-preview"],
    )

    output = capsys.readouterr()
    assert code == 1
    assert dev.published_messages == []
    assert dev.property_writes == []
    assert runtime.started is False
    assert "NON-RESTORABLE initial properties" in output.out
    assert "preflight_failed" in output.err


# --- device/serial resolution ------------------------------------------------


class FakeSession:
    def __init__(self, report=None, raise_exc=False):
        self._report = report
        self._raise = raise_exc

    def get(self, url, timeout=None):
        if self._raise:
            raise OSError("unreachable")
        return SimpleNamespace(json=lambda: self._report)


class FakeRuntime:
    def __init__(self, devices):
        self.devices = devices


def _mqtt_device(name, sn, *, physical_serial=_UNSET):
    return SimpleNamespace(
        name=name,
        sn=sn,
        physical_serial=sn if physical_serial is _UNSET else physical_serial,
    )


def test_serial_from_report_top_level_and_properties():
    assert probe.serial_from_report({"sn": "SN1"}) == "SN1"
    assert probe.serial_from_report({"properties": {"serialNumber": "SN2"}}) == "SN2"
    assert probe.serial_from_report({"deviceSn": "  SN3 "}) == "SN3"
    assert probe.serial_from_report({"properties": {}}) is None
    assert probe.serial_from_report("nope") is None


def test_resolve_by_api_ip_matches_device_by_serial():
    runtime = FakeRuntime([_mqtt_device("INV_2", "SN123"), _mqtt_device("INV_9", "OTHER")])
    session = FakeSession(report={"sn": "SN123", "properties": {"outputLimit": 200}})

    dev, reader, serial, err = probe.resolve_by_api_ip(runtime, session, "192.168.1.50", None)

    assert err is None
    assert dev.name == "INV_2"
    assert serial == "SN123"
    assert reader.ip == "192.168.1.50"


def test_resolve_by_api_ip_errors_when_no_device_matches():
    runtime = FakeRuntime([_mqtt_device("INV_9", "OTHER")])
    session = FakeSession(report={"sn": "SN123"})
    dev, reader, serial, err = probe.resolve_by_api_ip(runtime, session, "192.168.1.50", None)
    assert dev is None
    assert "no MQTT control device has a trusted physical serial" in err
    # The full HTTP serial is never echoed, only its redacted suffix.
    assert "SN123" not in err.replace("…N123", "")


def test_resolve_by_api_ip_errors_when_api_unreachable():
    runtime = FakeRuntime([_mqtt_device("INV_2", "SN123")])
    session = FakeSession(raise_exc=True)
    dev, reader, serial, err = probe.resolve_by_api_ip(runtime, session, "10.0.0.9", None)
    assert dev is None
    assert "could not read http://10.0.0.9/properties/report" in err


def _rich_device(name, sn, device_id=None, broker_ref=None, *, physical_serial=_UNSET):
    return SimpleNamespace(
        name=name, sn=sn, _device_id=device_id or sn, broker_ref=broker_ref,
        physical_serial=sn if physical_serial is _UNSET else physical_serial,
        source="zendure_cloud_mqtt", hardware_profile="solarflow_800_pro_2",
    )


def test_resolve_by_api_ip_aborts_when_serial_matches_two_devices():
    runtime = FakeRuntime([
        _rich_device("INV_A", "SN123", broker_ref="broker_a"),
        _rich_device("INV_B", "SN123", broker_ref="broker_b"),
    ])
    session = FakeSession(report={"sn": "SN123"})
    dev, reader, serial, err = probe.resolve_by_api_ip(runtime, session, "192.168.1.50", None)
    assert dev is None
    assert "ambiguous device selection" in err
    # Redacted: only the last-4 of the serial ever appears, never the full value.
    assert "…N123" in err or "serial=…" in err
    assert "SN123" not in err.replace("…N123", "")


def test_selectors_disambiguate_two_devices_sharing_a_serial():
    runtime = FakeRuntime([
        _rich_device("INV_A", "SN123", broker_ref="broker_a"),
        _rich_device("INV_B", "SN123", broker_ref="broker_b"),
    ])
    dev, err = probe.select_mqtt_device(runtime, broker_ref="broker_b")
    assert err is None
    assert dev.name == "INV_B"


def test_resolve_mqtt_device_errors_on_duplicate_names_not_first():
    runtime = FakeRuntime([_rich_device("INV", "SN1"), _rich_device("INV", "SN2")])
    dev, err = probe.resolve_mqtt_device(runtime, "INV")
    assert dev is None
    assert "ambiguous device selection" in err


# --- defect 2: serial-less Cloud cross-transport binding ---------------------


def _serialless_cloud_device(name, route_id, broker_ref="zendure_cloud"):
    # A serial-less Cloud device: sn falls back to the Cloud route id and there is
    # no trusted physical serial.
    return _rich_device(
        name, route_id, device_id=route_id, broker_ref=broker_ref,
        physical_serial=None,
    )


def test_evaluate_http_binding_matrix():
    verified = probe.evaluate_http_binding(
        SimpleNamespace(physical_serial="SN123"), "SN123"
    )
    assert verified.status == probe.BINDING_VERIFIED
    assert verified.verified is True
    assert verified.write_block_reason(acknowledged=False, exact_selectors=False) is None

    conflict = probe.evaluate_http_binding(
        SimpleNamespace(physical_serial="SN123"), "PHYS999"
    )
    assert conflict.status == probe.BINDING_CONFLICT
    assert conflict.write_block_reason(acknowledged=True, exact_selectors=True)

    unbound = probe.evaluate_http_binding(
        SimpleNamespace(physical_serial=None), "PHYS1234"
    )
    assert unbound.status == probe.BINDING_UNBOUND
    # Blocked without exact selectors, blocked without acknowledgement, allowed
    # only with both.
    assert unbound.write_block_reason(acknowledged=True, exact_selectors=False)
    assert unbound.write_block_reason(acknowledged=False, exact_selectors=True)
    assert unbound.write_block_reason(acknowledged=True, exact_selectors=True) is None

    unverified = probe.evaluate_http_binding(
        SimpleNamespace(physical_serial="SN123"), None
    )
    assert unverified.status == probe.BINDING_UNVERIFIED
    assert unverified.write_block_reason(acknowledged=True, exact_selectors=True)


def test_evaluate_http_binding_serial_comparison_is_case_insensitive():
    # The physical serial identity folds case (shared identity rule), so a
    # case-only difference between config and HTTP readback is a verified match.
    lower_cfg = probe.evaluate_http_binding(
        SimpleNamespace(physical_serial="serial-abc"), "SERIAL-ABC"
    )
    assert lower_cfg.status == probe.BINDING_VERIFIED
    upper_cfg = probe.evaluate_http_binding(
        SimpleNamespace(physical_serial="SERIAL-ABC"), "serial-abc"
    )
    assert upper_cfg.status == probe.BINDING_VERIFIED
    # The original values are preserved for display, never lowercased.
    assert upper_cfg.configured_serial == "SERIAL-ABC"
    assert upper_cfg.http_serial == "serial-abc"


def test_evaluate_http_binding_folds_whitespace_and_case():
    binding = probe.evaluate_http_binding(
        SimpleNamespace(physical_serial="  sn-1  "), "SN-1"
    )
    assert binding.status == probe.BINDING_VERIFIED


def test_evaluate_http_binding_still_flags_truly_different_serials():
    binding = probe.evaluate_http_binding(
        SimpleNamespace(physical_serial="SN-ABC"), "SN-XYZ"
    )
    assert binding.status == probe.BINDING_CONFLICT


def test_select_by_physical_serial_matches_case_insensitively():
    runtime = FakeRuntime([_mqtt_device("INV_2", "sn-abc")])
    dev, err = probe.select_by_physical_serial(runtime, "SN-ABC")
    assert err is None
    assert dev.name == "INV_2"


def test_resolve_by_api_ip_binds_case_differing_trusted_serial():
    runtime = FakeRuntime([_mqtt_device("INV_2", "sn123")])
    session = FakeSession(report={"sn": "SN123"})
    dev, reader, serial, err = probe.resolve_by_api_ip(
        runtime, session, "192.168.1.50", None
    )
    assert err is None
    assert dev.name == "INV_2"
    binding = probe.evaluate_http_binding(dev, reader.sn)
    assert binding.status == probe.BINDING_VERIFIED


def test_binding_summaries_never_expose_full_serial():
    for http in ("PHYS1234", None):
        for configured in ("SN123456", None):
            binding = probe.evaluate_http_binding(
                SimpleNamespace(physical_serial=configured), http
            )
            summary = binding.summary()
            if http:
                assert http not in summary
            if configured:
                assert configured not in summary


def test_resolve_by_api_ip_binds_matching_trusted_serial():
    runtime = FakeRuntime([_mqtt_device("INV_2", "SN123")])
    session = FakeSession(report={"sn": "SN123"})
    dev, reader, serial, err = probe.resolve_by_api_ip(runtime, session, "192.168.1.50", None)
    assert err is None
    binding = probe.evaluate_http_binding(dev, reader.sn)
    assert binding.status == probe.BINDING_VERIFIED


def test_resolve_by_api_ip_selects_serialless_cloud_device_by_exact_selectors():
    route = "CLOUD_ROUTE_ID"
    runtime = FakeRuntime([_serialless_cloud_device("INV_1", route)])
    session = FakeSession(report={"sn": "PHYSICAL_SERIAL"})
    # Exact route selectors uniquely select the serial-less Cloud device even
    # though the HTTP serial differs from the Cloud route id.
    dev, reader, serial, err = probe.resolve_by_api_ip(
        runtime, session, "192.168.1.50", "INV_1",
        device_id=route, broker_ref="zendure_cloud",
    )
    assert err is None
    assert dev.name == "INV_1"
    binding = probe.evaluate_http_binding(dev, reader.sn)
    assert binding.status == probe.BINDING_UNBOUND
    assert binding.http_serial == "PHYSICAL_SERIAL"


def test_resolve_by_api_ip_serialless_without_selectors_is_blocked():
    route = "CLOUD_ROUTE_ID"
    runtime = FakeRuntime([_serialless_cloud_device("INV_1", route)])
    session = FakeSession(report={"sn": "PHYSICAL_SERIAL"})
    dev, reader, serial, err = probe.resolve_by_api_ip(runtime, session, "192.168.1.50", None)
    assert dev is None
    assert "no MQTT control device has a trusted physical serial" in err


def test_resolve_by_api_ip_ambiguous_serialless_selection_blocked():
    runtime = FakeRuntime([
        _serialless_cloud_device("INV_1", "ROUTE_A", broker_ref="cloud_a"),
        _serialless_cloud_device("INV_2", "ROUTE_B", broker_ref="cloud_b"),
    ])
    session = FakeSession(report={"sn": "PHYSICAL_SERIAL"})
    # No explicit selector and no trusted serial to auto-match: blocked.
    dev, reader, serial, err = probe.resolve_by_api_ip(runtime, session, "192.168.1.50", None)
    assert dev is None
    assert "must be selected explicitly" in err


def test_main_serial_conflict_blocks_before_any_publish(monkeypatch, capsys):
    code, dev, runtime = _run_main_harness(
        monkeypatch,
        physical_serial="SN123",
        reader_sn="PHYS999",
        argv=["--confirm-writes", "--samples", "1", "--poll-interval", "0.01", "--settle", "0"],
    )
    output = capsys.readouterr()
    assert code == 1
    assert dev.published_messages == []
    assert dev.property_writes == []
    assert runtime.started is False
    assert "cross-transport identity conflict" in output.err


def test_main_serialless_unbound_blocks_write_without_acknowledgement(monkeypatch, capsys):
    code, dev, runtime = _run_main_harness(
        monkeypatch,
        physical_serial=None,
        reader_sn="PHYS1234",
        argv=[
            "--confirm-writes", "--device", "INV_1", "--device-id", "ROUTE",
            "--broker-ref", "zendure_cloud", "--samples", "1",
            "--poll-interval", "0.01", "--settle", "0",
        ],
    )
    output = capsys.readouterr()
    assert code == 1
    assert dev.published_messages == []
    assert dev.property_writes == []
    assert runtime.started is False
    assert "serial-less Cloud device" in output.err


def test_main_serialless_unbound_requires_exact_selectors(monkeypatch, capsys):
    code, dev, runtime = _run_main_harness(
        monkeypatch,
        physical_serial=None,
        reader_sn="PHYS1234",
        argv=[
            "--confirm-writes", "--confirm-unbound-api-readback",
            "--samples", "1", "--poll-interval", "0.01", "--settle", "0",
        ],
    )
    output = capsys.readouterr()
    assert code == 1
    assert dev.published_messages == []
    assert runtime.started is False
    assert "exact --device-name" in output.err


def test_main_serialless_unbound_proceeds_with_acknowledgement(monkeypatch, capsys):
    code, dev, runtime = _run_main_harness(
        monkeypatch,
        physical_serial=None,
        reader_sn="PHYS1234",
        argv=[
            "--confirm-writes", "--confirm-unbound-api-readback",
            "--device", "INV_1", "--device-id", "ROUTE",
            "--broker-ref", "zendure_cloud", "--samples", "1",
            "--poll-interval", "0.01", "--timeout", "0.05", "--settle", "0",
        ],
    )
    capsys.readouterr()
    assert code == 0
    assert runtime.started is True and runtime.stopped is True
    # A write actually happened (and was restored), proving the gate opened.
    assert dev.property_writes == [
        ({"smartMode": 1, "acMode": 2, "outputLimit": 500, "inputLimit": 0}, "probe_restore")
    ]


def test_dry_preview_serialless_cloud_states_unverified_binding(monkeypatch, capsys):
    code, dev, runtime = _run_main_harness(
        monkeypatch,
        physical_serial=None,
        reader_sn="PHYS1234",
        initial={"smartMode": 1, "acMode": 2, "outputLimit": 500, "inputLimit": 0},
        argv=["--dry-preview", "--device", "INV_1", "--device-id", "ROUTE", "--broker-ref", "zendure_cloud"],
    )
    output = capsys.readouterr()
    assert code == 0
    assert dev.published_messages == []
    assert runtime.started is False
    assert "physical serial not stored" in output.out
    assert "UNVERIFIED" in output.out
    assert "remains blocked" in output.out
    # The full HTTP serial is never printed, only its redacted suffix.
    assert "PHYS1234" not in output.out


def test_dry_preview_serial_conflict_exits_nonzero(monkeypatch, capsys):
    code, dev, runtime = _run_main_harness(
        monkeypatch,
        physical_serial="SN123",
        reader_sn="PHYS999",
        initial={"smartMode": 1, "acMode": 2, "outputLimit": 500, "inputLimit": 0},
        argv=["--dry-preview"],
    )
    output = capsys.readouterr()
    assert code == 1
    assert dev.published_messages == []
    assert runtime.started is False
    assert "identity conflict" in output.err


def test_reports_render_without_error():
    clock = FakeClock()
    sim = SimInverter(clock, latency=0.4, initial=200)
    dev = FakeMqttDevice(sim)
    reader = FakeReader(sim)
    opts = _opts(samples=2)
    samples = probe.run_probe(dev, reader, [200, 500], opts, sleep=clock.sleep, now=clock.now)
    stats = probe.summarize(samples, opts)

    text = probe.format_text_report(dev, reader, [200, 500], samples, stats)
    assert "setpoint match from submit ms" in text
    assert "local submit duration ms" in text
    assert "broker delivery from submit ms" in text
    assert "physical reaction from submit ms" in text
    assert "host->broker publish" not in text
    md = probe.format_markdown_report(dev, reader, [200, 500], stats)
    assert "| Setpoint match from submit p50 |" in md
    assert "| Broker delivery from submit p50 |" in md
    assert "| Physical reaction from submit p50 |" in md


# --- timing: distinct submit / broker / setpoint dimensions ------------------


class _DeliveryDevice(FakeMqttDevice):
    """A fake device whose service exposes broker delivery tracking."""

    def __init__(self, sim, *, deliver=True):
        super().__init__(sim)
        self._service = SimpleNamespace(
            delivery_confirmed=lambda mid: deliver and mid is not None
        )


class _TimelineInverter(SimInverter):
    """Setpoint and physical output become visible on independent deadlines."""

    def __init__(self, clock, *, setpoint_at, physical_at, initial):
        super().__init__(clock, latency=setpoint_at, initial=initial)
        self.physical_latency = physical_at
        self.command_started = None
        self.commanded_target = None

    def command(self, properties):
        self.command_started = self.clock.now()
        self.commanded_target = properties["outputLimit"]
        super().command(properties)

    def read(self):
        state = super().read()
        if (
            self.command_started is not None
            and self.clock.now() >= self.command_started + self.physical_latency
        ):
            self.state["outputHomePower"] = self.commanded_target
            state = dict(self.state)
        return state


class _TimedDeliveryDevice(FakeMqttDevice):
    def __init__(self, sim, clock, *, ack_after):
        super().__init__(sim)
        self.clock = clock
        self.ack_after = ack_after
        self._service = SimpleNamespace(delivery_confirmed=self._delivery_confirmed)

    def _delivery_confirmed(self, mid):
        return (
            mid is not None
            and self.sim.command_started is not None
            and self.clock.now() >= self.sim.command_started + self.ack_after
        )


class _SlowSubmissionDevice(_TimedDeliveryDevice):
    def _publish_message(self, message):
        self.clock.sleep(0.2)
        return super()._publish_message(message)


def test_mode_test_uses_the_common_submit_delivery_and_http_timeline():
    clock = FakeClock()
    sim = _TimelineInverter(
        clock, setpoint_at=1.0, physical_at=99.0, initial=0
    )
    sim.state.update({"smartMode": 0, "acMode": 1, "outputLimit": 0})
    dev = _TimedDeliveryDevice(sim, clock, ack_after=0.1)

    result = probe.run_mode_recovery_test(
        dev,
        FakeReader(sim),
        500,
        _opts(poll_interval=0.1, connect_timeout=2.0),
        sleep=clock.sleep,
        now=clock.now,
    )

    assert result["result"] == "mode_and_setpoint_verified"
    assert result["local_submit_duration_ms"] == pytest.approx(0.0)
    assert result["broker_delivery_status"] == "delivered"
    assert result["broker_delivery_from_submit_ms"] == pytest.approx(100.0)
    assert result["setpoint_match_from_submit_ms"] == pytest.approx(1000.0)


def test_all_latency_dimensions_share_the_pre_submit_origin():
    """PUBACK, setpoint and physical evidence use one submission timeline.

    Submit at 0ms, PUBACK at 100ms, HTTP setpoint at 1000ms and physical output
    at 1600ms.  The incremental physical-after-setpoint value remains available,
    but must not replace the primary physical-from-submit duration.
    """

    clock = FakeClock()
    sim = _TimelineInverter(
        clock, setpoint_at=1.0, physical_at=1.6, initial=200
    )
    dev = _TimedDeliveryDevice(sim, clock, ack_after=0.1)

    samples = probe.run_probe(
        dev,
        FakeReader(sim),
        [200, 500],
        _opts(
            samples=1,
            poll_interval=0.1,
            connect_timeout=1.0,
            verify_output=True,
            output_timeout=2.0,
        ),
        sleep=clock.sleep,
        now=clock.now,
    )

    sample = samples[0]
    assert sample.broker_delivery_from_submit_ms == pytest.approx(100.0)
    assert sample.setpoint_match_from_submit_ms == pytest.approx(1000.0)
    assert sample.physical_reaction_from_submit_ms == pytest.approx(1600.0)
    assert sample.physical_reaction_after_setpoint_ms == pytest.approx(600.0)


def test_physical_reaction_before_http_setpoint_keeps_its_first_timestamp():
    """Physical evidence is independent of when HTTP reports outputLimit."""

    clock = FakeClock()
    sim = _TimelineInverter(
        clock, setpoint_at=1.0, physical_at=0.4, initial=200
    )
    dev = _TimedDeliveryDevice(sim, clock, ack_after=0.1)

    sample = probe.run_probe(
        dev,
        FakeReader(sim),
        [200, 500],
        _opts(
            samples=1,
            poll_interval=0.1,
            connect_timeout=1.0,
            verify_output=True,
            output_timeout=2.0,
        ),
        sleep=clock.sleep,
        now=clock.now,
    )[0]

    assert sample.setpoint_match_from_submit_ms == pytest.approx(1000.0)
    assert sample.physical_reaction_from_submit_ms == pytest.approx(400.0)
    assert sample.physical_reaction_after_setpoint_ms is None


def test_physical_latency_is_not_claimed_when_baseline_already_matches_target():
    clock = FakeClock()
    sim = _TimelineInverter(
        clock, setpoint_at=0.5, physical_at=99.0, initial=200
    )
    sim.state["outputHomePower"] = 500
    dev = _TimedDeliveryDevice(sim, clock, ack_after=0.1)

    sample = probe.run_probe(
        dev,
        FakeReader(sim),
        [200, 500],
        _opts(
            samples=1,
            poll_interval=0.1,
            connect_timeout=1.0,
            verify_output=True,
            output_timeout=0.5,
        ),
        sleep=clock.sleep,
        now=clock.now,
    )[0]

    assert sample.matched_target is True
    assert sample.physical == "baseline_already_at_target"
    assert sample.physical_reaction_from_submit_ms is None
    assert sample.physical_reaction_after_setpoint_ms is None


def test_http_observation_is_not_blocked_by_slow_puback():
    clock = FakeClock()
    sim = _TimelineInverter(
        clock, setpoint_at=0.5, physical_at=99.0, initial=200
    )
    dev = _TimedDeliveryDevice(sim, clock, ack_after=2.0)

    sample = probe.run_probe(
        dev,
        FakeReader(sim),
        [200, 500],
        _opts(
            samples=1,
            poll_interval=0.1,
            connect_timeout=2.5,
            verify_output=False,
        ),
        sleep=clock.sleep,
        now=clock.now,
    )[0]

    assert sample.setpoint_match_from_submit_ms == pytest.approx(500.0)
    assert sample.broker_delivery_from_submit_ms == pytest.approx(2000.0)


def test_broker_timeout_does_not_erase_matching_http_setpoint_evidence():
    clock = FakeClock()
    sim = _TimelineInverter(
        clock, setpoint_at=0.5, physical_at=99.0, initial=200
    )
    dev = _TimedDeliveryDevice(sim, clock, ack_after=99.0)

    sample = probe.run_probe(
        dev,
        FakeReader(sim),
        [200, 500],
        _opts(
            samples=1,
            poll_interval=0.1,
            connect_timeout=0.8,
            verify_output=False,
        ),
        sleep=clock.sleep,
        now=clock.now,
    )[0]

    assert sample.matched_target is True
    assert sample.setpoint_match_from_submit_ms == pytest.approx(500.0)
    assert sample.broker_delivery_status == "timeout"
    assert sample.broker_delivery_from_submit_ms is None


def test_terminal_broker_delivery_status_is_not_relabelled_timeout():
    clock = FakeClock()
    sim = _TimelineInverter(
        clock, setpoint_at=0.5, physical_at=99.0, initial=200
    )
    dev = _TimedDeliveryDevice(sim, clock, ack_after=99.0)
    dev._service.delivery_status = lambda _reference: "disconnected"

    sample = probe.run_probe(
        dev,
        FakeReader(sim),
        [200, 500],
        _opts(
            samples=1,
            poll_interval=0.1,
            connect_timeout=0.8,
            verify_output=False,
        ),
        sleep=clock.sleep,
        now=clock.now,
    )[0]

    assert sample.broker_delivery_status == "disconnected"
    assert sample.broker_delivery_from_submit_ms is None


def test_local_submission_duration_is_included_in_every_primary_timeline():
    clock = FakeClock()
    sim = _TimelineInverter(
        clock, setpoint_at=0.5, physical_at=99.0, initial=200
    )
    dev = _SlowSubmissionDevice(sim, clock, ack_after=0.1)

    sample = probe.run_probe(
        dev,
        FakeReader(sim),
        [200, 500],
        _opts(
            samples=1,
            poll_interval=0.1,
            connect_timeout=1.0,
            verify_output=False,
        ),
        sleep=clock.sleep,
        now=clock.now,
    )[0]

    assert sample.local_submit_duration_ms == pytest.approx(200.0)
    assert sample.broker_delivery_from_submit_ms == pytest.approx(300.0)
    assert sample.setpoint_match_from_submit_ms == pytest.approx(700.0)


def test_broker_delivery_status_delivered_vs_untracked():
    clock = FakeClock()
    sim = SimInverter(clock, latency=0.2, initial=200)
    reader = FakeReader(sim)

    delivered = _DeliveryDevice(sim, deliver=True)
    samples = probe.run_probe(
        delivered, reader, [200, 500], _opts(samples=1), sleep=clock.sleep, now=clock.now
    )
    assert samples[0].broker_delivery_status == "delivered"
    assert samples[0].broker_delivery_from_submit_ms is not None
    assert samples[0].local_submit_duration_ms is not None

    sim2 = SimInverter(clock, latency=0.2, initial=200)
    untracked = FakeMqttDevice(sim2)  # no _service -> delivery unobservable
    samples2 = probe.run_probe(
        untracked, FakeReader(sim2), [200, 500], _opts(samples=1),
        sleep=clock.sleep, now=clock.now,
    )
    assert samples2[0].broker_delivery_status == "untracked"
    assert samples2[0].broker_delivery_from_submit_ms is None
