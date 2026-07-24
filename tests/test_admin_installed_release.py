# SPDX-License-Identifier: AGPL-3.0-or-later
"""Installed EMS release resolution and running-container probe tests."""

import pytest

from admin.container_names import DEFAULT_EMS_CONTAINER, resolve_ems_container_name
from admin.installed_release import (
    PROBE_ABSENT,
    PROBE_RUNNING_IDENTIFIED,
    PROBE_RUNNING_UNIDENTIFIED,
    PROBE_STOPPED,
    PROBE_UNAVAILABLE,
    SOURCE_COMPOSE_IMAGE,
    SOURCE_KNOWN_GOOD,
    SOURCE_LEGACY_COMPOSE,
    SOURCE_RUNNING_CONTAINER,
    SOURCE_RUNNING_CONTAINER_UNKNOWN,
    probe_running_release,
    resolve_installed_release,
    running_image_ref,
)

pytestmark = pytest.mark.simulation

_EMS = "ghcr.io/basecubedev/ems-solarflow-api-control"
_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64
_DIGEST_C = "sha256:" + "c" * 64


def _labeled(digest, *, release_tag=None, version=None, build_id="v0.8.0-f7265fc",
             revision="f" * 40):
    labels = {
        "org.opencontainers.image.revision": revision,
        "de.basecubedev.ems.build_id": build_id,
        "de.basecubedev.ems.channel": "stable",
    }
    if version is not None:
        labels["org.opencontainers.image.version"] = version
    if release_tag is not None:
        labels["de.basecubedev.ems.release_tag"] = release_tag
    return {"image_ref": None, "digest": digest, "labels": labels}


class _Docker:
    def __init__(self, *, container=None, containers=None, image_id=None,
                 images=None, image_id_error=False):
        self._container = container
        self._containers = containers
        self._image_id = image_id
        self._images = images or {}
        self._image_id_error = image_id_error

    def inspect_container(self, name):
        if self._containers is not None:
            found = self._containers.get(name)
            return dict(found) if found else None
        return dict(self._container) if self._container else None

    def inspect_container_image_id(self, _name):
        if self._image_id_error:
            raise RuntimeError("cannot read image id")
        return self._image_id

    def inspect_image(self, ref):
        found = self._images.get(ref)
        return dict(found) if found else None


class _RaisingContainerDocker:
    def inspect_container(self, _name):
        raise RuntimeError("docker daemon unreachable")

    def inspect_image(self, _ref):
        return _labeled(_DIGEST_A, release_tag="v0.8.0", version="v0.8.0")


def _running(image, name=DEFAULT_EMS_CONTAINER, status="running"):
    return {"container_name": name, "image": image, "status": status}


def _compose_ref(digest):
    return f"{_EMS}@{digest}"


# --- running unidentified blocks persisted fallbacks -----------------------


def test_running_unidentified_blocks_compose_and_known_good():
    running_ref = _compose_ref(_DIGEST_B)
    compose_ref = _compose_ref(_DIGEST_A)
    docker = _Docker(
        container=_running(running_ref),
        images={
            running_ref: {"image_ref": running_ref, "digest": _DIGEST_B, "labels": {}},
            compose_ref: _labeled(_DIGEST_A, release_tag="v0.8.0", version="v0.8.0"),
        },
    )
    known_good = {"ems_digest": _DIGEST_A, "system_tag": "v0.8.0", "build_id": "v0.8.0-f7265fc"}

    identity = resolve_installed_release(
        docker=docker, compose_ref=compose_ref, known_good=known_good
    )

    assert identity.tag is None
    assert identity.source == SOURCE_RUNNING_CONTAINER_UNKNOWN


def test_running_image_inspection_failure_keeps_release_unknown():
    running_ref = _compose_ref(_DIGEST_B)
    compose_ref = _compose_ref(_DIGEST_A)
    docker = _Docker(
        container=_running(running_ref),
        images={compose_ref: _labeled(_DIGEST_A, release_tag="v0.8.0", version="v0.8.0")},
    )

    identity = resolve_installed_release(docker=docker, compose_ref=compose_ref)

    assert identity.tag is None
    assert identity.source == SOURCE_RUNNING_CONTAINER_UNKNOWN


# --- compose / known-good fallback allowed when EMS is not running ----------


def test_no_running_container_allows_compose_fallback():
    compose_ref = _compose_ref(_DIGEST_A)
    docker = _Docker(
        container=None,
        images={compose_ref: _labeled(_DIGEST_A, release_tag="v0.8.0", version="v0.8.0")},
    )

    identity = resolve_installed_release(docker=docker, compose_ref=compose_ref)

    assert identity.tag == "v0.8.0"
    assert identity.source == SOURCE_COMPOSE_IMAGE


def test_stopped_container_allows_compose_fallback():
    compose_ref = _compose_ref(_DIGEST_A)
    docker = _Docker(
        container=_running(_compose_ref(_DIGEST_B), status="exited"),
        images={compose_ref: _labeled(_DIGEST_A, release_tag="v0.8.0", version="v0.8.0")},
    )

    identity = resolve_installed_release(docker=docker, compose_ref=compose_ref)

    assert identity.tag == "v0.8.0"
    assert identity.source == SOURCE_COMPOSE_IMAGE


def test_absent_container_allows_known_good_fallback():
    compose_ref = _compose_ref(_DIGEST_A)
    docker = _Docker(container=None, images={})
    known_good = {"ems_digest": _DIGEST_A, "system_tag": "v0.8.0", "build_id": "v0.8.0-f7265fc"}

    identity = resolve_installed_release(
        docker=docker, compose_ref=compose_ref, known_good=known_good
    )

    assert identity.tag == "v0.8.0"
    assert identity.source == SOURCE_KNOWN_GOOD


