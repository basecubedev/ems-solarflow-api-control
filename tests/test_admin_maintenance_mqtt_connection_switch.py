# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance preview/apply persistence of switched Zendure MQTT connections.

Covers the browser flow where an operator picks a different concrete MQTT
connection for an already configured MQTT inverter (local broker to local
broker, and either direction between a local broker and Zendure cloud). The
whole selected connection has to reach the generated config, not only its route
device id.
"""

import copy
import json
import types

import pytest

from admin.device_common_fields import common_device_value_fields
from admin.maintenance_config import (
    load_maintenance_config,
    prepare_maintenance_config_apply,
    preview_maintenance_config,
)

pytestmark = pytest.mark.simulation


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


BROKER_B1_REF = "local_b1"
BROKER_B2_REF = "local_b2"
BROKER_B1_HOST = "10.0.0.10"
BROKER_B2_HOST = "10.0.0.20"
CLOUD_REF = "zendure_cloud"
# Cloud broker profiles always reference the reserved account credential.
CLOUD_CREDENTIALS_REF = "zendure-cloud"
CLOUD_HOST = "mqtt.zen-iot.com"
SERIAL = "SN-A"


def _write_config(base_dir, data):
    config_dir = base_dir / "config"
    config_dir.mkdir(exist_ok=True)
    path = config_dir / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _broker_profiles(*refs):
    profiles = {}
    if BROKER_B1_REF in refs:
        profiles[BROKER_B1_REF] = {
            "enabled": True,
            "source": "local_mqtt",
            "host": BROKER_B1_HOST,
            "port": 1883,
            "tls": False,
        }
    if BROKER_B2_REF in refs:
        profiles[BROKER_B2_REF] = {
            "enabled": True,
            "source": "local_mqtt",
            "host": BROKER_B2_HOST,
            "port": 1883,
            "tls": False,
        }
    if CLOUD_REF in refs:
        profiles[CLOUD_REF] = {
            "enabled": True,
            "source": "zendure_cloud_mqtt",
            "host": CLOUD_HOST,
            "port": 1883,
            "tls": False,
            "credentials_ref": CLOUD_CREDENTIALS_REF,
        }
    return profiles


def _local_device(ref=BROKER_B1_REF, device_id="ROUTE-B1"):
    return {
        "name": "INV_1",
        "type": "zendure_mqtt",
        "enabled": True,
        "serial_number": SERIAL,
        "max_power": 800,
        "min_soc": 10,
        "mqtt": {
            "broker_ref": ref,
            "source": "local_mqtt",
            "topic_family": "zensdk_ha_scalar",
            "base_topic": "Zendure",
            "device_id": device_id,
        },
        "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": False},
    }


def _cloud_device(device_id="ROUTE-CLOUD", product_key="PK-CLOUD"):
    return {
        "name": "INV_1",
        "type": "zendure_mqtt",
        "enabled": True,
        "serial_number": SERIAL,
        "max_power": 800,
        "min_soc": 10,
        "mqtt": {
            "broker_ref": CLOUD_REF,
            "source": "zendure_cloud_mqtt",
            "topic_family": "legacy_zendure_json",
            "base_topic": "iot",
            "device_id": device_id,
            "product_key": product_key,
        },
        "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": False},
    }


def _config(device, broker_refs=(BROKER_B1_REF,)):
    return {
        "system": {"max_total_power": 1600},
        "devices": [
            {"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800},
            copy.deepcopy(device),
        ],
        "grid_meter": {"type": "shelly", "ip": "192.168.1.50"},
        "zendure_mqtt": {"brokers": _broker_profiles(*broker_refs)},
        "dashboard": {"enabled": True, "port": 8080},
    }


def _local_proposal(host, device_id, serial=SERIAL, credentials_ref=None):
    from admin.zendure_mqtt_config_proposals import build_proposals

    observation = {
        "source_type": "local_mqtt",
        "broker_host": host,
        "broker_port": 1883,
        "topic_family": "zensdk_ha_scalar",
        "serial_number": serial,
        "device_id": device_id,
        "metrics_seen": ["electricLevel", "outputHomePower"],
    }
    if credentials_ref:
        observation["credentials_ref"] = credentials_ref
    return build_proposals([observation])[0]


def _cloud_proposal(device_id="ROUTE-CLOUD", product_key="PK-CLOUD", serial=SERIAL):
    from admin.zendure_mqtt_config_proposals import build_proposals

    return build_proposals(
        [
            {
                "source_type": "zendure_cloud_mqtt",
                "broker_host": CLOUD_HOST,
                "broker_port": 1883,
                "topic_family": "legacy_zendure_json",
                "serial_number": serial,
                "device_id": device_id,
                "product_key": product_key,
                "metrics_seen": ["electricLevel", "outputHomePower"],
            }
        ]
    )[0]


def _connect_zendure_account():
    """A cloud broker profile may only be provisioned against connected auth."""

    from admin.credential_store import CredentialStore

    CredentialStore().zendure.save_token("account-api-key")


def _draft_from_proposal(proposal, name):
    """Mirror the browser's ``mconfigZendureMqttDraftFromProposal``."""

    fragment = proposal["config_fragment"]
    mqtt = fragment.get("mqtt") or {}
    caps = fragment.get("capabilities") or {}
    output_control = caps.get("write_output_limit") is True
    route = mqtt.get("device_id") or proposal.get("device_id") or ""
    item = {
        "kind": "zendure_mqtt",
        "original_name": None,
        "proposal_id": proposal.get("id") or "",
        "proposal_broker_ref": proposal.get("broker_ref") or mqtt.get("broker_ref") or "",
        "name": name,
        "enabled": True,
        "has_enabled_key": True,
        "serial_number": proposal.get("serial_number") or fragment.get("serial_number") or "",
        "device_id": route,
        "product_key": mqtt.get("product_key") or "",
        "hardware_generation": proposal.get("hardware_generation") or "",
        "hardware_model": proposal.get("hardware_model") or fragment.get("hardware_profile") or "",
        "power_write_profile": fragment.get("power_write_profile") or "",
        "alternative_layout": bool(proposal.get("alternative_layout")),
        "output_control": output_control,
        "supports_output_control": proposal.get("output_control_supported") is True
        or output_control,
        "trusted_write_target": (
            output_control and proposal.get("control_block_reason") != "write_target_missing"
        ),
        "mqtt": {
            "broker_ref": mqtt.get("broker_ref") or "",
            "source": mqtt.get("source") or "",
            "topic_family": mqtt.get("topic_family") or "",
            "base_topic": mqtt.get("base_topic"),
            "device_id": route,
            "product_key": mqtt.get("product_key") or "",
            "write_protocol": mqtt.get("write_protocol") or "",
        },
        "capabilities": {
            "read_power": caps.get("read_power") is not False,
            "read_soc": caps.get("read_soc") is not False,
            "write_output_limit": output_control,
        },
        "broker": {
            "ref": proposal.get("broker_ref") or "",
            "host": proposal.get("broker_host") or "",
            "port": proposal.get("broker_port"),
            "tls": proposal.get("broker_tls") is True,
            "tls_insecure": proposal.get("broker_tls_insecure") is True,
            "tls_mode": proposal.get("broker_tls_mode") or "",
            "credentials_ref": proposal.get("credentials_ref") or "",
            "source": proposal.get("connection_source") or proposal.get("source") or "",
        },
    }
    token = proposal.get("physical_identity_token")
    if token:
        item["physical_identity_token"] = token
    return item


