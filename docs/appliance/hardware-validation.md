# Physical-hardware validation gate

The appliance is **not complete** until a real Raspberry Pi has passed the cases
below. Everything that can be proven without hardware — the image contract, the
build authority, the release gates, the boot-flow simulator — is covered by the
automated suites, and none of it substitutes for a physical boot.

Record every run in this file's results table with the board, the storage class,
the image build ID and the date. A case that was not run is recorded as
`NOT RUN`, never as a pass.

> **Historical note.** Much of what follows was written while this project built
> a second, A/B image with two boot slots and a trial-boot commit. That image was
> removed — see [adr/single-image-appliance.md](adr/single-image-appliance.md) —
> and its cases are moot. The runs recorded below are **not** rewritten: they are
> evidence of what was actually built and inspected on the dates they name, and
> editing them to match today's code would be the one thing this document exists
> to prevent. Read a row that mentions slots as history.

## CURRENT RELEASE CANDIDATE

The one authoritative status block. Every claim here names the exact revision it
was produced from; a stage reached at an older revision is a historical run and
is recorded further down, never here.

A real build vouches for **one source tree**. The release-build revision below
is what the current artefacts were built from; when it does not equal the branch
HEAD, the release status is stale and no artefact may be called hardware-ready.

**It is stale here.** Development has continued past the revision the artefacts
were built from, so nothing in the table below describes what a build from HEAD
would produce. Two consequences worth stating plainly, because the table alone
does not:

- `release_not_stale` is one of the twelve required readiness invariants, so
  while this block is stale the release is **not** physically ready, whatever
  `physical_ready` said at the revision it was computed at.
- The release-build revision is not reachable from this branch. History was
  rewritten after it was produced, and it survives only on a local backup ref
  that was never pushed, so a third party who clones `feat/appliance-manager`
  cannot check it out, cannot recompute its tree hash and cannot re-run the
  freshness check -- it fails with `project_source_unavailable`.

Everything below therefore reads as: *this is what a build from that revision
proved, at that revision.* Re-running the finalizer at the current HEAD is what
would make it a statement about the release.

<!-- CURRENT-RC-BEGIN -->
| Property | Value |
|---|---|
| Branch | `feat/appliance-manager` |
| Release-build revision (what the real images were built from) | `01a61335c35b3ffabca1121983f8f161c0e36f75` |
| Source tree | `sha256:695881c540e1b651e0b1c6f72ba4f54a09d13f00cc104440358b6bb5df990622` |
| Stale | **True** — development has continued past the revision above. The artefacts and their evidence are unchanged and still describe that revision; the working tree no longer matches it, so nothing here describes what a build from HEAD would produce |
| rpi-image-gen | `a7b6d4806183` v2.7.0, tarball form, digest verified from the lock |
| Builder environment hash | `sha256:67c5a0885b7307e9715087ca2d58c7cbebb012123fe3ea54ca36d1d7cc588d32` |
| Builder base image | `debian-13-genericcloud-amd64-20260803-2559.qcow2`, digest verified from the lock |
| Evidence | [`reports/appliance/2026-08-13-rc/`](../../reports/appliance/2026-08-13-rc/) |
| Real rpi5 build | **PASS** — build `20260813200255`, 17 758 703 616 byte image `sha256:3491820e0a2b…`, update `sha256:8fed57b2cf98…` |
| Second independent rpi5 build | **PASS** — build `20260813192741`, same revision and tree, different build id, media identifiers and image digest |
| Real rpi4 build | **PASS** — build `20260813203546`, image `sha256:411ab1df2173…`, inspected separately |
| Real image structural inspection | **PASS** — 6 image-rota partitions, 6 distinct PARTUUIDs, both GPT headers and entry arrays, no overlap, `sgdisk` as an independent oracle |
| Real image content inspection | **PASS** — 90 pass, 0 fail, **0 not run**, no mandatory check skipped, on *both* the rpi5 and the rpi4 image |
| Real read-only root | **PASS** — `ro` on both boot partitions and `ro,relatime,commit=30` in both slot fstabs, both images |
| Real persistence activation | **PASS** — `6 shared paths declared` / `6 shared paths activated` in both slot roots of both images; the six tracked links are what activate them |
| Real update inspection | **PASS** — 18 pass, 0 fail, 0 not run; manifest, both sparse members expanded and re-hashed, filesystem types, detached signature verified against the trusted key |
| Real sparse cross-check | **PASS** — `simg2img` agrees with this project's decoder on both members |
| Signed production release gate | **PASS** — 0 failed, 1 optional NOT RUN (`source-authority`, which needs generator build dependencies a workstation does not carry; **PASS** inside the builder guest) |
| Release attestation | **PASS** — `sha256:3900a148c96c…`, signed by `D16E8DE0B133BD8F7BF1E6CDA5D4C295127CB181`, verified with `gpgv` against the trusted keyring |
| Hardware validation kit | **PASS** — 22 files re-verified from a carried copy, trusted signer confirmed, no private key material |
| Runtime gate: SFTP | **PASS** — a real session under the effective policy that was reported; traversal, symlink **and hardlink** escape, writes, shell, `-L`/`-R`/`-D`/`-A` and an undeclared subsystem all refused |
| Runtime gate: NetworkManager fail-closed | **PASS**, 0 cases NOT RUN |
| Runtime gate: Docker reconstruction | **PASS** — 21 cases against a real daemon, including no registry at all, an appliance that never ran InfluxDB, a deliberately stopped EMS **and** a deliberately stopped InfluxDB, and persistent data surviving the rebuild |
| Runtime gate: package lifecycle | **PASS** — 30 cases against real containers |
| ARM64 generic guest (optional gate) | **PASS** — a real aarch64 guest under full emulation, 44 checks, 0 failures, verified base image, record on its own virtio-serial channel |
| Runtime gate roll-up | **PASS** — 5/5, and **no gate reports a case as NOT RUN** |
| Source bundle | **PASS** — written from the git object tree, round-tripped object by object, `6 symlinks preserved` |
| Read-only root write audit | **PASS** — 5 cases against a genuinely read-only root; nothing written outside the declared mutable set |
| Full regression at this revision | **PASS** — 10 986 passed, 12 skipped, 0 failed (`pytest -m "not docker"`) |
| Appliance browser E2E | **PASS** — 100 passed, Chromium and Firefox |
| Physical Raspberry Pi | **NOT RUN** — no board has booted either image. A Pi 3B+ is on hand, and it cannot boot the A/B image at all |
| Physical readiness | **NOT READY** — at the release-build revision `physical_ready=true` with twelve invariants held and none unmet (`physical_tested=false`); the branch has since moved past that revision, and `release_not_stale` is one of those twelve, so the verdict does not carry to HEAD |
<!-- CURRENT-RC-END -->

