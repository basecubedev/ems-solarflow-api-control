# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bring a signed release onto the appliance over HTTPS.

This module is the transport and nothing else. It does not decide what is
trustworthy -- ``artifact_trust`` owns that, with a detached-signature chain
the transport never gets to shortcut.

The order is the security property, and it is the whole point:

1. the index is fetched and may name candidates -- it is never trusted,
2. the manifest and its detached signature are fetched into a staging
   directory,
3. the signature is verified against the root-owned keyring,
4. only then is the manifest's declared file name, size and digest read,
5. the file is fetched under exactly that declared size and hashed,
6. the digest is compared against the verified manifest,
7. only then is anything installed.

An index that lies can therefore waste bandwidth and nothing else. Reading the
digest before the signature was checked would make the index the authority,
which is precisely the mistake this ordering exists to prevent.

The appliance has no real-time clock. TLS certificate validity and signature
validity are both judged against the system clock, so a fetch before the clock
is synchronised fails for reasons that look like anything but a wrong clock.
That is checked first, and a clock this cannot confirm counts as unsynchronised.
"""

import functools
import hashlib
import http.client
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from appliance import artifact_trust
from appliance.version import same_version, version_key


INDEX_FORMAT_VERSION = 1

MAX_INDEX_BYTES = 1024 * 1024
MAX_SIGNATURE_BYTES = 64 * 1024

CONNECT_TIMEOUT = 30
CHUNK = 1024 * 1024
# Wall-clock limits for one transfer: an index, a manifest or a signature, and
# a package. A manager plan reads three documents inside a call the console
# waits for, and a test keeps them inside it.
DOCUMENT_DEADLINE = 120
DOWNLOAD_DEADLINE = 900


class FetchError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def https_url(value, *, label):
    """An absolute https URL, or a refusal naming which one was not.

    http is refused rather than upgraded: an operator who configured a plain
    URL should be told, not silently given a different one than they wrote.
    """

    text = str(value or "").strip()
    if not text:
        raise FetchError("release_source_unconfigured", f"no {label} is configured")
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme != "https":
        raise FetchError(
            "release_source_insecure",
            f"the {label} is {parsed.scheme or 'relative'}, and only https is accepted",
        )
    if not parsed.netloc:
        raise FetchError("release_source_invalid", f"the {label} names no host")
    return text


class _Deadline:
    """One transfer's wall-clock limit, and the connections it ends.

    Checked between body reads on a monotonic clock. A read http.client keeps
    alive inside one call -- a connect, a TLS handshake, a header, a chunk-size
    line or a chunked trailer sent a byte at a time -- never returns to be
    checked, so a timer at the same limit shuts down every connection the
    transfer opened, and whatever that read raises or returns then is the
    deadline's. ``held`` counts the sockets handed over.
    """

    def __init__(self, seconds, clock, label, timer):
        self._clock = clock
        self._seconds = seconds
        self._label = label
        self._at = clock() + max(float(seconds), 0.0)
        self._lock = threading.Lock()
        self._connections = []
        self._expired = False
        self.held = 0
        self._timer = timer(max(float(seconds), 0.0), self._expire)
        self._timer.daemon = True

    def __enter__(self):
        self._timer.start()
        return self

    def __exit__(self, *exc):
        self._timer.cancel()
        with self._lock:
            connections, self._connections = self._connections, []
        for connection in connections:
            connection.close()
        return False

    def remaining(self):
        left = self._at - self._clock()
        if self._expired or left <= 0:
            raise FetchError(
                "release_download_timed_out",
                f"{self._label} did not arrive within the {self._seconds:g} seconds "
                "this appliance allows",
            )
        return left

    def watch(self, sock):
        """Hold ``sock``'s connection to the deadline; the seconds left for it.

        A duplicate of the socket is kept: shutting it down ends the connection
        whichever object reads it, a TLS wrapper included, which takes over the
        original. A socket handed over after the deadline is refused.
        """

        twin = sock.dup()
        with self._lock:
            held = not self._expired
            if held:
                self._connections.append(twin)
                self.held += 1
        if not held:
            twin.close()
        return self.remaining()

    def _expire(self):
        with self._lock:
            self._expired = True
            connections, self._connections = self._connections, []
        for connection in connections:
            _end(connection)


def _end(connection):
    """Shut a connection down, waking any read blocked on it, and close it."""

    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    connection.close()


def _watched_socket(deadline, address, timeout=None, source_address=None):
    """``socket.create_connection``, each socket held to ``deadline`` before it connects.

    Every address the name resolves to is tried in turn, as there. The deadline
    can end each connect, none waits longer than what is left of it, and one
    that completes after the deadline is not used.
    """

    host, port = address
    failure = OSError(f"{host} resolves to no address")
    for family, kind, proto, _, target in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
        try:
            sock = socket.socket(family, kind, proto)
            return _held_connect(deadline, sock, target, timeout, source_address)
        except OSError as exc:
            failure = exc
    raise failure


def _held_connect(deadline, sock, target, timeout, source_address):
    """``sock`` connected to ``target``, held to ``deadline`` before it connects."""

    try:
        left = deadline.watch(sock)
        sock.settimeout(min(left, timeout) if isinstance(timeout, (int, float)) else left)
        if source_address:
            sock.bind(source_address)
        sock.connect(target)
        deadline.remaining()
    except BaseException:
        sock.close()
        raise
    return sock


class _InsecureRedirect(Exception):
    """A redirect to anything but https, refused before it is followed."""


class _UnheldConnection(Exception):
    """A connection that opened without handing its socket to the deadline."""


def _same_origin(first, second):
    """Whether two URLs share scheme, host and port, the scope of a credential."""

    try:
        return _origin(first) == _origin(second)
    except ValueError:
        return False


def _origin(url):
    parts = urllib.parse.urlsplit(url)
    return parts.scheme, parts.hostname, parts.port or {"https": 443, "http": 80}.get(parts.scheme)


class _HttpsRedirectsOnly(urllib.request.HTTPRedirectHandler):
    """A redirect is followed only to https, and refused before it is opened.

    urllib copies every request header onto a redirect, whatever host it names;
    an ``Authorization`` header goes along only to the same origin.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            fp.close()
            raise _InsecureRedirect(newurl)
        follow = super().redirect_request(req, fp, code, msg, headers, newurl)
        if follow is not None and not _same_origin(newurl, req.full_url):
            follow.remove_header("Authorization")
        return follow


