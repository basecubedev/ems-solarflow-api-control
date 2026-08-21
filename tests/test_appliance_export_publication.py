# SPDX-License-Identifier: AGPL-3.0-or-later
"""What proves that an export target publishes the EMS directory it names.

The appliance answers this from ``/proc/self/mountinfo``. That file's root
field is the path *inside* the source filesystem, not the absolute path on the
host: binding ``/persistent/ems/config`` publishes a record whose root reads
``/ems/config``. Comparing it against the absolute source path therefore holds
only while the source shares a filesystem with ``/`` — true on a developer
machine, false on the A/B appliance, where every source lives on the persistent
partition and every export would read as foreign.
"""

import os

import pytest

from appliance.export_state import device_id, path_within_filesystem, publishes_source

pytestmark = [pytest.mark.unit, pytest.mark.simulation]


def record_for(path, *, root, options=("ro",)):
    return {
        "root": root,
        "device": device_id(os.stat(str(path))),
        "options": frozenset(options),
    }


def test_a_path_is_named_relative_to_the_filesystem_carrying_it(tmp_path):
    """``/`` is a mount point on every host, so a path under it keeps its text."""

    assert path_within_filesystem("/") == "/"

    source = tmp_path / "ems" / "config"
    source.mkdir(parents=True)
    relative = path_within_filesystem(source)

    assert relative.startswith("/")
    assert relative.endswith("/ems/config")


def test_a_mount_rooted_at_the_source_is_accepted(tmp_path):
    source = tmp_path / "ems" / "config"
    source.mkdir(parents=True)

    record = record_for(source, root=path_within_filesystem(source))

    assert publishes_source(source, record)


def test_a_mount_rooted_elsewhere_on_the_same_filesystem_is_refused(tmp_path):
    """Same device is not enough: a sibling directory is a different export."""

    source = tmp_path / "ems" / "config"
    source.mkdir(parents=True)
    other = tmp_path / "ems" / "data"
    other.mkdir(parents=True)

    record = record_for(source, root=path_within_filesystem(other))

    assert not publishes_source(source, record)


def test_a_record_naming_another_device_is_refused(tmp_path):
    source = tmp_path / "ems" / "config"
    source.mkdir(parents=True)

    record = dict(record_for(source, root=path_within_filesystem(source)), device="0:0")

    assert not publishes_source(source, record)


def test_a_missing_source_is_refused(tmp_path):
    record = {"root": "/ems/config", "device": "0:0", "options": frozenset({"ro"})}

    assert not publishes_source(tmp_path / "absent", record)
