# SPDX-License-Identifier: AGPL-3.0-or-later
"""The unprivileged Appliance Manager web service.

This process runs as a normal user. It owns authentication, sessions, CSRF and
rendering; every privileged action is a typed call to the agent socket. It has
no Docker socket, no root and no way to run a host command, so a flaw here
cannot escalate beyond the agent's fixed allowlist.
"""

import base64
import hashlib
import json
import os
import posixpath
import re
import secrets
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from appliance import validation
from appliance.agent_client import AgentCallError, AgentClient, AgentUnavailableError
from appliance.auth import (
    CSRF_HEADER,
    SESSION_COOKIE_NAME,
    AuthError,
    AuthStore,
    LoginRateLimiter,
    SessionStore,
)
from appliance.audit import RESULT_DENIED, RESULT_FAILURE, RESULT_SUCCESS, WebLog
from appliance.config import load_config
from appliance.paths import ensure_directories, resolve_paths
from appliance.version import APPLIANCE_VERSION
from appliance.web_audit import WebAuditReporter

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_FILES = {
    "app.js": "application/javascript; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
}
MAX_BODY_BYTES = 64 * 1024

# A peer that connects and never finishes a request line would otherwise hold
# a worker thread for ever, and the appliance UI is the only recovery path
# there is when an Admin install or a rollback has gone wrong.
CONNECTION_TIMEOUT_SECONDS = 15

TEST_MODE_ENV = "EMS_APPLIANCE_TEST_MODE"
TEST_RESET_PATH = "/api/test/reset"

STATUS_FOR_CODE = {
    "support_archive_not_found": 404,
    "support_archive_too_large": 409,
    "agent_unavailable": 503,
    "operation_conflict": 409,
    "confirmation_token_mismatch": 403,
    "peer_not_allowed": 403,
    "unknown_operation_id": 404,
    "unknown_operation": 400,
    "timezone_unchanged": 409,
}

_OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")


