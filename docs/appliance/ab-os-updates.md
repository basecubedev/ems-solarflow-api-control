# Fail-safe A/B operating-system updates

This document defines how the EMS SolarFlow Raspberry Pi Appliance updates its
**host operating system**. It is the architecture reference; the operator-facing
description lives in [os-updates.md](os-updates.md).

The decision to build on the native Raspberry Pi `tryboot` mechanism rather than
on a third-party update framework is recorded in
[adr/ab-native-tryboot.md](adr/ab-native-tryboot.md).

## Three rollbacks that are not the same thing

The appliance already had two rollback mechanisms before this one existed. They
operate on different layers, recover from different failures, and none of them
substitutes for another.

| Mechanism | Layer | What it restores | Owner |
|---|---|---|---|
| **Admin image rollback** | Docker container | The previously running Admin container from its recorded image digest | `admin_lifecycle.py`, see [admin-recovery.md](admin-recovery.md) |
| **EMS backup / restore** | Application data | EMS configuration, runtime state and history from an EMS backup archive | EMS/Core and the Admin Console |
| **OS A/B rollback** | Raspberry Pi host OS | The previous known-good boot and root filesystem slot | `appliance/os_update.py`, this document |

Consequences that must stay true in code, UI and documentation:

- A/B **never** applies to Docker containers. An Admin update is still an image
  replacement with a digest-pinned rollback, and the word "slot" is not used for
  it.
- An OS A/B rollback restores the operating system, **not** EMS data. EMS
  configuration and data live on the shared persistent partition and are
  deliberately unaffected by a slot switch in either direction.
- An EMS restore does not repair a broken host OS, and an OS rollback does not
  undo an EMS configuration mistake.

A fourth mechanism exists on the hardware itself and is also not this one:

- **EEPROM A/B** is the Raspberry Pi 5 bootloader firmware's own redundant
  update path in SPI flash. It concerns the firmware that runs *before* any
  partition is read. This project reports EEPROM state read-only where it is
  useful for diagnosis and **never writes an EEPROM firmware slot**.

## Supported hardware

The A/B mechanism targets exactly this scope:

```text
board         Raspberry Pi 4 or later
os            Raspberry Pi OS Trixie, 64-bit
architecture  arm64
firmware      a bootloader that implements autoboot.txt and tryboot
storage       microSD, USB mass storage or NVMe — each one only where layout
              detection proves an A/B layout on that device
```

Nothing outside that scope is claimed. In particular:

- A storage class is never inferred from another. Passing on microSD says
  nothing about NVMe; see [ab-hardware-validation.md](ab-hardware-validation.md).
- Raspberry Pi 3 and earlier are not targeted: the appliance is arm64-only and
  the tryboot mechanism needs a current bootloader.
- Raspberry Pi Connect is not used. The appliance validates, stages, deploys and
  commits from its own host services; there is no cloud dependency in the update
  path.

## The layout

The layout is **not this project's**. It is produced by `rpi-image-gen`'s
`image-rota` layer, pinned in `packaging/appliance/image/rpi-image-gen.lock` and
explained in [adr/rpi-image-gen-image-rota.md](adr/rpi-image-gen-image-rota.md).

```text
GPT on a single device, produced by image-rota

  1  bootconfig   vfat   autoboot.txt only            /bootfs
  2  boot_a       vfat   firmware, kernel, config     /boot/firmware when slot A booted
  3  boot_b       vfat   firmware, kernel, config     /boot/firmware when slot B booted
  4  system_a     ext4   slot A root filesystem       / when slot A booted, read-only
  5  system_b     ext4   slot B root filesystem       / when slot B booted, read-only
  6  persistent   ext4   shared persistent state      /persistent
```

```text
slot A = boot_a + system_a
slot B = boot_b + system_b
```

The bootloader reads `autoboot.txt` from the **first** FAT partition and selects
a boot partition by number from it, which is why the selector is a partition of
its own: neither slot owns the file that decides which slot boots.

**Slot identity is the GPT label, never a PARTUUID this project pinned.**
`image-rota` generates partition identities per build and mandates stable
`PARTLABEL`s; upstream's `rpi-ab-slot-mapper` reads the booted partition's label,
derives the active slot from its `_a`/`_b` suffix, and publishes
`/dev/disk/by-slot/{active,other}/{boot,system}`. Two appliance media on one bus
are therefore distinguishable, which a fixed identity set could not manage.

There is no per-slot `cmdline.txt`. `image-rota` builds one bit-for-bit
identical slot pair, and every slot boots
`root=/dev/disk/by-slot/active/system`, resolved in the initramfs. An update
therefore carries one boot payload and one system payload, and a root filesystem
that named a slot would be a defect — the inactive-slot inspection refuses one.

`autoboot.txt` in its committed form, with slot A as the default:

```text
[all]
tryboot_a_b=1
boot_partition=2

[tryboot]
boot_partition=3
```

**Partition numbers are never guessed at runtime.** The numbers above are what
`image-rota` produces; the running system proves which partition is which by
matching the labels in the layout descriptor against block-device discovery on
the medium the firmware actually booted. See "Proving the active slot" below.

## Update states

An A/B operation is a durable record in the existing appliance operation store
(`appliance/operations.py`). There is no second operation database. The A/B
progress states live in the operation's stage field and in one shared
`ab-state.json` on the persistent partition, which is the only thing that
survives the reboot in the middle of the transaction.

