# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operating-system package status, installation and recovery.

Package names never come from the browser: an installation upgrades exactly the
packages the appliance itself parsed out of a simulated apt run, and never
removes one. A real active package-manager lock is reported, never removed, and
no distribution upgrade is offered.
"""

import contextlib
import fcntl
import os
import re
import struct
import time
from dataclasses import dataclass, field

from appliance.hostprobe import (
    APT_DAILY_UPDATE_STAMP,
    APT_LISTS_DIR,
    DPKG_DATABASE_LOCK_FILE,
    DPKG_LOCK_FILE,
)
from appliance.operations import (
    BUSY_CODES,
    STATE_FAILED_TERMINAL,
    STATE_SUCCEEDED,
    STATE_VERIFYING,
)
from appliance.redaction import bounded_redacted_log
from appliance.validation import (
    PACKAGE_REPAIR_CONFIGURE,
    PACKAGE_REPAIR_FIX_BROKEN,
    PACKAGE_REPAIR_REFRESH_INDEX,
    UPDATE_SCOPE_ALL,
    UPDATE_SCOPE_SECURITY,
)

INDEX_MAX_AGE_SECONDS = 20 * 60 * 60
CLOCK_AHEAD_TOLERANCE_SECONDS = 60 * 60
INDEX_REFRESH_TIMEOUT = 900
APT_SIMULATE_TIMEOUT = 120
DPKG_QUERY_TIMEOUT = 60

TYPE_UPDATE_INSTALL = "updates.install"
TYPE_UPDATE_REPAIR = "updates.repair"

PACKAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+._-]*$")
INST_LINE = re.compile(r"^Inst\s+(\S+)\s+(?:\[(\S+)\]\s+)?\((\S+)\s+([^)]*)\)")
REMV_LINE = re.compile(r"^Remv\s+(\S+)")

KERNEL_PREFIXES = ("linux-image", "linux-headers", "raspberrypi-kernel")
FIRMWARE_PREFIXES = ("raspi-firmware", "raspberrypi-bootloader", "firmware-", "rpi-eeprom")

LOCK_FREE = "free"
LOCK_HELD = "held"

# The two ways a check can end without an answer. The first says apt could
# not work out what is available; `apt-get -s` asks no mirror, so the cause is
# local -- a dependency it cannot meet, a malformed sources entry, damaged
# lists, the timeout. The second says there is no apt to ask at all.
UPDATE_CHECK_FAILED = "update_check_failed"
PACKAGE_MANAGER_UNAVAILABLE = "package_manager_unavailable"
LOCK_UNKNOWN = "unknown"

# struct flock as Python is built (64-bit offsets): l_type, l_whence, l_start,
# l_len, l_pid. The query is padded because the kernel's struct is longer than
# the packed fields on every architecture this runs on.
_FLOCK = struct.Struct("@hhqqi")
_FLOCK_QUERY = _FLOCK.pack(fcntl.F_WRLCK, os.SEEK_SET, 0, 0, 0) + bytes(32)


def _record_lock_state(path):
    """Whether another process holds a POSIX record lock on ``path``."""

    try:
        handle = os.open(str(path), os.O_RDONLY)
    except OSError:
        return LOCK_UNKNOWN
    try:
        answer = fcntl.fcntl(handle, fcntl.F_GETLK, _FLOCK_QUERY)
    except OSError:
        return LOCK_UNKNOWN
    finally:
        os.close(handle)
    return LOCK_FREE if _FLOCK.unpack_from(answer)[0] == fcntl.F_UNLCK else LOCK_HELD


class PackageError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PackageUpdate:
    name: str
    current_version: str
    new_version: str
    origin: str
    security: bool

    def to_dict(self):
        return {
            "name": self.name,
            "current_version": self.current_version,
            "new_version": self.new_version,
            "origin": self.origin,
            "security": self.security,
        }


@dataclass
class PackageState:
    updates: list = field(default_factory=list)
    held: list = field(default_factory=list)
    dpkg_issues: list = field(default_factory=list)
    lock_state: str = LOCK_UNKNOWN
    reboot_required: bool = False
    reboot_packages: list = field(default_factory=list)
    free_megabytes: int = 0
    # Whether the probe could answer at all. Without it, "0 MB free" and "the
    # probe did not run" were the same value, and the blocker treated both as
    # nothing to worry about.
    free_space_known: bool = True
    index_age_seconds: int = None
    error: str = ""
    # apt's own reason when the check failed, its first E: line.
    error_detail: str = ""

    @property
    def security_updates(self):
        return [item for item in self.updates if item.security]

    @property
    def normal_updates(self):
        return [item for item in self.updates if not item.security]

    @property
    def kernel_update(self):
        return any(item.name.startswith(KERNEL_PREFIXES) for item in self.updates)

    @property
    def firmware_update(self):
        return any(item.name.startswith(FIRMWARE_PREFIXES) for item in self.updates)

    @property
    def healthy(self):
        """Whether dpkg and apt would take an install.

        A report, not a gate: the blockers decide installs on their own. A
        failed update check is reported as itself, not held against the
        package manager -- except when apt names unmet dependencies, the one
        such failure the console's own Repair dependencies cures. Every other
        error, including one this build does not know, stays unhealthy.
        """

        unmet = self.error == UPDATE_CHECK_FAILED and "unmet dependencies" in self.error_detail.lower()
        return (
            not self.dpkg_issues
            and self.lock_state != LOCK_HELD
            and self.error in ("", UPDATE_CHECK_FAILED)
            and not unmet
        )

    def to_dict(self):
        return {
            "security_updates": [item.to_dict() for item in self.security_updates],
            "normal_updates": [item.to_dict() for item in self.normal_updates],
            "security_count": len(self.security_updates),
            "normal_count": len(self.normal_updates),
            "held": list(self.held),
            "kernel_update": self.kernel_update,
            "firmware_update": self.firmware_update,
            "reboot_required": self.reboot_required,
            "reboot_packages": list(self.reboot_packages),
            "package_manager": {
                "healthy": self.healthy,
                "dpkg_issues": list(self.dpkg_issues),
                "lock_state": self.lock_state,
            },
            "free_megabytes": self.free_megabytes,
            "free_space_known": self.free_space_known,
            "index_age_seconds": self.index_age_seconds,
            "error": self.error,
            "error_detail": self.error_detail,
        }


def parse_simulated_upgrade(text):
    """Parse ``apt-get -s upgrade`` ``Inst`` lines into typed updates."""

    updates = []
    for line in (text or "").splitlines():
        match = INST_LINE.match(line.strip())
        if not match:
            continue
        name, current, new_version, origin = match.groups()
        if not PACKAGE_NAME_RE.match(name):
            continue
        origin = (origin or "").strip()
        security = "security" in origin.lower()
        updates.append(
            PackageUpdate(
                name=name,
                current_version=current or "",
                new_version=new_version,
                origin=origin,
                security=security,
            )
        )
    return updates


def parse_held_packages(text):
    held = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].strip() == "hold" and PACKAGE_NAME_RE.match(parts[0]):
            held.append(parts[0])
    return sorted(set(held))


def parse_dpkg_audit(text):
    """The packages `dpkg --audit` names, one indented line each.

    Each group opens with a sentence over several unindented lines; only the
    first of them was skipped, and two broken packages read as twelve issues.
    """

    issues = []
    prose = False
    for line in (text or "").splitlines():
        if not line.strip():
            prose = False
        elif line[:1].isspace():
            prose = False
            issues.append(line.strip())
        elif prose or line.startswith("The following"):
            prose = True
        else:
            issues.append(line.strip())
    return issues


def apt_failure_line(result):
    """apt's own reason for a failed run: its first ``E:`` line.

    apt ends stdout with a progress line ("Reading package lists...") and puts
    every error before it, so the last line of the output is never the reason.
    Without an ``E:`` line (a timeout, a missing binary) stderr's last line is.
    """

    def lines(text):
        redacted = bounded_redacted_log(text or "")["text"]
        return [line.strip() for line in redacted.splitlines() if line.strip()]

    stderr = lines(result.stderr)
    errors = [line for line in stderr if line.startswith("E:")]
    if errors:
        return errors[0]
    rest = stderr or lines(result.stdout)
    return rest[-1] if rest else "no output"


class PackageService:
    def __init__(self, *, runner, probe, paths, config, operations, time_fn=None, operation_log=None):
        self.runner = runner
        self.probe = probe
        self.paths = paths
        self.config = config
        self.operations = operations
        self._time = time_fn or time.time
        self._operation_log = operation_log

    # --- read-only -------------------------------------------------------

    def lock_state(self):
        """Report whether apt or dpkg holds a package lock.

        apt and dpkg lock with POSIX record locks, which a flock() probe never
        sees, so the probe asks with F_GETLK whether a write lock could be
        placed and places none: a probe that coincides with apt starting can
        never make apt fail. dpkg's own lock is asked as well, because a dpkg
        run without a frontend takes only that one. An active lock is never
        removed: a half-finished dpkg run must be allowed to complete.
        """

        frontend = _record_lock_state(self.probe.root / DPKG_LOCK_FILE)
        database = _record_lock_state(self.probe.root / DPKG_DATABASE_LOCK_FILE)
        if LOCK_HELD in (frontend, database):
            return LOCK_HELD
        return frontend

    def check(self):
        """Read update state without modifying any package or index."""

        state = PackageState()
        if not self.runner.available("apt-get"):
            state.error = PACKAGE_MANAGER_UNAVAILABLE
            return state

        # --with-new-pkgs: a Raspberry Pi kernel is a versioned package, so every
        # kernel bump needs a new one, and plain `upgrade` keeps it back -- the
        # kernel fix was never shown and never installed. upgrade still never
        # removes a package.
        simulated = self.runner.run(
            "apt-get",
            ["-s", "-o", "Debug::NoLocking=1", "--with-new-pkgs", "upgrade"],
            timeout=APT_SIMULATE_TIMEOUT,
        )
        if simulated.ok:
            state.updates = parse_simulated_upgrade(simulated.stdout)
        else:
            state.error = UPDATE_CHECK_FAILED
            state.error_detail = apt_failure_line(simulated)

        selections = self.runner.run("dpkg", ["--get-selections"], timeout=DPKG_QUERY_TIMEOUT)
        state.held = parse_held_packages(selections.stdout if selections.ok else "")

        self._read_gates(state)

        reboot = self.probe.reboot_required()
        state.reboot_required = reboot["required"]
        state.reboot_packages = reboot["packages"]

        state.index_age_seconds = self._index_age()
        return state

    def _read_gates(self, state):
        """Fill in what ``_blockers`` decides on: dpkg's audit, the lock, /var."""

        audit = self.runner.run("dpkg", ["--audit"], timeout=DPKG_QUERY_TIMEOUT)
        state.dpkg_issues = parse_dpkg_audit(audit.stdout if audit.ok else audit.stderr)

        state.lock_state = self.lock_state()

        filesystem = self.probe.filesystem("/var")
        state.free_space_known = bool(filesystem.get("available"))
        state.free_megabytes = int(filesystem.get("free_mb", 0)) if state.free_space_known else 0
        return state

    def _index_age(self):
        """Seconds since the last refresh that reached every source, or None.

        apt-daily's stamp, which the console's refresh writes too, decides; the
        lists only stand in where there is no stamp or it lies more than an
        hour ahead of the clock.
        """

        for path in (self._update_stamp(), self._lists_dir()):
            try:
                age = int(self._time() - path.stat().st_mtime)
            except OSError:
                continue
            if age >= -CLOCK_AHEAD_TOLERANCE_SECONDS:
                return max(age, 0)
        return None

    def _lists_dir(self):
        return self.probe.root / APT_LISTS_DIR

    def _update_stamp(self):
        return self.probe.root / APT_DAILY_UPDATE_STAMP

    def _refresh_index(self):
        """Run ``apt-get update`` and stamp the index as refreshed now.

        apt leaves unchanged lists alone, so the refresh is marked in
        apt-daily's stamp, and on the lists for a Manager installed back to a
        version that reads only them. Without ``--error-on=any`` an unreachable
        mirror is only a warning; that run must not reset the age.
        """

        result = self.runner.run(
            "apt-get", ["update", "--error-on=any"], timeout=INDEX_REFRESH_TIMEOUT
        )
        if result.ok:
            now = self._time()
            stamp = self._update_stamp()
            try:
                stamp.parent.mkdir(parents=True, exist_ok=True)
                stamp.touch()
                os.chmod(stamp, 0o644)
                os.utime(stamp, (now, now))
            except OSError:
                with contextlib.suppress(OSError):
                    stamp.unlink()
            try:
                os.utime(self._lists_dir(), (now, now))
            except OSError:
                pass
        return result

    def _refresh_stale_index(self, operation):
        """Refresh an index a day old before a plan is made from it.

        A plan from an old index lists what was published then: the daily
        timer found nothing to install for as long as nobody refreshed it, and
        an install names versions the mirror may no longer carry. A plan the
        blockers refuse anyway is not refreshed for: a read-only root, a held
        lock or a full /var stop apt here exactly as they stop the install.
        """

        age = self._index_age()
        if age is not None and age < INDEX_MAX_AGE_SECONDS:
            return
        if self._blockers(self._read_gates(PackageState())):
            return
        self._advance(operation, "refreshing_index")
        result = self._refresh_index()
        if not result.ok:
            raise PackageError(
                "index_refresh_failed",
                "apt could not refresh the package index: " + apt_failure_line(result),
            )

    # --- planning --------------------------------------------------------

    def plan_install(self, operation, scope):
        self._refresh_stale_index(operation)
        state = self.check()
        # Without an answer there is no list to be empty: planning that as
        # "nothing to install" hid the failure, every day, behind the timer.
        if state.error:
            raise PackageError(
                state.error,
                "apt could not work out what there is to install"
                + (f": {state.error_detail}" if state.error_detail else ""),
            )
        targets = state.security_updates if scope == UPDATE_SCOPE_SECURITY else state.updates
        blockers = self._blockers(state)

        if not targets and not blockers:
            raise PackageError("no_updates_available", "there is nothing to install")

        values = {
            "scope": scope,
            "packages": [item.name for item in targets],
            "versions": {item.name: item.new_version for item in targets},
        }
        operation.requested_target.update(values)
        self.operations.update_target(operation.operation_id, values)

        return {
            "type": TYPE_UPDATE_INSTALL,
            "scope": scope,
            "packages": [item.to_dict() for item in targets],
            "package_count": len(targets),
            "blockers": blockers,
            "free_megabytes": state.free_megabytes,
            "free_space_known": state.free_space_known,
            "minimum_free_megabytes": self.config.minimum_free_megabytes,
            "reboot_required_before": state.reboot_required,
            "package_manager": state.to_dict()["package_manager"],
        }

    def plan_repair(self, operation, action):
        state = self._read_gates(PackageState())
        values = {"action": action}
        operation.requested_target.update(values)
        self.operations.update_target(operation.operation_id, values)
        blockers = [
            blocker for blocker in self._blockers(state) if blocker["code"] == "package_lock_held"
        ]
        removals = self._repair_removals() if action == PACKAGE_REPAIR_FIX_BROKEN else []
        if removals:
            blockers.append(
                {
                    "code": "repair_would_remove_packages",
                    "message": "apt can only repair the dependencies by removing "
                    + ", ".join(removals)
                    + "; removing packages is refused here, and one of them may be this "
                    "console. Repair them at the console, deciding what to remove.",
                }
            )
        return {
            "type": TYPE_UPDATE_REPAIR,
            "action": action,
            "dpkg_issues": state.dpkg_issues,
            "lock_state": state.lock_state,
            "removals": removals,
            "blockers": blockers,
        }

    def _repair_removals(self):
        """What `apt-get -f install` would remove, asked without letting it.

        A Manager package left unpacked with a dependency it cannot meet is one
        apt resolves by removing it -- and its prerm stops the agent running apt.
        """

        simulated = self.runner.run(
            "apt-get", ["-s", "-o", "Debug::NoLocking=1", "-f", "install"], timeout=APT_SIMULATE_TIMEOUT
        )
        names = []
        for line in (simulated.stdout or "").splitlines():
            match = REMV_LINE.match(line.strip())
            if match and PACKAGE_NAME_RE.match(match.group(1)):
                names.append(match.group(1))
        return sorted(set(names))

    def _root_is_read_only(self):
        try:
            return bool(os.statvfs("/").f_flag & os.ST_RDONLY)
        except OSError:
            return False

    def _blockers(self, state):
        blockers = []
        if self._root_is_read_only():
            # Keyed on the real mount state rather than on anything this
            # code believes about the host. The appliance image mounts its root
            # writable, so this fires only when something made it read-only --
            # an ext4 error remount is the usual one -- and apt would then fail
            # partway through a transaction. The UI hides the path; the refusal
            # has to exist on this side of the socket too.
            blockers.append(
                {
                    "code": "read_only_root",
                    "message": (
                        "the root filesystem is mounted read-only, so apt cannot write to it; "
                        "check the kernel log for a filesystem error that forced the remount"
                    ),
                }
            )
        if state.lock_state == LOCK_HELD:
            blockers.append(
                {
                    "code": "package_lock_held",
                    "message": "another package manager is running; wait until it finishes",
                }
            )
        if state.dpkg_issues:
            blockers.append(
                {
                    "code": "dpkg_incomplete",
                    "message": "a previous package operation was interrupted; run the repair first",
                }
            )
        if not state.free_space_known:
            # Not knowing how much room there is is not proof that there is
            # enough. apt dying inside a dpkg transaction leaves a package
            # manager whose documented repair on this appliance is re-flashing.
            blockers.append(
                {
                    "code": "free_space_unknown",
                    "message": (
                        "the free space on /var could not be measured, so there is no "
                        "proof this update has room to finish"
                    ),
                }
            )
        elif state.free_megabytes < self.config.minimum_free_megabytes:
            blockers.append(
                {
                    "code": "insufficient_disk_space",
                    "message": f"only {state.free_megabytes} MB free, "
                    f"{self.config.minimum_free_megabytes} MB required",
                }
            )
        return blockers

    # --- execution -------------------------------------------------------

    def execute(self, operation):
        if operation.type == TYPE_UPDATE_INSTALL:
            return self._execute_install(operation)
        if operation.type == TYPE_UPDATE_REPAIR:
            return self._execute_repair(operation)
        raise PackageError("unknown_operation_type", f"{operation.type} is not executable")

    def _execute_install(self, operation):
        scope = operation.requested_target.get("scope")
        packages = [str(name) for name in operation.requested_target.get("packages", [])]

        self._advance(operation, "preflight")
        state = self.check()
        blockers = self._blockers(state)
        if not all(PACKAGE_NAME_RE.match(name) for name in packages):
            blockers.append(
                {"code": "plan_invalid", "message": "the plan names a package that is not a package name; plan again"}
            )
        # apt-daily refreshes the index twice a day: what it offers at Confirm
        # is not what the plan showed, and "all" installs whatever it offers.
        planned = operation.requested_target.get("versions")
        if isinstance(planned, dict):
            offered = state.security_updates if scope == UPDATE_SCOPE_SECURITY else state.updates
            if {item.name: item.new_version for item in offered} != planned:
                blockers.append(
                    {
                        "code": "updates_changed_since_plan",
                        "message": "the package index changed since this plan was made; "
                        "plan again to see what would now be installed",
                    }
                )
        if blockers:
            self.operations.finish(
                operation.operation_id,
                STATE_FAILED_TERMINAL,
                stage="blocked",
                error={"code": blockers[0]["code"], "message": blockers[0]["message"]},
                result={"blockers": blockers},
            )
            raise PackageError(blockers[0]["code"], blockers[0]["message"])

        self._advance(operation, "installing")
        if scope == UPDATE_SCOPE_ALL:
            args = [
                "-y",
                "--no-remove",
                "--with-new-pkgs",
                "-o",
                "Dpkg::Options::=--force-confold",
                "upgrade",
            ]
        else:
            if not packages:
                raise PackageError("no_updates_available", "no security updates are pending")
            args = [
                "-y",
                "--no-remove",
                "-o",
                "Dpkg::Options::=--force-confold",
                "install",
                "--only-upgrade",
                *packages,
            ]
        result = self.runner.run("apt-get", args, timeout=3600)
        output = bounded_redacted_log((result.stdout or "") + (result.stderr or ""))

        self._advance(operation, "verifying", state=STATE_VERIFYING)
        after = self.check()
        payload = {
            "scope": scope,
            "requested_packages": packages,
            "changed_package_count": max(len(state.updates) - len(after.updates), 0),
            "remaining_updates": len(after.updates),
            "reboot_required": after.reboot_required,
            "package_manager": after.to_dict()["package_manager"],
            "output": output,
        }

        if not result.ok:
            self.operations.finish(
                operation.operation_id,
                STATE_FAILED_TERMINAL,
                stage="install_failed",
                error={"code": "package_install_failed", "message": "apt-get reported an error"},
                result=payload,
            )
            raise PackageError("package_install_failed", "the package installation failed")

        self.operations.finish(operation.operation_id, STATE_SUCCEEDED, result=payload)
        return payload

    def _execute_repair(self, operation):
        action = operation.requested_target.get("action")
        self._advance(operation, "preflight")
        if self.lock_state() == LOCK_HELD:
            self.operations.finish(
                operation.operation_id,
                STATE_FAILED_TERMINAL,
                stage="blocked",
                error={
                    "code": "package_lock_held",
                    "message": "another package manager is running; the lock is never removed",
                },
            )
            raise PackageError("package_lock_held", "another package manager holds the lock")

        self._advance(operation, f"repair_{action}")
        # --force-confold: the agent's stdin is /dev/null, so a conffile prompt
        # ended the repair with "EOF on stdin"; an operator's edited conffile
        # is kept, as on every install. --no-remove: apt fails rather than
        # removing a package, the plan having shown that none would be.
        if action == PACKAGE_REPAIR_CONFIGURE:
            result = self.runner.run(
                "dpkg", ["--force-confold", "--configure", "-a"], timeout=1800
            )
        elif action == PACKAGE_REPAIR_FIX_BROKEN:
            result = self.runner.run(
                "apt-get",
                ["-y", "--no-remove", "-o", "Dpkg::Options::=--force-confold", "-f", "install"],
                timeout=1800,
            )
        elif action == PACKAGE_REPAIR_REFRESH_INDEX:
            result = self._refresh_index()
        else:
            raise PackageError("invalid_repair_action", f"{action} is not a repair action")

        output = bounded_redacted_log((result.stdout or "") + (result.stderr or ""))
        self._advance(operation, "verifying", state=STATE_VERIFYING)
        after = self.check()
        payload = {
            "action": action,
            "package_manager": after.to_dict()["package_manager"],
            "output": output,
        }
        if not result.ok:
            self.operations.finish(
                operation.operation_id,
                STATE_FAILED_TERMINAL,
                stage="repair_failed",
                error={"code": "package_repair_failed", "message": f"{action} failed"},
                result=payload,
            )
            raise PackageError("package_repair_failed", f"{action} failed")

        self.operations.finish(operation.operation_id, STATE_SUCCEEDED, result=payload)
        return payload

    def _advance(self, operation, stage, *, state=None, detail=None):
        self.operations.advance(operation.operation_id, stage, state=state, detail=detail)
        if self._operation_log is not None:
            self._operation_log.record(
                operation.operation_id, stage, operation_type=operation.type, detail=detail
            )


