# SPDX-License-Identifier: AGPL-3.0-or-later
"""A required credential field must carry a non-whitespace value.

A record whose username/password (local) or username/password/client_id/app_key
(cloud) is present but only whitespace would pass a bare ``isinstance(value, str)
and value`` check yet the EMS runtime cannot authenticate or subscribe with it.
The completeness contract therefore requires ``value.strip()`` to be non-empty,
and Admin and the runtime must reach the same valid/invalid verdict. A genuine
password with leading/trailing spaces is preserved byte-for-byte — only its
emptiness after stripping is tested, never its stored value.
"""

import json

import pytest

from admin.credential_store import (
    CredentialStore,
    validate_resolved_mqtt_credential,
)
from ems.mqtt_credentials import FileMqttCredentialResolver
from ems.zendure_mqtt.config_entries import (
    SOURCE_LOCAL_MQTT,
    SOURCE_ZENDURE_CLOUD_MQTT,
)
from ems.zendure_mqtt.runtime import _cloud_runtime_ready

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.contract,
    pytest.mark.simulation,
]

# The genuinely-whitespace values that used to slip through a non-empty check.
WHITESPACE = [" ", "   ", "\t", "\n", "\r\n"]
BLANK_OR_MISSING = [None, ""]


def _store(tmp_path):
    return CredentialStore(config_dir=tmp_path / "config")


def _set_field(store, ref, field, value):
    """Overwrite one stored field with an (encrypted) raw value."""

    path = store.secrets_dir / f"mqtt-{ref}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    blob, encrypted = store._files.encrypt(value)
    record[field] = blob
    record[f"{field}_encrypted"] = encrypted
    path.write_text(json.dumps(record), encoding="utf-8")


# --- completeness helper: the one shared valid/invalid contract -------------


@pytest.mark.parametrize("blank", WHITESPACE + BLANK_OR_MISSING)
@pytest.mark.parametrize("field", ["username", "password"])
def test_local_record_rejects_blank_field(field, blank):
    record = {"username": "user", "password": "pass"}
    record[field] = blank
    result = validate_resolved_mqtt_credential(
        credentials_ref="r", source=SOURCE_LOCAL_MQTT, resolved=record
    )
    assert result.status == "invalid"


@pytest.mark.parametrize("blank", WHITESPACE + BLANK_OR_MISSING)
@pytest.mark.parametrize("field", ["username", "password", "client_id", "app_key"])
def test_cloud_record_rejects_blank_field(field, blank):
    record = {
        "username": "user",
        "password": "pass",
        "client_id": "cid",
        "app_key": "ak",
    }
    record[field] = blank
    result = validate_resolved_mqtt_credential(
        credentials_ref="r", source=SOURCE_ZENDURE_CLOUD_MQTT, resolved=record
    )
    assert result.status == "invalid"


def test_local_record_with_both_fields_whitespace_is_invalid():
    result = validate_resolved_mqtt_credential(
        credentials_ref="r",
        source=SOURCE_LOCAL_MQTT,
        resolved={"username": "  ", "password": "\t"},
    )
    assert result.status == "invalid"


def test_complete_record_stays_valid():
    result = validate_resolved_mqtt_credential(
        credentials_ref="r",
        source=SOURCE_LOCAL_MQTT,
        resolved={"username": "user", "password": "pass"},
    )
    assert result.status == "valid"


# --- runtime records on disk ------------------------------------------------


@pytest.mark.parametrize("blank", WHITESPACE)
def test_local_runtime_record_with_whitespace_password_is_invalid(tmp_path, blank):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("home", "user", "pass")
    _set_field(store, "home", "password", blank)
    result = store.validate_runtime_credential("home", expected_source=SOURCE_LOCAL_MQTT)
    assert result.status == "invalid"


@pytest.mark.parametrize("blank", WHITESPACE)
def test_local_runtime_record_with_whitespace_username_is_invalid(tmp_path, blank):
    store = _store(tmp_path)
    store.save_mqtt_broker_secret("home", "user", "pass")
    _set_field(store, "home", "username", blank)
    result = store.validate_runtime_credential("home", expected_source=SOURCE_LOCAL_MQTT)
    assert result.status == "invalid"


@pytest.mark.parametrize("field", ["username", "password", "client_id", "app_key"])
@pytest.mark.parametrize("blank", WHITESPACE)
def test_cloud_runtime_record_with_whitespace_field_is_invalid(tmp_path, field, blank):
    store = _store(tmp_path)
    store.save_mqtt_cloud_runtime_secret(
        "zendure-cloud",
        username="cloud-user",
        password="cloud-pass",
        client_id="cid",
        app_key="ak",
    )
    _set_field(store, "zendure-cloud", field, blank)
    result = store.validate_runtime_credential(
        "zendure-cloud", expected_source=SOURCE_ZENDURE_CLOUD_MQTT
    )
    assert result.status == "invalid"


# --- Admin and runtime agree on the same record -----------------------------


@pytest.mark.parametrize("field", ["username", "password", "client_id", "app_key"])
def test_admin_and_runtime_agree_on_whitespace_cloud_record(tmp_path, field):
    store = _store(tmp_path)
    store.save_mqtt_cloud_runtime_secret(
        "zendure-cloud",
        username="cloud-user",
        password="cloud-pass",
        client_id="cid",
        app_key="ak",
    )
    _set_field(store, "zendure-cloud", field, "   ")

    resolved = FileMqttCredentialResolver(store.secrets_dir).resolve("zendure-cloud")
    admin_valid = (
        validate_resolved_mqtt_credential(
            credentials_ref="zendure-cloud",
            source=SOURCE_ZENDURE_CLOUD_MQTT,
            resolved=resolved,
        ).status
        == "valid"
    )
    runtime_ready = _cloud_runtime_ready(resolved)
    assert admin_valid is False
    assert admin_valid == runtime_ready


# --- a real password with surrounding spaces is preserved, never trimmed ----


def test_password_with_surrounding_spaces_is_preserved_and_valid(tmp_path):
    store = _store(tmp_path)
    padded = "  s3cr3t  "
    store.save_mqtt_broker_secret("home", "user", padded)

    result = store.validate_runtime_credential("home", expected_source=SOURCE_LOCAL_MQTT)
    assert result.status == "valid"

    resolved = FileMqttCredentialResolver(store.secrets_dir).resolve("home")
    # The stored value keeps its surrounding spaces exactly.
    assert resolved.password == padded


def test_cloud_field_with_surrounding_spaces_is_preserved_and_valid(tmp_path):
    store = _store(tmp_path)
    padded_key = "  app key  "
    store.save_mqtt_cloud_runtime_secret(
        "zendure-cloud",
        username="cloud-user",
        password="cloud-pass",
        client_id="cid",
        app_key=padded_key,
    )
    result = store.validate_runtime_credential(
        "zendure-cloud", expected_source=SOURCE_ZENDURE_CLOUD_MQTT
    )
    assert result.status == "valid"
    resolved = FileMqttCredentialResolver(store.secrets_dir).resolve("zendure-cloud")
    assert resolved.app_key == padded_key
