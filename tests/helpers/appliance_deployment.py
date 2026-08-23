# SPDX-License-Identifier: AGPL-3.0-or-later
"""A recording ``docker(1)`` and an EMS deployment on disk.

Everything an A/B trial depends on downstream of the OS — the compose file, the
``.env`` beside it, the container runtime — is modelled here as bytes and as
argv. Nothing in this module implements the ``SlotBootstrapService`` or
``TrialDockerHealth`` protocols: the production ``DockerBackend`` is driven over
this runner, so a test that passes proves the production argv works and not that
a fake agreed with a fake.

The engine is deliberately stateful. ``docker save`` writes an archive whose
bytes the matching ``docker load`` reads back, so an appliance with no registry
can be modelled by giving the target slot the seed directory and an empty image
store — which is exactly the offline case a trial has to survive.
"""

import json
from pathlib import Path

from appliance.commands import CommandResult, RecordingRunner
from appliance.ab_persistence import PERSISTENT_SCHEMA_VERSION

ADMIN_DIGEST = "sha256:" + "a1" * 32
EMS_DIGEST = "sha256:" + "b2" * 32
INFLUX_DIGEST = "sha256:" + "c3" * 32

ADMIN_REPOSITORY = "ghcr.io/basecubedev/ems-solarflow-admin"
EMS_REPOSITORY = "ghcr.io/basecubedev/ems-solarflow-api-control"
INFLUX_REPOSITORY = "docker.io/library/influxdb"

ADMIN_REFERENCE = f"{ADMIN_REPOSITORY}@{ADMIN_DIGEST}"
EMS_REFERENCE = f"{EMS_REPOSITORY}@{EMS_DIGEST}"
INFLUX_REFERENCE = f"{INFLUX_REPOSITORY}@{INFLUX_DIGEST}"

ADMIN_CONTAINER = "ems-solarflow-admin"
EMS_CONTAINER = "ems-solarflow"
INFLUX_CONTAINER = "ems-influxdb"

ADMIN_SERVICE = "ems-solarflow-admin"
EMS_SERVICE = "ems"
INFLUX_SERVICE = "influxdb"

COMPOSE_TEXT = """\
services:
  ems-solarflow-admin:
    image: {admin}
    container_name: ems-solarflow-admin
  ems:
    image: {ems}
    container_name: ems-solarflow
  influxdb:
    image: {influx}
    container_name: ems-influxdb
"""

ENVIRONMENT_TEXT = "EMS_ADMIN_TAG=v1.5.0\nTZ=Europe/Berlin\n"

# Every docker(1) subcommand that changes the engine's state. A drift refusal
# is only worth anything if none of these ran.
MUTATIONS = ("load", "pull", "compose", "run", "start", "stop", "rm", "tag", "save")


class Image:
    def __init__(self, reference, digest, *, architecture="arm64", os_name="linux"):
        self.reference = reference
        self.digest = digest
        self.architecture = architecture
        self.os_name = os_name

    @property
    def repository(self):
        return self.reference.partition("@")[0]

    def payload(self):
        return {
            "Id": "sha256:" + self.digest.partition(":")[2][::-1],
            "RepoDigests": [f"{self.repository}@{self.digest}"],
            "Architecture": self.architecture,
            "Os": self.os_name,
            "Config": {"Labels": {}},
        }


