# SPDX-License-Identifier: AGPL-3.0-or-later
"""What has to be true before backup access may be called active.

The sshd policy and the mount table are only two of the preconditions. Without
the exact package-owned account there is nothing to confine, without a key there
is nothing to authenticate, and a path chain that grew a symbolic link after the
installation publishes a directory nobody validated. Each of those has to end in
``unavailable`` or ``degraded`` with a named reason — never in ``active``.
"""

import os

import pytest

from appliance.backup_confinement import (
    STATE_ACTIVE,
    BackupAccessActivation,
)
from appliance.validation import ValidationError
from tests.helpers.appliance import (
    SSHD_BACKUP_MATCH,
    build_test_services,
    seed_backup_account,
)

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.backup_restore, pytest.mark.appliance]

PUBLIC_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIH1cQ0kFvL5gLIQ0Q0mV3P6pC5J2Xw5RIu5Hn3fJ0hVb backup\n"
)

# The policy the appliance generates, as sshd -T reports it back.
COMPLIANT = SSHD_BACKUP_MATCH


def appliance(tmp_path, *, account=True, key=PUBLIC_KEY, record=True, uid=1500, record_uid=None):
    services = build_test_services(tmp_path)
    seed_backup_account(
        services,
        home=tmp_path / "var" / "lib" / "ems-backup",
        key=key,
        uid=uid,
        record_uid=record_uid,
        account=account,
        record=record,
    )
    services.host.sshd_backup_match = COMPLIANT.format(export_root=services.paths.export_root)
    return services


def activation(services):
    return BackupAccessActivation(
        runner=services.runner,
        config=services.config,
        paths=services.paths,
        systemd=services.systemd,
        probe=services.probe,
    )


# --- the account is a precondition, not a detail ----------------------------


def test_a_confined_chroot_without_the_account_is_not_active(tmp_path):
    services = appliance(tmp_path, account=False)

    report = activation(services).activate()

    assert report["state"] != STATE_ACTIVE, report
    assert "account" in report["reason"], report


def test_an_account_the_package_does_not_own_is_not_active(tmp_path):
    services = appliance(tmp_path, uid=4242, record_uid=1500)

    report = activation(services).activate()

    assert report["state"] != STATE_ACTIVE, report
    assert "identity" in report["reason"] or "ownership" in report["reason"], report


def test_an_account_without_an_ownership_record_is_not_active(tmp_path):
    services = appliance(tmp_path, record=False)

    report = activation(services).activate()

    assert report["state"] != STATE_ACTIVE, report


def test_backup_access_without_any_key_is_not_active(tmp_path, production_chroot_chain):
    services = appliance(tmp_path, key="")

    report = activation(services).activate()

    assert report["state"] != STATE_ACTIVE, report
    assert "key" in report["reason"], report


def test_a_key_the_package_cannot_attribute_is_not_activated(tmp_path):
    services = appliance(tmp_path)
    (services.home / ".ssh" / "authorized_keys").write_text(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKh0Jw8h2p5Vp0J0Z7T2y6cE1x3n5aX9r7Q0m1s2t3u4 x\n",
        encoding="utf-8",
    )

    report = activation(services).activate()

    assert report["state"] != STATE_ACTIVE, report
    assert report["authentication_disabled"] is True, report


def plant_key_file_symlink(services, tmp_path, text):
    """Replace the key file with a symlink to a file elsewhere that holds ``text``."""

    keys = services.home / ".ssh" / "authorized_keys"
    elsewhere = tmp_path / "elsewhere-keys"
    elsewhere.write_text(text, encoding="utf-8")
    keys.unlink()
    keys.symlink_to(elsewhere)
    return keys


def test_a_key_file_planted_as_a_symlink_is_not_activated_through(
    tmp_path, production_chroot_chain
):
    services = appliance(tmp_path)
    keys = plant_key_file_symlink(services, tmp_path, PUBLIC_KEY)

    report = activation(services).activate()

    assert report["state"] != STATE_ACTIVE, report
    assert report["reason"] == "authorized_keys_unsafe", report
    assert report["authentication_disabled"] is True, report
    assert not keys.is_symlink(), "the planted link was left where sshd reads keys"


