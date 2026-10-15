# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which Admin runs here, and the known-good record, are read from what runs.

Owner decision 2026-10-13 (A-M13b): the running containers' image labels are
the authority (agent-rules §2: Docker runtime inspection owns running
identity). The Admin self-update reads the running Admin by its immutable image
id, never by the image string ``docker ps`` reports, and the System Build
known-good record follows the pair that runs once it is one build.
"""

import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from admin.admin_update import (
    ADMIN_IMAGE_REPO,
    EMS_IMAGE_REPO,
    TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
    TRANSITION_STAGE_HEALTHCHECK_PENDING,
    AdminUpdateService,
    PendingTransitionStore,
    detect_current_admin_identity,
    make_transition_record,
)
from admin.image_identity import (
    LABEL_BUILD_ID,
    LABEL_CHANNEL,
    LABEL_RELEASE_TAG,
    LABEL_REVISION,
    LABEL_VERSION,
    ImageIdentity,
)
from admin.known_good import KnownGoodStore
from admin.running_pair_health import VERDICT_SECONDS, RunningPairHealthCheck
from admin.system_alignment import SystemAlignmentService
from admin.system_build import SystemBuildResolver, running_system_build

pytestmark = [
    pytest.mark.admin,
    pytest.mark.system_build,
    pytest.mark.contract,
]

T0 = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
OLD_REVISION = "a" * 40
NEW_REVISION = "b" * 40
ADMIN_CONTAINER = "ems-solarflow-admin"
RUNNING_IMAGE_ID = "sha256:" + "1" * 64
RUNNING_DIGEST = "sha256:" + "2" * 64
LATEST_DIGEST = "sha256:" + "3" * 64


def _labels(tag, revision):
    return {
        LABEL_VERSION: tag,
        LABEL_REVISION: revision,
        LABEL_BUILD_ID: f"{tag}-{revision[:7]}",
        LABEL_CHANNEL: "stable",
        LABEL_RELEASE_TAG: tag,
    }


def _image(ref, digest, tag, revision):
    return {"image_ref": ref, "digest": digest, "labels": _labels(tag, revision)}


class _AdminDocker:
    """A digest-pinned Admin as Docker 26 reports it.

    ``docker ps`` names the container's image by ``shown``: the bare repository
    (which Docker resolves to a local ``:latest`` of another build) or a short
    image id. The container itself still knows its immutable image id and the
    digest reference it was created from.
    """

    def __init__(self, shown):
        self.shown = shown
        self.declared = f"{ADMIN_IMAGE_REPO}@{RUNNING_DIGEST}"
        self.images = {
            RUNNING_IMAGE_ID: _image(RUNNING_IMAGE_ID, RUNNING_DIGEST, "v0.8.14", NEW_REVISION),
            self.declared: _image(self.declared, RUNNING_DIGEST, "v0.8.14", NEW_REVISION),
            f"{ADMIN_IMAGE_REPO}:v0.8.14": _image(
                f"{ADMIN_IMAGE_REPO}:v0.8.14", RUNNING_DIGEST, "v0.8.14", NEW_REVISION
            ),
            ADMIN_IMAGE_REPO: _image(ADMIN_IMAGE_REPO, LATEST_DIGEST, "v0.8.9", OLD_REVISION),
            f"{ADMIN_IMAGE_REPO}:latest": _image(
                f"{ADMIN_IMAGE_REPO}:latest", LATEST_DIGEST, "v0.8.9", OLD_REVISION
            ),
        }

    def probe(self):
        return {"state": "ready"}

    def inspect_container(self, name):
        if name != ADMIN_CONTAINER:
            return None
        return {"container_name": name, "status": "running", "image": self.shown}

    def inspect_container_image_id(self, name):
        return RUNNING_IMAGE_ID if name == ADMIN_CONTAINER else None

    def inspect_container_image_ref(self, name):
        return self.declared if name == ADMIN_CONTAINER else None

    def inspect_image(self, ref):
        return self.images.get(ref)


ENVIRON = {"EMS_ADMIN_CONTAINER_NAME": ADMIN_CONTAINER}


@pytest.mark.parametrize("shown", (ADMIN_IMAGE_REPO, "3f2a1b9c0d4e"))
def test_the_admin_update_reads_the_running_admin_by_its_image_id(shown, tmp_path):
    """Field report 2026-10-03: the status named the Admin by ``docker ps``.

    A digest-pinned Admin shows as its bare repository on Docker 26, which
    resolves to a local ``:latest`` of another build, or as a short image id.
    Its identity is read from the image the container runs, and its reference
    is the one it was created from.
    """

    docker = _AdminDocker(shown)

    identity, source = detect_current_admin_identity(docker=docker, environ=ENVIRON)

    assert source == "docker_inspect"
    assert identity.digest == RUNNING_DIGEST
    assert identity.version_label == "v0.8.14"
    assert identity.image_ref == docker.declared

    service = AdminUpdateService(docker=docker, environ=ENVIRON, state_dir=tmp_path)
    current = service.status()["current_admin"]
    assert current["image_ref"] == docker.declared
    assert current["digest"] == RUNNING_DIGEST
    assert current["release"] == "v0.8.14"
    assert service.plan("v0.8.14")["update_required"] is False


def test_a_running_admin_that_cannot_be_named_is_not_named_from_its_environment():
    """The environment names what the container was created from, not what runs."""

    class Unnamed(_AdminDocker):
        def inspect_container_image_id(self, name):
            return None

        def inspect_container_image_ref(self, name):
            return ADMIN_IMAGE_REPO

    environ = {
        **ENVIRON,
        "EMS_ADMIN_IMAGE": ADMIN_IMAGE_REPO,
        "EMS_ADMIN_TAG": "v0.8.14",
    }

    identity, source = detect_current_admin_identity(
        docker=Unnamed(ADMIN_IMAGE_REPO), environ=environ
    )

    assert source == "unknown"
    assert identity.digest is None


def test_the_environment_answers_when_no_admin_container_runs():
    class NoAdmin(_AdminDocker):
        def inspect_container(self, name):
            return None

    environ = {"EMS_ADMIN_IMAGE": ADMIN_IMAGE_REPO, "EMS_ADMIN_TAG": "v0.8.14"}

    identity, source = detect_current_admin_identity(
        docker=NoAdmin(ADMIN_IMAGE_REPO), environ=environ
    )

    assert source == "env"
    assert identity.digest == RUNNING_DIGEST


def _identity(role, tag, revision, digest):
    repo = ADMIN_IMAGE_REPO if role == "admin" else EMS_IMAGE_REPO
    return ImageIdentity(
        image_ref=f"{repo}@{digest}",
        digest=digest,
        version_label=tag,
        revision=revision,
        channel="stable",
        build_id=f"{tag}-{revision[:7]}",
        release_tag=tag,
    )


OLD_ADMIN = _identity("admin", "v0.8.13", OLD_REVISION, "sha256:" + "4" * 64)
OLD_EMS = _identity("ems", "v0.8.13", OLD_REVISION, "sha256:" + "5" * 64)
NEW_ADMIN = _identity("admin", "v0.8.14", NEW_REVISION, "sha256:" + "6" * 64)
NEW_EMS = _identity("ems", "v0.8.14", NEW_REVISION, "sha256:" + "7" * 64)


def _record_pair(store, admin, ems):
    store.record(running_system_build(admin, ems))


def _service(tmp_path, *, admin, ems, healthy=True, known_good=None, health=True):
    known_good = known_good or KnownGoodStore(tmp_path / "state")
    transitions = PendingTransitionStore(tmp_path / "state")
    service = SystemAlignmentService(
        resolver=SimpleNamespace(resolve=None),
        transition_store=transitions,
        embedded_resources=SimpleNamespace(),
        known_good_store=known_good,
        current_identity=lambda: admin,
        current_ems_identity=lambda: ems,
        persistent_ref=lambda: admin.image_ref,
        launcher=lambda record: None,
        running_pair_healthy=lambda: healthy,
        system_health_verdict=lambda pair, on_pass=None: health,
        now=lambda: T0,
    )
    return service, transitions, known_good


def test_a_running_pair_of_one_build_replaces_the_record_it_moved_away_from(tmp_path):
    """Owner decision 2026-10-13: the record follows the pair that runs.

    The record names the v0.8.13 pair; the Admin and the EMS both run v0.8.14
    (moved without the console's completion: an upgrade abandoned once its EMS
    was deployed, or both reinstalled outside the console). Once both run
    healthy and no transition is in flight, the record names v0.8.14.
    """

    service, _, known_good = _service(tmp_path, admin=NEW_ADMIN, ems=NEW_EMS)
    _record_pair(known_good, OLD_ADMIN, OLD_EMS)

    outcome = service.reconcile_known_good()

    assert outcome["state"] == "recorded"
    record = known_good.current()
    assert record["system_tag"] == "v0.8.14"
    assert record["admin_digest"] == NEW_ADMIN.digest
    assert record["ems_digest"] == NEW_EMS.digest
    assert record["build_id"] == NEW_EMS.build_id
    assert record["compatibility_mode"] == "modern_paired"
    assert outcome["known_good"] == record
    assert service.known_good_admin_running(record) is True


def test_a_missing_record_is_written_from_the_running_pair(tmp_path):
    service, _, known_good = _service(tmp_path, admin=NEW_ADMIN, ems=NEW_EMS)

    assert service.reconcile_known_good()["state"] == "recorded"
    assert known_good.current()["system_tag"] == "v0.8.14"


def test_a_running_pair_of_two_builds_is_named_and_never_recorded(tmp_path):
    """Field report 2026-10-03: an upgrade cancelled after it replaced the Admin.

    The Admin runs v0.8.14 and the EMS v0.8.13: not one System Build, so the
    record keeps the pair it last verified and the answer names both builds.
    """

    service, _, known_good = _service(tmp_path, admin=NEW_ADMIN, ems=OLD_EMS)
    _record_pair(known_good, OLD_ADMIN, OLD_EMS)
    before = known_good.path.read_bytes()

    outcome = service.reconcile_known_good()

    assert outcome["state"] == "unpaired"
    assert outcome["admin"]["release"] == "v0.8.14"
    assert outcome["ems"]["release"] == "v0.8.13"
    assert outcome["message"]
    assert known_good.path.read_bytes() == before


def test_a_record_naming_the_running_pair_is_left_as_it_is(tmp_path):
    service, _, known_good = _service(tmp_path, admin=NEW_ADMIN, ems=NEW_EMS)
    _record_pair(known_good, NEW_ADMIN, NEW_EMS)
    before = known_good.path.read_bytes()

    assert service.reconcile_known_good()["state"] == "current"
    assert known_good.path.read_bytes() == before


@pytest.mark.parametrize("healthy", (False, None))
def test_nothing_is_recorded_unless_both_containers_run_healthy(tmp_path, healthy):
    service, _, known_good = _service(
        tmp_path, admin=NEW_ADMIN, ems=NEW_EMS, healthy=healthy
    )
    _record_pair(known_good, OLD_ADMIN, OLD_EMS)
    before = known_good.path.read_bytes()

    assert service.reconcile_known_good()["state"] == "unverified"
    assert known_good.path.read_bytes() == before


def test_a_cancelled_upgrade_whose_health_check_failed_leaves_the_record(tmp_path):
    """Review 4: Cancel after a failed EMS health check recorded the failed build.

    Upgrade v0.8.13 -> v0.8.14: Admin and EMS replaced, the EMS health check
    failed, the operator cancelled. Both containers still run v0.8.14, and that
    pair still fails the check, so the record keeps v0.8.13.
    """

    service, transitions, known_good = _service(
        tmp_path, admin=NEW_ADMIN, ems=NEW_EMS, health=False
    )
    _record_pair(known_good, OLD_ADMIN, OLD_EMS)
    record = _begin(
        transitions, stage=TRANSITION_STAGE_HEALTHCHECK_PENDING, admin=NEW_ADMIN, ems=NEW_EMS
    )
    transitions.mark_failed(
        record.operation_id,
        error_code="healthcheck_failed",
        error_message="EMS health checks failed",
        resume_stage=TRANSITION_STAGE_HEALTHCHECK_PENDING,
        now=T0,
    )
    transitions.cancel(operation_id=record.operation_id)

    outcome = service.reconcile_known_good()

    assert outcome["state"] == "unverified"
    assert outcome["health"] == "failed"
    assert outcome["ems"]["release"] == "v0.8.14"
    assert known_good.current()["system_tag"] == "v0.8.13"


@pytest.mark.parametrize(
    ("verdict", "health"),
    ((lambda pair, on_pass=None: None, "checking"), (None, "unavailable")),
    ids=("checking", "no_health_check"),
)
def test_nothing_is_recorded_before_the_health_check_passed(tmp_path, verdict, health):
    service, _, known_good = _service(tmp_path, admin=NEW_ADMIN, ems=NEW_EMS)
    service._system_health_verdict = verdict

    outcome = service.reconcile_known_good()

    assert outcome["state"] == "unverified"
    assert outcome["health"] == health
    assert known_good.current() is None


def test_the_health_check_is_asked_for_the_pair_that_runs(tmp_path):
    asked = []
    service, _, _ = _service(tmp_path, admin=NEW_ADMIN, ems=NEW_EMS)
    service._system_health_verdict = lambda pair, on_pass=None: asked.append(pair) or True

    assert service.reconcile_known_good()["state"] == "recorded"
    assert asked == [(NEW_ADMIN.digest, NEW_EMS.digest)]


def test_nothing_is_recorded_from_an_identity_without_a_digest(tmp_path):
    unread = ImageIdentity(image_ref=None)
    service, _, known_good = _service(tmp_path, admin=NEW_ADMIN, ems=unread)

    assert service.reconcile_known_good()["state"] == "unverified"
    assert known_good.current() is None


def _begin(transitions, *, stage, admin, ems):
    return transitions.begin(
        make_transition_record(
            mode="guided_upgrade",
            system_tag=ems.release_tag,
            build_id=ems.build_id,
            revision=ems.revision,
            admin_image=f"{ADMIN_IMAGE_REPO}:{ems.release_tag}",
            admin_digest=admin.digest,
            ems_image=f"{EMS_IMAGE_REPO}:{ems.release_tag}",
            ems_digest=ems.digest,
            stage=stage,
            now=T0,
        )
    )


def test_nothing_is_recorded_while_a_transition_is_in_flight(tmp_path):
    """A replacement in flight proves nothing about the pair that runs."""

    service, transitions, known_good = _service(tmp_path, admin=NEW_ADMIN, ems=NEW_EMS)
    _record_pair(known_good, OLD_ADMIN, OLD_EMS)
    before = known_good.path.read_bytes()
    _begin(
        transitions,
        stage=TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
        admin=NEW_ADMIN,
        ems=NEW_EMS,
    )

    assert service.reconcile_known_good()["state"] == "transition_active"
    assert known_good.path.read_bytes() == before


def test_return_to_running_build_still_reads_the_record_a_failed_upgrade_left(tmp_path):
    """A recoverable failure keeps its way back: the record is not moved under it.

    The upgrade to v0.8.14 replaced the Admin and failed before its EMS was
    deployed. The EMS still runs the recorded v0.8.13, so Return to running
    build is offered against that record, and reconciling while the failure is
    open leaves the record alone.
    """

    service, transitions, known_good = _service(tmp_path, admin=NEW_ADMIN, ems=OLD_EMS)
    _record_pair(known_good, OLD_ADMIN, OLD_EMS)
    before = known_good.path.read_bytes()
    record = _begin(
        transitions,
        stage=TRANSITION_STAGE_HEALTHCHECK_PENDING,
        admin=NEW_ADMIN,
        ems=NEW_EMS,
    )
    transitions.mark_failed(
        record.operation_id,
        error_code="healthcheck_failed",
        error_message="EMS did not become healthy",
        resume_stage=TRANSITION_STAGE_HEALTHCHECK_PENDING,
        now=T0,
    )

    assert service.reconcile_known_good()["state"] == "transition_active"
    assert known_good.path.read_bytes() == before
    assert service.status()["transition"]["return_available"] is True


class _RegistryDocker:
    """A registry serving ``admin`` and ``ems`` under the Admin's tag."""

    def __init__(self, admin, ems):
        self._images = {
            f"{ADMIN_IMAGE_REPO}:{admin.version_label}": admin,
            f"{EMS_IMAGE_REPO}:{admin.version_label}": ems,
        }

    def pull(self, ref, on_progress=None):
        return None

    def inspect_image(self, ref):
        identity = self._images.get(ref)
        if identity is None:
            return None
        return {"image_ref": ref, "digest": identity.digest, "labels": _labels(
            identity.version_label, identity.revision
        )}


