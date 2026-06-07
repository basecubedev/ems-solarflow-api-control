#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.influx_utils import (
    InfluxHTTPClient,
    coerce_value,
    flux_time_literal,
    load_env_file,
    normalize_negative_option_args,
    parse_influx_csv,
    require_env,
)


STATE_CHANGE_FIELDS = {
    "soc-limit-change": "soc_limit",
    "pack-state-change": "pack_state",
    "ac-status-change": "ac_status",
    "dc-status-change": "dc_status",
}

SUPPORTED_EVENTS = [
    "pv-return",
    "pv-drop",
    "soc-limit-change",
    "pack-state-change",
    "ac-status-change",
    "dc-status-change",
    "fault-active",
    "output-mismatch",
    "pv-but-no-output",
    "output-while-dc-inactive",
    "idle-with-output-limit",
    "battery-flow-during-idle",
]


def parse_args():
    argv = normalize_negative_option_args(
        sys.argv,
        {"--start", "--stop"}
    )
    parser = argparse.ArgumentParser(
        description="Compact InfluxDB event discovery for Zendure telemetry"
    )
    parser.add_argument("--env", required=True, help="Path to InfluxDB .env file")
    parser.add_argument("--start", default="-24h", help="Flux range start")
    parser.add_argument("--stop", default="now", help="Flux range stop")
    parser.add_argument("--device", default="", help="Optional device filter")
    parser.add_argument(
        "--event",
        default="all",
        choices=["all"] + SUPPORTED_EVENTS,
        help="Event class to search"
    )
    parser.add_argument(
        "--bucket",
        default="",
        help="Override search bucket. Defaults to INFLUXDB_BUCKET_1M or raw."
    )
    parser.add_argument(
        "--interval",
        default="1m",
        help="Aggregate interval used for search queries"
    )
    return parser.parse_args(argv[1:])


def build_field_query(bucket, start, stop, fields, every, fn_name, device=""):
    field_filter = " or ".join(
        f'r._field == "{field}"'
        for field in fields
    )
    device_filter = ""

    if device:
        device_filter = f'\n  |> filter(fn: (r) => r.device == "{device}")'

    return f'''
from(bucket: "{bucket}")
  |> range(start: {flux_time_literal(start)}, stop: {flux_time_literal(stop)})
  |> filter(fn: (r) => r._measurement == "zendure_device")
  |> filter(fn: (r) => {field_filter}){device_filter}
  |> aggregateWindow(every: {every}, fn: {fn_name}, createEmpty: false)
  |> keep(columns: ["_time", "device", "_field", "_value"])
  |> sort(columns: ["device", "_time"])
'''.strip()


def series_by_device(rows):
    data = defaultdict(list)

    for row in rows:
        device = row.get("device", "")
        data[device].append(
            {
                "time": row.get("_time"),
                "field": row.get("_field"),
                "value": coerce_value(row.get("_value"))
            }
        )

    return data


def merged_timeline(rows):
    timeline = defaultdict(lambda: defaultdict(dict))

    for row in rows:
        timeline[row.get("device", "")][row.get("_time")][row.get("_field")] = (
            coerce_value(row.get("_value"))
        )

    return timeline


def emit_events(events):
    print("timestamp\tdevice\tevent_type\tkey_before\tkey_after\tshort_reason")

    for event in sorted(events, key=lambda item: (item["timestamp"], item["device"], item["event_type"])):
        print(
            f'{event["timestamp"]}\t{event["device"]}\t{event["event_type"]}\t'
            f'{event["key_before"]}\t{event["key_after"]}\t{event["short_reason"]}'
        )


def query_rows_with_bucket_fallback(client, primary_bucket, fallback_bucket, query_builder):
    tried = []

    for bucket in [primary_bucket, fallback_bucket]:
        if not bucket or bucket in tried:
            continue

        tried.append(bucket)

        try:
            return parse_influx_csv(query_builder(bucket)), bucket
        except requests.exceptions.HTTPError as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)

            if status_code == 404 and bucket != fallback_bucket:
                continue

            raise

    return [], fallback_bucket


