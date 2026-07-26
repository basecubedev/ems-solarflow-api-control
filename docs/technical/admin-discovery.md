# EMS SolarFlow Admin Console Technical Reference

This is the technical reference for the Admin Console. For a short user overview, see
[Admin Console](../user/admin-console.md); for the two guided flows, see
[Admin setup](../user/admin-setup.md) (Set up a new system) and
[Admin maintenance](../user/admin-maintenance.md) (Manage my existing
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
> [Admin setup](../user/admin-setup.md)); it runs the published image with
> no Git checkout. This page documents the technical/source and runtime behavior,
> including the local-build launcher `deploy/admin/start-admin-setup.sh`.

## Layout

The UI has two top-level tabs. **Setup** (the default) is a compact
step-by-step wizard with a stepper header and **five steps** — **01 Release**,
**02 Devices**, **03 Config**, **04 Prepare deployment**, **05 Start EMS**. Only
the active step shows its full content; the others collapse to a compact status
in the stepper (e.g. `Ready`, `3 devices`, `Draft ready`). **Devices** and
**Config** stay locked until the Release step reports its resources ready, and
`Next` is disabled until then.

### Release step: one paired System Build

The Release step selects **one System Build** — a matched Admin + EMS image pair
identified by matching revision, Build ID and channel (see
[System Build pairing](system-build-pairing.md) for the full identity model). In
the UI the catalogue is grouped **Latest**, **Stable**, **Unstable** and
**Experimental** (the `latest`, `stable`, `rc` and `development` channels
respectively, always in that order). Selecting an **Experimental** build is
itself the explicit decision — there is no separate acknowledgement checkbox.

For a current (paired) build the setup resources are **embedded** in the running
Admin image. They are verified against the build's manifest
(`resources_verified`) and imported into `data/admin/releases/<tag>/`
(`$EMS_ADMIN_DATA_DIR/releases/<tag>/`) **before any config** is written; Admin
never substitutes `main`-branch resources for a pinned build. If the running
Admin does not match the selected build it is aligned first
(**Update Admin Server**); a matching Admin continues straight to discovery. The
selection pointer is stored under
`$EMS_ADMIN_DATA_DIR/state/selected-release.json` and survives an Admin restart.

#### Legacy release fallback (older releases only)

For **older, pre-embedded releases** (the legacy-release compatibility class),
Admin instead reads public release metadata from GitHub and downloads the
selected release **source archive**, extracting only the whitelisted setup
resources into `data/admin/releases/<tag>/`: `config.template.json`,
`docker-compose.example.yml`, both `install-docker` scripts, and
`deploy/docker/*`. This is a **compatibility path for older releases**, not the
normal current System Build flow, and it never touches the embedded bundle. A
manifest records the concrete tag and cached paths. If GitHub is unavailable,
already cached releases remain selectable.

Admin Setup is a Docker-only path. Releases from `v0.6.0` onward are supported
when their resources can be verified — embedded for current builds, or (for a
legacy release) the config template, Linux and Windows Docker installers, Compose
example, and `deploy/docker` resources fetched from GitHub. Older releases remain
visible but disabled. Unstable builds (release candidates) newer than the support
floor stay selectable in the **Unstable** group with a not-stable warning.

The synthetic `latest` option maps the rolling Docker channel to setup resources
from the repository's `main` branch. It is selectable when those resources can be
verified, but it is never treated as stable and never replaces the newest
supported stable release as the default. If stable resources cannot be found,
`latest` is the fallback. Resource availability is checked again during
preparation before the strict extraction whitelist is applied.

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

