# Safety

EMS controls real power hardware. Take a few minutes to check the points below
before you let it run unattended.

## When EMS starts writing

There is no separate "enable live writes" switch to flip. The template ships
with `system.dry_run: false` and every write gate open, and only one thing
holds writes back: **template placeholders**. While any device IP, the grid
meter address or a serial still carries a placeholder, EMS runs in safe mode,
calculates targets and writes nothing. The placeholder values are any address
in `198.51.100.0/24`, a range reserved for documentation that home routers do
not hand out (the template uses `.50`, `.100`, `.101`), `0.0.0.0`, `localhost`,
`example.com` (and any `*.example.com`), `YOUR_SN` and `YOUR_TOKEN_HERE`.
An address that cannot be parsed at all holds writes back the same way.
`emsctl.py diagnose` and the Admin Console's Maintenance page name every field
that still keeps EMS in safe mode.

The moment the last placeholder is replaced and EMS restarts, it writes
`outputLimit` and reconciles `minSoc`, `socSet`, `acMode` and `inputLimit`.
So:

- Delete the template's second device if you have only one inverter. A
  leftover placeholder device keeps the whole system in safe mode.
- A config copied from an older template may still name `192.168.1.50`,
  `192.168.1.100` or `192.168.1.101`. These are read as real addresses now, so
  replace any of them you did not enter yourself.
- To watch before it acts, set `"dry_run": true` under `system` first, check
  the decisions in the dashboard and with `emsctl.py diagnose --control`, then
  set it back to `false`.
- To stop control at any time: `python3 emsctl.py system disable` (Docker:
  `docker compose exec ems python3 emsctl.py system disable`), or switch
  **EMS enabled** off in the dashboard. Inverters keep their last output limit.

## Features that write on their own

Two features are enabled in the template and act without an operator:

