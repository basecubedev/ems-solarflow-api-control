# SPDX-License-Identifier: AGPL-3.0-or-later
"""Activating and revoking the confined SFTP backup account.

The packaged sshd drop-in is only a file. What confines the account is the
policy the running daemon applies to it, and that policy exists only while the
package is installed. Two consequences are implemented here.

First, activation is fail-closed: the account's authentication is enabled only
after ``sshd -t`` accepted the configuration, the daemon reloaded it, and the
*effective* policy for the backup user was read back and matched against every
restriction the appliance promises. Any gap disables the account's key file
instead of leaving it usable behind a "degraded" label.

Second, the same mechanism is what package removal uses: with the drop-in gone
a surviving key would open an unconfined SFTP session over the whole host, so
the key file is moved aside before the package is.
"""

import stat
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from appliance.commands import CommandRunner
from appliance.export_state import inspect_exports
from appliance.hostprobe import HostProbe
from appliance.ssh_policy import (
    FORCED_COMMAND,
    OPTION_CHROOT,
    OPTION_FORCE_COMMAND,
    REQUIRED_RESTRICTIONS,
    VERIFIED_OPTIONS,
    evaluate_policy,
    read_effective_policy,
)
from appliance.ssh_service import parse_passwd_entry
from appliance.systemd import UNIT_SSH, SystemdBackend
from appliance.validation import ValidationError

STATE_ACTIVE = "active"
STATE_DEGRADED = "degraded"
STATE_UNAVAILABLE = "unavailable"

DISABLED_SUFFIX = ".disabled-by-appliance"
CONFLICT_SUFFIX = ".conflict"
MAX_CONFLICT_FILES = 20

@dataclass(frozen=True)
class KeyFiles:
    """The backup account's key files, each as the entry it is.

    ``active`` and ``disabled`` hold only for a regular file; ``unsafe`` names
    the key file or the preserved one where any other entry stands, a symlink
    included. ``set_aside`` is whether anything stands at the preserved name or
    a conflict name. Behind a symlinked ``.ssh`` nothing is read, and
    ``refusal`` is the store's ``ValidationError``.
    """

    ssh_dir: Path = None
    active: bool = False
    disabled: bool = False
    set_aside: bool = False
    conflicted: tuple = ()
    unsafe: tuple = ()
    refusal: ValidationError = None


def read_key_files(home):
    """The key files under ``home``, listed through ``AuthorizedKeysStore.entry_modes``."""

    from appliance.sshkeys import AuthorizedKeysStore

    if not home:
        return KeyFiles()
    store = AuthorizedKeysStore(home)
    try:
        modes = store.entry_modes()
    except ValidationError as refusal:
        return KeyFiles(ssh_dir=store.ssh_dir, refusal=refusal)
    active = store.path.name
    preserved = active + DISABLED_SUFFIX
    regular = {name for name, mode in modes.items() if stat.S_ISREG(mode)}
    conflicted = tuple(name for name in modes if name.startswith(preserved + CONFLICT_SUFFIX))
    return KeyFiles(
        ssh_dir=store.ssh_dir,
        active=active in regular,
        disabled=preserved in regular,
        set_aside=preserved in modes or bool(conflicted),
        conflicted=conflicted,
        unsafe=tuple(name for name in (active, preserved) if name in modes and name not in regular),
    )


__all__ = [
    "FORCED_COMMAND",
    "OPTION_CHROOT",
    "OPTION_FORCE_COMMAND",
    "REQUIRED_RESTRICTIONS",
    "VERIFIED_OPTIONS",
    "BackupAccessActivation",
    "KeyFiles",
    "build_activation",
    "evaluate_policy",
    "read_key_files",
]