def detect_state_change(rows, event_type):
    field = STATE_CHANGE_FIELDS[event_type]
    events = []

    for device, items in series_by_device(rows).items():
        previous_value = None

        for item in items:
            if item["field"] != field:
                continue

            current = item["value"]

            if previous_value is not None and current != previous_value:
                events.append(
                    {
                        "timestamp": item["time"],
                        "device": device,
                        "event_type": event_type,
                        "key_before": f"{field}={previous_value}",
                        "key_after": f"{field}={current}",
                        "short_reason": f"{field} changed"
                    }
                )

            previous_value = current

    return events


def detect_pv_return(rows, zero_threshold=5, pv_threshold=20, zero_minutes=30):
    events = []

    for device, items in series_by_device(rows).items():
        zero_run = 0

        for item in items:
            current = item["value"] or 0

            if current <= zero_threshold:
                zero_run += 1
                continue

            if zero_run >= zero_minutes and current > pv_threshold:
                events.append(
                    {
                        "timestamp": item["time"],
                        "device": device,
                        "event_type": "pv-return",
                        "key_before": f"solar<={zero_threshold} for {zero_run}m",
                        "key_after": f"solar={current}",
                        "short_reason": "PV returned after sustained zero phase"
                    }
                )

            zero_run = 0

    return events


def detect_pv_drop(rows, zero_threshold=5, pv_threshold=20):
    events = []

    for device, items in series_by_device(rows).items():
        previous = None

        for item in items:
            current = item["value"] or 0

            if previous is not None and previous > pv_threshold and current <= zero_threshold:
                events.append(
                    {
                        "timestamp": item["time"],
                        "device": device,
                        "event_type": "pv-drop",
                        "key_before": f"solar={previous}",
                        "key_after": f"solar={current}",
                        "short_reason": "PV dropped to near zero"
                    }
                )

            previous = current

    return events


def detect_fault_active(rows):
    events = []

    for device, items in series_by_device(rows).items():
        previous = 0

        for item in items:
            current = item["value"] or 0

            if previous == 0 and current > 0:
                events.append(
                    {
                        "timestamp": item["time"],
                        "device": device,
                        "event_type": "fault-active",
                        "key_before": "fault_level=0",
                        "key_after": f"fault_level={current}",
                        "short_reason": "Fault became active"
                    }
                )

            previous = current

    return events


def detect_timeline_condition(rows, event_type):
    events = []

    for device, points in merged_timeline(rows).items():
        consecutive = 0
        sorted_points = sorted(points.items())

        for timestamp, values in sorted_points:
            output_limit = values.get("output_limit", 0) or 0
            output = values.get("output", 0) or 0
            solar = values.get("solar", 0) or 0
            dc_status = values.get("dc_status", 0) or 0
            pack_in = values.get("pack_in", 0) or 0
            pack_out = values.get("pack_out", 0) or 0

            match = False
            reason = ""
            before = ""
            after = ""

            if event_type == "output-mismatch":
                match = output_limit >= 30 and output <= 5
                reason = "output limit active but output near zero"
                before = f"output_limit={output_limit}"
                after = f"output={output}"
            elif event_type == "pv-but-no-output":
                match = solar > 20 and output <= 5
                reason = "PV present while output stays near zero"
                before = f"solar={solar}"
                after = f"output={output}"
            elif event_type == "output-while-dc-inactive":
                match = output > 5 and dc_status == 0
                reason = "Output active while dc_status is inactive"
                before = f"dc_status={dc_status}"
                after = f"output={output}"
            elif event_type == "idle-with-output-limit":
                match = output_limit >= 30 and output <= 5 and solar <= 5
                reason = "Idle-like state with output limit still set"
                before = f"solar={solar},output_limit={output_limit}"
                after = f"output={output}"
            elif event_type == "battery-flow-during-idle":
                match = solar <= 5 and output <= 5 and (abs(pack_in) > 10 or abs(pack_out) > 10)
                reason = "Battery flow detected during idle-like state"
                before = f"pack_in={pack_in}"
                after = f"pack_out={pack_out}"

            if match:
                consecutive += 1
            else:
                consecutive = 0

            if event_type in {"output-mismatch", "pv-but-no-output", "idle-with-output-limit"} and consecutive < 2:
                continue

            if not match:
                continue

            if events and events[-1]["timestamp"] == timestamp and events[-1]["device"] == device and events[-1]["event_type"] == event_type:
                continue

            events.append(
                {
                    "timestamp": timestamp,
                    "device": device,
                    "event_type": event_type,
                    "key_before": before,
                    "key_after": after,
                    "short_reason": reason
                }
            )

    return events


