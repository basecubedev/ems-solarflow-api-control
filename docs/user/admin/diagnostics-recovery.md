# Diagnostics, support bundles and recovery

## Purpose

Collect evidence when something is wrong, and get an interrupted or failed
workflow back to a clean state.

## When to use this workflow

- EMS is not controlling as expected.
- A device is offline, stale or read-only and you do not know why.
- A workflow failed or was interrupted.
- You want to open a bug report or a device compatibility report.

## Prerequisites

- For the Admin checks: the console logged in.
- For the CLI: shell access to the EMS host.

## Admin diagnostics (read-only)

![EMS diagnostics card expanded with execution mode and the Run diagnostics button](../../assets/screenshots/admin/admin-maintenance-diagnostics.png)

**Where:** Maintenance → **Manual configuration / existing system** → **EMS
diagnostics**.

**What you select:** **Run diagnostics**.

**What it changes:** nothing. These are *read-only EMS checks from the installed
system*, and the config upgrade is checked in **dry-run mode only** — no config
file is changed.

**Expected result:** an execution mode and a list of checks with outcomes.

**If it differs:** for deeper evidence, or to attach something to an issue, use
the CLI below.

## CLI diagnostics

All `diagnose` commands are **read-only**. None of them writes config, runtime
state or hardware.

| Command | What it checks |
| --- | --- |
| `python3 emsctl.py diagnose` | Install-level health: config, paths, clients, basic reachability |
| `python3 emsctl.py diagnose --deep` | The above plus deeper runtime inspection |
| `python3 emsctl.py diagnose --hardware` | Device reachability and reported capabilities |
| `python3 emsctl.py diagnose --control` | The control decision: measurements, target, allocation, write eligibility |
| `python3 emsctl.py diagnose --control-quality --sample-seconds 60` | Samples control quality over a window |
| `python3 emsctl.py diagnose --support-bundle` | Writes a bundle of the above for sharing |

Add `--json` for machine-readable output; it is a versioned public contract.

### Docker equivalents

If you installed with the Admin Console or Docker Bootstrap, run them inside the
EMS container:

```bash
docker compose exec ems python3 emsctl.py diagnose
docker compose exec ems python3 emsctl.py diagnose --deep
docker compose exec ems python3 emsctl.py diagnose --hardware
docker compose exec ems python3 emsctl.py diagnose --control
docker compose exec ems python3 emsctl.py diagnose --control-quality --sample-seconds 60
docker compose exec ems python3 emsctl.py diagnose --support-bundle
```

### Example output (sanitized)

```text
$ docker compose exec ems python3 emsctl.py diagnose
EMS SolarFlow diagnosis
  status: warning

  Configuration ............ ok    config/config.json, schema 3
  Grid meter ............... ok    shelly @ 192.168.1.50
  Devices .................. warn  INV_1 ok, INV_2 stale (age 214s)
  Write gates .............. ok    hardware=on mqtt_local=on mqtt_zendure=on
  Control loop ............. ok    interval 5s, last cycle 1.2s ago

  Root causes
    [warning] device_telemetry_stale
      INV_2 has not reported telemetry within the stale threshold.
      Next check: confirm the device still publishes to demo-broker.
```

Values above are illustrative. Your own output names your real devices — sanitize
it before sharing.

## Support bundles

```bash
docker compose exec ems python3 emsctl.py diagnose --support-bundle
```

The bundle has a fixed file list: `diagnosis.json`,
`control-diagnostics.json`, `control-quality.json`, `redacted-config.json`,
`runtime-state.json`, `bundle-metadata.json`, plus `.txt` variants.

**The config in the bundle is redacted**, but a bundle still describes your
installation. **Before attaching one to a public issue, check for and remove:**

- serial numbers
- API keys and tokens
- MQTT usernames and passwords
- backup passwords
- public IP addresses and personal hostnames
- exact private filesystem paths

If you are not sure a value is safe, replace it. Nobody needs your real serial to
diagnose a control problem.

## Reporting hardware compatibility