class BackupAccessActivation:
    """Enable the backup account only while its confinement is proven."""

    def __init__(self, *, runner, config, paths, systemd=None, probe=None, time_fn=None):
        self.runner = runner
        self.config = config
        self.paths = paths
        self.systemd = systemd or SystemdBackend(runner)
        self.probe = probe or HostProbe(runner)

    # --- the account ------------------------------------------------------

    def account(self):
        if not self.runner.available("getent"):
            return parse_passwd_entry(self.config.backup_user, "")
        result = self.runner.run("getent", ["passwd", self.config.backup_user], timeout=15)
        return parse_passwd_entry(self.config.backup_user, result.stdout if result.ok else "")

    def account_home(self):
        account = self.account()
        return Path(account.home) if account.exists and account.home else None

    def _passwd_entry(self):
        account = self.account()
        if not account.exists:
            return None
        return SimpleNamespace(pw_uid=account.uid, pw_gid=account.gid, pw_dir=str(account.home))

    def ownership(self):
        """Is the live account, *and its home*, the exact pair this package created?"""

        from appliance import backup_ownership

        return backup_ownership.verify_ownership(
            self.paths, self.config.backup_user, entry=self._passwd_entry()
        )

    def account_ownership(self):
        """The account half alone: what a fail-closed step may still act through."""

        from appliance import backup_ownership

        return backup_ownership.verify_account(
            self.paths, self.config.backup_user, entry=self._passwd_entry()
        )

    def managed_home(self):
        """The home this package can prove it created, or ``None``.

        Every mutation below is bound to this and never to the passwd entry: an
        account whose home was replaced still points at a real directory, and
        that directory is the operator's.
        """

        verdict = self.ownership()
        return Path(verdict["record"].home) if verdict["owned"] else None

    def authorized_keys(self):
        """The keys sshd would accept, and how many this package can attribute.

        Read through ``AuthorizedKeysStore``, which never follows a symlinked
        ``.ssh`` or key file; its ``ValidationError`` reaches the caller.
        """

        from appliance import backup_ownership
        from appliance.sshkeys import (
            AuthorizedKeysStore,
            foreign_key_lines,
            parse_authorized_keys,
        )

        home = self.account_home()
        text = AuthorizedKeysStore(home).text() if home is not None else ""
        keys = parse_authorized_keys(text)
        unattributed = backup_ownership.unmanaged_keys(self.paths, keys)
        # A line this parser cannot read is key material all the same, and sshd
        # honours it. Counting only parsed keys let exactly the material this
        # gate exists to catch pass as "everything attributed".
        return keys, unattributed + foreign_key_lines(text)

    def authorized_keys_path(self):
        home = self.account_home()
        return None if home is None else home / ".ssh" / "authorized_keys"

    def disabled_keys_path(self):
        keys = self.authorized_keys_path()
        return None if keys is None else keys.with_name(keys.name + DISABLED_SUFFIX)

    def key_files(self):
        """The account's key files, read by ``read_key_files`` without following a symlink."""

        return read_key_files(self.account_home())

    def conflicted_key_files(self):
        """Preserved key files nobody has decided about yet.

        Read by ``read_key_files``; a symlinked ``.ssh`` raises its
        ``ValidationError`` to the caller.
        """

        files = self.key_files()
        if files.refusal is not None:
            raise files.refusal
        return [files.ssh_dir / name for name in files.conflicted]

    def observe(self):
        home = self.account_home()
        files = read_key_files(home)
        return {
            "account": self.config.backup_user,
            "home": str(home) if home is not None else "",
            "keys_present": files.active,
            "keys_disabled": files.disabled,
            "keys_conflicted": [str(files.ssh_dir / name) for name in files.conflicted],
            "keys_refused": files.refusal.code if files.refusal is not None else "",
            "openssh_installed": self.runner.available("sshd"),
        }

    def export_state(self):
        try:
            mounts = self.probe.mount_records()
        except Exception:
            mounts = {}
        return inspect_exports(self.paths, mounts=mounts)

    # --- fail-closed transitions ------------------------------------------

    def disable(self, *, reason=""):
        """Withdraw authentication, without touching state this package cannot claim.

        Two identities decide what may happen here. With the exact package-owned
        account *and* the exact package-owned home, the key file is moved out of
        sshd's reach and preserved. With the account but a home this package
        cannot prove it created, the key file in that home is the operator's:
        it is not read, moved, renamed or rewritten, and authentication is
        withdrawn through the account itself instead. Without even the account,
        nothing on the host is this package's to change.

        An account with no authorized key has no access to revoke, so the
        account itself is only expired once there is key material that an
        unconfined daemon could accept. A key file that appeared next to an
        already preserved one is a conflict only an operator can resolve, so
        both are kept and authentication stays off.

        The key file is moved through ``AuthorizedKeysStore``, which never
        follows a symlinked ``.ssh``; with one, nothing is moved, the account
        is expired, and ``keys_refused`` names why.
        """

        from appliance.sshkeys import AuthorizedKeysStore

        ownership = self.ownership()
        if not ownership["owned"]:
            return self._disable_through_account(reason, ownership)

        store = AuthorizedKeysStore(ownership["record"].home)
        preserved_name = store.path.name + DISABLED_SUFFIX
        try:
            moved = store.set_aside(_preserved_names(preserved_name)) is not None
            entries = store.entries()
        except ValidationError as refused:
            return self._disable_unsafe_keys(reason, ownership, refused.code)
        preserved = preserved_name in entries
        conflicts = [
            store.ssh_dir / name
            for name in entries
            if name.startswith(preserved_name + CONFLICT_SUFFIX)
        ]
        if preserved or conflicts:
            self._expire_account()
        return {
            "state": STATE_DEGRADED,
            "reason": reason,
            "authentication_disabled": True,
            "keys_preserved": preserved,
            "keys_conflicted": [str(item) for item in conflicts],
            "changed": moved,
            "ownership": ownership["reason"],
            "home_owned": True,
            "operator_state_untouched": True,
        }

    def _disable_unsafe_keys(self, reason, ownership, refused):
        """Fail closed past a key directory nothing may be moved through: expire the account."""

        return {
            "state": STATE_DEGRADED,
            "reason": reason,
            "authentication_disabled": self._expire_account(),
            "keys_preserved": False,
            "keys_conflicted": [],
            "keys_refused": refused,
            "changed": False,
            "ownership": ownership["reason"],
            "home_owned": True,
            "operator_state_untouched": True,
        }

    def _disable_through_account(self, reason, ownership):
        """Fail closed without a provable home: expire the account, change nothing else."""

        account = self.account_ownership()
        expired = self._expire_account() if account["owned"] else False
        return {
            "state": STATE_DEGRADED,
            "reason": reason,
            "authentication_disabled": expired,
            "keys_preserved": False,
            "keys_conflicted": [],
            "changed": False,
            "ownership": ownership["reason"],
            "account_owned": bool(account["owned"]),
            "home_owned": False,
            "operator_state_untouched": True,
        }

    def restore(self):
        """Put the preserved key file back, then lift the account's expiry.

        Moved through ``AuthorizedKeysStore``: a symlinked ``.ssh``, key file
        or preserved key file raises its ``ValidationError`` before the account
        is un-expired.
        """

        from appliance.sshkeys import AuthorizedKeysStore

        home = self.managed_home()
        if home is None:
            return False
        store = AuthorizedKeysStore(home)
        preserved_name = store.path.name + DISABLED_SUFFIX
        if any(name.startswith(preserved_name + CONFLICT_SUFFIX) for name in store.entries()):
            return False
        try:
            store.take_back(preserved_name)
        except OSError:
            return False
        return self._unexpire_account()

    def _expire_account(self):
        # Defence in depth: even with a key file restored by hand, an expired
        # account cannot open a session at all.
        if not self.runner.available("chage"):
            return False
        return bool(self.runner.run("chage", ["-E", "1", self.config.backup_user], timeout=30).ok)

    def _unexpire_account(self):
        if not self.runner.available("chage"):
            return True
        return bool(self.runner.run("chage", ["-E", "-1", self.config.backup_user], timeout=30).ok)

    # --- activation -------------------------------------------------------

    def effective_policy(self):
        return read_effective_policy(
            self.runner, user=self.config.backup_user, export_root=self.paths.export_root
        )

    def activate(self):
        """Verify the confinement, then enable or disable the account by result.

        The sshd policy alone is not the boundary: the account is chrooted into
        the export root, so an unmanaged entry there, a missing bind or a mount
        that publishes something other than the configured EMS directory would
        widen what a restored key can reach.
        """

        if not self.runner.available("sshd"):
            return self._report(
                STATE_UNAVAILABLE,
                "openssh_not_installed",
                policy=evaluate_policy({}, export_root=self.paths.export_root),
            )

        ownership = self.ownership()
        if not ownership["owned"]:
            # Without the exact package-owned account there is nothing this
            # appliance may confine, and nothing it may hand a key to.
            reason = (
                "backup_account_missing"
                if ownership["reason"] == "account_missing"
                else f"backup_account_{ownership['reason']}"
            )
            state = (
                STATE_UNAVAILABLE if ownership["reason"] == "account_missing" else STATE_DEGRADED
            )
            if state == STATE_DEGRADED:
                return self._disabled_report(reason)
            return self._report(state, reason)

        if not self.runner.run("sshd", ["-t"], timeout=30).ok:
            return self._disabled_report("sshd_config_invalid")

        if not self._reload():
            return self._disabled_report("sshd_reload_failed")

        policy = self.effective_policy()
        if not policy["confirmed"]:
            return self._disabled_report("confinement_not_confirmed", policy=policy)

        exports = self.export_state()
        if not exports["exact"]:
            reason = "exports_not_confined"
            if exports.get("boundary_problems"):
                reason = "path_boundary_violation"
            elif exports["unmanaged"]:
                reason = "export_root_not_exclusive"
            return self._disabled_report(reason, policy=policy, exports=exports)

        try:
            conflicted = self.conflicted_key_files()
        except ValidationError as refused:
            return self._disabled_report(refused.code, policy=policy, exports=exports)
        if conflicted:
            return self._disabled_report("key_conflict", policy=policy, exports=exports)

        try:
            restored = self.restore()
        except ValidationError as refused:
            return self._disabled_report(refused.code, policy=policy, exports=exports)
        if not restored:
            return self._disabled_report("key_restore_failed", policy=policy, exports=exports)

        try:
            keys, unattributed = self.authorized_keys()
        except ValidationError as refused:
            return self._disabled_report(refused.code, policy=policy, exports=exports)
        if unattributed:
            return self._disabled_report(
                "key_attribution_unknown", policy=policy, exports=exports
            )
        if not keys:
            return self._report(
                STATE_UNAVAILABLE, "no_authorized_key", policy=policy, exports=exports
            )
        return self._report(STATE_ACTIVE, "", policy=policy, exports=exports)

    def _reload(self):
        """Reload only a daemon that is running.

        A daemon that is not running cannot be applying an older policy, and it
        reads the packaged drop-in when it starts. A reload that fails while it
        *is* running is the dangerous case, and that is the one that fails.
        """

        try:
            if not self.systemd.unit_state(UNIT_SSH)["running"]:
                return True
            return bool(self.systemd.reload(UNIT_SSH).ok)
        except Exception:
            return False

    def _disabled_report(self, reason, *, policy=None, exports=None):
        outcome = self.disable(reason=reason)
        return self._report(
            STATE_DEGRADED, reason, policy=policy, exports=exports, disabled=outcome
        )

    def _report(self, state, reason, *, policy=None, exports=None, disabled=None):
        observed = self.observe()
        preserved = observed["keys_disabled"] or bool(observed["keys_conflicted"])
        # What the transition actually achieved outranks what the key files look
        # like: a replacement home keeps its own key file, and authentication was
        # withdrawn through the account instead.
        withdrawn = (
            bool(disabled["authentication_disabled"])
            if disabled is not None
            else (not observed["keys_present"] and preserved)
        )
        return {
            "state": state,
            "reason": reason,
            "account": self.config.backup_user,
            "authentication_disabled": withdrawn,
            "keys_present": observed["keys_present"],
            "keys_preserved": preserved,
            "keys_conflicted": observed["keys_conflicted"],
            "operator_state_untouched": bool((disabled or {}).get("operator_state_untouched", True)),
            "home_owned": bool((disabled or {}).get("home_owned", self.managed_home() is not None)),
            "policy": policy
            or evaluate_policy({}, export_root=self.paths.export_root),
            "exports": exports if exports is not None else self.export_state(),
        }


def _preserved_names(preserved):
    """Where a key file is set aside: the preserved name, then its conflict names."""

    conflicts = (
        preserved + (CONFLICT_SUFFIX if index == 1 else f"{CONFLICT_SUFFIX}.{index}")
        for index in range(1, MAX_CONFLICT_FILES + 1)
    )
    return (preserved, *conflicts)


def build_activation(*, paths, config, runner=None, probe=None):
    runner = runner or CommandRunner()
    return BackupAccessActivation(runner=runner, config=config, paths=paths, probe=probe)
