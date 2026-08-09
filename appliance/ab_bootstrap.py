# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rebuilding the container runtime a freshly written slot does not have.

``/var/lib/docker`` is per-slot: ``image-rota`` binds ``/var`` from
``/persistent/slots/system_<slot>/var``, which is exactly what keeps a rollback
from handing a newer content store to an older engine. The cost is that a slot
that has just been written has an empty image store, so the Admin and EMS
containers an operator depends on are not there yet.

The appliance therefore records what the running slot is made of — resolved
digests, never tags — onto the shared partition, and seeds those images beside
the record before the trial reboot is requested. The new slot loads the seed,
falls back to pulling the same digests, and reports what it could not rebuild.

A slot that cannot rebuild its runtime does not become known-good. That is the
whole point: an OS update that leaves an appliance with no way back to the Admin
console has not succeeded, however cleanly the kernel booted.
"""

import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from appliance.paths import AGENT_FILE_MODE, atomic_write

RUNTIME_RECORD = "runtime-images.json"
SEED_DIRECTORY = "runtime-seed"
RECORD_VERSION = 2

# One generation of seeds is kept. An appliance that accumulated every past
# slot's image archives would fill the partition the next update stages into,
# and an archive older than the record is not something to load anyway.
SEED_RETENTION = 1

ROLE_ADMIN = "admin"
ROLE_EMS = "ems"
ROLE_INFLUXDB = "influxdb"

# Admin is what an operator recovers the appliance through, so a slot without it
# is not recoverable. The others are only required once they are configured.
REQUIRED_ROLES = (ROLE_ADMIN,)

SOURCE_PRESENT = "already_present"
SOURCE_SEED = "loaded_from_seed"
SOURCE_REGISTRY = "pulled_from_registry"
SOURCE_UNAVAILABLE = "unavailable"


class BootstrapError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _digest_pinned(reference):
    """A reference that names a digest cannot be moved under the appliance."""

    return "@sha256:" in str(reference)


@dataclass(frozen=True)
class RuntimeImage:
    """One container the appliance is made of, and whether it was running.

    ``running`` is the intent the source slot had, not a snapshot of what
    happened to be up: an EMS an operator deliberately stopped before the
    update must come back stopped, and must not block the commit for it.
    """

    role: str
    reference: str
    required: bool = False
    running: bool = False

    def to_dict(self):
        return {
            "role": self.role,
            "reference": self.reference,
            "required": self.required,
            "running": self.running,
        }


@dataclass(frozen=True)
class RuntimeRecord:
    """What the running slot is made of, as exact digests.

    The deployment is part of it. Two slots holding the same image digests but
    different compose files are not the same appliance, so the next slot has to
    be able to tell that what it reconstructed is what was recorded.
    """

    images: tuple = ()
    recorded_at: float = 0.0
    version: int = RECORD_VERSION
    compose_digest: str = ""
    environment_digest: str = ""
    seeds: dict = field(default_factory=dict)

    def image(self, role):
        for entry in self.images:
            if entry.role == role:
                return entry
        return None

    def seed(self, role):
        return dict(self.seeds.get(role) or {})

    def to_dict(self):
        return {
            "version": self.version,
            "recorded_at": self.recorded_at,
            "images": [entry.to_dict() for entry in self.images],
            "compose_digest": self.compose_digest,
            "environment_digest": self.environment_digest,
            "seeds": {role: dict(entry) for role, entry in sorted(self.seeds.items())},
        }


@dataclass(frozen=True)
class ImageOutcome:
    role: str
    reference: str
    source: str
    required: bool
    detail: str = ""

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
        }


@dataclass(frozen=True)
class BootstrapReport:
    outcomes: tuple = ()
    started: tuple = ()
    problems: tuple = field(default_factory=tuple)

    @property
    def ok(self):
        return not self.problems and all(
            outcome.available for outcome in self.outcomes if outcome.required
        )

    def to_dict(self):
        return {
            "ok": self.ok,
            "images": [outcome.to_dict() for outcome in self.outcomes],
            "started": list(self.started),
            "problems": list(self.problems),
        }


class RuntimeRecordStore:
    """The shared record of what the appliance's containers actually are."""

    def __init__(self, directory, *, time_fn=None):
        self.directory = Path(directory)
        self._time = time_fn or time.time

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
        if not isinstance(payload, dict) or payload.get("version") != RECORD_VERSION:
            return RuntimeRecord()
        images = []
        for entry in payload.get("images") or []:
            if not isinstance(entry, dict):
                continue
            reference = str(entry.get("reference") or "")
            role = str(entry.get("role") or "")
            if not reference or not role:
                continue
            images.append(
                RuntimeImage(
                    role=role,
                    reference=reference,
                    required=bool(entry.get("required")),
                    running=bool(entry.get("running")),
                )
            )
        return RuntimeRecord(
            images=tuple(images),
            recorded_at=float(payload.get("recorded_at") or 0.0),
            compose_digest=str(payload.get("compose_digest") or ""),
            environment_digest=str(payload.get("environment_digest") or ""),
            seeds={
                str(role): dict(entry)
                for role, entry in (payload.get("seeds") or {}).items()
                if isinstance(entry, dict)
            },
        )

    def write(self, images, *, compose_digest="", environment_digest="", seeds=None):
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
            recorded_at=self._time(),
            compose_digest=str(compose_digest or ""),
            environment_digest=str(environment_digest or ""),
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
    """Seed the runtime before the trial, and rebuild it inside the trial."""

    def __init__(self, *, docker, store, known_good=None, compose_file=None):
        self.docker = docker
        self.store = store
        self.known_good = known_good
        self.compose_file = str(compose_file) if compose_file else ""

    # --- before the reboot -------------------------------------------------

    def record_running_runtime(self):
        """Resolve what is running now into digests the next slot can rebuild."""

        images = []
        admin = self._admin_reference()
        if admin:
            images.append(
                RuntimeImage(
                    role=ROLE_ADMIN,
                    reference=admin,
                    required=True,
                    running=self._running("ems-admin"),
                )
            )
        for role, container in ((ROLE_EMS, "ems-solarflow"), (ROLE_INFLUXDB, "influxdb")):
            reference = self._resolved_reference(container)
            if reference:
                images.append(
                    RuntimeImage(
                        role=role,
                        reference=reference,
                        required=False,
                        running=self._running(container),
                    )
                )
        if not images:
            raise BootstrapError(
                "runtime_not_resolvable",
                "no running container could be resolved to a digest, so the next slot "
                "would have nothing to rebuild from",
            )
        return self.store.write(
            images,
            compose_digest=self._file_digest(self.compose_file),
            environment_digest=self._environment_digest(),
        )

    def _file_digest(self, path):
        if not path:
            return ""
        try:
            return _digest_bytes(Path(path).read_bytes())
        except OSError:
            return ""

    def _environment_digest(self):
        """The deployment env beside the compose file, as one digest.

        Not its contents: an .env holds credentials, and a record on the
        persistent partition is read by both slots and by a support bundle.
        """

        if not self.compose_file:
            return ""
        return self._file_digest(Path(self.compose_file).parent / ".env")

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
                    "sha256": _digest_bytes(target.read_bytes()),
                    "size_bytes": target.stat().st_size,
                }
                _sync(target)
            except BootstrapError:
                raise
            except Exception as exc:
                raise BootstrapError(
                    "runtime_seed_failed",
                    f"{entry.role} ({entry.reference}) could not be seeded: {exc}",
                )
            seeded.append(entry.role)
        _sync(directory)
        self.store.write(
            record.images,
            compose_digest=record.compose_digest,
            environment_digest=record.environment_digest,
            seeds=metadata,
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

    # --- inside the trial slot ---------------------------------------------

    def reconstruct(self):
        """Make every recorded image available in this slot's image store."""

        record = self.store.read()
        if not record.images:
            return BootstrapReport(
                problems=(
                    "the shared partition carries no runtime record, so this slot cannot "
                    "rebuild the Admin and EMS containers",
                ),
            )
        if not self._daemon_ready():
            return BootstrapReport(problems=("the Docker daemon is not available in this slot",))

        outcomes = [self._restore(entry, record) for entry in record.images]
        started = self._start_admin(record)
        problems = [
            f"{outcome.role} ({outcome.reference}) could not be restored: {outcome.detail}"
            for outcome in outcomes
            if outcome.required and not outcome.available
        ]
        return BootstrapReport(
            outcomes=tuple(outcomes), started=tuple(started), problems=tuple(problems)
        )

    def _restore(self, entry, record=None):
        if self._image_present(entry.reference):
            return ImageOutcome(entry.role, entry.reference, SOURCE_PRESENT, entry.required)

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
            if self._image_present(entry.reference):
                return ImageOutcome(entry.role, entry.reference, SOURCE_SEED, entry.required)

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
        if self._image_present(entry.reference):
            return ImageOutcome(
                entry.role, entry.reference, SOURCE_REGISTRY, entry.required, detail
            )
        return ImageOutcome(
            entry.role,
            entry.reference,
            SOURCE_UNAVAILABLE,
            entry.required,
            "; ".join(
                filter(
                    None,
                    (detail, "neither the seed nor the registry produced the recorded digest"),
                )
            ),
        )

    def _start_admin(self, record):
        entry = record.image(ROLE_ADMIN)
        if entry is None or not self.compose_file:
            return ()
        try:
            self.docker.compose_up_service("admin")
        except Exception:
            return ()
        return ("admin",)

    # --- docker helpers ----------------------------------------------------

    def _daemon_ready(self):
        try:
            return bool(self.docker.daemon_running())
        except Exception:
            return False

    def _image_present(self, reference):
        try:
            state = self.docker.inspect_image(reference)
        except Exception:
            return False
        return bool(getattr(state, "exists", state))

    def _admin_reference(self):
        if self.known_good is None:
            return self._resolved_reference("ems-admin")
        entry = self.known_good.current()
        if entry and entry.get("admin_reference"):
            return str(entry["admin_reference"])
        return self._resolved_reference("ems-admin")

    def _running(self, container):
        """Was this container up when the update was planned?"""

        try:
            state = self.docker.inspect_container(container)
        except Exception:
            return False
        return bool(getattr(state, "exists", False)) and getattr(state, "state", "") == "running"

    def _resolved_reference(self, container):
        """A container's image as repository@digest, or nothing.

        A tag is deliberately not accepted as an answer: it can point somewhere
        else by the time the other slot tries to rebuild from it.
        """

        try:
            state = self.docker.inspect_container(container)
        except Exception:
            return ""
        image = str(getattr(state, "image", "") or "")
        if not image:
            return ""
        if _digest_pinned(image):
            return image
        try:
            resolved = self.docker.inspect_image(image)
        except Exception:
            return ""
        digest = str(getattr(resolved, "digest", "") or "")
        if not digest.startswith("sha256:"):
            return ""
        repository = image.rpartition(":")[0] or image
        return f"{repository}@{digest}"


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
    import os

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
