# SPDX-License-Identifier: AGPL-3.0-or-later
"""Android Sparse update members, and what must never reach a partition.

``image-rota``'s genimage configuration wraps every update payload in an
``android-sparse`` container and its ``post-image.sh`` packs those containers as
the ``boot`` and ``system`` members of ``update.tar.zst``. A writer that copies
a verified member's bytes onto a partition therefore writes the container, not
the filesystem — a slot that reads back byte-perfect and does not boot.

These tests build genuine containers with genuine chunk records, so what is
under test is the format rather than a model of it.
"""

import hashlib
import json
import struct

import pytest

from appliance import os_update, sparse
from appliance.os_update import TYPE_OS_UPDATE
from tests.helpers import android_sparse
from tests.helpers.android_sparse import BLOCK_SIZE
from tests.helpers.appliance_ab import ApplianceAbHost, build_ab_service
from tests.helpers.appliance_ab_artifacts import ReleaseDirectory, digest_of

pytestmark = [pytest.mark.unit, pytest.mark.simulation]

RELEASE_ID = "ems-solarflow-appliance-1.5.0-rpi5-arm64-ab"


def boot_chunks():
    return android_sparse.image_of(b"BOOT" * 256, block_size=BLOCK_SIZE, tail_blocks=3)


def system_chunks():
    return android_sparse.mixed_chunks(block_size=BLOCK_SIZE)


def sparse_pair():
    """The two members an image-rota update artifact actually carries."""

    members = {}
    for name, chunks in (("boot", boot_chunks()), ("system", system_chunks())):
        encoded = android_sparse.build(chunks, block_size=BLOCK_SIZE)
        plain = android_sparse.expanded(chunks, block_size=BLOCK_SIZE)
        members[name] = {"encoded": encoded, "expanded": plain}
    return members


def sparse_release(tmp_path, *, members=None, role_of=None):
    members = members or sparse_pair()
    role_of = role_of or {"boot": "boot", "system": "root"}
    directory = ReleaseDirectory(tmp_path)
    directory.publish(
        RELEASE_ID,
        blobs={name: entry["encoded"] for name, entry in members.items()},
        member_overrides={
            name: {
                "role": role_of[name],
                "encoding": sparse.ENCODING_ANDROID_SPARSE,
                "encoded_sha256": digest_of(entry["encoded"]),
                "expanded_sha256": digest_of(entry["expanded"]),
                "expanded_size": len(entry["expanded"]),
                "filesystem": "vfat" if name == "boot" else "ext4",
            }
            for name, entry in members.items()
        },
        manifest_overrides={"format_version": 2},
    )
    return directory, members


def run_update(tmp_path, host, directory):
    service = build_ab_service(tmp_path, host, directory)
    operation = service.operations.create(TYPE_OS_UPDATE)
    service.plan_update(operation, RELEASE_ID)
    service.operations.await_confirmation(operation.operation_id, {"plan": True})
    record = service.operations.get(operation.operation_id, include_token=True)
    service.operations.confirm(operation.operation_id, record.confirmation_token)
    return service, service.execute(operation)


# --- the regression ---------------------------------------------------------


def test_a_verified_sparse_member_is_never_written_to_a_partition(tmp_path):
    """The container's magic must not appear on the medium. Ever."""

    host = ApplianceAbHost(tmp_path)
    directory, members = sparse_release(tmp_path)
    service, result = run_update(tmp_path, host, directory)

    for role, name in (("boot_device", "boot"), ("root_device", "system")):
        written = service.backend.contents[result[role]]
        assert not written.startswith(struct.pack("<I", android_sparse.MAGIC)), role
        assert written == members[name]["expanded"], role


def test_the_written_slot_carries_the_expanded_digest(tmp_path):
    host = ApplianceAbHost(tmp_path)
    directory, members = sparse_release(tmp_path)
    _service, result = run_update(tmp_path, host, directory)

    assert result["boot_digest"] == digest_of(members["boot"]["expanded"])
    assert result["rootfs_digest"] == digest_of(members["system"]["expanded"])


