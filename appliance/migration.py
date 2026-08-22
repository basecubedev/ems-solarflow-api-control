# SPDX-License-Identifier: AGPL-3.0-or-later
"""Move an existing shared state layout into the web/agent split.

Runs as root from the package postinst and from the agent start-up. Migration
never destroys state: a source is removed only after its destination exists and
was verified, a symlinked source is refused rather than followed, and a
conflicting destination is preserved beside the source so an operator can
resolve it instead of silently losing one of the two.
"""

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

OWNER_WEB = "web"
OWNER_AGENT = "agent"
# The shared password file is read from inside the EMS containers, so it belongs
# to the identity they run as -- not to the web account and not to root.
OWNER_DEPLOYMENT = "deployment"

RESULT_MIGRATED = "migrated"
RESULT_SKIPPED = "skipped"
RESULT_ALREADY_DONE = "already_done"
RESULT_CONFLICT = "conflict"
RESULT_REFUSED = "refused"
RESULT_FAILED = "failed"

DEPLOYMENT_USER = "ems-deploy"
WEB_USER = "ems-appliance-web"
APPLIANCE_GROUP = "ems-appliance"

DIRECTORY_MODE = 0o750
PRIVATE_DIRECTORY_MODE = 0o700
FILE_MODE = 0o640

# Agent state is root:root and unreadable for the appliance group: the shared
# group exists for the socket, not for operation records or the audit trail.
AGENT_DIRECTORY_MODE = 0o700
AGENT_FILE_MODE = 0o600


@dataclass
class MigrationEntry:
    source: str
    destination: str
    owner: str
    result: str
    detail: str = ""

    def to_dict(self):
        return {
            "source": self.source,
            "destination": self.destination,
            "owner": self.owner,
            "result": self.result,
            "detail": self.detail,
        }


@dataclass
class MigrationReport:
    entries: list = field(default_factory=list)

    @property
    def migrated(self):
        return [item for item in self.entries if item.result == RESULT_MIGRATED]

    @property
    def findings(self):
        return [
            item
            for item in self.entries
            if item.result in (RESULT_CONFLICT, RESULT_REFUSED, RESULT_FAILED)
        ]

    @property
    def fatal(self):
        """Findings that leave the layout unusable, as opposed to needing a decision.

        A conflict keeps both copies and waits for an operator; a failed or
        refused move means the state did not arrive where the services read it.
        """

        return [item for item in self.entries if item.result in (RESULT_REFUSED, RESULT_FAILED)]

    @property
    def conflicts(self):
        return [item for item in self.entries if item.result == RESULT_CONFLICT]

    @property
    def ok(self):
        return not self.findings

    def to_dict(self):
        return {
            "ok": self.ok,
            "has_fatal": bool(self.fatal),
            "migrated": len(self.migrated),
            "findings": [item.to_dict() for item in self.findings],
            "fatal": [item.to_dict() for item in self.fatal],
            "conflicts": [item.to_dict() for item in self.conflicts],
            "entries": [item.to_dict() for item in self.entries],
        }


def _ids(name, group):
    import grp
    import pwd

    uid = gid = None
    try:
        uid = pwd.getpwnam(name).pw_uid
    except (KeyError, TypeError):
        uid = None
    try:
        gid = grp.getgrnam(group).gr_gid
    except (KeyError, TypeError):
        gid = None
    return uid, gid


def _apply_ownership(path, owner, *, mode=None):
    """Set the final owner and mode; missing accounts are not an error."""

    if owner == OWNER_DEPLOYMENT:
        uid, gid = _ids(DEPLOYMENT_USER, DEPLOYMENT_USER)
        directory_mode = DIRECTORY_MODE if mode is None else mode
        file_mode = 0o600
    elif owner == OWNER_WEB:
        uid, gid = _ids(WEB_USER, APPLIANCE_GROUP)
        directory_mode = DIRECTORY_MODE if mode is None else mode
        file_mode = FILE_MODE
    else:
        uid, gid = _ids("root", "root")
        directory_mode = AGENT_DIRECTORY_MODE if mode is None else mode
        file_mode = AGENT_FILE_MODE

    targets = [path]
    if path.is_dir():
        targets.extend(path.rglob("*"))
    for target in targets:
        try:
            if uid is not None and gid is not None:
                os.chown(target, uid, gid)
        except OSError:
            pass
        try:
            target.chmod(directory_mode if target.is_dir() else file_mode)
        except OSError:
            pass


def _same_content(source, destination):
    try:
        if source.is_file() and destination.is_file():
            return source.read_bytes() == destination.read_bytes()
    except OSError:
        return False
    return False


