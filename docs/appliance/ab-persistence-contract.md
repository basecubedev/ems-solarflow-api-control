# A/B persistence contract

What survives an A/B slot switch on this appliance, what deliberately does not,
and how the appliance proves it at boot.

The authoritative source is `appliance/ab_persistence.py`. This document
describes it; it does not restate it as a second authority. The declaration the
image ships is generated from that module into
`packaging/appliance/image/layer/ems-appliance.rootfs-overlay/etc/rpi-image-gen/slot-shared.d/50-ems-appliance.conf`.

Schema version: **3**.

## The slot root is read-only, and that is now proven

The A/B model rests on an immutable slot root: everything that has to survive a
switch is a bind mount from the persistent partition, everything else is
discarded, and a rollback therefore returns a slot to exactly what was flashed.

Three things enforce it, and all three are checked by the image inspection:

```text
/etc/fstab            ro,relatime,commit=30 for /      (image-rota writes it;
                                                        systemd-remount-fs
                                                        applies it)
cmdline.txt           ro                               (this project's layer
                                                        adds it; upstream adds
                                                        it only for erofs)
mount points          every shared path and /persistent exist in the image
```

The kernel command line decides the *initial* mount and `/etc/fstab` decides
what it stays. Neither was explicit before: the command line said nothing, so
the first mount relied on initramfs-tools happening to default to read-only,
and that default is not this project's to depend on.

The mount points are the part that only a real read-only root exposes. systemd
creates a missing mount point only on a filesystem it can write to, so a shared
path with no directory in the image is a bind that never happens on hardware —
and `/persistent` with no directory is a boot with no state at all.
`/opt/ems-solarflow`, `/var/lib/ems-appliance-os-update` and `/persistent` were
all missing. They are created by the image layer now, while the root is still
writable, and `scripts/appliance-audit-root-writes.sh` runs the package's own
boot-time write paths in a guest whose root really is read-only.

### The mutable set

```text
persistent shared    the six declared paths, /home, /etc/machine-id, the journal
slot-local mutable   /var in its entirety (upstream's slot-perst generator)
tmpfs / ephemeral    /run, /tmp
forbidden            every other path on the slot root
```

`/srv/ems-appliance-export` is on the read-only root on purpose: it holds only
mount points, and its contents are the read-only binds `setup-export-root.sh`
establishes. That script used to `chown` and `chmod` those directories
unconditionally, which fails with `EROFS` even when the ownership and mode are
already what was wanted; it checks first and writes only a difference now.

## The mechanism is upstream's

`rpi-image-gen`'s `image-rota` layer provides the shared partition and the
bind-mount machinery. The appliance declares paths; it does not mount them.

```text
/persistent                 the shared partition            (upstream: persistent.mount)
/persistent/shared/<path>   the backing directory           (upstream: slot-shared-generator)
/persistent/slots/system_a  slot A's /var                   (upstream: slot-perst-generator)
/persistent/slots/system_b  slot B's /var                   (upstream: slot-perst-generator)
/persistent/common/etc      machine identity                (upstream: machine-id-sync.service)
/persistent/home            /home                           (upstream: fstab)
/persistent/log/journal     /var/log/journal                (upstream: fstab)
```

A layer declares a shared path by dropping a `.conf` file into
`/etc/rpi-image-gen/slot-shared.d/`. At boot, upstream's generator emits one
`.mount` unit per declared path, binding `/persistent/shared/<path>` over
`<path>`.

### Upstream generates every mount and activates one

Run against the pinned generator, six declared paths produce six `.mount` units
and exactly one `local-fs.target.wants` link: upstream's `ln -sf` sits outside
both of its loops, so `unit_name` still holds the last path of the last
configuration file. Splitting the declaration into one file per path does not
help, because the link is outside the per-file loop too.

The units themselves are correct, so the image supplies the missing activation
rather than reimplementing the mechanism:

