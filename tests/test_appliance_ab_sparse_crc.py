# SPDX-License-Identifier: AGPL-3.0-or-later
"""The CRC information an Android Sparse container carries, honoured properly.

The signed manifest's SHA-256 over the expanded image remains the authority for
what a slot ends up holding, and nothing here weakens it. What was missing is
narrower: the parser read the header's ``image_checksum`` field and the payload
of every ``CHUNK_CRC32`` record and then discarded both. A container whose own
self-description contradicted its chunks was expanded without complaint, which
means a build pipeline emitting a broken container had one fewer chance to be
caught before an artefact was signed.

AOSP's ``libsparse`` computes one running CRC-32 over the whole expanded stream
— raw payloads, fill patterns and the zeros a don't-care run stands for — and a
``CHUNK_CRC32`` record asserts that running value at the point it appears. The
header field asserts the final value, or is zero to say "not computed".

The golden vectors are assembled from byte literals in this module and their
expected CRCs come from ``zlib``, so a parser that reimplemented the arithmetic
wrongly cannot agree with itself into a pass.
"""

import binascii
import struct
import zlib

import pytest

from appliance import sparse
from appliance.sparse import SparseError
from tests.helpers import android_sparse

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

BLOCK = 4096


def container(path, chunks, **kwargs):
    path.write_bytes(android_sparse.build(chunks, **kwargs))
    return path


# --- golden vectors, assembled here rather than by any project code ----------


