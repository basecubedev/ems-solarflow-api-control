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
    python3 scripts/serve_dashboard_preview.py --capture --output-dir docs/assets

Open, for example:
    http://127.0.0.1:8767/preview/aggregated
    http://127.0.0.1:8767/preview/devices
    http://127.0.0.1:8767/preview/control
    http://127.0.0.1:8767/preview/energy
    http://127.0.0.1:8767/preview/diagnose
    http://127.0.0.1:8767/preview/logs
    http://127.0.0.1:8767/preview/maintenance

Safety: the preview never contacts real Zendure/Shelly/MQTT/InfluxDB/Home
Assistant endpoints, never reads secrets from config.json, and never writes
runtime-state, auth files, or the dashboard database. It binds to loopback by
default.
"""

import argparse
import json
import os
import time
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, ROOT)

from dashboard.static_files import build_static_asset_index, static_asset_key  # noqa: E402
from dashboard_preview_data import (  # noqa: E402
    DEFAULT_SCENARIO,
    FLOW_VIEWS,
    SCENARIOS,
    build_scenario,
)

STATIC_DIR = os.path.join(ROOT, "dashboard", "static")
STATIC_ASSETS = build_static_asset_index(STATIC_DIR)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8767
# Capture mode is operator-oriented (Diagnose/Logs), so it defaults to the
# authenticated scenario unless the user explicitly selects one.
CAPTURE_DEFAULT_SCENARIO = "write-mode"
# Every view embedded in the README / docs, so a no-argument
# ``capture_dashboard_previews.py`` run regenerates the full screenshot set.
DEFAULT_CAPTURE_VIEWS = (
    "aggregated",
    "devices",
    "analytics",
    "control",
    "energy",
    "diagnose",
    "logs",
    "maintenance",
)
SAFE_RESPONSE_CONTENT_TYPES = {
    "application/json; charset=utf-8",
    "application/javascript; charset=utf-8",
    "text/css; charset=utf-8",
    "text/html; charset=utf-8",
}


def _preview_injection(view):
    """Minimal bootstrap injected into the real index.html.

    Sets the persisted flow view so the page opens directly in the requested
    view, then re-uses the existing frontend functions (setFlowView, runDiagnose)
    once the page is ready. Values are constants from a fixed allow-list, so no
    dynamic/user data is interpolated into the page.
    """

    view_json = json.dumps(view)
    before = (
        "<script>"
        f"try{{window.localStorage.setItem('dashboard.flowView',{view_json});}}"
        "catch(e){}"
        "</script>"
    )
    after = f"""
  <script>
    window.addEventListener("load", function () {{
      try {{
        if (typeof setFlowView === "function") setFlowView({view_json}, false);
        if ({view_json} === "diagnose" && typeof runDiagnose === "function") {{
          // Auto-run the real diagnose fetch as soon as auth status lands so the
          // tab shows a report (when authenticated) without a manual click.
          var tries = 0;
          var poll = window.setInterval(function () {{
            tries += 1;
            var st = window.state || {{}};
            if (st.diagnose && st.diagnose.report) {{ window.clearInterval(poll); return; }}
            if (st.auth && st.auth.authenticated && st.diagnose && !st.diagnose.running) {{
              runDiagnose(st.diagnose.profile);
            }}
            if (tries > 40) window.clearInterval(poll);
          }}, 100);
        }}
      }} catch (e) {{ /* preview bootstrap is best-effort */ }}
      document.body.dataset.previewReady = "true";
    }});
  </script>
