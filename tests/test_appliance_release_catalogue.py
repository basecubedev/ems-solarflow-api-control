# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a release source is allowed to turn into a version an operator gets.

This is the code that resolves the default choice -- `latest_stable` -- into a
concrete tag, and that builds the list an operator picks from. It reads either
the registry holding the images or, when an operator configured one, a release
index over the WAN. Every failure mode it handles matters here: drafts,
prereleases, an entry whose name is not a tag, a body over the size cap, an
unreachable source, and valid JSON that is simply not a list.
"""

import json
from http.client import IncompleteRead

import pytest

from appliance import registry_tags, releases
from appliance.releases import ReleaseCatalogue, ReleaseResolutionError

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]


REPOSITORY = "ghcr.io/basecubedev/ems-solarflow-admin"


class _Images:
    def __init__(self, *, allow_prerelease=False):
        self.allow_prerelease = allow_prerelease
        self.admin_repository = REPOSITORY


class _Config:
    def __init__(self, payload, *, allow_prerelease=False, url="https://example.invalid/i"):
        self.release_index_url = url
        self.images = _Images(allow_prerelease=allow_prerelease)
        self.payload = payload


def _no_registry(_repository):
    raise AssertionError("the registry must not be read when an index is configured")


def catalogue(payload, *, registry=_no_registry, **kwargs):
    config = _Config(payload, **kwargs)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return ReleaseCatalogue(config, fetcher=lambda _url: text, registry=registry)


# --- what the index may contain ----------------------------------------------


def test_a_bare_tag_list_resolves():
    assert catalogue(["v1.2.0", "v1.1.0"]).latest_stable().tag == "v1.2.0"


def test_a_draft_is_not_a_release():
    entries = [{"name": "v2.0.0", "draft": True}, {"name": "v1.9.0"}]

    assert catalogue(entries).latest_stable().tag == "v1.9.0"


def test_a_prerelease_is_listed_but_not_installable_unless_it_is_allowed():
    """Hiding it would answer "does this version exist?" with silence."""

    entries = [{"name": "v2.0.0-rc1", "prerelease": True}, {"name": "v1.9.0"}]

    refusing = catalogue(entries).available()
    assert [item.tag for item in refusing] == ["v1.9.0", "v2.0.0-rc1"]
    assert [item.installable for item in refusing] == [True, False]
    assert "allow_prerelease" in refusing[1].reason

    allowing = catalogue(entries, allow_prerelease=True).available()
    assert [item.tag for item in allowing] == ["v1.9.0", "v2.0.0-rc1"]
    assert [item.installable for item in allowing] == [True, True]
    assert allowing[1].reason == ""


def test_an_index_that_calls_a_candidate_a_release_is_not_believed():
    """The tag is the fail-closed half of the prerelease question."""

    entries = [{"name": "v2.0.0-rc1", "prerelease": False}, {"name": "v1.9.0"}]

    listed = catalogue(entries).available()

    assert [item.tag for item in listed] == ["v1.9.0", "v2.0.0-rc1"]
    assert listed[1].prerelease is True
    assert catalogue(entries).latest_stable().tag == "v1.9.0"


def test_installability_reaches_the_browser_through_to_dict():
    entry = catalogue([{"name": "v2.0.0-rc1", "prerelease": True}]).available()[0]

    assert entry.to_dict()["installable"] is False
    assert entry.to_dict()["reason"]


def test_releases_are_grouped_by_stability_then_ordered_newest_first():
    """The order the operator sees: every release, then every candidate.

    A candidate sorts directly below the release it precedes, so a plain
    version sort interleaves the two and buries the newest release under the
    candidates for the next one.
    """

    entries = ["v1.9.0", "v2.0.0-rc2", "v1.10.0", "v2.0.0-rc10", "v1.9.1"]

    result = catalogue(entries, allow_prerelease=True).available()

    assert [item.tag for item in result] == [
        "v1.10.0",
        "v1.9.1",
        "v1.9.0",
        "v2.0.0-rc10",
        "v2.0.0-rc2",
    ]
    assert [item.prerelease for item in result] == [False, False, False, True, True]


def test_latest_stable_is_the_newest_release_and_never_a_candidate():
    """Holds on an unfiltered list, which is now the only kind there is."""

    entries = ["v1.9.0", "v2.0.0-rc1", "v1.10.0"]

    for allowed in (True, False):
        result = catalogue(entries, allow_prerelease=allowed)

        assert any(item.prerelease for item in result.available())
        assert result.latest_stable().tag == "v1.10.0"


def test_an_index_of_only_candidates_resolves_no_stable_channel():
    with pytest.raises(ReleaseResolutionError) as error:
        catalogue(["v2.0.0-rc1"], allow_prerelease=True).latest_stable()

    assert error.value.code == "release_channel_unresolved"


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


# --- where the list comes from -----------------------------------------------


def test_without_a_configured_index_the_registry_holding_the_images_is_asked():
    """A git tag is not an image. Only the registry knows what can be pulled."""

    asked = []

    def _registry(repository):
        asked.append(repository)
        return ["latest", "v1.1.0", "v1.0.0"]

    result = catalogue(None, url="", registry=_registry).available()

    assert asked == [REPOSITORY]
    assert [item.tag for item in result] == ["v1.1.0", "v1.0.0"]


def test_a_mutable_tag_the_registry_publishes_is_not_an_installable_version():
    result = catalogue(None, url="", registry=lambda _r: ["latest", "main", "v1.0.0"]).available()

    assert [item.tag for item in result] == ["v1.0.0"]


def test_a_configured_index_wins_over_the_registry():
    """An operator who points at a mirror gets the mirror, not ghcr.io."""

    assert [item.tag for item in catalogue(["v3.0.0"]).available()] == ["v3.0.0"]


def test_a_registry_failure_keeps_its_own_code():
    def _unreachable(_repository):
        raise registry_tags.RegistryError("release_registry_unreachable", "no route")

    with pytest.raises(ReleaseResolutionError) as error:
        catalogue(None, url="", registry=_unreachable).available()

    assert error.value.code == "release_registry_unreachable"


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


# --- the transport an index is read over -------------------------------------


class _IndexResponse:
    def __init__(self, body, url):
        self._body = body
        self.url = url

    def read(self, size=-1):
        block, self._body = self._body, b""
        return block

    read1 = read

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _BreaksOff(_IndexResponse):
    """An answer whose headers arrived and whose body read fails."""

    def __init__(self, error, url):
        super().__init__(b"", url)
        self._error = error

    def read(self, size=-1):
        raise self._error

    read1 = read


def _network(monkeypatch, body=b'["v1.2.0", "v1.1.0"]', *, landing=None, answer=None):
    """The fetcher's own opener, replaced: what was asked for, and an answer."""

    from appliance import release_fetch

    asked = []

    def _open(request, *, timeout, deadline):
        url = request.full_url
        asked.append(url)
        if answer is not None:
            return answer(url)
        return _IndexResponse(body, landing or url)

    monkeypatch.setattr(release_fetch, "_open_watched", _open)
    return asked


