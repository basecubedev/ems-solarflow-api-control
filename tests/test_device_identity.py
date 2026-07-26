# SPDX-License-Identifier: AGPL-3.0-or-later
"""Authoritative inverter identity and browser-token security contracts."""

import copy
import json
import os

import pytest

from admin.device_identity import IdentityTokenKeyError, IdentityTokenKeyStore
from admin.models import MqttHardwareCandidate
from admin.maintenance_config import (
    load_maintenance_config,
    preview_maintenance_config,
    redact_config_for_browser,
)
from admin.zendure_mqtt_config_proposals import build_proposals
from ems.device_identity import (
    broker_sources_from_config,
    identity_evidence_conflict,
    opaque_identity_token,
    resolve_inverter_identity,
    resolve_inverter_identity_evidence,
    same_inverter_evidence,
)

pytestmark = pytest.mark.simulation

TOKEN_KEY = b"identity-test-key-material-32b!!"


def _mqtt_device(
    *,
    source="zendure_cloud_mqtt",
    broker_ref="cloud_a",
    product_key="PRODUCT_A",
    device_id="ROUTE_1234",
    topic_family="legacy_zendure_json",
    serial_number=None,
):
    device = {
        "type": "zendure_mqtt",
        "mqtt": {
            "source": source,
            "broker_ref": broker_ref,
            "product_key": product_key,
            "device_id": device_id,
            "topic_family": topic_family,
        },
    }
    if serial_number is not None:
        device["serial_number"] = serial_number
    return device


def test_serial_is_primary_across_api_and_mqtt_transports():
    api = resolve_inverter_identity({"sn": " AbC-123 ", "ip": "192.0.2.4"})
    mqtt = resolve_inverter_identity(
        _mqtt_device(serial_number="abc-123", device_id="ACCOUNT_ROUTE")
    )

    assert api is not None
    assert mqtt is not None
    assert api.kind == mqtt.kind == "physical_serial"
    assert api.normalized_components == mqtt.normalized_components == ("abc-123",)


@pytest.mark.parametrize(
    ("change", "value"),
    [
        ("broker_ref", "cloud_b"),
        ("product_key", "PRODUCT_B"),
        ("source", "local_mqtt"),
    ],
)
def test_serialless_mqtt_route_identity_is_scoped(change, value):
    first = _mqtt_device()
    second = _mqtt_device()
    second["mqtt"][change] = value

    left = resolve_inverter_identity(first)
    right = resolve_inverter_identity(second)

    assert left is not None and right is not None
    assert left.kind == right.kind == "scoped_mqtt_route"
    assert left.normalized_components != right.normalized_components
    assert opaque_identity_token(left, TOKEN_KEY) != opaque_identity_token(
        right, TOKEN_KEY
    )


def test_evidence_retains_route_alias_after_serial_enrichment():
    route_only = _mqtt_device(device_id="ROUTE_1234")
    serialized = _mqtt_device(device_id="ROUTE_1234", serial_number="SERIAL-001")

    before = resolve_inverter_identity_evidence(route_only, token_key=TOKEN_KEY)
    after = resolve_inverter_identity_evidence(serialized, token_key=TOKEN_KEY)

    assert before is not None and after is not None
    # Route-only: primary is the scoped route.
    assert before.primary.kind == "scoped_mqtt_route"
    # Serialized: primary is the serial, but the scoped route survives as an alias.
    assert after.primary.kind == "physical_serial"
    assert any(alias.kind == "scoped_mqtt_route" for alias in after.aliases)
    # The pre-enrichment route identity intersects the enriched evidence.
    assert same_inverter_evidence(before, after) is True
    assert not identity_evidence_conflict(before, after)
    # Alias tokens intersect: the route token is present in both.
    assert set(before.opaque_tokens) & set(after.opaque_tokens)


def test_evidence_matches_serial_existing_against_route_only_proposal():
    serialized = _mqtt_device(device_id="ROUTE_1234", serial_number="SERIAL-001")
    route_only = _mqtt_device(device_id="ROUTE_1234")

    existing = resolve_inverter_identity_evidence(serialized, token_key=TOKEN_KEY)
    proposal = resolve_inverter_identity_evidence(route_only, token_key=TOKEN_KEY)

    assert same_inverter_evidence(existing, proposal) is True


