# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whether the appliance's SSH identity is durable, and whether it is its own.

The A/B design says the host identity survives a power loss and a slot switch.
Two things had to hold for that claim and neither did:

- ``fsync`` results were discarded. Every flush of a private key, a public key,
  the key directory and its persistent parent could fail and the service still
  reported a successful initialization, so the first boot could hand back a
  fingerprint that was never on the medium.

- the verifier read the private key and the ``.pub`` file separately and checked
  neither against the other. Replacing only the ``.pub`` produced a report — and
  a support bundle — whose fingerprint was not the one sshd would offer, which
  is precisely the substitution a host-key fingerprint exists to detect.

``ssh-keygen`` is real here. Deriving a public key from a private key is the
check under test, so a fake that returned the file it was supposed to be
cross-checking would prove nothing.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from appliance import host_identity
from appliance.commands import CommandResult, RecordingRunner
from appliance.host_identity import (
    HostIdentityError,
    HostIdentityService,
    private_key_name,
    public_key_name,
)

pytestmark = [pytest.mark.unit, pytest.mark.simulation]

ROOT = Path(__file__).resolve().parents[1]
MACHINE_ID = "0123456789abcdef0123456789abcdef"
KEY_DIRECTORY = "var/lib/ems-appliance-manager/ssh"

requires_keygen = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None, reason="ssh-keygen is not installed"
)


class RealKeygenRunner(RecordingRunner):
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


def appliance(tmp_path):
    root = tmp_path / "slot"
    (root / "var/lib/ems-appliance-manager").mkdir(parents=True, exist_ok=True)
    (root / "persistent/common/etc").mkdir(parents=True, exist_ok=True)
    (root / "etc").mkdir(parents=True, exist_ok=True)
    (root / "persistent/common/etc/machine-id").write_text(MACHINE_ID + "\n")
    (root / "etc/machine-id").write_text(MACHINE_ID + "\n")
    network = root / "etc/NetworkManager/system-connections"
    network.mkdir(parents=True, exist_ok=True)
    network.chmod(0o700)
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


def key_path(root, key_type, *, public=False):
    name = public_key_name(key_type) if public else private_key_name(key_type)
    return root / KEY_DIRECTORY / name


# --- finding 6: every durability operation is authoritative -------------------


class FailingSync:
    """An ``fsync`` that fails for the paths a test names, and only those."""

    def __init__(self, *, match=lambda _path: True):
        self.match = match
        self.attempts = []
        self._real = os.fsync

    def install(self, monkeypatch):
        opened = {}
        real_open = os.open

        def tracking_open(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            opened[handle] = str(path)
            return handle

        def fsync(handle):
            path = opened.get(handle, "")
            self.attempts.append(path)
            if self.match(path):
                raise OSError(5, "Input/output error")
            return self._real(handle)

        monkeypatch.setattr(os, "open", tracking_open)
        monkeypatch.setattr(os, "fsync", fsync)
        return self


@requires_keygen
def test_a_private_key_that_cannot_be_flushed_fails_initialization(tmp_path, monkeypatch):
    root = appliance(tmp_path)
    FailingSync(match=lambda path: path.endswith(".new")).install(monkeypatch)

    report = service(root).ensure()

    assert not report.ok
    assert any("host_identity_not_durable" in problem for problem in report.problems)
    assert report.created == ()


@requires_keygen
def test_a_public_key_that_cannot_be_flushed_fails_initialization(tmp_path, monkeypatch):
    root = appliance(tmp_path)
    FailingSync(match=lambda path: path.endswith(".new.pub")).install(monkeypatch)

    report = service(root).ensure()

    assert not report.ok
    assert any("host_identity_not_durable" in problem for problem in report.problems)


@requires_keygen
def test_a_key_directory_that_cannot_be_flushed_fails_initialization(tmp_path, monkeypatch):
    root = appliance(tmp_path)
    directory = str(root / KEY_DIRECTORY)
    FailingSync(match=lambda path: path == directory).install(monkeypatch)

    report = service(root).ensure()

    assert not report.ok
    assert any("host_identity_not_durable" in problem for problem in report.problems)


@requires_keygen
def test_a_persistent_parent_that_cannot_be_flushed_fails_initialization(
    tmp_path, monkeypatch
):
    root = appliance(tmp_path)
    parent = str((root / KEY_DIRECTORY).parent)
    FailingSync(match=lambda path: path == parent).install(monkeypatch)

    report = service(root).ensure()

    assert not report.ok
    assert any("host_identity_not_durable" in problem for problem in report.problems)


@requires_keygen
def test_every_flush_is_actually_attempted(tmp_path, monkeypatch):
    root = appliance(tmp_path)
    watcher = FailingSync(match=lambda _path: False).install(monkeypatch)

    report = service(root).ensure()

    assert report.ok, report.problems
    directory = str(root / KEY_DIRECTORY)
    assert directory in watcher.attempts
    assert str(Path(directory).parent) in watcher.attempts
    assert any(path.endswith(".new") for path in watcher.attempts)
    assert any(path.endswith(".new.pub") for path in watcher.attempts)


@requires_keygen
def test_a_partially_committed_first_boot_is_retried_without_losing_a_key(
    tmp_path, monkeypatch
):
    """One key type committed, another not. The good key is never discarded."""

    root = appliance(tmp_path)
    first = host_identity.HOST_KEY_TYPES[0]
    second = host_identity.HOST_KEY_TYPES[1]
    FailingSync(match=lambda path: second in path).install(monkeypatch)
    failed = service(root).ensure()

    assert not failed.ok
    assert first in failed.created
    assert not key_path(root, second).exists()

    monkeypatch.undo()
    fingerprint = service(root).fingerprints()[first]
    recovered = service(root).ensure()

    assert recovered.ok, recovered.problems
    assert first in recovered.reused
    assert second in recovered.created
    assert recovered.fingerprints[first] == fingerprint


# --- finding 7: the reported identity is the one sshd serves ------------------


@requires_keygen
def test_a_replaced_public_key_is_refused(tmp_path):
    root = appliance(tmp_path)
    service(root).ensure()
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "", "-f", str(other / "key")],
        check=True,
        timeout=120,
    )
    replacement = (other / "key.pub").read_text(encoding="utf-8")
    key_path(root, "ed25519", public=True).write_text(replacement, encoding="utf-8")

    report = service(root).verify()

    assert not report.ok
    assert any("host_identity_keypair_mismatch" in problem for problem in report.problems)


