import os

import pytest

from admin.credential_store import CredentialStore, CredentialStoreError
from ems.mqtt_credentials import (
    MQTT_CREDENTIAL_REQUIRED_FIELDS,
    FileMqttCredentialResolver,
    MqttCredentialError,
    MqttCredentials,
    missing_mqtt_credential_fields,
    require_complete_mqtt_credentials,
    resolve_mqtt_cloud_profile_credentials,
    resolve_mqtt_profile_credentials,
    validate_mqtt_credentials_ref,
)
from ems.zendure_mqtt.config_entries import (
    SOURCE_LOCAL_MQTT,
    SOURCE_ZENDURE_CLOUD_MQTT,
)
from ems.zendure_mqtt.runtime import build_zendure_mqtt_runtime

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.contract,
]


def _store(tmp_path):
    return CredentialStore(config_dir=tmp_path)


# --- canonical credentials_ref syntax contract -----------------------------
# A configured credentials_ref is an immutable identifier: it must already use
# the canonical Core syntax so it resolves to exactly one credential file and
# stays unchanged from validation through runtime loading. The one authoritative
# helper is validate_mqtt_credentials_ref; nobody may silently normalize a value
# that is already in the config.


@pytest.mark.parametrize(
    "ref",
    ["local-broker", "zendure-cloud", "garage_mqtt", "broker-01", "home", "a", "0"],
)
def test_validate_credentials_ref_returns_canonical_value_unchanged(ref):
    assert validate_mqtt_credentials_ref(ref) == ref


@pytest.mark.parametrize(
    "ref",
    [
        "Bad Ref",
        "../secret",
        "broker/ref",
        "broker\\",
        "mqtt:home",
        " leading",
        "trailing ",
        "",
        "Home",
        "-leading-dash",
        "_leading-underscore",
        None,
    ],
)
def test_validate_credentials_ref_rejects_non_canonical_value(ref):
    with pytest.raises(MqttCredentialError):
        validate_mqtt_credentials_ref(ref)


def test_validate_credentials_ref_error_is_secret_free():
    with pytest.raises(MqttCredentialError) as caught:
        validate_mqtt_credentials_ref("Bad Ref")
    # The reference is echoed for the operator but never a secret value.
    assert "SUPER_SECRET_PASSWORD" not in str(caught.value)


def test_resolver_does_not_normalize_a_configured_reference(tmp_path):
    # The store persists records under the canonical (lowercased) ref, but the
    # resolver must not silently accept a differently-cased configured value as
    # a match — that is exactly the mismatch this contract forbids.
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("home", "user", "SUPER_SECRET_PASSWORD")
    resolver = FileMqttCredentialResolver(tmp_path / "secrets")
    assert resolver.resolve("home") == MqttCredentials("user", "SUPER_SECRET_PASSWORD")
    for configured in ("Home", "HOME", " home", "home "):
        with pytest.raises(MqttCredentialError):
            resolver.resolve(configured)


