#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Delete GHCR container versions of a feature-tag prefix beyond the newest N.

Development builds accumulate one image version per publish. This keeps only the
newest ``--keep`` builds of a feature branch (matched by its ``dev-<ref>-<hash>``
tag prefix) and deletes the older ones. Versions that also carry a tag outside
the prefix (e.g. a release tag) or a protected tag are never deleted, and
neither is a version one of whose tags another package still keeps: the EMS
image, the Admin image and their source of one build go together.

With ``--digest`` it deletes instead the versions a failed run pushed, named by
their digests, as long as they carry no tag but the run's own (``--own-tag``).
"""

import argparse
import json
import subprocess
import sys


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _under_prefix(tag, prefix):
    return tag == prefix or tag.startswith(prefix + "-")


def select_deletions(versions, prefix, keep, protect_tags=()):
    """Return the ids of prefix versions to delete, keeping the newest ``keep``.

    Pure decision logic: sort matching versions newest-first, keep the newest
    ``keep``, and from the remainder drop any version carrying a protected tag or
    a tag outside the prefix (those are shared and must never be deleted).
    """
    protect = set(protect_tags)
    matching = [
        version
        for version in versions
        if any(_under_prefix(tag, prefix) for tag in (version.get("tags") or []))
    ]
    matching.sort(
        key=lambda v: (v.get("created_at") or "", _as_int(v.get("id"))),
        reverse=True,
    )
    deletions = []
    for version in matching[max(keep, 0):]:
        tags = version.get("tags") or []
        if any(tag in protect for tag in tags):
            continue
        if any(not _under_prefix(tag, prefix) for tag in tags):
            continue
        deletions.append(version.get("id"))
    return deletions


def keep_builds_together(versions, deletions):
    """Drop from each package's deletions a version sharing a tag with a version another package keeps."""

    kept = {
        package: {
            tag
            for version in package_versions
            if version.get("id") not in set(deletions[package])
            for tag in (version.get("tags") or [])
        }
        for package, package_versions in versions.items()
    }
    together = {}
    for package, ids in deletions.items():
        elsewhere = set().union(*(tags for other, tags in kept.items() if other != package))
        by_id = {version.get("id"): version for version in versions[package]}
        together[package] = [
            version_id for version_id in ids
            if not set(by_id[version_id].get("tags") or []) & elsewhere
        ]
    return together


def select_digest_deletions(versions, digests, prefix, own_tags=()):
    """The ids of the versions named by ``digests`` that carry no tag but the run's own.

    A version that already carries one of the branch's shared tags is left
    alone, since deleting it would take that tag from the branch too; one
    carrying a tag outside the prefix is refused.
    """

    wanted, own = set(digests), set(own_tags)
    deletions = []
    for version in versions:
        if version.get("name") not in wanted:
            continue
        tags = version.get("tags") or []
        foreign = [tag for tag in tags if not _under_prefix(tag, prefix)]
        if foreign:
            raise SystemExit(f"refusing to delete {version.get('name')}: it carries {', '.join(foreign)}")
        if set(tags) - own:
            continue
        deletions.append(version.get("id"))
    return deletions


def _owner_path(owner, owner_type):
    if owner_type == "user":
        return f"/users/{owner}"
    if owner_type == "organization":
        return f"/orgs/{owner}"
    raise SystemExit(f"Unknown package owner type: {owner_type}")


def _gh_json(args):
    result = subprocess.run(
        ["gh", "api", *args], text=True, capture_output=True, check=False
    )
    return result


def _list_versions(owner_path, package):
    endpoint = f"{owner_path}/packages/container/{package}/versions"
    result = _gh_json(
        [
            "--paginate",
            endpoint,
            "--jq",
            ".[] | {id: .id, name: .name, created_at: .created_at, "
            "tags: .metadata.container.tags}",
        ]
    )
    if result.returncode != 0:
        if "HTTP 404" in result.stderr:
            print(f"Package {package} is absent (HTTP 404); nothing to prune.")
            return []
        sys.stderr.write(result.stderr)
        raise SystemExit(f"Failed to list versions for {package}")
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def _delete_version(owner_path, package, version_id):
    endpoint = f"{owner_path}/packages/container/{package}/versions/{version_id}"
    result = _gh_json(["--method", "DELETE", endpoint])
    if result.returncode != 0 and "last tagged version" in result.stderr.lower():
        print(f"  keeping version {version_id}: GHCR keeps the last tagged version of {package}")
        return
    if result.returncode != 0 and "HTTP 404" not in result.stderr:
        sys.stderr.write(result.stderr)
        raise SystemExit(f"Failed to delete version {version_id} from {package}")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--owner-type", default="user", choices=["user", "organization"])
    parser.add_argument("--package", required=True, action="append", dest="packages")
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--keep", type=int, default=2)
    parser.add_argument("--protect-tag", action="append", default=[], dest="protect_tags")
    parser.add_argument("--digest", action="append", default=[], dest="digests")
    parser.add_argument("--own-tag", action="append", default=[], dest="own_tags")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not args.prefix.startswith("dev-"):
        raise SystemExit("refusing to prune a non-development tag prefix")

    owner_path = _owner_path(args.owner, args.owner_type)
    versions = {package: _list_versions(owner_path, package) for package in args.packages}
    if args.digests:
        deletions = {package: select_digest_deletions(found, args.digests, args.prefix, args.own_tags)
                     for package, found in versions.items()}
    else:
        deletions = keep_builds_together(versions, {
            package: select_deletions(found, args.prefix, args.keep, args.protect_tags)
            for package, found in versions.items()
        })
    for package in args.packages:
        print(f"{package}: deleting {len(deletions[package])} '{args.prefix}' version(s)")
        for version_id in deletions[package]:
            print(f"  {'would delete' if args.dry_run else 'deleting'} version {version_id}")
            if not args.dry_run:
                _delete_version(owner_path, package, version_id)


if __name__ == "__main__":
    main()
