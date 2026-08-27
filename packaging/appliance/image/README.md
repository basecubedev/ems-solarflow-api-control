# EMS SolarFlow appliance image

Project-owned configuration for building the appliance image with Raspberry
Pi's own `rpi-image-gen`.

| | |
|---|---|
| upstream image layer | `image-rpios` |
| partition table | MBR, boot and root |
| root | writable |
| OS updates | `apt` |

The architecture is described in
[../../../docs/appliance/os-updates.md](../../../docs/appliance/os-updates.md)
and the decision behind this shape in
[../../../docs/appliance/adr/single-image-appliance.md](../../../docs/appliance/adr/single-image-appliance.md).

**`rpi-image-gen` is not vendored here.** This directory contains only the
configuration and the layer this project owns. The generator is supplied by the
build host, and the revision it must be is pinned in `rpi-image-gen.lock`.

## Layout

```text
rpi-image-gen.lock   the exact upstream revision and the contract it must satisfy
profiles/            one build profile per board: rpi3.yaml, rpi4.yaml, rpi5.yaml
shared/              everything the profiles have in common
layer/               the project layer and its rootfs overlay
```

## One image per board

The device layer selects the kernel, the firmware and the SoC, so a Pi 5 image
is not a Pi 4 image. Each profile names exactly one upstream device layer and
each artefact declares only the board class that layer is for:

```text
profiles/rpi3.yaml   device layer rpi3   class pi3   ...-rpi3-arm64.img
profiles/rpi4.yaml   device layer rpi4   class pi4   ...-rpi4-arm64.img
profiles/rpi5.yaml   device layer rpi5   class pi5   ...-rpi5-arm64.img
```

Three artefacts per release. `rpi4` and `rpi5` are upstream's layer **names**;
`pi4` and `pi5` are only the directories they live in and do not resolve.

At runtime the appliance normalises its device tree against
`rpi_image_gen.BOARD_CLASSES`, which is a wider set than the profiles: it also
recognises `cm4` and `cm5` without shipping an image for either, so an operator
is told what their appliance is rather than being told nothing. A board it
cannot identify is reported as unknown rather than being guessed at.

## `image-rpios` owns the disk

Nothing in this directory declares a partition, a PARTUUID or a mount.
`image-rpios` produces the whole table:

```text
boot   vfat   /boot/firmware   firmware, kernel, cmdline.txt
root   ext4   /                the whole appliance, writable
```

The sizes are the one exception, and they are stated in
`shared/ems-appliance.yaml` because the root is the whole appliance: the OS,
Docker, the EMS deployment, its data and its backups. Absolute sizes rather
than upstream's percentage defaults, because a release's evidence has to be
able to name the number. The tail of a larger medium is claimed on first boot
by `ems-appliance-grow-root.service`, which is the one partition change this
project makes — and only ever on a medium carrying a build marker it wrote.

## Building

One image per board, and the board is not optional: a build without
`--profile` produces rpi5 only.

```bash
# Fetch the pinned source. Verified before extraction; installs nothing.
scripts/appliance-fetch-rpi-image-gen.sh --into ../rpi-image-gen

# Check the source identity, the contract and this host's build dependencies.
scripts/appliance-check-rpi-image-gen.sh --rpi-image-gen ../rpi-image-gen

# One artefact per board. A release is three images.
scripts/appliance-build-rpi-image.sh --profile rpi5 --rpi-image-gen ../rpi-image-gen
scripts/appliance-build-rpi-image.sh --profile rpi4 --rpi-image-gen ../rpi-image-gen
scripts/appliance-build-rpi-image.sh --profile rpi3 --rpi-image-gen ../rpi-image-gen

# Or all of the above as one driver, with a truthful NOT RUN per missing
# prerequisite. Publishes nothing.
scripts/appliance-release-gates.sh --rpi-image-gen ../rpi-image-gen --fetch
```

Every script checks its prerequisites first and reports `NOT RUN` with the
missing tool or a stable reason code when the host cannot build. A skipped
build is never a pass. A checkout that is not the pinned contract is
`rpi_image_gen_incompatible` and stops the build; there is no fallback.

A source tree that cannot prove which upstream revision it is fails with
`rpi_image_gen_source_unverified`; a host missing build dependencies reports
NOT RUN with the binaries, packages and binfmt registration listed separately.
Neither is ever a pass.

None of these scripts push, publish, tag or upload anything.

There is no containerized build path. Upstream v2.7.0 builds through
`podman unshare`, `mmdebstrap` and `genimage` on the host, and cross-building
arm64 needs an aarch64 `binfmt_misc` registration — a host-wide kernel setting a
container cannot supply without privileged access to the host.

## Release gates

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

## Provenance is proven, not recorded

A release attestation names the rpi-image-gen revision that built it. That
claim is only worth something if the artefact came from that builder, so it is
not copied out of the lock:

```text
a completed build writes build-authority.json into its own output directory
  → the generator's source form, revision and tree hash
  → this project's full revision and tree hash
  → the package digest and the SHA-256 of the image
  → completed: true
signing verifies the artefact against exactly that record
  → image hash, profile, generator revision, generator tree hash,
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

The source tree itself is proven twice: once by the fetch or clone, and again
immediately before `./rpi-image-gen build` reads it. A git checkout must be at
the pinned commit with a clean tree, a clean index, the pinned commit object
present and nothing untracked under `bin/`, `config/`, `device/`, `image/`,
`layer/` or `site/`. A release tarball must still match the tree manifest
recorded beside its verified archive hash. Either failure is
`rpi_image_gen_source_modified` or `rpi_image_gen_source_unverified`, before a
single byte is built.

## Source bundles have to be the tracked tree

A build can be handed a source archive rather than a checkout, and an archive is
easy to produce badly: a rewritten file mode drops the executable bit off a
build hook, and a flattened symlink becomes a regular file that still parses.
Either produces a tree that builds and is not this project.

```bash
scripts/appliance-check-source-bundle.sh <bundle.tar>
```

compares the bundle against `git ls-tree` object by object — content, file mode,
symlink mode and symlink target. Paths a bundle deliberately omits have to be
declared with `--exclude`; a silent omission and a dropped file are
indistinguishable from the far end.
