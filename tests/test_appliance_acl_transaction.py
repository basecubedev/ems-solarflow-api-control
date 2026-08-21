# SPDX-License-Identifier: AGPL-3.0-or-later
"""An ACL this package applied is either in the manifest or it never happened.

The manifest is the only thing that lets removal tell the entries this package
granted from the entries an operator added: without it, purge either leaves
every ``ems-backup`` ACL behind forever or deletes ones it cannot prove are its
own. So a grant and its record are one transaction. Every way the record can
fail to exist — the staging file, the pre-state capture, the read-back, the
commit — has to leave the host with no untracked grant on it, and a mutation
that failed halfway has to be put back rather than reported as a partial
success.
"""

from pathlib import Path

import pytest

from tests.helpers.appliance_export_script import BACKUP_USER, ExportScriptHarness

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.backup_restore]


@pytest.fixture
def harness(tmp_path):
    host = ExportScriptHarness(tmp_path)
    host.seed_installation()
    return host


def granted_entries(harness):
    """Every object that carries an entry for the backup account right now."""

    return {
        path: entries
        for path, entries in harness.acl_state().items()
        if BACKUP_USER in (entries.get("access") or {})
        or BACKUP_USER in (entries.get("default") or {})
    }


def manifest_committed(harness):
    return harness.acl_manifest.is_file()


def staged_files(harness):
    directory = harness.acl_manifest.parent
    if not directory.is_dir():
        return []
    return sorted(item.name for item in directory.iterdir() if item.name.startswith("."))


# --- a run that cannot record what it is about to do changes nothing --------


def test_a_manifest_that_cannot_be_staged_stops_before_the_first_grant(harness):
    """A mode root ignores would prove nothing, so the path itself is blocked."""

    blocked = harness.root / "not-a-directory"
    blocked.write_text("a file where the manifest directory would go\n", encoding="utf-8")

    result = harness.run(EMS_APPLIANCE_ACL_MANIFEST=str(blocked / "acl-manifest.tsv"))

    assert result.returncode != 0, result.stdout + result.stderr
    assert granted_entries(harness) == {}, harness.acl_state()
    assert not manifest_committed(harness)
    assert harness.status()["status"] == "failed", harness.status()


def test_a_pre_state_that_cannot_be_captured_stops_before_the_first_grant(harness):
    harness.environment["EMS_STUB_GETFACL_FAIL_AT"] = "1"

    result = harness.run()

    assert result.returncode != 0, result.stdout + result.stderr
    assert granted_entries(harness) == {}, harness.acl_state()
    assert not manifest_committed(harness)


def test_a_previous_manifest_that_cannot_be_read_stops_before_the_first_grant(harness):
    harness.run()
    assert manifest_committed(harness)
    harness.acl_manifest.chmod(0o000)
    for path in list(harness.acl_state()):
        harness.set_acl(path, BACKUP_USER, "r-x")

    try:
        result = harness.run()
    finally:
        harness.acl_manifest.chmod(0o600)

    assert result.returncode != 0, result.stdout + result.stderr


# --- a run that mutated and then failed puts the pre-state back -------------


def test_a_read_back_that_fails_restores_the_captured_pre_state(harness):
    # 1: install-root pre-state, 2: its read-back, 3: config pre-state,
    # 4: the read-back this run must not be able to skip.
    harness.environment["EMS_STUB_GETFACL_FAIL_AT"] = "4"

    result = harness.run()

    assert result.returncode != 0, result.stdout + result.stderr
    assert granted_entries(harness) == {}, harness.acl_state()
    assert not manifest_committed(harness)


def test_a_failing_setfacl_restores_everything_already_granted(harness):
    # The traversal grant on the install root lands first; the recursive read
    # ACL on config is the one that fails.
    harness.environment["EMS_STUB_SETFACL_FAIL_AT"] = "2"

    result = harness.run()

    assert result.returncode != 0, result.stdout + result.stderr
    assert granted_entries(harness) == {}, harness.acl_state()
    assert not manifest_committed(harness)


