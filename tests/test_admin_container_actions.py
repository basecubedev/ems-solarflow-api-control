# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance container sync plan/action tests (no Docker required)."""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from admin.container_actions import (
    MaintenanceContainerActions,
    build_container_summary,
    build_container_sync_plan,
    build_ems_display_state,
    build_influx_display_state,
)
from admin.deployment import ONEOFF_TIMEOUT_SECONDS, DockerError

pytestmark = [
    pytest.mark.admin,
    pytest.mark.system_build,
    pytest.mark.integration,
    pytest.mark.simulation,
]


def make_overview(
    docker_available=True,
    config_exists=True,
    compose_exists=True,
    ems=None,
    influx=None,
    config_modified_at=None,
):
    return {
        "docker": {"available": docker_available},
        "paths": {
            "config": {
                "path": "/install/config/config.json",
                "exists": config_exists,
                "modified_at": config_modified_at,
            },
            "compose": {
                "path": "/install/docker-compose.yml",
                "exists": compose_exists,
            },
        },
        "containers": {
            "ems": ems
            or {
                "found": True,
                "running": True,
                "status": "running",
                "name": "ems-solarflow-api-control",
            },
            "influxdb": influx
            or {
                "found": False,
                "running": False,
                "status": "missing",
                "name": "ems-influxdb",
            },
        },
    }


def influx_config(enabled=True, mode="bundled", auto_init=True, auto_sync=True):
    return {
        "influxdb": {
            "enabled": enabled,
            "mode": mode,
            "auto_init": auto_init,
            "auto_sync": auto_sync,
        }
    }


# --- desired state --------------------------------------------------------


def test_ems_desired_running_when_config_and_compose_exist():
    plan = build_container_sync_plan({}, make_overview())
    assert plan["desired"]["ems"]["desired"] == "running"


def test_ems_unavailable_when_install_missing():
    plan = build_container_sync_plan(
        {}, make_overview(config_exists=False, compose_exists=False)
    )
    assert plan["desired"]["ems"]["desired"] == "unavailable"


def test_system_disabled_does_not_stop_ems():
    plan = build_container_sync_plan({"system": {"enabled": False}}, make_overview())
    assert plan["desired"]["ems"]["desired"] == "running"


def test_dashboard_disabled_does_not_stop_ems():
    plan = build_container_sync_plan({"dashboard": {"enabled": False}}, make_overview())
    assert plan["desired"]["ems"]["desired"] == "running"


def test_influx_bundled_enabled_desired_running():
    plan = build_container_sync_plan(influx_config(), make_overview())
    assert plan["desired"]["influxdb"]["desired"] == "running"


def test_influx_disabled_desired_stopped():
    plan = build_container_sync_plan(influx_config(enabled=False), make_overview())
    assert plan["desired"]["influxdb"]["desired"] == "stopped"


def test_influx_external_desired_stopped():
    plan = build_container_sync_plan(
        influx_config(mode="external"), make_overview()
    )
    assert plan["desired"]["influxdb"]["desired"] == "stopped"


def test_influx_missing_section_desired_stopped():
    plan = build_container_sync_plan({}, make_overview())
    assert plan["desired"]["influxdb"]["desired"] == "stopped"


# --- display state --------------------------------------------------------


RUNNING = {"found": True, "running": True, "status": "running", "name": "ems-influxdb"}
STOPPED = {"found": True, "running": False, "status": "exited", "name": "ems-influxdb"}
MISSING = {"found": False, "running": False, "status": "missing", "name": "ems-influxdb"}


def _services(plan):
    return plan["services"]


def test_influx_disabled_missing_reads_as_disabled_not_missing():
    plan = build_container_sync_plan(
        influx_config(enabled=False), make_overview(influx=MISSING)
    )
    influx = _services(plan)["influxdb"]
    assert influx["display_label"] == "Disabled"
    assert influx["display_state"] == "disabled"
    assert influx["desired_state"] == "stopped"
    assert influx["tone"] in ("muted", "info")
    assert influx["tone"] != "warn"
    assert "InfluxDB disabled" in plan["status_summary"]


