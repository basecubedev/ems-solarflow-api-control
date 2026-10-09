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

pytestmark = [
    pytest.mark.contract,
    pytest.mark.simulation,
]

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


_RENDER_PRELUDE = """
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

function renderCards(devices) {
  context.renderDevices(devices);
  return element("deviceGrid").innerHTML.split("<article").map((part) => "<article" + part);
}
"""


def _run_render_script(checks):
    """Run ``checks`` against the dashboard's own device-card renderer."""

    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for executable dashboard device render test")
    result = subprocess.run(
        [node, "-", str(ROOT / "dashboard/static/app.js")],
        input=_RENDER_PRELUDE + textwrap.dedent(checks),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_frontend_renders_read_only_tile_without_target_and_with_badge():
    _run_render_script(
        """
        const cards = renderCards({
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


def test_frontend_draws_no_charge_level_for_a_device_that_reports_none():
    """A bar at zero would say the battery is empty; there is no battery to show."""

    _run_render_script(
        """
        const tile = {
          online: true,
          read_only: true,
          soc: 0,
          pv_input_w: 0,
          output_w: 765,
          battery_power_w: 0,
          target_w: 0,
          output_limit_w: 0,
          battery_full_charge_assist: assistUnknown,
        };
        const cards = renderCards({
          Garage: { ...tile, soc_reported: false },
          Cellar: { ...tile, soc: 42, soc_reported: true },
          WR1: { ...tile, read_only: false, soc: 60 },
        });

        const garage = cards.find((html) => html.includes("Garage"));
        const cellar = cards.find((html) => html.includes("Cellar"));
        const wr1 = cards.find((html) => html.includes("WR1"));
        assert(garage && cellar && wr1, "all three cards rendered");
        assert(!garage.includes("Battery SOC"), "no charge level without a reported one");
        assert(!garage.includes("data-device-soc-fill"), "no charge bar without a reported one");
        assert(garage.includes(">Output<"), "its output is still shown");
        assert(cellar.includes("Battery SOC") && cellar.includes("42"), "a reported level is drawn");
        assert(wr1.includes("Battery SOC"), "a controlled device keeps its charge level");
        assert(
          !vm.runInContext('state.deviceSocValues.has("Garage")', context),
          "no level is remembered for it",
        );
        assert(vm.runInContext('state.deviceSocValues.has("Cellar")', context), "a reported one is");
        """
    )


def _dashboard(runtime, states=None):
    controller = _controller(["WR1"], {"WR1": True}, runtime=runtime)
    return build_dashboard_snapshot(
        controller,
        load_w=0,
        states=states or [_control_state()],
        targets=[300],
        effective_targets=[300],
        allocated_total_w=300,
        effective_total_w=300,
        enabled=True,
        max_total_power=1600,
        min_output_limit=35,
    )


def test_a_telemetry_only_zendure_device_without_a_charge_level_is_left_out_too():
    runtime = _FakeTelemetryRuntime(
        summaries=[{"name": "INV_2", "identifier": "ID2", "status": "online"}],
        snapshots={"ID2": _snapshot({"outputHomePower": 280})},
    )

    snapshot = _dashboard(runtime)

    assert snapshot["devices"]["INV_2"]["soc_reported"] is False
    assert snapshot["average_soc"] == 60


def test_an_external_inverter_is_a_read_only_tile_like_any_other():
    """An inverter with no local API at all -- read over MQTT from whatever a
    home-automation system republishes -- reaches the cockpit through the very
    same runtime as a telemetry-only Zendure device. One kind of read-only
    tile, and one path to it."""

    external = _FakeTelemetryRuntime(
        summaries=[
            {"name": "Kostal Piko", "identifier": "EXAMPLE0000001", "status": "online"}
        ],
        snapshots={"EXAMPLE0000001": _snapshot({"outputHomePower": 1234})},
    )
    controller = _controller(["WR1"], {"WR1": True}, runtime=external)

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

    tile = snapshot["devices"]["Kostal Piko"]
    assert tile["read_only"] is True
    assert tile["online"] is True
    assert tile["output_w"] == 1234
    # It is not controlled and never carries a target.
    assert tile["target_w"] == 0
    assert tile["capability"] is None
    # Its output is part of what the house produces.
    assert snapshot["inverter_output_w"] == 300 + 1234


def test_both_kinds_of_read_only_device_can_be_present_at_once():
    # One runtime, two kinds of device: a Zendure device with no write method
    # and an inverter with no command path at all.
    combined = _FakeTelemetryRuntime(
        summaries=[
            {"name": "INV_2", "identifier": "ID2", "status": "online"},
            {"name": "Kostal Piko", "identifier": "EXAMPLE0000001", "status": "stale"},
        ],
        snapshots={
            "ID2": _snapshot({"outputHomePower": 280, "solarInputPower": 315}),
            "EXAMPLE0000001": _snapshot({"outputHomePower": 1234}),
        },
    )
    controller = _controller(["WR1"], {"WR1": True}, runtime=combined)

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

    assert set(snapshot["devices"]) == {"WR1", "INV_2", "Kostal Piko"}
    # A stale reading is shown as offline but still counted: the power is real,
    # only the reading is old.
    assert snapshot["devices"]["Kostal Piko"]["online"] is False
    assert snapshot["inverter_output_w"] == 300 + 280 + 1234


def test_the_live_flow_draws_no_charge_level_for_a_device_that_reports_none():
    """Drawn fresh and updated in place, the battery of such a device reads "--"."""

    _run_render_script(
        """
        const layout = vm.runInContext("DEVICE_FLOW_LAYOUT", context);
        const tile = { online: true, read_only: true, soc: 42, output_w: 765, battery_power_w: 0 };
        const drawn = context.deviceFlowRow("Garage", { ...tile, soc_reported: false }, 0, layout, 0);
        const reported = context.deviceFlowRow("Cellar", { ...tile, soc: 0, soc_reported: true }, 0, layout, 0);
        assert(drawn.includes(">--<"), "an unreported level reads as unknown");
        assert(drawn.includes('data-battery-fill-target="0"'), "its bar is drawn empty");
        assert(!drawn.includes(">42%<"), "whatever the device view says");
        assert(!drawn.includes("battery-fill low"), "an unknown level is not drawn as low");
        assert(reported.includes(">0%<"), "a reported zero is still a reading");
        assert(reported.includes("battery-fill low"), "and an empty battery is drawn as low");

        const key = context.deviceFlowKey("Garage", 0);
        function fakeElement(attrs) {
          return {
            attrs,
            textContent: "",
            getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; },
            setAttribute(name, value) { this.attrs[name] = String(value); },
          };
        }
        const socText = fakeElement({ "data-flow-text": `${key}:battery-soc` });
        const fill = fakeElement({ "data-device-battery-fill": "0" });
        const container = {
          querySelectorAll(selector) {
            const attribute = selector.slice(1, -1);
            return [socText, fill].filter((el) => el.getAttribute(attribute) !== null);
          },
        };
        const update = (device) => context.updateDeviceFlowSnapshot(
          container, { home_load_w: 0, grid_power_w: 0 }, [["Garage", device]],
        );

        update({ ...tile, soc_reported: false });
        assert(socText.textContent === "--", `updated text: ${socText.textContent}`);
        assert(fill.attrs["data-battery-fill-target"] === "0", "its bar stays empty");

        update({ ...tile, soc: 0, soc_reported: false });
        assert(!fill.attrs.class.includes("low"), "an unreported level is not drawn as low");

        update({ ...tile, soc: 7 });
        assert(socText.textContent === "7%", `updated text: ${socText.textContent}`);
        assert(fill.attrs.class.includes("low"), "a reported low level is drawn as low");
        """
    )