"""
    return before, after


def normalize_views(raw_views):
    """Normalize a --views list, expanding the ``all`` alias.

    Returns ``None`` when no views were given (caller uses its own default),
    a list of concrete flow views otherwise. Raises ValueError on unknown views.
    """

    if not raw_views:
        return None
    if any(view == "all" for view in raw_views):
        return list(FLOW_VIEWS)
    invalid = [view for view in raw_views if view not in FLOW_VIEWS]
    if invalid:
        raise ValueError(
            f"unknown view(s): {', '.join(invalid)}; "
            f"choose from: {', '.join(FLOW_VIEWS)}, all"
        )
    return list(raw_views)


def resolve_scenario(args):
    """Pick the scenario, defaulting capture mode to the authenticated scenario."""

    if args.scenario:
        return args.scenario
    return CAPTURE_DEFAULT_SCENARIO if args.capture else DEFAULT_SCENARIO


def _landing_page(scenario):
    """Developer-only landing page listing preview views and scenarios.

    All interpolated values are HTML-escaped even though they are constants.
    """

    view_items = "\n".join(
        f'      <li><a href="/preview/{escape(view)}">/preview/{escape(view)}</a></li>'
        for view in FLOW_VIEWS
    )
    scenario_items = "\n".join(
        f"      <li><code>--scenario {escape(name)}</code></li>" for name in SCENARIOS
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dashboard preview</title>
  <style>
    body {{ font-family: system-ui, sans-serif; background: #0f1115; color: #e6e9ee;
           margin: 0; padding: 2rem; line-height: 1.5; }}
    h1 {{ font-size: 1.25rem; }}
    h2 {{ font-size: 0.95rem; text-transform: uppercase; letter-spacing: .08em;
          color: #9aa4b2; margin-top: 1.75rem; }}
    a {{ color: #6ea8fe; }}
    ul {{ list-style: none; padding-left: 0; }}
    li {{ margin: .25rem 0; }}
    code {{ background: #1b1f27; padding: .1rem .35rem; border-radius: .25rem; }}
    .note {{ margin-top: 1.75rem; color: #9aa4b2; font-size: .9rem; }}
  </style>
</head>
<body>
  <h1>Dashboard preview</h1>
  <p>Current scenario: <code>{escape(scenario)}</code></p>
  <h2>Views</h2>
  <ul>
{view_items}
  </ul>
  <h2>Scenarios</h2>
  <p>Restart the server with one of:</p>
  <ul>
{scenario_items}
  </ul>
  <p class="note">Synthetic data only — no real hardware, secrets, runtime state,
  or device writes. For trusted local use only.</p>
</body>
</html>
"""


