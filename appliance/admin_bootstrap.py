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

The owner is read from the deployment root, never looked up by name. A name can
be re-created with a different uid, while the directory and the numeric uid
baked into the compose file still agree -- and a name lookup at that point would
not.
"""

import os
import pwd
import subprocess
from pathlib import PurePosixPath

from appliance.auth import lock_path
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


# Written by the appliance before anything is deployed, so their presence is not
# evidence that an installation happened. Named exactly: any other file in a
# root-owned deployment root still refuses adoption.
#
# The lock is derived rather than spelled, because it is the store's artifact
# and not this module's fact. Listing only the password was enough to make
# setting one the thing that prevented ever installing Admin: the store takes a
# lock beside the record, the lock outlives the write, and the walk counted it
# as somebody else's installation.
SHARED_PASSWORD_FILE = PurePosixPath("config/dashboard-auth.json")
APPLIANCE_SCAFFOLD_FILES = frozenset(
    {SHARED_PASSWORD_FILE.as_posix(), lock_path(SHARED_PASSWORD_FILE).as_posix()}
)


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

        return self._adopt(root, self._unclaimed_directories(root), claim=claim)

    @staticmethod
    def _not_untouched(root, detail):
        """The refusal, naming the one thing that produced it.

        It used to list the possible causes instead -- "it holds files, or
        something below it cannot be read" -- which leaves an operator with
        three hypotheses, no path, and a walk that knew the answer and threw it
        away. The code stays: what changed is that the message can be acted on.
        """

        return BootstrapError(
            "deployment_root_root_owned",
            f"{root} is owned by root and is not an untouched deployment root: "
            f"{detail}; give it a non-root owner before installing Admin from here",
        )

    @staticmethod
    def _unclaimed_directories(root):
        """Every path below an install root nothing was ever installed in.

        A boot scaffolds ``config``, ``data`` and ``backups`` before Admin is
        ever installed, so an empty-directory test on the root alone would
        refuse a perfectly fresh appliance. What no installation can be without
        is a file: a compose file, an environment file or a configuration. The
        first one found ends the walk and refuses, naming it — as does anything
        that cannot be read or is not a plain directory, because a root this
        cannot see all of is not one to take over.

        Two files are scaffolding rather than an installation: the password the
        appliance, the Admin console and the dashboard share, and the lock its
        store holds while writing it. The appliance writes both on first boot,
        before anything is deployed, so treating either as evidence of an
        installation makes setting a password the thing that prevents ever
        installing Admin. They are named exactly, not tolerated as a class, and
        they are handed over with the directories.
        """

        claimed = [root]
        pending = [root]
        while pending:
            current = pending.pop()
            try:
                entries = list(current.iterdir())
            except OSError as exc:
                raise DeploymentBootstrap._not_untouched(
                    root, f"{current} could not be read ({exc.__class__.__name__})"
                ) from exc
            for entry in entries:
                if entry.is_dir() and not entry.is_symlink():
                    claimed.append(entry)
                    pending.append(entry)
                    continue
                if entry.is_symlink():
                    raise DeploymentBootstrap._not_untouched(
                        root, f"{entry} is a symbolic link"
                    )
                if not entry.is_file():
                    raise DeploymentBootstrap._not_untouched(
                        root, f"{entry} is neither a regular file nor a directory"
                    )
                try:
                    relative = entry.relative_to(root).as_posix()
                except ValueError:
                    raise DeploymentBootstrap._not_untouched(
                        root, f"{entry} is not inside the deployment root"
                    ) from None
                if relative not in APPLIANCE_SCAFFOLD_FILES:
                    raise DeploymentBootstrap._not_untouched(
                        root,
                        f"{entry} is a file this appliance did not put there "
                        f"(only {', '.join(sorted(APPLIANCE_SCAFFOLD_FILES))} is expected)",
                    )
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
        # The host stays on a deterministic UTC; the containers are what run
        # the EMS's local-hour control windows, so the zone is carried into
        # them instead.
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
