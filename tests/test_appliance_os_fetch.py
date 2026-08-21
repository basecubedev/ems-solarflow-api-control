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

from appliance import os_releases
from appliance.agent import AgentHandlers
from appliance.ab_persistence import PERSISTENT_SCHEMA_VERSION
from appliance.os_fetch import (
    INDEX_FORMAT_VERSION,
    FetchError,
    OsFetchService,
    parse_index,
)
from tests.helpers.appliance import build_test_services

pytestmark = [pytest.mark.integration, pytest.mark.simulation]

RELEASE_ID = "ems-solarflow-appliance-0.2.0-rpi5-arm64-ab"
ARCHIVE_NAME = f"{RELEASE_ID}.tar.zst"
BASE = "https://releases.example.org"

ARCHIVE_BYTES = b"a signed appliance release archive" * 64
ARCHIVE_DIGEST = "sha256:" + hashlib.sha256(ARCHIVE_BYTES).hexdigest()


def manifest_payload(*, archive_digest=ARCHIVE_DIGEST, archive_size=None, name=ARCHIVE_NAME):
    return {
        "format_version": 2,
        "release_version": "0.2.0",
        "build_id": "20260819120000",
        "created_at": "2026-08-19T12:00:00Z",
        "project_revision": "0" * 40,
        "architecture": "arm64",
        "device_layer": "rpi5",
        "image_layer": "image-rota",
        "layout_id": "ems-appliance-rota-v1",
        "os_release": "Raspberry Pi OS Trixie arm64",
        "slot_schema_version": 2,
        "persistent_schema_version": PERSISTENT_SCHEMA_VERSION,
        "compatible_hardware": ["pi5"],
        "appliance_manager_version": "0.1.0",
        "minimum_appliance_manager_version": "0.1.0",
        "archive": {
            "name": name,
            "digest": archive_digest,
            "size_bytes": len(ARCHIVE_BYTES) if archive_size is None else archive_size,
            "compression": "zstd",
        },
        "members": {
            # Unencoded members: the manifest schema requires both digests to
            # be the same value, because there is no transformation between them.
            "boot": {
                "encoding": "raw",
                "encoded_sha256": "sha256:" + "1" * 64,
                "expanded_sha256": "sha256:" + "1" * 64,
                "expanded_size": 1024,
            },
            "system": {
                "encoding": "raw",
                "encoded_sha256": "sha256:" + "3" * 64,
                "expanded_sha256": "sha256:" + "3" * 64,
                "expanded_size": 4096,
            },
        },
    }


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


class ScriptedVerifier:
    """gpgv, reduced to its verdict, so the ordering can be observed."""

    def __init__(self, *, valid=True):
        self.valid = valid
        self.calls = []

    def verify(self, manifest_path, signature_path):
        self.calls.append(str(manifest_path))
        if not self.valid:
            raise os_releases.ReleaseError(
                "release_signature_invalid", "the signature could not be verified"
            )
        return True


class Clock:
    def __init__(self, synchronised=True):
        self.synchronised = synchronised

    def system_time(self):
        return {"epoch": 1787000000.0, "ntp_synchronized": self.synchronised}


def build(tmp_path, *, responses=None, valid_signature=True, synchronised=True, index=None):
    services = build_test_services(tmp_path)
    release_dir = tmp_path / "var" / "lib" / "ems-appliance-os-update" / "releases"
    release_dir.mkdir(parents=True, exist_ok=True)
    config = services.config.__class__(
        **{
            **services.config.__dict__,
            "os_release_dir": str(release_dir),
            "os_release_index_url": f"{BASE}/index.json",
        }
    )
    body = index_payload() if index is None else index
    scripted = ScriptedHttps(
        {
            f"{BASE}/index.json": body,
            f"{BASE}/{RELEASE_ID}.manifest.json": manifest_payload(),
            f"{BASE}/{RELEASE_ID}.manifest.json.asc": b"-----BEGIN PGP SIGNATURE-----\n",
            f"{BASE}/{RELEASE_ID}.tar.zst": ARCHIVE_BYTES,
            **(responses or {}),
        }
    )
    catalogue = os_releases.OsReleaseCatalogue(
        os_releases.ReleaseSource(directory=str(release_dir), keyring=str(tmp_path / "keys.gpg")),
        verifier=ScriptedVerifier(valid=valid_signature),
    )
    service = OsFetchService(
        paths=services.paths,
        config=config,
        catalogue=catalogue,
        probe=Clock(synchronised=synchronised),
        operations=services.operations,
        fetcher=scripted,
    )
    services.os_fetch = service
    services.config = config
    service.scripted = scripted
    service.release_dir_path = release_dir
    return services, service


