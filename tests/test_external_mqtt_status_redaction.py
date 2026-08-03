# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cloud route data is masked at browser/support export boundaries."""

import json

import pytest

from ems.external_status import (
    mask_mqtt_topic,
    mask_route_identifier,
    sanitize_external_mqtt_status,
)

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]


def test_route_and_canonical_topic_masking_are_centralized():
    assert mask_route_identifier("ACCOUNT_ROUTE_1234") == "…1234"
    # Topic-shape masking applies only in Cloud scope.
    assert (
        mask_mqtt_topic(
            "iot/PRODUCT_SECRET/ACCOUNT_ROUTE_1234/properties/write",
            cloud_scoped=True,
        )
        == "iot/…/…/properties/write"
    )
    assert (
        mask_mqtt_topic(
            "/PRODUCT_SECRET/ACCOUNT_ROUTE_1234/properties/report", cloud_scoped=True
        )
        == "/…/…/properties/report"
    )
    # A leading-slash Cloud function/invoke or custom route is masked too.
    assert (
        mask_mqtt_topic("/PK/DEVICE/function/invoke", cloud_scoped=True)
        == "/…/…/function/invoke"
    )
    assert (
        mask_mqtt_topic("iot/PK/DEVICE/custom/vendor/action", cloud_scoped=True)
        == "iot/…/…/custom/vendor/action"
    )
    # Local (non-Cloud) topics are preserved as useful diagnostics.
    assert (
        mask_mqtt_topic("iot/LOCAL_PRODUCT/LOCAL_DEVICE/properties/write")
        == "iot/LOCAL_PRODUCT/LOCAL_DEVICE/properties/write"
    )
    # Filesystem paths are never mistaken for routes, even in Cloud scope.
    assert mask_mqtt_topic("/tmp/install/config/config.json", cloud_scoped=True) == (
        "/tmp/install/config/config.json"
    )


def test_mask_mqtt_topic_is_safe_on_malformed_input():
    for value in ("", "/", "///", "iot", "iot/only", "/a/b", None, 123, ["x"]):
        # No crash and no accidental masking of a non-route path/value.
        assert mask_mqtt_topic(value, cloud_scoped=True) == value


def test_source_aware_status_preserves_local_and_masks_cloud_routes():
    status = {
        "devices": [
            {
                "name": "Local",
                "source": "local_mqtt",
                "topic": "iot/LOCAL_PRODUCT/LOCAL_DEVICE/properties/write",
                "root_topic": "/LOCAL_PRODUCT/LOCAL_DEVICE/state/report",
            },
            {
                "name": "Cloud",
                "source": "zendure_cloud_mqtt",
                "product_key": "CLOUD_PRODUCT",
                "device_id": "CLOUD_DEVICE",
                "topic": "iot/CLOUD_PRODUCT/CLOUD_DEVICE/properties/write",
                "function_topic": "/CLOUD_PRODUCT/CLOUD_DEVICE/function/invoke",
                "custom_topic": "iot/CLOUD_PRODUCT/CLOUD_DEVICE/custom/vendor/action",
                "diagnostic_by_route": {"CLOUD_DEVICE": "pending"},
            },
        ],
        # A raw Cloud route repeated in free-form global text must still be masked.
        "error": "publish failed on iot/CLOUD_PRODUCT/CLOUD_DEVICE/function/invoke",
    }

    safe = sanitize_external_mqtt_status(status, sensitive_context=status)
    flat = json.dumps(safe, ensure_ascii=False)

    # Local MQTT topics stay visible (useful diagnostics, not Cloud secrets).
    assert "iot/LOCAL_PRODUCT/LOCAL_DEVICE/properties/write" in flat
    assert "/LOCAL_PRODUCT/LOCAL_DEVICE/state/report" in flat
    # Cloud account-scoped route material never leaks, in any position.
    assert "CLOUD_PRODUCT" not in flat
    assert "CLOUD_DEVICE" not in flat
    cloud = safe["devices"][1]
    assert cloud["topic"] == "iot/…/…/properties/write"
    assert cloud["function_topic"] == "/…/…/function/invoke"
    assert cloud["custom_topic"] == "iot/…/…/custom/vendor/action"
    # A mapping key that is a Cloud route id is masked, not preserved verbatim.
    assert "CLOUD_DEVICE" not in json.dumps(cloud["diagnostic_by_route"])
    # Both devices remain distinguishable.
    assert [device["name"] for device in safe["devices"]] == ["Local", "Cloud"]