class _WatchedHttpsHandler(urllib.request.HTTPSHandler):
    """Opens each https connection with its socket held to ``deadline`` first.

    ``_create_connection`` is the socket factory http.client connects through,
    so each socket is held before it connects, and so before the TLS handshake,
    a proxy tunnel or the request touches it. A connection that opened without
    handing a socket over -- an http.client that no longer connects through
    that factory -- is refused before its body is read; its TLS handshake and
    response head, read by then, were bounded by the server alone. The TLS context is
    the default one: the system trust store, the certificate chain and the
    host name verified.
    """

    def __init__(self, deadline):
        self._tls = ssl.create_default_context()
        super().__init__(context=self._tls)
        self._deadline = deadline

    def https_open(self, req):
        held = self._deadline.held
        response = self.do_open(self._connection, req, context=self._tls)
        if self._deadline.held == held:
            response.close()
            raise _UnheldConnection(req.full_url)
        return response

    def _connection(self, *args, **kwargs):
        connection = http.client.HTTPSConnection(*args, **kwargs)
        connection._create_connection = functools.partial(_watched_socket, self._deadline)
        return connection


def _open_watched(request, *, timeout, deadline):
    """``urlopen`` whose every connection the transfer's deadline can end."""

    opener = urllib.request.build_opener(_WatchedHttpsHandler(deadline), _HttpsRedirectsOnly())
    return opener.open(request, timeout=timeout)


