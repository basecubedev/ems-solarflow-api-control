# SPDX-License-Identifier: AGPL-3.0-or-later
"""The half of an object identity that optional tooling contributes.

Device, inode, type, owner, group and a generation come from stat(1) and are
readable wherever this package runs. The inode generation number ext4 keeps is
not: it needs lsattr, which this package does not depend on and which no
filesystem is obliged to answer.

That asymmetry decides the contract. The mandatory half carries every match. The
optional half only ever strengthens one or refuses it, because whether lsattr is
installed on the host that purges is not a fact about the object that was
granted an ACL — and a cleanup that became impossible when a tool was removed
would leave this package's own ACL entries behind for good.
"""

import pytest

from tests.helpers.appliance_backup_account import BACKUP_USER, BackupAccountHarness

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.backup_restore]


@pytest.fixture
def host(tmp_path):
    return BackupAccountHarness(tmp_path)


def manifest_header(host, key):
    for line in host.acl_manifest.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        if name == key:
            return value
    return ""


def stub_lsattr(host, body):
    return host.stub_command("lsattr", body)


def test_a_host_without_lsattr_still_identifies_every_object(monkeypatch, host):
    """Tool availability is a property of the host, never of the object."""

    monkeypatch.setattr(
        "tests.helpers.appliance_object_identity.inode_version", lambda path: ""
    )
    from tests.helpers import appliance_object_identity

    identity = appliance_object_identity.object_identity(host.root)

    assert identity, identity
    assert ":v" not in identity, identity
    assert len(identity.split(":")) >= 6, identity


def test_the_optional_signal_is_recorded_where_it_can_be_read(monkeypatch, host):
    monkeypatch.setattr(
        "tests.helpers.appliance_object_identity.inode_version", lambda path: "4242"
    )
    from tests.helpers import appliance_object_identity

    identity = appliance_object_identity.object_identity(host.root)

    assert identity.endswith(":v4242"), identity


def test_a_missing_lsattr_never_crashes_the_identity_helper(monkeypatch, host):
    def absent(*arguments, **keywords):
        raise FileNotFoundError("lsattr")

    monkeypatch.setattr("subprocess.run", absent)
    from tests.helpers import appliance_object_identity

    assert appliance_object_identity.inode_version(host.root) == ""


def acl_tree(host):
    tree = host.install_root / "config"
    tree.mkdir(parents=True, exist_ok=True)
    granted = tree / "config.json"
    granted.write_text("{}\n", encoding="utf-8")
    host.set_acl(granted, BACKUP_USER, "r--")
    host.run("ensure")
    return tree, granted


def test_an_entry_recorded_with_a_generation_is_still_withdrawn_without_one(host):
    """A purge on a host that lost lsattr may still finish what it started."""

    tree, granted = acl_tree(host)
    core = host.object_identity(granted).rsplit(":v", 1)[0]
    host.write_acl_manifest(
        entries=[(granted, "access", None, "r--")],
        roots=[(tree, "recursive")],
        identities={granted: f"{core}:v99"},
    )
    stub_lsattr(host, "exit 127")

    host.run_postrm("purge")

    assert host.acl_entry(granted, BACKUP_USER) is None, host.acl_state()


def test_a_conflicting_generation_preserves_the_entry(host):
    """Two generations that disagree describe two objects, so nothing is withdrawn."""

    tree, granted = acl_tree(host)
    core = host.object_identity(granted).rsplit(":v", 1)[0]
    host.write_acl_manifest(
        entries=[(granted, "access", None, "r--")],
        roots=[(tree, "recursive")],
        identities={granted: f"{core}:v1"},
    )
    stub_lsattr(host, "printf '%s ---------------- %s\\n' 2 \"$3\"")

    result = host.run_postrm("purge")

    assert host.acl_entry(granted, BACKUP_USER) == "r--", host.acl_state()
    assert "replaced" in result.stderr, result.stderr


def test_the_manifest_declares_which_optional_signals_it_recorded(tmp_path):
    from tests.helpers.appliance_export_script import ExportScriptHarness

    export = ExportScriptHarness(tmp_path / "export")
    export.seed_installation()

    assert export.run().returncode == 0
    declared = ""
    for line in export.acl_manifest.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        if name == "optional_identity":
            declared = value
    assert declared in ("inode_generation", "none"), declared