def test_influx_disabled_running_still_reads_disabled_and_stops():
    plan = build_container_sync_plan(
        influx_config(enabled=False), make_overview(influx=RUNNING)
    )
    influx = _services(plan)["influxdb"]
    assert influx["display_label"] == "Disabled"
    assert influx["desired_state"] == "stopped"
    assert _actions(plan)["influxdb"]["action"] == "stop"
    assert "missing" not in plan["status_summary"]


def test_influx_bundled_missing_is_configured_but_missing_warn():
    plan = build_container_sync_plan(influx_config(), make_overview(influx=MISSING))
    influx = _services(plan)["influxdb"]
    assert influx["display_label"] == "Configured · missing"
    assert influx["display_state"] == "missing"
    assert influx["desired_state"] == "running"
    assert influx["tone"] == "warn"
    assert "InfluxDB configured but missing" in plan["status_summary"]


def test_influx_bundled_running_is_running_ok():
    plan = build_container_sync_plan(influx_config(), make_overview(influx=RUNNING))
    influx = _services(plan)["influxdb"]
    assert influx["display_label"] == "Running"
    assert influx["display_state"] == "running"
    assert influx["desired_state"] == "running"
    assert influx["tone"] == "ok"
    assert "InfluxDB running" in plan["status_summary"]


def test_influx_external_is_external_info():
    plan = build_container_sync_plan(
        influx_config(mode="external"), make_overview(influx=MISSING)
    )
    influx = _services(plan)["influxdb"]
    assert influx["display_label"] == "External"
    assert influx["display_state"] == "external"
    assert influx["desired_state"] == "stopped"
    assert influx["tone"] in ("info", "muted")
    assert "InfluxDB external" in plan["status_summary"]


def test_ems_configured_running():
    plan = build_container_sync_plan(influx_config(), make_overview())
    ems = _services(plan)["ems"]
    assert ems["display_label"] == "Running"
    assert ems["desired_state"] == "running"
    assert ems["tone"] == "ok"


def test_ems_configured_but_missing():
    ems = {"found": False, "running": False, "status": "missing", "name": "ems"}
    plan = build_container_sync_plan(influx_config(), make_overview(ems=ems))
    display = _services(plan)["ems"]
    assert display["display_label"] == "Configured · missing"
    assert display["desired_state"] == "running"
    assert "EMS configured but missing" in plan["status_summary"]


def test_ems_configured_but_stopped():
    ems = {"found": True, "running": False, "status": "exited", "name": "ems"}
    plan = build_container_sync_plan(influx_config(), make_overview(ems=ems))
    display = _services(plan)["ems"]
    assert display["display_label"] == "Configured · stopped"
    assert display["tone"] == "warn"


def test_ems_not_configured():
    plan = build_container_sync_plan(
        influx_config(), make_overview(config_exists=False, compose_exists=False)
    )
    ems = _services(plan)["ems"]
    assert ems["display_label"] == "Not configured"
    assert ems["desired_state"] == "stopped"
    assert ems["tone"] == "muted"
    assert "EMS not configured" in plan["status_summary"]


def test_display_helpers_are_pure():
    ems = build_ems_display_state(True, True, RUNNING, "running")
    influx = build_influx_display_state(influx_config(enabled=False), MISSING, "stopped")
    assert build_container_summary(ems, influx) == "EMS running · InfluxDB disabled"


# --- actions --------------------------------------------------------------


def _actions(plan):
    return {item["service"]: item for item in plan["actions"]}


def _action(plan, service, action):
    return next(
        (
            item
            for item in plan["actions"]
            if item["service"] == service and item["action"] == action
        ),
        None,
    )


def test_config_apply_context_produces_ems_recreate_action():
    plan = build_container_sync_plan(influx_config(enabled=False), make_overview())
    assert _actions(plan)["ems"]["action"] == "recreate"


def test_running_disabled_influx_produces_stop_action():
    influx = {"found": True, "running": True, "status": "running", "name": "ems-influxdb"}
    plan = build_container_sync_plan(
        influx_config(enabled=False), make_overview(influx=influx)
    )
    assert _actions(plan)["influxdb"]["action"] == "stop"


