from pathlib import Path
import shutil
import subprocess
import textwrap
from types import SimpleNamespace

import pytest

from dashboard.telemetry import build_dashboard_snapshot


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_backend_keeps_positive_battery_power_for_charging():
    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="WR1")],
        runtime_state=None,
        device_online={"WR1": True},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
    )
    state = SimpleNamespace(
        solar=500,
        output=700,
        pack_out=220,
        pack_in=20,
        soc=64,
    )

    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=[state],
        targets=[700],
        effective_targets=[700],
        allocated_total_w=700,
        effective_total_w=700,
        enabled=True,
        max_total_power=700,
        min_output_limit=35,
    )

    assert snapshot["battery_power_w"] == 200
    assert snapshot["devices"]["WR1"]["battery_power_w"] == 200
    assert snapshot["devices"]["WR1"]["pack_output_w"] == 220
    assert snapshot["devices"]["WR1"]["pack_input_w"] == 20


def test_dashboard_backend_keeps_negative_battery_power_for_discharging():
    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="WR1")],
        runtime_state=None,
        device_online={"WR1": True},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
    )
    state = SimpleNamespace(
        solar=1400,
        output=700,
        pack_out=10,
        pack_in=710,
        soc=64,
    )

    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=[state],
        targets=[700],
        effective_targets=[700],
        allocated_total_w=700,
        effective_total_w=700,
        enabled=True,
        max_total_power=700,
        min_output_limit=35,
    )

    assert snapshot["battery_power_w"] == -700
    assert snapshot["devices"]["WR1"]["battery_power_w"] == -700


def test_dashboard_backend_snapshot_keeps_two_device_values_and_totals():
    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="WR1"), SimpleNamespace(name="WR2")],
        runtime_state=None,
        device_online={"WR1": True, "WR2": True},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
    )
    states = [
        SimpleNamespace(
            solar=1400,
            output=700,
            pack_out=710,
            pack_in=10,
            soc=63,
        ),
        SimpleNamespace(
            solar=600,
            output=300,
            pack_out=20,
            pack_in=220,
            soc=58,
        ),
    ]

    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=states,
        targets=[700, 300],
        effective_targets=[700, 300],
        allocated_total_w=1000,
        effective_total_w=1000,
        enabled=True,
        max_total_power=1400,
        min_output_limit=35,
    )

    assert set(snapshot["devices"]) == {"WR1", "WR2"}
    assert snapshot["devices"]["WR1"]["pv_input_w"] == 1400
    assert snapshot["devices"]["WR1"]["output_w"] == 700
    assert snapshot["devices"]["WR1"]["battery_power_w"] == 700
    assert snapshot["devices"]["WR1"]["soc"] == 63
    assert snapshot["devices"]["WR2"]["pv_input_w"] == 600
    assert snapshot["devices"]["WR2"]["output_w"] == 300
    assert snapshot["devices"]["WR2"]["battery_power_w"] == -200
    assert snapshot["devices"]["WR2"]["soc"] == 58
    assert snapshot["pv_total_w"] == 2000
    assert snapshot["inverter_output_w"] == 1000
    assert snapshot["battery_power_w"] == 500
    assert snapshot["average_soc"] == 60.5


def test_frontend_uses_normalized_battery_display_semantics():
    app_js = (ROOT / "dashboard/static/app.js").read_text(encoding="utf-8")

    assert "function normalizeBatteryPowerForDisplay(rawBatteryPowerW)" in app_js
    assert "function aggregatedBatteryPowerW(snapshot)" in app_js
    assert "const batteryFlow = normalizeBatteryPowerForDisplay(aggregatedBatteryPowerW(snapshot));" in app_js
    assert "function signedWatts(value)" in app_js
    assert 'setText("metricBattery", signedWatts(batteryFlow.valueW));' in app_js
    assert 'setText("flowBattery", signedWatts(batteryFlow.valueW));' in app_js
    assert "function batteryPipeDirection(batteryFlow)" in app_js
    assert 'setPipe("pipeBatteryInverter", batteryFlow.absW, batteryPipeDirection(batteryFlow));' in app_js
    assert 'setPipe("pipePvInverter", pvPower, "forward");' in app_js
    assert "inverterPvPortOffsetY: 32" in app_js
    assert "inverterBatteryPortOffsetY: 60" in app_js
    assert "sharedHomeGridGapY: 164" in app_js
    assert "const homeY = rowsCenterY - layout.sharedVisualHeight / 2;" in app_js
    assert 'return entries.reduce(' in app_js
    assert '{ id: "chartBattery", title: "Battery", field: "battery_power_w", color: "#f06d6d", unit: "W" }' in app_js
    assert "displayBatteryPower" not in app_js
    assert "invert: true" not in app_js


