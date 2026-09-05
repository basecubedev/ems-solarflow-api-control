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
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.contract,
]


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


def test_control_panel_is_not_rebuilt_while_its_view_is_off_screen():
    # The live snapshot path already renders only the visible view. The auth and
    # runtime refreshes call renderControlExplain directly, and one of them is on
    # a sixty-second timer that runs whatever is on screen -- measured at one
    # rebuild per minute, 3.3 to 57.8 ms, of a subtree that at twelve devices is
    # 3606 of the aggregated view's 4065 nodes.
    script = PRELUDE + """
const doc = makeDoc();
global.document = doc;

app.state.flowView = "aggregated";
const container = doc.getElementById("controlExplainView");
container.hidden = true;

app.renderControlExplain({
  timestamp: "2026-09-05T12:00:00Z",
  control_explain: { mode: "pv_first", devices: { WR1: {} } },
}, { forceRuntimeEditor: true });

console.log(JSON.stringify({ controlExplainView: container.innerHTML }));
"""
    out = run_node(script)
    assert out["controlExplainView"] == ""


def test_control_panel_is_rebuilt_once_its_view_is_on_screen():
    # The other half of the gate: deferring a rebuild must not lose it.
    # setFlowView renders the view it switches to, so this is the state the user
    # actually arrives in.
    script = PRELUDE + """
const doc = makeDoc();
global.document = doc;

app.state.flowView = "control";
const container = doc.getElementById("controlExplainView");
container.hidden = false;

app.renderControlExplain({
  timestamp: "2026-09-05T12:00:00Z",
  control_explain: { mode: "pv_first", devices: { WR1: {} } },
});

console.log(JSON.stringify({ length: container.innerHTML.length }));
"""
    out = run_node(script)
    assert out["length"] > 0


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


def test_device_flow_same_layout_does_not_replace_svg():
    script = PRELUDE + """
let innerHtmlWrites = 0;
const container = new FakeElement("deviceFlowView");
Object.defineProperty(container, "innerHTML", {
  get() { return this._html || ""; },
  set(value) { innerHtmlWrites += 1; this._html = value; },
});
container.querySelector = (selector) => selector === "[data-device-flow-root]" && container.innerHTML ? {} : null;
container.querySelectorAll = () => [];
const doc = makeDoc({
  getElementById(id) {
    if (id === "deviceFlowView") return container;
    return new FakeElement(id);
  },
});
global.document = doc;
app.state.flowView = "devices";
app.state.deviceFlowSignature = null;
app.state.flowActivity = new Map();

const first = {
  home_load_w: 500,
  grid_power_w: 0,
  devices: { WR1: { soc: 60, pv_input_w: 120, output_w: 80, battery_power_w: 40 } },
};
const second = {
  home_load_w: 510,
  grid_power_w: 2,
  devices: { WR1: { soc: 61, pv_input_w: 125, output_w: 82, battery_power_w: 43 } },
};
app.renderDeviceFlow(first);
app.renderDeviceFlow(second);

console.log(JSON.stringify({ innerHtmlWrites }));
"""
    out = run_node(script)
    assert out["innerHtmlWrites"] == 1


def test_flow_hysteresis_and_speed_buckets_are_stable():
    script = PRELUDE + """
app.state.flowActivity = new Map();
const activeAtEight = app.flowActive("pipe", 8);
const stillActiveAtFour = app.flowActive("pipe", 4);
const inactiveAtThree = app.flowActive("pipe", 3);
const stillInactiveAtSeven = app.flowActive("pipe", 7);
const activeAgainAtEight = app.flowActive("pipe", 8);

console.log(JSON.stringify({
  activeAtEight,
  stillActiveAtFour,
  inactiveAtThree,
  stillInactiveAtSeven,
  activeAgainAtEight,
  idle: app.flowSpeedBucket(0, false),
  lowA: app.flowSpeedBucket(9, true),
  lowB: app.flowSpeedBucket(149, true),
  medium: app.flowSpeedBucket(150, true),
  high: app.flowSpeedBucket(600, true),
}));
"""
    out = run_node(script)
    assert out == {
        "activeAtEight": True,
        "stillActiveAtFour": True,
        "inactiveAtThree": False,
        "stillInactiveAtSeven": False,
        "activeAgainAtEight": True,
        "idle": "idle",
        "lowA": "low",
        "lowB": "low",
        "medium": "medium",
        "high": "high",
    }


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