def test_missing_enabled_influx_produces_start_action():
    plan = build_container_sync_plan(influx_config(), make_overview())
    assert _action(plan, "influxdb", "start") is not None


def test_running_enabled_influx_produces_no_start_action():
    influx = {"found": True, "running": True, "status": "running", "name": "ems-influxdb"}
    plan = build_container_sync_plan(influx_config(), make_overview(influx=influx))
    # No start action when already running, but the schema sync still surfaces.
    assert _action(plan, "influxdb", "start") is None
    assert _action(plan, "influxdb", "sync") is not None


def test_bundled_enabled_produces_sync_action():
    plan = build_container_sync_plan(influx_config(), make_overview())
    assert _action(plan, "influxdb", "sync") is not None
    assert plan["auto_sync"] is True


def test_auto_sync_disabled_produces_no_sync_action():
    plan = build_container_sync_plan(
        influx_config(auto_sync=False), make_overview()
    )
    assert _action(plan, "influxdb", "sync") is None
    assert plan["auto_sync"] is False


def test_external_mode_produces_no_sync_action():
    plan = build_container_sync_plan(
        influx_config(mode="external"), make_overview()
    )
    assert _action(plan, "influxdb", "sync") is None


def test_docker_unavailable_produces_unavailable_plan():
    plan = build_container_sync_plan(
        influx_config(), make_overview(docker_available=False)
    )
    assert plan["available"] is False
    assert plan["actions"] == []
    assert "not available" in plan["message"]


# --- sync execution -------------------------------------------------------


@dataclass
class FakeContext:
    config_exists: bool
    config_path: Path


class FakeCompose:
    def __init__(self, fail=(), init_returncode=0, sync_returncode=0):
        self.calls = []
        self.fail = set(fail)
        self.init_returncode = init_returncode
        self.sync_returncode = sync_returncode

    def up(self, workspace, profiles=(), services=(), force_recreate=False, on_line=None):
        self.calls.append(
            ("up", tuple(profiles), tuple(services), force_recreate)
        )
        if "up" in self.fail or any(f"up:{service}" in self.fail for service in services):
            raise DockerError("compose_start_failed", "boom")

    def stop(self, workspace, services, profiles=(), on_line=None):
        self.calls.append(("stop", tuple(profiles), tuple(services)))
        if "stop" in self.fail:
            raise DockerError("docker_compose_stop_failed", "boom")

    def run_oneoff(
        self, workspace, service, command,
        timeout=ONEOFF_TIMEOUT_SECONDS, input_text=None,
    ):
        self.calls.append(("run_oneoff", service, tuple(command)))
        returncode = self.sync_returncode if "sync" in command else self.init_returncode
        return returncode, None


SYNC_WARNING = (
    "Could not sync the analytics schema. The EMS creates it by itself once "
    "InfluxDB answers; Repair analytics does it at once."
)


def make_actions(tmp_path, config, overview, compose, run_influx_init=None):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(config), encoding="utf-8")
    ctx = FakeContext(config_exists=True, config_path=cfg)
    # Point the workspace at a writable temp dir so the bundled-Influx start path
    # can create data/influxdb without touching a real install root.
    overview["paths"]["compose"]["path"] = str(tmp_path / "docker-compose.yml")
    overview["paths"]["config"]["path"] = str(cfg)
    return MaintenanceContainerActions(
        compose=compose,
        run_influx_init=run_influx_init,
        install_context_provider=lambda: ctx,
        overview_provider=lambda: overview,
    )


def _verbs(compose):
    return [call[0] for call in compose.calls]


def test_sync_recreates_ems_without_deleting_data(tmp_path):
    compose = FakeCompose()
    actions = make_actions(
        tmp_path, influx_config(enabled=False), make_overview(), compose
    )
    result = actions.sync()
    assert result["ok"] is True
    assert ("up", (), ("ems",), True) in compose.calls
    # No forbidden verbs are even reachable through the compose wrapper.
    assert set(_verbs(compose)) <= {"up", "stop", "run_oneoff"}


