# SPDX-License-Identifier: AGPL-3.0-or-later
"""How a signed OS release reaches the appliance.

Before this existed the release directory was local and nothing ever wrote to
it, so the update feature had no transport at all. What matters here is not
that a download works but the order it happens in: an index may only name
candidates, the detached signature decides whether the manifest may be
believed, and only a verified manifest says how large the archive is and what
it must hash to. These tests drive the real service against a scripted HTTPS
layer -- no socket is opened.
"""

import hashlib
import json

import pytest

from appliance.release_fetch import (
    INDEX_FORMAT_VERSION,
    FetchError,
    parse_index,
)

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

RELEASE_ID = "ems-solarflow-appliance-0.2.0-rpi5-arm64-ab"
ARCHIVE_NAME = f"{RELEASE_ID}.tar.zst"
BASE = "https://releases.example.org"

ARCHIVE_BYTES = b"a signed appliance release archive" * 64
ARCHIVE_DIGEST = "sha256:" + hashlib.sha256(ARCHIVE_BYTES).hexdigest()


def index_payload(release_id=RELEASE_ID, *, version=INDEX_FORMAT_VERSION, extra=()):
    entries = [
        {
            "release_id": release_id,
            "manifest_url": f"{BASE}/{release_id}.manifest.json",
            "signature_url": f"{BASE}/{release_id}.manifest.json.asc",
            "archive_url": f"{BASE}/{release_id}.tar.zst",
        }
    ]
    entries.extend(extra)
    return {"format_version": version, "releases": entries}


class ScriptedHttps:
    """The network, as a dictionary. Records what was asked for, in order."""

    def __init__(self, responses=None):
        self.responses = dict(responses or {})
        self.requested = []

    def read(self, url, *, label, max_bytes):
        self.requested.append(url)
        payload = self._body(url, label)
        if len(payload) > max_bytes:
            raise FetchError("release_download_too_large", f"{label} is too large")
        return payload

    def download(self, url, destination, *, label, expected_bytes):
        self.requested.append(url)
        payload = self._body(url, label)
        if len(payload) > expected_bytes:
            raise FetchError("release_download_too_large", f"{label} is too large")
        if len(payload) != expected_bytes:
            raise FetchError("release_download_truncated", f"{label} was truncated")
        destination.write_bytes(payload)
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def _body(self, url, label):
        if url not in self.responses:
            raise FetchError("release_download_failed", f"{label} is unreachable")
        body = self.responses[url]
        return body if isinstance(body, bytes) else json.dumps(body).encode()


# --- the index is a suggestion, never an authority ---------------------------


def test_an_index_entry_only_has_to_name_a_release_and_three_https_urls():
    candidates = parse_index(index_payload())

    assert [item["release_id"] for item in candidates] == [RELEASE_ID]
    assert candidates[0]["archive_url"].startswith("https://")


def test_a_plain_http_url_is_refused_and_never_upgraded():
    payload = index_payload()
    payload["releases"][0]["archive_url"] = f"http://releases.example.org/{ARCHIVE_NAME}"

    assert parse_index(payload) == []


def test_one_malformed_entry_does_not_discard_the_rest():
    payload = index_payload(extra=[{"release_id": "../../etc/passwd"}, "not-an-object"])

    assert [item["release_id"] for item in parse_index(payload)] == [RELEASE_ID]


def test_an_index_of_an_unknown_format_is_refused_whole():
    with pytest.raises(FetchError) as caught:
        parse_index(index_payload(version=999))

    assert caught.value.code == "release_index_unsupported"


def test_a_duplicate_release_id_is_taken_once():
    payload = index_payload()
    payload["releases"].append(dict(payload["releases"][0]))

    assert len(parse_index(payload)) == 1


# --- the clock, because this board has no RTC --------------------------------


# --- planning writes nothing -------------------------------------------------


# --- the ordering that is the whole point ------------------------------------


# --- what a successful fetch leaves behind -----------------------------------


# --- a refused plan must not keep the lock -----------------------------------


# --- the real fetcher, which the scripted layer above never exercises --------


