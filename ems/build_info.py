# SPDX-License-Identifier: AGPL-3.0-or-later
"""Best-effort build/version metadata shared by diagnostics and backups.

This module is intentionally import-side-effect-free so both ``emsctl.py`` and
``ems`` submodules can depend on it without import cycles. All values are
collected on a best-effort basis: missing git or a non-repo checkout yields safe
``"unknown"`` / ``None`` fallbacks rather than raising.
"""

import os
import shutil
import subprocess

from ems.paths import BASE_DIR
from ems.version import __version__


def _run_git(args, cwd):
    git_exe = shutil.which("git")
    if not git_exe:
        return None
    try:
        result = subprocess.run(
            [git_exe, *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def collect_build_info(base_dir=None):
    """Return best-effort build metadata.

    Keys: ``ems_version``, ``git_commit``, ``git_commit_short``,
    ``git_branch``, ``git_describe``, ``git_dirty``. String fields fall back to
    ``"unknown"`` and ``git_dirty`` to ``None`` when it cannot be determined.
    """

    base_dir = base_dir or BASE_DIR

    info = {
        "ems_version": __version__,
        "git_commit": "unknown",
        "git_commit_short": "unknown",
        "git_branch": "unknown",
        "git_describe": "unknown",
        "git_dirty": None,
    }

    if not os.path.isdir(os.path.join(base_dir, ".git")):
        return info

    commit = _run_git(["rev-parse", "HEAD"], base_dir)
    if commit:
        info["git_commit"] = commit
        info["git_commit_short"] = commit[:12]

    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], base_dir)
    if branch:
        info["git_branch"] = branch

    describe = _run_git(["describe", "--tags", "--always", "--dirty"], base_dir)
    if describe:
        info["git_describe"] = describe

    status = _run_git(["status", "--porcelain"], base_dir)
    if status is not None:
        info["git_dirty"] = bool(status.strip())

    return info
