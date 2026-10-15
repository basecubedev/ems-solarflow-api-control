# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.contract,
]


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "dashboard" / "static" / "app.js"
INDEX_HTML = ROOT / "dashboard" / "static" / "index.html"


class FlowTabParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._in_flow_tabs = False
        self._current_tab = None
        self.tabs = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "div" and "flow-view-tabs" in classes:
            self._in_flow_tabs = True
            return
        if self._in_flow_tabs and tag == "button" and "data-flow-view" in attrs:
            self._current_tab = {"view": attrs["data-flow-view"], "label": ""}

    def handle_data(self, data):
        if self._current_tab is not None:
            self._current_tab["label"] += data

    def handle_endtag(self, tag):
        if self._current_tab is not None and tag == "button":
            self._current_tab["label"] = self._current_tab["label"].strip()
            self.tabs.append(self._current_tab)
            self._current_tab = None
            return
        if self._in_flow_tabs and tag == "div":
            self._in_flow_tabs = False


def run_node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for executable analytics frontend test")
    result = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_top_level_dashboard_tabs_use_workflow_order():
    parser = FlowTabParser()
    parser.feed(INDEX_HTML.read_text(encoding="utf-8"))
    assert parser.tabs == [
        {"view": "aggregated", "label": "Overview"},
        {"view": "devices", "label": "Devices"},
        {"view": "energy", "label": "Energy"},
        {"view": "analytics", "label": "Analytics"},
        {"view": "control", "label": "Control"},
        {"view": "diagnose", "label": "Diagnose"},
        {"view": "logs", "label": "Logs"},
        {"view": "maintenance", "label": "Maintenance"},
    ]


def test_analytics_subtabs_present_in_markup():
    html = INDEX_HTML.read_text(encoding="utf-8")
    for tab in ("overview", "devices", "grid", "battery", "pv"):
        assert f'data-analytics-tab="{tab}"' in html


def test_dedicated_analytics_tab_and_history_panel_markup():
    html = INDEX_HTML.read_text(encoding="utf-8")
    # Analytics is now a dedicated top-level tab with its own in-wrap view.
    assert 'data-flow-view="analytics"' in html
    assert 'id="analyticsView"' in html
    # Clean unavailable state for when InfluxDB is not configured.
    assert 'id="analyticsUnavailable"' in html
    assert "InfluxDB analytics is not configured" in html
    # Lightweight SQLite history panel remains for Aggregate/Devices.
    assert 'class="chart-panel history-panel"' in html
    assert 'id="historyChart"' in html
    assert 'data-history-range=' in html


def test_analytics_unavailable_state_toggles_body():
    # available:false (InfluxDB not configured) shows the info state and hides
    # the chart body; an available payload does the reverse.
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const nodes = {{
  analyticsUnavailable: {{ hidden: false }},
  analyticsBody: {{ hidden: false }},
}};
global.document = {{ hidden: false, getElementById: (id) => nodes[id] || null }};

app.setAnalyticsAvailable(false);
const whenUnavailable = {{
  unavailableHidden: nodes.analyticsUnavailable.hidden,
  bodyHidden: nodes.analyticsBody.hidden,
  state: app.state.analytics.available,
}};
app.setAnalyticsAvailable(true);
const whenAvailable = {{
  unavailableHidden: nodes.analyticsUnavailable.hidden,
  bodyHidden: nodes.analyticsBody.hidden,
  state: app.state.analytics.available,
}};
console.log(JSON.stringify({{ whenUnavailable, whenAvailable }}));
"""
    out = run_node(script)
    assert out["whenUnavailable"] == {
        "unavailableHidden": False,
        "bodyHidden": True,
        "state": False,
    }
    assert out["whenAvailable"] == {
        "unavailableHidden": True,
        "bodyHidden": False,
        "state": True,
    }


def test_history_fetch_url_uses_sqlite_endpoint():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
global.document = {{ hidden: false, getElementById: () => null }};
app.state.history.range = "6h";
app.state.history.device = "WR1";
console.log(JSON.stringify({{ url: app.historyFetchUrl() }}));
"""
    out = run_node(script)
    assert out["url"].startswith("/api/history/series?")
    assert "range=6h" in out["url"]
    assert "series=pv%2Coutput%2Cbattery" in out["url"]
    assert "devices=WR1" in out["url"]


def test_analytics_kpis_render_per_tab():
    # uPlot is undefined under node, so the chart render is skipped, but the KPI
    # cards are pure data-to-HTML and exercise the per-tab config end to end.
    script = f"""
const app = require({json.dumps(str(APP_JS))});

const host = {{ innerHTML: "" }};
global.document = {{ getElementById: (id) => (id === "analyticsKpis" ? host : null) }};

const data = {{
  time: [0, 1800, 3600],
  series: {{
    pv: [1000, 1000, 1000],
    output: [500, 500, 500],
    battery: [200, -200, 200],
    grid: [100, -100, 100],
    home: [400, 400, 400],
  }},
}};
const snapshot = {{ average_soc: 73, devices: {{ WR1: {{ ac_mode: 2 }} }} }};
app.state.snapshot = snapshot;
app.state.analytics.data = data;
app.state.range = "24h";

const out = {{}};
for (const tab of app.ANALYTICS_TABS) {{
  app.state.analytics.tab = tab.id;
  host.innerHTML = "";
  app.renderAnalyticsKpis();
  out[tab.id] = host.innerHTML;
}}
console.log(JSON.stringify(out));
"""
    out = run_node(script)

    # Overview shows the six headline KPIs.
    assert "PV · 24h" in out["overview"]
    assert "Charge · 24h" in out["overview"]
    assert "Discharge · 24h" in out["overview"]
    assert "Current SoC" in out["overview"]
    assert "73%" in out["overview"]
    assert "Runtime Role" in out["overview"]
    assert "Output" in out["overview"]

    # Grid tab swaps in grid-specific KPIs.
    assert "Grid Import · 24h" in out["grid"]
    assert "Grid Export · 24h" in out["grid"]
    assert "Home · 24h" in out["grid"]
    assert "tone-grid" in out["grid"]

    # Battery tab focuses on charge/discharge.
    assert "Charge · 24h" in out["battery"]
    assert "Discharge · 24h" in out["battery"]
    assert "Grid Import" not in out["battery"]

    # PV tab exposes a peak KPI.
    assert "PV Peak" in out["pv"]


def test_analytics_overlay_markup_present():
    html = INDEX_HTML.read_text(encoding="utf-8")
    for overlay in ("soc", "target", "grid"):
        assert f'data-analytics-overlay="{overlay}"' in html
    assert 'id="analyticsCustomFrom"' in html
    assert 'id="analyticsCustomTo"' in html
    assert 'id="analyticsCustomApply"' in html


def test_grid_overlay_named_grid_power_not_grid_share():
    # "Grid Share" was misleading (it plots grid power, not a share metric).
    html = INDEX_HTML.read_text(encoding="utf-8")
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "Grid Share" not in html
    assert "Grid Share" not in app_js
    assert ">Grid Power</button>" in html


def test_overlays_are_all_data_backed_series():
    # Every visible overlay must map to a series the catalog/provider serves.
    script = f"""
const app = require({json.dumps(str(APP_JS))});
console.log(JSON.stringify({{
  overlays: app.ANALYTICS_OVERLAYS.map((o) => o.id),
  meta: Object.keys(app.ANALYTICS_SERIES_META),
}}));
"""
    out = run_node(script)
    for overlay in out["overlays"]:
        assert overlay in out["meta"], f"overlay {overlay} has no series metadata"
    assert set(out["overlays"]) == {"soc", "target", "grid", "ac_charge"}

    # Frontend metadata and the backend catalog are two lists of the same
    # series. A name that exists in one only serves an empty chart.
    from ems.history.provider import SERIES_CATALOG

    assert set(out["meta"]) == set(SERIES_CATALOG)


def test_analytics_overlays_extend_active_series_and_custom_range():
    script = f"""
const app = require({json.dumps(str(APP_JS))});

// Minimal stubs so the toggle/apply handlers (which touch the DOM and fetch)
// run without a browser. uPlot stays undefined so chart render is skipped.
global.document = {{ querySelectorAll: () => [], getElementById: () => null }};
global.fetch = async () => ({{ ok: false, json: async () => ({{}}) }});

// Overlays append to the active tab's base series, de-duplicated.
app.state.analytics.tab = "overview";
const base = app.activeAnalyticsSeries();
app.toggleAnalyticsOverlay("soc");
app.toggleAnalyticsOverlay("target");
const withOverlays = app.activeAnalyticsSeries();

// Grid tab already includes grid; toggling the grid overlay must not duplicate.
app.state.analytics.tab = "grid";
app.toggleAnalyticsOverlay("grid");
const gridSeries = app.activeAnalyticsSeries();

// Custom range validation.
const okApply = app.applyCustomRange("2026-06-01T00:00", "2026-06-02T00:00");
const customAfterApply = JSON.parse(JSON.stringify(app.state.analytics.custom));
const badApply = app.applyCustomRange("2026-06-02T00:00", "2026-06-01T00:00");

console.log(JSON.stringify({{
  base, withOverlays, gridSeries,
  okApply, badApply, customAfterApply,
}}));
"""
    out = run_node(script)
    assert out["base"] == ["pv", "output", "battery"]
    assert out["withOverlays"] == ["pv", "output", "battery", "soc", "target"]
    # grid overlay must not be duplicated when the tab already plots grid
    assert out["gridSeries"].count("grid") == 1
    assert out["okApply"] is True
    assert out["badApply"] is False
    assert out["customAfterApply"]["active"] is True
    assert out["customAfterApply"]["start"] < out["customAfterApply"]["end"]


