# SPDX-License-Identifier: AGPL-3.0-or-later
"""Existing runtime MQTT credential records are validated, never assumed.

A ``credentials_ref`` is usable only when the stored record exists, decrypts,
carries complete fields and resolves through the Core resolver
(``ems.mqtt_credentials.FileMqttCredentialResolver``). File existence alone must
never let an Apply pass: a broken record is reprovisioned when a trusted
replacement is available (discovery pool for local brokers, the Zendure API
credential for the cloud broker) and blocks the apply with an actionable,
secret-free error when it is not.
"""

import json

import pytest

from admin.credential_store import (
    CredentialStore,
    CredentialStoreError,
    MqttCredentialSourceConflictError,
    MqttCredentialsRefInvalidError,
)
from admin.mqtt_runtime_provisioning import (
    stage_runtime_credentials_for_config,
    stage_setup_runtime_credentials,
    validate_all_runtime_credentials,
)
from admin.zendure_cloud_mqtt import ZendureCloudDiscovery
from ems.mqtt_credentials import FileMqttCredentialResolver
from tests.test_admin_maintenance_mqtt_apply import (
    _cloud_broker_summary,
    _CloudFetch,
    _cloud_proposal,
    _existing_config,
    _local_observation,
    _local_proposal,
    _maintenance_apply_with_proposal,
    _paths,
    _request,
    _serve,
    _write_config,
)

pytestmark = pytest.mark.simulation

CLOUD_REF = "zendure-cloud"
API_KEY = "raw-account-api-key"
SECRET = "super-secret-password"


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _store(tmp_path):
    return CredentialStore(config_dir=tmp_path / "config")


def _record_path(store, ref):
    return store.secrets_dir / f"mqtt-{ref}.json"


def _corrupt_record(store, ref):
    """Make the stored record undecryptable while keeping the file present."""

    path = _record_path(store, ref)
    record = json.loads(path.read_text(encoding="utf-8"))
    for field in ("username", "password"):
        if field in record:
            record[field] = "bm90LWEtZmVybmV0LXRva2Vu"
            record[f"{field}_encrypted"] = True
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _strip_password(store, ref):
    """Leave a username-only record behind (incomplete authentication)."""

    return _strip_field(store, ref, "password")


def _strip_field(store, ref, field):
    """Remove one stored field, keeping the rest of the record intact."""

    path = _record_path(store, ref)
    record = json.loads(path.read_text(encoding="utf-8"))
    record.pop(field, None)
    record.pop(f"{field}_encrypted", None)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _blank_field(store, ref, field):
    """Replace one stored field's value with an (encrypted) empty string."""

    path = _record_path(store, ref)
    record = json.loads(path.read_text(encoding="utf-8"))
    blob, encrypted = store._files.encrypt("")
    record[field] = blob
    record[f"{field}_encrypted"] = encrypted
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


_CLOUD_RUNTIME_FIELDS = ("username", "password", "client_id", "app_key")

# The non-Fernet blob _corrupt_record leaves behind, used to recognize the
# corrupted previous record during fault injection.
_CORRUPT_MARKER = "bm90LWEtZmVybmV0LXRva2Vu"


def _patch_restore_failure(monkeypatch):
    """Fail only the rollback restore of the corrupted previous record.

    Fresh saves keep working. Both store write primitives are patched so the
    injection tracks the implementation: parsed-record writes (write_record)
    and, when present, raw-byte restores (write_raw).
    """

    from admin import credential_store as credential_store_module

    files = credential_store_module._EncryptedFiles
    original_record = files.write_record

    def _fail_record_restore(self, path, record):
        if _CORRUPT_MARKER in json.dumps(record):
            raise credential_store_module.CredentialStoreError(
                "Could not write the secret file."
            )
        return original_record(self, path, record)

    monkeypatch.setattr(files, "write_record", _fail_record_restore)
    if hasattr(files, "write_raw"):
        original_raw = files.write_raw

        def _fail_raw_restore(self, path, data):
            if _CORRUPT_MARKER.encode("ascii") in bytes(data):
                raise credential_store_module.CredentialStoreError(
                    "Could not write the secret file."
                )
            return original_raw(self, path, data)

        monkeypatch.setattr(files, "write_raw", _fail_raw_restore)


def _save_complete_cloud_record(store):
    store.save_mqtt_cloud_runtime_secret(
        CLOUD_REF,
        username="cloud-user",
        password=SECRET,
        client_id="cloud-client-1",
        app_key="cloud-app-key-1",
    )


def _cloud_discovery(store, fetch):
    return ZendureCloudDiscovery(
        store.zendure, device_list_fetcher=fetch, listener_factory=lambda c: None
    )


def _local_config(ref="home"):
    return {
        "devices": [
            {
                "type": "zendure_mqtt",
                "enabled": True,
                "sn": "SN-LOCAL1",
                "mqtt": {"broker_ref": "local_home"},
            }
        ],
        "zendure_mqtt": {
            "brokers": {
                "local_home": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "10.0.0.10",
                    "port": 1883,
                    "credentials_ref": ref,
                }
            }
        },
    }


def _cloud_config():
    return {
        "devices": [
            {
                "type": "zendure_mqtt",
                "enabled": True,
                "sn": "SN-CLOUD1",
                "mqtt": {"broker_ref": "zendure_cloud"},
            }
        ],
        "zendure_mqtt": {
            "brokers": {
                "zendure_cloud": {
                    "enabled": True,
                    "source": "zendure_cloud_mqtt",
                    "host": "mqtteu.zen-iot.com",
                    "port": 8883,
                    "credentials_ref": CLOUD_REF,
                }
            }
        },
    }


# --- Core-resolver-backed validation of one credential record --------------


def test_validate_reports_missing_record(tmp_path):
    store = _store(tmp_path)
    result = store.validate_runtime_credential("home")
    assert result.status == "missing"
    assert result.credentials_ref == "home"


