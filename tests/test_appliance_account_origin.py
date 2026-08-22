# SPDX-License-Identifier: AGPL-3.0-or-later
"""How a flashed appliance establishes ownership of the account in its image.

The A/B image bakes the backup account into ``/etc/passwd`` at build time,
because ``/etc`` is read-only and slot-local once the device runs. Everything
that proves the package created it -- the ownership record and the home marker
-- lives on the shared partition, which is empty on the first boot and hides
whatever the build chroot wrote there. So the first boot sees an account it
cannot account for, and refuses it. Correctly: that is what a foreign account
looks like too.

The image therefore also carries a slot-local origin declaration: read-only at
runtime, written where the shared mounts cannot reach it, naming the exact
account the build created. The first boot may use it to bind the record and the
marker to the home that is mounted *now* -- and nothing else.

An extra trust artefact in a fail-closed identity machine is only worth having
if it cannot be borrowed. These tests are about the borrowing.
"""

import pytest

from appliance import backup_ownership
from tests.helpers.appliance_backup_account import BACKUP_USER, BackupAccountHarness

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.backup_restore, pytest.mark.appliance]

SHIPPED_SHELL = "/usr/sbin/nologin"
SHIPPED_UID = 1500


@pytest.fixture
def host(tmp_path):
    return BackupAccountHarness(tmp_path)


@pytest.fixture
def flashed(host):
    """The state a freshly flashed appliance is in on its very first boot."""

    home = host.add_account(shell=SHIPPED_SHELL, uid=SHIPPED_UID)
    home.chmod(0o755)
    host.write_origin()
    return host


def test_the_state_this_is_about_is_the_one_that_used_to_refuse(host):
    """Without the declaration, the shipped account is indistinguishable from
    somebody else's -- and is still refused. The baseline this builds on."""

    host.add_account(shell=SHIPPED_SHELL, uid=SHIPPED_UID)

    refused = host.run("ensure")

    assert refused.returncode != 0
    assert "was not created by this package" in refused.stderr
    assert not host.marker.exists()


def test_a_flashed_appliance_adopts_the_account_its_image_shipped(flashed):
    adopted = flashed.run("ensure")

    assert adopted.returncode == 0, adopted.stderr
    assert flashed.run("ownership-state").stdout.strip() == "current"


def test_adoption_binds_the_home_that_is_mounted_now(flashed):
    """The chroot's record named a directory on the build filesystem. The point
    of doing this at boot is that the shared mount has a different identity."""

    flashed.run("ensure")

    record = flashed.record()
    mounted = flashed.home.stat()
    assert record["home_device"] == str(mounted.st_dev)
    assert record["home_inode"] == str(mounted.st_ino)
    assert flashed.home_marker().is_file()


def test_the_record_names_the_declaration_it_acted_on(flashed):
    """An adopted record has to say so, or the next reader cannot tell an
    account this package created from one it merely inherited."""

    flashed.run("ensure")

    assert flashed.record()["origin_nonce"] == flashed.origin_nonce


def test_adoption_creates_no_account(flashed):
    """The whole reason the first design was withdrawn: ``adduser`` writes
    ``/etc/passwd``, and on this device that filesystem is read-only."""

    flashed.run("ensure")

    assert not any(call.startswith("adduser") for call in flashed.calls())


@pytest.mark.parametrize(
    "field, value",
    [
        ("uid", "1600"),
        ("primary_gid", "1600"),
        ("home", "/var/lib/somewhere-else"),
        ("shell", "/bin/sh"),
    ],
)
def test_an_account_the_declaration_does_not_describe_cannot_claim_it(host, field, value):
    """One field is enough. The declaration describes one exact account."""

    host.add_account(shell=SHIPPED_SHELL, uid=SHIPPED_UID)
    host.write_origin(**{field: value})

    refused = host.run("ensure")

    assert refused.returncode != 0
    assert not host.marker.exists()


