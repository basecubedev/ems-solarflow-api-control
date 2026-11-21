# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The API paths the EMS dashboard server answers.

The Admin console serves the one browser app and forwards these paths to the
EMS; every other ``/api/`` path is its own. ``tests/test_ems_forwarding.py``
walks the routes ``dashboard/server.py`` dispatches and holds this list to
them, in both directions. Import-side-effect-free, so the Admin image can carry
it without the rest of the dashboard.
"""

EMS_API_PATHS = frozenset(
    {
        "/api/analytics/series",
        "/api/analytics/status",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/refresh",
        "/api/auth/status",
        "/api/diagnose",
        "/api/diagnose/support-bundle",
        "/api/energy-stats",
        "/api/events",
        "/api/history",
        "/api/history/series",
        "/api/live",
        "/api/logs",
        "/api/logs/level",
        "/api/maintenance/backups",
        "/api/maintenance/backups/create",
        "/api/maintenance/backups/inspect",
        "/api/maintenance/backups/restore",
        "/api/maintenance/backups/restore-plan",
        "/api/maintenance/config-upgrade",
        "/api/maintenance/config-upgrade/apply",
        "/api/maintenance/status",
        "/api/runtime",
        "/api/schedule",
        "/api/ui-config",
    }
)

EMS_API_PREFIXES = ("/api/runtime/", "/api/schedule/")


def is_ems_api_path(path):
    """Whether the EMS answers ``path`` (no query), so the Admin forwards it."""

    return path in EMS_API_PATHS or path.startswith(EMS_API_PREFIXES)
