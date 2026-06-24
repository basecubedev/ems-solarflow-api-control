# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared assertions for the Docker end-to-end suites."""
from pathlib import Path


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