INIT_CMD = ("python3", "emsctl.py", "influx", "init", "--no-start", "--json")
SYNC_CMD = ("python3", "emsctl.py", "influx", "sync", "--safe", "--json")
REPAIR_CMD = SYNC_CMD


def test_sync_full_bundled_flow_init_start_sync_recreate(tmp_path):
    compose = FakeCompose()
    actions = make_actions(tmp_path, influx_config(), make_overview(), compose)
    result = actions.sync()
    assert result["ok"] is True
    # Influx is prepared, started and schema-synced before EMS is recreated.
    assert compose.calls == [
        ("run_oneoff", "ems", INIT_CMD),
        ("up", ("with-analytics",), ("influxdb",), False),
        ("run_oneoff", "ems", SYNC_CMD),
        ("up", (), ("ems",), True),
    ]
    step_pairs = [(s["service"], s["action"]) for s in result["steps"]]
    assert step_pairs == [
        ("influxdb", "init"),
        ("influxdb", "start"),
        ("influxdb", "sync"),
        ("ems", "recreate"),
    ]


def test_sync_running_influx_still_syncs_before_recreate(tmp_path):
    influx = {"found": True, "running": True, "status": "running", "name": "ems-influxdb"}
    compose = FakeCompose()
    actions = make_actions(
        tmp_path, influx_config(), make_overview(influx=influx), compose
    )
    result = actions.sync()
    assert result["ok"] is True
    # Already running: no start, but init + schema sync still run, then recreate.
    assert compose.calls == [
        ("run_oneoff", "ems", INIT_CMD),
        ("run_oneoff", "ems", SYNC_CMD),
        ("up", (), ("ems",), True),
    ]


def test_sync_skips_schema_sync_when_auto_sync_disabled(tmp_path):
    compose = FakeCompose()
    actions = make_actions(
        tmp_path, influx_config(auto_sync=False), make_overview(), compose
    )
    result = actions.sync()
    assert result["ok"] is True
    assert ("run_oneoff", "ems", SYNC_CMD) not in compose.calls
    assert ("up", (), ("ems",), True) in compose.calls


def test_sync_skips_init_when_auto_init_disabled(tmp_path):
    compose = FakeCompose()
    actions = make_actions(
        tmp_path, influx_config(auto_init=False), make_overview(), compose
    )
    result = actions.sync()
    assert result["ok"] is True
    assert ("run_oneoff", "ems", INIT_CMD) not in compose.calls
    # auto_sync stays on, so the schema is still reconciled.
    assert ("run_oneoff", "ems", SYNC_CMD) in compose.calls


def test_a_failed_influx_start_still_restarts_the_ems_and_names_analytics(tmp_path):
    """Analytics is optional; the EMS controls the hardware without it.

    A bundled InfluxDB that does not start leaves the schema sync nothing to
    talk to, so it is skipped, but it does not keep the EMS from restarting
    against the config the operator just applied.
    """

    compose = FakeCompose(fail=("up:influxdb",))
    actions = make_actions(tmp_path, influx_config(), make_overview(), compose)
    result = actions.sync()

    assert result["ok"] is True
    assert result["status"] == "completed_with_warnings"
    assert ("run_oneoff", "ems", SYNC_CMD) not in compose.calls
    assert compose.calls == [
        ("run_oneoff", "ems", INIT_CMD),
        ("up", ("with-analytics",), ("influxdb",), False),
        ("up", (), ("ems",), True),
    ]
    assert result["warnings"] == [
        "Could not start bundled InfluxDB; Analytics stays unavailable until it runs."
    ]


def test_a_failed_schema_sync_still_restarts_the_ems(tmp_path):
    """The sync used to stop the apply here, and the EMS kept running the old
    config -- or none at all on a fresh install. The EMS now completes the schema
    itself once InfluxDB answers, so a sync that fails is a warning."""

    compose = FakeCompose(sync_returncode=1)
    actions = make_actions(tmp_path, influx_config(), make_overview(), compose)
    result = actions.sync()

    assert result["ok"] is True
    assert result["status"] == "completed_with_warnings"
    assert ("up", (), ("ems",), True) in compose.calls
    assert {"service": "influxdb", "action": "sync", "status": "error"} in result["steps"]
    assert result["steps"][-1] == {"service": "ems", "action": "recreate", "status": "ok"}
    assert result["warnings"] == [SYNC_WARNING]


