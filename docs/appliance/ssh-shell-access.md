# SSH shell access

The appliance ships three accounts and only one of them is a way in over the
network with a shell:

| Account | Over SSH | Reaches root |
| --- | --- | --- |
| `ems-backup` | SFTP only — chroot into the export root, forced command, no TTY | no |
| `ems-rescue` | refused: the shipped policy denies it password and keyboard-interactive | yes, at a keyboard or serial console |
| `ems-shell` | key only, once enabled | yes, through `sudo` |

`ems-shell` exists for the case the other two do not cover: the box is up, the
console answers, and something will not finish — and the reason is in a file or
a journal nobody can reach from a browser. The appliance manager's own update
sidecar runs with `docker run --rm`, so when it fails its container is already
gone; what it wrote is on disk, and reading it needs a shell.

## What it costs, stated plainly

The console may deploy a key onto this account. Since the account reaches root,
**whoever reaches the Appliance Manager console reaches root on the appliance.**
That was chosen deliberately over the alternative — keys only from a root shell
already on the box — because an operator locked out of a half-finished update
has no root shell to issue a key from.

Nothing here is protected by being hard to find. What holds the line is that two
independent things must both be true before a login exists, and each is false by
default:

1. **The enable flag**, in root-owned agent state. While it is off, the sshd
   policy refuses this account *every* authentication method, so a key that is
   already deployed is still not a login.
2. **A key.** An enabled account with an empty `authorized_keys` admits nobody.

Deploying a key does not set the flag, and setting the flag does not deploy a
key.

A third control is available and is not on by default: removing `ems-shell` from
`ssh_key_accounts` in `/etc/ems-appliance-manager/appliance.conf` refuses
console-deployed shell keys outright. An appliance administered only from a
keyboard should do that.

## What the policy allows

While enabled, the generated `Match User ems-shell` block admits public keys and
nothing else:

```
Match User ems-shell
    PubkeyAuthentication yes
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PermitTTY yes
    AllowTcpForwarding no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTunnel no
    GatewayPorts no
    PermitOpen none
```

Passwords are denied twice on purpose. `PasswordAuthentication no` alone leaves
PAM's keyboard-interactive path, which asks for the same password — the mistake
`ems-rescue` was already fixed for.

It is a shell, so it keeps a TTY and has no chroot or forced command. It is not
a route into the network behind the appliance, so forwarding and tunnelling stay
closed. A key holder who needs a tunnel has a shell and can say so explicitly.

## sudo without a password

The account has no password at all (`--disabled-password`), so `sudo` is
installed with `NOPASSWD` in `/etc/sudoers.d/ems-shell`. A password prompt that
no key holder can answer is not a safety property; it is an account that cannot
do the one thing it exists for. The key is the authentication.

The drop-in is validated with `visudo -cf` before it is installed, because a
sudoers file that does not parse takes `sudo` away from every account on the
host — including the rescue account someone would use to repair it.

## Lifecycle

The account is created once by the package's `shell-account.sh`, which owns it
and owns it alone. An account that already exists is left exactly as it is, so
an upgrade never resets a shell, a group membership or a home directory an
operator changed.

Creating it grants nothing: it is created with no key and with the enable flag
off.

## See also

- [ssh-backup-access.md](ssh-backup-access.md) — the SFTP account and its confinement
- [console-recovery.md](console-recovery.md) — `ems-rescue`, and why it is not an SSH login
- [security-model.md](security-model.md) — the web/agent privilege boundary this account is measured against
