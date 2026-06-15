# SPDX-License-Identifier: AGPL-3.0-or-later
import glob
import io
import json
import os
import tempfile
import time
import zipfile

import pytest

from dashboard.auth import write_password_file
from test_dashboard_server import (
    StoreStub,
    json_response,
    read_response,
    with_server,
)

SECRET_TOKEN = "leakytoken-SECRET-9999"


def write_diag_config(path):
    # grid_meter points at localhost so the hardware profile's probes fail fast
    # (connection refused) instead of timing out against an unroutable address.
    path.write_text(json.dumps({
        "system": {
            "enabled": True,
            "max_total_power": 900,
            "max_device_power": 800,
            "loop_interval": 5,
            "min_output_limit": 35,
            "runtime_state_path": "runtime-state.json",
        },
        "ha": {
            "enabled": True,
            "control_enabled": False,
            "url": "http://127.0.0.1:1",
            "token": SECRET_TOKEN,
        },
        "winter": {"enabled": False},
        "devices": [{"name": "WR1", "max_power": 800, "pv_priority_factor": 1.0}],
        "grid_meter": {"type": "shelly", "ip": "127.0.0.1"},
    }))


def write_diag_runtime(path):
    path.write_text(json.dumps({
        "timestamp": "2026-06-15T12:00:00+00:00",
        "grid_power_w": 142,
        "filtered_load_w": 131,
        "inverter_output_w": 130,
        "controller": {
            "enabled": True,
            "effective_target_total_w": 130,
            "commanded_total_w": 130,
            "filtered_load_w": 131,
        },
        "system": {
            "enabled": True,
            "max_total_power": 900,
            "min_output_limit": 35,
            "loop_interval": 5,
        },
        "winter": {"enabled": False},
        "devices": {
            "WR1": {
                "online": True,
                "enabled": True,
                "soc": 55,
                "min_soc": 15,
                "allocated_target_w": 130,
                "target_w": 130,
                "output_w": 130,
                "max_power": 800,
                "output_limit_w": 800,
            }
        },
    }))


def diag_server(tmp_path, configured=True):
    config_path = tmp_path / "config.json"
    runtime_path = tmp_path / "runtime-state.json"
    auth_file = tmp_path / "dashboard-auth.json"
    write_diag_config(config_path)
    write_diag_runtime(runtime_path)
    if configured:
        write_password_file(auth_file, "secret-password")

    server, base_url = with_server(
        StoreStub(),
        auth_file=str(auth_file),
        config_path=str(config_path),
        runtime_state_path=str(runtime_path),
    )
    return server, base_url


def login(base_url):
    _, headers, payload = json_response(
        f"{base_url}/api/auth/login",
        method="POST",
        payload={"password": "secret-password"},
    )
    return headers["Set-Cookie"], payload["csrf_token"]


def test_diagnose_install_returns_contract_when_authenticated(tmp_path):
    server, base_url = diag_server(tmp_path)
    try:
        cookie, _ = login(base_url)
        status, headers, payload = json_response(
            f"{base_url}/api/diagnose?profile=install",
            headers={"Cookie": cookie},
        )
        assert status == 200
        assert payload["schema_version"] == 1
        assert payload["profile"] == "install"
        assert payload["diagnosis"]["status"] in ("ok", "warning", "error")
        # security headers carried over from _send_json
        assert headers["Cache-Control"] == "no-store"
        assert "default-src 'self'" in headers["Content-Security-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    "profile", ["install", "deep", "hardware", "control", "control_quality"]
)
def test_diagnose_each_profile_returns_contract(tmp_path, profile):
    server, base_url = diag_server(tmp_path)
    try:
        cookie, _ = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/diagnose?profile={profile}",
            headers={"Cookie": cookie},
        )
        assert status == 200
        assert payload["schema_version"] == 1
        assert payload["profile"] == profile
        assert payload["diagnosis"]["status"] in ("ok", "warning", "error")
    finally:
        server.shutdown()
        server.server_close()


