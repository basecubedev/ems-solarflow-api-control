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
    legacy_route_folded_tokens,
    mqtt_route_conflict,
    normalize_mqtt_route_segment,
    normalize_physical_serial,
    opaque_identity_token,
    resolve_inverter_identity,
    resolve_inverter_identity_evidence,
    same_inverter_evidence,
    same_physical_inverter_evidence,
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
        ("source", "local_mqtt"),
    ],
)
def test_serialless_mqtt_anchor_is_broker_and_account_scoped(change, value):
    # The stable device anchor is scoped by source and broker/account, so a route
    # observed under a different broker/account scope is a different device.
    first = _mqtt_device()
    second = _mqtt_device()
    second["mqtt"][change] = value

    left = resolve_inverter_identity(first)
    right = resolve_inverter_identity(second)

    assert left is not None and right is not None
    assert left.kind == right.kind == "scoped_mqtt_device_anchor"
    assert left.normalized_components != right.normalized_components
    assert opaque_identity_token(left, TOKEN_KEY) != opaque_identity_token(
        right, TOKEN_KEY
    )


def test_serialless_mqtt_anchor_survives_product_key_change():
    # Defect 2: a product-key change keeps the stable device anchor identical (the
    # anchor is the browser equality/selection handle) while the precise route
    # alias — the exact write address — changes.
    route_only = resolve_inverter_identity_evidence(
        _mqtt_device(product_key=""), token_key=TOKEN_KEY
    )
    enriched = resolve_inverter_identity_evidence(
        _mqtt_device(product_key="PRODUCT_B"), token_key=TOKEN_KEY
    )

    assert route_only is not None and enriched is not None
    assert route_only.primary.kind == enriched.primary.kind == "scoped_mqtt_device_anchor"
    # Stable anchor is unchanged across the enrichment.
    assert route_only.primary.opaque_token == enriched.primary.opaque_token
    # The known product key adds a distinct precise route alias.
    assert not any(a.kind == "scoped_mqtt_route" for a in route_only.aliases)
    route = next(a for a in enriched.aliases if a.kind == "scoped_mqtt_route")
    assert route.opaque_token != enriched.primary.opaque_token
    # Same anchor, missing vs known product key: enrichment, never a conflict.
    assert same_inverter_evidence(route_only, enriched) is True
    assert not identity_evidence_conflict(route_only, enriched)


def test_evidence_retains_route_alias_after_serial_enrichment():
    route_only = _mqtt_device(device_id="ROUTE_1234")
    serialized = _mqtt_device(device_id="ROUTE_1234", serial_number="SERIAL-001")

    before = resolve_inverter_identity_evidence(route_only, token_key=TOKEN_KEY)
    after = resolve_inverter_identity_evidence(serialized, token_key=TOKEN_KEY)

    assert before is not None and after is not None
    # Route-only: primary is the stable device anchor.
    assert before.primary.kind == "scoped_mqtt_device_anchor"
    # Serialized: primary is the serial, but the device anchor survives as an alias.
    assert after.primary.kind == "physical_serial"
    assert any(alias.kind == "scoped_mqtt_device_anchor" for alias in after.aliases)
    # The pre-enrichment anchor identity intersects the enriched evidence.
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


def test_evidence_same_device_id_two_known_product_keys_is_route_conflict():
    # Defect 3: two serial-less observations share a device id but carry different
    # known product keys — two distinct precise routes. They must not be treated as
    # one inverter (shared anchor alone cannot merge them).
    left = resolve_inverter_identity_evidence(
        _mqtt_device(device_id="DEVICE-X", product_key="PK-A"), token_key=TOKEN_KEY
    )
    right = resolve_inverter_identity_evidence(
        _mqtt_device(device_id="DEVICE-X", product_key="PK-B"), token_key=TOKEN_KEY
    )

    assert left is not None and right is not None
    assert left.route_conflict(right) is True
    # The shared anchor intersects, but the route conflict keeps them apart.
    assert not left.comparison_keys.isdisjoint(right.comparison_keys)
    assert same_inverter_evidence(left, right) is False


def test_evidence_missing_product_enriches_single_known_route():
    # Defect 3: a missing-product observation and a single known product route on
    # the same device id enrich into one inverter (no conflict).
    missing = resolve_inverter_identity_evidence(
        _mqtt_device(device_id="DEVICE-X", product_key=""), token_key=TOKEN_KEY
    )
    known = resolve_inverter_identity_evidence(
        _mqtt_device(device_id="DEVICE-X", product_key="PK-A"), token_key=TOKEN_KEY
    )

    assert missing is not None and known is not None
    assert missing.route_conflict(known) is False
    assert same_inverter_evidence(missing, known) is True


def test_physical_serial_normalizer_is_case_insensitive():
    assert normalize_physical_serial(" AbC-123 ") == normalize_physical_serial("abc-123")
    assert normalize_physical_serial(" AbC-123 ") == "abc-123"