class HttpsFetcher:
    """Read a URL, bounded, over https only — including after a redirect.

    Bounded in time as well as in size. A socket timeout bounds one wait, not a
    transfer: a server sending a block every few seconds never trips it, nor
    one sending the TLS handshake, the response headers or a chunked body's
    framing a byte at a time, which http.client reads inside a single call. So
    each transfer has a deadline, checked before every read of the body, and a
    timer at the deadline shuts down every connection the transfer opened --
    each held before it connects, a redirect's included, and redirects are
    followed to https only. The transfer ends at its deadline as
    ``release_download_timed_out``.
    """

    def __init__(
        self,
        *,
        opener=None,
        timeout=CONNECT_TIMEOUT,
        document_deadline=DOCUMENT_DEADLINE,
        download_deadline=DOWNLOAD_DEADLINE,
        clock=None,
        timer=None,
    ):
        self._open = opener or _open_watched
        self.timeout = timeout
        self.document_deadline = document_deadline
        self.download_deadline = download_deadline
        self._clock = clock or time.monotonic
        self._timer = timer or threading.Timer

    def _deadline(self, seconds, label):
        return _Deadline(seconds, self._clock, label, self._timer)

    def _response(self, url, label, deadline):
        request = urllib.request.Request(https_url(url, label=label))
        wait = min(self.timeout, deadline.remaining())
        try:
            return self._open(request, timeout=wait, deadline=deadline)
        except _InsecureRedirect:
            raise FetchError(
                "release_source_insecure", f"{label} redirected to a non-https location"
            ) from None
        except _UnheldConnection:
            raise FetchError(
                "release_download_failed",
                f"{label} came over a connection its deadline could not end",
            ) from None
        except urllib.error.HTTPError as exc:
            raise FetchError(
                "release_download_failed", f"{label} answered HTTP {exc.code}"
            )
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
            deadline.remaining()
            raise FetchError(
                "release_download_failed",
                f"{label} is unreachable: {exc.__class__.__name__}",
            )

    @staticmethod
    def _still_https(response, label):
        # urllib follows redirects on its own, so the scheme has to be checked
        # where the bytes actually came from and not only where they were asked
        # for. A redirect to http is a downgrade, not a detail.
        final = getattr(response, "url", "") or ""
        if final and urllib.parse.urlsplit(final).scheme != "https":
            raise FetchError(
                "release_source_insecure",
                f"{label} redirected to a non-https location",
            )

    @staticmethod
    def _blocks(response, deadline, label):
        """The body as it arrives, until the server ends it or the deadline passes.

        A body that breaks off is a failed download like an unreachable host:
        every caller handles FetchError, and nothing else.
        """

        while True:
            deadline.remaining()
            try:
                block = response.read1(CHUNK)
            except (OSError, http.client.HTTPException) as exc:
                deadline.remaining()
                raise FetchError(
                    "release_download_failed",
                    f"{label} broke off: {exc.__class__.__name__}",
                ) from None
            if not block:
                deadline.remaining()
                return
            yield block

    def read(self, url, *, label, max_bytes):
        payload = bytearray()
        with self._deadline(self.document_deadline, label) as deadline:
            with self._response(url, label, deadline) as response:
                self._still_https(response, label)
                for block in self._blocks(response, deadline, label):
                    payload += block
                    if len(payload) > max_bytes:
                        raise FetchError(
                            "release_download_too_large",
                            f"{label} is larger than the {max_bytes} byte limit this "
                            "appliance accepts",
                        )
        return bytes(payload)

    def download(self, url, destination, *, label, expected_bytes):
        """Stream to ``destination``, hashing, refusing anything oversized."""

        digest = hashlib.sha256()
        written = 0
        with self._deadline(self.download_deadline, label) as deadline:
            with self._response(url, label, deadline) as response:
                self._still_https(response, label)
                with open(destination, "wb") as handle:
                    for block in self._blocks(response, deadline, label):
                        written += len(block)
                        if written > expected_bytes:
                            raise FetchError(
                                "release_download_too_large",
                                f"{label} sends more than the {expected_bytes} bytes its "
                                "verified manifest declares",
                            )
                        digest.update(block)
                        handle.write(block)
                    handle.flush()
                    os.fsync(handle.fileno())
        if written != expected_bytes:
            raise FetchError(
                "release_download_truncated",
                f"{label} ended after {written} of {expected_bytes} declared bytes",
            )
        return f"sha256:{digest.hexdigest()}"


