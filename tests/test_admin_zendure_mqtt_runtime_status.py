# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin bridge to the EMS-owned Zendure MQTT telemetry runtime status.

Admin is UI/orchestration only: these tests pin that the bridge passes through
the EMS-owned sanitized status, never exposes credentials, and degrades to a
friendly unavailable view rather than raising.
"""

import json
import time

import pytest

from admin import zendure_mqtt_runtime_status as bridge
from ems import paths

pytestmark = pytest.mark.simulation


def _write_config(base_dir, config):
    config_dir = base_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")


def _write_live_status(base_dir, status, *, written_at=None):
    data_dir = base_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "written_at": time.time() if written_at is None else written_at,
        "status": status,
    }
    (data_dir / paths.ZENDURE_MQTT_STATUS_FILENAME).write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _write_live_raw(base_dir, text):
    data_dir = base_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / paths.ZENDURE_MQTT_STATUS_FILENAME).write_text(text, encoding="utf-8")


def _live_status(**overrides):
    status = {
        "enabled": True,
        "broker_configured": True,
        "endpoint": "broker.local:1883",
        "configured_device_count": 1,
        "invalid_device_count": 0,
        "stale_after_seconds": 60,
        "running": True,
        "connected": True,
        "host": "broker.local",
        "port": 1883,
        "devices": [
            {
                "name": "Battery",
                "identifier": "DEV1",
                "topic_family": "zensdk_ha_scalar",
                "status": "online",
                "last_seen": "2026-07-09T10:00:00+00:00",
                "age_seconds": 3.0,
                "metric_count": 2,
                "metrics": ["electricLevel", "outputPower"],
                "capabilities": ["battery_storage"],
                "write_output_limit": False,
            }
        ],
    }
    status.update(overrides)
    return status


def _telemetry_device(name="Battery", device_id="DEV1"):
    return {
        "type": "zendure_mqtt",
        "name": name,
        "mqtt": {"topic_family": "zensdk_ha_scalar", "device_id": device_id},
    }


def test_missing_config_returns_friendly_unavailable(tmp_path):
    view = bridge.build_runtime_status_view(base_dir=str(tmp_path))
    assert view["available"] is False
    assert view["runtime_state"] == "unavailable"
    assert view["devices"] == []
    assert view["message"]


def test_invalid_config_json_returns_unavailable(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text("{not json", encoding="utf-8")
    view = bridge.build_runtime_status_view(base_dir=str(tmp_path))
    assert view["available"] is False
    assert view["runtime_state"] == "unavailable"


def _raise_import_error(_config):
    raise ImportError("No module named 'ems.zendure_mqtt.runtime'")


def test_offline_build_failure_on_unused_config_reports_inactive(
    tmp_path, monkeypatch
):
    # An Admin build whose EMS status builder cannot run (e.g. an older
    # container image without the runtime modules) must not show a red
    # "Unavailable" for an install that plainly does not use MQTT.
    _write_config(
        tmp_path,
        {"devices": [{"name": "WR1", "ip": "192.168.1.100", "sn": "SN1"}]},
    )
    monkeypatch.setattr(bridge, "_build_status_via_ems", _raise_import_error)
    view = bridge.build_runtime_status_view(base_dir=str(tmp_path))
    assert view["available"] is True
    assert view["runtime_state"] == "inactive"


def test_offline_build_failure_on_mqtt_config_stays_unavailable(
    tmp_path, monkeypatch
):
    # With MQTT actually configured, a failing builder is a real problem: the
    # card must keep warning instead of pretending the feature is inactive.
    _write_config(
        tmp_path,
        {
            "zendure_mqtt": {"host": "broker.local", "port": 1883},
            "devices": [_telemetry_device()],
        },
    )
    monkeypatch.setattr(bridge, "_build_status_via_ems", _raise_import_error)
    view = bridge.build_runtime_status_view(base_dir=str(tmp_path))
    assert view["available"] is False
    assert view["runtime_state"] == "unavailable"


def test_hostless_runtime_reports_inactive(tmp_path):
    # The feature is always on; without a broker host the runtime is inactive.
    # A stale legacy ``enabled`` key changes nothing.
    _write_config(tmp_path, {"zendure_mqtt": {"enabled": False}, "devices": []})
    view = bridge.build_runtime_status_view(base_dir=str(tmp_path))
    assert view["available"] is True
    assert view["runtime_state"] == "inactive"
    assert view["enabled"] is False
    assert "no MQTT broker is configured" in view["message"]
    # The message explains that no toggle exists: configuring a broker is all
    # it takes to activate telemetry.
    assert "activates automatically" in view["message"]


def test_status_payload_is_passed_through_sanitized(tmp_path):
    _write_config(
        tmp_path,
        {
            "zendure_mqtt": {
                "enabled": True,
                "host": "broker.local",
                "port": 1883,
                "stale_after_seconds": 45,
            },
            "devices": [_telemetry_device()],
        },
    )
    view = bridge.build_runtime_status_view(base_dir=str(tmp_path))
    assert view["available"] is True
    assert view["runtime_state"] == "configured"
    assert view["endpoint"] == "broker.local:1883"
    assert view["configured_device_count"] == 1
    assert view["stale_after_seconds"] == 45.0
    assert view["write_output_limit"] is False
    device = view["devices"][0]
    assert device["name"] == "Battery"
    assert device["status"] == "unseen"
    assert device["write_output_limit"] is False


def test_credentials_are_never_exposed(tmp_path):
    _write_config(
        tmp_path,
        {
            "zendure_mqtt": {
                "enabled": True,
                "host": "broker.local",
                "username": "secretuser",
                "password": "sup3r-secret-pw",
                "app_key": "secretAppKey",
                "token": "secretToken",
            },
            "devices": [_telemetry_device()],
        },
    )
    view = bridge.build_runtime_status_view(base_dir=str(tmp_path))
    flattened = json.dumps(view)
    for secret in ("secretuser", "sup3r-secret-pw", "secretAppKey", "secretToken"):
        assert secret not in flattened
    # No secret-looking key survives the defensive scrub either.
    for key in ("username", "password", "app_key", "token", "host"):
        assert key not in view


def test_secret_looking_fields_are_stripped_defensively():
    scrubbed = bridge._scrub(
        {
            "endpoint": "broker.local:1883",
            "password": "leak",
            "api_key": "leak",
            "nested": {"app_key": "leak", "keep": 1},
            "devices": [{"name": "ok", "credential": "leak"}],
        }
    )
    assert scrubbed == {
        "endpoint": "broker.local:1883",
        "nested": {"keep": 1},
        "devices": [{"name": "ok"}],
    }


def test_invalid_device_summaries_are_preserved_as_safe_issue_codes(tmp_path):
    _write_config(
        tmp_path,
        {
            "zendure_mqtt": {"enabled": True, "host": "broker.local"},
            "devices": [
                _telemetry_device(),
                {
                    "type": "zendure_mqtt",
                    "name": "BadTelemetry",
                    "mqtt": {"topic_family": "zensdk_ha_scalar"},
                },
            ],
        },
    )
    view = bridge.build_runtime_status_view(base_dir=str(tmp_path))
    assert view["invalid_device_count"] == 1
    invalid = [d for d in view["devices"] if d["status"] == "invalid"]
    assert len(invalid) == 1
    assert invalid[0]["name"] == "BadTelemetry"
    assert "device_identifier_missing" in invalid[0]["issues"]
    assert invalid[0]["write_output_limit"] is False


def test_runtime_build_failure_degrades_to_unavailable(tmp_path, monkeypatch):
    _write_config(
        tmp_path,
        {"zendure_mqtt": {"enabled": True, "host": "broker.local"}, "devices": []},
    )

    import ems.zendure_mqtt as zpkg

    def _boom(*_args, **_kwargs):
        raise RuntimeError("no runtime here")

    monkeypatch.setattr(zpkg, "build_zendure_mqtt_runtime", _boom)
    view = bridge.build_runtime_status_view(base_dir=str(tmp_path))
    assert view["available"] is False
    assert view["runtime_state"] == "unavailable"


def test_offline_fallback_reports_source_and_reason(tmp_path):
    _write_config(tmp_path, {"zendure_mqtt": {"enabled": False}, "devices": []})
    view = bridge.build_runtime_status_view(base_dir=str(tmp_path))
    assert view["source"] == "offline_config"
    assert view["live_available"] is False
    assert view["fallback_reason"]


def test_live_status_is_preferred_when_available(tmp_path):
    _write_config(
        tmp_path,
        {"zendure_mqtt": {"enabled": True, "host": "broker.local"}, "devices": []},
    )
    _write_live_status(tmp_path, _live_status())
    view = bridge.build_runtime_status_view(base_dir=str(tmp_path))
    assert view["available"] is True
    assert view["source"] == "live_runtime"
    assert view["live_available"] is True
    assert view["fallback_reason"] is None
    assert view["endpoint"] == "broker.local:1883"
    device = view["devices"][0]
    assert device["status"] == "online"
    assert device["age_seconds"] == 3.0
    assert device["metric_count"] == 2
    assert device["write_output_limit"] is False


def test_live_device_states_pass_through(tmp_path):
    _write_config(tmp_path, {"zendure_mqtt": {"enabled": True}, "devices": []})
    _write_live_status(
        tmp_path,
        _live_status(
            devices=[
                {"name": "A", "status": "online", "metric_count": 1},
                {"name": "B", "status": "stale", "metric_count": 1},
                {"name": "C", "status": "unseen", "metric_count": 0},
                {"name": "D", "status": "invalid", "issues": ["bad"]},
            ],
            invalid_device_count=1,
        ),
    )
    view = bridge.build_runtime_status_view(base_dir=str(tmp_path))
    states = {d["name"]: d["status"] for d in view["devices"]}
    assert states == {"A": "online", "B": "stale", "C": "unseen", "D": "invalid"}


def test_stale_live_snapshot_falls_back_to_offline(tmp_path):
    _write_config(
        tmp_path,
        {"zendure_mqtt": {"enabled": True, "host": "broker.local"}, "devices": []},
    )
    _write_live_status(
        tmp_path, _live_status(), written_at=time.time() - 10_000
    )
    view = bridge.build_runtime_status_view(base_dir=str(tmp_path))
    assert view["source"] == "offline_config"
    assert view["live_available"] is False
    assert "stale" in view["fallback_reason"].lower()


def test_malformed_live_snapshot_falls_back_safely(tmp_path):
    _write_config(
        tmp_path,
        {"zendure_mqtt": {"enabled": True, "host": "broker.local"}, "devices": []},
    )
    _write_live_raw(tmp_path, "{not json")
    view = bridge.build_runtime_status_view(base_dir=str(tmp_path))
    assert view["available"] is True
    assert view["source"] == "offline_config"
    assert view["live_available"] is False
    assert view["fallback_reason"]


def test_live_snapshot_without_status_object_falls_back(tmp_path):
    _write_config(
        tmp_path,
        {"zendure_mqtt": {"enabled": True, "host": "broker.local"}, "devices": []},
    )
    _write_live_raw(tmp_path, json.dumps({"written_at": time.time()}))
    view = bridge.build_runtime_status_view(base_dir=str(tmp_path))
    assert view["source"] == "offline_config"
    assert view["live_available"] is False


def test_live_status_never_exposes_credentials(tmp_path):
    _write_config(tmp_path, {"zendure_mqtt": {"enabled": True}, "devices": []})
    _write_live_status(
        tmp_path,
        _live_status(
            username="secretuser",
            password="sup3r-secret-pw",
            app_key="secretAppKey",
            token="secretToken",
        ),
    )
    view = bridge.build_runtime_status_view(base_dir=str(tmp_path))
    flattened = json.dumps(view)
    for secret in ("secretuser", "sup3r-secret-pw", "secretAppKey", "secretToken"):
        assert secret not in flattened
    for key in ("username", "password", "app_key", "token", "host", "port"):
        assert key not in view


def test_live_cloud_status_never_exposes_route_id_or_full_command_topic(tmp_path):
    route = "ACCOUNT_ROUTE_1234"
    topic = f"iot/PRODUCT_SECRET/{route}/properties/write"
    _write_config(tmp_path, {"zendure_mqtt": {"enabled": True}, "devices": []})
    _write_live_status(
        tmp_path,
        _live_status(
            brokers=[
                {
                    "broker_ref": "cloud_a",
                    "source": "zendure_cloud_mqtt",
                }
            ],
            devices=[
                {
                    "name": "Cloud battery",
                    "broker_ref": "cloud_a",
                    "source": "zendure_cloud_mqtt",
                    "identifier": route,
                    "effective_write_topic": topic,
                    "last_command": {
                        "device_id": route,
                        "topic": topic,
                        "correlation_id": "safe-correlation-id",
                    },
                }
            ],
        ),
    )

    view = bridge.build_runtime_status_view(base_dir=str(tmp_path))
    flattened = json.dumps(view)

    assert route not in flattened
    assert topic not in flattened
    assert "PRODUCT_SECRET" not in flattened
    assert view["devices"][0]["identifier"] == "…1234"
    assert view["devices"][0]["last_command"]["correlation_id"] == "safe-correlation-id"


def test_live_invalid_summary_uses_installed_config_to_mask_route_in_name(tmp_path):
    route = "ACCOUNT_ROUTE_7501"
    product = "PRODUCT_KEY_7501"
    _write_config(
        tmp_path,
        {
            "zendure_mqtt": {
                "brokers": {
                    "cloud_a": {
                        "source": "zendure_cloud_mqtt",
                        "host": "mqtt.example.invalid",
                    }
                }
            },
            "devices": [
                {
                    "type": "zendure_mqtt",
                    "name": f"Rejected Cloud {route}",
                    "mqtt": {
                        "broker_ref": "cloud_a",
                        "device_id": route,
                        "product_key": product,
                    },
                }
            ],
        },
    )
    # Invalid runtime summaries intentionally omit identifier/source details;
    # the name is the only field left that still contains the configured route.
    _write_live_status(
        tmp_path,
        _live_status(
            devices=[
                {
                    "name": f"Rejected Cloud {route}",
                    "broker_ref": "cloud_a",
                    "identifier": None,
                    "source": None,
                    "status": "invalid",
                    "issues": ["topic_family_missing"],
                }
            ],
            invalid_device_count=1,
        ),
    )

    view = bridge.build_runtime_status_view(base_dir=str(tmp_path))
    flattened = json.dumps(view)

    assert view["source"] == "live_runtime"
    assert route not in flattened
    assert product not in flattened
    assert view["devices"][0]["name"] != f"Rejected Cloud {route}"


def test_live_invalid_summary_masks_name_only_route_from_incomplete_cloud_config(
    tmp_path,
):
    route = "SECRET_CLOUD_ROUTE_7501"
    raw_name = f"Rejected Cloud {route}"
    _write_config(
        tmp_path,
        {
            "zendure_mqtt": {
                "brokers": {
                    "cloud_a": {
                        "source": "zendure_cloud_mqtt",
                        "host": "mqtt.example.invalid",
                    }
                }
            },
            "devices": [
                {
                    "type": "zendure_mqtt",
                    "name": raw_name,
                    "mqtt": {"broker_ref": "cloud_a"},
                }
            ],
        },
    )
    _write_live_status(
        tmp_path,
        _live_status(
            devices=[
                {
                    "name": raw_name,
                    "status": "invalid",
                    "issues": ["device_id_missing"],
                }
            ],
            invalid_device_count=1,
        ),
    )

    view = bridge.build_runtime_status_view(base_dir=str(tmp_path))

    assert route not in json.dumps(view)


def test_fallback_path_starts_no_broker_client(tmp_path, monkeypatch):
    _write_config(
        tmp_path,
        {"zendure_mqtt": {"enabled": True, "host": "broker.local"}, "devices": []},
    )

    import ems.zendure_mqtt as zpkg

    real_build = zpkg.build_zendure_mqtt_runtime

    def _guard(config, *args, **kwargs):
        runtime = real_build(config, *args, **kwargs)
        original_start = runtime.start

        def _no_start(*_a, **_k):
            raise AssertionError("fallback must not start the broker client")

        monkeypatch.setattr(runtime, "start", _no_start)
        assert original_start  # keep a reference; never invoked
        return runtime

    monkeypatch.setattr(zpkg, "build_zendure_mqtt_runtime", _guard)
    view = bridge.build_runtime_status_view(base_dir=str(tmp_path))
    assert view["source"] == "offline_config"
    assert view["available"] is True