@pytest.mark.parametrize(
    "admin, ems",
    ((NEW_ADMIN, NEW_EMS), (NEW_ADMIN, OLD_EMS), (OLD_ADMIN, NEW_EMS)),
)
def test_a_running_pair_is_one_build_exactly_when_a_resolution_accepts_it(admin, ems):
    """One pairing rule: what a resolution refuses is never recorded either."""

    from admin.system_build import SystemBuildError

    resolver = SystemBuildResolver(docker=_RegistryDocker(admin, ems))
    try:
        resolved = resolver.resolve(admin.version_label)
    except SystemBuildError as exc:
        resolved = exc.code
    try:
        running = running_system_build(admin, ems)
    except SystemBuildError as exc:
        running = exc.code

    if isinstance(resolved, str):
        assert running == resolved
    else:
        assert running.as_dict() == resolved.as_dict()


EMS_CONTAINER = "ems-solarflow-api-control"
ADMIN_IMAGE_ID = "sha256:" + "8" * 64
EMS_IMAGE_ID = "sha256:" + "9" * 64


class _PairDocker:
    """The Admin and EMS containers as ``docker ps`` and ``docker inspect`` see them."""

    def __init__(self, *, admin, ems, admin_detail="Up 1 hour (healthy)",
                 ems_detail="Up 1 hour", ems_status="running"):
        self.containers = {
            ADMIN_CONTAINER: {"status": "running", "status_detail": admin_detail,
                              "image": ADMIN_IMAGE_REPO},
            EMS_CONTAINER: {"status": ems_status, "status_detail": ems_detail,
                            "image": EMS_IMAGE_REPO},
        }
        self.image_ids = {ADMIN_CONTAINER: ADMIN_IMAGE_ID, EMS_CONTAINER: EMS_IMAGE_ID}
        self.images = {
            ADMIN_IMAGE_ID: _image(ADMIN_IMAGE_ID, admin.digest, admin.version_label,
                                   admin.revision),
            EMS_IMAGE_ID: _image(EMS_IMAGE_ID, ems.digest, ems.version_label, ems.revision),
        }

    def inspect_container(self, name):
        container = self.containers.get(name)
        return dict(container, container_name=name) if container else None

    def inspect_container_image_id(self, name):
        return self.image_ids.get(name)

    def inspect_image(self, ref):
        return self.images.get(ref)

    def list_containers(self, prefix):
        return []