def _switch_connection(draft, original_name, proposal):
    """Mirror the browser's ``mconfigSwitchInverterTransport`` replacement."""

    devices = draft["devices"]
    index = next(
        position
        for position, entry in enumerate(devices)
        if entry.get("original_name") == original_name
    )
    current = devices[index]
    replacement = _draft_from_proposal(proposal, current.get("name") or original_name)
    replacement["original_name"] = current.get("original_name")
    replacement["enabled"] = current.get("enabled") is not False
    replacement["has_enabled_key"] = True
    for key in common_device_value_fields():
        if current.get(key) is not None:
            replacement[key] = current[key]
    devices[index] = replacement
    return replacement


def _mqtt_draft_item(draft, original_name="INV_1"):
    return next(
        entry
        for entry in draft["devices"]
        if entry.get("original_name") == original_name
    )


def _mqtt_device(config_or_preview):
    devices = config_or_preview.get("devices") or []
    matches = [d for d in devices if d.get("type") == "zendure_mqtt"]
    assert len(matches) == 1, devices
    return matches[0]


def _resolve_draft(draft, proposals):
    """Run the server-side proposal resolution a browser draft passes through."""

    from admin.server import AdminHandler

    class _Stub:
        server = types.SimpleNamespace(identity_token_key=None)

        def _trusted_mqtt_proposals(self):
            return list(proposals)

    return AdminHandler._resolve_maintenance_mqtt_draft(_Stub(), draft)


