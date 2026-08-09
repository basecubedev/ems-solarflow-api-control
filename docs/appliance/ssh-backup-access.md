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

`ems-backup` is a file-export account, not an administration account.

| Path | Access |
|---|---|
| `/opt/ems-solarflow/config` | read-only |
| `/opt/ems-solarflow/backups` | read-only |
| `/opt/ems-solarflow/data` | read-only |

Administrative write access requires a separate system account and is not
enabled by default.

## Copy files with rsync or scp

```bash
rsync -a ems-backup@ems-solarflow.local:/opt/ems-solarflow/backups/ ./ems-backups/
```

```bash
scp -r ems-backup@ems-solarflow.local:/opt/ems-solarflow/config ./ems-config
```

The UI shows both commands with your appliance's real hostname.

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
