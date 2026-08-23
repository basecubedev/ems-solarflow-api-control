#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drive the *installed* Appliance Manager web service over HTTP.

Copied into a test guest and run there with the system Python. It imports
nothing from the repository — the point is to exercise the packaged service
through the same interface a browser uses, across the packaged users, the
packaged directories and the real Unix socket.

Usage: appliance_http_client.py <base-url> <password>
"""

import json
import sys
import urllib.error
import urllib.request

REPORT_MARKER = "APPLIANCE_HTTP_REPORT:"
CSRF_HEADER = "X-Appliance-CSRF"


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.cookie = ""
        self.csrf = ""

    def request(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self.cookie:
            request.add_header("Cookie", self.cookie)
        if self.csrf:
            request.add_header(CSRF_HEADER, self.csrf)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
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


def main(argv):
    # Required, not defaulted: this script is copied into the guest and cannot
    # import the module that owns the port. A fallback here would answer the
    # question the caller was supposed to answer, and answer it wrongly the
    # moment the service moves.
    if len(argv) < 2 or not argv[1].strip():
        raise SystemExit("usage: appliance_http_client.py <base-url> [password]")
    base = argv[1]
    password = argv[2] if len(argv) > 2 else "packaged-smoke-password"
    client = Client(base)
    report = {}

    report["initial_session"] = client.request("GET", "/api/session")
    configured = bool((report["initial_session"].get("body") or {}).get("password_configured"))

    if configured:
        report["setup"] = {"completed": True, "status": 409, "body": {"skipped": True}}
        report["login"] = client.request("POST", "/api/session/login", {"password": password})
    else:
        report["setup"] = client.request(
            "POST", "/api/session/setup", {"password": password, "confirmation": password}
        )
        report["login"] = {"completed": True, "status": 200, "body": {"skipped": True}}

    report["status"] = client.request("GET", "/api/status")
    report["session"] = client.request("GET", "/api/session")
    report["settings"] = client.request("GET", "/api/settings")

    # A repair plan is read-only: it inspects and waits for a confirmation that
    # this smoke test never gives.
    report["plan"] = client.request("POST", "/api/admin/repair", {})
    operation_id = ((report["plan"].get("body") or {}).get("operation") or {}).get("operation_id")
    if operation_id:
        report["cancel"] = client.request(
            "POST", "/api/operations/cancel", {"operation_id": operation_id}
        )
    report["logout"] = client.request("POST", "/api/session/logout")
    report["after_logout"] = client.request("GET", "/api/status")

    print(REPORT_MARKER + json.dumps(report, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
