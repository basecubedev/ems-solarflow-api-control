# A/B physical-hardware validation gate

A/B operating-system support is **not complete** until a real Raspberry Pi has
passed the cases below. Everything that can be proven without hardware — the
state machine, the selector parser, the layout authority, the write failure
matrix, the boot-flow simulator — is covered by the automated suites, and none
of it substitutes for a physical boot.

Record every run in this file's results table with the board, the storage class,
the image build ID and the date. A case that was not run is recorded as
`NOT RUN`, never as a pass.

## CURRENT RELEASE CANDIDATE

The one authoritative status block. Every claim here names the exact revision it
was produced from; a stage reached at an older revision is a historical run and
is recorded further down, never here.

A real build vouches for **one source tree**. The release-build revision below
is what the current artefacts were built from; when it does not equal the branch
HEAD, the release status is stale and no artefact may be called hardware-ready.
It is not stale here: `release-result.json` reports `stale: false`, because the
checkout is the exact revision and tree the artefacts were built from.

<!-- CURRENT-RC-BEGIN -->
| Property | Value |
|---|---|
| Branch | `feat/appliance-manager` |
| Release-build revision (what the real images were built from) | `01a61335c35b3ffabca1121983f8f161c0e36f75` |
| Source tree | `sha256:695881c540e1b651e0b1c6f72ba4f54a09d13f00cc104440358b6bb5df990622` |
| Stale | **False** — the checkout is the exact revision and tree the artefacts were built from |
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
| Physical Raspberry Pi | **NOT RUN** — no hardware |
| Physical readiness | **READY** — `physical_ready=true`, twelve readiness invariants hold, none unmet; `physical_tested=false` |
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

| Stage | What it proves | Reached at |
|---|---|---|
| Simulation verified | The state machine, selector parser, layout authority, write-failure matrix and boot-flow simulator | Current |
| Real upstream config validated | Both hardware profiles resolve through rpi-image-gen's own `ConfigLoader` and `LayerManager` at the pinned revision, and the project layer's dependencies resolve beside upstream's | Current |
| Real upstream artefact fixture validated | The update path drives genuine Android Sparse containers through zstd, tar, the member allowlist, sparse validation, expansion and filesystem identification | Current |
| Packaged system booted | The package installs and its units order, fail and recover under a real systemd, in a Debian Trixie guest that finished booting | Current — 2026-08-09 |
| Real image built | `rpi-image-gen build` produces an `.img` and `update.tar.zst` from a pinned source tree | Current — `50645a3`, three images (two rpi5, one rpi4) |
| Real image inspected | The partition table, labels and per-build identities of an image that was actually built | Current — `50645a3`, rpi5 and rpi4 |
| Real image contents inspected | The package and its exact version in both roots, the units enabled in both, six shared paths activated in both, the bootconfig selector, both boot partitions and a `root=` that names the active slot | Current — `50645a3`, 90 pass / 0 fail / 0 not run on both images |
| Real update artefact validated | Upstream's `update.tar.zst`, its Android Sparse members staged through the production allowlist extractor and cross-checked against `simg2img` | Current — `50645a3`, 18 pass / 0 fail / 0 not run |
| Signed production release | A manifest signed by a trusted key, verified cryptographically, with every production gate passed | Current — `50645a3`, signed by `D16E8DE0…` and verified with `gpgv` |
| Physical Raspberry Pi | The image boots a real board, and A/B update, rollback and persistence hold on hardware | **NOT REACHED** — no hardware |
| Real Pi boot verified | Everything below | **NOT REACHED** |

The last one is what this gate exists for. Nothing in the automated suites
substitutes for it, and nothing above it is evidence that a board boots.

## What the image-rota integration changed

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

## Required equipment

A Raspberry Pi 3 or 3B+ **cannot** stand in for either board. It is not a
question of speed: the A/B image is GPT with an EEPROM-read boot selector, and a
Pi 3 boot ROM reads neither, so it never reaches a bootloader and none of the
cases below can be attempted on it. See
[adr/raspberry-pi-3-ab-support.md](adr/raspberry-pi-3-ab-support.md).

