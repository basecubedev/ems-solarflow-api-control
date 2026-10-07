# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one flow the aggregated diagram could not draw: grid into the fleet.

A charging device reports no output, so before this the diagram showed an idle
inverter next to a grid that was importing hundreds of watts for it. These tests
pin the two rules that make the picture add up.
"""

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


def run_node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for executable dashboard flow tests")
    result = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


class PipeParser(HTMLParser):
    """Collect the ids and classes of the SVG flow pipes."""

    def __init__(self):
        super().__init__()
        self.pipes = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "g" and "energy-pipe" in classes and attrs.get("id"):
            self.pipes[attrs["id"]] = classes


def test_the_diagram_has_a_grid_to_inverter_pipe():
    parser = PipeParser()
    parser.feed(INDEX_HTML.read_text(encoding="utf-8"))

    assert "pipeGridInverter" in parser.pipes
    # It reuses the grid colour family rather than introducing a new one.
    assert "grid" in parser.pipes["pipeGridInverter"]
    # The node it feeds gained a state line, like the battery and grid nodes.
    assert 'id="flowInverterState"' in INDEX_HTML.read_text(encoding="utf-8")


def test_the_charge_is_not_drawn_twice_on_the_way_from_the_grid():
    """At night the whole import is the charge, and it has its own pipe now."""

    out = run_node(
        f"""
const app = require({json.dumps(str(APP_JS))});
console.log(JSON.stringify({{
  night: app.gridHomePipeWatts(600, 600),
  import_without_charge: app.gridHomePipeWatts(600, 0),
  export_untouched: app.gridHomePipeWatts(-900, 0),
  export_ignores_charge: app.gridHomePipeWatts(-900, 400),
  charge_exceeds_import: app.gridHomePipeWatts(100, 600),
  no_meter: app.gridHomePipeWatts(0, 0),
}}));
"""
    )

    assert out["night"] == 0
    assert out["import_without_charge"] == 600
    assert out["export_untouched"] == 900
    assert out["export_ignores_charge"] == 900
    # Never negative: the pipe falls idle instead of reversing.
    assert out["charge_exceeds_import"] == 0
    assert out["no_meter"] == 0


def test_the_inverter_keeps_its_output_and_states_the_charge_separately():
    """Both directions run at once in a mixed fleet, so one number cannot
    stand for both. The value stays the output; the charge gets its own line."""

    out = run_node(
        f"""
const app = require({json.dumps(str(APP_JS))});
console.log(JSON.stringify({{
  charging: app.inverterChargeLabel(600, true),
  idle: app.inverterChargeLabel(0, false),
  line_not_drawn: app.inverterChargeLabel(600, false),
}}));
"""
    )

    assert out["charging"] == "Charging 600 W"
    assert out["idle"] == ""
    assert out["line_not_drawn"] == ""


AGGREGATED_SCRIPT = r"""
const app = require(%(app)s);

function node() {
  const classes = new Set();
  const attrs = {};
  return {
    textContent: "",
    classes,
    classList: { toggle(name, on) { if (on) classes.add(name); else classes.delete(name); } },
    style: { setProperty() {} },
    getAttribute(name) { return name in attrs ? attrs[name] : null; },
    setAttribute(name, value) { attrs[name] = String(value); },
  };
}

const nodes = { flowInverterState: node(), pipeGridInverter: node() };
if (!%(with_pipe)s) delete nodes.pipeGridInverter;
global.document = {
  getElementById: (id) => nodes[id] || null,
  querySelector: () => null,
  querySelectorAll: () => [],
};
app.state.flowActivity = new Map();

