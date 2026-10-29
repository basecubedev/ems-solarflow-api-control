# Admin Console: Set up a new system

Best for most users who want a browser-guided setup with device discovery and
later maintenance. The Admin Console is orchestration/UI only; the EMS core stays
the source of truth. It is a Docker-only path.

Use this for a fresh install or a deliberate reinstall. To update or change an
existing system, use [admin-maintenance.md](admin-maintenance.md) instead. Setup
writes `config/config.json` (backing up any existing config first) and does not
touch the runtime databases in `data/`. It builds that file from the release
template and your choices alone: a config already in place is replaced, not
merged, and the only thing taken from it is where the shared password file
lives, so the password you signed in with keeps working. Everything else in the
config starts at the release defaults unless you choose otherwise in Setup: dry
run is off and the write gates are open, the dashboard listens on port 8080
without HTTPS, data file locations are the standard ones, and broker profiles
are only those Setup builds from what you select. What the dashboard keeps in
`data/runtime-state.json` -- system on/off, the total power limit, loop interval
and minimum output, each device's on/off and limits, winter and AC charging
on/off -- starts afresh too: Setup leaves a request beside that file, and the
EMS that starts on the new config keeps the old file as
`data/runtime-state.json.before-setup-<UTC time>` and builds a new one from the
config. A device you had switched off in the dashboard is active again (see
[runtime state](../technical/runtime-state.md#fresh-setup-reset)).

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
in the browser (the first visitor creates it), then continue with the guided
setup from the assistant. The
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

## Where the setup starts

The Admin Console never starts a setup on its own. Opened at its address, it
starts on its [assistant](admin/assistant.md), which reads the current install
state:

- **Nothing installed** — the assistant's first step, **Install EMS**, offers
  **Set up a new system** (and **Restore a backup**); the Overview offers the
  same. While nothing is installed, the console always opens on the assistant.
- **An existing, partial or Admin-prepared install** — the assistant names what
  is missing, and **Set up again** (its tile) or **Open the guided setup** on
  the **System** page reaches the setup deliberately. Updates, backups,
  diagnostics, settings and migrating an existing config live on their own
  pages (see [admin-maintenance.md](admin-maintenance.md)).
- **Settings in the old place** (a legacy root `config.json`) — the System page
  offers to move them to `config/config.json` first (see
  [config-layout.md](config-layout.md)).

Starting a setup while an install already exists asks for an explicit
confirmation — *Set up over the existing installation?* — before any replace
or reset behaviour. When another guided workflow holds the console, it asks
*End the current workflow and start setup?* first.

Docker Bootstrap and Developer Setup are documentation-level alternatives
([docker-bootstrap.md](docker-bootstrap.md),
[developer-setup.md](../developer/developer-setup.md)); they are not selectable
flows inside the Admin Console.

## Steps (Set up a new system)

The setup runs in **five steps**, shown in the stepper: **01 System Build**,
**02 Devices**, **03 Settings**, **04 Prepare**, **05 Start EMS**. Start the
Admin Console (see **Start** above), open `http://127.0.0.1:8090`, choose **Set
up a new system** on the assistant, then **Start guided setup**. The
step-by-step guide with screenshots is [Guided setup](admin/guided-setup.md).

1. **01 System Build** — choose one paired Admin + EMS **System Build**, then
   verify it. The list is grouped **Latest**, **Stable**, **Unstable** and
   **Experimental**; choosing an Experimental build is itself the explicit
   decision, with no separate acknowledgement checkbox. **Choosing a build does
   not download anything** — it only previews the build, so you can look at
   several releases without contacting the container registry. **Verify the
   System Build** then downloads (or reuses) the Admin and EMS images and
   verifies the pair; the embedded Admin + EMS resources are verified **before
   any config** is written. A successful verification is reused for the rest of
   this setup — if the running Admin does not match the chosen build, **Update
   the Admin console** aligns it first; otherwise **Continue** opens the
   devices. Changing the chosen build clears the previous verification, so you
   verify the new build once. If the download stops because the GitHub
   Container Registry limited it, no changes were made — wait and **Check
   again** (see [troubleshooting](../technical/troubleshooting-reference.md)).
2. **02 Devices** — the search runs on its own the first time. One physical
   device may be reachable over several connections (API, MQTT, Zendure MQTT).
   The **Search order** picks the preferred connection automatically: raising
   Zendure MQTT above Local API and searching again reconfigures a device that
   was auto-added over API to use MQTT instead — the same physical device is
   never listed twice. The search order chooses the connection only; it never
   enables output control by itself (see step 3).

   Priority moves a device on its own only when the new connection keeps output
   control. If the preferred connection cannot write, or its write path is not
   proven, the device is *not* switched: **03 Settings** shows the change as a
   question naming both connections and what happens to output control, with
   **Use …** and **Keep …**. Nothing changes until you answer, and answering
   again is required if discovery finds different devices in the meantime.
3. **03 Settings** — review and complete the generated settings. The selected
   connection shows as a short pill on each device card: **API**, **MQTT** or
   **Zendure MQTT**. Every newly added inverter receives a short sequential EMS
   name such as `INV_1`, `INV_2`, and so on. This is the operational identifier
   used in `config.json`, logs, the dashboard, and the Flowchart; model,
   address, serial number, hardware generation, and connection remain separate
   details on the card. You may edit the default before applying the config.
   **Add more devices** is a connection list, not just a device list: it shows
   every discovered connection that is not the one currently selected. A
   connection for a physical inverter you have not configured yet offers **Add
   the inverter**; a second connection for an inverter you already configured
   shows *Already set up as INV_1 via API* and offers **Use this connection**,
   which
   switches that one logical inverter to the other connection immediately —
   clicking it *is* the confirmation, so there is no second dialog, no duplicate
   device, and the EMS name, enabled state and all common values are preserved.
   (A switch the backend refuses outright — two different serials on one route,
   or an unresolved identity — is reported instead of performed.) The connection
   you left stays listed, so you can switch back without removing the inverter.
   A manual choice is kept even if you later change discovery priority. A
   serial-less Cloud MQTT inverter you select before its serial is known is
   recognized as the **same** inverter once discovery later reports the same
   Cloud route carrying a physical serial: it keeps your custom name and any
   dismissal, stays a single card, and is never offered as a second device to
   add. (Identity uses the trusted serial and scoped route, never a raw Cloud
   route id or display name; two different serials on one route are blocked as an
   *Identity conflict* rather than merged.)
   *Add a device
   by hand* also lets you add a read-only **Zendure MQTT broker** and one or
   more **Zendure MQTT devices** telemetry discovery could not reach. Pick a
   *Zendure hardware generation* (telemetry/topic grouping), then an *Exact
   hardware model* from the EMS/Core registry. The form has two separate identity
   fields: the **Physical serial number** identifies the inverter (telemetry
   matching, duplicate detection), and the **MQTT device ID** is the exact MQTT
   route/payload id a control write targets — the physical serial is never used as
   the route id. A telemetry-only device needs only the serial. The model's write
   protocol, supported operations, and validation maturity are displayed before
   control can be enabled. Choosing **Unknown, values only**, omitting the
   model, or receiving conflicting model evidence always keeps the device
   read-only; a generation or topic family alone never authorizes writes — but it
   never blocks one either. What *is* evaluated per connection is the **MQTT
   broker source**: the manual broker form provisions a local broker, so a
   ZenSDK/Cloud-scalar generation entered there is added as a telemetry source
   with the reason *"Output control is not verified for this MQTT broker
   source"*, while the older Hub/Hyper generation stays controllable. A supported
   exact model on a connection that carries its write route joins the same
   control loop as a local API device, without hand-editing the config file
   (see
   [Zendure MQTT output control](../technical/configuration.md#zendure-mqtt-output-control)).
   Output control is not a checkbox: it follows the model and the write route.
   That route is `iot/<productKey>/<deviceId>/…` for every hardware generation,
   so the form asks for a **Product key** and an **MQTT device ID** whenever a
   control-capable model is selected, reports *Output control: available*, *not
   ready* (naming the missing identifier) or *unknown*, and adds the device as
   a telemetry source until the route is complete.
   Switching a device to another connection keeps its activation state in both
   directions, the same rule the Admin maintenance flow follows: active stays
   active, and a device you deactivated stays deactivated.

   For the **grid meter**, a Zendure D0 or Smart Meter 3CT found over local HTTP
   is the simplest choice ("Zendure Grid Meter via local HTTP", no MQTT setup).
   If instead a D0 is discovered on a local MQTT broker, the proposal offers
   **Use as grid meter**: choosing it maps the D0's
   `Zendure/sensor/<serial>/totalPower` topic to the central grid meter, reusing
   the selected broker profile. Only one grid meter can be active, and a grid
   meter already chosen in this Setup is only replaced when you explicitly
   confirm it.
   **Continue to deployment** saves the generated settings once the **Settings
   check** says **Ready**. **Download encrypted** and **Apply to the
   installation** under *Generated settings* are the alternatives: the first
   saves the settings as an encrypted backup, the second writes them to
   `config/config.json` (backing up an existing one) without deploying
   anything.
4. **04 Prepare** — preparing starts on its own: the Admin Console writes the
   generated config to the standard `config/config.json` (backing up any
   existing config first) and prepares the EMS deployment: the Compose file
   plus the target EMS image and resources. An existing installation is
   replaced only after **Continue and replace these files**. If
   `config/config.json` changed after you generated the config — because
   Settings & devices, a restore or a migration edited it — the save stops with a
   conflict instead of overwriting that change. Go back to 03 Settings, **Check
   the current settings** and continue again.
5. **05 Start EMS** — **Start EMS** (or **Restart EMS**), wait until it runs,
   then run the checks under **Diagnostics**, or `emsctl.py diagnose`, to
   confirm the install.

After setup, **Open the dashboard** opens it at `http://<host-ip>:8080`. Work
through the
[first-run checklist](../first-run-checklist.md), and use
[admin-maintenance.md](admin-maintenance.md) for later updates and backups.

If a legacy root `config.json` is present, the Admin Console can use it as source
data, but the applied target is always `config/config.json`.

## How your answers are read

The guided setup and Settings & devices read a configuration field the same
way, so the same answer produces the same stored setting no matter where you
gave it:

- Leading and trailing spaces are removed. A hostname pasted with a trailing
  space is stored without it.
- **Emptying a field removes the setting** rather than storing an empty value,
  so EMS falls back to its own default instead of reading a blank where a
  number belongs.
- A list field (such as the Shelly channels) accepts a comma-separated entry.
- **Passwords are the exception**: leaving a password box blank keeps the stored
  secret, because the console never shows one back to you. **Clear the stored
  password** removes a stored password.
- Changing a grid meter's type removes the fields the new type cannot use — an
  HTTP meter's address does not linger on an MQTT meter — while any extra key
  you added to the config by hand is left alone.

## Restarting or discarding a setup

**Start setup over** in the guided setup discards the whole run, not just the
browser view: the Admin Console cancels the pending System Build transition and
deletes the generated config and deployment marker it created, then returns to
the first step. Your installed EMS, live `config/config.json`, runtime data,
containers, volumes and backups are left untouched. The **Progress** card of
step 01 offers the same action as **Discard the setup** when a setup
transition needs escaping; a Guided Upgrade offers **Cancel the update**,
which ends the update only.

Discarding a setup is never a half-action. If the console is still busy with
something the setup itself started — updating the Admin Console, confirming the
System Build, preparing the release resources, verifying the System Build
resources, saving or applying the configuration, preparing the deployment,
starting EMS — the discard is **refused** and says which operation is still
running. Nothing has been discarded at that point: your draft and the setup stay
exactly as they were, and you can discard once that operation finishes. The
reverse also holds: once a discard has begun, the operation it interrupted cannot
start a System Build change afterwards.

Resource verification is worth calling out, because the progress display is
still on the System Build check while it happens: the
files are being unpacked and written at that moment, so a discard would leave a
half-written copy behind. The console therefore disables the discard until the
verification finishes or fails, and tells you to wait rather than reporting a
discard it did not perform.

### If a setup step takes too long

A System Build change has a time limit. Once it passes, the console stops trying
to continue it and offers **Discard the setup** as the way out — that is what keeps a
setup interrupted by a power cut or a crashed Admin Console from blocking the
system forever.

Running out of time does not by itself mean the work has stopped, so the discard
is not offered unconditionally:

- **Time limit passed and nothing is running** — **Discard the setup** is
  available. This
  is the normal recovery, including after an Admin Console restart, where any
  work from before the restart is gone by definition.
- **Time limit passed but an operation is still running** — **Discard the
  setup** stays disabled, and the card says *The System Build transition has
  expired, but its work is still running.* Wait for it to finish; the discard becomes available immediately
  afterwards. This matters most for resource verification, which writes shared
  files: discarding mid-write would leave a half-written copy behind whether or not
  the time limit has passed.
- **The console cannot tell whether anything is running** — **Discard the
  setup** stays disabled too, and says so. It never guesses "probably finished".

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

A failed System Build step does not throw away the setup. The **Progress** card
of step 01 offers:

- **Resume the transition** — retry the step that failed, from the exact point
  it is safe to retry from;
- **Discard the setup** — stop the setup and remove the files it created, as
  above.

A Guided Upgrade recovery additionally offers **Go back to the version that
runs**, which puts the Admin Console back on the System Build your EMS is
currently running.
When that build is a Development build, the console asks you to accept its risk
first, as it does when you choose one.
That action is deliberately **not** offered during a setup. Returning is really
two steps — end the failed operation, then start a new one — and during a setup
there is no record that would own the new operation afterwards, so a later
retry, discard or Admin restart could not tell who it belonged to. Resuming and
discarding both have a clear owner and cover the same situations. If the
action is requested anyway, the server refuses it and changes nothing.

### One setup at a time, even with two browser windows

Two browser windows can be open on the same setup, but only one setup is ever
current, and only the window that is on the current setup can change it. When one
window changes the selected System Build or discards the setup, the other window's
confirmation stops being valid immediately — including its own Fresh Setup
confirmation, which belongs to the setup it was given for and is never carried
over to the replacement. The stale window says *This tab no longer owns the setup*
and offers **Open the current setup**; it never silently acts on the newer
setup, and nothing it had prepared is applied.

If the console cannot tell whether a pending System Build change belongs to the
setup you are looking at, it refuses that change and cancels nothing. This is
deliberate: cancelling somebody else's System Build transition, or deleting files
whose owner cannot be proven, would be worse than asking you to reload and act on
the setup the server actually has.

### If temporary files remain

Stopping the setup and clearing its files are two separate things, and the console
never pretends otherwise:

- **A setup still owns temporary files.** No new setup and no upgrade can start
  until the cleanup succeeds; the setup says *An earlier setup still owns
  temporary files. Release it under System before starting again.*, and
  **Cancel the unfinished workflow** on the System page clears it. Your live
  `config/config.json` and the running EMS were not changed by the failed
  cleanup. The state survives a browser reload and an Admin restart, and the
  cancel always applies to the same setup — so you never have to work out which
  files belong to what, and you never need to delete JSON files by hand.
- **Ownership needs a review.** If the console finds a file it cannot prove
  belongs to this setup — a generated config left by an older Admin version, or a
  deployment marker written before setups were tracked — it **keeps** it and says
  so instead of deleting it. Retrying would not change the answer, so nothing
  is offered to retry. Nothing was changed on your running system; a
  maintainer has to look at the leftover file. The same applies while a setup is
  running: such a file is never deployed either, and the console asks you to
  generate the configuration again.

Changing the selected System Build after a setup has started retires the
previous setup the same way: the console asks the server to supersede it, so
the earlier build's generated config and deployment plan are removed before the
new build continues. Nothing from the old build stays behind — and if the previous
setup was still busy, the build change is refused rather than cutting it off.

If you want to start a guided upgrade while a setup is still unfinished, the
console asks you to **Discard the setup** first. The upgrade only begins after that
cleanup is confirmed — an unfinished setup can never deploy over an upgrade, or
the other way round. This holds for both upgrade steps: neither verifying a build
nor upgrading the system starts while setup files remain. **Cancel the update** in
turn keeps your running system, your live configuration and the last known-good
build exactly as they are.

## If the configuration changed while you were working

Setup checks two things before it saves or applies anything: that the draft is
exactly the one the **Settings check** checked, and that `config/config.json`
has not changed since that check was made.

- **Changing the draft requires a new check.** As soon as you edit a device, a
  setting or a broker password, **Apply to the installation** and **Continue to
  deployment** wait until the check has run again. The check you looked at is
  what gets saved — a check of one draft can never save a different one.
- **A changed live configuration stops the save.** If `config/config.json`
  changes in the meantime — through Settings & devices, a restore, or another
  session — the save is refused and your draft is kept: *The installation's
  settings changed after this setup was opened. Your draft was not applied.*
  Choose **Check the current settings** to re-check it against the current
  config, then save again.
- **Another setup session can take over.** If the setup was started over or
  its build changed elsewhere, this browser tab belongs to an older session and
  can no longer change anything. The console says so and offers **Open the
  current setup** (continue with the session that is now current) or **Discard
  this tab's draft** (drop only this tab's unsaved draft). The other session is
  never affected.

After upgrading the Admin Console while a setup was unfinished, a configuration
generated by the older version has to be generated once more: the console
returns you to the settings step and asks you to check it again. Nothing is
deleted, and your installed system is not touched.

Full detail: [../admin-discovery.md](../technical/admin-discovery.md). Layout and legacy
migration: [config-layout.md](config-layout.md).
