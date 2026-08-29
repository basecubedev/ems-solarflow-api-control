# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which Admin versions exist, asked of the registry that holds the images.

A git tag is not an image and a curated index is a list that can disagree with
both. The registry answers with what can actually be pulled, so this is where
the installable versions come from -- which makes its failure modes, its
anonymous token exchange and its paging worth pinning down.
"""

import io
import json
import urllib.error

import pytest

from appliance import registry_tags
from appliance.registry_tags import RegistryError

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

REPOSITORY = "ghcr.io/basecubedev/ems-solarflow-admin"
CHALLENGE = (
    'Bearer realm="https://ghcr.io/token",service="ghcr.io",'
    'scope="repository:basecubedev/ems-solarflow-admin:pull"'
)


class _Response(io.BytesIO):
    def __init__(self, payload, *, url="https://ghcr.io/v2/", headers=None):
        super().__init__(json.dumps(payload).encode("utf-8"))
        self.url = url
        self.headers = headers or {}


class _Registry:
    """A registry that answers a challenge once and then serves pages."""

    def __init__(self, pages, *, token="anonymous-token", challenge=CHALLENGE):
        self.pages = list(pages)
        self.token = token
        self.challenge = challenge
        self.requests = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.requests.append((url, dict(request.header_items())))
        if url.startswith("https://ghcr.io/token"):
            return _Response({"token": self.token}, url=url)
        if self.challenge and "Authorization" not in dict(request.header_items()):
            raise urllib.error.HTTPError(
                url, 401, "Unauthorized", {"WWW-Authenticate": self.challenge}, None
            )
        page = self.pages.pop(0)
        return _Response(
            {"tags": page["tags"]},
            url=url,
            headers={"Link": page["link"]} if page.get("link") else {},
        )


def page(tags, link=""):
    return {"tags": tags, "link": link}


# --- what it reads -----------------------------------------------------------


def test_the_published_tags_are_what_the_registry_lists():
    registry = _Registry([page(["latest", "v0.7.0", "v0.8.0-RC1"])])

    assert registry_tags.list_tags(REPOSITORY, opener=registry) == [
        "latest",
        "v0.7.0",
        "v0.8.0-RC1",
    ]


def test_an_anonymous_token_is_fetched_from_the_challenge_and_then_used():
    """No credentials exist here; the pull scope the registry names is enough."""

    registry = _Registry([page(["v0.7.0"])])

    registry_tags.list_tags(REPOSITORY, opener=registry)

    urls = [url for url, _ in registry.requests]
    assert urls[0].startswith("https://ghcr.io/v2/basecubedev/ems-solarflow-admin/tags/list")
    assert urls[1].startswith("https://ghcr.io/token?")
    assert "scope=repository%3Abasecubedev%2Fems-solarflow-admin%3Apull" in urls[1]
    assert dict(registry.requests[2][1]).get("Authorization") == "Bearer anonymous-token"


def test_a_registry_that_needs_no_token_is_read_directly():
    registry = _Registry([page(["v1.0.0"])], challenge="")

    assert registry_tags.list_tags(REPOSITORY, opener=registry) == ["v1.0.0"]
    assert len(registry.requests) == 1


def test_paging_follows_the_link_header_the_registry_sends():
    registry = _Registry(
        [
            page(["v0.7.0"], link='</v2/basecubedev/ems-solarflow-admin/tags/list?last=v0.7.0>; rel="next"'),
            page(["v0.8.0"]),
        ],
        challenge="",
    )

    assert registry_tags.list_tags(REPOSITORY, opener=registry) == ["v0.7.0", "v0.8.0"]
    assert registry.requests[1][0].endswith("/tags/list?last=v0.7.0")


def test_the_whole_lookup_is_bounded_by_one_budget_not_by_request():
    """A challenge, a token and ten pages are eleven requests. An operator waits
    once, not eleven times, and the agent's own operation timeout is 30s."""

    ticks = iter([0.0, 0.0, 4.0, 9.9, 10.1])
    endless = page(["v0.7.0"], link='</v2/x/tags/list?last=v0.7.0>; rel="next"')
    registry = _Registry([endless] * 6, challenge="")

    with pytest.raises(RegistryError) as error:
        registry_tags.list_tags(REPOSITORY, opener=registry, clock=lambda: next(ticks))

    assert error.value.code == "release_registry_unreachable"
    assert "in time" in error.value.message


def test_paging_stops_at_the_bound_rather_than_following_forever():
    """A registry that always claims another page must not hold the agent."""

    endless = page(["v0.7.0"], link='</v2/x/tags/list?last=v0.7.0>; rel="next"')
    registry = _Registry([endless] * (registry_tags.MAX_PAGES + 5), challenge="")

    registry_tags.list_tags(REPOSITORY, opener=registry)

    assert len(registry.requests) == registry_tags.MAX_PAGES


# --- what it refuses ---------------------------------------------------------


def test_a_repository_with_no_registry_host_resolves_to_docker_hub():
    registry = _Registry([page(["v1.0.0"])], challenge="")

    registry_tags.list_tags("basecubedev/ems-solarflow-admin", opener=registry)

    assert registry.requests[0][0].startswith(
        "https://registry-1.docker.io/v2/basecubedev/ems-solarflow-admin/tags/list"
    )


def test_a_token_realm_that_is_not_https_is_refused():
    """The realm comes from the registry's own answer, so it is checked."""

    registry = _Registry([page(["v1.0.0"])], challenge='Bearer realm="http://ghcr.io/token"')

    with pytest.raises(RegistryError) as error:
        registry_tags.list_tags(REPOSITORY, opener=registry)

    assert error.value.code == "release_registry_unreachable"


def test_an_unreachable_registry_is_a_named_refusal():
    def _unreachable(request, timeout=None):
        raise urllib.error.URLError("no route")

    with pytest.raises(RegistryError) as error:
        registry_tags.list_tags(REPOSITORY, opener=_unreachable)

    assert error.value.code == "release_registry_unreachable"


def test_a_body_that_is_not_json_is_a_named_refusal():
    def _garbage(request, timeout=None):
        response = io.BytesIO(b"<html>no</html>")
        response.url = request.full_url
        response.headers = {}
        return response

    with pytest.raises(RegistryError) as error:
        registry_tags.list_tags(REPOSITORY, opener=_garbage)

    assert error.value.code == "release_registry_invalid"


def test_a_tag_list_that_is_not_a_list_of_strings_yields_no_tags():
    registry = _Registry([page([{"tag": "v1.0.0"}, "v0.9.0", 7])], challenge="")

    assert registry_tags.list_tags(REPOSITORY, opener=registry) == ["v0.9.0"]


def test_an_oversized_answer_is_refused_rather_than_truncated():
    def _flood(request, timeout=None):
        response = io.BytesIO(b" " * (registry_tags.MAX_TAGS_BYTES + 10))
        response.url = request.full_url
        response.headers = {}
        return response

    with pytest.raises(RegistryError) as error:
        registry_tags.list_tags(REPOSITORY, opener=_flood)

    assert error.value.code == "release_registry_invalid"
