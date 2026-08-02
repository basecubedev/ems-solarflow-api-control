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

import hashlib
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
from admin.models import DiscoveredDevice, utc_now_iso
from admin.setup_workflow import SETUP_TRANSITION_MODES, SetupWorkflowArtifacts
from admin.embedded_resources import (
    EmbeddedReleaseResources,
    ReleaseArchiveResources,
    write_release_resources,
)
from admin.image_identity import identify_image
from admin.install_context import detect_install_context
from admin.known_good import KnownGoodStore
from admin.mdns import MdnsProvider, build_candidate
from admin.operation_coordinator import OperationCoordinator
from admin.releases import ReleaseManager
from admin.system_alignment import SystemAlignmentService
from admin.system_build import SystemBuildResolver
from admin.workflow_lifecycle import (
    ReplacementActivity,
    admin_replacement_activity,
)

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

    @staticmethod
    def list_containers(name_prefix):
        # The deterministic runtime never starts a real updater sidecar, so it
        # can prove there is none — which is what a release has to establish.
        del name_prefix
        return []

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
    *,
    data_dir,
    docker,
    release_manager,
    instance_id_state,
    initial_running_tag,
    operation_coordinator=None,
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
        operation_coordinator=operation_coordinator,
    )


def _install_setup_commit_hold(system_alignment):
    """Let a browser test hold a Setup System Build start at its commit point.

    The route takes its lifecycle claim before calling into System Alignment, so
    blocking on entry here is exactly "the creation owns the workflow and its
    transition has not been committed yet" — the state a concurrent Abandon has to
    be refused in. Deterministic by construction: the test arms the hold, waits
    until a request is actually parked in it, and releases it explicitly.
    """

    state = {
        "armed": False,
        "entered": threading.Event(),
        "release": threading.Event(),
    }

    def gate(mode):
        if not state["armed"] or mode not in SETUP_TRANSITION_MODES:
            return
        state["entered"].set()
        state["release"].wait(60)

    real_start_resolved = system_alignment.start_resolved
    real_confirm = system_alignment.confirm_setup_build

    def start_resolved(*, mode, **kwargs):
        gate(mode)
        return real_start_resolved(mode=mode, **kwargs)

    def confirm_setup_build(*, mode, **kwargs):
        gate(mode)
        return real_confirm(mode=mode, **kwargs)

    system_alignment.start_resolved = start_resolved
    system_alignment.confirm_setup_build = confirm_setup_build
    return state


def _install_resource_import_hold(system_alignment):
    """Let a browser test park a real resource import inside its cache mutation.

    The importer runs under the operation's coordinator claim, so parking here is
    exactly "a live worker still owns this operation" — the state an expired
    transition must not be abandoned in. Deterministic by construction: the test
    arms the hold, the seed returns only once a request is actually parked, and
    the release is explicit.
    """

    state = {
        "armed": False,
        "entered": threading.Event(),
        "release": threading.Event(),
        "worker": None,
    }
    state["release"].set()

    def wrap(provider):
        if provider is None:
            return
        real = provider.import_into_cache

        def import_into_cache(**kwargs):
            if state["armed"]:
                state["entered"].set()
                state["release"].wait(60)
            return real(**kwargs)

        provider.import_into_cache = import_into_cache

    wrap(system_alignment._embedded)
    wrap(system_alignment._release_archive)
    return state


def _expire_transition(system_alignment):
    """Move the durable transition's TTL into the past under the store's lock.

    A controllable clock rather than a delay: the record's own ``expires_at`` is
    what every expiry check reads, so rewriting it is the whole simulation.
    """

    store = system_alignment._transitions
    with store._locked():
        raw = store._read_raw()
        if raw is None:
            return False
        raw["expires_at"] = "2000-01-01T00:00:00Z"
        store._write_raw(raw)
    return True


def _settle_resource_import_hold(resource_hold):
    """Release a parked import and wait for its worker to finish."""

    resource_hold["armed"] = False
    resource_hold["release"].set()
    worker = resource_hold["worker"]
    resource_hold["worker"] = None
    if worker is None:
        return True
    worker.join(30)
    return not worker.is_alive()


SEEDED_INVERTER_IP = "192.168.90.40"
SEEDED_INVERTER_SERIAL = "E2ESETUPSN0001"


def _build_test_mdns_provider():
    """A real ``MdnsProvider`` with no multicast and no network verification.

    Browser-test discovery must be a property of the scenario, never of the
    host network: with a real provider the same spec finds the developer's own
    hardware locally and nothing at all on a CI runner. The inert browser
    factory keeps the whole lifecycle (enable/disable/refresh/status) real while
    the only devices that can ever appear are the seeded ones.
    """

    seeded = {}
    provider = MdnsProvider(
        verifier=lambda ip, port: seeded.get((ip, int(port))),
        browser_factory=lambda service_type, handler: object(),
    )
    return provider, seeded


