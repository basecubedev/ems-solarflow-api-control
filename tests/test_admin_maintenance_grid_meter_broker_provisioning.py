# SPDX-License-Identifier: AGPL-3.0-or-later
"""Adopting an MQTT grid meter on a broker the config does not know yet.

Regression: the Maintenance grid-meter adoption used to hand the preview a
``grid_meter.mqtt.broker_ref`` for a broker discovered in the same session but
never declared, so validation refused the draft with "is not a configured
zendure_mqtt broker profile". The adopted draft now carries the same proposal
broker block an MQTT inverter draft entry carries and is provisioned through the
one shared resolver: an endpoint already declared under any ref is reused, a new
one provisions its own profile, and a ref that exists with different connection
data is rejected instead of being replaced.

No broker profile is pre-seeded in the regression fixtures.
"""

import json

import pytest

from admin.maintenance_config import (
    load_maintenance_config,
    prepare_maintenance_config_apply,
    preview_maintenance_config,
)

pytestmark = pytest.mark.simulation

D0_TOPIC = "Zendure/sensor/D0SERIAL/totalPower"
BROKER_REF = "local_mqtt_192_168_50_30_a1b2c3d4"
BROKER_HOST = "192.168.50.30"


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _config(**extra):
    data = {
        "system": {"max_total_power": 1600},
        "devices": [{"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800}],
        "grid_meter": {"type": "shelly", "ip": "192.168.1.50"},
    }
    data.update(extra)
    return data


def _write_config(base_dir, data):
    config_dir = base_dir / "config"
    config_dir.mkdir(exist_ok=True)
    path = config_dir / "config.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def _broker(**extra):
    """The broker block admin.js ``mqttProposalBrokerProfile`` puts on the draft."""

    broker = {
        "ref": BROKER_REF,
        "host": BROKER_HOST,
        "port": 1883,
        "tls": False,
        "tls_insecure": False,
        "tls_mode": "",
        "credentials_ref": "",
        "source": "local_mqtt",
    }
    broker.update(extra)
    return broker


def _adopted_grid_meter(broker=None, broker_ref=BROKER_REF):
    """Exactly the draft ``mconfigAdoptMqttGridMeterProposal`` writes."""

    meter = {
        "present": True,
        "type": "zendure_smartmeter_d0",
        "mqtt": {
            "broker_ref": broker_ref,
            "topic": D0_TOPIC,
            "payload_format": "number",
            "max_age_seconds": 15,
        },
    }
    if broker is not False:
        meter["broker"] = broker if broker else _broker(ref=broker_ref)
    return meter


def _preview(tmp_path, config, meter):
    _write_config(tmp_path, config)
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["grid_meter"] = meter
    return preview_maintenance_config(draft, base_dir=str(tmp_path))


def _brokers(preview):
    return (preview["preview"].get("zendure_mqtt") or {}).get("brokers") or {}


def _codes(preview):
    return [issue["code"] for issue in preview["validation"]["errors"]]


# --- the regression: a broker the config never declared -------------------


def test_new_broker_is_provisioned_so_the_preview_validates(tmp_path):
    preview = _preview(tmp_path, _config(), _adopted_grid_meter())

    assert preview["validation"]["ok"] is True
    assert _codes(preview) == []
    assert _brokers(preview) == {
        BROKER_REF: {
            "enabled": True,
            "source": "local_mqtt",
            "host": BROKER_HOST,
            "port": 1883,
            "tls": False,
        }
    }
    assert preview["preview"]["grid_meter"] == {
        "type": "zendure_smartmeter_d0",
        "mqtt": {
            "broker_ref": BROKER_REF,
            "topic": D0_TOPIC,
            "payload_format": "number",
            "max_age_seconds": 15,
        },
    }


def test_a_draft_without_the_broker_block_still_reports_the_unknown_ref(tmp_path):
    preview = _preview(tmp_path, _config(), _adopted_grid_meter(broker=False))

    assert preview["validation"]["ok"] is False
    assert "grid_meter_mqtt_invalid" in _codes(preview)


def test_the_adopted_draft_applies_and_survives_a_reload(tmp_path):
    path = _write_config(tmp_path, _config())
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    loaded["draft"]["grid_meter"] = _adopted_grid_meter()

    prepared = prepare_maintenance_config_apply(
        loaded["draft"], loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok"
    path.write_bytes(prepared["payload"])

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["zendure_mqtt"]["brokers"][BROKER_REF]["host"] == BROKER_HOST
    assert stored["grid_meter"]["mqtt"]["broker_ref"] == BROKER_REF

    reloaded = load_maintenance_config(base_dir=str(tmp_path))
    assert reloaded["draft"]["grid_meter"]["mqtt"]["broker_ref"] == BROKER_REF
    unchanged = preview_maintenance_config(reloaded["draft"], base_dir=str(tmp_path))
    assert unchanged["validation"]["ok"] is True
    assert unchanged["changed"] is False


# --- existing profiles are reused, never duplicated ----------------------


def _configured(ref=BROKER_REF, **profile):
    broker = {
        "enabled": True,
        "source": "local_mqtt",
        "host": BROKER_HOST,
        "port": 1883,
        "tls": False,
    }
    broker.update(profile)
    return _config(zendure_mqtt={"brokers": {ref: broker}})


def test_the_configured_profile_is_reused_without_a_duplicate(tmp_path):
    preview = _preview(tmp_path, _configured(), _adopted_grid_meter())

    assert preview["validation"]["ok"] is True
    assert list(_brokers(preview)) == [BROKER_REF]
    assert preview["preview"]["grid_meter"]["mqtt"]["broker_ref"] == BROKER_REF
    assert preview["changed"] is True  # only the meter changed


def test_the_same_endpoint_under_another_ref_is_reused(tmp_path):
    """Broker identity, not the requested name, decides which profile is used."""

    preview = _preview(tmp_path, _configured(ref="house_bridge"), _adopted_grid_meter())

    assert preview["validation"]["ok"] is True
    assert list(_brokers(preview)) == ["house_bridge"]
    assert preview["preview"]["grid_meter"]["mqtt"]["broker_ref"] == "house_bridge"


def test_adopting_twice_changes_nothing_the_second_time(tmp_path):
    first = _preview(tmp_path, _configured(), _adopted_grid_meter())
    applied = first["preview"]
    applied["grid_meter"] = {
        "type": "zendure_smartmeter_d0",
        "mqtt": {
            "broker_ref": BROKER_REF,
            "topic": D0_TOPIC,
            "payload_format": "number",
            "max_age_seconds": 15,
        },
    }
    second = _preview(tmp_path, applied, _adopted_grid_meter())

    assert second["validation"]["ok"] is True
    assert second["changed"] is False
    assert list(_brokers(second)) == [BROKER_REF]


def test_an_inverter_and_the_grid_meter_share_one_profile(tmp_path):
    _write_config(tmp_path, _config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["grid_meter"] = _adopted_grid_meter()
    draft["devices"].append(
        {
            "kind": "zendure_mqtt",
            "name": "INV_1",
            "enabled": True,
            "has_enabled_key": True,
            "serial_number": "SN-MQTT-1",
            "device_id": "DEV-1",
            "product_key": "PK-1",
            "mqtt": {
                "broker_ref": BROKER_REF,
                "source": "local_mqtt",
                "topic_family": "zensdk_ha_scalar",
                "device_id": "DEV-1",
                "product_key": "PK-1",
            },
            "capabilities": {"read_power": True, "read_soc": True},
            "broker": _broker(),
        }
    )
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))

    assert preview["validation"]["ok"] is True
    assert list(_brokers(preview)) == [BROKER_REF]
    assert preview["preview"]["grid_meter"]["mqtt"]["broker_ref"] == BROKER_REF
    assert preview["preview"]["devices"][-1]["mqtt"]["broker_ref"] == BROKER_REF


# --- conflicts and endpoint problems -------------------------------------


def test_a_ref_conflict_with_a_different_endpoint_is_refused(tmp_path):
    preview = _preview(
        tmp_path, _configured(host="192.168.50.99"), _adopted_grid_meter()
    )

    assert preview["validation"]["ok"] is False
    assert "zendure_mqtt_broker_conflict" in _codes(preview)
    # The operator's profile is never silently rewritten.
    assert _brokers(preview)[BROKER_REF]["host"] == "192.168.50.99"


def test_an_invalid_endpoint_is_reported_instead_of_provisioned(tmp_path):
    preview = _preview(
        tmp_path, _config(), _adopted_grid_meter(broker=_broker(port="not-a-port"))
    )

    assert preview["validation"]["ok"] is False
    assert "zendure_mqtt_broker_endpoint_invalid" in _codes(preview)
    assert _brokers(preview) == {}


def test_an_incomplete_endpoint_is_reported_instead_of_provisioned(tmp_path):
    preview = _preview(
        tmp_path, _config(), _adopted_grid_meter(broker=_broker(host=""))
    )

    assert preview["validation"]["ok"] is False
    assert "zendure_mqtt_broker_incomplete" in _codes(preview)
    assert _brokers(preview) == {}


# --- TLS, credentials and cloud safety -----------------------------------


def test_a_tls_broker_with_a_credential_reference_keeps_both(tmp_path):
    broker = _broker(
        port=8883,
        tls=True,
        tls_insecure=True,
        tls_mode="insecure_no_verify",
        credentials_ref=BROKER_REF,
    )
    preview = _preview(tmp_path, _config(), _adopted_grid_meter(broker=broker))

    assert preview["validation"]["ok"] is True
    assert _brokers(preview)[BROKER_REF] == {
        "enabled": True,
        "source": "local_mqtt",
        "host": BROKER_HOST,
        "port": 8883,
        "tls": True,
        "tls_insecure": True,
        "credentials_ref": BROKER_REF,
    }


def test_an_anonymous_broker_gets_no_credential_reference(tmp_path):
    preview = _preview(tmp_path, _config(), _adopted_grid_meter())

    assert "credentials_ref" not in _brokers(preview)[BROKER_REF]


def test_no_cloud_broker_profile_is_minted_for_a_grid_meter(tmp_path):
    broker = _broker(
        ref="zendure_cloud",
        host="mqtteu.zen-iot.com",
        port=8883,
        tls=True,
        source="zendure_cloud_mqtt",
    )
    preview = _preview(
        tmp_path,
        _config(),
        _adopted_grid_meter(broker=broker, broker_ref="zendure_cloud"),
    )

    assert preview["validation"]["ok"] is False
    assert _brokers(preview) == {}
    assert "grid_meter_mqtt_invalid" in _codes(preview)
