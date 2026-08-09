# SPDX-License-Identifier: AGPL-3.0-or-later
"""Applying the host configuration is one transaction over disk *and* runtime.

Restoring three files is not a rollback. Between writing them and failing, the
appliance has told systemd to re-arm the export watcher against the new path and
sshd to load the new Match policy; putting the old bytes back leaves the running
system on the new configuration with no file that says so. "Rolled back" is
therefore only allowed to be reported once the old watched path and the old
effective SSH policy were read back and matched.
"""

import stat

import pytest

from appliance.config import ApplianceConfig
from appliance.host_config import (
    HostConfigError,
    apply_host_config,
    describe,
    host_paths_file,
    live_activation,
    path_unit_dropin,
    sshd_policy_file,
)
from appliance.commands import CommandResult
from appliance.paths import AppliancePaths
from tests.helpers.appliance_host_runtime import FakeHost

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.config]


@pytest.fixture
def host_layout(tmp_path):
    def build(name="ems-solarflow"):
        paths = AppliancePaths(
            install_root=tmp_path / "opt" / name,
            config_dir=tmp_path / "etc" / "ems-appliance-manager",
            state_dir=tmp_path / "var" / "lib" / "ems-appliance-manager",
            log_dir=tmp_path / "var" / "log" / "ems-appliance-manager",
            runtime_dir=tmp_path / "run" / "ems-appliance-manager",
            export_root=tmp_path / "srv" / "ems-appliance-export",
        )
        paths.config_dir.mkdir(parents=True, exist_ok=True)
        paths.install_root.mkdir(parents=True, exist_ok=True)
        return paths

    return build


@pytest.fixture
def directories(tmp_path):
    return tmp_path / "etc" / "systemd" / "system", tmp_path / "etc" / "ssh" / "sshd_config.d"


def apply_configuration(paths, directories, host, *, marker, **activation):
    systemd_dir, sshd_dir = directories
    return apply_host_config(
        paths,
        ApplianceConfig(),
        systemd_dir=str(systemd_dir),
        sshd_dir=str(sshd_dir),
        activation=live_activation(runner=host, marker=str(marker), **activation),
    )


@pytest.fixture
def installed(host_layout, directories, tmp_path):
    """A host that already runs one applied configuration."""

    paths = host_layout("ems-solarflow")
    systemd_dir, sshd_dir = directories
    host = FakeHost(systemd_dir=systemd_dir, sshd_dir=sshd_dir)
    apply_configuration(paths, directories, host, marker=tmp_path)
    assert host.armed_path == str(paths.install_root)
    assert host.effective_chroot() == str(paths.export_root)
    return paths, host


# --- the runtime must not survive a rolled-back apply -----------------------


def test_a_failed_ssh_activation_restores_the_running_watcher(
    installed, host_layout, directories, tmp_path
):
    previous, host = installed
    moved = host_layout("moved-ems")
    host.fail("systemctl", "reload", "ssh.service")

    with pytest.raises(HostConfigError):
        apply_configuration(moved, directories, host, marker=tmp_path)

    assert host.armed_path == str(previous.install_root), host.sequence("systemctl")


def test_a_failed_ssh_activation_restores_the_files_it_wrote(
    installed, host_layout, directories, tmp_path
):
    previous, host = installed
    systemd_dir, sshd_dir = directories
    moved = host_layout("moved-ems")
    host.fail("systemctl", "reload", "ssh.service")

    with pytest.raises(HostConfigError):
        apply_configuration(moved, directories, host, marker=tmp_path)

    assert str(previous.install_root) in host_paths_file(previous).read_text(encoding="utf-8")
    assert str(previous.install_root) in path_unit_dropin(str(systemd_dir)).read_text(
        encoding="utf-8"
    )
    assert str(previous.export_root) in sshd_policy_file(str(sshd_dir)).read_text(encoding="utf-8")


def test_a_rolled_back_apply_reloads_systemd_after_restoring_the_files(
    installed, host_layout, directories, tmp_path
):
    _, host = installed
    moved = host_layout("moved-ems")
    host.fail("systemctl", "reload", "ssh.service")

    with pytest.raises(HostConfigError):
        apply_configuration(moved, directories, host, marker=tmp_path)

    sequence = host.sequence("systemctl")
    failure = min(
        index for index, call in enumerate(sequence) if call == "systemctl reload ssh.service"
    )
    assert "systemctl daemon-reload" in sequence[failure:], sequence
    assert "systemctl restart ems-appliance-export.path" in sequence[failure:], sequence


