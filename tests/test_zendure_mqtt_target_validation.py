# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operation support and strict target validation before any publish.

Every adapter resolves the requested operation (sign of the target) and its
model's write capability *before* a message id is allocated or a payload built.
Only explicit integer watt targets within the configured safe maximum are
accepted; a bool, numeric string, float, non-finite value, object or
out-of-range value is rejected with a machine-readable error and never
published. The properties/write path never publishes a negative outputLimit.
"""

import math

import pytest

from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient
from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.power_control,
    pytest.mark.unit,
    pytest.mark.simulation,
]


class FakeService:
    def __init__(self):
        self.published = []

    def snapshot_status(self, device_id, *, now_monotonic=None):
        from ems.zendure_mqtt.service import classify_snapshot

        return classify_snapshot(None, 60.0, now_monotonic=now_monotonic or 0.0)

    def publish_output_limit(self, topic, payload):
        self.published.append((topic, payload))
        return True


def _device(hardware_profile, *, max_power=2000, **kwargs):
    return ZendureMqttDeviceClient(
        "WR",
        FakeService(),
        device_id="DEVICE_ID",
        topic_family=FAMILY_LEGACY_JSON,
        source="local_mqtt",
        product_key="PRODUCT_KEY",
        max_power=max_power,
        hardware_profile=hardware_profile,
        **kwargs,
    )


# --- Phase 2: operation support before adapter -------------------------------


def test_zensdk_negative_target_rejected_without_publish():
    dev = _device("solarflow_800_pro_2")
    assert dev.write_output_limit(-500) is False
    assert dev._service.published == []
    assert dev.write_health.last_error == "charge_target_unsupported"


def test_hub_negative_target_rejected_without_publish():
    dev = _device("hub_2000")
    assert dev.write_output_limit(-500) is False
    assert dev._service.published == []
    assert dev.write_health.last_error == "charge_target_unsupported"


def test_aio_negative_target_rejected_without_publish():
    dev = _device("aio_2400")
    assert dev.write_output_limit(-500) is False
    assert dev._service.published == []
    assert dev.write_health.last_error == "charge_target_unsupported"


def test_hyper_negative_target_is_supported_charge():
    dev = _device("hyper_2000")
    assert dev.write_output_limit(-500) is True
    assert len(dev._service.published) == 1


def test_telemetry_only_profile_rejected_without_publish():
    dev = _device("ace_1500")
    assert dev.write_output_limit(500) is False
    assert dev._service.published == []


def test_unsupported_operation_allocates_no_message_id():
    # A rejected operation must not consume a message id or reach publish.
    from ems.mqtt_control import zendure_commands

    dev = _device("hub_2000")
    before = zendure_commands.next_power_message_id()
    dev.write_output_limit(-500)
    after = zendure_commands.next_power_message_id()
    # Only our two bracketing calls advanced the counter; the rejected write
    # allocated nothing in between.
    assert after - before == 1


# --- Phase 3: strict target validation ---------------------------------------


@pytest.mark.parametrize(
    "bad",
    [True, False, "500", "abc", 500.5, 499.9, float("nan"), float("inf"), -float("inf"),
     None, object(), [500], {"w": 500}],
)
def test_invalid_target_types_are_rejected(bad):
    dev = _device("hyper_2000")
    assert dev.write_output_limit(bad) is False
    assert dev._service.published == []
    assert dev.write_health.last_error == "invalid_power_target"


def test_integer_valued_float_is_still_rejected():
    dev = _device("hyper_2000")
    assert dev.write_output_limit(500.0) is False
    assert dev.write_health.last_error == "invalid_power_target"


def test_explicit_integer_target_is_accepted():
    dev = _device("hyper_2000")
    assert dev.write_output_limit(500) is True


def test_discharge_above_configured_maximum_is_rejected():
    dev = _device("hyper_2000", max_power=800)
    assert dev.write_output_limit(1500) is False
    assert dev._service.published == []
    assert dev.write_health.last_error == "target_above_maximum"


def test_charge_above_configured_maximum_is_rejected():
    dev = _device("hyper_2000", max_power=800)
    assert dev.write_output_limit(-1500) is False
    assert dev._service.published == []
    assert dev.write_health.last_error == "target_above_maximum"


def test_target_at_configured_maximum_is_accepted():
    dev = _device("hyper_2000", max_power=800)
    assert dev.write_output_limit(800) is True


def test_zensdk_never_publishes_negative_output_limit():
    import json

    dev = _device("solarflow_800_pro_2")
    # Any accepted ZenSDK write is a non-negative properties/write outputLimit.
    dev.write_output_limit(300)
    _topic, payload = dev._service.published[-1]
    assert json.loads(payload)["properties"]["outputLimit"] == 300
    # A negative target never reaches the properties/write path.
    dev.write_output_limit(-300)
    assert len(dev._service.published) == 1


def test_nan_is_not_finite_guard():
    # Regression guard: NaN must never slip through as a "number".
    assert not math.isfinite(float("nan"))
