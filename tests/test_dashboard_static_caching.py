# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static-asset caching contract for the dashboard server.

Every tab used to re-download and re-parse the whole frontend on every load
because the static responses carried ``Cache-Control: no-store`` and no
validator. These pin the revalidation path that replaced it, and that API
responses were not made cacheable along the way.

http.client is used rather than urllib because urllib turns a 304 into an
exception and hides the response headers this contract is about.
"""

import http.client
from urllib.parse import urlsplit

import pytest

from dashboard.server import start_dashboard_server

pytestmark = [
    pytest.mark.integration,
]


class StoreStub:
    def latest(self):
        return {"timestamp": "2026-06-03T12:00:00+00:00", "pv_total_w": 1200}

    def history(self, _range_name):
        return []


def with_server(**kwargs):
    try:
        server = start_dashboard_server(
            StoreStub(), host="127.0.0.1", port=0, **kwargs
        )
    except PermissionError as exc:
        pytest.skip(f"local socket creation is not permitted: {exc}")
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def request(base_url, path, headers=None):
    parts = urlsplit(base_url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    try:
        conn.request("GET", path, headers=headers or {})
        response = conn.getresponse()
        body = response.read()
        return response.status, dict(response.getheaders()), body
    finally:
        conn.close()


STATIC_PATHS = ("/", "/app.js", "/styles.css")


@pytest.mark.parametrize("path", STATIC_PATHS)
def test_static_asset_is_revalidatable(path):
    server, base_url = with_server()
    try:
        status, headers, body = request(base_url, path)
        assert status == 200
        assert body, "a static asset must have a body"
        assert headers.get("ETag"), f"{path} must carry a validator"
        cache_control = headers.get("Cache-Control", "")
        assert "no-store" not in cache_control, (
            f"{path} may not be uncacheable; that is the defect being fixed"
        )
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("path", STATIC_PATHS)
def test_matching_etag_returns_304_without_a_body(path):
    server, base_url = with_server()
    try:
        _, headers, body = request(base_url, path)
        etag = headers["ETag"]

        status, cached_headers, cached_body = request(
            base_url, path, headers={"If-None-Match": etag}
        )
        assert status == 304
        assert cached_body == b"", "a 304 must not resend the asset"
        assert cached_headers.get("ETag") == etag
        assert len(body) > 0
    finally:
        server.shutdown()
        server.server_close()


def test_stale_etag_returns_the_asset_again():
    server, base_url = with_server()
    try:
        status, _, body = request(
            base_url, "/app.js", headers={"If-None-Match": '"not-the-current-one"'}
        )
        assert status == 200
        assert body
    finally:
        server.shutdown()
        server.server_close()


def test_security_headers_survive_a_304():
    server, base_url = with_server()
    try:
        _, headers, _ = request(base_url, "/app.js")
        status, cached_headers, _ = request(
            base_url, "/app.js", headers={"If-None-Match": headers["ETag"]}
        )
        assert status == 304
        assert cached_headers.get("X-Content-Type-Options") == "nosniff"
        assert cached_headers.get("Content-Security-Policy")
    finally:
        server.shutdown()
        server.server_close()


def test_api_responses_stay_uncacheable():
    server, base_url = with_server()
    try:
        status, headers, _ = request(base_url, "/api/ui-config")
        assert status == 200
        assert "no-store" in headers.get("Cache-Control", ""), (
            "live API responses must never become cacheable"
        )
        assert not headers.get("ETag")
    finally:
        server.shutdown()
        server.server_close()
