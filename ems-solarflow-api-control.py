import argparse
import logging
import os
import sys
import time

from ems import config as cfg
from ems.clients import HAClient, ShellyClient, ZendureClient, create_session
from ems.controller import EMSController
from ems.logging_utils import log_event, setup_logging
from ems.runtime_state import RuntimeState, build_runtime_defaults
from ems.simulation import (
    built_in_simulation_frames,
    load_replay_frames,
    run_frames,
    run_live_preflight,
    run_self_tests,
)

# =====================
# CLI
# =====================


def parse_args():
    parser = argparse.ArgumentParser(
        description="Local Zendure SolarFlow EMS controller"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read live telemetry and calculate targets without hardware writes"
    )
    parser.add_argument(
        "--config",
        help="Path to config file"
    )
    parser.add_argument(
        "--no-ha",
        action="store_true",
        help="Disable Home Assistant reads and writes for this run"
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Run deterministic built-in simulation without hardware access"
    )
    parser.add_argument(
        "--replay",
        help="Replay a JSONL runtime trace without hardware access"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one loop iteration and exit"
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0,
        help="Run for this many seconds and then exit"
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="Run for this many EMS cycles and then exit"
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Read telemetry and validate live-test prerequisites without control writes"
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run local helper tests without hardware access"
    )

    return parser.parse_args()


def main():
    args = parse_args()
    base_dir = os.path.dirname(os.path.abspath(__file__))
    cfg.initialize(args, base_dir)
    setup_logging(cfg.LOG_LEVEL)

    log_event(
        logging.INFO,
        "startup",
        dry_run=cfg.DRY_RUN,
        simulation=cfg.SIMULATION_MODE,
        replay=bool(args.replay),
        allow_hardware_writes=cfg.ALLOW_HARDWARE_WRITES,
        allow_state_reconciliation_writes=cfg.ALLOW_STATE_RECONCILIATION_WRITES,
        ha_enabled=cfg.HA_ENABLED,
        ha_control_enabled=cfg.HA_CONTROL_ENABLED
    )

    if args.self_test:
        sys.exit(0 if run_self_tests() else 2)

    if cfg.SIMULATION_MODE and not args.replay:
        run_frames(built_in_simulation_frames(), "built_in_simulation")
        sys.exit(0)

    if args.replay:
        try:
            replay_frames = load_replay_frames(args.replay)
        except Exception as e:
            log_event(
                logging.ERROR,
                "replay_load_error",
                source=args.replay,
                error=e
            )
            sys.exit(1)

        run_frames(replay_frames, args.replay)
        sys.exit(0)

    session = create_session()

    ha = None

    if cfg.HA_ENABLED and not cfg.SIMULATION_MODE and not args.replay:
        ha = HAClient(
            cfg.HA_URL,
            cfg.HA_TOKEN,
            session
        )

    devices = [
        ZendureClient(
            d["name"],
            d["ip"],
            d["sn"],
            session,
            d.get("min_soc", 0),
            d.get("max_soc", 0),
            d.get("smart_mode", 1),
            d.get("grid_off_mode"),
            d.get("max_power", cfg.MAX_DEVICE_POWER),
            d.get("pv_kwp", 1.0),
            d.get("battery_kwh", 1.0),
            d.get("pv_priority_factor", 1.0)
        )
        for d in cfg.ZENDURE_CONFIG
    ]

    if not devices:
        log_event(logging.ERROR, "startup_abort", reason="no_devices")
        sys.exit(1)

    shelly = ShellyClient(
        cfg.SHELLY_IP,
        session
    )

    runtime_state = RuntimeState(
        cfg.runtime_state_path(),
        build_runtime_defaults(devices)
    )
    runtime_state.load_or_create()

    dashboard_store = None

    if cfg.safe_bool(
        cfg.DASHBOARD_CONFIG.get("enabled", False),
        False
    ):
        try:
            from dashboard.server import start_dashboard_server
            from dashboard.sqlite_store import DashboardStore
            from dashboard.runtime_write import build_validation_context

            dashboard_store = DashboardStore(
                cfg.dashboard_database_path(),
                retention_hours=cfg.safe_int(
                    cfg.DASHBOARD_CONFIG.get("history_hours", 48),
                    48,
                    minimum=1
                ),
                energy_savings=cfg.ENERGY_SAVINGS_CONFIG
            )
            start_dashboard_server(
                dashboard_store,
                host=str(cfg.DASHBOARD_CONFIG.get("host", "0.0.0.0")),
                port=cfg.safe_int(
                    cfg.DASHBOARD_CONFIG.get("port", 8080),
                    8080,
                    minimum=1
                ),
                runtime_state=runtime_state,
                auth_file=cfg.dashboard_file_path(
                    "auth_file",
                    "config/dashboard-auth.json"
                ),
                ssl_enabled=cfg.safe_bool(
                    cfg.DASHBOARD_CONFIG.get("ssl_enabled", False),
                    False
                ),
                ssl_cert_file=cfg.dashboard_file_path(
                    "ssl_cert_file",
                    "config/dashboard.crt"
                ),
                ssl_key_file=cfg.dashboard_file_path(
                    "ssl_key_file",
                    "config/dashboard.key"
                ),
                ssl_auto_generate=cfg.safe_bool(
                    cfg.DASHBOARD_CONFIG.get("ssl_auto_generate", True),
                    True
                ),
                base_dir=base_dir,
                runtime_validation=build_validation_context(
                    cfg.CONFIG,
                    runtime_state
                )
            )
        except Exception as e:
            log_event(
                logging.WARNING,
                "dashboard_start_failed",
                error=e
            )

    if args.preflight:
        if run_live_preflight(devices, shelly, ha):
            sys.exit(0)

        sys.exit(2)

    ems = EMSController(
        devices,
        shelly,
        ha,
        runtime_state=runtime_state,
        dashboard_store=dashboard_store
    )

    log_event(logging.INFO, "ems_started")

    start_time = time.time()
    cycles = 0

    while True:
        ems.run_once()
        cycles += 1

        if args.once:
            log_event(
                logging.INFO,
                "ems_stopped",
                reason="once",
                cycles=cycles
            )
            break

        if args.max_cycles and cycles >= args.max_cycles:
            log_event(
                logging.INFO,
                "ems_stopped",
                reason="max_cycles",
                cycles=cycles
            )
            break

        if args.duration and time.time() - start_time >= args.duration:
            log_event(
                logging.INFO,
                "ems_stopped",
                reason="duration",
                cycles=cycles,
                duration_s=round(time.time() - start_time, 1)
            )
            break


if __name__ == "__main__":
    main()
