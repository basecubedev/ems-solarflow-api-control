#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Benchmark for the energy pipe study.

Serves ``scripts/flow_pipe_study`` over a loopback port and drives real
browsers with Playwright, one scenario at a time. Every candidate renders the
same scene with the same geometry, colours, speed, phase and magnitude, so a
difference between two runs is a difference in how the pipe is painted.

    python3 scripts/flow_pipe_study/pipe_bench.py --matrix candidates \
        --browser firefox --gpu headed

The Playwright driver, the load gate and the environment record come from
``scripts/flow_lab_bench.py``; only the scenario matrix is new here.

Never run two of these at once: they contend for CPU and both results become
fiction.
"""

import argparse
import functools
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(ROOT, "scripts")
STUDY_DIR = os.path.join(SCRIPTS, "flow_pipe_study")
DRIVER = os.path.join(SCRIPTS, "flow_lab_driver.mjs")
DEFAULT_OUT = os.path.join(ROOT, "reports", "dashboard-perf")

sys.path.insert(0, SCRIPTS)
import flow_lab_bench as lab  # noqa: E402

CANDIDATES = (
    "capsule",
    "rect",
    "radius-el",
    "gradient-capsule",
    "repeating",
    "tokens",
    "core",
    "pulse",
    "minimal",
    "arrow",
    "plasma",
    "comet",
    "particles",
    "wave",
)

GLOWS = ("none", "static", "texture", "blur", "filter", "layered", "blend")

DESIGNED = ("capsule", "arrow", "plasma", "comet", "particles", "wave")

AGGREGATE_FLOWS = (2, 4, 8, 12, 24, 48)
DEVICE_COUNTS = (2, 4, 8, 12)
WATT_SAMPLES = (40, 100, 170, 300, 690, 1200, 2000, 3000)


def scenario(**over):
    base = {
        "candidate": "capsule",
        "scenario": "aggregate",
        "flows": 12,
        "devices": 4,
        "watts": "mixed",
        "speeds": "single",
        "reverse": "mixed",
        "motion": "on",
        "tokens": 4,
        "texture": "simple",
        "tile": 1,
        "pad": 0,
        "glow": "none",
        "anim": "var",
        "tabs": 1,
        "trace": False,
    }
    base.update(over)
    if "name" not in base:
        suffix = "" if base["candidate"] != "tokens" else "x%s" % base["tokens"]
        base["name"] = "%s%s-%s%s-%s" % (
            base["candidate"],
            suffix,
            base["scenario"],
            base["devices"] if base["scenario"] == "devices" else base["flows"],
            base["motion"],
        )
    return base


def matrix_smoke():
    return [scenario(candidate=name, name="smoke-%s" % name) for name in CANDIDATES]


def matrix_candidates():
    """Every candidate at one realistic scene, moving and still.

    The still run is not padding: it separates what the construction costs to
    paint once from what it costs to animate, and those are different questions.
    """
    cases = []
    for name in CANDIDATES:
        variants = [4] if name == "tokens" else [None]
        for tokens in variants:
            extra = {"tokens": tokens} if tokens else {}
            for motion in ("on", "off"):
                cases.append(scenario(candidate=name, motion=motion, **extra))
    return cases


def matrix_stress():
    """Where the candidates can actually differ.

    At a realistic twelve pipes every construction reaches the display's
    refresh ceiling, so a comparison there measures the monitor. These scenes
    are past the point where that is true.
    """
    cases = []
    for name in CANDIDATES:
        variants = [4] if name == "tokens" else [None]
        for tokens in variants:
            extra = {"tokens": tokens} if tokens else {}
            for flows in (48, 96):
                cases.append(scenario(candidate=name, flows=flows, **extra))
    return cases


def matrix_scaling():
    cases = []
    for name in ("capsule", "rect", "radius-el", "tokens", "core"):
        for flows in AGGREGATE_FLOWS:
            cases.append(scenario(candidate=name, flows=flows))
    for flows in (96, 192):
        cases.append(scenario(candidate="capsule", flows=flows))
    return cases


def matrix_devices():
    cases = []
    for name in ("capsule", "rect", "tokens", "core"):
        for devices in DEVICE_COUNTS:
            cases.append(scenario(candidate=name, scenario="devices", devices=devices))
    return cases


def matrix_tokencount():
    """Is the cost of N animated elements per pipe constant, linear or worse?"""
    cases = []
    for flows in (12, 48):
        for count in (1, 2, 4, 8, 16):
            cases.append(
                scenario(candidate="tokens", tokens=count, flows=flows,
                         name="tokens-x%d-f%d" % (count, flows))
            )
        cases.append(scenario(candidate="capsule", flows=flows,
                              name="tokens-capsule-f%d" % flows))
    return cases


def matrix_paint():
    """Painted area: a bigger texture and a bigger layer, same picture.

    Both axes are verified pixel-identical by pipe_verify.mjs, so anything
    measured here is the cost of the area and not of a different image.
    """
    cases = [scenario(name="paint-baseline")]
    for tile in (4, 16):
        cases.append(scenario(tile=tile, name="paint-tile%d" % tile))
    for pad in (24, 72):
        cases.append(scenario(pad=pad, name="paint-pad%d" % pad))
    cases.append(scenario(texture="rich", name="paint-rich"))
    cases.append(scenario(flows=48, name="paint-baseline-f48"))
    cases.append(scenario(flows=48, tile=16, name="paint-tile16-f48"))
    cases.append(scenario(flows=48, pad=72, name="paint-pad72-f48"))
    return cases


def matrix_paint_trace():
    return [dict(case, trace=True) for case in matrix_paint()]


def matrix_magnitude():
    cases = []
    for watts in WATT_SAMPLES:
        cases.append(scenario(watts=watts, speeds="single",
                              name="magnitude-%dw" % watts))
    cases.append(scenario(watts=0, name="magnitude-0w"))
    return cases


def matrix_tabs():
    cases = []
    for name in ("capsule", "core"):
        for tabs in (1, 2, 5, 10):
            cases.append(scenario(candidate=name, tabs=tabs,
                                  name="tabs-%s-%d" % (name, tabs)))
    return cases


def matrix_limit():
    """Where does it actually stop scaling?

    Ninety-six pipes was still free in both engines, so the limit is somewhere
    past the point any dashboard would reach. This finds it rather than
    reporting "no limit observed" from a scene that was never large enough.
    """
    cases = []
    for flows in (96, 192, 320, 480, 600):
        cases.append(scenario(candidate="capsule", flows=flows,
                              name="limit-capsule-f%d" % flows))
    for flows in (48, 96, 192):
        cases.append(scenario(candidate="tokens", tokens=8, flows=flows,
                              name="limit-tokens8-f%d" % flows))
    return cases


def matrix_glow():
    """Seven ways to make it glow, on the control, at three sizes.

    The previous study recorded a filter on the animated layer as ruinous. That
    was measured headless, on a software rasteriser, and a promoted layer is
    rasterised once and then only moved -- so the claim is re-tested here rather
    than inherited.
    """
    cases = []
    for mode in GLOWS:
        for flows in (12, 48, 96):
            cases.append(scenario(glow=mode, flows=flows,
                                  name="glow-%s-f%d" % (mode, flows)))
    return cases


def matrix_designs():
    """The designed flows against the control, with and without a halo."""
    cases = []
    for name in DESIGNED:
        for mode in ("none", "texture"):
            for flows in (12, 48):
                cases.append(scenario(candidate=name, glow=mode, flows=flows,
                                      name="design-%s-%s-f%d" % (name, mode, flows)))
    for name in DESIGNED:
        cases.append(scenario(candidate=name, glow="texture", flows=96,
                              name="design-%s-texture-f96" % name))
    return cases


def matrix_anim():
    """Is the animation actually on the compositor?

    The trace counted one style recalculation per frame for a transform
    animation that should never have touched the main thread. The suspect is the
    keyframe reading a custom property -- which production does too. `waapi`
    expresses the same motion with literal values.
    """
    cases = []
    for mode in ("var", "waapi"):
        for flows in (12, 48, 96, 192):
            cases.append(scenario(anim=mode, flows=flows,
                                  name="anim-%s-f%d" % (mode, flows)))
        cases.append(scenario(anim=mode, candidate="tokens", tokens=4, flows=48,
                              name="anim-%s-tokens48" % mode))
        cases.append(scenario(anim=mode, scenario="devices", devices=12,
                              name="anim-%s-dev12" % mode))
    return cases


def matrix_twolayer():
    """`plasma` at the sizes a dashboard actually draws.

    It is the one design that needs two animated layers per segment, so it is
    the one whose realistic-size cost must be measured rather than inferred
    from the layer-count law.
    """
    cases = []
    for name in ("capsule", "plasma"):
        for devices in (8, 12):
            cases.append(scenario(candidate=name, scenario="devices",
                                  devices=devices, glow="blur",
                                  name="twolayer-%s-dev%d" % (name, devices)))
    return cases


def matrix_finalists():
    cases = []
    for name in ("capsule", "rect", "radius-el", "core"):
        for flows in (12, 48):
            cases.append(scenario(candidate=name, flows=flows,
                                  name="final-%s-f%d" % (name, flows)))
    return cases


MATRICES = {
    "smoke": matrix_smoke,
    "candidates": matrix_candidates,
    "scaling": matrix_scaling,
    "stress": matrix_stress,
    "devices": matrix_devices,
    "tokencount": matrix_tokencount,
    "paint": matrix_paint,
    "paint-trace": matrix_paint_trace,
    "magnitude": matrix_magnitude,
    "tabs": matrix_tabs,
    "finalists": matrix_finalists,
    "limit": matrix_limit,
    "glow": matrix_glow,
    "designs": matrix_designs,
    "anim": matrix_anim,
    "twolayer": matrix_twolayer,
}


def start_server():
    handler = functools.partial(lab.QuietHandler, directory=STUDY_DIR)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def case_url(host, port, case):
    query = urlencode({
        "candidate": case["candidate"],
        "scenario": case["scenario"],
        "flows": case["flows"],
        "devices": case["devices"],
        "watts": case["watts"],
        "speeds": case["speeds"],
        "reverse": case["reverse"],
        "motion": case["motion"],
        "tokens": case["tokens"],
        "texture": case["texture"],
        "tile": case["tile"],
        "pad": case["pad"],
        "glow": case["glow"],
        "anim": case["anim"],
    })
    return "http://%s:%s/index.html?%s" % (host, port, query)


def run_case(node, server, case, browser, duration_ms, gpu, max_load):
    quiet = lab.wait_for_quiet(max_load)
    load = lab.load_average()
    host, port = server.server_address
    payload = {
        "url": case_url(host, port, case),
        "tabs": case["tabs"],
        "durationMs": duration_ms,
        "browser": browser,
        "gpu": gpu,
        "trace": bool(case.get("trace")) and browser == "chromium",
        "navigationTimeoutMs": 90000,
    }
    result = subprocess_run(node, payload, case)
    if result is None:
        return {"case": case, "load_average": load, "quiet_gate": quiet,
                "error": "driver failed"}
    if isinstance(result, str):
        return {"case": case, "load_average": load, "quiet_gate": quiet, "error": result}
    return {"case": case, "load_average": load, "quiet_gate": quiet, "result": result}


def subprocess_run(node, payload, case):
    import subprocess

    completed = subprocess.run(
        [node, DRIVER, json.dumps(payload)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=max(300, payload["durationMs"] / 1000 * 6 + 120 * max(1, case["tabs"])),
    )
    if completed.returncode != 0:
        return completed.stderr.strip()[:2000]
    return json.loads(completed.stdout)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", choices=sorted(MATRICES), default="smoke")
    parser.add_argument("--browser", choices=("chromium", "firefox"), default="chromium")
    parser.add_argument("--gpu", choices=("software", "gpu", "headed"), default="headed")
    parser.add_argument("--max-load", type=float, default=2.0)
    parser.add_argument("--duration-ms", type=int, default=8000)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--label", default=None)
    parser.add_argument("--print-only", action="store_true")
    parser.add_argument(
        "--trace",
        action="store_true",
        help=(
            "Chromium only: record a DevTools trace per case and count "
            "paint/raster/commit work. At the refresh ceiling the frame rate "
            "is the monitor's, so this is what still distinguishes two "
            "constructions."
        ),
    )
    args = parser.parse_args(argv)

    node = lab.node_binary()
    if not node:
        raise SystemExit("node is required to drive Playwright")
    executable = lab.browser_available(node, args.browser)
    if not executable:
        raise SystemExit("playwright's %s build is not installed" % args.browser)

    cases = MATRICES[args.matrix]()
    if args.trace:
        cases = [dict(case, trace=True) for case in cases]
    server = start_server()
    runs = []
    started = time.monotonic()
    try:
        for repeat in range(args.repeat):
            for index, case in enumerate(cases, 1):
                label = "[%d/%d] %s" % (index, len(cases), case["name"])
                if args.repeat > 1:
                    label += " (run %d/%d)" % (repeat + 1, args.repeat)
                print(label, file=sys.stderr, flush=True)
                entry = run_case(node, server, case, args.browser,
                                 args.duration_ms, args.gpu, args.max_load)
                entry["repeat"] = repeat + 1
                runs.append(entry)
    finally:
        server.shutdown()
        server.server_close()

    report = {
        "schema_version": 1,
        "kind": "energy-pipe-study",
        "label": args.label or args.matrix,
        "matrix": args.matrix,
        "trace_requested": bool(args.trace),
        "duration_ms": args.duration_ms,
        "environment": lab.environment(args.browser, executable, args.gpu),
        "wall_clock_seconds": round(time.monotonic() - started, 1),
        "runs": runs,
    }

    if args.print_only:
        print(json.dumps(report, indent=2))
        return 0

    os.makedirs(args.out, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    name = "pipes-%s-%s-%s.json" % (report["label"], args.browser, stamp)
    path = os.path.join(args.out, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
