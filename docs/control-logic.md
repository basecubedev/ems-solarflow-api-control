# Control Logic

The EMS calculates power targets from local load and device telemetry.

## Pipeline

1. Reload `runtime-state.json` if changed.
2. Optionally sync Home Assistant helper values with runtime state.
3. Read Shelly house load.
4. Read Zendure telemetry.
5. Detect runtime capabilities.
6. Run state reconciliation when due.
7. Detect strict night/minSoc idle.
8. Stabilize total target.
9. Allocate target across devices.
10. Apply device ramp and limits.
11. Apply `min_output_limit` while enabled.
12. Apply deadband and write cooldown.
13. Write `outputLimit` only behind safety gates.

## Stable Fast Output Control

The controller keeps an internal `commanded_total_w`.

It calculates:

```text
desired_total_w = commanded_total_w + filtered_load_w
```

Then it applies:

- load deadband
- target deadband
- median/EMA filtering
- total ramp limit
- per-device ramp limit
- write cooldown
- stale telemetry ramp reduction
- large import/export ramp bypass multiplier

This makes short loop intervals usable without large alternating target swings.

## Night / minSoc Idle

When all active and online devices are blocked at their discharge floor, the EMS
can enter a strict night/minSoc idle state. This state exists to avoid repeated
night-time API writes while still keeping the inverter wakeup value configured.

The state is entered only when every controlled device reports all of these
values exactly:

- `solarInputPower == 0`
- `solarPower1 == 0`
- `solarPower2 == 0`
- `solarPower3 == 0`
- `solarPower4 == 0`
- `packInputPower == 0`
- `outputPackPower == 0`
- `outputHomePower == 0`
- `electricLevel <= minSoc` or `socLimit == 2`

In this state the existing runtime `min_output_limit` is used as the
standby/wakeup `outputLimit`. If a device is already at that value, no write is
sent. If it is not, the EMS writes the value once and then suppresses further
`outputLimit` writes until the state is left.

The idle state is left as soon as any controlled device reports positive PV on
`solarInputPower` or one of `solarPower1` through `solarPower4`. The output
control memory is reset so the normal controller initializes from fresh
telemetry.

## PV-First Allocation

When PV can cover the requested target, the EMS allocates output using PV-first
weights and PV-only limits.

If PV-first allocation leaves unmet demand, the EMS may top up from battery only
on devices that:

- can export
- can discharge
- have SOC above minSoc
- have target headroom

## Battery Balancing

Battery discharge is weighted by usable battery energy:

```text
usable_percent = max(0, soc - minSoc)
weight = battery_kwh * usable_percent / 100
```

This favors devices with more usable energy while avoiding devices at or below
their discharge floor.

## Deadband

The EMS compares the calculated target with the runtime `outputLimit` when
available. If `outputLimit` is missing or zero, it falls back to current output.

Small changes below `deadband` are skipped.