def test_a_container_left_behind_is_named_in_the_warning_not_only_the_step(tmp_path):
    """The page renders the warning and not the step detail.

    A leftover container is the one part of the failure the operator has to act
    on: retrying beside a sync still running puts two of them into InfluxDB at
    once. Hiding it in a field nothing shows is the same as not reporting it.
    """

    from admin.deployment import DockerError

    class LeavesAContainer(FakeCompose):
        def run_oneoff(
            self, workspace, service, command,
            timeout=ONEOFF_TIMEOUT_SECONDS, input_text=None,
        ):
            if "sync" in command:
                raise DockerError(
                    "docker_compose_run_timeout",
                    "The one-off container command did not finish within 330 "
                    "seconds. Its container ems-oneoff-abc123456789 could not "
                    "be removed and may still be running; remove it before "
                    "trying again.",
                    leftover="ems-oneoff-abc123456789",
                )
            return super().run_oneoff(workspace, service, command, timeout=timeout)

    actions = make_actions(
        tmp_path, influx_config(), make_overview(), LeavesAContainer()
    )
    result = actions.sync()

    [warning] = result["warnings"]
    assert "ems-oneoff-abc123456789" in warning
    assert SYNC_WARNING in warning


def test_a_failure_with_nothing_left_behind_keeps_the_plain_warning(tmp_path):
    """Widening every failure message would bury the one that needs acting on."""

    from admin.deployment import DockerError

    class PlainFailure(FakeCompose):
        def run_oneoff(
            self, workspace, service, command,
            timeout=ONEOFF_TIMEOUT_SECONDS, input_text=None,
        ):
            if "sync" in command:
                raise DockerError(
                    "docker_compose_run_failed",
                    "Could not run the one-off container command.",
                )
            return super().run_oneoff(workspace, service, command, timeout=timeout)

    actions = make_actions(tmp_path, influx_config(), make_overview(), PlainFailure())
    result = actions.sync()

    assert result["warnings"] == [SYNC_WARNING]


def test_a_sync_killed_by_its_own_ceiling_is_a_visible_warning(tmp_path):
    """Why the ceiling is sized for a slow sync rather than a stuck one.

    A stuck sync is killed, and what it leaves behind is a schema with buckets
    and no downsampling tasks. That trade is acceptable because it is neither
    silent nor final: the step is an error the page shows, and the EMS, which is
    restarted all the same, completes the schema itself.
    """

    import subprocess

    class Hanging(FakeCompose):
        def run_oneoff(
            self, workspace, service, command,
            timeout=ONEOFF_TIMEOUT_SECONDS, input_text=None,
        ):
            if "sync" in command:
                raise subprocess.TimeoutExpired(cmd=list(command), timeout=timeout)
            return super().run_oneoff(workspace, service, command, timeout=timeout)

    compose = Hanging()
    actions = make_actions(tmp_path, influx_config(), make_overview(), compose)
    result = actions.sync()

    assert result["ok"] is True
    assert result["warnings"] == [SYNC_WARNING]
    assert ("up", (), ("ems",), True) in compose.calls

    step = next(step for step in result["steps"] if step["action"] == "sync")
    assert step["service"] == "influxdb"
    assert step["status"] == "error"
    # What the real ComposeCli raises on a kill is pinned next to it, in
    # tests/test_admin_deployment.py; here it only has to reach the operator.
    assert step["detail"]


def test_an_ems_that_cannot_be_restarted_is_the_failure_and_analytics_rides_along(tmp_path):
    compose = FakeCompose(sync_returncode=1, fail=("up:ems",))
    actions = make_actions(tmp_path, influx_config(), make_overview(), compose)
    result = actions.sync()

    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["message"] == "Could not recreate the EMS container."
    assert result["warnings"] == [SYNC_WARNING]