def plan_and_execute(services, release_id=RELEASE_ID):
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch({"operation": "ab.plan_fetch", "release_id": release_id})
    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    return services.operations.get(planned["operation"]["operation_id"]), planned["plan"]


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


def test_a_fetch_before_the_clock_is_synchronised_is_refused(tmp_path):
    services, service = build(tmp_path, synchronised=False)

    with pytest.raises(FetchError) as caught:
        service.plan_fetch(_operation(services), RELEASE_ID)

    assert caught.value.code == "clock_not_synchronised"
    assert "real-time clock" in caught.value.message


def test_a_clock_that_cannot_be_confirmed_counts_as_unsynchronised(tmp_path):
    services, service = build(tmp_path)
    service.probe = type("Unknown", (), {"system_time": lambda self: {"ntp_synchronized": None}})()

    with pytest.raises(FetchError) as caught:
        service.plan_fetch(_operation(services), RELEASE_ID)

    assert caught.value.code == "clock_not_synchronised"


def test_the_clock_is_checked_before_anything_is_downloaded(tmp_path):
    services, service = build(tmp_path, synchronised=False)

    with pytest.raises(FetchError):
        service.plan_fetch(_operation(services), RELEASE_ID)

    assert service.scripted.requested == []


# --- planning writes nothing -------------------------------------------------


def test_planning_reports_what_would_be_fetched_without_fetching_it(tmp_path):
    services, service = build(tmp_path)

    plan = service.plan_fetch(_operation(services), RELEASE_ID)

    assert plan["release_id"] == RELEASE_ID
    assert plan["archive_url"] == f"{BASE}/{ARCHIVE_NAME}"
    assert list(service.release_dir_path.iterdir()) == []


def test_the_plan_does_not_claim_a_size_it_cannot_know_yet(tmp_path):
    """The size lives in the manifest, and the manifest is not trusted yet."""

    services, service = build(tmp_path)

    plan = service.plan_fetch(_operation(services), RELEASE_ID)

    assert plan["size_known"] is False
    assert "signature has been verified" in plan["warning"]


# --- the ordering that is the whole point ------------------------------------


def test_the_signature_is_verified_before_the_archive_is_touched(tmp_path):
    services, service = build(tmp_path)

    plan_and_execute(services)

    requested = service.scripted.requested
    manifest = requested.index(f"{BASE}/{RELEASE_ID}.manifest.json")
    archive = requested.index(f"{BASE}/{ARCHIVE_NAME}")
    assert manifest < archive
    assert service.catalogue.verifier.calls, "the signature was never verified"


def test_an_unverifiable_signature_stops_before_the_archive(tmp_path):
    services, service = build(tmp_path, valid_signature=False)

    record, _ = plan_and_execute(services)

    assert record.error["code"] == "release_signature_invalid"
    assert f"{BASE}/{ARCHIVE_NAME}" not in service.scripted.requested
    assert list(service.release_dir_path.iterdir()) == []


def test_an_archive_that_does_not_match_the_verified_manifest_is_discarded(tmp_path):
    services, service = build(
        tmp_path, responses={f"{BASE}/{ARCHIVE_NAME}": b"x" * len(ARCHIVE_BYTES)}
    )

    record, _ = plan_and_execute(services)

    assert record.error["code"] == "artifact_digest_mismatch"
    assert list(service.release_dir_path.iterdir()) == []


