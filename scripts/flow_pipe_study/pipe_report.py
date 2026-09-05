#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Turn energy pipe study measurements into the tables the report quotes.

    python3 scripts/flow_pipe_study/pipe_report.py reports/dashboard-perf/pipes-*.json

Every table carries the browser, the rasterisation path and the load the case
ran under, because a number without those is not comparable to anything.
"""

import argparse
import json
import os
import sys
from collections import defaultdict


def load(paths):
    reports = []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            report = json.load(handle)
        report["_path"] = os.path.basename(path)
        reports.append(report)
    return reports


def rows(report):
    for entry in report["runs"]:
        case = entry["case"]
        if "error" in entry:
            yield {"name": case["name"], "case": case, "error": entry["error"]}
            continue
        result = entry["result"]
        totals = result["totals"]
        lab = result["perTab"][0].get("lab") or {}
        trace = result.get("trace") or {}
        counts = trace.get("counts") or {}
        durations = trace.get("totalMs") or {}
        yield {
            "name": case["name"],
            "case": case,
            "fps": totals["foregroundFps"],
            "meanFps": totals["meanFps"],
            "frameP95": totals["worstFrameP95Ms"],
            "lagP95": totals["worstLagP95Ms"],
            "lagMax": totals["worstLagMaxMs"],
            "longTasks": totals["longTasks"],
            "longTaskMs": totals["longTaskTotalMs"],
            "mutations": totals["mutations"],
            "animated": lab.get("animatedElements"),
            "painted": lab.get("paintedElements"),
            "cssAnimations": lab.get("cssAnimations"),
            "flows": lab.get("flows"),
            "stagePx": lab.get("stagePx"),
            "raster": (result.get("rasterisation") or {}).get("renderer"),
            "load": entry.get("load_average"),
            "rasterTasks": counts.get("RasterTask"),
            "rasterMs": durations.get("RasterTask"),
            "paints": counts.get("Paint"),
            "paintMs": durations.get("Paint"),
            "commits": counts.get("Commit"),
            "drawFrames": counts.get("DrawFrame"),
            "layoutMs": durations.get("Layout"),
            "styleMs": durations.get("UpdateLayoutTree"),
        }


def number(value, digits=1):
    if value is None:
        return "-"
    return ("%%.%df" % digits) % value


def table(report, keys, headers, digits):
    lines = ["| case | " + " | ".join(headers) + " |",
             "|---|" + "---:|" * len(headers)]
    for row in rows(report):
        if "error" in row:
            lines.append("| %s | %s |" % (row["name"], " | ".join(["err"] * len(headers))))
            continue
        cells = []
        for key, digit in zip(keys, digits):
            value = row.get(key)
            cells.append(number(value, digit) if isinstance(value, float) else
                         ("-" if value is None else str(value)))
        lines.append("| %s | %s |" % (row["name"], " | ".join(cells)))
    return "\n".join(lines)


PROFILES = {
    "default": (
        ["fps", "frameP95", "lagP95", "longTaskMs", "animated", "painted"],
        ["fps", "frame p95 ms", "lag p95 ms", "long task ms", "animated", "painted"],
        [1, 1, 1, 0, 0, 0],
    ),
    "trace": (
        ["fps", "frameP95", "rasterTasks", "rasterMs", "paints", "paintMs", "commits", "styleMs"],
        ["fps", "frame p95", "raster tasks", "raster ms", "paints", "paint ms", "commits", "style ms"],
        [1, 1, 0, 1, 0, 1, 0, 1],
    ),
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="default")
    args = parser.parse_args(argv)

    reports = load(args.paths)
    grouped = defaultdict(list)
    for report in reports:
        grouped[(report["matrix"], report["environment"]["browser"])].append(report)

    keys, headers, digits = PROFILES[args.profile]
    out = []
    for (matrix, browser), items in sorted(grouped.items()):
        for report in items:
            env = report["environment"]
            observed = set()
            for row in rows(report):
                if row.get("raster"):
                    observed.add(row["raster"])
            loads = [row["load"][0] for row in rows(report) if row.get("load")]
            out.append("### %s — %s (%s)\n" % (matrix, browser, report["_path"]))
            out.append("gpu mode `%s`; rasteriser %s; load %.2f-%.2f\n" % (
                env["gpu"],
                ", ".join(sorted(observed)) or "unrecorded",
                min(loads) if loads else 0.0,
                max(loads) if loads else 0.0,
            ))
            out.append(table(report, keys, headers, digits))
            out.append("")
    sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
