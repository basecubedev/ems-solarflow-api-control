# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reusable local dashboard preview server.

Serves the real dashboard static assets (``dashboard/static/``) with
deterministic, non-secret synthetic API responses so any dashboard change can be
inspected visually without real hardware, MQTT, Zendure/Shelly access, SQLite
history, passwords, or a running EMS loop.

Usage:
    python3 scripts/serve_dashboard_preview.py
    python3 scripts/serve_dashboard_preview.py --scenario firmware-status
    python3 scripts/serve_dashboard_preview.py --scenario write-mode
    python3 scripts/serve_dashboard_preview.py --host 127.0.0.1 --port 8767

Open, for example:
    http://127.0.0.1:8767/                      the dashboard

A request carrying the cookie ``ems-preview-scenario=<name>`` is answered from
that scenario instead of the server's own, so one browser context can show
another scenario without changing it for anyone else on the server. The
documentation captures in ``tests/e2e-dashboard/capture-docs.spec.ts`` use it.

Safety: the preview never contacts real Zendure/Shelly/MQTT/InfluxDB/Home
Assistant endpoints, never reads secrets from config.json, and never writes
runtime-state, auth files, or the dashboard database. The Schedule tab runs the
real schedule routes over a scratch copy of the scenario's runtime state in a
temporary directory of its own, removed when the process exits. It binds to
loopback by default.
"""

import argparse
import calendar
import json
import os
import threading
import time
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, ROOT)

from dashboard.static_files import (  # noqa: E402
    SHARED_ROOT_FILES,
    STATIC_CONTENT_TYPES,
    build_static_asset_index,
    lookup_static_asset,
    modern_asset_path,
    modern_redirect,
)
from dashboard_preview_data import (  # noqa: E402
    DEFAULT_SCENARIO,
    DIAGNOSE_PROFILES,
    SCENARIOS,
    build_scenario,
    diagnose_report,
)

STATIC_DIR = os.path.join(ROOT, "dashboard", "static")
STATIC_ASSETS = build_static_asset_index(STATIC_DIR)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8767
SCENARIO_COOKIE = "ems-preview-scenario"
CONSOLE_COOKIE = "ems-preview-console"
SAFE_RESPONSE_CONTENT_TYPES = {
    "application/json; charset=utf-8",
    *STATIC_CONTENT_TYPES.values(),
}


PREVIEW_MAINTENANCE = {
    "status": {
        "config_path": "config/config.json",
        "backup_dir": "data/backups",
        "backup_types": ["config", "databases", "influxdb"],
        "influxdb": {
            "enabled": True,
            "mode": "bundled",
            "backup_supported": True,
            "restore_supported": True,
        },
        "restore_available_in_dashboard": True,
    },
    "backups": [
        {
            "name": "ems-config-manual-2026-06-29-201500.tar.gz",
            "size_bytes": 12456,
            "modified_at": "2026-06-29T20:15:00Z",
            "encrypted": False,
            "manifest_available": True,
            "backup_type": "config",
            "backup_purpose": "manual",
            "ems_version": "0.6.0",
        },
        {
            "name": "ems-databases-manual-2026-06-28-093000.tar.gz",
            "size_bytes": 384122,
            "modified_at": "2026-06-28T09:30:00Z",
            "encrypted": False,
            "manifest_available": True,
            "backup_type": "databases",
            "backup_purpose": "manual",
            "ems_version": "0.6.0",
        },
        {
            "name": "ems-config-manual-2026-06-20-070000.tar.gz.enc",
            "size_bytes": 12992,
            "modified_at": "2026-06-20T07:00:00Z",
            "encrypted": True,
            "manifest_available": False,
            "backup_type": "config",
            "backup_purpose": "manual",
            "ems_version": None,
        },
    ],
    # Per-type manifests so Details is consistent with each row's backup type
    # (a database backup must not show config files, etc.).
    "manifests": {
        "config": {
            "backup_type": "config",
            "backup_purpose": "manual",
            "backup_format": 1,
            "created_at": "2026-06-29T20:15:00Z",
            "ems_version": "0.6.0",
            "git_commit_short": "b87add4324ea",
            "git_branch": "main",
            "encrypted": False,
            "encryption_method": None,
            "rollback_for": None,
            "skipped_count": 0,
            "files": [
                {"path": "config.json", "kind": "config", "sensitive": True},
                {"path": "runtime-state.json", "kind": "runtime_state", "sensitive": False},
                {"path": "dashboard-auth.json", "kind": "dashboard_auth", "sensitive": True},
            ],
        },
        "databases": {
            "backup_type": "databases",
            "backup_purpose": "manual",
            "backup_format": 1,
            "created_at": "2026-06-28T09:30:00Z",
            "ems_version": "0.6.0",
            "git_commit_short": "b87add4324ea",
            "git_branch": "main",
            "encrypted": False,
            "encryption_method": None,
            "rollback_for": None,
            "skipped_count": 0,
            "files": [
                {"path": "data/ems_dashboard.sqlite", "kind": "sqlite",
                 "sensitive": False, "privacy_relevant": True},
                {"path": "data/ems_state.sqlite", "kind": "sqlite",
                 "sensitive": False, "privacy_relevant": True},
            ],
        },
        "influxdb": {
            "backup_type": "influxdb",
            "backup_purpose": "manual",
            "backup_format": 1,
            "created_at": "2026-06-27T06:00:00Z",
            "ems_version": "0.6.0",
            "git_commit_short": "b87add4324ea",
            "git_branch": "main",
            "encrypted": False,
            "encryption_method": None,
            "rollback_for": None,
            "skipped_count": 0,
            "files": [
                {"path": "influxdb/20260627T060000Z.bolt", "kind": "influxdb",
                 "sensitive": False, "privacy_relevant": True},
                {"path": "influxdb/20260627T060000Z.sqlite", "kind": "influxdb",
                 "sensitive": False, "privacy_relevant": True},
            ],
        },
    },
    "config_upgrade": {
        "changed": True,
        "apply_available": True,
        "plan_id": "preview-config-upgrade-plan",
        "format_changed": True,
        "add_count": 3,
        "comment_add_count": 4,
        "comment_refresh_count": 21,
        "migration_count": 1,
        "refresh_default_count": 1,
        "template": "config.template.json",
        "items": [
            {"kind": "add", "path": "config_upgrade.on_startup", "value": "check"},
            {"kind": "add", "path": "dashboard.session_idle_timeout_seconds", "value": 1800},
            {"kind": "add", "path": "influxdb.token", "value": "<redacted>"},
            {
                "kind": "migrate",
                "path": "config_schema_version",
                "old_value": 2,
                "value": 3,
            },
            {
                "kind": "refresh_default",
                "path": "ac_charge_control.max_total_charge_power_w",
                "old_value": 1200,
                "value": None,
            },
            {"kind": "comment_add", "path": "config_upgrade._comment"},
            {"kind": "comment_add", "path": "dashboard._comment"},
            {"kind": "comment_refresh", "path": "system._comment"},
            {"kind": "comment_refresh", "path": "devices[0]._comment_soc"},
        ],
        "requires_restart": True,
    },
}


class PreviewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address,
        handler,
        scenario,
        device_count=None,
        freeze_timestamp=False,
        sse_interval=2.0,
        sse_max_per_ip=0,
        fixed_now=None,
    ):
        super().__init__(server_address, handler)
        self.scenario_name = scenario
        self.device_count = device_count
        self.freeze_timestamp = freeze_timestamp
        self.fixed_now = fixed_now
        self.sse_interval = sse_interval
        # 0 disables the limit. A positive value reproduces the production
        # per-IP cap so the polling-fallback cascade can be measured.
        self.sse_max_per_ip = sse_max_per_ip
        self._sse_counts = {}
        self._sse_lock = threading.Lock()

    def now(self):
        """The preview's clock: the pinned instant when there is one, else the real time."""

        return time.time() if self.fixed_now is None else self.fixed_now

    def acquire_sse_slot(self, remote):
        if self.sse_max_per_ip <= 0:
            return True
        with self._sse_lock:
            used = self._sse_counts.get(remote, 0)
            if used >= self.sse_max_per_ip:
                return False
            self._sse_counts[remote] = used + 1
            return True

    def release_sse_slot(self, remote):
        if self.sse_max_per_ip <= 0:
            return
        with self._sse_lock:
            used = self._sse_counts.get(remote, 0)
            if used <= 1:
                self._sse_counts.pop(remote, None)
            else:
                self._sse_counts[remote] = used - 1


