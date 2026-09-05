# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract for the dashboard's flow tile renderer.

The moving dashes are an HTML layer moved with a CSS transform, so that nothing
animates inside the flow SVG: an SVG element cannot be composited on its own, so
a CSS animation on one re-rasterises a subtree full of drop-shadow filters for
every frame it produces. A canvas renderer was built and measured too, and
rejected -- a canvas has to be repainted every frame, which on this page cost as
much as animating the SVG did. The measurements are in
``reports/dashboard-perf/flow-rendering-investigation.md``.

What these tests pin is what is easy to break silently:

- the renderer reads every appearance decision back out of the CSS, so
  ``animation_mode``, ``prefers-reduced-motion``, the idle state and the
  flow-speed buckets keep working without being reimplemented in JavaScript,
- each run is cut back around the device boxes, which is how the SVG hid the
  ends of every pipe,
- the dash phase carries across a corner and across a cut,
- a browser it cannot run in keeps the CSS animation rather than losing the flow,
- nothing in the layer uses a filter, because a filter puts the per-frame cost
  straight back.

They execute ``dashboard/static/app.js`` under node, like the other frontend
tests, and never touch a real browser.
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
STYLES_CSS = ROOT / "dashboard" / "static" / "styles.css"


def run_node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for dashboard flow-tile tests")
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

// The dashboard's own pipe: three axis-aligned runs of 88, 67 and 80 units.
const PIPE_D = "M204 91 H292 V158 H372";

function segmentsOf(d) {
  return app.flowSegments(app.parseFlowPath(d || PIPE_D));
}

function pipe(over) {
  return Object.assign({
    dash: 34,
    period: 52,
    width: 5,
    opacity: 0.68,
    color: "rgb(56, 213, 255)",
    faded: "rgba(56, 213, 255, 0)",
    seconds: 1.38,
    reverse: false,
  }, over || {});
}
""" % json.dumps(str(APP_JS))


# --------------------------------------------------------------- geometry


def test_parses_the_axis_aligned_commands_the_dashboard_draws_with():
    script = PRELUDE + """
console.log(JSON.stringify({
  aggregated: app.parseFlowPath("M204 91 H292 V158 H372"),
  doublesBack: app.parseFlowPath("M778 218 H818 V106 H778"),
  lineTo: app.parseFlowPath("M0 0 L10 10"),
  decimals: app.parseFlowPath("M1.5 2.25 H3.75"),
}));
"""
    out = run_node(script)
    assert out["aggregated"] == [
        {"x": 204, "y": 91},
        {"x": 292, "y": 91},
        {"x": 292, "y": 158},
        {"x": 372, "y": 158},
    ]
    # The grid pipe doubles back on itself; the direction has to survive.
    assert out["doublesBack"][-1] == {"x": 778, "y": 106}
    assert out["lineTo"] == [{"x": 0, "y": 0}, {"x": 10, "y": 10}]
    assert out["decimals"] == [{"x": 1.5, "y": 2.25}, {"x": 3.75, "y": 2.25}]


def test_refuses_geometry_it_would_have_to_guess_at():
    """A curve or a relative command must disable the renderer, not be approximated."""

    script = PRELUDE + """
console.log(JSON.stringify({
  curve: app.parseFlowPath("M0 0 C10 10 20 20 30 30"),
  quadratic: app.parseFlowPath("M0 0 Q5 5 10 0"),
  arc: app.parseFlowPath("M0 0 A5 5 0 0 1 10 10"),
  relative: app.parseFlowPath("m0 0 h10"),
  close: app.parseFlowPath("M0 0 H10 Z"),
  trailing: app.parseFlowPath("M0 0 H10 H"),
  empty: app.parseFlowPath(""),
  missing: app.parseFlowPath(null),
  singlePoint: app.parseFlowPath("M5 5"),
}));
"""
    out = run_node(script)
    for key, value in out.items():
        assert value is None, f"{key} should have been refused, got {value!r}"


def test_segments_carry_their_direction_and_their_place_along_the_pipe():
    script = PRELUDE + """
