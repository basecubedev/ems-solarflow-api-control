# SPDX-License-Identifier: AGPL-3.0-or-later
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

pytestmark = [
    pytest.mark.contract,
]


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
    assert "function renderControlExplain(snapshot, options = {})" in app_js
    assert "data-flow-view=\"control\"" in index_html
    assert "data-flow-view=\"energy\"" in index_html
    assert "id=\"controlExplainView\"" in index_html
    assert "id=\"energyStatsView\"" in index_html
    assert "const batteryFlow = normalizeBatteryPowerForDisplay(aggregatedBatteryPowerW(snapshot));" in app_js
    assert "function signedWatts(value)" in app_js
    assert "function renderEnergyStats(stats)" in app_js
    assert "id=\"energyStats\"" in index_html
    assert "Energy Delivered" in index_html
    assert "Based on measured inverter output." in index_html
    assert "class=\"devices-section\"" in index_html
    assert index_html.index('id="energyStats"') < index_html.index('id="deviceGrid"')
    assert index_html.index('id="deviceGrid"') < index_html.index('chart-panel history-panel')
    assert index_html.count('id="energyStats"') == 1
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
    # Phase 2/3: the single Analytics chart drives the battery series from the
    # shared CSS token, replacing the old hardcoded per-canvas battery color.
    assert 'battery: { label: "Battery Power", colorVar: "--battery", unit: "W" }' in app_js
    assert "displayBatteryPower" not in app_js
    assert "invert: true" not in app_js


