# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every MQTT credential consumer is discovered from the complete config.

Credential integrity is a global contract: Zendure broker profiles, direct MQTT
grid meters and grid meters that reference a named broker all consume the same
Core-owned ``credentials_ref`` records. One shared extractor
(:func:`collect_mqtt_credential_consumers`) is the single source of truth for
who references what, so validation, staging and final verification never each
grow their own feature-specific scanner.
"""

import pytest

from ems.mqtt_credentials import (
    MqttCredentialConsumer,
    collect_mqtt_credential_consumers,
    find_mqtt_credential_consumer_issues,
)

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]


def _by_component(config):
    consumers = collect_mqtt_credential_consumers(config)
    grouped = {}
    for consumer in consumers:
        grouped.setdefault(consumer.component, []).append(consumer)
    return consumers, grouped


def _direct_grid_meter_config(ref="grid-meter"):
    return {
        "grid_meter": {
            "type": "mqtt",
            "mqtt": {
                "host": "broker.local",
                "port": 1883,
                "topic": "meter/power",
                "credentials_ref": ref,
            },
        }
    }


def _local_broker_config(ref="local-main"):
    return {
        "devices": [
            {
                "type": "zendure_mqtt",
                "enabled": True,
                "sn": "SN-LOCAL1",
                "mqtt": {"broker_ref": "local-main"},
            }
        ],
        "zendure_mqtt": {
            "brokers": {
                "local-main": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "10.0.0.10",
                    "port": 1883,
                    "credentials_ref": ref,
                }
            }
        },
    }


def _cloud_broker_config(ref="zendure-cloud"):
    return {
        "devices": [
            {
                "type": "zendure_mqtt",
                "enabled": True,
                "sn": "SN-CLOUD1",
                "mqtt": {"broker_ref": "cloud-main"},
            }
        ],
        "zendure_mqtt": {
            "brokers": {
                "cloud-main": {
                    "enabled": True,
                    "source": "zendure_cloud_mqtt",
                    "host": "mqtteu.zen-iot.com",
                    "port": 8883,
                    "credentials_ref": ref,
                }
            }
        },
    }


def _named_broker_grid_meter_config(ref="home"):
    return {
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
                    "credentials_ref": ref,
                }
            }
        },
    }


# --- consumers are discovered with the right source and location ------------


def test_direct_grid_meter_is_a_local_mqtt_consumer():
    consumers, grouped = _by_component(_direct_grid_meter_config())
    assert len(consumers) == 1
    consumer = grouped["grid_meter"][0]
    assert consumer == MqttCredentialConsumer(
        credentials_ref="grid-meter",
        source="local_mqtt",
        component="grid_meter",
        config_path="grid_meter.mqtt.credentials_ref",
        broker_ref=None,
    )


def test_zendure_local_broker_is_a_local_mqtt_consumer():
    consumers, grouped = _by_component(_local_broker_config())
    assert len(consumers) == 1
    consumer = grouped["zendure_mqtt_broker"][0]
    assert consumer.credentials_ref == "local-main"
    assert consumer.source == "local_mqtt"
    assert consumer.component == "zendure_mqtt_broker"
    assert consumer.config_path == "zendure_mqtt.brokers.local-main.credentials_ref"


def test_zendure_cloud_broker_is_a_cloud_consumer():
    consumers, grouped = _by_component(_cloud_broker_config())
    assert len(consumers) == 1
    consumer = grouped["zendure_mqtt_broker"][0]
    assert consumer.credentials_ref == "zendure-cloud"
    assert consumer.source == "zendure_cloud_mqtt"
    assert consumer.config_path == "zendure_mqtt.brokers.cloud-main.credentials_ref"


def test_named_broker_grid_meter_usage_is_discovered():
    consumers, grouped = _by_component(_named_broker_grid_meter_config())
    # The broker profile is referenced by the grid meter, so both the broker
    # profile and the grid meter surface as consumers sharing one local ref.
    assert {c.component for c in consumers} == {"grid_meter", "zendure_mqtt_broker"}
    grid = grouped["grid_meter"][0]
    assert grid.credentials_ref == "home"
    assert grid.source == "local_mqtt"
    assert grid.broker_ref == "home"
    assert grid.config_path == "grid_meter.mqtt.broker_ref"
    broker = grouped["zendure_mqtt_broker"][0]
    assert broker.credentials_ref == "home"
    assert broker.source == "local_mqtt"


def test_named_broker_grid_meter_defaults_missing_source_to_local():
    # The runtime resolves a source-less named local broker as local_mqtt
    # (_zendure_mqtt_broker_connection defaults a missing source), so the
    # grid-meter consumer must too — otherwise its credential is dropped from
    # the local staging set and the grid meter cannot authenticate at runtime.
    config = {
        "grid_meter": {
            "type": "mqtt",
            "mqtt": {"broker_ref": "b1", "topic": "meter/power"},
        },
        "zendure_mqtt": {
            "brokers": {
                "b1": {
                    "enabled": True,
                    "host": "broker.local",
                    "port": 1883,
                    "credentials_ref": "gridcred",
                }
            }
        },
    }
    grid = [
        c
        for c in collect_mqtt_credential_consumers(config)
        if c.component == "grid_meter"
    ]
    assert len(grid) == 1
    assert grid[0].source == "local_mqtt"
    assert grid[0].credentials_ref == "gridcred"


def test_multiple_consumers_share_one_compatible_ref():
    config = _local_broker_config(ref="home")
    config["zendure_mqtt"]["brokers"]["local-shed"] = {
        "enabled": True,
        "source": "local_mqtt",
        "host": "10.0.0.11",
        "port": 1883,
        "credentials_ref": "home",
    }
    config["devices"].append(
        {
            "type": "zendure_mqtt",
            "enabled": True,
            "sn": "SN-LOCAL2",
            "mqtt": {"broker_ref": "local-shed"},
        }
    )
    consumers = collect_mqtt_credential_consumers(config)
    refs = {c.credentials_ref for c in consumers}
    assert refs == {"home"}
    assert len(consumers) == 2
    assert all(c.source == "local_mqtt" for c in consumers)


def test_configured_ref_is_never_normalized_by_extraction():
    # A non-canonical value survives extraction verbatim so downstream
    # validation can reject exactly what the config declared.
    consumers = collect_mqtt_credential_consumers(_direct_grid_meter_config("Bad Ref"))
    assert [c.credentials_ref for c in consumers] == ["Bad Ref"]


# --- consumers that must not produce a false credential requirement ----------


def test_anonymous_direct_grid_meter_has_no_consumer():
    config = {
        "grid_meter": {
            "type": "mqtt",
            "mqtt": {"host": "broker.local", "topic": "meter/power"},
        }
    }
    assert collect_mqtt_credential_consumers(config) == ()


def test_disabled_mqtt_grid_meter_has_no_consumer():
    config = _direct_grid_meter_config()
    config["grid_meter"]["enabled"] = False
    assert collect_mqtt_credential_consumers(config) == ()


def test_non_mqtt_grid_meter_has_no_consumer():
    config = {
        "grid_meter": {
            "type": "shelly",
            "ip": "192.168.1.50",
            # A stray mqtt block on a non-MQTT meter is not a credential consumer.
            "mqtt": {"credentials_ref": "should-be-ignored"},
        }
    }
    assert collect_mqtt_credential_consumers(config) == ()


@pytest.mark.parametrize(
    "config",
    [
        {"grid_meter": {"type": "mqtt", "mqtt": "not-a-dict"}},
        {"grid_meter": "not-a-dict"},
        {"grid_meter": {"type": "mqtt"}},
        {"grid_meter": {"type": "mqtt", "mqtt": {"credentials_ref": None}}},
        {},
        None,
        "not-a-config",
    ],
)
def test_malformed_config_yields_no_consumer_and_no_crash(config):
    assert collect_mqtt_credential_consumers(config) == ()


def test_disabled_or_unreferenced_broker_profile_is_not_a_consumer():
    # A broker profile no enabled device (or the grid meter) references keeps the
    # existing runtime semantics: it is not staged, so it is not a consumer.
    config = _local_broker_config()
    config["devices"] = []
    assert collect_mqtt_credential_consumers(config) == ()

    disabled = _local_broker_config()
    disabled["zendure_mqtt"]["brokers"]["local-main"]["enabled"] = False
    assert collect_mqtt_credential_consumers(disabled) == ()


# --- legacy top-level (implicit ``default``) broker is a consumer -----------
# A legacy single-broker install configures its broker directly under
# ``zendure_mqtt`` (no ``brokers`` block); the scanner must recognize that
# implicit ``default`` broker's credential exactly as the runtime resolves it,
# or a referenced credential silently escapes staging and validation.


def test_legacy_local_default_broker_is_a_consumer():
    config = {
        "zendure_mqtt": {
            "host": "broker.local",
            "source": "local_mqtt",
            "credentials_ref": "legacy-auth",
        }
    }
    consumers, grouped = _by_component(config)
    assert len(consumers) == 1
    consumer = grouped["zendure_mqtt_broker"][0]
    assert consumer == MqttCredentialConsumer(
        credentials_ref="legacy-auth",
        source="local_mqtt",
        component="zendure_mqtt_broker",
        config_path="zendure_mqtt.credentials_ref",
        broker_ref=None,
    )


def test_legacy_local_default_broker_is_a_local_requirement():
    from admin.mqtt_runtime_provisioning import runtime_credential_requirements

    config = {
        "zendure_mqtt": {
            "host": "broker.local",
            "source": "local_mqtt",
            "credentials_ref": "legacy-auth",
        }
    }
    requirements = runtime_credential_requirements(config)
    assert requirements["local"] == {"legacy-auth"}
    assert requirements["cloud"] == set()


def test_legacy_default_broker_defaults_missing_source_to_local():
    # The runtime resolves a source-less top-level broker as local_mqtt, so the
    # consumer must too or its credential is dropped from local staging.
    config = {"zendure_mqtt": {"host": "broker.local", "credentials_ref": "legacy-auth"}}
    consumers = collect_mqtt_credential_consumers(config)
    assert len(consumers) == 1
    assert consumers[0].source == "local_mqtt"
    assert consumers[0].credentials_ref == "legacy-auth"


def test_legacy_cloud_default_broker_is_a_cloud_consumer():
    config = {
        "zendure_mqtt": {
            "host": "cloud-broker",
            "source": "zendure_cloud_mqtt",
            "credentials_ref": "zendure-cloud",
        }
    }
    consumers, grouped = _by_component(config)
    assert len(consumers) == 1
    consumer = grouped["zendure_mqtt_broker"][0]
    assert consumer.credentials_ref == "zendure-cloud"
    assert consumer.source == "zendure_cloud_mqtt"
    assert consumer.config_path == "zendure_mqtt.credentials_ref"


def test_legacy_cloud_default_broker_is_a_cloud_requirement():
    from admin.mqtt_runtime_provisioning import runtime_credential_requirements

    config = {
        "zendure_mqtt": {
            "host": "cloud-broker",
            "source": "zendure_cloud_mqtt",
            "credentials_ref": "zendure-cloud",
        }
    }
    requirements = runtime_credential_requirements(config)
    assert requirements["cloud"] == {"zendure-cloud"}
    assert requirements["local"] == set()


def test_anonymous_legacy_default_broker_has_no_consumer():
    config = {"zendure_mqtt": {"enabled": True, "host": "broker.local", "port": 1883}}
    assert collect_mqtt_credential_consumers(config) == ()


def test_hostless_legacy_default_broker_has_no_consumer():
    # Without a host the runtime never resolves the top-level broker, so a stray
    # credentials_ref there is not a requirement.
    config = {"zendure_mqtt": {"credentials_ref": "legacy-auth"}}
    assert collect_mqtt_credential_consumers(config) == ()


def test_device_without_broker_ref_uses_the_legacy_default_consumer():
    config = {
        "devices": [
            {
                "type": "zendure_mqtt",
                "enabled": True,
                "sn": "SN-LEGACY1",
                "mqtt": {"topic_family": "iot"},
            }
        ],
        "zendure_mqtt": {"host": "broker.local", "credentials_ref": "legacy-auth"},
    }
    consumers = collect_mqtt_credential_consumers(config)
    assert [c.credentials_ref for c in consumers] == ["legacy-auth"]
    assert consumers[0].component == "zendure_mqtt_broker"


def test_default_plus_named_brokers_are_both_consumers():
    config = {
        "devices": [
            {
                "type": "zendure_mqtt",
                "enabled": True,
                "sn": "SN-SECONDARY",
                "mqtt": {"broker_ref": "secondary", "topic_family": "iot"},
            }
        ],
        "zendure_mqtt": {
            "host": "default.local",
            "source": "local_mqtt",
            "credentials_ref": "default-auth",
            "brokers": {
                "secondary": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "secondary.local",
                    "port": 1883,
                    "credentials_ref": "secondary-auth",
                }
            },
        },
    }
    consumers = collect_mqtt_credential_consumers(config)
    refs = sorted(c.credentials_ref for c in consumers)
    # Both distinct refs surface; neither overrides nor dedups the other.
    assert refs == ["default-auth", "secondary-auth"]
    assert all(c.component == "zendure_mqtt_broker" for c in consumers)
    paths = {c.credentials_ref: c.config_path for c in consumers}
    assert paths["default-auth"] == "zendure_mqtt.credentials_ref"
    assert paths["secondary-auth"] == "zendure_mqtt.brokers.secondary.credentials_ref"


def test_grid_meter_default_ref_consumes_the_legacy_default_credential():
    # A grid meter that selects the implicit ``default`` reaches its credential
    # through the same effective-profile resolver the runtime uses, so the
    # scanner collects the legacy top-level credential — not nothing — even when
    # named brokers exist alongside it.
    config = {
        "grid_meter": {
            "type": "mqtt",
            "mqtt": {"broker_ref": "default", "topic": "meter/power"},
        },
        "zendure_mqtt": {
            "host": "default.local",
            "source": "local_mqtt",
            "credentials_ref": "default-auth",
            "brokers": {
                "secondary": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "secondary.local",
                    "port": 1883,
                    "credentials_ref": "secondary-auth",
                }
            },
        },
    }
    got = {
        (c.credentials_ref, c.component)
        for c in collect_mqtt_credential_consumers(config)
    }
    assert ("default-auth", "grid_meter") in got
    assert ("default-auth", "zendure_mqtt_broker") in got
    grid = next(
        c
        for c in collect_mqtt_credential_consumers(config)
        if c.component == "grid_meter"
    )
    assert grid.broker_ref == "default"
    assert grid.source == "local_mqtt"


def test_legacy_default_and_cloud_named_cross_source_conflict():
    config = {
        "devices": [
            {
                "type": "zendure_mqtt",
                "enabled": True,
                "sn": "SN-CLOUD",
                "mqtt": {"broker_ref": "cloud-main", "topic_family": "cloud"},
            }
        ],
        "zendure_mqtt": {
            "host": "broker.local",
            "source": "local_mqtt",
            "credentials_ref": "shared",
            "brokers": {
                "cloud-main": {
                    "enabled": True,
                    "source": "zendure_cloud_mqtt",
                    "host": "mqtteu.zen-iot.com",
                    "port": 8883,
                    "credentials_ref": "shared",
                }
            },
        },
    }
    issues = find_mqtt_credential_consumer_issues(config)
    assert _codes(issues) == {"mqtt_credential_source_conflict"}
    assert issues[0]["credentials_ref"] == "shared"
    assert sorted(issues[0]["sources"]) == ["local_mqtt", "zendure_cloud_mqtt"]


@pytest.mark.parametrize("bad_ref", ["Bad Ref", "../secret", ""])
def test_invalid_legacy_default_ref_is_flagged_not_dropped(bad_ref):
    config = {"zendure_mqtt": {"host": "broker.local", "credentials_ref": bad_ref}}
    issues = find_mqtt_credential_consumer_issues(config)
    assert _codes(issues) == {"mqtt_credentials_ref_invalid"}
    assert issues[0]["path"] == "zendure_mqtt.credentials_ref"
    assert issues[0]["credentials_ref"] == bad_ref


# --- Phase 6: Core config validation issues ---------------------------------
# The same canonical/conflict contract Admin Apply enforces is available as a
# Core config-validation issue list so Preview and the Core validator agree.


def _codes(issues):
    return {issue["code"] for issue in issues}


def test_core_validation_flags_invalid_local_broker_ref():
    issues = find_mqtt_credential_consumer_issues(_local_broker_config("Bad Ref"))
    assert _codes(issues) == {"mqtt_credentials_ref_invalid"}
    issue = issues[0]
    assert issue["severity"] == "error"
    assert issue["credentials_ref"] == "Bad Ref"
    assert issue["path"] == "zendure_mqtt.brokers.local-main.credentials_ref"


def test_core_validation_flags_invalid_cloud_broker_ref():
    issues = find_mqtt_credential_consumer_issues(_cloud_broker_config("Bad Ref"))
    assert _codes(issues) == {"mqtt_credentials_ref_invalid"}
    assert issues[0]["credentials_ref"] == "Bad Ref"


def test_core_validation_flags_invalid_direct_grid_meter_ref():
    issues = find_mqtt_credential_consumer_issues(_direct_grid_meter_config("Bad Ref"))
    assert _codes(issues) == {"mqtt_credentials_ref_invalid"}
    assert issues[0]["path"] == "grid_meter.mqtt.credentials_ref"
    assert issues[0]["credentials_ref"] == "Bad Ref"


def test_core_validation_flags_cross_source_conflict_through_grid_meter():
    config = {
        "devices": [
            {
                "type": "zendure_mqtt",
                "enabled": True,
                "sn": "SN-CLOUD1",
                "mqtt": {"broker_ref": "cloud-main"},
            }
        ],
        "grid_meter": {
            "type": "mqtt",
            "mqtt": {
                "host": "broker.local",
                "port": 1883,
                "topic": "meter/power",
                "credentials_ref": "shared",
            },
        },
        "zendure_mqtt": {
            "brokers": {
                "cloud-main": {
                    "enabled": True,
                    "source": "zendure_cloud_mqtt",
                    "host": "mqtteu.zen-iot.com",
                    "port": 8883,
                    "credentials_ref": "shared",
                }
            }
        },
    }
    issues = find_mqtt_credential_consumer_issues(config)
    assert _codes(issues) == {"mqtt_credential_source_conflict"}
    issue = issues[0]
    assert issue["credentials_ref"] == "shared"
    assert sorted(issue["sources"]) == ["local_mqtt", "zendure_cloud_mqtt"]
    assert sorted(issue["consumers"]) == ["grid_meter", "zendure_mqtt_broker"]


def test_core_validation_accepts_valid_same_source_shared_ref():
    config = _local_broker_config(ref="home")
    config["zendure_mqtt"]["brokers"]["local-shed"] = {
        "enabled": True,
        "source": "local_mqtt",
        "host": "10.0.0.11",
        "port": 1883,
        "credentials_ref": "home",
    }
    config["devices"].append(
        {
            "type": "zendure_mqtt",
            "enabled": True,
            "sn": "SN-LOCAL2",
            "mqtt": {"broker_ref": "local-shed"},
        }
    )
    assert find_mqtt_credential_consumer_issues(config) == []


def test_core_validation_reports_no_secret_only_the_ref():
    issues = find_mqtt_credential_consumer_issues(_direct_grid_meter_config("Bad Ref"))
    blob = repr(issues)
    assert "password" not in blob