def test_mqtt_route_segment_normalizer_preserves_case_and_rejects_masks():
    # Defect 2: MQTT topic segments are case-sensitive addresses.
    assert normalize_mqtt_route_segment(" PK-A ") == "PK-A"
    assert normalize_mqtt_route_segment("pk-a") != normalize_mqtt_route_segment("PK-A")
    for masked in ("••••", "…abcd", "<redacted>", "your_product_key", "YOUR_KEY", ""):
        assert normalize_mqtt_route_segment(masked) is None


def test_mqtt_route_segments_are_case_sensitive():
    # Defect 2: PK/DEV and pk/dev are distinct write addresses — distinct anchors,
    # routes and browser tokens — and must never collapse.
    upper = resolve_inverter_identity_evidence(
        _mqtt_device(product_key="PK", device_id="DEV"), token_key=TOKEN_KEY
    )
    lower = resolve_inverter_identity_evidence(
        _mqtt_device(product_key="pk", device_id="dev"), token_key=TOKEN_KEY
    )
    assert upper is not None and lower is not None
    assert upper.comparison_keys.isdisjoint(lower.comparison_keys)
    assert set(upper.opaque_tokens).isdisjoint(lower.opaque_tokens)
    assert same_physical_inverter_evidence(upper, lower) is False


def test_mqtt_device_id_case_distinguishes_routes():
    # Defect 2: only the device-id case differs — still two distinct routes.
    left = resolve_inverter_identity_evidence(
        _mqtt_device(product_key="PK", device_id="DEV"), token_key=TOKEN_KEY
    )
    right = resolve_inverter_identity_evidence(
        _mqtt_device(product_key="PK", device_id="Dev"), token_key=TOKEN_KEY
    )
    assert left.comparison_keys.isdisjoint(right.comparison_keys)


def test_same_serial_with_route_conflict_is_one_physical_inverter():
    # Related defect: the same physical serial observed on two precise product
    # routes is one physical inverter even though the write address is ambiguous.
    # Physical identity and route conflict are separate answers.
    left = resolve_inverter_identity_evidence(
        _mqtt_device(device_id="DEV", product_key="PK-A", serial_number="SERIAL-1"),
        token_key=TOKEN_KEY,
    )
    right = resolve_inverter_identity_evidence(
        _mqtt_device(device_id="DEV", product_key="PK-B", serial_number="SERIAL-1"),
        token_key=TOKEN_KEY,
    )
    assert left is not None and right is not None
    assert same_physical_inverter_evidence(left, right) is True
    assert same_inverter_evidence(left, right) is True
    assert mqtt_route_conflict(left, right) is True


def test_serialless_route_conflict_is_not_one_physical_inverter():
    # Without a shared serial, a shared device anchor with two known product keys
    # cannot prove one inverter; the route conflict keeps them apart.
    left = resolve_inverter_identity_evidence(
        _mqtt_device(device_id="DEVICE-X", product_key="PK-A"), token_key=TOKEN_KEY
    )
    right = resolve_inverter_identity_evidence(
        _mqtt_device(device_id="DEVICE-X", product_key="PK-B"), token_key=TOKEN_KEY
    )
    assert mqtt_route_conflict(left, right) is True
    assert same_physical_inverter_evidence(left, right) is False


def test_legacy_route_folded_token_remaps_uppercase_route_uniquely():
    # Migration: a case-folded token from a prior release must still identify the
    # current exact-case route, but never equal a current exact-case token.
    exact = resolve_inverter_identity_evidence(
        _mqtt_device(product_key="PK", device_id="DEV"), token_key=TOKEN_KEY
    )
    legacy = legacy_route_folded_tokens(exact, TOKEN_KEY)
    assert legacy
    folded = resolve_inverter_identity_evidence(
        _mqtt_device(product_key="pk", device_id="dev"), token_key=TOKEN_KEY
    )
    assert set(legacy) & set(folded.opaque_tokens)
    assert set(legacy).isdisjoint(exact.opaque_tokens)


def test_legacy_route_folded_tokens_empty_for_lowercase_route():
    lower = resolve_inverter_identity_evidence(
        _mqtt_device(product_key="pk", device_id="dev"), token_key=TOKEN_KEY
    )
    assert legacy_route_folded_tokens(lower, TOKEN_KEY) == ()


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


def test_topic_family_does_not_scope_the_stable_device_anchor():
    # Defect 2: the same scoped device observed under two topic families keeps one
    # stable anchor — topic family is schema evidence, never identity material — so
    # a stored selection survives a topic-family change.
    first = _mqtt_device(product_key="", topic_family="zensdk_ha_scalar")
    second = _mqtt_device(product_key="", topic_family="legacy_zendure_json")

    left = resolve_inverter_identity(first, token_key=TOKEN_KEY)
    right = resolve_inverter_identity(second, token_key=TOKEN_KEY)

    assert left is not None and right is not None
    assert left.kind == right.kind == "scoped_mqtt_device_anchor"
    assert left.normalized_components == right.normalized_components
    assert left.opaque_token == right.opaque_token


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