```text
/etc/systemd/system/local-fs.target.wants/<escaped>.mount
    → /run/systemd/generator/<escaped>.mount        (one per declared path)

ems-appliance-persistence.service
    RequiresMountsFor=<every declared path>
```

systemd resolves a wants entry by its file name, so a link naming the
generator's output directory is enough. `RequiresMountsFor=` gives the
appliance's own chain the same ordering independently of `local-fs.target`.

`scripts/appliance-verify-slot-mounts.sh` runs the unmodified upstream generator
in a disposable mount namespace and compares what it wrote against what
`appliance/ab_persistence.py` declares. The image build calls it and fails on an
incomplete result, so a subtle upstream change becomes a build-time failure
instead of an appliance that loses its state one update later.

### The six links are the fragile part of a delivery

The activation links are the only symlinks in this repository, and they point
at `/run/systemd/generator/`, a directory that exists only inside a booted
appliance. On any other machine they are dangling by design, and that is what
makes them easy to lose in transit rather than in git:

```text
tar czf  archive.tar.gz tree/      6 links preserved
tar czhf archive.tar.gz tree/      0 links, "file removed before it could be
                                   read" per link — and exit status 0
```

`--dereference` resolves each link, finds nothing at the far end, warns, skips
it and still succeeds. A tree unpacked from such an archive builds, generates
six mount units, activates none of them, and loses every write to the shared
paths at the next slot switch. Both archives produced for earlier independent
reviews arrived in exactly that shape.

Nothing about it is a repository defect: `git ls-tree HEAD` carries all seven as
mode `120000` with the expected targets, and so do the index and the working
tree. So the defence is on the delivery path rather than in the tree:

```text
scripts/appliance-create-source-bundle.sh   writes from the git object tree
                                            (git archive), never the working
                                            directory, then verifies the result
                                            object by object and deletes a
                                            bundle that does not round-trip
scripts/appliance-check-source-bundle.sh    the same check for an archive that
                                            arrived from somewhere else
```

Both report `symlinks: 6 preserved`, and the tracked link names are asserted to
be exactly the declared mount units, so six links activating the wrong six
paths is a failure rather than a matching count. In a built image the same
property is read back per slot as `shared_activations: 6 shared paths
activated`.

### Why the appliance still verifies

Upstream guards each bind with `ConditionPathIsDirectory` on its source. If the
directory is missing the mount is skipped and the service falls back to the
path inside the read-only root. For a general-purpose image that is safe
degradation. For this appliance it is the worst possible failure: the directory
exists, the services start, everything looks healthy, and every write since the
last flash is discarded at the next slot switch — silently.

So `ems-appliance-persistence.service` proves each required path is really
backed by the device `/persistent` is mounted from, and **fails closed**.
`ems-appliance-agent.service`, `ems-appliance-web.service`,
`ems-appliance-slot-bootstrap.service` and `ems-appliance-ab-health.service`
all `Requires=` it, so a path that fell back stops them rather than letting them
write somewhere the next slot switch throws away.

Verify it by hand with:

```bash
sudo ems-appliance ab verify-persistence
```

## Classification

Every important path is exactly one of:

- **shared** — one copy, visible from both slots;
- **slot-local** — each slot has its own, by decision;
- **reconstructed** — rebuilt in a new slot from shared authority;
- **ephemeral** — not preserved, and nothing depends on it being preserved.

### EMS

| Path | Class | Notes |
|---|---|---|
| `/opt/ems-solarflow/config` | shared | `config.json`, the installation's control configuration |
| `/opt/ems-solarflow/data` | shared | `runtime-state.json`, the state database |
| `/opt/ems-solarflow/backups` | shared | operator backups |
| `/opt/ems-solarflow/docker-compose.yml` | shared | the deployment layout |
| Admin durable metadata | shared | under `/var/lib/ems-appliance-manager` |

All of `/opt/ems-solarflow` is one shared path.

## The schema version is a one-way door

`ab_persistence.PERSISTENT_SCHEMA_VERSION` is compared strictly against the
`persistent_schema_version` the running image declares. An appliance whose
manager implements a newer schema than its image reports
`persistence_identity_mismatch`, which withholds `persistence_ready` and blocks
the very update that would reconcile the two.

