# SPDX-License-Identifier: AGPL-3.0-or-later
"""Auto-drop root for ``emsctl.py`` inside the official EMS Docker container.

The container entrypoint drops privileges for the main EMS process, but
``docker compose exec ems python3 emsctl.py ...`` bypasses that path and would
otherwise run as root, creating root-owned files in the ``/app/config`` and
``/app/data`` bind mounts that the host user cannot delete.

UID/GID resolution mirrors ``docker-entrypoint.sh`` (``select_runtime_ids``):
explicit ``PUID``/``PGID`` first, then the owner of ``/app/data``, then the
owner of ``/app/config``, then the baked-in ``ems`` user. Resolution is kept in
a pure helper so it can be tested without root.
"""

import os

GUARD_ENV = "EMS_CLI_PRIVILEGE_DROPPED"
DATA_DIR = "/app/data"
CONFIG_DIR = "/app/config"
RUN_AS_USER = "ems"

_TRUTHY = ("1", "true", "yes")


def _positive_int(value):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _path_owner(path):
    try:
        info = os.stat(path)
    except OSError:
        return None, None
    return info.st_uid, info.st_gid


def _run_as_user_ids(run_as_user):
    try:
        import pwd

        entry = pwd.getpwnam(run_as_user)
    except (KeyError, ImportError):
        return None
    uid = _positive_int(entry.pw_uid)
    gid = _positive_int(entry.pw_gid)
    if uid is None or gid is None:
        return None
    return uid, gid


def resolve_runtime_ids(
    environ=None,
    data_dir=DATA_DIR,
    config_dir=CONFIG_DIR,
    run_as_user=RUN_AS_USER,
):
    """Resolve the target ``(uid, gid)`` to run as, or ``None`` if undecidable.

    Mirrors the entrypoint priority order. Invalid ``PUID``/``PGID`` values do
    not abort here (unlike the entrypoint, which refuses to start); a CLI
    invocation falls back to path-owner resolution so read-only commands keep
    working.
    """
    environ = os.environ if environ is None else environ

    puid = environ.get("PUID")
    pgid = environ.get("PGID")
    if puid is not None or pgid is not None:
        uid = _positive_int(puid)
        gid = _positive_int(pgid)
        if uid is not None and gid is not None:
            return uid, gid

    data_uid, data_gid = _path_owner(data_dir)
    uid = _positive_int(data_uid)
    gid = _positive_int(data_gid)
    if uid is not None and gid is not None:
        return uid, gid

    config_uid, config_gid = _path_owner(config_dir)
    uid = _positive_int(config_uid)
    gid = _positive_int(config_gid)
    if uid is not None and gid is not None:
        return uid, gid

    return _run_as_user_ids(run_as_user)


def _in_official_container(environ):
    flag = str(environ.get("EMS_IN_CONTAINER", "")).strip().lower()
    return flag in _TRUTHY


def maybe_drop_privileges(environ=None):
    """Drop to the runtime user when running as root in the official container.

    Returns the ``(uid, gid)`` dropped to, or ``None`` when no drop happens
    (native install, already non-root, guard already set, or no usable target).
    """
    environ = os.environ if environ is None else environ

    if environ.get(GUARD_ENV):
        return None
    if not _in_official_container(environ):
        return None
    if getattr(os, "geteuid", None) is None or os.geteuid() != 0:
        return None

    target = resolve_runtime_ids(environ)
    if target is None:
        return None
    uid, gid = target
    if uid == 0 or gid == 0:
        return None

    environ[GUARD_ENV] = "1"
    os.environ[GUARD_ENV] = "1"

    try:
        os.setgroups([gid])
    except OSError:
        pass
    os.setgid(gid)
    os.setuid(uid)
    return uid, gid