def _preview_and_payload(tmp_path, draft, revision):
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is True, preview["validation"]["errors"]
    prepared = prepare_maintenance_config_apply(draft, revision, base_dir=str(tmp_path))
    assert prepared["status"] == "ok", prepared
    return preview["preview"], json.loads(prepared["payload"])


# --- concrete local broker switches --------------------------------------


def test_local_broker_switch_persists_the_selected_connection(tmp_path):
    _write_config(tmp_path, _config(_local_device()))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    proposal = _local_proposal(BROKER_B2_HOST, "ROUTE-B2")
    _switch_connection(draft, "INV_1", proposal)

    preview, written = _preview_and_payload(tmp_path, draft, loaded["revision"])

    for config in (preview, written):
        mqtt = _mqtt_device(config)["mqtt"]
        assert mqtt["broker_ref"] == proposal["broker_ref"]
        assert mqtt["source"] == "local_mqtt"
        assert mqtt["device_id"] == "ROUTE-B2"
        assert mqtt["topic_family"] == "zensdk_ha_scalar"
    brokers = written["zendure_mqtt"]["brokers"]
    assert brokers[proposal["broker_ref"]]["host"] == BROKER_B2_HOST
    # The untouched original profile survives the switch.
    assert brokers[BROKER_B1_REF]["host"] == BROKER_B1_HOST


def test_switching_back_restores_the_original_broker_and_route(tmp_path):
    _write_config(
        tmp_path,
        _config(
            _local_device(ref=BROKER_B2_REF, device_id="ROUTE-B2"),
            broker_refs=(BROKER_B1_REF, BROKER_B2_REF),
        ),
    )
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    proposal = _local_proposal(BROKER_B1_HOST, "ROUTE-B1")
    _switch_connection(draft, "INV_1", proposal)

    preview, written = _preview_and_payload(tmp_path, draft, loaded["revision"])

    for config in (preview, written):
        mqtt = _mqtt_device(config)["mqtt"]
        # The existing profile for that endpoint is reused, not duplicated.
        assert mqtt["broker_ref"] == BROKER_B1_REF
        assert mqtt["source"] == "local_mqtt"
        assert mqtt["device_id"] == "ROUTE-B1"
    assert set(written["zendure_mqtt"]["brokers"]) == {BROKER_B1_REF, BROKER_B2_REF}
    assert written["zendure_mqtt"]["brokers"][BROKER_B1_REF] == _broker_profiles(
        BROKER_B1_REF
    )[BROKER_B1_REF]


# --- cloud <-> local switches --------------------------------------------


def test_cloud_to_local_switch_replaces_source_and_drops_cloud_route(tmp_path):
    _write_config(
        tmp_path,
        _config(_cloud_device(), broker_refs=(CLOUD_REF,)),
    )
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    proposal = _local_proposal(BROKER_B1_HOST, "ROUTE-B1")
    _switch_connection(draft, "INV_1", proposal)

    preview, written = _preview_and_payload(tmp_path, draft, loaded["revision"])

    for config in (preview, written):
        mqtt = _mqtt_device(config)["mqtt"]
        assert mqtt["broker_ref"] == proposal["broker_ref"]
        assert mqtt["source"] == "local_mqtt"
        assert mqtt["device_id"] == "ROUTE-B1"
        assert mqtt["topic_family"] == "zensdk_ha_scalar"
        assert mqtt["base_topic"] == "Zendure"
        # Cloud-only addressing must not survive onto a local connection.
        assert "product_key" not in mqtt
    assert written["zendure_mqtt"]["brokers"][CLOUD_REF]["host"] == CLOUD_HOST


