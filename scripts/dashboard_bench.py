#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reproducible dashboard performance benchmark.

Needs no EMS, no hardware and no network: it serves the synthetic preview
payloads from ``dashboard_preview_data`` and drives real browsers over
Playwright. Every run records the browser build it used, so a number can be
compared only against numbers taken the same way.

    python3 scripts/dashboard_bench.py --matrix ab --browser chromium
    python3 scripts/dashboard_bench.py --matrix baseline --out reports/dashboard-perf

The scenario matrix lives here; scripts/dashboard_bench_driver.mjs only
measures one scenario at a time.
"""

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
DRIVER = os.path.join(SCRIPTS, "dashboard_bench_driver.mjs")
DEFAULT_OUT = os.path.join(ROOT, "reports", "dashboard-perf")

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from serve_dashboard_preview import start_server  # noqa: E402

VIEWS = ("aggregated", "devices", "control", "energy")
TAB_COUNTS = (1, 2, 5, 10)
DEVICE_COUNTS = (2, 4, 8)


def scenario(**over):
    base = {
        "tabs": 1,
        "devices": 2,
        "view": "aggregated",
        "animation": "normal",
        "transport": "sse",
        "snapshots": "changing",
        "backdrop": "on",
        "extra_css": "",
        "extra_js": "",
    }
    base.update(over)
    return base


def matrix_quick():
    return [
        scenario(name="quick-aggregated"),
        scenario(name="quick-control", view="control"),
    ]


def matrix_ab():
    """The isolating experiments the plan makes phase 1 conditional on."""

    cases = []
    for view in ("aggregated", "devices", "control"):
        for animation in ("normal", "off"):
            cases.append(
                scenario(
                    name=f"ab-animation-{view}-{animation}",
                    view=view,
                    animation=animation,
                    devices=4,
                )
            )
    cases.extend(matrix_backdrop())
    for transport in ("sse", "polling"):
        cases.append(
            scenario(
                name=f"ab-transport-{transport}",
                view="control",
                transport=transport,
                devices=2,
            )
        )
    for snapshots in ("changing", "identical"):
        cases.append(
            scenario(
                name=f"ab-snapshots-{snapshots}",
                view="control",
                transport="polling",
                snapshots=snapshots,
                devices=2,
            )
        )
    return cases


def matrix_backdrop():
    """backdrop-filter with the flow animation off. **Superseded by `glass`.**

    This was written when the dash animation dominated everything, and turning
    it off was the only way to see past it. That reasoning has since become the
    bug: backdrop-filter's cost is not a standing cost, it is an INTERACTION
    with something animating above the backdrop root. Measured on a GPU, the
    blur is free when nothing moves and costs about 19% in the control view
    when something does. So this matrix pins the one cell where the answer is
    always "free", and the reports it produced cannot say anything about the
    question it is named for.

    Use `--matrix glass`, which crosses backdrop on/off with animation on/off
    instead of holding animation at one value.
    """

    return [
        scenario(
            name=f"ab-backdrop-{backdrop}",
            view="devices",
            backdrop=backdrop,
            animation="off",
            devices=4,
        )
        for backdrop in ("on", "off")
    ]


NO_DASH_ANIMATION = ".pipe-energy { animation: none !important; }"
NO_GLOW_FILTER = ".pipe-glow { filter: none !important; }"


def matrix_pipe_isolation():
    """Which half of the flow pipe costs: the motion, or the static glow filter.

    Removing the filters from the animating layer changed almost nothing, so the
    two candidates are separated here rather than assumed.
    """

    return [
        scenario(name="pipe-baseline", view="devices", devices=4),
        scenario(
            name="pipe-no-dash-animation",
            view="devices",
            devices=4,
            extra_css=NO_DASH_ANIMATION,
        ),
        scenario(
            name="pipe-no-glow-filter",
            view="devices",
            devices=4,
            extra_css=NO_GLOW_FILTER,
        ),
        scenario(
            name="pipe-neither",
            view="devices",
            devices=4,
            extra_css=NO_DASH_ANIMATION + NO_GLOW_FILTER,
        ),
    ]


NO_SOFT_PULSE = (
    ".solar-visual.active .solar-sun, .inverter-visual.active .inverter-led "
    "{ animation: none !important; }"
)
NO_FILL_PULSE = ".battery-visual.charging .battery-fill { animation: none !important; }"
NO_SUN_FILTER = ".solar-sun, .inverter-visual.active .inverter-led { filter: none !important; }"


def matrix_animations():
    """Each animated rule on its own, because animation_mode=off removes all of them."""

    variants = {
        "anim-baseline": "",
        "anim-no-dash": NO_DASH_ANIMATION,
        "anim-no-soft-pulse": NO_SOFT_PULSE,
        "anim-no-fill-pulse": NO_FILL_PULSE,
        "anim-no-pulse-filters": NO_SUN_FILTER,
        "anim-no-dash-no-pulses": NO_DASH_ANIMATION + NO_SOFT_PULSE + NO_FILL_PULSE,
    }
    return [
        scenario(name=name, view="devices", devices=4, extra_css=css)
        for name, css in variants.items()
    ]


def matrix_steps():
    """Candidate fix: keep the motion, cut how often it invalidates the raster.

    stroke-dashoffset is not compositable, so a continuous animation re-rasters
    the stroke every displayed frame. A stepped timing function moves the same
    distance in N discrete jumps per cycle, which is N raster invalidations per
    cycle instead of one per frame.
    """

    cases = [scenario(name="steps-continuous", view="devices", devices=4)]
    for count in (26, 13, 8, 4):
        cases.append(
            scenario(
                name=f"steps-{count}",
                view="devices",
                devices=4,
                extra_css=(
                    ".pipe-energy { animation-timing-function: steps(%d) !important; }"
                    % count
                ),
            )
        )
    return cases


JS_DRIVEN_FLOW = """
() => {
  const step = () => {
    const pipes = document.querySelectorAll('.pipe-energy');
    let offset = window.__pipeOffset || 0;
    offset = (offset - 4) % 52;
    window.__pipeOffset = offset;
    for (const pipe of pipes) pipe.style.strokeDashoffset = String(offset);
  };
  setInterval(step, __INTERVAL__);
}
"""


def matrix_js_flow():
    """Candidate fix: drive the flow from a timer instead of a CSS animation.

    steps() did not help, which says Firefox re-rasters while an animation is
    running on the property rather than when its value changes. A timer changes
    the value only when it actually changes, and nothing declares an animation.
    """

    cases = [scenario(name="js-flow-css-baseline", view="devices", devices=4)]
    for hz, interval in (("8hz", 125), ("12hz", 83), ("20hz", 50)):
        cases.append(
            scenario(
                name=f"js-flow-{hz}",
                view="devices",
                devices=4,
                extra_css=NO_DASH_ANIMATION,
                extra_js=JS_DRIVEN_FLOW.replace("__INTERVAL__", str(interval)),
            )
        )
    cases.append(
        scenario(
            name="js-flow-none",
            view="devices",
            devices=4,
            extra_css=NO_DASH_ANIMATION,
        )
    )
    return cases


def matrix_filters():
    """Which filter is actually being paid for, in the real dashboard.

    The flow lab showed that the cost of an animation is not the property it
    animates but whether a filter has to be re-evaluated for every frame it
    produces. Two rules here do that: ``.pipe-energy`` animates while carrying
    two drop-shadows, and ``softPulse`` animates the ``filter`` property itself
    on the sun and the inverter LED. ``.pipe-glow`` carries a static filter in
    the same subtree, which Firefox re-evaluates as well.

    Each is removed on its own, so the answer is attributable.
    """

    no_energy_filter = ".pipe-energy { filter: none !important; }"
    no_glow_filter = ".pipe-glow { filter: none !important; }"
    cheap_pulse = (
        "@keyframes cheapPulse { 50% { opacity: .55; } }"
        ".solar-visual.active .solar-sun,"
        ".inverter-visual.active .inverter-led"
        " { animation-name: cheapPulse !important; }"
    )
    variants = {
        "baseline": "",
        "no-energy-filter": no_energy_filter,
        "no-glow-filter": no_glow_filter,
        "cheap-pulse": cheap_pulse,
        "energy+pulse": no_energy_filter + cheap_pulse,
        "all-three": no_energy_filter + no_glow_filter + cheap_pulse,
    }
    cases = []
    for view in ("devices", "aggregated"):
        for name, css in variants.items():
            cases.append(
                scenario(
                    name="filters-%s-%s" % (view, name),
                    view=view,
                    devices=4,
                    extra_css=css,
                )
            )
    return cases


def matrix_flow_svg():
    """Why the lab's filter finding does not transfer to the dashboard.

    In the lab, removing the halo from the animating pipe recovered the frame
    rate. Here it recovers almost nothing -- and the difference is that the
    dashboard's flow SVG is full of *other* static filters: every device shell,
    icon bay, sun and LED carries a drop-shadow. If an animation invalidates
    the SVG's raster, all of them are re-evaluated for every frame, and taking
    two of them away leaves a dozen.

    The variant that matters is ``planned``: the dash animation gone (which is
    what moving it to a canvas does) plus softPulse animating opacity instead
    of a filter. If the SVG then holds still, its filters are rasterised once.
    """

    no_flow_filters = "#flowSvg *, .device-flow-svg * { filter: none !important; }"
    no_dash_animation = ".pipe-energy { animation: none !important; }"
    cheap_pulse = (
        "@keyframes cheapPulse { 50% { opacity: .55; } }"
        ".solar-visual.active .solar-sun,"
        ".inverter-visual.active .inverter-led"
        " { animation-name: cheapPulse !important; }"
    )
    no_fill_pulse = ".battery-visual.charging .battery-fill { animation: none !important; }"

    variants = {
        "baseline": "",
        "no-flow-filters": no_flow_filters,
        "cheap-pulse": cheap_pulse,
        "no-dash-animation": no_dash_animation,
        "planned": no_dash_animation + cheap_pulse,
        "planned+fillpulse": no_dash_animation + cheap_pulse + no_fill_pulse,
        "planned+no-flow-filters": no_dash_animation + cheap_pulse + no_flow_filters,
    }
    cases = []
    for view in ("devices", "aggregated"):
        for name, css in variants.items():
            cases.append(
                scenario(
                    name="flowsvg-%s-%s" % (view, name),
                    view=view,
                    devices=4,
                    extra_css=css,
                )
            )
    return cases


def matrix_pulses():
    """What is left once the dashes stop animating in the SVG.

    Moving the dashes to a canvas removes twelve of the twenty animations the
    devices view runs. The remaining eight are softPulse on the sun and the
    inverter LED and fillPulse on the battery fill. Firefox recovers as soon as
    softPulse stops animating a *filter*; Chromium keeps paying while any
    animation runs inside the SVG at all, because an SVG element cannot be
    composited on its own and every frame re-rasterises a subtree full of
    static drop-shadows.

    The question this answers: does promoting the pulsing elements let Chromium
    composite them, or do the pulses have to go?
    """

    no_dash = ".pipe-energy { animation: none !important; }"
    cheap_pulse = (
        "@keyframes cheapPulse { 50% { opacity: .55; } }"
        ".solar-visual.active .solar-sun,"
        ".inverter-visual.active .inverter-led"
        " { animation-name: cheapPulse !important; }"
    )
    pulses_off = (
        ".solar-visual.active .solar-sun,"
        ".inverter-visual.active .inverter-led,"
        ".battery-visual.charging .battery-fill { animation: none !important; }"
    )
    promote = (
        ".solar-sun, .inverter-led, .battery-fill"
        " { will-change: opacity !important; }"
    )
    no_pulse_filter = ".solar-sun, .inverter-led { filter: none !important; }"

    variants = {
        "canvas-only": no_dash,
        "canvas+cheap-pulse": no_dash + cheap_pulse,
        "canvas+cheap-pulse+promote": no_dash + cheap_pulse + promote,
        "canvas+cheap-pulse+nofilter": no_dash + cheap_pulse + no_pulse_filter,
        "canvas+pulses-off": no_dash + pulses_off,
        "canvas+pulses-off+nofilter": no_dash + pulses_off + no_pulse_filter,
    }
    return [
        scenario(name="pulses-%s" % name, view="devices", devices=4, extra_css=css)
        for name, css in variants.items()
    ]


def matrix_pulse_split():
    """Which of the two remaining pulses is Chromium paying for.

    softPulse runs on .solar-sun, which carries a static
    ``drop-shadow(0 0 14px)``, and on .inverter-led, which carries none.
    fillPulse runs on .battery-fill, which carries none either. If only the
    filtered one costs, the fix is a CSS edit; if any animation in the SVG
    costs, the pulses have to leave the SVG as well.
    """

    no_dash = ".pipe-energy { animation: none !important; }"
    soft_off = (
        ".solar-visual.active .solar-sun,"
        ".inverter-visual.active .inverter-led { animation: none !important; }"
    )
    fill_off = ".battery-visual.charging .battery-fill { animation: none !important; }"
    sun_off = ".solar-visual.active .solar-sun { animation: none !important; }"
    led_off = ".inverter-visual.active .inverter-led { animation: none !important; }"
    cheap_pulse = (
        "@keyframes cheapPulse { 50% { opacity: .55; } }"
        ".solar-visual.active .solar-sun,"
        ".inverter-visual.active .inverter-led"
        " { animation-name: cheapPulse !important; }"
    )
    sun_nofilter = ".solar-sun { filter: none !important; }"

    variants = {
        "all-off": no_dash + soft_off + fill_off,
        "fill-only": no_dash + soft_off,
        "led-only": no_dash + sun_off + fill_off + cheap_pulse,
        "sun-only": no_dash + led_off + fill_off + cheap_pulse,
        "sun-only-nofilter": no_dash + led_off + fill_off + cheap_pulse + sun_nofilter,
        "cheap-pulses-nofilter": no_dash + cheap_pulse + sun_nofilter,
    }
    return [
        scenario(name="split-%s" % name, view="devices", devices=4, extra_css=css)
        for name, css in variants.items()
    ]


def matrix_views():
    """The two flow views at the device counts a real installation has.

    The aggregated view is what a dashboard opens on, and it draws four pipes
    whatever the installation looks like. The devices view draws three per
    device, so it is the one whose cost grows.
    """

    return [
        scenario(name="views-%s-%ddev" % (view, devices), view=view, devices=devices)
        for view in ("aggregated", "devices")
        for devices in (2, 4, 8)
    ]


def matrix_tabs():
    """The acceptance goal: 1, 2, 5 and 10 tabs open at once."""

    return [
        scenario(name=f"tabs-{n}", tabs=n, view="control", devices=2)
        for n in TAB_COUNTS
    ]


def matrix_delivery():
    """What the shipped changes are supposed to move, plus the animation lever."""

    cases = [
        scenario(name=f"tabs-{n}", tabs=n, view="control", devices=2)
        for n in TAB_COUNTS
    ]
    cases.append(
        scenario(name="polling-identical", view="control", transport="polling",
                 snapshots="identical", devices=2)
    )
    cases.append(scenario(name="animation-normal", view="devices", devices=4))
    cases.append(
        scenario(name="animation-off", view="devices", devices=4, animation="off")
    )
    return cases


def matrix_baseline():
    cases = []
    for tabs in TAB_COUNTS:
        cases.append(scenario(name=f"tabs-{tabs}", tabs=tabs, view="control", devices=2))
    for view in VIEWS:
        cases.append(scenario(name=f"view-{view}", view=view, devices=2))
    for devices in DEVICE_COUNTS:
        cases.append(
            scenario(name=f"devices-{devices}", view="devices", devices=devices)
        )
    return cases


def matrix_glass():
    """Section 13: is the glass panel worth what it costs?

    backdrop-filter: blur(18px) sits on .metric and .flow-panel -- the panels
    that CONTAIN the animation, which is the pathological arrangement: an
    animating layer above a backdrop root forces the backdrop to be recomputed
    as the thing above it moves.

    Measured separately: with every animation frozen, turning the blur off
    changes the picture by a mean of 0.008/255 in Firefox and 0.18/255 in
    Chromium. The panels are 78-92% opaque over a near-uniform near-black
    page, and the only high-frequency thing behind them is a 1px grid at about
    1% contrast -- blur has almost nothing to act on. So if this costs frame
    rate, it is being paid for nothing.
    """

    cases = []
    for view in ("aggregated", "devices"):
        for animation in ("normal", "off"):
            for backdrop in ("on", "off"):
                cases.append(
                    scenario(
                        name="glass-%s-anim%s-backdrop%s" % (view, animation, backdrop),
                        view=view,
                        devices=4,
                        animation=animation,
                        backdrop=backdrop,
                    )
                )
    return cases


FF_CLIFF_VARIANTS = {
    "baseline": "",
    # Firefox documents a will-change budget in viewport areas; over it, the
    # hint is ignored for every element that asked. Every tile asks.
    "no-will-change": ".flow-tile-inner { will-change: auto !important; }",
    # Promote the layer once instead of asking for one promotion per tile.
    "layer-promoted": (
        ".flow-tile-inner { will-change: auto !important; }"
        ".flow-tile-layer { will-change: transform; transform: translateZ(0); }"
    ),
    "contain-paint": ".flow-tile-layer { contain: paint; }",
    "card-contain": ".device-card { contain: paint; }",
    "card-content-visibility": (
        ".device-card { content-visibility: auto; contain-intrinsic-size: 320px 260px; }"
    ),
    # Does the cliff move when the picture is short enough to fit the viewport?
    "no-device-filters": ".device-flow-svg * { filter: none !important; }",
    "no-backdrop-css": (
        ".metric, .flow-panel, .rules-panel, .chart-panel, .device-card,"
        " .energy-stats-panel { backdrop-filter: none !important; }"
    ),
}


PAUSE_OFFSCREEN_TILES = """
// Pause the animation on every tile that is not ENTIRELY inside the viewport.
//
// A previous attempt with an IntersectionObserver made things worse and was
// reverted, but it paused tiles that were not intersecting AT ALL. If the
// trigger is "not entirely inside", a tile straddling the fold still animates
// under that rule and the cliff survives -- so the two experiments are not the
// same one, and threshold 1.0 is the whole difference.
(() => {
  const apply = (entries) => {
    for (const entry of entries) {
      const inside = entry.isIntersecting && entry.intersectionRatio >= 0.999;
      const tile = entry.target;
      if (inside === !tile.classList.contains("still")) continue;
      tile.classList.toggle("still", !inside);
    }
  };
  const observer = new IntersectionObserver(apply, { threshold: [0, 0.999, 1] });
  const attach = () => {
    document.querySelectorAll(".flow-tile").forEach((tile) => observer.observe(tile));
  };
  attach();
  // The tile layer is rebuilt whenever the geometry changes, which replaces
  // every node, so the observer has to be re-attached rather than set up once.
  new MutationObserver(attach).observe(document.body, { childList: true, subtree: true });
})();
"""


def matrix_ff_cliff():
    """The unsolved problem: Firefox devices view collapses past two devices.

    The trigger was measured to be the tile layer growing taller than the
    viewport rather than the number of tiles -- 40 tiles in a 395px layer ran
    at 60.2 fps while 28 tiles in a 909px layer ran at 8.8. Each variant here
    is one mechanistic explanation made falsifiable.
    """

    cases = []
    for devices in (2, 4, 8):
        for name, css in FF_CLIFF_VARIANTS.items():
            cases.append(
                scenario(
                    name="ffcliff-%ddev-%s" % (devices, name),
                    view="devices",
                    devices=devices,
                    extra_css=css,
                )
            )
        cases.append(
            scenario(
                name="ffcliff-%ddev-pause-offscreen" % devices,
                view="devices",
                devices=devices,
                extra_js=PAUSE_OFFSCREEN_TILES,
            )
        )
    return cases


def matrix_gpu_recheck():
    """Re-test the previous study's conclusions on hardware it never used.

    Its Chromium numbers were taken on headless ANGLE/SwiftShader, a software
    rasteriser. backdrop-filter and large-layer compositing are exactly the
    things whose cost collapses on a GPU, so the conclusions drawn from them
    have to be re-run with --gpu gpu before they are carried into this study.
    Run this matrix twice, once with --gpu software and once with --gpu gpu,
    and compare like for like.
    """

    cases = []
    for view in ("aggregated", "devices"):
        for devices in (2, 4, 8):
            cases.append(
                scenario(
                    name="gpurecheck-%s-%ddev" % (view, devices),
                    view=view,
                    devices=devices,
                )
            )
    for backdrop in ("on", "off"):
        cases.append(
            scenario(
                name="gpurecheck-backdrop-%s" % backdrop,
                view="devices",
                devices=4,
                backdrop=backdrop,
            )
        )
    return cases


MATRICES = {
    "glass": matrix_glass,
    "ff-cliff": matrix_ff_cliff,
    "gpu-recheck": matrix_gpu_recheck,
    "filters": matrix_filters,
    "flow-svg": matrix_flow_svg,
    "views": matrix_views,
    "pulses": matrix_pulses,
    "pulse-split": matrix_pulse_split,
    "quick": matrix_quick,
    "ab": matrix_ab,
    "backdrop": matrix_backdrop,
    "pipe": matrix_pipe_isolation,
    "animations": matrix_animations,
    "steps": matrix_steps,
    "tabs": matrix_tabs,
    "delivery": matrix_delivery,
    "jsflow": matrix_js_flow,
    "baseline": matrix_baseline,
}


def node_binary():
    for candidate in ("node", "nodejs"):
        path = subprocess.run(
            ["which", candidate], capture_output=True, text=True, check=False
        ).stdout.strip()
        if path:
            return path
    return None


def browser_available(node, browser):
    probe = (
        "import('playwright').then(p => p.%s.executablePath())"
        ".then(path => { process.stdout.write(path); })"
        ".catch(() => process.exit(3));" % browser
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    path = result.stdout.strip()
    return path if path and os.path.exists(path) else None


def load_average():
    """Machine load when a case ran.

    This host is a live desktop, not a dedicated benchmark box. A run taken
    while something else was busy is not comparable to a quiet one, and the
    only way to notice afterwards is to have written the number down.
    """
    try:
        return [round(value, 2) for value in os.getloadavg()]
    except OSError:
        return None


def wait_for_quiet(threshold, timeout_seconds=900, poll_seconds=15):
    """Block until the machine is idle enough for the number to mean anything.

    This host is a live desktop and it is CPU-limited when several things run
    at once. A frame rate measured against a loaded machine is not a property
    of the renderer, and -- worse -- it is not distinguishable afterwards from
    one that is. So the harness waits rather than producing a number it would
    have to caveat, and records what it waited for.
    """

    if threshold is None or threshold <= 0:
        return {"waited_seconds": 0, "threshold": None, "load_at_start": load_average()}
    started = time.monotonic()
    while True:
        try:
            current = os.getloadavg()[0]
        except OSError:
            return {"waited_seconds": 0, "threshold": threshold, "load_at_start": None}
        waited = time.monotonic() - started
        if current <= threshold:
            return {
                "waited_seconds": round(waited, 1),
                "threshold": threshold,
                "load_at_start": [round(v, 2) for v in os.getloadavg()],
            }
        if waited >= timeout_seconds:
            print(
                "  ! load %.2f still above %.2f after %ds; measuring anyway and "
                "recording it" % (current, threshold, int(waited)),
                file=sys.stderr,
                flush=True,
            )
            return {
                "waited_seconds": round(waited, 1),
                "threshold": threshold,
                "timed_out": True,
                "load_at_start": [round(v, 2) for v in os.getloadavg()],
            }
        print(
            "  . waiting for a quiet machine: load %.2f > %.2f" % (current, threshold),
            file=sys.stderr,
            flush=True,
        )
        time.sleep(poll_seconds)


def run_case(node, case, browser, duration_ms, sse_interval, sse_max_per_ip, gpu="software", max_load=None):
    quiet = wait_for_quiet(max_load)
    load = load_average()
    # Production caps concurrent SSE streams per client. Without that cap every
    # tab opens one, and over HTTP/1.1 those streams occupy the browser's ~6
    # connections per host until the later tabs cannot load the page at all --
    # measured: ten tabs never finished navigating. The benchmark models the
    # cap so the multi-tab scenarios measure the product rather than the
    # harness.
    server = start_server(
        "127.0.0.1",
        0,
        "normal",
        device_count=case["devices"],
        freeze_timestamp=case["snapshots"] == "identical",
        sse_interval=sse_interval,
        sse_max_per_ip=sse_max_per_ip,
    )
    host, port = server.server_address
    payload = {
        "url": f"http://{host}:{port}/",
        "tabs": case["tabs"],
        "view": case["view"],
        "animation": case["animation"],
        "transport": case["transport"],
        "backdrop": case["backdrop"],
        "extraCss": case.get("extra_css", ""),
        "extraJs": case.get("extra_js", ""),
        "navigationTimeoutMs": 90000,
        "durationMs": duration_ms,
        "browser": browser,
        "gpu": gpu,
    }
    try:
        result = subprocess.run(
            [node, DRIVER, json.dumps(payload)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=max(300, duration_ms / 1000 * 6 + 120 * max(1, case["tabs"])),
        )
    finally:
        server.shutdown()
        server.server_close()

    if result.returncode != 0:
        return {"case": case, "load_average": load, "quiet_gate": quiet, "error": result.stderr.strip()[:2000]}
    return {
        "case": case,
        "load_average": load,
        "quiet_gate": quiet,
        "result": json.loads(result.stdout),
    }


def environment(browser, executable, gpu="software"):
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "browser": browser,
        "browser_executable": executable,
        "gpu": gpu,
        "note": (
            "Firefox on macOS is the reported symptom's environment and is not "
            "reproducible here; these numbers are Linux. The rasterisation path "
            "is not uniform across browsers: headless Chromium defaults to "
            "ANGLE/SwiftShader software rendering while Firefox reaches the real "
            "GPU either way. Read rasterisation.renderer in each entry rather "
            "than assuming; --gpu selects the path for Chromium."
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", choices=sorted(MATRICES), default="quick")
    parser.add_argument("--browser", choices=("chromium", "firefox"), default="chromium")
    parser.add_argument(
        "--gpu",
        choices=("software", "gpu", "headed"),
        default="software",
        help=(
            "rasterisation path: software is the default headless Chromium "
            "ANGLE/SwiftShader, gpu asks Chromium for the real device, headed "
            "opens a window on $DISPLAY. Firefox reaches the GPU regardless."
        ),
    )
    parser.add_argument(
        "--max-load",
        type=float,
        default=2.0,
        help=(
            "wait until the 1-minute load average is at or below this before "
            "each case. This host is CPU-limited when several things run at "
            "once, and a frame rate taken under load is not a property of the "
            "renderer. 0 disables the wait."
        ),
    )
    parser.add_argument("--duration-ms", type=int, default=8000)
    parser.add_argument("--sse-interval", type=float, default=2.0)
    parser.add_argument(
        "--sse-max-per-ip",
        type=int,
        default=2,
        help="concurrent SSE streams per client, as production caps them (0: no cap)",
    )
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--label", default=None, help="name recorded in the report")
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="run the whole matrix N times, so variance is visible rather than assumed",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="print the result instead of writing a report file",
    )
    args = parser.parse_args(argv)

    node = node_binary()
    if not node:
        raise SystemExit("node is required to drive Playwright")
    executable = browser_available(node, args.browser)
    if not executable:
        raise SystemExit(
            f"playwright's {args.browser} build is not installed; "
            f"run: npx playwright install {args.browser}"
        )

    cases = MATRICES[args.matrix]()
    runs = []
    started = time.monotonic()
    for repeat in range(args.repeat):
        for index, case in enumerate(cases, 1):
            label = f"[{index}/{len(cases)}] {case['name']}"
            if args.repeat > 1:
                label += f" (run {repeat + 1}/{args.repeat})"
            print(label, file=sys.stderr, flush=True)
            entry = run_case(
                node,
                case,
                args.browser,
                args.duration_ms,
                args.sse_interval,
                args.sse_max_per_ip,
                args.gpu,
                args.max_load,
            )
            entry["repeat"] = repeat + 1
            runs.append(entry)

    report = {
        "schema_version": 1,
        "label": args.label or args.matrix,
        "matrix": args.matrix,
        "duration_ms": args.duration_ms,
        "sse_max_per_ip": args.sse_max_per_ip,
        "environment": environment(args.browser, executable, args.gpu),
        "wall_clock_seconds": round(time.monotonic() - started, 1),
        "runs": runs,
    }

    if args.print_only:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    os.makedirs(args.out, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    name = f"{report['label']}-{args.browser}-{stamp}.json"
    path = os.path.join(args.out, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
