# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runtime control diagnostics expose the real control state, not a bare flag.

``describe()`` reports explicit ``control_requested`` / ``control_supported`` /
``control_ready`` fields, the resolved hardware identity and power-write profile,
the block reason when control is unavailable, and the operations the adapter
supports versus the operations the automatic controller can actually reach — so
Hyper charge-adapter support is visibly distinct from automatic-controller
support.
"""

import pytest

from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient
from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON

pytestmark = pytest.mark.simulation


class FakeService:
    def __init__(self):
        self.connected = False

    def snapshot_status(self, device_id, *, now_monotonic=None):
        from ems.zendure_mqtt.service import classify_snapshot

        return classify_snapshot(None, 60.0, now_monotonic=now_monotonic or 0.0)

    def publish_output_limit(self, topic, payload):
        return True


def _device(hardware_profile=None, **kwargs):
    return ZendureMqttDeviceClient(
        "WR-MQTT",
        FakeService(),
        device_id="DEV",
        topic_family=FAMILY_LEGACY_JSON,
        source="local_mqtt",
        product_key="PK",
        hardware_profile=hardware_profile,
        **kwargs,
    )


def test_hyper_diagnostics_distinguish_adapter_charge_from_controller():
    d = _device("hyper_2000").describe(now_monotonic=0.0)
    assert d["control_requested"] is True
    assert d["control_supported"] is True
    assert d["hardware_profile"] == "hyper_2000"
    assert d["power_write_profile"] == "legacy_object_device_automation"
    assert set(d["supported_operations"]) == {"discharge", "idle", "charge"}
    # The adapter understands charge; the automatic controller does not emit it.
    assert set(d["controller_reachable_operations"]) == {"discharge", "idle"}
    assert d["control_block_reason"] is None
    assert d["last_command_state"] is None
    assert isinstance(d["control_ready"], bool)


def test_deferred_profile_diagnostics_report_block_reason():
    d = _device("ace_1500").describe(now_monotonic=0.0)
    assert d["control_requested"] is True
    assert d["control_supported"] is False
    assert d["control_ready"] is False
    assert d["hardware_profile"] == "ace_1500"
    assert d["power_write_profile"] == "telemetry_only"
    assert d["supported_operations"] == []
    assert d["controller_reachable_operations"] == []
    assert d["control_block_reason"] == "hardware_profile_deferred"


def test_invalid_profile_diagnostics_report_unknown_block_reason():
    d = _device("typo_profile").describe(now_monotonic=0.0)
    assert d["control_supported"] is False
    assert d["control_ready"] is False
    assert d["control_block_reason"] == "hardware_profile_unknown"


def test_explicit_escape_hatch_diagnostics_report_supported():
    # The isolated custom escape hatch (explicit custom_properties_write + an
    # explicit write_topic) is the only no-profile write method that stays
    # supported; the built-in legacy_properties_write no longer authorizes.
    d = _device(
        write_protocol="custom_properties_write",
        write_topic="Zendure/number/CUST/outputLimit",
    ).describe(now_monotonic=0.0)
    assert d["control_supported"] is True
    assert d["control_block_reason"] is None
    assert set(d["controller_reachable_operations"]) == {"discharge", "idle"}


def test_built_in_legacy_protocol_without_profile_reports_missing():
    # A bare built-in legacy_properties_write is no longer an escape hatch.
    d = _device(write_protocol="legacy_properties_write").describe(now_monotonic=0.0)
    assert d["control_supported"] is False
    assert d["control_block_reason"] == "hardware_profile_missing"


def test_last_command_state_is_published_never_confirmed_after_write():
    dev = _device("hyper_2000")
    assert dev.describe(now_monotonic=0.0)["last_command_state"] is None
    assert dev.write_output_limit(500) is True
    # A successful publish is transport-level: reported "published", not confirmed.
    assert dev.describe(now_monotonic=0.0)["last_command_state"] == "published"


def test_a_scalar_telemetry_family_does_not_block_a_writable_profile():
    """Commands go to iot/<pk>/<dev>/…; the telemetry family is unrelated.

    The broker source is: this device is on the Zendure cloud broker, which
    carries that route on every family. Its local-broker counterpart is covered
    by ``test_zendure_mqtt_broker_source_enforcement.py``.
    """

    from ems.zendure_mqtt.topics import FAMILY_ZENSDK_HA_SCALAR

    dev = ZendureMqttDeviceClient(
        "WR",
        FakeService(),
        device_id="DEV",
        topic_family=FAMILY_ZENSDK_HA_SCALAR,
        source="zendure_cloud_mqtt",
        product_key="PK",
        hardware_profile="hyper_2000",
    )
    d = dev.describe(now_monotonic=0.0)
    assert d["control_supported"] is True
    assert d["control_block_reason"] is None


def test_a_telemetry_only_profile_reports_its_block_reason():
    from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON

    dev = ZendureMqttDeviceClient(
        "WR",
        FakeService(),
        device_id="DEV",
        topic_family=FAMILY_LEGACY_JSON,
        source="local_mqtt",
        product_key="PK",
        hardware_profile="ace_1500",
    )
    d = dev.describe(now_monotonic=0.0)
    assert d["control_supported"] is False
    assert d["control_block_reason"] == "hardware_profile_deferred"
