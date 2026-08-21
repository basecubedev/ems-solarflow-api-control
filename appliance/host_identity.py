# SPDX-License-Identifier: AGPL-3.0-or-later
"""The identity an appliance keeps across a slot switch, established once.

An A/B appliance is one host with two root filesystems. Anything that makes it
recognisable to the network has to live on the persistent partition and be
created exactly once: an SSH host key regenerated per slot would change the
appliance's fingerprint on every OS update, which is indistinguishable from the
attack the fingerprint exists to detect.

``/etc/ssh`` is deliberately not shared — each slot keeps its own distro
configuration — so only the key files are, named by a project drop-in. This
module owns creating and proving them, and nothing else may write them.

Order matters and is enforced by the unit, not by convention:

    persistent partition mounted
      → host identity established here
        → persistence verified
          → sshd, NetworkManager, the appliance services

Failures are terminal. sshd requires this unit, so an appliance whose key
directory cannot be proven does not offer SSH at all rather than offering it
under an identity nobody can vouch for.
"""

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from appliance import ab_persistence
from appliance.commands import CommandError

# Exactly the types the sshd drop-in names. A key type generated here that the
# drop-in does not reference would never be offered; one the drop-in names and
# this does not create would stop sshd from starting.
HOST_KEY_TYPES = ("ed25519", "rsa", "ecdsa")

KEY_DIRECTORY = ab_persistence.SSH_HOST_KEY_DIRECTORY
DROP_IN = ab_persistence.SSH_DROP_IN

DIRECTORY_MODE = 0o700
PRIVATE_MODE = 0o600
PUBLIC_MODE = 0o644

NETWORK_PROFILE_DIRECTORY = "/etc/NetworkManager/system-connections"
NETWORK_PROFILE_MODE = 0o700

RSA_BITS = "4096"
PRIVSEP_MISSING = "Missing privilege separation directory"

# A check that could not run and a check that passed are different answers.
# "ok" alone could not tell them apart, and "sshd accepted its configuration"
# is not something this appliance may report when sshd never read it.
VALID = "valid"
NOT_READY = "not_ready"
INVALID = "invalid"


class HostIdentityError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Finding:
    name: str
    ok: bool
    detail: str = ""
    state: str = ""

    @property
    def status(self):
        return self.state or (VALID if self.ok else INVALID)

    def to_dict(self):
        return {"name": self.name, "ok": self.ok, "state": self.status, "detail": self.detail}


@dataclass(frozen=True)
class IdentityReport:
    findings: tuple = ()
    created: tuple = ()
    reused: tuple = ()
    fingerprints: dict = field(default_factory=dict)
    problems: tuple = ()

    @property
    def ok(self):
        return not self.problems and all(finding.ok for finding in self.findings)

    def to_dict(self):
        return {
            "ok": self.ok,
            "created": list(self.created),
            "reused": list(self.reused),
            # Public fingerprints only. A private key never reaches a report, a
            # log line or a support bundle.
            "fingerprints": dict(self.fingerprints),
            "findings": [finding.to_dict() for finding in self.findings],
            "problems": list(self.problems),
        }


def private_key_name(key_type):
    return f"ssh_host_{key_type}_key"


def public_key_name(key_type):
    return f"{private_key_name(key_type)}.pub"


def ssh_may_start(report):
    """sshd is offered only once every declared key is proven.

    An incomplete identity is not a degraded service to run anyway: an
    appliance answering under a key nobody can vouch for is the state the
    fingerprint exists to make visible.
    """

    return bool(getattr(report, "ok", False))


