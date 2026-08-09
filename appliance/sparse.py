# SPDX-License-Identifier: AGPL-3.0-or-later
"""Android Sparse containers, and turning one into the filesystem it describes.

``image-rota`` emits both update payloads through genimage's ``android-sparse``
handler, so a verified ``boot`` or ``system`` member is a chunk table and not a
filesystem. Writing those bytes to a partition produces a slot that matches its
manifest and does not boot, which is why nothing downstream may treat a member
as an image until it has been through here.

The expander is in-process on purpose. A decoder invoked as a subprocess would
be one more executable to install, verify and keep on an allowlist, and its
output size would have to be trusted afterwards; parsing the container here
means every bound is checked before a byte is produced.

Nothing in this module takes a caller-supplied decoder, format or limit from a
request: the paths come from the release staging directory and the bounds from
the signed manifest.
"""

import hashlib
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

MAGIC = 0xED26FF3A
ENCODING_ANDROID_SPARSE = "android_sparse"
ENCODING_RAW = "raw"

CHUNK_RAW = 0xCAC1
CHUNK_FILL = 0xCAC2
CHUNK_DONT_CARE = 0xCAC3
CHUNK_CRC32 = 0xCAC4

FILE_HEADER_SIZE = 28
CHUNK_HEADER_SIZE = 12
SUPPORTED_MAJOR = 1

# A block size that is not a multiple of four is not a sparse image any tool
# produces, and the arithmetic below assumes it.
MIN_BLOCK_SIZE = 512
MAX_BLOCK_SIZE = 1024 * 1024

# Bounds a signed artifact still has to fit inside. The release pipeline is the
# thing most likely to be wrong here, not an attacker, and an updater that
# trusts a header field is one bad build away from filling the persistent
# partition or overrunning a partition it is about to write.
MAX_EXPANDED_BYTES = 16 * 1024 * 1024 * 1024
MAX_CHUNKS = 1 << 22

COPY_CHUNK = 1024 * 1024
ZERO_BLOCK = b"\x00" * COPY_CHUNK


class SparseError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SparseHeader:
    block_size: int
    total_blocks: int
    total_chunks: int
    image_checksum: int
    file_header_size: int = FILE_HEADER_SIZE
    chunk_header_size: int = CHUNK_HEADER_SIZE
    major_version: int = SUPPORTED_MAJOR
    minor_version: int = 0

    @property
    def expanded_size(self):
        return self.block_size * self.total_blocks

    def to_dict(self):
        return {
            "block_size": self.block_size,
            "total_blocks": self.total_blocks,
            "total_chunks": self.total_chunks,
            "expanded_size": self.expanded_size,
            "major_version": self.major_version,
            "minor_version": self.minor_version,
        }


@dataclass(frozen=True)
class ExpansionReport:
    source: str
    destination: str
    bytes_written: int
    digest: str
    chunks: int
    header: SparseHeader = None

    def to_dict(self):
        return {
            "source": self.source,
            "destination": self.destination,
            "bytes_written": self.bytes_written,
            "digest": self.digest,
            "chunks": self.chunks,
            "header": self.header.to_dict() if self.header else {},
        }


def is_sparse(path):
    """Does this file start with the Android Sparse magic?"""

    try:
        with open(str(path), "rb") as handle:
            prefix = handle.read(4)
    except OSError:
        return False
    return len(prefix) == 4 and struct.unpack("<I", prefix)[0] == MAGIC


def read_header(path):
    try:
        with open(str(path), "rb") as handle:
            return parse_header(handle.read(FILE_HEADER_SIZE))
    except OSError as exc:
        raise SparseError("sparse_unreadable", f"{path} could not be read: {exc}")


def parse_header(blob, *, max_expanded_bytes=MAX_EXPANDED_BYTES):
    """Validate a file header before anything is allocated from its fields."""

    if len(blob) < FILE_HEADER_SIZE:
        raise SparseError("sparse_header_truncated", "the sparse header is incomplete")
    (
        magic,
        major,
        minor,
        file_header_size,
        chunk_header_size,
        block_size,
        total_blocks,
        total_chunks,
        checksum,
    ) = struct.unpack("<IHHHHIIII", blob[:FILE_HEADER_SIZE])

    if magic != MAGIC:
        raise SparseError("sparse_magic_invalid", "this is not an Android Sparse image")
    if major != SUPPORTED_MAJOR:
        raise SparseError(
            "sparse_version_unsupported",
            f"sparse major version {major} is not one this appliance reads",
        )
    if file_header_size < FILE_HEADER_SIZE:
        raise SparseError(
            "sparse_header_invalid",
            f"the file header declares {file_header_size} bytes, the format has {FILE_HEADER_SIZE}",
        )
    if chunk_header_size < CHUNK_HEADER_SIZE:
        raise SparseError(
            "sparse_header_invalid",
            f"the chunk header declares {chunk_header_size} bytes, the format has "
            f"{CHUNK_HEADER_SIZE}",
        )
    if block_size < MIN_BLOCK_SIZE or block_size > MAX_BLOCK_SIZE or block_size % 4:
        raise SparseError(
            "sparse_block_size_invalid", f"{block_size} is not a usable sparse block size"
        )
    if total_chunks > MAX_CHUNKS:
        raise SparseError(
            "sparse_chunk_count_invalid",
            f"{total_chunks} chunks exceeds the {MAX_CHUNKS} this appliance reads",
        )
    if total_blocks and block_size > max_expanded_bytes // total_blocks:
        raise SparseError(
            "sparse_expanded_size_invalid",
            f"the image expands to {block_size * total_blocks} bytes, past the "
            f"{max_expanded_bytes}-byte limit",
        )
    return SparseHeader(
        block_size=block_size,
        total_blocks=total_blocks,
        total_chunks=total_chunks,
        image_checksum=checksum,
        file_header_size=file_header_size,
        chunk_header_size=chunk_header_size,
        major_version=major,
        minor_version=minor,
    )


