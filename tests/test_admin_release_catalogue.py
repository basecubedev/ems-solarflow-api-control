# SPDX-License-Identifier: AGPL-3.0-or-later
"""The release list comes from a CI-published catalogue, not from the API.

Every visit to Maintenance -> Upgrade used to spend a dozen unauthenticated
GitHub API calls -- the release list plus one ``git/trees`` read of half a
megabyte per eligible tag -- against a limit of sixty an hour per address, and
a failed check was retried on the next reload. A few visits after an upgrade
emptied the budget, and an empty budget emptied the list down to whatever was
cached. CI now answers both questions once, with a token, and publishes one
file the Admin reads over the content CDN. The API path stays as the fallback
for an Admin that cannot read the catalogue, and a catalogue that cannot be
trusted is ``None`` rather than an empty list, because "could not be read" must
never turn into "there are no releases".
"""

import io
import json
import sys
import urllib.error
from pathlib import Path
from urllib.parse import urlparse

import pytest

from admin.release_catalogue import (
    CATALOGUE_MAX_BYTES,
    CATALOGUE_SCHEMA,
    load_release_catalogue,
    release_catalogue_source,
)
from admin.releases import ReleaseManager

import test_admin_releases as harness

pytestmark = [
    pytest.mark.admin,
    pytest.mark.contract,
    pytest.mark.simulation,
]


def _entry(tag, *, channel="stable", prerelease=False, resources=True, published="2026-09-13T00:00:00Z"):
    return {
        "tag": tag,
        "name": tag,
        "channel": channel,
        "prerelease": prerelease,
        "published_at": published,
        "resources_present": resources,
    }


def _document(*entries, schema=CATALOGUE_SCHEMA, main_resources=True):
    return {
        "schema": schema,
        "generated_at": "2026-09-15T00:00:00Z",
        "main_resources_present": main_resources,
        "releases": list(entries),
    }


def _catalogue(*entries, main_resources=True):
    return {"releases": list(entries), "main_resources_present": main_resources}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _serving(document):
    raw = json.dumps(document).encode("utf-8") if not isinstance(document, bytes) else document

    def opener(request, timeout=None):
        return _Response(raw)

    return opener


def _failing(request, timeout=None):
    raise urllib.error.URLError("network down")


# --- the loader ------------------------------------------------------------


def test_a_valid_catalogue_yields_its_releases(tmp_path):
    releases = load_release_catalogue(
        "https://example.test/release-catalogue.json",
        cache_path=tmp_path / "cache.json",
        urlopen=_serving(_document(_entry("v0.8.4"), _entry("v0.7.0"))),
    )

    assert [item["tag"] for item in releases["releases"]] == ["v0.8.4", "v0.7.0"]
    assert releases["releases"][0]["resources_present"] is True
    assert releases["main_resources_present"] is True


def test_a_malformed_catalogue_is_none_not_empty(tmp_path):
    """"Could not be read" must not become "there are no releases"."""

    for document in (b"not json", _document({"tag": "nonsense"}), _document(_entry("v0.8.4"), schema=99)):
        assert load_release_catalogue(
            "https://example.test/release-catalogue.json",
            cache_path=tmp_path / "cache.json",
            urlopen=_serving(document),
        ) is None


def test_an_oversized_catalogue_is_refused(tmp_path):
    raw = b'{"schema": 1, "releases": [' + b" " * (CATALOGUE_MAX_BYTES + 1) + b"]}"

    assert load_release_catalogue(
        "https://example.test/release-catalogue.json",
        cache_path=tmp_path / "cache.json",
        urlopen=_serving(raw),
    ) is None


def test_a_transport_failure_uses_a_fresh_cache_and_otherwise_none(tmp_path):
    cache = tmp_path / "cache.json"
    load_release_catalogue(
        "https://example.test/release-catalogue.json",
        cache_path=cache,
        urlopen=_serving(_document(_entry("v0.8.4"))),
    )

    cached = load_release_catalogue(
        "https://example.test/release-catalogue.json", cache_path=cache, urlopen=_failing
    )
    assert [item["tag"] for item in cached["releases"]] == ["v0.8.4"]

    assert load_release_catalogue(
        "https://example.test/release-catalogue.json",
        cache_path=tmp_path / "absent.json",
        urlopen=_failing,
    ) is None


def test_a_catalogue_that_names_no_release_is_trusted(tmp_path):
    assert load_release_catalogue(
        "https://example.test/release-catalogue.json",
        cache_path=tmp_path / "cache.json",
        urlopen=_serving(_document()),
    ) == {"releases": [], "main_resources_present": True}


# --- the release manager -----------------------------------------------------


