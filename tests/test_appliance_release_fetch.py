# SPDX-License-Identifier: AGPL-3.0-or-later
"""How a signed OS release reaches the appliance.

Before this existed the release directory was local and nothing ever wrote to
it, so the update feature had no transport at all. What matters here is not
that a download works but the order it happens in: an index may only name
candidates, the detached signature decides whether the manifest may be
believed, and only a verified manifest says how large the archive is and what
it must hash to. These tests drive the real service against a scripted HTTPS
layer -- no socket is opened -- except the deadline's, which serve the real
fetcher over a loopback TLS server.
"""

import datetime
import hashlib
import ipaddress
import json
import socket
import ssl
import threading
import time
from http.client import IncompleteRead

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

    def __init__(
        self, payload, *, url="https://releases.example.org/thing", chunks=None, on_read=None
    ):
        self._payload = payload
        self._offset = 0
        self.url = url
        self._chunks = list(chunks or [])
        self._on_read = on_read

    def read(self, size=-1):
        if self._on_read is not None:
            self._on_read()
        if self._chunks:
            return self._chunks.pop(0)
        if size is None or size < 0:
            size = len(self._payload) - self._offset
        block = self._payload[self._offset : self._offset + size]
        self._offset += len(block)
        return block

    read1 = read

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fetcher(response=None, *, error=None, opened=None, **limits):
    from appliance.release_fetch import HttpsFetcher

    def opener(request, timeout=None, deadline=None):
        if opened is not None:
            opened.append(timeout)
        if error is not None:
            raise error
        return response

    return HttpsFetcher(opener=opener, **limits)


class SteppingClock:
    """A monotonic clock that moves only when the fake network delivers a block."""

    def __init__(self, step):
        self.now = 1000.0
        self.step = step

    def __call__(self):
        return self.now

    def advance(self):
        self.now += self.step


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


class BreaksOff(FakeResponse):
    """Headers arrived; the body read fails."""

    def __init__(self, error):
        super().__init__(b"")
        self._error = error

    def read(self, size=-1):
        raise self._error

    read1 = read


BODY_FAILURES = {
    "reset": ConnectionResetError(104, "reset"),
    "socket timeout": TimeoutError("timed out"),
    "incomplete": IncompleteRead(b"{"),
}


@pytest.mark.parametrize("failure", sorted(BODY_FAILURES))
def test_a_body_read_that_fails_is_a_download_failure(failure, tmp_path):
    """Its callers handle FetchError; a raw error answered the console as agent_error."""

    error = BODY_FAILURES[failure]
    with pytest.raises(FetchError) as read:
        fetcher(BreaksOff(error)).read(
            "https://releases.example.org/index.json", label="the index", max_bytes=1024
        )
    with pytest.raises(FetchError) as downloaded:
        fetcher(BreaksOff(error)).download(
            "https://releases.example.org/a.deb", tmp_path / "a.deb", label="the package",
            expected_bytes=10,
        )

    assert read.value.code == downloaded.value.code == "release_download_failed"
    assert type(error).__name__ in read.value.message


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


# --- a transfer ends on the clock, not only when the server stops --------------


def test_a_download_that_trickles_past_its_deadline_is_stopped(tmp_path):
    """The socket timeout bounds one read: a block every 20 s never trips it.

    Without a deadline over the whole transfer the package fetch, and the
    operation lock it holds, lasted as long as the server cared to send.
    """

    clock = SteppingClock(step=20)
    chunks = [b"x"] * 100 + [b""]
    response = FakeResponse(b"", chunks=chunks, on_read=clock.advance)

    with pytest.raises(FetchError) as caught:
        fetcher(response, clock=clock, download_deadline=300).download(
            "https://releases.example.org/a.deb",
            tmp_path / "a.deb",
            label="the manager package",
            expected_bytes=100,
        )

    assert caught.value.code == "release_download_timed_out"
    assert "300" in caught.value.message
    assert len(response._chunks) > 80


def test_a_document_that_trickles_past_its_deadline_is_stopped():
    clock = SteppingClock(step=20)
    response = FakeResponse(b"", chunks=[b"{"] * 100 + [b""], on_read=clock.advance)

    with pytest.raises(FetchError) as caught:
        fetcher(response, clock=clock, document_deadline=100).read(
            "https://releases.example.org/index.json", label="the index", max_bytes=1024
        )

    assert caught.value.code == "release_download_timed_out"
    assert len(response._chunks) > 90