def inspect(path, *, expected_size=None, max_expanded_bytes=MAX_EXPANDED_BYTES):
    """Read a container's header and check it against what was promised."""

    header = read_header(path)
    if header.block_size * header.total_blocks > max_expanded_bytes:
        raise SparseError(
            "sparse_expanded_size_invalid",
            f"the image expands to {header.expanded_size} bytes, past the "
            f"{max_expanded_bytes}-byte limit",
        )
    if expected_size is not None and header.expanded_size != int(expected_size):
        raise SparseError(
            "sparse_expanded_size_mismatch",
            f"the container expands to {header.expanded_size} bytes, the manifest "
            f"declares {expected_size}",
        )
    return header


class _FileSink:
    """Where expanded bytes go. Unwritten runs are seeked, never written."""

    def __init__(self, handle):
        self.handle = handle

    def write(self, block):
        os.write(self.handle, block)

    def skip(self, count):
        os.lseek(self.handle, count, os.SEEK_CUR)


class _NullSink:
    """Hash an expansion without producing it, for the release pipeline."""

    def write(self, block):
        return None

    def skip(self, count):
        return None


def summarize(source, *, max_expanded_bytes=MAX_EXPANDED_BYTES):
    """The size and digest a container expands to, without writing it.

    The release pipeline needs both to describe a member; a builder that had to
    materialise a multi-gigabyte image to hash it would be describing a copy.
    """

    return _drive(Path(source), _NullSink(), max_expanded_bytes=max_expanded_bytes)


class _Running:
    """The SHA-256 and the CRC-32 of everything a container expands to.

    ``libsparse`` keeps one running CRC over the whole expanded stream — raw
    payloads, fill patterns and the zeros a don't-care run stands for. A
    ``CHUNK_CRC32`` record asserts that running value where it appears, and the
    header's ``image_checksum`` asserts the final one.
    """

    def __init__(self):
        self.digest = hashlib.sha256()
        self.crc = 0

    def update(self, block):
        self.digest.update(block)
        self.crc = zlib.crc32(block, self.crc) & 0xFFFFFFFF

    def hexdigest(self):
        return self.digest.hexdigest()


def _drive(source_path, sink, *, expected_size=None, max_expanded_bytes=MAX_EXPANDED_BYTES):
    header = inspect(
        source_path, expected_size=expected_size, max_expanded_bytes=max_expanded_bytes
    )
    digest = _Running()
    written = 0
    chunks = 0
    with open(str(source_path), "rb") as reader:
        reader.seek(header.file_header_size)
        for _ in range(header.total_chunks):
            written += _expand_chunk(reader, sink, header, digest, written)
            chunks += 1
        if reader.read(1):
            raise SparseError(
                "sparse_trailing_data",
                "the container carries data past its last declared chunk",
            )
    if written != header.expanded_size:
        raise SparseError(
            "sparse_expanded_size_mismatch",
            f"the chunks produced {written} bytes, the header declares "
            f"{header.expanded_size}",
        )
    # A zero image_checksum is the format's way of saying "not computed"; any
    # other value is a claim the container makes about itself.
    if header.image_checksum and header.image_checksum != digest.crc:
        raise SparseError(
            "sparse_image_checksum_mismatch",
            f"the container expands to CRC32 {digest.crc:#010x}, its header declares "
            f"{header.image_checksum:#010x}",
        )
    return ExpansionReport(
        source=str(source_path),
        destination="",
        bytes_written=written,
        digest=f"sha256:{digest.hexdigest()}",
        chunks=chunks,
        header=header,
    )