class ApplianceWebApp:
    """Routing, session handling and agent delegation."""

    def __init__(self, *, paths=None, config=None, agent=None, auth=None, sessions=None,
                 rate_limiter=None, audit=None, time_fn=None):
        self.paths = paths or resolve_paths()
        self.config = config or load_config(self.paths)
        self.agent = agent or AgentClient(self.paths.agent_socket)
        self.auth = auth or AuthStore(self.paths.auth_file, time_fn=time_fn)
        self.sessions = sessions or SessionStore(
            idle_timeout=self.config.session_timeout_seconds,
            absolute_max=self.config.session_absolute_max_seconds,
            time_fn=time_fn,
        )
        self.rate_limiter = rate_limiter or LoginRateLimiter(time_fn=time_fn)
        self.web_log = WebLog(self.paths.appliance_log, time_fn=time_fn)
        self.audit = audit or WebAuditReporter(
            self.agent, log=self.web_log, time_fn=time_fn
        )
        self._time = time_fn or time.time
        self._lock = threading.Lock()
        # Browser tests need a deterministic reset. The endpoint only exists
        # when the host explicitly starts the service in test mode.
        self.test_mode = os.environ.get(TEST_MODE_ENV) == "1"
        self.test_reset_hook = None

    def names_this_appliance(self, host):
        """Whether a Host header names an address this appliance answers to.

        Loopback and the configured hostname, with or without the mDNS suffix
        and with any port. Anything else is a request that reached us under a
        name somebody else controls, which is what DNS rebinding produces.
        """

        name = (host or "").strip().lower()
        if not name:
            return False
        if name.startswith("["):
            name = name.partition("]")[0].lstrip("[")
        elif name.count(":") == 1:
            name = name.partition(":")[0]
        if name in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            return True
        try:
            configured = str(self.probe_hostname() or "").strip().lower()
        except Exception:
            configured = ""
        if configured and name in (configured, f"{configured}.local"):
            return True
        # An address literal is the appliance's own LAN address as typed by the
        # operator; a name we cannot resolve to ourselves is not.
        return bool(re.fullmatch(r"[0-9.]+", name)) or bool(re.fullmatch(r"[0-9a-f:]+", name))

    def probe_hostname(self):
        import socket

        return socket.gethostname()

    # --- session ---------------------------------------------------------

    def session_for(self, session_id):
        return self.sessions.get(session_id, self.auth.generation())

    def login(self, password, *, source_ip):
        """The lock serialises the rate limiter and the session store, nothing else.

        Deriving the password hash costs hundreds of milliseconds on a Pi and
        recording an audit event is a round trip to the agent; holding the lock
        across either lets a handful of unauthenticated attempts make the login
        page unusable for the operator.
        """

        with self._lock:
            limited = self.rate_limiter.limited(source_ip)
            retry_after = self.rate_limiter.retry_after(source_ip) if limited else 0
        if limited:
            self.audit.record(
                "login.failure",
                source_ip=source_ip,
                result=RESULT_DENIED,
                reason="rate_limited",
            )
            raise AuthError(
                "login_rate_limited",
                f"too many failed attempts; try again in {retry_after} seconds",
            )

        verified = self.auth.verify(password)

        if not verified:
            with self._lock:
                self.rate_limiter.record_failure(source_ip)
            self.audit.record(
                "login.failure",
                source_ip=source_ip,
                result=RESULT_FAILURE,
                reason="invalid_password",
            )
            raise AuthError("invalid_credentials", "the appliance password is not correct")

        with self._lock:
            self.rate_limiter.reset(source_ip)
            session = self.sessions.create(self.auth.generation())
        self.audit.record("login.success", source_ip=source_ip, result=RESULT_SUCCESS)
        return session

    def logout(self, session_id, *, source_ip):
        self.sessions.destroy(session_id)
        self.audit.record(
            "logout", source_ip=source_ip, result=RESULT_SUCCESS, reason="session_ended"
        )

    def create_first_password(self, password, confirmation, *, source_ip):
        record = self.auth.create(password, confirmation)
        self.audit.record("password.change", source_ip=source_ip, reason="first_password")
        return self.sessions.create(record["generation"])

    def change_password(self, current, new_password, confirmation, *, source_ip):
        self.auth.change(current, new_password, confirmation)
        self.sessions.destroy_all()
        self.audit.record(
            "password.change",
            source_ip=source_ip,
            result=RESULT_SUCCESS,
            reason="password_changed",
        )

    def audit_status(self):
        return self.audit.status()

    # --- agent -----------------------------------------------------------

    def call(self, operation, *, session=None, source_ip="", **fields):
        return self.agent.call(
            operation, actor="appliance-admin" if session else "", source_ip=source_ip, **fields
        )


