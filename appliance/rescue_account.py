# SPDX-License-Identifier: AGPL-3.0-or-later
"""The account that gets an operator back in when nothing else does.

Under A/B a failed update rebooted into the other slot. A single-slot appliance
has no such move, and without this it had no login either: no human account,
root locked, ``sulogin`` at rescue.target.

So the package ships a rescue account with a password that is written down. The
consequence is stated once and not argued again: those credentials are public
knowledge, so anything exposing the appliance beyond a private network is a
login for whoever finds it. Changing the password is offered, never demanded --
this module is what lets the console say which state the appliance is in.

Read-only. Nothing here creates or changes an account; the package's
``rescue-account.sh`` owns that, and owns it alone.

See docs/appliance/console-recovery.md.
"""

import functools
from dataclasses import dataclass
from pathlib import Path

from appliance import paths as paths_module

ACCOUNT = "ems-rescue"

# Documented, and deliberately the same as the account name: a password an
# operator has to look up in a panic is one they will not have. Changing it is
# offered by the console and never required.
DEFAULT_PASSWORD = "ems-rescue"

HASH_FILE = "rescue-password.hash"

# Shells that mean "this account cannot be logged into".
NO_LOGIN_SHELLS = ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false")

# What crypt(3) writes in front of a hash to disable it.
LOCK_PREFIXES = ("!", "*")


class RescueAccountError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RescueState:
    """What the console may say about the rescue account.

    ``password_is_default`` is a tri-state on purpose. ``None`` is "this could
    not be read", which is neither of the two answers and must not be rendered
    as either.
    """

    present: bool = False
    password_is_default: object = None
    locked: bool = False
    shell: str = ""
    uid: int = 0
    unreadable: str = ""

    @property
    def can_log_in(self):
        return self.present and not self.locked and self.shell not in NO_LOGIN_SHELLS

    def to_dict(self):
        return {
            "account": ACCOUNT,
            "present": self.present,
            "password_is_default": self.password_is_default,
            "locked": self.locked,
            "can_log_in": self.can_log_in,
            "shell": self.shell,
            "uid": self.uid,
            "unreadable": self.unreadable,
        }


@functools.cache
def default_hash():
    """The shipped hash, from the one file the postinst also reads.

    Declaring it twice would let the console's idea of "still the default"
    drift away from what was actually set.
    """

    target = paths_module.packaged_data(HASH_FILE)
    try:
        text = Path(target).read_text(encoding="utf-8").strip()
    except (OSError, ValueError) as exc:
        raise RescueAccountError(
            "rescue_hash_missing", f"{target} could not be read: {exc}"
        )
    if not text.startswith("$"):
        raise RescueAccountError("rescue_hash_invalid", f"{target} holds no crypt hash")
    return text


def _entry(path, account):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        return None, str(exc)[:200]
    for line in text.splitlines():
        fields = line.split(":")
        if fields and fields[0] == account:
            return fields, ""
    return None, ""


def state(root="/", *, account=ACCOUNT):
    """What this appliance's rescue account is, read from the host's own files."""

    base = Path(root or "/")
    passwd, passwd_error = _entry(base / "etc" / "passwd", account)
    if passwd is None:
        return RescueState(unreadable=passwd_error)

    try:
        uid = int(passwd[2])
    except (IndexError, ValueError):
        uid = 0
    shell = passwd[6] if len(passwd) > 6 else ""

    shadow, shadow_error = _entry(base / "etc" / "shadow", account)
    if shadow is None:
        # Not knowing is its own answer. Reporting "changed" here would tell an
        # operator their appliance is safer than this code can see.
        return RescueState(
            present=True,
            shell=shell,
            uid=uid,
            unreadable=shadow_error or f"{account} has no shadow entry",
        )

    field = shadow[1] if len(shadow) > 1 else ""
    locked = field.startswith(LOCK_PREFIXES)
    stored = field.lstrip("!*")
    try:
        expected = default_hash()
    except RescueAccountError as exc:
        return RescueState(present=True, locked=locked, shell=shell, uid=uid, unreadable=exc.message)

    return RescueState(
        present=True,
        password_is_default=stored == expected,
        locked=locked,
        shell=shell,
        uid=uid,
    )


__all__ = [
    "ACCOUNT",
    "DEFAULT_PASSWORD",
    "RescueAccountError",
    "RescueState",
    "default_hash",
    "state",
]