Use the
[Device compatibility report](https://github.com/basecubedev/ems-solarflow-api-control/issues/new?template=device_compatibility_report.yml)
template when a device does not behave as documented — **and when it works**.

**Positive reports are welcome and useful.** The maintainer does not own hardware
from every Zendure generation, so a working report is what lets a device be
marked validated for everyone else. Include the model, firmware version,
connection type and sanitized logs.

See [Supported setups](../supported-setups.md) for the current support tiers.

## Workflow recovery

![Workflow recovery card in Maintenance](../../assets/screenshots/admin/admin-maintenance-recovery.png)

**Where:** Maintenance → **Manual configuration / existing system** → **Workflow
recovery**.

**What you see:** the lifecycle verdict for a workflow that did not finish
cleanly, and only the actions that are actually allowed for it.

| Action | What it does | Offered for |
| --- | --- | --- |
| **Resume** | Retry the failed step from the point it is safe to retry from | Setup and upgrade |
| **Discard setup** | End the setup and remove the files it created | Setup |
| **Return to running build** | Put the Admin back on the build EMS is actually running | Upgrade only |
| **Cancel upgrade** | End the upgrade; running system, live config and known-good build stay as they are | Upgrade |
| **Retry cleanup** | Retry a cleanup that failed | When files remain and cleanup can succeed |

### What recovery never touches

Your live `config/config.json`, `data/`, runtime databases, backups, volumes, and
any container or file it cannot prove it owns.

### Two messages worth understanding

- **"Setup has stopped. Temporary files remain."** No new setup or upgrade can
  start until cleanup succeeds. **Retry cleanup** is offered. Your live config and
  running EMS were not changed by the failed cleanup. The message survives a
  reload and an Admin restart, and the retry always applies to the same setup — so
  you never have to work out which files belong to what, and you never need to
  delete JSON files by hand.
- **"Files were kept for review."** The console found a file it **cannot prove**
  belongs to this workflow — for example a generated config left by an older Admin
  version. It **keeps** it and says so instead of deleting it. **Retry cleanup** is
  not offered, because retrying would not change the answer. Nothing on your
  running system was changed; a maintainer has to look at the leftover file.

That distinction is deliberate. Deleting a file whose owner cannot be proven would
be worse than asking you to look.

### When a step ran out of time

A System Build change has a time limit. Passing it does not by itself mean work
has stopped, so the discard is not offered unconditionally:

- **Time passed, nothing running** → Discard is available. This is the normal
  recovery, including after an Admin restart.
- **Time passed, an operation is still running** → Discard stays disabled and says
  so. Wait; it becomes available immediately afterwards. This matters most during
  resource verification, which writes shared files — discarding mid-write would
  leave a half-written copy behind.
- **The console cannot tell whether anything is running** → Discard stays disabled
  and says so. It never guesses "probably finished".

## What happens in the background

- Diagnostics come from the EMS-owned diagnostics service, the same one the CLI
  and the dashboard use — one implementation, one answer.
- Workflow lifecycle is a durable server-side record. The browser never decides
  whether a workflow may be resumed, discarded or cleaned up.
- Cleanup requires exact ownership proof and canonical-path validation. Being
  inside a cleanup scope is not permission to delete.

## Warnings and common problems

| Symptom | Meaning | What to do |
| --- | --- | --- |
| Discard refused, naming an operation | That operation is still running | Wait, then discard. Nothing was half-discarded |
| *Files were kept for review* | Ownership could not be proven | Do not delete by hand; report it |
| Upgrade blocked by setup files | An unfinished setup exists | Discard the setup first |
| Diagnostics show stale device | Telemetry older than the threshold | Check the transport — [MQTT](mqtt.md) |
| Everything looks fine but control is wrong | Likely a second controller | Ensure nothing else writes `outputLimit` |

## Recovery or next steps

- Roll back a change → [Backup and restore](backup-restore.md)
- Retry an upgrade → [Guided Upgrade](guided-upgrade.md)
- Read the control decision live →
  [Dashboard control](../dashboard/control.md)
- Command-level detail →
  [Troubleshooting](../troubleshooting.md) ·
  [Troubleshooting reference](../../technical/troubleshooting-reference.md) ·
  [CLI reference](../../cli.md)