def test_external_cloud_status_masks_nested_command_route_and_drops_credentials():
    route = "ACCOUNT_ROUTE_1234"
    topic = f"iot/PRODUCT_SECRET/{route}/properties/write"
    status = {
        "brokers": [
            {
                "broker_ref": "cloud_a",
                "source": "zendure_cloud_mqtt",
                "password": "BROKER_PASSWORD",
            }
        ],
        "devices": [
            {
                "name": "Battery",
                "broker_ref": "cloud_a",
                "source": "zendure_cloud_mqtt",
                "identifier": route,
                "product_key": "PRODUCT_SECRET",
                "display_name": f"Zendure {route}",
                "reason": f"publish iot/PRODUCT_SECRET/{route}/properties/write pending",
                "detail": "iot/OTHER_PRODUCT/OTHER_ROUTE/properties/write",
                "diagnostic_by_route": {route: "pending"},
                "effective_write_topic": topic,
                "last_command": {
                    "device_id": route,
                    "topic": topic,
                    "correlation_id": "safe-correlation-id",
                },
            }
        ],
        "authorization_code": "AUTHORIZATION_SECRET",
    }

    safe = sanitize_external_mqtt_status(status)
    flattened = json.dumps(safe)

    for raw in (
        route,
        topic,
        "PRODUCT_SECRET",
        "BROKER_PASSWORD",
        "AUTHORIZATION_SECRET",
    ):
        assert raw not in flattened
    device = safe["devices"][0]
    assert device["identifier"] == "…1234"
    assert route not in device["display_name"]
    assert "PRODUCT_SECRET" not in device["reason"]
    assert "OTHER_PRODUCT" not in device["detail"]
    assert "OTHER_ROUTE" not in device["detail"]
    assert route not in device["diagnostic_by_route"]
    assert device["effective_write_topic"] == "iot/…/…/properties/write"
    assert device["last_command"]["correlation_id"] == "safe-correlation-id"


def test_external_boundary_audit_negatives_and_local_positives():
    """One mixed status through the shared boundary: Cloud route/credential
    material is absent while local diagnostics stay available."""

    status = {
        "brokers": [
            {
                "broker_ref": "cloud_a",
                "source": "zendure_cloud_mqtt",
                "username": "CLOUD_USERNAME",
                "password": "CLOUD_PASSWORD",
                "app_key": "CLOUD_APP_KEY",
                "authorization": "CLOUD_AUTH_TOKEN",
                "access_token": "CLOUD_ACCESS_TOKEN",
            },
            {"broker_ref": "local_b", "source": "local_mqtt"},
        ],
        "devices": [
            {
                "name": "Cloud Battery",
                "source": "zendure_cloud_mqtt",
                "broker_ref": "cloud_a",
                "product_key": "CLOUD_PRODUCT_KEY",
                "device_id": "CLOUD_ROUTE_ID",
                "effective_write_topic": "iot/CLOUD_PRODUCT_KEY/CLOUD_ROUTE_ID/properties/write",
            },
            {
                "name": "Garage Local Battery",
                "source": "local_mqtt",
                "broker_ref": "local_b",
                "device_id": "GARAGE_LOCAL_ID",
                "effective_write_topic": "iot/GARAGE_PRODUCT/GARAGE_LOCAL_ID/properties/write",
            },
        ],
    }

    safe = sanitize_external_mqtt_status(status)
    flattened = json.dumps(safe, ensure_ascii=False)

    # Negative assertions: no Cloud route/product/topic and no credential leaks.
    for raw in (
        "CLOUD_ROUTE_ID",
        "CLOUD_PRODUCT_KEY",
        "iot/CLOUD_PRODUCT_KEY/CLOUD_ROUTE_ID/properties/write",
        "CLOUD_PASSWORD",
        "CLOUD_USERNAME",
        "CLOUD_APP_KEY",
        "CLOUD_AUTH_TOKEN",
        "CLOUD_ACCESS_TOKEN",
    ):
        assert raw not in flattened, raw

    # Positive assertions: local diagnostics remain available and labelled.
    local = safe["devices"][1]
    assert local["name"] == "Garage Local Battery"
    assert local["device_id"] == "GARAGE_LOCAL_ID"
    assert (
        local["effective_write_topic"]
        == "iot/GARAGE_PRODUCT/GARAGE_LOCAL_ID/properties/write"
    )
    # The Cloud device stays present (distinguishable) but fully route-masked.
    assert safe["devices"][0]["name"] == "Cloud Battery"
    assert safe["devices"][0]["effective_write_topic"] == "iot/…/…/properties/write"


