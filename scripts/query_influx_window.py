#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

import argparse
import csv
import json
import sys
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.influx_utils import (
    InfluxHTTPClient,
    coerce_value,
    ensure_parent_dir,
    flux_time_literal,
    load_env_file,
    normalize_negative_option_args,
    parse_influx_csv,
    parse_time_value,
    require_env,
)


DEFAULT_FIELDS = [
    "solar",
    "output",
    "output_limit",
    "soc",
    "soc_limit",
    "ac_status",
    "dc_status",
    "pack_state",
    "pack_in",
    "pack_out",
]


def parse_args():
    argv = normalize_negative_option_args(
        sys.argv,
        {"--start", "--stop"}
    )
    parser = argparse.ArgumentParser(
        description="Bounded raw window query for InfluxDB telemetry"
    )
    parser.add_argument("--env", required=True, help="Path to InfluxDB .env file")
    parser.add_argument("--start", required=True, help="Explicit start time")
    parser.add_argument("--stop", required=True, help="Explicit stop time")
    parser.add_argument("--device", default="", help="Device name")
    parser.add_argument(
        "--all-devices",
        action="store_true",
        help="Allow querying all devices in one window"
    )
    parser.add_argument(
        "--fields",
        default=",".join(DEFAULT_FIELDS),
        help="Comma-separated field list"
    )
    parser.add_argument(
        "--measurement",
        default="zendure_device",
        help="Measurement name"
    )
    parser.add_argument(
        "--bucket",
        default="",
        help="Override raw bucket"
    )
    parser.add_argument(
        "--allow-large-window",
        action="store_true",
        help="Allow windows larger than 30 minutes"
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=500,
        help="Maximum rows to print to terminal"
    )
    parser.add_argument("--csv-out", default="", help="Optional CSV export path")
    parser.add_argument("--jsonl-out", default="", help="Optional JSONL export path")
    return parser.parse_args(argv[1:])


def validate_args(args):
    if not args.device and not args.all_devices:
        raise SystemExit("Use --device or --all-devices")

    start = parse_time_value(args.start, now=datetime.now(timezone.utc))
    stop = parse_time_value(args.stop, now=datetime.now(timezone.utc))

    if stop <= start:
        raise SystemExit("--stop must be after --start")

    if stop - start > timedelta(minutes=30) and not args.allow_large_window:
        raise SystemExit(
            "Raw window exceeds 30m. Use --allow-large-window for larger exports."
        )

    return start, stop


def build_query(bucket, measurement, start, stop, fields, device, all_devices):
    field_filter = " or ".join(
        f'r._field == "{field}"'
        for field in fields
    )
    device_filter = ""

    if measurement == "zendure_device" and not all_devices and device:
        device_filter = f'\n  |> filter(fn: (r) => r.device == "{device}")'

    return f'''
from(bucket: "{bucket}")
  |> range(start: {flux_time_literal(start)}, stop: {flux_time_literal(stop)})
  |> filter(fn: (r) => r._measurement == "{measurement}")
  |> filter(fn: (r) => {field_filter}){device_filter}
  |> keep(columns: ["_time", "device", "source", "run_id", "_field", "_value"])
  |> sort(columns: ["_time", "device", "_field"])
'''.strip()


def write_csv(path, rows):
    ensure_parent_dir(path)

    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["_time", "device", "source", "run_id", "_field", "_value"]
        )
        writer.writeheader()
        writer.writerows(rows)


def write_jsonl(path, rows):
    ensure_parent_dir(path)

    with open(path, "w") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def pivot_rows(rows, fields):
    pivoted = OrderedDict()

    for row in rows:
        key = (
            row.get("_time", ""),
            row.get("device", ""),
            row.get("source", ""),
            row.get("run_id", "")
        )
        target = pivoted.setdefault(
            key,
            {
                "time": key[0],
                "device": key[1],
                "source": key[2],
                "run_id": key[3]
            }
        )
        target[row.get("_field", "")] = row.get("_value")

    result = []

    for item in pivoted.values():
        for field in fields:
            item.setdefault(field, "")
        result.append(item)

    return result


def print_rows(rows, fields, max_rows):
    wide_rows = pivot_rows(rows, fields)
    print("\t".join(["time", "device", "source", "run_id"] + fields))

    for row in wide_rows[:max_rows]:
        values = [
            str(row.get("time", "")),
            str(row.get("device", "")),
            str(row.get("source", "")),
            str(row.get("run_id", "")),
        ] + [str(row.get(field, "")) for field in fields]
        print("\t".join(values))

    if len(wide_rows) > max_rows:
        print(f"... truncated terminal output to {max_rows} rows")


def main():
    args = parse_args()
    validate_args(args)

    env_values = load_env_file(args.env)
    require_env(
        env_values,
        "INFLUXDB_URL",
        "INFLUXDB_ORG",
        "INFLUXDB_TOKEN",
        "INFLUXDB_BUCKET_RAW",
    )

    bucket = args.bucket or env_values["INFLUXDB_BUCKET_RAW"]
    client = InfluxHTTPClient(
        env_values["INFLUXDB_URL"],
        env_values["INFLUXDB_ORG"],
        env_values["INFLUXDB_TOKEN"]
    )
    fields = [field.strip() for field in args.fields.split(",") if field.strip()]
    raw_rows = parse_influx_csv(
        client.query_csv(
            build_query(
                bucket,
                args.measurement,
                args.start,
                args.stop,
                fields,
                args.device,
                args.all_devices
            )
        )
    )

    rows = []
    for row in raw_rows:
        coerced = dict(row)
        coerced["_value"] = coerce_value(row.get("_value"))
        rows.append(coerced)

    if args.csv_out:
        write_csv(args.csv_out, rows)

    if args.jsonl_out:
        write_jsonl(args.jsonl_out, rows)

    print_rows(rows, fields, args.max_rows)


if __name__ == "__main__":
    main()
