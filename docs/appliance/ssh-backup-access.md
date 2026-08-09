# SSH and backup access

Open **SSH & Backup Access**.

## SSH service

The appliance shows the SSH unit state and the effective hardening options, and
compares them with the recommended defaults:

```text
PermitRootLogin        no
PasswordAuthentication no
PubkeyAuthentication   yes
```

**Enable SSH** enables and starts the service. It enables nothing else — there
is no operation in the appliance that can turn on password authentication.
**Disable SSH** stops and disables it.

## Add an SSH public key

1. Choose the account. Only accounts listed in
   `/etc/ems-appliance-manager/appliance.conf` (`ssh_key_accounts`) can be
   managed; `ems-backup` is the default.
2. Paste an OpenSSH **public** key, for example:

   ```text
   ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... you@your-laptop
   ```

3. Press **Add key**, review the preview (type, comment, fingerprint) and
   confirm.

Accepted key types:

```text
ssh-ed25519
sk-ssh-ed25519@openssh.com
ssh-rsa and the ecdsa-sha2-nistp* / sk-ecdsa variants where required
```

Refused: private keys, malformed keys, empty keys, duplicate keys, unsupported
key types, and keys above the size limit. Never paste a private key — the
appliance never asks for one and never displays one.

`authorized_keys` is written atomically with `~/.ssh` at `0700` and
`authorized_keys` at `0600`.

## Remove keys

Each key row has **Remove** (by fingerprint). **Revoke all** removes every key
of an account and states clearly that remote access through that account stops
immediately.

## Generate a key pair on your own computer

```bash
ssh-keygen -t ed25519 -C "you@your-laptop"
cat ~/.ssh/id_ed25519.pub
```

Paste the `.pub` line — never `~/.ssh/id_ed25519`.

## The dedicated backup account

`ems-backup` is a file-export account, not an administration account. It is
created, confined and removed by this package, so its name is fixed: a
different `backup_user` in `appliance.conf` is refused when the configuration
is loaded rather than failing later.

It has **no interactive shell** (`/usr/sbin/nologin`) and the sshd policy
`/etc/ssh/sshd_config.d/ems-appliance-backup.conf` restricts it further. That
file is *generated* from `appliance.conf` by `ems-appliance host-config
--apply`, not shipped as a conffile, so a custom export root reaches the
effective chroot directory instead of disagreeing with it:

```text
Match User ems-backup
    ChrootDirectory /srv/ems-appliance-export
    PasswordAuthentication no
    PubkeyAuthentication yes
    PermitTTY no
    AllowTcpForwarding no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTunnel no
    GatewayPorts no
    PermitOpen none
    ForceCommand internal-sftp -P symlink,hardlink,rename,posix-rename,remove,mkdir,rmdir,setstat,fsetstat
```

`ForceCommand internal-sftp` means the account can transfer files and nothing
else. **rsync and scp do not work with it** — both need to execute a remote
command. Use SFTP.

The `-P` list is part of the boundary, not decoration: it denies every SFTP
request that could write, move or delete. The appliance compares the effective
forced command against exactly this command — a plain `internal-sftp`, a
shorter list or an extra option is a policy violation, not an equivalent.

### The export root

`ForceCommand` alone removes the shell but not the filesystem: an account with
only that restriction can still read `/etc/passwd`, walk `/usr` and list every
world-readable path on the appliance. So the session is confined as well.

`ChrootDirectory` puts the session inside a dedicated export root that only
root can write:

```text
/srv/ems-appliance-export/          root:root 0755   ← the session's "/"
  config/    read-only bind mount of /opt/ems-solarflow/config
  backups/   read-only bind mount of /opt/ems-solarflow/backups
  data/      read-only bind mount of /opt/ems-solarflow/data
```

The account therefore sees exactly three directories and cannot reach anything
else. The exports are **bind mounts of the live EMS directories**, not copies:
a backup taken through SFTP is always current, and nothing is duplicated into a
shadow tree that could go stale.

The export root is an **exclusive** boundary. Anything else inside it — a file,
a hidden file, a symlink, an extra directory — would be visible to the account,
so the setup refuses to activate and names what has to be moved. Operator
content is never deleted to make room:

```text
ems-appliance: the export root contains entries this appliance does not
manage: host-note.txt; move them out of /srv/ems-appliance-export
```

`/srv/ems-appliance-export` and every parent must stay root-owned and not
writable by group or others, or sshd refuses the chroot with
`bad ownership or modes for chroot directory`.

`authorized_keys` still lives in the account's real home
(`/var/lib/ems-backup/.ssh/authorized_keys`). sshd reads it as root *before*
chrooting, so key management is unaffected by the confinement.

| Path in the SFTP session | Host path | Access |
|---|---|---|
| `/config` | `/opt/ems-solarflow/config` | read-only |
| `/backups` | `/opt/ems-solarflow/backups` | read-only |
| `/data` | `/opt/ems-solarflow/data` | read-only |

Read-only is enforced twice: the bind mount carries `ro`, and POSIX ACLs grant
the account read-and-traverse only. `/usr/lib/ems-appliance-manager/setup-export-root.sh`
applies both — a traversal-only ACL entry on the install root and a recursive
**plus default** read ACL on each export, so files EMS creates later stay
readable without becoming writable. The `acl` package is a declared dependency.

A bind mount that cannot be made read-only is unmounted again rather than left
as a writable export.

### Which paths may be exported

The export root turns host paths into kernel mounts, so a redirected path would
turn EMS backup access into host access: `/opt/ems-solarflow/config → /etc`
must never become an export. Every path is therefore validated *before* any ACL
or mount change, and the whole run is refused if one of them fails:

| Rule | Applies to |
|---|---|
| absolute, no trailing slash, no empty, `.` or `..` segment, no character needing quoting | the configured install root and export root |
| a real directory, not a symlink, at exactly the configured location | both roots and every export source and target |
| every **existing** parent component is a real directory | both roots |
| the two roots are not identical and neither lives inside the other | the two roots |
| only `config`, `backups` and `data` exist inside it | the export root |
| root-owned, mode 0755 | every export target |

A path that does not exist yet is validated against its nearest existing
parent first, so an export root below a symlinked parent — say
`/srv/redirect → /outside`, `export_root = /srv/redirect/ems-export` — is
refused *before* the directory is created, and nothing is written below the
redirected destination.

The configured path is the identity. It is never canonicalised: a symlinked
install root is rejected rather than silently rewritten into its target. A
separate data partition is supported as a **mount** at the configured path.

Neither root may contain the other. An installation root inside the export root
would publish the entire installation — including secrets and Admin state — as
one more directory in the chroot.

The recursive ACL walk and both identity checks act on an **open directory
handle** (`/proc/self/fd/N`) for the source that was validated, so a source
swapped while the ACLs are being applied cannot receive them. The handle is
checked again immediately before the bind, by device and inode as well as by
path.

A mount at an export target that is not the bind this feature made is
**unmounted first, and the object that becomes visible underneath is validated
again** before anything is created or mounted there. A symlink hidden below a
foreign mount therefore never becomes the target of a root-owned operation.

After mounting, the kernel is asked what is actually there: a mount point alone
is not evidence — the mount must carry the validated subtree and be read-only
in effect, or it is removed again.

A refusal names the export and its configured path. Where a rejected symlink
pointed is never repeated into the status file, the journal or the UI.

### When the export root is built

| Trigger | What runs |
|---|---|
| Boot | `ems-appliance-export.service` |
| Package install, reinstall, upgrade | the postinst, before the services start |
| A directory appears under `/opt/ems-solarflow` later | `ems-appliance-export.path` triggers the service |
| Manually | `sudo systemctl start ems-appliance-export.service` |

Start the **unit**, not the script it runs. The unit re-validates backup access
afterwards, so authentication follows the boundary the run produced. Running
`setup-export-root.sh` by hand rebuilds the mounts but leaves authentication
where it was; `sudo ems-appliance backup-access activate` then re-enables it
once the state is exact again.

The watcher is not optional. If `ems-appliance-export.path` fails to start on a
live host, the package installation fails rather than leaving an installation
where a directory created later is silently never published.

### When the confinement is activated

What protects the appliance is the policy the *running* daemon applies, not the
drop-in on disk. Backup access is therefore fail-closed: after the package
writes its configuration it runs

```bash
sudo ems-appliance backup-access activate
```

