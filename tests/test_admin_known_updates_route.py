# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTTP contract of the menu's update hint: signed in, read-only, never an error page."""

import threading

import pytest

from admin.releases import ReleaseManager
from admin.server import create_server
from tests.admin_auth_helpers import authenticate, raw_request
from tests.admin_auth_helpers import request as _request
from tests.helpers.system_alignment import SetupReadySystemAlignment

pytestmark = [pytest.mark.contract, pytest.mark.admin, pytest.mark.maintenance]

UPDATES = "/api/admin/maintenance/updates"


@pytest.fixture
def served(isolated_install_root, tmp_path):
    manager = ReleaseManager(data_dir=tmp_path / "admin-data", project_dir=tmp_path)
    srv = create_server(
        "127.0.0.1", 0, system_alignment=SetupReadySystemAlignment(), release_manager=manager
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield manager, base
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_hint_needs_a_signed_in_session(served):
    _manager, base = served
    authenticate(base)

    status, _headers, _payload = raw_request(f"{base}{UPDATES}")

    assert status == 401


def test_the_hint_is_what_the_release_manager_knows(served, monkeypatch):
    manager, base = served
    known = {"state": "available", "running": "v0.10.3", "recommended": "v0.11.0", "checked_at": "2026-11-18T10:00:00Z"}
    monkeypatch.setattr(manager, "known_update", lambda: known)
    authenticate(base)

    status, _headers, payload = _request(f"{base}{UPDATES}")

    assert status == 200 and payload == {"ok": True, "release": known}


def test_a_hint_that_cannot_be_read_is_not_knowing_rather_than_an_error(served, monkeypatch):
    manager, base = served

    def broken():
        raise RuntimeError("state directory unreadable")

    monkeypatch.setattr(manager, "known_update", broken)
    authenticate(base)

    status, _headers, payload = _request(f"{base}{UPDATES}")

    assert status == 200 and payload["release"]["state"] == "unknown"