def golden(block_size=512):
    """One raw chunk, one fill chunk and one don't-care run, by hand."""

    raw_payload = bytes(range(256)) * 2
    assert len(raw_payload) == block_size
    fill_value = struct.pack("<I", 0xA5A5A5A5)
    expanded = raw_payload + fill_value * (block_size // 4) + b"\x00" * block_size
    crc = zlib.crc32(expanded) & 0xFFFFFFFF

    body = b"".join(
        (
            struct.pack("<HHII", 0xCAC1, 0, 1, 12 + block_size) + raw_payload,
            struct.pack("<HHII", 0xCAC2, 0, 1, 12 + 4) + fill_value,
            struct.pack("<HHII", 0xCAC3, 0, 1, 12),
        )
    )
    header = struct.pack("<IHHHHIIII", 0xED26FF3A, 1, 0, 28, 12, block_size, 3, 3, crc)
    return header + body, expanded, crc


def test_a_golden_container_expands_to_its_declared_bytes(tmp_path):
    blob, expanded, _crc = golden()
    source = tmp_path / "golden.sparse"
    source.write_bytes(blob)

    report = sparse.expand(source, tmp_path / "golden.img")

    assert (tmp_path / "golden.img").read_bytes() == expanded
    assert report.bytes_written == len(expanded)


def test_a_golden_container_with_a_wrong_header_checksum_is_refused(tmp_path):
    blob, _expanded, crc = golden()
    corrupted = bytearray(blob)
    corrupted[24:28] = struct.pack("<I", (crc + 1) & 0xFFFFFFFF)
    source = tmp_path / "golden.sparse"
    source.write_bytes(bytes(corrupted))

    with pytest.raises(SparseError) as excinfo:
        sparse.expand(source, tmp_path / "golden.img")

    assert excinfo.value.code == "sparse_image_checksum_mismatch"


def test_a_golden_container_with_a_crc_record_is_accepted(tmp_path):
    blob, expanded, crc = golden()
    header = bytearray(blob[:28])
    header[20:24] = struct.pack("<I", 4)
    record = struct.pack("<HHII", 0xCAC4, 0, 0, 12 + 4) + struct.pack("<I", crc)
    source = tmp_path / "golden.sparse"
    source.write_bytes(bytes(header) + blob[28:] + record)

    report = sparse.expand(source, tmp_path / "golden.img")

    assert (tmp_path / "golden.img").read_bytes() == expanded
    assert report.chunks == 4


def test_a_golden_container_with_a_wrong_crc_record_is_refused(tmp_path):
    blob, _expanded, crc = golden()
    header = bytearray(blob[:28])
    header[20:24] = struct.pack("<I", 4)
    record = struct.pack("<HHII", 0xCAC4, 0, 0, 12 + 4) + struct.pack(
        "<I", (crc ^ 0xFFFFFFFF) & 0xFFFFFFFF
    )
    source = tmp_path / "golden.sparse"
    source.write_bytes(bytes(header) + blob[28:] + record)

    with pytest.raises(SparseError) as excinfo:
        sparse.expand(source, tmp_path / "golden.img")

    assert excinfo.value.code == "sparse_crc32_chunk_mismatch"


# --- the header field --------------------------------------------------------


def test_a_zero_image_checksum_means_not_computed(tmp_path):
    chunks = android_sparse.mixed_chunks()
    source = container(tmp_path / "image.sparse", chunks, checksum=0)

    report = sparse.expand(source, tmp_path / "image.img")

    assert report.bytes_written == len(android_sparse.expanded(chunks))


def test_a_non_zero_image_checksum_is_verified(tmp_path):
    chunks = android_sparse.mixed_chunks()
    source = container(tmp_path / "image.sparse", chunks, checksum=0xDEADBEEF)

    with pytest.raises(SparseError) as excinfo:
        sparse.expand(source, tmp_path / "image.img")

    assert excinfo.value.code == "sparse_image_checksum_mismatch"


def test_a_correct_image_checksum_passes(tmp_path):
    chunks = android_sparse.mixed_chunks()
    expected = binascii.crc32(android_sparse.expanded(chunks)) & 0xFFFFFFFF
    source = container(tmp_path / "image.sparse", chunks, checksum=expected)

    report = sparse.expand(source, tmp_path / "image.img")

    assert report.header.image_checksum == expected


def test_the_checksum_covers_the_zeros_a_dont_care_run_stands_for(tmp_path):
    """A don't-care run is unwritten on the medium and still hashed and CRC'd."""

    chunks = [android_sparse.raw(b"x" * BLOCK), android_sparse.dont_care(8)]
    without_tail = binascii.crc32(b"x" * BLOCK) & 0xFFFFFFFF
    source = container(tmp_path / "image.sparse", chunks, checksum=without_tail)

    with pytest.raises(SparseError) as excinfo:
        sparse.expand(source, tmp_path / "image.img")

    assert excinfo.value.code == "sparse_image_checksum_mismatch"


# --- the chunk records -------------------------------------------------------


def test_a_crc_record_asserts_the_running_value_at_its_position(tmp_path):
    head = [android_sparse.raw(b"a" * BLOCK)]
    running = binascii.crc32(android_sparse.expanded(head)) & 0xFFFFFFFF
    chunks = head + [android_sparse.crc32(running), android_sparse.raw(b"b" * BLOCK)]
    source = container(tmp_path / "image.sparse", chunks)

    report = sparse.expand(source, tmp_path / "image.img")

    assert report.bytes_written == 2 * BLOCK


def test_a_crc_record_that_does_not_match_is_refused(tmp_path):
    head = [android_sparse.raw(b"a" * BLOCK)]
    running = binascii.crc32(android_sparse.expanded(head)) & 0xFFFFFFFF
    chunks = head + [android_sparse.crc32(running ^ 0xFF), android_sparse.raw(b"b" * BLOCK)]
    source = container(tmp_path / "image.sparse", chunks)

    with pytest.raises(SparseError) as excinfo:
        sparse.expand(source, tmp_path / "image.img")

    assert excinfo.value.code == "sparse_crc32_chunk_mismatch"


def test_a_trailing_crc_record_is_the_whole_image_checksum(tmp_path):
    body = [android_sparse.raw(b"a" * BLOCK), android_sparse.dont_care(3)]
    total = binascii.crc32(android_sparse.expanded(body)) & 0xFFFFFFFF
    chunks = body + [android_sparse.crc32(total)]
    source = container(tmp_path / "image.sparse", chunks, checksum=total)

    report = sparse.expand(source, tmp_path / "image.img")

    assert report.header.image_checksum == total


def test_the_summary_path_verifies_the_same_crc_information(tmp_path):
    chunks = android_sparse.mixed_chunks()
    source = container(tmp_path / "image.sparse", chunks, checksum=0xDEADBEEF)

    with pytest.raises(SparseError) as excinfo:
        sparse.summarize(source)

    assert excinfo.value.code == "sparse_image_checksum_mismatch"


def test_a_failed_crc_leaves_no_partial_image_behind(tmp_path):
    chunks = android_sparse.mixed_chunks()
    source = container(tmp_path / "image.sparse", chunks, checksum=0xDEADBEEF)
    destination = tmp_path / "image.img"

    with pytest.raises(SparseError):
        sparse.expand(source, destination)

    assert not destination.exists()


# --- what stays authoritative -----------------------------------------------


def test_the_signed_expanded_digest_still_decides(tmp_path):
    chunks = android_sparse.mixed_chunks()
    source = container(tmp_path / "image.sparse", chunks)

    with pytest.raises(SparseError) as excinfo:
        sparse.expand(
            source, tmp_path / "image.img", expected_digest="sha256:" + "0" * 64
        )

    assert excinfo.value.code == "sparse_expanded_digest_mismatch"


def test_an_unsupported_minor_version_with_crc_data_is_refused(tmp_path):
    chunks = android_sparse.mixed_chunks()
    source = container(tmp_path / "image.sparse", chunks, major=2)

    with pytest.raises(SparseError) as excinfo:
        sparse.expand(source, tmp_path / "image.img")

    assert excinfo.value.code == "sparse_version_unsupported"


def test_a_crc_record_carrying_blocks_is_still_malformed(tmp_path):
    chunk = android_sparse.Chunk(android_sparse.CHUNK_CRC32, 1, struct.pack("<I", 0))
    source = container(tmp_path / "image.sparse", [android_sparse.raw(b"a" * BLOCK), chunk])

    with pytest.raises(SparseError) as excinfo:
        sparse.expand(source, tmp_path / "image.img")

    assert excinfo.value.code == "sparse_chunk_invalid"
