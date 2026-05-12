#!/usr/bin/env python3

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ems.clients import ShellyClient, ZendureClient, create_session, fetch_all_devices
from ems.logging_utils import log_event, setup_logging
from scripts.influx_utils import InfluxHTTPClient, build_line_protocol, load_env_file, require_env


def parse_args():
    parser = argparse.ArgumentParser(
        description="Read-only Zendure runtime collector for InfluxDB 2"
    )
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--env", required=True, help="Path to InfluxDB .env file")
    parser.add_argument("--interval", type=float, default=5.0, help="Poll interval in seconds")
    parser.add_argument("--duration", type=float, default=0, help="Optional total run time in seconds")
    parser.add_argument("--run-id", default="", help="Optional capture run id tag")
    parser.add_argument("--bucket", default="", help="Override raw bucket name")
    parser.add_argument(
        "--runtime-state-path",
        default="",
        help="Optional runtime-state path override for read-only capture"
    )
    parser.add_argument(
        "--include-runtime-state",
        action="store_true",
        help="Capture runtime-state.json contents in read-only mode"
    )
    parser.add_argument(
        "--skip-shelly",
        action="store_true",
        help="Skip Shelly reads even when configured"
    )
    parser.add_argument(
        "--log-level",
        default="info",
        help="Logging level"
    )
    return parser.parse_args()


def load_config(path):
    with open(path) as handle:
        return json.load(handle)


def read_runtime_state(path):
    if not path or not os.path.exists(path):
        return None

    with open(path) as handle:
        return json.load(handle)


def build_device_fields(device_state):
    return {
        "available": True,
        "soc": device_state.soc,
        "min_soc": device_state.min_soc,
        "max_soc": device_state.max_soc,
        "solar": device_state.solar,
        "solar1": device_state.solar1,
        "solar2": device_state.solar2,
        "solar3": device_state.solar3,
        "solar4": device_state.solar4,
        "output": device_state.output,
        "output_limit": device_state.output_limit,
        "pack_in": device_state.pack_in,
        "pack_out": device_state.pack_out,
        "soc_limit": device_state.soc_limit,
        "pack_state": device_state.pack_state,
        "fault_level": device_state.fault_level,
        "smart_mode": device_state.smart_mode,
        "grid_off_mode": device_state.grid_off_mode,
        "ac_mode": device_state.ac_mode,
        "ac_status": device_state.ac_status,
        "dc_status": device_state.dc_status,
        "grid_state": device_state.grid_state,
        "rssi": device_state.rssi,
        "voltage": device_state.voltage,
        "temp": device_state.temp,
        "remain_minutes": device_state.remain_minutes,
        "pv_present": device_state.solar > 0 or any(
            panel > 0
            for panel in (
                device_state.solar1,
                device_state.solar2,
                device_state.solar3,
                device_state.solar4,
            )
        ),
        "output_active": device_state.output > 0,
        "fault_active": device_state.fault_level > 0,
    }


def runtime_state_points(runtime_state, run_id, timestamp_ns):
    lines = []

    system = runtime_state.get("system", {})
    if isinstance(system, dict):
        line = build_line_protocol(
            "ems_runtime",
            {"source": "ems", "scope": "system", "run_id": run_id},
            {
                "enabled": bool(system.get("enabled", True)),
                "max_total_power": system.get("max_total_power"),
                "loop_interval": system.get("loop_interval"),
                "min_output_limit": system.get("min_output_limit"),
            },
            timestamp_ns
        )
        if line:
            lines.append(line)

    for section_name in ("ha", "winter"):
        section = runtime_state.get(section_name, {})
        if not isinstance(section, dict):
            continue

        line = build_line_protocol(
            "ems_runtime",
            {"source": "ems", "scope": section_name, "run_id": run_id},
            section,
            timestamp_ns
        )
        if line:
            lines.append(line)

    devices = runtime_state.get("devices", {})

    if isinstance(devices, dict):
        for device_name, device_data in devices.items():
            if not isinstance(device_data, dict):
                continue

            line = build_line_protocol(
                "ems_runtime",
                {
                    "source": "ems",
                    "scope": "device",
                    "device": device_name,
                    "run_id": run_id
                },
                device_data,
                timestamp_ns
            )
            if line:
                lines.append(line)

    return lines


