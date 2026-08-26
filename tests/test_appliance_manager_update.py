# SPDX-License-Identifier: AGPL-3.0-or-later
"""The operator's button: fetch a manager package, install it, be able to undo.

The order is the security property and it is the same one the OS fetch defends:
an index may only name candidates, the detached signature decides whether the
manifest may be believed, and only a verified manifest says what the archive
must hash to.

Two things are specific to a package. Going backwards is a feature, not an
error — a revert is the only recovery this path has, so an older package must
install as readily as a newer one. And the deadline that undoes an install
nobody confirmed is armed *before* the install starts, because afterwards the
process doing the arming is the one being replaced.
"""

import hashlib
import json

import pytest

from appliance import (
    manager_install,
    manager_releases,
    manager_retention,
    manager_update,
    manager_verify,
    os_releases,
    persistent_state,
)
from appliance.agent import AgentHandlers
from appliance.os_fetch import FetchError
from tests.helpers.appliance import build_test_services

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

BASE = "https://releases.example.org"
RELEASE_ID = "ems-appliance-manager-0.2.0-arm64"
ARCHIVE_NAME = "ems-appliance-manager_0.2.0_arm64.deb"
ARCHIVE_BYTES = b"a signed appliance manager package" * 64
ARCHIVE_DIGEST = "sha256:" + hashlib.sha256(ARCHIVE_BYTES).hexdigest()


def manifest_payload(*, version="0.2.0", digest=ARCHIVE_DIGEST, size=None, state_schemas=None):
    return {
        "format_version": manager_releases.MANIFEST_FORMAT_VERSION,
        "package": manager_releases.PACKAGE_NAME,
        "version": version,
        "architecture": "arm64",
        "build_id": "20260826010000",
        "created_at": "2026-08-26T01:00:00Z",
        "project_revision": "0" * 40,
        "artifact": {
            "name": ARCHIVE_NAME,
            "digest": digest,
            "size_bytes": len(ARCHIVE_BYTES) if size is None else size,
        },
        "reproducibility": {
            "source_date_epoch": 1787000000,
            "dpkg_deb": "1.22.11",
            "compression": "xz",
        },
        "state_schemas": state_schemas or manager_releases.implemented_state_schemas(),
    }


def index_payload(*, release_id=RELEASE_ID, extra=()):
    entries = [
        {
            "release_id": release_id,
            "manifest_url": f"{BASE}/{release_id}.manifest.json",
            "signature_url": f"{BASE}/{release_id}.manifest.json.asc",
            "archive_url": f"{BASE}/{ARCHIVE_NAME}",
            "release_version": "0.2.0",
            "created_at": "2026-08-26T01:00:00Z",
        }
    ]
    entries.extend(extra)
    return {"format_version": 1, "releases": entries}


class ScriptedHttps:
    """The network as a dictionary, recording what was asked for, in order."""

    def __init__(self, responses=None):
        self.responses = dict(responses or {})
        self.requested = []

    def read(self, url, *, label, max_bytes):
        self.requested.append(url)
        payload = self._body(url, label)
        if len(payload) > max_bytes:
            raise FetchError("release_download_too_large", f"{label} is too large")
        return payload

    def download(self, url, destination, *, label, expected_bytes):
        self.requested.append(url)
        payload = self._body(url, label)
        if len(payload) != expected_bytes:
            raise FetchError("release_download_truncated", f"{label} was truncated")
        destination.write_bytes(payload)
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def _body(self, url, label):
        if url not in self.responses:
            raise FetchError("release_download_failed", f"{label} is unreachable")
        body = self.responses[url]
        return body if isinstance(body, bytes) else json.dumps(body).encode()


class ScriptedVerifier:
    """gpgv reduced to its verdict, so the ordering can be observed."""

    def __init__(self, *, valid=True):
        self.valid = valid
        self.calls = []

    def verify(self, manifest_path, signature_path):
        self.calls.append(str(manifest_path))
        if not self.valid:
            raise os_releases.ReleaseError(
                "release_signature_invalid", "the signature could not be verified"
            )
        return True


class Clock:
    def __init__(self, synchronised=True):
        self.synchronised = synchronised

    def system_time(self):
        return {"epoch": 1787000000.0, "ntp_synchronized": self.synchronised}