def test_an_archive_longer_than_the_verified_manifest_declares_is_refused(tmp_path):
    services, service = build(
        tmp_path, responses={f"{BASE}/{ARCHIVE_NAME}": ARCHIVE_BYTES + b"more"}
    )

    record, _ = plan_and_execute(services)

    assert record.error["code"] == "release_download_too_large"
    assert list(service.release_dir_path.iterdir()) == []


def test_a_manifest_declaring_an_implausible_size_is_refused(tmp_path):
    payload = manifest_payload(archive_size=64 * 1024 * 1024 * 1024)
    services, service = build(
        tmp_path, responses={f"{BASE}/{RELEASE_ID}.manifest.json": payload}
    )

    record, _ = plan_and_execute(services)

    assert record.error["code"] == "release_manifest_invalid"


# --- what a successful fetch leaves behind -----------------------------------


def test_a_verified_release_is_placed_where_the_catalogue_reads_it(tmp_path):
    services, service = build(tmp_path)

    record, _ = plan_and_execute(services)

    present = sorted(item.name for item in service.release_dir_path.iterdir())
    assert record.result["release_id"] == RELEASE_ID
    assert present == sorted(
        [ARCHIVE_NAME, f"{RELEASE_ID}.manifest.json", f"{RELEASE_ID}.manifest.json.asc"]
    )
    assert service.catalogue.get(RELEASE_ID).release_id == RELEASE_ID


def test_the_staging_directory_never_survives_the_operation(tmp_path):
    services, service = build(tmp_path)

    plan_and_execute(services)

    leftovers = [item.name for item in service.release_dir_path.iterdir() if item.is_dir()]
    assert leftovers == []


def test_a_release_that_is_already_here_is_not_fetched_again(tmp_path):
    services, service = build(tmp_path)
    plan_and_execute(services)
    service.scripted.requested.clear()

    with pytest.raises(Exception) as caught:
        plan_and_execute(services)

    assert getattr(caught.value, "code", "") == "release_already_present"
    assert f"{BASE}/{ARCHIVE_NAME}" not in service.scripted.requested


def test_a_release_the_index_does_not_offer_is_refused(tmp_path):
    services, service = build(tmp_path)

    with pytest.raises(FetchError) as caught:
        service.plan_fetch(_operation(services), "some-other-release")

    assert caught.value.code == "unknown_release"


def test_an_unconfigured_index_is_reported_and_not_guessed(tmp_path):
    services, service = build(tmp_path)
    service.config = service.config.__class__(
        **{**service.config.__dict__, "os_release_index_url": ""}
    )

    listing = service.index()

    assert listing["configured"] is False
    assert listing["releases"] == []


def test_the_index_reports_which_releases_are_already_local(tmp_path):
    services, service = build(tmp_path)
    plan_and_execute(services)

    listing = service.index()

    assert listing["local"] == [RELEASE_ID]
    assert listing["releases"][0]["present"] is True


# --- a refused plan must not keep the lock -----------------------------------


def test_a_refused_fetch_plan_releases_the_operation_lock(tmp_path):
    """Otherwise one bad release id wedges every later operation."""

    services, service = build(tmp_path)
    handlers = AgentHandlers(services, executor=lambda target: target())

    with pytest.raises(Exception) as caught:
        handlers.dispatch({"operation": "ab.plan_fetch", "release_id": "not-offered"})

    assert getattr(caught.value, "code", "") == "unknown_release"
    assert services.operations.active() is None


def _operation(services):
    from appliance.os_fetch import TYPE_OS_FETCH

    return services.operations.create(TYPE_OS_FETCH)


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
    from appliance.os_fetch import HttpsFetcher

    def opener(request, timeout=None):
        if error is not None:
            raise error
        return response

    return HttpsFetcher(opener=opener)


def test_the_fetcher_refuses_a_plain_http_url_outright():
    from appliance.os_fetch import HttpsFetcher

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
