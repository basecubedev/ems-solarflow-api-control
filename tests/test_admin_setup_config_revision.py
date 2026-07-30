# SPDX-License-Identifier: AGPL-3.0-or-later
"""The legacy browser-held ``config_revision`` is no longer mutation authority.

Archive 98 bound Setup mutations to the live config revision the browser sent
back. That proved only the live file state — not which draft was reviewed — so
the exact-preview contract replaced it: mutations present ``setup_workflow_id``
and ``config_preview_id`` (see ``tests/test_admin_setup_preview_authority.py``).
These tests pin the migration edge: a request carrying only the old revision
proof is refused with the stable workflow/preview codes, and the live config —
including a newer Maintenance edit — survives byte-exact.

See ``docs/technical/admin-workflow-state.md`` §2.2.
"""

import hashlib
from pathlib import Path

import pytest

from admin.install_context import detect_install_context
from tests.helpers.setup_config import start_setup_workflow
from tests.test_admin_server import (
    _control_export_body,
    _control_export_manager,
    _request,
    _serve,
)

pytestmark = pytest.mark.simulation


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _live_path():
    return Path(detect_install_context().config_path)


def _write_live(payload):
    path = _live_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return path


def _live_revision():
    return hashlib.sha256(_live_path().read_bytes()).hexdigest()


def _legacy_body(revision):
    return {**_control_export_body(), "config_revision": revision}


@pytest.mark.parametrize(
    "path", ["/api/setup/config/apply", "/api/setup/config/write"]
)
def test_a_raw_config_revision_alone_cannot_mutate(tmp_path, path):
    """The pre-workflow request shape is refused before anything changes."""

    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _write_live('{"live": "A"}\n')
    try:
        revision = {"expected_revision": _live_revision(), "expect_absent": False}
        status, _, payload = _request(
            f"{base}{path}", method="POST", body=_legacy_body(revision)
        )

        assert status == 409, payload
        assert payload["error"] == "setup_workflow_required"
        assert _live_path().read_text(encoding="utf-8") == '{"live": "A"}\n'
        assert not list(tmp_path.rglob("generated/config.json"))
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_matching_revision_with_a_workflow_still_needs_the_exact_preview(
    tmp_path,
):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _write_live('{"live": "A"}\n')
    try:
        workflow_id = start_setup_workflow(base, _request)
        revision = {"expected_revision": _live_revision(), "expect_absent": False}
        status, _, payload = _request(
            f"{base}/api/setup/config/apply",
            method="POST",
            body={**_legacy_body(revision), "setup_workflow_id": workflow_id},
        )

        assert status == 409, payload
        assert payload["error"] == "setup_preview_required"
        assert _live_path().read_text(encoding="utf-8") == '{"live": "A"}\n'
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_bytes_survive_a_stale_legacy_apply(tmp_path):
    """The original defect stays closed for pre-workflow request shapes.

    Revision A is captured before the Maintenance edit, exactly as an old
    browser holding an open draft would have it. Applying that draft afterwards
    must not replace revision B — regardless of which refusal code fires first.
    """

    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _write_live('{"live": "A"}\n')
    try:
        stale = {"expected_revision": _live_revision(), "expect_absent": False}
        maintenance = '{"live": "B — edited by Maintenance"}\n'
        _write_live(maintenance)

        status, _, payload = _request(
            f"{base}/api/setup/config/apply",
            method="POST",
            body=_legacy_body(stale),
        )

        assert _live_path().read_text(encoding="utf-8") == maintenance, (
            "a stale Setup draft overwrote a newer Maintenance edit"
        )
        assert status == 409, payload
    finally:
        srv.shutdown()
        srv.server_close()