def test_a_failed_stop_of_a_disabled_influxdb_still_restarts_the_ems(tmp_path):
    influx = {"found": True, "running": True, "status": "running", "name": "ems-influxdb"}
    compose = FakeCompose(fail=("stop",))
    actions = make_actions(
        tmp_path, influx_config(enabled=False), make_overview(influx=influx), compose
    )
    result = actions.sync()

    assert result["ok"] is True
    assert result["status"] == "completed_with_warnings"
    assert ("up", (), ("ems",), True) in compose.calls
    assert result["warnings"] == ["Could not stop bundled InfluxDB; it keeps running."]


def test_sync_stops_influx_when_disabled(tmp_path):
    influx = {"found": True, "running": True, "status": "running", "name": "ems-influxdb"}
    compose = FakeCompose()
    actions = make_actions(
        tmp_path,
        influx_config(enabled=False),
        make_overview(influx=influx),
        compose,
    )
    result = actions.sync()
    assert result["ok"] is True
    assert ("stop", ("with-analytics",), ("influxdb",)) in compose.calls
    assert "run_oneoff" not in _verbs(compose)


def test_sync_does_not_stop_ems_when_system_disabled(tmp_path):
    config = {"system": {"enabled": False}, **influx_config(enabled=False)}
    compose = FakeCompose()
    actions = make_actions(tmp_path, config, make_overview(), compose)
    actions.sync()
    # EMS is only recreated, never stopped.
    assert ("up", (), ("ems",), True) in compose.calls
    assert "stop" not in _verbs(compose)


def test_analytics_alone_failing_is_said_once_not_twice(tmp_path):
    overview = make_overview()
    overview["paths"]["config"]["exists"] = True
    compose = FakeCompose(sync_returncode=1)
    actions = make_actions(tmp_path, influx_config(), overview, compose)
    plan = actions.plan()
    plan["actions"] = [item for item in plan["actions"] if item["service"] != "ems"]
    actions.plan = lambda: plan

    result = actions.sync()

    assert result["ok"] is False
    assert result["message"] == "Analytics could not be set up."
    assert result["warnings"] == [SYNC_WARNING]


def test_failed_influx_init_skips_analytics_and_still_restarts_the_ems(tmp_path):
    compose = FakeCompose(init_returncode=1)
    actions = make_actions(tmp_path, influx_config(), make_overview(), compose)
    result = actions.sync()

    assert result["ok"] is True
    assert result["status"] == "completed_with_warnings"
    # No secrets, so InfluxDB is neither started nor synced; the EMS is.
    assert compose.calls == [
        ("run_oneoff", "ems", INIT_CMD),
        ("up", (), ("ems",), True),
    ]
    assert result["warnings"] == [
        "Could not initialise bundled InfluxDB secrets; Analytics stays "
        "unavailable until that works."
    ]


def test_sync_unavailable_when_docker_missing(tmp_path):
    compose = FakeCompose()
    actions = make_actions(
        tmp_path,
        influx_config(),
        make_overview(docker_available=False),
        compose,
    )
    result = actions.sync()
    assert result["ok"] is False
    assert result["status"] == "unavailable"
    assert compose.calls == []


# --- has the running EMS read the saved settings? -------------------------
# The plan used to answer "Config changed and EMS should reload it." whenever
# EMS was desired running — a claim about the past that nothing had checked, on
# a page whose job is to tell an owner what state their system is in.


def _running_ems(started_at=None):
    ems = {
        "found": True,
        "running": True,
        "status": "running",
        "name": "ems-solarflow-api-control",
    }
    if started_at is not None:
        ems["started_at"] = started_at
    return ems


def _ems_reason(plan):
    return _actions(plan)["ems"]["reason"]


def test_settings_saved_after_ems_started_are_reported_as_pending():
    plan = build_container_sync_plan(
        {},
        make_overview(
            ems=_running_ems("2026-09-09T10:00:00+00:00"),
            config_modified_at="2026-09-09T10:05:00+00:00",
        ),
    )
    assert plan["config_state"]["state"] == "pending"
    assert "changed after EMS started" in _ems_reason(plan)