# What an index entry may say about a release beyond where to fetch it. None of
# it is trusted: it exists so an operator choosing between several published
# releases has something to choose by, rather than a column of opaque
# identifiers. The signed manifest remains the only authority. The one way a
# claim reaches a plan is as a refusal -- a release_version the signed manifest
# contradicts blocks the install the page offered under that name.
DESCRIPTION_FIELDS = ("release_version", "created_at", "build_id", "board")

MAX_DESCRIPTION = 64


def _description(entry):
    """The entry's own account of itself, bounded and stringified."""

    described = {}
    for field in DESCRIPTION_FIELDS:
        value = entry.get(field)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            text = str(value).strip()
            if text:
                described[field] = text[:MAX_DESCRIPTION]
    return described


def sort_key(entry):
    """Newest first, by what the entry claims, with the id as the tiebreak.

    An index that says nothing about its releases still sorts deterministically;
    it just sorts by identifier, which is the state this had before.
    """

    return (
        version_key(entry.get("release_version") or ""),
        str(entry.get("created_at") or ""),
        str(entry.get("build_id") or ""),
        str(entry.get("release_id") or ""),
    )


def claim_matches(claimed, signed):
    """Whether an index's release_version is the version its signed manifest names.

    Exact but for a ``v`` prefix, for the appliance and the image build alike.
    ``_description`` cuts a claim to MAX_DESCRIPTION, so a signed version longer
    than that never matches its claim: installing it through the index is
    refused, deliberately, rather than matched by a prefix.
    """

    return same_version(claimed, signed, package=True)


def parse_index(payload):
    """Candidate releases an index names. Nothing here is trusted.

    Every entry is checked for shape only — a release id this appliance would
    accept and three https URLs. What the entry claims about the release never
    permits an install, because the signed manifest is what decides; the claims
    label the choice, and a claimed version the manifest contradicts blocks it.
    """

    if not isinstance(payload, dict):
        raise FetchError("release_index_invalid", "the release index is not an object")
    version = payload.get("format_version")
    if version != INDEX_FORMAT_VERSION:
        raise FetchError(
            "release_index_unsupported",
            f"the release index is format {version or 'unknown'}, this appliance reads "
            f"format {INDEX_FORMAT_VERSION}",
        )
    entries = payload.get("releases")
    if not isinstance(entries, list):
        raise FetchError("release_index_invalid", "the release index lists no releases")

    candidates = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            release_id = artifact_trust.validate_release_id(entry.get("release_id"))
            manifest_url = https_url(entry.get("manifest_url"), label="the manifest url")
            signature_url = https_url(entry.get("signature_url"), label="the signature url")
            archive_url = https_url(entry.get("archive_url"), label="the archive url")
        except (FetchError, artifact_trust.ReleaseError):
            # A malformed entry is skipped, never repaired by guessing: the
            # index is a suggestion and one bad row does not poison the rest.
            continue
        if release_id in seen:
            continue
        seen.add(release_id)
        candidates.append(
            {
                "release_id": release_id,
                "manifest_url": manifest_url,
                "signature_url": signature_url,
                "archive_url": archive_url,
                "described": _description(entry),
            }
        )
    candidates.sort(key=lambda item: sort_key({**item, **item["described"]}), reverse=True)
    return candidates


__all__ = [
    "FetchError",
    "HttpsFetcher",
    "claim_matches",
    "https_url",
    "parse_index",
]
