# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fetch, plan and apply an Appliance Manager package on an operator's button.

Never on a timer: an automatic update would distribute an untested package to
every appliance at once, and the revert this path provides has to be a decision
somebody made. Going backwards is a first-class outcome for the same reason —
it is the only recovery a single-slot appliance has.

The fetch order is the one ``os_fetch`` established and is the security
property: an index only names candidates, the detached signature decides
whether the manifest may be believed, and only a verified manifest says what
the archive must hash to.

See docs/appliance/os-updates.md.
"""

import json
import shutil
import time
from pathlib import Path

from appliance import (
    manager_install,
    manager_releases,
    manager_retention,
    manager_verify,
    os_fetch,
    os_releases,
    persistent_state,
)
from appliance.version import version_key

TYPE_MANAGER_UPDATE = "manager.update"
TYPE_MANAGER_REVERT = "manager.revert"

DIRECTION_UPGRADE = "upgrade"
DIRECTION_DOWNGRADE = "downgrade"
DIRECTION_REINSTALL = "reinstall"
DIRECTION_REVERT = "revert"

STAGING_PREFIX = ".manager-fetch-"

MAX_PACKAGE_BYTES = 256 * 1024 * 1024
FREE_SPACE_MARGIN = 128 * 1024 * 1024

NO_WAY_BACK = (
    "This appliance has kept no earlier package, so a failed install has no way back "
    "except the console recovery procedure."
)


class ManagerUpdateError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def direction(*, offered, installed):
    """Which way this install moves, by the project's own version order."""

    if not installed:
        return DIRECTION_UPGRADE
    offered_key, installed_key = version_key(offered), version_key(installed)
    if offered_key > installed_key:
        return DIRECTION_UPGRADE
    if offered_key < installed_key:
        return DIRECTION_DOWNGRADE
    return DIRECTION_REINSTALL


