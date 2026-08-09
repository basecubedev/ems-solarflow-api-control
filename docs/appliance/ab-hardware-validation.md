# A/B physical-hardware validation gate

A/B operating-system support is **not complete** until a real Raspberry Pi has
passed the cases below. Everything that can be proven without hardware — the
state machine, the selector parser, the layout authority, the write failure
matrix, the boot-flow simulator — is covered by the automated suites, and none
of it substitutes for a physical boot.

Record every run in this file's results table with the board, the storage class,
the image build ID and the date. A case that was not run is recorded as
`NOT RUN`, never as a pass.

## Verification stages

Each stage is a strictly stronger claim than the one above it. Anything not
listed as reached has not been reached.

| Stage | What it proves | State |
|---|---|---|
| Simulation verified | The state machine, selector parser, layout authority, write-failure matrix and boot-flow simulator | Reached |
| Real upstream config validated | Both hardware profiles resolve through rpi-image-gen's own `ConfigLoader` and `LayerManager` at the pinned revision, and the project layer's dependencies resolve beside upstream's | Reached |
| Real upstream artefact fixture validated | The update path drives genuine Android Sparse containers through zstd, tar, the member allowlist, sparse validation, expansion and filesystem identification | Reached |
| Real image built | `rpi-image-gen build` produces an `.img` and `update.tar.zst` from a pinned source tree | **NOT REACHED** — no build host with the upstream dependencies and an aarch64 binfmt handler |
| Real image inspected | The partition table, labels and per-build identities of an image that was actually built | **NOT REACHED** — depends on the stage above |
| Real Pi boot verified | Everything below | **NOT REACHED** |

The last three are what this gate exists for. Nothing in the automated suites
substitutes for them.

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

```text
Raspberry Pi 4 and Raspberry Pi 5
one microSD card
one USB SSD
one NVMe drive on a Pi 5 carrier
a switchable power supply for the power-cut cases
a serial console (UART) — a Pi that will not boot shows why only here
a second machine to re-image from
```

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
| 0.13 | `scripts/appliance-release-gates.sh --rpi-image-gen <tree>` | Strict by default: `RESULT: PASS` and exit 0 only when every required gate PASSed; a required gate that did not run is `RESULT: NOT RUN` and exit 3; a failure is `RESULT: FAIL` and exit 1 |
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
| 2.2 | Check the selector before the trial | `[all]` still boot partition 2 |
| 2.3 | Trial-boot B | B boots, reports `tryboot=1`, health passes |
| 2.3a | Inspection before the trial | `inspection.ok=true`; the selector was untouched while it ran |
| 2.4 | Commit | `[all]` boot partition 3, `[tryboot]` boot partition 2 |
| 2.5 | `docker image ls` in slot B before commit | Admin image present, restored from the seed |
| 2.6 | Disconnect the WAN, repeat 2.1–2.4 | The trial still commits; reconstruction used the seed only |
| 2.7 | `cat /etc/machine-id` in slot B | Identical to the value recorded in 1.9 |
| 2.8 | `ssh-keyscan` in slot B | Same host key as 1.10 |
| 2.9 | `findmnt /var` in slot B | Bound from `/persistent/slots/system_b/var` |
| 2.5 | Reboot normally | B boots as the default |
| 2.6 | EMS configuration and data | unchanged |
| 2.7 | SSH host key fingerprint | unchanged from before the update |
| 2.8 | Network settings, hostname, mDNS name | unchanged |
| 2.9 | Admin console reachable | yes |
| 2.10 | Appliance authentication | the same password still works |

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

## Procedure for one storage class

```text
 0  Prepare a builder with rpi-image-gen's dependencies and verify the pin:
      scripts/appliance-check-rpi-image-gen.sh --rpi-image-gen <checkout>
 1  Build the appliance image:
      scripts/appliance-build-rpi-ab-image.sh --profile rpi5 --output out/
 2  Record the build ID and the image sha256 from the manifest.
 3  Flash the image to the target medium from the second machine.
 4  Boot with the serial console attached and capture the log.
 5  Run group 1.
 6  Build an update artifact from a second, slightly different build:
      scripts/appliance-build-rpi-ab-update.sh --output out/
 7  Run group 2, capturing `ems-appliance ab status --json` after every step.
 8  Run group 3.
 9  Run group 4, one case per boot, re-imaging between destructive cases.
10  Record every result in the table above with the date and build IDs.
```

The build host needs upstream's dependency set — `mmdebstrap`, `podman`,
`uidmap`, `pv`, `btrfs-progs`, `dctrl-tools`, `python3-debian`,
`python3-jsonschema`, `flex` — and, when it is not itself arm64, a registered
`qemu-user-static` binfmt handler. `appliance-check-rpi-image-gen.sh` lists what
is missing and the build wrapper refuses to start without it, reporting
`rpi_image_gen_dependencies_missing` rather than producing a partial image.

For every power-cut case, record what the selector partition contained
afterwards (`ems-appliance ab status --json` plus a raw copy of `autoboot.txt`),
because that file is the whole safety argument.

## Pre-hardware validation record

What was actually run before the hardware gate, on 2026-08-08, after the
deployment-authority and build-provenance work. A result that is not listed here
was not produced.

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
