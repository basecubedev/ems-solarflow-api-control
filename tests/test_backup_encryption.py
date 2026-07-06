# SPDX-License-Identifier: AGPL-3.0-or-later
"""Streaming AEAD backup encryption (ems.backup_crypto)."""

import base64
import json
import os
import struct

import pytest

from ems import backup_crypto


def _read_header(enc_path):
    with open(enc_path, "rb") as handle:
        assert handle.read(len(backup_crypto.MAGIC)) == backup_crypto.MAGIC
        version = handle.read(1)[0]
        header_len = struct.unpack(">I", handle.read(4))[0]
        header = json.loads(handle.read(header_len).decode("utf-8"))
    return version, header


def _count_chunks(enc_path):
    with open(enc_path, "rb") as handle:
        handle.read(len(backup_crypto.MAGIC) + 1)
        header_len = struct.unpack(">I", handle.read(4))[0]
        handle.read(header_len)
        chunks = 0
        while True:
            flag = handle.read(1)
            if not flag:
                break
            length = struct.unpack(">I", handle.read(4))[0]
            handle.read(length)
            chunks += 1
            if flag == b"\x01":
                break
    return chunks


def _roundtrip(tmp_path, payload, **kwargs):
    plain = tmp_path / "plain.bin"
    plain.write_bytes(payload)
    enc = tmp_path / "plain.bin.enc"
    backup_crypto.encrypt_file(str(plain), str(enc), "pw-correct", **kwargs)
    out = backup_crypto.decrypt_file_to_temp(str(enc), "pw-correct")
    try:
        assert open(out, "rb").read() == payload
    finally:
        os.remove(out)
    return enc


def test_default_is_streaming_version_2(tmp_path):
    enc = _roundtrip(tmp_path, b"hello streaming")
    version, header = _read_header(str(enc))
    assert version == backup_crypto.STREAMING_VERSION
    assert header["algorithm"] == "chacha20-poly1305"
    assert header["kdf"] == "pbkdf2-sha256"
    assert header["iterations"] == backup_crypto.DEFAULT_KDF_ITERATIONS


def test_chacha20_roundtrip(tmp_path):
    _roundtrip(tmp_path, b"a" * 1000, algorithm="chacha20-poly1305")


def test_aes_gcm_roundtrip(tmp_path):
    enc = _roundtrip(tmp_path, b"b" * 1000, algorithm="aes-256-gcm")
    _, header = _read_header(str(enc))
    assert header["algorithm"] == "aes-256-gcm"


def test_wrong_password_fails(tmp_path):
    plain = tmp_path / "p.bin"
    plain.write_bytes(b"secret")
    enc = tmp_path / "p.bin.enc"
    backup_crypto.encrypt_file(str(plain), str(enc), "right")
    with pytest.raises(backup_crypto.BackupPasswordError):
        backup_crypto.decrypt_file_to_temp(str(enc), "wrong")


@pytest.mark.parametrize("path", [None, "", "bad\x00backup.enc", "missing.enc"])
def test_invalid_encrypted_backup_paths_fail_cleanly(path):
    assert backup_crypto.is_encrypted_backup(path) is False
    with pytest.raises(backup_crypto.BackupFormatError):
        backup_crypto.decrypt_file_to_temp(path, "pw")


def test_encrypted_backup_symlink_is_rejected(tmp_path):
    enc = _roundtrip(tmp_path, b"payload")
    link = tmp_path / "linked.enc"
    link.symlink_to(enc)

    assert backup_crypto.is_encrypted_backup(link) is False
    with pytest.raises(backup_crypto.BackupFormatError, match="symlink"):
        backup_crypto.decrypt_file_to_temp(link, "pw")


def test_file_path_validation_confines_to_allowed_root(tmp_path):
    allowed = tmp_path / "backups"
    allowed.mkdir()
    inside = allowed / "inside.enc"
    inside.write_bytes(b"data")
    outside = tmp_path / "outside.enc"
    outside.write_bytes(b"data")

    assert backup_crypto._validated_existing_file_path(
        inside, allowed_root=allowed
    ) == str(inside)
    with pytest.raises(backup_crypto.BackupFormatError, match="outside allowed"):
        backup_crypto._validated_existing_file_path(
            outside, allowed_root=allowed
        )

    link = allowed / "linked.enc"
    link.symlink_to(inside)
    with pytest.raises(backup_crypto.BackupFormatError, match="symlink"):
        backup_crypto._validated_existing_file_path(link, allowed_root=allowed)


