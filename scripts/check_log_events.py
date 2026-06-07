#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Check structured EMS logs for required or forbidden event names."""

import argparse
import re
import sys


def parse_args():
    parser = argparse.ArgumentParser(
        description="Validate event=<name> entries in an EMS log file."
    )
    parser.add_argument(
        "log_file",
        help="Path to a log file produced by simulation or replay."
    )
    parser.add_argument(
        "--require",
        action="append",
        default=[],
        help="Event name that must appear at least once. Can be repeated."
    )
    parser.add_argument(
        "--forbid",
        action="append",
        default=[],
        help="Event name that must not appear. Can be repeated."
    )
    return parser.parse_args()


def extract_events(text):
    return set(re.findall(r"(?:^|\s)event=([A-Za-z0-9_:-]+)(?=\s|$)", text))


def main():
    args = parse_args()

    try:
        with open(args.log_file, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as exc:
        print(f"ERROR: cannot read log file {args.log_file}: {exc}", file=sys.stderr)
        return 2

    events = extract_events(text)
    missing = [event for event in args.require if event not in events]
    forbidden_present = [event for event in args.forbid if event in events]

    if missing:
        print("Missing required events:", file=sys.stderr)
        for event in missing:
            print(f"  - {event}", file=sys.stderr)

    if forbidden_present:
        print("Forbidden events found:", file=sys.stderr)
        for event in forbidden_present:
            print(f"  - {event}", file=sys.stderr)

    if missing or forbidden_present:
        print(
            "Observed events: "
            + (", ".join(sorted(events)) if events else "(none)"),
            file=sys.stderr
        )
        return 1

    for event in args.require:
        print(f"OK required event found: {event}")

    for event in args.forbid:
        print(f"OK forbidden event absent: {event}")

    if not args.require and not args.forbid:
        print(f"Observed {len(events)} unique event(s).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
