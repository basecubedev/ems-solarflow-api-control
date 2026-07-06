# SPDX-License-Identifier: AGPL-3.0-or-later
"""Post-config-apply container synchronisation for Maintenance.

After the Maintenance workflow writes a new ``config/config.json`` the running
Docker stack still has to be brought in line with it. This module computes a
conservative desired/current/action plan and, on explicit confirmation, runs
only the required ``docker compose`` operations.

Safety: this never removes containers, volumes or data. Disabling a feature
stops the feature-owned container (``docker compose stop``) but leaves its
bind-mounted data and secrets untouched. ``down``/``rm``/``down -v`` and any
volume-removing command are intentionally not used.
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from admin.deployment import DockerCompose
from admin.install_context import detect_install_context
from admin.maintenance import run_maintenance_overview

ANALYTICS_PROFILE = "with-analytics"
EMS_SERVICE = "ems"
INFLUX_SERVICE = "influxdb"

DESIRED_RUNNING = "running"
DESIRED_STOPPED = "stopped"
DESIRED_UNAVAILABLE = "unavailable"

_UNAVAILABLE_MESSAGE = "Docker/Compose is not available. Re-check the Admin deployment."


@dataclass(frozen=True)
class ServiceDesiredState:
    service: str
    desired: str  # "running" | "stopped" | "unavailable"
    reason: str


@dataclass(frozen=True)
class ServiceAction:
    service: str
    action: str  # "none" | "start" | "recreate" | "stop" | "unavailable"
    label: str
    reason: str


def _ems_desired(config_exists, compose_exists):
    # system.enabled/dashboard.enabled disable app features, not the runtime
    # container: EMS stays desired whenever a standard install is present.
    if config_exists and compose_exists:
        return ServiceDesiredState(
            EMS_SERVICE, DESIRED_RUNNING, "Standard EMS installation is present."
        )
    return ServiceDesiredState(
        EMS_SERVICE,
        DESIRED_UNAVAILABLE,
        "No standard EMS installation was detected.",
    )


def _influx_desired(config):
    influx = config.get("influxdb") if isinstance(config, dict) else None
    if not isinstance(influx, dict):
        return ServiceDesiredState(
            INFLUX_SERVICE,
            DESIRED_STOPPED,
            "InfluxDB analytics is not configured.",
        )
    enabled = influx.get("enabled") is True
    mode = influx.get("mode")
    if enabled and mode == "bundled":
        return ServiceDesiredState(
            INFLUX_SERVICE, DESIRED_RUNNING, "Bundled InfluxDB is enabled in config."
        )
    if enabled and mode == "external":
        return ServiceDesiredState(
            INFLUX_SERVICE,
            DESIRED_STOPPED,
            "InfluxDB is configured in external mode; the bundled container is not used.",
        )
    if not enabled:
        return ServiceDesiredState(
            INFLUX_SERVICE, DESIRED_STOPPED, "InfluxDB analytics is disabled in config."
        )
    return ServiceDesiredState(
        INFLUX_SERVICE,
        DESIRED_STOPPED,
        "InfluxDB analytics configuration is incomplete; the bundled container is not used.",
    )


def influx_auto_init_enabled(config):
    influx = config.get("influxdb") if isinstance(config, dict) else None
    if not isinstance(influx, dict):
        return False
    return influx.get("auto_init") is not False


def influx_auto_sync_enabled(config):
    influx = config.get("influxdb") if isinstance(config, dict) else None
    if not isinstance(influx, dict):
        return False
    return influx.get("auto_sync") is not False


def _current_state(container):
    if not isinstance(container, dict):
        return {"found": False, "running": False, "status": "unknown", "name": None}
    status = container.get("status") or ("running" if container.get("running") else "missing")
    return {
        "found": bool(container.get("found")),
        "running": bool(container.get("running")),
        "status": str(status),
        "name": container.get("name"),
    }


def _display_container_state(current):
    # Collapse the raw docker status into the three states the UI reasons about.
    if current.get("running"):
        return "running"
    if not current.get("found"):
        return "missing"
    return "stopped"


def build_ems_display_state(config_present, compose_present, current, desired_state):
    """Derive the user-facing EMS status (feature/container/desired separated)."""

    container_state = _display_container_state(current)
    configured = bool(config_present) and bool(compose_present)
    if not configured:
        return {
            "configured": False,
            "container_present": bool(current.get("found")),
            "container_state": container_state,
            "desired_state": "stopped",
            "display_state": "not_configured",
            "display_label": "Not configured",
            "display_detail": "config.json or docker-compose.yml is missing",
            "tone": "muted",
        }

    if container_state == "running":
        label, tone, detail = "Running", "ok", "EMS container is running"
    elif container_state == "stopped":
        label, tone, detail = (
            "Configured · stopped",
            "warn",
            "EMS is configured, but the container is stopped",
        )
    else:
        label, tone, detail = (
            "Configured · missing",
            "warn",
            "config.json and docker-compose.yml exist, but the EMS container is missing",
        )

    return {
        "configured": True,
        "container_present": bool(current.get("found")),
        "container_state": container_state,
        "desired_state": desired_state or "running",
        "display_state": container_state,
        "display_label": label,
        "display_detail": detail,
        "tone": tone,
    }


def build_influx_display_state(config, current, desired_state):
    """Derive the user-facing InfluxDB status.

    A disabled feature with a missing container is healthy (``muted``); an enabled
    bundled feature with a missing/stopped container is actionable (``warn``).
    """

    raw = config.get("influxdb") if isinstance(config, dict) else None
    influx_cfg = raw if isinstance(raw, dict) else {}
    enabled = influx_cfg.get("enabled") is True
    mode = str(influx_cfg.get("mode") or "").lower()
    container_state = _display_container_state(current)

    if not enabled:
        return {
            "feature_state": "disabled",
            "mode": mode or "bundled",
            "configured": False,
            "container_present": bool(current.get("found")),
            "container_state": container_state,
            "desired_state": "stopped",
            "display_state": "disabled",
            "display_label": "Disabled",
            "display_detail": "Analytics is disabled in config.json; bundled InfluxDB is not required",
            "tone": "muted",
        }

    if mode == "external":
        return {
            "feature_state": "external",
            "mode": "external",
            "configured": True,
            "container_present": bool(current.get("found")),
            "container_state": container_state,
            "desired_state": "stopped",
            "display_state": "external",
            "display_label": "External",
            "display_detail": "External InfluxDB is configured; bundled container is not required",
            "tone": "info",
        }

    if container_state == "running":
        label, tone, detail = "Running", "ok", "Bundled Analytics is active"
    elif container_state == "stopped":
        label, tone, detail = (
            "Configured · stopped",
            "warn",
            "Bundled Analytics is enabled, container exists but is stopped",
        )
    else:
        label, tone, detail = (
            "Configured · missing",
            "warn",
            "Bundled Analytics is enabled, but the container is missing",
        )

    return {
        "feature_state": "bundled",
        "mode": "bundled",
        "configured": True,
        "container_present": bool(current.get("found")),
        "container_state": container_state,
        "desired_state": desired_state or "running",
        "display_state": container_state,
        "display_label": label,
        "display_detail": detail,
        "tone": tone,
    }


def _summary_word(display):
    state = display.get("display_state")
    if state == "not_configured":
        return "not configured"
    if state == "disabled":
        return "disabled"
    if state == "external":
        return "external"
    if display.get("configured") and state == "missing":
        return "configured but missing"
    if display.get("configured") and state == "stopped":
        return "stopped"
    return state or "unknown"


def build_container_summary(ems_display, influx_display):
    return f"EMS {_summary_word(ems_display)} · InfluxDB {_summary_word(influx_display)}"


def _ems_action(desired, available):
    if not available:
        return ServiceAction(
            EMS_SERVICE, "unavailable", "EMS", _UNAVAILABLE_MESSAGE
        )
    if desired.desired == DESIRED_RUNNING:
        # Recreate so EMS re-reads the freshly written mounted config cleanly.
        return ServiceAction(
            EMS_SERVICE,
            "recreate",
            "Recreate EMS",
            "Config changed and EMS should reload it.",
        )
    return ServiceAction(
        EMS_SERVICE, "none", "EMS", "No standard EMS installation to manage."
    )


def _influx_action(desired, current, available):
    if not available:
        return ServiceAction(
            INFLUX_SERVICE, "unavailable", "Bundled InfluxDB", _UNAVAILABLE_MESSAGE
        )
    if desired.desired == DESIRED_RUNNING:
        if current["running"]:
            return ServiceAction(
                INFLUX_SERVICE, "none", "Bundled InfluxDB", "Bundled InfluxDB is already running."
            )
        return ServiceAction(
            INFLUX_SERVICE,
            "start",
            "Start bundled InfluxDB",
            "Bundled InfluxDB is enabled but not running.",
        )
    if current["running"]:
        return ServiceAction(
            INFLUX_SERVICE,
            "stop",
            "Stop bundled InfluxDB",
            "Bundled InfluxDB is disabled or external; the container is stopped (data is preserved).",
        )
    return ServiceAction(
        INFLUX_SERVICE, "none", "Bundled InfluxDB", "Bundled InfluxDB is not desired and not running."
    )


def _influx_sync_action(desired, available, config):
    if not available:
        return ServiceAction(
            INFLUX_SERVICE, "unavailable", "InfluxDB schema", _UNAVAILABLE_MESSAGE
        )
    if desired.desired != DESIRED_RUNNING:
        return ServiceAction(
            INFLUX_SERVICE,
            "none",
            "InfluxDB schema",
            "Bundled InfluxDB schema sync is not needed.",
        )
    if not influx_auto_sync_enabled(config):
        return ServiceAction(
            INFLUX_SERVICE,
            "none",
            "InfluxDB schema",
            "InfluxDB auto_sync is disabled in config.",
        )
    return ServiceAction(
        INFLUX_SERVICE,
        "sync",
        "Sync InfluxDB schema",
        "Analytics buckets, retention and tasks will be reconciled.",
    )


def _summary(actions):
    parts = []
    for action in actions:
        if action.action == "recreate" and action.service == EMS_SERVICE:
            parts.append("EMS will be recreated so it reads the new config.")
        elif action.action == "start" and action.service == INFLUX_SERVICE:
            parts.append("Bundled InfluxDB will be started because Analytics is enabled.")
        elif action.action == "stop" and action.service == INFLUX_SERVICE:
            parts.append("Bundled InfluxDB will be stopped because Analytics is disabled or external.")
        elif action.action == "sync" and action.service == INFLUX_SERVICE:
            parts.append("InfluxDB schema will be synced before EMS is recreated.")
    if not parts:
        return "No container changes are required."
    parts.append("No volumes or data will be removed.")
    return " ".join(parts)


def build_container_sync_plan(config, overview):
    """Build the desired/current/action plan from config and a maintenance overview.

    Pure and side-effect free: it never touches Docker. ``overview`` is the dict
    returned by :func:`admin.maintenance.run_maintenance_overview`.
    """

    paths = overview.get("paths", {}) if isinstance(overview, dict) else {}
    compose_info = paths.get("compose", {}) if isinstance(paths, dict) else {}
    config_info = paths.get("config", {}) if isinstance(paths, dict) else {}
    compose_path = compose_info.get("path")
    compose_exists = bool(compose_info.get("exists"))
    config_exists = bool(config_info.get("exists"))
    install_root = str(Path(compose_path).parent) if compose_path else None

    docker_available = bool(
        (overview.get("docker") or {}).get("available") if isinstance(overview, dict) else False
    )
    available = docker_available and compose_exists

    containers = overview.get("containers", {}) if isinstance(overview, dict) else {}
    ems_desired = _ems_desired(config_exists, compose_exists)
    influx_desired = _influx_desired(config if isinstance(config, dict) else {})
    ems_current = _current_state(containers.get(EMS_SERVICE))
    influx_current = _current_state(containers.get(INFLUX_SERVICE))

    ems_display = build_ems_display_state(
        config_exists, compose_exists, ems_current, ems_desired.desired
    )
    influx_display = build_influx_display_state(
        config if isinstance(config, dict) else {}, influx_current, influx_desired.desired
    )

    ems_action = _ems_action(ems_desired, available)
    influx_action = _influx_action(influx_desired, influx_current, available)
    influx_sync_action = _influx_sync_action(influx_desired, available, config)
    actions = [
        asdict(action)
        for action in (ems_action, influx_action, influx_sync_action)
        if action.action in ("start", "recreate", "stop", "sync")
    ]

    plan = {
        "ok": True,
        "available": available,
        "install_root": install_root,
        "compose_path": compose_path,
        "requires_confirmation": True,
        "desired": {
            EMS_SERVICE: asdict(ems_desired),
            INFLUX_SERVICE: asdict(influx_desired),
        },
        "current": {
            EMS_SERVICE: ems_current,
            INFLUX_SERVICE: influx_current,
        },
        "services": {
            EMS_SERVICE: ems_display,
            INFLUX_SERVICE: influx_display,
        },
        "status_summary": build_container_summary(ems_display, influx_display),
        "actions": actions,
        "summary": _summary([ems_action, influx_action, influx_sync_action]),
        "auto_init": influx_auto_init_enabled(config),
        "auto_sync": influx_auto_sync_enabled(config),
    }
    if not available:
        plan["message"] = _UNAVAILABLE_MESSAGE
    return plan


def _default_run_influx_init(compose, workspace):
    return compose.run_oneoff(
        workspace,
        EMS_SERVICE,
        ["python3", "emsctl.py", "influx", "init", "--no-start", "--json"],
    )


def _default_run_influx_sync(compose, workspace):
    return compose.run_oneoff(
        workspace,
        EMS_SERVICE,
        ["python3", "emsctl.py", "influx", "sync", "--json"],
        timeout=120,
    )


def _ensure_influx_data_dir(workspace):
    # Admin starts the bundled InfluxDB directly (not via ``influx init``), so the
    # host-side bind-mount target must exist first. Idempotent; never deletes.
    Path(workspace, "data", "influxdb").mkdir(parents=True, exist_ok=True)


class MaintenanceContainerActions:
    """Plan and (on confirmation) run the post-apply container sync.

    Injectable so tests drive it with a fake compose/command runner and never
    touch a real Docker daemon.
    """

    def __init__(
        self,
        compose=None,
        run_influx_init=None,
        run_influx_sync=None,
        install_context_provider=detect_install_context,
        overview_provider=run_maintenance_overview,
    ):
        self.compose = compose or DockerCompose()
        self.run_influx_init = run_influx_init or _default_run_influx_init
        self.run_influx_sync = run_influx_sync or _default_run_influx_sync
        self._install_context_provider = install_context_provider
        self._overview_provider = overview_provider

    def _load_config(self, context):
        if not context.config_exists:
            return {}
        try:
            parsed = json.loads(context.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def plan(self):
        context = self._install_context_provider()
        config = self._load_config(context)
        overview = self._overview_provider()
        return build_container_sync_plan(config, overview)

    def sync(self):
        plan = self.plan()
        if not plan.get("available"):
            return {
                "ok": False,
                "status": "unavailable",
                "message": plan.get("message", _UNAVAILABLE_MESSAGE),
                "plan": plan,
                "steps": [],
            }
        workspace = plan["install_root"]
        steps = []

        influx_action = next(
            (
                item
                for item in plan["actions"]
                if item["service"] == INFLUX_SERVICE and item["action"] in ("start", "stop")
            ),
            None,
        )
        ems_action = next(
            (
                item
                for item in plan["actions"]
                if item["service"] == EMS_SERVICE and item["action"] == "recreate"
            ),
            None,
        )
        influx_desired_running = (
            plan["desired"][INFLUX_SERVICE]["desired"] == DESIRED_RUNNING
        )

        # 1. Bundled InfluxDB secrets before the container is started. On failure
        #    do not start/sync/recreate anything; surface a clear error.
        if influx_desired_running and plan.get("auto_init"):
            try:
                returncode, detail = self.run_influx_init(self.compose, workspace)
            except Exception as exc:  # never leak a traceback to the UI
                return self._failure(
                    "Could not initialise bundled InfluxDB secrets.",
                    steps,
                    plan,
                    step={"service": INFLUX_SERVICE, "action": "init", "status": "error"},
                    detail=str(exc),
                )
            if returncode != 0:
                return self._failure(
                    "Could not initialise bundled InfluxDB secrets.",
                    steps,
                    plan,
                    step={"service": INFLUX_SERVICE, "action": "init", "status": "error"},
                    detail=detail,
                )
            steps.append({"service": INFLUX_SERVICE, "action": "init", "status": "ok"})

        # 2. Stop a disabled/external bundled InfluxDB before touching EMS.
        if influx_action is not None and influx_action["action"] == "stop":
            step = self._run_step(
                influx_action,
                lambda: self.compose.stop(
                    workspace, services=(INFLUX_SERVICE,), profiles=(ANALYTICS_PROFILE,)
                ),
            )
            steps.append(step)
            if step["status"] != "ok":
                return self._failure("Could not stop bundled InfluxDB.", steps, plan)

        # 3. Start bundled InfluxDB if it is desired and not yet running.
        if influx_action is not None and influx_action["action"] == "start":
            step = self._run_step(
                influx_action,
                lambda: (
                    _ensure_influx_data_dir(workspace),
                    self.compose.up(
                        workspace, profiles=(ANALYTICS_PROFILE,), services=(INFLUX_SERVICE,)
                    ),
                ),
            )
            steps.append(step)
            if step["status"] != "ok":
                return self._failure("Could not start bundled InfluxDB.", steps, plan)

        # 4. Reconcile the InfluxDB schema before EMS starts against it. The EMS
        #    Influx helper waits for readiness, so Admin does not poll here.
        if influx_desired_running and plan.get("auto_sync"):
            try:
                returncode, detail = self.run_influx_sync(self.compose, workspace)
            except Exception as exc:  # never leak a traceback to the UI
                return self._failure(
                    "Could not sync InfluxDB analytics schema.",
                    steps,
                    plan,
                    step={"service": INFLUX_SERVICE, "action": "sync", "status": "error"},
                    detail=str(exc),
                )
            if returncode != 0:
                return self._failure(
                    "Could not sync InfluxDB analytics schema.",
                    steps,
                    plan,
                    step={"service": INFLUX_SERVICE, "action": "sync", "status": "error"},
                    detail=detail,
                )
            steps.append({"service": INFLUX_SERVICE, "action": "sync", "status": "ok"})

        # 5. Recreate EMS last so it starts against the ready Analytics backend.
        if ems_action is not None and ems_action["action"] == "recreate":
            step = self._run_step(
                ems_action,
                lambda: self.compose.up(
                    workspace, services=(EMS_SERVICE,), force_recreate=True
                ),
            )
            steps.append(step)
            if step["status"] != "ok":
                return self._failure("Could not recreate the EMS container.", steps, plan)

        return {
            "ok": True,
            "status": "completed",
            "plan": plan,
            "steps": steps,
            "overview": self._overview_provider(),
        }

    def _run_step(self, action, run):
        try:
            run()
        except Exception as exc:  # never leak a traceback to the UI
            detail = getattr(exc, "message", None) or str(exc)
            return {
                "service": action["service"],
                "action": action["action"],
                "status": "error",
                "detail": detail,
            }
        return {"service": action["service"], "action": action["action"], "status": "ok"}

    def _failure(self, message, steps, plan, step=None, detail=None):
        if step is not None:
            if detail:
                step = {**step, "detail": detail}
            steps.append(step)
        return {
            "ok": False,
            "status": "error",
            "message": message,
            "plan": plan,
            "steps": steps,
        }


def build_maintenance_container_plan():
    """Return the current read-only container sync plan (never mutates Docker)."""

    return MaintenanceContainerActions().plan()


def run_maintenance_container_sync():
    """Run the confirmed container sync and return its result."""

    return MaintenanceContainerActions().sync()