def _manager(tmp_path, source, *, api_opener=None, running_tag="latest", cache_latest=True):
    data = tmp_path / "data"
    if cache_latest:
        harness._write_cached(data, "latest")
    else:
        data.mkdir(parents=True, exist_ok=True)
    running = {
        "digest": "sha256:" + "l" * 64,
        "labels": {
            "de.basecubedev.ems.channel": "latest",
            "de.basecubedev.ems.build_serial": "169",
            "de.basecubedev.ems.release_tag": "latest",
            "org.opencontainers.image.version": "latest",
            "org.opencontainers.image.revision": "d" * 40,
            "de.basecubedev.ems.build_id": "latest-x",
            "de.basecubedev.ems.contains_release": "v0.8.4",
        },
    }
    return ReleaseManager(
        data_dir=data,
        project_dir=harness._project_compose(tmp_path, running_tag),
        urlopen=api_opener or harness._opener(),
        docker=harness._FakeDocker(
            container=harness._running_container(running_tag),
            images={harness._ref(running_tag): running},
        ),
        release_source=source,
    )


def test_the_listing_asks_the_api_for_nothing_when_the_catalogue_answers(tmp_path):
    api_calls = []

    def counting(request, timeout=None):
        api_calls.append(request.full_url)
        raise AssertionError(f"unexpected API call: {request.full_url}")

    manager = _manager(
        tmp_path,
        lambda: _catalogue(_entry("v0.8.4"), _entry("v0.8.3"), _entry("v0.7.0", published="2026-07-07T00:00:00Z")),
        api_opener=counting,
    )
    result = manager.list_releases()
    by_tag = {item["tag"]: item for item in result["releases"]}

    assert api_calls == []
    assert set(by_tag) >= {"v0.8.4", "v0.8.3", "v0.7.0", "latest"}
    assert by_tag["v0.8.4"]["docker_supported"] is True
    assert by_tag["latest"]["docker_supported"] is True
    assert by_tag["v0.7.0"]["upgrade_state"] == "downgrade_blocked"
    assert result["default_release"] != "v0.7.0"


def test_a_release_whose_resources_are_absent_is_not_docker_supported(tmp_path):
    manager = _manager(tmp_path, lambda: _catalogue(_entry("v0.8.4"), _entry("v0.8.3", resources=False)))
    by_tag = {item["tag"]: item for item in manager.list_releases()["releases"]}

    assert by_tag["v0.8.3"]["docker_supported"] is False
    assert "missing" in by_tag["v0.8.3"]["reason"]


def test_release_candidates_keep_their_channel(tmp_path):
    manager = _manager(tmp_path, lambda: _catalogue(_entry("v0.9.0-RC1", channel="rc", prerelease=True)))
    by_tag = {item["tag"]: item for item in manager.list_releases()["releases"]}

    assert by_tag["v0.9.0-RC1"]["channel"] == "rc"
    assert by_tag["v0.9.0-RC1"]["prerelease"] is True


def test_an_unusable_catalogue_falls_back_to_the_api(tmp_path):
    """``None`` from the source is "ask the API", never "show nothing"."""

    manager = _manager(tmp_path, lambda: None, api_opener=harness._opener(payload=harness._two_line_payload()))
    by_tag = {item["tag"]: item for item in manager.list_releases()["releases"]}

    assert "v0.8.3" in by_tag


def test_a_raising_source_never_breaks_the_listing(tmp_path):
    def broken():
        raise RuntimeError("catalogue exploded")

    manager = _manager(tmp_path, broken, api_opener=harness._opener(payload=harness._two_line_payload()))

    assert "v0.8.3" in {item["tag"] for item in manager.list_releases()["releases"]}


def test_the_production_source_reads_the_public_catalogue(tmp_path):
    source = release_catalogue_source(
        "https://example.test/release-catalogue.json",
        cache_path=tmp_path / "cache.json",
        urlopen=_serving(_document(_entry("v0.8.4"))),
    )

    assert [item["tag"] for item in source()["releases"]] == ["v0.8.4"]


# --- the CI builder ----------------------------------------------------------


