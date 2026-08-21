# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rebuilding the container runtime a freshly written slot does not have.

``/var`` is per-slot under image-rota, so ``/var/lib/docker`` is too, and a slot
that has just been written has an empty image store. Whatever an operator
recovers the appliance through has to be back before that slot may call itself
known-good.

Two properties matter here. Identity is a digest and never a tag, because a tag
can point somewhere else by the time the other slot rebuilds from it. And an
appliance with no registry access must still be able to finish a trial, which is
what seeding the images onto the shared partition before the reboot buys.
"""

import hashlib
import json
from types import SimpleNamespace

import pytest

from appliance import ab_bootstrap
from appliance.ab_bootstrap import (
    ROLE_ADMIN,
    ROLE_EMS,
    SOURCE_PRESENT,
    SOURCE_REGISTRY,
    SOURCE_SEED,
    SOURCE_UNAVAILABLE,
    BootstrapError,
    RuntimeImage,
    RuntimeRecordStore,
    SlotBootstrapService,
)

pytestmark = [pytest.mark.unit, pytest.mark.simulation]

ADMIN = "ghcr.io/example/ems-admin@sha256:" + "a" * 64
EMS = "ghcr.io/example/ems-solarflow@sha256:" + "b" * 64


class FakeDocker:
    """A Docker daemon that can be offline, empty, or hold exactly some images."""

    def __init__(self, *, running=True, present=(), containers=None, pullable=()):
        self.running = running
        self.present = set(present)
        self.containers = dict(containers or {})
        self.pullable = set(pullable)
        self.pulls = []
        self.saved = []
        self.loaded = []
        self.started = []
        self.overrides = []
        self.save_fails = False

    def daemon_running(self):
        return self.running

    def image_id_of(self, reference):
        """A stable stand-in for the image config digest docker load restores."""

        blob = hashlib.sha256(str(reference).encode("utf-8")).hexdigest()
        return f"sha256:{blob}"

    def inspect_image(self, reference):
        present = reference in self.present or reference in {
            self.image_id_of(item) for item in self.present
        }
        return SimpleNamespace(
            reference=reference,
            exists=present,
            digest=str(reference).partition("@")[2],
            image_id=self.image_id_of(reference) if reference in self.present else reference,
            architecture="arm64",
            os="linux",
        )

    def inspect_container(self, name, *, strict=False):
        return self.containers.get(
            name, SimpleNamespace(name=name, image="", exists=False, state="missing")
        )

    def pull_image(self, reference):
        self.pulls.append(reference)
        if reference not in self.pullable:
            raise RuntimeError(f"cannot pull {reference}")
        self.present.add(reference)

    def save_image(self, reference, path):
        if self.save_fails:
            raise RuntimeError("no space")
        self.saved.append((reference, str(path)))
        with open(path, "wb") as handle:
            handle.write(reference.encode("utf-8"))

    def load_image(self, path):
        with open(path, "rb") as handle:
            reference = handle.read().decode("utf-8")
        self.loaded.append(str(path))
        self.present.add(reference)

    def compose_up_service(self, service, *, overrides=()):
        self.started.append(service)
        self.overrides.append(tuple(str(item) for item in overrides))


def store(tmp_path):
    return RuntimeRecordStore(tmp_path / "os-update", time_fn=lambda: 1000.0)


def service(tmp_path, docker, **kwargs):
    kwargs.setdefault("compose_file", tmp_path / "docker-compose.yml")
    return SlotBootstrapService(docker=docker, store=store(tmp_path), **kwargs)


def recorded(tmp_path, images=((ROLE_ADMIN, ADMIN, True), (ROLE_EMS, EMS, False))):
    return store(tmp_path).write(
        [
            RuntimeImage(
                role=role,
                reference=reference,
                required=required,
                state=ab_bootstrap.STATE_RUNNING,
                digest=reference.partition("@")[2],
                platform={"os": "linux", "architecture": "arm64"},
            )
            for role, reference, required in images
        ]
    )


# --- what is recorded ---------------------------------------------------------


def test_a_mutable_tag_is_never_recorded_as_an_identity(tmp_path):
    with pytest.raises(BootstrapError) as caught:
        store(tmp_path).write([RuntimeImage(role=ROLE_ADMIN, reference="ems-admin:latest")])

    assert caught.value.code == "runtime_reference_not_pinned"


def test_the_record_round_trips(tmp_path):
    recorded(tmp_path)

    record = store(tmp_path).read()

    assert record.image(ROLE_ADMIN).reference == ADMIN
    assert record.image(ROLE_ADMIN).required is True
    assert record.image(ROLE_EMS).required is False


def test_a_record_of_another_version_is_ignored_rather_than_misread(tmp_path):
    target = store(tmp_path)
    target.directory.mkdir(parents=True, exist_ok=True)
    target.path.write_text(json.dumps({"version": 99, "images": []}), encoding="utf-8")

    assert store(tmp_path).read().images == ()


def container(image, *, name="ems-solarflow-admin", state="running"):
    return SimpleNamespace(name=name, image=image, exists=True, state=state)


def resolving(docker, digest):
    docker.inspect_image = lambda reference: SimpleNamespace(
        exists=True,
        digest=str(reference).partition("@")[2] or digest,
        architecture="arm64",
        os="linux",
    )
    return docker


def test_a_known_good_admin_digest_that_matches_the_running_one_is_accepted(tmp_path):
    docker = resolving(
        FakeDocker(containers={"ems-solarflow-admin": container(ADMIN)}),
        ADMIN.partition("@")[2],
    )
    known_good = SimpleNamespace(current=lambda: {"admin_reference": ADMIN})

    record = service(tmp_path, docker, known_good=known_good).record_running_runtime()

    assert record.image(ROLE_ADMIN).reference == ADMIN


def test_a_known_good_admin_digest_that_is_not_the_running_one_blocks_planning(tmp_path):
    """Neither side may be silently preferred; the operator has to be told."""

    docker = resolving(
        FakeDocker(containers={"ems-solarflow-admin": container(ADMIN)}),
        ADMIN.partition("@")[2],
    )
    stale = "ghcr.io/example/ems-admin@sha256:" + "c" * 64
    known_good = SimpleNamespace(current=lambda: {"admin_reference": stale})

    with pytest.raises(BootstrapError) as caught:
        service(tmp_path, docker, known_good=known_good).record_running_runtime()

    assert caught.value.code == "admin_runtime_authority_drift"


def test_a_tagged_admin_is_resolved_from_the_engine_and_checked_against_known_good(
    tmp_path,
):
    docker = resolving(
        FakeDocker(containers={"ems-solarflow-admin": container("ems-admin:latest")}),
        "sha256:" + "a" * 64,
    )
    known_good = SimpleNamespace(
        current=lambda: {"admin_reference": "ems-admin@sha256:" + "a" * 64}
    )

    record = service(tmp_path, docker, known_good=known_good).record_running_runtime()

    assert record.image(ROLE_ADMIN).reference == "ems-admin@sha256:" + "a" * 64


def test_a_container_running_a_tag_is_resolved_to_its_digest(tmp_path):
    docker = FakeDocker(
        containers={"ems-solarflow-admin": container("ems-solarflow-admin:1.2.3")}
    )
    docker.inspect_image = lambda reference: SimpleNamespace(
        exists=True, digest="sha256:" + "c" * 64, architecture="arm64", os="linux"
    )

    record = service(tmp_path, docker).record_running_runtime()

    assert record.image(ROLE_ADMIN).reference == "ems-solarflow-admin@sha256:" + "c" * 64


def test_a_container_state_that_cannot_be_determined_blocks_planning(tmp_path):
    class Unreachable(FakeDocker):
        def inspect_container(self, name, *, strict=False):
            raise RuntimeError("cannot connect to the Docker daemon")

    with pytest.raises(BootstrapError) as caught:
        service(tmp_path, Unreachable()).record_running_runtime()

    assert caught.value.code == "runtime_state_unknown"


def test_nothing_resolvable_is_an_error_not_an_empty_record(tmp_path):
    with pytest.raises(BootstrapError) as caught:
        service(tmp_path, FakeDocker()).record_running_runtime()

    assert caught.value.code == "runtime_not_resolvable"


# --- seeding ------------------------------------------------------------------


def test_the_recorded_images_are_seeded_onto_the_shared_partition(tmp_path):
    record = recorded(tmp_path)
    docker = FakeDocker(present={ADMIN, EMS})

    seeded = service(tmp_path, docker).seed(record)

    assert set(seeded) == {ROLE_ADMIN, ROLE_EMS}
    assert (store(tmp_path).seed_directory / "admin.tar").is_file()


def test_a_seed_that_cannot_be_written_is_an_error(tmp_path):
    record = recorded(tmp_path)
    docker = FakeDocker(present={ADMIN, EMS})
    docker.save_fails = True

    with pytest.raises(BootstrapError) as caught:
        service(tmp_path, docker).seed(record)

    assert caught.value.code == "runtime_seed_failed"


# --- rebuilding inside the trial slot ----------------------------------------


def test_an_image_already_in_the_slot_is_not_pulled_again(tmp_path):
    recorded(tmp_path)
    docker = FakeDocker(present={ADMIN, EMS})

    report = service(tmp_path, docker).reconstruct()

    assert report.ok
    assert docker.pulls == []
    assert {outcome.source for outcome in report.outcomes} == {SOURCE_PRESENT}


def test_an_offline_slot_rebuilds_from_the_seed(tmp_path):
    """The whole point of seeding: no WAN, and the trial still finishes."""

    record = recorded(tmp_path)
    source = FakeDocker(present={ADMIN, EMS})
    service(tmp_path, source).seed(record)

    offline = FakeDocker(present=set(), pullable=set())
    report = service(tmp_path, offline).reconstruct()

    assert report.ok
    assert offline.pulls == []
    assert {outcome.source for outcome in report.outcomes} == {SOURCE_SEED}


def test_without_a_seed_the_recorded_digest_is_pulled(tmp_path):
    recorded(tmp_path)
    docker = FakeDocker(present=set(), pullable={ADMIN, EMS})

    report = service(tmp_path, docker).reconstruct()

    assert report.ok
    assert docker.pulls == [ADMIN, EMS]
    assert {outcome.source for outcome in report.outcomes} == {SOURCE_REGISTRY}


def test_a_slot_that_can_neither_load_nor_pull_the_admin_image_fails(tmp_path):
    recorded(tmp_path)
    docker = FakeDocker(present=set(), pullable=set())

    report = service(tmp_path, docker).reconstruct()

    assert not report.ok
    admin = next(item for item in report.outcomes if item.role == ROLE_ADMIN)
    assert admin.source == SOURCE_UNAVAILABLE
    assert any("admin" in problem for problem in report.problems)


def test_an_optional_image_that_cannot_be_restored_does_not_fail_the_slot(tmp_path):
    recorded(tmp_path)
    docker = FakeDocker(present={ADMIN}, pullable=set())

    report = service(tmp_path, docker).reconstruct()

    assert report.ok
    ems = next(item for item in report.outcomes if item.role == ROLE_EMS)
    assert ems.source == SOURCE_UNAVAILABLE


def test_a_slot_without_a_runtime_record_cannot_rebuild(tmp_path):
    report = service(tmp_path, FakeDocker()).reconstruct()

    assert not report.ok
    assert any("no runtime record" in problem for problem in report.problems)


def test_a_slot_without_a_docker_daemon_cannot_rebuild(tmp_path):
    recorded(tmp_path)

    report = service(tmp_path, FakeDocker(running=False)).reconstruct()

    assert not report.ok
    assert any("Docker daemon" in problem for problem in report.problems)


def test_every_recorded_running_service_is_started(tmp_path):
    recorded(tmp_path)
    docker = FakeDocker(present={ADMIN, EMS})

    report = service(tmp_path, docker).reconstruct()

    assert docker.started == ["ems-solarflow-admin", "ems"]
    assert report.started == ("ems-solarflow-admin", "ems")


def test_a_service_recorded_as_stopped_is_not_started(tmp_path):
    store(tmp_path).write(
        [
            RuntimeImage(
                role=ROLE_ADMIN,
                reference=ADMIN,
                required=True,
                state=ab_bootstrap.STATE_RUNNING,
                digest=ADMIN.partition("@")[2],
            ),
            RuntimeImage(
                role=ROLE_EMS,
                reference=EMS,
                state=ab_bootstrap.STATE_STOPPED_CLEAN,
                digest=EMS.partition("@")[2],
            ),
        ]
    )
    docker = FakeDocker(present={ADMIN, EMS})

    report = service(tmp_path, docker).reconstruct()

    assert docker.started == ["ems-solarflow-admin"]
    assert report.ok, report.problems


def test_a_service_whose_image_is_unavailable_is_never_started(tmp_path):
    recorded(tmp_path)
    docker = FakeDocker(present={ADMIN}, pullable=set())

    service(tmp_path, docker).reconstruct()

    assert docker.started == ["ems-solarflow-admin"]


def test_the_report_is_serialisable(tmp_path):
    recorded(tmp_path)
    docker = FakeDocker(present={ADMIN, EMS})

    payload = json.loads(json.dumps(service(tmp_path, docker).reconstruct().to_dict()))

    assert payload["ok"] is True
    assert {entry["role"] for entry in payload["images"]} == {ROLE_ADMIN, ROLE_EMS}


def test_the_seed_can_be_discarded(tmp_path):
    record = recorded(tmp_path)
    docker = FakeDocker(present={ADMIN, EMS})
    target = service(tmp_path, docker)
    target.seed(record)

    target.discard_seed()

    assert not store(tmp_path).seed_directory.exists()


def test_only_digest_pinned_references_are_treated_as_identities():
    assert ab_bootstrap._digest_pinned(ADMIN)
    assert not ab_bootstrap._digest_pinned("ems-admin:latest")


# --- offline reconstruction ---------------------------------------------------


def running_containers():
    return {
        "ems-solarflow-admin": SimpleNamespace(name="ems-solarflow-admin", image=ADMIN, exists=True, state="running"),
        "ems-solarflow": SimpleNamespace(
            name="ems-solarflow", image=EMS, exists=True, state="running"
        ),
    }


def source_slot(tmp_path, **kwargs):
    docker = FakeDocker(present={ADMIN, EMS}, containers=running_containers())
    return service(tmp_path, docker, **kwargs), docker


def test_the_record_captures_the_deployment_not_only_the_images(tmp_path):
    """Two slots with the same digests and different compose files differ."""

    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services:\n  admin:\n    image: admin\n", encoding="utf-8")
    (tmp_path / ".env").write_text("EMS_ADMIN_PORT=8090\n", encoding="utf-8")
    bootstrap, _docker = source_slot(tmp_path, compose_file=compose)

    record = bootstrap.record_running_runtime()

    assert record.compose_digest.startswith("sha256:")
    assert record.environment_digest.startswith("sha256:")
    assert record.compose_digest != record.environment_digest


def test_the_record_never_carries_the_environment_contents(tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    (tmp_path / ".env").write_text("EMS_ADMIN_PASSWORD=hunter2\n", encoding="utf-8")
    bootstrap, _docker = source_slot(tmp_path, compose_file=compose)

    record = bootstrap.record_running_runtime()

    assert "hunter2" not in json.dumps(record.to_dict())


def test_the_record_captures_whether_each_container_was_running(tmp_path):
    containers = running_containers()
    containers["ems-solarflow"] = SimpleNamespace(
        name="ems-solarflow", image=EMS, exists=True, state="exited"
    )
    docker = FakeDocker(present={ADMIN, EMS}, containers=containers)
    record = service(tmp_path, docker).record_running_runtime()

    assert record.image(ROLE_ADMIN).running is True
    assert record.image(ROLE_EMS).running is False


def test_a_seed_is_recorded_with_its_digest_and_size(tmp_path):
    bootstrap, _docker = source_slot(tmp_path)
    record = bootstrap.record_running_runtime()

    bootstrap.seed(record)
    stored = bootstrap.store.read()

    for entry in record.images:
        seed = stored.seed(entry.role)
        assert seed["sha256"].startswith("sha256:")
        assert seed["size_bytes"] > 0
        assert seed["reference"] == entry.reference


def test_an_offline_slot_reconstructs_from_the_seed_alone(tmp_path):
    bootstrap, _docker = source_slot(tmp_path)
    record = bootstrap.record_running_runtime()
    bootstrap.seed(record)

    # The trial slot: empty image store, and nothing pullable.
    target = FakeDocker(present=set(), pullable=set())
    report = service(tmp_path, target).reconstruct()

    assert report.ok, report.problems
    assert {outcome.source for outcome in report.outcomes} == {SOURCE_SEED}
    assert target.pulls == []


def test_a_seed_that_does_not_match_its_digest_is_never_loaded(tmp_path):
    bootstrap, _docker = source_slot(tmp_path)
    record = bootstrap.record_running_runtime()
    bootstrap.seed(record)
    seed = bootstrap.store.seed_directory / f"{ROLE_ADMIN}.tar"
    seed.write_bytes(seed.read_bytes() + b"tampered")

    target = FakeDocker(present=set(), pullable=set())
    report = service(tmp_path, target).reconstruct()

    assert str(seed) not in target.loaded
    admin = next(outcome for outcome in report.outcomes if outcome.role == ROLE_ADMIN)
    assert "does not match its recorded digest" in admin.detail
    assert not report.ok


def test_a_tampered_seed_still_falls_back_to_the_exact_digest(tmp_path):
    bootstrap, _docker = source_slot(tmp_path)
    record = bootstrap.record_running_runtime()
    bootstrap.seed(record)
    seed = bootstrap.store.seed_directory / f"{ROLE_ADMIN}.tar"
    seed.write_bytes(b"not the archive that was written")

    target = FakeDocker(present=set(), pullable={ADMIN, EMS})
    report = service(tmp_path, target).reconstruct()

    assert report.ok, report.problems
    assert ADMIN in target.pulls
    assert str(seed) not in target.loaded


def test_the_registry_fallback_names_the_exact_digest(tmp_path):
    bootstrap, _docker = source_slot(tmp_path)
    record = bootstrap.record_running_runtime()

    target = FakeDocker(present=set(), pullable={ADMIN, EMS})
    report = service(tmp_path, target).reconstruct()

    assert report.ok, report.problems
    assert target.pulls
    for reference in target.pulls:
        assert "@sha256:" in reference
    assert set(target.pulls) == {entry.reference for entry in record.images}


def test_seeding_keeps_one_generation(tmp_path):
    bootstrap, _docker = source_slot(tmp_path)
    record = bootstrap.record_running_runtime()
    bootstrap.store.seed_directory.mkdir(parents=True, exist_ok=True)
    stale = bootstrap.store.seed_directory / "influxdb.tar"
    stale.write_bytes(b"an archive from an older deployment")

    bootstrap.seed(record)

    assert not stale.exists()
    assert {path.name for path in bootstrap.store.seed_directory.iterdir()} == {
        f"{entry.role}.tar" for entry in record.images
    }


# --- the reconstruction has to fit inside the budgets around it ---------------


def test_reconstruction_is_bounded_and_reports_instead_of_being_killed(tmp_path):
    """The regression: three roles, each allowed up to 900 s of docker load and
    600 s of pull, under a unit that killed the whole thing at 900 s. A SIGKILL
    mid-restore leaves no report at all, so the trial slot fails a gate that
    never says why."""

    docker = FakeDocker()
    bootstrap = service(tmp_path, docker)
    recorded(tmp_path)

    clock = {"now": 0.0}

    def slow_restore(entry, record=None):
        clock["now"] += ab_bootstrap.DEFAULT_RECONSTRUCTION_BUDGET_SECONDS
        return ab_bootstrap.ImageOutcome(
            entry.role, entry.reference, ab_bootstrap.SOURCE_PRESENT, entry.required
        )

    bootstrap._restore = slow_restore
    bootstrap._time = lambda: clock["now"]

    report = bootstrap.reconstruct()

    assert any("budget" in problem for problem in report.problems), report.problems


def test_the_unit_outlives_the_reconstruction_budget_it_grants():
    import re
    from pathlib import Path as _Path

    units = _Path(__file__).resolve().parents[1] / "packaging" / "appliance" / "systemd"
    text = (units / "ems-appliance-slot-bootstrap.service").read_text(encoding="utf-8")
    match = re.search(r"^TimeoutStartSec=(\d+)", text, re.M)

    assert match
    assert int(match.group(1)) > ab_bootstrap.DEFAULT_RECONSTRUCTION_BUDGET_SECONDS


def test_the_budget_leaves_room_for_the_health_verdict_inside_the_window():
    """Reconstruction runs before the health check, and the health window is
    stamped from boot: a budget that fills it rolls back every update."""

    from appliance import ab_health

    assert (
        ab_bootstrap.DEFAULT_RECONSTRUCTION_BUDGET_SECONDS
        + ab_health.DEFAULT_SETTLE_SECONDS
        < ab_health.DEFAULT_HEALTH_WINDOW_SECONDS
    )
