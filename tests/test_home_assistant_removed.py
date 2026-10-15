# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Home Assistant integration is removed (HA-R).

Owner decision 2026-10-12: status publishing and helper control are gone, and
Home Assistant is offered nowhere. What an installation that used it still
carries keeps working: an ``ha`` section in ``config.json`` loads and is
ignored, whatever its content, and neither a config upgrade nor a Maintenance
apply adds or drops one; ``ha`` fields in ``runtime-state.json`` load and stay
as any key the EMS does not know; the dashboard refuses an ``ha`` section like
any unknown one; ``emsctl ha`` and ``ha-control`` are invalid choices; and a
token left in an old ``ha`` section stays redacted in a support bundle.
"""

import importlib.util
import inspect
import json
import logging
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import emsctl
from admin.config_runtime_overlap import applies_live
from admin.maintenance_config import (
    load_maintenance_config,
    prepare_maintenance_config_apply,
    redact_config_for_browser,
)
from dashboard.auth import write_password_file
from dashboard.runtime_write import (
    SECTION_FIELDS,
    RuntimeWriteError,
    apply_section_update,
)
from ems import clients
from ems import config as cfg
from ems import simulation
from ems.config_catalog import (
    GRID_METER_VARIANTS,
    build_default_template,
    get_config_feature_field_index,
    get_config_feature_sections,
)
from ems.controller import EMSController
from ems.runtime_state import RuntimeState, build_runtime_defaults
from tests._emsctl_test_helpers import run_emsctl, write_config
from tests.test_dashboard_frontend_xss import APP_JS, run_node
from tests.test_dashboard_server import StoreStub, json_response, with_server

pytestmark = [
    pytest.mark.contract,
    pytest.mark.config,
]

ROOT = Path(__file__).resolve().parents[1]
OLD_SECTION = {
    "enabled": True,
    "control_enabled": True,
    "url": "http://ha-host.lan:8123",
    "token": "old-ha-token-that-must-stay-secret",
}


def test_home_assistant_is_offered_nowhere():
    template = json.loads((ROOT / "config" / "config.template.json").read_text())
    grid_meter_types = get_config_feature_field_index()["grid_meter.type"]["options"]

    assert "ha" not in template
    assert "ha" not in build_default_template()
    assert "ha" not in cfg.default_safe_config()
    assert not [path for path in get_config_feature_field_index() if path.startswith("ha.")]
    assert "ha" not in {section["id"] for section in get_config_feature_sections()}
    assert "ha" not in GRID_METER_VARIANTS
    assert "ha" not in grid_meter_types
    assert "ha" not in cfg.GRID_METER_MODEL_TRANSPORTS
    assert not [name for name in dir(cfg) if name.startswith("HA_")]
    assert not hasattr(clients, "HAClient")
    assert not hasattr(simulation, "SimulatedHAClient")
    assert "ha" not in inspect.signature(simulation.run_live_preflight).parameters
    assert "ha" not in inspect.signature(EMSController).parameters
    assert not {
        "publish_to_ha", "publish_winter_to_ha", "publish_sensor", "device_ha_extra",
        "sync_ha_runtime_state", "ha_update_runtime_field", "runtime_ha_enabled",
        "runtime_ha_control_enabled",
    } & set(dir(EMSController))
    assert "ha" not in SECTION_FIELDS
    assert not {"ha", "ha-control"} & set(emsctl.TOP_LEVEL_COMMANDS)


def _entry_script():
    spec = importlib.util.spec_from_file_location(
        "ems_entry_script", ROOT / "ems-solarflow-api-control.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_ems_takes_no_home_assistant_flag(capsys):
    entry = _entry_script()

    with patch.object(sys, "argv", ["ems-solarflow-api-control.py", "--no-ha"]):
        with pytest.raises(SystemExit) as stopped:
            entry.parse_args()

    assert stopped.value.code == 2
    assert "unrecognized arguments: --no-ha" in capsys.readouterr().err


@pytest.mark.parametrize(
    "section",
    [
        OLD_SECTION,
        {"enabled": True, "url": "http://homeassistant.local:8123", "token": "YOUR_TOKEN_HERE"},
        {"enabled": "yes", "control_enabled": None},
        "ha",
        None,
    ],
    ids=["configured", "placeholder", "junk-values", "string", "null"],
)
def test_a_config_with_an_ha_section_loads_and_starts_unchanged(
    tmp_path, capsys, caplog, section
):
    """A placeholder token in an enabled section used to hold the whole EMS in safe mode."""

    def loaded(with_section):
        config = json.loads((ROOT / "config" / "config.template.json").read_text())
        config.pop("ha", None)
        config["config_upgrade"]["on_startup"] = "check"
        config["grid_meter"]["ip"] = "192.168.1.50"
        config["devices"] = [{"name": "WR1", "ip": "192.168.1.20", "sn": "SN-HA-R-0001"}]
        if with_section:
            config["ha"] = section
        (tmp_path / "config").mkdir(exist_ok=True)
        shutil.copy(
            ROOT / "config" / "config.template.json",
            tmp_path / "config" / "config.template.json",
        )
        path = tmp_path / ("with.json" if with_section else "without.json")
        path.write_text(json.dumps(config))
        before = path.read_bytes()
        args = SimpleNamespace(
            config=str(path), dry_run=False, simulate=False, replay=None,
            self_test=False,
        )
        snapshot = {name: getattr(cfg, name) for name in dir(cfg) if name.isupper()}
        try:
            cfg.initialize(args, str(tmp_path))
            result = {
                name: getattr(cfg, name)
                for name in dir(cfg)
                if name.isupper() and name not in ("ARGS", "CONFIG")
            }
            result["CONFIG"] = {
                key: value for key, value in cfg.CONFIG.items() if key != "ha"
            }
        finally:
            for name, saved in snapshot.items():
                setattr(cfg, name, saved)
        assert path.read_bytes() == before
        return result

    with caplog.at_level(logging.DEBUG):
        assert loaded(True) == loaded(False)

    assert "ha." not in capsys.readouterr().err
    assert "ha." not in caplog.text


def test_config_upgrade_adds_no_ha_section_and_leaves_an_old_one_as_it_is():
    without = cfg.build_config_upgrade_plan({"system": {"enabled": True}}, str(ROOT))
    old = {"system": {"enabled": True}, "ha": dict(OLD_SECTION, custom="kept")}
    upgraded = cfg.build_config_upgrade_plan(old, str(ROOT))

    assert "ha" not in without["upgraded_config"]
    assert upgraded["upgraded_config"]["ha"] == old["ha"]
    for plan in (without, upgraded):
        touched = plan["add"] + plan["comment_add"] + plan["comment_refresh"] + plan["migrate"]
        assert not [item for item in touched if str(item.get("path", "")).startswith("ha")]


def test_a_maintenance_apply_keeps_an_old_ha_section_and_writes_no_stale_toggle(
    tmp_path, isolated_install_root
):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({
        "system": {"max_total_power": 1600},
        "devices": [{"name": "WR1", "ip": "192.168.1.20", "sn": "AAA", "max_power": 800}],
        "grid_meter": {"type": "shelly", "ip": "192.168.1.50"},
        "ha": OLD_SECTION,
    }))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["devices"][0]["max_power"] = 600
    draft["features"]["ha.enabled"] = False

    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )

    assert prepared["status"] == "ok"
    payload = json.loads(prepared["payload"])
    assert payload["devices"][0]["max_power"] == 600
    assert payload["ha"] == OLD_SECTION
    assert not applies_live("ha.enabled")
    assert not applies_live("ha.control_enabled")


def _device():
    return SimpleNamespace(
        name="WR1", sn="SN-HA-R-0001", max_power=800, grid_off_mode=None,
        pv_priority_factor=1.0, ac_charge_enabled=True,
    )


def test_a_runtime_state_with_ha_fields_loads_and_keeps_them(tmp_path):
    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({
        "system": {"enabled": True, "max_total_power": 800, "loop_interval": 5,
                   "min_output_limit": 35},
        "ha": {"enabled": True, "control_enabled": True},
        "winter": {"enabled": True},
        "devices": {"WR1": {"enabled": True, "identity": "SN-HA-R-0001"}},
    }))

    with patch.object(cfg, "CONFIG", {"ha": dict(OLD_SECTION)}):
        defaults = build_runtime_defaults([_device()])
    state = RuntimeState(str(path), defaults)
    state.load_or_create()
    state.update_section("winter", {"enabled": False})

    assert "ha" not in defaults
    saved = json.loads(path.read_text())
    assert saved["ha"] == {"enabled": True, "control_enabled": True}
    assert saved["winter"] == {"enabled": False}


def test_emsctl_creates_a_runtime_state_without_ha_fields(tmp_path):
    result = run_emsctl(tmp_path, "status")

    assert result.returncode == 0, result.stderr
    assert "ha" not in json.loads((tmp_path / "runtime-state.json").read_text())


def _runtime_file(tmp_path):
    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({
        "system": {"enabled": True, "max_total_power": 800, "loop_interval": 5,
                   "min_output_limit": 35},
        "ha": {"enabled": False, "control_enabled": False},
        "winter": {"enabled": True},
        "devices": {},
    }))
    state = RuntimeState(str(path), {})
    state.load_or_create()
    return path, state


@pytest.mark.parametrize("section", ["ha", "bogus"])
def test_the_dashboard_write_refuses_an_ha_section_like_any_unknown_one(tmp_path, section):
    path, state = _runtime_file(tmp_path)
    before = path.read_bytes()

    with pytest.raises(RuntimeWriteError, match=f"unsupported runtime section {section}"):
        apply_section_update(state, section, {"enabled": True})

    assert path.read_bytes() == before


def test_the_dashboard_route_answers_an_ha_section_as_it_answers_an_unknown_one(tmp_path):
    path, state = _runtime_file(tmp_path)
    before = path.read_bytes()
    auth_file = tmp_path / "dashboard-auth.json"
    write_password_file(auth_file, "secret-password")
    server, base_url = with_server(StoreStub(), runtime_state=state, auth_file=str(auth_file))

    try:
        _, login_headers, login = json_response(
            f"{base_url}/api/auth/login", method="POST",
            payload={"password": "secret-password"},
        )
        headers = {"Cookie": login_headers["Set-Cookie"], "X-CSRF-Token": login["csrf_token"]}
        answers = {
            section: json_response(
                f"{base_url}/api/runtime/{section}", method="PATCH",
                payload={"enabled": True}, headers=headers,
            )
            for section in ("ha", "bogus")
        }
    finally:
        server.shutdown()
        server.server_close()

    status, _, body = answers["ha"]
    assert (status, body) == (answers["bogus"][0], answers["bogus"][2])
    assert status == 404
    assert path.read_bytes() == before


@pytest.mark.parametrize("command", ["ha", "ha-control"])
def test_emsctl_names_a_removed_command_an_invalid_choice(tmp_path, command):
    result = run_emsctl(tmp_path, command, "enable")

    assert result.returncode == 2
    assert f"invalid choice: '{command}'" in result.stderr
    assert not (tmp_path / "runtime-state.json").exists()


def test_emsctl_help_completion_and_menu_name_no_home_assistant(tmp_path):
    outputs = [
        run_emsctl(tmp_path, "--help").stdout,
        run_emsctl(tmp_path, "examples").stdout,
        run_emsctl(tmp_path, "completion", "bash").stdout,
        run_emsctl(tmp_path, "completion", "zsh").stdout,
        run_emsctl(tmp_path, "interactive", input_text="quit\n").stdout,
    ]

    for output in outputs:
        assert output
        assert "Home Assistant" not in output
        assert " ha " not in output and "ha-control" not in output
        assert "HA " not in output


def test_diagnose_checks_nothing_of_an_old_ha_section(tmp_path):
    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    config["ha"] = {"enabled": False, "control_enabled": True, "url": "not a url"}
    config_path.write_text(json.dumps(config))

    result = run_emsctl(tmp_path, "diagnose", "--json")

    assert result.returncode == 0, result.stderr
    codes = [check["code"] for check in json.loads(result.stdout)["checks"]]
    assert not [code for code in codes if code.startswith("ha_") or "_ha_" in code]


def test_a_token_in_an_old_ha_section_stays_redacted_in_the_support_bundle(tmp_path):
    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    config["ha"] = dict(OLD_SECTION)
    config_path.write_text(json.dumps(config))
    output = tmp_path / "support.zip"

    result = run_emsctl(tmp_path, "diagnose", "--support-bundle", "--output", str(output))

    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(output) as bundle:
        combined = b"".join(bundle.read(name) for name in bundle.namelist())
        redacted = json.loads(bundle.read("redacted-config.json"))
    assert OLD_SECTION["token"].encode() not in combined
    assert redacted["ha"].get("token") != OLD_SECTION["token"]


def test_a_token_in_an_old_ha_section_stays_redacted_in_the_admin_view():
    view = redact_config_for_browser({"ha": dict(OLD_SECTION)})

    assert view["ha"]["token"] != OLD_SECTION["token"]
    assert view["ha"]["url"] == OLD_SECTION["url"]


def test_the_dashboard_offers_no_home_assistant_card_for_a_runtime_state_with_ha_fields():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
app.state.auth = {{ configured: true, authenticated: true, csrfToken: "token" }};
app.state.runtime = {{
  system: {{ enabled: true, max_total_power: 800, min_output_limit: 35, loop_interval: 5 }},
  devices: {{ WR1: {{ enabled: true, max_power: 800, offgrid_socket_mode: "off", pv_priority_factor: 1.0 }} }},
  winter: {{ enabled: false }},
  ac_charge_control: {{ enabled: true }},
  ha: {{ enabled: true, control_enabled: true }},
  _limits: {{
    system: {{ max_total_power: 800, min_output_limit: 800 }},
    devices: {{ WR1: 800 }},
    fallback_device_max_power: 800
  }}
}};
const html = app.runtimeControlPanel();
const endpoints = [...html.matchAll(/data-runtime-endpoint="([^"]+)"/g)].map((match) => match[1]);
console.log(JSON.stringify({{ html, endpoints }}));
"""
    output = run_node(script)

    assert output["endpoints"] == [
        "/api/runtime/system",
        "/api/runtime/device/WR1",
        "/api/runtime/winter",
        "/api/runtime/ac_charge_control",
    ]
    assert "Home Assistant" not in output["html"]
    assert "HA " not in output["html"]


