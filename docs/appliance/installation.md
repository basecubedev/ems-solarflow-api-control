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
/usr/lib/tmpfiles.d/ems-appliance-manager.conf
/etc/logrotate.d/ems-appliance-manager
/etc/ems-appliance-manager/appliance.conf
/etc/ems-appliance-manager/allowed-images.conf
```

and creates the service accounts `ems-appliance-web` (unprivileged, `nologin`)
and `ems-backup` (read-only file export), plus the shared `ems-appliance` group
that guards the agent socket.

Installation never moves, rewrites or restructures an existing EMS installation
under `/opt/ems-solarflow`.

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

/var/lib/ems-appliance-manager/
  auth.json                    the appliance password (0600)
  state.json
  operations/                  durable operation records
  known-good/                  verified Admin history with digests
  ssh-keys/
  compose-backup/
  packages/                    previous Appliance Manager package

/var/log/ems-appliance-manager/
  appliance.log
  audit.log
  operations.log
```

All paths are defined once in `appliance/paths.py` and validated against their
canonical base. The browser can never submit a filesystem path.

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
