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
        self.save_fails = False

    def daemon_running(self):
        return self.running

    def inspect_image(self, reference):
        return SimpleNamespace(reference=reference, exists=reference in self.present, digest="")

    def inspect_container(self, name):
        return self.containers.get(name, SimpleNamespace(name=name, image=""))

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

    def compose_up_service(self, service):
        self.started.append(service)


def store(tmp_path):
    return RuntimeRecordStore(tmp_path / "os-update", time_fn=lambda: 1000.0)


def service(tmp_path, docker, **kwargs):
    kwargs.setdefault("compose_file", tmp_path / "docker-compose.yml")
    return SlotBootstrapService(docker=docker, store=store(tmp_path), **kwargs)


def recorded(tmp_path, images=((ROLE_ADMIN, ADMIN, True), (ROLE_EMS, EMS, False))):
    return store(tmp_path).write(
        [RuntimeImage(role=role, reference=reference, required=required)
         for role, reference, required in images]
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


def test_the_running_admin_is_recorded_from_the_known_good_digest(tmp_path):
    docker = FakeDocker()
    known_good = SimpleNamespace(current=lambda: {"admin_reference": ADMIN})

    record = service(tmp_path, docker, known_good=known_good).record_running_runtime()

    assert record.image(ROLE_ADMIN).reference == ADMIN


def test_a_container_running_a_tag_is_resolved_to_its_digest(tmp_path):
    docker = FakeDocker(containers={"ems-admin": SimpleNamespace(image="ems-admin:1.2.3")})
    docker.inspect_image = lambda reference: SimpleNamespace(
        exists=True, digest="sha256:" + "c" * 64
    )

    record = service(tmp_path, docker).record_running_runtime()

    assert record.image(ROLE_ADMIN).reference == "ems-admin@sha256:" + "c" * 64


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


def test_the_admin_container_is_started_after_its_image_is_available(tmp_path):
    recorded(tmp_path)
    docker = FakeDocker(present={ADMIN, EMS})

    report = service(tmp_path, docker).reconstruct()

    assert docker.started == ["admin"]
    assert report.started == ("admin",)


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