const out = [];
for (const charge of %(charges)s) {
  app.renderAggregatedSnapshot({
    inverter_charge_w: charge, inverter_output_w: 0, grid_power_w: charge,
    home_load_w: 200, pv_total_w: 0, average_soc: 40, devices: {},
  });
  out.push({
    state: nodes.flowInverterState.textContent,
    active: nodes.pipeGridInverter ? nodes.pipeGridInverter.classes.has("active") : null,
  });
}
console.log(JSON.stringify(out));
"""


def test_the_aggregated_inverter_says_charging_while_its_charge_line_is_drawn():
    """The line keeps flowing as a charge fades, and the node says so with it."""

    started, fading, stopped = run_node(AGGREGATED_SCRIPT % {
        "app": json.dumps(str(APP_JS)),
        "charges": json.dumps([600, 5, 0]),
        "with_pipe": "true",
    })

    assert started == {"state": "Charging 600 W", "active": True}
    assert fading == {"state": "Charging 5 W", "active": True}
    assert stopped == {"state": "", "active": False}


def test_the_aggregated_charge_label_does_not_need_the_pipe_element():
    (frame,) = run_node(AGGREGATED_SCRIPT % {
        "app": json.dumps(str(APP_JS)),
        "charges": json.dumps([600]),
        "with_pipe": "false",
    })

    assert frame == {"state": "Charging 600 W", "active": None}


def test_every_charge_decision_reason_has_readable_text():
    """A raw slug in the Control tab is the backend leaking into the UI.

    The frontend maps known reasons to sentences and falls through to the slug
    otherwise, so a reason the backend emits without a mapping reads as
    `ac_charge_share_too_small` to an operator.
    """

    from ems.controller import EMSController
    import inspect
    import re

    emitted = set(
        re.findall(
            r'decision_reason = "(ac_charge_[a-z_]+)"',
            inspect.getsource(EMSController.explain_charge_allocation),
        )
    )
    assert emitted, "no charge reasons found; the regex or the source moved"

    source = APP_JS.read_text(encoding="utf-8")
    mapped = set(re.findall(r"^\s*(ac_charge_[a-z_]+):", source, re.MULTILINE))

    assert emitted <= mapped, sorted(emitted - mapped)


DEVICE_FLOW_SCRIPT = r"""
const app = require(%(app)s);

class Node {
  constructor(tag, attrs, text) {
    this.tag = tag;
    this.attrs = attrs;
    this.textContent = text;
    this.style = { props: {}, setProperty(name, value) { this.props[name] = value; } };
  }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
}

function parse(html) {
  const nodes = [];
  const tag = /<(\w+)\b([^>]*)>([^<]*)/g;
  let match;
  while ((match = tag.exec(html))) {
    const attrs = {};
    const attr = /([\w-]+)(?:="([^"]*)")?/g;
    let pair;
    while ((pair = attr.exec(match[2]))) attrs[pair[1]] = pair[2] ?? "";
    nodes.push(new Node(match[1], attrs, match[3].trim()));
  }
  return nodes;
}

const container = {
  hidden: false,
  nodes: [],
  set innerHTML(html) { this.nodes = parse(html); },
  get innerHTML() { return ""; },
  querySelectorAll(selector) {
    const name = /^\[([\w-]+)\]$/.exec(selector);
    return name ? this.nodes.filter((node) => name[1] in node.attrs) : [];
  },
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
};
global.document = {
  getElementById: (id) => (id === "deviceFlowView" ? container : null),
  querySelector: () => null,
  querySelectorAll: () => [],
};
app.state.flowView = "aggregated";
app.state.deviceFlowSignature = null;
app.state.flowActivity = new Map();

function find(attribute, key) {
  return container.nodes.find((node) => node.attrs[attribute] === key) || null;
}

function row(key) {
  const pipe = find("data-flow-pipe", `${key}:house`);
  const inverter = find("data-flow-visual", `${key}:inverter`);
  const state = find("data-flow-text", `${key}:inverter-state`);
  return {
    pipe: pipe ? pipe.attrs.class.split(" ") : null,
    watts: pipe ? pipe.attrs["data-flow-watts"] : null,
    width: pipe ? pipe.style.props["--pipe-width"] || null : null,
    inverter: inverter ? inverter.attrs.class.split(" ") : null,
    state: state ? state.textContent : null,
  };
}

