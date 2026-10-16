#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolve the stable releases the Admin replacement canary upgrades between.

The canary starts on the Admin of the previous stable release and replaces it
with the Admin/EMS pair of the latest stable release: the upgrade an
installation takes. Which releases are stable, and in which order, is the
Admin's own release listing (``ReleaseManager.list_releases``) over the release
catalogue built from the GitHub Releases, so "stable" has one definition.

Each image is then read from its registry manifest. The tag resolves to the
content digest the run is pinned to, and the build identity comes from the
image's labels, held to the pairing rule the Admin applies to a System Build
(``validate_system_build_pair``). Whether both Admins serve the shared page
object hooks is not decided here: ``scripts/admin_test_contract.py`` probes the
pulled images before the first container starts.
"""

import argparse
import hashlib
import json
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from admin.admin_update import ADMIN_IMAGE_REPO, EMS_IMAGE_REPO  # noqa: E402
from admin.image_identity import parse_labels  # noqa: E402
from admin.release_catalogue import release_catalogue_source  # noqa: E402
from admin.releases import DEVELOPMENT_CATALOGUE_UNREADABLE, ReleaseManager  # noqa: E402
from admin.system_build import (  # noqa: E402
    SystemBuildError,
    classify_channel,
    validate_system_build_pair,
)

OUTPUT_FIELDS = (
    "tag",
    "revision",
    "build_id",
    "admin_image",
    "admin_digest",
    "ems_image",
    "ems_digest",
)
INDEX_MEDIA_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
MANIFEST_MEDIA_TYPES = INDEX_MEDIA_TYPES + (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
PLATFORM = ("linux", "amd64")
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
TIMEOUT_SECONDS = 30
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CHALLENGE_PARAMETER = re.compile(r'(\w+)="([^"]*)"')


class BlockedPrecondition(RuntimeError):
    """No stable source/target pair can be resolved for the canary."""


def admin_release_listing(release_source=None, *, urlopen=None):
    """The release listing a fresh Admin Setup shows, read without a running build."""

    with tempfile.TemporaryDirectory(prefix="canary-releases-") as scratch:
        directory = Path(scratch)
        source = release_source or release_catalogue_source(
            cache_path=directory / "release-catalogue-cache.json"
        )
        manager = ReleaseManager(
            data_dir=directory / "admin", release_source=source, urlopen=urlopen
        )
        return manager.list_releases(for_upgrade=False)


def stable_releases(listing):
    """The stable releases the listing offers, newest first, in its own order."""

    releases = listing.get("releases") if isinstance(listing, dict) else None
    if not isinstance(releases, list):
        raise BlockedPrecondition("the Admin release listing has no releases array")
    return [
        item["tag"]
        for item in releases
        if isinstance(item, dict)
        and item.get("channel") == "stable"
        and item.get("selectable") is True
    ]


def choose_pair(listing, *, source_tag=None, target_tag=None):
    """The source and target tags: by default the previous and the latest stable.

    A listing that warns is refused whatever it names: a release page it could
    not read may have held the latest stable release, and the pair it gives
    would then be an older one. Only the warning about the development
    catalogue concerns nothing the canary reads.
    """

    stable = stable_releases(listing)
    target = target_tag or listing.get("latest_stable")
    if not target:
        warnings = "; ".join(listing.get("warnings") or []) or "no warning"
        raise BlockedPrecondition(
            f"the Admin release listing names no latest stable release ({warnings})"
        )
    gaps = [
        warning
        for warning in listing.get("warnings") or []
        if warning != DEVELOPMENT_CATALOGUE_UNREADABLE
    ]
    if gaps:
        raise BlockedPrecondition(
            f"the Admin release listing may be missing releases ({'; '.join(gaps)})"
        )
    if target not in stable:
        raise BlockedPrecondition(f"'{target}' is not a stable release the Admin offers")
    older = stable[stable.index(target) + 1 :]
    if source_tag:
        if source_tag not in older:
            raise BlockedPrecondition(
                f"'{source_tag}' is not a stable release the Admin offers below '{target}'"
            )
        return source_tag, target
    if not older:
        raise BlockedPrecondition(
            f"no stable release below '{target}' can act as the replacement source"
        )
    return older[0], target


class Registry:
    """Anonymous reads of manifests and config blobs, each checked against its digest."""

    def __init__(self, opener=None, timeout=TIMEOUT_SECONDS):
        self._open = opener or urllib.request.urlopen
        self._timeout = timeout
        self._tokens = {}

    def image(self, repository, tag):
        """The tag's content digest and the labels of its linux/amd64 image."""

        host, _, path = repository.partition("/")
        accept = ", ".join(MANIFEST_MEDIA_TYPES)
        digest, document = self._fetch(host, path, f"manifests/{tag}", accept)
        manifest = _json(document, f"{repository}:{tag}")
        if manifest.get("mediaType") in INDEX_MEDIA_TYPES or "manifests" in manifest:
            platform = _platform_digest(manifest, f"{repository}:{tag}")
            _, document = self._fetch(host, path, f"manifests/{platform}", accept, platform)
            manifest = _json(document, f"{repository}@{platform}")
        config = (manifest.get("config") or {}).get("digest")
        if not isinstance(config, str) or not DIGEST_RE.fullmatch(config):
            raise BlockedPrecondition(f"{repository}:{tag} names no image configuration")
        _, document = self._fetch(host, path, f"blobs/{config}", "*/*", config)
        labels = (_json(document, f"{repository}:{tag}").get("config") or {}).get("Labels")
        return digest, labels if isinstance(labels, dict) else {}

    def _fetch(self, host, path, suffix, accept, expected=None):
        url = f"https://{host}/v2/{path}/{suffix}"
        try:
            try:
                body = self._read(url, accept, self._tokens.get((host, path)))
            except urllib.error.HTTPError as exc:
                if exc.code != 401 or (host, path) in self._tokens:
                    raise
                self._tokens[(host, path)] = self._token(exc.headers.get("WWW-Authenticate", ""))
                body = self._read(url, accept, self._tokens[(host, path)])
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise BlockedPrecondition(f"{url} could not be read: {exc}") from exc
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if expected and digest != expected:
            raise BlockedPrecondition(f"{url} does not match its digest {expected}")
        return digest, body

    def _read(self, url, accept, token):
        request = urllib.request.Request(
            url, headers={"Accept": accept, "User-Agent": "ems-solarflow-canary"}
        )
        if token:
            request.add_unredirected_header("Authorization", f"Bearer {token}")
        with self._open(request, timeout=self._timeout) as response:
            body = response.read(MAX_DOCUMENT_BYTES + 1)
        if len(body) > MAX_DOCUMENT_BYTES:
            raise ValueError(f"the answer is larger than {MAX_DOCUMENT_BYTES} bytes")
        return body

    def _token(self, challenge):
        scheme, _, parameters = str(challenge).partition(" ")
        values = dict(_CHALLENGE_PARAMETER.findall(parameters))
        realm = values.pop("realm", "")
        if scheme.lower() != "bearer" or urllib.parse.urlsplit(realm).scheme != "https":
            raise ValueError("the registry asks for credentials the canary does not hold")
        query = urllib.parse.urlencode(
            {key: value for key, value in values.items() if key in ("service", "scope")}
        )
        payload = json.loads(self._read(f"{realm}?{query}", "application/json", None))
        if not isinstance(payload, dict):
            raise ValueError("the registry token endpoint returned no token")
        token = payload.get("token") or payload.get("access_token")
        if not token:
            raise ValueError("the registry token endpoint returned no token")
        return str(token)


