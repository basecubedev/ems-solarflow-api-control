# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin adapter: discovery observations -> Zendure MQTT config proposals."""

import json
import subprocess
import sys
from types import SimpleNamespace

from admin.models import MqttHardwareCandidate
from admin.server import AdminHandler
from admin import zendure_mqtt_config_proposals as proposal_module
from admin.zendure_mqtt_config_proposals import (
    annotate_identity_tokens,
    build_proposals,
    proposals_from_brokers,
    proposals_from_sources,
)


def _scalar_candidate(**overrides):
    fields = dict(
        broker_id="b1",
        broker_host="10.0.0.5",
        broker_port=1883,
        topic_family="zensdk_ha_scalar",
        device_id="ABC123",
        serial_number="ABC123",
        model_hint="SolarFlow 800",
        metrics_seen=["electricLevel", "outputHomePower", "solarInputPower", "outputLimit"],
    )
    fields.update(overrides)
    return MqttHardwareCandidate(**fields)


def _legacy_candidate(**overrides):
    fields = dict(
        broker_id="b1",
        broker_host="10.0.0.5",
        broker_port=1883,
        topic_family="legacy_zendure_json",
        device_id="dev9",
        serial_number="ABC123",
        product_key="PKKEY",
        metrics_seen=["packInput", "inputLimit"],
    )
    fields.update(overrides)
    return MqttHardwareCandidate(**fields)


def test_converts_scalar_observation_into_proposal():
    proposals = build_proposals([_scalar_candidate().to_dict()])
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["source"] == "zendure_mqtt"
    assert proposal["device_id"] == "ABC123"
    assert proposal["topic_family"] == "zensdk_ha_scalar"
    assert proposal["role_hint"] == "battery_inverter_candidate"
    assert "battery_storage" in proposal["capabilities"]
    assert "output_control" in proposal["capabilities"]
    assert proposal["config_fragment"]["type"] == "zendure_mqtt"


def test_converts_legacy_json_observation_into_proposal():
    proposals = build_proposals([_legacy_candidate().to_dict()])
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["topic_family"] == "legacy_zendure_json"
    assert proposal["product_key"] == "PKKEY"
    assert proposal["config_fragment"]["mqtt"]["base_topic"] == "iot"


def test_deduplicates_observations_for_one_logical_device():
    # Same serial across a scalar and a legacy observation -> one proposal.
    proposals = build_proposals(
        [_scalar_candidate().to_dict(), _legacy_candidate().to_dict()]
    )
    assert len(proposals) == 1
    metrics = proposals[0]["metrics"]
    assert "outputLimit" in metrics and "inputLimit" in metrics


def test_proposal_carries_no_secrets():
    candidate = _scalar_candidate().to_dict()
    # Simulate transient/broker fields that must never reach a proposal.
    candidate["password"] = "hunter2"
    candidate["username"] = "admin"
    candidate["token"] = "sekret-token"
    candidate["app_key"] = "cloud-app-key"
    proposals = build_proposals([candidate])
    blob = repr(proposals).lower()
    for secret in ("hunter2", "sekret-token", "cloud-app-key", "password", "token"):
        assert secret not in blob
    assert proposals[0]["config_fragment"]["mqtt"]["app_key"] is None


def test_scalar_inverter_stays_telemetry_only():
    # A scalar (ZenSDK/HA) inverter has no verified output-write topic, so it
    # remains capability-based telemetry-only even with output_control observed.
    proposals = build_proposals([_scalar_candidate().to_dict()])
    proposal = proposals[0]
    caps = proposal["config_fragment"]["capabilities"]
    assert caps["write_output_limit"] is False
    assert proposal["output_control_supported"] is False
    # A known ZenSDK model on a scalar HA transport cannot take an MQTT power
    # write (it is controlled over the local HTTP API): transport-incompatible.
    assert proposal["output_control_reason"] == "transport_incompatible"


def _legacy_inverter_candidate(**overrides):
    fields = dict(
        broker_id="b1",
        broker_host="10.0.0.5",
        broker_port=1883,
        topic_family="legacy_zendure_json",
        device_id="dev9",
        serial_number="LEG123",
        product_key="PKKEY",
        model_hint="Hyper 2000",
        metrics_seen=["electricLevel", "outputHomePower", "outputLimit", "solarInputPower"],
    )
    fields.update(overrides)
    return MqttHardwareCandidate(**fields)


def test_legacy_inverter_with_output_control_is_controllable():
    # A discovered legacy inverter that reports outputLimit resolves a supported
    # write method, so Admin proposes it as a first-class controllable inverter.
    proposals = build_proposals([_legacy_inverter_candidate().to_dict()])
    assert len(proposals) == 1
    proposal = proposals[0]
    fragment = proposal["config_fragment"]
    assert fragment["capabilities"]["write_output_limit"] is True
    # The resolved hardware identity is pinned into config, not a write protocol.
    assert fragment["hardware_profile"] == "hyper_2000"
    assert fragment["power_write_profile"] == "legacy_object_device_automation"
    assert "write_protocol" not in fragment["mqtt"]
    assert proposal["output_control_supported"] is True
    assert proposal["output_control_reason"] == "legacy_object_device_automation"