class DockerEngine:
    """A docker daemon with an image store, containers and an archive shelf."""

    def __init__(self, *, version="27.3.1", compose_file=""):
        self.version = version
        self.compose_file = str(compose_file)
        self.images = {}
        self.containers = {}
        self.registry = {}
        self.calls = []
        self.compose_calls = []
        self.loaded = []
        self.pulled = []
        self.saved = []
        self.execs = []
        self.exec_results = {}
        self.load_result = None
        self.save_fails = False

    # --- setting the scene ------------------------------------------------

    def add_image(self, image):
        self.images[image.reference] = image
        return image

    def publish(self, image):
        self.registry[image.reference] = image
        return image

    def add_container(self, name, image, *, running=True, health="none"):
        self.containers[name] = {"image": image.reference, "running": running, "health": health}
        return name

    def remove_container(self, name):
        self.containers.pop(name, None)

    @property
    def mutations(self):
        return [args for tool, args in self.calls if tool == "docker" and args[:1] and args[0] in MUTATIONS]

    def started_services(self):
        return [args[-1] for args in self.compose_calls if "up" in args]

    # --- the command surface ----------------------------------------------

    def runner(self):
        engine = self

        class _Runner(RecordingRunner):
            def run(self, tool, args=(), **kwargs):
                args = tuple(args)
                engine.calls.append((tool, args))
                if tool != "docker":
                    return super().run(tool, args, **kwargs)
                return engine.docker(args)

        return _Runner({}, default="")

    def docker(self, args):
        handlers = (
            ("version", self._version),
            ("inspect", self._inspect_container),
            ("image", self._image),
            ("save", self._save),
            ("load", self._load),
            ("pull", self._pull),
            ("compose", self._compose),
            ("exec", self._exec),
        )
        for prefix, handler in handlers:
            if args[:1] == (prefix,):
                return handler(args)
        return CommandResult("docker", args, 1, "", f"unknown docker subcommand {args[:1]}")

    def _result(self, args, code=0, stdout="", stderr=""):
        return CommandResult("docker", args, code, stdout, stderr)

    def _version(self, args):
        if self.version is None:
            return self._result(args, 1, "", "cannot connect to the Docker daemon")
        return self._result(args, 0, f"{self.version}\n")

    def _inspect_container(self, args):
        name = args[-1]
        entry = self.containers.get(name)
        if entry is None:
            return self._result(args, 1, "", f"No such container: {name}")
        running = bool(entry.get("running", True))
        restarting = bool(entry.get("restarting"))
        status = entry.get("status") or ("running" if running else "exited")
        payload = {
            "Id": "c" * 64,
            "State": {
                "Running": running and not restarting,
                "Restarting": restarting,
                "Status": status,
                "ExitCode": int(entry.get("exit_code", 0)),
                "Health": {"Status": entry["health"], "Log": []},
                "StartedAt": "2026-08-08T00:00:00Z",
            },
            "Config": {"Image": entry["image"], "Labels": {}},
            "Image": entry["image"],
            "RestartCount": int(entry.get("restart_count", 0)),
            "NetworkSettings": {"Ports": {}},
        }
        return self._result(args, 0, json.dumps(payload))

    def _image(self, args):
        if args[:2] != ("image", "inspect"):
            return self._result(args, 1, "", "unsupported image subcommand")
        image = self.images.get(args[-1])
        if image is None:
            return self._result(args, 1, "", "No such image")
        return self._result(args, 0, json.dumps(image.payload()))

    def _save(self, args):
        if self.save_fails:
            return self._result(args, 1, "", "no space left on device")
        reference = args[-1]
        target = Path(args[args.index("-o") + 1])
        image = self.images.get(reference)
        if image is None:
            return self._result(args, 1, "", "No such image")
        target.write_text(json.dumps({"reference": reference, "digest": image.digest}))
        self.saved.append(reference)
        return self._result(args, 0)

    def _load(self, args):
        path = Path(args[args.index("-i") + 1])
        self.loaded.append(str(path))
        if self.load_result is not None:
            return self.load_result(args, self)
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            return self._result(args, 1, "", "invalid tar archive")
        reference = payload["reference"]
        source = self.registry.get(reference) or Image(reference, payload["digest"])
        self.images[reference] = source
        return self._result(args, 0, f"Loaded image: {reference}\n")

    def _pull(self, args):
        reference = args[-1]
        self.pulled.append(reference)
        image = self.registry.get(reference)
        if image is None:
            return self._result(args, 1, "", f"manifest for {reference} not found")
        self.images[reference] = image
        return self._result(args, 0, f"Status: Downloaded newer image for {reference}\n")

    def _exec(self, args):
        """What each service answers when asked in its own terms."""

        name = args[1]
        argv = tuple(args[2:])
        self.execs.append((name, argv))
        if name not in self.containers or not self.containers[name].get("running", True):
            return self._result(args, 1, "", f"Error: container {name} is not running")
        override = self.exec_results.get(name)
        if override is not None:
            return override(args, self)
        if argv[:1] == ("python3",):
            return self._result(
                args, 0, json.dumps({"schema_version": 1, "diagnosis": {"status": "ok"}})
            )
        if argv[:1] == ("influx",):
            return self._result(args, 0, "OK\n")
        return self._result(args, 1, "", f"unknown exec {argv}")

    def _compose(self, args):
        self.compose_calls.append(args)
        if "up" not in args:
            return self._result(args, 0)
        service = args[-1]
        containers = {
            ADMIN_SERVICE: (ADMIN_CONTAINER, ADMIN_REFERENCE),
            EMS_SERVICE: (EMS_CONTAINER, EMS_REFERENCE),
            INFLUX_SERVICE: (INFLUX_CONTAINER, INFLUX_REFERENCE),
        }
        if service not in containers:
            return self._result(args, 1, "", f"no such service: {service}")
        name, reference = containers[service]
        image = self.images.get(reference)
        if image is None:
            return self._result(args, 1, "", f"pull access denied for {reference}")
        self.add_container(name, image, health="healthy")
        return self._result(args, 0, f"Container {name} Started\n")


