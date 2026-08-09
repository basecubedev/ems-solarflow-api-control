# EMS SolarFlow A/B appliance image

Project-owned configuration for building the fail-safe A/B appliance image with
Raspberry Pi's own `rpi-image-gen`. The architecture is described in
[../../../docs/appliance/ab-os-updates.md](../../../docs/appliance/ab-os-updates.md)
and the decision to build on `image-rota` in
[../../../docs/appliance/adr/rpi-image-gen-image-rota.md](../../../docs/appliance/adr/rpi-image-gen-image-rota.md).

**`rpi-image-gen` is not vendored here.** This directory contains only the
configuration and the layer this project owns. The generator is supplied by the
build host, and the revision it must be is pinned in `rpi-image-gen.lock`.

## Layout

```text
rpi-image-gen.lock   the exact upstream revision and the contract it must satisfy
config/              the rpi-image-gen configuration
layer/               the project layer and its rootfs overlay
assets/              files copied into the image verbatim
```

## `image-rota` owns the disk

Nothing in this directory declares a partition, a size, a PARTUUID or a mount.
`image-rota` produces the whole GPT:

```text
bootconfig   vfat   /bootfs          autoboot.txt, the boot selector
boot_a       vfat   /boot/firmware   slot A boot filesystem
boot_b       vfat                    slot B boot filesystem
system_a     ext4   /                slot A root filesystem, read-only
system_b     ext4                    slot B root filesystem, read-only
persistent   ext4   /persistent      everything that survives a slot switch
```

Slot identity is the GPT **label**. Upstream's `rpi-ab-slot-mapper` reads the
booted partition's `PARTLABEL`, derives the active slot from its `_a`/`_b`
suffix and publishes `/dev/disk/by-slot/{active,other}/{boot,system}`. Partition
identities are generated per build, so two appliance media on one bus never
collide — which a fixed PARTUUID set could not avoid.

## Both slots start identical

`image-rota` builds one bit-for-bit identical slot pair: the same boot
filesystem and the same root filesystem in both slots, differing only in
partition identity. There is no per-slot `cmdline.txt`; every slot boots
`root=/dev/disk/by-slot/active/system` and the initramfs resolves it.

That is why an update carries one `boot` payload and one `system` payload, and
why the root filesystem must not contain a marker naming a slot.

## Shared state

The project declares its shared paths to upstream's `slot-shared` mechanism in
`layer/ems-appliance.rootfs-overlay/etc/rpi-image-gen/slot-shared.d/50-ems-appliance.conf`,
generated from `appliance/ab_persistence.py`. Upstream binds each declared path
from `/persistent/shared/<path>` at boot.

Upstream's generator guards every bind with `ConditionPathIsDirectory` and
therefore fails **open**. `ems-appliance-persistence.service` verifies the binds
and fails closed, and the agent, web, bootstrap and health units `Requires=` it.
See [../../../docs/appliance/ab-persistence-contract.md](../../../docs/appliance/ab-persistence-contract.md).

## Building

```bash
scripts/appliance-check-rpi-image-gen.sh --rpi-image-gen ../rpi-image-gen
scripts/appliance-build-rpi-ab-image.sh  --output out/
scripts/appliance-inspect-rpi-ab-image.sh out/ems-solarflow-appliance-*.img
scripts/appliance-build-rpi-ab-update.sh --output out/
scripts/appliance-inspect-rpi-ab-update.sh out/ems-solarflow-appliance-*.manifest.json
```

Every script checks its prerequisites first and reports `NOT RUN` with the
missing tool or a stable reason code when the host cannot build. A skipped build
is never a pass. A checkout that is not the pinned contract is
`rpi_image_gen_incompatible` and stops the build; there is no fallback.

None of these scripts push, publish, tag or upload anything.

## What a release pipeline must still do

```text
build the image and the update artifact on a clean builder
record the rpi-image-gen revision and the project git revision
sign  <artifact>.manifest.json with the project OS-release key
publish the manifest, the .sha256 and the detached signature next to the artifact
never place a private key in CI configuration
```

The runtime refuses an unsigned artifact in production; see
`appliance/os_releases.py`.
