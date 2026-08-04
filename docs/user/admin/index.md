# Admin Console — step-by-step guides

Screenshot-led walkthroughs for the local browser UI (**EMS SolarFlow Admin**).
Each guide follows the same shape: what the workflow is for, when to use it, what
you need first, numbered steps with what you see and what changes, then warnings
and recovery.

New here? Read [what the Admin Console is](../admin-console.md) first — product
overview, install command, login, networking and HTTPS.

![Admin Console start page recommending Maintenance, with Guided setup and Maintenance as the two choices](../../assets/screenshots/admin/admin-landing.png)

## Choose your path

| You want to | Start here |
| --- | --- |
| Install EMS for the first time | [Guided Setup](guided-setup.md) |
| Update an installed EMS to a newer version | [Guided Upgrade](guided-upgrade.md) |
| Inspect, change or repair an installed EMS | [Maintenance](maintenance.md) |
| Add, edit, disable or remove a device | [Device management](device-management.md) |
| Set up Local MQTT or Zendure Cloud MQTT | [MQTT connections](mqtt.md) |
| Save a snapshot, or roll one back | [Backup and restore](backup-restore.md) |
| Something is wrong and you need evidence | [Diagnostics and recovery](diagnostics-recovery.md) |
| Open the console for the very first time | [First start and login](first-start.md) |

## Setup or Maintenance?

The start page detects your install state and recommends one of exactly two
flows. It preselects, but it never acts on its own.

```text
Guided setup
→ builds or recreates an installation

Maintenance
→ manages an existing installation conservatively
```

- **Guided setup** writes a fresh `config/config.json` (backing up any existing
  one first) and prepares a deployment. Use it for a first install or a
  deliberate reinstall.
- **Maintenance** works with what is already installed: upgrades, config edits,
  diagnostics, backups, restore and recovery. It changes as little as possible
  and previews before it writes.

Choosing **Guided setup** on a host that already has an installation requires an
explicit confirmation first.

## Admin and EMS move together

In `v0.8.0-RC` the Admin Console and EMS are **one paired System Build**. You
pick a single build; the console aligns both sides to it.

- There is no separate "Admin version" choice, and no supported way to run EMS on
  one build while the Admin stays on another.
- During a [Guided Upgrade](guided-upgrade.md), Admin alignment is an automatic
  stage. If the Admin has to be replaced, the browser shows a full-screen
  reconnect overlay and the upgrade continues on its own afterwards.
- Selecting a build downloads nothing. **Verify System Build** is the step that
  downloads and proves the pair.

Background: [System-build pairing](../../technical/system-build-pairing.md).

## What changes what

Use this before you click something you are unsure about.

| Action | Reads | Writes config | Recreates containers |
| --- | --- | --- | --- |
| Start page, Maintenance overview | Yes | No | No |
| EMS diagnostics, Zendure MQTT telemetry | Yes | No | No |
| Discovery / device scan | Yes | No | No |
| Config preview | Yes | No | No |
| Config apply (Setup or Maintenance) | Yes | Yes | Optional |
| Guided Upgrade | Yes | Yes | Yes |
| Create backup | Yes | No | No |
| Restore preview | Yes | No | No |
| Restore (confirmed) | Yes | Yes | Possibly |

Every writing action previews the change and asks for explicit confirmation.
Config apply, Guided Upgrade and restore back up what they replace first.

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