def test_a_transfer_inside_its_deadline_is_untouched(tmp_path):
    clock = SteppingClock(step=1)
    body = b"appliance release bytes"
    response = FakeResponse(b"", chunks=[body[:10], body[10:], b""], on_read=clock.advance)

    digest = fetcher(response, clock=clock, download_deadline=300).download(
        "https://releases.example.org/a.deb",
        tmp_path / "a.deb",
        label="the manager package",
        expected_bytes=len(body),
    )

    assert digest == "sha256:" + hashlib.sha256(body).hexdigest()


def test_no_socket_wait_is_longer_than_what_is_left_of_the_deadline():
    opened = []

    fetcher(FakeResponse(b"{}"), opened=opened, document_deadline=10).read(
        "https://releases.example.org/index.json", label="the index", max_bytes=1024
    )

    assert opened and opened[0] <= 10


def test_a_plan_s_three_documents_fit_inside_the_call_that_waits_for_them():
    """A manager plan reads the index, the manifest and its signature.

    Each may outlast its deadline by one socket timeout. A client that gives up
    before the agent does strands the operation lock behind the call.
    """

    from appliance import protocol, release_fetch

    worst = 3 * (release_fetch.DOCUMENT_DEADLINE + release_fetch.CONNECT_TIMEOUT)

    assert worst < protocol.IMAGE_OPERATION_TIMEOUT


def test_the_troubleshooting_page_names_the_limits_the_fetcher_enforces():
    from pathlib import Path

    from appliance import release_fetch

    text = (Path(__file__).resolve().parents[1] / "docs/appliance/troubleshooting.md").read_text(
        encoding="utf-8"
    )
    section = text.split('## "release_download_timed_out"', 1)[1].split("\n## ", 1)[0]

    assert f"{release_fetch.DOCUMENT_DEADLINE} seconds" in section
    assert f"{release_fetch.DOWNLOAD_DEADLINE} seconds" in section


# --- the deadline ends a read http.client keeps alive inside one call ----------

SERVER_PATIENCE = 10

HEAD = b"HTTP/1.1 200 OK\r\n"
BODY = b"{}"
CHUNKED = HEAD + b"Transfer-Encoding: chunked\r\n\r\n2\r\n" + BODY + b"\r\n"
STALLS = {
    "the TLS handshake": (None, HEAD + b"Content-Length: 2\r\n\r\n{}"),
    "the response headers": (HEAD + b"X-Pad: y\r\n", b"Content-Length: 2\r\n\r\n{}"),
    "a chunk-size line": (CHUNKED + b"1;x", b"\r\n \r\n0\r\n\r\n"),
    "a chunked trailer": (CHUNKED + b"0\r\nX-Pad: y\r\n", b"\r\n"),
}
BODY_STALLS = ("a chunk-size line", "a chunked trailer")


class ManualTimer:
    """threading.Timer's shape; it fires only when a test passes the deadline."""

    def __init__(self, interval, function):
        self.interval = interval
        self.function = function
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


class ManualTimers:
    """The timer factory a fetcher is given; remembers every timer it made."""

    def __init__(self):
        self.made = []

    def __call__(self, interval, function):
        timer = ManualTimer(interval, function)
        self.made.append(timer)
        return timer

    def deadline_passes(self):
        self.made[-1].function()