def test_analytics_loading_and_chart_wrap_markup():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="analyticsLoading"' in html
    assert "analytics-chart-wrap" in html


def test_analytics_panel_visibility_gates_hidden_views():
    # InfluxDB analytics lives in its own dedicated tab now: it is only visible
    # (and only fetches) when that tab is active. The lightweight SQLite history
    # is visible on the operational Aggregate/Devices views instead.
    script = f"""
const app = require({json.dumps(str(APP_JS))});
global.document = {{ hidden: false }};
const analytics = {{}};
const history = {{}};
for (const view of ["aggregated", "devices", "analytics", "control", "energy", "diagnose", "logs"]) {{
  app.state.flowView = view;
  analytics[view] = app.analyticsPanelVisible();
  history[view] = app.historyVisible();
}}
app.state.flowView = "analytics";
global.document.hidden = true;
const analyticsBackgrounded = app.analyticsPanelVisible();
global.document.hidden = false;
app.state.flowView = "aggregated";
global.document.hidden = true;
const historyBackgrounded = app.historyVisible();
console.log(JSON.stringify({{ analytics, history, analyticsBackgrounded, historyBackgrounded }}));
"""
    out = run_node(script)
    # Analytics (InfluxDB) only on its own tab.
    assert out["analytics"]["analytics"] is True
    for view in ("aggregated", "devices", "control", "energy", "diagnose", "logs"):
        assert out["analytics"][view] is False
    # Lightweight history (SQLite) only on the operational views.
    assert out["history"]["aggregated"] is True
    assert out["history"]["devices"] is True
    for view in ("analytics", "control", "energy", "diagnose", "logs"):
        assert out["history"][view] is False
    # Backgrounded tabs never auto-fetch (lazy loading).
    assert out["analyticsBackgrounded"] is False
    assert out["historyBackgrounded"] is False


def test_analytics_zoom_markup_present():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="analyticsBackToLive"' in html


def test_analytics_detect_zoom_pure():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
console.log(JSON.stringify({{
  zoomedIn: app.detectZoom(100, 200, 0, 1000),
  fullExtent: app.detectZoom(0, 1000, 0, 1000),
  invalid: app.detectZoom(null, 200, 0, 1000),
}}));
"""
    out = run_node(script)
    assert out["zoomedIn"] == {"start": 100, "end": 200}
    assert out["fullExtent"] is None
    assert out["invalid"] is None


def test_analytics_zoom_drives_fetch_url_and_pauses_refresh():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
global.document = {{ hidden: false, getElementById: () => null }};

app.state.flowView = "analytics";
app.state.range = "30d";
app.state.analytics.tab = "overview";

const liveUrl = app.analyticsFetchUrl();
const liveRefresh = app.analyticsShouldAutoRefresh();

// Simulate a zoom into a 2h window.
app.state.analytics.zoom = {{ start: 1000, end: 8200 }};
const zoomUrl = app.analyticsFetchUrl();
const zoomRefresh = app.analyticsShouldAutoRefresh();

console.log(JSON.stringify({{ liveUrl, liveRefresh, zoomUrl, zoomRefresh }}));
"""
    out = run_node(script)
    # Live mode uses the period token and auto-refreshes; the InfluxDB analytics
    # tab uses the dedicated analytics endpoint, never the SQLite history one.
    assert "/api/analytics/series" in out["liveUrl"]
    assert "range=30d" in out["liveUrl"]
    assert out["liveRefresh"] is True
    # Zoomed: the URL uses the visible start/end (so the backend picks the finer
    # profile) and auto-refresh is paused.
    assert "start=1000" in out["zoomUrl"]
    assert "end=8200" in out["zoomUrl"]
    assert "range=30d" not in out["zoomUrl"]
    assert out["zoomRefresh"] is False


def test_analytics_back_to_live_clears_zoom():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
global.document = {{ hidden: false, getElementById: () => null }};
global.fetch = async () => ({{ ok: false, json: async () => ({{}}) }});

app.state.analytics.zoom = {{ start: 1000, end: 8200 }};
app.backToLive();
console.log(JSON.stringify({{ zoom: app.state.analytics.zoom }}));
"""
    out = run_node(script)
    assert out["zoom"] is None


def test_analytics_zoom_survives_real_data_requery():
    # With real InfluxDB data a zoom triggers a backend requery; the returned
    # dataset's extent equals the zoom range. The scale handler must NOT treat a
    # visible scale matching that extent as "back to live" -- zoom stays active
    # until the user explicitly leaves it.
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const nodes = {{ analyticsBackToLive: {{ hidden: true }} }};
global.document = {{ hidden: false, getElementById: (id) => nodes[id] || null }};
global.fetch = async () => ({{ ok: true, json: async () => ({{}}) }});

app.state.flowView = "analytics";
app.state.range = "30d";
app.state.analytics.tab = "overview";
// Demo mode short-circuits scheduleZoomRequery so no real timer/fetch runs,
// while still exercising the pure scale-handler logic under test.
app.state.demoMode = true;

// 1) User zooms from the 30d view into a ~2h window.
app.onAnalyticsXScale({{
  data: [[0, 2592000]],
  scales: {{ x: {{ min: 1000, max: 8200 }} }},
}});
const afterZoom = {{
  zoom: app.state.analytics.zoom,
  url: app.analyticsFetchUrl(),
  refresh: app.analyticsShouldAutoRefresh(),
  buttonHidden: nodes.analyticsBackToLive.hidden,
}};

// 2) The requery returns finer data whose extent equals the zoom range; the
// re-render fires the scale handler with the visible scale == loaded extent.
app.onAnalyticsXScale({{
  data: [[1000, 8200]],
  scales: {{ x: {{ min: 1000, max: 8200 }} }},
}});
const afterRequery = {{
  zoom: app.state.analytics.zoom,
  refresh: app.analyticsShouldAutoRefresh(),
  buttonHidden: nodes.analyticsBackToLive.hidden,
}};

// 3) Explicit Back to live resets to live mode.
app.backToLive();
const afterBackToLive = {{
  zoom: app.state.analytics.zoom,
  refresh: app.analyticsShouldAutoRefresh(),
  buttonHidden: nodes.analyticsBackToLive.hidden,
}};

console.log(JSON.stringify({{ afterZoom, afterRequery, afterBackToLive }}));
"""
    out = run_node(script)
    # Zoom-in: state set, URL uses start/end (finer profile), refresh paused,
    # Back to live shown.
    assert out["afterZoom"]["zoom"] == {"start": 1000, "end": 8200}
    assert "start=1000" in out["afterZoom"]["url"]
    assert "end=8200" in out["afterZoom"]["url"]
    assert "range=30d" not in out["afterZoom"]["url"]
    assert out["afterZoom"]["refresh"] is False
    assert out["afterZoom"]["buttonHidden"] is False
    # Requery whose extent equals the zoom range must NOT clear zoom.
    assert out["afterRequery"]["zoom"] == {"start": 1000, "end": 8200}
    assert out["afterRequery"]["refresh"] is False
    assert out["afterRequery"]["buttonHidden"] is False
    # Explicit Back to live returns to live mode.
    assert out["afterBackToLive"]["zoom"] is None
    assert out["afterBackToLive"]["refresh"] is True
    assert out["afterBackToLive"]["buttonHidden"] is True


def test_analytics_chart_inverts_battery_display_only():
    # Display-only sign flip for the Analytics uPlot chart: the battery line is
    # rendered inverted (charging below zero, discharging above zero) while the
    # raw Analytics state and every other series stay on the backend convention.
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const data = {{
  time: [0, 1800, 3600],
  series: {{
    pv: [1000, 1000, 1000],
    output: [500, 500, 500],
    battery: [200, -450, null],
  }},
}};