@requires_keygen
def test_the_reported_fingerprint_comes_from_the_private_key(tmp_path):
    root = appliance(tmp_path)
    probe = service(root)
    probe.ensure()
    derived = subprocess.run(
        ["ssh-keygen", "-y", "-f", str(key_path(root, "ed25519"))],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    ).stdout.split()[1]

    from appliance.sshkeys import fingerprint_of

    assert probe.fingerprints()["ed25519"] == fingerprint_of(derived)


@requires_keygen
def test_a_replaced_public_key_never_reaches_a_report(tmp_path):
    root = appliance(tmp_path)
    probe = service(root)
    probe.ensure()
    expected = probe.fingerprints()["ed25519"]
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "", "-f", str(other / "key")],
        check=True,
        timeout=120,
    )
    key_path(root, "ed25519", public=True).write_text(
        (other / "key.pub").read_text(encoding="utf-8"), encoding="utf-8"
    )

    report = service(root).verify()

    assert report.fingerprints.get("ed25519") in (None, expected)
    assert not report.ok


@requires_keygen
def test_a_public_key_of_the_wrong_algorithm_is_refused(tmp_path):
    root = appliance(tmp_path)
    service(root).ensure()
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ecdsa", "-N", "", "-C", "", "-f", str(other / "key")],
        check=True,
        timeout=120,
    )
    key_path(root, "ed25519", public=True).write_text(
        (other / "key.pub").read_text(encoding="utf-8"), encoding="utf-8"
    )

    report = service(root).verify()

    assert not report.ok


@requires_keygen
def test_a_comment_difference_alone_is_not_a_mismatch(tmp_path):
    root = appliance(tmp_path)
    service(root).ensure()
    public = key_path(root, "ed25519", public=True)
    key_type, blob = public.read_text(encoding="utf-8").split()[:2]
    public.write_text(f"{key_type} {blob} root@appliance\n", encoding="utf-8")

    report = service(root).verify()

    assert report.ok, report.problems


@requires_keygen
def test_a_keypair_that_cannot_be_derived_is_not_silently_accepted(tmp_path):
    root = appliance(tmp_path)
    service(root).ensure()
    key_path(root, "ed25519").write_text("not a private key\n", encoding="utf-8")
    key_path(root, "ed25519").chmod(0o600)

    report = service(root).verify()

    assert not report.ok


@requires_keygen
def test_ssh_stays_blocked_while_the_identity_is_incomplete(tmp_path):
    root = appliance(tmp_path)
    service(root).ensure()
    key_path(root, "ed25519").unlink()

    report = service(root).verify()

    assert not report.ok
    assert not host_identity.ssh_may_start(report)


@requires_keygen
def test_ssh_may_start_once_every_declared_key_is_proven(tmp_path):
    root = appliance(tmp_path)

    report = service(root).ensure()

    assert host_identity.ssh_may_start(report)


# --- finding 8: the permission fixture does not depend on the runner's uid ----


