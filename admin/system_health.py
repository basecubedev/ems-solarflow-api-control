# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail-closed semantic validation of an EMS diagnostics result.

The System Build / Guided Upgrade state machine may only mark a build
``known_good`` after EMS reports an *explicitly* valid, successful diagnosis.
An empty, unknown, malformed or wrong-typed result must fail closed — it must
never be interpreted as "healthy because nothing said failed".

This module owns only that single semantic: *does this diagnostics mapping
represent an explicit EMS success?* It invents no second diagnosis vocabulary —
the allowed statuses are the EMS diagnostics contract's non-failing summary
buckets (see ``admin/ems_cli.py`` ``_summarize``). The transition/known-good
decision lives in ``admin/system_alignment.py``; the HTTP handlers only
orchestrate and render the outcome.
"""

from collections.abc import Mapping
from dataclasses import dataclass

# The EMS diagnostics summary buckets that count as an explicit pass. Anything
# else — ``failed``, ``unavailable``, an unknown value, or a missing/blank
# status — fails closed.
SUCCESS_STATUSES = ("ok", "warning")

# Explicit non-success statuses that carry a more specific error code than the
# catch-all "invalid" bucket.
_FAILED_STATUS = "failed"
_UNAVAILABLE_STATUS = "unavailable"

ERROR_RESULT_INVALID = "healthcheck_result_invalid"
ERROR_UNAVAILABLE = "healthcheck_unavailable"
ERROR_FAILED = "healthcheck_failed"


@dataclass(frozen=True)
class HealthValidationResult:
    """Outcome of validating an EMS diagnostics result.

    ``success`` is the fail-closed verdict. ``status`` is the recognized EMS
    summary status when one was present (contract vocabulary, safe to surface).
    ``error_code``/``message`` describe a failure without echoing any raw
    payload value (no credentials, tokens or unbounded environment values).
    """

    success: bool
    status: str | None = None
    error_code: str | None = None
    message: str | None = None

    @property
    def passed(self) -> bool:
        return self.success


def _ok(status: str) -> HealthValidationResult:
    return HealthValidationResult(success=True, status=status)


def _fail(error_code: str, message: str) -> HealthValidationResult:
    return HealthValidationResult(
        success=False, error_code=error_code, message=message
    )


def validate_system_health_result(diagnostics) -> HealthValidationResult:
    """Validate an EMS diagnostics result, failing closed on anything unclear.

    Success requires, at minimum: a mapping whose ``available`` is exactly
    ``True``, whose ``summary`` is a mapping, and whose ``summary.status`` is an
    explicitly allowed success value. Every other shape — including malformed or
    wrong-typed input — returns ``success=False`` and never raises.
    """

    if not isinstance(diagnostics, Mapping):
        return _fail(
            ERROR_RESULT_INVALID, "diagnostics result is not a mapping"
        )

    if "available" not in diagnostics:
        return _fail(
            ERROR_RESULT_INVALID,
            "diagnostics result is missing the 'available' flag",
        )
    available = diagnostics.get("available")
    if available is False:
        return _fail(
            ERROR_UNAVAILABLE, "EMS diagnostics reported unavailable"
        )
    # ``bool`` is an ``int`` subtype, so require *exactly* True — a truthy 1 or
    # "yes" is not an explicit availability signal.
    if available is not True:
        return _fail(
            ERROR_RESULT_INVALID,
            "diagnostics 'available' flag is not exactly true",
        )

    if "summary" not in diagnostics or not isinstance(
        diagnostics.get("summary"), Mapping
    ):
        return _fail(
            ERROR_RESULT_INVALID,
            "diagnostics result is missing a summary mapping",
        )
    summary = diagnostics["summary"]

    status = summary.get("status")
    if not isinstance(status, str) or not status.strip():
        return _fail(
            ERROR_RESULT_INVALID,
            "diagnostics summary is missing an explicit status",
        )

    if status in SUCCESS_STATUSES:
        return _ok(status)
    if status == _FAILED_STATUS:
        return _fail(ERROR_FAILED, "EMS diagnostics reported a failed status")
    if status == _UNAVAILABLE_STATUS:
        return _fail(
            ERROR_UNAVAILABLE, "EMS diagnostics reported an unavailable status"
        )
    return _fail(
        ERROR_RESULT_INVALID, "EMS diagnostics reported an unrecognized status"
    )