def test_the_key_check_does_not_read_a_planted_symlink_as_root(tmp_path):
    services = appliance(tmp_path)
    plant_key_file_symlink(services, tmp_path, "root:$6$hash:20000::::::\n")

    with pytest.raises(ValidationError) as refused:
        activation(services).authorized_keys()

    assert refused.value.code == "authorized_keys_unsafe"


def test_the_key_check_does_not_read_through_a_symlinked_ssh_directory(tmp_path):
    services = appliance(tmp_path)
    ssh_dir = services.home / ".ssh"
    elsewhere = tmp_path / "elsewhere-ssh"
    ssh_dir.rename(elsewhere)
    ssh_dir.symlink_to(elsewhere)

    with pytest.raises(ValidationError) as refused:
        activation(services).authorized_keys()

    assert refused.value.code == "ssh_directory_unsafe"


# --- disable and restore move key files without following a symlink ----------


DISABLED_NAME = "authorized_keys.disabled-by-appliance"


def plant_ssh_directory_symlink(services, tmp_path, names):
    """Make ``.ssh`` a symlink to a directory elsewhere holding ``names``."""

    ssh_dir = services.home / ".ssh"
    elsewhere = tmp_path / "elsewhere-ssh"
    elsewhere.mkdir()
    for item in ssh_dir.iterdir():
        item.unlink()
    ssh_dir.rmdir()
    ssh_dir.symlink_to(elsewhere)
    for name in names:
        (elsewhere / name).write_text(PUBLIC_KEY, encoding="utf-8")
    return elsewhere


def chage_calls(services, value):
    return [call for call in services.host.calls if call[0] == "chage" and call[1][:2] == ("-E", value)]


def test_disabling_through_a_symlinked_ssh_directory_moves_nothing_and_expires_the_account(tmp_path):
    services = appliance(tmp_path)
    elsewhere = plant_ssh_directory_symlink(services, tmp_path, ["authorized_keys"])

    outcome = activation(services).disable(reason="requested")

    assert sorted(item.name for item in elsewhere.iterdir()) == ["authorized_keys"]
    assert (elsewhere / "authorized_keys").read_text(encoding="utf-8") == PUBLIC_KEY
    assert (services.home / ".ssh").is_symlink()
    assert outcome["keys_refused"] == "ssh_directory_unsafe", outcome
    assert outcome["authentication_disabled"] is True, outcome
    assert chage_calls(services, "1"), services.host.calls


def test_restoring_through_a_symlinked_ssh_directory_is_refused(tmp_path):
    services = appliance(tmp_path)
    elsewhere = plant_ssh_directory_symlink(services, tmp_path, [DISABLED_NAME])

    with pytest.raises(ValidationError) as refused:
        activation(services).restore()

    assert refused.value.code == "ssh_directory_unsafe"
    assert sorted(item.name for item in elsewhere.iterdir()) == [DISABLED_NAME]
    assert not chage_calls(services, "-1"), services.host.calls


def test_activation_through_a_symlinked_ssh_directory_leaves_its_target_alone(
    tmp_path, production_chroot_chain
):
    services = appliance(tmp_path)
    elsewhere = plant_ssh_directory_symlink(services, tmp_path, ["authorized_keys"])

    report = activation(services).activate()

    assert report["state"] != STATE_ACTIVE, report
    assert report["reason"] == "ssh_directory_unsafe", report
    assert report["authentication_disabled"] is True, report
    assert sorted(item.name for item in elsewhere.iterdir()) == ["authorized_keys"]
    assert not chage_calls(services, "-1"), services.host.calls


