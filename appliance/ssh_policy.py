# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one effective-SSH-policy model the appliance judges its accounts by.

``sshd -T -C user=…`` is the only authority for what the running daemon would
apply, and every consumer — activation, ``backup-access status``,
``host-config`` drift detection and ``verify-install`` — must judge the same
directives against the same expectations. A second, smaller model somewhere
would report "confined" for a policy nobody checked.

The backup account is judged by its confinement. The rescue account, whose
password is printed in this repository, is judged by whether that password
is refused at all. Both are read back from the daemon, because the drop-in
the package writes is a promise and only ``sshd`` knows whether it read it.
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
    "lsetstat",
    "fsync",
    "copy-data",
)
# -R makes the whole subsystem read-only in sshd itself. The -P list stays as
# the second, explicit refusal of the individual write requests: the read-only
# mounts and this flag are two independent reasons a write fails, and the
# generated policy is what an operator reads to know that.
FORCED_COMMAND = f"{SFTP_PROGRAM} -R -P {','.join(DENIED_SFTP_REQUESTS)}"

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
    with none of the write operations denied. Both refusals have to be present
    -- ``-R`` makes the subsystem read-only in sshd itself, ``-P`` names the
    individual requests -- the denied set is compared as a set because ``-P``
    carries no order, and any further token is refused because it is an option
    nobody evaluated.
    """

    tokens = str(forced or "").split()
    if len(tokens) != 4 or tokens[0] != SFTP_PROGRAM:
        return False
    if tokens[1] != "-R" or tokens[2] != "-P":
        return False
    return set(tokens[3].split(",")) == set(DENIED_SFTP_REQUESTS)


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


# --- the rescue account -------------------------------------------------------

# The two methods the shipped policy refuses the rescue account, named once so
# the Match block host_config writes and the question asked of the running
# daemon cannot drift apart. ``pubkeyauthentication`` is deliberately absent:
# the block leaves it to the global setting, and console-recovery.md offers a
# key login on that account.
RESCUE_REFUSED_METHODS = (
    ("passwordauthentication", "no"),
    ("kbdinteractiveauthentication", "no"),
)

REFUSAL_REFUSED = "refused"
REFUSAL_ACCEPTED = "accepted"
REFUSAL_UNKNOWN = "unknown"
REFUSAL_ABSENT = "absent"


def evaluate_password_refusal(effective):
    """Does this effective policy refuse the account its password?

    The same per-option shape as :func:`evaluate_policy`, so a card can show
    which directive the daemon answered differently.
    """

    effective = effective or {}
    restrictions = {}
    for option, expected in RESCUE_REFUSED_METHODS:
        actual = str(effective.get(option, ""))
        restrictions[option] = {
            "value": actual,
            "expected": expected,
            "confirmed": actual.lower() == expected,
        }
    violations = [
        option for option, _ in RESCUE_REFUSED_METHODS if not restrictions[option]["confirmed"]
    ]
    return {"restrictions": restrictions, "violations": violations}


def read_password_refusal(runner, *, user):
    """Whether the running daemon refuses ``user`` a password, asked of the daemon.

    A drop-in on disk is a promise. An ``/etc/ssh/sshd_config`` carried over
    from an older install has no ``Include`` line for the drop-in directory,
    dpkg never rewrites a modified conffile, and a daemon that was not reloaded
    runs what it read before -- in each case the block is there and sshd
    applies its defaults, which take a password.

    There is deliberately no fallback to a bare ``sshd -T`` here, unlike
    ``SshService.effective_config``: without ``-C`` sshd skips every Match
    block, so an answer that is not about this account is not an answer, and
    it reads as unknown rather than as a refusal. Absent means there is no
    sshd to ask, which is the state a flashed image ships in.
    """

    verdict = {"state": REFUSAL_ABSENT, "user": user, "restrictions": {}, "violations": []}
    if runner is None or not runner.available("sshd"):
        return verdict
    result = runner.run(
        "sshd",
        ["-T", "-C", f"user={user},host=localhost,addr=127.0.0.1"],
        timeout=20,
    )
    effective = parse_sshd_config(result.stdout) if result.ok else {}
    if not effective:
        verdict["state"] = REFUSAL_UNKNOWN
        return verdict
    verdict.update(evaluate_password_refusal(effective))
    verdict["state"] = REFUSAL_ACCEPTED if verdict["violations"] else REFUSAL_REFUSED
    return verdict
