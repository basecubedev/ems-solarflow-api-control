# Observed Firmware State: No Energy Path

## Purpose

This document records an observed/inferred Zendure firmware state from
InfluxDB runtime captures.

The goal is to separate factual observations from later EMS control
decisions.

This is not an official Zendure state definition. It is a documented
runtime observation based on local telemetry.

---

## Summary

A device can be considered to be in an observed **no-energy-path** state
when all of the following are true:

```text
PV input is effectively zero
DC path is inactive
AC path is inactive
output is effectively zero
battery charge/discharge flow is effectively zero
```

In this state, no relevant energy flow was observed.

---

## Observed Condition

Recommended detection condition for analysis:

```text
solar <= 1
solar1 <= 1
solar2 <= 1
solar3 <= 1
solar4 <= 1
dc_status == 0
ac_status == 0
output <= 2
pack_in <= 2
pack_out <= 2
```

Interpretation:

```text
PV = 0
DC path = off
AC path = off
no AC output
no pack charge
no pack discharge
```

---

## InfluxDB Observation

A 24h raw InfluxDB capture was analyzed.

The following query searched for counterexamples:

```text
PV ~= 0
DC path off
AC path off
but output / pack_in / pack_out > 2W
```

Result:

```text
No counterexamples found.
```

A second query counted valid no-energy-path samples:

```text
WR1: 4216 samples
WR2: 4228 samples
```

With a 5 second capture interval this corresponds to approximately:

```text
WR1: 5.86 hours
WR2: 5.87 hours
```

This supports the working assumption:

```text
PV ~= 0 + DC path off + AC path off => no relevant energy flow
```

---

## Important Interpretation Rules

Do not use `soc_limit` alone to infer night idle or no-energy state.

Examples:

```text
soc_limit == 2 + pack_state == 1
```

can still mean:

```text
battery is protected from discharge but can charge from PV
```

Therefore `soc_limit == 2` alone does not mean night mode.

Also do not use `dc_status` or `ac_status` alone as a final state
indicator. They should be interpreted together with PV and power-flow
values.

---

## Related Field Interpretation

### DC Path / `dc_status`

Working interpretation:

```text
dc_status == 1
```

means the DC path is active or available for energy flow.

```text
dc_status == 0
```

means the DC path is inactive.

### AC Path / `ac_status`

Working interpretation:

```text
ac_status == 1
```

means the AC/inverter path is active or available for energy flow.

```text
ac_status == 0
```

means the AC path is inactive.

### Pack State / `pack_state`

Based on Zendure/zenSDK-style field definitions:

```text
0 = Standby / Idle
1 = Charging
2 = Discharging
```

### SOC Limit / `soc_limit`

Working interpretation:

```text
soc_limit == 2
```

likely indicates lower SOC protection / discharge limit.

Important:

```text
soc_limit == 2
```

does not block PV charging and does not imply that the complete device is
inactive.

---

## Recommended EMS Usage

This observation may be used as input for future EMS logic, but only with
a stability window.

Do not classify a device as no-energy-path based on a single sample.

Recommended future control condition:

```text
no_energy_path_stable =
    PV ~= 0
    and dc_status == 0
    and ac_status == 0
    and output ~= 0
    and pack_in ~= 0
    and pack_out ~= 0
    for at least 120-300 seconds
```

Potential EMS behavior in this state:

```text
continue telemetry
continue Home Assistant publishing
continue runtime-state updates
skip output writes
do not force min_output_limit
wait for stable PV/path return
```

---

## Relation to Night/minSOC Idle

The no-energy-path state can be part of night/minSOC idle detection, but
it is not identical to it.

A stronger night/minSOC idle candidate is:

```text
no-energy-path state
and pack_state == 0
and (soc <= min_soc or soc_limit == 2)
```

Still, EMS should treat this as an inferred state and avoid hard logic
until confirmed over multiple captures.

---

## Flux Query: Counterexample Search

```flux
from(bucket: "zendure_raw")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "zendure_device")
  |> filter(fn: (r) =>
    r._field == "solar" or
    r._field == "solar1" or
    r._field == "solar2" or
    r._field == "solar3" or
    r._field == "solar4" or
    r._field == "dc_status" or
    r._field == "ac_status" or
    r._field == "output" or
    r._field == "pack_in" or
    r._field == "pack_out"
  )
  |> pivot(rowKey:["_time", "device"], columnKey: ["_field"], valueColumn: "_value")
  |> filter(fn: (r) =>
    r.solar <= 1 and
    r.solar1 <= 1 and
    r.solar2 <= 1 and
    r.solar3 <= 1 and
    r.solar4 <= 1 and
    r.dc_status == 0 and
    r.ac_status == 0 and
    (
      r.output > 2 or
      r.pack_in > 2 or
      r.pack_out > 2
    )
  )
  |> keep(columns: ["_time", "device", "solar", "solar1", "solar2", "solar3", "solar4", "dc_status", "ac_status", "output", "pack_in", "pack_out"])
```

Expected result for the analyzed capture:

```text
No rows returned.
```

---

## Flux Query: Count Valid No-Energy Samples

```flux
from(bucket: "zendure_raw")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "zendure_device")
  |> filter(fn: (r) =>
    r._field == "solar" or
    r._field == "solar1" or
    r._field == "solar2" or
    r._field == "solar3" or
    r._field == "solar4" or
    r._field == "dc_status" or
    r._field == "ac_status" or
    r._field == "output" or
    r._field == "pack_in" or
    r._field == "pack_out"
  )
  |> pivot(rowKey:["_time", "device"], columnKey: ["_field"], valueColumn: "_value")
  |> filter(fn: (r) =>
    r.solar <= 1 and
    r.solar1 <= 1 and
    r.solar2 <= 1 and
    r.solar3 <= 1 and
    r.solar4 <= 1 and
    r.dc_status == 0 and
    r.ac_status == 0 and
    r.output <= 2 and
    r.pack_in <= 2 and
    r.pack_out <= 2
  )
  |> group(columns: ["device"])
  |> count(column: "output")
```

---

## Follow-up

Recommended next steps:

1. Confirm this observation over the multi-day capture.
2. Add a stable no-energy-path helper only after repeated confirmation.
3. Use this state in documentation and Mermaid diagrams as an inferred
   firmware/runtime state.
4. Do not replace current PV/pack/output checks with path-only logic yet.
