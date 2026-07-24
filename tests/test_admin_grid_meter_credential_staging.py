# SPDX-License-Identifier: AGPL-3.0-or-later
"""Direct MQTT grid meters obey the same global credential contract as brokers.

A direct grid meter (``grid_meter.mqtt.credentials_ref``) is a local MQTT
credential consumer: its reference must be canonical, must not conflict with a
cloud source, must be staged and reprovisioned like any other local record, and
must be revalidated in the final transaction boundary. These contracts run
through the one shared staging path, never a grid-meter-specific transaction.
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
    runtime_credential_requirements,
    stage_runtime_credentials_for_config,
    validate_all_runtime_credentials,
    validate_config_credential_references,
)
from admin.zendure_cloud_mqtt import ZendureCloudDiscovery
from ems.mqtt_credentials import FileMqttCredentialResolver
from tests.test_admin_maintenance_mqtt_apply import _CloudFetch

pytestmark = pytest.mark.simulation

SECRET = "super-secret-password"
API_KEY = "raw-account-api-key"
CLOUD_REF = "zendure-cloud"


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _store(tmp_path):
    return CredentialStore(config_dir=tmp_path / "config")


def _record_path(store, ref):
    return store.secrets_dir / f"mqtt-{ref}.json"


def _strip_field(store, ref, field):
    """Remove one stored field, keeping the rest of the record intact."""

    path = _record_path(store, ref)
    record = json.loads(path.read_text(encoding="utf-8"))
    record.pop(field, None)
    record.pop(f"{field}_encrypted", None)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


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


def _cloud_discovery(store, fetch):
    return ZendureCloudDiscovery(
        store.zendure, device_list_fetcher=fetch, listener_factory=lambda c: None
    )


def _direct_grid_meter_config(ref="grid-meter"):
    return {
        "grid_meter": {
            "type": "mqtt",
            "mqtt": {
                "host": "broker.local",
                "port": 1883,
                "topic": "meter/power",
                "credentials_ref": ref,
            },
        }
    }


# --- Phase 2: canonical direct grid-meter references ------------------------
# A direct grid meter must reject a non-canonical reference before any
# credential mutation, config write or network call — Admin never normalizes a
# configured reference behind the config's back.

_NON_CANONICAL_REFS = [
    "Bad Ref",
    "Home",
    "../secret",
    "broker/ref",
    "broker\\",
    "mqtt:home",
    " leading",
    "trailing ",
    "",
]


@pytest.mark.parametrize("ref", _NON_CANONICAL_REFS)
def test_validate_rejects_non_canonical_direct_grid_meter_ref(tmp_path, ref):
    with pytest.raises(MqttCredentialsRefInvalidError) as excinfo:
        validate_config_credential_references(_direct_grid_meter_config(ref))
    assert excinfo.value.code == "mqtt_credentials_ref_invalid"
    assert excinfo.value.credentials_ref == ref


@pytest.mark.parametrize("ref", _NON_CANONICAL_REFS)
def test_stage_rejects_non_canonical_direct_grid_meter_ref_before_any_write(
    tmp_path, ref
):
    store = _store(tmp_path)
    fetch = _CloudFetch()
    with pytest.raises(MqttCredentialsRefInvalidError) as excinfo:
        stage_runtime_credentials_for_config(
            _direct_grid_meter_config(ref),
            credential_store=store,
            cloud_discovery=_cloud_discovery(store, fetch),
        )
    assert excinfo.value.credentials_ref == ref
    assert SECRET not in str(excinfo.value)
    # No credential file was created for the (would-be normalized) reference.
    assert not store.secrets_dir.exists() or (
        list(store.secrets_dir.glob("mqtt-*.json")) == []
    )
    assert fetch.calls == 0


@pytest.mark.parametrize("ref", ["grid-meter", "garage_mqtt", "broker-01"])
def test_validate_accepts_canonical_direct_grid_meter_ref(tmp_path, ref):
    # A canonical reference passes the syntax gate unchanged (whether or not a
    # record exists yet is the staging step's concern, not this gate's).
    validate_config_credential_references(_direct_grid_meter_config(ref))


def test_stage_accepts_canonical_direct_grid_meter_ref_with_record(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("grid-meter", "ems", SECRET)
    store.save_mqtt_discovery_secret("grid-meter", "ems", SECRET)
    stage_runtime_credentials_for_config(
        _direct_grid_meter_config("grid-meter"),
        credential_store=store,
        cloud_discovery=_cloud_discovery(store, _CloudFetch()),
    )
    resolved = FileMqttCredentialResolver(store.secrets_dir).resolve("grid-meter")
    assert (resolved.username, resolved.password) == ("ems", SECRET)


# --- Phase 3: source conflicts across the complete config -------------------
# A direct grid meter is a local consumer. A ref it shares with a Zendure Cloud
# broker is a cross-source conflict: cloud provisioning would overwrite the
# local record. Same-source sharing (grid meter + local broker) stays allowed.


def _grid_meter_and_cloud_broker_config(ref="shared"):
    return {
        "devices": [
            {
                "type": "zendure_mqtt",
                "enabled": True,
                "sn": "SN-CLOUD1",
                "mqtt": {"broker_ref": "cloud-main"},
            }
        ],
        "grid_meter": {
            "type": "mqtt",
            "mqtt": {
                "host": "broker.local",
                "port": 1883,
                "topic": "meter/power",
                "credentials_ref": ref,
            },
        },
        "zendure_mqtt": {
            "brokers": {
                "cloud-main": {
                    "enabled": True,
                    "source": "zendure_cloud_mqtt",
                    "host": "mqtteu.zen-iot.com",
                    "port": 8883,
                    "credentials_ref": ref,
                }
            }
        },
    }


def _grid_meter_and_local_broker_config(ref="home"):
    return {
        "devices": [
            {
                "type": "zendure_mqtt",
                "enabled": True,
                "sn": "SN-LOCAL1",
                "mqtt": {"broker_ref": "local-main"},
            }
        ],
        "grid_meter": {
            "type": "mqtt",
            "mqtt": {
                "host": "broker.local",
                "port": 1883,
                "topic": "meter/power",
                "credentials_ref": ref,
            },
        },
        "zendure_mqtt": {
            "brokers": {
                "local-main": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "10.0.0.10",
                    "port": 1883,
                    "credentials_ref": ref,
                }
            }
        },
    }


def test_validate_rejects_direct_grid_meter_and_cloud_broker_sharing_ref(tmp_path):
    with pytest.raises(MqttCredentialSourceConflictError) as excinfo:
        validate_config_credential_references(_grid_meter_and_cloud_broker_config())
    assert excinfo.value.code == "mqtt_credential_source_conflict"
    assert excinfo.value.credentials_ref == "shared"
    assert sorted(excinfo.value.sources) == ["local_mqtt", "zendure_cloud_mqtt"]
    assert sorted(getattr(excinfo.value, "consumers", [])) == [
        "grid_meter",
        "zendure_mqtt_broker",
    ]


def test_stage_blocks_direct_grid_meter_cloud_conflict_before_mutation(tmp_path):
    store = _store(tmp_path)
    store.zendure.save_token(API_KEY)
    # A pre-existing local record under the shared ref must survive untouched.
    store.save_mqtt_broker_secret("shared", "ems", SECRET)
    before = _record_path(store, "shared").read_bytes()
    fetch = _CloudFetch()
    with pytest.raises(MqttCredentialSourceConflictError) as excinfo:
        stage_runtime_credentials_for_config(
            _grid_meter_and_cloud_broker_config(),
            credential_store=store,
            cloud_discovery=_cloud_discovery(store, fetch),
        )
    assert excinfo.value.credentials_ref == "shared"
    assert SECRET not in str(excinfo.value)
    # No mutation and no cloud round-trip happened.
    assert fetch.calls == 0
    assert _record_path(store, "shared").read_bytes() == before


def test_validate_allows_direct_grid_meter_and_local_broker_sharing_ref(tmp_path):
    # Two local consumers may reuse one local credential record.
    validate_config_credential_references(_grid_meter_and_local_broker_config())


# --- Phase 4: direct grid meters join runtime requirements and staging ------
# A direct grid meter's credential is required, staged and reprovisioned like
# any other local record — through the one shared staging path, not a
# grid-meter-specific transaction.


def test_requirements_include_direct_grid_meter_local_ref(tmp_path):
    requirements = runtime_credential_requirements(_direct_grid_meter_config("grid-meter"))
    assert requirements["local"] == {"grid-meter"}
    assert requirements["cloud"] == set()


def _source_less_named_broker_grid_meter_config(ref="gridcred"):
    return {
        "grid_meter": {
            "type": "mqtt",
            "mqtt": {"broker_ref": "b1", "topic": "meter/power"},
        },
        "zendure_mqtt": {
            "brokers": {
                "b1": {
                    "enabled": True,
                    "host": "broker.local",
                    "port": 1883,
                    "credentials_ref": ref,
                }
            }
        },
    }


def test_requirements_include_source_less_named_broker_grid_meter(tmp_path):
    # A grid meter using a source-less local broker is valid at runtime, so its
    # credential must be a local staging requirement.
    requirements = runtime_credential_requirements(
        _source_less_named_broker_grid_meter_config("gridcred")
    )
    assert requirements["local"] == {"gridcred"}
    assert requirements["cloud"] == set()


def test_stage_blocks_missing_source_less_named_broker_grid_meter_credential(tmp_path):
    # Without the requirement the apply would silently succeed and the grid
    # meter would fail to authenticate at runtime; staging must block instead.
    store = _store(tmp_path)
    with pytest.raises(CredentialStoreError):
        stage_runtime_credentials_for_config(
            _source_less_named_broker_grid_meter_config("gridcred"),
            credential_store=store,
            cloud_discovery=_cloud_discovery(store, _CloudFetch()),
        )
    assert not _record_path(store, "gridcred").exists()


def test_stage_reuses_valid_grid_meter_record_without_rewrite(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("grid-meter", "ems", SECRET)
    before = _record_path(store, "grid-meter").read_bytes()
    fetch = _CloudFetch()
    changes = stage_runtime_credentials_for_config(
        _direct_grid_meter_config("grid-meter"),
        credential_store=store,
        cloud_discovery=_cloud_discovery(store, fetch),
    )
    assert changes == []
    assert _record_path(store, "grid-meter").read_bytes() == before
    assert fetch.calls == 0


def test_stage_blocks_missing_grid_meter_credential_without_replacement(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(CredentialStoreError, match="was not found"):
        stage_runtime_credentials_for_config(
            _direct_grid_meter_config("grid-meter"),
            credential_store=store,
            cloud_discovery=_cloud_discovery(store, _CloudFetch()),
        )
    assert not _record_path(store, "grid-meter").exists()


def test_stage_creates_missing_grid_meter_record_from_discovery(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_discovery_secret("grid-meter", "ems", SECRET)
    changes = stage_runtime_credentials_for_config(
        _direct_grid_meter_config("grid-meter"),
        credential_store=store,
        cloud_discovery=_cloud_discovery(store, _CloudFetch()),
    )
    assert [change.credentials_ref for change in changes] == ["grid-meter"]
    resolved = FileMqttCredentialResolver(store.secrets_dir).resolve("grid-meter")
    assert (resolved.username, resolved.password) == ("ems", SECRET)
    # Rolling back a creation deletes the new record.
    assert store.rollback_credential_changes(changes) == []
    assert not _record_path(store, "grid-meter").exists()


def test_stage_blocks_empty_grid_meter_record(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("grid-meter", None, None)
    path = _record_path(store, "grid-meter")
    before = path.read_bytes()
    with pytest.raises(CredentialStoreError) as excinfo:
        stage_runtime_credentials_for_config(
            _direct_grid_meter_config("grid-meter"),
            credential_store=store,
            cloud_discovery=_cloud_discovery(store, _CloudFetch()),
        )
    assert "grid-meter" in str(excinfo.value)
    assert path.read_bytes() == before


def test_stage_blocks_username_only_grid_meter_record(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("grid-meter", "ems", SECRET)
    path = _strip_field(store, "grid-meter", "password")
    before = path.read_bytes()
    with pytest.raises(CredentialStoreError):
        stage_runtime_credentials_for_config(
            _direct_grid_meter_config("grid-meter"),
            credential_store=store,
            cloud_discovery=_cloud_discovery(store, _CloudFetch()),
        )
    assert path.read_bytes() == before


def test_stage_blocks_cloud_record_under_grid_meter_ref(tmp_path):
    # A cloud runtime record stored under a local grid-meter ref is the wrong
    # source: it cannot back a local grid meter and there is no trusted
    # replacement, so the apply is blocked.
    store = _store(tmp_path)
    store.save_mqtt_cloud_runtime_secret(
        "grid-meter",
        username="cloud-user",
        password=SECRET,
        client_id="cloud-client-1",
        app_key="cloud-app-key-1",
    )
    path = _record_path(store, "grid-meter")
    before = path.read_bytes()
    with pytest.raises(CredentialStoreError):
        stage_runtime_credentials_for_config(
            _direct_grid_meter_config("grid-meter"),
            credential_store=store,
            cloud_discovery=_cloud_discovery(store, _CloudFetch()),
        )
    assert path.read_bytes() == before


def test_stage_rotates_grid_meter_record_and_rolls_back_byte_exact(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("grid-meter", "ems", "old-password")
    old_bytes = _record_path(store, "grid-meter").read_bytes()
    store.save_mqtt_discovery_secret("grid-meter", "ems", "new-password")
    changes = stage_runtime_credentials_for_config(
        _direct_grid_meter_config("grid-meter"),
        credential_store=store,
        cloud_discovery=_cloud_discovery(store, _CloudFetch()),
    )
    resolved = FileMqttCredentialResolver(store.secrets_dir).resolve("grid-meter")
    assert (resolved.username, resolved.password) == ("ems", "new-password")
    assert [change.credentials_ref for change in changes] == ["grid-meter"]
    assert store.rollback_credential_changes(changes) == []
    assert _record_path(store, "grid-meter").read_bytes() == old_bytes


def test_stage_ignores_anonymous_direct_grid_meter(tmp_path):
    store = _store(tmp_path)
    config = {
        "grid_meter": {
            "type": "mqtt",
            "mqtt": {"host": "broker.local", "port": 1883, "topic": "meter/power"},
        }
    }
    fetch = _CloudFetch()
    changes = stage_runtime_credentials_for_config(
        config,
        credential_store=store,
        cloud_discovery=_cloud_discovery(store, fetch),
    )
    assert changes == []
    assert fetch.calls == 0
    assert list(store.secrets_dir.glob("mqtt-*.json")) == []


# --- Phase 5: direct grid meters in the final validation boundary -----------
# The last transaction boundary re-resolves every referenced credential — grid
# meters included — so a record a later staging step broke fails the apply
# before config.json is written.


def test_final_validation_reports_removed_grid_meter_record(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("grid-meter", "ems", SECRET)
    _record_path(store, "grid-meter").unlink()
    assert validate_all_runtime_credentials(
        _direct_grid_meter_config("grid-meter"), credential_store=store
    ) == ["grid-meter"]


def test_final_validation_reports_corrupted_grid_meter_record(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("grid-meter", "ems", SECRET)
    _corrupt_record(store, "grid-meter")
    assert validate_all_runtime_credentials(
        _direct_grid_meter_config("grid-meter"), credential_store=store
    ) == ["grid-meter"]


def test_final_validation_reports_cloud_record_under_grid_meter_ref(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_cloud_runtime_secret(
        "grid-meter",
        username="cloud-user",
        password=SECRET,
        client_id="cloud-client-1",
        app_key="cloud-app-key-1",
    )
    assert validate_all_runtime_credentials(
        _direct_grid_meter_config("grid-meter"), credential_store=store
    ) == ["grid-meter"]


def test_final_validation_reports_incomplete_grid_meter_record(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("grid-meter", "ems", SECRET)
    _strip_field(store, "grid-meter", "password")
    assert validate_all_runtime_credentials(
        _direct_grid_meter_config("grid-meter"), credential_store=store
    ) == ["grid-meter"]


def test_final_validation_passes_for_valid_grid_meter_record(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("grid-meter", "ems", SECRET)
    assert (
        validate_all_runtime_credentials(
            _direct_grid_meter_config("grid-meter"), credential_store=store
        )
        == []
    )


class _SideEffectCloudDiscovery:
    """Real cloud provisioning that also corrupts an earlier local record."""

    def __init__(self, real, victim_ref):
        self._real = real
        self._victim_ref = victim_ref

    def provision_runtime_credentials(self, credential_store, *, ref, transaction):
        self._real.provision_runtime_credentials(
            credential_store, ref=ref, transaction=transaction
        )
        _corrupt_record(credential_store, self._victim_ref)


def _grid_meter_and_cloud_broker_distinct_config():
    return {
        "devices": [
            {
                "type": "zendure_mqtt",
                "enabled": True,
                "sn": "SN-CLOUD1",
                "mqtt": {"broker_ref": "cloud-main"},
            }
        ],
        "grid_meter": {
            "type": "mqtt",
            "mqtt": {
                "host": "broker.local",
                "port": 1883,
                "topic": "meter/power",
                "credentials_ref": "grid-meter",
            },
        },
        "zendure_mqtt": {
            "brokers": {
                "cloud-main": {
                    "enabled": True,
                    "source": "zendure_cloud_mqtt",
                    "host": "mqtteu.zen-iot.com",
                    "port": 8883,
                    "credentials_ref": CLOUD_REF,
                }
            }
        },
    }


def test_stage_final_boundary_fails_when_grid_meter_broken_mid_staging(tmp_path):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("grid-meter", "ems", SECRET)  # reused as valid
    store.zendure.save_token(API_KEY)  # cloud can provision
    real = _cloud_discovery(store, _CloudFetch())
    discovery = _SideEffectCloudDiscovery(real, victim_ref="grid-meter")
    with pytest.raises(CredentialStoreError) as excinfo:
        stage_runtime_credentials_for_config(
            _grid_meter_and_cloud_broker_distinct_config(),
            credential_store=store,
            cloud_discovery=discovery,
        )
    # The affected grid-meter reference is named (secret-free) and the freshly
    # provisioned cloud record was rolled back.
    assert "grid-meter" in str(excinfo.value)
    assert SECRET not in str(excinfo.value)
    assert not _record_path(store, CLOUD_REF).exists()
