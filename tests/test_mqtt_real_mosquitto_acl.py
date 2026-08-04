# SPDX-License-Identifier: AGPL-3.0-or-later
"""Two ACL accounts on one broker endpoint stay isolated per credentials_ref.

Release gate: one Mosquitto instance, two accounts each scoped to their own
topic tree, and two connection profiles sharing host/port/TLS but differing only
by ``credentials_ref``. Proves the runtime builds two independent services, each
device sees only its own tree, and there is no first-credential fallback or
cross-publish.
"""

import pytest

from ems.mqtt_credentials import MqttCredentials
from tests.helpers.mosquitto import (
    require_real_broker_environment,
    mosquitto_acl_broker,
    publish_once,
    publish_until,
)

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.e2e,
    pytest.mark.docker,
]


require_real_broker_environment()


class _Resolver:
    def __init__(self, records):
        self._records = records

    def resolve(self, ref):
        creds = self._records[ref]
        return MqttCredentials(creds[0], creds[1])


def _config(host, port):
    # Two profiles, identical endpoint, distinct credentials_ref; each device is
    # bound to its own broker profile with no cross-broker satisfaction.
    return {
        "zendure_mqtt": {
            "enabled": True,
            "brokers": {
                "tree_a": {
                    "enabled": True, "source": "local_mqtt",
                    "host": host, "port": port, "credentials_ref": "cred_a",
                },
                "tree_b": {
                    "enabled": True, "source": "local_mqtt",
                    "host": host, "port": port, "credentials_ref": "cred_b",
                },
            },
        },
        "devices": [
            {
                "type": "zendure_mqtt", "name": "DevA", "sn": "SN-A",
                "mqtt": {"broker_ref": "tree_a", "topic_family": "zensdk_ha_scalar",
                         "device_id": "SN-A"},
            },
            {
                "type": "zendure_mqtt", "name": "DevB", "sn": "SN-B",
                "mqtt": {"broker_ref": "tree_b", "topic_family": "zensdk_ha_scalar",
                         "device_id": "SN-B"},
            },
        ],
    }


def _metric(runtime, identifier, metric):
    snap = runtime.snapshots().get(identifier)
    metrics = getattr(snap, "metrics", None) or {} if snap else {}
    return metrics.get(metric)


def test_two_acl_accounts_stay_isolated_per_credentials_ref(tmp_path):
    from ems.zendure_mqtt.runtime import build_zendure_mqtt_runtime

    # Each account is scoped by ACL to its own device subtree in the shared
    # Zendure scalar namespace.
    accounts = [
        ("user_a", "pass_a", "Zendure/sensor/SN-A/#"),
        ("user_b", "pass_b", "Zendure/sensor/SN-B/#"),
    ]
    resolver = _Resolver({
        "cred_a": ("user_a", "pass_a"),
        "cred_b": ("user_b", "pass_b"),
    })

    with mosquitto_acl_broker(tmp_path, accounts) as (host, port):
        runtime = build_zendure_mqtt_runtime(_config(host, port), credential_resolver=resolver)
        # Two brokers => two independent services, no shared first credential.
        assert runtime.broker_count == 2
        runtime.start()
        try:
            # Each account publishes into its own tree with its own credentials.
            # Both devices receive their own metric only if each service
            # authenticated with its OWN credential: a first-credential fallback
            # would make service B connect as user_a, which the ACL forbids from
            # reading tree B, so SN-B would never arrive.
            def _publish_both():
                publish_once(host, port, "Zendure/sensor/SN-A/electricLevel", "41",
                             username="user_a", password="pass_a")
                publish_once(host, port, "Zendure/sensor/SN-B/electricLevel", "77",
                             username="user_b", password="pass_b")

            publish_until(
                _publish_both,
                lambda: _metric(runtime, "SN-A", "electricLevel") is not None
                and _metric(runtime, "SN-B", "electricLevel") is not None,
                message="ACL-scoped metrics never both arrived",
            )
            snaps = runtime.snapshots()
            assert set(snaps.keys()) <= {"SN-A", "SN-B"}
            a_metrics = getattr(snaps.get("SN-A"), "metrics", {}) or {}
            b_metrics = getattr(snaps.get("SN-B"), "metrics", {}) or {}
            assert "electricLevel" in a_metrics
            assert "electricLevel" in b_metrics
        finally:
            runtime.stop()
