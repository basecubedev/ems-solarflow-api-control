# Configuration Guide

The EMS uses one static installation config. With the recommended Docker setup,
the file is:

```text
config/config.json
```

On first Docker start, the container creates that file from the versioned
template if it does not exist. Existing Docker configs are not overwritten.

New native/developer setups use the same standard layout:

```text
config/config.json
```

You can also pass an explicit path with `--config`.

For native/manual setups, create the config from the versioned template:

```bash
mkdir -p config data
cp config/config.template.json config/config.json
```

Older native checkouts may still use a root `config.json`. That legacy layout is
still read as a fallback, but new setups should use `config/config.json`. See
[Config Layout](../user/config-layout.md) for the full layout and legacy
migration states.

`config.json` and `config/config.json` are local and ignored by Git. Do not
commit real Home Assistant tokens, Zendure serial numbers, or local IP
addresses.

## Quick Start

1. Start Docker once so it creates `config/config.json`, or copy
   `config/config.template.json` to `config/config.json` for native Python.
2. Configure the real grid meter IP.
3. Configure one or more real Zendure device IPs and serial numbers.
4. Review power, SOC, battery, and PV limits for the installation.
5. Keep Home Assistant disabled unless you want HA integration.
6. Optionally run `docker compose exec ems python3 emsctl.py config init`
   (Docker) or `python3 emsctl.py config init` (native Python) for guided
   setup.
7. Optionally set `dry_run=true` for a no-write validation run.
8. Run diagnostics.
9. Monitor the first live run before unattended operation.

The template profile is intended for normal standalone live control after real
local values are configured and installation limits are reviewed:
`dry_run=false`, `allow_hardware_writes=true`, and
`allow_state_reconciliation_writes=true`.

If required placeholders are still present, EMS forces safe mode: control
disabled, dry-run enabled, and hardware writes blocked. This prevents an
untouched template from writing to hardware.

Docker:

```bash
docker compose exec ems python3 emsctl.py diagnose
```

Native Python:

```bash
python3 emsctl.py diagnose
```

Safe first checks:

```bash
python3 -B ems-solarflow-api-control.py --simulate --max-cycles 1
python3 -B ems-solarflow-api-control.py --preflight --dry-run
python3 -B ems-solarflow-api-control.py --dry-run --once
```

## Updating config.json After Upgrades

New releases may add config keys. EMS keeps older configs running by applying
conservative runtime fallback defaults in memory. Normal startup does not
rewrite `config.json`.

To review missing keys:

```bash
python3 emsctl.py config upgrade --dry-run
```

Inside Docker:

```bash
docker compose exec ems python3 emsctl.py config upgrade --dry-run
```

To update `config.json` interactively:

```bash
python3 emsctl.py config upgrade
```

Inside Docker:

```bash
docker compose exec ems python3 emsctl.py config upgrade
```

The upgrade command uses `config.template.json` as the source for missing
user-facing config defaults and explanatory `_comment*` keys. A dry-run also
reports how many existing template-managed comments differ from the current
template.

Before writing normal upgrade changes, EMS asks whether to create a normal
config backup with the existing backup tool. Backups are stored in
`data/backups/` by default.

After a successful interactive upgrade, EMS may offer to refresh explanatory
comments from the current template. This optional refresh updates only exact
template-managed `_comment*` paths. It does not change configuration values or
unknown user keys. If you accept the refresh and no upgrade backup was already
created, EMS creates a normal config backup before writing the comment changes.

For automation:

```bash
python3 emsctl.py config upgrade --yes --backup
python3 emsctl.py config upgrade --yes --no-backup
```

`--yes` remains non-interactive: it applies the normal upgrade according to the
selected backup policy, does not ask about comment refresh, and does not refresh
existing comments.

After upgrading:

Docker:

```bash
docker compose exec ems python3 emsctl.py diagnose --deep
```

Native Python:

```bash
python3 emsctl.py diagnose --deep
```

`config.template.json` contains the defaults users adopt into `config.json`.
Runtime fallback defaults are a separate safety net so old or incomplete
configs can still start safely. The `config_schema_version` value tracks config
compatibility, not the application version.

## Config vs Runtime-State

`config.json` contains static installation and safety settings:

- Home Assistant URL and token
- grid meter type and IP
- Zendure device IPs and serial numbers
- static device metadata
- safety flags
- output-control defaults
- winter defaults

`data/runtime-state.json` contains temporary mutable operator/runtime values in
new generated configs:

- EMS enabled state
- runtime max total power
- runtime loop interval
- runtime minimum output limit
- per-device enabled state
- per-device runtime max power
- per-device offgrid socket mode
- per-device runtime PV priority factor
- Home Assistant and winter runtime toggles

The EMS creates the runtime-state file automatically on first start. Deleting it
resets runtime values from `config.json` defaults. Do not maintain runtime
state as a second static config.

## Home Assistant Settings

`ha.enabled` enables Home Assistant publishing and optional helper reads. The
template default is `false` for standalone operation.

`ha.control_enabled` allows Home Assistant helpers to update runtime-state
values. The template default is `false`. It does not grant Zendure
hardware-write permission by itself.

`ha.url` is the Home Assistant base URL, for example:

```text
http://homeassistant.local:8123
```

`ha.token` is a Home Assistant long-lived access token.

Standalone mode:

```json
{
  "ha": {
    "enabled": false,
    "control_enabled": false,
    "url": "",
    "token": ""
  }
}
```

## System Settings

`system.enabled` is the default EMS enabled state used when runtime-state is
created.

`system.dry_run` calculates targets but blocks Zendure hardware writes. The
template value is `false` for normal standalone control after required
placeholders are replaced. Set it to `true` for a manual no-write validation
run.

`system.simulation_mode` runs without real hardware. Most users should keep it
`false` and use `--simulate` from the command line when needed.

`system.allow_hardware_writes` allows Zendure `/properties/write` calls when
`dry_run=false`. The template value is `true` so normal `outputLimit` control
works after local device and grid meter configuration. Required placeholders
still force safe mode and block writes.