def test_legacy_alt_layout_inverter_is_controllable():
    # The leading-slash "alt" JSON layout must not be treated as telemetry-only
    # merely because of its topic layout: the write method is the same.
    proposals = build_proposals(
        [_legacy_inverter_candidate(topic_family="legacy_zendure_json_alt").to_dict()]
    )
    proposal = proposals[0]
    fragment = proposal["config_fragment"]
    assert fragment["mqtt"]["topic_family"] == "legacy_zendure_json_alt"
    assert fragment["capabilities"]["write_output_limit"] is True
    assert proposal["output_control_supported"] is True


def test_ignores_unusable_observations_without_crashing():
    observations = [
        {"topic_family": "zensdk_ha_scalar"},  # no device id/serial
        {"device_id": "X"},  # no topic family
        "not-a-mapping",
        None,
        _scalar_candidate().to_dict(),  # one good one survives
    ]
    proposals = build_proposals(observations)
    assert len(proposals) == 1


def test_proposals_from_brokers_flattens_devices():
    brokers = [
        {"id": "b1", "devices": [_scalar_candidate().to_dict()]},
        {"id": "b2", "devices": []},
        {"id": "b3"},  # no devices key
        "junk",
    ]
    proposals = proposals_from_brokers(brokers)
    assert len(proposals) == 1


def test_accepts_candidate_objects_directly():
    proposals = build_proposals([_scalar_candidate()])
    assert len(proposals) == 1


def test_known_serial_is_written_top_level_in_config_fragment():
    proposals = build_proposals([_scalar_candidate().to_dict()])
    fragment = proposals[0]["config_fragment"]
    assert fragment["serial_number"] == "ABC123"
    # Serial lives at the top level where duplicate detection reads it.
    assert "serial_number" not in fragment["mqtt"]


def test_mqtt_without_serial_stays_valid_with_device_id():
    candidate = _scalar_candidate(serial_number=None, device_id="DEVONLY").to_dict()
    proposals = build_proposals([candidate])
    fragment = proposals[0]["config_fragment"]
    assert "serial_number" not in fragment
    assert fragment["mqtt"]["device_id"] == "DEVONLY"


def test_masked_product_key_is_dropped_from_fragment():
    candidate = _scalar_candidate(product_key="…abcd").to_dict()
    proposals = build_proposals([candidate])
    fragment = proposals[0]["config_fragment"]
    assert "product_key" not in fragment["mqtt"]
    assert proposals[0]["product_key"] is None


def test_masked_device_key_only_candidate_makes_no_proposal():
    candidate = _scalar_candidate(
        serial_number=None,
        device_id="••••",
        topic_family="device_list_only",
    ).to_dict()
    assert build_proposals([candidate]) == []


def test_device_list_only_with_serial_is_marked_waiting():
    candidate = _scalar_candidate(
        topic_family="device_list_only",
        metrics_seen=[],
    ).to_dict()
    proposals = build_proposals([candidate])
    assert len(proposals) == 1
    assert "waiting_for_mqtt_telemetry" in proposals[0]["warnings"]
    assert proposals[0]["config_fragment"]["serial_number"] == "ABC123"
    # A device-list-only candidate must not claim a real observed topic family.
    assert proposals[0]["topic_family"] == "unknown"


def test_observed_device_is_not_marked_waiting():
    proposals = build_proposals([_scalar_candidate().to_dict()])
    assert "waiting_for_mqtt_telemetry" not in proposals[0]["warnings"]


