#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Benchmark for the flow rendering lab.

Serves ``scripts/flow_lab`` over a loopback port and drives real browsers with
Playwright, one scenario at a time. The lab renders the same scene -- the
dashboard's own pipe geometry, colours and speed buckets -- with a selectable
technique, so a difference between two runs is a difference between techniques.

    python3 scripts/flow_lab_bench.py --matrix renderers --browser firefox
    python3 scripts/flow_lab_bench.py --matrix scaling   --browser chromium
    python3 scripts/flow_lab_bench.py --matrix trace     --browser chromium

Never run two of these at once: they contend for CPU and both results become
fiction.
"""

import argparse
import functools
import json
import os
import platform
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
LAB_DIR = os.path.join(SCRIPTS, "flow_lab")
DRIVER = os.path.join(SCRIPTS, "flow_lab_driver.mjs")
DEFAULT_OUT = os.path.join(ROOT, "reports", "dashboard-perf")

RENDERERS = (
    "dashoffset",
    "canvas-bloom",
    "svg-transform",
    "svg-pattern",
    "svg-mask",
    "dom-tiles",
    "motion-path",
    "canvas",
    "canvas-worker",
    "webgl",
    "none",
)

# dom-tiles paints its pattern with a background image on the one layer it
# moves, so what the pattern looks like is independent of what it costs. These
# are the same mechanism wearing different clothes, and measuring them against
# each other is how that claim gets checked rather than assumed.
METAPHORS = (
    "dash",
    "capsule",
    "particles",
    "comet",
    "chevron",
    "pulse",
    "sweep",
)

# Four devices is what the dashboard's devices view draws: three pipes each.
REALISTIC_FLOWS = 12


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401 - silence the access log
        return

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        SimpleHTTPRequestHandler.end_headers(self)


def start_server():
    handler = functools.partial(QuietHandler, directory=LAB_DIR)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def scenario(**over):
    base = {
        "renderer": "dashoffset",
        "flows": 12,
        "motion": "on",
        "active": 1.0,
        "speeds": "mixed",
        "tabs": 1,
        "glow": "both",
        "metaphor": "dash",
        "trace": False,
    }
    base.update(over)
    if "name" not in base:
        base["name"] = "%s-f%s-t%s-%s-glow%s" % (
            base["renderer"], base["flows"], base["tabs"], base["motion"], base["glow"]
        )
    return base


def matrix_smoke():
    return [
        scenario(renderer=name, flows=12, name="smoke-%s" % name)
        for name in RENDERERS
    ]


def matrix_renderers():
    """Every candidate at realistic complexity, with its own motion-off floor."""

    cases = []
    for name in RENDERERS:
        cases.append(scenario(renderer=name, flows=REALISTIC_FLOWS))
        if name != "none":
            cases.append(scenario(renderer=name, flows=REALISTIC_FLOWS, motion="off"))
    return cases


def matrix_scaling():
    cases = []
    for name in RENDERERS:
        for flows in (1, 10, 50, 100):
            cases.append(scenario(renderer=name, flows=flows))
    return cases


def matrix_tabs():
    cases = []
    for name in RENDERERS:
        for tabs in (1, 2, 5, 10):
            cases.append(scenario(renderer=name, flows=REALISTIC_FLOWS, tabs=tabs))
    return cases


GLOW_MODES = ("both", "static-only", "energy-only", "none")


def matrix_glow():
    """Is the cost the technique, or a filter sitting on top of it?

    Two filters are involved and they have to be separated: the halo on the
    layer that animates, and the static blur on the sibling .pipe-glow. Taking
    both away at once cannot say which one was being paid for.
    """

    cases = []
    for name in ("dashoffset", "dom-tiles", "canvas", "svg-transform", "motion-path"):
        for glow in GLOW_MODES:
            cases.append(scenario(renderer=name, flows=REALISTIC_FLOWS, glow=glow))
    return cases


def matrix_glow_trace():
    return [
        scenario(renderer="dashoffset", flows=REALISTIC_FLOWS, glow=glow, trace=True,
                 name="trace-dashoffset-glow-%s" % glow)
        for glow in GLOW_MODES
    ] + [
        scenario(renderer="canvas", flows=REALISTIC_FLOWS, glow=glow, trace=True,
                 name="trace-canvas-glow-%s" % glow)
        for glow in ("both", "none")
    ]


def matrix_glow_scaling():
    """Does the filter finding hold when the scene gets bigger?"""

    cases = []
    for name in ("dashoffset", "dom-tiles", "canvas"):
        for flows in (12, 50, 100):
            for glow in ("both", "static-only"):
                cases.append(scenario(renderer=name, flows=flows, glow=glow))
    return cases


def matrix_idle():
    """Half the pipes idle, which is the ordinary state of a real dashboard."""

    return [
        scenario(renderer=name, flows=REALISTIC_FLOWS, active=0.5, name="idle-%s" % name)
        for name in RENDERERS
    ]


def matrix_trace():
    return [
        scenario(renderer=name, flows=REALISTIC_FLOWS, trace=True, name="trace-%s" % name)
        for name in RENDERERS
    ]


FINALISTS = ("dashoffset", "canvas", "canvas-bloom", "dom-tiles")


def matrix_finalists():
    """The candidates that reached the still-scene baseline, across scene size."""

    return [
        scenario(renderer=name, flows=flows)
        for name in FINALISTS
        for flows in (12, 50, 100)
    ]


def matrix_finalists_tabs():
    """The acceptance goal: 1, 2, 5 and 10 tabs open at once."""

    return [
        scenario(renderer=name, flows=REALISTIC_FLOWS, tabs=tabs)
        for name in FINALISTS
        for tabs in (1, 2, 5, 10)
    ]


def matrix_finalists_idle():
    """Half the pipes idle, which is the ordinary state of a real dashboard."""

    return [
        scenario(renderer=name, flows=REALISTIC_FLOWS, active=0.5,
                 name="idle-%s" % name)
        for name in FINALISTS
    ]


def matrix_metaphors():
    """Does the choice of visual metaphor cost anything?

    All seven are one translated layer with a different background image, so
    the expectation is that they land on top of each other. A metaphor that
    does not is the interesting result, not the boring one.
    """

    cases = []
    for metaphor in METAPHORS:
        cases.append(
            scenario(
                renderer="dom-tiles",
                metaphor=metaphor,
                flows=REALISTIC_FLOWS,
                name="metaphor-%s" % metaphor,
            )
        )
    cases.append(
        scenario(
            renderer="dom-tiles",
            flows=REALISTIC_FLOWS,
            motion="off",
            name="metaphor-floor-off",
        )
    )
    return cases


def matrix_metaphor_scaling():
    """The same question where a per-pixel difference would actually show."""

    cases = []
    for metaphor in METAPHORS:
        for flows in (12, 50, 100):
            cases.append(
                scenario(
                    renderer="dom-tiles",
                    metaphor=metaphor,
                    flows=flows,
                    name="metaphor-%s-f%d" % (metaphor, flows),
                )
            )
    return cases


def matrix_tech():
    """The technology comparison: one representative of each family.

    dashoffset is the control, dom-tiles is what ships, and the rest are the
    candidates this study exists to judge. Each gets its own motion-off floor,
    because a renderer that is cheap only because it never drew anything is a
    result that has to be excluded rather than trusted.
    """

    families = ("dashoffset", "dom-tiles", "canvas", "canvas-worker", "webgl")
    cases = []
    for name in families:
        cases.append(scenario(renderer=name, flows=REALISTIC_FLOWS, name="tech-%s" % name))
        cases.append(
            scenario(
                renderer=name,
                flows=REALISTIC_FLOWS,
                motion="off",
                name="tech-%s-off" % name,
            )
        )
    return cases


def matrix_tech_scaling():
    families = ("dashoffset", "dom-tiles", "canvas", "canvas-worker", "webgl")
    return [
        scenario(renderer=name, flows=flows, name="techscale-%s-f%d" % (name, flows))
        for name in families
        for flows in (1, 10, 50, 100)
    ]


def matrix_tech_tabs():
    families = ("dashoffset", "dom-tiles", "canvas", "canvas-worker", "webgl")
    return [
        scenario(
            renderer=name,
            flows=REALISTIC_FLOWS,
            tabs=tabs,
            name="techtabs-%s-t%d" % (name, tabs),
        )
        for name in families
        for tabs in (1, 2, 5, 10)
    ]


MATRICES = {
    "metaphors": matrix_metaphors,
    "metaphor-scaling": matrix_metaphor_scaling,
    "tech": matrix_tech,
    "tech-scaling": matrix_tech_scaling,
    "tech-tabs": matrix_tech_tabs,
    "finalists": matrix_finalists,
    "finalists-tabs": matrix_finalists_tabs,
    "finalists-idle": matrix_finalists_idle,
    "smoke": matrix_smoke,
    "renderers": matrix_renderers,
    "scaling": matrix_scaling,
    "tabs": matrix_tabs,
    "idle": matrix_idle,
    "glow": matrix_glow,
    "glow-trace": matrix_glow_trace,
    "glow-scaling": matrix_glow_scaling,
    "trace": matrix_trace,
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


def case_url(host, port, case):
    query = urlencode(
        {
            "renderer": case["renderer"],
            "flows": case["flows"],
            "motion": case["motion"],
            "active": case["active"],
            "speeds": case["speeds"],
            "glow": case["glow"],
            "metaphor": case.get("metaphor", "dash"),
        }
    )
    return "http://%s:%s/index.html?%s" % (host, port, query)


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


def run_case(node, server, case, browser, duration_ms, gpu="software", max_load=None):
    quiet = wait_for_quiet(max_load)
    load = load_average()
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
    result = subprocess.run(
        [node, DRIVER, json.dumps(payload)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=max(300, duration_ms / 1000 * 6 + 120 * max(1, case["tabs"])),
    )
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
            "Linux. The rasterisation path is not uniform: headless Chromium "
            "defaults to ANGLE/SwiftShader software rendering, while Firefox "
            "reaches the real GPU either way, so a Chromium and a Firefox run "
            "of the same case are not necessarily on the same hardware path. "
            "Read rasterisation.renderer in each entry rather than assuming. "
            "Ratios between two runs of the same browser on the same path in "
            "the same session are the comparison this harness supports."
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", choices=sorted(MATRICES), default="smoke")
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
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--label", default=None)
    parser.add_argument("--print-only", action="store_true")
    args = parser.parse_args(argv)

    node = node_binary()
    if not node:
        raise SystemExit("node is required to drive Playwright")
    executable = browser_available(node, args.browser)
    if not executable:
        raise SystemExit(
            "playwright's %s build is not installed; run: npx playwright install %s"
            % (args.browser, args.browser)
        )

    cases = MATRICES[args.matrix]()
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
                entry = run_case(node, server, case, args.browser, args.duration_ms, args.gpu, args.max_load)
                entry["repeat"] = repeat + 1
                runs.append(entry)
    finally:
        server.shutdown()
        server.server_close()

    report = {
        "schema_version": 1,
        "kind": "flow-lab",
        "label": args.label or args.matrix,
        "matrix": args.matrix,
        "duration_ms": args.duration_ms,
        "environment": environment(args.browser, executable, args.gpu),
        "wall_clock_seconds": round(time.monotonic() - started, 1),
        "runs": runs,
    }

    if args.print_only:
        print(json.dumps(report, indent=2))
        return 0

    os.makedirs(args.out, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    name = "flowlab-%s-%s-%s.json" % (report["label"], args.browser, stamp)
    path = os.path.join(args.out, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