def build(
    tmp_path,
    *,
    responses=None,
    valid_signature=True,
    synchronised=True,
    index=None,
    installed_version="0.1.0",
    configured=True,
):
    services = build_test_services(tmp_path)
    config = services.config.__class__(
        **{
            **services.config.__dict__,
            "manager_index_url": f"{BASE}/manager-index.json" if configured else "",
        }
    )
    scripted = ScriptedHttps(
        {
            f"{BASE}/manager-index.json": index_payload() if index is None else index,
            f"{BASE}/{RELEASE_ID}.manifest.json": manifest_payload(),
            f"{BASE}/{RELEASE_ID}.manifest.json.asc": b"-----BEGIN PGP SIGNATURE-----\n",
            f"{BASE}/{ARCHIVE_NAME}": ARCHIVE_BYTES,
            **(responses or {}),
        }
    )
    reverter = tmp_path / "usr" / "lib" / "ems-appliance-manager" / "verify-manager.sh"
    reverter.parent.mkdir(parents=True, exist_ok=True)
    reverter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    service = manager_update.ManagerUpdateService(
        paths=services.paths,
        config=config,
        verifier=ScriptedVerifier(valid=valid_signature),
        probe=Clock(synchronised=synchronised),
        operations=services.operations,
        runner=services.runner,
        fetcher=scripted,
        time_fn=services.clock,
        installed_version=installed_version,
        state_mountpoint=services.paths.state_dir,
        reverter=str(reverter),
    )
    services.manager = service
    services.config = config
    service.scripted = scripted
    return services, service


def handlers(services):
    return AgentHandlers(services, executor=lambda target: target())


def plan(services, operation, **fields):
    return handlers(services).dispatch({"operation": operation, **fields})


def plan_and_execute(services, operation, **fields):
    planned = plan(services, operation, **fields)
    handlers(services).dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    return services.operations.get(planned["operation"]["operation_id"]), planned["plan"]


def seed_previous(services, *, version="0.1.0", body=b"the package that is running"):
    """An appliance whose running manager was itself installed once."""

    archive = services.paths.packages_dir
    archive.mkdir(parents=True, exist_ok=True)
    staged = archive / "seed.deb"
    staged.write_bytes(body)
    manager_retention.retain(
        services.paths,
        staged,
        sha256="sha256:" + hashlib.sha256(body).hexdigest(),
        version=version,
        build_id="20260801000000",
        state_reads=persistent_state.readable_floors(),
        rotate=False,
    )
    staged.unlink()


# --- the index is a suggestion, never an authority ---------------------------


def test_the_index_says_what_is_on_offer_and_what_is_already_running(tmp_path):
    _, service = build(tmp_path)

    listing = service.sources()

    assert listing["configured"]
    assert not listing["error"]
    assert [entry["release_id"] for entry in listing["releases"]] == [RELEASE_ID]
    assert listing["installed_version"] == "0.1.0"


def test_an_unconfigured_index_is_reported_rather_than_raised(tmp_path):
    _, service = build(tmp_path, configured=False)

    listing = service.sources()

    assert not listing["configured"]
    assert listing["releases"] == []


def test_an_unreachable_index_names_the_failure_and_offers_nothing(tmp_path):
    _, service = build(tmp_path, responses={f"{BASE}/manager-index.json": None})
    service.scripted.responses.pop(f"{BASE}/manager-index.json")

    listing = service.sources()

    assert listing["error"] == "release_download_failed"
    assert listing["releases"] == []


# --- planning ----------------------------------------------------------------


def test_a_plan_says_which_direction_the_install_goes(tmp_path):
    services, _ = build(tmp_path, installed_version="0.1.0")

    planned = plan(services, "manager.plan_update", release_id=RELEASE_ID)

    assert planned["plan"]["direction"] == manager_update.DIRECTION_UPGRADE
    assert planned["plan"]["version"] == "0.2.0"


def test_an_older_package_is_planned_rather_than_refused(tmp_path):
    """The revert is the only recovery this path has; refusing it removes it."""

    services, _ = build(tmp_path, installed_version="0.9.0")

    planned = plan(services, "manager.plan_update", release_id=RELEASE_ID)

    assert planned["plan"]["direction"] == manager_update.DIRECTION_DOWNGRADE
    assert planned["plan"]["blockers"] == []


def test_a_plan_writes_nothing(tmp_path):
    services, _ = build(tmp_path)

    plan(services, "manager.plan_update", release_id=RELEASE_ID)

    assert not manager_install.request_path(services.paths).exists()
    assert not manager_verify.deadline_path(services.paths).exists()
    assert not manager_retention.read(services.paths).current.present


def test_an_appliance_with_nothing_to_go_back_to_says_so_in_the_plan(tmp_path):
    services, _ = build(tmp_path)

    planned = plan(services, "manager.plan_update", release_id=RELEASE_ID)

    assert planned["plan"]["revert_available"] is False
    assert "no way back" in planned["plan"]["warning"]


