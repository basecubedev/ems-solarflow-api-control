# SPDX-License-Identifier: AGPL-3.0-or-later
"""Path boundaries of the read-only SFTP export root.

The export root is built from host paths, and the one thing that must never
happen is that a redirected path turns into an exported one. ``/etc`` reached
through ``/opt/ems-solarflow/config`` is the whole backup boundary gone, so a
refusal has to happen *before* an ACL is granted and before a bind mount is
made, not after.

Every mutating tool is a recording stub here, so "no ACL was granted" and "no
mount was attempted" are observable facts rather than claims. The real kernel
behaviour is proven in ``test_appliance_sftp_confinement.py``.
"""

import os

import pytest

from tests.helpers.appliance_object_identity import object_identity
from tests.helpers.appliance_export_script import (
    BACKUP_USER,
    EXPORT_NAMES,
    EXPORT_SCRIPT,
    ExportScriptHarness,
)

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.backup_restore, pytest.mark.appliance]


@pytest.fixture
def harness(tmp_path):
    host = ExportScriptHarness(tmp_path)
    host.seed_installation()
    host.seed_export_root()
    return host


def mutations(host):
    """Every call that would change permissions or mounts on a host path.

    Writing the appliance's own status file is not a mutation of the export
    boundary, so it does not count here.
    """

    roots = (str(host.install_root), str(host.export_root))
    return [
        line
        for line in host.calls()
        if line.split(" ", 1)[0] in ("setfacl", "mount", "chown", "chmod")
        and any(root in line for root in roots)
    ]


# --- the normal case --------------------------------------------------------


def test_a_normal_installation_is_exported_read_only(harness):
    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    status = harness.status()
    assert status["status"] == "configured", status
    assert {entry["name"] for entry in status["paths"]} == set(EXPORT_NAMES)
    assert all(entry["state"] == "mounted" for entry in status["paths"]), status
    assert all(entry["read_only"] is True for entry in status["paths"]), status


def test_a_source_on_another_filesystem_is_still_exported(harness):
    """A data partition is a mount, not a redirection: it must be allowed."""

    harness.mark_mounted(harness.install_root / "data", options="rw,relatime")

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    data = next(item for item in harness.status()["paths"] if item["name"] == "data")
    assert data["state"] == "mounted", harness.status()


# --- refused sources --------------------------------------------------------


def test_a_source_symlinked_to_etc_is_refused_before_any_mutation(harness):
    outside = harness.outside("etc")
    harness.replace_with_symlink(harness.install_root / "config", outside)

    result = harness.run()

    assert result.returncode != 0
    assert harness.status()["status"] == "failed", harness.status()
    assert not mutations(harness), harness.calls()
    assert not any(str(outside) in line for line in harness.calls())


def test_a_source_symlinked_to_a_sibling_is_refused(harness):
    harness.replace_with_symlink(
        harness.export_source("backups"), harness.install_root / "secrets"
    )

    result = harness.run()

    assert result.returncode != 0
    assert harness.status()["status"] == "failed"
    assert not mutations(harness), harness.calls()


def test_a_symlinked_install_root_is_refused(harness):
    """The documented policy: a separate partition is a mount, not a symlink."""

    real = harness.root / "elsewhere" / "ems"
    (real / "config").mkdir(parents=True)
    harness.replace_with_symlink(harness.install_root, real)

    result = harness.run()

    assert result.returncode != 0
    assert harness.status()["status"] == "failed", harness.status()
    assert not mutations(harness), harness.calls()


def test_the_refusal_names_the_export_and_not_the_redirected_host_path(harness):
    outside = harness.outside("etc")
    harness.replace_with_symlink(harness.install_root / "config", outside)

    result = harness.run()

    status = harness.status()
    assert "config" in status["detail"]
    assert str(outside) not in status["detail"], status
    assert str(outside) not in result.stderr, result.stderr


