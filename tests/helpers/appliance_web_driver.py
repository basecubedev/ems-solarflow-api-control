#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drive the real Appliance Manager web service over HTTP as the packaged account.

Started by :mod:`tests.helpers.appliance_permissions` inside a container as
``ems-appliance-web``. It binds the production ``ApplianceWebServer`` to the
loopback address, walks the authentication endpoints and prints one JSON report
line, so the test sees exactly what a browser on a real appliance would see.
"""

import json
import sys
import threading
import urllib.error
import urllib.request

from appliance.agent_client import AgentClient, AgentUnavailableError
from appliance.auth import AuthStore
from appliance.config import load_config
from appliance.paths import ensure_directories, resolve_paths
from appliance.web import ApplianceWebApp, ApplianceWebServer

PASSWORD = "appliance-permission-probe"
OTHER_PASSWORD = "appliance-permission-probe-2"
WRONG_PASSWORD = "definitely-not-the-password"

REPORT_MARKER = "APPLIANCE_REPORT:"


class ScriptedAgent:
    """Stands in for the agent socket and records every typed call it receives.

    The four auth operations are answered from a real AuthStore, because the
    shared password moved behind this boundary: the web tier no longer reads
    the file at all, so a stub that only records calls would make every login
    fail for a reason that has nothing to do with what is being tested. The
    store sits on a web-owned path -- the real one is root-owned in the
    deployment root, which this process must not be able to write, and proving
    that is the live-agent scenario's job.
    """

    def __init__(self, *, reachable=True, auth_path=None):
        self.reachable = reachable
        self.calls = []
        paths = resolve_paths()
        self.auth_path = auth_path or (paths.web_state_dir / "scripted-auth.json")
        self.auth = AuthStore(self.auth_path, iterations=1000)

    def call(self, operation, *, actor="", source_ip="", timeout=None, **fields):
        entry = {"operation": operation, "actor": actor, "source_ip": source_ip}
        entry.update(fields)
        self.calls.append(entry)
        if not self.reachable:
            raise AgentUnavailableError("the appliance agent is not reachable")
        return self._auth(operation, fields)

    def _auth(self, operation, fields):
        """Mirrors appliance/agent.py, which is the only other implementation."""

        if operation == "auth.state":
            return {"configured": self.auth.configured(), "generation": self.auth.generation()}
        if operation == "auth.verify":
            return {"ok": bool(self.auth.verify(fields["password"]))}
        if operation == "auth.create":
            self.auth.create(fields["password"], fields.get("confirmation") or None)
            return {"generation": self.auth.generation()}
        if operation == "auth.change":
            self.auth.change(
                fields["current_password"],
                fields["password"],
                fields.get("confirmation") or None,
            )
            return {"generation": self.auth.generation()}
        return {"recorded": True}

    def available(self):
        return self.reachable


class Client:
    def __init__(self, port):
        self.port = port
        self.cookie = ""
        self.csrf = ""

    def request(self, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self.cookie:
            request.add_header("Cookie", self.cookie)
        if self.csrf:
            request.add_header("X-Appliance-CSRF", self.csrf)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read().decode("utf-8")
                record = {
                    "completed": True,
                    "status": int(response.status),
                    "body": json.loads(raw) if raw.strip() else {},
                }
                self._absorb(response.headers.get("Set-Cookie") or "", record["body"])
                return record
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            return {
                "completed": True,
                "status": int(exc.code),
                "body": json.loads(raw) if raw.strip() else {},
            }
        except Exception as exc:
            return {
                "completed": False,
                "status": 0,
                "exception": type(exc).__name__,
                "message": str(exc)[:200],
                "body": {},
            }

    def _absorb(self, set_cookie, body):
        if set_cookie and "Max-Age=0" not in set_cookie:
            self.cookie = set_cookie.split(";", 1)[0]
        elif set_cookie:
            self.cookie = ""
        token = (body or {}).get("csrf_token")
        if token:
            self.csrf = token


def start_server(agent):
    paths = resolve_paths()
    ensure_directories(paths, role="web")
    app = ApplianceWebApp(paths=paths, config=load_config(paths), agent=agent)
    server = ApplianceWebServer(app, ("127.0.0.1", 0))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, app


def authentication_scenario(agent):
    """Setup, login, logout, password change and the rate limiter, in order."""

    server, app = start_server(agent)
    client = Client(server.server_address[1])
    report = {}
    try:
        report["setup"] = client.request("POST", "/api/session/setup",
                                         {"password": PASSWORD, "confirmation": PASSWORD})
        report["auth_file_exists"] = getattr(agent, "auth_path", app.paths.auth_file).is_file()
        report["session_after_setup"] = client.request("GET", "/api/session")

        report["logout"] = client.request("POST", "/api/session/logout")
        report["login_failure"] = client.request("POST", "/api/session/login",
                                                 {"password": WRONG_PASSWORD})
        report["login_success"] = client.request("POST", "/api/session/login",
                                                 {"password": PASSWORD})
        report["password_change"] = client.request(
            "POST",
            "/api/settings/password",
            {
                "current_password": PASSWORD,
                "password": OTHER_PASSWORD,
                "confirmation": OTHER_PASSWORD,
            },
        )

        client.cookie = ""
        client.csrf = ""
        attempts = []
        for _ in range(6):
            attempts.append(client.request("POST", "/api/session/login",
                                           {"password": WRONG_PASSWORD}))
        report["rate_limit_attempts"] = attempts
        report["rate_limited"] = attempts[-1]
    finally:
        report["agent_calls"] = getattr(agent, "calls", [])
        report["audit_status"] = app.audit_status()
        server.shutdown()
        server.server_close()
    return report


def state_access_scenario(_agent):
    """Try to read agent-owned state directly, the way a compromised web process would."""

    paths = resolve_paths()
    targets = {
        "agent_state_dir": paths.agent_state_dir,
        "operations_dir": paths.operations_dir,
        "known_good_dir": paths.known_good_dir,
        "agent_log_dir": paths.agent_log_dir,
        "audit_log_dir": paths.audit_log_dir,
        "audit_log": paths.audit_log,
        "agent_log": paths.agent_log,
        "operations_log": paths.operations_log,
    }
    report = {}
    for name, path in targets.items():
        entry = {"path": str(path), "listed": False, "read": False, "error": ""}
        try:
            if path.is_dir():
                entry["entries"] = sorted(item.name for item in path.iterdir())
                entry["listed"] = True
            else:
                entry["content"] = path.read_text(encoding="utf-8", errors="replace")[:4096]
                entry["read"] = True
        except OSError as exc:
            entry["error"] = type(exc).__name__
        report[name] = entry

    tokens = []
    try:
        for record in sorted(paths.operations_dir.glob("*.json")):
            payload = json.loads(record.read_text(encoding="utf-8"))
            if payload.get("confirmation_token"):
                tokens.append(payload["confirmation_token"])
    except OSError as exc:
        report["token_scan_error"] = type(exc).__name__
    report["confirmation_tokens_read"] = tokens
    return report


def live_agent():
    return AgentClient(resolve_paths().agent_socket, timeout=30)


SCENARIOS = {
    "authentication": (authentication_scenario, True),
    "authentication_agent_down": (authentication_scenario, False),
    # Talks to a real privileged agent over the Unix socket, so the audit entry
    # is written by root through the typed operation and nothing else.
    "authentication_live_agent": (authentication_scenario, None),
    "state_access": (state_access_scenario, True),
}


def main(argv):
    name = argv[1] if len(argv) > 1 else "authentication"
    scenario, reachable = SCENARIOS[name]
    agent = live_agent() if reachable is None else ScriptedAgent(reachable=reachable)
    try:
        report = scenario(agent)
        report["scenario_error"] = ""
    except Exception as exc:  # the report itself must survive a driver failure
        report = {"scenario_error": f"{type(exc).__name__}: {exc}"}
    print(REPORT_MARKER + json.dumps(report, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