Adding or removing a shared path therefore changes this number, and doing so
after a release needs a migration path. Version 3 added `/var/lib/ems-backup`
before any image shipped, which is the only moment such a change is free.

### Appliance

| Path | Class | Notes |
|---|---|---|
| `/var/lib/ems-appliance-manager` | shared | authentication, operations, package ownership, backup-account ownership, known-good Admin records, SSH host keys |
| `/var/log/ems-appliance-manager` | shared | appliance, agent and audit logs |
| `/etc/ems-appliance-manager` | shared | host configuration, image allowlist, the A/B layout descriptor |
| `/var/lib/ems-appliance-os-update` | shared | staged artifacts, the pending trial record, known-good and fallback state, the runtime seed |
| `/var/lib/ems-backup` | shared | the confined backup account's home: the operator's `authorized_keys` and the marker proving this package created it |

`/var/lib/ems-appliance-os-update` being shared is what makes A/B work at all:
the trial slot reads the pending record the source slot wrote, and the
known-good and fallback history survives whichever slot is running.

### Host

| Path | Class | Owner | Notes |
|---|---|---|---|
| `/etc/machine-id` | shared | rpi-image-gen | synchronised from `/persistent/common/etc/machine-id` |
| SSH host keys | shared | this project | `/var/lib/ems-appliance-manager/ssh`, named by a drop-in |
| `/etc/ssh` | **slot-local** | — | the distro's `sshd_config`, `moduli` and package defaults |
| `/etc/NetworkManager/system-connections` | shared | this project | connection profiles |
| `/home` | shared | rpi-image-gen | |
| `/var/log/journal` | shared | rpi-image-gen | one journal for both slots |
| hostname | slot-local | — | set from configuration on every boot |

### Docker

| Path | Class | Notes |
|---|---|---|
| `/var/lib/docker` | **slot-local** | inside the per-slot `/var` |
| Admin/EMS/InfluxDB images | reconstructed | from digests recorded on the shared partition |
| container filesystems | ephemeral | recreated from the compose deployment |

## Machine identity

**One physical appliance is one Linux machine, whichever slot booted.**

`image-rota` owns this: `machine-id-sync.service` copies
`/persistent/common/etc/machine-id` into `/run/machine-id` early in boot, or
seeds it from the running identity when the shared file is empty. The appliance
does not reimplement it; the trial-health `machine_identity` gate proves it
happened, and a slot that came up with an identity of its own fails the gate.

The consequence is that DHCP leases, systemd journal identity and anything
keyed on the machine ID stay stable across an OS update.

## SSH host identity

`/etc/ssh` as a whole is **not** shared. Sharing it would couple one slot's
distro `sshd_config`, `moduli` and package-generated defaults to the other's,
which is exactly the coupling A/B exists to prevent — a slot could then be
broken by the other slot's package state.

Only the host keys are shared, in an appliance-owned directory under an
already-shared path:

```text
/var/lib/ems-appliance-manager/ssh/ssh_host_*_key
```

They are named by a project drop-in that ships in the image and is therefore
slot-local and identical in both slots:

```text
/etc/ssh/sshd_config.d/50-ems-appliance-hostkeys.conf
```

An operator's `known_hosts` entry therefore survives an OS update, while each
slot keeps its own OpenSSH configuration.

`ems-appliance-host-identity.service` is what creates them, exactly once, on the
first boot of an A/B appliance. It runs after the persistent mounts and before
persistence verification, sshd and NetworkManager, and the image makes
`ssh.service` `Requires=` it.

It is idempotent by construction: an existing key is validated and left
byte-for-byte as it is, and only an absent type is created. A new key is written
under a staging name, fsynced and renamed into place, so a crash leaves nothing
partial. Before sshd may read them, the directory and each key must be a real
file rather than a symlink, root-owned and not group- or world-readable — a
symlinked key path would let whoever could create one decide where sshd reads a
private key from, so it is refused rather than replaced.