def test_an_unexpected_activation_error_still_rolls_the_runtime_back(
    installed, host_layout, directories, tmp_path
):
    previous, host = installed
    moved = host_layout("moved-ems")

    class Exploding(FakeHost):
        def run(self, tool, args=(), **kwargs):
            if tool == "systemctl" and tuple(args)[:1] == ("reload",):
                raise RuntimeError("the host went away")
            return super().run(tool, args, **kwargs)

    exploding = Exploding(systemd_dir=host.systemd_dir, sshd_dir=host.sshd_dir)
    exploding.armed_path = host.armed_path
    exploding.loaded_policy = dict(host.loaded_policy)

    with pytest.raises(HostConfigError):
        apply_configuration(moved, directories, exploding, marker=tmp_path)

    assert exploding.armed_path == str(previous.install_root), exploding.sequence("systemctl")


# --- a successful apply is verified, not assumed ----------------------------


def test_a_watcher_that_stays_on_the_old_path_is_not_a_successful_apply(
    installed, host_layout, directories, tmp_path
):
    previous, host = installed
    moved = host_layout("moved-ems")

    class Deaf(FakeHost):
        """systemd accepts the restart but keeps watching the old path."""

        def run(self, tool, args=(), **kwargs):
            armed = self.armed_path
            result = super().run(tool, args, **kwargs)
            if tool == "systemctl" and tuple(args)[:1] == ("restart",):
                self.armed_path = armed
            return result

    deaf = Deaf(systemd_dir=host.systemd_dir, sshd_dir=host.sshd_dir)
    deaf.armed_path = host.armed_path
    deaf.loaded_policy = dict(host.loaded_policy)

    with pytest.raises(HostConfigError):
        apply_configuration(moved, directories, deaf, marker=tmp_path)

    assert str(previous.install_root) in host_paths_file(previous).read_text(encoding="utf-8")


# --- drift reporting --------------------------------------------------------


def test_an_effective_policy_that_drops_a_restriction_is_drift(installed, directories):
    paths, host = installed
    systemd_dir, sshd_dir = directories
    host.loaded_policy["permittty"] = "yes"

    report = describe(
        paths,
        ApplianceConfig(),
        systemd_dir=str(systemd_dir),
        sshd_dir=str(sshd_dir),
        runner=host,
    )

    assert report["consistent"] is False, report
    assert "effective_ssh_policy" in report["drift"], report


def test_an_empty_active_watched_path_is_drift(installed, directories):
    paths, host = installed
    systemd_dir, sshd_dir = directories
    host.armed_path = ""

    report = describe(
        paths,
        ApplianceConfig(),
        systemd_dir=str(systemd_dir),
        sshd_dir=str(sshd_dir),
        runner=host,
    )

    assert report["consistent"] is False, report
    assert "active_watched_path" in report["drift"], report


# --- the rollback result is explicit ----------------------------------------


class Authentication:
    """Stands in for the fail-closed backup-access disable step."""

    def __init__(self):
        self.disabled = 0

    def __call__(self, reason=""):
        self.disabled += 1
        return True


def test_a_rolled_back_apply_reports_both_halves_of_the_rollback(
    installed, host_layout, directories, tmp_path
):
    _, host = installed
    moved = host_layout("moved-ems")
    host.fail("systemctl", "reload", "ssh.service")
    authentication = Authentication()

    with pytest.raises(HostConfigError) as excinfo:
        apply_configuration(
            moved, directories, host, marker=tmp_path, disable_authentication=authentication
        )

    rollback = excinfo.value.rollback
    assert rollback["applied"] is False, rollback
    assert rollback["disk_rollback"] == "succeeded", rollback
    assert rollback["runtime_rollback"] == "succeeded", rollback
    assert rollback["authentication_disabled"] is True, rollback
    assert rollback["remaining_drift"] == [], rollback
    assert authentication.disabled == 1


