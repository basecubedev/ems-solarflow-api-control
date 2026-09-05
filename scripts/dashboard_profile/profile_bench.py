#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Attribution profiler for the dashboard: what is the page doing, not how fast.

The dashboard benchmark answers "how many frames". When a frame rate says the
page is fine and a user says it is not, the question left over is where the main
thread goes. This drives the real dashboard through
``scripts/dashboard_profile/profile_driver.mjs``, which charges main-thread time
to the callback that consumed it, and adds the three scenarios the frame-rate
harness has no way to express: a page with nothing arriving at all, a page in
the background, and a second, trivial page beside it.

    python3 scripts/dashboard_profile/profile_bench.py --matrix attribution \
        --browser firefox --gpu headed

Every matrix runs one case at a time and waits for a quiet machine first.
Never run two of these at once.

Running it on macOS is the point of it existing; see README.md in this
directory.
"""

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(ROOT, "scripts")
DRIVER = os.path.join(SCRIPTS, "dashboard_profile", "profile_driver.mjs")
DEFAULT_OUT = os.path.join(ROOT, "reports", "dashboard-perf")

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from serve_dashboard_preview import start_server  # noqa: E402
import dashboard_bench as bench  # noqa: E402

VIEWS = ("aggregated", "devices", "control", "energy")

# How much data reaches the page, which is the axis the frame-rate harness
# cannot express. `silent` is the important one: the page has rendered once and
# then nothing arrives at all, so whatever it still costs is the page merely
# existing.
FEEDS = {
    "live": {"freeze_timestamp": False, "sse_interval": 2.0},
    "frozen": {"freeze_timestamp": True, "sse_interval": 2.0},
    "silent": {"freeze_timestamp": False, "sse_interval": 3600.0},
}


def scenario(**over):
    base = {
        "view": "aggregated",
        "devices": 4,
        "animation": "off",
        "transport": "sse",
        "feed": "live",
        "software": False,
        "foreground": "dashboard",
        "deep_reads": False,
        "cpu_throttle": 1,
        "dashboard_open": True,
        "neighbour": False,
        "extra_js": "",
    }
    base.update(over)
    if "name" not in base:
        base["name"] = "%s-%s-anim%s-%s" % (
            base["view"], base["feed"], base["animation"],
            "sw" if base["software"] else "gpu",
        )
    return base


def matrix_feed():
    """What does the page cost when nothing is arriving?

    Separates the cost of being on screen from the cost of processing updates,
    which is the split the reported symptom needs and the frame-rate harness
    cannot make.
    """
    cases = []
    for view in ("aggregated", "devices", "control"):
        for feed in ("silent", "frozen", "live"):
            cases.append(scenario(view=view, feed=feed, animation="off"))
    return cases


def matrix_attribution():
    """Where the main thread goes, with the animation on and off."""
    cases = []
    for view in VIEWS:
        for animation in ("normal", "off"):
            cases.append(scenario(view=view, animation=animation, feed="live"))
    return cases


def matrix_visibility():
    """Does a backgrounded dashboard stop working?"""
    cases = []
    for animation in ("normal", "off"):
        cases.append(scenario(view="devices", animation=animation,
                              foreground="dashboard",
                              name="visible-anim%s" % animation))
        cases.append(scenario(view="devices", animation=animation,
                              foreground="neighbour", neighbour=True,
                              name="background-anim%s" % animation))
    return cases


def matrix_neighbour():
    """The browser-wide symptom: is a second page slower with the dashboard open?

    The neighbour is a trivial page; only its own responsiveness is read. The
    control is the same page with no dashboard open at all.
    """
    cases = [
        scenario(dashboard_open=False, neighbour=True, foreground="neighbour",
                 name="neighbour-alone"),
    ]
    for animation in ("normal", "off"):
        for view in ("devices", "control"):
            cases.append(scenario(view=view, animation=animation, neighbour=True,
                                  foreground="neighbour",
                                  name="neighbour-with-%s-anim%s" % (view, animation)))
    return cases


def matrix_software():
    """Rendering-path sensitivity, recorded rather than inferred."""
    cases = []
    for software in (False, True):
        for view in ("aggregated", "devices"):
            for animation in ("normal", "off"):
                cases.append(scenario(view=view, animation=animation,
                                      software=software))
    return cases


def matrix_charts():
    """Isolate the chart and polling views from the flow views.

    `analytics` draws a uPlot canvas; `logs` runs a two-second poll of its own.
    Both are recurring work the flow views do not have.
    """
    cases = []
    for view in ("aggregated", "energy", "analytics", "logs"):
        cases.append(scenario(view=view, animation="off", feed="live",
                              name="charts-%s" % view))
    return cases


# Turns the flow tile renderer off at runtime. The layer stops being rebuilt and
# the CSS animation takes the flow back -- which animation_mode=off has already
# disabled, so the two sides of this A/B look the same on screen.
# Immediately invoked. page.evaluate() treats a string as an expression, so an
# uninvoked arrow function evaluates to a function object and the body never
# runs -- which silently turned this A/B into a comparison of two identical
# pages the first time it was written.
DISABLE_TILES = (
    "(() => { let ok = false;"
    " try { flowTileState.active = false; ok = true; } catch (e) {}"
    " document.body.classList.remove('flow-tiles-active');"
    " document.querySelectorAll('.flow-tile-layer').forEach((n) => n.remove());"
    " window.__tilesDisabled = ok;"
    " return ok; })()"
)


def matrix_flowlayer():
    """Is the flow tile layer still being rebuilt when it cannot animate?

    With animation_mode=off the tiles are painted `still`. The rebuild path
    reads geometry and computed style back for every pipe on every snapshot
    regardless, and only the DOM write is skipped when nothing changed. This
    measures what that read-back costs by removing it.
    """
    cases = []
    for view in ("aggregated", "devices", "control"):
        for animation in ("off", "normal"):
            cases.append(scenario(view=view, animation=animation, feed="live",
                                  deep_reads=True,
                                  name="tiles-on-%s-anim%s" % (view, animation)))
            cases.append(scenario(view=view, animation=animation, feed="live",
                                  extra_js=DISABLE_TILES, deep_reads=True,
                                  name="tiles-off-%s-anim%s" % (view, animation)))
    return cases


def matrix_reads():
    """Where does the extra time with animation OFF actually go?

    Same code, same call counts, more time per call. The candidate explanation
    is a forced style/layout flush: with an animation running the style is
    recalculated every frame anyway, so a reader arrives to a clean tree; with
    it off, the first reader pays for everything that accumulated. This charges
    the layout-forcing reads themselves, so the explanation is measured rather
    than argued.
    """
    cases = []
    for view in ("devices", "control"):
        for animation in ("normal", "off"):
            cases.append(scenario(view=view, animation=animation, feed="live",
                                  deep_reads=True,
                                  name="reads-%s-anim%s" % (view, animation)))
    return cases


def matrix_reads_after():
    """The next layer down: what still forces layout once the first fix is in.

    The control and energy views no longer rebuild the tile layer at all, and
    their remaining cost sits in the snapshot handler -- which shows the same
    animation-off penalty, so the same class of cause is likely. This charges
    the reads in every view so the next target is chosen from a measurement.
    """
    cases = []
    for view in ("aggregated", "devices", "control", "energy"):
        for animation in ("normal", "off"):
            cases.append(scenario(view=view, animation=animation, feed="live",
                                  deep_reads=True, devices=4,
                                  name="reads2-%s-anim%s" % (view, animation)))
    return cases


# Switches off the animated border on control-stage results, through the CSSOM
# so the dashboard's `style-src 'self'` CSP cannot refuse it. Returns how many
# rules it changed, so a run that treated nothing is visible in the report.
DISABLE_RESULT_BORDER = (
    "(() => { let n = 0;"
    " for (const sheet of document.styleSheets) {"
    "   try {"
    "     for (const rule of sheet.cssRules) {"
    "       if (rule.style && rule.style.animationName === 'controlResultBorderFlow') {"
    "         rule.style.animation = 'none'; n += 1; } } }"
    "   catch (e) {} }"
    " return n; })()"
)


def matrix_result_border():
    """Chromium draws the control view at 46 fps with the animation on.

    Less main-thread work than with it off, so the cost is compositor-side. The
    candidate is the control-stage result border: `background-position` animated
    on a pseudo-element that also carries `mask-composite: exclude`. Neither is
    a compositable property, so every frame repaints the element and recomputes
    its mask, once per stage per device.
    """
    # animation="normal" is not a detail: `.dashboard-animation-off` already
    # stops this keyframe, so running the A/B at this module's default of "off"
    # compares two pages that both have it off -- which is exactly what the
    # first attempt did.
    cases = []
    for devices in (2, 4, 8):
        cases.append(scenario(view="control", devices=devices, animation="normal",
                              name="border-on-control-%ddev" % devices))
        cases.append(scenario(view="control", devices=devices, animation="normal",
                              extra_js=DISABLE_RESULT_BORDER,
                              name="border-off-control-%ddev" % devices))
    # The same keyframe also drives .primary-button.compact::after, which is in
    # the top bar of every view -- so the aggregated view is the control for
    # "does one of these cost anything on its own".
    for view in ("aggregated", "devices"):
        cases.append(scenario(view=view, animation="normal",
                              name="border-on-%s" % view))
        cases.append(scenario(view=view, animation="normal",
                              extra_js=DISABLE_RESULT_BORDER,
                              name="border-off-%s" % view))
    return cases


DISABLE_RESULT_MASK = (
    "(() => { let n = 0;"
    " for (const sheet of document.styleSheets) {"
    "   try {"
    "     for (const rule of sheet.cssRules) {"
    "       if (rule.style && rule.style.animationName === 'controlResultBorderFlow') {"
    "         rule.style.webkitMask = 'none'; rule.style.mask = 'none';"
    "         rule.style.webkitMaskComposite = 'add'; rule.style.maskComposite = 'add';"
    "         n += 1; } } }"
    "   catch (e) {} }"
    " return n; })()"
)


def matrix_result_border_parts():
    """Which half of the effect costs: the animated paint, or the mask?

    The answer decides which fix is the minimal one. Keeping the animation and
    replacing the mask is a different change from keeping the mask and making
    the animation compositable, and only a measurement can say which is needed.
    """
    cases = []
    for browser_view in ("control",):
        for devices in (4, 8):
            cases.append(scenario(view=browser_view, devices=devices, animation="normal",
                                  name="parts-both-%ddev" % devices))
            cases.append(scenario(view=browser_view, devices=devices, animation="normal",
                                  extra_js=DISABLE_RESULT_BORDER,
                                  name="parts-noanim-%ddev" % devices))
            cases.append(scenario(view=browser_view, devices=devices, animation="normal",
                                  extra_js=DISABLE_RESULT_MASK,
                                  name="parts-nomask-%ddev" % devices))
    return cases


def matrix_throttle():
    """Slow the main thread down until the cost becomes visible.

    This desktop has so much headroom that the defect found in this
    investigation cost 25-36 ms per snapshot and never dropped a frame, which is
    why a frame-rate harness could not find it. Chromium's CPU throttling scales
    main-thread work by an exact factor, so the same page can be measured on the
    class of machine where 25 ms matters -- without inventing a macOS number.
    """
    cases = []
    for rate in (1, 4, 8, 16):
        for view in ("aggregated", "control"):
            for animation in ("normal", "off"):
                cases.append(scenario(view=view, animation=animation,
                                      cpu_throttle=rate,
                                      name="throttle%dx-%s-anim%s" % (rate, view, animation)))
    return cases


def matrix_devices():
    cases = []
    for devices in (2, 4, 8):
        for view in ("devices", "control"):
            cases.append(scenario(view=view, devices=devices, animation="off",
                                  name="scale-%s-%ddev" % (view, devices)))
    return cases


MATRICES = {
    "feed": matrix_feed,
    "attribution": matrix_attribution,
    "visibility": matrix_visibility,
    "neighbour": matrix_neighbour,
    "software": matrix_software,
    "charts": matrix_charts,
    "flowlayer": matrix_flowlayer,
    "reads": matrix_reads,
    "throttle": matrix_throttle,
    "reads-after": matrix_reads_after,
    "result-border": matrix_result_border,
    "result-border-parts": matrix_result_border_parts,
    "devices": matrix_devices,
}


def run_case(node, case, browser, gpu, duration_ms, max_load):
    quiet = bench.wait_for_quiet(max_load) if hasattr(bench, "wait_for_quiet") else None
    load = bench.load_average() if hasattr(bench, "load_average") else None
    feed = FEEDS[case["feed"]]
    server = start_server(
        host="127.0.0.1",
        port=0,
        device_count=case["devices"],
        freeze_timestamp=feed["freeze_timestamp"],
        sse_interval=feed["sse_interval"],
    )
    host, port = server.server_address
    payload = {
        "url": "http://%s:%s/" % (host, port),
        "view": case["view"],
        "animation": case["animation"],
        "transport": case["transport"],
        "durationMs": duration_ms,
        "browser": browser,
        "gpu": gpu,
        "software": bool(case["software"]),
        "deepReads": bool(case["deep_reads"]),
        "cpuThrottle": int(case["cpu_throttle"]),
        "foreground": case["foreground"],
        "dashboardOpen": bool(case["dashboard_open"]),
        "neighbourUrl": None,
        "extraJs": case["extra_js"],
        "navigationTimeoutMs": 90000,
    }
    if case["neighbour"] and case["foreground"] != "neighbour":
        payload["foreground"] = case["foreground"]
    try:
        completed = subprocess.run(
            [node, DRIVER, json.dumps(payload)],
            cwd=ROOT, capture_output=True, text=True, check=False,
            timeout=max(300, duration_ms / 1000 * 6 + 180),
        )
    finally:
        server.shutdown()
        server.server_close()
    entry = {"case": case, "load_average": load, "quiet_gate": quiet}
    if completed.returncode != 0:
        entry["error"] = completed.stderr.strip()[:2000]
    else:
        entry["result"] = json.loads(completed.stdout)
    return entry


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", choices=sorted(MATRICES), default="attribution")
    parser.add_argument("--browser", choices=("chromium", "firefox"), default="firefox")
    parser.add_argument("--gpu", choices=("software", "gpu", "headed"), default="headed")
    parser.add_argument("--max-load", type=float, default=2.0)
    parser.add_argument("--duration-ms", type=int, default=10000)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--label", default=None)
    args = parser.parse_args(argv)

    node = bench.node_binary()
    if not node:
        raise SystemExit("node is required to drive Playwright")
    executable = bench.browser_available(node, args.browser)
    if not executable:
        raise SystemExit(
            "playwright's %s build is not installed; run: npx playwright install %s"
            % (args.browser, args.browser)
        )

    cases = MATRICES[args.matrix]()
    runs = []
    started = time.monotonic()
    for repeat in range(args.repeat):
        for index, case in enumerate(cases, 1):
            label = "[%d/%d] %s" % (index, len(cases), case["name"])
            if args.repeat > 1:
                label += " (run %d/%d)" % (repeat + 1, args.repeat)
            print(label, file=sys.stderr, flush=True)
            entry = run_case(node, case, args.browser, args.gpu,
                             args.duration_ms, args.max_load)
            entry["repeat"] = repeat + 1
            runs.append(entry)

    report = {
        "schema_version": 1,
        "kind": "dashboard-profile",
        "label": args.label or args.matrix,
        "matrix": args.matrix,
        "duration_ms": args.duration_ms,
        "environment": {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "browser": args.browser,
            "browser_executable": executable,
            "gpu": args.gpu,
            "note": (
                "Read rasterisation.renderer per run rather than assuming a "
                "path. The renderer string names the device WebGL was given, "
                "not the page compositor: it can prove software, never "
                "hardware. Compare ratios within one browser and session."
            ),
        },
        "wall_clock_seconds": round(time.monotonic() - started, 1),
        "runs": runs,
    }
    os.makedirs(args.out, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    name = "profile-%s-%s-%s.json" % (report["label"], args.browser, stamp)
    path = os.path.join(args.out, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
