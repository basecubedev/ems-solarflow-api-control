# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rebuilding the application a freshly written slot does not have.

``/var/lib/docker`` is per-slot: ``image-rota`` binds ``/var`` from
``/persistent/slots/system_<slot>/var``, which is exactly what keeps a rollback
from handing a newer content store to an older engine. The cost is that a slot
that has just been written has an empty image store, so the containers an
operator depends on are not there yet.

What the source slot records is therefore not a list of images but a deployment
authority: the compose file and the environment beside it, by digest, and every
service with the exact image digest it ran and the state it was in. That object
is fingerprinted, the fingerprint is bound into the pending trial, and the
target slot proves it again before it touches Docker.

Two rules follow from that and are the reason this module exists:

- an identity is a digest. A tag can point somewhere else by the time the other
  slot rebuilds from it, and ``docker load`` printing a name says nothing about
  which digest entered the store.
- a deployment that changed after the plan was made is not this deployment. It
  is refused, never re-recorded: a new deployment needs a new plan, because the
  operator confirmed the old one.

A slot that cannot rebuild its application does not become known-good. An OS
update that leaves an appliance with no way back to the Admin console has not
succeeded, however cleanly the kernel booted.
"""

import hashlib
import json
import os
import platform
import shutil
import stat
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from appliance.paths import AGENT_FILE_MODE, atomic_write

RUNTIME_RECORD = "runtime-images.json"
SEED_DIRECTORY = "runtime-seed"
RUNTIME_IMAGE_PINS = "runtime-image-pins.json"
RECORD_VERSION = 4
# A record is written by the slot that is being replaced and read by the slot
# that replaces it, so the reader is a version ahead across exactly the update
# that ships a new schema. It has to be able to read the older record and
# reproduce the fingerprint the writer computed, or that one update could never
# commit on any appliance — and the next attempt would fail the same way.
READABLE_RECORD_VERSIONS = (3, RECORD_VERSION)

# One generation of seeds is kept. An appliance that accumulated every past
# slot's image archives would fill the partition the next update stages into,
# and an archive older than the record is not something to load anyway.
SEED_RETENTION = 1

ROLE_ADMIN = "admin"
ROLE_EMS = "ems"
ROLE_INFLUXDB = "influxdb"

ROLES = (ROLE_ADMIN, ROLE_EMS, ROLE_INFLUXDB)

# Admin is what an operator recovers the appliance through, so a slot without it
# is not recoverable. The others are only required once they are configured.
REQUIRED_ROLES = (ROLE_ADMIN,)

# Influx first because EMS writes into it, Admin next because it is the recovery
# path, EMS last. Compose dependencies are not relied on: every recorded service
# is started explicitly, so a service that was deliberately stopped stays that
# way instead of being pulled up as somebody else's dependency.
START_ORDER = (ROLE_INFLUXDB, ROLE_ADMIN, ROLE_EMS)

DEFAULT_CONTAINERS = {
    ROLE_ADMIN: "ems-solarflow-admin",
    ROLE_EMS: "ems-solarflow",
    ROLE_INFLUXDB: "ems-influxdb",
}
DEFAULT_SERVICES = {
    ROLE_ADMIN: "ems-solarflow-admin",
    ROLE_EMS: "ems",
    ROLE_INFLUXDB: "influxdb",
}

# What a container was, precisely enough to reproduce it. "Not running" is not
# an intent: a container that crashed, that is restarting, or that was created
# and never started says nothing about what the operator wanted, and only a
# state that can be proven may be rebuilt as that state.
STATE_ABSENT = "absent"
STATE_RUNNING = "running"
STATE_STOPPED_CLEAN = "stopped_clean"
STATE_FAILED = "failed"
STATE_RESTARTING = "restarting"
STATE_CREATED = "created"
STATE_UNKNOWN = "unknown"

# The only three an OS update may be planned against.
SETTLED_STATES = (STATE_ABSENT, STATE_RUNNING, STATE_STOPPED_CLEAN)

SOURCE_PRESENT = "already_present"
SOURCE_SEED = "loaded_from_seed"
SOURCE_REGISTRY = "pulled_from_registry"
SOURCE_UNAVAILABLE = "unavailable"

DEPLOYMENT_AUTHORITY_READY = "deployment_authority_ready"
DEPLOYMENT_AUTHORITY_DRIFT = "deployment_authority_drift"
DEPLOYMENT_AUTHORITY_MISSING = "deployment_authority_missing"

RUNTIME_RECORD_MISSING = "runtime_record_missing"
DOCKER_DAEMON_UNAVAILABLE = "docker_daemon_unavailable"

SEED_READY = "runtime_seed_ready"
SEED_INCOMPLETE = "runtime_seed_incomplete"

# One bound for the whole reconstruction rather than per image. Each role may
# spend up to 900 s in `docker load` and 600 s in a pull fallback, so three
# roles could ask for 75 minutes -- far past both the unit's start timeout and
# the health window that is stamped from boot. Exceeding this is reported as a
# problem the trial slot can act on; being SIGKILLed is not.
DEFAULT_RECONSTRUCTION_BUDGET_SECONDS = 1200

RECONSTRUCTION_TIMED_OUT = "application_reconstruction_timed_out"

RECONSTRUCTION_READY = "application_reconstruction_ready"
RECONSTRUCTION_INCOMPLETE = "application_reconstruction_incomplete"


class BootstrapError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _digest_pinned(reference):
    """A reference that names a digest cannot be moved under the appliance."""

    return "@sha256:" in str(reference)


def host_architecture(machine=None):
    """This machine's OCI architecture name, or nothing if it has no mapping."""

    known = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}
    return known.get(machine if machine is not None else platform.machine(), "")


