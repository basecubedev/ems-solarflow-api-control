# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stable Cloud proposal ids across serial enrichment (defect 3).

A Cloud proposal keeps a route-anchored selection id while its physical identity
is enriched from route-only to serial-bearing, and a stored route-only selection
still resolves afterwards via its trusted alias token. Tampered/stale selections
still fail closed, and independent broker/account scopes stay separate.
"""

import json

import pytest

from admin.models import SOURCE_ZENDURE_CLOUD_MQTT
from admin.zendure_mqtt_config_proposals import (
    annotate_identity_tokens,
    build_proposals,
    index_trusted_proposals,
    resolve_selected_proposals,
)

pytestmark = pytest.mark.simulation

TOKEN_KEY = b"identity-test-key-material-32b!!"


def _cloud_proposal(
    *,
    device_id="ROUTE-1",
    serial=None,
    broker_ref="zendure_cloud",
    product_key="PK",
    topic_family="legacy_zendure_json",
):
    fragment = {
        "type": "zendure_mqtt",
        "mqtt": {
            "broker_ref": broker_ref,
            "source": "zendure_cloud_mqtt",
            "device_id": device_id,
            "product_key": product_key,
            "topic_family": topic_family,
        },
    }
    if serial is not None:
        fragment["serial_number"] = serial
    return {
        "id": f"zendure-mqtt:{serial}" if serial is not None else f"zendure-mqtt:{device_id}",
        "broker_ref": broker_ref,
        "connection_source": "zendure_cloud_mqtt",
        "serial_number": serial,
        "device_id": device_id,
        "product_key": product_key,
        "topic_family": topic_family,
        "target": "device",
        "config_fragment": fragment,
        "seen_topics": [],
    }


def _local_proposal(*, device_id="ROUTE-L", serial=None, broker_ref="local_mqtt_a"):
    fragment = {
        "type": "zendure_mqtt",
        "mqtt": {
            "broker_ref": broker_ref,
            "source": "local_mqtt",
            "device_id": device_id,
            "product_key": "PK",
            "topic_family": "legacy_zendure_json",
        },
    }
    if serial is not None:
        fragment["serial_number"] = serial
    return {
        "id": f"zendure-mqtt:{serial or device_id}",
        "broker_ref": broker_ref,
        "connection_source": "local_mqtt",
        "serial_number": serial,
        "device_id": device_id,
        "product_key": "PK",
        "topic_family": "legacy_zendure_json",
        "target": "device",
        "config_fragment": fragment,
        "seen_topics": [],
    }


def _annotate(proposal):
    annotate_identity_tokens([proposal], TOKEN_KEY)
    return proposal


def _route_token(device_id="ROUTE-1", broker_ref="zendure_cloud"):
    route_only = _annotate(
        _cloud_proposal(device_id=device_id, serial=None, broker_ref=broker_ref)
    )
    return route_only["physical_identity_token"]


# --- stable proposal identity -----------------------------------------------


def test_route_only_cloud_id_is_route_token_anchored():
    proposal = _annotate(_cloud_proposal(serial=None))
    assert proposal["id"].startswith("zendure-mqtt:opaque:v1:")
    assert proposal["id"].endswith(":zendure_cloud")


def test_serial_enriched_same_route_keeps_the_same_cloud_id():
    route_only = _annotate(_cloud_proposal(device_id="ROUTE-1", serial=None))
    enriched = _annotate(_cloud_proposal(device_id="ROUTE-1", serial="SERIAL-1"))
    assert route_only["id"] == enriched["id"]
    # The serial becomes the primary identity, but the route alias is retained.
    assert enriched["physical_identity_token"] != route_only["physical_identity_token"]
    assert route_only["physical_identity_token"] in enriched[
        "physical_identity_alias_tokens"
    ]


def test_cloud_id_is_stable_across_topic_family_change():
    # Defect 2: the stable device anchor excludes topic family, so a stored
    # selection survives a topic-family change on one scoped device.
    scalar = _annotate(_cloud_proposal(device_id="ROUTE-1", topic_family="zensdk_ha_scalar"))
    legacy = _annotate(_cloud_proposal(device_id="ROUTE-1", topic_family="legacy_zendure_json"))
    assert scalar["id"] == legacy["id"]
    assert scalar["physical_identity_token"] == legacy["physical_identity_token"]


def test_cloud_id_is_stable_across_product_key_enrichment():
    # Defect 2: the anchor excludes the product key, so a route-only selection made
    # before the product key was known still resolves to the enriched proposal.
    route_only = _annotate(_cloud_proposal(device_id="ROUTE-1", product_key=None))
    enriched = _annotate(_cloud_proposal(device_id="ROUTE-1", product_key="PK-LATE"))
    assert route_only["id"] == enriched["id"]
    # The anchor token (browser equality) is unchanged; the precise route alias is
    # added by the known product key.
    assert route_only["physical_identity_token"] == enriched["physical_identity_token"]
    assert route_only["physical_identity_token"] in enriched[
        "physical_identity_alias_tokens"
    ]


def test_two_product_keys_on_one_cloud_device_id_keep_distinct_ids_and_tokens():
    # Defect 3: two known product keys on one device id are two distinct routes.
    # They never share a browser token (the shared anchor is withheld) and each
    # gets a distinct route-anchored id.
    def cand(product_key, metric):
        return {
            "broker_id": "cloud",
            "topic_family": "legacy_zendure_json",
            "device_id": "DEVICE-X",
            "serial_number": None,
            "product_key": product_key,
            "model_hint": "Hyper 2000",
            "metrics_seen": [metric],
            "topics_seen": [],
            "source_type": SOURCE_ZENDURE_CLOUD_MQTT,
            "tls_mode": "system_ca",
        }

    proposals = build_proposals([cand("PK-A", "outputLimit"), cand("PK-B", "inputLimit")])
    assert all(
        "identity_route_product_conflict" in p["warnings"] for p in proposals
    )
    annotate_identity_tokens(proposals, TOKEN_KEY)
    ids = [p["id"] for p in proposals]
    assert len(set(ids)) == 2
    # No shared browser identity token bridges the two routes.
    tokens = [p["physical_identity_token"] for p in proposals]
    assert len(set(tokens)) == 2
    alias_a, alias_b = (set(p.get("physical_identity_alias_tokens", [])) for p in proposals)
    assert alias_a.isdisjoint(alias_b)
    # Both survive the trusted index; neither route is dropped.
    assert len(index_trusted_proposals(proposals)) == 2


def test_conflicting_serials_on_one_cloud_route_keep_distinct_ids():
    # Two different serials contest one Cloud route: anchoring both to the shared
    # route token would collide and silently drop one from the trusted index, so a
    # conflicted proposal keeps its distinct serial-based id.
    def cand(serial):
        return {
            "broker_id": "cloud",
            "topic_family": "legacy_zendure_json",
            "device_id": "ROUTE-1",
            "serial_number": serial,
            "product_key": "PK",
            "model_hint": "Hyper 2000",
            "metrics_seen": ["outputLimit"],
            "topics_seen": [],
            "source_type": SOURCE_ZENDURE_CLOUD_MQTT,
            "tls_mode": "system_ca",
        }

    proposals = build_proposals([cand("S1"), cand("S2")])
    assert all(
        "identity_route_serial_conflict" in p["warnings"] for p in proposals
    )
    annotate_identity_tokens(proposals, TOKEN_KEY)
    ids = [p["id"] for p in proposals]
    assert len(set(ids)) == 2
    assert not any(pid.startswith("zendure-mqtt:opaque:v1:") for pid in ids)
    # Both survive the (id, broker_ref) trusted index — neither is dropped.
    assert len(index_trusted_proposals(proposals)) == 2


def test_different_cloud_account_scope_gets_a_distinct_id():
    a = _annotate(_cloud_proposal(device_id="ROUTE-1", broker_ref="zendure_cloud"))
    b = _annotate(_cloud_proposal(device_id="ROUTE-1", broker_ref="cloud_account_2"))
    assert a["id"] != b["id"]


def test_local_route_only_gets_route_token_id_with_generation():
    # A serial-less local route is route-primary, so it gets a route-token id and
    # keeps its discovery-generation suffix (freshness stays part of the id).
    route_only = _local_proposal(device_id="ROUTE-L", serial=None)
    route_only["discovery_generation"] = 7
    _annotate(route_only)
    assert route_only["id"].startswith("zendure-mqtt:opaque:v1:")
    assert route_only["id"].endswith(":g7")


def test_local_serial_bearing_keeps_serial_based_id():
    # The enrichment-stable id is a Cloud guarantee; a serial-bearing local
    # proposal keeps its serial-based id, so local generation freshness stays part
    # of the id (local enrichment is recovered by the alias-token remap instead).
    enriched = _local_proposal(device_id="ROUTE-L", serial="SERIAL-L")
    _annotate(enriched)
    assert not enriched["id"].startswith("zendure-mqtt:opaque:v1:")
    assert enriched["id"] == "zendure-mqtt:SERIAL-L"
    # The route alias token is still attached so the remap can recover a stored
    # local route-only selection after enrichment.
    route_token = _route_local_token()
    assert route_token in enriched["physical_identity_alias_tokens"]


def _route_local_token():
    route_only = _annotate(_local_proposal(device_id="ROUTE-L", serial=None))
    return route_only["physical_identity_token"]


def test_stored_local_route_only_selection_remaps_after_enrichment():
    enriched = _annotate(_local_proposal(device_id="ROUTE-L", serial="SERIAL-L"))
    stored = {
        "id": "zendure-mqtt:ROUTE-L",
        "broker_ref": "local_mqtt_a",
        "target": "device",
        "physical_identity_token": _route_local_token(),
    }
    resolved, errors = resolve_selected_proposals([stored], [enriched])
    assert errors == []
    assert resolved[0]["serial_number"] == "SERIAL-L"


# --- trust resolution across enrichment --------------------------------------


def test_stored_route_only_selection_resolves_after_enrichment():
    # A selection stored before the stable-id change carries an old id but the
    # trusted route alias token; it must remap to the enriched proposal.
    enriched = _annotate(_cloud_proposal(device_id="ROUTE-1", serial="SERIAL-1"))
    stored = {
        "id": "zendure-mqtt:ROUTE-1",
        "broker_ref": "zendure_cloud",
        "target": "device",
        "physical_identity_token": _route_token("ROUTE-1"),
    }
    resolved, errors = resolve_selected_proposals([stored], [enriched])
    assert errors == []
    assert resolved[0]["serial_number"] == "SERIAL-1"


def test_tampered_identity_token_is_rejected():
    enriched = _annotate(_cloud_proposal(device_id="ROUTE-1", serial="SERIAL-1"))
    sel = {
        "id": enriched["id"],
        "broker_ref": "zendure_cloud",
        "target": "device",
        "physical_identity_token": "opaque:v1:forged-token-value",
    }
    resolved, errors = resolve_selected_proposals([sel], [enriched])
    assert resolved == []
    assert errors[0]["code"] == "zendure_mqtt_proposal_conflict"


def test_tampered_broker_ref_is_not_remapped_across_scope():
    enriched = _annotate(_cloud_proposal(device_id="ROUTE-1", serial="SERIAL-1"))
    sel = {
        "id": "zendure-mqtt:ROUTE-1",
        "broker_ref": "cloud_account_2",
        "target": "device",
        "physical_identity_token": _route_token("ROUTE-1"),
    }
    resolved, errors = resolve_selected_proposals([sel], [enriched])
    assert resolved == []
    assert errors[0]["code"] == "zendure_mqtt_proposal_unknown"


def test_ambiguous_alias_remap_fails_closed():
    # A stored selection whose alias token matches two current trusted proposals
    # (same broker) is ambiguous and must fail closed rather than guess one.
    first = _annotate(_cloud_proposal(device_id="ROUTE-1", serial="SERIAL-1"))
    second = _annotate(_cloud_proposal(device_id="ROUTE-1", serial="SERIAL-2"))
    # Force distinct ids so both survive the trusted index but share the anchor.
    second["id"] = second["id"] + ":dup"
    shared_anchor = _route_token("ROUTE-1")
    stored = {
        "id": "zendure-mqtt:legacy",
        "broker_ref": "zendure_cloud",
        "target": "device",
        "physical_identity_token": shared_anchor,
    }
    resolved, errors = resolve_selected_proposals([stored], [first, second])
    assert resolved == []
    assert errors[0]["code"] == "zendure_mqtt_proposal_unknown"


def test_stale_unrelated_proposal_still_rejected():
    current = _annotate(_cloud_proposal(device_id="ROUTE-NEW", serial="SERIAL-NEW"))
    old = _annotate(_cloud_proposal(device_id="ROUTE-OLD", serial="SERIAL-OLD"))
    stored = {
        "id": old["id"],
        "broker_ref": "zendure_cloud",
        "target": "device",
        "physical_identity_token": old["physical_identity_token"],
        "physical_identity_alias_tokens": old["physical_identity_alias_tokens"],
    }
    resolved, errors = resolve_selected_proposals([stored], [current])
    assert resolved == []
    assert errors[0]["code"] == "zendure_mqtt_proposal_unknown"
    assert "SERIAL-OLD" not in json.dumps(resolved)


def _legacy_folded_token(device_id, product_key):
    # The exact token an old case-folded observation of the route would carry — the
    # legacy token a browser stored before route ids were compared case-sensitively.
    folded = _annotate(
        _cloud_proposal(device_id=device_id.casefold(), product_key=product_key.casefold())
    )
    return folded["physical_identity_token"]


def test_legacy_case_folded_token_remaps_to_exact_case_proposal():
    # Migration: a selection stored before route ids were case-sensitive carries a
    # case-folded token; it must still resolve to the current exact-case proposal.
    exact = _annotate(_cloud_proposal(device_id="ROUTE-UP", product_key="PK"))
    stored = {
        "id": "zendure-mqtt:stale-folded",
        "broker_ref": "zendure_cloud",
        "target": "device",
        "physical_identity_token": _legacy_folded_token("ROUTE-UP", "PK"),
    }
    resolved, errors = resolve_selected_proposals([stored], [exact], TOKEN_KEY)
    assert errors == []
    assert resolved[0]["device_id"] == "ROUTE-UP"


def test_legacy_case_folded_token_matching_two_routes_fails_closed():
    # Two current case-distinct routes fold to one legacy token; the remap must fail
    # closed rather than merge them.
    a = _annotate(_cloud_proposal(device_id="ROUTE-UP", product_key="PK"))
    b = _annotate(_cloud_proposal(device_id="Route-Up", product_key="Pk"))
    stored = {
        "id": "zendure-mqtt:stale-folded",
        "broker_ref": "zendure_cloud",
        "target": "device",
        "physical_identity_token": _legacy_folded_token("ROUTE-UP", "PK"),
    }
    resolved, errors = resolve_selected_proposals([stored], [a, b], TOKEN_KEY)
    assert resolved == []
    assert errors[0]["code"] == "zendure_mqtt_proposal_unknown"


def test_legacy_folded_token_colliding_with_lowercase_current_route_fails_closed():
    # The folded form of an uppercase route ("ROUTE-UP") is byte-identical to a
    # coexisting all-lowercase route's CURRENT token ("route-up"). A stale folded
    # selection must not silently bind to the lowercase device (a different write
    # address) — it fails closed even though the id/token hits the lowercase one.
    upper = _annotate(_cloud_proposal(device_id="ROUTE-UP", product_key="PK"))
    lower = _annotate(_cloud_proposal(device_id="route-up", product_key="pk"))
    stored = {
        "id": lower["id"],
        "broker_ref": "zendure_cloud",
        "target": "device",
        "physical_identity_token": _legacy_folded_token("ROUTE-UP", "PK"),
    }
    resolved, errors = resolve_selected_proposals([stored], [upper, lower], TOKEN_KEY)
    assert resolved == []
    assert errors[0]["code"] == "zendure_mqtt_proposal_unknown"


def test_legacy_remap_requires_token_key():
    # Without the token key the server cannot derive legacy tokens, so a stale
    # case-folded selection fails closed rather than silently resolving.
    exact = _annotate(_cloud_proposal(device_id="ROUTE-UP", product_key="PK"))
    stored = {
        "id": "zendure-mqtt:stale-folded",
        "broker_ref": "zendure_cloud",
        "target": "device",
        "physical_identity_token": _legacy_folded_token("ROUTE-UP", "PK"),
    }
    resolved, errors = resolve_selected_proposals([stored], [exact])
    assert resolved == []
    assert errors[0]["code"] == "zendure_mqtt_proposal_unknown"