def test_frontend_battery_flow_scenarios_execute_against_app_js():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for executable dashboard flow semantics test")

    script = textwrap.dedent(
        """
        const fs = require("fs");
        const vm = require("vm");
        const appPath = process.argv[2];
        const source = fs.readFileSync(appPath, "utf8")
          .split('document.querySelectorAll(".range-tabs button")')[0];

        class FakeClassList {
          constructor() { this.values = new Set(); }
          toggle(name, force) {
            if (force) this.values.add(name);
            else this.values.delete(name);
          }
          contains(name) { return this.values.has(name); }
        }

        class FakeElement {
          constructor(id = "") {
            this.id = id;
            this.textContent = "";
            this.innerHTML = "";
            this.className = "";
            this.children = [];
            this.attrs = {};
            this.classList = new FakeClassList();
            this.style = {
              values: {},
              setProperty: (key, value) => { this.style.values[key] = value; },
            };
          }
          setAttribute(key, value) { this.attrs[key] = value; }
          appendChild(child) { this.children.push(child); }
        }

        const elements = new Map();
        function element(id) {
          if (!elements.has(id)) elements.set(id, new FakeElement(id));
          return elements.get(id);
        }
        const flowWrap = new FakeElement("flowWrap");

        const context = {
          console,
          document: {
            getElementById: element,
            createElement: () => new FakeElement(),
            querySelector: (selector) => selector === ".flow-wrap" ? flowWrap : null,
            querySelectorAll: () => [],
          },
          window: { addEventListener: () => {} },
        };

        vm.createContext(context);
        vm.runInContext(source, context, { filename: appPath });

        function assert(condition, message) {
          if (!condition) throw new Error(message);
        }

        function assertBatteryFlow(input, expected) {
          const flow = context.normalizeBatteryPowerForDisplay(input);
          assert(flow.valueW === expected.valueW, `valueW mismatch for ${input}`);
          assert(flow.absW === expected.absW, `absW mismatch for ${input}`);
          assert(flow.state === expected.state, `state mismatch for ${input}`);
          assert(flow.isCharging === expected.isCharging, `isCharging mismatch for ${input}`);
          assert(flow.isDischarging === expected.isDischarging, `isDischarging mismatch for ${input}`);
          assert(flow.isIdle === expected.isIdle, `isIdle mismatch for ${input}`);
        }

        function render(overrides) {
          context.renderSnapshot({
            timestamp: "2026-05-27T12:00:00Z",
            pv_total_w: 500,
            inverter_output_w: 700,
            home_load_w: 700,
            grid_power_w: 0,
            battery_power_w: 0,
            average_soc: 64,
            controller: { enabled: true },
            rules: {},
            devices: {},
            ...overrides,
          });
        }

        assertBatteryFlow(300, {
          valueW: 300,
          absW: 300,
          state: "charging",
          isCharging: true,
          isDischarging: false,
          isIdle: false,
        });
        assertBatteryFlow(-250, {
          valueW: -250,
          absW: 250,
          state: "discharging",
          isCharging: false,
          isDischarging: true,
          isIdle: false,
        });
        assertBatteryFlow(0, {
          valueW: 0,
          absW: 0,
          state: "idle",
          isCharging: false,
          isDischarging: false,
          isIdle: true,
        });
        assertBatteryFlow(null, {
          valueW: 0,
          absW: 0,
          state: "idle",
          isCharging: false,
          isDischarging: false,
          isIdle: true,
        });

        render({ pv_total_w: 1400, battery_power_w: 300 });
        assert(element("flowBattery").textContent === "+300 W", "charging displays signed positive battery watts");
        assert(element("metricBattery").textContent === "+300 W", "metric charging display is positive");
        assert(element("flowBatteryState").textContent === "Charging", "positive power is charging");
        assert(element("visualBattery").classList.contains("charging"), "charging class is active");
        assert(!element("visualBattery").classList.contains("discharging"), "discharging class is not active while charging");
        assert(element("pipeBatteryInverter").classList.contains("reverse"), "charging pipe flows inverter to battery");
        assert(element("pipePvInverter").classList.contains("active"), "PV pipe is active above threshold");
        assert(!element("pipePvInverter").classList.contains("reverse"), "PV pipe remains PV to inverter");

        render({ pv_total_w: 500, battery_power_w: -250 });
        assert(element("flowBattery").textContent === "-250 W", "discharging displays signed negative battery watts");
        assert(element("metricBattery").textContent === "-250 W", "metric discharging display is negative");
        assert(element("flowBatteryState").textContent === "Discharging", "negative power is discharging");
        assert(!element("visualBattery").classList.contains("charging"), "charging class is not active while discharging");
        assert(element("visualBattery").classList.contains("discharging"), "discharging class is active");
        assert(!element("pipeBatteryInverter").classList.contains("reverse"), "discharging pipe flows battery to inverter");
        assert(element("pipeBatteryInverter").classList.contains("active"), "battery pipe is active while discharging");
        assert(element("pipePvInverter").classList.contains("active"), "PV pipe stays active for PV above threshold");

        render({ battery_power_w: 0 });
        assert(element("flowBattery").textContent === "0 W", "idle displays zero battery watts");
        assert(element("flowBatteryState").textContent === "Idle", "zero power is idle");
        assert(!element("visualBattery").classList.contains("charging"), "charging class is not active while idle");
        assert(!element("visualBattery").classList.contains("discharging"), "discharging class is not active while idle");
        assert(!element("pipeBatteryInverter").classList.contains("active"), "battery pipe is not active while idle");

        render({
          battery_power_w: 999,
          devices: {
            WR1: { soc: 71, pv_input_w: 400, output_w: 100, battery_power_w: 300 },
            WR2: { soc: 52, pv_input_w: 200, output_w: 100, battery_power_w: 100 },
          },
        });
        assert(element("flowBattery").textContent === "+400 W", "aggregated positive devices sum to charging value");
        assert(element("flowBatteryState").textContent === "Charging", "positive aggregate is charging");

        render({
          devices: {
            WR1: { soc: 71, pv_input_w: 100, output_w: 300, battery_power_w: -250 },
            WR2: { soc: 52, pv_input_w: 100, output_w: 250, battery_power_w: -150 },
          },
        });
        assert(element("flowBattery").textContent === "-400 W", "aggregated negative devices sum to discharging value");
        assert(element("flowBatteryState").textContent === "Discharging", "negative aggregate is discharging");

        render({
          devices: {
            WR1: { soc: 71, pv_input_w: 900, output_w: 600, battery_power_w: 300 },
            WR2: { soc: 52, pv_input_w: 500, output_w: 600, battery_power_w: -100 },
          },
        });
        assert(element("flowBattery").textContent === "+200 W", "mixed devices can still aggregate to charging");
        assert(element("flowBatteryState").textContent === "Charging", "positive mixed aggregate is charging");

        render({
          devices: {
            WR1: { soc: 71, pv_input_w: 200, output_w: 100, battery_power_w: 100 },
            WR2: { soc: 52, pv_input_w: 100, output_w: 200, battery_power_w: -100 },
          },
        });
        assert(element("flowBattery").textContent === "0 W", "balanced devices aggregate to zero");
        assert(element("flowBatteryState").textContent === "Idle", "zero aggregate is idle");
        assert(!element("pipeBatteryInverter").classList.contains("active"), "balanced aggregate has no active battery animation");

        context.setFlowView("devices");
        assert(element("flowSvg").hidden === true, "aggregated SVG is hidden in device view");
        assert(element("deviceFlowView").hidden === false, "device view is visible after switching");
        assert(flowWrap.classList.contains("view-devices"), "device class is applied to the flow board");

        render({
          home_load_w: 450,
          grid_power_w: -120,
          devices: {
            WR1: { soc: 71, pv_input_w: 1400, output_w: 700, battery_power_w: 700 },
            WR2: { soc: 52, pv_input_w: 500, output_w: 700, battery_power_w: -200 },
          },
        });
        const deviceHtml = element("deviceFlowView").innerHTML;
        const deviceRows = [...deviceHtml.matchAll(/class="device-flow-device"/g)].length;
        const sharedHomes = [...deviceHtml.matchAll(/class="device-flow-shared-home/g)].length;
        const pipeGroups = [...deviceHtml.matchAll(/class="energy-pipe/g)].length;
        assert(deviceRows === 2, "exactly two device SVG groups are rendered for WR1 and WR2");
        assert(sharedHomes === 1, "one shared home/grid module is rendered");
        assert(pipeGroups === 7, "each device renders PV, battery and output pipes plus one shared grid pipe");
        assert(deviceHtml.includes('class="device-flow-svg"'), "device view renders an SVG board");
        assert(deviceHtml.includes("device-visual"), "device modules reuse aggregated visual classes");
        assert(deviceHtml.includes("visual-shell"), "device modules reuse visual shells");
        assert(deviceHtml.includes("visual-icon-bay"), "device modules reuse icon bays");
        assert(deviceHtml.includes("visual-label"), "device modules reuse visual labels");
        assert(deviceHtml.includes("visual-value"), "device modules reuse visual values");
        assert(deviceHtml.includes("visual-state"), "battery module reuses visual state text");
        assert(deviceHtml.includes("pipe-base"), "device pipes render base layer");
        assert(deviceHtml.includes("pipe-glow"), "device pipes render glow layer");
        assert(deviceHtml.includes("pipe-energy"), "device pipes render animated energy layer");
        assert(deviceHtml.includes('d="M228 66 H292 V136 H342"'), "first device PV pipe uses the upper inverter port");
        assert(deviceHtml.includes('d="M228 216 H292 V164 H342"'), "first device battery pipe uses the lower inverter port");
        assert(!deviceHtml.includes("device-flow-pipe"), "simplified device pipe class is not used");
        assert(deviceHtml.includes("WR1 PV"), "charging device is rendered");
        assert(deviceHtml.includes("Charging"), "charging state is rendered in device view");
        assert(deviceHtml.includes("+700 W"), "charging display is positive in device view");
        assert(deviceHtml.includes("WR2 PV"), "discharging device is rendered");
        assert(deviceHtml.includes("Discharging"), "discharging state is rendered in device view");
        assert(deviceHtml.includes("-200 W"), "discharging display is negative in device view");
        assert(deviceHtml.includes("450 W"), "device view renders the shared home value");
        assert(deviceHtml.includes("-120 W"), "device view renders the shared grid value");
        assert(deviceHtml.includes("Export"), "device view renders grid export state");
        assert(deviceHtml.includes("Grid"), "device view renders a grid module");
        assert(!deviceHtml.includes("Shared"), "device view no longer uses placeholder home text");
        assert(element("deviceFlowView").hidden === false, "refresh keeps current device view visible");

        render({
          devices: [
            { device: "WR-A", soc_percent: 70, pv_power_w: 100, inverter_output_w: 90, battery_power_w: 0 },
            { device: "WR-B", soc_percent: 74, pv_power_w: 200, inverter_output_w: 190, battery_power_w: -10 },
            { device: "WR-C", soc_percent: 77, pv_power_w: 300, inverter_output_w: 290, battery_power_w: 10 },
          ],
        });
        const arrayDeviceHtml = element("deviceFlowView").innerHTML;
        const arrayDeviceRows = [...arrayDeviceHtml.matchAll(/class="device-flow-device"/g)].length;
        assert(arrayDeviceRows === 3, "array devices render dynamically beyond two inverters");
        assert(arrayDeviceHtml.includes("WR-A PV"), "array device A is rendered");
        assert(arrayDeviceHtml.includes("WR-B PV"), "array device B is rendered");
        assert(arrayDeviceHtml.includes("WR-C PV"), "array device C is rendered");

        render({ devices: {} });
        assert(element("deviceFlowView").innerHTML.includes("No per-device telemetry available."), "empty devices do not crash");

        context.setFlowView("aggregated");
        assert(element("flowSvg").hidden === false, "aggregated SVG is visible after switching back");
        assert(element("deviceFlowView").hidden === true, "device view is hidden after switching back");
        assert(flowWrap.classList.contains("view-aggregated"), "aggregated class is restored");
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


def test_battery_fill_has_valid_svg_fill_css():
    styles = (ROOT / "dashboard/static/styles.css").read_text(encoding="utf-8")

    assert ".battery-fill { fill: var(--battery);" in styles
    assert ".battery-fill { fill: linear-gradient(" not in styles
    assert "device-flow-pipe" not in styles


def test_device_flow_mobile_layout_does_not_force_horizontal_scroll():
    styles = (ROOT / "dashboard/static/styles.css").read_text(encoding="utf-8")

    assert "overflow-x: auto" not in styles
    assert "min-width: 820px" not in styles
    assert "min-width: 840px" not in styles
    assert "max-width: 100%;" in styles
    assert ".device-flow-view { padding: 6px; overflow-x: hidden; }" in styles
    assert ".device-flow-svg { min-width: 0; width: 100%; }" in styles