def test_an_ems_started_after_the_last_save_is_not_told_it_missed_a_change():
    plan = build_container_sync_plan(
        {},
        make_overview(
            ems=_running_ems("2026-09-09T10:05:00+00:00"),
            config_modified_at="2026-09-09T10:00:00+00:00",
        ),
    )
    assert plan["config_state"]["state"] == "current"
    assert "changed" not in _ems_reason(plan)
    assert "changed" not in plan["summary"]


@pytest.mark.parametrize(
    "ems, config_modified_at",
    [
        (_running_ems(), "2026-09-09T10:00:00+00:00"),
        (_running_ems("2026-09-09T10:00:00+00:00"), None),
        (_running_ems("not a timestamp"), "2026-09-09T10:00:00+00:00"),
        (_running_ems("2026-09-09T10:00:00+00:00"), "not a timestamp"),
        (
            {
                "found": True,
                "running": False,
                "status": "exited",
                "name": "ems-solarflow-api-control",
            },
            "2026-09-09T10:00:00+00:00",
        ),
    ],
)
def test_an_unprovable_answer_is_unknown_and_never_a_claim(ems, config_modified_at):
    plan = build_container_sync_plan(
        {}, make_overview(ems=ems, config_modified_at=config_modified_at)
    )
    assert plan["config_state"]["state"] == "unknown"
    assert "changed" not in _ems_reason(plan)
    assert "changed" not in plan["summary"]


@pytest.mark.parametrize(
    "config_modified_at", ["2026-09-09T10:05:00+00:00", "2026-09-09T09:55:00+00:00", None]
)
def test_the_restart_stays_available_whatever_the_answer_is(config_modified_at):
    """Recreating EMS is also how an owner restarts it; no verdict removes that."""

    plan = build_container_sync_plan(
        {},
        make_overview(
            ems=_running_ems("2026-09-09T10:00:00+00:00"),
            config_modified_at=config_modified_at,
        ),
    )
    assert _actions(plan)["ems"]["action"] == "recreate"


def test_an_unreadable_config_runs_nothing_and_says_why(tmp_path):
    """Read as {}, a JSON typo looked like Analytics disabled and stopped InfluxDB."""

    influx = {"found": True, "running": True, "status": "running", "name": "ems-influxdb"}
    compose = FakeCompose()
    actions = make_actions(
        tmp_path, influx_config(), make_overview(influx=influx), compose
    )
    (tmp_path / "config.json").write_text('{"influxdb": {"enabled": true,}', encoding="utf-8")

    plan = actions.plan()
    result = actions.sync()

    assert plan["available"] is False
    assert plan["actions"] == []
    assert "config/config.json cannot be read" in plan["message"]
    assert result["ok"] is False
    assert compose.calls == []


# --- repair analytics -----------------------------------------------------

RUNNING_INFLUX = {"found": True, "running": True, "status": "running", "name": "ems-influxdb"}


def test_repair_runs_only_the_schema_sync_against_a_running_bundled_influxdb(tmp_path):
    compose = FakeCompose()
    actions = make_actions(
        tmp_path, influx_config(), make_overview(influx=RUNNING_INFLUX), compose
    )
    result = actions.repair_analytics()

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert compose.calls == [("run_oneoff", "ems", REPAIR_CMD)]
    assert result["steps"] == [{"service": "influxdb", "action": "repair", "status": "ok"}]
    assert "Nothing was deleted" in result["message"]


def test_no_admin_button_runs_a_sync_that_can_delete_data():
    """Restart EMS now says it deletes no data, and runs after every applied
    config. The full sync shortens a retention and drops what falls out of it,
    so an Admin button only ever runs the safe one."""

    from admin.container_actions import SAFE_SYNC_COMMAND

    assert "--safe" in SAFE_SYNC_COMMAND
    assert SYNC_CMD == SAFE_SYNC_COMMAND


def test_repair_runs_against_an_external_influxdb_too(tmp_path):
    compose = FakeCompose()
    actions = make_actions(
        tmp_path, influx_config(mode="external"), make_overview(), compose
    )
    result = actions.repair_analytics()

    assert result["ok"] is True
    assert compose.calls == [("run_oneoff", "ems", REPAIR_CMD)]