Failure is terminal. An appliance whose key directory cannot be proven offers no
SSH at all rather than offering it under an identity nobody can vouch for. Only
public fingerprints are ever reported; no private key reaches a report, a log
line or a support bundle.

```bash
sudo ems-appliance host-identity --ensure
```

Note that upstream's `openssh-server` layer ships its own
`slot-shared.d/openssh-server.conf` declaring `Path=/etc/ssh`. This project does
not enable that layer's declaration.

## Network

Only `/etc/NetworkManager/system-connections` is shared: the connection profiles
an operator configured. NetworkManager's package state, its `conf.d`
drop-ins and its runtime state stay slot-local, so a new slot's NetworkManager
is the one its own package set installed.

The directory is root-owned and `0700`; NetworkManager refuses profiles that are
group- or world-readable, and the bind mount preserves the mode of the backing
directory on the persistent partition.

## Docker state

`/var/lib/docker` is slot-local by decision, because it is version-coupled: a
rollback to an older slot would otherwise hand an older engine a content store
written by a newer one.

The cost is that a freshly written slot has no images. That is paid by
`ems-appliance-slot-bootstrap.service`, which runs before trial health:

1. before the trial reboot, the source slot resolves its running containers to
   digests, records them under `/var/lib/ems-appliance-os-update`, and saves the
   images beside the record;
2. in the trial slot, the bootstrap loads the seed, falls back to pulling the
   same digests, and starts Admin;
3. trial health requires the Admin runtime to be available.

Because the images are seeded onto the shared partition, an appliance with no
registry access can still complete a trial boot. Registry access is a fallback,
not a dependency.

## Slot-local by decision

Each slot keeps its own copy, and sharing any of them would make one slot depend
on the other's package set — the failure A/B exists to prevent.

```text
/                 /usr              /lib              /lib/modules
/boot/firmware    /var              /var/lib/docker   /var/lib/dpkg
/var/lib/apt      /var/cache/apt    /etc/systemd/system   /etc/ssh
```

`/var/lib/dpkg` and `/var/lib/apt` are what make each slot independently
installable; `/lib/modules` belongs to the kernel that slot booted.

## What a slot switch is allowed to lose

- container filesystems and anything written inside a container that is not on
  a bind-mounted shared path;
- `/var` outside the declared shared paths, including caches and `/var/tmp`;
- the package database, which is what makes each slot independently installable;
- anything written to the read-only root, which cannot happen at runtime.

## Testing

```bash
pytest tests/test_appliance_ab_persistence.py
pytest tests/test_appliance_ab_slot_persistence.py
pytest tests/test_appliance_ab_bootstrap.py
```

`tests/test_appliance_ab_slot_persistence.py` drives a full update across a
simulated slot switch and asserts that every operator-visible file is still
reachable at its normal path afterwards.

## Verification status

| Property | Status |
|---|---|
| Contract and verifier | unit/simulation verified |
| slot-shared declaration matches the contract | contract test |
| Fail-closed unit ordering | contract test on the unit files |
| Runtime reconstruction from a seed | unit/simulation verified |
| A healthy slot verifies in a booted systemd guest | verified in the amd64 guest gate |
| A slot whose persistent source is gone fails closed | verified in the amd64 guest gate |
| Real bind mounts on a booted image | **not yet verified on hardware** |
| Machine identity across a real slot switch | **not yet verified on hardware** |

The healthy-slot half used to report NOT RUN. The verifier asked two different
authorities about one partition: the mountpoint check compares against the
device the layout descriptor resolves to and skips when there is none, while
the bind check went on comparing against an alias set the running system had
never used, so a guest with `/persistent` and all seven binds on the same
partition was told every bind was foreign. The mountpoint's own source now
joins that set — after it has survived its own check, so a partition of the
wrong identity cannot become the authority for its own binds.

See [`ab-hardware-validation.md`](ab-hardware-validation.md) for what physical
validation still has to establish.
