#!/usr/bin/env python3

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ems.logging_utils import log_event, setup_logging
from scripts.influx_utils import (
    InfluxHTTPClient,
    load_env_file,
    require_influx_api_env,
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Create and optionally backfill InfluxDB buckets for Zendure telemetry"
    )
    parser.add_argument("--env", required=True, help="Path to InfluxDB .env file")
    parser.add_argument(
        "--backfill-start",
        default="-24h",
        help="Flux range start for optional backfill"
    )
    parser.add_argument(
        "--skip-backfill",
        action="store_true",
        help="Create buckets only"
    )
    parser.add_argument(
        "--skip-1m",
        action="store_true",
        help="Skip the zendure_1m bucket"
    )
    parser.add_argument(
        "--skip-15m",
        action="store_true",
        help="Skip the zendure_15m bucket"
    )
    parser.add_argument(
        "--log-level",
        default="info",
        help="Logging level"
    )
    parser.add_argument(
        "--check-connection",
        action="store_true",
        help="Validate URL, token, org, and raw bucket, then exit"
    )
    return parser.parse_args()


def backfill_flux(source_bucket, target_bucket, every, start_literal, org_name):
    return f'''
from(bucket: "{source_bucket}")
  |> range(start: {start_literal})
  |> filter(fn: (r) => r._measurement == "zendure_device" or r._measurement == "shelly_meter" or r._measurement == "ems_runtime")
  |> aggregateWindow(every: {every}, fn: last, createEmpty: false)
  |> to(bucket: "{target_bucket}", org: "{org_name}")
'''.strip()


def ensure_bucket(client, bucket_name):
    bucket, created = client.ensure_bucket(bucket_name, retention_seconds=0)
    log_event(
        logging.INFO,
        "influx_bucket_ready",
        bucket=bucket_name,
        created=created,
        bucket_id=bucket.get("id", "")
    )


def check_connection(client, raw_bucket):
    org_id = client.get_org_id()
    bucket, created = client.ensure_bucket(raw_bucket, retention_seconds=0)
    log_event(
        logging.INFO,
        "influx_connection_ok",
        org_found=True,
        org_id=org_id,
        raw_bucket=raw_bucket,
        raw_bucket_created=created,
        raw_bucket_id=bucket.get("id", "")
    )


def run_backfill(client, source_bucket, target_bucket, every, start_literal, org_name):
    client.query_raw(
        backfill_flux(
            source_bucket,
            target_bucket,
            every,
            start_literal,
            org_name
        )
    )
    log_event(
        logging.INFO,
        "influx_bucket_backfilled",
        source_bucket=source_bucket,
        target_bucket=target_bucket,
        every=every,
        start=start_literal
    )


def main():
    args = parse_args()
    setup_logging(args.log_level)

    env_values = load_env_file(args.env)
    required_keys = [
        "INFLUXDB_URL",
        "INFLUXDB_ORG",
        "INFLUXDB_TOKEN",
        "INFLUXDB_BUCKET_RAW",
    ]

    if not args.check_connection:
        required_keys.extend([
            "INFLUXDB_BUCKET_1M",
            "INFLUXDB_BUCKET_15M",
        ])

    try:
        require_influx_api_env(
            env_values,
            *required_keys,
        )
    except ValueError as exc:
        raise SystemExit(str(exc))

    client = InfluxHTTPClient(
        env_values["INFLUXDB_URL"],
        env_values["INFLUXDB_ORG"],
        env_values["INFLUXDB_TOKEN"]
    )
    raw_bucket = env_values["INFLUXDB_BUCKET_RAW"]

    if args.check_connection:
        check_connection(client, raw_bucket)
        return

    log_event(
        logging.INFO,
        "influx_bucket_setup_started",
        raw_bucket=raw_bucket,
        backfill=not args.skip_backfill,
        backfill_start=args.backfill_start
    )

    if not args.skip_1m:
        bucket_1m = env_values["INFLUXDB_BUCKET_1M"]
        ensure_bucket(client, bucket_1m)
        if not args.skip_backfill:
            run_backfill(
                client,
                raw_bucket,
                bucket_1m,
                "1m",
                args.backfill_start,
                env_values["INFLUXDB_ORG"]
            )

    if not args.skip_15m:
        bucket_15m = env_values["INFLUXDB_BUCKET_15M"]
        ensure_bucket(client, bucket_15m)
        if not args.skip_backfill:
            run_backfill(
                client,
                raw_bucket,
                bucket_15m,
                "15m",
                args.backfill_start,
                env_values["INFLUXDB_ORG"]
            )

    log_event(logging.INFO, "influx_bucket_setup_finished")


if __name__ == "__main__":
    main()
