# SPDX-License-Identifier: AGPL-3.0-or-later
"""Planning an OS update and writing it into the inactive slot.

The order here is the safety argument, not a style choice:

    plan → preconditions → confirmation → revalidate → invalidate the target
    → stage → write boot → flush → read back → write root → flush → read back
    → inspect the written root read-only → arm the selector → reboot

Every destructive step is preceded by a durable record of what is about to
happen, so a power loss leaves a state that can be classified rather than
guessed at. Until the selector is armed, and in fact until a booted target slot
has proven itself, the default boot slot is untouched.

The operation record binds the exact physical identity the plan was made
against — device, both partitions, both digests, the layout and the persistent
schema. It is revalidated immediately before the first write, because a plan an
operator confirmed minutes ago is not evidence about the disk right now.
"""

import hashlib
import json
import os
import time
from dataclasses import dataclass, field

from appliance import (
    ab_blocks,
    ab_boot,
    ab_inspect,
    ab_layout,
    ab_persistence,
    os_artifacts,
    os_releases,
    rpi_image_gen,
    sparse,
)
from appliance.ab_blocks import BlockError
from appliance.ab_layout import LayoutError
from appliance.ab_state import PendingTrial
from appliance.operations import (
    STATE_FAILED_RECOVERABLE,
    STATE_FAILED_TERMINAL,
    STATE_RUNNING,
    STATE_VERIFYING,
)

TYPE_OS_UPDATE = "ab.update"
TYPE_OS_ROLLBACK = "ab.rollback"

STAGE_STAGING = "staging"
STAGE_SPARSE_VALIDATED = "sparse_validated"
STAGE_IMAGE_EXPANDING = "image_expanding"
STAGE_EXPANDED_VERIFIED = "expanded_verified"
STAGE_WRITING_INACTIVE = "writing_inactive"
STAGE_VERIFYING_INACTIVE = "verifying_inactive"
STAGE_READY_FOR_TRYBOOT = "ready_for_tryboot"
STAGE_TRYBOOT_REQUESTED = "tryboot_requested"

AUTHORITY_FIELD = "ab_authority"

# Staging writes the whole artifact to the persistent partition before any
# block device is touched, so the space has to be there before the plan is
# confirmed rather than discovered halfway through.
MINIMUM_STAGING_BYTES = 2 * 1024 * 1024 * 1024


class OsUpdateError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class WriteAuthority:
    """Exactly what a confirmed plan is allowed to write, and to where."""

    layout_id: str
    slot_schema_version: int
    persistent_schema_version: int
    device: str
    active_slot: str
    target_slot: str
    boot_device: str
    boot_partuuid: str
    root_device: str
    root_partuuid: str
    release_id: str
    build_id: str
    artifact_digest: str
    boot_digest: str
    rootfs_digest: str
    boot_expanded_digest: str = ""
    rootfs_expanded_digest: str = ""
    boot_expanded_size: int = 0
    rootfs_expanded_size: int = 0
    boot_encoding: str = ""
    rootfs_encoding: str = ""
    hardware_profile: str = ""

    def to_dict(self):
        return {
            "layout_id": self.layout_id,
            "slot_schema_version": self.slot_schema_version,
            "persistent_schema_version": self.persistent_schema_version,
            "device": self.device,
            "active_slot": self.active_slot,
            "target_slot": self.target_slot,
            "boot_device": self.boot_device,
            "boot_partuuid": self.boot_partuuid,
            "root_device": self.root_device,
            "root_partuuid": self.root_partuuid,
            "release_id": self.release_id,
            "build_id": self.build_id,
            "artifact_digest": self.artifact_digest,
            "boot_digest": self.boot_digest,
            "rootfs_digest": self.rootfs_digest,
            "boot_expanded_digest": self.boot_expanded_digest,
            "rootfs_expanded_digest": self.rootfs_expanded_digest,
            "boot_expanded_size": self.boot_expanded_size,
            "rootfs_expanded_size": self.rootfs_expanded_size,
            "boot_encoding": self.boot_encoding,
            "rootfs_encoding": self.rootfs_encoding,
            "hardware_profile": self.hardware_profile,
        }

    def fingerprint(self):
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def authority_from_dict(payload):
    known = set(WriteAuthority.__dataclass_fields__)
    try:
        return WriteAuthority(**{key: value for key, value in payload.items() if key in known})
    except TypeError:
        raise OsUpdateError("ab_authority_invalid", "the recorded write authority is incomplete")