def test_docker_unavailable_allows_legacy_compose_tag():
    identity = resolve_installed_release(docker=None, compose_ref=f"{_EMS}:v0.8.0")

    assert identity.tag == "v0.8.0"
    assert identity.source == SOURCE_LEGACY_COMPOSE


# --- immutable image id wins over a moved mutable tag ----------------------


def test_immutable_running_image_id_wins_over_moved_tag():
    running_tag_ref = f"{_EMS}:v0.9.0"
    docker = _Docker(
        container=_running(running_tag_ref),
        image_id=_DIGEST_C,
        images={
            _DIGEST_C: _labeled(_DIGEST_C, release_tag="v0.8.0", version="v0.8.0"),
            running_tag_ref: _labeled(_DIGEST_B, release_tag="v0.9.0", version="v0.9.0"),
        },
    )

    identity = resolve_installed_release(docker=docker, compose_ref=_compose_ref(_DIGEST_A))

    assert identity.tag == "v0.8.0"
    assert identity.source == SOURCE_RUNNING_CONTAINER
    assert identity.image_ref == _DIGEST_C


# --- running-container probe states ----------------------------------------


def test_probe_absent():
    probe = probe_running_release(_Docker(container=None), DEFAULT_EMS_CONTAINER)
    assert probe.running is False
    assert probe.status == PROBE_ABSENT
    assert probe.identity is None


def test_probe_stopped():
    docker = _Docker(container=_running(f"{_EMS}:v0.8.0", status="exited"))
    probe = probe_running_release(docker, DEFAULT_EMS_CONTAINER)
    assert probe.running is False
    assert probe.status == PROBE_STOPPED


def test_probe_running_identified():
    running_ref = _compose_ref(_DIGEST_A)
    docker = _Docker(
        container=_running(running_ref),
        images={running_ref: _labeled(_DIGEST_A, release_tag="v0.8.0", version="v0.8.0")},
    )
    probe = probe_running_release(docker, DEFAULT_EMS_CONTAINER)
    assert probe.running is True
    assert probe.status == PROBE_RUNNING_IDENTIFIED
    assert probe.identity.tag == "v0.8.0"


def test_probe_running_unidentified():
    running_ref = _compose_ref(_DIGEST_A)
    docker = _Docker(
        container=_running(running_ref),
        images={running_ref: {"image_ref": running_ref, "digest": _DIGEST_A, "labels": {}}},
    )
    probe = probe_running_release(docker, DEFAULT_EMS_CONTAINER)
    assert probe.running is True
    assert probe.status == PROBE_RUNNING_UNIDENTIFIED
    assert probe.identity is None


def test_probe_unavailable_when_inspect_raises():
    probe = probe_running_release(_RaisingContainerDocker(), DEFAULT_EMS_CONTAINER)
    assert probe.running is False
    assert probe.status == PROBE_UNAVAILABLE


def test_docker_unavailable_never_claims_running_identity():
    identity = resolve_installed_release(
        docker=_RaisingContainerDocker(), compose_ref=f"{_EMS}:v0.8.0"
    )
    assert identity.source != SOURCE_RUNNING_CONTAINER
    assert identity.source != SOURCE_RUNNING_CONTAINER_UNKNOWN


# --- running_image_ref helper ----------------------------------------------


def test_running_image_ref_prefers_immutable_id():
    docker = _Docker(container=_running(f"{_EMS}:v0.9.0"), image_id=_DIGEST_C)
    assert running_image_ref(docker, DEFAULT_EMS_CONTAINER) == _DIGEST_C


def test_running_image_ref_falls_back_to_mutable_when_no_id():
    running_ref = _compose_ref(_DIGEST_A)
    docker = _Docker(container=_running(running_ref), image_id=None)
    assert running_image_ref(docker, DEFAULT_EMS_CONTAINER) == running_ref


def test_running_image_ref_none_when_not_running():
    docker = _Docker(container=_running(f"{_EMS}:v0.8.0", status="exited"))
    assert running_image_ref(docker, DEFAULT_EMS_CONTAINER) is None


# --- shared EMS container-name resolution ----------------------------------


def test_container_name_prefers_explicit_argument(monkeypatch):
    monkeypatch.setenv("EMS_CONTAINER_NAME", "env-ems")
    assert (
        resolve_ems_container_name(
            explicit="explicit-ems",
            compose_text="services:\n  ems:\n    container_name: compose-ems\n",
        )
        == "explicit-ems"
    )


def test_container_name_env_overrides_compose(monkeypatch):
    monkeypatch.setenv("EMS_CONTAINER_NAME", "env-ems")
    assert (
        resolve_ems_container_name(
            compose_text="services:\n  ems:\n    container_name: compose-ems\n"
        )
        == "env-ems"
    )


def test_container_name_from_compose_when_env_absent(monkeypatch):
    monkeypatch.delenv("EMS_CONTAINER_NAME", raising=False)
    compose = (
        "services:\n"
        "  ems:\n    container_name: compose-ems\n"
        "  influxdb:\n    container_name: ems-influxdb\n"
    )
    assert resolve_ems_container_name(compose_text=compose) == "compose-ems"


def test_container_name_falls_back_to_canonical(monkeypatch):
    monkeypatch.delenv("EMS_CONTAINER_NAME", raising=False)
    assert resolve_ems_container_name(compose_text="") == DEFAULT_EMS_CONTAINER
