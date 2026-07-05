# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance config draft, preview and explicit apply tests."""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from admin.maintenance_config import (
    load_maintenance_config,
    prepare_maintenance_config_apply,
    preview_maintenance_config,
    summarize_config_changes,
)
from admin.server import ScanRegistry, create_server
from tests.admin_auth_helpers import auth_headers, authenticate

pytestmark = pytest.mark.simulation


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _config():
    return {
        "_comment": ["keep me"],
        "system": {"max_total_power": 1600},
        "devices": [
            {"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800, "min_soc": 10},
            {"name": "WR2", "ip": "192.168.1.101", "sn": "BBB", "max_power": 600},
        ],
        "grid_meter": {"type": "shelly", "ip": "192.168.1.50"},
        "dashboard": {"enabled": True, "port": 8080},
        "influxdb": {"enabled": True, "mode": "bundled"},
        "winter": {"enabled": False},
        "custom_vendor_block": {"keep": True, "nested": [1, 2, 3]},
    }


def _write_config(base_dir, data):
    config_dir = base_dir / "config"
    config_dir.mkdir(exist_ok=True)
    path = config_dir / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --- load ---------------------------------------------------------------


def test_load_reads_standard_config(tmp_path):
    _write_config(tmp_path, _config())
    result = load_maintenance_config(base_dir=str(tmp_path))
    assert result["status"] == "ok"
    assert result["config_path"].endswith("config/config.json")
    assert result["summary"]["device_count"] == 2
    assert result["summary"]["grid_meter_type"] == "shelly"
    assert result["summary"]["influx_mode"] == "bundled"
    devices = result["draft"]["devices"]
    assert devices[0]["original_name"] == "WR1"
    assert devices[0]["ip"] == "192.168.1.100"
    assert result["draft"]["grid_meter"]["type"] == "shelly"
    assert len(result["revision"]) == 64
    assert "winter.enabled" in result["draft"]["features"]


def test_load_missing_config_is_clear_status(tmp_path):
    result = load_maintenance_config(base_dir=str(tmp_path))
    assert result["status"] == "missing"
    assert "message" in result


def test_load_invalid_json_is_clear_status(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text("{not json", encoding="utf-8")
    result = load_maintenance_config(base_dir=str(tmp_path))
    assert result["status"] == "invalid"


def test_load_never_exposes_secret_feature_values(tmp_path):
    data = _config()
    data["influxdb"]["token"] = "s3cret-token"
    _write_config(tmp_path, data)
    result = load_maintenance_config(base_dir=str(tmp_path))
    assert "influxdb.token" not in result["draft"]["features"]
    for section in result["catalog"]["feature_sections"]:
        assert all(field["path"] != "influxdb.token" for field in section["fields"])


# --- preview ------------------------------------------------------------


def test_preview_preserves_unknown_keys(tmp_path):
    _write_config(tmp_path, _config())
    result = load_maintenance_config(base_dir=str(tmp_path))
    draft = result["draft"]
    draft["devices"][0]["max_power"] = 600
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["status"] == "ok"
    assert preview["preview"]["custom_vendor_block"] == {"keep": True, "nested": [1, 2, 3]}
    assert preview["preview"]["_comment"] == ["keep me"]


def test_preview_reports_changed_fields(tmp_path):
    _write_config(tmp_path, _config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["devices"][0]["max_power"] = 600
    draft["grid_meter"]["type"] = "shelly_3em_gen1"

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    diff = preview["diff"]
    assert preview["changed"] is True
    change_paths = {c["path"] for c in diff["changes"]}
    assert "devices[0].max_power" in change_paths
    assert "grid_meter.type" in change_paths


def test_preview_reports_removed_device(tmp_path):
    _write_config(tmp_path, _config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["devices"] = [d for d in draft["devices"] if d.get("original_name") != "WR2"]

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["preview"]["devices"] == [preview["preview"]["devices"][0]]
    assert len(preview["preview"]["devices"]) == 1
    assert any(e["path"].startswith("devices[1]") for e in preview["diff"]["removed"])


def test_preview_reports_added_device(tmp_path):
    _write_config(tmp_path, _config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["devices"].append(
        {"original_name": None, "name": "WR3", "ip": "192.168.1.102", "sn": "CCC", "enabled": True}
    )
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert len(preview["preview"]["devices"]) == 3
    assert any(e["path"].startswith("devices[2]") for e in preview["diff"]["added"])


def test_preview_removes_grid_meter_from_draft_without_writing(tmp_path):
    path = _write_config(tmp_path, _config())
    original = path.read_text(encoding="utf-8")
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["grid_meter"]["present"] = False

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))

    assert "grid_meter" not in preview["preview"]
    assert any(e["path"].startswith("grid_meter") for e in preview["diff"]["removed"])
    assert path.read_text(encoding="utf-8") == original


def test_preview_validates_bad_host(tmp_path):
    _write_config(tmp_path, _config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["devices"][0]["ip"] = "not a host!!"
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is False
    assert any(e["code"] == "device_host_invalid" for e in preview["validation"]["errors"])


def test_load_and_preview_zendure_3ct_http_grid_meter(tmp_path):
    data = _config()
    data["grid_meter"] = {"type": "zendure_smartmeter_3ct_http", "ip": "192.168.1.60"}
    _write_config(tmp_path, data)

    loaded = load_maintenance_config(base_dir=str(tmp_path))
    assert loaded["summary"]["grid_meter_type"] == "zendure_smartmeter_3ct_http"
    draft = loaded["draft"]
    assert draft["grid_meter"]["type"] == "zendure_smartmeter_3ct_http"
    assert draft["grid_meter"]["ip"] == "192.168.1.60"

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is True
    assert preview["preview"]["grid_meter"] == {
        "type": "zendure_smartmeter_3ct_http",
        "ip": "192.168.1.60",
    }


def test_preview_requires_ip_for_zendure_3ct_http_grid_meter(tmp_path):
    _write_config(tmp_path, _config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["grid_meter"]["type"] = "zendure_smartmeter_3ct_http"
    draft["grid_meter"]["ip"] = ""

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is False
    assert any(
        e["code"] == "grid_meter_host_invalid" for e in preview["validation"]["errors"]
    )


def test_switch_to_zendure_3ct_http_drops_stale_tasmota_and_mqtt_keys(tmp_path):
    data = _config()
    data["grid_meter"] = {
        "type": "tasmota_http",
        "ip": "192.168.1.60",
        "url": "http://192.168.1.60/cm?cmnd=Status%2010",
        "power_path": "StatusSNS.SML.Power_curr",
        "mqtt": {"host": "mqtt.local", "topic": "meter/grid"},
        "topic": "meter/grid",
    }
    _write_config(tmp_path, data)
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["grid_meter"] = {
        "present": True,
        "type": "zendure_smartmeter_3ct_http",
        "ip": "192.168.1.60",
    }

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    grid = preview["preview"]["grid_meter"]
    assert grid["type"] == "zendure_smartmeter_3ct_http"
    assert grid["ip"] == "192.168.1.60"
    assert "url" not in grid
    assert "power_path" not in grid
    assert "mqtt" not in grid
    assert "topic" not in grid


def test_preview_does_not_write_config(tmp_path):
    path = _write_config(tmp_path, _config())
    original = path.read_text(encoding="utf-8")
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["devices"][0]["max_power"] = 123
    draft["features"]["winter.enabled"] = True
    preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert path.read_text(encoding="utf-8") == original


def test_prepare_apply_rejects_stale_real_config(tmp_path):
    path = _write_config(tmp_path, _config())
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    changed = _config()
    changed["system"]["max_total_power"] = 999
    path.write_text(json.dumps(changed), encoding="utf-8")

    result = prepare_maintenance_config_apply(
        loaded["draft"], loaded["revision"], base_dir=str(tmp_path)
    )

    assert result["status"] == "conflict"
    assert json.loads(path.read_text(encoding="utf-8"))["system"]["max_total_power"] == 999


def test_prepare_apply_serializes_valid_reviewed_draft_without_writing(tmp_path):
    path = _write_config(tmp_path, _config())
    original = path.read_bytes()
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    loaded["draft"]["devices"].append(
        {"name": "WR3", "ip": "192.168.1.102", "sn": "CCC", "enabled": True}
    )

    result = prepare_maintenance_config_apply(
        loaded["draft"], loaded["revision"], base_dir=str(tmp_path)
    )

    assert result["status"] == "ok"
    assert json.loads(result["payload"])["devices"][-1]["sn"] == "CCC"
    assert path.read_bytes() == original


def test_preview_missing_config_is_clear_status(tmp_path):
    result = preview_maintenance_config({}, base_dir=str(tmp_path))
    assert result["status"] == "missing"


def test_feature_change_is_coerced_and_diffed(tmp_path):
    _write_config(tmp_path, _config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["features"]["winter.enabled"] = True
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["preview"]["winter"]["enabled"] is True
    assert any(c["path"] == "winter.enabled" for c in preview["diff"]["changes"])


# --- diff ---------------------------------------------------------------


def test_summarize_ignores_comment_keys():
    before = {"_comment": ["a"], "x": 1}
    after = {"_comment": ["b"], "x": 2}
    diff = summarize_config_changes(before, after)
    assert diff["changes"] == [{"path": "x", "before": 1, "after": 2}]


def test_summarize_bounds_long_strings():
    before = {"x": "a"}
    after = {"x": "b" * 500}
    diff = summarize_config_changes(before, after)
    assert diff["changes"][0]["after"].endswith("…")
    assert len(diff["changes"][0]["after"]) <= 200


# --- endpoints ----------------------------------------------------------


def _server(config_apply=None):
    registry = ScanRegistry(scan_runner=lambda *a, **k: ([], []))
    srv = create_server(
        "127.0.0.1", 0, registry=registry, config_apply=config_apply
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    return srv, base


def _get(url):
    req = urllib.request.Request(url, headers=auth_headers(url, "GET"), method="GET")
    with urllib.request.urlopen(req) as resp:
        return resp.status, json.loads(resp.read())


def _post(url, body):
    data = json.dumps(body).encode("utf-8")
    headers = dict(auth_headers(url, "POST"))
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def test_config_endpoint_reads_current_config(tmp_path, monkeypatch):
    _write_config(tmp_path, _config())
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _server()
    try:
        status, payload = _get(f"{base}/api/admin/maintenance/config")
    finally:
        srv.shutdown()
        srv.server_close()
    assert status == 200
    assert payload["status"] == "ok"
    assert payload["summary"]["device_count"] == 2


def test_preview_endpoint_returns_validation_and_diff(tmp_path, monkeypatch):
    _write_config(tmp_path, _config())
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _server()
    try:
        load_status, loaded = _get(f"{base}/api/admin/maintenance/config")
        draft = loaded["draft"]
        draft["devices"][0]["max_power"] = 555
        status, payload = _post(
            f"{base}/api/admin/maintenance/config/preview", {"draft": draft}
        )
    finally:
        srv.shutdown()
        srv.server_close()
    assert load_status == 200
    assert status == 200
    assert payload["changed"] is True
    assert "validation" in payload


def test_preview_endpoint_rejects_custom_path(tmp_path, monkeypatch):
    _write_config(tmp_path, _config())
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _server()
    try:
        status, payload = _post(
            f"{base}/api/admin/maintenance/config/preview",
            {"path": "/etc/evil.json", "draft": {}},
        )
    finally:
        srv.shutdown()
        srv.server_close()
    assert status == 400


def test_apply_endpoint_requires_confirmation_and_leaves_config_unchanged(
    tmp_path, monkeypatch
):
    path = _write_config(tmp_path, _config())
    original = path.read_bytes()
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    srv, base = _server()
    try:
        status, payload = _post(
            f"{base}/api/admin/maintenance/config/apply",
            {
                "draft": loaded["draft"],
                "revision": loaded["revision"],
                "confirm": False,
                "backup": True,
            },
        )
    finally:
        srv.shutdown()
        srv.server_close()
    assert status == 400
    assert "confirmation" in payload["error"]
    assert path.read_bytes() == original


def test_apply_endpoint_writes_reviewed_draft_and_creates_backup(
    tmp_path, monkeypatch
):
    from admin.config_apply import ConfigApplyService
    from admin.install_context import detect_install_context

    path = _write_config(tmp_path, _config())
    original = path.read_bytes()
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    loaded["draft"]["devices"][0]["ip"] = "192.168.1.111"
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    service = ConfigApplyService(
        None,
        tmp_path / "admin-data",
        install_context_provider=lambda: detect_install_context(base_dir=str(tmp_path)),
    )
    srv, base = _server(config_apply=service)
    try:
        status, payload = _post(
            f"{base}/api/admin/maintenance/config/apply",
            {
                "draft": loaded["draft"],
                "revision": loaded["revision"],
                "confirm": True,
                "backup": True,
            },
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    assert payload["ok"] is True
    assert json.loads(path.read_text(encoding="utf-8"))["devices"][0]["ip"] == "192.168.1.111"
    assert payload["backup_path"]
    assert Path(payload["backup_path"]).read_bytes() == original
