# SPDX-License-Identifier: AGPL-3.0-or-later
"""The generated host configuration is the only host-path authority.

``appliance.conf`` names two movable roots. Everything that acts on them — the
Python services, the export setup script, the systemd path watcher and the sshd
policy that confines the backup account — has to derive from that one value. A
generated artefact that is missing, stale or disagrees with the configuration is
drift, not a detail: it is the difference between a chroot into the configured
export root and a chroot into the packaged default.
"""

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

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.config]


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
