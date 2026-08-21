# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operating-system package status, installation and recovery.

Package names never come from the browser: an installation upgrades exactly the
packages the appliance itself parsed out of a simulated apt run. A real active
package-manager lock is reported, never removed, and no distribution upgrade is
offered.
"""

import fcntl
import os
import re
import time
from dataclasses import dataclass, field

from appliance.hostprobe import DPKG_LOCK_FILE
from appliance.operations import STATE_FAILED_TERMINAL, STATE_SUCCEEDED, STATE_VERIFYING
from appliance.redaction import bounded_redacted_log
from appliance.validation import (
    PACKAGE_REPAIR_CONFIGURE,
    PACKAGE_REPAIR_FIX_BROKEN,
    PACKAGE_REPAIR_REFRESH_INDEX,
    UPDATE_SCOPE_ALL,
    UPDATE_SCOPE_SECURITY,
)

TYPE_UPDATE_INSTALL = "updates.install"
TYPE_UPDATE_REPAIR = "updates.repair"

PACKAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+._-]*$")
INST_LINE = re.compile(r"^Inst\s+(\S+)\s+(?:\[(\S+)\]\s+)?\((\S+)\s+([^)]*)\)")

KERNEL_PREFIXES = ("linux-image", "linux-headers", "raspberrypi-kernel")
FIRMWARE_PREFIXES = ("raspi-firmware", "raspberrypi-bootloader", "firmware-")

LOCK_FREE = "free"
LOCK_HELD = "held"
LOCK_UNKNOWN = "unknown"


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
    index_age_seconds: int = None
    error: str = ""

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
        return not self.dpkg_issues and self.lock_state != LOCK_HELD and not self.error

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
            "index_age_seconds": self.index_age_seconds,
            "error": self.error,
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
    issues = []
    for line in (text or "").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("The following"):
            continue
        issues.append(entry)
    return issues


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
        """Report whether another package manager holds the dpkg frontend lock.

        The lock is probed with a non-blocking flock and released immediately.
        An active lock is never removed: a half-finished dpkg run must be
        allowed to complete.
        """

        path = self.probe.root / DPKG_LOCK_FILE
        try:
            handle = os.open(str(path), os.O_RDONLY)
        except OSError:
            return LOCK_UNKNOWN
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
            return LOCK_FREE
        except OSError:
            return LOCK_HELD
        finally:
            os.close(handle)

    def check(self):
        """Read update state without modifying any package or index."""

        state = PackageState()
        if not self.runner.available("apt-get"):
            state.error = "package_manager_unavailable"
            return state

        simulated = self.runner.run(
            "apt-get", ["-s", "-o", "Debug::NoLocking=1", "upgrade"], timeout=120
        )
        if simulated.ok:
            state.updates = parse_simulated_upgrade(simulated.stdout)
        else:
            state.error = "update_check_failed"

        selections = self.runner.run("dpkg", ["--get-selections"], timeout=60)
        state.held = parse_held_packages(selections.stdout if selections.ok else "")

        audit = self.runner.run("dpkg", ["--audit"], timeout=60)
        state.dpkg_issues = parse_dpkg_audit(audit.stdout if audit.ok else audit.stderr)

        state.lock_state = self.lock_state()

        reboot = self.probe.reboot_required()
        state.reboot_required = reboot["required"]
        state.reboot_packages = reboot["packages"]

        filesystem = self.probe.filesystem("/var")
        state.free_megabytes = filesystem.get("free_mb", 0) if filesystem.get("available") else 0

        lists_dir = self.probe.root / "var/lib/apt/lists"
        try:
            state.index_age_seconds = int(self._time() - lists_dir.stat().st_mtime)
        except OSError:
            state.index_age_seconds = None

        return state

    # --- planning --------------------------------------------------------

    def plan_install(self, operation, scope):
        state = self.check()
        targets = state.security_updates if scope == UPDATE_SCOPE_SECURITY else state.updates
        blockers = self._blockers(state)

        if not targets and not blockers:
            raise PackageError("no_updates_available", "there is nothing to install")

        values = {"scope": scope, "packages": [item.name for item in targets]}
        operation.requested_target.update(values)
        self.operations.update_target(operation.operation_id, values)

        return {
            "type": TYPE_UPDATE_INSTALL,
            "scope": scope,
            "packages": [item.to_dict() for item in targets],
            "package_count": len(targets),
            "blockers": blockers,
            "free_megabytes": state.free_megabytes,
            "minimum_free_megabytes": self.config.minimum_free_megabytes,
            "reboot_required_before": state.reboot_required,
            "package_manager": state.to_dict()["package_manager"],
        }

    def plan_repair(self, operation, action):
        state = self.check()
        values = {"action": action}
        operation.requested_target.update(values)
        self.operations.update_target(operation.operation_id, values)
        return {
            "type": TYPE_UPDATE_REPAIR,
            "action": action,
            "dpkg_issues": state.dpkg_issues,
            "lock_state": state.lock_state,
            "blockers": [
                blocker for blocker in self._blockers(state) if blocker["code"] == "package_lock_held"
            ],
        }

    def _root_is_read_only(self):
        try:
            return bool(os.statvfs("/").f_flag & os.ST_RDONLY)
        except OSError:
            return False

    def _blockers(self, state):
        blockers = []
        if self._root_is_read_only():
            # On an A/B image the slot root is read-only and belongs to the
            # running slot: apt would fail partway through, and anything it did
            # manage to write is discarded at the next slot switch. The UI hides
            # the path; the refusal has to exist on this side of the socket too.
            blockers.append(
                {
                    "code": "read_only_root",
                    "message": (
                        "this appliance runs an A/B image, where the root filesystem belongs "
                        "to the running slot and is read-only; host packages come with an OS "
                        "update instead"
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
        if state.free_megabytes and state.free_megabytes < self.config.minimum_free_megabytes:
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
        packages = [
            name
            for name in operation.requested_target.get("packages", [])
            if PACKAGE_NAME_RE.match(str(name))
        ]

        self._advance(operation, "preflight")
        state = self.check()
        blockers = self._blockers(state)
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
            args = ["-y", "-o", "Dpkg::Options::=--force-confold", "upgrade"]
        else:
            if not packages:
                raise PackageError("no_updates_available", "no security updates are pending")
            args = [
                "-y",
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
        if action == PACKAGE_REPAIR_CONFIGURE:
            result = self.runner.run("dpkg", ["--configure", "-a"], timeout=1800)
        elif action == PACKAGE_REPAIR_FIX_BROKEN:
            result = self.runner.run("apt-get", ["-y", "-f", "install"], timeout=1800)
        elif action == PACKAGE_REPAIR_REFRESH_INDEX:
            result = self.runner.run("apt-get", ["update"], timeout=900)
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