class ApplianceRequestHandler(BaseHTTPRequestHandler):
    server_version = f"EMSApplianceManager/{APPLIANCE_VERSION}"
    protocol_version = "HTTP/1.1"
    timeout = CONNECTION_TIMEOUT_SECONDS

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except TimeoutError:
            self.close_connection = True

    @property
    def app(self):
        return self.server.app

    # --- helpers ---------------------------------------------------------

    def log_message(self, fmt, *args):
        return

    def _client_ip(self):
        return self.client_address[0] if self.client_address else ""

    def _session(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = cookies.SimpleCookie()
        try:
            jar.load(raw)
        except cookies.CookieError:
            return None
        morsel = jar.get(SESSION_COOKIE_NAME)
        if morsel is None:
            return None
        return self.app.session_for(morsel.value)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("malformed JSON body")
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        return payload

    def _send(self, status, payload, *, extra_headers=()):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status, code, message, **extra):
        payload = {"error": code, "message": message}
        payload.update(extra)
        self._send(status, payload)

    def _session_cookie(self, session, *, clear=False):
        parts = [
            f"{SESSION_COOKIE_NAME}={'' if clear else session.session_id}",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
        ]
        if clear:
            parts.append("Max-Age=0")
        if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https":
            parts.append("Secure")
        return ("Set-Cookie", "; ".join(parts))

    def _require_session(self):
        session = self._session()
        if session is None:
            self._error(401, "authentication_required", "sign in to use the Appliance Manager")
            return None
        self.app.sessions.touch(session.session_id, self.app.auth.generation())
        return session

    def _require_csrf(self, session):
        token = self.headers.get(CSRF_HEADER) or ""
        if not token or not secrets.compare_digest(token, session.csrf_token or ""):
            self._error(403, "csrf_token_invalid", "the CSRF token is missing or invalid")
            return False
        host = (self.headers.get("Host") or "").split("/", 1)[0]
        if not self.app.names_this_appliance(host):
            # Under DNS rebinding the browser makes Origin and Host agree on the
            # attacker's name, so comparing them proves nothing on its own.
            self._error(403, "csrf_host_rejected", "the request names another host")
            return False
        origin = self.headers.get("Origin")
        if origin:
            # Compare the origin's authority exactly. A suffix comparison would
            # accept "http://evil-<host>", which ends with the real host. An
            # absent Origin is left to the token and the SameSite cookie: a
            # browser always sends one, and refusing without it would only lock
            # out non-browser callers that already hold a 256-bit token.
            _, _, authority = origin.partition("://")
            if not authority or authority.split("/", 1)[0] != host:
                self._error(403, "csrf_origin_rejected", "the request origin is not accepted")
                return False
        return True

    def _agent(self, operation, session, **fields):
        try:
            return self.app.call(
                operation, session=session, source_ip=self._client_ip(), **fields
            )
        except AgentUnavailableError as exc:
            self._error(503, "agent_unavailable", exc.message)
        except AgentCallError as exc:
            status = STATUS_FOR_CODE.get(exc.code, 400)
            self._error(status, exc.code, exc.message, field=exc.field)
        return None

    # --- routing ---------------------------------------------------------

    def do_GET(self):
        path = posixpath.normpath(self.path.split("?", 1)[0])
        if path in ("/", "/index.html"):
            return self._serve_index()
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/") :])
        if path == "/api/session":
            return self._session_state()
        if path.startswith("/api/support/archive/"):
            return self._download_support_archive(path[len("/api/support/archive/") :])
        if path.startswith("/api/"):
            return self._api_get(path)
        return self._error(404, "not_found", "unknown path")

    def do_POST(self):
        path = posixpath.normpath(self.path.split("?", 1)[0])
        try:
            body = self._body()
        except ValueError as exc:
            return self._error(400, "invalid_request", str(exc))

        if path == TEST_RESET_PATH:
            return self._test_reset(body)
        if path == "/api/session/setup":
            return self._setup_password(body)
        if path == "/api/session/login":
            return self._login(body)
        if path == "/api/session/logout":
            return self._logout()
        if path.startswith("/api/"):
            return self._api_post(path, body)
        return self._error(404, "not_found", "unknown path")

    def do_DELETE(self):
        path = posixpath.normpath(self.path.split("?", 1)[0])
        session = self._require_session()
        if session is None:
            return None
        if not self._require_csrf(session):
            return None
        if path.startswith("/api/ssh/keys/"):
            fingerprint = self._decode_fingerprint(path[len("/api/ssh/keys/") :])
            account = self.headers.get("X-Appliance-Account") or self.app.config.backup_user
            result = self._agent(
                "ssh.plan_key_remove", session, account=account, fingerprint=fingerprint
            )
            return None if result is None else self._send(200, result)
        return self._error(404, "not_found", "unknown path")

    @staticmethod
    def _decode_fingerprint(raw):
        return raw.replace("%3A", ":").replace("%2B", "+").replace("%2F", "/")

    # --- session endpoints ----------------------------------------------

    def _session_state(self):
        session = self._session()
        payload = {
            "authenticated": session is not None,
            "password_configured": self.app.auth.configured(),
            "appliance_version": APPLIANCE_VERSION,
        }
        if session is not None:
            payload["csrf_token"] = session.csrf_token
            payload["expires_at"] = session.expires_at
            # Only an authenticated caller learns anything about the host.
            payload["security_audit"] = self.app.audit_status()
        return self._send(200, payload)

    def _test_reset(self, body):
        if not self.app.test_mode:
            return self._error(404, "not_found", "unknown path")
        if body.get("expire_sessions"):
            self.app.sessions.destroy_all()
            return self._send(200, {"reset": "sessions"})
        self.app.sessions.destroy_all()
        self.app.rate_limiter.failures.clear()
        try:
            self.app.paths.auth_file.unlink()
        except OSError:
            pass
        # Agent state is not the web service's to touch, not even in test mode:
        # the harness clears operation records through its own privileged hook.
        if self.app.test_reset_hook is not None:
            self.app.test_reset_hook(body)
        return self._send(200, {"reset": "appliance"})

    def _setup_password(self, body):
        if self.app.auth.configured():
            return self._error(
                409, "password_already_configured", "an appliance password already exists"
            )
        try:
            session = self.app.create_first_password(
                body.get("password"), body.get("confirmation"), source_ip=self._client_ip()
            )
        except AuthError as exc:
            return self._error(400, exc.code, exc.message)
        return self._send(
            200,
            {
                "authenticated": True,
                "csrf_token": session.csrf_token,
                "security_audit": self.app.audit_status(),
            },
            extra_headers=[self._session_cookie(session)],
        )

    def _login(self, body):
        try:
            session = self.app.login(body.get("password"), source_ip=self._client_ip())
        except AuthError as exc:
            status = 429 if exc.code == "login_rate_limited" else 401
            return self._error(status, exc.code, exc.message)
        return self._send(
            200,
            {
                "authenticated": True,
                "csrf_token": session.csrf_token,
                "security_audit": self.app.audit_status(),
            },
            extra_headers=[self._session_cookie(session)],
        )

    def _logout(self):
        session = self._session()
        if session is not None:
            self.app.logout(session.session_id, source_ip=self._client_ip())
        return self._send(
            200,
            {"authenticated": False},
            extra_headers=[self._session_cookie(session, clear=True)] if session else (),
        )

    # --- read-only API ---------------------------------------------------

    def _api_get(self, path):
        session = self._require_session()
        if session is None:
            return None

        routes = {
            "/api/status": ("status.get", {}),
            "/api/system": ("system.get", {}),
            "/api/network": ("network.get", {}),
            "/api/docker": ("docker.get", {}),
            "/api/admin": ("admin.get", {}),
            "/api/admin/releases": ("admin.releases", {}),
            "/api/updates": ("updates.get", {}),
            "/api/ab": ("ab.status", {}),
            "/api/ab/sources": ("ab.sources", {}),
            "/api/ssh/keys": ("ssh.get", {}),
            "/api/backup": ("backup.get", {}),
            "/api/operations": ("operations.list", {}),
            "/api/network/wifi/scan": ("network.wifi.scan", {}),
        }
        if path in routes:
            operation, fields = routes[path]
            result = self._agent(operation, session, **fields)
            return None if result is None else self._send(200, result)

        if path == "/api/settings":
            return self._send(200, self._settings())

        if path.startswith("/api/operations/"):
            operation_id = path[len("/api/operations/") :]
            if not _OPERATION_ID.match(operation_id):
                return self._error(400, "invalid_operation_id", "malformed operation id")
            result = self._agent("operations.get", session, operation_id=operation_id)
            return None if result is None else self._send(200, result)

        if path.startswith("/api/logs/"):
            source = path[len("/api/logs/") :]
            lines = self._query_int("lines", validation.DEFAULT_LOG_LINES)
            result = self._agent("logs.read", session, source=source, lines=lines)
            return None if result is None else self._send(200, result)

        return self._error(404, "not_found", "unknown path")

    def _query_int(self, name, default):
        _, _, query = self.path.partition("?")
        for pair in query.split("&"):
            key, _, value = pair.partition("=")
            if key == name and value.isdigit():
                return int(value)
        return default

    def _settings(self):
        config = self.app.config
        return {
            "appliance_version": APPLIANCE_VERSION,
            "session_timeout_seconds": config.session_timeout_seconds,
            "session_absolute_max_seconds": config.session_absolute_max_seconds,
            "automatic_security_updates": config.automatic_security_updates,
            "admin_repository": config.images.admin_repository,
            "allow_prerelease": config.images.allow_prerelease,
            "backup_user": config.backup_user,
            "ssh_key_accounts": list(config.ssh_key_accounts),
            "web_port": config.web_port,
            "admin_port": config.admin_port,
            "security_audit": self.app.audit_status(),
            "configuration_file": str(self.app.paths.appliance_conf),
            "note": "Host settings are owned by the appliance configuration file and are "
            "read-only in the browser.",
        }

    # --- mutating API ----------------------------------------------------

    def _api_post(self, path, body):
        session = self._require_session()
        if session is None:
            return None
        if not self._require_csrf(session):
            return None

        if path == "/api/settings/password":
            try:
                self.app.change_password(
                    body.get("current_password"),
                    body.get("password"),
                    body.get("confirmation"),
                    source_ip=self._client_ip(),
                )
            except AuthError as exc:
                return self._error(400, exc.code, exc.message)
            return self._send(
                200,
                {
                    "changed": True,
                    "sessions_invalidated": True,
                    "security_audit": self.app.audit_status(),
                },
            )

        plan_routes = {
            "/api/admin/plan-install": ("admin.plan_install", self._install_fields),
            "/api/admin/rollback": ("admin.plan_rollback", lambda _: {}),
            "/api/admin/repair": ("admin.plan_repair", lambda _: {}),
            "/api/admin/start": ("admin.plan_lifecycle", lambda _: {"action": "start"}),
            "/api/admin/stop": ("admin.plan_lifecycle", lambda _: {"action": "stop"}),
            "/api/admin/restart": ("admin.plan_lifecycle", lambda _: {"action": "restart"}),
            "/api/updates/plan": ("updates.plan", lambda b: {"scope": b.get("scope")}),
            "/api/updates/repair": ("updates.plan_repair", lambda b: {"action": b.get("action")}),
            # The browser sends a release id and nothing else. Every device
            # path, PARTUUID, URL, key and partition number comes from the
            # root-owned configuration, the signed manifest or layout discovery.
            "/api/ab/plan-update": (
                "ab.plan_update",
                lambda b: {"release_id": b.get("release_id"), "repair": bool(b.get("repair"))},
            ),
            "/api/ab/plan-rollback": ("ab.plan_rollback", lambda _: {}),
            "/api/ab/plan-fetch": (
                "ab.plan_fetch",
                lambda b: {"release_id": b.get("release_id")},
            ),
            "/api/ssh/enable": ("ssh.plan_service", lambda _: {"enabled": True}),
            "/api/ssh/disable": ("ssh.plan_service", lambda _: {"enabled": False}),
            "/api/ssh/keys": ("ssh.plan_key_add", self._key_fields),
            "/api/ssh/keys/remove-plan": ("ssh.plan_key_remove", self._fingerprint_fields),
            "/api/ssh/keys/revoke": ("ssh.plan_revoke_all", self._account_fields),
            "/api/network/wifi/plan": ("network.wifi.plan", self._wifi_fields),
            "/api/network/hostname": (
                "network.hostname.plan",
                lambda b: {"hostname": b.get("hostname")},
            ),
            "/api/system/timezone": (
                "system.timezone.plan",
                lambda b: {"timezone": b.get("timezone")},
            ),
            "/api/system/reboot": ("system.plan_reboot", lambda _: {}),
            "/api/system/shutdown": ("system.plan_shutdown", lambda _: {}),
            "/api/support/archive": ("support.plan_archive", lambda _: {}),
        }

        if path in plan_routes:
            operation, builder = plan_routes[path]
            result = self._agent(operation, session, **builder(body))
            return None if result is None else self._send(200, result)

        confirm_paths = (
            "/api/admin/execute-install",
            "/api/updates/install",
            "/api/ab/execute",
            "/api/network/wifi/apply",
            "/api/operations/confirm",
        )
        if path in confirm_paths:
            return self._confirm(session, body)

        if path == "/api/ab/acknowledge":
            result = self._agent(
                "ab.acknowledge", session, operation_id=body.get("operation_id")
            )
            return None if result is None else self._send(200, result)

        if path == "/api/operations/cancel":
            result = self._agent(
                "operations.cancel", session, operation_id=body.get("operation_id")
            )
            return None if result is None else self._send(200, result)

        if path == "/api/operations/acknowledge":
            result = self._agent(
                "operations.acknowledge", session, operation_id=body.get("operation_id")
            )
            return None if result is None else self._send(200, result)

        return self._error(404, "not_found", "unknown path")

    def _confirm(self, session, body):
        result = self._agent(
            "operations.execute",
            session,
            operation_id=body.get("operation_id"),
            confirmation_token=body.get("confirmation_token"),
        )
        return None if result is None else self._send(200, result)

    @staticmethod
    def _install_fields(body):
        fields = {
            "channel": body.get("channel"),
            "reinstall": bool(body.get("reinstall", False)),
        }
        if body.get("tag"):
            fields["tag"] = body.get("tag")
        return fields

    @staticmethod
    def _key_fields(body):
        return {
            "account": body.get("account"),
            "public_key": body.get("public_key"),
        }

    @staticmethod
    def _fingerprint_fields(body):
        return {"account": body.get("account"), "fingerprint": body.get("fingerprint")}

    @staticmethod
    def _account_fields(body):
        return {"account": body.get("account")}

    @staticmethod
    def _wifi_fields(body):
        return {
            "ssid": body.get("ssid"),
            "passphrase": body.get("passphrase", ""),
            "hidden": bool(body.get("hidden", False)),
        }

    # --- static ----------------------------------------------------------

    def _serve_index(self):
        return self._serve_file("index.html", "text/html; charset=utf-8")

    def _download_support_archive(self, operation_id):
        """Hand the operator the archive the docs tell them to attach.

        On an A/B image there is no shell and the archive lives in root-owned
        agent state, so without this route it could be created and never
        retrieved. The bytes come through the agent like every other privileged
        read; the web process never reaches that directory itself.
        """

        session = self._require_session()
        if session is None:
            return None
        if not re.fullmatch(r"[0-9a-f]{32}", operation_id or ""):
            return self._error(400, "invalid_request", "that is not an operation id")
        payload = self._agent("support.read_archive", session, operation_id=operation_id)
        if payload is None:
            return None
        try:
            body = base64.b64decode(payload["content_base64"], validate=True)
        except (KeyError, ValueError):
            return self._error(502, "agent_response_invalid", "the archive could not be read")
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Disposition", f'attachment; filename="{payload["name"]}"'
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return None

    def _serve_static(self, name):
        if name not in STATIC_FILES:
            return self._error(404, "not_found", "unknown asset")
        return self._serve_file(name, STATIC_FILES[name])

    def _serve_file(self, name, content_type):
        path = os.path.join(STATIC_DIR, name)
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError:
            return self._error(404, "not_found", "asset unavailable")
        etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return None
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Revalidate every time: an appliance-manager update changes app.js
        # against an API that changed with it, and a cached copy of the old one
        # is a UI that misreports the host it is talking to.
        self.send_header("Cache-Control", "no-cache")
        self.send_header("ETag", etag)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class ApplianceWebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, app, address):
        self.app = app
        super().__init__(address, ApplianceRequestHandler)


def build_server(*, paths=None, config=None, agent=None, address=None):
    paths = paths or resolve_paths()
    ensure_directories(paths, role="web")
    config = config or load_config(paths)
    app = ApplianceWebApp(paths=paths, config=config, agent=agent)
    bind = address or (config.web_address, config.web_port)
    return ApplianceWebServer(app, bind)


def serve(*, paths=None, config=None, agent=None, address=None):
    server = build_server(paths=paths, config=config, agent=agent, address=address)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0
