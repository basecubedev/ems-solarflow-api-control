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

    def to_dict(self):
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


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
    ):
        self.runner = runner
        self.root = Path(root)
        self.key_directory = str(key_directory)
        self.key_types = tuple(key_types)
        self.network_directory = str(network_directory)
        self.machine_id_source = str(machine_id_source)
        self.require_root = os.geteuid() == 0 if require_root is None else bool(require_root)

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
        return True

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
            if create:
                self._ensure_directory()
            self.verify_directory()
            findings.append(Finding("key_directory", True, self.key_directory))
        except HostIdentityError as exc:
            return IdentityReport(
                findings=(Finding("key_directory", False, exc.message),),
                problems=(exc.message,),
            )

        for key_type in self.key_types:
            try:
                present = (
                    self.private_key(key_type).exists()
                    or self.public_key(key_type).exists()
                )
                if present or not create:
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
                findings.append(Finding(f"host_key:{key_type}", False, exc.message))
                problems.append(exc.message)

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
            self._sync(staging)
            os.replace(staging, self.private_key(key_type))
            os.replace(f"{staging}.pub", self.public_key(key_type))
            self._sync_directory()
        except OSError as exc:
            raise HostIdentityError(
                "host_key_placement_failed", f"the {key_type} host key could not be placed: {exc}"
            )
        return key_type

    def _sync(self, path):
        try:
            handle = os.open(str(path), os.O_RDONLY)
        except OSError:
            return False
        try:
            os.fsync(handle)
        finally:
            os.close(handle)
        return True

    def _sync_directory(self):
        return self._sync(self.directory)

    # --- what the report says ----------------------------------------------

    def fingerprints(self):
        """Public fingerprints, which are the only key material ever reported."""

        found = {}
        for key_type in self.key_types:
            public = self.public_key(key_type)
            if not public.is_file():
                continue
            try:
                blob = public.read_text(encoding="utf-8", errors="replace").split()
            except OSError:
                continue
            if len(blob) >= 2:
                from appliance.sshkeys import fingerprint_of

                try:
                    found[key_type] = fingerprint_of(blob[1])
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
        """``sshd -t``. A configuration it refuses must not start."""

        if self.runner is None:
            return Finding("sshd_config", False, "no command runner is configured")
        try:
            result = self.runner.run("sshd", ["-t"], timeout=30)
        except CommandError as exc:
            return Finding("sshd_config", False, exc.message)
        if not getattr(result, "ok", False):
            return Finding(
                "sshd_config",
                False,
                (getattr(result, "stderr", "") or "sshd refused its configuration").strip()[:200],
            )
        return Finding("sshd_config", True, "sshd accepts its configuration")
