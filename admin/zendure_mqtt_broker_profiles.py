# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared Zendure MQTT broker-profile builder for every Admin config path.

Fresh Setup discovery, Maintenance discovery and manual MQTT configuration all
provision ``zendure_mqtt.brokers`` profiles through this module, so there is
exactly one rule for endpoint parsing, identity comparison, reuse, collision
handling and cloud auth gating. Secret-free by construction: profiles carry only
non-secret connection metadata plus an external ``credentials_ref``; no
credential value is ever read or written here.
"""

import copy
import hashlib
import ipaddress
import re

from admin.credential_store import ZENDURE_CLOUD_CREDENTIALS_REF
from ems.config import optional_json_bool, parse_mqtt_port, resolve_mqtt_tls_metadata
from ems.zendure_mqtt.config_entries import (
    DEFAULT_BROKER_REF,
    SOURCE_LOCAL_MQTT,
    SOURCE_ZENDURE_CLOUD_MQTT,
    normalized_broker_identity,
)

# Well-known Zendure cloud broker profile provisioned for a selected cloud
# proposal. Enabled and secret-free: the cloud API key is resolved at runtime
# from the external secret store via ``credentials_ref`` (never written here).
# A cloud device is only usable once that external Zendure account auth exists.
CLOUD_BROKER_REF = "zendure_cloud"
CLOUD_BROKER_PROFILE = {
    "enabled": True,
    "source": SOURCE_ZENDURE_CLOUD_MQTT,
    "host": "mqtteu.zen-iot.com",
    "port": 8883,
    "tls": True,
    "tls_insecure": True,
    "credentials_ref": ZENDURE_CLOUD_CREDENTIALS_REF,
}
LOCAL_BROKER_REF = "local_mqtt"

_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


class BrokerEndpointError(ValueError):
    """A proposal's broker endpoint is explicitly invalid (bad port/TLS metadata)."""


def _issue(code, message):
    return {"code": code, "message": message}


def valid_host(value):
    value = str(value or "").strip()
    if not value or len(value) > 253:
        return False
    try:
        return ipaddress.ip_address(value).version == 4
    except ValueError:
        pass
    hostname = value[:-1] if value.endswith(".") else value
    return bool(hostname) and all(
        _HOST_LABEL.fullmatch(label) for label in hostname.split(".")
    )


def is_local_broker_ref(ref):
    ref = str(ref or "")
    return ref == LOCAL_BROKER_REF or ref.startswith(LOCAL_BROKER_REF + "_")


def default_zendure_cloud_auth_available():
    """True when Zendure auth material exists to apply a cloud broker against.

    Either the account credential (allows (re)provisioning through the
    deviceList) or an existing runtime credential record (the apply's staging
    step validates and reuses it — existence here only routes where a problem
    is reported, never proves usability). Fail-closed: any lookup problem is
    treated as "not connected" so a cloud proposal cannot be applied against
    a broker whose auth can neither be confirmed nor repaired.
    """

    try:
        from admin.credential_store import (
            ZENDURE_CLOUD_CREDENTIALS_REF,
            CredentialStore,
        )

        store = CredentialStore()
        if store.zendure.token_saved():
            return True
        return store.credential_exists(ZENDURE_CLOUD_CREDENTIALS_REF)
    except Exception:
        return False


def existing_broker_profiles(config):
    zmqtt = config.get("zendure_mqtt")
    brokers = zmqtt.get("brokers") if isinstance(zmqtt, dict) else None
    return dict(brokers) if isinstance(brokers, dict) else {}


def set_broker_profile(config, ref, profile):
    # The telemetry feature is always on: a provisioned broker runs on its own
    # per-profile enabled flag, so no top-level toggle is written.
    zmqtt = config.get("zendure_mqtt")
    if not isinstance(zmqtt, dict):
        zmqtt = {}
        config["zendure_mqtt"] = zmqtt
    brokers = zmqtt.get("brokers")
    if not isinstance(brokers, dict):
        brokers = {}
        zmqtt["brokers"] = brokers
    brokers.setdefault(ref, profile)


