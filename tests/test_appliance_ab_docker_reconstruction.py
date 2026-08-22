# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rebuilding a slot's application against a real Docker engine.

Every reconstruction claim so far was made against a runner that answered
``docker(1)`` from a dictionary. That proves the argv the code builds and the
branch it takes; it cannot prove that ``docker save`` produces something
``docker load`` accepts, that a loaded image lands in the store under the digest
the record names, that a pull of ``repository@sha256:...`` resolves at all, or
that an image built for another architecture is refused by the engine rather
than only by this project's comparison.

So this tier builds three real contract images — an Admin that answers
``/api/admin/auth/status``, an EMS that runs ``emsctl.py diagnose --json`` and an
Influx that answers ``influx ping`` — pushes them into a registry that runs on
this host and is under this test's control, and then takes the registry away.

The scenarios are the ones an appliance actually meets on a freshly written
slot with an empty ``/var/lib/docker``:

    the seed is complete            -> loaded from the medium, no network
    the seed is corrupt             -> refused, and the fallback names a digest
    no seed at all                  -> pulled by exact digest
    the image is another platform   -> refused, nothing is committed
    EMS was deliberately stopped    -> rebuilt, and left stopped

Nothing here touches a production container name: the layout is namespaced per
test run, the registry is a container this module started, and both are removed
on the way out.
"""

import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from appliance import ab_bootstrap, commands
from appliance.docker_backend import DockerBackend, DockerError

pytestmark = [pytest.mark.integration, pytest.mark.docker, pytest.mark.system_build, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_IMAGE = "registry:2"

ADMIN_DOCKERFILE = """
FROM python:3.13-alpine
COPY server.py /server.py
ENV EMS_CONTRACT_HEALTHY=1
EXPOSE 8080
CMD ["python3", "/server.py"]
"""

ADMIN_SERVER = """
import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        healthy = os.environ.get("EMS_CONTRACT_HEALTHY") == "1"
        if self.path != "/api/admin/auth/status":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(
            {"authenticated": False, "password_configured": healthy, "contract": "admin"}
        ).encode()
        self.send_response(200 if healthy else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
"""

EMS_DOCKERFILE = """
FROM python:3.13-alpine
COPY emsctl.py /app/emsctl.py
ENV EMS_CONTRACT_HEALTHY=1
WORKDIR /app
CMD ["sh", "-c", "trap 'exit 0' TERM INT; while true; do sleep 1 & wait $!; done"]
"""

EMS_CTL = """
import json, os, sys

healthy = os.environ.get("EMS_CONTRACT_HEALTHY") == "1"
print(json.dumps({
    "schema_version": 1,
    "diagnosis": {"status": "ok" if healthy else "error", "sections": [], "metrics": {}},
}))
sys.exit(0 if healthy else 1)
"""

INFLUX_DOCKERFILE = """
FROM alpine:3.20
COPY influx /usr/local/bin/influx
RUN chmod 0755 /usr/local/bin/influx
ENV EMS_CONTRACT_HEALTHY=1
CMD ["sh", "-c", "trap 'exit 0' TERM INT; while true; do sleep 1 & wait $!; done"]
"""

INFLUX_CLI = """#!/bin/sh
[ "$1" = ping ] || { echo "unsupported: $*" >&2; exit 2; }
[ "$EMS_CONTRACT_HEALTHY" = 1 ] || { echo "not ready" >&2; exit 1; }
echo "OK"
"""


def docker_available():
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True, text=True, check=False, timeout=120,
    )
    return probe.returncode == 0


requires_docker = pytest.mark.skipif(
    not docker_available(), reason="a real Docker daemon rebuilds these slots"
)


def run(*argv, timeout=900, check=True):
    result = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise AssertionError(f"{' '.join(argv)} failed: {result.stderr.strip()[-400:]}")
    return result


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class ContractRegistry:
    """Three real images in a registry this test starts, uses and stops."""

    def __init__(self, tmp_path):
        self.tag = uuid.uuid4().hex[:10]
        self.port = free_port()
        self.container = f"ems-contract-registry-{self.tag}"
        self.host = f"127.0.0.1:{self.port}"
        self.work = tmp_path / "contract-images"
        self.work.mkdir(parents=True, exist_ok=True)
        self.references = {}

    def start(self):
        run("docker", "pull", "-q", REGISTRY_IMAGE)
        run(
            "docker", "run", "-d", "--name", self.container,
            "-p", f"127.0.0.1:{self.port}:5000", REGISTRY_IMAGE,
        )
        for _ in range(60):
            probe = subprocess.run(
                ["docker", "exec", self.container, "true"],
                capture_output=True, text=True, check=False, timeout=30,
            )
            if probe.returncode == 0 and self._reachable():
                return
            time.sleep(0.5)
        raise AssertionError("the contract registry never became reachable")

    def _reachable(self):
        with socket.socket() as probe:
            probe.settimeout(1)
            return probe.connect_ex(("127.0.0.1", self.port)) == 0

    def build(self, role, dockerfile, files, *, platform=None):
        context = self.work / role
        context.mkdir(parents=True, exist_ok=True)
        (context / "Dockerfile").write_text(dockerfile)
        for name, body in files.items():
            (context / name).write_text(body)
        repository = f"{self.host}/ems-contract-{role}"
        local = f"{repository}:{self.tag}"
        argv = ["docker", "build", "-q", "-t", local]
        if platform:
            argv += ["--platform", platform]
        run(*argv, str(context))
        run("docker", "push", "-q", local)
        digest = self._digest(local)
        reference = f"{repository}@{digest}"
        self.references[role] = reference
        return reference

    def _digest(self, local):
        result = run(
            "docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", local
        )
        return result.stdout.strip().partition("@")[2]

    def stop(self):
        subprocess.run(["docker", "stop", "-t", "2", self.container],
                       capture_output=True, check=False, timeout=120)

    def remove(self):
        subprocess.run(["docker", "rm", "-f", self.container],
                       capture_output=True, check=False, timeout=120)

    def start_again(self):
        """Every case that takes the registry away hands it back to the next one."""

        if self._reachable():
            return
        subprocess.run(["docker", "start", self.container],
                       capture_output=True, check=False, timeout=120)
        for _ in range(60):
            if self._reachable():
                return
            time.sleep(0.5)
        raise AssertionError("the contract registry could not be restarted")


class Deployment:
    """The contract services this appliance runs, under names only this test uses."""

    def __init__(self, tmp_path, registry, *, roles=ab_bootstrap.ROLES, data=None):
        self.registry = registry
        # Per deployment, not per registry: the module builds the images once
        # and several cases run their own slot against them at the same names.
        self.tag = uuid.uuid4().hex[:10]
        self.roles = tuple(roles)
        self.root = tmp_path / "deployment"
        self.root.mkdir(parents=True, exist_ok=True)
        self.data = data
        self.compose_file = self.root / "docker-compose.yml"
        self.project = f"emscontract{self.tag}"
        # Every role is named, even the ones this deployment does not run: a
        # role left out of the layout falls back to the production container
        # name, and a stray ems-influxdb on the developer's host would then be
        # read as this appliance's.
        self.containers = {
            role: f"ems-contract-{role}-{self.tag}" for role in ab_bootstrap.ROLES
        }
        self.services = {role: role for role in ab_bootstrap.ROLES}

    def write(self, references):
        services = {}
        for role in self.roles:
            services[role] = {
                "image": references[role],
                "container_name": self.containers[role],
                "environment": ["EMS_CONTRACT_HEALTHY=1"],
                "restart": "no",
            }
        services[ab_bootstrap.ROLE_ADMIN]["ports"] = [f"127.0.0.1:{free_port()}:8080"]
        if self.data is not None and ab_bootstrap.ROLE_EMS in services:
            services[ab_bootstrap.ROLE_EMS]["volumes"] = [f"{self.data}:/data"]
        self.compose_file.write_text(
            json.dumps({"services": services}, indent=2, sort_keys=True) + "\n"
        )
        (self.root / ".env").write_text(f"EMS_CONTRACT_TAG={self.tag}\n")

    def up(self):
        run("docker", "compose", "-p", self.project, "-f", str(self.compose_file), "up", "-d")

    def down(self):
        subprocess.run(
            ["docker", "compose", "-p", self.project, "-f", str(self.compose_file),
             "down", "-v", "--remove-orphans"],
            capture_output=True, check=False, timeout=300,
        )
        # The reconstruction starts services through the compose file without a
        # project name, so it owns containers this project does not: they are
        # removed by name rather than left on a shared host.
        for name in self.containers.values():
            subprocess.run(["docker", "rm", "-f", name],
                           capture_output=True, check=False, timeout=120)

    def layout(self):
        return ab_bootstrap.DeploymentLayout(
            compose_file=str(self.compose_file),
            install_root=str(self.root),
            containers=dict(self.containers),
            services=dict(self.services),
        )


@pytest.fixture(scope="module")
def registry(tmp_path_factory):
    if not docker_available():
        pytest.skip("no docker")
    work = tmp_path_factory.mktemp("contract-registry")
    contract = ContractRegistry(work)
    contract.start()
    try:
        contract.build(
            ab_bootstrap.ROLE_ADMIN, ADMIN_DOCKERFILE, {"server.py": ADMIN_SERVER}
        )
        contract.build(ab_bootstrap.ROLE_EMS, EMS_DOCKERFILE, {"emsctl.py": EMS_CTL})
        contract.build(
            ab_bootstrap.ROLE_INFLUXDB, INFLUX_DOCKERFILE, {"influx": INFLUX_CLI}
        )
        yield contract
    finally:
        contract.remove()
        for reference in contract.references.values():
            subprocess.run(["docker", "rmi", "-f", reference],
                           capture_output=True, check=False, timeout=120)


class Slot:
    """A source slot that recorded a deployment, and the target that rebuilds it."""

    def __init__(self, tmp_path, registry, *, stopped=(), roles=ab_bootstrap.ROLES, data=None):
        self.registry = registry
        self.deployment = Deployment(tmp_path, registry, roles=roles, data=data)
        self.deployment.write(
            {role: registry.references[role] for role in self.deployment.roles}
        )
        self.deployment.up()
        for role in stopped:
            run("docker", "stop", self.deployment.containers[role], timeout=120)
        self.store = ab_bootstrap.RuntimeRecordStore(tmp_path / "shared")
        self.backend = DockerBackend(
            commands.CommandRunner(), compose_file=str(self.deployment.compose_file)
        )
        self.service = ab_bootstrap.SlotBootstrapService(
            docker=self.backend,
            store=self.store,
            deployment=self.deployment.layout(),
        )

    def record_and_seed(self):
        record = self.service.record_running_runtime()
        self.service.seed(record)
        return self.store.read()

    def empty_the_image_store(self):
        """What a freshly written slot has: the record, the seed, and no images."""

        self.deployment.down()
        for reference in self.registry.references.values():
            subprocess.run(["docker", "rmi", "-f", reference],
                           capture_output=True, check=False, timeout=180)

    def teardown(self):
        self.deployment.down()


@pytest.fixture
def slot(tmp_path, registry):
    registry.start_again()
    made = Slot(tmp_path, registry)
    try:
        yield made
    finally:
        made.teardown()
        registry.start_again()


def outcomes_by_role(report):
    return {outcome.role: outcome for outcome in report.outcomes}


# --- the deployment authority, taken from a real engine ----------------------


@requires_docker
def test_a_running_deployment_records_every_service_by_digest(slot):
    record = slot.service.record_running_runtime()

    assert {entry.role for entry in record.images} == set(ab_bootstrap.ROLES)
    for entry in record.images:
        assert entry.state == ab_bootstrap.STATE_RUNNING
        assert entry.digest.startswith("sha256:")
        assert "@sha256:" in entry.reference
        assert entry.platform.get("architecture")


@requires_docker
def test_the_recorded_deployment_is_the_one_on_disk(slot):
    record = slot.service.record_running_runtime()

    assert slot.service.deployment_drift(record) == ()

    slot.deployment.compose_file.write_text(
        slot.deployment.compose_file.read_text() + "\n# edited after the plan\n"
    )
    assert slot.service.deployment_drift(record) != ()


# --- the seed is what makes a slot with no WAN able to finish ----------------


@requires_docker
def test_a_loaded_image_never_regains_the_digest_the_record_names(slot):
    """A repository digest cannot survive an archive, and this is why.

    ``docker save`` re-serialises the manifest on its way out: the archive is an
    OCI layout whose ``index.json`` names a manifest this host computed, not the
    one the registry signed. The blob whose sha256 *is* the repository digest is
    not in the archive at all, so no amount of reading it can give that name
    back, and ``docker image inspect repository@sha256:...`` says "No such
    image" after the load.

    What does survive, byte for byte, is the image config digest — Docker's
    image ID. It commits to ``rootfs.diff_ids`` and therefore to the layer
    content, which is what makes it usable as an identity rather than a label.
    So this is a fact about Docker, and the reconstruction below is built on the
    identity that does survive rather than on the one that does not.
    """

    record = slot.record_and_seed()
    entry = record.image(ab_bootstrap.ROLE_ADMIN)
    identity = slot.backend.inspect_image(entry.reference)
    slot.empty_the_image_store()

    slot.backend.load_image(slot.store.seed_directory / f"{ab_bootstrap.ROLE_ADMIN}.tar")

    assert slot.backend.inspect_image(entry.reference).exists is False
    loaded = slot.backend.inspect_image(identity.image_id or entry.digest)
    assert loaded.exists is True


@requires_docker
def test_the_record_binds_the_identity_that_survives_save_and_load(slot):
    """Docker's own binding of repository@digest to an image id, taken online.

    It is recorded while the registry is still the authority for that digest,
    which is what makes it usable later, offline, when nothing else can attest
    to it.
    """

    record = slot.record_and_seed()

    for role in ab_bootstrap.ROLES:
        entry = record.image(role)
        identity = slot.backend.inspect_image(entry.reference)
        assert identity.image_id, role
        assert entry.image_id == identity.image_id, role
        assert record.seed(role)["image_id"] == identity.image_id, role


@requires_docker
def test_a_freshly_written_slot_rebuilds_from_the_seed_with_no_registry(slot):
    record = slot.record_and_seed()
    slot.empty_the_image_store()
    slot.registry.stop()

    report = slot.service.reconstruct()

    outcomes = outcomes_by_role(report)
    assert report.ok, report.problems
    for role in ab_bootstrap.ROLES:
        assert outcomes[role].source == ab_bootstrap.SOURCE_SEED, outcomes[role].detail
        assert outcomes[role].digest == record.image(role).digest
        # What it is started from is the identity that was verified, and it is
        # content-addressed either way — never a tag.
        assert outcomes[role].runtime_reference == record.image(role).image_id
        assert outcomes[role].runtime_reference.startswith("sha256:")
    assert set(report.started) == set(ab_bootstrap.ROLES)


@requires_docker
def test_the_containers_a_seeded_slot_starts_run_the_verified_image(slot):
    record = slot.record_and_seed()
    slot.empty_the_image_store()
    slot.registry.stop()

    report = slot.service.reconstruct()

    assert report.ok, report.problems
    for role in ab_bootstrap.ROLES:
        container = slot.backend.inspect_container(slot.deployment.containers[role])
        assert container.exists, role
        assert container.image_id == record.image(role).image_id, role


@requires_docker
def test_a_seed_the_record_cannot_attribute_is_never_started(slot):
    """The seed's own metadata is not the authority; the record is.

    A seed archive that loads an image the record does not name must not be
    accepted just because it loaded cleanly.
    """

    record = slot.record_and_seed()
    stranger = "sha256:" + "b" * 64
    slot.store.write(
        [
            entry if entry.role != ab_bootstrap.ROLE_ADMIN
            else ab_bootstrap.RuntimeImage(
                role=entry.role,
                reference=entry.reference,
                required=entry.required,
                state=entry.state,
                digest=entry.digest,
                platform=entry.platform,
                image_id=stranger,
            )
            for entry in record.images
        ],
        compose=record.compose,
        environment=record.environment,
        seeds=record.seeds,
        recorded_at=record.recorded_at,
    )
    slot.empty_the_image_store()
    slot.registry.stop()

    report = slot.service.reconstruct()

    outcomes = outcomes_by_role(report)
    admin = outcomes[ab_bootstrap.ROLE_ADMIN]
    assert admin.source == ab_bootstrap.SOURCE_UNAVAILABLE, admin.detail
    assert not report.ok
    assert stranger in admin.detail or "image id" in admin.detail, admin.detail


@requires_docker
def test_every_reconstructed_service_answers_its_own_contract(slot):
    """Admin health, EMS diagnose and Influx ping, from rebuilt containers."""

    slot.service.record_running_runtime()
    slot.empty_the_image_store()

    report = slot.service.reconstruct()
    assert report.ok, report.problems

    admin = slot.backend.exec_in_container(
        slot.deployment.containers[ab_bootstrap.ROLE_ADMIN],
        ["python3", "-c",
         "import urllib.request;"
         "print(urllib.request.urlopen('http://127.0.0.1:8080/api/admin/auth/status')"
         ".read().decode())"],
        timeout=60,
    )
    assert admin.ok, admin.stderr
    assert json.loads(admin.stdout)["contract"] == "admin"

    ems = slot.backend.exec_in_container(
        slot.deployment.containers[ab_bootstrap.ROLE_EMS],
        ["python3", "/app/emsctl.py", "diagnose", "--json"],
        timeout=60,
    )
    assert ems.ok, ems.stderr
    assert json.loads(ems.stdout)["diagnosis"]["status"] == "ok"

    influx = slot.backend.exec_in_container(
        slot.deployment.containers[ab_bootstrap.ROLE_INFLUXDB], ["influx", "ping"], timeout=60
    )
    assert influx.ok, influx.stderr
    assert "OK" in influx.stdout


@requires_docker
def test_a_seed_that_does_not_match_its_record_is_never_loaded(slot):
    record = slot.record_and_seed()
    seed = slot.store.seed_directory / f"{ab_bootstrap.ROLE_ADMIN}.tar"
    payload = bytearray(seed.read_bytes())
    payload[len(payload) // 2] ^= 0xFF
    seed.write_bytes(bytes(payload))
    slot.empty_the_image_store()

    report = slot.service.reconstruct()

    admin = outcomes_by_role(report)[ab_bootstrap.ROLE_ADMIN]
    assert admin.source == ab_bootstrap.SOURCE_REGISTRY, admin.detail
    assert admin.digest == record.image(ab_bootstrap.ROLE_ADMIN).digest
    assert "sha256" in admin.detail or "does not" in admin.detail


@requires_docker
def test_a_truncated_seed_is_refused_rather_than_half_loaded(slot):
    slot.record_and_seed()
    seed = slot.store.seed_directory / f"{ab_bootstrap.ROLE_INFLUXDB}.tar"
    seed.write_bytes(seed.read_bytes()[: seed.stat().st_size // 3])
    slot.empty_the_image_store()

    report = slot.service.reconstruct()

    influx = outcomes_by_role(report)[ab_bootstrap.ROLE_INFLUXDB]
    assert influx.source == ab_bootstrap.SOURCE_REGISTRY, influx.detail


@requires_docker
def test_a_zero_length_seed_is_refused(slot):
    slot.record_and_seed()
    (slot.store.seed_directory / f"{ab_bootstrap.ROLE_EMS}.tar").write_bytes(b"")
    slot.empty_the_image_store()

    report = slot.service.reconstruct()

    ems = outcomes_by_role(report)[ab_bootstrap.ROLE_EMS]
    assert ems.source == ab_bootstrap.SOURCE_REGISTRY, ems.detail


@requires_docker
def test_with_no_seed_at_all_the_fallback_pulls_the_exact_digest(slot):
    record = slot.service.record_running_runtime()
    slot.empty_the_image_store()

    report = slot.service.reconstruct()

    outcomes = outcomes_by_role(report)
    assert report.ok, report.problems
    for role in ab_bootstrap.ROLES:
        assert outcomes[role].source == ab_bootstrap.SOURCE_REGISTRY
        assert outcomes[role].digest == record.image(role).digest
        assert "@sha256:" in outcomes[role].reference


@requires_docker
def test_a_mutable_tag_can_never_reach_a_runtime_record(slot):
    """Refused when it is written, so no rebuild ever has one to pull."""

    record = slot.service.record_running_runtime()
    mutable = ab_bootstrap.RuntimeImage(
        role=ab_bootstrap.ROLE_ADMIN,
        reference=f"{slot.registry.host}/ems-contract-admin:{slot.registry.tag}",
        required=True,
        state=ab_bootstrap.STATE_RUNNING,
        digest=record.image(ab_bootstrap.ROLE_ADMIN).digest,
    )

    with pytest.raises(ab_bootstrap.BootstrapError) as error:
        slot.store.write([mutable], compose=record.compose, environment=record.environment)

    assert error.value.code == "runtime_reference_not_pinned"
    assert slot.store.read().image(ab_bootstrap.ROLE_ADMIN).reference.count("@sha256:") == 1


# --- an image for the wrong machine -----------------------------------------


@requires_docker
def test_an_image_built_for_another_architecture_is_refused(slot, registry):
    """A valid image, correctly pulled, for a machine this is not."""

    record = slot.service.record_running_runtime()
    entry = record.image(ab_bootstrap.ROLE_ADMIN)
    wanted = dict(entry.platform)
    foreign = "arm64" if wanted.get("architecture") != "arm64" else "amd64"
    slot.service.required_platform = {"architecture": foreign, "os": wanted.get("os", "linux")}

    report = slot.service.reconstruct()

    admin = outcomes_by_role(report)[ab_bootstrap.ROLE_ADMIN]
    assert admin.source == ab_bootstrap.SOURCE_UNAVAILABLE
    assert "arm64" in admin.detail or "amd64" in admin.detail
    assert not report.ok
    assert ab_bootstrap.ROLE_ADMIN not in report.started


# --- a service an operator deliberately stopped ------------------------------


@requires_docker
def test_an_ems_that_was_deliberately_stopped_comes_back_stopped(tmp_path, registry):
    registry.start_again()
    stopped = Slot(tmp_path, registry, stopped=(ab_bootstrap.ROLE_EMS,))
    try:
        record = stopped.record_and_seed()
        assert record.image(ab_bootstrap.ROLE_EMS).state == ab_bootstrap.STATE_STOPPED_CLEAN
        stopped.empty_the_image_store()

        report = stopped.service.reconstruct()

        outcomes = outcomes_by_role(report)
        assert report.ok, report.problems
        # The image authority still had to be satisfied for the stopped service:
        # it is restored and verified like any other, and only the *starting* is
        # skipped. Which source it came from is not what this case is about, so
        # both are accepted — the seed is simply the one that answers first now.
        assert outcomes[ab_bootstrap.ROLE_EMS].source in (
            ab_bootstrap.SOURCE_SEED,
            ab_bootstrap.SOURCE_REGISTRY,
        )
        assert outcomes[ab_bootstrap.ROLE_EMS].digest == record.image(
            ab_bootstrap.ROLE_EMS
        ).digest
        assert ab_bootstrap.ROLE_EMS not in report.started
        assert ab_bootstrap.ROLE_ADMIN in report.started
        state = stopped.backend.inspect_container(
            stopped.deployment.containers[ab_bootstrap.ROLE_EMS]
        )
        assert state.state != "running"
    finally:
        stopped.teardown()
        stopped.registry.start_again()


@requires_docker
def test_a_slot_with_no_runtime_record_refuses_to_guess(tmp_path):
    store = ab_bootstrap.RuntimeRecordStore(tmp_path / "empty")
    service = ab_bootstrap.SlotBootstrapService(
        docker=DockerBackend(commands.CommandRunner()), store=store
    )

    report = service.reconstruct()

    assert report.code == ab_bootstrap.RUNTIME_RECORD_MISSING
    assert not report.ok


@requires_docker
def test_the_registry_is_really_gone_while_the_seed_cases_run(slot):
    """The no-network case is only evidence if the network was really taken away."""

    slot.record_and_seed()
    slot.empty_the_image_store()
    slot.registry.stop()
    try:
        with pytest.raises(DockerError) as error:
            slot.backend.pull_image(slot.registry.references[ab_bootstrap.ROLE_ADMIN])
        assert error.value.code == "image_pull_failed"
    finally:
        slot.registry.start_again()


def test_the_contract_images_carry_no_project_source():
    """The contract images are fixtures, not a packaging path for this project."""

    assert "COPY . " not in ADMIN_DOCKERFILE
    assert str(ROOT) not in ADMIN_DOCKERFILE + EMS_DOCKERFILE + INFLUX_DOCKERFILE
    assert os.environ.get("EMS_CONTRACT_HEALTHY") is None


# --- deployments that are not the three-service default ----------------------


@requires_docker
def test_an_appliance_that_never_ran_influxdb_reconstructs_without_it(tmp_path, registry):
    """A service the operator never deployed is not one to invent on a rebuild."""

    registry.start_again()
    roles = (ab_bootstrap.ROLE_ADMIN, ab_bootstrap.ROLE_EMS)
    slot = Slot(tmp_path, registry, roles=roles)
    try:
        record = slot.record_and_seed()
        assert {image.role for image in record.images} == set(roles)
        slot.empty_the_image_store()
        slot.registry.stop()

        report = slot.service.reconstruct()

        outcomes = outcomes_by_role(report)
        assert report.ok, report.problems
        assert ab_bootstrap.ROLE_INFLUXDB not in outcomes
        assert ab_bootstrap.ROLE_INFLUXDB not in report.started
        for role in roles:
            assert outcomes[role].source == ab_bootstrap.SOURCE_SEED, outcomes[role].detail
            assert outcomes[role].digest == record.image(role).digest
    finally:
        slot.teardown()
        slot.registry.start_again()


@requires_docker
def test_the_data_a_service_wrote_survives_the_rebuild(tmp_path, registry):
    """Reconstruction restores images and containers, never persistent data."""

    registry.start_again()
    data = tmp_path / "persistent-data"
    data.mkdir()
    slot = Slot(tmp_path, registry, data=data)
    try:
        (data / "state.json").write_text('{"written": "before the rebuild"}\n')
        slot.record_and_seed()
        slot.empty_the_image_store()
        slot.registry.stop()

        report = slot.service.reconstruct()
        assert report.ok, report.problems

        assert (data / "state.json").read_text() == '{"written": "before the rebuild"}\n'
        seen = slot.backend.exec_in_container(
            slot.deployment.containers[ab_bootstrap.ROLE_EMS],
            ["cat", "/data/state.json"],
            timeout=60,
        )
        assert seen.ok, seen.stderr
        assert "before the rebuild" in seen.stdout
    finally:
        slot.teardown()
        slot.registry.start_again()


@requires_docker
def test_an_influxdb_that_was_deliberately_stopped_comes_back_stopped(tmp_path, registry):
    """The optional role is the one a trial is most tempted to tidy up.

    A stopped EMS is a required service the gate has to account for. InfluxDB
    is optional, so "not running" and "not deployed" look alike from a distance
    — and a trial that started it anyway would have invented a state the source
    slot never had.
    """

    registry.start_again()
    stopped = Slot(tmp_path, registry, stopped=(ab_bootstrap.ROLE_INFLUXDB,))
    try:
        record = stopped.record_and_seed()
        assert record.image(
            ab_bootstrap.ROLE_INFLUXDB
        ).state == ab_bootstrap.STATE_STOPPED_CLEAN
        stopped.empty_the_image_store()

        report = stopped.service.reconstruct()

        outcomes = outcomes_by_role(report)
        assert report.ok, report.problems
        assert outcomes[ab_bootstrap.ROLE_INFLUXDB].digest == record.image(
            ab_bootstrap.ROLE_INFLUXDB
        ).digest
        assert ab_bootstrap.ROLE_INFLUXDB not in report.started
        assert ab_bootstrap.ROLE_ADMIN in report.started
        assert ab_bootstrap.ROLE_EMS in report.started
        state = stopped.backend.inspect_container(
            stopped.deployment.containers[ab_bootstrap.ROLE_INFLUXDB]
        )
        assert state.state != "running"
    finally:
        stopped.teardown()
        stopped.registry.start_again()
