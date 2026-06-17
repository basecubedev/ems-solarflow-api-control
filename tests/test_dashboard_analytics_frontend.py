# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "dashboard" / "static" / "app.js"
INDEX_HTML = ROOT / "dashboard" / "static" / "index.html"


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
