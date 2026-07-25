# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail-closed contract for EMS diagnostics -> System Build health success.

A System Build (or Guided Upgrade) may only be marked ``known_good`` when the
EMS diagnostics result is an *explicitly* valid, successful mapping. Empty,
unknown or malformed diagnostics must fail closed and never become the installed
baseline. This is the single semantic validator both the normal apply path and
the resume path share.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from admin.server import ScanRegistry, create_server
from admin.system_health import (
    SUCCESS_STATUSES,
    HealthValidationResult,
    validate_system_health_result,
)
from tests.admin_auth_helpers import auth_headers, authenticate

pytestmark = pytest.mark.simulation


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


# --- success cases -------------------------------------------------------

@pytest.mark.parametrize(
    "diagnostics",
    [
        {"available": True, "summary": {"status": "ok"}},
        {"available": True, "summary": {"status": "warning"}},
        # Extra fields never invalidate an otherwise explicit success.
        {
            "available": True,
            "mode": "container",
            "summary": {"status": "ok", "ok": 3, "failed": 0},
        },
    ],
)
def test_explicit_success_passes(diagnostics):
    result = validate_system_health_result(diagnostics)
    assert isinstance(result, HealthValidationResult)
    assert result.success is True
    assert result.passed is True
    assert result.error_code is None
    assert result.status in SUCCESS_STATUSES


def test_allowed_success_statuses_are_ok_and_warning():
    # Locked to the EMS diagnostics contract's non-failing summary buckets.
    assert set(SUCCESS_STATUSES) == {"ok", "warning"}


# --- failure / unverified cases ------------------------------------------

@pytest.mark.parametrize(
    "diagnostics, expected_code",
    [
        (None, "healthcheck_result_invalid"),
        ([], "healthcheck_result_invalid"),
        ("ok", "healthcheck_result_invalid"),
        (42, "healthcheck_result_invalid"),
        ({}, "healthcheck_result_invalid"),
        ({"available": True}, "healthcheck_result_invalid"),
        ({"available": False}, "healthcheck_unavailable"),
        ({"available": "yes", "summary": {"status": "ok"}}, "healthcheck_result_invalid"),
        ({"available": 1, "summary": {"status": "ok"}}, "healthcheck_result_invalid"),
        ({"summary": {"status": "ok"}}, "healthcheck_result_invalid"),
        ({"available": True, "summary": None}, "healthcheck_result_invalid"),
        ({"available": True, "summary": []}, "healthcheck_result_invalid"),
        ({"available": True, "summary": {}}, "healthcheck_result_invalid"),
        ({"available": True, "summary": {"status": None}}, "healthcheck_result_invalid"),
        ({"available": True, "summary": {"status": ""}}, "healthcheck_result_invalid"),
        ({"available": True, "summary": {"status": "   "}}, "healthcheck_result_invalid"),
        ({"available": True, "summary": {"status": 3}}, "healthcheck_result_invalid"),
        ({"available": True, "summary": {"status": "unknown"}}, "healthcheck_result_invalid"),
        ({"available": True, "summary": {"status": "banana"}}, "healthcheck_result_invalid"),
        ({"available": True, "summary": {"status": "failed"}}, "healthcheck_failed"),
        ({"available": True, "summary": {"status": "unavailable"}}, "healthcheck_unavailable"),
        # available failure is decided before a (missing) summary is inspected.
        ({"available": False, "summary": {"status": "ok"}}, "healthcheck_unavailable"),
    ],
)
def test_fail_closed_cases(diagnostics, expected_code):
    result = validate_system_health_result(diagnostics)
    assert result.success is False
    assert result.passed is False
    assert result.error_code == expected_code
    # A stable, human-readable message is always present and never empty.
    assert isinstance(result.message, str) and result.message


def test_malformed_types_never_raise():
    # Exhaustively hostile inputs must produce a result, never an exception.
    for hostile in (object(), b"bytes", 3.14, True, {"available": True, "summary": 7}):
        result = validate_system_health_result(hostile)
        assert result.success is False


