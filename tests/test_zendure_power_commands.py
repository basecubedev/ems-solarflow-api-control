# SPDX-License-Identifier: AGPL-3.0-or-later
"""Command-contract tests for the model-aware Zendure power write adapter.

Exact ``function/invoke`` payloads per hardware profile, asserted against
sanitized fixtures. Unsupported operations (Hub/AIO AC charging) and unknown or
deferred profiles must be rejected without producing a command — never a silent
fallback.
"""

import json
from pathlib import Path

import pytest

from ems.mqtt_control.zendure_commands import (
    PowerCommandError,
    ZendurePowerCommand,
    build_power_command,
    next_power_message_id,
)

pytestmark = pytest.mark.simulation

_FIXTURES = Path(__file__).parent / "fixtures" / "zendure_mqtt"
_PRODUCT_KEY = "PRODUCT_KEY"
_DEVICE_ID = "DEVICE_ID"
_INVOKE_TOPIC = "iot/PRODUCT_KEY/DEVICE_ID/function/invoke"


def _fixture(name):
    return json.loads((_FIXTURES / name).read_text())


def _build(profile, target_w, *, message_id=1, timestamp=1234567890,
           product_key=_PRODUCT_KEY, device_id=_DEVICE_ID):
    return build_power_command(
        hardware_profile=profile,
        target_w=target_w,
        product_key=product_key,
        device_id=device_id,
        message_id=message_id,
        timestamp=timestamp,
    )


# --- Hub scalar automation --------------------------------------------------


def test_hub1200_builds_scalar_discharge_command():
    cmd = _build("hub_1200", 500)
    assert isinstance(cmd, ZendurePowerCommand)
    assert cmd.topic == _INVOKE_TOPIC
    assert cmd.operation == "discharge"
    assert cmd.target_w == 500
    assert cmd.payload == _fixture("hub1200_discharge_command.json")


def test_hub2000_builds_scalar_discharge_command():
    cmd = _build("hub_2000", 500)
    assert cmd.payload == _fixture("hub2000_discharge_command.json")


def test_hub_zero_target_builds_scalar_idle_command():
    cmd = _build("hub_1200", 0)
    assert cmd.operation == "idle"
    assert cmd.payload == _fixture("hub1200_idle_command.json")


def test_hub_negative_target_is_rejected():
    with pytest.raises(PowerCommandError):
        _build("hub_1200", -500)
    with pytest.raises(PowerCommandError):
        _build("hub_2000", -500)


# --- Hyper / AIO object automation ------------------------------------------


def test_hyper_builds_object_discharge_command():
    cmd = _build("hyper_2000", 500)
    assert cmd.topic == _INVOKE_TOPIC
    assert cmd.operation == "discharge"
    assert cmd.payload == _fixture("hyper2000_discharge_command.json")


def test_hyper_builds_object_charge_command():
    # An EMS target of -500 W becomes a positive 500 W charging value.
    cmd = _build("hyper_2000", -500)
    assert cmd.operation == "charge"
    assert cmd.target_w == -500
    assert cmd.payload == _fixture("hyper2000_charge_command.json")


def test_hyper_builds_object_idle_command():
    cmd = _build("hyper_2000", 0)
    assert cmd.operation == "idle"
    assert cmd.payload == _fixture("hyper2000_idle_command.json")


def test_aio_builds_object_discharge_command():
    cmd = _build("aio_2400", 500)
    assert cmd.payload == _fixture("aio2400_discharge_command.json")


def test_aio_builds_object_idle_command():
    cmd = _build("aio_2400", 0)
    assert cmd.payload == _fixture("aio2400_idle_command.json")


def test_aio_negative_target_is_rejected():
    with pytest.raises(PowerCommandError):
        _build("aio_2400", -500)


# --- common invoke envelope -------------------------------------------------


def test_invoke_command_contains_message_id():
    cmd = _build("hyper_2000", 500, message_id=42)
    assert cmd.payload["messageId"] == 42


def test_invoke_command_contains_device_key():
    cmd = _build("hub_2000", 500)
    assert cmd.payload["deviceKey"] == _DEVICE_ID


def test_invoke_command_contains_device_id():
    cmd = _build("hub_2000", 500)
    assert cmd.payload["deviceId"] == _DEVICE_ID


def test_invoke_command_contains_timestamp():
    cmd = _build("hyper_2000", 500, timestamp=1700000000)
    assert cmd.payload["timestamp"] == 1700000000


def test_message_ids_are_monotonic():
    first = next_power_message_id()
    second = next_power_message_id()
    third = next_power_message_id()
    assert first < second < third


# --- fail-closed rejections -------------------------------------------------


def test_unknown_profile_cannot_build_power_command():
    with pytest.raises(PowerCommandError):
        _build("no_such_profile", 500)
    with pytest.raises(PowerCommandError):
        _build(None, 500)


def test_ace_profile_cannot_build_power_command():
    with pytest.raises(PowerCommandError):
        _build("ace_1500", 500)


def test_telemetry_only_and_zensdk_profiles_are_not_invoke_commands():
    # ZenSDK keeps its own properties/write path; the invoke builder never emits
    # a command for it, and never for a deferred SuperBase profile.
    with pytest.raises(PowerCommandError):
        _build("solarflow_zensdk", 500)
    with pytest.raises(PowerCommandError):
        _build("superbase_v4600", 500)


def test_missing_identifiers_are_rejected():
    with pytest.raises(PowerCommandError):
        _build("hyper_2000", 500, product_key=None)
    with pytest.raises(PowerCommandError):
        _build("hyper_2000", 500, device_id=None)


def test_invalid_message_id_or_timestamp_is_rejected():
    with pytest.raises(PowerCommandError):
        _build("hyper_2000", 500, message_id=0)
    with pytest.raises(PowerCommandError):
        _build("hyper_2000", 500, timestamp=None)
