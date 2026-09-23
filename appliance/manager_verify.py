# SPDX-License-Identifier: AGPL-3.0-or-later
"""The deadline that reverts a manager install nobody confirmed.

A package install commits itself: dpkg replaces the manager, systemd restarts
it, and silence means the new one stays. This arms a repeating timer to make
silence mean the opposite, and the reverter it runs is a copy taken out of the
*outgoing* package before anything is unpacked. That copy is the script alone:
the unit that runs it and the timer come from the package being judged, which
is the limit the ADR records.

See docs/appliance/adr/manager-self-update.md for what this does not replace.
"""

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from appliance import artifact_trust

DEADLINE_NAME = "verify-deadline.json"
VERDICT_NAME = "verify-verdict.json"
ATTEMPTS_NAME = "verify-revert-attempts"
# The ticks the reverter has spent on the armed deadline: the window is
# counted in them as well as on a clock this board restores stale at boot.
TICKS_NAME = "verify-ticks"
REVERTER_NAME = "verify-manager.armed.sh"

# How the reverter asks dpkg what it has, named here so the shell script and the
# tests cannot drift apart about it. ``${Version}`` alone also answers for a
# package dpkg unpacked and never configured -- which is the state the deadline
# exists to catch.
DPKG_STATE_QUERY = "${db:Status-Status}|${Version}"
DPKG_STATE_INSTALLED = "installed"

# The reverter retries a refused revert rather than spending its one attempt on
# a dpkg frontend lock that a tick later has cleared.
REVERT_ATTEMPTS = 5

PACKAGED_REVERTER = "/usr/lib/ems-appliance-manager/verify-manager.sh"
VERIFY_TIMER = "ems-appliance-manager-verify.timer"

DEADLINE_SCHEMA_VERSION = 1
READABLE_DEADLINE_VERSIONS = (1,)

DEFAULT_WINDOW_SECONDS = 900

VERDICT_PENDING = "pending"
VERDICT_CONFIRMED = "confirmed"
VERDICT_REVERTED = "reverted"
VERDICT_REVERT_FAILED = "revert_failed"
VERDICT_UNAVAILABLE = "revert_unavailable"

SETTLED_VERDICTS = frozenset(
    {VERDICT_CONFIRMED, VERDICT_REVERTED, VERDICT_REVERT_FAILED, VERDICT_UNAVAILABLE}
)

FILE_MODE = 0o600
REVERTER_MODE = 0o700


class ManagerVerifyError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class VerifyDeadline:
    """What an install promised, and by when it has to have proved it."""

    armed: bool = False
    expected_version: str = ""
    build_id: str = ""
    previous_path: str = ""
    previous_sha256: str = ""
    operation_id: str = ""
    armed_at: int = 0
    deadline_epoch: int = 0
    window_seconds: int = 0
    unreadable: str = ""

    @property
    def revert_available(self):
        return bool(self.previous_path)

    def expired(self, now):
        return self.armed and int(now) >= self.deadline_epoch

    def to_dict(self):
        return {
            "armed": self.armed,
            "expected_version": self.expected_version,
            "build_id": self.build_id,
            "previous_path": self.previous_path,
            "operation_id": self.operation_id,
            "armed_at": self.armed_at,
            "deadline_epoch": self.deadline_epoch,
            "window_seconds": self.window_seconds,
            "revert_available": self.revert_available,
            "unreadable": self.unreadable,
        }


@dataclass(frozen=True)
class VerifyVerdict:
    """What the reverter decided, or that it has not decided yet."""

    verdict: str = VERDICT_PENDING
    detail: str = ""
    decided_at: str = ""

    @property
    def settled(self):
        return self.verdict in SETTLED_VERDICTS

    def to_dict(self):
        return {
            "verdict": self.verdict,
            "detail": self.detail,
            "decided_at": self.decided_at,
            "settled": self.settled,
        }


def deadline_path(paths):
    return Path(paths.packages_dir) / DEADLINE_NAME


def verdict_path(paths):
    return Path(paths.packages_dir) / VERDICT_NAME


def reverter_path(paths):
    return Path(paths.packages_dir) / REVERTER_NAME


def _write(target, payload, *, mode=FILE_MODE):
    handle, staging = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(staging, mode)
        os.replace(staging, target)
    except OSError as exc:
        try:
            os.unlink(staging)
        except OSError:
            pass
        raise ManagerVerifyError("deadline_not_writable", f"{target}: {exc}")


