# SPDX-License-Identifier: AGPL-3.0-or-later
"""The tags a container repository publishes, read from the registry itself.

The registry is the authority on which Admin versions an appliance can install.
A git tag is not an image -- this project tagged twenty releases before it built
an Admin image for the first one -- and a hand-written index is a third list
that can disagree with both. So the list an operator picks from is asked of the
place the image is pulled from anyway.

Nothing here is trusted: a tag is a candidate until ``validate_release_tag``
accepts it, and the install path still verifies the pulled image's OCI labels.

The lookup is read through ``release_fetch``'s transport: one deadline over the
whole lookup, whose timer ends every connection it opened, and redirects
followed to https only, the registry's token to its own origin alone.
"""

import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from appliance import release_fetch

DEFAULT_TIMEOUT = 10
MAX_TAGS_BYTES = 512 * 1024
MAX_PAGES = 10
PAGE_SIZE = 100
DEFAULT_REGISTRY = "registry-1.docker.io"

_CHALLENGE_PARAMETER = re.compile(r'(\w+)="([^"]*)"')
_NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?')


class RegistryError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def split_repository(repository):
    """The registry host and the repository path a reference names."""

    text = str(repository or "").strip().strip("/")
    if not text:
        raise RegistryError("release_registry_invalid", "no image repository is configured")
    head, _, rest = text.partition("/")
    if rest and ("." in head or ":" in head or head == "localhost"):
        return head, rest
    return DEFAULT_REGISTRY, text if rest else f"library/{text}"


def _open(opener, url, headers, deadline):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise RegistryError(
            "release_registry_unreachable", "only an https registry endpoint is read"
        )
    request = urllib.request.Request(url, headers=headers)
    try:
        return opener(request, timeout=deadline.remaining(), deadline=deadline)
    except urllib.error.HTTPError:
        raise
    except release_fetch._InsecureRedirect:
        raise RegistryError(
            "release_registry_unreachable", "the registry redirected to a non-https location"
        ) from None
    except release_fetch._UnheldConnection:
        raise RegistryError(
            "release_registry_unreachable",
            "the registry answered over a connection its deadline could not end",
        ) from None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        deadline.remaining()
        raise RegistryError(
            "release_registry_unreachable",
            f"the registry is unreachable: {exc.__class__.__name__}",
        ) from exc


def _read(response, deadline):
    label = "the registry answer"
    release_fetch.HttpsFetcher._still_https(response, label)
    payload = bytearray()
    for block in release_fetch.HttpsFetcher._blocks(response, deadline, label):
        payload += block
        if len(payload) > MAX_TAGS_BYTES:
            raise RegistryError(
                "release_registry_invalid",
                f"the registry sends more than the {MAX_TAGS_BYTES} bytes this appliance reads",
            )
    try:
        return json.loads(bytes(payload).decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise RegistryError("release_registry_invalid", "the registry answer is not JSON") from exc


def _refused(exc):
    """A transport refusal under the registry's own code."""

    if exc.code == "release_download_timed_out":
        return RegistryError("release_registry_unreachable", "the registry did not answer in time")
    return RegistryError("release_registry_unreachable", exc.message)


def _token(opener, challenge, deadline):
    """An anonymous pull token, from the realm the registry's challenge names.

    The realm is a URL the registry chose, so it is held to https like every
    other endpoint here. No credentials are sent to it; there are none to send.
    """

    parameters = dict(_CHALLENGE_PARAMETER.findall(challenge or ""))
    realm = parameters.pop("realm", "")
    if not realm:
        raise RegistryError(
            "release_registry_unreachable", "the registry challenge names no token realm"
        )
    query = urllib.parse.urlencode(
        {key: value for key, value in parameters.items() if key in ("service", "scope")}
    )
    payload, _ = _page(opener, f"{realm}?{query}" if query else realm, {}, deadline)
    token = ""
    if isinstance(payload, dict):
        token = payload.get("token") or payload.get("access_token") or ""
    if not token:
        raise RegistryError("release_registry_invalid", "the token endpoint returned no token")
    return str(token)


def _page(opener, url, headers, deadline):
    try:
        with _open(opener, url, headers, deadline) as response:
            return _read(response, deadline), response.headers.get("Link", "")
    except urllib.error.HTTPError as exc:
        raise RegistryError(
            "release_registry_unreachable", f"the registry answered HTTP {exc.code}"
        ) from exc


def list_tags(repository, *, opener=None, timeout=DEFAULT_TIMEOUT, clock=time.monotonic):
    """Every tag the repository publishes, in the order the registry lists them.

    A challenge, its token and every page are read under one deadline of
    ``timeout`` seconds, a body kept alive inside one read included.
    """

    opener = opener or release_fetch._open_watched
    host, path = split_repository(repository)
    base = f"https://{host}"
    try:
        with release_fetch._Deadline(timeout, clock, "the registry", threading.Timer) as deadline:
            return _lookup(opener, base, f"{base}/v2/{path}/tags/list?n={PAGE_SIZE}", deadline)
    except release_fetch.FetchError as exc:
        raise _refused(exc) from None


def _lookup(opener, base, url, deadline):
    headers = {"Accept": "application/json"}
    try:
        with _open(opener, url, headers, deadline) as response:
            payload, link = _read(response, deadline), response.headers.get("Link", "")
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise RegistryError(
                "release_registry_unreachable", f"the registry answered HTTP {exc.code}"
            ) from exc
        token = _token(opener, exc.headers.get("WWW-Authenticate", ""), deadline)
        headers["Authorization"] = f"Bearer {token}"
        payload, link = _page(opener, url, headers, deadline)

    tags = []
    pages = 1
    while True:
        listed = payload.get("tags") if isinstance(payload, dict) else None
        tags.extend(item for item in (listed or []) if isinstance(item, str))
        match = _NEXT_LINK.search(link or "")
        if not match or pages >= MAX_PAGES:
            break
        following = urllib.parse.urljoin(base, match.group(1))
        if not release_fetch._same_origin(following, base):
            raise RegistryError(
                "release_registry_invalid", "the registry names a next page on another host"
            )
        payload, link = _page(opener, following, headers, deadline)
        pages += 1
    return tags