```text
unsupported            this host is not an A/B appliance and never will be
single_slot            a normal single-root installation; package updates only
ready                  A/B layout proven, no operation in flight
staging                artifact downloaded and being verified
writing_inactive       inactive boot and root are being written
verifying_inactive     written images are being read back and checked
ready_for_tryboot      inactive slot verified, selector not yet armed
tryboot_requested      selector armed, reboot requested
booted_trial           the target slot is running under tryboot
health_verifying       the trial slot is proving itself
committing             the selector is being switched to the target slot
committed              the target slot is the default; the old slot is the
                       rollback candidate
fallback_observed      the source slot booted again with an uncommitted trial
failed_recoverable     the operation can be re-planned after a re-stage
manual_action_required the selector or slot state cannot be proven
failed_terminal        the operation ended and nothing else will happen
```

Mapping onto the generic operation model:

| A/B state | Operation state |
|---|---|
| `staging`, `writing_inactive`, `verifying_inactive`, `ready_for_tryboot`, `tryboot_requested`, `committing` | `running` |
| `booted_trial`, `health_verifying` | `verifying` |
| `committed` | `succeeded` |
| `fallback_observed`, `failed_recoverable` | `failed_recoverable` |
| `manual_action_required` | `manual_action_required` |
| `failed_terminal` | `failed_terminal` |

## The one invariant

```text
before a verified commit:   the default boot slot is unchanged
after a verified commit:    the new slot is default and the old slot is the
                            recorded rollback candidate
```

Every design decision below follows from it. The default selector is only ever
written after the target slot has booted, proven itself and asked to commit
itself. A trial boot is one-shot: if the target slot never reaches its commit,
the next ordinary boot returns to the previous default with nothing changed.

Where the selector state cannot be proven, the operation becomes
`manual_action_required`. Nothing guesses which slot is safe.


## Shared state

The persistent partition is mounted at `/persistent` and every shared path is
bound over its normal location by `rpi-image-gen`'s `slot-shared` generator, so
an operator never sees a different path depending on which slot booted.

The full classification — what is shared, what is slot-local, what is
reconstructed, and the machine-identity, SSH and Docker decisions — is in
[ab-persistence-contract.md](ab-persistence-contract.md). It is not restated
here.

The one property worth repeating: upstream's generator fails **open**, skipping
a bind whose source directory is missing. `ems-appliance-persistence.service`
therefore verifies the binds and fails closed, and the agent, web, bootstrap and
health units `Requires=` it.

## Proving the active slot

Authority for "which slot am I" is never a single signal. `appliance/ab_layout.py`
collects independent ones and requires them to agree:

```text
/proc/device-tree/chosen/bootloader/partition   the boot partition the firmware used
/proc/device-tree/chosen/bootloader/tryboot     whether this is a one-shot trial boot
/proc/cmdline                                   root=/dev/disk/by-slot/active/system
findmnt                                         what is mounted at / and /boot/firmware
lsblk --json, blkid                             the partitions of the boot device
/etc/ems-appliance-manager/ab-layout.json       the image-build layout descriptor
/dev/disk/by-slot/{active,other}/*              upstream's slot mapper symlinks
selector partition autoboot.txt                 the default and trial boot partitions
/etc/ems-appliance-slot                         the slot marker inside this rootfs
```

Any disagreement produces `layout_drift`, which disables every A/B mutation and
leaves the appliance in a read-only reporting mode. Drift is a refusal, never a
best-effort guess.

The inactive slot is proven separately, before anything is written to it:

```text
its boot partition is not the active boot partition
its root filesystem is not mounted read-write anywhere
its root filesystem is not the active root device
both of its partitions are on the same approved physical device
both partitions are at least as large as the artifact's images
```

"The other partition number" is never sufficient on its own.

## Single-slot installations

A normal Raspberry Pi OS installation reports:

```text
mode           single_slot
ab_supported   false
reason         ab_layout_not_present
```

The System Updates page then keeps its existing package-update behaviour and
says that image-based host updates require re-imaging onto an A/B appliance
image. There is no partial or disabled A/B control, and there is **no in-place
conversion**: the appliance never resizes, moves or repartitions a running
installation's storage, and no such action is reachable from the browser or the
agent. See [installation.md](installation.md).

## Where the code lives

| Module | Responsibility |
|---|---|
| `appliance/ab_layout.py` | Read-only slot and partition discovery, drift detection |
| `appliance/ab_persistence.py` | The shared-path contract and its read-only verifier |
| `appliance/os_releases.py` | The release index, signature and compatibility authority |
| `appliance/os_artifacts.py` | Bounded, traversal-safe `.tar.zst` extraction |
| `appliance/os_update.py` | Planning, staging and the inactive-slot write |
| `appliance/ab_boot.py` | The `autoboot.txt` parser/serializer and the tryboot transaction |
| `appliance/ab_health.py` | Trial-boot detection, health gates, commit and fallback |
| `appliance/ab_blocks.py` | The block-device backend and its fake for tests |

## Related documents

- [adr/ab-native-tryboot.md](adr/ab-native-tryboot.md) — why native tryboot
- [ab-hardware-validation.md](ab-hardware-validation.md) — the physical-hardware gate
- [os-updates.md](os-updates.md) — the operator-facing update page
- [installation.md](installation.md) — imaging and first boot
- [security-model.md](security-model.md) — the privilege boundary
- [troubleshooting.md](troubleshooting.md) — recovery from a wedged update