def broker_endpoint(proposal):
    """Parse a proposal's non-secret broker endpoint.

    An absent port stays ``None`` so a protocol default may be applied later where
    the schema allows it; an explicitly supplied *invalid* port (or contradictory
    TLS metadata) raises :class:`BrokerEndpointError` instead of being silently
    dropped to ``None`` and mistaken for "not supplied". TLS mode, ``tls_insecure``
    and ``credentials_ref`` are preserved so a TLS/authenticated broker keeps its
    effective connection metadata; secrets are never read here.
    """

    raw_port = proposal.get("broker_port")
    if raw_port in (None, ""):
        port = None
    else:
        try:
            port = parse_mqtt_port(raw_port)
        except ValueError as exc:
            raise BrokerEndpointError(str(exc)) from None

    try:
        tls, tls_insecure = resolve_mqtt_tls_metadata(
            tls_mode=proposal.get("broker_tls_mode"),
            tls=optional_json_bool(proposal.get("broker_tls"), "broker_tls", default=None),
            tls_insecure=optional_json_bool(
                proposal.get("broker_tls_insecure"), "broker_tls_insecure", default=None
            ),
        )
    except ValueError as exc:
        raise BrokerEndpointError(str(exc)) from None

    host = proposal.get("broker_host")
    source = proposal.get("connection_source")
    credentials_ref = proposal.get("credentials_ref")
    return {
        "host": host.strip() if isinstance(host, str) and host.strip() else None,
        "port": port,
        "tls": tls,
        "tls_insecure": tls_insecure,
        "credentials_ref": (
            credentials_ref.strip()
            if isinstance(credentials_ref, str) and credentials_ref.strip()
            else None
        ),
        "source": source.strip() if isinstance(source, str) and source.strip() else None,
    }


def draft_broker_endpoint(broker):
    """Parse the Admin draft broker block a selected proposal travels with.

    Every Admin consumer of a discovered connection — an MQTT inverter draft
    entry and the central MQTT grid meter alike — carries the proposal's
    non-secret endpoint under these draft key names, so the mapping to
    :func:`broker_endpoint` lives here once. Raises
    :class:`BrokerEndpointError` exactly like :func:`broker_endpoint`.
    """

    broker = broker if isinstance(broker, dict) else {}
    return broker_endpoint(
        {
            "broker_host": broker.get("host"),
            "broker_port": broker.get("port"),
            "broker_tls": broker.get("tls"),
            "broker_tls_insecure": broker.get("tls_insecure"),
            "broker_tls_mode": broker.get("tls_mode"),
            "credentials_ref": broker.get("credentials_ref"),
            "connection_source": broker.get("source"),
        }
    )


def endpoint_broker_profile(endpoint, source):
    """Shape a proposal endpoint as a broker profile for identity comparison.

    Must carry every field ``normalized_broker_identity`` keys on — including
    ``tls_insecure`` and the non-secret ``credentials_ref`` — or the reuse check
    would compute a different identity than the profile it later provisions, and
    two selections on the *same* authenticated broker would each mint a separate
    profile instead of sharing one.
    """

    return {
        "source": source,
        "host": endpoint.get("host"),
        "port": endpoint.get("port"),
        "tls": endpoint.get("tls"),
        "tls_insecure": endpoint.get("tls_insecure"),
        "credentials_ref": endpoint.get("credentials_ref"),
    }


def collision_safe_local_ref(base_ref, identity, existing):
    """Deterministic distinct ref for a local endpoint that collides on ``base_ref``.

    The suffix is a stable hash of the (secret-free) connection-profile identity, so a
    newly discovered broker that requests an already-used ref keeps its own
    profile instead of silently inheriting the existing endpoint.
    """

    digest = hashlib.sha256(
        "\x1f".join("" if part is None else str(part) for part in identity).encode("utf-8")
    ).hexdigest()[:6]
    ref = f"{base_ref}_{digest}"
    candidate = ref
    counter = 2
    while candidate in existing:
        if normalized_broker_identity(existing[candidate]) == identity:
            return candidate
        candidate = f"{ref}_{counter}"
        counter += 1
    return candidate


def _provision_cloud_broker(config, ref, endpoint, label, validation, cloud_auth_available):
    if not cloud_auth_available():
        validation["errors"].append(
            _issue(
                "zendure_mqtt_broker_auth_missing",
                f"{label}: connect your Zendure account before applying this "
                "cloud MQTT device.",
            )
        )
        return False
    profile = copy.deepcopy(CLOUD_BROKER_PROFILE)
    if valid_host(endpoint["host"]):
        profile["host"] = endpoint["host"]
    if endpoint["port"]:
        profile["port"] = endpoint["port"]
    set_broker_profile(config, ref, profile)
    return True


