# AC Charging From Surplus

The EMS charges battery devices from AC when the house exports more than it can
place, instead of letting that surplus leave.

**On by default**, for the installation and for every device. What keeps it from
acting is the surplus itself: it charges only against your own export, and stops
the moment the house needs the power back. It is still the one feature that can
spend energy rather than place it — read [user/safety.md](user/safety.md), and
note that a config upgrade turns it on for an installation that predates it.

## Configuration

```json
{
  "ac_charge_control": {
    "enabled": true,
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
  "ac_charge_enabled": true,
  "max_charge_power_w": 0
}
```

See [technical/configuration.md](technical/configuration.md) for every key.

## What has to agree before a device charges

Six independent permissions, and any single no is enough:

1. `ac_charge_control.enabled`
2. the device's own `ac_charge_enabled`
3. a hardware model the catalogue records as AC chargeable — see
   [Which models charge](#which-models-charge)
4. current telemetry that allows charging
5. the device being online and enabled
6. nobody else owning its AC mode

## Which models charge

The permission is decided per model, not per protocol family: every ZenSDK model
builds the identical charge command, which proves the command is well formed and
says nothing about whether the hardware has an AC input for battery charging.

| Charges | Model | Catalogue rating | Evidence |
|---|---|---:|---|
| yes | SolarFlow 800 Pro 2 | 1000 W | **measured** on real hardware 2026-09-13 |
| yes | SolarFlow 800 Pro | 1000 W | vendor catalogue |
| yes | SolarFlow 1600 AC+ | 1600 W | vendor catalogue |
| yes | SolarFlow 2400 AC | 2400 W | vendor catalogue |
| yes | SolarFlow 2400 AC+ | 2400 W | vendor catalogue |
| yes | SolarFlow 3000 Mix AC+ | 3000 W | vendor catalogue |
| yes | SolarFlow 4000 Mix AC+ | 4000 W | vendor catalogue |
| yes | Hyper 2000 | 1600 W | vendor catalogue |
| no | SolarFlow 800, 800 Plus | — | no AC charging of the battery |
| no | SolarFlow 2400 Pro | — | not an AC charger; not the 2400 AC series |
| no | AIO 2400 | — | different system architecture |
| no | Hub 1200, Hub 2000 | — | only via an ACE 1500, which the EMS cannot command |
| no | ACE 1500, SuperBase | — | telemetry-only: no write path at all |

The catalogue rating is **not** the charge limit. The limit comes from the
device's own `chargeMaxLimit`, capped by `max_charge_power_w` and the system
maximum; a device that reports no ceiling charges nothing.

Only the 800 Pro 2 was put on a probe here. The rest are enabled on the strength
of the vendor catalogue, which is recorded per model as `charge_evidence` and
surfaced in the `ac_charge_not_delivered` warning below. If a row turns out to
be wrong, the failure is quiet but not silent: the command is accepted, no
current flows, and that warning fires.

### Checking a model that is enabled from the catalogue

If your model's evidence is **vendor catalogue** rather than *measured*, nobody
has yet seen it draw a commanded charge. Four things settle it, and all of them
are read-only:

1. **Does the EMS ever decide to charge?** `event=ac_charge_direction` with
   `charging=true` in the log. If it never appears, the installation is not
   exporting enough for long enough — check `charge_start_w` against your actual
   surplus before concluding anything about the hardware.
2. **Does current actually flow?** The device tile's **AC Charge**, or
   `sensor.ems_solarflow_<device>_ac_charge`. A number above zero while the EMS
   commands a charge is the proof.
3. **Does the EMS complain?** `event=ac_charge_not_delivered` is the negative
   result: the command was accepted and nothing flowed for six cycles. If it
   fires repeatedly, the catalogue row is wrong for that model — set
   `ac_charge_enabled` to `false` for the device and please report it.
4. **What does the device say about itself?**

   ```bash
   python3 emsctl.py diagnose --hardware --json
   ```

   Each device carries `reported_properties` with the property **names** it
   reports (never values) and which of them the EMS does not read. That is the
   most useful thing to send back from an unfamiliar model: it says whether the
   device reports `chargeMaxLimit` at all — without it the EMS charges nothing —
   and it is how fields the EMS was never taught get found.

Note that `max_total_charge_power_w` defaults to **1200 W** for the whole
installation, which is below what a 2400-class device could draw. That is the
protective default, not a property of your hardware; raise it only for a circuit
you know carries it.

## Behavior

A small charge is **concentrated rather than spread**. Devices are weighted by
the room left in their batteries, so a nearly full one takes a sliver — and a
sliver still costs a full AC direction change while buying nothing, and may be
too small to show up in `gridInputPower` at all. Shares below roughly 50 W are
therefore dropped and re-allocated to devices that can use them, down to a
single device if need be. Six devices taking 21 W each is six direction changes
buying nothing; one device taking 126 W is one that works.

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

Both switches take effect without restarting the EMS, from the CLI:

```bash
python3 emsctl.py ac-charge disable            # the whole feature
python3 emsctl.py device WR1 ac-charge off     # one device
```

or from the dashboard's Control tab in [write mode](dashboard.md#dashboard-write-mode)
— **AC charging** as its own card for the installation, and a per-device toggle
next to each device's enabled flag.

## Safety

A power command — charge, idle or discharge — travels on the device's normal
transport write gate. Charging is not gated separately: what prevents it is the
permission set above, not a second gate. Making the way *back* depend on an
extra gate would leave hardware drawing from the grid when that gate closed.

A stop you asked for leaves a running charge alone: an update or a restart is
meant to preserve the last state, not reset it. The EMS does return a charging
device when it stops by *itself* — `--once`, `--max-cycles`, `--duration`, an
unhandled error — because nothing is coming back to supervise it. Either way the
charge is bounded by the device's own maximum SoC. See
[user/safety.md](user/safety.md).

**While the regulator charges a device, it owns that device's AC direction.**
Every cycle each device gets a default claim of `ac_output`, and the state
reconciler writes `acMode` whenever telemetry disagrees with the claim — so
without a claim of its own the regulator's `acMode = 1` would be overwritten
with `acMode = 2` once per loop, against the power command putting it back. The
regulator therefore claims the device at priority 100 with *no* desired mode,
meaning "the power command owns this". An operator park (150) or maintenance
(200) still takes the device away mid-charge, and a device found charging that
the EMS did not command stays a leftover the reconciler may reclaim.

**A running charge is stopped even when the new target is zero.** The write
deadband suppresses a command that would change nothing, and it decides that by
comparing the target against what the device is doing. A charging device reports
`outputLimit` 0 and `outputHomePower` 0 while drawing hundreds of watts, so the
reference is taken from the measured AC input instead — otherwise "switch this
device off" would compare 0 against 0, skip the write, and leave the hardware
charging. Local-API devices are also covered by the startup `acMode` reconcile;
MQTT control devices are output-only and have no such path.

**A charge does not outlive the meter that justifies it.** A grid-meter client
that cannot reach its hardware returns the last good reading and marks it stale;
nothing caps how long it may do that. For discharging that is tolerable — the
EMS keeps placing energy you already own. Charging spends, and a held reading of
"still exporting 900 W" looks exactly like a real surplus, so charging stops
once the last successful read is older than `telemetry_max_age_seconds`
(default 10 s, about two loops) and logs `ac_charge_stopped_stale_meter`. A
single missed read is tolerated on purpose: ending a charge for one dropped
packet costs a full re-entry window.

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

**A device that stops answering hands its share to the others.** Its capacity
leaves the total, its target collapses to zero, and the remaining devices take
up the slack; the direction holds while something can still absorb the surplus.
The EMS cannot write to it, so it keeps whatever it was last told — the same
situation as a killed process, except the EMS is running and will command it
again the moment it answers. See [user/safety.md](user/safety.md).

**The full-charge assist outranks the regulator.** Both want the AC direction of
the same device, and the claim ladder settles it in one place: the assist (150)
beats the regulator (100), its claim forbids output control, and the regulator
stops commanding that device in the same cycle rather than both writing a
direction at it.

### Known limit: a device that accepts a charge and draws nothing

A model whose catalogue row is wrong still **holds its share of the
allocation**. Measured on a two-device fleet where only one responds: both are
commanded 600 W, the EMS assumes 2000 W of capacity, 600 W is absorbed, and the
rest of the surplus keeps leaving. The working device does not take up the
slack.

`ac_charge_not_delivered` names the device, so this is diagnosable rather than
silent — but until you act on it the fleet charges at part of its capacity. Turn
`ac_charge_enabled` off for that device and please report it, so the catalogue
row can be corrected.

It is not dropped from the allocation automatically on purpose. Doing that needs
a signal that a device is not charging, and both available witnesses can be
absent on a model nobody has measured: `gridInputPower` may not be reported, and
`acStatus` is not in every MQTT snapshot. Excluding a device on a missing field
would stop a charge that works. The first measurement on an unproven model is
what decides this, which is why `diagnose --hardware` reports the property names
a device actually sends.

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
| `emsctl diagnose --control` | the configured band, limit and permitted devices |
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
ac_charge_band_collapsed
ac_charge_direction
ac_charge_entry_rate_limited
ac_charge_not_delivered
ac_charge_stopped_stale_meter
ac_charge_kept_across_stop
ac_charge_released_on_shutdown
ac_charge_release_failed
```

`ac_charge_direction` is `info` when the direction changes and `debug`
otherwise. `ac_charge_entry_rate_limited` is a `warning`: reaching the hourly
cap means the thresholds do not fit the installation.

`ac_charge_entry_rate_limited` is said **once per episode**, not once per
cycle: the condition holds as long as the surplus does, and one line per loop
buries every other event instead of surfacing this one. The usual cause is a
collapsed band — `charge_hysteresis_w` at 0, or `charge_start_w` at 0 — which
puts entry and exit at the same threshold; `ac_charge_band_collapsed` names that
once at startup.

`ac_charge_kept_across_stop` is an `info` and the counterpart of
`ac_charge_released_on_shutdown`: a stop you asked for leaves a charging device
as it is, because a restart is coming. It names the signal that ended the run,
so a device still drawing after `docker compose down` is explained rather than
surprising. The release event is what you see when the EMS stopped by itself.

`ac_charge_stopped_stale_meter` is a `warning`: the load reading the charge
rests on stopped being a measurement. It means the grid meter is unreachable,
not that anything about the charging is wrong.

`ac_charge_not_delivered` is a `warning` and the one to watch on a model
enabled from the catalogue rather than from a measurement. It fires once when a
commanded charge has produced no measured AC input for six consecutive cycles,
and carries the model and its `charge_evidence`. It **changes nothing** — the
charge keeps being commanded. If it repeats on your hardware, that model's
catalogue row is likely wrong; turn `ac_charge_enabled` off for the device and
please report it.
