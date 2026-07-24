# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared consumer/broker/credential contract matrix across the whole stack.

Every supported broker/consumer shape — legacy top-level default (local and
cloud), named local/cloud brokers, default-plus-named, shared references, grid
meters (named-broker and direct), plus the credential failure modes (missing,
non-canonical, cross-source, whitespace) — is exercised once and asserted at
each contract layer that must agree: consumer detection, per-source
requirement, config-integrity validation, seeded-record credential validation,
and (for accepted configurations) EMS runtime reconstruction. A config that any
layer rejects must be rejected with a stable code and reconstruct nothing new.
"""

import pytest

from admin.credential_store import (
    CredentialStore,
    MqttCredentialsRefInvalidError,
    MqttCredentialSourceConflictError,
)
from admin.mqtt_runtime_provisioning import (
    runtime_credential_requirements,
    validate_all_runtime_credentials,
    validate_config_credential_references,
)
from ems import config as cfg
from ems.mqtt_credentials import (
    collect_mqtt_credential_consumers,
    find_mqtt_credential_consumer_issues,
)
from ems.zendure_mqtt.runtime import build_zendure_mqtt_runtime
from tests.helpers.fake_mqtt import FakeMqttNetwork

pytestmark = pytest.mark.simulation


def _mqtt_device(sn, broker_ref=None, topic_family="iot"):
    # serial_number/mqtt.device_id make the entry addressable so the runtime
    # (not only the consumer scanner) builds a broker for it.
    mqtt = {"topic_family": topic_family, "device_id": sn}
    if broker_ref is not None:
        mqtt["broker_ref"] = broker_ref
    return {
        "type": "zendure_mqtt",
        "enabled": True,
        "name": f"dev-{sn}",
        "serial_number": sn,
        "mqtt": mqtt,
    }


def _local_broker(host, credentials_ref=None, **extra):
    profile = {"enabled": True, "source": "local_mqtt", "host": host, "port": 1883}
    if credentials_ref is not None:
        profile["credentials_ref"] = credentials_ref
    profile.update(extra)
    return profile


def _cloud_broker(credentials_ref):
    return {
        "enabled": True,
        "source": "zendure_cloud_mqtt",
        "host": "mqtteu.zen-iot.com",
        "port": 8883,
        "credentials_ref": credentials_ref,
    }


# Runtime credential records to seed, keyed by ref. "local"/"cloud" pick the
# store method; whitespace values model a present-but-unusable field.
LOCAL_VALID = {"source": "local", "username": "user", "password": "pass"}
LOCAL_WS = {"source": "local", "username": "user", "password": "   "}
CLOUD_VALID = {
    "source": "cloud",
    "username": "user",
    "password": "pass",
    "client_id": "cid",
    "app_key": "ak",
}
CLOUD_WS = {**CLOUD_VALID, "app_key": "   "}


CASES = {
    "legacy_local_default_with_credentials": {
        "config": {
            "devices": [_mqtt_device("SN1")],
            "zendure_mqtt": {"host": "b.local", "credentials_ref": "legacy"},
        },
        "consumers": {("legacy", "zendure_mqtt_broker")},
        "local": {"legacy"},
        "cloud": set(),
        "config_valid": True,
        "issue_code": None,
        "records": {"legacy": LOCAL_VALID},
        "affected": [],
        "runtime_clean": True,
    },
    "legacy_cloud_default": {
        "config": {
            "devices": [_mqtt_device("SN1", topic_family="cloud")],
            "zendure_mqtt": {
                "host": "mqtteu.zen-iot.com",
                "source": "zendure_cloud_mqtt",
                "credentials_ref": "cloudref",
            },
        },
        "consumers": {("cloudref", "zendure_mqtt_broker")},
        "local": set(),
        "cloud": {"cloudref"},
        "config_valid": True,
        "issue_code": None,
        "records": {"cloudref": CLOUD_VALID},
        "affected": [],
        "runtime_clean": True,
    },
    "legacy_default_without_credentials": {
        "config": {
            "devices": [_mqtt_device("SN1")],
            "zendure_mqtt": {"host": "b.local"},
        },
        "consumers": set(),
        "local": set(),
        "cloud": set(),
        "config_valid": True,
        "issue_code": None,
        "records": {},
        "affected": [],
        "runtime_clean": True,
    },
    "named_local_broker": {
        "config": {
            "devices": [_mqtt_device("SN1", broker_ref="home")],
            "zendure_mqtt": {"brokers": {"home": _local_broker("b.local", "homeref")}},
        },
        "consumers": {("homeref", "zendure_mqtt_broker")},
        "local": {"homeref"},
        "cloud": set(),
        "config_valid": True,
        "issue_code": None,
        "records": {"homeref": LOCAL_VALID},
        "affected": [],
        "runtime_clean": True,
    },
    "named_cloud_broker": {
        "config": {
            "devices": [_mqtt_device("SN1", broker_ref="cloud")],
            "zendure_mqtt": {"brokers": {"cloud": _cloud_broker("cloudref")}},
        },
        "consumers": {("cloudref", "zendure_mqtt_broker")},
        "local": set(),
        "cloud": {"cloudref"},
        "config_valid": True,
        "issue_code": None,
        "records": {"cloudref": CLOUD_VALID},
        "affected": [],
        "runtime_clean": True,
    },
    "default_plus_named_brokers": {
        "config": {
            "devices": [
                _mqtt_device("SN1"),
                _mqtt_device("SN2", broker_ref="secondary"),
            ],
            "zendure_mqtt": {
                "host": "default.local",
                "credentials_ref": "defaultref",
                "brokers": {"secondary": _local_broker("second.local", "secondref")},
            },
        },
        "consumers": {
            ("defaultref", "zendure_mqtt_broker"),
            ("secondref", "zendure_mqtt_broker"),
        },
        "local": {"defaultref", "secondref"},
        "cloud": set(),
        "config_valid": True,
        "issue_code": None,
        "records": {"defaultref": LOCAL_VALID, "secondref": LOCAL_VALID},
        "affected": [],
        "runtime_clean": True,
    },
    "same_ref_multiple_local_consumers": {
        "config": {
            "devices": [
                _mqtt_device("SN1", broker_ref="home"),
                _mqtt_device("SN2", broker_ref="shed"),
            ],
            "zendure_mqtt": {
                "brokers": {
                    "home": _local_broker("a.local", "shared"),
                    "shed": _local_broker("b.local", "shared"),
                }
            },
        },
        "consumers": {("shared", "zendure_mqtt_broker")},
        "local": {"shared"},
        "cloud": set(),
        "config_valid": True,
        "issue_code": None,
        "records": {"shared": LOCAL_VALID},
        "affected": [],
        "runtime_clean": True,
    },
    "same_ref_local_and_cloud": {
        "config": {
            "devices": [
                _mqtt_device("SN1", broker_ref="home"),
                _mqtt_device("SN2", broker_ref="cloud", topic_family="cloud"),
            ],
            "zendure_mqtt": {
                "brokers": {
                    "home": _local_broker("a.local", "shared"),
                    "cloud": _cloud_broker("shared"),
                }
            },
        },
        "consumers": {("shared", "zendure_mqtt_broker")},
        "local": {"shared"},
        "cloud": {"shared"},
        "config_valid": False,
        "config_error": MqttCredentialSourceConflictError,
        "issue_code": "mqtt_credential_source_conflict",
    },
    "grid_meter_named_broker": {
        "config": {
            "devices": [_mqtt_device("SN1", broker_ref="home")],
            "grid_meter": {
                "type": "mqtt",
                "mqtt": {"broker_ref": "home", "topic": "meter/power"},
            },
            "zendure_mqtt": {"brokers": {"home": _local_broker("b.local", "homeref")}},
        },
        "consumers": {
            ("homeref", "zendure_mqtt_broker"),
            ("homeref", "grid_meter"),
        },
        "local": {"homeref"},
        "cloud": set(),
        "config_valid": True,
        "issue_code": None,
        "records": {"homeref": LOCAL_VALID},
        "affected": [],
        "runtime_clean": True,
        "grid_meter_host": "b.local",
    },
    "grid_meter_direct_settings": {
        "config": {
            "devices": [_mqtt_device("SN1")],
            "grid_meter": {
                "type": "mqtt",
                "mqtt": {
                    "host": "b.local",
                    "port": 1883,
                    "topic": "meter/power",
                    "credentials_ref": "meterref",
                },
            },
            "zendure_mqtt": {"host": "b.local"},
        },
        "consumers": {("meterref", "grid_meter")},
        "local": {"meterref"},
        "cloud": set(),
        "config_valid": True,
        "issue_code": None,
        "records": {"meterref": LOCAL_VALID},
        "affected": [],
        "runtime_clean": True,
        "grid_meter_host": "b.local",
    },
    "grid_meter_effective_default": {
        # A grid meter selects the implicit ``default`` while a named broker also
        # exists: it must resolve the legacy top-level broker, and both the broker
        # and the grid meter consume its credential.
        "config": {
            "devices": [_mqtt_device("SN1")],
            "grid_meter": {
                "type": "mqtt",
                "mqtt": {"broker_ref": "default", "topic": "meter/power"},
            },
            "zendure_mqtt": {
                "host": "default.local",
                "source": "local_mqtt",
                "credentials_ref": "defaultref",
                "brokers": {"secondary": _local_broker("second.local")},
            },
        },
        "consumers": {
            ("defaultref", "zendure_mqtt_broker"),
            ("defaultref", "grid_meter"),
        },
        "local": {"defaultref"},
        "cloud": set(),
        "config_valid": True,
        "issue_code": None,
        "records": {"defaultref": LOCAL_VALID},
        "affected": [],
        "runtime_clean": True,
        "grid_meter_host": "default.local",
    },
    "whitespace_local_credential": {
        "config": {
            "devices": [_mqtt_device("SN1", broker_ref="home")],
            "zendure_mqtt": {"brokers": {"home": _local_broker("b.local", "homeref")}},
        },
        "consumers": {("homeref", "zendure_mqtt_broker")},
        "local": {"homeref"},
        "cloud": set(),
        "config_valid": True,
        "issue_code": None,
        "records": {"homeref": LOCAL_WS},
        "affected": ["homeref"],
    },
    "whitespace_cloud_credential": {
        "config": {
            "devices": [_mqtt_device("SN1", broker_ref="cloud")],
            "zendure_mqtt": {"brokers": {"cloud": _cloud_broker("cloudref")}},
        },
        "consumers": {("cloudref", "zendure_mqtt_broker")},
        "local": set(),
        "cloud": {"cloudref"},
        "config_valid": True,
        "issue_code": None,
        "records": {"cloudref": CLOUD_WS},
        "affected": ["cloudref"],
    },
    "missing_legacy_default_credential": {
        "config": {
            "devices": [_mqtt_device("SN1")],
            "zendure_mqtt": {"host": "b.local", "credentials_ref": "missing"},
        },
        "consumers": {("missing", "zendure_mqtt_broker")},
        "local": {"missing"},
        "cloud": set(),
        "config_valid": True,
        "issue_code": None,
        "records": {},
        "affected": ["missing"],
    },
    "invalid_legacy_default_ref": {
        "config": {
            "devices": [_mqtt_device("SN1")],
            "zendure_mqtt": {"host": "b.local", "credentials_ref": "Bad Ref"},
        },
        "consumers": {("Bad Ref", "zendure_mqtt_broker")},
        "config_valid": False,
        "config_error": MqttCredentialsRefInvalidError,
        "issue_code": "mqtt_credentials_ref_invalid",
    },
}


def _seed(store, records):
    for ref, spec in records.items():
        if spec["source"] == "cloud":
            store.save_mqtt_cloud_runtime_secret(
                ref,
                username=spec["username"],
                password=spec["password"],
                client_id=spec["client_id"],
                app_key=spec["app_key"],
            )
        else:
            store.save_mqtt_broker_secret(ref, spec["username"], spec["password"])


# --- consumer detection ------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CASES))
def test_consumer_detection(name):
    case = CASES[name]
    got = {
        (c.credentials_ref, c.component)
        for c in collect_mqtt_credential_consumers(case["config"])
    }
    assert got == case["consumers"]


# --- per-source requirement --------------------------------------------------


@pytest.mark.parametrize(
    "name", sorted(n for n, c in CASES.items() if c.get("config_valid"))
)
def test_source_requirement(name):
    case = CASES[name]
    requirements = runtime_credential_requirements(case["config"])
    assert requirements["local"] == case["local"]
    assert requirements["cloud"] == case["cloud"]


# --- config-integrity validation (canonical + single-source) -----------------


@pytest.mark.parametrize("name", sorted(CASES))
def test_config_integrity_validation(name):
    case = CASES[name]
    if case["config_valid"]:
        validate_config_credential_references(case["config"])  # must not raise
        assert find_mqtt_credential_consumer_issues(case["config"]) == []
    else:
        with pytest.raises(case["config_error"]):
            validate_config_credential_references(case["config"])
        codes = {i["code"] for i in find_mqtt_credential_consumer_issues(case["config"])}
        assert case["issue_code"] in codes


# --- seeded-record credential validation ------------------------------------


@pytest.mark.parametrize(
    "name", sorted(n for n, c in CASES.items() if c.get("config_valid"))
)
def test_seeded_credential_validation(name, tmp_path):
    case = CASES[name]
    store = CredentialStore(config_dir=tmp_path / "config")
    _seed(store, case["records"])
    affected = validate_all_runtime_credentials(
        case["config"], credential_store=store
    )
    assert sorted(affected) == sorted(case["affected"])


# --- runtime reconstruction for the clean, valid configurations -------------


@pytest.mark.parametrize(
    "name", sorted(n for n, c in CASES.items() if c.get("runtime_clean"))
)
def test_runtime_reconstruction_is_clean(name, tmp_path):
    from ems.mqtt_credentials import FileMqttCredentialResolver

    case = CASES[name]
    store = CredentialStore(config_dir=tmp_path / "config")
    _seed(store, case["records"])
    network = FakeMqttNetwork()
    runtime = build_zendure_mqtt_runtime(
        case["config"],
        service_factory=network.telemetry_service_factory(),
        credential_resolver=FileMqttCredentialResolver(store.secrets_dir),
    )
    try:
        issues = {b["broker_ref"]: b["issue"] for b in runtime.status()["brokers"]}
        assert issues, "runtime built no brokers for a valid config"
        assert all(v is None for v in issues.values()), issues
    finally:
        runtime.stop()


# --- runtime and resolver build the same broker refs ------------------------


@pytest.mark.parametrize(
    "name", sorted(n for n, c in CASES.items() if c.get("config_valid"))
)
def test_runtime_and_resolver_broker_refs_agree(name, tmp_path):
    from ems.mqtt_credentials import FileMqttCredentialResolver
    from ems.zendure_mqtt.config_entries import effective_mqtt_broker_profile_map
    from ems.zendure_mqtt.runtime import load_zendure_mqtt_broker_configs

    case = CASES[name]
    store = CredentialStore(config_dir=tmp_path / "config")
    _seed(store, case.get("records", {}))
    resolver_refs = set(effective_mqtt_broker_profile_map(case["config"]))
    runtime_brokers, errors, _stale = load_zendure_mqtt_broker_configs(
        case["config"].get("zendure_mqtt"),
        credential_resolver=FileMqttCredentialResolver(store.secrets_dir),
    )
    # A ref the resolver names either builds a broker or lands in errors (e.g. its
    # credential record is whitespace/missing); either way the resolved ref set is
    # identical — the runtime never invents or drops a broker ref.
    assert set(runtime_brokers) | set(errors) == resolver_refs


# --- a grid meter resolves the same broker the resolver names ----------------


@pytest.mark.parametrize(
    "name", sorted(n for n, c in CASES.items() if c.get("grid_meter_host"))
)
def test_grid_meter_resolves_the_effective_broker(name):
    case = CASES[name]
    resolved = cfg.resolve_grid_meter_mqtt_settings(case["config"])
    assert resolved["host"] == case["grid_meter_host"]


# --- rejected shapes: Core and Maintenance agree, config stays byte-identical -

REJECTED = {
    "named_default_reserved": {
        "config": {
            "devices": [_mqtt_device("SN1")],
            "zendure_mqtt": {
                "host": "top.local",
                "credentials_ref": "topref",
                "brokers": {"default": _local_broker("collide.local", "collideref")},
            },
        },
        "code": "mqtt_broker_ref_reserved",
    },
    "grid_meter_unknown_broker": {
        "config": {
            "devices": [_mqtt_device("SN1")],
            "grid_meter": {
                "type": "mqtt",
                "mqtt": {"broker_ref": "nope", "topic": "meter/power"},
            },
            "zendure_mqtt": {"brokers": {"home": _local_broker("b.local", "homeref")}},
        },
        # The grid-meter resolver raises; Maintenance surfaces it as this code.
        "code": "grid_meter_mqtt_invalid",
    },
}


@pytest.mark.parametrize("name", sorted(REJECTED))
def test_rejected_shape_fails_core_and_maintenance(name):
    from admin.maintenance_config import _validate

    case = REJECTED[name]
    codes = {i["code"] for i in _validate(case["config"])["errors"]}
    assert case["code"] in codes


def test_reserved_named_default_is_rejected_by_core_validation():
    from ems.zendure_mqtt.config_entries import find_reserved_mqtt_broker_ref_issues

    config = REJECTED["named_default_reserved"]["config"]
    codes = {i["code"] for i in find_reserved_mqtt_broker_ref_issues(config)}
    assert "mqtt_broker_ref_reserved" in codes


def test_unknown_grid_meter_broker_is_rejected_by_core_resolver():
    config = REJECTED["grid_meter_unknown_broker"]["config"]
    with pytest.raises(ValueError, match="not a configured"):
        cfg.resolve_grid_meter_mqtt_settings(config)


# --- grid-meter broker_ref XOR inline settings (Phase 2 case in the matrix) --


def test_grid_meter_broker_ref_plus_credentials_ref_is_config_rejected():
    config = {
        "devices": [_mqtt_device("SN1", broker_ref="home")],
        "grid_meter": {
            "type": "mqtt",
            "mqtt": {
                "broker_ref": "home",
                "topic": "meter/power",
                "credentials_ref": "meter-secret-ref",
            },
        },
        "zendure_mqtt": {"brokers": {"home": _local_broker("b.local", "homeref")}},
    }
    with pytest.raises(cfg.MqttBrokerReferenceAmbiguousError) as caught:
        cfg.resolve_grid_meter_mqtt_settings(config)
    assert caught.value.code == "mqtt_broker_reference_ambiguous"
    assert "credentials_ref" in caught.value.fields
    # Only field names surface, never the configured reference value.
    assert "meter-secret-ref" not in str(caught.value)