# Synthetic Maintenance-tab data. Mirrors the sanitized shapes the real
# /api/maintenance/* endpoints return so the tab renders fully in the preview.
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
        "template": "config.template.json",
        "items": [
            {"kind": "add", "path": "config_upgrade.on_startup", "value": "check"},
            {"kind": "add", "path": "dashboard.session_idle_timeout_seconds", "value": 1800},
            {"kind": "add", "path": "ha.token", "value": "<redacted>"},
            {
                "kind": "migrate",
                "path": "config_schema_version",
                "old_value": 2,
                "value": 3,
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

    def __init__(self, server_address, handler, scenario):
        super().__init__(server_address, handler)
        self.scenario_name = scenario


class PreviewHandler(BaseHTTPRequestHandler):
    server_version = "DashboardPreview/1.0"

    # --- request routing -------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/preview", "/preview/"):
            self._send_landing()
            return
        if path.startswith("/preview/"):
            self._send_preview(path[len("/preview/"):])
            return
        if path in ("/preview-diagnose.html", "/preview-logs.html"):
            # Backwards-compatible aliases for the original screenshot helper.
            view = "diagnose" if "diagnose" in path else "logs"
            self._send_preview(view)
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
            self._send_json(self._scenario()["diagnose"])
            return
        if path == "/api/logs":
            self._send_logs(parsed.query)
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

        self._send_static(path)

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
        return build_scenario(self.server.scenario_name)

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
    }

    def _history_series(self, query, source):
        # Columnar series derived from the synthetic snapshots. The same shape is
        # served for SQLite history and InfluxDB analytics; only ``source``
        # differs so the dashboard can label the data origin.
        items = self._scenario()["history"]
        requested = [s for s in (query.get("series", [""])[0] or "").split(",") if s]
        keys = [s for s in requested if s in self._SERIES_FIELDS] or [
            "pv",
            "output",
            "battery",
        ]
        time_axis = []
        cols = {key: [] for key in keys}
        for item in items:
            iso = str(item.get("timestamp", "")).replace("Z", "+00:00")
            try:
                epoch = int(time.mktime(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")))
            except (ValueError, TypeError):
                continue
            time_axis.append(epoch)
            for key in keys:
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
        """Preview-only write handler: never mutates disk, config, or devices."""

        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_json_body()

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

    def _send_landing(self):
        body = _landing_page(self.server.scenario_name).encode("utf-8")
        self._send_bytes(body, "text/html; charset=utf-8")

    def _send_preview(self, raw_view):
        view = raw_view.strip("/").lower()
        if view not in FLOW_VIEWS:
            self.send_error(404, "Unknown preview view")
            return
        index_path = os.path.join(STATIC_DIR, "index.html")
        with open(index_path, encoding="utf-8") as handle:
            html = handle.read()
        before, after = _preview_injection(view)
        seed = self._chart_seed_script(view)
        marker = '<script src="/app.js"></script>'
        html = html.replace(marker, f"{before}\n  {marker}{after}{seed}", 1)
        self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")

    def _chart_seed_script(self, view):
        """Inline the chart series so the headless screenshot is never blank.

        The captured charts otherwise show the async "Loading…" state because
        Firefox screenshots at the load event, before the fetch resolves (and a
        blocking XHR is rejected on the main thread). Embedding the same series
        the API would return lets the chart render synchronously on load.
        """

        scenario = self._scenario()
        auth = scenario["auth"]
        snapshot = scenario["snapshot"]
        runtime = scenario["runtime"]

        # Seed auth + runtime + the live snapshot so the page renders the logged-in
        # state of the scenario (e.g. write-mode shows the Control editor buttons
        # and the Diagnose/Logs content) instead of the async "Connecting" state.
        body = (
            "if(typeof setConnection==='function')setConnection('Live',true);"
            f"state.auth.configured={json.dumps(bool(auth.get('auth_configured')))};"
            f"state.auth.authenticated={json.dumps(bool(auth.get('authenticated')))};"
            f"state.auth.csrfToken={json.dumps(auth.get('csrf_token'))};"
            f"state.runtime={json.dumps(runtime)};"
            "if(typeof renderAuthState==='function')renderAuthState();"
            f"if(typeof updateSnapshot==='function')updateSnapshot({json.dumps(snapshot)});"
        )

        query = {"range": ["24h"], "series": ["pv,output,battery,soc,grid"]}
        if view in ("aggregated", "devices"):
            data = self._history_series(query, source="sqlite")
            body += (
                f"state.history.data={json.dumps(data)};"
                "if(typeof renderHistoryChart==='function')renderHistoryChart();"
                "if(typeof setHistoryLoading==='function')setHistoryLoading(false);"
            )
        elif view == "analytics":
            data = self._history_series(query, source="influxdb")
            body += (
                f"state.analytics.data={json.dumps(data)};"
                # Enable the Grid Power overlay so the analytics preview also
                # demonstrates the grid meter exchange line (import positive /
                # export negative) on top of the overview series.
                "if(state.analytics&&state.analytics.overlays)"
                "state.analytics.overlays.grid=true;"
                "if(typeof setAnalyticsAvailable==='function')setAnalyticsAvailable(true);"
                "if(typeof renderAnalytics==='function')renderAnalytics();"
                "if(typeof setAnalyticsLoading==='function')setAnalyticsLoading(false);"
            )
        elif view == "control":
            # Force the runtime editor (control buttons) to render with the
            # seeded auth + runtime, even before the async runtime fetch lands.
            body += (
                "if(typeof renderControlExplain==='function')"
                "renderControlExplain(state.snapshot,{forceRuntimeEditor:true});"
            )
        elif view == "diagnose":
            body += (
                f"state.diagnose.report={json.dumps(scenario['diagnose'])};"
                "if(typeof renderDiagnoseView==='function')renderDiagnoseView();"
            )
        elif view == "logs":
            body += (
                f"state.logs.lines={json.dumps(self._log_lines())};"
                "if(typeof applyLogs==='function')applyLogs();"
            )
        elif view == "maintenance":
            body += (
                f"state.maintenance.status={json.dumps(PREVIEW_MAINTENANCE['status'])};"
                f"state.maintenance.backups={json.dumps(PREVIEW_MAINTENANCE['backups'])};"
                "state.maintenance.configUpgrade="
                f"{json.dumps(PREVIEW_MAINTENANCE['config_upgrade'])};"
                "if(typeof setFlowView==='function')"
                "setFlowView('maintenance',false);"
            )
        return (
            "\n  <script>window.addEventListener('load',function(){"
            "try{if(typeof state!=='undefined'){" + body + "}}catch(e){}});</script>"
        )

    def _send_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        last_timestamp = None
        started = time.monotonic()
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
                time.sleep(2)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _log_lines(self, after=0):
        now = time.time()
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

    def _send_static(self, request_path):
        asset = STATIC_ASSETS.get(static_asset_key(request_path))
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


def start_server(host=DEFAULT_HOST, port=DEFAULT_PORT, scenario=DEFAULT_SCENARIO):
    """Start the preview server and return the running PreviewServer instance."""

    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}")
    try:
        server = PreviewServer((host, int(port)), PreviewHandler, scenario)
    except OSError as exc:
        raise SystemExit(
            f"could not bind preview server to {host}:{port} ({exc}). "
            "Is the port already in use? Try a different --port."
        )
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _print_urls(host, port, scenario):
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"Dashboard preview server: scenario={scenario}")
    print("Landing page:")
    print(f"  http://{display_host}:{port}/preview")
    print("Views:")
    for view in FLOW_VIEWS:
        print(f"  http://{display_host}:{port}/preview/{view}")
    print("Synthetic data only — no real hardware, secrets, or runtime state.")
    print("Press Ctrl+C to stop.")


def _print_scenarios():
    print("Scenarios:")
    for name in SCENARIOS:
        print(f"  {name}")


def _print_views():
    print("Views:")
    for view in FLOW_VIEWS:
        print(f"  {view}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Local dashboard preview server.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--scenario",
        default=None,
        choices=SCENARIOS,
        help="synthetic data scenario (default: normal; capture default: write-mode)",
    )
    parser.add_argument(
        "--capture",
        action="store_true",
        help="capture screenshots instead of serving interactively",
    )
    parser.add_argument(
        "--views",
        nargs="+",
        metavar="VIEW",
        help="views to capture: flow view names or 'all' (default: diagnose logs)",
    )
    parser.add_argument(
        "--output-dir",
        default=os.path.join(ROOT, "docs", "assets"),
    )
    parser.add_argument(
        "--list-scenarios",
        action="store_true",
        help="print available scenarios and exit",
    )
    parser.add_argument(
        "--list-views",
        action="store_true",
        help="print available views and exit",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if args.list_scenarios:
        _print_scenarios()
        return
    if args.list_views:
        _print_views()
        return

    scenario = resolve_scenario(args)
    try:
        views = normalize_views(args.views)
    except ValueError as exc:
        raise SystemExit(str(exc))

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"WARNING: binding preview server to {args.host}. The preview is "
            "intended for trusted local networks only."
        )

    server = start_server(args.host, args.port, scenario)
    try:
        if args.capture:
            from capture_dashboard_previews import capture_assets

            written = capture_assets(
                args.host, args.port, args.output_dir, scenario, views
            )
            print("Captured preview screenshots:")
            for path in written:
                print(f"  {os.path.relpath(path, ROOT)}")
            return
        _print_urls(args.host, args.port, scenario)
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nStopping preview server.")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