REMOVED_PATHS = ("docs/home-assistant.md", "homeassistant-dashboard")
NOT_WALKED = ("tests/", "reports/", "develop/", "docs/developer/review-coverage.md")
WALKED_SUFFIXES = (".md", ".yml", ".yaml", ".html", ".js", ".css", ".ts")
HOME_ASSISTANT_TEXT = re.compile(
    r"home.?assistant|--no-ha\b|ha[-_]control|ha_enabled|\bha\.(?:enabled|url|token)"
    r"|\"ha\"\s*:|\bha:\s*\{|/runtime/ha\b|runtime\.ha\b"
    r"|\bsensor\.ems_solarflow|binary_sensor\.|input_(?:boolean|number|select)\.|last_seen_age_s"
    r"|\bha_\w|_ha_|_ha\b",
    re.IGNORECASE,
)
HA_ABBREVIATION = re.compile(r"\bHA\b")
EXEMPT_REFERENCES = re.compile(
    r"zendure[- ]ha\b|HA/deviceList|api/ha/deviceList|HA token|ha_device_list_token|zensdk_ha_scalar"
    r"|Zendure App / Home Assistant|tests/test_home_assistant_removed\.py\S*",
    re.IGNORECASE,
)
REMOVAL_NOTE_HEADING = re.compile(r"^#+ (?:Removed HA Commands|Former Home Assistant)")