def main():
    args = parse_args()
    setup_logging(args.log_level)

    config = load_config(args.config)
    env_values = load_env_file(args.env)
    require_env(
        env_values,
        "INFLUXDB_URL",
        "INFLUXDB_ORG",
        "INFLUXDB_TOKEN",
        "INFLUXDB_BUCKET_RAW",
    )

    bucket = args.bucket or env_values["INFLUXDB_BUCKET_RAW"]
    session = create_session()
    influx = InfluxHTTPClient(
        env_values["INFLUXDB_URL"],
        env_values["INFLUXDB_ORG"],
        env_values["INFLUXDB_TOKEN"],
        session=session
    )

    devices = [
        ZendureClient(
            item["name"],
            item["ip"],
            item["sn"],
            session,
            item.get("min_soc", 0),
            item.get("max_soc", 0),
            item.get("smart_mode", 1),
            item.get("grid_off_mode"),
            item.get("max_power"),
            item.get("pv_kwp", 1.0),
            item.get("battery_kwh", 1.0),
            item.get("pv_priority_factor", 1.0)
        )
        for item in config.get("devices", [])
    ]

    if not devices:
        raise SystemExit("No devices configured in config.json")

    shelly = None
    shelly_ip = config.get("shelly", {}).get("ip")
    if shelly_ip and not args.skip_shelly:
        shelly = ShellyClient(shelly_ip, session)

    runtime_state_path = args.runtime_state_path or config.get(
        "system",
        {}
    ).get(
        "runtime_state_path",
        "runtime-state.json"
    )

    log_event(
        logging.INFO,
        "influx_capture_started",
        bucket=bucket,
        device_count=len(devices),
        include_runtime_state=args.include_runtime_state,
        run_id=args.run_id or "-",
        interval_s=args.interval,
        started_at=datetime.now(timezone.utc).isoformat()
    )

    started = time.time()
    cycles = 0

    while True:
        timestamp_ns = time.time_ns()
        lines = []
        states = fetch_all_devices(devices)

        for device, state in zip(devices, states):
            if state is None:
                log_event(
                    logging.WARNING,
                    "influx_capture_device_unavailable",
                    device=device.name
                )
                line = build_line_protocol(
                    "zendure_device",
                    {
                        "device": device.name,
                        "source": "zendure",
                        "run_id": args.run_id
                    },
                    {"available": False},
                    timestamp_ns
                )
                if line:
                    lines.append(line)
                continue

            line = build_line_protocol(
                "zendure_device",
                {
                    "device": device.name,
                    "source": "zendure",
                    "run_id": args.run_id
                },
                build_device_fields(state),
                timestamp_ns
            )
            if line:
                lines.append(line)

        if shelly:
            house_load = shelly.get_power()
            line = build_line_protocol(
                "shelly_meter",
                {"source": "shelly", "run_id": args.run_id},
                {"house_load": house_load},
                timestamp_ns
            )
            if line:
                lines.append(line)

        if args.include_runtime_state:
            runtime_state = read_runtime_state(runtime_state_path)
            if runtime_state is not None:
                lines.extend(
                    runtime_state_points(runtime_state, args.run_id, timestamp_ns)
                )

        try:
            influx.write_lines(bucket, lines)
        except Exception as exc:
            log_event(
                logging.ERROR,
                "influx_capture_write_error",
                error=exc,
                line_count=len(lines)
            )
        else:
            cycles += 1
            log_event(
                logging.INFO,
                "influx_capture_cycle",
                cycle=cycles,
                line_count=len(lines)
            )

        if args.duration and time.time() - started >= args.duration:
            break

        time.sleep(max(args.interval, 0.1))

    log_event(
        logging.INFO,
        "influx_capture_stopped",
        cycles=cycles,
        duration_s=round(time.time() - started, 1)
    )


if __name__ == "__main__":
    main()
