# Zendure Runtime Telemetry with InfluxDB

This workspace is for long-running, read-only telemetry capture and compact
event discovery. It is not part of the normal EMS control loop.

## Goals

- record Zendure and optional Shelly telemetry over hours or days
- keep raw data locally for later inspection
- use downsampling mainly for fast event search, not because raw retention must
  be aggressively reduced
- inspect only narrow raw windows after aggregate queries identify candidate
  events

The local InfluxDB instance may grow to roughly `50GB`. That is acceptable for
this workflow. The efficiency requirement is about search and analysis, not only
about storage.

## Setup

1. Copy the environment template:

   ```bash
   cp develop/influxdb/.env.example develop/influxdb/.env
   ```

2. Edit the local `.env` values.

3. Start InfluxDB:

   ```bash
   docker compose -f develop/influxdb/docker-compose.yml up -d
   ```

4. Create the search buckets and optionally backfill them from the raw bucket:

   ```bash
   python3 scripts/setup_influx_buckets.py \
     --env develop/influxdb/.env \
     --backfill-start=-24h
   ```

5. Import the Flux task files in the InfluxDB UI if you want automatic
   recurring downsampling tasks.

## Capture

Collector example:

```bash
python3 scripts/capture_runtime_to_influx.py \
  --config config.json \
  --env develop/influxdb/.env \
  --interval 5 \
  --duration 86400 \
  --include-runtime-state \
  --run-id sunrise-test-001
```

Properties:

- read-only Zendure polling
- optional Shelly capture
- optional read-only runtime-state capture
- no `EMSController`
- no `/properties/write`
- no Home Assistant writes
- no runtime-state mutation

## Event Discovery

Search aggregated data first:

```bash
python3 scripts/query_influx_events.py \
  --env develop/influxdb/.env \
  --start=-24h \
  --stop=now \
  --event pv-return
```

The query script prefers `INFLUXDB_BUCKET_1M` for event discovery and falls
back to the raw bucket when the 1-minute bucket does not exist yet.

Supported event types:

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

## Raw Window Inspection

Inspect raw windows only after event search:

```bash
python3 scripts/query_influx_window.py \
  --env develop/influxdb/.env \
  --start 2026-05-12T04:45:00Z \
  --stop 2026-05-12T05:05:00Z \
  --device WR1 \
  --fields solar,output,output_limit,soc,soc_limit,ac_status,dc_status,pack_state,pack_in,pack_out
```

Safety defaults:

- explicit `--start` and `--stop`
- explicit `--device` unless `--all-devices` is set
- default max raw window `30m`
- larger windows require `--allow-large-window`
- terminal output is capped; use `--csv-out` or `--jsonl-out` for large exports

## Workflow

1. Capture at least one full night and sunrise.
2. Use `query_influx_events.py` to find candidate transitions.
3. Use `query_influx_window.py` on small windows only.
4. Write confirmed findings to:
   - `docs/develop/runtime-firmware.md`
   - `docs/develop/observations.md`
5. Convert findings into follow-up code or control-logic tasks.

## Buckets

Use the Python setup helper for bucket creation and optional backfill:

```bash
python3 scripts/setup_influx_buckets.py \
  --env develop/influxdb/.env \
  --backfill-start=-24h
```

Useful variants:

```bash
python3 scripts/setup_influx_buckets.py \
  --env develop/influxdb/.env \
  --skip-backfill

python3 scripts/setup_influx_buckets.py \
  --env develop/influxdb/.env \
  --skip-15m \
  --backfill-start=-48h
```

The script:

- creates `zendure_1m` if missing
- creates `zendure_15m` if missing
- can backfill both buckets from `zendure_raw`
- is safe to rerun
