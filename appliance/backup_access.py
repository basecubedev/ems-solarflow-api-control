# SPDX-License-Identifier: AGPL-3.0-or-later
"""Host-level backup access.

The appliance does not define a second EMS backup format. It reports where the
EMS backup directory is, whether the dedicated read-only account can reach it,
and shows ready-to-use SFTP commands. It never displays or accepts a private
key.

The account is confined, not merely shell-less: sshd chroots it into
``/srv/ems-appliance-export`` and the EMS directories appear there as read-only
bind mounts. What this module reports is the state it can observe — the live
mount table and the effective sshd configuration — not what the setup script
intended.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from appliance.backup_confinement import (
    OPTION_CHROOT,
    OPTION_FORCE_COMMAND,
    evaluate_policy,
)
from appliance.export_state import (
    STATE_FOREIGN,
    STATE_MOUNTED,
    STATE_UNMOUNTED,
    STATE_WRITABLE,
    inspect_exports,
)

ACCESS_READ_ONLY = "read-only"
# The packaged sshd drop-in forces internal-sftp for this account, so scp and
# rsync (which need to execute a remote command) are not available.
ACCESS_PROTOCOL = "sftp"

EXPORT_DEFAULTS = {
    "config": ACCESS_READ_ONLY,
    "backups": ACCESS_READ_ONLY,
    "data": ACCESS_READ_ONLY,
}

STATUS_CONFIGURED = "configured"
STATUS_PENDING = "pending"
STATUS_DEGRADED = "degraded"
STATUS_UNKNOWN = "unknown"


@dataclass(frozen=True)
class ExportPath:
    name: str
    path: str
    exists: bool
    access: str
    size_bytes: int
    entries: int
    export_target: str = ""
    state: str = STATE_UNMOUNTED
    read_only: bool = False
    mounted_source: str = ""
    source_verified: bool = False

    @property
    def confined(self):
        """Mounted, read-only and publishing the configured directory — all three.

        Reported rather than left to be recombined: the three inputs are next to
        each other in this payload, and any consumer that checks only one of
        them would call a writable or redirected export a confined one.
        """

        return self.state == STATE_MOUNTED and self.read_only and self.source_verified

    def to_dict(self):
        return {
            "name": self.name,
            "path": self.path,
            "exists": self.exists,
            "access": self.access,
            "size_bytes": self.size_bytes,
            "size_mb": round(self.size_bytes / (1024 * 1024), 1),
            "entries": self.entries,
            "export_target": self.export_target,
            "state": self.state,
            "read_only": self.read_only,
            "mounted_source": self.mounted_source,
            "source_verified": self.source_verified,
            "confined": self.confined,
        }


def directory_size(path, *, max_entries=20000):
    total, count = 0, 0
    if not path.is_dir():
        return 0, 0
    for entry in path.rglob("*"):
        if count >= max_entries:
            break
        try:
            if entry.is_file():
                total += entry.stat().st_size
                count += 1
        except OSError:
            continue
    return total, count


class BackupAccessService:
    def __init__(self, *, paths, config, ssh_service, probe):
        self.paths = paths
        self.config = config
        self.ssh_service = ssh_service
        self.probe = probe

    # --- confinement ------------------------------------------------------

    def export_state(self):
        """What the kernel says the export root is, right now."""

        try:
            mounts = self.probe.mount_records()
        except Exception:
            mounts = {}
        return inspect_exports(self.paths, mounts=mounts)

    def confinement_state(self):
        """Every restriction the appliance promises, as sshd would apply it."""

        try:
            effective = self.ssh_service.effective_config(user=self.config.backup_user)
        except Exception:
            effective = {}
        return evaluate_policy(effective, export_root=self.paths.export_root)

    def chroot_state(self, policy=None):
        """The chroot and forced command sshd would actually apply."""

        policy = self.confinement_state() if policy is None else policy
        restrictions = policy["restrictions"]
        return {
            "expected": str(self.paths.export_root),
            "configured": restrictions[OPTION_CHROOT]["value"],
            "force_command": restrictions[OPTION_FORCE_COMMAND]["value"],
            "enforced": bool(
                restrictions[OPTION_CHROOT]["confirmed"]
                and restrictions[OPTION_FORCE_COMMAND]["confirmed"]
            ),
            "available": policy["available"],
        }

    # --- reporting --------------------------------------------------------

    def export_paths(self, state=None):
        state = self.export_state() if state is None else state
        records = []
        for entry in state["entries"]:
            path = Path(entry["source"])
            size, entries = directory_size(path)
            records.append(
                ExportPath(
                    name=entry["name"],
                    path=entry["source"],
                    exists=entry["source_present"],
                    access=EXPORT_DEFAULTS.get(entry["name"], ACCESS_READ_ONLY),
                    size_bytes=size,
                    entries=entries,
                    export_target=entry["target"],
                    state=entry["state"],
                    read_only=entry["read_only"],
                    mounted_source=entry["mounted_source"],
                    source_verified=entry["source_verified"],
                )
            )
        return records

    def status(self):
        account_name = self.config.backup_user
        try:
            account = self.ssh_service.account(account_name)
            account_record = account.to_dict()
            keys = []
            if account.exists and account.home:
                keys = [key.to_dict() for key in self.ssh_service.keystore(account_name).list()]
            account_record["key_count"] = len(keys)
            account_record["keys"] = keys
        except Exception:
            account_record = {"name": account_name, "exists": False, "key_count": 0, "keys": []}

        host = self.probe.hostname()
        target_host = host["mdns"] or host["hostname"] or "ems-solarflow.local"
        state = self.export_state()
        exports = [item.to_dict() for item in self.export_paths(state)]
        policy = self.confinement_state()
        chroot = self.chroot_state(policy)

        return {
            "account": account_record,
            "host": target_host,
            "paths": exports,
            "write_access": False,
            "protocol": ACCESS_PROTOCOL,
            "shell_access": False,
            "confined": bool(policy["confirmed"]) and state["confined"],
            "chroot": chroot,
            "confinement": policy,
            "export_root": str(self.paths.export_root),
            "unmanaged_entries": state["unmanaged"],
            "export_access": self.export_access(exports, chroot, policy=policy, state=state),
            "examples": self.examples(account_name, target_host),
            "note": self.note(policy),
        }

    @staticmethod
    def note(policy):
        """Say only what was observed; an unverified restriction is not a promise."""

        if not policy["available"]:
            return (
                "The effective SSH policy for the backup account could not be read, so "
                "no restriction is confirmed. Backup access is reported as unverified "
                "until it can be."
            )
        if policy["confirmed"]:
            return (
                "The backup account is chrooted into the export root and restricted to "
                "SFTP: no shell, no TTY and no forwarding, verified against the policy "
                "sshd applies. The exported EMS directories are read-only bind mounts. "
                "Administrative write access requires a separate host account and is not "
                "enabled by default."
            )
        return (
            "The running SSH daemon does not apply every restriction this account "
            "requires (" + ", ".join(policy["violations"]) + "). Backup access is "
            "disabled until the confinement is in force again."
        )

    def export_access(self, exports=None, chroot=None, *, policy=None, state=None):
        """The observed export state, with the setup script's report as detail."""

        state = self.export_state() if state is None else state
        exports = (
            [item.to_dict() for item in self.export_paths(state)] if exports is None else exports
        )
        policy = self.confinement_state() if policy is None else policy
        chroot = self.chroot_state(policy) if chroot is None else chroot
        recorded = self._recorded_status()

        present = [item for item in exports if item["exists"]]
        failed = [item["name"] for item in exports if item["state"] == STATE_WRITABLE]
        foreign = [item["name"] for item in exports if item["state"] == STATE_FOREIGN]
        unmounted = [
            item["name"] for item in present if item["state"] not in (STATE_MOUNTED,)
        ]

        if state["unmanaged"]:
            status = STATUS_DEGRADED
            detail = "the export root contains unmanaged entries: " + ", ".join(
                state["unmanaged"]
            )
        elif not present:
            status, detail = STATUS_PENDING, "no EMS export directory exists yet"
        elif failed:
            status = STATUS_DEGRADED
            detail = "exported read-write instead of read-only: " + ", ".join(failed)
        elif foreign:
            status = STATUS_DEGRADED
            detail = "not published from the configured EMS directory: " + ", ".join(foreign)
        elif unmounted:
            status = STATUS_DEGRADED
            detail = "not published into the export root: " + ", ".join(unmounted)
        elif not policy["available"]:
            status = STATUS_UNKNOWN
            detail = "the effective sshd configuration could not be read"
        elif not chroot["enforced"]:
            status = STATUS_DEGRADED
            detail = "sshd does not chroot the backup account into the export root"
        elif not policy["confirmed"]:
            status = STATUS_DEGRADED
            detail = "sshd does not enforce: " + ", ".join(policy["violations"])
        else:
            status = STATUS_CONFIGURED
            detail = "read-only SFTP export root is active"

        return {
            "status": status,
            "detail": detail,
            "export_root": str(self.paths.export_root),
            "mounted": [item["name"] for item in exports if item["state"] == STATE_MOUNTED],
            "missing": [item["name"] for item in exports if not item["exists"]],
            "reported": recorded,
        }

    def _recorded_status(self):
        """What the packaged setup script last wrote, for diagnosis only."""

        try:
            payload = json.loads(self.paths.export_status_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"status": STATUS_UNKNOWN, "detail": "the export root has not been set up yet"}
        if not isinstance(payload, dict):
            return {"status": STATUS_UNKNOWN, "detail": "the export status file is malformed"}
        return payload

    def examples(self, account_name, target_host):
        # /data/backups, not /backups. EMS writes its archives under data, and
        # the separate /backups export publishes a directory no writer in this
        # project has ever used -- so the obvious command came back empty and
        # said nothing, which a backup feature only discovers at restore time.
        return [
            {
                "title": "Copy all EMS backups",
                "command": f"sftp -r {account_name}@{target_host}:/data/backups ./ems-backups",
            },
            {
                "title": "Copy the EMS configuration",
                "command": f"sftp -r {account_name}@{target_host}:/config ./ems-config",
            },
            {
                "title": "Browse the exported paths",
                "command": f"sftp {account_name}@{target_host}",
            },
        ]
