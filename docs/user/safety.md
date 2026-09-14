# Safety

EMS controls real power hardware. Take a few minutes to check the points below
before you let it run unattended.

## Before enabling live writes

- Check your inverter serial numbers.
- Check inverter IP addresses.
- Check your grid meter direction (import positive, export negative).
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

Until your real values are filled in, EMS stays in safe mode: it calculates
targets but does not write to hardware. Review your settings, then enable live
writes.

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

AC charging is off until you switch it on, because it is the one EMS feature
that can *spend* energy rather than place it. Before enabling it:

- Decide which devices may charge. It is per device (`ac_charge_enabled`), on
  top of whether the model's AC charge path is established at all.
- Set `max_charge_power_w` per device and `max_total_charge_power_w` for the
  installation. Leaving the per-device value at 0 derives it from the device's
  output limit, which may be more than you want to draw.
- Start with the default thresholds. Charging begins only after a sustained
  surplus, and stops the moment the house needs the power back.

You can stop it at any time without restarting the EMS:

```bash
python3 emsctl.py ac-charge disable            # the whole feature
python3 emsctl.py device WR1 ac-charge off     # one device
```

### What happens if the EMS stops while charging

On a clean shutdown the EMS returns every device it put into charge. If the
process is killed — power loss, `kill -9`, a container removed mid-cycle — it
writes nothing, and **the device keeps charging until its own maximum SoC stops
it**. Nothing in the device times the command out.

That is bounded, not unlimited: the charge ends at the device's configured
maximum SoC. It still costs whatever that energy costs. If the EMS is stopped
abruptly while charging, check the device and stop it in the Zendure app or with
`emsctl.py device WR1 ac-mode output` once the EMS is back.

The EMS itself recovers on the next start: a device found charging with a
healthy battery is taken back into output mode. A device found charging at its
discharge floor is left alone, because there the firmware is recovering it.

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