# --- the schedule ------------------------------------------------------------

SCHEDULE_ACTOR = "schedule"


# The agent's own limit on one apt-get install, with room for the check after it.
SCHEDULED_INSTALL_WAIT_SECONDS = 3600 + 300


def run_scheduled_security_updates(client, config, *, sleep=None):
    """Install the waiting security updates, if the operator asked for that.

    Driven through the agent, exactly as the browser drives it: the same plan,
    the same blockers, the same operation lock, the same audit entry. A second
    path that ran ``apt`` on its own -- `unattended-upgrades`, a cron line --
    would be a writer this appliance's gates do not cover, and those gates are
    the difference between a patched OS and one whose dpkg transaction died
    half way, for which the documented recovery is re-flashing and restoring a
    backup.

    Never a full upgrade, and never a reboot: an operator decides both. A
    reboot the updates ask for is reported and left to them, because the
    battery this appliance controls does not stop needing control while it
    restarts.

    Returns what happened, so the caller can say it rather than guess:
    ``ran`` plus a ``reason`` for every case where it did not.
    """

    if not config.automatic_security_updates:
        return {"ran": False, "reason": "not_enabled"}

    from appliance.agent_client import AgentCallError, AgentUnavailableError, wait_until_settled

    try:
        planned = client.call("updates.plan", actor=SCHEDULE_ACTOR, scope=UPDATE_SCOPE_SECURITY)
    except AgentUnavailableError as exc:
        return {"ran": False, "reason": "agent_unavailable", "detail": str(exc)}
    except AgentCallError as exc:
        # An operator at the console owns the appliance; a timer waits for the
        # next one rather than competing for the lock.
        if exc.code in BUSY_CODES:
            return {"ran": False, "reason": "busy", "detail": exc.message}
        # A planner that refuses because there is nothing waiting is the
        # ordinary daily answer, not a failure to report as one.
        if exc.code == "no_updates_available":
            return {"ran": False, "reason": "nothing_to_install"}
        return {"ran": False, "reason": "plan_failed", "detail": exc.message}

    plan = planned["plan"]
    operation_id = planned["operation"]["operation_id"]
    blockers = [item["code"] for item in plan.get("blockers", [])]
    reboot = bool(plan.get("reboot_required_before"))

    def abandon(reason, **extra):
        # The run's result says why; a banner a day, for a timer nobody
        # watched, says nothing more.
        try:
            client.call("operations.cancel", actor=SCHEDULE_ACTOR, operation_id=operation_id)
            client.call("operations.acknowledge", actor=SCHEDULE_ACTOR, operation_id=operation_id)
        except (AgentCallError, AgentUnavailableError):
            pass
        return {"ran": False, "reason": reason, "reboot_required": reboot, **extra}

    if blockers:
        return abandon("blocked", blockers=blockers)
    if not plan.get("package_count"):
        return abandon("nothing_to_install")

    try:
        client.call(
            "operations.execute",
            actor=SCHEDULE_ACTOR,
            operation_id=operation_id,
            confirmation_token=planned["confirmation_token"],
        )
        # execute returns once apt has been started, and an install the timer
        # reported before it ran is what the journal said for a failed one.
        record = wait_until_settled(
            client, operation_id, timeout=SCHEDULED_INSTALL_WAIT_SECONDS, sleep=sleep
        )
    except (AgentCallError, AgentUnavailableError) as exc:
        return {
            "ran": False,
            "reason": "install_failed",
            "detail": getattr(exc, "message", str(exc)),
            "reboot_required": reboot,
        }

    result = record.get("result") or {}
    if not record.get("settled"):
        return {"ran": False, "reason": "still_running", "operation_id": operation_id, "reboot_required": reboot}
    if record.get("state") != STATE_SUCCEEDED:
        return {
            "ran": False,
            "reason": "install_failed",
            "detail": (record.get("error") or {}).get("message", ""),
            "operation_id": operation_id,
            "reboot_required": bool(result.get("reboot_required", reboot)),
        }
    return {
        "ran": True,
        "reason": "",
        "installed": int(result.get("changed_package_count", plan.get("package_count")) or 0),
        "operation_id": operation_id,
        "reboot_required": bool(result.get("reboot_required", reboot)),
    }
