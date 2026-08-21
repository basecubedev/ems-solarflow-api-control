# SPDX-License-Identifier: AGPL-3.0-or-later
"""What NetworkManager is allowed to start against.

``/etc/NetworkManager/system-connections`` is one of the seven shared paths bound
from the persistent partition, so it holds the operator's real profiles and the
credentials that make the appliance reachable at all.

Upstream's slot-shared generator guards every bind with
``ConditionPathIsDirectory`` and therefore fails *open*: when the persistent
source is missing the bind is skipped and what stays behind is the empty
slot-local directory the image shipped. NetworkManager starting against that
comes up with no profiles, reports itself healthy, and writes any new profile
into a directory the next slot switch discards.

The image therefore ships a drop-in that makes NetworkManager require the
verification service — the one unit that fails closed. ssh.service already had
the same shape for host identity, for the same reason.
"""

from pathlib import Path

import pytest

from appliance import ab_image, ab_persistence

pytestmark = [pytest.mark.contract, pytest.mark.simulation]

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "packaging/appliance/image/layer/ems-appliance.rootfs-overlay"
NETWORK_DROP_IN = (
    OVERLAY / "etc/systemd/system/NetworkManager.service.d/50-ems-appliance-persistence.conf"
)
SSH_DROP_IN = OVERLAY / "etc/systemd/system/ssh.service.d/50-ems-appliance-host-identity.conf"
PERSISTENCE_UNIT = ROOT / "packaging/appliance/systemd/ems-appliance-persistence.service"


def test_the_network_profiles_are_a_shared_path():
    """If this stops being shared, the drop-in below is answering nothing."""

    shared = {entry.target for entry in ab_persistence.SHARED_PATHS}

    assert "/etc/NetworkManager/system-connections" in shared


def test_network_manager_requires_the_verification_service():
    text = NETWORK_DROP_IN.read_text(encoding="utf-8")

    assert "Requires=ems-appliance-persistence.service" in text
    assert "After=ems-appliance-persistence.service" in text


def test_ordering_alone_would_not_be_fail_closed():
    """After= says when, Requires= says whether. The bind failing open needs both."""

    text = NETWORK_DROP_IN.read_text(encoding="utf-8")
    directives = [line.strip() for line in text.splitlines() if "=" in line and line[0] != "#"]

    assert any(line.startswith("Requires=") for line in directives)


def test_ssh_keeps_the_same_shape_for_host_identity():
    text = SSH_DROP_IN.read_text(encoding="utf-8")

    assert "Requires=ems-appliance-host-identity.service" in text
    assert "After=ems-appliance-host-identity.service" in text


def test_the_verification_service_is_what_actually_fails_closed():
    text = PERSISTENCE_UNIT.read_text(encoding="utf-8")

    assert "ExecStart=/usr/bin/ems-appliance ab verify-persistence" in text
    assert "RequiresMountsFor=/etc/NetworkManager/system-connections" in text


def test_the_image_inspection_requires_both_drop_ins():
    """A drop-in that never reached the image is a drop-in that does nothing."""

    assert set(ab_image.SERVICE_DROP_INS) == {
        "etc/systemd/system/ssh.service.d/50-ems-appliance-host-identity.conf",
        "etc/systemd/system/NetworkManager.service.d/50-ems-appliance-persistence.conf",
    }
    for path, unit in ab_image.SERVICE_DROP_INS.items():
        shipped = OVERLAY / path
        assert shipped.is_file(), path
        assert f"Requires={unit}" in shipped.read_text(encoding="utf-8")


def test_the_drop_in_is_image_only_and_not_in_the_package():
    """A single-slot host installing the .deb has no persistent partition."""

    package_units = ROOT / "packaging/appliance/systemd"
    shipped = [item.name for item in package_units.iterdir()]

    assert "50-ems-appliance-persistence.conf" not in shipped
