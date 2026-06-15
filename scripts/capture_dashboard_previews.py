# SPDX-License-Identifier: AGPL-3.0-or-later
"""Capture README/dashboard preview screenshots for operator-only tabs.

This helper serves the real dashboard static files with synthetic, non-secret
demo API responses, then uses Firefox headless and ImageMagick `convert` to
write the Diagnose and Logs preview JPGs.

Usage:
    python3 scripts/capture_dashboard_previews.py
    python3 scripts/capture_dashboard_previews.py --serve-only
"""

import argparse
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(ROOT, "dashboard", "static")
DEFAULT_PORT = 8767


DIAGNOSE_REPORT = {
    "schema_version": 1,
    "profile": "hardware",
    "status": "warning",
    "diagnosis": {
        "version": 1,
        "timestamp": "2026-06-15T12:00:00+00:00",
        "status": "warning",
        "metrics": {
            "ok": 18,
            "warning": 3,
            "error": 0,
            "devices": 2,
        },
        "warnings": [
            "Dashboard database has no recent energy rows.",
            "WR2 read-only probe returned a transient timeout.",
        ],
        "errors": [],
        "root_causes": [
            {
                "code": "dashboard_data_gap",
                "severity": "warning",
                "title": "Dashboard data gap",
                "message": "The dashboard database is reachable, but recent telemetry rows are sparse.",
                "suggested_next_check": "Check the EMS loop and dashboard write interval.",
            }
        ],
        "sections": [
            {
                "id": "environment",
                "title": "Environment",
                "status": "ok",
                "warnings": [],
                "errors": [],
            },
            {
                "id": "config",
                "title": "Config",
                "status": "ok",
                "warnings": [],
                "errors": [],
            },
            {
                "id": "hardware",
                "title": "Hardware",
                "status": "warning",
                "warnings": [
                    "Zendure device WR2 read-only probe failed: TimeoutError",
                ],
                "errors": [],
            },
            {
                "id": "dashboard",
                "title": "Dashboard",
                "status": "warning",
                "warnings": [
                    "SQLite table snapshots latest row is older than 1 hour",
                ],
                "errors": [],
            },
        ],
    },
}


LOG_LINES = [
    (1, "INFO", "ems.startup event=startup dry_run=false simulation=false dashboard=true"),
    (2, "INFO", "dashboard_started host=0.0.0.0 port=8080 https=false"),
    (3, "INFO", "control_cycle filtered_load_w=792 target_total_w=800 commanded_total_w=800"),
    (4, "DEBUG", "allocation device=WR1 pv_input_w=1200 target_w=320 decision=deadband"),
    (5, "DEBUG", "allocation device=WR2 pv_input_w=650 target_w=480 decision=send"),
    (6, "WARNING", "diagnose hardware probe timeout device=WR2 endpoint=/properties/report"),
    (7, "INFO", "event=dashboard_log_level_changed level=DEBUG"),
    (8, "INFO", "runtime_state_saved path=data/runtime-state.json"),
    (9, "ERROR", "grid_meter_read_error type=shelly reason=temporary HTTP 503"),
    (10, "INFO", "grid_meter_recovered type=shelly power_w=804"),
    (11, "INFO", "control_cycle filtered_load_w=804 target_total_w=800 commanded_total_w=800"),
]


def demo_snapshot():
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "pv_total_w": 1850,
        "inverter_output_w": 800,
        "home_load_w": 800,
        "grid_power_w": 0,
        "battery_power_w": 1050,
        "average_soc": 59.5,
        "controller": {
            "enabled": True,
            "max_total_power_w": 800,
            "min_output_limit_w": 0,
            "allocated_target_total_w": 800,
            "effective_target_total_w": 800,
            "commanded_total_w": 800,
            "filtered_load_w": 800,
        },
        "rules": {
            "ems_enabled": {
                "active": True,
                "reason": "demo mode static control preview",
            },
            "pv_priority_balancing": {
                "active": True,
                "reason": "WR1 keeps more PV available for charging",
            },
            "battery_balancing": {
                "active": True,
                "reason": "two devices share an 800 W system limit",
            },
        },
        "devices": {
            "WR1": {
                "online": True,
                "enabled": True,
                "soc": 62,
                "battery_power_w": 880,
                "pack_input_w": 20,
                "pack_output_w": 900,
                "pv_input_w": 1200,
                "output_w": 320,
                "target_w": 320,
                "allocated_target_w": 320,
                "output_limit_w": 320,
                "mode": "solar",
            },
            "WR2": {
                "online": True,
                "enabled": True,
                "soc": 57,
                "battery_power_w": 170,
                "pack_input_w": 30,
                "pack_output_w": 200,
                "pv_input_w": 650,
                "output_w": 480,
                "target_w": 480,
                "allocated_target_w": 480,
                "output_limit_w": 480,
                "mode": "solar",
            },
        },
    }


def runtime_state():
    return {
        "system": {
            "enabled": True,
            "max_total_power": 800,
            "loop_interval": 5,
            "min_output_limit": 0,
        },
        "ha": {
            "enabled": False,
            "control_enabled": False,
        },
        "winter": {
            "enabled": False,
        },
        "devices": {
            "WR1": {
                "enabled": True,
                "max_power": 800,
                "offgrid_socket_mode": "off",
                "pv_priority_factor": 1.2,
            },
            "WR2": {
                "enabled": True,
                "max_power": 800,
                "offgrid_socket_mode": "off",
                "pv_priority_factor": 1.0,
            },
        },
    }


class PreviewHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/preview-diagnose.html", "/preview-logs.html"):
            self._send_preview_html(parsed.path)
            return
        if parsed.path == "/api/auth/status":
            self._send_json({
                "auth_configured": True,
                "authenticated": True,
                "csrf_token": "demo-csrf",
                "write_mode_available": True,
                "write_mode_active": True,
            })
            return
        if parsed.path == "/api/live":
            self._send_json(demo_snapshot())
            return
        if parsed.path == "/api/runtime":
            self._send_json(runtime_state())
            return
        if parsed.path == "/api/history":
            self._send_json({"range": "6h", "items": []})
            return
        if parsed.path == "/api/diagnose":
            self._send_json(DIAGNOSE_REPORT)
            return
        if parsed.path == "/api/logs":
            self._send_logs(parsed.query)
            return
        self._send_static(parsed.path)

    def log_message(self, _fmt, *_args):
        return

    def _send_logs(self, query_string):
        query = parse_qs(query_string)
        after = int((query.get("after", ["0"]) or ["0"])[0] or "0")
        now = time.time()
        lines = [
            {
                "seq": seq,
                "ts": now - (len(LOG_LINES) - seq) * 14,
                "level": level,
                "logger": "ems",
                "message": message,
            }
            for seq, level, message in LOG_LINES
            if seq > after
        ]
        self._send_json({
            "lines": lines,
            "cursor": LOG_LINES[-1][0],
            "dropped": False,
            "service_level": "DEBUG",
        })

    def _send_preview_html(self, path):
        with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
            html = f.read()
        view = "diagnose" if path == "/preview-diagnose.html" else "logs"
        before = (
            "<script>"
            f"try{{window.localStorage.setItem('dashboard.flowView','{view}');}}catch(e){{}}"
            "</script>"
        )
        after = f"""
  <script>
    window.addEventListener("load", () => {{
      const snapshot = demoSnapshot();
      updateSnapshot(snapshot);
      setConnection("Demo", true);
      setInterval(() => setConnection("Demo", true), 100);
      state.history = demoHistory(snapshot);
      state.runtime = demoRuntimeState();
      state.auth = {{ configured: true, authenticated: true, csrfToken: "demo-csrf" }};
      renderAuthState();
      if ({json.dumps(view)} === "diagnose") {{
        state.diagnose.profile = "hardware";
        state.diagnose.report = {json.dumps(DIAGNOSE_REPORT)};
        setFlowView("diagnose", false);
        renderDiagnoseView();
      }} else {{
        state.logs.serviceLevel = "DEBUG";
        state.logs.lines = {json.dumps([
            {"seq": seq, "level": level, "logger": "ems", "message": message}
            for seq, level, message in LOG_LINES
        ])}.map((line, index) => ({{ ...line, ts: Date.now() / 1000 - (10 - index) * 14 }}));
        state.logs.cursor = 11;
        setFlowView("logs", false);
        applyLogs();
      }}
      document.body.dataset.screenshotReady = "true";
    }});
  </script>
"""
        html = html.replace(
            '<script src="/app.js"></script>',
            before + '\n  <script src="/app.js"></script>' + after,
        )
        self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")

    def _send_static(self, path):
        path = "/index.html" if path in ("", "/") else path
        full_path = os.path.abspath(os.path.join(STATIC_DIR, path.lstrip("/")))
        static_root = os.path.abspath(STATIC_DIR)
        if (
            os.path.commonpath([static_root, full_path]) != static_root
            or not os.path.isfile(full_path)
        ):
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
        with open(full_path, "rb") as f:
            self._send_bytes(f.read(), content_type)

    def _send_json(self, payload):
        self._send_bytes(
            json.dumps(payload, sort_keys=True).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _send_bytes(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def require_executable(name):
    path = shutil.which(name)
    if not path:
        raise SystemExit(f"required executable not found: {name}")
    return path


def capture_assets(port, output_dir):
    firefox = require_executable("firefox")
    convert = require_executable("convert")
    os.makedirs(output_dir, exist_ok=True)
    targets = {
        "diagnose": "preview-diagnose.jpg",
        "logs": "preview-logs.jpg",
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        for view, filename in targets.items():
            png_path = os.path.join(tmpdir, f"{view}.png")
            url = f"http://127.0.0.1:{port}/preview-{view}.html"
            subprocess.run(
                [
                    firefox,
                    "--headless",
                    "--window-size=1440,1200",
                    "--screenshot",
                    png_path,
                    url,
                ],
                check=True,
                cwd=ROOT,
            )
            subprocess.run(
                [
                    convert,
                    png_path,
                    "-quality",
                    "88",
                    os.path.join(output_dir, filename),
                ],
                check=True,
                cwd=ROOT,
            )


def start_server(port):
    server = ThreadingHTTPServer(("127.0.0.1", port), PreviewHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--output-dir",
        default=os.path.join(ROOT, "docs", "assets"),
    )
    parser.add_argument("--serve-only", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    server = start_server(args.port)
    if args.serve_only:
        print(f"Serving preview pages on http://127.0.0.1:{args.port}")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return
    try:
        capture_assets(args.port, args.output_dir)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