`system.allow_state_reconciliation_writes` allows SOC and mode reconciliation
writes. The template value is `true` because this is part of the default
regulation profile after local device limits and SOC limits have been reviewed.
Required placeholders still force safe mode and block these writes.

`system.reconcile_ac_mode_on_start` keeps the legacy startup compatibility gate
enabled. Runtime AC mode intent is evaluated during the control loop, but
startup reconciliation writes happen only when the reported `acMode` is a known
value and differs from the desired runtime target. This prevents blind repeated
startup writes while still keeping normal output devices aligned to `acMode=2`;
explicit runtime output intent can still return a device to `acMode=2`.

Runtime AC charge power is not a static device config value. Set it through
runtime-state, for example with `python3 emsctl.py device WR1 ac-charge-power
200`. The controller applies it as `inputLimit` on the next loop only while the
device runtime role is `ac_input`; in `ac_output` mode the stored value is
ignored for hardware writes.

`system.reconcile_smart_mode` allows smart mode reconciliation and is required
for the intended Zendure runtime/RAM mode behavior.

`system.log_level` controls log verbosity. Common values are `info` and
`debug`. The default `info` level focuses on lifecycle events (startup,
dashboard/Influx start), actual hardware/state writes, and warnings/errors
(unreachable devices, write failures, invalid config, stale telemetry). Normal
per-cycle control-loop traces (`output_control_state`,
`output_control_deadband_hold`, `output_control_ramp_limited`, unchanged
reconciliation events, repeated `winter_mode_state`) are emitted at `debug`;
set `log_level` to `debug` to see them.

`system.max_total_power` is the default maximum combined EMS target in watts
(Admin label: **Maximum system output**). New configurations default to 800 W.

`system.max_device_power` is the default per-device maximum in watts.

`system.deadband` is the per-device write-suppression deadband in watts (Admin
label: **Device deadband**). EMS skips sending a new `outputLimit` to a device
while the new target is within this many watts of the value the device already
holds; it is distinct from `system.output_control.target_deadband_w`, which acts
on the total system target. New configurations default to 2 W.

`system.runtime_state_path` is the path to temporary mutable runtime state. The
default for new generated configs is `data/runtime-state.json`. Older
root-level `runtime-state.json` files from previous setups are no longer
required after switching to `data/runtime-state.json` and may be removed
manually.

`system.min_output_limit` is the default runtime minimum `outputLimit` while EMS
is enabled. It also defines the standby total used when positive house load is
present but no active online device has export capacity, and the standby/wakeup
value used by strict night/minSoc idle. Use `0` to disable this floor and the
idle parking behavior.