def test_a_runtime_that_cannot_be_restored_is_never_reported_as_rolled_back(
    installed, host_layout, directories, tmp_path
):
    """The watcher moved to the new path and cannot be moved back."""

    previous, host = installed
    moved = host_layout("moved-ems")

    class Sticky(FakeHost):
        def __init__(self, **options):
            super().__init__(**options)
            self.restarts = 0

        def run(self, tool, args=(), **kwargs):
            if tool == "systemctl" and tuple(args)[:2] == ("restart", "ems-appliance-export.path"):
                self.restarts += 1
                if self.restarts > 1:
                    self.fail("systemctl", "restart", "ems-appliance-export.path")
            return super().run(tool, args, **kwargs)

    sticky = Sticky(systemd_dir=host.systemd_dir, sshd_dir=host.sshd_dir)
    sticky.armed_path = host.armed_path
    sticky.loaded_policy = dict(host.loaded_policy)
    sticky.fail("systemctl", "reload", "ssh.service")

    with pytest.raises(HostConfigError) as excinfo:
        apply_configuration(
            moved,
            directories,
            sticky,
            marker=tmp_path,
            disable_authentication=Authentication(),
        )

    rollback = excinfo.value.rollback
    assert rollback["runtime_rollback"] == "failed", rollback
    assert "watched_path_not_restored" in rollback["remaining_drift"], rollback
    assert "path_watcher_not_re_armed" in rollback["failed_steps"], rollback
    assert rollback["authentication_disabled"] is True, rollback
    assert sticky.armed_path == str(moved.install_root)
    assert str(previous.install_root) in host_paths_file(previous).read_text(encoding="utf-8")


def test_a_successful_apply_reports_the_verified_runtime(
    installed, host_layout, directories, tmp_path
):
    _, host = installed
    moved = host_layout("moved-ems")

    report = apply_configuration(moved, directories, host, marker=tmp_path)

    assert report["applied"] is True, report
    assert report["runtime"]["verified"] is True, report
    assert report["runtime"]["state"] == "verified", report
    assert report["runtime"]["watched_path"] == str(moved.install_root), report
    assert report["runtime"]["ssh_policy_confirmed"] is True, report
    assert host.armed_path == str(moved.install_root)


# --- an unreadable runtime is a failed apply, never a quiet success ---------


def test_an_unreadable_effective_policy_fails_the_apply(
    installed, host_layout, directories, tmp_path
):
    """``sshd -t`` passing says nothing about what the daemon would apply."""

    _, host = installed
    moved = host_layout("moved-ems")
    authentication = Authentication()
    host.fail("sshd", "-T")

    with pytest.raises(HostConfigError) as excinfo:
        apply_configuration(
            moved, directories, host, marker=tmp_path, disable_authentication=authentication
        )

    assert excinfo.value.code == "ssh_policy_unreadable", excinfo.value.code
    assert excinfo.value.rollback["authentication_disabled"] is True, excinfo.value.rollback
    assert excinfo.value.rollback["disk_rollback"] == "succeeded", excinfo.value.rollback


def test_no_applied_report_ever_claims_an_unconfirmed_policy(
    installed, host_layout, directories, tmp_path
):
    for _ in range(2):
        report = apply_configuration(
            host_layout("moved-ems"), directories, installed[1], marker=tmp_path
        )
        assert report["applied"] is True, report
        assert isinstance(report["runtime"]["ssh_policy_confirmed"], bool), report
        assert report["runtime"]["ssh_policy_confirmed"] is True, report


def test_a_runtime_that_cannot_be_read_back_rolls_the_apply_back(
    installed, host_layout, directories, tmp_path
):
    """The watcher answered before the apply and refuses to answer after it."""

    previous, host = installed
    moved = host_layout("moved-ems")

    class Silent(FakeHost):
        def __init__(self, **options):
            super().__init__(**options)
            self.applied = False

        def run(self, tool, args=(), **kwargs):
            result = super().run(tool, args, **kwargs)
            if tool == "systemctl" and tuple(args)[:2] == ("restart", "ems-appliance-export.path"):
                self.applied = True
            if (
                self.applied
                and tool == "systemctl"
                and tuple(args)[:2] == ("show", "ems-appliance-export.path")
            ):
                return CommandResult(tool=tool, args=tuple(args), returncode=1, stdout="", stderr="")
            return result

    silent = Silent(systemd_dir=host.systemd_dir, sshd_dir=host.sshd_dir)
    silent.armed_path = host.armed_path
    silent.loaded_policy = dict(host.loaded_policy)

    with pytest.raises(HostConfigError) as excinfo:
        apply_configuration(
            moved, directories, silent, marker=tmp_path, disable_authentication=Authentication()
        )

    assert excinfo.value.code == "runtime_state_unverified", excinfo.value.code
    assert str(previous.install_root) in host_paths_file(previous).read_text(encoding="utf-8")