### What the previous run left open, and what closed it

The evidence committed before this run described `da37737`, and one thing about
it looked like a contradiction: the release evidence proved six tracked
persistence symlinks, and the archive handed to an independent review had none.

**It was neither a repository nor a working-tree defect.** All six are in `HEAD`,
in the index and in the working tree, as mode `120000` with the expected
targets. What loses them is the delivery path, and it is reproducible:

```text
tar czf  tree/     6 links preserved
tar czhf tree/     0 links, "file removed before it could be read" per link,
                   exit status 0
```

They are the only symlinks in the repository and they point into
`/run/systemd/generator/`, which exists only inside a booted appliance, so off
the appliance they dangle by design and `--dereference` skips them silently.
Both review archives carry exactly that signature: the directory present, the
six entries gone. Nothing was restored, because nothing was missing.

The defences already existed — the bundle is written from the git object tree
and refused unless it round-trips — and one binding was added: the tracked link
*names* are now asserted equal to the declared mount units, so six links
activating the wrong six paths fails instead of matching on a count. In the
images built for this run the same property is read back per slot as
`6 shared paths declared` / `6 shared paths activated`, on both profiles.

Two enumerated confinement cases were also closed. `sftp(1) ln` without `-s` is
a hardlink through the `hardlink@openssh.com` extension; a hardlink names an
inode rather than a path, so the chroot is not what bounds it and the read-only
export mount has to refuse it. The real session now asks that, alongside the
symlink form it already asked. And a deliberately stopped InfluxDB is now a
reconstruction case of its own: a stopped EMS is a required service the gate
accounts for, while InfluxDB is optional, so "not running" and "not deployed"
look alike and a trial that started it would have invented a state the source
slot never had.

### The ARM64 gate, and the two defects it found

`arm64_guest` is optional and does not gate `physical_ready`. It now **passes**:
a real aarch64 Debian 13 guest under `qemu-system-aarch64` 10.0.11 with AAVMF
firmware and full CPU emulation, 44 checks, 0 failures, exit 0, on a base image
verified against the digest the lock pins.

```text
result: PASS
reason: booted aarch64 guest, verified input
release_gate: pass
record_channel: dedicated
run_id: 2026-08-11T21:29:09Z-3370197
package_sha256: dc814ed543e32f50e4f79704846953270232ecc9d7d33cbd91759232770aefa6
base_image_sha256: 37f7b60e4128c33f5b4a94e30b9c4034e0aa2c567550b4d5cca2cf0437e9588f
```

It took two fixes, and the second could not be seen until the first was made.

**The record was going to a console that revokes it.** Three runs reported
`FAIL` with the guest printing `== install ==` and then nothing at all until its
exit marker. The tier was run from cloud-init with `> /dev/ttyAMA0 2>&1` — the
console the kernel, systemd and `agetty` share. `agetty` calls `vhangup()` when
it claims that console, which revokes every descriptor already open on it, so
the tier lost the rest of its output and died on its next write under `set -e`.
The exit marker survived only because the driver wrote it through a second,
fresh open. The amd64 driver runs the same tier over SSH, which is why it never
saw this.

The tier is no longer given a terminal. Its record is written to a file in the
guest and delivered once, on a fresh open after the tier has finished, to a
virtio-serial port nothing else writes to. The console keeps its boot log plus
an `APPLIANCE_EVIDENCE stage=...` heartbeat, so a guest that never finishes
still names the stage it stopped in, and `result.txt` records which channel the
record came from — reading it from the shared console is a labelled fallback,
never a silent one. Both `console.log` and `evidence.log` are preserved with
every run.

**The failure that then became readable.** Every install check passed except
one:

```text
ok      ems-appliance-agent.service: active
ok      ems-appliance-web.service: active
ok      agent_socket: /run/ems-appliance-manager/agent.sock mode 660
failed  web_to_agent: ems-appliance-web cannot use the agent socket: TimeoutError
```

Both units active, the socket present with the right mode, and a round-trip
that timed out. Asked directly in the same guest, with a budget nothing was
cutting short, the agent answered that same request correctly:

```text
connect_seconds: 0.0
reply_seconds: 22.2
ok: True
```

The probe gave each attempt 8 seconds and retried four times. A reply that takes
22 seconds is never seen by any number of attempts that each give up at 8 — which
is why the previous release's fix, spreading the budget over more attempts, could
not have worked. The deadline now grows with the attempts (8, 16, 32, capped at
64) instead of only the pause between them.

