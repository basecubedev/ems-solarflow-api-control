# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the SFTP export root actually is, according to the kernel.

The backup account is chrooted into the export root, so the usable boundary is
not the sshd policy alone: it is the policy plus an export root that contains
only the three managed mount points, each publishing the configured EMS
directory, read-only. This module answers that question once, and the
activation path, the runtime status and ``verify-install`` all use the answer
rather than each deciding for themselves.

Nothing here mutates anything, and nothing here trusts the report the setup
script wrote — the mount table and the directory itself are the evidence.
"""

import os
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path

from appliance.paths import chroot_chain_problems, runtime_boundary_problems

EXPECTED_EXPORTS = ("config", "backups", "data")

STATE_MOUNTED = "mounted"
STATE_MISSING = "missing"
STATE_UNMOUNTED = "not_mounted"
STATE_WRITABLE = "writable"
STATE_FOREIGN = "foreign"

EXPORT_ROOT_MODE = 0o755


def device_id(entry):
    """``major:minor`` of a stat result, the form ``mountinfo`` uses."""

    return f"{os.major(entry.st_dev)}:{os.minor(entry.st_dev)}"


def path_within_filesystem(path, *, mounts=None, is_mount=None):
    """A path the way ``mountinfo`` names it: relative to its filesystem's root.

    Binding ``/persistent/ems/config`` publishes a record whose root reads
    ``/ems/config``, because the persistent partition is mounted at
    ``/persistent``. The absolute path only equals that text while the source
    happens to sit on the filesystem mounted at ``/`` — true on a developer
    machine, false on the A/B appliance.

    Stopping at the nearest mount point is not enough there either: on the A/B
    image the enclosing directory is itself a bind whose own root is not ``/``,
    so ``/opt/ems-solarflow/config`` is published as
    ``/shared/opt/ems-solarflow/config``. The enclosing mount's root has to be
    carried along, or every export reads as foreign and backup access disables
    itself on each boot.
    """

    at_mount = is_mount or (lambda candidate: os.path.ismount(candidate))
    current = Path(path)
    parts = []
    while current != current.parent and not at_mount(str(current)):
        parts.append(current.name)
        current = current.parent
    tail = "/" + "/".join(reversed(parts)) if parts else "/"

    enclosing = (mounts or {}).get(str(current)) or {}
    root = str(enclosing.get("root") or "/").rstrip("/")
    if not root:
        return tail
    return root if tail == "/" else root + tail


def publishes_source(source, record, *, mounts=None, is_mount=None):
    """Whether the mount record really publishes ``source``.

    Two independent facts have to agree: the mount carries the filesystem the
    source lives on, and it is rooted at the source's place inside it.
    """

    try:
        source_entry = os.stat(str(source))
    except OSError:
        return False
    if record.get("device") != device_id(source_entry):
        return False
    return str(record.get("root") or "") == path_within_filesystem(
        source, mounts=mounts, is_mount=is_mount
    )


@dataclass(frozen=True)
class ExportEntry:
    name: str
    source: str
    target: str
    source_present: bool
    state: str
    read_only: bool
    source_verified: bool
    mounted_source: str
    detail: str = ""

    @property
    def confined(self):
        return self.state == STATE_MOUNTED and self.read_only and self.source_verified

    def to_dict(self):
        return {
            "name": self.name,
            "source": self.source,
            "target": self.target,
            "source_present": self.source_present,
            "state": self.state,
            "read_only": self.read_only,
            "source_verified": self.source_verified,
            "mounted_source": self.mounted_source,
            "confined": self.confined,
            "detail": self.detail,
        }


def unmanaged_entries(export_root):
    """Names inside the chroot root that this feature does not manage."""

    try:
        names = sorted(os.listdir(str(export_root)))
    except OSError:
        return []
    unmanaged = []
    for name in names:
        path = os.path.join(str(export_root), name)
        if name not in EXPECTED_EXPORTS:
            unmanaged.append(name)
            continue
        try:
            entry = os.lstat(path)
        except OSError:
            unmanaged.append(name)
            continue
        if not stat_module.S_ISDIR(entry.st_mode):
            unmanaged.append(name)
    return unmanaged


def _root_problems(export_root):
    problems = []
    try:
        entry = os.lstat(str(export_root))
    except FileNotFoundError:
        return ["the export root does not exist"]
    except OSError as exc:
        return [f"the export root cannot be inspected: {exc.__class__.__name__}"]
    if stat_module.S_ISLNK(entry.st_mode):
        return ["the export root is a symbolic link"]
    if not stat_module.S_ISDIR(entry.st_mode):
        return ["the export root is not a directory"]
    problems.extend(chroot_chain_problems(export_root))
    return problems


def _inspect_one(name, source, target, record, mounts=None):
    source_present = False
    detail = ""
    try:
        entry = os.lstat(str(source))
        if stat_module.S_ISLNK(entry.st_mode):
            detail = f"{name} is a symbolic link, not a real EMS directory"
        elif not stat_module.S_ISDIR(entry.st_mode):
            detail = f"{name} is not a directory"
        else:
            source_present = True
    except OSError:
        source_present = False

    if record is None:
        state = STATE_MISSING if not source_present and not detail else STATE_UNMOUNTED
        if state == STATE_UNMOUNTED and not detail:
            detail = f"{name} is not published at {target}"
        return ExportEntry(
            name=name,
            source=str(source),
            target=str(target),
            source_present=source_present,
            state=state,
            read_only=False,
            source_verified=False,
            mounted_source="",
            detail=detail,
        )

    options = record.get("options") or frozenset()
    read_only = "ro" in options
    mounted_source = str(record.get("root") or "")
    verified = source_present and publishes_source(source, record, mounts=mounts)
    if not verified:
        detail = detail or f"{target} does not publish {source}"
        state = STATE_FOREIGN
    elif not read_only:
        detail = f"{name} is exported read-write"
        state = STATE_WRITABLE
    else:
        state = STATE_MOUNTED
    return ExportEntry(
        name=name,
        source=str(source),
        target=str(target),
        source_present=source_present,
        state=state,
        read_only=read_only,
        source_verified=verified,
        mounted_source=mounted_source,
        detail=detail,
    )


def inspect_exports(paths, *, mounts=None):
    """The complete export state: exactly three managed entries, or a reason."""

    mounts = {} if mounts is None else mounts
    # The configured paths are re-validated here rather than only at install
    # time: a component that became a symbolic link since would redirect what
    # the chroot publishes.
    boundary = runtime_boundary_problems(paths)
    problems = boundary + _root_problems(paths.export_root)
    unmanaged = unmanaged_entries(paths.export_root)
    if unmanaged:
        problems.append(
            "the export root contains entries this appliance does not manage: "
            + ", ".join(unmanaged)
        )

    sources = paths.export_paths()
    targets = paths.export_targets()
    entries = [
        _inspect_one(
            name, sources[name], targets[name], mounts.get(str(targets[name])), mounts
        )
        for name in EXPECTED_EXPORTS
    ]

    pending = [item.name for item in entries if item.state == STATE_MISSING]
    present = [item for item in entries if item.state != STATE_MISSING]
    for item in present:
        if not item.confined and item.detail:
            problems.append(item.detail)

    return {
        "export_root": str(paths.export_root),
        "expected": list(EXPECTED_EXPORTS),
        "boundary_problems": boundary,
        "unmanaged": unmanaged,
        "entries": [item.to_dict() for item in entries],
        "pending": pending,
        "mounted": [item.name for item in entries if item.state == STATE_MOUNTED],
        "exact": not problems,
        "confined": bool(present) and not problems,
        "problems": problems,
    }


def verify_reported_exports(report, state, paths):
    """Cross-check the setup script's report against the observed state.

    The report is diagnostic input: an entry it omits, an entry it invents and a
    source or target it names wrongly are all reasons to refuse it, whatever
    status it claims.
    """

    problems = []
    sources = paths.export_paths()
    targets = paths.export_targets()
    reported = {}
    for entry in report.get("paths") or []:
        name = str(entry.get("name") or "")
        if name in reported:
            problems.append(f"{name} is reported twice")
        reported[name] = entry

    for name in EXPECTED_EXPORTS:
        entry = reported.pop(name, None)
        if entry is None:
            problems.append(f"{name} is missing from the export report")
            continue
        if str(entry.get("source") or "") != str(sources[name]):
            problems.append(f"{name} is reported with a source that is not {sources[name]}")
        if str(entry.get("target") or "") != str(targets[name]):
            problems.append(f"{name} is reported with a target that is not {targets[name]}")
    for name in sorted(reported):
        problems.append(f"{name or 'an unnamed export'} is not an expected export")

    problems.extend(state["problems"])
    return problems
