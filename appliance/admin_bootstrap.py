# SPDX-License-Identifier: AGPL-3.0-or-later
"""Create the Admin deployment a flashed appliance does not have yet.

The image ships ``/opt/ems-solarflow`` as an empty mount point, so the first
Admin installation has no compose file to edit. This module runs the installer
the package already ships — the same script the recovery documentation names —
and hands the result back to the normal install path, which keeps ownership of
pulling, pinning, starting and verifying the image.

Two things are deliberately not decided here. The script is never asked to
start anything (``--no-start``) and never asked to overwrite anything (no
``--force``): a deployment that already exists belongs to whoever created it.

The owner is read from the deployment root, never looked up by name. On an A/B
appliance ``/etc/passwd`` is slot-local while ``/opt/ems-solarflow`` is shared,
so the account name may resolve to a different uid in the other slot. The
directory and the numeric uid baked into the compose file still agree, and a
name lookup at that point would not.
"""

import os
import pwd
import subprocess

from appliance.paths import package_helper

INSTALLER_NAME = "install-admin-console.sh"

INSTALL_TIMEOUT_SECONDS = 300


class BootstrapError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def installer_path():
    return package_helper(INSTALLER_NAME)


# Written by the appliance before anything is deployed, so its presence is not
# evidence that an installation happened. Named exactly: any other file in a
# root-owned deployment root still refuses adoption.
APPLIANCE_SCAFFOLD_FILES = frozenset({"config/dashboard-auth.json"})