const matrix = app.analyticsChartSeriesData(data, ["pv", "output", "battery"]);
console.log(JSON.stringify({{
  // Pure helper: only battery flips sign, nulls pass through.
  helper: {{
    batteryCharge: app.analyticsChartDisplayValue("battery", 200),
    batteryDischarge: app.analyticsChartDisplayValue("battery", -450),
    pv: app.analyticsChartDisplayValue("pv", 1000),
    nullValue: app.analyticsChartDisplayValue("battery", null),
  }},
  matrix,
  // Raw analytics state must be untouched by chart rendering.
  rawBattery: data.series.battery,
  // Tooltip translates the inverted display value back to charge/discharge.
  tooltip: {{
    charging: app.analyticsSeriesTooltip("battery", -200, "W"),
    discharging: app.analyticsSeriesTooltip("battery", 450, "W"),
    pv: app.analyticsSeriesTooltip("pv", 1000, "W"),
    none: app.analyticsSeriesTooltip("battery", null, "W"),
  }},
}}));
"""
    out = run_node(script)

    # Helper inverts battery only.
    assert out["helper"]["batteryCharge"] == -200
    assert out["helper"]["batteryDischarge"] == 450
    assert out["helper"]["pv"] == 1000
    assert out["helper"]["nullValue"] is None

    # Chart data matrix: [time, pv, output, battery]. Non-battery untouched,
    # battery inverted, nulls preserved.
    assert out["matrix"][0] == [0, 1800, 3600]
    assert out["matrix"][1] == [1000, 1000, 1000]
    assert out["matrix"][2] == [500, 500, 500]
    assert out["matrix"][3] == [-200, 450, None]

    # Raw Analytics state (and therefore KPIs that integrate it) is unchanged.
    assert out["rawBattery"] == [200, -450, None]

    # Tooltip never implies the API sign convention changed.
    assert out["tooltip"]["charging"] == "Charge 200 W"
    assert out["tooltip"]["discharging"] == "Discharge 450 W"
    assert out["tooltip"]["pv"] == "1000 W"
    assert out["tooltip"]["none"] == "--"


def test_history_chart_inverts_battery_like_analytics():
    # The History chart (Aggregate/Devices) must share the Analytics chart's
    # display-only battery inversion: charging below zero, discharging above
    # zero. It builds its uPlot matrix with the same analyticsChartSeriesData
    # helper over the fixed HISTORY_SERIES set (which includes battery).
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const data = {{
  time: [0, 1800, 3600],
  series: {{
    pv: [1000, 1000, 1000],
    output: [500, 500, 500],
    battery: [200, -450, null],
  }},
}};

const seriesIds = app.HISTORY_SERIES.filter((id) => app.ANALYTICS_SERIES_META[id]);
const matrix = app.analyticsChartSeriesData(data, seriesIds);
console.log(JSON.stringify({{
  historySeries: app.HISTORY_SERIES,
  seriesIds,
  matrix,
  rawBattery: data.series.battery,
}}));
"""
    out = run_node(script)

    # History plots PV, inverter output and battery, with battery present so it
    # is subject to the inversion.
    assert out["historySeries"] == ["pv", "output", "battery"]
    assert "battery" in out["seriesIds"]

    # Matrix: [time, pv, output, battery]. Non-battery untouched, battery
    # inverted (charge<0, discharge>0), nulls preserved.
    assert out["matrix"][0] == [0, 1800, 3600]
    assert out["matrix"][1] == [1000, 1000, 1000]
    assert out["matrix"][2] == [500, 500, 500]
    assert out["matrix"][3] == [-200, 450, None]

    # Raw history state is unchanged by chart rendering.
    assert out["rawBattery"] == [200, -450, None]


def test_analytics_kpis_use_raw_battery_sign_despite_chart_inversion():
    # KPI charge/discharge integration must keep using the raw battery sign
    # (charging positive, discharging negative) even though the chart inverts the
    # battery line. Charge counts positive raw samples, discharge counts negative.
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const host = {{ innerHTML: "" }};
global.document = {{ getElementById: (id) => (id === "analyticsKpis" ? host : null) }};
const data = {{
  time: [0, 3600, 7200],
  series: {{ battery: [1000, 1000, -1000] }},
}};
app.state.snapshot = {{ average_soc: 50, devices: {{}} }};
app.state.analytics.data = data;
app.state.range = "24h";
app.state.analytics.tab = "battery";
app.renderAnalyticsKpis();
console.log(JSON.stringify({{ html: host.innerHTML }}));
"""
    out = run_node(script)
    # Charge integrates the positive raw samples (1.5 kWh), discharge the negative
    # one (500 Wh). If the chart inversion leaked into KPIs these would swap (the
    # all-positive-then-negative ramp makes the two magnitudes distinct).
    assert "Charge · 24h" in out["html"]
    assert "Discharge · 24h" in out["html"]
    assert "1.5 kWh" in out["html"]
    assert "500 Wh" in out["html"]


def test_range_tabs_are_tabs_and_announce_the_selected_one():
    html = INDEX_HTML.read_text(encoding="utf-8")
    for attr in ("data-range=", "data-history-range="):
        buttons = [
            part.split(">", 1)[0] for part in html.split("<button")[1:] if attr in part.split(">", 1)[0]
        ]
        assert buttons, attr
        for button in buttons:
            assert 'role="tab"' in button and 'type="button"' in button, button
        selected = [b for b in buttons if 'aria-selected="true"' in b]
        assert len(selected) == 1 and 'class="active"' in selected[0]
    script = f"""
const app = require({json.dumps(str(APP_JS))});
function fake() {{
  const classes = new Set();
  return {{
    attrs: {{}},
    classList: {{ toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)), has: (n) => classes.has(n) }},
    setAttribute(key, value) {{ this.attrs[key] = value; }},
  }};
}}
const buttons = [fake(), fake(), fake()];
app.markRangeTab(buttons, buttons[1]);
const picked = buttons.map((b) => [b.attrs["aria-selected"], b.classList.has("active")]);
app.markRangeTab(buttons, null);
const custom = buttons.map((b) => [b.attrs["aria-selected"], b.classList.has("active")]);
console.log(JSON.stringify({{ picked, custom }}));
"""
    out = run_node(script)
    assert out["picked"] == [["false", False], ["true", True], ["false", False]]
    assert out["custom"] == [["false", False]] * 3


def test_device_soc_bar_is_a_progressbar_with_its_value():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const grid = {{ innerHTML: "", querySelectorAll: () => [] }};
global.document = {{ getElementById: (id) => (id === "deviceGrid" ? grid : null), querySelectorAll: () => [] }};
app.renderDevices({{ WR1: {{ soc: 61.6, online: true }} }});
console.log(JSON.stringify({{ html: grid.innerHTML }}));
"""
    html = run_node(script)["html"]
    bar = html.split('class="soc-bar"', 1)[1].split(">", 1)[0]
    assert 'role="progressbar"' in bar
    assert 'aria-valuemin="0"' in bar and 'aria-valuemax="100"' in bar
    assert 'aria-valuenow="62"' in bar


def test_analytics_energy_is_not_integrated_across_a_data_gap():
    """A step far longer than the series' spacing is a gap and adds no energy."""
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const pos = (value) => Math.max(0, value);
const minute = [0, 60, 120, 180];
const gap = [0, 60, 120, 120 + 6 * 3600, 180 + 6 * 3600];
console.log(JSON.stringify({{
  regular: app.integrateSeries({{ time: minute, series: {{ pv: [1000, 1000, 1000, 1000] }} }}, "pv", pos),
  gapped: app.integrateSeries({{ time: gap, series: {{ pv: [1000, 1000, 1000, 1000, 1000] }} }}, "pv", pos),
}}));
"""
    out = run_node(script)
    assert out["regular"] == pytest.approx(50.0)
    assert out["gapped"] == pytest.approx(50.0)


def test_analytics_series_peak_and_power_label():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const data = {{ time: [0, 1, 2], series: {{ pv: [100, 2500, null] }} }};
console.log(JSON.stringify({{
  peak: app.seriesPeak(data, "pv"),
  power: app.powerLabel(app.seriesPeak(data, "pv")),
  energy: app.energyLabel(1500),
}}));
"""
    out = run_node(script)
    assert out["peak"] == 2500
    assert out["power"] == "2.50 kW"
    assert out["energy"] == "1.5 kWh"


def test_analytics_and_history_zoom_controls_are_in_the_markup():
    # Drag-zoom was undiscoverable: nothing on the page said the chart could be
    # zoomed at all. Both charts now carry a visible +/- pair in the same corner
    # as Back to live.
    html = INDEX_HTML.read_text(encoding="utf-8")
    for element_id in (
        "analyticsZoomIn",
        "analyticsZoomOut",
        "historyZoomIn",
        "historyZoomOut",
        "historyBackToLive",
    ):
        assert f'id="{element_id}"' in html
    assert html.count('class="analytics-zoom-controls"') == 2