def test_a_conflict_in_a_symlinked_ssh_directory_s_target_is_refused_as_the_link(
    tmp_path, production_chroot_chain
):
    """The conflict gate lists ``.ssh`` without following it, as every other step does."""

    services = appliance(tmp_path)
    conflict = DISABLED_NAME + ".conflict"
    elsewhere = plant_ssh_directory_symlink(services, tmp_path, ["authorized_keys", conflict])

    report = activation(services).activate()

    assert report["reason"] == "ssh_directory_unsafe", report
    assert report["keys_conflicted"] == [], report
    assert report["authentication_disabled"] is True, report
    assert sorted(item.name for item in elsewhere.iterdir()) == ["authorized_keys", conflict]
    assert not chage_calls(services, "-1"), services.host.calls


def test_the_status_names_a_symlinked_ssh_directory_instead_of_listing_its_target(tmp_path):
    services = appliance(tmp_path)
    plant_ssh_directory_symlink(
        services, tmp_path, ["authorized_keys", DISABLED_NAME + ".conflict"]
    )

    observed = activation(services).observe()

    assert observed["keys_conflicted"] == []
    assert observed["keys_refused"] == "ssh_directory_unsafe"


def test_the_status_reads_no_key_file_through_a_symlinked_ssh_directory(tmp_path):
    services = appliance(tmp_path)
    plant_ssh_directory_symlink(services, tmp_path, ["authorized_keys", DISABLED_NAME])

    observed = activation(services).observe()

    assert observed["keys_present"] is False, observed
    assert observed["keys_disabled"] is False, observed
    assert observed["keys_refused"] == "ssh_directory_unsafe", observed


def test_the_activation_report_reads_no_key_file_through_a_symlinked_ssh_directory(
    tmp_path, production_chroot_chain
):
    services = appliance(tmp_path)
    plant_ssh_directory_symlink(services, tmp_path, ["authorized_keys", DISABLED_NAME])

    report = activation(services).activate()

    assert report["reason"] == "ssh_directory_unsafe", report
    assert report["keys_present"] is False, report
    assert report["keys_preserved"] is False, report


@pytest.mark.parametrize(
    ("name", "flag"), [("authorized_keys", "keys_present"), (DISABLED_NAME, "keys_disabled")]
)
def test_the_status_takes_a_key_file_planted_as_a_symlink_for_no_key(tmp_path, name, flag):
    services = appliance(tmp_path)
    ssh_dir = services.home / ".ssh"
    elsewhere = tmp_path / "elsewhere-keys"
    elsewhere.write_text(PUBLIC_KEY, encoding="utf-8")
    (ssh_dir / "authorized_keys").unlink()
    (ssh_dir / name).symlink_to(elsewhere)

    observed = activation(services).observe()

    assert observed[flag] is False, observed


def test_a_preserved_key_file_planted_as_a_symlink_is_not_restored(tmp_path):
    services = appliance(tmp_path)
    ssh_dir = services.home / ".ssh"
    elsewhere = tmp_path / "elsewhere-keys"
    elsewhere.write_text(PUBLIC_KEY, encoding="utf-8")
    (ssh_dir / "authorized_keys").unlink()
    (ssh_dir / DISABLED_NAME).symlink_to(elsewhere)

    with pytest.raises(ValidationError) as refused:
        activation(services).restore()

    assert refused.value.code == "authorized_keys_unsafe"
    assert not os.path.lexists(ssh_dir / "authorized_keys")
    assert (ssh_dir / DISABLED_NAME).is_symlink()
    assert not chage_calls(services, "-1"), services.host.calls


def test_a_key_file_planted_as_a_symlink_is_not_restored_over(tmp_path):
    services = appliance(tmp_path)
    keys = plant_key_file_symlink(services, tmp_path, PUBLIC_KEY)

    with pytest.raises(ValidationError) as refused:
        activation(services).restore()

    assert refused.value.code == "authorized_keys_unsafe"
    assert keys.is_symlink()
    assert not chage_calls(services, "-1"), services.host.calls


