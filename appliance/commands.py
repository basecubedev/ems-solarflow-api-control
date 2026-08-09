# SPDX-License-Identifier: AGPL-3.0-or-later
"""The only place in the Appliance Manager that starts a host process.

Callers name a tool from a fixed allowlist and pass already-validated argument
strings. There is no shell, no PATH lookup of a caller-supplied name and no way
to express a command as one string, so a request cannot smuggle an executable
or a shell metacharacter into the host.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass

DEFAULT_TIMEOUT = 60

EXECUTABLES = {
    "docker": ("/usr/bin/docker", "/usr/local/bin/docker"),
    "systemctl": ("/usr/bin/systemctl", "/bin/systemctl"),
    "journalctl": ("/usr/bin/journalctl", "/bin/journalctl"),
    "apt-get": ("/usr/bin/apt-get",),
    "apt": ("/usr/bin/apt",),
    "dpkg": ("/usr/bin/dpkg",),
    "dpkg-query": ("/usr/bin/dpkg-query",),
    "nmcli": ("/usr/bin/nmcli",),
    "hostnamectl": ("/usr/bin/hostnamectl", "/bin/hostnamectl"),
    "timedatectl": ("/usr/bin/timedatectl", "/bin/timedatectl"),
    "ssh-keygen": ("/usr/bin/ssh-keygen",),
    "sshd": ("/usr/sbin/sshd",),
    "getent": ("/usr/bin/getent", "/bin/getent"),
    "chage": ("/usr/bin/chage",),
    "ss": ("/usr/bin/ss", "/bin/ss"),
    "avahi-resolve": ("/usr/bin/avahi-resolve",),
    # A/B layout discovery and the inactive-slot write. Every path these are
    # given comes from the root-owned layout manifest, never from a request.
    "lsblk": ("/usr/bin/lsblk", "/bin/lsblk"),
    "blkid": ("/usr/sbin/blkid", "/sbin/blkid"),
    "findmnt": ("/usr/bin/findmnt", "/bin/findmnt"),
    "mount": ("/usr/bin/mount", "/bin/mount"),
    "umount": ("/usr/bin/umount", "/bin/umount"),
    "fsck.vfat": ("/usr/sbin/fsck.vfat", "/sbin/fsck.vfat"),
    "e2fsck": ("/usr/sbin/e2fsck", "/sbin/e2fsck"),
    "blockdev": ("/usr/sbin/blockdev", "/sbin/blockdev"),
    # Detached-signature verification of an OS release manifest, against a
    # root-owned keyring named by the host configuration.
    "gpg": ("/usr/bin/gpg", "/usr/bin/gpgv"),
}


class CommandError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CommandResult:
    tool: str
    args: tuple
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self):
        return self.returncode == 0 and not self.timed_out


class CommandRunner:
    """Run an allowlisted host tool with validated arguments."""

    def __init__(self, *, executables=None, default_timeout=DEFAULT_TIMEOUT, env=None):
        self.executables = dict(executables or EXECUTABLES)
        self.default_timeout = default_timeout
        self.env = dict(
            env
            or {
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                "LC_ALL": "C",
                "DEBIAN_FRONTEND": "noninteractive",
            }
        )

    def resolve(self, tool):
        candidates = self.executables.get(tool)
        if not candidates:
            raise CommandError("tool_not_allowed", f"{tool!r} is not an allowlisted host tool")
        for candidate in candidates:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        found = shutil.which(tool, path=self.env.get("PATH"))
        if found and found in candidates:
            return found
        raise CommandError("tool_unavailable", f"{tool} is not installed on this host")

    def available(self, tool):
        try:
            self.resolve(tool)
        except CommandError:
            return False
        return True

    def run(self, tool, args=(), *, timeout=None, input_text=None, check=False):
        executable = self.resolve(tool)
        argv = [executable]
        for arg in args:
            if not isinstance(arg, str) or arg == "" or "\x00" in arg:
                raise CommandError("invalid_argument", "command arguments must be non-empty strings")
            argv.append(arg)

        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout or self.default_timeout,
                input=input_text,
                env=self.env,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired:
            result = CommandResult(tool, tuple(args), 124, "", "timed out", timed_out=True)
            if check:
                raise CommandError("command_timeout", f"{tool} timed out")
            return result
        except OSError as exc:
            raise CommandError("command_failed", f"{tool} could not be started: {exc}")

        result = CommandResult(
            tool,
            tuple(args),
            completed.returncode,
            completed.stdout or "",
            completed.stderr or "",
        )
        if check and not result.ok:
            raise CommandError("command_failed", f"{tool} exited with {result.returncode}")
        return result


class RecordingRunner(CommandRunner):
    """Deterministic runner for tests: fixed replies, no host process."""

    def __init__(self, replies=None, *, default=None):
        self.replies = dict(replies or {})
        self.default = default
        self.calls = []

    def resolve(self, tool):
        if tool not in EXECUTABLES:
            raise CommandError("tool_not_allowed", f"{tool!r} is not an allowlisted host tool")
        return f"/usr/bin/{tool}"

    def available(self, tool):
        return tool in EXECUTABLES

    def run(self, tool, args=(), *, timeout=None, input_text=None, check=False):
        self.resolve(tool)
        args = tuple(args)
        self.calls.append((tool, args, input_text))
        reply = self.replies.get((tool, args))
        if reply is None:
            reply = self.replies.get(tool)
        if callable(reply):
            reply = reply(args)
        if reply is None:
            if self.default is None:
                result = CommandResult(tool, args, 1, "", f"no reply for {tool} {args}")
            else:
                result = CommandResult(tool, args, 0, self.default, "")
        elif isinstance(reply, CommandResult):
            result = reply
        elif isinstance(reply, tuple):
            code, stdout, stderr = (list(reply) + ["", ""])[:3]
            result = CommandResult(tool, args, int(code), str(stdout), str(stderr))
        else:
            result = CommandResult(tool, args, 0, str(reply), "")
        if check and not result.ok:
            raise CommandError("command_failed", f"{tool} exited with {result.returncode}")
        return result