# --- the deployment authority ------------------------------------------------


@dataclass(frozen=True)
class DeploymentFile:
    """One file the deployment is defined by: path, digest, owner and mode.

    Owner and mode are recorded rather than required. What matters is that the
    file is the one that was confirmed, and an appliance whose compose file is
    owned by the account that installed it is not thereby un-updatable — but a
    compose file that changed hands between the plan and the trial is drift.
    """

    path: str = ""
    sha256: str = ""
    present: bool = False
    uid: int = -1
    mode: int = -1

    def to_dict(self):
        return {
            "path": self.path,
            "sha256": self.sha256,
            "present": self.present,
            "uid": self.uid,
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, payload):
        if not isinstance(payload, dict):
            return cls()
        return cls(
            path=str(payload.get("path") or ""),
            sha256=str(payload.get("sha256") or ""),
            present=bool(payload.get("present")),
            uid=int(payload.get("uid", -1)),
            mode=int(payload.get("mode", -1)),
        )


@dataclass(frozen=True)
class DeploymentLayout:
    """Where the deployment lives and which container each role is.

    Every field is host configuration. Nothing here is ever taken from a
    request, and the environment file is derived from the compose file rather
    than named separately, so the two cannot describe different deployments.
    """

    compose_file: str = ""
    install_root: str = ""
    containers: dict = field(default_factory=lambda: dict(DEFAULT_CONTAINERS))
    services: dict = field(default_factory=lambda: dict(DEFAULT_SERVICES))

    @property
    def environment_file(self):
        if not self.compose_file:
            return ""
        return str(Path(self.compose_file).parent / ".env")

    def container(self, role):
        return self.containers.get(role) or DEFAULT_CONTAINERS.get(role, "")

    def service(self, role):
        return self.services.get(role) or DEFAULT_SERVICES.get(role, "")

    @property
    def root(self):
        return self.install_root or (
            str(Path(self.compose_file).parent) if self.compose_file else ""
        )


@dataclass(frozen=True)
class RuntimeImage:
    """One service the appliance is made of, and the state it was in.

    ``state`` is the intent the source slot had, not a snapshot of a container
    that happened to be up: an EMS an operator deliberately stopped before the
    update must come back stopped and must not block the commit for it. An
    ``unknown`` state is never guessed at — it blocks planning instead.
    """

    role: str
    reference: str = ""
    required: bool = False
    state: str = STATE_ABSENT
    digest: str = ""
    platform: dict = field(default_factory=dict)
    image_id: str = ""

    @property
    def present(self):
        return self.state in (STATE_RUNNING, STATE_STOPPED_CLEAN)

    @property
    def running(self):
        return self.state == STATE_RUNNING

    def to_dict(self):
        return {
            "role": self.role,
            "reference": self.reference,
            "required": self.required,
            "state": self.state,
            "present": self.present,
            "intended_running": self.running,
            "image_digest": self.digest,
            "image_id": self.image_id,
            "platform": dict(self.platform),
        }

    @classmethod
    def from_dict(cls, payload):
        role = str(payload.get("role") or "")
        reference = str(payload.get("reference") or "")
        if not role or not reference:
            return None
        return cls(
            role=role,
            reference=reference,
            required=bool(payload.get("required")),
            state=str(payload.get("state") or STATE_ABSENT),
            digest=str(payload.get("image_digest") or ""),
            platform=dict(payload.get("platform") or {}),
            image_id=str(payload.get("image_id") or ""),
        )


