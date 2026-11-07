# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The backup account reads the backups, every one of them, and nothing live.

Owner decision 2026-11-04: backups are fetched over SFTP, unencrypted ones
included, and the live configuration is out of reach. So the export is one
read-only bind of ``data/backups`` at ``/backups``. Earlier packages exported
``config``, ``backups`` and ``data``; a run retires the two live ones and
withdraws what the manifest says this package granted on them.

Before this, the grant never reached a file at all: it was applied through
``/proc/self/fd/N``, a link setfacl -R does not walk below, so every 0600
archive stayed unreadable and the test stub, which walked through the link,
could not tell.
"""

import os
import pwd
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.helpers.appliance_export_script import BACKUP_USER, ExportScriptHarness
from tests.helpers.appliance_object_identity import object_identity

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.backup_restore, pytest.mark.appliance]


@pytest.fixture
def harness(tmp_path):
    host = ExportScriptHarness(tmp_path)
    host.seed_installation()
    return host


def archives(harness):
    backups = harness.export_source("backups")
    nested = backups / "2026" / "11"
    nested.mkdir(parents=True)
    archive = nested / "ems-backup-20261104.tar.gz"
    archive.write_bytes(b"archive")
    archive.chmod(0o600)
    return backups, archive


def test_the_export_publishes_the_backups_and_nothing_live(harness):
    for name in ("config", "data"):
        (harness.install_root / name).mkdir(parents=True, exist_ok=True)

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert [entry["name"] for entry in harness.status()["paths"]] == ["backups"]
    assert harness.status()["paths"][0]["source"] == str(
        harness.install_root / "data" / "backups"
    )
    assert sorted(item.name for item in harness.export_root.iterdir()) == ["backups"]
    for name in ("config", "data"):
        assert harness.acl_entry(harness.install_root / name, BACKUP_USER) is None


def test_the_grant_reaches_every_archive_below_the_export(harness):
    """Applied through the handle itself, setfacl -R changed the directory only."""

    backups, archive = archives(harness)

    assert harness.run().returncode == 0

    for path in (backups, archive.parent, archive):
        assert harness.acl_entry(path, BACKUP_USER) == "rX", (path, harness.acl_state())


def test_an_archive_rotated_away_during_the_walk_is_not_a_failure(harness):
    """EMS deletes old archives; one gone between readdir and read was a failed run,
    and a failed export run sets the backup keys aside."""

    backups, archive = archives(harness)
    harness.environment["EMS_STUB_GETFACL_VANISH"] = str(archive)

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert harness.status()["status"] == "configured"
    assert not any(str(archive) in line for line in harness.manifest_lines())


def test_a_backup_directory_that_appears_later_is_mounted_by_the_refresh(harness):
    """data/backups exists once EMS wrote its first archive; the watcher sees that."""

    shutil.rmtree(harness.export_source("backups"))
    (harness.install_root / "data").mkdir(parents=True, exist_ok=True)
    assert harness.run().returncode == 0
    assert harness.status()["paths"][0]["state"] == "missing"

    archives(harness)
    result = harness.run("--refresh-acl")

    assert result.returncode == 0, result.stdout + result.stderr
    assert harness.status()["paths"][0]["state"] == "mounted", harness.status()


def test_a_failed_refresh_leaves_the_last_report_alone(harness):
    """A refusal on the refresh path used to rewrite the report as failed, with
    no paths, until the next full run; verify-install then failed on it."""

    assert harness.run().returncode == 0
    before = harness.status_file.read_text(encoding="utf-8")
    harness.environment["EMS_STUB_SETFACL_FAIL_AT"] = "1"

    result = harness.run("--refresh-acl")

    assert result.returncode != 0
    assert harness.status_file.read_text(encoding="utf-8") == before


# --- retiring the exports earlier packages made ------------------------------


def previous_three_export_install(harness, *, operator_changed=False):
    """What a run of an earlier package left: three binds and their grants."""

    for name in ("config", "data"):
        (harness.install_root / name).mkdir(parents=True, exist_ok=True)
        target = harness.export_root / name
        target.mkdir(parents=True, exist_ok=True)
        harness.mark_mounted(target, options="ro,relatime")
    lines = [
        "# ems-appliance ACL manifest v3",
        "schema=3",
        f"user={BACKUP_USER}",
        f"install_root={harness.install_root}",
        "installation_id=test-installation",
        "recorded_at=2026-11-01T00:00:00Z",
        "optional_identity=none",
    ]
    for name in ("config", "data"):
        root = harness.install_root / name
        identity = object_identity(root)
        lines.append(f"root\t{root}\t{identity}\trecursive")
        for scope in ("access", "default"):
            lines.append(f"entry\t{root}\t{identity}\t{scope}\tno\t-\tr-x")
            harness.set_acl(root, BACKUP_USER, "r-x", kind=scope)
    if operator_changed:
        harness.set_acl(harness.install_root / "config", BACKUP_USER, "rwx")
    harness.acl_manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    harness.acl_state_file.write_text("committed\n", encoding="utf-8")


def test_a_run_retires_the_live_exports_an_earlier_package_made(harness):
    previous_three_export_install(harness)

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(item.name for item in harness.export_root.iterdir()) == ["backups"]
    for name in ("config", "data"):
        root = harness.install_root / name
        assert harness.acl_entry(root, BACKUP_USER) is None, harness.acl_state()
        assert harness.acl_entry(root, BACKUP_USER, kind="default") is None
        assert not any(f"\t{root}\t" in line for line in harness.manifest_lines())


def test_a_retired_grant_an_operator_changed_is_left_to_them_and_reported(harness):
    previous_three_export_install(harness, operator_changed=True)

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert harness.acl_entry(harness.install_root / "config", BACKUP_USER) == "rwx"
    assert harness.acl_entry(harness.install_root / "data", BACKUP_USER) is None
    assert str(harness.install_root / "config") in result.stderr


def test_a_retired_export_with_operator_content_is_refused_not_deleted(harness):
    previous_three_export_install(harness)
    target = harness.export_root / "config"
    harness.run("--teardown")
    (target / "operator-note.txt").write_text("mine\n", encoding="utf-8")

    result = harness.run()

    assert result.returncode != 0
    assert (target / "operator-note.txt").is_file()
    assert "not empty" in result.stderr


# --- the real tools ----------------------------------------------------------


def acl_capable_scratch():
    """A tmpfs this user may set ACLs on; the test scratch here is ZFS without them."""

    if shutil.which("setfacl") is None or not Path("/dev/shm").is_dir():
        return None
    try:
        probe = Path(tempfile.mkdtemp(prefix="ems-acl-probe-", dir="/dev/shm"))
    except OSError:
        return None
    user = pwd.getpwuid(os.getuid()).pw_name
    works = subprocess.run(
        ["setfacl", "-m", f"u:{user}:r", str(probe)], capture_output=True, check=False
    ).returncode == 0
    if not works:
        shutil.rmtree(probe, ignore_errors=True)
        return None
    return probe


def test_the_real_setfacl_grant_makes_a_nested_0600_archive_readable():
    """The test the stub could not be: real setfacl and getfacl, a real tree."""

    scratch = acl_capable_scratch()
    if scratch is None:
        pytest.skip("no filesystem here takes POSIX ACLs from this user")
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
        harness = ExportScriptHarness(scratch, account=user)
        for tool in ("setfacl", "getfacl"):
            (harness.stub_dir / tool).unlink()
        harness.seed_installation()
        backups, archive = archives(harness)

        result = harness.run("--refresh-acl")

        assert result.returncode == 0, result.stdout + result.stderr
        acl = subprocess.run(
            ["getfacl", "-p", str(archive)], capture_output=True, text=True, check=True
        ).stdout
        assert f"user:{user}:r--" in acl, acl
        assert "mask::r--" in acl, acl
        manifest = harness.acl_manifest.read_text(encoding="utf-8")
        assert f"entry\t{archive}\t" in manifest
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_an_entry_an_earlier_package_left_by_inheritance_is_the_packages_to_withdraw(harness):
    """Released packages granted the default ACL on data itself, so archives EMS
    wrote since inherited u:ems-backup. Recorded as pre-existing, purge would
    have put them back instead of withdrawing them."""

    previous_three_export_install(harness)
    backups, archive = archives(harness)
    harness.set_acl(archive, BACKUP_USER, "r-x")

    assert harness.run().returncode == 0

    entries = [line.split("\t") for line in harness.manifest_lines() if line.startswith("entry\t")]
    mine = [entry for entry in entries if entry[1] == str(archive) and entry[3] == "access"]
    assert mine and mine[0][4] == "no", mine
