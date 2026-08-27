# SPDX-License-Identifier: AGPL-3.0-or-later
"""Something to go back to, which the appliance never actually kept.

``ems-appliance rollback-manager`` reads ``previous.deb`` and reinstalls it. It
has been in the CLI and in `docs/appliance/installation.md` for as long as both
have existed, and nothing in this repository ever wrote that file — so the one
documented way back from a bad manager could only ever answer that there was
nothing to go back to.

dpkg keeps no copy of the archive it installed from, and a package cannot be
rebuilt out of an unpacked filesystem. Retention has to happen while the archive
is still in hand, or it cannot happen at all.
"""

import json
from pathlib import Path

import pytest

from appliance import manager_retention as retention

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]


class FakePaths:
    def __init__(self, root):
        self.packages_dir = Path(root) / "packages"


@pytest.fixture
def paths(tmp_path):
    return FakePaths(tmp_path)


def archive(tmp_path, name, body):
    target = tmp_path / name
    target.write_bytes(body)
    return target


def test_a_fresh_appliance_has_nothing_to_go_back_to(paths):
    """Its manager arrived in the image, not through an install."""

    kept = retention.read(paths)

    assert not kept.can_revert
    with pytest.raises(retention.RetentionError) as refusal:
        retention.revert_target(paths)
    assert refusal.value.code == "no_previous_package"


def test_the_first_install_keeps_itself_and_still_offers_no_revert(paths, tmp_path):
    retention.retain(
        paths, archive(tmp_path, "a.deb", b"one"), sha256="aaa", version="0.1.0"
    )

    kept = retention.read(paths)

    assert kept.current.present
    assert kept.current.version == "0.1.0"
    assert not kept.can_revert, "there is no earlier package, and saying otherwise would lie"


def test_the_second_install_is_what_creates_a_way_back(paths, tmp_path):
    retention.retain(paths, archive(tmp_path, "a.deb", b"one"), sha256="aaa", version="0.1.0")
    retention.retain(paths, archive(tmp_path, "b.deb", b"two"), sha256="bbb", version="0.2.0")

    kept = retention.read(paths)

    assert kept.can_revert
    assert kept.current.version == "0.2.0"
    assert kept.previous.version == "0.1.0"
    assert Path(kept.previous.path).read_bytes() == b"one"


def test_the_archive_moves_and_not_only_the_record(paths, tmp_path):
    """A record naming a file that is not there is the shape being fixed."""

    retention.retain(paths, archive(tmp_path, "a.deb", b"one"), sha256="aaa", version="0.1.0")
    retention.retain(paths, archive(tmp_path, "b.deb", b"two"), sha256="bbb", version="0.2.0")

    target = retention.revert_target(paths)

    assert Path(target.path).is_file()
    assert Path(target.path).read_bytes() == b"one"


def test_only_one_step_back_is_kept(paths, tmp_path):
    for index, (name, body, version) in enumerate(
        [("a.deb", b"one", "0.1.0"), ("b.deb", b"two", "0.2.0"), ("c.deb", b"three", "0.3.0")]
    ):
        retention.retain(
            paths, archive(tmp_path, name, body), sha256=str(index), version=version
        )

    kept = retention.read(paths)

    assert (kept.current.version, kept.previous.version) == ("0.3.0", "0.2.0")
    assert Path(kept.previous.path).read_bytes() == b"two"


def test_seeding_the_current_slot_displaces_nothing(paths, tmp_path):
    """What an image build does for a manager that was never installed."""

    retention.retain(
        paths, archive(tmp_path, "shipped.deb", b"image"), sha256="aaa",
        version="0.1.0", rotate=False,
    )

    kept = retention.read(paths)

    assert kept.current.present
    assert not kept.can_revert


def test_a_record_pointing_at_a_file_that_vanished_is_refused(paths, tmp_path):
    retention.retain(paths, archive(tmp_path, "a.deb", b"one"), sha256="aaa", version="0.1.0")
    retention.retain(paths, archive(tmp_path, "b.deb", b"two"), sha256="bbb", version="0.2.0")
    Path(retention.read(paths).previous.path).unlink()

    with pytest.raises(retention.RetentionError) as refusal:
        retention.revert_target(paths)

    assert refusal.value.code == "previous_package_missing"


def test_the_record_keys_on_the_digest_not_the_version(paths, tmp_path):
    """Two commits can build one version; a revert must not pick by name.

    Choosing a target by version string is how a revert reinstalls the very
    archive it is trying to leave.
    """

    retention.retain(paths, archive(tmp_path, "a.deb", b"one"), sha256="aaa",
                     version="0.1.0", build_id="20260101")
    retention.retain(paths, archive(tmp_path, "b.deb", b"two"), sha256="bbb",
                     version="0.1.0", build_id="20260202")

    target = retention.revert_target(paths)

    assert (target.sha256, target.build_id) == ("aaa", "20260101")
    assert target.version == "0.1.0", "the versions are equal, which is the point"


def test_an_unreadable_record_refuses_rather_than_reverting_blind(paths, tmp_path):
    retention.retain(paths, archive(tmp_path, "a.deb", b"one"), sha256="aaa", version="0.1.0")
    (Path(paths.packages_dir) / retention.RECORD_NAME).write_text("{not json", encoding="utf-8")

    assert retention.read(paths).unreadable
    with pytest.raises(retention.RetentionError) as refusal:
        retention.revert_target(paths)
    assert refusal.value.code == "retention_unreadable"


def test_a_record_from_a_newer_manager_is_not_guessed_at(paths, tmp_path):
    retention.retain(paths, archive(tmp_path, "a.deb", b"one"), sha256="aaa", version="0.1.0")
    record = Path(paths.packages_dir) / retention.RECORD_NAME
    payload = json.loads(record.read_text(encoding="utf-8"))
    payload["schema_version"] = 99
    record.write_text(json.dumps(payload), encoding="utf-8")

    assert "99" in retention.read(paths).unreadable


def test_retaining_something_that_is_not_there_is_an_error(paths, tmp_path):
    with pytest.raises(retention.RetentionError) as refusal:
        retention.retain(paths, tmp_path / "absent.deb", sha256="aaa", version="0.1.0")

    assert refusal.value.code == "retention_source_missing"


def test_the_kept_archives_are_not_world_readable(paths, tmp_path):
    """They are root's, on a host where the web account is not."""

    retention.retain(paths, archive(tmp_path, "a.deb", b"one"), sha256="aaa", version="0.1.0")

    kept = Path(retention.read(paths).current.path)

    assert kept.stat().st_mode & 0o077 == 0
    assert Path(paths.packages_dir).stat().st_mode & 0o077 == 0
