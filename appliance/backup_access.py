# SPDX-License-Identifier: AGPL-3.0-or-later
"""Host-level backup access.

The appliance does not define a second EMS backup format. It reports where the
EMS backup directory is, whether the dedicated read-only account can reach it,
and shows ready-to-use rsync/scp commands. It never displays or accepts a
private key.
"""

from dataclasses import dataclass

ACCESS_READ_ONLY = "read-only"

EXPORT_DEFAULTS = {
    "config": ACCESS_READ_ONLY,
    "backups": ACCESS_READ_ONLY,
    "data": ACCESS_READ_ONLY,
}


@dataclass(frozen=True)
class ExportPath:
    name: str
    path: str
    exists: bool
    access: str
    size_bytes: int
    entries: int

    def to_dict(self):
        return {
            "name": self.name,
            "path": self.path,
            "exists": self.exists,
            "access": self.access,
            "size_bytes": self.size_bytes,
            "size_mb": round(self.size_bytes / (1024 * 1024), 1),
            "entries": self.entries,
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

    def export_paths(self):
        records = []
        for name, path in self.paths.export_paths().items():
            size, entries = directory_size(path)
            records.append(
                ExportPath(
                    name=name,
                    path=str(path),
                    exists=path.is_dir(),
                    access=EXPORT_DEFAULTS.get(name, ACCESS_READ_ONLY),
                    size_bytes=size,
                    entries=entries,
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
        exports = [item.to_dict() for item in self.export_paths()]

        return {
            "account": account_record,
            "host": target_host,
            "paths": exports,
            "write_access": False,
            "examples": self.examples(account_name, target_host),
            "note": "Administrative write access requires a separate host account and is not "
            "enabled by default.",
        }

    def examples(self, account_name, target_host):
        backups = self.paths.ems_backups_dir
        config_dir = self.paths.ems_config_dir
        return [
            {
                "title": "Copy all EMS backups",
                "command": f"rsync -a {account_name}@{target_host}:{backups}/ ./ems-backups/",
            },
            {
                "title": "Copy the EMS configuration",
                "command": f"scp -r {account_name}@{target_host}:{config_dir} ./ems-config",
            },
        ]