def owned_by_another(monkeypatch, target, *, uid=65534):
    """Make exactly ``target`` report a non-root owner, whoever runs the test.

    ``chown`` is not a portable fixture here: an unprivileged user cannot use it
    at all, and inside a user namespace only one uid is mapped, so it fails with
    EINVAL. The production check reads ``st_uid``, so that is what is controlled
    — the branch under test is the real one either way.
    """

    real = Path.stat

    def stat(self, *args, **kwargs):
        info = real(self, *args, **kwargs)
        if Path(self) != Path(target):
            return info
        fields = list(info[:10])
        fields[4] = uid
        fields[5] = uid
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "stat", stat)
    return target


def test_a_key_directory_owned_by_another_account_is_refused(tmp_path, monkeypatch):
    root = appliance(tmp_path)
    directory = root / KEY_DIRECTORY
    directory.mkdir(parents=True)
    directory.chmod(0o700)
    owned_by_another(monkeypatch, directory)

    with pytest.raises(HostIdentityError) as excinfo:
        service(root, require_root=True).verify_directory()

    assert excinfo.value.code == "host_key_directory_owner_wrong"


@requires_keygen
def test_a_private_key_owned_by_another_account_is_refused(tmp_path, monkeypatch):
    root = appliance(tmp_path)
    service(root).ensure()
    owned_by_another(monkeypatch, key_path(root, "ed25519"))

    with pytest.raises(HostIdentityError) as excinfo:
        service(root, require_root=True).verify_key("ed25519")

    assert excinfo.value.code == "host_key_owner_wrong"


def test_the_owner_fixture_is_the_same_under_root_and_a_normal_user(tmp_path, monkeypatch):
    """No permission assertion may depend on the uid the suite happens to run as."""

    root = appliance(tmp_path)
    directory = root / KEY_DIRECTORY
    directory.mkdir(parents=True)
    directory.chmod(0o700)

    owned_by_another(monkeypatch, directory, uid=0)
    assert service(root, require_root=True).verify_directory() is True

    owned_by_another(monkeypatch, directory)
    with pytest.raises(HostIdentityError):
        service(root, require_root=True).verify_directory()


def test_the_root_only_checks_are_skipped_deterministically(tmp_path):
    root = appliance(tmp_path)
    directory = root / KEY_DIRECTORY
    directory.mkdir(parents=True)
    directory.chmod(0o700)

    assert service(root, require_root=False).verify_directory() is True


# --- phase 12: the lifecycle the units express -------------------------------

UNIT = ROOT / "packaging/appliance/systemd/ems-appliance-host-identity.service"
SSH_DROP_IN = (
    ROOT
    / "packaging/appliance/image/layer/ems-appliance.rootfs-overlay"
    / "etc/systemd/system/ssh.service.d/50-ems-appliance-host-identity.conf"
)


def test_the_identity_is_established_only_after_the_persistent_partition():
    text = UNIT.read_text(encoding="utf-8")

    assert "RequiresMountsFor=/persistent" in text
    assert "After=local-fs.target" in text
    assert "RequiresMountsFor=/var/lib/ems-appliance-manager" in text


def test_the_persistence_verification_runs_after_the_identity():
    assert "Before=ems-appliance-persistence.service" in UNIT.read_text(encoding="utf-8")


def test_sshd_is_blocked_rather_than_ordered_after_a_failed_identity():
    """``Requires=``, not ``After=``. An unprovable identity offers no SSH."""

    text = SSH_DROP_IN.read_text(encoding="utf-8")

    assert "Requires=ems-appliance-host-identity.service" in text
    assert "After=ems-appliance-host-identity.service" in text


def test_the_unit_never_regenerates_on_a_later_boot():
    assert "--ensure" in UNIT.read_text(encoding="utf-8")


@requires_keygen
def test_a_first_boot_creates_the_keys_and_a_trial_slot_reuses_them(tmp_path):
    """Slot A creates the identity; slot B's trial finds the same fingerprints.

    Both slots see the same persistent tree, which is the whole mechanism: a
    slot that generated its own keys would present the appliance to the network
    as a different host after every OS update.
    """

    root = appliance(tmp_path)
    first = service(root).ensure()
    assert sorted(first.created) == sorted(host_identity.HOST_KEY_TYPES)

    # The trial slot: a different root filesystem seeing the same persistent
    # subtree, which is what image-rota's bind mount produces — the same bytes
    # at the same path, not a link the identity service would refuse.
    trial = appliance(tmp_path / "slot-b")
    shutil.rmtree(trial / KEY_DIRECTORY, ignore_errors=True)
    shutil.copytree(root / KEY_DIRECTORY, trial / KEY_DIRECTORY)

    second = service(trial).ensure()

    assert second.created == ()
    assert sorted(second.reused) == sorted(host_identity.HOST_KEY_TYPES)
    assert second.fingerprints == first.fingerprints
    assert host_identity.ssh_may_start(second)