def test_a_partial_grant_never_leaves_an_operator_entry_behind(harness):
    operator = harness.install_root / "config" / "operator.json"
    operator.write_text("{}\n", encoding="utf-8")
    harness.set_acl(operator, BACKUP_USER, "rwx")
    harness.environment["EMS_STUB_SETFACL_FAIL_AT"] = "3"

    result = harness.run()

    assert result.returncode != 0, result.stdout + result.stderr
    assert harness.acl_entry(operator, BACKUP_USER) == "rwx", harness.acl_state()


def test_a_commit_that_cannot_be_flushed_rolls_the_grant_back(harness):
    harness.environment["EMS_STUB_SYNC_RC"] = "1"

    result = harness.run()

    assert result.returncode != 0, result.stdout + result.stderr
    assert granted_entries(harness) == {}, harness.acl_state()
    assert not manifest_committed(harness)


def test_an_aborted_run_leaves_no_staged_manifest_behind(harness):
    harness.environment["EMS_STUB_SETFACL_FAIL_AT"] = "2"

    harness.run()

    assert [name for name in staged_files(harness) if name.endswith(".staged")] == []


def test_an_incomplete_restore_is_written_to_a_recovery_manifest(harness):
    """What could not be put back is recorded, never silently kept."""

    # The traversal grant lands, the next one fails, and so does every attempt
    # to undo the first: the one state that must not disappear unrecorded.
    harness.environment["EMS_STUB_SETFACL_FAIL_FROM"] = "2"

    result = harness.run()

    assert result.returncode != 0, result.stdout + result.stderr
    recovery = harness.acl_manifest.parent / "acl-recovery.tsv"
    assert recovery.is_file(), sorted(
        item.name for item in harness.acl_manifest.parent.iterdir()
    )
    text = recovery.read_text(encoding="utf-8")
    assert "unresolved=" in text, text
    assert str(harness.install_root) in text, text
    assert not manifest_committed(harness)


def test_the_recovery_manifest_is_readable_only_by_root(harness):
    harness.environment["EMS_STUB_SETFACL_FAIL_FROM"] = "2"

    harness.run()

    recovery = harness.acl_manifest.parent / "acl-recovery.tsv"
    assert recovery.stat().st_mode & 0o077 == 0, oct(recovery.stat().st_mode)


# --- a run that succeeded records exactly what it did ----------------------


def test_a_successful_run_commits_one_complete_manifest(harness):
    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert manifest_committed(harness)
    assert [name for name in staged_files(harness) if name.endswith(".staged")] == []
    lines = harness.manifest_lines()
    assert any(line.startswith("root\t") for line in lines), lines
    assert any(line.startswith("entry\t") for line in lines), lines


def test_every_object_the_run_granted_appears_in_the_manifest(harness):
    harness.run()

    recorded = {
        line.split("\t")[1]
        for line in harness.manifest_lines()
        if line.startswith("entry\t")
    }
    for path in granted_entries(harness):
        assert path in recorded, (path, sorted(recorded))


def test_the_manifest_records_the_canonical_path_not_the_open_handle(harness):
    """The handle is the authority; the path is what an operator reads."""

    harness.run()

    for line in harness.manifest_lines():
        if line.startswith(("root\t", "entry\t")):
            assert "/proc/self/fd/" not in line, line
            assert line.split("\t")[1].startswith(str(harness.install_root)), line


def swap_config_after_the_first_recursive_grant(harness):
    """Move the validated source aside and put a new directory at its path.

    The hook fires between the ACL mutation and the identity check that follows
    it, which is the window in which the configured path stops naming the object
    this run changed.
    """

    harness.hook(
        "setfacl",
        "\n".join(
            [
                'case " $* " in *" -R "*) ;; *) exit 0 ;; esac',
                f'[ -e "{harness.root}/swapped" ] && exit 0',
                f': > "{harness.root}/swapped"',
                f'mv "{harness.install_root}/config" "{harness.root}/config-moved"',
                f'mkdir -p "{harness.install_root}/config"',
                "exit 0",
            ]
        ),
    )
    return harness.root / "config-moved"


def test_a_source_swapped_after_validation_is_refused_and_rolled_back(harness):
    """A swap between validation and record-keeping may not survive as a grant."""

    swap_config_after_the_first_recursive_grant(harness)

    result = harness.run()

    assert result.returncode != 0, result.stdout + result.stderr
    assert not manifest_committed(harness), harness.manifest_lines()


