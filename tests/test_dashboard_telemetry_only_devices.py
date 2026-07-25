# SPDX-License-Identifier: AGPL-3.0-or-later
"""Telemetry-only Zendure MQTT devices are visible read-only in the dashboard.

A Zendure MQTT device that streams telemetry but is not write-enabled
(``capabilities.write_output_limit`` unset) is excluded from the control loop
and never joins ``controller.devices``. It must still appear in the dashboard as
a read-only tile so a healthy but uncontrolled inverter is never invisible. The
tile carries live telemetry, is flagged ``read_only``, contributes to the
aggregate totals, and never gains a control target.
"""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from dashboard.telemetry import build_dashboard_snapshot

pytestmark = pytest.mark.simulation

ROOT = Path(__file__).resolve().parents[1]


class _FakeTelemetryRuntime:
    def __init__(self, summaries, snapshots):
        self._summaries = summaries
        self._snapshots = snapshots

    def device_summaries(self):
        return self._summaries

    def snapshots(self):
        return self._snapshots


def _snapshot(metrics):
    return SimpleNamespace(metrics=metrics)


def _controller(devices, online, runtime=None):
    return SimpleNamespace(
        devices=[SimpleNamespace(name=name) for name in devices],
        runtime_state=None,
        device_online=online,
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
        zendure_mqtt_runtime=runtime,
    )


def _control_state():
    return SimpleNamespace(solar=400, output=300, pack_out=110, pack_in=10, soc=60)


def test_telemetry_only_device_appears_as_read_only_tile_with_live_values():
    runtime = _FakeTelemetryRuntime(
        summaries=[{"name": "INV_2", "identifier": "ID2", "status": "online"}],
        snapshots={
            "ID2": _snapshot(
                {
                    "electricLevel": 88,
                    "solarInputPower": 315,
                    "outputHomePower": 280,
                    "outputPackPower": 0,
                    "packInputPower": 0,
                    "outputLimit": 300,
                }
            )
        },
    )
    controller = _controller(["WR1"], {"WR1": True}, runtime=runtime)

    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=[_control_state()],
        targets=[300],
        effective_targets=[300],
        allocated_total_w=300,
        effective_total_w=300,
        enabled=True,
        max_total_power=1600,
        min_output_limit=35,
    )

    assert set(snapshot["devices"]) == {"WR1", "INV_2"}
    tile = snapshot["devices"]["INV_2"]
    assert tile["read_only"] is True
    assert tile["online"] is True
    assert tile["soc"] == 88
    assert tile["pv_input_w"] == 315
    assert tile["output_w"] == 280
    assert tile["output_limit_w"] == 300
    assert tile["target_w"] == 0
    assert tile["allocated_target_w"] == 0
    assert tile["capability"] is None
    assert tile["battery_full_charge_assist"]["status"] == "unknown"

    assert "read_only" not in snapshot["devices"]["WR1"]
    assert snapshot["pv_total_w"] == 715
    assert snapshot["inverter_output_w"] == 580
    assert snapshot["average_soc"] == 74.0


def test_no_telemetry_runtime_leaves_control_only_snapshot_unchanged():
    controller = _controller(["WR1"], {"WR1": True}, runtime=None)

    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=[_control_state()],
        targets=[300],
        effective_targets=[300],
        allocated_total_w=300,
        effective_total_w=300,
        enabled=True,
        max_total_power=1600,
        min_output_limit=35,
    )

    assert set(snapshot["devices"]) == {"WR1"}
    assert snapshot["pv_total_w"] == 400


def test_stale_telemetry_device_is_shown_offline_and_unseen_is_skipped():
    runtime = _FakeTelemetryRuntime(
        summaries=[
            {"name": "STALE", "identifier": "S1", "status": "stale"},
            {"name": "UNSEEN", "identifier": "U1", "status": "unseen"},
            {"name": "INVALID", "identifier": None, "status": "invalid"},
        ],
        snapshots={
            "S1": _snapshot({"electricLevel": 50, "solarInputPower": 100, "outputHomePower": 90}),
        },
    )
    controller = _controller(["WR1"], {"WR1": True}, runtime=runtime)

    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=[_control_state()],
        targets=[300],
        effective_targets=[300],
        allocated_total_w=300,
        effective_total_w=300,
        enabled=True,
        max_total_power=1600,
        min_output_limit=35,
    )

    assert set(snapshot["devices"]) == {"WR1", "STALE"}
    assert snapshot["devices"]["STALE"]["online"] is False
    assert snapshot["devices"]["STALE"]["read_only"] is True
    assert snapshot["rules"]["offline_devices"]["active"] is True