def test_zoom_window_is_pure_and_clamps_to_the_bounds():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
console.log(JSON.stringify({{
  centred: app.zoomWindow(0, 1000, 500, 0.5, 0, 1000),
  atPointer: app.zoomWindow(0, 1000, 800, 0.5, 0, 1000),
  atLeftEdge: app.zoomWindow(0, 1000, 0, 0.5, 0, 1000),
  shiftedOffTheRightBound: app.zoomWindow(600, 1000, 1000, 2, 0, 1000),
  neverWiderThanTheBounds: app.zoomWindow(0, 1000, 500, 4, 0, 1000),
  floored: app.zoomWindow(0, 1000, 500, 0.001, 0, 1000),
  step: app.ZOOM_STEP,
  minSpan: app.MIN_ZOOM_SPAN_S,
  invalidNumber: app.zoomWindow(null, 1000, 500, 0.5, 0, 1000),
  emptyBounds: app.zoomWindow(0, 1000, 500, 0.5, 0, 0),
  emptyWindow: app.zoomWindow(500, 500, 500, 0.5, 0, 1000),
  negativeFactor: app.zoomWindow(0, 1000, 500, -1, 0, 1000),
}}));
"""
    out = run_node(script)
    # The factor is a span multiplier, so 0.5 halves the window.
    assert out["centred"] == {"min": 250, "max": 750}
    # The centre keeps its place in the window: 800 sits at 80% before and after.
    assert out["atPointer"] == {"min": 400, "max": 900}
    assert out["atLeftEdge"] == {"min": 0, "max": 500}
    # Widening past a bound shifts the window back inside instead of shrinking it.
    assert out["shiftedOffTheRightBound"] == {"min": 200, "max": 1000}
    assert out["neverWiderThanTheBounds"] == {"min": 0, "max": 1000}
    # A minute is as far in as it goes, centred on the pointer.
    assert out["floored"] == {"min": 470, "max": 530}
    assert out["minSpan"] == 60
    assert out["step"] > 1
    # Fail closed on anything that does not describe a window.
    for key in ("invalidNumber", "emptyBounds", "emptyWindow", "negativeFactor"):
        assert out[key] is None, key


def test_detect_zoom_reports_a_widened_window_too():
    # Zooming back out after a re-query produces a window wider than the loaded
    # data, because the re-query narrowed the data to the zoom. Reporting that as
    # "not a zoom" would widen the axis and leave the stale viewport in the URL.
    script = f"""
const app = require({json.dumps(str(APP_JS))});
console.log(JSON.stringify({{
  widened: app.detectZoom(500, 1500, 1000, 1400),
  unchanged: app.detectZoom(1000, 1400, 1000, 1400),
}}));
"""
    out = run_node(script)
    assert out["widened"] == {"start": 500, "end": 1500}
    assert out["unchanged"] is None


# One fake uPlot instance, shared by the zoom contracts below. Its setScale does
# what uPlot's does -- move the scale, then fire the setScale hook -- which is
# what makes "the wheel and the buttons run the drag-zoom's path" a claim these
# tests can actually check rather than assert about the source.
FAKE_CHART = """
function makeChart(app, kind, min, max, data) {
  const chart = {
    data,
    scales: { x: { min, max } },
    listeners: [],
    setScaleCalls: [],
    over: {
      getBoundingClientRect: () => ({ left: 0, width: 1000 }),
      addEventListener: (type, handler, options) => {
        chart.listeners.push({ type, options });
        chart.handler = handler;
      },
    },
    posToVal: (pos) => {
      const scale = chart.scales.x;
      return scale.min + (pos / 1000) * (scale.max - scale.min);
    },
    setScale: (key, range) => {
      chart.setScaleCalls.push({ key, min: range.min, max: range.max });
      chart.scales.x = { min: range.min, max: range.max };
      if (kind === "analytics") app.onAnalyticsXScale(chart);
      else app.onHistoryXScale(chart);
    },
  };
  return chart;
}

// Deterministic debounce: the requery timer is captured rather than waited on,
// so the test decides when the 180ms window elapses.
function captureTimers() {
  const captured = { pending: null };
  global.setTimeout = (fn, ms) => {
    captured.pending = { fn, ms };
    return 1;
  };
  global.clearTimeout = () => {
    captured.pending = null;
  };
  return captured;
}
"""


def test_wheel_and_buttons_reach_the_backend_through_the_drag_zoom_path():
    # The contract this whole feature stands on: neither input fetches, and
    # neither debounces. Both move the chart's x scale, and everything after
    # that -- viewport, Back to live, the one debounced request -- is the path
    # a drag already used.
    script = f"""
const app = require({json.dumps(str(APP_JS))});
{FAKE_CHART}
global.document = {{ hidden: false, getElementById: () => null }};
const requests = [];
global.fetch = async (url) => {{
  requests.push(url);
  return {{ ok: true, json: async () => ({{ available: false }}) }};
}};
const timers = captureTimers();

app.state.demoMode = false;
app.state.flowView = "analytics";
app.state.range = "24h";
app.state.analytics.zoom = null;
app.state.analytics.liveExtent = {{ start: 0, end: 86400 }};
const chart = makeChart(app, "analytics", 0, 86400, [[0, 86400]]);
app.state.analytics.chart = chart;

// 1) One wheel notch forward, pointer at the middle of the plot.
let prevented = 0;
app.onZoomWheel("analytics", {{
  deltaY: -120,
  clientX: 500,
  preventDefault: () => {{ prevented += 1; }},
}});
const afterWheel = {{
  prevented,
  setScaleCalls: chart.setScaleCalls.length,
  zoom: app.state.analytics.zoom,
  requestsBeforeTimer: requests.length,
  debounceMs: timers.pending && timers.pending.ms,
}};

// 2) The debounce elapses: exactly one request, carrying the zoom window.
timers.pending.fn();
await Promise.resolve();
const afterDebounce = {{ requests: requests.slice() }};

// 3) The + button, from the window the wheel left behind.
app.onZoomButton("analytics", "in");
const afterButton = {{
  setScaleCalls: chart.setScaleCalls.length,
  zoom: app.state.analytics.zoom,
  requestsBeforeTimer: requests.length,
}};
timers.pending.fn();
await Promise.resolve();

// 4) The re-query has narrowed the loaded data down to the zoom itself. Zooming
// back out therefore asks for a window wider than the data on the chart, and
// that has to re-query too -- otherwise the - button goes dead after the first
// zoom, because the data it is clamped against is the zoom.
app.state.analytics.chart = makeChart(app, "analytics", 20000, 30000, [[20000, 30000]]);
app.onZoomButton("analytics", "out");
const afterZoomingOutOnce = {{
  zoom: app.state.analytics.zoom,
  requestsBeforeTimer: requests.length,
  pendingTimer: timers.pending !== null,
}};
timers.pending.fn();
await Promise.resolve();
const wideningRequest = requests[requests.length - 1];

// 5) Zooming out past the live window is Back to live, not a viewport that
// happens to equal it.
for (let step = 0; step < 6; step += 1) app.onZoomButton("analytics", "out");
const afterZoomingOut = {{ zoom: app.state.analytics.zoom, requests: requests.length }};

console.log(JSON.stringify({{
  afterWheel,
  afterDebounce,
  afterButton,
  afterZoomingOutOnce,
  wideningRequest,
  afterZoomingOut,
}}));
"""
    out = run_node(f"(async () => {{{script}}})();")

    # The wheel cancels the scroll, moves the scale once, and records a viewport
    # through the scale hook -- without fetching anything itself.
    assert out["afterWheel"]["prevented"] == 1
    assert out["afterWheel"]["setScaleCalls"] == 1
    assert out["afterWheel"]["zoom"] == {"start": 16200, "end": 70200}
    assert out["afterWheel"]["requestsBeforeTimer"] == 0
    assert out["afterWheel"]["debounceMs"] == 180

    # One request when the shared debounce elapses, carrying the zoom window.
    assert len(out["afterDebounce"]["requests"]) == 1
    url = out["afterDebounce"]["requests"][0]
    assert "/api/analytics/series" in url
    assert "start=16200" in url
    assert "end=70200" in url
    assert "range=" not in url

    # The button behaves identically: one scale move, no fetch of its own.
    assert out["afterButton"]["setScaleCalls"] == 2
    assert out["afterButton"]["zoom"] == {"start": 26325, "end": 60075}
    assert out["afterButton"]["requestsBeforeTimer"] == 1

    # Widening past the loaded data is a zoom too, and it re-queries.
    assert out["afterZoomingOutOnce"]["zoom"] == {"start": 17000, "end": 33000}
    assert out["afterZoomingOutOnce"]["requestsBeforeTimer"] == 2
    assert out["afterZoomingOutOnce"]["pendingTimer"] is True
    assert "start=17000" in out["wideningRequest"]
    assert "end=33000" in out["wideningRequest"]

    # Zooming out to the live window returns to live -- the same exit the
    # Back to live button uses.
    assert out["afterZoomingOut"]["zoom"] is None
    assert out["afterZoomingOut"]["requests"] == 4


def test_the_wheel_listener_is_non_passive_so_the_page_cannot_scroll_away():
    # A passive listener cannot cancel the scroll, and a chart that zooms while
    # the page slides out from under it is worse than no wheel at all.
    script = f"""
const app = require({json.dumps(str(APP_JS))});
{FAKE_CHART}
global.document = {{ hidden: false, getElementById: () => null }};
app.state.analytics.liveExtent = {{ start: 0, end: 86400 }};
const chart = makeChart(app, "analytics", 0, 86400, [[0, 86400]]);
app.state.analytics.chart = chart;
app.bindWheelZoom("analytics", chart);