def test_a_source_replaced_after_validation_is_refused_before_it_is_mounted(harness):
    """The window between the ACL grant and the bind mount is re-verified."""

    outside = harness.outside("etc")
    harness.hook(
        "setfacl",
        f'rm -rf "{harness.install_root}/config" && '
        f'ln -s "{outside}" "{harness.install_root}/config"',
    )

    result = harness.run()

    assert result.returncode != 0
    assert not harness.called("mount"), harness.calls()
    assert harness.status()["status"] in ("failed", "degraded"), harness.status()


# --- the configured roots themselves ----------------------------------------


def test_an_export_root_below_a_symlinked_parent_is_refused_before_it_is_created(tmp_path):
    """A parent that redirects elsewhere must be refused before mkdir.

    The export root does not exist yet, so nothing stops the script from
    creating it — except the policy that every existing parent component is a
    real directory. Following the redirect would chown, chmod and ACL a
    directory the operator never named.
    """

    host = ExportScriptHarness(tmp_path)
    host.seed_installation()
    outside = host.outside("outside")
    (host.root / "srv").mkdir(parents=True, exist_ok=True)
    (host.root / "srv" / "redirect").symlink_to(outside)
    host.export_root = host.root / "srv" / "redirect" / "ems-export"

    result = host.run(EMS_APPLIANCE_EXPORT_ROOT=str(host.export_root))

    assert result.returncode != 0
    assert not (outside / "ems-export").exists(), sorted(outside.iterdir())
    assert not mutations(host), host.calls()
    assert host.status()["status"] == "failed", host.status()


def test_an_existing_export_root_below_a_symlinked_parent_is_refused(tmp_path):
    host = ExportScriptHarness(tmp_path)
    host.seed_installation()
    outside = host.outside("outside")
    (outside / "ems-export").mkdir(parents=True, exist_ok=True)
    (host.root / "srv").mkdir(parents=True, exist_ok=True)
    (host.root / "srv" / "redirect").symlink_to(outside)
    host.export_root = host.root / "srv" / "redirect" / "ems-export"

    result = host.run(EMS_APPLIANCE_EXPORT_ROOT=str(host.export_root))

    assert result.returncode != 0
    assert not mutations(host), host.calls()


def test_an_install_root_inside_the_export_root_is_refused(harness):
    """The chroot would otherwise publish the whole installation as /ems."""

    inside = harness.export_root / "ems"
    for name in EXPORT_NAMES:
        (inside / name).mkdir(parents=True, exist_ok=True)

    result = harness.run(EMS_APPLIANCE_INSTALL_ROOT=str(inside))

    assert result.returncode != 0
    assert not mutations(harness), harness.calls()


def test_identical_roots_are_refused(harness):
    result = harness.run(EMS_APPLIANCE_INSTALL_ROOT=str(harness.export_root))

    assert result.returncode != 0
    assert not mutations(harness), harness.calls()


# --- the export root is an exclusive managed boundary ------------------------


def test_an_unexpected_file_in_the_export_root_stops_activation(harness):
    """The account sees the chroot root, so an unmanaged entry is visible."""

    (harness.export_root / "host-note.txt").write_text("operator note\n", encoding="utf-8")

    result = harness.run()

    assert result.returncode != 0
    status = harness.status()
    assert status["status"] == "failed", status
    assert "host-note.txt" in status["detail"], status
    assert not harness.called("mount"), harness.calls()


def test_an_unexpected_directory_in_the_export_root_stops_activation(harness):
    (harness.export_root / "private").mkdir()

    result = harness.run()

    assert result.returncode != 0
    assert harness.status()["status"] == "failed", harness.status()


def test_a_hidden_file_in_the_export_root_stops_activation(harness):
    (harness.export_root / ".operator-key").write_text("secret\n", encoding="utf-8")

    result = harness.run()

    assert result.returncode != 0
    assert harness.status()["status"] == "failed", harness.status()