class _Diagnosis:
    """``EmsCliDiagnostics`` as the upgrade's health gate reads it."""

    def __init__(self, status):
        self.status = status
        self.runs = 0

    def run(self):
        self.runs += 1
        return {"available": True, "summary": {"status": self.status}}


def _join_health_checks():
    for thread in threading.enumerate():
        if thread.name == "running-pair-health":
            thread.join(timeout=10)


def _production_service(tmp_path, monkeypatch, docker, diagnosis=None):
    """The productive wiring, its health check run in the reading thread."""

    from admin import running_pair_health
    from admin.server import _build_system_alignment

    monkeypatch.delenv("EMS_CONTAINER_NAME", raising=False)
    monkeypatch.delenv("EMS_ADMIN_CONTAINER_NAME", raising=False)
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path / "install"))
    monkeypatch.setattr(running_pair_health, "_start_in_background", lambda work: work())
    service = _build_system_alignment(
        release_manager=SimpleNamespace(development_build=lambda tag: None),
        admin_data_dir=tmp_path / "admin",
        docker=docker,
        ems_cli=diagnosis or _Diagnosis("ok"),
    )
    known_good = KnownGoodStore(tmp_path / "admin" / "state")
    _record_pair(known_good, OLD_ADMIN, OLD_EMS)
    return service, known_good


