# SPDX-License-Identifier: AGPL-3.0-or-later
"""Browser-CPU guardrail tests for the dashboard frontend.

These execute dashboard/static/app.js under node (like the other frontend
tests) and assert the optimizations that keep browser CPU low:

- live snapshots only render the visible view (hidden views are not rebuilt),
- the analytics/history uPlot charts are reused (setData) instead of being
  destroyed/recreated when their structure is unchanged,
- the analytics KPI cache is not re-integrated when the data is unchanged,
- the dashboard animation mode applies the matching root class.

They are deterministic and never touch a real InfluxDB or browser.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "dashboard" / "static" / "app.js"


def run_node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for dashboard performance guardrail tests")
    result = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# Shared JS prelude: a minimal recording DOM and (optional) fake uPlot.
PRELUDE = """
const app = require(%s);

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(name) { this.values.add(name); }
  remove(name) { this.values.delete(name); }
  toggle(name, force) {
    const on = force === undefined ? !this.values.has(name) : force;
    if (on) this.values.add(name); else this.values.delete(name);
    return on;
  }
  contains(name) { return this.values.has(name); }
}

class FakeElement {
  constructor(id = "") {
    this.id = id;
    this.textContent = "";
    this.innerHTML = "";
    this.hidden = false;
    this.className = "";
    this.children = [];
    this.attrs = {};
    this.classList = new FakeClassList();
    this.clientWidth = 600;
    this.isConnected = true;
    this.style = { setProperty: () => {} };
  }
  setAttribute(key, value) { this.attrs[key] = value; }
  appendChild(child) { this.children.push(child); }
  querySelector() { return null; }
  querySelectorAll() { return []; }
}

function makeDoc(extra = {}) {
  const nodes = new Map();
  const doc = {
    hidden: false,
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, new FakeElement(id));
      return nodes.get(id);
    },
    createElement: () => new FakeElement(),
    querySelector: () => null,
    querySelectorAll: () => [],
  };
  Object.assign(doc, extra);
  doc._nodes = nodes;
  return doc;
}
""" % json.dumps(str(APP_JS))


def test_hidden_views_not_rebuilt_on_live_snapshot():
    # Analytics tab active: a live snapshot must update only global metrics and
    # the analytics live KPIs, never rebuild the device/energy/control views.
    script = PRELUDE + """
const doc = makeDoc();
global.document = doc;

app.state.flowView = "analytics";
app.state.analytics.tab = "overview";

app.updateSnapshot({
  timestamp: "2026-06-17T12:00:00Z",
  pv_total_w: 1800,
  inverter_output_w: 620,
  home_load_w: 620,
  grid_power_w: 0,
  battery_power_w: 0,
  average_soc: 61,
  rules: {},
  devices: { WR1: { ac_mode: 2 } },
  energy_stats: { enabled: true },
  control_explain: { mode: "pv_first" },
});

console.log(JSON.stringify({
  // Global metrics always update.
  metricPv: doc.getElementById("metricPv").textContent,
  // Hidden views must stay empty (their renderers were not called).
  deviceGrid: doc.getElementById("deviceGrid").innerHTML,
  deviceFlowView: doc.getElementById("deviceFlowView").innerHTML,
  energyStats: doc.getElementById("energyStats").innerHTML,
  controlExplainView: doc.getElementById("controlExplainView").innerHTML,
}));
"""
    out = run_node(script)
    assert out["metricPv"] == "1.80 kW"
    assert out["deviceGrid"] == ""
    assert out["deviceFlowView"] == ""
    assert out["energyStats"] == ""
    assert out["controlExplainView"] == ""


def test_aggregated_view_still_rebuilds_its_own_flow():
    # Sanity: the active view IS rendered (the gating does not break the visible
    # view). Aggregated active -> the flow SVG texts update.
    script = PRELUDE + """
const doc = makeDoc();
global.document = doc;