class PreviewHandler(BaseHTTPRequestHandler):
    server_version = "DashboardPreview/1.0"

    # --- request routing -------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/ui-config":
            console = self._console_address()
            payload = {"animation_mode": "normal", "admin_console": console, "console_operator": True}
            if console is not None:
                payload["signpost"] = True
            self._send_json(payload)
            return

        if path == "/api/live":
            self._send_json(self._scenario()["snapshot"])
            return
        if path == "/api/events":
            self._send_events()
            return
        if path == "/api/history":
            self._send_json({
                "range": parse_qs(parsed.query).get("range", ["6h"])[0],
                "items": self._scenario()["history"],
            })
            return
        if path == "/api/history/series":
            self._send_json(
                self._history_series(parse_qs(parsed.query), source="sqlite")
            )
            return
        if path == "/api/analytics/status":
            self._send_json({"available": True, "provider": "influxdb"})
            return
        if path == "/api/analytics/series":
            # Preview the InfluxDB analytics tab with the same synthetic series.
            self._send_json(
                self._history_series(parse_qs(parsed.query), source="influxdb")
            )
            return
        if path == "/api/runtime":
            self._send_json(self._scenario()["runtime"])
            return
        if path == "/api/auth/status":
            self._send_json(self._scenario()["auth"])
            return
        if path == "/api/diagnose":
            self._send_diagnose(parse_qs(parsed.query))
            return
        if path == "/api/logs":
            self._send_logs(parsed.query)
            return
        if path == "/api/schedule" or path.startswith("/api/schedule/"):
            self._send_schedule("GET", parsed, {})
            return
        if path == "/api/maintenance/status":
            self._send_json(PREVIEW_MAINTENANCE["status"])
            return
        if path == "/api/maintenance/backups":
            self._send_json({"items": PREVIEW_MAINTENANCE["backups"]})
            return
        if path == "/api/maintenance/config-upgrade":
            self._send_json(PREVIEW_MAINTENANCE["config_upgrade"])
            return

        self._send_static(path, parsed.query)

    def do_POST(self):
        self._handle_write()

    def do_PUT(self):
        self._handle_write()

    def do_PATCH(self):
        self._handle_write()

    def do_DELETE(self):
        self._handle_write()

    def log_message(self, _fmt, *_args):
        return

    # --- handlers --------------------------------------------------------

    def _scenario(self):
        return build_scenario(
            self._scenario_name(),
            device_count=self.server.device_count,
            freeze_timestamp=self.server.freeze_timestamp,
            now=self.server.fixed_now,
        )

    def _cookie(self, name):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        morsel = cookies.get(name)
        return morsel.value if morsel is not None else None

    def _console_address(self):
        """The Admin console a request's cookie ``ems-preview-console=<port>`` names, else none.

        Only that browser context sees a console, and with it the signpost that
        sends the reader there, so the other pages' captures stay as they are.
        """

        value = self._cookie(CONSOLE_COOKIE)
        if not value or not value.isdigit() or not 1 <= int(value) <= 65535:
            return None
        return {"port": int(value), "https_port": None}

    def _scenario_name(self):
        """The scenario this request's cookie names, else the server's own.

        An unknown or malformed cookie falls back to the server's scenario
        rather than failing the request.
        """

        value = self._cookie(SCENARIO_COOKIE)
        return value if value in SCENARIOS else self.server.scenario_name

    # Map each chart series id to the history-item field it reads. ``target``
    # (EMS commanded) tracks the inverter output in the synthetic data.
    _SERIES_FIELDS = {
        "pv": "pv_total_w",
        "output": "inverter_output_w",
        "battery": "battery_power_w",
        "soc": "average_soc",
        "home": "home_load_w",
        "grid": "grid_power_w",
        "target": "inverter_output_w",
        "consumers": "consumer_total_w",
        "home_other": "home_other_w",
    }

    def _history_series(self, query, source):
        # Columnar series derived from the synthetic snapshots. The same shape is
        # served for SQLite history and InfluxDB analytics; only ``source``
        # differs so the dashboard can label the data origin.
        items = self._scenario()["history"]
        requested = [s for s in (query.get("series", [""])[0] or "").split(",") if s]
        keys = [s for s in requested if s in self._SERIES_FIELDS or s.startswith("consumer:")] or [
            "pv",
            "output",
            "battery",
        ]
        time_axis = []
        cols = {key: [] for key in keys}
        for item in items:
            iso = str(item.get("timestamp", "")).replace("Z", "+00:00")
            try:
                epoch = calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
            except (ValueError, TypeError):
                continue
            time_axis.append(epoch)
            for key in keys:
                if key.startswith("consumer:"):
                    consumer = (item.get("consumers") or {}).get(unquote(key[len("consumer:"):])) or {}
                    cols[key].append(consumer.get("power_w"))
                else:
                    cols[key].append(item.get(self._SERIES_FIELDS[key]))
        return {
            "source": source,
            "range": query.get("range", ["24h"])[0],
            "window": "raw",
            "time": time_axis,
            "series": cols,
            "devices": [],
            "meta": {"point_count": len(time_axis)},
        }

    def _maintenance_inspect(self, body):
        name = (body or {}).get("file") or ""
        password = (body or {}).get("password")
        match = next(
            (b for b in PREVIEW_MAINTENANCE["backups"] if b["name"] == name), None
        )
        if match and match.get("encrypted") and not password:
            return {"name": name, "encrypted": True, "manifest_available": False}
        backup_type = (match or {}).get("backup_type", "config")
        manifest = PREVIEW_MAINTENANCE["manifests"].get(
            backup_type, PREVIEW_MAINTENANCE["manifests"]["config"]
        )
        return {
            "name": name or "ems-config-manual-2026-06-29-201500.tar.gz",
            "encrypted": bool(match and match.get("encrypted")),
            "manifest_available": True,
            "manifest": manifest,
        }

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, BrokenPipeError, ConnectionResetError):
            return {}

    def _handle_write(self):
        """Preview-only write handler: never mutates config, devices or the
        installation; schedule writes land in the preview's scratch directory."""

        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_json_body()

        if path == "/api/schedule" or path.startswith("/api/schedule/"):
            self._send_schedule(self.command, parsed, body)
            return
        if path == "/api/auth/login":
            # Let reviewers exercise the write UI without a real password.
            self._send_json({
                "auth_configured": True,
                "authenticated": True,
                "csrf_token": "preview-csrf-token",
            })
            return
        if path in ("/api/auth/logout", "/api/auth/refresh"):
            self._send_json({"ok": True})
            return
        if path == "/api/maintenance/backups/create":
            # Preview-only: report a created backup without writing anything.
            self._send_json({
                "created": True,
                "backup": {
                    "name": "ems-config-manual-2026-06-30-120000.tar.gz",
                    "path": "data/backups/ems-config-manual-2026-06-30-120000.tar.gz",
                    "backup_type": "config",
                },
            })
            return
        if path == "/api/maintenance/backups/inspect":
            self._send_json(self._maintenance_inspect(body))
            return
        if path == "/api/maintenance/backups/restore-plan":
            self._send_json({
                "file": (body.get("file") if isinstance(body, dict) else None)
                or "ems-config-manual-2026-06-29-201500.tar.gz",
                "backup_type": "config",
                "encrypted": False,
                "actions": [
                    {"path": "config/config.json", "action": "would_replace_conflict", "status": "conflict"},
                    {"path": "runtime-state.json", "action": "would_skip_identical", "status": "identical"},
                ],
                "requires_restart": True,
                "requires_relogin": True,
                "warnings": [
                    "Config restore changes files on disk. Restart EMS for "
                    "restored settings to take effect.",
                ],
            })
            return
        if path == "/api/maintenance/backups/restore":
            self._send_json({
                "restored": True,
                "backup_type": "config",
                "rollback_backup": "data/backups/ems-config-rollback-2026-06-30-120000.tar.gz",
                "actions": [
                    {"path": "config/config.json", "action": "restored", "status": "conflict"},
                ],
                "requires_restart": True,
                "requires_relogin": True,
                "message": (
                    "Restore completed (preview only — nothing was written). "
                    "Restart EMS for restored settings to take effect."
                ),
            })
            return
        if path == "/api/maintenance/config-upgrade/apply":
            self._send_json({
                "changed": True,
                "backup": "data/backups/ems-config-manual-2026-06-30-120000.tar.gz",
                "backup_name": "ems-config-manual-2026-06-30-120000.tar.gz",
                "requires_restart": True,
                "requires_relogin": False,
                "applied": {
                    "keys_added": 3,
                    "values_migrated": 1,
                    "comments_added": 4,
                    "comments_refreshed": 21,
                    "format_changed": True,
                },
                "applied_count": 30,
                "message": (
                    "Config upgraded. Restart EMS for changed settings to take "
                    "effect. (preview only — nothing was written)"
                ),
            })
            return

        # Runtime PATCH / log-level / other write endpoints all echo a clear
        # preview-only acknowledgement and change nothing.
        self._send_json({
            "status": "preview-only",
            "applied": False,
            "message": "Preview server does not apply changes.",
        })

    def _send_events(self):
        remote = self.client_address[0]
        if not self.server.acquire_sse_slot(remote):
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b'{"error": "sse_connection_limit"}')
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        last_timestamp = None
        started = time.monotonic()
        interval = max(0.05, float(self.server.sse_interval))
        try:
            # Bounded stream so the daemon thread cannot live forever; the
            # frontend reconnects transparently.
            while time.monotonic() - started < 600:
                snapshot = self._scenario()["snapshot"]
                timestamp = snapshot.get("timestamp")
                if timestamp != last_timestamp:
                    payload = json.dumps(snapshot, sort_keys=True)
                    self.wfile.write(f"event: telemetry\ndata: {payload}\n\n".encode())
                    self.wfile.flush()
                    last_timestamp = timestamp
                time.sleep(interval)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            self.server.release_sse_slot(remote)

    def _send_diagnose(self, query):
        """The report for the profile asked for, refused like the real server's when it names none it knows."""

        profile = (query.get("profile") or [None])[0]
        if profile not in DIAGNOSE_PROFILES:
            self._send_json({"error": "invalid_profile", "supported": list(DIAGNOSE_PROFILES)}, status=400)
            return
        self._send_json(diagnose_report(self._scenario_name(), profile, now=self.server.fixed_now))

    def _log_lines(self, after=0):
        now = self.server.now()
        log_lines = self._scenario()["logs"]
        return [
            {
                "seq": seq,
                "ts": now - (len(log_lines) - seq) * 14,
                "level": level,
                "logger": "ems",
                "message": message,
            }
            for seq, level, message in log_lines
            if seq > after
        ]

    def _send_logs(self, query_string):
        query = parse_qs(query_string)
        after = int((query.get("after", ["0"]) or ["0"])[0] or "0")
        log_lines = self._scenario()["logs"]
        lines = self._log_lines(after)
        self._send_json({
            "lines": lines,
            "cursor": log_lines[-1][0] if log_lines else 0,
            "dropped": False,
            "service_level": "DEBUG",
        })

    def _send_static(self, request_path, query=""):
        moved = modern_redirect(request_path, query)
        if moved is not None:
            self.send_response(302)
            self.send_header("Location", moved)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        asset_path = modern_asset_path(request_path, SHARED_ROOT_FILES)
        asset = lookup_static_asset(STATIC_ASSETS, asset_path) if asset_path else None
        if asset is None:
            self.send_error(404, "Not Found")
            return
        full_path, content_type = asset
        with open(full_path, "rb") as handle:
            self._send_bytes(handle.read(), content_type)

    def _drain_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > 0:
            try:
                self.rfile.read(length)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _send_schedule(self, method, parsed, body):
        api = getattr(self.server, "schedule_api", None)
        if api is None:
            self._send_json({"error": "schedule_unavailable"}, status=404)
            return
        status, payload = api.handle(method, parsed.path, body if isinstance(body, dict) else {}, parsed.query)
        self._send_json(payload, status=status)

    def _send_json(self, payload, status=200):
        self._send_bytes(
            json.dumps(payload, sort_keys=True).encode("utf-8"),
            "application/json; charset=utf-8",
            status=status,
        )

    def _send_bytes(self, body, content_type, status=200):
        if content_type not in SAFE_RESPONSE_CONTENT_TYPES:
            content_type = "application/octet-stream"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def start_server(
    host=DEFAULT_HOST,
    port=DEFAULT_PORT,
    scenario=DEFAULT_SCENARIO,
    device_count=None,
    freeze_timestamp=False,
    sse_interval=2.0,
    sse_max_per_ip=0,
    fixed_now=None,
):
    """Start the preview server and return the running PreviewServer instance."""

    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}")
    try:
        server = PreviewServer(
            (host, int(port)),
            PreviewHandler,
            scenario,
            device_count=device_count,
            freeze_timestamp=freeze_timestamp,
            sse_interval=sse_interval,
            sse_max_per_ip=sse_max_per_ip,
            fixed_now=fixed_now,
        )
    except OSError as exc:
        raise SystemExit(
            f"could not bind preview server to {host}:{port} ({exc}). "
            "Is the port already in use? Try a different --port."
        )
    server.schedule_api = _preview_schedule_api(server)
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _preview_schedule_api(server):
    """The real schedule routes over a scratch copy of the scenario's runtime state.

    The rules and the runtime state they change live in a temporary directory
    of this process's own, removed when it exits, so the Schedule tab can be
    exercised end to end without touching an installation.
    """

    import atexit
    import shutil
    import tempfile
    from zoneinfo import ZoneInfo

    from dashboard.schedule_api import ScheduleApi
    from ems.runtime_commands import build_validation_context
    from ems.runtime_state import RuntimeState
    from ems.schedule_store import ScheduleStore

    runtime = build_scenario(server.scenario_name, device_count=server.device_count)["runtime"]
    limits = runtime.get("_limits", {})
    scratch = tempfile.mkdtemp(prefix="ems-dashboard-preview-schedule-")
    atexit.register(shutil.rmtree, scratch, True)
    state_path = os.path.join(scratch, "runtime-state.json")
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump({key: value for key, value in runtime.items() if key != "_limits"}, handle)
    runtime_state = RuntimeState(state_path, {"devices": {}})
    runtime_state.load_if_changed(force=True)
    devices = sorted(runtime.get("devices", {}))
    config = {
        "system": {"max_total_power": limits.get("system", {}).get("max_total_power", 800)},
        "devices": [
            {"name": name, "max_power": limits.get("devices", {}).get(name, 800)} for name in devices
        ],
    }
    context = build_validation_context(config)
    return ScheduleApi(
        ScheduleStore(os.path.join(scratch, "schedules.json")),
        runtime_state=runtime_state,
        devices=lambda: {name: None for name in devices},
        validation_context=lambda: context,
        zone=ZoneInfo("Europe/Berlin"),
        clock=lambda: datetime.fromtimestamp(server.now(), timezone.utc),
        device_settings=lambda: limits.get("device_settings_offered", {}),
    )