def _seed_local_api_inverter(provider, seeded, *, ip, serial):
    """Publish one verified Local-API inverter through the real merge path.

    Guided Setup auto-adds a discovered inverter, so this single seeded device
    is what makes the browser's own draft previewable without it selecting
    anything — the scenario, not the host network, decides that it exists.
    """

    seeded[(ip, 80)] = DiscoveredDevice(
        ip=ip,
        api_family="zendure_local_http",
        device_type="zendure_solarflow_800_pro2",
        role_suggestion="inverter",
        port=80,
        display_name="SolarFlow 800 Pro 2",
        model="SolarFlow 800 Pro 2",
        serial_number=serial,
        confidence=0.95,
        config_ready=True,
    )
    pending = provider.handle_candidate(
        build_candidate(
            service_name=f"Zendure-800Pro2-{serial}",
            hostname="zendure-e2e.local.",
            addresses=[ip],
            port=80,
            properties={b"sn": serial.encode(), b"model": b"800Pro2"},
        ),
        force_verify=True,
    )
    result = getattr(pending, "result", None)
    if callable(result):
        result(30)


def _reset_test_mdns_provider(provider, seeded):
    """Return the inert provider to the empty state it is constructed in.

    Disabling first is what makes the rest safe: it shuts the verify executor
    down and drops it, so nothing can merge a device into the store afterwards.
    Nothing else can be in flight either — the inert browser factory never calls
    the handler, and both paths that do submit a candidate (the seeder and
    ``refresh``) block on their own futures before returning.

    The device store is cleared through its public API. The two candidate maps
    have none, so they are cleared here under the provider's own lock — the same
    one ``handle_candidate`` and ``_verify_and_merge`` take to touch them.
    """

    provider.disable()
    seeded.clear()
    provider._store.clear()
    with provider._lock:
        provider._candidate_cache.clear()
        provider._known_candidates.clear()


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
    # One coordinator for the injected alignment service and the runtime, so the
    # deterministic runtime proves the same worker ownership production does.
    operation_coordinator = OperationCoordinator()
    system_alignment = build_test_system_alignment(
        data_dir=data_dir,
        docker=docker,
        release_manager=release_manager,
        instance_id_state=instance_id_state,
        initial_running_tag=initial_running_tag,
        operation_coordinator=operation_coordinator,
    )
    commit_hold = _install_setup_commit_hold(system_alignment)
    resource_hold = _install_resource_import_hold(system_alignment)
    mdns_provider, seeded_mdns_devices = _build_test_mdns_provider()
    runtime = create_admin_runtime(
        release_manager=release_manager,
        system_alignment=system_alignment,
        operation_coordinator=operation_coordinator,
        mdns_provider=mdns_provider,
        docker=docker,
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

    switch_serial = "SWITCH-SERIAL"
    # Cloud proposals always share the one reserved cloud broker ref.
    _CLOUD_SWITCH_REF = "zendure_cloud"

    def _switchback_common():
        return {
            "config_schema_version": 3,
            "system": {"max_total_power": 2000},
            "grid_meter": {"type": "shelly", "ip": "192.168.60.2"},
        }

    def _switchback_mqtt_device(broker_ref, device_id, product_key):
        # mqtt.source is deliberately omitted: the broker profile is the
        # authority, so the draft must resolve the transport from it.
        return {
            "type": "zendure_mqtt",
            "name": "INV_1",
            "serial_number": switch_serial,
            "hardware_profile": "hyper_2000",
            "max_power": 642,
            "min_soc": 22,
            "mqtt": {
                "broker_ref": broker_ref,
                "topic_family": "legacy_zendure_json",
                "device_id": device_id,
                "product_key": product_key,
            },
            "capabilities": {
                "read_power": True,
                "read_soc": True,
                "write_output_limit": True,
            },
        }

    def _switchback_local_ref(host):
        """The ref real discovery assigns this endpoint.

        The seeded config has to name its profiles exactly as a config written
        from discovery would, or the browser cannot recognize the installed
        connection among the offered ones.
        """

        from ems.zendure_mqtt.config_entries import (
            normalized_broker_identity,
            stable_local_broker_ref,
        )

        return stable_local_broker_ref(
            normalized_broker_identity(
                {"source": "local_mqtt", "host": host, "port": 1883, "tls": False}
            )
        )

    def _local_broker(host):
        return {
            "enabled": True,
            "source": "local_mqtt",
            "host": host,
            "port": 1883,
            "tls": False,
            "username": "local-user",
            "password": "e2e-local-broker-secret",
        }

    def _cloud_broker():
        return {
            "enabled": True,
            "source": "zendure_cloud_mqtt",
            "host": "mqtt.zen-iot.com",
            "port": 8883,
            "tls": True,
            "username": "cloud-user",
            "password": "e2e-cloud-broker-secret",
        }

    def local_broker_switchback_config():
        """INV_1 on local broker b1, with b2 configured as a second scope."""

        config = _switchback_common()
        config["zendure_mqtt"] = {
            "brokers": {
                _switchback_local_ref("192.168.60.10"): _local_broker("192.168.60.10"),
                _switchback_local_ref("192.168.60.11"): _local_broker("192.168.60.11"),
            }
        }
        config["devices"] = [
            _switchback_mqtt_device(
                _switchback_local_ref("192.168.60.10"),
                "SWITCH-ROUTE-B1",
                "SWITCH-PK",
            )
        ]
        return config

    def api_cloud_switchback_config():
        """INV_1 on the local API, with a Cloud broker available to switch to."""

        config = _switchback_common()
        config["zendure_mqtt"] = {"brokers": {_CLOUD_SWITCH_REF: _cloud_broker()}}
        config["devices"] = [
            {
                "name": "INV_1",
                "ip": "192.168.60.20",
                "sn": switch_serial,
                "max_power": 642,
                "min_soc": 22,
            }
        ]
        return config

    def local_cloud_switchback_config():
        """INV_1 on local broker b1, with the Cloud broker available to move to."""

        config = _switchback_common()
        config["zendure_mqtt"] = {
            "brokers": {
                _switchback_local_ref("192.168.60.10"): _local_broker("192.168.60.10"),
                _CLOUD_SWITCH_REF: _cloud_broker(),
            }
        }
        config["devices"] = [
            _switchback_mqtt_device(
                _switchback_local_ref("192.168.60.10"),
                "SWITCH-ROUTE-B1",
                "SWITCH-PK",
            )
        ]
        return config

    def cloud_local_switchback_config():
        """INV_1 on the Cloud broker, with local b1 declared to move back to."""

        config = _switchback_common()
        config["zendure_mqtt"] = {
            "brokers": {
                _switchback_local_ref("192.168.60.10"): _local_broker("192.168.60.10"),
                _CLOUD_SWITCH_REF: _cloud_broker(),
            }
        }
        config["devices"] = [
            _switchback_mqtt_device(
                _CLOUD_SWITCH_REF, "SWITCH-ROUTE-CLOUD", "SWITCH-PK"
            )
        ]
        return config

    def cloud_api_switchback_config():
        """INV_1 on Cloud MQTT with no stated source, discoverable over the API."""

        config = _switchback_common()
        config["zendure_mqtt"] = {"brokers": {_CLOUD_SWITCH_REF: _cloud_broker()}}
        config["devices"] = [
            _switchback_mqtt_device(
                _CLOUD_SWITCH_REF, "SWITCH-ROUTE-CLOUD", "SWITCH-PK"
            )
        ]
        return config

    serialless_route = "E2E_CLOUD_ROUTE_7501"
    serialless_product = "E2E_CLOUD_PRODUCT_75"
    serialized_route = "E2E_CLOUD_ROUTE_7502"
    serialized_product = "E2E_CLOUD_PRODUCT_76"
    serialized_serial = "E2E-CLOUD-SERIAL-7502"

    def serialless_cloud_config():
        return {
            "config_schema_version": 3,
            "system": {"max_total_power": 1600},
            "grid_meter": {"type": "shelly", "ip": "192.168.75.2"},
            "zendure_mqtt": {
                "brokers": {
                    "zendure_cloud": {
                        "enabled": True,
                        "source": "zendure_cloud_mqtt",
                        "host": "mqtt.zen-iot.com",
                        "port": 8883,
                        "tls": True,
                        "username": "cloud-e2e-user",
                        "password": "cloud-e2e-password",
                    }
                }
            },
            "devices": [
                {
                    "name": "Existing API inverter",
                    "ip": "192.168.75.20",
                    "sn": "E2E-API-7501",
                    "max_power": 800,
                }
            ],
        }

    def cloud_identity_observation(
        *,
        source,
        host,
        port,
        device_id=serialless_route,
        serial_number=None,
        product_key=serialless_product,
        display_name="Serial-less SolarFlow",
    ):
        return {
            "broker_id": f"{source}:{host}:{port}",
            "broker_host": host,
            "broker_port": port,
            "topic_family": "legacy_zendure_json",
            "device_id": device_id,
            "serial_number": serial_number,
            "product_key": product_key,
            "model_hint": display_name,
            "display_name": display_name,
            "metrics_seen": ["packInput", "outputHomePower"],
            "topics_seen": [
                f"iot/{product_key}/{device_id}/properties/report"
            ],
            "source_type": source,
            "tls_mode": "system_ca" if source == "zendure_cloud_mqtt" else None,
        }

    enrichment_serial = "E2E-CLOUD-SERIAL-7501"

    def serialless_cloud_route_enrichment_config():
        config = serialless_cloud_config()
        config["devices"].append(
            {
                "name": "Roof Serial-less",
                "type": "zendure_mqtt",
                "pv_kwp": 2.5,
                "mqtt": {
                    "broker_ref": "zendure_cloud",
                    "topic_family": "legacy_zendure_json",
                    "product_key": serialless_product,
                    "device_id": serialless_route,
                },
                "capabilities": {
                    "read_power": True,
                    "read_soc": True,
                    "write_output_limit": False,
                },
            }
        )
        return config

    def seed_serialized_same_route_candidate():
        # The same Cloud route re-observed, now carrying a physical serial: the
        # route alias must still resolve to the existing serial-less device.
        runtime.zendure_cloud_discovery._trusted_candidates = [
            cloud_identity_observation(
                source="zendure_cloud_mqtt",
                host="mqtt.zen-iot.com",
                port=8883,
                device_id=serialless_route,
                serial_number=enrichment_serial,
                product_key=serialless_product,
                display_name="Serialized Roof",
            ),
        ]
        runtime.zendure_cloud_discovery._candidates = []

    def seed_serialless_cloud_candidate():
        runtime.zendure_cloud_discovery._trusted_candidates = [
            cloud_identity_observation(
                source="zendure_cloud_mqtt",
                host="mqtt.zen-iot.com",
                port=8883,
            ),
            cloud_identity_observation(
                source="zendure_cloud_mqtt",
                host="mqtt.zen-iot.com",
                port=8883,
                device_id=serialized_route,
                serial_number=serialized_serial,
                product_key=serialized_product,
                display_name="Serialized Cloud SolarFlow",
            ),
        ]
        runtime.zendure_cloud_discovery._candidates = []

    def clear_local_mqtt_candidates():
        generation = runtime.mqtt_discovery.store.begin_refresh()
        runtime.mqtt_discovery.store.complete_refresh(
            generation, [], success=True
        )

    def seed_other_scope_local_candidate():
        observation = cloud_identity_observation(
            source="local_mqtt",
            host="192.168.75.30",
            port=1883,
        )
        generation = runtime.mqtt_discovery.store.begin_refresh()
        runtime.mqtt_discovery.store.complete_refresh(
            generation,
            [
                {
                    "id": "mqtt:192.168.75.30:1883",
                    "host": "192.168.75.30",
                    "port": 1883,
                    "tls": False,
                    "reachable": True,
                    "topic_refresh_success": True,
                    "devices": [observation],
                }
            ],
            success=True,
        )

    def seed_api_serial_local_candidate():
        """A real local observation of the configured Local API inverter.

        Replacing that device's transport is proposal-authorized, so the backend
        needs its own candidate for that serial — the browser cannot supply one.
        """

        generation = runtime.mqtt_discovery.store.begin_refresh()
        runtime.mqtt_discovery.store.complete_refresh(
            generation,
            [
                {
                    "id": "mqtt:192.168.50.30:1883",
                    "host": "192.168.50.30",
                    "port": 1883,
                    "tls": False,
                    "reachable": True,
                    "topic_refresh_success": True,
                    "devices": [
                        {
                            "broker_id": "local_mqtt:192.168.50.30:1883",
                            "broker_host": "192.168.50.30",
                            "broker_port": 1883,
                            "source_type": "local_mqtt",
                            "topic_family": "zensdk_ha_scalar",
                            "device_id": "API-SERIAL",
                            "serial_number": "API-SERIAL",
                            "model_hint": "SolarFlow 800 Pro 2",
                            "display_name": "SolarFlow 800 Pro 2",
                            "metrics_seen": ["electricLevel", "outputHomePower"],
                            "topics_seen": [
                                "Zendure/sensor/API-SERIAL/electricLevel"
                            ],
                        }
                    ],
                }
            ],
            success=True,
        )

    def seed_api_serial_controllable_cloud_candidate():
        """The same inverter on the Zendure cloud broker with a complete route.

        The telemetry family is scalar, but the cloud broker carries the
        canonical write route on every family and the route
        iot/<productKey>/<deviceId>/… is complete, so the connection is
        control-capable — the classification of its telemetry topics is not what
        decides that.
        """

        runtime.zendure_cloud_discovery._trusted_candidates = [
            {
                "broker_id": "zendure_cloud_mqtt:mqtt.zen-iot.com:8883",
                "broker_host": "mqtt.zen-iot.com",
                "broker_port": 8883,
                "tls_mode": "encrypted_no_verify",
                "source_type": "zendure_cloud_mqtt",
                "topic_family": "zensdk_ha_scalar",
                "device_id": "API-SERIAL",
                "serial_number": "API-SERIAL",
                "product_key": "API-PK",
                "model_hint": "SolarFlow 800 Pro 2",
                "display_name": "SolarFlow 800 Pro 2",
                "metrics_seen": [
                    "electricLevel",
                    "outputHomePower",
                    "outputLimit",
                ],
            }
        ]
        runtime.zendure_cloud_discovery._candidates = []

    def seed_api_serial_controllable_local_candidate():
        """The same inverter on a *local* broker publishing scalar metrics only.

        Every other axis is complete — supported model, product key, MQTT route
        id — so this connection isolates the broker-source axis: no hardware
        evidence exists that such a broker relays a command back to the device,
        so output control must stay unavailable.
        """

        generation = runtime.mqtt_discovery.store.begin_refresh()
        runtime.mqtt_discovery.store.complete_refresh(
            generation,
            [
                {
                    "id": "mqtt:192.168.50.30:1883",
                    "host": "192.168.50.30",
                    "port": 1883,
                    "tls": False,
                    "reachable": True,
                    "topic_refresh_success": True,
                    "devices": [
                        {
                            "broker_id": "local_mqtt:192.168.50.30:1883",
                            "broker_host": "192.168.50.30",
                            "broker_port": 1883,
                            "source_type": "local_mqtt",
                            "topic_family": "zensdk_ha_scalar",
                            "device_id": "API-SERIAL",
                            "serial_number": "API-SERIAL",
                            "product_key": "API-PK",
                            "model_hint": "SolarFlow 800 Pro 2",
                            "display_name": "SolarFlow 800 Pro 2",
                            "metrics_seen": [
                                "electricLevel",
                                "outputHomePower",
                                "outputLimit",
                            ],
                            "topics_seen": [
                                "Zendure/sensor/API-SERIAL/electricLevel"
                            ],
                        }
                    ],
                }
            ],
            success=True,
        )

    def _switchback_observation(*, source, host, port, device_id, serial=None):
        return {
            "broker_id": f"{source}:{host}:{port}",
            "broker_host": host,
            "broker_port": port,
            "source_type": source,
            "topic_family": "legacy_zendure_json",
            "device_id": device_id,
            "serial_number": serial or switch_serial,
            "product_key": "SWITCH-PK",
            # Matches the stored hardware_profile, so a switch keeps resolving
            # the same concrete model and the device stays control-capable.
            "model_hint": "Hyper 2000",
            "display_name": "Hyper 2000",
            "metrics_seen": ["packInput", "outputHomePower"],
            "topics_seen": [f"iot/SWITCH-PK/{device_id}/properties/report"],
            "tls_mode": "system_ca" if source == "zendure_cloud_mqtt" else None,
        }

    def seed_switchback_local_candidates(*endpoints):
        """Real local observations behind the switchback connection offers.

        The connection switch is authorized by a current trusted proposal, so
        these specs need the backend's own discovery state — a browser-side
        proposal mock proves nothing to the server. Each endpoint is
        ``(host, device_id)`` or ``(host, device_id, serial)``; a distinct serial
        seeds another physical inverter on that broker.
        """

        generation = runtime.mqtt_discovery.store.begin_refresh()
        runtime.mqtt_discovery.store.complete_refresh(
            generation,
            [
                {
                    "id": f"mqtt:{endpoint[0]}:1883",
                    "host": endpoint[0],
                    "port": 1883,
                    "tls": False,
                    "reachable": True,
                    "topic_refresh_success": True,
                    "devices": [
                        _switchback_observation(
                            source="local_mqtt",
                            host=endpoint[0],
                            port=1883,
                            device_id=endpoint[1],
                            serial=endpoint[2] if len(endpoint) > 2 else None,
                        )
                    ],
                }
                for endpoint in endpoints
            ],
            success=True,
        )

    def seed_switchback_cloud_candidate():
        runtime.zendure_cloud_discovery._trusted_candidates = [
            _switchback_observation(
                source="zendure_cloud_mqtt",
                host="mqtt.zen-iot.com",
                port=8883,
                device_id="SWITCH-ROUTE-CLOUD",
            )
        ]
        runtime.zendure_cloud_discovery._candidates = []

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
        elif scenario == "delete_install_config":
            # Deletion is a revision state too; the browser tests need to reach it.
            try:
                detect_install_context().config_path.unlink()
            except FileNotFoundError:
                pass
        elif scenario == "mixed_transports":
            write_install_config(mixed_transport_config())
        elif scenario == "mixed_transports_api_mqtt_switch":
            write_install_config(mixed_transport_config())
            seed_api_serial_local_candidate()
        elif scenario == "mixed_transports_api_mqtt_control_switch":
            write_install_config(mixed_transport_config())
            clear_local_mqtt_candidates()
            seed_api_serial_controllable_cloud_candidate()
        elif scenario == "mixed_transports_api_local_scalar_switch":
            write_install_config(mixed_transport_config())
            seed_api_serial_controllable_local_candidate()
        elif scenario == "maintenance_local_broker_switchback":
            write_install_config(local_broker_switchback_config())
            seed_switchback_local_candidates(
                ("192.168.60.10", "SWITCH-ROUTE-B1"),
                ("192.168.60.11", "SWITCH-ROUTE-B2"),
            )
        elif scenario == "maintenance_api_cloud_switchback":
            write_install_config(api_cloud_switchback_config())
            clear_local_mqtt_candidates()
            seed_switchback_cloud_candidate()
        elif scenario == "maintenance_cloud_api_switchback":
            write_install_config(cloud_api_switchback_config())
            clear_local_mqtt_candidates()
            seed_switchback_cloud_candidate()
        elif scenario == "maintenance_local_cloud_switchback":
            write_install_config(local_cloud_switchback_config())
            # Both connections of one physical inverter are observed at once, so
            # each direction of the switch has a real proposal to resolve.
            seed_switchback_local_candidates(("192.168.60.10", "SWITCH-ROUTE-B1"))
            seed_switchback_cloud_candidate()
        elif scenario == "maintenance_cloud_local_switchback":
            write_install_config(cloud_local_switchback_config())
            seed_switchback_local_candidates(("192.168.60.10", "SWITCH-ROUTE-B1"))
            seed_switchback_cloud_candidate()
        elif scenario == "maintenance_foreign_inverter_proposal":
            write_install_config(local_broker_switchback_config())
            seed_switchback_local_candidates(
                ("192.168.60.10", "SWITCH-ROUTE-B1"),
                ("192.168.60.11", "SWITCH-ROUTE-OTHER", "SWITCH-SERIAL-OTHER"),
            )
        elif scenario == "serialless_cloud_identity":
            write_install_config(serialless_cloud_config())
            clear_local_mqtt_candidates()
            seed_serialless_cloud_candidate()
        elif scenario == "serialless_cloud_identity_other_scope":
            seed_serialless_cloud_candidate()
            seed_other_scope_local_candidate()
        elif scenario == "serialless_cloud_route_enrichment":
            write_install_config(serialless_cloud_route_enrichment_config())
            clear_local_mqtt_candidates()
            seed_serialized_same_route_candidate()
        elif scenario == "setup_local_api_inverter":
            _seed_local_api_inverter(
                mdns_provider,
                seeded_mdns_devices,
                ip=SEEDED_INVERTER_IP,
                serial=SEEDED_INVERTER_SERIAL,
            )
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
        elif scenario == "setup_transition_hold_commit":
            # Park the next Setup System Build start inside its lifecycle claim,
            # before its transition is committed, so a browser test can prove that
            # exactly one of creation and abandonment wins.
            commit_hold["release"].clear()
            commit_hold["entered"].clear()
            commit_hold["armed"] = True
        elif scenario == "setup_transition_await_commit":
            # Deterministic handshake instead of a delay: this returns only once a
            # request is actually parked in the hold.
            return {
                "ok": True,
                "scenario": scenario,
                "holding": commit_hold["entered"].wait(30),
            }
        elif scenario == "setup_transition_release_commit":
            commit_hold["armed"] = False
            commit_hold["release"].set()
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
        elif scenario == "setup_resource_import_running":
            # A Setup transition whose resource importer really is running: it
            # holds both the durable claim and the operation's coordinator claim,
            # and it stays parked inside the cache mutation until released.
            resource_hold["release"].clear()
            resource_hold["entered"].clear()
            resource_hold["armed"] = True
            started = system_alignment.start(
                requested_tag="v9.9.10",
                mode="fresh_install",
            )
            operation_id = started["operation_id"]
            system_alignment.resume(operation_id=operation_id)
            # Link the operation to the active Guided Setup workflow exactly as
            # the confirm route does, so a browser already inside Fresh Setup can
            # prove ownership of the transition it is shown.
            owner = runtime.setup_workflows.active()
            if owner is not None:
                runtime.setup_workflows.record_transition(
                    owner["workflow_id"],
                    operation_id=operation_id,
                    transition_mode="fresh_install",
                    selected_system_tag="v9.9.10",
                )

            def run_import():
                try:
                    system_alignment.verify_resources(operation_id=operation_id)
                except Exception:
                    # The outcome belongs to the browser assertions; a released
                    # import that can no longer advance must not kill the runtime.
                    pass

            worker = threading.Thread(
                target=run_import, name="e2e-resource-import", daemon=True
            )
            resource_hold["worker"] = worker
            worker.start()
            return {
                "ok": True,
                "scenario": scenario,
                "operation_id": operation_id,
                "holding": resource_hold["entered"].wait(30),
            }
        elif scenario == "setup_transition_expire":
            return {
                "ok": True,
                "scenario": scenario,
                "expired": _expire_transition(system_alignment),
            }
        elif scenario == "setup_resource_import_release":
            return {
                "ok": True,
                "scenario": scenario,
                "settled": _settle_resource_import_hold(resource_hold),
            }
        elif scenario == "system_build_v070_resource_failure":
            _BROKEN_RESOURCE_TAGS.add("v0.7.0")
        elif scenario == "setup_cleanup_pending":
            # A terminal Guided Setup whose cleanup did not converge: the
            # workflow stays the owner of the file it left behind, so a
            # replacement Setup and both Guided Upgrade phases stay blocked and
            # only a retry under this exact id can clear it.
            record = runtime.setup_workflows.ensure_active()
            workflow_id = record["workflow_id"]
            artifacts = SetupWorkflowArtifacts(data_dir, workflow_id=workflow_id)
            target = artifacts.generated_config_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('{"devices": []}\n', encoding="utf-8")
            artifacts.record_generated(
                workflow_id=workflow_id,
                preview_id="pv-" + "0" * 16,
                draft_fingerprint="sha256:" + "0" * 64,
                base_config_revision={
                    "expected_revision": None,
                    "expect_absent": True,
                },
                prepared_config_sha256="0" * 64,
            )
            runtime.setup_workflows.finish(
                workflow_id,
                status="abandoned",
                cleanup={
                    "state": "pending",
                    "attempted_at": utc_now_iso(),
                    "failed_count": 1,
                    "review_count": 0,
                    "artifacts": [
                        {"kind": "generated_config", "status": "failed"}
                    ],
                },
            )
            return {
                "ok": True,
                "scenario": scenario,
                "setup_workflow_id": workflow_id,
            }
        elif scenario == "installed_system_artifacts":
            # What every installed system carries and no Guided Setup workflow
            # owns: the pre-workflow generated config and the deployment marker.
            legacy = Path(data_dir) / "generated" / "config.json"
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text('{"devices": [{"name": "INV_1"}]}\n', encoding="utf-8")
            marker = Path(data_dir) / "state" / ".admin-deployment.json"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                json.dumps({"release": "v0.6.0-rc", "source": "admin_install"}) + "\n",
                encoding="utf-8",
            )
            return {
                "ok": True,
                "scenario": scenario,
                "legacy_generated_config": hashlib.sha256(
                    legacy.read_bytes()
                ).hexdigest(),
                "deployment_marker": hashlib.sha256(marker.read_bytes()).hexdigest(),
            }
        elif scenario == "installed_system_artifact_digests":
            legacy = Path(data_dir) / "generated" / "config.json"
            marker = Path(data_dir) / "state" / ".admin-deployment.json"
            return {
                "ok": True,
                "scenario": scenario,
                "legacy_generated_config": (
                    hashlib.sha256(legacy.read_bytes()).hexdigest()
                    if legacy.is_file()
                    else None
                ),
                "deployment_marker": (
                    hashlib.sha256(marker.read_bytes()).hexdigest()
                    if marker.is_file()
                    else None
                ),
            }
        elif scenario == "setup_cleanup_stranded_review":
            # The record the pre-claim cleanup left behind: terminal, blocking,
            # and blaming installed-system files this workflow never created.
            record = runtime.setup_workflows.ensure_active()
            workflow_id = record["workflow_id"]
            runtime.setup_workflows.finish(
                workflow_id,
                status="abandoned",
                cleanup={
                    "state": "review_required",
                    "attempted_at": utc_now_iso(),
                    "failed_count": 0,
                    "review_count": 2,
                    "artifacts": [
                        {"kind": "legacy_generated_config", "status": "review_required"},
                        {"kind": "deployment_marker", "status": "review_required"},
                    ],
                },
            )
            return {
                "ok": True,
                "scenario": scenario,
                "setup_workflow_id": workflow_id,
            }
        elif scenario == "guided_upgrade_transition":
            # A cancellable Guided Upgrade transition, so a browser test can
            # switch away from it without driving a whole upgrade first.
            started = system_alignment.start(
                requested_tag="v9.9.10",
                mode="guided_upgrade",
            )
            system_alignment.resume(operation_id=started["operation_id"])
            return {
                "ok": True,
                "scenario": scenario,
                "operation_id": started["operation_id"],
            }
        elif scenario == "workflow_state_corrupt":
            # The state an old or crashed Admin can leave behind: a durable
            # workflow record no reader can validate, which every authority read
            # then refuses. Only the advanced release resolves it.
            record_path = runtime.setup_workflows.path
            record_path.parent.mkdir(parents=True, exist_ok=True)
            record_path.write_text("{ not a workflow record", encoding="utf-8")
            return {
                "ok": True,
                "scenario": scenario,
                "digest": hashlib.sha256(record_path.read_bytes()).hexdigest(),
            }
        elif scenario == "workflow_owner_conflict":
            # Two durable records claiming the console at once: a Guided Setup
            # workflow beside a live Guided Upgrade transition.
            started = system_alignment.start(
                requested_tag="v9.9.10",
                mode="guided_upgrade",
            )
            system_alignment.resume(operation_id=started["operation_id"])
            record = runtime.setup_workflows.ensure_active()
            return {
                "ok": True,
                "scenario": scenario,
                "operation_id": started["operation_id"],
                "setup_workflow_id": record["workflow_id"],
            }
        elif scenario == "workflow_replacement_status_unknown":
            # A Docker daemon that cannot answer. Releasing durable state then
            # has to refuse: unreachable is not proof of an idle replacement.
            runtime.workflow_lifecycle.bind_install_state_probe(
                lambda _operation_id: ReplacementActivity.UNKNOWN
            )
        elif scenario == "workflow_replacement_status_inactive":
            runtime.workflow_lifecycle.bind_install_state_probe(
                lambda _operation_id: ReplacementActivity.INACTIVE
            )
        elif scenario == "workflow_recovery_backups":
            # Read back what the advanced release quarantined, so a browser test
            # can assert the backup without reaching into the filesystem itself.
            root = Path(data_dir) / "state" / "workflow-recovery"
            manifests = []
            if root.is_dir():
                for entry in sorted(root.iterdir()):
                    manifest = entry / "recovery-manifest.json"
                    if manifest.is_file():
                        manifests.append(json.loads(manifest.read_text("utf-8")))
            return {"ok": True, "scenario": scenario, "manifests": manifests}
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

        # Before the durable state goes away: a still-parked import would
        # otherwise keep running against a transition that no longer exists.
        _settle_resource_import_hold(resource_hold)
        state_dir = Path(data_dir) / "state"
        for name in (
            "pending-transition.json",
            "known-good-system-build.json",
            "selected-release.json",
            # A leftover Guided Setup record would keep its cleanup state — and
            # therefore its blocking — alive across specs.
            "guided-setup-workflow.json",
            ".admin-deployment.json",
        ):
            try:
                (state_dir / name).unlink()
            except FileNotFoundError:
                pass
        import shutil as _shutil

        _shutil.rmtree(Path(data_dir) / "workflows", ignore_errors=True)
        _shutil.rmtree(Path(data_dir) / "generated", ignore_errors=True)
        # A recovery backup from an earlier spec would otherwise be counted by
        # the next one as its own quarantine.
        _shutil.rmtree(
            Path(data_dir) / "state" / "workflow-recovery", ignore_errors=True
        )
        releases_dir = Path(data_dir) / "releases"
        if releases_dir.exists():
            import shutil

            for child in releases_dir.iterdir():
                shutil.rmtree(child, ignore_errors=True)
        docker.set_running_admin_tag(initial_running_tag)
        docker._running_ems_tag = initial_running_tag
        runtime.workflow_lifecycle.bind_install_state_probe(
            lambda operation_id: admin_replacement_activity(docker, operation_id)
        )
        instance_id_state["value"] = original_instance_id
        backup_failure_state["enabled"] = False
        docker._delay_latest_until_development = False
        docker._latest_validation_waiting.clear()
        docker._development_validation_seen.clear()
        commit_hold["armed"] = False
        commit_hold["release"].set()
        docker.restore_images()
        _BROKEN_RESOURCE_TAGS.clear()
        _BROKEN_RESOURCE_TAGS.add(_BROKEN_RESOURCE_TAG)
        try:
            detect_install_context().config_path.unlink()
        except FileNotFoundError:
            pass
        runtime.zendure_cloud_discovery._trusted_candidates = []
        runtime.zendure_cloud_discovery._candidates = []
        _reset_test_mdns_provider(mdns_provider, seeded_mdns_devices)
        clear_local_mqtt_candidates()
        _restore_initial_bundle(
            Path(data_dir) / "embedded-bundle", initial_running_tag
        )

    runtime.test_reset = test_reset
    return runtime
