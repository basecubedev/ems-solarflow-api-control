# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build real Android Sparse images, the way genimage does.

``image-rota``'s ``genimage.cfg`` wraps every update payload in an
``android-sparse`` container, so ``update.tar.zst``'s ``boot`` and ``system``
members are sparse files rather than filesystems. Fixtures that pretended
otherwise are what let a writer that copies member bytes straight onto a
partition look correct.

Every image here is a genuine container with a genuine header and genuine chunk
records, so a parser that gets the format wrong fails against these rather than
against a mock of this project's idea of the format.
"""

import binascii
import struct

MAGIC = 0xED26FF3A

CHUNK_RAW = 0xCAC1
CHUNK_FILL = 0xCAC2
CHUNK_DONT_CARE = 0xCAC3
CHUNK_CRC32 = 0xCAC4

FILE_HEADER_SIZE = 28
CHUNK_HEADER_SIZE = 12

BLOCK_SIZE = 4096


class Chunk:
    """One sparse chunk, and the expanded bytes it stands for."""

    def __init__(self, kind, blocks, payload=b""):
        self.kind = kind
        self.blocks = blocks
        self.payload = payload

    def header(self, *, chunk_header_size=CHUNK_HEADER_SIZE):
        total = chunk_header_size + len(self.payload)
        return struct.pack(
            "<HHII", self.kind, 0, self.blocks, total
        )

    def encode(self, **kwargs):
        return self.header(**kwargs) + self.payload

    def expanded(self, block_size=BLOCK_SIZE):
        if self.kind == CHUNK_RAW:
            return self.payload
        if self.kind == CHUNK_FILL:
            return self.payload * (self.blocks * block_size // len(self.payload))
        if self.kind == CHUNK_DONT_CARE:
            return b"\x00" * (self.blocks * block_size)
        return b""


def raw(payload, *, block_size=BLOCK_SIZE):
    if len(payload) % block_size:
        payload = payload + b"\x00" * (block_size - len(payload) % block_size)
    return Chunk(CHUNK_RAW, len(payload) // block_size, payload)


def fill(value, blocks):
    return Chunk(CHUNK_FILL, blocks, struct.pack("<I", value))


def dont_care(blocks):
    return Chunk(CHUNK_DONT_CARE, blocks, b"")


def crc32(value):
    return Chunk(CHUNK_CRC32, 0, struct.pack("<I", value))


def build(
    chunks,
    *,
    block_size=BLOCK_SIZE,
    magic=MAGIC,
    major=1,
    minor=0,
    file_header_size=FILE_HEADER_SIZE,
    chunk_header_size=CHUNK_HEADER_SIZE,
    total_blocks=None,
    total_chunks=None,
    checksum=None,
    trailer=b"",
):
    """A sparse container. Every header field is overridable, so malformed
    inputs are built the same way valid ones are."""

    blocks = sum(chunk.blocks for chunk in chunks)
    if checksum is None:
        checksum = binascii.crc32(expanded(chunks, block_size=block_size)) & 0xFFFFFFFF
    header = struct.pack(
        "<IHHHHIIII",
        magic,
        major,
        minor,
        file_header_size,
        chunk_header_size,
        block_size,
        blocks if total_blocks is None else total_blocks,
        len(chunks) if total_chunks is None else total_chunks,
        checksum,
    )
    body = b"".join(chunk.encode(chunk_header_size=chunk_header_size) for chunk in chunks)
    return header + body + trailer


def expanded(chunks, *, block_size=BLOCK_SIZE):
    return b"".join(chunk.expanded(block_size) for chunk in chunks)


def image_of(payload, *, block_size=BLOCK_SIZE, tail_blocks=0):
    """A sparse container holding ``payload`` and a run of unwritten blocks."""

    chunks = [raw(payload, block_size=block_size)]
    if tail_blocks:
        chunks.append(dont_care(tail_blocks))
    return chunks


def mixed_chunks(*, block_size=BLOCK_SIZE):
    """One of each chunk kind, in the order a real image tends to use them."""

    return [
        raw(b"boot filesystem header" .ljust(block_size, b"\x00"), block_size=block_size),
        fill(0x00000000, 2),
        dont_care(4),
        raw(b"tail" .ljust(block_size, b"\x2a"), block_size=block_size),
    ]
