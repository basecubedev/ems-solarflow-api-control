# EMS SolarFlow Admin Console Technical Reference

This is the technical reference for the Admin Console. For a short user overview, see
[admin.md](../user/admin-console.md); for the two guided flows, see
[setup/admin-setup.md](../user/admin-setup.md) (Set up a new system) and
[setup/admin-maintenance.md](../user/admin-maintenance.md) (Manage my existing
system). This page documents the internals: the setup wizard, release and
build-identity gating, network discovery, deployment-capable Docker setup, and
security.

Admin sits next to EMS. It is **not** part of the control loop and does not
replace any EMS logic — EMS remains the source of truth. Admin covers device
discovery, config generation and apply, guided upgrades, deployment, and
backup/restore, all through EMS-owned tooling. Device discovery — finding
supported EMS devices on the local network and showing them in the EMS dashboard
style — is described in detail below.

> For normal users, use `install-admin-console.sh` (see
> [setup/admin-setup.md](../user/admin-setup.md)); it runs the published image with
> no Git checkout. This page documents the technical/source and runtime behavior,
> including the local-build launcher `deploy/admin/start-admin-setup.sh`.

## Layout

The UI has two top-level tabs. **Setup** (the default) is a compact
step-by-step wizard with a stepper header and three steps — **01 Release**,
**02 Devices**, **03 Config**. Only the active step shows its full content; the
others collapse to a compact status in the stepper (e.g. `Ready`, `3 devices`,
`Draft ready`). **Devices** and **Config** stay locked until the Release step
reports its resources ready, and `Next` is disabled until then.

The Release step reads public release metadata from GitHub and downloads the
selected release source archive. It extracts only the existing setup resources
into `data/admin/releases/<tag>/` for local previews or
`$EMS_ADMIN_DATA_DIR/releases/<tag>/`: `config.template.json`,
`docker-compose.example.yml`, both `install-docker` scripts, and
`deploy/docker/*`. A manifest records the concrete tag and cached paths, and
the selection pointer is stored under
`$EMS_ADMIN_DATA_DIR/state/selected-release.json`. The selected cached release
survives an Admin restart. If GitHub is unavailable, already cached releases
remain selectable.

Admin Setup is a Docker-only path. Stable releases from `v0.6.0` onward are
supported when GitHub confirms that the tag contains the config template,
Linux and Windows Docker installers, Compose example, and `deploy/docker`
resources. Older releases remain visible but disabled. Release candidates
newer than the support floor remain selectable with an **RC / not stable**
warning.

The synthetic `latest` option maps the rolling Docker channel to setup
resources from the repository's `main` branch. It is selectable when those
resources can be verified, but it is never treated as stable and never replaces
the newest supported stable release as the default. If stable resources cannot
be found, `latest` is the fallback. Resource availability is checked again
during preparation before the strict extraction whitelist is applied.

The build-identity gate only applies to the **maintenance/upgrade** flow, which
requests the release list with `?flow=upgrade` (`list_releases(for_upgrade=True)`).
**Guided Setup** is a fresh install with no running build to protect, so it lists
releases without the gate (`for_upgrade=False`): every supported release
(`>= v0.6.0`) with available Docker resources is selectable, including legacy
`v0.6.x` images that predate the build-identity labels. Only the running-build
comparison below is skipped for Setup — the `< v0.6.0` filter and `latest`-first
ordering are unchanged.

