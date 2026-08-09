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

STATE_ACTIVE = "active"
STATE_DEGRADED = "degraded"
STATE_UNAVAILABLE = "unavailable"

DISABLED_SUFFIX = ".disabled-by-appliance"
CONFLICT_SUFFIX = ".conflict"
MAX_CONFLICT_FILES = 20

__all__ = [
    "FORCED_COMMAND",
    "OPTION_CHROOT",
    "OPTION_FORCE_COMMAND",
    "REQUIRED_RESTRICTIONS",
    "VERIFIED_OPTIONS",
    "BackupAccessActivation",
    "build_activation",
    "evaluate_policy",
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
        """The keys sshd would accept, and how many this package can attribute."""

        from appliance import backup_ownership
        from appliance.sshkeys import parse_authorized_keys

        path = self.authorized_keys_path()
        text = ""
        if path is not None and path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
        keys = parse_authorized_keys(text)
        return keys, backup_ownership.unmanaged_keys(self.paths, keys)

    def authorized_keys_path(self):
        home = self.account_home()
        return None if home is None else home / ".ssh" / "authorized_keys"

    def disabled_keys_path(self):
        keys = self.authorized_keys_path()
        return None if keys is None else keys.with_name(keys.name + DISABLED_SUFFIX)

    def conflicted_key_files(self):
        """Preserved key files nobody has decided about yet."""

        disabled = self.disabled_keys_path()
        if disabled is None or not disabled.parent.is_dir():
            return []
        prefix = disabled.name + CONFLICT_SUFFIX
        return sorted(
            item for item in disabled.parent.iterdir() if item.name.startswith(prefix)
        )

    def observe(self):
        keys = self.authorized_keys_path()
        disabled = self.disabled_keys_path()
        return {
            "account": self.config.backup_user,
            "home": str(keys.parent.parent) if keys else "",
            "keys_present": bool(keys and keys.is_file()),
            "keys_disabled": bool(disabled and disabled.is_file()),
            "keys_conflicted": [str(item) for item in self.conflicted_key_files()],
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
        """

        ownership = self.ownership()
        if not ownership["owned"]:
            return self._disable_through_account(reason, ownership)

        home = Path(ownership["record"].home)
        keys = home / ".ssh" / "authorized_keys"
        disabled = keys.with_name(keys.name + DISABLED_SUFFIX)
        moved = False
        if keys.is_file():
            keys.replace(disabled if not disabled.exists() else self._free_conflict_path(disabled))
            moved = True
        conflicts = self.conflicted_key_files()
        if disabled.is_file() or conflicts:
            self._expire_account()
        return {
            "state": STATE_DEGRADED,
            "reason": reason,
            "authentication_disabled": True,
            "keys_preserved": disabled.is_file(),
            "keys_conflicted": [str(item) for item in conflicts],
            "changed": moved,
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

    @staticmethod
    def _free_conflict_path(disabled):
        for index in range(1, MAX_CONFLICT_FILES + 1):
            suffix = CONFLICT_SUFFIX if index == 1 else f"{CONFLICT_SUFFIX}.{index}"
            candidate = disabled.with_name(disabled.name + suffix)
            if not candidate.exists():
                return candidate
        raise OSError(f"{disabled.parent} holds too many unresolved key files")

    def restore(self):
        home = self.managed_home()
        if home is None or self.conflicted_key_files():
            return False
        keys = home / ".ssh" / "authorized_keys"
        disabled = keys.with_name(keys.name + DISABLED_SUFFIX)
        try:
            if disabled.is_file() and not keys.is_file():
                disabled.replace(keys)
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

        if self.conflicted_key_files():
            return self._disabled_report("key_conflict", policy=policy, exports=exports)

        if not self.restore():
            return self._disabled_report("key_restore_failed", policy=policy, exports=exports)

        keys, unattributed = self.authorized_keys()
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


def build_activation(*, paths, config, runner=None, probe=None):
    runner = runner or CommandRunner()
    return BackupAccessActivation(runner=runner, config=config, paths=paths, probe=probe)
