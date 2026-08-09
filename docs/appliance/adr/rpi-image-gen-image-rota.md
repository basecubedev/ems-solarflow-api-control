# ADR: build the A/B appliance image with rpi-image-gen `image-rota`

Status: accepted
Date: 2026-08-07

## Context

The A/B appliance was first built against an assumed `rpi-image-gen` interface:
a checkout exposing `$GENERATOR/build.sh --config ... --output ...`, plus a
project-owned partition table in `packaging/appliance/image/manifests/layout.json`
with six fixed PARTUUIDs, project hooks that ran `sfdisk`, and a project-owned
`/persist` bind-mount scheme.

None of that matches the official tool.

`rpi-image-gen` at the pinned release `v2.7.0`
(`a7b6d4806183195f3efadb533f58c8e46393d057`) has:

- no `build.sh` at any point in the A/B-capable releases; the executable is
  `./rpi-image-gen` and the interface is
  `rpi-image-gen build -c <config> -S <srcdir>`;
- an official A/B image layer, `image-rota` (`image/gpt/ab_userdata`, layer
  version 5.5.1), which owns the whole GPT;
- an official shared-slot mechanism, `slot-shared`, driven by
  `/etc/rpi-image-gen/slot-shared.d/*.conf` and a systemd generator;
- an official update artifact, `update.tar.zst`, holding exactly two members
  named `boot` and `system`, both android-sparse images.

Continuing with the project's own layout would mean maintaining a second
partition-table authority, a second persistence framework and a second update
format alongside the ones Raspberry Pi ship and test.

## Decision

`image-rota` is the layout authority. The project supplies packages, units,
markers and shared-path declarations, and nothing else.

RAUC is **not** introduced. The native path is sufficient: `image-rota` provides
redundant slots, `rpi-ab-slot-mapper` provides runtime slot selection, and the
firmware's `autoboot.txt` `tryboot` mechanism provides the one-shot trial boot
this project's state machine was already written against.

### What upstream owns

| Concern | Upstream mechanism |
|---|---|
| Partition table, sizes, order | `image-rota` via `genimage` |
| Partition identities | `genimage`, generated per build |
| Slot naming | GPT `PARTLABEL`: `boot_a`/`boot_b`, `system_a`/`system_b`, `bootconfig`, `persistent` |
| Runtime slot selection | `rpi-ab-slot-mapper` → `/dev/disk/by-slot/{active,other}/{boot,system}` |
| Boot selector | `autoboot.txt` on the `bootconfig` partition, mounted at `/bootfs` |
| Persistent partition | `persistent.mount` → `/persistent` |
| Per-slot `/var` | `slot-perst-generator` → `/persistent/slots/system_<slot>/var` |
| Shared paths | `slot-shared-generator` → `/persistent/shared/<path>` bind mounts |
| Machine identity | `machine-id-sync.service` → `/persistent/common/etc/machine-id` |
| Update artifact | `post-image.sh` → `update.tar.zst` with members `boot`, `system` |

### What this project owns

- the Appliance Manager package and its units;
- the slot-shared declaration for its own paths;
- the OS build marker and the layout descriptor it installs into the rootfs;
- release signing, staging, inactive-slot writing and read-back verification;
- inactive-slot filesystem inspection before tryboot is armed;
- the trial-health gates, the commit and the fallback classification;
- new-slot Docker/Admin/EMS reconstruction.

## Consequences

### The root filesystem is read-only

`image-rota` mounts `/` read-only (`ext4` `ro` or `erofs`). Everything the
appliance writes must be a shared path, a per-slot `/var` path, or a tmpfs.
This is why `/etc/ems-appliance-manager` is a declared shared path rather than
an ordinary directory.

### Slot identity is by label, never by a pinned PARTUUID

Upstream generates partition GUIDs per build and mandates stable `PARTLABEL`s.
The project therefore binds slot authority to the label plus the physical
parent device, and discovers PARTUUIDs from the running device rather than
declaring them. Two appliance media on one bus no longer collide, which the
previous fixed-PARTUUID layout could not avoid.

### Both slots share one boot payload

Upstream builds a single slot pair, bit-for-bit identical, and selects the root
filesystem through `root=/dev/disk/by-slot/active/system` resolved in the
initramfs. There is no per-slot `cmdline.txt`, so the project no longer derives
one, and an update carries one `boot` payload written to whichever slot is
inactive.

### Upstream's shared-slot mechanism fails open; this appliance must not

The `slot-shared` generator guards every bind mount with
`ConditionPathIsDirectory` on the source, so a missing shared directory silently
degrades to the slot-local path. That is the right default for a general-purpose
image and the wrong one for this appliance: a silent fallback means every write
since the last flash is lost at the next slot switch.

The project therefore consumes upstream's mechanism and adds one fail-closed
verifier on top. `ems-appliance-persistence.service` proves each declared path
is really backed by the persistent partition, and the agent, web and health
units `Requires=` it. See
[`../ab-persistence-contract.md`](../ab-persistence-contract.md).

### `/etc/ssh` is not shared

Upstream's `openssh-server` layer declares `Path=/etc/ssh`, which shares the
distro's `sshd_config`, `moduli` and package-generated defaults between slots.
This project does not enable that declaration. Host keys live in an
appliance-owned directory under an already-shared path and are named by a
project drop-in, so machine identity survives a slot switch while each slot
keeps its own OS configuration.

### An incompatible generator is a refusal

`appliance/rpi_image_gen.py` verifies a checkout against
`packaging/appliance/image/rpi-image-gen.lock` before any build runs. A checkout
that is not the pinned contract produces `rpi_image_gen_incompatible`, and the
build stops. There is no fallback to the old project-owned generator, because a
silent fallback would produce an image whose layout nothing at runtime agrees
with.
