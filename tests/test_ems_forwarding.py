# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Admin console passes the EMS dashboard's API through, and nothing more.

The one browser app is served by the Admin console, so the dashboard's pages
call the EMS through the console's own address. What these tests hold:

- the forwarded paths are exactly the ones the dashboard server dispatches,
  and none of the console's own;
- a request reaches the EMS with its body, its EMS cookie and its CSRF token,
  and with nothing the client was not meant to send it; the answer comes back
  with the console's security headers and nothing the EMS was not meant to say;
- the forwarding needs no console session and adds no authority: the EMS
  decides every request as it decides a direct one;
- the live stream arrives event by event, not when it ends;
- an EMS that is not set up, switched off or silent is said as such;
- the EMS counts a forwarded client as that client only with the forwarding key;
- the console tells the EMS where it answers, for the EMS's own dashboard.
"""

import json
import os
import re
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from admin.ems_forwarding import (
    MAX_FORWARD_BODY_BYTES,
    EmsUpstream,
    ForwardingRefusal,
    LINK_COOKIE_NAME,
    LINK_ENDED,
    EmsSessions,
    ems_host,
    link_cookie,
    one_line,
    ems_upstream,
    installed_ems,
    secure_cookie,
    shares_host_network,
)
from admin.server import create_server
from dashboard.api_paths import EMS_API_PATHS, EMS_API_PREFIXES, is_ems_api_path
from dashboard.console_address import (
    ConsoleCheck,
    console_answers,
    CONSOLE_ADDRESS_NAME,
    console_address,
    publish_console_address,
    read_console_address,
)
from dashboard.host_reach import default_gateway, host_candidates, in_bridge_network
from dashboard.forwarding import FORWARDING_KEY_HEADER, FORWARDING_KEY_NAME, ForwardingKey, forwarded_client
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
    [link] = headers.get_all("Set-Cookie")
    assert re.fullmatch(rf"{LINK_COOKIE_NAME}=[A-Za-z0-9_-]{{43}}; Path=/; HttpOnly; SameSite=Strict", link)
    seen = fake_ems.seen[0]["headers"]
    assert "Cookie" not in seen
    assert seen["X-Forwarded-For"] == "127.0.0.1"
    assert seen[FORWARDING_KEY_HEADER] == KEY
    assert "Authorization" not in seen
    _call(f"{base}/api/live", headers={"Cookie": f"ems_admin_session=adm; {link.split(';', 1)[0]}"})
    assert fake_ems.seen[1]["headers"]["Cookie"] == "ems_dashboard_session=abc"


def test_a_write_reaches_the_ems_unchanged_and_needs_no_console_session(console, fake_ems):
    _, base = console
    body = json.dumps({"password": "pw"}).encode()
    status, _, answer = _call(
        f"{base}/api/auth/login", method="POST", body=body,
        headers={"Content-Type": "application/json", "X-CSRF-Token": "t1"},
    )
    assert status == 201
    assert json.loads(answer)["answered"] == "/api/auth/login"
    seen = fake_ems.seen[0]
    assert seen["body"] == body
    assert seen["headers"]["X-CSRF-Token"] == "t1"
    assert seen["headers"]["Content-Type"] == "application/json"


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
    return SimpleNamespace(config_exists=True, config_path=config, install_root=tmp_path)


def test_the_ems_is_found_from_the_installed_dashboard_settings(tmp_path):
    assert ems_upstream(_context(tmp_path, {"port": 8085})) == EmsUpstream(port=8085)
    assert ems_upstream(_context(tmp_path, {})) == EmsUpstream(port=8080)
    assert ems_upstream(_context(tmp_path, {"port": "8086"})) == EmsUpstream(port=8086)
    assert ems_upstream(_context(tmp_path, {"port": 70000})) == EmsUpstream(port=8080)
    assert ems_upstream(_context(tmp_path, {"ssl_enabled": "true"})).https
    assert ems_upstream(_context(tmp_path, {"enabled": "yes"})) == EmsUpstream(port=8080)
    secure = ems_upstream(_context(tmp_path, {"ssl_enabled": True}))
    assert secure == EmsUpstream(port=8080, https=True, cafile=tmp_path / "config" / "dashboard.crt")
    for off in (False, "false", "off"):
        with pytest.raises(ForwardingRefusal) as refused:
            ems_upstream(_context(tmp_path, {"enabled": off}))
        assert (refused.value.code, refused.value.status) == ("ems_dashboard_disabled", 503)
    with pytest.raises(ForwardingRefusal) as refused:
        ems_upstream(SimpleNamespace(config_exists=False))
    assert refused.value.code == "ems_not_configured"


def test_a_cookie_is_secure_exactly_when_the_browser_s_connection_is():
    cookie = "ems_dashboard_session=abc; Path=/; HttpOnly; SameSite=Strict; Secure"
    assert secure_cookie(cookie, False) == "ems_dashboard_session=abc; Path=/; HttpOnly; SameSite=Strict"
    assert secure_cookie(cookie, True) == cookie
    assert secure_cookie("a=b; Path=/", True) == "a=b; Path=/; Secure"


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


def test_the_browser_holds_a_link_of_the_console_s_making_never_the_ems_session():
    sessions = EmsSessions()
    assert sessions.adopt(None, ["ems_admin_session=planted; Path=/"]) is None
    link = sessions.adopt(None, ["ems_dashboard_session=s1; Path=/; HttpOnly; SameSite=Strict; Secure"])
    assert link and "s1" not in link
    assert sessions.session_for(f"x=1; {LINK_COOKIE_NAME}={link}") == "s1"
    assert sessions.session_for("ems_dashboard_session=s1") is None
    renewed = sessions.adopt(f"{LINK_COOKIE_NAME}={link}", ["ems_dashboard_session=s2; Path=/"])
    assert renewed != link
    assert sessions.session_for(f"{LINK_COOKIE_NAME}={link}") is None
    assert sessions.session_for(f"{LINK_COOKIE_NAME}={renewed}") == "s2"
    ended = sessions.adopt(f"{LINK_COOKIE_NAME}={renewed}", ["ems_dashboard_session=; Path=/; Max-Age=0; HttpOnly"])
    assert ended is LINK_ENDED
    assert sessions.session_for(f"{LINK_COOKIE_NAME}={renewed}") is None
    assert link_cookie(LINK_ENDED, True) == f"{LINK_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict; Secure"
    assert one_line("a\r\nb") == "a  b"


def test_only_the_newest_links_are_kept():
    sessions = EmsSessions(limit=2)
    links = [sessions.adopt(None, [f"ems_dashboard_session=s{index}"]) for index in range(3)]
    assert [sessions.session_for(f"{LINK_COOKIE_NAME}={link}") for link in links] == [None, "s1", "s2"]


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


class _OlderAdmin(BaseHTTPRequestHandler):
    """An Admin console from before it carried the dashboard: no ``/api/console``."""

    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_the_ems_dashboard_names_the_console_only_while_it_answers_as_one(tmp_path):
    config = tmp_path / "config" / "config.json"
    config.parent.mkdir()
    config.write_text("{}", encoding="utf-8")
    server = start_dashboard_server(
        _Store(), host="127.0.0.1", port=0, config_path=str(config), auth_file=str(tmp_path / "auth.json"),
    )
    base = f"http://127.0.0.1:{server.server_address[1]}"
    current = create_server("127.0.0.1", 0)
    threading.Thread(target=current.serve_forever, daemon=True).start()
    older = _serve(_OlderAdmin)
    gone = socket.socket()
    gone.bind(("127.0.0.1", 0))
    gone_port = gone.getsockname()[1]
    gone.close()
    try:
        assert json.loads(_call(f"{base}/api/ui-config")[2])["admin_console"] is None
        for port, named in ((current.server_address[1], True), (older.server_address[1], False), (gone_port, False)):
            server.console_check = ConsoleCheck(answers=lambda port: console_answers(port, ["127.0.0.1"]))
            publish_console_address(config.parent, console_address(port))
            expected = {"port": port, "https_port": None} if named else None
            assert json.loads(_call(f"{base}/api/ui-config")[2])["admin_console"] == expected, port
    finally:
        for running in (server, current, older):
            running.shutdown()
            running.server_close()


def test_the_console_says_signed_out_that_it_carries_the_dashboard(console):
    _, base = console
    status, _, body = _call(f"{base}/api/console")
    assert (status, json.loads(body)) == (200, {"console": "ems-solarflow-admin", "dashboard": True})


def test_a_console_check_is_asked_again_only_after_its_time():
    asked = []
    now = [0.0]
    check = ConsoleCheck(answers=lambda port: asked.append(port) or True, clock=lambda: now[0])
    assert check({"port": 8090}) and check({"port": 8090})
    assert asked == [8090]
    now[0] = 31.0
    check({"port": 8090})
    assert asked == [8090, 8090]


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
        server.console_check = lambda record: True
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
