# SPDX-License-Identifier: AGPL-3.0-or-later
"""Diagnostic broker views resolve from the one Core effective resolver.

The runtime, the credential scanner, the grid-meter resolver and diagnostics
must all see the SAME effective broker profiles. The sanitized diagnostic view
builder (:func:`zendure_mqtt_broker_profile_views`) must therefore derive its
profiles from :func:`iter_effective_mqtt_broker_profiles` — the single Core
resolver — rather than re-iterating ``zendure_mqtt.brokers`` with its own
priority/override logic. Otherwise a reserved named ``default`` could appear as
the effective default in diagnostics while the runtime keeps the legacy
top-level broker, and downstream components would disagree on which broker a ref
points at.
"""

import pytest

from ems.zendure_mqtt.config_entries import (
    DEFAULT_BROKER_REF,
    iter_effective_mqtt_broker_profiles,
    zendure_mqtt_broker_profile_views,
)
from ems.mqtt_credentials import MqttCredentials
from ems.zendure_mqtt.runtime import load_zendure_mqtt_broker_configs

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]


class _StubResolver:
    def resolve(self, ref):
        return MqttCredentials("user", "pass", "client", "appkey")


def _resolver_hosts(zendure_mqtt):
    return {
        p.broker_ref: p.config.get("host")
        for p in iter_effective_mqtt_broker_profiles({"zendure_mqtt": zendure_mqtt})
    }


# --- legacy default beside a secondary broker -------------------------------


def test_legacy_default_plus_secondary_matches_resolver():
    zendure_mqtt = {
        "host": "top.local",
        "brokers": {"secondary": {"host": "secondary.local"}},
    }
    views = zendure_mqtt_broker_profile_views(zendure_mqtt)
    assert set(views) == {DEFAULT_BROKER_REF, "secondary"}
    assert views[DEFAULT_BROKER_REF].host == "top.local"
    assert views["secondary"].host == "secondary.local"
    # Views and the Core resolver expose the same refs and hosts.
    assert {ref: v.host for ref, v in views.items()} == _resolver_hosts(zendure_mqtt)


# --- reserved named ``default`` never overrides the legacy default -----------


def test_named_default_never_overrides_legacy_default_in_views():
    zendure_mqtt = {
        "host": "top.local",
        "brokers": {
            "default": {"host": "named.local"},
            "secondary": {"host": "secondary.local"},
        },
    }
    views = zendure_mqtt_broker_profile_views(zendure_mqtt)
    # The reserved named default is never shown as the effective default.
    assert views[DEFAULT_BROKER_REF].host == "top.local"
    hosts = {v.host for v in views.values()}
    assert "named.local" not in hosts
    # Runtime and diagnostics agree on refs and hosts.
    assert {ref: v.host for ref, v in views.items()} == _resolver_hosts(zendure_mqtt)


def test_views_match_runtime_hosts_when_named_default_present():
    zendure_mqtt = {
        "host": "top.local",
        "brokers": {
            "default": {"host": "named.local"},
            "secondary": {"host": "secondary.local"},
        },
    }
    views = zendure_mqtt_broker_profile_views(zendure_mqtt)
    runtime_brokers, _errors, _stale = load_zendure_mqtt_broker_configs(
        zendure_mqtt, credential_resolver=_StubResolver()
    )
    view_hosts = {ref: v.host for ref, v in views.items()}
    runtime_hosts = {ref: b.host for ref, b in runtime_brokers.items()}
    assert view_hosts == runtime_hosts


# --- named ``default`` without a top-level host has no effective default -----


def test_named_default_without_top_level_host_yields_no_default_view():
    zendure_mqtt = {"brokers": {"default": {"host": "named.local"}}}
    views = zendure_mqtt_broker_profile_views(zendure_mqtt)
    assert DEFAULT_BROKER_REF not in views
    assert all(v.host != "named.local" for v in views.values())
    assert set(views) == set(_resolver_hosts(zendure_mqtt))
