# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 0: the focused Zendure MQTT development-release gate is well defined.

The ``mqtt_release`` marker selects a fast, hardware-free subset that runs on
every feature/development build (``pytest -m mqtt_release``) instead of the whole
non-Docker regression. This guard keeps that selection honest: every listed
module must exist and collect, and the gate must actually cover the
release-contract categories from the task.
"""

from pathlib import Path

import pytest

from tests.conftest import MQTT_RELEASE_MODULES

pytestmark = pytest.mark.simulation

TESTS_DIR = Path(__file__).resolve().parent


def test_every_release_module_exists():
    for module in MQTT_RELEASE_MODULES:
        assert (TESTS_DIR / f"{module}.py").is_file(), module


def test_release_gate_covers_each_contract_category():
    # One representative module per Phase 0 category must be in the gate.
    required = {
        "command lifecycle": "test_zendure_mqtt_command_lifecycle",
        "no-ack confirmation": "test_zendure_mqtt_no_ack_confirmation",
        "safety preemption": "test_zendure_mqtt_safety_preemption",
        "capability selection": "test_zendure_power_capability",
        "config round trips": "test_ems_zendure_mqtt_config_mapping",
        "migration review/apply": "test_zendure_mqtt_migration",
        "packaged-image imports": "test_admin_docker_image_contract",
    }
    for category, module in required.items():
        assert module in MQTT_RELEASE_MODULES, category


def test_release_gate_is_non_trivial():
    # A safety net against an accidentally emptied gate.
    assert len(MQTT_RELEASE_MODULES) >= 20