def test_evidence_same_route_claiming_different_serial_is_a_conflict():
    serial_a = _mqtt_device(device_id="ROUTE_1234", serial_number="SERIAL-A")
    serial_b = _mqtt_device(device_id="ROUTE_1234", serial_number="SERIAL-B")

    left = resolve_inverter_identity_evidence(serial_a, token_key=TOKEN_KEY)
    right = resolve_inverter_identity_evidence(serial_b, token_key=TOKEN_KEY)

    assert identity_evidence_conflict(left, right) is True
    # A conflict is never treated as the same inverter.
    assert same_inverter_evidence(left, right) is False


def test_evidence_different_routes_with_serials_are_independent():
    first = _mqtt_device(device_id="ROUTE_A", serial_number="SERIAL-A")
    second = _mqtt_device(device_id="ROUTE_B", serial_number="SERIAL-B")

    left = resolve_inverter_identity_evidence(first, token_key=TOKEN_KEY)
    right = resolve_inverter_identity_evidence(second, token_key=TOKEN_KEY)

    assert same_inverter_evidence(left, right) is False
    assert identity_evidence_conflict(left, right) is False


def test_evidence_same_serial_with_additional_route_is_enrichment_not_conflict():
    first = _mqtt_device(device_id="ROUTE_A", serial_number="SERIAL-001")
    second = _mqtt_device(device_id="ROUTE_B", serial_number="SERIAL-001")

    left = resolve_inverter_identity_evidence(first, token_key=TOKEN_KEY)
    right = resolve_inverter_identity_evidence(second, token_key=TOKEN_KEY)

    assert same_inverter_evidence(left, right) is True
    assert identity_evidence_conflict(left, right) is False


def test_evidence_same_serial_under_different_cloud_scopes_matches():
    first = _mqtt_device(
        broker_ref="cloud_a", device_id="ROUTE_A", serial_number="SERIAL-001"
    )
    second = _mqtt_device(
        broker_ref="cloud_b", device_id="ROUTE_B", serial_number="SERIAL-001"
    )
    sources = {"cloud_a": "zendure_cloud_mqtt", "cloud_b": "zendure_cloud_mqtt"}

    left = resolve_inverter_identity_evidence(
        first, broker_sources=sources, token_key=TOKEN_KEY
    )
    right = resolve_inverter_identity_evidence(
        second, broker_sources=sources, token_key=TOKEN_KEY
    )

    assert same_inverter_evidence(left, right) is True


def test_evidence_opaque_tokens_contain_no_raw_identifiers():
    device = _mqtt_device(
        product_key="PRODUCT_SECRET",
        device_id="ROUTE_SECRET",
        serial_number="SERIAL-SECRET",
    )
    evidence = resolve_inverter_identity_evidence(device, token_key=TOKEN_KEY)

    assert evidence is not None
    for token in evidence.opaque_tokens:
        assert token.startswith("opaque:v1:")
        for raw in ("PRODUCT_SECRET", "ROUTE_SECRET", "SERIAL-SECRET"):
            assert raw not in token
            assert raw.lower() not in token.lower()


def test_topic_family_scopes_route_when_product_scope_is_unavailable():
    first = _mqtt_device(product_key="", topic_family="zensdk_ha_scalar")
    second = _mqtt_device(product_key="", topic_family="legacy_zendure_json")

    left = resolve_inverter_identity(first)
    right = resolve_inverter_identity(second)

    assert left is not None and right is not None
    assert left.normalized_components != right.normalized_components


def test_multiple_named_cloud_accounts_keep_distinct_broker_scopes():
    sources = {
        "cloud_a": "zendure_cloud_mqtt",
        "cloud_b": "zendure_cloud_mqtt",
    }
    left = resolve_inverter_identity(
        _mqtt_device(broker_ref="cloud_a"), broker_sources=sources
    )
    right = resolve_inverter_identity(
        _mqtt_device(broker_ref="cloud_b"), broker_sources=sources
    )

    assert left is not None and right is not None
    assert left.normalized_components != right.normalized_components


