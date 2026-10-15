# SPDX-License-Identifier: AGPL-3.0-or-later
"""The System Build's EMS identity reads the container every other view reads.

Recovery, the known-good record and the upgrade direction name the EMS by the
container they inspect, and so does the check that the running pair runs. The
Maintenance overview, the release listing and the start page resolve that name
through ``resolve_ems_container_name``: the environment first, then the compose
file, then the default.
"""

import pytest

from admin.admin_update import DEFAULT_ADMIN_CONTAINER
from admin.container_names import resolve_ems_container_name
from admin.server import (
    _installed_ems_identity,
    _running_ems_identity,
    _running_pair_healthy,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.system_build,
    pytest.mark.contract,
]

IMAGE_ID = "sha256:" + "c" * 64
EMS_DIGEST = "sha256:" + "d" * 64
BUILD_ID = "v0.8.14-eeeeeee"


class _OneContainerDocker:
    """Docker that knows exactly one EMS container, under one name, beside the Admin."""

    def __init__(self, name):
        self.name = name

    def inspect_container(self, name):
        if name == DEFAULT_ADMIN_CONTAINER:
            return {"container_name": name, "status": "running", "image": "admin"}
        if name != self.name:
            return None
        return {"container_name": name, "status": "running", "image": "ems:v0.8.14"}

    def inspect_container_image_id(self, name):
        return IMAGE_ID if name == self.name else None

    def inspect_image(self, image_ref):
        if image_ref != IMAGE_ID:
            return None
        return {
            "image_ref": image_ref,
            "digest": EMS_DIGEST,
            "labels": {
                "org.opencontainers.image.revision": "e" * 40,
                "de.basecubedev.ems.channel": "stable",
                "de.basecubedev.ems.build_id": BUILD_ID,
                "de.basecubedev.ems.release_tag": "v0.8.14",
            },
        }


def _build_named_by(identity):
    def read(docker):
        result = identity(docker)
        return result.digest, result.build_id

    return read


@pytest.mark.parametrize(
    ("env_name", "compose_name"),
    (
        (None, None),
        (None, "compose-ems"),
        ("env-ems", None),
        ("env-ems", "compose-ems"),
    ),
)
@pytest.mark.parametrize(
    ("read", "expected"),
    (
        (_build_named_by(_running_ems_identity), (EMS_DIGEST, BUILD_ID)),
        (_build_named_by(_installed_ems_identity), (EMS_DIGEST, BUILD_ID)),
        (_running_pair_healthy, True),
    ),
    ids=("running_identity", "installed_identity", "running_pair_healthy"),
)
def test_the_ems_identity_inspects_the_container_the_overview_inspects(
    tmp_path, monkeypatch, env_name, compose_name, read, expected
):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    monkeypatch.delenv("EMS_ADMIN_CONTAINER_NAME", raising=False)
    if env_name is None:
        monkeypatch.delenv("EMS_CONTAINER_NAME", raising=False)
    else:
        monkeypatch.setenv("EMS_CONTAINER_NAME", env_name)
    compose = "services:\n  ems:\n    image: ems\n"
    if compose_name is not None:
        compose += f"    container_name: {compose_name}\n"
    (tmp_path / "docker-compose.yml").write_text(compose)
    overview_name = resolve_ems_container_name(compose_text=compose)

    assert read(_OneContainerDocker(overview_name)) == expected
