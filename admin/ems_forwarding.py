# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The EMS dashboard's API, forwarded from the Admin console's own address.

The Admin console serves the one browser app, the dashboard's pages as well as
its own, so the browser asks a single origin for everything. A path the EMS
answers (``dashboard.api_paths``) is passed through to the EMS's dashboard
server on this host, and its answer back:

- one sign-in, the console's. The browser holds no session of the EMS's and no
  cookie passes either way. The console answers the EMS's sign-in paths itself
  (``admin.server``), and for a request its signed-in operator sent, with its
  CSRF token for a change, it says so to the EMS (``operator``); the EMS takes
  that word only with the forwarding key and decides everything else as for any
  request.
- an allow-list of headers each way, so neither side sees what the other did
  not mean to send it; the Admin's own security headers go on every answer.
- the live stream (``/api/events``) is passed on as it arrives, not buffered.
- the client is named to the EMS (``dashboard.forwarding``), so its per-client
  limits stay per client.

The EMS is reached through its local socket file in the installation's data
directory (``ems.paths.resolve_dashboard_socket_path``), which this console
mounts as the EMS does, so no port of the host's is needed. An EMS that has no
socket, as one older than the console after going back a version, is found
the way it reads its own settings: the port and the HTTPS switch of
``dashboard`` in the installed ``config.json``, on this host's loopback, where
the Admin's host network reaches the port the EMS container publishes; an
Admin in a Docker bridge network reaches the host at its default gateway
(``dashboard.host_reach``), or at the host ``EMS_ADMIN_EMS_HOST`` names. A
refusal says why in its own code, so the app can tell an EMS that is not set up
from one that does not answer.
"""

import http.client
import json
import os
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from dashboard.forwarding import (
    CONSOLE_OPERATOR_HEADER,
    FORWARDED_FOR_HEADER,
    FORWARDING_KEY_HEADER,
    FORWARDING_KEY_NAME,
    ForwardingKey,
)
from dashboard.host_reach import first_answering, host_candidates, in_bridge_network
from ems.config import normalize_dashboard_config, safe_bool, safe_int
from ems.paths import resolve_dashboard_socket_path

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
    """Where the EMS's dashboard server answers on this host: its socket file first, else its port."""

    port: int
    https: bool = False
    cafile: Path | None = None
    socket: Path | None = None


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
    local = resolve_dashboard_socket_path(context.data_dir)
    if not safe_bool(dashboard.get("ssl_enabled"), False):
        return EmsUpstream(port=port, socket=local)
    cert = str(dashboard.get("ssl_cert_file") or DEFAULT_CERT_FILE)
    if cert.startswith(EMS_CONTAINER_ROOT):
        cert = cert[len(EMS_CONTAINER_ROOT):]
    cafile = Path(cert) if Path(cert).is_absolute() else Path(context.install_root) / cert
    return EmsUpstream(port=port, https=True, cafile=cafile, socket=local)


_KEYS = {}
_KEYS_LOCK = threading.Lock()


def forwarding_key_for(context):
    """The installation's forwarding key file beside its ``config.json``, one reader per file."""

    path = os.fspath(Path(context.config_path).parent / FORWARDING_KEY_NAME)
    with _KEYS_LOCK:
        return _KEYS.setdefault(path, ForwardingKey(path))


def installed_ems():
    """The installed EMS's upstream and the forwarding key, from this host's install context."""

    from admin.install_context import detect_install_context

    context = detect_install_context()
    return ems_upstream(context), forwarding_key_for(context).ensure()


def one_line(value):
    """A header value as one line: a line break in it could start a header of its own."""

    return str(value).replace("\r", " ").replace("\n", " ")


def _tls_context(cafile):
    context = ssl.create_default_context(cafile=str(cafile))
    context.check_hostname = False
    context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    context.verify_flags |= getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    return context


class SocketConnection(http.client.HTTPConnection):
    """HTTP to the EMS through its local socket file."""

    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self._path = os.fspath(path)

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def _connection(upstream, timeout):
    if upstream.socket is not None:
        local = SocketConnection(upstream.socket, timeout)
        try:
            local.connect()
            return local
        except OSError:
            local.close()
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


def _request_headers(handler, client, key, operator):
    headers = {name: handler.headers[name] for name in REQUEST_HEADERS if handler.headers.get(name) is not None}
    if operator and key:
        headers[CONSOLE_OPERATOR_HEADER] = "1"
    headers[FORWARDED_FOR_HEADER] = client
    headers["X-Forwarded-Proto"] = "https" if getattr(handler.server, "https_active", False) else "http"
    host = handler.headers.get("Host")
    if host:
        headers["X-Forwarded-Host"] = host
    if key:
        headers[FORWARDING_KEY_HEADER] = key
    return headers


def forward(handler, upstream, *, key, security_headers, operator=False):
    """Pass the handler's request to the EMS and its answer back to the browser.

    ``operator`` says the console's signed-in operator sent it, which the
    caller decided (its session, and its CSRF token for a change); the EMS
    takes that word only with the key. Raises :class:`ForwardingRefusal`
    before anything is written to the browser; once the answer has started, a
    broken stream just ends it.
    """

    target = _request_target(handler)
    streaming = target.split("?", 1)[0] in STREAM_PATHS
    body = _request_body(handler)
    client = handler.client_address[0] if handler.client_address else "unknown"
    connection = _connection(upstream, CONNECT_TIMEOUT_SECONDS)
    try:
        try:
            connection.request(handler.command, target, body=body, headers=_request_headers(handler, client, key, operator))
            connection.sock.settimeout(STREAM_TIMEOUT_SECONDS if streaming else READ_TIMEOUT_SECONDS)
            response = connection.getresponse()
        except ssl.SSLError:
            raise ForwardingRefusal("ems_certificate_mismatch", 502, "The EMS dashboard's certificate does not match.")
        except TimeoutError:
            raise ForwardingRefusal("ems_timeout", 504, "The EMS did not answer in time.")
        except (OSError, http.client.HTTPException):
            raise ForwardingRefusal("ems_unreachable", 502, "The EMS does not answer.")
        _relay(handler, response, security_headers)
    finally:
        connection.close()


def _relay(handler, response, security_headers):
    handler.send_response(response.status, response.reason)
    for name, value in security_headers.items():
        handler.send_header(name, value)
    for name in RESPONSE_HEADERS:
        value = response.getheader(name)
        if value is not None:
            handler.send_header(name, one_line(value))
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
