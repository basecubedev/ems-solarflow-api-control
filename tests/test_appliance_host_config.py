# SPDX-License-Identifier: AGPL-3.0-or-later
"""The generated host configuration is the only host-path authority.

``appliance.conf`` names two movable roots. Everything that acts on them — the
Python services, the export setup script, the systemd path watcher and the sshd
policy that confines the backup account — has to derive from that one value. A
generated artefact that is missing, stale or disagrees with the configuration is
drift, not a detail: it is the difference between a chroot into the configured
export root and a chroot into the packaged default.
"""

import os

import pytest

from appliance.config import ApplianceConfig, ConfigError, load_config
from appliance.host_config import (
    HostConfigError,
    apply_host_config,
    describe,
    host_paths_file,
    path_unit_dropin,
    sshd_policy_file,
)
from appliance.paths import AppliancePaths, PathBoundaryError, resolve_paths
from tests.helpers.appliance_host_runtime import FakeHost

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.config, pytest.mark.appliance]


def layout(tmp_path, *, install_root=None, export_root=None):
    paths = AppliancePaths(
        install_root=install_root or (tmp_path / "opt" / "ems-solarflow"),
        config_dir=tmp_path / "etc" / "ems-appliance-manager",
        state_dir=tmp_path / "var" / "lib" / "ems-appliance-manager",
        log_dir=tmp_path / "var" / "log" / "ems-appliance-manager",
        runtime_dir=tmp_path / "run" / "ems-appliance-manager",
        export_root=export_root or (tmp_path / "srv" / "ems-appliance-export"),
    )
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.install_root.mkdir(parents=True, exist_ok=True)
    return paths


def write_conf(paths, body):
    paths.appliance_conf.write_text("[appliance]\n" + body, encoding="utf-8")
    return paths.appliance_conf


def environ(paths):
    return {"EMS_APPLIANCE_CONFIG_DIR": str(paths.config_dir)}


# --- the configured path identity is preserved ------------------------------


def test_a_symlinked_install_root_is_rejected_not_canonicalised(tmp_path):
    """A configured symlink must not be silently rewritten into its target."""

    paths = layout(tmp_path)
    real = tmp_path / "elsewhere" / "ems"
    real.mkdir(parents=True)
    link = tmp_path / "opt" / "linked-ems"
    link.symlink_to(real)
    write_conf(paths, f"install_root = {link}\nexport_root = {paths.export_root}\n")

    with pytest.raises(PathBoundaryError):
        resolve_paths(environ(paths))


def test_a_symlinked_export_root_is_rejected(tmp_path):
    paths = layout(tmp_path)
    real = tmp_path / "elsewhere" / "export"
    real.mkdir(parents=True)
    link = tmp_path / "srv" / "linked-export"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real)
    write_conf(paths, f"install_root = {paths.install_root}\nexport_root = {link}\n")

    with pytest.raises(PathBoundaryError):
        resolve_paths(environ(paths))


def test_a_root_below_a_symlinked_parent_is_rejected(tmp_path):
    paths = layout(tmp_path)
    (tmp_path / "outside").mkdir()
    (tmp_path / "srv").mkdir(parents=True, exist_ok=True)
    (tmp_path / "srv" / "redirect").symlink_to(tmp_path / "outside")
    write_conf(
        paths,
        f"install_root = {paths.install_root}\n"
        f"export_root = {tmp_path}/srv/redirect/ems-export\n",
    )

    with pytest.raises(PathBoundaryError):
        resolve_paths(environ(paths))


def test_a_real_directory_at_the_configured_path_is_accepted(tmp_path):
    """A separate data partition is mounted at the path; that stays supported."""

    paths = layout(tmp_path)
    (tmp_path / "srv").mkdir(parents=True, exist_ok=True)
    paths.export_root.mkdir(parents=True, exist_ok=True)
    write_conf(paths, f"install_root = {paths.install_root}\nexport_root = {paths.export_root}\n")

    resolved = resolve_paths(environ(paths))

    assert resolved.install_root == paths.install_root
    assert resolved.export_root == paths.export_root


def test_relative_and_dotted_segments_are_rejected(tmp_path):
    paths = layout(tmp_path)
    write_conf(paths, f"install_root = {tmp_path}/opt/../opt/ems\nexport_root = {paths.export_root}\n")

    with pytest.raises(PathBoundaryError):
        resolve_paths(environ(paths))


def test_repeated_slashes_are_rejected(tmp_path):
    paths = layout(tmp_path)
    write_conf(paths, f"install_root = {tmp_path}//opt/ems\nexport_root = {paths.export_root}\n")

    with pytest.raises(PathBoundaryError):
        resolve_paths(environ(paths))