def read(paths):
    """The armed deadline, or that there is none."""

    try:
        payload = json.loads(deadline_path(paths).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return VerifyDeadline()
    except (OSError, ValueError) as exc:
        return VerifyDeadline(unreadable=str(exc)[:200])
    if not isinstance(payload, dict):
        return VerifyDeadline(unreadable="the deadline record is not an object")
    version = payload.get("schema_version")
    if version not in READABLE_DEADLINE_VERSIONS:
        return VerifyDeadline(
            unreadable=f"deadline record version {version!r} cannot be read by this manager"
        )
    return VerifyDeadline(
        armed=True,
        expected_version=str(payload.get("expected_version") or ""),
        build_id=str(payload.get("build_id") or ""),
        previous_path=str(payload.get("previous_path") or ""),
        previous_sha256=str(payload.get("previous_sha256") or ""),
        operation_id=str(payload.get("operation_id") or ""),
        armed_at=int(payload.get("armed_at") or 0),
        deadline_epoch=int(payload.get("deadline_epoch") or 0),
        window_seconds=int(payload.get("window_seconds") or 0),
    )


def read_verdict(paths):
    try:
        payload = json.loads(verdict_path(paths).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return VerifyVerdict()
    except (OSError, ValueError):
        return VerifyVerdict(detail="the verdict could not be read")
    if not isinstance(payload, dict):
        return VerifyVerdict(detail="the verdict is not an object")
    return VerifyVerdict(
        verdict=str(payload.get("verdict") or VERDICT_PENDING),
        detail=str(payload.get("detail") or ""),
        decided_at=str(payload.get("decided_at") or ""),
    )


def _snapshot_reverter(paths, reverter):
    source = Path(reverter)
    if not source.is_file():
        raise ManagerVerifyError(
            "reverter_missing",
            f"{source} is not on this appliance, so nothing could undo the install",
        )
    target = reverter_path(paths)
    handle, staging = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.")
    os.close(handle)
    try:
        shutil.copyfile(source, staging)
        os.chmod(staging, REVERTER_MODE)
        os.replace(staging, target)
    except OSError as exc:
        try:
            os.unlink(staging)
        except OSError:
            pass
        raise ManagerVerifyError("reverter_not_writable", f"{target}: {exc}")
    return target


def _digest_of(previous):
    """What is in the kept archive now, or nothing if it cannot be read."""

    if not previous:
        return ""
    try:
        return artifact_trust.file_digest(previous)
    except OSError:
        return ""


def arm(
    paths,
    runner,
    *,
    expected_version,
    build_id,
    previous,
    now,
    operation_id="",
    window_seconds=DEFAULT_WINDOW_SECONDS,
    reverter=PACKAGED_REVERTER,
):
    """Take the outgoing package's reverter, write the deadline, start the timer.

    An empty ``previous`` is a first install: it arms, reports that there is
    nothing to go back to, and leaves the appliance to a person if it expires.
    """

    directory = Path(paths.packages_dir)
    directory.mkdir(parents=True, exist_ok=True)
    snapshot = _snapshot_reverter(paths, reverter)

    # The archive, not the slot it sits in. ``previous.deb`` is a name that
    # ``manager_retention.retain`` rewrites on every install, so a deadline that
    # kept only the path can be made to reinstall a package this appliance has
    # never run -- including the one it was armed to undo.
    previous_sha256 = _digest_of(previous)

    deadline = int(now) + int(window_seconds)
    _write(
        deadline_path(paths),
        {
            "schema_version": DEADLINE_SCHEMA_VERSION,
            "expected_version": expected_version,
            "build_id": build_id,
            "previous_path": str(previous or ""),
            "previous_sha256": previous_sha256,
            "operation_id": operation_id,
            "armed_at": int(now),
            "deadline_epoch": deadline,
            "window_seconds": int(window_seconds),
        },
    )
    try:
        verdict_path(paths).unlink()
    except FileNotFoundError:
        pass
    # A tick count a previous deadline left behind would shorten this one.
    try:
        ticks_path(paths).unlink()
    except FileNotFoundError:
        pass

    # The deadline is written before the timer is started on purpose: a timer
    # that fired first would find nothing to judge. But a deadline with no timer
    # judges nothing and blocks everything -- the console gates both Update and
    # Revert on `armed` -- so an arm that fails must leave none behind.
    def abandon(code, message):
        try:
            deadline_path(paths).unlink()
        except OSError:
            pass
        raise ManagerVerifyError(code, message)

    if runner is None or not runner.available("systemctl"):
        abandon(
            "systemctl_unavailable", "systemctl is not available, so no deadline could be armed"
        )
    result = runner.run("systemctl", ["enable", "--now", VERIFY_TIMER], timeout=60)
    if not result.ok:
        abandon(
            "verify_timer_failed",
            (result.stderr or result.stdout or f"{VERIFY_TIMER} could not be started").strip(),
        )
    return VerifyDeadline(
        armed=True,
        expected_version=expected_version,
        build_id=build_id,
        previous_path=str(previous or ""),
        previous_sha256=previous_sha256,
        operation_id=operation_id,
        armed_at=int(now),
        deadline_epoch=deadline,
        window_seconds=int(window_seconds),
    ), snapshot


def ticks_path(paths):
    return Path(paths.packages_dir) / TICKS_NAME


def attempts_path(paths):
    """Where the reverter counts its refused reverts. Written only by it."""

    return Path(paths.packages_dir) / ATTEMPTS_NAME


def disarm(paths, runner):
    """Retire a deadline the appliance no longer needs judged."""

    try:
        deadline_path(paths).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ManagerVerifyError("deadline_not_writable", f"{deadline_path(paths)}: {exc}")
    try:
        ticks_path(paths).unlink()
    except OSError:
        pass
    if runner is not None and runner.available("systemctl"):
        runner.run("systemctl", ["disable", "--now", VERIFY_TIMER], timeout=60)
    return True


__all__ = [
    "DPKG_STATE_INSTALLED",
    "DPKG_STATE_QUERY",
    "ManagerVerifyError",
    "REVERT_ATTEMPTS",
    "TICKS_NAME",
    "VerifyDeadline",
    "VerifyVerdict",
    "arm",
    "attempts_path",
    "deadline_path",
    "disarm",
    "read",
    "read_verdict",
    "reverter_path",
    "ticks_path",
    "verdict_path",
]
