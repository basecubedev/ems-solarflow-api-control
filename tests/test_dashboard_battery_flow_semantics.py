import json
from pathlib import Path
import shutil
import subprocess
import textwrap
from types import SimpleNamespace

import pytest

from dashboard.telemetry import build_dashboard_snapshot
from ems.target_control import (
    ControlExplanation,
    ControlLimitExplanation,
    DeviceControlExplanation,
)


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
    assert snapshot["control_explain"] is None


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


def test_dashboard_snapshot_publishes_control_explanation_data():
    explanation = ControlExplanation(
        mode="pv_first",
        requested_total_w=1000,
        effective_target_total_w=1000,
        allocated_target_total_w=1000,
        commanded_total_w=1000,
        devices={
            "WR1": DeviceControlExplanation(
                device="WR1",
                online=True,
                pv_input_w=1400,
                output_w=700,
                soc=63,
                min_soc=15,
                max_soc=100,
                pv_priority_factor=1.2,
                allocated_target_w=700,
                effective_target_w=700,
                decision_reason="pv_first_allocation",
            ),
            "WR2": DeviceControlExplanation(
                device="WR2",
                online=True,
                pv_input_w=600,
                output_w=300,
                soc=58,
                min_soc=15,
                max_soc=100,
                pv_priority_factor=1.0,
                allocated_target_w=300,
                effective_target_w=300,
                decision_reason="pv_first_allocation",
            ),
        },
        limits=[
            ControlLimitExplanation(
                name="pv_surplus_available",
                active=True,
                value=2000,
                reason="total PV can cover the requested output",
            )
        ],
    )
    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="WR1"), SimpleNamespace(name="WR2")],
        runtime_state=None,
        device_online={"WR1": True, "WR2": True},
        commanded_total_w=1000,
        filtered_load_w=0,
        _dashboard_capabilities=[],
        last_control_explanation=explanation,
    )
    states = [
        SimpleNamespace(solar=1400, output=700, pack_out=710, pack_in=10, soc=63),
        SimpleNamespace(solar=600, output=300, pack_out=20, pack_in=220, soc=58),
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
    payload = json.loads(json.dumps(snapshot))

    assert payload["pv_total_w"] == 2000
    assert payload["devices"]["WR1"]["target_w"] == 700
    assert payload["control_explain"]["mode"] == "pv_first"
    assert payload["control_explain"]["requested_total_w"] == 1000
    assert set(payload["control_explain"]["devices"]) == {"WR1", "WR2"}
    assert payload["control_explain"]["devices"]["WR1"]["pv_input_w"] == 1400
    assert payload["control_explain"]["devices"]["WR1"]["soc"] == 63
    assert (
        payload["control_explain"]["devices"]["WR1"]["pv_priority_factor"]
        == 1.2
    )
    assert payload["control_explain"]["devices"]["WR1"]["allocated_target_w"] == 700
    assert payload["control_explain"]["devices"]["WR2"]["effective_target_w"] == 300
    assert payload["control_explain"]["limits"][0]["name"] == "pv_surplus_available"


def test_frontend_uses_normalized_battery_display_semantics():
    app_js = (ROOT / "dashboard/static/app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "dashboard/static/index.html").read_text(encoding="utf-8")

    assert "function normalizeBatteryPowerForDisplay(rawBatteryPowerW)" in app_js
    assert "function aggregatedBatteryPowerW(snapshot)" in app_js
    assert "function renderControlExplain(snapshot)" in app_js
    assert "data-flow-view=\"control\"" in index_html
    assert "id=\"controlExplainView\"" in index_html
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


def test_frontend_control_explain_view_executes_against_app_js():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for executable dashboard control view test")

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
          add(name) { this.values.add(name); }
          remove(name) { this.values.delete(name); }
          contains(name) { return this.values.has(name); }
        }

        class FakeElement {
          constructor(id = "") {
            this.id = id;
            this.dataset = {};
            this.textContent = "";
            this.innerHTML = "";
            this.hidden = false;
            this.children = [];
            this.attrs = {};
            this.listeners = {};
            this.classList = new FakeClassList();
            this.style = { setProperty: () => {} };
          }
          setAttribute(key, value) { this.attrs[key] = value; }
          addEventListener(type, handler) { this.listeners[type] = handler; }
          appendChild(child) { this.children.push(child); }
          click() {
            if (this.listeners.click) this.listeners.click();
          }
        }

        const elements = new Map();
        function element(id) {
          if (!elements.has(id)) elements.set(id, new FakeElement(id));
          return elements.get(id);
        }

        const flowWrap = new FakeElement("flowWrap");
        const flowButtons = ["aggregated", "devices", "control"].map((view) => {
          const button = new FakeElement(`${view}Button`);
          button.dataset.flowView = view;
          return button;
        });

        const context = {
          console,
          document: {
            getElementById: element,
            createElement: () => new FakeElement(),
            querySelector: (selector) => selector === ".flow-wrap" ? flowWrap : null,
            querySelectorAll: (selector) => selector === "[data-flow-view]" ? flowButtons : [],
          },
          window: {
            addEventListener: () => {},
            localStorage: {
              value: "aggregated",
              getItem: () => "aggregated",
              setItem: (_key, value) => { context.window.localStorage.value = value; },
            },
          },
        };

        vm.createContext(context);
        vm.runInContext(source, context, { filename: appPath });

        function assert(condition, message) {
          if (!condition) throw new Error(message);
        }

        function render(controlExplain) {
          context.renderSnapshot({
            timestamp: "2026-05-27T12:00:00Z",
            pv_total_w: 1800,
            inverter_output_w: 620,
            home_load_w: 620,
            grid_power_w: 0,
            battery_power_w: 0,
            average_soc: 61,
            controller: { enabled: true },
            rules: {},
            devices: {},
            control_explain: controlExplain,
          });
        }

        context.initFlowViewSwitch();
        flowButtons.find((button) => button.dataset.flowView === "control").click();

        assert(element("flowSvg").hidden === true, "aggregated view is hidden after Control click");
        assert(element("deviceFlowView").hidden === true, "device flow view is hidden after Control click");
        assert(element("controlExplainView").hidden === false, "control view is visible after Control click");
        assert(flowWrap.classList.contains("view-control"), "control class is applied to the flow board");
        assert(flowButtons[2].attrs["aria-selected"] === "true", "Control tab is selected");

        render({
          mode: "pv_first",
          filtered_load_w: -5,
          requested_total_w: 620,
          effective_target_total_w: 620,
          allocated_target_total_w: 620,
          commanded_total_w: 620,
          max_total_power_w: 1400,
          min_output_limit_w: 35,
          undistributed_target_w: 0,
          limits: [
            { name: "pv_surplus_available", active: true, value: 1800, reason: "total PV can cover the requested output" },
            { name: "device_output_limits", active: false, value: "[260, 360]", reason: "device targets fit within capability limits" },
          ],
          notes: ["test context note"],
            devices: {
            WR1: {
              pv_input_w: 1200,
              output_w: 250,
              output_limit_w: 250,
              soc: 64,
              min_soc: 15,
              max_soc: 100,
              max_output_w: 800,
              pv_only_limit_w: 1180,
              pv_weight: 1416,
              pv_priority_factor: 1.2,
              capacity_weight: 0.49,
              charge_balance_multiplier: 1.0,
              soc_gap_percent: 6,
              raw_target_w: 270,
              allocated_target_w: 260,
              effective_target_w: 260,
              adjustment_delta_w: -10,
              decision_reason: "PV priority applied",
              limiting_reason: "pv_only_limit",
              write_decision: "send",
              write_reason: "output_limit_update",
              command_target_w: 260,
              deadband_reference_w: 250,
              deadband_w: 5,
            },
            WR2: {
              pv_input_w: 600,
              output_w: 360,
              output_limit_w: 360,
              soc: 58,
              min_soc: 15,
              max_soc: 100,
              max_output_w: 800,
              pv_only_limit_w: 590,
              pv_weight: 590,
              pv_priority_factor: 1.0,
              capacity_weight: 0.46,
              charge_balance_multiplier: 1.0,
              soc_gap_percent: 6,
              raw_target_w: 350,
              allocated_target_w: 360,
              effective_target_w: 360,
              adjustment_delta_w: 10,
              decision_reason: "Remaining demand assigned",
              write_decision: "skip",
              write_reason: "deadband",
              command_target_w: 360,
              deadband_reference_w: 360,
              deadband_w: 5,
            },
          },
        });

        const html = element("controlExplainView").innerHTML;
        const expectedPipelineStages = [
          "Measurements",
          "Target",
          "Distribution",
          "Limits / Gates",
          "Commands",
          "Result",
        ];
        let previousStageIndex = -1;
        for (const stage of expectedPipelineStages) {
          const stageIndex = html.indexOf(stage);
          assert(stageIndex > previousStageIndex, `${stage} appears in global pipeline order`);
          previousStageIndex = stageIndex;
        }
        const firstDeviceIndex = html.indexOf('data-control-device="WR1"');
        const expectedDeviceStages = [
          "Inputs",
          "Weighting",
          "Raw Target",
          "Adjustments / Limits",
          "Final Target",
        ];
        previousStageIndex = firstDeviceIndex;
        for (const stage of expectedDeviceStages) {
          const stageIndex = html.indexOf(stage, previousStageIndex);
          assert(stageIndex > previousStageIndex, `${stage} appears in per-device decision order`);
          previousStageIndex = stageIndex;
        }
        const deviceFlows = [...html.matchAll(/data-control-device=/g)].length;
        assert(deviceFlows === 2, "WR1 and WR2 each render one decision flow");
        assert(html.includes("control-decision-board"), "control view renders a decision board");
        assert(html.includes("control-stage-row"), "control stages are grouped into rail rows");
        assert([...html.matchAll(/class="control-flow-rail"/g)].length === 1, "only the global row renders a flow rail");
        assert([...html.matchAll(/class="control-flow-node"/g)].length === 6, "global flow rail has one node per global stage");
        assert(html.includes("control-global-pipeline"), "global decision pipeline is rendered");
        assert([...html.matchAll(/control-pipeline-stage/g)].length === 6, "global pipeline renders six stages");
        assert([...html.matchAll(/class="control-result control-stage-result/g)].length === 16, "every global and device stage has one primary result");
        assert([...html.matchAll(/class="control-stage-step"/g)].length === 16, "every stage header has a visible step number");
        assert(html.includes('<span class="control-stage-step">01</span>'), "stage numbering starts at 01");
        assert(html.includes('<span class="control-stage-step">05</span>'), "device flow includes the fifth step");
        assert(html.includes('class="control-stage-title"'), "stage titles use the stronger title class");
        assert(!html.includes("control-result-divider"), "uneven result divider is no longer rendered");
        assert(html.includes("control-stage-subtitle"), "stage headers include explanatory subtitles");
        assert(html.includes("control-context-rail"), "context rail is rendered separately");
        assert(!html.includes("control-color-legend"), "control color legend is no longer rendered");
        assert(!html.includes("PV / Solar"), "legend text is removed after reducing semantic fact colors");
        assert(html.includes("control-device-panel-measurements"), "device measurements are separated");
        assert(html.includes("control-device-panel-context"), "device context is separated");
        assert([...html.matchAll(/class="control-device-reason/g)].length === 2, "each device renders one decision reason note");
        assert(html.includes("control-device-decision-flow"), "per-device decision chain is rendered");
        assert(!html.includes("control-summary-flow"), "old flat global summary is not rendered");
        assert(html.includes("control-flow"), "per-device decision chain is rendered");
        assert(html.includes("WR1"), "WR1 explanation is rendered");
        assert(html.includes("WR2"), "WR2 explanation is rendered");
        assert(html.includes("Filtered load"), "filtered load appears in measurement stage");
        assert(html.includes("PV total"), "PV total appears in measurement stage");
        assert(html.includes("Output total"), "output total appears in measurement stage");
        assert(html.includes("Demand basis"), "measurement handoff result is rendered");
        assert(html.includes("Requested"), "requested total label is rendered");
        assert(html.includes("Effective target"), "target handoff result is rendered");
        assert(html.includes("620 W"), "requested and effective totals are visible");
        assert(html.includes("Write gate"), "write gate summary is visible");
        assert(html.includes("Commandable total"), "limits/gates handoff result is rendered");
        assert(html.includes("Command decision"), "command handoff result is rendered");
        assert(html.includes("Context"), "configuration context is visible");
        assert(html.includes("Max power"), "max power is visible in context/gates");
        assert(html.includes("Min output"), "min output is visible in context/gates");
        assert(html.includes("Inactive gates"), "inactive limits are visible in context");
        assert(html.includes("test context note"), "notes are visible in context");
        assert(html.includes("Distribution"), "distribution stage is visible");
        assert(html.includes("WR1: 260 W"), "WR1 split summary is visible");
        assert(html.includes("WR2: 360 W"), "WR2 split summary is visible");
        assert(html.includes("Share"), "share is visible inside raw target stage");
        assert(html.includes("Raw Target"), "raw target stage is visible");
        assert(html.includes("Adjustments / Limits"), "adjustments stage is visible");
        assert(html.includes("Final Target"), "final target stage is visible");
        assert(!html.includes("Write Decision"), "write decision is merged into final target stage");
        assert(html.includes("Input state"), "device measurement result is visible");
        assert(html.includes("Effective weight"), "weighting result is visible");
        assert(html.includes("Raw target"), "raw target result is visible");
        assert(html.includes("Adjusted target"), "adjustment result is visible");
        assert(html.includes("Final / write"), "final/write result is visible");
        assert(html.includes("Result / Demand basis"), "global result label clarifies handoff");
        assert(html.includes("Result / Final / write"), "device final/write result label clarifies handoff");
        assert(html.includes("Live values define the demand basis"), "global subtitle explains the first stage");
        assert(html.includes("Adjusted target and write gate finish the decision"), "final subtitle explains the merged final stage");
        assert(html.includes("43.5%"), "WR1 share percentage is visible");
        assert(html.includes("620 W x 43.5% = 270 W"), "raw allocation formula is visible");
        assert(html.includes("260 W"), "WR1 allocated/effective target is visible");
        assert(html.includes("360 W"), "WR2 allocated/effective target is visible");
        assert(html.includes("-10 W"), "WR1 adjustment delta is visible");
        assert(html.includes("+10 W"), "WR2 adjustment delta is visible");
        assert(html.includes("pv only limit"), "limiting reason is visible");
        assert(html.includes("PV priority applied"), "WR1 decision reason is visible");
        assert(html.includes("Remaining demand assigned"), "WR2 decision reason is visible");
        assert(html.includes("control-device-reason tone-neutral"), "device reasons stay visually secondary");
        assert(html.includes("Send"), "send write decision is visible");
        assert(html.includes("No write"), "skip/no-write decision is visible");
        assert(html.includes("tone-send"), "send write decision uses send tone");
        assert(html.includes("tone-skip"), "skip write decision uses neutral skip tone");
        assert(!html.includes("tone-warn\\\">No write"), "skip/no-write is not classified as warn");
        assert(html.includes("role-solar"), "PV facts use solar role styling");
        assert(html.includes("role-battery"), "SOC and battery facts use battery role styling");
        assert(html.includes("role-output"), "target and output facts use output role styling");
        assert(html.includes("role-neutral"), "neutral command/gate facts stay calm");
        assert(html.includes("role-config"), "configuration values use secondary role styling");
        assert(!html.includes("role-config control-result"), "configuration values are not primary results");
        assert(html.includes("output limit update"), "write reason is visible");
        assert(html.includes("deadband"), "deadband reason is visible");
        assert(html.includes("1.20x"), "PV priority factor is formatted");
        assert(!html.includes("undefined"), "missing optional fields are hidden");
        assert(!html.includes("null"), "null optional fields are hidden");
        assert(element("controlExplainView").hidden === false, "refresh keeps Control visible");
        assert(element("deviceFlowView").hidden === true, "device flow remains hidden");

        render({
          mode: "pv_first",
          requested_total_w: 260,
          effective_target_total_w: 260,
          allocated_target_total_w: 260,
          devices: {
            WR1: {
              online: true,
              pv_input_w: 1200,
              output_w: 250,
              soc: 64,
              raw_target_w: 260,
              allocated_target_w: 260,
              effective_target_w: 260,
              write_decision: "send",
              write_reason: "output_limit_update",
              command_target_w: 260,
            },
            WR2: {
              online: false,
              pv_input_w: 0,
              output_w: 0,
              soc: 58,
              raw_target_w: 0,
              allocated_target_w: 0,
              effective_target_w: 0,
              write_decision: "blocked",
              write_reason: "offline",
              command_target_w: 0,
            },
          },
        });
        const blockedHtml = element("controlExplainView").innerHTML;
        assert(blockedHtml.includes("Blocked"), "blocked write decision is visible");
        assert(blockedHtml.includes("tone-blocked"), "blocked write decision uses blocked tone");
        assert(blockedHtml.includes("Target is distributed according to device weight, SOC, PV availability, and configured limits."), "missing decision reason falls back gracefully");
        assert(blockedHtml.includes("WR2 is blocked because offline."), "blocked devices get a concrete reason");

        assert(context.demoModeFromSearch("?demo=1") === true, "demo=1 activates demo mode");
        assert(context.demoModeFromSearch("?demo=true") === true, "demo=true activates demo mode");
        assert(context.demoModeFromSearch("?demo=0") === false, "demo=0 does not activate demo mode");
        assert(context.demoModeFromSearch("") === false, "demo mode is not enabled by default");

        const demo = context.demoSnapshot();
        const demoDevices = Object.keys(demo.devices);
        assert(demoDevices.length === 2, "demo renders exactly two devices");
        assert(demoDevices[0] === "WR1" && demoDevices[1] === "WR2", "demo devices are WR1 and WR2");
        assert(demo.pv_total_w <= 2000, "demo PV total respects 2 kWp limit");
        assert(demo.inverter_output_w <= 800, "demo output respects 800 W system limit");
        assert(demo.home_load_w <= 800, "demo home load is capped at 800 W");
        assert(demo.battery_power_w > 0, "demo preserves frontend battery sign semantics for charging");
        assert(demo.control_explain.max_total_power_w === 800, "demo control explain carries the 800 W cap");
        assert(demo.control_explain.devices.WR1.decision_reason.includes("stronger PV"), "demo WR1 has a clear decision reason");
        assert(demo.control_explain.devices.WR2.decision_reason.includes("carries more house load"), "demo WR2 has a clear decision reason");

        context.renderSnapshot(demo);
        const demoHtml = element("controlExplainView").innerHTML;
        assert(element("metricPv").textContent === "1.85 kW", "demo renders aggregated PV metric");
        assert(element("metricHome").textContent === "800 W", "demo renders aggregated home metric");
        assert(element("flowBatteryState").textContent === "Charging", "demo renders charging battery flow");
        assert(element("deviceFlowView").innerHTML.includes("WR1 PV"), "demo renders WR1 in device view");
        assert(element("deviceFlowView").innerHTML.includes("WR2 PV"), "demo renders WR2 in device view");
        assert(!demoHtml.includes("control-color-legend"), "demo control view does not render the removed legend");
        assert(demoHtml.includes("WR1 has stronger PV"), "demo control view renders WR1 reason");
        assert(demoHtml.includes("WR2 carries more house load"), "demo control view renders WR2 reason");
        assert(demoHtml.includes("Result / Final total"), "demo control view renders global result handoff boxes");
        assert(demoHtml.includes("Result / Final / write"), "demo control view renders device final/write handoff boxes");
        assert(!demoHtml.includes("undefined"), "demo control view has no undefined values");

        render(null);
        assert(
          element("controlExplainView").innerHTML.includes("No control explanation data available yet."),
          "missing control explanation shows fallback"
        );

        context.setFlowView("aggregated");
        assert(element("flowSvg").hidden === false, "aggregated view is visible after switching back");
        assert(element("controlExplainView").hidden === true, "control view hides after switching back");
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
    assert "overflow-y: auto" not in styles
    assert "max-height: 560px" not in styles
    assert "min-width: 820px" not in styles
    assert "min-width: 840px" not in styles
    assert "max-width: 100%;" in styles
    assert ".device-flow-view { padding: 6px; overflow-x: hidden; }" in styles
    assert ".device-flow-svg { min-width: 0; width: 100%; }" in styles
    assert ".flow-wrap.view-control { overflow: visible; }" in styles
    assert ".control-global-pipeline" in styles
    assert ".control-flow-rail" in styles
    assert ".control-flow-node" in styles
    assert ".control-stage-step" in styles
    assert ".control-stage-header" in styles
    assert ".role-solar" in styles
    assert ".role-battery" in styles
    assert ".role-output" in styles
    assert ".role-grid" in styles
    assert "var(--pv)" in styles
    assert "var(--battery)" in styles
    assert "var(--output)" in styles
    assert "var(--grid)" in styles
    assert ".control-result-divider" not in styles
    assert "@keyframes controlRailSweep" in styles
    assert "@keyframes controlRailFlow" not in styles
    assert "@keyframes controlResultBorderFlow" in styles
    assert "height: 58px;" in styles
    assert ".control-context-rail" in styles
    assert ".control-color-legend" not in styles
    assert ".control-legend-chip" not in styles
    assert ".control-device-reason" in styles
    assert ".demo-pill" in styles
    assert ".control-device-panels" in styles
    assert ".control-summary-flow" not in styles
    assert ".tone-skip {\n  border-color: rgba(148,163,184,.14);" in styles
    assert ".tone-skip,\n.tone-warn" not in styles
    tone_send = styles.split(".tone-send {", 1)[1].split(".tone-skip {", 1)[0]
    assert "rgba(34,211,238,.28)" in tone_send
    assert "rgba(57,229,140,.30)" not in tone_send
    assert "grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));" in styles
    assert ".control-device-decision-flow {\n  grid-template-columns: repeat(5, minmax(142px, 1fr));" in styles
    assert ".control-stage:not(:last-child)::after" in styles
