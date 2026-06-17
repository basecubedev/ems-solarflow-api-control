# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import logging
import mimetypes
import os
import tempfile
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlparse

from dashboard.auth import (
    SESSION_COOKIE_NAME,
    LoginRateLimiter,
    SessionStore,
    auth_configured,
    resolve_auth_path,
    verify_password_file,
)
from dashboard.runtime_write import (
    RuntimeWriteError,
    apply_device_update,
    apply_section_update,
    apply_system_update,
    attach_limits,
    build_validation_context,
    runtime_payload,
)
from dashboard.sqlite_store import SUPPORTED_RANGES


STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_JSON_BODY_BYTES = 16 * 1024
MAX_SSE_CONNECTIONS = 8
MAX_SSE_CONNECTIONS_PER_IP = 2
SSE_MAX_CONNECTION_SECONDS = 30 * 60
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'self'"
    ),
    "Permissions-Policy": (
        "geolocation=(), microphone=(), camera=(), payment=(), usb=()"
    ),
}


def _split_csv(value):
    """Split a comma-separated query value into a clean list of tokens."""
    if not value:
        return []
    return [token.strip() for token in value.split(",") if token.strip()]


def _parse_time_param(value):
    """Parse a custom-range bound: epoch seconds or ISO 8601, to aware UTC."""
    from datetime import datetime, timezone

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromtimestamp(float(text), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class JsonBodyTooLarge(ValueError):
    pass


class JsonBodyLengthError(ValueError):
    pass


class SSEConnectionLimiter:
    def __init__(self, max_global, max_per_ip):
        self.max_global = int(max_global)
        self.max_per_ip = int(max_per_ip)
        self.lock = threading.Lock()
        self.total = 0
        self.by_ip = {}

    def acquire(self, remote_addr):
        remote_addr = remote_addr or "unknown"
        with self.lock:
            if self.total >= self.max_global:
                return False
            if self.by_ip.get(remote_addr, 0) >= self.max_per_ip:
                return False
            self.total += 1
            self.by_ip[remote_addr] = self.by_ip.get(remote_addr, 0) + 1
            return True

    def release(self, remote_addr):
        remote_addr = remote_addr or "unknown"
        with self.lock:
            if self.total > 0:
                self.total -= 1
            current = self.by_ip.get(remote_addr, 0)
            if current <= 1:
                self.by_ip.pop(remote_addr, None)
            else:
                self.by_ip[remote_addr] = current - 1


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address,
        handler,
        store,
        runtime_state=None,
        auth_file=None,
        https_active=False,
        session_timeout_seconds=1800,
        session_absolute_max_seconds=43200,
        runtime_validation=None,
        config_path=None,
        runtime_state_path=None,
        log_buffer=None,
        log_redaction=False,
        animation_mode="normal",
        sse_max_connections=MAX_SSE_CONNECTIONS,
        sse_max_connections_per_ip=MAX_SSE_CONNECTIONS_PER_IP,
        sse_max_connection_seconds=SSE_MAX_CONNECTION_SECONDS,
    ):
        super().__init__(address, handler)
        self.store = store
        self.runtime_state = runtime_state
        self.log_buffer = log_buffer
        self.log_redaction = bool(log_redaction)
        # UI-only hint for the frontend bootstrap (no control/auth impact).
        self.animation_mode = (
            animation_mode if animation_mode in ("normal", "reduced", "off")
            else "normal"
        )
        self.auth_file = auth_file or resolve_auth_path(BASE_DIR)
        self.https_active = bool(https_active)
        # Paths the diagnose endpoints use to build service args. runtime_state_path
        # falls back to the RuntimeState object's own path when not supplied.
        self.config_path = config_path
        self.runtime_state_path = runtime_state_path or getattr(
            runtime_state, "path", None
        )
        # Single-flight guard so diagnose runs (esp. the hardware profile, which
        # makes network probes) cannot be hammered concurrently.
        self.diagnose_lock = threading.Lock()
        self.sessions = SessionStore(
            timeout_seconds=session_timeout_seconds,
            absolute_max_seconds=session_absolute_max_seconds,
        )
        self.login_limiter = LoginRateLimiter()
        self.runtime_validation = runtime_validation or build_validation_context(
            runtime_state=runtime_state
        )
        self.sse_limiter = SSEConnectionLimiter(
            sse_max_connections,
            sse_max_connections_per_ip,
        )
        self.sse_max_connection_seconds = int(sse_max_connection_seconds)
        # Two independent history sources, built lazily and cached:
        #
        # - ``sqlite_history_provider`` always reads the local SQLite snapshot
        #   store. It backs the lightweight Aggregate/Devices history charts
        #   (``/api/history/series``) and is always available, with no external
        #   dependency. InfluxDB never silently replaces it.
        # - ``analytics_provider`` is the optional InfluxDB-backed long-term
        #   analytics source (``/api/analytics/series``). It is ``None`` unless
        #   InfluxDB is explicitly enabled in config; the dashboard then shows a
        #   clean "not configured" state instead of broken charts.
        self._sqlite_provider = None
        self._analytics_provider = None
        self._analytics_built = False
        self._history_provider_lock = threading.Lock()

    def sqlite_history_provider(self):
        """SQLite snapshot provider for the operational history charts."""
        with self._history_provider_lock:
            if self._sqlite_provider is None:
                from ems.history.provider import SqliteHistoryProvider

                self._sqlite_provider = SqliteHistoryProvider(
                    getattr(self.store, "path", None)
                )
            return self._sqlite_provider

    def analytics_provider(self):
        """InfluxDB analytics provider, or ``None`` when not configured.

        Returns ``None`` (rather than falling back to SQLite) so the Analytics
        tab can render a dedicated unavailable state and the two data sources
        never get mixed.
        """
        with self._history_provider_lock:
            if not self._analytics_built:
                self._analytics_provider = self._build_analytics_provider()
                self._analytics_built = True
            return self._analytics_provider

    def _build_analytics_provider(self):
        influx_cfg = self._influx_config()
        if not (influx_cfg and influx_cfg.get("enabled")):
            return None
        try:
            from ems.history.influx_provider import InfluxHistoryProvider

            return InfluxHistoryProvider(influx_cfg)
        except Exception:
            logging.exception("failed to build InfluxDB analytics provider")
            return None

    def _influx_config(self):
        if not (self.config_path and os.path.exists(self.config_path)):
            return None
        try:
            with open(self.config_path, encoding="utf-8") as handle:
                raw_config = json.load(handle)
            from ems.config import normalize_influxdb_config

            return normalize_influxdb_config(raw_config.get("influxdb"))
        except Exception:
            return None


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server_version = "EMSDashboard/1.0"

    def end_headers(self):
        if not getattr(self, "_security_headers_sent", False):
            self._send_security_headers()
        super().end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/live":
            self._send_json(self.server.store.latest())
            return

        if parsed.path == "/api/history":
            query = parse_qs(parsed.query)
            range_name = query.get("range", ["6h"])[0]

            if range_name not in SUPPORTED_RANGES:
                self._send_json(
                    {
                        "error": "unsupported_range",
                        "supported": sorted(SUPPORTED_RANGES.keys()),
                    },
                    status=400,
                )
                return

            self._send_json({
                "range": range_name,
                "items": self.server.store.history(range_name),
            })
            return

        if parsed.path == "/api/history/series":
            self._handle_history_series(parse_qs(parsed.query))
            return

        if parsed.path == "/api/analytics/status":
            self._handle_analytics_status()
            return

        if parsed.path == "/api/analytics/series":
            self._handle_analytics_series(parse_qs(parsed.query))
            return

        if parsed.path == "/api/energy-stats":
            self._send_json(self.server.store.energy_summary())
            return

        if parsed.path == "/api/ui-config":
            # Read-only UI bootstrap hints (no auth/control/runtime impact).
            self._send_json({"animation_mode": self.server.animation_mode})
            return

        if parsed.path == "/api/auth/status":
            self._send_json(self._auth_status_payload())
            return

        if parsed.path == "/api/runtime":
            self._send_json(
                attach_limits(
                    runtime_payload(self.server.runtime_state),
                    self.server.runtime_validation,
                )
            )
            return

        if parsed.path == "/api/events":
            self._send_events()
            return

        if parsed.path == "/api/diagnose":
            self._handle_diagnose(parse_qs(parsed.query, keep_blank_values=True))
            return

        if parsed.path == "/api/diagnose/support-bundle":
            self._handle_support_bundle()
            return

        if parsed.path == "/api/logs":
            self._handle_logs(parse_qs(parsed.query))
            return

        self._send_static(parsed.path)

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/auth/login":
            self._handle_login()
            return

        if parsed.path == "/api/auth/logout":
            self._handle_logout()
            return

        if parsed.path == "/api/auth/refresh":
            self._handle_refresh()
            return

        if parsed.path == "/api/logs/level":
            self._handle_set_log_level()
            return

        self._send_json({"error": "read_only"}, status=405)

    def do_PUT(self):
        self._send_json({"error": "read_only"}, status=405)

    def do_PATCH(self):
        parsed = urlparse(self.path)

        if parsed.path.startswith("/api/runtime/"):
            self._handle_runtime_patch(parsed.path)
            return

        self._send_json({"error": "read_only"}, status=405)

    def do_DELETE(self):
        self._send_json({"error": "read_only"}, status=405)

    def log_message(self, fmt, *args):
        logging.debug("dashboard_http " + fmt, *args)

    def _send_json(self, payload, status=200, headers=None):
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self._send_security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_security_headers(self):
        self._security_headers_sent = True
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)

    def _read_json_body(self):
        length = self._json_body_length()
        if length <= 0:
            return {}

        try:
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError("request body must be valid JSON") from exc

        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")

        return payload

    def _auth_status_payload(self):
        configured = self._auth_configured()
        session = self._current_session() if configured else None
        authenticated = session is not None
        payload = {
            "auth_configured": configured,
            "authenticated": authenticated,
            "write_mode_available": configured,
            "write_mode_active": authenticated,
        }
        if authenticated:
            payload["csrf_token"] = session.csrf_token
            if session.expires_at is None:
                payload["session_expires_in_seconds"] = None
            else:
                remaining = session.expires_at - self.server.sessions.time_fn()
                payload["session_expires_in_seconds"] = max(0, int(remaining))
        return payload

    def _auth_configured(self):
        try:
            return auth_configured(self.server.auth_file)
        except ValueError:
            logging.warning("dashboard_auth_file_invalid path=%s", self.server.auth_file)
            return False

    def _handle_login(self):
        remote = self.client_address[0] if self.client_address else "unknown"

        body_error = self._json_body_preflight()
        if body_error:
            self._send_json(body_error[0], status=body_error[1])
            return

        if not self._auth_configured():
            self._send_json({"error": "invalid_password"}, status=403)
            return

        if self.server.login_limiter.is_limited(remote):
            self._send_json({"error": "login_rate_limited"}, status=429)
            return

        try:
            payload = self._read_json_body()
        except JsonBodyTooLarge as exc:
            self._send_json(
                {"error": "request_too_large", "message": str(exc)},
                status=413,
            )
            return
        except JsonBodyLengthError as exc:
            self._send_json({"error": "bad_request", "message": str(exc)}, status=400)
            return
        except ValueError as exc:
            self._send_json({"error": "bad_request", "message": str(exc)}, status=400)
            return

        password = payload.get("password")
        if not isinstance(password, str) or not verify_password_file(
            self.server.auth_file,
            password,
        ):
            self.server.login_limiter.record_failure(remote)
            self._send_json({"error": "invalid_password"}, status=403)
            return

        self.server.login_limiter.reset(remote)
        session = self.server.sessions.create()
        self._send_json(
            {
                **self._auth_status_payload(),
                "authenticated": True,
                "write_mode_active": True,
                "csrf_token": session.csrf_token,
            },
            headers={
                "Set-Cookie": self._session_cookie(session.session_id),
            },
        )

    def _handle_logout(self):
        session_id = self._session_cookie_value()
        self.server.sessions.destroy(session_id)
        self._send_json(
            self._auth_status_payload(),
            headers={
                "Set-Cookie": self._expired_session_cookie(),
            },
        )

    def _handle_refresh(self):
        # Genuine-activity heartbeat: slide the idle timeout (bounded by the
        # absolute cap). Treated as a state change, so it needs the full
        # write-auth path (valid session + matching CSRF token); background
        # polling, which carries no CSRF token, can never renew a session.
        body_error = self._json_body_preflight()
        if body_error:
            self._send_json(body_error[0], status=body_error[1])
            return

        auth_error = self._require_write_auth()
        if auth_error:
            self._send_json(auth_error[0], status=auth_error[1])
            return

        self.server.sessions.touch(self._session_cookie_value())
        self._send_json(self._auth_status_payload())

    DIAGNOSE_PROFILES = (
        "install",
        "deep",
        "hardware",
        "control",
        "control_quality",
    )

    def _resolve_series_query(self, query):
        """Parse range/start/end/series/devices from a series request.

        Returns ``(range_name, start, end, series, devices)`` on success, or
        ``None`` after already sending the appropriate 400 error response.
        """
        from ems.history.provider import (
            HISTORY_RANGE_SECONDS,
            normalize_series,
            resolve_range,
        )

        range_name = query.get("range", ["24h"])[0]
        start_param = query.get("start", [None])[0]
        end_param = query.get("end", [None])[0]

        if start_param is not None or end_param is not None:
            # Custom date range: explicit start/end bounds override the token.
            start = _parse_time_param(start_param)
            end = _parse_time_param(end_param)
            if start is None or end is None or start >= end:
                self._send_json({"error": "invalid_range"}, status=400)
                return None
            range_name = "custom"
        elif range_name in HISTORY_RANGE_SECONDS:
            start, end = resolve_range(range_name)
        else:
            self._send_json(
                {
                    "error": "unsupported_range",
                    "supported": sorted(HISTORY_RANGE_SECONDS.keys()),
                },
                status=400,
            )
            return None

        series = normalize_series(_split_csv(query.get("series", [None])[0]))
        devices = _split_csv(query.get("devices", [None])[0]) or None
        return range_name, start, end, series, devices

    def _serve_series(self, provider, query, *, log_label):
        from ems.history.provider import decimate_history_result

        parsed = self._resolve_series_query(query)
        if parsed is None:
            return
        range_name, start, end, series, devices = parsed

        try:
            result = provider.query(start, end, devices=devices, series=series)
        except Exception:
            logging.exception("%s series query failed", log_label)
            self._send_json({"error": "history_unavailable"}, status=503)
            return

        decimate_history_result(result)
        payload = result.to_dict()
        payload["range"] = range_name
        self._send_json(payload)

    def _handle_history_series(self, query):
        # Lightweight operational history for the Aggregate/Devices charts.
        # Always backed by the local SQLite snapshot store: InfluxDB never
        # replaces this source, so these views work with zero external
        # dependencies and remain the default experience.
        self._serve_series(
            self.server.sqlite_history_provider(), query, log_label="history"
        )

    def _handle_analytics_status(self):
        provider = self.server.analytics_provider()
        available = bool(provider and provider.available())
        payload = {"available": available, "provider": "influxdb"}
        if not provider:
            payload["reason"] = "not_configured"
        elif not available:
            payload["reason"] = "unreachable"
        self._send_json(payload)

    def _handle_analytics_series(self, query):
        # Long-term analytics, backed exclusively by InfluxDB. When InfluxDB is
        # not configured we return a 200 with an explicit unavailable marker so
        # the Analytics tab can show a clean info state instead of an error.
        provider = self.server.analytics_provider()
        if not provider:
            self._send_json(
                {
                    "available": False,
                    "reason": "not_configured",
                    "source": "influxdb",
                }
            )
            return
        self._serve_series(provider, query, log_label="analytics")

    def _diagnose_args(self):
        # The browser never supplies paths or sampling: paths come from the
        # server's known config/runtime locations and sample_seconds is forced
        # to 0 so the handler can never block on diagnose_control_samples().
        return SimpleNamespace(
            config=self.server.config_path,
            runtime_state=self.server.runtime_state_path,
            dashboard_auth=self.server.auth_file,
            sample_seconds=0,
            output=None,
        )

    def _handle_diagnose(self, query):
        auth_error = self._require_read_auth()
        if auth_error:
            self._send_json(auth_error[0], status=auth_error[1])
            return

        profile_values = query.get("profile")
        profile = profile_values[0] if profile_values else None
        if profile not in self.DIAGNOSE_PROFILES:
            self._send_json(
                {"error": "invalid_profile", "supported": list(self.DIAGNOSE_PROFILES)},
                status=400,
            )
            return

        if not self.server.diagnose_lock.acquire(blocking=False):
            self._send_json({"error": "diagnose_busy"}, status=429)
            return

        try:
            from ems import diagnostics

            runner = {
                "install": diagnostics.run_install_diagnosis,
                "deep": diagnostics.run_deep_diagnosis,
                "hardware": diagnostics.run_hardware_diagnosis,
                "control": diagnostics.run_control_diagnosis,
                "control_quality": diagnostics.run_control_quality_diagnosis,
            }[profile]
            report = runner(self._diagnose_args())
            report["profile"] = profile
            redacted = diagnostics.diagnose_redact_report_for_http(report)
        except Exception:
            logging.exception("dashboard_diagnose_failed profile=%s", profile)
            self._send_json({"error": "diagnose_failed"}, status=500)
            return
        finally:
            self.server.diagnose_lock.release()

        self._send_json(redacted)

    def _handle_support_bundle(self):
        auth_error = self._require_read_auth()
        if auth_error:
            self._send_json(auth_error[0], status=auth_error[1])
            return

        if not self.server.diagnose_lock.acquire(blocking=False):
            self._send_json({"error": "diagnose_busy"}, status=429)
            return

        tmp_path = None
        try:
            from ems import diagnostics

            args = self._diagnose_args()
            # Build a complete read-only report (all profiles) for the bundle.
            args.deep = True
            args.control = True
            args.control_quality = True
            args.hardware = True
            args.support_bundle = True
            report = diagnostics.run_diagnosis(args)

            config_data, _ = diagnostics.diagnose_json_file(
                report["project"]["config_path"]
            )
            # Server-chosen tempfile only — never an output path from the browser.
            fd, tmp_path = tempfile.mkstemp(prefix="ems-support-", suffix=".zip")
            os.close(fd)
            args.output = tmp_path
            diagnostics.diagnose_write_support_bundle(
                report,
                args,
                config_data if isinstance(config_data, dict) else {},
                report["project"]["runtime_state_path"],
            )
            with open(tmp_path, "rb") as f:
                body = f.read()
        except Exception:
            logging.exception("dashboard_support_bundle_failed")
            self._send_json({"error": "support_bundle_failed"}, status=500)
            return
        finally:
            self.server.diagnose_lock.release()
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        filename = f"ems-support-{timestamp}.zip"
        self.send_response(200)
        self._send_security_headers()
        self.send_header("Content-Type", "application/zip")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header(
            "Content-Disposition", f'attachment; filename="{filename}"'
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    LOG_LEVELS = {
        "DEBUG": 10,
        "INFO": 20,
        "WARNING": 30,
        "ERROR": 40,
        "CRITICAL": 50,
    }
    MAX_LOG_LINES = 1000

    def _handle_logs(self, query):
        auth_error = self._require_read_auth()
        if auth_error:
            self._send_json(auth_error[0], status=auth_error[1])
            return

        try:
            after = self._log_int_param(query, "after", minimum=0)
            limit = self._log_int_param(
                query, "limit", default=self.MAX_LOG_LINES, minimum=0
            )
        except ValueError as exc:
            self._send_json({"error": "bad_request", "message": str(exc)}, status=400)
            return
        if limit is None or limit > self.MAX_LOG_LINES:
            limit = self.MAX_LOG_LINES

        min_levelno = None
        level = (query.get("level", [None]) or [None])[0]
        if level:
            min_levelno = self.LOG_LEVELS.get(str(level).upper())
            if min_levelno is None:
                self._send_json(
                    {"error": "bad_request", "message": "unknown level"},
                    status=400,
                )
                return

        buffer = self.server.log_buffer
        if buffer is None:
            self._send_json({"lines": [], "cursor": 0, "dropped": False})
            return

        result = buffer.get_lines(after=after, limit=limit, min_levelno=min_levelno)
        if self.server.log_redaction:
            from ems import diagnostics

            for line in result["lines"]:
                line["message"] = diagnostics.diagnose_redact_text(line["message"])
        # Current runtime verbosity of the service so the UI can reflect it.
        result["service_level"] = logging.getLevelName(
            logging.getLogger().getEffectiveLevel()
        )
        self._send_json(result)

    def _handle_set_log_level(self):
        # Changing the service's runtime log verbosity is a state change, so it
        # needs the full write-auth path (valid session + CSRF). It sets the root
        # logger level, affecting every handler (the ring buffer and stderr).
        body_error = self._json_body_preflight()
        if body_error:
            self._send_json(body_error[0], status=body_error[1])
            return

        auth_error = self._require_write_auth()
        if auth_error:
            self._send_json(auth_error[0], status=auth_error[1])
            return

        try:
            payload = self._read_json_body()
        except JsonBodyTooLarge as exc:
            self._send_json({"error": "request_too_large", "message": str(exc)}, status=413)
            return
        except (JsonBodyLengthError, ValueError) as exc:
            self._send_json({"error": "bad_request", "message": str(exc)}, status=400)
            return

        level = payload.get("level")
        levelno = self.LOG_LEVELS.get(str(level).upper()) if level else None
        if levelno is None:
            self._send_json(
                {"error": "bad_request", "message": "unknown level"}, status=400
            )
            return

        logging.getLogger().setLevel(levelno)
        logging.info("event=dashboard_log_level_changed level=%s", logging.getLevelName(levelno))
        self._send_json({"service_level": logging.getLevelName(levelno)})

    def _log_int_param(self, query, name, default=None, minimum=None):
        raw = (query.get(name, [None]) or [None])[0]
        if raw is None or raw == "":
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if minimum is not None and value < minimum:
            raise ValueError(f"{name} must be >= {minimum}")
        return value

    def _handle_runtime_patch(self, path):
        body_error = self._json_body_preflight()
        if body_error:
            self._send_json(body_error[0], status=body_error[1])
            return

        auth_error = self._require_write_auth()
        if auth_error:
            self._send_json(auth_error[0], status=auth_error[1])
            return

        try:
            payload = self._read_json_body()
            if path == "/api/runtime/system":
                result = apply_system_update(
                    self.server.runtime_state,
                    payload,
                    self.server.runtime_validation,
                )
            elif path == "/api/runtime/ha":
                result = apply_section_update(
                    self.server.runtime_state,
                    "ha",
                    payload,
                    self.server.runtime_validation,
                )
            elif path == "/api/runtime/winter":
                result = apply_section_update(
                    self.server.runtime_state,
                    "winter",
                    payload,
                    self.server.runtime_validation,
                )
            elif path.startswith("/api/runtime/device/"):
                device_name = unquote(path.rsplit("/", 1)[-1])
                result = apply_device_update(
                    self.server.runtime_state,
                    device_name,
                    payload,
                    self.server.runtime_validation,
                )
            else:
                self._send_json({"error": "not_found"}, status=404)
                return
        except RuntimeWriteError as exc:
            self._send_json({"error": "invalid_runtime_update", "message": str(exc)}, status=400)
            return
        except ValueError as exc:
            self._send_json({"error": "bad_request", "message": str(exc)}, status=400)
            return

        self._send_json({"updated": True, **result})

    def _json_body_preflight(self):
        try:
            self._json_body_length()
        except JsonBodyTooLarge as exc:
            return {"error": "request_too_large", "message": str(exc)}, 413
        except JsonBodyLengthError as exc:
            return {"error": "bad_request", "message": str(exc)}, 400
        return None

    def _json_body_length(self):
        raw_length = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(raw_length)
        except (TypeError, ValueError) as exc:
            raise JsonBodyLengthError("Content-Length must be an integer") from exc
        if length < 0:
            raise JsonBodyLengthError("Content-Length must be >= 0")
        if length > MAX_JSON_BODY_BYTES:
            raise JsonBodyTooLarge(
                f"request body exceeds {MAX_JSON_BODY_BYTES} bytes"
            )
        return length

    def _require_write_auth(self):
        if not self._auth_configured():
            return {"error": "auth_not_configured"}, 403

        session = self._current_session()
        if session is None:
            return {"error": "not_authenticated"}, 401

        csrf_token = self.headers.get("X-CSRF-Token", "")
        if not csrf_token or not hmac_compare(csrf_token, session.csrf_token):
            return {"error": "csrf_failed"}, 403

        return None

    def _require_read_auth(self):
        # For side-effect-free GET endpoints (diagnostics, logs). A valid session
        # is required, but NOT a CSRF token: GET changes no state, SameSite=Strict
        # already blocks cross-site cookie use, and CSP frame-ancestors 'none'
        # blocks framing. This omission is deliberate, not an oversight.
        if not self._auth_configured():
            return {"error": "auth_not_configured"}, 403

        if self._current_session() is None:
            return {"error": "not_authenticated"}, 401

        return None

    def _current_session(self):
        return self.server.sessions.get(self._session_cookie_value())

    def _session_cookie_value(self):
        raw = self.headers.get("Cookie", "")
        if not raw:
            return None
        parsed = cookies.SimpleCookie()
        try:
            parsed.load(raw)
        except cookies.CookieError:
            return None
        morsel = parsed.get(SESSION_COOKIE_NAME)
        return morsel.value if morsel else None

    def _session_cookie(self, session_id):
        parts = [
            f"{SESSION_COOKIE_NAME}={session_id}",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
        ]
        if self.server.https_active:
            parts.append("Secure")
        return "; ".join(parts)

    def _expired_session_cookie(self):
        parts = [
            f"{SESSION_COOKIE_NAME}=",
            "Path=/",
            "Max-Age=0",
            "HttpOnly",
            "SameSite=Strict",
        ]
        if self.server.https_active:
            parts.append("Secure")
        return "; ".join(parts)

    def _send_events(self):
        remote = self.client_address[0] if self.client_address else "unknown"
        if not self.server.sse_limiter.acquire(remote):
            self._send_json({"error": "sse_connection_limit"}, status=429)
            return

        self.send_response(200)
        self._send_security_headers()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_timestamp = None
        started_at = time.monotonic()

        try:
            while time.monotonic() - started_at < self.server.sse_max_connection_seconds:
                snapshot = self.server.store.latest()
                timestamp = snapshot.get("timestamp")

                if timestamp != last_timestamp:
                    payload = json.dumps(snapshot, sort_keys=True)
                    message = f"event: telemetry\ndata: {payload}\n\n"
                    try:
                        self.wfile.write(message.encode("utf-8"))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    last_timestamp = timestamp

                time.sleep(1)
        finally:
            self.server.sse_limiter.release(remote)

    def _send_static(self, request_path):
        path = "/index.html" if request_path in ("", "/") else request_path
        normalized = os.path.normpath(path.lstrip("/"))
        full_path = os.path.abspath(os.path.join(STATIC_DIR, normalized))
        static_root = os.path.abspath(STATIC_DIR)

        if (
            os.path.commonpath([static_root, full_path]) != static_root
            or not os.path.isfile(full_path)
        ):
            self.send_error(404)
            return

        with open(full_path, "rb") as f:
            body = f.read()

        content_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
        self.send_response(200)
        self._send_security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def hmac_compare(left, right):
    import hmac

    return hmac.compare_digest(str(left), str(right))


def start_dashboard_server(
    store,
    host="0.0.0.0",
    port=8080,
    runtime_state=None,
    auth_file=None,
    ssl_enabled=False,
    ssl_cert_file=None,
    ssl_key_file=None,
    ssl_auto_generate=True,
    base_dir=None,
    session_timeout_seconds=1800,
    session_absolute_max_seconds=43200,
    runtime_validation=None,
    config_path=None,
    runtime_state_path=None,
    log_buffer=None,
    log_redaction=False,
    animation_mode="normal",
    sse_max_connections=MAX_SSE_CONNECTIONS,
    sse_max_connections_per_ip=MAX_SSE_CONNECTIONS_PER_IP,
    sse_max_connection_seconds=SSE_MAX_CONNECTION_SECONDS,
):
    server = DashboardHTTPServer(
        (host, int(port)),
        DashboardRequestHandler,
        store,
        runtime_state=runtime_state,
        auth_file=auth_file,
        https_active=ssl_enabled,
        session_timeout_seconds=session_timeout_seconds,
        session_absolute_max_seconds=session_absolute_max_seconds,
        runtime_validation=runtime_validation,
        config_path=config_path,
        runtime_state_path=runtime_state_path,
        log_buffer=log_buffer,
        log_redaction=log_redaction,
        animation_mode=animation_mode,
        sse_max_connections=sse_max_connections,
        sse_max_connections_per_ip=sse_max_connections_per_ip,
        sse_max_connection_seconds=sse_max_connection_seconds,
    )

    if ssl_enabled:
        from dashboard.https import ensure_dashboard_ssl_context

        context = ensure_dashboard_ssl_context(
            {
                "host": host,
                "ssl_cert_file": ssl_cert_file or os.path.join("config", "dashboard.crt"),
                "ssl_key_file": ssl_key_file or os.path.join("config", "dashboard.key"),
                "ssl_auto_generate": ssl_auto_generate,
            },
            base_dir or BASE_DIR,
        )
        server.socket = context.wrap_socket(server.socket, server_side=True)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    logging.info(
        "dashboard_started host=%s port=%s https=%s",
        host,
        port,
        bool(ssl_enabled),
    )
    return server