@dataclass(frozen=True)
class RuntimeRecord:
    """What the appliance is, as one versioned and fingerprinted object.

    The deployment is part of it. Two slots holding the same image digests but
    different compose files are not the same appliance, so the next slot has to
    be able to tell that what it reconstructed is what was recorded.
    """

    images: tuple = ()
    recorded_at: float = 0.0
    version: int = RECORD_VERSION
    compose: DeploymentFile = field(default_factory=DeploymentFile)
    environment: DeploymentFile = field(default_factory=DeploymentFile)
    seeds: dict = field(default_factory=dict)

    def image(self, role):
        for entry in self.images:
            if entry.role == role:
                return entry
        return None

    def seed(self, role):
        return dict(self.seeds.get(role) or {})

    @property
    def compose_digest(self):
        return self.compose.sha256

    @property
    def environment_digest(self):
        return self.environment.sha256

    def authority(self):
        """The canonical object the fingerprint is taken over.

        Seeds are deliberately outside it: they are evidence that the recorded
        images were staged, not part of what the operator confirmed, and they
        are written in a second pass after the record itself.
        """

        # The shape is the one this record's own version defines, not the one
        # the running code writes. A reader that added a field would otherwise
        # compute a fingerprint the writer could not have produced, and the
        # trial would fail a gate nothing had actually failed.
        def service(entry):
            payload = entry.to_dict()
            if self.version < 4:
                payload.pop("image_id", None)
            return payload

        return {
            "schema_version": self.version,
            "captured_at": round(float(self.recorded_at), 3),
            "compose": self.compose.to_dict(),
            "environment": self.environment.to_dict(),
            "services": {entry.role: service(entry) for entry in sorted(
                self.images, key=lambda item: item.role
            )},
        }

    @property
    def fingerprint(self):
        if not self.images and not self.compose.path:
            return ""
        return _digest_bytes(
            json.dumps(self.authority(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        )

    def to_dict(self):
        return {
            "version": self.version,
            "recorded_at": self.recorded_at,
            "fingerprint": self.fingerprint,
            "images": [entry.to_dict() for entry in self.images],
            "compose": self.compose.to_dict(),
            "environment": self.environment.to_dict(),
            "seeds": {role: dict(entry) for role, entry in sorted(self.seeds.items())},
        }


@dataclass(frozen=True)
class ImageOutcome:
    role: str
    reference: str
    source: str
    required: bool
    detail: str = ""
    digest: str = ""
    platform: dict = field(default_factory=dict)
    # The name this slot may start the service from. It is the recorded
    # repository@digest whenever the store holds it, and otherwise the verified
    # image config digest. Never a tag: a tag can point elsewhere by the time
    # compose resolves it.
    runtime_reference: str = ""

    @property
    def available(self):
        return self.source != SOURCE_UNAVAILABLE

    def to_dict(self):
        return {
            "role": self.role,
            "reference": self.reference,
            "source": self.source,
            "required": self.required,
            "available": self.available,
            "detail": self.detail,
            "image_digest": self.digest,
            "platform": dict(self.platform),
            "runtime_reference": self.runtime_reference or self.reference,
        }


@dataclass(frozen=True)
class BootstrapReport:
    outcomes: tuple = ()
    started: tuple = ()
    problems: tuple = ()
    code: str = ""

    @property
    def ok(self):
        return not self.problems and all(
            outcome.available for outcome in self.outcomes if outcome.required
        )

    def to_dict(self):
        return {
            "ok": self.ok,
            "code": self.code,
            "images": [outcome.to_dict() for outcome in self.outcomes],
            "started": list(self.started),
            "problems": list(self.problems),
        }


class RuntimeRecordStore:
    """The shared record of what the appliance's application actually is."""

    def __init__(self, directory, *, time_fn=None):
        self.directory = Path(directory)
        self._time = time_fn or time.time

    def now(self):
        return self._time()

    @property
    def path(self):
        return self.directory / RUNTIME_RECORD

    @property
    def seed_directory(self):
        return self.directory / SEED_DIRECTORY

    def read(self):
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return RuntimeRecord()
        version = payload.get("version") if isinstance(payload, dict) else None
        if version not in READABLE_RECORD_VERSIONS:
            return RuntimeRecord()
        images = []
        for entry in payload.get("images") or []:
            if not isinstance(entry, dict):
                continue
            image = RuntimeImage.from_dict(entry)
            if image is not None:
                images.append(image)
        return RuntimeRecord(
            images=tuple(images),
            recorded_at=float(payload.get("recorded_at") or 0.0),
            version=int(version),
            compose=DeploymentFile.from_dict(payload.get("compose")),
            environment=DeploymentFile.from_dict(payload.get("environment")),
            seeds={
                str(role): dict(entry)
                for role, entry in (payload.get("seeds") or {}).items()
                if isinstance(entry, dict)
            },
        )

    def write(self, images, *, compose=None, environment=None, seeds=None, recorded_at=None):
        """Record only digest-pinned references; a tag is not an identity."""

        pinned = []
        for entry in images:
            if not _digest_pinned(entry.reference):
                raise BootstrapError(
                    "runtime_reference_not_pinned",
                    f"{entry.role} is recorded as {entry.reference}, which is a mutable tag",
                )
            pinned.append(entry)
        record = RuntimeRecord(
            images=tuple(pinned),
            recorded_at=self._time() if recorded_at is None else float(recorded_at),
            compose=compose or DeploymentFile(),
            environment=environment or DeploymentFile(),
            seeds=dict(seeds or {}),
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_write(
            self.path,
            json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n",
            mode=AGENT_FILE_MODE,
            owner_root=True,
        )
        return record


class SlotBootstrapService:
    """Record the deployment before the trial, and rebuild it inside the trial."""

    def __init__(
        self,
        *,
        docker,
        store,
        known_good=None,
        compose_file=None,
        deployment=None,
        required_platform=None,
        reconstruction_budget_seconds=DEFAULT_RECONSTRUCTION_BUDGET_SECONDS,
        time_fn=None,
    ):
        self.docker = docker
        self.store = store
        self.known_good = known_good
        self.reconstruction_budget_seconds = max(int(reconstruction_budget_seconds), 0)
        self._time = time_fn or time.monotonic
        if deployment is None:
            deployment = DeploymentLayout(compose_file=str(compose_file or ""))
        self.deployment = deployment
        self.required_platform = dict(required_platform or {})

    @property
    def compose_file(self):
        return self.deployment.compose_file

    # --- before the reboot -------------------------------------------------

    def observe_running_runtime(self, *, recorded_at=None):
        """What the appliance is right now, as an authority object. Writes nothing.

        The capture moment is part of the record but is not part of what can
        drift, so an observation taken to compare against a recorded authority
        is given that authority's timestamp: what is compared is the compose
        file, the environment and every service identity.
        """

        images = []
        for role in ROLES:
            entry = self._service_record(role)
            if entry is not None:
                images.append(entry)
        if not any(entry.present for entry in images):
            raise BootstrapError(
                "runtime_not_resolvable",
                "no running container could be resolved to a digest, so the next slot "
                "would have nothing to rebuild from",
            )
        for entry in images:
            if not _digest_pinned(entry.reference):
                raise BootstrapError(
                    "runtime_reference_not_pinned",
                    f"{entry.role} resolves to {entry.reference}, which is a mutable tag",
                )
        return RuntimeRecord(
            images=tuple(images),
            recorded_at=self.store.now() if recorded_at is None else float(recorded_at),
            compose=self._deployment_file(self.deployment.compose_file),
            environment=self._deployment_file(self.deployment.environment_file),
        )

    def record_running_runtime(self):
        """Resolve what is running now into an authority the next slot can rebuild."""

        observed = self.observe_running_runtime()
        return self.store.write(
            observed.images,
            compose=observed.compose,
            environment=observed.environment,
            recorded_at=observed.recorded_at,
        )

    def _service_record(self, role):
        container = self.deployment.container(role)
        required = role in REQUIRED_ROLES
        state, image = self._container_state(container)
        if state == STATE_UNKNOWN:
            raise BootstrapError(
                "runtime_state_unknown",
                f"the state of {container} could not be determined, so this deployment "
                "cannot be planned against",
            )
        if state not in SETTLED_STATES:
            raise BootstrapError(
                "runtime_state_not_settled",
                f"{container} is {state}; a slot cannot be rebuilt into a state the source "
                "slot cannot prove was intended",
            )
        if state == STATE_ABSENT:
            return None
        reference = self._resolved_reference(role, image)
        if not reference:
            raise BootstrapError(
                "runtime_reference_not_pinned",
                f"{container} runs {image or 'an unresolvable image'}, which cannot be "
                "resolved to a digest",
            )
        resolved = self._inspect_image(reference)
        return RuntimeImage(
            role=role,
            reference=reference,
            required=required,
            state=state,
            digest=str(getattr(resolved, "digest", "") or "") or reference.partition("@")[2],
            platform=_platform_of(resolved),
            # Docker's own binding of repository@digest to the image config
            # digest, taken while the registry is still the authority for it.
            # It is the only identity `docker save` and `docker load` preserve.
            image_id=str(getattr(resolved, "image_id", "") or ""),
        )

    def _container_state(self, container):
        """One container's lifecycle, as an intent rather than an observation.

        A clean exit is the one non-running state an operator can be said to
        have chosen. Everything else — a restart loop, a non-zero exit, a
        container created and never started — is reported as what it is.
        """

        try:
            state = self.docker.inspect_container(container, strict=True)
        except Exception:
            return STATE_UNKNOWN, ""
        if not getattr(state, "exists", False):
            return STATE_ABSENT, ""
        lifecycle = str(getattr(state, "state", "") or "")
        image = str(getattr(state, "image", "") or "")
        if lifecycle == "running":
            return STATE_RUNNING, image
        if lifecycle == "restarting":
            return STATE_RESTARTING, image
        if lifecycle == "created":
            return STATE_CREATED, image
        if lifecycle == "exited" and int(getattr(state, "exit_code", 0) or 0) == 0:
            return STATE_STOPPED_CLEAN, image
        return STATE_FAILED, image

    def _deployment_file(self, path):
        if not path:
            return DeploymentFile()
        target = Path(path)
        try:
            info = target.lstat()
            digest = _digest_bytes(target.read_bytes())
        except OSError:
            return DeploymentFile(path=str(target), sha256="", present=False)
        return DeploymentFile(
            path=str(target),
            sha256=digest,
            present=True,
            uid=info.st_uid,
            mode=stat.S_IMODE(info.st_mode),
        )

    def seed(self, record=None):
        """Copy the recorded images beside the record on the shared partition.

        This is what makes an appliance with no WAN able to finish a trial: the
        new slot loads the images it needs from the medium it already has.
        Each archive is hashed and the digest recorded, so the next slot can
        tell an interrupted copy from a complete one before loading it.
        """

        record = record or self.store.read()
        directory = self.store.seed_directory
        self._prune_seeds(record)
        seeded, metadata = [], {}
        for entry in record.images:
            target = directory / f"{entry.role}.tar"
            try:
                directory.mkdir(parents=True, exist_ok=True)
                self.docker.save_image(entry.reference, target)
                metadata[entry.role] = {
                    "file": target.name,
                    "reference": entry.reference,
                    "image_digest": entry.digest,
                    "image_id": entry.image_id,
                    "platform": dict(entry.platform),
                    "sha256": _digest_bytes(target.read_bytes()),
                    "size_bytes": target.stat().st_size,
                    "captured_at": record.recorded_at,
                }
                if not _sync(target):
                    raise BootstrapError(
                        "runtime_seed_not_durable",
                        f"{target.name} could not be flushed to the persistent partition",
                    )
            except BootstrapError:
                raise
            except Exception as exc:
                raise BootstrapError(
                    "runtime_seed_failed",
                    f"{entry.role} ({entry.reference}) could not be seeded: {exc}",
                )
            seeded.append(entry.role)
        if not _sync(directory):
            raise BootstrapError(
                "runtime_seed_not_durable",
                "the seed directory could not be flushed to the persistent partition",
            )
        self.store.write(
            record.images,
            compose=record.compose,
            environment=record.environment,
            seeds=metadata,
            recorded_at=record.recorded_at,
        )
        return tuple(seeded)

    def _prune_seeds(self, record):
        """Keep one generation. Old archives are the partition's free space."""

        directory = self.store.seed_directory
        if not directory.is_dir():
            return ()
        wanted = {f"{entry.role}.tar" for entry in record.images}
        removed = []
        for item in sorted(directory.iterdir()):
            if item.name not in wanted:
                try:
                    item.unlink()
                except OSError:
                    continue
                removed.append(item.name)
        return tuple(removed)

    def discard_seed(self):
        shutil.rmtree(self.store.seed_directory, ignore_errors=True)
        return True

    def seed_bytes(self):
        directory = self.store.seed_directory
        if not directory.is_dir():
            return 0
        total = 0
        for item in directory.iterdir():
            try:
                total += item.stat().st_size
            except OSError:
                continue
        return total

    # --- the deployment authority ------------------------------------------

    def deployment_drift(self, record=None):
        """Is the deployment on disk still the one that was recorded?

        Recomputed, never re-read from the record: the digest is the whole
        authority, and a fingerprint that refreshed itself on drift would mean
        the operator confirmed one deployment and the appliance rebuilt another.
        """

        record = record if record is not None else self.store.read()
        problems = []
        problems.extend(self._file_drift("compose file", record.compose))
        problems.extend(self._file_drift("environment file", record.environment))
        for entry in record.images:
            if entry.state not in SETTLED_STATES:
                problems.append(
                    f"the recorded state of {entry.role} is {entry.state}, which is not a "
                    "state a slot can be rebuilt into"
                )
        return tuple(problems)

    def _file_drift(self, label, declared):
        if not declared.path:
            return ()
        target = Path(declared.path)
        root = self.deployment.root
        if root and not _within(target, Path(root)):
            return (f"the {label} {target} is outside the EMS installation root",)
        try:
            info = target.lstat()
        except OSError:
            if declared.present:
                return (f"the {label} {target} is gone",)
            return ()
        if not declared.present:
            return (f"the {label} {target} appeared after the deployment was recorded",)
        if stat.S_ISLNK(info.st_mode):
            return (f"the {label} {target} is a symlink and was a regular file",)
        if not stat.S_ISREG(info.st_mode):
            return (f"the {label} {target} is not a regular file",)
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o002:
            return (f"the {label} {target} is world-writable",)
        if declared.uid >= 0 and info.st_uid != declared.uid:
            return (
                f"the {label} {target} is owned by uid {info.st_uid}, it was "
                f"owned by uid {declared.uid}",
            )
        if declared.mode >= 0 and mode != declared.mode:
            return (
                f"the {label} {target} is mode {mode:04o}, it was {declared.mode:04o}",
            )
        try:
            observed = _digest_bytes(target.read_bytes())
        except OSError as exc:
            return (f"the {label} {target} could not be read: {exc}",)
        if observed != declared.sha256:
            return (f"the {label} {target} changed after the deployment was recorded",)
        return ()

    def deployment_changed(self, record=None):
        """Recompute the deployment now and compare it with a recorded authority.

        ``deployment_drift`` answers for the files. This answers for the running
        services: an Admin container restarted onto another image between the
        plan and the write is the same compose file and a different appliance.
        """

        record = record if record is not None else self.store.read()
        if not record.images:
            return ("no deployment authority has been recorded",)
        try:
            observed = self.observe_running_runtime(recorded_at=record.recorded_at)
        except BootstrapError as exc:
            return (exc.message,)
        # Compared in the recorded schema. An observation is always taken at the
        # current one, so against an older record the two objects would differ
        # by shape alone and every unchanged deployment would look changed.
        observed = replace(observed, version=record.version)
        if observed.fingerprint != record.fingerprint:
            return ("the running deployment is no longer the one that was recorded",)
        return ()

    def deployment_state(self, record=None):
        record = record if record is not None else self.store.read()
        if not record.images or not record.compose.path:
            return DEPLOYMENT_AUTHORITY_MISSING
        if self.deployment_drift(record):
            return DEPLOYMENT_AUTHORITY_DRIFT
        return DEPLOYMENT_AUTHORITY_READY

    def seed_state(self, record=None):
        record = record if record is not None else self.store.read()
        if not record.images:
            return DEPLOYMENT_AUTHORITY_MISSING
        for entry in record.images:
            declared = record.seed(entry.role)
            path = self.store.seed_directory / f"{entry.role}.tar"
            if _seed_problem(path, declared, entry.reference):
                return SEED_INCOMPLETE
        return SEED_READY

    # --- inside the trial slot ---------------------------------------------

    def reconstruct(self):
        """Rebuild every recorded service, in the state it was recorded in."""

        record = self.store.read()
        if not record.images:
            return BootstrapReport(
                code=RUNTIME_RECORD_MISSING,
                problems=(
                    "the shared partition carries no runtime record, so this slot cannot "
                    "rebuild the Admin and EMS containers",
                ),
            )

        # Before docker load, docker pull or docker compose up. A deployment
        # that changed after the plan was confirmed is not executed at all.
        drift = self.deployment_drift(record)
        if drift:
            return BootstrapReport(
                code=DEPLOYMENT_AUTHORITY_DRIFT,
                problems=tuple(
                    [
                        "the EMS deployment changed after this OS update was planned: "
                        + "; ".join(drift)
                    ]
                ),
            )

        if not self._daemon_ready():
            return BootstrapReport(
                code=DOCKER_DAEMON_UNAVAILABLE,
                problems=("the Docker daemon is not available in this slot",),
            )

        outcomes, exhausted = self._restore_within_budget(record)
        started, refused = self._start_services(record, outcomes)
        problems = [
            f"{outcome.role} ({outcome.reference}) could not be restored: {outcome.detail}"
            for outcome in outcomes
            if outcome.required and not outcome.available
        ]
        problems.extend(refused)
        if exhausted:
            problems.append(
                f"the {self.reconstruction_budget_seconds}s reconstruction budget was "
                "exhausted before every recorded service was restored"
            )
        return BootstrapReport(
            outcomes=tuple(outcomes),
            started=tuple(started),
            problems=tuple(problems),
            code="" if not problems else RECONSTRUCTION_INCOMPLETE,
        )

    def _restore_within_budget(self, record):
        """Restore each recorded image while the budget lasts.

        A role the budget did not reach is reported as unavailable with the
        reason named, so the health gates fail on a fact instead of on the
        silence a killed process leaves behind.
        """

        deadline = self._time() + self.reconstruction_budget_seconds
        outcomes, exhausted = [], False
        for entry in record.images:
            if self.reconstruction_budget_seconds and self._time() >= deadline:
                exhausted = True
                outcomes.append(
                    ImageOutcome(
                        entry.role,
                        entry.reference,
                        SOURCE_UNAVAILABLE,
                        entry.required,
                        "the reconstruction budget was exhausted before this image",
                    )
                )
                continue
            outcomes.append(self._restore(entry, record))
        return outcomes, exhausted

    def _restore(self, entry, record=None):
        if self._verified_image(entry) is None:
            return ImageOutcome(
                entry.role,
                entry.reference,
                SOURCE_PRESENT,
                entry.required,
                digest=entry.digest,
                platform=entry.platform,
                runtime_reference=entry.reference,
            )

        seed = self.store.seed_directory / f"{entry.role}.tar"
        declared = (record or self.store.read()).seed(entry.role)
        if seed.is_file():
            problem = _seed_problem(seed, declared, entry.reference)
            if problem:
                # A seed that does not match its record is not loaded. The
                # registry fallback below still names the exact digest.
                return self._pull(entry, detail=problem)
            try:
                self.docker.load_image(seed)
            except Exception as exc:
                return ImageOutcome(
                    entry.role, entry.reference, SOURCE_UNAVAILABLE, entry.required, str(exc)
                )
            # docker load's output names an image, not a digest. Only an
            # inspection of the store answers what was actually imported.
            problem = self._verified_image(entry)
            if problem is None:
                return ImageOutcome(
                    entry.role,
                    entry.reference,
                    SOURCE_SEED,
                    entry.required,
                    digest=entry.digest,
                    platform=entry.platform,
                    runtime_reference=entry.reference,
                )
            # A repository digest is a registry's name for a manifest, and
            # `docker save` re-serialises that manifest, so no archive can carry
            # it back. The image config digest is restored byte for byte and
            # commits to rootfs.diff_ids, so asking Docker to confirm *that* is
            # what makes the loaded image the recorded one.
            restored = self._verified_by_image_id(entry, declared)
            if restored is None:
                return ImageOutcome(
                    entry.role,
                    entry.reference,
                    SOURCE_SEED,
                    entry.required,
                    digest=entry.digest,
                    platform=entry.platform,
                    runtime_reference=entry.image_id,
                )
            return self._pull(entry, detail="; ".join((problem, restored)))

        return self._pull(entry)

    def _pull(self, entry, *, detail=""):
        """The fallback names the exact digest; a tag could point anywhere."""

        if not _digest_pinned(entry.reference):
            return ImageOutcome(
                entry.role,
                entry.reference,
                SOURCE_UNAVAILABLE,
                entry.required,
                "the recorded reference is a mutable tag and is never pulled",
            )
        try:
            self.docker.pull_image(entry.reference)
        except Exception as exc:
            return ImageOutcome(
                entry.role,
                entry.reference,
                SOURCE_UNAVAILABLE,
                entry.required,
                "; ".join(filter(None, (detail, str(exc)))),
            )
        problem = self._verified_image(entry)
        if problem is None:
            return ImageOutcome(
                entry.role,
                entry.reference,
                SOURCE_REGISTRY,
                entry.required,
                detail,
                digest=entry.digest,
                platform=entry.platform,
                runtime_reference=entry.reference,
            )
        return ImageOutcome(
            entry.role,
            entry.reference,
            SOURCE_UNAVAILABLE,
            entry.required,
            "; ".join(filter(None, (detail, problem))),
        )

    def _start_services(self, record, outcomes):
        """Start exactly the services the source slot had running, in order.

        ``compose_up_service`` returns a command result rather than raising on a
        non-zero exit, so a service was previously appended to ``started``
        whether or not compose had actually started it. Health is a second,
        independent check — but a reconstruction report that claims a service it
        did not start is the wrong thing to hand a health gate.
        """

        available = {outcome.role for outcome in outcomes if outcome.available}
        started, refused = [], []
        pins = self._write_image_pins(outcomes)
        for role in START_ORDER:
            entry = record.image(role)
            if entry is None or not entry.running or role not in available:
                continue
            service = self.deployment.service(role)
            if not service or not self.compose_file:
                continue
            try:
                result = self.docker.compose_up_service(service, overrides=pins)
            except Exception as exc:
                refused.append(f"{service} could not be started: {exc}")
                continue
            if result is not None and not getattr(result, "ok", True):
                refused.append(
                    f"{service} could not be started: "
                    + (str(getattr(result, "stderr", "") or "").strip()[:200] or "compose refused")
                )
                continue
            started.append(service)
        return started, tuple(refused)

    def _write_image_pins(self, outcomes):
        """Pin every service to the image this slot actually verified.

        The recorded compose file is authority and is never rewritten; this is
        an overlay compose passes after it. Without it a service whose image
        came out of a seed would be started from `repository@digest`, which no
        loaded archive can carry — compose would go to the registry the seed
        exists to avoid. With it, what starts is what was inspected.
        """

        services = {}
        for outcome in outcomes:
            if not outcome.available:
                continue
            service = self.deployment.service(outcome.role)
            reference = outcome.runtime_reference or outcome.reference
            if not service or not reference:
                continue
            # Both producers are content-addressed already; a name that is
            # neither is not written at all, so an overlay can never be the way
            # a mutable tag reaches a container.
            if not _digest_pinned(reference) and not reference.startswith("sha256:"):
                continue
            services[service] = {"image": reference}
        if not services:
            return ()
        target = self.store.directory / RUNTIME_IMAGE_PINS
        try:
            self.store.directory.mkdir(parents=True, exist_ok=True)
            atomic_write(
                target,
                json.dumps({"services": services}, indent=2, sort_keys=True) + "\n",
                mode=AGENT_FILE_MODE,
                owner_root=True,
            )
        except OSError:
            return ()
        return (str(target),)

    # --- docker helpers ----------------------------------------------------

    def _daemon_ready(self):
        try:
            return bool(self.docker.daemon_running())
        except Exception:
            return False

    def _inspect_image(self, reference):
        try:
            return self.docker.inspect_image(reference)
        except Exception:
            return None

    def _verified_image(self, entry):
        """Is the recorded image in this slot's store, at its exact identity?

        Returns nothing when it is, and why not when it is not. A digest and a
        platform are both part of the identity: an arm64 appliance that loaded
        an amd64 image would commit a slot whose containers cannot start.
        """

        state = self._inspect_image(entry.reference)
        if state is None or not bool(getattr(state, "exists", False)):
            return "the recorded image is not in this slot's image store"
        digest = str(getattr(state, "digest", "") or "")
        expected = entry.digest or entry.reference.partition("@")[2]
        if expected and digest != expected:
            return (
                f"the image store holds digest {digest or 'none'} for this reference, "
                f"the source slot recorded {expected}"
            )
        observed = _platform_of(state)
        for wanted in (entry.platform, self.required_platform):
            problem = _platform_problem(wanted, observed)
            if problem:
                return problem
        return None

    def _verified_by_image_id(self, entry, declared=None):
        """Did the load restore exactly the image the record was written for?

        Trust boundary: the seed's own metadata is never the answer. It only
        says which image config digest to ask Docker about; Docker's inspection
        of its store is what decides, and it has to answer with that exact id
        and a matching platform.
        """

        expected = entry.image_id
        if not expected:
            return (
                "the record carries no image id for this service, so a loaded archive "
                "cannot be attributed to it"
            )
        seeded = str((declared or {}).get("image_id") or "")
        if seeded and seeded != expected:
            return f"the seed was written for image {seeded}, the record names {expected}"
        state = self._inspect_image(expected)
        if state is None or not bool(getattr(state, "exists", False)):
            return f"the loaded archive did not put image {expected} in this slot's store"
        observed = str(getattr(state, "image_id", "") or "")
        if observed != expected:
            return f"the image store answered with {observed or 'no id'} for {expected}"
        platform = _platform_of(state)
        for wanted in (entry.platform, self.required_platform):
            problem = _platform_problem(wanted, platform)
            if problem:
                return problem
        return None

    def _admin_reference(self):
        if self.known_good is None:
            return ""
        entry = self.known_good.current()
        if entry and entry.get("admin_reference"):
            return str(entry["admin_reference"])
        return ""

    def _resolved_reference(self, role, image):
        """A container's image as repository@digest, or nothing.

        A tag is deliberately not accepted as an answer: it can point somewhere
        else by the time the other slot tries to rebuild from it.

        For Admin the known-good record is a consistency check and never a
        substitute. Preferring it meant an appliance whose Admin had been
        replaced since the last known-good entry would record the *older*
        digest, and the trial slot would faithfully restore an Admin the
        operator had already moved off.
        """

        running = self._running_reference(image)
        if role != ROLE_ADMIN:
            return running
        recorded = self._admin_reference()
        if not recorded or not _digest_pinned(recorded):
            return running
        if not running:
            return ""
        if recorded != running:
            raise BootstrapError(
                "admin_runtime_authority_drift",
                f"the Admin container runs {running}, the known-good record names "
                f"{recorded}; neither may be chosen over the other",
            )
        return running

    def _running_reference(self, image):
        if not image:
            return ""
        if _digest_pinned(image):
            return image
        resolved = self._inspect_image(image)
        digest = str(getattr(resolved, "digest", "") or "")
        if not digest.startswith("sha256:"):
            return ""
        repository = image.rpartition(":")[0] or image
        return f"{repository}@{digest}"


def _platform_of(state):
    architecture = str(getattr(state, "architecture", "") or "")
    operating_system = str(getattr(state, "os", "") or "")
    if not architecture and not operating_system:
        return {}
    return {"os": operating_system, "architecture": architecture}


def _platform_problem(wanted, observed):
    if not wanted:
        return ""
    for key in ("os", "architecture"):
        expected = str(wanted.get(key) or "")
        if not expected:
            continue
        if str(observed.get(key) or "") != expected:
            return (
                f"the image platform is {observed.get('os') or '?'}/"
                f"{observed.get('architecture') or '?'}, this slot needs "
                f"{wanted.get('os') or '?'}/{wanted.get('architecture') or '?'}"
            )
    return ""


def _within(target, root):
    try:
        Path(os.path.normpath(str(target))).relative_to(os.path.normpath(str(root)))
    except ValueError:
        return False
    return True


def _digest_bytes(blob):
    return f"sha256:{hashlib.sha256(blob).hexdigest()}"


def _seed_problem(path, declared, reference):
    """Is this archive the one the source slot recorded? Say why, if not."""

    if not declared:
        return "the seed archive carries no recorded digest"
    if declared.get("reference") != reference:
        return f"the seed was written for {declared.get('reference')}"
    try:
        observed = _digest_bytes(path.read_bytes())
    except OSError as exc:
        return f"the seed archive could not be read: {exc}"
    if observed != declared.get("sha256"):
        return "the seed archive does not match its recorded digest"
    return ""


def _sync(path):
    try:
        handle = os.open(str(path), os.O_RDONLY)
    except OSError:
        return False
    try:
        os.fsync(handle)
    except OSError:
        return False
    finally:
        os.close(handle)
    return True
