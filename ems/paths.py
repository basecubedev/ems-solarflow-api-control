# SPDX-License-Identifier: AGPL-3.0-or-later
"""Central path resolution shared by EMS core and its management tools.

This module is intentionally tiny and import-side-effect-free so that both
``emsctl.py`` and ``ems.diagnostics`` can depend on it without creating an
import cycle (``ems.diagnostics`` must never import ``emsctl``).
"""

import os
from pathlib import Path

# Project root = the directory that contains this ``ems/`` package.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKER_TEMPLATE_PATH = Path("/app/config.template.json")

# Config layout states describe how the standard (``config/config.json``) and
# legacy (``config.json``) locations coexist in an install root.
LAYOUT_NONE = "none"
LAYOUT_LEGACY_ROOT_ONLY = "legacy_root_only"
LAYOUT_STANDARD_ONLY = "standard_only"
LAYOUT_BOTH_SAME = "both_same"
LAYOUT_BOTH_DIFFERENT = "both_different"


def _base_path(base_dir=None):
    return Path(BASE_DIR if base_dir is None else base_dir)


def resolve_config_path(explicit_path=None, *, base_dir=None):
    """Resolve the active EMS config or its canonical creation target."""

    if explicit_path:
        return Path(explicit_path)

    env_path = os.environ.get("EMS_CONFIG_FILE")
    if env_path:
        return Path(env_path)

    base = _base_path(base_dir)
    canonical = base / "config" / "config.json"
    legacy = base / "config.json"
    if canonical.exists():
        return canonical
    if legacy.exists():
        return legacy
    return canonical


def standard_config_path(base_dir=None):
    """The canonical Docker/Admin config location, regardless of existence."""

    return _base_path(base_dir) / "config" / "config.json"


def legacy_config_path(base_dir=None):
    """The legacy native root config location, regardless of existence."""

    return _base_path(base_dir) / "config.json"


def detect_config_layout_state(base_dir=None):
    """Classify how the standard and legacy config locations coexist.

    Returns one of ``LAYOUT_NONE``, ``LAYOUT_LEGACY_ROOT_ONLY``,
    ``LAYOUT_STANDARD_ONLY``, ``LAYOUT_BOTH_SAME`` or ``LAYOUT_BOTH_DIFFERENT``.
    ``both_*`` compares file contents so callers can tell an in-progress
    migration (identical) from two diverged configs.
    """

    standard = standard_config_path(base_dir)
    legacy = legacy_config_path(base_dir)
    standard_exists = standard.exists()
    legacy_exists = legacy.exists()

    if not standard_exists and not legacy_exists:
        return LAYOUT_NONE
    if standard_exists and not legacy_exists:
        return LAYOUT_STANDARD_ONLY
    if legacy_exists and not standard_exists:
        return LAYOUT_LEGACY_ROOT_ONLY

    try:
        same = standard.read_bytes() == legacy.read_bytes()
    except OSError:
        same = False
    return LAYOUT_BOTH_SAME if same else LAYOUT_BOTH_DIFFERENT


def resolve_template_path(explicit_path=None, *, base_dir=None):
    """Resolve the EMS template, including the Docker image fallback."""

    if explicit_path:
        return Path(explicit_path)

    env_path = os.environ.get("EMS_TEMPLATE_FILE")
    if env_path:
        return Path(env_path)

    base = _base_path(base_dir)
    canonical = base / "config" / "config.template.json"
    legacy = base / "config.template.json"
    if canonical.exists():
        return canonical
    if legacy.exists():
        return legacy
    if DOCKER_TEMPLATE_PATH.exists():
        return DOCKER_TEMPLATE_PATH
    return canonical


def resolve_data_dir(explicit_path=None, *, base_dir=None):
    """Resolve the EMS mutable-data directory."""

    return Path(explicit_path) if explicit_path else _base_path(base_dir) / "data"


ZENDURE_MQTT_STATUS_FILENAME = "zendure-mqtt-status.json"


def resolve_zendure_mqtt_status_path(explicit_path=None, *, base_dir=None):
    """Resolve the live Zendure MQTT telemetry status file.

    The running EMS persists a credential-free status snapshot here so out-of-
    process readers (Admin) can prefer live runtime state over a config-derived
    fallback without talking to the broker themselves.
    """

    if explicit_path:
        return Path(explicit_path)
    return resolve_data_dir(base_dir=base_dir) / ZENDURE_MQTT_STATUS_FILENAME


def resolve_compose_path(explicit_path=None, *, base_dir=None):
    """Resolve the EMS Compose file."""

    return (
        Path(explicit_path)
        if explicit_path
        else _base_path(base_dir) / "docker-compose.yml"
    )


def resolve_project_path(path):
    if not path:
        return path
    if os.path.isabs(path):
        return path
    return os.path.join(BASE_DIR, path)


def resolve_runtime_path(args, config):
    if args.runtime_state:
        path = args.runtime_state
    else:
        path = (
            config.get("system", {})
            .get("runtime_state_path", "runtime-state.json")
        )

    if not os.path.isabs(path):
        path = os.path.join(BASE_DIR, path)

    return path


def resolve_dashboard_auth_path(args, config):
    # Lazy import keeps this module dependency-light; dashboard.auth only pulls
    # in stdlib, so the cost is negligible when it is needed.
    from dashboard import auth as dashboard_auth

    path = args.dashboard_auth or (
        config.get("dashboard", {})
        .get("auth_file", dashboard_auth.DEFAULT_AUTH_FILE)
    )

    if not os.path.isabs(path):
        path = os.path.join(BASE_DIR, path)

    return path