def test_the_admin_records_the_pair_its_containers_run(tmp_path, monkeypatch):
    """The productive wiring: Docker names the running pair, by image id, and
    its EMS passes the health check an upgrade requires."""

    docker = _PairDocker(admin=NEW_ADMIN, ems=NEW_EMS)
    diagnosis = _Diagnosis("ok")
    service, known_good = _production_service(tmp_path, monkeypatch, docker, diagnosis)

    outcome = service.reconcile_known_good()

    assert outcome["state"] == "recorded"
    assert known_good.current()["system_tag"] == "v0.8.14"
    assert known_good.current()["ems_digest"] == NEW_EMS.digest
    assert diagnosis.runs == 1


@pytest.mark.parametrize(
    "diagnosis",
    (
        _Diagnosis("failed"),
        _Diagnosis("unavailable"),
        SimpleNamespace(run=lambda: {"available": False, "summary": {"status": "ok"}}),
        SimpleNamespace(run=lambda: {}),
    ),
    ids=("failed", "unavailable", "not_available", "malformed"),
)
def test_a_pair_whose_ems_fails_the_upgrade_health_check_is_not_recorded(
    tmp_path, monkeypatch, diagnosis
):
    """The upgrade's own gate, ``validate_system_health_result``, fails closed."""

    docker = _PairDocker(admin=NEW_ADMIN, ems=NEW_EMS, ems_detail="Up 2 seconds")
    service, known_good = _production_service(tmp_path, monkeypatch, docker, diagnosis)
    before = known_good.path.read_bytes()

    outcome = service.reconcile_known_good()

    assert outcome["state"] == "unverified"
    assert outcome["health"] == "failed"
    assert known_good.path.read_bytes() == before


