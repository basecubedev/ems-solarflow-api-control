# A/B physical-hardware validation gate

A/B operating-system support is **not complete** until a real Raspberry Pi has
passed the cases below. Everything that can be proven without hardware — the
state machine, the selector parser, the layout authority, the write failure
matrix, the boot-flow simulator — is covered by the automated suites, and none
of it substitutes for a physical boot.

Record every run in this file's results table with the board, the storage class,
the image build ID and the date. A case that was not run is recorded as
`NOT RUN`, never as a pass.

## Why a simulator is not enough

The automated boot-flow simulator drives the same state machine the appliance
uses, but it models the firmware. It cannot prove that:

- the bootloader on a given board actually honours `tryboot_a_b=1`,
- `reboot '0 tryboot'` reaches the firmware as a one-shot flag,
- `/proc/device-tree/chosen/bootloader/tryboot` is populated on that firmware,
- a FAT write to the selector partition survives a power cut on that storage,
- the storage controller for NVMe or USB behaves like the SD path.

Those are firmware and storage properties. Only hardware answers them.

## Required equipment

```text
Raspberry Pi 4 and Raspberry Pi 5
one microSD card
one USB SSD
one NVMe drive on a Pi 5 carrier
a switchable power supply for the power-cut cases
a serial console (UART) — a Pi that will not boot shows why only here
a second machine to re-image from
```

The power-cut cases require cutting power at the wall or with a switchable PDU.
Pulling the plug on a `poweroff` is not the same test.

## Case list

### Group 1 — first boot and identity

| # | Case | Expected |
|---|---|---|
| 1.1 | Flash the A/B image, boot it | Slot A boots |
| 1.2 | `ems-appliance ab status` | `mode=ab`, `active_slot=A`, `known_good=A` |
| 1.3 | `ems-appliance ab verify-persistence` | passes, `/persistent` mounted, every shared path backed by it |
| 1.4 | Complete first-run setup, install Admin, configure EMS | Appliance and EMS reachable |
| 1.5 | Reboot normally | Slot A boots again, all data intact |

### Group 2 — a healthy update

| # | Case | Expected |
|---|---|---|
| 2.1 | Stage an update artifact | Written to inactive slot B, read-back verified |
| 2.2 | Check the selector before the trial | `[all]` still boot partition 2 |
| 2.3 | Trial-boot B | B boots, reports `tryboot=1`, health passes |
| 2.4 | Commit | `[all]` boot partition 3, `[tryboot]` boot partition 2 |
| 2.5 | Reboot normally | B boots as the default |
| 2.6 | EMS configuration and data | unchanged |
| 2.7 | SSH host key fingerprint | unchanged from before the update |
| 2.8 | Network settings, hostname, mDNS name | unchanged |
| 2.9 | Admin console reachable | yes |
| 2.10 | Appliance authentication | the same password still works |

### Group 3 — the next update, in the other direction

| # | Case | Expected |
|---|---|---|
| 3.1 | Stage into A while B is default | A written, B untouched |
| 3.2 | Trial-boot A, health passes, commit | A default, B rollback candidate |

### Group 4 — failure and fallback

| # | Case | Expected |
|---|---|---|
| 4.1 | Cut power while writing the inactive slot | The default slot still boots; the operation is `failed_recoverable`; the interrupted slot is never offered as bootable |
| 4.2 | Corrupt the inactive boot partition after staging, then trial | Trial fails or falls back; default unchanged |
| 4.3 | Corrupt the inactive root filesystem after staging, then trial | As 4.2 |
| 4.4 | Break a health gate in the target slot (stop the agent before the health service runs) | No commit, normal reboot returns to the previous default |
| 4.5 | Cut power during the trial boot, before commit | Next boot is the previous default; `fallback_observed` |
| 4.6 | Cut power during the commit write of `autoboot.txt` | Either the old or the new selector, both parse; no `manual_action_required` from a torn file |
| 4.7 | Trial boot where `/persistent` is missing | Health fails, no commit |
| 4.8 | Manual rollback to the previous known-good slot | Trial boot of the previous slot, health, commit |

### Group 5 — storage classes

Every case in groups 1, 2 and 4 is repeated per storage class. **A pass on one
class is never reported for another.**

| Class | Board | Status |
|---|---|---|
| microSD | Pi 4 | NOT RUN |
| microSD | Pi 5 | NOT RUN |
| USB SSD | Pi 4 | NOT RUN |
| USB SSD | Pi 5 | NOT RUN |
| NVMe | Pi 5 | NOT RUN |

## Procedure for one storage class

```text
 1  Build the appliance image:
      scripts/appliance-build-rpi-ab-image.sh --output out/
 2  Record the build ID and the image sha256 from the manifest.
 3  Flash the image to the target medium from the second machine.
 4  Boot with the serial console attached and capture the log.
 5  Run group 1.
 6  Build an update artifact from a second, slightly different build:
      scripts/appliance-build-rpi-ab-update.sh --output out/
 7  Run group 2, capturing `ems-appliance ab status --json` after every step.
 8  Run group 3.
 9  Run group 4, one case per boot, re-imaging between destructive cases.
10  Record every result in the table above with the date and build IDs.
```

For every power-cut case, record what the selector partition contained
afterwards (`ems-appliance ab status --json` plus a raw copy of `autoboot.txt`),
because that file is the whole safety argument.

## Results

| Date | Board | Storage | Image build | Group | Result | Notes |
|---|---|---|---|---|---|---|
| — | — | — | — | 1–5 | NOT RUN | No Raspberry Pi hardware was available when this gate was written. |
