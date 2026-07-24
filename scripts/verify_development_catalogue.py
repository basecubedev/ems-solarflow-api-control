#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verify one exact installable System Build in a production catalogue."""

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from admin.development_catalogue import load_development_builds  # noqa: E402


def _attempt_source(source, attempt):
    if urlparse(source).scheme not in {"http", "https"}:
        return source
    separator = "&" if "?" in source else "?"
    return f"{source}{separator}catalogue_attempt={attempt}"


def _find(args, attempt):
    with tempfile.TemporaryDirectory(prefix="catalogue-verification-") as directory:
        builds = load_development_builds(
            _attempt_source(args.source, attempt),
            cache_path=Path(directory) / "cache.json",
        )
    return next((entry for entry in builds if entry.get("tag") == args.tag), None)


def _mismatches(entry, args):
    expected = {
        "tag": args.tag,
        "channel": "development",
        "revision": args.revision,
        "build_id": args.build_id,
        "admin_image": args.admin_image,
        "admin_digest": args.admin_digest,
        "ems_image": args.ems_image,
        "ems_digest": args.ems_digest,
        "installable": True,
    }
    return {
        field: {"actual": entry.get(field), "expected": value}
        for field, value in expected.items()
        if entry.get(field) != value
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--admin-image", required=True)
    parser.add_argument("--admin-digest", required=True)
    parser.add_argument("--ems-image", required=True)
    parser.add_argument("--ems-digest", required=True)
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--delay-seconds", type=float, default=0)
    args = parser.parse_args(argv)

    failure = "entry was not found"
    for attempt in range(max(1, args.attempts)):
        entry = _find(args, attempt)
        if entry is not None:
            mismatches = _mismatches(entry, args)
            if not mismatches:
                print(json.dumps(entry, sort_keys=True))
                return 0
            failure = json.dumps(mismatches, sort_keys=True)
        if attempt + 1 < max(1, args.attempts):
            time.sleep(max(0, args.delay_seconds))
    print(f"catalogue verification failed for {args.tag}: {failure}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
