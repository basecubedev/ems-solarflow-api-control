# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared pytest fixtures for the EMS test suite."""

import pytest

from ems import paths

# Focused Zendure MQTT development-release gate: the fast, hardware-free subset
# that must pass on every feature/development build without running the full
# non-Docker regression. Grouped by the release-contract categories. The real
# Mosquitto lifecycle tests are a separate Docker tier (marked ``docker``) and
# are intentionally NOT listed here so this gate stays fast and deterministic.
# Run it with: pytest -m mqtt_release
MQTT_RELEASE_MODULES = frozenset(
    {
        # MQTT command lifecycle
        "test_zendure_mqtt_command_lifecycle",
        "test_zendure_mqtt_command_coordinator",
        "test_zendure_mqtt_command_state",
        "test_zendure_mqtt_confirmation",
        "test_zendure_mqtt_no_ack_confirmation",
        "test_zendure_mqtt_safety_preemption",
        "test_zendure_mqtt_reply_contract",
        "test_controller_write_dispatch",
        "test_zendure_mqtt_dispatch_audit",
        # MQTT profile and capability selection
        "test_zendure_hardware_profiles",
        "test_zendure_hardware_resolution",
        "test_zendure_hardware_evidence",
        "test_zendure_power_capability",
        "test_zendure_mqtt_power_write_profile",
        "test_zendure_mqtt_power_adapter",
        "test_zendure_mqtt_target_validation",
        "test_zendure_mqtt_evidence_provenance",
        # Admin MQTT config round trips
        "test_ems_zendure_mqtt_config_mapping",
        "test_admin_maintenance_explicit_identifier_clear",
        "test_admin_mqtt_control_use_case",
        "test_admin_mqtt_setup_maintenance_parity",
        # Admin migration review and apply
        "test_zendure_mqtt_migration",
        "test_zendure_mqtt_migration_integration",
        "test_zendure_mqtt_migration_validation",
        "test_admin_zendure_mqtt_migration_apply",
        "test_admin_zendure_mqtt_migration_endpoints",
        # Admin packaged-image imports
        "test_admin_docker_image_contract",
    }
)


def pytest_collection_modifyitems(config, items):
    """Auto-apply the ``mqtt_release`` marker to the focused release modules.

    Centralized here so the focused gate is defined in one place instead of
    editing a ``pytestmark`` in every module; a module rename surfaces as a
    missing member in :data:`MQTT_RELEASE_MODULES` via the guard test.
    """

    marker = pytest.mark.mqtt_release
    for item in items:
        module = getattr(item, "module", None)
        name = getattr(module, "__name__", "").rsplit(".", 1)[-1]
        if name in MQTT_RELEASE_MODULES:
            item.add_marker(marker)


@pytest.fixture
def isolated_install_root(tmp_path_factory, monkeypatch):
    """Point EMS path resolution at an empty temporary install root.

    Admin config preview/export/server code resolves the active EMS config
    through ``ems.paths`` (``paths.BASE_DIR`` when no explicit install root is
    given). In a real developer checkout that root holds a gitignored
    ``config/config.json`` and ``data/`` left behind by running EMS locally, so
    the default resolution would read the developer's real runtime files and let
    the outcome depend on the working tree.

    Isolating ``BASE_DIR`` to an empty directory (and clearing any ambient EMS
    path env overrides) keeps those tests deterministic without requiring a
    clean checkout or ``git clean -fdX``. Tests that intentionally validate path
    resolution override ``BASE_DIR``/``EMS_INSTALL_DIR`` themselves, which layers
    cleanly on top of this baseline.
    """

    root = tmp_path_factory.mktemp("isolated_install_root")
    for var in ("EMS_INSTALL_DIR", "EMS_CONFIG_FILE", "EMS_TEMPLATE_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(paths, "BASE_DIR", str(root))
    monkeypatch.setenv("EMS_ADMIN_DATA_DIR", str(root / "admin-data"))
    return root