The probe also asked for `status.get`, which collects the whole overview: the
Docker daemon, `sshd`, NetworkManager, the Admin container. During a postinst
that is every one of those subsystems' cold start, and none of them is what
`web_to_agent` asks. It asks for the operation records the agent already holds.
Both halves matter on real hardware too: a loaded or slow board during first
boot is the same shape as an emulated one.

`arm64_guest` stays optional by design: a Raspberry Pi boots its own firmware
and its own kernel, so a generic UEFI guest can raise confidence and can never
be the proof. Readiness does not rest on it and no artefact is called
hardware-ready because of it.

## Verification stages

Each stage is a strictly stronger claim than the one above it. "Reached" here
means *reached at some revision*, with the revision named; whether it holds for
the current candidate is the table above, and only that table.

Several stages were reached on the A/B image and say so. They are what was
proven on the dates they name, about an artefact this project no longer builds,
and they are not claims about the image it builds now — for that, read
[the evidence table further down](#the-image-and-what-has-been-established-about-it).

| Stage | What it proves | Reached at |
|---|---|---|
| Simulation verified | The state machine, selector parser, layout authority, write-failure matrix and boot-flow simulator | Current |
| Real upstream config validated | Both hardware profiles resolve through rpi-image-gen's own `ConfigLoader` and `LayerManager` at the pinned revision, and the project layer's dependencies resolve beside upstream's | Current |
| Real upstream artefact fixture validated | The update path drives genuine Android Sparse containers through zstd, tar, the member allowlist, sparse validation, expansion and filesystem identification | **A/B image** — at the time it was built |
| Packaged system booted | The package installs and its units order, fail and recover under a real systemd, in a Debian Trixie guest that finished booting | Current — 2026-08-09 |
| Real image built | `rpi-image-gen build` produces an `.img` and `update.tar.zst` from a pinned source tree | **A/B image** — `50645a3`, three images (two rpi5, one rpi4) |
| Real image inspected | The partition table, labels and per-build identities of an image that was actually built | **A/B image** — `50645a3`, rpi5 and rpi4 |
| Real image contents inspected | The package and its exact version in both roots, the units enabled in both, six shared paths activated in both, the bootconfig selector, both boot partitions and a `root=` that names the active slot | **A/B image** — `50645a3`, 90 pass / 0 fail / 0 not run on both images |
| Real update artefact validated | Upstream's `update.tar.zst`, its Android Sparse members staged through the production allowlist extractor and cross-checked against `simg2img` | **A/B image** — `50645a3`, 18 pass / 0 fail / 0 not run |
| Signed production release | A manifest signed by a trusted key, verified cryptographically, with every production gate passed | **A/B image** — `50645a3`, signed by `D16E8DE0…` and verified with `gpgv` |
| Physical Raspberry Pi | The image boots a real board, the root grows to the medium, and the agent, the web service and Admin come up | **NOT REACHED** — no hardware |
| Real Pi boot verified | Everything below | **NOT REACHED** |

The last one is what this gate exists for. Nothing in the automated suites
substitutes for it, and nothing above it is evidence that a board boots.

## Historical — what the image-rota integration changed

*This section describes the removed A/B image. Nothing below it is a property of
the image this project builds now; it is kept because the runs recorded further
down were made against it.*

The layout, the slot mapping, the shared mounts and the update artifact are now
`rpi-image-gen`'s, so the hardware gate has to prove upstream's mechanisms on
this appliance's hardware, not only this project's state machine:

- `rpi-ab-slot-mapper` publishes `/dev/disk/by-slot/{active,other}/{boot,system}`
  from the booted partition's GPT label;
- `slot-shared-generator` binds every declared path, and **fails open** when a
  source directory is missing — the appliance's verifier must catch that;
- `slot-perst-generator` binds `/var` per slot, which is what makes
  `/var/lib/docker` slot-local and the slot bootstrap necessary;
- `machine-id-sync.service` keeps one machine identity across a slot switch.

None of these has been exercised on hardware yet.

## Why a simulator is not enough

The automated boot-flow simulator drives the same state machine the appliance
uses, but it models the firmware. It cannot prove that:

- the bootloader on a given board actually honours `tryboot_a_b=1`,
- `reboot '0 tryboot'` reaches the firmware as a one-shot flag,
- `/proc/device-tree/chosen/bootloader/tryboot` is populated on that firmware,
- a FAT write to the selector partition survives a power cut on that storage,
- the storage controller for NVMe or USB behaves like the SD path.

Those are firmware and storage properties. Only hardware answers them.

### What an emulated boot of the real image does reach (2026-08-21)

The kernel and initramfs were read out of the real rpi5 image's FAT boot
partition with `mtools` — no mount, no root — and booted under
`qemu-system-aarch64 -machine virt -cpu cortex-a76`. This is not a substitute
for hardware and does not close any case above. It moves one line:

- the image's own kernel runs: `6.18.39+rpt-rpi-2712`, 16K pages, to
  `Run /init as init process`
- the image's own initramfs runs, `systemd-udevd` starts
- **`scripts/local-premount/90-rpi-ab-root` executes** — the first time outside
  a fixture — and its fail-closed branch is confirmed on the real artefact:
  `FATAL: AB missing /dev/disk/by-slot/active/system - rebooting`. It does not
  guess, does not fall back to a partition, names what is missing and reboots.

The happy path is out of reach there, and not because of a defect.
`usr/bin/rpi-bootdev-tag` reads `/proc/device-tree/chosen/bootloader/boot-mode`
and `.../partition`, which only the Raspberry Pi bootloader writes; without it
no device is tagged `RPI_ONBOOTDEV`, so `99-rpi-01-abslot.rules` creates no
`disk/by-slot/*` links. Presenting the image as virtio and as NVMe both fail at
that same point. Reaching the happy path would mean forging those device-tree
nodes, which would test the forgery rather than the device.

## Required equipment

```text
one Raspberry Pi of each supported board:
  Raspberry Pi 3 or 3B+   (SD only)
  Raspberry Pi 4
  Raspberry Pi 5
one microSD card per board, 16 GB or larger
one USB SSD, 16 GB or larger        (Pi 4 and Pi 5)
one NVMe drive on a Pi 5 carrier    (Pi 5)
a serial adapter, for a boot that never reaches the network
a card reader, for reading the boot partition afterwards
```


### How the offline seed became usable

`runtime_seed_unaddressable` was the defect that made the seed dead weight: the
slot wrote the archives and then refused to use them, so an appliance with no
WAN fell back to the registry the seed exists to avoid.

The cause is a fact about Docker, not a bug in the caller. A repository digest
is a *registry's* name for a manifest, and `docker save` re-serialises that
manifest on the way out — the archive is an OCI layout whose `index.json` names
a manifest the saving host computed. The blob whose sha256 **is** the repository
digest is not in the archive at all, so nothing can read it back, and after
`docker load` the store answers "No such image" for `repository@sha256:...`.
Only a pull ever writes that mapping.

What does survive, byte for byte, is the image config digest — Docker's image
ID. It commits to `rootfs.diff_ids` and therefore to the exact layer content, so
it is an identity rather than a label. The chain the reconstruction now rests on:

1. **Online, at record time.** The image is present as `repository@digest`
   because it was pulled. `docker image inspect` reports the image ID for that
   digest, and *Docker* is the authority for the binding. It goes into the
   runtime record, inside the fingerprint the trial is gated on.
2. **The archive is hashed** and its sha256, size, platform and image ID are
   written beside it.
3. **Offline, in the trial slot.** The archive is re-hashed against the record
   before anything is loaded. After `docker load`, the store is inspected again
   — first for the recorded `repository@digest`, and if the store does not hold
   it, for the recorded image ID. That inspection has to answer with exactly
   that ID and a matching platform, or the seed is refused and the fallback
   still names the exact digest.
4. **The service is started from what was verified.** The recorded compose file
   is authority and is never rewritten; the verified reference is handed to
   compose as an overlay after it. Both possible values are content-addressed —
   the digest reference or the image ID — and a name that is neither is not
   written at all, so an overlay can never be how a mutable tag reaches a
   container.

The seed's own metadata is never the authority. It only says which image ID to
ask Docker about; Docker's inspection of its own store is what decides.

The record schema went from 3 to 4 for this. A record is written by the slot
being replaced and read by the slot replacing it, so across exactly this update
the reader is one schema ahead — and the trial is gated on the fingerprint the
*writer* computed. The fingerprint is therefore taken over the shape the record
declares, not the shape the running code writes. Without that, the update
introducing the schema would be the one update that could never commit, and
every retry would fail identically.

### Minimum supported medium: 16 GB

Not a recommendation. The image is about **8.25 GiB** — a 256 MiB boot
partition and an 8 GiB root — and the root grows to fill the medium on first
boot.

What the medium has to hold *after* that decides the floor: the Docker store the
seed archive is unpacked into, the EMS data and the operator's backups, about
**3.9 GiB** beyond the image. Those are measured rather than estimated; see
`reports/appliance/<run-id>/media-sizing.json` for the run they came from.

The enforced floor is **14,500,000,000 bytes**, below the nominal 16 GB because
media are marketed in decimal gigabytes and vendors differ by a few percent, so
a genuine 16 GB card must pass. It is declared in `appliance/media_sizing.py`
and recorded in every image's `minimum_media_bytes` build metadata.

The growth is a transaction: measure, mutate, verify, and only then record. A
card whose filesystem did not actually grow leaves no marker and is retried on
the next boot rather than reported as finished.

The power-cut cases require cutting power at the wall or with a switchable PDU.
Pulling the plug on a `poweroff` is not the same test.

## Case list

### Group 0 — build the image (prerequisite)

| # | Case | Expected |
|---|---|---|
| 0.0 | Actions → **Appliance image build** → Run workflow | Every requested board green, and an `.img.xz` plus its build authority as artefacts. This is the ordinary path; 0.1 to 0.5 are what it runs, and what to run by hand when it fails |
| 0.1 | `scripts/appliance-check-rpi-image-gen.sh --rpi-image-gen <checkout>` | PASS against the pinned revision |
| 0.2 | `scripts/appliance-build-rpi-image.sh --profile rpi5` on a builder with the upstream dependencies | PASS, image produced |
| 0.2b | The same with `--profile rpi4` and `--profile rpi3` | PASS; three separate artefacts, not one relabelled |
| 0.3 | `scripts/appliance-inspect-rpi-image.sh <image>` | PASS: an MBR with a FAT `boot` and an ext4 `root` |
| 0.3b | The same inspection's content findings | PASS with **no** mandatory NOT RUN: the package and its exact version in the root, the dpkg status, the build marker, the four enabled units and each one's program, the writable-root fstab line, no shipped host key, the runtime helpers, and `root=/dev/disk/by-slot/system rw` on the boot partition. No mount and no root: the Pi 5 root filesystem uses 16 KiB ext4 blocks that no 4 KiB-page kernel will mount |
| 0.4 | The `build-authority.json` the build wrote | Names the source form, the generator revision, the source tree hash and the SHA-256 of the image, `completed: true` |
| 0.5 | `scripts/appliance-release-gates.sh --mode builder` | PASS (builder qualification) |

### Group 1 — first boot and identity

The card is the only copy. Everything here is what a freshly flashed appliance
has to do before any of it can be trusted, and the growth in 1.4 is the one
partition change this project makes.

| # | Case | Expected |
|---|---|---|
| 1.1 | Flash the image for this board, boot it | It reaches a login prompt and answers on the network |
| 1.2 | `sudo ems-appliance image-check` | Confirms the medium was written from an image this project built. A `.deb` on somebody else's Raspberry Pi OS answers no, and that is what keeps the growth helper off it |
| 1.3 | `findmnt /` | The root is **writable**, on the partition the image was flashed to |
| 1.4 | `cat /var/lib/ems-appliance-manager/.root-grown` | `outcome=grown`, with the medium's byte counts. On a card no larger than the image, `already_filled`. A missing marker after a successful boot is a failure: the next boot would retry |
| 1.5 | `df -h /` | The root fills the medium, not the 8 GiB the image shipped |
| 1.6 | `systemctl is-active ems-appliance-agent ems-appliance-web` | Both active, and the console answers on port 8088 |
| 1.7 | `ls -l /etc/ssh/ssh_host_*` | Present, and created on this boot — the image ships no host key, and Debian's `sshd-keygen.service` is what makes them |
| 1.8 | First visit to the console | The password gate asks for a new password and its confirmation |
| 1.9 | `sudo ems-appliance status` | Names the installed manager version and the host it is on |
| 1.10 | Complete first-run setup, install Admin, configure EMS | Appliance and EMS reachable |
| 1.11 | Reboot normally | Everything comes back, data intact, and `.root-grown` is **not** rewritten |
| 1.12 | `ssh-keyscan` the appliance | The same host key as in 1.7, across the reboot |
| 1.13 | `systemctl status ems-appliance-manager-verify.timer` | Inactive. The deadline timer is armed by a manager install and by nothing else |

### Groups 2 to 4 — the A/B update cases

Removed with the mechanism they tested. What replaced them, on this image, is
`apt`: an ordinary Debian upgrade with no project-specific commit, trial or
fallback to prove. The cases that remain are in group 1 (does it boot, does the
root grow, do the services come up) and group 5 (does that hold on each storage
class).

The one update case this project still owns is the Appliance Manager's own
package, and it has its own row in the results table below: no appliance has
fetched and installed one over HTTPS, and the deadline in `manager_verify.py`
has never expired on a board.

### Group 5 — storage classes

Every case in group 1 is repeated per storage class. **A pass on one class is
never reported for another.**

| Class | Board | Status |
|---|---|---|
| microSD | Pi 3B+ | NOT RUN |
| microSD | Pi 4 | NOT RUN |
| microSD | Pi 5 | NOT RUN |
| USB SSD | Pi 4 | NOT RUN |
| USB SSD | Pi 5 | NOT RUN |
| NVMe | Pi 5 | NOT RUN |

## Which steps destroy something

Two operations in this gate are not reversible, and neither is wrapped in a
one-click script. Each is run by hand, so the operator can read it first.

| Marker | Operation | What it costs |
|---|---|---|
| **FLASH DESTROYS TARGET MEDIA** | Writing the image to the card, SSD or NVMe | Every partition on that device, including any appliance already on it. Confirm the device node immediately before writing; a wrong `of=` takes the workstation's own disk |
| **POWER CUT TEST** | Cutting power at the wall or PDU mid-write | Deliberate. May leave the medium needing a re-flash, which is the point of the case |

A third irreversible thing happens without an operator: the **first boot grows
the root partition** to fill the medium. It runs once, only on a medium
`ems-appliance image-check` recognises, and it is the only repartitioning
anything in this project does. A card flashed with an appliance image is
therefore already committed to it before case 1.1 finishes.

The read-only helpers below never do any of this. `capture-baseline` and
`collect-evidence` write no block device, restart no service and touch no SSH
key.

## Procedure for one storage class

```text
 0  Get an image for this board. Either the CI build --
      Actions -> "Appliance image build" -> Run workflow -- or, on a host with the
      generator's prerequisites:
        scripts/appliance-builder-vm.sh --profile rpi5 --output out/
 1  Verify what was downloaded before writing it to anything:
      sha256sum -c ems-solarflow-appliance-<version>-<board>-arm64.img.xz.sha256
 2  Record the build ID and the image sha256 from the build authority beside it.
 3  FLASH DESTROYS TARGET MEDIA — flash the image to the target medium from
      the second machine, after confirming the device node.
 4  Boot with the serial console attached and capture the log.
 5  Capture the baseline: scripts/appliance-hardware-capture-baseline.sh
 6  Run group 1.
 7  Run group 5 for this storage class, one case per boot, re-imaging between
      destructive cases. Contains POWER CUT TEST and FLASH DESTROYS TARGET MEDIA.
 8  Collect the evidence: scripts/appliance-hardware-collect-evidence.sh
 9  Record every result in the table above with the date and build IDs.
```

The build host needs upstream's dependency set — `mmdebstrap`, `podman`,
`uidmap`, `pv`, `btrfs-progs`, `dctrl-tools`, `python3-debian`,
`python3-jsonschema`, `flex` — and, when it is not itself arm64, a registered
`qemu-user-static` binfmt handler. `appliance-check-rpi-image-gen.sh` lists what
is missing and the build wrapper refuses to start without it, reporting
`rpi_image_gen_dependencies_missing` rather than producing a partial image.

`scripts/appliance-builder-vm.sh` provisions all of that inside a throwaway
guest, so none of it has to be installed on a workstation. Two things about the
build host are worth knowing before running the generator by hand:

- **Do not run it as root.** `bin/ns` is `#!/bin/sh` and evals a bash function
  definition when it is already root; on Debian, where `/bin/sh` is dash, the
  first layer dies with `[[: not found` and exit 127. The non-root path hands
  the same eval to bash through `podman unshare`. Rootless is the supported
  model — `uidmap` and `dbus-user-session` are in upstream's dependency list for
  it — and rootless podman needs a login session for its `XDG_RUNTIME_DIR`.
- **Put `/usr/sbin` on the build user's `PATH`.** Five declared binaries
  (`mkfs.btrfs`, `veritysetup`, `mkdosfs`, `mke2fs`, `fdisk`) live there, and
  Debian keeps it off a non-root `PATH`, so the dependency probe reports a fully
  provisioned machine as unusable.

For every power-cut case, record whether the appliance came back at all, and
what `/var/lib/ems-appliance-manager/.root-grown` contained afterwards. A card
cut mid-growth is the case that decides whether the growth transaction is one:
the marker is written after the filesystem grew, never before, so a boot that
was interrupted has to retry rather than report itself finished.

## Run history

Every block below is one dated run at one revision. None of them is the current
status: that is the CURRENT RELEASE CANDIDATE table at the top of this file, and
a stage reached here has to be reached again for a revision that changed the
code it depends on.

### Historical run — 2026-08-09, release-candidate hardening

| Gate | Result |
|---|---|
| Packaged runtime gate, real booted Debian Trixie guest (`appliance-smoke-vm-amd64.sh`) | PASS — pinned base image `debian-13-genericcloud-amd64-20260803-2559.qcow2`, install, verify-install, service ordering, state boundary, packaged HTTP authentication, audit trail, export root, reinstall, A/B units inert on a single-slot guest, host identity policy |
| Repository hygiene gate | PASS — 1103 tracked files, 0 rejected |
| Mount-independent image content inspection against real filesystems (16 KiB ext4, FAT12/32) | PASS — every mandatory content check, 0 NOT RUN |
| GPT structural verification, including the backup header and both entry-array CRCs | PASS |
| Slot-pairing payload equality (`system_a`/`system_b`, `boot_a`/`boot_b`) | PASS — a single changed byte is detected |
| Cryptographic detached-signature verification with an ephemeral test key | PASS — valid, wrong key, tampered manifest, tampered signature, missing signature and missing trust policy all behave |
| Transactional persistent growth | PASS — grow, already-filled, growpart failure, kernel-not-reread, resize2fs failure, filesystem-not-grown, retry after a partial growth |
| Hardware kit assembled from build authority | PASS — mixed builds, missing signature, missing gate report and a stale image all refused |
| **Real rpi4 / rpi5 image build from this revision** | **NOT RUN** — the review host has no `mmdebstrap`, `podman` or arm64 binfmt handler, and the disposable builder guest was not run to completion in this session |
| **ARM64 generic guest** | **NOT RUN** — `qemu-system-aarch64` is not installed on the review host |
| **Real Docker runtime reconstruction tier** | **NOT RUN** — the guest test images and the controlled local registry it needs are not built yet |
| **Real SFTP protocol confinement tier** | **NOT RUN** — see the guest smoke tier, which proves the drop-in and the export root but not a real SFTP session |
| **Physical Raspberry Pi** | **NOT RUN** — no hardware |

### Historical run — 2026-08-08, before the builder guest

What was run after the deployment-authority and build-provenance work. A result
that is not listed here was not produced.

| Gate | Result |
|---|---|
| A/B focused suite (`test_appliance_ab_*`, `build_provenance`, `source_bundle`, `host_identity*`, `rpi_image_gen*`) | PASS — 805 passed, 6 skipped |
| Full appliance suite (`-k appliance -m "not docker and not browser and not slow"`) | PASS — 1995 passed, 7 skipped |
| Full non-Docker regression (`-m "not docker and not browser"`) | PASS — 10415 passed, 12 skipped, 212 deselected, 32m30s |
| Docker tier (`-m docker`) | PASS — 40 passed, 172 skipped |
| Host-identity and permission suites as a normal user | PASS — 43 passed |
| The same suites under `unshare -r -m` (root namespace) | PASS — 43 passed |
| Appliance UI, Chromium | PASS — 49 passed |
| Appliance UI, Firefox | PASS — 49 passed |
| Package build and inspection, amd64 and arm64 | PASS — both carry every A/B unit, the growth helper and the new authority modules |
| `ruff`, `compileall`, `node --check` (both bundles), `git diff --check` | PASS |
| ShellCheck over every appliance shell script | PASS — informational findings only, all pre-existing |
| Test classification (`test_test_classification.py`) | PASS — 34 passed |
| Pinned upstream fetch (`appliance-fetch-rpi-image-gen.sh`, tarball form) | PASS — sha256:f10e70b5… (v2.7.0), tree sha256:9ae7e080… |
| Source authority against that real tree | PASS — every contract check; source_identity PASS |
| Tamper detection against that real tree (one line appended to `config/trixie-minbase-ab.yaml`) | PASS — FAIL `rpi_image_gen_source_modified` |
| Tree authority is stable across running upstream's own tooling | PASS — hash unchanged after the upstream tier imported `site/config_loader` |
| `appliance-verify-slot-mounts.sh` against that real tree | PASS — 6 declared, 6 generated, 6 activated |
| Source-bundle parity of the repository (`git archive` → checker) | PASS — 1088 tracked objects, 0 missing, 0 changed |
| `appliance-release-gates.sh` against that real tree | INCOMPLETE — 2 gates PASS, 7 truthful NOT RUN; strict mode exits 3 on this host, which is the point |
| Real update artifact (`tar -I zstd` with members `boot` and `system`) through the runtime parser, extractor and both release scripts | PASS |
| **Real `rpi-image-gen` image build** | **NOT RUN** — `rpi_image_gen_dependencies_missing` |
| **Real built-image inspection** | **NOT RUN** — no image to inspect |
| **Upstream-generated `update.tar.zst`** | **NOT RUN** — produced only by a real build |
| **Signed production release manifest** | **NOT RUN** — needs a real build authority and a signing key |
| **systemd-in-container tiers (SFTP confinement, packaged services)** | **NOT RUN** — "systemd did not finish booting in the container" |
| **QEMU / arm64 guest boot** | **NOT RUN** — no arm64 binfmt handler |
| **Physical Raspberry Pi** | **NOT RUN** — no hardware |

The build host was missing twelve of upstream's dependencies (`mmdebstrap`,
`podman`, `uidmap`, `pv`, `btrfs-progs`, `flex`, `dosfstools`, `e2fsprogs`,
`fdisk`, `cryptsetup`, `dctrl-tools`, `python3-jsonschema`) and had no registered
arm64 binfmt handler, so an aarch64 binary could not execute at all. Installing
either needs root, which was not available non-interactively.

