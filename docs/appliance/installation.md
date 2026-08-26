# Raspberry Pi Appliance Manager — Installation

## Timezone

The appliance host runs on UTC and stays there: `/etc/localtime` lives on the
read-only slot root, so a host timezone could not survive a slot switch and
`timedatectl` cannot write one at all on an A/B image.

What the EMS actually needs is the zone its containers run in, because that is
what decides when an hour-based control window opens — a winter midday charge
window set to hour 12 fires at 12:00 local only if the container agrees what
local means. Set it in the appliance UI under **Network → Timezone**, or in
`appliance.conf`:

```ini
timezone = Europe/Berlin
```

The value chosen in the UI is written to `/etc/ems-appliance-manager/timezone`,
which is a shared path, and it outranks the packaged default. It reaches the
containers as `TZ` the next time the deployment starts.

## Supported platforms

| Item | Supported |
|---|---|
| Hardware | Raspberry Pi 4, Raspberry Pi 5 — **reverse-engineered**: derived and tested in emulation, not confirmed on a physical board (see [the hardware gate](ab-hardware-validation.md)) |
| Operating system | Raspberry Pi OS 64-bit (Trixie). The appliance image is built from Trixie; the manager package also installs on Bookworm |
| Architecture | `arm64` only |
| Package | `ems-appliance-manager_<version>_arm64.deb` |

Other boards and 32-bit systems are out of scope for this release.

The **Raspberry Pi 3 and 3B+ cannot run the A/B appliance image**, and that is
not a sizing decision that a future release could revisit cheaply: the image
uses a GPT layout and the EEPROM boot selector that Pi 4 and Pi 5 firmware
provide, and a Pi 3 boot ROM reads neither. See
[adr/raspberry-pi-3-ab-support.md](adr/raspberry-pi-3-ab-support.md) for the
evidence, and [../user/hardware-requirements.md](../user/hardware-requirements.md)
for what a Pi 3 can and cannot be used for.

## Three installation shapes

```text
Single-slot installation
  the .deb on an existing Raspberry Pi OS system
  → classic package updates
  → a major OS generation change requires re-imaging

Single-slot appliance image
  ems-solarflow-appliance-<version>-<rpi4|rpi5>-arm64-single.img.xz, flashed
  → one writable root, patched by apt
  → the Manager updates as a .deb
  → no slot to fall back to: recovery is reflash plus backup restore

A/B appliance image
  ems-solarflow-appliance-<version>-<rpi4|rpi5>-arm64-ab.img.xz, flashed
  → image-based fail-safe host updates
  → the inactive slot is staged, trial-booted, health-checked
  → commit, or automatic fallback to the previous slot
```

### Which image

Both images are the same appliance. They differ only in how the operating
system underneath it is patched, and that choice cannot be changed later
without reflashing — so it is worth one minute now.

| | Single-slot image | A/B image |
|---|---|---|
| OS security patches | `apt`, minutes, a few MB | a rebuilt image, ~877 MB written per update |
| Failed OS update | recover by hand: reflash and restore a backup | automatic rollback to the previous slot |
| Card wear | ordinary | a full slot rewritten per OS release |
| Card space | the whole medium is one root | two fixed slots plus a shared partition |
| Physical access needed after a bad update | yes | no |

Take the **A/B image** if the appliance will be somewhere you would rather not
have to reach — a cellar, a meter cabinet, another building. That is what it is
for, and it is the default recommendation.

Take the **single-slot image** if you would rather patch weekly with `apt` than
write most of a gigabyte to an SD card for every OS release, and you can get to
the machine if something goes wrong.

### How each shape gets its updates

| | `.deb` on your own OS | Single-slot image | A/B image |
|---|---|---|---|
| Operating system | `apt` | `apt` | a signed image, trial-booted |
| Appliance Manager | a new `.deb`, by hand | a new `.deb`, by hand | comes with the OS image |
| Admin and EMS containers | the Admin console | the Admin console | the Admin console |