@dataclass
class UpdatePlan:
    current_release: str
    current_build_id: str
    current_slot: str
    target_release: str
    target_build_id: str
    target_slot: str
    artifact_digest: str
    boot_image_bytes: int
    rootfs_image_bytes: int
    persistent_schema_version: int
    appliance_manager_version: str
    expects_reboot: bool = True
    automatic_fallback: str = ""
    risk: str = "moderate"
    blockers: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    authority: dict = field(default_factory=dict)
    kind: str = "update"

    def to_dict(self):
        return {
            "type": TYPE_OS_ROLLBACK if self.kind == "rollback" else TYPE_OS_UPDATE,
            "kind": self.kind,
            "current_release": self.current_release,
            "current_build_id": self.current_build_id,
            "current_slot": self.current_slot,
            "target_release": self.target_release,
            "target_build_id": self.target_build_id,
            "target_slot": self.target_slot,
            "artifact_digest": self.artifact_digest,
            "boot_image_bytes": self.boot_image_bytes,
            "rootfs_image_bytes": self.rootfs_image_bytes,
            "persistent_schema_version": self.persistent_schema_version,
            "appliance_manager_version": self.appliance_manager_version,
            "expects_reboot": self.expects_reboot,
            "automatic_fallback": self.automatic_fallback,
            "risk": self.risk,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            AUTHORITY_FIELD: dict(self.authority),
        }


FALLBACK_DESCRIPTION = (
    "The trial boot is one-shot. If the new slot does not prove itself, the next "
    "ordinary boot returns to the current slot with nothing changed."
)


