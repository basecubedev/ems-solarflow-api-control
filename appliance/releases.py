# SPDX-License-Identifier: AGPL-3.0-or-later
"""List every published Admin version, and say which of them this host installs.

Channels are names, never image references. The versions come from the registry
the images are pulled from, which is the only list that answers what exists; a
host configuration may point at a release index instead, for a mirror or an
air-gapped site.

``available()`` never filters. Whether a version may be installed is a property
of the entry (``installable``, ``reason``), because a release candidate that
exists and is refused is something the operator needs to see rather than
something to hide. The refusal itself is enforced in ``protocol.py``, at request
validation, and this is a projection of that policy -- never a second authority.

A mutable name such as ``latest`` is published by the registry too and is dropped
here like any other non-version tag, so no channel can ever resolve to one.
"""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, replace

from appliance.registry_tags import RegistryError, list_tags
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


# Named once so the greyed-out option and the "Prereleases allowed" fact in the
# settings view speak about the same setting in the same words.
PRERELEASE_NOT_ALLOWED_REASON = (
    "Release candidates are not enabled on this appliance "
    "(allow_prerelease in allowed-images.conf)"
)


@dataclass(frozen=True)
class ReleaseTarget:
    tag: str
    channel: str
    prerelease: bool = False
    installable: bool = True
    reason: str = ""

    def to_dict(self):
        return {
            "tag": self.tag,
            "channel": self.channel,
            "prerelease": self.prerelease,
            "installable": self.installable,
            "reason": self.reason,
        }


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
        # An index states its own prerelease flag and may state it wrongly. The
        # tag is the fail-closed half: a document that calls ``v2.0.0-rc1`` a
        # release must not make ``latest_stable`` resolve to a candidate.
        flag = is_prerelease_tag(tag) or bool(prerelease)
        releases.append(ReleaseTarget(tag=tag, channel=CHANNEL_EXACT, prerelease=flag))

    # Every release first, then every candidate -- each newest first. A plain
    # version sort interleaves them, because a candidate sorts directly below
    # the release it precedes, which buries the newest release under the
    # candidates for the next one.
    releases.sort(key=lambda item: (not item.prerelease, version_key(item.tag)), reverse=True)
    return releases


def _with_installability(item, allow_prerelease):
    if not item.prerelease or allow_prerelease:
        return item
    return replace(item, installable=False, reason=PRERELEASE_NOT_ALLOWED_REASON)


class ReleaseCatalogue:
    def __init__(self, config, *, fetcher=None, registry=None):
        self.config = config
        self._fetch = fetcher or self._http_fetch
        self._list_tags = registry or list_tags

    def _http_fetch(self, url):
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
                return response.read(MAX_INDEX_BYTES).decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ReleaseResolutionError(
                "release_index_unreachable", f"release index is unreachable: {exc.__class__.__name__}"
            )

    def _registry_payload(self):
        try:
            return self._list_tags(self.config.images.admin_repository)
        except RegistryError as exc:
            raise ReleaseResolutionError(exc.code, exc.message) from exc

    def available(self):
        url = (self.config.release_index_url or "").strip()
        if url:
            try:
                payload = json.loads(self._fetch(url))
            except ValueError:
                raise ReleaseResolutionError(
                    "release_index_invalid", "release index is not valid JSON"
                ) from None
        else:
            payload = self._registry_payload()
        allowed = bool(self.config.images.allow_prerelease)
        return [_with_installability(item, allowed) for item in parse_release_index(payload)]

    def latest_stable(self):
        for release in self.available():
            if not release.prerelease:
                return ReleaseTarget(tag=release.tag, channel=CHANNEL_LATEST_STABLE)
        raise ReleaseResolutionError(
            "release_channel_unresolved",
            "no stable release is published; pick an exact tag or configure release_index_url",
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
