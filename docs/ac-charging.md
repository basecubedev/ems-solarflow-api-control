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

`ac_discharge_enabled: false` forbids output only. The device takes no share of
the output and is never parked at the standby floor; a discharge that was
running is ended with a written zero; it may still be charged.

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

The limit comes from the device's own `chargeMaxLimit` (or `chargeLimit`, the
name some firmware uses), capped by `max_charge_power_w` and the system maximum.
A device that reports neither falls back to its model's **rated charge power**:
the lower of the catalogue rating above and the limit Zendure-HA uses for the
same model, or the catalogue rating alone where Zendure-HA has none. That is
1000 W for both 800 Pro models, 1600 W for the 1600 AC+, 2400 W for the 2400 AC
and AC+, 3000 W for the 3000 Mix AC+, 3200 W for the 4000 Mix AC+ and 1200 W for
the Hyper 2000. A device that *reports* a ceiling of 0 has refused a charge, and
neither the rating nor `max_charge_power_w` overrides that. A device with no usable ceiling charges nothing
and logs `ac_charge_ceiling_unknown`.

**On MQTT a charge is also held to the device's `max_power`.** The MQTT client
refuses any command above `max_power` before it publishes, a charge as much as
a discharge, so the regulator never allocates more than that to an MQTT device.
A 2400 AC on MQTT with the default `max_power` of 800 W therefore charges at up
to 800 W; raise `max_power` for the device to let it take more.

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
   device reports `chargeMaxLimit` at all — without it the EMS falls back to the
   model's rated charge power — and it is how fields the EMS was never taught
   get found.

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
buying nothing; one device taking 126 W is one that works. A device that is
already charging keeps its place until its share falls below half of that, so
a total wobbling around the point where a share crosses 50 W does not switch
its AC direction out and back in.

Charging starts only after the discharge side has already reached zero and a
surplus above `charge_start_w` has persisted — `entry_confirm_cycles` of the
last `entry_window_cycles` observations, five of seven by default. The surplus
is what remains once the chargeable devices stop feeding out: a device held at
the standby floor `min_output_limit` exports those watts itself, and they vanish
the moment it switches to charging, so they are not counted.

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

Switching the EMS off (`system.enabled`) or a single device (its runtime
`enabled`) is not one of these switches, but it also ends a charge: a device the
EMS is charging gets one final command — the exit to idle — and then nothing
more, logged as `ac_charge_ended_on_disable`. Without it a disabled device kept
drawing from the grid with nobody watching. The command has done its job when
the device reports that it left the charge, not when it answered: a device that
took it and charged on is sent it again once the resend window has passed (30 s,
the MQTT confirmation window), and nothing in between.

A claim that takes a charging device from the regulator — an operator park
(`runtime_role` `ac_input`) or a maintenance routine such as the full-charge
assist — is held to the same rule, logged with `reason=claimed` and the claim's
own reason as `claim`. The exception is a claim that commands a charge of its
own and can write it: an AC-input role with `ac_charge_power_w` on the local API
with state reconciliation allowed takes the charge over. The EMS releases its
record of it (`ac_charge_handed_to_claim`) and writes nothing, and neither
switching control off nor stopping the EMS ends the claim's charge. A park as AC
input without a power ends the EMS's charge by its setpoint alone on the local
API — `inputLimit = 0`, the device staying in `acMode = 1` for the role, so the
relay is not moved out and back; over MQTT, where nothing keeps that role, it is
the exit to idle.

Both switches live in [runtime-state](technical/runtime-state.md), which the
EMS seeds from `config.json` when it loads it. The Dashboard therefore shows
the value the EMS applies, the same value the Admin's feature list shows
until an operator flips a switch at runtime. The Admin edits `config.json`
and mirrors the flag into runtime-state on Apply; it never reads the runtime
switch back into the config.

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
(200) still takes the device away mid-charge — and ends the EMS's charge with
one command, see above — and a device found charging that the EMS did not
command stays a leftover the reconciler may reclaim.

