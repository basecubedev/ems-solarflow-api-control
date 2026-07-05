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