# The control view puts one animated result chip on every stage of every device,
# and the border on each of them used to animate `background-position` -- a
# paint property. Chromium cannot composite that, so it repainted every chip on
# every frame: measured headed on a GPU, the control view ran at 45.2 fps with
# eight devices and 134.6 with the same animation switched off, frame p95 27.8 ms
# against 7.0. Removing the mask instead of the animation changed nothing
# (48.7 fps), so the animated paint property is the cost and the mask is not.
# Firefox was unaffected in all three arrangements.
def test_the_result_ring_animates_a_property_the_compositor_can_carry():
    css = (ROOT / "dashboard" / "static" / "styles.css").read_text(encoding="utf-8")

    assert "@keyframes controlResultRingSlide" in css, (
        "the control result ring needs a keyframe the compositor can carry"
    )
    start = css.index("@keyframes controlResultRingSlide")
    body = css[start:css.index("}", css.index("{", start) + 1) + 2]
    assert "transform" in body, "the ring has to move with a transform"
    for paint_property in ("background-position", "mask-position", "background-size"):
        assert paint_property not in body, (
            f"{paint_property} is a paint property; animating it repaints every "
            "chip on every frame"
        )

    # The chips must no longer use the paint-animated keyframe at all.
    for block in css.split("}"):
        if ".control-result" in block and "controlResultBorderFlow" in block:
            raise AssertionError(
                "a .control-result rule still animates controlResultBorderFlow, "
                "which animates background-position"
            )


def test_no_rule_animates_a_property_that_forces_a_repaint():
    """The general form of the lesson, instead of one element's name.

    `transform` and `opacity` go to the compositor. Anything that changes what a
    pixel looks like -- `background-position`, `background-size`,
    `mask-position`, `filter` -- repaints the element on every frame, and the
    cost is multiplied by however many elements the rule matches.

    The previous fix moved the control-stage chips off `background-position` and
    left the same keyframe on `.primary-button.compact::after`, on the measured
    grounds that it was one element. It is one element in the read-only
    dashboard. With authentication configured the runtime editor renders a
    submit button per stage card -- fifteen at twelve devices -- and the
    authenticated control view measured 51.9 fps at four devices and 35.7 at
    twelve, against 136.1 and 133.6 with the animation stopped, painting 4431
    times per ten seconds against 175.

    So the rule is not "this element is fine": it is that no keyframe on this
    page may move a paint property, because no rule here is guaranteed to stay
    matched by one element.
    """

    css = (ROOT / "dashboard" / "static" / "styles.css").read_text(encoding="utf-8")
    paint_properties = (
        "background-position", "background-size", "mask-position",
        "mask-size", "filter", "box-shadow", "clip-path",
    )

    offenders = []
    cursor = 0
    while True:
        start = css.find("@keyframes", cursor)
        if start == -1:
            break
        open_brace = css.index("{", start)
        depth, index = 0, open_brace
        while True:
            if css[index] == "{":
                depth += 1
            elif css[index] == "}":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        name = css[start + len("@keyframes"):open_brace].strip()
        body = css[open_brace:index]
        moved = [p for p in paint_properties if f"{p}:" in body.replace(" ", "")
                 or f"{p} :" in body]
        if moved:
            offenders.append((name, moved))
        cursor = index

    # A keyframe nobody references costs nothing. The name appearing more than
    # once means a rule names it in an `animation` or `animation-name`.
    referenced = [(name, moved) for name, moved in offenders if css.count(name) > 1]
    assert not referenced, (
        "these keyframes move a property the compositor cannot carry and are "
        f"still referenced by a rule: {referenced}. Animate a transform on a "
        "child instead; see reports/dashboard-perf/"
        "final-dashboard-performance-audit.md"
    )


