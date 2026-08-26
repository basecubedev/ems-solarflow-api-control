# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand a verified package to a unit that survives installing it.

Everything that can refuse runs here, before dpkg does: signature, digest, state
compatibility, retention. Afterwards the interpreter's own module files are the
new ones, so a later decision would be taken by unproven code.

See docs/appliance/installation.md for the flow and
tests/test_appliance_manager_install.py for the properties it defends.
"""

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from appliance import manager_releases, manager_retention

INSTALL_UNIT = "ems-appliance-manager-install.service"

REQUEST_NAME = "install-request.json"
RESULT_NAME = "install-result.json"

OUTCOME_INSTALLED = "installed"
OUTCOME_REVERTED = "reverted"
OUTCOME_PENDING = "pending"

FILE_MODE = 0o600


class ManagerInstallError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class InstallOutcome:
    """What the unit reported, or that it has not reported yet."""

    outcome: str = OUTCOME_PENDING
    detail: str = ""
    finished_at: str = ""

    @property
    def settled(self):
        return self.outcome != OUTCOME_PENDING

    def to_dict(self):
        return {
            "outcome": self.outcome,
            "detail": self.detail,
            "finished_at": self.finished_at,
            "settled": self.settled,
        }


def request_path(paths):
    return Path(paths.packages_dir) / REQUEST_NAME


def result_path(paths):
    return Path(paths.packages_dir) / RESULT_NAME


def _write(target, payload):
    handle, staging = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(staging, FILE_MODE)
        os.replace(staging, target)
    except OSError as exc:
        try:
            os.unlink(staging)
        except OSError:
            pass
        raise ManagerInstallError("install_request_not_writable", f"{target}: {exc}")


def read_outcome(paths):
    """What the last install ended as. Absence means it has not ended."""

    try:
        payload = json.loads(result_path(paths).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return InstallOutcome()
    except (OSError, ValueError):
        return InstallOutcome(outcome=OUTCOME_PENDING, detail="the result could not be read")
    if not isinstance(payload, dict):
        return InstallOutcome(outcome=OUTCOME_PENDING, detail="the result is not an object")
    return InstallOutcome(
        outcome=str(payload.get("outcome") or OUTCOME_PENDING),
        detail=str(payload.get("detail") or ""),
        finished_at=str(payload.get("finished_at") or ""),
    )


def prepare(paths, *, release, archive, state_schemas, verifier=None, manifest_path="",
            signature_path="", architecture="arm64", retained_at=""):
    """Prove the package may be installed, keep the outgoing one, and stage it.

    Everything that can refuse happens here, before a single byte is unpacked.
    An appliance that cannot say what its state is formatted as refuses, because
    a package cannot be shown to be able to read a state nobody recorded.
    """

    if verifier is not None:
        if not manifest_path or not signature_path:
            raise ManagerInstallError(
                "manager_signature_missing",
                "a signature was required but no manifest and signature were given",
            )
        verifier.verify(manifest_path, signature_path)

    if not release.signed:
        raise ManagerInstallError(
            "manager_not_signed",
            "this package carries no verified signature and is not installable",
        )

    manager_releases.verify_artifact(release, archive)

    # `None` means undecidable, never "look it up here". Where the record comes
    # from is the caller's question -- an A/B host reads it off /persistent, a
    # package-only host from its own state directory -- and answering it here
    # would let a lookup that found nothing pass as a lookup that was not asked.
    problems = manager_releases.compatibility_problems(
        release, architecture=architecture, state_schemas=state_schemas
    )
    if problems:
        raise ManagerInstallError(problems[0]["code"], problems[0]["message"])

    # Retained before the request exists, so the archive to go back to is on
    # disk before anything can start replacing what is running.
    retention = manager_retention.retain(
        paths,
        archive,
        sha256=release.artifact_digest,
        version=release.version,
        build_id=release.build_id,
        retained_at=retained_at,
    )

    _write(
        request_path(paths),
        {
            "archive": retention.current.path,
            "version": release.version,
            "build_id": release.build_id,
            "sha256": release.artifact_digest,
            "requested_at": retained_at,
        },
    )
    try:
        result_path(paths).unlink()
    except FileNotFoundError:
        pass
    return retention


def start(runner):
    """Start the unit that owns the install, and do not wait for it.

    Waiting would mean waiting inside the process the install restarts.
    """

    if runner is None or not runner.available("systemctl"):
        raise ManagerInstallError(
            "systemctl_unavailable", "systemctl is not available, so no install can be started"
        )
    result = runner.run("systemctl", ["start", "--no-block", INSTALL_UNIT], timeout=60)
    if not result.ok:
        raise ManagerInstallError(
            "install_unit_failed",
            (result.stderr or result.stdout or f"{INSTALL_UNIT} could not be started").strip(),
        )
    return INSTALL_UNIT