@pytest.fixture(scope="module")
def tls_material(tmp_path_factory):
    """A test CA and its 127.0.0.1 certificate: ``(ca, certificate, key)``.

    Both carry key identifiers, which Python's default verification requires of
    a chain since 3.13, as a mirror's real chain does.
    """

    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    directory = tmp_path_factory.mktemp("release-fetch-tls")
    now = datetime.datetime.now(datetime.timezone.utc)

    def issued(subject, key, issuer, issuer_key, extensions):
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer)]))
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), False
            )
        )
        for extension, critical in extensions:
            builder = builder.add_extension(extension, critical)
        return builder.sign(issuer_key, hashes.SHA256())

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = issued(
        "release-fetch-test-ca",
        ca_key,
        "release-fetch-test-ca",
        ca_key,
        [
            (x509.BasicConstraints(ca=True, path_length=None), True),
            (x509.KeyUsage(False, False, False, False, False, True, True, False, False), True),
        ],
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = issued(
        "127.0.0.1",
        key,
        "release-fetch-test-ca",
        ca_key,
        [
            (x509.BasicConstraints(ca=False, path_length=None), True),
            (x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False),
            (x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False),
        ],
    )
    paths = directory / "ca.crt", directory / "server.crt", directory / "server.key"
    paths[0].write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    paths[1].write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    paths[2].write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return paths


@pytest.fixture
def trusted_tls(tls_material, monkeypatch):
    """The test CA as the default trust store, which the fetcher's own TLS reads."""

    monkeypatch.setenv("SSL_CERT_FILE", str(tls_material[0]))
    return tls_material


class LoopbackHttps:
    """One https connection on 127.0.0.1, served by ``script`` on a thread.

    The real fetcher is served here because what the deadline has to reach is
    http.client's own reading: a TLS handshake, a header, a chunk-size line or
    a chunked trailer sent a byte at a time never returns to the fetcher between
    its reads, so no clock check sees it. A server that is not cut off finishes
    its response after SERVER_PATIENCE, so a fetcher that waits fails rather
    than hangs.
    """

    def __init__(self, material, script):
        _, cert, key = material
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.settimeout(SERVER_PATIENCE)
        self.port = self.listener.getsockname()[1]
        self.released = threading.Event()
        self.patience_ran_out = False
        self._script = script
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def url(self, path="/index.json"):
        return f"https://127.0.0.1:{self.port}{path}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.released.set()
        self._thread.join(timeout=SERVER_PATIENCE)
        self.listener.close()
        return False

    def waits_for_the_fetcher(self):
        """Whether the fetcher let go before the server's patience ran out."""

        if self.released.wait(SERVER_PATIENCE):
            return True
        self.patience_ran_out = True
        return False

    def _serve(self):
        try:
            conn, _ = self.listener.accept()
        except OSError:
            return
        with conn:
            try:
                self._script(self, conn)
            except OSError:
                pass

    def tls(self, conn):
        """The connection after its handshake, with the request read off it."""

        tls = self.context.wrap_socket(conn, server_side=True)
        request = b""
        while b"\r\n\r\n" not in request:
            block = tls.recv(4096)
            if not block:
                break
            request += block
        return tls


def stalls_at(sent, rest, timers, reading=None):
    """Send ``sent``, let the deadline pass, and finish only if nobody stopped reading.

    With ``reading``, the deadline passes once the fetcher is inside the read
    that stalls, past its own check of the deadline between reads.
    """

    def script(server, conn):
        if sent is None:
            conn.recv(1, socket.MSG_PEEK)
            timers.deadline_passes()
            if not server.waits_for_the_fetcher():
                with server.tls(conn) as tls:
                    tls.sendall(rest)
            return
        with server.tls(conn) as tls:
            tls.sendall(sent)
            if reading is not None:
                reading.wait(SERVER_PATIENCE)
            timers.deadline_passes()
            if not server.waits_for_the_fetcher():
                tls.sendall(rest)

    return script


class ReadsPastTheBody:
    """The fetcher's response; ``reading`` is set as a read starts after ``BODY``.

    A server stalling the chunked framing sends ``BODY`` and then holds the
    next read inside http.client.
    """

    def __init__(self, response, reading):
        self._response = response
        self._reading = reading
        self._delivered = 0

    def read1(self, size):
        if self._delivered >= len(BODY):
            self._reading.set()
        block = self._response.read1(size)
        self._delivered += len(block)
        return block

    def __getattr__(self, name):
        return getattr(self._response, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._response.__exit__(*exc)


def stalling_reads_seen(reading):
    """The fetcher's own opener, with its response watched for the read that stalls."""

    from appliance import release_fetch

    def opener(request, **held):
        return ReadsPastTheBody(release_fetch._open_watched(request, **held), reading)

    return opener


def fetcher_for(stall, timers):
    """The real fetcher, and the event its stalling body read sets, if it has one."""

    from appliance.release_fetch import HttpsFetcher

    reading = threading.Event() if stall in BODY_STALLS else None
    opener = stalling_reads_seen(reading) if reading else None
    return HttpsFetcher(timeout=SERVER_PATIENCE * 3, timer=timers, opener=opener), reading


@pytest.mark.parametrize("stall", sorted(STALLS))
def test_a_read_the_server_keeps_alive_inside_http_client_ends_on_its_deadline(
    stall, trusted_tls
):
    """The deadline is checked between body reads, and none of these is one.

    Before the timer, a server trickling the handshake, a header, a chunk-size
    line or a trailer held the transfer, and the agent's operation lock with it,
    for as long as each byte came inside the socket timeout. The deadline passes
    while the fetcher is blocked in that read, and only shutting its connection
    down ends it before the server's patience does.
    """

    timers = ManualTimers()
    sent, rest = STALLS[stall]
    transport, reading = fetcher_for(stall, timers)

    with LoopbackHttps(trusted_tls, stalls_at(sent, rest, timers, reading)) as server:
        with pytest.raises(FetchError) as caught:
            transport.read(server.url(), label="the index", max_bytes=1024)

    assert caught.value.code == "release_download_timed_out"
    assert not server.patience_ran_out
    assert timers.made[-1].cancelled


@pytest.mark.parametrize("stall", BODY_STALLS)
def test_a_download_the_server_keeps_alive_ends_on_its_deadline(stall, trusted_tls, tmp_path):
    timers = ManualTimers()
    sent, rest = STALLS[stall]
    transport, reading = fetcher_for(stall, timers)

    with LoopbackHttps(trusted_tls, stalls_at(sent, rest, timers, reading)) as server:
        with pytest.raises(FetchError) as caught:
            transport.download(
                server.url("/a.deb"), tmp_path / "a.deb", label="the package", expected_bytes=2
            )

    assert caught.value.code == "release_download_timed_out"
    assert not server.patience_ran_out


def test_a_connection_its_deadline_does_not_hold_is_refused(trusted_tls, monkeypatch):
    """An http.client that connected past the socket factory the deadline is
    handed its sockets through would leave the transfer bounded by the server
    alone; such a response is refused rather than read."""

    from appliance import release_fetch

    def unheld(deadline, address, timeout=None, source_address=None):
        return socket.create_connection(address, timeout, source_address)

    monkeypatch.setattr(release_fetch, "_watched_socket", unheld)

    def answers(server, conn):
        with server.tls(conn) as tls:
            tls.sendall(HEAD + b"Content-Length: 2\r\n\r\n" + BODY)

    with LoopbackHttps(trusted_tls, answers) as server:
        with pytest.raises(FetchError) as caught:
            release_fetch.HttpsFetcher(timeout=SERVER_PATIENCE, timer=ManualTimers()).read(
                server.url(), label="the index", max_bytes=1024
            )

    assert caught.value.code == "release_download_failed"
    assert "deadline" in caught.value.message


@pytest.fixture
def unanswered_address():
    """A loopback address whose connect is never answered: its accept queue is full.

    A further SYN is dropped, so a connect waits out its socket timeout, as one
    to a mirror behind a firewall that drops packets does, without leaving the
    host.
    """

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(0)
    queued = socket.create_connection(listener.getsockname(), timeout=SERVER_PATIENCE)
    try:
        yield listener.getsockname()
    finally:
        queued.close()
        listener.close()


def deadline_passes_once_held(monkeypatch, timers, holds):
    """Pass the deadline on another thread once the fetcher holds ``holds`` sockets.

    A socket is held before it connects, so the deadline passes while that
    connect waits or just before it starts.
    """

    from appliance import release_fetch

    hold = release_fetch._Deadline.watch
    held = []
    reached = threading.Event()

    def watch(deadline, sock):
        left = hold(deadline, sock)
        held.append(sock)
        if len(held) == holds:
            reached.set()
        return left

    monkeypatch.setattr(release_fetch._Deadline, "watch", watch)

    def passes():
        if reached.wait(SERVER_PATIENCE):
            timers.deadline_passes()

    threading.Thread(target=passes, daemon=True).start()


def test_a_connect_never_answered_ends_on_its_deadline_for_every_address(
    unanswered_address, monkeypatch
):
    """A name resolving to two silent addresses took a full socket wait for each,
    the deadline's timer having no socket to end until one connected."""

    from appliance.release_fetch import HttpsFetcher

    resolve = socket.getaddrinfo

    def twice(name, *args, **kwargs):
        if name == "mirror.example.test":
            entry = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", unanswered_address)
            return [entry, entry]
        return resolve(name, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", twice)
    timers = ManualTimers()
    deadline_passes_once_held(monkeypatch, timers, holds=1)
    url = f"https://mirror.example.test:{unanswered_address[1]}/index.json"
    started = time.monotonic()

    with pytest.raises(FetchError) as caught:
        HttpsFetcher(timeout=SERVER_PATIENCE, timer=timers).read(
            url, label="the index", max_bytes=1024
        )

    assert caught.value.code == "release_download_timed_out"
    assert time.monotonic() - started < SERVER_PATIENCE


def test_a_redirect_s_connect_never_answered_ends_on_its_deadline(
    trusted_tls, unanswered_address, monkeypatch
):
    """A redirect's connection used to be held only once it had connected, with
    the socket wait the first request was given."""

    from appliance.release_fetch import HttpsFetcher

    host, port = unanswered_address
    target = f"https://{host}:{port}/index.json"

    def redirects(server, conn):
        with server.tls(conn) as tls:
            tls.sendall(
                b"HTTP/1.1 302 Found\r\nLocation: " + target.encode() + b"\r\n"
                b"Content-Length: 0\r\n\r\n"
            )

    timers = ManualTimers()
    deadline_passes_once_held(monkeypatch, timers, holds=2)
    started = time.monotonic()

    with LoopbackHttps(trusted_tls, redirects) as server:
        with pytest.raises(FetchError) as caught:
            HttpsFetcher(timeout=SERVER_PATIENCE, timer=timers).read(
                server.url(), label="the index", max_bytes=1024
            )

    assert caught.value.code == "release_download_timed_out"
    assert time.monotonic() - started < SERVER_PATIENCE


def test_a_deadline_passing_between_hold_and_connect_ends_the_transfer(
    unanswered_address, monkeypatch
):
    """Shutting down a socket that has not begun to connect does nothing, and on
    Linux its connect then reports success at once: only the deadline read after
    the connect ends the transfer there, not the handshake's socket wait."""

    from appliance import release_fetch

    hold = release_fetch._Deadline.watch

    def watch(deadline, sock):
        left = hold(deadline, sock)
        deadline._expire()
        return left

    monkeypatch.setattr(release_fetch._Deadline, "watch", watch)
    host, port = unanswered_address
    started = time.monotonic()

    with pytest.raises(FetchError) as caught:
        release_fetch.HttpsFetcher(timeout=SERVER_PATIENCE, timer=ManualTimers()).read(
            f"https://{host}:{port}/index.json", label="the index", max_bytes=1024
        )

    assert caught.value.code == "release_download_timed_out"
    assert time.monotonic() - started < SERVER_PATIENCE / 2


def test_a_mirror_whose_first_address_refuses_is_reached_on_the_next(
    trusted_tls, monkeypatch
):
    """Every address a name resolves to is tried in turn, as create_connection
    does: a dead first address does not fail a download the next one serves."""

    from appliance.release_fetch import HttpsFetcher

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    refused = probe.getsockname()
    probe.close()

    def answers(server, conn):
        with server.tls(conn) as tls:
            tls.sendall(HEAD + b"Content-Length: 2\r\n\r\n" + BODY)

    with LoopbackHttps(trusted_tls, answers) as server:
        resolve = socket.getaddrinfo
        served = ("127.0.0.1", server.port)

        def refused_first(name, port, *args, **kwargs):
            if (name, port) == served:
                entry = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "")
                return [entry + (refused,), entry + (served,)]
            return resolve(name, port, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", refused_first)
        body = HttpsFetcher(timeout=SERVER_PATIENCE, timer=ManualTimers()).read(
            server.url(), label="the index", max_bytes=1024
        )

    assert body == BODY


def test_the_fetcher_still_verifies_the_server_certificate(tls_material):
    """Its own TLS reads the system trust store, which here lacks the test CA."""

    from appliance.release_fetch import HttpsFetcher

    def answers(server, conn):
        with server.tls(conn) as tls:
            tls.sendall(HEAD + b"Content-Length: 2\r\n\r\n{}")

    with LoopbackHttps(tls_material, answers) as server:
        with pytest.raises(FetchError) as caught:
            HttpsFetcher(timeout=SERVER_PATIENCE, timer=ManualTimers()).read(
                server.url(), label="the index", max_bytes=1024
            )

    assert caught.value.code == "release_download_failed"


def test_a_complete_read_over_tls_returns_the_body_and_cancels_its_timer(trusted_tls):
    from appliance.release_fetch import HttpsFetcher

    timers = ManualTimers()

    def answers(server, conn):
        with server.tls(conn) as tls:
            tls.sendall(CHUNKED + b"0\r\n\r\n")

    with LoopbackHttps(trusted_tls, answers) as server:
        body = HttpsFetcher(timeout=SERVER_PATIENCE, document_deadline=7, timer=timers).read(
            server.url(), label="the index", max_bytes=1024
        )

    assert body == b"{}"
    assert [(timer.interval, timer.started, timer.cancelled) for timer in timers.made] == [
        (7.0, True, True)
    ]


def test_a_redirect_to_http_is_refused_before_it_is_followed(trusted_tls):
    """Only an https connection is held to the deadline, so no other is opened."""

    from appliance.release_fetch import HttpsFetcher

    plain = socket.create_server(("127.0.0.1", 0))
    plain.setblocking(False)
    target = f"http://127.0.0.1:{plain.getsockname()[1]}/index.json"

    def redirects(server, conn):
        with server.tls(conn) as tls:
            tls.sendall(
                b"HTTP/1.1 302 Found\r\nLocation: " + target.encode() + b"\r\n"
                b"Content-Length: 0\r\n\r\n"
            )

    try:
        with LoopbackHttps(trusted_tls, redirects) as server:
            with pytest.raises(FetchError) as caught:
                HttpsFetcher(timeout=SERVER_PATIENCE, timer=ManualTimers()).read(
                    server.url(), label="the index", max_bytes=1024
                )
        with pytest.raises(BlockingIOError):
            plain.accept()
    finally:
        plain.close()

    assert caught.value.code == "release_source_insecure"


# --- the image build reads through the same transport -------------------------


def test_the_image_build_s_read_ends_on_a_deadline(trusted_tls, monkeypatch):
    """``scripts/appliance-fetch-manager-package.py`` gave each socket wait 30 s
    and nothing bounded a transfer, so a mirror trickling the index, a manifest
    or the package held the image build for as long as it cared to send.

    Interleaving: the response head and part of the body have arrived when the
    deadline passes, while the build reads the rest; it stops there, before the
    server's patience runs out and it sends the rest.
    """

    from tests.test_appliance_fetch_manager_package import fetcher

    timers = ManualTimers()
    monkeypatch.setattr(threading, "Timer", timers)

    def trickles(server, conn):
        with server.tls(conn) as tls:
            tls.sendall(HEAD + b"Content-Length: 4\r\n\r\n" + BODY)
            if timers.made:
                timers.deadline_passes()
            if not server.waits_for_the_fetcher():
                tls.sendall(BODY)

    with LoopbackHttps(trusted_tls, trickles) as server:
        with pytest.raises(OSError) as stopped:
            fetcher.fetch(server.url(), limit=1024, label="the index")

    assert "release_download_timed_out" in str(stopped.value)
    assert not server.patience_ran_out


def test_the_image_build_still_falls_back_when_no_index_is_published(trusted_tls, tmp_path):
    """Exit 3 is "no Manager release yet", read from the index's 404."""

    from tests.test_appliance_fetch_manager_package import fetcher

    def not_published(server, conn):
        with server.tls(conn) as tls:
            tls.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")

    keyring = tmp_path / "keyring.gpg"
    keyring.write_bytes(b"")
    with LoopbackHttps(trusted_tls, not_published) as server:
        status = fetcher.main(
            ["--index", server.url(), "--into", str(tmp_path / "into"), "--keyring", str(keyring)]
        )

    assert status == 3
