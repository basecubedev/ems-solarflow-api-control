# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Admin console passes the EMS dashboard's API through, and nothing more.

The one browser app is served by the Admin console, so the dashboard's pages
call the EMS through the console's own address. What these tests hold:

- the forwarded paths are exactly the ones the dashboard server dispatches,
  and none of the console's own;
- a request reaches the EMS with its body and with nothing the client was not
  meant to send it, no cookie either way; the answer comes back with the
  console's security headers and nothing the EMS was not meant to say;
- one sign-in: the console answers the EMS's sign-in paths itself, and says
  its signed-in operator sent a request only with its session, and with its
  CSRF token for a change; the EMS takes that word only with the key;
- the live stream arrives event by event, not when it ends;
- an EMS that is not set up, switched off or silent is said as such;
- the EMS counts a forwarded client as that client only with the forwarding key;
- the console tells the EMS where it answers, for the EMS's own dashboard;
- the console reaches the EMS through its socket file, and through its port
  only when the EMS has none; the port only points to the console while the
  console answers, and gives out no data then.
"""

import json
import os
import re
import socket
import socketserver
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.request
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from admin.ems_forwarding import (
    MAX_FORWARD_BODY_BYTES,
    EmsUpstream,
    ForwardingRefusal,
    ems_host,
    one_line,
    ems_upstream,
    installed_ems,
    shares_host_network,
)
from admin.server import create_server
from tests.admin_auth_helpers import TEST_PASSWORD, auth_headers, authenticate
from dashboard.api_paths import EMS_API_PATHS, EMS_API_PREFIXES, is_ems_api_path
from dashboard.console_address import (
    CONSOLE_ADDRESS_NAME,
    LEASE_SECONDS,
    ConsoleLease,
    console_address,
    publish_console_address,
    read_console_address,
    read_live_console,
)
from dashboard.host_reach import default_gateway, host_candidates, in_bridge_network
from dashboard.auth import write_password_file
from dashboard.forwarding import CONSOLE_OPERATOR_HEADER, FORWARDING_KEY_HEADER, FORWARDING_KEY_NAME, ForwardingKey, forwarded_client
from dashboard.server import start_dashboard_server

pytestmark = [pytest.mark.contract, pytest.mark.admin]

ROOT = Path(__file__).resolve().parents[1]
KEY = "ab" * 32


@pytest.fixture(autouse=True)
def _isolate_install_root(isolated_install_root):
    return isolated_install_root


def _dispatch_source():
    source = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
    start = source.index("class DashboardRequestHandler")
    return source[source.index("    def do_GET", start):source.index("    def _handle_schedule", start)]


def test_the_forwarded_paths_are_the_ones_the_dashboard_server_dispatches():
    dispatch = _dispatch_source()
    exact = set(re.findall(r'parsed\.path == "(/api/[^"]+)"', dispatch))
    prefixes = set(re.findall(r'parsed\.path\.startswith\("(/api/[^"]+)"\)', dispatch))
    schedule = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
    schedule = schedule[schedule.index("def _is_schedule_path"):]
    schedule = schedule[:schedule.index("\n\n")]
    assert "_is_schedule_path(parsed.path)" in dispatch
    exact |= set(re.findall(r'path == "(/api/[^"]+)"', schedule))
    prefixes |= set(re.findall(r'path\.startswith\("(/api/[^"]+)"\)', schedule))
    assert exact == set(EMS_API_PATHS)
    assert prefixes == set(EMS_API_PREFIXES)


def test_no_path_the_console_answers_itself_is_forwarded():
    source = (ROOT / "admin" / "server.py").read_text(encoding="utf-8")
    own = set(re.findall(r'path == "(/api/[^"]+)"', source))
    own |= set(re.findall(r'startswith\("(/api/[^"]+)"\)', source))
    assert len(own) > 100
    assert sorted(path for path in own if is_ems_api_path(path)) == []


class FakeEms(BaseHTTPRequestHandler):
    """The EMS's dashboard server as the forwarding sees it: records, answers by path."""

    def log_message(self, *_args):
        pass

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.seen.append(
            {"method": self.command, "path": self.path, "headers": dict(self.headers.items()), "body": body}
        )

    def _answer(self):
        self._record()
        if self.path.startswith("/api/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"event: telemetry\ndata: 1\n\n")
            self.wfile.flush()
            self.server.release.wait(10)
            self.wfile.write(b"event: telemetry\ndata: 2\n\n")
            self.wfile.flush()
            return
        body = json.dumps({"answered": self.path, "method": self.command}).encode()
        self.send_response(201 if self.command == "POST" else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", "ems_dashboard_session=abc; Path=/; HttpOnly; SameSite=Strict; Secure")
        self.send_header("Set-Cookie", "ems_admin_session=planted; Path=/")
        self.send_header("X-Internal", "not for the browser")
        self.send_header("Content-Security-Policy", "default-src *")
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _answer


@pytest.fixture()
def fake_ems():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeEms)
    server.daemon_threads = True
    server.seen = []
    server.release = threading.Event()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()


@pytest.fixture()
def console(fake_ems):
    server = create_server("127.0.0.1", 0)
    server.ems_upstream = lambda: (EmsUpstream(port=fake_ems.server_address[1]), KEY)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _call(url, method="GET", body=None, headers=None):
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def test_a_read_reaches_the_ems_with_its_query_and_comes_back_with_the_console_s_headers(console, fake_ems):
    _, base = console
    status, headers, body = _call(
        f"{base}/api/live?since=1",
        headers={
            "Cookie": "ems_admin_session=adm; ems_dashboard_session=s1; theme=dark",
            "X-Forwarded-For": "203.0.113.9",
            "Authorization": "Basic eDp5",
        },
    )
    assert status == 200
    assert json.loads(body) == {"answered": "/api/live?since=1", "method": "GET"}
    assert headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert headers.get("X-Internal") is None
    assert headers.get_all("Set-Cookie") is None
    seen = fake_ems.seen[0]["headers"]
    assert "Cookie" not in seen
    assert seen["X-Forwarded-For"] == "127.0.0.1"
    assert seen[FORWARDING_KEY_HEADER] == KEY
    assert "Authorization" not in seen
    assert CONSOLE_OPERATOR_HEADER not in seen


def test_a_signed_out_write_reaches_the_ems_without_the_console_s_word(console, fake_ems):
    _, base = console
    body = json.dumps({"enabled": False}).encode()
    status, _, answer = _call(
        f"{base}/api/runtime/device/a", method="PATCH", body=body,
        headers={"Content-Type": "application/json", "X-CSRF-Token": "t1"},
    )
    assert status == 200
    assert json.loads(answer)["answered"] == "/api/runtime/device/a"
    seen = fake_ems.seen[0]
    assert seen["body"] == body
    assert seen["headers"]["Content-Type"] == "application/json"
    assert "X-CSRF-Token" not in seen["headers"] and CONSOLE_OPERATOR_HEADER not in seen["headers"]


def test_the_console_answers_the_ems_sign_in_paths_itself(console, fake_ems):
    _, base = console
    status, _, body = _call(f"{base}/api/auth/status")
    assert status == 200 and json.loads(body)["authenticated"] is False
    authenticate(base)
    status, headers, body = _call(f"{base}/api/auth/login", method="POST", body=json.dumps({"password": TEST_PASSWORD}).encode(),
                                  headers={"Content-Type": "application/json"})
    assert status == 200 and json.loads(body)["csrf_token"]
    assert "ems_admin_session=" in headers["Set-Cookie"]
    assert fake_ems.seen == []


def test_the_console_vouches_for_its_signed_in_operator_and_a_change_needs_its_csrf_token(console, fake_ems):
    _, base = console
    authenticate(base)
    signed_in = auth_headers(base, "PATCH")
    cookie = {"Cookie": signed_in["Cookie"]}
    assert _call(f"{base}/api/live", headers=cookie)[0] == 200
    assert fake_ems.seen[-1]["headers"][CONSOLE_OPERATOR_HEADER] == "1"
    assert _call(f"{base}/api/auth/status", headers=cookie)[0] == 200
    assert len(fake_ems.seen) == 1
    for refused in ({}, {"X-CSRF-Token": "not-the-token"}):
        status, _, body = _call(f"{base}/api/runtime/device/a", method="PATCH", body=b"{}", headers={**cookie, **refused})
        assert (status, json.loads(body)["error"]) == (403, "csrf_failed")
    assert len(fake_ems.seen) == 1
    assert _call(f"{base}/api/runtime/device/a", method="PATCH", body=b"{}", headers=signed_in)[0] == 200
    seen = fake_ems.seen[-1]["headers"]
    assert seen[CONSOLE_OPERATOR_HEADER] == "1" and seen[FORWARDING_KEY_HEADER] == KEY
    assert "Cookie" not in seen and "X-CSRF-Token" not in seen


@pytest.mark.parametrize("method, path", [("PATCH", "/api/runtime/device/a"), ("PUT", "/api/schedule/r1"), ("DELETE", "/api/schedule/r1")])
def test_every_method_the_ems_takes_is_forwarded(console, fake_ems, method, path):
    _, base = console
    status, _, answer = _call(f"{base}{path}", method=method, body=b"{}", headers={"Content-Type": "application/json"})
    assert status == 200
    assert json.loads(answer) == {"answered": path, "method": method}


def test_a_path_the_console_owns_is_never_forwarded(console, fake_ems):
    _, base = console
    status, _, answer = _call(f"{base}/api/admin/status")
    assert status == 403
    assert json.loads(answer)["error"] == "auth_not_configured"
    status, _, _ = _call(f"{base}/api/admin/status", method="PUT", body=b"{}")
    assert status == 405
    assert fake_ems.seen == []


def test_a_body_too_large_is_refused_before_the_ems_sees_it(console, fake_ems):
    _, base = console
    status, _, answer = _call(f"{base}/api/schedule", method="POST", body=b"x" * (MAX_FORWARD_BODY_BYTES + 1))
    assert status == 413
    assert json.loads(answer)["error"] == "request_too_large"
    assert fake_ems.seen == []


def test_the_live_stream_arrives_event_by_event(console, fake_ems):
    """The second event is held back until the first has reached the client."""

    server, _ = console
    with socket.create_connection(server.server_address, timeout=10) as sock:
        sock.sendall(b"GET /api/events HTTP/1.1\r\nHost: test\r\n\r\n")
        received = b""
        while b"data: 1" not in received:
            chunk = sock.recv(4096)
            assert chunk, "the stream ended before its first event"
            received += chunk
        assert b"data: 2" not in received
        assert b"text/event-stream" in received
        fake_ems.release.set()
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            received += chunk
    assert received.index(b"data: 1") < received.index(b"data: 2")


def test_an_ems_that_does_not_answer_is_said_as_such(console):
    server, base = console
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
    server.ems_upstream = lambda: (EmsUpstream(port=port), None)
    status, _, answer = _call(f"{base}/api/live")
    assert status == 502
    assert json.loads(answer)["error"] == "ems_unreachable"


def test_an_installation_without_an_ems_is_said_as_such(console):
    server, base = console
    server.ems_upstream = installed_ems
    status, _, answer = _call(f"{base}/api/live")
    assert status == 503
    assert json.loads(answer)["error"] == "ems_not_configured"


def _context(tmp_path, dashboard):
    config = tmp_path / "config" / "config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"dashboard": dashboard}), encoding="utf-8")
    return SimpleNamespace(config_exists=True, config_path=config, install_root=tmp_path, data_dir=tmp_path / "data")


def test_the_ems_is_found_from_the_installed_dashboard_settings(tmp_path):
    local = tmp_path / "data" / "run" / "dashboard.sock"
    assert ems_upstream(_context(tmp_path, {"port": 8085})) == EmsUpstream(port=8085, socket=local)
    assert ems_upstream(_context(tmp_path, {})) == EmsUpstream(port=8080, socket=local)
    assert ems_upstream(_context(tmp_path, {"port": "8086"})) == EmsUpstream(port=8086, socket=local)
    assert ems_upstream(_context(tmp_path, {"port": 70000})) == EmsUpstream(port=8080, socket=local)
    assert ems_upstream(_context(tmp_path, {"ssl_enabled": "true"})).https
    assert ems_upstream(_context(tmp_path, {"enabled": "yes"})) == EmsUpstream(port=8080, socket=local)
    secure = ems_upstream(_context(tmp_path, {"ssl_enabled": True}))
    assert secure == EmsUpstream(port=8080, https=True, cafile=tmp_path / "config" / "dashboard.crt", socket=local)
    for off in (False, "false", "off"):
        with pytest.raises(ForwardingRefusal) as refused:
            ems_upstream(_context(tmp_path, {"enabled": off}))
        assert (refused.value.code, refused.value.status) == ("ems_dashboard_disabled", 503)
    with pytest.raises(ForwardingRefusal) as refused:
        ems_upstream(SimpleNamespace(config_exists=False))
    assert refused.value.code == "ems_not_configured"


def test_a_header_value_comes_back_on_one_line():
    assert one_line("a\r\nb") == "a  b"


def test_a_forwarded_client_counts_only_with_the_key():
    headers = {"X-Forwarded-For": "192.0.2.7", FORWARDING_KEY_HEADER: KEY}
    assert forwarded_client(headers, "172.17.0.1", KEY) == "192.0.2.7"
    assert forwarded_client(headers, "172.17.0.1", None) == "172.17.0.1"
    assert forwarded_client({**headers, FORWARDING_KEY_HEADER: "cd" * 32}, "172.17.0.1", KEY) == "172.17.0.1"
    assert forwarded_client({"X-Forwarded-For": "192.0.2.7"}, "172.17.0.1", KEY) == "172.17.0.1"
    assert forwarded_client({**headers, "X-Forwarded-For": "not an address"}, "172.17.0.1", KEY) == "172.17.0.1"


def test_the_key_is_made_once_private_and_read_back(tmp_path):
    key = ForwardingKey(tmp_path / FORWARDING_KEY_NAME)
    assert key.read() is None
    made = key.ensure()
    assert made and len(made) == 64
    assert os.stat(tmp_path / FORWARDING_KEY_NAME).st_mode & 0o777 == 0o600
    assert ForwardingKey(tmp_path / FORWARDING_KEY_NAME).ensure() == made
    (tmp_path / FORWARDING_KEY_NAME).write_text("short\n", encoding="ascii")
    assert ForwardingKey(tmp_path / FORWARDING_KEY_NAME).read() is None


@pytest.mark.parametrize("left", ["", "short\n", "zz" * 32])
def test_a_key_file_left_broken_is_written_again(tmp_path, left):
    (tmp_path / FORWARDING_KEY_NAME).write_text(left, encoding="ascii")
    made = ForwardingKey(tmp_path / FORWARDING_KEY_NAME).ensure()
    assert made and len(made) == 64
    assert ForwardingKey(tmp_path / FORWARDING_KEY_NAME).read() == made
    assert os.stat(tmp_path / FORWARDING_KEY_NAME).st_mode & 0o777 == 0o600
    assert sorted(path.name for path in tmp_path.iterdir()) == [FORWARDING_KEY_NAME]


def test_the_ems_is_reached_on_loopback_unless_the_admin_s_network_names_the_host(monkeypatch):
    monkeypatch.delenv("EMS_ADMIN_EMS_HOST", raising=False)
    assert (ems_host(), shares_host_network()) == ("127.0.0.1", True)
    monkeypatch.setenv("EMS_ADMIN_EMS_HOST", "host.docker.internal")
    assert (ems_host(), shares_host_network()) == ("host.docker.internal", False)


@pytest.mark.parametrize("target", [b"/api/live?\x01", b"/api/schedule/\xff", b"/api/live?a=\x7f"])
def test_a_path_that_cannot_be_passed_on_is_refused_with_an_answer(console, fake_ems, target):
    _, base = console
    port = int(base.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        raw.sendall(b"GET " + target + b" HTTP/1.0\r\nHost: x\r\n\r\n")
        answer = b""
        while chunk := raw.recv(65536):
            answer += chunk
    assert answer.startswith(b"HTTP/1.0 400"), answer[:80]
    assert b'"bad_request"' in answer
    assert fake_ems.seen == []


class _Store:
    def latest(self):
        return {"timestamp": 1}


def _open_stream(port, headers):
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    lines = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
    sock.sendall(f"GET /api/events HTTP/1.1\r\nHost: test\r\n{lines}\r\n".encode())
    status = sock.recv(64).split(b"\r\n", 1)[0]
    return sock, status


def test_the_ems_counts_live_streams_per_forwarded_client_only_with_the_key(tmp_path):
    """One stream per client: a keyed client and the unkeyed address are counted apart."""

    config = tmp_path / "config" / "config.json"
    config.parent.mkdir()
    config.write_text("{}", encoding="utf-8")
    (config.parent / FORWARDING_KEY_NAME).write_text(KEY + "\n", encoding="ascii")
    server = start_dashboard_server(
        _Store(), host="127.0.0.1", port=0, config_path=str(config), auth_file=str(tmp_path / "auth.json"),
        sse_max_connections_per_ip=1, sse_max_connection_seconds=2,
    )
    port = server.server_address[1]
    keyed = {"X-Forwarded-For": "192.0.2.7", FORWARDING_KEY_HEADER: KEY}
    opened = []
    try:
        first, status = _open_stream(port, keyed)
        opened.append(first)
        assert b"200" in status
        unkeyed, status = _open_stream(port, {"X-Forwarded-For": "192.0.2.7"})
        opened.append(unkeyed)
        assert b"200" in status
        again, status = _open_stream(port, keyed)
        opened.append(again)
        assert b"429" in status
    finally:
        for sock in opened:
            sock.close()
        server.shutdown()
        server.server_close()


def test_the_console_s_address_is_published_beside_the_config_and_read_back(tmp_path):
    record = console_address(8090, 8091)
    assert publish_console_address(tmp_path / "missing", record) is False
    assert publish_console_address(tmp_path, record) is True
    assert read_console_address(tmp_path / CONSOLE_ADDRESS_NAME) == {"port": 8090, "https_port": 8091}
    assert publish_console_address(tmp_path, record) is False
    assert publish_console_address(tmp_path, None) is True
    assert not (tmp_path / CONSOLE_ADDRESS_NAME).exists()
    (tmp_path / CONSOLE_ADDRESS_NAME).write_text('{"port": "8090"}', encoding="utf-8")
    assert read_console_address(tmp_path / CONSOLE_ADDRESS_NAME) is None
    with pytest.raises(ValueError):
        console_address(0)


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _age(path, seconds):
    then = time.time() - seconds
    os.utime(path, (then, then))


def test_the_ems_dashboard_names_the_console_only_while_its_lease_is_renewed(tmp_path):
    """A console that stopped, or an older one that renews nothing, is named for half a minute at most."""

    config = tmp_path / "config" / "config.json"
    config.parent.mkdir()
    config.write_text("{}", encoding="utf-8")
    server = start_dashboard_server(
        _Store(), host="127.0.0.1", port=0, config_path=str(config), auth_file=str(tmp_path / "auth.json"),
    )
    base = f"http://127.0.0.1:{server.server_address[1]}"
    lease = config.parent / CONSOLE_ADDRESS_NAME
    try:
        assert json.loads(_call(f"{base}/api/ui-config")[2])["admin_console"] is None
        publish_console_address(config.parent, console_address(8090))
        assert json.loads(_call(f"{base}/api/ui-config")[2])["admin_console"] == {"port": 8090, "https_port": None}
        for seconds in (LEASE_SECONDS + 5, -(LEASE_SECONDS + 5)):
            _age(lease, seconds)
            assert json.loads(_call(f"{base}/api/ui-config")[2])["admin_console"] is None, seconds
        _age(lease, LEASE_SECONDS - 5)
        assert json.loads(_call(f"{base}/api/ui-config")[2])["admin_console"] is not None
    finally:
        server.shutdown()
        server.server_close()


def test_the_console_publishes_its_lease_once_the_installation_is_there_and_renews_it(tmp_path):
    config_dir = tmp_path / "config"
    lease = ConsoleLease(lambda: config_dir, console_address(8090, 8091))
    assert lease.renew() is False
    config_dir.mkdir()
    assert lease.renew() is True
    path = config_dir / CONSOLE_ADDRESS_NAME
    assert read_live_console(path) == {"port": 8090, "https_port": 8091}
    _age(path, LEASE_SECONDS + 5)
    assert read_live_console(path) is None
    lease.renew()
    assert read_live_console(path) == {"port": 8090, "https_port": 8091}


def test_the_console_says_signed_out_that_it_carries_the_dashboard(console):
    _, base = console
    status, _, body = _call(f"{base}/api/console")
    assert (status, json.loads(body)) == (200, {"console": "ems-solarflow-admin", "dashboard": True})


def test_the_host_is_the_loopback_then_the_default_gateway(tmp_path):
    table = tmp_path / "route"
    table.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "eth0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\n"
        "eth0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\n",
        encoding="ascii",
    )
    assert default_gateway(table) == "172.17.0.1"
    assert host_candidates(table, bridged=True) == ["127.0.0.1", "172.17.0.1"]
    assert host_candidates(table, bridged=False) == ["127.0.0.1"]
    assert host_candidates(tmp_path / "missing", bridged=True) == ["127.0.0.1"]


def test_the_gateway_is_a_candidate_only_inside_a_bridge_network(tmp_path):
    mark = tmp_path / "dockerenv"
    net = tmp_path / "net"
    net.mkdir()
    for name in ("lo", "eth0"):
        (net / name).mkdir()
    assert not in_bridge_network(net, mark)
    mark.write_text("", encoding="ascii")
    assert in_bridge_network(net, mark)
    (net / "docker0").mkdir()
    assert not in_bridge_network(net, mark)


class _EmsUiConfig(BaseHTTPRequestHandler):
    """An EMS's dashboard server as far as its start-up hints go."""

    def log_message(self, *_args):
        pass

    def do_GET(self):
        body = json.dumps({"animation_mode": "auto", "admin_console": None}).encode()
        self.send_response(200 if self.path == "/api/ui-config" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_a_host_other_than_the_loopback_is_used_only_once_it_answers_as_the_ems(monkeypatch, fake_ems):
    import admin.ems_forwarding as forwarding

    monkeypatch.delenv("EMS_ADMIN_EMS_HOST", raising=False)
    monkeypatch.setattr(forwarding, "_REACHED", {})
    monkeypatch.setattr(forwarding, "LOOPBACK_EMS_HOST", "127.0.0.3")
    monkeypatch.setattr(forwarding, "host_candidates", lambda: ["127.0.0.3", "127.0.0.1"])
    router = fake_ems.server_address[1]
    assert forwarding.ems_host(EmsUpstream(port=router)) == "127.0.0.3"
    assert forwarding._REACHED == {}
    ems = _serve(_EmsUiConfig)
    try:
        assert forwarding.ems_host(EmsUpstream(port=ems.server_address[1])) == "127.0.0.1"
    finally:
        ems.shutdown()
        ems.server_close()


def test_a_miss_is_not_kept_so_an_ems_back_from_a_restart_is_found_at_once(monkeypatch, fake_ems):
    import admin.ems_forwarding as forwarding

    monkeypatch.delenv("EMS_ADMIN_EMS_HOST", raising=False)
    monkeypatch.setattr(forwarding, "_REACHED", {})
    candidates = [["127.0.0.2"]]
    monkeypatch.setattr(forwarding, "host_candidates", lambda: candidates[0])
    port = fake_ems.server_address[1]
    assert forwarding.ems_host(EmsUpstream(port=port)) == "127.0.0.1"
    assert forwarding._REACHED == {}
    candidates[0] = ["127.0.0.2", "127.0.0.1"]
    assert forwarding.ems_host(EmsUpstream(port=port)) == "127.0.0.1"
    assert port in forwarding._REACHED


@pytest.mark.parametrize(
    "header, name",
    [("192.168.1.110:8080", "192.168.1.110"), ("ems.local", "ems.local"), ("[fe80::1]:8080", "[fe80::1]"), ("a b:1", None), ("x\r\ny", None), (None, None)],
)
def test_the_page_names_the_console_only_at_a_plain_host_name(header, name):
    from dashboard.server import _request_hostname

    assert _request_hostname(header) == name


def test_the_dashboard_page_may_ask_the_console_it_names_and_nothing_else(tmp_path):
    config = tmp_path / "config" / "config.json"
    config.parent.mkdir()
    config.write_text("{}", encoding="utf-8")
    server = start_dashboard_server(
        _Store(), host="127.0.0.1", port=0, config_path=str(config), auth_file=str(tmp_path / "auth.json"),
    )
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        policy = _call(f"{base}/")[1]["Content-Security-Policy"]
        assert "connect-src 'self';" in policy
        publish_console_address(config.parent, console_address(8090, 8091))
        policy = _call(f"{base}/", headers={"Host": "ems.local:8080"})[1]["Content-Security-Policy"]
        assert "connect-src 'self' http://ems.local:8090 https://ems.local:8091;" in policy
        assert "connect-src 'self';" in _call(f"{base}/api/ui-config")[1]["Content-Security-Policy"]
    finally:
        server.shutdown()
        server.server_close()


def test_the_ems_is_found_where_it_answers_and_named_hosts_win(monkeypatch, fake_ems):
    import admin.ems_forwarding as forwarding

    monkeypatch.delenv("EMS_ADMIN_EMS_HOST", raising=False)
    monkeypatch.setattr(forwarding, "_REACHED", {})
    monkeypatch.setattr(forwarding, "host_candidates", lambda: ["127.0.0.2", "127.0.0.1"])
    port = fake_ems.server_address[1]
    assert forwarding.ems_host(EmsUpstream(port=port)) == "127.0.0.1"
    monkeypatch.setenv("EMS_ADMIN_EMS_HOST", "host.docker.internal")
    assert forwarding.ems_host(EmsUpstream(port=port)) == "host.docker.internal"


def test_the_console_serves_the_chart_library_the_dashboard_pages_load(console):
    _, base = console
    status, headers, body = _call(f"{base}/uPlot.iife.min.js")
    assert status == 200
    assert "javascript" in headers["Content-Type"]
    assert body == (ROOT / "dashboard" / "static" / "uPlot.iife.min.js").read_bytes()


@pytest.fixture()
def short_dir():
    """A directory with a short path: a socket file's path may hold only about a hundred bytes."""

    path = Path(tempfile.mkdtemp(prefix="ems-"))
    try:
        yield path
    finally:
        for entry in sorted(path.rglob("*"), reverse=True):
            entry.rmdir() if entry.is_dir() and not entry.is_symlink() else entry.unlink()
        path.rmdir()


def _installation(root, record=None, key=None):
    config = root / "config" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text("{}", encoding="utf-8")
    if key:
        (config.parent / FORWARDING_KEY_NAME).write_text(key + "\n", encoding="ascii")
    if record:
        publish_console_address(config.parent, record)
    return config


def _local_call(path, target, method="GET", headers=None):
    connection = http.client.HTTPConnection("localhost", timeout=10)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.connect(os.fspath(path))
    connection.sock = sock
    try:
        connection.request(method, target, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _dashboard(config, tmp, socket_path):
    return start_dashboard_server(
        _Store(), host="127.0.0.1", port=0, config_path=str(config), auth_file=str(tmp / "auth.json"), socket_path=socket_path,
    )


def _stop(server):
    for running in (getattr(server, "socket_listener", None), server):
        if running is not None:
            running.shutdown()
            running.server_close()


def test_the_ems_answers_on_its_socket_file_as_the_same_server(short_dir):
    config = _installation(short_dir)
    local = short_dir / "data" / "run" / "dashboard.sock"
    server = _dashboard(config, short_dir, local)
    try:
        assert stat.S_ISSOCK(local.lstat().st_mode)
        assert stat.S_IMODE(local.lstat().st_mode) == 0o660
        status, body = _local_call(local, "/api/live")
        assert (status, json.loads(body)) == (200, {"timestamp": 1})
        listener = server.socket_listener
        assert listener.sessions is server.sessions and listener.maintenance_lock is server.maintenance_lock
        assert listener.https_active is False
    finally:
        _stop(server)


def test_a_socket_file_no_one_answers_on_is_replaced_and_anything_else_is_left(short_dir):
    config = _installation(short_dir)
    local = short_dir / "data" / "run" / "dashboard.sock"
    local.parent.mkdir(parents=True)
    left = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    left.bind(os.fspath(local))
    left.close()
    server = _dashboard(config, short_dir, local)
    second = _dashboard(config, short_dir, local)
    try:
        assert server.socket_listener is not None
        assert second.socket_listener is None
        assert _local_call(local, "/api/live")[0] == 200
    finally:
        _stop(second)
        _stop(server)
    other = short_dir / "data" / "run" / "other.sock"
    other.write_text("not a socket", encoding="utf-8")
    server = _dashboard(config, short_dir, other)
    try:
        assert server.socket_listener is None
        assert other.read_text(encoding="utf-8") == "not a socket"
        assert _call(f"http://127.0.0.1:{server.server_address[1]}/api/live")[0] == 200
    finally:
        _stop(server)


def test_the_port_only_points_to_the_console_while_the_console_renews_its_lease(short_dir):
    """The port gives out no data and takes no sign-in while the console runs; the socket and the console's own key always get answers."""

    record = console_address(8090)
    config = _installation(short_dir, record=record, key=KEY)
    local = short_dir / "data" / "run" / "dashboard.sock"
    server = _dashboard(config, short_dir, local)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    named = {"port": 8090, "https_port": None}
    try:
        status, _, body = _call(f"{base}/api/live")
        assert (status, json.loads(body)) == (410, {"error": "moved_to_console", "admin_console": named})
        assert _call(f"{base}/api/auth/login", method="POST", body=b"{}", headers={"Content-Type": "application/json"})[0] == 410
        assert _call(f"{base}/api/runtime/system", method="PATCH", body=b"{}")[0] == 410
        assert _call(f"{base}/api/schedule", method="DELETE")[0] == 410
        config_answer = json.loads(_call(f"{base}/api/ui-config")[2])
        assert config_answer["signpost"] is True and config_answer["admin_console"] == named
        assert _call(f"{base}/")[0] == 200

        assert _call(f"{base}/api/live", headers={FORWARDING_KEY_HEADER: KEY})[0] == 200
        assert "signpost" not in json.loads(_call(f"{base}/api/ui-config", headers={FORWARDING_KEY_HEADER: KEY})[2])
        assert _call(f"{base}/api/live", headers={FORWARDING_KEY_HEADER: "cd" * 32})[0] == 410
        assert _local_call(local, "/api/live")[0] == 200
        assert "signpost" not in json.loads(_local_call(local, "/api/ui-config")[1])

        _age(config.parent / CONSOLE_ADDRESS_NAME, LEASE_SECONDS + 5)
        assert _call(f"{base}/api/live")[0] == 200
        assert "signpost" not in json.loads(_call(f"{base}/api/ui-config")[2])
    finally:
        _stop(server)


class _UnixEms(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _ = self.socket.accept()
        return request, ("local", 0)


def test_the_console_reaches_the_ems_through_its_socket_and_by_its_port_when_it_has_none(short_dir, fake_ems):
    local = short_dir / "dashboard.sock"
    over_socket = _UnixEms(os.fspath(local), FakeEms)
    over_socket.seen = []
    over_socket.release = threading.Event()
    threading.Thread(target=over_socket.serve_forever, daemon=True).start()
    console = create_server("127.0.0.1", 0)
    console.ems_upstream = lambda: (EmsUpstream(port=fake_ems.server_address[1], socket=local), KEY)
    threading.Thread(target=console.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{console.server_address[1]}"
    try:
        assert _call(f"{base}/api/live")[0] == 200
        assert [seen["path"] for seen in over_socket.seen] == ["/api/live"] and fake_ems.seen == []
        assert over_socket.seen[0]["headers"][FORWARDING_KEY_HEADER] == KEY
        over_socket.shutdown()
        over_socket.server_close()
        assert local.exists()
        assert _call(f"{base}/api/runtime")[0] == 200
        assert [seen["path"] for seen in fake_ems.seen] == ["/api/runtime"]
        local.unlink()
        assert _call(f"{base}/api/history")[0] == 200
        assert [seen["path"] for seen in fake_ems.seen] == ["/api/runtime", "/api/history"]
    finally:
        over_socket.release.set()
        console.shutdown()
        console.server_close()


def test_the_console_s_status_finds_the_ems_on_its_socket_wherever_its_port_is_published(short_dir):
    from admin.deployment import probe_dashboard

    gone = socket.socket()
    gone.bind(("127.0.0.1", 0))
    nowhere = f"http://127.0.0.1:{gone.getsockname()[1]}"
    gone.close()
    local = short_dir / "dashboard.sock"
    assert probe_dashboard(nowhere, local, timeout=1.0) is False
    over_socket = _UnixEms(os.fspath(local), FakeEms)
    over_socket.seen = []
    over_socket.release = threading.Event()
    threading.Thread(target=over_socket.serve_forever, daemon=True).start()
    try:
        assert probe_dashboard(nowhere, local, timeout=1.0) is True
        assert [seen["path"] for seen in over_socket.seen] == ["/api/ui-config"]
    finally:
        over_socket.shutdown()
        over_socket.server_close()


def _local_send(path, target, method="POST", headers=None):
    connection = http.client.HTTPConnection("localhost", timeout=10)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.connect(os.fspath(path))
    connection.sock = sock
    try:
        connection.request(method, target, body=b"{}", headers={"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        connection.close()


def test_the_ems_takes_the_console_s_word_only_through_its_socket_with_the_key(short_dir):
    """The console's word replaces a session of the EMS's own only through the socket; the key alone on the port, or no password set, counts for nothing."""

    config = _installation(short_dir, key=KEY)
    auth = short_dir / "config" / "dashboard-auth.json"
    local = short_dir / "data" / "run" / "dashboard.sock"
    server = start_dashboard_server(_Store(), host="127.0.0.1", port=0, config_path=str(config), auth_file=str(auth), socket_path=local)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    vouched = {CONSOLE_OPERATOR_HEADER: "1", FORWARDING_KEY_HEADER: KEY}
    try:
        assert _local_send(local, "/api/auth/refresh", headers=vouched)[0] == 403
        write_password_file(auth, "pw")
        status, body = _local_send(local, "/api/auth/refresh", headers=vouched)
        assert status == 200 and body["authenticated"] is True and "csrf_token" not in body
        assert _local_call(local, "/api/ui-config", headers={FORWARDING_KEY_HEADER: KEY})[1].find(b'"console_operator": true') >= 0
        assert _call(f"{base}/api/auth/refresh", method="POST", body=b"{}", headers=vouched)[0] == 401
        assert json.loads(_call(f"{base}/api/ui-config", headers={FORWARDING_KEY_HEADER: KEY})[2])["console_operator"] is False
        for unproven in ({CONSOLE_OPERATOR_HEADER: "1"}, {CONSOLE_OPERATOR_HEADER: "1", FORWARDING_KEY_HEADER: "cd" * 32},
                         {FORWARDING_KEY_HEADER: KEY}, {CONSOLE_OPERATOR_HEADER: "yes", FORWARDING_KEY_HEADER: KEY}):
            assert _local_send(local, "/api/auth/refresh", headers=unproven)[0] == 401, unproven
        assert b'"console_operator": false' in _local_call(local, "/api/ui-config")[1]
    finally:
        _stop(server)


def test_with_the_signpost_switched_off_the_port_answers_in_full_while_the_console_runs(short_dir):
    """``dashboard.signpost: false`` is for a proxy or VPN that reaches this port and not the console."""

    config = _installation(short_dir, record=console_address(8090))
    server = start_dashboard_server(
        _Store(), host="127.0.0.1", port=0, config_path=str(config), auth_file=str(short_dir / "auth.json"), signpost=False,
    )
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert _call(f"{base}/api/live")[0] == 200
        assert json.loads(_call(f"{base}/api/ui-config")[2]) == {"animation_mode": "normal", "admin_console": None, "console_operator": False}
    finally:
        server.shutdown()
        server.server_close()


def test_the_socket_server_keeps_what_the_handler_stores_later_on_the_network_server(short_dir):
    config = _installation(short_dir)
    server = _dashboard(config, short_dir, short_dir / "data" / "run" / "dashboard.sock")
    try:
        listener = server.socket_listener
        listener._external_device_alias_cache = {"alias": 1}
        assert server._external_device_alias_cache == {"alias": 1}
        assert "_external_device_alias_cache" not in vars(listener)
        assert stat.S_IMODE((short_dir / "data" / "run").lstat().st_mode) == 0o750
    finally:
        _stop(server)


def test_a_socket_directory_of_its_own_left_open_is_made_private_again(short_dir):
    config = _installation(short_dir)
    run = short_dir / "data" / "run"
    run.mkdir(parents=True)
    run.chmod(0o777)
    server = _dashboard(config, short_dir, run / "dashboard.sock")
    try:
        assert server.socket_listener is not None
        assert stat.S_IMODE(run.lstat().st_mode) == 0o750
    finally:
        _stop(server)


def test_a_socket_directory_others_may_write_to_is_refused(short_dir, monkeypatch):
    import dashboard.server as dashboard_server

    config = _installation(short_dir)
    run = short_dir / "data" / "run"
    run.mkdir(parents=True)
    run.chmod(0o777)
    monkeypatch.setattr(dashboard_server.os, "geteuid", lambda: os.getuid() + 1)
    server = _dashboard(config, short_dir, run / "dashboard.sock")
    try:
        assert server.socket_listener is None
        assert not (run / "dashboard.sock").exists()
        assert _call(f"http://127.0.0.1:{server.server_address[1]}/api/live")[0] == 200
    finally:
        _stop(server)


def test_without_a_key_the_console_never_vouches(fake_ems):
    """``operator`` asks for the header, but the EMS could not prove it, so none is sent."""

    console = create_server("127.0.0.1", 0)
    console.ems_upstream = lambda: (EmsUpstream(port=fake_ems.server_address[1]), None)
    threading.Thread(target=console.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{console.server_address[1]}"
    try:
        authenticate(base)
        assert _call(f"{base}/api/live", headers={"Cookie": auth_headers(base, "GET")["Cookie"]})[0] == 200
        seen = fake_ems.seen[-1]["headers"]
        assert CONSOLE_OPERATOR_HEADER not in seen and FORWARDING_KEY_HEADER not in seen
    finally:
        console.shutdown()
        console.server_close()
