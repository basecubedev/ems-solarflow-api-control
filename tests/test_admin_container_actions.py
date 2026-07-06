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
from admin.deployment import DockerError

pytestmark = pytest.mark.simulation


def make_overview(
    docker_available=True,
    config_exists=True,
    compose_exists=True,
    ems=None,
    influx=None,
):
    return {
        "docker": {"available": docker_available},
        "paths": {
            "config": {
                "path": "/install/config/config.json",
                "exists": config_exists,
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
        if "up" in self.fail:
            raise DockerError("compose_start_failed", "boom")

    def stop(self, workspace, services, profiles=(), on_line=None):
        self.calls.append(("stop", tuple(profiles), tuple(services)))
        if "stop" in self.fail:
            raise DockerError("docker_compose_stop_failed", "boom")

    def run_oneoff(self, workspace, service, command, timeout=180):
        self.calls.append(("run_oneoff", service, tuple(command)))
        returncode = self.sync_returncode if "sync" in command else self.init_returncode
        return returncode, None


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
SYNC_CMD = ("python3", "emsctl.py", "influx", "sync", "--json")


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


def test_failed_influx_start_aborts_before_sync_and_recreate(tmp_path):
    compose = FakeCompose(fail=("up",))
    actions = make_actions(tmp_path, influx_config(), make_overview(), compose)
    result = actions.sync()
    assert result["ok"] is False
    assert ("run_oneoff", "ems", SYNC_CMD) not in compose.calls
    # Only the failing influx start up-call ran; EMS was never recreated.
    assert compose.calls == [
        ("run_oneoff", "ems", INIT_CMD),
        ("up", ("with-analytics",), ("influxdb",), False),
    ]


def test_failed_influx_sync_aborts_before_ems_recreate(tmp_path):
    compose = FakeCompose(sync_returncode=1)
    actions = make_actions(tmp_path, influx_config(), make_overview(), compose)
    result = actions.sync()
    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["message"] == "Could not sync InfluxDB analytics schema."
    # EMS is not recreated when the schema sync fails.
    assert ("up", (), ("ems",), True) not in compose.calls
    assert result["steps"][-1] == {
        "service": "influxdb",
        "action": "sync",
        "status": "error",
    }


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


def test_failed_influx_init_stops_sync_before_compose_up(tmp_path):
    compose = FakeCompose(init_returncode=1)
    actions = make_actions(tmp_path, influx_config(), make_overview(), compose)
    result = actions.sync()
    assert result["ok"] is False
    assert result["status"] == "error"
    # Init failed, so nothing was started or recreated.
    assert "up" not in _verbs(compose)


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
