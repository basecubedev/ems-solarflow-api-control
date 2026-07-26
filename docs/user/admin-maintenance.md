# Admin Maintenance

Use Maintenance for an existing EMS installation. This is the second of the two
Admin Console flows; the first is [Admin setup](admin-setup.md) (Set up a new
system).

## What Maintenance does

- detects the current installation
- checks config and containers
- runs diagnostics
- creates backups
- guides updates
- helps with restore

Maintenance reads the real system state. It never silently replaces config or
deletes data. The Admin Console is orchestration and UI only; the EMS core stays
the source of truth and owns the standard `config/config.json` layout.

## Opening Maintenance

Start the Admin Console and open `http://127.0.0.1:8090` (see
[Admin setup](admin-setup.md#start)). The start screen detects the current
install and recommends the safest flow. Any existing installation is routed to
**Manage my existing system**; the setup wizard is never started automatically.
A legacy root `config.json` is offered a migration to `config/config.json` first
(see [config-layout.md](config-layout.md)).

Maintenance offers three paths:

| Path | What it is |
|---|---|
| **Guided upgrade** | Pick one Target System Build; align Admin + EMS together (recommended) |
| **Manual configuration** | Inspect, edit, and restart an existing EMS setup |
| **Backup / restore** | Create, inspect, restore, or delete EMS backups |

## Guided upgrade

You make **one** version decision: the **Target System Build**. A System Build is
a matched Admin + EMS pair, so choosing it aligns both the Admin Console and EMS
to the same build — there is no separate Admin-target choice and no way to move
EMS forward while intentionally leaving the Admin behind.

The Target System Build selector uses the same catalogue as Guided Setup, grouped
**Latest**, **Stable**, **Unstable**, then **Experimental**. Selecting an
Experimental build is itself the explicit decision — there is no separate
acknowledgement checkbox; the experimental-build acknowledgement is bound
automatically to that exact build so an interrupted upgrade can resume safely.

Selecting a target System Build — like Guided Setup — **downloads nothing**: it
only previews the build, so you can browse several targets without contacting the
container registry. A build shown as *Resources cached* only means its release
resources are already on disk; that is **not** a verified System Build.

**Verify System Build** downloads (or reuses) the Admin and EMS images, verifies
the exact paired build, and returns an immutable **selection fingerprint** of the
resolved pair (tag, channel, revision, build id, Admin digest, EMS digest). The
plan you then build is bound to that fingerprint. Changing the target, or
verifying again, clears the previous verification and plan, so you verify the new
target once. If verification stops with a *GitHub Container Registry rate limit
reached* message, no changes were made — wait and select **Verify System Build**
again (see [troubleshooting](../technical/troubleshooting-reference.md)).

While the images download and verify, the **Verify System Build** button shows a
spinning progress ring and reads *Verifying…*; on the first run this can take a
moment, so the spinner is your signal that it is working, not stuck.

The installation-specific preflight (current state, Zendure MQTT migration review,
backup readiness) does **not** run during Verify. It runs at **Upgrade system**,
immediately before any change, together with a re-check of the selection
fingerprint: the target is re-resolved and, if its image or build metadata moved
since verification (for example a mutable `latest` tag re-pushed to a new digest),
the upgrade is rejected before any preflight, backup, migration, or deployment,
and you must **Verify System Build** again. Otherwise the installation preflight
runs and the upgrade proceeds.

Choose the target System Build. Review the plan, leave **Create a backup**
enabled, and give explicit confirmation before execution. The page then shows ordered progress
through this pipeline:

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

The live stage tracker highlights the current step, gently pulses it, and labels
it *Working…*, so a long step (for example while the target image downloads, or
during the health check) stays visibly active rather than looking frozen. If the
Admin Console itself is replaced, a full-screen reconnect spinner takes over and
the page reconnects on its own.

The current-state preflight and the verified backup always run **under the
Admin that is currently running**, before any Admin alignment. The target Admin
is never assumed before the backup and preflight are complete.

Guided upgrade only ever moves **forward**. It never removes containers, volumes,
or data. Downgrades belong to the Backup / restore flow. The Admin Console asks
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
  The page shows a reconnect screen while the Admin restarts on the new image,
  then the upgrade continues on its own from the saved transition.

You confirm the whole upgrade plan **once**. There is no second, Admin-specific
confirmation, no standalone *Update Admin* button, and no option to skip Admin
alignment (or leave the Admin on a different build) in the normal flow.

If you are asked to log in again after the Admin Console restart, use the same
EMS Dashboard/Admin password. The upgrade resumes exactly where it left off: the
completed backup and current-state preflight are **not** repeated.

A standalone Admin repair exists only under **Advanced → Recovery**, for
restoring an inconsistent Admin after a failed transition. If the new Admin
Console does not come back on its own, check its logs and start it again:

```bash
docker compose -f docker-compose.admin.yml logs
docker compose -f docker-compose.admin.yml up -d
```

## Manual configuration

This path inspects and edits an existing installation.

- **Overview** is read-only. It shows the install state, the resolved
  `config/config.json`, `data/`, and `docker-compose.yml` paths, the EMS and
  InfluxDB containers, the Admin image and EMS image as separate component
  identities, and a link to the local dashboard
  (`http://localhost:8080` by default). It never builds, starts, stops, or
  changes anything.
- **Config editor** loads your real config as a draft you can edit, then shows a
  preview of the changes. Nothing is written by editing or previewing. Applying
  the draft is the one action that writes config — it validates the change,
  backs up the current config first, then writes it. Apply writes `config.json`
  and additionally mirrors the whitelisted overlapping values it changed (system
  power/loop limits, winter enable, and per-device enabled/max power/PV priority)
  into `data/runtime-state.json` so the change goes live immediately instead of
  waiting for an EMS restart; it still never moves `data/` history,
  `docker-compose.yml`, or databases. Fields that also carry a live override set
  from the Dashboard show a badge with the effective value, and a **Reset live
  overrides** button writes the installed config values back so the live EMS
  matches the installed config again.

  The hardware section looks and works like the Fresh Install Config step.
  Configured hardware renders as collapsible cards — the grid meter, local-API
  inverters, Zendure MQTT devices (telemetry-only, or controllable for a
  supported family), and the Zendure MQTT broker —
  and every card shows the same fields, labels, and units as Fresh Install
  (shared catalog), with advanced and expert settings in nested collapsed
  areas. The grid-meter card shows the connection fields for the selected
  meter type (for example Tasmota URL and power path, MQTT broker/topic
  settings, or the Zendure SmartMeter D0 serial that generates its MQTT topic
  automatically).

  Adding hardware also works like Fresh Install: the **Add more devices** row
  runs discovery (plus a manual scan), lists candidates as cards with
  one-click **Add as inverter / Add as grid meter** actions and their match
  state against the current config (In config, Not found, IP changed), and
  offers manual adding for devices discovery cannot reach. Adding a device —
  from discovery or manually — opens its configured card, where all device
  configuration happens. Discovery here also surfaces Zendure MQTT config
  proposals you can add straight to the draft.

  A newly added Local API or Zendure MQTT inverter receives the next compact
  EMS name (`INV_1`, `INV_2`, …) from one sequence shared by all transports.
  The name is the operational identifier used by config, logs, dashboard state,
  and the Flowchart; model, address, serial number, transport, and hardware
  generation remain separate card details. You may edit the proposed name
  before applying. Existing configured names are preserved exactly and are not
  migrated or renumbered when you open the editor, remove/reorder another
  device, run discovery, or apply an unrelated change.

  Every new inverter — manual or discovered, Local API or Zendure MQTT — starts
  with the same central default values (smart mode, output limit, PV size,
  PV priority, battery size, SoC limits) from the config template/catalog, and
  both transports edit the identical common field set on their cards. Another
  configured inverter's values are never used as a template for a new one, and
  the values you see on a new card are exactly what preview and apply write.
  The default output limit is the generic central default (800 W), not a
  model-specific value — review it for your hardware. PV size is a
  configurable estimate for power sharing; discovery cannot measure the
  connected PV array. Existing explicit values are never replaced by new
  defaults: a device from an older config that lacks some of these values keeps
  its stored shape on a no-op apply (the missing values are shown as inherited
  defaults in the editor, and the EMS runtime falls back to its built-in
  defaults). New devices and transport switches materialize the central
  defaults; an existing incomplete device keeps missing fields until a draft
  change explicitly applies them. The preview shows exactly which values would
  be written before you apply.

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
  already configured over another transport — for example a Local API scan
  sees an inverter you configured over MQTT, or an MQTT proposal matches a
  configured Local API inverter — the review offers **Use Local API instead**
  / **Use Local MQTT instead** / **Use Zendure Cloud MQTT instead** on that
  device rather than a second **Add as inverter** action. Switching the
  transport replaces the connection of the same logical device: the configured
  name, enabled state, and all common tuning values are preserved (also across
  a rename in the same draft), only the connection fields change, and stale
  fields of the previous transport are removed. A serial-less Cloud device is
  also recognized as the same inverter when discovery later reports the **same
  Cloud route now carrying a physical serial**: the review shows it *In config*
  with no second **Add**, and the existing entry is enriched with the serial
  (name and common values preserved) instead of being duplicated. The same
  trusted identity can never be added as two separate API and MQTT devices;
  contradictory evidence — the **same Cloud route claiming a different physical
  serial** — is shown as a blocked **Identity conflict** and fails validation
  instead of guessing or merging. Fresh Setup applies the identical rules: a
  route-only Cloud inverter selected in Setup keeps its custom name and its
  dismissal, renders one card and produces one device in the preview once the
  same route later reports a serial. Browser-facing
  status and support data retain trusted physical serials and useful non-secret
  context, but remove credentials and expose only masked shapes for Cloud route,
  product and topic identifiers — never their full account-scoped values.

  **Start discovery** searches all three sources, like the setup flow: the
  local network (mDNS refresh plus network scans), local MQTT brokers (a fresh
  read-only listen on reachable brokers, trying anonymous access and every
  saved discovery credential), and — when a Zendure API key is saved — the
  Zendure cloud MQTT broker. Local and cloud results land in the same review
  list; a device already found on a local broker is not offered a second time
  via the cloud. Without a saved API key the cloud source is simply skipped.
  The collapsed **Discovery sources** rows underneath hold the same settings
  as the setup Discovery step — the MQTT credential pool for
  password-protected brokers and the Zendure API key (save/test/refresh) —
  shared with setup, not a second copy. mDNS has no row here: it refreshes
  automatically with every discovery run.

  For Zendure MQTT devices, **Hardware generation** groups telemetry and topic
  layouts, while **Exact hardware model** selects the concrete EMS/Core registry
  identity. Choose **Unknown / telemetry only** when the exact model cannot be
  established. The write protocol, validation maturity, supported operations,
  and current control readiness are shown read-only from the Core catalog.
  Output control is offered only for a supported exact model on a compatible
  transport; a topic family or generation alone never enables it. Conflicting
  discovery evidence stays telemetry-only until you review and correct the
  model. A newly added device that resolves to a control-ready model with a
  write target enables output control by default, so a supported inverter you
  add is controllable without a second step; clear the **Output control**
  checkbox to keep it telemetry-only instead. An existing device's model and
  control setting are preserved on a no-op apply and never silently changed (see
  [Zendure MQTT output control](../technical/configuration.md#zendure-mqtt-output-control)).
  Stored passwords (broker or MQTT grid meter) are never displayed; leave the
  password field blank to keep one, or use the clear checkbox to remove it.

  Re-applying an existing broker with a different username or password
  **rotates the stored credential in place**: the new value is staged before
  the config is written, and if the apply fails the previous credential is
  restored exactly, so the live config never references a secret that does not
  match its stored record.

### Zendure MQTT migration

The Manual configuration path includes a compact **Zendure MQTT migration**
card with Review → Backup → Apply → Validate stages. Review shows affected
devices, the exact-model decision for each device, and whether control is kept
or disabled. Broker credentials and API keys are never rendered.

Backup defaults on. Apply requires the review fingerprint and explicit
confirmation; a stale review is rejected and refreshed. Backup, validation, or
atomic-write failure leaves the active config unchanged. After success, Admin
reloads the real Maintenance config and the runtime/control-readiness view. The
same EMS-owned migration service is used by Guided Upgrade—there is no separate
browser migration algorithm.

## Backup / restore

The **Backup / restore** path creates config, database, and system backups,
inspects what is inside a backup, previews a restore before anything is written,
and restores behind an automatic rollback backup. See
[Backup and restore](admin-backup-restore.md) for the full workflow.

Restore supports **config**, **database** and **bundled InfluxDB** backups. The
InfluxDB restore is orchestrated through the existing EMS CLI restore flow
(replace-style, preview and confirmation required); external InfluxDB is not
covered. See [Backup and restore](admin-backup-restore.md).

## Safety

Maintenance is conservative by default:

- **Backup before risky changes.** Config apply, guided upgrade, and restore all
  back up what they replace before writing.
- **No silent downgrade.** Guided upgrade only moves forward; restore never
  installs an older EMS image.
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
