# SPDX-License-Identifier: AGPL-3.0-or-later
"""EMS-owned credential store: Zendure token, MQTT secrets, migration."""

from pathlib import Path

import pytest

from admin.credential_store import (
    CredentialStore,
    CredentialStoreError,
    MqttBrokerSecret,
    MqttCredentialsRefInvalidError,
    ZendureCloudTokenStore,
    default_config_dir,
)
from admin.secret_store import ZendureTokenStore

pytestmark = pytest.mark.simulation

TOKEN = "super-secret-zendure-api-token-value"


def _store(tmp_path):
    return CredentialStore(config_dir=tmp_path)


def test_default_config_dir_uses_ems_config_dir_first(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_CONFIG_DIR", str(tmp_path / "explicit-config"))
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path / "install"))
    from ems import paths

    monkeypatch.setattr(paths, "BASE_DIR", str(tmp_path / "base"))
    assert default_config_dir() == tmp_path / "explicit-config"


def test_default_config_dir_uses_ems_install_dir_config(tmp_path, monkeypatch):
    monkeypatch.delenv("EMS_CONFIG_DIR", raising=False)
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path / "install"))
    from ems import paths

    monkeypatch.setattr(paths, "BASE_DIR", str(tmp_path / "base"))
    assert default_config_dir() == tmp_path / "install" / "config"


def test_default_config_dir_falls_back_to_paths_base_dir_config(tmp_path, monkeypatch):
    monkeypatch.delenv("EMS_CONFIG_DIR", raising=False)
    monkeypatch.delenv("EMS_INSTALL_DIR", raising=False)
    from ems import paths

    monkeypatch.setattr(paths, "BASE_DIR", str(tmp_path / "base"))
    assert default_config_dir() == tmp_path / "base" / "config"


def test_zendure_token_saves_and_loads_without_plaintext(tmp_path):
    store = _store(tmp_path)
    store.save_zendure_token(TOKEN)
    assert store.load_zendure_token() == TOKEN
    raw = store.zendure.token_path.read_text(encoding="utf-8")
    assert TOKEN not in raw
    assert store.zendure.token_path.parts[-2] == "secrets"


def test_forget_zendure_token_removes_secret(tmp_path):
    store = _store(tmp_path)
    store.save_zendure_token(TOKEN)
    store.forget_zendure_token()
    assert store.load_zendure_token() is None
    assert not store.zendure.token_path.exists()
    assert store.zendure.settings()["credentials_ref"] is None


def test_mqtt_broker_secret_saves_and_loads(tmp_path):
    store = _store(tmp_path)
    ref = store.save_mqtt_broker_secret("homeassistant", "mqtt-user", "mqtt-pass")
    secret = store.load_mqtt_broker_secret(ref)
    assert isinstance(secret, MqttBrokerSecret)
    assert secret.username == "mqtt-user"
    assert secret.password == "mqtt-pass"