def test_encrypted_detection_honors_allowed_root(tmp_path):
    allowed = tmp_path / "backups"
    allowed.mkdir()
    plain = tmp_path / "plain.bin"
    plain.write_bytes(b"payload")
    inside = allowed / "inside.enc"
    outside = tmp_path / "outside.enc"
    backup_crypto.encrypt_file(str(plain), str(inside), "pw")
    backup_crypto.encrypt_file(str(plain), str(outside), "pw")

    assert backup_crypto.is_encrypted_backup(inside, allowed_root=allowed)
    assert not backup_crypto.is_encrypted_backup(outside, allowed_root=allowed)


def test_tampered_chunk_fails(tmp_path):
    plain = tmp_path / "p.bin"
    plain.write_bytes(b"x" * 50)
    enc = tmp_path / "p.bin.enc"
    backup_crypto.encrypt_file(str(plain), str(enc), "pw")
    raw = bytearray(enc.read_bytes())
    raw[-1] ^= 0x01  # flip a ciphertext/tag byte
    enc.write_bytes(raw)
    with pytest.raises(backup_crypto.BackupPasswordError):
        backup_crypto.decrypt_file_to_temp(str(enc), "pw")


def test_truncated_file_fails(tmp_path):
    plain = tmp_path / "p.bin"
    plain.write_bytes(b"y" * 5000)
    enc = tmp_path / "p.bin.enc"
    backup_crypto.encrypt_file(str(plain), str(enc), "pw", chunk_size=4096)
    raw = enc.read_bytes()
    enc.write_bytes(raw[: len(raw) - 20])
    with pytest.raises(
        (backup_crypto.BackupPasswordError, backup_crypto.BackupFormatError)
    ):
        backup_crypto.decrypt_file_to_temp(str(enc), "pw")


def test_reordered_chunks_fail(tmp_path):
    plain = tmp_path / "p.bin"
    # Two equal-sized non-final chunks so swapping keeps the byte layout valid.
    size = backup_crypto.MIN_CHUNK_SIZE
    plain.write_bytes(b"A" * size + b"B" * size + b"C" * 4)
    enc = tmp_path / "p.bin.enc"
    backup_crypto.encrypt_file(str(plain), str(enc), "pw", chunk_size=size)
    assert _count_chunks(str(enc)) == 3

    with open(enc, "rb") as handle:
        prefix_len = len(backup_crypto.MAGIC) + 1
        head = handle.read(prefix_len)
        header_len_raw = handle.read(4)
        header_len = struct.unpack(">I", header_len_raw)[0]
        header = handle.read(header_len)
        records = []
        while True:
            flag = handle.read(1)
            if not flag:
                break
            length = struct.unpack(">I", handle.read(4))[0]
            body = handle.read(length)
            records.append((flag, length, body))
    # Swap the first two (non-final) chunks.
    records[0], records[1] = records[1], records[0]
    with open(enc, "wb") as out:
        out.write(head + header_len_raw + header)
        for flag, length, body in records:
            out.write(flag + struct.pack(">I", length) + body)

    with pytest.raises(backup_crypto.BackupPasswordError):
        backup_crypto.decrypt_file_to_temp(str(enc), "pw")


def test_unsupported_algorithm_in_header_fails(tmp_path):
    plain = tmp_path / "p.bin"
    plain.write_bytes(b"data")
    enc = tmp_path / "p.bin.enc"
    backup_crypto.encrypt_file(str(plain), str(enc), "pw")
    raw = enc.read_bytes()
    header_len = struct.unpack(">I", raw[9:13])[0]
    header = json.loads(raw[13 : 13 + header_len].decode("utf-8"))
    header["algorithm"] = "rot13"
    new_header = json.dumps(header, sort_keys=True).encode("utf-8")
    rebuilt = (
        raw[:9]
        + struct.pack(">I", len(new_header))
        + new_header
        + raw[13 + header_len :]
    )
    enc.write_bytes(rebuilt)
    with pytest.raises(backup_crypto.BackupFormatError):
        backup_crypto.decrypt_file_to_temp(str(enc), "pw")


def test_unsupported_version_fails(tmp_path):
    plain = tmp_path / "p.bin"
    plain.write_bytes(b"data")
    enc = tmp_path / "p.bin.enc"
    backup_crypto.encrypt_file(str(plain), str(enc), "pw")
    raw = bytearray(enc.read_bytes())
    raw[len(backup_crypto.MAGIC)] = 99  # bogus version byte
    enc.write_bytes(raw)
    with pytest.raises(backup_crypto.BackupFormatError):
        backup_crypto.decrypt_file_to_temp(str(enc), "pw")


