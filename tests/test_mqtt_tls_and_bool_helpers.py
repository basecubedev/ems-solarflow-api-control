# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared Core TLS-mode, strict-boolean and stable-ref helpers.

The lowest layer every MQTT discovery/preview path reuses so Admin and Core
agree on TLS semantics, never enable a flag by string truthiness, and generate a
broker ref that is stable across topology changes.
"""

import pytest

from ems import config as cfg
from ems.zendure_mqtt.config_entries import (
    normalized_broker_identity,
    stable_local_broker_ref,
)

pytestmark = pytest.mark.simulation


# --- TLS mode normalization (defect 1) --------------------------------------
@pytest.mark.parametrize(
    "mode,expected",
    [
        (None, (False, False)),
        ("", (False, False)),
        ("plaintext", (False, False)),
        ("disabled", (False, False)),
        ("system_ca", (True, False)),
        ("insecure_no_verify", (True, True)),
    ],
)
def test_normalize_mqtt_tls_mode(mode, expected):
    assert cfg.normalize_mqtt_tls_mode(mode) == expected


@pytest.mark.parametrize(
    "alias", ["", "plaintext", "plain", "disabled", "none", "tcp"]
)
def test_plain_aliases_resolve_to_plaintext(alias):
    assert cfg.normalize_mqtt_tls_mode(alias) == (False, False)


@pytest.mark.parametrize(
    "alias", ["system_ca", "tls", "mqtts", "ssl", "secure", "  TLS  "]
)
def test_system_ca_aliases_resolve_to_verified_tls(alias):
    assert cfg.normalize_mqtt_tls_mode(alias) == (True, False)


def test_unknown_tls_mode_raises_never_downgrades():
    with pytest.raises(ValueError):
        cfg.normalize_mqtt_tls_mode("totally_unknown")


# --- canonical mode names (one alias vocabulary) -----------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", ""),
        ("system_ca", "system_ca"),
        ("insecure_no_verify", "insecure_no_verify"),
        # Transport-security modes the Zendure cloud discovery client reports.
        ("encrypted_no_verify", "insecure_no_verify"),
        ("pinned_ca", "insecure_no_verify"),
        ("  Pinned_CA  ", "insecure_no_verify"),
        # Anything else is passed through untouched so strict validation, not a
        # silent rewrite, decides what an unknown mode means.
        ("totally_unknown", "totally_unknown"),
    ],
)
def test_canonical_mqtt_tls_mode(value, expected):
    assert cfg.canonical_mqtt_tls_mode(value) == expected


@pytest.mark.parametrize(
    "tls,tls_insecure,expected",
    [
        (False, False, None),
        (None, False, None),
        (True, False, "system_ca"),
        (True, True, "insecure_no_verify"),
    ],
)
def test_mqtt_tls_mode_name(tls, tls_insecure, expected):
    assert cfg.mqtt_tls_mode_name(tls=tls, tls_insecure=tls_insecure) == expected


def test_cloud_discovery_modes_come_from_core():
    from admin import zendure_cloud_mqtt as cloud

    assert set(cloud.TLS_MODES) == set(cfg.MQTT_TLS_OBSERVED_MODES)
    assert {cloud.TLS_SYSTEM_CA, cloud.TLS_PINNED_CA, cloud.TLS_ENCRYPTED_NO_VERIFY} == (
        set(cfg.MQTT_TLS_OBSERVED_MODES)
    )
    # Every observed mode a cloud connection can report resolves canonically.
    for mode in cfg.MQTT_TLS_OBSERVED_MODES:
        assert cfg.normalize_mqtt_tls_mode(cfg.canonical_mqtt_tls_mode(mode))[0] is True


def test_canonical_mode_names_round_trip_through_metadata():
    for name in ("system_ca", "insecure_no_verify"):
        tls, insecure = cfg.normalize_mqtt_tls_mode(name)
        assert cfg.mqtt_tls_mode_name(tls=tls, tls_insecure=insecure) == name


def test_resolve_tls_metadata_mode_is_authoritative():
    assert cfg.resolve_mqtt_tls_metadata(tls_mode="system_ca") == (True, False)
    assert cfg.resolve_mqtt_tls_metadata(tls_mode="insecure_no_verify") == (True, True)
    assert cfg.resolve_mqtt_tls_metadata(tls_mode="plaintext") == (False, False)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tls_mode": "system_ca", "tls": False},  # contradictory tls=false + mode
        {"tls_mode": "insecure_no_verify", "tls_insecure": False},
        {"tls_mode": "plaintext", "tls_insecure": True},
        {"tls": False, "tls_insecure": True},  # insecure without tls
        {"tls_mode": "bogus"},
    ],
)
def test_resolve_tls_metadata_rejects_contradictions(kwargs):
    with pytest.raises(ValueError):
        cfg.resolve_mqtt_tls_metadata(**kwargs)


def test_resolve_tls_metadata_from_flags_without_mode():
    assert cfg.resolve_mqtt_tls_metadata(tls=True, tls_insecure=True) == (True, True)
    assert cfg.resolve_mqtt_tls_metadata(tls=True) == (True, False)
    assert cfg.resolve_mqtt_tls_metadata() == (False, False)


# --- paho TLS application ----------------------------------------------------
class _RecordingClient:
    def __init__(self):
        self.tls_set_calls = []
        self.tls_insecure_calls = []

    def tls_set(self, *args, **kwargs):
        self.tls_set_calls.append((args, kwargs))

    def tls_insecure_set(self, value):
        self.tls_insecure_calls.append(bool(value))


def test_configure_mqtt_client_tls_plaintext_touches_nothing():
    client = _RecordingClient()
    cfg.configure_mqtt_client_tls(client, tls=False, tls_insecure=False)
    assert client.tls_set_calls == []
    assert client.tls_insecure_calls == []


def test_configure_mqtt_client_tls_verified_uses_default_verification():
    client = _RecordingClient()
    cfg.configure_mqtt_client_tls(client, tls=True, tls_insecure=False)
    assert client.tls_set_calls == [((), {})]
    assert client.tls_insecure_calls == []


def test_configure_mqtt_client_tls_insecure_skips_chain_and_hostname():
    import ssl

    client = _RecordingClient()
    cfg.configure_mqtt_client_tls(client, tls=True, tls_insecure=True)
    assert client.tls_set_calls == [((), {"cert_reqs": ssl.CERT_NONE})]
    assert client.tls_insecure_calls == [True]


def test_configure_mqtt_client_tls_pinned_ca_verifies_chain_only():
    client = _RecordingClient()
    cfg.configure_mqtt_client_tls(
        client, tls=True, tls_insecure=False, ca_certs="/tmp/ca.pem"
    )
    assert client.tls_set_calls == [((), {"ca_certs": "/tmp/ca.pem"})]
    assert client.tls_insecure_calls == [True]


def test_configure_mqtt_client_tls_insecure_without_tls_is_rejected():
    client = _RecordingClient()
    with pytest.raises(ValueError):
        cfg.configure_mqtt_client_tls(client, tls=False, tls_insecure=True)
    assert client.tls_set_calls == []


# --- strict JSON booleans (defect 4) ----------------------------------------
@pytest.mark.parametrize("value", [True, False])
def test_require_json_bool_accepts_real_booleans(value):
    assert cfg.require_json_bool(value, "flag") is value


@pytest.mark.parametrize("value", ["true", "false", "0", "1", 0, 1, None, [], {}])
def test_require_json_bool_rejects_non_booleans(value):
    with pytest.raises(ValueError):
        cfg.require_json_bool(value, "flag")


def test_optional_json_bool_defaults_only_for_none():
    assert cfg.optional_json_bool(None, "flag", default=False) is False
    assert cfg.optional_json_bool(None, "flag", default=True) is True
    assert cfg.optional_json_bool(True, "flag") is True
    for bad in ("false", "0", 0, 1):
        with pytest.raises(ValueError):
            cfg.optional_json_bool(bad, "flag")


# --- stable broker ref (defect 5) -------------------------------------------
def _identity(host, port=1883, tls=False, insecure=False):
    return normalized_broker_identity(
        {
            "source": "local_mqtt",
            "host": host,
            "port": port,
            "tls": tls,
            "tls_insecure": insecure,
        }
    )


def test_stable_ref_same_identity_same_ref():
    ref = stable_local_broker_ref(_identity("10.0.0.10"))
    assert ref == stable_local_broker_ref(_identity("10.0.0.10"))
    assert ref.startswith("local_mqtt_")


def test_stable_ref_differs_by_endpoint():
    assert stable_local_broker_ref(_identity("10.0.0.10")) != stable_local_broker_ref(
        _identity("10.0.0.20")
    )
    # Same host, different port.
    assert stable_local_broker_ref(_identity("10.0.0.10", port=1883)) != (
        stable_local_broker_ref(_identity("10.0.0.10", port=8883))
    )


@pytest.mark.parametrize(
    "profile",
    [
        {"host": "broker", "port": "broken", "tls": False},
        {"host": "broker", "port": 0, "tls": False},
        {"host": "broker", "port": 1883, "tls": "false"},
        {"host": "broker", "port": 1883, "tls": False, "tls_insecure": True},
    ],
)
def test_identity_rejects_noncanonical_connection_metadata(profile):
    with pytest.raises(ValueError):
        normalized_broker_identity(profile)


def test_identity_distinguishes_non_secret_credential_refs():
    base = {"host": "broker", "port": 1883, "tls": False}
    assert normalized_broker_identity({**base, "credentials_ref": "a"}) != (
        normalized_broker_identity({**base, "credentials_ref": "b"})
    )
    # Same host/port, different TLS mode.
    assert stable_local_broker_ref(_identity("10.0.0.10", tls=False)) != (
        stable_local_broker_ref(_identity("10.0.0.10", port=1883, tls=True))
    )
    assert stable_local_broker_ref(
        _identity("10.0.0.10", port=8883, tls=True, insecure=False)
    ) != stable_local_broker_ref(
        _identity("10.0.0.10", port=8883, tls=True, insecure=True)
    )


def test_stable_ref_never_contains_credentials():
    # A credential smuggled onto the profile is not part of identity.
    ref = stable_local_broker_ref(_identity("10.0.0.10"))
    assert "secret" not in ref and "hunter2" not in ref