let prevented = 0;
chart.handler({{ deltaY: -120, clientX: 250, preventDefault: () => {{ prevented += 1; }} }});

console.log(JSON.stringify({{
  registered: chart.listeners,
  prevented,
  window: chart.scales.x,
}}));
"""
    out = run_node(script)
    assert out["registered"] == [{"type": "wheel", "options": {"passive": False}}]
    assert out["prevented"] == 1
    # Anchored on the pointer at 25% of the plot: 21600 keeps its quarter-way
    # place in the narrowed window. Centred on the middle it would be
    # 16200..70200 instead.
    assert out["window"] == {"min": 8100, "max": 62100}


def test_zoom_controls_are_disabled_until_something_is_loaded():
    # Fail closed: with no chart and no loaded window there is nothing to zoom.
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const nodes = {{
  analyticsZoomIn: {{ disabled: false }},
  analyticsZoomOut: {{ disabled: false }},
  historyZoomIn: {{ disabled: false }},
  historyZoomOut: {{ disabled: false }},
  analyticsBackToLive: {{ hidden: false }},
  historyBackToLive: {{ hidden: false }},
}};
global.document = {{ hidden: false, getElementById: (id) => nodes[id] || null }};

app.state.analytics.chart = null;
app.state.analytics.liveExtent = null;
app.state.analytics.zoom = null;
app.state.history.chart = null;
app.state.history.data = null;
app.state.history.zoom = null;
app.renderZoomControls();
const empty = {{
  analytics: nodes.analyticsZoomIn.disabled,
  history: nodes.historyZoomIn.disabled,
  analyticsBackToLive: nodes.analyticsBackToLive.hidden,
  historyBackToLive: nodes.historyBackToLive.hidden,
}};

app.state.analytics.chart = {{ setScale: () => {{}} }};
app.state.analytics.liveExtent = {{ start: 0, end: 86400 }};
app.state.history.chart = {{ setScale: () => {{}} }};
app.state.history.data = {{ time: [0, 86400] }};
app.renderZoomControls();
const loaded = {{
  analytics: nodes.analyticsZoomOut.disabled,
  history: nodes.historyZoomOut.disabled,
}};

console.log(JSON.stringify({{ empty, loaded }}));
"""
    out = run_node(script)
    assert out["empty"]["analytics"] is True
    assert out["empty"]["history"] is True
    assert out["empty"]["analyticsBackToLive"] is True
    assert out["empty"]["historyBackToLive"] is True
    assert out["loaded"]["analytics"] is False
    assert out["loaded"]["history"] is False


def test_history_zoom_pauses_the_refresh_and_never_requeries():
    # The History panel has no finer profile to ask for: its zoom only rescales
    # the axis. So it must not fetch, and the 30s refresh -- which resets the
    # axis -- has to wait while a window is being read.
    script = f"""
const app = require({json.dumps(str(APP_JS))});
{FAKE_CHART}
const nodes = {{ historyBackToLive: {{ hidden: true }} }};
global.document = {{ hidden: false, getElementById: (id) => nodes[id] || null }};
const requests = [];
global.fetch = async (url) => {{
  requests.push(url);
  return {{ ok: true, json: async () => ({{ time: [], series: {{}} }}) }};
}};
const timers = captureTimers();

app.state.demoMode = false;
app.state.flowView = "aggregated";
app.state.history.zoom = null;
app.state.history.data = {{ time: [0, 86400], series: {{}} }};
const chart = makeChart(app, "history", 0, 86400, [[0, 86400]]);
app.state.history.chart = chart;

const live = {{
  refresh: app.historyShouldAutoRefresh(),
  buttonHidden: nodes.historyBackToLive.hidden,
}};

app.onZoomWheel("history", {{ deltaY: -120, clientX: 500, preventDefault: () => {{}} }});
const zoomed = {{
  zoom: app.state.history.zoom,
  refresh: app.historyShouldAutoRefresh(),
  buttonHidden: nodes.historyBackToLive.hidden,
  url: app.historyFetchUrl(),
  requests: requests.length,
  pendingTimer: timers.pending !== null,
}};

app.historyBackToLive();
await Promise.resolve();
const back = {{
  zoom: app.state.history.zoom,
  refresh: app.historyShouldAutoRefresh(),
  buttonHidden: nodes.historyBackToLive.hidden,
  requests: requests.slice(),
}};

console.log(JSON.stringify({{ live, zoomed, back }}));
"""
    out = run_node(f"(async () => {{{script}}})();")
    assert out["live"]["refresh"] is True
    assert out["live"]["buttonHidden"] is True

    # Zoomed: a viewport, a visible way back, no request and no pending one.
    assert out["zoomed"]["zoom"] == {"start": 16200, "end": 70200}
    assert out["zoomed"]["refresh"] is False
    assert out["zoomed"]["buttonHidden"] is False
    assert out["zoomed"]["requests"] == 0
    assert out["zoomed"]["pendingTimer"] is False
    # The History URL never carries a window: there is only one profile here.
    assert "start=" not in out["zoomed"]["url"]
    assert "end=" not in out["zoomed"]["url"]

    # Back to live is the reset, and it reloads the live window once.
    assert out["back"]["zoom"] is None
    assert out["back"]["refresh"] is True
    assert out["back"]["buttonHidden"] is True
    assert len(out["back"]["requests"]) == 1
    assert "/api/history/series" in out["back"]["requests"][0]
    assert "start=" not in out["back"]["requests"][0]


def test_a_history_refresh_keeps_the_window_the_reader_is_looking_at():
    # uPlot's setData resets the x scale, and the History panel reloads whenever
    # its view is entered -- switching to Devices and back is enough. Without
    # re-applying the viewport the axis snaps to the live window while the zoom
    # state, and the Back to live button with it, say otherwise.
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const container = {{ clientWidth: 600, innerHTML: "" }};
const nodes = {{
  historyChart: container,
  historyEmpty: {{ hidden: false, textContent: "" }},
  historyBackToLive: {{ hidden: true }},
  historyZoomIn: {{ disabled: true }},
  historyZoomOut: {{ disabled: true }},
}};
global.document = {{ hidden: false, getElementById: (id) => nodes[id] || null }};

// A fake uPlot with the one behaviour under test: setData resets the x scale.
global.uPlot = function (opts, data, host) {{
  this.data = data;
  this.scales = {{ x: {{ min: data[0][0], max: data[0][data[0].length - 1] }} }};
  this.over = {{ addEventListener: () => {{}} }};
  this.setData = (next) => {{
    this.data = next;
    this.scales.x = {{ min: next[0][0], max: next[0][next[0].length - 1] }};
    if (opts.hooks && opts.hooks.setScale) {{
      opts.hooks.setScale.forEach((hook) => hook(this, "x"));
    }}
  }};
  this.setScale = (key, range) => {{
    this.scales.x = {{ min: range.min, max: range.max }};
    if (opts.hooks && opts.hooks.setScale) {{
      opts.hooks.setScale.forEach((hook) => hook(this, "x"));
    }}
  }};
  this.destroy = () => {{}};
}};

app.state.flowView = "aggregated";
app.state.history.device = "";
app.state.history.zoom = null;
app.state.history.data = {{
  time: [0, 43200, 86400],
  series: {{ pv: [1, 2, 3], output: [1, 2, 3], battery: [1, 2, 3] }},
}};
app.renderHistoryChart();

// The reader zooms into the middle of the day.
app.onZoomButton("history", "in");
const zoomed = {{
  zoom: app.state.history.zoom,
  window: app.state.history.chart.scales.x,
  buttonHidden: nodes.historyBackToLive.hidden,
}};

// Leaving the view and coming back reloads the same window and re-renders
// through the reuse path.
app.renderHistoryChart();
const afterReload = {{
  zoom: app.state.history.zoom,
  window: app.state.history.chart.scales.x,
  buttonHidden: nodes.historyBackToLive.hidden,
}};

console.log(JSON.stringify({{ zoomed, afterReload }}));
"""
    out = run_node(script)
    assert out["zoomed"]["zoom"] == {"start": 16200, "end": 70200}
    assert out["zoomed"]["window"] == {"min": 16200, "max": 70200}
    assert out["zoomed"]["buttonHidden"] is False

    # The viewport, the axis and the button still agree after the reload.
    assert out["afterReload"]["zoom"] == {"start": 16200, "end": 70200}
    assert out["afterReload"]["window"] == {"min": 16200, "max": 70200}
    assert out["afterReload"]["buttonHidden"] is False


def test_an_incomplete_schema_shows_its_own_hint():
    """A cause with a fix must reach the operator, not the generic copy.

    The info state only rendered a hint for `reason: "unreachable"`, so a
    missing analytics bucket -- the one cause an operator can act on -- would
    have fallen back to "not configured" and hidden the command that fixes it.
    """

    script = f"""