The Manager is installed with `dpkg`, not from an APT repository, so `apt` does
not offer it an upgrade on either of the first two shapes: you download the new
`.deb`, check its checksum and install it, exactly as at first install.
`sudo ems-appliance rollback-manager` reinstalls the previous one if a new
package misbehaves. Only the A/B image ships the Manager as part of the
operating-system image, because there the root filesystem is read-only and
`dpkg` could not write to it anyway.

**An installation is never converted in place** — not a `.deb` installation
into either image, and not one image into the other. The partition tables are not
different sizes of the same thing; they are different table types with
different partition counts. Moving means flashing the other image and restoring
an EMS backup onto it. The appliance does not resize, move or repartition a
running installation's storage, and no such action is reachable from the
browser or the agent. All three shapes stay fully supported. See
[ab-os-updates.md](ab-os-updates.md) and
[adr/single-slot-image-variant.md](adr/single-slot-image-variant.md).

## Install

```bash
sudo apt install ./ems-appliance-manager_0.1.0_arm64.deb
```

Verify the checksum first:

```bash
sha256sum -c ems-appliance-manager_0.1.0_arm64.deb.sha256
```

Release artefacts are the package, its SHA-256 checksum, a detached signature,
release notes, the supported OS versions and the upgrade instructions.

The package installs:

```text
/usr/lib/ems-appliance-manager/appliance/   Python package and static UI assets
/usr/bin/ems-appliance                      host CLI
/usr/lib/systemd/system/ems-appliance-agent.service
/usr/lib/systemd/system/ems-appliance-web.service
/usr/lib/systemd/system/ems-appliance-export.service
/usr/lib/systemd/system/ems-appliance-export.path
/usr/lib/systemd/system/ems-appliance-backup-access-disable.service
/usr/lib/systemd/system/ems-appliance-host-identity.service
/usr/lib/systemd/system/ems-appliance-persistence.service
/usr/lib/systemd/system/ems-appliance-ab-health.service
/usr/lib/systemd/system/ems-appliance-slot-bootstrap.service
/usr/lib/systemd/system/ems-appliance-grow-persistent.service
/usr/lib/systemd/system/ems-appliance-grow-root.service
/usr/lib/ems-appliance-manager/setup-export-root.sh
/usr/lib/ems-appliance-manager/backup-account.sh
/usr/lib/ems-appliance-manager/install-admin-console.sh
/usr/lib/ems-appliance-manager/grow-persistent.sh
/usr/lib/ems-appliance-manager/grow-root.sh
/usr/lib/tmpfiles.d/ems-appliance-manager.conf
/etc/logrotate.d/ems-appliance-manager
/etc/ems-appliance-manager/appliance.conf
/etc/ems-appliance-manager/allowed-images.conf
```

Three further files are **generated** from `appliance.conf`, not shipped, so
they always agree with the configured host paths:

```text
/etc/ems-appliance-manager/host-paths.env
/etc/systemd/system/ems-appliance-export.path.d/host-paths.conf
/etc/ssh/sshd_config.d/ems-appliance-backup.conf
```

A fourth is generated from the account rather than from the configuration, when
the installation creates the backup account:

```text
/usr/lib/ems-appliance-manager/backup-account-origin
```

