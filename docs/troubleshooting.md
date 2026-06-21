# Troubleshooting

## Symptom Index

| Symptom | Start Here |
|---|---|
| Dashboard not reachable | [Dashboard Not Reachable](#dashboard-not-reachable) |
| Config placeholders / safe mode | [Config Still Contains Placeholders](#config-still-contains-placeholders) |
| Docker permission denied | [Docker Permission Denied](#docker-permission-denied) |
| Grid meter not reachable | [Grid Meter Not Reachable](#grid-meter-not-reachable) |
| Zendure device not reachable | [Zendure Device Not Reachable](#zendure-device-not-reachable) |
| Analytics / InfluxDB not reachable | [Analytics Or InfluxDB Not Reachable](#analytics-or-influxdb-not-reachable) |
| EMS not regulating | [EMS Not Regulating](#ems-not-regulating) |
| No power changes | [No Power Changes](#no-power-changes) |
| Opening an issue | [Opening a GitHub Issue](#opening-a-github-issue) |

Daily command sheet: [common-commands.md](common-commands.md).
First-run validation: [first-run-checklist.md](first-run-checklist.md).

## Start Here With Docker

For the recommended Docker setup, run these checks first:

```bash
docker compose ps
docker compose logs -f
docker compose exec ems python3 emsctl.py diagnose
```

Use hardware diagnostics only when you are ready to probe the configured local
meter and Zendure devices. It is read-only.

```bash
docker compose exec ems python3 emsctl.py diagnose --hardware
```

Common first fixes:

- Dashboard not reachable: verify `docker compose ps`, host port `8080`, and
  container logs.
- Config still contains placeholders: edit `config/config.json`, replace
  example values and `YOUR_SN`, restart, then run diagnose again.
- Docker socket permission denied: run Docker with `sudo` or complete the
  optional post-install steps in [install-docker.md](install-docker.md).
- Grid meter not reachable: check meter IP, meter type, local network routing,
  and `diagnose --hardware`.
- Zendure device not reachable: check device IP, serial number, local network
  routing, and `diagnose --hardware`.
- Analytics/InfluxDB not reachable: the dashboard still works without
  InfluxDB; see [influxdb.md](influxdb.md).
- Home Assistant not configured: Home Assistant is optional and not required
  for standalone EMS control.

The EMS uses structured logs:

```text
event=<name> key=value key=value
```

Use these logs to validate behavior during dry-run checks and live operation.
Change one setting at a time and run a short dry-run or bounded live test after
each change.

The template profile is intended for normal standalone live control after real
local values are configured and installation limits are reviewed. If required
placeholders are still present, EMS forces safe mode: control disabled, dry-run
enabled, and hardware writes blocked. Use `--dry-run` or set
`system.dry_run=true` when you want an explicit no-write validation run.

## Basic Checks

When you're not sure where to start, diagnostics can give a quick overview.

Docker:

```bash
docker compose exec ems python3 emsctl.py diagnose
docker compose exec ems python3 emsctl.py diagnose --deep
docker compose exec ems python3 emsctl.py diagnose --support-bundle
```

Native Python:

```bash
python3 emsctl.py diagnose
python3 emsctl.py diagnose --deep
python3 emsctl.py diagnose --json
python3 emsctl.py diagnose --support-bundle
```

Normal diagnose is local/read-only. `--deep` adds local operational checks such
as SQLite integrity, table summaries, recent configured log patterns, Docker
hints, and dashboard loopback reachability. `--hardware` is opt-in and performs
short-timeout read-only network probes only. `--support-bundle` writes a
redacted ZIP for GitHub/support requests. Exit code `1` means at least one
diagnostic error was found; warnings still exit `0`.

Use control diagnostics when EMS output looks surprising:

Docker:

```bash
docker compose exec ems python3 emsctl.py diagnose --control
docker compose exec ems python3 emsctl.py diagnose --control --sample-seconds 30
```

Native Python:

```bash
python3 emsctl.py diagnose --control
python3 emsctl.py diagnose --control --json
python3 emsctl.py diagnose --control --sample-seconds 30
```

The control report explains the local regulation path: current grid power,
filtered grid power, calculated target, final output, deadband state, control
enabled/disabled state, dry-run state, device allocation, SOC protection, and
likely root causes. Common findings:

- `Control disabled`: runtime control is off.
- `Dry run enabled`: EMS calculates targets but does not write hardware.
- `Deadband active`: filtered meter power is inside the configured threshold.
- `Grid meter signal appears noisy`: local samples oscillate or vary strongly.
- `Grid meter values are stale`: confirmed meter timestamps stopped updating or
  repeated read failures were reported. Repeated grid values alone do not mean
  the meter is stale.
- `Minimum SOC protection active`: one or more devices are protected by SOC.

Use control quality diagnostics when the system regulates but the result looks
poor over time:

Docker:

```bash
docker compose exec ems python3 emsctl.py diagnose --control-quality --sample-seconds 60
```

Native Python:

```bash
python3 emsctl.py diagnose --control-quality --sample-seconds 60
python3 emsctl.py diagnose --quality --json
```

Interpretation:

- Export/import quality measures how close the sampled grid power is to the
  zero-export target. Positive values are import; negative values are export.
- Export peaks show the strongest negative grid-power samples. Small short
  peaks can happen; large or long export periods mean EMS is not consistently
  holding zero export.
- The regulation quality score is a coarse diagnostic indicator. It penalizes
  average deviation from zero, export duration, export peak size, and large
  import peaks, then classifies the result as `excellent`, `good`,
  `acceptable`, `poor`, or `critical`.
- PV diagnostics can identify missing PV telemetry, likely system/device
  output limits, or PV that appears unused. It cannot prove a hardware fault on
  its own.
- SOC balancing warnings highlight large SOC spread, lower-SOC devices doing
  too much work, and devices protected by minimum SOC.

Inside Docker:

```bash
docker compose exec ems python3 emsctl.py diagnose
docker compose exec ems python3 emsctl.py diagnose --hardware
docker compose exec ems python3 emsctl.py diagnose --control
docker compose exec ems python3 emsctl.py diagnose --control-quality --sample-seconds 60
docker compose exec ems python3 emsctl.py diagnose --deep
docker compose exec ems python3 emsctl.py diagnose --support-bundle
```

## EMS Not Regulating

Docker:

```bash
docker compose exec ems python3 emsctl.py diagnose --control
```

Native Python:

```bash
python3 emsctl.py diagnose --control
```

Review `Control Snapshot`, `Decision Explanation`, `Write Path`, and `Likely
Causes`. Common findings include `Control disabled`, `Dry run enabled`,
`Deadband active`, device offline state, and minimum SOC protection.

Runtime-state file age is not used as proof of stale EMS control activity. If
no live control timestamp is available, the control staleness check is skipped
and shown as informational output.

## Export Peaks

Docker:

```bash
docker compose exec ems python3 emsctl.py diagnose --control-quality --sample-seconds 60
```

Native Python:

```bash
python3 emsctl.py diagnose --control-quality --sample-seconds 60
```

Review `Export / Import Quality` and `Regulation Quality`. Positive grid power
is import. Negative grid power is export. Short small export peaks can be
normal; large peaks or long export duration mean EMS is not consistently
holding the zero-export target.

## Uneven Battery Usage

Docker:

```bash
docker compose exec ems python3 emsctl.py diagnose --control-quality --sample-seconds 60
```

Native Python:

```bash
python3 emsctl.py diagnose --control-quality --sample-seconds 60
```

Review `SOC Balancing`. A warning can indicate high SOC spread, a lower-SOC
device contributing more than expected, a protected min-SOC device, or runtime
max-power limits that prevent balanced distribution.

## Hardware Communication Problems

Docker:

```bash
docker compose exec ems python3 emsctl.py diagnose --hardware
```

Native Python:

```bash
python3 emsctl.py diagnose --hardware
```

This runs short-timeout read-only checks for configured Shelly, EcoTracker, and
Zendure endpoints. It does not write to devices. Use it when telemetry is
missing, a grid meter cannot be parsed, or configured devices appear
unreachable.

## Docker Problems

Run from the directory with `docker-compose.yml`:

```bash
docker compose ps
docker compose logs -f
docker compose exec ems python3 emsctl.py diagnose
```

Stop following logs with `Ctrl+C`; the container keeps running.

For native Python, run:

```bash
python3 emsctl.py diagnose
```

Inside Docker:

```bash
docker compose exec ems python3 emsctl.py diagnose
```

Review container detection, `/app/config`, `/app/data`, UID/GID ownership,
runtime-state path location, and whether mutable files resolve below
`/app/data`.

### Dashboard Not Reachable

Check the container and published port:

```bash
docker compose ps
docker compose logs -f
```

The example Compose file publishes `8080:8080`. Open
`http://<host-ip>:8080`, or `http://127.0.0.1:8080` on the same host.

### Config Still Contains Placeholders

Edit the Docker config:

```bash
nano config/config.json
docker compose restart
docker compose exec ems python3 emsctl.py diagnose
```

Template placeholders force safe mode until replaced. Safe mode disables EMS
control, enables dry-run, and blocks hardware writes.

### Docker Permission Denied

If Docker reports permission denied for the daemon socket, run the command with
`sudo` or complete the optional Docker post-install group setup in
[install-docker.md](install-docker.md). Open a new login session after changing
group membership.

### Grid Meter Not Reachable

Check `grid_meter.type`, `grid_meter.ip`, local network routing, and firewall
rules. Then run:

```bash
docker compose exec ems python3 emsctl.py diagnose --hardware
```

### Zendure Device Not Reachable

Check each device `ip` and `sn` in `config/config.json`, verify the device is
on the same reachable network, and run:

```bash
docker compose exec ems python3 emsctl.py diagnose --hardware
```

### Analytics Or InfluxDB Not Reachable

InfluxDB is optional. The dashboard keeps using local SQLite history when
InfluxDB is disabled. For analytics setup and repair, see
[influxdb.md](influxdb.md).

## Backup / Restore Problems

See [Backup / Restore](cli.md#backup--restore) for the full workflow. Common
issues:

Backups are stored in `data/backups/` by default. Docker users see the same
folder on the host via the existing `./data:/app/data` mount.

- **"InfluxDB analytics is disabled. Nothing to back up."** — `backup create
  --type influxdb` only applies when `influxdb.enabled` is true. Enable bundled
  analytics first (`emsctl.py influx init`).
- **"External InfluxDB detected. … not supported for external mode."** —
  Automatic InfluxDB backup/restore covers **bundled** mode only. Use your
  external InfluxDB provider's backup tooling. External mode never blocks config
  or database backups.
- **"no InfluxDB token resolved"** — the backup needs a usable token. Set
  `influxdb.token`, export the `influxdb.token_env` variable, or run
  `python3 emsctl.py influx init` to generate `deploy/docker/influxdb.env`.
- **"failed to start bundled InfluxDB via docker compose"** — Docker/Compose is
  required. Check `docker compose ps` and see [Docker Problems](#docker-problems).
- **Encrypted archive won't open** — restoring or inspecting a `.tar.gz.enc`
  prompts for the password; a wrong password aborts cleanly. The password is
  never stored and cannot be recovered.
- **After an InfluxDB restore** the data is replaced (`influx restore --full`,
  bundled mode only — it restores org/buckets/users/tokens and history). Verify
  with `python3 emsctl.py influx status` and `python3 emsctl.py diagnose --deep`.
  If you created a rollback InfluxDB backup you can restore it the same way.

When **migrating or recovering a whole setup**, restore in this order so the
bundled token and config stay in sync:

1. Restore the config backup first (`ems-config-...`).
2. Verify the bundled InfluxDB secret/config files are present
   (`deploy/docker/influxdb.env`, `config.json`).
3. Restore the InfluxDB backup (`ems-influxdb-...`); create a rollback first.
4. Run `python3 emsctl.py influx status` and `python3 emsctl.py diagnose --deep`.

## Opening a GitHub Issue

Diagnostics are optional. You can open an issue even if you cannot run them or
are not sure which command applies to your setup.

If available, attach a support bundle to help identify issues faster.

Native Python:

```bash
python3 emsctl.py diagnose
python3 emsctl.py diagnose --support-bundle
```

Docker:

```bash
docker compose exec ems python3 emsctl.py diagnose
docker compose exec ems python3 emsctl.py diagnose --support-bundle
```

The support bundle is redacted and helps identify common installation,
runtime, hardware, control, and performance problems without exposing tokens,
passwords, serial numbers, dashboard auth files, or database contents.

Helpful links:

- [Getting help](../README.md#getting-help)
- [CLI documentation](cli.md)

## Optional Local Validation

If you want to collect more detail, these local checks can help.

Compile:

```bash
python3 -m py_compile ems-solarflow-api-control.py emsctl.py
```

Run self-tests:

```bash
python3 -B ems-solarflow-api-control.py --self-test
```

Run tests from the repository root:

```bash
python -m pytest -q
```

Direct `pytest -q` is also supported when `pytest.ini` is present. Both
commands require `pytest` in the active Python environment.

Run simulation:

```bash
python3 -B ems-solarflow-api-control.py --simulate --max-cycles 1
```

Run preflight against live devices without control writes:

```bash
python3 -B ems-solarflow-api-control.py --preflight --dry-run
```

Run one dry-run control cycle:

```bash
python3 -B ems-solarflow-api-control.py --dry-run --no-ha --once
```

Check required events:

```bash
python3 scripts/check_log_events.py /tmp/ems-sim.log \
  --require startup \
  --require target_calculation
```

## Config vs Runtime State

`config.json` contains static installation and safety settings.

The configured runtime-state file, `data/runtime-state.json` in new generated
configs, contains temporary runtime/operator values and can override some
defaults from `config.json` after the first start.

Important runtime fields:

```json
{
  "system": {
    "enabled": true,
    "max_total_power": 800,
    "loop_interval": 2,
    "min_output_limit": 35
  },
  "ha": {
    "enabled": false,
    "control_enabled": false
  },
  "devices": {
    "WR1": {
      "enabled": true,
      "max_power": 800,
      "offgrid_socket_mode": "off",
      "pv_priority_factor": 1.0
    }
  }
}
```

Runtime-editable values are limited to the fields shown above:

- system `enabled`, `max_total_power`, `loop_interval`, `min_output_limit`
- HA runtime `enabled` and `control_enabled`
- winter runtime `enabled`
- per-device `enabled`, `max_power`, `offgrid_socket_mode`, and
  `pv_priority_factor`

Other safety and tuning values are config-only and require editing
`config.json` plus a restart. Examples: `dry_run`, `allow_hardware_writes`,
`allow_state_reconciliation_writes`, `deadband`, `output_control`,
`redistribute_clamped_power`, `pv_kwp_weighting`,
`pv_charge_balance_enabled`, `pv_charge_balance_deadband_percent`,
`pv_charge_balance_full_bias_percent`, `pv_charge_balance_strength`,
`battery_kwh_weighting`, `soc_reconcile_interval`, HA URL/token, and device
IP/SN.

Reset runtime state:

```bash
rm data/runtime-state.json
```

Do this only while the EMS is stopped. On next start, EMS recreates the
configured runtime-state file from `config.json` defaults. Older root-level
`runtime-state.json` files from previous setups are no longer required after
switching to `data/runtime-state.json` and may be removed manually.

Relevant events:

```text
runtime_state_created
runtime_state_loaded
runtime_state_changed
runtime_state_saved
runtime_state_load_error
```

More detail: [runtime-state.md](runtime-state.md), [configuration.md](configuration.md).

## Docker Files Owned By Root

### Symptoms

- `docker compose exec ems id` shows `uid=0(root)`
- files in `config/` or `data/` are owned by `0:0`
- container logs show `EMS refuses to start as root.`
- startup reports that `/app/data` or `/app/config` is not writable by the
  non-root runtime user

### Fix host directory ownership

Stop the container, fix the bind-mounted host directories, and start again with
your host UID/GID:

```bash
docker compose down
mkdir -p config data
sudo chown -R "$(id -u):$(id -g)" config data
cat > .env <<EOF_ENV
PUID=$(id -u)
PGID=$(id -g)
EOF_ENV
docker compose up -d
```

Verify PID 1 inside the container:

```bash
docker compose exec ems sh -c 'cat /proc/1/status | grep -E "^(Name|Uid|Gid):"'
```

Verify host ownership:

```bash
find config data -maxdepth 1 -type f -printf '%u:%g %p\n'
```

Generated files should match your non-zero `id -u` and `id -g`, not `0:0`.

## No Power Changes

### Symptoms

- target calculation looks correct
- device output does not change
- logs show dry-run events only
- Home Assistant target sensors change, but hardware does not

### Check safety flags

```json
{
  "system": {
    "enabled": true,
    "dry_run": false,
    "simulation_mode": false,
    "allow_hardware_writes": true
  }
}
```

Also check whether the EMS was started with one of these flags:

```text
--dry-run
--simulate
--replay
--preflight
```

These modes do not perform normal live output control writes.

Expected dry-run event:

```text
event=dry_run_output_limit
```

Expected live-write event:

```text
event=write_output_limit
```

Other relevant events:

```text
control_disabled_skip_write
device_disabled_skip_write
offline_skip_write
deadband_skip_write
write_output_limit_error
```

More detail: [safety.md](safety.md), [configuration.md](configuration.md),
[runtime-state.md](runtime-state.md).

## Regulation Is Too Slow

### Symptoms

- house load changes quickly, but EMS target follows too late
- inverter output lags behind demand
- target rises or falls only in small steps
- Home Assistant helper changes take effect late

### Check these settings

```json
{
  "system": {
    "loop_interval": 2,
    "output_control": {
      "filter_enabled": true,
      "ema_alpha": 0.85,
      "ramp_enabled": true,
      "ramp_up_w_per_cycle": 600,
      "ramp_down_w_per_cycle": 700,
      "device_ramp_enabled": true,
      "device_ramp_up_w_per_cycle": 500,
      "device_ramp_down_w_per_cycle": 600,
      "large_import_bypass_w": 600,
      "large_export_bypass_w": 600,
      "bypass_ramp_multiplier": 1.5,
      "telemetry_max_age_seconds": 10,
      "stale_telemetry_ramp_factor": 0.5
    }
  }
}
```

### Tuning hints

| Symptom | Setting | Direction |
|---|---|---|
| Control loop reacts too late | `loop_interval` | lower carefully |
| Filter is too smooth | `ema_alpha` | increase |
| Total target rises too slowly | `ramp_up_w_per_cycle` | increase |
| Total target falls too slowly | `ramp_down_w_per_cycle` | increase |
| Per-device target changes too slowly | `device_ramp_*_w_per_cycle` | increase |
| Stale telemetry slows response | `telemetry_max_age_seconds` / `stale_telemetry_ramp_factor` | check telemetry freshness first |

Validate after tuning:

```bash
python3 -B ems-solarflow-api-control.py --dry-run --duration 120
```

Control-chain details: [control-logic.md](control-logic.md) and
[control-flow.md](control-flow.md).

Relevant events:

```text
output_control_state
output_control_ramp_limited
output_control_device_ramp_limited
output_control_stale_telemetry
output_control_bypass
output_control_sign_change_fast_response
target_calculation
```

## Regulation Oscillates Or Writes Too Often

### Symptoms

- target jumps up and down every cycle
- many repeated `write_output_limit` events
- actual output never settles
- grid import/export alternates quickly

### Check these settings

```json
{
  "system": {
    "deadband": 5,
    "output_control": {
      "load_deadband_w": 5,
      "target_deadband_w": 5,
      "filter_enabled": true,
      "median_window": 3,
      "ema_alpha": 0.85,
      "ramp_enabled": true
    }
  }
}
```

### Tuning hints

| Symptom | Setting | Direction |
|---|---|---|
| too many small target changes | `target_deadband_w` or `deadband` | increase |
| noisy load input | `load_deadband_w` | increase |
| output follows every spike | `ema_alpha` | decrease |
| target jumps too hard | `ramp_up_w_per_cycle` / `ramp_down_w_per_cycle` | decrease |
| devices fight each other | disable other controllers | check Zendure app, HEMS, HA automations |

Relevant events:

```text
output_control_deadband_hold
deadband_skip_write
write_output_limit
```

Control-chain details: [control-logic.md](control-logic.md) and
[control-flow.md](control-flow.md).

## Dashboard Values Do Not Add Up Exactly

### Symptoms

- `home`, target, output limit, and actual output differ in the same moment
- global target and per-device output do not match exactly
- off-grid socket mode looks like it should affect power totals

`home` is a calculated runtime/dashboard value. It is not the smoothed control
target. The EMS target can be filtered, ramped, clamped, and rate-limited before
an `outputLimit` write is attempted. The actual Zendure output can then lag or
remain lower because of device state, available PV/battery power, API timing, or
firmware behavior.

Off-grid socket mode is a mode/state value, not power. Do not add it to the
home-load, target, output-limit, or actual-output calculation.

More detail: [control-logic.md](control-logic.md),
[home-assistant.md](home-assistant.md), and [runtime-state.md](runtime-state.md).

## Device Is Online But Does Not Deliver Power

### Symptoms

- EMS writes a non-zero `outputLimit`
- `sensor.ems_solarflow_<device>_target` is above zero
- actual output remains zero or much lower
- battery does not discharge

### Check telemetry

Important fields and sensors:

```text
outputHomePower
outputLimit
solarInputPower
packInputPower
outputPackPower
electricLevel
socLimit
packState
acStatus
dcStatus
```

Home Assistant sensors:

```text
sensor.ems_solarflow_<device>_target
sensor.ems_solarflow_<device>_output
sensor.ems_solarflow_<device>_output_limit
sensor.ems_solarflow_<device>_soc_limit
sensor.ems_solarflow_<device>_pack_state
binary_sensor.<device>_ac_active
binary_sensor.<device>_dc_active
binary_sensor.<device>_available
```

### Common causes

| Cause | Check |
|---|---|
| battery is at or below `min_soc` | device SOC and configured `min_soc` |
| device reports no discharge capacity | `no_discharge_capacity` event |
| telemetry is stale | `binary_sensor.<device>_available` and `last_seen_age_s` |
| AC/DC path inactive | `acStatus`, `dcStatus` |
| device is runtime-disabled | `runtime-state.json` device `enabled=false` |
| target is clamped by max power | `max_total_power`, device `max_power` |

Relevant events:

```text
capability_detection
no_discharge_capacity
night_min_soc_idle_enter
night_min_soc_idle_hold_skip_write
night_min_soc_idle_park_write
min_output_limit_applied
```

Related docs: [configuration.md](configuration.md), [winter-mode.md](winter-mode.md),
[safety.md](safety.md).

## Output Stays At 0 W Or Device Does Not Wake Up

Some installations treat repeated `outputLimit=0` like a stop, idle, or sleep
state. `min_output_limit` can keep a small standby/wakeup target while EMS
control is enabled.

Check:

```json
{
  "system": {
    "min_output_limit": 35
  }
}
```

Runtime override:

```bash
python3 emsctl.py system min-output-limit 30
```

Use `0` to disable this behavior.

Relevant events:

```text
min_output_limit_applied
night_min_soc_idle_park_write
night_min_soc_idle_hold_skip_write
```

## Home Assistant Entities Missing

Home Assistant entities are created by REST state writes. They appear after the
EMS has published at least once.

After an HA restart, entities can temporarily appear as stale, unavailable, or
restored until the EMS publishes fresh states again.

Check:

- `ha.enabled=true` in `config.json`
- runtime `ha.enabled=true`
- valid HA URL and token
- not running with `--no-ha`
- not running simulation or replay
- Home Assistant is reachable from the EMS host

Relevant events:

```text
ha_publish_no_devices
ha_write_error
runtime_state_ha_write
```

## Home Assistant Helpers Are Ignored

### Symptoms

- changing HA max power has no effect
- changing HA enable switch has no effect
- changing HA loop interval has no effect
- HA sensors exist, but controls do not change EMS behavior

### Check static config

```json
{
  "ha": {
    "enabled": true,
    "control_enabled": true
  }
}
```

### Check runtime state

```json
{
  "ha": {
    "enabled": true,
    "control_enabled": true
  }
}
```

### Expected helpers

```text
input_boolean.ems_solarflow_ha_enabled
input_boolean.ems_solarflow_ha_control_enabled
input_boolean.ems_solarflow_enable
input_number.ems_solarflow_max_power
input_number.ems_solarflow_interval
input_number.ems_solarflow_min_output_limit
input_boolean.ems_solarflow_winter_enabled
```

Per-device helper example:

```text
input_boolean.ems_solarflow_wr1_enabled
input_number.ems_solarflow_wr1_max_power
input_select.ems_solarflow_wr1_offgrid_socket_mode
```

Relevant events:

```text
runtime_state_ha_sync
runtime_state_ha_read_error
ha_runtime_sync_failed
runtime_state_changed
```

If HA helper sync fails, EMS continues with the last valid local
`runtime-state.json` values.

HA helper values can update `runtime-state.json` only when static
`ha.enabled=true`, static `ha.control_enabled=true`, runtime `ha.enabled=true`,
and runtime `ha.control_enabled=true`. `--no-ha`, simulation, and replay disable
HA reads and writes for that run.

More detail: [home-assistant.md](home-assistant.md).

## Device Offline Or Stale Telemetry

### Symptoms

- device sensors stay visible but do not update
- writes are skipped for one device
- target allocation looks lower than expected
- HA `binary_sensor.<device>_available` is off

Relevant events:

```text
offline_skip_write
output_control_stale_telemetry
preflight_device_unreachable
```

The EMS may use cached state for calculation, but it suppresses writes to
devices without fresh telemetry.

Check:

- device IP address
- local network reachability
- device Wi-Fi quality
- `telemetry_max_age_seconds`
- HA `last_seen_age_s` attribute

More detail: [configuration.md](configuration.md) and
[home-assistant.md](home-assistant.md).

## Unexpected SOC Or Mode Changes

Check whether state reconciliation writes are enabled:

```json
{
  "system": {
    "allow_state_reconciliation_writes": true,
    "soc_reconcile_interval": 10,
    "reconcile_ac_mode_on_start": true,
    "reconcile_smart_mode": true
  }
}
```

Runtime output writes and persistent state reconciliation writes are separate
write paths. Output-limit writes require normal hardware writes to be enabled.
State reconciliation writes additionally require
`allow_state_reconciliation_writes=true`.

Relevant events:

```text
dry_run_soc_limits
write_soc_limits
soc_limits_unchanged
dry_run_device_modes
write_device_modes
device_modes_unchanged
dry_run_runtime_device_state_write
write_runtime_device_state
```

Set `allow_state_reconciliation_writes=false` while validating normal output
control only if you deliberately want a conservative troubleshooting variant.
The normal template profile keeps it enabled for the full regulation profile
after required placeholders are replaced and local limits are reviewed.

Related docs: [configuration.md](configuration.md), [winter-mode.md](winter-mode.md),
[safety.md](safety.md).

## Winter Mode

Relevant events:

```text
winter_mode_state
winter_ramp
winter_summer_reset
dry_run_winter_ac_charge_limit
write_winter_ac_charge_limit
```

If no winter event appears, check:

- `winter.enabled`
- runtime `winter.enabled`
- current month versus `winter.months`
- `soc_reconcile_interval`
- current hour versus `winter.adjust_hour`
- `allow_state_reconciliation_writes`

Winter logic runs through SOC reconciliation. It is not a per-cycle output
control mechanism.

## One Device Is Used Too Much Or Too Little

### Check device metadata

```json
{
  "name": "WR1",
  "max_power": 800,
  "pv_kwp": 2.0,
  "pv_priority_factor": 1.0,
  "battery_kwh": 1.92,
  "min_soc": 15,
  "max_soc": 100
}
```

### Tuning hints

| Field | Effect |
|---|---|
| `max_power` | hard per-device output limit |
| `pv_kwp` | PV-size weighting |
| `pv_priority_factor` | manual PV priority correction |
| `battery_kwh` | battery weighting |
| `min_soc` | lower discharge boundary |
| `max_soc` | upper SOC/headroom boundary |

Relevant events:

```text
balance_weight
pv_first_limit
pv_first_limited
pv_first_battery_topup
pv_first_battery_topup_unmet
target_calculation
```

Keep `pv_priority_factor=1.0` first. Adjust only after confirming realistic
`pv_kwp`, `battery_kwh`, and SOC limits.

Runtime tuning is available without editing `config.json`:

```bash
python3 emsctl.py device WR1 pv-priority-factor 1.3
python3 emsctl.py device WR2 pv-priority-factor 0.7
```

This changes PV-first weighting only. It does not create additional PV power
and does not override device power limits.

## Preflight Fails

Run:

```bash
python3 -B ems-solarflow-api-control.py --preflight --dry-run
```

Relevant events:

```text
preflight_start
preflight_ha_ok
preflight_shelly_ok
preflight_device_ok
preflight_device_unreachable
preflight_abort
preflight_failed
preflight_ok
```

Common causes:

| Event | Meaning |
|---|---|
| `preflight_device_unreachable` | Zendure device cannot be reached |
| `preflight_abort` | required preflight input is missing or invalid |
| `preflight_failed` | at least one required check failed |

## Safe Diagnostic Workflow

For deeper local validation, this order is a useful starting point:

```bash
python3 -B ems-solarflow-api-control.py --simulate --max-cycles 1
python3 -B ems-solarflow-api-control.py --preflight --dry-run
python3 -B ems-solarflow-api-control.py --dry-run --duration 120
python3 -B ems-solarflow-api-control.py --duration 60
```

Only run the final live test when these are true:

```text
dry_run=false
simulation_mode=false
allow_hardware_writes=true
runtime system.enabled=true
at least one runtime device enabled=true
```

## Issue Report Checklist

Share any of these details that are easy to provide:

```text
EMS version / commit:
Number of devices:
Device model(s):
Firmware version if known:
Home Assistant enabled:
HA control enabled:
dry_run:
allow_hardware_writes:
allow_state_reconciliation_writes:
loop_interval:
deadband:
max_total_power:
min_output_limit:
output_control settings changed from default: yes/no
```

If available, one complete EMS cycle with these events can help:

```text
startup
runtime_state_loaded
capability_detection
output_control_state
target_calculation
write_output_limit or dry_run_output_limit
```

Remove secrets before posting logs or config snippets:

```text
Home Assistant token
Zendure serial numbers
local IP addresses if desired
```
