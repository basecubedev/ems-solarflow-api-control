#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolve the immutable source and target Development builds for the canary.

The replacement journey needs two published builds of the same feature branch:
an older source Admin to start from, and a newer Admin/EMS pair to replace into.
Both sides are addressed by digest, never by a mutable tag, and the two Admin
digests must differ or the run would assert nothing.

Test-hook compatibility is never inferred from tag naming. An entry declaring an
``admin_test_contract`` other than the required version is rejected here, before
anything is pulled; an entry that predates the field stays eligible because the
authority for what an image actually serves is the fail-closed image probe in
``scripts/admin_test_contract.py``. ``--require-declared-contract`` tightens this
to a declaration once every catalogue entry carries one.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from admin_test_contract import load_contract  # noqa: E402
from development_catalogue import build_sort_key, feature_branch_prefix  # noqa: E402

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REQUIRED_FIELDS = (
    "tag",
    "display_name",
    "revision",
    "build_id",
    "admin_image",
    "admin_digest",
    "ems_image",
    "ems_digest",
)
CONTRACT_FIELD = "admin_test_contract"


class BlockedPrecondition(RuntimeError):
    """The catalogue cannot supply a usable source/target pair."""


def _complete(entry):
    if not isinstance(entry, dict):
        return False
    if entry.get("channel") != "development" or entry.get("installable") is not True:
        return False
    return all(isinstance(entry.get(field), str) and entry[field] for field in REQUIRED_FIELDS)


def declared_contract(entry):
    return entry.get(CONTRACT_FIELD)


def eligible_builds(catalogue, version, *, require_declared=False):
    builds = catalogue.get("builds") if isinstance(catalogue, dict) else None
    if not isinstance(builds, list):
        raise BlockedPrecondition("the catalogue does not contain a builds array")
    eligible = []
    for entry in builds:
        if not _complete(entry):
            continue
        declared = declared_contract(entry)
        if declared is None:
            if require_declared:
                continue
        elif declared != version:
            continue
        eligible.append(entry)
    eligible.sort(key=build_sort_key, reverse=True)
    return eligible


def _named(builds, tag, role, version):
    for entry in builds:
        if entry["tag"] == tag:
            return entry
    raise BlockedPrecondition(
        f"no installable Development build named '{tag}' is eligible as the {role} "
        f"of the replacement canary (admin test contract {version})"
    )


def resolve(catalogue, version, *, source_tag=None, target_tag=None, require_declared=False):
    builds = eligible_builds(catalogue, version, require_declared=require_declared)
    if not builds:
        raise BlockedPrecondition(
            f"no installable Development build declares admin test contract {version}"
        )

    target = _named(builds, target_tag, "target", version) if target_tag else builds[0]
    if source_tag:
        source = _named(builds, source_tag, "source", version)
    else:
        branch = feature_branch_prefix(target)
        older = [
            entry
            for entry in builds
            if feature_branch_prefix(entry) == branch
            and build_sort_key(entry) < build_sort_key(target)
        ]
        if not older:
            raise BlockedPrecondition(
                f"no older installable Development build of '{branch}' can act as the "
                f"replacement source for '{target['tag']}'; publish a second build of "
                "that branch or name an explicit source"
            )
        source = older[0]

    for role, entry, fields in (
        ("source", source, ("admin_digest",)),
        ("target", target, ("admin_digest", "ems_digest")),
    ):
        for field in fields:
            if not DIGEST_RE.fullmatch(entry[field]):
                raise BlockedPrecondition(
                    f"{role} {field} is not an immutable digest: {entry[field]}"
                )
    if source["admin_digest"] == target["admin_digest"]:
        raise BlockedPrecondition(
            "source and target resolve to the same Admin digest "
            f"({source['admin_digest']}); the canary would replace an image with itself"
        )
    return source, target


def outputs(source, target):
    lines = []
    for role, entry in (("source", source), ("target", target)):
        for field in REQUIRED_FIELDS:
            lines.append(f"{role}_{field}={entry[field]}")
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalogue", required=True, type=Path)
    parser.add_argument("--source-tag", default="")
    parser.add_argument("--target-tag", default="")
    parser.add_argument("--require-declared-contract", action="store_true")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--contract", type=Path)
    args = parser.parse_args(argv)

    version, _hooks = load_contract(args.contract) if args.contract else load_contract()
    try:
        catalogue = json.loads(args.catalogue.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"could not read the Development catalogue: {exc}", file=sys.stderr)
        return 1
    try:
        source, target = resolve(
            catalogue,
            version,
            source_tag=args.source_tag or None,
            target_tag=args.target_tag or None,
            require_declared=args.require_declared_contract,
        )
    except BlockedPrecondition as exc:
        print(f"replacement canary is blocked: {exc}", file=sys.stderr)
        return 1

    lines = outputs(source, target)
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