def test_file_resolver_reads_admin_record_without_admin_dependency(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("home", "SECRET_USER_123", "SUPER_SECRET_PASSWORD")
    got = FileMqttCredentialResolver(tmp_path / "secrets").resolve("home")
    assert got == MqttCredentials("SECRET_USER_123", "SUPER_SECRET_PASSWORD")


@pytest.mark.parametrize("ref", ["missing", "../escape", "BAD REF"])
def test_file_resolver_failure_is_controlled_and_secret_free(tmp_path, ref):
    with pytest.raises(MqttCredentialError) as caught:
        FileMqttCredentialResolver(tmp_path / "secrets").resolve(ref)
    assert "SUPER_SECRET_PASSWORD" not in str(caught.value)


def test_malformed_and_missing_password_are_rejected(tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "mqtt-bad.json").write_text("not json", encoding="utf-8")
    with pytest.raises(MqttCredentialError, match="malformed"):
        FileMqttCredentialResolver(secrets).resolve("bad")
    store = _store(tmp_path)
    with pytest.raises(CredentialStoreError, match="both be non-empty"):
        store.save_mqtt_broker_secret("partial", "user", None)


def test_reference_is_authoritative_and_inline_conflict_rejected():
    class Resolver:
        def resolve(self, ref):
            return MqttCredentials("resolved", "secret")

    assert resolve_mqtt_profile_credentials(
        {"credentials_ref": "home"}, Resolver()
    )["username"] == "resolved"
    with pytest.raises(MqttCredentialError, match="conflicts"):
        resolve_mqtt_profile_credentials(
            {"credentials_ref": "home", "username": "inline"}, Resolver()
        )


def test_telemetry_and_control_receive_resolved_credentials():
    seen = []

    class Resolver:
        def resolve(self, ref):
            return MqttCredentials("runtime-user", "runtime-password")

    class Service:
        running = False
        connected = False

        def __init__(self, config):
            seen.append(config)

        def snapshots(self):
            return {}

        def status(self):
            return {"last_error": None}

        def start(self): pass
        def stop(self): pass

    config = {
        "zendure_mqtt": {"enabled": True, "brokers": {"home": {
            "source": "local_mqtt", "host": "broker", "credentials_ref": "home"
        }}},
        "devices": [{"name": "read", "type": "zendure_mqtt", "sn": "A",
                     "mqtt": {"broker_ref": "home", "topic_family": "legacy_json"}}],
    }
    build_zendure_mqtt_runtime(config, service_factory=Service, credential_resolver=Resolver())
    assert (seen[0].username, seen[0].password) == ("runtime-user", "runtime-password")


@pytest.mark.parametrize(
    "username,password", [("user", None), (None, "password"), ("", "password"), ("user", "")]
)
def test_admin_store_rejects_incomplete_authentication_pairs(
    tmp_path, username, password
):
    store = _store(tmp_path)
    with pytest.raises(CredentialStoreError, match="both be non-empty"):
        store.save_mqtt_broker_secret("partial", username, password)
    with pytest.raises(CredentialStoreError, match="both be non-empty"):
        store.save_mqtt_discovery_secret("partial", username, password)


# --- core-owned source-specific completeness contract -----------------------
# The required-field lists and the completeness test live in the EMS Core
# (ems.mqtt_credentials), not in Admin. A required field is complete only when it
# is a non-whitespace string; the stored value is never trimmed, so a real
# password with surrounding spaces stays byte-for-byte intact.

WHITESPACE = [" ", "   ", "\t", "\n", "\r\n"]


def test_core_defines_the_per_source_required_fields():
    assert MQTT_CREDENTIAL_REQUIRED_FIELDS[SOURCE_LOCAL_MQTT] == (
        "username",
        "password",
    )
    assert MQTT_CREDENTIAL_REQUIRED_FIELDS[SOURCE_ZENDURE_CLOUD_MQTT] == (
        "username",
        "password",
        "client_id",
        "app_key",
    )


@pytest.mark.parametrize("blank", WHITESPACE + [None, ""])
@pytest.mark.parametrize("field", ["username", "password"])
def test_missing_local_field_is_reported(field, blank):
    creds = {"username": "user", "password": "pass"}
    creds[field] = blank
    assert missing_mqtt_credential_fields(creds, source=SOURCE_LOCAL_MQTT) == (field,)


@pytest.mark.parametrize("blank", WHITESPACE + [None, ""])
@pytest.mark.parametrize("field", ["username", "password", "client_id", "app_key"])
def test_missing_cloud_field_is_reported(field, blank):
    creds = {
        "username": "user",
        "password": "pass",
        "client_id": "cid",
        "app_key": "ak",
    }
    creds[field] = blank
    assert missing_mqtt_credential_fields(creds, source=SOURCE_ZENDURE_CLOUD_MQTT) == (
        field,
    )


def test_complete_records_report_nothing_missing():
    assert (
        missing_mqtt_credential_fields(
            MqttCredentials("user", "pass"), source=SOURCE_LOCAL_MQTT
        )
        == ()
    )
    assert (
        missing_mqtt_credential_fields(
            MqttCredentials("user", "pass", "cid", "ak"),
            source=SOURCE_ZENDURE_CLOUD_MQTT,
        )
        == ()
    )


def test_padded_field_is_complete_and_reported_verbatim():
    # A genuine value with surrounding spaces is complete after stripping is only
    # tested, never applied — the object still carries the padded value.
    creds = MqttCredentials("user", "  s3cr3t  ")
    assert missing_mqtt_credential_fields(creds, source=SOURCE_LOCAL_MQTT) == ()
    require_complete_mqtt_credentials(
        creds, source=SOURCE_LOCAL_MQTT, credentials_ref="home"
    )
    assert creds.password == "  s3cr3t  "


def test_require_complete_error_names_ref_and_field_but_no_secret():
    with pytest.raises(MqttCredentialError) as caught:
        require_complete_mqtt_credentials(
            MqttCredentials("user", "   "),
            source=SOURCE_LOCAL_MQTT,
            credentials_ref="home-auth",
        )
    message = str(caught.value)
    assert "home-auth" in message
    assert "password" in message
    assert "local_mqtt" in message


# --- the resolvers enforce the core contract (never fall back to anonymous) --


class _Fixed:
    def __init__(self, credentials):
        self._credentials = credentials

    def resolve(self, ref):
        return self._credentials


@pytest.mark.parametrize("blank", WHITESPACE)
@pytest.mark.parametrize("field", ["username", "password"])
def test_local_resolver_rejects_whitespace_required_field(field, blank):
    values = {"username": "user", "password": "pass"}
    values[field] = blank
    resolver = _Fixed(MqttCredentials(values["username"], values["password"]))
    with pytest.raises(MqttCredentialError) as caught:
        resolve_mqtt_profile_credentials({"credentials_ref": "home"}, resolver)
    # The reference may be named; the whitespace/real value never is.
    assert "home" in str(caught.value)


def test_local_resolver_preserves_padded_password_byte_for_byte():
    resolver = _Fixed(MqttCredentials("user", "  s3cr3t  "))
    resolved = resolve_mqtt_profile_credentials({"credentials_ref": "home"}, resolver)
    assert resolved["password"] == "  s3cr3t  "
    assert resolved["username"] == "user"


def test_local_profile_without_ref_stays_anonymous():
    # No credentials_ref: an anonymous local broker is explicitly allowed.
    resolved = resolve_mqtt_profile_credentials({"host": "b.local"})
    assert resolved.get("username") is None
    assert resolved.get("password") is None


@pytest.mark.parametrize("blank", WHITESPACE)
@pytest.mark.parametrize("field", ["username", "password", "client_id", "app_key"])
def test_cloud_resolver_rejects_whitespace_required_field(field, blank):
    values = {
        "username": "user",
        "password": "pass",
        "client_id": "cid",
        "app_key": "ak",
    }
    values[field] = blank
    resolver = _Fixed(MqttCredentials(**values))
    with pytest.raises(MqttCredentialError):
        resolve_mqtt_cloud_profile_credentials({"credentials_ref": "cloud"}, resolver)


def test_cloud_resolver_fills_complete_record():
    resolver = _Fixed(MqttCredentials("user", "pass", "cid", "ak"))
    resolved = resolve_mqtt_cloud_profile_credentials(
        {"credentials_ref": "cloud"}, resolver
    )
    assert resolved["username"] == "user"
    assert resolved["client_id"] == "cid"
    assert resolved["app_key"] == "ak"


# --- shared source-specific completeness contract ---------------------------
# One helper owns the per-source required-field lists, so credential-store
# validation, provisioning result checks, post-save verification and the
# Setup/Maintenance reuse decisions can never drift apart.


def test_shared_validator_requires_complete_cloud_contract():
    from admin.credential_store import validate_resolved_mqtt_credential

    complete = MqttCredentials("user", "secret", "client", "app-key")
    result = validate_resolved_mqtt_credential(
        credentials_ref="zendure-cloud",
        source="zendure_cloud_mqtt",
        resolved=complete,
    )
    assert result.status == "valid"
    assert result.credentials_ref == "zendure-cloud"


@pytest.mark.parametrize(
    "resolved",
    [
        MqttCredentials(None, "secret", "client", "app-key"),
        MqttCredentials("user", None, "client", "app-key"),
        MqttCredentials("user", "secret", None, "app-key"),
        MqttCredentials("user", "secret", "client", None),
        MqttCredentials("", "secret", "client", "app-key"),
        MqttCredentials("user", "", "client", "app-key"),
        MqttCredentials("user", "secret", "", "app-key"),
        MqttCredentials("user", "secret", "client", ""),
    ],
)
def test_shared_validator_rejects_incomplete_cloud_records(resolved):
    from admin.credential_store import validate_resolved_mqtt_credential

    result = validate_resolved_mqtt_credential(
        credentials_ref="zendure-cloud",
        source="zendure_cloud_mqtt",
        resolved=resolved,
    )
    assert result.status == "invalid"
    assert result.credentials_ref == "zendure-cloud"
    assert result.reason
    assert "secret" not in (result.reason or "")


def test_shared_validator_accepts_complete_local_pair():
    from admin.credential_store import validate_resolved_mqtt_credential

    result = validate_resolved_mqtt_credential(
        credentials_ref="home",
        source="local_mqtt",
        resolved=MqttCredentials("user", "secret"),
    )
    assert result.status == "valid"


@pytest.mark.parametrize(
    "resolved",
    [
        MqttCredentials(None, None),
        MqttCredentials("user", None),
        MqttCredentials(None, "secret"),
        MqttCredentials("", ""),
        MqttCredentials("user", ""),
        MqttCredentials("", "secret"),
    ],
)
def test_shared_validator_rejects_incomplete_local_records(resolved):
    # A record referenced from a broker's credentials_ref stands for real
    # authentication: an empty or one-sided pair must never pass as valid.
    from admin.credential_store import validate_resolved_mqtt_credential

    result = validate_resolved_mqtt_credential(
        credentials_ref="home",
        source="local_mqtt",
        resolved=resolved,
    )
    assert result.status == "invalid"
    assert result.credentials_ref == "home"
    assert "secret" not in (result.reason or "")


# --- transactional staging (create / reuse / rotate / rollback) -----------
# The one shared implementation lives in admin.mqtt_runtime_provisioning
# (stage_runtime_credentials_for_config and the setup staging around it) and
# uses CredentialChange snapshots for every local and cloud change; its
# behavior is covered in tests/test_admin_mqtt_runtime_provisioning.py. Only
# the snapshot/rollback primitive is exercised here.


def test_rollback_restores_rotated_record_and_deletes_created_one(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("home", "user", "old-password")
    old_bytes = (tmp_path / "secrets" / "mqtt-home.json").read_bytes()

    rotation = store.snapshot_mqtt_credential_change("home")
    store.save_mqtt_broker_secret("home", "user", "new-password")
    creation = store.snapshot_mqtt_credential_change("shed")
    store.save_mqtt_broker_secret("shed", "user2", "password2")

    assert store.rollback_credential_changes([rotation, creation]) == []
    # Byte-level put-back for the rotated record, deletion for the new one.
    assert (tmp_path / "secrets" / "mqtt-home.json").read_bytes() == old_bytes
    assert store.load_mqtt_broker_secret("shed") is None


def test_snapshot_captures_raw_bytes_without_parsing(tmp_path):
    # A snapshot must not depend on the record being parseable: the exact
    # on-disk bytes are the rollback target, and a malformed existing file is
    # an existing file — never "missing".
    store = _store(tmp_path)
    secrets = tmp_path / "secrets"
    secrets.mkdir(exist_ok=True)
    path = secrets / "mqtt-home.json"
    path.write_bytes(b"{broken json")

    change = store.snapshot_mqtt_credential_change("home")
    assert change.existed_before is True
    assert change.raw_bytes == b"{broken json"

    store.save_mqtt_broker_secret("home", "user", "new-password")
    assert store.rollback_credential_changes([change]) == []
    assert path.exists()
    assert path.read_bytes() == b"{broken json"


def test_rollback_preserves_unknown_record_format_byte_for_byte(tmp_path):
    # Unknown future fields and the exact formatting must survive a rollback;
    # a parsed-JSON round-trip would silently normalize them away.
    raw = (
        b'{\n'
        b'    "version": 99,\n'
        b'    "ref": "home",\n'
        b'    "future_format": {"nested": [1, 2, 3]},\n'
        b'    "note": "unknown fields and formatting must survive"\n'
        b'}\n'
    )
    store = _store(tmp_path)
    secrets = tmp_path / "secrets"
    secrets.mkdir(exist_ok=True)
    path = secrets / "mqtt-home.json"
    path.write_bytes(raw)

    change = store.snapshot_mqtt_credential_change("home")
    store.save_mqtt_broker_secret("home", "user", "password")
    assert store.rollback_credential_changes([change]) == []
    assert path.read_bytes() == raw


def test_rollback_restore_failure_leaves_no_partial_file(tmp_path, monkeypatch):
    # A failed restore must not truncate or half-write the credential file:
    # the write goes to a temporary file first, so the target keeps its
    # pre-restore content and the failed ref is reported.
    from admin import credential_store as credential_store_module

    store = _store(tmp_path)
    secrets = tmp_path / "secrets"
    secrets.mkdir(exist_ok=True)
    path = secrets / "mqtt-home.json"
    path.write_bytes(b"{broken json")

    change = store.snapshot_mqtt_credential_change("home")
    store.save_mqtt_broker_secret("home", "user", "new-password")
    rotated = path.read_bytes()

    real_replace = os.replace

    def _fail_replace(src, dst, *args, **kwargs):
        if str(dst) == str(path):
            raise OSError("simulated replace failure")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(credential_store_module.os, "replace", _fail_replace)
    assert store.rollback_credential_changes([change]) == ["home"]
    assert path.read_bytes() == rotated
    assert list(secrets.glob("*.tmp")) == []