def _move(source, destination, owner, *, mode=None):
    """Copy first, verify, then drop the source."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=False)
        verified = destination.is_dir()
    else:
        shutil.copy2(source, destination)
        verified = destination.is_file() and destination.stat().st_size == source.stat().st_size
    if not verified:
        raise OSError(f"{destination} was not written completely")

    _apply_ownership(destination, owner, mode=mode)

    if source.is_dir():
        shutil.rmtree(source, ignore_errors=True)
    else:
        source.unlink(missing_ok=True)
    return True


def _is_empty_directory(path):
    try:
        return path.is_dir() and not any(path.iterdir())
    except OSError:
        return False


def _preserve(source, destination, owner, mode):
    preserved = destination.parent / f"{destination.name}.migrated-conflict"
    if not preserved.exists():
        if source.is_dir():
            shutil.copytree(source, preserved, dirs_exist_ok=True, symlinks=False)
        else:
            shutil.copy2(source, preserved)
        _apply_ownership(preserved, owner, mode=mode)
    return preserved


def _merge_directory(source, destination, owner, mode):
    """Move entries the destination does not have; keep conflicts side by side."""

    conflicts = []
    for entry in sorted(source.rglob("*")):
        if entry.is_symlink():
            conflicts.append(f"{entry} is a symlink")
            continue
        relative = entry.relative_to(source)
        target = destination / relative
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, target)
            continue
        if not _same_content(entry, target):
            conflicts.append(str(relative))
            _preserve(entry, target, owner, mode)

    _apply_ownership(destination, owner, mode=mode)
    if conflicts:
        return conflicts

    shutil.rmtree(source, ignore_errors=True)
    return []


def _migrate_one(source, destination, owner, *, mode=None):
    # A symlinked source could redirect the copy outside the appliance layout.
    if source.is_symlink():
        return MigrationEntry(
            str(source),
            str(destination),
            owner,
            RESULT_REFUSED,
            "the old path is a symlink and was left untouched",
        )

    if not source.exists():
        if destination.exists():
            _apply_ownership(destination, owner, mode=mode)
        return MigrationEntry(str(source), str(destination), owner, RESULT_SKIPPED)

    if source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        try:
            conflicts = _merge_directory(source, destination, owner, mode)
        except OSError as exc:
            return MigrationEntry(
                str(source), str(destination), owner, RESULT_FAILED, exc.__class__.__name__
            )
        if conflicts:
            return MigrationEntry(
                str(source),
                str(destination),
                owner,
                RESULT_CONFLICT,
                "both layouts hold different data for: " + ", ".join(conflicts[:5]),
            )
        return MigrationEntry(str(source), str(destination), owner, RESULT_MIGRATED)

    if destination.exists() and not _is_empty_directory(destination):
        if _same_content(source, destination):
            source.unlink(missing_ok=True)
            _apply_ownership(destination, owner, mode=mode)
            return MigrationEntry(str(source), str(destination), owner, RESULT_ALREADY_DONE)
        try:
            preserved = _preserve(source, destination, owner, mode)
        except OSError as exc:
            return MigrationEntry(
                str(source), str(destination), owner, RESULT_FAILED, exc.__class__.__name__
            )
        return MigrationEntry(
            str(source),
            str(destination),
            owner,
            RESULT_CONFLICT,
            f"both layouts hold data; the old copy is kept at {preserved}",
        )

    try:
        _move(source, destination, owner, mode=mode)
    except OSError as exc:
        return MigrationEntry(
            str(source), str(destination), owner, RESULT_FAILED, exc.__class__.__name__
        )
    return MigrationEntry(str(source), str(destination), owner, RESULT_MIGRATED)


def migration_plan(paths):
    """The legacy shared layout mapped onto its owner in the new split."""

    return (
        # An appliance installed before the password became shared carries the
        # old web-owned file; it moves to the store the Admin console reads.
        (paths.legacy_auth_file, paths.auth_file, OWNER_DEPLOYMENT, DIRECTORY_MODE),
        (paths.legacy_state_file, paths.state_file, OWNER_WEB, DIRECTORY_MODE),
        (paths.legacy_operations_dir, paths.operations_dir, OWNER_AGENT, AGENT_DIRECTORY_MODE),
        (paths.legacy_known_good_dir, paths.known_good_dir, OWNER_AGENT, AGENT_DIRECTORY_MODE),
        (paths.legacy_ssh_keys_dir, paths.ssh_keys_dir, OWNER_AGENT, AGENT_DIRECTORY_MODE),
        (
            paths.legacy_compose_backup_dir,
            paths.compose_backup_dir,
            OWNER_AGENT,
            AGENT_DIRECTORY_MODE,
        ),
        (paths.legacy_packages_dir, paths.packages_dir, OWNER_AGENT, AGENT_DIRECTORY_MODE),
        (paths.legacy_appliance_log, paths.appliance_log, OWNER_WEB, DIRECTORY_MODE),
        (paths.legacy_operations_log, paths.operations_log, OWNER_AGENT, AGENT_DIRECTORY_MODE),
        (paths.legacy_audit_log, paths.audit_log, OWNER_AGENT, AGENT_DIRECTORY_MODE),
    )


def migrate_state(paths, *, create_directories=True):
    """Move legacy state into the split layout. Safe to run repeatedly."""

    report = MigrationReport()

    if create_directories:
        for directory in paths.web_directories():
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            _apply_ownership(
                directory,
                OWNER_WEB,
                mode=PRIVATE_DIRECTORY_MODE
                if directory == paths.web_sessions_dir
                else DIRECTORY_MODE,
            )
        for directory in paths.agent_directories():
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            _apply_ownership(directory, OWNER_AGENT, mode=AGENT_DIRECTORY_MODE)

    for source, destination, owner, mode in migration_plan(paths):
        report.entries.append(_migrate_one(Path(source), Path(destination), owner, mode=mode))

    return report


def write_report(paths, report):
    target = paths.agent_state_dir / "migration.json"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n")
        target.chmod(AGENT_FILE_MODE)
    except OSError:
        return None
    return target
