# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writing to a partition, and every way that goes wrong.

Storage does not fail politely. A write returns success from a cache, a flush
returns success from the same cache, a controller returns EIO halfway through, a
USB enclosure disappears, and a medium reads back something other than what was
written. Each of those has corrupted somebody's update, so each one is a case
here, driven through the fake backend so the destructive path is covered without
a real /dev/mmcblk*.

The guard tests exist because the worst outcome of this file would be a test run
that destroyed the development host's own disk.
"""

import pytest

from appliance import ab_blocks
from appliance.ab_blocks import BlockError, FakeBlockBackend, RealBlockBackend

pytestmark = [pytest.mark.unit, pytest.mark.simulation]

PAYLOAD = b"appliance-rootfs-image" * 1024


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "root.img"
    path.write_bytes(PAYLOAD)
    return path


@pytest.fixture
def fake():
    return FakeBlockBackend(sizes={"/dev/fake3": 1024 * 1024, "/dev/fake5": 1024 * 1024})


def digest(blob):
    import hashlib

    return "sha256:" + hashlib.sha256(blob).hexdigest()


# --- the happy path ----------------------------------------------------------


def test_a_written_partition_reads_back_as_what_was_written(fake, source):
    written = fake.write("/dev/fake5", source, expected_bytes=len(PAYLOAD))

    assert written == digest(PAYLOAD)
    assert fake.read_digest("/dev/fake5", len(PAYLOAD)) == digest(PAYLOAD)


def test_writing_takes_the_device_exclusively(fake, source):
    fake.write("/dev/fake5", source, expected_bytes=len(PAYLOAD))

    assert fake.opened_exclusive == ["/dev/fake5"]


# --- the failure matrix ------------------------------------------------------


def test_a_short_write_is_a_failure_not_a_partial_success(fake, source):
    fake.short_write_after = 100

    with pytest.raises(BlockError) as caught:
        fake.write("/dev/fake5", source, expected_bytes=len(PAYLOAD))

    assert caught.value.code == "block_write_short"


def test_an_eio_halfway_through_is_reported(fake, source):
    fake.fail_write_after = 512

    with pytest.raises(BlockError) as caught:
        fake.write("/dev/fake5", source, expected_bytes=len(PAYLOAD))

    assert caught.value.code == "block_write_failed"


def test_a_flush_that_fails_is_never_treated_as_written(fake, source):
    fake.fail_flush = True

    with pytest.raises(BlockError) as caught:
        fake.write("/dev/fake5", source, expected_bytes=len(PAYLOAD))

    assert caught.value.code == "block_flush_failed"


def test_a_read_back_that_differs_is_caught(fake, source):
    fake.write("/dev/fake5", source, expected_bytes=len(PAYLOAD))
    fake.corrupt_readback = True

    assert fake.read_digest("/dev/fake5", len(PAYLOAD)) != digest(PAYLOAD)


def test_a_device_that_disappears_before_the_read_back_is_an_error(fake, source):
    fake.write("/dev/fake5", source, expected_bytes=len(PAYLOAD))
    fake.disappear_before_readback = True

    with pytest.raises(BlockError) as caught:
        fake.read_digest("/dev/fake5", len(PAYLOAD))

    assert caught.value.code == "block_device_unavailable"


def test_a_busy_device_is_never_written(fake, source):
    fake.mark_busy("/dev/fake5")

    with pytest.raises(BlockError) as caught:
        fake.write("/dev/fake5", source, expected_bytes=len(PAYLOAD))

    assert caught.value.code == "block_device_busy"
    assert "/dev/fake5" not in fake.contents


def test_an_unknown_device_is_never_written(fake, source):
    with pytest.raises(BlockError) as caught:
        fake.write("/dev/nowhere", source, expected_bytes=len(PAYLOAD))

    assert caught.value.code == "block_device_unavailable"


def test_a_read_back_shorter_than_the_image_is_an_error(fake, source):
    fake.short_write_after = 100
    with pytest.raises(BlockError):
        fake.write("/dev/fake5", source, expected_bytes=len(PAYLOAD))

    with pytest.raises(BlockError) as caught:
        fake.read_digest("/dev/fake5", len(PAYLOAD))

    assert caught.value.code == "block_read_short"


def test_a_partition_smaller_than_the_image_is_visible_before_the_write(fake):
    fake.set_size("/dev/fake5", 10)

    assert fake.size("/dev/fake5") < len(PAYLOAD)


# --- the real backend against a plain file -----------------------------------


def test_the_real_backend_writes_flushes_and_reads_back(tmp_path, source):
    """A regular file stands in for the partition; the code path is the same."""

    target = tmp_path / "partition.bin"
    target.write_bytes(b"\x00" * (len(PAYLOAD) + 4096))
    backend = RealBlockBackend()

    written = backend.write(target, source, expected_bytes=len(PAYLOAD))

    assert written == digest(PAYLOAD)
    assert backend.read_digest(target, len(PAYLOAD)) == digest(PAYLOAD)
    assert backend.size(target) >= len(PAYLOAD)


def test_the_real_backend_reports_a_medium_that_ends_early(tmp_path, source):
    target = tmp_path / "small.bin"
    target.write_bytes(b"\x00" * 16)
    backend = RealBlockBackend()
    backend.write(target, source, expected_bytes=len(PAYLOAD))

    with pytest.raises(BlockError) as caught:
        backend.read_digest(target, len(PAYLOAD) * 4)

    assert caught.value.code == "block_read_short"


def test_the_real_backend_refuses_a_device_it_cannot_open(tmp_path, source):
    backend = RealBlockBackend()

    with pytest.raises(BlockError) as caught:
        backend.write(tmp_path / "absent" / "device", source, expected_bytes=len(PAYLOAD))

    assert caught.value.code == "block_device_busy"


# --- the destructive-write guard ---------------------------------------------


def guard(path, **overrides):
    values = {
        "environ": {
            ab_blocks.ENV_OPT_IN: "1",
            ab_blocks.ENV_ALLOWLIST: "/dev/loop9",
        },
        "mounts": {},
        "euid": 0,
    }
    values.update(overrides)
    return ab_blocks.ab_block_guard(path, **values)


def test_every_condition_together_permits_a_destructive_write():
    assert guard("/dev/loop9").permitted is True


def test_without_the_environment_opt_in_nothing_is_permitted():
    verdict = guard("/dev/loop9", environ={ab_blocks.ENV_ALLOWLIST: "/dev/loop9"})

    assert verdict.permitted is False
    assert any("EMS_APPLIANCE_AB_BLOCK_WRITE" in reason for reason in verdict.reasons)


def test_a_non_root_caller_is_never_permitted():
    assert guard("/dev/loop9", euid=1000).permitted is False


def test_a_device_outside_the_allowlist_is_never_permitted():
    verdict = guard("/dev/loop8")

    assert verdict.permitted is False
    assert any("ALLOWLIST" in reason for reason in verdict.reasons)


def test_a_mounted_device_is_never_permitted():
    verdict = guard("/dev/loop9", mounts={"/mnt": {"source": "/dev/loop9"}})

    assert verdict.permitted is False
    assert any("is mounted" in reason for reason in verdict.reasons)


@pytest.mark.parametrize(
    "device", ["/dev/mmcblk0", "/dev/mmcblk0p3", "/dev/nvme0n1", "/dev/sda", "/dev/sda1"]
)
def test_system_storage_is_never_written_merely_because_it_exists(device):
    verdict = guard(
        device,
        environ={ab_blocks.ENV_OPT_IN: "1", ab_blocks.ENV_ALLOWLIST: device},
    )

    assert verdict.permitted is False
    assert verdict.not_system_storage is False


def test_the_guard_reads_only_the_environment_it_was_given():
    """No ambient opt-in: a stale shell variable must not arm a real write."""

    verdict = ab_blocks.ab_block_guard("/dev/loop9", environ={}, mounts={}, euid=0)

    assert verdict.permitted is False


def test_the_verdict_is_json_serialisable():
    import json

    payload = json.loads(json.dumps(guard("/dev/loop9").to_dict()))

    assert payload["permitted"] is True
    assert payload["reasons"] == []
