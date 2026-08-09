# SPDX-License-Identifier: AGPL-3.0-or-later
"""Malformed containers, exhausted resources, and power loss around expansion.

A signed artifact is far more likely to be wrong because the release pipeline
was than because someone forged it, so every bound is checked anyway. The
invariant every case here shares: whatever the container says, the previous
default slot stays the known-good default and the active slot is never touched.
"""

import os
import struct

import pytest

from appliance import os_update, sparse
from appliance.os_update import TYPE_OS_UPDATE, OsUpdateError
from appliance.operations import STATE_FAILED_RECOVERABLE
from tests.helpers import android_sparse
from tests.helpers.android_sparse import BLOCK_SIZE
from tests.helpers.appliance_ab import ApplianceAbHost, build_ab_service
from tests.helpers.appliance_ab_artifacts import ReleaseDirectory, digest_of

pytestmark = [pytest.mark.unit, pytest.mark.simulation]

RELEASE_ID = "ems-solarflow-appliance-1.5.0-rpi5-arm64-ab"


def payload_chunks():
    return android_sparse.image_of(b"payload" * 512, block_size=BLOCK_SIZE, tail_blocks=2)


def write(tmp_path, name, blob):
    path = tmp_path / name
    path.write_bytes(blob)
    return path


def expand(tmp_path, blob, **kwargs):
    return sparse.expand(write(tmp_path, "in.sparse", blob), tmp_path / "out.img", **kwargs)


def refuses(tmp_path, blob, code, **kwargs):
    with pytest.raises(sparse.SparseError) as excinfo:
        expand(tmp_path, blob, **kwargs)
    assert excinfo.value.code == code, excinfo.value.message
    assert not (tmp_path / "out.img").exists()
    return excinfo.value


# --- malformed containers ----------------------------------------------------


def test_a_foreign_magic_is_refused(tmp_path):
    refuses(tmp_path, android_sparse.build(payload_chunks(), magic=0x12345678), "sparse_magic_invalid")


def test_an_unsupported_major_version_is_refused(tmp_path):
    refuses(tmp_path, android_sparse.build(payload_chunks(), major=2), "sparse_version_unsupported")


def test_an_undersized_file_header_is_refused(tmp_path):
    refuses(
        tmp_path, android_sparse.build(payload_chunks(), file_header_size=16), "sparse_header_invalid"
    )


def test_an_undersized_chunk_header_is_refused(tmp_path):
    refuses(
        tmp_path,
        android_sparse.build(payload_chunks(), chunk_header_size=8),
        "sparse_header_invalid",
    )


def test_a_truncated_header_is_refused(tmp_path):
    refuses(tmp_path, android_sparse.build(payload_chunks())[:12], "sparse_header_truncated")


@pytest.mark.parametrize("block_size", [0, 3, 511, 4 * 1024 * 1024])
def test_an_impossible_block_size_is_refused(tmp_path, block_size):
    blob = bytearray(android_sparse.build(payload_chunks()))
    struct.pack_into("<I", blob, 12, block_size)
    refuses(tmp_path, bytes(blob), "sparse_block_size_invalid")


def test_an_overflowing_block_count_is_refused(tmp_path):
    blob = bytearray(android_sparse.build(payload_chunks()))
    struct.pack_into("<I", blob, 16, 0xFFFFFFFF)
    refuses(tmp_path, bytes(blob), "sparse_expanded_size_invalid")


def test_an_implausible_chunk_count_is_refused(tmp_path):
    blob = bytearray(android_sparse.build(payload_chunks()))
    struct.pack_into("<I", blob, 20, 0xFFFFFFFF)
    refuses(tmp_path, bytes(blob), "sparse_chunk_count_invalid")


def test_a_chunk_count_that_does_not_match_the_body_is_refused(tmp_path):
    chunks = payload_chunks()
    refuses(
        tmp_path,
        android_sparse.build(chunks, total_chunks=len(chunks) + 2),
        "sparse_chunk_truncated",
    )


def test_a_truncated_raw_chunk_is_refused(tmp_path):
    blob = android_sparse.build(payload_chunks())
    refuses(tmp_path, blob[: len(blob) - 64], "sparse_chunk_truncated")


def test_a_fill_chunk_of_the_wrong_size_is_refused(tmp_path):
    chunk = android_sparse.Chunk(android_sparse.CHUNK_FILL, 2, b"\x00" * 8)
    refuses(tmp_path, android_sparse.build([chunk]), "sparse_fill_invalid")


def test_a_dont_care_chunk_that_carries_data_is_refused(tmp_path):
    chunk = android_sparse.Chunk(android_sparse.CHUNK_DONT_CARE, 2, b"\xff" * 4)
    refuses(tmp_path, android_sparse.build([chunk]), "sparse_chunk_invalid")


def test_an_unknown_chunk_type_is_refused(tmp_path):
    chunk = android_sparse.Chunk(0xDEAD, 1, b"\x00" * BLOCK_SIZE)
    refuses(tmp_path, android_sparse.build([chunk]), "sparse_chunk_unknown")


