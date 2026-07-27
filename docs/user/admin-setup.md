# Admin Console: Set up a new system

Best for most users who want a browser-guided setup with device discovery and
later maintenance. The Admin Console is orchestration/UI only; the EMS core stays
the source of truth. It is a Docker-only path.

Use this for a fresh install or a deliberate reinstall. To update or change an
existing system, use [admin-maintenance.md](admin-maintenance.md) instead. Setup
writes only `config/config.json` (backing up any existing config first); it does
not touch `data/` or runtime databases.

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

The default uses **host networking** for reliable LAN device discovery. EMS
SolarFlow is a local LAN system, so host networking lets discovery see the LAN
like a local host process; the UI is also reachable from another device on your
LAN at `http://<host-ip>:8090`. This is normal local-appliance behaviour.

If you need Docker bridge networking instead — for example in a restricted
environment where host networking is not available — use `--bridge`:

```bash
sh install-admin-console.sh --bridge
```

Bridge mode publishes the UI on `127.0.0.1:8090` and isolates the container from
the host network, so automatic LAN discovery can be less reliable; enter your LAN
CIDR manually if a scan sees only Docker networks.

A fresh install may start with only the Admin Console — no EMS container and no
`config/config.json` yet. On first start, create the shared EMS/Admin password
in the browser (the first visitor creates it), then continue Guided setup. The
password is saved to `config/dashboard-auth.json` and later reused by the EMS
Dashboard.

Run the Admin Console only on a trusted local machine or trusted LAN — never
expose it to the internet. Contributors building from a Git checkout use
`deploy/admin/start-admin-setup.sh` instead — see
[developer-setup.md](../developer/developer-setup.md).

## Layout

```text
Config:  ./config/config.json
Data:    ./data/
Admin:   ./data/admin/
Compose: ./docker-compose.yml
```

`./data/admin/` holds only Admin Console state, releases, staging, backup-set
metadata and logs. It is not a live EMS runtime layout.

## Two Admin Console flows

The Admin Console opens on a start/router screen that detects the current install
state and recommends the safest of exactly **two** flows:

- **Set up a new system** — for first-time setup or a clean reinstall.
- **Manage my existing system** — for updates, backups, diagnostics, changing
  settings, and migrating an existing config (see
  [admin-maintenance.md](admin-maintenance.md)).

Docker Bootstrap and Developer Setup are documentation-level alternatives
([docker-bootstrap.md](docker-bootstrap.md),
[developer-setup.md](../developer/developer-setup.md)); they are not selectable flows inside
the Admin Console.

The router recommends and preselects a flow but never acts silently:

- A fresh install root is recommended for **Set up a new system**.
- Any existing, legacy, partial or Admin-prepared install is recommended for
  **Manage my existing system** and does not auto-start the setup wizard.
- Choosing **Set up a new system** while an install already exists requires an
  explicit confirmation before any replace/reset behavior.
- A legacy root `config.json` routes to Maintenance, which offers to migrate it
  to `config/config.json` first (see [config-layout.md](config-layout.md)).

## Steps (Set up a new system)

The wizard runs in **five stages**, shown in the stepper: **01 Release**,
**02 Devices**, **03 Config**, **04 Prepare deployment**, **05 Start EMS**. Start
the Admin Console (see **Start** above), open `http://127.0.0.1:8090`, and pick
**Set up a new system**.

1. **01 Release** — select one paired Admin + EMS **System Build**, then verify
   it. The catalogue is grouped **Latest**, **Stable**, **Unstable** and
   **Experimental**; selecting an Experimental build is itself the explicit
   decision, with no separate acknowledgement checkbox. **Selecting a build does
   not download anything** — it only previews the build, so you can browse
   several releases without contacting the container registry. **Verify System
   Build** then downloads (or reuses) the Admin and EMS images and verifies the
   pair; the embedded Admin + EMS resources are verified **before any config** is
   written. A successful verification is reused for the rest of this setup — if
   the running Admin does not match the selected build, **Update Admin Server**
   aligns it first; otherwise **Continue** to discovery. Changing the selected
   build clears the previous verification, so you verify the new build once.
   If the download stops with a *GitHub Container Registry rate limit reached*
   message, no changes were made — wait and select **Verify System Build** again
   (see [troubleshooting](../technical/troubleshooting-reference.md)).