class FakeResponse:
    """Enough of an http response to drive HttpsFetcher: bytes and a final url."""

    def __init__(self, payload, *, url="https://releases.example.org/thing", chunks=None):
        self._payload = payload
        self._offset = 0
        self.url = url
        self._chunks = list(chunks or [])

    def read(self, size=-1):
        if self._chunks:
            return self._chunks.pop(0)
        if size is None or size < 0:
            size = len(self._payload) - self._offset
        block = self._payload[self._offset : self._offset + size]
        self._offset += len(block)
        return block

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fetcher(response=None, *, error=None):
    from appliance.release_fetch import HttpsFetcher

    def opener(request, timeout=None):
        if error is not None:
            raise error
        return response

    return HttpsFetcher(opener=opener)


def test_the_fetcher_refuses_a_plain_http_url_outright():
    from appliance.release_fetch import HttpsFetcher

    with pytest.raises(FetchError) as caught:
        HttpsFetcher(opener=lambda *a, **k: None).read(
            "http://releases.example.org/index.json", label="the index", max_bytes=1024
        )

    assert caught.value.code == "release_source_insecure"


def test_the_fetcher_refuses_a_redirect_that_lands_on_http():
    """urllib follows redirects itself, so the landing scheme is what counts."""

    response = FakeResponse(b"{}", url="http://releases.example.org/index.json")

    with pytest.raises(FetchError) as caught:
        fetcher(response).read(
            "https://releases.example.org/index.json", label="the index", max_bytes=1024
        )

    assert caught.value.code == "release_source_insecure"


def test_the_fetcher_refuses_a_body_over_its_cap():
    with pytest.raises(FetchError) as caught:
        fetcher(FakeResponse(b"x" * 100)).read(
            "https://releases.example.org/index.json", label="the index", max_bytes=10
        )

    assert caught.value.code == "release_download_too_large"


def test_a_body_that_exactly_fits_the_cap_is_accepted():
    payload = fetcher(FakeResponse(b"x" * 10)).read(
        "https://releases.example.org/index.json", label="the index", max_bytes=10
    )

    assert payload == b"x" * 10


def test_the_fetcher_stops_a_download_that_exceeds_the_declared_size(tmp_path):
    """The cap is the verified manifest's number, checked while streaming."""

    response = FakeResponse(b"", chunks=[b"x" * 8, b"x" * 8, b""])

    with pytest.raises(FetchError) as caught:
        fetcher(response).download(
            "https://releases.example.org/a.tar.zst",
            tmp_path / "a.tar.zst",
            label="the archive",
            expected_bytes=10,
        )

    assert caught.value.code == "release_download_too_large"


def test_a_download_that_ends_early_is_a_failure_not_a_short_file(tmp_path):
    response = FakeResponse(b"", chunks=[b"x" * 4, b""])

    with pytest.raises(FetchError) as caught:
        fetcher(response).download(
            "https://releases.example.org/a.tar.zst",
            tmp_path / "a.tar.zst",
            label="the archive",
            expected_bytes=10,
        )

    assert caught.value.code == "release_download_truncated"


def test_a_complete_download_returns_the_digest_it_hashed(tmp_path):
    body = b"appliance release bytes"
    response = FakeResponse(b"", chunks=[body, b""])

    digest = fetcher(response).download(
        "https://releases.example.org/a.tar.zst",
        tmp_path / "a.tar.zst",
        label="the archive",
        expected_bytes=len(body),
    )

    assert digest == "sha256:" + hashlib.sha256(body).hexdigest()
    assert (tmp_path / "a.tar.zst").read_bytes() == body


def test_an_unreachable_host_is_reported_as_a_download_failure():
    import urllib.error

    with pytest.raises(FetchError) as caught:
        fetcher(error=urllib.error.URLError("no route")).read(
            "https://releases.example.org/index.json", label="the index", max_bytes=1024
        )

    assert caught.value.code == "release_download_failed"


def test_an_http_error_status_names_the_code():
    import urllib.error

    error = urllib.error.HTTPError(
        "https://releases.example.org/index.json", 404, "Not Found", {}, None
    )

    with pytest.raises(FetchError) as caught:
        fetcher(error=error).read(
            "https://releases.example.org/index.json", label="the index", max_bytes=1024
        )

    assert caught.value.code == "release_download_failed"
    assert "404" in caught.value.message