console.log(JSON.stringify({
  forward: segmentsOf(PIPE_D),
  doublesBack: segmentsOf("M778 218 H818 V106 H778"),
}));
"""
    out = run_node(script)
    lengths = [s["length"] for s in out["forward"]]
    befores = [s["before"] for s in out["forward"]]
    directions = [s["direction"] for s in out["forward"]]
    assert lengths == [88, 67, 80]
    # The dash phase of a run is where it starts along the pipe.
    assert befores == [0, 88, 155]
    assert directions == ["right", "down", "right"]
    # H818 then V106 then back to H778: right, up, left.
    assert [s["direction"] for s in out["doublesBack"]] == ["right", "up", "left"]


# ------------------------------------------------- reading the CSS policy


def test_dash_pattern_comes_from_the_computed_style():
    script = PRELUDE + """
console.log(JSON.stringify({
  pattern: app.flowDashPattern("34px 18px"),
  commas: app.flowDashPattern("34, 18"),
  none: app.flowDashPattern("none"),
  empty: app.flowDashPattern(""),
  allZero: app.flowDashPattern("0px 0px"),
}));
"""
    out = run_node(script)
    assert out["pattern"] == [34, 18]
    assert out["commas"] == [34, 18]
    # prefers-reduced-motion sets stroke-dasharray: none -- a solid pipe.
    assert out["none"] is None
    assert out["empty"] is None
    assert out["allZero"] is None


def test_animation_policy_is_read_back_rather_than_reimplemented():
    """Every way the CSS can say "do not move" has to reach the renderer."""

    script = PRELUDE + """
const style = (over) => Object.assign({
  animationName: "pipeFlow",
  animationDuration: "1.38s",
  animationPlayState: "running",
  animationIterationCount: "infinite",
}, over);

console.log(JSON.stringify({
  running: app.flowAnimationSeconds(style()),
  milliseconds: app.flowAnimationSeconds(style({ animationDuration: "1380ms" })),
  reducedMode: app.flowAnimationSeconds(style({ animationDuration: "3s" })),
  animationOff: app.flowAnimationSeconds(style({ animationName: "none" })),
  idlePaused: app.flowAnimationSeconds(style({ animationPlayState: "paused" })),
  reducedMotion: app.flowAnimationSeconds(
    style({ animationDuration: "0.001ms", animationIterationCount: "1" })
  ),
  missing: app.flowAnimationSeconds(null),
}));
"""
    out = run_node(script)
    assert out["running"] == pytest.approx(1.38)
    assert out["milliseconds"] == pytest.approx(1.38)
    # dashboard-animation-reduced slows the same keyframe down.
    assert out["reducedMode"] == pytest.approx(3.0)
    # dashboard-animation-off and prefers-reduced-motion both stop it.
    assert out["animationOff"] == 0
    assert out["reducedMotion"] == 0
    # An idle pipe is paused by CSS and must not move.
    assert out["idlePaused"] == 0
    assert out["missing"] == 0


# ------------------------------------------------------------ the occlusion


def test_a_run_is_cut_back_around_a_device_box():
    """The SVG hid the ends of each pipe behind the device boxes. The tile layer
    is above the SVG, so it has to cut the same stretches out itself."""

    script = PRELUDE + """
