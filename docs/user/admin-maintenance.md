# Admin Maintenance

This is the behavioural reference for an existing EMS installation in the Admin
Console: every page but the guided setup, which has its own reference,
[Admin setup](admin-setup.md). The step-by-step guides with screenshots are in
[docs/user/admin/](admin/index.md).

## What the console does for an existing system

- detects the current installation
- checks config and containers
- runs diagnostics
- creates backups
- guides updates
- helps with restore

It reads the real system state. It never silently replaces config or
deletes data. The Admin Console is orchestration and UI only; the EMS core stays
the source of truth and owns the standard `config/config.json` layout.

## The pages

Start the Admin Console and open `http://127.0.0.1:8090` (see
[Admin setup](admin-setup.md#start)). It opens on the
[assistant](admin/assistant.md), or on the Overview if you turned the assistant
off; the setup is never started on its own. Settings in the old place (a
legacy root `config.json`) are offered a move to `config/config.json` on the
**System** page first (see [config-layout.md](config-layout.md)).

An existing installation is managed on these pages:

| Page | What it is |
|---|---|
| **Overview** | Is everything in order: what needs your attention, ranked, the tiles, Control & safety, the versions |
| **Updates** | One Target System Build; Admin + EMS aligned together (the guided upgrade) |
| **Settings & devices** | Features, the safety switches, expert tuning, devices and the network search |
| **Services** | What runs, **Restart EMS now**, **Repair analytics** |
| **Backups** | Create, inspect, download, bring back, restore or delete EMS backups |
| **Diagnostics** | Read-only EMS checks |
| **System** | Files, device telemetry, an older MQTT setup, unfinished workflows, the guided setup |

Reading and changing are separate: the Overview and Diagnostics never edit
anything; the System page changes something only through a button you confirm
(moving the settings file to its new place, applying the MQTT migration,
cancelling or clearing an unfinished workflow); and Settings & devices restarts
EMS only when you confirm **Restart EMS now**. Every page has its own address
(`#/updates`, `#/settings/safety`, …).

## Guided upgrade

You make **one** version decision: the **Target System Build**. A System Build is
a matched Admin + EMS pair, so choosing it aligns both the Admin Console and EMS
to the same build — there is no separate Admin-target choice and no way to move
EMS forward while intentionally leaving the Admin behind.

The **Updates** page lists the versions grouped **Stable releases**, **Rolling
build**, **Release candidates** and **Development builds**, and recommends one
(see [Guided Upgrade](admin/guided-upgrade.md) for which). Choosing a
development build is itself the explicit decision — there is no separate
acknowledgement checkbox; the acknowledgement is bound automatically to that
exact build so an interrupted upgrade can resume safely.

**Choosing a version checks it at once**, and changes nothing on the host: the
Admin and EMS images are downloaded (or reused), the exact paired build is
verified, and the check returns an immutable **selection fingerprint** of the
resolved pair (tag, channel, revision, build id, Admin digest, EMS digest). The
update you then start is bound to that fingerprint. Choosing another version,
or checking again, clears the previous check, so the new target is checked
once. If the check stops because the GitHub Container Registry limited the
downloads, no changes were made — wait and **Check again** (see
[troubleshooting](../technical/troubleshooting-reference.md)). While the images
download and verify, the card reads *Checking the version*; on the first run
this can take a few minutes.

The installation-specific preflight (current state, Zendure MQTT migration review,
backup readiness) does **not** run during the check. It runs when you start the
update, immediately before any change, together with a re-check of the selection
fingerprint: the target is re-resolved and, if its image or build metadata moved
since the check (for example a mutable `latest` tag re-pushed to a new digest),
the update is refused before any preflight, backup, migration, or deployment,
and the page asks you to check the version again. Otherwise the installation
preflight runs and the update proceeds.

Choose the target version. Review the plan of what the update will do, leave
**Back up first (recommended)** on, and give one explicit confirmation. The page
then shows ordered progress while the server runs this pipeline:

1. Resolve the target System Build and re-check the verified selection
   fingerprint, rejecting the upgrade if the resolved pair changed since Verify.
2. Inspect the current installation.
3. Review the Zendure MQTT migration, including affected devices and devices
   that will lose control.
4. Create and verify a backup.
5. Apply the reviewed Zendure MQTT migration through the EMS-owned migration
   service.
6. Run the generic config upgrade.
7. Validate the final config with target-compatible EMS code.
8. Align the Admin to the target System Build, reconnect if needed, and verify
   its identity.
9. Prepare the target EMS image and resources.
10. Recreate EMS.
11. Run the health check.
12. Run diagnostics.
13. Mark the Known-Good state.

The page follows it in five phases — **Check the new version**, **Back up and
prepare**, **Update the Admin console**, **Install EMS …**, **Check that
everything runs** — with the running phase saying what it does and a clock
counting up, so a long step (for example while the target image downloads, or
during the health check) stays visibly active rather than looking frozen. If the
Admin Console itself is replaced, the page shows *The Admin console does not
answer. Reconnecting…* and reloads from the new console once it answers. On a
slow host (a Raspberry Pi pulling the new image) that can take several minutes:
after two the page offers **Reload page**, and it keeps waiting either way —
nothing is wrong underneath, the transition is stored and resumes as soon as
the new Admin answers.

You do not have to keep the page open. The replaced Admin picks the upgrade up
itself when it starts, so closing the console, losing the network or putting the
laptop to sleep during the replacement no longer leaves the upgrade waiting; when
you come back, it has either finished or is on a step you can see.

The other side of that: **closing the console does not stop an upgrade.** The
confirmation you gave is what the system acts on, and it outlives the browser
by design. An update that stopped offers **Cancel the update** when the server
allows it — never while a step is mid-flight, rather than leaving something half
applied.

If the replacement never arrives — the container was killed by a power cut or an
out-of-memory kill mid-download — the console says so instead of waiting, and
offers **Cancel the update** so you can start it again.

The current-state preflight and the verified backup always run **under the
Admin that is currently running**, before any Admin alignment. The target Admin
is never assumed before the backup and preflight are complete.

Guided upgrade moves **forward**, or back by one or more patches inside the
release line you are running — `v0.8.3` back to `v0.8.1`, so a bad patch can be
undone. It never offers that by itself: the proposed build is the newest stable
release that is neither the one you are running nor behind it; without one,
nothing is proposed and you choose, and the rolling `latest` is proposed only
when nothing identifiable runs. Going further back, to an older
minor or major, belongs to the Backup / restore flow. It never removes
containers, volumes, or data. The Admin Console asks
for confirmation before changing config, compose files, containers, or data.

**The release tag names the build; the runtime image is the verified digest.**
After verification the release tag (`v0.8.0`, `latest`, an `-RC` or a `dev-…`
alias) is only a label. Guided upgrade pulls and writes the EMS image into
`docker-compose.yml` by its exact verified digest
(`ghcr.io/basecubedev/ems-solarflow-api-control@sha256:…`), not the tag, so a
later registry change to that tag can never alter the installed EMS image or the
image a restart/recreate runs. The Maintenance Overview still shows the readable
release (for example `v0.8.0`) — recovered from the running image's build labels,
the exact local Compose image, or a digest-matching known-good record — alongside
the digest where space permits; the digest is never shown as the version. A
release you have only **prepared** (downloaded) is never shown as installed, so
preparing a newer build does not make a genuine forward upgrade look like a
downgrade.

**A running EMS container is the active baseline.** Its immutable image identity
is authoritative: if that identity cannot be read (for example a digest-pinned
image whose build labels are missing), the installed release is shown as
**unknown** with a short warning, and the Compose or last-known-good release is
**not** presented as the running one. The Compose image and the known-good record
are used only as fallbacks when no EMS container is active (absent, stopped, or
Docker unavailable). A mutable tag that was moved after the container started
cannot change the perceived running release, because the immutable image identity
is preferred over the tag.

A reference that names no particular image is treated as unreadable rather than
resolved. `docker ps` reports a container created from a digest-pinned reference
as the bare repository, and asking Docker about a bare repository answers for
`:latest` — a different image whose build labels would otherwise be reported as
the running release, as confidently as a correct answer. Admin therefore reads
the reference and the immutable id from the container itself, and shows the
release as **unknown** rather than naming a build that is not running.

Because verification already downloaded the exact EMS image, Guided upgrade
**reuses that local image and makes no registry request** when the verified
digest is already present; it contacts the registry only when the exact digest is
missing. The EMS container is still recreated, so the upgrade always takes effect.
When the exact verified digest is missing locally and the digest pull fails, the
typed failure — a GitHub Container Registry rate limit, a network error, or a
generic pull failure — is preserved through the whole upgrade job: no Compose
change is written and the EMS container is not recreated. The specific message
(and, for a rate limit, the actionable GHCR guidance) stays visible, the verified
target remains selected, and you can retry.

### Admin alignment (automatic)

Admin alignment is an automatic stage of the pipeline, not a separate decision:

- If the running Admin already matches the target System Build, it is kept as-is
  — the container is **not** recreated.
- If the Admin content matches but its persistent Compose tag is stale, the tag
  is corrected so it points at the canonical System Build (a persistent retag).
- If the Admin does not match, it is updated to the target build automatically.
  The page loses contact while the Admin restarts on the new image, reloads
  from it, and the upgrade continues on its own from the saved transition.

You confirm the whole upgrade **once**. There is no second, Admin-specific
confirmation, no standalone *Update Admin* button, and no option to skip Admin
alignment (or leave the Admin on a different build) in the normal flow.

**Admin and EMS are named separately.** **What runs now** on the Updates page
names the EMS container's release and the Admin's own release; the latter is
read the same way, from the running Admin container's build labels by its
immutable image id — the answer the Appliance Manager gives too. The Overview's
**Versions**, the update's **Runs now** and
`/api/admin/maintenance/admin-update/status` (`current_admin.release`) all use
it; the Admin's environment is read only when no Admin container runs to
answer.

The System Build known-good record follows what runs and passes the health check
an upgrade requires. An upgrade or setup that completes records its pair; besides
that, whenever the System Build state is read (`/api/admin/system-alignment/status`)
and no transition is in flight, the console reads the running Admin and EMS
containers by their image ids. When both run — Docker reports them running, and a
Docker health check, where the image has one, not failing — and their build labels
are one System Build by the rule a resolution checks before it installs a pair, the
record is set to that pair once its EMS passes the health check an upgrade requires
before it records a pair (the EMS diagnosis, judged by the same fail-closed rule);
`running_pair.state` is `recorded`, or `current` when the record already named it.
An Admin and EMS that both moved without the console's completion, or a record that
was never written, are picked up this way.

The EMS image has no Docker health check, so Docker's word alone would count an
EMS in a crash loop (`Up 2 seconds`) as healthy. That health check runs several
`docker exec` commands, so the status read never waits for it: the first read for
a pair the record does not name starts one check in the background and answers
`running_pair.state: unverified` with `health: checking`. When the check passes,
the console records the pair right then, however long the check took and whether
or not the page is read again (outside a transition the console does not poll):
by the same rules as a read, and only while the pair the check was run for still
runs and the record is still the one that read found. If anything moved in
between, nothing is written and a later read decides afresh. A
failed check answers `health: failed` and leaves the record alone; its verdict
counts for two minutes, after which the next read checks again, so a pair that
never passes costs at most one diagnosis every two minutes while the page is read.
A record that already names the running pair, or a pair that is not one build,
runs no check at all.

A Guided Upgrade cancelled after it replaced the Admin leaves the Admin on the
target build and EMS on the old one. That is not one System Build, so it is never
recorded: the record keeps the pair it last verified, `running_pair` names both
builds (`state: unpaired`), and **What runs now** on the Updates page reads
**Not a pair**. An Admin reinstalled from the Appliance
Manager at another build than the EMS is named the same way. Upgrading to one
System Build aligns both and records the pair again. While a transition is in
flight, including a recoverable failure whose **Go back to the version that
runs** reads the record, the record is not touched.

If you are asked to sign in again after the Admin Console restart, use the same
EMS Dashboard/Admin password; the page then opens **Updates** and follows the
update on. The upgrade resumes exactly where it left off: the completed backup
and current-state preflight are **not** repeated.

When **Go back to the version that runs** finds the Admin Console already on
that version, **System alignment** on the Updates page shows what is left:
**Continue** checks that version's files, and **Release the console** leads to
the System page, where the recovery card frees the console. If the new Admin
Console does not come back on its own, check its logs and start it again:

```bash
docker compose -f docker-compose.admin.yml logs
docker compose -f docker-compose.admin.yml up -d
```

## Overview and Services

- **The Overview** is read-only. It answers whether the installation is in
  order, ranks what needs attention (errors before warnings before notes, each
  with what to do), and shows the EMS, analytics, control and backup tiles,
  **Control & safety** and **Versions**: the Admin image and the EMS image as
  separate component identities, and the local dashboard's address
  (`http://localhost:8080` by default). It never builds, starts, stops, or
  changes anything. **Saved settings are newer than the running EMS** is the
  finding for a settings file written after EMS started: until a restart, part
  of what you saved is not in effect.
- **Services** shows the EMS and analytics database containers and what a
  restart would change. **Restart EMS now** recreates EMS with the saved
  settings and starts or stops the bundled InfluxDB to match them; when
  analytics is on it also creates the missing analytics buckets and
  downsampling tasks. **Repair analytics** creates the missing buckets and tasks
  without touching any container; it deletes nothing. The two never run at the
  same time. A shorter retention or a removed downsampling entry is applied only
  by a full schema sync, `docker compose exec ems python3 emsctl.py influx
  sync`, because it deletes the data that falls out of the new retention.

## Settings & devices

This page edits an existing installation.

- **Four tabs over one draft** — Features, Safety, Expert, Devices — with
  **Search settings** across every setting and one bar at the foot of the page
  that counts your unsaved changes. Switching a tab never reloads and never
  discards unsaved changes; they also survive a reload, and **Discard** drops
  them.
- **Safety** holds the switches that decide whether EMS may change your
  inverters, with nothing hidden behind a disclosure. They are **on by
  default**: a fresh installation controls your inverters without you enabling
  anything, and the switches exist to stop it. A switch your settings file does
  not mention is shown at that default; nothing is written until you change it.
- **Check changes** sends your draft to the server, which merges it with the
  current config and shows the result: whether it is valid, and every value that
  would change, split into *Takes effect at once* and *Needs an EMS restart*.
  Nothing is written by editing or checking. **Save and apply** is the one
  action that writes config — it validates the change, backs up the current
  config first (unless you switch **Back up my settings first** off), then
  writes it. A value its field cannot hold — text in a number field, a switch
  that is neither true nor false, a number outside the field's range — is
  refused by name instead of being saved as text, read as *off* or cut to a
  whole number; a value you did not change is left as it is stored. Saving
  writes `config.json` and additionally mirrors the whitelisted overlapping
  values it changed (system power/loop limits, winter enable, and per-device
  enabled/max power/PV priority) into `data/runtime-state.json` so the change
  goes live immediately instead of waiting for an EMS restart; it still never
  moves `data/` history, `docker-compose.yml`, or databases. Fields that also
  carry a live override set from the Dashboard show **Running: …** with the
  effective value, and **Put saved values back** makes the live EMS use the
  saved values again.

  The **Devices** tab holds the configured hardware as collapsible cards — the
  grid meter, local-API inverters, Zendure MQTT devices (telemetry-only, or
  controllable for a supported family), read-only devices and the Zendure MQTT
  broker — and every card shows the same fields, labels, and units as the
  guided setup (shared catalog), with advanced and expert settings folded
  away. The grid-meter card shows the connection fields for the selected meter
  type (for example Tasmota URL and power path, MQTT broker/topic settings, or
  the Zendure SmartMeter D0 serial that generates its MQTT topic automatically).

  Adding hardware works like the guided setup: **Search my network** runs
  discovery (plus a **Scan** of one address or range), lists what it found as
  cards with one-click **Add inverter**, **Use as grid meter** or **Add as
  read-only device** (an E3/DC, see
  [device management](admin/device-management.md)) and their match state
  against the current config (**In the settings**, **Not found**, **Address
  changed**), and **Add an inverter**, **Add an inverter over MQTT** and **Add an
  E3/DC (read only)** add one by hand. Adding a device — from discovery or by
  hand — opens its card, where all device configuration happens. Discovery here
  also surfaces Zendure MQTT config proposals you can add straight to the draft.
  An MQTT proposal recognized as a grid meter offers **Use as grid meter**
  instead of **Add inverter** and fills the grid-meter card; if a grid meter is
  already configured you are asked before it is replaced. When that meter sits
  on an MQTT broker your config does not know yet, the broker profile is added
  to the same draft, exactly as it is for an MQTT inverter — an already
  configured broker is reused, never duplicated.
  Like every discovery action, this only changes the draft — nothing reaches the
  live config before you check and save.

  A newly added Local API or Zendure MQTT inverter receives the next compact
  EMS name (`INV_1`, `INV_2`, …) from one sequence shared by all transports.
  The name is the operational identifier used by config, logs, dashboard state,
  and the Flowchart; model, address, serial number, transport, and hardware
  generation remain separate card details. On a Zendure MQTT card the
  **Serial number** (physical identity) and the **MQTT device ID** (exact MQTT
  route/payload id) are two independent fields: editing one never changes the
  other, and the physical serial is never used as the route id. Output control
  needs an explicit MQTT device ID; clearing it disables control until you enter
  the real route id. Clearing an identifier field really removes it: Preview and
  Apply write the field exactly as you left it and a reload shows it empty, so a
  value you deleted never reappears. The one exception is a redacted Cloud value
  shown as `••••` — leaving that mask untouched keeps the stored identifier,
  because the browser is never given the real one. You may edit the proposed name
  before applying. Existing configured names are preserved exactly and are not
  migrated or renumbered when you open the editor, remove/reorder another
  device, run discovery, or apply an unrelated change.

  Every new inverter — manual or discovered, Local API or Zendure MQTT — starts
  with the same central default values (smart mode, output limit, PV size,
  PV priority, battery size, SoC limits, winter policy `auto`) from the config
  template/catalog, and both transports edit the identical common field set on
  their cards. Another
  configured inverter's values are never used as a template for a new one, and
  the values you see on a new card are exactly what checking and saving write.
  Under **Advanced settings** each inverter card offers its **Winter policy**:
  `auto` follows the plan for the device's type (PV with battery, battery only
  with PV size 0, PV only), the other entries pick one plan for this inverter.
  The plans and their defaults are described in
  [winter-mode.md](../winter-mode.md#device-types-and-policies).
  The default output limit is the generic central default (800 W), not a
  model-specific value — review it for your hardware. PV size is a
  configurable estimate for power sharing; discovery cannot measure the
  connected PV array. Existing explicit values are never replaced by new
  defaults: a device from an older config that lacks some of these values keeps
  its stored shape on a no-op apply (the missing values are shown as inherited
  defaults in the editor, and the EMS runtime falls back to its built-in
  defaults). New devices and transport switches materialize the central
  defaults; an existing incomplete device keeps missing fields until a draft
  change explicitly applies them. **Check changes** shows exactly which values
  would be written before you save.

  One physical inverter is always one config entry with one selected connection.
  A trusted serial number is the strongest cross-transport identity. A Cloud MQTT
  device with no physical serial instead receives a server-generated opaque
  equality token derived from its broker/account, product and route scope. The
  token is non-reversible and contains neither the route ID nor credentials; the
  browser compares it but never reconstructs identity from masked fields. The
  same route under another broker/account/product scope remains a separate
  device. Alternate broker-ref names for the sole configured Cloud account are
  normalized to that one account; multiple Cloud refs are kept distinct rather
  than guessed to match. When discovery finds an identity that is
  already configured over another connection — for example an API scan
  sees an inverter you configured over MQTT, or an MQTT proposal matches a
  configured API inverter — the review offers **Use this connection** on that
  device rather than a second **Add inverter** action. Switching the
  connection replaces the connection of the same logical device: the configured
  name, enabled state, and all common tuning values are preserved (also across
  a rename in the same draft), only the connection fields change, and stale
  fields of the previous connection are removed. This holds between two MQTT
  connections as well — one local broker to another, or a local broker to and
  from your Zendure Cloud account: the applied config carries the selected
  broker, transport, topic identity and route as one whole, never the new route
  on the old broker. If the selected broker is not configured yet, applying adds
  its profile; a broker name that already means a different connection is
  reported as a conflict instead of being overwritten.

  Switching is reversible inside one discovery session. What each discovered
  connection offers is decided by the *current draft*, not by what is installed:
  the moment you switch, the connection the inverter no longer uses is offered
  as **Use this connection** again, and the whole review — cards, notes, actions and
  counts — is rebuilt from the discovery results already on hand. No rescan, no
  reload. So `b1 → b2 → b1` between two local brokers, and `API → Zendure MQTT →
  API` (or the reverse), all work without leaving the page, each step selecting
  that exact broker and route and leaving exactly one logical inverter behind.
  *In the settings* therefore means "this is the installed connection and you
  have not changed it"; a connection you selected in this session reads as a draft change
  until you apply. Removing an inverter from the draft makes its connections
  addable again. Where a trusted identity would match more than one draft
  device, the action is a blocked **Identity conflict** rather than a guess at
  which inverter you meant.

  A configured MQTT device does not have to state its transport: the broker
  profile it references is the authority, and the Admin backend resolves the
  transport from it, so a Cloud device shows **Zendure MQTT** on its card before
  any discovery has run. When the transport genuinely cannot be resolved, the
  card claims none rather than defaulting to local MQTT.

  A serial-less Cloud device is
  also recognized as the same inverter when discovery later reports the **same
  Cloud route now carrying a physical serial**: the review shows it *In the settings*
  with no second **Add inverter**, and the existing entry is enriched with the serial
  (name and common values preserved) instead of being duplicated. The same
  trusted identity can never be added as two separate API and MQTT devices;
  contradictory evidence — the **same Cloud route claiming a different physical
  serial** — is shown as a blocked **Identity conflict** and fails validation
  instead of guessing or merging. Fresh Setup applies the identical rules: a
  route-only Cloud inverter selected in Setup keeps its custom name and its
  dismissal, renders one card and produces one device in the check once the
  same route later reports a serial. Browser-facing
  status and support data retain trusted physical serials and useful non-secret
  context, but remove credentials and expose only masked shapes for Cloud route,
  product and topic identifiers — never their full account-scoped values.

  **Search my network** searches all three sources, like the setup flow: the
  local network (mDNS refresh plus network scans), local MQTT brokers (a fresh
  read-only listen on reachable brokers, trying anonymous access and every
  saved discovery credential), and — when a Zendure API key is saved — the
  Zendure cloud MQTT broker. Local and cloud results land in the same review
  list; a device already found on a local broker is not offered a second time
  via the cloud. Without a saved API key the cloud source is simply skipped.
  The **What the search may use** card beside it holds the same settings as
  the setup's *Credentials for the search* — the MQTT credential pool for
  password-protected brokers and the Zendure API key (**Save key**, **Test**,
  **Find cloud devices**, **Forget key**) — shared with setup, not a second
  copy. mDNS has no row here: it refreshes automatically with every search.

  For Zendure MQTT devices, **Zendure hardware generation** groups telemetry and
  topic layouts, while **Exact hardware model** selects the concrete EMS/Core
  registry identity. Choose **Unknown / telemetry only** when the exact model
  cannot be established; the card then reads *Unknown model, read only*. The write protocol, validation maturity, supported operations,
  and current control readiness are shown read-only from the Core catalog.
  **Output control** is shown read-only, because it is a capability rather than
  a setting: EMS/Core derives it from the exact model, an implemented write
  route for that model, the MQTT broker source that route would travel through,
  and a complete write address (product key and MQTT device ID). A topic family
  or hardware generation alone never enables control, and conflicting discovery
  evidence stays telemetry-only until you review and correct the model. The
  broker source is the one connection property that does decide: the Zendure
  cloud broker carries the write route for every device, and a local broker
  carries it where it publishes the device's own JSON report topics. A local
  broker seen publishing scalar metrics only reads *output control is not
  verified for this MQTT broker*. When control is unavailable the card
  names the reason. A
  supported inverter you add or switch to is therefore controllable without a
  second step, exactly like a Local API inverter, which has no such switch
  either (see
  [Zendure MQTT output control](../technical/configuration.md#zendure-mqtt-output-control)).
  The card only reports this verdict; the derived value is written when you
  change a field or when the draft is loaded, so it always appears in
  **Check changes** before it is applied.

  An **external device** (`"type": "external_mqtt"`, see
  [External Devices over MQTT](../technical/configuration.md#external-devices-over-mqtt-external_mqtt))
  is an inverter or battery whose values a home-automation system such as FHEM
  publishes into the project's MQTT namespace. Discovery offers it under the local broker it was seen on, and
  **Add inverter** adds it to the draft as a read-only card; there is nothing to
  set beyond what every device has, because the catalog fixes its topics and it
  is never controlled. Like any device it has a **Device name** and an
  **Enabled** switch; switched off, it stays in the config but EMS neither reads
  it nor shows or counts its values (the device itself is never switched).
  Discovery shows the installed device as **In the settings**, whatever the broker
  profile it sits on is called, and the same device on another broker as an
  alternative: **Use this connection** moves it there on apply, keeping its name and
  switch. **Remove** takes it out of the config. Its catalog family and
  device id are not edited on this page, and its broker (and a retired
  family) only through **Use this connection**; apart from name, switch and
  connection, applying writes the installed entry back exactly as it is,
  whatever the browser sends for it, and a draft that leaves the entry out
  without its **Remove** is refused rather than applied. An entry an earlier
  development build adopted names topics EMS no longer reads; its card says
  so, every apply is refused until it is moved or removed, and **Use this
  connection** on the device discovery finds under the same id in the
  project's namespace moves it there, which also gives it the namespace's
  catalog family.

  Local API, Local MQTT and Zendure Cloud MQTT are alternative control
  transports of the same logical inverter. Switching between them keeps the
  device's identity, activation state, limits, SoC settings and allocation
  parameters, in every direction: a device you deactivated stays deactivated, and
  one you did not stays active. Activation is the device's **Enabled** state and
  nothing else; a device that cannot control output is telemetry-only by
  capability, which never deactivates it. Only you make a device inactive.
  Control capability, unlike activation, is re-evaluated for the new connection:
  moving a device to a broker whose write route is not verified keeps it enabled
  and turns it into a telemetry source, and the card says why.

  A discovered connection for which no write route resolves — an unknown model,
  or a missing product key / MQTT device ID — is offered as **Replace the control
  connection** with a warning, because that is exactly what the action does. It
  is a visible decision instead of a silent loss of regulation, and the change
  still passes through **Check changes** before anything is applied.
  Stored passwords (broker or MQTT grid meter) are never displayed; leave the
  password field blank to keep one, or tick **Clear the stored password** to
  remove it.

  Re-applying an existing broker with a different username or password
  **rotates the stored credential in place**: the new value is staged before
  the config is written, and if the apply fails the previous credential is
  restored exactly, so the live config never references a secret that does not
  match its stored record.

### Older MQTT device setup

The card is on the page only while a migration is actually pending; a
review that cannot be loaded also keeps it visible.

The **System** page carries the **Older MQTT device setup** card: the review
shows the affected devices, the exact-model decision for each device, whether
control is kept (**Control kept**) or switched off (**Control off, readings
kept**), and whether the migrated setup would be valid. Broker credentials and
API keys are never rendered.

**Back up my settings first** defaults on. **Apply the migration** requires the
review fingerprint and explicit confirmation; a stale review is rejected and
read again (*The review was out of date and has been read again. Check it and
apply again.*). Backup, validation, or atomic-write failure leaves the active
config unchanged. After success, the console reloads the real settings and the
runtime/control-readiness view. The
same EMS-owned migration service is used by Guided Upgrade—there is no separate
browser migration algorithm.

## Backups

The **Backups** page creates config, database, and system backups under
**Create a backup**, inspects what is inside a backup, previews a restore before anything is written,
and restores behind an automatic rollback backup. See
[Backup and restore](admin-backup-restore.md) for the full workflow.

Restore supports **config**, **database** and **bundled InfluxDB** backups. The
InfluxDB restore is orchestrated through the existing EMS CLI restore flow
(replace-style, preview and confirmation required); external InfluxDB is not
covered. See [Backup and restore](admin-backup-restore.md).

## Unfinished setup or update

**System → Unfinished setup or update** resolves a Guided Setup or Guided
Upgrade that can no longer be finished, switched away from, or started again.
The card is on the page only while a workflow needs it, and every other page
shows a banner with **Open recovery** meanwhile.

You normally do not need SSH or manual JSON deletion. Use this card instead.

It shows which guided workflow currently owns the console, its state and age, a
shortened workflow/operation reference, and whether an operation is still
running. Two actions can appear, and only when the Admin Console says they are
available:

- **Cancel the unfinished workflow** — the normal path. It stops the current workflow
  through its own owner: a cancellable System Build transition is cancelled, a
  Guided Setup is terminated with its claim-aware cleanup, and a Guided Upgrade
  context is cleared only for the operation it belongs to. No state file is
  deleted.
- **Force-clear leftover records** — for old Admin versions, corrupt
  workflow metadata and orphaned records left by a crash. It asks twice, backs
  the affected Admin workflow files up with their hashes, and only then clears
  them.

While an Admin workflow operation is still running, neither action is offered:
the card says *A step is running right now; recovery waits for it.* A failed
setup is resumed from the guided setup's **Resume the transition**, a stopped
update from its **Try again**. There is no force reset.

Two more states show up here rather than in a guided workflow:

- **Conflicting workflow records** — a Guided Setup and a Guided Upgrade both
  claim the console. Neither workflow can be entered around it, and nothing is
  discarded on its own; the recovery card is where you resolve it.
- **Unsupported or stale state** — Admin workflow metadata this version cannot
  use, for example an operation type from a newer Admin. The technical details
  name each affected file and why it is stuck.

If the Admin Console cannot reach Docker, the advanced release is refused: it
cannot prove that no Admin replacement is still running, and an unreachable
daemon is not proof that nothing is. Restore Docker access and try again.

### What recovery never touches

Both actions leave the installed system alone:

```text
config/config.json
data/runtime-state.json
docker-compose.yml
state/.admin-deployment.json
state/known-good-system-build.json
backups, containers and volumes
```

Only Admin workflow metadata is ever released:
`state/guided-setup-workflow.json`, `state/pending-transition.json` and
`state/guided-upgrade-context.json`. The Admin Console derives that list itself;
a browser can never name a file.

### Recovery backups

A release writes its backup under the Admin data directory:

```text
<admin data>/state/workflow-recovery/<UTC timestamp>/
```

The directory holds a byte-exact copy of each released file plus a
`recovery-manifest.json` recording when it ran, the selected reason, the Admin
revision, each file's SHA-256 and size, and the exact workflow-state fingerprint
the recovery was previewed against. Backups are kept until you remove them; the
Admin Console never deletes them by itself, and no credential material is copied
into the manifest.

### Switching between Guided Setup and Guided Upgrade

You do not need to find a discard action first. Choosing the other guided
workflow shows a preview of exactly what will be stopped and what stays
untouched, asks once, and then performs the switch as one backend operation.

Simply opening another page does **not** discard your work: your Guided Setup
draft is still there when you come back. Only an explicit switch, **Start setup
over**, or a workflow reset ends it.

Two cases are deliberately not switched automatically:

- an operation is still running — resume or wait for it;
- the previous Setup left files it cannot prove it owns — this is an ownership
  question an operator decides, so the recovery card is offered instead.

## Safety

Maintenance is conservative by default:

- **Backup before risky changes.** Config apply, guided upgrade, and restore all
  back up what they replace before writing.
- **No silent downgrade.** Guided upgrade moves forward, or back inside the
  release line you are running, and never proposes going back by itself;
  restore never installs an older EMS image. The line you are on is read from
  the EMS container's image even while that container is stopped, so a
  crashed EMS can be upgraded or reinstalled from here.
- **Preview before restore.** A restore always starts with a preview and is only
  applied after you confirm it.
- **Confirmation before writes.** The Admin Console asks before it changes
  config, compose files, containers, or data.
- **Diagnostics after update.** Run diagnostics after an upgrade and review the
  result.

Run the Admin Console only on a trusted local machine and never expose it to the
internet — it controls Docker on the host.

## Advanced details

For version detection, release checks, upgrade gating, and Docker execution
details, see the [Admin technical reference](../technical/admin-discovery.md).
The CLI equivalents for backup and restore are documented in
[backup-restore.md](../technical/backup-restore.md).
