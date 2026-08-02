# SPDX-License-Identifier: AGPL-3.0-or-later
"""Can this *discovered connection* write ``outputLimit``?

Runtime, Maintenance and the MQTT proposal generator already answer that for a
configured device, a draft entry and a proposal respectively — all three from
``ems.zendure_mqtt.capability``. Setup's batch planner needs the same answer for
a candidate it has not configured yet, and must not grow a fourth opinion. This
module is the adapter: it maps a discovered connection onto the canonical
resolver and returns the tri-state the connection planner expects.

``True``/``False`` are resolved verdicts. ``None`` means *not resolved* and is
never treated as capable anywhere downstream, so an unrecognized payload keeps a
transport switch behind an explicit confirmation instead of silently allowing it.
"""

from ems.zendure_mqtt.capability import resolve_output_control_capability

SOURCE_LOCAL_API = "local_api"

_INVERTER_ROLE = "inverter"


def _text(value):
    return str(value if value is not None else "").strip()


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _local_api_output_control(payload):
    """Local HTTP API control, which is the EMS's own native write transport.

    There is no capability axis to resolve here: ``ems.clients`` writes
    ``outputLimit`` over the local Zendure HTTP API, and discovery only proposes
    the inverter role for exactly those endpoints. An unverified observation
    stays unresolved rather than capable.
    """

    role = _text(payload.get("role_suggestion")) or _text(payload.get("role"))
    if role.lower() != _INVERTER_ROLE:
        return None
    if payload.get("verified") is False:
        return None
    return True


def _mqtt_output_control(payload):
    supported = payload.get("output_control_supported")
    if isinstance(supported, bool):
        # Core already resolved this proposal; its answer is the answer.
        return supported
    fragment = _mapping(payload.get("config_fragment"))
    mqtt = _mapping(fragment.get("mqtt")) or _mapping(payload.get("mqtt"))
    broker_source = _text(mqtt.get("source")) or _text(payload.get("connection_source"))
    if not broker_source:
        return None
    capability = resolve_output_control_capability(
        topic_family=_text(payload.get("topic_family")) or _text(mqtt.get("topic_family")),
        hardware_profile=(
            _text(fragment.get("hardware_profile"))
            or _text(payload.get("hardware_model"))
            or None
        ),
        broker_source=broker_source,
        product_key=_text(mqtt.get("product_key")),
        device_id=_text(mqtt.get("device_id")),
        write_protocol=_text(mqtt.get("write_protocol")) or None,
        write_topic=_text(mqtt.get("write_topic")) or None,
    )
    return capability.supported


def payload_output_control(payload, source=None):
    """The output-control verdict for one discovered connection record.

    ``source`` names the transport it was offered on; when it is not given the
    record's own connection source decides, so a caller holding a bare
    observation or proposal does not have to classify it first.
    """

    if not isinstance(payload, dict):
        return None
    if source is None:
        source = (
            SOURCE_LOCAL_API
            if not _text(payload.get("connection_source"))
            and not _mapping(payload.get("mqtt"))
            else None
        )
    if source == SOURCE_LOCAL_API:
        return _local_api_output_control(payload)
    return _mqtt_output_control(payload)


def connection_output_control(entry):
    """The output-control verdict for one Setup planning entry.

    ``entry`` is anything exposing the ``payload``/``source`` pair the Setup
    planner builds — the trusted discovery record and which transport it was
    offered on. An entry whose payload is not a trusted record (an unresolved
    legacy hint) has no capability, because nothing has been resolved about it.
    """

    return payload_output_control(
        getattr(entry, "payload", None), getattr(entry, "source", None)
    )


__all__ = ["connection_output_control", "payload_output_control"]