# --- a rollback restores values, not the names of what is wrong -------------


def test_a_rollback_to_a_different_wrong_value_is_drift(
    installed, host_layout, directories, tmp_path
):
    """``PermitTTY yes`` and ``PermitTTY forced-commands-only`` break the same rule."""

    _, host = installed
    moved = host_layout("moved-ems")
    host.loaded_policy["permittty"] = "yes"

    class Weakened(FakeHost):
        def run(self, tool, args=(), **kwargs):
            result = super().run(tool, args, **kwargs)
            if tool == "systemctl" and tuple(args)[:2] == ("reload", "ssh.service"):
                self.loaded_policy["permittty"] = "forced-commands-only"
            return result

    weakened = Weakened(systemd_dir=host.systemd_dir, sshd_dir=host.sshd_dir)
    weakened.armed_path = host.armed_path
    weakened.loaded_policy = dict(host.loaded_policy)
    weakened.loaded_policy["permittty"] = "yes"
    weakened.fail("systemctl", "restart", "ems-appliance-export.path")

    with pytest.raises(HostConfigError) as excinfo:
        apply_configuration(
            moved, directories, weakened, marker=tmp_path, disable_authentication=Authentication()
        )

    rollback = excinfo.value.rollback
    assert "ssh_policy_not_restored" in rollback["remaining_drift"], rollback
    difference = rollback["differences"]["ssh_policy_not_restored"]["permittty"]
    assert difference == {"expected": "yes", "observed": "forced-commands-only"}, difference
    assert rollback["runtime_rollback"] == "failed", rollback


def test_a_rollback_that_restored_every_value_reports_no_drift(
    installed, host_layout, directories, tmp_path
):
    _, host = installed
    moved = host_layout("moved-ems")
    host.fail("systemctl", "reload", "ssh.service")

    with pytest.raises(HostConfigError) as excinfo:
        apply_configuration(
            moved, directories, host, marker=tmp_path, disable_authentication=Authentication()
        )

    rollback = excinfo.value.rollback
    assert rollback["remaining_drift"] == [], rollback
    assert rollback["differences"] == {}, rollback


def test_a_previously_degraded_policy_is_restored_exactly(
    installed, host_layout, directories, tmp_path
):
    """A host that was already degraded gets that exact degraded state back."""

    _, host = installed
    moved = host_layout("moved-ems")
    host.loaded_policy["gatewayports"] = "clientspecified"
    host.fail("systemctl", "reload", "ssh.service")

    with pytest.raises(HostConfigError) as excinfo:
        apply_configuration(
            moved, directories, host, marker=tmp_path, disable_authentication=Authentication()
        )

    rollback = excinfo.value.rollback
    assert rollback["runtime_rollback"] == "succeeded", rollback
    assert host.loaded_policy["gatewayports"] == "clientspecified", host.loaded_policy


# --- an artefact the appliance did not generate is not overwritten ----------


def test_a_symlinked_artefact_is_refused_before_anything_is_written(
    host_layout, directories, tmp_path
):
    """A pre-existing symlink is operator state, not a file to replace."""

    paths = host_layout("ems-solarflow")
    systemd_dir, sshd_dir = directories
    host = FakeHost(systemd_dir=systemd_dir, sshd_dir=sshd_dir)
    elsewhere = tmp_path / "operator-host-paths.env"
    elsewhere.write_text("EMS_APPLIANCE_INSTALL_ROOT=/operator\n", encoding="utf-8")
    target = host_paths_file(paths)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(elsewhere)

    with pytest.raises(HostConfigError) as excinfo:
        apply_configuration(paths, directories, host, marker=tmp_path)

    assert target.is_symlink(), "the operator's symlink was replaced"
    assert target.readlink() == elsewhere
    assert elsewhere.read_text(encoding="utf-8") == "EMS_APPLIANCE_INSTALL_ROOT=/operator\n"
    assert excinfo.value.code == "host_artifact_not_generated", excinfo.value.code
    assert not path_unit_dropin(str(systemd_dir)).exists(), "a later artefact was still written"


