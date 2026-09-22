# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one owner of whether the appliance offers a root-capable SSH shell.

``ems-backup`` is confined to SFTP and ``ems-rescue`` is a console account whose
password sshd refuses, so neither answers the question an operator has when an
update stops half way: get me a shell on this box. ``ems-shell`` does, and it
reaches root through ``sudo``.

What that costs is stated rather than implied. The console may deploy a key onto
this account, so whoever reaches the console reaches root on the appliance. The
alternative -- keys only from a root shell already on the box -- was offered and
not chosen; see docs/appliance/ssh-shell-access.md.

Two independent things must be true before a login exists: this flag, and a key.
Both are false when they cannot be read, and this module is the only writer of
the first. The flag lives in root-owned agent state rather than in
``appliance.conf`` because that file belongs to the operator, and an agent that
rewrites it would be a second author of their configuration.

The packaged ``shell-account.sh`` owns the account itself and owns it alone,
exactly as ``rescue-account.sh`` does for the rescue account.
"""

import json
import os
import tempfile
from pathlib import Path

ACCOUNT = "ems-shell"

STATE_NAME = "shell-access.json"
STATE_SCHEMA_VERSION = 1
STATE_MODE = 0o600
STATE_DIR_MODE = 0o700

# Written by the packaged account script and named here so the script, the
# documentation and the tests cannot drift apart about where it lives. The
# account has no password at all, so reaching root needs NOPASSWD -- a password
# prompt no key holder can answer is not a safety property, it is a broken
# account.
SUDOERS_PATH = "/etc/sudoers.d/ems-shell"

SHELL_PATH = "/bin/bash"

# The agent writes authorized_keys and runs under ProtectHome=yes, which gives
# it an empty read-only tmpfs where /home would be. A home under there cannot
# be written from the one process that has to write it, so the account is
# unusable no matter what the console reports. 0.3.3 and 0.3.4 shipped exactly
# that, and it failed on a live appliance with EROFS rather than at any gate.
UNWRITABLE_HOME_PREFIX = "/home/"


def home_is_writable(home) -> bool:
    """Whether the agent could write this account's authorized_keys."""

    text = str(home or "")
    return bool(text) and not text.startswith(UNWRITABLE_HOME_PREFIX)


def state_path(paths):
    return Path(paths.agent_state_dir) / STATE_NAME


def enabled(paths) -> bool:
    """Whether sshd should admit a key for this account. False unless proven."""

    try:
        raw = state_path(paths).read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        payload = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("schema_version") != STATE_SCHEMA_VERSION:
        return False
    return payload.get("enabled") is True


def set_enabled(paths, value: bool) -> bool:
    """Record the operator's decision. Replaced atomically, never appended to."""

    target = state_path(paths)
    target.parent.mkdir(parents=True, exist_ok=True, mode=STATE_DIR_MODE)
    payload = {"schema_version": STATE_SCHEMA_VERSION, "enabled": bool(value)}
    handle, staging = tempfile.mkstemp(dir=str(target.parent), prefix=f".{STATE_NAME}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, sort_keys=True) + "\n")
        os.chmod(staging, STATE_MODE)
        os.replace(staging, target)
    except OSError:
        try:
            os.unlink(staging)
        except OSError:
            pass
        raise
    return bool(value)


def apply(paths, config, *, value, activation):
    """Move the flag and the sshd policy together, or move neither.

    The policy is rendered from the flag, so a flag that survived a rolled-back
    transaction would leave the recorded state and the running daemon
    disagreeing -- the exact drift the host-configuration transaction exists to
    refuse. Both the console operation and the CLI verb come through here so
    there is one way the two halves can move.

    Imported inside the call because host_config reads this module to render
    the block it is about to write.
    """

    from appliance.host_config import apply_host_config

    previous = enabled(paths)
    set_enabled(paths, value)
    try:
        return apply_host_config(paths, config, activation=activation)
    except Exception:
        set_enabled(paths, previous)
        raise


__all__ = [
    "ACCOUNT",
    "apply",
    "SHELL_PATH",
    "STATE_NAME",
    "STATE_SCHEMA_VERSION",
    "SUDOERS_PATH",
    "enabled",
    "home_is_writable",
    "set_enabled",
    "state_path",
]