def test_the_runtime_editor_is_not_rebuilt_when_it_has_not_changed():
    """`runtimeControlPanel()` takes no snapshot and reads none.

    It is built from `state.runtime` and `state.auth`, which change when
    `/api/runtime` is re-fetched and not otherwise -- but it was written into
    the DOM on every snapshot, twice a second, destroying and recreating every
    input in the runtime editor. Measured at 3.2 ms per snapshot with twelve
    devices and authentication configured.
    """
    script = PRELUDE + """
const doc = makeDoc();
global.document = doc;

const container = doc.getElementById("controlExplainView");
const runtimeMount = doc.getElementById("runtimeEditorMount");
const explainMount = doc.getElementById("controlExplainMount");
// The shell already exists, which is the steady state after the first render.
container.querySelector = (selector) =>
  selector === "#runtimeEditorMount" ? runtimeMount
  : selector === "#controlExplainMount" ? explainMount
  : null;

let runtimeWrites = 0;
let explainWrites = 0;
for (const [node, count] of [[runtimeMount, "runtime"], [explainMount, "explain"]]) {
  let stored = "";
  Object.defineProperty(node, "innerHTML", {
    configurable: true,
    get() { return stored; },
    set(value) {
      stored = String(value);
      if (count === "runtime") runtimeWrites += 1; else explainWrites += 1;
    },
  });
}

app.state.flowView = "control";
container.hidden = false;
const snapshot = {
  timestamp: "2026-09-05T12:00:00Z",
  control_explain: { mode: "pv_first", devices: { WR1: {} } },
};
app.renderControlExplain(snapshot);
app.renderControlExplain(snapshot);
app.renderControlExplain(snapshot);

console.log(JSON.stringify({ runtimeWrites, explainWrites }));
"""
    out = run_node(script)
    assert out["runtimeWrites"] == 1, (
        "the runtime editor was rebuilt %d times for three renders of unchanged "
        "runtime state" % out["runtimeWrites"]
    )
    # The explain panel is snapshot-derived and must still be written every time.
    assert out["explainWrites"] == 3


def test_the_two_animation_routes_move_the_tile_the_same_way():
    """The tile is driven by `element.animate()` where the browser has it and by
    the CSS keyframes where it does not. Two implementations of one motion, and
    nothing stopped them drifting apart: flipping a sign in either place would
    send half the browsers the wrong way and no test would notice.

    So the vectors are compared. The magnitude is a dash period in both and is
    not the question; the axis and the sign are.
    """

    css = (ROOT / "dashboard" / "static" / "styles.css").read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")

    block = re.search(r"const FLOW_TILE_VECTORS = \{(.*?)\};", source, re.S)
    assert block, "FLOW_TILE_VECTORS is missing"
    from_js = {
        name: (int(x), int(y))
        for name, x, y in re.findall(
            r"(\w+):\s*\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]", block.group(1)
        )
    }
    assert set(from_js) == {"right", "left", "down", "up"}, from_js

    def translate_arguments(text, at):
        """The arguments of the translate3d() starting at `at`, split on the
        commas that are not inside a nested calc()."""
        opened = text.index("(", at)
        depth, index = 0, opened
        while True:
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        inner = text[opened + 1:index]
        args, depth, part = [], 0, ""
        for char in inner:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            if char == "," and depth == 0:
                args.append(part.strip())
                part = ""
            else:
                part += char
        args.append(part.strip())
        return args

    for direction, vector in sorted(from_js.items()):
        name = "flowTile" + direction.capitalize()
        start = css.find("@keyframes %s " % name)
        assert start != -1, f"@keyframes {name} is missing"
        moved = css.find("translate3d", start)
        assert moved != -1 and moved < css.index("}", css.index("}", start) + 1), (
            f"@keyframes {name} does not translate3d"
        )
        # "0", "var(--tile-step)" or "calc(-1 * var(--tile-step))" per axis.
        args = translate_arguments(css, moved)
        assert len(args) == 3, (name, args)
        from_css = []
        for arg in args[:2]:
            if arg == "0":
                from_css.append(0)
            else:
                from_css.append(-1 if "-1" in arg else 1)
        assert tuple(from_css) == vector, (
            f"{name} moves {tuple(from_css)} in CSS and {vector} in "
            f"FLOW_TILE_VECTORS; a browser without element.animate() would run "
            f"this pipe the other way"
        )