app.state.flowView = "aggregated";
app.updateSnapshot({
  timestamp: "2026-06-17T12:00:00Z",
  pv_total_w: 1800,
  inverter_output_w: 620,
  home_load_w: 620,
  grid_power_w: 0,
  battery_power_w: 0,
  average_soc: 61,
  rules: {},
  devices: {},
});
console.log(JSON.stringify({
  flowPv: doc.getElementById("flowPv").textContent,
  metricPv: doc.getElementById("metricPv").textContent,
}));
"""
    out = run_node(script)
    assert out["flowPv"] == "1.80 kW"
    assert out["metricPv"] == "1.80 kW"


def test_analytics_chart_reused_when_structure_unchanged():
    # Same signature across refreshes -> setData (reuse); changing the overlay
    # set -> destroy + recreate.
    script = PRELUDE + """
const events = [];
function FakeChart(opts, data, container) {
  events.push("create");
  this.data = data;
  this.setData = (d) => { events.push("setData"); this.data = d; };
  this.destroy = () => { events.push("destroy"); };
  this.setScale = () => {};
  this.setSize = () => {};
}
global.uPlot = FakeChart;
global.document = makeDoc();
global.window = { innerWidth: 1200 };

app.state.flowView = "analytics";
app.state.analytics.tab = "overview";
app.state.analytics.data = {
  time: [0, 1800, 3600],
  series: { pv: [1, 2, 3], output: [1, 2, 3], battery: [1, -2, 3] },
};

app.renderAnalyticsChart();          // create
app.renderAnalyticsChart();          // reuse -> setData
const afterReuse = events.slice();

app.state.analytics.overlays.soc = true;  // structure changes
app.renderAnalyticsChart();          // destroy + create
const afterStructureChange = events.slice();

console.log(JSON.stringify({ afterReuse, afterStructureChange }));
"""
    out = run_node(script)
    # First render creates; second reuses via setData without destroying.
    assert out["afterReuse"] == ["create", "setData"]
    # Overlay change forces a destroy + recreate.
    assert out["afterStructureChange"] == ["create", "setData", "destroy", "create"]


def test_analytics_chart_destroyed_on_empty_data():
    # Going to a no-data state tears the chart down and drops the signature.
    script = PRELUDE + """
const events = [];
function FakeChart(opts, data, container) {
  events.push("create");
  this.setData = () => events.push("setData");
  this.destroy = () => events.push("destroy");
  this.setScale = () => {};
}
global.uPlot = FakeChart;
global.document = makeDoc();
global.window = { innerWidth: 1200 };

app.state.flowView = "analytics";
app.state.analytics.tab = "overview";
app.state.analytics.data = { time: [0, 1800], series: { pv: [1, 2], output: [1, 2], battery: [1, 2] } };
app.renderAnalyticsChart();          // create

app.state.analytics.data = { time: [], series: {} };
app.renderAnalyticsChart();          // empty -> destroy

console.log(JSON.stringify({
  events,
  signature: app.state.analytics.chartSignature,
  chart: app.state.analytics.chart,
}));
"""
    out = run_node(script)
    assert out["events"] == ["create", "destroy"]
    assert out["signature"] is None
    assert out["chart"] is None


def test_analytics_kpi_cache_not_reintegrated_when_data_unchanged():
    # The series-based KPI cache is keyed by a stable data identity: an unchanged
    # dataset must not recompute (a live snapshot change does not invalidate it).
    script = PRELUDE + """
global.document = makeDoc();

const data = {
  source: "influxdb",
  time: [0, 1800, 3600],
  series: { pv: [1000, 1000, 1000], battery: [200, -200, 200] },
};
app.state.analytics.tab = "overview";
app.state.analytics.data = data;
app.state.analytics.kpiCache = { dataKey: null, values: {} };