def test_a_record_that_names_the_running_pair_runs_no_health_check(tmp_path, monkeypatch):
    """The check costs several ``docker exec``: it runs only when a record would change."""

    docker = _PairDocker(admin=NEW_ADMIN, ems=NEW_EMS)
    diagnosis = _Diagnosis("ok")
    service, known_good = _production_service(tmp_path, monkeypatch, docker, diagnosis)
    _record_pair(known_good, NEW_ADMIN, NEW_EMS)

    assert service.reconcile_known_good()["state"] == "current"
    assert diagnosis.runs == 0


def test_a_pair_of_two_builds_runs_no_health_check(tmp_path, monkeypatch):
    docker = _PairDocker(admin=NEW_ADMIN, ems=OLD_EMS)
    diagnosis = _Diagnosis("ok")
    service, _ = _production_service(tmp_path, monkeypatch, docker, diagnosis)

    assert service.reconcile_known_good()["state"] == "unpaired"
    assert diagnosis.runs == 0


@pytest.mark.parametrize(
    "containers",
    (
        {"ems_detail": "Up 1 hour (unhealthy)"},
        {"admin_detail": "Up 5 seconds (health: starting)"},
        {"ems_status": "restarting", "ems_detail": "Restarting (1) 3 seconds ago"},
    ),
)
def test_a_pair_that_does_not_run_healthy_is_not_recorded(tmp_path, monkeypatch, containers):
    docker = _PairDocker(admin=NEW_ADMIN, ems=NEW_EMS, **containers)
    service, known_good = _production_service(tmp_path, monkeypatch, docker)
    before = known_good.path.read_bytes()

    assert service.reconcile_known_good()["state"] == "unverified"
    assert known_good.path.read_bytes() == before


