# SPDX-License-Identifier: AGPL-3.0-or-later
"""Encrypted Zendure token store tests."""

import base64
import json
import stat

import pytest

from admin.secret_store import SecretStoreError, ZendureTokenStore

pytestmark = [
    pytest.mark.admin,
    pytest.mark.integration,
    pytest.mark.simulation,
]

TOKEN = "super-secret-zendure-api-token-value"


def _store(tmp_path):
    return ZendureTokenStore(data_dir=tmp_path)


def test_token_can_be_saved_and_loaded(tmp_path):
    store = _store(tmp_path)
    assert store.token_saved() is False
    result = store.save_token(TOKEN)
    assert result["token_saved"] is True
    assert store.token_saved() is True
    assert store.load_token() == TOKEN


def test_token_file_does_not_contain_plaintext_token(tmp_path):
    store = _store(tmp_path)
    store.save_token(TOKEN)
    raw = store.token_path.read_text(encoding="utf-8")
    assert TOKEN not in raw


def test_delete_removes_token(tmp_path):
    store = _store(tmp_path)
    store.save_token(TOKEN)
    result = store.delete_token()
    assert result["token_saved"] is False
    assert result["removed"] is True
    assert store.token_saved() is False
    assert store.load_token() is None


def test_missing_store_returns_token_saved_false(tmp_path):
    store = _store(tmp_path)
    assert store.token_saved() is False
    assert store.load_token() is None
    settings = store.settings()
    assert settings["token_saved"] is False
    assert "token" not in settings


def test_settings_never_returns_raw_token(tmp_path):
    store = _store(tmp_path)
    store.save_token(TOKEN)
    store.update_metadata(
        last_status="ok", last_device_count=2, last_broker="mqtt.example.invalid:8883"
    )
    settings = store.settings()
    assert settings["token_saved"] is True
    assert settings["last_status"] == "ok"
    assert settings["last_device_count"] == 2
    assert TOKEN not in repr(settings)
    assert "token" not in [k for k in settings if k not in ("token_saved",)]


def test_empty_token_is_rejected(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(SecretStoreError):
        store.save_token("   ")


def test_file_permissions_are_set_best_effort(tmp_path):
    store = _store(tmp_path)
    result = store.save_token(TOKEN)
    assert result["permissions_enforced"] in (True, False)
    if result["permissions_enforced"]:
        mode = stat.S_IMODE(store.token_path.stat().st_mode)
        assert mode == 0o600
        key_mode = stat.S_IMODE(store.key_path.stat().st_mode)
        assert key_mode == 0o600


# --- fail-closed encryption (no silent downgrade to base64) ----------------


def _poison_fernet_encrypt(monkeypatch):
    """Break Fernet.encrypt while leaving construction and decrypt intact."""

    from cryptography.fernet import Fernet

    def _boom(self, data):
        raise RuntimeError("simulated cipher failure")

    monkeypatch.setattr(Fernet, "encrypt", _boom)


def _corrupt_key(store, monkeypatch):
    store.state_dir.mkdir(parents=True, exist_ok=True)
    store.key_path.write_bytes(b"not-a-valid-fernet-key")


def _key_read_fails(store, monkeypatch):
    store.state_dir.mkdir(parents=True, exist_ok=True)
    store.key_path.mkdir()  # a directory cannot be read as key bytes


def _key_creation_fails(store, monkeypatch):
    from cryptography.fernet import Fernet

    def _boom():
        raise RuntimeError("simulated key generation failure")

    monkeypatch.setattr(Fernet, "generate_key", _boom)


def _encrypt_fails(store, monkeypatch):
    _poison_fernet_encrypt(monkeypatch)


@pytest.mark.parametrize(
    "inject",
    [_corrupt_key, _key_read_fails, _key_creation_fails, _encrypt_fails],
    ids=["malformed-key", "key-read-fails", "key-creation-fails", "encrypt-fails"],
)
def test_new_token_write_fails_closed(tmp_path, monkeypatch, inject):
    store = _store(tmp_path)
    inject(store, monkeypatch)
    with pytest.raises(SecretStoreError) as excinfo:
        store.save_token(TOKEN)
    # No token record (or base64 fallback) is written, and no partial temp file
    # is left behind.
    assert not store.token_path.exists()
    assert list(store.state_dir.glob("*.tmp")) == []
    assert TOKEN not in str(excinfo.value)


def test_failed_rotation_preserves_existing_token_byte_identical(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.save_token(TOKEN)
    before = store.token_path.read_bytes()

    _poison_fernet_encrypt(monkeypatch)
    with pytest.raises(SecretStoreError):
        store.save_token("rotated-secret-token-value")

    # The previous record is untouched (byte identical) and still resolvable,
    # and no obfuscated fallback record was written.
    assert store.token_path.read_bytes() == before
    assert list(store.state_dir.glob("*.tmp")) == []
    assert store.load_token() == TOKEN


def test_legacy_unencrypted_token_stays_readable_and_status_reports_it(tmp_path):
    store = _store(tmp_path)
    store.save_token(TOKEN)
    record = json.loads(store.token_path.read_text(encoding="utf-8"))
    assert record["encrypted"] is True

    # An existing historical record with encrypted=False (base64) must remain
    # readable for migration/replacement, and its status must report it as not
    # encrypted.
    legacy_blob = base64.b64encode(TOKEN.encode("utf-8")).decode("ascii")
    record["encrypted"] = False
    record["token"] = legacy_blob
    store.token_path.write_text(json.dumps(record), encoding="utf-8")

    assert store.load_token() == TOKEN
    assert store.settings()["encrypted"] is False
