# ADR: ship a second image variant with one writable root

Status: accepted
Date: 2026-08-23

## Context

The appliance shipped exactly one image: `image-rota`, two boot slots, a
read-only slot root, and OS updates that replace a whole inactive slot and
commit only after a trial boot proves itself. That design earns its cost at a
generation change — a new Debian, a new kernel, a new layout — where a failed
update would otherwise brick a device an owner cannot reach.

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
writes a few megabytes. A/B also requires a signing key for every OS release,
because a slot image is a whole operating system arriving from the network.

The two update models are not two settings. They are two products, and the
choice belongs to the operator at flash time — before there is anything to
convert.

## Decision

Build and publish **two** image variants from one source tree:

| | `ab` | `single` |
|---|---|---|
| upstream image layer | `image-rota` | `image-rpios` |
| partition table | GPT, six partitions | MBR, boot + root |
| root filesystem | read-only, per slot | writable |
| kernel command line | `root=/dev/disk/by-slot/active/system ro` | `root=/dev/disk/by-slot/system rw` |
| OS patches | signed image rebuild | `apt`, unattended |
| Manager patches | slot update, ~877 MB | `.deb`, ~350 KB |
| failure recovery | trial boot, automatic rollback | none |
| signing key | required | not needed at all |

Neither replaces the other, and neither is a degraded form of the other. The
A/B image remains the default recommendation for an appliance nobody will be
able to reach; the single-slot image is for an owner who would rather patch
weekly and keep a backup.

### One table, not three answers

`appliance/image_variants.py` declares both variants and every fact that
follows from choosing one: the upstream image layer, the project layer, the
root device, whether the root is read-only, whether an A/B layout descriptor
exists, whether an update archive exists.

The build side, the image inspector and the booted appliance all read that
table. Three separate answers to "which variant is this" would drift, and the
one that drifted would be the one deciding a gate.

### What is *not* duplicated

The single-slot layer is a second file — upstream resolves a layer name to its
latest version only, refuses a colliding name and version, and derives the
overlay directory from the layer file's own stem, so one file cannot be two
layers. But everything in it that is not about slots is byte-identical to the
A/B layer, and a contract test keeps it that way:

- the `mmdebstrap` package list,
- the hook that deletes the host keys the build chroot generated,
- the hook that installs the `.deb` with `dpkg`,
- the build-marker fields other than `image_layer` and `layout_id`.

### What the single-slot image deliberately does not carry

- **No layout descriptor.** Its absence *is* the mechanism: the runtime
  discovers `MODE_SINGLE_SLOT`, refuses every mutating A/B plan with a reason,
  and reports `single_slot`. There is no descriptor this project could write
  that would describe an image with one root.
- **No shared-slot binds.** The seven bind mounts exist so a slot switch loses
  nothing. With one root there is nothing to lose and nothing to bind.
- **No host-identity unit.** The image ships no SSH host keys either way — a
  private key inside a public artefact is compromised whether or not anything
  reads it. On a read-only root, Debian's own `sshd-keygen.service` cannot run
  (`ConditionPathIsReadWrite=/etc/ssh`), which is why the A/B image needs its
  own producer onto the persistent partition. On a writable root that condition
  is true, the unit ships enabled, and it makes the pair on first boot.
- **No update archive, and no signing key.** There is no OS release transport
  to trust, so there is nothing to verify.

## Consequences

**The persistence verifier had to learn the difference.** It is anchored on the
build marker rather than on the layout descriptor, deliberately: the descriptor
lives inside one of the binds the verifier exists to prove, so a skipped bind
would take the descriptor with it and the one unit that fails closed would
quietly skip. Every appliance image writes that marker, so a single-slot image
satisfied the condition and was asked to prove a contract it does not have.
Both the agent and the web service `Requires=` that unit, so nothing would have
started.

The verifier now reads which image the marker names, and answers "not
applicable" only when the marker *positively* names a known image layer with no
A/B layout. Absent, empty or unrecognised is treated exactly as before. An
absence can therefore never turn the check off.

**`apt` needed no code change.** The refusal keys off
`os.statvfs("/").f_flag & ST_RDONLY` — the real mount state, not a flag — so a
writable root lifts it by itself.

**Recovery is the operator's.** A single-slot appliance that fails an OS patch
has no slot to fall back to. The documented recovery is to reflash and restore
a backup, and the installation guide says so before the download link, not
after it.

**Two artefacts per board.** Four images per release instead of two, four sets
of release evidence, and a build-authority record that names the variant so an
A/B authority can never vouch for a single-slot artefact.

## Alternatives considered

**One layer with a variant switch.** Not possible: see above — upstream's
resolver, its duplicate-layer refusal and its overlay-directory derivation all
key on the layer file itself, and both variants are built from one source root.

**Convert an installed appliance between variants.** Rejected. The partition
tables are not merely different sizes, they are different table types with
different partition counts. `docs/appliance/installation.md` already states
that a single-slot *installation* is never converted in place; the same is true
between image variants, and for the same reason.

**Keep A/B only and accept the write load.** This is what prompted the work:
roughly 877 MB written weekly to a consumer SD card, for a patch that `apt`
delivers in megabytes.

**Drop A/B and ship only the single-slot image.** Rejected. A/B is the only
shape that survives a failed generation change without physical access, and
that is exactly the case an unattended appliance in somebody's cellar has to
survive.
