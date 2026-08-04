# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local docs-preview server for the Admin Console.

Serves the real admin static assets (``admin/static/``) with deterministic,
non-secret demo API responses loaded from ``tests/fixtures/admin_docs/`` so the
Admin Console screens can be screenshotted for the docs without real hardware,
Docker, discovery, MQTT, config.json, passwords, or a running EMS.

A small drive script (``admin_docs_preview.js``, injected before ``</body>``)
reads a ``?screen=`` query parameter and navigates the already-authenticated
Admin SPA to the requested screen so each URL renders one documented view.

Usage::

    python3 scripts/serve_admin_docs_preview.py
    python3 scripts/serve_admin_docs_preview.py --host 127.0.0.1 --port 8092

Open, for example::

    http://127.0.0.1:8092/?screen=landing
    http://127.0.0.1:8092/?screen=maintenance-overview
    http://127.0.0.1:8092/?screen=guided-upgrade

Safety: the preview never contacts real Zendure/Shelly/MQTT/Docker/InfluxDB
endpoints, never reads secrets from config.json, and never writes runtime-state,
auth files, config or the dashboard database. It binds to loopback by default.
"""

import argparse
import base64
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
STATIC_DIR = os.path.join(ROOT, "admin", "static")
FIXTURE_DIR = os.path.join(ROOT, "tests", "fixtures", "admin_docs")
DRIVE_SCRIPT = os.path.join(SCRIPT_DIR, "admin_docs_preview.js")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8092

# Screens that need an active, server-confirmed Fresh Setup transition to be
# reachable at all (Guided Setup steps 02-05).
SETUP_STEP_SCREENS = frozenset(
    {"discovery", "config-preview", "setup-deployment", "setup-start-done"}
)

# Firefox ``--screenshot`` captures at the ``load`` event and does not wait for
# post-load fetches. A hidden image pointed at ``/__hold`` deliberately delays
# ``load`` so the SPA has time to authenticate, fetch its demo data, navigate to
# the requested screen and render before the screenshot is taken.
HOLD_SECONDS = 5.0
# 1x1 transparent PNG.
_HOLD_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _load_fixture(name):
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as handle:
        return json.load(handle)


def build_routes():
    """Map (method, api-path) to the demo JSON payload the SPA expects."""

    common = _load_fixture("common.json")
    overview = _load_fixture("maintenance_overview_demo.json")
    backups = _load_fixture("backup_restore_demo.json")
    upgrade = _load_fixture("guided_upgrade_demo.json")
    setup = _load_fixture("guided_setup_demo.json")
    zendure_mqtt = _load_fixture("zendure_mqtt_demo.json")

    get_routes = {
        "/api/admin/auth/status": common["auth_status"],
        "/api/admin/install-state": common["install_state"],
        "/api/discovery/mdns/status": common["mdns_status"],
        "/api/discovery/devices": common["mdns_devices"],
        "/api/discovery/mqtt-brokers": common["mqtt_brokers"],
        "/api/admin/maintenance/admin-update/resume": common["admin_update_resume"],
        "/api/admin/maintenance/overview": overview["overview"],
        "/api/admin/maintenance/config": overview["config"],
        "/api/admin/maintenance/containers/plan": overview["containers_plan"],
        "/api/admin/maintenance/backups": backups["backups_list"],
        "/api/admin/maintenance/zendure-mqtt/runtime-status": zendure_mqtt["runtime_status"],
        "/api/admin/maintenance/admin-update/status": upgrade["admin_update_status"],
        "/api/setup/config-template": setup["config_template"],
        "/api/setup/config/catalog": setup["config_catalog"],
        "/api/discovery/networks": setup["networks"],
        "/api/setup/deployment/plan": setup["deployment_plan"],
        "/api/setup/deployment/status": setup["deployment_status"],
        # Without this the generic empty-payload fallback would clear
        # generated_ready and re-lock the deployment/start steps.
        "/api/setup/config/status": setup["config_status"],
    }
    post_routes = {
        "/api/setup/config-preview": setup["config_preview"],
        "/api/admin/maintenance/admin-update/plan": upgrade["admin_update_plan"],
        "/api/admin/system-alignment/validate": setup["system_build_validate"],
    }
    # /api/setup/releases branches on the ?flow= query parameter.
    releases = {
        "upgrade": upgrade["releases_upgrade"],
        "setup": setup["releases_setup"],
    }
    # Guided Setup steps 02-05 are authorized only by a server-confirmed setup
    # transition, never by browser state, so those screens are served one. Every
    # other screen gets the idle payload — an always-active transition would make
    # the SPA resume into Guided Setup and take over the landing page.
    alignment = {
        "active": setup["system_alignment_status"],
        "idle": setup["system_alignment_idle"],
    }
    return get_routes, post_routes, releases, alignment


class DocsPreviewHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the preview quiet
        pass

    def _requesting_screen(self):
        """The ?screen= of the page that issued this API call.

        Same-origin XHR carries the full page URL in Referer, which is the only
        way a per-screen payload can be chosen: the SPA's own fetches cannot
        carry the screen, and letting the browser pick would put presentation
        state in charge of workflow authority.
        """

        referer = self.headers.get("Referer") or ""
        return parse_qs(urlparse(referer).query).get("screen", [""])[0]

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, filename, content_type):
        try:
            with open(os.path.join(STATIC_DIR, filename), "rb") as handle:
                body = handle.read()
        except OSError:
            self.send_error(404)
            return
        self._send_bytes(body, content_type)

    def _send_index(self):
        try:
            with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as handle:
                html = handle.read()
        except OSError:
            self.send_error(404)
            return
        inject = (
            '  <img src="/__hold" alt="" width="1" height="1"'
            ' style="position:absolute;left:-9999px;opacity:0" aria-hidden="true">\n'
            '  <script src="/_docs_preview.js"></script>\n</body>'
        )
        html = html.replace("</body>", inject, 1)
        self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send_index()
            return
        if path == "/admin.js":
            self._send_static("admin.js", "application/javascript")
            return
        if path == "/admin.css":
            self._send_static("admin.css", "text/css")
            return
        if path == "/__hold":
            # Slow response that holds the load event open (see HOLD_SECONDS).
            time.sleep(HOLD_SECONDS)
            self._send_bytes(_HOLD_PNG, "image/png")
            return
        if path == "/_docs_preview.js":
            try:
                with open(DRIVE_SCRIPT, "rb") as handle:
                    body = handle.read()
            except OSError:
                self.send_error(404)
                return
            self._send_bytes(body, "application/javascript")
            return
        if path == "/api/setup/releases":
            flow = parse_qs(parsed.query).get("flow", ["setup"])[0]
            self._send_json(self.server.releases.get(flow, self.server.releases["setup"]))
            return
        if path == "/api/admin/system-alignment/status":
            key = "active" if self._requesting_screen() in SETUP_STEP_SCREENS else "idle"
            self._send_json(self.server.alignment[key])
            return
        if path in self.server.get_routes:
            self._send_json(self.server.get_routes[path])
            return
        if path.startswith("/api/"):
            # Any other read the SPA attempts degrades to a benign empty payload.
            self._send_json({"ok": True})
            return
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if path in self.server.post_routes:
            self._send_json(self.server.post_routes[path])
            return
        if path.startswith("/api/"):
            self._send_json({"ok": True})
            return
        self.send_error(404)


def start_server(host, port):
    get_routes, post_routes, releases, alignment = build_routes()
    server = ThreadingHTTPServer((host, port), DocsPreviewHandler)
    server.get_routes = get_routes
    server.post_routes = post_routes
    server.releases = releases
    server.alignment = alignment
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    server = start_server(args.host, args.port)
    print(f"Admin docs preview on http://{args.host}:{args.port}/?screen=landing")
    print("Screens: landing, guided-setup-start, discovery, config-preview,")
    print("         maintenance-overview, backup-restore, guided-upgrade,")
    print("         upgrade-run-1, upgrade-run-2, upgrade-run-3, upgrade-run-4,")
    print("         upgrade-done, admin-update-reconnect")
    try:
        import time

        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
