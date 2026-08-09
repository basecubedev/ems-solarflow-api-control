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

from appliance.commands import CommandRunner
from appliance.export_state import inspect_exports
from appliance.hostprobe import HostProbe
from appliance.ssh_service import parse_passwd_entry, parse_sshd_config
from appliance.systemd import UNIT_SSH, SystemdBackend

STATE_ACTIVE = "active"
STATE_DEGRADED = "degraded"
STATE_UNAVAILABLE = "unavailable"

DISABLED_SUFFIX = ".disabled-by-appliance"
CONFLICT_SUFFIX = ".conflict"
MAX_CONFLICT_FILES = 20

OPTION_CHROOT = "chrootdirectory"
OPTION_FORCE_COMMAND = "forcecommand"
FORCED_COMMAND = "internal-sftp"

# Every restriction the appliance tells an operator is in force. Reporting a
# subset as "confined" would be a claim the appliance never checked.
REQUIRED_RESTRICTIONS = (
    ("passwordauthentication", "no"),
    ("kbdinteractiveauthentication", "no"),
    ("pubkeyauthentication", "yes"),
    ("permittty", "no"),
    ("allowtcpforwarding", "no"),
    ("allowagentforwarding", "no"),
    ("x11forwarding", "no"),
    ("permittunnel", "no"),
    ("gatewayports", "no"),
)

VERIFIED_OPTIONS = (OPTION_CHROOT, OPTION_FORCE_COMMAND) + tuple(
    option for option, _ in REQUIRED_RESTRICTIONS
)


def evaluate_policy(effective, *, export_root):
    """Compare the effective sshd policy for the backup user with the promise."""

    effective = effective or {}
    restrictions = {}

    chroot = str(effective.get(OPTION_CHROOT, ""))
    restrictions[OPTION_CHROOT] = {
        "value": chroot,
        "expected": str(export_root),
        "confirmed": bool(chroot) and chroot == str(export_root),
    }

    forced = str(effective.get(OPTION_FORCE_COMMAND, ""))
    restrictions[OPTION_FORCE_COMMAND] = {
        "value": forced,
        "expected": FORCED_COMMAND,
        "confirmed": forced.startswith(FORCED_COMMAND),
    }

    for option, expected in REQUIRED_RESTRICTIONS:
        actual = str(effective.get(option, ""))
        restrictions[option] = {
            "value": actual,
            "expected": expected,
            "confirmed": actual.lower() == expected,
        }

    violations = [name for name in VERIFIED_OPTIONS if not restrictions[name]["confirmed"]]
    return {
        "available": bool(effective),
        "confirmed": bool(effective) and not violations,
        "restrictions": restrictions,
        "violations": violations,
    }


class BackupAccessActivation:
    """Enable the backup account only while its confinement is proven."""

    def __init__(self, *, runner, config, paths, systemd=None, probe=None, time_fn=None):
        self.runner = runner
        self.config = config
        self.paths = paths
        self.systemd = systemd or SystemdBackend(runner)
        self.probe = probe or HostProbe(runner)

    # --- the account ------------------------------------------------------

    def account_home(self):
        if not self.runner.available("getent"):
            return None
        result = self.runner.run("getent", ["passwd", self.config.backup_user], timeout=15)
        account = parse_passwd_entry(self.config.backup_user, result.stdout if result.ok else "")
        return Path(account.home) if account.exists and account.home else None

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
        """Move the key file out of sshd's reach; the material is preserved.

        An account with no authorized key has no access to revoke, so the
        account itself is only expired once there is key material that an
        unconfined daemon could accept. A key file that appeared next to an
        already preserved one is a conflict only an operator can resolve, so
        both are kept and authentication stays off.
        """

        keys = self.authorized_keys_path()
        disabled = self.disabled_keys_path()
        moved = False
        if keys is not None and keys.is_file():
            keys.replace(disabled if not disabled.exists() else self._free_conflict_path(disabled))
            moved = True
        conflicts = self.conflicted_key_files()
        if (disabled is not None and disabled.is_file()) or conflicts:
            self._expire_account()
        return {
            "state": STATE_DEGRADED,
            "reason": reason,
            "authentication_disabled": True,
            "keys_preserved": bool(disabled and disabled.is_file()),
            "keys_conflicted": [str(item) for item in conflicts],
            "changed": moved,
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
        keys = self.authorized_keys_path()
        disabled = self.disabled_keys_path()
        if keys is None or self.conflicted_key_files():
            return False
        if disabled.is_file() and not keys.is_file():
            disabled.replace(keys)
        self._unexpire_account()
        return True

    def _expire_account(self):
        # Defence in depth: even with a key file restored by hand, an expired
        # account cannot open a session at all.
        if self.runner.available("chage"):
            self.runner.run("chage", ["-E", "1", self.config.backup_user], timeout=30)

    def _unexpire_account(self):
        if self.runner.available("chage"):
            self.runner.run("chage", ["-E", "-1", self.config.backup_user], timeout=30)

    # --- activation -------------------------------------------------------

    def effective_policy(self):
        if not self.runner.available("sshd"):
            return evaluate_policy({}, export_root=self.paths.export_root)
        result = self.runner.run(
            "sshd",
            ["-T", "-C", f"user={self.config.backup_user},host=localhost,addr=127.0.0.1"],
            timeout=20,
        )
        effective = parse_sshd_config(result.stdout) if result.ok else {}
        return evaluate_policy(effective, export_root=self.paths.export_root)

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

        if not self.runner.run("sshd", ["-t"], timeout=30).ok:
            return self._disabled_report("sshd_config_invalid")

        if not self._reload():
            return self._disabled_report("sshd_reload_failed")

        policy = self.effective_policy()
        if not policy["confirmed"]:
            return self._disabled_report("confinement_not_confirmed", policy=policy)

        exports = self.export_state()
        if not exports["exact"]:
            reason = (
                "export_root_not_exclusive" if exports["unmanaged"] else "exports_not_confined"
            )
            return self._disabled_report(reason, policy=policy, exports=exports)

        if self.conflicted_key_files():
            return self._disabled_report("key_conflict", policy=policy, exports=exports)

        self.restore()
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
        self.disable(reason=reason)
        return self._report(STATE_DEGRADED, reason, policy=policy, exports=exports)

    def _report(self, state, reason, *, policy=None, exports=None):
        observed = self.observe()
        preserved = observed["keys_disabled"] or bool(observed["keys_conflicted"])
        return {
            "state": state,
            "reason": reason,
            "account": self.config.backup_user,
            "authentication_disabled": not observed["keys_present"] and preserved,
            "keys_present": observed["keys_present"],
            "keys_preserved": preserved,
            "keys_conflicted": observed["keys_conflicted"],
            "policy": policy
            or evaluate_policy({}, export_root=self.paths.export_root),
            "exports": exports if exports is not None else self.export_state(),
        }


def build_activation(*, paths, config, runner=None, probe=None):
    runner = runner or CommandRunner()
    return BackupAccessActivation(runner=runner, config=config, paths=paths, probe=probe)
