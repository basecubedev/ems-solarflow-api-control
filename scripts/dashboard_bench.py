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
    """backdrop-filter with the flow animation off.

    Measured with the animation on, the animation dominates and the backdrop
    question cannot be answered: that is what the first run of this experiment
    showed. Isolating means removing the larger effect, not averaging it in.
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


MATRICES = {
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


def run_case(node, case, browser, duration_ms, sse_interval, sse_max_per_ip):
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
        return {"case": case, "error": result.stderr.strip()[:2000]}
    return {"case": case, "result": json.loads(result.stdout)}


def environment(browser, executable):
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "browser": browser,
        "browser_executable": executable,
        "note": (
            "Firefox on macOS is the reported symptom's environment and is not "
            "reproducible here; these numbers are Linux."
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", choices=sorted(MATRICES), default="quick")
    parser.add_argument("--browser", choices=("chromium", "firefox"), default="chromium")
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
            )
            entry["repeat"] = repeat + 1
            runs.append(entry)

    report = {
        "schema_version": 1,
        "label": args.label or args.matrix,
        "matrix": args.matrix,
        "duration_ms": args.duration_ms,
        "sse_max_per_ip": args.sse_max_per_ip,
        "environment": environment(args.browser, executable),
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
