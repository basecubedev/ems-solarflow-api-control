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
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from admin.deployment import EMS_SERVICE, DockerCompose
from admin.install_context import detect_install_context
from admin.maintenance import run_maintenance_overview
from ems.influx_setup import INFLUX_READY_TIMEOUT_SECONDS, SCHEMA_LOCK_WAIT_SECONDS

ANALYTICS_PROFILE = "with-analytics"
INFLUX_SERVICE = "influxdb"

# The schema sync waits for InfluxDB inside this call, so the call has to
# outlive the wait and still leave room for its own requests. The count comes
# from the operator's `influxdb.downsampling`: nineteen on a fresh install of
# the shipped chain, five more per level. The allowance covers one level beyond
# what we ship, plus the one data probe the repair's safe sync makes before it
# shortens a retention; a longer chain needs this raised, and a test says so by
# counting.
#
# Sized for a sync that is slow, not for one where every request runs into its
# own timeout. That worst case is roughly twenty minutes, which cannot be held
# inside a synchronous Admin request, and covering it would buy nothing: a sync
# whose every request times out has failed, and waiting it out does not change
# the outcome. The measurement this is built on is the live Pi 3B+ appliance,
# where all nineteen requests of a full sync answered in well under a second.
#
# The gap between this figure and the per-request tolerance is deliberate and
# not free: a sync whose requests all land between the two -- slow enough to
# take ten seconds each, quick enough to succeed -- is killed although it was
# working. That band is the price of a ceiling an operator can sit in front of,
# and it is paid knowingly because the outcome is detected and the sync can
# simply be run again. Closing it means making the sync asynchronous, not
# raising this number.
#
# Being killed here is neither silent nor final -- the call says it timed out,
# the step is an error the page shows as a warning, and the EMS, restarted all
# the same, completes the schema itself (ems/history/schema_keeper.py). The sync
# is idempotent, so running it again is safe as well.
INFLUX_SYNC_REQUEST_ALLOWANCE = 25
INFLUX_SYNC_SLOW_REQUEST_SECONDS = 10
INFLUX_SYNC_TIMEOUT_SECONDS = (
    INFLUX_READY_TIMEOUT_SECONDS
    + SCHEMA_LOCK_WAIT_SECONDS
    + INFLUX_SYNC_REQUEST_ALLOWANCE * INFLUX_SYNC_SLOW_REQUEST_SECONDS
    + 30
)

DESIRED_RUNNING = "running"
DESIRED_STOPPED = "stopped"
DESIRED_UNAVAILABLE = "unavailable"

CONFIG_STATE_PENDING = "pending"
CONFIG_STATE_CURRENT = "current"
CONFIG_STATE_UNKNOWN = "unknown"

_FRACTION_RE = re.compile(r"(\.\d{1,6})\d*")

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


def _analytics_enabled(config):
    influx = config.get("influxdb") if isinstance(config, dict) else None
    return isinstance(influx, dict) and bool(influx.get("enabled"))


def influx_auto_sync_enabled(config):
    influx = config.get("influxdb") if isinstance(config, dict) else None
    if not isinstance(influx, dict):
        return False
    return influx.get("auto_sync") is not False


