# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Settings tab that lists every value away from this release's defaults.

The list is drawn in the browser (``frontend/admin-modern/src/lib/settings.js``,
``departures``) from the view the Maintenance config route answers. These tests
feed it the real view of real files, so the template, the catalogue, the
operating values in the runtime state and the browser's reading of them are
held together.
"""

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from admin.maintenance_config import _catalog, load_maintenance_config
from ems.retired_defaults import RETIRED_DEFAULTS

pytestmark = [
    pytest.mark.admin,
    pytest.mark.config,
    pytest.mark.contract,
]

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / "frontend" / "admin-modern" / "src" / "lib" / "settings.js"
TEMPLATE = ROOT / "config" / "config.template.json"


def _template():
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))


def _install(base_dir, config, runtime=None):
    (base_dir / "config").mkdir(exist_ok=True)
    (base_dir / "config" / "config.json").write_text(json.dumps(config), encoding="utf-8")
    if runtime is not None:
        (base_dir / "data").mkdir(exist_ok=True)
        (base_dir / "data" / "runtime-state.json").write_text(json.dumps(runtime), encoding="utf-8")


def _departures(base_dir):
    """The rows the tab lists for the install at ``base_dir``: group, path, value, default."""

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to evaluate the Admin's departures")
    view = load_maintenance_config(base_dir=str(base_dir))
    assert view["status"] == "ok", view
    payload = base_dir / "view.json"
    payload.write_text(json.dumps(view), encoding="utf-8")
    script = (
        f"const fs = await import('node:fs'); const m = await import({json.dumps(SETTINGS.as_uri())});"
        f"const view = JSON.parse(fs.readFileSync({json.dumps(str(payload))}, 'utf8'));"
        "process.stdout.write(JSON.stringify(m.departures(view).flatMap((group) => group.rows.map((row) =>"
        " [group.id, row.field.path, row.value ?? null, row.standard]))));"
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout)


def _set(config, path, value):
    node = config
    *parents, key = path.split(".")
    for part in parents:
        node = node.setdefault(part, {})
    node[key] = value


def test_a_config_from_this_releases_template_departs_from_no_default(tmp_path):
    _install(tmp_path, _template())

    assert _departures(tmp_path) == []


def test_every_value_an_earlier_release_shipped_as_its_default_is_listed(tmp_path):
    config = _template()
    retired = {}
    for entry in RETIRED_DEFAULTS:
        retired.setdefault(entry.path, entry.values[0])
    for path, value in retired.items():
        _set(config, path, copy.deepcopy(value))
    _install(tmp_path, config)

    listed = {row[1]: row for row in _departures(tmp_path)}

    assert set(listed) == set(retired)
    assert all(listed[path][2] == value for path, value in retired.items())


def test_an_operating_value_is_listed_once_as_the_ems_runs_it(tmp_path):
    config = _template()
    config["system"]["min_output_limit"] = 40
    for number, device in enumerate(config["devices"], start=1):
        device["sn"] = f"SERIAL-{number}"
    device = config["devices"][1]
    runtime = {
        "system": {"min_output_limit": 35, "loop_interval": 7},
        "devices": {device["name"]: {"identity": device["sn"], "pv_priority_factor": 2.0}},
    }
    _install(tmp_path, config, runtime)

    rows = {row[1]: row for row in _departures(tmp_path)}

    assert "system.min_output_limit" not in rows
    assert rows["system.loop_interval"] == ["section:system", "system.loop_interval", 7, 5]
    assert rows["devices[].pv_priority_factor"] == ["device:1", "devices[].pv_priority_factor", 2.0, 1.0]
    assert len(rows) == 2


def test_a_device_field_names_the_default_a_new_device_gets():
    catalog = _catalog()
    fields = next(section for section in catalog["hardware_sections"] if section["id"] == "devices")["fields"]
    common = catalog["default_device"]["common"]
    defaults = {field["path"].split(".", 1)[1]: field.get("default") for field in fields}

    assert {key: defaults[key] for key in common} == common


def test_a_switch_spelled_as_text_is_listed_with_its_text_whatever_it_says(tmp_path):
    config = _template()
    config["system"]["allow_hardware_writes"] = "true"
    config["system"]["dry_run"] = "false"
    config["system"]["simulation_mode"] = False
    _install(tmp_path, config)

    rows = _departures(tmp_path)

    assert sorted(rows) == [
        ["section:system", "system.allow_hardware_writes", "true", True],
        ["section:system", "system.dry_run", "false", False],
    ]


CONNECTIONS = {
    "dashboard.host": "127.0.0.1",
    "dashboard.port": 8081,
    "influxdb.url": "http://influx.lan:8086",
    "influxdb.host_url": "http://192.168.1.5:8086",
    "influxdb.org": "home",
    "influxdb.token_env": "HOME_INFLUX_TOKEN",
    "influxdb.bucket_prefix": "house",
}


def test_a_connection_away_from_its_default_is_not_listed(tmp_path):
    config = _template()
    for path, value in CONNECTIONS.items():
        _set(config, path, value)
    _install(tmp_path, config)

    assert _departures(tmp_path) == []


def test_a_broker_that_skips_tls_verification_is_listed_beside_its_hidden_address(tmp_path):
    config = _template()
    _set(config, "zendure_mqtt.host", "192.168.1.20")
    _set(config, "zendure_mqtt.tls_insecure", True)
    _set(config, "zendure_mqtt.keepalive_seconds", 60)
    _install(tmp_path, config)

    assert sorted(_departures(tmp_path)) == [
        ["section:zendure_mqtt", "zendure_mqtt.keepalive_seconds", 60, 30],
        ["section:zendure_mqtt", "zendure_mqtt.tls_insecure", True, False],
    ]


def test_every_address_port_or_url_the_tab_could_list_is_marked_as_a_connection():
    unmarked = [
        field["path"]
        for section in _catalog()["feature_sections"]
        for field in section["fields"]
        if field["path"].rsplit(".", 1)[-1] in {"host", "port", "url", "host_url"} and not field.get("connection")
    ]

    assert unmarked == []