def test_a_symlink_in_the_export_root_stops_activation(harness):
    (harness.export_root / "shortcut").symlink_to(harness.outside("etc"))

    result = harness.run()

    assert result.returncode != 0
    assert harness.status()["status"] == "failed", harness.status()


def test_unexpected_content_is_reported_but_never_deleted(harness):
    note = harness.export_root / "host-note.txt"
    note.write_text("operator note\n", encoding="utf-8")

    harness.run()

    assert note.is_file(), "the export setup must never delete operator content"


# --- refused targets --------------------------------------------------------


def test_a_symlinked_export_target_is_refused(harness):
    outside = harness.outside("etc")
    harness.replace_with_symlink(harness.export_root / "config", outside)

    result = harness.run()

    assert result.returncode != 0
    assert harness.status()["status"] == "failed", harness.status()
    assert not mutations(harness), harness.calls()


def test_a_symlinked_export_root_is_refused(harness):
    outside = harness.outside("etc")
    harness.replace_with_symlink(harness.export_root, outside)

    result = harness.run()

    assert result.returncode != 0
    assert not mutations(harness), harness.calls()


def test_a_target_mounted_from_the_wrong_source_is_not_reported_as_exported(harness):
    """A pre-existing mount is replaced or refused, never adopted."""

    outside = harness.outside("etc")
    harness.mark_mounted(harness.export_root / "config", options="ro,relatime")
    harness.environment["EMS_STUB_BIND_SOURCE"] = str(outside)

    result = harness.run()

    config = next(item for item in harness.status()["paths"] if item["name"] == "config")
    assert config["state"] != "mounted", harness.status()
    assert result.returncode != 0


def test_a_bind_that_cannot_be_made_read_only_is_not_left_mounted(harness):
    harness.environment["EMS_STUB_REMOUNT_RC"] = "32"

    result = harness.run()

    assert result.returncode != 0
    assert harness.called("umount"), harness.calls()
    assert all(item["state"] != "mounted" for item in harness.status()["paths"])


def test_a_foreign_mount_is_revalidated_after_it_has_been_unmounted(harness):
    """Unmounting reveals the real target; it must be checked again, not used."""

    outside = harness.outside("etc")
    target = harness.export_root / "config"
    for entry in sorted(target.rglob("*"), reverse=True):
        entry.unlink() if not entry.is_dir() else entry.rmdir()
    target.rmdir()
    target.symlink_to(outside)
    harness.mark_mounted(target, options="ro,relatime")

    result = harness.run()

    assert result.returncode != 0
    assert target.is_symlink(), "the hidden symlink must survive untouched"
    assert not any(
        line.startswith(("chown", "chmod", "setfacl")) and str(outside) in line
        for line in harness.calls()
    ), harness.calls()


# --- source identity during ACL preparation ---------------------------------


def test_the_read_acl_is_applied_through_the_validated_directory_handle(harness):
    """The recursive ACL walk must not follow the path a second time."""

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    acl_calls = harness.called("setfacl")
    recursive = [line for line in acl_calls if " -R " in f" {line} "]
    assert recursive, acl_calls
    assert all("/proc/self/fd/" in line for line in recursive), acl_calls


# --- status output ----------------------------------------------------------


def test_the_status_file_is_root_only_and_leaves_no_partial_file(harness):
    harness.run()

    assert harness.status_file.is_file()
    assert harness.status_file.stat().st_mode & 0o777 == 0o600
    leftovers = [item.name for item in harness.status_file.parent.iterdir() if item.name.startswith(".")]
    assert not leftovers, leftovers


# --- unified host configuration --------------------------------------------


def test_the_script_uses_the_canonical_host_path_variables(harness):
    """Python and the shell tooling must not read different variable names."""

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    status = harness.status()
    assert status["root"] == str(harness.install_root), status
    assert status["export_root"] == str(harness.export_root), status