It describes the account that was created, and it is what lets a flashed A/B
image establish ownership of an account it could not create at runtime — see
[the security model](security-model.md#the-account-the-image-carries). On a
system you installed the package on yourself it is written and never read.
Purge removes it.

and creates the service accounts `ems-appliance-web` (unprivileged, `nologin`),
`ems-backup` (read-only file export) and `ems-deploy` (owner of the hosted
deployment and the uid its containers run as), plus the shared `ems-appliance`
group that guards the agent socket.

Installation never moves, rewrites or restructures an existing EMS installation
under `/opt/ems-solarflow`. `ems-deploy` only takes ownership of a deployment
root nothing was ever installed in — which is what a freshly flashed appliance
has. A root that already holds files keeps the owner it has, and the appliance
runs the containers as that owner.

## Moving the host paths

`install_root` and `export_root` in `appliance.conf` may be changed. After
either changes, run

```bash
sudo ems-appliance host-config --apply
```

which regenerates all three derived artefacts as **one transaction**: it
validates the new configuration, writes the environment file, the path-unit
drop-in and the sshd Match policy, then reloads systemd, re-arms the watcher
and checks that sshd still accepts its configuration. If any step fails, the
previous set of generated files is restored, so a new environment file can
never end up next to an old chroot directory.

Both roots must be absolute paths to real directories, must not be reached
through a symbolic link, and must not overlap in either direction. A separate
data partition is a **mount** at the configured path, not a symlink.

`backup_user` is not configurable: this package creates, confines and removes
exactly one account, and a different value is refused when the configuration is
loaded.

`ems-appliance host-config` (without `--apply`) shows what is configured and
whether every generated artefact still matches — including the chroot directory
the running sshd would apply and the path the running watcher follows. Any
mismatch is drift, and `verify-install` fails on it.

## What "installed" means

On a live host the package refuses to report success over a broken appliance.
Package configuration **fails** when:

```text
a required directory could not be created
the state migration lost data (a refused or failed move)
the agent service did not start
the web service did not start
the web account cannot reach the agent socket
agent state does not belong to root alone
the read-only SFTP export root could not be configured
the export path watcher did not start
the generated host configuration disagrees with appliance.conf
a backup account exists that this package did not create
```

The last step of the postinst is the same check you can run yourself:

```bash
sudo ems-appliance verify-install
sudo ems-appliance verify-install --json
```

A **migration conflict** is different: both copies are preserved, the old one is
kept beside the new one as `<name>.migrated-conflict`, and the install succeeds
with a warning. It needs your decision, not a failed package.

Optional host features never fail the package. Docker, NetworkManager, OpenSSH
and `acl` are each reported as `unavailable` with the capability that is missing:

```text
unavailable  docker: docker is not installed; Admin container management is unavailable
```

### Installing into an image-build root

When the package is installed into a chroot while a Raspberry Pi image is
assembled, there is no running systemd. Starting services there is impossible,
so it is **deferred, not failed**: the layout is created, ownership is applied,
the units are enabled for the first boot, and the postinst prints

```text
ems-appliance: no running systemd: the services are enabled and start on first boot.
```

Verification in that context uses `ems-appliance verify-install --offline`,
which reports the unit, socket and connectivity checks as `deferred` and still
fails on a missing directory or wrong ownership.

## Build the package from source

```bash
packaging/appliance/build-deb.sh --output dist
```

The script stages the package, builds it with `dpkg-deb --root-owner-group`,
writes `<package>.sha256` and prints the signing command. Sign the artefact
before publishing:

```bash
gpg --armor --detach-sign dist/ems-appliance-manager_0.1.0_arm64.deb
```

### Smoke-test a built package in a clean guest

Two drivers run the *same* guest check
(`scripts/appliance-guest-smoke.sh`), so the two architectures are held to one
standard:

```bash
scripts/appliance-smoke-amd64.sh     # Debian 13 systemd guest, amd64
scripts/appliance-smoke-arm64.sh     # booted Debian 13 aarch64 VM under QEMU
```

Each one builds the package for its architecture, verifies the checksum,
installs it into a throw-away guest and then checks: `verify-install`, both
units active, socket ownership, that `ems-appliance-web` can use the socket and
can neither list nor read agent state or the audit log, the packaged HTTP flow
(first password, login, status, audit health, logout), that no password reached
the audit log, the chroot-safe export root, and a reinstall.

Both remove their guest and build directory on exit and change nothing on the
developer host.

Exit status is deliberately three-valued, so a run that could not happen is
never mistaken for a pass:

| Status | Meaning |
|---|---|
| `0` | `RESULT: PASS` — every check passed in the guest |
| `1` | `RESULT: FAIL` — the guest ran and a check failed |
| `2` | the command line is wrong; nothing was attempted |
| `3` | `RESULT: NOT RUN` — the host lacks Docker or QEMU; nothing was tested |

An option that takes a value refuses the end of the arguments, an empty value
and a following option, so `--image --keep` is a usage error rather than a run
against a path called `--keep`. A value that legitimately starts with a dash
uses the explicit form:

```bash
scripts/appliance-smoke-arm64.sh --image=/path/to/image.qcow2
```

The ARM64 driver needs a real aarch64 VM:

```bash
sudo apt install qemu-system-arm qemu-efi-aarch64 cloud-image-utils xorriso \
                 curl gpgv debian-archive-keyring
```

It boots `debian-13-genericcloud-arm64.qcow2` with EFI firmware and hands the
package and the guest scripts in on a second ISO. Under full emulation on an
x86 host this takes a long time; on an aarch64 host with `/dev/kvm` it uses KVM
automatically.

The guest's record does not travel on the boot console. `agetty` claims that
console and calls `vhangup()` on it, which revokes every descriptor already
open there, so a tier that logged to it lost the rest of its output and died on
its next write — a real failure that took two runs to become readable. The
record goes to a virtio-serial port nothing else writes to, delivered once on a
fresh open after the tier has finished, and is preserved as `evidence.log`
beside `console.log`. The console keeps the boot log plus an
`APPLIANCE_EVIDENCE stage=...` heartbeat, so a guest that never finishes still
names the stage it stopped in. `result.txt` records which channel the record
came from; reading it from the shared console is a labelled fallback, never a
silent one.

For a release validation the run has to be reproducible, so the driver refuses
to guess:

```bash
scripts/appliance-smoke-arm64.sh --image ~/images/debian-13-arm64.qcow2
```

| Input | Rule |
|---|---|
| Base image | `--image` is preferred; a downloaded image is verified against `SHA512SUMS` from the same directory |
| Checksum manifest | verified against `SHA512SUMS.sign` with `gpgv` and a Debian keyring whenever both are present |
| Unverifiable image | `RESULT: NOT RUN` unless `--allow-unverified-image` is passed explicitly |
| UEFI variable store | taken from the firmware package's own `AAVMF_VARS.fd`; `AAVMF_CODE.fd` without its template reports `NOT RUN` rather than booting against a blank store |
| Guest architecture | the guest proves `dpkg --print-architecture` is `arm64` before the package is installed, and a `PASS` without `aarch64` on the console is rejected |

Working files are removed on exit unless `--keep` is given.

`--output DIR` preserves the evidence a result has to be reproducible from.
Each run gets its own `DIR/run-<run id>/` directory, so a console log from an
earlier run can never be read as this one's:

```text
result.txt              the verdict, machine-readable — written last
inputs.txt              firmware, base image and package with their checksums
run.txt                 run id, start and end time, driver revision, checksums
environment.txt         the host as the run found it, tool by tool
missing-requirements.txt  what the run needed and did not find
console.log             the guest serial console, in full          (once qemu ran)
qemu-command.txt        the exact emulator invocation              (once qemu ran)
qemu-status.txt         how the emulator ended                     (once qemu ran)
```

The first five are owed by **every** terminal result, including `NOT RUN` and a
usage error, and they are written before the first check that can end the run —
an evidence directory an operator was pointed at is never left empty. The last
three are owed only once the emulator actually started, because a run that
stopped before it has no console log to preserve. `DIR/latest.txt` names the
most recent run directory and is replaced atomically.

The only terminal outcome that owes no evidence is one where the `--output` path
itself is the fault, since there is nowhere to write it.

`result.txt` separates what the run proved from what it is allowed to be
consumed as:

```text
result:             PASS | FAIL | NOT RUN | USAGE ERROR
exit_code:          0 | 1 | 3 | 2
reason_code:        a stable code, e.g. required_tool_missing, firmware_unavailable
verified:           true | false
qemu_started:       true | false
evidence_complete:  true, and only written once every required file is there
verification:       verified | unverified
release_gate:       pass | no
timeout:       none | expired | killed
```

A functional pass on an unverified base image exits zero and says
`release_gate: no`; only a verified pass may be consumed as a release gate. If
requested evidence cannot be written or copied, the run ends with `RESULT:
EVIDENCE INCOMPLETE` and a non-zero status — it never claims to have preserved
something it did not.

### Verify a built package without a Raspberry Pi

The maintainer scripts can be exercised on any Debian Bookworm system by
building the same package for the local architecture:

```bash
packaging/appliance/build-deb.sh --output dist --arch amd64
dpkg -i dist/ems-appliance-manager_0.1.0_amd64.deb
getent passwd ems-appliance-web ems-backup
stat -c '%n %a %U:%G' /var/lib/ems-appliance-manager
ems-appliance --version
dpkg --purge ems-appliance-manager
```

This checks the package layout, the service accounts, the directory modes and
that purge removes only appliance state. It is not a substitute for the
Raspberry Pi 4 and Raspberry Pi 5 appliance tests; only the real hardware
covers first boot, reboot persistence and the OS update path.

## Appliance layout

```text
/opt/ems-solarflow/            existing EMS installation, never restructured
  docker-compose.yml
  docker-compose.admin.yml     Admin service (when the Admin installer created it)
  .env.admin                   Admin image and tag
  config/config.json
  data/
  backups/
  admin/
    environment
    bootstrap-state.json

/etc/ems-appliance-manager/
  appliance.conf               host configuration, root-writable only
  allowed-images.conf          the image allowlist

/var/lib/ems-appliance-manager/      root:ems-appliance 0750
  web/                               ems-appliance-web
    auth/auth.json                   the appliance password (0600)
    sessions/
    ui-preferences/
  agent/                             root
    operations/                      durable operation records
    known-good/                      verified Admin history with digests
    compose-backup/
    package-state/
    recovery/
    ssh-keys/
    packages/                        previous Appliance Manager package

/var/log/ems-appliance-manager/      root:ems-appliance 0750
  web/appliance.log                  ems-appliance-web
  agent/operations.log               root
  audit/audit.log                    root
```

All paths are defined once in `appliance/paths.py` and validated against their
canonical base. The browser can never submit a filesystem path.

### Moving the EMS installation or the export root

`appliance.conf` is the single authority for both movable roots:

```ini
[appliance]
install_root = /opt/ems-solarflow
export_root = /srv/ems-appliance-export
```

Both must be absolute paths to real directories. A separate data partition is
supported as a mount, not as a symlink, and a path containing spaces, quotes or
backslashes is refused rather than quoted differently by each consumer.

After changing either value, regenerate the derived files:

```bash
sudo ems-appliance host-config --apply
sudo systemctl daemon-reload
sudo systemctl restart ems-appliance-agent.service ems-appliance-web.service
sudo systemctl start ems-appliance-export.service
```

`host-config --apply` writes two root-owned generated files:

| File | Read by |
|---|---|
| `/etc/ems-appliance-manager/host-paths.env` | the agent, web and export units (`EnvironmentFile=`), `setup-export-root.sh`, the purge script |
| `/etc/systemd/system/ems-appliance-export.path.d/host-paths.conf` | the export watcher, which cannot expand variables itself |

`ems-appliance host-config` without `--apply` prints what is configured and
whether the generated files still agree; `verify-install` fails on drift.

The web service can read agent state through the shared group but cannot write
it; it reaches privileged state through the agent API.

### Upgrading from the previous shared layout

An installation created before the split is migrated automatically by the
postinst, and can be re-run at any time:

```bash
sudo ems-appliance migrate-state
```

The migration copies and verifies before it removes anything, refuses a
symlinked source instead of following it, and keeps both copies with a
recoverable finding when the old and the new location disagree. Running it
again changes nothing.

## First start

The first boot requires a wired Ethernet connection. The image ships no WLAN
profile and there is no way to preconfigure one on the card; WLAN is configured
afterwards from the Network page.

Open the appliance:

```text
http://ems-solarflow.local:8088
```

The name is published by `avahi-daemon`, which the image installs. When it does
not resolve — some networks and some Windows configurations block mDNS — use the
address instead. Look the appliance up in your router's list of connected
devices (usually under *DHCP*, *Clients* or *Network*; the entry is named
`ems-solarflow`), then open `http://<address>:8088`.

The first start requires:

1. **Create a password.** There is no default one. The same password opens the
   Appliance Manager, the Admin console and the dashboard, and changing it from
   any of them changes it for all three.
2. **Confirm the hostname** (Network section).
3. **Confirm the timezone** (Overview → system time).
4. **Review the network state** (Network section).
5. **Review the Admin installation state** (Admin section).

Before authentication the interface exposes nothing but the login page and
whether a password exists yet.

Password rules: any non-empty password, hashed with PBKDF2-SHA256
(600 000 iterations). There is no minimum length: the same password opens the
Admin console and the dashboard, which have never imposed one, and how strong it
is, is the operator's decision about their own device. Sessions use a
`HttpOnly`, `SameSite=Strict` cookie, an
idle timeout, an absolute maximum lifetime, CSRF validation on every mutation
and login rate limiting.

## Reset the password locally

Password recovery never depends on the EMS Admin. On the console, or over SSH as
an already authorised host user:

```bash
sudo ems-appliance password-reset
```

The reset rotates a generation marker, so **every existing browser session is
signed out immediately**. There is no unauthenticated network reset endpoint.

## Host CLI

```bash
sudo ems-appliance status              # host, Docker, Admin and update summary
sudo ems-appliance status --json
sudo ems-appliance repair              # inspect the Admin deployment
sudo ems-appliance repair --apply      # execute the listed repair actions
sudo ems-appliance operations          # recent appliance operations
sudo ems-appliance password-reset
sudo ems-appliance rollback-manager    # reinstall the previous Appliance package
sudo ems-appliance allowlist           # print the agent operation allowlist
sudo ems-appliance host-config         # the configured host roots and their drift
sudo ems-appliance host-config --apply # regenerate the derived host-path files
sudo ems-appliance backup-access       # what the SFTP confinement is right now
```

The web interface is never the only recovery path.

## Service control

```bash
systemctl status ems-appliance-agent.service
systemctl status ems-appliance-web.service
journalctl -u ems-appliance-agent -n 200
```

## Appliance Manager self-update

The Appliance Manager updates through its signed package, separately from Admin
updates. Its current version is visible in Settings.

`sudo ems-appliance rollback-manager` reinstalls the package that was running
before the current one, from
`/var/lib/ems-appliance-manager/packages/previous.deb`.

**It has something to reinstall only when the manager installed the update
itself.** dpkg keeps no copy of the archive it unpacked and hands its
maintainer scripts no path to it, so an update applied with `apt` or
`dpkg --install` by hand leaves nothing behind to go back to, and the command
says so rather than pretending otherwise. A freshly flashed appliance is in the
same position until its first managed update: its manager arrived inside the
image rather than through an install.

## Image integration

For a prebuilt appliance image: include the package, enable both units, create
the directory layout through the shipped tmpfiles rules and verify on first
boot that `http://ems-solarflow.local:8088` serves the first-run password page.
Publish the image checksum next to the package checksum.
