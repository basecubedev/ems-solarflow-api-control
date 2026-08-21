# Fail-safe A/B operating-system updates

This document defines how the EMS SolarFlow Raspberry Pi Appliance updates its
**host operating system**. It is the architecture reference; the operator-facing
description lives in [os-updates.md](os-updates.md).

The decision to build on the native Raspberry Pi `tryboot` mechanism rather than
on a third-party update framework is recorded in
[adr/ab-native-tryboot.md](adr/ab-native-tryboot.md).

## Three rollbacks that are not the same thing

The appliance already had two rollback mechanisms before this one existed. They
operate on different layers, recover from different failures, and none of them
substitutes for another.

| Mechanism | Layer | What it restores | Owner |
|---|---|---|---|
| **Admin image rollback** | Docker container | The previously running Admin container from its recorded image digest | `admin_lifecycle.py`, see [admin-recovery.md](admin-recovery.md) |
| **EMS backup / restore** | Application data | EMS configuration, runtime state and history from an EMS backup archive | EMS/Core and the Admin Console |
| **OS A/B rollback** | Raspberry Pi host OS | The previous known-good boot and root filesystem slot | `appliance/os_update.py`, this document |

Consequences that must stay true in code, UI and documentation:

- A/B **never** applies to Docker containers. An Admin update is still an image
  replacement with a digest-pinned rollback, and the word "slot" is not used for
  it.
- An OS A/B rollback restores the operating system, **not** EMS data. EMS
  configuration and data live on the shared persistent partition and are
  deliberately unaffected by a slot switch in either direction.
- An EMS restore does not repair a broken host OS, and an OS rollback does not
  undo an EMS configuration mistake.

A fourth mechanism exists on the hardware itself and is also not this one:

- **EEPROM A/B** is the Raspberry Pi 5 bootloader firmware's own redundant
  update path in SPI flash. It concerns the firmware that runs *before* any
  partition is read. This project reports EEPROM state read-only where it is
  useful for diagnosis and **never writes an EEPROM firmware slot**.

## Supported hardware

The A/B mechanism targets exactly this scope:

```text
boards        Raspberry Pi 4 Model B and Raspberry Pi 5, as separate artefacts
os            Raspberry Pi OS Trixie, 64-bit
architecture  arm64
firmware      a bootloader that implements autoboot.txt and tryboot
storage       microSD, USB mass storage or NVMe — each one only where layout
              detection proves an A/B layout on that device
```

**One image per board, and no image claims another's hardware.** The device
layer selects the kernel and the firmware, so a Pi 5 image is not a Pi 4 image:

```text
profiles/rpi5-ab.yaml   device layer rpi5   board class pi5
profiles/rpi4-ab.yaml   device layer rpi4   board class pi4

ems-solarflow-appliance-<version>-rpi5-arm64-ab.img
ems-solarflow-appliance-<version>-rpi4-arm64-ab.img
```

Each signed manifest carries the `device_layer` it was built from and only the
board classes that layer is for. At runtime the appliance normalises
`/proc/device-tree/compatible` to a bounded board class and refuses an artefact
that does not list it. A board it cannot identify blocks planning with
`hardware_not_supported`; it is never guessed at, because an image built for
another SoC does not boot and the appliance would be recoverable only by
reflashing.

Recognising a board and being able to update it are separate answers. CM4 and
CM5 resolve to a board class — an operator should be told what their appliance
is — but this project ships no `cm4-ab.yaml` or `cm5-ab.yaml` build profile, so
they report `hardware_has_no_build_profile` rather than `supported: true`. A
board is only supported once an installable artefact exists for it.

Nothing outside that scope is claimed. In particular:

- A storage class is never inferred from another. Passing on microSD says
  nothing about NVMe; see [ab-hardware-validation.md](ab-hardware-validation.md).
- Raspberry Pi 3 and earlier are not targeted: the appliance is arm64-only and
  the tryboot mechanism needs a current bootloader.