@pytest.mark.parametrize(
    "url",
    ["http://mirror.example.org/releases.json", "file:///etc/ems-appliance-manager/releases.json"],
)
def test_an_index_that_is_not_https_is_refused_and_never_read(monkeypatch, url):
    """The index picks the tag *Latest stable* installs. Over plain http anyone on
    the path picks it, an old release included; ``file:`` reads the root agent's
    own disk. The Manager's own index has been https-only from the start."""

    asked = _network(monkeypatch)

    with pytest.raises(ReleaseResolutionError) as error:
        ReleaseCatalogue(_Config(None, url=url)).available()

    assert error.value.code == "release_index_insecure"
    assert asked == []


def test_an_index_that_redirects_to_http_is_refused(monkeypatch):
    _network(monkeypatch, landing="http://mirror.example.org/releases.json")

    with pytest.raises(ReleaseResolutionError) as error:
        ReleaseCatalogue(_Config(None, url="https://mirror.example.org/releases.json")).available()

    assert error.value.code == "release_index_insecure"


def test_an_https_index_is_read(monkeypatch):
    asked = _network(monkeypatch)

    listed = ReleaseCatalogue(_Config(None, url="https://mirror.example.org/r.json")).available()

    assert asked == ["https://mirror.example.org/r.json"]
    assert [item.tag for item in listed] == ["v1.2.0", "v1.1.0"]


@pytest.mark.parametrize(
    "error",
    [ConnectionResetError(104, "reset"), TimeoutError("timed out"), IncompleteRead(b'["v1')],
    ids=["reset", "socket timeout", "incomplete"],
)
def test_an_index_whose_body_breaks_off_is_reported_unreachable(monkeypatch, error):
    """The list answers empty with the reason, as it did before the index went
    through HttpsFetcher, rather than failing the call as an agent error."""

    _network(monkeypatch, answer=lambda url: _BreaksOff(error, url))

    with pytest.raises(ReleaseResolutionError) as refused:
        ReleaseCatalogue(_Config(None, url="https://mirror.example.org/r.json")).available()

    assert refused.value.code == "release_index_unreachable"
