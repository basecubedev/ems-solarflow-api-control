# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lightweight admin discovery HTTP server.

Stdlib ``http.server`` only, no framework, no database. Scan state is kept in a
small in-memory registry; each scan runs on a bounded background thread so the
POST returns immediately and the UI polls the result endpoint. The server never
reads secrets. It only writes the real EMS ``config.json`` on the explicit
``config/apply`` action; preview and export stay non-destructive. Maintenance
offers a backup by default and requires explicit confirmation before apply.
"""

import copy
import hmac
import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from admin import auth as admin_auth
from admin.admin_update import (
    DEFAULT_ADMIN_CONTAINER,
    TRANSITION_MODE_AUTOMATED_SETUP,
    TRANSITION_MODE_FRESH_INSTALL,
    TRANSITION_MODE_GUIDED_UPGRADE,
    AdminUpdateService,
    PendingTransitionStore,
    SystemTransitionLauncher,
    admin_image_ref_from_env,
)
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
from admin.discovery_preparation import (
    DISCOVERY_SOURCES,
    SOURCE_LOCAL_API,
    SOURCE_LOCAL_MQTT,
    DiscoveryPreparationError,
    DiscoveryPreparationStore,
)
from admin.discovery_connections import (
    DiscoveryConnectionsError,
    DiscoveryConnectionsStore,
)
from admin.credential_store import CredentialStore, CredentialStoreError
from admin.discovery_run import run_discovery
from admin.ems_cli import EmsCliDiagnostics
from admin.embedded_resources import (
    EmbeddedReleaseResources,
    ReleaseArchiveResources,
)
from admin.backup_restore import (
    BackupJob,
    BackupJobRegistry,
    BackupRestoreError,
    BackupRestoreService,
    CONFLICT_POLICIES,
    CREATE_SCOPES,
    RESTORE_SCOPES,
)
from admin.gateway_probe import probe_gateway_candidates
from admin.guided_upgrade import (
    GuidedUpgradeExecutor,
    UpgradeJob,
    UpgradeJobRegistry,
    plan_upgrade_steps,
)
from admin.guided_upgrade_context import (
    GuidedUpgradeContextPersistenceError,
    GuidedUpgradeContextStore,
)
from admin.install_context import detect_install_context
from admin.image_identity import identify_image
from admin.operation_coordinator import OperationCoordinator
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
from admin.container_names import DEFAULT_EMS_CONTAINER
from admin.maintenance import run_maintenance_overview
from admin.maintenance_config import (
    load_maintenance_config,
    prepare_maintenance_config_apply,
    preview_maintenance_config,
    redact_config_for_browser,
)
from admin.mdns import MdnsProvider
from admin.mqtt_discovery import MqttBrokerDiscovery
from admin.mqtt_topic_discovery import default_topic_discoverer
from admin.models import SOURCE_ZENDURE_CLOUD_MQTT, utc_now_iso
from admin.mqtt_runtime_provisioning import (
    stage_runtime_credentials_for_config,
    stage_setup_runtime_credentials,
)
from admin.secret_store import SecretStoreError
from admin.zendure_cloud_auth import ZendureCloudError
from admin.zendure_cloud_mqtt import (
    ZendureCloudDiscovery,
    credential_mode_is_supported,
)
from admin import zendure_mqtt_config_proposals
from admin.zendure_mqtt_migration_review import (
    load_migration_review,
    prepare_migration_apply,
)
from admin.zendure_mqtt_runtime_status import build_runtime_status_view
from admin.networks import detect_network_suggestions
from admin.development_catalogue import development_catalogue_source
from admin.releases import ReleaseError, ReleaseManager, default_admin_data_dir
from admin.known_good import KnownGoodStore
from admin.setup_config import build_setup_catalog
from admin.setup_intent import (
    SetupIntentError,
    SetupIntentStore,
    default_runtime_state_fingerprint,
)
from admin.system_alignment import (
    SystemAlignmentError,
    SystemAlignmentService,
    terminal_system_build_action_state,
)
from admin.system_health import (
    HealthValidationResult,
    validate_system_health_result,
)
from admin.system_build import (
    CachingBuildResolver,
    SystemBuildError,
    SystemBuildResolver,
    is_development_build_tag,
)
from dashboard.auth import LoginRateLimiter, SessionStore
from dashboard.static_files import build_static_asset_index, static_asset_key

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_JSON_BODY_BYTES = 4 * 1024
MAX_CONFIG_PREVIEW_BODY_BYTES = 64 * 1024
MAX_ZENDURE_MQTT_PREVIEW_PROPOSALS = 20
MAX_TRACKED_SCANS = 20

# Admin uses its own session cookie: the password is shared with the EMS
# Dashboard, but the browser sessions are separate (a Dashboard login must not
# grant Admin access and vice versa).
ADMIN_SESSION_COOKIE_NAME = "ems_admin_session"

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
    """Validate an initial password. Returns an error code or ``None``.

    The shared EMS Dashboard/Admin password has no length or complexity
    requirement: any non-empty value is accepted. Only structurally invalid
    requests (missing password, confirmation mismatch) are rejected here.
    """

    if not isinstance(password, str) or not password:
        return "password_required"
    if not isinstance(confirm, str) or password != confirm:
        return "password_mismatch"
    return None


def _hmac_compare(left, right):
    return hmac.compare_digest(str(left), str(right))


def _surface_mqtt_credential_consumer_issues(preview):
    """Add the global MQTT credentials_ref contract issues to a Setup preview.

    Read-only: mirrors the Maintenance preview so both flows show a bad
    reference (non-canonical or cross-source) before an apply is attempted. The
    apply itself is unaffected — it defers to credential staging, which rejects
    the same references with a stable structured code.
    """

    if not isinstance(preview, dict):
        return
    from ems.mqtt_credentials import find_mqtt_credential_consumer_issues

    issues = find_mqtt_credential_consumer_issues(preview.get("config"))
    if not issues:
        return
    validation = preview.setdefault("validation", {"errors": [], "warnings": [], "info": []})
    validation.setdefault("errors", [])
    for issue in issues:
        validation["errors"].append(
            {"code": issue["code"], "message": issue["message"]}
        )
    preview["ready"] = False

# Guided-upgrade preflight rejection reasons -> HTTP status. Accepted runs spawn
# a job (202) whose live step progress is polled; only rejections use this map.
_UPGRADE_STATUS_CODES = {
    "confirm_required": 400,
    "target_required": 400,
    "target_not_prepared": 409,
    "config_missing": 409,
    "compose_missing": 409,
    "system_build_verification_required": 409,
    "system_build_verification_stale": 409,
}

_TRUSTED_UPGRADE_FAILURE_CODES = frozenset(
    {
        "system_build_registry_rate_limited",
        "image_pull_rate_limited",
        "image_pull_network_error",
        "image_pull_failed",
        "target_digest_mismatch",
    }
)

# Admin self-update execute() error codes -> HTTP status. A missing/mismatched
# plan is a conflict; a failed updater launch is a server error; everything else
# (confirm/plan validation) is a bad request.
_ADMIN_UPDATE_STATUS_CODES = {
    "confirm_required": 400,
    "plan_required": 400,
    "admin_update_not_required": 409,
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
            "progress": {
                "total_hosts": 0,
                "checked_hosts": 0,
                "found_devices": 0,
                "failed_hosts": 0,
                "percent": 0,
            },
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

    def all_devices(self):
        """Union of devices across every tracked scan, deduplicated by id.

        Later scans win on collision so a re-scan refreshes a host's fields.
        """

        merged = {}
        with self._lock:
            records = [self._scans[scan_id] for scan_id in self._order if scan_id in self._scans]
        for record in records:
            for device in record.get("devices") or []:
                key = device.get("id") or f"{device.get('api_family')}:{device.get('ip')}"
                merged[key] = device
        return list(merged.values())

    def _progress_updater(self, scan_id):
        def update(info):
            total = info.get("total_hosts") or 0
            checked = info.get("checked_hosts") or 0
            percent = int(checked / total * 100) if total else 0
            with self._lock:
                record = self._scans.get(scan_id)
                if record is None:
                    return
                record["progress"] = {
                    "total_hosts": total,
                    "checked_hosts": checked,
                    "found_devices": info.get("found_devices", 0),
                    "failed_hosts": info.get("failed_hosts", 0),
                    "percent": percent,
                }
        return update

    def _run(self, scan_id, cidr, timeout_ms, max_workers):
        try:
            devices, errors = self._scan_runner(
                cidr, timeout_ms=timeout_ms, max_workers=max_workers,
                progress_callback=self._progress_updater(scan_id),
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
            # A finished scan reports full progress even if the callback never
            # ran (empty range, or a scan_runner that ignores the callback).
            if payload["status"] == "finished":
                total = record["progress"].get("total_hosts") or \
                    record["progress"].get("checked_hosts") or 0
                record["progress"] = {
                    "total_hosts": total,
                    "checked_hosts": total,
                    "found_devices": len(payload["devices"]),
                    "failed_hosts": len(payload["errors"]),
                    "percent": 100,
                }

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
    zendure_cloud_discovery: ZendureCloudDiscovery
    discovery_preparation: DiscoveryConnectionsStore
    credential_store: CredentialStore
    mdns_provider: MdnsProvider
    release_manager: ReleaseManager
    config_preview: ConfigPreviewGenerator
    config_export: ConfigExportService
    config_apply: ConfigApplyService
    deployment: DeploymentService
    ems_cli: EmsCliDiagnostics
    guided_upgrade: GuidedUpgradeExecutor
    upgrade_jobs: UpgradeJobRegistry
    guided_upgrade_context: GuidedUpgradeContextStore
    system_alignment: SystemAlignmentService
    admin_update: AdminUpdateService
    backup_service: BackupRestoreService
    backup_jobs: BackupJobRegistry
    auth_sessions: SessionStore
    auth_login_limiter: LoginRateLimiter
    auth_setup_limiter: LoginRateLimiter
    admin_instance_id: str
    setup_intents: SetupIntentStore
    static_assets: dict = field(default_factory=dict)
    # The single authority for worker liveness and atomic abandonment, shared by
    # both listeners. Guided Upgrade and deployment workers claim it before they
    # start mutating and release it when they stop; expired-transition abandon
    # is coordinated through it so a worker can never register between "proven
    # inactive" and durable cancellation.
    operation_coordinator: OperationCoordinator = field(
        default_factory=OperationCoordinator
    )
    # Whether the process started an optional HTTPS listener at all (global; the
    # per-request transport is reported separately via AdminServer.https_active).
    https_configured: bool = False
    https_port: int = 8091


def _running_admin_identity(docker):
    """Read the running Admin's trusted image identity without browser input."""

    container_name = os.environ.get("EMS_ADMIN_CONTAINER_NAME", DEFAULT_ADMIN_CONTAINER)
    image_ref = None
    try:
        container = docker.inspect_container(container_name)
    except Exception:
        container = None
    if isinstance(container, dict):
        exact_image = getattr(docker, "inspect_container_image_id", None)
        if callable(exact_image):
            try:
                image_ref = exact_image(container_name)
            except Exception:
                image_ref = None
            if not image_ref:
                return identify_image(docker, "")
        else:
            image_ref = container.get("image")
    image_ref = image_ref or admin_image_ref_from_env()
    return identify_image(docker, image_ref) if image_ref else identify_image(docker, "")


def _running_ems_identity(docker):
    """Read the running EMS image identity for safe partial-build recovery."""

    container_name = os.environ.get("EMS_CONTAINER_NAME", DEFAULT_EMS_CONTAINER)
    try:
        container = docker.inspect_container(container_name)
    except Exception:
        container = None
    image_ref = None
    if isinstance(container, dict):
        if container.get("status") != "running":
            return identify_image(docker, "")
        exact_image = getattr(docker, "inspect_container_image_id", None)
        if callable(exact_image):
            try:
                image_ref = exact_image(container_name)
            except Exception:
                image_ref = None
            if not image_ref:
                return identify_image(docker, "")
        else:
            image_ref = container.get("image")
    return identify_image(docker, image_ref) if image_ref else identify_image(docker, "")


def _build_system_alignment(*, release_manager, admin_data_dir, docker):
    """Compose the single productive alignment service for this Admin runtime."""

    state_dir = Path(admin_data_dir) / "state"
    transition_store = PendingTransitionStore(state_dir)
    return SystemAlignmentService(
        # One verified resolution is reused across validate → Continue / Update
        # Admin Server / re-render, so the explicit verification pulls each image
        # at most once and later actions never re-pull the same build.
        resolver=CachingBuildResolver(
            SystemBuildResolver(
                docker=docker,
                development_build_source=release_manager.development_build,
            )
        ),
        transition_store=transition_store,
        embedded_resources=EmbeddedReleaseResources(release_manager=release_manager),
        release_archive_resources=ReleaseArchiveResources(
            release_manager=release_manager
        ),
        known_good_store=KnownGoodStore(state_dir),
        current_identity=lambda: _running_admin_identity(docker),
        current_ems_identity=lambda: _running_ems_identity(docker),
        persistent_ref=admin_image_ref_from_env,
        launcher=SystemTransitionLauncher(
            store=transition_store,
            docker=docker,
            release_manager=release_manager,
        ),
    )


