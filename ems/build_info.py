# SPDX-License-Identifier: AGPL-3.0-or-later
"""Honest build/version identity shared by diagnostics and backups.

Import-side-effect-free so both ``emsctl.py`` and ``ems`` submodules can depend
on it without import cycles. Release identity is never hardcoded: it comes from
CI build environment variables (a real release tag) or, failing that, the local
Git checkout. Absent values are ``None`` — never a fake ``"unknown"`` and never a
channel name like ``latest`` masquerading as a release version.
"""

import os
import re
import shutil
import subprocess

from ems.paths import BASE_DIR

# A real release tag: v1.2.3 with an optional pre-release/build suffix
# (v1.2.3-rc1, v1.2.3-beta.1, v1.2.3+build.5). Channel names such as ``latest``
# or ``main`` never match, so they can never become a release version.
RELEASE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

# Placeholders that older/foreign metadata used for "no value".
_ABSENT = {"", "unknown", "none", "null", "-"}


def _clean(value):
    """Normalize a raw string to a real value or ``None``.

    Whitespace is stripped and placeholders (``unknown``/``none``/...) are treated
    as absent so they never leak into stored metadata or the UI.
    """

    if value is None:
        return None
    text = str(value).strip()
    return text if text.lower() not in _ABSENT else None


def _clean_bool(value):
    text = _clean(value)
    if text is None:
        return None
    return text.lower() in ("1", "true", "yes", "on", "dirty")


def _release_version(raw):
    tag = _clean(raw)
    if tag is not None and RELEASE_TAG_RE.match(tag):
        return tag
    return None


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


def _git_info(base_dir):
    """Best-effort git metadata for a local checkout (``None`` values if absent)."""

    info = {
        "git_commit": None,
        "git_commit_short": None,
        "git_branch": None,
        "git_describe": None,
        "git_dirty": None,
    }
    if not os.path.isdir(os.path.join(base_dir, ".git")):
        return info

    commit = _clean(_run_git(["rev-parse", "HEAD"], base_dir))
    if commit:
        info["git_commit"] = commit
        info["git_commit_short"] = commit[:12]
    info["git_branch"] = _clean(
        _run_git(["rev-parse", "--abbrev-ref", "HEAD"], base_dir)
    )
    info["git_describe"] = _clean(
        _run_git(["describe", "--tags", "--always", "--dirty"], base_dir)
    )
    status = _run_git(["status", "--porcelain"], base_dir)
    if status is not None:
        info["git_dirty"] = bool(status.strip())
    return info


def collect_build_info(base_dir=None, environ=None):
    """Return honest build identity for the running EMS.

    Keys: ``ems_version``/``release_version`` (a real release tag or ``None``),
    ``build_label`` (best display label or ``None``), ``git_commit``,
    ``git_commit_short``, ``git_branch``, ``git_describe``, ``git_dirty``,
    ``build_id``, ``build_serial``, ``channel``.

    CI build environment variables win; a local Git checkout fills whatever they
    do not provide. A release version is set only for an actual release tag, so
    ``latest``/``main``/scheduled builds report ``ems_version=None``.
    """

    base_dir = base_dir or BASE_DIR
    environ = os.environ if environ is None else environ

    release_version = _release_version(environ.get("EMS_RELEASE_TAG"))

    git_commit = _clean(environ.get("EMS_GIT_COMMIT"))
    git_commit_short = _clean(environ.get("EMS_GIT_COMMIT_SHORT"))
    git_branch = _clean(environ.get("EMS_GIT_BRANCH"))
    git_describe = _clean(environ.get("EMS_GIT_DESCRIBE"))
    git_dirty = _clean_bool(environ.get("EMS_GIT_DIRTY"))

    # Env and local git are whole sources, never mixed: if CI provided any git
    # identity we trust it entirely (a packaged image has no ``.git`` to read),
    # otherwise the local checkout is the only source.
    env_has_git = git_dirty is not None or any(
        field is not None
        for field in (git_commit, git_commit_short, git_branch, git_describe)
    )
    if env_has_git:
        if git_commit_short is None and git_commit:
            git_commit_short = git_commit[:12]
    else:
        local = _git_info(base_dir)
        git_commit = local["git_commit"]
        git_commit_short = local["git_commit_short"]
        git_branch = local["git_branch"]
        git_describe = local["git_describe"]
        git_dirty = local["git_dirty"]

    build_id = _clean(environ.get("EMS_BUILD_ID"))
    build_serial = _clean(environ.get("EMS_BUILD_SERIAL"))
    channel = _clean(environ.get("EMS_CHANNEL"))

    build_label = (
        git_describe or release_version or git_commit_short or build_id
    )

    return {
        "ems_version": release_version,
        "release_version": release_version,
        "build_label": build_label,
        "git_commit": git_commit,
        "git_commit_short": git_commit_short,
        "git_branch": git_branch,
        "git_describe": git_describe,
        "git_dirty": git_dirty,
        "build_id": build_id,
        "build_serial": build_serial,
        "channel": channel,
    }
