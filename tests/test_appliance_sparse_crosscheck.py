# SPDX-License-Identifier: AGPL-3.0-or-later
"""How the external sparse cross-check gets at the members it compares.

The cross-check exists because the appliance's own sparse decoder is the one
piece of the update path with no second opinion. It unpacked the artefact with
``tar -I zstd -xf`` first — so the verifier written to check untrusted input was
itself handed that input raw, before the safe parser ever saw it. A member
called ``../../etc/cron.d/x``, an absolute path, a symlink or a third member
nobody expected would have been written to the host running the check.

The members are now staged through the same allowlist the appliance writes a
slot from, and simg2img is pointed at exactly those staged files.
"""

import io
import tarfile
from pathlib import Path

import pytest

from appliance import os_artifacts

pytestmark = [pytest.mark.unit, pytest.mark.simulation]

ROOT = Path(__file__).resolve().parents[1]
CROSSCHECK = ROOT / "scripts/appliance-crosscheck-sparse.sh"
MEMBERS = ("boot", "system")


def archive_with(path, entries):
    with tarfile.open(path, "w") as archive:
        for info, payload in entries:
            archive.addfile(info, io.BytesIO(payload) if payload is not None else None)
    return path


def regular(name, payload=b"content"):
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.type = tarfile.REGTYPE
    return info, payload


def test_the_two_real_members_are_staged(tmp_path):
    archive = archive_with(
        tmp_path / "update.tar", [regular("boot"), regular("system", b"root")]
    )

    staged = os_artifacts.stage_members(archive, tmp_path / "staging", MEMBERS)

    assert sorted(staged.members) == ["boot", "system"]
    assert (tmp_path / "staging/system").read_bytes() == b"root"


@pytest.mark.parametrize(
    ("label", "entries"),
    [
        ("traversal", [regular("boot"), regular("system"), regular("../escape")]),
        ("absolute", [regular("boot"), regular("system"), regular("/etc/cron.d/x")]),
        ("nested", [regular("boot"), regular("system"), regular("sub/system")]),
        ("unexpected", [regular("boot"), regular("system"), regular("payload")]),
        ("duplicate", [regular("boot"), regular("system"), regular("system", b"again")]),
    ],
)
def test_an_archive_member_the_appliance_never_writes_is_refused(tmp_path, label, entries):
    archive = archive_with(tmp_path / f"{label}.tar", entries)

    with pytest.raises(os_artifacts.ArtifactError) as raised:
        os_artifacts.stage_members(archive, tmp_path / "staging", MEMBERS)

    assert raised.value.code == "artifact_member_refused"
    assert not (tmp_path / "staging").exists() or list((tmp_path / "staging").iterdir()) == []


def test_a_symlink_member_is_refused(tmp_path):
    link = tarfile.TarInfo("system")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/shadow"
    archive = archive_with(tmp_path / "link.tar", [regular("boot"), (link, None)])

    with pytest.raises(os_artifacts.ArtifactError) as raised:
        os_artifacts.stage_members(archive, tmp_path / "staging", MEMBERS)

    assert raised.value.code == "artifact_member_refused"


def test_a_hardlink_member_is_refused(tmp_path):
    link = tarfile.TarInfo("system")
    link.type = tarfile.LNKTYPE
    link.linkname = "boot"
    archive = archive_with(tmp_path / "hardlink.tar", [regular("boot"), (link, None)])

    with pytest.raises(os_artifacts.ArtifactError) as raised:
        os_artifacts.stage_members(archive, tmp_path / "staging", MEMBERS)

    assert raised.value.code == "artifact_member_refused"


def test_a_device_node_member_is_refused(tmp_path):
    node = tarfile.TarInfo("system")
    node.type = tarfile.CHRTYPE
    archive = archive_with(tmp_path / "device.tar", [regular("boot"), (node, None)])

    with pytest.raises(os_artifacts.ArtifactError) as raised:
        os_artifacts.stage_members(archive, tmp_path / "staging", MEMBERS)

    assert raised.value.code == "artifact_member_refused"


def test_an_archive_missing_a_member_is_refused(tmp_path):
    archive = archive_with(tmp_path / "short.tar", [regular("boot")])

    with pytest.raises(os_artifacts.ArtifactError) as raised:
        os_artifacts.stage_members(archive, tmp_path / "staging", MEMBERS)

    assert raised.value.code == "artifact_member_missing"


def test_extract_still_verifies_digests_on_top_of_the_shared_staging():
    """The digest check is what ``extract`` adds; the allowlist is shared."""

    source = (ROOT / "appliance/os_artifacts.py").read_text(encoding="utf-8")

    assert "def stage_members(" in source
    assert "staged = stage_members(archive_path, staging_dir, set(release.members))" in source
    assert "artifact_member_digest_mismatch" in source


def test_the_crosscheck_never_unpacks_an_untrusted_archive_with_tar():
    text = CROSSCHECK.read_text(encoding="utf-8")

    assert "tar -I zstd -xf" not in text
    assert "os_artifacts.stage_members" in text