Selected Zendure MQTT proposals are not trusted by content: the browser submits
a stable proposal `id` (and its `broker_ref`) that the backend resolves back to
the full proposal held in current discovery state. Serial, device id, broker
identity, topic family, capabilities and connection metadata (TLS mode,
`tls_insecure`, non-secret `credentials_ref`) all come from that stored
proposal; the browser may only add its selection (`replace_grid_meter`). An
unknown, stale or forged proposal `id`, or a submitted field that conflicts with
the stored proposal, is rejected before any config is generated. Each locally
discovered broker gets a deterministic, endpoint-derived `local_mqtt_<slug>_<hash>`
`broker_ref` that stays stable whether the broker is discovered alone or
alongside others; two brokers that share a broker id but differ in host/port/TLS
are never merged onto one profile.

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
disabled. Downgrades remain intentionally unsupported. A moving `latest` image
tag is not treated as a concrete installed version.

Discovery is deferred until the Devices step is first opened and then runs once
per session (mDNS keeps polling on its own). The global discovery context —
**Detected networks / gateways** (compact) and **Discovery progress** — sits in
the main discovery area, above the unified **Detected devices** list. Source
detail sections (Local API results, manual CIDR scan, mDNS controls, ignored
devices, Local MQTT, Zendure MQTT, config preview) live in collapsed details
under **Details** so the page stays short. **Diagnostics / Advanced** exposes the
Deployment and System panels; only in-UI Network / WiFi editing remains a
placeholder for a later phase.

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
  probes on ports 1883 and 8883 whenever the user starts a network scan, and on
  refresh lists read-only Zendure hardware candidates seen per reachable broker
  (see [Local MQTT discovery](#local-mqtt-discovery)).
- Optionally discovers real hardware through the encrypted Zendure **cloud** MQTT
  broker using a saved Zendure API key or HA/deviceList token — the discovery
  pass is read-only and TLS-only, and the discovered devices join the same
  trusted proposal set as local candidates, so Setup and Maintenance can apply them (see
  [Zendure MQTT discovery (cloud)](#zendure-mqtt-discovery-cloud)).
- Lets the operator enable/disable each discovery source and set the source
  **priority**, then presents one **unified detected-devices** list that
  deduplicates a device found through multiple sources (see
  [Discovery preparation and unified results](#discovery-preparation-and-unified-results)).
- Scans a manually entered private CIDR range (e.g. `192.168.178.0/24`).
- Probes known local HTTP device APIs and classifies responses:
  - Zendure local HTTP device — `GET /properties/report` → role `inverter`
  - Shelly Pro / Gen2 meter — `GET /rpc/Shelly.GetStatus` → role `grid_meter`
  - Shelly 3EM Gen1 meter — `GET /status` → role `grid_meter`
  - everHome EcoTracker — `GET /v1/json` → role `grid_meter`
- Shows each device with IP, API family/type, suggested role, serial (or
  `missing`), confidence, and config readiness.

## Discovery preparation and unified results

The Devices step opens with a **Discovery preparation** panel, then the global
**Detected networks / gateways** and **Discovery progress** sections, then the
unified **Detected devices** panel, all above the source-specific detail sections
grouped under **Details**.

Preparation is setup orchestration — it never creates an EMS runtime fallback
and never writes the safety-critical `config/config.json`. Its **non-secret**
state (priority, per-source enable flags, local API scan ranges/manual hosts, and
the endpoint-independent local MQTT discovery credential pool references)
persists in the EMS config area, `config/discovery-connections.json`
(`admin/discovery_connections.py`), so a later EMS runtime can read it. A legacy
Admin-local `<admin-data>/state/discovery-preparation.json` is migrated in on
first read.

```json
{
  "priority": ["local_api", "local_mqtt", "zendure_mqtt"],
  "local_api": {"enabled": true, "scan_ranges": [], "manual_hosts": []},
  "local_mqtt": {
    "enabled": true,
    "credential_refs": ["home-assistant", "mosquitto"]
  },
  "zendure_mqtt": {"enabled": true, "token_ref": "zendure-cloud"}
}
```

Discovery holds no broker-specific connection config (host/port/TLS/user): it
scans for endpoints and tries anonymous plus every pooled credential against
them. Broker-specific connection config, if ever needed, belongs to the later
Config step. A legacy `local_mqtt.brokers` list is still tolerated on read for
backward compatibility, but the Discovery flow neither creates nor manages it.

**Secrets are stored separately**, encrypted at rest, under `config/secrets/`
(`admin/credential_store.py`): the Zendure Cloud token
(`zendure-cloud.json`), each discovery credential's username/password in its own
namespace (`mqtt-discovery-<id>.json`, separate from any legacy per-broker
`mqtt-<id>.json`), and the Fernet key (`.secret-key`), all `0600`
best-effort. `config/discovery-connections.json` holds only references
(`credential_refs`, `token_ref`), never a raw token or password.

- **Priority** is edited with up/down buttons and is always stored as a full
  permutation of the three sources (unknown/duplicate entries are dropped and
  missing sources appended in default order). The default order is `local_api`,
  `local_mqtt`, `zendure_mqtt`.
- **Enabled** flags gate whether a source contributes to the unified list.
  Disabled sources stay listed (visually marked) and keep their own detail panel.
- Each source row has a **Configure** action that opens the matching detail
  panel (Local API results, Local MQTT, or Zendure MQTT) where credentials live.
  Credentials/tokens themselves stay in their own stores and are never mirrored
  into the preparation file.

The **unified list** (`admin/discovery_unify.py`) groups per-source candidates by
identity and picks a source strictly by the configured priority:

- Strong identity is `serial_number` (case-insensitive); a device found via
  several sources collapses into one card with `id: "serial:<serial>"`,
  `confidence: "high"`, and a `Selected by priority` label. The Config step
  reconciles the same per-serial identity: the priority-selected transport
  becomes the configured one (a manual transport choice overrides priority and
  survives later rescans), so exactly one transport is written per serial.
- When no serial is known, a candidate keeps a per-source weak identity and is
  **never** merged with another weak candidate (`confidence: "low"`).
- Every original candidate is preserved: the unified card lists all contributing
  sources, and the source detail panels still show every broker-specific
  candidate (the same serial seen on two local brokers keeps both broker rows).

API:

- `GET /api/discovery/preparation` — current priority + enabled flags.
- `POST /api/discovery/preparation` — save priority/enabled (normalized; returns
  the stored payload). No secrets are accepted or echoed.
- `POST /api/discovery/run` — aggregate the already-collected per-source state
  into `{priority, sources, devices, details}` selected by priority. Without a
  body (or with `{"refresh": false}`) it is read-only: it never starts a new
  network scan. With `{"refresh": true}` (the fresh-install **Run discovery**
  action, orchestrated by `admin/discovery_run.py`) every *enabled* source is
  refreshed exactly once first — concurrently, and one failing source only adds
  a redaction-safe warning instead of discarding the other sources' results —
  and the payload additionally carries `refresh` (`status`:
  `ok`/`partial`/`failed` plus per-source `{ok, error, message}`) and
  `warnings`. Selection priority is applied only after every refresh has
  finished, so completion order can never override it. Neither variant ever
  writes `config.json`.
- `POST /api/discovery/source/<source>/refresh` — re-run one source's collector
  (`local_api` → mDNS refresh, `local_mqtt` → broker refresh, `zendure_mqtt` →
  cloud refresh). `<source>` is one of `local_api`, `local_mqtt`, `zendure_mqtt`.
- `GET /api/discovery/connections` — redaction-safe connections state: priority,
  enable flags, local API ranges, the local MQTT discovery credential pool (each
  with `username_configured`/`password_configured`/`credentials_encrypted`), and
  Zendure `token_saved`. Never a raw token or password.
- `POST /api/discovery/connections/local-api` — save scan ranges / manual hosts.
- `GET /api/discovery/connections/mqtt-credentials` — list the redacted discovery
  credential pool.
- `POST /api/discovery/connections/mqtt-credentials` — add/update a pooled
  discovery credential (`{id?, label, username, password}`); the `username`/
  `password` is stored in `config/secrets/` and only its `credential_ref` is
  persisted. `label` is required (or derived from `id`).
- `DELETE /api/discovery/connections/mqtt-credentials/<id>` — remove one credential
  ref and forget its stored secret; other credentials are untouched.
- `POST /api/discovery/connections/mqtt-brokers` /
  `DELETE /api/discovery/connections/mqtt-brokers/<id>` — legacy per-broker
  connection entries, retained for backward compatibility only; the Discovery UI
  no longer uses them.

## Maintenance discovery sources

The Maintenance editor's **Add more devices** row reuses the same discovery
services with source parity but without the preparation UI: **Start
discovery** refreshes mDNS, scans recommended networks, re-listens on known
local MQTT brokers (`POST /api/discovery/mqtt-brokers/refresh`, anonymous plus
the pooled credentials) and — only when `GET
/api/discovery/zendure-cloud-mqtt/settings` reports `token_saved` — refreshes
the cloud source (`POST /api/discovery/zendure-cloud-mqtt/refresh`), then
reads the combined `GET /api/discovery/mqtt-proposals`. There is no
priority/enable editing and no per-source detail list in Maintenance; all
results flow into the one review card list. Each source is its own progress
work unit, and a failing source only marks its unit failed — the draft and the
other sources' results are untouched.

The review is transport-aware around one physical identity (the physical
serial; the MQTT routing id only when no serial exists — shared resolver
`zendure_physical_identity` / `physicalInverterIdentity`, contract-tested for
backend/browser equivalence). A discovered serial that is already configured
over another transport renders as an **Alternative transport** row with a
**Use … instead** action that switches the configured device's connection in
place — name, enabled state, and common tuning values preserved, stale
transport fields removed — never as a second **Add as inverter** result. The
same serial can never enter the draft twice across transports; the backend
merge additionally enforces duplicate-identity and identity-conflict
validation, so a buggy client cannot apply a duplicate.

The **Discovery sources** rows under the discovery actions expose the setup
flow's source-config blocks (local MQTT credential pool, Zendure credential) by
**moving the parked DOM nodes** (`#inline-config-parking`,
`data-inline-config`) into maintenance slots
(`data-maintenance-source-slot`) — the same nodes with their bound handlers,
never copies. The setup re-render's `parkInlineConfigs()` skips a node mounted
in Maintenance; closing a row or switching the admin view parks it back. mDNS
has no maintenance row: it refreshes automatically with every run, and
enabling/disabling it stays a setup decision.

The shared handlers select their request contract from the node's current
owner before sending anything: mounted in a Maintenance slot they call the
generic `/api/discovery/...` routes (Admin session + CSRF only), mounted in
Guided Setup they go through the operation-gated `/api/setup/discovery/...`
aliases with the confirmed `X-Setup-Operation-ID`. Maintenance credential
actions therefore never depend on Guided Setup transition state — they keep
working for manually installed systems and after Setup state files were
cleaned up or removed during recovery — while Setup keeps its
confirmed-operation gate for every alias, connectivity probes included (broker
probe and the Zendure credential test persist discovery-store state, so they
are not exempt).

## Current limitations

- The Admin Console requires a password, but is still LAN-only by design. Run it
  only on a trusted local network (see [Security note](#security-note)).
- InfluxDB restore in Admin is limited to **bundled** InfluxDB and is
  orchestrated through the EMS CLI restore flow (Admin never implements a
  separate InfluxDB restore engine). External InfluxDB is not covered (see
  [admin-backup-restore.md](../user/admin-backup-restore.md)).
- No SSDP/ARP discovery and no `ping`/`nmap`/`arp` shell-outs.
- MQTT discovery is read-only. Local brokers use a brief anonymous,
  subscribe-only topic listen; the Zendure cloud broker uses the saved Zendure
  API token over TLS. Neither publishes, issues `properties/read|write`, nor promotes MQTT
  devices into `config.json`.

Config generation/apply, guided upgrade, deployment, and a full preview-first
backup/restore flow are implemented (see the setup and maintenance guides). The
discovery result model is shaped so a device can be promoted (config name,
display name, role, per-device parameters) without a schema change.

New inverter promotions use the compact operational name `INV_n`. One allocator
covers Local API, local MQTT, Zendure cloud MQTT, and manual entries, while the
descriptive display/model name and physical serial or device ID remain separate
metadata. Existing configured names are never rewritten automatically, and a
transport change carries the current operational name to the replacement entry.

## Devices-step scan actions

The Devices step has two single-purpose toggle buttons, each of which starts a
run and — while that run is active — becomes its own **Cancel** control:

- **Run discovery** first finishes the network detection + LAN device scan in
  the browser, then triggers the backend-orchestrated run
  (`POST /api/discovery/run` with `{"refresh": true}`), which refreshes every
  enabled source (mDNS, Local MQTT, Zendure cloud MQTT) exactly once and
  returns the unified result; the UI never re-implements that fan-out. It is
  triggered automatically the first time the Devices step is opened. After a
  completed run it reads **Run discovery again**. A passive `N sources enabled`
  status shows how many sources the button will scan.
- **Scan networks** re-runs only the network detection and LAN device scan
  (clearing the per-session scan memory so already-scanned networks are scanned
  again).

Cancelling stops the in-flight scan but keeps devices already found (it bumps the
scan session generation so queued/running scans abandon their results; it is not
a results reset). The device scan starts as soon as the first network is found
and stays active until the gateway probe has finished adding networks and every
LAN network has been scanned, so a slow gateway probe never cuts the run short.

## Network suggestions

The page lists detected local networks (`GET /api/discovery/networks`) so you can
start a scan without knowing your CIDR. Each entry shows the interface, address,
a **Recommended** or **Advanced** badge, and a `Scan this network` button.
Manual CIDR entry always remains available.

Detection is Linux stdlib only (`socket.if_nameindex` + `fcntl` ioctls +
`/proc/net/route`) — no shell-out, no packet capture, and it runs only when the
API is requested (opening the Devices step, **Run discovery**, or **Scan
networks**). It never starts a scan on its own.
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
`/24` of any responder to the same network list. It re-runs whenever network
detection runs (**Run discovery** / **Scan networks**).

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

## Local MQTT discovery

MQTT brokers are shown separately from EMS devices. The mDNS listener resolves
`_mqtt._tcp.local.` service name, hostname, address, port, and TXT data. The
normal network scan automatically checks the same selected or manually entered
validated network, using short bounded TCP connects to ports 1883 and 8883.
There is no separate MQTT network selector or probe button. An open port is only
a conservative broker candidate; it is not treated as config-ready.

Each broker candidate can additionally show read-only **hardware candidates**
grouped under it. On refresh, Admin infers the endpoint's transport (`1883` →
plain, `8883` → TLS; an explicit `tls` flag from mDNS/details overrides this) and
runs an attempt matrix against each reachable broker: **anonymous first, then every
saved discovery credential**. Each attempt subscribes briefly (`Zendure/#`,
`iot/+/+/#`, `/+/+/#`) with a bounded timeout and classifies the topics it sees
into Zendure topic families (`zensdk_ha_scalar`, `legacy_zendure_json`,
`legacy_zendure_json_write_observed`, `legacy_zendure_json_alt`). A topic
family names the observed telemetry schema (which parser reads the payload),
never the hardware generation: a new ZenSDK device (e.g. a SolarFlow 800 Pro 2
on the Zendure cloud broker) publishes the leading-slash JSON report that is
classified `legacy_zendure_json_alt`, so the user-facing hardware generation is
resolved from the product model where known (neutral schema aliases:
`zendure_json_report` / `zendure_json_report_leading_slash`; the stored
`legacy_*` config values remain valid). Device id,
serial, model hint, metrics, and sample topics are extracted from topic paths and
— only where present and valid — JSON report payloads. Anonymous is always tried;
a failed credential never suppresses the next one; one broker's failure never
stops the others. Each candidate carries redacted `attempts` (stable
`mqtt-probe:<host>:<port>:<transport>:anonymous|credential:<id>` identity, label,
status, device count) so the UI can explain what worked, and device candidates are
de-duplicated across attempts. The discovery pass itself is read-only:

- It never publishes and never writes `acMode`/`inputLimit`/`outputLimit`.
- Passwords are never shown or logged.
- The same serial/device id seen on two brokers stays as two separate
  candidates (the candidate id embeds the broker id); candidates are never
  merged across brokers.
- Collected topics and candidates per broker are bounded; malformed
  topics/payloads are ignored rather than fatal.
- Nothing is promoted into `config.json`. The running EMS still uses exactly one
  configured connection method per device, decided later in the Config step.

Topic discovery requires `paho-mqtt` in the Admin image; when it is unavailable
discovery degrades to broker-endpoint-only candidates.

### D0 grid-meter mapping from local MQTT

A hardware candidate whose only grid-power evidence is `totalPower` is classified
as a **grid-meter candidate** (`role_hint = grid_meter_candidate`, `read_power`
true, `write_output_limit` always false). When such a candidate is observed on a
**local** broker (`source_type = local_mqtt`) under the `zensdk_ha_scalar` family
and carries an exact observed `Zendure/sensor/<serial>/totalPower` topic, its
proposal gets `target = grid_meter` and a read-only `grid_meter_fragment`
(`type: zendure_smartmeter_d0`, `payload_format: number`, referencing the local
broker profile via `broker_ref`). The Admin then offers **"Use as grid meter"**
instead of adding a telemetry device, writes the result to the central
`grid_meter` block (never to `devices[]`), keeps exactly one grid meter active,
and never silently replaces an already-selected grid meter.

Weak or unsafe evidence never becomes an auto-applicable D0 grid meter: a bare
`totalPower` metric without an exact safe local topic keeps only a role hint plus
a `grid_power_metric_seen_but_topic_unavailable` warning; the `number/…` write
channel, extra path segments, foreign/custom prefixes, and cloud topics (whose
prefix is the secret account app key) are all rejected. Cloud MQTT D0
auto-mapping is **not supported** in this release. Local HTTP remains the
recommended Zendure grid-meter path (see
[configuration](configuration.md)); MQTT is an optional alternative.

Credentials are a reusable **discovery credential pool**, not per-broker
connection config: the Local MQTT inline config exposes only a compact
label/username/password form and the saved-credential list (**Optional discovery
credentials**). There is no broker host/port/TLS form in Discovery — endpoints are
found automatically and adding devices happens later in the Config step. Each
saved credential's username/password is stored encrypted in `config/secrets`
(`mqtt-discovery-<id>.json`) and only its redacted status (`username_configured`,
`password_configured`, `credentials_encrypted`) is ever returned. Pooled
credentials ride only on the transient per-attempt broker copy handed to topic
discovery; stored/returned candidates never carry a username or password.

API: `GET /api/discovery/mqtt-brokers`,
`POST /api/discovery/mqtt-brokers/refresh`, and
`POST /api/discovery/mqtt-brokers/probe` with `{"cidr": "192.168.178.0/24"}`.
Broker candidates carry `transport`, `auth_mode`, and `mqtt_connect_status`, and
the probe response lists `tested_combinations` (per port/transport: hosts checked
and open endpoints). Broker candidates also carry an optional `devices` array of
hardware candidates. Refresh restarts mDNS browsing, rechecks known broker
endpoints, and runs the read-only topic discovery for reachable brokers. Probes
do TCP-level checks only; they do not authenticate or create permanent
connections.

### Connection-profile identity and idempotent apply

A broker **connection profile** is identified by its secret-free
`(source, host, port, tls, tls_insecure, credentials_ref)` tuple
(`normalized_broker_identity`). This single rule governs broker equality across
Admin proposal building, config preview and Core:

- Two selections on the **same** endpoint with the **same** `credentials_ref`
  (for example a D0 grid meter and a control device discovered together on one
  authenticated broker) resolve to **one** shared broker profile — never two.
- Two selections on the same endpoint with **different** `credentials_ref` (two
  accounts on one host) stay **distinct** profiles and, at runtime, distinct
  MQTT services with isolated reads and writes.

Setup apply is **idempotent**: because apply uses the existing installed config
as its preview base, re-selecting a Zendure MQTT device already present in the
config is a no-op rather than a duplicate-identity error. A genuine conflict
still blocks — an existing HTTP device that shares a serial (a different
transport for one physical device), or two distinct proposals for the same
device id in a single apply.

The full setup-to-runtime lifecycle (discovery → trusted proposal → preview →
apply → `config.json` → Core credential resolution → runtime → telemetry →
controller → transport-specific publish → cleanup) is guarded by the **MQTT
release contract** test suite (`tests/test_mqtt_release_contract_*.py`, harness
in `tests/helpers/mqtt_release_contract.py`, map in
`tests/helpers/MQTT_RELEASE_CONTRACT.md`). Its fast tier uses fake MQTT/HTTP
transports; the `-m docker` tier proves the same boundaries against a real local
`eclipse-mosquitto:2` (auth, ACL isolation, TLS). Real **Zendure hardware**
control is not part of this validation; per-generation physical-hardware
validation status is tracked in
[supported-setups.md](../user/supported-setups.md).

## Zendure MQTT discovery (cloud)

A separate panel below Local MQTT discovery discovers real hardware through the
encrypted Zendure cloud MQTT broker. The discovery pass itself is read-only:
it writes no config, touches no EMS runtime, and never publishes. Discovered
cloud devices join the same trusted proposal set as local candidates, so
selecting one in Setup or Maintenance generates a normal config proposal;
the apply then provisions the cloud runtime credential record automatically
(see the configuration guide).

Credential: the panel takes exactly one value — either a raw **Zendure API
key** or Zendure's base64 **HA/deviceList token** — with no credential-type
selector. The backend auto-detects the shape. A raw key uses the fixed EU cloud
base (`https://app.zendure.tech/eu`); an HA token is decoded locally into its
Zendure API base plus `appKey`. Token-provided URLs are restricted to Zendure's
known HTTPS API bases, so a crafted credential cannot turn discovery into an
arbitrary server-side request. The operator never enters a separate `api_url`
or manual broker host/user/password. Requests may optionally carry
`credential_mode`; omitted/empty, `zendure_api_key`, and
`ha_device_list_token` are accepted. Other modes receive a clear
`unsupported_credential_mode` 400. Manual MQTT broker credentials remain a
separate feature in the Local MQTT broker section.

Credential storage (`admin/credential_store.py`): the Zendure API key or HA
token is encrypted at rest (`cryptography`/Fernet) under
`config/secrets/zendure-cloud.json`, with the Fernet key in
`config/secrets/.secret-key`, both written `0600` best-effort.
`config/discovery-connections.json` holds only the `token_ref`, never a raw key.
The `admin.secret_store.ZendureTokenStore` shim may still exist internally for
compatibility, but the credential store is the current storage layer. The raw
credential is never returned to the browser and never logged; only redacted
metadata (last check time, status, device count, broker host/port, TLS mode) is exposed.
`GET settings` reports key status without the key.

Cloud flow (`admin/zendure_cloud_auth.py`, `admin/zendure_cloud_mqtt.py`):

- Resolve a raw API key to the fixed EU API base, or decode the Zendure HA token
  into its allow-listed Zendure `api_url` and `app_key`.
- Sign and POST the Zendure deviceList request to fetch the real device list plus
  the MQTT connection info; that returned host, port, username, password, and
  clientId are the source of truth for the broker. The fixed endpoint path,
  client id, and signing key are Zendure's own server-side contract (it rejects
  other client ids with `code 1002`). The request headers must use a
  **seconds**-epoch `timestamp` and a **5-digit** integer `nonce` — Zendure
  rejects a milliseconds timestamp (`code 1004`) or any other nonce length/format
  (`code 1007`).
- Seed one candidate per device (`discovery_status = device_list_only`,
  medium confidence) using `productModel`/`snNumber`/`deviceName`.
- Connect to the returned broker with **TLS** (paho `tls_set()` before connect),
  MQTT 3.1, bounded per-device subscriptions (`/<pk>/<dev>/#`,
  `iot/<pk>/<dev>/#`, `<appKey>/#`), and enrich matching candidates with
  observed topics/metrics (`discovery_status = mqtt_observed`).

TLS rules: cloud MQTT is encrypted-only and never falls back to plaintext. The
deviceList usually returns the broker without a port; because 1883 is the
*plaintext* port, the effective TLS connection resolves to **8883** when the API
omits a port **or** reports the plaintext `1883` (a TLS handshake against 1883
always fails, so it is upgraded rather than attempted). An explicit non-plaintext
port supplied by the API is honoured as-is. The Zendure cloud broker presents a **self-signed**
certificate on 8883, so cloud discovery defaults to `encrypted_no_verify`
(encrypted but certificate not verified — never labelled "secure" in the UI).
`system_ca` (verifies chain + hostname) and `pinned_ca` (with a configured CA
file) remain available. A TLS/connect failure is surfaced as an actionable
sub-status and never crashes Admin: the deviceList candidates are still returned,
and because the device list itself succeeded the persisted `last_status` stays
`ok` (only a deviceList failure is a hard error).

Redaction: appKey, productKey, and deviceKey are never sent to the browser raw —
candidate ids prefer the serial, keys are masked, and observed topics have the
account-scoped segments redacted. Serial numbers may be shown in the local Admin
browser but are never written to server error text.

API (all require Admin auth):

- `GET /api/discovery/zendure-cloud-mqtt/settings` — redacted credential status.
- `POST /api/discovery/zendure-cloud-mqtt/token` — save/replace the API key or
  HA/deviceList token.
  Field `api_key` (or `token` as a backwards-compatible alias, but never both);
  explicit modes `zendure_api_key` and `ha_device_list_token` are accepted.
- `DELETE /api/discovery/zendure-cloud-mqtt/token` — forget the credential +
  cache.
- `POST /api/discovery/zendure-cloud-mqtt/test` — deviceList only (no MQTT
  connection); returns the device count and broker. Uses the supplied `api_key`
  / token or the saved one.
- `POST /api/discovery/zendure-cloud-mqtt/refresh` — full cloud discovery
  (deviceList + read-only TLS MQTT listen), returns cloud candidates.

The deviceList request uses a generous ~25s timeout (clamped at ~30s) because the
live Zendure endpoint can take ~14-15s to respond; a slow response surfaces a
distinct, redaction-safe timeout message rather than a generic failure.

Cloud discovery needs neither the EMS nor InfluxDB and works during a fresh
install when only the Admin container exists.

### Cloud candidates as config proposals

Cloud candidates are part of the one **trusted proposal set**
(`proposals_from_sources()` in `admin/zendure_mqtt_config_proposals.py`):
`GET /api/discovery/mqtt-proposals` returns local-broker and cloud proposals
together, and the config-preview trust resolve validates submitted selections
against exactly the same combined set. Cloud proposals carry
`broker_ref: "zendure_cloud"`; selecting one provisions the well-known
`zendure_cloud` broker profile in the preview (TLS, secret-free
`credentials_ref`) and requires the saved Zendure account credential
(fail-closed otherwise). Rules:

- A device already proposed on a discovered **local** broker is not offered a
  second time via the cloud (the local connection wins); a cloud-only device
  keeps its cloud proposal.
- deviceList-only candidates (no observed MQTT telemetry yet) become proposals
  flagged `waiting_for_mqtt_telemetry`; masked-only identifiers (`…`/`••••`)
  never become proposals.
- The Admin-only cloud TLS modes (`encrypted_no_verify`, `pinned_ca`) are
  translated to the canonical `insecure_no_verify` before proposal endpoints
  are recorded, so no Admin-only mode string reaches config preview.
- Local proposal ids keep their `:g<generation>` stamp from the broker store;
  cloud proposals take no part in that generation/TTL bookkeeping.

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