const segment = { x: 100, y: 50, length: 200, horizontal: true, before: 0, direction: "right" };
const box = { x0: 0, x1: 140, y0: 20, y1: 80 };
const middle = { x0: 200, x1: 240, y0: 20, y1: 80 };
const elsewhere = { x0: 100, x1: 300, y0: 400, y1: 500 };
console.log(JSON.stringify({
  atTheStart: app.flowVisibleRuns(segment, [box]),
  inTheMiddle: app.flowVisibleRuns(segment, [middle]),
  bothEnds: app.flowVisibleRuns(segment, [box, { x0: 260, x1: 400, y0: 20, y1: 80 }]),
  notOverlapping: app.flowVisibleRuns(segment, [elsewhere]),
  none: app.flowVisibleRuns(segment, []),
  fullyCovered: app.flowVisibleRuns(segment, [{ x0: 0, x1: 500, y0: 20, y1: 80 }]),
}));
"""
    out = run_node(script)
    assert out["atTheStart"] == [
        {"x": 140, "y": 50, "length": 160, "horizontal": True, "direction": "right", "before": 40},
    ]
    assert [(r["x"], r["length"]) for r in out["inTheMiddle"]] == [(100, 100), (240, 60)]
    assert [(r["x"], r["length"]) for r in out["bothEnds"]] == [(140, 120)]
    # A box on a different row must not clip this run.
    assert [(r["x"], r["length"]) for r in out["notOverlapping"]] == [(100, 200)]
    assert [(r["x"], r["length"]) for r in out["none"]] == [(100, 200)]
    assert out["fullyCovered"] == []


def test_cutting_a_run_keeps_the_dash_phase_it_had():
    """A cut moves where a run starts, so the phase it carries has to move with
    it or the dashes jump at every device box."""

    script = PRELUDE + """
const rightwards = { x: 100, y: 50, length: 200, horizontal: true, before: 88, direction: "right" };
const leftwards = { x: 100, y: 50, length: 200, horizontal: true, before: 88, direction: "left" };
const cut = { x0: 0, x1: 140, y0: 20, y1: 80 };
console.log(JSON.stringify({
  rightwards: app.flowVisibleRuns(rightwards, [cut]),
  leftwards: app.flowVisibleRuns(leftwards, [cut]),
}));
"""
    out = run_node(script)
    # Travelling right, the cut removes the first 40 units of the run.
    assert out["rightwards"][0]["before"] == 88 + 40
    # Travelling left, the same cut is at the far end, so nothing is skipped.
    assert out["leftwards"][0]["before"] == 88


# ------------------------------------------------------------ the appearance


def test_the_dash_gradient_never_fades_through_black():
    """`transparent` is transparent *black*; CSS interpolates through it and
    leaves a dark fringe on a coloured dash."""

    script = PRELUDE + """
console.log(JSON.stringify({
  faded: app.flowFadedColor("rgb(56, 213, 255)"),
  fromRgba: app.flowFadedColor("rgba(56, 213, 255, 0.5)"),
  garbage: app.flowFadedColor("nonsense"),
  horizontal: app.flowTileBackground(pipe(), true),
  vertical: app.flowTileBackground(pipe(), false),
  solid: app.flowTileBackground(pipe({ dash: 0, period: 0 }), true),
}));
"""
    out = run_node(script)
    assert out["faded"] == "rgba(56, 213, 255, 0)"
    assert out["fromRgba"] == "rgba(56, 213, 255, 0)"
    assert out["garbage"] == "rgba(0, 0, 0, 0)"
    for key in ("horizontal", "vertical"):
        assert "transparent" not in out[key], out[key]
        assert "repeating-linear-gradient" in out[key]
    assert out["horizontal"].startswith("repeating-linear-gradient(90deg,")
    assert out["vertical"].startswith("repeating-linear-gradient(180deg,")
    # An idle pipe has no dash pattern at all: a plain line, no gradient.
    assert out["solid"] == "rgb(56, 213, 255)"


def test_the_gradient_repeats_at_the_dash_period():
    script = PRELUDE + """
const background = app.flowTileBackground(pipe(), true);
const stops = background.match(/(-?[\\d.]+)px/g).map((v) => parseFloat(v));
console.log(JSON.stringify({ stops }));
"""
    out = run_node(script)
    # ramp, dash - ramp, dash, period
    assert out["stops"][-1] == 52
    assert out["stops"][-2] == 34
    assert out["stops"][0] < out["stops"][1] < out["stops"][2]


# ------------------------------------------------------------- the fallback


def test_a_browser_it_cannot_run_in_keeps_the_css_animation():
    """Declining must leave the flow animated, not leave the view empty."""

    script = PRELUDE + """
