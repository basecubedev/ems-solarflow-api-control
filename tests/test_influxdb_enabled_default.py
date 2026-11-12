# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract: every reader of ``influxdb.enabled`` answers as the EMS runs it.

The EMS merges the release defaults under config.json, so a missing key means
analytics on. The Admin's container plan, its status display, the backup and
the dashboard read the raw file; they used to take a missing key for off, and
one screen said "Analytics: On" next to "Analytics database: Off".
"""

import ast
import copy
import json
from pathlib import Path

import pytest

from admin import config_preview, container_actions, maintenance_config
from admin.deployment import DeploymentService
from dashboard import maintenance as dashboard_maintenance
from ems import backup
from ems.config import (
    apply_runtime_config_defaults,
    normalize_influxdb_config,
    resolve_influxdb_config,
)
from ems.influx_setup import DOCKER_FIRST_SECRET_FILE

pytestmark = [
    pytest.mark.config,
    pytest.mark.admin,
    pytest.mark.backup_restore,
    pytest.mark.contract,
]

ROOT = Path(__file__).resolve().parents[1]

CONFIGS = {
    "no_block": {},
    "empty_block": {"influxdb": {}},
    "block_without_key": {"influxdb": {"mode": "bundled", "org": "ems"}},
    "external_without_key": {"influxdb": {"mode": "external"}},
    "enabled": {"influxdb": {"enabled": True}},
    "disabled": {"influxdb": {"enabled": False}},
    "disabled_text": {"influxdb": {"enabled": "false"}},
    "enabled_null": {"influxdb": {"enabled": None}},
    "block_null": {"influxdb": None},
    "block_text": {"influxdb": "off"},
}


def _as_the_ems_runs_it(config):
    return normalize_influxdb_config(apply_runtime_config_defaults(config)["influxdb"])


@pytest.fixture(params=sorted(CONFIGS))
def case(request):
    config = CONFIGS[request.param]
    return copy.deepcopy(config), _as_the_ems_runs_it(copy.deepcopy(config))


def test_the_resolver_is_the_block_the_ems_runs(case):
    config, ems = case
    assert resolve_influxdb_config(config) == ems


def test_a_missing_key_means_analytics_on():
    for name in ("no_block", "empty_block", "block_without_key"):
        assert _as_the_ems_runs_it(CONFIGS[name])["enabled"] is True, name


def test_admin_container_plan_follows_the_ems(case):
    config, ems = case
    bundled = ems["enabled"] and ems["mode"] == "bundled"
    desired = container_actions._influx_desired(config).desired
    assert desired == ("running" if bundled else "stopped")
    assert container_actions._analytics_enabled(config) is ems["enabled"]
    assert container_actions.influx_auto_init_enabled(config) is ems["auto_init"]
    assert container_actions.influx_auto_sync_enabled(config) is ems["auto_sync"]


def test_admin_status_display_follows_the_ems(case):
    config, ems = case
    display = container_actions.build_influx_display_state(
        config, {"found": False, "running": False, "status": "missing"}, None
    )
    if not ems["enabled"]:
        assert display["feature_state"] == "disabled"
    else:
        assert display["feature_state"] == ems["mode"]


def test_admin_maintenance_summary_follows_the_ems(case):
    config, ems = case
    summary = maintenance_config._summary(config, {"devices": []})
    assert summary["influx_enabled"] is ems["enabled"]
    assert summary["influx_mode"] == ems["mode"]


def test_admin_deployment_plan_follows_the_ems(case):
    config, ems = case
    plan = DeploymentService._influx_plan(None, None, {"config": config, "ready": True})
    assert plan["enabled"] is ems["enabled"]
    assert plan["bundled"] is (ems["enabled"] and ems["mode"] == "bundled")


def test_admin_config_preview_moves_the_secret_as_the_ems_reads_it(case):
    config, ems = case
    config_preview._normalize_bundled_influx_secret(config)
    moved = isinstance(config.get("influxdb"), dict) and (
        config["influxdb"].get("secret_file") == DOCKER_FIRST_SECRET_FILE
    )
    has_block = isinstance(config.get("influxdb"), dict)
    assert moved is (has_block and ems["enabled"] and ems["mode"] == "bundled")


def test_backup_and_dashboard_follow_the_ems(case, tmp_path):
    config, ems = case
    assert backup.detect_influxdb_status(config)["enabled"] is ems["enabled"]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    status = dashboard_maintenance.maintenance_status(str(config_path), str(tmp_path))
    assert status["influxdb"]["enabled"] is ems["enabled"]
    assert status["influxdb"]["mode"] == ems["mode"]


PRODUCTION = ("admin", "dashboard", "ems", "mqtt_mapping", "scripts", "tools", "emsctl.py")


def _production_files():
    for entry in PRODUCTION:
        path = ROOT / entry
        yield from ([path] if path.is_file() else sorted(path.rglob("*.py")))


def test_no_reader_normalizes_a_raw_influxdb_block():
    """Only the resolver may hand a block to the normalizer: a raw block without
    the key would read as off again."""

    callers = []
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == "normalize_influxdb_config":
                callers.append(str(path.relative_to(ROOT)))
    assert set(callers) == {"ems/config.py"}


RAW_BLOCK_EXCEPTIONS = {
    ("admin/config_preview.py", "_normalize_bundled_influx_secret"):
        "writes the Docker secret path into the block; reads the switch through the resolver",
    ("admin/maintenance_config.py", "_feature_draft"):
        "asks whether the file spells the switch so that an editor would misread it",
    ("ems/config_catalog.py", "render_default_template"): "the release template, not a config",
    ("ems/config_init.py", "apply_system_basics"): "the init wizard writes the block",
    ("ems/config_init.py", "apply_analytics"): "the init wizard writes the block",
}


def _enclosing_functions(tree):
    owner = {}

    def visit(node, function):
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            owner[child] = inner
            visit(child, inner)

    visit(tree, None)
    return owner


def _raw_block_reads():
    for path in _production_files():
        relative = str(path.relative_to(ROOT))
        if relative == "ems/config.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript):
                key, base = node.slice, node.value
            elif isinstance(node, ast.Call) and getattr(node.func, "attr", None) in (
                "get", "setdefault", "pop",
            ) and node.args:
                key, base = node.args[0], node.func.value
            else:
                continue
            if not (isinstance(key, ast.Constant) and key.value == "influxdb"):
                continue
            if getattr(base, "id", None) == "manifest":
                continue
            yield relative, owner.get(node)


def test_no_reader_reads_the_raw_influxdb_block():
    """A raw read takes a missing key for off; every read goes through the resolver."""

    reads = set(_raw_block_reads())
    assert sorted(read for read in reads if read not in RAW_BLOCK_EXCEPTIONS) == []
    assert sorted(set(RAW_BLOCK_EXCEPTIONS) - reads) == []


def test_no_config_file_configures_no_analytics():
    assert resolve_influxdb_config(None)["enabled"] is False
    overview = {
        "paths": {"config": {"exists": False}, "compose": {"exists": True, "path": "/x/docker-compose.yml"}},
        "docker": {"available": True},
        "containers": {},
    }
    plan = container_actions.build_container_sync_plan(None, overview)
    assert plan["desired"]["influxdb"]["desired"] == "stopped"
    assert plan["analytics_enabled"] is False
    assert not [action for action in plan["actions"] if action["service"] == "influxdb"]


def _overview(running=False):
    return {
        "paths": {"config": {"exists": True}, "compose": {"exists": True, "path": "/x/docker-compose.yml"}},
        "docker": {"available": True},
        "containers": {"influxdb": {"found": running, "running": running, "status": "running" if running else "missing"}},
    }


def test_analytics_without_a_named_secret_file_is_not_set_up_here():
    """Without one, the setup writes the image's default path, which no container reads."""

    plan = container_actions.build_container_sync_plan({"devices": []}, _overview())
    assert plan["desired"]["influxdb"]["desired"] == "running"
    assert plan["influx_setup_ready"] is False
    assert not [action for action in plan["actions"] if action["service"] == "influxdb"]
    assert plan["services"]["influxdb"]["display_label"] == "Configured · not set up"

    named = {"influxdb": {"secret_file": DOCKER_FIRST_SECRET_FILE}}
    plan = container_actions.build_container_sync_plan(named, _overview())
    assert plan["influx_setup_ready"] is True
    assert {action["action"] for action in plan["actions"] if action["service"] == "influxdb"} == {"start", "sync"}