def test_masked_mapping_key_collisions_retain_every_evidence_entry():
    first = "ACCOUNT_ALPHA_ROUTE_1234"
    second = "ACCOUNT_BRAVO_ROUTE_1234"
    safe = sanitize_external_mqtt_status(
        {
            "source": "zendure_cloud_mqtt",
            "devices": [
                {
                    "source": "zendure_cloud_mqtt",
                    "device_id": first,
                    "diagnostic_by_route": {
                        first: "first evidence",
                        second: "second evidence",
                    },
                },
                {
                    "source": "zendure_cloud_mqtt",
                    "device_id": second,
                },
            ],
        }
    )

    evidence = safe["devices"][0]["diagnostic_by_route"]
    assert len(evidence) == 2
    assert set(evidence.values()) == {"first evidence", "second evidence"}
    assert len(set(evidence)) == 2
    assert first not in json.dumps(safe)
    assert second not in json.dumps(safe)


def test_secret_like_device_names_are_not_mistaken_for_credential_fields():
    safe = sanitize_external_mqtt_status(
        {
            "devices": {
                "Secret Shed": {"soc": 50, "password": "RAW_PASSWORD"},
                "token": {"soc": 60},
                "username": {"soc": 70},
            }
        }
    )

    assert set(safe["devices"]) == {"Secret Shed", "token", "username"}
    assert safe["devices"]["Secret Shed"] == {"soc": 50}
    assert "RAW_PASSWORD" not in json.dumps(safe)


def test_short_credentials_do_not_corrupt_status_schema_or_ordinary_text():
    safe = sanitize_external_mqtt_status(
        {
            "average_soc": 61,
            "battery_power_w": 420,
            "status": "happy",
            "message": "auth p for user a",
        },
        sensitive_context={
            "zendure_mqtt": {"username": "a", "password": "p"}
        },
    )

    assert safe["average_soc"] == 61
    assert safe["battery_power_w"] == 420
    assert safe["status"] == "happy"
    assert safe["message"] == "auth <redacted> for user <redacted>"


def test_short_cloud_routes_do_not_corrupt_unrelated_local_text():
    safe = sanitize_external_mqtt_status(
        {
            "devices": [
                {
                    "source": "zendure_cloud_mqtt",
                    "device_id": "X",
                    "product_key": "P",
                },
                {
                    "source": "local_mqtt",
                    "name": "Local Power",
                    "status": "Power available",
                },
            ]
        }
    )

    assert safe["devices"][1]["name"] == "Local Power"
    assert safe["devices"][1]["status"] == "Power available"


def test_config_redaction_mode_preserves_editable_mqtt_fields_and_serials():
    safe = sanitize_external_mqtt_status(
        {
            "grid_meter": {
                "type": "mqtt",
                "username": "meter",
                "topic": "meter/power",
            },
            "devices": [
                {
                    "source": "zendure_cloud_mqtt",
                    "serial_number": "S1",
                    "device_id": "S1",
                }
            ],
        },
        drop_secrets=False,
    )

    assert safe["grid_meter"]["username"] == "meter"
    assert safe["grid_meter"]["topic"] == "meter/power"
    assert safe["devices"][0]["serial_number"] == "S1"
    assert safe["devices"][0]["device_id"] == "••••"


@pytest.mark.parametrize("source_key", ["connection_source", "source_type"])
def test_alternate_cloud_source_fields_mask_structured_routes(source_key):
    safe = sanitize_external_mqtt_status(
        {
            source_key: "zendure_cloud_mqtt",
            "device_id": "ACCOUNT_ROUTE_7501",
            "product_key": "PRODUCT_ACCOUNT_7501",
        }
    )

    assert "ACCOUNT_ROUTE_7501" not in json.dumps(safe)
    assert "PRODUCT_ACCOUNT_7501" not in json.dumps(safe)


def test_orphan_cloud_route_mapping_keys_fail_closed():
    route = "ORPHAN_CLOUD_ROUTE_7501"
    safe = sanitize_external_mqtt_status(
        {
            "connection_source": "zendure_cloud_mqtt",
            "diagnostic_by_route": {route: {"status": "bad"}},
            "devices": {route: {"status": "offline"}},
        }
    )

    assert route not in json.dumps(safe)
    assert len(safe["diagnostic_by_route"]) == 1
    assert len(safe["devices"]) == 1