def test_a_directory_where_an_artefact_belongs_is_refused(host_layout, directories, tmp_path):
    paths = host_layout("ems-solarflow")
    systemd_dir, sshd_dir = directories
    host = FakeHost(systemd_dir=systemd_dir, sshd_dir=sshd_dir)
    target = host_paths_file(paths)
    target.mkdir(parents=True, exist_ok=True)

    with pytest.raises(HostConfigError) as excinfo:
        apply_configuration(paths, directories, host, marker=tmp_path)

    assert target.is_dir()
    assert excinfo.value.code == "host_artifact_not_generated", excinfo.value.code


def test_a_rollback_restores_the_recorded_owner(installed, host_layout, directories, tmp_path):
    """The previous owner is part of the artefact, not a detail of its bytes."""

    previous, host = installed
    target = host_paths_file(previous)
    import os

    before = os.stat(target)
    moved = host_layout("moved-ems")
    host.fail("systemctl", "reload", "ssh.service")

    with pytest.raises(HostConfigError) as excinfo:
        apply_configuration(moved, directories, host, marker=tmp_path)

    after = os.stat(target)
    assert excinfo.value.rollback["disk_rollback"] == "succeeded", excinfo.value.rollback
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)


def test_ownership_that_cannot_be_restored_is_never_a_successful_rollback(
    installed, host_layout, directories, tmp_path, monkeypatch
):
    """Restoring the bytes as the current user is not restoring the file."""

    previous, host = installed
    target = host_paths_file(previous)
    import appliance.host_config as host_config

    saved = host_config._snapshot([target])[target]
    saved["uid"] = saved["uid"] + 1
    monkeypatch.setattr(
        host_config, "_snapshot", lambda targets: {path: dict(saved) for path in targets}
    )

    def refuse(*_arguments, **_keywords):
        raise PermissionError("only root may change the owner")

    monkeypatch.setattr(host_config.os, "chown", refuse)
    moved = host_layout("moved-ems")
    host.fail("systemctl", "reload", "ssh.service")

    with pytest.raises(HostConfigError) as excinfo:
        apply_configuration(moved, directories, host, marker=tmp_path)

    rollback = excinfo.value.rollback
    assert rollback["disk_rollback"] == "failed", rollback
    assert any(str(target) in item for item in rollback["remaining_drift"]), rollback
    assert rollback["authentication_disabled"] is not None, rollback


# --- the runtime baseline is not optional -----------------------------------


class RefusingCapture:
    """An activation whose baseline cannot be read."""

    def __init__(self, runner):
        self.runner = runner
        self.activated = False

    def capture(self, paths, config):
        raise OSError("systemctl is not answering")

    def __call__(self, paths, config):
        self.activated = True

    def disable_authentication(self, reason=""):
        self.disabled = reason
        return True


def test_a_baseline_that_cannot_be_read_aborts_before_any_write(
    host_layout, directories, tmp_path
):
    from appliance.config import ApplianceConfig
    from appliance.host_config import apply_host_config

    paths = host_layout("ems-solarflow")
    systemd_dir, sshd_dir = directories
    host = FakeHost(systemd_dir=systemd_dir, sshd_dir=sshd_dir)
    activation = RefusingCapture(host)

    with pytest.raises(HostConfigError) as excinfo:
        apply_host_config(
            paths,
            ApplianceConfig(),
            systemd_dir=str(systemd_dir),
            sshd_dir=str(sshd_dir),
            activation=activation,
        )

    assert excinfo.value.code == "runtime_snapshot_unavailable", excinfo.value.code
    assert activation.activated is False, "activation ran without a baseline"
    assert not host_paths_file(paths).exists(), "a generated file was written"
    assert not path_unit_dropin(str(systemd_dir)).exists()
    assert not sshd_policy_file(str(sshd_dir)).exists()
    assert excinfo.value.rollback["runtime_rollback"] != "not_required", excinfo.value.rollback
    assert excinfo.value.rollback["authentication_disabled"] is True, excinfo.value.rollback
