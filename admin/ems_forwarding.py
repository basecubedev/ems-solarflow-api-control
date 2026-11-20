# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The EMS dashboard's API, forwarded from the Admin console's own address.

The Admin console serves the one browser app, the dashboard's pages as well as
its own, so the browser asks a single origin for everything. A path the EMS
answers (``dashboard.api_paths``) is passed through to the EMS's dashboard
server on this host, and its answer back, unchanged in what it decides:

- no authority of its own. The EMS checks its session cookie and CSRF token as
  it always does; the Admin session plays no part, and nothing here signs a
  request in.
- the browser never holds the EMS's session. The console keeps it, behind a
  link cookie of its own making (:class:`EmsSessions`), new at every sign-in,
  and hands the EMS its session in the link's place.
- an allow-list of headers each way, so neither side sees what the other did
  not mean to send it; the Admin's own security headers go on every answer.
- the live stream (``/api/events``) is passed on as it arrives, not buffered.
- the client is named to the EMS (``dashboard.forwarding``), so its per-client
  limits stay per client.

The EMS is found the way the EMS reads its own settings: the port and the
HTTPS switch of ``dashboard`` in the installed ``config.json``. It answers on
this host's loopback, where the Admin's host network reaches the port the EMS
container publishes; an Admin in a Docker bridge network reaches the host at
its default gateway (``dashboard.host_reach``), or at the host
``EMS_ADMIN_EMS_HOST`` names. A refusal says why in its own code, so the app
can tell an EMS that is not set up from one that does not answer.
"""

import http.client
import json
import os
import secrets
import ssl
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from dashboard.auth import SESSION_COOKIE_NAME
from dashboard.console_address import publish_console_address
from dashboard.forwarding import (
    FORWARDED_FOR_HEADER,
    FORWARDING_KEY_HEADER,
    FORWARDING_KEY_NAME,
    ForwardingKey,
)
from dashboard.host_reach import first_answering, host_candidates, in_bridge_network
from ems.config import normalize_dashboard_config, safe_bool, safe_int

EMS_HOST_ENV = "EMS_ADMIN_EMS_HOST"
LOOPBACK_EMS_HOST = "127.0.0.1"
DEFAULT_EMS_PORT = 8080
DEFAULT_CERT_FILE = "config/dashboard.crt"
EMS_CONTAINER_ROOT = "/app/"
MAX_FORWARD_BODY_BYTES = 1024 * 1024
MAX_DISCARDED_BYTES = 4 * MAX_FORWARD_BODY_BYTES
CONNECT_TIMEOUT_SECONDS = 3.0
READ_TIMEOUT_SECONDS = 120.0
STREAM_TIMEOUT_SECONDS = 60.0
STREAM_PATHS = frozenset({"/api/events"})
CHUNK_BYTES = 64 * 1024

REQUEST_HEADERS = (
    "Accept",
    "Accept-Language",
    "Cache-Control",
    "Content-Type",
    "Last-Event-ID",
    "X-CSRF-Token",
)
RESPONSE_HEADERS = (
    "Cache-Control",
    "Content-Disposition",
    "Content-Length",
    "Content-Type",
    "ETag",
    "Pragma",
    "Retry-After",
)


class ForwardingRefusal(Exception):
    """Why a request could not be passed to the EMS: a code and its HTTP status."""

    def __init__(self, code, status, message):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message


@dataclass(frozen=True)
class EmsUpstream:
    """Where the EMS's dashboard server answers on this host."""

    port: int
    https: bool = False
    cafile: Path | None = None


HOST_RECHECK_SECONDS = 60.0
PROOF_TIMEOUT_SECONDS = 1.0
_REACHED = {}
_REACHED_LOCK = threading.Lock()