def test_local_to_cloud_switch_persists_the_cloud_connection(tmp_path):
    _connect_zendure_account()
    _write_config(tmp_path, _config(_local_device()))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    proposal = _cloud_proposal()
    _switch_connection(draft, "INV_1", proposal)

    preview, written = _preview_and_payload(tmp_path, draft, loaded["revision"])

    for config in (preview, written):
        mqtt = _mqtt_device(config)["mqtt"]
        assert mqtt["broker_ref"] == proposal["broker_ref"]
        assert mqtt["source"] == "zendure_cloud_mqtt"
        assert mqtt["topic_family"] == "legacy_zendure_json"
        assert mqtt["base_topic"] == "iot"
    assert written["zendure_mqtt"]["brokers"][proposal["broker_ref"]]["host"] == CLOUD_HOST
    written_mqtt = _mqtt_device(written)["mqtt"]
    assert written_mqtt["device_id"] == "ROUTE-CLOUD"
    assert written_mqtt["product_key"] == "PK-CLOUD"


# --- broker profile resolution -------------------------------------------


def test_existing_profile_for_the_selected_endpoint_is_reused_unchanged(tmp_path):
    _write_config(
        tmp_path,
        _config(_local_device(), broker_refs=(BROKER_B1_REF, BROKER_B2_REF)),
    )
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    proposal = _local_proposal(BROKER_B2_HOST, "ROUTE-B2")
    _switch_connection(draft, "INV_1", proposal)

    _, written = _preview_and_payload(tmp_path, draft, loaded["revision"])

    assert _mqtt_device(written)["mqtt"]["broker_ref"] == BROKER_B2_REF
    assert written["zendure_mqtt"]["brokers"] == _broker_profiles(
        BROKER_B1_REF, BROKER_B2_REF
    )


def test_new_endpoint_persists_exactly_one_new_profile(tmp_path):
    _write_config(tmp_path, _config(_local_device()))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    proposal = _local_proposal("10.0.0.77", "ROUTE-NEW")
    _switch_connection(draft, "INV_1", proposal)

    _, written = _preview_and_payload(tmp_path, draft, loaded["revision"])

    brokers = written["zendure_mqtt"]["brokers"]
    assert set(brokers) == {BROKER_B1_REF, proposal["broker_ref"]}
    assert brokers[proposal["broker_ref"]]["host"] == "10.0.0.77"


def test_conflicting_broker_ref_blocks_preview_and_apply(tmp_path):
    _write_config(
        tmp_path,
        _config(
            _local_device(ref=BROKER_B2_REF, device_id="ROUTE-B2"),
            broker_refs=(BROKER_B1_REF, BROKER_B2_REF),
        ),
    )
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    proposal = _local_proposal("10.0.0.77", "ROUTE-NEW")
    item = _switch_connection(draft, "INV_1", proposal)
    # A third endpoint offered under a ref that already names a different
    # endpoint in the stored config must never silently replace that profile.
    item["broker"]["ref"] = BROKER_B1_REF
    item["mqtt"]["broker_ref"] = BROKER_B1_REF
    item["proposal_broker_ref"] = BROKER_B1_REF

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is False
    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "invalid", prepared
    assert "payload" not in prepared


def test_cloud_switch_keeps_the_credentials_ref_without_exposing_secrets(tmp_path):
    _connect_zendure_account()
    _write_config(tmp_path, _config(_local_device()))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    proposal = _cloud_proposal()
    _switch_connection(draft, "INV_1", proposal)

    preview, written = _preview_and_payload(tmp_path, draft, loaded["revision"])

    for config in (preview, written):
        profile = config["zendure_mqtt"]["brokers"][proposal["broker_ref"]]
        assert profile["credentials_ref"] == CLOUD_CREDENTIALS_REF
        assert "password" not in profile
        assert "username" not in profile
    assert "account-api-key" not in json.dumps(preview)


# --- no-op and ordinary edit safety --------------------------------------


