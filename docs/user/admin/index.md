# Admin Console — step-by-step guides

Screenshot-led walkthroughs for the local browser console (**EMS SolarFlow
Admin**) on port `8090`. It installs EMS, updates it, changes its devices and
settings, backs it up and repairs it. Each guide follows the same shape: what
the workflow is for, when to use it, what you need first, numbered steps with
what you see and what changes, then warnings and recovery.

New here? Read [what the Admin Console is](../admin-console.md) first — product
overview, install command, sign-in, networking and HTTPS.

![Admin Console Overview with the side menu, the verdict Everything is in order, the four tiles for EMS, the analytics database, control and the last backup, and the Control and safety and Versions cards](../../assets/screenshots/admin/admin-overview.png)

## Not sure where to start?

Open the console at its address and it starts on the
[assistant](assistant.md): it says what is still to do on this installation —
install EMS, add your devices, make a first backup — highlights the next step
with its button, and links to the page for anything else you came to do.

## Choose your guide

| You want to | Start here |
| --- | --- |
| Know where to start, or what is still open | [The assistant](assistant.md) |
| Open the console for the very first time, or sign in | [First start and sign-in](first-start.md) |
| Install EMS for the first time | [Guided setup](guided-setup.md) |
| Update an installed EMS to a newer version | [Guided Upgrade](guided-upgrade.md) |
| Read the state of an installation, change a setting, restart EMS | [Maintenance](maintenance.md) |
| Add, edit, disable or remove a device | [Device management](device-management.md) |
| Set up Local MQTT or Zendure Cloud MQTT | [MQTT connections](mqtt.md) |
| Save a snapshot, or roll one back | [Backup and restore](backup-restore.md) |
| Something is wrong and you need evidence | [Diagnostics and recovery](diagnostics-recovery.md) |
| Use the console in your language, in another colour scheme or on a phone | [Appearance, language and phone](appearance.md) |

## The pages

The side menu lists the pages in two groups, in this order:

```text
Overview · Assistant · Updates · Settings & devices · Services · Backups
System: Diagnostics · System
```

| Page | What it shows and does |
| --- | --- |
| **Overview** | *Your installation at a glance and what needs doing*: one verdict, what needs your attention, tiles for EMS, the analytics database, control and the last backup, **Control & safety** and **Versions** |
| **Assistant** | *What is still to do, and the way to what you came for* — see [The assistant](assistant.md) |
| **Updates** | *Versions, and the guided upgrade*: what runs now, the available versions, the update itself |
| **Settings & devices** | *Devices, features and the safety switches*, in the tabs **Features**, **Safety**, **Expert** and **Devices** |
| **Services** | *State and restart of EMS and the analytics database*: **Restart EMS now** and **Repair analytics** |
| **Backups** | *Save your settings, or put an older copy back*: create, download, bring back, restore, delete |
| **Diagnostics** | *Read-only checks of the installed EMS* |
| **System** | *Files, device telemetry and unfinished workflows*, and the way into the guided setup |

**Guided setup** has no place in the menu. The assistant, the Overview of an
empty host and the System page open it.

The top bar always names the page, says how the installation is — **All
good**, **A note**, **Needs a look**, **Needs attention**, **Not installed**,
**Checking…** or **Unknown state**, a pill that leads to the Overview — and
carries **Sign out** and the **Appearance** button. The foot of the side menu
holds **Open the dashboard**, **Hide menu text** and **Classic view**. On a
phone the menu becomes a bar at the bottom with four pages and **More**; see
[Appearance, language and phone](appearance.md#on-a-phone).

While a guided setup or an update holds the console, every other page shows a
banner that says so, with **Continue the setup** or **Show the update**.

## Admin and EMS move together

The Admin Console and EMS are **one paired System Build**. You pick a single
version; the console brings both sides to it.

- There is no separate "Admin version" choice, and no supported way to run EMS
  on one build while the Admin Console stays on another.
- During a [Guided Upgrade](guided-upgrade.md), updating the Admin Console is a
  step of its own. When it is replaced, the page loses contact for a moment,
  reconnects by itself and follows the update on.
- Choosing a version on the **Updates** page checks it at once and changes
  nothing; in the guided setup, **Verify the System Build** does the download
  and the check.

Background: [System-build pairing](../../technical/system-build-pairing.md).

## What changes what

Use this before you click something you are unsure about.

| Action | Reads | Writes settings | Recreates containers |
| --- | --- | --- | --- |
| Assistant, Overview, System, Diagnostics | Yes | No | No |
| **Search my network**, a scan | Yes | No | No |
| **Check changes** on Settings & devices | Yes | No | No |
| **Save and apply** on Settings & devices | Yes | Yes | No |
| **Restart EMS now** on Services | Yes | No | Yes |
| Guided setup, steps 01 to 03 | Yes | No | No |
| Guided setup, **Continue to deployment** and **04 Prepare** | Yes | Yes | No |
| Guided setup, **Start EMS** | Yes | No | Yes |
| Guided Upgrade | Yes | Yes | Yes |
| **Create backup** | Yes | No | No |
| Restore preview | Yes | No | No |
| **Restore now** (confirmed) | Yes | Yes | Possibly |

Every writing action shows what it will do first and asks for an explicit
confirmation. Saving settings, the guided upgrade and a restore back up what
they replace first, unless you switch that off.

## The deprecated Classic console

The previous design, **Classic**, still answers at
`http://<host>:8090/classic/` and is removed in a later release. It uses the
same password, the same server and the same checks, so switching changes
nothing on the host. **Classic view** at the foot of the side menu opens the
same page there; Classic's **New design** button comes back. Every workflow
runs in Modern; these guides describe Modern only.

The console's address opens Modern except in three cases: an address that
names a Classic page (such as `#setup` or `#maintenance-upgrade`, from a link
or bookmark made before Modern moved to the address), `?ui=classic` from such a
link, and a browser too old for Modern. Modern needs Chrome or Edge 114,
Firefox 125 or Safari 17 (iPadOS 17) or newer. An old bookmark of `/modern/`
opens the same page at the address.

## Deeper reference

The guides here are task-oriented. The full behavioural reference — every edge
case, refusal, ownership rule and recovery state — lives in:

- [Admin Console: Set up a new system](../admin-setup.md)
- [Admin Maintenance](../admin-maintenance.md)
- [Admin Console: Backup / restore](../admin-backup-restore.md)
- [Admin discovery (technical)](../../technical/admin-discovery.md)
- [Admin architecture (technical)](../../technical/admin-architecture.md)

## Related

- [EMS Dashboard guides](../dashboard/index.md) — the everyday operating UI.
- [Connection types](../connection-types.md) — Local API vs Local MQTT vs cloud.
- [Supported setups](../supported-setups.md) — whether your hardware fits.
- [Safety](../safety.md) — the pre-live checklist for hardware writes.
