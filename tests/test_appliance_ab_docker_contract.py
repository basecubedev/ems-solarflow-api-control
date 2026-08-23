# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Docker interface a trial slot judges itself through.

``TrialHealthService`` decides whether a freshly booted slot may make itself the
default. If it asks the Docker backend questions the production backend does not
answer, every exception becomes a failed gate and no real trial slot can ever
commit — while a purpose-built fake that happens to implement the names the
health service invented keeps the suite green.

So the production ``DockerBackend`` is driven here through a recording command
runner, over the same construction path production uses. No ad hoc fake backend
appears in this module at all.
"""

import json

import pytest

from appliance import ab_bootstrap, ab_docker_health, ab_health
from appliance.ab_bootstrap import ROLE_ADMIN, ROLE_EMS, RuntimeImage, RuntimeRecordStore
from appliance.ab_state import AbStateStore, PendingTrial
from appliance.commands import CommandResult, RecordingRunner
from appliance.docker_backend import DockerBackend
from tests.helpers.appliance_ab import PARTUUIDS, ApplianceAbHost, build_health_service

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

ADMIN_DIGEST = "sha256:" + "1" * 64
EMS_DIGEST = "sha256:" + "2" * 64
ADMIN_REFERENCE = f"ghcr.io/basecubedev/ems-admin@{ADMIN_DIGEST}"
EMS_REFERENCE = f"ghcr.io/basecubedev/ems-solarflow@{EMS_DIGEST}"


def container_payload(*, image, running=True, exit_code=0):
    return {
        "Id": "c" * 64,
        "State": {
            "Running": running,
            "Status": "running" if running else "exited",
            "ExitCode": exit_code,
            "Health": {"Status": "healthy", "Log": []},
            "StartedAt": "2026-08-08T00:00:00Z",
        },
        "Config": {"Image": image, "Labels": {}},
        "Image": image,
        "RestartCount": 0,
        "NetworkSettings": {"Ports": {"8090/tcp": [{"HostPort": "8090"}]}},
    }


def image_payload(reference, digest):
    return {
        "Id": "sha256:" + "f" * 64,
        "RepoDigests": [f"{reference.partition('@')[0]}@{digest}"],
        "Architecture": "arm64",
        "Os": "linux",
        "Config": {"Labels": {}},
    }


def healthy_runner(*, containers=None, images=None, version="27.3.1", execs=None):
    """A recording runner that answers docker(1) like a healthy host."""

    answers = dict(execs or {})
    known_containers = {
        "ems-solarflow-admin": container_payload(image=ADMIN_REFERENCE),
        "ems-solarflow": container_payload(image=EMS_REFERENCE),
    }
    known_containers.update(containers or {})
    known_images = {
        ADMIN_REFERENCE: image_payload(ADMIN_REFERENCE, ADMIN_DIGEST),
        EMS_REFERENCE: image_payload(EMS_REFERENCE, EMS_DIGEST),
    }
    known_images.update(images or {})

    def docker(args):
        if args[:1] == ("version",):
            if version is None:
                return CommandResult("docker", args, 1, "", "cannot connect to the daemon")
            return CommandResult("docker", args, 0, f"{version}\n", "")
        if args[:2] == ("inspect", "--type"):
            payload = known_containers.get(args[-1])
            if payload is None:
                return CommandResult("docker", args, 1, "", "No such container")
            return CommandResult("docker", args, 0, json.dumps(payload), "")
        if args[:2] == ("image", "inspect"):
            payload = known_images.get(args[-1])
            if payload is None:
                return CommandResult("docker", args, 1, "", "No such image")
            return CommandResult("docker", args, 0, json.dumps(payload), "")
        if args[:1] == ("exec",):
            answer = answers.get(args[1])
            if answer is not None:
                return CommandResult("docker", args, *answer)
            return CommandResult(
                "docker", args, 0, json.dumps({"diagnosis": {"status": "ok"}}), ""
            )
        return CommandResult("docker", args, 0, "", "")

    return RecordingRunner({"docker": docker})


def production_backend(**kwargs):
    return DockerBackend(
        healthy_runner(**kwargs), compose_file="/opt/ems-solarflow/docker-compose.yml"
    )


ADMIN_INSTANCE_ID = "9f2c41d8a7b04e5c8d3f6a1b2c4e5f70"


def answering_admin(url, timeout):
    return 200, json.dumps(
        {
            "admin_instance_id": ADMIN_INSTANCE_ID,
            "auth_configured": True,
            "authenticated": False,
            "requires_initial_password": False,
            "recovery_required": False,
        }
    )


def runtime_image(role, reference, *, required=False, running=True):
    return RuntimeImage(
        role=role,
        reference=reference,
        required=required,
        state=(
            ab_bootstrap.STATE_RUNNING if running else ab_bootstrap.STATE_STOPPED_CLEAN
        ),
        digest=reference.partition("@")[2],
        platform={"os": "linux", "architecture": "arm64"},
    )


def trial_probe(*, backend=None, http_probe=answering_admin):
    return ab_docker_health.DockerTrialHealth(
        backend if backend is not None else production_backend(), http_probe=http_probe
    )


# --- the interface mismatch itself ------------------------------------------


def test_the_production_backend_answers_the_trial_health_protocol():
    probe = trial_probe()
    for name in ("daemon_usable", "admin_runtime", "ems_runtime"):
        assert callable(getattr(probe, name)), name


def test_the_production_backend_reports_a_usable_daemon():
    assert trial_probe().daemon_usable().ok


def test_a_stopped_daemon_is_reported_rather_than_raised():
    result = trial_probe(backend=production_backend(version=None)).daemon_usable()
    assert not result.ok
    assert result.code == "docker_daemon_unreachable"


def test_the_admin_gate_requires_the_exact_recorded_digest():
    probe = trial_probe()
    assert probe.admin_runtime(ADMIN_DIGEST).ok
    other = probe.admin_runtime("sha256:" + "9" * 64)
    assert not other.ok
    assert other.code == "admin_image_digest_mismatch"


def test_the_admin_gate_requires_the_http_endpoint_to_answer():
    probe = trial_probe(http_probe=lambda url, timeout: (502, ""))
    result = probe.admin_runtime(ADMIN_DIGEST)
    assert not result.ok
    assert result.code == "admin_http_unhealthy"


def test_the_admin_gate_refuses_a_container_that_only_exists():
    backend = production_backend(
        containers={"ems-solarflow-admin": container_payload(image=ADMIN_REFERENCE, running=False)}
    )
    result = trial_probe(backend=backend).admin_runtime(ADMIN_DIGEST)
    assert not result.ok
    assert result.code == "admin_container_not_running"


def test_the_ems_gate_accepts_a_deliberately_stopped_deployment():
    backend = production_backend(
        containers={"ems-solarflow": container_payload(image=EMS_REFERENCE, running=False)}
    )
    result = trial_probe(backend=backend).ems_runtime(EMS_DIGEST, expected_running=False)
    assert result.ok


def test_the_ems_gate_requires_a_previously_running_deployment_to_run():
    backend = production_backend(
        containers={"ems-solarflow": container_payload(image=EMS_REFERENCE, running=False)}
    )
    result = trial_probe(backend=backend).ems_runtime(EMS_DIGEST, expected_running=True)
    assert not result.ok
    assert result.code == "ems_container_not_running"


def test_the_probe_never_builds_a_command_from_a_string():
    backend = production_backend()
    probe = ab_docker_health.DockerTrialHealth(backend, http_probe=answering_admin)
    probe.daemon_usable()
    probe.admin_runtime(ADMIN_DIGEST)
    for tool, args, _stdin in backend.runner.calls:
        assert tool == "docker"
        assert all(isinstance(arg, str) and " " not in arg.strip() or arg.startswith("{{") for arg in args)


# --- the health service over the production backend -------------------------


@pytest.fixture
def host(tmp_path):
    host = ApplianceAbHost(tmp_path, slot="B", tryboot=True)
    host.write_os_build(
        {
            "release_version": "1.5.0",
            "build_id": "20260807-1",
            "layout_id": "ems-appliance-rota-v1",
            "persistent_schema_version": 1,
            "slot_schema_version": 1,
        }
    )
    return host


def arm(host, fingerprint):
    """A pending trial bound to the deployment it was planned against."""

    store = AbStateStore(host.ab_state_dir)
    store.ensure()
    store.set_pending(
        PendingTrial(
            operation_id="op-1",
            source_slot="A",
            target_slot="B",
            target_release="ems-solarflow-appliance-1.5.0-rpi5-arm64-ab",
            target_build_id="20260807-1",
            artifact_digest="sha256:" + "c" * 64,
            expected_boot_partition=3,
            expected_root_partuuid=PARTUUIDS["system_b"],
            trial_requested_at=1000.0,
            release_version="1.5.0",
            boot_digest="sha256:" + "d" * 64,
            rootfs_digest="sha256:" + "e" * 64,
            deployment_fingerprint=fingerprint,
        )
    )
    return store


@pytest.fixture
def runtime(host):
    store = RuntimeRecordStore(host.ab_state_dir)
    store.write(
        [
            runtime_image(ROLE_ADMIN, ADMIN_REFERENCE, required=True, running=True),
            runtime_image(ROLE_EMS, EMS_REFERENCE, running=True),
        ]
    )
    return store


@pytest.fixture
def state(host, runtime):
    return arm(host, runtime.read().fingerprint)


def test_a_healthy_trial_slot_commits_against_the_production_backend(host, state, runtime):
    """The regression: the real backend must not fail every application gate."""

    service = build_health_service(
        host, state, docker=trial_probe(), runtime=runtime, time_fn=lambda: 1100.0
    )
    report = service.evaluate()
    failed = [gate.name for gate in report.gates if gate.required and not gate.passed]
    assert failed == [], f"the production Docker backend failed {failed}"
    assert report.result == ab_health.RESULT_HEALTHY


def test_a_missing_admin_container_blocks_the_commit(host, state, runtime):
    backend = production_backend(containers={"ems-solarflow-admin": None})
    service = build_health_service(
        host,
        state,
        docker=trial_probe(backend=backend),
        runtime=runtime,
        time_fn=lambda: 1100.0,
    )
    report = service.evaluate()
    assert report.result == ab_health.RESULT_UNHEALTHY
    assert any("admin_runtime" in reason for reason in report.reasons)


def test_an_admin_container_on_another_digest_blocks_the_commit(host, state, runtime):
    other = "sha256:" + "7" * 64
    reference = f"ghcr.io/basecubedev/ems-admin@{other}"
    backend = production_backend(
        containers={"ems-solarflow-admin": container_payload(image=reference)},
        images={reference: image_payload(reference, other)},
    )
    service = build_health_service(
        host,
        state,
        docker=trial_probe(backend=backend),
        runtime=runtime,
        time_fn=lambda: 1100.0,
    )
    report = service.evaluate()
    assert report.result == ab_health.RESULT_UNHEALTHY
    assert any("admin_runtime" in reason for reason in report.reasons)


def test_a_stopped_ems_deployment_recorded_as_stopped_still_commits(host):
    store = RuntimeRecordStore(host.ab_state_dir)
    store.write(
        [
            runtime_image(ROLE_ADMIN, ADMIN_REFERENCE, required=True, running=True),
            runtime_image(ROLE_EMS, EMS_REFERENCE, running=False),
        ]
    )
    state = arm(host, store.read().fingerprint)
    backend = production_backend(
        containers={"ems-solarflow": container_payload(image=EMS_REFERENCE, running=False)}
    )
    service = build_health_service(
        host, state, docker=trial_probe(backend=backend), runtime=store, time_fn=lambda: 1100.0
    )
    report = service.evaluate()
    assert report.result == ab_health.RESULT_HEALTHY


# --- exact command construction -----------------------------------------------


def calls_of(backend, tool="docker"):
    return [args for name, args, _stdin in backend.runner.calls if name == tool]


def test_the_daemon_probe_builds_a_fixed_argv():
    backend = production_backend()
    trial_probe(backend=backend).daemon_usable()

    assert calls_of(backend) == [("version", "--format", "{{.Server.Version}}")]


def test_the_container_inspection_builds_a_fixed_argv():
    backend = production_backend()
    backend.inspect_container("ems-solarflow-admin")

    assert calls_of(backend) == [
        ("inspect", "--type", "container", "--format", "{{json .}}", "ems-solarflow-admin")
    ]


def test_the_image_inspection_builds_a_fixed_argv():
    backend = production_backend()
    backend.inspect_image(ADMIN_REFERENCE)

    assert calls_of(backend) == [
        ("image", "inspect", "--format", "{{json .}}", ADMIN_REFERENCE)
    ]


def test_compose_up_names_the_configured_file_and_one_service():
    backend = production_backend()
    backend.compose_up_service("admin")

    assert calls_of(backend) == [
        (
            "compose",
            "-f",
            "/opt/ems-solarflow/docker-compose.yml",
            "up",
            "-d",
            "--no-deps",
            "admin",
        )
    ]


def test_image_load_names_a_path_and_never_a_shell_redirect():
    backend = production_backend()
    backend.load_image("/var/lib/ems-appliance-os-update/runtime-seed/admin.tar")

    assert calls_of(backend) == [
        ("load", "-i", "/var/lib/ems-appliance-os-update/runtime-seed/admin.tar")
    ]


def test_no_docker_argument_is_ever_a_composed_command_string():
    backend = production_backend()
    probe = trial_probe(backend=backend)
    probe.daemon_usable()
    probe.admin_runtime(ADMIN_DIGEST)
    probe.ems_runtime(EMS_DIGEST, expected_running=True)
    backend.compose_up_service("admin")
    backend.load_image("/tmp/seed.tar")

    for args in calls_of(backend):
        for argument in args:
            assert isinstance(argument, str)
            assert ";" not in argument
            assert "&&" not in argument
            assert "|" not in argument
            assert "$(" not in argument
