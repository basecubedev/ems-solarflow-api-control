# SPDX-License-Identifier: AGPL-3.0-or-later
"""A host whose systemd and sshd keep the state the generated files put there.

The point of the host-configuration transaction is that the files on disk and
what the running daemons apply cannot drift apart. A recording stub cannot show
that: it answers the same thing before and after a rollback. This fake keeps two
pieces of runtime state instead — the path the ``.path`` unit is armed with and
the policy sshd has loaded — and only changes them when the command that would
really change them succeeds.
"""

from appliance.commands import CommandResult
from appliance.host_config import PATH_UNIT, path_unit_dropin, sshd_policy_file
from appliance.ssh_policy import parse_sshd_config

UNIT_SSH = "ssh.service"

BASE_POLICY = {
    "passwordauthentication": "no",
    "kbdinteractiveauthentication": "no",
    "pubkeyauthentication": "yes",
    "permittty": "no",
    "allowtcpforwarding": "no",
    "allowagentforwarding": "no",
    "x11forwarding": "no",
    "permittunnel": "no",
    "gatewayports": "no",
    "forcecommand": "internal-sftp -P symlink,hardlink",
}


def policy_of(sshd_dir):
    """The Match policy the generated file currently asks sshd to apply."""

    try:
        text = sshd_policy_file(str(sshd_dir)).read_text(encoding="utf-8")
    except OSError:
        return {}
    values = dict(BASE_POLICY)
    for line in text.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or entry.startswith("Match "):
            continue
        key, _, value = entry.partition(" ")
        values[key.strip().lower()] = value.strip()
    return values


def watched_of(systemd_dir):
    for line in _read(path_unit_dropin(str(systemd_dir))).splitlines():
        if line.startswith("PathChanged=") and line.partition("=")[2]:
            return line.partition("=")[2].strip()
    return ""


def _read(path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


class FakeHost:
    """``sshd`` and ``systemctl`` with real, observable runtime state."""

    def __init__(
        self,
        *,
        systemd_dir,
        sshd_dir,
        tools=("sshd", "systemctl"),
        path_unit_active="waiting",
        ssh_active="active",
        enabled="enabled",
    ):
        self.systemd_dir = systemd_dir
        self.sshd_dir = sshd_dir
        self.tools = set(tools)
        self.calls = []
        self.failures = {}
        self.path_unit_active = path_unit_active
        self.ssh_active = ssh_active
        self.enabled = enabled
        self.armed_path = watched_of(systemd_dir)
        self.loaded_policy = policy_of(sshd_dir)

    # --- scripting --------------------------------------------------------

    def fail(self, tool, *args, returncode=1):
        self.failures[(tool, tuple(args))] = returncode
        return self

    def succeed(self, tool, *args):
        self.failures.pop((tool, tuple(args)), None)
        return self

    def _failure(self, tool, args):
        for length in range(len(args), -1, -1):
            code = self.failures.get((tool, tuple(args[:length])))
            if code is not None:
                return code
        return 0

    # --- the runner protocol ----------------------------------------------

    def available(self, tool):
        return tool in self.tools

    def run(self, tool, args=(), *, timeout=None, input_text=None, check=False):
        args = tuple(str(item) for item in args)
        self.calls.append((tool, args))
        code = self._failure(tool, args)
        stdout = ""

        if tool == "sshd" and args[:1] == ("-T",):
            stdout = "".join(f"{key} {value}\n" for key, value in sorted(self.loaded_policy.items()))
        elif tool == "systemctl" and args[:1] == ("is-active",):
            unit = args[1] if len(args) > 1 else ""
            stdout = (self.ssh_active if unit == UNIT_SSH else self.path_unit_active) + "\n"
        elif tool == "systemctl" and args[:1] == ("is-enabled",):
            stdout = f"{self.enabled}\n"
        elif tool == "systemctl" and args[:2] == ("show", PATH_UNIT):
            stdout = f"Paths={self.armed_path} (PathChanged)\n" if self.armed_path else "Paths=\n"
        elif tool == "systemctl" and args[:1] == ("restart",) and code == 0:
            if args[1:2] == (PATH_UNIT,):
                self.armed_path = watched_of(self.systemd_dir)
        elif tool == "systemctl" and args[:1] == ("reload",) and code == 0:
            if args[1:2] == (UNIT_SSH,):
                self.loaded_policy = policy_of(self.sshd_dir)

        return CommandResult(tool=tool, args=args, returncode=code, stdout=stdout, stderr="")

    # --- observation ------------------------------------------------------

    def effective_chroot(self):
        return parse_sshd_config(
            "".join(f"{key} {value}\n" for key, value in self.loaded_policy.items())
        ).get("chrootdirectory", "")

    def sequence(self, tool=None):
        return [
            " ".join((name, *args))
            for name, args in self.calls
            if tool is None or name == tool
        ]