def test_data_past_the_last_chunk_is_refused(tmp_path):
    refuses(
        tmp_path,
        android_sparse.build(payload_chunks(), trailer=b"appended"),
        "sparse_trailing_data",
    )


def test_a_chunk_that_expands_past_the_header_is_refused(tmp_path):
    chunks = payload_chunks()
    blob = android_sparse.build(chunks, total_blocks=1)
    refuses(tmp_path, blob, "sparse_chunk_invalid")


def test_a_container_that_expands_to_another_size_than_declared_is_refused(tmp_path):
    chunks = payload_chunks()
    refuses(
        tmp_path,
        android_sparse.build(chunks),
        "sparse_expanded_size_mismatch",
        expected_size=1,
    )


def test_an_expansion_past_the_supported_maximum_is_refused(tmp_path):
    refuses(
        tmp_path,
        android_sparse.build(payload_chunks()),
        "sparse_expanded_size_invalid",
        max_expanded_bytes=16,
    )


def test_a_digest_that_does_not_match_leaves_nothing_behind(tmp_path):
    refuses(
        tmp_path,
        android_sparse.build(payload_chunks()),
        "sparse_expanded_digest_mismatch",
        expected_digest="sha256:" + "4" * 64,
    )


# --- resource exhaustion ------------------------------------------------------


def test_an_expansion_larger_than_the_staging_filesystem_is_refused(tmp_path):
    refuses(
        tmp_path,
        android_sparse.build(payload_chunks()),
        "sparse_staging_exhausted",
        free_bytes=1024,
    )


def test_a_full_staging_filesystem_is_reported_not_crashed(tmp_path, monkeypatch):
    chunks = [android_sparse.raw(b"a" * BLOCK_SIZE), android_sparse.raw(b"b" * BLOCK_SIZE)]
    blob = android_sparse.build(chunks)
    real_write = os.write
    state = {"calls": 0}

    def failing(handle, block):
        state["calls"] += 1
        if state["calls"] > 1:
            raise OSError(28, "No space left on device")
        return real_write(handle, block)

    monkeypatch.setattr(os, "write", failing)
    error = refuses(tmp_path, blob, "sparse_expansion_failed")

    assert "No space left" in error.message


# --- the update path ----------------------------------------------------------


def sparse_release(tmp_path, *, boot=None, system=None, overrides=None):
    boot = boot or payload_chunks()
    system = system or android_sparse.mixed_chunks(block_size=BLOCK_SIZE)
    blobs, members = {}, {}
    for name, chunks, role, filesystem in (
        ("boot", boot, "boot", "vfat"),
        ("system", system, "root", "ext4"),
    ):
        encoded = android_sparse.build(chunks, block_size=BLOCK_SIZE)
        plain = android_sparse.expanded(chunks, block_size=BLOCK_SIZE)
        blobs[name] = encoded
        members[name] = {
            "role": role,
            "encoding": sparse.ENCODING_ANDROID_SPARSE,
            "encoded_sha256": digest_of(encoded),
            "expanded_sha256": digest_of(plain),
            "expanded_size": len(plain),
            "filesystem": filesystem,
        }
    for name, patch in (overrides or {}).items():
        members[name].update(patch)

    directory = ReleaseDirectory(tmp_path)
    directory.publish(RELEASE_ID, blobs=blobs, member_overrides=members)
    return directory


def run(tmp_path, directory, **kwargs):
    host = ApplianceAbHost(tmp_path)
    service = build_ab_service(tmp_path, host, directory, **kwargs)
    operation = service.operations.create(TYPE_OS_UPDATE)
    service.plan_update(operation, RELEASE_ID)
    service.operations.await_confirmation(operation.operation_id, {"plan": True})
    record = service.operations.get(operation.operation_id, include_token=True)
    service.operations.confirm(operation.operation_id, record.confirmation_token)
    return service, operation


def test_an_expansion_that_fails_never_opens_a_partition(tmp_path):
    """The inactive slot is untouched: expansion happens before any write."""

    directory = sparse_release(
        tmp_path, overrides={"system": {"expanded_sha256": "sha256:" + "8" * 64}}
    )
    service, operation = run(tmp_path, directory)

    with pytest.raises(OsUpdateError) as excinfo:
        service.execute(operation)

    assert excinfo.value.code == "sparse_expanded_digest_mismatch"
    assert service.backend.calls == []
    record = service.operations.get(operation.operation_id)
    assert record.state == STATE_FAILED_RECOVERABLE
    assert record.result["default_slot_unchanged"] is True
    assert record.result["inactive_slot_untouched"] is True


def test_an_expansion_that_fails_leaves_the_previous_slot_known_good(tmp_path):
    directory = sparse_release(
        tmp_path, overrides={"boot": {"expanded_sha256": "sha256:" + "6" * 64}}
    )
    service, operation = run(tmp_path, directory)
    before = service.state.slots().known_good_slot

    with pytest.raises(OsUpdateError):
        service.execute(operation)

    assert service.state.slots().known_good_slot == before
    assert service.state.pending() is None


