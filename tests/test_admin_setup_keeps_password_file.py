# SPDX-License-Identifier: AGPL-3.0-or-later
"""A setup replaces the config, never the password file the console is guarded by.

The Admin login reads where the shared password file lives from the config it
replaces. Moved to the default, the next request finds no password there: every
session ends and first-password setup opens to whoever reaches the console next.
"""

import json

import pytest

from admin.auth import resolve_admin_auth_paths
from tests.test_admin_maintenance_mqtt_apply import _request
from tests.test_admin_mqtt_credential_promotion_transaction import (
    _authorized,
    _discovery_with_proposal,
    _serve,
    _write_body,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.authority,
    pytest.mark.setup,
    pytest.mark.integration,
]


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


@pytest.mark.parametrize(
    "configured", ["secrets/admin-auth.json", "  secrets/admin-auth.json  "]
)
def test_a_setup_apply_keeps_the_session_and_the_password_file(
    tmp_path, monkeypatch, configured
):
    root = tmp_path / "install"
    (root / "config").mkdir(parents=True)
    (root / "secrets").mkdir()
    (root / "config" / "config.json").write_text(
        json.dumps({"dashboard": {"auth_file": configured, "port": 9999}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("EMS_INSTALL_DIR", str(root))
    before = resolve_admin_auth_paths().auth_file
    discovery = _discovery_with_proposal()
    srv, base = _serve(discovery, tmp_path)
    srv.credential_store.save_mqtt_discovery_secret("home", "user", "password")
    try:
        assert before == root / "secrets" / "admin-auth.json"
        assert before.exists()
        assert not (root / "config" / "dashboard-auth.json").exists()
        body = _authorized(base, {**_write_body(discovery), "overwrite": True})
        status, payload = _request(f"{base}/api/setup/config/apply", "POST", body)
        assert status == 200 and payload.get("ok") is True, payload

        written = json.loads((root / "config" / "config.json").read_text(encoding="utf-8"))
        assert written["dashboard"]["auth_file"] == "secrets/admin-auth.json"
        assert written["dashboard"].get("port") != 9999
        assert resolve_admin_auth_paths().auth_file == before
        status, payload = _request(f"{base}/api/admin/auth/status")
        assert status == 200 and payload.get("auth_configured") is True, payload
        status, payload = _request(f"{base}/api/setup/workflow")
        assert status == 200, payload
    finally:
        srv.shutdown()
        srv.server_close()
