# Operating-system updates

Open **System Updates**. The page has two modes, and an appliance is in exactly
one of them:

| Installation | Host updates |
|---|---|
| **Single-slot** — a normal Raspberry Pi OS root filesystem | Classic package updates. A major OS generation change requires re-imaging. |
| **A/B appliance image** | Image-based fail-safe host updates: staged into the inactive slot, trial-booted, health-checked, then committed or automatically rolled back. |

The mode is detected, never chosen: an appliance without an A/B layout reports
`single_slot` and keeps the package-update behaviour described below. **The move
to A/B requires physically re-imaging onto an A/B appliance image.** Nothing in
the browser or the agent repartitions a running installation, and no feature to
do so exists. See [ab-os-updates.md](ab-os-updates.md).

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

## A/B image-managed appliances

On an appliance built from an A/B image the page shows the slot state instead of
a package list:

```text
OS image update available
Current slot            A or B
Current OS build        the build id in the running root filesystem
Inactive slot           where the next update is staged
Last known-good slot    what a rollback would return to
Trial status            whether a trial boot is pending or running
Update readiness        every production prerequisite, one line each
```

*Update readiness* is a bounded set: the board class, the A/B layout, the
persistent data, the artifact decoder, the sparse decoder, the persistent host
identity and the container-runtime record. While any of them is missing the plan
buttons are disabled and the page says which one and why — an update that cannot
be decoded, written or recovered from is not one to offer.

The update path is:

```text
plan → confirmation → stage and verify the archive's members
     → validate each Android Sparse container → expand it → verify the expanded
       digest → write the inactive slot → verify by read-back
     → arm a one-shot trial boot → reboot → health check
     → commit, or return to the current slot
```

Everything up to and including the expansion happens on the persistent
partition. The inactive slot stops being a rollback candidate only immediately
before the first destructive byte, so power loss during staging or conversion
leaves both slots exactly as they were.

The health check is what decides. It requires the Docker daemon usable, the
Admin container running at the exact digest the previous slot recorded and its
loopback endpoint answering, and EMS in the state it was in before the update —
running again if it was running, still stopped if an operator had stopped it.

The default boot slot does not move until a booted target slot has proven
itself. If it does not, the next ordinary boot returns to the current slot with
nothing changed, and the page reports the fallback. **Nothing is retried
automatically**: a new plan and a new confirmation are required after the
inactive slot has been staged again.

`apt upgrade` is **not** the normal update path there. A live package mutation on
an image-managed host creates slot drift and can disappear after a rollback, so
package-manager recovery stays available in Expert mode only for repairing a
broken active slot, and is labelled as recovery.

### Rolling the operating system back

*Roll back the operating system* targets only the recorded previous known-good
slot — the one whose exact build and digests were written when it was promoted.
It trial-boots that slot and commits it only when it proves itself, exactly like
an update. There is no arbitrary historical image and no direct selector flip.

### From the console

```bash
sudo ems-appliance ab status
sudo ems-appliance ab verify-persistence
sudo ems-appliance host-identity
sudo ems-appliance verify-install
```

## Major OS upgrades

Unattended distribution upgrades (for example Bookworm → Trixie) are
deliberately not supported. For a major OS generation change:

1. Create or export an EMS backup (EMS Admin Console).
2. Flash the new supported appliance image.
3. Restore the EMS backup.

On an A/B appliance a major OS generation change arrives as a normal image
update, because the whole root filesystem is replaced rather than upgraded in
place.

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