def test_a_path_swap_cannot_redirect_the_rollback(harness):
    """The object the run changed is the object the run puts back.

    Rollback follows the handle the ACL was applied through, so the directory
    that was moved aside is restored and the replacement at the configured path
    is left exactly as its creator left it.
    """

    moved = swap_config_after_the_first_recursive_grant(harness)

    result = harness.run()

    assert result.returncode != 0, result.stdout + result.stderr
    assert moved.is_dir(), sorted(item.name for item in harness.root.iterdir())
    for path in (moved, moved / "marker"):
        assert harness.acl_entry(path, BACKUP_USER) is None, (path, harness.acl_state())
        assert harness.acl_entry(path, BACKUP_USER, kind="default") is None, (
            path,
            harness.acl_state(),
        )
    assert granted_entries(harness) == {}, harness.acl_state()


def test_a_path_swap_leaves_the_replacement_directory_untouched(harness):
    """Whoever put a directory at the configured path keeps it as they left it."""

    swap_config_after_the_first_recursive_grant(harness)
    replacement = harness.install_root / "config"

    harness.run()

    assert replacement.is_dir()
    assert harness.acl_entry(replacement, BACKUP_USER) is None, harness.acl_state()
    assert harness.acl_entry(replacement, BACKUP_USER, kind="default") is None, (
        harness.acl_state()
    )
    assert sorted(item.name for item in replacement.iterdir()) == []


def test_a_rollback_that_could_not_reach_the_swapped_object_says_so(harness):
    """An unrestorable grant is recorded, never left for nobody to find."""

    swap_config_after_the_first_recursive_grant(harness)
    harness.environment["EMS_STUB_SETFACL_FAIL_FROM"] = "2"

    harness.run()

    recovery = harness.acl_manifest.parent / "acl-recovery.tsv"
    assert recovery.is_file(), sorted(item.name for item in harness.acl_manifest.parent.iterdir())
    assert "unresolved=" in recovery.read_text(encoding="utf-8")


# --- a commit that could not be made durable is not a commit ----------------


def fail_the_parent_flush(harness):
    """Break the one flush that makes the renamed manifest durable.

    The package-state directory is flushed once when the transaction opens and
    again right after the manifest is renamed into it, so the second match is
    the commit this test is about.
    """

    harness.environment["EMS_STUB_SYNC_FAIL_PATH"] = str(harness.acl_manifest.parent)
    harness.environment["EMS_STUB_SYNC_FAIL_PATH_AT"] = "2"


def manifest_text(harness):
    try:
        return harness.acl_manifest.read_text(encoding="utf-8")
    except OSError:
        return ""


def test_a_commit_that_cannot_be_made_durable_leaves_no_authoritative_manifest(harness):
    """A manifest describing grants the rollback just undid is worse than none."""

    fail_the_parent_flush(harness)

    result = harness.run()

    assert result.returncode != 0, result.stdout + result.stderr
    assert granted_entries(harness) == {}, harness.acl_state()
    assert not manifest_committed(harness), manifest_text(harness)


def test_a_failed_commit_restores_the_previous_manifest_exactly(harness):
    assert harness.run().returncode == 0
    previous = harness.acl_manifest.read_text(encoding="utf-8")
    harness.run("--teardown")
    fail_the_parent_flush(harness)

    result = harness.run()

    assert result.returncode != 0, result.stdout + result.stderr
    assert manifest_committed(harness), sorted(
        item.name for item in harness.acl_manifest.parent.iterdir()
    )
    assert harness.acl_manifest.read_text(encoding="utf-8") == previous


def test_a_failed_commit_never_leaves_a_manifest_describing_withdrawn_grants(harness):
    fail_the_parent_flush(harness)

    harness.run()

    granted = set(granted_entries(harness))
    for line in harness.manifest_lines():
        if line.startswith("entry\t"):
            assert line.split("\t")[1] in granted, line


def test_a_failed_commit_records_the_transaction_state(harness):
    fail_the_parent_flush(harness)

    harness.run()

    assert harness.transaction_state() in ("rollback_complete", "recovery_required"), (
        harness.transaction_state()
    )


def test_a_successful_run_records_a_committed_transaction(harness):
    assert harness.run().returncode == 0

    assert harness.transaction_state() == "committed", harness.transaction_state()