```text
Raspberry Pi 4 and Raspberry Pi 5
one microSD card, 32 GB or larger
one USB SSD, 32 GB or larger
one NVMe drive on a Pi 5 carrier, 32 GB or larger
a switchable power supply for the power-cut cases
a serial console (UART) — a Pi that will not boot shows why only here
a second machine to re-image from
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

### Minimum supported medium: 32 GB

Not a recommendation. The image is about **16.5 GiB** — two 4 GiB slot roots,
two 256 MiB boot partitions, a bootconfig partition and an 8 GiB persistent
partition — and a card marketed as "16 GB" holds roughly 14.8 to 15.9 GiB of
addressable bytes. It cannot hold the image at all.

What the medium has to hold after first boot decides the rest. The persistent
partition carries both slots' Docker stores, the seed archives an offline
reconstruction is rebuilt from, a staged OS update, the EMS data and the
operator's backups: **about 11.2 GiB**, against the 8 GiB the image ships. The
measured total requirement is **about 21.2 GB**, so the smallest standard
medium that satisfies it is 32 GB.

The enforced floor is **30,000,000,000 bytes**, below the nominal 32 GB because
vendors differ by a few percent and a genuine 32 GB card must pass. It is
declared in `appliance/media_sizing.py`, recorded in every image's
`minimum_media_bytes` build metadata, and measured in
`reports/appliance/<run-id>/media-sizing.json`.

The persistent partition is grown to fill the medium on first boot. That growth
is a transaction: a card whose filesystem did not actually grow is retried on
the next boot rather than marked as finished.

The power-cut cases require cutting power at the wall or with a switchable PDU.
Pulling the plug on a `poweroff` is not the same test.

## Case list

### Group 0 — build the image (prerequisite)

| # | Case | Expected |
|---|---|---|
| 0.1 | `scripts/appliance-check-rpi-image-gen.sh --rpi-image-gen <checkout>` | PASS against the pinned revision |
| 0.2 | `scripts/appliance-build-rpi-ab-image.sh --profile rpi5` on a builder with the upstream dependencies | PASS, image and `update.tar.zst` produced |
| 0.2b | The same with `--profile rpi4` | PASS; a separate artefact, not the Pi 5 image relabelled |
| 0.2c | `scripts/appliance-verify-slot-mounts.sh --rpi-image-gen <tree>` | PASS: every declared shared path is generated **and** activated |
| 0.3 | `scripts/appliance-inspect-rpi-ab-image.sh <image>` | PASS: six partitions, image-rota labels, distinct identities |
| 0.3b | The same inspection's content findings | PASS with **no** NOT RUN: the package and its exact version in both slot roots, the dpkg status, the layout descriptor, the build marker, the persistence configuration, the six shared activations, the four services, the slot generators, the machine-id policy, no shipped host key, the runtime helpers, both service drop-ins, the bootconfig `tryboot_a_b=1` selector, and `root=/dev/disk/by-slot/active/system` with a read-only root on **both** boot partitions. No mount and no root: the Pi 5 root filesystem uses 16 KiB ext4 blocks that no 4 KiB-page kernel will mount |
| 0.3c | The GPT structures | PASS: primary and backup headers, both entry-array CRCs, partition ranges inside the disk, no overlap, and `sgdisk --verify` agreeing where gdisk is installed |
| 0.3d | The slot pairing | PASS: `system_a` and `system_b` hash to the same payload with different PARTUUIDs, and so do `boot_a` and `boot_b` |
| 0.4 | Build a second image and `--compare` it | No partition identity is reused between builds |
| 0.5 | `scripts/appliance-build-rpi-ab-update.sh --profile rpi5 --sign-key <key>` then `appliance-inspect-rpi-ab-update.sh` | PASS, members `boot` and `system`, signature verifies |
| 0.6 | The manifest each build produced | Both members declare `encoding: android_sparse` with distinct `encoded_sha256` and `expanded_sha256` |
| 0.7 | `simg2img` the real `system` member and compare | Its SHA-256 equals the manifest's `expanded_sha256` and its size equals `expanded_size` |
| 0.8 | The `build-authority.json` the build wrote | Names the source form, the generator revision, the source tree hash and the SHA-256 of both artefacts, `completed: true` |
| 0.9 | `appliance-build-rpi-ab-update.sh --sign-key <key>` with no `--build-authority` | FAIL `provenance_unverified`; a development artefact is never signed |
| 0.10 | Append one byte to `update.tar.zst`, then sign with its build authority | FAIL `build_authority_mismatch` |
| 0.11 | Edit any file under the generator's `config/`, `layer/` or `image/` and rebuild | FAIL `rpi_image_gen_source_modified` before `./rpi-image-gen build` runs |
| 0.12 | `scripts/appliance-create-source-bundle.sh` for every source or review archive | PASS: the bundle self-verifies before it is handed over, and an archive that does not round-trip is deleted rather than delivered |
| 0.12b | `scripts/appliance-check-source-bundle.sh <bundle>` on the delivered source archive | PASS: 0 missing, 0 wrong modes, 0 wrong symlink targets, 0 undeclared, 0 unsafe, 0 duplicate, 6 symlinks preserved |
| 0.13 | `scripts/appliance-release-gates.sh --mode builder --rpi-image-gen <tree>` | Builder qualification. Strict by default: `RESULT: PASS (builder qualification)` and exit 0 only when every required gate PASSed; a required gate that did not run is `RESULT: NOT RUN` and exit 3. It says of itself that it is not a release |
| 0.13c | `scripts/appliance-finalize-rpi-release.sh --sign-key <key> --keyring <file> --trusted-fingerprint <fpr>` on the signing host | The trusted half: it verifies the build authority and its builder environment, signs, verifies the signature against the keyring and the trust policy, runs `--mode production` (which builds nothing and requires the signature, the full content inspection, the sparse cross-check and the source bundle), and assembles the kit. `RESULT: PASS (signed production release)` |
| 0.13d | `scripts/appliance-hardware-validation-kit.sh --gate-report <report>` | PASS only with exactly one completed build per profile, a signed manifest, both inspection reports with nothing NOT RUN, and a gate report that says PASS. `--development-kit` reports INCOMPLETE and `physical_ready=false` |
| 0.13e | `scripts/appliance_verify_hardware_kit.py --kit <dir> --keyring <file> --trusted-fingerprint <fpr>` | The kit re-verified from the directory rather than from the run that made it: every file re-hashed against `KIT-SHA256SUMS`, no file in the kit that the list does not name, the attestation's detached signature verified by a trusted key, every artefact the attestation binds re-hashed out of the kit's own profile directories, and no private key material. `physical_ready=true` only when all of that holds |
| 0.13f | `scripts/appliance_runtime_gates.py --from-log <gate>=<log>` | The runtime evidence a release is bound to. Each gate carries its result, the digest of the guest log it was read out of, the environment and — for anything that did not run — the exact prerequisite. A command-line verdict can never overrule a log, and a required gate that did not run is never a pass |
| 0.13g | `scripts/appliance-guest-sftp-session.sh` in the packaged-runtime guest | A real SFTP session with a key issued through the appliance's own authenticated key management: login, the exported directories visible, a known file fetched, `cd ..` bounded by the chroot, `/etc/passwd` unreachable. Read-only is asked of the protocol and the mounts together — put, overwrite, rename, mkdir, rmdir, rm, chmod and symlink all refused, with the export tree and the file behind it re-read afterwards. Shell, `-R`, `-D` and `-A` are refused; `-L` is asked by *using* the forward, because the local listener belongs to the ssh client and appears whatever the server allows. The upgrade path is included: the old root-owned `0700` key directory is put back, the login is confirmed to fail with it, and the package's own `ensure` is what makes it work again |
| 0.13h | `scripts/appliance-guest-network-persistence.sh <overlay>` in the same guest | Asked of systemd rather than read from two unit files: `Requires=`/`After=ems-appliance-persistence.service` are what systemd loaded, the healthy slot starts NetworkManager, and with the persistent source taken away the verification fails closed and NetworkManager is refused instead of consuming the slot-local fallback |
| 0.13i | `pytest tests/test_appliance_ab_docker_reconstruction.py` against a real Docker daemon | Three real contract images in a registry the test controls: the deployment authority recorded by digest, drift detected, a corrupt, a truncated and a zero-length seed each refused with the fallback naming the exact digest, `platform_mismatch` refusing an image for another architecture, a mutable tag refused at record time, and an EMS an operator stopped rebuilt and left stopped. A slot with an emptied image store and **no registry at all** now rebuilds every service from the seed and starts them; the containers are re-inspected and run the exact image the record names. `runtime_seed_unaddressable` is closed — see below |
| 0.14 | The medium the image is flashed to | At least 30,000,000,000 bytes (a 32 GB card). The image is ~16.5 GiB and the persistent partition needs ~11.2 GiB once both Docker stores, the seeds, a staged update and the operator's data are on it |
| 0.13a | `scripts/appliance-builder-vm.sh --release-gate --profile rpi5 --profile rpi4` | The gate builds the images itself, so it only reaches PASS on a host with the generator's prerequisites. This runs it in the disposable builder and brings the verdict and `dist/gates/` back; the developer host is not modified |
| 0.13b | The same with `--allow-not-run` on a host without the builder prerequisites | `RESULT: INCOMPLETE` and exit 0; the word PASS never appears |

### Group 1 — first boot and identity

| # | Case | Expected |
|---|---|---|
| 1.1 | Flash the A/B image, boot it | Slot A boots |
| 1.2 | `ems-appliance ab status` | `mode=ab`, `active_slot=A`, `known_good=A` |
| 1.3 | `ems-appliance ab verify-persistence` | passes, `/persistent` mounted, every shared path backed by it |
| 1.4 | Complete first-run setup, install Admin, configure EMS | Appliance and EMS reachable |
| 1.5 | Reboot normally | Slot A boots again, all data intact |
| 1.6 | `findmnt /` | Read-only, source `/dev/disk/by-slot/active/system` |
| 1.7 | `ls -l /dev/disk/by-slot/` | `active/`, `other/` and `persistent` resolve to this medium |
| 1.8 | `findmnt /var` | Bound from `/persistent/slots/system_a/var` |
| 1.9 | `cat /etc/machine-id` and `/persistent/common/etc/machine-id` | Identical |
| 1.10 | `ssh-keyscan` the appliance, record the host key | Recorded for case 3.x |
| 1.11 | Insert a second appliance card in a USB reader, `ab status` | Unchanged active slot; no drift from duplicate labels |

### Group 2 — a healthy update

| # | Case | Expected |
|---|---|---|
| 2.1 | Stage an update artifact | Written to inactive slot B, read-back verified |
| 2.2 | Check the selector before the trial | `[all]` still names slot A's boot partition, read back from `ems-appliance ab status --json` rather than assumed |
| 2.3 | Trial-boot B | B boots, reports `tryboot=1`, health passes |
| 2.3a | Inspection before the trial | `inspection.ok=true`; the selector was untouched while it ran |
| 2.4 | Commit | `[all]` names slot B's boot partition and `[tryboot]` names slot A's, both compared against `ab status --json`, never against a fixed number |
| 2.5 | `docker image ls` in slot B before commit | Admin image present, restored from the seed |
| 2.6 | Disconnect the WAN, repeat 2.1–2.4 | The trial still commits; reconstruction used the seed only |
| 2.7 | `cat /etc/machine-id` in slot B | Identical to the value recorded in 1.9 |
| 2.8 | `ssh-keyscan` in slot B | Same host key as 1.10 |
| 2.9 | `findmnt /var` in slot B | Bound from `/persistent/slots/system_b/var` |
| 2.10 | Reboot normally | B boots as the default |
| 2.11 | EMS configuration and data | unchanged |
| 2.12 | SSH host key fingerprint | unchanged from before the update |
| 2.13 | Network settings, hostname, mDNS name | unchanged |
| 2.14 | Admin console reachable | yes |
| 2.15 | Appliance authentication | the same password still works |

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
| 4.9 | Edit `/opt/ems-solarflow/docker-compose.yml` after the plan is confirmed, then trial | `deployment_authority_drift`; no `docker load`, `pull` or `compose up` runs; no commit; the browser offers a new plan and no bypass |
| 4.10 | Edit `/opt/ems-solarflow/.env` after the plan is confirmed, then trial | As 4.9 |
| 4.11 | Stop EMS deliberately, then update | EMS comes back stopped, its image authority is still proven, and the slot commits |
| 4.12 | Run Admin, EMS and InfluxDB, then update | All three are reconstructed and all three are health gates |
| 4.13 | Delete one seed archive and disconnect the WAN, then trial | The affected service is `unavailable`; the slot does not commit if it is required |
| 4.14 | Seed an amd64 image for one service, then trial | Refused on platform; no commit |
| 4.15 | Replace one `ssh_host_*_key.pub` on the persistent partition, then reboot | `host_identity_keypair_mismatch`; `ssh.service` does not start |
| 4.16 | Fill the persistent partition so an `fsync` fails during first-boot key creation | `host_identity_not_durable`; no success is reported and SSH stays blocked |

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

## Which steps destroy something

Four operations in this gate are not reversible, and none of them is wrapped in
a one-click script. Each is run by hand, so the operator can read it first.

| Marker | Operation | What it costs |
|---|---|---|
| **FLASH DESTROYS TARGET MEDIA** | Writing the image to the card, SSD or NVMe | Every partition on that device, including any appliance already on it. Confirm the device node immediately before writing; a wrong `of=` takes the workstation's own disk |
| **POWER CUT TEST** | Cutting power at the wall or PDU mid-write | Deliberate. May leave the medium needing a re-flash, which is the point of the case |
| **TRYBOOT REBOOT** | `reboot '0 tryboot'` | Reboots the appliance into the untested slot. Recoverable, but the appliance is offline until it comes back or falls back |
| **ROLLBACK REBOOT** | Committing or rolling back the selector | Changes which slot boots by default. Recoverable only from the other slot |

The read-only helpers below never do any of these. `capture-baseline`,
`verify-slot`, `verify-persistence` and `collect-evidence` write no block
device, change no selector, restart no service and touch no SSH key.

## Procedure for one storage class

```text
 0  Prepare a builder with rpi-image-gen's dependencies and verify the pin:
      scripts/appliance-check-rpi-image-gen.sh --rpi-image-gen <checkout>
 1  Build the appliance image:
      scripts/appliance-build-rpi-ab-image.sh --profile rpi5 --output out/
 2  Record the build ID and the image sha256 from the manifest.
 3  FLASH DESTROYS TARGET MEDIA — flash the image to the target medium from
      the second machine, after confirming the device node.
 4  Boot with the serial console attached and capture the log.
 5  Capture the baseline: scripts/appliance-hardware-capture-baseline.sh
 6  Run group 1.
 7  Build an update artifact from a second, slightly different build:
      scripts/appliance-build-rpi-ab-update.sh --output out/
 8  Run group 2, capturing `ems-appliance ab status --json` after every step.
      Contains TRYBOOT REBOOT and ROLLBACK REBOOT.
 9  Run group 3.
10  Run group 4, one case per boot, re-imaging between destructive cases.
      Contains POWER CUT TEST and FLASH DESTROYS TARGET MEDIA.
11  Record every result in the table above with the date and build IDs.
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

For every power-cut case, record what the selector partition contained
afterwards (`ems-appliance ab status --json` plus a raw copy of `autoboot.txt`),
because that file is the whole safety argument.

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
