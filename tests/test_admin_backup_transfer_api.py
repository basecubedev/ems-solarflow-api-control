# SPDX-License-Identifier: AGPL-3.0-or-later
"""A backup that cannot leave the machine is not a backup.

Admin could create, inspect, preview, restore and delete archives, but never
hand one over or take one back. The documented recovery for a failed OS upgrade
is to re-flash and restore a backup — which was on the card being re-flashed.

Both directions go through the existing owner: export resolves the id the way
every other route does (``BackupStore.resolve``, which only ever matches names
discovered by listing the backup directory), and import lands a validated file
in that same directory so list/inspect/preview/restore pick it up with no
second authority and no second format.
"""

import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from admin.backup_restore import BackupRestoreService
from admin.install_context import detect_install_context
from admin.server import create_server
from tests.admin_auth_helpers import auth_headers, authenticate
from tests.test_admin_backup_restore import _build_install, _make_config_archive

pytestmark = [
    pytest.mark.admin,
    pytest.mark.backup_restore,
    pytest.mark.integration,
    pytest.mark.simulation,
]


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


@pytest.fixture()
def install(tmp_path):
    return _build_install(tmp_path)


@pytest.fixture()
def server(install):
    service = BackupRestoreService(
        context_provider=lambda: detect_install_context(base_dir=str(install))
    )
    srv = create_server("127.0.0.1", 0, backup_service=service)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    try:
        yield base
    finally:
        srv.shutdown()
        srv.server_close()


