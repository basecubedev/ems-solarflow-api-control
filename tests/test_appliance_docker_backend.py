# SPDX-License-Identifier: AGPL-3.0-or-later
"""How the Docker backend reads what Docker answers."""

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.appliance]


def test_a_digest_reference_is_answered_with_the_digest_it_names():
    """An image reachable by an index and a platform digest made a pinned Admin
    verify as image_mismatch: the first same-repository digest was taken."""

    from appliance.docker_backend import _repository_digest

    digests = ["ghcr.io/x/admin@sha256:" + "a" * 64, "ghcr.io/x/admin@sha256:" + "d" * 64]

    assert _repository_digest("ghcr.io/x/admin@sha256:" + "d" * 64, digests) == "sha256:" + "d" * 64
    assert _repository_digest("ghcr.io/x/admin:v1", digests) == "sha256:" + "a" * 64


def test_a_host_without_docker_has_no_container_rather_than_an_error():
    """The runner refuses a tool that is not installed; a strict inspect let
    that through and the whole Admin status section failed with it."""

    from appliance.commands import CommandError
    from appliance.docker_backend import DockerBackend

    class NoDocker:
        def available(self, tool):
            return False

        def run(self, tool, args=(), **kwargs):
            raise CommandError("tool_unavailable", f"{tool} is not installed")

    backend = DockerBackend(NoDocker())

    assert backend.inspect_container("ems-solarflow-admin", strict=True).exists is False
    assert backend.inspect_container("ems-solarflow-admin").exists is False