def test_telemetry_device_never_shadows_a_control_device_of_the_same_name():
    runtime = _FakeTelemetryRuntime(
        summaries=[{"name": "WR1", "identifier": "ID1", "status": "online"}],
        snapshots={"ID1": _snapshot({"electricLevel": 1, "outputHomePower": 9999})},
    )
    controller = _controller(["WR1"], {"WR1": True}, runtime=runtime)

    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=[_control_state()],
        targets=[300],
        effective_targets=[300],
        allocated_total_w=300,
        effective_total_w=300,
        enabled=True,
        max_total_power=1600,
        min_output_limit=35,
    )

    assert set(snapshot["devices"]) == {"WR1"}
    assert "read_only" not in snapshot["devices"]["WR1"]
    assert snapshot["devices"]["WR1"]["output_w"] == 300


def test_read_only_flag_survives_the_json_snapshot_roundtrip():
    runtime = _FakeTelemetryRuntime(
        summaries=[{"name": "INV_2", "identifier": "ID2", "status": "online"}],
        snapshots={"ID2": _snapshot({"electricLevel": 88, "outputHomePower": 280})},
    )
    controller = _controller(["WR1"], {"WR1": True}, runtime=runtime)

    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=[_control_state()],
        targets=[300],
        effective_targets=[300],
        allocated_total_w=300,
        effective_total_w=300,
        enabled=True,
        max_total_power=1600,
        min_output_limit=35,
    )
    roundtrip = json.loads(json.dumps(snapshot, sort_keys=True))
    assert roundtrip["devices"]["INV_2"]["read_only"] is True


def test_frontend_renders_read_only_tile_without_target_and_with_badge():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for executable dashboard device render test")

    script = textwrap.dedent(
        """
        const fs = require("fs");
        const vm = require("vm");
        const appPath = process.argv[2];
        const source = fs.readFileSync(appPath, "utf8")
          .split('document.querySelectorAll(".range-tabs button")')[0];

        class FakeElement {
          constructor(id = "") {
            this.id = id;
            this.textContent = "";
            this.innerHTML = "";
            this.className = "";
            this.children = [];
            this.dataset = {};
            this.style = { setProperty: () => {} };
          }
          setAttribute() {}
          appendChild(child) { this.children.push(child); }
          querySelectorAll() { return []; }
        }

        const elements = new Map();
        function element(id) {
          if (!elements.has(id)) elements.set(id, new FakeElement(id));
          return elements.get(id);
        }

        const context = {
          console,
          document: {
            getElementById: element,
            createElement: () => new FakeElement(),
            querySelector: () => null,
            querySelectorAll: () => [],
          },
          window: { addEventListener: () => {}, localStorage: null },
        };

        vm.createContext(context);
        vm.runInContext(source, context, { filename: appPath });

        context.readDeviceSocFillWidths = () => new Map();
        context.applyDeviceSocFillStarts = () => {};
        context.animateDeviceSocFills = () => {};

        function assert(condition, message) {
          if (!condition) throw new Error(message);
        }

        const assistUnknown = { status: "unknown", enabled: false };
        context.renderDevices({
          WR1: {
            online: true,
            soc: 60,
            pv_input_w: 400,
            output_w: 300,
            battery_power_w: 100,
            target_w: 260,
            output_limit_w: 300,
            battery_full_charge_assist: assistUnknown,
          },
          INV_2: {
            online: true,
            read_only: true,
            soc: 88,
            pv_input_w: 315,
            output_w: 280,
            battery_power_w: 0,
            target_w: 0,
            output_limit_w: 300,
            battery_full_charge_assist: assistUnknown,
          },
        });

        const cards = element("deviceGrid").children.map((card) => card.innerHTML);
        const wr1 = cards.find((html) => html.includes("WR1"));
        const inv = cards.find((html) => html.includes("INV_2"));
        assert(wr1, "WR1 card rendered");
        assert(inv, "INV_2 card rendered");
        assert(!wr1.includes("Telemetry only"), "controlled device has no telemetry-only badge");
        assert(wr1.includes(">Target<"), "controlled device shows a Target value");
        assert(inv.includes("Telemetry only"), "read-only device shows a telemetry-only badge");
        assert(!inv.includes(">Target<"), "read-only device omits the Target value");
        assert(inv.includes(">Output<"), "read-only device still shows Output telemetry");
        """
    )

    result = subprocess.run(
        [node, "-", str(ROOT / "dashboard/static/app.js")],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