def expand(
    source,
    destination,
    *,
    expected_size=None,
    expected_digest=None,
    max_expanded_bytes=MAX_EXPANDED_BYTES,
    free_bytes=None,
):
    """Write the filesystem a container describes, and prove what was written.

    The output is never left behind on failure: a half-expanded image that
    survived would be indistinguishable from a good one at the next retry.
    """

    source_path = Path(source)
    target = Path(destination)
    header = inspect(
        source_path, expected_size=expected_size, max_expanded_bytes=max_expanded_bytes
    )
    if free_bytes is not None and header.expanded_size > int(free_bytes):
        raise SparseError(
            "sparse_staging_exhausted",
            f"expanding needs {header.expanded_size} bytes, the staging filesystem has "
            f"{int(free_bytes)}",
        )

    handle = None
    try:
        handle = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        report = _drive(
            source_path,
            _FileSink(handle),
            expected_size=expected_size,
            max_expanded_bytes=max_expanded_bytes,
        )
        os.ftruncate(handle, report.bytes_written)
        os.fsync(handle)
    except OSError as exc:
        _discard(handle, target)
        raise SparseError("sparse_expansion_failed", f"the image could not be expanded: {exc}")
    except SparseError:
        _discard(handle, target)
        raise
    os.close(handle)

    if expected_digest and report.digest != expected_digest:
        target.unlink(missing_ok=True)
        raise SparseError(
            "sparse_expanded_digest_mismatch",
            f"the expanded image hashes to {report.digest}, the manifest declares "
            f"{expected_digest}",
        )
    return ExpansionReport(
        source=report.source,
        destination=str(target),
        bytes_written=report.bytes_written,
        digest=report.digest,
        chunks=report.chunks,
        header=report.header,
    )


def _discard(handle, target):
    if handle is not None:
        try:
            os.close(handle)
        except OSError:
            pass
    try:
        Path(target).unlink(missing_ok=True)
    except OSError:
        pass


def _expand_chunk(reader, sink, header, digest, written):
    raw = reader.read(header.chunk_header_size)
    if len(raw) < header.chunk_header_size:
        raise SparseError(
            "sparse_chunk_truncated", "a chunk header ends before the container does"
        )
    kind, _reserved, blocks, total = struct.unpack("<HHII", raw[:CHUNK_HEADER_SIZE])
    if total < header.chunk_header_size:
        raise SparseError(
            "sparse_chunk_invalid", f"a chunk declares {total} bytes, less than its header"
        )
    payload_size = total - header.chunk_header_size
    span = blocks * header.block_size
    if blocks and header.block_size > (MAX_EXPANDED_BYTES - written) // blocks:
        raise SparseError(
            "sparse_expanded_size_invalid", "the chunks expand past the supported maximum"
        )
    if written + span > header.expanded_size:
        raise SparseError(
            "sparse_chunk_invalid",
            "a chunk expands past the size the header declares",
        )

    if kind == CHUNK_RAW:
        if payload_size != span:
            raise SparseError(
                "sparse_chunk_invalid",
                f"a raw chunk carries {payload_size} bytes for {span} bytes of blocks",
            )
        return _copy(reader, sink, digest, span, truncated="sparse_chunk_truncated")
    if kind == CHUNK_FILL:
        if payload_size != 4:
            raise SparseError(
                "sparse_fill_invalid", f"a fill chunk carries {payload_size} bytes, not 4"
            )
        value = reader.read(4)
        if len(value) != 4:
            raise SparseError("sparse_chunk_truncated", "a fill chunk ends early")
        return _emit(sink, digest, value * (COPY_CHUNK // 4), span)
    if kind == CHUNK_DONT_CARE:
        if payload_size:
            raise SparseError(
                "sparse_chunk_invalid", "a don't-care chunk carries data it must not"
            )
        return _emit(sink, digest, ZERO_BLOCK, span, sparse_hole=True)
    if kind == CHUNK_CRC32:
        if payload_size != 4 or blocks:
            raise SparseError("sparse_chunk_invalid", "a crc32 chunk is malformed")
        value = reader.read(4)
        if len(value) != 4:
            raise SparseError("sparse_chunk_truncated", "a crc32 chunk ends early")
        declared = struct.unpack("<I", value)[0]
        if declared != digest.crc:
            raise SparseError(
                "sparse_crc32_chunk_mismatch",
                f"a crc32 record declares {declared:#010x}, the chunks before it expand "
                f"to {digest.crc:#010x}",
            )
        return 0
    raise SparseError("sparse_chunk_unknown", f"chunk type 0x{kind:04x} is not one of the four")


def _copy(reader, sink, digest, span, *, truncated):
    remaining = span
    while remaining:
        block = reader.read(min(COPY_CHUNK, remaining))
        if not block:
            raise SparseError(truncated, "a chunk ends before the bytes it declared")
        sink.write(block)
        digest.update(block)
        remaining -= len(block)
    return span


def _emit(sink, digest, pattern, span, *, sparse_hole=False):
    """Produce ``span`` bytes of a repeating pattern, hashing all of them.

    Unwritten runs are seeked over rather than written, so a mostly-empty image
    costs its real size on the medium and not its declared one. The digest still
    covers the zeros, because that is what the partition will read back.
    """

    remaining = span
    if sparse_hole:
        sink.skip(span)
        while remaining:
            step = min(len(pattern), remaining)
            digest.update(pattern[:step])
            remaining -= step
        return span
    while remaining:
        step = min(len(pattern), remaining)
        sink.write(pattern[:step])
        digest.update(pattern[:step])
        remaining -= step
    return span