def test_the_encoded_and_expanded_digests_are_distinct(tmp_path):
    _directory, members = sparse_release(tmp_path)

    for entry in members.values():
        assert digest_of(entry["encoded"]) != digest_of(entry["expanded"])


# --- reading the container --------------------------------------------------


def test_a_real_container_is_recognised(tmp_path):
    path = tmp_path / "system.sparse"
    path.write_bytes(android_sparse.build(system_chunks()))

    assert sparse.is_sparse(path)
    header = sparse.read_header(path)
    assert header.block_size == BLOCK_SIZE
    assert header.expanded_size == len(android_sparse.expanded(system_chunks()))


def test_a_filesystem_image_is_not_mistaken_for_a_container(tmp_path):
    path = tmp_path / "boot.vfat"
    path.write_bytes(b"\xeb\x3c\x90MSDOS5.0" + b"\x00" * 4096)

    assert not sparse.is_sparse(path)


def test_every_chunk_kind_expands_to_the_bytes_it_stands_for(tmp_path):
    chunks = [
        android_sparse.raw(b"A" * BLOCK_SIZE),
        android_sparse.fill(0x0000FFFF, 2),
        android_sparse.dont_care(3),
        android_sparse.crc32(0),
        android_sparse.raw(b"Z" * BLOCK_SIZE),
    ]
    source = tmp_path / "mixed.sparse"
    source.write_bytes(android_sparse.build(chunks))
    target = tmp_path / "mixed.img"

    plain = android_sparse.expanded(chunks)
    report = sparse.expand(source, target, expected_size=len(plain))

    assert target.read_bytes() == plain
    assert report.digest == digest_of(plain)
    assert report.bytes_written == len(plain)


def test_expansion_verifies_the_declared_digest(tmp_path):
    chunks = boot_chunks()
    source = tmp_path / "boot.sparse"
    source.write_bytes(android_sparse.build(chunks))
    plain = android_sparse.expanded(chunks)

    with pytest.raises(sparse.SparseError) as excinfo:
        sparse.expand(
            source,
            tmp_path / "boot.img",
            expected_size=len(plain),
            expected_digest="sha256:" + "0" * 64,
        )

    assert excinfo.value.code == "sparse_expanded_digest_mismatch"


def test_a_failed_expansion_leaves_no_output_behind(tmp_path):
    chunks = boot_chunks()
    source = tmp_path / "boot.sparse"
    source.write_bytes(android_sparse.build(chunks))
    target = tmp_path / "boot.img"

    with pytest.raises(sparse.SparseError):
        sparse.expand(source, target, expected_size=1)

    assert not target.exists()


# --- the wire format ---------------------------------------------------------

# Written out byte by byte from AOSP's sparse_format.h rather than through this
# project's own encoder, so the two cannot drift together: 28-byte file header,
# 12-byte chunk header, little-endian throughout.
GOLDEN = bytes.fromhex(
    "3aff26ed"  # magic     0xED26FF3A
    "0100"      # major     1
    "0000"      # minor     0
    "1c00"      # file_hdr_sz   28
    "0c00"      # chunk_hdr_sz  12
    "00020000"  # blk_sz        512
    "03000000"  # total_blks    3
    "02000000"  # total_chunks  2
    "00000000"  # image_checksum
    "c1ca"      # CHUNK_TYPE_RAW
    "0000"      # reserved
    "01000000"  # chunk_sz      1 block
    "0c020000"  # total_sz      12 + 512
) + b"R" * 512 + bytes.fromhex(
    "c2ca"      # CHUNK_TYPE_FILL
    "0000"
    "02000000"  # chunk_sz      2 blocks
    "10000000"  # total_sz      12 + 4
    "efbeadde"  # fill value
)


def test_the_parser_reads_the_documented_wire_format(tmp_path):
    path = tmp_path / "golden.sparse"
    path.write_bytes(GOLDEN)

    header = sparse.read_header(path)

    assert header.block_size == 512
    assert header.total_blocks == 3
    assert header.total_chunks == 2
    assert header.expanded_size == 1536