def test_an_ems_that_fails_the_upgrade_health_check_is_never_recorded(tmp_path, monkeypatch):
    """Review 4: an EMS ``Up 2 seconds`` in a crash loop counted as healthy.

    The EMS image has no Docker health check, so Docker says only that the
    container runs. The upgrade to v0.8.14 failed its EMS health check and was
    cancelled; both containers still run v0.8.14. The record keeps v0.8.13,
    because that pair fails the check an upgrade requires before it records one.
    """

    from admin.releases import ReleaseManager
    from admin.server import create_admin_runtime

    monkeypatch.delenv("EMS_CONTAINER_NAME", raising=False)
    monkeypatch.delenv("EMS_ADMIN_CONTAINER_NAME", raising=False)
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path / "install"))
    docker = _PairDocker(admin=NEW_ADMIN, ems=NEW_EMS, ems_detail="Up 2 seconds")
    diagnosis = _Diagnosis("failed")
    runtime = create_admin_runtime(
        release_manager=ReleaseManager(data_dir=tmp_path / "admin", docker=docker),
        docker=docker,
        ems_cli=diagnosis,
    )
    known_good = KnownGoodStore(tmp_path / "admin" / "state")
    _record_pair(known_good, OLD_ADMIN, OLD_EMS)
    before = known_good.path.read_bytes()

    first = runtime.system_alignment.reconcile_known_good()
    _join_health_checks()
    second = runtime.system_alignment.reconcile_known_good()

    assert first["state"] == "unverified"
    assert second["state"] == "unverified"
    assert second["health"] == "failed"
    assert second["ems"]["release"] == "v0.8.14"
    assert diagnosis.runs == 1
    assert known_good.path.read_bytes() == before


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class _Deferred:
    """The background start of the health check: it ends when the test says so."""

    def __init__(self):
        self.pending = []

    def __call__(self, work):
        self.pending.append(work)

    def finish(self):
        while self.pending:
            self.pending.pop(0)()


