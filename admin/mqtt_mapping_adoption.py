# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The EMS device a saved mapping stands for, offered without a search.

The Admin wrote the mapping, so it already knows what Find devices would have
to discover: the broker, the account and the ``ems-solarflow/<id>/state``
topic. Each active mapped device becomes the observation discovery would make
of it and takes the one proposal path every adopted device takes, so the
config apply checks it like any other selection.
"""

import contextlib

from admin.models import MqttHardwareCandidate
from admin.mqtt_broker import local_profile_endpoints
from admin.zendure_mqtt_config_proposals import build_proposals, connection_dedup_key
from ems.zendure_mqtt.builtin_broker import BUILTIN_PROFILE_REF, mappings_path
from mqtt_mapping.config import read_mappings_document, saved_brokers

MAPPING_BROKER_REF_FIELD = "mapping_broker_ref"


def _endpoint(host, port, *, tls=False, tls_insecure=False, credentials_ref=None):
    return {
        "host": host,
        "port": port,
        "tls": bool(tls),
        "tls_insecure": bool(tls and tls_insecure),
        "credentials_ref": credentials_ref or None,
    }


def _has_account(credential_store, credentials_ref):
    secret = credential_store.load_mqtt_discovery_secret(credentials_ref)
    return secret is not None and bool(secret.username)


def _broker_endpoints(broker, credential_store, address):
    """``mapping broker_ref -> endpoint`` for every broker a mapping may read now, with no secret."""

    endpoints = {}
    settings, _issues, _exists = broker.load()
    refs = broker.discovery_credential_refs()
    if settings is not None and settings.enabled and address and refs:
        endpoints[BUILTIN_PROFILE_REF] = _endpoint(address, settings.port, credentials_ref=refs[0])
    for ref, entry in saved_brokers(read_mappings_document(mappings_path(broker.config_dir))).items():
        if entry["credentials_ref"] and not _has_account(credential_store, entry["credentials_ref"]):
            continue
        endpoints[ref] = _endpoint(
            entry["host"], entry["port"], tls=entry["tls"], tls_insecure=entry["tls_insecure"], credentials_ref=entry["credentials_ref"]
        )
    config = broker._config_loader()
    profiles = (config.get("zendure_mqtt") or {}).get("brokers") or {}
    for ref, entry in local_profile_endpoints(config, credential_store).items():
        endpoints[ref] = _endpoint(
            entry["host"],
            entry["port"],
            tls=entry["tls"],
            tls_insecure=entry["tls_insecure"],
            credentials_ref=profiles[ref].get("credentials_ref"),
        )
    return endpoints


def _observation(device, endpoint):
    candidate = MqttHardwareCandidate(
        broker_id=f"mqtt:{endpoint['host']}:{endpoint['port']}",
        broker_host=endpoint["host"],
        broker_port=endpoint["port"],
        topic_family=device.family,
        device_id=device.id,
        display_name=device.catalog.label,
        credentials_ref=endpoint["credentials_ref"],
    )
    candidate.metrics_seen = [metric for target in device.targets for metric in device.catalog.keys[target.key].metrics]
    candidate.topics_seen = [device.output_topic]
    return {**candidate.to_dict(), "tls": endpoint["tls"], "tls_insecure": endpoint["tls_insecure"]}


def mapped_observations(broker, credential_store, *, address) -> dict[str, list[dict]]:
    """``mapping broker_ref -> observations`` of the active mapped devices whose broker can be read.

    A broker that is off, unknown or without its account offers nothing: a
    device added there would only ever be stale. Never raises.
    """

    with contextlib.suppress(Exception):
        mappings = broker.mappings()
        if mappings is None:
            return {}
        endpoints = _broker_endpoints(broker, credential_store, address)
        observed = {}
        for device in mappings.active_devices():
            endpoint = endpoints.get(device.broker_ref)
            if endpoint is not None:
                observed.setdefault(device.broker_ref, []).append(_observation(device, endpoint))
        return observed
    return {}


def with_mapped_devices(proposals, observed) -> list[dict]:
    """``proposals`` led by each mapped device's own, marked with its mapping's broker.

    A discovery proposal of the very same connection gives way, so the mapped
    device keeps one id whatever the searches do meanwhile.
    """

    mapped = []
    for ref, observations in observed.items():
        for proposal in build_proposals(observations):
            proposal[MAPPING_BROKER_REF_FIELD] = ref
            mapped.append(proposal)
    taken = {connection_dedup_key(proposal) for proposal in mapped}
    return [*mapped, *(proposal for proposal in proposals if connection_dedup_key(proposal) not in taken)]


__all__ = ["MAPPING_BROKER_REF_FIELD", "mapped_observations", "with_mapped_devices"]
