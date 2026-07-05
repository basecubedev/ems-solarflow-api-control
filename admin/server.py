# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lightweight admin discovery HTTP server.

Stdlib ``http.server`` only, no framework, no database. Scan state is kept in a
small in-memory registry; each scan runs on a bounded background thread so the
POST returns immediately and the UI polls the result endpoint. The server never
reads secrets. It only writes the real EMS ``config.json`` on the explicit
``config/apply`` action; preview and export stay non-destructive. Maintenance
offers a backup by default and requires explicit confirmation before apply.
"""

import hmac
import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from admin import auth as admin_auth
from admin.admin_update import AdminUpdateService
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
from dashboard.auth import LoginRateLimiter, SessionStore
from dashboard.static_files import build_static_asset_index, static_asset_key

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_JSON_BODY_BYTES = 4 * 1024
MAX_CONFIG_PREVIEW_BODY_BYTES = 64 * 1024
MAX_TRACKED_SCANS = 20

# Admin uses its own session cookie: the password is shared with the EMS
# Dashboard, but the browser sessions are separate (a Dashboard login must not
# grant Admin access and vice versa).
ADMIN_SESSION_COOKIE_NAME = "ems_admin_session"
MIN_PASSWORD_LENGTH = 8

# Only these paths are reachable without an Admin session. Everything else
# (setup, maintenance, discovery, config, deployment, backup) requires auth.
PUBLIC_POST_PATHS = frozenset(
    {
        "/api/admin/auth/setup",
        "/api/admin/auth/login",
        "/api/admin/auth/logout",
    }
)


def _validate_new_password(password, confirm):
    """Validate an initial password. Returns an error code or ``None``."""

    if not isinstance(password, str) or not password:
        return "password_required"
    if len(password) < MIN_PASSWORD_LENGTH:
        return "password_too_short"
    if not isinstance(confirm, str) or password != confirm:
        return "password_mismatch"
    return None


def _hmac_compare(left, right):
    return hmac.compare_digest(str(left), str(right))

# Guided-upgrade preflight rejection reasons -> HTTP status. Accepted runs spawn
# a job (202) whose live step progress is polled; only rejections use this map.
_UPGRADE_STATUS_CODES = {
    "confirm_required": 400,
    "target_required": 400,
    "target_not_prepared": 409,
    "config_missing": 409,
    "compose_missing": 409,
}

# Admin self-update execute() error codes -> HTTP status. A missing/mismatched
# plan is a conflict; a failed updater launch is a server error; everything else
# (confirm/plan validation) is a bad request.
_ADMIN_UPDATE_STATUS_CODES = {
    "confirm_required": 400,
    "plan_required": 400,
    "unknown_plan": 409,
    "state_corrupt": 409,
    "state_unreadable": 409,
    "updater_start_failed": 500,
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
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'self'"
    ),
    "Permissions-Policy": (
        "geolocation=(), microphone=(), camera=(), payment=(), usb=()"
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


@dataclass
class AdminRuntime:
    """Shared Admin state backing both the HTTP and (optional) HTTPS listeners.

    The parallel HTTPS listener reuses one runtime so sessions, rate limiters,
    discovery state, mDNS, and the backup/upgrade job registries are never
    duplicated across transports; only ``AdminServer.https_active`` differs.
    """

    registry: ScanRegistry
    network_detector: object
    gateway_prober: object
    mqtt_discovery: MqttBrokerDiscovery
    mdns_provider: MdnsProvider
    release_manager: ReleaseManager
    config_preview: ConfigPreviewGenerator
    config_export: ConfigExportService
    config_apply: ConfigApplyService
    deployment: DeploymentService
    ems_cli: EmsCliDiagnostics
    guided_upgrade: GuidedUpgradeExecutor
    upgrade_jobs: UpgradeJobRegistry
    admin_update: AdminUpdateService
    backup_service: BackupRestoreService
    backup_jobs: BackupJobRegistry
    auth_sessions: SessionStore
    auth_login_limiter: LoginRateLimiter
    auth_setup_limiter: LoginRateLimiter
    static_assets: dict = field(default_factory=dict)
    # Whether the process started an optional HTTPS listener at all (global; the
    # per-request transport is reported separately via AdminServer.https_active).
    https_configured: bool = False
    https_port: int = 8091


def create_admin_runtime(
    registry=None,
    static_assets=None,
    network_detector=None,
    gateway_prober=None,
    mdns_provider=None,
    mqtt_discovery=None,
    release_manager=None,
    config_export=None,
    config_apply=None,
    deployment=None,
    ems_cli=None,
    guided_upgrade=None,
    admin_update=None,
    backup_service=None,
):
    """Build the shared Admin service graph once, for one or more listeners."""

    registry = registry or ScanRegistry()
    network_detector = network_detector or detect_network_suggestions
    gateway_prober = gateway_prober or probe_gateway_candidates
    mqtt_discovery = mqtt_discovery or MqttBrokerDiscovery()
    mdns_provider = mdns_provider or MdnsProvider(
        mqtt_handler=mqtt_discovery.add_mdns_candidate
    )
    # Give the release manager a read-only Docker inspector so it can compare a
    # running build's identity against release targets; harmless when the daemon
    # is absent (all inspections degrade to an unknown identity).
    release_manager = release_manager or ReleaseManager(docker=DockerCli())
    config_preview = ConfigPreviewGenerator(release_manager)
    admin_data_dir = getattr(release_manager, "data_dir", default_admin_data_dir())
    config_export = config_export or ConfigExportService(
        config_preview, admin_data_dir
    )
    config_apply = config_apply or ConfigApplyService(config_export, admin_data_dir)
    deployment = deployment or DeploymentService(
        release_manager, config_export, admin_data_dir=admin_data_dir
    )
    ems_cli = ems_cli or EmsCliDiagnostics()
    guided_upgrade = guided_upgrade or GuidedUpgradeExecutor(
        release_manager=release_manager, ems_cli=ems_cli
    )
    # Admin self-update: read-only status/plan plus an out-of-request updater. It
    # reuses the release manager's Admin data dir for the pending-state file and a
    # read-only Docker inspector for build identity; both degrade safely when the
    # daemon is absent.
    admin_update = admin_update or AdminUpdateService(
        docker=DockerCli(), release_manager=release_manager
    )
    static_assets = (
        static_assets
        if static_assets is not None
        else build_static_asset_index(STATIC_DIR)
    )
    return AdminRuntime(
        registry=registry,
        network_detector=network_detector,
        gateway_prober=gateway_prober,
        mqtt_discovery=mqtt_discovery,
        mdns_provider=mdns_provider,
        release_manager=release_manager,
        config_preview=config_preview,
        config_export=config_export,
        config_apply=config_apply,
        deployment=deployment,
        ems_cli=ems_cli,
        guided_upgrade=guided_upgrade,
        upgrade_jobs=UpgradeJobRegistry(),
        admin_update=admin_update,
        backup_service=backup_service or BackupRestoreService(),
        backup_jobs=BackupJobRegistry(),
        # Shared password, separate Admin session. Setup can be hammered but only
        # succeeds once, so a simple per-address limiter is enough there.
        auth_sessions=SessionStore(timeout_seconds=1800, absolute_max_seconds=43200),
        auth_login_limiter=LoginRateLimiter(),
        auth_setup_limiter=LoginRateLimiter(max_failures=10, window_seconds=60),
        static_assets=static_assets,
    )


class AdminServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler, runtime=None, *, https_active=False):
        super().__init__(server_address, handler)
        # Both listeners point at one AdminRuntime; the handlers keep reading
        # self.server.<attr>, so expose each shared service as an attribute.
        self.runtime = runtime if runtime is not None else create_admin_runtime()
        runtime = self.runtime
        self.registry = runtime.registry
        self.network_detector = runtime.network_detector
        self.gateway_prober = runtime.gateway_prober
        self.mqtt_discovery = runtime.mqtt_discovery
        self.mdns_provider = runtime.mdns_provider
        self.release_manager = runtime.release_manager
        self.config_preview = runtime.config_preview
        self.config_export = runtime.config_export
        self.config_apply = runtime.config_apply
        self.deployment = runtime.deployment
        self.ems_cli = runtime.ems_cli
        self.guided_upgrade = runtime.guided_upgrade
        self.upgrade_jobs = runtime.upgrade_jobs
        self.admin_update = runtime.admin_update
        self.backup_service = runtime.backup_service
        self.backup_jobs = runtime.backup_jobs
        self.auth_sessions = runtime.auth_sessions
        self.auth_login_limiter = runtime.auth_login_limiter
        self.auth_setup_limiter = runtime.auth_setup_limiter
        self.static_assets = runtime.static_assets
        # Local HTTP appliance: the Secure cookie flag is only added when this
        # listener is the HTTPS one (never required for local HTTP).
        self.https_active = bool(https_active)


class AdminHandler(BaseHTTPRequestHandler):
    server_version = "AdminDiscovery/1.0"

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("", "/", "/index.html"):
            self._send_static("/index.html")
            return
        # The unauthenticated browser must be able to load the login/setup UI and
        # read auth status; every other GET below requires a valid Admin session.
        if path == "/api/admin/auth/status":
            self._send_json(self._auth_status_payload())
            return
        if not path.startswith("/api/"):
            self._send_static(path)
            return
        auth_error = self._require_admin_read_auth()
        if auth_error:
            self._send_json(auth_error[0], status=auth_error[1])
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
        if path == "/api/admin/maintenance/admin-update/status":
            self._handle_admin_update_status()
            return
        if path == "/api/admin/maintenance/admin-update/resume":
            self._handle_admin_update_resume()
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
        # Only authenticated /api/ paths reach here; static was served above.
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path in PUBLIC_POST_PATHS:
            self._handle_public_auth_post(path)
            return
        auth_error = self._require_admin_write_auth()
        if auth_error:
            self._send_json(auth_error[0], status=auth_error[1])
            return
        if path == "/api/admin/auth/refresh":
            # Genuine-activity heartbeat: slide the idle timeout. It is treated as
            # a state change, so the write-auth gate above already required a valid
            # session and CSRF token.
            self.server.auth_sessions.touch(self._admin_session_cookie_value())
            self._send_json(self._auth_status_payload())
            return
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
        if path == "/api/admin/maintenance/admin-update/plan":
            self._handle_admin_update_plan()
            return
        if path == "/api/admin/maintenance/admin-update/execute":
            self._handle_admin_update_execute()
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

    # --- auth ------------------------------------------------------------

    def _handle_public_auth_post(self, path):
        if path == "/api/admin/auth/setup":
            self._handle_auth_setup()
        elif path == "/api/admin/auth/login":
            self._handle_auth_login()
        elif path == "/api/admin/auth/logout":
            self._handle_auth_logout()

    def _auth_status_payload(self):
        paths = admin_auth.resolve_admin_auth_paths()
        auth = admin_auth.admin_auth_status()
        session = self._current_admin_session() if auth.valid else None
        authenticated = session is not None
        payload = {
            "auth_configured": auth.configured,
            "authenticated": authenticated,
            "requires_initial_password": not auth.configured,
            "recovery_required": auth.recovery_required,
        }
        if auth.recovery_required:
            # A malformed file must not offer first-password setup; report a clear
            # recovery state instead.
            payload["error"] = auth.error
            payload["message"] = auth.message
        elif not auth.configured:
            payload["shared_password_file"] = admin_auth.SHARED_AUTH_HINT
            payload["message"] = "Create a password to protect the Admin Console."
        elif not authenticated:
            payload["message"] = "Use your EMS Dashboard password."
        if paths.warning:
            payload["warning"] = paths.warning
        if authenticated:
            payload["csrf_token"] = session.csrf_token
            if session.expires_at is None:
                payload["session_expires_in_seconds"] = None
            else:
                remaining = session.expires_at - self.server.auth_sessions.time_fn()
                payload["session_expires_in_seconds"] = max(0, int(remaining))
        return payload

    def _handle_auth_setup(self):
        remote = self.client_address[0] if self.client_address else "unknown"
        if self.server.auth_setup_limiter.is_limited(remote):
            self._send_json({"error": "setup_rate_limited"}, status=429)
            return
        auth = admin_auth.admin_auth_status()
        if auth.recovery_required:
            # A malformed auth file must not reopen first-password setup and must
            # never be overwritten here.
            self._send_json(
                {"error": auth.error, "message": auth.message}, status=409
            )
            return
        if auth.configured:
            self._send_json(
                {
                    "error": "auth_already_configured",
                    "message": "Password is already configured. Please log in.",
                },
                status=409,
            )
            return
        if not admin_auth.install_dir_available():
            self._send_json(
                {
                    "error": "install_dir_unavailable",
                    "message": (
                        "Admin install directory is not mounted. "
                        "Start the Admin Console with install-admin-console.sh."
                    ),
                },
                status=409,
            )
            return
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, status=400)
            return
        error = _validate_new_password(body.get("password"), body.get("confirm_password"))
        if error:
            self.server.auth_setup_limiter.record_failure(remote)
            self._send_json({"error": error}, status=400)
            return
        try:
            # Initial password creation must not overwrite a password created by
            # another browser; the atomic helper turns that race into a 409.
            admin_auth.create_shared_password_if_missing(body["password"])
        except FileExistsError:
            self._send_json(
                {
                    "error": "auth_already_configured",
                    "message": "Password is already configured. Please log in.",
                },
                status=409,
            )
            return
        except OSError as exc:
            self._send_json(
                {"error": "auth_write_failed", "message": f"Could not save the password: {exc}"},
                status=500,
            )
            return
        session = self.server.auth_sessions.create()
        self._send_json(
            {**self._auth_status_payload(), "authenticated": True,
             "csrf_token": session.csrf_token},
            headers={"Set-Cookie": self._admin_session_cookie(session.session_id)},
        )

    def _handle_auth_login(self):
        remote = self.client_address[0] if self.client_address else "unknown"
        auth = admin_auth.admin_auth_status()
        if auth.recovery_required:
            self._send_json(
                {"error": auth.error, "message": auth.message}, status=409
            )
            return
        if not auth.configured:
            self._send_json({"error": "auth_not_configured"}, status=403)
            return
        if self.server.auth_login_limiter.is_limited(remote):
            self._send_json({"error": "login_rate_limited"}, status=429)
            return
        body = self._read_json_body()
        if body is None:
            return
        password = body.get("password") if isinstance(body, dict) else None
        if not isinstance(password, str) or not admin_auth.verify_admin_password(password):
            self.server.auth_login_limiter.record_failure(remote)
            self._send_json({"error": "invalid_password"}, status=403)
            return
        self.server.auth_login_limiter.reset(remote)
        session = self.server.auth_sessions.create()
        self._send_json(
            {**self._auth_status_payload(), "authenticated": True,
             "csrf_token": session.csrf_token},
            headers={"Set-Cookie": self._admin_session_cookie(session.session_id)},
        )

    def _handle_auth_logout(self):
        # Logout takes no input and may clear the cookie even if already logged
        # out, so any accidental body is drained rather than required.
        self._drain_body()
        self.server.auth_sessions.destroy(self._admin_session_cookie_value())
        self._send_json(
            self._auth_status_payload(),
            headers={"Set-Cookie": self._expired_admin_session_cookie()},
        )

    def _require_admin_read_auth(self):
        # Side-effect-free GET endpoints need a valid session but not a CSRF
        # token (SameSite=Strict + CSP frame-ancestors 'none' already block
        # cross-site cookie use), mirroring the Dashboard read-auth model.
        auth = admin_auth.admin_auth_status()
        if auth.recovery_required:
            return {"error": auth.error, "message": auth.message}, 403
        if not auth.configured:
            return {"error": "auth_not_configured"}, 403
        if self._current_admin_session() is None:
            return {"error": "not_authenticated"}, 401
        return None

    def _require_admin_write_auth(self):
        auth = admin_auth.admin_auth_status()
        if auth.recovery_required:
            return {"error": auth.error, "message": auth.message}, 403
        if not auth.configured:
            return {"error": "auth_not_configured"}, 403
        session = self._current_admin_session()
        if session is None:
            return {"error": "not_authenticated"}, 401
        csrf_token = self.headers.get("X-CSRF-Token", "")
        if not csrf_token or not _hmac_compare(csrf_token, session.csrf_token):
            return {"error": "csrf_failed"}, 403
        return None

    def _current_admin_session(self):
        return self.server.auth_sessions.get(self._admin_session_cookie_value())

    def _admin_session_cookie_value(self):
        raw = self.headers.get("Cookie", "")
        if not raw:
            return None
        parsed = cookies.SimpleCookie()
        try:
            parsed.load(raw)
        except cookies.CookieError:
            return None
        morsel = parsed.get(ADMIN_SESSION_COOKIE_NAME)
        return morsel.value if morsel else None

    def _admin_session_cookie(self, session_id):
        parts = [
            f"{ADMIN_SESSION_COOKIE_NAME}={session_id}",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
        ]
        if getattr(self.server, "https_active", False):
            parts.append("Secure")
        return "; ".join(parts)

    def _expired_admin_session_cookie(self):
        parts = [
            f"{ADMIN_SESSION_COOKIE_NAME}=",
            "Path=/",
            "Max-Age=0",
            "HttpOnly",
            "SameSite=Strict",
        ]
        if getattr(self.server, "https_active", False):
            parts.append("Secure")
        return "; ".join(parts)

    # --- handlers --------------------------------------------------------

    def _status_payload(self):
        return {
            "service": "ems-solarflow-admin",
            "version": "admin",
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
                "guided_upgrade",
                "admin_container_update",
                "backup_restore",
                "backup_delete",
                "restore_preview",
            ],
            "writes_config": True,
            "writes_generated_config": True,
            "admin_https": self._admin_https_status(),
            "time": utc_now_iso(),
        }

    def _admin_https_status(self):
        # One listener answers each request: ``current_request_https`` reflects
        # this transport, ``configured`` reflects whether the process started the
        # optional HTTPS listener at all (shared via the runtime).
        runtime = getattr(self.server, "runtime", None)
        return {
            "configured": bool(getattr(runtime, "https_configured", False)),
            "current_request_https": bool(getattr(self.server, "https_active", False)),
            "port": int(getattr(runtime, "https_port", 8091)),
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

    # --- Admin Console self-update ---------------------------------------

    def _handle_admin_update_status(self):
        # Read-only: current Admin identity and any pending update. Docker being
        # unavailable is a valid (unsupported) status, never a 500.
        try:
            payload = self.server.admin_update.status()
        except Exception:  # a status fault must never 500 the read-only route
            self._send_json(
                {
                    "ok": False,
                    "supported": False,
                    "reason": "status_unavailable",
                    "message": "Could not read the Admin update status.",
                    "current_admin": None,
                    "pending": None,
                },
                status=200,
            )
            return
        self._send_json(payload)

    def _handle_admin_update_resume(self):
        try:
            payload = self.server.admin_update.resume()
        except Exception:
            self._send_json(
                {
                    "ok": False,
                    "error": "resume_unavailable",
                    "message": "Could not read the pending Admin update.",
                    "pending": None,
                    "resume_available": False,
                },
                status=200,
            )
            return
        self._send_json(payload)

    def _handle_admin_update_plan(self):
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, status=400)
            return
        if set(body) - {"target_release"}:
            self._send_json({"error": "unsupported admin-update field"}, status=400)
            return
        target_release = body.get("target_release")
        try:
            payload = self.server.admin_update.plan(target_release)
        except ValueError as exc:
            # An invalid tag or a smuggled image ref is rejected server-side.
            self._send_json(
                {"ok": False, "error": "invalid_release", "message": str(exc)},
                status=400,
            )
            return
        except Exception:
            self._send_json(
                {"ok": False, "error": "plan_failed",
                 "message": "Could not plan the Admin Console update."},
                status=500,
            )
            return
        self._send_json(payload)

    def _handle_admin_update_execute(self):
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, status=400)
            return
        if set(body) - {"plan_id", "confirm"}:
            self._send_json({"error": "unsupported admin-update field"}, status=400)
            return
        try:
            result = self.server.admin_update.execute(
                body.get("plan_id"), body.get("confirm") is True
            )
        except Exception:
            self._send_json(
                {"ok": False, "error": "execute_failed",
                 "message": "The Admin Console update failed unexpectedly."},
                status=500,
            )
            return
        if result.get("ok"):
            self._send_json(result, status=202)
            return
        self._send_json(
            result, status=_ADMIN_UPDATE_STATUS_CODES.get(result.get("error"), 400)
        )

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
        if body.get("conflict_policy", "replace") not in CONFLICT_POLICIES:
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

    def _send_json(self, payload, status=200, headers=None):
        self._send_bytes(
            json.dumps(payload).encode("utf-8"),
            "application/json; charset=utf-8",
            status=status,
            headers=headers,
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
                  guided_upgrade=None, admin_update=None, backup_service=None,
                  runtime=None, https_active=False):
    """Create (but do not start) an ``AdminServer`` bound to ``host:port``.

    Pass a shared ``runtime`` to bind a second (HTTPS) listener to the same
    Admin state; otherwise one is built from the individual service overrides.
    """

    if runtime is None:
        runtime = create_admin_runtime(
            registry=registry,
            static_assets=static_assets,
            network_detector=network_detector,
            gateway_prober=gateway_prober,
            mdns_provider=mdns_provider,
            mqtt_discovery=mqtt_discovery,
            release_manager=release_manager,
            config_export=config_export,
            config_apply=config_apply,
            deployment=deployment,
            ems_cli=ems_cli,
            guided_upgrade=guided_upgrade,
            admin_update=admin_update,
            backup_service=backup_service,
        )
    return AdminServer(
        (host, int(port)), AdminHandler, runtime=runtime, https_active=https_active,
    )