def test_repair_reads_the_mode_through_the_resolver(tmp_path):
    from types import SimpleNamespace

    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    context = SimpleNamespace(config_exists=True, config_path=tmp_path / "config.json")
    actions = container_actions.MaintenanceContainerActions(
        compose=object(),
        install_context_provider=lambda: context,
        overview_provider=lambda: _overview(running=False),
    )
    result = actions.repair_analytics()
    assert result["status"] == "influx_secret_misplaced"

    (tmp_path / "config.json").write_text(
        json.dumps({"influxdb": {"mode": "BUNDLED", "secret_file": DOCKER_FIRST_SECRET_FILE}}),
        encoding="utf-8",
    )
    assert actions.repair_analytics()["status"] == "influx_not_running"


def test_an_unchanged_settings_draft_writes_no_analytics_switch():
    """The draft goes back on every apply; a switch it invents is a change nobody made."""

    assert "influxdb.enabled" not in maintenance_config._feature_draft({"devices": []})
    assert "influxdb.enabled" not in maintenance_config._feature_draft({"influxdb": {}})
    assert maintenance_config._feature_draft({"influxdb": {"enabled": True}})["influxdb.enabled"] is True
    for misread in ({"influxdb": {"enabled": "false"}}, {"influxdb": {"enabled": None}}, {"influxdb": None}):
        assert maintenance_config._feature_draft(misread)["influxdb.enabled"] is False


