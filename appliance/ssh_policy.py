# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one effective-SSH-policy model the appliance judges the backup account by.

``sshd -T -C user=…`` is the only authority for what the running daemon would
apply, and every consumer — activation, ``backup-access status``,
``host-config`` drift detection and ``verify-install`` — must judge the same
directives against the same expectations. A second, smaller model somewhere
would report "confined" for a policy nobody checked.
"""

OPTION_CHROOT = "chrootdirectory"
OPTION_FORCE_COMMAND = "forcecommand"

# The one forced command: generated into the Match block and expected back from
# the running daemon. A second, shorter definition anywhere would let the
# generator and the check drift apart without anything failing.
SFTP_PROGRAM = "internal-sftp"
DENIED_SFTP_REQUESTS = (
    "symlink",
    "hardlink",
    "rename",
    "posix-rename",
    "remove",
    "mkdir",
    "rmdir",
    "setstat",
    "fsetstat",
)
FORCED_COMMAND = f"{SFTP_PROGRAM} -P {','.join(DENIED_SFTP_REQUESTS)}"

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
    ("permitopen", "none"),
)

VERIFIED_OPTIONS = (OPTION_CHROOT, OPTION_FORCE_COMMAND) + tuple(
    option for option, _ in REQUIRED_RESTRICTIONS
)


def parse_sshd_config(text):
    values = {}
    for line in (text or "").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        key, _, value = entry.partition(" ")
        if key:
            values[key.strip().lower()] = value.strip()
    return values


def forced_command_confirmed(forced):
    """Is this exactly the confinement the appliance generates?

    A prefix match would accept a plain ``internal-sftp``: the same program
    with none of the write operations denied. The denied set is compared as a
    set because ``-P`` carries no order, and any further token is refused
    because it is an option nobody evaluated.
    """

    tokens = str(forced or "").split()
    if len(tokens) != 3 or tokens[0] != SFTP_PROGRAM or tokens[1] != "-P":
        return False
    return set(tokens[2].split(",")) == set(DENIED_SFTP_REQUESTS)


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
        "confirmed": forced_command_confirmed(forced),
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


def read_effective_policy(runner, *, user, export_root):
    """The policy the running daemon would apply to ``user``, evaluated.

    Without a connection specification ``sshd -T`` skips every ``Match`` block,
    so the backup account's chroot and forced command are only visible when the
    user is named. A policy that cannot be read is unavailable, never a pass.
    """

    if runner is None or not runner.available("sshd"):
        return evaluate_policy({}, export_root=export_root)
    result = runner.run(
        "sshd",
        ["-T", "-C", f"user={user},host=localhost,addr=127.0.0.1"],
        timeout=20,
    )
    effective = parse_sshd_config(result.stdout) if result.ok else {}
    return evaluate_policy(effective, export_root=export_root)