def ems_host(upstream=None):
    """The host the EMS's published port answers on, as this Admin's network reaches it.

    The host ``EMS_ADMIN_EMS_HOST`` names, else this host's loopback when it
    takes a connection, else, in a bridge network, the default gateway once it
    has answered ``/api/ui-config`` as the EMS does (over HTTPS, with the EMS's
    own certificate): nothing that carries a password or a session goes to a
    host before it proved to be the EMS. A host found is kept a minute; when
    none is found the loopback is used and nothing kept, so an EMS that was
    restarting is found again at the next request.
    """

    configured = os.environ.get(EMS_HOST_ENV, "").strip()
    if configured or upstream is None:
        return configured or LOOPBACK_EMS_HOST
    port = upstream.port
    now = time.monotonic()
    with _REACHED_LOCK:
        known = _REACHED.get(port)
        if known and known[0] > now:
            return known[1]
    host = next((candidate for candidate in host_candidates() if _answers_as_ems(candidate, upstream)), None)
    if host is None:
        return LOOPBACK_EMS_HOST
    with _REACHED_LOCK:
        _REACHED[port] = (now + HOST_RECHECK_SECONDS, host)
    return host


def _answers_as_ems(host, upstream):
    """Whether ``host`` is the EMS: the loopback by taking a connection, any other by the EMS's own answer."""

    if host == LOOPBACK_EMS_HOST:
        return first_answering(upstream.port, [host]) is not None
    try:
        if upstream.https:
            connection = http.client.HTTPSConnection(host, upstream.port, timeout=PROOF_TIMEOUT_SECONDS, context=_tls_context(upstream.cafile))
        else:
            connection = http.client.HTTPConnection(host, upstream.port, timeout=PROOF_TIMEOUT_SECONDS)
    except (OSError, ssl.SSLError):
        return False
    try:
        connection.request("GET", "/api/ui-config")
        response = connection.getresponse()
        body = response.read(4096)
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()
    if response.status != 200:
        return False
    try:
        payload = json.loads(body)
    except ValueError:
        return False
    return isinstance(payload, dict) and "animation_mode" in payload and "admin_console" in payload


def shares_host_network():
    """Whether this Admin's ports are the host's: ``EMS_ADMIN_EMS_HOST`` names the loopback, or nothing and the Admin is not in a bridge network."""

    configured = os.environ.get(EMS_HOST_ENV, "").strip()
    if configured:
        return configured in ("127.0.0.1", "localhost", "::1")
    return not in_bridge_network()