def test_a_manifest_that_could_not_be_withdrawn_is_not_left_under_its_own_name(harness):
    """If the authoritative name cannot be cleared, nothing may be reading it."""

    fail_the_parent_flush(harness)
    harness.environment["EMS_STUB_MV_FAIL_PATH"] = str(harness.acl_manifest)

    result = harness.run()

    assert result.returncode != 0, result.stdout + result.stderr
    assert not manifest_committed(harness), manifest_text(harness)


# --- the manifest and its transaction state are restored as one pair --------
#
# A manifest and the state beside it are one authority. postrm withdraws entries
# only while the state says "committed", so a failed run that restored the
# previous manifest but left the state at rollback_complete has produced a pair
# nobody can act on: the grants are still on the host and no purge may remove
# them. See test_appliance_account_ownership.py for the purge side of this.


def committed_run(harness):
    """One successful run, its binds released the way removal does."""

    assert harness.run().returncode == 0
    manifest = harness.acl_manifest.read_text(encoding="utf-8")
    assert harness.transaction_state() == "committed", harness.transaction_state()
    harness.run("--teardown")
    return manifest


def test_a_failed_commit_restores_the_previous_manifest_and_its_state_together(harness):
    previous = committed_run(harness)
    fail_the_parent_flush(harness)

    result = harness.run()

    assert result.returncode != 0, result.stdout + result.stderr
    assert harness.acl_manifest.read_text(encoding="utf-8") == previous
    assert harness.transaction_state() == "committed", harness.transaction_state()


def test_a_failure_before_the_rename_puts_the_previous_state_back(harness):
    """The manifest was never replaced, but its state was; the pair still breaks."""

    previous = committed_run(harness)
    harness.environment["EMS_STUB_SETFACL_FAIL_AT"] = "2"

    result = harness.run()

    assert result.returncode != 0, result.stdout + result.stderr
    assert harness.acl_manifest.read_text(encoding="utf-8") == previous
    assert harness.transaction_state() == "committed", harness.transaction_state()


def test_a_restored_manifest_keeps_the_mode_it_was_committed_with(harness):
    committed_run(harness)
    mode = harness.acl_manifest.stat().st_mode & 0o777
    fail_the_parent_flush(harness)

    harness.run()

    assert harness.acl_manifest.stat().st_mode & 0o777 == mode


def test_an_incomplete_rollback_never_claims_the_previous_transaction_state(harness):
    """After a rollback that could not finish, "committed" would be a lie."""

    committed_run(harness)
    # Withdraw the grants outside the package, so the next run really has
    # something to put back rather than re-granting what is already there.
    harness.acl_db.write_text("{}", encoding="utf-8")
    force_an_incomplete_rollback(harness)

    result = harness.run()

    assert result.returncode != 0, result.stdout + result.stderr
    assert harness.transaction_state() == "recovery_required", harness.transaction_state()


def test_a_first_run_that_fails_leaves_no_committed_pair(harness):
    """There is no previous authority to restore, so nothing may claim one."""

    fail_the_parent_flush(harness)

    harness.run()

    assert not manifest_committed(harness), manifest_text(harness)
    assert harness.transaction_state() != "committed", harness.transaction_state()


# --- a rollback that cannot record what it left behind reports it -----------


def force_an_incomplete_rollback(harness):
    harness.environment["EMS_STUB_SETFACL_FAIL_FROM"] = "2"


def test_a_recovery_manifest_that_cannot_be_staged_is_reported(harness):
    blocked = harness.root / "blocked-recovery"
    blocked.write_text("a file where the recovery manifest would go\n", encoding="utf-8")
    force_an_incomplete_rollback(harness)

    result = harness.run(EMS_APPLIANCE_ACL_RECOVERY=str(blocked / "acl-recovery.tsv"))

    assert result.returncode != 0, result.stdout + result.stderr
    assert "recovery" in result.stderr.lower(), result.stderr
    assert harness.status()["status"] == "failed", harness.status()


def test_a_recovery_manifest_that_cannot_be_flushed_is_reported(harness):
    force_an_incomplete_rollback(harness)
    harness.environment["EMS_STUB_SYNC_FAIL_PATH"] = f"{harness.acl_recovery}.staged"

    result = harness.run()

    assert result.returncode != 0, result.stdout + result.stderr
    assert "recovery" in result.stderr.lower(), result.stderr