def test_frontend_energy_statistics_executes_against_app_js():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for executable dashboard energy stats test")

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
            this.hidden = false;
            this.children = [];
            this.attrs = {};
            this.className = "";
            this.classList = new FakeClassList();
            this.style = { setProperty: () => {} };
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
        const shell = new FakeElement("shell");
        const context = {
          console,
          document: {
            getElementById: element,
            createElement: () => new FakeElement(),
            querySelector: (selector) => {
              if (selector === ".flow-wrap") return flowWrap;
              if (selector === ".shell") return shell;
              return null;
            },
            querySelectorAll: () => [],
          },
          window: { addEventListener: () => {}, localStorage: null },
        };

        vm.createContext(context);
        vm.runInContext(source, context, { filename: appPath });

        function assert(condition, message) {
          if (!condition) throw new Error(message);
        }

        function render(energyStats) {
          // Energy stats only render while the Energy view is on screen
          // (live snapshots are view-gated to avoid rebuilding hidden views).
          context.setFlowView("energy");
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
            control_explain: null,
            energy_stats: energyStats,
          });
          return element("energyStats").innerHTML;
        }

        const stats = {
          enabled: true,
          currency: "EUR",
          price_per_kwh: 0.35,
          today: { inverter_output_kwh: 3.2, savings_value: 1.12 },
          yesterday: { inverter_output_kwh: 4.2, savings_value: 1.47, peak_output_w: 780 },
          last_7_days: { inverter_output_kwh: 18.4, savings_value: 6.44 },
          last_4_weeks: { inverter_output_kwh: 72.1, savings_value: 25.24 },
          last_12_months: { inverter_output_kwh: 520.0, savings_value: 182.0 },
          best_day: { date: "2026-06-14", inverter_output_kwh: 8.4, savings_value: 2.94 },
          monthly_current_year: [
            { month: 1, label: "Jan", inverter_output_kwh: 22.4, savings_value: 7.84 },
            { month: 2, label: "Feb", inverter_output_kwh: 31.2, savings_value: 10.92 },
          ],
          yearly: [
            { year: 2018, inverter_output_kwh: 10, savings_value: 3.5 },
            { year: 2019, inverter_output_kwh: 20, savings_value: 7 },
            { year: 2020, inverter_output_kwh: 30, savings_value: 10.5 },
            { year: 2021, inverter_output_kwh: 40, savings_value: 14 },
            { year: 2022, inverter_output_kwh: 50, savings_value: 17.5 },
            { year: 2023, inverter_output_kwh: 60, savings_value: 21 },
            { year: 2024, inverter_output_kwh: 70, savings_value: 24.5 },
            { year: 2025, inverter_output_kwh: 320.0, savings_value: 112.0 },
            { year: 2026, inverter_output_kwh: 840.0, savings_value: 294.0 },
            { year: 2027, inverter_output_kwh: 910.0, savings_value: 318.5 },
          ],
          lifetime: { inverter_output_kwh: 2070.0, savings_value: 724.5, since_date: "2026-05-31" },
        };

        const html = render(stats);
        assert(html.includes("energy-report-board"), "energy report board renders");
        assert(html.includes("energy-period-pipeline"), "energy KPI pipeline renders");
        assert(html.includes("energy-period-stage"), "KPI cards render as period stages");
        assert(html.includes("energy-stage-head"), "KPI cards use stage headers");
        assert(!html.includes("energy-stage-step"), "KPI cards do not render step numbers");
        assert(!html.includes("energy-stage-dot"), "KPI cards do not render header icons");
        assert(html.includes("energy-context-rail"), "energy context rail renders");
        assert(html.includes("energy-context-item"), "energy context items render");
        assert(html.includes("Currency"), "currency context renders");
        assert(html.includes("With data"), "months-with-data context renders");
        assert(html.includes("energy-report-section"), "energy report sections render");
        assert(html.includes("energy-summary-card"), "summary cards render");
        assert(!html.includes("energy-lifetime-result"), "lifetime no longer renders as hero result card");
        assert(html.includes("energy-kpi-grid"), "primary KPI grid renders");
        assert(html.includes("Today"), "Today card renders");
        assert(html.includes("Yesterday"), "Yesterday card renders");
        assert(html.includes("Previous day output"), "Yesterday subtitle renders");
        assert(html.includes("Last 7 Days"), "Last 7 Days card renders");
        assert(html.includes("Last 4 Weeks"), "Last 4 Weeks card renders");
        assert(html.includes("Last 12 Months"), "Last 12 Months card renders");
        assert(html.includes("Best Day"), "Best Day card renders");
        assert(html.includes("2026-06-14"), "Best Day date renders");
        assert(html.includes("Monthly Summary"), "monthly section renders");
        assert(html.includes("Yearly Summary"), "yearly section renders");
        assert(html.includes("Result / Lifetime"), "Lifetime card renders as a normal result summary");
        assert(html.includes("All stored daily totals"), "Lifetime subtitle remains descriptive");
        assert(html.includes("Date") && html.includes("2026-05-31"), "Lifetime since date renders as a detail field");
        assert(html.includes("3.2 kWh"), "Today kWh renders");
        assert(html.includes("4.2 kWh"), "Yesterday kWh renders");
        assert(html.includes("18.4 kWh"), "Last 7 Days kWh renders");
        assert(html.includes("72.1 kWh"), "Last 4 Weeks kWh renders");
        assert(html.includes("520.0 kWh"), "Last 12 Months kWh renders");
        assert(html.includes("2,070 kWh"), "Lifetime kWh renders");
        assert(html.includes("€1.12"), "Today savings renders");
        assert(html.includes("€1.47"), "Yesterday savings renders");
        assert(html.includes("€724.50"), "Lifetime savings renders");
        assert(html.includes("Jan"), "January renders");
        assert(html.includes("Feb"), "February renders");
        assert(html.includes("Mar"), "missing months are filled");
        assert(html.includes("0.0 kWh"), "missing month zero value renders");
        assert(html.includes("energy-zero"), "zero-data months are muted");
        assert(html.includes("energy-current"), "current month or year is highlighted");
        assert([...html.matchAll(/energy-month-card/g)].length === 12, "all 12 month cards render");
        assert([...html.matchAll(/energy-year-card/g)].length === 8, "year cards are limited to latest 8 years");
        assert(!html.includes(">2018<"), "oldest years beyond the limit are hidden");
        assert(!html.includes(">2019<"), "second old year beyond the limit is hidden");
        assert(html.includes(">2020<"), "latest 8 years start with 2020");
        assert(html.includes(">2027<"), "latest year is visible");
        assert(!html.includes("undefined"), "energy stats do not render undefined");
        assert(!html.includes("null"), "energy stats do not render null");
        assert(
          html.indexOf("Today") < html.indexOf("Yesterday")
            && html.indexOf("Yesterday") < html.indexOf("Last 7 Days"),
          "Yesterday renders directly after Today"
        );

        assert(context.formatSavings({ savings_value: 12.4 }, "CHF") === "12.40 CHF", "non-EUR currency uses code fallback");
        assert(context.formatSavings({}, "EUR") === "--", "missing savings has calm fallback");

        assert(render(undefined).includes("Energy statistics not available yet."), "missing stats fallback renders");
        assert(render({ enabled: false }).includes("Energy statistics are disabled."), "disabled fallback renders");
        assert(render({ enabled: true, currency: "EUR", today: { inverter_output_kwh: 0 }, lifetime: { inverter_output_kwh: 0 } }).includes("Waiting for the first measured inverter output sample."), "first sample fallback renders");
        assert(render({ enabled: true, currency: "EUR", today: { inverter_output_kwh: 0 }, lifetime: { inverter_output_kwh: 0, since_date: "2026-05-31" } }).includes("2026-05-31"), "lifetime since date renders before energy accumulates");

        const demo = context.demoSnapshot();
        context.renderSnapshot(demo);
        const demoHtml = element("energyStats").innerHTML;
        assert(demo.energy_stats.enabled === true, "demo carries enabled energy stats");
        assert(demo.energy_stats.yesterday, "demo carries Yesterday energy stats");
        assert(demoHtml.includes("energy-kpi-grid"), "demo renders energy board content");
        assert(demoHtml.includes("Today"), "demo renders Today card");
        assert(demoHtml.includes("Yesterday"), "demo renders Yesterday card");
        assert(demoHtml.includes("Lifetime"), "demo renders Lifetime card");

        context.setFlowView("energy");
        assert(element("flowSvg").hidden === true, "aggregated SVG is hidden in Energy tab");
        assert(element("deviceFlowView").hidden === true, "device view is hidden in Energy tab");
        assert(element("controlExplainView").hidden === true, "control view is hidden in Energy tab");
        assert(element("energyStatsView").hidden === false, "energy stats view is visible in Energy tab");
        assert(flowWrap.classList.contains("view-energy"), "energy class is applied to the flow board");
        assert(shell.classList.contains("view-energy"), "energy class is applied to the dashboard shell");

        context.setFlowView("control");
        assert(element("controlExplainView").hidden === false, "control view is visible after switching back");
        assert(element("energyStatsView").hidden === true, "energy stats are hidden in Control tab");
        assert(!shell.classList.contains("view-energy"), "dashboard shell leaves energy mode outside Energy tab");
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
        assert(pipeGroups === 7, "each device renders PV, battery and one house line pipe plus one shared grid pipe");
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

        render({
          devices: {
            INV_1: { serial_number: "EOD1NLN9P010902", soc: 71, pv_input_w: 900, output_w: 600, battery_power_w: 300 },
            INV_2: { serial_number: "EOD1NLN9P010611", soc: 52, pv_input_w: 500, output_w: 400, battery_power_w: -100 },
          },
        });
        const compactDeviceHtml = element("deviceFlowView").innerHTML;
        assert(compactDeviceHtml.includes("INV_1"), "Flowchart renders the first compact EMS key");
        assert(compactDeviceHtml.includes("INV_2"), "Flowchart renders the second compact EMS key");
        assert(!compactDeviceHtml.includes("EOD1NLN9P010902"), "Flowchart label excludes the serial number");
        assert(!compactDeviceHtml.includes("EOD1NLN9P010611"), "Flowchart label excludes the second serial number");

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
        const shell = new FakeElement("shell");
        const flowButtons = ["aggregated", "devices", "control", "energy"].map((view) => {
          const button = new FakeElement(`${view}Button`);
          button.dataset.flowView = view;
          return button;
        });

        const context = {
          console,
          document: {
            getElementById: element,
            createElement: () => new FakeElement(),
            querySelector: (selector) => {
              if (selector === ".flow-wrap") return flowWrap;
              if (selector === ".shell") return shell;
              return null;
            },
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
        assert(element("energyStatsView").hidden === true, "energy view is hidden after Control click");
        assert(flowWrap.classList.contains("view-control"), "control class is applied to the flow board");
        assert(flowButtons[2].attrs["aria-selected"] === "true", "Control tab is selected");
        assert(flowButtons[3].attrs["aria-selected"] === "false", "Energy tab is not selected while Control is active");

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
        const firstDeviceIndex = html.indexOf('data-control-device="WR1"');
        const globalPipelineHtml = html.slice(html.indexOf("control-global-pipeline"), firstDeviceIndex);
        for (const stage of expectedPipelineStages) {
          const stageIndex = globalPipelineHtml.indexOf(`<h3 class="control-stage-title">${stage}</h3>`);
          assert(stageIndex > previousStageIndex, `${stage} appears in global pipeline order`);
          previousStageIndex = stageIndex;
        }
        const expectedDeviceStages = [
          "Inputs",
          "Weighting",
          "Raw Target",
          "Adjustments / Limits",
          "Final Target",
        ];
        const firstDeviceHtml = html.slice(firstDeviceIndex, html.indexOf('data-control-device="WR2"'));
        previousStageIndex = -1;
        for (const stage of expectedDeviceStages) {
          const stageIndex = firstDeviceHtml.indexOf(`<h3 class="control-stage-title">${stage}</h3>`);
          assert(stageIndex > previousStageIndex, `${stage} appears in per-device decision order`);
          previousStageIndex = stageIndex;
        }
        const deviceFlows = [...html.matchAll(/data-control-device=/g)].length;
        assert(deviceFlows === 2, "WR1 and WR2 each render one decision flow");
        assert(html.includes("control-decision-board"), "control view renders a decision board");
        assert(html.includes("control-stage-row"), "control stages are grouped into rows");
        assert(!html.includes("control-flow-rail"), "control view no longer renders a flow rail");
        assert(!html.includes("control-flow-node"), "control view no longer renders rail nodes");
        assert(html.includes("control-global-pipeline"), "global decision pipeline is rendered");
        assert([...html.matchAll(/control-pipeline-stage/g)].length === 6, "global pipeline renders six stages");
        assert([...html.matchAll(/class="control-result control-stage-result/g)].length === 16, "every global and device stage has one primary result");
        assert([...html.matchAll(/class="control-stage-step"/g)].length === 16, "every stage header has a visible step number");
        assert(html.includes('<span class="control-stage-step">01</span>'), "stage numbering starts at 01");
        assert(html.includes('<span class="control-stage-step">05</span>'), "device flow includes the fifth step");
        assert([...html.matchAll(/class="control-stage-kicker"/g)].length === 16, "every stage header groups number and icon above the title");
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
        assert(element("energyStatsView").hidden === true, "energy stats remain hidden from Control");
        assert(!html.includes("Energy Delivered"), "Control tab does not render Energy Statistics heading");
        assert(!html.includes("Monthly Summary"), "Control tab does not render Energy monthly summary");

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

        // Control tab is active (clicked above): renderSnapshot renders the
        // global metrics plus the control view. Aggregated/device content is
        // view-gated, so render those views explicitly to assert their output.
        context.renderSnapshot(demo);
        const demoHtml = element("controlExplainView").innerHTML;
        context.renderViewSnapshot("aggregated", demo);
        context.renderViewSnapshot("devices", demo);
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
        assert(element("energyStatsView").hidden === true, "energy view hides after switching back");
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
    assert ".device-flow-view { padding: calc(6px * var(--d)); overflow-x: hidden; }" in styles
    assert ".device-flow-svg { min-width: 0; width: 100%; }" in styles
    assert ".flow-wrap.view-control,\n.flow-wrap.view-energy { overflow: visible; }" in styles
    assert ".flow-wrap.view-energy .energy-stats-view { display: block !important; }" in styles
    assert ".energy-stats-view" in styles
    assert ".shell.view-energy > .devices-section,\n.shell.view-energy > .chart-panel { display: none; }" in styles
    assert ".energy-report-board" in styles
    assert ".energy-period-pipeline" in styles
    assert ".energy-period-stage" in styles
    assert ".energy-stage-head" in styles
    assert ".energy-stage-step" not in styles
    assert ".energy-stage-dot" not in styles
    assert ".energy-context-rail" in styles
    assert ".energy-report-section" in styles
    assert ".energy-summary-card" in styles
    assert ".energy-lifetime-result" not in styles
    assert ".energy-zero" in styles
    assert ".energy-current" in styles
    assert ".control-global-pipeline" in styles
    assert ".control-flow-rail" not in styles
    assert ".control-flow-node" not in styles
    assert ".control-stage-step" in styles
    assert ".control-stage-header" in styles
    assert ".control-stage-kicker" in styles
    assert ".role-solar" in styles
    assert ".role-battery" in styles
    assert ".role-output" in styles
    assert ".role-grid" in styles
    assert "var(--pv)" in styles
    assert "var(--battery)" in styles
    assert "var(--output)" in styles
    assert "var(--grid)" in styles
    assert ".control-result-divider" not in styles
    assert "@keyframes controlRailSweep" not in styles
    assert "@keyframes controlRailFlow" not in styles
    # The travelling border is still there; it is no longer driven by a paint
    # property. `controlResultBorderFlow` animated `background-position`, which
    # cost the authenticated control view three quarters of its frame rate once
    # the runtime editor put twenty of those buttons on screen. Both the chips
    # and the buttons now translate a child instead.
    assert "@keyframes controlResultBorderFlow" not in styles
    assert "@keyframes controlResultRingSlide" in styles
    assert ".button-ring i" in styles
    assert "height: 58px;" in styles
    assert ".control-context-rail" in styles
    assert ".control-color-legend" not in styles
    assert ".control-legend-chip" not in styles
    assert ".control-device-reason" in styles
    assert ".demo-pill" in styles
    assert ".control-device-panels" in styles
    assert ".control-summary-flow" not in styles
    assert (
        '.tone-skip {\n  border-color: color-mix(in srgb, var(--muted) 14%, transparent);'
        in styles
    )
    assert ".tone-skip,\n.tone-warn" not in styles
    tone_send = styles.split(".tone-send {", 1)[1].split(".tone-skip {", 1)[0]
    assert "color-mix(in srgb, var(--accent2) 28%, transparent)" in tone_send
    assert "color-mix(in srgb, var(--battery) 30%, transparent)" not in tone_send
    assert "grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));" in styles
    assert ".control-device-decision-flow {\n  grid-template-columns: repeat(5, minmax(142px, 1fr));" in styles
    assert ".control-stage:not(:last-child)::after" in styles


def _grid_controller(health):
    return SimpleNamespace(
        devices=[SimpleNamespace(name="WR1")],
        runtime_state=None,
        device_online={"WR1": True},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
        shelly=SimpleNamespace(health=health),
    )


def _snapshot_for(controller):
    return build_dashboard_snapshot(
        controller,
        load_w=240,
        states=[SimpleNamespace(solar=0, output=0, pack_out=0, pack_in=0, soc=50)],
        targets=[0],
        effective_targets=[0],
        allocated_total_w=0,
        effective_total_w=0,
        enabled=True,
        max_total_power=800,
        min_output_limit=35,
    )


def test_grid_power_is_marked_valid_after_a_successful_read():
    controller = _grid_controller(
        SimpleNamespace(success_count=3, stale_used=False)
    )

    assert _snapshot_for(controller)["grid_power_valid"] is True


def test_grid_power_is_marked_invalid_while_a_stale_value_is_used():
    """An unreachable meter keeps publishing its last value, or its initial 0.

    Reported as valid, the statistics would integrate it and claim no grid
    import for an installation whose meter is simply down.
    """

    never_answered = _grid_controller(
        SimpleNamespace(success_count=0, stale_used=False)
    )
    stale = _grid_controller(SimpleNamespace(success_count=5, stale_used=True))

    assert _snapshot_for(never_answered)["grid_power_valid"] is False
    assert _snapshot_for(stale)["grid_power_valid"] is False


def test_grid_power_counts_as_valid_without_health_data():
    """Simulation and replay clients have no health; their readings are real."""

    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="WR1")],
        runtime_state=None,
        device_online={"WR1": True},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
    )

    assert _snapshot_for(controller)["grid_power_valid"] is True


def _two_device_controller(fresh, *, runtime_state=None):
    """Two devices, the second one offline, freshness answered by ``fresh``.

    The names are checked here because the snapshot builder treats a raising
    freshness lookup as "measured": a typo in ``fresh`` would otherwise make a
    test pass without ever asking the question it claims to ask.
    """

    assert set(fresh) == {"WR1", "WR2"}, fresh

    return SimpleNamespace(
        devices=[SimpleNamespace(name="WR1"), SimpleNamespace(name="WR2")],
        runtime_state=runtime_state,
        device_online={"WR1": True, "WR2": False},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
        telemetry_fresh=lambda name: fresh[name],
    )


def _two_device_snapshot(controller):
    state = SimpleNamespace(solar=500, output=300, pack_out=0, pack_in=0, soc=50)

    return build_dashboard_snapshot(
        controller,
        load_w=100,
        states=[state, state],
        targets=[300, 300],
        effective_targets=[300, 300],
        allocated_total_w=600,
        effective_total_w=600,
        enabled=True,
        max_total_power=800,
        min_output_limit=35,
    )


def test_device_power_stays_valid_through_a_single_failed_read():
    """A failed read is not a gap while the cached state is still current.

    The controller keeps publishing that state and the control loop keeps
    regulating on it, so the statistics integrate it over the same window.
    Otherwise one network hiccup would mark the day, the month and the year.
    """

    controller = _two_device_controller({"WR1": True, "WR2": True})
    snapshot = _two_device_snapshot(controller)

    assert snapshot["device_power_valid"] is True
    # The tile still says what happened, which is what the Overview shows.
    assert snapshot["devices"]["WR2"]["online"] is False


def test_device_power_is_invalid_once_telemetry_ages_out():
    """Past the staleness window the last value is no longer a reading.

    Integrating it would let a device that dropped off the network keep
    contributing PV and battery energy for as long as it is gone.
    """

    controller = _two_device_controller({"WR1": True, "WR2": False})
    snapshot = _two_device_snapshot(controller)

    assert snapshot["device_power_valid"] is False
    assert snapshot["grid_power_valid"] is True


def test_a_controller_that_cannot_date_its_telemetry_is_taken_at_its_word():
    """Simulation, replay and test doubles have no timestamps; they measure.

    The same choice the grid reading makes for a client without health data,
    and the same one ``sample_is_measured`` makes for a snapshot without the
    flags: absent evidence is not evidence of a gap.
    """

    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="WR1"), SimpleNamespace(name="WR2")],
        runtime_state=None,
        device_online={"WR1": True, "WR2": False},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
    )

    assert _two_device_snapshot(controller)["device_power_valid"] is True


