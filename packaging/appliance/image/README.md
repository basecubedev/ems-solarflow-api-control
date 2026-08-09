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
profiles/            one build profile per board: rpi4-ab.yaml, rpi5-ab.yaml
shared/              everything the profiles have in common
layer/               the project layer and its rootfs overlay
assets/              files copied into the image verbatim
```

## One image per board

The device layer selects the kernel, the firmware and the SoC, so a Pi 5
image is not a Pi 4 image. Each profile names exactly one upstream device
layer and each artefact declares only the board class that layer is for:

```text
profiles/rpi4-ab.yaml   device layer rpi4   class pi4   ...-<version>-rpi4-arm64-ab.img
profiles/rpi5-ab.yaml   device layer rpi5   class pi5   ...-<version>-rpi5-arm64-ab.img
```

`rpi4` and `rpi5` are upstream's layer **names**; `pi4` and `pi5` are only the
directories they live in and do not resolve. `image-rota` accepts device
classes `cm4`, `pi4`, `cm5` and `pi5` only.

At runtime the appliance normalises its device tree to one of those bounded
board classes and refuses an artefact that does not list it. A board it
cannot identify blocks the update with `hardware_not_supported` rather than
being guessed at.

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
sign  <artifact>.manifest.json with the project OS-release key
publish the manifest, the .sha256 and the detached signature next to the artifact
never place a private key in CI configuration
```

`scripts/appliance-release-gates.sh` runs that sequence as one driver — fetch,
source authority, dependencies, build per board, inspect, sign, source-bundle
parity — reporting each gate as PASS, FAIL or a truthful NOT RUN with the
prerequisite it needs. It publishes nothing.

Strict is the default, and a release must use it. "No image was built" and "the
image is good" are not the same answer:

```text
every required gate PASS      RESULT: PASS         exit 0
a gate failed                 RESULT: FAIL         exit 1
a required gate never ran     RESULT: NOT RUN      exit 3
--allow-not-run, gates skipped RESULT: INCOMPLETE  exit 0, never the word PASS
```

`--allow-not-run` exists for exploring on a host without the builder
prerequisites. Production CI uses the default.

The runtime refuses an unsigned artifact in production; see
`appliance/os_releases.py`.

## Provenance is proven, not recorded

A release manifest names the rpi-image-gen revision that built it. That claim is
only worth something if the artefact came from that builder, so it is not copied
out of the lock:

```text
a completed build writes build-authority.json into its own output directory
  → the generator's source form, revision and tree hash
  → this project's full revision and tree hash
  → the package digest and the SHA-256 of the image and the update
  → completed: true
production signing verifies the artefact against exactly that record
  → update hash, profile, generator revision, generator tree hash,
    project revision, project tree hash, build id
```

Both trees, not just upstream's. `PROJECT_REVISION=$(git rev-parse --short HEAD)`
named the last commit and said nothing about the files the build packaged, so an
image built from a working tree with local appliance edits, a staged change, or
an untracked script under `packaging/` claimed the clean revision it was
branched from. `appliance/project_source.py` refuses that outright:

```text
project_source_dirty                uncommitted or staged changes
project_source_untracked            untracked files under appliance/, packaging/,
                                    scripts/, config/ or .github/
project_source_unavailable          the commit object is not in this repository
build_source_changed_during_build   either tree moved while the build ran
```

Neither tree may move during the build either. Both are hashed again after the
artefacts are collected, because a build is long enough to be edited during.

Anything else is refused, including an `update.tar.zst` edited after its build.
An artefact supplied with `--update` and no build authority stays supported for
a development bench, but only as one:

```text
provenance.verified   false
rpi_image_gen_revision  "unverified"
--sign-key            refused with provenance_unverified
```

The source tree itself is proven twice: once by the fetch or clone, and again
immediately before `./rpi-image-gen build` reads it. A git checkout must be at
the pinned commit with a clean tree, a clean index, the pinned commit object
present and nothing untracked under `bin/`, `config/`, `device/`, `image/`,
`layer/` or `site/`. A release tarball must still match the tree manifest
recorded beside its verified archive hash. Either failure is
`rpi_image_gen_source_modified` or `rpi_image_gen_source_unverified`, before a
single byte is built.

## Source bundles have to be the tracked tree

Persistence activation depends on symlinks tracked in git. An archive that
flattens them into regular files produces a tree that still builds, generates
six bind mounts, activates none of them, and discards every write to the shared
paths at the next slot switch.

```bash
scripts/appliance-check-source-bundle.sh <bundle.tar>
```

compares the bundle against `git ls-tree` object by object — content, file mode,
symlink mode and symlink target. Paths a bundle deliberately omits have to be
declared with `--exclude`; a silent omission and a dropped file are
indistinguishable from the far end.

## Update artefacts are Android Sparse containers

`image-rota`'s genimage configuration wraps both payloads in an
`android-sparse` container and `post-image.sh` packs those containers as the
`boot` and `system` members of `update.tar.zst`. A member's bytes are a chunk
table, not a filesystem.

So `scripts/appliance-build-rpi-ab-update.sh` reads each member's container and
records both identities in the signed manifest:

```json
"system": {
  "role": "root",
  "encoding": "android_sparse",
  "encoded_sha256": "sha256:…",
  "expanded_sha256": "sha256:…",
  "expanded_size": 4294967296,
  "filesystem": "ext4"
}
```

The appliance expands and verifies before writing, in `appliance/sparse.py`.
No `simg2img` is installed on the appliance; the Debian package that would
otherwise provide it is `android-sdk-libsparse-utils`.

## Building

```bash
# Fetch the pinned source. Verified before extraction; installs nothing.
scripts/appliance-fetch-rpi-image-gen.sh --into ../rpi-image-gen

# Check the source identity, the contract and this host's build dependencies.
scripts/appliance-check-rpi-image-gen.sh --rpi-image-gen ../rpi-image-gen

# Prove every declared shared path is generated *and* activated.
scripts/appliance-verify-slot-mounts.sh --rpi-image-gen ../rpi-image-gen

# One artefact per board.
scripts/appliance-build-rpi-ab-image.sh --profile rpi5 --rpi-image-gen ../rpi-image-gen
scripts/appliance-build-rpi-ab-image.sh --profile rpi4 --rpi-image-gen ../rpi-image-gen

# Describe and sign the update artefact the image build produced. The build
# authority the image build wrote is what makes signing possible at all.
scripts/appliance-build-rpi-ab-update.sh --profile rpi5 \
    --build-authority out/build-authority.json --sign-key <keyid>

# Or all of the above as one driver, with a truthful NOT RUN per missing
# prerequisite. Publishes nothing.
scripts/appliance-release-gates.sh --rpi-image-gen ../rpi-image-gen --fetch
```

A source tree that cannot prove which upstream revision it is fails with
`rpi_image_gen_source_unverified`; a host missing build dependencies reports
NOT RUN with the binaries, packages and binfmt registration listed separately.
Neither is ever a pass.

There is no containerized build path. Upstream v2.7.0 builds through
`podman unshare`, `mmdebstrap` and `genimage` on the host, and cross-building
arm64 needs an aarch64 `binfmt_misc` registration — a host-wide kernel setting a
container cannot supply without privileged access to the host.