def test_unsupported_algorithm_on_encrypt_rejected(tmp_path):
    plain = tmp_path / "p.bin"
    plain.write_bytes(b"data")
    enc = tmp_path / "p.bin.enc"
    with pytest.raises(ValueError):
        backup_crypto.encrypt_file(str(plain), str(enc), "pw", algorithm="rot13")


@pytest.mark.parametrize(
    "chunk_size",
    [
        backup_crypto.MIN_CHUNK_SIZE - 1,
        backup_crypto.MAX_CHUNK_SIZE + 1,
        0,
    ],
)
def test_encrypt_file_rejects_out_of_range_chunk_size(tmp_path, chunk_size):
    plain = tmp_path / "p.bin"
    plain.write_bytes(b"data")
    enc = tmp_path / "p.bin.enc"
    with pytest.raises(ValueError, match="chunk_size"):
        backup_crypto.encrypt_file(
            str(plain), str(enc), "pw", chunk_size=chunk_size
        )
    # An out-of-range parameter must never leave a half-written .enc behind.
    assert not enc.exists()


@pytest.mark.parametrize(
    "iterations",
    [
        backup_crypto.MIN_KDF_ITERATIONS - 1,
        backup_crypto.MAX_KDF_ITERATIONS + 1,
    ],
)
def test_encrypt_file_rejects_out_of_range_iterations(tmp_path, iterations):
    plain = tmp_path / "p.bin"
    plain.write_bytes(b"data")
    enc = tmp_path / "p.bin.enc"
    with pytest.raises(ValueError, match="iterations"):
        backup_crypto.encrypt_file(
            str(plain), str(enc), "pw", iterations=iterations
        )
    assert not enc.exists()


def test_legacy_fernet_still_restores(tmp_path):
    plain = tmp_path / "legacy.bin"
    plain.write_bytes(b"legacy payload " * 100)
    enc = tmp_path / "legacy.bin.enc"
    backup_crypto._encrypt_file_legacy_fernet(str(plain), str(enc), "pw")

    version, _ = ((enc.read_bytes()[len(backup_crypto.MAGIC)]), None)
    assert version == backup_crypto.LEGACY_FERNET_VERSION

    out = backup_crypto.decrypt_file_to_temp(str(enc), "pw")
    try:
        assert open(out, "rb").read() == plain.read_bytes()
    finally:
        os.remove(out)

    with pytest.raises(backup_crypto.BackupPasswordError):
        backup_crypto.decrypt_file_to_temp(str(enc), "wrong")


def _split_envelope(enc_path):
    """Return ``(prefix, header_bytes, chunk_bytes)`` of an encrypted file."""

    raw = enc_path.read_bytes()
    header_len = struct.unpack(">I", raw[9:13])[0]
    prefix = raw[:9]
    header = raw[13 : 13 + header_len]
    chunks = raw[13 + header_len :]
    return prefix, header, chunks


def _rewrite_with_header(enc_path, header_obj):
    """Re-encode ``enc_path`` with a replacement header object (or raw bytes)."""

    prefix, _header, chunks = _split_envelope(enc_path)
    if isinstance(header_obj, (bytes, bytearray)):
        new_header = bytes(header_obj)
    else:
        new_header = json.dumps(header_obj, sort_keys=True).encode("utf-8")
    enc_path.write_bytes(
        prefix + struct.pack(">I", len(new_header)) + new_header + chunks
    )


def _make_enc(tmp_path, **kwargs):
    plain = tmp_path / "p.bin"
    plain.write_bytes(b"payload " * 64)
    enc = tmp_path / "p.bin.enc"
    backup_crypto.encrypt_file(str(plain), str(enc), "pw", **kwargs)
    return enc


def _header_of(enc_path):
    _prefix, header, _chunks = _split_envelope(enc_path)
    return json.loads(header.decode("utf-8"))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda h: h.update({"iterations": "300000"}),  # non-integer
        lambda h: h.update({"iterations": True}),  # bool masquerading as int
        lambda h: h.update(
            {"iterations": backup_crypto.MIN_KDF_ITERATIONS - 1}
        ),  # below min
        lambda h: h.update(
            {"iterations": backup_crypto.MAX_KDF_ITERATIONS + 1}
        ),  # above max
        lambda h: h.pop("iterations"),  # missing
        lambda h: h.update({"salt": "!!!notbase64!!!"}),  # invalid base64
        lambda h: h.update({"salt": base64.b64encode(b"short").decode()}),  # wrong len
        lambda h: h.pop("salt"),  # missing
        lambda h: h.update({"base_nonce": base64.b64encode(b"x" * 4).decode()}),
        lambda h: h.pop("base_nonce"),  # missing
        lambda h: h.update({"kdf": "scrypt"}),  # unsupported KDF
        lambda h: h.update({"chunk_size": "4096"}),  # non-integer
        lambda h: h.update(
            {"chunk_size": backup_crypto.MIN_CHUNK_SIZE - 1}
        ),  # below min
        lambda h: h.update(
            {"chunk_size": backup_crypto.MAX_CHUNK_SIZE + 1}
        ),  # above max
        lambda h: h.pop("chunk_size"),  # missing
    ],
)
def test_malformed_header_fields_rejected(tmp_path, mutate):
    enc = _make_enc(tmp_path)
    header = _header_of(enc)
    mutate(header)
    _rewrite_with_header(enc, header)
    with pytest.raises(backup_crypto.BackupFormatError):
        backup_crypto.decrypt_file_to_temp(str(enc), "pw")