def test_device_power_is_valid_while_every_device_reports():
    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="WR1")],
        runtime_state=None,
        device_online={"WR1": True},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
    )

    assert _snapshot_for(controller)["device_power_valid"] is True


def test_a_disabled_device_does_not_stop_the_statistics():
    """Switching a unit off for the season is a decision, not a gap.

    An enabled device whose telemetry has aged out halts the figures, because
    its last reading would otherwise be integrated as a measurement. A disabled
    one is not being run at all.
    """

    controller = _two_device_controller(
        {"WR1": True, "WR2": False},
        runtime_state=SimpleNamespace(
            get_device=lambda name, key, default: (
                False if (name, key) == ("WR2", "enabled") else default
            ),
        ),
    )
    snapshot = _two_device_snapshot(controller)

    assert snapshot["devices"]["WR2"]["enabled"] is False
    assert snapshot["device_power_valid"] is True
    # It is still reported as offline, which is what the Overview shows.
    assert snapshot["rules"]["offline_devices"]["active"] is True


def _disabled_second_device():
    return SimpleNamespace(
        get_device=lambda name, key, default: (
            False if (name, key) == ("WR2", "enabled") else default
        ),
    )


def test_a_disabled_device_that_went_quiet_contributes_nothing():
    """The exemption from holding the statistics is paid for by not counting.

    A unit switched off for the season keeps publishing its last reading until
    the EMS restarts. It does not hold the figures -- switching it off was a
    decision -- so its frozen PV would otherwise be integrated as measured
    energy for as long as the process runs.
    """

    controller = _two_device_controller(
        {"WR1": True, "WR2": False}, runtime_state=_disabled_second_device()
    )
    snapshot = _two_device_snapshot(controller)

    assert snapshot["device_power_valid"] is True
    assert snapshot["pv_total_w"] == 500
    assert snapshot["inverter_output_w"] == 300
    # The tile still carries the device's own last values.
    assert snapshot["devices"]["WR2"]["pv_input_w"] == 500