const app = require({json.dumps(str(APP_JS))});
const made = [];
function node(tag) {{
  const element = {{ tag, textContent: "", style: {{}}, hidden: false }};
  element.querySelector = (selector) => made.find((item) => item.tag === selector) || null;
  return element;
}}
const heading = node("h3");
const detail = node("p");
made.push(heading, detail);
const unavailable = node("div");
const nodes = {{ analyticsUnavailable: unavailable, analyticsBody: {{ hidden: false }} }};
global.document = {{ hidden: false, getElementById: (id) => nodes[id] || null }};

app.setAnalyticsAvailable(false, {{
  available: false,
  reason: "schema_incomplete",
  missing_buckets: ["ems_1m"],
  hint: "Analytics history is enabled, but its downsampled buckets are missing.\\nRun: python3 emsctl.py influx sync",
}});
console.log(JSON.stringify({{ heading: heading.textContent, detail: detail.textContent }}));
"""
    out = run_node(script)

    assert "schema" in out["heading"].lower()
    assert "influx sync" in out["detail"]


def test_every_integrating_kpi_has_its_series_in_the_tab_that_shows_it():
    """A KPI integrates the fetched series, and a tab fetches only its own.

    A KPI whose series is not in its tab's list reads an empty array and renders
    a permanent zero, which looks like a measurement rather than a wiring
    mistake. Overlays do not count: they are off by default.
    """

    out = run_node(
        f"""
const app = require({json.dumps(str(APP_JS))});
console.log(JSON.stringify({{
  tabs: app.ANALYTICS_TABS.map((t) => ({{id: t.id, series: t.series, kpis: t.kpis}})),
  live: Object.entries(app.ANALYTICS_KPIS)
    .filter(([, kpi]) => kpi.live)
    .map(([id]) => id),
}}));
"""
    )

    source = APP_JS.read_text(encoding="utf-8")
    live = set(out["live"])
    missing = []
    for tab in out["tabs"]:
        for kpi in tab["kpis"]:
            if kpi in live:
                continue
            match = re.search(
                rf'^\s*{re.escape(kpi)}:.*?(?:integrateSeries|seriesPeak)\(d, "([a-z_]+)"',
                source,
                re.MULTILINE,
            )
            assert match, f"KPI {kpi} neither live nor reading a series"
            if match.group(1) not in tab["series"]:
                missing.append(f"{tab['id']}/{kpi} needs series {match.group(1)}")

    assert missing == []


def test_a_history_range_past_the_retention_says_how_much_it_shows():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const nodes = {{
  historyChart: {{ clientWidth: 600, innerHTML: "" }},
  historyEmpty: {{ hidden: true, textContent: "" }},
  historyRetention: {{ hidden: true, textContent: "" }},
}};
global.document = {{ hidden: false, getElementById: (id) => nodes[id] || null }};
global.uPlot = function () {{
  this.scales = {{ x: {{}} }};
  this.over = {{ addEventListener: () => {{}} }};
  this.setData = () => {{}};
  this.setScale = () => {{}};
  this.destroy = () => {{}};
}};
app.state.flowView = "aggregated";
app.state.history.data = {{
  time: [0, 60], series: {{ pv: [1, 2], output: [1, 2], battery: [1, 2] }},
  meta: {{ retention_hours: 48 }},
}};
app.renderHistoryChart();
const truncated = {{ ...nodes.historyRetention }};
app.state.history.data = {{
  time: [0, 60], series: {{ pv: [1, 2], output: [1, 2], battery: [1, 2] }}, meta: {{}},
}};
app.renderHistoryChart();
console.log(JSON.stringify({{ truncated, full: nodes.historyRetention }}));
"""
    out = run_node(script)

    assert out["truncated"]["hidden"] is False
    assert "48 h" in out["truncated"]["textContent"]
    assert "dashboard.history_hours" in out["truncated"]["textContent"]
    assert out["full"]["hidden"] is True


def test_the_history_panel_has_a_place_for_the_retention_note():
    html = (ROOT / "dashboard" / "static" / "index.html").read_text(encoding="utf-8")

    assert 'id="historyRetention"' in html


def _kpi_cards(data, *, tab="overview", device="", snapshot=None, range_token="24h"):
    """The KPI cards of ``tab`` as {id: [label, value]}."""
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const host = {{ innerHTML: "" }};
global.document = {{ getElementById: (id) => (id === "analyticsKpis" ? host : null) }};
app.state.snapshot = {json.dumps(snapshot)};
app.state.analytics.data = {json.dumps(data)};
app.state.analytics.tab = {json.dumps(tab)};
app.state.analytics.device = {json.dumps(device)};
app.state.range = {json.dumps(range_token)};
app.renderAnalyticsKpis();
const cards = {{}};
for (const match of host.innerHTML.matchAll(
  /data-analytics-kpi="([^"]+)"><span class="analytics-kpi-label">([^<]*)<\\/span><span class="analytics-kpi-value">([^<]*)</g
)) {{
  cards[match[1]] = [match[2], match[3]];
}}
console.log(JSON.stringify(cards));
"""
    return run_node(script)


def _series_payload(start_iso, end_iso, range_name, step=1800, points=4):
    start = 1_780_000_000
    return {
        "range": range_name,
        "start": start_iso,
        "end": end_iso,
        "time": [start + index * step for index in range(points)],
        "series": {
            "pv": [1000] * points,
            "output": [500] * points,
            "battery": [200] * points,
        },
    }


def test_a_custom_range_names_the_window_its_kpis_integrate():
    """The period button still reads 24h; the KPIs integrate three days."""
    data = _series_payload("2026-06-01T00:00:00+00:00", "2026-06-04T00:00:00+00:00", "custom")
    cards = _kpi_cards(data, tab="pv", range_token="24h")
    assert cards["pv"][0] == "PV · 3d"
    assert cards["output"][0] == "Output · 3d"
    assert cards["pvPeak"][0] == "PV Peak · 3d"
    assert all("24h" not in label for label, _ in cards.values())


def test_a_zoomed_window_names_its_span():
    data = _series_payload("2026-06-01T10:00:00+00:00", "2026-06-01T11:30:00+00:00", "custom")
    cards = _kpi_cards(data, tab="battery", range_token="7d")
    assert cards["charge"][0] == "Charge · 1.5h"
    assert cards["discharge"][0] == "Discharge · 1.5h"

    short = _series_payload("2026-06-01T10:00:00+00:00", "2026-06-01T10:20:00+00:00", "custom")
    assert _kpi_cards(short, tab="battery")["charge"][0] == "Charge · 20m"


def test_a_period_button_keeps_its_own_name():
    data = _series_payload("2026-06-01T00:00:00+00:00", "2026-06-08T00:00:00+00:00", "7d")
    cards = _kpi_cards(data, tab="pv", range_token="7d")
    assert cards["pv"][0] == "PV · 7d"
    assert cards["pvPeak"][0] == "PV Peak · 7d"


def _kpi_cards_in_turn(steps, *, tab):
    """The KPI cards after each (device, data) step, rendered in one page."""
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const host = {{ innerHTML: "" }};
global.document = {{ getElementById: (id) => (id === "analyticsKpis" ? host : null) }};
app.state.snapshot = {{ average_soc: 50, devices: {{ WR1: {{ soc: 80, ac_mode: 2 }} }} }};
app.state.analytics.tab = {json.dumps(tab)};
const rendered = [];
for (const [device, data] of {json.dumps(steps)}) {{
  app.state.analytics.device = device;
  app.state.analytics.data = data;
  app.renderAnalyticsKpis();
  const cards = {{}};
  for (const match of host.innerHTML.matchAll(
    /data-analytics-kpi="([^"]+)"><span class="analytics-kpi-label">([^<]*)<\\/span><span class="analytics-kpi-value">([^<]*)</g
  )) {{
    cards[match[1]] = [match[2], match[3]];
  }}
  rendered.push(cards);
}}
console.log(JSON.stringify(rendered));
"""
    return run_node(script)


def _custom_range_answer(devices, pv_w, grid_w):
    """A custom-range answer; the device filter changes its values, not its axis."""
    payload = _series_payload("2026-06-01T00:00:00+00:00", "2026-06-04T00:00:00+00:00", "custom")
    payload["source"] = "influxdb"
    payload["devices"] = devices
    payload["series"] = {"pv": [pv_w] * 4, "output": [pv_w / 2] * 4, "grid": [grid_w] * 4}
    return payload


def test_a_device_chosen_under_a_custom_range_gets_its_own_energy_kpis():
    every, one = _kpi_cards_in_turn(
        [["", _custom_range_answer([], 2000, 400)], ["WR1", _custom_range_answer(["WR1"], 500, 400)]],
        tab="pv",
    )
    assert every["pv"] == ["PV · 3d", "3.0 kWh"]
    assert one["pv"] == ["PV · 3d", "750 Wh"]
    assert one["output"] == ["Output · 3d", "375 Wh"]
    assert one["soc"] == ["Current SoC", "80%"]

    back = _kpi_cards_in_turn(
        [["WR1", _custom_range_answer(["WR1"], 500, 400)], ["", _custom_range_answer([], 2000, 400)]],
        tab="pv",
    )[1]
    assert back["pv"] == ["PV · 3d", "3.0 kWh"]


