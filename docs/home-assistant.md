# Home Assistant Integration

Home Assistant is optional.

The EMS can run standalone with `config.json` and `runtime-state.json`.

## Roles

Home Assistant has two independent roles:

- status publishing
- optional runtime-state control helpers

Status publishing is enabled with:

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
input_boolean.ems_solarflow_wr1_offgrid_socket
```

Repeat for each device name in lowercase.

## Published Global Sensors

```text
sensor.ems_solarflow_load
sensor.ems_solarflow_target_total
sensor.ems_solarflow_solar_total
sensor.ems_solarflow_battery_power
sensor.ems_solarflow_home
sensor.ems_solarflow_soc_avg
```

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
```

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

It includes EMS control, runtime device control, winter status, device state,
battery status, PV details, and power-flow visualization.