def test_opaque_token_is_stable_keyed_and_contains_no_raw_route_identity():
    identity = resolve_inverter_identity(_mqtt_device())
    assert identity is not None

    first = opaque_identity_token(identity, TOKEN_KEY)
    second = opaque_identity_token(identity, TOKEN_KEY)
    rotated = opaque_identity_token(identity, b"rotated-identity-key-material-32")

    assert first == second
    assert first.startswith("opaque:v1:")
    assert "ROUTE_1234" not in first
    assert "route_1234" not in first
    assert first != rotated


def test_serialless_local_api_uses_endpoint_fallback():
    identity = resolve_inverter_identity({"ip": " Example.Local ", "port": 8080})

    assert identity is not None
    assert identity.kind == "local_api_endpoint"
    assert identity.normalized_components == ("example.local", "8080")


@pytest.mark.parametrize("placeholder", ["<redacted>", "[redacted]", "redacted"])
def test_redaction_placeholders_are_not_identity_evidence(placeholder):
    assert resolve_inverter_identity(
        _mqtt_device(device_id=placeholder, product_key="KNOWN_PRODUCT")
    ) is None


def test_identity_key_store_creates_stable_restrictive_admin_state_key(tmp_path):
    store = IdentityTokenKeyStore(tmp_path / "state")

    first = store.load_or_create()
    second = store.load_or_create()

    assert first == second
    assert len(first) == 32
    assert store.path.read_bytes() == first
    if os.name == "posix":
        assert store.path.stat().st_mode & 0o777 == 0o600


def test_invalid_identity_key_fails_closed_instead_of_issuing_unkeyed_tokens(tmp_path):
    store = IdentityTokenKeyStore(tmp_path / "state")
    store.state_dir.mkdir(parents=True)
    store.path.write_bytes(b"not-a-valid-key")

    with pytest.raises(IdentityTokenKeyError, match="invalid"):
        store.load_or_create()


def _write_installed_config(tmp_path, config):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")


def test_serialless_cloud_config_round_trips_through_redacted_token(tmp_path):
    config = {
        "devices": [
            {
                "name": "WR ACCOUNT_ROUTE_1234",
                "type": "zendure_mqtt",
                "mqtt": {
                    "broker_ref": "cloud_a",
                    "topic_family": "legacy_zendure_json",
                    "product_key": "PRODUCT_A",
                    "device_id": "ACCOUNT_ROUTE_1234",
                    "write_topic": "iot/PRODUCT_A/ACCOUNT_ROUTE_1234/properties/write",
                },
                "capabilities": {
                    "read_power": True,
                    "read_soc": True,
                    "write_output_limit": False,
                },
            },
            {"name": "WR2", "ip": "192.0.2.10", "sn": "SERIAL-2"},
        ],
        "zendure_mqtt": {
            "brokers": {
                "cloud_a": {
                    "host": "mqtt.example.invalid",
                    "port": 8883,
                    "tls": True,
                    "source": "zendure_cloud_mqtt",
                    "credentials_ref": "zendure_cloud:cloud_a",
                }
            }
        },
    }
    _write_installed_config(tmp_path, config)
    key = b"maintenance-identity-token-key-32"

    loaded = load_maintenance_config(
        base_dir=str(tmp_path), identity_token_key=key
    )
    device = loaded["draft"]["devices"][0]
    token = device["physical_identity_token"]
    configured_identity = resolve_inverter_identity(
        config["devices"][0],
        broker_sources=broker_sources_from_config(config),
        token_key=key,
    )
    trusted_proposal = build_proposals(
        [
            MqttHardwareCandidate(
                broker_id="zendure-cloud",
                broker_host="mqtt.example.invalid",
                broker_port=8883,
                topic_family="legacy_zendure_json",
                device_id="ACCOUNT_ROUTE_1234",
                serial_number=None,
                product_key="PRODUCT_A",
                source_type="zendure_cloud_mqtt",
                tls_mode="system_ca",
            ).to_dict()
        ]
    )[0]
    explicit_cloud_proposal_identity = resolve_inverter_identity(
        trusted_proposal["config_fragment"],
        token_key=key,
    )
    assert configured_identity is not None
    assert explicit_cloud_proposal_identity is not None
    assert token == configured_identity.opaque_token
    assert token == explicit_cloud_proposal_identity.opaque_token
    assert token.startswith("opaque:v1:")
    assert device["mqtt"]["device_id"] == "••••"
    assert "ACCOUNT_ROUTE_1234" not in device["name"]
    assert "PRODUCT_A" not in str(device["mqtt"].get("effective_write_topic"))
    assert "ACCOUNT_ROUTE_1234" not in json.dumps(loaded)

    preview = preview_maintenance_config(
        loaded["draft"], base_dir=str(tmp_path), identity_token_key=key
    )

    assert not any(
        error["code"] == "device_identity_conflict"
        for error in preview["validation"]["errors"]
    )
    assert preview["changed"] is False
    assert preview["preview"]["devices"][0]["physical_identity_token"] == token