const snapshots = %(snapshots)s;
const out = [];
for (const snapshot of snapshots) {
  if (snapshot.unrendered) {
    app.state.snapshotSequence += snapshot.unrendered;
    continue;
  }
  app.state.snapshotSequence += 1;
  app.renderDeviceFlow(snapshot);
  out.push({ pro: row("row-0-800-Pro-2"), ac: row("row-1-2400-AC") });
}
console.log(JSON.stringify(out));
"""


def _fleet(charge, output):
    return {
        "home_load_w": 200,
        "grid_power_w": 0,
        "devices": {
            "800 Pro 2": {"soc": 82, "pv_input_w": 1400, "output_w": 800, "battery_power_w": 560},
            "2400 AC": {"soc": 34, "pv_input_w": 0, "output_w": output, "ac_charge_w": charge, "battery_power_w": charge - output},
        },
    }


def test_the_device_view_draws_the_charge_into_a_charging_inverter():
    """A charging device reports no output, so its row showed no flow at all.

    The line between house and inverter carries the charge, in the grid colour
    family as on the aggregated diagram, and its inverter says what it takes --
    first when the view is built, then on each snapshot that only updates it.
    It is one line that turns round, not a second one laid over the output, and
    the inverter says "Charging" for exactly as long as that line is drawn.
    """

    snapshots = [
        _fleet(charge=600, output=0),
        _fleet(charge=900, output=0),
        _fleet(charge=5, output=0),
        _fleet(charge=0, output=300),
    ]
    built, updated, fading, discharging = run_node(DEVICE_FLOW_SCRIPT % {
        "app": json.dumps(str(APP_JS)),
        "snapshots": json.dumps(snapshots),
    })

    for frame, watts in ((built, "600 W"), (updated, "900 W"), (fading, "5 W")):
        assert frame["ac"]["pipe"] is not None, "no line from the house to the charging inverter"
        assert {"grid", "active", "reverse"} <= set(frame["ac"]["pipe"])
        assert "active" in frame["ac"]["inverter"]
        assert frame["ac"]["state"] == f"Charging {watts}"
        assert {"output", "active"} <= set(frame["pro"]["pipe"])
        assert "reverse" not in frame["pro"]["pipe"]
        assert frame["pro"]["state"] == ""

    assert {"output", "active"} <= set(discharging["ac"]["pipe"])
    assert not {"grid", "reverse"} & set(discharging["ac"]["pipe"])
    assert discharging["ac"]["state"] == ""


def test_a_trickle_left_after_the_flow_turns_is_not_drawn_as_the_old_flow():
    """Each direction earns its own place on the line.

    A standby draw after the output stops, a 1 W reading at night and a few
    watts of output after a charge are none of them a flow; the line stays the
    neutral idle output line and the inverter claims no charge.
    """

    snapshots = [
        _fleet(charge=0, output=300),
        _fleet(charge=5, output=0),
        _fleet(charge=1, output=0),
        _fleet(charge=600, output=0),
        _fleet(charge=0, output=5),
    ]
    feeding, standby, noise, charging, trickle = run_node(DEVICE_FLOW_SCRIPT % {
        "app": json.dumps(str(APP_JS)),
        "snapshots": json.dumps(snapshots),
    })

    assert {"output", "active"} <= set(feeding["ac"]["pipe"])
    assert {"grid", "active", "reverse"} <= set(charging["ac"]["pipe"])
    assert charging["ac"]["state"] == "Charging 600 W"
    for frame in (standby, noise, trickle):
        assert "output" in frame["ac"]["pipe"]
        assert not {"grid", "reverse", "active"} & set(frame["ac"]["pipe"])
        assert frame["ac"]["state"] == ""


def test_the_line_keeps_its_direction_while_both_flows_are_reported():
    """A frame with charge and output both above the threshold is a transition.

    The line holds the direction it has instead of turning with every snapshot
    in which the other reading is a few watts larger; a flow takes it over
    only once it clearly dominates.
    """

    snapshots = [
        _fleet(charge=320, output=300),
        _fleet(charge=300, output=320),
        _fleet(charge=320, output=300),
        _fleet(charge=6, output=300),
        _fleet(charge=400, output=300),
        _fleet(charge=700, output=300),
    ]
    frames = run_node(DEVICE_FLOW_SCRIPT % {
        "app": json.dumps(str(APP_JS)),
        "snapshots": json.dumps(snapshots),
    })
    transition, standby_after_charge, smaller_charge, dominant_charge = (
        frames[:3], frames[3], frames[4], frames[5]
    )

    for frame in transition:
        assert {"grid", "active", "reverse"} <= set(frame["ac"]["pipe"])
        assert frame["ac"]["state"].startswith("Charging ")
    for frame in (standby_after_charge, smaller_charge):
        assert {"output", "active"} <= set(frame["ac"]["pipe"])
        assert frame["ac"]["state"] == ""
    assert {"grid", "active", "reverse"} <= set(dominant_charge["ac"]["pipe"])
    assert dominant_charge["ac"]["state"] == "Charging 700 W"


def test_a_reading_that_is_not_a_number_draws_an_idle_line_rather_than_nan():
    snapshot = _fleet(charge=0, output=0)
    snapshot["devices"]["2400 AC"].update({"output_w": "n/a", "ac_charge_w": "n/a"})

    (frame,) = run_node(DEVICE_FLOW_SCRIPT % {
        "app": json.dumps(str(APP_JS)),
        "snapshots": json.dumps([snapshot]),
    })

    assert frame["ac"]["watts"] == "0"
    assert {"output", "idle"} <= set(frame["ac"]["pipe"])
    assert frame["ac"]["state"] == ""


def _device_frames(snapshots, before=""):
    script = DEVICE_FLOW_SCRIPT % {
        "app": json.dumps(str(APP_JS)),
        "snapshots": json.dumps(snapshots),
    }
    return run_node(script.replace("const snapshots =", f"{before}\nconst snapshots =", 1))


def test_the_first_frame_of_the_device_view_is_drawn_to_its_own_scale():
    """A full build sizes its ribbons as an update would, whatever came before."""

    snapshot = _fleet(charge=2400, output=0)
    (first,) = _device_frames([snapshot], before="app.flowScaleReference(250);")
    _, settled = _device_frames([snapshot, snapshot])

    assert first["ac"]["width"] is not None
    assert first["ac"]["width"] == settled["ac"]["width"]
    assert first["pro"]["width"] == settled["pro"]["width"]


def test_a_reading_that_is_not_a_number_does_not_resize_the_other_rows():
    broken = _fleet(charge=0, output=0)
    broken["devices"]["2400 AC"]["output_w"] = "n/a"
    clean = _fleet(charge=0, output=0)
    for snapshot in (broken, clean):
        snapshot["devices"]["800 Pro 2"]["output_w"] = 200

    _, with_broken = _device_frames([broken, broken])
    _, with_clean = _device_frames([clean, clean])

    assert with_broken["pro"]["width"] is not None
    assert with_broken["pro"]["width"] == with_clean["pro"]["width"]



def test_a_direction_remembered_from_before_a_hidden_spell_is_forgotten():
    """Snapshots that went by unrendered leave the line no direction to keep."""

    charging, returned = _device_frames([
        _fleet(charge=600, output=0),
        {"unrendered": 120},
        _fleet(charge=300, output=500),
    ])

    assert {"grid", "active", "reverse"} <= set(charging["ac"]["pipe"])
    assert {"output", "active"} <= set(returned["ac"]["pipe"])
    assert returned["ac"]["state"] == ""


AGGREGATED_SCALE_SCRIPT = r"""
const app = require(%(app)s);
const nodes = {};
global.document = {
  getElementById: (id) => {
    if (!nodes[id]) {
      const classes = new Set();
      const attrs = {};
      nodes[id] = {
        textContent: "",
        classList: { toggle(name, on) { if (on) classes.add(name); else classes.delete(name); } },
        style: { props: {}, setProperty(name, value) { this.props[name] = value; } },
        getAttribute(name) { return name in attrs ? attrs[name] : null; },
        setAttribute(name, value) { attrs[name] = String(value); },
      };
    }
    return nodes[id];
  },
  querySelector: () => null,
  querySelectorAll: () => [],
};
app.state.flowActivity = new Map();
app.renderAggregatedSnapshot({
  inverter_charge_w: %(charge)s, inverter_output_w: 300, grid_power_w: 0,
  home_load_w: 300, pv_total_w: 1400, average_soc: 40, devices: {},
});
console.log(JSON.stringify({ width: nodes.pipeInverterHome.style.props["--pipe-width"] }));
"""


def test_an_aggregated_charge_that_is_not_a_number_does_not_resize_the_diagram():
    widths = [
        run_node(AGGREGATED_SCALE_SCRIPT % {"app": json.dumps(str(APP_JS)), "charge": charge})["width"]
        for charge in ('"n/a"', "0")
    ]

    assert widths[0] is not None
    assert widths[0] == widths[1]
