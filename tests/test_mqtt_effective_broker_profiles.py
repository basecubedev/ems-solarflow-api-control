# SPDX-License-Identifier: AGPL-3.0-or-later
"""One Core resolver enumerates the broker profiles the EMS runtime builds.

The legacy single-broker install configures its broker directly under
``zendure_mqtt`` (top-level host/source/credentials_ref) and its devices omit
``mqtt.broker_ref``; the EMS runtime maps that to an implicit ``default``
profile. :func:`iter_effective_mqtt_broker_profiles` is the one Core helper that
enumerates those effective profiles — the implicit legacy default and every
named ``zendure_mqtt.brokers`` profile — so the credential-consumer scanner,
config validation and the runtime never each reinvent broker resolution and
drift apart.
"""

import pytest

from ems.zendure_mqtt.config_entries import (
    DEFAULT_BROKER_REF,
    ORIGIN_LEGACY_DEFAULT,
    ORIGIN_NAMED_PROFILE,
    effective_mqtt_broker_profile_map,
    get_effective_mqtt_broker_profile,
    iter_effective_mqtt_broker_profiles,
    zendure_mqtt_broker_ref,
)
from ems.mqtt_credentials import MqttCredentials
from ems.zendure_mqtt.runtime import load_zendure_mqtt_broker_configs

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]


class _StubResolver:
    """Resolve any ref to complete credentials so broker refs (not credential
    files) are what the runtime/resolver parity test actually compares."""

    def resolve(self, ref):
        return MqttCredentials("user", "pass", "client", "appkey")


def _by_ref(config):
    return {
        profile.broker_ref: profile
        for profile in iter_effective_mqtt_broker_profiles(config)
    }


# --- legacy top-level broker maps to the implicit ``default`` profile --------


def test_legacy_single_broker_is_the_default_profile():
    config = {
        "zendure_mqtt": {
            "host": "broker.local",
            "source": "local_mqtt",
            "credentials_ref": "legacy-auth",
        }
    }
    profiles = _by_ref(config)
    assert set(profiles) == {DEFAULT_BROKER_REF}
    default = profiles[DEFAULT_BROKER_REF]
    assert default.origin == ORIGIN_LEGACY_DEFAULT
    # The profile's config is the top-level block, so credential/source reads
    # come straight from the legacy fields.
    assert default.config.get("credentials_ref") == "legacy-auth"
    assert default.config.get("host") == "broker.local"


def test_named_brokers_are_named_profiles():
    config = {
        "zendure_mqtt": {
            "brokers": {
                "home": {
                    "host": "broker.local",
                    "source": "local_mqtt",
                    "credentials_ref": "home-auth",
                }
            }
        }
    }
    profiles = _by_ref(config)
    assert set(profiles) == {"home"}
    assert profiles["home"].origin == ORIGIN_NAMED_PROFILE
    assert profiles["home"].config.get("credentials_ref") == "home-auth"


def test_default_plus_named_brokers_are_both_effective():
    config = {
        "zendure_mqtt": {
            "host": "default.local",
            "credentials_ref": "default-auth",
            "brokers": {
                "secondary": {
                    "host": "secondary.local",
                    "credentials_ref": "secondary-auth",
                }
            },
        }
    }
    profiles = _by_ref(config)
    assert set(profiles) == {DEFAULT_BROKER_REF, "secondary"}
    assert profiles[DEFAULT_BROKER_REF].origin == ORIGIN_LEGACY_DEFAULT
    assert profiles["secondary"].origin == ORIGIN_NAMED_PROFILE


def test_named_brokers_without_top_level_host_have_no_default_profile():
    config = {
        "zendure_mqtt": {
            "brokers": {
                "home": {"host": "broker.local", "credentials_ref": "home-auth"}
            }
        }
    }
    assert set(_by_ref(config)) == {"home"}


def test_bare_anonymous_zendure_mqtt_still_has_a_default_profile():
    # No brokers block at all: the single implicit default profile still exists
    # (anonymous, host-less) so the resolution stays total.
    assert set(_by_ref({"zendure_mqtt": {"enabled": True}})) == {DEFAULT_BROKER_REF}


