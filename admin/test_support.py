# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic Admin runtime for browser (Playwright) end-to-end tests.

Enabled only when ``EMS_ADMIN_TEST_MODE=1`` (never set in a normal deployment).
It replaces the *external effects* the browser cannot deterministically exercise
— Docker image inspection/pull, the Admin-replacement launcher, the GitHub
release download and the embedded resource bundle location — with in-process
adapters that implement the same production interfaces. Everything the workflow
tests care about stays real: authentication, CSRF, the ``SystemAlignmentService``
state machine, resource-strategy decisions, transition persistence, discovery
authorization and server-driven button gating.

The deterministic catalog offers the three production-facing selections plus
explicit mismatch/failure fixtures:

* ``latest``   — modern, already aligned with the running Admin (embedded).
* ``Development`` — immutable development pair (embedded).
* ``v9.9.10`` — modern, needs an Admin update (different Admin digest).
* ``v0.7.0``  — legacy release (``legacy_release`` / ``release_archive``),
  installable by the modern Admin without a downgrade.
"""

import io
import json
import os
import shutil
import threading
import uuid
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse

from admin.admin_update import (
    ADMIN_IMAGE_REPO,
    EMS_IMAGE_REPO,
    PendingTransitionStore,
)
from admin.development_catalogue import development_catalogue_source
from admin.embedded_resources import (
    EmbeddedReleaseResources,
    ReleaseArchiveResources,
    write_release_resources,
)
from admin.image_identity import identify_image
from admin.install_context import detect_install_context
from admin.known_good import KnownGoodStore
from admin.releases import ReleaseManager
from admin.system_alignment import SystemAlignmentService
from admin.system_build import SystemBuildResolver

TEST_MODE_ENV = "EMS_ADMIN_TEST_MODE"
PACKAGED_RESOURCES_ENV = "EMS_ADMIN_TEST_PACKAGED_RESOURCES"

_MODERN_REVISION = "f7265fc747c2223f126f0ee7801e030c6226edf4"
_UPDATE_REVISION = "abcdef1234567890abcdef1234567890abcdef12"
_DEVELOPMENT_REVISION = "deadbee1234567890abcdef1234567890abcdef1"
_DEVELOPMENT_TAG = "dev-development-deadbee-100-1"
_OLDER_DEVELOPMENT_REVISION = "cafebad1234567890abcdef1234567890abcdef1"
_OLDER_DEVELOPMENT_TAG = "dev-development-cafebad-99-1"
_LEGACY_REVISION = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
_FAILED_UPDATE_TAG = "v9.9.11"


def _labels(*, revision, build_id, channel, release_tag, version):
    return {
        "org.opencontainers.image.version": version,
        "org.opencontainers.image.revision": revision,
        "de.basecubedev.ems.build_id": build_id,
        "de.basecubedev.ems.channel": channel,
        "de.basecubedev.ems.release_tag": release_tag,
    }


# tag -> (channel, revision, build_id, admin_digest, ems_digest)
_CATALOG = {
    "latest": ("latest", _MODERN_REVISION, "latest-f7265fc",
               "sha256:admin-latest", "sha256:ems-latest"),
    _DEVELOPMENT_TAG: (
        "development", _DEVELOPMENT_REVISION, _DEVELOPMENT_TAG,
        "sha256:" + "a" * 64, "sha256:" + "b" * 64,
    ),
    _OLDER_DEVELOPMENT_TAG: (
        "development", _OLDER_DEVELOPMENT_REVISION, _OLDER_DEVELOPMENT_TAG,
        "sha256:" + "c" * 64, "sha256:" + "d" * 64,
    ),
    "v9.9.9": ("stable", _MODERN_REVISION, "v9.9.9-f7265fc",
               "sha256:admin-999", "sha256:ems-999"),
    "v9.9.10": ("stable", _UPDATE_REVISION, "v9.9.10-abcdef1",
                "sha256:admin-9910", "sha256:ems-9910"),
    "v0.7.0": ("stable", _LEGACY_REVISION, "123456789-1",
               "sha256:admin-070", "sha256:ems-070"),
    # A legacy release whose historical archive cannot be verified (the offline
    # download fails), used to prove resource-failure blocks progress visibly.
    "v0.6.9": ("stable", "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1", "223456789-1",
               "sha256:admin-069", "sha256:ems-069"),
    _FAILED_UPDATE_TAG: (
        "stable",
        "c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2",
        "v9.9.11-c3d4e5f",
        "sha256:admin-9911",
        "sha256:ems-9911",
    ),
}

_BROKEN_RESOURCE_TAG = "v0.6.9"
_BROKEN_RESOURCE_TAGS = {_BROKEN_RESOURCE_TAG}

# The Admin that is running for the test session is the rolling Latest Admin.
_RUNNING_TAG = "latest"
_ADMIN_CONTAINER = "ems-admin"


def _packaged_resources_dir():
    if os.environ.get(PACKAGED_RESOURCES_ENV) != "1":
        return None
    resources = Path("/app/release-resources")
    descriptor = resources / "system-build.json"
    if not descriptor.is_file():
        raise RuntimeError("packaged System Build descriptor is unavailable")
    return resources


def _initial_running_tag():
    resources = _packaged_resources_dir()
    if resources is None:
        return _RUNNING_TAG
    descriptor = json.loads(
        (resources / "system-build.json").read_text(encoding="utf-8")
    )
    tag = descriptor.get("system_tag")
    if tag not in _CATALOG:
        raise RuntimeError(f"packaged System Build {tag!r} is not in the test catalogue")
    return tag


def _restore_initial_bundle(bundle_dir: Path, running_tag: str):
    resources = _packaged_resources_dir()
    if resources is None:
        _build_embedded_bundle(bundle_dir, running_tag)
        return
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    shutil.copytree(resources, bundle_dir)


def _images():
    images = {}
    for tag, (channel, revision, build_id, admin_digest, ems_digest) in _CATALOG.items():
        labels = _labels(
            revision=revision, build_id=build_id, channel=channel,
            release_tag=tag, version=tag,
        )
        images[f"{ADMIN_IMAGE_REPO}:{tag}"] = {"digest": admin_digest, "labels": labels}
        images[f"{EMS_IMAGE_REPO}:{tag}"] = {"digest": ems_digest, "labels": labels}
        # A real registry also serves each content by its immutable digest, so a
        # digest-pinned pull/inspect (Guided Upgrade deploys by digest) resolves
        # to the exact content even after the tag has moved to another digest.
        images[f"{ADMIN_IMAGE_REPO}@{admin_digest}"] = {
            "digest": admin_digest,
            "labels": labels,
        }
        images[f"{EMS_IMAGE_REPO}@{ems_digest}"] = {
            "digest": ems_digest,
            "labels": labels,
        }
    return images


def _write_test_development_catalogue(data_dir: Path):
    path = data_dir / "development-builds.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "builds": [
                    {
                        "tag": _DEVELOPMENT_TAG,
                        "display_name": "system-build-action-gating",
                        "channel": "development",
                        "revision": _DEVELOPMENT_REVISION,
                        "build_id": _DEVELOPMENT_TAG,
                        "run_id": "100",
                        "run_attempt": 1,
                        "created_at": "2026-07-17T12:00:00Z",
                        "admin_image": f"{ADMIN_IMAGE_REPO}:{_DEVELOPMENT_TAG}",
                        "admin_digest": _CATALOG[_DEVELOPMENT_TAG][3],
                        "ems_image": f"{EMS_IMAGE_REPO}:{_DEVELOPMENT_TAG}",
                        "ems_digest": _CATALOG[_DEVELOPMENT_TAG][4],
                        "installable": True,
                    },
                    {
                        "tag": _OLDER_DEVELOPMENT_TAG,
                        "display_name": "system-build-action-gating",
                        "channel": "development",
                        "revision": _OLDER_DEVELOPMENT_REVISION,
                        "build_id": _OLDER_DEVELOPMENT_TAG,
                        "run_id": "99",
                        "run_attempt": 1,
                        "created_at": "2026-07-16T12:00:00Z",
                        "admin_image": f"{ADMIN_IMAGE_REPO}:{_OLDER_DEVELOPMENT_TAG}",
                        "admin_digest": _CATALOG[_OLDER_DEVELOPMENT_TAG][3],
                        "ems_image": f"{EMS_IMAGE_REPO}:{_OLDER_DEVELOPMENT_TAG}",
                        "ems_digest": _CATALOG[_OLDER_DEVELOPMENT_TAG][4],
                        "installable": True,
                    },
                    {
                        "tag": "dev-development",
                        "display_name": "floating alias",
                        "channel": "development",
                        "installable": True,
                    },
                    {
                        "tag": "dev-development-feedbad-98-1",
                        "display_name": "failed build",
                        "channel": "development",
                        "revision": "feedbad1234567890abcdef1234567890abcdef1",
                        "build_id": "dev-development-feedbad-98-1",
                        "run_id": "98",
                        "run_attempt": 1,
                        "created_at": "2026-07-15T12:00:00Z",
                        "admin_image": f"{ADMIN_IMAGE_REPO}:dev-development-feedbad-98-1",
                        "admin_digest": "sha256:" + "e" * 64,
                        "ems_image": f"{EMS_IMAGE_REPO}:dev-development-feedbad-98-1",
                        "ems_digest": "sha256:" + "f" * 64,
                        "installable": False,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return development_catalogue_source(path)


class _TestDocker:
    """A DockerCli-shaped double backed by the deterministic image catalog.

    The running Admin identity is mutable so a simulated Admin replacement can
    flip it to the target build, exactly as a real reconnect would.
    """

    def __init__(self, running_tag=_RUNNING_TAG):
        self._images = _images()
        self._running_admin_tag = running_tag
        self._running_ems_tag = running_tag
        self._delay_latest_until_development = False
        self._latest_validation_waiting = threading.Event()
        self._development_validation_seen = threading.Event()

    # --- resolver / identity surface ------------------------------------
    def pull(self, ref, on_progress=None):
        if self._delay_latest_until_development:
            if ref.endswith(":latest"):
                self._latest_validation_waiting.set()
                self._development_validation_seen.wait()
            elif ref.endswith(f":{_DEVELOPMENT_TAG}"):
                self._development_validation_seen.set()
        if ref not in self._images:
            raise RuntimeError(f"pull failed: {ref} not found")

    def inspect_image(self, ref):
        entry = self._images.get(ref)
        if entry is None:
            return None
        return {"image_ref": ref, "digest": entry["digest"], "labels": entry["labels"]}

    def inspect_container(self, name):
        if name == _ADMIN_CONTAINER:
            return {"image": f"{ADMIN_IMAGE_REPO}:{self._running_admin_tag}",
                    "status": "running"}
        return None

    def inspect_container_image_id(self, name):
        if name == _ADMIN_CONTAINER:
            return f"{ADMIN_IMAGE_REPO}:{self._running_admin_tag}"
        return None

    # --- simulated Admin replacement ------------------------------------
    def set_running_admin_tag(self, tag):
        self._running_admin_tag = tag

    # --- simulated moved tag (a re-pushed digest after Verify) ----------
    def repin_target(self, tag, *, ems_digest=None, admin_digest=None):
        # Move only the mutable tag to a new digest. The previously verified
        # digest ref stays registered and resolvable — a moved tag must never
        # invalidate the immutable content the operator already verified.
        if ems_digest is not None:
            entry = self._images[f"{EMS_IMAGE_REPO}:{tag}"]
            entry["digest"] = ems_digest
            self._images[f"{EMS_IMAGE_REPO}@{ems_digest}"] = {
                "digest": ems_digest,
                "labels": entry["labels"],
            }
        if admin_digest is not None:
            entry = self._images[f"{ADMIN_IMAGE_REPO}:{tag}"]
            entry["digest"] = admin_digest
            self._images[f"{ADMIN_IMAGE_REPO}@{admin_digest}"] = {
                "digest": admin_digest,
                "labels": entry["labels"],
            }

    def restore_images(self):
        self._images = _images()


def _release_archive(tag):
    files = {
        f"{tag}/config.template.json": b'{"devices": []}\n',
        f"{tag}/docker-compose.example.yml": b"services: {}\n",
        f"{tag}/install-docker.sh": b"#!/bin/sh\n",
        f"{tag}/install-docker.ps1": b"Write-Host install\n",
        f"{tag}/deploy/docker/compose.influxdb.yml": b"services: {}\n",
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as handle:
        for name, value in files.items():
            handle.writestr(name, value)
    return output.getvalue()


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _offline_opener(request, timeout=None):
    """Fake the GitHub metadata + release archive download (network boundary)."""

    url = request.full_url
    if "/git/trees/" in url:
        paths = [
            "config.template.json",
            "docker-compose.example.yml",
            "install-docker.sh",
            "install-docker.ps1",
            "deploy/docker/compose.influxdb.yml",
        ]
        return _Response(
            json.dumps({"tree": [{"path": p, "type": "blob"} for p in paths]}).encode()
        )
    if "/commits/" in url:
        ref = unquote(url.rsplit("/commits/", 1)[1])
        tag = ref.rsplit("/", 1)[-1]
        if tag not in _CATALOG:
            raise ValueError(f"unknown test release ref: {ref}")
        return _Response(json.dumps({"sha": _CATALOG[tag][1]}).encode())
    if urlparse(url).hostname == "api.github.com":
        payload = [
            {
                "tag_name": tag, "name": tag,
                "published_at": "2026-01-01T10:00:00Z",
                "prerelease": False, "draft": False,
                "zipball_url": f"https://example.test/{tag}.zip",
            }
            for tag in (
                _FAILED_UPDATE_TAG,
                "v9.9.10",
                "v9.9.9",
                "v0.7.0",
                "v0.6.9",
            )
        ]
        return _Response(json.dumps(payload).encode())
    tag = urlparse(url).path.rstrip(".zip").rsplit("/", 1)[-1]
    broken_refs = _BROKEN_RESOURCE_TAGS | {
        _CATALOG[item][1] for item in _BROKEN_RESOURCE_TAGS
    }
    if tag in broken_refs:
        import urllib.error

        raise urllib.error.URLError("release resources could not be verified")
    return _Response(_release_archive(tag if tag in _CATALOG else "v0.7.0"))


def _write_stub_source(root: Path):
    """Write the minimal source tree the embedded bundle generator copies."""

    (root / "config.template.json").write_text('{"devices": []}\n', encoding="utf-8")
    (root / "docker-compose.example.yml").write_text("services: {}\n", encoding="utf-8")
    (root / "install-docker.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "install-docker.ps1").write_text("Write-Host install\n", encoding="utf-8")
    deploy = root / "deploy" / "docker"
    deploy.mkdir(parents=True, exist_ok=True)
    (deploy / "compose.influxdb.yml").write_text("services: {}\n", encoding="utf-8")


def _build_embedded_bundle(bundle_dir: Path, tag: str):
    """Generate a verifiable embedded bundle for ``tag`` (a modern build)."""

    channel, revision, build_id, _admin_digest, _ems_digest = _CATALOG[tag]
    source = bundle_dir.parent / "src"
    source.mkdir(parents=True, exist_ok=True)
    _write_stub_source(source)
    if bundle_dir.exists():
        import shutil

        shutil.rmtree(bundle_dir)
    write_release_resources(
        bundle_dir,
        source_root=source,
        system_tag=tag,
        channel=channel,
        revision=revision,
        build_id=build_id,
        release_tag=tag,
        admin_image=f"{ADMIN_IMAGE_REPO}:{tag}",
        ems_image=f"{EMS_IMAGE_REPO}:{tag}",
    )


def build_test_system_alignment(
    *, data_dir, docker, release_manager, instance_id_state, initial_running_tag
):
    """Build the real alignment service driven by the deterministic adapters."""

    admin_data_dir = Path(data_dir)
    state_dir = admin_data_dir / "state"
    bundle_dir = admin_data_dir / "embedded-bundle"
    if not (bundle_dir / "system-build.json").exists():
        _restore_initial_bundle(bundle_dir, initial_running_tag)
    transition_store = PendingTransitionStore(state_dir)

    def launcher(record):
        # A modern Admin update: simulate the container being replaced by the
        # target build so the reconnect/resume sees the aligned running Admin,
        # including the target build's embedded resource bundle.
        if record.system_tag == _FAILED_UPDATE_TAG:
            raise RuntimeError("simulated Admin replacement failure")
        docker.set_running_admin_tag(record.system_tag)
        _build_embedded_bundle(bundle_dir, record.system_tag)
        instance_id_state["value"] = uuid.uuid4().hex

    return SystemAlignmentService(
        resolver=SystemBuildResolver(
            docker=docker,
            development_build_source=release_manager.development_build,
        ),
        transition_store=transition_store,
        embedded_resources=EmbeddedReleaseResources(
            release_manager=release_manager, resources_dir=bundle_dir
        ),
        release_archive_resources=ReleaseArchiveResources(
            release_manager=release_manager
        ),
        known_good_store=KnownGoodStore(state_dir),
        current_identity=lambda: identify_image(
            docker, f"{ADMIN_IMAGE_REPO}:{docker._running_admin_tag}"
        ),
        current_ems_identity=lambda: identify_image(
            docker, f"{EMS_IMAGE_REPO}:{docker._running_ems_tag}"
        ),
        # The sidecar rewrites the persisted compose image reference as part of
        # the same replacement.  Mirror that state here so post-reconnect
        # validation sees both the running container and its restart identity
        # pinned to the selected System Build.
        persistent_ref=lambda: (
            f"{ADMIN_IMAGE_REPO}:{docker._running_admin_tag}"
        ),
        launcher=launcher,
    )


def build_test_runtime(*, data_dir):
    """Compose the deterministic Admin runtime for ``EMS_ADMIN_TEST_MODE``."""

    from admin.server import create_admin_runtime

    initial_running_tag = _initial_running_tag()
    docker = _TestDocker(initial_running_tag)
    instance_id_state = {"value": None}
    development_source = _write_test_development_catalogue(Path(data_dir))
    release_manager = ReleaseManager(
        data_dir=Path(data_dir),
        docker=docker,
        urlopen=_offline_opener,
        development_source=development_source,
    )
    system_alignment = build_test_system_alignment(
        data_dir=data_dir,
        docker=docker,
        release_manager=release_manager,
        instance_id_state=instance_id_state,
        initial_running_tag=initial_running_tag,
    )
    runtime = create_admin_runtime(
        release_manager=release_manager,
        system_alignment=system_alignment,
    )
    original_instance_id = runtime.admin_instance_id
    instance_id_state["value"] = original_instance_id

    def test_admin_instance_id():
        # The replacement process cannot answer browser probes until the
        # durable handoff has advanced past the launch request.  Keeping the
        # original identity visible during ``admin_update_pending`` prevents
        # the test runtime from racing ahead of the production lifecycle.
        transition = system_alignment.status().get("transition")
        if transition and transition.get("stage") != "admin_update_pending":
            return instance_id_state["value"]
        return original_instance_id

    runtime.test_admin_instance_id = test_admin_instance_id
    backup_failure_state = {"enabled": False}
    original_backup = runtime.config_apply._backup

    def test_backup(target):
        if backup_failure_state["enabled"]:
            raise OSError("simulated config backup failure")
        return original_backup(target)

    runtime.config_apply._backup = test_backup

    def legacy_mqtt_config():
        return {
            "config_schema_version": 3,
            "zendure_mqtt": {
                "brokers": {
                    "local_e2e": {
                        "host": "10.0.0.9",
                        "port": 1883,
                        "username": "mqtt-e2e",
                        "password": "e2e-super-secret-broker-password",
                    }
                }
            },
            "devices": [
                {
                    "type": "zendure_mqtt",
                    "name": "Legacy Hyper",
                    "product": "Hyper 2000",
                    "mqtt": {
                        "broker_ref": "local_e2e",
                        "topic_family": "legacy_zendure_json",
                        "device_id": "E2E-DEVICE",
                        "product_key": "E2E-PRODUCT",
                    },
                    "capabilities": {"write_output_limit": True},
                }
            ],
        }

    def mixed_transport_config():
        return {
            "config_schema_version": 3,
            "system": {"max_total_power": 2000},
            "grid_meter": {"type": "shelly", "ip": "192.168.50.2"},
            "zendure_mqtt": {
                "brokers": {
                    "local_mixed": {
                        "enabled": True,
                        "source": "local_mqtt",
                        "host": "192.168.50.10",
                        "port": 1883,
                        "tls": False,
                        "username": "local-user",
                        "password": "e2e-local-broker-secret",
                    },
                    "cloud_mixed": {
                        "enabled": True,
                        "source": "zendure_cloud_mqtt",
                        "host": "mqtt.zen-iot.com",
                        "port": 8883,
                        "tls": True,
                        "username": "cloud-user",
                        "password": "e2e-cloud-broker-secret",
                    },
                }
            },
            "devices": [
                {
                    "name": "Local API inverter",
                    "ip": "192.168.50.20",
                    "sn": "API-SERIAL",
                    "max_power": 800,
                },
                {
                    "type": "zendure_mqtt",
                    "name": "Local MQTT inverter",
                    "serial_number": "LOCAL-MQTT-SERIAL",
                    "hardware_profile": "hyper_2000",
                    "mqtt": {
                        "broker_ref": "local_mixed",
                        "topic_family": "legacy_zendure_json",
                        "device_id": "LOCAL-MQTT-ID",
                        "product_key": "LOCAL-PK",
                    },
                    "capabilities": {
                        "read_power": True,
                        "read_soc": True,
                        "write_output_limit": True,
                    },
                },
                {
                    "type": "zendure_mqtt",
                    "name": "Cloud MQTT inverter",
                    "serial_number": "CLOUD-MQTT-SERIAL",
                    "hardware_profile": "hyper_2000",
                    "mqtt": {
                        "broker_ref": "cloud_mixed",
                        "source": "zendure_cloud_mqtt",
                        "topic_family": "legacy_zendure_json",
                        "device_id": "CLOUD-MQTT-ID",
                        "product_key": "CLOUD-PK",
                    },
                    "capabilities": {
                        "read_power": True,
                        "read_soc": True,
                        "write_output_limit": True,
                    },
                },
            ],
        }

    def write_install_config(config):
        target = detect_install_context().config_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(config, indent=2) + "\n", encoding="utf-8"
        )

    def test_seed(scenario):
        backup_failure_state["enabled"] = scenario == "mqtt_backup_failure"
        if scenario in {"mqtt_migration", "mqtt_backup_failure"}:
            write_install_config(legacy_mqtt_config())
            # Guided-upgrade scenarios need a concrete older version so the
            # real upgrade-direction check can prove that v9.9.10 is forward.
            docker.set_running_admin_tag("v9.9.9")
            docker._running_ems_tag = "v9.9.9"
            _build_embedded_bundle(
                Path(data_dir) / "embedded-bundle", "v9.9.9"
            )
        elif scenario == "mixed_transports":
            write_install_config(mixed_transport_config())
        elif scenario == "mqtt_mutate":
            target = detect_install_context().config_path
            config = json.loads(target.read_text(encoding="utf-8"))
            config["e2e_review_nonce"] = uuid.uuid4().hex
            write_install_config(config)
        elif scenario == "system_build_admin_aligned":
            started = system_alignment.start(
                requested_tag="v9.9.10",
                mode="fresh_install",
            )
            system_alignment.resume(operation_id=started["operation_id"])
        elif scenario == "guided_upgrade_blocking_setup":
            write_install_config(legacy_mqtt_config())
            docker.set_running_admin_tag("v9.9.9")
            docker._running_ems_tag = "v9.9.9"
            _build_embedded_bundle(
                Path(data_dir) / "embedded-bundle", "v9.9.9"
            )
            started = system_alignment.start(
                requested_tag="v9.9.10",
                mode="fresh_install",
            )
            system_alignment.resume(operation_id=started["operation_id"])
        elif scenario == "system_build_latest_mismatch":
            docker.set_running_admin_tag("v9.9.9")
            _build_embedded_bundle(Path(data_dir) / "embedded-bundle", "v9.9.9")
        elif scenario == "system_build_development_aligned":
            docker.set_running_admin_tag(_DEVELOPMENT_TAG)
            _build_embedded_bundle(
                Path(data_dir) / "embedded-bundle", _DEVELOPMENT_TAG
            )
        elif scenario == "system_build_selection_race":
            docker._delay_latest_until_development = True
            docker._latest_validation_waiting.clear()
            docker._development_validation_seen.clear()
        elif scenario == "system_build_resource_verification_running":
            started = system_alignment.start(
                requested_tag="latest",
                mode="fresh_install",
            )
            system_alignment._transitions.claim_resource_verification(
                started["operation_id"]
            )
        elif scenario == "system_build_v070_resource_failure":
            _BROKEN_RESOURCE_TAGS.add("v0.7.0")
        elif scenario == "guided_upgrade_target_moved":
            # The verified upgrade target (v9.9.10) is re-pushed to a new EMS
            # digest, so a re-resolve at execute time yields a different pair.
            docker.repin_target("v9.9.10", ems_digest="sha256:ems-9910-moved")
        else:
            return {"ok": False, "error": "unknown test scenario"}
        return {"ok": True, "scenario": scenario}

    runtime.test_seed = test_seed

    def test_reset():
        """Return the deterministic runtime to a known first-run state."""

        state_dir = Path(data_dir) / "state"
        for name in (
            "pending-transition.json",
            "known-good-system-build.json",
            "selected-release.json",
        ):
            try:
                (state_dir / name).unlink()
            except FileNotFoundError:
                pass
        releases_dir = Path(data_dir) / "releases"
        if releases_dir.exists():
            import shutil

            for child in releases_dir.iterdir():
                shutil.rmtree(child, ignore_errors=True)
        docker.set_running_admin_tag(initial_running_tag)
        docker._running_ems_tag = initial_running_tag
        instance_id_state["value"] = original_instance_id
        backup_failure_state["enabled"] = False
        docker._delay_latest_until_development = False
        docker._latest_validation_waiting.clear()
        docker._development_validation_seen.clear()
        docker.restore_images()
        _BROKEN_RESOURCE_TAGS.clear()
        _BROKEN_RESOURCE_TAGS.add(_BROKEN_RESOURCE_TAG)
        try:
            detect_install_context().config_path.unlink()
        except FileNotFoundError:
            pass
        _restore_initial_bundle(
            Path(data_dir) / "embedded-bundle", initial_running_tag
        )

    runtime.test_reset = test_reset
    return runtime