def test_validate_accepts_resolvable_record(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("home", "ems", SECRET)
    result = store.validate_runtime_credential(
        "home", expected_username="ems", expected_password=SECRET
    )
    assert result.status == "valid"


def test_validate_rejects_undecryptable_record(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("home", "ems", SECRET)
    _corrupt_record(store, "home")
    result = store.validate_runtime_credential("home")
    assert result.status == "invalid"
    assert result.reason


def test_validate_rejects_incomplete_record(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("home", "ems", SECRET)
    _strip_password(store, "home")
    result = store.validate_runtime_credential("home")
    assert result.status == "invalid"


def test_validate_detects_credential_mismatch(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("home", "ems", "old-password")
    result = store.validate_runtime_credential(
        "home", expected_username="ems", expected_password="new-password"
    )
    assert result.status == "mismatch"


def test_validate_rejects_empty_local_record(tmp_path):
    # A configured credentials_ref promises authentication; a record with
    # neither username nor password must not silently downgrade the broker
    # to anonymous access.
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("home", None, None)
    result = store.validate_runtime_credential("home", expected_source="local_mqtt")
    assert result.status == "invalid"
    assert result.credentials_ref == "home"
    assert result.reason


def test_validate_rejects_empty_local_record_without_expected_source(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("home", None, None)
    result = store.validate_runtime_credential("home")
    assert result.status == "invalid"


def test_validate_rejects_local_record_with_blank_credentials(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("home", "ems", SECRET)
    _blank_field(store, "home", "username")
    _blank_field(store, "home", "password")
    result = store.validate_runtime_credential("home", expected_source="local_mqtt")
    assert result.status == "invalid"


def test_validate_rejects_cloud_record_under_local_ref(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_cloud_runtime_secret(
        "home",
        username="cloud-user",
        password=SECRET,
        client_id="cloud-client-1",
        app_key="cloud-app-key-1",
    )
    result = store.validate_runtime_credential("home", expected_source="local_mqtt")
    assert result.status == "invalid"


def test_validate_rejects_wrong_record_type_for_cloud(tmp_path):
    # A plain local broker record stored under the cloud ref is not a usable
    # cloud runtime credential even though it resolves as username/password.
    store = _store(tmp_path)
    store.save_mqtt_broker_secret(CLOUD_REF, "ems", SECRET)
    result = store.validate_runtime_credential(
        CLOUD_REF, expected_source="zendure_cloud_mqtt"
    )
    assert result.status == "invalid"


@pytest.mark.parametrize("field", _CLOUD_RUNTIME_FIELDS)
def test_validate_rejects_cloud_record_missing_required_field(tmp_path, field):
    # The EMS runtime needs all four fields to use the cloud broker (the
    # connection is built from client_id, the subscriptions from app_key), so
    # a record missing any of them must never validate as a usable cloud
    # runtime credential.
    store = _store(tmp_path)
    _save_complete_cloud_record(store)
    _strip_field(store, CLOUD_REF, field)
    result = store.validate_runtime_credential(
        CLOUD_REF, expected_source="zendure_cloud_mqtt"
    )
    assert result.status == "invalid"
    assert result.credentials_ref == CLOUD_REF
    assert result.reason
    assert SECRET not in (result.reason or "")


@pytest.mark.parametrize("field", _CLOUD_RUNTIME_FIELDS)
def test_validate_rejects_cloud_record_with_empty_required_field(tmp_path, field):
    store = _store(tmp_path)
    _save_complete_cloud_record(store)
    _blank_field(store, CLOUD_REF, field)
    result = store.validate_runtime_credential(
        CLOUD_REF, expected_source="zendure_cloud_mqtt"
    )
    assert result.status == "invalid"
    assert result.credentials_ref == CLOUD_REF
    assert SECRET not in (result.reason or "")


def test_validate_accepts_complete_cloud_record(tmp_path):
    store = _store(tmp_path)
    _save_complete_cloud_record(store)
    result = store.validate_runtime_credential(
        CLOUD_REF, expected_source="zendure_cloud_mqtt"
    )
    assert result.status == "valid"


def test_validate_result_carries_no_secret_values(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("home", "ems", SECRET)
    for result in (
        store.validate_runtime_credential(
            "home", expected_username="ems", expected_password="different"
        ),
        store.validate_runtime_credential("home"),
    ):
        assert SECRET not in repr(result)
        assert "different" not in repr(result)


# --- canonical credentials_ref gate before any staging ----------------------
# A configured credentials_ref must already be canonical: staging must never
# normalize "Bad Ref" to "bad-ref" behind the config's back (the config would
# then reference a file the Core resolver rejects). The gate runs before any
# snapshot, save or network call, so an invalid reference creates no file.


_NON_CANONICAL_REFS = ["Bad Ref", "../secret", "broker/ref", "mqtt:home", "Home", ""]


@pytest.mark.parametrize("ref", _NON_CANONICAL_REFS)
def test_stage_rejects_non_canonical_local_ref_before_any_write(tmp_path, ref):
    store = _store(tmp_path)
    config = _local_config(ref=ref)
    with pytest.raises(MqttCredentialsRefInvalidError) as excinfo:
        stage_runtime_credentials_for_config(
            config,
            credential_store=store,
            cloud_discovery=_cloud_discovery(store, _CloudFetch()),
        )
    assert excinfo.value.code == "mqtt_credentials_ref_invalid"
    assert excinfo.value.credentials_ref == ref
    assert SECRET not in str(excinfo.value)
    # No credential file was created for the (would-be normalized) reference.
    assert not store.secrets_dir.exists() or (
        list(store.secrets_dir.glob("mqtt-*.json")) == []
    )


@pytest.mark.parametrize("ref", _NON_CANONICAL_REFS)
def test_stage_rejects_non_canonical_cloud_ref_before_any_write(tmp_path, ref):
    store = _store(tmp_path)
    store.zendure.save_token(API_KEY)
    config = _cloud_config()
    config["zendure_mqtt"]["brokers"]["zendure_cloud"]["credentials_ref"] = ref
    fetch = _CloudFetch()
    with pytest.raises(MqttCredentialsRefInvalidError) as excinfo:
        stage_runtime_credentials_for_config(
            config, credential_store=store, cloud_discovery=_cloud_discovery(store, fetch)
        )
    assert excinfo.value.credentials_ref == ref
    # The gate is before any cloud provisioning round-trip.
    assert fetch.calls == 0
    assert list(store.secrets_dir.glob("mqtt-*.json")) == []


def test_stage_setup_rejects_non_canonical_ref_without_persisting_manual_secret(
    tmp_path,
):
    # A bad reference elsewhere in the generated config must block the whole
    # setup apply before even the manual broker secret is persisted.
    store = _store(tmp_path)
    config = _local_config(ref="Bad Ref")
    changes = []
    with pytest.raises(MqttCredentialsRefInvalidError):
        stage_setup_runtime_credentials(
            config,
            {
                "name": "manual",
                "host": "10.0.0.20",
                "port": 1883,
                "username": "manual-user",
                "password": "manual-password",
            },
            changes,
            credential_store=store,
            cloud_discovery=_cloud_discovery(store, _CloudFetch()),
        )
    assert changes == []
    assert list(store.secrets_dir.glob("mqtt-*.json")) == []


def test_stage_accepts_canonical_ref_unchanged(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_discovery_secret("garage_mqtt", "ems", SECRET)
    changes = stage_runtime_credentials_for_config(
        _local_config(ref="garage_mqtt"),
        credential_store=store,
        cloud_discovery=_cloud_discovery(store, _CloudFetch()),
    )
    # The canonical reference is used verbatim: the staged record resolves under
    # exactly the configured ref through the Core resolver.
    assert [change.credentials_ref for change in changes] == ["garage_mqtt"]
    resolved = FileMqttCredentialResolver(store.secrets_dir).resolve("garage_mqtt")
    assert (resolved.username, resolved.password) == ("ems", SECRET)


def test_maintenance_apply_rejects_non_canonical_configured_ref(monkeypatch, tmp_path):
    # A config.json whose broker profile references a non-canonical
    # credentials_ref (e.g. edited by hand) must be blocked with the stable code
    # before config.json or any credential file changes.
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _existing_config())
    fetch = _CloudFetch()
    srv, base = _serve(
        tmp_path, fetch, local_observation=_local_observation(credentials_ref="home")
    )
    srv.credential_store.save_mqtt_discovery_secret("home", "ems", SECRET)
    try:
        proposal = _local_proposal(base)
        status, payload = _maintenance_apply_with_proposal(base, proposal)
        assert status == 200 and payload.get("ok") is True, payload

        config_path, secrets_dir = _paths(tmp_path)
        # Rewrite the applied broker's credentials_ref to a non-canonical value.
        config = json.loads(config_path.read_text(encoding="utf-8"))
        for profile in config["zendure_mqtt"]["brokers"].values():
            if profile.get("credentials_ref") == "home":
                profile["credentials_ref"] = "Bad Ref"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        original = config_path.read_bytes()

        status, loaded = _request(f"{base}/api/admin/maintenance/config")
        assert status == 200 and loaded["status"] == "ok", loaded
        status, payload = _request(
            f"{base}/api/admin/maintenance/config/apply",
            "POST",
            {"draft": loaded["draft"], "revision": loaded["revision"], "confirm": True},
        )
        assert status == 400, payload
        assert payload.get("ok") is False
        assert payload.get("code") == "mqtt_credentials_ref_invalid"
        assert payload.get("credentials_ref") == "Bad Ref"
        blob = json.dumps(payload)
        assert SECRET not in blob
        # config.json untouched and no file created under the normalized name.
        assert config_path.read_bytes() == original
        assert not (secrets_dir / "mqtt-bad-ref.json").exists()
    finally:
        srv.shutdown()
        srv.server_close()


# --- one credentials_ref belongs to one credential source -------------------
# A credentials_ref resolves to a single credential file, so it can back only
# one source. A ref shared by a local and a cloud broker is a conflict: local
# staging would write a local record, cloud staging would replace it, and the
# final config would validate structurally while only one broker could use it.
# The conflict is rejected before any snapshot, save, cloud call or config write.


def _shared_ref_conflict_config(ref="shared"):
    return {
        "devices": [
            {
                "type": "zendure_mqtt",
                "enabled": True,
                "sn": "SN-LOCAL1",
                "mqtt": {"broker_ref": "local_home"},
            },
            {
                "type": "zendure_mqtt",
                "enabled": True,
                "sn": "SN-CLOUD1",
                "mqtt": {"broker_ref": "zendure_cloud"},
            },
        ],
        "zendure_mqtt": {
            "brokers": {
                "local_home": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "10.0.0.10",
                    "port": 1883,
                    "credentials_ref": ref,
                },
                "zendure_cloud": {
                    "enabled": True,
                    "source": "zendure_cloud_mqtt",
                    "host": "mqtteu.zen-iot.com",
                    "port": 8883,
                    "credentials_ref": ref,
                },
            }
        },
    }


def test_stage_rejects_local_and_cloud_sharing_one_ref(tmp_path):
    store = _store(tmp_path)
    store.zendure.save_token(API_KEY)
    # A pre-existing local record under the shared ref must survive untouched.
    store.save_mqtt_broker_secret("shared", "ems", SECRET)
    before = _record_path(store, "shared").read_bytes()
    fetch = _CloudFetch()
    with pytest.raises(MqttCredentialSourceConflictError) as excinfo:
        stage_runtime_credentials_for_config(
            _shared_ref_conflict_config(),
            credential_store=store,
            cloud_discovery=_cloud_discovery(store, fetch),
        )
    assert excinfo.value.code == "mqtt_credential_source_conflict"
    assert excinfo.value.credentials_ref == "shared"
    assert sorted(excinfo.value.sources) == ["local_mqtt", "zendure_cloud_mqtt"]
    assert SECRET not in str(excinfo.value)
    # No mutation and no cloud round-trip happened.
    assert fetch.calls == 0
    assert _record_path(store, "shared").read_bytes() == before


def test_stage_allows_multiple_local_brokers_sharing_one_ref(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_discovery_secret("home", "ems", SECRET)
    config = _local_config(ref="home")
    config["zendure_mqtt"]["brokers"]["local_shed"] = {
        "enabled": True,
        "source": "local_mqtt",
        "host": "10.0.0.11",
        "port": 1883,
        "credentials_ref": "home",
    }
    config["devices"].append(
        {
            "type": "zendure_mqtt",
            "enabled": True,
            "sn": "SN-LOCAL2",
            "mqtt": {"broker_ref": "local_shed"},
        }
    )
    # Two local brokers sharing one local record stage a single record, no error.
    changes = stage_runtime_credentials_for_config(
        config,
        credential_store=store,
        cloud_discovery=_cloud_discovery(store, _CloudFetch()),
    )
    assert [change.credentials_ref for change in changes] == ["home"]


def test_stage_allows_multiple_cloud_brokers_sharing_one_ref(tmp_path):
    store = _store(tmp_path)
    store.zendure.save_token(API_KEY)
    _save_complete_cloud_record(store)
    config = _cloud_config()
    config["zendure_mqtt"]["brokers"]["zendure_cloud_2"] = dict(
        config["zendure_mqtt"]["brokers"]["zendure_cloud"]
    )
    config["devices"].append(
        {
            "type": "zendure_mqtt",
            "enabled": True,
            "sn": "SN-CLOUD2",
            "mqtt": {"broker_ref": "zendure_cloud_2"},
        }
    )
    fetch = _CloudFetch()
    changes = stage_runtime_credentials_for_config(
        config,
        credential_store=store,
        cloud_discovery=_cloud_discovery(store, fetch),
    )
    # The single valid cloud record is reused for both brokers, no refetch.
    assert changes == []
    assert fetch.calls == 0


def test_maintenance_apply_rejects_cross_source_shared_ref(monkeypatch, tmp_path):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _existing_config())
    fetch = _CloudFetch()
    srv, base = _serve(
        tmp_path, fetch, local_observation=_local_observation(credentials_ref="home")
    )
    srv.credential_store.save_mqtt_discovery_secret("home", "ems", SECRET)
    try:
        # Apply a valid cloud device (broker ref "zendure-cloud") and a valid
        # local device (broker ref "home"): two real, valid brokers.
        status, payload = _maintenance_apply_with_proposal(base, _cloud_proposal(base))
        assert status == 200 and payload.get("ok") is True, payload
        status, payload = _maintenance_apply_with_proposal(base, _local_proposal(base))
        assert status == 200 and payload.get("ok") is True, payload

        config_path, secrets_dir = _paths(tmp_path)
        # Repoint the local broker at the cloud ref: one reference now claims
        # both the local and the cloud source.
        config = json.loads(config_path.read_text(encoding="utf-8"))
        for profile in config["zendure_mqtt"]["brokers"].values():
            if profile.get("source") == "local_mqtt":
                profile["credentials_ref"] = "zendure-cloud"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        original = config_path.read_bytes()
        cloud_record_before = (secrets_dir / "mqtt-zendure-cloud.json").read_bytes()

        status, loaded = _request(f"{base}/api/admin/maintenance/config")
        assert status == 200 and loaded["status"] == "ok", loaded
        status, payload = _request(
            f"{base}/api/admin/maintenance/config/apply",
            "POST",
            {"draft": loaded["draft"], "revision": loaded["revision"], "confirm": True},
        )
        assert status == 400, payload
        assert payload.get("ok") is False
        assert payload.get("code") == "mqtt_credential_source_conflict"
        assert payload.get("credentials_ref") == "zendure-cloud"
        assert sorted(payload.get("sources") or []) == [
            "local_mqtt",
            "zendure_cloud_mqtt",
        ]
        blob = json.dumps(payload)
        for secret in (SECRET, API_KEY, fetch.password, fetch.app_key):
            assert secret not in blob
        # Nothing was rotated or written.
        assert config_path.read_bytes() == original
        assert (secrets_dir / "mqtt-zendure-cloud.json").read_bytes() == cloud_record_before
    finally:
        srv.shutdown()
        srv.server_close()


# --- final verification of every referenced credential after staging --------
# A later staging operation can overwrite or invalidate a record an earlier
# operation already validated, so after all staging a complete re-verification
# resolves every referenced credential through the Core resolver. Its failure
# fails the whole transaction and rolls every staged change back.


def _cloud_and_local_config():
    config = _local_config()
    cloud = _cloud_config()
    config["devices"] += cloud["devices"]
    config["zendure_mqtt"]["brokers"].update(cloud["zendure_mqtt"]["brokers"])
    return config


def test_validate_all_runtime_credentials_passes_when_every_record_resolves(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("home", "ems", SECRET)
    _save_complete_cloud_record(store)
    assert (
        validate_all_runtime_credentials(
            _cloud_and_local_config(), credential_store=store
        )
        == []
    )


def test_validate_all_runtime_credentials_reports_removed_record(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("home", "ems", SECRET)
    _save_complete_cloud_record(store)
    _record_path(store, "home").unlink()  # removed after it was validated
    assert validate_all_runtime_credentials(
        _cloud_and_local_config(), credential_store=store
    ) == ["home"]


def test_validate_all_runtime_credentials_reports_corrupted_record(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("home", "ems", SECRET)
    _save_complete_cloud_record(store)
    _corrupt_record(store, CLOUD_REF)
    assert validate_all_runtime_credentials(
        _cloud_and_local_config(), credential_store=store
    ) == [CLOUD_REF]


def test_validate_all_runtime_credentials_reports_wrong_source(tmp_path):
    # A cloud ref backed by a plain local record is not a usable cloud runtime
    # credential even though it resolves as username/password.
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("home", "ems", SECRET)
    store.save_mqtt_broker_secret(CLOUD_REF, "ems", SECRET)
    assert validate_all_runtime_credentials(
        _cloud_and_local_config(), credential_store=store
    ) == [CLOUD_REF]


class _SideEffectCloudDiscovery:
    """Real cloud provisioning that also invalidates an earlier local record.

    Models a later staging operation silently breaking a record an earlier
    operation already validated: the final verification must catch it.
    """

    def __init__(self, real, victim_ref):
        self._real = real
        self._victim_ref = victim_ref

    def provision_runtime_credentials(self, credential_store, *, ref, transaction):
        self._real.provision_runtime_credentials(
            credential_store, ref=ref, transaction=transaction
        )
        _corrupt_record(credential_store, self._victim_ref)


def test_stage_fails_and_rolls_back_when_later_staging_breaks_earlier_record(
    tmp_path,
):
    store = _store(tmp_path)
    store.save_mqtt_discovery_secret("home", "ems", SECRET)  # home is created
    store.zendure.save_token(API_KEY)  # cloud can provision
    real = _cloud_discovery(store, _CloudFetch())
    discovery = _SideEffectCloudDiscovery(real, victim_ref="home")

    with pytest.raises(CredentialStoreError) as excinfo:
        stage_runtime_credentials_for_config(
            _cloud_and_local_config(),
            credential_store=store,
            cloud_discovery=discovery,
        )
    # The affected reference is named (secret-free).
    assert "home" in str(excinfo.value)
    assert SECRET not in str(excinfo.value)
    # Both staged records were rolled back: the created local record is gone and
    # the provisioned cloud record is gone.
    assert not _record_path(store, "home").exists()
    assert not _record_path(store, CLOUD_REF).exists()


# --- staging decisions for existing records ---------------------------------


def test_stage_reuses_valid_local_record_without_rewrite(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_discovery_secret("home", "ems", SECRET)
    store.save_mqtt_broker_secret("home", "ems", SECRET)
    before = _record_path(store, "home").read_bytes()
    fetch = _CloudFetch()
    changes = stage_runtime_credentials_for_config(
        _local_config(),
        credential_store=store,
        cloud_discovery=_cloud_discovery(store, fetch),
    )
    assert changes == []
    assert _record_path(store, "home").read_bytes() == before
    assert fetch.calls == 0


def test_stage_blocks_on_undecryptable_local_record_without_replacement(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("home", "ems", SECRET)
    path = _corrupt_record(store, "home")
    before = path.read_bytes()
    with pytest.raises(CredentialStoreError) as excinfo:
        stage_runtime_credentials_for_config(
            _local_config(),
            credential_store=store,
            cloud_discovery=_cloud_discovery(store, _CloudFetch()),
        )
    assert "home" in str(excinfo.value)
    assert SECRET not in str(excinfo.value)
    # The broken record is left for the operator to inspect, not deleted.
    assert path.read_bytes() == before


def test_stage_blocks_empty_local_record_without_replacement(tmp_path):
    # No discovery replacement exists, so the empty record cannot be repaired:
    # the apply must fail instead of connecting the broker anonymously.
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("home", None, None)
    path = _record_path(store, "home")
    before = path.read_bytes()
    with pytest.raises(CredentialStoreError) as excinfo:
        stage_runtime_credentials_for_config(
            _local_config(),
            credential_store=store,
            cloud_discovery=_cloud_discovery(store, _CloudFetch()),
        )
    assert "home" in str(excinfo.value)
    assert path.read_bytes() == before


def test_stage_rotates_empty_local_record_from_discovery(tmp_path):
    # An empty record with a trusted discovery replacement is repaired instead
    # of being reused as anonymous credentials.
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("home", None, None)
    store.save_mqtt_discovery_secret("home", "ems", SECRET)
    changes = stage_runtime_credentials_for_config(
        _local_config(),
        credential_store=store,
        cloud_discovery=_cloud_discovery(store, _CloudFetch()),
    )
    resolved = FileMqttCredentialResolver(store.secrets_dir).resolve("home")
    assert (resolved.username, resolved.password) == ("ems", SECRET)
    assert [change.credentials_ref for change in changes] == ["home"]


def test_stage_ignores_anonymous_local_broker(tmp_path):
    # A broker without credentials_ref needs no authentication: no credential
    # validation, no provisioning, no secret write.
    store = _store(tmp_path)
    config = _local_config()
    del config["zendure_mqtt"]["brokers"]["local_home"]["credentials_ref"]
    fetch = _CloudFetch()
    changes = stage_runtime_credentials_for_config(
        config,
        credential_store=store,
        cloud_discovery=_cloud_discovery(store, fetch),
    )
    assert changes == []
    assert fetch.calls == 0
    assert list(store.secrets_dir.glob("mqtt-*.json")) == []


def test_stage_distrusts_label_only_discovery_credential(tmp_path):
    # A pool entry without a complete username/password pair (label-only save)
    # is no trusted replacement: staging must block instead of writing the
    # empty runtime record the completeness contract forbids.
    store = _store(tmp_path)
    store.save_mqtt_discovery_secret("home", None, None, label="Home broker")
    with pytest.raises(CredentialStoreError) as excinfo:
        stage_runtime_credentials_for_config(
            _local_config(),
            credential_store=store,
            cloud_discovery=_cloud_discovery(store, _CloudFetch()),
        )
    assert "home" in str(excinfo.value)
    assert not _record_path(store, "home").exists()


def test_stage_blocks_invalid_record_with_label_only_pool_entry(tmp_path):
    # An unusable runtime record plus a label-only pool entry has no repair
    # path: the apply is blocked and the broken record kept for inspection.
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("home", "ems", SECRET)
    path = _corrupt_record(store, "home")
    before = path.read_bytes()
    store.save_mqtt_discovery_secret("home", None, None, label="Home broker")
    with pytest.raises(CredentialStoreError) as excinfo:
        stage_runtime_credentials_for_config(
            _local_config(),
            credential_store=store,
            cloud_discovery=_cloud_discovery(store, _CloudFetch()),
        )
    assert "home" in str(excinfo.value)
    assert SECRET not in str(excinfo.value)
    assert path.read_bytes() == before


def test_stage_reprovisions_invalid_local_record_from_discovery(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_discovery_secret("home", "ems", SECRET)
    store.save_mqtt_broker_secret("home", "ems", SECRET)
    broken = _corrupt_record(store, "home").read_bytes()
    changes = stage_runtime_credentials_for_config(
        _local_config(),
        credential_store=store,
        cloud_discovery=_cloud_discovery(store, _CloudFetch()),
    )
    resolved = FileMqttCredentialResolver(store.secrets_dir).resolve("home")
    assert (resolved.username, resolved.password) == ("ems", SECRET)
    # The replacement is transactional: rolling the staged change back restores
    # the previous (broken) record byte for byte.
    assert [change.credentials_ref for change in changes] == ["home"]
    assert changes[0].existed_before is True
    assert store.rollback_credential_changes(changes) == []
    assert _record_path(store, "home").read_bytes() == broken


def test_stage_reuses_valid_cloud_record_without_network(tmp_path):
    store = _store(tmp_path)
    store.zendure.save_token(API_KEY)
    store.save_mqtt_cloud_runtime_secret(
        CLOUD_REF,
        username="cloud-user",
        password=SECRET,
        client_id="client-1",
        app_key="app-key-1",
    )
    fetch = _CloudFetch()
    changes = stage_runtime_credentials_for_config(
        _cloud_config(),
        credential_store=store,
        cloud_discovery=_cloud_discovery(store, fetch),
    )
    assert changes == []
    assert fetch.calls == 0


def test_stage_reprovisions_broken_cloud_record_with_api_key(tmp_path):
    store = _store(tmp_path)
    store.zendure.save_token(API_KEY)
    store.save_mqtt_cloud_runtime_secret(
        CLOUD_REF, username="cloud-user", password=SECRET
    )
    _corrupt_record(store, CLOUD_REF)
    fetch = _CloudFetch()
    changes = stage_runtime_credentials_for_config(
        _cloud_config(),
        credential_store=store,
        cloud_discovery=_cloud_discovery(store, fetch),
    )
    assert fetch.calls == 1
    resolved = FileMqttCredentialResolver(store.secrets_dir).resolve(CLOUD_REF)
    assert resolved.password == fetch.password
    # The pre-change snapshot allows a later apply failure to restore the
    # previous record instead of orphaning the rotation.
    assert [change.credentials_ref for change in changes] == [CLOUD_REF]
    assert changes[0].existed_before is True


@pytest.mark.parametrize("field", ("client_id", "app_key"))
def test_stage_reprovisions_cloud_record_missing_runtime_field(tmp_path, field):
    # A record holding only MQTT username/password cannot serve the runtime
    # (no cloud session/subscriptions); with the account key available it is
    # reprovisioned instead of being reused as-is.
    store = _store(tmp_path)
    store.zendure.save_token(API_KEY)
    _save_complete_cloud_record(store)
    _strip_field(store, CLOUD_REF, field)
    fetch = _CloudFetch()
    changes = stage_runtime_credentials_for_config(
        _cloud_config(),
        credential_store=store,
        cloud_discovery=_cloud_discovery(store, fetch),
    )
    assert fetch.calls == 1
    resolved = FileMqttCredentialResolver(store.secrets_dir).resolve(CLOUD_REF)
    assert resolved.client_id == fetch.client_id
    assert resolved.app_key == fetch.app_key
    assert [change.credentials_ref for change in changes] == [CLOUD_REF]


def test_stage_blocks_cloud_record_missing_app_key_without_api_key(tmp_path):
    store = _store(tmp_path)
    _save_complete_cloud_record(store)
    path = _strip_field(store, CLOUD_REF, "app_key")
    before = path.read_bytes()
    with pytest.raises(CredentialStoreError) as excinfo:
        stage_runtime_credentials_for_config(
            _cloud_config(),
            credential_store=store,
            cloud_discovery=_cloud_discovery(store, _CloudFetch()),
        )
    message = str(excinfo.value)
    assert CLOUD_REF in message
    assert SECRET not in message
    # The incomplete record is neither reported valid nor silently discarded.
    assert path.read_bytes() == before


def test_stage_blocks_broken_cloud_record_without_api_key(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_cloud_runtime_secret(
        CLOUD_REF, username="cloud-user", password=SECRET
    )
    path = _corrupt_record(store, CLOUD_REF)
    before = path.read_bytes()
    with pytest.raises(CredentialStoreError) as excinfo:
        stage_runtime_credentials_for_config(
            _cloud_config(),
            credential_store=store,
            cloud_discovery=_cloud_discovery(store, _CloudFetch()),
        )
    message = str(excinfo.value)
    assert CLOUD_REF in message
    assert SECRET not in message
    # The broken record is not reported valid and not silently discarded.
    assert path.read_bytes() == before


# --- rotation of changed local credentials ----------------------------------


def test_stage_rotates_local_record_to_new_discovery_password(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("home", "ems", "old-password")
    old_bytes = _record_path(store, "home").read_bytes()
    store.save_mqtt_discovery_secret("home", "ems", "new-password")
    changes = stage_runtime_credentials_for_config(
        _local_config(),
        credential_store=store,
        cloud_discovery=_cloud_discovery(store, _CloudFetch()),
    )
    resolved = FileMqttCredentialResolver(store.secrets_dir).resolve("home")
    assert (resolved.username, resolved.password) == ("ems", "new-password")
    mode = _record_path(store, "home").stat().st_mode & 0o777
    assert mode == 0o600
    # The rotation is transactional: the snapshot restores the old record
    # byte for byte on a later apply failure.
    assert [change.credentials_ref for change in changes] == ["home"]
    assert changes[0].existed_before is True
    assert store.rollback_credential_changes(changes) == []
    assert _record_path(store, "home").read_bytes() == old_bytes


def test_stage_rollback_restores_malformed_local_record_bytes(tmp_path):
    # Rollback must put back whatever bytes were on disk: a malformed record
    # is operator-owned evidence, not something to discard as nonexistent.
    store = _store(tmp_path)
    store.save_mqtt_discovery_secret("home", "ems", SECRET)
    path = _record_path(store, "home")
    path.write_bytes(b"{broken json")
    changes = stage_runtime_credentials_for_config(
        _local_config(),
        credential_store=store,
        cloud_discovery=_cloud_discovery(store, _CloudFetch()),
    )
    resolved = FileMqttCredentialResolver(store.secrets_dir).resolve("home")
    assert (resolved.username, resolved.password) == ("ems", SECRET)
    assert [change.credentials_ref for change in changes] == ["home"]
    assert changes[0].existed_before is True
    assert store.rollback_credential_changes(changes) == []
    assert path.read_bytes() == b"{broken json"


def test_stage_rotates_local_record_to_new_username(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("home", "old-user", SECRET)
    store.save_mqtt_discovery_secret("home", "new-user", SECRET)
    stage_runtime_credentials_for_config(
        _local_config(),
        credential_store=store,
        cloud_discovery=_cloud_discovery(store, _CloudFetch()),
    )
    resolved = FileMqttCredentialResolver(store.secrets_dir).resolve("home")
    assert (resolved.username, resolved.password) == ("new-user", SECRET)


def test_stage_creates_missing_local_record_from_discovery(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_discovery_secret("home", "ems", SECRET)
    changes = stage_runtime_credentials_for_config(
        _local_config(),
        credential_store=store,
        cloud_discovery=_cloud_discovery(store, _CloudFetch()),
    )
    assert [change.credentials_ref for change in changes] == ["home"]
    assert changes[0].existed_before is False
    resolved = FileMqttCredentialResolver(store.secrets_dir).resolve("home")
    assert (resolved.username, resolved.password) == ("ems", SECRET)
    # Rolling back a creation deletes the new record instead of restoring one.
    assert store.rollback_credential_changes(changes) == []
    assert not _record_path(store, "home").exists()


def test_stage_missing_local_record_without_discovery_source_writes_nothing(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(CredentialStoreError, match="was not found"):
        stage_runtime_credentials_for_config(
            _local_config(),
            credential_store=store,
            cloud_discovery=_cloud_discovery(store, _CloudFetch()),
        )
    assert not _record_path(store, "home").exists()


def _manual_broker_config(ref="local_mqtt"):
    return {
        "devices": [
            {
                "type": "zendure_mqtt",
                "enabled": True,
                "sn": "SN-MAN1",
                "mqtt": {"broker_ref": ref},
            }
        ],
        "zendure_mqtt": {
            "brokers": {
                ref: {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "10.0.0.20",
                    "port": 1883,
                    "credentials_ref": ref,
                }
            }
        },
    }


def test_setup_manual_broker_credentials_win_over_pool_entry(tmp_path):
    # The operator's just-typed manual broker credential is authoritative for
    # its ref: a discovery-pool entry that happens to share the same
    # normalized ref must not rotate the record back to its stale value.
    from admin.mqtt_runtime_provisioning import stage_setup_runtime_credentials

    store = _store(tmp_path)
    store.save_mqtt_discovery_secret("local_mqtt", "pool-user", "pool-password")
    changes = []
    stage_setup_runtime_credentials(
        _manual_broker_config(),
        {
            "name": "local_mqtt",
            "host": "10.0.0.20",
            "port": 1883,
            "username": "manual-user",
            "password": "manual-password",
        },
        changes,
        credential_store=store,
        cloud_discovery=_cloud_discovery(store, _CloudFetch()),
    )
    resolved = FileMqttCredentialResolver(store.secrets_dir).resolve("local_mqtt")
    assert (resolved.username, resolved.password) == ("manual-user", "manual-password")


def test_setup_manual_broker_reapply_keeps_manual_credentials(tmp_path):
    # A re-apply with unchanged manual credentials reuses the identical record
    # without staging a change — and still must not rotate it to a same-named
    # pool entry.
    from admin.mqtt_runtime_provisioning import stage_setup_runtime_credentials

    store = _store(tmp_path)
    store.save_mqtt_broker_secret("local_mqtt", "manual-user", "manual-password")
    store.save_mqtt_discovery_secret("local_mqtt", "pool-user", "pool-password")
    changes = []
    stage_setup_runtime_credentials(
        _manual_broker_config(),
        {
            "name": "local_mqtt",
            "host": "10.0.0.20",
            "port": 1883,
            "username": "manual-user",
            "password": "manual-password",
        },
        changes,
        credential_store=store,
        cloud_discovery=_cloud_discovery(store, _CloudFetch()),
    )
    assert changes == []
    resolved = FileMqttCredentialResolver(store.secrets_dir).resolve("local_mqtt")
    assert (resolved.username, resolved.password) == ("manual-user", "manual-password")


def test_setup_staging_collapses_duplicate_config_refs(tmp_path):
    # Two broker profiles sharing one credentials_ref stage a single record.
    from admin.mqtt_runtime_provisioning import stage_setup_runtime_credentials

    store = _store(tmp_path)
    store.save_mqtt_discovery_secret("home", "ems", SECRET)
    config = _local_config()
    config["zendure_mqtt"]["brokers"]["local_shed"] = {
        "enabled": True,
        "source": "local_mqtt",
        "host": "10.0.0.11",
        "port": 1883,
        "credentials_ref": "home",
    }
    config["devices"].append(
        {
            "type": "zendure_mqtt",
            "enabled": True,
            "sn": "SN-LOCAL2",
            "mqtt": {"broker_ref": "local_shed"},
        }
    )
    changes = []
    stage_setup_runtime_credentials(
        config,
        None,
        changes,
        credential_store=store,
        cloud_discovery=_cloud_discovery(store, _CloudFetch()),
    )
    assert [change.credentials_ref for change in changes] == ["home"]


def test_maintenance_apply_rotates_changed_local_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _existing_config())
    fetch = _CloudFetch()
    srv, base = _serve(
        tmp_path, fetch, local_observation=_local_observation(credentials_ref="home")
    )
    srv.credential_store.save_mqtt_discovery_secret("home", "ems", "old-password")
    try:
        proposal = _local_proposal(base)
        status, payload = _maintenance_apply_with_proposal(base, proposal)
        assert status == 200 and payload.get("ok") is True, payload

        # The broker password changed; discovery now carries the new value.
        srv.credential_store.save_mqtt_discovery_secret("home", "ems", "new-password")
        status, loaded = _request(f"{base}/api/admin/maintenance/config")
        assert status == 200 and loaded["status"] == "ok", loaded
        status, payload = _request(
            f"{base}/api/admin/maintenance/config/apply",
            "POST",
            {
                "draft": loaded["draft"],
                "revision": loaded["revision"],
                "confirm": True,
            },
        )
        assert status == 200 and payload.get("ok") is True, payload

        config_path, secrets_dir = _paths(tmp_path)
        resolved = FileMqttCredentialResolver(secrets_dir).resolve("home")
        assert resolved.password == "new-password"
        raw = config_path.read_text(encoding="utf-8")
        assert "old-password" not in raw and "new-password" not in raw
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_rotation_rolls_back_on_config_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _existing_config())
    fetch = _CloudFetch()
    srv, base = _serve(
        tmp_path, fetch, local_observation=_local_observation(credentials_ref="home")
    )
    srv.credential_store.save_mqtt_discovery_secret("home", "ems", "old-password")
    try:
        proposal = _local_proposal(base)
        status, payload = _maintenance_apply_with_proposal(base, proposal)
        assert status == 200 and payload.get("ok") is True, payload

        config_path, secrets_dir = _paths(tmp_path)
        config_before = config_path.read_bytes()
        record_before = (secrets_dir / "mqtt-home.json").read_bytes()
        srv.credential_store.save_mqtt_discovery_secret("home", "ems", "new-password")

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        srv.config_apply.apply_maintenance = _boom
        status, loaded = _request(f"{base}/api/admin/maintenance/config")
        assert status == 200 and loaded["status"] == "ok", loaded
        status, payload = _request(
            f"{base}/api/admin/maintenance/config/apply",
            "POST",
            {
                "draft": loaded["draft"],
                "revision": loaded["revision"],
                "confirm": True,
            },
        )
        assert status == 500, payload
        assert payload.get("ok") is False

        # The rotation is rolled back exactly: old record bytes, old password.
        assert (secrets_dir / "mqtt-home.json").read_bytes() == record_before
        assert config_path.read_bytes() == config_before
        resolved = FileMqttCredentialResolver(secrets_dir).resolve("home")
        assert resolved.password == "old-password"
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_config_failure_restores_malformed_record_bytes(
    monkeypatch, tmp_path
):
    # A config write failure after rotating over a malformed record must
    # restore the malformed original byte for byte, keeping the evidence.
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _existing_config())
    fetch = _CloudFetch()
    srv, base = _serve(
        tmp_path, fetch, local_observation=_local_observation(credentials_ref="home")
    )
    srv.credential_store.save_mqtt_discovery_secret("home", "ems", SECRET)
    try:
        proposal = _local_proposal(base)
        status, payload = _maintenance_apply_with_proposal(base, proposal)
        assert status == 200 and payload.get("ok") is True, payload

        config_path, secrets_dir = _paths(tmp_path)
        config_before = config_path.read_bytes()
        record_path = secrets_dir / "mqtt-home.json"
        record_path.write_bytes(b"{broken json")

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        srv.config_apply.apply_maintenance = _boom
        status, loaded = _request(f"{base}/api/admin/maintenance/config")
        assert status == 200 and loaded["status"] == "ok", loaded
        status, payload = _request(
            f"{base}/api/admin/maintenance/config/apply",
            "POST",
            {
                "draft": loaded["draft"],
                "revision": loaded["revision"],
                "confirm": True,
            },
        )
        assert status == 500, payload
        assert payload.get("ok") is False
        assert record_path.read_bytes() == b"{broken json"
        assert config_path.read_bytes() == config_before
    finally:
        srv.shutdown()
        srv.server_close()


def test_setup_apply_rotates_changed_local_credentials(tmp_path):
    from tests.test_admin_mqtt_credential_promotion_transaction import (
        _authorized,
        _discovery_with_proposal,
        _serve as _serve_setup,
        _write_body,
    )

    discovery = _discovery_with_proposal()
    srv, base = _serve_setup(discovery, tmp_path)
    srv.credential_store.save_mqtt_discovery_secret("home", "ems", "old-password")
    try:
        body = _authorized(base, _write_body(discovery))
        status, payload = _request(f"{base}/api/setup/config/apply", "POST", body)
        assert status == 200 and payload.get("ok") is True, payload

        srv.credential_store.save_mqtt_discovery_secret("home", "ems", "new-password")
        # The first apply changed the live config and consumed its preview, so
        # the re-apply is reviewed again, exactly as the browser re-previews.
        status, payload = _request(
            f"{base}/api/setup/config/apply",
            "POST",
            _authorized(base, _write_body(discovery)),
        )
        assert status == 200 and payload.get("ok") is True, payload
        # Setup and Maintenance share the rotation contract.
        secret = srv.credential_store.load_mqtt_broker_secret("home")
        assert secret is not None and secret.password == "new-password"
    finally:
        srv.shutdown()
        srv.server_close()


# --- rollback failures inside staging are surfaced, never swallowed ---------


def test_stage_surfaces_rollback_failure_of_earlier_staged_change(
    tmp_path, monkeypatch
):
    from admin import credential_store as credential_store_module

    store = _store(tmp_path)
    store.save_mqtt_discovery_secret("home", "ems", SECRET)
    config = _local_config()
    cloud = _cloud_config()
    config["devices"] += cloud["devices"]
    config["zendure_mqtt"]["brokers"].update(cloud["zendure_mqtt"]["brokers"])

    def _broken_delete(self, path):
        raise credential_store_module.CredentialStoreError(
            "Could not remove the secret file."
        )

    monkeypatch.setattr(
        credential_store_module._EncryptedFiles, "delete_file", _broken_delete
    )
    # The local record stages fine; the cloud ref then fails (no API key). The
    # rollback of the freshly created local record fails too — that failure
    # must ride on the raised error instead of disappearing.
    with pytest.raises(CredentialStoreError) as excinfo:
        stage_runtime_credentials_for_config(
            config,
            credential_store=store,
            cloud_discovery=_cloud_discovery(store, _CloudFetch()),
        )
    assert tuple(getattr(excinfo.value, "rollback_failed_refs", ())) == ("home",)
    assert SECRET not in str(excinfo.value)


def test_stage_preserves_provisioning_rollback_metadata_for_invalid_record(
    tmp_path, monkeypatch
):
    # Invalid existing record -> trusted reprovisioning -> verification fails
    # -> restoring the previous record fails. The structured provisioning
    # error (credentials_ref + rollback_failed_refs) must reach the caller
    # even though the staging layer adds context for invalid records.
    import ems.mqtt_credentials as mqtt_credentials
    from admin.credential_store import CredentialProvisioningError

    store = _store(tmp_path)
    store.zendure.save_token(API_KEY)
    _save_complete_cloud_record(store)
    _corrupt_record(store, CLOUD_REF)

    def _broken_resolve(self, ref):
        raise mqtt_credentials.MqttCredentialError("verification failed")

    monkeypatch.setattr(
        mqtt_credentials.FileMqttCredentialResolver, "resolve", _broken_resolve
    )
    _patch_restore_failure(monkeypatch)
    with pytest.raises(CredentialProvisioningError) as excinfo:
        stage_runtime_credentials_for_config(
            _cloud_config(),
            credential_store=store,
            cloud_discovery=_cloud_discovery(store, _CloudFetch()),
        )
    exc = excinfo.value
    assert exc.credentials_ref == CLOUD_REF
    assert tuple(exc.rollback_failed_refs) == (CLOUD_REF,)
    assert SECRET not in str(exc)


def test_stage_merges_rollback_failures_across_layers(tmp_path, monkeypatch):
    # A cloud rollback failure reported by the provisioning helper and a local
    # rollback failure discovered by the staging layer must both be named —
    # refs only, no secret values.
    import ems.mqtt_credentials as mqtt_credentials
    from admin import credential_store as credential_store_module
    from admin.credential_store import CredentialProvisioningError

    store = _store(tmp_path)
    store.zendure.save_token(API_KEY)
    store.save_mqtt_discovery_secret("home", "ems", SECRET)
    _save_complete_cloud_record(store)
    _corrupt_record(store, CLOUD_REF)

    config = _local_config()
    cloud = _cloud_config()
    config["devices"] += cloud["devices"]
    config["zendure_mqtt"]["brokers"].update(cloud["zendure_mqtt"]["brokers"])

    def _broken_resolve(self, ref):
        raise mqtt_credentials.MqttCredentialError("verification failed")

    def _broken_delete(self, path):
        raise credential_store_module.CredentialStoreError(
            "Could not remove the secret file."
        )

    monkeypatch.setattr(
        mqtt_credentials.FileMqttCredentialResolver, "resolve", _broken_resolve
    )
    _patch_restore_failure(monkeypatch)
    monkeypatch.setattr(
        credential_store_module._EncryptedFiles, "delete_file", _broken_delete
    )
    with pytest.raises(CredentialProvisioningError) as excinfo:
        stage_runtime_credentials_for_config(
            config,
            credential_store=store,
            cloud_discovery=_cloud_discovery(store, _CloudFetch()),
        )
    refs = tuple(excinfo.value.rollback_failed_refs)
    assert set(refs) == {"home", CLOUD_REF}
    assert len(refs) == 2
    assert SECRET not in str(excinfo.value)


def test_maintenance_apply_rollback_contract_for_invalid_cloud_record(
    monkeypatch, tmp_path
):
    # The complete HTTP contract when reprovisioning an invalid record and its
    # rollback both fail: stable code plus a high-severity credential_rollback
    # block naming the refs.
    import ems.mqtt_credentials as mqtt_credentials

    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    config_path = _write_config(tmp_path, _existing_config())
    fetch = _CloudFetch()
    srv, base = _serve(tmp_path, fetch)
    try:
        proposal = _cloud_proposal(base)
        status, payload = _maintenance_apply_with_proposal(base, proposal)
        assert status == 200 and payload.get("ok") is True, payload
        original = config_path.read_bytes()
        _corrupt_record(srv.credential_store, CLOUD_REF)

        def _broken_resolve(self, ref):
            raise mqtt_credentials.MqttCredentialError("verification failed")

        monkeypatch.setattr(
            mqtt_credentials.FileMqttCredentialResolver, "resolve", _broken_resolve
        )
        _patch_restore_failure(monkeypatch)
        status, loaded = _request(f"{base}/api/admin/maintenance/config")
        assert status == 200 and loaded["status"] == "ok", loaded
        status, payload = _request(
            f"{base}/api/admin/maintenance/config/apply",
            "POST",
            {
                "draft": loaded["draft"],
                "revision": loaded["revision"],
                "confirm": True,
            },
        )
        assert status == 400, payload
        assert payload.get("ok") is False
        assert payload.get("code") == "credential_provisioning_failed"
        rollback = payload.get("credential_rollback")
        assert rollback is not None, payload
        assert rollback["severity"] == "high"
        assert rollback["failed_refs"] == [CLOUD_REF]
        blob = json.dumps(payload)
        for secret in (API_KEY, fetch.password, fetch.app_key, fetch.client_id):
            assert secret not in blob
        assert config_path.read_bytes() == original
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_apply_reports_provisioning_rollback_failure(
    monkeypatch, tmp_path
):
    import ems.mqtt_credentials as mqtt_credentials
    from admin import credential_store as credential_store_module

    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    config_path = _write_config(tmp_path, _existing_config())
    original = config_path.read_bytes()
    fetch = _CloudFetch()
    srv, base = _serve(tmp_path, fetch)
    try:
        proposal = _cloud_proposal(base)

        def _broken_resolve(self, ref):
            raise mqtt_credentials.MqttCredentialError("verification failed")

        def _broken_delete(self, path):
            raise credential_store_module.CredentialStoreError(
                "Could not remove the secret file."
            )

        monkeypatch.setattr(
            mqtt_credentials.FileMqttCredentialResolver, "resolve", _broken_resolve
        )
        monkeypatch.setattr(
            credential_store_module._EncryptedFiles, "delete_file", _broken_delete
        )
        status, payload = _maintenance_apply_with_proposal(base, proposal)
        assert status >= 400, payload
        assert payload.get("ok") is False
        assert payload.get("code") == "credential_provisioning_failed"
        rollback = payload.get("credential_rollback")
        assert rollback is not None, payload
        assert rollback["severity"] == "high"
        assert rollback["failed_refs"] == [CLOUD_REF]
        assert rollback["message"]
        blob = json.dumps(payload)
        for secret in (API_KEY, fetch.password, fetch.app_key, fetch.client_id):
            assert secret not in blob
        assert config_path.read_bytes() == original
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_apply_successful_internal_rollback_is_not_flagged(
    monkeypatch, tmp_path
):
    import ems.mqtt_credentials as mqtt_credentials

    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    config_path = _write_config(tmp_path, _existing_config())
    original = config_path.read_bytes()
    fetch = _CloudFetch()
    srv, base = _serve(tmp_path, fetch)
    try:
        proposal = _cloud_proposal(base)

        def _broken_resolve(self, ref):
            raise mqtt_credentials.MqttCredentialError("verification failed")

        monkeypatch.setattr(
            mqtt_credentials.FileMqttCredentialResolver, "resolve", _broken_resolve
        )
        status, payload = _maintenance_apply_with_proposal(base, proposal)
        assert status >= 400, payload
        assert payload.get("ok") is False
        assert payload.get("code") == "credential_provisioning_failed"
        # The internal rollback succeeded: a normal provisioning error, no
        # high-severity rollback section, no orphan record.
        assert "credential_rollback" not in payload, payload
        assert config_path.read_bytes() == original
        _, secrets_dir = _paths(tmp_path)
        assert not (secrets_dir / f"mqtt-{CLOUD_REF}.json").exists()
    finally:
        srv.shutdown()
        srv.server_close()


# --- Maintenance apply end to end -------------------------------------------


def test_maintenance_apply_blocks_on_undecryptable_local_credential(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _existing_config())
    fetch = _CloudFetch()
    srv, base = _serve(
        tmp_path, fetch, local_observation=_local_observation(credentials_ref="home")
    )
    srv.credential_store.save_mqtt_discovery_secret("home", "ems", SECRET)
    try:
        proposal = _local_proposal(base)
        status, payload = _maintenance_apply_with_proposal(base, proposal)
        assert status == 200 and payload.get("ok") is True, payload

        config_path, secrets_dir = _paths(tmp_path)
        original = config_path.read_bytes()
        _corrupt_record(srv.credential_store, "home")
        srv.credential_store.forget_mqtt_discovery_secret("home")
        broken = (secrets_dir / "mqtt-home.json").read_bytes()

        # Re-apply the unchanged config: the broken record must fail the apply,
        # not pass as runtime-ready just because the file exists.
        status, loaded = _request(f"{base}/api/admin/maintenance/config")
        assert status == 200 and loaded["status"] == "ok", loaded
        status, payload = _request(
            f"{base}/api/admin/maintenance/config/apply",
            "POST",
            {
                "draft": loaded["draft"],
                "revision": loaded["revision"],
                "confirm": True,
            },
        )
        assert status >= 400, payload
        assert payload.get("ok") is False
        assert "home" in json.dumps(payload)
        assert SECRET not in json.dumps(payload)
        assert config_path.read_bytes() == original
        assert (secrets_dir / "mqtt-home.json").read_bytes() == broken
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_apply_blocks_empty_local_credential_record(
    monkeypatch, tmp_path
):
    # An authenticated broker whose record decayed to an empty (anonymous)
    # one, with no discovery replacement, must fail the apply instead of
    # silently downgrading the broker to anonymous access.
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _existing_config())
    fetch = _CloudFetch()
    srv, base = _serve(
        tmp_path, fetch, local_observation=_local_observation(credentials_ref="home")
    )
    srv.credential_store.save_mqtt_discovery_secret("home", "ems", SECRET)
    try:
        proposal = _local_proposal(base)
        status, payload = _maintenance_apply_with_proposal(base, proposal)
        assert status == 200 and payload.get("ok") is True, payload

        config_path, secrets_dir = _paths(tmp_path)
        original = config_path.read_bytes()
        srv.credential_store.save_mqtt_broker_secret("home", None, None)
        srv.credential_store.forget_mqtt_discovery_secret("home")
        empty = (secrets_dir / "mqtt-home.json").read_bytes()

        status, loaded = _request(f"{base}/api/admin/maintenance/config")
        assert status == 200 and loaded["status"] == "ok", loaded
        status, payload = _request(
            f"{base}/api/admin/maintenance/config/apply",
            "POST",
            {
                "draft": loaded["draft"],
                "revision": loaded["revision"],
                "confirm": True,
            },
        )
        assert status >= 400, payload
        assert payload.get("ok") is False
        assert "home" in json.dumps(payload)
        assert config_path.read_bytes() == original
        assert (secrets_dir / "mqtt-home.json").read_bytes() == empty
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_apply_repairs_broken_cloud_credential(monkeypatch, tmp_path):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _existing_config())
    fetch = _CloudFetch()
    srv, base = _serve(tmp_path, fetch)
    try:
        proposal = _cloud_proposal(base)
        status, payload = _maintenance_apply_with_proposal(base, proposal)
        assert status == 200 and payload.get("ok") is True, payload
        assert fetch.calls >= 1
        calls_after_first_apply = fetch.calls

        _corrupt_record(srv.credential_store, CLOUD_REF)

        status, loaded = _request(f"{base}/api/admin/maintenance/config")
        assert status == 200 and loaded["status"] == "ok", loaded
        status, payload = _request(
            f"{base}/api/admin/maintenance/config/apply",
            "POST",
            {
                "draft": loaded["draft"],
                "revision": loaded["revision"],
                "confirm": True,
            },
        )
        assert status == 200 and payload.get("ok") is True, payload
        # The broken record was reprovisioned through the Zendure API, and the
        # EMS runtime reconstructs without Admin state.
        assert fetch.calls == calls_after_first_apply + 1
        config_path, secrets_dir = _paths(tmp_path)
        resolver = FileMqttCredentialResolver(secrets_dir)
        assert resolver.resolve(CLOUD_REF).password == fetch.password
        config = json.loads(config_path.read_text(encoding="utf-8"))
        summary = _cloud_broker_summary(config, resolver)
        assert summary is not None
        assert summary.get("issue") is None, summary
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("field", ("client_id", "app_key"))
def test_maintenance_apply_blocks_incomplete_device_list_credentials(
    monkeypatch, tmp_path, field
):
    # A deviceList response without client_id/app_key cannot yield a
    # runtime-usable cloud record; the apply must fail before config.json
    # changes instead of staging a record the EMS cannot subscribe with.
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    config_path = _write_config(tmp_path, _existing_config())
    original = config_path.read_bytes()
    fetch = _CloudFetch()
    srv, base = _serve(tmp_path, fetch)
    try:
        proposal = _cloud_proposal(base)
        setattr(fetch, field, None)
        status, payload = _maintenance_apply_with_proposal(base, proposal)
        assert status >= 400, payload
        assert payload.get("ok") is False
        assert payload.get("reason") == "credential_provisioning_failed"
        blob = json.dumps(payload)
        assert CLOUD_REF in blob
        for secret in (API_KEY, fetch.password):
            assert secret not in blob
        assert config_path.read_bytes() == original
        _, secrets_dir = _paths(tmp_path)
        assert not (secrets_dir / f"mqtt-{CLOUD_REF}.json").exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_apply_reuses_valid_cloud_credential_without_refetch(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _existing_config())
    fetch = _CloudFetch()
    srv, base = _serve(tmp_path, fetch)
    try:
        proposal = _cloud_proposal(base)
        status, payload = _maintenance_apply_with_proposal(base, proposal)
        assert status == 200 and payload.get("ok") is True, payload
        calls_after_first_apply = fetch.calls

        status, loaded = _request(f"{base}/api/admin/maintenance/config")
        assert status == 200 and loaded["status"] == "ok", loaded
        status, payload = _request(
            f"{base}/api/admin/maintenance/config/apply",
            "POST",
            {
                "draft": loaded["draft"],
                "revision": loaded["revision"],
                "confirm": True,
            },
        )
        assert status == 200 and payload.get("ok") is True, payload
        # The existing valid record is reused: no new deviceList round-trip.
        assert fetch.calls == calls_after_first_apply
    finally:
        srv.shutdown()
        srv.server_close()
