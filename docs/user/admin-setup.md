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
   resources. If `config/config.json` changed after you generated the config —
   because Maintenance, a restore or a migration edited it — the prepare stops
   with a conflict instead of overwriting that change. Reopen 03 Config, review
   the current config and generate it again.
5. **05 Start EMS** — start (or restart) EMS, wait for the health check, then run
   `emsctl.py diagnose` to confirm the install.

After setup, open the dashboard at `http://<host-ip>:8080`, work through the
[first-run checklist](../first-run-checklist.md), and use
[admin-maintenance.md](admin-maintenance.md) for later updates and backups.

If a legacy root `config.json` is present, the Admin Console can use it as source
data, but the applied target is always `config/config.json`.

## Restarting or discarding a setup

**Restart setup** in Guided Setup discards the whole wizard run, not just the
browser view: the Admin Console cancels the pending System Build transition and
deletes the generated config and deployment marker it created, then returns to
the first step. Your installed EMS, live `config/config.json`, runtime data,
containers, volumes and backups are left untouched. The recovery panel offers
the same action as **Discard setup** when a setup transition needs escaping; a
Guided Upgrade offers **Cancel upgrade**, which ends the upgrade only.

Discarding a setup is never a half-action. If the console is still busy with
something the setup itself started — updating the Admin Server, confirming the
System Build, preparing the release resources, verifying the System Build
resources, saving or applying the configuration, preparing the deployment,
starting EMS — the discard is **refused** and says which operation is still
running. Nothing has been discarded at that point: your draft and the setup stay
exactly as they were, and you can discard once that operation finishes. The
reverse also holds: once a discard has begun, the operation it interrupted cannot
start a System Build change afterwards.

Resource verification is worth calling out, because the progress display is
still on **Verifying selected System Build resources** while it happens: the
files are being unpacked and written at that moment, so a discard would leave a
half-written copy behind. The console therefore disables the discard until the
verification finishes or fails, and tells you to wait rather than reporting a
discard it did not perform.

### If a setup step takes too long

A System Build change has a time limit. Once it passes, the console stops trying
to continue it and offers **Discard setup** as the way out — that is what keeps a
setup interrupted by a power cut or a crashed Admin Console from blocking the
system forever.

Running out of time does not by itself mean the work has stopped, so the discard
is not offered unconditionally:

- **Time limit passed and nothing is running** — Discard setup is available. This
  is the normal recovery, including after an Admin Console restart, where any
  work from before the restart is gone by definition.
- **Time limit passed but an operation is still running** — Discard setup stays
  disabled, and the panel says the setup has run out of time *but its operation is
  still running*. Wait for it to finish; the discard becomes available immediately
  afterwards. This matters most for resource verification, which writes shared
  files: discarding mid-write would leave a half-written copy behind whether or not
  the time limit has passed.
- **The console cannot tell whether anything is running** — Discard setup stays
  disabled too, and says so. It never guesses "probably finished".

### If the Admin data directory cannot be written

Starting a System Build change writes two things: a note in the setup's own record
saying which operation it owns, and the operation itself. If the second write fails
— a full disk, a read-only mount, anything the filesystem refuses — the first one
is taken back before you are told, so **nothing was started and the setup stays
exactly as it was**. Fix the Admin data directory and start the step again; no
cleanup of your own is needed. This holds for a plain refusal and for a real
filesystem error alike.

There is one case the console cannot tidy up for you: if it also cannot read the
setup's record back to take that note away, it says so explicitly — nothing was
started, but the setup's record and the operation list no longer agree. It does not
report a clean "nothing happened", and it never rewrites or deletes a record it
could not read, because that record is the only proof of which files belong to your
setup. Check the Admin data directory, then discard the setup and start it again.

### If a System Build step fails

A failed System Build step does not throw away the setup. The recovery panel
offers:

- **Resume** — retry the step that failed, from the exact point it is safe to
  retry from;
- **Discard setup** — stop the setup and remove the files it created, as above.