which validates the sshd configuration, reloads the daemon, and reads the
effective policy for the backup account back with
`sshd -T -C user=ems-backup,...`. All of these must be confirmed:

```text
ChrootDirectory                   PermitTTY no
ForceCommand internal-sftp -P …   AllowTcpForwarding no
PasswordAuthentication no         AllowAgentForwarding no
KbdInteractiveAuthentication no   X11Forwarding no
PubkeyAuthentication yes          PermitTunnel no
PermitOpen none                   GatewayPorts no
```

The forced command is compared exactly, including its full list of denied SFTP
requests. Everything else is compared against the value above.

The sshd policy is only half the boundary. Activation additionally requires the
export root to be exclusive and every **present** export to be exact: mounted
at its expected target, publishing the configured EMS directory, read-only. A
missing EMS directory stays `pending` and does not block activation; a present
one that is not exact does.

Source identity is proven from the kernel's own mount table — the mount root
and the device the bind carries — not from the `ro` option alone. A foreign
read-only mount at an expected target is never called confined.

If the configuration is invalid, the reload fails, a restriction is not
confirmed, the export root is not exclusive or an export is not exact, the
account's `authorized_keys` is moved to `authorized_keys.disabled-by-appliance`
and the account is expired. The UI then reports the reason by name. The
appliance never reports confined, read-only access as active on the strength of
a file it wrote.

| Reason | What was not confirmed |
|---|---|
| `sshd_config_invalid` | `sshd -t` refused the configuration |
| `sshd_reload_failed` | a running daemon did not reload the new policy |
| `ssh_policy_unreadable` | `sshd -T -C user=…` could not report what the daemon would apply |
| `confinement_not_confirmed` | a restriction the account requires is not in force |
| `export_root_not_exclusive` | the chroot root holds something this feature does not manage |
| `exports_not_confined` | an existing export is unmounted, read-write, or publishes something else |
| `key_conflict` | two key files exist and only an operator can decide which is current |
| `backup_account_home_marker_missing` | the home does not carry the ownership marker this package wrote |
| `backup_account_home_marker_mismatch` | a file is at the marker path, but it is not the marker this package wrote |
| `backup_account_home_identity_mismatch` | the recorded home is not that directory any more |
| `backup_account_ownership_record_requires_migration` | the record predates the marker; see below |

A policy that cannot be *read* is not a policy that holds. `ems-appliance
host-config --apply` fails and rolls back rather than reporting an applied
configuration whose confinement nothing confirmed, and a successful apply never
reports an unread policy as verified:

```text
verified            every runtime component that was expected to be live was read and agreed
offline_deferred    no running systemd; the generated files take effect on the next boot
unavailable         a component that should have answered did not — the apply is rolled back
```

A rollback of that transaction compares the **values** the daemon applies, not
the names of what is wrong: `PermitTTY yes` and `PermitTTY forced-commands-only`
break the same rule and are not the same policy. A host that was already
degraded gets that exact degraded state back, and anything that did not come
back is named with what was expected and what is there:

```json
{
  "ssh_policy_not_restored": {
    "permittty": {"expected": "yes", "observed": "forced-commands-only"}
  }
}
```

If `ems-appliance-export.service` fails after access was active,
`ems-appliance-backup-access-disable.service` runs as its `OnFailure` unit and
takes the authentication away immediately. A successful run re-validates it
through `ExecStartPost`. Authentication follows the verified boundary; it never
survives it.

If both `authorized_keys` and `authorized_keys.disabled-by-appliance` exist,
neither file is discarded: the live one is preserved under a `.conflict` name
and authentication stays disabled until an operator decides which key is
current.

OpenSSH stays optional: without it the feature reports `unavailable` and the
package still installs.

```bash
sudo ems-appliance backup-access            # what is in force right now
sudo ems-appliance backup-access disable    # revoke until it is verified again
```

### Removal and purge

| Step | What happens to backup access |
|---|---|
| `apt remove` | authentication is disabled first, the account is expired, the binds are unmounted; the key material is preserved next to `authorized_keys` |
| reinstall / upgrade | the keys are restored, but only once the effective confinement is verified again |
| `apt purge` | the ACL entries this feature granted are withdrawn, the generated sshd policy and host configuration are removed, the package-created account, its package-created home and its keys are removed, and the export root is removed once nothing is mounted |

