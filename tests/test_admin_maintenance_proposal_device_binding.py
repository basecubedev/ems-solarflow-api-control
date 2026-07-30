# SPDX-License-Identifier: AGPL-3.0-or-later
"""A trusted proposal is only authorized for the device it belongs to.

Resolving a discovery proposal proves that a connection exists and that the
browser did not invent it. It does not prove that the connection belongs to the
configured inverter the draft names. Every check here therefore compares the
selected proposal against the *stored* device identified by ``original_name`` —
never against the browser's editable echo of that device, which a crafted draft
can rewrite or drop entirely.

Same-device evidence (shared physical serial, shared scoped route being enriched
with a serial) stays a legitimate connection switch.
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
from admin.zendure_mqtt_config_draft import TRUSTED_CONNECTION_SELECTION_FIELD

pytestmark = pytest.mark.simulation


@pytest.fixture
def install_root(isolated_install_root):
    """The resolved install root, so the draft resolver reads this config.

    ``_resolve_maintenance_mqtt_draft`` compares against the config at the
    resolved install path — exactly what the running Admin server does — so the
    stored config has to live there and not in an unrelated temp directory.
    """

    return isolated_install_root


IDENTITY_KEY = b"maintenance-binding-identity-32b"
BROKER_B1_REF = "local_b1"
BROKER_B1_HOST = "10.0.0.10"
BROKER_B2_HOST = "10.0.0.20"
CLOUD_HOST = "mqtt.zen-iot.com"
SERIAL_A = "SN-A"
SERIAL_OTHER = "SN-OTHER"
MISMATCH_CODE = "mqtt_proposal_identity_mismatch"


def _write_config(base_dir, data):
    config_dir = base_dir / "config"
    config_dir.mkdir(exist_ok=True)
    path = config_dir / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _mqtt_device(serial=SERIAL_A, device_id="ROUTE-A", ref=BROKER_B1_REF):
    device = {
        "name": "INV_1",
        "type": "zendure_mqtt",
        "enabled": True,
        "max_power": 800,
        "min_soc": 10,
        "mqtt": {
            "broker_ref": ref,
            "source": "local_mqtt",
            "topic_family": "zensdk_ha_scalar",
            "base_topic": "Zendure",
            "device_id": device_id,
        },
        "capabilities": {
            "read_power": True,
            "read_soc": True,
            "write_output_limit": False,
        },
    }
    if serial:
        device["serial_number"] = serial
    return device


def _config(device):
    return {
        "system": {"max_total_power": 1600},
        "devices": [
            {"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800},
            copy.deepcopy(device),
        ],
        "grid_meter": {"type": "shelly", "ip": "192.168.1.50"},
        "zendure_mqtt": {
            "brokers": {
                BROKER_B1_REF: {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": BROKER_B1_HOST,
                    "port": 1883,
                    "tls": False,
                }
            }
        },
        "dashboard": {"enabled": True, "port": 8080},
    }


def _annotated(observations):
    from admin.zendure_mqtt_config_proposals import (
        annotate_identity_tokens,
        build_proposals,
    )

    return annotate_identity_tokens(build_proposals(observations), IDENTITY_KEY)


def _local_proposal(host, device_id, serial):
    observation = {
        "source_type": "local_mqtt",
        "broker_host": host,
        "broker_port": 1883,
        "topic_family": "zensdk_ha_scalar",
        "device_id": device_id,
        "metrics_seen": ["electricLevel", "outputHomePower"],
    }
    if serial:
        observation["serial_number"] = serial
    return _annotated([observation])[0]


def _cloud_proposal(device_id="ROUTE-CLOUD", product_key="PK-CLOUD", serial=SERIAL_A):
    observation = {
        "source_type": "zendure_cloud_mqtt",
        "broker_host": CLOUD_HOST,
        "broker_port": 1883,
        "topic_family": "legacy_zendure_json",
        "device_id": device_id,
        "product_key": product_key,
        "metrics_seen": ["electricLevel", "outputHomePower"],
    }
    if serial:
        observation["serial_number"] = serial
    return _annotated([observation])[0]


def _draft_from_proposal(proposal, name):
    """Mirror the browser's ``mconfigZendureMqttDraftFromProposal``."""

    fragment = proposal["config_fragment"]
    mqtt = fragment.get("mqtt") or {}
    route = mqtt.get("device_id") or proposal.get("device_id") or ""
    item = {
        "kind": "zendure_mqtt",
        "original_name": None,
        "proposal_id": proposal.get("id") or "",
        "proposal_broker_ref": proposal.get("broker_ref") or "",
        "name": name,
        "enabled": True,
        "has_enabled_key": True,
        "serial_number": proposal.get("serial_number")
        or fragment.get("serial_number")
        or "",
        "device_id": route,
        "product_key": mqtt.get("product_key") or "",
        "output_control": False,
        "supports_output_control": False,
        "mqtt": {
            "broker_ref": mqtt.get("broker_ref") or "",
            "source": mqtt.get("source") or "",
            "topic_family": mqtt.get("topic_family") or "",
            "base_topic": mqtt.get("base_topic"),
            "device_id": route,
            "product_key": mqtt.get("product_key") or "",
        },
        "capabilities": {
            "read_power": True,
            "read_soc": True,
            "write_output_limit": False,
        },
        "broker": {
            "ref": proposal.get("broker_ref") or "",
            "host": proposal.get("broker_host") or "",
            "port": proposal.get("broker_port"),
            "tls": proposal.get("broker_tls") is True,
            "tls_insecure": proposal.get("broker_tls_insecure") is True,
            "tls_mode": proposal.get("broker_tls_mode") or "",
            "credentials_ref": proposal.get("credentials_ref") or "",
            "source": proposal.get("connection_source") or "",
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


def _resolve_draft(draft, proposals):
    from admin.server import AdminHandler

    class _Stub:
        server = types.SimpleNamespace(identity_token_key=IDENTITY_KEY)

        def _trusted_mqtt_proposals(self):
            return copy.deepcopy(list(proposals))

    return AdminHandler._resolve_maintenance_mqtt_draft(_Stub(), draft)


def _mqtt_item(draft, original_name="INV_1"):
    return next(
        entry
        for entry in draft["devices"]
        if entry.get("original_name") == original_name
    )


def _mqtt_config_device(config):
    matches = [d for d in config.get("devices") or [] if d.get("type") == "zendure_mqtt"]
    assert len(matches) == 1, config.get("devices")
    return matches[0]


def _preview(draft, base_dir):
    return preview_maintenance_config(
        draft, base_dir=str(base_dir), identity_token_key=IDENTITY_KEY
    )


def _prepare(draft, revision, base_dir):
    return prepare_maintenance_config_apply(
        draft, revision, base_dir=str(base_dir), identity_token_key=IDENTITY_KEY
    )


def _assert_merge_rejects(install_root, draft, revision):
    """The merge layer refuses on its own, whatever reached it."""

    marked = copy.deepcopy(draft)
    _mqtt_item(marked)[TRUSTED_CONNECTION_SELECTION_FIELD] = True

    preview = _preview(marked, install_root)
    assert preview["validation"]["ok"] is False, preview["validation"]
    codes = [issue["code"] for issue in preview["validation"]["errors"]]
    assert MISMATCH_CODE in codes, codes
    prepared = _prepare(marked, revision, install_root)
    assert prepared["status"] == "invalid", prepared
    assert "payload" not in prepared
    return preview["preview"]


def _assert_same_device_switch_applies(install_root, draft, revision, proposals):
    resolved, error = _resolve_draft(draft, proposals)
    assert error is None, error
    preview = _preview(resolved, install_root)
    assert preview["validation"]["ok"] is True, preview["validation"]["errors"]
    prepared = _prepare(resolved, revision, install_root)
    assert prepared["status"] == "ok", prepared
    return preview["preview"], json.loads(prepared["payload"])


# --- a proposal for another physical inverter is refused -------------------


def test_cross_device_proposal_cannot_replace_a_named_inverter(install_root):
    """1. stored SN-A + trusted proposal SN-OTHER + original_name INV_1."""

    _write_config(install_root, _config(_mqtt_device()))
    loaded = load_maintenance_config(base_dir=str(install_root))
    draft = loaded["draft"]
    other = _local_proposal(BROKER_B2_HOST, "ROUTE-OTHER", SERIAL_OTHER)
    _switch_connection(draft, "INV_1", other)

    resolved, error = _resolve_draft(draft, [other])

    assert resolved is None
    assert error
    preview = _assert_merge_rejects(install_root, draft, loaded["revision"])
    stored = _mqtt_config_device(preview)
    assert stored["serial_number"] == SERIAL_A
    assert stored["mqtt"]["device_id"] == "ROUTE-A"
    assert stored["mqtt"]["broker_ref"] == BROKER_B1_REF
    assert set(preview["zendure_mqtt"]["brokers"]) == {BROKER_B1_REF}


def test_dropping_the_physical_identity_token_does_not_bypass_the_check(install_root):
    """2. The stored device, not the browser echo, decides."""

    _write_config(install_root, _config(_mqtt_device()))
    loaded = load_maintenance_config(base_dir=str(install_root))
    draft = loaded["draft"]
    other = _local_proposal(BROKER_B2_HOST, "ROUTE-OTHER", SERIAL_OTHER)
    item = _switch_connection(draft, "INV_1", other)
    item.pop("physical_identity_token", None)

    resolved, error = _resolve_draft(draft, [other])

    assert resolved is None
    assert error
    _assert_merge_rejects(install_root, draft, loaded["revision"])


def test_dropping_serial_and_route_does_not_bypass_the_check(install_root):
    """3. An identity-less draft entry still cannot re-home a stored device."""

    _write_config(install_root, _config(_mqtt_device()))
    loaded = load_maintenance_config(base_dir=str(install_root))
    draft = loaded["draft"]
    other = _local_proposal(BROKER_B2_HOST, "ROUTE-OTHER", SERIAL_OTHER)
    item = _switch_connection(draft, "INV_1", other)
    item["serial_number"] = ""
    item["device_id"] = ""
    item["mqtt"]["device_id"] = ""
    item.pop("physical_identity_token", None)

    resolved, error = _resolve_draft(draft, [other])

    assert resolved is None
    assert error
    _assert_merge_rejects(install_root, draft, loaded["revision"])


def test_replacing_every_identity_field_with_the_proposal_is_still_refused(install_root):
    """4. A fully consistent foreign selection is still a different inverter."""

    _write_config(install_root, _config(_mqtt_device()))
    loaded = load_maintenance_config(base_dir=str(install_root))
    draft = loaded["draft"]
    other = _local_proposal(BROKER_B2_HOST, "ROUTE-OTHER", SERIAL_OTHER)
    item = _switch_connection(draft, "INV_1", other)
    # Every browser-owned identity field now agrees with the foreign proposal.
    assert item["serial_number"] == SERIAL_OTHER
    assert item["mqtt"]["device_id"] == "ROUTE-OTHER"
    assert item["physical_identity_token"] == other["physical_identity_token"]

    resolved, error = _resolve_draft(draft, [other])

    assert resolved is None
    assert error
    _assert_merge_rejects(install_root, draft, loaded["revision"])


def test_a_proposal_without_shared_identity_evidence_is_refused(install_root):
    """7. No serial and no shared scoped route proves nothing about the device."""

    _write_config(install_root, _config(_mqtt_device()))
    loaded = load_maintenance_config(base_dir=str(install_root))
    draft = loaded["draft"]
    anonymous = _local_proposal(BROKER_B2_HOST, "ROUTE-UNRELATED", None)
    _switch_connection(draft, "INV_1", anonymous)

    resolved, error = _resolve_draft(draft, [anonymous])

    assert resolved is None
    assert error
    _assert_merge_rejects(install_root, draft, loaded["revision"])


# --- same physical inverter stays switchable ------------------------------


def test_same_serial_on_another_broker_remains_a_valid_switch(install_root):
    """5. A shared trusted serial authorizes the connection change."""

    _write_config(install_root, _config(_mqtt_device()))
    loaded = load_maintenance_config(base_dir=str(install_root))
    draft = loaded["draft"]
    same = _local_proposal(BROKER_B2_HOST, "ROUTE-B2", SERIAL_A)
    _switch_connection(draft, "INV_1", same)

    preview, written = _assert_same_device_switch_applies(
        install_root, draft, loaded["revision"], [same]
    )

    for config in (preview, written):
        device = _mqtt_config_device(config)
        assert device["serial_number"] == SERIAL_A
        assert device["mqtt"]["broker_ref"] == same["broker_ref"]
        assert device["mqtt"]["device_id"] == "ROUTE-B2"
    assert written["zendure_mqtt"]["brokers"][same["broker_ref"]]["host"] == (
        BROKER_B2_HOST
    )


def test_route_only_device_enriched_by_a_serial_bearing_proposal(install_root):
    """6. The same scoped route gaining a serial is enrichment, not a swap."""

    _write_config(install_root, _config(_mqtt_device(serial=None, device_id="ROUTE-A")))
    loaded = load_maintenance_config(base_dir=str(install_root))
    draft = loaded["draft"]
    # Same endpoint as the stored local_b1 profile, same route, now with a serial.
    enriched = _local_proposal(BROKER_B1_HOST, "ROUTE-A", SERIAL_A)
    _switch_connection(draft, "INV_1", enriched)

    preview, written = _assert_same_device_switch_applies(
        install_root, draft, loaded["revision"], [enriched]
    )

    for config in (preview, written):
        device = _mqtt_config_device(config)
        assert device["serial_number"] == SERIAL_A
        assert device["mqtt"]["device_id"] == "ROUTE-A"
        # The already declared profile for that endpoint is reused, not duplicated.
        assert device["mqtt"]["broker_ref"] == BROKER_B1_REF
    assert set(written["zendure_mqtt"]["brokers"]) == {BROKER_B1_REF}


def test_local_to_cloud_switch_of_one_serial_remains_valid(install_root):
    """A cross-transport move of the same physical inverter is unaffected."""

    from admin.credential_store import CredentialStore

    CredentialStore().zendure.save_token("account-api-key")
    _write_config(install_root, _config(_mqtt_device()))
    loaded = load_maintenance_config(base_dir=str(install_root))
    draft = loaded["draft"]
    cloud = _cloud_proposal()
    _switch_connection(draft, "INV_1", cloud)

    _, written = _assert_same_device_switch_applies(
        install_root, draft, loaded["revision"], [cloud]
    )

    mqtt = _mqtt_config_device(written)["mqtt"]
    assert mqtt["source"] == "zendure_cloud_mqtt"
    assert mqtt["device_id"] == "ROUTE-CLOUD"
    assert mqtt["product_key"] == "PK-CLOUD"


def test_adding_a_foreign_proposal_as_a_new_device_stays_allowed(install_root):
    """A proposal that names no stored device adds an inverter as before."""

    _write_config(install_root, _config(_mqtt_device()))
    loaded = load_maintenance_config(base_dir=str(install_root))
    draft = loaded["draft"]
    other = _local_proposal(BROKER_B2_HOST, "ROUTE-OTHER", SERIAL_OTHER)
    added = _draft_from_proposal(other, "INV_2")
    draft["devices"].append(added)

    resolved, error = _resolve_draft(draft, [other])
    assert error is None, error
    preview = _preview(resolved, install_root)

    assert preview["validation"]["ok"] is True, preview["validation"]["errors"]
    serials = sorted(
        device.get("serial_number")
        for device in preview["preview"]["devices"]
        if device.get("type") == "zendure_mqtt"
    )
    assert serials == [SERIAL_A, SERIAL_OTHER]


def test_an_ordinary_manual_serial_correction_is_still_allowed(install_root):
    """A non-proposal edit keeps the documented manual-correction behavior."""

    _write_config(install_root, _config(_mqtt_device()))
    loaded = load_maintenance_config(base_dir=str(install_root))
    draft = loaded["draft"]
    item = _mqtt_item(draft)
    item["serial_number"] = "SN-A-CORRECTED"

    preview = _preview(draft, install_root)

    assert preview["validation"]["ok"] is True, preview["validation"]["errors"]
    assert _mqtt_config_device(preview["preview"])["serial_number"] == "SN-A-CORRECTED"
