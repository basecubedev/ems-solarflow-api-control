# SPDX-License-Identifier: AGPL-3.0-or-later
"""The tags a container repository publishes, read from the registry itself.

The registry is the authority on which Admin versions an appliance can install.
A git tag is not an image -- this project tagged twenty releases before it built
an Admin image for the first one -- and a hand-written index is a third list
that can disagree with both. So the list an operator picks from is asked of the
place the image is pulled from anyway.

Nothing here is trusted: a tag is a candidate until ``validate_release_tag``
accepts it, and the install path still verifies the pulled image's OCI labels.
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

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


class _Budget:
    """One wall-clock budget for the whole lookup.

    A per-request timeout bounds nothing here: a challenge, its token exchange
    and ten pages are eleven requests, and the operator's request would sit
    behind all of them long past the agent's own operation timeout.
    """

    def __init__(self, seconds, clock):
        self._clock = clock
        self._deadline = clock() + max(float(seconds), 0.0)

    def remaining(self):
        left = self._deadline - self._clock()
        if left <= 0:
            raise RegistryError(
                "release_registry_unreachable", "the registry did not answer in time"
            )
        return left


def split_repository(repository):
    """The registry host and the repository path a reference names."""

    text = str(repository or "").strip().strip("/")
    if not text:
        raise RegistryError("release_registry_invalid", "no image repository is configured")
    head, _, rest = text.partition("/")
    if rest and ("." in head or ":" in head or head == "localhost"):
        return head, rest
    return DEFAULT_REGISTRY, text if rest else f"library/{text}"


def _open(opener, url, headers, budget):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise RegistryError(
            "release_registry_unreachable", "only an https registry endpoint is read"
        )
    request = urllib.request.Request(url, headers=headers)
    try:
        return opener(request, timeout=budget.remaining())
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RegistryError(
            "release_registry_unreachable",
            f"the registry is unreachable: {exc.__class__.__name__}",
        ) from exc


def _read(response):
    payload = response.read(MAX_TAGS_BYTES + 1)
    if len(payload) > MAX_TAGS_BYTES:
        raise RegistryError(
            "release_registry_invalid",
            f"the registry sends more than the {MAX_TAGS_BYTES} bytes this appliance reads",
        )
    try:
        return json.loads(payload.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise RegistryError("release_registry_invalid", "the registry answer is not JSON") from exc


def _token(opener, challenge, budget):
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
    payload, _ = _page(opener, f"{realm}?{query}" if query else realm, {}, budget)
    token = ""
    if isinstance(payload, dict):
        token = payload.get("token") or payload.get("access_token") or ""
    if not token:
        raise RegistryError("release_registry_invalid", "the token endpoint returned no token")
    return str(token)


def _page(opener, url, headers, budget):
    try:
        with _open(opener, url, headers, budget) as response:
            return _read(response), response.headers.get("Link", "")
    except urllib.error.HTTPError as exc:
        raise RegistryError(
            "release_registry_unreachable", f"the registry answered HTTP {exc.code}"
        ) from exc


def list_tags(repository, *, opener=None, timeout=DEFAULT_TIMEOUT, clock=time.monotonic):
    """Every tag the repository publishes, in the order the registry lists them."""

    opener = opener or urllib.request.urlopen
    budget = _Budget(timeout, clock)
    host, path = split_repository(repository)
    base = f"https://{host}"
    url = f"{base}/v2/{path}/tags/list?n={PAGE_SIZE}"
    headers = {"Accept": "application/json"}

    try:
        with _open(opener, url, headers, budget) as response:
            payload, link = _read(response), response.headers.get("Link", "")
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise RegistryError(
                "release_registry_unreachable", f"the registry answered HTTP {exc.code}"
            ) from exc
        token = _token(opener, exc.headers.get("WWW-Authenticate", ""), budget)
        headers["Authorization"] = f"Bearer {token}"
        payload, link = _page(opener, url, headers, budget)

    tags = []
    pages = 1
    while True:
        listed = payload.get("tags") if isinstance(payload, dict) else None
        tags.extend(item for item in (listed or []) if isinstance(item, str))
        match = _NEXT_LINK.search(link or "")
        if not match or pages >= MAX_PAGES:
            break
        payload, link = _page(opener, urllib.parse.urljoin(base, match.group(1)), headers, budget)
        pages += 1
    return tags