def _json(document, name):
    try:
        payload = json.loads(document)
    except ValueError as exc:
        raise BlockedPrecondition(f"{name} is not a JSON document") from exc
    if not isinstance(payload, dict):
        raise BlockedPrecondition(f"{name} is not a JSON object")
    return payload


def _platform_digest(index, name):
    for entry in index.get("manifests") or []:
        platform = entry.get("platform") if isinstance(entry, dict) else None
        if not isinstance(platform, dict):
            continue
        if (platform.get("os"), platform.get("architecture")) == PLATFORM:
            digest = entry.get("digest")
            if isinstance(digest, str) and DIGEST_RE.fullmatch(digest):
                return digest
    raise BlockedPrecondition(f"{name} publishes no {'/'.join(PLATFORM)} image")


def resolve_release(registry, tag):
    """The digest-pinned Admin/EMS pair of one release, as its labels name it."""

    identities = []
    for repository in (ADMIN_IMAGE_REPO, EMS_IMAGE_REPO):
        digest, labels = registry.image(repository, tag)
        identities.append(parse_labels(labels, image_ref=f"{repository}:{tag}", digest=digest))
    admin, ems = identities
    try:
        validate_system_build_pair(tag, classify_channel(tag), admin, ems)
    except SystemBuildError as exc:
        raise BlockedPrecondition(
            f"{tag} is not one published System Build: {exc.message}"
        ) from exc
    return {
        "tag": tag,
        "revision": admin.revision,
        "build_id": admin.build_id,
        "admin_image": f"{ADMIN_IMAGE_REPO}:{tag}",
        "admin_digest": admin.digest,
        "ems_image": f"{EMS_IMAGE_REPO}:{tag}",
        "ems_digest": ems.digest,
    }


def resolve(listing, registry, *, source_tag=None, target_tag=None):
    source_tag, target_tag = choose_pair(listing, source_tag=source_tag, target_tag=target_tag)
    source = resolve_release(registry, source_tag)
    target = resolve_release(registry, target_tag)
    if source["admin_digest"] == target["admin_digest"]:
        raise BlockedPrecondition(
            "source and target resolve to the same Admin digest "
            f"({source['admin_digest']}); the canary would replace an image with itself"
        )
    return source, target


def outputs(source, target):
    lines = []
    for role, entry in (("source", source), ("target", target)):
        for field in OUTPUT_FIELDS:
            lines.append(f"{role}_{field}={entry[field]}")
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-tag", default="")
    parser.add_argument("--target-tag", default="")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)

    try:
        source, target = resolve(
            admin_release_listing(),
            Registry(),
            source_tag=args.source_tag or None,
            target_tag=args.target_tag or None,
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
