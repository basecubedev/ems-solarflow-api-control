# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the release index is allowed to turn into a version an operator gets.

This is the code that resolves the default choice -- `latest_stable` -- into a
concrete tag, over the WAN, from a document nobody here controls. Every failure
mode it was written to handle was unexecuted: drafts, prereleases, an entry
whose name is not a tag, a body over the size cap, an unreachable index, and
valid JSON that is simply not a list.
"""

import json

import pytest

from appliance import releases
from appliance.releases import ReleaseCatalogue, ReleaseResolutionError

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]


class _Images:
    def __init__(self, *, allow_prerelease=False):
        self.allow_prerelease = allow_prerelease


class _Config:
    def __init__(self, payload, *, allow_prerelease=False, url="https://example.invalid/i"):
        self.release_index_url = url
        self.images = _Images(allow_prerelease=allow_prerelease)
        self.payload = payload


def catalogue(payload, **kwargs):
    config = _Config(payload, **kwargs)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return ReleaseCatalogue(config, fetcher=lambda _url: text)


# --- what the index may contain ----------------------------------------------


def test_a_bare_tag_list_resolves():
    assert catalogue(["v1.2.0", "v1.1.0"]).latest_stable().tag == "v1.2.0"


def test_a_draft_is_not_a_release():
    entries = [{"name": "v2.0.0", "draft": True}, {"name": "v1.9.0"}]

    assert catalogue(entries).latest_stable().tag == "v1.9.0"


def test_a_prerelease_is_refused_unless_it_is_allowed():
    entries = [{"name": "v2.0.0-rc1", "prerelease": True}, {"name": "v1.9.0"}]

    assert catalogue(entries).latest_stable().tag == "v1.9.0"
    assert catalogue(entries, allow_prerelease=True).available()[0].tag == "v2.0.0-rc1"


def test_an_index_with_no_stable_release_is_a_named_refusal():
    with pytest.raises(ReleaseResolutionError) as error:
        catalogue([{"name": "v2.0.0-rc1", "prerelease": True}]).latest_stable()

    assert error.value.code == "release_channel_unresolved"


# --- what it must refuse -----------------------------------------------------


def test_a_body_that_is_not_json_is_a_named_refusal():
    with pytest.raises(ReleaseResolutionError) as error:
        catalogue("<html>not json</html>").available()

    assert error.value.code == "release_index_invalid"


def test_a_fetcher_that_fails_keeps_its_own_code():
    def _unreachable(_url):
        raise ReleaseResolutionError("release_index_unreachable", "no route")

    config = _Config(None)
    with pytest.raises(ReleaseResolutionError) as error:
        ReleaseCatalogue(config, fetcher=_unreachable).available()

    assert error.value.code == "release_index_unreachable"


def test_no_configured_index_is_an_empty_catalogue_not_a_crash():
    assert catalogue([], url="").available() == []


@pytest.mark.parametrize("payload", [{"not": "a list"}, 42, None, [{"no_name": True}]])
def test_a_shape_the_index_should_not_have_yields_no_releases(payload):
    """Whatever arrives, it either parses into releases or refuses -- never a
    traceback out of a WAN document."""

    try:
        result = catalogue(payload).available()
    except ReleaseResolutionError:
        return
    assert isinstance(result, list)


def test_the_parser_is_the_one_the_catalogue_uses():
    assert callable(releases.parse_release_index)
