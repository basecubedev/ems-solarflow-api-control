# SPDX-License-Identifier: AGPL-3.0-or-later
"""What has to be true before backup access may be called active.

The sshd policy and the mount table are only two of the preconditions. Without
the exact package-owned account there is nothing to confine, without a key there
is nothing to authenticate, and a path chain that grew a symbolic link after the
installation publishes a directory nobody validated. Each of those has to end in
``unavailable`` or ``degraded`` with a named reason — never in ``active``.
"""

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
