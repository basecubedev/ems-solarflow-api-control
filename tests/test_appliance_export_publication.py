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

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]


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


# --- the A/B shape ------------------------------------------------------------


def test_a_source_under_a_bind_is_named_through_that_binds_own_root(tmp_path):
    """The regression, in the shape the appliance actually has.

    On the A/B image ``/opt/ems-solarflow`` is itself a bind of
    ``/persistent/shared/opt/ems-solarflow``, so the kernel records the export's
    root as ``/shared/opt/ems-solarflow/config``. Walking only to the nearest
    mount point stops at ``/opt/ems-solarflow`` and answers ``/config``, so every
    export reads as foreign and backup access disables itself on every boot.

    The expected text is written out here rather than derived from the function
    under test, which would agree with itself either way.
    """

    source = tmp_path / "opt" / "ems-solarflow" / "config"
    source.mkdir(parents=True)
    enclosing = tmp_path / "opt" / "ems-solarflow"
    mounts = {
        str(enclosing): {
            "root": "/shared/opt/ems-solarflow",
            "device": device_id(os.stat(str(enclosing))),
            "options": frozenset({"rw"}),
        }
    }

    assert (
        path_within_filesystem(source, mounts=mounts, is_mount=lambda p: p == str(enclosing))
        == "/shared/opt/ems-solarflow/config"
    )


def test_an_export_of_a_source_under_a_bind_is_accepted(tmp_path):
    source = tmp_path / "opt" / "ems-solarflow" / "config"
    source.mkdir(parents=True)
    enclosing = tmp_path / "opt" / "ems-solarflow"
    enclosing_record = {
        "root": "/shared/opt/ems-solarflow",
        "device": device_id(os.stat(str(enclosing))),
        "options": frozenset({"rw"}),
    }
    mounts = {str(enclosing): enclosing_record}
    record = record_for(source, root="/shared/opt/ems-solarflow/config")

    assert publishes_source(
        source, record, mounts=mounts, is_mount=lambda p: p == str(enclosing)
    )


def test_a_sibling_under_the_same_bind_is_still_refused(tmp_path):
    """The bind root is carried along, not used to wave the comparison through."""

    source = tmp_path / "opt" / "ems-solarflow" / "config"
    source.mkdir(parents=True)
    enclosing = tmp_path / "opt" / "ems-solarflow"
    mounts = {
        str(enclosing): {
            "root": "/shared/opt/ems-solarflow",
            "device": device_id(os.stat(str(enclosing))),
            "options": frozenset({"rw"}),
        }
    }
    record = record_for(source, root="/shared/opt/ems-solarflow/data")

    assert not publishes_source(
        source, record, mounts=mounts, is_mount=lambda p: p == str(enclosing)
    )