def test_empty_header_rejected(tmp_path):
    enc = _make_enc(tmp_path)
    _rewrite_with_header(enc, b"")
    with pytest.raises(backup_crypto.BackupFormatError):
        backup_crypto.decrypt_file_to_temp(str(enc), "pw")


def test_oversized_header_rejected_without_reading_it(tmp_path):
    enc = _make_enc(tmp_path)
    prefix, _header, chunks = _split_envelope(enc)
    bogus_len = backup_crypto.MAX_HEADER_SIZE + 1
    # Declares a huge header but stores no bytes for it: must be rejected by the
    # size bound before any allocation/read is attempted.
    enc.write_bytes(prefix + struct.pack(">I", bogus_len) + chunks)
    with pytest.raises(backup_crypto.BackupFormatError, match="header too large"):
        backup_crypto.decrypt_file_to_temp(str(enc), "pw")


def test_header_not_json_rejected(tmp_path):
    enc = _make_enc(tmp_path)
    _rewrite_with_header(enc, b"not json at all")
    with pytest.raises(backup_crypto.BackupFormatError):
        backup_crypto.decrypt_file_to_temp(str(enc), "pw")


def test_header_not_object_rejected(tmp_path):
    enc = _make_enc(tmp_path)
    _rewrite_with_header(enc, b"[1, 2, 3]")
    with pytest.raises(backup_crypto.BackupFormatError):
        backup_crypto.decrypt_file_to_temp(str(enc), "pw")


def test_invalid_chunk_flag_rejected(tmp_path):
    enc = _make_enc(tmp_path, chunk_size=backup_crypto.MIN_CHUNK_SIZE)
    raw = bytearray(enc.read_bytes())
    _, header, _ = _split_envelope(enc)
    first_chunk = 13 + len(header)
    raw[first_chunk] = 0x05  # neither 0x00 nor 0x01
    enc.write_bytes(raw)
    with pytest.raises(backup_crypto.BackupFormatError, match="chunk flag"):
        backup_crypto.decrypt_file_to_temp(str(enc), "pw")


def test_oversized_chunk_length_rejected(tmp_path):
    enc = _make_enc(tmp_path, chunk_size=backup_crypto.MIN_CHUNK_SIZE)
    _, header, _ = _split_envelope(enc)
    raw = bytearray(enc.read_bytes())
    # Overwrite the first chunk's declared ciphertext length with an absurd value.
    length_pos = 13 + len(header) + 1
    raw[length_pos : length_pos + 4] = struct.pack(">I", 0xFFFFFFFF)
    enc.write_bytes(raw)
    with pytest.raises(backup_crypto.BackupFormatError, match="exceeds maximum"):
        backup_crypto.decrypt_file_to_temp(str(enc), "pw")


def test_malformed_files_write_no_output(tmp_path):
    enc = _make_enc(tmp_path)
    header = _header_of(enc)
    header["chunk_size"] = backup_crypto.MAX_CHUNK_SIZE + 1
    _rewrite_with_header(enc, header)

    before = set(os.listdir(tmp_path))
    with pytest.raises(backup_crypto.BackupFormatError):
        backup_crypto.decrypt_file_to_temp(str(enc), "pw", temp_dir=str(tmp_path))
    # No restored temp output left behind.
    assert set(os.listdir(tmp_path)) == before


def test_large_input_streams_in_multiple_chunks(tmp_path):
    payload = os.urandom(64 * 1024)
    enc = _roundtrip(tmp_path, payload, chunk_size=4096)
    # 64 KiB / 4 KiB == 16 chunks; proves the data was processed in pieces
    # rather than as one whole-file blob.
    assert _count_chunks(str(enc)) == 16


def test_empty_input_roundtrip(tmp_path):
    enc = _roundtrip(tmp_path, b"")
    assert _count_chunks(str(enc)) == 1
