# SPDX-License-Identifier: AGPL-3.0-or-later
import json
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
    assert set(out["overlays"]) == {"soc", "target", "grid"}


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