The A/B page's deployment-drift rendering is covered by the frontend contract
tier rather than the browser tier: the Playwright fixture host is deliberately a
single-slot appliance, so the browser never renders the A/B view.

The simulated tiers are not a substitute for any of the NOT RUN rows.

## Results

| Date | Board | Storage | Image build | Group | Result | Notes |
|---|---|---|---|---|---|---|
| — | — | — | — | 0 | NOT RUN | The build host had 12 of upstream's dependencies missing and no arm64 binfmt handler; installing either needs root. Every source, contract and provenance gate in group 0 that does not need a build passed against the real pinned v2.7.0 tree. |
| — | — | — | — | 1–5 | NOT RUN | No Raspberry Pi hardware was available when this gate was written. |

The code-level scope is closed: every gate that does not require a builder host
or a physical board has been run and passed. What remains is a real image build
on a suitable builder, and then this table.

## The image, and what has been established about it

One image, one writable root, patched by `apt` (see
[adr/single-image-appliance.md](adr/single-image-appliance.md)). It builds, and
the artefacts it produces satisfy their contract. **Nothing about it has been
confirmed on physical hardware**, and none of the A/B results recorded above
make that true either: a different partition table, a different boot device and
a writable root are exactly the things a physical boot has to answer for.

This is the current evidence table. The rows above it are history.