# --- roots may not overlap in either direction ------------------------------


def test_an_install_root_inside_the_export_root_is_rejected(tmp_path):
    paths = layout(tmp_path)
    write_conf(
        paths,
        f"install_root = {paths.export_root}/ems\nexport_root = {paths.export_root}\n",
    )

    with pytest.raises(PathBoundaryError):
        resolve_paths(environ(paths))


def test_an_export_root_inside_the_install_root_is_rejected(tmp_path):
    paths = layout(tmp_path)
    write_conf(
        paths,
        f"install_root = {paths.install_root}\nexport_root = {paths.install_root}/export\n",
    )

    with pytest.raises(PathBoundaryError):
        resolve_paths(environ(paths))


def test_identical_roots_are_rejected(tmp_path):
    paths = layout(tmp_path)
    write_conf(
        paths,
        f"install_root = {paths.install_root}\nexport_root = {paths.install_root}\n",
    )

    with pytest.raises(PathBoundaryError):
        resolve_paths(environ(paths))


# --- generated artefacts ----------------------------------------------------


def test_apply_generates_the_ssh_policy_for_the_configured_export_root(tmp_path):
    paths = layout(tmp_path, export_root=tmp_path / "srv" / "custom-export")
    paths.export_root.mkdir(parents=True, exist_ok=True)
    config = ApplianceConfig()

    report = apply_host_config(
        paths,
        config,
        systemd_dir=str(tmp_path / "etc" / "systemd" / "system"),
        sshd_dir=str(tmp_path / "etc" / "ssh" / "sshd_config.d"),
    )

    policy = sshd_policy_file(str(tmp_path / "etc" / "ssh" / "sshd_config.d"))
    assert policy.is_file(), report
    text = policy.read_text(encoding="utf-8")
    assert f"ChrootDirectory {paths.export_root}" in text, text
    assert f"Match User {config.backup_user}" in text, text
    assert str(paths.export_root) in report["written"][-1] or report["written"], report


def test_apply_generates_the_path_unit_dropin_for_the_configured_install_root(tmp_path):
    paths = layout(tmp_path, install_root=tmp_path / "srv" / "ems")
    systemd_dir = tmp_path / "etc" / "systemd" / "system"

    apply_host_config(
        paths,
        ApplianceConfig(),
        systemd_dir=str(systemd_dir),
        sshd_dir=str(tmp_path / "etc" / "ssh" / "sshd_config.d"),
    )

    dropin = path_unit_dropin(str(systemd_dir))
    assert dropin.is_file()
    assert f"PathChanged={paths.install_root}" in dropin.read_text(encoding="utf-8")


def test_a_missing_path_unit_dropin_is_drift(tmp_path):
    paths = layout(tmp_path, install_root=tmp_path / "srv" / "ems")
    systemd_dir = tmp_path / "etc" / "systemd" / "system"
    sshd_dir = tmp_path / "etc" / "ssh" / "sshd_config.d"
    apply_host_config(
        paths, ApplianceConfig(), systemd_dir=str(systemd_dir), sshd_dir=str(sshd_dir)
    )
    path_unit_dropin(str(systemd_dir)).unlink()

    report = describe(
        paths, ApplianceConfig(), systemd_dir=str(systemd_dir), sshd_dir=str(sshd_dir)
    )

    assert report["consistent"] is False, report
    assert "path_unit_dropin" in report["drift"], report


def test_a_missing_ssh_policy_is_drift(tmp_path):
    paths = layout(tmp_path)
    systemd_dir = tmp_path / "etc" / "systemd" / "system"
    sshd_dir = tmp_path / "etc" / "ssh" / "sshd_config.d"
    apply_host_config(
        paths, ApplianceConfig(), systemd_dir=str(systemd_dir), sshd_dir=str(sshd_dir)
    )
    sshd_policy_file(str(sshd_dir)).unlink()

    report = describe(
        paths, ApplianceConfig(), systemd_dir=str(systemd_dir), sshd_dir=str(sshd_dir)
    )

    assert report["consistent"] is False, report
    assert "sshd_policy" in report["drift"], report


def test_edited_generated_content_is_drift(tmp_path):
    paths = layout(tmp_path)
    systemd_dir = tmp_path / "etc" / "systemd" / "system"
    sshd_dir = tmp_path / "etc" / "ssh" / "sshd_config.d"
    apply_host_config(
        paths, ApplianceConfig(), systemd_dir=str(systemd_dir), sshd_dir=str(sshd_dir)
    )
    policy = sshd_policy_file(str(sshd_dir))
    policy.write_text(
        policy.read_text(encoding="utf-8").replace(
            str(paths.export_root), "/srv/ems-appliance-export"
        ),
        encoding="utf-8",
    )

    report = describe(
        paths, ApplianceConfig(), systemd_dir=str(systemd_dir), sshd_dir=str(sshd_dir)
    )

    assert report["consistent"] is False, report


