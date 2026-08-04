# SPDX-License-Identifier: AGPL-3.0-or-later
import argparse
import logging
import os
import sys
import time

from ems import config as cfg
from ems import paths
from ems.clients import (
    HAClient,
    ZendureClient,
    close_grid_meter_client,
    create_grid_meter_client,
    create_session,
)
from ems.controller import EMSController
from ems.device_identity import broker_sources_from_config
from ems.logging_utils import log_event, setup_logging
from ems.runtime_state import RuntimeState, build_runtime_defaults
from ems.simulation import (
    built_in_simulation_frames,
    load_replay_frames,
    run_frames,
    run_live_preflight,
    run_self_tests,
)
from ems.zendure_mqtt.config_entries import (
    duplicate_device_name_startup_error,
    duplicate_zendure_identity_startup_error,
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

    dashboard_enabled = cfg.safe_bool(cfg.DASHBOARD_CONFIG.get("enabled", False), False)
    if dashboard_enabled:
        # Install the in-memory log ring buffer right after logging is set up so
        # the dashboard log viewer captures output from startup onward.
        from ems.log_buffer import install_log_buffer

        install_log_buffer(
            capacity=cfg.safe_int(
                cfg.DASHBOARD_CONFIG.get("log_buffer_lines", 5000),
                5000,
                minimum=1,
            )
        )

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

    # A physical Zendure device must be configured only once. Refuse to start
    # rather than let two entries (e.g. an API device and an MQTT telemetry
    # entry for the same serial) drive conflicting state.
    duplicate_identity = duplicate_zendure_identity_startup_error(
        cfg.CONFIG.get("devices"),
        broker_sources=broker_sources_from_config(cfg.CONFIG),
    )
    if duplicate_identity:
        log_event(
            logging.ERROR,
            "startup_abort",
            reason="duplicate_zendure_device_identity",
            **duplicate_identity,
        )
        sys.exit(1)

    # Device names are the runtime identity key for controller state,
    # runtime-state.json and history: two active devices sharing a name would
    # silently merge, so refusing to start is the safe behavior.
    duplicate_name = duplicate_device_name_startup_error(cfg.CONFIG.get("devices"))
    if duplicate_name:
        log_event(
            logging.ERROR,
            "startup_abort",
            reason="duplicate_device_name",
            **duplicate_name,
        )
        sys.exit(1)

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
        for d in cfg.http_control_device_configs()
    ]

    # An unsafe, unmigrated MQTT control config must not be silently rewritten at
    # startup: refuse to run the control runtime and point the operator at the
    # EMS-owned migration (emsctl config migrate-zendure-mqtt). Telemetry-only and
    # already-pinned control devices never block.
    from ems.zendure_mqtt.migration import (
        zendure_mqtt_control_migration_startup_error,
    )

    migration_required = zendure_mqtt_control_migration_startup_error(cfg.CONFIG)
    if migration_required and not (
        cfg.SIMULATION_MODE or args.replay
    ):
        log_event(
            logging.ERROR,
            "startup_abort",
            reason=migration_required["code"],
            control_devices_needing_migration=migration_required["count"],
        )
        sys.exit(1)

    # Write-capable Zendure MQTT devices join the same control loop as HTTP
    # devices. Publishing is additionally guarded by the MQTT write gates.
    zendure_mqtt_control_runtime = None
    from ems.zendure_mqtt.control_runtime import (
        MqttControlStartupError,
        build_zendure_mqtt_control_runtime_or_abort,
    )

    try:
        zendure_mqtt_control_runtime = build_zendure_mqtt_control_runtime_or_abort(
            cfg.CONFIG
        )
    except MqttControlStartupError as e:
        # MQTT control is configured but its runtime could not be built. Aborting
        # is safer than silently running with fewer controllable inverters than
        # configured.
        log_event(
            logging.ERROR,
            "startup_abort",
            reason="mqtt_control_runtime_build_failed",
            error=e.__cause__ or e,
        )
        sys.exit(1)

    if zendure_mqtt_control_runtime is not None:
        accepted = len(zendure_mqtt_control_runtime.devices)
        rejected = zendure_mqtt_control_runtime.rejected
        configured = accepted + len(rejected)
        # An enabled but invalid control device must not be silently skipped:
        # starting with fewer controllable inverters than configured is unsafe.
        if rejected:
            codes = sorted(
                {
                    issue["code"]
                    for entry in rejected
                    for issue in entry.issues
                    if issue.get("code")
                }
            )
            log_event(
                logging.ERROR,
                "startup_abort",
                reason="invalid_mqtt_control_device",
                configured_control_devices=configured,
                accepted_control_devices=accepted,
                rejected_control_devices=len(rejected),
                issue_codes=",".join(codes),
            )
            sys.exit(1)
        if zendure_mqtt_control_runtime.devices:
            zendure_mqtt_control_runtime.start()
            devices = devices + zendure_mqtt_control_runtime.devices
            log_event(
                logging.INFO,
                "zendure_mqtt_control_runtime",
                configured_control_devices=configured,
                accepted_control_devices=accepted,
                rejected_control_devices=len(rejected),
            )

    if not devices:
        log_event(logging.ERROR, "startup_abort", reason="no_devices")
        sys.exit(1)

    shelly = create_grid_meter_client(
        cfg.GRID_METER_CONFIG,
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
            from ems.log_buffer import get_log_buffer

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
                config_path=cfg.config_path(),
                runtime_state_path=cfg.runtime_state_path(),
                log_buffer=get_log_buffer(),
                log_redaction=cfg.safe_bool(
                    cfg.DASHBOARD_CONFIG.get("log_redaction", False),
                    False
                ),
                animation_mode=str(
                    cfg.DASHBOARD_CONFIG.get("animation_mode", "normal")
                ),
                session_timeout_seconds=cfg.safe_session_timeout(
                    cfg.DASHBOARD_CONFIG.get(
                        "session_idle_timeout_seconds", 1800
                    ),
                    1800
                ),
                session_absolute_max_seconds=cfg.safe_session_timeout(
                    cfg.DASHBOARD_CONFIG.get(
                        "session_absolute_max_seconds", 43200
                    ),
                    43200
                ),
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
        ok = run_live_preflight(devices, shelly, ha)
        close_grid_meter_client(shelly)
        sys.exit(0 if ok else 2)

    # Native InfluxDB telemetry writer: active only when influxdb is enabled and
    # we are reading real hardware (not simulation/replay). Started lazily and
    # non-blocking; InfluxDB being unavailable never blocks or stops the EMS.
    influx_writer = None
    if (
        cfg.INFLUXDB_CONFIG
        and cfg.INFLUXDB_CONFIG.get("enabled")
        and not cfg.SIMULATION_MODE
        and not getattr(args, "replay", None)
    ):
        try:
            from ems.history.influx_writer import InfluxTelemetryWriter

            influx_writer = InfluxTelemetryWriter(cfg.INFLUXDB_CONFIG)
            influx_writer.start()
        except Exception as e:
            log_event(logging.WARNING, "influx_writer_start_failed", error=e)
            influx_writer = None

    # Zendure MQTT telemetry runtime. It reads snapshots and drives no writes;
    # any control device shares its broker service so a broker used by both keeps
    # a single connection and snapshot cache.
    zendure_mqtt_runtime = None
    zendure_mqtt_status_path = None

    def _mqtt_control_status():
        if zendure_mqtt_control_runtime is None:
            return None
        return zendure_mqtt_control_runtime.status()

    try:
        from ems.zendure_mqtt.runtime import build_zendure_mqtt_runtime

        shared_services = (
            zendure_mqtt_control_runtime.services_by_ref
            if zendure_mqtt_control_runtime is not None
            else None
        )
        zendure_mqtt_runtime = build_zendure_mqtt_runtime(
            cfg.CONFIG, shared_services=shared_services
        )
        zendure_mqtt_runtime.start()
        status = zendure_mqtt_runtime.status()
        log_event(
            logging.INFO,
            "zendure_mqtt_runtime",
            enabled=status["enabled"],
            broker_configured=status["broker_configured"],
            broker_count=status.get("broker_count", 0),
            running=status["running"],
            configured_devices=status["configured_device_count"],
            invalid_devices=status["invalid_device_count"],
        )
        # Persist a live status snapshot Admin can prefer over its config-derived
        # fallback. Read-only: no broker publish is involved. Control devices are
        # merged in so they appear in runtime status too.
        zendure_mqtt_status_path = paths.resolve_zendure_mqtt_status_path()
        zendure_mqtt_runtime.write_status_file(
            zendure_mqtt_status_path, control_status=_mqtt_control_status()
        )
    except Exception as e:
        log_event(logging.WARNING, "zendure_mqtt_runtime_start_failed", error=e)

    ems = EMSController(
        devices,
        shelly,
        ha,
        runtime_state=runtime_state,
        dashboard_store=dashboard_store,
        influx_writer=influx_writer,
        zendure_mqtt_runtime=zendure_mqtt_runtime
    )

    log_event(logging.INFO, "ems_started")

    start_time = time.time()
    cycles = 0

    try:
        while True:
            ems.run_once()
            cycles += 1

            # Self-heal: re-attempt broker connections that failed at boot.
            # start() is idempotent, never raises on connection failure and
            # throttles failed attempts internally.
            if zendure_mqtt_control_runtime is not None:
                zendure_mqtt_control_runtime.start()
            if zendure_mqtt_runtime is not None:
                zendure_mqtt_runtime.start()

            if zendure_mqtt_runtime is not None and zendure_mqtt_status_path is not None:
                zendure_mqtt_runtime.write_status_file(
                    zendure_mqtt_status_path, control_status=_mqtt_control_status()
                )

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
    finally:
        # Release the grid-meter client's runtime resources (the MQTT grid meter
        # owns a network loop/connection/thread; HTTP clients own none). Safe and
        # idempotent, and never masks a primary shutdown error.
        close_grid_meter_client(shelly)
        if influx_writer:
            influx_writer.stop()
        if zendure_mqtt_runtime is not None:
            zendure_mqtt_runtime.stop()
        if zendure_mqtt_control_runtime is not None:
            zendure_mqtt_control_runtime.stop()


if __name__ == "__main__":
    main()
