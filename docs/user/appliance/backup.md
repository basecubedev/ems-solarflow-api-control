# SSH & backup access

## Purpose

Get your configuration and data off the appliance, with a read-only account
that copies files and can do nothing else. The same page, **SSH & backup
access** — *Shell access for you, and a read-only account that copies files off
this appliance* — also switches SSH on and off, shows the console rescue
account, and grants a root-capable shell when you need one.

## When to use this workflow

- Before an operating-system update, and regularly: a backup on the card itself
  is lost with the card.
- The assistant's optional **Set up backup access** row is still **To do**.
- You need a shell on the appliance.

## Prerequisites

- You are signed in — see [First start](first-start.md).
- An SSH key pair on your own computer. The appliance never asks for a private
  key and never generates one for you; you paste the public half, the line
  that starts with `ssh-ed25519` or `ssh-rsa`.

## What there is to save

| | |
| --- | --- |
| **Configuration** | Your devices, limits and control settings |
| **Runtime state** | What the controller currently believes |
| **Backups** | Snapshots the EMS Admin console made |

An operating-system update does not touch any of it: `apt` patches the system
packages around it.

**Re-flashing the card is a different matter, and it erases all of it.** A
backup protects against that, and against the card failing, which no software
mechanism can.

## Step-by-step instructions

![The SSH & backup access page: SSH service stopped with Switch SSH on, the console rescue account with the shipped password, Add a public key for ems-backup, root-capable shell access disabled, no keys for ems-backup yet, the backup account and export access, the readable paths and the commands to copy files off](../../assets/screenshots/appliance/appliance-backup-access.png)

### 1 — Switch SSH on

**What you see:** **SSH service** — *Key-based remote access to this
appliance* — with **Running** or **Stopped**, the service state, and the
password login, which is always off.

**What you select:** **Switch SSH on**, then **Confirm** in the plan *Switch
the SSH service on*.

### 2 — Add your public key

**What you see:** **Add a public key** — *Paste an OpenSSH public key* — with
**Account** and **Public key**, and the warning *Never paste a private key. Only
public keys are accepted.*

1. Keep **Account** on `ems-backup`.
2. Paste your public key.
3. Press **Add key**, check the plan — *Add an SSH public key*, with the key's
   type, comment and fingerprint — and press **Add key** again.

**Expected result:** **Keys for ems-backup** lists the key, and the assistant's
**Set up backup access** reads **Done**.

### 3 — Check what the account can see

**What you see:**

- **Backup account** — *Copies files off this appliance, read-only* —
  **Available**, with the account, its authorized keys, the protocol (SFTP), and
  **No** for shell access and write access.
- **Export access** — *What the backup account can see, as sshd applies it* —
  **Set up** and **Confined to the export root** when everything is in place,
  with the export root.
- **Readable paths** — *What the backup account can copy* — the directories,
  each **read-only** with the state **Read-only export** and its size.

The read-only export itself is set up by the appliance, not by a button; the
**Export access** card reports whether it is in place. The account is
**read-only and confined**: it can see those directories and nothing else, it
has no shell, and it cannot write. If any part of that confinement cannot be
proved, the card says **Not confined** or names what sshd does not enforce, and
activation switches the account off rather than leaving it half-open.

### 4 — Copy the files

**SFTP only.** Not `scp`, not `rsync` — the account is confined to an SFTP
session and other tools are refused. **Copy files from this appliance** shows
the commands to run on your own computer, for example:

```bash
sftp -r ems-backup@ems-solarflow.local:/data/backups ./ems-backups
sftp -r ems-backup@ems-solarflow.local:/config ./ems-config
```

Or open a session and fetch what you need:

```bash
sftp -i ~/.ssh/your-key ems-backup@ems-solarflow.local
```

```text
get -r /config
get -r /data
```

`/data` is the one that carries your snapshots — they live in `data/backups`.
There is a `/backups` entry in the session too; it is a leftover and is empty.

A graphical client works too: FileZilla, WinSCP or Cyberduck, protocol **SFTP**,
user `ems-backup`, and your key file.

## Turning it off

- **Remove** in the key's row of **Keys for ems-backup** takes one key away,
  after its plan.
- **Switch SSH off** stops the SSH service. The keys stay, so switching it on
  again does not need a new key. Switching off is deliberately fail-closed: if
  it cannot prove the account is off afterwards, it reports failure rather than
  success.

Revoking the export itself is a console command rather than a button, because
it must work when the browser does not:

```bash
sudo ems-appliance backup-access disable
```

## The console rescue account

**Console rescue account** shows `ems-rescue`, the account for a keyboard at
the appliance when the web console does not answer. Its state says whether it
still has the **Shipped password**, which is published with this project — fine
on a private network, a login for anyone who reaches the appliance from outside
one — or **Changed**, and whether the running SSH server refuses its password
from the network (**Refused by the running sshd**). Change it at the console
with `sudo passwd ems-rescue`. See
[When it stops working](recovery.md#the-web-page-does-not-load).

## Root-capable shell access

**Root-capable shell access** — *Key-only SSH shell, off until switched on* —
is a second account, `ems-shell`, for an SSH shell. *This account has a shell
and reaches root through sudo. A key deployed on it is root on this appliance.
It is key-only and never accepts a password.*

1. Press **Enable shell access** and confirm the plan with **Enable**.
2. In **Add a public key**, choose that account and add your key.
3. When you are done, **Disable shell access** switches it off again.

**Key deployment** says whether `appliance.conf` allows keys to be added to it
from this console. Details are in
[SSH shell access](../../appliance/ssh-shell-access.md).

## What happens in the background

- The appliance's agent writes the key into the account's `authorized_keys`
  and checks what the running SSH server actually enforces, which is what the
  **Export access** card reports.
- A password set where the Appliance Manager could not confirm it holds back
  switching SSH on, shell access and adding a key until you confirm it under
  **Settings**; see [First start](first-start.md#one-password-for-everything).

## Expected result

A copy of your configuration and your backups is on your own computer, and the
appliance's backup account still can do nothing but read.

## Warnings and common problems

| Symptom | Meaning | What to do |
| --- | --- | --- |
| `scp` or `rsync` is refused | The account accepts SFTP only | Use `sftp` or an SFTP client |
| *Permission denied (publickey)* | The key is not on `ems-backup`, or the client offers another key | Check **Keys for ems-backup**; pass the key with `-i` |
| **Export access** is not **Set up** | The export is not in place, or sshd does not enforce the confinement | Read its detail; collect a support archive in **Diagnostics** |
| **Add key** is refused | The same key is already there, or the password still has to be confirmed | Check the key list; confirm the password under **Settings** |

## What this is not

It is a copy, not a restore mechanism. Putting a configuration back is done from
the EMS Admin console, which understands what the files mean; see
[Backup and restore](../admin/backup-restore.md).

## Recovery or next steps

- [SSH backup access, in technical detail](../../appliance/ssh-backup-access.md)
- [Updates](updates.md) — the backup matters most before one
- [When it stops working](recovery.md)
