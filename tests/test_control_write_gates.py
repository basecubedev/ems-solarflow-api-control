# SPDX-License-Identifier: AGPL-3.0-or-later
"""The three named control-write gates: API, MQTT-local, MQTT-Zendure."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import ems.config as cfg

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
]


def _enabled_preconditions():
    return patch.multiple(
        cfg,
        DRY_RUN=False,
        SIMULATION_MODE=False,
        ARGS=SimpleNamespace(replay=False),
    )


def test_each_gate_is_independent():
    with _enabled_preconditions(), patch.multiple(
        cfg,
        ALLOW_HARDWARE_WRITES=True,
        ALLOW_MQTT_LOCAL_CONTROL_WRITES=False,
        ALLOW_MQTT_ZENDURE_CONTROL_WRITES=False,
    ):
        assert cfg.hardware_writes_allowed() is True
        assert cfg.mqtt_local_control_writes_allowed() is False
        assert cfg.mqtt_zendure_control_writes_allowed() is False

    with _enabled_preconditions(), patch.multiple(
        cfg,
        ALLOW_HARDWARE_WRITES=False,
        ALLOW_MQTT_LOCAL_CONTROL_WRITES=True,
        ALLOW_MQTT_ZENDURE_CONTROL_WRITES=False,
    ):
        assert cfg.hardware_writes_allowed() is False
        assert cfg.mqtt_local_control_writes_allowed() is True
        assert cfg.mqtt_zendure_control_writes_allowed() is False


def test_simulation_blocks_every_gate():
    with patch.multiple(
        cfg,
        DRY_RUN=False,
        SIMULATION_MODE=True,
        ARGS=SimpleNamespace(replay=False),
        ALLOW_HARDWARE_WRITES=True,
        ALLOW_MQTT_LOCAL_CONTROL_WRITES=True,
        ALLOW_MQTT_ZENDURE_CONTROL_WRITES=True,
    ):
        assert cfg.hardware_writes_allowed() is False
        assert cfg.mqtt_local_control_writes_allowed() is False
        assert cfg.mqtt_zendure_control_writes_allowed() is False


def test_control_writes_allowed_dispatches_by_gate_name():
    with _enabled_preconditions(), patch.multiple(
        cfg,
        ALLOW_HARDWARE_WRITES=False,
        ALLOW_MQTT_LOCAL_CONTROL_WRITES=True,
        ALLOW_MQTT_ZENDURE_CONTROL_WRITES=False,
    ):
        assert cfg.control_writes_allowed("api") is False
        assert cfg.control_writes_allowed("mqtt_local") is True
        assert cfg.control_writes_allowed("mqtt_zendure") is False
        # Unknown gate falls back to the API gate.
        assert cfg.control_writes_allowed("something_else") is False


# --- effective write-gate decision (transport-aware diagnostics) ------------


def test_decision_reports_effective_gate_per_transport():
    with _enabled_preconditions(), patch.multiple(
        cfg,
        ALLOW_HARDWARE_WRITES=True,
        ALLOW_MQTT_LOCAL_CONTROL_WRITES=False,
        ALLOW_MQTT_ZENDURE_CONTROL_WRITES=False,
    ):
        http = cfg.resolve_write_gate("api")
        assert http.allowed is True
        assert http.transport == "http"
        assert http.gate_name == "allow_hardware_writes"
        assert http.gate_enabled is True

        local = cfg.resolve_write_gate("mqtt_local")
        assert local.allowed is False
        assert local.gate_name == "allow_mqtt_local_control_writes"
        assert local.blocked_by == ("allow_mqtt_local_control_writes",)

        cloud = cfg.resolve_write_gate("mqtt_zendure")
        assert cloud.gate_name == "allow_mqtt_zendure_control_writes"


def test_no_transport_is_classified_as_experimental():
    # MQTT (local and Zendure cloud) is a normal control transport: the write-gate
    # decision must not carry a generic experimental flag derived from transport.
    with _enabled_preconditions(), patch.multiple(
        cfg,
        ALLOW_HARDWARE_WRITES=True,
        ALLOW_MQTT_LOCAL_CONTROL_WRITES=True,
        ALLOW_MQTT_ZENDURE_CONTROL_WRITES=True,
    ):
        for gate in ("api", "mqtt_local", "mqtt_zendure"):
            decision = cfg.resolve_write_gate(gate)
            assert not hasattr(decision, "experimental")
            assert "experimental" not in decision.as_log_fields()


def test_decision_surfaces_global_blockers():
    with patch.multiple(
        cfg,
        DRY_RUN=True,
        SIMULATION_MODE=True,
        ARGS=SimpleNamespace(replay=True),
        ALLOW_HARDWARE_WRITES=True,
        ALLOW_MQTT_LOCAL_CONTROL_WRITES=True,
        ALLOW_MQTT_ZENDURE_CONTROL_WRITES=True,
    ):
        decision = cfg.resolve_write_gate("api")
        assert decision.allowed is False
        assert "dry_run" in decision.blocked_by
        assert "simulation_mode" in decision.blocked_by
        assert "replay_mode" in decision.blocked_by


def test_resolve_device_write_gate_reads_control_gate():
    device = SimpleNamespace(control_gate="mqtt_local")
    with _enabled_preconditions(), patch.multiple(
        cfg,
        ALLOW_HARDWARE_WRITES=True,
        ALLOW_MQTT_LOCAL_CONTROL_WRITES=False,
        ALLOW_MQTT_ZENDURE_CONTROL_WRITES=False,
    ):
        decision = cfg.resolve_device_write_gate(device)
        assert decision.transport == "mqtt_local"
        assert decision.allowed is False


def test_control_writes_allowed_matches_decision():
    with _enabled_preconditions(), patch.multiple(
        cfg,
        ALLOW_HARDWARE_WRITES=True,
        ALLOW_MQTT_LOCAL_CONTROL_WRITES=True,
        ALLOW_MQTT_ZENDURE_CONTROL_WRITES=False,
    ):
        for gate in ("api", "mqtt_local", "mqtt_zendure"):
            assert cfg.control_writes_allowed(gate) == cfg.resolve_write_gate(gate).allowed