def main():
    args = parse_args()
    env_values = load_env_file(args.env)
    require_env(
        env_values,
        "INFLUXDB_URL",
        "INFLUXDB_ORG",
        "INFLUXDB_TOKEN",
        "INFLUXDB_BUCKET_RAW",
    )

    bucket = (
        args.bucket
        or env_values.get("INFLUXDB_BUCKET_1M")
        or env_values["INFLUXDB_BUCKET_RAW"]
    )
    raw_bucket = env_values["INFLUXDB_BUCKET_RAW"]
    client = InfluxHTTPClient(
        env_values["INFLUXDB_URL"],
        env_values["INFLUXDB_ORG"],
        env_values["INFLUXDB_TOKEN"]
    )

    requested_events = SUPPORTED_EVENTS if args.event == "all" else [args.event]
    all_events = []

    for event_name in requested_events:
        if event_name in STATE_CHANGE_FIELDS:
            rows, _used_bucket = query_rows_with_bucket_fallback(
                client,
                bucket,
                raw_bucket,
                lambda selected_bucket: client.query_csv(
                    build_field_query(
                        selected_bucket,
                        args.start,
                        args.stop,
                        [STATE_CHANGE_FIELDS[event_name]],
                        args.interval,
                        "last",
                        args.device
                    )
                )
            )
            all_events.extend(detect_state_change(rows, event_name))
            continue

        if event_name == "pv-return":
            rows, _used_bucket = query_rows_with_bucket_fallback(
                client,
                bucket,
                raw_bucket,
                lambda selected_bucket: client.query_csv(
                    build_field_query(
                        selected_bucket,
                        args.start,
                        args.stop,
                        ["solar"],
                        args.interval,
                        "max",
                        args.device
                    )
                )
            )
            all_events.extend(detect_pv_return(rows))
            continue

        if event_name == "pv-drop":
            rows, _used_bucket = query_rows_with_bucket_fallback(
                client,
                bucket,
                raw_bucket,
                lambda selected_bucket: client.query_csv(
                    build_field_query(
                        selected_bucket,
                        args.start,
                        args.stop,
                        ["solar"],
                        args.interval,
                        "max",
                        args.device
                    )
                )
            )
            all_events.extend(detect_pv_drop(rows))
            continue

        if event_name == "fault-active":
            rows, _used_bucket = query_rows_with_bucket_fallback(
                client,
                bucket,
                raw_bucket,
                lambda selected_bucket: client.query_csv(
                    build_field_query(
                        selected_bucket,
                        args.start,
                        args.stop,
                        ["fault_level"],
                        args.interval,
                        "max",
                        args.device
                    )
                )
            )
            all_events.extend(detect_fault_active(rows))
            continue

        rows, _used_bucket = query_rows_with_bucket_fallback(
            client,
            bucket,
            raw_bucket,
            lambda selected_bucket: client.query_csv(
                build_field_query(
                    selected_bucket,
                    args.start,
                    args.stop,
                    ["solar", "output", "output_limit", "dc_status", "pack_in", "pack_out"],
                    args.interval,
                    "last",
                    args.device
                )
            )
        )
        all_events.extend(detect_timeline_condition(rows, event_name))

    emit_events(all_events)


if __name__ == "__main__":
    main()