A Guided Upgrade recovery additionally offers **Return to running build**, which
puts the Admin Console back on the System Build your EMS is currently running.
That action is deliberately **not** offered during a setup. Returning is really
two steps — end the failed operation, then start a new one — and during a setup
there is no record that would own the new operation afterwards, so a later
retry, discard or Admin restart could not tell who it belonged to. Resume and
Discard setup both have a clear owner and cover the same situations. If the
action is requested anyway, the server refuses it and changes nothing.

### One setup at a time, even with two browser windows

Two browser windows can be open on the same setup, but only one setup is ever
current, and only the window that is on the current setup can change it. When one
window changes the selected System Build or discards the setup, the other window's
confirmation stops being valid immediately — including its own Fresh Setup
confirmation, which belongs to the setup it was given for and is never carried
over to the replacement. The stale window says its setup session was replaced and
offers to open the current setup or discard it; it never silently acts on the
newer setup, and nothing it had prepared is applied.

If the console cannot tell whether a pending System Build change belongs to the
setup you are looking at, it refuses that change and cancels nothing. This is
deliberate: cancelling somebody else's System Build transition, or deleting files
whose owner cannot be proven, would be worse than asking you to reload and act on
the setup the server actually has.

### If temporary files remain

Stopping the setup and clearing its files are two separate things, and the console
never pretends otherwise:

- **Setup has stopped. Temporary files remain.** No new setup and no upgrade can
  start until the cleanup succeeds, and the console offers **Retry cleanup**. Your
  live `config/config.json` and the running EMS were not changed by the failed
  cleanup. The message survives a browser reload and an Admin restart, and the
  retry always applies to the same setup — so you never have to work out which
  files belong to what, and you never need to delete JSON files by hand.
- **Files were kept for review.** If the console finds a file it cannot prove
  belongs to this setup — a generated config left by an older Admin version, or a
  deployment marker written before setups were tracked — it **keeps** it and says
  so instead of deleting it. **Retry cleanup** is not offered, because retrying
  would not change the answer. Nothing was changed on your running system; a
  maintainer has to look at the leftover file. The same applies while a setup is
  running: such a file is never deployed either, and the console asks you to
  generate the configuration again.

Changing the selected System Build after a setup has started retires the
previous setup the same way: the console asks the server to supersede it, so
the earlier build's generated config and deployment plan are removed before the
new build continues. Nothing from the old build stays behind — and if the previous
setup was still busy, the build change is refused rather than cutting it off.

If you want to start a guided upgrade while a setup is still unfinished, the
console asks you to **Discard setup** first. The upgrade only begins after that
cleanup is confirmed — an unfinished setup can never deploy over an upgrade, or
the other way round. This holds for both upgrade steps: neither verifying a build
nor upgrading the system starts while setup files remain. **Cancel upgrade** in
turn keeps your running system, your live configuration and the last known-good
build exactly as they are.

## If the configuration changed while you were working

Setup checks two things before it saves or applies anything: that the draft is
exactly the one shown in the preview, and that `config/config.json` has not
changed since that preview was created.

- **Changing the draft requires a new preview.** As soon as you edit a device,
  a setting or a broker password, Apply and Continue are disabled until the
  preview has refreshed. The preview you looked at is what gets applied — a
  preview of one draft can never save a different one.
- **A changed live configuration stops the save.** If `config/config.json`
  changes in the meantime — through Maintenance, a restore, or another session
  — Apply is refused and your draft is kept. Choose **Review current
  configuration** to re-check it against the current config, then apply again.
- **Another setup session can take over.** If setup was restarted or its build
  changed elsewhere, this browser tab belongs to an older session and can no
  longer change anything. The console says so and offers **Open current setup**
  (continue with the session that is now current) or **Discard local draft**
  (drop only this tab's unsaved draft). The other session is never affected.

After upgrading the Admin Console while a setup was unfinished, a configuration
generated by the older version has to be generated once more: the console
returns you to the config preview and asks you to regenerate it. Nothing is
deleted, and your installed system is not touched.

Full detail: [../admin-discovery.md](../technical/admin-discovery.md). Layout and legacy
migration: [config-layout.md](config-layout.md).