@pytest.mark.parametrize("config", [{}, None, {"zendure_mqtt": "nope"}, "x"])
def test_missing_or_malformed_config_yields_no_profiles(config):
    # The helper must never raise on junk input.
    assert list(iter_effective_mqtt_broker_profiles(config)) == []


# --- scanner and runtime resolve the SAME set of brokers --------------------


@pytest.mark.parametrize(
    "config",
    [
        {"zendure_mqtt": {"host": "broker.local", "credentials_ref": "legacy-auth"}},
        {
            "zendure_mqtt": {
                "brokers": {
                    "home": {"host": "a.local", "credentials_ref": "home-auth"},
                    "shed": {"host": "b.local", "credentials_ref": "shed-auth"},
                }
            }
        },
        {
            "zendure_mqtt": {
                "host": "default.local",
                "credentials_ref": "default-auth",
                "brokers": {
                    "secondary": {
                        "host": "secondary.local",
                        "credentials_ref": "secondary-auth",
                    }
                },
            }
        },
    ],
)
def test_runtime_and_resolver_agree_on_broker_refs(config):
    resolver_refs = {p.broker_ref for p in iter_effective_mqtt_broker_profiles(config)}
    runtime_brokers, _errors, _stale = load_zendure_mqtt_broker_configs(
        config["zendure_mqtt"], credential_resolver=_StubResolver()
    )
    assert resolver_refs == set(runtime_brokers)


# --- single-ref lookup and the collision-free map ---------------------------


def test_get_effective_profile_returns_the_named_profile():
    config = {
        "zendure_mqtt": {
            "brokers": {"home": {"host": "home.local", "credentials_ref": "home-auth"}}
        }
    }
    profile = get_effective_mqtt_broker_profile(config, "home")
    assert profile.broker_ref == "home"
    assert profile.origin == ORIGIN_NAMED_PROFILE
    assert profile.config.get("host") == "home.local"


def test_get_effective_default_resolves_legacy_top_level_beside_named():
    config = {
        "zendure_mqtt": {
            "host": "default.local",
            "credentials_ref": "default-auth",
            "brokers": {"secondary": {"host": "secondary.local"}},
        }
    }
    # The implicit default resolves to the top-level profile regardless of the
    # presence of named brokers.
    default = get_effective_mqtt_broker_profile(config, DEFAULT_BROKER_REF)
    assert default.origin == ORIGIN_LEGACY_DEFAULT
    assert default.config.get("host") == "default.local"
    assert get_effective_mqtt_broker_profile(config, "secondary").config[
        "host"
    ] == "secondary.local"


def test_get_effective_unknown_ref_is_none():
    config = {"zendure_mqtt": {"host": "b.local"}}
    assert get_effective_mqtt_broker_profile(config, "nope") is None


def test_effective_map_has_unique_refs():
    config = {
        "zendure_mqtt": {
            "host": "default.local",
            "brokers": {"secondary": {"host": "secondary.local"}},
        }
    }
    profiles = effective_mqtt_broker_profile_map(config)
    assert set(profiles) == {DEFAULT_BROKER_REF, "secondary"}


def test_effective_map_never_lets_named_default_overwrite_legacy():
    config = {
        "zendure_mqtt": {
            "host": "top.local",
            "brokers": {"default": {"host": "collide.local"}},
        }
    }
    profiles = effective_mqtt_broker_profile_map(config)
    # Exactly one entry for the default ref, and it is the legacy top-level one.
    assert profiles[DEFAULT_BROKER_REF].origin == ORIGIN_LEGACY_DEFAULT
    assert profiles[DEFAULT_BROKER_REF].config.get("host") == "top.local"


def test_device_without_broker_ref_selects_the_default_profile():
    device = {"type": "zendure_mqtt", "enabled": True, "sn": "SN1", "mqtt": {}}
    config = {
        "devices": [device],
        "zendure_mqtt": {"host": "broker.local", "credentials_ref": "legacy-auth"},
    }
    # The runtime maps a broker_ref-less device to the implicit default...
    assert zendure_mqtt_broker_ref(device) == DEFAULT_BROKER_REF
    # ...and the resolver exposes exactly that profile.
    assert DEFAULT_BROKER_REF in _by_ref(config)
