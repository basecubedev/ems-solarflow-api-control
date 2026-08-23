# ADR: native Raspberry Pi tryboot for A/B operating-system updates

- Status: accepted
- Date: 2026-08-07
- Scope: host operating-system updates on the EMS SolarFlow Raspberry Pi
  Appliance. Not Docker images, not EMS data, not bootloader EEPROM firmware.

## Context

The appliance needs host OS updates that cannot brick an installation that is
controlling real power hardware. Package-by-package `apt` upgrades cannot give
that: a half-applied upgrade, a kernel that does not boot or a firmware package
that breaks the boot chain leaves an appliance an operator has to reach
physically, and the appliance is usually installed next to an inverter rather
than next to a monitor.

The requirement is therefore an image-based update with two root slots, an
automatic return to the previous slot when the new one does not come up, and a
commit that only happens after the new slot has proven itself from inside.

Two families of implementation exist for Raspberry Pi:

1. The **native Raspberry Pi mechanism**: `autoboot.txt` with `tryboot_a_b=1`,
   a one-shot trial boot requested with `reboot '0 tryboot'`, and the firmware
   exposing what it did in `/proc/device-tree/chosen/bootloader/`.
2. A **third-party update framework**, principally RAUC, and to a lesser extent
   SWUpdate or Mender.

## Decision

Use the native Raspberry Pi tryboot A/B mechanism.

Use Raspberry Pi's `rpi-image-gen` A/B support to produce both the initial
appliance image and the update artifact, rather than writing a second image
layout generator. The Appliance Manager owns everything after the artifact
exists: local validation, staging, the inactive-slot write, the selector
transaction, health verification, commit and fallback classification.

Do **not** add RAUC.

## Why not RAUC

RAUC is a good fit for products that need one update framework across several
SoC families with different bootloaders. This appliance is not that product. The
concrete reasons:

- **It would be a second privileged update system.** The appliance already has
  exactly one privileged component with a fixed typed-operation allowlist, a
  durable operation store, an audit trail and a peer-credential-checked socket
  (see [../security-model.md](../security-model.md)). RAUC would introduce a
  second root service with its own D-Bus interface, its own bundle format, its
  own signature trust store and its own state, none of which flows through the
  existing operation model. The project rule is one owner per mutation path.
- **The fail-safe property comes from the firmware either way.** On Raspberry Pi,
  RAUC does not implement the boot fallback itself; it drives the same
  `autoboot.txt` / `tryboot` mechanism through a bootloader interface. Adding it
  buys an abstraction over a mechanism this project uses directly, not a
  different safety guarantee.
- **The trial-boot semantics are the interesting part, and they are ours.** What
  actually decides whether an appliance is safe is which health gates must pass
  before a commit — the persistent partition mounted, the agent socket usable,
  `verify-install` passing, the EMS installation still discoverable. Those are
  appliance-specific and would have to be written regardless of the framework.
- **Dependency surface.** RAUC pulls in a D-Bus service, a bundle tooling chain
  and a signing model into an appliance image whose whole point is to be small
  and auditable.

No blocking limitation of native tryboot was found for this scope. Specifically,
each requirement of this project is satisfied by the documented native
mechanism:

| Requirement | Native mechanism |
|---|---|
| Two independent boot + root slots | Two FAT boot partitions and two ext4 roots, selected by `boot_partition` |
| Default slot unchanged during a trial | `[all]` section keeps the current slot; only `[tryboot]` points at the target |
| One-shot trial | `reboot '0 tryboot'` sets a flag the firmware consumes for exactly one boot |
| The trial slot can tell it is on trial | `/proc/device-tree/chosen/bootloader/tryboot` |
| The trial slot can tell which partition booted | `/proc/device-tree/chosen/bootloader/partition` |
| Automatic fallback | An uncommitted trial simply does not survive the next boot |
| Atomic commit | Rewrite the small `autoboot.txt`, `fsync`, rename, re-read and re-parse |

## If a blocker is found later

If native tryboot turns out to be insufficient — for example if a supported
board's firmware does not expose the tryboot property, or the selector cannot be
written atomically on a given storage class — the response is:

1. Record the exact limitation, with the board, firmware version and evidence.
2. Write a superseding ADR in this directory. This one is then marked superseded.
3. Replace the mechanism. **Do not run two competing A/B mechanisms**: a second
   selector authority is precisely the failure this project's rules forbid.

Nothing may silently switch architecture.

## Consequences

- The initial move to A/B requires physically re-imaging onto an A/B appliance
  image. No in-place conversion of an existing single-slot installation exists,
  and none may be added: repartitioning a running appliance's storage from a
  browser is not a recoverable operation.
- Existing single-slot installations remain fully supported with package
  updates. They are a first-class mode, not a degraded one.
- The appliance depends on the Raspberry Pi bootloader's tryboot behaviour. That
  dependency is explicit, is checked at layout-discovery time, and produces
  `unsupported` rather than a broken update button where it does not hold.
- Anything that is not the host OS keeps its own mechanism: Docker images keep
  digest-pinned image rollback, EMS data keeps backup/restore, EEPROM firmware
  is reported read-only and never written here.
- Until a physical Raspberry Pi has passed the cases in
  [../ab-hardware-validation.md](../ab-hardware-validation.md), A/B support is
  not claimed as complete.
