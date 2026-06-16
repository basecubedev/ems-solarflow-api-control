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

Safety: the preview never contacts real Zendure/Shelly/MQTT/InfluxDB/Home
Assistant endpoints, never reads secrets from config.json, and never writes
runtime-state, auth files, or the dashboard database. It binds to loopback by
default.
"""

import argparse
import json
import mimetypes
import os
import time
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dashboard_preview_data import (  # noqa: E402
    DEFAULT_SCENARIO,
    FLOW_VIEWS,
    SCENARIOS,
    build_scenario,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(ROOT, "dashboard", "static")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8767
# Capture mode is operator-oriented (Diagnose/Logs), so it defaults to the
# authenticated scenario unless the user explicitly selects one.
CAPTURE_DEFAULT_SCENARIO = "write-mode"
DEFAULT_CAPTURE_VIEWS = ("diagnose", "logs")


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

    def _handle_write(self):
        """Preview-only write handler: never mutates disk, config, or devices."""

        parsed = urlparse(self.path)
        path = parsed.path
        self._drain_body()

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
        marker = '<script src="/app.js"></script>'
        html = html.replace(marker, f"{before}\n  {marker}{after}", 1)
        self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")

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
                    self.wfile.write(f"event: telemetry\ndata: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    last_timestamp = timestamp
                time.sleep(2)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _send_logs(self, query_string):
        query = parse_qs(query_string)
        after = int((query.get("after", ["0"]) or ["0"])[0] or "0")
        now = time.time()
        log_lines = self._scenario()["logs"]
        lines = [
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
        self._send_json({
            "lines": lines,
            "cursor": log_lines[-1][0] if log_lines else 0,
            "dropped": False,
            "service_level": "DEBUG",
        })

    def _send_static(self, request_path):
        path = "/index.html" if request_path in ("", "/") else request_path
        normalized = os.path.normpath(path.lstrip("/"))
        full_path = os.path.abspath(os.path.join(STATIC_DIR, normalized))
        static_root = os.path.abspath(STATIC_DIR)
        if (
            os.path.commonpath([static_root, full_path]) != static_root
            or not os.path.isfile(full_path)
        ):
            self.send_error(404, "Not Found")
            return
        content_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
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
