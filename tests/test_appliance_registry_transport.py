# SPDX-License-Identifier: AGPL-3.0-or-later
"""The registry lookup over a real TLS connection: where its token goes, and when it ends.

The registry decides which tag *Latest stable* installs, and it is read with an
anonymous bearer token. Two things are only visible over a real connection:
which hops urllib follows, with which headers, and whether a body the server
keeps alive inside one read ends on the lookup's deadline. The test CA and the
loopback server are the fetcher's own, from ``test_appliance_release_fetch``.
"""

import json
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from appliance import registry_tags
from appliance.registry_tags import RegistryError
from tests.test_appliance_release_fetch import (
    HEAD,
    SERVER_PATIENCE,
    LoopbackHttps,
    ManualTimers,
    tls_material,
    trusted_tls,
)

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

__all__ = ["tls_material", "trusted_tls"]

PATH = "basecubedev/ems-solarflow-admin"
TOKEN = "anonymous-pull-token"


class LoopbackRegistry:
    """An https registry on 127.0.0.1 that answers every request through ``route``.

    ``route(registry, path, headers)`` returns ``(status, headers, payload)``;
    every request is recorded with its headers, so a test sees what reached
    which host.
    """

    def __init__(self, material, route):
        _, cert, key = material
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        registry = self
        self.requests = []
        self.route = route

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                headers = dict(self.headers.items())
                registry.requests.append((self.path, headers))
                status, extra, payload = registry.route(registry, self.path, headers)
                body = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                for name, value in extra.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def origin(self):
        return f"https://127.0.0.1:{self.port}"

    @property
    def repository(self):
        return f"127.0.0.1:{self.port}/{PATH}"

    def challenge(self):
        return (
            f'Bearer realm="{self.origin}/token",service="test",'
            f'scope="repository:{PATH}:pull"'
        )

    def tags_url(self):
        return f"{self.origin}/v2/{PATH}/tags/list?n={registry_tags.PAGE_SIZE}"

    def saw_token(self):
        return any(
            headers.get("Authorization") == f"Bearer {TOKEN}" for _, headers in self.requests
        )

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=SERVER_PATIENCE)
        return False


def authenticated(then):
    """A registry route: the challenge, its token, and ``then`` once the token is sent."""

    def route(registry, path, headers):
        if path.startswith("/token"):
            return 200, {}, {"token": TOKEN}
        if headers.get("Authorization") != f"Bearer {TOKEN}":
            return 401, {"WWW-Authenticate": registry.challenge()}, {}
        return then(registry, path, headers)

    return route


def serves(tags):
    def route(registry, path, headers):
        return 200, {"Content-Type": "application/json"}, {"tags": tags}

    return route


# --- where the token goes ------------------------------------------------------


def test_a_redirect_to_http_is_refused_before_the_http_hop_is_contacted(trusted_tls):
    """urllib followed it, and the registry's bearer token went along over plain
    http: whoever sits on that hop chooses the tag *Latest stable* installs."""

    plain = socket.create_server(("127.0.0.1", 0))
    plain.setblocking(False)
    target = f"http://127.0.0.1:{plain.getsockname()[1]}/v2/{PATH}/tags/list"

    def downgrade(registry, path, headers):
        return 302, {"Location": target}, None

    try:
        with LoopbackRegistry(trusted_tls, authenticated(downgrade)) as registry:
            with pytest.raises(RegistryError) as refused:
                registry_tags.list_tags(registry.repository, timeout=5)
        with pytest.raises(BlockingIOError):
            plain.accept()
    finally:
        plain.close()

    assert refused.value.code == "release_registry_unreachable"
    assert "non-https" in refused.value.message
    assert registry.saw_token(), "the lookup never reached the authenticated request"


def test_the_token_does_not_follow_a_redirect_to_another_host(trusted_tls):
    """A registry may send a read on to a CDN with a signed URL, as Docker Hub
    does for blobs. urllib copies every request header onto a redirect, whatever
    host it names, so the token reached the other host."""

    with LoopbackRegistry(trusted_tls, serves(["v1.0.0"])) as elsewhere:

        def offload(registry, path, headers):
            return 302, {"Location": f"{elsewhere.origin}/v2/{PATH}/tags/list"}, None

        with LoopbackRegistry(trusted_tls, authenticated(offload)) as registry:
            tags = registry_tags.list_tags(registry.repository, timeout=SERVER_PATIENCE)

    assert tags == ["v1.0.0"]
    assert registry.saw_token()
    assert elsewhere.requests, "the redirect was not followed"
    assert not any("Authorization" in headers for _, headers in elsewhere.requests)


def test_the_token_follows_a_redirect_on_the_registry_s_own_origin(trusted_tls):
    def moved(registry, path, headers):
        if path.startswith("/moved"):
            return 200, {}, {"tags": ["v1.0.0"]}
        return 307, {"Location": f"{registry.origin}/moved/tags/list"}, None

    with LoopbackRegistry(trusted_tls, authenticated(moved)) as registry:
        tags = registry_tags.list_tags(registry.repository, timeout=SERVER_PATIENCE)

    assert tags == ["v1.0.0"]
    moved_requests = [headers for path, headers in registry.requests if path.startswith("/moved")]
    assert [headers.get("Authorization") for headers in moved_requests] == [f"Bearer {TOKEN}"]


# --- when it ends -----------------------------------------------------------


def test_a_body_kept_alive_inside_one_read_ends_on_the_lookup_s_deadline(
    trusted_tls, monkeypatch
):
    """The lookup checked its budget only between requests, so a body trickled
    inside one read was bounded by the server alone, and a *Latest stable* plan
    waited on it holding the operation lock.

    Interleaving: the response head and part of the body have arrived when the
    deadline passes, while the lookup reads the rest. Whether it is already
    blocked in that read or about to start it, the lookup ends there, before the
    server's patience runs out and it sends the rest.
    """

    timers = ManualTimers()
    monkeypatch.setattr(threading, "Timer", timers)
    body = json.dumps({"tags": ["v1.0.0"]}).encode()
    sent, rest = body[:5], body[5:]

    def trickles(server, conn):
        with server.tls(conn) as tls:
            tls.sendall(HEAD + b"Content-Length: %d\r\n\r\n" % len(body) + sent)
            if timers.made:
                timers.deadline_passes()
            if not server.waits_for_the_fetcher():
                tls.sendall(rest)

    with LoopbackHttps(trusted_tls, trickles) as server:
        with pytest.raises(RegistryError) as refused:
            registry_tags.list_tags(
                f"127.0.0.1:{server.port}/{PATH}", timeout=SERVER_PATIENCE * 3
            )

    assert refused.value.code == "release_registry_unreachable"
    assert "in time" in refused.value.message
    assert not server.patience_ran_out
    assert timers.made[-1].cancelled