def test_untouched_mqtt_device_stays_byte_identical(tmp_path):
    stored = _config(_local_device())
    path = _write_config(tmp_path, stored)
    loaded = load_maintenance_config(base_dir=str(tmp_path))

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))
    assert preview["changed"] is False, preview["diff"]
    prepared = prepare_maintenance_config_apply(
        loaded["draft"], loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok", prepared
    assert json.loads(prepared["payload"]) == json.loads(
        path.read_text(encoding="utf-8")
    )


def test_editing_only_common_values_keeps_the_connection_identity(tmp_path):
    _write_config(tmp_path, _config(_local_device()))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    item = _mqtt_draft_item(draft)
    item["max_power"] = 750
    item["min_soc"] = 15

    _, written = _preview_and_payload(tmp_path, draft, loaded["revision"])

    device = _mqtt_device(written)
    assert device["max_power"] == 750
    assert device["min_soc"] == 15
    assert device["mqtt"] == _local_device()["mqtt"]


def test_masked_cloud_route_values_survive_an_ordinary_edit(tmp_path):
    _write_config(tmp_path, _config(_cloud_device(), broker_refs=(CLOUD_REF,)))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    item = _mqtt_draft_item(draft)
    # The browser never sees the raw cloud route/product values.
    assert item["mqtt"]["device_id"] != "ROUTE-CLOUD"
    item["max_power"] = 700

    _, written = _preview_and_payload(tmp_path, draft, loaded["revision"])

    mqtt = _mqtt_device(written)["mqtt"]
    assert mqtt["device_id"] == "ROUTE-CLOUD"
    assert mqtt["product_key"] == "PK-CLOUD"
    assert mqtt["broker_ref"] == CLOUD_REF


def test_switch_preserves_name_enabled_and_common_values(tmp_path):
    device = _local_device()
    device["enabled"] = False
    _write_config(tmp_path, _config(device))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    proposal = _local_proposal(BROKER_B2_HOST, "ROUTE-B2")
    _switch_connection(draft, "INV_1", proposal)

    _, written = _preview_and_payload(tmp_path, draft, loaded["revision"])

    switched = _mqtt_device(written)
    assert switched["name"] == "INV_1"
    assert switched["enabled"] is False
    assert switched["max_power"] == 800
    assert switched["min_soc"] == 10
    assert switched["serial_number"] == SERIAL


def test_switch_without_a_proposal_id_still_replaces_the_connection(tmp_path):
    _write_config(tmp_path, _config(_local_device()))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    proposal = _local_proposal(BROKER_B2_HOST, "ROUTE-B2")
    item = _switch_connection(draft, "INV_1", proposal)
    # Discovery that emits no opaque proposal id must not silently degrade to
    # "new route on the old broker" — the invalid combination this fixes.
    item.pop("proposal_id")
    item.pop("proposal_broker_ref")

    _, written = _preview_and_payload(tmp_path, draft, loaded["revision"])

    mqtt = _mqtt_device(written)["mqtt"]
    assert mqtt["broker_ref"] == proposal["broker_ref"]
    assert mqtt["device_id"] == "ROUTE-B2"


# --- trust boundary ------------------------------------------------------


def test_tampered_connection_fields_cannot_rehome_a_device(tmp_path):
    _write_config(tmp_path, _config(_local_device()))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    proposal = _local_proposal(BROKER_B2_HOST, "ROUTE-B2")
    item = _switch_connection(draft, "INV_1", proposal)
    # A crafted draft claims the trusted b2 proposal but names a foreign
    # connection; the resolved selection, not the browser echo, must win.
    item["mqtt"]["broker_ref"] = "attacker_broker"
    item["mqtt"]["source"] = "zendure_cloud_mqtt"
    item["mqtt"]["topic_family"] = "legacy_zendure_json"

    resolved, error = _resolve_draft(draft, [proposal])
    assert error is None, error
    resolved_mqtt = _mqtt_draft_item(resolved)["mqtt"]
    assert resolved_mqtt["broker_ref"] == proposal["broker_ref"]
    assert resolved_mqtt["source"] == "local_mqtt"
    assert resolved_mqtt["topic_family"] == "zensdk_ha_scalar"

    _, written = _preview_and_payload(tmp_path, resolved, loaded["revision"])
    mqtt = _mqtt_device(written)["mqtt"]
    assert mqtt["broker_ref"] == proposal["broker_ref"]
    assert mqtt["source"] == "local_mqtt"
    assert "attacker_broker" not in json.dumps(written)


def test_stale_proposal_selection_fails_closed(tmp_path):
    _write_config(tmp_path, _config(_local_device()))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    _switch_connection(draft, "INV_1", _local_proposal(BROKER_B2_HOST, "ROUTE-B2"))

    resolved, error = _resolve_draft(draft, [])

    assert resolved is None
    assert error
