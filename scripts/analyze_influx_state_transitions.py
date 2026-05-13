#!/usr/bin/env python3

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.influx_utils import (
    InfluxHTTPClient,
    flux_time_literal,
    load_env_file,
    normalize_negative_option_args,
    parse_influx_csv,
    require_influx_api_env,
)


STATE_FIELDS = [
    "soc_limit",
    "pack_state",
    "ac_status",
    "dc_status",
    "grid_state",
]


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Summarize InfluxDB state transition candidates from Zendure telemetry"
    )
    parser.add_argument("--env", required=True, help="Path to InfluxDB .env file")
    parser.add_argument("--bucket", default="", help="Raw bucket name override")
    parser.add_argument(
        "--range",
        dest="range_start",
        default="-24h",
        help="Flux range start, for example -24h"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum rows per transition table"
    )
    parser.add_argument(
        "--gap-seconds",
        type=int,
        default=30,
        help="Minimum per-device data gap to report"
    )
    parser.add_argument(
        "--output",
        default="",
        help="Optional Markdown output path"
    )
    return parser.parse_args(argv[1:])


def field_filter(fields):
    return " or ".join(
        f'r._field == {json.dumps(field)}'
        for field in fields
    )


def state_change_flux(bucket, range_start, limit):
    return f'''
from(bucket: {json.dumps(bucket)})
  |> range(start: {flux_time_literal(range_start)})
  |> filter(fn: (r) => r._measurement == "zendure_device")
  |> filter(fn: (r) => {field_filter(STATE_FIELDS)})
  |> group(columns: ["device", "_field"])
  |> sort(columns: ["_time"])
  |> difference(nonNegative: false)
  |> filter(fn: (r) => exists r._value and r._value != 0)
  |> keep(columns: ["_time", "device", "_field", "_value"])
  |> limit(n: {int(limit)})
'''.strip()


def availability_change_flux(bucket, range_start, limit):
    return f'''
from(bucket: {json.dumps(bucket)})
  |> range(start: {flux_time_literal(range_start)})
  |> filter(fn: (r) => r._measurement == "zendure_device")
  |> filter(fn: (r) => r._field == "available")
  |> map(fn: (r) => ({{r with _value: if r._value == true then 1 else 0}}))
  |> group(columns: ["device", "_field"])
  |> sort(columns: ["_time"])
  |> difference(nonNegative: false)
  |> filter(fn: (r) => exists r._value and r._value != 0)
  |> keep(columns: ["_time", "device", "_field", "_value"])
  |> limit(n: {int(limit)})
'''.strip()


def data_gap_flux(bucket, range_start, gap_seconds, limit):
    return f'''
from(bucket: {json.dumps(bucket)})
  |> range(start: {flux_time_literal(range_start)})
  |> filter(fn: (r) => r._measurement == "zendure_device")
  |> filter(fn: (r) => r._field == "available")
  |> group(columns: ["device"])
  |> sort(columns: ["_time"])
  |> elapsed(unit: 1s)
  |> filter(fn: (r) => r.elapsed > {int(gap_seconds)})
  |> keep(columns: ["_time", "device", "elapsed"])
  |> limit(n: {int(limit)})
'''.strip()


def query_rows(client, flux):
    return parse_influx_csv(client.query_csv(flux))


def markdown_table(title, rows, columns):
    lines = [f"## {title}", ""]

    if not rows:
        lines.extend(["No candidates found.", ""])
        return lines

    lines.append("| " + " | ".join(columns) + " |")
    lines.append("| " + " | ".join("---" for _ in columns) + " |")

    for row in rows:
        lines.append(
            "| "
            + " | ".join(str(row.get(column, "")) for column in columns)
            + " |"
        )

    lines.append("")
    return lines


def build_report(bucket, range_start, state_rows, availability_rows, gap_rows):
    lines = [
        "# InfluxDB State Transition Candidates",
        "",
        f"- Bucket: `{bucket}`",
        f"- Range: `{range_start}`",
        "",
        "These are transition candidates. Confirm firmware behavior by",
        "inspecting raw values around each timestamp before creating EMS logic",
        "changes.",
        "",
    ]
    lines.extend(
        markdown_table(
            "Discrete State Changes",
            state_rows,
            ["_time", "device", "_field", "_value"]
        )
    )
    lines.extend(
        markdown_table(
            "Availability Changes",
            availability_rows,
            ["_time", "device", "_field", "_value"]
        )
    )
    lines.extend(
        markdown_table(
            "Data Gaps",
            gap_rows,
            ["_time", "device", "elapsed"]
        )
    )
    lines.extend([
        "## Next Step",
        "",
        "For each candidate timestamp, inspect raw values in a narrow window,",
        "for example `--start <timestamp-minus-15m> --stop <timestamp-plus-15m>`",
        "with `scripts/query_influx_window.py`.",
        "",
    ])
    return "\n".join(lines)


def main(argv=None):
    argv = normalize_negative_option_args(
        argv or sys.argv,
        {"--range"}
    )
    args = parse_args(argv)

    env_values = load_env_file(args.env)
    try:
        require_influx_api_env(
            env_values,
            "INFLUXDB_URL",
            "INFLUXDB_ORG",
            "INFLUXDB_TOKEN",
            "INFLUXDB_BUCKET_RAW",
        )
    except ValueError as exc:
        raise SystemExit(str(exc))

    bucket = args.bucket or env_values["INFLUXDB_BUCKET_RAW"]
    client = InfluxHTTPClient(
        env_values["INFLUXDB_URL"],
        env_values["INFLUXDB_ORG"],
        env_values["INFLUXDB_TOKEN"]
    )

    state_rows = query_rows(
        client,
        state_change_flux(bucket, args.range_start, args.limit)
    )
    availability_rows = query_rows(
        client,
        availability_change_flux(bucket, args.range_start, args.limit)
    )
    gap_rows = query_rows(
        client,
        data_gap_flux(bucket, args.range_start, args.gap_seconds, args.limit)
    )

    report = build_report(
        bucket,
        args.range_start,
        state_rows,
        availability_rows,
        gap_rows
    )

    if args.output:
        Path(args.output).write_text(report + "\n")
    else:
        print(report)


if __name__ == "__main__":
    main()