const added = [];
const removed = [];
global.window = {};
global.document = {
  body: { classList: { add: (n) => added.push(n), remove: (n) => removed.push(n) } },
  getElementById: () => null,
  querySelectorAll: () => [],
  createElement: () => ({ style: {}, classList: { add() {} } }),
};
const started = app.initFlowTiles();
console.log(JSON.stringify({ started, added, active: app.flowTileState.active }));
"""
    out = run_node(script)
    assert out["started"] is False
    assert out["active"] is False
    assert out["added"] == []


def test_a_page_with_no_readable_pipe_keeps_the_css_animation():
    script = PRELUDE + """
const classes = [];
global.window = { getComputedStyle: () => ({}) };
global.document = {
  body: {
    classList: {
      add: (n) => classes.push(["add", n]),
      remove: (n) => classes.push(["remove", n]),
    },
  },
  getElementById: () => null,
  querySelectorAll: () => [],
  createElement: () => ({ style: {}, classList: { add() {} } }),
};
const started = app.initFlowTiles();
console.log(JSON.stringify({ started, classes, active: app.flowTileState.active }));
"""
    out = run_node(script)
    assert out["started"] is False
    assert out["active"] is False
    # It may add the class while it tries, but it must take it off again.
    assert out["classes"][-1] == ["remove", "flow-tiles-active"]


def test_invalidating_an_inactive_renderer_does_nothing():
    script = PRELUDE + """
app.flowTileState.active = false;
app.flowTileState.frameId = null;
app.invalidateFlowTiles();
console.log(JSON.stringify({ frameId: app.flowTileState.frameId }));
"""
    out = run_node(script)
    assert out["frameId"] is None


# --------------------------------------------------------- the CSS contract


def read_styles():
    return STYLES_CSS.read_text(encoding="utf-8")


def test_the_layer_moves_with_a_transform_and_nothing_else():
    """A transform on a promoted layer is handled by the compositor. Animating
    anything that needs a repaint -- a background position, a filter -- is the
    cost this change exists to remove."""

    css = read_styles()
    for name in ("flowTileRight", "flowTileLeft", "flowTileDown", "flowTileUp"):
        match = re.search(r"@keyframes %s \{([^}]*\}[^}]*)\}" % name, css)
        assert match, f"{name} keyframes are missing"
        body = match.group(1)
        assert "translate3d" in body, body
        assert "filter" not in body, body

    inner = re.search(r"\.flow-tile-inner\s*\{([^}]*)\}", css)
    assert inner, ".flow-tile-inner rule is missing"
    assert "will-change: transform" in inner.group(1)
    assert "filter" not in inner.group(1)


def test_no_rule_in_the_tile_layer_carries_a_filter():
    css = read_styles()
    for match in re.finditer(r"(\.flow-tile[^{]*)\{([^}]*)\}", css):
        assert "filter" not in match.group(2), match.group(1).strip()


def test_the_svg_dash_is_switched_off_only_while_the_renderer_runs():
    css = read_styles()
    match = re.search(r"\.flow-tiles-active \.pipe-energy\s*\{([^}]*)\}", css)
    assert match, "the renderer must hide the SVG dash it replaces"
    assert "display: none" in match.group(1)
    # The CSS animation itself stays declared: the renderer reads its duration,
    # direction and dash pattern back out of the computed style.
    assert "animation: pipeFlow var(--pipe-speed) linear infinite" in css


def test_soft_pulse_no_longer_animates_a_filter():
    """Animating the filter property makes every frame re-evaluate a blur, which
    measured as expensive as the flow dashes themselves."""

    css = read_styles()
    match = re.search(r"@keyframes softPulse \{([^}]*\}[^}]*)\}", css)
    assert match, "softPulse keyframes are missing"
    assert "filter" not in match.group(1), match.group(1)
    assert "opacity" in match.group(1)


def test_the_renderer_is_wired_into_the_dashboard_bootstrap():
    source = APP_JS.read_text(encoding="utf-8")
    assert "initFlowTiles();" in source
    # A new snapshot, a view change and an animation-mode change all move the
    # pipes, so each has to bring the layer up to date. Count call sites only,
    # not the declaration.
    calls = source.count("  invalidateFlowTiles();")
    assert calls >= 3, calls