def _provision_local_broker(config, ref, endpoint, label, validation):
    if not valid_host(endpoint["host"]) or not endpoint["port"]:
        validation["errors"].append(
            _issue(
                "zendure_mqtt_broker_incomplete",
                f"{label}: Zendure MQTT broker profile is incomplete. "
                "Configure the broker before applying this device.",
            )
        )
        return False
    profile = {
        "enabled": True,
        "source": SOURCE_LOCAL_MQTT,
        "host": endpoint["host"],
        "port": endpoint["port"],
        "tls": endpoint["tls"],
    }
    # Preserve the effective TLS verification mode and the non-secret credential
    # reference so a discovered TLS/authenticated broker can reconnect at runtime
    # instead of silently downgrading to plain, anonymous MQTT.
    if endpoint["tls"]:
        profile["tls_insecure"] = bool(endpoint.get("tls_insecure"))
    if endpoint.get("credentials_ref"):
        profile["credentials_ref"] = endpoint["credentials_ref"]
    set_broker_profile(config, ref, profile)
    return True


def resolve_broker_ref(
    config, ref, endpoint, label, validation, cloud_auth_available,
    ref_conflict="mint",
):
    """Resolve ``ref`` to a usable enabled broker profile; return its effective ref.

    Returns the ref the device/grid meter must actually reference, or ``None`` on
    an actionable error (already appended to ``validation``). A local proposal is
    compared by secret-free connection-profile identity, never by ref name alone:

    * an endpoint that already matches a declared profile (under any ref) reuses
      that profile, so a matching broker is never duplicated;
    * a requested ref that already exists but points at a *different* endpoint is
      handled per ``ref_conflict``: ``"mint"`` (Fresh Setup) assigns a
      deterministic distinct ref with its own profile, ``"reject"``
      (Maintenance) reports an actionable conflict instead — an existing
      operator-declared profile is never silently replaced either way.

    The implicit ``default`` broker and non-local (cloud) refs keep their existing
    behavior: an already-declared profile is authoritative. A cloud proposal
    provisions an enabled, secret-free cloud profile only when external Zendure
    account auth exists; a local proposal provisions one only when a usable
    host/port is known.
    """

    existing = existing_broker_profiles(config)
    is_local = source_is_local(endpoint, ref)

    candidate_identity = None
    if is_local:
        candidate_identity = normalized_broker_identity(
            endpoint_broker_profile(endpoint, SOURCE_LOCAL_MQTT)
        )
        if candidate_identity is not None:
            for existing_ref, profile in existing.items():
                if normalized_broker_identity(profile) == candidate_identity:
                    return existing_ref

    if ref == DEFAULT_BROKER_REF:
        return ref

    if ref in existing:
        if not is_local:
            return ref
        existing_identity = normalized_broker_identity(existing[ref])
        if candidate_identity is None or existing_identity == candidate_identity:
            return ref
        if ref_conflict == "reject":
            validation["errors"].append(
                _issue(
                    "zendure_mqtt_broker_conflict",
                    f"{label}: broker profile '{ref}' already exists with "
                    "different connection settings and was not replaced. Update "
                    "or remove the existing broker profile before adding this "
                    "device.",
                )
            )
            return None
        ref = collision_safe_local_ref(ref, candidate_identity, existing)

    if ref == CLOUD_BROKER_REF:
        if _provision_cloud_broker(
            config, ref, endpoint, label, validation, cloud_auth_available
        ):
            return ref
        return None
    if is_local_broker_ref(ref):
        # ``local_mqtt`` plus the deterministic ``local_mqtt_<slug>_<hash>`` refs
        # the proposal builder mints for additional local brokers each provision
        # their own profile from their own endpoint, so brokers never share one.
        if _provision_local_broker(config, ref, endpoint, label, validation):
            return ref
        return None
    validation["errors"].append(
        _issue(
            "zendure_mqtt_broker_unresolved",
            f"{label}: broker '{ref}' is not a configured zendure_mqtt.brokers "
            "profile. Add the broker before selecting this device.",
        )
    )
    return None


def source_is_local(endpoint, ref):
    """True when a proposal endpoint/ref denotes a local (non-cloud) broker."""

    source = str(endpoint.get("source") or "").strip().lower()
    if source == SOURCE_ZENDURE_CLOUD_MQTT or ref == CLOUD_BROKER_REF:
        return False
    if source == SOURCE_LOCAL_MQTT:
        return True
    return is_local_broker_ref(ref)


__all__ = [
    "BrokerEndpointError",
    "CLOUD_BROKER_PROFILE",
    "CLOUD_BROKER_REF",
    "LOCAL_BROKER_REF",
    "ZENDURE_CLOUD_CREDENTIALS_REF",
    "broker_endpoint",
    "collision_safe_local_ref",
    "default_zendure_cloud_auth_available",
    "draft_broker_endpoint",
    "endpoint_broker_profile",
    "existing_broker_profiles",
    "is_local_broker_ref",
    "resolve_broker_ref",
    "set_broker_profile",
    "source_is_local",
    "valid_host",
]