def _checked_service(tmp_path, status="ok"):
    """A record naming v0.8.13 while v0.8.14 runs, its health check on a fake clock."""

    clock, deferred, diagnosis = _Clock(), _Deferred(), _Diagnosis(status)
    service, transitions, known_good = _service(tmp_path, admin=NEW_ADMIN, ems=NEW_EMS)
    _record_pair(known_good, OLD_ADMIN, OLD_EMS)
    service._system_health_verdict = RunningPairHealthCheck(
        diagnosis.run, clock=clock, start=deferred
    ).verdict
    return SimpleNamespace(
        service=service, transitions=transitions, known_good=known_good,
        clock=clock, deferred=deferred, diagnosis=diagnosis,
    )


def test_a_pair_whose_check_passes_is_recorded_when_the_check_ends(tmp_path):
    """Review 4 (r11): a pass waited for a read inside its two-minute window.

    Outside a transition the console does not poll, so the status is read on a
    page load or an action, minutes apart. Read at 0 s starts the check, it
    passes at 30 s, the next read comes at 300 s: the pass had expired, the read
    started the check again, and the record never followed the pair. The pair is
    recorded when its check passes, with no read in between.
    """

    s = _checked_service(tmp_path)

    first = s.service.reconcile_known_good()
    s.clock.now += 30
    s.deferred.finish()

    assert first["health"] == "checking"
    assert s.known_good.current()["system_tag"] == "v0.8.14"
    s.clock.now += 270
    assert s.service.reconcile_known_good()["state"] == "current"
    assert s.diagnosis.runs == 1


def test_a_check_longer_than_the_verdict_window_still_records_its_pair(tmp_path):
    """The EMS diagnosis may take longer than the window a verdict counts for."""

    s = _checked_service(tmp_path)

    s.service.reconcile_known_good()
    s.clock.now += VERDICT_SECONDS + 5
    s.deferred.finish()

    assert s.known_good.current()["system_tag"] == "v0.8.14"
    assert s.service.reconcile_known_good()["state"] == "current"
    assert s.diagnosis.runs == 1


MID_ADMIN = _identity("admin", "v0.8.12", "c" * 40, "sha256:" + "a" * 64)
MID_EMS = _identity("ems", "v0.8.12", "c" * 40, "sha256:" + "b" * 64)


def _pair_moved(s):
    s.service._current_identity = lambda: MID_ADMIN
    s.service._current_ems_identity = lambda: MID_EMS


def _ems_image_replaced(s):
    s.service._current_ems_identity = lambda: _identity(
        "ems", "v0.8.14", NEW_REVISION, "sha256:" + "d" * 64
    )


def _stopped_running(s):
    s.service._running_pair_healthy = lambda: False


def _transition_began(s):
    _begin(
        s.transitions,
        stage=TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
        admin=NEW_ADMIN,
        ems=NEW_EMS,
    )


def _record_rewritten(s):
    _record_pair(s.known_good, MID_ADMIN, MID_EMS)


@pytest.mark.parametrize(
    "change",
    (_pair_moved, _ems_image_replaced, _stopped_running, _transition_began,
     _record_rewritten),
    ids=("pair_moved", "ems_image_replaced", "stopped_running", "transition_began",
         "record_rewritten"),
)
def test_a_check_that_ends_after_something_moved_records_nothing(tmp_path, change):
    """Interleaving: the read starts the check, then the pair, its containers,
    a transition or the record moves, then the check passes.

    The pass is the verdict on the pair the check started for, under the record
    it read then; the write that follows it applies the read's own rules again
    and leaves the record as it finds it.
    """

    s = _checked_service(tmp_path)

    s.service.reconcile_known_good()
    change(s)
    before = s.known_good.path.read_bytes()
    s.deferred.finish()

    assert s.known_good.path.read_bytes() == before


def test_a_check_that_fails_records_nothing_and_answers_the_next_read(tmp_path):
    s = _checked_service(tmp_path, status="failed")
    before = s.known_good.path.read_bytes()

    s.service.reconcile_known_good()
    s.deferred.finish()

    assert s.known_good.path.read_bytes() == before
    outcome = s.service.reconcile_known_good()
    assert outcome["health"] == "failed"
    assert s.diagnosis.runs == 1