- **Winter mode** (`winter.enabled: true`, months 10 to 3) raises `minSoc`
  once a day up to `winter_min_soc` (40 %) and sets a winter AC charge
  `inputLimit` of 200 W. On a device with PV, `minSoc` leads the SoC by at
  most 3 points, and while the battery is below it the device exports at most
  `min_output_limit` for up to three hours so PV charges the battery first. On a battery without PV
  of its own (`pv_kwp: 0`) the noon step may lead the SoC by up to two steps,
  and the firmware may then charge it from the grid at full AC power. See
  [winter-mode.md](../winter-mode.md#device-types-and-policies).
- **Battery full-charge assist** (`battery_full_charge_assist.enabled: true`)
  raises `socSet` to 100 % every 28 days; with `enable_ac_charge_mode: true`
  it switches the inverter to AC input at `force_time` (14:00 local time; in
  Docker the container's [time zone](../docker.md#time-zone)) on the due day
  and charges from the grid at 600 W.

Turn either off in the config if you do not want it.

## Before the first live run

- Check your inverter serial numbers.
- Check inverter IP addresses.
- Check your grid meter direction (import positive, export negative). EMS
  does not invert a meter. If the sign is wrong, turn the clamp around or
  select the other channels on the meter; see
  [Troubleshooting](troubleshooting.md).
- Check the maximum output limit.
- Check minimum and maximum battery SOC.
- Make sure no other controller writes Zendure output limits. Use only one
  active controller: disable Zendure HEMS, Smart Matching, Zendure schedules /
  energy plans, Home Assistant or ioBroker automations that set inverter power,
  and any second EMS instance. This matters especially for Zendure Cloud MQTT
  control — the cloud services write the same setpoints. The EMS cannot disable
  them for you; it reports `external_control_suspected` when a foreign writer
  repeatedly overrides a confirmed target.
- Start with conservative settings.
- Monitor the first live run.


## Zendure Cloud MQTT control confirmation

Cloud MQTT control is only proven when the whole path checks out — a broker
publish succeeding is **not** the same as the device applying the command. The
EMS keeps these facts separate and honest:

- **Broker delivery is not device acceptance.** `broker_delivery=delivered`
  (the QoS 1 PUBACK) only means the broker received the publish; a command is
  confirmed only when telemetry proves every commanded property (mode *and*
  setpoint) actually took effect and has trustworthy per-property time
  provenance. A matching cached value or a property with no observation time
  cannot confirm a command. Late PUBACK evidence is retained independently of
  newer commands within a bounded ledger. If an unresolved raw MQTT identifier
  becomes ambiguous across a disconnect, EMS quarantines it rather than ever
  attributing its callback to the wrong command.
- **A known model always writes to its canonical topic.** A stale
  `mqtt.write_topic` left in an upgraded config can never misroute control; it is
  flagged and cleaned up by maintenance. Only the isolated custom escape hatch
  uses an explicit topic.
- **Validate before trusting it.** To prove Cloud MQTT control end to end on real
  hardware, stop the EMS and use the hardware probe
  ([developer/mqtt-write-latency-probe.md](../developer/mqtt-write-latency-probe.md)):
  it confirms the masked canonical topic shape, broker delivery, an exact
  setpoint match, the required mode properties, and a fully verified restore of
  every property the operation can modify. Restore verification also requires an
  observed post-command state transition before the complete initial state
  returns, so an unchanged read cannot hide a still-pending test command. The
  probe refuses the first write if any required initial value is missing and
  never substitutes a normal atomic power command for a partial restore. A read
  error during cleanup cannot skip the full restore attempt: the probe records
  the fault, keeps verification fail-closed, exits non-zero without sufficient
  evidence, and always stops the MQTT runtime it started.
  Do not conclude "Cloud MQTT control works" from movement toward the target or a
  broker PUBACK alone.

## AC charging draws from the grid

**AC charging is on by default**, for the installation and for every device,
like the other EMS features. It is still the one feature that can *spend* energy
rather than place it, so know what that means before you run it:

- **It acts only against your own surplus.** Entry needs a sustained export
  above `charge_start_w`; an installation that never exports never charges. It
  stops the moment the house needs the power back.
- **A config upgrade turns it on.** A `config.json` written before the feature
  existed has neither key, and the upgrade fills both in as enabled. If you do
  not want that, set `ac_charge_control.enabled` to `false`, or
  `ac_charge_enabled` to `false` on the devices you want to keep out of it,
  before the upgrade runs.
- **Which devices can charge is decided by the model**, not by the switch — see
  [supported-setups.md](supported-setups.md#supported-zendure-devices-local-api--zensdk).
  Only the 800 Pro 2 was measured here; the other models are enabled on the
  device catalogue's word, and the EMS logs `ac_charge_not_delivered` if a
  commanded charge never draws any current.
- **Check `max_total_charge_power_w`** for your installation — your circuit and
  your fuse, which nothing in the EMS can measure. The default is 1200 W.
  `max_charge_power_w` per device at 0 means "ask the device for its own
  ceiling" — or its model's rated charge power when it reports none.

You can stop it at any time without restarting the EMS, from the dashboard's
Control tab in write mode or from the CLI:

```bash
python3 emsctl.py ac-charge disable            # the whole feature
python3 emsctl.py device WR1 ac-charge off     # one device
```

Either switch stops a running charge in the same cycle. Stopping a device that
is drawing from the grid is the one action that must never wait for a threshold,
a counter or a restart. Disabling the EMS (`system.enabled`) or one device does
too: a charging device gets one final command that ends the charge, and then the
EMS writes nothing more to it, whether the charge was the regulator's or the
full-charge assist's. Parking a charging device ends the EMS's charge the same
way, unless the park sets a charge power of its own, which then takes the charge
over: switching the EMS off or stopping it does not end that one.

So does losing the grid meter. A meter client that cannot reach its hardware
keeps returning its last reading, and "still exporting" is indistinguishable
from a real surplus — so charging stops when that reading is older than
`telemetry_max_age_seconds` and says so with `ac_charge_stopped_stale_meter`.
Discharging continues, because placing energy you already own on a stale reading
costs nothing like drawing from the grid on one does.

### What happens if the EMS stops while charging

**Stopping the EMS leaves the devices as they are.** That is deliberate: a stop
you asked for — `docker stop`, `docker compose down`, `systemctl stop`, Ctrl-C —
is usually a restart, and resetting every device for the length of an update
would drop the house's cover and throw away a charge that then has to re-confirm
its entry window. Discharging devices keep their `outputLimit` regardless;
charging devices now keep theirs too. The EMS says so when it happens:
`event=ac_charge_kept_across_stop` names the signal that stopped it and the
device it left charging.

A **charging** device therefore keeps drawing while the EMS is away. That is
bounded — the charge ends at the device's configured maximum SoC — but it still
costs whatever that energy costs, and an update that never comes back leaves it
drawing until then. If a restart does not complete, or once the EMS is back
after an abrupt stop, check the device and stop it in the Zendure app or with
`emsctl.py device WR1 ac-mode output`.

The EMS **does** return a charging device when it stops by itself: `--once`,
`--max-cycles`, `--duration` or an error that ends it. In normal operation a
failing cycle (`diagnose --control` names it) does not end the EMS: it stops
where it raised, what it wrote before stands, and a charge it no longer reaches
runs on, up to the maximum SoC. An unstoppable kill (power loss, `kill -9`, a
container removed, not stopped) writes nothing; nothing times the command out.

The same applies to a device that drops off the network mid-charge: the EMS
cannot write to a device it cannot reach, so that one keeps charging until it is
reachable again. The remaining devices adapt in the same cycle.

The EMS itself recovers on the next start. A device it finds in AC input —
charging or not, above its discharge floor or at it — gets one exit command
(`acMode=2`, `inputLimit=0`) if the EMS could have charged it: AC charging on,
the device's own AC charging switch on, a model that can charge from AC. The
EMS cannot tell its predecessor's charge from an app's or from the firmware's
protection charge, so it ends it, on the normal power write gate, also with
`allow_state_reconciliation_writes` off; a disabled device, or any device while
the EMS is switched off, gets it as its final command. A device that answers it
and charges on gets it again every 30 seconds, three times at most, then
`ac_charge_start_exit_unconfirmed` is logged. A device in AC input again after
it took that command, or still after the third, is left to whoever put it
there: at the floor the firmware recovering an empty battery, which the EMS
leaves alone until it is done; above the floor the state reconciler takes it
back only where it is allowed to, and an MQTT device with the next power
command. A device the EMS could never have charged gets no exit command: above
the floor the same holds, and at the floor its charge is the firmware's from
the start. Neither does a device you parked in AC input, across a restart as
well. A device that comes back from the network charging at its floor is not a
new start and earns no exit command: the charge is either the EMS's own, which
it regulates or ends as before, or the firmware's, which it leaves alone.

## During the first live run

- Watch grid power.
- Watch inverter output.
- Watch battery SOC.
- Stop EMS if values look wrong.

The [Admin Console](admin-console.md) dashboard and diagnostics make it easy to
watch these values during the first run.

## Backups before risky changes

Create a backup before updates, restore, config changes, or a reinstall. The
Admin Console can create and restore backups for you — see
[Backup and restore](admin-backup-restore.md).

## Do not expose local control interfaces

EMS and the Admin Console are designed for trusted local networks. Do not expose
the Admin Console — or the EMS ports — to the internet. Use them only on a
trusted local network.

The Admin Console requires a password (the same one as the EMS Dashboard) before
any setup, maintenance or backup action. This is a local safeguard, not a
substitute for keeping the appliance off the public internet.

The Admin Console is intended for a trusted local network. Optional HTTPS can
protect local browser traffic, but the generated self-signed certificate is not
a replacement for a VPN or a properly secured reverse proxy for remote access.

Like the Dashboard, the Admin Console can write runtime *overrides*, but only for
a fixed whitelist of keys (system power/loop limits, winter/HA enable, per-device
enabled/max power/PV priority/off-grid mode) and only through the same validated
runtime-write limits — never the hardware `outputLimit` or state-reconciliation
write gates. The safety property is the whitelist, not the store: no other key
can reach `runtime-state.json` from the Admin.

## Technical safety model

For write gates, runtime write types, and control internals, see the
[technical safety model](../technical/safety-model.md).