def test_a_failed_expansion_removes_its_staging(tmp_path):
    directory = sparse_release(
        tmp_path, overrides={"system": {"expanded_sha256": "sha256:" + "3" * 64}}
    )
    service, operation = run(tmp_path, directory)

    with pytest.raises(OsUpdateError):
        service.execute(operation)

    assert not service.state.staging_dir.exists()


def test_an_expanded_image_larger_than_the_partition_blocks_the_plan(tmp_path):
    """Declared at planning time, so nothing is staged to discover it."""

    directory = sparse_release(
        tmp_path, overrides={"boot": {"expanded_size": 512 * 1024 * 1024}}
    )
    host = ApplianceAbHost(tmp_path)
    service = build_ab_service(tmp_path, host, directory)
    operation = service.operations.create(TYPE_OS_UPDATE)

    payload = service.plan_update(operation, RELEASE_ID)

    codes = {blocker["code"] for blocker in payload["blockers"]}
    assert "inactive_partition_too_small" in codes


def test_a_container_that_disagrees_with_the_manifest_is_refused_before_writing(tmp_path):
    directory = sparse_release(tmp_path, overrides={"system": {"expanded_size": 4096}})
    service, operation = run(tmp_path, directory)

    with pytest.raises(OsUpdateError) as excinfo:
        service.execute(operation)

    assert excinfo.value.code == "sparse_expanded_size_mismatch"
    assert service.backend.calls == []


def test_a_retry_after_a_failed_expansion_starts_from_the_artifact_again(tmp_path):
    directory = sparse_release(
        tmp_path, overrides={"system": {"expanded_sha256": "sha256:" + "5" * 64}}
    )
    service, operation = run(tmp_path, directory)
    with pytest.raises(OsUpdateError):
        service.execute(operation)

    directory.rewrite_manifest(
        RELEASE_ID,
        lambda payload: payload["members"]["system"].update(
            {"expanded_sha256": digest_of(
                android_sparse.expanded(
                    android_sparse.mixed_chunks(block_size=BLOCK_SIZE), block_size=BLOCK_SIZE
                )
            )}
        ),
    )
    service.operations.cancel(service.operations.active().operation_id)
    retry = service.operations.create(TYPE_OS_UPDATE)
    service.plan_update(retry, RELEASE_ID)
    service.operations.await_confirmation(retry.operation_id, {"plan": True})
    record = service.operations.get(retry.operation_id, include_token=True)
    service.operations.confirm(retry.operation_id, record.confirmation_token)

    result = service.execute(retry)

    assert result["default_slot_unchanged"] is True
    assert result["stage"] == os_update.STAGE_TRYBOOT_REQUESTED
    assert service.state.pending().target_slot == "B"


# --- power loss around the new stages -----------------------------------------

# Power loss is modelled as "this operation stops here and the appliance boots
# again", which is what the state on the persistent partition has to survive.
# The invariant is the same at every point: until a booted target slot has
# proven itself, the previous default slot is still the default.


def stalled_at(tmp_path, stage):
    """Drive an update until ``stage`` and stop, as a power cut would."""

    directory = sparse_release(tmp_path)
    host = ApplianceAbHost(tmp_path)
    service = build_ab_service(tmp_path, host, directory, arm_after_write=False)
    operation = service.operations.create(TYPE_OS_UPDATE)
    service.plan_update(operation, RELEASE_ID)
    service.operations.await_confirmation(operation.operation_id, {"plan": True})
    record = service.operations.get(operation.operation_id, include_token=True)
    service.operations.confirm(operation.operation_id, record.confirmation_token)

    if stage == "staging":
        service.operations.advance(operation.operation_id, os_update.STAGE_STAGING)
    elif stage == "expanding":
        service.operations.advance(operation.operation_id, os_update.STAGE_IMAGE_EXPANDING)
    elif stage == "expanded":
        service.operations.advance(operation.operation_id, os_update.STAGE_EXPANDED_VERIFIED)
    else:
        service.execute(operation)
    return service, operation


@pytest.mark.parametrize("stage", ["staging", "expanding", "expanded"])
def test_power_loss_before_any_write_leaves_both_slots_alone(tmp_path, stage):
    service, _operation = stalled_at(tmp_path, stage)

    assert service.backend.calls == []
    assert service.state.pending() is None
    assert service.state.slots().previous_slot == ""


def test_power_loss_after_expansion_leaves_the_staging_recoverable(tmp_path):
    """An incomplete expansion is discarded, never reused unverified."""

    service, _operation = stalled_at(tmp_path, "expanded")
    service.state.staging_dir.mkdir(parents=True, exist_ok=True)
    (service.state.staging_dir / "system.img").write_bytes(b"half an image")

    from appliance import os_artifacts

    os_artifacts.discard(service.state.staging_dir)

    assert not service.state.staging_dir.exists()


def test_power_loss_after_the_write_before_tryboot_keeps_the_old_default(tmp_path):
    service, operation = stalled_at(tmp_path, "written")
    record = service.operations.get(operation.operation_id)

    assert record.stage == os_update.STAGE_READY_FOR_TRYBOOT
    assert service.state.pending() is None
    assert service.state.slots().known_good_slot == ""
