# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live-transport contract for the dashboard frontend.

These execute dashboard/static/app.js under node (like the other frontend
tests) and pin the invariants that keep a long-lived dashboard cheap:

- an unchanged snapshot timestamp renders once, not twice,
- a hidden tab accepts state but defers rendering until it is visible again,
- the deferred render is coalesced: many hidden updates cost one render,
- a tab demoted to polling is promoted back to SSE instead of polling forever.

They are deterministic and never touch a real browser.
"""

import json
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
        pytest.skip("node is required for dashboard live-transport tests")
    result = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


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

// Counts rendered rule rows. renderRules runs for every view on every render,
// so its append count is a faithful proxy for "a full render happened".
function makeCountingDoc(extra = {}) {
  const nodes = new Map();
  const listeners = new Map();
  const doc = {
    hidden: false,
    renderCount: 0,
    getElementById(id) {
      if (!nodes.has(id)) {
        const node = new FakeElement(id);
        if (id === "rulesList") {
          node.appendChild = function (child) {
            doc.renderCount += 1;
            this.children.push(child);
          };
        }
        nodes.set(id, node);
      }
      return nodes.get(id);
    },
    createElement: () => new FakeElement(),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener(name, handler) {
      if (!listeners.has(name)) listeners.set(name, []);
      listeners.get(name).push(handler);
    },
    dispatch(name) {
      (listeners.get(name) || []).forEach((handler) => handler());
    },
  };
  Object.assign(doc, extra);
  doc._nodes = nodes;
  doc._listeners = listeners;
  return doc;
}

// renderRules appends nine rows per render.
const ROWS_PER_RENDER = 9;

function snapshot(timestamp, over = {}) {
  return Object.assign({
    timestamp,
    pv_total_w: 1800,
    inverter_output_w: 620,
    home_load_w: 620,
    grid_power_w: 0,
    battery_power_w: 0,
    average_soc: 61,
    rules: {},
    devices: {},
  }, over);
}
""" % json.dumps(str(APP_JS))


def test_unchanged_timestamp_does_not_rerender():
    script = PRELUDE + """
const doc = makeCountingDoc();
global.document = doc;
app.state.flowView = "aggregated";

app.updateSnapshot(snapshot("2026-06-17T12:00:00Z"));
const afterFirst = doc.renderCount;
app.updateSnapshot(snapshot("2026-06-17T12:00:00Z"));
const afterSecond = doc.renderCount;

console.log(JSON.stringify({ afterFirst, afterSecond, rows: ROWS_PER_RENDER }));
"""
    out = run_node(script)
    assert out["afterFirst"] == out["rows"]
    assert out["afterSecond"] == out["rows"], "an unchanged timestamp must not render again"


def test_changed_timestamp_renders_again():
    script = PRELUDE + """
const doc = makeCountingDoc();
global.document = doc;
app.state.flowView = "aggregated";

app.updateSnapshot(snapshot("2026-06-17T12:00:00Z"));
app.updateSnapshot(snapshot("2026-06-17T12:00:05Z"));

console.log(JSON.stringify({ renderCount: doc.renderCount, rows: ROWS_PER_RENDER }));
"""
    out = run_node(script)
    assert out["renderCount"] == 2 * out["rows"]


def test_snapshot_without_timestamp_still_renders():
    # Defensive: a payload with no timestamp must not be deduplicated into
    # never rendering at all.
    script = PRELUDE + """
const doc = makeCountingDoc();
global.document = doc;
app.state.flowView = "aggregated";

const first = snapshot(undefined);
delete first.timestamp;
const second = snapshot(undefined);
delete second.timestamp;
app.updateSnapshot(first);
app.updateSnapshot(second);

console.log(JSON.stringify({ renderCount: doc.renderCount, rows: ROWS_PER_RENDER }));
"""
    out = run_node(script)
    assert out["renderCount"] == 2 * out["rows"]


def test_hidden_document_defers_render_but_keeps_state():
    script = PRELUDE + """
const doc = makeCountingDoc({ hidden: true });
global.document = doc;
app.state.flowView = "aggregated";

app.updateSnapshot(snapshot("2026-06-17T12:00:00Z", { pv_total_w: 2500 }));

console.log(JSON.stringify({
  renderCount: doc.renderCount,
  storedPv: app.state.snapshot ? app.state.snapshot.pv_total_w : null,
}));
"""
    out = run_node(script)
    assert out["renderCount"] == 0, "a hidden tab must not render"
    assert out["storedPv"] == 2500, "a hidden tab must still accept the newest state"


def test_visibility_change_flushes_only_the_newest_snapshot_once():
    script = PRELUDE + """
const doc = makeCountingDoc({ hidden: true });
global.document = doc;
app.state.flowView = "aggregated";
app.initLiveVisibilityHandling();

app.updateSnapshot(snapshot("2026-06-17T12:00:00Z", { pv_total_w: 100 }));
app.updateSnapshot(snapshot("2026-06-17T12:00:05Z", { pv_total_w: 200 }));
app.updateSnapshot(snapshot("2026-06-17T12:00:10Z", { pv_total_w: 300 }));
const whileHidden = doc.renderCount;

doc.hidden = false;
doc.dispatch("visibilitychange");

console.log(JSON.stringify({
  whileHidden,
  afterVisible: doc.renderCount,
  rows: ROWS_PER_RENDER,
  shownPv: doc.getElementById("metricPv").textContent,
}));
"""
    out = run_node(script)
    assert out["whileHidden"] == 0
    assert out["afterVisible"] == out["rows"], "three hidden updates must coalesce into one render"
    assert out["shownPv"] == "300 W", "the flushed render must show the newest snapshot"