def test_message_never_leaks_raw_values():
    # A status string is contract vocabulary, but arbitrary payload values must
    # never be echoed back into the error message.
    secret = "s3cr3t-token-should-not-appear"
    diagnostics = {
        "available": True,
        "token": secret,
        "summary": {"status": "banana", "detail": secret},
    }
    result = validate_system_health_result(diagnostics)
    assert result.success is False
    assert secret not in (result.message or "")


# --- resume path shares the same fail-closed contract --------------------


class _RawEmsCli:
    def __init__(self, payload):
        self._payload = payload

    def run(self, check_ids=None):
        return self._payload


class _ResumeSystemAlignment:
    """Minimal alignment stub paused at healthcheck_pending on resume."""

    def __init__(self):
        self.recover_calls = []

    def status(self, *, operation_active=None):
        del operation_active
        return {
            "active": True,
            "transition": {
                "operation_id": "op",
                "mode": "guided_upgrade",
                "stage": "healthcheck_pending",
            },
            "known_good": None,
        }

    def recover_ems_operation(
        self,
        *,
        operation_id,
        healthcheck_passed=None,
        healthcheck_error_code=None,
        healthcheck_error_message=None,
    ):
        self.recover_calls.append(
            {
                "passed": healthcheck_passed,
                "error_code": healthcheck_error_code,
                "error_message": healthcheck_error_message,
            }
        )
        stage = "completed" if healthcheck_passed else "failed_recoverable"
        return {"status": stage, "stage": stage, "operation_id": operation_id}


def _post(url, body):
    data = json.dumps(body).encode("utf-8")
    headers = dict(auth_headers(url, "POST"))
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def _resume_with_diagnostics(payload):
    alignment = _ResumeSystemAlignment()
    registry = ScanRegistry(scan_runner=lambda *a, **k: ([], []))
    srv = create_server(
        "127.0.0.1",
        0,
        registry=registry,
        ems_cli=_RawEmsCli(payload),
        system_alignment=alignment,
    )
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    try:
        status, body = _post(
            base + "/api/admin/system-alignment/resume", {"operation_id": "op"}
        )
    finally:
        srv.shutdown()
    return status, body, alignment


@pytest.mark.parametrize(
    "payload, expected_error_code",
    [
        ({}, "healthcheck_result_invalid"),
        ({"available": True}, "healthcheck_result_invalid"),
        ({"available": True, "summary": {}}, "healthcheck_result_invalid"),
        ({"available": True, "summary": {"status": None}}, "healthcheck_result_invalid"),
        ({"available": True, "summary": {"status": "banana"}}, "healthcheck_result_invalid"),
        ({"available": True, "summary": {"status": "failed"}}, "healthcheck_failed"),
        ({"available": True, "summary": {"status": "unavailable"}}, "healthcheck_unavailable"),
    ],
)
def test_resume_fails_closed_on_bad_diagnostics(payload, expected_error_code):
    status, body, alignment = _resume_with_diagnostics(payload)
    assert status == 200, body
    # Resume derives the same fail-closed verdict as the normal apply path, and
    # forwards the specific error code rather than the generic fallback.
    assert len(alignment.recover_calls) == 1
    call = alignment.recover_calls[0]
    assert call["passed"] is False
    assert call["error_code"] == expected_error_code
    assert body["stage"] == "failed_recoverable"


@pytest.mark.parametrize("status_value", ["ok", "warning"])
def test_resume_completes_on_explicit_success(status_value):
    payload = {"available": True, "summary": {"status": status_value}}
    status, body, alignment = _resume_with_diagnostics(payload)
    assert status == 200, body
    assert len(alignment.recover_calls) == 1
    call = alignment.recover_calls[0]
    assert call["passed"] is True
    assert call["error_code"] is None
    assert body["stage"] == "completed"
