# SPDX-License-Identifier: AGPL-3.0-or-later
"""The block-device path against a real loop device.

Its own module because it is a different execution level: it needs root and an
explicit opt-in, and it is skipped everywhere else rather than quietly turning
into a unit test. The deterministic coverage of the same code lives in
test_appliance_ab_blocks.py, driven through the fake backend.

Nothing here touches real storage. The guard in appliance/ab_blocks.py requires
the environment opt-in, root, an explicit allowlist, an unmounted device and a
device that is not system storage, and this module supplies a loop device backed
by a temporary file.
"""

import os
import subprocess

import pytest

from appliance import ab_blocks
from appliance.ab_blocks import RealBlockBackend

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

PAYLOAD = b"appliance-rootfs-image" * 1024


def digest(blob):
    import hashlib

    return "sha256:" + hashlib.sha256(blob).hexdigest()


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "root.img"
    path.write_bytes(PAYLOAD)
    return path


@pytest.mark.skipif(
    os.environ.get("EMS_APPLIANCE_AB_LOOP") != "1" or os.geteuid() != 0,
    reason="the loop-device tier needs root and EMS_APPLIANCE_AB_LOOP=1",
)
def test_a_loop_device_round_trips_through_the_real_backend(tmp_path, source):
    backing = tmp_path / "loop.img"
    backing.write_bytes(b"\x00" * (16 * 1024 * 1024))
    device = subprocess.run(
        ["losetup", "--find", "--show", str(backing)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    try:
        verdict = ab_blocks.ab_block_guard(
            device,
            environ={
                ab_blocks.ENV_OPT_IN: "1",
                ab_blocks.ENV_ALLOWLIST: device,
            },
            mounts={},
        )
        assert verdict.permitted, verdict.reasons
        backend = RealBlockBackend()
        backend.write(device, source, expected_bytes=len(PAYLOAD))
        assert backend.read_digest(device, len(PAYLOAD)) == digest(PAYLOAD)
    finally:
        subprocess.run(["losetup", "--detach", device], check=False)