def test_restoring_leaves_a_live_key_file_in_place_beside_a_preserved_one(tmp_path):
    """The key file sshd reads is the operator's now; the preserved one is not put over it."""

    services = appliance(tmp_path)
    ssh_dir = services.home / ".ssh"
    operator_keys = "# the operator's own file\n"
    (ssh_dir / DISABLED_NAME).write_text(PUBLIC_KEY, encoding="utf-8")
    (ssh_dir / "authorized_keys").write_text(operator_keys, encoding="utf-8")

    activation(services).restore()

    assert (ssh_dir / "authorized_keys").read_text(encoding="utf-8") == operator_keys
    assert (ssh_dir / DISABLED_NAME).read_text(encoding="utf-8") == PUBLIC_KEY


def test_a_dangling_key_file_symlink_is_moved_out_of_sshds_reach(tmp_path):
    services = appliance(tmp_path)
    keys = services.home / ".ssh" / "authorized_keys"
    keys.unlink()
    keys.symlink_to(tmp_path / "not-there-yet")

    outcome = activation(services).disable(reason="requested")

    assert not os.path.lexists(keys)
    assert (keys.parent / DISABLED_NAME).is_symlink()
    assert outcome["authentication_disabled"] is True, outcome
    assert not (tmp_path / "not-there-yet").exists()


def test_an_account_that_cannot_be_un_expired_is_not_active(tmp_path):
    services = appliance(tmp_path)
    services.host.fail_command("chage")

    report = activation(services).activate()

    assert report["state"] != STATE_ACTIVE, report


def test_a_complete_installation_is_active(tmp_path, production_chroot_chain):
    services = appliance(tmp_path)

    report = activation(services).activate()

    assert report["state"] == STATE_ACTIVE, report
    assert report["keys_present"] is True, report


# --- runtime path drift -----------------------------------------------------


def redirect_parent(services):
    """Turn the EMS installation root into a symlink, after the installation."""

    install_root = services.paths.install_root
    elsewhere = services.paths.install_root.parent / "elsewhere"
    elsewhere.mkdir(parents=True, exist_ok=True)
    for name in ("config", "backups", "data"):
        (elsewhere / name).mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.rmtree(install_root)
    install_root.symlink_to(elsewhere)
    return elsewhere


def test_a_symlinked_install_root_disables_backup_authentication(tmp_path):
    services = appliance(tmp_path)
    redirect_parent(services)

    report = activation(services).activate()

    assert report["state"] != STATE_ACTIVE, report
    assert report["authentication_disabled"] is True, report


def test_a_symlinked_install_root_is_a_named_boundary_problem(tmp_path):
    services = appliance(tmp_path)
    redirect_parent(services)

    state = activation(services).export_state()

    assert state["exact"] is False, state
    assert any("symbolic link" in problem for problem in state["problems"]), state


def test_a_symlinked_export_target_is_a_named_boundary_problem(tmp_path):
    services = appliance(tmp_path)
    target = services.paths.export_targets()["config"]
    elsewhere = tmp_path / "elsewhere-config"
    elsewhere.mkdir(parents=True, exist_ok=True)
    target.rmdir()
    target.symlink_to(elsewhere)

    state = activation(services).export_state()

    assert state["exact"] is False, state


# --- verify-install sees the same policy ------------------------------------


def test_verify_install_fails_when_a_promised_restriction_is_dropped(tmp_path):
    services = appliance(tmp_path)
    services.host.sshd_backup_match = COMPLIANT.format(
        export_root=services.paths.export_root
    ).replace("permittty no", "permittty yes")

    from appliance.install_check import verify_installation

    report = verify_installation(services.paths, runner=services.runner, live=False)

    assert report["ok"] is False, report
    assert any("host_paths" in failure for failure in report["failures"]), report


def test_verify_install_fails_on_a_runtime_path_boundary_violation(tmp_path):
    services = appliance(tmp_path)
    redirect_parent(services)

    from appliance.install_check import verify_installation

    report = verify_installation(services.paths, runner=services.runner, live=False)

    assert report["ok"] is False, report
    assert any("path" in failure for failure in report["failures"]), report