def test_the_expander_reproduces_the_documented_wire_format(tmp_path):
    source = tmp_path / "golden.sparse"
    source.write_bytes(GOLDEN)
    target = tmp_path / "golden.img"

    sparse.expand(source, target, expected_size=1536)

    assert target.read_bytes() == b"R" * 512 + bytes.fromhex("efbeadde") * 256


# --- the manifest authority --------------------------------------------------


def test_a_manifest_without_expanded_authority_is_refused(tmp_path):
    from appliance import os_releases

    directory = ReleaseDirectory(tmp_path)
    directory.publish(
        RELEASE_ID,
        blobs={"boot": b"x" * 16, "system": b"y" * 16},
        member_overrides={
            "boot": {"digest": digest_of(b"x" * 16), "role": "boot"},
            "system": {"digest": digest_of(b"y" * 16), "role": "root"},
        },
        manifest_overrides={"format_version": 2},
    )

    with pytest.raises(os_releases.ReleaseError) as excinfo:
        directory.catalogue().get(RELEASE_ID)

    assert excinfo.value.code == "release_manifest_invalid"


def test_a_format_one_manifest_is_refused_for_installation(tmp_path):
    """Old records stay readable for diagnostics; they are not installable."""

    from appliance import os_releases

    directory = ReleaseDirectory(tmp_path)
    directory.publish(RELEASE_ID)
    directory.rewrite_manifest(
        RELEASE_ID,
        lambda payload: payload.update(
            {
                "format_version": 1,
                "members": {
                    "boot": {"digest": payload["members"]["boot"]["encoded_sha256"], "role": "boot"},
                    "system": {
                        "digest": payload["members"]["system"]["encoded_sha256"],
                        "role": "root",
                    },
                },
            }
        ),
    )

    with pytest.raises(os_releases.ReleaseError) as excinfo:
        directory.catalogue().get(RELEASE_ID)

    assert excinfo.value.code == "release_manifest_unsupported"
    assert "cannot be inferred" in excinfo.value.message


def test_the_operation_authority_binds_both_identities(tmp_path):
    host = ApplianceAbHost(tmp_path)
    directory, members = sparse_release(tmp_path)
    service = build_ab_service(tmp_path, host, directory)
    operation = service.operations.create(TYPE_OS_UPDATE)
    payload = service.plan_update(operation, RELEASE_ID)

    authority = payload[os_update.AUTHORITY_FIELD]
    assert authority["boot_digest"] == digest_of(members["boot"]["encoded"])
    assert authority["boot_expanded_digest"] == digest_of(members["boot"]["expanded"])
    assert authority["rootfs_digest"] == digest_of(members["system"]["encoded"])
    assert authority["rootfs_expanded_digest"] == digest_of(members["system"]["expanded"])
    assert authority["boot_expanded_size"] == len(members["boot"]["expanded"])
    assert authority["rootfs_expanded_size"] == len(members["system"]["expanded"])
    assert authority["hardware_profile"]


def test_changing_an_expanded_identity_invalidates_the_plan(tmp_path):
    host = ApplianceAbHost(tmp_path)
    directory, _members = sparse_release(tmp_path)
    service = build_ab_service(tmp_path, host, directory)
    operation = service.operations.create(TYPE_OS_UPDATE)
    first = service.plan_update(operation, RELEASE_ID)

    directory.rewrite_manifest(
        RELEASE_ID,
        lambda payload: payload["members"]["system"].update(
            {"expanded_sha256": "sha256:" + "1" * 64}
        ),
    )
    second = service.plan_update(operation, RELEASE_ID)

    assert first[os_update.AUTHORITY_FIELD] != second[os_update.AUTHORITY_FIELD]


def digest_bytes(blob):
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def test_the_release_manifest_json_names_the_encoding(tmp_path):
    directory, _members = sparse_release(tmp_path)
    payload = json.loads(
        (directory.root / f"{RELEASE_ID}.manifest.json").read_text(encoding="utf-8")
    )

    for entry in payload["members"].values():
        assert entry["encoding"] == "android_sparse"
        assert entry["encoded_sha256"] != entry["expanded_sha256"]
