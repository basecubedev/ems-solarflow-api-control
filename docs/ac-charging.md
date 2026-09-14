# AC Charging From Surplus

Optional. The EMS charges battery devices from AC when the house exports more
than it can place, instead of letting that surplus leave.

It is **off until you switch it on**, because it is the one feature that can
spend energy rather than place it. Read [user/safety.md](user/safety.md) before
enabling it.

## Configuration

```json
{
  "ac_charge_control": {
    "enabled": false,
    "charge_start_w": 150,
    "charge_hysteresis_w": 50,
    "entry_confirm_cycles": 5,
    "entry_window_cycles": 7,
    "max_charge_entries_per_hour": 12,
    "max_total_charge_power_w": 1200
  }
}
```

Per device:

```json
{
  "ac_discharge_enabled": true,
  "ac_charge_enabled": false,
  "max_charge_power_w": 0
}
```

See [technical/configuration.md](technical/configuration.md) for every key.

## What has to agree before a device charges

Six independent permissions, and any single no is enough:

1. `ac_charge_control.enabled`
2. the device's own `ac_charge_enabled`
3. a hardware model whose AC charge path is **established** — see
   [user/supported-setups.md](user/supported-setups.md)
4. current telemetry that allows charging
5. the device being online and enabled
6. nobody else owning its AC mode

## Behavior

Charging starts only after the discharge side has already reached zero and a
surplus above `charge_start_w` has persisted — `entry_confirm_cycles` of the
last `entry_window_cycles` observations, five of seven by default.

Charging stops **immediately** when the house needs the power back. The exit is
never delayed by a threshold, a confirmation count or the rate limit.

The lower edge of the band is derived, never configured:

```text
stop = max(0, charge_start_w - charge_hysteresis_w)
```

A load oscillating around zero causes no mode change at all: idle and discharge
are the same AC mode, and the mode boundary sits at `charge_start_w`.

See [technical/control-logic.md](technical/control-logic.md) for the direction
rules and why entry and exit measure different quantities.

## Runtime control

Both switches take effect without restarting the EMS:

```bash
python3 emsctl.py ac-charge disable            # the whole feature
python3 emsctl.py device WR1 ac-charge off     # one device
```

## Safety

A power command — charge, idle or discharge — travels on the device's normal
transport write gate. Charging is not gated separately: what prevents it is the
permission set above, not a second gate. Making the way *back* depend on an
extra gate would leave hardware drawing from the grid when that gate closed.

On a clean shutdown the EMS returns every device it put into charge. A killed
process writes nothing and the device charges on until its own maximum SoC stops
it — bounded, but it still costs. See [user/safety.md](user/safety.md).

## Known interactions in a mixed fleet

**A device that cannot charge still honours the standby output floor.** While
one device charges, another that may not will be written `min_output_limit`
(35 W by default) rather than zero, because that floor exists so a device is not
told to stop. It reads as a contradiction during a charge and costs a small,
continuous round trip. It is the floor's normal behaviour whenever the EMS wants
zero from a device, not something charging introduced; set `min_output_limit` to
0 if your hardware tolerates a true stop.

**A device that drops off the network mid-charge keeps charging.** The EMS
cannot write to a device it cannot reach, so the last commanded charge stands
until the device returns — at which point the EMS commands it back. The total
target adapts immediately: the unreachable device leaves the charge capacity and
the remaining devices take over what they can.

**A pack being drained is not charged.** If something outside the EMS draws from
a battery — a third-party inverter on the same pack — that device is skipped for
charging rather than charged through it. Running an EMS alongside another
controller on the same hardware remains unsupported; see
[user/safety.md](user/safety.md).

**A device with no battery never charges**, whatever its configuration says.
Battery presence is read from telemetry, not from the config.

## Where charging is visible

A charging device reports `output` as 0, so anything that reads the output
alone shows it as idle. The measured AC input power (`gridInputPower`) is
carried alongside the output and surfaces in four places:

| Surface | Name |
|---|---|
| Dashboard device tile | `ac_charge_w` |
| Dashboard flow / snapshot total | `inverter_charge_w` |
| Analytics | the **AC Charge** series and overlay |
| Home Assistant | `sensor.ems_solarflow_<device>_ac_charge` |
| InfluxDB | `zendure_device.grid_input` |

This is the measured value, not the commanded charge target. The two differ by
the device's own ramp, which was measured at 100–170 W/s.

In the aggregated flow diagram a charging fleet draws a **grid → inverter**
pipe, and the inverter node states `Charging <W>` above its output. Its value
stays the output it feeds the house, because in a mixed fleet one device can
export while another charges. What the charger takes off the meter is not drawn
a second time on the grid → home pipe, so at night — when the whole import is
the charge — that pipe falls idle rather than showing the same watts twice.

The diagram has no busbar node, so the inward flow is anchored at the grid. When
the charge is actually covered by a sibling inverter rather than by the grid,
the grid node still reads the truth (near zero) while the pipe overstates its
role. Preview it with `serve_dashboard_preview.py --scenario ac-charging`.

Energy statistics count charged energy separately (**AC Charge**, kWh) and
deliberately attach no monetary value to it.

The household load is corrected for it: the grid meter cannot tell a charging
device from an appliance, so the charge power is subtracted again before
`home_load_w` is reported. Without that, a device charging at 800 W would show
up as 800 W of extra household consumption.

## Logs

```text
ac_charge_direction
ac_charge_entry_rate_limited
ac_charge_released_on_shutdown
ac_charge_release_failed
```

`ac_charge_direction` is `info` when the direction changes and `debug`
otherwise. `ac_charge_entry_rate_limited` is a `warning`: reaching the hourly
cap means the thresholds do not fit the installation.