def _json_request(url, method="GET", body=None):
    data = None
    headers = dict(auth_headers(url, method))
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def _export(base, backup_id, headers=None):
    url = base + "/api/admin/maintenance/backups/export"
    request_headers = dict(auth_headers(url, "POST"))
    request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    req = urllib.request.Request(
        url,
        data=json.dumps({"id": backup_id}).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def _import(base, name, payload, headers=None):
    url = base + "/api/admin/maintenance/backups/import"
    request_headers = dict(auth_headers(url, "POST"))
    request_headers["Content-Type"] = "application/octet-stream"
    if name is not None:
        request_headers["X-Backup-Filename"] = name
    request_headers.update(headers or {})
    req = urllib.request.Request(
        url, data=payload, headers=request_headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def _backups(base):
    status, data = _json_request(base + "/api/admin/maintenance/backups")
    assert status == 200
    return data["backups"]


def _backup_dir(install):
    return os.path.join(str(install), "data", "backups")


# --- export ---------------------------------------------------------------


def test_an_archive_can_be_downloaded_byte_for_byte(server, install):
    path = _make_config_archive(install)
    entry = _backups(server)[0]
    status, body, headers = _export(server, entry["id"])
    assert status == 200
    with open(path, "rb") as handle:
        assert body == handle.read()
    assert headers["Content-Type"] == "application/gzip"
    assert os.path.basename(path) in headers["Content-Disposition"]


def test_export_needs_a_known_id_and_never_takes_a_path(server, install):
    _make_config_archive(install)
    for bogus in ("", "../../etc/passwd", "ems-config-manual-2026.tar.gz", "x" * 64):
        status, body, _ = _export(server, bogus)
        assert status == 400, bogus
        assert b"passwd" not in body


def test_export_is_refused_without_a_csrf_token(server, install):
    _make_config_archive(install)
    entry = _backups(server)[0]
    status, _, _ = _export(server, entry["id"], headers={"X-CSRF-Token": ""})
    assert status == 403


# --- import ---------------------------------------------------------------


def test_an_exported_archive_can_be_put_back(server, install):
    path = _make_config_archive(install)
    with open(path, "rb") as handle:
        payload = handle.read()
    name = os.path.basename(path)
    os.remove(path)
    assert _backups(server) == []

    status, data = _import(server, name, payload)
    assert status == 200, data
    assert data["ok"] is True

    restored = _backups(server)
    assert len(restored) == 1
    assert restored[0]["backup_type"] == "config"
    assert restored[0]["valid"] is True


def test_an_import_never_overwrites_an_existing_archive(server, install):
    path = _make_config_archive(install)
    with open(path, "rb") as handle:
        payload = handle.read()
    before = os.path.getmtime(path)

    status, data = _import(server, os.path.basename(path), payload)
    assert status == 200, data
    with open(path, "rb") as handle:
        assert handle.read() == payload
    assert os.path.getmtime(path) == before
    assert len(_backups(server)) == 2


def test_an_import_that_is_not_an_ems_archive_is_refused(server, install):
    status, data = _import(
        server, "ems-config-manual-2026-01-01-000000.tar.gz", b"not a tar at all"
    )
    assert status == 400
    assert data["ok"] is False
    assert os.listdir(_backup_dir(install)) == []


@pytest.mark.parametrize(
    "name",
    [
        None,
        "",
        "../../evil.tar.gz",
        "evil.tar.gz",
        "ems-config-manual.zip",
        "ems-config-manual-2026-01-01-000000.tar.gz.bak",
        "ems-/../config.tar.gz",
    ],
)
def test_an_import_refuses_a_name_it_did_not_expect(server, install, name):
    status, data = _import(server, name, b"anything")
    assert status == 400, name
    assert data["ok"] is False
    assert os.listdir(_backup_dir(install)) == []


def test_an_import_bigger_than_the_limit_is_refused_without_writing_it(
    server, install, monkeypatch
):
    from admin import server as server_module

    monkeypatch.setattr(server_module, "MAX_BACKUP_UPLOAD_BYTES", 64)
    status, data = _import(
        server, "ems-config-manual-2026-01-01-000000.tar.gz", b"x" * 4096
    )
    assert status == 413
    assert data["ok"] is False
    assert os.listdir(_backup_dir(install)) == []


def test_an_import_leaves_no_temporary_file_behind_when_it_fails(server, install):
    _import(server, "ems-config-manual-2026-01-01-000000.tar.gz", b"broken")
    assert os.listdir(_backup_dir(install)) == []


def test_import_is_refused_without_a_csrf_token(server, install):
    status, data = _import(
        server,
        "ems-config-manual-2026-01-01-000000.tar.gz",
        b"anything",
        headers={"X-CSRF-Token": ""},
    )
    assert status == 403
    assert os.listdir(_backup_dir(install)) == []


# --- the page offers both directions --------------------------------------


def _static(name):
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "admin",
        "static",
        name,
    )
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def test_the_backup_page_offers_both_directions():
    html = _static("index.html")
    panel = html.split('id="maintenance-backup-panel"', 1)[1]
    assert 'id="backup-import-input"' in panel
    js = _static("admin.js")
    assert '"/api/admin/maintenance/backups/export"' in js
    assert '"/api/admin/maintenance/backups/import"' in js
    assert 'data-backup-action="export"' in js


def test_the_download_keeps_the_csrf_gated_post():
    """A plain link the address bar can follow would drop the CSRF token."""

    js = _static("admin.js")
    body = js.split("async function exportBackup", 1)[1].split("\nasync function ", 1)[0]
    assert 'method: "POST"' in body
    assert "res.blob()" in body


def test_a_refused_import_does_not_corrupt_the_next_request(server, install):
    """_drain_body stops at the JSON ceiling, so it cannot drain an archive.

    Leaving a refused upload's bytes in the socket would make the next request
    on that connection read them as a request line.
    """

    import http.client
    from urllib.parse import urlparse

    parsed = urlparse(server)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    try:
        url = server + "/api/admin/maintenance/backups/import"
        headers = dict(auth_headers(url, "POST"))
        headers["Content-Type"] = "application/octet-stream"
        headers["X-Backup-Filename"] = "definitely-not-a-backup.tar.gz"
        conn.request("POST", "/api/admin/maintenance/backups/import", b"x" * 8192, headers)
        refusal = conn.getresponse()
        assert refusal.status == 400
        refusal.read()
        # The server said it is done with this connection rather than leaving
        # 8 KiB of body behind it.
        assert refusal.will_close or refusal.getheader("Connection") == "close"
    finally:
        conn.close()

    # A fresh request still gets a real answer.
    status, data = _json_request(server + "/api/admin/maintenance/backups")
    assert status == 200
    assert data["ok"] is True