| Case | Status | Evidence |
|---|---|---|
| `image-rpios` + `docker-debian-trixie` resolves and builds, **rpi5** | **PASS** | built three times in the builder VM on 2026-08-23/24; 28m12s for the first |
| the same, **rpi4** | **PASS** | built by the release-gate run of 2026-08-24 |
| The built images satisfy the single-slot contract | **PASS** | recorded by that gate run: rpi4 and rpi5 each 30 pass, 0 fail, 11 NOT RUN, **no mandatory check unanswered** |
| The single-slot release gates pass end to end | **PASS** | `appliance-release-gates.sh --variant single --profile rpi5 --profile rpi4`: `RESULT: PASS (builder qualification, 0 optional gate(s) NOT RUN)`. The slot-mount gate and every update-archive gate report NOT APPLICABLE with their reason rather than being dropped |
| The first-boot growth unit ships, is enabled, and its program is there | **PASS** | same artefact: the unit is linked into `multi-user.target.wants`, and `/usr/lib/ems-appliance-manager/grow-root.sh` is in the image |
| `image-rpios` + `rpi3` builds | **PASS** | built twice in the builder VM on 2026-08-26, at project revisions `9be2fea` and `4d3baa2`; 28m26s and 28m10s; both `RESULT: PASS (built ems-solarflow-appliance-0.1.0-rpi3-arm64-single.img)` |
| The built image satisfies the single-slot contract, **rpi3** | **PASS** | `appliance-inspect-rpi-image.sh` on both — run on 2026-08-26 under its pre-rename name and switch, `appliance-inspect-rpi-ab-image.sh --variant single`: **33 pass, 0 fail, 12 NOT RUN, all twelve optional and each with its reason**. MBR with two partitions, `kernel8.img`, 20 device-tree blobs, `enable_uart=1`, the arm64 package installed and every enabled unit's program present |
| The rescue account is in the flashed image | **PASS** | read out of the `4d3baa2` image's ext4 root: `ems-rescue`, uid 1001, shell `/bin/bash`, in the `sudo` group, and `/etc/shadow` holding exactly the shipped hash. `root` stays `*` |
| The manager's install and deadline units are in the flashed image | **PASS** | same image: `ems-appliance-manager-verify.service`, its `.timer`, `verify-manager.sh`, `rescue-account.sh` and `rescue-password.hash` are all present, and the timer is **not** enabled — arming is what enables it |
| The single-slot release gates pass end to end, **rpi3** | **INCOMPLETE** | four gates ran in the builder VM on 2026-08-26 at `c8deb3e` and each passed — `source-authority`, `source-bundle`, `build-rpi3` (`RESULT: PASS`) and `image-inspection-rpi3` (33 mandatory pass, 0 fail, 0 mandatory NOT RUN, 12 optional NOT RUN). The run was cut off before it wrote a verdict, so **there is no gate verdict for rpi3** and none may be claimed. The rpi4/rpi5 single-slot gate of 2026-08-24 is unaffected |
| The built image boots on a Pi 3B+ | NOT RUN | the board is available and the maintainer's live test is planned; nothing has been attempted yet |
| A Pi 3B+ runs Docker, Admin, EMS and InfluxDB in 1 GB of RAM | NOT RUN | **unmeasured**, and the memory table's "1 GB suffices" was written about other boards |
| The built image boots on a Pi 4 | NOT RUN | needs a Pi 4 or Pi 5; the available board is a Pi 3B+ |
| The built image boots on a Pi 5 | NOT RUN | as above |
| The root is genuinely writable when `ems-appliance-agent` starts | NOT RUN | needs a booted guest |
| The agent and the web service actually come up | NOT RUN | needs a booted guest |
| Debian's `sshd-keygen.service` produces the host keys on first boot | NOT RUN | the unit ships enabled with `ConditionPathIsReadWrite=/etc/ssh`, read out of the built rpi5 rootfs; that it fires is unproven |
| `apt full-upgrade` completes on the booted image | NOT RUN | the source-level refusal is lifted; that a real upgrade completes is a different claim |
| The root partition grows to the medium on first boot | NOT RUN | the transaction is covered by `tests/test_appliance_grow_root.py` against a fake medium; the real-tool tier skips on this host (the container cannot open the loop device's partition nodes), and growing the filesystem the script runs from needs hardware regardless |
| An image built in CI is the same thing | NOT RUN | `.github/workflows/appliance-image.yml` **has** been dispatched: 2026-08-27, all three boards green in 28.6 minutes wall clock. That establishes it builds; it does not establish that what it builds is the same artefact, because nothing compared the two and no board has booted either. It cannot produce a signable release by design — a hosted runner is not the approved builder |

### The A/B path after the shared build scripts were changed

The single-slot variant was added by teaching the *existing* builder, gate
runner and finalizer about variants rather than by copying them, so the A/B
image is now built by code that was edited. Re-proven on 2026-08-24 in the
builder VM:

`appliance-release-gates.sh --variant ab --profile rpi5 --profile rpi4` →
`RESULT: PASS (builder qualification, 4 optional gate(s) NOT RUN)`.

Both boards: `build`, `inspect-image`, `describe` and `inspect-update` PASS,
plus `source-authority`, `slot-mounts` and `source-bundle`. The recorded image
inspections are **96 pass, 0 fail, 0 NOT RUN** per board — a fuller inspection
than this workstation can produce, because the builder guest has `gdisk` and
the independent GPT oracle therefore runs there.

The four NOT RUN are environmental, and both kinds are optional in builder
mode: `sign-*` has no `--sign-key`, which is deliberate — a disposable builder
guest is the wrong place for a production key — and `crosscheck-*` reports
`external_decoder_unavailable`, because the guest has no Docker to run a second
sparse decoder in.

### What the real builds produced

| | single-slot | A/B, for comparison |
|---|---|---|
| `.img` | 8,866,758,656 B (8.26 GiB) | 17,758,703,616 B (16.5 GiB) |
| `.img.xz` | 254,230,972 B (242 MB) | 509,381,124 B (486 MB) |
| build time | 28m12s | — |

Read off that artefact, not inferred:

- `root=/dev/disk/by-slot/system` on the kernel command line, with `rw` and
  without `ro`. This is the composition that could not be checked any other
  way: the appliance layer appends `rw` during customize, and upstream's own
  `setup.sh` rewrites the `root=` token afterwards at image assembly. Both
  survived.
- `/etc/fstab` mounts `/` `rw,relatime,errors=remount-ro,commit=30`.
- No `ab-layout.json`, so the runtime discovers a single slot.
- `ems-appliance-agent.service` and `ems-appliance-web.service` installed and
  enabled; no host private key shipped.
- The root carries its own dpkg database — unlike an A/B slot root, where
  `/var` is bound per slot.

What *is* established without hardware, and how:

- The partition table, the boot device name and the writable root are asserted
  against a synthetic image built with `mkfs.ext4`/`mkfs.vfat` in
  `tests/test_appliance_single_slot_image_contents.py` — the inspector reads
  the image without mounting it, and a read-only root fails there.
- That upstream's `image-rpios` writes an `rw` root and points the kernel at
  `/dev/disk/by-slot/system` is asserted against the pinned upstream bytes in
  `tests/test_appliance_rpi_image_gen_upstream.py`.

Neither is a boot. Do not upgrade any row above without the evidence it names.

The root growth deserves naming separately, because it is the one thing here
that *writes to a partition table*, and because an earlier revision of this
document overstated what backs it.

**Neither variant's growth has been proven against real partitioning tools on
this host.** `tests/test_appliance_grow_persistent_real.py` exists for exactly
that and is the A/B twin's real-tool tier, but all seven of its cases *skip*
here: the disposable container cannot open the loop device's partition nodes
(`/dev/loopNpM: Can't open blockdev`), so `growpart` and `resize2fs` are never
reached. No release report records that tier passing either — the
"Transactional persistent growth — PASS" row further down describes the unit
tier, whose partitioning tools are fakes.

What *is* proven for both is the transaction around those tools: every failure
mode leaves no marker, so the next boot retries rather than recording a card as
finished that never grew. What is not proven is the tools themselves doing what
the transaction assumes — and for the root that includes the part no loop
device would answer anyway, growing the filesystem the script is running from.

Until a real single-slot image has booted and grown its own root, that step is
unproven, and a single-slot appliance simply uses the root the build sized.

What is proven without hardware: the helper refuses any medium that does not
*positively* say it was flashed from a single-slot appliance image, so a `.deb`
installation on somebody else's Raspberry Pi OS is never repartitioned; and a
growth that fails at any step leaves no marker, so the next boot retries rather
than recording a card as finished that never grew.