def test_diagnose_requires_authentication(tmp_path):
    server, base_url = diag_server(tmp_path)
    try:
        status, _, payload = json_response(f"{base_url}/api/diagnose?profile=install")
        assert status == 401
        assert payload["error"] == "not_authenticated"

        # forged / unknown session cookie is also rejected
        status, _, payload = json_response(
            f"{base_url}/api/diagnose?profile=install",
            headers={"Cookie": "ems_dashboard_session=forged-value"},
        )
        assert status == 401
        assert payload["error"] == "not_authenticated"
    finally:
        server.shutdown()
        server.server_close()


def test_diagnose_when_auth_not_configured_is_forbidden(tmp_path):
    server, base_url = diag_server(tmp_path, configured=False)
    try:
        status, _, payload = json_response(f"{base_url}/api/diagnose?profile=install")
        assert status == 403
        assert payload["error"] == "auth_not_configured"
    finally:
        server.shutdown()
        server.server_close()


def test_diagnose_unknown_profile_is_bad_request(tmp_path):
    server, base_url = diag_server(tmp_path)
    try:
        cookie, _ = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/diagnose?profile=bogus",
            headers={"Cookie": cookie},
        )
        assert status == 400
        assert payload["error"] == "invalid_profile"
        assert "install" in payload["supported"]
    finally:
        server.shutdown()
        server.server_close()


def test_diagnose_missing_profile_is_bad_request(tmp_path):
    server, base_url = diag_server(tmp_path)
    try:
        cookie, _ = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/diagnose",
            headers={"Cookie": cookie},
        )
        assert status == 400
        assert payload["error"] == "invalid_profile"
        assert "install" in payload["supported"]
    finally:
        server.shutdown()
        server.server_close()


def test_diagnose_empty_profile_is_bad_request(tmp_path):
    server, base_url = diag_server(tmp_path)
    try:
        cookie, _ = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/diagnose?profile=",
            headers={"Cookie": cookie},
        )
        assert status == 400
        assert payload["error"] == "invalid_profile"
        assert "install" in payload["supported"]
    finally:
        server.shutdown()
        server.server_close()


def test_diagnose_rejects_non_get(tmp_path):
    server, base_url = diag_server(tmp_path)
    try:
        cookie, _ = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/diagnose?profile=install",
            method="POST",
            payload={},
            headers={"Cookie": cookie},
        )
        assert status == 405
        assert payload["error"] == "read_only"
    finally:
        server.shutdown()
        server.server_close()


def test_diagnose_is_snapshot_only_and_ignores_sample_seconds(tmp_path):
    server, base_url = diag_server(tmp_path)
    try:
        cookie, _ = login(base_url)
        started = time.monotonic()
        status, _, payload = json_response(
            f"{base_url}/api/diagnose?profile=control&sample_seconds=30",
            headers={"Cookie": cookie},
        )
        elapsed = time.monotonic() - started
        assert status == 200
        # sample_seconds is forced to 0 server-side: no multi-second sleep.
        assert elapsed < 5
    finally:
        server.shutdown()
        server.server_close()


def test_diagnose_does_not_leak_secrets(tmp_path):
    server, base_url = diag_server(tmp_path)
    try:
        cookie, _ = login(base_url)
        status, _, body = read_response(
            f"{base_url}/api/diagnose?profile=deep",
            headers={"Cookie": cookie},
        )
        assert status == 200
        assert SECRET_TOKEN.encode() not in body
    finally:
        server.shutdown()
        server.server_close()


