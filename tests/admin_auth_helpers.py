# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared helpers so admin HTTP tests can authenticate against the Admin server.

The Admin Console now protects every non-auth API behind a shared password and
an ``ems_admin_session`` cookie. These helpers let the existing HTTP tests keep
exercising the real endpoints by transparently creating the shared password and
attaching the session cookie / CSRF token, without each test having to spell out
the login handshake.
"""

import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit

TEST_PASSWORD = "admin-test-password"

# base URL -> (cookie header value, csrf token) for authenticated servers.
_CREDENTIALS = {}


def _base_of(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def raw_request(url, method="GET", body=None, headers=None):
    """Issue a request without attaching any Admin session automatically."""

    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.headers, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, json.loads(exc.read() or b"null")


def authenticate(base, password=TEST_PASSWORD):
    """Create (or reuse) the shared password and log in, caching the session."""

    status, headers, payload = raw_request(
        f"{base}/api/admin/auth/setup",
        method="POST",
        body={"password": password, "confirm_password": password},
    )
    if status == 409:
        status, headers, payload = raw_request(
            f"{base}/api/admin/auth/login",
            method="POST",
            body={"password": password},
        )
    cookie_header = headers.get("Set-Cookie", "") if headers else ""
    cookie = cookie_header.split(";", 1)[0] if cookie_header else ""
    csrf = (payload or {}).get("csrf_token")
    _CREDENTIALS[base] = (cookie, csrf)
    return cookie, csrf


def auth_headers(url, method):
    """Session/CSRF headers for a registered base URL (empty if unregistered)."""

    creds = _CREDENTIALS.get(_base_of(url))
    if not creds:
        return {}
    cookie, csrf = creds
    headers = {}
    if cookie:
        headers["Cookie"] = cookie
    if csrf and method.upper() != "GET":
        headers["X-CSRF-Token"] = csrf
    return headers


def request(url, method="GET", body=None, headers=None):
    """Request with the registered Admin session/CSRF attached automatically."""

    merged = dict(auth_headers(url, method))
    merged.update(headers or {})
    return raw_request(url, method=method, body=body, headers=merged)