def test_trusted_device_name_never_exempts_route_keyed_diagnostics():
    route = "ORPHAN_CLOUD_ROUTE_7501"
    safe = sanitize_external_mqtt_status(
        {
            "source": "zendure_cloud_mqtt",
            "diagnostic_by_route": {route: {"status": "bad"}},
        },
        sensitive_context={
            "zendure_mqtt": {
                "brokers": {"cloud_a": {"source": "zendure_cloud_mqtt"}}
            },
            "devices": [
                {
                    "name": route,
                    "type": "zendure_mqtt",
                    "mqtt": {
                        "broker_ref": "cloud_a",
                        "device_id": "DIFFERENT_ROUTE_9999",
                    },
                }
            ],
        },
    )

    assert route not in json.dumps(safe)
    assert len(safe["diagnostic_by_route"]) == 1


def test_non_string_cloud_route_fields_use_fixed_mask():
    safe = sanitize_external_mqtt_status(
        {
            "source_type": "zendure_cloud_mqtt",
            "device_id": 123456789,
            "product_key": 987654321,
        }
    )

    assert safe["device_id"] == "••••"
    assert safe["product_key"] == "••••"


def test_sanitizer_is_idempotent_for_explicit_redaction_placeholders():
    safe = sanitize_external_mqtt_status(
        {
            "diagnosis": {"metrics": {"token": "<redacted>"}},
            "devices": [
                {
                    "source": "zendure_cloud_mqtt",
                    "device_id": "<redacted>",
                    "product_key": "<redacted>",
                }
            ],
        },
        drop_secrets=False,
    )

    assert safe["diagnosis"]["metrics"]["token"] == "<redacted>"
    assert safe["devices"][0]["device_id"] == "<redacted>"


def test_local_status_identifier_is_not_needlessly_masked():
    safe = sanitize_external_mqtt_status(
        {
            "devices": [
                {
                    "source": "local_mqtt",
                    "broker_ref": "garage",
                    "identifier": "LOCAL_DEVICE",
                }
            ]
        }
    )

    assert safe["devices"][0]["identifier"] == "LOCAL_DEVICE"


def test_named_cloud_broker_scope_masks_source_omitted_config_device():
    route = "ACCOUNT_ROUTE_5678"
    config = {
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
                "mqtt": {
                    "broker_ref": "cloud_a",
                    "product_key": "PRODUCT_ACCOUNT_A",
                    "device_id": route,
                    "write_topic": (
                        f"iot/PRODUCT_ACCOUNT_A/{route}/properties/write"
                    ),
                },
            }
        ],
    }

    flattened = json.dumps(sanitize_external_mqtt_status(config))

    assert route not in flattened
    assert "PRODUCT_ACCOUNT_A" not in flattened
    assert f"iot/PRODUCT_ACCOUNT_A/{route}/properties/write" not in flattened


def test_legacy_top_level_cloud_scope_masks_default_ref_device():
    route = "LEGACY_ACCOUNT_ROUTE_9876"
    config = {
        "zendure_mqtt": {
            "source": "zendure_cloud_mqtt",
            "host": "mqtt.example.invalid",
        },
        "devices": [
            {
                "type": "zendure_mqtt",
                "mqtt": {
                    "topic_family": "legacy_zendure_json",
                    "product_key": "LEGACY_PRODUCT_ACCOUNT",
                    "device_id": route,
                },
            }
        ],
    }

    flattened = json.dumps(sanitize_external_mqtt_status(config))

    assert route not in flattened
    assert "LEGACY_PRODUCT_ACCOUNT" not in flattened


def test_incomplete_cloud_context_masks_name_left_as_only_route_evidence():
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
                },
            },
            {
                "name": f"Rejected Cloud {route}",
                "type": "zendure_mqtt",
                "mqtt": {
                    "broker_ref": "cloud_a",
                    "product_key": "KNOWN_PRODUCT",
                    "device_id": "<redacted>",
                },
            },
        ],
    }
    status = {
        "devices": [
            {
                "name": f"Rejected Cloud {route}",
                "status": "invalid",
                "issues": ["device_id_missing"],
            }
        ]
    }

    safe = sanitize_external_mqtt_status(status, sensitive_context=config)

    assert route not in json.dumps(safe)
    assert safe["devices"][0]["name"] != f"Rejected Cloud {route}"