def test_valid_cloud_display_name_survives_load_and_preview_redaction(tmp_path):
    config = {
        "devices": [
            {
                "name": "Roof Serial-less",
                "type": "zendure_mqtt",
                "mqtt": {
                    "broker_ref": "cloud_a",
                    "topic_family": "legacy_zendure_json",
                    "product_key": "PRODUCT_A",
                    "device_id": "ACCOUNT_ROUTE_1234",
                },
                "capabilities": {
                    "read_power": True,
                    "read_soc": True,
                    "write_output_limit": False,
                },
            }
        ],
        "zendure_mqtt": {
            "brokers": {
                "cloud_a": {
                    "host": "mqtt.example.invalid",
                    "port": 8883,
                    "tls": True,
                    "source": "zendure_cloud_mqtt",
                    "credentials_ref": "zendure_cloud:cloud_a",
                }
            }
        },
    }
    _write_installed_config(tmp_path, config)
    key = b"maintenance-identity-token-key-32"

    loaded = load_maintenance_config(
        base_dir=str(tmp_path), identity_token_key=key
    )
    preview = preview_maintenance_config(
        loaded["draft"], base_dir=str(tmp_path), identity_token_key=key
    )

    assert loaded["draft"]["devices"][0]["name"] == "Roof Serial-less"
    assert preview["preview"]["devices"][0]["name"] == "Roof Serial-less"
    assert "ACCOUNT_ROUTE_1234" not in json.dumps(loaded)
    assert "ACCOUNT_ROUTE_1234" not in json.dumps(preview)


def test_conflicting_serial_and_server_token_fail_preview_closed(tmp_path):
    config = {
        "devices": [
            {"name": "WR1", "ip": "192.0.2.11", "sn": "SERIAL-A"},
            {"name": "WR2", "ip": "192.0.2.12", "sn": "SERIAL-B"},
        ]
    }
    _write_installed_config(tmp_path, config)
    key = b"maintenance-identity-token-key-32"
    loaded = load_maintenance_config(
        base_dir=str(tmp_path), identity_token_key=key
    )
    first, second = loaded["draft"]["devices"]
    first["physical_identity_token"] = second["physical_identity_token"]

    preview = preview_maintenance_config(
        loaded["draft"], base_dir=str(tmp_path), identity_token_key=key
    )

    assert preview["validation"]["ok"] is False
    assert any(
        error["code"] == "device_identity_conflict"
        for error in preview["validation"]["errors"]
    )


def _cloud_broker_config():
    return {
        "brokers": {
            "cloud_a": {
                "host": "mqtt.example.invalid",
                "port": 8883,
                "tls": True,
                "source": "zendure_cloud_mqtt",
                "credentials_ref": "zendure_cloud:cloud_a",
            }
        }
    }


def _cloud_device(name, device_id, *, serial_number=None, pv_kwp=None):
    device = {
        "name": name,
        "type": "zendure_mqtt",
        "mqtt": {
            "broker_ref": "cloud_a",
            "topic_family": "legacy_zendure_json",
            "product_key": "PRODUCT_A",
            "device_id": device_id,
        },
        "capabilities": {
            "read_power": True,
            "read_soc": True,
            "write_output_limit": False,
        },
    }
    if serial_number is not None:
        device["serial_number"] = serial_number
    if pv_kwp is not None:
        device["pv_kwp"] = pv_kwp
    return device