2. **02 Devices** — run discovery. One physical device may be reachable over
   several connections (API, MQTT, Zendure MQTT). **Discovery priority** picks
   the preferred connection automatically: raising Zendure MQTT above Local API
   and rescanning reconfigures a device that was auto-added over API to use
   MQTT instead — the same physical device is never listed twice. Discovery
   priority chooses the connection only; it never enables output control by
   itself (see step 3 and **04**).
3. **03 Config** — review and complete the generated config. The selected
   connection shows as a short pill on each device card: **API**, **MQTT** or
   **Zendure MQTT**. Every newly added inverter receives a short sequential EMS
   name such as `INV_1`, `INV_2`, and so on. This is the operational identifier
   used in `config.json`, logs, the dashboard, and the Flowchart; model,
   address, serial number, hardware generation, and connection remain separate
   details on the card. You may edit the default before applying the config.
   **Add more devices** is a connection list, not just a device list: it shows
   every discovered connection that is not the one currently selected. A
   connection for a physical inverter you have not configured yet offers **Add
   inverter**; a second connection for an inverter you already configured shows
   *Already configured as INV_1 via API* and offers **Use connection**, which
   switches that one logical inverter to the other connection immediately —
   no confirmation dialog, no duplicate device, and the EMS name, enabled state
   and all common values are preserved. The connection you left stays listed,
   so you can switch back without removing the inverter. A manual choice is
   kept even if you later change discovery priority. A
   serial-less Cloud MQTT inverter you select before its serial is known is
   recognized as the **same** inverter once discovery later reports the same
   Cloud route carrying a physical serial: it keeps your custom name and any
   dismissal, stays a single card, and is never offered as a second device to
   add. (Identity uses the trusted serial and scoped route, never a raw Cloud
   route id or display name; two different serials on one route are blocked as an
   *Identity conflict* rather than merged.)
   *Add a device
   manually* also lets you add a read-only **Zendure MQTT broker** and one or
   more **Zendure MQTT devices** telemetry discovery could not reach. Pick a
   friendly *Hardware generation* (telemetry/topic grouping), then an *Exact
   hardware model* from the EMS/Core registry. The form has two separate identity
   fields: the **Physical serial number** identifies the inverter (telemetry
   matching, duplicate detection), and the **MQTT device ID** is the exact MQTT
   route/payload id a control write targets — the physical serial is never used as
   the route id. A telemetry-only device needs only the serial. The model's write
   protocol, supported operations, and validation maturity are displayed before
   control can be enabled. Choosing **Unknown / telemetry only**, omitting the
   model, or receiving conflicting model evidence always keeps the device
   read-only; generation or topic family alone never authorizes writes. A
   supported exact model on a compatible transport exposes **Enable EMS output
   control over MQTT** — which requires the MQTT device ID — and joins the same
   control loop as a local API device — without
   hand-editing the config file (see
   [Zendure MQTT output control](../technical/configuration.md#zendure-mqtt-output-control)).

   For the **grid meter**, a Zendure D0 or Smart Meter 3CT found over local HTTP
   is the simplest choice ("Zendure Grid Meter via local HTTP", no MQTT setup).
   If instead a D0 is discovered on a local MQTT broker, the proposal offers
   **"Use as grid meter"**: choosing it maps the D0's
   `Zendure/sensor/<serial>/totalPower` topic to the central grid meter, reusing
   the selected broker profile. Only one grid meter can be active, and an
   existing grid meter is only replaced when you explicitly confirm it.
4. **04 Prepare deployment** — the Admin Console writes the generated config to
   the standard `config/config.json` (backing up any existing config first) and
   prepares the EMS deployment: the Compose file plus the target EMS image and
   resources.
5. **05 Start EMS** — start (or restart) EMS, wait for the health check, then run
   `emsctl.py diagnose` to confirm the install.

After setup, open the dashboard at `http://<host-ip>:8080`, work through the
[first-run checklist](../first-run-checklist.md), and use
[admin-maintenance.md](admin-maintenance.md) for later updates and backups.

If a legacy root `config.json` is present, the Admin Console can use it as source
data, but the applied target is always `config/config.json`.

Full detail: [../admin-discovery.md](../technical/admin-discovery.md). Layout and legacy
migration: [config-layout.md](config-layout.md).
