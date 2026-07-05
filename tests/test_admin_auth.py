# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin Console authentication: shared password, sessions, CSRF, fresh install.

These tests drive the real Admin HTTP server without any auto-login helper, so
``_request`` here is intentionally unauthenticated and every handshake is
explicit. The shared password file must always land under the resolved EMS
install root (``config/dashboard-auth.json``), never inside the Admin container.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from admin import auth as admin_auth
from admin.server import create_server
from dashboard.auth import verify_password_file, write_password_file

pytestmark = pytest.mark.simulation


# --- path resolution --------------------------------------------------------


def test_resolve_admin_auth_paths_defaults_without_config(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    paths = admin_auth.resolve_admin_auth_paths()
    assert paths.config_exists is False
    assert paths.source == admin_auth.SOURCE_DEFAULT_MISSING_CONFIG
    assert paths.auth_file == tmp_path / "config" / "dashboard-auth.json"


def test_resolve_admin_auth_paths_honours_configured_relative_auth_file(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.json").write_text(
        json.dumps({"dashboard": {"auth_file": "config/custom-auth.json"}})
    )
    paths = admin_auth.resolve_admin_auth_paths()
    assert paths.source == admin_auth.SOURCE_CONFIG_DASHBOARD_AUTH_FILE
    assert paths.auth_file == tmp_path / "config" / "custom-auth.json"


def test_resolve_admin_auth_paths_honours_configured_absolute_auth_file(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    absolute = tmp_path / "elsewhere" / "auth.json"
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.json").write_text(
        json.dumps({"dashboard": {"auth_file": str(absolute)}})
    )
    paths = admin_auth.resolve_admin_auth_paths()
    assert paths.auth_file == absolute


def test_resolve_admin_auth_paths_falls_back_on_broken_config(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.json").write_text("{ not json")
    paths = admin_auth.resolve_admin_auth_paths()
    assert paths.source == admin_auth.SOURCE_DEFAULT_CONFIG_PARSE_FAILED
    assert paths.warning
    assert paths.auth_file == tmp_path / "config" / "dashboard-auth.json"


def _serve():
    srv = create_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _request(url, method="GET", body=None, headers=None):
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


# --- fresh install ----------------------------------------------------------


def test_admin_auth_status_requires_initial_password_on_fresh_install(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _serve()
    try:
        status, _, payload = _request(f"{base}/api/admin/auth/status")
        assert status == 200
        assert payload["auth_configured"] is False
        assert payload["requires_initial_password"] is True
        assert payload["shared_password_file"] == "config/dashboard-auth.json"
    finally:
        srv.shutdown()
        srv.server_close()


def test_admin_auth_setup_creates_shared_dashboard_auth_file(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _serve()
    try:
        status, headers, payload = _request(
            f"{base}/api/admin/auth/setup",
            method="POST",
            body={"password": "secret-password", "confirm_password": "secret-password"},
        )
        assert status == 200
        assert payload["authenticated"] is True
        assert payload["csrf_token"]
        assert "ems_admin_session=" in headers["Set-Cookie"]

        auth_file = tmp_path / "config" / "dashboard-auth.json"
        assert auth_file.exists()
        assert verify_password_file(auth_file, "secret-password")
    finally:
        srv.shutdown()
        srv.server_close()


def test_admin_auth_setup_does_not_require_config_json(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    assert not (tmp_path / "config" / "config.json").exists()
    srv, base = _serve()
    try:
        status, _, payload = _request(
            f"{base}/api/admin/auth/setup",
            method="POST",
            body={"password": "secret-password", "confirm_password": "secret-password"},
        )
        assert status == 200
        assert (tmp_path / "config" / "dashboard-auth.json").exists()
        assert not (tmp_path / "config" / "config.json").exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_admin_auth_setup_does_not_overwrite_existing_password(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    auth_file = tmp_path / "config" / "dashboard-auth.json"
    write_password_file(auth_file, "first-password")

    srv, base = _serve()
    try:
        status, _, payload = _request(
            f"{base}/api/admin/auth/setup",
            method="POST",
            body={"password": "second-password", "confirm_password": "second-password"},
        )
        assert status == 409
        assert payload["error"] == "auth_already_configured"
        assert verify_password_file(auth_file, "first-password")
        assert not verify_password_file(auth_file, "second-password")
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize(
    "body,error",
    [
        # A missing password field is still a malformed request.
        ({"confirm_password": "x"}, "password_required"),
        ({"password": "", "confirm_password": ""}, "password_required"),
        # Confirmation mismatch during setup is still rejected.
        ({"password": "abc", "confirm_password": "mismatch"}, "password_mismatch"),
    ],
)
def test_admin_auth_setup_validates_password(tmp_path, monkeypatch, body, error):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _serve()
    try:
        status, _, payload = _request(
            f"{base}/api/admin/auth/setup", method="POST", body=body
        )
        assert status == 400
        assert payload["error"] == error
        assert not (tmp_path / "config" / "dashboard-auth.json").exists()
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("password", ["x", "1234567", "short"])
def test_admin_auth_setup_accepts_short_password(tmp_path, monkeypatch, password):
    # The shared password has no length/complexity requirement: passwords shorter
    # than the old 8-character minimum must be accepted at setup.
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _serve()
    try:
        status, headers, payload = _request(
            f"{base}/api/admin/auth/setup",
            method="POST",
            body={"password": password, "confirm_password": password},
        )
        assert status == 200
        assert payload["authenticated"] is True
        assert "ems_admin_session=" in headers["Set-Cookie"]

        auth_file = tmp_path / "config" / "dashboard-auth.json"
        assert auth_file.exists()
        assert verify_password_file(auth_file, password)
    finally:
        srv.shutdown()
        srv.server_close()


def test_admin_login_accepts_short_password(tmp_path, monkeypatch):
    # A short password created at setup must also authenticate at login.
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    write_password_file(tmp_path / "config" / "dashboard-auth.json", "x")

    srv, base = _serve()
    try:
        status, headers, payload = _request(
            f"{base}/api/admin/auth/login",
            method="POST",
            body={"password": "x"},
        )
        assert status == 200
        assert payload["authenticated"] is True
        assert "ems_admin_session=" in headers["Set-Cookie"]
    finally:
        srv.shutdown()
        srv.server_close()


# --- existing password / login ---------------------------------------------


def test_admin_login_uses_existing_dashboard_password(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    auth_file = tmp_path / "config" / "dashboard-auth.json"
    write_password_file(auth_file, "dashboard-password")

    srv, base = _serve()
    try:
        status, _, payload = _request(f"{base}/api/admin/auth/status")
        assert status == 200
        assert payload["auth_configured"] is True
        assert payload["requires_initial_password"] is False

        status, headers, payload = _request(
            f"{base}/api/admin/auth/login",
            method="POST",
            body={"password": "dashboard-password"},
        )
        assert status == 200
        assert payload["authenticated"] is True
        assert payload["csrf_token"]
        assert "ems_admin_session=" in headers["Set-Cookie"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_admin_login_rejects_wrong_password(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    write_password_file(tmp_path / "config" / "dashboard-auth.json", "correct-horse")
    srv, base = _serve()
    try:
        status, _, payload = _request(
            f"{base}/api/admin/auth/login",
            method="POST",
            body={"password": "wrong"},
        )
        assert status == 403
        assert payload["error"] == "invalid_password"
    finally:
        srv.shutdown()
        srv.server_close()


def test_admin_auth_respects_dashboard_auth_file_from_config(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.json").write_text(
        json.dumps({"dashboard": {"auth_file": "config/custom-admin-auth.json"}})
    )
    write_password_file(tmp_path / "config" / "custom-admin-auth.json", "secret-password")

    srv, base = _serve()
    try:
        status, _, payload = _request(
            f"{base}/api/admin/auth/login",
            method="POST",
            body={"password": "secret-password"},
        )
        assert status == 200
        assert payload["authenticated"] is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_admin_auth_status_survives_unparseable_config(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.json").write_text("{ not json")
    write_password_file(tmp_path / "config" / "dashboard-auth.json", "secret-password")

    srv, base = _serve()
    try:
        status, _, payload = _request(f"{base}/api/admin/auth/status")
        assert status == 200
        # A broken config must not crash the login page; it falls back to the
        # default shared auth path, which already holds the password.
        assert payload["auth_configured"] is True
    finally:
        srv.shutdown()
        srv.server_close()


# --- malformed auth file (recovery, not first-password setup) ---------------


def _write_malformed_auth_file(tmp_path):
    auth_file = tmp_path / "config" / "dashboard-auth.json"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text("{not-json", encoding="utf-8")
    return auth_file


def test_admin_auth_status_reports_malformed_auth_file_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_malformed_auth_file(tmp_path)

    srv, base = _serve()
    try:
        status, _, payload = _request(f"{base}/api/admin/auth/status")
        assert status == 200
        assert payload["auth_configured"] is True
        assert payload["requires_initial_password"] is False
        assert payload["recovery_required"] is True
        assert payload["error"] == "auth_file_invalid"
    finally:
        srv.shutdown()
        srv.server_close()


def test_admin_auth_setup_does_not_overwrite_malformed_auth_file(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    auth_file = _write_malformed_auth_file(tmp_path)

    srv, base = _serve()
    try:
        status, _, payload = _request(
            f"{base}/api/admin/auth/setup",
            method="POST",
            body={"password": "secret-password", "confirm_password": "secret-password"},
        )
        assert status == 409
        assert payload["error"] == "auth_file_invalid"
        assert auth_file.read_text(encoding="utf-8") == "{not-json"
    finally:
        srv.shutdown()
        srv.server_close()


def test_admin_auth_login_reports_malformed_auth_file(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_malformed_auth_file(tmp_path)

    srv, base = _serve()
    try:
        status, _, payload = _request(
            f"{base}/api/admin/auth/login",
            method="POST",
            body={"password": "secret-password"},
        )
        assert status == 409
        assert payload["error"] == "auth_file_invalid"
    finally:
        srv.shutdown()
        srv.server_close()


def test_admin_auth_malformed_file_blocks_protected_api(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_malformed_auth_file(tmp_path)

    srv, base = _serve()
    try:
        status, _, payload = _request(f"{base}/api/admin/install-state")
        assert status in (401, 403)
        assert payload["error"] in {"not_authenticated", "auth_file_invalid"}
    finally:
        srv.shutdown()
        srv.server_close()


# --- protected endpoints ----------------------------------------------------


def test_admin_api_get_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    write_password_file(tmp_path / "config" / "dashboard-auth.json", "secret-password")
    srv, base = _serve()
    try:
        status, _, payload = _request(f"{base}/api/admin/install-state")
        assert status == 401
        assert payload["error"] == "not_authenticated"
    finally:
        srv.shutdown()
        srv.server_close()


def test_admin_api_get_blocked_before_password_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _serve()
    try:
        status, _, payload = _request(f"{base}/api/admin/install-state")
        assert status == 403
        assert payload["error"] == "auth_not_configured"

        # Discovery and setup surfaces are locked the same way.
        status, _, _ = _request(f"{base}/api/discovery/networks")
        assert status == 403
        status, _, _ = _request(f"{base}/api/setup/config/catalog")
        assert status == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_admin_api_post_requires_session_and_csrf(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    write_password_file(tmp_path / "config" / "dashboard-auth.json", "secret-password")
    srv, base = _serve()
    try:
        status, _, payload = _request(
            f"{base}/api/admin/start-path",
            method="POST",
            body={"choice": "setup_new", "confirm": False},
        )
        assert status == 401
        assert payload["error"] == "not_authenticated"

        login_status, login_headers, login_payload = _request(
            f"{base}/api/admin/auth/login",
            method="POST",
            body={"password": "secret-password"},
        )
        assert login_status == 200
        cookie = login_headers["Set-Cookie"].split(";", 1)[0]

        status, _, payload = _request(
            f"{base}/api/admin/start-path",
            method="POST",
            body={"choice": "setup_new", "confirm": False},
            headers={"Cookie": cookie},
        )
        assert status == 403
        assert payload["error"] == "csrf_failed"

        status, _, payload = _request(
            f"{base}/api/admin/start-path",
            method="POST",
            body={"choice": "setup_new", "confirm": False},
            headers={"Cookie": cookie, "X-CSRF-Token": login_payload["csrf_token"]},
        )
        assert status in (200, 409)
    finally:
        srv.shutdown()
        srv.server_close()


def test_admin_static_and_status_are_public(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    write_password_file(tmp_path / "config" / "dashboard-auth.json", "secret-password")
    srv, base = _serve()
    try:
        # The unauthenticated browser must still load the login/setup UI.
        status, _, _ = _request(f"{base}/api/admin/auth/status")
        assert status == 200
        with urllib.request.urlopen(f"{base}/") as resp:
            assert resp.status == 200
        with urllib.request.urlopen(f"{base}/admin.js") as resp:
            assert resp.status == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_admin_logout_clears_session_and_cookie(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    write_password_file(tmp_path / "config" / "dashboard-auth.json", "secret-password")
    srv, base = _serve()
    try:
        _, login_headers, _ = _request(
            f"{base}/api/admin/auth/login",
            method="POST",
            body={"password": "secret-password"},
        )
        cookie = login_headers["Set-Cookie"].split(";", 1)[0]

        status, headers, payload = _request(
            f"{base}/api/admin/auth/logout", method="POST", headers={"Cookie": cookie}
        )
        assert status == 200
        assert payload["authenticated"] is False
        assert "Max-Age=0" in headers["Set-Cookie"]

        # The destroyed session no longer grants read access.
        status, _, _ = _request(
            f"{base}/api/admin/install-state", headers={"Cookie": cookie}
        )
        assert status == 401
    finally:
        srv.shutdown()
        srv.server_close()


def test_admin_session_cookie_name_is_separate_from_dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    write_password_file(tmp_path / "config" / "dashboard-auth.json", "secret-password")
    srv, base = _serve()
    try:
        _, headers, _ = _request(
            f"{base}/api/admin/auth/login",
            method="POST",
            body={"password": "secret-password"},
        )
        set_cookie = headers["Set-Cookie"]
        assert "ems_admin_session=" in set_cookie
        assert "ems_dashboard_session=" not in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=Strict" in set_cookie
    finally:
        srv.shutdown()
        srv.server_close()
