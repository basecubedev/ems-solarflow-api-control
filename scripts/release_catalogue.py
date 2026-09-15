#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build and verify the public release catalogue.

``build`` asks the GitHub API once, in CI with a token, for everything the
Admin used to ask it for on every visit to the upgrade page: the release list
and, per release, whether the setup resources are present in its tree. The
answer is written as one JSON file that the Admin reads over the content CDN.
The resource rule is imported from ``admin.releases`` so the two readers can
never disagree about what "present" means.

``verify`` reads a published catalogue back through the Admin's own loader and
requires a named tag to be in it, so a publish is only reported as done once
the file the installations will read actually says so.
"""

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from admin.release_catalogue import CATALOGUE_SCHEMA, load_release_catalogue  # noqa: E402
from admin.releases import (  # noqa: E402
    GITHUB_RELEASE_PAGES,
    GITHUB_RELEASES_PER_PAGE,
    REPO,
    _is_release_candidate,
    _version,
    resources_present,
)

API = "https://api.github.com"


def _request(url, token):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ems-solarflow-release-catalogue",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def _get_json(url, token, opener):
    with opener(_request(url, token), timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def list_releases(token, opener=urllib.request.urlopen):
    entries = []
    for page in range(1, GITHUB_RELEASE_PAGES + 1):
        batch = _get_json(
            f"{API}/repos/{REPO}/releases?per_page={GITHUB_RELEASES_PER_PAGE}&page={page}",
            token,
            opener,
        )
        if not isinstance(batch, list):
            raise ValueError("GitHub returned an invalid release list")
        entries.extend(batch)
        if len(batch) < GITHUB_RELEASES_PER_PAGE:
            break
    return entries


def tree_has_resources(tag, token, opener=urllib.request.urlopen):
    """Whether the ref's tree carries the setup resources.

    A ref GitHub does not know (404) has no tree and therefore no resources;
    that is an answer, recorded as such. Any other failure is not an answer,
    and the build aborts rather than publish a guess for a day.
    """

    try:
        payload = _get_json(f"{API}/repos/{REPO}/git/trees/{tag}?recursive=1", token, opener)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise
    if not isinstance(payload, dict) or not isinstance(payload.get("tree"), list):
        raise ValueError(f"GitHub returned an invalid tree for {tag}")
    return resources_present(
        item.get("path")
        for item in payload["tree"]
        if isinstance(item, dict) and item.get("type") == "blob"
    )


def build_catalogue(token, opener=urllib.request.urlopen, now=None):
    releases = []
    seen = set()
    for entry in list_releases(token, opener):
        if not isinstance(entry, dict) or entry.get("draft"):
            continue
        tag = str(entry.get("tag_name") or "").strip()
        if not tag or not _version(tag) or tag in seen:
            continue
        seen.add(tag)
        prerelease = bool(entry.get("prerelease"))
        releases.append(
            {
                "tag": tag,
                "name": str(entry.get("name") or tag),
                "channel": "rc" if _is_release_candidate(tag, prerelease) else "stable",
                "prerelease": prerelease,
                "published_at": entry.get("published_at"),
                "resources_present": tree_has_resources(tag, token, opener),
            }
        )
    releases.sort(key=lambda item: _version(item["tag"]), reverse=True)
    generated_at = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema": CATALOGUE_SCHEMA,
        "generated_at": generated_at,
        "main_resources_present": tree_has_resources("main", token, opener),
        "releases": releases,
    }


def write_catalogue(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def _build(args):
    token = os.environ.get(args.token_env, "").strip()
    payload = build_catalogue(token)
    write_catalogue(args.output, payload)
    if load_release_catalogue(Path(args.output)) is None:
        raise SystemExit("the written catalogue does not pass the Admin's own loader")
    print(f"{len(payload['releases'])} releases -> {args.output}")
    return 0


def _verify(args):
    deadline = time.monotonic() + args.timeout
    attempt = 0
    while True:
        attempt += 1
        separator = "&" if "?" in args.source else "?"
        with tempfile.TemporaryDirectory(prefix="release-catalogue-verify-") as directory:
            releases = load_release_catalogue(
                f"{args.source}{separator}catalogue_attempt={attempt}",
                cache_path=Path(directory) / "cache.json",
            )
        tags = {item["tag"] for item in (releases or {}).get("releases", [])}
        if releases is not None and args.expect_tag in tags:
            print(f"catalogue lists {args.expect_tag} ({len(tags)} releases)")
            return 0
        if time.monotonic() >= deadline:
            print(
                f"catalogue does not list {args.expect_tag} after {attempt} attempts",
                file=sys.stderr,
            )
            return 1
        time.sleep(min(30, 5 * attempt))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="write the catalogue from the GitHub API")
    build.add_argument("--output", required=True)
    build.add_argument("--token-env", default="GITHUB_TOKEN")
    build.set_defaults(handler=_build)
    verify = subparsers.add_parser("verify", help="require a tag in a published catalogue")
    verify.add_argument("--source", required=True)
    verify.add_argument("--expect-tag", required=True)
    verify.add_argument("--timeout", type=float, default=600.0)
    verify.set_defaults(handler=_verify)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