**At the battery floor a charge is the firmware's only when the EMS can tell.**
There the firmware may charge an empty pack by itself, and that is respected.
But after a restart a charge at the floor may be the EMS's own, left drawing
from the grid by the process before. The EMS then writes the exit once; a device that charges
on after it is the firmware's, and nothing more is written until that charge
ends. That exit is only for a device the EMS could have charged: with the
feature or the device's **AC charging** switch off, or on a model without an AC
charge path, no charge can be the EMS's own, so it is the firmware's from the
first cycle and nothing is written. The EMS's own record survives a reset of its regulation memory and a
device's absence, because the transport keeps it: it opens with a charge
command and ends only when the device reports that it left the charge. A device
still charging after an exit it answered and did not carry out is therefore
still the EMS's own, and is sent the exit again, never handed to the firmware.
For the same reason a failed read or a device that is unreachable for a while
does not make a floor charge unattributable: whatever charges there when it
answers again is the firmware's, and no exit is written into it. A
firmware charge counts from the moment the device shows the charge mode with a
setpoint, before current flows, so the state reconciler leaves its first two
seconds alone too. See
[technical/control-logic.md](technical/control-logic.md#firmware-owned-charging).

**Leaving a charge is one command, on every transport.** A device in
`acMode = 1` ignores a bare `outputLimit`, so the way back carries the direction
with its setpoint: `smartMode=1`, `acMode=2`, the new `outputLimit` and
`inputLimit=0` in one write. MQTT always sends that set. The local API keeps its
single-property `outputLimit` write, and sends the set while a charge the EMS
commanded is on record, until the device reports that it left the charge. A
device someone else holds in AC input — the vendor app, a schedule — gets the
bare `outputLimit` the EMS always wrote; taking it back is the state
reconciler's, behind its own gate. An exit the device
answered and did not carry out goes out again once per resend window (30 s),
never once per cycle. The exit therefore needs no state
reconciliation — with `allow_state_reconciliation_writes` off it used to never
arrive — and the shutdown release is a complete command rather than an
`outputLimit` the charging device ignored. On an MQTT device whose commands are
acknowledged (Hyper 2000), a change of direction does not queue behind the
command it replaces.

**Inside a running charge the local API changes the power with one value.**
Entering a charge is the atomic set. Once the device shows the charge mode the
EMS set — `acMode = 1` with `smartMode = 1`, after a charge this EMS started — a
new charge power is a bare `inputLimit`, which the device honours inside the
charge direction (measured on an 800 Pro 2, 2026-09-13). `smartMode` is a
flash-persistent mode, and the full set on every power change rewrote it several
hundred times an hour under a noisy surplus. Whenever the device shows anything
else, the set is sent whole again. MQTT keeps sending the set.

**A running charge is stopped even when the new target is zero.** The write
deadband suppresses a command that would change nothing, and it decides that by
comparing the target against what the device is doing. A charging device reports
`outputLimit` 0 and `outputHomePower` 0 while drawing hundreds of watts, so the
reference is taken from the measured AC input instead — otherwise "switch this
device off" would compare 0 against 0, skip the write, and leave the hardware
charging. A device that already shows the exit written (`acMode = 2`, no
`inputLimit`) while its current runs down for about two seconds is not sent the
exit again. Local-API devices are also covered by the startup `acMode` reconcile;
MQTT control devices have no state reconciliation and so no such path.

**A charge does not outlive the meter that justifies it.** A grid-meter client
that cannot reach its hardware returns the last good reading and marks it stale;
nothing caps how long it may do that. For discharging that is tolerable — the
EMS keeps placing energy you already own. Charging spends, and a held reading of
"still exporting 900 W" looks exactly like a real surplus, so charging stops
once the last successful read is older than `telemetry_max_age_seconds`
(default 10 s, about two loops) and logs `ac_charge_stopped_stale_meter`. A
single missed read is tolerated on purpose: ending a charge for one dropped
packet costs a full re-entry window.

That tolerance keeps a charge running; it never starts or sizes one. A held
reading is not an entry observation, however young, and the cycle that leaves
night/minSoc idle — which starts without a commanded total — holds what the
devices are doing rather than integrating the held reading into a first charge.
That hold is not part of the feature: it applies with AC charging off too, to
every cycle that starts without a commanded total, see
[technical/control-logic.md](technical/control-logic.md#grid-meter-unavailable-hold).

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
charging rather than charged through it. A device feeding out under its
`outputLimit` is not drained from outside as long as the pack delivers no more
than that output plus a 60 W margin for the inverter's own consumption (a
judgement, not a measurement): the EMS is the
only writer of that value, so the standby floor `min_output_limit` does not
keep a device out of charging, while an off-grid load of several hundred watts
on the same pack still does. Running an EMS alongside another
controller on the same hardware remains unsupported; see
[user/safety.md](user/safety.md).

**Night/minSoc idle does not swallow a surplus.** A device with no PV input and
an empty pack looks exactly like a plant at night. While the idle holds, the
charge decision keeps running on the meter's export against `charge_start_w`,
and once it enters a charge — the same five of seven observations as always —
the idle is left with `night_min_soc_idle_exit reason=ac_charge_surplus`. A single export spike does not end the idle, and a stale meter
reading never does. See
[technical/control-logic.md](technical/control-logic.md#night--minsoc-idle).

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
| `emsctl diagnose --control` | the configured band, limit, permitted devices and those the configured model refuses |
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
ac_charge_capacity_below_stop
ac_charge_ceiling_unknown
ac_charge_direction
ac_charge_ended_on_disable
ac_charge_entry_rate_limited
ac_charge_handed_to_claim
ac_charge_not_delivered
ac_charge_refused
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
as it is, because a restart is coming. It names the signal that ended the run
and every device the EMS last put into charge — by the same record the release
uses, so a charge whose regulator target was reset or zeroed is named too — so
a device still drawing after `docker compose down` is explained rather than
surprising. The release event is what you see when the EMS stopped by itself.

`ac_charge_capacity_below_stop` is a `warning`, said once until the capacity is
sufficient again: the
devices that may charge can together take no more than the band's lower edge
(`stop = max(0, charge_start_w - charge_hysteresis_w)`), counting
`max_total_charge_power_w`. A charge that small would leave again on the next
cycle, so the EMS does not start one. Raise `max_total_charge_power_w`,
`max_power` on an MQTT device, or `max_charge_power_w` where it is set below
the device's ceiling, or lower the band.

`ac_charge_ceiling_unknown` is a `warning`, said once per device until it has
a usable ceiling again: the device
may charge by every other rule, but has no usable ceiling. With
`reason=no_reported_or_rated_charge_limit` it reports no `chargeMaxLimit` or
`chargeLimit` and its model carries no rated charge power; set
`max_charge_power_w` for the device to unblock it. With
`reason=device_reports_zero_charge_limit` the device itself reports 0, which
the EMS respects even over `max_charge_power_w`; check the charge limit in the
Zendure app. While another device charges, the Control view names such a device
with `ac_charge_no_ceiling`.

`ac_charge_refused` is a `warning`, said once per device and reason, and again
only if the reason clears and returns. The device may charge by the operator's
switches and answers, but leaves out something a charge cannot do without, so
it is never charged: `reason=pack_count_unreported` — the report carries no
`packNum`, even beside a populated `packData`; `reason=max_soc_unreported` — no
`socSet`; `reason=model_unidentified` — neither a pinned `hardware_profile` nor
the reported `product` names a known model, and the line carries both as
`pinned_profile` and `reported_product`. None of these is the device refusing:
a confirmed zero packs, a reported zero charge limit and a full pack are, and
say nothing. A pinned model wins over a reported product that names another
one, so that is not a refusal either. `diagnose --control` lists what config
alone can tell — a pinned model without an AC charge path
(`model_cannot_charge`), an MQTT device whose pin names no known model
(`model_unidentified`), a telemetry-only device (`telemetry_only`) — under
**Refused devices**.

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