def test_a_disabled_device_that_still_answers_is_still_counted():
    """Excluded from control is not the same as absent: its PV is real."""

    controller = _two_device_controller(
        {"WR1": True, "WR2": True}, runtime_state=_disabled_second_device()
    )
    snapshot = _two_device_snapshot(controller)

    assert snapshot["pv_total_w"] == 1000
    assert snapshot["inverter_output_w"] == 600


def test_an_enabled_device_that_went_quiet_stays_in_the_aggregates():
    """Deliberately the other way round, and the reason is the gate.

    Its sample is dropped whole, so nothing it contributes is ever integrated.
    Leaving the cached value in the totals keeps the Overview showing what the
    control loop is still steering with.
    """

    controller = _two_device_controller({"WR1": True, "WR2": False})
    snapshot = _two_device_snapshot(controller)

    assert snapshot["device_power_valid"] is False
    assert snapshot["pv_total_w"] == 1000


def test_every_validity_flag_the_gate_reads_is_one_the_snapshot_writes():
    """Two lists that have to agree, walked.

    ``sample_is_measured`` treats a flag missing from the snapshot as measured,
    so renaming a key on the publishing side would disable the whole gate in
    silence -- every figure would look measured again. The registry is the
    authority; this test holds the publisher to it.
    """

    from ems.energy_channels import SAMPLE_VALIDITY_FLAGS

    snapshot = _two_device_snapshot(
        _two_device_controller({"WR1": True, "WR2": True})
    )

    missing = [flag for flag in SAMPLE_VALIDITY_FLAGS if flag not in snapshot]

    assert not missing, f"snapshot does not publish {missing}"
    assert all(isinstance(snapshot[flag], bool) for flag in SAMPLE_VALIDITY_FLAGS)


def test_a_freshness_lookup_that_raises_never_breaks_the_snapshot():
    """Building the snapshot is what keeps the whole dashboard alive.

    A controller that answers the freshness question with an exception must not
    take the dashboard down with it, and the sample counts as measured -- the
    same direction as every other missing-evidence fallback here.
    """

    def boom(name):
        raise RuntimeError("no clock for you")

    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="WR1"), SimpleNamespace(name="WR2")],
        runtime_state=None,
        device_online={"WR1": True, "WR2": True},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
        telemetry_fresh=boom,
    )

    assert _two_device_snapshot(controller)["device_power_valid"] is True
