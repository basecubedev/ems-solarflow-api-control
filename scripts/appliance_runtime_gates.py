#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Turn what the guests proved into evidence a release can be bound to.

    scripts/appliance_runtime_gates.py --output FILE
        [--from-log GATE=PATH]... [--gate GATE=RESULT[:REASON]]...
        [--environment TEXT]

A runtime gate used to be a sentence in a summary. A sentence cannot be
re-checked: it does not say which log it came from, whether that log still says
the same thing, or what a gate that did not run was waiting for.

Each entry here carries its result, the digest of the log it was read out of,
the environment that produced it and — when it did not run — the exact
prerequisite. ``--from-log`` reads the verdict a guest script printed rather
than accepting one on the command line; ``--gate`` is for the tiers whose
verdict is a pytest run, and it can never invent a pass for a gate that has a
log saying otherwise.

Exit status: 0 every required gate passed, 1 one did not.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from appliance import runtime_gates  # noqa: E402

VERDICTS = {
    "RESULT: PASS": runtime_gates.PASS,
    "RESULT: FAIL": runtime_gates.FAIL,
    "RESULT: NOT RUN": runtime_gates.NOT_RUN,
    "RESULT: INCOMPLETE": runtime_gates.FAIL,
}


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", required=True)
    parser.add_argument("--from-log", action="append", default=[], metavar="GATE=PATH")
    parser.add_argument("--gate", action="append", default=[], metavar="GATE=RESULT[:REASON]")
    parser.add_argument("--environment", default="")
    parser.add_argument("--created-at", default="")
    return parser.parse_args(argv)


def verdict_of(path):
    """The last RESULT line a guest script printed, and the line itself."""

    text = Path(path).read_text(encoding="utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip().startswith("RESULT:")]
    if not lines:
        return runtime_gates.NOT_RUN, "the log carries no verdict"
    last = lines[-1]
    for prefix, result in VERDICTS.items():
        if last.startswith(prefix):
            return result, last
    return runtime_gates.NOT_RUN, last


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    records, problems = [], []

    for item in args.from_log:
        name, _, path = item.partition("=")
        target = Path(path)
        if not target.is_file():
            records.append(
                runtime_gates.record(
                    name,
                    runtime_gates.NOT_RUN,
                    reason=f"no log at {path}; run the tier that writes it",
                    environment=args.environment,
                )
            )
            continue
        result, detail = verdict_of(target)
        records.append(
            runtime_gates.record(
                name,
                result,
                reason=detail,
                evidence=target,
                environment=args.environment,
            )
        )

    logged = {record.name for record in records}
    for item in args.gate:
        name, _, rest = item.partition("=")
        result, _, reason = rest.partition(":")
        if name in logged:
            problems.append(f"{name} has a log; a command-line verdict may not replace it")
            continue
        records.append(
            runtime_gates.record(
                name, result, reason=reason, environment=args.environment
            )
        )

    if problems:
        for problem in problems:
            print(f"appliance-runtime-gates: {problem}", file=sys.stderr)
        return 2

    gates = runtime_gates.build(records, created_at=args.created_at)
    runtime_gates.write(args.output, gates)

    for name in runtime_gates.GATES:
        entry = gates.gates.get(name)
        marker = "required" if name in runtime_gates.REQUIRED_GATES else "optional"
        result = entry.result if entry else "absent"
        print(f"{name:<28} {result:<8} {marker}")
        if entry and entry.result != runtime_gates.PASS:
            print(f"  {entry.reason}")
    print()
    print(f"runtime gates: {args.output}")
    if gates.required_pass:
        print("RESULT: PASS")
        return 0
    print(f"RESULT: FAIL ({', '.join(gates.unmet)})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