Removal takes the authentication with it because it takes the confinement with
it: the generated policy and the read-only binds are part of the package, so a
surviving key would open an *unconfined* SFTP session over the whole host after
the next sshd reload. **Removal fails closed**: if neither
`ems-appliance backup-access disable` nor the direct maintainer fallback can
revoke the authentication, the removal stops and says why, instead of leaving a
usable key without the chroot that confined it.

Purge is ownership-gated. The package records that it created the account:

```json
{
  "account": "ems-backup",
  "created_by_package": true,
  "home": "/var/lib/ems-backup",
  "home_created_by_package": true,
  "authorized_keys": "/var/lib/ems-backup/.ssh/authorized_keys",
  "recorded_at": "2026-08-06T00:00:00Z"
}
```

in `/var/lib/ems-appliance-manager/agent/package-state/backup-account.json`
(root-only). Without that record, purge removes the package-managed key files
and nothing else — no account, no home, no operator data. An account that
already exists when the package is installed is a **conflict**: the
installation fails and names it, rather than adopting an account it would later
delete.

### An ownership record from an older version

Ask the host what its record proves:

```bash
ems-appliance backup-account status
```

`current` needs nothing. `legacy_manual_migration_required` means the record
predates the ownership marker, so backup access stays disabled until an
administrator decides. Review the home directory it names, then adopt it
explicitly:

```bash
sudo ems-appliance backup-account migrate-ownership
```

This refuses unless the account identity, the recorded home, the absence of a
foreign marker, the root-owned confined home, the absence of foreign home
content and the attribution of every key in it all check out — and it refuses a
record with no schema version outright, because nothing in such a record can
establish ownership. Reinstalling does **not** perform this step. See
[security-model.md](security-model.md) for the full state table.

Purge withdraws only the entries this feature added (`setfacl -x u:ems-backup`).
ACL entries an operator set for other accounts are left alone, and EMS
configuration, data and backups are never touched in any of these steps. A
mount or an account purge could not withdraw is reported explicitly; purge does
not claim to be clean when it is not.

### What the appliance reports

**SSH & Backup Access → Export access** reports what it can observe, not what
the setup script intended: the live host mount table and the effective sshd
configuration for the account.

| State | Meaning |
|---|---|
| `configured` | every promised restriction is confirmed and every present export is a read-only mount |
| `pending` | no EMS export directory exists yet |
| `degraded` | the export root holds an unmanaged entry, an export is missing from the root, mounted read-write, published from somewhere other than the configured EMS directory, the chroot is not in effect, or a promised restriction is not enforced |
| `unknown` | the effective sshd configuration could not be read |

An export that is mounted read-write is reported as `degraded`. The appliance
never calls a writable export read-only.

When a restriction is not enforced, the card names it (`Not enforced by sshd:
allowtcpforwarding`) rather than only saying "degraded", and a refused export
source appears as `Export setup: failed — …`. The descriptive note is derived
from the observation: it does not claim forwarding, passwords or TTY are
disabled unless that was verified.

Administrative write access requires a separate system account and is not
enabled by default.

## Copy files with SFTP

Paths are relative to the export root, so they are short:

```bash
sftp -r ems-backup@ems-solarflow.local:/backups ./ems-backups
```

```bash
sftp -r ems-backup@ems-solarflow.local:/config ./ems-config
```

```bash
sftp ems-backup@ems-solarflow.local
```

The UI shows these commands with your appliance's real hostname.

If you need rsync semantics, run it against a host account that has a shell —
the backup account deliberately does not. Do not relax the drop-in to make
rsync work; that would give a stolen key a shell.

## Backup assistance, not a second backup format

The Appliance Manager does not define an EMS backup format. It shows where the
EMS backup directory is, how large it is and whether it is reachable, prepares
the read-only export account, and can create a support archive.

## Support archive

**Diagnostics → Create support archive** produces a bounded, redacted archive
containing OS version, hardware information, systemd and Docker service state,
Admin container metadata, bounded Admin logs, Appliance Manager logs,
package-manager state and storage information, plus a `manifest.json` listing
every included file.

It never includes passwords, SSH private keys, complete `authorized_keys`
contents, MQTT credentials, EMS secrets or tokens.