def create_admin_runtime(
    registry=None,
    static_assets=None,
    network_detector=None,
    gateway_prober=None,
    mdns_provider=None,
    mqtt_discovery=None,
    zendure_cloud_discovery=None,
    discovery_preparation=None,
    release_manager=None,
    config_export=None,
    config_apply=None,
    deployment=None,
    ems_cli=None,
    guided_upgrade=None,
    guided_upgrade_context=None,
    system_alignment=None,
    admin_update=None,
    backup_service=None,
    setup_intents=None,
):
    """Build the shared Admin service graph once, for one or more listeners."""

    registry = registry or ScanRegistry()
    network_detector = network_detector or detect_network_suggestions
    gateway_prober = gateway_prober or probe_gateway_candidates
    # Give the release manager a read-only Docker inspector so it can compare a
    # running build's identity against release targets; harmless when the daemon
    # is absent (all inspections degrade to an unknown identity).
    docker = DockerCli()
    # Give the release manager a productive development-build catalogue source
    # (the CI-published, read-only JSON index) so installable development builds
    # appear in Setup without any test injection; a missing catalogue is empty.
    release_manager = release_manager or ReleaseManager(
        docker=docker, development_source=development_catalogue_source()
    )
    config_preview = ConfigPreviewGenerator(release_manager)
    admin_data_dir = getattr(release_manager, "data_dir", default_admin_data_dir())
    # EMS-owned credential store (config/secrets/). Persists the Zendure token and
    # local MQTT broker credentials so a later EMS runtime can read them; a legacy
    # Admin-local Zendure token is migrated in on first read.
    credential_store = CredentialStore(legacy_admin_data_dir=admin_data_dir)
    # Discovery preparation metadata (priority, enable flags, scan ranges, and the
    # endpoint-independent local MQTT credential pool refs) in the EMS config
    # area. Migrates the legacy Admin-local preparation file in on first read.
    discovery_preparation = discovery_preparation or DiscoveryConnectionsStore(
        legacy_preparation_store=DiscoveryPreparationStore(admin_data_dir)
    )
    mqtt_discovery = mqtt_discovery or MqttBrokerDiscovery(
        topic_discoverer=default_topic_discoverer,
        credential_lookup=credential_store.load_mqtt_discovery_secret,
        credential_refs_provider=(
            lambda: discovery_preparation.load()["local_mqtt"]["credential_refs"]
        ),
    )
    mdns_provider = mdns_provider or MdnsProvider(
        mqtt_handler=mqtt_discovery.add_mdns_candidate
    )
    # Admin-only Zendure cloud MQTT discovery, now backed by the EMS-owned token
    # store. Works with only the Admin container present (no EMS, no InfluxDB).
    zendure_cloud_discovery = zendure_cloud_discovery or ZendureCloudDiscovery(
        credential_store.zendure
    )
    # Seed any legacy persisted brokers so an old saved broker stays discoverable;
    # the new Discovery flow no longer creates these (credentials are a pool).
    try:
        mqtt_discovery.set_configured_brokers(
            discovery_preparation.load()["local_mqtt"]["brokers"]
        )
    except Exception:
        pass
    config_export = config_export or ConfigExportService(
        config_preview, admin_data_dir
    )
    config_apply = config_apply or ConfigApplyService(config_export, admin_data_dir)
    deployment = deployment or DeploymentService(
        release_manager, config_export, admin_data_dir=admin_data_dir
    )
    ems_cli = ems_cli or EmsCliDiagnostics()
    guided_upgrade = guided_upgrade or GuidedUpgradeExecutor(
        release_manager=release_manager, ems_cli=ems_cli, config_apply=config_apply
    )
    # Durable, secret-free Guided Upgrade context beside the transition state, so
    # an automatic resume after an Admin restart can rebuild the run.
    guided_upgrade_context = guided_upgrade_context or GuidedUpgradeContextStore(
        Path(admin_data_dir) / "state"
    )
    system_alignment = system_alignment or _build_system_alignment(
        release_manager=release_manager,
        admin_data_dir=admin_data_dir,
        docker=docker,
    )
    # Admin self-update: read-only status/plan plus an out-of-request updater. It
    # reuses the release manager's Admin data dir for the pending-state file and a
    # read-only Docker inspector for build identity; both degrade safely when the
    # daemon is absent.
    admin_update = admin_update or AdminUpdateService(
        docker=docker, release_manager=release_manager
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
        zendure_cloud_discovery=zendure_cloud_discovery,
        discovery_preparation=discovery_preparation,
        credential_store=credential_store,
        mdns_provider=mdns_provider,
        release_manager=release_manager,
        config_preview=config_preview,
        config_export=config_export,
        config_apply=config_apply,
        deployment=deployment,
        ems_cli=ems_cli,
        guided_upgrade=guided_upgrade,
        upgrade_jobs=UpgradeJobRegistry(),
        guided_upgrade_context=guided_upgrade_context,
        system_alignment=system_alignment,
        admin_update=admin_update,
        backup_service=backup_service or BackupRestoreService(),
        backup_jobs=BackupJobRegistry(),
        # Shared password, separate Admin session. Setup can be hammered but only
        # succeeds once, so a simple per-address limiter is enough there.
        auth_sessions=SessionStore(timeout_seconds=1800, absolute_max_seconds=43200),
        auth_login_limiter=LoginRateLimiter(),
        auth_setup_limiter=LoginRateLimiter(max_failures=10, window_seconds=60),
        admin_instance_id=uuid.uuid4().hex,
        setup_intents=setup_intents or SetupIntentStore(),
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
        self.zendure_cloud_discovery = runtime.zendure_cloud_discovery
        self.discovery_preparation = runtime.discovery_preparation
        self.credential_store = runtime.credential_store
        self.mdns_provider = runtime.mdns_provider
        self.release_manager = runtime.release_manager
        self.config_preview = runtime.config_preview
        self.config_export = runtime.config_export
        self.config_apply = runtime.config_apply
        self.deployment = runtime.deployment
        self.ems_cli = runtime.ems_cli
        self.guided_upgrade = runtime.guided_upgrade
        self.upgrade_jobs = runtime.upgrade_jobs
        self.guided_upgrade_context = runtime.guided_upgrade_context
        self.system_alignment = runtime.system_alignment
        self.operation_coordinator = runtime.operation_coordinator
        self.admin_update = runtime.admin_update
        self.backup_service = runtime.backup_service
        self.backup_jobs = runtime.backup_jobs
        self.auth_sessions = runtime.auth_sessions
        self.auth_login_limiter = runtime.auth_login_limiter
        self.auth_setup_limiter = runtime.auth_setup_limiter
        self.admin_instance_id = runtime.admin_instance_id
        self.test_admin_instance_id = getattr(
            runtime, "test_admin_instance_id", None
        )
        self.setup_intents = runtime.setup_intents
        self.static_assets = runtime.static_assets
        # Present only for the gated browser-test runtime; ``None`` (and its route
        # 404s) in every normal deployment.
        self.test_reset = getattr(runtime, "test_reset", None)
        self.test_seed = getattr(runtime, "test_seed", None)
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
        if path == "/api/admin/system-alignment/status":
            self._handle_system_alignment_status()
            return
        if path == "/api/admin/install-state":
            self._send_json(
                detect_install_state(
                    ems_container_probe=default_runtime_state_fingerprint
                ).as_dict()
            )
            return
        if path == "/api/admin/maintenance/overview":
            self._send_json(run_maintenance_overview())
            return
        if path == "/api/admin/maintenance/config":
            self._send_json(load_maintenance_config())
            return
        if path == "/api/admin/maintenance/zendure-mqtt/runtime-status":
            self._send_json(build_runtime_status_view())
            return
        if path == "/api/admin/maintenance/zendure-mqtt/migration-review":
            self._handle_zendure_mqtt_migration_review()
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
        if path == "/api/discovery/preparation":
            self._send_json(self.server.discovery_preparation.load())
            return
        if path == "/api/discovery/connections":
            self._send_json(self._connections_view())
            return
        if path == "/api/discovery/connections/mqtt-credentials":
            self._handle_mqtt_credentials_list()
            return
        if path == "/api/discovery/mqtt-brokers":
            self._send_json({
                "candidates": self.server.mqtt_discovery.candidates(),
            })
            return
        if path == "/api/discovery/mqtt-proposals":
            self._send_json({
                "proposals": self._public_mqtt_proposals(),
            })
            return
        if path == "/api/discovery/zendure-cloud-mqtt/settings":
            self._handle_zendure_cloud_settings()
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
        path = self._setup_discovery_write_path(path)
        if path is None:
            return
        if path == "/api/admin/auth/refresh":
            # Genuine-activity heartbeat: slide the idle timeout. It is treated as
            # a state change, so the write-auth gate above already required a valid
            # session and CSRF token.
            self.server.auth_sessions.touch(self._admin_session_cookie_value())
            self._send_json(self._auth_status_payload())
            return
        if path == "/api/admin/test/reset":
            # Behind the write-auth gate above (session + CSRF). The hook exists
            # only for the gated browser-test runtime; otherwise this 404s.
            reset = getattr(self.server, "test_reset", None)
            if not callable(reset):
                self._send_json({"error": "not found"}, status=404)
                return
            reset()
            self._send_json({"ok": True})
            return
        if path == "/api/admin/test/seed":
            # Authenticated browser-test fixture hook.  Productive runtimes do
            # not provide it, so the route remains unavailable outside the
            # explicitly gated test server.
            seed = getattr(self.server, "test_seed", None)
            if not callable(seed):
                self._send_json({"error": "not found"}, status=404)
                return
            body = self._read_json_body()
            if body is None:
                return
            if not isinstance(body, dict) or set(body) != {"scenario"}:
                self._send_json({"error": "scenario is required"}, status=400)
                return
            result = seed(body.get("scenario"))
            if not result.get("ok"):
                self._send_json(result, status=400)
                return
            self._send_json(result)
            return
        if path == "/api/admin/start-path":
            self._handle_start_path()
            return
        if path == "/api/admin/config/migrate-legacy":
            self._handle_migrate_legacy()
            return
        if path == "/api/admin/system-alignment/validate":
            self._handle_system_alignment_validate()
            return
        if path == "/api/admin/system-alignment/resume":
            self._handle_system_alignment_resume()
            return
        if path == "/api/admin/system-alignment/verify-resources":
            self._handle_system_alignment_verify_resources()
            return
        if path == "/api/admin/system-alignment/return-to-running-build":
            self._handle_system_alignment_return()
            return
        if path == "/api/admin/system-alignment/cancel":
            self._handle_system_alignment_cancel()
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
        if path == "/api/admin/maintenance/zendure-mqtt/migration-apply":
            self._handle_zendure_mqtt_migration_apply()
            return
        if path == "/api/admin/maintenance/containers/sync":
            self._handle_maintenance_container_sync()
            return
        if path == "/api/admin/maintenance/upgrade/validate":
            self._handle_maintenance_upgrade_validate()
            return
        if path == "/api/admin/maintenance/upgrade/execute":
            self._handle_maintenance_upgrade_execute()
            return
        if path == "/api/admin/maintenance/upgrade/resume":
            self._handle_maintenance_upgrade_resume()
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
            self._handle_release_prepare(mode=TRANSITION_MODE_FRESH_INSTALL)
            return
        if path == "/api/setup/automated/releases/prepare":
            self._handle_release_prepare(mode=TRANSITION_MODE_AUTOMATED_SETUP)
            return
        if path == "/api/setup/system-build/update-admin":
            self._handle_setup_update_admin()
            return
        if path == "/api/setup/system-build/confirm":
            self._handle_setup_system_build_confirm()
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
        if path == "/api/discovery/preparation":
            self._handle_discovery_preparation_save()
            return
        if path == "/api/discovery/run":
            self._handle_discovery_run()
            return
        if path == "/api/discovery/connections/local-api":
            self._handle_connections_local_api_save()
            return
        if path == "/api/discovery/connections/mqtt-credentials":
            self._handle_mqtt_credential_save()
            return
        if path == "/api/discovery/connections/mqtt-brokers":
            self._handle_connections_broker_save()
            return
        if path.startswith("/api/discovery/source/") and path.endswith("/refresh"):
            source = path[len("/api/discovery/source/"):-len("/refresh")]
            self._handle_discovery_source_refresh(source)
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
        if path == "/api/discovery/zendure-cloud-mqtt/token":
            self._handle_zendure_cloud_save_token()
            return
        if path == "/api/discovery/zendure-cloud-mqtt/test":
            self._handle_zendure_cloud_test()
            return
        if path == "/api/discovery/zendure-cloud-mqtt/refresh":
            self._handle_zendure_cloud_refresh()
            return
        self._send_json({"error": "not found"}, status=404)

    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        auth_error = self._require_admin_write_auth()
        if auth_error:
            self._send_json(auth_error[0], status=auth_error[1])
            return
        path = self._setup_discovery_write_path(path)
        if path is None:
            return
        if path == "/api/discovery/zendure-cloud-mqtt/token":
            self._handle_zendure_cloud_delete_token()
            return
        if path.startswith("/api/discovery/connections/mqtt-credentials/"):
            credential_id = path[len("/api/discovery/connections/mqtt-credentials/"):]
            self._handle_mqtt_credential_delete(credential_id)
            return
        if path.startswith("/api/discovery/connections/mqtt-brokers/"):
            broker_id = path[len("/api/discovery/connections/mqtt-brokers/"):]
            self._handle_connections_broker_delete(broker_id)
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
        instance_provider = getattr(self.server, "test_admin_instance_id", None)
        instance_id = (
            instance_provider()
            if callable(instance_provider)
            else self.server.admin_instance_id
        )
        payload = {
            "admin_instance_id": instance_id,
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
        session_id = self._admin_session_cookie_value()
        self.server.setup_intents.invalidate_session(session_id)
        self.server.auth_sessions.destroy(session_id)
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
        session_id = self._admin_session_cookie_value()
        if choice != "setup_new":
            self.server.setup_intents.invalidate_session(session_id)
        elif result.get("ok"):
            intent = self.server.setup_intents.issue(session_id=session_id)
            result["setup_intent_id"] = intent.intent_id
            result["existing_install_confirmed"] = bool(
                result.get("state") != "none" and confirm
            )
        status = 409 if result.get("requires_confirmation") else 200
        self._send_json(result, status=status)

    def _claim_setup_intent(self):
        # A setup mutation atomically consumes its intent: after this returns True
        # the same intent can never authorize a second operation, even a parallel
        # one or a retry after the mutation itself fails.
        try:
            self.server.setup_intents.claim(
                self.headers.get("X-Setup-Intent-ID"),
                session_id=self._admin_session_cookie_value(),
            )
        except SetupIntentError as exc:
            self._send_json(
                {"error": exc.reason, "message": exc.message}, status=exc.status
            )
            return False
        return True

    def _handle_migrate_legacy(self):
        if self._reject_unrelated_transition_write():
            return
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

    # --- paired System Build alignment ---------------------------------

    @staticmethod
    def _alignment_error_status(code):
        if code == "system_build_registry_rate_limited":
            return 429
        if code in {
            "acknowledgement_required",
            "unsupported_field",
            "system_build_invalid_tag",
            "system_build_dev_floating",
        }:
            return 400
        if code in {
            "transition_active",
            "transition_worker_active",
            "transition_context_mismatch",
            "operation_mismatch",
            "invalid_transition",
            "not_resumable",
            "system_build_mismatch",
            "system_build_resources_invalid",
            "system_build_alignment_required",
        }:
            return 409
        return 400

    def _send_alignment_error(self, exc, *, requested_tag=None):
        code = getattr(exc, "code", None) or getattr(exc, "reason", None)
        code = code or "system_alignment_failed"
        message = getattr(exc, "message", None) or str(exc)
        payload = {"ok": False, "error": code, "message": message}
        if requested_tag is not None:
            payload["action_state"] = terminal_system_build_action_state(
                requested_tag, code, message
            )
        if code in {"transition_active", "transition_context_mismatch"}:
            payload["transition"] = self._alignment_status().get("transition")
        self._send_json(
            payload,
            status=self._alignment_error_status(code),
        )

    def _alignment_status(self):
        """The single production status read: always worker-aware.

        Every response family that embeds a transition goes through here, so
        the server coordinator's liveness verdict can never be dropped by an
        individual route. Safe from any thread — the probe only takes the
        coordinator's own leaf lock, and nothing reachable from
        ``OperationCoordinator.abandon``'s cancel callback builds status.
        """

        try:
            payload = self.server.system_alignment.status(
                operation_active=self._operation_active
            )
        except Exception as exc:
            return {
                "ok": False,
                "active": True,
                "error": "system_alignment_status_failed",
                "message": str(exc),
                "transition": None,
                "known_good": None,
            }
        return payload if isinstance(payload, dict) else {
            "ok": False,
            "active": True,
            "transition": None,
            "known_good": None,
        }

    def _alignment_resources_verified(self):
        checker = getattr(self.server.system_alignment, "resources_verified", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        transition = (self._alignment_status().get("transition") or {})
        return transition.get("stage") in {
            "resources_verified",
            "ems_operation_pending",
            "ems_operation_running",
            "healthcheck_pending",
            "completed",
        }

    def _require_alignment_resources(self, *, allow_ems_pending=False):
        status = self._alignment_status()
        if self._alignment_resources_allowed(
            status, allow_ems_pending=allow_ems_pending
        ):
            return True
        transition = status.get("transition") or {}
        self._send_json(
            {
                "ok": False,
                "error": "system_alignment_incomplete",
                "message": "Verify the paired System Build resources before making EMS changes.",
                "transition": transition or None,
            },
            status=409,
        )
        return False

    @staticmethod
    def _alignment_resources_allowed(status, *, allow_ems_pending=False):
        transition = status.get("transition") or {}
        allowed_stages = {"resources_verified"}
        if allow_ems_pending:
            allowed_stages.add("ems_operation_pending")
        return bool(
            status.get("active")
            and transition.get("mode")
            in {TRANSITION_MODE_FRESH_INSTALL, TRANSITION_MODE_AUTOMATED_SETUP}
            and transition.get("stage") in allowed_stages
        )

    def _recover_container_conflict_transition(
        self, status, *, container_name, action
    ):
        """Resume only the exact recoverable failure this action resolves.

        A container conflict can be discovered by the asynchronous deployment
        preflight after the EMS operation has been claimed. The conflict button
        is already the operator's explicit confirmation to resolve that exact
        Docker object, so it may restore this one failure to its recorded
        ``ems_operation_pending`` retry point. No other recoverable failure is
        allowed through this path.
        """

        transition = status.get("transition") or {}
        if not (
            status.get("active")
            and transition.get("mode")
            in {TRANSITION_MODE_FRESH_INSTALL, TRANSITION_MODE_AUTOMATED_SETUP}
            and transition.get("stage") == "failed_recoverable"
            and transition.get("failed_stage") == "ems_operation_running"
            and transition.get("resume_stage") == "ems_operation_pending"
            and transition.get("error_code") == "compose_container_name_conflict"
            and isinstance(transition.get("operation_id"), str)
            and transition.get("operation_id")
        ):
            return False

        try:
            deployment_status = self.server.deployment.status()
        except Exception:
            return False
        conflict = (
            deployment_status.get("conflict")
            if isinstance(deployment_status, dict)
            else None
        )
        action_matches = bool(
            isinstance(conflict, dict)
            and conflict.get("container_name") == container_name
            and (
                (
                    action == "remove_stopped_and_continue"
                    and conflict.get("safe_fix_available") is True
                )
                or (
                    action == "replace_running_and_continue"
                    and conflict.get("replace_available") is True
                )
            )
        )
        if not action_matches:
            return False

        retry = getattr(self.server.system_alignment, "retry", None)
        if not callable(retry):
            return False
        transition_tag = transition.get("system_tag")
        stored_ack = bool(
            transition.get("development_risk_acknowledged") is True
            and transition.get("development_risk_acknowledged_for_tag")
            == transition_tag
        )
        try:
            recovered = retry(
                operation_id=transition["operation_id"],
                development_risk_acknowledged=stored_ack,
            )
        except (SystemBuildError, SystemAlignmentError) as exc:
            self._send_alignment_error(exc)
            return None
        if not isinstance(recovered, dict) or recovered.get("stage") != "ems_operation_pending":
            self._send_json(
                {
                    "ok": False,
                    "error": "container_conflict_recovery_failed",
                    "message": "The EMS operation could not be resumed for conflict resolution.",
                    "transition": self._alignment_status().get("transition"),
                },
                status=409,
            )
            return None
        return True

    def _reject_nonstartable_ems_stage(self, stage):
        if stage not in {
            "ems_operation_running",
            "healthcheck_pending",
            "completed",
            "cancelled",
        }:
            return False
        self._send_json(
            {
                "ok": False,
                "error": "system_transition_in_progress",
                "message": "This System Build operation cannot start another EMS job.",
                "transition": self._alignment_status().get("transition"),
            },
            status=409,
        )
        return True

    def _reject_unrelated_transition_write(self):
        service = self.server.system_alignment
        pending = getattr(service, "is_transition_pending", None)
        try:
            blocked = bool(callable(pending) and pending())
        except Exception:
            blocked = True
        if not blocked:
            return False
        status = self._alignment_status()
        self._send_json(
            {
                "ok": False,
                "error": "system_transition_in_progress",
                "message": "Finish or recover the active System Build transition first.",
                "transition": status.get("transition"),
            },
            status=409,
        )
        return True

    def _setup_discovery_write_path(self, path):
        """Gate every ``/api/setup/discovery/`` alias on the confirmed operation.

        Deliberately without a diagnostic-probe exemption: broker probe and
        Zendure credential test persist discovery-store state (candidates,
        status metadata), and Maintenance reaches the same handlers through the
        generic ``/api/discovery/`` routes that need only session auth + CSRF.
        """

        prefix = "/api/setup/discovery/"
        if not path.startswith(prefix):
            return path
        operation_id = (self.headers.get("X-Setup-Operation-ID") or "").strip()
        validator = getattr(
            self.server.system_alignment,
            "validate_setup_discovery_operation",
            None,
        )
        try:
            if not callable(validator):
                raise SystemAlignmentError(
                    "system_alignment_incomplete",
                    "Setup discovery operation validation is unavailable",
                )
            validator(operation_id=operation_id)
        except (SystemBuildError, SystemAlignmentError) as exc:
            code = getattr(exc, "code", "system_alignment_incomplete")
            message = getattr(exc, "message", None) or str(exc)
        except Exception:
            code = "system_alignment_incomplete"
            message = "The confirmed Setup operation could not be verified."
        else:
            return "/api/discovery/" + path[len(prefix):]

        self._send_json(
            {
                "ok": False,
                "error": code,
                "message": message,
                "return_to": "system_build",
                "return_to_step": 1,
                "transition": self._alignment_status().get("transition"),
            },
            status=409,
        )
        return None

    def _start_system_alignment(
        self, requested_tag, mode, *, development_risk_acknowledged=False
    ):
        result = self.server.system_alignment.start(
            requested_tag=requested_tag,
            mode=mode,
            development_risk_acknowledged=development_risk_acknowledged,
        )
        if not isinstance(result, dict):
            raise SystemAlignmentError(
                "system_alignment_failed", "System Build alignment returned no result"
            )
        # An already-aligned Admin still commits resource verification as its
        # own stage before any config/Compose/EMS handoff.
        if result.get("stage") == "admin_aligned":
            verifier = getattr(self.server.system_alignment, "verify_resources", None)
            if callable(verifier) and result.get("operation_id"):
                verified = verifier(operation_id=result["operation_id"])
                result = {**result, **verified, "system_build": result.get("system_build")}
        return result

    def _guided_upgrade_stored_acknowledgement(self, requested_tag):
        checker = getattr(
            self.server.system_alignment,
            "development_acknowledgement_allows_automatic_resume",
            None,
        )
        if not callable(checker):
            return False
        try:
            return bool(checker(requested_tag=requested_tag))
        except Exception:
            return False

    def _start_resolved_system_alignment(
        self, system_build, mode, *, request_fingerprint=None,
        development_risk_acknowledged=False, pre_launch=None,
    ):
        result = self.server.system_alignment.start_resolved(
            system_build=system_build,
            mode=mode,
            request_fingerprint=request_fingerprint,
            development_risk_acknowledged=development_risk_acknowledged,
            pre_launch=pre_launch,
        )
        if not isinstance(result, dict):
            raise SystemAlignmentError(
                "system_alignment_failed", "System Build alignment returned no result"
            )
        if result.get("stage") == "admin_aligned":
            verified = self.server.system_alignment.verify_resources(
                operation_id=result.get("operation_id")
            )
            result = {
                **result,
                **verified,
                "system_build": result.get("system_build"),
            }
        return result

    def _handle_system_alignment_status(self):
        self._send_json(self._alignment_status())

    def _handle_system_alignment_validate(self):
        # Read-only Fresh Setup verdict. Selecting the build in Guided Setup is the
        # explicit decision, so validation never gates on a risk acknowledgement.
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict) or set(body) - {"tag", "acknowledge_risk"}:
            self._send_json({"ok": False, "error": "unsupported_field"}, status=400)
            return
        validator = getattr(self.server.system_alignment, "validate", None)
        if not callable(validator):
            self._send_json(
                {"ok": False, "error": "system_alignment_unavailable"}, status=503
            )
            return
        try:
            result = validator(requested_tag=body.get("tag"))
        except (SystemBuildError, SystemAlignmentError) as exc:
            self._send_alignment_error(exc, requested_tag=body.get("tag"))
            return
        self._send_json(result)

    def _handle_system_alignment_resume(self):
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict) or set(body) - {
            "operation_id",
            "tag",
            "acknowledge_risk",
        }:
            self._send_json({"error": "unsupported_field"}, status=400)
            return
        operation_id = body.get("operation_id")
        try:
            transition = (self._alignment_status().get("transition") or {})
            if transition.get("operation_id") != operation_id:
                raise SystemAlignmentError(
                    "operation_mismatch", "the active System Build operation differs"
                )
            transition_tag = transition.get("system_tag")
            if body.get("tag") is not None and body.get("tag") != transition_tag:
                raise SystemAlignmentError(
                    "transition_context_mismatch",
                    "the acknowledged System Build differs from the active transition",
                )
            manual_retry = transition.get("stage") == "failed_recoverable"
            # Fresh Install recovery authorizes itself from the transition's own
            # stored, tag-bound acknowledgement — a browser-provided flag is never
            # trusted during recovery.
            stored_ack = bool(
                transition.get("development_risk_acknowledged") is True
                and transition.get("development_risk_acknowledged_for_tag")
                == transition_tag
            )
            if (
                manual_retry
                and is_development_build_tag(transition_tag)
                and not stored_ack
            ):
                self._send_json(
                    {"ok": False, "error": "acknowledgement_required"}, status=400
                )
                return
            retry = getattr(self.server.system_alignment, "retry", None)
            if manual_retry and callable(retry):
                result = retry(
                    operation_id=operation_id,
                    development_risk_acknowledged=stored_ack,
                )
            else:
                result = transition

            stage = result.get("stage")
            if stage == "admin_reconnect_pending":
                result = self.server.system_alignment.resume(operation_id=operation_id)
                stage = result.get("stage")
            if stage == "admin_aligned":
                result = self.server.system_alignment.verify_resources(
                    operation_id=operation_id
                )
                self._send_json(result)
                return
            if stage == "admin_update_pending":
                self._send_json(result)
                return

            recover = getattr(self.server.system_alignment, "recover_ems_operation", None)
            if not callable(recover):
                raise SystemAlignmentError(
                    "not_resumable", "EMS transition recovery is unavailable"
                )
            if stage == "ems_operation_running" and self._operation_active(
                operation_id
            ):
                raise SystemAlignmentError(
                    "transition_active", "the EMS deployment worker is still running"
                )
            recover_kwargs = {"healthcheck_passed": None}
            if stage == "healthcheck_pending":
                recover_kwargs = self._recover_healthcheck_kwargs(
                    transition.get("mode")
                )
            result = recover(operation_id=operation_id, **recover_kwargs)
            if (
                stage == "ems_operation_running"
                and result.get("stage") == "healthcheck_pending"
            ):
                result = recover(
                    operation_id=operation_id,
                    **self._recover_healthcheck_kwargs(transition.get("mode")),
                )
        except (SystemBuildError, SystemAlignmentError) as exc:
            self._send_alignment_error(exc)
            return
        self._send_json(result)

    def _operation_active(self, operation_id):
        """True while a mutating worker for this operation holds a live claim.

        Admin replacement runs in a detached sidecar with no local claim, so an
        expired admin_reconnect_pending orphan reports inactive (escapable).
        """

        return self.server.operation_coordinator.is_active(operation_id)

    def _recover_healthcheck_kwargs(self, mode):
        health = self._transition_healthcheck_result(mode)
        return {
            "healthcheck_passed": health.success,
            "healthcheck_error_code": health.error_code,
            "healthcheck_error_message": health.message,
        }

    def _transition_healthcheck_result(self, mode):
        if mode in {TRANSITION_MODE_FRESH_INSTALL, TRANSITION_MODE_AUTOMATED_SETUP}:
            try:
                status = self.server.deployment.status()
            except Exception:
                return HealthValidationResult(success=False)
            passed = bool(
                isinstance(status, dict)
                and status.get("running") is True
                and status.get("dashboard_reachable") is True
            )
            return HealthValidationResult(success=passed)
        try:
            diagnostics = self.server.ems_cli.run()
        except Exception:
            diagnostics = None
        # Fail closed: only an explicit, valid EMS success may pass the resume
        # health gate. Resume and normal apply share this one validator.
        return validate_system_health_result(diagnostics)

    def _handle_system_alignment_verify_resources(self):
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict) or set(body) - {"operation_id"}:
            self._send_json({"error": "unsupported_field"}, status=400)
            return
        try:
            result = self.server.system_alignment.verify_resources(
                operation_id=body.get("operation_id")
            )
        except (SystemBuildError, SystemAlignmentError) as exc:
            self._send_alignment_error(exc)
            return
        self._send_json(result)

    def _handle_system_alignment_return(self):
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict) or set(body) - {"operation_id", "confirm"}:
            self._send_json({"error": "unsupported_field"}, status=400)
            return
        if body.get("confirm") is not True:
            self._send_json({"error": "confirmation_required"}, status=400)
            return
        try:
            result = self.server.system_alignment.return_to_running_build(
                operation_id=body.get("operation_id"), confirm=True
            )
        except (SystemBuildError, SystemAlignmentError) as exc:
            self._send_alignment_error(exc)
            return
        self._send_json(result)

    def _handle_system_alignment_cancel(self):
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict) or set(body) - {"operation_id", "confirm"}:
            self._send_json({"error": "unsupported_field"}, status=400)
            return
        if body.get("confirm") is not True:
            self._send_json({"error": "confirmation_required"}, status=400)
            return
        try:
            result = self.server.system_alignment.cancel(
                operation_id=body.get("operation_id"),
                coordinator=self.server.operation_coordinator,
            )
        except SystemAlignmentError as exc:
            self._send_alignment_error(exc)
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
        draft, resolution_error = self._resolve_maintenance_mqtt_draft(draft)
        if resolution_error:
            self._send_json(
                {
                    "status": "invalid",
                    "message": resolution_error,
                    "validation": {
                        "ok": False,
                        "errors": [
                            {
                                "code": "mqtt_proposal_untrusted",
                                "message": resolution_error,
                            }
                        ],
                        "warnings": [],
                        "info": [],
                    },
                },
                status=400,
            )
            return
        self._send_json(preview_maintenance_config(draft))

    def _handle_zendure_mqtt_migration_review(self):
        # Read-only EMS-owned migration review + config fingerprint. Never mutates
        # and never returns broker secrets.
        self._send_json(load_migration_review())

    def _handle_zendure_mqtt_migration_apply(self):
        # Confirmed, EMS-owned migration apply. Auth + CSRF are already enforced by
        # the write gate; this additionally requires an explicit confirmation and a
        # matching review fingerprint, backs up by default and writes atomically —
        # a stale preview, a config that changed, or a migration whose result would
        # be invalid all leave the original config active.
        if self._reject_unrelated_transition_write():
            return
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, status=400)
            return
        if set(body) - {"confirm", "revision", "backup"}:
            self._send_json({"error": "unsupported apply field"}, status=400)
            return
        if body.get("confirm") is not True:
            self._send_json(
                {"error": "explicit migration confirmation is required"}, status=400
            )
            return
        revision = body.get("revision")
        backup = body.get("backup", True)
        if not isinstance(revision, str) or not revision:
            self._send_json(
                {"error": "a matching review revision is required"}, status=400
            )
            return
        if not isinstance(backup, bool):
            self._send_json({"error": "backup must be a boolean"}, status=400)
            return
        prepared = prepare_migration_apply(revision)
        status = prepared.get("status")
        if status == "conflict":
            self._send_json(prepared, status=409)
            return
        if status == "missing":
            self._send_json(prepared, status=404)
            return
        if status != "ok":
            self._send_json(prepared, status=400)
            return
        if not prepared.get("changed"):
            # Idempotent no-op: nothing to migrate, so nothing is written.
            self._send_json(
                {
                    "ok": True,
                    "status": "noop",
                    "changed": False,
                    "warnings": prepared["warnings"],
                }
            )
            return
        with self.server.config_apply.apply_transaction():
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
                    {
                        "ok": False,
                        "status": "error",
                        "message": f"Apply failed: {exc}",
                    },
                    status=500,
                )
                return
        result["warnings"] = prepared["warnings"]
        self._send_json(result, status=200)

    def _handle_maintenance_config_apply(self):
        if self._reject_unrelated_transition_write():
            return
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
        draft, resolution_error = self._resolve_maintenance_mqtt_draft(draft)
        if resolution_error:
            self._send_json(
                {
                    "status": "invalid",
                    "message": resolution_error,
                    "validation": {
                        "ok": False,
                        "errors": [
                            {
                                "code": "mqtt_proposal_untrusted",
                                "message": resolution_error,
                            }
                        ],
                        "warnings": [],
                        "info": [],
                    },
                },
                status=400,
            )
            return
        prepared = prepare_maintenance_config_apply(draft, revision)
        if prepared.get("status") != "ok":
            status = 409 if prepared.get("status") == "conflict" else 400
            self._send_json(prepared, status=status)
            return
        payload, status_code = self._maintenance_apply_transaction(
            prepared, revision, backup
        )
        self._send_json(payload, status=status_code)

    def _maintenance_apply_transaction(self, prepared, revision, backup):
        """Stage credentials and apply the config as one serialized unit.

        Staging happens before the config write, exactly like the setup apply:
        the EMS config must never become active referencing a credential record
        that does not resolve, and a staging failure must leave config.json
        untouched. The whole unit — staging, write, rollback — runs inside the
        shared apply transaction so a parallel Apply can neither interleave nor
        roll back over this request's result.
        """

        with self.server.config_apply.apply_transaction():
            try:
                changes = stage_runtime_credentials_for_config(
                    json.loads(prepared["payload"]),
                    credential_store=self.server.credential_store,
                    cloud_discovery=self.server.zendure_cloud_discovery,
                )
            except CredentialStoreError as exc:
                return (
                    self._attach_credential_rollback_failure(
                        self._credential_error_payload(
                            exc,
                            reason="credential_provisioning_failed",
                            status="error",
                        ),
                        getattr(exc, "rollback_failed_refs", ()),
                    ),
                    400,
                )
            try:
                result = self.server.config_apply.apply_maintenance(
                    prepared["payload"], revision, create_backup=backup
                )
            except ConfigChangedError as exc:
                return (
                    self._rollback_staged_credentials(
                        changes,
                        {"ok": False, "status": "conflict", "message": str(exc)},
                    ),
                    409,
                )
            except OSError as exc:
                return (
                    self._rollback_staged_credentials(
                        changes,
                        {
                            "ok": False,
                            "status": "error",
                            "message": f"Apply failed: {exc}",
                        },
                    ),
                    500,
                )
        result["validation"] = prepared["validation"]
        result["diff"] = prepared["diff"]
        return result, 200

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
        if self._reject_unrelated_transition_write():
            return
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

    def _handle_maintenance_upgrade_validate(self):
        """Read-only System Build validation for the Guided Upgrade selector.

        Resolves the selected tag into a verified Admin/EMS pair, reports the
        Admin alignment decision and the upgrade/downgrade direction. It never
        starts a transition, imports resources, writes config, or touches
        containers — the browser must not mark a build prepared on its own.
        """

        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict) or set(body) - {"tag", "acknowledge_risk"}:
            self._send_json({"ok": False, "error": "unsupported_field"}, status=400)
            return
        if (
            is_development_build_tag(body.get("tag"))
            and body.get("acknowledge_risk") is not True
        ):
            self._send_json(
                {"ok": False, "error": "acknowledgement_required"}, status=400
            )
            return
        validator = getattr(
            self.server.system_alignment, "validate_upgrade_target", None
        )
        if not callable(validator):
            self._send_json(
                {"ok": False, "error": "system_alignment_unavailable"}, status=503
            )
            return
        try:
            result = validator(requested_tag=body.get("tag"))
        except (SystemBuildError, SystemAlignmentError) as exc:
            self._send_alignment_error(exc)
            return
        self._send_json(result)

    def _handle_maintenance_upgrade_execute(self):
        # Confirmed mutation: bump the EMS image and force-recreate only the EMS
        # service. The target is resolved from the prepared release, not the body.
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, status=400)
            return
        if set(body) - {
            "confirm",
            "target_release",
            "options",
            "acknowledge_risk",
            "migration_revision",
            "selection_fingerprint",
        }:
            self._send_json({"error": "unsupported upgrade field"}, status=400)
            return
        options = body.get("options", {})
        if not isinstance(options, dict):
            self._send_json({"error": "options must be a JSON object"}, status=400)
            return
        if body.get("confirm") is not True:
            self._send_json(
                {
                    "ok": False,
                    "status": "rejected",
                    "reason": "confirm_required",
                    "message": "Explicit upgrade confirmation is required.",
                },
                status=400,
            )
            return
        target_release = body.get("target_release")
        # The System Build the operator verified is the only one that may run. A
        # mutable tag can re-resolve to a different pair between Verify and
        # Upgrade, so the verified selection fingerprint is required and compared
        # against the freshly resolved pair BEFORE any preflight or mutation.
        submitted_fingerprint = body.get("selection_fingerprint")
        if not isinstance(submitted_fingerprint, str) or not submitted_fingerprint:
            self._send_json(
                {
                    "ok": False,
                    "status": "conflict",
                    "reason": "system_build_verification_required",
                    "message": "Verify the selected System Build before upgrading.",
                },
                status=_UPGRADE_STATUS_CODES["system_build_verification_required"],
            )
            return
        executor = self.server.guided_upgrade
        try:
            system_build = self.server.system_alignment.resolve(target_release)
        except (SystemBuildError, SystemAlignmentError) as exc:
            self._send_alignment_error(exc)
            return
        authoritative_fingerprint = self.server.system_alignment.selection_fingerprint(
            system_build
        )
        if submitted_fingerprint != authoritative_fingerprint:
            self._send_json(
                {
                    "ok": False,
                    "status": "conflict",
                    "reason": "system_build_verification_stale",
                    "message": (
                        "The selected System Build changed after verification. "
                        "Verify it again before upgrading."
                    ),
                },
                status=_UPGRADE_STATUS_CODES["system_build_verification_stale"],
            )
            return
        try:
            rejection, run_context = executor.preflight(
                target_release,
                options,
                confirm=True,
                system_build=system_build,
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
        migration = run_context.migration
        if migration.get("required") and body.get("migration_revision") != migration.get(
            "revision"
        ):
            self._send_json(
                {
                    "ok": False,
                    "status": "conflict",
                    "reason": "mqtt_migration_review_stale",
                    "message": (
                        "The Zendure MQTT migration changed after planning. "
                        "Review and confirm the current upgrade plan."
                    ),
                    "migration": migration.get("review"),
                    "migration_revision": migration.get("revision"),
                },
                status=409,
            )
            return

        fingerprint_fn = getattr(executor, "request_fingerprint", None)
        if not callable(fingerprint_fn):
            self._send_json(
                {
                    "ok": False,
                    "status": "error",
                    "message": "Guided Upgrade request binding is unavailable.",
                },
                status=500,
            )
            return
        request_fingerprint = fingerprint_fn(target_release, options)
        status = self._alignment_status()
        transition = status.get("transition") or {}
        resuming = bool(status.get("active"))
        if resuming and (
            transition.get("mode") != TRANSITION_MODE_GUIDED_UPGRADE
            or transition.get("system_tag") != target_release
            or transition.get("request_fingerprint") != request_fingerprint
        ):
            self._send_alignment_error(
                SystemAlignmentError(
                    "transition_context_mismatch",
                    "the resumed operation differs from the prepared Guided Upgrade",
                )
            )
            return
        # A failed transition must be recovered explicitly (its own route),
        # never restarted from the execute button.
        if resuming and transition.get("stage") == "failed_recoverable":
            self._send_json(
                {
                    "ok": False,
                    "error": "system_transition_in_progress",
                    "message": "Recover the failed System Build transition before retrying.",
                    "transition": transition,
                },
                status=409,
            )
            return

        # A development System Build requires an explicit, tag-bound risk
        # acknowledgement before a NEW transition. The expected reconnect of the
        # same transition reuses the stored, tag-bound acknowledgement.
        development_risk_acknowledged = body.get("acknowledge_risk") is True
        if is_development_build_tag(target_release) and not development_risk_acknowledged:
            if resuming and self._guided_upgrade_stored_acknowledgement(target_release):
                development_risk_acknowledged = True
            else:
                self._send_json(
                    {"ok": False, "error": "acknowledgement_required"}, status=400
                )
                return

        try:
            if resuming:
                pre_alignment = executor.resume_alignment(run_context)
            else:
                failure, pre_alignment = executor.prepare_alignment(run_context)
                if failure is not None:
                    self._send_json(failure, status=409)
                    return
            # Persist the durable, secret-free execution context BEFORE the Admin
            # replacement is launched, so an automatic resume after the Admin
            # restarts can rebuild the run without the browser resending options.
            # A persistence failure aborts before any transition is committed.
            def _persist_before_launch(record):
                self._persist_guided_upgrade_context(
                    record.operation_id,
                    target_release,
                    run_context.options,
                    request_fingerprint,
                    pre_alignment,
                )

            alignment = self._start_resolved_system_alignment(
                system_build,
                TRANSITION_MODE_GUIDED_UPGRADE,
                request_fingerprint=request_fingerprint,
                development_risk_acknowledged=development_risk_acknowledged,
                pre_launch=_persist_before_launch,
            )
        except GuidedUpgradeContextPersistenceError:
            self._send_json(
                {
                    "ok": False,
                    "error": "guided_upgrade_context_persistence_failed",
                    "message": (
                        "The Guided Upgrade state could not be saved. No Admin "
                        "update was started."
                    ),
                },
                status=500,
            )
            return
        except (SystemBuildError, SystemAlignmentError) as exc:
            self._send_alignment_error(exc)
            return
        except Exception:  # preparation failed before a durable transition exists
            self._send_json(
                {"ok": False, "status": "error", "message": "Upgrade failed unexpectedly."},
                status=500,
            )
            return
        if alignment.get("stage") in {
            "admin_update_pending",
            "admin_reconnect_pending",
        }:
            self._send_json(alignment, status=202)
            return
        alignment_stage = alignment.get("stage") or (
            self._alignment_status().get("transition") or {}
        ).get("stage")
        if alignment_stage == "failed_recoverable":
            self._send_json(
                {
                    "ok": False,
                    "error": "system_transition_in_progress",
                    "message": "Recover the failed System Build transition before retrying.",
                    "transition": self._alignment_status().get("transition"),
                },
                status=409,
            )
            return
        if self._reject_nonstartable_ems_stage(alignment_stage):
            return
        operation_id = alignment.get("operation_id")
        try:
            if alignment_stage == "resources_verified":
                self.server.system_alignment.begin_ems_operation(
                    operation_id=operation_id
                )
        except SystemAlignmentError as exc:
            self._send_alignment_error(exc)
            return
        self._start_guided_upgrade_job(
            operation_id, executor, run_context, pre_alignment
        )

    def _start_guided_upgrade_job(
        self, operation_id, executor, run_context, pre_alignment
    ):
        """Submit (or re-attach to) the single EMS upgrade job for this operation.

        Keyed by ``operation_id`` so several execute/resume requests can never
        start a second job: a repeat returns the existing job's live snapshot.
        """

        job = UpgradeJob(uuid.uuid4().hex, plan_upgrade_steps(run_context.options))
        job, _created = self.server.upgrade_jobs.get_or_submit(
            operation_id,
            job,
            lambda handle: handle.finish(
                self._run_guided_upgrade_alignment(
                    executor, run_context, pre_alignment, handle, operation_id
                )
            ),
            coordinator=self.server.operation_coordinator,
        )
        if job is None:
            # Abandonment won the claim race: never start a worker for a
            # cancelled transition.
            self._send_json(
                {
                    "ok": False,
                    "error": "system_transition_in_progress",
                    "message": (
                        "The System Build transition was abandoned before the "
                        "upgrade could start."
                    ),
                    "transition": self._alignment_status().get("transition"),
                },
                status=409,
            )
            return
        snapshot = job.snapshot()
        transition = self._alignment_status().get("transition")
        self._send_json(
            {
                "ok": True,
                "job_id": job.job_id,
                "status": snapshot["status"],
                "steps": snapshot["steps"],
                # Keep System Build progress tied to the persisted transition;
                # the generic job status is not a workflow stage.
                "transition": transition,
            },
            status=202,
        )

    def _handle_maintenance_upgrade_resume(self):
        """Continue a Guided Upgrade automatically after the Admin reconnects.

        Takes only ``operation_id``. The server advances the reconnected Admin
        (identity check, resource import), reloads the durable execution context
        (options + completed backup), rebuilds the run context without repeating
        the backup or preflight, and starts the single EMS upgrade job. The
        browser never resends the target, options, or a plan.
        """

        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict) or set(body) - {"operation_id"}:
            self._send_json({"ok": False, "error": "unsupported_field"}, status=400)
            return
        operation_id = body.get("operation_id")
        if not operation_id or not isinstance(operation_id, str):
            self._send_json(
                {"ok": False, "error": "operation_id_required"}, status=400
            )
            return
        transition = self._alignment_status().get("transition") or {}
        if transition.get("mode") != TRANSITION_MODE_GUIDED_UPGRADE:
            self._send_json(
                {"ok": False, "error": "no_guided_upgrade_transition"}, status=409
            )
            return
        if transition.get("operation_id") != operation_id:
            self._send_alignment_error(
                SystemAlignmentError(
                    "operation_mismatch",
                    "the active System Build operation does not match",
                )
            )
            return
        target_release = transition.get("system_tag")
        try:
            stage = self._advance_guided_upgrade_reconnect(operation_id, transition)
        except (SystemBuildError, SystemAlignmentError) as exc:
            self._send_alignment_error(exc)
            return
        # Still waiting for the replacement Admin: report and let the poller wait.
        if stage in {"admin_update_pending", "admin_reconnect_pending"}:
            self._send_json(
                {
                    "ok": True,
                    "status": stage,
                    "stage": stage,
                    "operation_id": operation_id,
                    "reconnect": True,
                },
                status=202,
            )
            return
        if stage == "failed_recoverable":
            self._send_json(
                {
                    "ok": False,
                    "error": "system_transition_in_progress",
                    "message": "Recover the failed System Build transition first.",
                    "transition": self._alignment_status().get("transition"),
                },
                status=409,
            )
            return
        context = None
        store = getattr(self.server, "guided_upgrade_context", None)
        if store is not None:
            context = store.load(
                operation_id=operation_id, target_system_tag=target_release
            )
        if context is None:
            self._send_json(
                {
                    "ok": False,
                    "error": "guided_upgrade_context_unavailable",
                    "message": "The Guided Upgrade context could not be restored.",
                },
                status=409,
            )
            return
        executor = self.server.guided_upgrade
        try:
            # Reconstruct the verified pair from the durable transition, never a
            # fresh resolve: the replacement Admin's resolver cache is empty, so
            # re-resolving the tag could pick up a moved digest. The transition
            # pins the exact pair verified before the Admin was replaced.
            system_build = self.server.system_alignment.transition_build(
                operation_id=operation_id
            )
            rejection, run_context = executor.preflight(
                target_release, context.options, confirm=True, system_build=system_build
            )
        except (SystemBuildError, SystemAlignmentError) as exc:
            self._send_alignment_error(exc)
            return
        except Exception:
            self._send_json(
                {"ok": False, "status": "error", "message": "Upgrade failed unexpectedly."},
                status=500,
            )
            return
        if rejection is not None:
            self._send_json(
                rejection,
                status=_UPGRADE_STATUS_CODES.get(rejection.get("reason"), 409),
            )
            return
        # Replay the completed pre-alignment work (verify/preflight/backup) without
        # repeating it, consuming the durable backup state (exact verified
        # archive) rather than re-asserting it from the enabled option.
        pre_alignment = executor.resume_alignment(
            run_context,
            backup={
                "completed": context.backup_completed,
                "verified": context.backup_verified,
                "reference": context.backup_reference,
            },
            migration={
                "required": context.mqtt_migration_required,
                "completed": context.mqtt_migration_completed,
                "revision": context.mqtt_migration_revision,
            },
        )
        try:
            if stage == "resources_verified":
                self.server.system_alignment.begin_ems_operation(
                    operation_id=operation_id
                )
        except SystemAlignmentError as exc:
            self._send_alignment_error(exc)
            return
        self._start_guided_upgrade_job(
            operation_id, executor, run_context, pre_alignment
        )

    def _advance_guided_upgrade_reconnect(self, operation_id, transition):
        """Advance a reconnected Admin to ``resources_verified`` when possible."""

        stage = transition.get("stage")
        if stage == "admin_reconnect_pending":
            result = self.server.system_alignment.resume(operation_id=operation_id)
            stage = result.get("stage")
        if stage == "admin_aligned":
            result = self.server.system_alignment.verify_resources(
                operation_id=operation_id
            )
            stage = result.get("stage")
        return stage

    @staticmethod
    def _guided_backup_status(pre_alignment):
        """Return ``(completed, archive_reference, verified)`` for the backup step.

        ``archive_reference`` is the exact created archive path (never the
        execution-context string), and ``verified`` reflects EMS Core's real
        manifest/checksum verification — not merely a zero exit code.
        """

        for step in getattr(pre_alignment, "steps", ()) or ():
            if isinstance(step, dict) and step.get("id") == "backup":
                return (
                    step.get("status") == "ok",
                    step.get("archive"),
                    bool(step.get("verified")),
                )
        return False, None, False

    @staticmethod
    def _guided_migration_status(pre_alignment):
        """Return the reviewed/completed migration state for durable resume."""

        required = False
        revision = None
        completed = False
        for step in getattr(pre_alignment, "steps", ()) or ():
            if not isinstance(step, dict):
                continue
            if step.get("id") == "migration_review":
                required = bool(step.get("required"))
                revision = step.get("revision")
            elif step.get("id") == "mqtt_migration":
                completed = step.get("status") == "ok"
        return required, completed if required else False, revision

    def _persist_guided_upgrade_context(
        self, operation_id, target_release, options, request_fingerprint, pre_alignment
    ):
        """Durably save the Guided Upgrade execution context, or fail closed.

        Raises :class:`GuidedUpgradeContextPersistenceError` when the context
        cannot be made durable, so the caller can abort before an Admin
        replacement launches — the resume path depends on this context.
        """

        store = getattr(self.server, "guided_upgrade_context", None)
        if store is None or not operation_id:
            raise GuidedUpgradeContextPersistenceError(
                "no guided upgrade context store is available"
            )
        backup_completed, backup_reference, backup_verified = (
            self._guided_backup_status(pre_alignment)
        )
        migration_required, migration_completed, migration_revision = (
            self._guided_migration_status(pre_alignment)
        )
        try:
            store.save(
                operation_id=operation_id,
                target_system_tag=target_release,
                options=options,
                request_fingerprint=request_fingerprint,
                pre_alignment_completed=True,
                backup_completed=backup_completed,
                backup_reference=backup_reference,
                backup_verified=backup_verified,
                mqtt_migration_required=migration_required,
                mqtt_migration_completed=migration_completed,
                mqtt_migration_revision=migration_revision,
            )
        except Exception as exc:
            raise GuidedUpgradeContextPersistenceError(str(exc)) from exc

    def _run_guided_upgrade_alignment(
        self, executor, run_context, pre_alignment, progress, operation_id
    ):
        if not self.server.system_alignment.claim_ems_operation(
            operation_id=operation_id
        ):
            return {
                "ok": False,
                "status": "rejected",
                "reason": "ems_operation_already_claimed",
                "message": "This EMS operation is already running.",
                "steps": [],
                "warnings": [],
            }
        try:
            result = executor.run(
                run_context,
                pre_alignment=pre_alignment,
                progress=progress,
            )
            if not isinstance(result, dict):
                raise TypeError("Guided Upgrade returned no result")
        except Exception:
            self.server.system_alignment.finish_ems_operation(
                operation_id=operation_id,
                succeeded=False,
                error_code="ems_upgrade_unexpected_failure",
                error_message="Guided EMS upgrade failed unexpectedly.",
            )
            raise
        if not result.get("ok"):
            reason = result.get("reason")
            error_code = (
                reason if reason in _TRUSTED_UPGRADE_FAILURE_CODES else "ems_upgrade_failed"
            )
            self.server.system_alignment.finish_ems_operation(
                operation_id=operation_id,
                succeeded=False,
                error_code=error_code,
                error_message=result.get("message") or "Guided EMS upgrade failed.",
            )
            return result
        self.server.system_alignment.finish_ems_operation(
            operation_id=operation_id, succeeded=True
        )
        diagnostics = result.get("diagnostics")
        if not isinstance(diagnostics, dict):
            try:
                diagnostics = self.server.ems_cli.run()
            except Exception:
                diagnostics = None
        # Fail closed: an empty, unknown or malformed diagnosis must never mark
        # the build known-good. This is the same validator the resume path uses.
        health = validate_system_health_result(diagnostics)
        healthy = health.success
        completion = self.server.system_alignment.finish_healthcheck(
            operation_id=operation_id,
            passed=healthy,
            error_code=None if healthy else (health.error_code or "healthcheck_failed"),
            error_message=(
                None if healthy else (health.message or "EMS health checks did not pass.")
            ),
        )
        completion_stage = completion.get("stage") or completion.get("status")
        if not healthy or completion_stage != "completed":
            transition = (self._alignment_status().get("transition") or {})
            result = {
                **result,
                "ok": False,
                "status": "failed",
                "reason": transition.get("error_code") or "healthcheck_failed",
                "message": transition.get("error_message")
                or "EMS health checks did not pass.",
            }
        return result

    def _send_upgrade_job(self, job_id):
        job = self.server.upgrade_jobs.get(job_id.strip("/"))
        if job is None:
            self._send_json({"ok": False, "error": "unknown job_id"}, status=404)
            return
        self._send_json(
            {
                "ok": True,
                **job,
                "transition": self._alignment_status().get("transition"),
            }
        )

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
        if self._reject_unrelated_transition_write():
            return
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
        if self._reject_unrelated_transition_write():
            return
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
        if self._reject_unrelated_transition_write():
            return
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
        if self._reject_unrelated_transition_write():
            return
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

    def _handle_release_prepare(self, *, mode):
        """Verify the embedded resources for an already-aligned Admin.

        This prepares EMS resources only. It never updates, retags or recreates
        the Admin container: aligning the Admin is the explicit ``update-admin``
        action. An Admin that is not yet the selected build is refused with
        ``system_build_alignment_required`` (return the user to Step 1).
        """

        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, status=400)
            return
        if set(body) - {"tag", "acknowledge_risk"}:
            self._send_json({"error": "unsupported_field"}, status=400)
            return
        if not self._claim_setup_intent():
            return
        preparer = getattr(self.server.system_alignment, "prepare_setup_resources", None)
        if not callable(preparer):
            self._send_json(
                {"ok": False, "error": "system_alignment_unavailable"}, status=503
            )
            return
        try:
            result = preparer(
                requested_tag=body.get("tag"),
                mode=mode,
                # Guided Setup authorizes the server-validated build behind a valid
                # setup intent; the selection is the explicit decision.
                development_risk_acknowledged=True,
            )
        except (SystemBuildError, SystemAlignmentError) as exc:
            self._send_alignment_error(exc, requested_tag=body.get("tag"))
            return
        build = result.get("system_build") or {}
        result.setdefault("tag", build.get("canonical_tag") or body.get("tag"))
        result.setdefault("resources", {})
        result.setdefault("warnings", [])
        if self._alignment_resources_verified():
            result["status"] = "ready_for_ems"
            self._send_json(result)
            return
        status = self._alignment_status()
        self._send_json(
            {
                "ok": False,
                "error": "system_alignment_incomplete",
                "message": "System Build resources have not been verified.",
                "transition": status.get("transition"),
            },
            status=409,
        )

    def _handle_setup_update_admin(self):
        """Align the Admin to the selected System Build from Guided Setup Step 1.

        Revalidates the pair, starts the shared ``SystemAlignmentService`` (the v2
        transition) and returns reconnect information. It never prepares a legacy
        Admin-update plan, writes Setup config, or deploys EMS.
        """

        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict) or set(body) - {"tag", "acknowledge_risk"}:
            self._send_json({"ok": False, "error": "unsupported_field"}, status=400)
            return
        if not self._claim_setup_intent():
            return
        try:
            result = self._start_system_alignment(
                body.get("tag"),
                TRANSITION_MODE_FRESH_INSTALL,
                # A valid setup intent from the authenticated Fresh Setup workflow
                # authorizes the server-validated build itself; the build choice is
                # the explicit decision (no browser risk flag is trusted).
                development_risk_acknowledged=True,
            )
        except (SystemBuildError, SystemAlignmentError) as exc:
            self._send_alignment_error(exc, requested_tag=body.get("tag"))
            return
        if result.get("status") == "admin_alignment_started" or result.get("reconnect"):
            self._send_json(result, status=202)
            return
        self._send_json(result)

    def _handle_setup_system_build_confirm(self):
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict) or set(body) - {"tag", "acknowledge_risk"}:
            self._send_json({"ok": False, "error": "unsupported_field"}, status=400)
            return
        tag = body.get("tag")
        if not self._claim_setup_intent():
            return
        confirmer = getattr(self.server.system_alignment, "confirm_setup_build", None)
        if not callable(confirmer):
            self._send_json(
                {"ok": False, "error": "system_alignment_unavailable"}, status=503
            )
            return
        try:
            result = confirmer(
                requested_tag=tag,
                mode=TRANSITION_MODE_FRESH_INSTALL,
                # Fresh Setup authorizes the server-validated build behind a valid
                # setup intent; the selection is the explicit decision.
                development_risk_acknowledged=True,
            )
        except (SystemBuildError, SystemAlignmentError) as exc:
            self._send_alignment_error(exc, requested_tag=tag)
            return
        if not isinstance(result, dict) or result.get("resources_verified") is not True:
            self._send_json(
                {
                    "ok": False,
                    "error": "system_alignment_incomplete",
                    "message": "System Build resources have not been verified.",
                    "transition": self._alignment_status().get("transition"),
                },
                status=409,
            )
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
        proposals, error = self._preview_mqtt_proposals(body)
        if error is not None:
            self._send_json({"error": error}, status=400)
            return
        broker, manual_devices, error = self._preview_manual_mqtt(body)
        if error is not None:
            self._send_json({"error": error}, status=400)
            return
        preview = self.server.config_preview.generate(
            draft, count, features, proposals, broker, manual_devices
        )
        # Read-only preview surfaces the global MQTT credentials_ref contract
        # early (canonical refs, single-source ownership) so Setup Preview shows
        # a bad reference before an apply is attempted. The apply defers to the
        # shared credential-staging layer, which rejects the same references with
        # a stable structured code and a byte-exact rollback.
        _surface_mqtt_credential_consumer_issues(preview)
        self._send_json(redact_config_for_browser(copy.deepcopy(preview)))

    @staticmethod
    def _preview_manual_mqtt(body):
        """Validate the optional manual Zendure MQTT broker/device fields.

        Returns ``(broker, manual_devices, None)`` on success or
        ``(None, None, message)`` on a malformed field. Per-entry mapping and
        capability validation happen later in the preview generator.
        """

        broker = body.get("zendure_mqtt_broker")
        if broker is not None and not isinstance(broker, dict):
            return None, None, "zendure_mqtt_broker must be a JSON object"
        manual_devices = body.get("zendure_mqtt_manual_devices")
        if manual_devices is None:
            return broker, None, None
        if not isinstance(manual_devices, list):
            return None, None, "zendure_mqtt_manual_devices must be a JSON array"
        if len(manual_devices) > MAX_ZENDURE_MQTT_PREVIEW_PROPOSALS:
            return None, None, "too many zendure_mqtt_manual_devices"
        if not all(isinstance(entry, dict) for entry in manual_devices):
            return None, None, "zendure_mqtt_manual_devices entries must be objects"
        return broker, manual_devices, None

    def _preview_mqtt_proposals(self, body):
        """Validate and resolve the optional ``zendure_mqtt_proposals`` field.

        Returns ``(proposals, None)`` on success or ``(None, message)`` on a
        malformed or untrusted field. Shared by preview and
        download/write/apply so the same shape checks gate every path.

        The browser is not authoritative for proposal content: each submitted
        selection carries only a proposal ``id`` (plus its ``broker_ref``) that
        is resolved back to the full proposal held in current discovery state.
        Trusted identity, broker and capability values come from stored state;
        an unknown, stale or forged selection is rejected here, before any config
        is generated.
        """

        proposals = body.get("zendure_mqtt_proposals")
        if proposals is None:
            return None, None
        if not isinstance(proposals, list):
            return None, "zendure_mqtt_proposals must be a JSON array"
        if len(proposals) > MAX_ZENDURE_MQTT_PREVIEW_PROPOSALS:
            return None, "too many zendure_mqtt_proposals"
        if not all(isinstance(entry, dict) for entry in proposals):
            return None, "zendure_mqtt_proposals entries must be objects"

        resolved, errors = zendure_mqtt_config_proposals.resolve_selected_proposals(
            proposals, self._trusted_mqtt_proposals()
        )
        if errors:
            return None, errors[0]["message"]
        return resolved, None

    def _trusted_mqtt_proposals(self):
        """The one trusted proposal set: local broker devices + cloud candidates.

        Used only behind server-side selection resolution. Read-only: it never
        triggers a broker or cloud refresh. Cloud candidates include their full
        write-target identity here, while the public endpoint is redacted by
        :meth:`_public_mqtt_proposals`.
        """

        trusted_candidates = getattr(
            self.server.zendure_cloud_discovery, "trusted_candidates", None
        )
        cloud_candidates = (
            trusted_candidates()
            if callable(trusted_candidates)
            else self.server.zendure_cloud_discovery.candidates()
        )
        return zendure_mqtt_config_proposals.proposals_from_sources(
            self.server.mqtt_discovery.candidates(),
            cloud_candidates,
        )

    def _public_mqtt_proposals(self):
        """Browser-safe proposal view with raw write identities removed."""

        proposals = copy.deepcopy(self._trusted_mqtt_proposals())
        for proposal in proposals:
            if not isinstance(proposal, dict):
                continue
            proposal["product_key"] = None
            fragment = proposal.get("config_fragment")
            mqtt = fragment.get("mqtt") if isinstance(fragment, dict) else None
            is_cloud = (
                proposal.get("connection_source") == SOURCE_ZENDURE_CLOUD_MQTT
                or (
                    isinstance(mqtt, dict)
                    and mqtt.get("source") == SOURCE_ZENDURE_CLOUD_MQTT
                )
            )
            if is_cloud:
                # The trusted device id is the observed MQTT routing key. Keep
                # it server-side and expose the physical serial as the stable
                # browser identity instead.
                serial = proposal.get("serial_number")
                proposal["device_id"] = (
                    serial.strip()
                    if isinstance(serial, str) and serial.strip()
                    else None
                )
            if isinstance(mqtt, dict):
                mqtt.pop("product_key", None)
                if is_cloud:
                    mqtt.pop("device_id", None)
        return proposals

    def _resolve_maintenance_mqtt_draft(self, draft):
        """Inject trusted write targets for proposal-backed draft devices.

        Maintenance keeps an opaque proposal id in the browser draft. Before
        preview/apply, that id is resolved against current in-memory discovery
        and only then are the raw product key and MQTT routing id restored. A
        stale id or changed proposal identity fails closed; raw keys never cross
        the HTTP boundary.
        """

        resolved_draft = copy.deepcopy(draft)
        devices = resolved_draft.get("devices")
        if not isinstance(devices, list):
            return resolved_draft, None
        trusted = self._trusted_mqtt_proposals()
        for item in devices:
            if not isinstance(item, dict):
                continue
            proposal_id = item.get("proposal_id")
            if not isinstance(proposal_id, str) or not proposal_id.strip():
                continue
            item_mqtt = item.get("mqtt") if isinstance(item.get("mqtt"), dict) else {}
            broker_ref = item.get("proposal_broker_ref") or item_mqtt.get("broker_ref")
            selected, errors = zendure_mqtt_config_proposals.resolve_selected_proposals(
                [{"id": proposal_id.strip(), "broker_ref": broker_ref}], trusted
            )
            if errors or not selected:
                return None, (
                    errors[0]["message"]
                    if errors
                    else "The selected MQTT proposal is no longer available; refresh discovery."
                )
            proposal = selected[0]
            fragment = proposal.get("config_fragment")
            trusted_mqtt = fragment.get("mqtt") if isinstance(fragment, dict) else None
            if not isinstance(trusted_mqtt, dict):
                return None, "The selected MQTT proposal has no usable device configuration."

            trusted_serial = str(fragment.get("serial_number") or "").strip()
            submitted_serial = str(item.get("serial_number") or "").strip()
            trusted_device_id = str(trusted_mqtt.get("device_id") or "").strip()
            submitted_device_id = str(
                item_mqtt.get("device_id") or item.get("device_id") or ""
            ).strip()
            if trusted_serial and submitted_serial and trusted_serial != submitted_serial:
                return None, "The selected MQTT proposal identity changed; refresh discovery."
            if trusted_device_id and submitted_device_id:
                accepted_device_ids = {trusted_device_id}
                if trusted_serial:
                    accepted_device_ids.add(trusted_serial)
                if (
                    not zendure_mqtt_config_proposals.is_masked_zendure_identifier(
                        submitted_device_id
                    )
                    and submitted_device_id not in accepted_device_ids
                ):
                    return None, "The selected MQTT proposal identity changed; refresh discovery."

            if trusted_device_id:
                item["device_id"] = trusted_device_id
                item.setdefault("mqtt", {})["device_id"] = trusted_device_id

            product_key = trusted_mqtt.get("product_key")
            if isinstance(product_key, str) and product_key.strip():
                item["product_key"] = product_key.strip()
                item.setdefault("mqtt", {})["product_key"] = product_key.strip()

            # The broker endpoint is proposal-owned, not browser-owned.
            item["broker"] = {
                "ref": proposal.get("broker_ref") or trusted_mqtt.get("broker_ref") or "",
                "host": proposal.get("broker_host") or "",
                "port": proposal.get("broker_port"),
                "tls": proposal.get("broker_tls") is True,
                "tls_insecure": proposal.get("broker_tls_insecure") is True,
                "tls_mode": proposal.get("broker_tls_mode") or "",
                "credentials_ref": proposal.get("credentials_ref") or "",
                "source": proposal.get("connection_source") or proposal.get("source") or "",
            }
        return resolved_draft, None

    def _stage_setup_credentials(self, config, broker, changes):
        """Stage every runtime credential a setup write/apply depends on.

        Delegates to the shared provisioning module, driven by the generated
        target config exactly like the Maintenance apply. ``changes`` receives
        exactly the records the caller still has to roll back: the manual
        broker change immediately, the config-driven changes only once their
        staging succeeded (a config-staging failure rolls its own changes back
        internally before raising). Raises ``CredentialStoreError`` (before
        any config change) on a missing source credential, an unusable record
        without a trusted replacement or a failed cloud provisioning.
        """

        stage_setup_runtime_credentials(
            config,
            broker,
            changes,
            credential_store=self.server.credential_store,
            cloud_discovery=self.server.zendure_cloud_discovery,
        )

    def _rollback_staged_credentials(self, changes, payload, error=None):
        """Best-effort rollback of staged credential changes onto a response.

        A rollback failure must never mask the original apply error, but it
        must also never be silent: the response gains an explicit high-severity
        ``credential_rollback`` section naming the affected refs (safe metadata
        only, never secret values) so the operator knows manual cleanup is
        required and the apply is not reported as clean. ``error`` may carry
        ``rollback_failed_refs`` from a rollback that already failed inside the
        provisioning helper; those refs are surfaced the same way.
        """

        failed = self.server.credential_store.rollback_credential_changes(changes)
        inherited = tuple(getattr(error, "rollback_failed_refs", ()) or ())
        return self._attach_credential_rollback_failure(
            payload, [*inherited, *(ref for ref in failed if ref not in inherited)]
        )

    def _credential_error_payload(self, exc, *, reason, status=None):
        """Build the base credential-failure response for a staging error.

        ``code`` is the exception's own stable code when it carries one
        (invalid credentials_ref, source conflict) and otherwise the shared
        ``credential_provisioning_failed`` code. Any non-secret ``credentials_ref``
        and conflicting ``sources`` the exception names are echoed so the
        operator (and the Setup/Maintenance UIs) can identify the reference,
        never a secret value.
        """

        payload = {
            "ok": False,
            "code": getattr(exc, "code", None) or "credential_provisioning_failed",
            "reason": reason,
            "message": str(exc),
        }
        if status is not None:
            payload["status"] = status
        ref = getattr(exc, "credentials_ref", None)
        if isinstance(ref, str) and ref:
            payload["credentials_ref"] = ref
        sources = getattr(exc, "sources", None)
        if sources:
            payload["sources"] = [str(source) for source in sources]
        consumers = getattr(exc, "consumers", None)
        if consumers:
            payload["consumers"] = [str(consumer) for consumer in consumers]
        return payload

    def _attach_credential_rollback_failure(self, payload, failed_refs):
        failed = [str(ref) for ref in failed_refs or ()]
        if failed:
            payload["credential_rollback"] = {
                "severity": "high",
                "failed_refs": failed,
                "message": (
                    "Rolling back staged MQTT credential changes failed for "
                    "these references. Inspect config/secrets and restore or "
                    "remove the records manually before retrying."
                ),
            }
        return payload

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
        # Zendure MQTT proposals share the preview sanitize/validate
        # path; the same field-shape checks gate download/write/apply.
        proposals, error = self._preview_mqtt_proposals(body)
        if error is not None:
            self._send_json({"error": error}, status=400)
            return None
        broker, manual_devices, error = self._preview_manual_mqtt(body)
        if error is not None:
            self._send_json({"error": error}, status=400)
            return None
        return draft, count, overwrite, features, proposals, broker, manual_devices

    def _handle_config_download(self):
        request = self._config_export_request()
        if request is None:
            return
        draft, count, _overwrite, features, proposals, broker, manual_devices = request
        try:
            payload, _preview = self.server.config_export.serialize(
                draft, count, features, proposals, broker, manual_devices
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
        if not self._require_alignment_resources():
            return
        request = self._config_export_request()
        if request is None:
            return
        draft, count, overwrite, features, proposals, broker, manual_devices = request
        payload, status_code = self._setup_config_transaction(
            draft,
            count,
            features,
            proposals,
            broker,
            manual_devices,
            lambda change: self.server.config_export.write(
                draft,
                count,
                overwrite,
                features,
                proposals,
                broker,
                manual_devices,
                prepared=change,
            ),
            failure_reason="write_failed",
            failure_message="Could not save generated config: {exc}",
        )
        self._send_json(payload, status=status_code)

    def _handle_config_apply(self):
        if not self._require_alignment_resources():
            return
        request = self._config_export_request()
        if request is None:
            return
        draft, count, _overwrite, features, proposals, broker, manual_devices = request
        payload, status_code = self._setup_config_transaction(
            draft,
            count,
            features,
            proposals,
            broker,
            manual_devices,
            lambda change: self.server.config_apply.apply(
                draft,
                count,
                features,
                proposals,
                broker,
                manual_devices,
                prepared=change,
            ),
            failure_reason="apply_failed",
            failure_message="Could not apply the config to the EMS installation: {exc}",
        )
        self._send_json(payload, status=status_code)

    def _setup_config_transaction(
        self,
        draft,
        count,
        features,
        proposals,
        broker,
        manual_devices,
        commit,
        *,
        failure_reason,
        failure_message,
    ):
        """Stage setup credentials and commit the config as one serialized unit.

        The target config is generated first and credential staging works
        from it, exactly like the Maintenance apply, so both flows share one
        reuse/rotate/provision decision path. Staging happens before the
        config write so a credential problem fails before config.json
        changes; a failed or declined commit rolls the staged records back so
        no config ever references a missing secret and no orphan secret
        survives. Everything — validation, staging, commit, rollback — runs
        inside the one apply transaction shared with Maintenance, so parallel
        Apply requests serialize instead of corrupting each other's
        credential state. Returns ``(payload, http_status)``.
        """

        changes = []
        with self.server.config_apply.apply_transaction():
            # Capture the config's revision (or its expected absence) before
            # staging so the commit can refuse to overwrite a config edited or
            # created externally while credentials were being staged.
            expected_revision, expect_absent = (
                self.server.config_apply.capture_config_revision()
            )
            try:
                # Serialize the target config exactly once: credentials are
                # staged for these exact bytes and the commit writes them, so the
                # staged and written configs can never diverge.
                change = self.server.config_export.prepare(
                    draft,
                    count,
                    features,
                    proposals,
                    broker,
                    manual_devices,
                    expected_revision=expected_revision,
                    expect_absent=expect_absent,
                )
            except ConfigExportValidationError as exc:
                return self._validation_failure_payload(exc.preview, changes), 422
            try:
                self._stage_setup_credentials(change.parsed_config, broker, changes)
            except CredentialStoreError as exc:
                return (
                    self._rollback_staged_credentials(
                        changes,
                        # "reason" keeps the legacy Setup value; "code" reflects
                        # the specific credential failure (invalid ref, source
                        # conflict, or the shared provisioning code).
                        self._credential_error_payload(
                            exc, reason="credential_promotion_failed"
                        ),
                        error=exc,
                    ),
                    400,
                )
            try:
                result = commit(change)
            except ConfigExportValidationError as exc:
                return self._validation_failure_payload(exc.preview, changes), 422
            except ConfigChangedError as exc:
                # The config was edited or created externally during staging;
                # never overwrite it. Roll the staged credentials back so no
                # orphan secret survives and the external config bytes are kept.
                return (
                    self._rollback_staged_credentials(
                        changes,
                        {"ok": False, "status": "conflict", "message": str(exc)},
                    ),
                    409,
                )
            except OSError as exc:
                return (
                    self._rollback_staged_credentials(
                        changes,
                        {
                            "ok": False,
                            "reason": failure_reason,
                            "message": failure_message.format(exc=exc),
                        },
                    ),
                    500,
                )
            if result.get("reason") == "target_exists":
                # The config was not written (overwrite declined); the staged
                # records would reference nothing, so remove the newly created
                # ones.
                return self._rollback_staged_credentials(changes, result), 409
        return result, 200

    def _handle_deployment_prepare(self):
        if not self._require_alignment_resources():
            return
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
        payload = dict(result["job"])
        # The durable backend transition stays the source of truth; prepare
        # only mirrors it and never advances the System Build stage itself.
        transition = self._alignment_status().get("transition")
        if transition:
            payload["transition"] = transition
        self._send_json(payload, status=result.get("status", 202))

    def _send_deployment_job(self, job_id):
        job = self.server.deployment.job(job_id.strip("/"))
        if job is None:
            self._send_json({"error": "unknown job_id"}, status=404)
            return
        self._send_json(job)

    def _handle_deployment_start(self):
        if not self._require_alignment_resources(allow_ems_pending=True):
            return
        body = self._read_optional_json_body()
        if body is None:
            return
        if body:
            self._send_json(
                {"error": "deployment start does not accept parameters"}, status=400
            )
            return
        transition = self._alignment_status().get("transition") or {}
        operation_id = transition.get("operation_id")
        if self._reject_nonstartable_ems_stage(transition.get("stage")):
            return
        if operation_id:
            try:
                if transition.get("stage") == "resources_verified":
                    self.server.system_alignment.begin_ems_operation(
                        operation_id=operation_id
                    )
                claimed = self.server.system_alignment.claim_ems_operation(
                    operation_id=operation_id
                )
            except SystemAlignmentError as exc:
                self._send_alignment_error(exc)
                return
            if not claimed:
                self._send_json(
                    {
                        "ok": False,
                        "error": "ems_operation_already_claimed",
                        "message": "This EMS deployment is already running.",
                    },
                    status=409,
                )
                return

        # Claim before the worker starts mutating, so liveness has no
        # registration gap; a refused claim means abandonment already won.
        worker_token = None
        if operation_id:
            worker_token = self.server.operation_coordinator.claim(operation_id)
            if worker_token is None:
                self._send_json(
                    {
                        "ok": False,
                        "reason": "ems_deployment_abandoned",
                        "message": (
                            "The System Build transition was abandoned before "
                            "the EMS deployment could start."
                        ),
                    },
                    status=409,
                )
                return

        def on_complete(job):
            self._complete_deployment_alignment(operation_id, job)

        def on_healthcheck(_job):
            self.server.system_alignment.finish_ems_operation(
                operation_id=operation_id,
                succeeded=True,
            )

        worker_started = False
        try:
            try:
                start_kwargs = {
                    "on_complete": on_complete if operation_id else None,
                }
                # The productive deployment service exposes the exact boundary
                # between container startup and dashboard health verification.
                # Test and third-party adapters with the older callback surface
                # continue to finalize through on_complete below.
                if operation_id and isinstance(
                    self.server.deployment, DeploymentService
                ):
                    start_kwargs["on_healthcheck"] = on_healthcheck
                result = self.server.deployment.start(**start_kwargs)
                if not isinstance(result, dict):
                    raise TypeError("deployment start returned no result")
            except Exception:
                if operation_id:
                    try:
                        self.server.system_alignment.finish_ems_operation(
                            operation_id=operation_id,
                            succeeded=False,
                            error_code="ems_deployment_unexpected_failure",
                            error_message="EMS deployment failed unexpectedly.",
                        )
                    except SystemAlignmentError:
                        pass
                self._send_json(
                    {
                        "ok": False,
                        "reason": "ems_deployment_unexpected_failure",
                        "message": "EMS deployment failed unexpectedly.",
                    },
                    status=500,
                )
                return
            if not result.get("ok", False):
                if operation_id:
                    self.server.system_alignment.finish_ems_operation(
                        operation_id=operation_id,
                        succeeded=False,
                        error_code=result.get("reason") or "ems_deployment_failed",
                        error_message=result.get("message")
                        or "EMS deployment failed.",
                    )
                payload = {
                    "ok": False,
                    "reason": result.get("reason"),
                    "message": result.get("message"),
                }
                if result.get("detail"):
                    payload["detail"] = result["detail"]
                self._send_json(payload, status=result.get("status", 409))
                return
            # The worker is running; its terminal callback releases the claim.
            worker_started = True
            self._send_json(result["job"], status=result.get("status", 202))
        finally:
            if worker_token is not None and not worker_started:
                self.server.operation_coordinator.release(worker_token)

    def _complete_deployment_alignment(self, operation_id, job):
        """Commit terminal deployment state from its worker, never a GET poll."""

        if not operation_id or not isinstance(job, dict):
            return
        try:
            if job.get("status") == "failed":
                error = job.get("error") or {}
                transition = self._alignment_status().get("transition") or {}
                if transition.get("stage") == "healthcheck_pending":
                    self.server.system_alignment.finish_healthcheck(
                        operation_id=operation_id,
                        passed=False,
                        error_code=error.get("code") or "healthcheck_failed",
                        error_message=error.get("message") or "EMS health check failed.",
                    )
                else:
                    self.server.system_alignment.finish_ems_operation(
                        operation_id=operation_id,
                        succeeded=False,
                        error_code=error.get("code") or "ems_deployment_failed",
                        error_message=error.get("message") or "EMS deployment failed.",
                    )
                return
            if job.get("status") != "succeeded":
                return
            transition = self._alignment_status().get("transition") or {}
            if transition.get("stage") == "ems_operation_running":
                # Compatibility path for deployment adapters that only expose a
                # terminal completion callback.
                self.server.system_alignment.finish_ems_operation(
                    operation_id=operation_id, succeeded=True
                )
            healthy = job.get("dashboard_reachable") is True
            self.server.system_alignment.finish_healthcheck(
                operation_id=operation_id,
                passed=healthy,
                error_code=None if healthy else "healthcheck_failed",
                error_message=(
                    None
                    if healthy
                    else "EMS started but its dashboard health check failed."
                ),
            )
        except SystemAlignmentError:
            # A concurrent resume may already have committed the same terminal
            # observation; the durable service owns idempotency.
            return
        finally:
            # Terminal callback reached: release the claim so an expired
            # orphan becomes abandonable.
            self.server.operation_coordinator.release_operation(operation_id)

    def _send_deployment_start_job(self, job_id):
        job_id = job_id.strip("/")
        job = self.server.deployment.start_job(job_id)
        if job is None:
            self._send_json({"error": "unknown job_id"}, status=404)
            return
        payload = dict(job)
        transition = self._alignment_status().get("transition")
        if transition is not None:
            payload["transition"] = transition
        self._send_json(payload)

    def _handle_deployment_permission_repair(self):
        if not self._require_alignment_resources(allow_ems_pending=True):
            return
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
        alignment = self._alignment_status()
        if not self._alignment_resources_allowed(
            alignment, allow_ems_pending=True
        ):
            recovered = self._recover_container_conflict_transition(
                alignment, container_name=container_name, action=action
            )
            if recovered is None:
                return
            if not recovered:
                self._require_alignment_resources(allow_ems_pending=True)
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
        payload = dict(result)
        transition = self._alignment_status().get("transition")
        if transition is not None:
            payload["transition"] = transition
        self._send_json(payload)

    def _validation_failure_payload(self, preview, changes=None):
        payload = {
            "ok": False,
            "reason": "validation_failed",
            "message": "Fix config validation errors before exporting.",
            "validation": preview["validation"],
        }
        if changes is not None:
            self._rollback_staged_credentials(changes, payload)
        return payload

    def _send_validation_failure(self, preview, changes=None):
        self._send_json(self._validation_failure_payload(preview, changes), status=422)

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

    # --- Zendure cloud MQTT discovery ------------------------------------

    def _handle_zendure_cloud_settings(self):
        # Read-only redacted status; the raw token is never returned. A store
        # fault degrades to a clear not-configured status, never a 500.
        try:
            payload = self.server.zendure_cloud_discovery.settings()
        except Exception:
            self._send_json(
                {
                    "token_saved": False,
                    "last_status": "error",
                    "last_error": "Could not read the Zendure cloud settings.",
                }
            )
            return
        self._send_json(payload)

    @staticmethod
    def _zendure_api_key_field(body, *, required):
        """Extract the Zendure credential from ``api_key`` or ``token``.

        Both fields accept the single auto-detected credential value; supplying
        both is rejected.
        """

        api_key = body.get("api_key")
        token = body.get("token")
        if api_key is not None and token is not None:
            return None, "provide only api_key, not both api_key and token"
        value = api_key if api_key is not None else token
        if value is None:
            if required:
                return None, "api_key is required"
            return None, None
        if not isinstance(value, str) or not value.strip():
            return None, "api_key must be a non-empty string"
        return value.strip(), None

    def _handle_zendure_cloud_save_token(self):
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict) or set(body) - {
            "api_key", "token", "validate", "credential_mode"
        }:
            self._send_json({"error": "unsupported API key field"}, status=400)
            return
        if not credential_mode_is_supported(body.get("credential_mode")):
            self._send_json(
                {
                    "ok": False,
                    "error": "unsupported_credential_mode",
                    "message": (
                        "Use a Zendure API key or HA/deviceList token for "
                        "Zendure MQTT discovery."
                    ),
                },
                status=400,
            )
            return
        api_key, error = self._zendure_api_key_field(body, required=True)
        if error is not None:
            self._send_json({"error": error}, status=400)
            return
        validate = body.get("validate", False)
        if not isinstance(validate, bool):
            self._send_json({"error": "validate must be a boolean"}, status=400)
            return
        try:
            result = self.server.zendure_cloud_discovery.save_token(
                api_key, validate=validate
            )
        except ZendureCloudError as exc:
            self._send_json(
                {"ok": False, "error": "invalid_api_key", "message": str(exc)},
                status=400,
            )
            return
        except (SecretStoreError, CredentialStoreError) as exc:
            self._send_json(
                {"ok": False, "error": "store_failed", "message": str(exc)},
                status=500,
            )
            return
        self._set_zendure_token_ref("zendure-cloud")
        self._send_json(result)

    def _handle_zendure_cloud_delete_token(self):
        self._drain_body()
        try:
            result = self.server.zendure_cloud_discovery.delete_token()
        except (SecretStoreError, CredentialStoreError) as exc:
            self._send_json(
                {"ok": False, "error": "store_failed", "message": str(exc)},
                status=500,
            )
            return
        self._set_zendure_token_ref(None)
        self._send_json(result)

    def _set_zendure_token_ref(self, token_ref):
        # Best-effort: the connections metadata only mirrors whether a token is
        # configured; a metadata write fault must not fail the token operation.
        try:
            self.server.discovery_preparation.set_zendure_token_ref(token_ref)
        except Exception:
            pass

    def _handle_zendure_cloud_test(self):
        body = self._read_optional_json_body()
        if body is None:
            return
        if set(body) - {"api_key", "token", "credential_mode"}:
            self._send_json({"error": "unsupported API key field"}, status=400)
            return
        if not credential_mode_is_supported(body.get("credential_mode")):
            self._send_json(
                {
                    "ok": False,
                    "error": "unsupported_credential_mode",
                    "message": (
                        "Use a Zendure API key or HA/deviceList token for "
                        "Zendure MQTT discovery."
                    ),
                },
                status=400,
            )
            return
        api_key, error = self._zendure_api_key_field(body, required=False)
        if error is not None:
            self._send_json({"error": error}, status=400)
            return
        try:
            result = self.server.zendure_cloud_discovery.test(api_key=api_key)
        except ZendureCloudError as exc:
            self._send_json(
                {"ok": False, "error": "invalid_api_key", "message": str(exc)},
                status=400,
            )
            return
        except Exception:
            self._send_json(
                {
                    "ok": False,
                    "error": "test_failed",
                    "message": "Zendure device list request failed.",
                },
                status=502,
            )
            return
        status = 200 if result.get("ok") else 400
        self._send_json(result, status=status)

    def _handle_zendure_cloud_refresh(self):
        self._drain_body()
        try:
            result = self.server.zendure_cloud_discovery.refresh()
        except Exception:
            self._send_json(
                {
                    "ok": False,
                    "error": "refresh_failed",
                    "message": "Zendure cloud discovery failed unexpectedly.",
                },
                status=502,
            )
            return
        status = 200 if result.get("ok") else 400
        self._send_json(result, status=status)

    # --- unified discovery preparation / run -----------------------------

    def _handle_discovery_preparation_save(self):
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, status=400)
            return
        try:
            saved = self.server.discovery_preparation.save(body)
        except (DiscoveryPreparationError, DiscoveryConnectionsError) as exc:
            self._send_json(
                {"ok": False, "error": "store_failed", "message": str(exc)},
                status=500,
            )
            return
        self._send_json(saved)

    # --- discovery connections (persistent, EMS-owned) -------------------

    def _connections_view(self):
        """Redaction-safe connections state; never any raw secret value."""

        connections = self.server.discovery_preparation.load()
        brokers = []
        for broker in connections["local_mqtt"]["brokers"]:
            item = dict(broker)
            ref = broker.get("credentials_ref")
            status = (
                self.server.credential_store.mqtt_broker_secret_status(ref)
                if ref
                else None
            )
            item["username_configured"] = bool(status and status["username_configured"])
            item["password_configured"] = bool(status and status["password_configured"])
            item["credentials_encrypted"] = bool(status and status["encrypted"])
            item["transport"] = "tls" if broker.get("tls") else "plaintext"
            item["auth_mode"] = (
                "username_password"
                if item["username_configured"] or item["password_configured"]
                else "anonymous"
            )
            brokers.append(item)
        credentials = self._mqtt_credentials_view(connections)
        zendure = self.server.zendure_cloud_discovery.settings()
        return {
            "discovery_priority": connections["discovery_priority"],
            "sources": connections["sources"],
            "local_api": connections["local_api"],
            "local_mqtt": {
                "enabled": connections["local_mqtt"]["enabled"],
                "credential_refs": connections["local_mqtt"]["credential_refs"],
                "credentials": credentials,
                # Legacy broker entries are tolerated on read only; the Discovery
                # flow no longer manages broker-specific connection config.
                "brokers": brokers,
            },
            "zendure_mqtt": {
                "enabled": connections["zendure_mqtt"]["enabled"],
                "token_ref": connections["zendure_mqtt"]["token_ref"],
                "token_saved": bool(zendure.get("token_saved")),
            },
        }

    def _mqtt_credentials_view(self, connections):
        """Redacted discovery credential pool entries (never a raw secret)."""

        credentials = []
        for ref in connections["local_mqtt"]["credential_refs"]:
            status = self.server.credential_store.mqtt_discovery_secret_status(ref)
            credentials.append(
                {
                    "id": status["id"],
                    "label": status["label"] or status["id"],
                    "username_configured": status["username_configured"],
                    "password_configured": status["password_configured"],
                    "credentials_encrypted": status["credentials_encrypted"],
                }
            )
        return credentials

    def _handle_mqtt_credentials_list(self):
        connections = self.server.discovery_preparation.load()
        self._send_json({"credentials": self._mqtt_credentials_view(connections)})

    def _handle_mqtt_credential_save(self):
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, status=400)
            return
        label = str(body.get("label") or "").strip()
        raw_id = str(body.get("id") or "").strip()
        if not label and not raw_id:
            self._send_json({"error": "label is required"}, status=400)
            return
        credential_id = CredentialStore.normalize_ref(raw_id or label)
        label = label or credential_id
        username = body.get("username")
        password = body.get("password")
        try:
            self.server.credential_store.save_mqtt_discovery_secret(
                credential_id, username, password, label=label
            )
            self.server.discovery_preparation.add_credential_ref(credential_id)
        except (DiscoveryPreparationError, DiscoveryConnectionsError, CredentialStoreError) as exc:
            self._send_json(
                {"ok": False, "error": "store_failed", "message": str(exc)},
                status=500,
            )
            return
        self._send_json(self._connections_view())

    def _handle_mqtt_credential_delete(self, credential_id):
        self._drain_body()
        credential_id = CredentialStore.normalize_ref(credential_id)
        removed = self.server.discovery_preparation.remove_credential_ref(credential_id)
        try:
            self.server.credential_store.forget_mqtt_discovery_secret(credential_id)
        except CredentialStoreError:
            pass
        self._send_json({"ok": True, "removed": bool(removed)})

    def _handle_connections_local_api_save(self):
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, status=400)
            return
        try:
            self.server.discovery_preparation.save({"local_api": body})
        except (DiscoveryPreparationError, DiscoveryConnectionsError) as exc:
            self._send_json(
                {"ok": False, "error": "store_failed", "message": str(exc)},
                status=500,
            )
            return
        self._send_json(self._connections_view())

    def _handle_connections_broker_save(self):
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, status=400)
            return
        host = str(body.get("host") or "").strip()
        if not host:
            self._send_json({"error": "host is required"}, status=400)
            return
        # Validate the full broker body with the shared Core helpers before any
        # secret or metadata is written. An invalid port/TLS field returns 400
        # instead of being silently dropped (or string-coerced), and no orphan
        # credential file is created ahead of a rejected broker.
        from ems.config import (
            default_mqtt_port,
            parse_mqtt_port,
            resolve_mqtt_tls_metadata,
        )

        try:
            tls, _tls_insecure = resolve_mqtt_tls_metadata(
                tls_mode=body.get("tls_mode"), tls=body.get("tls")
            )
            port = parse_mqtt_port(body.get("port"), default=default_mqtt_port(tls))
        except ValueError as exc:
            self._send_json(
                {"ok": False, "error": "invalid_broker", "message": str(exc)},
                status=400,
            )
            return
        username = body.get("username")
        password = body.get("password")
        try:
            CredentialStore._validate_mqtt_auth_pair(username, password)
        except CredentialStoreError as exc:
            self._send_json(
                {"ok": False, "error": "invalid_broker", "message": str(exc)},
                status=400,
            )
            return
        broker_id = CredentialStore.normalize_ref(
            body.get("id") or body.get("label") or host
        )
        credentials_ref = self._existing_broker_ref(broker_id)
        try:
            if username or password:
                self.server.credential_store.save_mqtt_broker_secret(
                    broker_id, username, password
                )
                credentials_ref = broker_id
            broker = {
                "id": broker_id,
                "label": body.get("label") or host,
                "host": host,
                "port": port,
                "tls": tls,
                "tls_mode": body.get("tls_mode"),
                "credentials_ref": credentials_ref,
            }
            self.server.discovery_preparation.upsert_broker(broker)
        except (DiscoveryPreparationError, DiscoveryConnectionsError, CredentialStoreError) as exc:
            self._send_json(
                {"ok": False, "error": "store_failed", "message": str(exc)},
                status=500,
            )
            return
        self._reseed_configured_brokers()
        self._send_json(self._connections_view())

    def _handle_connections_broker_delete(self, broker_id):
        self._drain_body()
        broker_id = CredentialStore.normalize_ref(broker_id)
        removed = self.server.discovery_preparation.remove_broker(broker_id)
        if removed and removed.get("credentials_ref"):
            try:
                self.server.credential_store.forget_mqtt_broker_secret(
                    removed["credentials_ref"]
                )
            except CredentialStoreError:
                pass
        self._reseed_configured_brokers()
        self._send_json({"ok": True, "removed": bool(removed)})

    def _existing_broker_ref(self, broker_id):
        for broker in self.server.discovery_preparation.load()["local_mqtt"]["brokers"]:
            if broker["id"] == broker_id:
                return broker.get("credentials_ref")
        return None

    def _reseed_configured_brokers(self):
        try:
            self.server.mqtt_discovery.set_configured_brokers(
                self.server.discovery_preparation.load()["local_mqtt"]["brokers"]
            )
        except Exception:
            pass

    def _handle_discovery_run(self):
        body = self._read_optional_json_body()
        if body is None:
            return
        refresh = isinstance(body, dict) and bool(body.get("refresh"))
        preparation = self.server.discovery_preparation.load()
        self._send_json(
            run_discovery(
                preparation,
                refresh=refresh,
                registry=self.server.registry,
                mdns_provider=self.server.mdns_provider,
                mqtt_discovery=self.server.mqtt_discovery,
                zendure_cloud_discovery=self.server.zendure_cloud_discovery,
            )
        )

    def _handle_discovery_source_refresh(self, source):
        self._drain_body()
        if source not in DISCOVERY_SOURCES:
            self._send_json({"error": "unknown discovery source"}, status=404)
            return
        try:
            if source == SOURCE_LOCAL_API:
                result = self.server.mdns_provider.refresh()
            elif source == SOURCE_LOCAL_MQTT:
                result = self.server.mqtt_discovery.refresh()
            else:
                result = self.server.zendure_cloud_discovery.refresh()
        except Exception:
            self._send_json(
                {"ok": False, "error": "refresh_failed", "source": source},
                status=502,
            )
            return
        self._send_json({"ok": True, "source": source, "result": result})

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
        "progress": record.get("progress") or {
            "total_hosts": 0,
            "checked_hosts": 0,
            "found_devices": 0,
            "failed_hosts": 0,
            "percent": 0,
        },
    }


def create_server(host="127.0.0.1", port=8090, registry=None, static_assets=None,
                  network_detector=None, gateway_prober=None, mdns_provider=None,
                  mqtt_discovery=None, zendure_cloud_discovery=None,
                  release_manager=None, config_export=None,
                  config_apply=None, deployment=None, ems_cli=None,
                  guided_upgrade=None, guided_upgrade_context=None,
                  system_alignment=None, admin_update=None,
                  backup_service=None, setup_intents=None,
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
            zendure_cloud_discovery=zendure_cloud_discovery,
            release_manager=release_manager,
            config_export=config_export,
            config_apply=config_apply,
            deployment=deployment,
            ems_cli=ems_cli,
            guided_upgrade=guided_upgrade,
            guided_upgrade_context=guided_upgrade_context,
            system_alignment=system_alignment,
            admin_update=admin_update,
            backup_service=backup_service,
            setup_intents=setup_intents,
        )
    return AdminServer(
        (host, int(port)), AdminHandler, runtime=runtime, https_active=https_active,
    )