def test_a_missing_environment_file_is_drift(tmp_path):
    paths = layout(tmp_path)
    systemd_dir = tmp_path / "etc" / "systemd" / "system"
    sshd_dir = tmp_path / "etc" / "ssh" / "sshd_config.d"
    apply_host_config(
        paths, ApplianceConfig(), systemd_dir=str(systemd_dir), sshd_dir=str(sshd_dir)
    )
    host_paths_file(paths).unlink()

    report = describe(
        paths, ApplianceConfig(), systemd_dir=str(systemd_dir), sshd_dir=str(sshd_dir)
    )

    assert report["environment_present"] is False
    assert report["consistent"] is False


# --- transactional apply ----------------------------------------------------


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="the write is blocked by a directory mode, which root ignores",
)
def test_a_failed_artefact_write_restores_every_previous_artefact(tmp_path):
    paths = layout(tmp_path)
    systemd_dir = tmp_path / "etc" / "systemd" / "system"
    sshd_dir = tmp_path / "etc" / "ssh" / "sshd_config.d"
    apply_host_config(
        paths, ApplianceConfig(), systemd_dir=str(systemd_dir), sshd_dir=str(sshd_dir)
    )
    before = {
        "environment": host_paths_file(paths).read_text(encoding="utf-8"),
        "dropin": path_unit_dropin(str(systemd_dir)).read_text(encoding="utf-8"),
        "policy": sshd_policy_file(str(sshd_dir)).read_text(encoding="utf-8"),
    }

    moved = layout(tmp_path, install_root=tmp_path / "srv" / "moved-ems")
    blocked = sshd_policy_file(str(sshd_dir))
    blocked.parent.chmod(0o500)
    try:
        with pytest.raises(HostConfigError):
            apply_host_config(
                moved,
                ApplianceConfig(),
                systemd_dir=str(systemd_dir),
                sshd_dir=str(sshd_dir),
            )
    finally:
        blocked.parent.chmod(0o755)

    assert host_paths_file(paths).read_text(encoding="utf-8") == before["environment"]
    assert path_unit_dropin(str(systemd_dir)).read_text(encoding="utf-8") == before["dropin"]
    assert sshd_policy_file(str(sshd_dir)).read_text(encoding="utf-8") == before["policy"]


# --- the supported custom-root contract -------------------------------------


def test_a_non_default_backup_user_is_refused_explicitly(tmp_path):
    paths = layout(tmp_path)
    write_conf(paths, "backup_user = someone-else\n")

    with pytest.raises(ConfigError) as excinfo:
        load_config(paths)

    assert excinfo.value.code == "backup_user_unsupported"


def test_the_packaged_backup_user_is_accepted(tmp_path):
    paths = layout(tmp_path)
    write_conf(paths, "backup_user = ems-backup\n")

    assert load_config(paths).backup_user == "ems-backup"


# --- live activation ---------------------------------------------------------


def apply_with(paths, host, tmp_path, *, marker=None):
    from appliance.host_config import live_activation

    return apply_host_config(
        paths,
        ApplianceConfig(),
        systemd_dir=str(tmp_path / "etc" / "systemd" / "system"),
        sshd_dir=str(tmp_path / "etc" / "ssh" / "sshd_config.d"),
        activation=live_activation(
            runner=host, marker=marker or str(tmp_path), disable_authentication=lambda reason="": True
        ),
    )


def host_for(tmp_path, **options):
    return FakeHost(
        systemd_dir=tmp_path / "etc" / "systemd" / "system",
        sshd_dir=tmp_path / "etc" / "ssh" / "sshd_config.d",
        **options,
    )


def test_an_sshd_that_cannot_validate_yet_does_not_roll_the_configuration_back(tmp_path):
    """openssh-server is often configured after this package on a first install."""

    paths = layout(tmp_path)
    host = host_for(tmp_path, path_unit_active="inactive")
    host.fail("sshd", "-t")

    report = apply_with(paths, host, tmp_path)

    assert host_paths_file(paths).is_file(), report
    assert sshd_policy_file(str(tmp_path / "etc" / "ssh" / "sshd_config.d")).is_file()


def test_a_policy_a_working_sshd_refuses_rolls_the_configuration_back(tmp_path):
    paths = layout(tmp_path)
    host = host_for(tmp_path)
    attempts = {"count": 0}
    original = host.run

    def run(tool, args=(), **kwargs):
        if tool == "sshd" and tuple(args)[:1] == ("-t",):
            attempts["count"] += 1
            if attempts["count"] > 1:
                host.fail("sshd", "-t")
        return original(tool, args, **kwargs)

    host.run = run

    with pytest.raises(HostConfigError):
        apply_with(paths, host, tmp_path)

    assert not host_paths_file(paths).exists()


