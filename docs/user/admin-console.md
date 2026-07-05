# EMS SolarFlow Admin Console

The Admin Console (product name **EMS SolarFlow Admin**) is the local browser UI
for setup and maintenance. It runs next to EMS, not inside the control loop. EMS
still owns the control logic; the Admin Console is UI and orchestration only.

The Admin Console is a Docker path. Run it only on a trusted local machine.

## Use it for

- first setup
- device discovery
- config generation
- diagnostics
- updates
- backups
- restore

## Two flows

The Admin Console start screen detects your install state and recommends one of
two flows. It never acts silently.

- **Set up a new system** — for a fresh install or a deliberate reinstall.
  See the [Admin setup guide](admin-setup.md).
- **Manage my existing system** — for updates, config changes, diagnostics,
  backups and restore. See the [Admin maintenance guide](admin-maintenance.md).

Mutating actions preview the change and ask for confirmation. Config apply,
guided upgrade and restore back up what they replace first.

## It does not replace EMS

- EMS still runs the control loop and remains the source of truth.
- EMS owns config semantics and backup/restore behavior — every backup is a
  normal EMS backup archive.
- Docker is the runtime. The Admin Console orchestrates EMS containers; it does
  not replace them.

## Files

| Path | Purpose |
| --- | --- |
| `config/config.json` | EMS config |
| `data/` | EMS runtime data (state, history, optional analytics) |
| `data/backups/` | EMS backup archives |
| `data/admin/` | Admin Console state, temporary files and logs |

## Start

Install and start the Admin Console in a local EMS folder:

```bash
mkdir -p ems-solarflow-api-control
cd ems-solarflow-api-control
curl -fsSLO https://raw.githubusercontent.com/basecubedev/ems-solarflow-api-control/main/deploy/admin/install-admin-console.sh
sh install-admin-console.sh
```

Then open:

```text
http://127.0.0.1:8090
```

## Login

The Admin Console uses the same password as the EMS Dashboard.

On the first start, if no password exists yet, the first browser user creates it.
The password is stored in `config/dashboard-auth.json` and is shared with EMS.

After that, log in with the EMS Dashboard password.

### Networking

The default uses **host networking**. EMS SolarFlow is a local LAN system, so
host networking lets discovery see the LAN more like a local host process, which
is the most reliable mode. The UI is then also reachable from another device on
your LAN at `http://<host-ip>:8090`.

Bridge networking is available with `--bridge`:

```bash
sh install-admin-console.sh --bridge
```

In bridge mode the container is isolated from the host network, Docker port
publishing controls how the UI is reached (`127.0.0.1:8090` by default), and
automatic LAN discovery can be less reliable — enter your LAN CIDR manually if a
scan sees only Docker networks.

Contributors who build from source use `deploy/admin/start-admin-setup.sh`.
See the [Developer Setup guide](../developer/developer-setup.md).

## Optional HTTPS

The Admin Console uses HTTP on port `8090` by default.

You can optionally enable a second HTTPS listener on port `8091` with `--https`:

```bash
sh install-admin-console.sh --https
```

HTTP stays available, so you are not locked out if your browser does not trust
the generated certificate. HTTPS is an additional URL
(`https://<host>:8091`), never a redirect.

If the Admin Console generates a self-signed certificate, your browser will show
a certificate warning. This is expected for local installations. Use HTTPS only
if you understand this warning or provide your own trusted certificate.

Do not expose the Admin Console HTTP or HTTPS ports to the internet.

## Safety

The Admin Console is designed for a trusted local EMS host or trusted LAN. The
Zendure local APIs are not encrypted. Do not expose the Admin Console — or the
EMS ports — to the internet. A deployment-capable Admin container controls the
host Docker engine, which is effectively root-equivalent.

Full technical reference: [admin-discovery.md](../technical/admin-discovery.md).