def test_route_only_device_enriched_by_serial_draft_stays_one_device(tmp_path):
    config = {
        "devices": [_cloud_device("Roof", "ACCOUNT_ROUTE_1234", pv_kwp=2.5)],
        "zendure_mqtt": _cloud_broker_config(),
    }
    _write_installed_config(tmp_path, config)
    key = b"maintenance-identity-token-key-32"

    # The user re-observes the same Cloud route, now with a physical serial, and
    # the draft item still points at the existing entry (original_name) but adds
    # the serial. It must enrich the one device, not create a duplicate.
    enriched = _cloud_device(
        "Roof", "ACCOUNT_ROUTE_1234", serial_number="SERIAL-001", pv_kwp=2.5
    )
    enriched["original_name"] = "Roof"
    draft = {"devices": [enriched]}

    preview = preview_maintenance_config(
        draft, base_dir=str(tmp_path), identity_token_key=key
    )

    assert not any(
        error["code"] == "device_identity_conflict"
        for error in preview["validation"]["errors"]
    )
    devices = [d for d in preview["preview"]["devices"] if d.get("type") == "zendure_mqtt"]
    assert len(devices) == 1
    merged = devices[0]
    assert merged["name"] == "Roof"
    assert merged.get("serial_number") == "SERIAL-001"
    assert merged.get("pv_kwp") == 2.5
    # The Cloud route id is preserved (never lost) but never leaked verbatim.
    assert "ACCOUNT_ROUTE_1234" not in json.dumps(preview)


def test_same_route_claiming_different_serial_fails_preview_closed(tmp_path):
    config = {
        "devices": [
            _cloud_device("Existing", "ACCOUNT_ROUTE_1234", serial_number="SERIAL-A")
        ],
        "zendure_mqtt": _cloud_broker_config(),
    }
    _write_installed_config(tmp_path, config)
    key = b"maintenance-identity-token-key-32"

    keep = _cloud_device(
        "Existing", "ACCOUNT_ROUTE_1234", serial_number="SERIAL-A"
    )
    keep["original_name"] = "Existing"
    # A second draft item claims the same Cloud route but a different serial.
    intruder = _cloud_device(
        "Intruder", "ACCOUNT_ROUTE_1234", serial_number="SERIAL-B"
    )
    draft = {"devices": [keep, intruder]}

    preview = preview_maintenance_config(
        draft, base_dir=str(tmp_path), identity_token_key=key
    )

    assert preview["validation"]["ok"] is False
    assert any(
        error["code"] == "device_identity_conflict"
        for error in preview["validation"]["errors"]
    )


def test_maintenance_masks_name_only_cloud_route_in_mixed_partial_config(tmp_path):
    route = "SECRET_CLOUD_ROUTE_7501"
    config = {
        "zendure_mqtt": {
            "brokers": {
                "cloud_a": {"source": "zendure_cloud_mqtt"},
                "local_a": {"source": "local_mqtt"},
            }
        },
        "devices": [
            {
                "name": "Local inverter",
                "type": "zendure_mqtt",
                "mqtt": {
                    "broker_ref": "local_a",
                    "device_id": "LOCAL_ROUTE_1234",
                    "topic_family": "legacy_zendure_json",
                },
                "capabilities": {
                    "read_power": True,
                    "read_soc": True,
                    "write_output_limit": False,
                },
            },
            {
                "name": f"Rejected Cloud {route}",
                "type": "zendure_mqtt",
                "mqtt": {
                    "broker_ref": "cloud_a",
                    "product_key": "KNOWN_PRODUCT",
                },
            },
        ],
    }
    _write_installed_config(tmp_path, config)
    key = b"maintenance-identity-token-key-32"

    loaded = load_maintenance_config(
        base_dir=str(tmp_path), identity_token_key=key
    )
    redacted = redact_config_for_browser(
        copy.deepcopy(config), identity_token_key=key
    )

    loaded_text = json.dumps(loaded)
    redacted_text = json.dumps(redacted)
    assert route not in loaded_text
    assert route not in redacted_text
    assert "Local inverter" in loaded_text
    assert "LOCAL_ROUTE_1234" in redacted_text
