# Raspberry Pi Appliance Manager — Installation

## Supported platforms

| Item | Supported |
|---|---|
| Hardware | Raspberry Pi 4, Raspberry Pi 5 |
| Operating system | Raspberry Pi OS 64-bit (Bookworm) |
| Architecture | `arm64` only |
| Package | `ems-appliance-manager_<version>_arm64.deb` |

Other boards and 32-bit systems are out of scope for this release.

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
/usr/lib/ems-appliance-manager/setup-export-root.sh
/usr/lib/ems-appliance-manager/backup-account.sh
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

and creates the service accounts `ems-appliance-web` (unprivileged, `nologin`)
and `ems-backup` (read-only file export), plus the shared `ems-appliance` group
that guards the agent socket.

Installation never moves, rewrites or restructures an existing EMS installation
under `/opt/ems-solarflow`.

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
| `3` | `RESULT: NOT RUN` — the host lacks Docker or QEMU; nothing was tested |

The ARM64 driver needs a real aarch64 VM:

```bash
sudo apt install qemu-system-arm qemu-efi-aarch64 cloud-image-utils xorriso \
                 curl gpgv debian-archive-keyring
```

It boots `debian-13-genericcloud-arm64.qcow2` with EFI firmware, hands the
package and the guest script in on a second ISO, and reads the result from the
serial console. Under full emulation on an x86 host this takes a long time;
on an aarch64 host with `/dev/kvm` it uses KVM automatically.

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

Open the appliance:

```text
http://ems-solarflow.local:8080
```

The first start requires:

1. **Create an Appliance Manager password.** There is no default password. It is
   independent from the EMS Admin password, so you can still sign in when the
   EMS install root is unreadable.
2. **Confirm the hostname** (Network section).
3. **Confirm the timezone** (Overview → system time).
4. **Review the network state** (Network section).
5. **Review the Admin installation state** (Admin section).

Before authentication the interface exposes nothing but the login page and
whether a password exists yet.

Password rules: at least 12 characters, hashed with PBKDF2-SHA256
(600 000 iterations). Sessions use a `HttpOnly`, `SameSite=Strict` cookie, an
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

The Appliance Manager updates through its signed OS package, separately from
Admin updates. Its current version is visible in Settings. Install a new
package with `apt`; the previous package is retained under
`/var/lib/ems-appliance-manager/packages/previous.deb` so
`sudo ems-appliance rollback-manager` can put it back.

## Image integration

For a prebuilt appliance image: include the package, enable both units, create
the directory layout through the shipped tmpfiles rules and verify on first
boot that `http://ems-solarflow.local:8080` serves the first-run password page.
Publish the image checksum next to the package checksum.