def test_a_recovery_manifest_that_cannot_be_renamed_is_reported(harness):
    force_an_incomplete_rollback(harness)
    harness.environment["EMS_STUB_MV_FAIL_PATH"] = str(harness.acl_recovery)

    result = harness.run()

    assert result.returncode != 0, result.stdout + result.stderr
    assert "recovery" in result.stderr.lower(), result.stderr
    assert not harness.acl_recovery.exists()


def test_a_rollback_that_lost_its_evidence_is_never_reported_as_clean(harness):
    force_an_incomplete_rollback(harness)
    harness.environment["EMS_STUB_MV_FAIL_PATH"] = str(harness.acl_recovery)

    result = harness.run()

    combined = (result.stdout + result.stderr).lower()
    assert "configured" not in harness.status().get("status", ""), harness.status()
    assert "could not" in combined or "cannot" in combined, combined


def test_a_failure_detail_never_makes_the_status_file_unreadable(harness):
    """An unreadable status is indistinguishable from no status at all."""

    force_an_incomplete_rollback(harness)
    harness.environment["EMS_STUB_MV_FAIL_PATH"] = str(harness.acl_recovery)

    harness.run()

    assert harness.status().get("status") == "failed", harness.status_file.read_text(
        encoding="utf-8"
    )


def test_a_recorded_incomplete_rollback_keeps_its_recovery_manifest(harness):
    force_an_incomplete_rollback(harness)

    result = harness.run()

    assert result.returncode != 0, result.stdout + result.stderr
    assert harness.acl_recovery.is_file()
    text = harness.acl_recovery.read_text(encoding="utf-8")
    for field in ("schema=", "installation_id=", "recorded_at=", "unresolved=", "state="):
        assert field in text, (field, text)


# --- a reinstall says what it re-granted -------------------------------------


def reinstall(harness):
    """A second run, with the simulated binds released the way removal does."""

    harness.run("--teardown")
    return harness.run()


def test_a_reinstall_reports_an_operator_change_it_granted_again(harness):
    """The original pre-package state is still what purge restores."""

    assert harness.run().returncode == 0
    granted = harness.install_root / "config" / "marker"
    harness.set_acl(granted, BACKUP_USER, "rwx")

    result = reinstall(harness)

    assert result.returncode == 0, result.stdout + result.stderr
    assert str(granted) in result.stderr, result.stderr
    assert "changed after the last" in result.stderr, result.stderr
    assert harness.status()["status"] == "configured", harness.status()
    assert "re-granted" in harness.status()["detail"], harness.status()


def test_the_manifest_names_the_recorded_installation(harness):
    """One installation, one identifier: the grant belongs to the same install."""

    record = harness.acl_manifest.parent / "backup-account.json"
    record.write_text('{"installation_id": "recorded-installation"}\n', encoding="utf-8")

    assert harness.run().returncode == 0

    header = dict(
        line.split("=", 1)
        for line in harness.acl_manifest.read_text(encoding="utf-8").splitlines()
        if "=" in line and "\t" not in line
    )
    assert header["installation_id"] == "recorded-installation", header


def test_a_reinstall_that_changed_nothing_reports_nothing(harness):
    assert harness.run().returncode == 0

    result = reinstall(harness)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "changed after the last" not in result.stderr, result.stderr
    assert "re-granted" not in (harness.status().get("detail") or ""), harness.status()


def test_the_read_grant_sets_the_mask_that_would_otherwise_cap_it():
    """A named-user ACL is capped by the mask, and the mask is derived from the
    group bits: on the 0600 files the EMS writes its secrets as, a grant that
    leaves the mask alone is present in getfacl and effective for nothing."""

    script = (
        Path(__file__).resolve().parents[1]
        / "packaging" / "appliance" / "bin" / "setup-export-root.sh"
    ).read_text(encoding="utf-8")

    assert 'setfacl -R -m "u:${BACKUP_USER}:rX,m::rX"' in script
    assert 'setfacl -R -d -m "u:${BACKUP_USER}:rX,m::rX"' in script
