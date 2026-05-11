# Control Logic

The EMS calculates power targets from local load and device telemetry.

## Pipeline

1. Reload `runtime-state.json` if changed.
2. Optionally sync Home Assistant helper values with runtime state.
3. Read Shelly house load.
4. Read Zendure telemetry.
5. Detect runtime capabilities.
6. Run state reconciliation when due.
7. Stabilize total target.
8. Allocate target across devices.
9. Apply device ramp and limits.
10. Apply `min_output_limit` while enabled.
11. Apply deadband and write cooldown.
12. Write `outputLimit` only behind safety gates.

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