- Raspberry Pi Connect is not used. The appliance validates, stages, deploys and
  commits from its own host services; there is no cloud dependency in the update
  path.

## The layout

The layout is **not this project's**. It is produced by `rpi-image-gen`'s
`image-rota` layer, pinned in `packaging/appliance/image/rpi-image-gen.lock` and
explained in [adr/rpi-image-gen-image-rota.md](adr/rpi-image-gen-image-rota.md).

```text
GPT on a single device, produced by image-rota

  1  bootconfig   vfat   autoboot.txt only            /bootfs
  2  boot_a       vfat   firmware, kernel, config     /boot/firmware when slot A booted
  3  boot_b       vfat   firmware, kernel, config     /boot/firmware when slot B booted
  4  system_a     ext4   slot A root filesystem       / when slot A booted, read-only
  5  system_b     ext4   slot B root filesystem       / when slot B booted, read-only
  6  persistent   ext4   shared persistent state      /persistent
```

```text
slot A = boot_a + system_a
slot B = boot_b + system_b
```

The bootloader reads `autoboot.txt` from the **first** FAT partition and selects
a boot partition by number from it, which is why the selector is a partition of
its own: neither slot owns the file that decides which slot boots.

**Slot identity is the GPT label, never a PARTUUID this project pinned.**
`image-rota` generates partition identities per build and mandates stable
`PARTLABEL`s; upstream's `rpi-ab-slot-mapper` reads the booted partition's label,
derives the active slot from its `_a`/`_b` suffix, and publishes
`/dev/disk/by-slot/{active,other}/{boot,system}`. Two appliance media on one bus
are therefore distinguishable, which a fixed identity set could not manage.

There is no per-slot `cmdline.txt`. `image-rota` builds one bit-for-bit
identical slot pair, and every slot boots
`root=/dev/disk/by-slot/active/system`, resolved in the initramfs. An update
therefore carries one boot payload and one system payload, and a root filesystem
that named a slot would be a defect — the inactive-slot inspection refuses one.

`autoboot.txt` in its committed form, with slot A as the default:

```text
[all]
tryboot_a_b=1
boot_partition=2

[tryboot]
boot_partition=3
```

**Partition numbers are never guessed at runtime.** The numbers above are what
`image-rota` produces; the running system proves which partition is which by
matching the labels in the layout descriptor against block-device discovery on
the medium the firmware actually booted. See "Proving the active slot" below.

## How a release reaches the appliance

The appliance installs from `os_release_dir`, and until an update is in that
directory there is nothing to install. Two ways fill it.

**Downloading it.** Set `os_release_index_url` in `appliance.conf` to an
`https` URL serving a release index, and **Updates → Download an OS release**
offers what it lists. The index is a plain JSON document:

```json
{
  "format_version": 1,
  "releases": [
    {
      "release_id": "ems-solarflow-appliance-0.2.0-rpi5-arm64-ab",
      "manifest_url": "https://.../ems-solarflow-appliance-0.2.0-rpi5-arm64-ab.manifest.json",
      "signature_url": "https://.../ems-solarflow-appliance-0.2.0-rpi5-arm64-ab.manifest.json.asc",
      "archive_url": "https://.../ems-solarflow-appliance-0.2.0-rpi5-arm64-ab.tar.zst"
    }
  ]
}
```

GitHub Releases can host both the index and the files it names.

**Copying it in.** Place the manifest, its `.asc` signature and the archive in
`os_release_dir` by hand. Nothing about the install path differs afterwards —
the same signature check decides, whichever way the files arrived. An appliance
with no `os_release_index_url` says so in the manager rather than implying an
update could arrive on its own.

### What the index is allowed to decide: nothing

An index entry names a candidate. It is fetched over `https` and never trusted,
because everything that decides what is written comes from the signed manifest:

1. the manifest and its detached signature are fetched into a staging directory
   inside `os_release_dir`,
