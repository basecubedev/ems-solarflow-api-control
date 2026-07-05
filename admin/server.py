# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lightweight admin discovery HTTP server.

Stdlib ``http.server`` only, no framework, no database. Scan state is kept in a
small in-memory registry; each scan runs on a bounded background thread so the
POST returns immediately and the UI polls the result endpoint. The server never
reads secrets. It only writes the real EMS ``config.json`` on the explicit
``config/apply`` action; preview and export stay non-destructive. Maintenance
offers a backup by default and requires explicit confirmation before apply.
"""

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from admin.config_apply import ConfigApplyService, ConfigChangedError
from admin.config_export import ConfigExportService, ConfigExportValidationError
from admin.config_preview import ConfigPreviewGenerator
from admin.deployment import DeploymentService, DockerCli
from admin.discovery import (
    CidrValidationError,
    DEFAULT_PORTS,
    clamp_max_workers,
    clamp_timeout_ms,
    scan_network,
    validate_cidr,
)
from admin.ems_cli import EmsCliDiagnostics
from admin.backup_restore import (
    BackupJob,
    BackupJobRegistry,
    BackupRestoreError,
    BackupRestoreService,
    CONFLICT_POLICIES,
    CREATE_SCOPES,
    RESTORE_SCOPES,
    admin_restore_block_reason,
)
from admin.gateway_probe import probe_gateway_candidates
from admin.guided_upgrade import (
    GuidedUpgradeExecutor,
    UpgradeJob,
    UpgradeJobRegistry,
    plan_upgrade_steps,
)
from admin.install_context import detect_install_context
from admin.install_state import (
    LegacyMigrationError,
    detect_install_state,
    migrate_legacy_root_config,
    select_start_path,
)
from admin.container_actions import (
    build_maintenance_container_plan,
    run_maintenance_container_sync,
)
from admin.maintenance import run_maintenance_overview
from admin.maintenance_config import (
    load_maintenance_config,
    prepare_maintenance_config_apply,
    preview_maintenance_config,
)
from admin.mdns import MdnsProvider
from admin.mqtt_discovery import MqttBrokerDiscovery
from admin.models import utc_now_iso
from admin.networks import detect_network_suggestions
from admin.releases import ReleaseError, ReleaseManager, default_admin_data_dir
from admin.setup_config import build_setup_catalog
from dashboard.static_files import build_static_asset_index, static_asset_key

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_JSON_BODY_BYTES = 4 * 1024
MAX_CONFIG_PREVIEW_BODY_BYTES = 64 * 1024
MAX_TRACKED_SCANS = 20

# Guided-upgrade preflight rejection reasons -> HTTP status. Accepted runs spawn
# a job (202) whose live step progress is polled; only rejections use this map.
_UPGRADE_STATUS_CODES = {
    "confirm_required": 400,
    "target_required": 400,
    "target_not_prepared": 409,
    "config_missing": 409,
    "compose_missing": 409,
}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'self'"
    ),
}


class ScanRegistry:
    """Thread-safe in-memory store of discovery scan state.

    Old scans are evicted once ``MAX_TRACKED_SCANS`` is exceeded so a long-lived
    preview cannot grow unbounded. State is intentionally not persisted.
    """

    def __init__(self, scan_runner=scan_network):
        self._lock = threading.Lock()
        self._scans = {}
        self._order = []
        self._scan_runner = scan_runner

    def start(self, cidr, ports, timeout_ms, max_workers):
        scan_id = uuid.uuid4().hex
        record = {
            "scan_id": scan_id,
            "status": "running",
            "cidr": cidr,
            "ports": ports,
            "timeout_ms": timeout_ms,
            "max_workers": max_workers,
            "started_at": utc_now_iso(),
            "finished_at": None,
            "devices": [],
            "errors": [],
        }
        with self._lock:
            self._scans[scan_id] = record
            self._order.append(scan_id)
            self._evict_locked()

        thread = threading.Thread(
            target=self._run, args=(scan_id, cidr, timeout_ms, max_workers), daemon=True
        )
        thread.start()
        return record

    def get(self, scan_id):
        with self._lock:
            record = self._scans.get(scan_id)
            return dict(record) if record is not None else None

    def _run(self, scan_id, cidr, timeout_ms, max_workers):
        try:
            devices, errors = self._scan_runner(
                cidr, timeout_ms=timeout_ms, max_workers=max_workers
            )
            payload = {
                "status": "finished",
                "devices": [device.to_dict() for device in devices],
                "errors": errors,
            }
        except Exception as exc:  # surface scan failure as scan state, not a 500
            payload = {"status": "failed", "errors": [{"error": str(exc)}]}

        with self._lock:
            record = self._scans.get(scan_id)
            if record is None:
                return
            record.update(payload)
            record["finished_at"] = utc_now_iso()

    def _evict_locked(self):
        while len(self._order) > MAX_TRACKED_SCANS:
            oldest = self._order.pop(0)
            self._scans.pop(oldest, None)


class AdminServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler, registry=None, static_assets=None,
                 network_detector=None, gateway_prober=None, mdns_provider=None,
                 mqtt_discovery=None, release_manager=None, config_export=None,
                 config_apply=None, deployment=None, ems_cli=None,
                 guided_upgrade=None, backup_service=None):
        super().__init__(server_address, handler)
        self.registry = registry or ScanRegistry()
        self.network_detector = network_detector or detect_network_suggestions
        self.gateway_prober = gateway_prober or probe_gateway_candidates
        self.mqtt_discovery = mqtt_discovery or MqttBrokerDiscovery()
        self.mdns_provider = mdns_provider or MdnsProvider(
            mqtt_handler=self.mqtt_discovery.add_mdns_candidate
        )
        # Give the release manager a read-only Docker inspector so it can compare
        # a running build's identity against release targets; harmless when the
        # daemon is absent (all inspections degrade to an unknown identity).
        self.release_manager = release_manager or ReleaseManager(docker=DockerCli())
        self.config_preview = ConfigPreviewGenerator(self.release_manager)
        admin_data_dir = getattr(
            self.release_manager, "data_dir", default_admin_data_dir()
        )
        self.config_export = config_export or ConfigExportService(
            self.config_preview,
            admin_data_dir,
        )
        self.config_apply = config_apply or ConfigApplyService(
            self.config_export,
            admin_data_dir,
        )
        self.deployment = deployment or DeploymentService(
            self.release_manager,
            self.config_export,
            admin_data_dir=admin_data_dir,
        )
        self.ems_cli = ems_cli or EmsCliDiagnostics()
        self.guided_upgrade = guided_upgrade or GuidedUpgradeExecutor(
            release_manager=self.release_manager,
            ems_cli=self.ems_cli,
        )
        self.upgrade_jobs = UpgradeJobRegistry()
        self.backup_service = backup_service or BackupRestoreService()
        self.backup_jobs = BackupJobRegistry()
        self.static_assets = (
            static_assets
            if static_assets is not None
            else build_static_asset_index(STATIC_DIR)
        )


class AdminHandler(BaseHTTPRequestHandler):
    server_version = "AdminDiscovery/1.0"

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("", "/", "/index.html"):
            self._send_static("/index.html")
            return
        if path == "/api/admin/status":
            self._send_json(self._status_payload())
            return
        if path == "/api/admin/install-state":
            self._send_json(detect_install_state().as_dict())
            return
        if path == "/api/admin/maintenance/overview":
            self._send_json(run_maintenance_overview())
            return
        if path == "/api/admin/maintenance/config":
            self._send_json(load_maintenance_config())
            return
        if path == "/api/admin/maintenance/containers/plan":
            self._handle_maintenance_container_plan()
            return
        if path.startswith("/api/admin/maintenance/upgrade/jobs/"):
            self._send_upgrade_job(
                path[len("/api/admin/maintenance/upgrade/jobs/"):]
            )
            return
        if path.startswith("/api/admin/maintenance/backups/jobs/"):
            self._send_backup_job(
                path[len("/api/admin/maintenance/backups/jobs/"):]
            )
            return
        if path == "/api/admin/maintenance/backups":
            self._handle_backups_list()
            return
        if path == "/api/setup/releases":
            # Guided Setup (fresh install) lists every supported release; the
            # maintenance/upgrade flow passes ?flow=upgrade to apply the
            # upgrade-only build-identity gate against the running EMS build.
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            for_upgrade = "flow=upgrade" in query
            self._send_json(
                self.server.release_manager.list_releases(for_upgrade=for_upgrade)
            )
            return
        if path == "/api/setup/config-template":
            self._handle_config_template()
            return
        if path == "/api/setup/config/catalog":
            self._send_json(build_setup_catalog())
            return
        if path == "/api/setup/config-preview":
            self._send_json(self.server.config_preview.generate())
            return
        if path == "/api/setup/config/status":
            self._send_json(self.server.config_export.status())
            return
        if path == "/api/setup/deployment/plan":
            plan = self.server.deployment.plan()
            plan["install_context"] = detect_install_context().as_dict()
            self._send_json(plan)
            return
        if path == "/api/setup/deployment/status":
            self._send_json(self.server.deployment.status())
            return
        if path.startswith("/api/setup/deployment/start/jobs/"):
            self._send_deployment_start_job(
                path[len("/api/setup/deployment/start/jobs/"):]
            )
            return
        if path.startswith("/api/setup/deployment/jobs/"):
            self._send_deployment_job(path[len("/api/setup/deployment/jobs/"):])
            return
        if path == "/api/discovery/networks":
            self._send_json(self.server.network_detector())
            return
        if path == "/api/discovery/mdns/status":
            self._send_json(self.server.mdns_provider.status())
            return
        if path == "/api/discovery/mqtt-brokers":
            self._send_json({
                "candidates": self.server.mqtt_discovery.candidates(),
            })
            return
        if path in ("/api/discovery/devices", "/api/discovery/results"):
            self._send_json({
                "devices": self.server.mdns_provider.devices(),
                "ignored_devices": (
                    self.server.mdns_provider.ignored_devices()
                    if hasattr(self.server.mdns_provider, "ignored_devices") else []
                ),
            })
            return
        if path.startswith("/api/discovery/result/"):
            self._send_result(path[len("/api/discovery/result/"):])
            return
        if path.startswith("/api/"):
            self._send_json({"error": "not found"}, status=404)
            return
        self._send_static(path)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/admin/start-path":
            self._handle_start_path()
            return
        if path == "/api/admin/config/migrate-legacy":
            self._handle_migrate_legacy()
            return
        if path == "/api/admin/maintenance/diagnostics/run":
            self._handle_maintenance_diagnostics()
            return
        if path == "/api/admin/maintenance/config/preview":
            self._handle_maintenance_config_preview()
            return
        if path == "/api/admin/maintenance/config/apply":
            self._handle_maintenance_config_apply()
            return
        if path == "/api/admin/maintenance/containers/sync":
            self._handle_maintenance_container_sync()
            return
        if path == "/api/admin/maintenance/upgrade/execute":
            self._handle_maintenance_upgrade_execute()
            return
        if path == "/api/admin/maintenance/backups/create":
            self._handle_backup_create()
            return
        if path == "/api/admin/maintenance/backups/inspect":
            self._handle_backup_inspect()
            return
        if path == "/api/admin/maintenance/backups/diff":
            self._handle_backup_diff()
            return
        if path == "/api/admin/maintenance/backups/restore/preview":
            self._handle_backup_restore_preview()
            return
        if path == "/api/admin/maintenance/backups/restore/execute":
            self._handle_backup_restore_execute()
            return
        if path == "/api/admin/maintenance/backups/delete":
            self._handle_backup_delete()
            return
        if path == "/api/setup/releases/prepare":
            self._handle_release_prepare()
            return
        if path in (
            "/api/setup/config-preview",
            "/api/setup/config-preview/validate",
        ):
            self._handle_config_preview()
            return
        if path == "/api/setup/config/download":
            self._handle_config_download()
            return
        if path == "/api/setup/config/write":
            self._handle_config_write()
            return
        if path == "/api/setup/config/apply":
            self._handle_config_apply()
            return
        if path == "/api/setup/deployment/prepare":
            self._handle_deployment_prepare()
            return
        if path == "/api/setup/deployment/start":
            self._handle_deployment_start()
            return
        if path == "/api/setup/deployment/repair-permissions":
            self._handle_deployment_permission_repair()
            return
        if path == "/api/setup/deployment/resolve-container-conflict":
            self._handle_deployment_container_conflict()
            return
        if path == "/api/discovery/scan":
            self._handle_scan()
            return
        if path == "/api/discovery/gateway-probe":
            self._handle_gateway_probe()
            return
        if path == "/api/discovery/mdns/enable":
            self._drain_body()
            self._send_json(self.server.mdns_provider.enable())
            return
        if path == "/api/discovery/mdns/disable":
            self._drain_body()
            self._send_json(self.server.mdns_provider.disable())
            return
        if path == "/api/discovery/mdns/refresh":
            self._drain_body()
            self._send_json(self.server.mdns_provider.refresh())
            return
        if path == "/api/discovery/mqtt-brokers/refresh":
            self._drain_body()
            mdns_status = self.server.mdns_provider.refresh()
            result = self.server.mqtt_discovery.refresh()
            result["mdns"] = mdns_status
            self._send_json(result)
            return
        if path == "/api/discovery/mqtt-brokers/probe":
            self._handle_mqtt_probe()
            return
        self._send_json({"error": "not found"}, status=404)

    def log_message(self, _fmt, *_args):
        return

    # --- handlers --------------------------------------------------------

    def _status_payload(self):
        return {
            "service": "ems-solarflow-admin",
            "version": "mvp",
            "capabilities": [
                "install_state_routing",
                "legacy_config_migration",
                "maintenance_overview",
                "maintenance_config_preview",
                "ems_cli_diagnostics",
                "discovery",
                "release_resources",
                "config_preview",
                "config_export",
                "config_apply",
                "deployment_prepare",
                "deployment_start",
            ],
            "writes_config": True,
            "writes_generated_config": True,
            "active_device_list": "planned",
            "time": utc_now_iso(),
        }

    def _handle_start_path(self):
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, status=400)
            return
        choice = body.get("choice")
        confirm = body.get("confirm", False)
        if not isinstance(confirm, bool):
            self._send_json({"error": "confirm must be a boolean"}, status=400)
            return
        try:
            result = select_start_path(choice, confirm=confirm)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        status = 409 if result.get("requires_confirmation") else 200
        self._send_json(result, status=status)

    def _handle_migrate_legacy(self):
        body = self._read_optional_json_body()
        if body is None:
            return
        overwrite = body.get("overwrite", False)
        if not isinstance(overwrite, bool):
            self._send_json({"error": "overwrite must be a boolean"}, status=400)
            return
        try:
            result = migrate_legacy_root_config(overwrite=overwrite)
        except LegacyMigrationError as exc:
            self._send_json(
                {"ok": False, "reason": exc.reason, "message": exc.message},
                status=exc.status,
            )
            return
        except OSError as exc:
            self._send_json(
                {
                    "ok": False,
                    "reason": "migration_failed",
                    "message": f"Could not migrate the legacy config: {exc}",
                },
                status=500,
            )
            return
        self._send_json(result)

    def _handle_maintenance_diagnostics(self):
        # User-triggered read-only EMS checks. The body carries no command input;
        # any accidental payload is drained so the allowlist is the only surface.
        body = self._read_optional_json_body()
        if body is None:
            return
        try:
            result = self.server.ems_cli.run()
        except Exception:  # a check-runner fault must not 500 the Admin route
            self._send_json(
                {
                    "available": False,
                    "mode": "unavailable",
                    "container": None,
                    "checks": [],
                    "summary": {
                        "status": "unavailable",
                        "ok": 0,
                        "warning": 0,
                        "failed": 0,
                        "unavailable": 0,
                    },
                    "message": "EMS CLI diagnostics could not be run.",
                }
            )
            return
        self._send_json(result)

    def _handle_maintenance_config_preview(self):
        # Preview only: the merge never writes, and the config path is resolved
        # from the install context, so the request must not carry a target path.
        body = self._read_json_body(MAX_CONFIG_PREVIEW_BODY_BYTES)
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, status=400)
            return
        if "path" in body or "config_path" in body:
            self._send_json(
                {"error": "custom config paths are not supported"}, status=400
            )
            return
        draft = body.get("draft", body)
        if not isinstance(draft, dict):
            self._send_json({"error": "draft must be a JSON object"}, status=400)
            return
        self._send_json(preview_maintenance_config(draft))

    def _handle_maintenance_config_apply(self):
        body = self._read_json_body(MAX_CONFIG_PREVIEW_BODY_BYTES)
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, status=400)
            return
        if set(body) - {"draft", "revision", "confirm", "backup"}:
            self._send_json({"error": "unsupported apply field"}, status=400)
            return
        if body.get("confirm") is not True:
            self._send_json(
                {"error": "explicit apply confirmation is required"}, status=400
            )
            return
        draft = body.get("draft")
        revision = body.get("revision")
        backup = body.get("backup", True)
        if not isinstance(draft, dict) or not isinstance(revision, str):
            self._send_json({"error": "draft and revision are required"}, status=400)
            return
        if not isinstance(backup, bool):
            self._send_json({"error": "backup must be a boolean"}, status=400)
            return
        prepared = prepare_maintenance_config_apply(draft, revision)
        if prepared.get("status") != "ok":
            status = 409 if prepared.get("status") == "conflict" else 400
            self._send_json(prepared, status=status)
            return
        try:
            result = self.server.config_apply.apply_maintenance(
                prepared["payload"], revision, create_backup=backup
            )
        except ConfigChangedError as exc:
            self._send_json(
                {"ok": False, "status": "conflict", "message": str(exc)},
                status=409,
            )
            return
        except OSError as exc:
            self._send_json(
                {"ok": False, "status": "error", "message": f"Apply failed: {exc}"},
                status=500,
            )
            return
        result["validation"] = prepared["validation"]
        result["diff"] = prepared["diff"]
        self._send_json(result)

    def _handle_maintenance_container_plan(self):
        # Read-only: derive the desired/current/action plan from the live config
        # and Docker state. Docker being unavailable is a valid (degraded) plan.
        try:
            plan = build_maintenance_container_plan()
        except Exception:  # a plan fault must never 500 the read-only route
            self._send_json(
                {
                    "ok": False,
                    "available": False,
                    "message": "Could not read the container sync plan.",
                },
                status=200,
            )
            return
        self._send_json(plan)

    def _handle_maintenance_container_sync(self):
        # Confirmed mutation: recreate EMS and start/stop the optional bundled
        # InfluxDB. The config path is resolved from the install context, so the
        # request must not carry a target path.
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, status=400)
            return
        if set(body) - {"confirm", "reason"}:
            self._send_json({"error": "unsupported sync field"}, status=400)
            return
        if body.get("confirm") is not True:
            self._send_json(
                {"error": "explicit sync confirmation is required"}, status=400
            )
            return
        reason = body.get("reason", "config_apply")
        if not isinstance(reason, str):
            self._send_json({"error": "reason must be a string"}, status=400)
            return
        try:
            result = run_maintenance_container_sync()
        except Exception:  # never leak a traceback to the UI
            self._send_json(
                {
                    "ok": False,
                    "status": "error",
                    "message": "Container sync failed unexpectedly.",
                },
                status=500,
            )
            return
        status = 200
        if result.get("status") == "unavailable":
            status = 409
        elif not result.get("ok"):
            status = 500
        self._send_json(result, status=status)

    def _handle_maintenance_upgrade_execute(self):
        # Confirmed mutation: bump the EMS image and force-recreate only the EMS
        # service. The target is resolved from the prepared release, not the body.
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, status=400)
            return
        if set(body) - {"confirm", "target_release", "options"}:
            self._send_json({"error": "unsupported upgrade field"}, status=400)
            return
        options = body.get("options", {})
        if not isinstance(options, dict):
            self._send_json({"error": "options must be a JSON object"}, status=400)
            return
        try:
            rejection, run_context = self.server.guided_upgrade.preflight(
                body.get("target_release"),
                options,
                confirm=body.get("confirm") is True,
            )
        except Exception:  # never leak a traceback to the UI
            self._send_json(
                {"ok": False, "status": "error", "message": "Upgrade failed unexpectedly."},
                status=500,
            )
            return
        if rejection is not None:
            self._send_json(
                rejection,
                status=_UPGRADE_STATUS_CODES.get(rejection.get("reason"), 400),
            )
            return
        # Real (slow) upgrade work runs on a job thread; the UI polls for live
        # step progress. Guard checks already passed synchronously above.
        executor = self.server.guided_upgrade
        job = UpgradeJob(uuid.uuid4().hex, plan_upgrade_steps(run_context.options))
        self.server.upgrade_jobs.submit(
            job, lambda handle: handle.finish(executor.run(run_context, progress=handle))
        )
        snapshot = job.snapshot()
        self._send_json(
            {
                "ok": True,
                "job_id": job.job_id,
                "status": snapshot["status"],
                "steps": snapshot["steps"],
            },
            status=202,
        )

    def _send_upgrade_job(self, job_id):
        job = self.server.upgrade_jobs.get(job_id.strip("/"))
        if job is None:
            self._send_json({"ok": False, "error": "unknown job_id"}, status=404)
            return
        self._send_json({"ok": True, **job})

    # --- backup / restore ------------------------------------------------

    def _handle_backups_list(self):
        try:
            result = self.server.backup_service.list_backups()
        except BackupRestoreError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
            return
        except Exception:  # a listing fault must not 500 the read-only route
            self._send_json(
                {"ok": False, "error": "The backup list could not be read."},
                status=500,
            )
            return
        self._send_json(result)

    def _handle_backup_create(self):
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, status=400)
            return
        if set(body) - {"scope", "encrypt", "password", "label", "confirm"}:
            self._send_json({"error": "unsupported backup field"}, status=400)
            return
        if body.get("scope") not in CREATE_SCOPES:
            self._send_json({"error": "unsupported backup scope"}, status=400)
            return
        service = self.server.backup_service
        job = BackupJob(uuid.uuid4().hex, service.plan_create_steps(body["scope"]))

        def runner(handle):
            try:
                result = service.create_backup(body, progress=handle)
            except BackupRestoreError as exc:
                result = {"ok": False, "status": "failed", "message": str(exc)}
            handle.finish(result)

        self.server.backup_jobs.submit(job, runner)
        snapshot = job.snapshot()
        self._send_json(
            {"ok": True, "job_id": job.job_id, "status": snapshot["status"],
             "steps": snapshot["steps"]},
            status=202,
        )

    def _send_backup_job(self, job_id):
        job = self.server.backup_jobs.get(job_id.strip("/"))
        if job is None:
            self._send_json({"ok": False, "error": "unknown job_id"}, status=404)
            return
        self._send_json({"ok": True, **job})

    def _handle_backup_inspect(self):
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict) or set(body) - {"id", "password"}:
            self._send_json({"error": "unsupported inspect field"}, status=400)
            return
        try:
            result = self.server.backup_service.inspect_backup(
                body.get("id"), password=body.get("password")
            )
        except BackupRestoreError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
            return
        self._send_json(result)

    def _handle_backup_diff(self):
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict) or set(body) - {"id", "file", "password"}:
            self._send_json({"error": "unsupported diff field"}, status=400)
            return
        try:
            result = self.server.backup_service.diff_backup_file(
                body.get("id"), body.get("file"), password=body.get("password")
            )
        except BackupRestoreError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
            return
        self._send_json(result)

    def _handle_backup_restore_preview(self):
        body = self._read_json_body()
        if body is None:
            return
        allowed = {"id", "scope", "password", "conflict_policy", "rollback",
                   "auto_rollback"}
        if not isinstance(body, dict) or set(body) - allowed:
            self._send_json({"error": "unsupported preview field"}, status=400)
            return
        if body.get("scope") not in RESTORE_SCOPES:
            self._send_json({"error": "unsupported restore scope"}, status=400)
            return
        if body.get("conflict_policy", "abort") not in CONFLICT_POLICIES:
            self._send_json({"error": "unsupported conflict policy"}, status=400)
            return
        try:
            result = self.server.backup_service.create_restore_plan(body)
        except BackupRestoreError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
            return
        self._send_json(result)

    def _handle_backup_restore_execute(self):
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict) or set(body) - {"plan_id", "confirm"}:
            self._send_json({"error": "unsupported execute field"}, status=400)
            return
        if body.get("confirm") is not True:
            self._send_json(
                {"error": "explicit restore confirmation is required"}, status=400
            )
            return
        plan_id = body.get("plan_id")
        service = self.server.backup_service
        plan = service.plans.get(plan_id) if isinstance(plan_id, str) else None
        if plan is None:
            self._send_json(
                {"ok": False, "error": "unknown or expired restore plan"}, status=409
            )
            return
        if plan.blocked:
            self._send_json(
                {"ok": False, "error": plan.block_reason or "the restore plan is blocked"},
                status=409,
            )
            return
        unsupported = admin_restore_block_reason(plan.targets)
        if unsupported:
            self._send_json({"ok": False, "error": unsupported}, status=409)
            return
        job = BackupJob(uuid.uuid4().hex, service.plan_restore_steps(plan))

        def runner(handle):
            try:
                result = service.restore_from_plan(plan_id, confirm=True, progress=handle)
            except BackupRestoreError as exc:
                result = {"ok": False, "status": "failed", "message": str(exc)}
            handle.finish(result)

        self.server.backup_jobs.submit(job, runner)
        snapshot = job.snapshot()
        self._send_json(
            {"ok": True, "job_id": job.job_id, "status": snapshot["status"],
             "steps": snapshot["steps"]},
            status=202,
        )

    def _handle_backup_delete(self):
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict) or set(body) - {"id", "confirm", "mode"}:
            self._send_json({"error": "unsupported delete field"}, status=400)
            return
        if body.get("confirm") is not True:
            self._send_json(
                {"error": "explicit delete confirmation is required"}, status=400
            )
            return
        mode = body.get("mode", "archive")
        if not isinstance(mode, str):
            self._send_json({"error": "mode must be a string"}, status=400)
            return
        try:
            result = self.server.backup_service.delete_backup(
                body.get("id"), confirm=True, mode=mode
            )
        except BackupRestoreError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
            return
        self._send_json(result)

    def _handle_release_prepare(self):
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, status=400)
            return
        try:
            result = self.server.release_manager.prepare(body.get("tag"))
        except ReleaseError as exc:
            self._send_json({"error": str(exc)}, status=exc.status)
            return
        self._send_json(result)

    def _handle_config_template(self):
        try:
            result = self.server.release_manager.config_template()
        except ReleaseError as exc:
            self._send_json({"error": str(exc)}, status=exc.status)
            return
        self._send_json(result)

    def _handle_config_preview(self):
        body = self._read_json_body(MAX_CONFIG_PREVIEW_BODY_BYTES)
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, status=400)
            return
        draft = body.get("devices", body.get("draft", []))
        if not isinstance(draft, list):
            self._send_json({"error": "devices must be a JSON array"}, status=400)
            return
        count = body.get("supported_grid_meter_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            count = None
        features = body.get("features")
        if not isinstance(features, dict):
            features = None
        self._send_json(
            self.server.config_preview.generate(draft, count, features)
        )

    def _config_export_request(self):
        body = self._read_json_body(MAX_CONFIG_PREVIEW_BODY_BYTES)
        if body is None:
            return None
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, status=400)
            return None
        if "path" in body:
            self._send_json(
                {"error": "custom output paths are not supported"}, status=400
            )
            return None
        draft = body.get("devices", body.get("draft", []))
        if not isinstance(draft, list):
            self._send_json({"error": "devices must be a JSON array"}, status=400)
            return None
        count = body.get("supported_grid_meter_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            count = None
        overwrite = body.get("overwrite", False)
        if not isinstance(overwrite, bool):
            self._send_json({"error": "overwrite must be a boolean"}, status=400)
            return None
        features = body.get("features")
        if not isinstance(features, dict):
            features = None
        return draft, count, overwrite, features

    def _handle_config_download(self):
        request = self._config_export_request()
        if request is None:
            return
        draft, count, _overwrite, features = request
        try:
            payload, _preview = self.server.config_export.serialize(
                draft, count, features
            )
        except ConfigExportValidationError as exc:
            self._send_validation_failure(exc.preview)
            return
        self._send_bytes(
            payload,
            "application/json; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="config.json"'},
        )

    def _handle_config_write(self):
        request = self._config_export_request()
        if request is None:
            return
        draft, count, overwrite, features = request
        try:
            result = self.server.config_export.write(
                draft, count, overwrite, features
            )
        except ConfigExportValidationError as exc:
            self._send_validation_failure(exc.preview)
            return
        except OSError as exc:
            self._send_json(
                {
                    "ok": False,
                    "reason": "write_failed",
                    "message": f"Could not save generated config: {exc}",
                },
                status=500,
            )
            return
        status = 409 if result.get("reason") == "target_exists" else 200
        self._send_json(result, status=status)

    def _handle_config_apply(self):
        request = self._config_export_request()
        if request is None:
            return
        draft, count, _overwrite, features = request
        try:
            result = self.server.config_apply.apply(draft, count, features)
        except ConfigExportValidationError as exc:
            self._send_validation_failure(exc.preview)
            return
        except OSError as exc:
            self._send_json(
                {
                    "ok": False,
                    "reason": "apply_failed",
                    "message": (
                        "Could not apply the config to the EMS installation: "
                        f"{exc}"
                    ),
                },
                status=500,
            )
            return
        self._send_json(result)

    def _handle_deployment_prepare(self):
        body = self._read_optional_json_body()
        if body is None:
            return
        overwrite = body.get("overwrite", False)
        if not isinstance(overwrite, bool):
            self._send_json({"error": "overwrite must be a boolean"}, status=400)
            return
        result = self.server.deployment.prepare(overwrite=overwrite)
        if not result.get("ok", False):
            payload = {
                "ok": False,
                "reason": result.get("reason"),
                "message": result.get("message"),
            }
            if result.get("detail"):
                payload["detail"] = result["detail"]
            for key in ("paths", "existing", "requires_confirmation"):
                if key in result:
                    payload[key] = result[key]
            self._send_json(payload, status=result.get("status", 409))
            return
        self._send_json(result["job"], status=result.get("status", 202))

    def _send_deployment_job(self, job_id):
        job = self.server.deployment.job(job_id.strip("/"))
        if job is None:
            self._send_json({"error": "unknown job_id"}, status=404)
            return
        self._send_json(job)

    def _handle_deployment_start(self):
        body = self._read_optional_json_body()
        if body is None:
            return
        if body:
            self._send_json(
                {"error": "deployment start does not accept parameters"}, status=400
            )
            return
        result = self.server.deployment.start()
        if not result.get("ok", False):
            payload = {
                "ok": False,
                "reason": result.get("reason"),
                "message": result.get("message"),
            }
            if result.get("detail"):
                payload["detail"] = result["detail"]
            self._send_json(payload, status=result.get("status", 409))
            return
        self._send_json(result["job"], status=result.get("status", 202))

    def _send_deployment_start_job(self, job_id):
        job = self.server.deployment.start_job(job_id.strip("/"))
        if job is None:
            self._send_json({"error": "unknown job_id"}, status=404)
            return
        self._send_json(job)

    def _handle_deployment_permission_repair(self):
        body = self._read_optional_json_body()
        if body is None:
            return
        if body:
            self._send_json(
                {"error": "permission repair does not accept parameters"}, status=400
            )
            return
        result = self.server.deployment.repair_workspace_permissions()
        if not result.get("ok", False):
            payload = {
                "ok": False,
                "reason": result.get("reason"),
                "message": result.get("message"),
            }
            if result.get("detail"):
                payload["detail"] = result["detail"]
            self._send_json(payload, status=result.get("status", 409))
            return
        self._send_json(result)

    def _handle_deployment_container_conflict(self):
        body = self._read_json_body()
        if body is None:
            return
        container_name = body.get("container_name")
        action = body.get("action")
        if not isinstance(container_name, str) or not isinstance(action, str):
            self._send_json(
                {"error": "container_name and action must be strings"}, status=400
            )
            return
        result = self.server.deployment.resolve_container_conflict(
            container_name, action
        )
        if not result.get("ok", False):
            payload = {
                "ok": False,
                "reason": result.get("reason"),
                "message": result.get("message"),
            }
            if result.get("detail"):
                payload["detail"] = result["detail"]
            self._send_json(payload, status=result.get("status", 409))
            return
        self._send_json(result)

    def _send_validation_failure(self, preview):
        self._send_json(
            {
                "ok": False,
                "reason": "validation_failed",
                "message": "Fix config validation errors before exporting.",
                "validation": preview["validation"],
            },
            status=422,
        )

    def _handle_scan(self):
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, status=400)
            return

        try:
            validate_cidr(body.get("cidr"))
        except CidrValidationError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return

        timeout_ms = clamp_timeout_ms(body.get("timeout_ms"))
        max_workers = clamp_max_workers(body.get("max_workers"))
        ports = self._normalize_ports(body.get("ports"))

        record = self.server.registry.start(
            body["cidr"].strip(), ports, timeout_ms, max_workers
        )
        self._send_json(_result_view(record), status=202)

    def _handle_gateway_probe(self):
        body = self._read_optional_json_body()
        if body is None:
            return
        result = self.server.gateway_prober(
            timeout_ms=body.get("timeout_ms"),
            max_workers=body.get("max_workers"),
        )
        self._send_json(result)

    def _handle_mqtt_probe(self):
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, status=400)
            return
        try:
            validate_cidr(body.get("cidr"))
        except CidrValidationError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        result = self.server.mqtt_discovery.probe(
            body["cidr"].strip(),
            timeout_ms=body.get("timeout_ms"),
            max_workers=body.get("max_workers"),
        )
        self._send_json(result)

    def _send_result(self, scan_id):
        record = self.server.registry.get(scan_id.strip("/"))
        if record is None:
            self._send_json({"error": "unknown scan_id"}, status=404)
            return
        self._send_json(_result_view(record))

    def _normalize_ports(self, ports):
        if not isinstance(ports, list):
            return list(DEFAULT_PORTS)
        valid = [p for p in ports if isinstance(p, int) and 1 <= p <= 65535]
        return valid or list(DEFAULT_PORTS)

    # --- IO helpers ------------------------------------------------------

    def _read_json_body(self, max_bytes=MAX_JSON_BODY_BYTES):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            self._send_json({"error": "invalid Content-Length"}, status=400)
            return None
        if length <= 0:
            self._send_json({"error": "empty body"}, status=400)
            return None
        if length > max_bytes:
            self._send_json({"error": "request body too large"}, status=413)
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json({"error": "invalid JSON body"}, status=400)
            return None

    def _drain_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return
        if 0 < length <= MAX_JSON_BODY_BYTES:
            self.rfile.read(length)

    def _read_optional_json_body(self):
        """Read an optional JSON object body; ``{}`` when absent, ``None`` on error."""

        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            self._send_json({"error": "invalid Content-Length"}, status=400)
            return None
        if length <= 0:
            return {}
        if length > MAX_JSON_BODY_BYTES:
            self._send_json({"error": "request body too large"}, status=413)
            return None
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json({"error": "invalid JSON body"}, status=400)
            return None
        if not isinstance(parsed, dict):
            self._send_json({"error": "expected a JSON object"}, status=400)
            return None
        return parsed

    def _send_static(self, request_path):
        key = static_asset_key(request_path)
        asset = self.server.static_assets.get(key) if key is not None else None
        if asset is None:
            self._send_json({"error": "not found"}, status=404)
            return
        full_path, content_type = asset
        with open(full_path, "rb") as handle:
            self._send_bytes(handle.read(), content_type)

    def _send_json(self, payload, status=200):
        self._send_bytes(
            json.dumps(payload).encode("utf-8"),
            "application/json; charset=utf-8",
            status=status,
        )

    def _send_bytes(self, body, content_type, status=200, headers=None):
        self.send_response(status)
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _result_view(record):
    """Public result shape returned by the scan/result endpoints."""

    return {
        "scan_id": record["scan_id"],
        "status": record["status"],
        "cidr": record["cidr"],
        "started_at": record["started_at"],
        "finished_at": record["finished_at"],
        "devices": record["devices"],
        "errors": record["errors"],
    }


def create_server(host="127.0.0.1", port=8090, registry=None, static_assets=None,
                  network_detector=None, gateway_prober=None, mdns_provider=None,
                  mqtt_discovery=None, release_manager=None, config_export=None,
                  config_apply=None, deployment=None, ems_cli=None,
                  guided_upgrade=None, backup_service=None):
    """Create (but do not start) an ``AdminServer`` bound to ``host:port``."""

    return AdminServer(
        (host, int(port)), AdminHandler, registry, static_assets, network_detector,
        gateway_prober, mdns_provider, mqtt_discovery, release_manager,
        config_export, config_apply, deployment, ems_cli, guided_upgrade,
        backup_service,
    )