def test_the_secret_less_state_has_its_own_display_code():
    display = container_actions.build_influx_display_state(
        {"devices": []}, {"found": False, "running": False, "status": "missing"}, "running"
    )
    assert (display["display_state"], display["container_state"]) == ("secret_misplaced", "missing")


@pytest.mark.parametrize(
    "config, features, expected",
    [
        (
            {"influxdb": {"enabled": False, "secret_file": "deploy/docker/influxdb.env"}},
            {"influxdb.enabled": True},
            DOCKER_FIRST_SECRET_FILE,
        ),
        ({"devices": []}, {"influxdb.enabled": True}, "deploy/docker/influxdb.env"),
        (
            {"influxdb": {"enabled": True, "secret_file": "deploy/docker/influxdb.env"}},
            {"influxdb.enabled": True},
            "deploy/docker/influxdb.env",
        ),
        (
            {"influxdb": {"enabled": True, "secret_file": "deploy/docker/influxdb.env"}},
            {},
            "deploy/docker/influxdb.env",
        ),
        (
            {"influxdb": {"enabled": False, "mode": "external", "secret_file": "x.env"}},
            {"influxdb.enabled": True},
            "x.env",
        ),
    ],
)
def test_switching_analytics_on_moves_the_bundled_secret_where_docker_reads_it(
    config, features, expected
):
    """Only the apply that switches analytics on; an apply that switches nothing writes nothing."""

    merged = copy.deepcopy(config)
    maintenance_config._merge_features(merged, features, [])
    assert resolve_influxdb_config(merged)["secret_file"] == expected


def test_off_then_on_moves_the_secret_of_a_config_that_had_analytics_by_default():
    merged = {"devices": []}
    maintenance_config._merge_features(merged, {"influxdb.enabled": False}, [])
    maintenance_config._merge_features(merged, {"influxdb.enabled": True}, [])
    assert container_actions.influx_setup_ready(merged) is True
