#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Turn flow-lab benchmark JSON into the comparison tables.

    python3 scripts/flow_lab_report.py reports/dashboard-perf/flowlab-*.json

Two ratios carry the argument:

* ``on/off`` -- a renderer's frame rate with its animation running, divided by
  the same renderer standing still. It is the only figure that isolates the
  cost of the motion from the cost of the scene, and it is what "close to the
  no-animation baseline" means.
* ``vs control`` -- the candidate against ``dashoffset``, the technique in
  production today.

Everything is a median over repeats, and only runs of the same browser from the
same report are ever compared.
"""

import argparse
import json
import statistics
import sys
from collections import defaultdict


def median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def load(paths):
    reports = []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("kind") != "flow-lab":
            continue
        data["_path"] = path
        reports.append(data)
    return reports


def collect(report):
    """key -> list of per-run measurements."""

    grouped = defaultdict(list)
    for entry in report["runs"]:
        case = entry["case"]
        if "result" not in entry:
            grouped[key_of(case)].append({"error": entry.get("error", "failed")})
            continue
        result = entry["result"]
        totals = result["totals"]
        foreground = result["perTab"][-1]
        grouped[key_of(case)].append(
            {
                "fps": totals.get("foregroundFps"),
                "meanFps": totals.get("meanFps"),
                "lagP95": totals.get("worstLagP95Ms"),
                "lagMax": totals.get("worstLagMaxMs"),
                "mutations": totals.get("mutations"),
                "longTaskMs": totals.get("longTaskTotalMs"),
                "elements": (
                    (foreground.get("lab") or {}).get("svgElements", 0)
                    + (foreground.get("lab") or {}).get("overlayElements", 0)
                ),
                "paintedPx": (foreground.get("lab") or {}).get("paintedPx"),
                "trace": result.get("trace"),
            }
        )
    return grouped


def key_of(case):
    # The metaphor is part of a candidate's identity: dom-tiles/dash and
    # dom-tiles/chevron are the same mechanism painted differently, and rolling
    # them together would silently average two different experiments.
    label = case["renderer"]
    metaphor = case.get("metaphor", "dash")
    if metaphor and metaphor != "dash":
        label = "%s/%s" % (label, metaphor)
    return (
        label, case["flows"], case["tabs"], case["motion"],
        case["active"], case.get("glow", "both"),
    )


def summarize(measurements):
    ok = [m for m in measurements if "error" not in m]
    if not ok:
        return None
    return {
        "runs": len(ok),
        "fps": median([m["fps"] for m in ok]),
        "meanFps": median([m["meanFps"] for m in ok]),
        "lagP95": median([m["lagP95"] for m in ok]),
        "lagMax": median([m["lagMax"] for m in ok]),
        "longTaskMs": median([m["longTaskMs"] for m in ok]),
        "elements": ok[0]["elements"],
        "paintedPx": ok[0]["paintedPx"],
        "trace": next((m["trace"] for m in ok if m.get("trace")), None),
    }


def ratio(a, b):
    if not a or not b:
        return None
    return a / b


def print_report(report):
    grouped = collect(report)
    summaries = {key: summarize(values) for key, values in grouped.items()}
    browser = report["environment"]["browser"]
    print("=" * 78)
    print("%s  |  matrix %s  |  %s  |  %.0fs"
          % (report["_path"].split("/")[-1], report["matrix"], browser,
             report.get("wall_clock_seconds", 0)))
    print("=" * 78)

    flows_seen = sorted({key[1] for key in summaries})
    tabs_seen = sorted({key[3] if False else key[2] for key in summaries})

    for flows in flows_seen:
        for tabs in tabs_seen:
            rows = {
                key: value for key, value in summaries.items()
                if key[1] == flows and key[2] == tabs
            }
            glows = sorted({key[5] for key in rows})
            if not rows:
                continue
            control = rows.get(("dashoffset", flows, tabs, "on", 1.0, "both"))
            if control is None:
                for (name, f, t, motion, active, g), value in rows.items():
                    if name == "dashoffset" and motion == "on" and g == "both":
                        control = value
                        break
            control_fps = control["fps"] if control else None
            header = "flows %d, tabs %d" % (flows, tabs)
            print("\n%s" % header)
            print("%-26s %8s %8s %9s %9s %8s %9s %6s"
                  % ("renderer", "fps on", "fps off", "on/off", "vs ctl",
                     "lagP95", "elements", "runs"))
            # Whatever the report actually contains, in a stable order, so a
            # renderer added to the lab is not silently missing from the table.
            preferred = ("none", "dashoffset", "svg-transform", "svg-pattern",
                         "svg-mask", "dom-tiles", "motion-path", "canvas",
                         "canvas-bloom", "canvas-worker", "webgl")
            present = sorted({key[0] for key in rows})
            ordered = [n for n in preferred if n in present]
            ordered += [n for n in present if n not in preferred]
            for renderer in ordered:
              for glow in glows:
                on = None
                off = None
                for (name, f, t, motion, active, g), value in rows.items():
                    if name != renderer or g != glow:
                        continue
                    if motion == "on":
                        on = value
                    else:
                        off = value
                if not on:
                    continue
                on_off = ratio(on["fps"], off["fps"] if off else None)
                vs_control = ratio(on["fps"], control_fps)
                print("%-26s %8.1f %8s %9s %9s %8.1f %9d %6d" % (
                    renderer + ("" if glow == "both" else "/" + glow),
                    on["fps"] or 0,
                    ("%.1f" % off["fps"]) if off and off["fps"] else "-",
                    ("%.2f" % on_off) if on_off else "-",
                    ("%.2f" % vs_control) if vs_control else "-",
                    on["lagP95"] or 0,
                    on["elements"] or 0,
                    on["runs"],
                ))

    traced = {key: value for key, value in summaries.items() if value and value.get("trace")}
    if traced:
        print("\nChromium trace, events in the measured window")
        names = ["UpdateLayoutTree", "Layout", "PrePaint", "Paint",
                 "UpdateLayerTree", "RasterTask", "CompositeLayers", "Commit"]
        print("%-15s %s" % ("renderer", ""), end="")
        print(" ".join("%9s" % n[:9] for n in names))
        for key in sorted(traced, key=lambda k: k[0]):
            counts = traced[key]["trace"]["counts"]
            print("%-15s " % key[0] + " ".join("%9d" % counts.get(n, 0) for n in names))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)
    reports = load(args.paths)
    if not reports:
        print("no flow-lab reports given", file=sys.stderr)
        return 1
    for report in reports:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