def test_mqtt_broker_secret_file_has_no_plaintext(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("homeassistant", "mqtt-user", "mqtt-pass")
    raw = store._mqtt_path("homeassistant").read_text(encoding="utf-8")
    assert "mqtt-pass" not in raw
    assert "mqtt-user" not in raw


def test_mqtt_broker_secret_status_is_redacted(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("hass", "u", "p")
    status = store.mqtt_broker_secret_status("hass")
    assert status == {
        "credentials_ref": "hass",
        "saved": True,
        "username_configured": True,
        "password_configured": True,
        "encrypted": True,
    }
    assert "u" not in str(status) or status["username_configured"] is True
    assert "p" not in [status.get("password")]


def test_multiple_broker_secrets_are_independent(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("broker-a", "ua", "pa")
    store.save_mqtt_broker_secret("broker-b", "ub", "pb")
    assert store.load_mqtt_broker_secret("broker-a").password == "pa"
    assert store.load_mqtt_broker_secret("broker-b").password == "pb"


def test_forget_mqtt_broker_secret_detaches_it(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("hass", "u", "p")
    store.forget_mqtt_broker_secret("hass")
    assert store.load_mqtt_broker_secret("hass") is None
    assert store.mqtt_broker_secret_status("hass")["saved"] is False


def test_legacy_admin_token_is_migrated(tmp_path):
    legacy_dir = tmp_path / "admin-data"
    legacy = ZendureTokenStore(legacy_dir)
    legacy.save_token(TOKEN)
    legacy.update_metadata(last_status="ok", last_device_count=2)

    config_dir = tmp_path / "config"
    store = CredentialStore(config_dir=config_dir, legacy_admin_data_dir=legacy_dir)
    # First read migrates the token into config/secrets without deleting legacy.
    assert store.load_zendure_token() == TOKEN
    assert store.zendure.token_path.exists()
    assert legacy.token_path.exists()
    settings = store.zendure.settings()
    assert settings["token_saved"] is True
    assert settings["last_status"] == "ok"


def test_migration_is_idempotent_and_prefers_new_token(tmp_path):
    legacy_dir = tmp_path / "admin-data"
    ZendureTokenStore(legacy_dir).save_token("old-token")
    config_dir = tmp_path / "config"

    store = CredentialStore(config_dir=config_dir, legacy_admin_data_dir=legacy_dir)
    assert store.load_zendure_token() == "old-token"
    store.save_zendure_token("new-token")
    # A fresh store instance must keep the new token, not re-migrate the old one.
    reopened = CredentialStore(config_dir=config_dir, legacy_admin_data_dir=legacy_dir)
    assert reopened.load_zendure_token() == "new-token"


# --- fail-closed encryption (no silent downgrade to base64) ----------------


def _poison_encrypt(monkeypatch, *, only=None):
    """Break Fernet.encrypt (leaving construction/decrypt intact).

    ``only`` restricts the failure to a single plaintext value so multi-field
    records can prove atomicity (one field encrypts, the next fails).
    """

    from cryptography.fernet import Fernet

    original = Fernet.encrypt

    def _maybe_boom(self, data):
        if only is None or data == only.encode("utf-8"):
            raise RuntimeError("simulated cipher failure")
        return original(self, data)

    monkeypatch.setattr(Fernet, "encrypt", _maybe_boom)


def _no_temp_files(store):
    return list(store.secrets_dir.glob("*.tmp")) == []


def test_zendure_token_save_fails_closed(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _poison_encrypt(monkeypatch)
    with pytest.raises(CredentialStoreError) as excinfo:
        store.save_zendure_token(TOKEN)
    assert not store.zendure.token_path.exists()
    assert _no_temp_files(store)
    assert TOKEN not in str(excinfo.value)


def test_zendure_cloud_token_store_save_fails_closed(tmp_path, monkeypatch):
    store = ZendureCloudTokenStore(tmp_path)
    _poison_encrypt(monkeypatch)
    with pytest.raises(CredentialStoreError):
        store.save_token(TOKEN)
    assert not store.token_path.exists()


def test_mqtt_broker_secret_save_fails_closed(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _poison_encrypt(monkeypatch)
    with pytest.raises(CredentialStoreError) as excinfo:
        store.save_mqtt_broker_secret("hass", "mqtt-user", "mqtt-pass")
    assert not store._mqtt_path("hass").exists()
    assert _no_temp_files(store)
    assert "mqtt-user" not in str(excinfo.value)
    assert "mqtt-pass" not in str(excinfo.value)


def test_mqtt_discovery_secret_save_fails_closed(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _poison_encrypt(monkeypatch)
    with pytest.raises(CredentialStoreError):
        store.save_mqtt_discovery_secret("pool-a", "disc-user", "disc-pass")
    assert not store._mqtt_discovery_path("pool-a").exists()
    assert _no_temp_files(store)


def test_mqtt_cloud_runtime_secret_save_fails_closed(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _poison_encrypt(monkeypatch)
    with pytest.raises(CredentialStoreError):
        store.save_mqtt_cloud_runtime_secret(
            "zendure-cloud",
            username="cloud-user",
            password="cloud-pass",
            client_id="cid-1",
            app_key="ak-1",
        )
    assert not store._mqtt_path("zendure-cloud").exists()
    assert _no_temp_files(store)


def test_malformed_key_makes_new_credential_write_fail_closed(tmp_path):
    store = _store(tmp_path)
    store.secrets_dir.mkdir(parents=True, exist_ok=True)
    store._files.key_path.write_bytes(b"not-a-valid-fernet-key")
    with pytest.raises(CredentialStoreError):
        store.save_mqtt_broker_secret("hass", "u", "p")
    assert not store._mqtt_path("hass").exists()
    assert _no_temp_files(store)


def test_mqtt_broker_secret_rotation_is_atomic(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("hass", "old-user", "old-pass")
    before = store._mqtt_path("hass").read_bytes()

    # Username encrypts, password fails -> the record must not be written and
    # the previous record must stay byte identical and usable.
    _poison_encrypt(monkeypatch, only="new-pass")
    with pytest.raises(CredentialStoreError):
        store.save_mqtt_broker_secret("hass", "new-user", "new-pass")

    assert store._mqtt_path("hass").read_bytes() == before
    assert _no_temp_files(store)
    secret = store.load_mqtt_broker_secret("hass")
    assert (secret.username, secret.password) == ("old-user", "old-pass")


def test_mqtt_cloud_runtime_secret_rotation_is_atomic(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.save_mqtt_cloud_runtime_secret(
        "zendure-cloud",
        username="old-user",
        password="old-pass",
        client_id="old-cid",
        app_key="old-ak",
    )
    before = store._mqtt_path("zendure-cloud").read_bytes()

    # A later field (app_key) fails after earlier fields encrypted.
    _poison_encrypt(monkeypatch, only="new-ak")
    with pytest.raises(CredentialStoreError):
        store.save_mqtt_cloud_runtime_secret(
            "zendure-cloud",
            username="new-user",
            password="new-pass",
            client_id="new-cid",
            app_key="new-ak",
        )

    assert store._mqtt_path("zendure-cloud").read_bytes() == before
    assert _no_temp_files(store)


# --- legacy unencrypted-record read compatibility (read-only) --------------


# A checked-in legacy (base64, pre-Fernet) record. The bytes are copied from a
# static fixture — never encoded from a password-shaped argument here — so the
# read-only backward-compatibility coverage carries no clear-text credential
# flow. The fixture decodes to neutral synthetic sentinels, not a real secret.
_LEGACY_RECORD_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "credentials"
    / "legacy-obfuscated-mqtt-record.json"
)
LEGACY_FIXTURE_USERNAME = "fixture-user"
LEGACY_FIXTURE_VALUE = "fixture-value"


def _install_legacy_broker_record(store, ref):
    store.secrets_dir.mkdir(parents=True, exist_ok=True)
    path = store._mqtt_path(ref)
    path.write_bytes(_LEGACY_RECORD_FIXTURE.read_bytes())
    return path


def test_legacy_unencrypted_broker_secret_is_readable_and_reported(tmp_path):
    store = _store(tmp_path)
    _install_legacy_broker_record(store, "legacy")

    secret = store.load_mqtt_broker_secret("legacy")
    assert (secret.username, secret.password) == (
        LEGACY_FIXTURE_USERNAME,
        LEGACY_FIXTURE_VALUE,
    )
    assert secret.encrypted is False

    status = store.mqtt_broker_secret_status("legacy")
    assert status["saved"] is True
    assert status["encrypted"] is False


def test_reading_a_legacy_record_never_rewrites_it(tmp_path):
    store = _store(tmp_path)
    path = _install_legacy_broker_record(store, "legacy")
    before = path.read_bytes()

    store.mqtt_broker_secret_status("legacy")
    store.load_mqtt_broker_secret("legacy")
    store.validate_runtime_credential("legacy")

    assert path.read_bytes() == before  # status/read must not silently re-encrypt


def test_new_save_over_legacy_record_requires_encryption(tmp_path, monkeypatch):
    store = _store(tmp_path)
    path = _install_legacy_broker_record(store, "legacy")
    before = path.read_bytes()

    # A fresh save must use Fernet or fail — never re-persist an obfuscated
    # record just because the previous one was unencrypted.
    _poison_encrypt(monkeypatch)
    with pytest.raises(CredentialStoreError):
        store.save_mqtt_broker_secret("legacy", "rotated-user", "rotated-pass")
    assert path.read_bytes() == before

    # With encryption available the rotation succeeds and is encrypted.
    monkeypatch.undo()
    store.save_mqtt_broker_secret("legacy", "rotated-user", "rotated-pass")
    assert store.mqtt_broker_secret_status("legacy")["encrypted"] is True


# --- credential ref path safety (no user-controlled path flows) ------------
# A configured/existing credentials_ref must be validated by the EMS Core
# authority, never silently normalized: an out-of-contract value is rejected
# with MqttCredentialsRefInvalidError before any filesystem operation, so
# traversal, separators, absolute paths, collisions and symlink escapes can
# never cross the store boundary.

INVALID_REFS = [
    "../outside",
    "../../secret",
    "/absolute/path",
    "\\windows\\path",
    ".",
    "..",
    "",
    "   ",
    "Mixed-Case",  # uppercase is not canonical
    "%2e%2e%2fetc",  # url-encoded traversal
    "e⁄tc",  # unicode fraction slash
    "with\x00nul",  # embedded NUL
    "ref?query=1",  # query-like text
    "ref#frag",  # fragment-like text
    "a" * 300,  # very long value
]


def _record_operations(store, ref):
    return {
        "load_broker": lambda: store.load_mqtt_broker_secret(ref),
        "save_broker": lambda: store.save_mqtt_broker_secret(ref, "u", "p"),
        "forget_broker": lambda: store.forget_mqtt_broker_secret(ref),
        "status_broker": lambda: store.mqtt_broker_secret_status(ref),
        "credential_exists": lambda: store.credential_exists(ref),
        "validate_runtime": lambda: store.validate_runtime_credential(ref),
        "snapshot": lambda: store.snapshot_mqtt_credential_change(ref),
        "save_cloud": lambda: store.save_mqtt_cloud_runtime_secret(
            ref, username="u", password="p", client_id="c", app_key="a"
        ),
        "load_discovery": lambda: store.load_mqtt_discovery_secret(ref),
        "save_discovery": lambda: store.save_mqtt_discovery_secret(ref, "u", "p"),
        "forget_discovery": lambda: store.forget_mqtt_discovery_secret(ref),
        "status_discovery": lambda: store.mqtt_discovery_secret_status(ref),
    }


@pytest.mark.parametrize("ref", INVALID_REFS)
def test_invalid_credential_ref_is_rejected_without_touching_files(tmp_path, ref):
    store = _store(tmp_path)
    for name, operation in _record_operations(store, ref).items():
        with pytest.raises(MqttCredentialsRefInvalidError):
            operation()
        # No read, write, delete, snapshot or key material was ever created.
        assert list(store.secrets_dir.glob("*")) == [], name


def test_two_invalid_refs_that_would_collide_are_both_rejected(tmp_path):
    store = _store(tmp_path)
    # Naive normalization would map both of these to the same "outside"
    # filename and let one clobber the other; validation rejects both instead.
    for ref in ("../outside", "..\\outside"):
        with pytest.raises(MqttCredentialsRefInvalidError):
            store.save_mqtt_broker_secret(ref, "u", "p")
    assert list(store.secrets_dir.glob("*")) == []


def test_symlink_escape_is_refused_for_read_write_and_delete(tmp_path):
    store = _store(tmp_path)
    store.secrets_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-secret.json"
    outside.write_text('{"ref": "home", "smuggled": true}', encoding="utf-8")
    # A managed filename inside secrets_dir that points outside it.
    link = store._mqtt_path("home")
    link.symlink_to(outside)

    for operation in (
        lambda: store.load_mqtt_broker_secret("home"),
        lambda: store.mqtt_broker_secret_status("home"),
        lambda: store.snapshot_mqtt_credential_change("home"),
    ):
        with pytest.raises(MqttCredentialsRefInvalidError):
            operation()

    before = outside.read_text(encoding="utf-8")
    with pytest.raises(MqttCredentialsRefInvalidError):
        store.save_mqtt_broker_secret("home", "new-user", "new-pass")
    # The escaping symlink was never written through to the outside file.
    assert outside.read_text(encoding="utf-8") == before

    with pytest.raises(MqttCredentialsRefInvalidError):
        store.forget_mqtt_broker_secret("home")
    assert outside.exists()


def test_canonical_and_fixed_records_still_work(tmp_path):
    store = _store(tmp_path)
    # Fixed well-known records: the Zendure token (zendure-cloud.json) and the
    # cloud runtime record (mqtt-zendure-cloud.json).
    store.save_zendure_token(TOKEN)
    assert store.load_zendure_token() == TOKEN
    store.save_mqtt_cloud_runtime_secret(
        "zendure-cloud", username="cu", password="cp", client_id="ci", app_key="ak"
    )
    assert store.credential_exists("zendure-cloud") is True

    # Canonical local broker refs round-trip and return the exact ref.
    for ref in ("home", "homeassistant", "broker-a", "local_mqtt", "pool-1"):
        assert store.save_mqtt_broker_secret(ref, "u", "p") == ref
        assert store.load_mqtt_broker_secret(ref).password == "p"

    # Canonical discovery refs round-trip too.
    store.save_mqtt_discovery_secret("pool-a", "du", "dp", label="Pool A")
    assert store.load_mqtt_discovery_secret("pool-a").username == "du"


def test_concurrent_writes_do_not_share_a_deterministic_temp_name(tmp_path):
    store = _store(tmp_path)
    store.secrets_dir.mkdir(parents=True, exist_ok=True)
    captured = []
    real_mkstemp = __import__("tempfile").mkstemp

    def _tracking_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        captured.append(name)
        return fd, name

    import tempfile as _tempfile

    original = _tempfile.mkstemp
    _tempfile.mkstemp = _tracking_mkstemp
    try:
        store.save_mqtt_broker_secret("home", "u1", "p1")
        store.save_mqtt_broker_secret("home", "u2", "p2")
    finally:
        _tempfile.mkstemp = original

    assert len(captured) == 2
    assert captured[0] != captured[1]  # unique temp names, no fixed ".tmp"
    assert list(store.secrets_dir.glob("*.tmp")) == []
