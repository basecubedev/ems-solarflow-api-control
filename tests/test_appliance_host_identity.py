# SPDX-License-Identifier: AGPL-3.0-or-later
"""The identity an appliance keeps across a slot switch.

An SSH host key regenerated per slot would change the appliance's fingerprint on
every OS update, which is exactly what the fingerprint exists to warn about. So
the keys live on the persistent partition, are created exactly once, and are
proven before sshd is allowed to read them.

``ssh-keygen`` is real here. A fake that produced key-shaped files would prove
nothing about whether the appliance ends up with usable host keys.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from appliance import host_identity
from appliance.commands import CommandError, CommandResult, RecordingRunner
from appliance.host_identity import (
    HostIdentityError,
    HostIdentityService,
    private_key_name,
    public_key_name,
)

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
MACHINE_ID = "0123456789abcdef0123456789abcdef"

requires_keygen = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None, reason="ssh-keygen is not installed"
)


class RealKeygenRunner(RecordingRunner):
    """The allowlisted runner, with ssh-keygen actually executed."""

    def run(self, tool, args=(), **kwargs):
        args = tuple(args)
        self.calls.append((tool, args, None))
        if tool == "ssh-keygen":
            completed = subprocess.run(
                ["ssh-keygen", *args], capture_output=True, text=True, timeout=120
            )
            return CommandResult(
                tool, args, completed.returncode, completed.stdout, completed.stderr
            )
        if tool == "sshd":
            return CommandResult(tool, args, 0, "", "")
        return super().run(tool, args, **kwargs)


def appliance(tmp_path, *, machine_id=MACHINE_ID, drop_in=True, network=True):
    """A slot root with the persistent tree an A/B image mounts under it."""

    root = tmp_path / "slot"
    (root / "var/lib/ems-appliance-manager").mkdir(parents=True, exist_ok=True)
    (root / "persistent/common/etc").mkdir(parents=True, exist_ok=True)
    (root / "etc").mkdir(parents=True, exist_ok=True)
    if machine_id:
        (root / "persistent/common/etc/machine-id").write_text(machine_id + "\n")
        (root / "etc/machine-id").write_text(machine_id + "\n")
    if network:
        target = root / "etc/NetworkManager/system-connections"
        target.mkdir(parents=True, exist_ok=True)
        target.chmod(0o700)
    if drop_in:
        source = (
            ROOT
            / "packaging/appliance/image/layer/ems-appliance.rootfs-overlay"
            / "etc/ssh/sshd_config.d/50-ems-appliance-hostkeys.conf"
        )
        target = root / "etc/ssh/sshd_config.d"
        target.mkdir(parents=True, exist_ok=True)
        (target / "50-ems-appliance-hostkeys.conf").write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8"
        )
    return root


def service(root, **kwargs):
    kwargs.setdefault("runner", RealKeygenRunner({}))
    kwargs.setdefault("require_root", False)
    return HostIdentityService(root=root, **kwargs)


# --- first boot ---------------------------------------------------------------


@requires_keygen
def test_the_first_boot_creates_every_declared_host_key(tmp_path):
    root = appliance(tmp_path)

    report = service(root).ensure()

    assert report.ok, report.problems
    assert sorted(report.created) == sorted(host_identity.HOST_KEY_TYPES)
    for key_type in host_identity.HOST_KEY_TYPES:
        private = root / "var/lib/ems-appliance-manager/ssh" / private_key_name(key_type)
        assert private.is_file()
        assert private.stat().st_mode & 0o077 == 0


@requires_keygen
def test_the_keys_land_in_the_directory_the_sshd_drop_in_names(tmp_path):
    root = appliance(tmp_path)
    service(root).ensure()

    drop_in = (root / "etc/ssh/sshd_config.d/50-ems-appliance-hostkeys.conf").read_text()
    for key_type in host_identity.HOST_KEY_TYPES:
        assert f"/var/lib/ems-appliance-manager/ssh/{private_key_name(key_type)}" in drop_in


@requires_keygen
def test_the_key_directory_is_private_and_not_world_readable(tmp_path):
    root = appliance(tmp_path)
    service(root).ensure()

    directory = root / "var/lib/ems-appliance-manager/ssh"
    assert directory.stat().st_mode & 0o077 == 0


@requires_keygen
def test_generation_leaves_no_staging_files_behind(tmp_path):
    root = appliance(tmp_path)
    service(root).ensure()

    directory = root / "var/lib/ems-appliance-manager/ssh"
    assert not [item for item in directory.iterdir() if item.name.startswith(".")]


# --- the second slot ----------------------------------------------------------


@requires_keygen
def test_a_second_slot_reuses_byte_identical_keys(tmp_path):
    """The persistent directory is the same one; the other slot is not."""

    root = appliance(tmp_path)
    first = service(root).ensure()
    directory = root / "var/lib/ems-appliance-manager/ssh"
    before = {path.name: path.read_bytes() for path in sorted(directory.iterdir())}

    other_slot = tmp_path / "slot-b"
    other_slot.mkdir()
    for relative in ("etc", "persistent"):
        shutil.copytree(root / relative, other_slot / relative, symlinks=True)
    # The shared bind, modelled: both slots see the same persistent directory.
    (other_slot / "var/lib").mkdir(parents=True)
    os.symlink(
        root / "var/lib/ems-appliance-manager",
        other_slot / "var/lib/ems-appliance-manager",
    )

    second = service(other_slot).ensure()

    assert second.ok, second.problems
    assert second.created == ()
    assert sorted(second.reused) == sorted(host_identity.HOST_KEY_TYPES)
    assert second.fingerprints == first.fingerprints
    assert {path.name: path.read_bytes() for path in sorted(directory.iterdir())} == before


@requires_keygen
def test_running_it_twice_changes_nothing(tmp_path):
    root = appliance(tmp_path)
    first = service(root).ensure()
    second = service(root).ensure()

    assert second.created == ()
    assert second.fingerprints == first.fingerprints


@requires_keygen
def test_only_a_missing_key_type_is_created(tmp_path):
    """Repair, never regeneration: the other fingerprints must not move."""

    root = appliance(tmp_path)
    first = service(root).ensure()
    directory = root / "var/lib/ems-appliance-manager/ssh"
    (directory / private_key_name("ecdsa")).unlink()
    (directory / public_key_name("ecdsa")).unlink()

    second = service(root).ensure()

    assert second.ok, second.problems
    assert second.created == ("ecdsa",)
    assert second.fingerprints["ed25519"] == first.fingerprints["ed25519"]
    assert second.fingerprints["rsa"] == first.fingerprints["rsa"]


@requires_keygen
def test_a_missing_public_half_is_rebuilt_from_the_private_key(tmp_path):
    """The crash window of first-boot placement, retried."""

    root = appliance(tmp_path)
    first = service(root).ensure()
    directory = root / "var/lib/ems-appliance-manager/ssh"
    secret = (directory / private_key_name("rsa")).read_bytes()
    (directory / public_key_name("rsa")).unlink()

    report = service(root).ensure()

    assert report.ok, report.problems
    assert report.created == ()
    assert (directory / private_key_name("rsa")).read_bytes() == secret
    assert report.fingerprints["rsa"] == first.fingerprints["rsa"]


@requires_keygen
def test_a_public_half_without_its_private_key_is_never_replaced(tmp_path):
    """There is no secret to recover from, so nothing is invented."""

    root = appliance(tmp_path)
    service(root).ensure()
    directory = root / "var/lib/ems-appliance-manager/ssh"
    (directory / private_key_name("rsa")).unlink()

    report = service(root).ensure()

    assert not report.ok
    assert any("host_key_private_half_missing" in problem for problem in report.problems)


# --- refusals -----------------------------------------------------------------


def test_a_symlinked_key_directory_is_refused(tmp_path):
    root = appliance(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, root / "var/lib/ems-appliance-manager/ssh")

    report = service(root).ensure()

    assert not report.ok
    assert any("symlink" in problem for problem in report.problems)


@requires_keygen
def test_a_symlinked_host_key_is_refused(tmp_path):
    root = appliance(tmp_path)
    service(root).ensure()
    directory = root / "var/lib/ems-appliance-manager/ssh"
    foreign = tmp_path / "foreign_key"
    foreign.write_text("not the appliance's key\n")
    (directory / private_key_name("ed25519")).unlink()
    os.symlink(foreign, directory / private_key_name("ed25519"))

    report = service(root).ensure()

    assert not report.ok
    assert any("symlink" in problem for problem in report.problems)


@requires_keygen
def test_a_world_readable_private_key_is_refused(tmp_path):
    root = appliance(tmp_path)
    service(root).ensure()
    private = root / "var/lib/ems-appliance-manager/ssh" / private_key_name("ed25519")
    private.chmod(0o644)

    report = service(root).ensure()

    assert not report.ok
    assert any("0644" in problem for problem in report.problems)


def test_a_key_directory_owned_by_another_account_is_refused(tmp_path, monkeypatch):
    from tests.test_appliance_host_identity_durability import owned_by_another

    root = appliance(tmp_path)
    directory = root / "var/lib/ems-appliance-manager/ssh"
    directory.mkdir(parents=True)
    directory.chmod(0o700)
    owned_by_another(monkeypatch, directory)
    probe = service(root, require_root=True)

    with pytest.raises(HostIdentityError) as excinfo:
        probe.verify_directory()

    assert excinfo.value.code == "host_key_directory_owner_wrong"


def test_a_group_readable_key_directory_is_refused(tmp_path):
    root = appliance(tmp_path)
    directory = root / "var/lib/ems-appliance-manager/ssh"
    directory.mkdir(parents=True)
    directory.chmod(0o750)

    with pytest.raises(HostIdentityError) as excinfo:
        service(root).verify_directory()

    assert excinfo.value.code == "host_key_directory_mode_wrong"


@requires_keygen
def test_sshd_refusing_its_configuration_is_a_failure(tmp_path):
    root = appliance(tmp_path)

    class RefusingSshd(RealKeygenRunner):
        def run(self, tool, args=(), **kwargs):
            if tool == "sshd":
                self.calls.append((tool, tuple(args), None))
                return CommandResult(tool, tuple(args), 255, "", "no host keys available")
            return super().run(tool, args, **kwargs)

    probe = service(root, runner=RefusingSshd({}))
    probe.ensure()
    finding = probe.validate_sshd()

    assert not finding.ok
    assert "no host keys" in finding.detail


@requires_keygen
def test_a_missing_privilege_separation_directory_is_not_a_bad_configuration(tmp_path):
    """sshd's runtime directory belongs to ssh.service, which has not started.

    This unit is ordered Before=ssh.service, so /run/sshd — ssh.service's own
    RuntimeDirectory — does not exist yet when it runs. ``sshd -t`` then exits
    non-zero complaining about the directory rather than about the config, the
    unit failed, and ssh.service Requires= it: SSH could never come up, and a
    recovery that repaired the key directory never recovered. Found by driving
    the recovery case in a booted guest.
    """

    root = appliance(tmp_path)

    class NoPrivsepDir(RealKeygenRunner):
        def run(self, tool, args=(), **kwargs):
            if tool == "sshd":
                self.calls.append((tool, tuple(args), None))
                return CommandResult(
                    tool, tuple(args), 255, "", "Missing privilege separation directory: /run/sshd"
                )
            return super().run(tool, args, **kwargs)

    probe = service(root, runner=NoPrivsepDir({}))
    probe.ensure()
    finding = probe.validate_sshd()

    assert finding.ok, finding.detail
    assert "/run/sshd" in finding.detail
    assert "not checked" in finding.detail.lower()


def test_a_drop_in_naming_other_key_paths_is_refused(tmp_path):
    root = appliance(tmp_path, drop_in=False)
    target = root / "etc/ssh/sshd_config.d"
    target.mkdir(parents=True)
    (target / "50-ems-appliance-hostkeys.conf").write_text("HostKey /etc/ssh/ssh_host_rsa_key\n")
    keys = root / "var/lib/ems-appliance-manager/ssh"
    keys.mkdir(parents=True)
    keys.chmod(0o700)

    report = service(root).verify()

    assert not report.ok
    assert any("50-ems-appliance-hostkeys.conf" in problem for problem in report.problems)


def test_a_machine_identity_that_is_not_the_shared_one_is_refused(tmp_path):
    root = appliance(tmp_path)
    (root / "etc/machine-id").write_text("ffffffffffffffffffffffffffffffff\n")
    keys = root / "var/lib/ems-appliance-manager/ssh"
    keys.mkdir(parents=True)
    keys.chmod(0o700)

    report = service(root).verify()

    assert not report.ok
    assert any("machine-id" in problem for problem in report.problems)


# --- nothing private is ever reported ----------------------------------------


@requires_keygen
def test_a_report_carries_public_fingerprints_and_no_key_material(tmp_path):
    root = appliance(tmp_path)
    report = service(root).ensure()
    directory = root / "var/lib/ems-appliance-manager/ssh"

    rendered = repr(report.to_dict())
    assert "PRIVATE KEY" not in rendered
    for key_type in host_identity.HOST_KEY_TYPES:
        private = (directory / private_key_name(key_type)).read_text(encoding="utf-8")
        assert private.strip() not in rendered
        assert report.fingerprints[key_type].startswith("SHA256:")


@requires_keygen
def test_the_support_archive_never_collects_the_key_directory():
    """The archive names what it collects; the key directory is not in it."""

    text = (ROOT / "appliance/support_archive.py").read_text(encoding="utf-8")

    assert "ssh_host" not in text
    assert host_identity.KEY_DIRECTORY not in text


# --- the ordering the unit enforces -------------------------------------------


def test_the_unit_runs_before_everything_that_presents_the_appliance():
    unit = (
        ROOT / "packaging/appliance/systemd/ems-appliance-host-identity.service"
    ).read_text(encoding="utf-8")

    assert "RequiresMountsFor=/persistent" in unit
    assert "After=local-fs.target" in unit
    assert "RequiresMountsFor=/var/lib/ems-appliance-manager" in unit
    assert "Before=ems-appliance-persistence.service" in unit
    assert "Before=ssh.service sshd.service NetworkManager.service" in unit


def test_sshd_requires_the_initializer_in_the_image():
    drop_in = (
        ROOT
        / "packaging/appliance/image/layer/ems-appliance.rootfs-overlay"
        / "etc/systemd/system/ssh.service.d/50-ems-appliance-host-identity.conf"
    ).read_text(encoding="utf-8")

    assert "Requires=ems-appliance-host-identity.service" in drop_in
    assert "After=ems-appliance-host-identity.service" in drop_in


# --- what "validated" is allowed to mean -------------------------------------


def test_a_configuration_sshd_accepted_is_reported_as_valid():
    service = HostIdentityService(
        runner=RecordingRunner({("sshd", ("-t",)): (0, "", "")}),
        root=Path("/nonexistent"),
    )

    finding = service.validate_sshd()

    assert finding.ok is True
    assert finding.status == host_identity.VALID
    assert finding.to_dict()["state"] == "valid"


def test_a_configuration_sshd_refused_is_reported_as_invalid():
    service = HostIdentityService(
        runner=RecordingRunner(
            {("sshd", ("-t",)): (1, "", "/etc/ssh/sshd_config.d/50-ems.conf: bad option")}
        ),
        root=Path("/nonexistent"),
    )

    finding = service.validate_sshd()

    assert finding.ok is False
    assert finding.status == host_identity.INVALID
    assert "bad option" in finding.detail


def test_a_check_that_could_not_run_is_never_reported_as_validated():
    """The reproduction: ok=True used to be the only way to say "not now"."""

    service = HostIdentityService(
        runner=RecordingRunner(
            {("sshd", ("-t",)): (255, "", f"{host_identity.PRIVSEP_MISSING} /run/sshd")}
        ),
        root=Path("/nonexistent"),
    )

    finding = service.validate_sshd()

    assert finding.status == host_identity.NOT_READY
    assert finding.status != host_identity.VALID
    assert "not checked" in finding.detail


def test_an_uninstallable_sshd_is_not_ready_rather_than_invalid():
    class Absent(RecordingRunner):
        def run(self, tool, args=(), **kwargs):
            raise CommandError("tool_unavailable", "sshd is not installed on this host")

    service = HostIdentityService(runner=Absent({}), root=Path("/nonexistent"))

    finding = service.validate_sshd()

    assert finding.status == host_identity.NOT_READY
    assert finding.ok is False


def test_the_three_states_are_distinct():
    assert len({host_identity.VALID, host_identity.NOT_READY, host_identity.INVALID}) == 3
