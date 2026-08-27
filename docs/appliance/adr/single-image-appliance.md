# ADR: the appliance is one image with one writable root

Status: accepted
Date: 2026-08-27

Supersedes the two-variant decision of 2026-08-23 and the A/B decisions that
preceded it.

## Context

The appliance shipped `image-rota`: two boot slots, a read-only slot root, and
OS updates that replace a whole inactive slot and commit only after a trial boot
proves itself. That design earns its cost at a generation change — a new Debian,
a new kernel, a new layout — where a failed update would otherwise brick a device
an owner cannot reach.

It is the wrong instrument for a `libssl` patch.

Measured on the rpi5 artefact of release 0.1.0:

| | bytes |
|---|---|
| `.img` | 17,758,703,616 |
| `.img.xz` | 509,381,124 |
| `.update.tar.zst` | 328,795,420 |
| written to the card per A/B update (`boot` + `system`) | ~877,000,000 |

A weekly security rebuild therefore writes roughly 877 MB to a 32 GB SD card
whose slot partitions sit at fixed offsets, and takes as long as staging,
writing and verifying that much data takes. `apt full-upgrade` for the same CVE
writes a few megabytes.

For a while both were built, and an operator chose at flash time. That was worse
than either. Two image shapes meant two partition tables, two sets of release
gates, two build authorities, a persistence contract that applied to one of them,
and a runtime that had to ask which one it was booted from before answering
almost any question. The A/B half was also the half that had never booted on a
Raspberry Pi: no board had run it, and the boards that could were only two of the
three this project builds for, because a Raspberry Pi 3 cannot boot a GPT image
image-rota produces.

The cost was carried by every part of the system, and the benefit was carried by
a mechanism nobody had yet started.

## Decision

Build and publish **one** image: `image-rpios`, an MBR with a FAT boot partition
and one writable ext4 root, patched in place by `apt`.

| | |
|---|---|
| upstream image layer | `image-rpios` |
| partition table | MBR, boot + root |
| root filesystem | writable |
| kernel command line | `root=/dev/disk/by-slot/system rw` |
| OS patches | `apt` (unattended only if the operator enables `automatic_security_updates`, which defaults to false) |
| Manager patches | a signed `.deb`, installed on an operator's button |
| failed OS update | write the card again and restore a backup |
| failed Manager update | a deadline reinstalls the previous package |
| boards | rpi3, rpi4, rpi5 — three artefacts per release |

## Consequences

**A failed OS upgrade is recovered from a backup, not from a second slot.**
That is the real cost of this decision and it is not hidden anywhere: the user
documentation says it, the updates page says it, and the backup exists so that
saying it is honest. `apt` upgrading a running Debian is a well-understood
operation with a long record; the appliance does not attempt to improve on it.

**The Appliance Manager keeps a way back of its own.** It is the part most
likely to break the box, because it is the part that serves the page an operator
would fix the box from. It updates only on an operator's button, installs an
older package as readily as a newer one, and arms a deadline that reinstalls the
previous package if the new one does not prove itself — see
[manager-self-update.md](manager-self-update.md). Doing nothing does not confirm
an install there.

**The Raspberry Pi 3 stops being an exception.** It could not boot the A/B image
and could boot this one; now there is only one image and it boots on every board
this project builds for.

**Five state-schema axes are retired rather than removed.**
`persistent_paths`, `slot_layout`, `ab_state`, `runtime_record` and
`confirmed_authority` described state the A/B mechanism kept. Nothing writes them
any more, but `persistent_state.RETIRED_SCHEMAS` still declares them, frozen at
the last version that did. An appliance refuses any package that does not declare
an axis its own record names, and it refuses it *before* anything could be
installed to fix that — so dropping the axes would have made the first
post-removal package uninstallable on every existing appliance.

**Nothing is converted in place.** An appliance running the A/B image is not
migrated to this one; it is re-flashed, which erases the card, which is why the
backup comes first. No appliance had been flashed with either image on real
hardware when this decision was taken.
