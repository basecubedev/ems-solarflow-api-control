# User documentation

Everything a normal user needs: install, operate, diagnose. You do not need the
[technical reference](../technical/) or the
[developer docs](../developer/) for any of this.

## Start here

| Your situation | Go to |
| --- | --- |
| **New installation** | [Supported setups](supported-setups.md) → [Admin Console](admin-console.md) → [Guided Setup](admin/guided-setup.md) |
| **Existing installation** | [Maintenance](admin/maintenance.md) and the [EMS Dashboard guides](dashboard/index.md) |
| **Upgrade to a newer version** | [Guided Upgrade](admin/guided-upgrade.md) |
| **Something is wrong** | [Troubleshooting](troubleshooting.md) → [Diagnostics and recovery](admin/diagnostics-recovery.md) |
| **Report device compatibility** | [Supported setups](supported-setups.md#help-improve-compatibility) — positive reports are welcome too |

## Step-by-step guides

Screenshot-led walkthroughs. Each one states what you see, what to select, what
it changes, and what to do when the result differs.

### Admin Console — [all guides](admin/index.md)

| Guide | Use |
| --- | --- |
| [First start and login](admin/first-start.md) | Password setup, login, task selection, reconnect, logout |
| [Guided Setup](admin/guided-setup.md) | Install a new system, step 01 to 05 |
| [Guided Upgrade](admin/guided-upgrade.md) | Update an installed system safely |
| [Maintenance](admin/maintenance.md) | Inspect, change and repair an existing system |
| [Device management](admin/device-management.md) | Add, edit, disable, remove; switch connections |
| [MQTT connections](admin/mqtt.md) | Local MQTT, Zendure Cloud MQTT, brokers, write gates |
| [Backup and restore](admin/backup-restore.md) | Snapshots, previews, rollback |
| [Diagnostics and recovery](admin/diagnostics-recovery.md) | Evidence, support bundles, stuck workflows |

### EMS Dashboard — [all guides](dashboard/index.md)

| Guide | Use |
| --- | --- |
| [Overview and navigation](dashboard/overview.md) | Tiles, Live Flow, Rules, tabs |
| [Device cards](dashboard/devices.md) | Per-inverter state, offline and write-blocked reasons |
| [Energy and analytics](dashboard/energy.md) | Delivered energy, history, long-range analytics |
| [Control pipeline](dashboard/control.md) | Why EMS wrote that value |
| [Runtime settings](dashboard/runtime-settings.md) | Change live values safely |
| [Diagnostics and maintenance](dashboard/diagnostics.md) | Diagnose, logs, browser backups |

## Setup paths

Two user paths, both converging on the same standard `config/config.json`.

| Path | Audience | Start |
| --- | --- | --- |
| Admin Console | Most users | [admin-console.md](admin-console.md) |
| Docker Bootstrap | Shell-only Docker users | [docker-bootstrap.md](docker-bootstrap.md) |

Developer Setup is a source-checkout path for contributing, not a normal user
setup: [developer-setup.md](../developer/developer-setup.md).

## Reference and help

| Topic | Document |
| --- | --- |
| What the project is | [Project overview](project-overview.md) |
| Whether your hardware fits | [Supported setups](supported-setups.md) |
| Local API vs Local MQTT vs cloud | [Connection types](connection-types.md) |
| Before enabling hardware writes | [Safety](safety.md) |
| Standard config layout | [Config layout](config-layout.md) |
| Short answers | [FAQ](faq.md) |
| Common problems | [Troubleshooting](troubleshooting.md) |
| First validation after install | [First-run checklist](../first-run-checklist.md) |
| Daily commands | [Common commands](../common-commands.md) |

Full behavioural references for the Admin Console:
[Set up a new system](admin-setup.md) ·
[Maintenance](admin-maintenance.md) ·
[Backup / restore](admin-backup-restore.md).

The complete map of all three audiences is in [docs/README.md](../README.md).