`system.loop_interval` is the control loop interval in seconds (Admin label:
**Loop interval**). New configurations default to 5 seconds. Keep that default
for Zendure Cloud MQTT control: a live SolarFlow 800 Pro 2 measurement observed
a 2.886 s median and 3.012 s p95 from MQTT publish to confirmed device
telemetry. A 3 s loop therefore has effectively no cloud-latency margin, whereas
5 s leaves useful headroom for normal jitter. This interval is the time between
EMS decisions; a load change can wait up to one additional interval before its
command is published, and filtering or ramp limits may intentionally spread the
final target over several cycles. See the
[live Cloud MQTT latency measurement](zendure-mqtt-power-control.md#live-cloud-latency-measurement)
for the measurement definition, complete statistics and limitations.

`system.redistribute_clamped_power` redistributes target power when one device
is clamped by limits.

`system.pv_kwp_weighting` weights PV-first distribution by configured PV size.

`system.pv_charge_balance_enabled` enables a PV-first charge balancing bias.
When total PV can cover the requested output, devices with higher SOC receive
more PV-first output weight so lower-SOC devices can keep more local PV for
charging.

In PV-first mode, devices that are full or charge-inhibited are prioritized for
AC output up to their available PV and device limits. This helps Zendure systems
use PV from a full battery directly in the house while batteries with charge
headroom keep more PV for charging. Export capability, max power, SOC limits,
and safety gates still apply.

`system.pv_charge_balance_deadband_percent` defines the SOC gap where the bias
starts. `system.pv_charge_balance_full_bias_percent` defines the gap where the
configured bias reaches full strength.

`system.pv_charge_balance_strength` controls the maximum PV-first charge
balancing bias. Values above `1.0` are clamped to `1.0`.

`system.battery_kwh_weighting` weights battery top-up by configured battery
capacity.

`system.soc_reconcile_interval` controls how often SOC/mode reconciliation is
checked, measured in EMS cycles. Use `0` to disable cyclic reconciliation.

Manual no-write validation flags:

```json
{
  "system": {
    "dry_run": true,
    "allow_hardware_writes": true,
    "allow_state_reconciliation_writes": true
  }
}
```

## Battery Full-Charge Assist

`battery_full_charge_assist.enabled` defaults to `true` for new configs; set it
to `false` in `config.json` to opt out. When enabled, EMS tracks battery-backed
devices in the core state database and can temporarily request `socSet=1000` so
firmware reaches its Max-SoC state within `interval_days`.

On first enable with an empty EMS state database, EMS assumes the battery was
recently full and schedules the first assist for `now + interval_days`. It does
not immediately start AC charging only because the feature was enabled. If the
feature is disabled and later enabled again, EMS treats that as a new tracking
start and seeds the schedule from the current time, unless an assist or restore
is already active.

`battery_full_charge_assist.assist_window_days` allows an early start when the
device is due soon and current SOC is at or above
`battery_full_charge_assist.assist_start_soc`. On the due day,
`battery_full_charge_assist.force_time` starts assist at or after the configured
local `HH:MM` time regardless of current SOC, unless firmware already reports
`socLimit == 1`.

`battery_full_charge_assist.enable_ac_charge_mode` controls whether active
assist requests AC input mode through the existing runtime AC mode intent
foundation. There is no separate assist-owned `acMode` writer.

`battery_full_charge_assist.state_database_path` defaults to
`data/ems_state.sqlite`. This is a core EMS database and is independent of the
dashboard database. Deleting it resets full-charge assist history and the
remembered enabled/disabled state.

More detail: [battery-full-charge-assist.md](../battery-full-charge-assist.md).

## Dashboard

`dashboard.enabled` starts the optional web dashboard alongside the EMS loop.
It is read-only by default. Runtime write mode is unavailable until a local
admin password is configured with `emsctl dashboard set-password`.

`dashboard.host` and `dashboard.port` define the listen address. The template
uses `0.0.0.0:8080`.

`dashboard.database_path` stores local SQLite history. Relative paths are
resolved from the project directory.

`dashboard.history_hours` controls automatic cleanup. Keep it short-term; the
dashboard API supports `1h`, `6h`, `12h`, and `24h` ranges.

`dashboard.write_interval_seconds` limits how often the EMS loop persists
dashboard telemetry. The default is `5`.

`dashboard.auth_file` stores dashboard password hash metadata. Missing file
means dashboard auth is not configured and write mode is unavailable.

`dashboard.ssl_enabled` switches the same dashboard port from HTTP to HTTPS.
When enabled, `dashboard.ssl_cert_file` and `dashboard.ssl_key_file` are used.
If either file is missing and `dashboard.ssl_auto_generate=true`, EMS creates a
self-signed LAN certificate and restricts the private-key file permissions.

`dashboard.session_idle_timeout_seconds` (default `1800`) is the login idle
timeout that slides on genuine user activity. `dashboard.session_absolute_max_seconds`
(default `43200`) is the hard cap on a session's lifetime measured from login.
For both, `0` disables the bound (an explicit "infinite" opt-in) and negative
values are rejected back to the default. The secure defaults are 30 min / 12 h;
disabling a bound weakens the "walk away → logged out" and stolen-cookie
protections.

`dashboard.log_buffer_lines` (default `5000`) sizes the in-memory log ring buffer
that backs the Logs tab. It is count-based, so quieter systems retain a longer
window; ~5000 lines covers well over 15 minutes at typical settings and costs a
few MB of RAM. `dashboard.log_redaction` (default `false`) masks secret-looking
values in served log lines; enable it for shared/remote deployments.

`dashboard.animation_mode` (default `normal`) controls how much the animated
energy-flow view animates. It is purely visual and never affects control, auth
or data. Treat it as a preference and accessibility setting rather than a
performance one: measured in Firefox on a GPU, `off` leaves the frame rate
unchanged and makes the dashboard's main-thread work 1.3 to 2.3 times more
expensive, because the remaining cost is a layout flush that a running animation
keeps small. Whether it still pays on a device weak enough for the saved
compositor work to dominate has not been measured.

- `normal` — full animated flow view (pipe motion, glow/blur filters).
- `reduced` — keeps state colours and basic flow indication but drops glow
  filters and slows pipe motion.
- `off` — no continuous pipe animations and no glow/blur filters.

The browser-level `prefers-reduced-motion` setting is always respected on top of
this mode. The value is exposed read-only at `/api/ui-config` and applied as a
root CSS class by the frontend.

Dashboard and Admin runtime writes are constrained by the configured system and
device power limits. For example, a runtime `max_total_power` update cannot
exceed the configured `system.max_total_power`, and a device runtime `max_power`
update cannot exceed that device's configured `max_power`. Admin maintenance
Apply mirrors the overlapping keys it changed into runtime-state through this
same whitelist; a mirrored value the runtime validator rejects is skipped with a
warning while the config write stands. Pure-config keys (ports, credentials,
`grid_meter.*`, `min_soc`, …) are not runtime-writable and still take effect only
after an EMS restart.

The dashboard also sends browser security headers, caps JSON request bodies,
and limits concurrent Server-Sent Events connections. These protections are for
local hardening; public exposure should use a VPN, reverse proxy, strong TLS,
and external access control.

## Energy Savings

`energy_savings.enabled` controls lightweight daily SQLite energy statistics.
The statistics use measured inverter AC output from the device telemetry, not
control targets or requested output limits.

`energy_savings.price_per_kwh` and `energy_savings.currency` are stored on each
daily row when that row is created. Historical savings are summed from the
stored daily values, so old days are not recalculated if the configured price
changes later. A price of `0.0` still tracks kWh and reports zero savings.

`energy_savings.max_sample_delta_seconds` protects the integration from large
false jumps after restarts or downtime. Intervals above this value are skipped.

`energy_savings.timezone` defines the calendar timezone used for daily
statistics and period lookups such as Today and Yesterday. It defaults to
`Europe/Berlin`.

## Output Control

`system.output_control` is advanced tuning for fast control loops. Most users
should keep the defaults. The ramp and target-deadband knobs below are surfaced
as primary Admin fields (they no longer require opening Advanced settings); the
remaining smoothing and bypass fields stay expert-level. All defaults listed
here apply to newly created configurations only — an existing `config.json`
keeps whatever it already sets.

`load_deadband_w` ignores very small load changes before target calculation
(default 5 W).

`target_deadband_w` holds the total system target when the newly desired total
is only slightly different (Admin label: **System deadband**). New
configurations default to 5 W.

`filter_enabled` enables load filtering.

`filter_method` selects the filter. The default is `median_ema`.

`median_window` is the number of load samples used for median filtering.

`ema_alpha` controls exponential smoothing. Higher values react faster.

`sign_change_fast_response_enabled` lets the median/EMA filter react faster
when `raw_load` has already crossed zero with meaningful magnitude but the
smoothed value still points in the old direction.

`sign_change_threshold_w` is the fixed watt threshold used to qualify a
sign-change mismatch. It is intentionally a fixed configurable value in V1, not
a percentage of system power.

`sign_change_filter_reset_factor` controls how strongly the smoothed value is
pulled toward `raw_load` during a sign-change mismatch. `1.0` resets directly to
`raw_load`. Lower values keep a softer transition.

`ramp_enabled` limits total target changes per cycle.

`ramp_up_w_per_cycle` limits how fast the total target can rise (Admin label:
**System ramp up**). New configurations default to 500 W per cycle.

`ramp_down_w_per_cycle` limits how fast the total target can fall (Admin label:
**System ramp down**). New configurations default to 300 W per cycle — the
slower down-ramp helps prevent undershoot when inverter output reacts more
slowly than the EMS control target.

`device_ramp_enabled` limits per-device target changes.

`device_ramp_up_w_per_cycle` limits per-device upward changes (Admin label:
**Device ramp up**). New configurations default to 400 W per cycle.

`device_ramp_down_w_per_cycle` limits per-device downward changes (Admin label:
**Device ramp down**). New configurations default to 200 W per cycle — the
reduced down-ramp avoids repeatedly lowering the target while the inverter is
still reacting to an earlier command.

`large_import_bypass_w` can bypass normal smoothing during large imports.

`large_export_bypass_w` can bypass normal smoothing during large exports.

`bypass_ramp_multiplier` increases ramp speed during bypass situations.

`telemetry_max_age_seconds` marks device telemetry as stale after this age.

`stale_telemetry_ramp_factor` reduces ramp speed when telemetry is stale.

## Winter Settings

Winter mode is optional but enabled by default for new configs; set
`winter.enabled` to `false` in `config.json` to opt out.

`winter.enabled` enables the static winter feature default. The runtime winter
toggle can still enable or disable winter behavior through runtime-state.

`winter.months` defines active winter months as numbers from `1` to `12`.

`winter.summer_min_soc` is the target `minSoc` outside winter mode.

`winter.winter_min_soc` is the desired winter `minSoc`.

`winter.ramp_step_percent` limits daily `minSoc` increases.

`winter.adjust_hour` is the hour used for daily winter adjustment.

`winter.ac_charge_power` is the conservative `inputLimit` used only during the
winter/SOC reconciliation context.

Winter logic runs as SOC reconciliation. It does not change normal output target
calculation and must not create per-cycle mode writes.

More detail: [winter-mode.md](../winter-mode.md).

## Device Settings

Each Zendure device entry defines static installation data:

```json
{
  "name": "INV_1",
  "ip": "192.168.1.100",
  "sn": "YOUR_SN",
  "smart_mode": 1,
  "max_power": 800,
  "pv_kwp": 1.0,
  "pv_priority_factor": 1.0,
  "battery_kwh": 1.0,
  "min_soc": 15,
  "max_soc": 100
}
```

`name` is the local device name used in logs, Home Assistant entities, and CLI
commands.

`ip` is the local Zendure device IP address.

`sn` is the Zendure device serial number.

`smart_mode=1` is runtime/RAM mode.

`max_power` is the default maximum output target for this device.

`pv_kwp` is the configured PV size used for PV-first weighting.

`pv_priority_factor` is the default PV-first priority for this device. It can
be overridden at runtime without editing `config.json` or restarting the EMS:

```bash
python3 emsctl.py device WR1 pv-priority-factor 1.3
```

`battery_kwh` is the configured battery capacity used for battery weighting.

`min_soc` and `max_soc` are static SOC boundaries in percent. Use `0` to leave
the corresponding value unmanaged.

`enabled` is optional and defaults to `true`. It means the same thing for every
transport: `"enabled": false` removes the device from the control loop, for a
local-API device exactly as for a Zendure MQTT control device. A non-boolean
value (for example the string `"false"`) is never trusted as enabled and also
removes the device, so a mistyped flag cannot silently keep an inverter under
EMS control. A config whose only devices are disabled has no control device and
does not start.

Static device metadata stays in `config.json`, not in runtime-state.
`pv_priority_factor` is an exception: the config value remains the installation
default, while runtime-state can override the active weighting.

## Grid Meter Settings

`grid_meter.type` selects the local household/grid power meter implementation.
Positive values mean grid import and negative values mean grid export, unless
the physical device is installed or reports values differently. Run
`emsctl config init` for guided setup.

### Supported grid meters

| Device / integration | `grid_meter.type` | Required fields | Notes |
| --- | --- | --- | --- |
| Shelly Pro/Plus Gen2/Gen3 | `shelly` | `ip` | Uses `/rpc/Shelly.GetStatus` |
| Shelly 3EM Gen1 | `shelly_3em_gen1` | `ip` | Uses `/status` |
| everHome EcoTracker | `ecotracker` | `ip` | Uses `/v1/json` |
| Zendure Grid Meter via local HTTP | `zendure_grid_meter_http` | `ip` (opt. `port`) | Internal/discovery generic type. Local REST `/properties/report`, reads `total_power`. Works for both D0 and Smart Meter 3CT. Manual setup offers the concrete 3CT/D0 local-API types below. |
| Zendure Smart Meter 3CT — Local API | `zendure_smartmeter_3ct_http` | `ip` (opt. `port`) | Local REST `/properties/report`, reads `total_power`. Shares the Zendure local-HTTP reader with the D0 local-API meter. |
| Zendure Smart Meter D0 — Local API | `zendure_smartmeter_d0_http` | `ip` (opt. `port`) | Local REST `/properties/report`, reads `total_power`. Same shared reader as the 3CT; a distinct type so a D0 is never stored as a 3CT. |
| Tasmota HTTP / SmartMeter | `tasmota_http` | `ip` or `url`, `power_path` | Uses `Status 10` JSON |
| Zendure Smart Meter D0 — Local MQTT | `zendure_smartmeter_d0` | `mqtt.topic` + (`mqtt.broker_ref` or `mqtt.host`) | Optional alternative; D0 preset, numeric payload |
| Generic MQTT grid meter | `mqtt` | `mqtt.host`, `mqtt.topic`, `mqtt.payload_format` | Numeric or JSON payload |

### Shelly Pro/Plus Gen2/Gen3 (`shelly`)

The EMS reads `http://<ip>/rpc/Shelly.GetStatus`. By default it uses the
aggregate `em:0.total_act_power` value and falls back to summing all `em1:*`
clamp values if the aggregate is unavailable.

The optional `grid_meter.channels` field selects individual clamps. Valid
values are `a`, `b`, `c`, `em1:0`, `em1:1`, and `em1:2`. Do not use `total` or
`sum` in `channels`.

Example: Shelly Pro/Plus with all clamps:

```json
{
  "grid_meter": {
    "type": "shelly",
    "ip": "192.0.2.50"
  }
}
```

Example: Shelly selected clamp C:

```json
{
  "grid_meter": {
    "type": "shelly",
    "ip": "192.0.2.50",
    "channels": ["c"]
  }
}
```

Multiple selections such as `["a", "c"]` sum only the selected clamps.

### Shelly 3EM Gen1 (`shelly_3em_gen1`)

The EMS reads `http://<ip>/status`. By default it prefers the top-level
`total_power` value and otherwise sums all numeric `emeters[].power` values.

The optional `grid_meter.channels` field selects phases or clamps. Valid values
are `a`, `b`, `c`, `0`, `1`, `2`, `emeter:0`, `emeter:1`, and `emeter:2`.
Phase letters are case-insensitive. When `channels` is configured, the EMS
ignores `total_power` and sums only the selected `emeters[].power` values:

```text
a / 0 / emeter:0 -> emeters[0].power
b / 1 / emeter:1 -> emeters[1].power
c / 2 / emeter:2 -> emeters[2].power
```

The EMS does not invert the sign automatically. Correct reversed clamp polarity
on the device.

Example: Shelly 3EM Gen1 with all phases:

```json
{
  "grid_meter": {
    "type": "shelly_3em_gen1",
    "ip": "192.0.2.51"
  }
}
```

Example: Shelly 3EM Gen1 selected phases A and C:

```json
{
  "grid_meter": {
    "type": "shelly_3em_gen1",
    "ip": "192.0.2.51",
    "channels": ["a", "c"]
  }
}
```

### everHome EcoTracker (`ecotracker`)

The EMS reads `http://<ip>/v1/json` and uses the flat JSON `power` field.
Phase values and energy counters are not required for EMS control.

Example: everHome EcoTracker:

```json
{
  "grid_meter": {
    "type": "ecotracker",
    "ip": "192.0.2.60"
  }
}
```

### Zendure Grid Meter via local HTTP (`zendure_grid_meter_http`)

The **recommended** Zendure grid-meter connection. The EMS reads
`http://<ip>:<port>/properties/report` (port defaults to 80 and the discovered
port is preserved) and uses the flat JSON `total_power` field. This is a local
REST endpoint that needs no MQTT broker and no app MQTT configuration; current
known firmware exposes it without authentication.

Both a Zendure D0 and a Smart Meter 3CT serve the same flat report, so numeric
`total_power` alone makes the meter usable and its sign is used as reported. The
per-phase fields (`a_aprt_power`, `b_aprt_power`, `c_aprt_power`) are **not** used
to identify the model — a D0 reports the same fields — and `meterType` /
`protocolType` are **not** treated as proven D0 identifiers.

The older `zendure_smartmeter_3ct_http` type is accepted as a backward-compatible
alias for the same client, so existing configs keep working.

Example: Zendure grid meter via local HTTP:

```json
{
  "grid_meter": {
    "type": "zendure_grid_meter_http",
    "ip": "192.168.1.50"
  }
}
```

### Zendure Smart Meter 3CT / D0 — Local API (`zendure_smartmeter_3ct_http`, `zendure_smartmeter_d0_http`)

When you know the model, use the concrete local-API type instead of the generic
one. Both read the same flat `total_power` from `/properties/report` through the
**shared** Zendure local-HTTP reader (there is no second HTTP client); only the
config type and the user-facing label differ, so a manually added D0 is never
stored or shown as a 3CT. Each needs only `grid_meter.ip` (the discovered port
is preserved). The D0 stays read-only regardless of transport, and the sign is
used as reported (positive import, negative export).

```json
{
  "grid_meter": {
    "type": "zendure_smartmeter_d0_http",
    "ip": "192.168.1.60"
  }
}
```

The Local API (`zendure_smartmeter_d0_http`) and Local MQTT
(`zendure_smartmeter_d0`) D0 entries are strictly separate: the HTTP entry
carries only `ip`/`port`, the MQTT entry carries only the `grid_meter.mqtt`
block. Switching a meter between them drops the fields that do not belong to the
selected transport.

### Tasmota HTTP / SmartMeter (`tasmota_http`)

With `grid_meter.ip`, the default endpoint is
`http://<ip>/cm?cmnd=Status%2010`. Alternatively, set `grid_meter.url` to an
explicit endpoint. `grid_meter.power_path` is always required and must contain
the dot-separated path to the current power value. The keys depend on the
active Tasmota meter script, so the EMS does not guess them.

Example: Tasmota SML using the default endpoint:

```json
{
  "grid_meter": {
    "type": "tasmota_http",
    "ip": "192.0.2.70",
    "power_path": "StatusSNS.SML.Power_curr"
  }
}
```

Example: Tasmota with an explicit URL and OBIS-style key:

```json
{
  "grid_meter": {
    "type": "tasmota_http",
    "url": "http://192.0.2.70/cm?cmnd=Status%2010",
    "power_path": "StatusSNS.SM.16_7_0"
  }
}
```

### Zendure SmartMeter D0 (MQTT) (`zendure_smartmeter_d0`)

An **optional alternative** to local HTTP, for a D0 already publishing to a
broker. The EMS subscribes to an existing MQTT broker; it does not run a broker,
never publishes, and never writes to the D0. The default topic is
`Zendure/sensor/<serial>/totalPower`, and the payload is numeric watts:

```text
positive = grid import
negative = grid export
```

Configure the topic (and `payload_format`, `max_age_seconds`) under
`grid_meter.mqtt`. For the connection, prefer a named broker profile with
`broker_ref` so host, port, TLS and credentials live once in
`zendure_mqtt.brokers` and the broker password is never duplicated into the
`grid_meter` block:

```json
{
  "grid_meter": {
    "type": "zendure_smartmeter_d0",
    "mqtt": {
      "broker_ref": "local_mqtt",
      "topic": "Zendure/sensor/D0DEMO123456/totalPower",
      "payload_format": "number",
      "max_age_seconds": 15
    }
  },
  "zendure_mqtt": {
    "brokers": {
      "local_mqtt": {
        "enabled": true,
        "source": "local_mqtt",
        "host": "192.0.2.10",
        "port": 1883,
        "tls": false,
        "username": "YOUR_MQTT_USER",
        "password": "YOUR_MQTT_PASSWORD"
      }
    }
  }
}
```

Rules:

- The D0 topic must be exactly `Zendure/sensor/<serial>/totalPower` — four
  segments, a non-empty serial, and no MQTT wildcards (`+`/`#`). Extra path
  segments, the `number` write channel, a foreign/cloud prefix, or a
  leading/trailing separator are rejected. The canonical topic is generated from
  the serial, so entering the serial in guided setup is enough.
- The referenced broker profile must **exist**, be **enabled**, use
  `source: local_mqtt`, and carry a valid **host** and **port**. Admin preview
  validates the broker through the same EMS Core resolver used at startup, so a
  preview that is `ready` will not be rejected at runtime. Zendure Cloud MQTT D0
  grid meters are not supported (cloud topic prefixes carry the secret account
  app key).
- The MQTT port must be an integer in the range **1–65535**. An explicit invalid
  port (`0`, `-1`, `70000`, `"broken"`, a boolean, …) is **rejected**, never
  silently replaced or clamped. When the port is omitted, the protocol default
  applies (`1883` plain, `8883` for TLS).
- TLS is supported via the broker profile (`"tls": true`; use
  `"tls_insecure": true` only when you explicitly accept unverified
  certificates — it skips certificate-chain *and* hostname verification, which
  is required for brokers with self-signed certificates such as the Zendure
  cloud broker). The grid-meter MQTT client applies TLS before connecting and
  never publishes.
- Several local brokers stay separate: each discovered broker keeps its own
  profile and `broker_ref`, so credentials and TLS settings never cross broker
  boundaries.
- The legacy inline form (host/port/username/password directly under
  `grid_meter.mqtt`, no `broker_ref`) still works. Do **not** combine `broker_ref`
  with inline connection fields — that is rejected as ambiguous.

Live D0 validation currently depends on external tester feedback.

Example: legacy inline broker configuration:

```json
{
  "grid_meter": {
    "type": "zendure_smartmeter_d0",
    "mqtt": {
      "host": "192.0.2.10",
      "port": 1883,
      "username": "YOUR_MQTT_USER",
      "password": "YOUR_MQTT_PASSWORD",
      "topic": "Zendure/sensor/D0DEMO123456/totalPower",
      "payload_format": "number",
      "max_age_seconds": 15
    }
  }
}
```

### Generic MQTT grid meter (`mqtt`)

Use this type for custom MQTT-based meters. It uses the same MQTT client and
backend as the D0 preset. Set `grid_meter.mqtt.payload_format` to `number` for
a plain numeric payload, or to `json` and provide
`grid_meter.mqtt.value_path` for a JSON payload.

MQTT meters cache the latest parsed value. The control loop does not wait for a
message. If no value has arrived, or its age exceeds
`grid_meter.mqtt.max_age_seconds`, the meter is treated as stale and the last
cached value is used.

Example: Generic MQTT JSON payload:

```json
{
  "grid_meter": {
    "type": "mqtt",
    "mqtt": {
      "host": "192.0.2.10",
      "port": 1883,
      "topic": "meter/grid",
      "payload_format": "json",
      "value_path": "power.total",
      "max_age_seconds": 15
    }
  }
}
```

For a numeric payload, use `"payload_format": "number"` and omit `value_path`.

### Legacy config compatibility

Legacy configs with only `shelly.ip` still work. New configs should use
`grid_meter`.

If your meter returns a different JSON structure, please open a GitHub issue
and include the meter type, relevant config, logs, and an anonymized example
payload if possible.

## Zendure MQTT Telemetry and Control

Telemetry from one or more MQTT brokers. The feature is **always on** and has
no enable toggle: a broker runs as soon as its host is configured (a Zendure
cloud broker additionally needs a stored runtime credential), and without any
broker the runtime is simply inactive — never a config error. A legacy
top-level `zendure_mqtt.enabled` key in existing configs is ignored;
per-profile `enabled` flags under `brokers` still apply. Telemetry is read-only
by default: telemetry-only devices (`capabilities.write_output_limit=false`)
never publish and never write `outputLimit`. Publishing happens for control
devices — those with `capabilities.write_output_limit=true`, a supported write
method and an enabled write gate — see
[Zendure MQTT output control](#zendure-mqtt-output-control)
below. A discovered device is controllable only where an **exact supported
hardware model** resolves to a verified write method, its **broker profile
source** is a proven carrier for that route, and the write address is complete;
a topic family or hardware generation alone never authorizes writes, and
unknown or conflicting model evidence stays telemetry-only.

Each MQTT device names exactly one broker profile via `mqtt.broker_ref`. There
is no fallback and no implicit runtime priority: a device assigned to a broker is
only ever satisfied by that broker's telemetry. Define profiles under
`zendure_mqtt.brokers` so mixed installs are explicit:

```json
{
  "zendure_mqtt": {
    "stale_after_seconds": 60,
    "brokers": {
      "zendure_cloud": {
        "enabled": true,
        "source": "zendure_cloud_mqtt",
        "host": "mqtteu.zen-iot.com",
        "port": 8883,
        "tls": true,
        "tls_insecure": true,
        "credentials_ref": "zendure-cloud"
      },
      "local_mqtt": {
        "enabled": true,
        "source": "local_mqtt",
        "host": "192.168.20.10",
        "port": 1883
      }
    }
  },
  "devices": [
    {
      "name": "INV_1",
      "type": "zendure_mqtt",
      "enabled": true,
      "serial_number": "…",
      "mqtt": {
        "broker_ref": "zendure_cloud",
        "topic_family": "zensdk_ha_scalar",
        "device_id": "…"
      },
      "capabilities": { "read_power": true, "read_soc": true, "write_output_limit": false }
    }
  ]
}
```

The Admin Fresh Install and Maintenance flows assign new inverters compact
operational names (`INV_1`, `INV_2`, …) across Local API and MQTT transports.
That `name` is the stable key used by runtime state, logs, dashboard devices,
and the EMS Flowchart. Model, address, serial/device ID, transport, maximum
power, and hardware generation remain separate metadata. Existing config names
are not migrated automatically, and the proposed compact name can be edited
before applying.

Notes:

- Broker credentials (`username`, `password`, `app_key`, tokens) never live in
  `config.json`. A profile carries a non-secret `credentials_ref` and the secret
  is resolved from the external secret store at runtime. Credentials are never
  returned through status, diagnostics or the Admin UI. This holds for local
  discovery, manual local-broker setup, the Zendure cloud runtime record and
  Maintenance alike.
- A Zendure **cloud** profile needs a Core-resolvable runtime credential record
  (the encrypted `mqtt-<credentials_ref>.json` under `config/secrets/`) holding
  the complete four-field contract — MQTT `username`, `password`, `client_id`
  and `app_key`. The runtime builds the cloud connection from `client_id` and
  its subscriptions from `app_key`, so a record missing or blanking any of the
  four fields is invalid. Setup and Maintenance apply provision this record
  automatically when the config references it: the credentials are fetched
  from the Zendure deviceList (a response lacking any required field is
  rejected), persisted atomically, verified to resolve back to the complete
  contract through the Core resolver, and rolled back from a raw pre-change
  byte snapshot (a rotated — even malformed — record is restored byte for
  byte, a new one removed) if a later apply step fails. Existing records are
  validated through the Core resolver at apply time — file existence alone is
  never trusted. A valid record is reused with no network call by both Setup
  and Maintenance (they share one staging service); a record that no longer
  decrypts or is incomplete is reprovisioned when the Zendure API key is
  saved, and otherwise blocks the apply with the stable
  `credential_provisioning_failed` code (a partly failed rollback additionally
  reports a high-severity `credential_rollback` section naming the affected
  refs, never secret values). Local broker records follow the same contract
  against the discovery credential pool, including in-place rotation when the
  discovered credentials changed; a local `credentials_ref` stands for real
  authentication and must resolve to a complete username/password pair — an
  empty record never downgrades the broker to anonymous access (anonymous
  brokers simply carry no `credentials_ref`). Credential staging and the
  config write run as one serialized apply transaction shared by Setup and
  Maintenance. Should the record be missing at runtime anyway, the broker
  reports `broker_auth_missing` and is never connected (it will not dial the
  cloud broker anonymously).
- An enabled `zendure_mqtt` device must reference a **usable** broker profile:
  the profile must exist, be enabled, carry a host/port and a supported
  `source`, and a cloud profile must have an external credential reference.
  Otherwise config validation blocks with a sanitized code
  (`zendure_mqtt_broker_ref_unknown` / `_disabled` / `_incomplete` /
  `zendure_mqtt_broker_auth_missing`) that never leaks serials, hosts or secrets.
- The broker profile is authoritative for the transport `source` (local vs
  Zendure cloud), which also selects the MQTT write gate. Omit `mqtt.source` on
  the device; a device `source` that contradicts its broker profile is rejected
  (`mqtt_source_mismatch`) so device config can never pick a different gate.
- Disabled broker profiles may exist as long as no enabled device references them.
- `capabilities.write_output_limit=true` opts a device in to **MQTT output
  control** (see below). Without it, the device stays telemetry-only. An enabled
  device whose pinned hardware profile does resolve to a supported write method
  but stays telemetry-only is reported by `diagnose` as
  `zendure_mqtt_control_ready_but_telemetry_only`, so an unnoticed downgrade
  cannot hide as a normal telemetry-only entry.
- Backward compatible: an old single-broker block (top-level `host`/`port` with
  no `brokers`) maps to an implicit `default` broker, and devices without a
  `mqtt.broker_ref` use it. API-only devices need no migration.

### Zendure MQTT output control

A `zendure_mqtt` device with `capabilities.write_output_limit=true` is a
**control** device: it joins the same control loop, target calculation,
distribution and safety gates as API devices. The EMS controller stays the
source of truth for demand, distribution and write decisions; MQTT is a
first-class control transport that builds the write topic and payload.

Output control is enabled per device by capability: it is available where the
pinned hardware profile resolves to a **verified write method** on the device's
**broker source**, decided by the shared helper
`ems.zendure_mqtt.capability.mqtt_output_control_capability`. Admin Setup,
Maintenance and manual entry all create a controllable device for a supported
model without hand-editing `config.json`.

`write_output_limit` is a **capability, not an operator preference**. Admin
derives it from four independent axes — the pinned model, an implemented write
route for that model, a broker source that carries that route, and a complete
write address (`mqtt.product_key` plus `mqtt.device_id`) — and shows it
read-only, so a control-capable inverter is
controlled whenever it is enabled, the same rule a Local API device follows
(which carries no such key at all). Whether a device participates at all is its
`enabled` flag, and that is the only activation authority: a device that cannot
control output is telemetry-only by capability and stays active. Hand-editing
`write_output_limit=false` on a control-capable device remains valid config and
keeps the device telemetry-only, but Admin will re-derive it to `true` when the
Maintenance draft is loaded, where it appears in the preview diff.

Local API, Local MQTT and Zendure Cloud MQTT are **alternative control
transports of the same logical device**. Switching between them preserves the
device's identity, `enabled` state, limits, SoC settings and allocation
parameters; `enabled` is never a capability decision. Whether the new connection
can *control* the device is, however, evaluated per transport: the broker source
is one of the capability axes, so a device moved to a broker whose write route
is unverified stays enabled and becomes a telemetry source, with the reason
shown on its card. See
[Model-Aware Zendure MQTT Power Control](zendure-mqtt-power-control.md#broker-source)
for the source matrix and the machine-readable reasons
(`broker_source_write_unverified`, `broker_source_unknown`).

**Scope of MQTT control.** Output-limit control is supported where the resolved
hardware profile carries an implemented write method: `zensdk_properties_write`
(ZenSDK `properties/write`) or the `legacy_hub_device_automation` /
`legacy_object_device_automation` `function/invoke` automation commands.
Full API state reconciliation — Smart Mode, AC Mode, SoC setting, winter/full-
charge assist — is **API-only** and is not available over MQTT
(`supports_state_reconciliation=False` for MQTT control devices). Some older
Zendure generations and topic families have not been validated on the
maintainer's own hardware; please report anonymized MQTT traces and results.

Each control device must resolve to an explicit, supported **write method**
before it can publish. The pinned `hardware_profile` selects it
(registry: `ems/mqtt_control/zendure_profiles.py`); a topic family never does:

| Hardware profile | Write method | Notes |
| --- | --- | --- |
| SolarFlow 800 / 800 Plus / 800 Pro / 800 Pro 2 / 1600 AC+ / 2400 AC / 2400 AC+ / 2400 Pro / 4000 AC+ | `zensdk_properties_write` | Publishes `{deviceId, messageId, timestamp, properties:{outputLimit}}` to `iot/<productKey>/<deviceId>/properties/write`. Needs `mqtt.product_key`. |
| Hyper 2000 / AIO 2400 | `legacy_object_device_automation` | Publishes a `deviceAutomation` `function/invoke` command to `iot/<productKey>/<deviceId>/function/invoke`; acknowledged on `function/invoke/reply`. Needs `mqtt.product_key`. |
| Hub 1200 / Hub 2000 | `legacy_hub_device_automation` | Same `function/invoke` topic with a scalar watt value; acknowledged on `function/invoke/reply`. Needs `mqtt.product_key`. |
| ACE 1500 / SuperBase V4600 / SuperBase V6400 | `telemetry_only` — **read-only** | Never publishes. |
| none pinned | none — **read-only** | A topic family or hardware generation alone never selects a write method; only the explicit escape hatch below can. |
| none pinned, with explicit topic | `custom_properties_write` | Explicit advanced escape hatch: `mqtt.write_protocol` set to `custom_properties_write` plus an explicit valid `mqtt.write_topic`; publishes the same properties payload to that topic. |

**Telemetry family and write family are separate.** `mqtt.topic_family` names
how a device's *reports* are parsed (`zensdk_ha_scalar`,
`zendure_cloud_scalar`, `legacy_zendure_json`, `legacy_zendure_json_alt`). Every
built-in write method publishes to `iot/<productKey>/<deviceId>/…` regardless of
it, so the observed telemetry family never decides whether a device is
controllable. What a scalar family does *not* carry is a product key — its
topics have no such segment — so a device discovered only through scalar
telemetry has an incomplete write route until the product key is known from the
cloud device list, an existing config or manual entry. That is reported as
`write_target_missing`, not as a transport problem. See
`docs/technical/zendure-mqtt-power-control.md` for the full capability model.

Pin `hardware_profile` to a supported model to make a device controllable;
`mqtt.write_protocol` accepts only the explicit `custom_properties_write` escape
hatch, never a built-in write method name. An enabled control device that does
not resolve to a supported write method (or is otherwise unaddressable) fails
config validation, and startup aborts rather than silently controlling fewer
inverters.

MQTT control writes are gated separately from API writes by transport:

| `system` flag | Default | Gates |
| --- | --- | --- |
| `allow_mqtt_local_control_writes` | `true` | devices on a `local_mqtt` broker |
| `allow_mqtt_zendure_control_writes` | `true` | devices on a `zendure_cloud_mqtt` broker |

All gates default on in the template; switch a gate off for read-only
validation of its transport. Configs missing the `system` keys resolve to the
same release defaults (all gates on) at load time, while the simulation/replay
safe config and template placeholder safety force every gate off until real
values are configured.

Both still require the shared precondition (`dry_run=false`,
`simulation_mode=false`, not replay). Control devices are output-only: they are
excluded from the read-only telemetry runtime and from every state
reconciliation writer. Their telemetry is subject to freshness: a stale or
missing snapshot (broker disconnect / stalled updates) is treated as an
unavailable read, so the controller never acts on disconnected devices.

Mock and in-process broker tests verify EMS integration and broker isolation.
They do **not** prove that every Zendure firmware accepts the generated write
command. Please open a GitHub issue for both successful and unsuccessful device
tests, including model, firmware, topic family and anonymized diagnostics.

## First-Run Validation

Compile:

```bash
python3 -m py_compile ems-solarflow-api-control.py
```

Simulation:

```bash
python3 -B ems-solarflow-api-control.py --simulate --max-cycles 1
```

Live read-only preflight:

```bash
python3 -B ems-solarflow-api-control.py --preflight --dry-run
```

## Live Writes And Validation

The copied template is already configured for normal live standalone control
after you enter real local values and review installation-specific limits:

```json
{
  "system": {
    "dry_run": false,
    "allow_hardware_writes": true,
    "allow_state_reconciliation_writes": true,
    "reconcile_ac_mode_on_start": true,
    "reconcile_smart_mode": true
  }
}
```

Use this optional staged validation path when you want extra caution:

1. Set `dry_run=true`.
2. Validate telemetry and grid meter readings with simulation, preflight, and
   dry-run.
3. Set `dry_run=false`.
4. Use bounded live runs for first tests and monitor the result.

Example bounded live run:

```bash
python3 -B ems-solarflow-api-control.py --duration 120
```

State reconciliation writes are enabled in the default regulation profile:

```json
{
  "system": {
    "dry_run": false,
    "allow_hardware_writes": true,
    "allow_state_reconciliation_writes": true
  }
}
```

More examples: [configuration-examples.md](../configuration-examples.md).