In the upgrade flow, release selection only ever allows real upgrades; downgrades
belong to the Backup/Restore flow. When the running EMS build can be inspected
(the running container's image, or the compose-declared image when it is stopped),
each target is compared by build identity rather than tag name alone, because
`latest` is a channel, not a version. In order: an identical image digest is
`already_current`; a `latest` **target** is a rolling channel switch and is always
`upgrade_available` (basis `channel`) unless it is that same image — it is never
blocked as older-than-running or already-current, so the list never dead-ends when
the running build is the newest stable; two comparable SemVer tags require the
target to be `>=` current (a lower target is `downgrade_blocked`, even if its build
serial is higher); when a running `latest` makes SemVer incomparable the monotonic
`build_serial` decides (`upgrade_available` or `older_than_running_build`); and
when the target image is not local yet its identity cannot be settled from the
listing alone, so it is `identity_unknown`. Each release carries its
`upgrade_state`. Proven non-upgrades (`older_than_running_build`,
`downgrade_blocked`, `already_current`) are non-selectable with a short reason
(`latest` excepted, as above). An `identity_unknown` target stays selectable:
preparation and Guided Upgrade pull the target image and re-inspect its labels,
then refuse it only if it is genuinely older or still unverifiable — so the guard
lives in the backend, not only the UI, and a not-yet-local stable target is not
blocked prematurely.

Older published images (`v0.6.x`) predate the build-identity labels. When both
sides lack a `build_serial` but carry comparable supported SemVer tags, the
SemVer comparison is authoritative on its own: `v0.6.0 -> v0.6.1` is a normal
upgrade (still `upgrade_available`), `v0.6.1 -> v0.6.0` is `downgrade_blocked`,
and each such upgrade carries an `upgrade_warning` noting the SemVer fallback.
Only the unprovable case — a running `latest` whose build cannot be ordered
against an unlabeled stable — stays `identity_unknown` and blocked. Setting
`ADMIN_ALLOW_LEGACY_UNVERIFIED_UPGRADES=true` is a test/development override that
lets such an unlabeled, supported legacy target through with a clear warning
(`upgrade_state=upgrade_available`, basis `legacy_unverified`). The override only
applies to that unprovable fallback: a SemVer-proven downgrade and a
`build_serial`-proven older build are never relaxed, and build identity remains
preferred whenever the labels are present.

The Admin does not duplicate installer behavior: the cached files are the same
Docker install and Compose resources shipped in normal EMS releases. This step
does not run either installer, start Docker, or write `config.json`. The cached
template is validated, exposed read-only at `GET /api/setup/config-template`,
and used by the Config step as the draft base. The
`POST /api/setup/config-preview` endpoint accepts the browser's selected-device
draft and returns a generated config plus structured validation. The same draft
can be downloaded through `POST /api/setup/config/download` or saved atomically
to the fixed Admin-managed `generated/config.json` path through
`POST /api/setup/config/write`. Saving to `generated/config.json` never targets
an EMS runtime config and requires explicit confirmation before replacing an
existing generated file.

The explicit `POST /api/setup/config/apply` action is the one path that does
write the real EMS config. It resolves the target through the shared install
context (`<EMS_INSTALL_DIR>/config/config.json` for a standard Docker-first
layout, never `/app/config/config.json` when `EMS_INSTALL_DIR` is available),
validates the generated config, backs up any existing config to
`data/admin/backups/config/config-before-admin-apply-YYYYMMDD-HHMMSS.json`, then
writes atomically. Fresh installs create `config/config.json` without a backup;
existing installs are always preserved as a backup before being replaced. Apply
touches only `config.json` — it never moves `data/`, `docker-compose.yml`, or
runtime databases. Preview stays non-destructive.

Verified discovered devices replace only the template's `devices` and
`grid_meter` values; all other release-specific defaults remain intact. The
planned image reference is shown, but pulling it remains part of the later
Deployment step.

If a concrete active or prepared image tag is found, older releases are
disabled. Downgrades remain intentionally unsupported until a later
Backup/Restore flow exists. A moving `latest` image tag is not treated as a
concrete installed version.

Discovery is deferred until the Devices step is first opened and then runs once
per session (mDNS keeps polling on its own). Technical lists (detected networks,
manual CIDR scan, mDNS controls, ignored devices, MQTT broker candidates, config
preview) live in collapsed details so the page stays short. **Diagnostics /
Advanced** holds the Deployment, System, and Network / WiFi placeholders for
later phases.

## Discovery capabilities

- Serves a small local admin web UI (stdlib `http.server`, vanilla JS).
- Suggests detected private/local networks so you can start a scan without
  knowing your CIDR (see [Network suggestions](#network-suggestions)).
- Optionally probes common home-router gateway IPs so a routed VLAN/subnet can
  be suggested for a scan (see [Gateway candidate probe](#gateway-candidate-probe)).
- Runs live mDNS discovery for Zendure `_http._tcp` and `_zendure._tcp`
  services in the background and merges verified devices into the list (see
  [Live mDNS discovery](#live-mdns-discovery)).
- Discovers MQTT broker candidates through `_mqtt._tcp` mDNS and automatic TCP
  probes on ports 1883 and 8883 whenever the user starts a network scan.
- Scans a manually entered private CIDR range (e.g. `192.168.178.0/24`).
- Probes known local HTTP device APIs and classifies responses:
  - Zendure local HTTP device — `GET /properties/report` → role `inverter`
  - Shelly Pro / Gen2 meter — `GET /rpc/Shelly.GetStatus` → role `grid_meter`
  - Shelly 3EM Gen1 meter — `GET /status` → role `grid_meter`
  - everHome EcoTracker — `GET /v1/json` → role `grid_meter`
- Shows each device with IP, API family/type, suggested role, serial (or
  `missing`), confidence, and config readiness.

## Current limitations

- The Admin Console requires a password, but is still LAN-only by design. Run it
  only on a trusted local network (see [Security note](#security-note)).
- InfluxDB restore is not available in Admin. InfluxDB backups can be created,
  inspected, listed and deleted, but restore uses the EMS CLI (see
  [setup/admin-backup-restore.md](../user/admin-backup-restore.md)).
- No SSDP/ARP discovery and no `ping`/`nmap`/`arp` shell-outs.
- MQTT discovery is endpoint-only: no credentials, login, subscriptions, topic
  scanning, or device extraction from topics.

Config generation/apply, guided upgrade, deployment, and a full preview-first
backup/restore flow are implemented (see the setup and maintenance guides). The
discovery result model is shaped so a device can be promoted (config name,
display name, role, per-device parameters) without a schema change.

## Network suggestions

The page lists detected local networks (`GET /api/discovery/networks`) so you can
start a scan without knowing your CIDR. Each entry shows the interface, address,
a **Recommended** or **Advanced** badge, and a `Scan this network` button.
Manual CIDR entry always remains available.

Detection is Linux stdlib only (`socket.if_nameindex` + `fcntl` ioctls +
`/proc/net/route`) — no shell-out, no packet capture, and it runs only when the
API is requested or you click **Refresh**. It never starts a scan on its own.
Networks broader than `/24` are narrowed to a scan-safe `/24` around the
interface address, and public ranges are never suggested.

Likely Docker/container bridge networks (`172.16.0.0/12`, or `docker*`/`br-*`/
`veth*` interfaces) are de-emphasized: they are moved out of the primary list
into a collapsed **Advanced: Docker/container networks** section. They are not
removed — you can still expand and scan them manually.

## Gateway candidate probe

Directly connected networks are detected automatically, but IoT/energy devices
often live in another routed VLAN/subnet behind the home router. Alongside
direct-route detection, the page automatically probes a short list of common
gateway addresses (`POST /api/discovery/gateway-probe`) and adds the matching
`/24` of any responder to the same network list. It re-runs on **Refresh**.

- Router API integration is intentionally out of scope; this is a generic,
  home-network-friendly heuristic.
- It uses cheap TCP connect probes (ports 80/443/53) with a short timeout and
  bounded parallelism — no ICMP/ping, no shell-out, no background loop.
- Candidates are `192.168.{0,1,2,10,20,30,50,100,178}.1`, with `.254`
  fallbacks for the same subnets.
- A responding candidate only means the network is **probably reachable**, not
  that it definitely exists. Reachable candidates are added to the same
  **Available networks** multi-select list (with a **Gateway** badge and a note
  that they were discovered indirectly and are not directly connected to this
  host). Non-responding candidates are simply not added. The probe runs
  automatically with network detection; there is no separate manual trigger.
- It never scans a full `/24` on its own — you still start the normal device
  scan explicitly. It never writes `config.json`.

The **Discovered devices** section has a **Keep previous results** toggle. When
on, devices from successive scans accumulate (useful when scanning several
networks one after another); when off, each scan starts from a fresh result.

In Docker bridge mode, reaching a routed VLAN depends on host routing; host
networking (below) gives the most realistic LAN/VLAN view.

## Live mDNS discovery

The page runs mDNS discovery in the background while it is open and merges
discovered Zendure devices into the same **Discovered devices** list.

- **Service types:** `_http._tcp` and `_zendure._tcp`. General HTTP services are
  ignored unless their instance name starts with `Zendure-`.
- **Verified, not assumed:** an mDNS hit is only a candidate. It is HTTP-verified
  the same way a network scan is (`GET /properties/report`) before it is shown as
  a real, config-ready device. Unverified candidates are not promoted.
- **Own discovery source:** mDNS **always merges** and **never clears** manual
  scan results. It does not depend on the **Keep previous results** toggle (that
  toggle only controls manual-scan replace-vs-keep behavior).
- **Dedup / update:** devices are keyed by serial number where possible; the same
  device found by mDNS and a network scan shows as one entry with both sources.
  IP/port and `last_seen` update when a known device reappears at a new address.
- **Stale marker:** a device is marked *stale* after ~2 minutes without an event
  and stays visible (marked *old* after ~10 minutes) until you clear results.
- **Status:** the **Automatic Zendure discovery** control shows whether discovery
  is running, disabled, or unavailable and reports the verified-device count.

API: `GET /api/discovery/mdns/status`, `GET /api/discovery/devices`,
`POST /api/discovery/mdns/enable`, `POST /api/discovery/mdns/disable`.
`GET /api/discovery/results` remains as a compatibility alias.

mDNS uses the `zeroconf` library, which is installed in the Admin image. It
remains optional for direct host-Python previews; if it is absent, automatic
discovery reports an unavailable dependency and the rest of discovery keeps
working. mDNS is link-local multicast: it typically only works within the same
LAN/VLAN, so **host networking is recommended** (see below). A separate IoT VLAN
needs mDNS reflection on the router, or you fall back to the gateway probe /
manual CIDR scan. No `config.json` is written.

## MQTT broker candidates

MQTT brokers are shown separately from EMS devices. The mDNS listener resolves
`_mqtt._tcp.local.` service name, hostname, address, port, and TXT data. The
normal network scan automatically checks the same selected or manually entered
validated network, using short bounded TCP connects to ports 1883 and 8883.
There is no separate MQTT network selector or probe button. An open port is only
a conservative broker candidate; it is not treated as config-ready.

API: `GET /api/discovery/mqtt-brokers`,
`POST /api/discovery/mqtt-brokers/refresh`, and
`POST /api/discovery/mqtt-brokers/probe` with `{"cidr": "192.168.178.0/24"}`.
Refresh restarts mDNS browsing and rechecks known broker endpoints. Probes do
not authenticate, create permanent connections, or subscribe to topics.

### Docker networking

Network mode and UI bind address are independent concerns. The **network mode**
(host vs bridge) changes what discovery can see; the **UI bind host**
(`127.0.0.1` vs `0.0.0.0`) changes what address the Admin server listens on. Do
not conflate the two.

The installed Admin Console (`install-admin-console.sh`) and the published-image
runtime Compose files default to **host networking**.

- **Host network (default):** improves LAN/mDNS/broadcast/subnet discovery by
  exposing the host's real interfaces, so detection finds your actual LAN. The
  web UI bind is controlled by the Admin server bind host; the image binds
  `0.0.0.0:8090`, so with host networking the UI is reachable on every host
  address. Use it only on a trusted local machine or trusted LAN.
- **Bridge network (`--bridge`):** the container usually only sees Docker
  networks (`172.17.0.0/16`, `172.18.0.0/16`, …), which are not useful for LAN
  discovery, so the UI shows a clear warning and you enter your LAN CIDR
  manually. Docker port publishing controls UI reachability (`127.0.0.1:8090` by
  default).

The source/developer launcher builds from a checkout and defaults to bridge; add
host networking with a Compose override instead of `--bridge`:

  ```bash
  deploy/admin/start-admin-setup.sh --hostnet
  # or manually, on top of any base compose file:
  docker compose \
    -f deploy/admin/docker-compose.yml \
    -f deploy/admin/docker-compose.hostnet.yml up --build
  ```

  Use host networking only on a trusted local machine — the admin UI is then
  reachable on the host's real addresses.

## Preview command

```bash
python3 scripts/serve_admin_preview.py --host 127.0.0.1 --port 8090
```

Then open http://127.0.0.1:8090, enter a local CIDR, and start a scan.

## Docker setup (default: deployment-capable)

The normal setup path is deployment-capable out of the box. The launcher
discovers the host Docker socket group id for you and starts the Admin container
in deployment-controller mode (Docker-out-of-Docker), so Step 04 can download
the EMS/InfluxDB images and Step 05 can start the prepared stack:

```bash
deploy/admin/start-admin-setup.sh
```

Then open http://127.0.0.1:8090. Add `--hostnet` to also scan the real LAN
(host networking). The Admin container controls the *host* Docker engine over
the mounted `/var/run/docker.sock`: EMS and InfluxDB run as sibling containers
on the same engine. It ships only the Docker **client** (CLI + Compose plugin);
there is no daemon inside the Admin container and no Docker-in-Docker.

The image is a `python:3.14-slim` base with `requests`, `zeroconf`, and the
Docker client — no Node, no build toolchain, a non-root user, and a read-only
root filesystem. It keeps `/app` read-only. The launcher resolves the real EMS
install root and its `./data/admin` directory to absolute paths and exports them
as `EMS_INSTALL_DIR` and `EMS_ADMIN_DATA_DIR`; the compose files mount each at
that same path inside the container (same-path mounting). This is required when
bind mounts are sent through the host Docker socket, since those sources must be
valid host paths. The launcher also exports the invoking host user's `PUID` and
`PGID`. Admin passes those values to the generated EMS deployment so its
container and mounted workspace use the same non-root identity.

> **Security:** mounting `/var/run/docker.sock` grants effectively
> root-equivalent control of the host through the Docker API. Run Admin Setup
> only on a trusted local machine and never expose the Admin Console to the internet.

Verify the status APIs:

```bash
curl -fsS http://127.0.0.1:8090/api/admin/status
curl -fsS http://127.0.0.1:8090/api/discovery/networks
curl -fsS http://127.0.0.1:8090/api/discovery/mdns/status
curl -fsS http://127.0.0.1:8090/api/setup/deployment/status
```

Step 04 prepares the standard install root as the live deployment target
(`${EMS_INSTALL_DIR}/config`, `.../data`, `.../docker-compose.yml`). It runs the
release bootstrap with `--no-start`, writes `PUID`/`PGID` to the install-root
`.env`, pulls the planned images, and verifies that `config/` and `data/` are
writable by that identity. Admin can now write these standard EMS layout files
directly; it is no longer a read-only config generator.

Because this writes real installation files, Step 04 refuses to replace an
existing standard install without explicit confirmation. If
`${EMS_INSTALL_DIR}/config/config.json` or `.../docker-compose.yml` already
exists and is not an install already owned by a matching Admin deployment marker,
prepare stops with HTTP 409 `existing_install_conflict` (structured with the
resolved `paths`, which files `exist`, and `requires_confirmation`). The wizard
then shows a clear warning and the user must confirm replacement; a fresh install
(neither file present) and an existing Admin-prepared install continue without
extra prompts. Auto-prepare never confirms on the user's behalf.

Once confirmed (or on a fresh/Admin-owned install), it copies an existing
`config/config.json` or `docker-compose.yml` to
`${EMS_INSTALL_DIR}/data/admin/backups/` before writing atomically, reports the
backup paths on the prepare job, and never deletes `data/` or its runtime
databases — so the standard layout is never silently replaced. Admin-owned state
(the prepared marker) stays under `data/admin/state/`; `data/admin/` holds only
Admin state, backups, staging, and logs. Step 05 is the only wizard step that runs
`docker compose up -d`; it checks the prepared marker, config hash, and mount
writability first, then reports Compose service state and dashboard
reachability. If `config/` or `data/` later have incorrect ownership, the wizard
can repair only those directories and retry the preflight. It does not remove
runtime data or Docker volumes.

The repair is normally automatic. As a Linux-only manual fallback from the
install root:

```bash
sudo chown -R "$(id -u):$(id -g)" config data
```

### Install-context detection inside the container

The config-preview wizard reuses the EMS path resolver (`ems.paths`) through
`admin/install_context.py` to detect an existing EMS installation and use its
`config.json` as the preview base. The minimal `ems/__init__.py` and
`ems/paths.py` are copied into the Admin image so this import chain works; the
Admin image deliberately does **not** carry the full EMS core.

Inside the container `ems.paths.BASE_DIR` resolves from `/app/ems` to `/app`,
which holds only Admin/orchestration code — never the user's EMS install. To
point Admin at the real installation, the launcher exports `EMS_INSTALL_DIR` as
the absolute path of the EMS project root and the compose files mount it at that
same path; `detect_install_context()` then resolves
`${EMS_INSTALL_DIR}/config/config.json` (legacy `${EMS_INSTALL_DIR}/config.json`
still works), `.../data/`, and `.../docker-compose.yml` from there. Because the
install root is mounted at the same absolute path inside the container, that path
is also host-valid — which matters when Admin forwards bind mounts to the host
Docker daemon for sibling EMS containers. When `EMS_INSTALL_DIR` is unset (e.g.
direct Compose use without the launcher), detection falls back to the release
template as the preview base.

The install root mount points at the standard `./config`, `./data`,
`./docker-compose.yml` layout, which is also the live deployment target. Admin
never treats a private `data/admin/deployment/` directory as the runtime EMS
layout; `data/admin/` holds only Admin-owned state, staging, release cache,
backups, and logs.

### Published-image runtime (no source checkout)

`deploy/admin/start-admin-setup.sh` and `docker-compose.yml` build the image
locally from the checkout — the Developer Setup / build-from-source path. Normal
users instead run `install-admin-console.sh`, which needs no checkout: it writes a
self-contained `docker-compose.admin.yml` (published
`ghcr.io/basecubedev/ems-solarflow-admin` image, resolved host paths baked in),
creates `config/` and `data/admin/`, and starts the Admin Console.

The same runtime is also available as fixed repository Compose files that use the
published image with no `build:` section:

- `deploy/admin/docker-compose.runtime.yml` — deployment-capable, **host
  networking** (Docker socket, no port mapping). The end-user default.
- `deploy/admin/docker-compose.runtime.bridge.yml` — deployment-capable, bridge
  networking. Publishes the UI on `${EMS_ADMIN_BIND:-127.0.0.1}:${EMS_ADMIN_PORT:-8090}`.
- `deploy/admin/docker-compose.runtime.discovery-only.yml` — restricted, no
  socket, host networking.

All keep same-path mounting for `EMS_INSTALL_DIR` and `EMS_ADMIN_DATA_DIR`. Host
networking is the default so discovery sees the real LAN; the bridge file is the
opt-in for Docker bridge networking with a published port, matching
`install-admin-console.sh --bridge`. Set `EMS_ADMIN_TAG` to pin an image tag
(default `latest`).

### Restricted discovery-only mode

To run discovery and build a config draft **without** granting any Docker
access, start the restricted mode. No Docker socket is mounted, so Step 04
reports that the Admin container was started in restricted mode and the wizard
stays read-only through config generation:

```bash
deploy/admin/start-admin-setup.sh --discovery-only
# or directly:
docker compose -f deploy/admin/docker-compose.discovery-only.yml up --build
```

### Troubleshooting: DOCKER_GID

`start-admin-setup.sh` reads the socket's owning group with
`stat -c '%g' /var/run/docker.sock` (falling back to `getent group docker`) and
exports `DOCKER_GID` for Compose, so you normally do not set it yourself. If the
container user cannot access the socket, Step 04 shows a permission problem;
start Compose directly with an explicit id as a fallback:

```bash
DOCKER_GID="$(getent group docker | cut -d: -f3)" \
  docker compose -f deploy/admin/docker-compose.yml up --build
```

## Raspberry Pi resource note

The container is designed for small Raspberry Pi deployments:

- Pi 4 (2 GB): supported. Pi 4 (4 GB): comfortable. Pi 3: best-effort.
- No permanent discovery daemon — a scan runs only on manual request and frees
  resources when it finishes.
- Idle footprint targets well under 100 MB RSS; a scan raises usage only while
  it runs, with bounded concurrency and short request timeouts.

Scanning the host's real LAN from a Pi works out of the box with the installer's
host-networking default. On the source/developer path, add the
`docker-compose.hostnet.yml` override (see [Docker networking](#docker-networking)).

## Security note

Device discovery scans the local network, so guardrails are enforced:

- CIDR is validated with `ipaddress`; only private, link-local, or loopback
  ranges are allowed. Public ranges are rejected (no unsafe override exists).
- Ranges broader than `/24` are rejected.
- Timeout and worker count are clamped to safe bounds.
- No user input is ever passed to a shell.
- The server sends the same security headers as the dashboard and blocks static
  path traversal; dynamic values are escaped before insertion into the DOM.
- Every non-auth API requires a valid Admin session; mutating requests also
  require the session CSRF token. The Admin session uses its own
  `ems_admin_session` cookie and the shared `config/dashboard-auth.json`
  password (see [admin-architecture.md](admin-architecture.md#authentication)).

The preview binds to `127.0.0.1` by default. If you publish it on `0.0.0.0` or
use host networking, treat it as **trusted local network only** — never expose
it to the internet.
