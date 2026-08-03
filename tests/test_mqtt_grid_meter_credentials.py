# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest

from ems import config as cfg
from ems.clients import create_grid_meter_client
from ems.mqtt_credentials import MqttCredentialError, MqttCredentials

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]


class Resolver:
    def __init__(self, records):
        self.records = records

    def resolve(self, ref):
        try:
            return self.records[ref]
        except KeyError as exc:
            raise MqttCredentialError(f"MQTT credential reference '{ref}' was not found") from exc


class FakeMqttClient:
    def username_pw_set(self, username, password):
        self.auth = (username, password)

    def connect(self, *_args, **_kwargs):
        return None

    def subscribe(self, *_args, **_kwargs):
        return None

    def loop_start(self):
        return None


@pytest.mark.parametrize("meter_type", ["mqtt", "zendure_smartmeter_d0"])
def test_named_broker_credentials_reach_runtime_client(meter_type):
    config = {
        "grid_meter": {
            "type": meter_type,
            "mqtt": {"broker_ref": "home", "topic": "meter/power"},
        },
        "zendure_mqtt": {
            "enabled": True,
            "brokers": {
                "home": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "broker.local",
                    "port": 1883,
                    "credentials_ref": "home",
                }
            },
        },
    }
    resolved = cfg.resolve_grid_meter_mqtt_settings(config)
    assert resolved["credentials_ref"] == "home"
    fake = FakeMqttClient()
    resolved["_mqtt_client_factory"] = lambda: fake

    create_grid_meter_client(
        {"type": meter_type, "mqtt": resolved},
        object(),
        mqtt_credential_resolver=Resolver(
            {"home": MqttCredentials("runtime-user", "runtime-password")}
        ),
    )

    assert fake.auth == ("runtime-user", "runtime-password")


def test_grid_meter_unknown_credentials_ref_fails_safely():
    with pytest.raises(MqttCredentialError, match="was not found") as caught:
        create_grid_meter_client(
            {
                "type": "mqtt",
                "mqtt": {
                    "host": "broker.local",
                    "topic": "meter/power",
                    "credentials_ref": "missing",
                },
            },
            object(),
            mqtt_credential_resolver=Resolver({}),
        )
    assert "password" not in str(caught.value)


@pytest.mark.parametrize("blank", [" ", "\t", "\n", "   "])
def test_grid_meter_direct_incomplete_record_fails_safely(blank):
    # A direct MQTT grid meter whose credential record is present but only
    # whitespace must not build an anonymous client: the Core completeness
    # contract rejects it before a connection is attempted.
    fake = FakeMqttClient()
    with pytest.raises(MqttCredentialError) as caught:
        create_grid_meter_client(
            {
                "type": "mqtt",
                "mqtt": {
                    "host": "broker.local",
                    "topic": "meter/power",
                    "credentials_ref": "meter-auth",
                    "_mqtt_client_factory": lambda: fake,
                },
            },
            object(),
            mqtt_credential_resolver=Resolver(
                {"meter-auth": MqttCredentials("meter-user", blank)}
            ),
        )
    assert not hasattr(fake, "auth")
    assert "meter-user" not in str(caught.value)


def test_grid_meter_named_broker_incomplete_record_fails_safely():
    fake = FakeMqttClient()
    config = {
        "grid_meter": {
            "type": "mqtt",
            "mqtt": {"broker_ref": "home", "topic": "meter/power"},
        },
        "zendure_mqtt": {
            "brokers": {
                "home": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "broker.local",
                    "port": 1883,
                    "credentials_ref": "home",
                }
            }
        },
    }
    resolved = cfg.resolve_grid_meter_mqtt_settings(config)
    resolved["_mqtt_client_factory"] = lambda: fake
    with pytest.raises(MqttCredentialError):
        create_grid_meter_client(
            {"type": "mqtt", "mqtt": resolved},
            object(),
            mqtt_credential_resolver=Resolver(
                {"home": MqttCredentials("home-user", "   ")}
            ),
        )
    assert not hasattr(fake, "auth")


def test_grid_meter_rejects_reference_with_inline_credentials():
    with pytest.raises(ValueError, match="conflicts with inline credentials"):
        cfg.normalize_mqtt_grid_meter_settings(
            {
                "type": "mqtt",
                "mqtt": {
                    "host": "broker.local",
                    "topic": "meter/power",
                    "credentials_ref": "home",
                    "username": "inline",
                },
            }
        )


def test_invalid_tls_flag_does_not_produce_misleading_port_error():
    # A bad TLS flag must not surface as a "port is invalid" message: the
    # connection-validation error preserves the TLS-specific wording.
    with pytest.raises(ValueError) as caught:
        create_grid_meter_client(
            {
                "type": "mqtt",
                "mqtt": {
                    "host": "broker.local",
                    "topic": "meter/power",
                    "tls": "false",
                },
            },
            object(),
        )
    message = str(caught.value)
    assert "port is invalid" not in message
    assert "tls" in message.lower()
