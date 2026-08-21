# Backups

Getting your configuration and data off the appliance.

## What there is to save

| | |
| --- | --- |
| **Configuration** | Your devices, limits and control settings |
| **Runtime state** | What the controller currently believes |
| **Backups** | Snapshots the Admin Console made |

An OS update does not touch any of it — it lives on an area shared by both
system slots. A backup protects against the card failing, which no software
mechanism can.

![The SSH and backup access page showing the account state and the exported directories](../../assets/screenshots/appliance/appliance-backup-access.png)

## Turning the export on

1. Open **Backup**.
2. Press **Activate**.
3. Add the public half of an SSH key. The appliance never asks for a private
   key and never generates one for you.

The account it enables is **read-only and confined**: it can see three
directories and nothing else, it has no shell, and it cannot write. If any part
of that confinement cannot be proved, activation switches the account off rather
than leaving it half-open.

> **After flashing, this needs one manual step.** The account is created when
> the image is built, but the record proving the appliance owns it cannot be
> written until the box actually runs. Until that is addressed, activation on a
> freshly flashed appliance reports that it does not own the account. See
> [When it stops working](recovery.md).

## Getting the files

**SFTP only.** Not `scp`, not `rsync` — the account is confined to an SFTP
session and other tools are refused.

```bash
sftp -i ~/.ssh/your-key ems-backup@ems-solarflow.local
```

Once connected:

```text
get -r /config
get -r /data
get -r /backups
```

A graphical client works too: FileZilla, WinSCP or Cyberduck, protocol **SFTP**,
user `ems-backup`, and your key file.

## Turning it off

Press **Disable**. The key material stays, so turning it on again does not need
a new key. Disabling is deliberately fail-closed: if it cannot prove the account
is off afterwards, it reports failure rather than success.

## What this is not

It is a copy, not a restore mechanism. Putting a configuration back is done from
the Admin Console, which understands what the files mean.

## Related

- [SSH backup access, in technical detail](../../appliance/ssh-backup-access.md)
