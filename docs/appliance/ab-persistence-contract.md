# A/B persistence contract

What survives an A/B slot switch on this appliance, what deliberately does not,
and how the appliance proves it at boot.

The authoritative source is `appliance/ab_persistence.py`. This document
describes it; it does not restate it as a second authority. The declaration the
image ships is generated from that module into
`packaging/appliance/image/layer/ems-appliance.rootfs-overlay/etc/rpi-image-gen/slot-shared.d/50-ems-appliance.conf`.

Schema version: **2**.

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

### Appliance

| Path | Class | Notes |
|---|---|---|
| `/var/lib/ems-appliance-manager` | shared | authentication, operations, package ownership, backup-account ownership, known-good Admin records, SSH host keys |
| `/var/log/ems-appliance-manager` | shared | appliance, agent and audit logs |
| `/etc/ems-appliance-manager` | shared | host configuration, image allowlist, the A/B layout descriptor |
| `/var/lib/ems-appliance-os-update` | shared | staged artifacts, the pending trial record, known-good and fallback state, the runtime seed |

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
| Real bind mounts on a booted image | **not yet verified on hardware** |
| Machine identity across a real slot switch | **not yet verified on hardware** |

See [`ab-hardware-validation.md`](ab-hardware-validation.md) for what physical
validation still has to establish.