def _current_state(container):
    if not isinstance(container, dict):
        return {
            "found": False,
            "running": False,
            "status": "unknown",
            "name": None,
            "started_at": None,
        }
    status = container.get("status") or ("running" if container.get("running") else "missing")
    return {
        "found": bool(container.get("found")),
        "running": bool(container.get("running")),
        "status": str(status),
        "name": container.get("name"),
        "started_at": container.get("started_at"),
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


def _parse_timestamp(value):
    """Read one Docker/filesystem timestamp, or ``None`` if it proves nothing."""

    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = _FRACTION_RE.sub(r"\1", text, count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    # Docker reports 0001-01-01 for a container that has never run.
    return parsed if parsed.year >= 1970 else None


def _config_state(config_modified_at, ems_current):
    """Has the running EMS read the settings file as it is saved right now?

    EMS reads ``config.json`` when it starts, so a file written after that start
    cannot be live. Both observations have to be readable and EMS has to be
    running for either answer; anything else is ``unknown``, never a claim.
    """

    modified = _parse_timestamp(config_modified_at)
    started = _parse_timestamp((ems_current or {}).get("started_at"))
    if not (ems_current or {}).get("running") or modified is None or started is None:
        state = CONFIG_STATE_UNKNOWN
    elif modified > started:
        state = CONFIG_STATE_PENDING
    else:
        state = CONFIG_STATE_CURRENT
    return {
        "state": state,
        "config_modified_at": modified.isoformat() if modified else None,
        "ems_started_at": started.isoformat() if started else None,
    }


_EMS_RECREATE_REASONS = {
    CONFIG_STATE_PENDING: "The settings file changed after EMS started.",
    CONFIG_STATE_CURRENT: "EMS is running the settings file as it is saved.",
    CONFIG_STATE_UNKNOWN: (
        "Whether EMS is running the saved settings could not be determined."
    ),
}

_EMS_RECREATE_SUMMARIES = {
    CONFIG_STATE_PENDING: "EMS will be recreated so it reads the newer settings.",
    CONFIG_STATE_CURRENT: (
        "EMS will be recreated; it is already running the saved settings."
    ),
    CONFIG_STATE_UNKNOWN: "EMS will be recreated so it reads the saved settings.",
}


def _ems_action(desired, available, config_state=CONFIG_STATE_UNKNOWN):
    if not available:
        return ServiceAction(
            EMS_SERVICE, "unavailable", "EMS", _UNAVAILABLE_MESSAGE
        )
    if desired.desired == DESIRED_RUNNING:
        # Recreating is also the only restart an owner has here, so it stays on
        # offer whatever the verdict is; only the stated reason follows the facts.
        return ServiceAction(
            EMS_SERVICE,
            "recreate",
            "Recreate EMS",
            _EMS_RECREATE_REASONS[config_state],
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
        "Missing analytics buckets and downsampling tasks will be created; no data is deleted.",
    )


def _summary(actions, config_state=CONFIG_STATE_UNKNOWN):
    parts = []
    for action in actions:
        if action.action == "recreate" and action.service == EMS_SERVICE:
            parts.append(_EMS_RECREATE_SUMMARIES[config_state])
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

    config_state = _config_state(config_info.get("modified_at"), ems_current)
    ems_action = _ems_action(ems_desired, available, config_state["state"])
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
        "config_state": config_state,
        "actions": actions,
        "summary": _summary(
            [ems_action, influx_action, influx_sync_action], config_state["state"]
        ),
        "auto_init": influx_auto_init_enabled(config),
        "auto_sync": influx_auto_sync_enabled(config),
        "analytics_enabled": _analytics_enabled(config),
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
        list(SAFE_SYNC_COMMAND),
        timeout=INFLUX_SYNC_TIMEOUT_SECONDS,
    )


_INIT_WARNING = (
    "Could not initialise bundled InfluxDB secrets; Analytics stays unavailable "
    "until that works."
)
_START_WARNING = "Could not start bundled InfluxDB; Analytics stays unavailable until it runs."
_STOP_WARNING = "Could not stop bundled InfluxDB; it keeps running."
# Both the restart and the repair run the safe sync: an Admin button never
# deletes analytics data. A shorter retention or a retired task is applied by
# `emsctl influx sync`, which an operator runs on purpose.
SAFE_SYNC_COMMAND = ("python3", "emsctl.py", "influx", "sync", "--safe", "--json")
_EMSCTL_ERROR = re.compile(r'"error":\s*("(?:[^"\\]|\\.)*")')
_SYNC_WARNING = (
    "Could not sync the analytics schema. The EMS creates it by itself once "
    "InfluxDB answers; Repair analytics does it at once."
)


def _emsctl_error(detail):
    """The ``error`` field of an emsctl ``--json`` failure, from the output tail."""

    match = _EMSCTL_ERROR.search(detail or "")
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except ValueError:
        return None


def _repair_failure(detail, auto_sync):
    error = _emsctl_error(detail)
    message = (
        f"Could not create the analytics schema: {error.rstrip('.')}."
        if error
        else "Could not create the analytics schema."
    )
    if auto_sync:
        message += " The EMS keeps trying by itself."
    return message


def _with_leftover(message, exc):
    """Carry a container the failure could not clean up into the warning.

    The step detail is not rendered -- the page shows only the warning -- and a
    leftover container is the one part of the failure the operator has to act
    on: retrying beside it is what puts two schema syncs into InfluxDB at once.
    """

    if not getattr(exc, "leftover", None):
        return message

    return f"{message} {exc.message}"


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

    @staticmethod
    def _config_read_error(context):
        if not context.config_exists:
            return None
        try:
            parsed = json.loads(context.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            return f"config/config.json cannot be read: {exc}"
        if not isinstance(parsed, dict):
            return "config/config.json does not hold a JSON object."
        return None

    def plan(self):
        """The sync plan; nothing to run while the config cannot be read.

        Read as ``{}``, an unreadable config looked like "Analytics disabled":
        the sync stopped the bundled InfluxDB and recreated EMS against a file
        it could not parse.
        """

        context = self._install_context_provider()
        config = self._load_config(context)
        overview = self._overview_provider()
        plan = build_container_sync_plan(config, overview)
        config_error = self._config_read_error(context)
        if config_error is not None:
            plan["available"] = False
            plan["actions"] = []
            plan["message"] = (
                f"{config_error.rstrip('.')}. Fix the file or restore a backup; "
                "containers are left as they are."
            )
        return plan

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

        # Analytics is optional and the EMS controls the hardware without it, so
        # an InfluxDB step that fails is a warning: the EMS is restarted against
        # the applied config all the same, and it completes the schema itself
        # once InfluxDB answers (ems/history/schema_keeper.py).
        warnings = []

        # 1. Bundled InfluxDB secrets before the container is started. Without
        #    them InfluxDB is neither started nor synced.
        influx_ready = influx_desired_running
        if influx_desired_running and plan.get("auto_init"):
            step, warning = self._run_analytics_step(
                "init", lambda: self.run_influx_init(self.compose, workspace), _INIT_WARNING
            )
            steps.append(step)
            if warning:
                warnings.append(warning)
                influx_ready = False

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
                warnings.append(_STOP_WARNING)

        # 3. Start bundled InfluxDB if it is desired and not yet running.
        if influx_ready and influx_action is not None and influx_action["action"] == "start":
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
                warnings.append(_START_WARNING)
                influx_ready = False

        # 4. Reconcile the InfluxDB schema before EMS starts against it. The EMS
        #    Influx helper waits for readiness, so Admin does not poll here.
        if influx_ready and plan.get("auto_sync"):
            step, warning = self._run_analytics_step(
                "sync", lambda: self.run_influx_sync(self.compose, workspace), _SYNC_WARNING
            )
            steps.append(step)
            if warning:
                warnings.append(warning)

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
                return self._failure(
                    "Could not recreate the EMS container.", steps, plan, warnings=warnings
                )
        elif warnings:
            return self._failure("Analytics could not be set up.", steps, plan, warnings=warnings)

        result = {
            "ok": True,
            "status": "completed_with_warnings" if warnings else "completed",
            "plan": plan,
            "steps": steps,
            "overview": self._overview_provider(),
        }
        if warnings:
            result["warnings"] = warnings
        return result

    def repair_analytics(self):
        """Create the missing analytics schema now, on the operator's request.

        The running EMS does the same by itself while ``auto_sync`` is on; this
        is the way to do it at once, and the only way when it is off, which is
        why ``auto_sync`` does not gate it. It runs the safe sync, which deletes
        no data and leaves what an operator changed by hand, and it touches no
        container: a bundled InfluxDB that is not running is started by the
        ordinary restart, which syncs as well.
        """

        plan = self.plan()
        if not plan.get("available"):
            return {
                "ok": False,
                "status": "unavailable",
                "message": plan.get("message", _UNAVAILABLE_MESSAGE),
                "steps": [],
            }
        config = self._load_config(self._install_context_provider())
        if not _analytics_enabled(config):
            return {
                "ok": False,
                "status": "analytics_disabled",
                "message": "Analytics is switched off in the configuration; there is no schema to repair.",
                "steps": [],
            }
        bundled = config["influxdb"].get("mode", "bundled") == "bundled"
        if bundled and not plan["current"][INFLUX_SERVICE]["running"]:
            return {
                "ok": False,
                "status": "influx_not_running",
                "message": (
                    "Bundled InfluxDB is not running. Restart EMS now starts it "
                    "and creates the analytics schema."
                ),
                "steps": [],
            }
        step = {"service": INFLUX_SERVICE, "action": "repair", "status": "ok"}
        try:
            returncode, detail = self.run_influx_sync(self.compose, plan["install_root"])
        except Exception as exc:  # never leak a traceback to the UI
            message = _with_leftover(_repair_failure(None, plan.get("auto_sync")), exc)
            return {
                "ok": False,
                "status": "error",
                "message": message,
                "steps": [{**step, "status": "error", "detail": str(exc)}],
            }
        if returncode != 0:
            failed = {**step, "status": "error"}
            if detail:
                failed["detail"] = detail
            return {
                "ok": False,
                "status": "error",
                "message": _repair_failure(detail, plan.get("auto_sync")),
                "steps": [failed],
            }
        return {
            "ok": True,
            "status": "completed",
            "message": (
                "The analytics schema was checked and anything missing was created. "
                "Nothing was deleted, and what was changed by hand was left as it is; "
                "Run the checks shows anything still incomplete."
            ),
            "steps": [step],
        }

    def _run_analytics_step(self, action, run, warning):
        """Run an InfluxDB one-off; return its step and the warning, if it failed."""

        step = {"service": INFLUX_SERVICE, "action": action, "status": "ok"}
        try:
            returncode, detail = run()
        except Exception as exc:  # never leak a traceback to the UI
            return {**step, "status": "error", "detail": str(exc)}, _with_leftover(warning, exc)
        if returncode != 0:
            failed = {**step, "status": "error"}
            if detail:
                failed["detail"] = detail
            return failed, warning
        return step, None

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

    def _failure(self, message, steps, plan, step=None, detail=None, warnings=()):
        if step is not None:
            if detail:
                step = {**step, "detail": detail}
            steps.append(step)
        result = {
            "ok": False,
            "status": "error",
            "message": message,
            "plan": plan,
            "steps": steps,
        }
        if warnings:
            result["warnings"] = list(warnings)
        return result


def build_maintenance_container_plan():
    """Return the current read-only container sync plan (never mutates Docker)."""

    return MaintenanceContainerActions().plan()


# One container action at a time: a repair pressed while the restart syncs would
# put two schema syncs side by side, and both report into the same status line.
_CONTAINER_ACTION_LOCK = threading.Lock()
_BUSY_MESSAGE = "Another container action is still running; try again once it has finished."


def _one_at_a_time(run):
    if not _CONTAINER_ACTION_LOCK.acquire(blocking=False):
        return {"ok": False, "status": "busy", "message": _BUSY_MESSAGE, "steps": []}
    try:
        return run()
    finally:
        _CONTAINER_ACTION_LOCK.release()


def run_maintenance_container_sync():
    """Run the confirmed container sync and return its result."""

    return _one_at_a_time(lambda: MaintenanceContainerActions().sync())


def run_maintenance_analytics_repair():
    """Run the confirmed analytics schema repair and return its result."""

    return _one_at_a_time(lambda: MaintenanceContainerActions().repair_analytics())
