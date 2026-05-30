import json
import logging
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from dashboard.sqlite_store import SUPPORTED_RANGES


STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, store):
        super().__init__(address, handler)
        self.store = store


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server_version = "EMSDashboard/1.0"

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

        if parsed.path == "/api/events":
            self._send_events()
            return

        self._send_static(parsed.path)

    def do_POST(self):
        self._send_json({"error": "read_only"}, status=405)

    def do_PUT(self):
        self._send_json({"error": "read_only"}, status=405)

    def do_PATCH(self):
        self._send_json({"error": "read_only"}, status=405)

    def do_DELETE(self):
        self._send_json({"error": "read_only"}, status=405)

    def log_message(self, fmt, *args):
        logging.debug("dashboard_http " + fmt, *args)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_timestamp = None

        while True:
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
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_dashboard_server(store, host="0.0.0.0", port=8080):
    server = DashboardHTTPServer((host, int(port)), DashboardRequestHandler, store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    logging.info("dashboard_started host=%s port=%s", host, port)
    return server