def test_an_idle_path_watcher_is_not_restarted(tmp_path):
    paths = layout(tmp_path)
    host = host_for(tmp_path, path_unit_active="inactive")

    apply_with(paths, host, tmp_path)

    assert "systemctl restart ems-appliance-export.path" not in host.sequence()


def test_a_running_path_watcher_is_re_armed(tmp_path):
    paths = layout(tmp_path)
    host = host_for(tmp_path)

    apply_with(paths, host, tmp_path)

    assert "systemctl restart ems-appliance-export.path" in host.sequence()
    assert host.armed_path == str(paths.install_root)


def test_a_watcher_that_cannot_be_re_armed_rolls_the_configuration_back(tmp_path):
    paths = layout(tmp_path)
    host = host_for(tmp_path)
    host.fail("systemctl", "restart", "ems-appliance-export.path")

    with pytest.raises(HostConfigError):
        apply_with(paths, host, tmp_path)

    assert not host_paths_file(paths).exists()


def test_an_image_build_root_skips_systemd_entirely(tmp_path):
    paths = layout(tmp_path)
    host = host_for(tmp_path)

    apply_with(paths, host, tmp_path, marker=str(tmp_path / "absent-systemd"))

    assert not any(tool == "systemctl" for tool, _ in host.calls), host.calls
    assert host_paths_file(paths).is_file()


# --- sshd validation is three outcomes, not two -----------------------------
#
# `sshd_usable` was a boolean, so "openssh is not on this host" and "openssh is
# here and cannot validate yet" were the same answer — and both were reported as
# nothing left to prove. A first install on a host whose openssh had no host
# keys therefore recorded an SSH policy state of "not installed", which is a
# statement about the host and was not true.


def test_an_sshd_that_cannot_validate_yet_is_reported_as_not_ready(tmp_path):
    paths = layout(tmp_path)
    host = host_for(tmp_path, path_unit_active="inactive")
    host.fail("sshd", "-t", stderr="sshd: no hostkeys available -- exiting.")

    report = apply_with(paths, host, tmp_path)

    runtime = report["runtime"]
    assert runtime["sshd_state"] == "not_ready"
    assert "hostkeys" in runtime["sshd_detail"]
    assert runtime["ssh_policy_state"] == "not_ready"
    assert runtime["ssh_policy_confirmed"] is False


def test_a_host_without_openssh_is_reported_as_not_installed(tmp_path):
    """Different from not ready: this one really is a statement about the host."""

    paths = layout(tmp_path)
    host = host_for(tmp_path, tools=("systemctl",), path_unit_active="inactive")

    runtime = apply_with(paths, host, tmp_path)["runtime"]

    assert runtime["sshd_state"] == "absent"
    assert runtime["ssh_policy_state"] == "not_installed"


def test_a_working_sshd_is_reported_as_pass(tmp_path):
    paths = layout(tmp_path)
    host = host_for(tmp_path)

    runtime = apply_with(paths, host, tmp_path)["runtime"]

    assert runtime["sshd_state"] == "pass"
    assert runtime["ssh_policy_state"] == "verified"
    assert runtime["ssh_policy_confirmed"] is True


def test_an_sshd_that_names_our_policy_is_a_failure_even_when_it_was_broken_before(tmp_path):
    """The gap: a bad drop-in written where sshd already could not validate.

    The old check only ran when sshd validated *before* the write, so on a host
    whose openssh was not configured yet a policy sshd refuses was installed and
    the apply reported success.
    """

    paths = layout(tmp_path)
    host = host_for(tmp_path, path_unit_active="inactive")
    host.fail(
        "sshd",
        "-t",
        stderr="/etc/ssh/sshd_config.d/ems-appliance-backup.conf line 3: Bad configuration option",
    )

    with pytest.raises(HostConfigError) as error:
        apply_with(paths, host, tmp_path)

    assert error.value.code == "sshd_config_invalid"


def test_a_complaint_that_is_not_about_our_policy_still_does_not_roll_back(tmp_path):
    """openssh-server is often configured after this package on a first install."""

    paths = layout(tmp_path)
    host = host_for(tmp_path, path_unit_active="inactive")
    host.fail("sshd", "-t", stderr="sshd: no hostkeys available -- exiting.")

    report = apply_with(paths, host, tmp_path)

    assert host_paths_file(paths).is_file(), report
    assert sshd_policy_file(str(tmp_path / "etc" / "ssh" / "sshd_config.d")).is_file()
