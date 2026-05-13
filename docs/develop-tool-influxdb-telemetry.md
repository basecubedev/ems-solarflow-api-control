# Develop Tool: InfluxDB Runtime Telemetry

This document describes the optional InfluxDB telemetry tooling under
`develop/influxdb/` and `scripts/`.

This is a development and analysis feature. It is not part of the normal EMS
control loop and it is not required for operating the EMS.

## Purpose

The toolset captures Zendure runtime telemetry over longer periods to improve
understanding of firmware behavior.

Typical use cases:

- night behavior
- sunrise and PV return
- minSoc and idle behavior
- AC/DC state transitions
- pack state transitions
- output limit versus actual output
- fault telemetry versus real export capability

The goal is to document firmware behavior and derive better EMS logic later. It
does not directly influence live EMS control decisions.

## Safety Model

The collector is intentionally read-only.

It:

- reads Zendure `/properties/report`
- optionally reads Shelly load
- optionally reads `runtime-state.json`
- writes telemetry to local InfluxDB

It does not:

- instantiate `EMSController`
- call Zendure `/properties/write`
- modify `runtime-state.json`
- modify `config.json`
- write Home Assistant states

## Components

Main artifacts:

- `develop/influxdb/docker-compose.yml`
- `develop/influxdb/.env.example`
- `develop/influxdb/README.md`
- `develop/influxdb/tasks/downsample_1m.flux`
- `develop/influxdb/tasks/downsample_15m.flux`
- `scripts/capture_runtime_to_influx.py`
- `scripts/setup_influx_buckets.py`
- `scripts/query_influx_events.py`
- `scripts/query_influx_window.py`

## Setup

Copy the local environment file:

```bash
cp develop/influxdb/.env.example develop/influxdb/.env
```

`INFLUXDB_TOKEN` is the API token used by the Python scripts. The InfluxDB web
UI login uses `INFLUXDB_ADMIN_USER` and `INFLUXDB_ADMIN_PASSWORD`. For simple
local development, keep `INFLUXDB_TOKEN` equal to `INFLUXDB_ADMIN_TOKEN` unless
you intentionally create a separate API token.

Start InfluxDB:

```bash
docker compose -f develop/influxdb/docker-compose.yml up -d
```

Create the downsample buckets:

```bash
python3 scripts/setup_influx_buckets.py \
  --env develop/influxdb/.env \
  --backfill-start=-24h
```

Connection check:

```bash
python3 scripts/setup_influx_buckets.py \
  --env develop/influxdb/.env \
  --check-connection
```

This setup helper:

- creates `zendure_1m` if missing
- creates `zendure_15m` if missing
- can backfill both buckets from `zendure_raw`
- is safe to rerun

Docker first-start values are applied only when `develop/influxdb/data/` is
created. Changing `.env` later does not update an already initialized
username/password/token. To reset a local development instance:

```bash
docker compose -f develop/influxdb/docker-compose.yml down
rm -rf develop/influxdb/data
docker compose -f develop/influxdb/docker-compose.yml up -d
```

This deletes locally captured InfluxDB data.

## Capture

Example capture for one day:

```bash
python3 scripts/capture_runtime_to_influx.py \
  --config config.json \
  --env develop/influxdb/.env \
  --interval 5 \
  --duration 86400 \
  --include-runtime-state \
  --run-id sunrise-test-001
```

Important flags:

- `--interval`: poll interval in seconds
- `--duration`: bounded capture duration
- `--run-id`: optional tag for separating capture sessions
- `--include-runtime-state`: include read-only runtime-state snapshots
- `--skip-shelly`: disable Shelly capture if needed

## Event Discovery

Use the compact event query first. This avoids scanning or printing large raw
ranges.

Example:

```bash
python3 scripts/query_influx_events.py \
  --env develop/influxdb/.env \
  --start=-24h \
  --stop=now \
  --event all
```

Supported event groups:

- `pv-return`
- `pv-drop`
- `soc-limit-change`
- `pack-state-change`
- `ac-status-change`
- `dc-status-change`
- `fault-active`
- `output-mismatch`
- `pv-but-no-output`
- `output-while-dc-inactive`
- `idle-with-output-limit`
- `battery-flow-during-idle`

Bucket behavior:

- the script prefers `INFLUXDB_BUCKET_1M`
- if the 1-minute bucket is missing, it falls back to the raw bucket

## Raw Window Inspection

After event discovery, inspect only a small raw window.

Example:

```bash
python3 scripts/query_influx_window.py \
  --env develop/influxdb/.env \
  --bucket zendure_raw \
  --start=-10m \
  --stop=now \
  --device WR1 \
  --fields solar,output,output_limit,soc,soc_limit,ac_status,dc_status,pack_state,pack_in,pack_out
```

Behavior:

- explicit `--start` and `--stop` required
- explicit `--device` required unless `--all-devices` is set
- default maximum raw window is `30m`
- larger windows require `--allow-large-window`
- terminal output is pivoted by timestamp to make multiple fields readable
- larger exports should go to `--csv-out` or `--jsonl-out`

## Downsampling

Downsampling is used mainly for fast event discovery and token-efficient
analysis, not primarily for storage reduction.

The local InfluxDB can grow large, for example up to roughly `50GB`, but the
workflow should still be:

1. search aggregate buckets first
2. identify interesting timestamps
3. inspect only small raw windows

Example backfill into the 1-minute bucket:

```bash
python3 scripts/setup_influx_buckets.py \
  --env develop/influxdb/.env \
  --skip-15m \
  --backfill-start=-24h
```

## Typical Workflow

1. Start InfluxDB.
2. Run capture for at least one night and sunrise.
3. Query `zendure_1m` for candidate events.
4. Inspect only narrow raw windows around those events.
5. Document findings in:
   - `docs/develop/runtime-firmware.md`
   - `docs/develop/observations.md`
6. Derive follow-up EMS changes from confirmed firmware behavior.

## Stop

Stop the local InfluxDB stack when finished:

```bash
docker compose -f develop/influxdb/docker-compose.yml down
```

## Scope Reminder

This feature helps understand firmware behavior and supports later EMS design
decisions.

It is not a direct operating feature of the EMS and it should not be confused
with the normal control path used for production output regulation.