def _walked_files():
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode().split("\0")
    return [
        name for name in listed
        if name.endswith(WALKED_SUFFIXES)
        and not name.startswith(NOT_WALKED)
        and (ROOT / name).is_file()
    ]


def _home_assistant_lines(name):
    in_fence = in_removal_note = False
    for number, line in enumerate((ROOT / name).read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("```"):
            in_fence = not in_fence
        elif name.endswith(".md") and not in_fence and line.startswith("#"):
            in_removal_note = bool(REMOVAL_NOTE_HEADING.match(line))
        if in_removal_note:
            continue
        text = EXEMPT_REFERENCES.sub("", line)
        if HOME_ASSISTANT_TEXT.search(text) or HA_ABBREVIATION.search(text):
            yield f"{name}:{number}: {line.strip()}"


@pytest.mark.documentation
def test_no_document_page_or_form_offers_home_assistant():
    """Docs, frontends and repo forms name Home Assistant nowhere.

    Exempt: the third-party Zendure-HA project, Zendure's HA/deviceList token
    and the zensdk_ha_scalar topic family, which are Zendure's reference
    implementation, cloud credential and topic tree; the sections that state
    what an installation with an old ``ha`` section or ``--no-ha`` gets now;
    and the name of this module.
    """

    offending = [line for name in _walked_files() for line in _home_assistant_lines(name)]

    assert [path for path in REMOVED_PATHS if (ROOT / path).exists()] == []
    assert offending == []


def test_js_grid_meter_type_choices_are_the_catalog_grid_meter_types():
    """A type the catalog does not know reads as unresolved, not as chosen."""
    from ems.config_catalog import grid_meter_types

    js = (ROOT / "admin" / "static" / "admin.js").read_text(encoding="utf-8")
    choices = js.split("const GRID_METER_TYPE_CHOICES = new Set([", 1)[1].split("]);", 1)[0]

    assert set(json.loads("[" + choices.rstrip().rstrip(",") + "]")) == set(grid_meter_types())