2. the signature is verified against `os_release_keyring`,
3. only then are the archive's name, size and digest read from it,
4. the archive is fetched under exactly that declared size and hashed as it
   streams,
5. the digest is compared against the verified manifest,
6. only then is anything moved into `os_release_dir`, manifest last.

An index that lies therefore costs bandwidth and nothing else. A download that
fails any step leaves the release directory as it was: the staging directory is
removed, and because the manifest is moved in last, an interrupted fetch leaves
files the catalogue does not offer rather than a release whose archive is
missing. A release already present is never silently refetched or overwritten.

Plain `http` is refused rather than upgraded, including after a redirect —
being told that the configured URL is insecure is more useful than being
quietly given a different one.

### The keyring is not shipped with the appliance

`os_release_keyring` points at `/etc/ems-appliance-manager/os-release-keyring.gpg`
by default, and **no package puts a key there**. That is deliberate: a trust
anchor an appliance ships with itself is one an attacker who ships appliances
also controls. Until an operator installs the public key of whoever signs their
releases, every artifact is refused.

The manager says so rather than letting an update fail at the last moment:
**Release keyring** appears in the update-readiness list and is red until the
file exists. Install it with the key you obtained out of band:

```bash
sudo install -m 0644 release-key.gpg \
     /etc/ems-appliance-manager/os-release-keyring.gpg
```

On an A/B appliance `/etc` belongs to the running slot, so this has to be
repeated after a slot switch unless the key is baked into the image you build.

### The clock comes first

This board has no real-time clock. After a power cut it starts somewhere in the
past until `systemd-timesyncd` catches up, and both the TLS certificate check
and the release signature are judged against that time. A download started
before the clock is synchronised fails with certificate and signature errors
that say nothing about a clock, so the appliance checks first and refuses with
`clock_not_synchronised`. A clock it cannot confirm counts as unsynchronised.

Wait for time synchronisation after a cold boot and try again.

## Update states

An A/B operation is a durable record in the existing appliance operation store
(`appliance/operations.py`). There is no second operation database. The A/B
progress states live in the operation's stage field and in one shared
`ab-state.json` on the persistent partition, which is the only thing that
survives the reboot in the middle of the transaction.

```text
unsupported            this host is not an A/B appliance and never will be
single_slot            a normal single-root installation; package updates only
ready                  A/B layout proven, no operation in flight
staging                artifact downloaded, members extracted and their encoded
                       digests verified
sparse_validated       each member's Android Sparse container structurally
                       checked against the size the manifest signed
image_expanding        containers being expanded into staging
expanded_verified      each expanded image hashes to the digest the manifest
                       declares
writing_inactive       inactive boot and root are being written
verifying_inactive     written images are being read back and checked
ready_for_tryboot      inactive slot verified, selector not yet armed
tryboot_requested      selector armed, reboot requested
booted_trial           the target slot is running under tryboot
health_verifying       the trial slot is proving itself
committing             the selector is being switched to the target slot
committed              the target slot is the default; the old slot is the
                       rollback candidate
fallback_observed      the source slot booted again with an uncommitted trial
failed_recoverable     the operation can be re-planned after a re-stage
manual_action_required the selector or slot state cannot be proven
failed_terminal        the operation ended and nothing else will happen
```

Mapping onto the generic operation model:

| A/B state | Operation state |
|---|---|
| `staging`, `sparse_validated`, `image_expanding`, `expanded_verified`, `writing_inactive`, `verifying_inactive`, `ready_for_tryboot`, `tryboot_requested`, `committing` | `running` |
| `booted_trial`, `health_verifying` | `verifying` |
| `committed` | `succeeded` |
| `fallback_observed`, `failed_recoverable` | `failed_recoverable` |
| `manual_action_required` | `manual_action_required` |
| `failed_terminal` | `failed_terminal` |

## Update members are containers, not filesystems

`image-rota`'s genimage configuration wraps both update payloads in an
`android-sparse` container, and its `post-image.sh` packs those containers as
the `boot` and `system` members of `update.tar.zst`. A member's bytes are
therefore a chunk table, and writing them to a partition produces a slot that
matches its manifest and does not boot.

