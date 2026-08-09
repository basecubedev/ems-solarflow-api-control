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

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from appliance.paths import AGENT_FILE_MODE, atomic_write

RUNTIME_RECORD = "runtime-images.json"
SEED_DIRECTORY = "runtime-seed"
RECORD_VERSION = 1

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
    role: str
    reference: str
    required: bool = False

    def to_dict(self):
        return {"role": self.role, "reference": self.reference, "required": self.required}


@dataclass(frozen=True)
class RuntimeRecord:
    """What the running slot is made of, as exact digests."""

    images: tuple = ()
    recorded_at: float = 0.0
    version: int = RECORD_VERSION

    def image(self, role):
        for entry in self.images:
            if entry.role == role:
                return entry
        return None

    def to_dict(self):
        return {
            "version": self.version,
            "recorded_at": self.recorded_at,
            "images": [entry.to_dict() for entry in self.images],
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
                    role=role, reference=reference, required=bool(entry.get("required"))
                )
            )
        return RuntimeRecord(
            images=tuple(images),
            recorded_at=float(payload.get("recorded_at") or 0.0),
        )

    def write(self, images):
        """Record only digest-pinned references; a tag is not an identity."""

        pinned = []
        for entry in images:
            if not _digest_pinned(entry.reference):
                raise BootstrapError(
                    "runtime_reference_not_pinned",
                    f"{entry.role} is recorded as {entry.reference}, which is a mutable tag",
                )
            pinned.append(entry)
        record = RuntimeRecord(images=tuple(pinned), recorded_at=self._time())
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
            images.append(RuntimeImage(role=ROLE_ADMIN, reference=admin, required=True))
        for role, container in ((ROLE_EMS, "ems-solarflow"), (ROLE_INFLUXDB, "influxdb")):
            reference = self._resolved_reference(container)
            if reference:
                images.append(RuntimeImage(role=role, reference=reference, required=False))
        if not images:
            raise BootstrapError(
                "runtime_not_resolvable",
                "no running container could be resolved to a digest, so the next slot "
                "would have nothing to rebuild from",
            )
        return self.store.write(images)

    def seed(self, record=None):
        """Copy the recorded images beside the record on the shared partition.

        This is what makes an appliance with no WAN able to finish a trial: the
        new slot loads the images it needs from the medium it already has.
        """

        record = record or self.store.read()
        directory = self.store.seed_directory
        seeded = []
        for entry in record.images:
            target = directory / f"{entry.role}.tar"
            try:
                directory.mkdir(parents=True, exist_ok=True)
                self.docker.save_image(entry.reference, target)
            except Exception as exc:
                raise BootstrapError(
                    "runtime_seed_failed",
                    f"{entry.role} ({entry.reference}) could not be seeded: {exc}",
                )
            seeded.append(entry.role)
        return tuple(seeded)

    def discard_seed(self):
        import shutil

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

        outcomes = [self._restore(entry) for entry in record.images]
        started = self._start_admin(record)
        problems = [
            f"{outcome.role} ({outcome.reference}) could not be restored: {outcome.detail}"
            for outcome in outcomes
            if outcome.required and not outcome.available
        ]
        return BootstrapReport(
            outcomes=tuple(outcomes), started=tuple(started), problems=tuple(problems)
        )

    def _restore(self, entry):
        if self._image_present(entry.reference):
            return ImageOutcome(entry.role, entry.reference, SOURCE_PRESENT, entry.required)

        seed = self.store.seed_directory / f"{entry.role}.tar"
        if seed.is_file():
            try:
                self.docker.load_image(seed)
            except Exception as exc:
                return ImageOutcome(
                    entry.role, entry.reference, SOURCE_UNAVAILABLE, entry.required, str(exc)
                )
            if self._image_present(entry.reference):
                return ImageOutcome(entry.role, entry.reference, SOURCE_SEED, entry.required)

        try:
            self.docker.pull_image(entry.reference)
        except Exception as exc:
            return ImageOutcome(
                entry.role, entry.reference, SOURCE_UNAVAILABLE, entry.required, str(exc)
            )
        if self._image_present(entry.reference):
            return ImageOutcome(entry.role, entry.reference, SOURCE_REGISTRY, entry.required)
        return ImageOutcome(
            entry.role,
            entry.reference,
            SOURCE_UNAVAILABLE,
            entry.required,
            "neither the seed nor the registry produced the recorded digest",
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