def test_every_compact_button_carries_the_ring_element():
    """The travelling border is an element now, not a pseudo-element, so a
    button that does not contain it simply has no border.

    That is the same shape of mistake the border cost in the first place: a rule
    whose reach nobody re-checked. There were five of these buttons in the
    markup and two more generated, and the runtime editor multiplies one of them
    by the device count.
    """

    ring = '<span class="button-ring" aria-hidden="true"><i></i></span>'
    seen = 0
    for path in (
        ROOT / "dashboard" / "static" / "index.html",
        ROOT / "dashboard" / "static" / "app.js",
    ):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"primary-button compact", text):
            close = text.find(">", match.end())
            assert close != -1, f"{path.name}: unterminated tag near {match.start()}"
            following = text[close + 1:close + 1 + len(ring) + 8]
            assert "button-ring" in following, (
                f"{path.name}: a `primary-button compact` at offset "
                f"{match.start()} does not open with the ring element, so it "
                f"renders without its border:\n  {following!r}"
            )
            seen += 1
    assert seen >= 7, f"expected at least seven compact buttons, found {seen}"


def test_the_result_ring_still_stops_for_reduced_motion_and_animation_off():
    """Making it cheap must not make it unstoppable.

    Both switches targeted `.control-result::after` by name. A construction
    that moves the animation somewhere else silently takes the accessibility
    setting with it.
    """

    css = (ROOT / "dashboard" / "static" / "styles.css").read_text(encoding="utf-8")
    reduced = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    reduced = reduced[:reduced.index("\n}\n")]
    for ring in ("control-result-ring", "button-ring"):
        assert ring in reduced, (
            f"prefers-reduced-motion no longer reaches .{ring}"
        )
    off = css[css.index(".dashboard-animation-off"):]
    for ring in ("control-result-ring", "button-ring"):
        assert ring in off, (
            f"dashboard-animation-off no longer reaches .{ring}"
        )


# The lightweight SQLite History panel belongs only to the operational
# aggregated/devices views. setFlowView() must reload it exactly for those
# transitions and never when switching to analytics/control/energy/diagnose/logs
# (a hidden reload was an unnecessary fetch + render every tab switch). A past
# bug tested the historyVisible *function object* (always truthy) instead of the
# per-view boolean, so this guards the behavior, not just the variable name.
def test_history_reload_only_for_aggregated_and_devices_views():
    script = PRELUDE + """
const doc = makeDoc();
global.document = doc;
global.window = {
  localStorage: { getItem: () => null, setItem: () => {} },
  innerWidth: 1200,
};

// Record which fetches loadHistory() issues. loadHistory() synchronously calls
// fetch(historyFetchUrl()) (the first await), so a history reload is observable
// as a fetch to /api/history/series during the setFlowView() call.
const historyCalls = [];
global.fetch = (url) => {
  if (String(url).startsWith("/api/history/series")) {
    historyCalls.push(String(url));
  }
  return Promise.resolve({ ok: false, json: async () => ({}) });
};

// Live data path (not demo) so loadHistory() actually fetches; unauthenticated
// so switching to logs does not start a polling interval and hang node.
app.state.demoMode = false;
app.state.auth = app.state.auth || {};
app.state.auth.authenticated = false;
app.state.snapshot = null;

function transition(from, to) {
  app.state.flowView = from;
  const before = historyCalls.length;
  app.setFlowView(to, false);
  return historyCalls.length > before;
}

console.log(JSON.stringify({
  aggregated_to_devices: transition("aggregated", "devices"),
  devices_to_aggregated: transition("devices", "aggregated"),
  aggregated_to_analytics: transition("aggregated", "analytics"),
  devices_to_control: transition("devices", "control"),
  analytics_to_logs: transition("analytics", "logs"),
  aggregated_to_energy: transition("aggregated", "energy"),
  devices_to_diagnose: transition("devices", "diagnose"),
}));
"""
    out = run_node(script)
    # Operational views reload history.
    assert out["aggregated_to_devices"] is True
    assert out["devices_to_aggregated"] is True
    # Every non-history view must NOT reload history.
    assert out["aggregated_to_analytics"] is False
    assert out["devices_to_control"] is False
    assert out["analytics_to_logs"] is False
    assert out["aggregated_to_energy"] is False
    assert out["devices_to_diagnose"] is False


def test_setflowview_history_guard_uses_local_boolean_not_function():
    # Static guard: the reload condition must test the per-view boolean, not the
    # historyVisible() function object (which is always truthy). Keep the fragile
    # `if (historyVisible && previousView` pattern from coming back.
    source = APP_JS.read_text(encoding="utf-8")
    assert "if (isHistoryPanelVisible && previousView !== nextView)" in source
    assert "if (historyVisible && previousView" not in source
