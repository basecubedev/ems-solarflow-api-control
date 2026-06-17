# Configuration Guide

The EMS uses one static installation config:

```text
config.json
```

Create it from the versioned template:

```bash
cp config.template.json config.json
```

`config.json` is local and ignored by Git. Do not commit real Home Assistant
tokens, Zendure serial numbers, or local IP addresses.

## Quick Start

1. Copy `config.template.json` to `config.json`.
2. Configure the real grid meter IP.
3. Configure one or more real Zendure device IPs and serial numbers.
4. Review power, SOC, battery, and PV limits for the installation.
5. Keep Home Assistant disabled unless you want HA integration.
6. Optionally set `dry_run=true` for a no-write validation run.
7. Run simulation, preflight, or dry-run if you want extra checks.
8. Monitor the first bounded live run before unattended operation.

The template default is standalone-first and live-control-ready after local
configuration:
`dry_run=false`, `allow_hardware_writes=true`, and
`allow_state_reconciliation_writes=true`.

This exposes the main regulation features with minimal setup. It is not a
universal safety profile; review device limits, SOC limits, grid meter readings,
and installation-specific constraints before normal operation.

Safe first checks:

```bash
python3 -B ems-solarflow-api-control.py --simulate --max-cycles 1
python3 -B ems-solarflow-api-control.py --preflight --dry-run
python3 -B ems-solarflow-api-control.py --dry-run --once
```

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
template default is `false` for normal standalone control. Set it to `true` for
a manual no-write validation run.

`system.simulation_mode` runs without real hardware. Most users should keep it
`false` and use `--simulate` from the command line when needed.

`system.allow_hardware_writes` allows Zendure `/properties/write` calls when
`dry_run=false`. The template default is `true` so normal `outputLimit` control
works after local device and grid meter configuration.

`system.allow_state_reconciliation_writes` allows SOC and mode reconciliation
writes. The template default is `true` because this is part of the default
regulation profile after local device limits and SOC limits have been reviewed.

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
`debug`.

`system.max_total_power` is the default maximum combined EMS target in watts.

`system.max_device_power` is the default per-device maximum in watts.

`system.deadband` is the general legacy target deadband in watts.

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

`system.loop_interval` is the control loop interval in seconds.

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

`battery_full_charge_assist.enabled` defaults to `false`. When enabled, EMS
tracks battery-backed devices in the core state database and can temporarily
request `socSet=1000` so firmware reaches its Max-SoC state within
`interval_days`.

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

More detail: [battery-full-charge-assist.md](battery-full-charge-assist.md).

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
energy-flow view animates, to reduce browser CPU/GPU load on weak devices or
long-open tabs. It is purely visual and never affects control, auth or data:

- `normal` — full animated flow view (pipe motion, glow/blur filters).
- `reduced` — keeps state colours and basic flow indication but drops glow
  filters and slows pipe motion.
- `off` — no continuous pipe animations and no glow/blur filters.

The browser-level `prefers-reduced-motion` setting is always respected on top of
this mode. The value is exposed read-only at `/api/ui-config` and applied as a
root CSS class by the frontend.

Dashboard write requests are constrained by the configured system and device
power limits. For example, a runtime `max_total_power` update cannot exceed the
configured `system.max_total_power`, and a device runtime `max_power` update
cannot exceed that device's configured `max_power`.

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
should keep the defaults.

`load_deadband_w` ignores very small load changes before target calculation.

`target_deadband_w` avoids writes when the new target is close to the current
commanded target.

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

`ramp_up_w_per_cycle` limits how fast the total target can rise.

`ramp_down_w_per_cycle` limits how fast the total target can fall.

`device_ramp_enabled` limits per-device target changes.

`device_ramp_up_w_per_cycle` limits per-device upward changes.

`device_ramp_down_w_per_cycle` limits per-device downward changes.

`large_import_bypass_w` can bypass normal smoothing during large imports.

`large_export_bypass_w` can bypass normal smoothing during large exports.

`bypass_ramp_multiplier` increases ramp speed during bypass situations.

`telemetry_max_age_seconds` marks device telemetry as stale after this age.

`stale_telemetry_ramp_factor` reduces ramp speed when telemetry is stale.

## Winter Settings

Winter mode is optional.

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

More detail: [winter-mode.md](winter-mode.md).

## Device Settings

Each Zendure device entry defines static installation data:

```json
{
  "name": "WR1",
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

Static device metadata stays in `config.json`, not in runtime-state.
`pv_priority_factor` is an exception: the config value remains the installation
default, while runtime-state can override the active weighting.

## Grid Meter Settings

`grid_meter.type` selects the local household/grid power meter implementation.
Supported values are `shelly`, `ecotracker`, and `tasmota_http`.

`grid_meter.ip` is the local meter IP address. The EMS controller only uses the
meter's current grid power value as the input for target calculation.

Shelly Pro 3EM uses:

```text
http://<ip>/rpc/Shelly.GetStatus
```

Shelly uses the aggregate `em:0.total_act_power` value by default and falls
back to summing all `em1:*` clamp values if the aggregate value is not
available.

Use `grid_meter.channels` when only selected Shelly clamps should be used. A
single item list such as `["c"]` is valid and reads only clamp C. Multiple
items such as `["a", "c"]` sum only those selected clamps. Valid entries are
`a`, `b`, `c`, `em1:0`, `em1:1`, and `em1:2`; `total` and `sum` are not valid
inside `channels`.

everHome EcoTracker uses:

```text
http://<ip>/v1/json
```

The EMS reads the required flat JSON `power` field. Positive values mean grid
import, negative values mean grid export. Phase values and energy counters are
optional and are not required for EMS control.

Tasmota HTTP JSON uses the `Status 10` sensor endpoint:

```text
http://<ip>/cm?cmnd=Status%2010
```

Set `grid_meter.power_path` to the dot-separated path of your current power
field inside the JSON response. Tasmota smart meter keys depend on the active
meter script, so the EMS does not guess a default. Positive power means grid
import; negative power means export/feed-in when your meter reports signed
values that way.

Shelly example:

```json
{
  "grid_meter": {
    "type": "shelly",
    "ip": "192.168.1.50"
  }
}
```

Shelly selected clamp C example:

```json
{
  "grid_meter": {
    "type": "shelly",
    "ip": "192.168.1.50",
    "channels": ["c"]
  }
}
```

Shelly selected clamps A and C example:

```json
{
  "grid_meter": {
    "type": "shelly",
    "ip": "192.168.1.50",
    "channels": ["a", "c"]
  }
}
```

EcoTracker example:

```json
{
  "grid_meter": {
    "type": "ecotracker",
    "ip": "192.168.1.60"
  }
}
```

Tasmota SML example:

```json
{
  "grid_meter": {
    "type": "tasmota_http",
    "ip": "192.168.1.70",
    "power_path": "StatusSNS.SML.Power_curr"
  }
}
```

Tasmota OBIS-style key example:

```json
{
  "grid_meter": {
    "type": "tasmota_http",
    "url": "http://192.168.1.70/cm?cmnd=Status%2010",
    "power_path": "StatusSNS.SM.16_7_0"
  }
}
```

Legacy configs with only `shelly.ip` still work. New configs should use
`grid_meter`.

If your meter returns a different JSON structure, please open a GitHub issue
and include the meter type, relevant config, logs, and an anonymized example
payload if possible.

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

More examples: [configuration-examples.md](configuration-examples.md).