def test_diagnose_redacts_free_form_messages(tmp_path, monkeypatch):
    from ems import diagnostics

    def fake_install(_args):
        return {
            "schema_version": 1,
            "status": "warning",
            "diagnosis": {
                "status": "warning",
                "metrics": {"checks": 2, "token": SECRET_TOKEN},
                "warnings": [f"token={SECRET_TOKEN}"],
                "errors": [f"probe URL http://user:{SECRET_TOKEN}@example.test/path failed"],
                "sections": [],
                "root_causes": [],
            },
        }

    monkeypatch.setattr(diagnostics, "run_install_diagnosis", fake_install)
    server, base_url = diag_server(tmp_path)
    try:
        cookie, _ = login(base_url)
        status, _, body = read_response(
            f"{base_url}/api/diagnose?profile=install",
            headers={"Cookie": cookie},
        )
        assert status == 200
        assert SECRET_TOKEN.encode() not in body
        payload = json.loads(body.decode("utf-8"))
        assert payload["profile"] == "install"
        assert payload["diagnosis"]["status"] == "warning"
        assert payload["diagnosis"]["metrics"]["token"] == "<redacted>"
        assert "<redacted>" in payload["diagnosis"]["warnings"][0]
        assert "http://<redacted>:<redacted>@example.test" in payload["diagnosis"]["errors"][0]
    finally:
        server.shutdown()
        server.server_close()


def test_diagnose_single_flight_returns_busy(tmp_path):
    server, base_url = diag_server(tmp_path)
    try:
        cookie, _ = login(base_url)
        # Hold the single-flight lock to simulate a concurrent in-progress run.
        assert server.diagnose_lock.acquire(blocking=False)
        try:
            status, _, payload = json_response(
                f"{base_url}/api/diagnose?profile=install",
                headers={"Cookie": cookie},
            )
            assert status == 429
            assert payload["error"] == "diagnose_busy"
        finally:
            server.diagnose_lock.release()
    finally:
        server.shutdown()
        server.server_close()


def test_support_bundle_streams_redacted_zip(tmp_path):
    server, base_url = diag_server(tmp_path)
    try:
        cookie, _ = login(base_url)
        before = set(glob.glob(os.path.join(tempfile.gettempdir(), "ems-support-*.zip")))
        status, headers, body = read_response(
            f"{base_url}/api/diagnose/support-bundle",
            headers={"Cookie": cookie},
        )
        assert status == 200
        assert headers["Content-Type"] == "application/zip"
        assert "attachment" in headers["Content-Disposition"]

        archive = zipfile.ZipFile(io.BytesIO(body))
        names = set(archive.namelist())
        assert "diagnosis.json" in names
        assert "bundle-metadata.json" in names
        assert "redacted-config.json" in names
        # bundle is redacted: no plaintext secret, no raw config.json
        assert "config.json" not in names
        for name in names:
            assert SECRET_TOKEN.encode() not in archive.read(name)

        # server picks the tempfile and cleans it up (no leftover)
        after = set(glob.glob(os.path.join(tempfile.gettempdir(), "ems-support-*.zip")))
        assert after == before
    finally:
        server.shutdown()
        server.server_close()


def test_support_bundle_requires_auth(tmp_path):
    server, base_url = diag_server(tmp_path)
    try:
        status, _, payload = json_response(f"{base_url}/api/diagnose/support-bundle")
        assert status == 401
        assert payload["error"] == "not_authenticated"
    finally:
        server.shutdown()
        server.server_close()


def test_support_bundle_when_auth_not_configured_is_forbidden(tmp_path):
    server, base_url = diag_server(tmp_path, configured=False)
    try:
        status, _, payload = json_response(f"{base_url}/api/diagnose/support-bundle")
        assert status == 403
        assert payload["error"] == "auth_not_configured"
    finally:
        server.shutdown()
        server.server_close()


def test_support_bundle_rejects_non_get(tmp_path):
    server, base_url = diag_server(tmp_path)
    try:
        cookie, _ = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/diagnose/support-bundle",
            method="POST",
            payload={},
            headers={"Cookie": cookie},
        )
        assert status == 405
        assert payload["error"] == "read_only"
    finally:
        server.shutdown()
        server.server_close()