def test_an_appliance_that_kept_its_running_package_can_offer_a_way_back(tmp_path):
    services, _ = build(tmp_path)
    seed_previous(services)

    planned = plan(services, "manager.plan_update", release_id=RELEASE_ID)

    assert planned["plan"]["revert_available"] is True


def test_a_clock_that_is_not_synchronised_blocks_the_plan(tmp_path):
    services, _ = build(tmp_path, synchronised=False)

    with pytest.raises(Exception) as refusal:
        plan(services, "manager.plan_update", release_id=RELEASE_ID)

    assert getattr(refusal.value, "code", "") == "clock_not_synchronised"


def test_a_package_that_could_not_read_this_appliances_state_is_blocked(tmp_path):
    """The offered package's manager implements less than this appliance wrote."""

    services, _ = build(tmp_path)
    persistent_state.write_stamp(
        services.paths.state_dir,
        schemas={name: value + 1 for name, value in persistent_state.implemented_schemas().items()},
    )

    planned = plan(services, "manager.plan_update", release_id=RELEASE_ID)

    codes = [entry["code"] for entry in planned["plan"]["blockers"]]
    assert "artifact_state_schema_too_old" in codes


def test_a_package_that_reads_only_newer_state_than_this_appliance_has_is_blocked(tmp_path):
    ahead = manager_releases.implemented_state_schemas()
    ahead["reads"] = {name: value + 1 for name, value in ahead["reads"].items()}
    ahead["implements"] = {name: value + 1 for name, value in ahead["implements"].items()}
    services, _ = build(
        tmp_path,
        responses={
            f"{BASE}/{RELEASE_ID}.manifest.json": manifest_payload(state_schemas=ahead)
        },
    )

    planned = plan(services, "manager.plan_update", release_id=RELEASE_ID)

    codes = [entry["code"] for entry in planned["plan"]["blockers"]]
    assert "artifact_state_schema_unreadable" in codes


def test_planning_does_not_claim_the_state_record(tmp_path):
    services, _ = build(tmp_path)

    plan(services, "manager.plan_update", release_id=RELEASE_ID)

    assert not persistent_state.stamp_path(services.paths.state_dir).exists()


# --- execution ---------------------------------------------------------------


def test_the_signature_is_verified_before_the_archive_is_asked_for(tmp_path):
    services, service = build(tmp_path)

    plan_and_execute(services, "manager.plan_update", release_id=RELEASE_ID)

    assert service.scripted.requested.index(f"{BASE}/{ARCHIVE_NAME}") > service.scripted.requested.index(
        f"{BASE}/{RELEASE_ID}.manifest.json.asc"
    )


def test_an_unsigned_manifest_stops_before_the_archive_is_asked_for(tmp_path):
    """The plan itself needs a verified manifest: it has nothing else to report."""

    services, service = build(tmp_path, valid_signature=False)

    with pytest.raises(Exception) as refusal:
        plan(services, "manager.plan_update", release_id=RELEASE_ID)

    assert getattr(refusal.value, "code", "") == "release_signature_invalid"
    assert f"{BASE}/{ARCHIVE_NAME}" not in service.scripted.requested
    assert not manager_install.request_path(services.paths).exists()


def test_an_archive_that_is_not_what_the_manifest_named_is_never_staged(tmp_path):
    services, _ = build(tmp_path, responses={f"{BASE}/{ARCHIVE_NAME}": b"something else" * 100})

    record, _ = plan_and_execute(services, "manager.plan_update", release_id=RELEASE_ID)

    assert record.state == "failed_terminal"
    assert not manager_install.request_path(services.paths).exists()


def test_a_successful_execution_stages_the_package_and_starts_the_unit(tmp_path):
    services, _ = build(tmp_path)

    record, _ = plan_and_execute(services, "manager.plan_update", release_id=RELEASE_ID)

    request = json.loads(
        manager_install.request_path(services.paths).read_text(encoding="utf-8")
    )
    assert record.state == "succeeded"
    assert request["version"] == "0.2.0"
    assert manager_retention.read(services.paths).current.present
    started = [call for call in services.host.calls if call[0] == "systemctl"]
    assert any(manager_install.INSTALL_UNIT in " ".join(call[1]) for call in started)


def test_the_deadline_is_armed_before_the_install_is_started(tmp_path):
    services, _ = build(tmp_path)

    plan_and_execute(services, "manager.plan_update", release_id=RELEASE_ID)

    calls = [" ".join(call[1]) for call in services.host.calls if call[0] == "systemctl"]
    armed = [index for index, call in enumerate(calls) if manager_verify.VERIFY_TIMER in call]
    started = [index for index, call in enumerate(calls) if manager_install.INSTALL_UNIT in call]
    assert armed and started
    assert armed[0] < started[0], "afterwards the arming process is the one being replaced"


