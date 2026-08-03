# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared helpers and assertions for the Docker end-to-end suites."""
import os
from pathlib import Path

# Compose reads its project/profile/file selection from the environment. A
# developer or CI shell exporting any of these would silently change which
# services a generated test project starts, so they never reach a test project.
AMBIENT_COMPOSE_VARS = (
    "COMPOSE_PROFILES",
    "COMPOSE_FILE",
    "COMPOSE_ENV_FILES",
    "COMPOSE_PATH_SEPARATOR",
    "COMPOSE_PROJECT_NAME",
    "COMPOSE_IGNORE_ORPHANS",
    "COMPOSE_REMOVE_ORPHANS",
)


def compose_env(**overrides):
    """Return an ``os.environ`` copy without ambient Compose selection."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in AMBIENT_COMPOSE_VARS
    }
    env.update(overrides)
    return env


def assert_no_root_owned_files(*bases, exclude=()):
    """Fail if any file under ``bases`` is owned by root (uid 0 or gid 0).

    ``exclude`` lists directories whose subtrees are skipped. The bundled
    InfluxDB data directory is the typical exclusion: it belongs to the
    separate ``influxdb`` container (which writes as its own uid with gid 0),
    not to any EMS command, so it is outside the EMS privilege-drop contract.
    """
    skip = [Path(path).resolve() for path in exclude]
    for base in bases:
        base = Path(base)
        for path in base.rglob("*"):
            resolved = path.resolve()
            if any(resolved == ex or ex in resolved.parents for ex in skip):
                continue
            info = path.stat()
            assert info.st_uid != 0, path
            assert info.st_gid != 0, path