def _print_urls(host, port, scenario):
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"Dashboard preview server: scenario={scenario}")
    print("Modern dashboard:")
    print(f"  http://{display_host}:{port}/")
    print("Synthetic data only — no real hardware, secrets, or runtime state.")
    print("Press Ctrl+C to stop.")


def _print_scenarios():
    print("Scenarios:")
    for name in SCENARIOS:
        print(f"  {name}")


def _instant(text):
    """An ISO-8601 instant with its UTC offset, as epoch seconds."""

    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO-8601 instant: {text!r}")
    if moment.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{text!r} names no UTC offset, so it is no single instant")
    return moment.timestamp()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Local dashboard preview server.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--scenario",
        default=DEFAULT_SCENARIO,
        choices=SCENARIOS,
        help=f"synthetic data scenario (default: {DEFAULT_SCENARIO})",
    )
    parser.add_argument(
        "--devices",
        type=int,
        default=None,
        metavar="N",
        help="scale the scenario to exactly N devices (benchmarks)",
    )
    parser.add_argument(
        "--sse-interval",
        type=float,
        default=2.0,
        metavar="SECONDS",
        help="seconds between telemetry emissions (default: 2.0)",
    )
    parser.add_argument(
        "--identical-snapshots",
        action="store_true",
        help="freeze the snapshot timestamp so every emission is unchanged",
    )
    parser.add_argument(
        "--now",
        type=_instant,
        default=None,
        metavar="ISO-8601",
        help="pin the preview's clock to one instant with its offset, e.g. "
        "2026-06-17T18:30:00+02:00: the history, logs, diagnose reports and "
        "schedule read that instant, so every capture shows the same day",
    )
    parser.add_argument(
        "--sse-max-per-ip",
        type=int,
        default=0,
        metavar="N",
        help="refuse more than N concurrent SSE streams per client IP (0: no limit)",
    )
    parser.add_argument(
        "--list-scenarios",
        action="store_true",
        help="print available scenarios and exit",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if args.list_scenarios:
        _print_scenarios()
        return

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"WARNING: binding preview server to {args.host}. The preview is "
            "intended for trusted local networks only."
        )

    server = start_server(
        args.host,
        args.port,
        args.scenario,
        device_count=args.devices,
        freeze_timestamp=args.identical_snapshots,
        sse_interval=args.sse_interval,
        sse_max_per_ip=args.sse_max_per_ip,
        fixed_now=args.now,
    )
    try:
        _print_urls(args.host, args.port, args.scenario)
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nStopping preview server.")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
