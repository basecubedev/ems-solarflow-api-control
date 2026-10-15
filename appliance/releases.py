# SPDX-License-Identifier: AGPL-3.0-or-later
"""List every published Admin version, and say which of them this host installs.

Channels are names, never image references. The versions come from the registry
the images are pulled from, which is the only list that answers what exists; a
host configuration may point at a release index instead, for a mirror or an
air-gapped site.

``available()`` never filters. Whether a version may be installed is a property
of the entry (``installable``, ``reason``), because a release candidate that
exists and is refused is something the operator needs to see rather than
something to hide. A candidate tag is refused in ``protocol.py``, at request
validation; a tag only the index calls a prerelease is refused when the exact
channel resolves, from the same entry the list shows, so the two never disagree.

A mutable name such as ``latest`` is published by the registry too and is dropped
here like any other non-version tag, so no channel can ever resolve to one.
"""

import json
from dataclasses import dataclass, replace

from appliance.registry_tags import RegistryError, list_tags
from appliance.release_fetch import FetchError, HttpsFetcher
from appliance.validation import (
    CHANNEL_CURRENT,
    CHANNEL_EXACT,
    CHANNEL_LATEST_STABLE,
    CHANNEL_PREVIOUS_KNOWN_GOOD,
    ValidationError,
    is_prerelease_tag,
    validate_release_tag,
)
from appliance.version import (
    TRACK_STABLE,
    TRACK_UNSTABLE,
    newest_stable,
    release_track,
    version_key,
)

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

    @property
    def track(self):
        """``release_track`` of the tag; an index's own prerelease flag may only demote."""

        track = release_track(self.tag)
        return TRACK_UNSTABLE if self.prerelease and track == TRACK_STABLE else track

    def to_dict(self):
        return {
            "tag": self.tag,
            "channel": self.channel,
            "prerelease": self.prerelease,
            "track": self.track,
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


# How a refused read of the index is reported, by the transport's code. A body
# over the cap used to be cut and then fail as JSON, so it stays an invalid index.
_INDEX_FETCH_CODES = {
    "release_source_insecure": "release_index_insecure",
    "release_download_too_large": "release_index_invalid",
}


class ReleaseCatalogue:
    def __init__(self, config, *, fetcher=None, registry=None):
        self.config = config
        self._fetch = fetcher or self._https_fetch
        self._list_tags = registry or list_tags

    def _https_fetch(self, url):
        """The index, over https only, as the Manager's own index is read.

        Over plain http whoever is on the path chooses the tag *Latest stable*
        installs, an old release included, and ``file:`` would read the agent's
        own disk; a redirect to http is refused as well. The call this serves
        waits 30 s, so the read's deadline and each socket wait are
        ``DEFAULT_TIMEOUT``.
        """

        transport = HttpsFetcher(timeout=DEFAULT_TIMEOUT, document_deadline=DEFAULT_TIMEOUT)
        try:
            payload = transport.read(url, label="the release index", max_bytes=MAX_INDEX_BYTES)
        except FetchError as exc:
            code = _INDEX_FETCH_CODES.get(exc.code, "release_index_unreachable")
            raise ReleaseResolutionError(code, exc.message) from None
        return payload.decode("utf-8", errors="replace")

    def _registry_payload(self):
        try:
            return self._list_tags(self.config.images.admin_repository)
        except RegistryError as exc:
            raise ReleaseResolutionError(exc.code, exc.message) from exc

    @property
    def index_url(self):
        """The configured release index, or "" where the registry is the list."""

        return (self.config.release_index_url or "").strip()

    def available(self):
        url = self.index_url
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
        release = latest_stable_of(self.available())
        if release is not None:
            return ReleaseTarget(tag=release.tag, channel=CHANNEL_LATEST_STABLE)
        raise ReleaseResolutionError(
            "release_channel_unresolved",
            "no stable release is published; pick an exact tag or configure release_index_url",
        )


def latest_stable_of(targets):
    """The ReleaseTarget the Latest stable channel resolves to, or None."""

    return newest_stable(
        targets, version_of=lambda item: item.tag, stable=lambda item: item.track == TRACK_STABLE
    )


def _refuse_what_the_list_refuses(catalogue, tag):
    """An exact tag the version list shows as refused is refused with its reason.

    ``protocol.py`` refuses a candidate tag at request validation. A release
    index may also call a plain tag a prerelease, and the list greys it out;
    this is where the agent agrees. The registry lists tags alone, so it can
    refuse nothing the tag rule has not, and is not asked. An index that cannot
    be read refuses nothing here: the tag rule has already run, and an exact
    tag is the way in when it is unreachable.
    """

    if not catalogue.index_url:
        return
    try:
        listed = catalogue.available()
    except ReleaseResolutionError:
        return
    for item in listed:
        if item.tag == tag and not item.installable:
            raise ReleaseResolutionError(
                "prerelease_not_allowed", f"{tag} is listed as a prerelease: {item.reason}"
            )


def resolve_channel(channel, *, catalogue, current_tag, previous_known_good, requested_tag=None):
    """Turn a channel name into one concrete, validated release tag."""

    if channel == CHANNEL_EXACT:
        if not requested_tag:
            raise ReleaseResolutionError("release_tag_required", "an exact tag must be provided")
        tag = validate_release_tag(requested_tag)
        _refuse_what_the_list_refuses(catalogue, tag)
        return ReleaseTarget(tag=tag, channel=CHANNEL_EXACT)

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
