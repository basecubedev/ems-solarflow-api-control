#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Turn dashboard benchmark JSON into the comparison table.

    python3 scripts/dashboard_bench_report.py reports/dashboard-perf/*.json \
        > reports/dashboard-perf/comparison-2026-09-04.md

Runs are grouped by case name and compared across the report labels given, so
the same scenario measured on two builds sits on one row. A regression is never
averaged away: every case keeps its own row and the worst tab is what a totals
column reports.
"""

import argparse
import json
import os
import sys
from collections import OrderedDict

METRICS = (
    ("worstLagP95Ms", "lag p95 (ms)", "lower"),
    ("worstLagMaxMs", "lag max (ms)", "lower"),
    ("mutations", "DOM mutations", "lower"),
    ("longTaskTotalMs", "long tasks (ms)", "lower"),
    ("foregroundFps", "fps", "higher"),
)


def load(paths):
    reports = []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            report = json.load(handle)
        report["_path"] = os.path.basename(path)
        reports.append(report)
    return reports


def index_runs(report):
    runs = OrderedDict()
    for entry in report.get("runs", []):
        name = entry["case"]["name"]
        runs[name] = entry
    return runs


def fmt(value):
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def delta(before, after, direction):
    if before in (None, 0) or after is None:
        return "—"
    change = (after - before) / before * 100
    better = change < 0 if direction == "lower" else change > 0
    marker = "✅" if better and abs(change) >= 5 else ("⚠️" if not better and abs(change) >= 5 else "·")
    return f"{change:+.0f}% {marker}"


def render(reports, title):
    out = [f"# {title}", ""]
    env = reports[0].get("environment", {})
    out += [
        "Produced by `scripts/dashboard_bench.py`. Read "
        "[README.md](README.md) for what each metric means and what these runs "
        "cannot show.",
        "",
        "| Property | Value |",
        "|---|---|",
        f"| Platform | `{env.get('platform')}` |",
        f"| Recorded | {env.get('recorded_at')} |",
    ]
    for report in reports:
        out.append(
            f"| Build `{report['label']}` ({report['environment']['browser']}) "
            f"| `{report['_path']}`, {report['wall_clock_seconds']} s |"
        )
    out.append("")

    indexed = [(r, index_runs(r)) for r in reports]
    case_names = []
    for _, runs in indexed:
        for name in runs:
            if name not in case_names:
                case_names.append(name)

    baseline_label = reports[0]["label"]
    for key, label, direction in METRICS:
        out += [f"## {label}", ""]
        header = "| Case | " + " | ".join(
            f"{r['label']} ({r['environment']['browser']})" for r, _ in indexed
        )
        if len(indexed) > 1:
            header += " | vs " + baseline_label
        out.append(header + " |")
        out.append("|---" * (len(indexed) + 1 + (1 if len(indexed) > 1 else 0)) + "|")

        for name in case_names:
            values = []
            for _, runs in indexed:
                entry = runs.get(name)
                if not entry or "result" not in entry:
                    values.append(None)
                else:
                    values.append(entry["result"]["totals"].get(key))
            row = f"| `{name}` | " + " | ".join(fmt(v) for v in values)
            if len(indexed) > 1:
                row += " | " + delta(values[0], values[-1], direction)
            out.append(row + " |")
        out.append("")

    failures = [
        (r["label"], e["case"]["name"], e["error"])
        for r, runs in indexed
        for e in runs.values()
        if "error" in e
    ]
    if failures:
        out += ["## Runs that did not complete", ""]
        for label, name, error in failures:
            out.append(f"- `{label}` / `{name}`: {error.splitlines()[0][:160]}")
        out.append("")
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+")
    parser.add_argument("--title", default="Dashboard performance comparison")
    args = parser.parse_args(argv)
    reports = load(args.reports)
    if not reports:
        raise SystemExit("no reports given")
    sys.stdout.write(render(reports, args.title) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
