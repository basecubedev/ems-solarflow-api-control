#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render a profile run into a table, and say what the numbers mean.

``profile_bench.py`` writes JSON because a benchmark that is only readable as
prose cannot be re-checked. This turns one or more of those files into the
table a person reads, without ever recomputing anything: every column here is a
field the driver recorded.

    python3 scripts/dashboard_profile/profile_report.py \
        reports/dashboard-perf/profile-audit-scale-chromium-2026-09-04.json

``--columns`` picks a narrower set; ``--markdown`` emits a table that can be
pasted into a report.
"""

import argparse
import json
import sys

# Every column is a recorded field or a ratio of two of them. `perSnapMs` is
# the one worth knowing: main-thread milliseconds charged to the snapshot
# handler, divided by the number of snapshots it actually handled.
COLUMNS = (
    "name", "fps", "frameP95Ms", "lagP95Ms", "blockingMs", "blockingTasks",
    "workMs", "perSnapMs", "snapshots", "domNodes", "mutations",
    "recalcStyle", "recalcStyleMs", "layouts", "layoutMs",
    "nodeDelta", "listenerDelta", "heapDeltaMb",
    "paints", "rasterTasks", "frames", "treatment",
)
DEFAULT_COLUMNS = (
    "name", "fps", "frameP95Ms", "lagP95Ms", "blockingMs", "workMs",
    "perSnapMs", "domNodes", "mutations", "recalcStyle", "recalcStyleMs",
    "layouts", "treatment",
)

SNAPSHOT_HANDLERS = ("listener:telemetry", "sse:onmessage")


def _level(engine, key, field="delta"):
    value = (engine or {}).get(key)
    return value.get(field) if isinstance(value, dict) else None


def row(entry):
    case = entry["case"]
    result = entry.get("result")
    if not result:
        return {"name": case["name"], "error": (entry.get("error") or "")[:120]}
    page = result["dashboard"] or result["neighbour"] or {}
    engine = result.get("engine") or {}
    trace = result.get("trace") or {}
    handler = max(
        (w for w in page.get("work", []) if w["name"] in SNAPSHOT_HANDLERS),
        key=lambda w: w["ms"], default=None,
    )
    heap = _level(engine, "JSHeapUsedSize")
    return {
        "name": case["name"],
        "view": case["view"],
        "devices": case["devices"],
        "animation": case["animation"],
        "fps": round(page["fps"], 1) if page.get("fps") else None,
        "frameP95Ms": page.get("frameP95Ms"),
        "lagP95Ms": page.get("lagP95Ms"),
        "blockingMs": page.get("blockingMs"),
        "blockingTasks": page.get("blockingTasks"),
        "workMs": page.get("attributedWorkMs"),
        "snapshots": handler["calls"] if handler else 0,
        "perSnapMs": round(handler["ms"] / handler["calls"], 1)
        if handler and handler["calls"] else None,
        "domNodes": page.get("domNodes"),
        "mutations": page.get("mutations"),
        "recalcStyle": engine.get("RecalcStyleCount"),
        "recalcStyleMs": round(engine["RecalcStyleDuration"] * 1000, 1)
        if engine.get("RecalcStyleDuration") is not None else None,
        "layouts": engine.get("LayoutCount"),
        "layoutMs": round(engine["LayoutDuration"] * 1000, 1)
        if engine.get("LayoutDuration") is not None else None,
        "nodeDelta": _level(engine, "Nodes"),
        "listenerDelta": _level(engine, "JSEventListeners"),
        "heapDeltaMb": round(heap / 1048576, 2) if heap is not None else None,
        "paints": (trace.get("paint") or {}).get("count"),
        "rasterTasks": (trace.get("rasterTask") or {}).get("count"),
        "frames": trace.get("frames"),
        # What a treatment reported back. A blank here on an A/B case means the
        # treatment matched nothing, which looks exactly like a null result.
        "treatment": result["config"].get("extraJsResult"),
    }


def render(rows, columns, markdown):
    header = list(columns)
    body = [[("" if r.get(c) is None else str(r.get(c))) for c in header] for r in rows]
    widths = [max(len(header[i]), *(len(b[i]) for b in body)) if body else len(header[i])
              for i in range(len(header))]
    if markdown:
        yield "| " + " | ".join(header) + " |"
        yield "|" + "|".join("---" for _ in header) + "|"
        for line in body:
            yield "| " + " | ".join(line) + " |"
        return
    yield "  ".join(h.ljust(widths[i]) for i, h in enumerate(header))
    for line in body:
        yield "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(line))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+")
    parser.add_argument("--columns", default=",".join(DEFAULT_COLUMNS))
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args(argv)

    columns = [c for c in args.columns.split(",") if c]
    unknown = [c for c in columns if c not in COLUMNS]
    if unknown:
        raise SystemExit("unknown column(s): %s" % ", ".join(unknown))

    for path in args.reports:
        with open(path, encoding="utf-8") as handle:
            report = json.load(handle)
        print("== %s (%s, %s, %s ms per case) ==" % (
            report["label"], report["environment"]["browser"],
            report["environment"]["gpu"], report["duration_ms"]))
        for line in render([row(e) for e in report["runs"]], columns, args.markdown):
            print(line)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