def test_the_deadline_names_the_version_the_install_has_to_produce(tmp_path):
    services, _ = build(tmp_path)

    plan_and_execute(services, "manager.plan_update", release_id=RELEASE_ID)

    deadline = manager_verify.read(services.paths)
    assert deadline.armed
    assert deadline.expected_version == "0.2.0"


def test_executing_claims_the_state_record(tmp_path):
    services, _ = build(tmp_path)

    plan_and_execute(services, "manager.plan_update", release_id=RELEASE_ID)

    stamp = persistent_state.read_stamp(services.paths.state_dir)
    assert stamp.present
    assert stamp.schemas == persistent_state.implemented_schemas()


def test_the_downloaded_archive_leaves_no_staging_directory_behind(tmp_path):
    services, _ = build(tmp_path)

    plan_and_execute(services, "manager.plan_update", release_id=RELEASE_ID)

    leftovers = [
        entry.name
        for entry in services.paths.packages_dir.iterdir()
        if entry.name.startswith(manager_update.STAGING_PREFIX)
    ]
    assert leftovers == []


# --- going back --------------------------------------------------------------


def test_a_revert_is_refused_when_nothing_was_kept(tmp_path):
    services, _ = build(tmp_path)

    with pytest.raises(Exception) as refusal:
        plan(services, "manager.plan_revert")

    assert getattr(refusal.value, "code", "") == "no_previous_package"


def test_a_revert_names_the_package_it_would_put_back(tmp_path):
    services, _ = build(tmp_path)
    seed_previous(services)
    plan_and_execute(services, "manager.plan_update", release_id=RELEASE_ID)

    planned = plan(services, "manager.plan_revert")

    assert planned["plan"]["version"] == "0.1.0"
    assert planned["plan"]["direction"] == manager_update.DIRECTION_REVERT


def test_a_revert_installs_the_kept_archive_and_swaps_what_is_kept(tmp_path):
    services, _ = build(tmp_path)
    seed_previous(services)
    plan_and_execute(services, "manager.plan_update", release_id=RELEASE_ID)

    record, _ = plan_and_execute(services, "manager.plan_revert")

    kept = manager_retention.read(services.paths)
    request = json.loads(
        manager_install.request_path(services.paths).read_text(encoding="utf-8")
    )
    assert record.state == "succeeded"
    assert kept.current.version == "0.1.0", "the package going back on is now the current one"
    assert kept.previous.version == "0.2.0", "and the one being left is what a revert would undo"
    assert request["version"] == "0.1.0"


def test_a_revert_that_no_longer_hashes_to_what_was_kept_is_refused(tmp_path):
    services, _ = build(tmp_path)
    seed_previous(services)
    plan_and_execute(services, "manager.plan_update", release_id=RELEASE_ID)
    (services.paths.packages_dir / manager_retention.PREVIOUS_NAME).write_bytes(b"tampered")

    record, _ = plan_and_execute(services, "manager.plan_revert")

    assert record.state == "failed_terminal"
    assert record.error["code"] == "manager_artifact_corrupt"


def test_a_revert_arms_its_own_deadline(tmp_path):
    services, _ = build(tmp_path)
    seed_previous(services)
    plan_and_execute(services, "manager.plan_update", release_id=RELEASE_ID)

    plan_and_execute(services, "manager.plan_revert")

    assert manager_verify.read(services.paths).expected_version == "0.1.0"


# --- what the appliance reports ---------------------------------------------


def test_the_status_reports_what_is_installed_kept_and_pending(tmp_path):
    services, service = build(tmp_path)
    seed_previous(services)

    status = service.status()

    assert status["installed_version"] == "0.1.0"
    assert status["retention"]["current"]["version"] == "0.1.0"
    assert status["verify"]["armed"] is False
    assert status["outcome"]["outcome"] == manager_install.OUTCOME_PENDING


def test_the_status_surfaces_a_reverting_verdict(tmp_path):
    services, service = build(tmp_path)
    manager_verify.verdict_path(services.paths).parent.mkdir(parents=True, exist_ok=True)
    manager_verify.verdict_path(services.paths).write_text(
        json.dumps({"verdict": manager_verify.VERDICT_REVERTED, "detail": "the deadline expired"}),
        encoding="utf-8",
    )

    status = service.status()

    assert status["verdict"]["verdict"] == manager_verify.VERDICT_REVERTED
    assert status["verdict"]["settled"] is True