class OsUpdateService:
    """Plan, stage and write an OS update into the inactive slot."""

    def __init__(
        self,
        *,
        paths,
        config,
        operations,
        catalogue,
        state,
        probe,
        backend=None,
        packages=None,
        runner=None,
        time_fn=None,
        appliance_version="",
        minimum_staging_bytes=MINIMUM_STAGING_BYTES,
        arm_after_write=True,
        remount_selector=True,
        inspector=None,
        bootstrap=None,
    ):
        self.paths = paths
        self.config = config
        self.operations = operations
        self.catalogue = catalogue
        self.state = state
        self.probe = probe
        self.backend = backend or ab_blocks.RealBlockBackend()
        self.packages = packages
        self.runner = runner
        self._time = time_fn or time.time
        self.appliance_version = appliance_version
        self.minimum_staging_bytes = int(minimum_staging_bytes)
        self.arm_after_write = bool(arm_after_write)
        self.remount_selector = bool(remount_selector)
        self.inspector = inspector or ab_inspect.InactiveSlotInspector(runner=runner)
        self.bootstrap = bootstrap

    # --- discovery -------------------------------------------------------

    def status(self):
        layout = ab_layout.discover(self.probe)
        mounts = self.probe.mounts()
        persistence = ab_persistence.verify(layout, mounts)
        payload = layout.to_dict()
        payload["persistence"] = persistence.to_dict()
        hardware = self._hardware()
        decoder = self._decoder()
        payload["hardware"] = hardware
        payload["artifacts"] = decoder
        payload["readiness"] = self._readiness(layout, persistence, decoder, hardware)
        try:
            payload["ab_state"] = self.state.summary()
        except Exception as exc:  # a corrupt shared state must stay visible
            payload["ab_state"] = {"error": getattr(exc, "code", "ab_state_corrupt")}
        payload["releases"] = [item.to_dict() for item in self._releases()]
        return payload

    def _hardware(self):
        board = self._board()
        return {
            "board_class": board,
            "supported": bool(board),
            "reason": "" if board else "hardware_not_supported",
        }

    def _decoder(self):
        from appliance import install_check

        return install_check.ab_decoder_state()

    def _readiness(self, layout, persistence, decoder, hardware):
        """Every production prerequisite, as one bounded set of booleans.

        Deliberately not a summary of internals: each field answers a question
        an operator can act on, and the plan action is disabled unless all of
        them hold. The image builder is not represented — an appliance never has
        an rpi-image-gen checkout, and a field that is always false would be
        noise; that pair is reported by the build host's own check instead.
        """

        return {
            "hardware_supported": bool(hardware["supported"]),
            "artifact_decoder_ready": bool(decoder["artifact_decoder_ready"]),
            "sparse_decoder_ready": bool(decoder["sparse_decoder_ready"]),
            "persistence_ready": bool(persistence.ok),
            "host_identity_ready": self._host_identity_ready(),
            "docker_reconstruction_ready": self._reconstruction_ready(),
            "layout_ready": bool(layout.may_mutate),
        }

    def _host_identity_ready(self):
        from appliance.host_identity import HostIdentityService

        try:
            return bool(
                HostIdentityService(runner=self.runner, root=self.probe.root).verify().ok
            )
        except Exception:
            return False

    def _reconstruction_ready(self):
        """Can a trial slot rebuild the runtime an operator recovers through?"""

        if self.bootstrap is None:
            return False
        try:
            record = self.bootstrap.store.read()
        except Exception:
            return False
        return any(entry.required for entry in record.images)

    def _releases(self):
        try:
            return self.catalogue.available()
        except os_releases.ReleaseError:
            return []

    # --- planning --------------------------------------------------------

    def plan_update(self, operation, release_id, *, repair=False):
        layout = ab_layout.discover(self.probe)
        release = self.catalogue.get(os_releases.validate_release_id(release_id))
        return self._plan(operation, layout, release, kind="update", repair=repair)

    def plan_rollback(self, operation):
        """Roll back to the recorded previous known-good slot, and only that.

        There is no arbitrary historical image: with two slots, the rollback
        target is the other slot whose exact build and digests were recorded when
        it was the known-good one.
        """

        layout = ab_layout.discover(self.probe)
        history = self.state.slots()
        previous = history.previous_slot
        if not previous:
            raise OsUpdateError(
                "no_previous_known_good_slot",
                "no previous known-good slot has been recorded; there is nothing to roll back to",
            )
        record = history.record(previous)
        if record is None:
            raise OsUpdateError(
                "no_previous_known_good_slot",
                f"slot {previous} is named as previous but its build was never recorded",
            )
        if layout.active_slot and previous == layout.active_slot:
            raise OsUpdateError(
                "rollback_target_is_active",
                f"slot {previous} is the running slot; there is nothing to roll back to",
            )
        return self._plan_recorded_slot(operation, layout, record)

    def _plan(self, operation, layout, release, *, kind, repair=False):
        blockers = self._preconditions(layout, operation)
        board = self._board()
        problems = os_releases.compatibility_problems(
            release,
            layout=layout.manifest,
            board=board,
            appliance_version=self.appliance_version,
            persistent_schema_version=ab_persistence.PERSISTENT_SCHEMA_VERSION,
            current_build_id=str(layout.os_build.get("build_id") or ""),
            repair=repair,
        )
        blockers.extend(problems)
        downgrade = os_releases.downgrade_problem(
            release, current_version=str(layout.os_build.get("release_version") or "")
        )
        if downgrade:
            blockers.append(downgrade)
        if release.verified != os_releases.VERIFIED_SIGNATURE:
            blockers.append(
                {
                    "code": "artifact_not_signed",
                    "message": "this artifact carries no verified signature and is not installable",
                }
            )

        target_slot = layout.inactive_slot
        authority = {}
        boot_bytes = rootfs_bytes = 0
        if layout.may_mutate and target_slot:
            try:
                inactive = ab_layout.prove_inactive_slot(layout, self.probe.mounts())
                boot_bytes, rootfs_bytes = self._image_sizes(release)
                blockers.extend(self._capacity_problems(inactive, boot_bytes, rootfs_bytes))
                authority = self._authority(layout, inactive, release, target_slot).to_dict()
            except LayoutError as exc:
                blockers.append({"code": exc.code, "message": exc.message})

        plan = UpdatePlan(
            current_release=str(layout.os_build.get("release_version") or "unknown"),
            current_build_id=str(layout.os_build.get("build_id") or "unknown"),
            current_slot=layout.active_slot,
            target_release=release.release_version,
            target_build_id=release.build_id,
            target_slot=target_slot,
            artifact_digest=release.archive_digest,
            boot_image_bytes=boot_bytes,
            rootfs_image_bytes=rootfs_bytes,
            persistent_schema_version=release.persistent_schema_version,
            appliance_manager_version=release.minimum_appliance_manager_version,
            automatic_fallback=FALLBACK_DESCRIPTION,
            risk="moderate" if not blockers else "blocked",
            blockers=blockers,
            authority=authority,
            kind=kind,
        )
        self.operations.update_target(
            operation.operation_id,
            {"release_id": release.release_id, AUTHORITY_FIELD: authority, "kind": kind},
        )
        return plan.to_dict()

    def _plan_recorded_slot(self, operation, layout, record):
        """A rollback plan: the target is a slot, not a downloadable artifact."""

        blockers = self._preconditions(layout, operation)
        if layout.may_mutate and layout.inactive_slot != record.slot:
            blockers.append(
                {
                    "code": "rollback_target_not_inactive",
                    "message": (
                        f"the recorded previous slot is {record.slot}, but the inactive slot is "
                        f"{layout.inactive_slot or 'unknown'}"
                    ),
                }
            )
        authority = {}
        if layout.may_mutate and layout.inactive_slot == record.slot:
            try:
                inactive = ab_layout.prove_inactive_slot(layout, self.probe.mounts())
                authority = WriteAuthority(
                    layout_id=layout.manifest.layout_id,
                    slot_schema_version=layout.manifest.slot_schema_version,
                    persistent_schema_version=ab_persistence.PERSISTENT_SCHEMA_VERSION,
                    device=layout.device,
                    active_slot=layout.active_slot,
                    target_slot=record.slot,
                    boot_device=inactive.boot.path,
                    boot_partuuid=inactive.boot.partuuid,
                    root_device=inactive.root.path,
                    root_partuuid=inactive.root.partuuid,
                    release_id=record.build_id,
                    build_id=record.build_id,
                    artifact_digest=record.artifact_digest,
                    boot_digest=record.boot_digest,
                    rootfs_digest=record.rootfs_digest,
                ).to_dict()
            except LayoutError as exc:
                blockers.append({"code": exc.code, "message": exc.message})

        plan = UpdatePlan(
            current_release=str(layout.os_build.get("release_version") or "unknown"),
            current_build_id=str(layout.os_build.get("build_id") or "unknown"),
            current_slot=layout.active_slot,
            target_release=record.release_version,
            target_build_id=record.build_id,
            target_slot=record.slot,
            artifact_digest=record.artifact_digest,
            boot_image_bytes=0,
            rootfs_image_bytes=0,
            persistent_schema_version=ab_persistence.PERSISTENT_SCHEMA_VERSION,
            appliance_manager_version=self.appliance_version,
            automatic_fallback=FALLBACK_DESCRIPTION,
            risk="moderate" if not blockers else "blocked",
            blockers=blockers,
            authority=authority,
            kind="rollback",
        )
        self.operations.update_target(
            operation.operation_id,
            {"rollback_slot": record.slot, AUTHORITY_FIELD: authority, "kind": "rollback"},
        )
        return plan.to_dict()

    # --- preconditions ---------------------------------------------------

    def _preconditions(self, layout, operation=None):
        blockers = []
        if layout.mode == ab_layout.MODE_SINGLE_SLOT:
            blockers.append(
                {
                    "code": "ab_layout_not_present",
                    "message": (
                        "this appliance has a single root filesystem; A/B OS updates require "
                        "reinstalling onto an A/B-capable appliance image"
                    ),
                }
            )
            return blockers
        if layout.drift:
            blockers.append(
                {
                    "code": "layout_drift",
                    "message": "the A/B layout could not be proven: " + "; ".join(layout.drift),
                }
            )
        if not layout.active_slot:
            blockers.append(
                {"code": "active_slot_unknown", "message": "the running slot could not be proven"}
            )
        if not layout.inactive_slot:
            blockers.append(
                {
                    "code": "inactive_slot_unknown",
                    "message": "the inactive slot could not be identified",
                }
            )

        persistence = ab_persistence.verify(layout, self.probe.mounts())
        if not persistence.ok:
            blockers.append(
                {
                    "code": "persistence_unavailable",
                    "message": "the shared persistent partition is not usable: "
                    + "; ".join(persistence.problems),
                }
            )

        # The operation that is doing the planning is itself active; a
        # conflict means some *other* mutation holds the appliance.
        planning_id = getattr(operation, "operation_id", "")
        active = self.operations.active()
        if active is not None and active.operation_id != planning_id:
            blockers.append(
                {
                    "code": "operation_conflict",
                    "message": f"{active.type} ({active.operation_id}) is still running",
                }
            )

        if self.packages is not None:
            try:
                package_state = self.packages.check()
            except Exception:
                package_state = None
            if package_state is not None and getattr(package_state, "lock_state", "") == "held":
                blockers.append(
                    {
                        "code": "package_operation_active",
                        "message": "a package installation is running; wait until it finishes",
                    }
                )

        pending = self.state.pending()
        if pending is not None and not pending.committed:
            blockers.append(
                {
                    "code": "ab_trial_pending",
                    "message": (
                        f"a trial boot of slot {pending.target_slot} is already pending; "
                        "acknowledge it before planning another update"
                    ),
                }
            )

        try:
            self.inspector.assert_no_leak()
        except ab_inspect.InspectionError as exc:
            blockers.append({"code": exc.code, "message": exc.message})

        # The tools have to be there before an artifact is fetched, not after:
        # a missing decompressor discovered mid-staging has already spent the
        # persistent partition's free space on something unreadable.
        decoder = self._decoder()
        if not decoder["artifact_decoder_ready"]:
            blockers.append(
                {
                    "code": "artifact_decoder_missing",
                    "message": (
                        "this appliance cannot read a .tar.zst update artifact; install "
                        + ", ".join(decoder["packages"] or ["zstd"])
                    ),
                }
            )

        free = self._staging_free_bytes()
        if free is not None and free < self.minimum_staging_bytes:
            blockers.append(
                {
                    "code": "insufficient_staging_space",
                    "message": (
                        f"the persistent partition has {free // (1024 * 1024)} MiB free; "
                        f"staging an OS artifact needs at least "
                        f"{self.minimum_staging_bytes // (1024 * 1024)} MiB"
                    ),
                }
            )
        return blockers

    def _staging_free_bytes(self):
        try:
            stats = os.statvfs(str(self.state.directory))
        except OSError:
            return None
        return stats.f_bavail * stats.f_frsize

    def _board(self):
        """The bounded board class this appliance is, or nothing.

        A raw ``compatible`` string is not an answer an artefact can be matched
        against: the same board answers to several, and an image built for
        another SoC does not boot. An unidentified board blocks the update.
        """

        return rpi_image_gen.detect_board_class(self.probe.root)

    def _image_sizes(self, release):
        """What each partition has to hold: the expanded image, not the member.

        The manifest signs both sizes, so capacity is decided at planning time
        rather than discovered after the archive has already been staged.
        """

        return release.boot_member().expanded_size, release.root_member().expanded_size

    def _capacity_problems(self, inactive, boot_bytes, rootfs_bytes):
        problems = []
        if boot_bytes and inactive.boot.size_bytes < boot_bytes:
            problems.append(
                {
                    "code": "inactive_partition_too_small",
                    "message": (
                        f"the boot image needs {boot_bytes} bytes, slot {inactive.slot} has "
                        f"{inactive.boot.size_bytes}"
                    ),
                }
            )
        if rootfs_bytes and inactive.root.size_bytes < rootfs_bytes:
            problems.append(
                {
                    "code": "inactive_partition_too_small",
                    "message": (
                        f"the root image needs {rootfs_bytes} bytes, slot {inactive.slot} has "
                        f"{inactive.root.size_bytes}"
                    ),
                }
            )
        return problems

    def _authority(self, layout, inactive, release, target_slot):
        return WriteAuthority(
            layout_id=layout.manifest.layout_id,
            slot_schema_version=layout.manifest.slot_schema_version,
            persistent_schema_version=ab_persistence.PERSISTENT_SCHEMA_VERSION,
            device=layout.device,
            active_slot=layout.active_slot,
            target_slot=target_slot,
            boot_device=inactive.boot.path,
            boot_partuuid=inactive.boot.partuuid,
            root_device=inactive.root.path,
            root_partuuid=inactive.root.partuuid,
            release_id=release.release_id,
            build_id=release.build_id,
            artifact_digest=release.archive_digest,
            boot_digest=release.boot_member().encoded_digest,
            rootfs_digest=release.root_member().encoded_digest,
            boot_expanded_digest=release.boot_member().expanded_digest,
            rootfs_expanded_digest=release.root_member().expanded_digest,
            boot_expanded_size=release.boot_member().expanded_size,
            rootfs_expanded_size=release.root_member().expanded_size,
            boot_encoding=release.boot_member().encoding,
            rootfs_encoding=release.root_member().encoding,
            hardware_profile=release.device_layer,
        )

    # --- execution -------------------------------------------------------

    def execute(self, operation):
        record = self.operations.get(operation.operation_id)
        recorded = (record.requested_target or {}).get(AUTHORITY_FIELD) or {}
        if not recorded:
            self._fail(operation, "ab_authority_missing", "this plan carries no write authority")
        authority = authority_from_dict(recorded)
        kind = str((record.requested_target or {}).get("kind") or "update")

        try:
            self._revalidate(authority)
        except (OsUpdateError, LayoutError, ab_inspect.InspectionError) as exc:
            self._fail(operation, exc.code, exc.message)

        if kind == "rollback":
            # The previous known-good slot is already on the medium; a rollback
            # writes nothing and proves the slot through the same trial boot.
            return self._arm(operation, authority, kind=kind, release_version="")

        release = self.catalogue.get(authority.release_id)
        archive = self.catalogue.archive_path(release)

        self.operations.advance(operation.operation_id, STAGE_STAGING, state=STATE_RUNNING)
        self.catalogue.verify_archive(release, archive)
        staged = os_artifacts.extract(archive, self.state.staging_dir, release)

        # Both members are containers, not filesystems. Nothing may reach a
        # partition until each one has been structurally validated against the
        # size the manifest signed, expanded, and hashed as what a partition
        # will actually read back.
        try:
            self.operations.advance(operation.operation_id, STAGE_SPARSE_VALIDATED)
            self._validate_members(authority, release, staged)
            self.operations.advance(operation.operation_id, STAGE_IMAGE_EXPANDING)
            boot_source = self._expand_member(
                staged, release.boot_member(), authority.boot_expanded_digest
            )
            root_source = self._expand_member(
                staged, release.root_member(), authority.rootfs_expanded_digest
            )
            self.operations.advance(operation.operation_id, STAGE_EXPANDED_VERIFIED)
        except sparse.SparseError as exc:
            os_artifacts.discard(self.state.staging_dir)
            self.operations.finish(
                operation.operation_id,
                STATE_FAILED_RECOVERABLE,
                stage=STAGE_IMAGE_EXPANDING,
                result={
                    "default_slot_unchanged": True,
                    "target_slot": authority.target_slot,
                    "inactive_slot_untouched": True,
                },
                error={"code": exc.code, "message": exc.message},
            )
            raise OsUpdateError(exc.code, exc.message)

        # The target slot stops being a known-good rollback candidate here,
        # before the first destructive byte. An interrupted write must never
        # leave a slot that a later rollback would offer as intact.
        self.state.invalidate_slot(authority.target_slot)

        self.operations.advance(operation.operation_id, STAGE_WRITING_INACTIVE)
        try:
            boot_digest = self._write_partition(
                authority.boot_device, boot_source, expected=authority.boot_expanded_digest
            )
            rootfs_digest = self._write_partition(
                authority.root_device, root_source, expected=authority.rootfs_expanded_digest
            )
        except BlockError as exc:
            os_artifacts.discard(self.state.staging_dir)
            self.operations.finish(
                operation.operation_id,
                STATE_FAILED_RECOVERABLE,
                stage=STAGE_WRITING_INACTIVE,
                result={"default_slot_unchanged": True, "target_slot": authority.target_slot},
                error={"code": exc.code, "message": exc.message},
            )
            raise OsUpdateError(exc.code, exc.message)

        self.operations.advance(
            operation.operation_id, STAGE_VERIFYING_INACTIVE, state=STATE_VERIFYING
        )
        os_artifacts.discard(self.state.staging_dir)

        # Matching bytes are a statement about the medium, not about the slot,
        # and after the reboot the appliance is already running what was written.
        inspection = self._inspect(operation, authority, release)

        result = {
            "target_slot": authority.target_slot,
            "boot_device": authority.boot_device,
            "root_device": authority.root_device,
            "boot_digest": boot_digest,
            "rootfs_digest": rootfs_digest,
            "default_slot_unchanged": True,
            "inspection": inspection.to_dict(),
            "stage": STAGE_READY_FOR_TRYBOOT,
        }
        self.operations.advance(operation.operation_id, STAGE_READY_FOR_TRYBOOT)
        if self.arm_after_write:
            result.update(
                self._arm(
                    operation, authority, kind="update", release_version=release.release_version
                )
            )
        return result

    def _validate_members(self, authority, release, staged):
        """Read every container's header before anything is expanded or written.

        A signed artifact is far more likely to be wrong because the release
        pipeline was, than because someone forged it. The bounds are checked
        anyway: an expanded size the target partition cannot hold has to fail
        here, not with a partially overwritten slot.
        """

        capacities = {
            release.boot_member().name: self.backend.size(authority.boot_device),
            release.root_member().name: self.backend.size(authority.root_device),
        }
        for member in (release.boot_member(), release.root_member()):
            if member.encoding != sparse.ENCODING_ANDROID_SPARSE:
                continue
            sparse.inspect(staged.path(member.name), expected_size=member.expanded_size)
            capacity = capacities[member.name]
            if member.expanded_size > capacity:
                raise sparse.SparseError(
                    "inactive_partition_too_small",
                    f"{member.name} expands to {member.expanded_size} bytes, the target "
                    f"partition holds {capacity}",
                )

    def _expand_member(self, staged, member, expected_digest):
        """Turn one container into the filesystem the manifest describes."""

        source = staged.path(member.name)
        if member.encoding != sparse.ENCODING_ANDROID_SPARSE:
            return source
        destination = staged.directory / f"{member.name}.img"
        sparse.expand(
            source,
            destination,
            expected_size=member.expanded_size,
            expected_digest=expected_digest,
            free_bytes=self._staging_free_bytes(),
        )
        # The container has served its purpose and is the larger of the two on
        # a mostly-full staging partition.
        try:
            source.unlink()
        except OSError:
            pass
        return destination

    def _inspect(self, operation, authority, release):
        """Prove the written slot before anything points the firmware at it."""

        try:
            report = self.inspector.inspect(
                authority, release, appliance_version=release.appliance_manager_version
            )
        except ab_inspect.InspectionError as exc:
            self._fail_after_write(operation, authority, exc.code, exc.message)
        if not report.ok:
            self._fail_after_write(
                operation,
                authority,
                "inactive_slot_inspection_failed",
                "the written slot did not pass inspection: " + "; ".join(report.problems)
                or "the inspection mounts could not be cleaned up",
                result={"inspection": report.to_dict()},
            )
        return report

    def _fail_after_write(self, operation, authority, code, message, *, result=None):
        """The target slot is written but unusable; the default slot is intact."""

        payload = {"default_slot_unchanged": True, "target_slot": authority.target_slot}
        payload.update(result or {})
        self.operations.finish(
            operation.operation_id,
            STATE_FAILED_RECOVERABLE,
            stage=STAGE_VERIFYING_INACTIVE,
            result=payload,
            error={"code": code, "message": message},
        )
        raise OsUpdateError(code, message)

    # --- the trial boot ----------------------------------------------------

    def _arm(self, operation, authority, *, kind, release_version):
        """Record the trial durably, point [tryboot] at it, then ask to reboot.

        The pending record goes first. A selector armed without it would boot a
        slot that cannot prove what it is, which is manual_action_required; a
        record without an armed selector is an ordinary boot of the source slot,
        which classifies cleanly as a fallback. Both are safe, and this order is
        the one that always leaves the trial slot able to identify itself.
        """

        trial = self.pending_trial(
            operation, authority, kind=kind, release_version=release_version
        )
        self.state.set_pending(trial)
        seeded = self._seed_runtime()

        layout = ab_layout.discover(self.probe)
        manifest = layout.manifest
        transaction = ab_boot.SelectorTransaction(
            self._selector_path(manifest),
            runner=self.runner,
            mountpoint=manifest.selector_mountpoint,
            remount=self.remount_selector,
        )
        try:
            selector = transaction.arm_trial(
                default_partition=layout.slot_devices(authority.active_slot).boot.number,
                trial_partition=trial.expected_boot_partition,
            )
        except ab_boot.SelectorError as exc:
            self.state.clear_pending()
            self.operations.finish(
                operation.operation_id,
                STATE_FAILED_RECOVERABLE,
                stage=STAGE_READY_FOR_TRYBOOT,
                result={"default_slot_unchanged": True, "target_slot": authority.target_slot},
                error={"code": exc.code, "message": exc.message},
            )
            raise OsUpdateError(exc.code, exc.message)

        self.operations.advance(operation.operation_id, STAGE_TRYBOOT_REQUESTED)
        payload = {
            "stage": STAGE_TRYBOOT_REQUESTED,
            "selector": selector.to_dict(),
            "pending_trial": trial.to_dict(),
            "runtime_seed": seeded,
            "default_slot_unchanged": True,
        }
        try:
            ab_boot.request_trial_reboot(self.runner)
            payload["reboot_requested"] = True
        except ab_boot.SelectorError as exc:
            payload["reboot_requested"] = False
            payload["reboot_error"] = exc.code
        return payload

    def _seed_runtime(self):
        """Put the running slot's images where the trial slot can load them.

        /var/lib/docker is per-slot, so the slot that is about to boot has an
        empty image store. Seeding is what lets an appliance with no registry
        access finish a trial instead of coming up without an Admin console.
        A seed that could not be written is reported, not fatal: the trial slot
        can still pull the same digests, and its health gates decide.
        """

        if self.bootstrap is None:
            return {"seeded": [], "reason": "no runtime bootstrap is configured"}
        try:
            record = self.bootstrap.record_running_runtime()
            return {"seeded": list(self.bootstrap.seed(record))}
        except Exception as exc:
            return {"seeded": [], "reason": getattr(exc, "code", "runtime_seed_failed")}

    def _selector_path(self, manifest):
        return self.probe.root / str(manifest.selector_mountpoint).lstrip("/") / (
            ab_boot.SELECTOR_NAME
        )

    def _write_partition(self, device, source, *, expected):
        """Write one image, flush it, and prove the medium holds it.

        The read-back is not a formality: a write that returned success and a
        flush that returned success are both statements about a cache.
        """

        size = os.path.getsize(str(source))
        capacity = self.backend.size(device)
        if capacity < size:
            raise BlockError(
                "inactive_partition_too_small",
                f"{device} holds {capacity} bytes, the image needs {size}",
            )
        written = self.backend.write(device, source, expected_bytes=size)
        if written != expected:
            raise BlockError(
                "block_write_digest_mismatch",
                f"{device} was written with {written}, the manifest declares {expected}",
            )
        self.backend.flush_device(device)
        observed = self.backend.read_digest(device, size)
        if observed != expected:
            raise BlockError(
                "block_readback_mismatch",
                f"{device} reads back as {observed}, the manifest declares {expected}",
            )
        return observed

    def _revalidate(self, authority):
        """The disk as it is now must still be the disk the plan was made against."""

        self.inspector.assert_no_leak()
        layout = ab_layout.discover(self.probe)
        if not layout.may_mutate:
            raise OsUpdateError(
                "layout_drift", "the A/B layout no longer matches the plan: " + "; ".join(layout.drift)
            )
        if layout.manifest.layout_id != authority.layout_id:
            raise OsUpdateError("layout_changed", "the appliance layout changed after the plan")
        if layout.active_slot != authority.active_slot:
            raise OsUpdateError("active_slot_changed", "the running slot changed after the plan")
        if layout.inactive_slot != authority.target_slot:
            raise OsUpdateError(
                "inactive_slot_changed", "the inactive slot changed after the plan"
            )
        if layout.device != authority.device:
            raise OsUpdateError("device_changed", "the storage device changed after the plan")

        inactive = ab_layout.prove_inactive_slot(layout, self.probe.mounts())
        if inactive.boot.path != authority.boot_device or inactive.root.path != (
            authority.root_device
        ):
            raise OsUpdateError(
                "inactive_slot_changed", "the inactive slot's devices changed after the plan"
            )
        if inactive.boot.partuuid != authority.boot_partuuid or inactive.root.partuuid != (
            authority.root_partuuid
        ):
            raise OsUpdateError(
                "inactive_slot_changed", "the inactive slot's identity changed after the plan"
            )
        active = layout.slot_devices(layout.active_slot)
        if authority.boot_device in (active.boot.path, active.root.path) or (
            authority.root_device in (active.boot.path, active.root.path)
        ):
            raise OsUpdateError(
                "write_would_touch_active_slot",
                "the recorded write target resolves to the running slot",
            )
        return layout

    def _fail(self, operation, code, message):
        self.operations.finish(
            operation.operation_id,
            STATE_FAILED_TERMINAL,
            stage="preflight_failed",
            result={"default_slot_unchanged": True},
            error={"code": code, "message": message},
        )
        raise OsUpdateError(code, message)

    # --- pending trial ---------------------------------------------------

    def pending_trial(self, operation, authority, *, kind="update", release_version=""):
        """The record the target slot reads after the reboot.

        Written and flushed before the reboot is requested: a trial that booted
        without it cannot prove what it is, and becomes manual_action_required.
        """

        return PendingTrial(
            operation_id=operation.operation_id,
            source_slot=authority.active_slot,
            target_slot=authority.target_slot,
            target_release=authority.release_id,
            target_build_id=authority.build_id,
            artifact_digest=authority.artifact_digest,
            expected_boot_partition=self._boot_partition(authority),
            expected_root_partuuid=authority.root_partuuid,
            trial_requested_at=self._time(),
            boot_digest=authority.boot_digest,
            rootfs_digest=authority.rootfs_digest,
            release_version=release_version,
            kind=kind,
        )

    def _boot_partition(self, authority):
        """The partition number the firmware will be asked to trial-boot.

        Discovered from the medium, never declared: ``image-rota`` generates the
        table, so a number this project wrote down would be a second authority.
        """

        layout = ab_layout.discover(self.probe)
        if layout.manifest is None:
            raise OsUpdateError("ab_layout_not_present", "this appliance has no A/B layout")
        return layout.slot_devices(authority.target_slot).boot.number
