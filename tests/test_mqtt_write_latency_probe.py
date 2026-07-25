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
    source = "zendure_cloud_mqtt"
    control_gate = "mqtt_zendure"
    broker_ref = "zendure_cloud"
    hardware_profile = "solarflow_800_pro_2"
    max_power = 800

    def __init__(self, sim):
        self.sim = sim
        self.property_writes = []

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
        self.sim.command(json.loads(message.payload)["properties"])
        return SimpleNamespace(accepted=True, mid=1)

    def write_properties(self, properties, *, reason, **_kwargs):
        self.property_writes.append((dict(properties), reason))
        self.sim.command(dict(properties))
        return True

    def describe(self):
        return {"power_write_profile": "zensdk_properties_write"}


class FakeReader:
    ip = "192.168.1.50"
    name = "inv-http"

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
    assert all(s.publish_ok and not s.timed_out for s in samples)
    for s in samples:
        assert s.matched_target
        assert s.mode_ok is True
        assert s.latency_ms == pytest.approx(500.0)

    stats = probe.summarize(samples, _opts(samples=3))
    assert stats["landed"] == 3
    assert stats["latency_p50_ms"] == pytest.approx(500.0)
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
    assert any("wrote 500W" in line for line in lines)
    assert any("landed" in line for line in lines)


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
    assert samples[0].latency_ms is None


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
    sim = SimInverter(clock, latency=0.1, initial=200)
    dev = FakeMqttDevice(sim)
    reader = FakeReader(sim)
    sim.state["outputHomePower"] = 490
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
    assert result["result"] == "mode_and_setpoint_landed"
    assert sim.state["acMode"] == 2
    assert sim.state["outputLimit"] == 200


# --- full-state restore ------------------------------------------------------


def test_restore_writes_and_verifies_the_complete_initial_state():
    clock = FakeClock()
    sim = SimInverter(clock, latency=0.2, initial=500)
    dev = FakeMqttDevice(sim)
    reader = FakeReader(sim)
    initial = {"smartMode": 0, "acMode": 1, "outputLimit": 0, "inputLimit": 0}
    report = probe.restore_initial_state(
        dev, reader, initial, _opts(), sleep=clock.sleep, now=clock.now
    )
    assert report["restored"] is True
    assert report["verified"] is True
    assert dev.property_writes == [(initial, "probe_restore")]
    assert sim.state["smartMode"] == 0
    assert sim.state["acMode"] == 1


def test_restore_reports_failed_verification_honestly():
    clock = FakeClock()
    sim = SimInverter(clock, latency=999.0, initial=500)
    dev = FakeMqttDevice(sim)
    reader = FakeReader(sim)
    report = probe.restore_initial_state(
        dev, reader, {"outputLimit": 0}, _opts(timeout=1.0),
        sleep=clock.sleep, now=clock.now,
    )
    assert report["restored"] is True
    assert report["verified"] is False


def test_initial_restore_state_captures_only_reported_numeric_properties():
    session = FakeSession(
        report={"properties": {"smartMode": 0, "acMode": 1, "outputLimit": 100, "electricLevel": 60}}
    )
    captured = probe.initial_restore_state("10.0.0.5", session)
    assert captured == {"smartMode": 0, "acMode": 1, "outputLimit": 100}


# --- preview -----------------------------------------------------------------


def test_preview_shows_topic_qos_properties_and_no_secrets(capsys):
    clock = FakeClock()
    sim = SimInverter(clock, latency=0.1, initial=200)
    dev = FakeMqttDevice(sim)
    reader = FakeReader(sim)
    gate = SimpleNamespace(gate_name="allow_mqtt_zendure_control_writes", gate_enabled=True, blocked_by=())
    initial = {"smartMode": 1, "acMode": 2, "outputLimit": 200, "inputLimit": 0}
    probe.print_preview(dev, reader, [200, 500], gate, initial)
    out = capsys.readouterr().out
    assert "qos=1" in out
    assert "retain=False" in out
    assert "iot/PK/SN123/properties/write" in out
    assert "'smartMode': 1" in out
    assert "restore plan" in out
    assert "password" not in out.lower()


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


def _mqtt_device(name, sn):
    return SimpleNamespace(name=name, sn=sn)


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
    assert "no MQTT control device has serial SN123" in err


def test_resolve_by_api_ip_errors_when_api_unreachable():
    runtime = FakeRuntime([_mqtt_device("INV_2", "SN123")])
    session = FakeSession(raise_exc=True)
    dev, reader, serial, err = probe.resolve_by_api_ip(runtime, session, "10.0.0.9", None)
    assert dev is None
    assert "could not read http://10.0.0.9/properties/report" in err


def test_reports_render_without_error():
    clock = FakeClock()
    sim = SimInverter(clock, latency=0.4, initial=200)
    dev = FakeMqttDevice(sim)
    reader = FakeReader(sim)
    opts = _opts(samples=2)
    samples = probe.run_probe(dev, reader, [200, 500], opts, sleep=clock.sleep, now=clock.now)
    stats = probe.summarize(samples, opts)

    text = probe.format_text_report(dev, reader, [200, 500], samples, stats)
    assert "latency ms" in text
    md = probe.format_markdown_report(dev, reader, [200, 500], stats)
    assert "| Latency p50 |" in md
