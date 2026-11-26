# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Staging folders of a backup, and the sweep of those a killed backup left.

Backlog 19: a database snapshot folder beside each SQLite file, the archive
parts in the backup folder and an Admin upload all stayed for good when their
process died half-way, plaintext database copies among them. The sweep may
delete only what it can prove abandoned: a claim it can lock, whose content
names this owner and the folder's kind. A live backup is held by a real
``flock`` in a second process here, and killed with SIGKILL, which runs no
cleanup at all.
"""

import errno
import fcntl
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ems import backup
from ems import backup_staging as staging
from ems.backup_staging import (
    ARCHIVE_PARTS,
    CLAIM_NAME,
    DATABASE_SNAPSHOTS,
    IMPORT_UPLOADS,
    RESTORE_PARTS,
    claim_staging,
    sweep_abandoned,
)
from tests.test_backup import create, create_db, write_db_project, write_project

pytestmark = [
    pytest.mark.backup_restore,
    pytest.mark.integration,
]

ROOT = Path(__file__).resolve().parents[1]
HOLDER = (
    "import sys\n"
    "from ems.backup_staging import claim_staging\n"
    "folder = claim_staging(sys.argv[1], sys.argv[2])\n"
    "print(folder.path, flush=True)\n"
    "sys.stdin.read()\n"
)


class Holder:
    """A backup in another process that holds its claim until it is killed."""

    def __init__(self, parent, kind):
        self.process = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(parent), kind],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.path = self.process.stdout.readline().strip()
        assert self.path, "the holder made no staging folder"

    def kill(self):
        self.process.kill()
        self.process.wait(timeout=30)
        self.process.stdout.close()
        self.process.stdin.close()


@pytest.fixture
def holders():
    started = []

    def start(parent, kind):
        holder = Holder(parent, kind)
        started.append(holder)
        return holder

    yield start
    for holder in started:
        if holder.process.poll() is None:
            holder.kill()


def abandoned(parent, kind):
    """A claimed folder whose process let go of its lock without removing it."""

    folder = claim_staging(str(parent), kind)
    Path(folder.path, "snapshot.sqlite").write_bytes(b"plaintext copy")
    os.close(folder._claim_fd)
    folder._claim_fd = None
    return folder.path


def hidden(directory):
    return sorted(name for name in os.listdir(directory) if name.startswith("."))


def test_a_folder_stays_while_its_backup_runs_and_goes_once_it_is_killed(tmp_path, holders):
    holder = holders(tmp_path, DATABASE_SNAPSHOTS)
    Path(holder.path, "snapshot.sqlite").write_bytes(b"in use")

    assert sweep_abandoned(tmp_path, (DATABASE_SNAPSHOTS,)) == []
    assert os.path.isfile(os.path.join(holder.path, "snapshot.sqlite"))

    holder.kill()

    assert sweep_abandoned(tmp_path, (DATABASE_SNAPSHOTS,)) == [holder.path]
    assert os.listdir(tmp_path) == []


def test_a_released_folder_is_gone_and_a_claim_names_its_owner_and_kind(tmp_path):
    folder = claim_staging(str(tmp_path), ARCHIVE_PARTS)
    claim = json.loads(Path(folder.path, CLAIM_NAME).read_text(encoding="utf-8"))
    assert os.path.basename(folder.path).startswith(ARCHIVE_PARTS)
    assert (claim["schema"], claim["owner"], claim["kind"]) == (1, "ems-backup", ARCHIVE_PARTS)
    assert oct(os.stat(folder.path).st_mode & 0o777) == "0o700"

    folder.release()
    folder.release()

    assert os.listdir(tmp_path) == []


def test_an_unknown_kind_is_refused_before_anything_is_made(tmp_path):
    with pytest.raises(ValueError, match="unknown staging kind"):
        claim_staging(str(tmp_path), "tmp")
    assert os.listdir(tmp_path) == []


def _no_claim(path):
    os.remove(os.path.join(path, CLAIM_NAME))


def _empty_claim(path):
    Path(path, CLAIM_NAME).write_bytes(b"")


def _foreign_owner(path):
    Path(path, CLAIM_NAME).write_text(
        json.dumps({"schema": 1, "owner": "someone-else", "kind": DATABASE_SNAPSHOTS}), encoding="utf-8"
    )


def _other_kind(path):
    Path(path, CLAIM_NAME).write_text(
        json.dumps({"schema": 1, "owner": "ems-backup", "kind": ARCHIVE_PARTS}), encoding="utf-8"
    )


def _oversized_claim(path):
    claim = {"schema": 1, "owner": "ems-backup", "kind": DATABASE_SNAPSHOTS}
    Path(path, CLAIM_NAME).write_text(json.dumps(claim) + " " * 5000, encoding="utf-8")


def _symlinked_claim(path):
    elsewhere = os.path.join(os.path.dirname(path), "valid-claim")
    os.replace(os.path.join(path, CLAIM_NAME), elsewhere)
    os.symlink(elsewhere, os.path.join(path, CLAIM_NAME))


def _fifo_claim(path):
    os.remove(os.path.join(path, CLAIM_NAME))
    os.mkfifo(os.path.join(path, CLAIM_NAME))


@pytest.mark.parametrize(
    "unprove",
    [_no_claim, _empty_claim, _foreign_owner, _other_kind, _oversized_claim, _symlinked_claim, _fifo_claim],
)
def test_a_folder_without_a_claim_that_proves_it_abandoned_stays(tmp_path, unprove):
    path = abandoned(tmp_path, DATABASE_SNAPSHOTS)
    unprove(path)

    assert sweep_abandoned(tmp_path, (DATABASE_SNAPSHOTS,)) == []
    assert os.path.isfile(os.path.join(path, "snapshot.sqlite"))


def test_a_symlink_named_like_a_folder_is_never_followed(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = abandoned(outside, DATABASE_SNAPSHOTS)
    parent = tmp_path / "data"
    parent.mkdir()
    link = parent / (DATABASE_SNAPSHOTS + "link")
    os.symlink(target, link)
    opened = []
    real_open = os.open

    def recording_open(path, *args, **kwargs):
        opened.append(os.fspath(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(staging.os, "open", recording_open)

    assert sweep_abandoned(parent, (DATABASE_SNAPSHOTS,)) == []
    monkeypatch.undo()
    assert opened == []
    assert os.path.isfile(os.path.join(target, "snapshot.sqlite"))
    assert os.path.islink(link)


def test_only_the_kinds_asked_for_and_only_their_names_are_swept(tmp_path):
    snapshots = abandoned(tmp_path, DATABASE_SNAPSHOTS)
    parts = abandoned(tmp_path, ARCHIVE_PARTS)
    renamed = os.path.join(tmp_path, "kept-by-hand")
    os.rename(abandoned(tmp_path, IMPORT_UPLOADS), renamed)

    assert sweep_abandoned(tmp_path, (ARCHIVE_PARTS, IMPORT_UPLOADS)) == [parts]
    assert os.path.isdir(snapshots) and os.path.isdir(renamed)


def test_a_parent_that_cannot_be_listed_sweeps_nothing(tmp_path):
    assert sweep_abandoned(tmp_path / "missing", (DATABASE_SNAPSHOTS,)) == []


def test_a_database_backup_sweeps_the_snapshots_a_killed_one_left(tmp_path, holders):
    base, config = write_db_project(tmp_path)
    data_dir = os.path.join(base, "data")
    left = abandoned(data_dir, DATABASE_SNAPSHOTS)
    running = holders(data_dir, DATABASE_SNAPSHOTS)
    untouched = os.path.join(data_dir, DATABASE_SNAPSHOTS + "from-an-older-release")
    os.mkdir(untouched)

    create_db(base, config)

    assert not os.path.exists(left)
    assert sorted(os.path.join(data_dir, name) for name in hidden(data_dir)) == sorted([running.path, untouched])


def test_an_archive_is_written_in_a_claimed_folder_that_a_later_backup_sweeps(tmp_path, monkeypatch):
    base, config, config_path = write_project(tmp_path)
    backup_dir = os.path.join(base, "backup")
    os.makedirs(backup_dir)
    left = abandoned(backup_dir, ARCHIVE_PARTS)
    upload = abandoned(backup_dir, IMPORT_UPLOADS)
    written = []
    real_write = backup._write_tar

    def recording_write(path, *args, **kwargs):
        written.append(os.path.realpath(path))
        return real_write(path, *args, **kwargs)

    monkeypatch.setattr(backup, "_write_tar", recording_write)

    path = create(tmp_path, config, base, config_path)

    staging = os.path.dirname(written[0])
    assert os.path.dirname(staging) == backup_dir
    assert os.path.basename(staging).startswith(ARCHIVE_PARTS)
    assert not os.path.exists(left) and not os.path.exists(upload)
    assert os.listdir(backup_dir) == [os.path.basename(path)]


def _swap_after_new_file(monkeypatch, tmp_path, victim):
    """After each staged file is made, rename the folder away and put a link to ``victim`` in its place."""

    real_new_file = staging.StagingFolder.new_file

    def new_file_then_swap(self, name):
        path = real_new_file(self, name)
        if not os.path.islink(self.path):
            os.rename(self.path, str(tmp_path / f"moved-{os.path.basename(self.path)}"))
            os.symlink(victim, self.path)
        return path

    monkeypatch.setattr(staging.StagingFolder, "new_file", new_file_then_swap)


def test_a_staged_file_is_written_through_the_folder_it_was_made_in(tmp_path, monkeypatch):
    """Backlog 67: the archive, the upload and the database snapshots reopen
    their staged files by path; a folder renamed away and replaced by a link
    must not redirect those writes."""

    victim = tmp_path / "victim"
    victim.mkdir()
    _swap_after_new_file(monkeypatch, tmp_path, victim)
    folder = claim_staging(str(tmp_path), ARCHIVE_PARTS)

    path = folder.new_file("archive.part")
    with open(path, "wb") as handle:
        handle.write(b"archive bytes")

    assert os.listdir(victim) == []
    (moved,) = [name for name in os.listdir(tmp_path) if name.startswith("moved-")]
    assert Path(tmp_path, moved, "archive.part").read_bytes() == b"archive bytes"
    folder.release()


@pytest.mark.parametrize("password", [None, "pw-secret"])
def test_a_swapped_folder_cannot_redirect_an_archive(tmp_path, monkeypatch, password):
    base, config, config_path = write_project(tmp_path)
    backup_dir = Path(base, "backup")
    backup_dir.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    _swap_after_new_file(monkeypatch, backup_dir, victim)

    path = create(tmp_path, config, base, config_path, password=password)
    monkeypatch.undo()

    assert os.listdir(victim) == []
    assert backup.inspect_backup(path, password=password)["manifest"]["backup_type"] == "config"


def test_a_swapped_folder_cannot_redirect_a_database_snapshot(tmp_path, monkeypatch):
    base, config = write_db_project(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    _swap_after_new_file(monkeypatch, Path(base, "data"), victim)

    path = create_db(base, config)
    monkeypatch.undo()

    assert os.listdir(victim) == []
    roles = {db["role"]: db for db in backup.inspect_backup(path)["manifest"]["databases"]}
    assert roles["dashboard_history"]["included"] is True


@pytest.mark.parametrize("password", [None, "pw-secret"])
def test_an_archive_from_a_staging_folder_stays_readable_by_its_owner_only(tmp_path, password):
    base, config, config_path = write_project(tmp_path)
    path = create(tmp_path, config, base, config_path, password=password)

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_a_failed_archive_leaves_no_part_behind(tmp_path, monkeypatch):
    base, config, config_path = write_project(tmp_path)

    def failing_write(path, *args, **kwargs):
        Path(path).write_bytes(b"partial")
        raise backup.BackupError("disk full")

    monkeypatch.setattr(backup, "_write_tar", failing_write)
    with pytest.raises(backup.BackupError, match="disk full"):
        create(tmp_path, config, base, config_path)
    assert os.listdir(os.path.join(base, "backup")) == []


def test_a_fifo_claim_with_a_valid_claim_waiting_in_it_is_no_proof(tmp_path):
    path = abandoned(tmp_path, DATABASE_SNAPSHOTS)
    claim = os.path.join(path, CLAIM_NAME)
    text = Path(claim).read_bytes()
    os.remove(claim)
    os.mkfifo(claim)
    reader = os.open(claim, os.O_RDONLY | os.O_NONBLOCK)
    writer = os.open(claim, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(writer, text)

        assert sweep_abandoned(tmp_path, (DATABASE_SNAPSHOTS,)) == []
    finally:
        os.close(writer)
        os.close(reader)
    assert os.path.isfile(os.path.join(path, "snapshot.sqlite"))


def test_no_sweep_between_the_steps_of_a_claim_takes_the_folder(tmp_path, monkeypatch):
    """The claim is locked before it is written: a sweep run after each step
    of claim_staging (open, lock, write, sync) finds either an empty claim or
    a held lock, and the folder stays. Written first, a readable claim with a
    free lock would be swept from under the backup making it.
    """

    sweeps = []
    busy = []

    def after(real):
        def step(*args, **kwargs):
            result = real(*args, **kwargs)
            if not busy:
                busy.append(True)
                try:
                    sweeps.append((real.__name__, sweep_abandoned(tmp_path, (ARCHIVE_PARTS,))))
                finally:
                    busy.clear()
            return result

        return step

    for name in ("open", "write", "fsync"):
        monkeypatch.setattr(staging.os, name, after(getattr(os, name)))
    monkeypatch.setattr(staging.fcntl, "flock", after(fcntl.flock))
    folder = claim_staging(str(tmp_path), ARCHIVE_PARTS)
    monkeypatch.undo()

    assert [name for name, _ in sweeps] == ["open", "open", "flock", "write", "fsync"]
    assert all(removed == [] for _, removed in sweeps), sweeps
    assert os.path.isfile(os.path.join(folder.path, CLAIM_NAME))
    assert sweep_abandoned(tmp_path, (ARCHIVE_PARTS,)) == []
    folder.release()
    assert os.listdir(tmp_path) == []


def test_a_claim_replaced_while_the_sweep_reads_it_stops_the_sweep(tmp_path, monkeypatch):
    path = abandoned(tmp_path, DATABASE_SNAPSHOTS)
    claim = os.path.join(path, CLAIM_NAME)
    real_read = os.read

    def read_then_replace(fd, size):
        data = real_read(fd, size)
        replacement = claim + ".new"
        Path(replacement).write_bytes(data)
        os.replace(replacement, claim)
        return data

    monkeypatch.setattr(staging.os, "read", read_then_replace)

    assert sweep_abandoned(tmp_path, (DATABASE_SNAPSHOTS,)) == []
    monkeypatch.undo()
    assert os.path.isfile(os.path.join(path, "snapshot.sqlite"))


def test_a_release_removes_the_claim_last_and_while_it_still_holds_the_lock(tmp_path, monkeypatch):
    folder = claim_staging(str(tmp_path), DATABASE_SNAPSHOTS)
    Path(folder.new_file("0.sqlite")).write_bytes(b"plaintext copy")
    Path(folder.path, "0.sqlite-journal").write_bytes(b"journal")
    Path(folder.new_file("1.sqlite")).write_bytes(b"plaintext copy")
    claim = os.path.join(folder.path, CLAIM_NAME)
    order = []
    real_unlink, real_rmdir = os.unlink, os.rmdir

    def unlink(name, *args, **kwargs):
        if name == CLAIM_NAME:
            probe = os.open(claim, os.O_RDONLY)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)
        result = real_unlink(name, *args, **kwargs)
        order.append(name)
        return result

    def rmdir(path, *args, **kwargs):
        if os.fspath(path) == folder.path:
            assert folder._claim_fd is None, "the claim is still open when its folder goes"
        order.append(os.path.basename(path))
        return real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(staging.os, "unlink", unlink)
    monkeypatch.setattr(staging.os, "rmdir", rmdir)
    folder.release()
    monkeypatch.undo()

    assert order[-2:] == [CLAIM_NAME, os.path.basename(folder.path)]
    assert set(order[:-2]) == {"0.sqlite", "0.sqlite-journal", "1.sqlite"}
    assert os.listdir(tmp_path) == []


def test_a_release_leaves_what_it_did_not_put_there_and_a_later_sweep_finishes_it(tmp_path):
    """Its own folder is removed by what the process created, never by a
    recursive delete; anything else keeps the folder and its claim."""

    folder = claim_staging(str(tmp_path), DATABASE_SNAPSHOTS)
    Path(folder.new_file("0.sqlite")).write_bytes(b"plaintext copy")
    Path(folder.path, "0.sqlite.tmp").write_bytes(b"plaintext copy")

    folder.release()

    assert sorted(os.listdir(folder.path)) == [CLAIM_NAME, "0.sqlite.tmp"]
    assert sweep_abandoned(tmp_path, (DATABASE_SNAPSHOTS,)) == [folder.path]
    assert os.listdir(tmp_path) == []


def test_a_folder_holding_a_directory_is_never_swept(tmp_path):
    """No staging kind makes subfolders: a directory in one was moved there,
    and nothing is removed recursively."""

    path = abandoned(tmp_path, DATABASE_SNAPSHOTS)
    os.mkdir(os.path.join(path, "influxdb"))
    Path(path, "influxdb", "engine.tsm").write_bytes(b"not a staged file")

    assert sweep_abandoned(tmp_path, (DATABASE_SNAPSHOTS,)) == []
    assert os.path.isfile(os.path.join(path, "influxdb", "engine.tsm"))
    assert os.path.isfile(os.path.join(path, "snapshot.sqlite"))


def test_an_empty_folder_of_another_owner_swapped_in_is_refused_untouched(tmp_path, monkeypatch):
    """An empty directory of another account passes the emptiness check; it
    shows another owner than the claim this process just made in it."""

    opened = []
    real_open_folder, real_fstat = staging._open_folder, os.fstat

    def open_folder(path):
        fd = real_open_folder(path)
        opened.append(fd)
        return fd

    def fstat(fd):
        status = real_fstat(fd)
        if opened and fd == opened[0]:
            return type("Status", (), {"st_uid": status.st_uid + 4242, "st_mode": status.st_mode})()
        return status

    monkeypatch.setattr(staging, "_open_folder", open_folder)
    monkeypatch.setattr(staging.os, "fstat", fstat)
    with pytest.raises(OSError, match="not a folder this process made"):
        claim_staging(str(tmp_path), DATABASE_SNAPSHOTS)
    monkeypatch.undo()

    (left,) = os.listdir(tmp_path)
    assert os.listdir(tmp_path / left) == []


def test_a_folder_moved_in_under_the_name_is_refused_untouched(tmp_path, monkeypatch):
    """Someone who may rename entries in the parent can put a directory of
    theirs, or one they moved from elsewhere, where the new folder was."""

    moved = tmp_path / "secrets"
    moved.mkdir(mode=0o700)
    (moved / "key").write_text("keep me")
    real_mkdtemp = staging.tempfile.mkdtemp

    def mkdtemp_then_swap(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        os.rmdir(path)
        os.rename(moved, path)
        return path

    monkeypatch.setattr(staging.tempfile, "mkdtemp", mkdtemp_then_swap)
    with pytest.raises(OSError, match="not the empty folder"):
        claim_staging(str(tmp_path), DATABASE_SNAPSHOTS)
    monkeypatch.undo()

    (swapped,) = os.listdir(tmp_path)
    assert os.listdir(tmp_path / swapped) == ["key"]
    assert (tmp_path / swapped / "key").read_text() == "keep me"


def test_a_folder_another_account_owns_is_never_swept(tmp_path, monkeypatch):
    path = abandoned(tmp_path, DATABASE_SNAPSHOTS)
    monkeypatch.setattr(staging.os, "geteuid", lambda: os.getuid() + 4242)

    assert sweep_abandoned(tmp_path, (DATABASE_SNAPSHOTS,)) == []
    monkeypatch.undo()
    assert os.path.isfile(os.path.join(path, "snapshot.sqlite"))


@pytest.mark.parametrize("code", [errno.ENOLCK, errno.EOPNOTSUPP])
def test_a_filesystem_without_locks_still_backs_up_and_its_folders_are_never_swept(tmp_path, monkeypatch, code):
    def no_locks(fd, operation):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(staging.fcntl, "flock", no_locks)
    folder = claim_staging(str(tmp_path), ARCHIVE_PARTS)
    Path(folder.new_file("archive.part")).write_bytes(b"archive")

    assert not os.path.exists(os.path.join(folder.path, CLAIM_NAME))
    assert sweep_abandoned(tmp_path, (ARCHIVE_PARTS,)) == []
    folder.release()
    assert os.listdir(tmp_path) == []


def test_any_other_lock_failure_fails_the_claim_and_leaves_nothing(tmp_path, monkeypatch):
    def broken(fd, operation):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(staging.fcntl, "flock", broken)
    with pytest.raises(OSError, match="I/O error"):
        claim_staging(str(tmp_path), ARCHIVE_PARTS)
    assert os.listdir(tmp_path) == []


def _outside_data(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    Path(outside, "keep.sqlite").write_bytes(b"not ours")
    Path(outside, CLAIM_NAME).write_bytes(b"not ours either")
    return outside


def test_a_release_whose_folder_was_swapped_for_a_link_removes_nothing_outside(tmp_path):
    outside = _outside_data(tmp_path)
    folder = claim_staging(str(tmp_path), DATABASE_SNAPSHOTS)
    moved = str(tmp_path / "moved-away")
    os.rename(folder.path, moved)
    os.symlink(outside, folder.path)

    folder.release()

    assert sorted(os.listdir(outside)) == sorted([CLAIM_NAME, "keep.sqlite"])
    assert os.path.islink(folder.path)


def test_a_sweep_whose_folder_turns_into_a_link_after_the_listing_removes_nothing(tmp_path, monkeypatch):
    parent = tmp_path / "data"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = abandoned(outside, DATABASE_SNAPSHOTS)
    path = abandoned(parent, DATABASE_SNAPSHOTS)
    real_open = os.open
    swapped = []

    def swap_then_open(name, *args, **kwargs):
        if os.fspath(name) == path and not swapped:
            swapped.append(True)
            os.rename(path, str(parent / "moved-away"))
            os.symlink(target, path)
        return real_open(name, *args, **kwargs)

    monkeypatch.setattr(staging.os, "open", swap_then_open)

    assert sweep_abandoned(parent, (DATABASE_SNAPSHOTS,)) == []
    monkeypatch.undo()
    assert swapped == [True]
    assert os.path.isfile(os.path.join(target, "snapshot.sqlite"))
    assert os.path.isfile(os.path.join(target, CLAIM_NAME))


def test_a_release_whose_closes_fail_still_drops_the_lock_and_raises_nothing(tmp_path, monkeypatch):
    folder = claim_staging(str(tmp_path), ARCHIVE_PARTS)
    claim = os.path.join(folder.path, CLAIM_NAME)
    probe = os.open(claim, os.O_RDONLY)
    real_close = os.close

    def failing_close(fd):
        real_close(fd)
        raise OSError(errno.EIO, "close reported an error")

    monkeypatch.setattr(staging.os, "close", failing_close)
    try:
        folder.release()
    finally:
        monkeypatch.undo()
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(probe)
    assert folder._claim_fd is None and folder._folder_fd is None


def _recording_claims(monkeypatch):
    claimed = []
    real_claim = staging.claim_staging

    def recording(parent, kind):
        folder = real_claim(parent, kind)
        claimed.append((os.fspath(parent), kind, folder.path))
        return folder

    monkeypatch.setattr(backup.backup_staging, "claim_staging", recording)
    return claimed


def test_a_restored_file_is_staged_in_a_claimed_folder_beside_it_and_nothing_stays(tmp_path, monkeypatch):
    """Backlog 60: a restore killed before its rename left a .restore-* copy for good."""

    target = tmp_path / "config" / "config.json"
    target.parent.mkdir()
    target.write_text("{}")
    os.chmod(target, 0o640)
    claimed = _recording_claims(monkeypatch)

    backup._atomic_write(str(target), b'{"restored": true}')

    assert claimed == [(str(target.parent), RESTORE_PARTS, claimed[0][2])]
    assert os.path.dirname(claimed[0][2]) == str(target.parent)
    assert target.read_bytes() == b'{"restored": true}'
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o640
    assert os.listdir(target.parent) == ["config.json"]


def test_a_staging_folder_swapped_for_a_link_cannot_redirect_a_restore(tmp_path, monkeypatch):
    """A parent another account may write lets it rename the folder away and
    put a link to its own directory in its place; the restore keeps writing
    through the folder it made and checked."""

    target = tmp_path / "config" / "config.json"
    target.parent.mkdir()
    target.write_text("{}")
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "restore.part").write_text("not yours")
    real_create = staging.StagingFolder.create

    def create_then_swap(self, name):
        fd = real_create(self, name)
        os.rename(self.path, str(tmp_path / "moved-away"))
        os.symlink(victim, self.path)
        return fd

    monkeypatch.setattr(staging.StagingFolder, "create", create_then_swap)

    backup._atomic_write(str(target), b'{"restored": true}')

    assert target.read_bytes() == b'{"restored": true}' and not target.is_symlink()
    assert (victim / "restore.part").read_text() == "not yours"
    assert sorted(os.listdir(victim)) == ["restore.part"]


def test_a_new_restored_file_stays_private(tmp_path):
    target = tmp_path / "data" / "runtime-state.json"

    backup._atomic_write(str(target), b"{}")

    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert os.listdir(target.parent) == ["runtime-state.json"]


def test_a_restore_sweeps_what_a_killed_restore_left_and_keeps_a_running_one(tmp_path, holders):
    data = tmp_path / "data"
    data.mkdir()
    left = abandoned(data, RESTORE_PARTS)
    running = holders(data, RESTORE_PARTS)

    backup._atomic_write(str(data / "runtime-state.json"), b"{}")

    assert not os.path.exists(left)
    assert sorted(os.listdir(data)) == sorted(["runtime-state.json", os.path.basename(running.path)])


def test_a_restore_into_a_folder_that_takes_no_staging_fails_as_a_restore_error(tmp_path, monkeypatch):
    database = tmp_path / "ems_dashboard.sqlite"
    database.write_bytes(_sqlite_bytes(tmp_path, "live"))

    def refuse(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(staging.tempfile, "mkdtemp", refuse)
    with pytest.raises(backup.BackupError, match="No space left"):
        backup._atomic_write(str(tmp_path / "config.json"), b"{}")
    with pytest.raises(backup.BackupError, match="No space left"):
        backup._restore_sqlite(str(database), b"{}")


def _sqlite_bytes(tmp_path, value):
    import sqlite3

    path = tmp_path / f"source-{value}.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", (value,))
    conn.commit()
    conn.close()
    data = path.read_bytes()
    path.unlink()
    return data


def test_a_restored_database_is_staged_in_a_claimed_folder_and_nothing_stays(tmp_path, monkeypatch):
    import sqlite3

    data = tmp_path / "data"
    data.mkdir()
    target = data / "ems_dashboard.sqlite"
    target.write_bytes(_sqlite_bytes(tmp_path, "old"))
    left = abandoned(data, RESTORE_PARTS)
    restored = _sqlite_bytes(tmp_path, "new")
    claimed = _recording_claims(monkeypatch)

    backup._restore_sqlite(str(target), restored)
    monkeypatch.undo()

    assert [(parent, kind) for parent, kind, _ in claimed] == [(str(data), RESTORE_PARTS)]
    conn = sqlite3.connect(target)
    assert conn.execute("SELECT x FROM t").fetchall() == [("new",)]
    conn.close()
    assert not os.path.exists(left)
    assert os.listdir(data) == ["ems_dashboard.sqlite"]


def test_a_corrupt_database_replaced_by_a_restore_keeps_its_mode(tmp_path):
    import sqlite3

    target = tmp_path / "ems_dashboard.sqlite"
    target.write_bytes(b"this is not a database" * 100)
    os.chmod(target, 0o644)

    backup._restore_sqlite(str(target), _sqlite_bytes(tmp_path, "new"))

    assert stat.S_IMODE(os.stat(target).st_mode) == 0o644
    conn = sqlite3.connect(target)
    assert conn.execute("SELECT x FROM t").fetchall() == [("new",)]
    conn.close()
    assert os.listdir(tmp_path) == ["ems_dashboard.sqlite"]


def test_a_database_backup_also_sweeps_restore_parts_beside_the_databases(tmp_path):
    base, config = write_db_project(tmp_path)
    data_dir = os.path.join(base, "data")
    left = abandoned(data_dir, RESTORE_PARTS)

    create_db(base, config)

    assert not os.path.exists(left)


def test_a_swapped_staging_folder_cannot_feed_a_database_restore(tmp_path, monkeypatch):
    import sqlite3

    data = tmp_path / "data"
    data.mkdir()
    target = data / "ems_dashboard.sqlite"
    target.write_bytes(_sqlite_bytes(tmp_path, "old"))
    restored = _sqlite_bytes(tmp_path, "mine")
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "restore.sqlite").write_bytes(_sqlite_bytes(tmp_path, "theirs"))
    real_create = staging.StagingFolder.create

    def create_then_swap(self, name):
        fd = real_create(self, name)
        os.rename(self.path, str(tmp_path / "moved-away"))
        os.symlink(victim, self.path)
        return fd

    monkeypatch.setattr(staging.StagingFolder, "create", create_then_swap)
    backup._restore_sqlite(str(target), restored)
    monkeypatch.undo()

    conn = sqlite3.connect(target)
    assert conn.execute("SELECT x FROM t").fetchall() == [("mine",)]
    conn.close()
