# Operating-system updates

Open **System Updates**.

## What the check reports

The update check is read-only: it never modifies a package or a package index.

| Item | Meaning |
|---|---|
| Security updates | Packages whose candidate comes from a security archive |
| Normal package updates | Everything else that is upgradable |
| Held packages | Packages pinned with `dpkg` hold |
| Kernel update | A `linux-image*` / `raspberrypi-kernel` upgrade is pending |
| Firmware update | A `raspi-firmware` / bootloader / `firmware-*` upgrade is pending |
| Reboot required | `/var/run/reboot-required` exists, with the packages that set it |
| Package-manager health | dpkg consistency and whether another package manager holds the lock |

## Install security updates

Basic mode offers **Install security updates**. Only the packages the appliance
itself parsed out of a simulated apt run are upgraded — the browser never sends
a package name.

Before installing, the plan shows:

```text
01 Free disk space
02 dpkg state
03 apt lock state
04 any appliance operation already running
05 the package summary
06 an explicit confirmation
```

Blockers stop the confirmation: an active package-manager lock, an interrupted
dpkg run, or insufficient free space.

During installation the operation reports its stage, captures bounded output and
prevents a second package operation. Afterwards it runs a dpkg consistency
check, detects the reboot requirement, reports the changed package count and
shows failures explicitly.

## Install all updates (Expert mode)

Expert mode adds **Install all available OS updates**. It uses the same plan,
confirmation and verification path.

## Package-manager recovery (Expert mode)

Three strictly defined actions:

| Action | What it runs |
|---|---|
| Complete pending package configuration | `dpkg --configure -a` |
| Repair package dependencies | `apt-get -y -f install` |
| Refresh package indexes | `apt-get update` |

There are no free-form apt arguments. **A real active package-manager lock is
never removed** — the operation refuses with `package_lock_held` and asks you to
wait for the other package manager to finish.

## Major OS upgrades

Unattended distribution upgrades (for example Bookworm → Trixie) are
deliberately not supported. For a major OS generation change:

1. Create or export an EMS backup (EMS Admin Console).
2. Flash the new supported appliance image.
3. Restore the EMS backup.

## Reboot and shutdown

**Overview → Power** offers *Restart Raspberry Pi* and *Shut down*. Before
either, the plan shows the running host operations, the EMS and Admin state and
warns when a package installation is active. An active package operation blocks
the confirmation. After a reboot request the UI shows a reconnect screen and
checks periodically whether the appliance is reachable again.

## From the console

```bash
sudo ems-appliance status        # includes the security-update count and reboot flag
```