class ManagerUpdateService:
    """One owner for every mutation of the installed Appliance Manager."""

    def __init__(
        self,
        *,
        paths,
        config,
        verifier,
        probe,
        operations,
        runner,
        state_mountpoint,
        fetcher=None,
        time_fn=None,
        operation_log=None,
        installed_version="",
        architecture="arm64",
        reverter=manager_verify.PACKAGED_REVERTER,
    ):
        self.paths = paths
        self.config = config
        self.verifier = verifier
        self.probe = probe
        self.operations = operations
        self.runner = runner
        self.state_mountpoint = Path(state_mountpoint)
        self.fetcher = fetcher or os_fetch.HttpsFetcher()
        self.installed_version = installed_version
        self.architecture = architecture
        self.reverter = reverter
        self._time = time_fn
        self._operation_log = operation_log

    # --- preconditions ----------------------------------------------------

    def _now(self):
        return self._time() if self._time is not None else time.time()

    def _require_clock(self):
        """The board has no real-time clock, and both TLS and gpgv judge by it."""

        record = self.probe.system_time()
        if record.get("ntp_synchronized") is True:
            return record
        raise ManagerUpdateError(
            "clock_not_synchronised",
            "the system clock is not known to be synchronised; this board has no real-time "
            "clock, so a download now would fail certificate and signature checks for reasons "
            "that do not name the clock. Wait for time synchronisation and try again",
        )

    def _packages_dir(self):
        directory = Path(self.paths.packages_dir)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    # --- what this appliance's state is formatted as ----------------------

    def _state_schemas(self, *, claim):
        """The recorded schemas, claiming unclaimed axes only when asked to.

        ``None`` is undecidable and every caller refuses on it. Planning reads;
        executing claims, so the record is durable before the package that has
        to be able to read it is unpacked.
        """

        verdict, stamp = persistent_state.reconcile(
            self.state_mountpoint,
            written_by={"version": self.installed_version},
            written_at=str(self._now()),
            write=claim,
        )
        if verdict.outcome == persistent_state.STATE_UNREADABLE:
            return None, verdict
        return persistent_state.merge(stamp.schemas, verdict.implemented), verdict

    # --- discovery --------------------------------------------------------

    def sources(self):
        """What the configured index offers. Nothing here is trusted."""

        listing = {
            "configured": False,
            "error": "",
            "releases": [],
            "installed_version": self.installed_version,
        }
        try:
            url = os_fetch.https_url(
                self.config.manager_index_url, label="the manager package index url"
            )
        except os_fetch.FetchError:
            return listing
        listing["configured"] = True
        try:
            raw = self.fetcher.read(
                url, label="the manager package index", max_bytes=os_fetch.MAX_INDEX_BYTES
            )
            candidates = os_fetch.parse_index(json.loads(raw.decode("utf-8", errors="replace")))
        except os_fetch.FetchError as exc:
            listing["error"] = exc.code
            return listing
        except ValueError:
            listing["error"] = "release_index_invalid"
            return listing
        for candidate in candidates:
            described = candidate.get("described") or {}
            candidate["direction"] = direction(
                offered=str(described.get("release_version") or ""),
                installed=self.installed_version,
            )
        listing["releases"] = candidates
        return listing

    def _candidate(self, release_id):
        wanted = os_releases.validate_release_id(release_id)
        listing = self.sources()
        if not listing["configured"]:
            raise ManagerUpdateError(
                "release_source_unconfigured",
                "no manager package index is configured, so this appliance cannot fetch one",
            )
        if listing["error"]:
            raise ManagerUpdateError(
                listing["error"], "the manager package index could not be read"
            )
        for candidate in listing["releases"]:
            if candidate["release_id"] == wanted:
                return candidate
        raise ManagerUpdateError(
            "unknown_release", f"{wanted} is not offered by the manager package index"
        )

    # --- status -----------------------------------------------------------

    def status(self):
        retention = manager_retention.read(self.paths)
        return {
            "installed_version": self.installed_version,
            "configured": bool(str(self.config.manager_index_url or "").strip()),
            "retention": retention.to_dict(),
            "can_revert": retention.can_revert,
            "verify": manager_verify.read(self.paths).to_dict(),
            "verdict": manager_verify.read_verdict(self.paths).to_dict(),
            "outcome": manager_install.read_outcome(self.paths).to_dict(),
        }

    # --- planning ---------------------------------------------------------

    def plan_update(self, operation, release_id):
        """Say what would be installed and what stands in the way. Writes nothing."""

        self._advance(operation, "preflight")
        self._require_clock()
        candidate = self._candidate(release_id)

        staging = self._packages_dir() / f"{STAGING_PREFIX}plan-{operation.operation_id}"
        try:
            staging.mkdir(mode=0o700, exist_ok=False)
            release = self._verified_manifest(operation, candidate, staging)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        recorded, verdict = self._state_schemas(claim=False)
        blockers = list(
            manager_releases.compatibility_problems(
                release, architecture=self.architecture, state_schemas=recorded
            )
        )
        if verdict.outcome == persistent_state.STATE_BEHIND:
            blockers.append({"code": "state_schema_behind", "message": verdict.detail})

        retention = manager_retention.read(self.paths)
        moving = direction(offered=release.version, installed=self.installed_version)
        self.operations.update_target(
            operation.operation_id,
            {"release_id": candidate["release_id"], "version": release.version},
        )
        return {
            "type": TYPE_MANAGER_UPDATE,
            "release_id": candidate["release_id"],
            "version": release.version,
            "build_id": release.build_id,
            "installed_version": self.installed_version,
            "direction": moving,
            "size_bytes": release.artifact_size,
            "digest": release.artifact_digest,
            "blockers": blockers,
            # What a revert would have *after* this install, not before it: the
            # package running now becomes the kept one when it is displaced.
            "revert_available": retention.current.present,
            "verify_window_seconds": manager_verify.DEFAULT_WINDOW_SECONDS,
            "warning": self._warning(moving, retention.current.present),
        }

    @staticmethod
    def _warning(moving, revert_available):
        parts = []
        if moving == DIRECTION_DOWNGRADE:
            parts.append("This installs an older Appliance Manager than the one running.")
        parts.append(
            "The appliance restarts its agent and web service during the install, so this "
            "console is briefly unreachable."
        )
        if not revert_available:
            parts.append(NO_WAY_BACK)
        return " ".join(parts)

    def plan_revert(self, operation):
        """Say which kept package would go back on. Writes nothing."""

        self._advance(operation, "preflight")
        try:
            target = manager_retention.revert_target(self.paths)
        except manager_retention.RetentionError as exc:
            raise ManagerUpdateError(exc.code, exc.message)

        recorded, verdict = self._state_schemas(claim=False)
        blockers = list(self._retained_problems(target, recorded))
        if verdict.outcome == persistent_state.STATE_BEHIND:
            blockers.append({"code": "state_schema_behind", "message": verdict.detail})

        self.operations.update_target(
            operation.operation_id,
            {"sha256": target.sha256, "version": target.version},
        )
        return {
            "type": TYPE_MANAGER_REVERT,
            "version": target.version,
            "build_id": target.build_id,
            "installed_version": self.installed_version,
            "direction": DIRECTION_REVERT,
            "digest": target.sha256,
            "blockers": blockers,
            "state_compatibility_known": bool(target.state_implements),
            "verify_window_seconds": manager_verify.DEFAULT_WINDOW_SECONDS,
            "warning": (
                "This puts back the package this appliance was running before the last install. "
                + (
                    ""
                    if target.state_implements
                    else "It arrived before this manager recorded what a package can read, so "
                    "its state compatibility could not be checked."
                )
            ).strip(),
        }

    def _retained_problems(self, target, recorded):
        """A kept package judged on what was written down when it arrived.

        A package with no recorded declaration is not refused: the revert is the
        only recovery a single-slot appliance has, and taking it away because of
        a missing annotation is worse than the risk it describes.
        """

        if not target.state_implements:
            return []
        stand_in = manager_releases.ManagerRelease(
            release_id=target.build_id or "retained",
            version=target.version,
            architecture=target.architecture or self.architecture,
            build_id=target.build_id,
            created_at=target.retained_at,
            project_revision="",
            artifact_name=Path(target.path).name,
            artifact_digest=target.sha256,
            artifact_size=1,
            state_implements=target.state_implements,
            state_reads=target.state_reads,
        )
        return manager_releases.compatibility_problems(
            stand_in, architecture=self.architecture, state_schemas=recorded
        )

    # --- execution --------------------------------------------------------

    def execute(self, operation):
        if operation.type == TYPE_MANAGER_REVERT:
            return self._execute_revert(operation)
        return self._execute_update(operation)

    def _execute_update(self, operation):
        release_id = str((operation.requested_target or {}).get("release_id") or "")
        self._advance(operation, "preflight")
        self._require_clock()
        candidate = self._candidate(release_id)

        directory = self._packages_dir()
        staging = directory / f"{STAGING_PREFIX}{operation.operation_id}"
        try:
            staging.mkdir(mode=0o700, exist_ok=False)
        except OSError as exc:
            raise ManagerUpdateError(
                "release_staging_unavailable", f"{staging} could not be created: {exc}"
            )
        try:
            release, archive = self._fetch_into(operation, candidate, staging)
            return self._apply(operation, release=release, archive=archive)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _verified_manifest(self, operation, candidate, staging):
        """The manifest, once its detached signature has been believed.

        Everything below this call reads the manifest as an authority, so
        nothing above it may.
        """

        self._advance(operation, "fetching_manifest", detail=candidate["manifest_url"])
        manifest_bytes = self.fetcher.read(
            candidate["manifest_url"],
            label="the package manifest",
            max_bytes=os_releases.MAX_MANIFEST_BYTES,
        )
        signature_bytes = self.fetcher.read(
            candidate["signature_url"],
            label="the manifest signature",
            max_bytes=os_fetch.MAX_SIGNATURE_BYTES,
        )
        manifest_path = staging / f"{candidate['release_id']}.manifest.json"
        signature_path = Path(str(manifest_path) + ".asc")
        manifest_path.write_bytes(manifest_bytes)
        signature_path.write_bytes(signature_bytes)

        self._advance(operation, "verifying_signature")
        self.verifier.verify(manifest_path, signature_path)
        try:
            payload = json.loads(manifest_bytes.decode("utf-8"))
        except ValueError:
            raise ManagerUpdateError(
                "manager_manifest_invalid", f"{manifest_path.name} is not valid JSON"
            )
        return manager_releases.parse_manifest(
            payload,
            release_id=candidate["release_id"],
            verified=manager_releases.VERIFIED_SIGNATURE,
        )

    def _fetch_into(self, operation, candidate, staging):
        release = self._verified_manifest(operation, candidate, staging)

        declared = int(release.artifact_size)
        if declared <= 0 or declared > MAX_PACKAGE_BYTES:
            raise ManagerUpdateError(
                "manager_manifest_invalid",
                f"the verified manifest declares a package size of {declared} bytes",
            )
        usage = shutil.disk_usage(staging)
        if usage.free < declared + FREE_SPACE_MARGIN:
            raise ManagerUpdateError(
                "packages_directory_full",
                f"{staging.parent} has {usage.free} bytes free and this package needs "
                f"{declared + FREE_SPACE_MARGIN}",
            )

        archive = staging / Path(release.artifact_name or "package.deb").name
        self._advance(operation, "fetching_package", detail=archive.name)
        observed = self.fetcher.download(
            candidate["archive_url"],
            archive,
            label="the manager package",
            expected_bytes=declared,
        )
        if observed != release.artifact_digest:
            raise ManagerUpdateError(
                "manager_artifact_corrupt",
                f"the downloaded package hashes to {observed}, the verified manifest "
                f"declares {release.artifact_digest}",
            )
        return release, archive

    def _apply(self, operation, *, release, archive):
        recorded, verdict = self._state_schemas(claim=True)
        if verdict.outcome == persistent_state.STATE_BEHIND:
            raise ManagerUpdateError("state_schema_behind", verdict.detail)

        self._advance(operation, "staging_package")
        retention = manager_install.prepare(
            self.paths,
            release=release,
            archive=archive,
            state_schemas=recorded,
            architecture=self.architecture,
            retained_at=str(self._now()),
        )
        return self._arm_and_start(
            operation,
            version=release.version,
            build_id=release.build_id,
            previous=retention.previous.path,
        )

    def _execute_revert(self, operation):
        self._advance(operation, "preflight")
        try:
            target = manager_retention.revert_target(self.paths)
        except manager_retention.RetentionError as exc:
            raise ManagerUpdateError(exc.code, exc.message)

        _, verdict = self._state_schemas(claim=True)
        if verdict.outcome == persistent_state.STATE_BEHIND:
            raise ManagerUpdateError("state_schema_behind", verdict.detail)

        self._advance(operation, "staging_package")
        _, retention = manager_install.prepare_revert(
            self.paths, retained_at=str(self._now())
        )
        return self._arm_and_start(
            operation,
            version=target.version,
            build_id=target.build_id,
            previous=retention.previous.path,
        )

    def _arm_and_start(self, operation, *, version, build_id, previous):
        """Arm the deadline, then start the install. Never the other way round.

        Afterwards the process doing the arming is the one being replaced.
        """

        self._advance(operation, "arming_deadline")
        deadline, _ = manager_verify.arm(
            self.paths,
            self.runner,
            expected_version=version,
            build_id=build_id,
            previous=previous,
            now=int(self._now()),
            operation_id=operation.operation_id,
            reverter=self.reverter,
        )
        self._advance(operation, "starting_install")
        manager_install.start(self.runner)
        return {
            "stage": "install_started",
            "version": version,
            "build_id": build_id,
            "deadline_epoch": deadline.deadline_epoch,
            "revert_available": deadline.revert_available,
            "unit": manager_install.INSTALL_UNIT,
            "warning": "The agent and web service restart while the package is unpacked. "
            "The install is judged by "
            + manager_verify.VERIFY_TIMER
            + ", which puts the previous package back if no healthy result appears in time.",
        }

    def _advance(self, operation, stage, *, detail=None):
        self.operations.advance(operation.operation_id, stage, detail=detail)
        if self._operation_log is not None:
            self._operation_log.record(
                operation.operation_id, stage, operation_type=operation.type, detail=detail
            )
        return stage


__all__ = [
    "DIRECTION_DOWNGRADE",
    "DIRECTION_REINSTALL",
    "DIRECTION_REVERT",
    "DIRECTION_UPGRADE",
    "ManagerUpdateError",
    "ManagerUpdateService",
    "STAGING_PREFIX",
    "TYPE_MANAGER_REVERT",
    "TYPE_MANAGER_UPDATE",
    "direction",
]