def ems_upstream(context):
    """The installed EMS's dashboard server, or a refusal naming why there is none.

    ``dashboard`` is read as the EMS reads it at its start: its defaults
    filled in, the switches and the port parsed by the same helpers.
    """

    if not getattr(context, "config_exists", False):
        raise ForwardingRefusal("ems_not_configured", 503, "The EMS is not set up yet.")
    try:
        config = json.loads(Path(context.config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        raise ForwardingRefusal("ems_config_unreadable", 503, "The EMS configuration cannot be read.")
    dashboard = normalize_dashboard_config(config.get("dashboard") if isinstance(config, dict) else None)
    if not safe_bool(dashboard.get("enabled"), False):
        raise ForwardingRefusal("ems_dashboard_disabled", 503, "The EMS dashboard is switched off.")
    port = safe_int(dashboard.get("port"), DEFAULT_EMS_PORT, minimum=1)
    if port > 65535:
        port = DEFAULT_EMS_PORT
    if not safe_bool(dashboard.get("ssl_enabled"), False):
        return EmsUpstream(port=port)
    cert = str(dashboard.get("ssl_cert_file") or DEFAULT_CERT_FILE)
    if cert.startswith(EMS_CONTAINER_ROOT):
        cert = cert[len(EMS_CONTAINER_ROOT):]
    cafile = Path(cert) if Path(cert).is_absolute() else Path(context.install_root) / cert
    return EmsUpstream(port=port, https=True, cafile=cafile)


_KEYS = {}
_KEYS_LOCK = threading.Lock()


def forwarding_key_for(context):
    """The installation's forwarding key file beside its ``config.json``, one reader per file."""

    path = os.fspath(Path(context.config_path).parent / FORWARDING_KEY_NAME)
    with _KEYS_LOCK:
        return _KEYS.setdefault(path, ForwardingKey(path))


def installed_ems(console=None):
    """The installed EMS's upstream and the forwarding key, from this host's install context.

    ``console`` is this console's own address record
    (``dashboard.console_address``); it is published for the EMS's dashboard
    while the EMS is there to read it.
    """

    from admin.install_context import detect_install_context

    context = detect_install_context()
    upstream = ems_upstream(context)
    if console is not None:
        publish_console_address(Path(context.config_path).parent, console)
    return upstream, forwarding_key_for(context).ensure()


def secure_cookie(value, https_active):
    """A ``Set-Cookie`` value whose ``Secure`` attribute follows the browser's connection.

    The EMS sets ``Secure`` when its own listener is HTTPS; the browser reaches
    the cookie through the Admin's listener, which decides whether it may keep it.
    """

    parts = [part.strip() for part in value.split(";")]
    kept = [part for part in parts if part and part.lower() != "secure"]
    if https_active:
        kept.append("Secure")
    return "; ".join(kept)


def one_line(value):
    """A header value as one line: a line break in it could start a header of its own."""

    return str(value).replace("\r", " ").replace("\n", " ")


LINK_COOKIE_NAME = "ems_console_link"
LINK_COOKIE_ATTRIBUTES = "Path=/; HttpOnly; SameSite=Strict"
MAX_LINKED_SESSIONS = 256
LINK_ENDED = object()


def cookie_value(header, name):
    """The value of cookie ``name`` in a ``Cookie`` header, or ``None``."""

    for part in (header or "").split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key == name:
            return value
    return None


class EmsSessions:
    """The EMS sessions this console holds for its browsers, each behind a link of its own making.

    The EMS sets its session cookie on its sign-in and ends it on its
    sign-out. The console keeps that session here and gives the browser a
    link cookie instead, a fresh random value at every session the EMS sets,
    with the console's own attributes; nothing the EMS answers becomes a
    cookie in the browser, and the EMS's session never leaves this host. The
    newest :data:`MAX_LINKED_SESSIONS` links are kept; a console restart
    forgets them, as it forgets its own sessions.
    """

    def __init__(self, limit=MAX_LINKED_SESSIONS):
        self._limit = limit
        self._lock = threading.Lock()
        self._sessions = OrderedDict()

    def session_for(self, cookie_header):
        """The EMS session the browser's link names, or ``None``."""

        link = cookie_value(cookie_header, LINK_COOKIE_NAME)
        if not link:
            return None
        with self._lock:
            return self._sessions.get(link)

    def adopt(self, cookie_header, set_cookies):
        """Take the EMS's answer about its session: a new link to set, :data:`LINK_ENDED`, or ``None`` when it said nothing.

        The browser's previous link is dropped either way, so a link outlives
        neither a new sign-in nor a sign-out.
        """

        said = False
        session = None
        for cookie in set_cookies:
            name, _, rest = one_line(cookie).partition("=")
            if name.strip() != SESSION_COOKIE_NAME:
                continue
            said = True
            value, _, attributes = rest.partition(";")
            ended = not value.strip() or "max-age=0" in attributes.lower().replace(" ", "")
            session = None if ended else value.strip()
        if not said:
            return None
        previous = cookie_value(cookie_header, LINK_COOKIE_NAME)
        with self._lock:
            if previous:
                self._sessions.pop(previous, None)
            if session is None:
                return LINK_ENDED
            link = secrets.token_urlsafe(32)
            self._sessions[link] = session
            while len(self._sessions) > self._limit:
                self._sessions.popitem(last=False)
        return link


EMS_SESSIONS = EmsSessions()


def link_cookie(link, https_active):
    """The ``Set-Cookie`` value for a link :meth:`EmsSessions.adopt` made, or for ending it."""

    if link is LINK_ENDED:
        return secure_cookie(f"{LINK_COOKIE_NAME}=; Max-Age=0; {LINK_COOKIE_ATTRIBUTES}", https_active)
    return secure_cookie(f"{LINK_COOKIE_NAME}={link}; {LINK_COOKIE_ATTRIBUTES}", https_active)


def _tls_context(cafile):
    context = ssl.create_default_context(cafile=str(cafile))
    context.check_hostname = False
    context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    context.verify_flags |= getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    return context


def _connection(upstream, timeout):
    if not upstream.https:
        return http.client.HTTPConnection(ems_host(upstream), upstream.port, timeout=timeout)
    try:
        context = _tls_context(upstream.cafile)
    except (OSError, ssl.SSLError):
        raise ForwardingRefusal("ems_certificate_unreadable", 502, "The EMS dashboard's certificate cannot be read.")
    return http.client.HTTPSConnection(ems_host(upstream), upstream.port, timeout=timeout, context=context)


def _request_target(handler):
    """The request's path and query as sent, refused unless it is printable ASCII."""

    target = handler.path
    if not target.startswith("/") or not target.isascii() or not target.isprintable() or " " in target:
        raise ForwardingRefusal("bad_request", 400, "The request path is not valid.")
    return target


def _request_body(handler):
    if handler.headers.get("Transfer-Encoding"):
        raise ForwardingRefusal("length_required", 411, "A request body needs a Content-Length.")
    raw = handler.headers.get("Content-Length")
    if raw is None:
        return None
    try:
        length = int(raw)
    except ValueError:
        raise ForwardingRefusal("bad_request", 400, "Content-Length must be an integer.")
    if length < 0:
        raise ForwardingRefusal("bad_request", 400, "Content-Length must be >= 0.")
    if length > MAX_FORWARD_BODY_BYTES:
        _discard(handler.rfile, min(length, MAX_DISCARDED_BYTES))
        raise ForwardingRefusal("request_too_large", 413, "The request body is too large.")
    return handler.rfile.read(length) if length else b""


def _discard(stream, length):
    """Read and drop up to ``length`` bytes, so the browser still reads the refusal it is sent."""

    left = length
    try:
        while left > 0:
            chunk = stream.read(min(CHUNK_BYTES, left))
            if not chunk:
                return
            left -= len(chunk)
    except OSError:
        return


def _request_headers(handler, client, key, sessions):
    headers = {name: handler.headers[name] for name in REQUEST_HEADERS if handler.headers.get(name) is not None}
    session = sessions.session_for(handler.headers.get("Cookie"))
    if session:
        headers["Cookie"] = f"{SESSION_COOKIE_NAME}={session}"
    headers[FORWARDED_FOR_HEADER] = client
    headers["X-Forwarded-Proto"] = "https" if getattr(handler.server, "https_active", False) else "http"
    host = handler.headers.get("Host")
    if host:
        headers["X-Forwarded-Host"] = host
    if key:
        headers[FORWARDING_KEY_HEADER] = key
    return headers


def forward(handler, upstream, *, key, security_headers, sessions=EMS_SESSIONS):
    """Pass the handler's request to the EMS and its answer back to the browser.

    Raises :class:`ForwardingRefusal` before anything is written to the
    browser; once the answer has started, a broken stream just ends it.
    """

    target = _request_target(handler)
    streaming = target.split("?", 1)[0] in STREAM_PATHS
    body = _request_body(handler)
    client = handler.client_address[0] if handler.client_address else "unknown"
    connection = _connection(upstream, CONNECT_TIMEOUT_SECONDS)
    try:
        try:
            connection.request(handler.command, target, body=body, headers=_request_headers(handler, client, key, sessions))
            connection.sock.settimeout(STREAM_TIMEOUT_SECONDS if streaming else READ_TIMEOUT_SECONDS)
            response = connection.getresponse()
        except ssl.SSLError:
            raise ForwardingRefusal("ems_certificate_mismatch", 502, "The EMS dashboard's certificate does not match.")
        except TimeoutError:
            raise ForwardingRefusal("ems_timeout", 504, "The EMS did not answer in time.")
        except (OSError, http.client.HTTPException):
            raise ForwardingRefusal("ems_unreachable", 502, "The EMS does not answer.")
        _relay(handler, response, security_headers, sessions)
    finally:
        connection.close()


def _relay(handler, response, security_headers, sessions):
    handler.send_response(response.status, response.reason)
    for name, value in security_headers.items():
        handler.send_header(name, value)
    for name in RESPONSE_HEADERS:
        value = response.getheader(name)
        if value is not None:
            handler.send_header(name, one_line(value))
    link = sessions.adopt(handler.headers.get("Cookie"), response.msg.get_all("Set-Cookie") or [])
    if link is not None:
        handler.send_header("Set-Cookie", link_cookie(link, getattr(handler.server, "https_active", False)))
    if response.getheader("Content-Length") is None:
        handler.send_header("Connection", "close")
        handler.close_connection = True
    try:
        handler.end_headers()
    except OSError:
        handler.close_connection = True
        return
    if handler.command == "HEAD":
        return
    try:
        while True:
            chunk = response.read1(CHUNK_BYTES)
            if not chunk:
                break
            handler.wfile.write(chunk)
            handler.wfile.flush()
    except (OSError, http.client.HTTPException):
        handler.close_connection = True