class DeploymentBootstrap:
    """Owner identity and packaged installer for a first Admin deployment."""

    def __init__(
        self,
        paths,
        config,
        *,
        runner=None,
        lookup=None,
        geteuid=None,
        stat=None,
        chown=None,
    ):
        self.paths = paths
        self.config = config
        self._run = runner or subprocess.run
        self._lookup = lookup or pwd.getpwnam
        self._geteuid = geteuid or os.geteuid
        self._stat = stat or os.stat
        self._chown = chown or os.chown

    # --- ownership -------------------------------------------------------

    def identity(self, *, claim=False):
        """Which uid/gid the hosted containers run as, and who owns their files.

        Read-only unless ``claim`` is set. Planning asks what the owner would
        be so it can show it; only the confirmed execution hands the directory
        over. A preview that chowns is not a preview.

        The deployment root is the authority, not a second setting: the compose
        files this project generates bind host paths into the container at the
        same path and run it as that owner, so an identity that disagrees with
        the directory is one that cannot write its own configuration.

        A root-owned root is only adopted while nothing has been installed in
        it — that is a freshly flashed appliance, and taking it over is the
        whole point. A root-owned root that holds an installation belongs to
        whoever made it, and this module does not restructure installations.
        """

        root = self.paths.install_root
        try:
            entry = self._stat(root)
        except OSError:
            raise BootstrapError(
                "deployment_root_missing",
                f"{root} does not exist, so no Admin deployment can be created there",
            )
        if not root.is_dir():
            raise BootstrapError(
                "deployment_root_not_a_directory", f"{root} is not a directory"
            )
        if entry.st_uid != 0:
            return (entry.st_uid, entry.st_gid)

        directories = self._unclaimed_directories(root)
        if directories is None:
            raise BootstrapError(
                "deployment_root_root_owned",
                f"{root} is owned by root and is not an untouched deployment root — it "
                "holds files, or something below it cannot be read; give it a non-root "
                "owner before installing Admin from here",
            )
        return self._adopt(root, directories, claim=claim)

    @staticmethod
    def _unclaimed_directories(root):
        """Every path below an install root nothing was ever installed in.

        A boot scaffolds ``config``, ``data`` and ``backups`` before Admin is
        ever installed, so an empty-directory test on the root alone would
        refuse a perfectly fresh appliance. What no installation can be without
        is a file: a compose file, an environment file or a configuration. The
        first one found ends the walk and the answer is no — as does anything
        that cannot be read or is not a plain directory, because a root this
        cannot see all of is not one to take over.

        One file is scaffolding rather than an installation: the password the
        appliance, the Admin console and the dashboard share. The appliance
        writes it on first boot, before anything is deployed, so treating it as
        evidence of an installation would make setting a password the thing that
        prevents ever installing Admin. It is named exactly, not tolerated as a
        class, and it is handed over with the directories.
        """

        claimed = [root]
        pending = [root]
        while pending:
            current = pending.pop()
            try:
                entries = list(current.iterdir())
            except OSError:
                return None
            for entry in entries:
                if entry.is_dir() and not entry.is_symlink():
                    claimed.append(entry)
                    pending.append(entry)
                    continue
                if entry.is_symlink() or not entry.is_file():
                    return None
                try:
                    relative = entry.relative_to(root).as_posix()
                except ValueError:
                    return None
                if relative not in APPLIANCE_SCAFFOLD_FILES:
                    return None
                claimed.append(entry)
        return claimed

    def _adopt(self, root, directories, *, claim):
        account = self.config.deployment_user
        try:
            entry = self._lookup(account)
        except KeyError:
            raise BootstrapError(
                "deployment_account_missing",
                f"the {account} account does not exist; reinstall the appliance package",
            )
        if entry.pw_uid == 0 or entry.pw_gid == 0:
            raise BootstrapError(
                "deployment_account_privileged",
                f"the {account} account resolves to root; refusing to run the "
                "hosted containers as root",
            )
        if not claim:
            return (entry.pw_uid, entry.pw_gid)
        # The scaffolded directories and the shared password file go with the
        # root. Handing over the root alone would leave the installer unable to
        # write inside its own deployment, and would leave the containers unable
        # to read the password they authenticate against.
        for directory in directories:
            try:
                self._chown(directory, entry.pw_uid, entry.pw_gid)
            except OSError as exc:
                raise BootstrapError(
                    "deployment_root_not_claimable",
                    f"{directory} could not be handed to {account}: {exc}",
                )
        return (entry.pw_uid, entry.pw_gid)

    # --- installer -------------------------------------------------------

    def run(self, *, tag, uid, gid):
        """Write the deployment for ``tag`` and return what was run.

        Privileges are dropped to the deployment owner when there are any to
        drop, so every file the installer creates already belongs to the
        identity the container will run as. Nothing is chowned afterwards:
        a second ownership pass is a second authority.
        """

        helper = installer_path()
        if not helper.is_file():
            raise BootstrapError(
                "installer_missing",
                f"the packaged Admin installer {helper} is not installed",
            )
        command = [
            str(helper),
            "--tag",
            str(tag),
            "--install-dir",
            str(self.paths.install_root),
            "--no-start",
        ]
        privileges = {}
        if self._geteuid() == 0:
            privileges = {"user": uid, "group": gid, "extra_groups": []}
        environment = dict(os.environ)
        environment["PUID"] = str(uid)
        environment["PGID"] = str(gid)
        # /etc/localtime is on the read-only slot root, so the host cannot carry
        # the operator's zone across a slot switch. The containers can, and they
        # are what runs the EMS's local-hour control windows.
        environment["TZ"] = str(getattr(self.config, "timezone", "UTC") or "UTC")
        try:
            completed = self._run(  # noqa: S603 - fixed packaged path, no caller input
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=INSTALL_TIMEOUT_SECONDS,
                env=environment,
                **privileges,
            )
        except subprocess.TimeoutExpired:
            raise BootstrapError(
                "installer_timeout",
                f"the Admin installer did not finish within {INSTALL_TIMEOUT_SECONDS}s",
            )
        except OSError as exc:
            raise BootstrapError("installer_failed", f"the Admin installer could not run: {exc}")

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            raise BootstrapError(
                "installer_failed",
                "the Admin installer failed: " + (detail[-1] if detail else "no output"),
            )
        return {
            "installer": str(helper),
            "tag": str(tag),
            "install_dir": str(self.paths.install_root),
            "uid": int(uid),
            "gid": int(gid),
        }
