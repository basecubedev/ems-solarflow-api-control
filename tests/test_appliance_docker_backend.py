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