def test_the_meter_kpis_name_no_device_when_one_is_chosen():
    """Grid Import, Grid Export and Home read the meter whatever the filter."""
    cards = _kpi_cards_in_turn([["WR1", _custom_range_answer(["WR1"], 500, 400)]], tab="grid")[0]
    assert cards["gridImport"] == ["Grid Import · 3d", "600 Wh"]
    assert cards["gridExport"][0] == "Grid Export · 3d"
    assert cards["home"][0] == "Home · 3d"
    assert all("WR1" not in label for label, _ in cards.values())


def _two_answers_in_flight(order):
    """A 30-s refresh for every device in flight when the operator picks WR1.

    The two answers are resolved by hand in ``order`` ("choice" or "refresh"
    first); after each the KPI cards, the device the data describes and
    whether the loading note shows are recorded.
    """
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const nodes = {{ analyticsKpis: {{ innerHTML: "" }}, analyticsLoading: {{ hidden: true }} }};
global.document = {{ hidden: false, getElementById: (id) => nodes[id] || null, querySelectorAll: () => [] }};
const pending = [];
global.fetch = (url) => new Promise((resolve) => pending.push({{ url, resolve }}));
const answers = {{
  refresh: {json.dumps(_custom_range_answer([], 2000, 400))},
  choice: {json.dumps(_custom_range_answer(["WR1"], 500, 400))},
}};
app.state.snapshot = {{ average_soc: 50, devices: {{ WR1: {{ soc: 80, ac_mode: 2 }} }} }};
app.state.analytics.tab = "pv";
app.state.analytics.custom = {{ active: true, start: 1780000000, end: 1780259200 }};
(async () => {{
  app.state.analytics.device = "";
  const loads = {{ refresh: app.loadAnalytics(false) }};
  app.state.analytics.device = "WR1";
  loads.choice = app.loadAnalytics();
  const requests = {{ refresh: pending[0], choice: pending[1] }};
  const steps = [];
  for (const name of {json.dumps(order)}) {{
    requests[name].resolve({{ ok: true, json: async () => answers[name] }});
    await loads[name];
    const cards = {{}};
    for (const match of nodes.analyticsKpis.innerHTML.matchAll(
      /data-analytics-kpi="([^"]+)"><span class="analytics-kpi-label">[^<]*<\\/span><span class="analytics-kpi-value">([^<]*)</g
    )) {{
      cards[match[1]] = match[2];
    }}
    const data = app.state.analytics.data;
    steps.push({{ cards, devices: data ? data.devices : null, loading: !nodes.analyticsLoading.hidden }});
  }}
  console.log(JSON.stringify({{ urls: pending.map((request) => request.url), steps }}));
}})();
"""
    return run_node(script)


def test_an_older_answer_landing_last_does_not_replace_the_device_chosen_since():
    """The refresh for every device answers after WR1's own answer, and is dropped."""
    result = _two_answers_in_flight(["choice", "refresh"])
    assert "devices=WR1" not in result["urls"][0]
    assert "devices=WR1" in result["urls"][1]
    after_choice, after_refresh = result["steps"]
    assert after_choice["cards"]["pv"] == "750 Wh"
    assert after_refresh == after_choice
    assert after_refresh["devices"] == ["WR1"]
    assert after_refresh["cards"] == {
        "pv": "750 Wh", "pvPeak": "500 W", "output": "375 Wh", "soc": "80%"
    }


def test_an_older_answer_landing_first_is_not_shown_while_the_choice_loads():
    after_refresh, after_choice = _two_answers_in_flight(["refresh", "choice"])["steps"]
    assert after_refresh == {"cards": {}, "devices": None, "loading": True}
    assert after_choice["devices"] == ["WR1"]
    assert after_choice["cards"]["pv"] == "750 Wh"
    assert after_choice["loading"] is False


WR1_SHOWN = {"pv": "750 Wh", "data": True, "pvSeries": 500, "loading": False, "bodyHidden": False}


def _one_selection_in_turn(events, answers=None):
    """The operator's pick of WR1 and the 30-s refreshes for it, settled by hand.

    ``events`` holds ``["pick"]`` or ``["refresh"]`` to start a request, and
    ``["answer", n]``, ``["503", n]`` or ``["fail", n]`` to settle request
    ``n``; ``answers[n]`` is the body request ``n`` answers with (WR1's
    750-Wh answer by default). After each settlement the PV card, whether and
    which data is held, whether the loading note shows and whether the chart
    body is hidden are recorded.
    """
    if answers is None:
        answers = [_custom_range_answer(["WR1"], 500, 400)] * len(events)
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const nodes = {{ analyticsKpis: {{ innerHTML: "" }}, analyticsLoading: {{ hidden: true }},
  analyticsBody: {{ hidden: false }}, analyticsUnavailable: {{ hidden: true }} }};
global.document = {{ hidden: false, getElementById: (id) => nodes[id] || null, querySelectorAll: () => [] }};
const pending = [];
global.fetch = (url) => new Promise((resolve, reject) => pending.push({{ url, resolve, reject }}));
const answers = {json.dumps(answers)};
app.state.snapshot = {{ average_soc: 50, devices: {{ WR1: {{ soc: 80, ac_mode: 2 }} }} }};
app.state.analytics.tab = "pv";
app.state.analytics.custom = {{ active: true, start: 1780000000, end: 1780259200 }};
app.state.analytics.device = "WR1";
(async () => {{
  const loads = [];
  const steps = [];
  for (const [kind, n] of {json.dumps(events)}) {{
    if (kind === "pick") loads.push(app.loadAnalytics());
    if (kind === "refresh") loads.push(app.loadAnalytics(false));
    if (kind === "answer") pending[n].resolve({{ ok: true, json: async () => answers[n] }});
    if (kind === "503") pending[n].resolve({{ ok: false, json: async () => ({{ error: "history_unavailable" }}) }});
    if (kind === "fail") pending[n].reject(new Error("network"));
    if (kind === "pick" || kind === "refresh") continue;
    await loads[n];
    const pv = nodes.analyticsKpis.innerHTML.match(
      /data-analytics-kpi="pv"><span class="analytics-kpi-label">[^<]*<\\/span><span class="analytics-kpi-value">([^<]*)</
    );
    const data = app.state.analytics.data;
    steps.push({{
      settled: `${{kind}} ${{n}}`,
      pv: pv ? pv[1] : null,
      data: !!data,
      pvSeries: data && data.series ? data.series.pv[0] : null,
      loading: !nodes.analyticsLoading.hidden,
      bodyHidden: !!nodes.analyticsBody.hidden,
    }});
  }}
  console.log(JSON.stringify({{ urls: [...new Set(pending.map((request) => request.url))], steps }}));
}})();
"""
    return run_node(script)


def test_answers_slower_than_the_refresh_are_each_shown_for_the_same_selection():
    """Every answer takes longer than 30 s, so each refresh starts before the last answer."""
    result = _one_selection_in_turn(
        [["pick"], ["refresh"], ["answer", 0], ["refresh"], ["answer", 1], ["refresh"], ["answer", 2]]
    )
    assert len(result["urls"]) == 1
    assert result["steps"] == [
        {"settled": f"answer {n}", **WR1_SHOWN} for n in range(3)
    ]


def test_a_refresh_failing_before_the_choice_answers_does_not_discard_that_answer():
    result = _one_selection_in_turn([["pick"], ["refresh"], ["fail", 1], ["answer", 0]])
    assert len(result["urls"]) == 1
    assert result["steps"][-1] == {"settled": "answer 0", **WR1_SHOWN}


def test_an_older_failure_landing_after_a_newer_answer_keeps_the_newer_answer():
    """The pick's 503 lands after the refresh started behind it has answered; the server answers on a thread each."""
    result = _one_selection_in_turn([["pick"], ["refresh"], ["answer", 1], ["503", 0]])
    assert len(result["urls"]) == 1
    assert result["steps"] == [
        {"settled": "answer 1", **WR1_SHOWN},
        {"settled": "503 0", **WR1_SHOWN},
    ]


def test_an_older_unreachable_answer_landing_after_a_newer_answer_keeps_the_chart():
    """The pick's "InfluxDB unreachable" lands after the refresh started behind it has answered."""
    unreachable = {"available": False, "reason": "unreachable", "source": "influxdb", "hint": "x"}
    result = _one_selection_in_turn(
        [["pick"], ["refresh"], ["answer", 1], ["answer", 0]],
        [unreachable, _custom_range_answer(["WR1"], 500, 400)],
    )
    assert result["steps"][-1] == {"settled": "answer 0", **WR1_SHOWN}


def test_an_older_answer_landing_after_a_newer_one_does_not_replace_it():
    """The pick's answer lands after the refresh started behind it has answered with newer data."""
    result = _one_selection_in_turn(
        [["pick"], ["refresh"], ["answer", 1], ["answer", 0]],
        [_custom_range_answer(["WR1"], 500, 400), _custom_range_answer(["WR1"], 1000, 400)],
    )
    newer = {"pv": "1.5 kWh", "data": True, "pvSeries": 1000, "loading": False, "bodyHidden": False}
    assert result["steps"] == [{"settled": "answer 1", **newer}, {"settled": "answer 0", **newer}]


