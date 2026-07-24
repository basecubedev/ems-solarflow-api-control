#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Atomically mutate a checked-out development-build catalogue."""

import argparse
import json
import os
import re
import tempfile
from pathlib import Path

MAX_BUILDS_PER_FEATURE_BRANCH = 2
MAX_DEVELOPMENT_BUILDS = 100

_IMMUTABLE_DEVELOPMENT_TAG = re.compile(
    r"^(?P<prefix>dev-.+)-[0-9a-f]{7,40}-[1-9][0-9]*-[1-9][0-9]*$"
)


def _read(path):
    if not path.exists():
        return {"builds": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("builds"), list):
        raise ValueError("catalogue must contain a builds array")
    if not all(isinstance(entry, dict) for entry in payload["builds"]):
        raise ValueError("every catalogue entry must be an object")
    return payload


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _upsert(payload, entry):
    tag = entry.get("tag") if isinstance(entry, dict) else None
    if not isinstance(tag, str) or not tag:
        raise ValueError("entry must contain a tag")
    builds = [item for item in payload["builds"] if item.get("tag") != tag]
    builds.append(entry)
    builds.sort(key=_build_sort_key, reverse=True)

    branch_counts = {}
    retained = []
    for item in builds:
        branch = _feature_branch_prefix(item)
        count = branch_counts.get(branch, 0)
        if count >= MAX_BUILDS_PER_FEATURE_BRANCH:
            continue
        branch_counts[branch] = count + 1
        retained.append(item)
    payload["builds"] = retained[:MAX_DEVELOPMENT_BUILDS]


def _positive_int(value):
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _build_sort_key(entry):
    return (
        entry.get("created_at") or "",
        _positive_int(entry.get("run_id")),
        _positive_int(entry.get("run_attempt")),
        entry.get("tag") or "",
    )


def _feature_branch_prefix(entry):
    tag = str(entry.get("tag") or "")
    match = _IMMUTABLE_DEVELOPMENT_TAG.fullmatch(tag)
    return match.group("prefix") if match else tag


def _remove_prefix(payload, prefix):
    if not prefix.startswith("dev-"):
        raise ValueError("tag prefix must be a development tag")
    payload["builds"] = [
        entry
        for entry in payload["builds"]
        if not (
            entry.get("tag") == prefix
            or str(entry.get("tag") or "").startswith(prefix + "-")
        )
    ]


def _remove_tag(payload, tag):
    if not _IMMUTABLE_DEVELOPMENT_TAG.fullmatch(tag):
        raise ValueError("tag must be an immutable development tag")
    payload["builds"] = [
        entry for entry in payload["builds"] if entry.get("tag") != tag
    ]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalogue", required=True, type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    upsert = subparsers.add_parser("upsert")
    upsert.add_argument("--entry", required=True, type=Path)
    remove = subparsers.add_parser("remove-prefix")
    remove.add_argument("--tag-prefix", required=True)
    remove_tag = subparsers.add_parser("remove-tag")
    remove_tag.add_argument("--tag", required=True)
    args = parser.parse_args(argv)

    payload = _read(args.catalogue)
    if args.command == "upsert":
        _upsert(payload, json.loads(args.entry.read_text(encoding="utf-8")))
    elif args.command == "remove-prefix":
        _remove_prefix(payload, args.tag_prefix)
    else:
        _remove_tag(payload, args.tag)
    _write(args.catalogue, payload)


if __name__ == "__main__":
    main()