def test_only_one_run_may_hold_the_export_root(harness):
    """The watcher, the postinst and an operator can all start a run at once."""

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert (harness.stub_dir / "export.lock").exists(), sorted(
        item.name for item in harness.stub_dir.iterdir()
    )
    assert "flock" in EXPORT_SCRIPT.read_text(encoding="utf-8")


# --- the ACL manifest -------------------------------------------------------


def manifest_roots(harness):
    return {
        line.split("\t")[1]: line.split("\t")
        for line in harness.manifest_lines()
        if line.startswith("root\t")
    }


def manifest_entries(harness):
    """Every recorded ``(object, scope)`` line, keyed for lookup."""

    return {
        (fields[1], fields[3]): fields
        for fields in (line.split("\t") for line in harness.manifest_lines())
        if fields[0] == "entry"
    }


def test_every_granted_acl_entry_is_recorded_with_its_object_identity(harness):
    harness.run()

    roots = manifest_roots(harness)
    entries = manifest_entries(harness)
    assert str(harness.install_root) in roots, harness.manifest_lines()
    for name in EXPORT_NAMES:
        source = str(harness.export_source(name))
        assert source in roots, harness.manifest_lines()
        assert roots[source][3] == "recursive"
        identity = object_identity(source)
        assert roots[source][2] == identity
        record = entries.get((source, "access"))
        assert record, harness.manifest_lines()
        assert record[2] == identity, record


def test_a_recorded_identity_is_more_than_a_device_and_inode(harness):
    """Device and inode are reusable; what is recorded may not be."""

    harness.run()

    roots = manifest_roots(harness)
    source = str(harness.install_root / "config")
    fields = roots[source][2].split(":")
    entry = os.stat(source)
    assert fields[0] == str(entry.st_dev), roots[source]
    assert fields[1] == str(entry.st_ino), roots[source]
    assert fields[2] == "directory", roots[source]
    assert fields[3] == str(entry.st_uid), roots[source]
    assert fields[4] == str(entry.st_gid), roots[source]
    assert ":".join(fields[5:]), "the identity carries no generation signal"


def test_a_recursive_grant_records_every_object_it_changed(harness):
    """The subtree root alone cannot say which descendants were touched."""

    harness.run()

    entries = manifest_entries(harness)
    marker = str(harness.install_root / "config" / "marker")
    assert (marker, "access") in entries, harness.manifest_lines()
    assert entries[(marker, "access")][4] == "no", entries[(marker, "access")]


def test_an_acl_entry_that_predates_the_installation_is_recorded_as_preserved(harness):
    operator = harness.install_root / "config" / "operator.json"
    operator.write_text("{}\n", encoding="utf-8")
    harness.set_acl(operator, BACKUP_USER, "rwx")

    harness.run()

    record = manifest_entries(harness).get((str(operator), "access"))
    assert record, harness.manifest_lines()
    assert record[4] == "yes", record
    assert record[5] == "rwx", record


def test_the_manifest_declares_the_schema_its_reader_requires(harness):
    harness.run()

    lines = harness.manifest_lines()
    assert "schema=3" in lines, lines
    assert f"user={BACKUP_USER}" in lines, lines
    for line in lines:
        fields = line.split("\t")
        if fields[0] == "entry":
            assert len(fields) == 7, fields
            assert all(field for field in fields), fields


def test_a_second_run_does_not_record_its_own_grants_as_pre_existing(harness):
    operator = harness.install_root / "config" / "operator.json"
    operator.write_text("{}\n", encoding="utf-8")
    harness.set_acl(operator, BACKUP_USER, "rwx")
    assert harness.run().returncode == 0
    first = manifest_entries(harness)

    harness.run("--teardown")
    assert harness.run().returncode == 0

    second = manifest_entries(harness)
    assert second.keys() == first.keys(), (sorted(first), sorted(second))
    for key, record in second.items():
        assert record[4:6] == first[key][4:6], (key, record, first[key])
    assert second[(str(operator), "access")][5] == "rwx", second[(str(operator), "access")]