def test_the_soc_card_follows_the_device_filter_like_the_role_card():
    snapshot = {
        "average_soc": 50,
        "devices": {"WR1": {"soc": 80, "ac_mode": 1}, "WR2": {"soc": 20, "ac_mode": 2}},
    }
    data = _series_payload("2026-06-01T00:00:00+00:00", "2026-06-02T00:00:00+00:00", "24h")
    every = _kpi_cards(data, snapshot=snapshot)
    assert every["soc"] == ["Current SoC", "50%"]
    assert every["role"] == ["Runtime Role", "Mixed"]

    one = _kpi_cards(data, device="WR1", snapshot=snapshot)
    assert one["soc"] == ["Current SoC", "80%"]
    assert one["role"] == ["Runtime Role", "AC Input"]

    unknown = _kpi_cards(
        data, device="WR1", snapshot={"average_soc": 50, "devices": {"WR1": {"ac_mode": 2}}}
    )
    assert unknown["soc"] == ["Current SoC", "--"]


def test_a_read_only_device_without_a_charge_level_shows_none_and_has_no_runtime_role():
    external = {"soc": 0, "soc_reported": False, "ac_mode": 0, "read_only": True}
    snapshot = {"average_soc": 80, "devices": {"WR1": {"soc": 80, "ac_mode": 1}, "Garage": external}}
    data = _series_payload("2026-06-01T00:00:00+00:00", "2026-06-02T00:00:00+00:00", "24h")

    every = _kpi_cards(data, snapshot=snapshot)
    assert every["soc"] == ["Current SoC", "80%"]
    assert every["role"] == ["Runtime Role", "AC Input"]

    garage = _kpi_cards(data, device="Garage", snapshot=snapshot)
    assert garage["soc"] == ["Current SoC", "--"]
    assert garage["role"] == ["Runtime Role", "--"]

    controlled = _kpi_cards(
        data, device="WR1", snapshot={"devices": {"WR1": {"soc": 0, "soc_reported": False}}}
    )
    assert controlled["soc"] == ["Current SoC", "--"]


_R7_GOOD = _custom_range_answer(["WR1"], 500, 400)
_R7_NEWER = _custom_range_answer(["WR1"], 1000, 400)
_R7_NEWEST = _custom_range_answer(["WR1"], 1500, 400)
_R7_UNREACHABLE = {"available": False, "reason": "unreachable", "source": "influxdb", "hint": "x"}


def test_an_unavailable_newer_answer_does_not_block_older_data():
    result = _one_selection_in_turn(
        [["pick"], ["refresh"], ["answer", 1], ["answer", 0]], [_R7_GOOD, _R7_UNREACHABLE]
    )
    assert result["steps"][-1]["pvSeries"] == 500, result["steps"]


def test_three_answers_in_flight_landing_in_order_are_each_shown():
    result = _one_selection_in_turn(
        [["pick"], ["refresh"], ["refresh"], ["answer", 0], ["answer", 1], ["answer", 2]],
        [_R7_GOOD, _R7_NEWER, _R7_NEWEST],
    )
    assert [s["pvSeries"] for s in result["steps"]] == [500, 1000, 1500], result["steps"]


def test_an_old_answer_for_the_selection_is_shown_after_another_selection_showed_data():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const nodes = {{ analyticsKpis: {{ innerHTML: "" }}, analyticsLoading: {{ hidden: true }},
  analyticsBody: {{ hidden: false }}, analyticsUnavailable: {{ hidden: true }} }};
global.document = {{ hidden: false, getElementById: (id) => nodes[id] || null, querySelectorAll: () => [] }};
const pending = [];
global.fetch = (url) => new Promise((resolve, reject) => pending.push({{ url, resolve, reject }}));
const good = {json.dumps(_R7_GOOD)};
const other = {json.dumps(_custom_range_answer(["WR2"], 900, 400))};
app.state.snapshot = {{ average_soc: 50, devices: {{ WR1: {{ soc: 80, ac_mode: 2 }}, WR2: {{ soc: 70, ac_mode: 2 }} }} }};
app.state.analytics.tab = "pv";
app.state.analytics.custom = {{ active: true, start: 1780000000, end: 1780259200 }};
app.state.analytics.device = "WR1";
(async () => {{
  const l0 = app.loadAnalytics();
  app.state.analytics.device = "WR2";
  const l1 = app.loadAnalytics();
  pending[1].resolve({{ ok: true, json: async () => other }}); await l1;
  app.state.analytics.device = "WR1";
  const l2 = app.loadAnalytics();
  pending[0].resolve({{ ok: true, json: async () => good }}); await l0;
  const d = app.state.analytics.data;
  console.log(JSON.stringify({{ devices: d ? d.devices : null }}));
}})();
"""
    assert run_node(script)["devices"] == ["WR1"]


def test_three_requests_newest_fails_then_oldest_data_lands():
    """pick(0) slow; refresh(1) answers data; refresh(2) answers 503; then pick(0)'s data lands."""
    result = _one_selection_in_turn(
        [["pick"], ["refresh"], ["refresh"], ["answer", 1], ["503", 2], ["answer", 0]],
        [_R7_GOOD, _R7_NEWER, _R7_GOOD],
    )
    assert result["steps"][-1]["data"] is True, result["steps"]


def test_a_b_a_detour_whose_b_answer_failed():
    """A: pick(0) slow, refresh(1) answers data; operator picks B (2), B answers 503;
    operator picks A again (3), which fails; then A's pick(0) data lands."""
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const nodes = {{ analyticsKpis: {{ innerHTML: "" }}, analyticsLoading: {{ hidden: true }},
  analyticsBody: {{ hidden: false }}, analyticsUnavailable: {{ hidden: true }} }};
global.document = {{ hidden: false, getElementById: (id) => nodes[id] || null, querySelectorAll: () => [] }};
const pending = [];
global.fetch = (url) => new Promise((resolve, reject) => pending.push({{ url, resolve, reject }}));
const good = {json.dumps(_R7_GOOD)};
const newer = {json.dumps(_R7_NEWER)};
app.state.snapshot = {{ average_soc: 50, devices: {{ WR1: {{ soc: 80, ac_mode: 2 }}, WR2: {{ soc: 70, ac_mode: 2 }} }} }};
app.state.analytics.tab = "pv";
app.state.analytics.custom = {{ active: true, start: 1780000000, end: 1780259200 }};
app.state.analytics.device = "WR1";
const read = (label) => {{ const d = app.state.analytics.data; return {{ label, device: app.state.analytics.device,
  data: !!d, pvSeries: d && d.series ? d.series.pv[0] : null, loading: !nodes.analyticsLoading.hidden }}; }};
(async () => {{
  const steps = [];
  const l0 = app.loadAnalytics();
  const l1 = app.loadAnalytics(false);
  pending[1].resolve({{ ok: true, json: async () => newer }}); await l1; steps.push(read("A refresh(1) data"));
  app.state.analytics.device = "WR2";
  const l2 = app.loadAnalytics();
  pending[2].resolve({{ ok: false, json: async () => ({{ error: "history_unavailable" }}) }}); await l2; steps.push(read("B pick(2) 503"));
  app.state.analytics.device = "WR1";
  const l3 = app.loadAnalytics();
  pending[3].reject(new Error("network")); await l3; steps.push(read("A pick(3) fails"));
  pending[0].resolve({{ ok: true, json: async () => good }}); await l0; steps.push(read("A pick(0) data lands"));
  console.log(JSON.stringify({{ urls: pending.map((p) => p.url), steps }}));
}})();
"""
    result = run_node(script)
    assert result["steps"][-1]["data"] is True, result["steps"]


def test_older_data_is_shown_after_a_newer_unavailable_replaced_newer_data():
    """pick(0) slow; refresh(1) answers data; refresh(2) answers "unavailable"; then pick(0)'s data lands."""

    result = _one_selection_in_turn(
        [["pick"], ["refresh"], ["refresh"], ["answer", 1], ["answer", 2], ["answer", 0]],
        [_R7_GOOD, _R7_NEWER, _R7_UNREACHABLE],
    )

    assert result["steps"][1]["bodyHidden"] is True
    assert result["steps"][-1]["data"] is True, result["steps"]
    assert result["steps"][-1]["bodyHidden"] is False, result["steps"]


def test_a_dropped_older_answer_leaves_the_loading_note_of_a_pending_pick():
    """refresh(0) slow; refresh(1) answers; the operator re-picks the same selection (2); refresh(0) lands."""

    result = _one_selection_in_turn(
        [["refresh"], ["refresh"], ["answer", 1], ["pick"], ["answer", 0]],
        [_R7_GOOD, _R7_NEWER],
    )

    assert result["steps"][-1]["pvSeries"] == 1000
    assert result["steps"][-1]["loading"] is True, result["steps"]