class EmsDeployment:
    """The compose file and environment an appliance's EMS deployment is."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.compose_file.write_text(
            COMPOSE_TEXT.format(
                admin=ADMIN_REFERENCE, ems=EMS_REFERENCE, influx=INFLUX_REFERENCE
            ),
            encoding="utf-8",
        )
        self.environment_file.write_text(ENVIRONMENT_TEXT, encoding="utf-8")

    @property
    def compose_file(self):
        return self.root / "docker-compose.yml"

    @property
    def environment_file(self):
        return self.root / ".env"

    def mutate_compose(self, text=None):
        self.compose_file.write_text(
            text if text is not None else COMPOSE_TEXT.format(
                admin=ADMIN_REFERENCE, ems=EMS_REFERENCE, influx=INFLUX_REFERENCE
            )
            + "  # edited after the plan was made\n",
            encoding="utf-8",
        )
        return self.compose_file

    def mutate_environment(self, text=None):
        self.environment_file.write_text(
            text if text is not None else ENVIRONMENT_TEXT + "EMS_ADMIN_TAG=v9.9.9\n",
            encoding="utf-8",
        )
        return self.environment_file


def source_engine(deployment, *, admin=True, ems=True, influx=False, running=True):
    """An engine holding the deployment a source slot is actually running."""

    engine = DockerEngine(compose_file=str(deployment.compose_file))
    if admin:
        image = engine.add_image(Image(ADMIN_REFERENCE, ADMIN_DIGEST))
        engine.publish(image)
        engine.add_container(ADMIN_CONTAINER, image, running=running, health="healthy")
    if ems:
        image = engine.add_image(Image(EMS_REFERENCE, EMS_DIGEST))
        engine.publish(image)
        engine.add_container(EMS_CONTAINER, image, running=running, health="healthy")
    if influx:
        image = engine.add_image(Image(INFLUX_REFERENCE, INFLUX_DIGEST))
        engine.publish(image)
        engine.add_container(INFLUX_CONTAINER, image, running=running, health="healthy")
    return engine


def target_engine(deployment, *, registry=(), version="27.3.1"):
    """A freshly written slot: a live daemon and an empty image store."""

    engine = DockerEngine(version=version, compose_file=str(deployment.compose_file))
    for image in registry:
        engine.publish(image)
    return engine


# --- the appliance, wired the way appliance/services.py wires it -------------


def deployment_layout(deployment):
    from appliance.ab_bootstrap import DeploymentLayout

    return DeploymentLayout(
        compose_file=str(deployment.compose_file), install_root=str(deployment.root)
    )


def bootstrap_service(engine, store, deployment):
    """Production wiring: the real backend over the recording engine."""

    from appliance.ab_bootstrap import SlotBootstrapService
    from appliance.docker_backend import DockerBackend

    backend = DockerBackend(engine.runner(), compose_file=str(deployment.compose_file))
    return SlotBootstrapService(
        docker=backend, store=store, deployment=deployment_layout(deployment)
    )


ADMIN_INSTANCE_ID = "9f2c41d8a7b04e5c8d3f6a1b2c4e5f70"


def answering_admin(url, timeout):
    """The Admin auth-status body, exactly as ``admin/server.py`` answers it."""

    assert url.startswith("http://127.0.0.1:"), url
    return 200, json.dumps(
        {
            "admin_instance_id": ADMIN_INSTANCE_ID,
            "auth_configured": True,
            "authenticated": False,
            "requires_initial_password": False,
            "recovery_required": False,
        }
    )


def trial_health(engine, deployment, *, http_probe=answering_admin):
    from appliance.ab_docker_health import DockerTrialHealth
    from appliance.docker_backend import DockerBackend

    backend = DockerBackend(engine.runner(), compose_file=str(deployment.compose_file))
    return DockerTrialHealth(backend, http_probe=http_probe)


class PlannedAppliance:
    """One appliance with a real deployment, planned and executed for real.

    The bootstrap, the Docker backend and the update service are the production
    classes; only the block layer, the command runner and the release directory
    are fixtures. Nothing stands in for the deployment: the compose file, the
    ``.env`` and the containers are the ones the plan is bound to.
    """

    def __init__(self, tmp_path, *, influx=False, running=True, release_id=None):
        from appliance import ab_bootstrap, os_update
        from tests.helpers.appliance_ab import ApplianceAbHost, build_ab_service
        from tests.helpers.appliance_ab_artifacts import ReleaseDirectory

        self.os_update = os_update
        self.host = ApplianceAbHost(tmp_path)
        self.releases = ReleaseDirectory(tmp_path)
        self.releases.publish()
        self.release_id = release_id or "ems-solarflow-appliance-1.5.0-arm64-ab"
        self.deployment = EmsDeployment(self.host.root / "opt/ems-solarflow")
        for name in ("config", "data"):
            (self.deployment.root / name).mkdir(parents=True, exist_ok=True)
        self.engine = source_engine(self.deployment, influx=influx, running=running)
        self.store = ab_bootstrap.RuntimeRecordStore(self.host.ab_state_dir)
        self.bootstrap = bootstrap_service(self.engine, self.store, self.deployment)
        self.service = build_ab_service(
            tmp_path, self.host, self.releases, bootstrap=self.bootstrap
        )

    def plan(self, **kwargs):
        active = self.service.operations.active()
        if active is not None:
            self.service.operations.cancel(active.operation_id)
        operation = self.service.operations.create(self.os_update.TYPE_OS_UPDATE)
        return operation, self.service.plan_update(operation, self.release_id, **kwargs)

    def confirm_and_run(self, operation):
        self.service.operations.await_confirmation(operation.operation_id, {"plan": True})
        record = self.service.operations.get(operation.operation_id, include_token=True)
        self.service.operations.confirm(operation.operation_id, record.confirmation_token)
        return self.service.execute(operation)

    def target(self, operation):
        return self.service.operations.get(operation.operation_id).requested_target


class TrialAppliance:
    """One appliance across the reboot: two engines, one shared partition."""

    def __init__(self, tmp_path, *, admin=True, ems=True, influx=False, running=True,
                 slot="B"):
        from appliance.ab_bootstrap import RuntimeRecordStore
        from appliance.ab_state import AbStateStore
        from tests.helpers.appliance_ab import ApplianceAbHost

        self.host = ApplianceAbHost(tmp_path, slot=slot, tryboot=True)
        self.host.write_os_build(
            {
                "release_version": "1.5.0",
                "build_id": "20260807-1",
                "layout_id": "ems-appliance-rota-v1",
                "persistent_schema_version": PERSISTENT_SCHEMA_VERSION,
                "slot_schema_version": 2,
            }
        )
        self.deployment = EmsDeployment(self.host.root / "opt/ems-solarflow")
        for name in ("config", "data"):
            (self.host.root / "opt/ems-solarflow" / name).mkdir(parents=True, exist_ok=True)
        self.store = RuntimeRecordStore(self.host.ab_state_dir, time_fn=lambda: 1000.0)
        self.source = source_engine(
            self.deployment, admin=admin, ems=ems, influx=influx, running=running
        )
        # The registry the trial slot could reach if it had a WAN. The seed on
        # the shared partition is what makes it unnecessary, not unavailable.
        self.target = target_engine(
            self.deployment, registry=list(self.source.images.values())
        )
        self.state = AbStateStore(self.host.ab_state_dir)
        self.state.ensure()

    # --- before the reboot ------------------------------------------------

    def capture(self):
        service = bootstrap_service(self.source, self.store, self.deployment)
        record = service.record_running_runtime()
        service.seed(record)
        return self.store.read()

    def arm(self, record, *, operation_id="op-1", source_slot="A", target_slot="B"):
        from appliance.ab_state import PendingTrial
        from tests.helpers.appliance_ab import PARTUUIDS, SLOT_BOOT_PARTITION, SLOT_LABELS

        self.state.set_pending(
            PendingTrial(
                operation_id=operation_id,
                source_slot=source_slot,
                target_slot=target_slot,
                target_release="ems-solarflow-appliance-1.5.0-rpi5-arm64-ab",
                target_build_id="20260807-1",
                artifact_digest="sha256:" + "c" * 64,
                expected_boot_partition=SLOT_BOOT_PARTITION[target_slot],
                expected_root_partuuid=PARTUUIDS[SLOT_LABELS[target_slot]["root"]],
                trial_requested_at=1000.0,
                release_version="1.5.0",
                boot_digest="sha256:" + "d" * 64,
                rootfs_digest="sha256:" + "e" * 64,
                deployment_fingerprint=record.fingerprint,
            )
        )
        return self.state

    # --- after the reboot -------------------------------------------------

    def reconstruct(self):
        return bootstrap_service(self.target, self.store, self.deployment).reconstruct()

    def health(self, **kwargs):
        from tests.helpers.appliance_ab import build_health_service

        service = build_health_service(
            self.host,
            self.state,
            docker=trial_health(self.target, self.deployment, **kwargs),
            runtime=self.store,
            bootstrap=bootstrap_service(self.target, self.store, self.deployment),
            time_fn=lambda: 1100.0,
        )
        return service.evaluate()

    def trial(self, **kwargs):
        """Capture, arm, reboot, rebuild, judge. Nothing injected in between."""

        record = self.capture()
        self.arm(record)
        report = self.reconstruct()
        return report, self.health(**kwargs)
