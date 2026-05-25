# Home Assistant Integration

Home Assistant is optional.

The EMS can run standalone with `config.json` and `runtime-state.json`.
The template default keeps Home Assistant disabled with `ha.enabled=false` and
`ha.control_enabled=false`.

## Roles

Home Assistant has two independent roles:

- status publishing
- optional runtime-state control helpers

Home Assistant is not a safety authority. If helper sync fails, times out, or
raises an error, the EMS logs the failure and continues the control loop with
the current local runtime-state values.

`runtime-state.json` remains the local control state. Home Assistant helpers can
change only the runtime fields documented below, and only when both static and
runtime HA control are enabled.

Enable status publishing and optional helper controls manually with:

```json
{
  "ha": {
    "enabled": true,
    "control_enabled": true,
    "url": "http://homeassistant.local:8123",
    "token": "YOUR_TOKEN"
  }
}
```

Use `--no-ha` to disable HA for a run.

## Runtime Helpers

Global helpers:

```text
input_boolean.ems_solarflow_ha_enabled
input_boolean.ems_solarflow_ha_control_enabled
input_boolean.ems_solarflow_enable
input_number.ems_solarflow_max_power
input_number.ems_solarflow_interval
input_number.ems_solarflow_min_output_limit
input_boolean.ems_solarflow_winter_enabled
```

Per-device helpers:

```text
input_boolean.ems_solarflow_wr1_enabled
input_number.ems_solarflow_wr1_max_power
input_select.ems_solarflow_wr1_offgrid_socket_mode
```

Repeat for each device name in lowercase.

Offgrid socket mode options:

```text
off
eco
standard
```

Helper changes are ignored for a run when static `ha.enabled=false`, static
`ha.control_enabled=false`, runtime `ha.enabled=false`, runtime
`ha.control_enabled=false`, `--no-ha`, simulation, or replay is active.

## Published Global Sensors

```text
sensor.ems_solarflow_load
sensor.ems_solarflow_target_total
sensor.ems_solarflow_solar_total
sensor.ems_solarflow_battery_power
sensor.ems_solarflow_home
sensor.ems_solarflow_soc_avg
```

`sensor.ems_solarflow_target_total` is the effective EMS command intent: the
sum of per-device targets after allocation, device ramp, enabled/offline gates,
`min_output_limit`, and device max clamping. The sensor exposes
`controller_target_w` for the internal stabilized controller target and
`allocated_target_w` for the post-allocation target before final control gates.

`sensor.ems_solarflow_home` is a calculated display/runtime value, not the
smoothed control target. Short-term differences between home load, controller
target, per-device target, written `outputLimit`, and actual device output are
expected because the EMS filters, ramps, clamps, rate-limits writes, and then
waits for device/API behavior to catch up.

## Published Device Sensors

For each device:

```text
sensor.ems_solarflow_wr1_soc
sensor.ems_solarflow_wr1_min_soc
sensor.ems_solarflow_wr1_max_soc
sensor.ems_solarflow_wr1_solar
sensor.ems_solarflow_wr1_output
sensor.ems_solarflow_wr1_target
sensor.ems_solarflow_wr1_output_limit
sensor.ems_solarflow_wr1_soc_limit
sensor.ems_solarflow_wr1_pack_state
sensor.ems_solarflow_wr1_battery_power
sensor.ems_solarflow_wr1_battery_power_avg
sensor.ems_solarflow_wr1_remaining_time
sensor.ems_solarflow_wr1_panel1
sensor.ems_solarflow_wr1_panel2
sensor.ems_solarflow_wr1_panel3
sensor.ems_solarflow_wr1_panel4
binary_sensor.wr1_fault
binary_sensor.wr1_ac_active
binary_sensor.wr1_dc_active
binary_sensor.wr1_grid_online
binary_sensor.wr1_available
```

`sensor.ems_solarflow_wr1_target` follows the same effective command semantics
as the global target. Its `allocated_target_w` attribute contains the
post-allocation per-device target before final control gates.

Per-device sensors include freshness attributes:

```text
available
telemetry_source
last_seen
last_seen_age_s
```

`binary_sensor.wr1_available` is `off` when the EMS is publishing cached or
fallback telemetry for that device. Cached values remain visible in HA, but
should be treated as last-known data rather than live measurements.

## Winter Sensors

```text
binary_sensor.ems_solarflow_winter_enabled
binary_sensor.ems_solarflow_winter_active
binary_sensor.ems_solarflow_winter_adjust_window
sensor.ems_solarflow_winter_summer_min_soc
sensor.ems_solarflow_winter_min_soc
sensor.ems_solarflow_winter_ramp_step
sensor.ems_solarflow_winter_ac_charge_power
sensor.ems_solarflow_winter_last_adjust_date
sensor.ems_solarflow_wr1_winter_min_soc_target
sensor.ems_solarflow_wr1_winter_estimated_ramp_days
```

## Dashboard

The repository contains a dashboard example:

```text
homeassistant-dashboard/dashboard.yaml
```

Dashboard preview:

```text
homeassistant-dashboard/dashboard-preview.jpg
```

It includes EMS control, runtime device control, winter status, device state,
battery status, PV details, and power-flow visualization.

Troubleshooting stale, unavailable, or ignored HA values:
[troubleshooting.md](troubleshooting.md).