def test_repair_ignores_auto_sync_off_because_the_operator_asked(tmp_path):
    compose = FakeCompose()
    actions = make_actions(
        tmp_path,
        influx_config(auto_sync=False),
        make_overview(influx=RUNNING_INFLUX),
        compose,
    )

    assert actions.repair_analytics()["ok"] is True
    assert compose.calls == [("run_oneoff", "ems", REPAIR_CMD)]


def test_repair_of_a_stopped_bundled_influxdb_points_to_the_restart(tmp_path):
    compose = FakeCompose()
    actions = make_actions(tmp_path, influx_config(), make_overview(), compose)
    result = actions.repair_analytics()

    assert result["ok"] is False
    assert result["status"] == "influx_not_running"
    assert "Restart EMS now" in result["message"]
    assert compose.calls == []


def test_repair_with_analytics_off_runs_nothing(tmp_path):
    compose = FakeCompose()
    actions = make_actions(
        tmp_path, influx_config(enabled=False), make_overview(influx=RUNNING_INFLUX), compose
    )
    result = actions.repair_analytics()

    assert result["ok"] is False
    assert result["status"] == "analytics_disabled"
    assert compose.calls == []


class FailingRepair(FakeCompose):
    """An emsctl sync that exits 1 with its --json error in the output tail."""

    def __init__(self, error):
        super().__init__()
        self.error = error

    def run_oneoff(
        self, workspace, service, command,
        timeout=ONEOFF_TIMEOUT_SECONDS, input_text=None,
    ):
        self.calls.append(("run_oneoff", service, tuple(command)))
        tail = json.dumps({"action": "sync", "error": self.error, "ok": False}, indent=2)
        return 1, tail


def test_a_failed_repair_names_the_cause_and_what_happens_next(tmp_path):
    compose = FailingRepair("influx sync failed: 401 Client Error: Unauthorized")
    actions = make_actions(
        tmp_path, influx_config(), make_overview(influx=RUNNING_INFLUX), compose
    )
    result = actions.repair_analytics()

    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["message"] == (
        "Could not create the analytics schema: influx sync failed: 401 Client "
        "Error: Unauthorized. The EMS keeps trying by itself."
    )
    assert result["steps"][0]["status"] == "error"


def test_a_failed_repair_with_auto_sync_off_promises_no_retry(tmp_path):
    compose = FailingRepair("no InfluxDB token")
    actions = make_actions(
        tmp_path,
        influx_config(auto_sync=False),
        make_overview(influx=RUNNING_INFLUX),
        compose,
    )

    assert actions.repair_analytics()["message"] == (
        "Could not create the analytics schema: no InfluxDB token."
    )


def test_a_repair_pressed_while_the_restart_runs_waits_its_turn(monkeypatch):
    """Protected interleaving: Restart EMS now holds the container-action lock
    (it is mid-sync) when Repair analytics arrives. The repair runs nothing and
    says so, instead of starting a second schema sync beside the first."""

    from admin import container_actions

    constructed = []

    class Recording:
        def __init__(self):
            constructed.append(self)

        def repair_analytics(self):
            return {"ok": True, "status": "completed"}

    monkeypatch.setattr(container_actions, "MaintenanceContainerActions", Recording)

    with container_actions._CONTAINER_ACTION_LOCK:
        busy = container_actions.run_maintenance_analytics_repair()
        also_busy = container_actions.run_maintenance_container_sync()

    assert busy["status"] == "busy"
    assert also_busy["status"] == "busy"
    assert constructed == []
    assert container_actions.run_maintenance_analytics_repair()["ok"] is True


def test_repair_is_unavailable_without_docker(tmp_path):
    compose = FakeCompose()
    actions = make_actions(
        tmp_path, influx_config(), make_overview(docker_available=False), compose
    )
    result = actions.repair_analytics()

    assert result["ok"] is False
    assert result["status"] == "unavailable"
    assert compose.calls == []
