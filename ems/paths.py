# SPDX-License-Identifier: AGPL-3.0-or-later
"""Project path resolution shared by the CLI and the diagnose service layer.

This module is intentionally tiny and import-side-effect-free so that both
``emsctl.py`` and ``ems.diagnostics`` can depend on it without creating an
import cycle (``ems.diagnostics`` must never import ``emsctl``).
"""

import os

# Project root = the directory that contains this ``ems/`` package.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