def _key_material_of(text):
    """``<type> <base64>`` from an OpenSSH public key line, comment dropped.

    The comment is not part of the key. ``ssh-keygen -y`` emits none and a
    ``.pub`` on disk usually carries one, so comparing whole lines would report
    a mismatch for two identical keys.
    """

    for line in str(text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            return f"{parts[0]} {parts[1]}"
    return ""


def _key_material(path):
    try:
        return _key_material_of(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""


def _stated(exc):
    """A problem an operator can look up: the stable code, then the detail."""

    return f"{exc.code}: {exc.message}"


class HostIdentityService:
    """Establish and prove the identity both slots share."""

    def __init__(
        self,
        *,
        runner=None,
        root="/",
        key_directory=KEY_DIRECTORY,
        key_types=HOST_KEY_TYPES,
        network_directory=NETWORK_PROFILE_DIRECTORY,
        machine_id_source=ab_persistence.MACHINE_ID_SOURCE,
        require_root=None,
        persistent_mounts=None,
    ):
        self.runner = runner
        self.root = Path(root)
        self.key_directory = str(key_directory)
        self.key_types = tuple(key_types)
        self.network_directory = str(network_directory)
        self.machine_id_source = str(machine_id_source)
        self.require_root = os.geteuid() == 0 if require_root is None else bool(require_root)
        self._persistent_mounts = persistent_mounts

    # --- paths -------------------------------------------------------------

    def _path(self, absolute):
        return self.root / str(absolute).lstrip("/")

    @property
    def directory(self):
        return self._path(self.key_directory)

    def private_key(self, key_type):
        return self.directory / private_key_name(key_type)

    def public_key(self, key_type):
        return self.directory / public_key_name(key_type)

    # --- verification ------------------------------------------------------

    def verify_on_persistent_partition(self):
        """The key directory has to be the shared one, not the slot's own /var.

        This is the one state-writing unit that cannot order itself after the
        persistence verification -- it deliberately runs before it, because a
        shared path whose ownership it just corrected has to be checked
        afterwards. So the check the unit cannot express belongs here: if a
        shared bind was silently skipped the directory still exists, on the
        slot's own filesystem, and an identity minted there is lost at the next
        slot switch while every client sees a changed host key.
        """

        if self._persistent_mounts is None:
            return
        mounts = self._persistent_mounts() or {}
        target = str(self.key_directory)
        enclosing = ""
        for mountpoint in mounts:
            point = str(mountpoint)
            if (target == point or target.startswith(point.rstrip("/") + "/")) and len(
                point
            ) > len(enclosing):
                enclosing = point
        if not enclosing:
            raise HostIdentityError(
                "host_identity_not_shared",
                f"{target} is not backed by the persistent partition, so a host key "
                "created there would not survive a slot switch",
            )

    def verify_directory(self):
        """The key directory must be a real, root-owned, private directory.

        A symlink here is the whole attack: it would let anything that could
        create one decide where sshd reads its private keys from.
        """

        target = self.directory
        if target.is_symlink():
            raise HostIdentityError(
                "host_key_directory_not_a_directory",
                f"{self.key_directory} is a symlink; host keys are never followed through one",
            )
        if not target.is_dir():
            raise HostIdentityError(
                "host_key_directory_missing", f"{self.key_directory} is not a directory"
            )
        info = target.stat()
        if self.require_root and info.st_uid != 0:
            raise HostIdentityError(
                "host_key_directory_owner_wrong",
                f"{self.key_directory} is owned by uid {info.st_uid}, not root",
            )
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o077:
            raise HostIdentityError(
                "host_key_directory_mode_wrong",
                f"{self.key_directory} is mode {mode:04o}; host keys need {DIRECTORY_MODE:04o}",
            )
        return True

    def verify_key(self, key_type):
        """One key pair, proven before sshd is allowed to read it."""

        private = self.private_key(key_type)
        public = self.public_key(key_type)
        if private.is_symlink() or public.is_symlink():
            raise HostIdentityError(
                "host_key_not_a_regular_file",
                f"{private.name} is a symlink; a host key is never read through one",
            )
        if not private.is_file():
            raise HostIdentityError(
                "host_key_missing", f"{private.name} is missing from {self.key_directory}"
            )
        if not public.is_file():
            raise HostIdentityError(
                "host_key_missing", f"{public.name} is missing from {self.key_directory}"
            )
        info = private.stat()
        if self.require_root and info.st_uid != 0:
            raise HostIdentityError(
                "host_key_owner_wrong",
                f"{private.name} is owned by uid {info.st_uid}, not root",
            )
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o077:
            raise HostIdentityError(
                "host_key_mode_wrong",
                f"{private.name} is mode {mode:04o}; a private host key needs {PRIVATE_MODE:04o}",
            )
        self.verify_keypair(key_type)
        return True

    def verify_keypair(self, key_type):
        """The ``.pub`` beside a private key has to be that key's public half.

        Checking both files structurally proves nothing about their
        relationship: replacing only the ``.pub`` yields a status report and a
        support bundle carrying a fingerprint that is not the one sshd offers,
        which is exactly the substitution a host-key fingerprint exists to
        detect. So the public key is derived from the private key and compared.
        """

        derived = self.derived_public_key(key_type)
        declared = _key_material(self.public_key(key_type))
        if not declared:
            raise HostIdentityError(
                "host_key_unreadable",
                f"{public_key_name(key_type)} carries no usable key material",
            )
        if derived != declared:
            raise HostIdentityError(
                "host_identity_keypair_mismatch",
                f"{public_key_name(key_type)} is not the public half of "
                f"{private_key_name(key_type)}",
            )
        return True

    def derived_public_key(self, key_type):
        """``ssh-keygen -y``: the public key the private key actually has.

        Fixed argv through the allowlisted runner. Nothing derived here is ever
        logged, and the private key never leaves the file it is in.
        """

        if self.runner is None:
            raise HostIdentityError(
                "host_key_verification_unavailable",
                "no command runner is configured, so host keys cannot be verified",
            )
        private = self.private_key(key_type)
        try:
            result = self.runner.run("ssh-keygen", ["-y", "-f", str(private)], timeout=60)
        except CommandError as exc:
            raise HostIdentityError("host_key_unreadable", exc.message)
        if not getattr(result, "ok", False):
            raise HostIdentityError(
                "host_key_unreadable",
                f"{private.name} could not be read as a private host key",
            )
        material = _key_material_of(getattr(result, "stdout", ""))
        if not material:
            raise HostIdentityError(
                "host_key_unreadable",
                f"no public key could be derived from {private.name}",
            )
        return material

    # --- establishing it ---------------------------------------------------

    def verify(self):
        """Prove the identity without creating anything."""

        return self._establish(create=False)

    def ensure(self):
        """Create what is missing, prove what is there, and never regenerate.

        Idempotent by construction: an existing key is validated and left
        exactly as it is, so the fingerprint an operator verified on first boot
        survives every slot switch afterwards.
        """

        return self._establish(create=True)

    def _establish(self, *, create):

        findings = []
        created, reused, problems = [], [], []

        try:
            self.verify_on_persistent_partition()
            if create:
                self._ensure_directory()
            self.verify_directory()
            findings.append(Finding("key_directory", True, self.key_directory))
        except HostIdentityError as exc:
            return IdentityReport(
                findings=(Finding("key_directory", False, _stated(exc)),),
                problems=(_stated(exc),),
            )

        for key_type in self.key_types:
            try:
                present = (
                    self.private_key(key_type).exists()
                    or self.public_key(key_type).exists()
                )
                if present or not create:
                    if create:
                        self._recover_public_half(key_type)
                    self.verify_key(key_type)
                    reused.append(key_type)
                else:
                    # Only what is absent. An existing key is never replaced,
                    # because its fingerprint is the appliance's identity.
                    self._generate(key_type)
                    self.verify_key(key_type)
                    created.append(key_type)
                findings.append(Finding(f"host_key:{key_type}", True, "present and private"))
            except HostIdentityError as exc:
                findings.append(Finding(f"host_key:{key_type}", False, _stated(exc)))
                problems.append(_stated(exc))

        findings.append(self._machine_identity_finding())
        findings.append(self._network_directory_finding())
        findings.append(self._drop_in_finding())

        problems.extend(finding.detail for finding in findings if not finding.ok)
        return IdentityReport(
            findings=tuple(findings),
            created=tuple(created),
            reused=tuple(reused),
            fingerprints=self.fingerprints(),
            problems=tuple(sorted(set(problems))),
        )

    def _ensure_directory(self):
        target = self.directory
        if target.is_symlink():
            raise HostIdentityError(
                "host_key_directory_not_a_directory",
                f"{self.key_directory} is a symlink and is never replaced automatically",
            )
        try:
            target.mkdir(parents=True, exist_ok=True)
            os.chmod(target, DIRECTORY_MODE)
            if self.require_root:
                os.chown(target, 0, 0)
        except OSError as exc:
            raise HostIdentityError(
                "host_key_directory_unusable",
                f"{self.key_directory} could not be prepared: {exc}",
            )
        return target

    def _recover_public_half(self, key_type):
        """Finish a placement a crash interrupted. Never a new identity.

        The private key is renamed into place first, so the window a power loss
        can land in leaves a valid secret with no ``.pub`` beside it. Retrying
        then found "something is present", tried to verify a pair, and failed
        for as long as the appliance existed.

        The public half is derived from the secret rather than invented, so the
        fingerprint an operator verified on first boot is preserved. The mirror
        case has no secret to derive from: a ``.pub`` alone is refused rather
        than silently replaced by a new identity, because that is exactly the
        substitution a host key fingerprint exists to detect.
        """

        private = self.private_key(key_type)
        public = self.public_key(key_type)
        if private.is_symlink() or public.is_symlink():
            return False
        if not private.is_file():
            if public.is_file():
                raise HostIdentityError(
                    "host_key_private_half_missing",
                    f"{public_key_name(key_type)} is present without "
                    f"{private_key_name(key_type)}; this identity cannot be recovered",
                )
            return False
        if public.is_file():
            return False

        material = self.derived_public_key(key_type)
        staging = self.directory / f".{public_key_name(key_type)}.recovered"
        try:
            handle = os.open(
                str(staging), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PUBLIC_MODE
            )
            try:
                os.write(handle, f"{material} ems-appliance\n".encode())
            finally:
                os.close(handle)
            os.chmod(staging, PUBLIC_MODE)
            if self.require_root:
                os.chown(staging, 0, 0)
        except OSError as exc:
            raise HostIdentityError(
                "host_key_placement_failed",
                f"the {key_type} public host key could not be rebuilt: {exc}",
            )
        self._sync(staging)
        try:
            os.replace(staging, public)
        except OSError as exc:
            raise HostIdentityError(
                "host_key_placement_failed",
                f"the {key_type} public host key could not be placed: {exc}",
            )
        self._sync_directory()
        return True

    def _generate(self, key_type):
        """One key pair, placed atomically so a crash leaves nothing partial."""

        if self.runner is None:
            raise HostIdentityError(
                "host_key_generation_unavailable",
                "no command runner is configured, so host keys cannot be generated",
            )
        staging = self.directory / f".{private_key_name(key_type)}.new"
        for leftover in (staging, Path(f"{staging}.pub")):
            try:
                leftover.unlink()
            except OSError:
                pass

        arguments = ["-q", "-t", key_type, "-N", "", "-C", "", "-f", str(staging)]
        if key_type == "rsa":
            arguments = ["-q", "-t", key_type, "-b", RSA_BITS, "-N", "", "-C", "", "-f", str(staging)]
        try:
            result = self.runner.run("ssh-keygen", arguments, timeout=120)
        except CommandError as exc:
            raise HostIdentityError("host_key_generation_failed", exc.message)
        if not getattr(result, "ok", False):
            raise HostIdentityError(
                "host_key_generation_failed",
                f"ssh-keygen could not create an {key_type} host key",
            )

        try:
            os.chmod(staging, PRIVATE_MODE)
            os.chmod(f"{staging}.pub", PUBLIC_MODE)
            if self.require_root:
                os.chown(staging, 0, 0)
                os.chown(f"{staging}.pub", 0, 0)
        except OSError as exc:
            raise HostIdentityError(
                "host_key_placement_failed", f"the {key_type} host key could not be placed: {exc}"
            )

        # Every flush below is authoritative. The whole claim this module makes
        # is that an identity established once survives a power loss, and a
        # flush whose result was discarded proves nothing about the medium: the
        # first boot would hand back a fingerprint that was never written.
        self._sync(staging)
        self._sync(f"{staging}.pub")
        try:
            os.replace(staging, self.private_key(key_type))
            os.replace(f"{staging}.pub", self.public_key(key_type))
        except OSError as exc:
            raise HostIdentityError(
                "host_key_placement_failed", f"the {key_type} host key could not be placed: {exc}"
            )
        self._sync_directory()
        self._sync_parent()
        return key_type

    def _sync(self, path):
        """Flush one path, or fail. There is no third outcome worth having."""

        try:
            handle = os.open(str(path), os.O_RDONLY)
        except OSError as exc:
            raise HostIdentityError(
                "host_identity_not_durable", f"{path} could not be opened to flush it: {exc}"
            )
        try:
            os.fsync(handle)
        except OSError as exc:
            raise HostIdentityError(
                "host_identity_not_durable", f"{path} could not be flushed to the medium: {exc}"
            )
        finally:
            os.close(handle)
        return True

    def _sync_directory(self):
        return self._sync(self.directory)

    def _sync_parent(self):
        """The persistent directory the key directory was created in.

        A directory entry is only durable once the directory that holds it is.
        """

        return self._sync(self.directory.parent)

    # --- what the report says ----------------------------------------------

    def fingerprints(self):
        """Public fingerprints, derived from the private keys sshd serves.

        Reading the ``.pub`` file would report whatever is in it. What an
        operator compares against a login prompt has to come from the key
        itself, so a replaced ``.pub`` produces no fingerprint at all rather
        than a plausible wrong one.
        """

        from appliance.sshkeys import fingerprint_of

        found = {}
        for key_type in self.key_types:
            if not self.private_key(key_type).is_file():
                continue
            try:
                material = self.derived_public_key(key_type)
            except HostIdentityError:
                continue
            try:
                found[key_type] = fingerprint_of(material.split()[1])
            except Exception:
                continue
        return found

    def _machine_identity_finding(self):
        """Upstream owns the synchronisation; this only proves it happened."""

        shared = self._read(self._path(self.machine_id_source))
        running = self._read(self._path("/etc/machine-id"))
        if not shared:
            return Finding(
                "machine_identity",
                False,
                f"{self.machine_id_source} carries no machine identity",
            )
        if shared != running:
            return Finding(
                "machine_identity",
                False,
                f"/etc/machine-id does not come from {self.machine_id_source}",
            )
        return Finding("machine_identity", True, "stable across both slots")

    def _network_directory_finding(self):
        target = self._path(self.network_directory)
        if not target.is_dir():
            return Finding(
                "network_profiles", False, f"{self.network_directory} is not a directory"
            )
        mode = stat.S_IMODE(target.stat().st_mode)
        if mode & 0o077:
            try:
                os.chmod(target, NETWORK_PROFILE_MODE)
            except OSError as exc:
                return Finding(
                    "network_profiles",
                    False,
                    f"{self.network_directory} is mode {mode:04o} and could not be tightened: {exc}",
                )
        return Finding("network_profiles", True, f"{self.network_directory} is private")

    def _drop_in_finding(self):
        """sshd must be pointed at exactly these files and nothing else."""

        drop_in = self._path(DROP_IN)
        try:
            text = drop_in.read_text(encoding="utf-8")
        except OSError:
            return Finding("sshd_drop_in", False, f"{DROP_IN} is missing")
        declared = {
            line.split(None, 1)[1].strip()
            for line in text.splitlines()
            if line.strip().startswith("HostKey ")
        }
        expected = {
            f"{self.key_directory}/{private_key_name(key_type)}" for key_type in self.key_types
        }
        if declared != expected:
            return Finding(
                "sshd_drop_in",
                False,
                f"{DROP_IN} names {sorted(declared)}, the initializer creates {sorted(expected)}",
            )
        return Finding("sshd_drop_in", True, f"{DROP_IN} names the persistent host keys")

    def _read(self, path):
        try:
            return path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            return ""

    # --- sshd --------------------------------------------------------------

    def validate_sshd(self):
        """``sshd -t``. A configuration it refuses must not start.

        Three answers, not two: sshd accepted the configuration, sshd refused
        it, or sshd could not be asked yet. The third is not a pass — it is a
        check that has not happened — and it is reported as ``not_ready`` so
        nothing downstream can read it as validated SSH configuration.
        """

        if self.runner is None:
            return Finding(
                "sshd_config", False, "no command runner is configured", state=NOT_READY
            )
        try:
            result = self.runner.run("sshd", ["-t"], timeout=30)
        except CommandError as exc:
            if exc.code == "tool_unavailable":
                return Finding("sshd_config", False, exc.message, state=NOT_READY)
            return Finding("sshd_config", False, exc.message, state=INVALID)
        if not getattr(result, "ok", False):
            stderr = (getattr(result, "stderr", "") or "").strip()
            # This unit is ordered Before=ssh.service, so /run/sshd — which is
            # ssh.service's own RuntimeDirectory — does not exist yet. sshd then
            # refuses to start for that reason alone, which says nothing about
            # the configuration. Failing here fails the unit, and ssh.service
            # Requires= this one, so SSH could never come up at all.
            if PRIVSEP_MISSING in stderr:
                return Finding(
                    "sshd_config",
                    True,
                    "not checked: sshd's runtime directory /run/sshd does not exist yet; "
                    "systemd creates it when ssh.service starts",
                    state=NOT_READY,
                )
            return Finding(
                "sshd_config",
                False,
                (stderr or "sshd refused its configuration")[:200],
                state=INVALID,
            )
        return Finding(
            "sshd_config", True, "sshd accepts its configuration", state=VALID
        )
