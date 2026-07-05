# Develop Tool: InfluxDB State Transition Analysis

This document describes how to analyze a copied 24h InfluxDB runtime capture for
firmware and runtime state transitions. The InfluxDB tooling is development-only
and is not part of the EMS control loop.

## Purpose

Start with factual observations before creating EMS logic tasks. Treat firmware
states as observed or inferred, not official Zendure state definitions.

Do not assume:

- missing data means zero power
- offline means output was zero
- one transition is enough evidence for a code change

Separate firmware behavior from EMS decisions. If `ems_runtime` was not
captured, mark EMS decision fields as unavailable.

## Required Local Setup

Use a copied local InfluxDB data directory at:

```text
develop/influxdb/data/
```

The `.env` values must match the copied database, especially:

```text
INFLUXDB_URL
INFLUXDB_ORG
INFLUXDB_TOKEN
INFLUXDB_BUCKET_RAW
```

Only raw values are required. Downsample buckets are useful but optional.

## Start With Copied Data

Start the local InfluxDB container:

```bash
docker compose -f develop/influxdb/docker-compose.yml up -d
```

Check API access without backfilling:

```bash
python3 scripts/setup_influx_buckets.py \
  --env develop/influxdb/.env \
  --check-connection
```

Do not reset `develop/influxdb/data/` during analysis unless you intentionally
want to delete the copied capture.

## Overview Queries

Use aggregate windows first. Inspect per device and avoid exporting full raw
data.

```flux
from(bucket: "zendure_raw")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "zendure_device")
  |> filter(fn: (r) => r._field == "solar" or r._field == "solar1" or r._field == "solar2" or r._field == "solar3" or r._field == "solar4" or r._field == "output" or r._field == "output_limit" or r._field == "pack_in" or r._field == "pack_out" or r._field == "soc")
  |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
  |> yield(name: "overview_1m")
```

For slower trend inspection:

```flux
from(bucket: "zendure_raw")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "zendure_device")
  |> filter(fn: (r) => r._field == "solar" or r._field == "output" or r._field == "output_limit" or r._field == "soc")
  |> aggregateWindow(every: 5m, fn: mean, createEmpty: false)
  |> yield(name: "overview_5m")
```

## Find Transition Windows

Use state-change queries for discrete fields:

```flux
from(bucket: "zendure_raw")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "zendure_device")
  |> filter(fn: (r) => r._field == "soc_limit" or r._field == "pack_state" or r._field == "ac_status" or r._field == "dc_status" or r._field == "grid_state")
  |> group(columns: ["device", "_field"])
  |> sort(columns: ["_time"])
  |> difference(nonNegative: false)
  |> filter(fn: (r) => exists r._value and r._value != 0)
  |> rename(columns: {_value: "delta"})
  |> yield(name: "state_changes")
```

For boolean `available`, convert to numeric before `difference()`:

```flux
from(bucket: "zendure_raw")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "zendure_device")
  |> filter(fn: (r) => r._field == "available")
  |> map(fn: (r) => ({r with _value: if r._value == true then 1 else 0}))
  |> group(columns: ["device", "_field"])
  |> sort(columns: ["_time"])
  |> difference(nonNegative: false)
  |> filter(fn: (r) => exists r._value and r._value != 0)
  |> rename(columns: {_value: "delta"})
  |> yield(name: "availability_changes")
```

Detect data gaps:

```flux
from(bucket: "zendure_raw")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "zendure_device")
  |> filter(fn: (r) => r._field == "available")
  |> group(columns: ["device"])
  |> sort(columns: ["_time"])
  |> elapsed(unit: 1s)
  |> filter(fn: (r) => r.elapsed > 30)
  |> yield(name: "data_gaps")
```

Helper script:

```bash
python3 scripts/analyze_influx_state_transitions.py \
  --env develop/influxdb/.env \
  --bucket zendure_raw \
  --range -24h \
  --output docs/develop-tool-influxdb-state-transition-observations.md
```

The helper reports transition candidates only. Confirm each candidate with raw
window inspection.

The transition tables use `delta`, not the actual new firmware status value.
`delta` is the numeric difference between consecutive samples after Flux
`difference()`. For binary status fields, `-1` usually means `1 -> 0` and `+1`
usually means `0 -> 1`. Do not infer firmware meaning from the delta alone.

## Key Windows To Identify

Evening PV shutdown:

- first stable window where `solar == 0` and `solar1..solar4 == 0`
- record output, output_limit, pack_in, pack_out, soc_limit, ac_status,
  dc_status, pack_state, and grid_state before and after

Night idle or minSoc idle:

- `solar == 0`
- `output == 0`
- `pack_in == 0`
- `pack_out == 0`
- `soc <= min_soc` or `soc_limit == 2`
- record whether telemetry and status fields continue
- record whether `available` stays true
- record EMS runtime fields only if `ems_runtime` was captured

Morning PV return:

- first `solar > 0` or any panel field greater than zero after night idle
- record delay until output or output_limit changes
- record dc_status, ac_status, pack_state changes
- record whether output resumes without manual action

Offline and reconnect:

- `available false -> true`
- data gaps above the expected capture interval
- record outage duration and whether values jump after reconnect

## Inspect Raw Data Around Transitions

Inspect only narrow windows, normally plus/minus 15 minutes around a candidate:

```bash
python3 scripts/query_influx_window.py \
  --env develop/influxdb/.env \
  --bucket zendure_raw \
  --start 2026-05-12T04:45:00Z \
  --stop 2026-05-12T05:15:00Z \
  --device WR1 \
  --fields solar,solar1,solar2,solar3,solar4,output,output_limit,pack_in,pack_out,soc,soc_limit,pack_state,ac_status,dc_status,grid_state,available
```

If runtime state was captured, inspect EMS runtime fields separately:

```flux
from(bucket: "zendure_raw")
  |> range(start: time(v: "2026-05-12T04:45:00Z"), stop: time(v: "2026-05-12T05:15:00Z"))
  |> filter(fn: (r) => r._measurement == "ems_runtime")
  |> filter(fn: (r) => r._field == "enabled" or r._field == "max_total_power" or r._field == "loop_interval" or r._field == "min_output_limit")
  |> yield(name: "runtime_window")
```

## Observation Template

```md
## Observation: <device> <transition>

- Date/time:
- Device:
- Transition type:
- Trigger field(s):
- Previous state:
- New state:
- PV values:
- Output/output_limit:
- Battery pack_in/pack_out:
- SOC/soc_limit:
- ac_status/dc_status:
- pack_state/grid_state:
- Telemetry continued: yes/no/unknown
- EMS behavior:
- Firmware inference:
- Recommended code change: none / candidate / required
- Confidence: low / medium / high
```

## Suggested Follow-Up

Before opening EMS logic tasks, complete this checklist:

- Which transitions were observed?
- Which transitions need another 24h capture?
- Which observations should update firmware-state docs?
- Which observations suggest EMS logic changes?
- Which observations are only informational?

Create separate tasks for any EMS logic change. Do not change controller logic
from a single unconfirmed transition.