def test_importing_helper_does_not_require_paho_or_runtime_client():
    # Run in a subprocess so import side effects are isolated from this session.
    code = (
        "import sys;"
        "import admin.zendure_mqtt_config_proposals;"
        "import ems.zendure_mqtt.config_mapping;"
        "assert 'paho.mqtt.client' not in sys.modules, 'paho was imported';"
        "assert 'ems.zendure_mqtt.client' not in sys.modules, 'runtime client imported';"
        "assert 'ems.zendure_mqtt.service' not in sys.modules, 'runtime service imported';"
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def _cloud_candidate(**overrides):
    fields = dict(
        broker_id="zendure-cloud:mqtteu.zen-iot.com",
        broker_host="mqtteu.zen-iot.com",
        broker_port=8883,
        topic_family="zendure_cloud_scalar",
        device_id="CLOUDDEV",
        serial_number="CLOUDDEV",
        model_hint="SolarFlow 800",
        metrics_seen=["electricLevel", "outputHomePower"],
        source_type="zendure_cloud_mqtt",
    )
    fields.update(overrides)
    return MqttHardwareCandidate(**fields)


def test_local_proposal_gets_local_broker_ref():
    # A discovered local broker always gets a stable, endpoint-derived ref (never
    # the bare ``local_mqtt``), so its ref cannot change when another broker
    # appears later.
    proposals = build_proposals([_scalar_candidate().to_dict()])
    ref = proposals[0]["broker_ref"]
    assert ref.startswith("local_mqtt_")
    assert proposals[0]["config_fragment"]["mqtt"]["broker_ref"] == ref
    assert proposals[0]["config_fragment"]["mqtt"]["source"] == "local_mqtt"


def test_cloud_proposal_gets_cloud_broker_ref():
    proposals = build_proposals([_cloud_candidate().to_dict()])
    assert proposals[0]["broker_ref"] == "zendure_cloud"
    assert proposals[0]["config_fragment"]["mqtt"]["broker_ref"] == "zendure_cloud"
    assert proposals[0]["config_fragment"]["mqtt"]["source"] == "zendure_cloud_mqtt"


def test_serialless_cloud_route_remains_a_trusted_config_proposal():
    proposals = build_proposals(
        [
            _cloud_candidate(
                serial_number=None,
                device_id="ACCOUNT_ROUTE_1234",
                product_key="PRODUCT_SCOPE",
            ).to_dict()
        ]
    )

    assert len(proposals) == 1
    fragment = proposals[0]["config_fragment"]
    assert "serial_number" not in fragment
    assert fragment["mqtt"]["device_id"] == "ACCOUNT_ROUTE_1234"
    assert fragment["mqtt"]["product_key"] == "PRODUCT_SCOPE"


def test_public_serialless_cloud_proposal_exposes_only_opaque_identity(monkeypatch):
    route = "ACCOUNT_ROUTE_1234"
    product = "PRODUCT_ACCOUNT_A"
    topic = f"iot/{product}/{route}/properties/write"
    trusted = {
        "id": f"zendure-mqtt:{route}",
        "broker_ref": "cloud_a",
        "connection_source": "zendure_cloud_mqtt",
        "device_id": route,
        "serial_number": None,
        "product_key": product,
        "display_name": f"Zendure {route}",
        "reason": f"last publish {topic}",
        "seen_topics": [topic],
        "config_fragment": {
            "type": "zendure_mqtt",
            "name": f"Zendure {route}",
            "mqtt": {
                "source": "zendure_cloud_mqtt",
                "broker_ref": "cloud_a",
                "topic_family": "legacy_zendure_json",
                "device_id": route,
                "write_topic": topic,
            },
        },
    }
    monkeypatch.setattr(
        proposal_module,
        "proposals_from_sources",
        lambda local, cloud: [trusted],
    )
    handler = object.__new__(AdminHandler)
    handler.server = SimpleNamespace(
        identity_token_key=b"public-proposal-identity-key-32b",
        mqtt_discovery=SimpleNamespace(candidates=lambda: []),
        zendure_cloud_discovery=SimpleNamespace(trusted_candidates=lambda: []),
    )

    proposal = handler._public_mqtt_proposals()[0]
    flattened = json.dumps(proposal)

    assert proposal["physical_identity_token"].startswith("opaque:v1:")
    assert proposal["id"].startswith("zendure-mqtt:opaque:v1:")
    assert proposal["device_id"] == "…1234"
    assert route not in flattened
    assert product not in flattened
    assert topic not in flattened


def test_server_issues_distinct_opaque_ids_for_same_local_route_on_two_brokers(
    monkeypatch,
):
    route = "LOCAL_SHARED_ROUTE"

    def trusted(ref):
        return {
            "id": f"zendure-mqtt:{route}",
            "broker_ref": ref,
            "connection_source": "local_mqtt",
            "device_id": route,
            "serial_number": None,
            "config_fragment": {
                "type": "zendure_mqtt",
                "mqtt": {
                    "source": "local_mqtt",
                    "broker_ref": ref,
                    "topic_family": "zensdk_ha_scalar",
                    "device_id": route,
                },
            },
        }

    monkeypatch.setattr(
        proposal_module,
        "proposals_from_sources",
        lambda local, cloud: [trusted("garage"), trusted("shed")],
    )
    handler = object.__new__(AdminHandler)
    handler.server = SimpleNamespace(
        identity_token_key=b"public-proposal-identity-key-32b",
        mqtt_discovery=SimpleNamespace(candidates=lambda: []),
        zendure_cloud_discovery=SimpleNamespace(trusted_candidates=lambda: []),
    )

    proposals = handler._public_mqtt_proposals()

    assert len({proposal["id"] for proposal in proposals}) == 2
    assert len(
        {proposal["physical_identity_token"] for proposal in proposals}
    ) == 2
    assert all(
        proposal["id"].startswith("zendure-mqtt:opaque:v1:")
        for proposal in proposals
    )


def test_public_proposals_mask_cloud_route_from_other_scope_candidate(monkeypatch):
    route = "ACCOUNT_ROUTE_SHARED_ACROSS_SCOPES"
    product = "PRODUCT_ACCOUNT_SCOPE"

    def trusted(source, ref):
        return {
            "id": f"zendure-mqtt:{route}:{ref}",
            "broker_ref": ref,
            "connection_source": source,
            "device_id": route,
            "serial_number": None,
            "product_key": product if source == "zendure_cloud_mqtt" else None,
            "display_name": f"Zendure {route}",
            "config_fragment": {
                "type": "zendure_mqtt",
                "name": f"Zendure {route}",
                "mqtt": {
                    "source": source,
                    "broker_ref": ref,
                    "topic_family": "legacy_zendure_json",
                    "device_id": route,
                    "product_key": (
                        product if source == "zendure_cloud_mqtt" else None
                    ),
                },
            },
        }

    monkeypatch.setattr(
        proposal_module,
        "proposals_from_sources",
        lambda local, cloud: [
            trusted("zendure_cloud_mqtt", "cloud"),
            trusted("local_mqtt", "garage"),
        ],
    )
    handler = object.__new__(AdminHandler)
    handler.server = SimpleNamespace(
        identity_token_key=b"public-proposal-identity-key-32b",
        mqtt_discovery=SimpleNamespace(candidates=lambda: []),
        zendure_cloud_discovery=SimpleNamespace(trusted_candidates=lambda: []),
    )

    proposals = handler._public_mqtt_proposals()
    flattened = json.dumps(proposals)

    assert len(proposals) == 2
    assert len({proposal["physical_identity_token"] for proposal in proposals}) == 2
    assert route not in flattened
    assert product not in flattened


def test_same_physical_serial_on_local_and_cloud_stays_two_connections():
    # One physical inverter reachable over two transports is two connection
    # alternatives, not a duplicate observation: suppressing either one would
    # make that direction of the switch unofferable.
    proposals = build_proposals(
        [_scalar_candidate().to_dict(), _cloud_candidate(serial_number="ABC123", device_id="ABC123").to_dict()]
    )
    assert len(proposals) == 2

    combined = proposals_from_sources(
        [{"devices": [_scalar_candidate().to_dict()]}],
        [
            _cloud_candidate(
                serial_number=" abc123 ", device_id="CLOUD_ROUTE"
            ).to_dict()
        ],
    )

    combined = annotate_identity_tokens(
        combined, b"proposal-alternatives-identity-32b"
    )
    assert len(combined) == 2
    assert {proposal["connection_source"] for proposal in combined} == {
        "local_mqtt",
        "zendure_cloud_mqtt",
    }
    assert len({proposal["id"] for proposal in combined}) == 2
    assert len({proposal["broker_ref"] for proposal in combined}) == 2
    # Both connections still describe one physical inverter.
    tokens = {
        proposal["physical_identity_token"]
        for proposal in annotate_identity_tokens(
            combined, b"proposal-alternatives-identity-32b"
        )
    }
    assert len(tokens) == 1


def test_duplicate_local_observations_of_one_connection_collapse():
    combined = proposals_from_sources(
        [
            {
                "devices": [
                    _scalar_candidate().to_dict(),
                    _scalar_candidate().to_dict(),
                ]
            }
        ],
        [],
    )
    assert len(combined) == 1


def test_two_local_brokers_with_one_serial_stay_two_connections():
    combined = proposals_from_sources(
        [
            {"devices": [_scalar_candidate(broker_host="10.0.0.5").to_dict()]},
            {"devices": [_scalar_candidate(broker_host="10.0.0.6").to_dict()]},
        ],
        [],
    )

    assert len(combined) == 2
    assert len({proposal["broker_ref"] for proposal in combined}) == 2
    assert all(
        proposal["connection_source"] == "local_mqtt" for proposal in combined
    )


def test_local_b1_b2_and_cloud_are_three_alternatives_for_one_serial():
    combined = proposals_from_sources(
        [
            {"devices": [_scalar_candidate(broker_host="10.0.0.5").to_dict()]},
            {"devices": [_scalar_candidate(broker_host="10.0.0.6").to_dict()]},
        ],
        [_cloud_candidate(serial_number="ABC123", device_id="CLOUD_ROUTE").to_dict()],
    )

    combined = annotate_identity_tokens(
        combined, b"proposal-alternatives-identity-32b"
    )
    assert len(combined) == 3
    assert len({proposal["broker_ref"] for proposal in combined}) == 3
    # A selection names (id, broker_ref); two local brokers legitimately share a
    # serial-anchored id, so the pair — not the id alone — has to stay distinct.
    assert (
        len({(proposal["id"], proposal["broker_ref"]) for proposal in combined}) == 3
    )
    assert sorted(proposal["connection_source"] for proposal in combined) == [
        "local_mqtt",
        "local_mqtt",
        "zendure_cloud_mqtt",
    ]


def test_a_trusted_selection_resolves_either_alternative():
    from admin.zendure_mqtt_config_proposals import resolve_selected_proposals

    key = b"proposal-alternatives-key-32byte"
    combined = annotate_identity_tokens(
        proposals_from_sources(
            [{"devices": [_scalar_candidate().to_dict()]}],
            [
                _cloud_candidate(
                    serial_number="ABC123", device_id="CLOUD_ROUTE"
                ).to_dict()
            ],
        ),
        key,
    )

    for proposal in combined:
        selected, errors = resolve_selected_proposals(
            [{"id": proposal["id"], "broker_ref": proposal["broker_ref"]}],
            combined,
            key,
        )
        assert not errors, errors
        assert selected[0]["connection_source"] == proposal["connection_source"]
        assert selected[0]["broker_ref"] == proposal["broker_ref"]


def test_public_proposals_keep_both_alternatives_without_exposing_cloud_ids(
    monkeypatch,
):
    route = "CLOUD_ACCOUNT_ROUTE"
    product = "CLOUD_PRODUCT_KEY"
    combined = proposals_from_sources(
        [{"devices": [_scalar_candidate().to_dict()]}],
        [
            _cloud_candidate(
                serial_number="ABC123", device_id=route, product_key=product
            ).to_dict()
        ],
    )
    monkeypatch.setattr(
        proposal_module, "proposals_from_sources", lambda local, cloud: combined
    )
    handler = object.__new__(AdminHandler)
    handler.server = SimpleNamespace(
        identity_token_key=b"public-proposal-identity-key-32b",
        mqtt_discovery=SimpleNamespace(candidates=lambda: []),
        zendure_cloud_discovery=SimpleNamespace(trusted_candidates=lambda: []),
    )

    public = handler._public_mqtt_proposals()
    flattened = json.dumps(public)

    assert len(public) == 2
    assert {proposal["connection_source"] for proposal in public} == {
        "local_mqtt",
        "zendure_cloud_mqtt",
    }
    assert route not in flattened
    assert product not in flattened


def test_same_raw_route_on_local_and_cloud_stays_separate_without_serial():
    route = "SHARED_ACCOUNT_SCOPED_ROUTE"
    combined = proposals_from_sources(
        [
            {
                "devices": [
                    _scalar_candidate(
                        serial_number=None,
                        device_id=route,
                    ).to_dict()
                ]
            }
        ],
        [
            _cloud_candidate(
                serial_number=None,
                device_id=route,
            ).to_dict()
        ],
    )

    assert len(combined) == 2
    assert {proposal["connection_source"] for proposal in combined} == {
        "local_mqtt",
        "zendure_cloud_mqtt",
    }


def _d0_local_candidate(**overrides):
    fields = dict(
        broker_id="b1",
        broker_host="10.0.0.5",
        broker_port=1883,
        topic_family="zensdk_ha_scalar",
        device_id="D0SN",
        serial_number="D0SN",
        metrics_seen=["totalPower"],
        topics_seen=["Zendure/sensor/D0SN/totalPower"],
        source_type="local_mqtt",
    )
    fields.update(overrides)
    return MqttHardwareCandidate(**fields)


def test_local_d0_observation_becomes_grid_meter_proposal():
    proposals = build_proposals([_d0_local_candidate().to_dict()])
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["role_hint"] == "grid_meter_candidate"
    assert proposal["target"] == "grid_meter"
    assert "Zendure/sensor/D0SN/totalPower" in proposal["seen_topics"]
    fragment = proposal["grid_meter_fragment"]
    assert fragment["type"] == "zendure_smartmeter_d0"
    assert fragment["mqtt"]["broker_ref"].startswith("local_mqtt_")
    assert fragment["mqtt"]["broker_ref"] == proposal["broker_ref"]
    assert fragment["mqtt"]["topic"] == "Zendure/sensor/D0SN/totalPower"
    assert fragment["mqtt"]["payload_format"] == "number"


def test_cloud_secret_prefixed_topics_never_reach_proposal_output():
    secret = "SECRET_APP_KEY"
    candidate = _d0_local_candidate(
        source_type="zendure_cloud_mqtt",
        topics_seen=[f"{secret}/sensor/D0SN/totalPower"],
    )
    proposals = build_proposals([candidate.to_dict()])
    assert len(proposals) == 1
    proposal = proposals[0]
    # No secret-prefixed topic is carried, and cloud never auto-maps to D0.
    assert proposal["target"] == "device"
    assert proposal["grid_meter_fragment"] is None
    import json

    assert secret not in json.dumps(proposal)


# --- deterministic collision-safe broker refs (two local brokers) -----------
def _local_on(broker_id, host, **overrides):
    return _legacy_candidate(
        broker_id=broker_id, broker_host=host, device_id=f"dev-{broker_id}",
        serial_number=f"SN-{broker_id}", **overrides
    ).to_dict()


def _refs_for(candidates):
    return [p["broker_ref"] for p in build_proposals(candidates)]


def test_two_local_brokers_get_distinct_deterministic_refs():
    refs = _refs_for([_local_on("A-B", "10.0.0.10"), _local_on("A_B", "10.0.0.20")])
    # Two distinct brokers, so neither keeps the bare backward-compatible ref.
    assert len(set(refs)) == 2
    assert all(ref.startswith("local_mqtt_") for ref in refs)


def test_slug_colliding_broker_ids_do_not_share_a_ref():
    # ``A-B`` and ``A_B`` both normalize to the ``a_b`` slug; the identity hash
    # keeps them apart regardless.
    refs = _refs_for([_local_on("A-B", "10.0.0.10"), _local_on("A_B", "10.0.0.20")])
    assert refs[0] != refs[1]


def test_broker_refs_are_independent_of_discovery_order():
    forward = _refs_for([_local_on("A-B", "10.0.0.10"), _local_on("A_B", "10.0.0.20")])
    reversed_ = _refs_for([_local_on("A_B", "10.0.0.20"), _local_on("A-B", "10.0.0.10")])
    assert dict(zip(["A-B", "A_B"], forward)) == {
        "A-B": reversed_[1],
        "A_B": reversed_[0],
    }


def test_repeated_preview_generation_is_stable():
    candidates = [_local_on("A-B", "10.0.0.10"), _local_on("A_B", "10.0.0.20")]
    assert _refs_for(candidates) == _refs_for(candidates)


def test_same_broker_identity_yields_same_ref_across_runs():
    a = _refs_for([_local_on("A-B", "10.0.0.10"), _local_on("other", "10.0.0.30")])
    b = _refs_for([_local_on("A-B", "10.0.0.10"), _local_on("other", "10.0.0.30")])
    assert a == b


def test_broker_refs_never_embed_credentials():
    # Broker identity (and therefore the display ref) is built from non-secret
    # fields only; a transient credential smuggled onto the observation dict must
    # neither change the ref nor leak into it.
    base = [_local_on("A-B", "10.0.0.10"), _local_on("A_B", "10.0.0.20")]
    baseline = _refs_for(base)

    tainted = [dict(base[0], password="hunter2", username="admin", token="tok"), base[1]]
    tainted_refs = _refs_for(tainted)
    assert tainted_refs == baseline
    refs_blob = " ".join(tainted_refs)
    for secret in ("hunter2", "admin", "tok"):
        assert secret not in refs_blob


# --- TLS + credential metadata propagation (defect 1) -----------------------
def test_plain_broker_proposal_has_no_tls():
    proposal = build_proposals([_scalar_candidate(tls_mode="plaintext").to_dict()])[0]
    assert proposal["broker_tls"] is False
    assert proposal["broker_tls_insecure"] is False


def test_system_ca_broker_proposal_preserves_secure_tls():
    proposal = build_proposals([_scalar_candidate(tls_mode="system_ca").to_dict()])[0]
    assert proposal["broker_tls"] is True
    assert proposal["broker_tls_insecure"] is False
    assert proposal["broker_tls_mode"] == "system_ca"


def test_insecure_broker_proposal_preserves_insecure_flag():
    proposal = build_proposals(
        [_scalar_candidate(tls_mode="insecure_no_verify").to_dict()]
    )[0]
    assert proposal["broker_tls"] is True
    assert proposal["broker_tls_insecure"] is True
    assert proposal["broker_tls_mode"] == "insecure_no_verify"


def test_credentials_ref_survives_into_proposal():
    candidate = _scalar_candidate(tls_mode="system_ca", credentials_ref="cred-1")
    proposal = build_proposals([candidate.to_dict()])[0]
    assert proposal["credentials_ref"] == "cred-1"
    # No secret field is ever carried, only the non-secret reference.
    import json

    blob = json.dumps(proposal)
    for secret in ("password", "hunter2", "token"):
        assert secret not in blob


# --- ref stability under topology change (scenario 7) -----------------------
def test_broker_a_keeps_ref_when_broker_b_is_discovered():
    a_alone = build_proposals([_local_on("A", "10.0.0.10")])[0]["broker_ref"]
    together = {
        p["broker_ref"]
        for p in build_proposals(
            [_local_on("A", "10.0.0.10"), _local_on("B", "10.0.0.20")]
        )
    }
    assert a_alone in together
    assert len(together) == 2


# --- same broker_id, different endpoints must not merge (scenario 8) ---------
def test_same_broker_id_different_host_yields_two_refs():
    refs = _refs_for(
        [_local_on("home", "10.0.0.10"), _local_on("home", "10.0.0.20")]
    )
    assert len(set(refs)) == 2


def test_same_host_different_tls_mode_yields_two_refs():
    refs = _refs_for(
        [
            _local_on("home", "10.0.0.10", broker_port=8883, tls_mode="system_ca"),
            _local_on("home", "10.0.0.10", broker_port=8883, tls_mode="insecure_no_verify"),
        ]
    )
    assert len(set(refs)) == 2


# --- combined trusted source: local brokers + cloud candidates ---------------
def _cloud_list_only(**overrides):
    """A realistic deviceList-only candidate as ZendureCloudDiscovery emits it."""

    fields = dict(
        broker_id="zendure-cloud:mqtt.example.invalid",
        broker_host="mqtt.example.invalid",
        broker_port=8883,
        topic_family="device_list_only",
        device_id="SN-CLOUD1",
        serial_number="SN-CLOUD1",
        model_hint="SolarFlow 800",
        metrics_seen=[],
        source_type="zendure_cloud_mqtt",
        tls_mode="encrypted_no_verify",
    )
    fields.update(overrides)
    return MqttHardwareCandidate(**fields)


def _broker_with(devices, **overrides):
    broker = {"id": "mqtt:10.0.0.5:1883", "host": "10.0.0.5", "port": 1883,
              "devices": devices}
    broker.update(overrides)
    return broker


def test_sources_include_cloud_candidates_as_cloud_proposals():
    proposals = proposals_from_sources([], [_cloud_candidate().to_dict()])
    assert len(proposals) == 1
    assert proposals[0]["broker_ref"] == "zendure_cloud"
    assert proposals[0]["serial_number"] == "CLOUDDEV"
    assert proposals[0]["config_fragment"]["mqtt"]["broker_ref"] == "zendure_cloud"


def test_sources_accept_admin_cloud_tls_mode():
    # ZendureCloudDiscovery candidates carry the Admin-only mode
    # ``encrypted_no_verify``; they must resolve to a TLS endpoint instead of
    # being dropped as an unknown TLS mode, and no Admin-only mode string may
    # leak into the proposal endpoint.
    proposals = proposals_from_sources([], [_cloud_list_only().to_dict()])
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["broker_ref"] == "zendure_cloud"
    assert proposal["broker_host"] == "mqtt.example.invalid"
    assert proposal["broker_tls"] is True
    assert proposal["broker_tls_insecure"] is True
    assert proposal.get("broker_tls_mode") != "encrypted_no_verify"
    assert "waiting_for_mqtt_telemetry" in proposal["warnings"]


def test_sources_exclude_masked_only_cloud_candidates():
    masked = _cloud_list_only(serial_number=None, device_id="…abcd").to_dict()
    assert proposals_from_sources([], [masked]) == []


def test_sources_offer_both_connections_for_a_device_seen_on_both():
    brokers = [_broker_with([_scalar_candidate().to_dict()])]
    cloud = [_cloud_candidate(serial_number="ABC123", device_id="ABC123").to_dict()]
    proposals = proposals_from_sources(brokers, cloud)
    assert len(proposals) == 2
    assert {proposal["broker_ref"] for proposal in proposals} == {
        next(
            p["broker_ref"]
            for p in proposals
            if p["broker_ref"].startswith("local_mqtt_")
        ),
        "zendure_cloud",
    }


def test_a_cloud_candidate_repeated_in_the_broker_list_collapses():
    # The same cloud observation reaching both source lists is one connection,
    # not an alternative to itself.
    cloud = _cloud_candidate(serial_number="ABC123", device_id="CLOUD_ROUTE").to_dict()
    proposals = proposals_from_sources([_broker_with([cloud])], [cloud])
    assert len(proposals) == 1
    assert proposals[0]["broker_ref"] == "zendure_cloud"


def test_sources_stamp_generation_on_local_proposals_only():
    brokers = [
        _broker_with([_scalar_candidate().to_dict()], discovery_generation=7)
    ]
    proposals = proposals_from_sources(brokers, [_cloud_candidate().to_dict()])
    local = next(p for p in proposals if p["broker_ref"].startswith("local_mqtt_"))
    cloud = next(p for p in proposals if p["broker_ref"] == "zendure_cloud")
    assert local["discovery_generation"] == 7
    assert local["id"].endswith(":g7")
    assert "discovery_generation" not in cloud
    assert not cloud["id"].endswith(":g7")


def test_sources_without_cloud_match_brokers_only_output():
    brokers = [_broker_with([_scalar_candidate().to_dict()], discovery_generation=3)]
    assert proposals_from_sources(brokers, []) == proposals_from_brokers(brokers)


# --- topic family is a telemetry schema, never a hardware generation ---------
#
# Live-reproduced: a SolarFlow 800 Pro 2 (ZenSDK generation) on the Zendure
# cloud broker reports via the leading-slash JSON properties topic, which is
# classified as the internal ``legacy_zendure_json_alt`` family. That family
# names the observed topic/payload format only; it must never re-label the
# hardware as the older Hub/Hyper generation.

PRO2_MODEL = "SolarFlow 800 Pro2"
PRO2_SERIAL = "EOD1PRO2LIVE001"
LEGACY_GENERATION_LABEL = "Older Zendure Hub / Hyper generation"


def _pro2_cloud_json_report_candidate(*, trusted=False):
    import json as _json

    from admin.zendure_cloud_mqtt import CloudCandidateSet

    candidate_set = CloudCandidateSet(
        "mqtteu.zen-iot.com", 8883, "encrypted_no_verify", app_key="APPKEY"
    )
    candidate_set.seed_devices(
        [
            {
                "productKey": "PKPRO2",
                "deviceKey": "DKPRO2",
                "productModel": PRO2_MODEL,
                "snNumber": PRO2_SERIAL,
                "deviceName": "Balcony south",
            }
        ]
    )
    candidate_set.observe(
        "/PKPRO2/DKPRO2/properties/report",
        _json.dumps(
            {
                "properties": {
                    "packNum": 1,
                    "heatState": 0,
                    "packInputPower": 0,
                    "outputPackPower": 120,
                    "outputHomePower": 240,
                    "electricLevel": 77,
                }
            }
        ),
    )
    results = (
        candidate_set.trusted_results() if trusted else candidate_set.results()
    )
    assert len(results) == 1
    return results[0]


def test_cloud_json_report_does_not_reclassify_solarflow_pro2_as_legacy_hardware():
    candidate = _pro2_cloud_json_report_candidate()

    # The candidate keeps its cloud identity and the observed JSON-report schema.
    assert candidate["source_type"] == "zendure_cloud_mqtt"
    assert candidate["model_hint"] == PRO2_MODEL
    assert candidate["discovery_status"] == "mqtt_observed"
    assert candidate["topic_family"] == "legacy_zendure_json_alt"

    proposals = proposals_from_sources([], [candidate])
    assert len(proposals) == 1
    proposal = proposals[0]

    # Telemetry schema: the JSON report format is recognized and drives the
    # parser choice — the config preview keeps the observed JSON family.
    assert proposal["topic_family"] == "legacy_zendure_json_alt"
    assert proposal["telemetry_schema"] == "zendure_json_report_leading_slash"
    fragment = proposal["config_fragment"]
    assert fragment["mqtt"]["topic_family"] == "legacy_zendure_json_alt"
    from ems.zendure_mqtt.topics import JSON_FAMILIES

    assert fragment["mqtt"]["topic_family"] in JSON_FAMILIES

    # Hardware generation: derived from the product model, not the topic layout.
    assert proposal["hardware_generation"] == "solarflow_zensdk"
    assert proposal["hardware_generation_label"] == "New SolarFlow / ZenSDK generation"
    assert proposal["hardware_generation_label"] != LEGACY_GENERATION_LABEL
    assert proposal["hardware_model"] == "solarflow_800_pro_2"
    assert proposal["alternative_layout"] is True

    # No write support is ever derived from the topic family for cloud
    # discovery: the observed metrics carry no output control.
    assert fragment["capabilities"]["write_output_limit"] is False
    assert "output_control" not in proposal["capabilities"]


def test_cloud_json_report_trusted_flow_uses_route_id_for_runtime_subscription():
    from ems.zendure_mqtt.config_entries import zendure_cloud_device_subscriptions

    candidate = _pro2_cloud_json_report_candidate(trusted=True)
    assert candidate["serial_number"] == PRO2_SERIAL
    assert candidate["device_id"] == "DKPRO2"

    proposal = proposals_from_sources([], [candidate])[0]
    fragment = proposal["config_fragment"]
    assert fragment["serial_number"] == PRO2_SERIAL
    assert fragment["mqtt"]["device_id"] == "DKPRO2"
    assert zendure_cloud_device_subscriptions(
        [fragment], "zendure_cloud"
    ) == (
        "/PKPRO2/DKPRO2/#",
        "iot/PKPRO2/DKPRO2/#",
    )


def test_cloud_device_list_only_trusted_flow_uses_route_id_for_runtime_subscription():
    from admin.zendure_cloud_mqtt import CloudCandidateSet
    from ems.zendure_mqtt.config_entries import zendure_cloud_device_subscriptions

    candidate_set = CloudCandidateSet(
        "mqtteu.zen-iot.com", 8883, "encrypted_no_verify", app_key="APPKEY"
    )
    candidate_set.seed_devices(
        [
            {
                "productKey": "PKPRO2",
                "deviceKey": "DKPRO2",
                "productModel": PRO2_MODEL,
                "snNumber": PRO2_SERIAL,
                "deviceName": "Balcony south",
            }
        ]
    )

    candidate = candidate_set.trusted_results()[0]
    assert candidate["discovery_status"] == "device_list_only"
    assert candidate["serial_number"] == PRO2_SERIAL
    assert candidate["device_id"] == "DKPRO2"

    proposal = proposals_from_sources([], [candidate])[0]
    fragment = proposal["config_fragment"]
    assert fragment["serial_number"] == PRO2_SERIAL
    assert fragment["mqtt"]["device_id"] == "DKPRO2"
    assert zendure_cloud_device_subscriptions(
        [fragment], "zendure_cloud"
    ) == (
        "/PKPRO2/DKPRO2/#",
        "iot/PKPRO2/DKPRO2/#",
    )


def test_cloud_json_report_proposal_keeps_json_parser_when_added_to_config():
    # Adding the proposal to config (the maintenance/new-device projection) must
    # keep the observed JSON-report family even though the hardware generation
    # resolves to ZenSDK — the generation must never re-home the topic identity.
    from admin.zendure_mqtt_config_draft import apply_zendure_mqtt_draft_fields

    candidate = _pro2_cloud_json_report_candidate()
    proposal = proposals_from_sources([], [candidate])[0]
    fragment_mqtt = proposal["config_fragment"]["mqtt"]
    draft = {
        "name": proposal["display_name"],
        "serial_number": proposal["serial_number"],
        "hardware_generation": proposal["hardware_generation"],
        "hardware_model": proposal["hardware_model"],
        "alternative_layout": proposal["alternative_layout"],
        "mqtt": {
            "broker_ref": fragment_mqtt.get("broker_ref") or "",
            "topic_family": fragment_mqtt.get("topic_family") or "",
            "base_topic": fragment_mqtt.get("base_topic"),
            "device_id": fragment_mqtt.get("device_id") or "",
        },
    }
    device = {}
    apply_zendure_mqtt_draft_fields(device, draft)
    assert device["mqtt"]["topic_family"] == "legacy_zendure_json_alt"
    assert device["capabilities"]["write_output_limit"] is False
