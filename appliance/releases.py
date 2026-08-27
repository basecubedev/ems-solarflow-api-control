# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolve installable Admin versions.

Channels are names, never image references. ``latest_stable`` needs a release
index the host configuration points at; without one the channel is reported as
unresolvable instead of silently falling back to a mutable ``latest`` tag.
"""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from appliance.validation import (
    CHANNEL_CURRENT,
    CHANNEL_EXACT,
    CHANNEL_LATEST_STABLE,
    CHANNEL_PREVIOUS_KNOWN_GOOD,
    ValidationError,
    is_prerelease_tag,
    validate_release_tag,
)
from appliance.version import version_key

DEFAULT_TIMEOUT = 10
MAX_INDEX_BYTES = 512 * 1024


@dataclass(frozen=True)
class ReleaseTarget:
    tag: str
    channel: str
    prerelease: bool = False

    def to_dict(self):
        return {"tag": self.tag, "channel": self.channel, "prerelease": self.prerelease}


class ReleaseResolutionError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message




def parse_release_index(payload):
    """Accept a list of tags or GitHub-style release objects."""

    if isinstance(payload, dict):
        payload = payload.get("releases") or payload.get("tags") or []
    if not isinstance(payload, list):
        raise ReleaseResolutionError("release_index_invalid", "release index is not a list")

    releases = []
    for entry in payload:
        if isinstance(entry, str):
            tag, prerelease, draft = entry, None, False
        elif isinstance(entry, dict):
            tag = entry.get("tag_name") or entry.get("tag") or entry.get("name") or ""
            prerelease = entry.get("prerelease")
            draft = bool(entry.get("draft"))
        else:
            continue
        if draft:
            continue
        try:
            tag = validate_release_tag(tag)
        except ValidationError:
            continue
        flag = is_prerelease_tag(tag) if prerelease is None else bool(prerelease)
        releases.append(ReleaseTarget(tag=tag, channel=CHANNEL_EXACT, prerelease=flag))

    releases.sort(key=lambda item: version_key(item.tag), reverse=True)
    return releases


class ReleaseCatalogue:
    def __init__(self, config, *, fetcher=None):
        self.config = config
        self._fetch = fetcher or self._http_fetch

    def _http_fetch(self, url):
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
                return response.read(MAX_INDEX_BYTES).decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ReleaseResolutionError(
                "release_index_unreachable", f"release index is unreachable: {exc.__class__.__name__}"
            )

    def available(self):
        url = (self.config.release_index_url or "").strip()
        if not url:
            return []
        try:
            payload = json.loads(self._fetch(url))
        except ValueError:
            raise ReleaseResolutionError("release_index_invalid", "release index is not valid JSON")
        releases = parse_release_index(payload)
        if not self.config.images.allow_prerelease:
            releases = [item for item in releases if not item.prerelease]
        return releases

    def latest_stable(self):
        for release in self.available():
            if not release.prerelease:
                return ReleaseTarget(tag=release.tag, channel=CHANNEL_LATEST_STABLE)
        raise ReleaseResolutionError(
            "release_channel_unresolved",
            "no stable release is available; configure release_index_url or pick an exact tag",
        )


def resolve_channel(channel, *, catalogue, current_tag, previous_known_good, requested_tag=None):
    """Turn a channel name into one concrete, validated release tag."""

    if channel == CHANNEL_EXACT:
        if not requested_tag:
            raise ReleaseResolutionError("release_tag_required", "an exact tag must be provided")
        return ReleaseTarget(tag=validate_release_tag(requested_tag), channel=CHANNEL_EXACT)

    if channel == CHANNEL_CURRENT:
        if not current_tag:
            raise ReleaseResolutionError(
                "release_channel_unresolved", "no Admin version is currently installed"
            )
        return ReleaseTarget(tag=validate_release_tag(current_tag), channel=CHANNEL_CURRENT)

    if channel == CHANNEL_PREVIOUS_KNOWN_GOOD:
        if not previous_known_good:
            raise ReleaseResolutionError(
                "release_channel_unresolved", "no previous known-good Admin has been recorded"
            )
        tag = previous_known_good.get("admin_version") or ""
        return ReleaseTarget(
            tag=validate_release_tag(tag), channel=CHANNEL_PREVIOUS_KNOWN_GOOD
        )

    if channel == CHANNEL_LATEST_STABLE:
        return catalogue.latest_stable()

    raise ReleaseResolutionError("invalid_release_channel", f"{channel!r} is not a release channel")