def test_the_builder_records_what_the_admin_used_to_ask_for(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import release_catalogue as builder

    complete = {"tree": [{"path": p, "type": "blob"} for p in (
        "config.template.json", "docker-compose.example.yml", "install-docker.sh",
        "install-docker.ps1", "deploy/docker/compose.influxdb.yml",
    )]}
    incomplete = {"tree": [{"path": "README.md", "type": "blob"}]}
    listing = [
        {"tag_name": "v0.8.4", "name": "v0.8.4", "published_at": "2026-09-13T00:00:00Z", "prerelease": False, "draft": False},
        {"tag_name": "v0.9.0-RC1", "name": "rc", "published_at": "2026-09-14T00:00:00Z", "prerelease": True, "draft": False},
        {"tag_name": "v0.7.0", "name": "v0.7.0", "published_at": "2026-07-07T00:00:00Z", "prerelease": False, "draft": False},
        {"tag_name": "appliance-image-v0.3.1", "name": "appliance", "prerelease": False, "draft": False},
        {"tag_name": "v0.6.0", "name": "draft", "prerelease": False, "draft": True},
    ]

    def opener(request, timeout=None):
        url = request.full_url
        if "/releases?" in url:
            return _Response(json.dumps(listing if "page=1" in url else []).encode())
        tree = complete if "/git/trees/v0.7.0" not in url else incomplete
        return _Response(json.dumps(tree).encode())

    payload = builder.build_catalogue("token", opener=opener)
    by_tag = {item["tag"]: item for item in payload["releases"]}

    assert payload["schema"] == CATALOGUE_SCHEMA
    assert payload["main_resources_present"] is True
    assert list(by_tag) == ["v0.9.0-RC1", "v0.8.4", "v0.7.0"]
    assert by_tag["v0.9.0-RC1"]["channel"] == "rc"
    assert by_tag["v0.8.4"]["resources_present"] is True
    assert by_tag["v0.7.0"]["resources_present"] is False

    out = tmp_path / "release-catalogue.json"
    builder.write_catalogue(out, payload)
    assert [item["tag"] for item in load_release_catalogue(out)["releases"]] == ["v0.9.0-RC1", "v0.8.4", "v0.7.0"]


def test_the_rolling_channel_needs_no_api_call_either(tmp_path):
    """`latest` used to be the one row still read from `git/trees/main`."""

    api_calls = []

    def counting(request, timeout=None):
        api_calls.append(request.full_url)
        raise AssertionError(f"unexpected API call: {request.full_url}")

    manager = _manager(
        tmp_path,
        lambda: _catalogue(_entry("v0.8.4"), main_resources=False),
        api_opener=counting,
        cache_latest=False,
    )
    by_tag = {item["tag"]: item for item in manager.list_releases()["releases"]}

    assert api_calls == []
    assert by_tag["latest"]["docker_supported"] is False
    assert "missing" in by_tag["latest"]["reason"]


def test_placing_the_running_build_costs_one_container_read_per_listing(tmp_path):
    """Reading the channel off the running image must not multiply `docker ps`.

    The rolling decision now asks the running identity several times per
    listing; it is read once per request and forgotten at the next, so a
    container replaced between requests is still seen fresh.
    """

    class Counting(harness._FakeDocker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.container_reads = 0

        def inspect_container(self, name):
            self.container_reads += 1
            return super().inspect_container(name)

    data = tmp_path / "data"
    data.mkdir()
    running = {
        "digest": "sha256:" + "l" * 64,
        "labels": {
            "de.basecubedev.ems.channel": "latest",
            "de.basecubedev.ems.build_serial": "169",
            "de.basecubedev.ems.release_tag": "latest",
            "org.opencontainers.image.version": "latest",
            "org.opencontainers.image.revision": "d" * 40,
            "de.basecubedev.ems.build_id": "latest-x",
            "de.basecubedev.ems.contains_release": "v0.8.4",
        },
    }
    docker = Counting(
        container=harness._running_container("latest"),
        images={harness._ref("latest"): running},
    )
    manager = ReleaseManager(
        data_dir=data,
        project_dir=harness._project_compose(tmp_path, "latest"),
        urlopen=harness._opener(),
        docker=docker,
        release_source=lambda: _catalogue(_entry("v0.8.4"), _entry("v0.7.0")),
    )

    manager.list_releases()
    first = docker.container_reads
    manager.list_releases()

    assert first <= 2
    assert docker.container_reads == 2 * first


def test_prepare_learns_the_download_from_the_catalogue_not_the_api(tmp_path):
    api_calls = []

    def counting(request, timeout=None):
        api_calls.append(urlparse(request.full_url).hostname)
        if urlparse(request.full_url).hostname == "api.github.com":
            raise AssertionError(f"unexpected API call: {request.full_url}")
        return harness._opener()(request, timeout)

    manager = _manager(tmp_path, lambda: _catalogue(_entry("v0.8.4")), api_opener=counting, cache_latest=False)
    manager._known_downloads.clear()

    manager._catalogued_releases()

    assert "v0.8.4" in manager._known_downloads
    assert "api.github.com" not in api_calls


def test_the_builder_records_an_unknown_ref_as_absent_and_aborts_on_anything_else(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import release_catalogue as builder

    def not_found(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    def rate_limited(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "rate limit", {}, None)

    assert builder.tree_has_resources("v0.0.1", "token", opener=not_found) is False
    with pytest.raises(urllib.error.HTTPError):
        builder.tree_has_resources("v0.8.4", "token", opener=rate_limited)