app.ensureAnalyticsKpiCache();
const key1 = app.state.analytics.kpiCache.dataKey;
// Plant a sentinel; if the cache is recomputed it will be discarded.
app.state.analytics.kpiCache.values.__sentinel = "kept";

// Same data identity -> no recompute (sentinel survives).
app.ensureAnalyticsKpiCache();
const afterSameData = app.state.analytics.kpiCache.values.__sentinel;

// Changed data identity -> recompute (sentinel dropped, key changes).
app.state.analytics.data = {
  source: "influxdb",
  time: [0, 1800, 3600, 5400],
  series: { pv: [1000, 1000, 1000, 1000], battery: [200, -200, 200, 0] },
};
app.ensureAnalyticsKpiCache();
const key2 = app.state.analytics.kpiCache.dataKey;
const afterChangedData = app.state.analytics.kpiCache.values.__sentinel;

console.log(JSON.stringify({
  key1, key2,
  sameKeyAcrossUnchangedData: key1 === app.analyticsDataKey(data),
  afterSameData,
  afterChangedData: afterChangedData === undefined,
  keyChanged: key1 !== key2,
}));
"""
    out = run_node(script)
    assert out["sameKeyAcrossUnchangedData"] is True
    assert out["afterSameData"] == "kept"  # not recomputed
    assert out["afterChangedData"] is True  # recomputed (sentinel gone)
    assert out["keyChanged"] is True


def test_analytics_data_key_is_stable_and_distinguishing():
    script = PRELUDE + """
const a = { source: "influxdb", time: [0, 60, 120], series: { pv: [1, 2, 3] } };
const b = { source: "influxdb", time: [0, 60, 120], series: { pv: [9, 9, 9] } };
const c = { source: "influxdb", time: [0, 60, 180], series: { pv: [1, 2, 3] } };
console.log(JSON.stringify({
  same: app.analyticsDataKey(a) === app.analyticsDataKey(b),  // values differ, identity same
  differentExtent: app.analyticsDataKey(a) !== app.analyticsDataKey(c),
  empty: app.analyticsDataKey(null),
}));
"""
    out = run_node(script)
    # Same time-extent/point-count/series => same identity (values are integrated
    # once and cached; identity intentionally ignores per-sample values).
    assert out["same"] is True
    assert out["differentExtent"] is True
    assert out["empty"] == "empty"


def test_animation_mode_applies_root_class():
    script = PRELUDE + """
const body = new FakeElement("body");
global.document = makeDoc({ body });

const reduced = app.setAnimationMode("reduced");
const reducedClasses = {
  reduced: body.classList.contains("dashboard-animation-reduced"),
  off: body.classList.contains("dashboard-animation-off"),
  normal: body.classList.contains("dashboard-animation-normal"),
};

app.setAnimationMode("off");
const offClasses = {
  reduced: body.classList.contains("dashboard-animation-reduced"),
  off: body.classList.contains("dashboard-animation-off"),
};

const bogus = app.setAnimationMode("bogus");
const bogusClasses = {
  normal: body.classList.contains("dashboard-animation-normal"),
  off: body.classList.contains("dashboard-animation-off"),
};

console.log(JSON.stringify({ reduced, reducedClasses, offClasses, bogus, bogusClasses }));
"""
    out = run_node(script)
    assert out["reduced"] == "reduced"
    assert out["reducedClasses"] == {"reduced": True, "off": False, "normal": False}
    assert out["offClasses"] == {"reduced": False, "off": True}
    assert out["bogus"] == "normal"
    assert out["bogusClasses"] == {"normal": True, "off": False}


def test_animation_mode_css_classes_present():
    css = (ROOT / "dashboard" / "static" / "styles.css").read_text(encoding="utf-8")
    assert ".dashboard-animation-reduced .pipe-energy" in css
    assert ".dashboard-animation-off .pipe-energy" in css
    # Browser-level reduced motion is still honored.
    assert "@media (prefers-reduced-motion: reduce)" in css