def test_a_declaration_for_a_different_account_is_not_this_one(host):
    host.add_account(shell=SHIPPED_SHELL, uid=SHIPPED_UID)
    host.write_origin(account="someone-else")

    refused = host.run("ensure")

    assert refused.returncode != 0
    assert not host.marker.exists()


def test_a_declaration_of_an_unknown_schema_is_not_read(host):
    host.add_account(shell=SHIPPED_SHELL, uid=SHIPPED_UID)
    host.write_origin(schema_version="99")

    refused = host.run("ensure")

    assert refused.returncode != 0
    assert not host.marker.exists()


def test_a_declaration_anyone_could_have_written_is_refused(flashed):
    """It is only trustworthy because it is part of a read-only image. A mode
    that lets the world write it is the case where that stopped being true."""

    flashed.origin.chmod(0o666)

    refused = flashed.run("ensure")

    assert refused.returncode != 0
    assert not flashed.marker.exists()


def test_a_declaration_reached_through_a_symbolic_link_is_refused(host, tmp_path):
    host.add_account(shell=SHIPPED_SHELL, uid=SHIPPED_UID)
    host.write_origin()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / host.origin.name).write_text(
        host.origin.read_text(encoding="utf-8"), encoding="utf-8"
    )
    host.origin.unlink()
    host.origin.symlink_to(elsewhere / host.origin.name)

    refused = host.run("ensure")

    assert refused.returncode != 0
    assert not host.marker.exists()


def test_a_home_that_already_holds_something_is_never_adopted(flashed):
    """The declaration says which account. It says nothing about whose files
    are in the home, and adoption may not decide that question."""

    stray = flashed.home / "authorized_keys.backup"
    stray.write_text("ssh-ed25519 AAAA somebody\n", encoding="utf-8")

    refused = flashed.run("ensure")

    assert refused.returncode != 0
    assert stray.read_text(encoding="utf-8") == "ssh-ed25519 AAAA somebody\n"
    assert not flashed.marker.exists()


def test_a_conflicting_record_is_not_overruled_by_the_declaration(flashed):
    """The declaration answers "there is no record". It is not a second opinion
    about a record that is there and does not match."""

    flashed.write_record(uid=4242)

    refused = flashed.run("ensure")

    assert refused.returncode != 0
    assert "no longer the account this package created" in refused.stderr


def test_a_second_run_binds_nothing_new(flashed):
    flashed.run("ensure")
    first = flashed.marker.read_text(encoding="utf-8")
    first_marker = flashed.home_marker().read_text(encoding="utf-8")

    again = flashed.run("ensure")

    assert again.returncode == 0, again.stderr
    assert flashed.marker.read_text(encoding="utf-8") == first
    assert flashed.home_marker().read_text(encoding="utf-8") == first_marker


def test_the_declaration_cannot_be_spent_twice(flashed):
    """Deleting the record must not hand the home back. By then the marker is
    in it, and a home with content is somebody's."""

    flashed.run("ensure")
    flashed.marker.unlink()

    refused = flashed.run("ensure")

    assert refused.returncode != 0
    assert not flashed.marker.exists()


def test_creating_the_account_leaves_the_declaration_the_next_boot_needs(host):
    """The build chroot runs exactly this path. What it writes here is the only
    thing that reaches the device, because everything else it writes is on a
    filesystem the image does not carry."""

    created = host.run("ensure")

    assert created.returncode == 0, created.stderr
    assert host.origin.is_file()
    values = backup_ownership.read_declaration(host.origin)
    assert values["account"] == BACKUP_USER
    assert values["uid"] == host.account_field(2)
    assert values["primary_gid"] == host.account_field(3)
    assert values["home"] == str(host.home)
    assert values["shell"] == host.account_field(6)
    assert values["nonce"]


def test_the_declaration_is_not_world_writable_when_it_is_written(host):
    host.run("ensure")

    assert host.origin.stat().st_mode & 0o022 == 0