So a member carries two identities and the manifest signs both:

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

`encoded_sha256` is what the archive carries and what extraction verifies.
`expanded_sha256` is what the partition receives and what the read-back proves.
They are never the same value, and a manifest that omits the expanded pair is
refused rather than having one inferred for it — which is why the release
manifest format is now 2 and format 1 is readable only for diagnostics.

The expander is in-process (`appliance/sparse.py`). No `simg2img` is installed:
a decoder invoked as a subprocess would be another executable to allowlist and
verify, and its output size would still have to be trusted afterwards. Every
bound — magic, versions, header sizes, block size, chunk count, per-chunk
extent, the running total, integer overflow — is checked before a byte is
produced, and the expanded image must fit the target partition before anything
is written.

## The one invariant

```text
before a verified commit:   the default boot slot is unchanged
after a verified commit:    the new slot is default and the old slot is the
                            recorded rollback candidate
```

Every design decision below follows from it. The default selector is only ever
written after the target slot has booted, proven itself and asked to commit
itself. A trial boot is one-shot: if the target slot never reaches its commit,
the next ordinary boot returns to the previous default with nothing changed.

Where the selector state cannot be proven, the operation becomes
`manual_action_required`. Nothing guesses which slot is safe.


## Shared state

The persistent partition is mounted at `/persistent` and every shared path is
bound over its normal location by `rpi-image-gen`'s `slot-shared` generator, so
an operator never sees a different path depending on which slot booted.

The full classification — what is shared, what is slot-local, what is
reconstructed, and the machine-identity, SSH and Docker decisions — is in
[ab-persistence-contract.md](ab-persistence-contract.md). It is not restated
here.

The one property worth repeating: upstream's generator fails **open**, skipping
a bind whose source directory is missing. `ems-appliance-persistence.service`
therefore verifies the binds and fails closed, and the agent, web, bootstrap and
health units `Requires=` it.

## The deployment authority

An OS update is only safe if the appliance an operator had before the reboot is
the one they have after it. The OS is only half of that: the other half is the
EMS deployment, which lives on the shared partition and is not part of any slot.

Before a trial is armed, the running slot records one versioned, fingerprinted
object onto the persistent partition:

```json
{
  "schema_version": 4,
  "captured_at": 1754630000.0,
  "compose":     {"path": "/opt/ems-solarflow/docker-compose.yml", "sha256": "sha256:..."},
  "environment": {"path": "/opt/ems-solarflow/.env",               "sha256": "sha256:..."},
  "services": {
    "admin":    {"state": "running", "image_digest": "sha256:...",
                 "platform": {"os": "linux", "architecture": "arm64"}},
    "ems":      {"state": "stopped_clean", "image_digest": "sha256:...", "...": "..."},
    "influxdb": {"state": "absent",        "image_digest": "",           "...": "..."}
  }
}
```

Per service the state is an intent, never a boolean and never "not running":

```text
absent          not deployed on this appliance        — allowed, not a failure
running         deployed and running                  — must come back running
stopped_clean   exited 0, so deliberately stopped     — must come back stopped
failed          exited non-zero                       — blocks planning
restarting      in a restart loop                     — blocks planning
created         created and never started             — blocks planning
unknown         the daemon did not answer             — blocks planning
```

Only the first three are states a slot can be rebuilt into. A container that
crashed, that is restarting, or that was created and never started says nothing
about what the operator wanted, so it is never normalised into "stopped": an OS
update planned against it would reconstruct an intent nobody expressed.

`absent` and "expected but failed to reconstruct" are different states and are
never conflated. A fresh Appliance with Admin installed and no EMS deployment
yet is a supported thing to update; an EMS that was running and did not come
back is not.

The canonical hash of that object is the **deployment fingerprint**, and it is
part of the object an operator confirms. A confirmed plan carries both halves of
the authority, and the confirmation hash covers the whole thing:

```json
{
  "schema_version": 1,
  "os_write": {"device": "...", "target_slot": "B", "boot_partuuid": "...",
               "rootfs_expanded_digest": "sha256:...", "...": "..."},
  "deployment_fingerprint": "sha256:...",
  "deployment_schema": 4
}
```

They stay two values rather than one digest because they are proven against
different things and an operator fixes them differently: `os_write` answers
"which bytes, onto which partitions", the fingerprint answers "which appliance
those partitions belong to".

It is verified at every phase of the update:

```text
plan            captured, fingerprinted, written to the shared partition
confirmation    the fingerprint is inside the hash the operator's plan is sealed with
execute         recomputed and compared *before* the target slot is invalidated
arm             proven once more, with a resolvable Admin image, before tryboot
pending trial   the confirmed fingerprint is stored in the pending record
bootstrap       the compose and .env digests are recomputed before Docker runs
trial health    the fingerprint is compared again before the slot may commit
```

Two questions are asked separately at execute time, because they drift for
different reasons: the recorded deployment *files* must still be what they were,
and the running *services* must still resolve to the identities that were
recorded. A compose file edited after the confirmation and an Admin container
restarted onto another image are both "not this deployment".

Drift is refused, never re-recorded:

```text
before the write    failed_recoverable, replan_required; the inactive slot is
                    untouched, the boot default is unchanged, and the known-good
                    history still holds its rollback candidate
after the reboot    the trial does not commit and the appliance falls back
```

The seed is deliberately outside the authority. It is exported from the
confirmed record afterwards and is availability, not identity: a `docker save`
that runs out of space reports `runtime_seed_incomplete` and keeps the confirmed
fingerprint, so the trial slot can still pull the same exact digests. What never
happens is a tryboot into a slot that is already known to be unable to rebuild —
the deployment and a digest-nameable Admin image are proven again immediately
before the selector is armed.

The browser is told what to do about it and offered no way around it:

> The EMS deployment changed after this OS update was planned.
> Create a new update plan before continuing.

Refusing to auto-refresh is the point. A fingerprint that updated itself on
drift would mean the operator confirmed one deployment and the appliance
committed another.

## Reconstructing the application

`/var/lib/docker` is per-slot, so a freshly written slot has an empty image
store. Before the trial reboot the recorded images are saved beside the record
on the shared partition — one generation, hashed and sized — which is what lets
an appliance with no WAN finish a trial.

Inside the trial slot, reconstruction:

```text
1  recomputes the compose and .env digests            → drift refuses everything
2  restores every recorded image                       seed first, registry second
3  proves each restored image by inspecting the store  digest and OCI platform
4  starts every service recorded as running            influxdb, admin, ems
```

`docker load` printing a name is not evidence: only an inspection of the image
store answers which digest was imported. The platform is checked against the
recorded one *and* against this machine's architecture, so a Pi slot cannot
commit holding amd64 images. The registry fallback names `repository@sha256:…`
and never a tag.

A service recorded as stopped is not started and does not block the commit — but
its image authority and its persistent data are still proven.

### Rolling the OS back is not rolling the deployment back

```text
OS slot rollback   ≠   EMS configuration rollback   ≠   database rollback
```

The EMS configuration, its data and the compose file are shared, so slot A
returning after slot B was active reconstructs against whatever the appliance is
deployed as *now*. It does not restore an older compose file, and it does not
undo an EMS upgrade that happened while slot B was active. The Admin/EMS
compatibility model remains the authority for that; this mechanism only replaces
the operating system underneath it.

## Proving the active slot

Authority for "which slot am I" is never a single signal. `appliance/ab_layout.py`
collects independent ones and requires them to agree:

```text
/proc/device-tree/chosen/bootloader/partition   the boot partition the firmware used
/proc/device-tree/chosen/bootloader/tryboot     whether this is a one-shot trial boot
/proc/cmdline                                   root=/dev/disk/by-slot/active/system
findmnt                                         what is mounted at / and /boot/firmware
lsblk --json, blkid                             the partitions of the boot device
/etc/ems-appliance-manager/ab-layout.json       the image-build layout descriptor
/dev/disk/by-slot/{active,other}/*              upstream's slot mapper symlinks
selector partition autoboot.txt                 the default and trial boot partitions
/etc/ems-appliance-slot                         the slot marker inside this rootfs
```

Any disagreement produces `layout_drift`, which disables every A/B mutation and
leaves the appliance in a read-only reporting mode. Drift is a refusal, never a
best-effort guess.

The inactive slot is proven separately, before anything is written to it:

```text
its boot partition is not the active boot partition
its root filesystem is not mounted read-write anywhere
its root filesystem is not the active root device
both of its partitions are on the same approved physical device
both partitions are at least as large as the artifact's images
```

"The other partition number" is never sufficient on its own.

### The selector is what the next boot reconciles from

A commit writes the boot selector and then the slot history. A power loss
between them leaves an appliance running the new slot on an ordinary boot while
its own state still says the trial never committed, and deciding that from the
JSON alone would abandon a slot that is already the default.

So the next ordinary boot asks the selector first:

```text
running the trial's target slot, no tryboot, default partition == target
    → the commit ran; record known-good and mark the trial committed
running the source slot, no tryboot
    → the trial did not commit; record a fallback and consume the pending trial
```

Only `commit` ever moves the default — arming a trial writes the target into
`[tryboot]` and leaves `[all]` on the source slot — so a default that names the
target proves the commit ran, which in turn proves health had already passed.

## Single-slot installations

A normal Raspberry Pi OS installation reports:

```text
mode           single_slot
ab_supported   false
reason         ab_layout_not_present
```

The System Updates page then keeps its existing package-update behaviour and
says that image-based host updates require re-imaging onto an A/B appliance
image. There is no partial or disabled A/B control, and there is **no in-place
conversion**: the appliance never resizes, moves or repartitions a running
installation's storage, and no such action is reachable from the browser or the
agent. See [installation.md](installation.md).

## Where the code lives

| Module | Responsibility |
|---|---|
| `appliance/ab_layout.py` | Read-only slot and partition discovery, drift detection |
| `appliance/ab_persistence.py` | The shared-path contract and its read-only verifier |
| `appliance/os_releases.py` | The release index, signature and compatibility authority |
| `appliance/os_artifacts.py` | Bounded, traversal-safe `.tar.zst` extraction |
| `appliance/os_update.py` | Planning, staging and the inactive-slot write |
| `appliance/ab_boot.py` | The `autoboot.txt` parser/serializer and the tryboot transaction |
| `appliance/ab_health.py` | Trial-boot detection, health gates, commit and fallback |
| `appliance/ab_blocks.py` | The block-device backend and its fake for tests |
| `appliance/ab_bootstrap.py` | The deployment authority, seeding and reconstruction |
| `appliance/ab_docker_health.py` | The one Docker question set a trial slot is judged by |
| `appliance/build_authority.py` | What a completed builder run produced, and its hash |
| `appliance/project_source.py` | This repository's own revision and tree, proven before a build reads it |
| `appliance/source_bundle.py` | Bundle-to-tracked-tree parity, object by object, both directions |
| `appliance/ab_state.py` | The durable state both slots read, flushed before the step it authorises |
| `appliance/host_identity.py` | The SSH identity established once and never regenerated |

## Related documents

- [adr/ab-native-tryboot.md](adr/ab-native-tryboot.md) — why native tryboot
- [ab-hardware-validation.md](ab-hardware-validation.md) — the physical-hardware gate
- [os-updates.md](os-updates.md) — the operator-facing update page
- [installation.md](installation.md) — imaging and first boot
- [security-model.md](security-model.md) — the privilege boundary
- [troubleshooting.md](troubleshooting.md) — recovery from a wedged update
