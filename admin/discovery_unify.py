# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unify per-source discovery candidates into one deduplicated device view.

Admin-only and display-only. Candidates from the local HTTP/API scan, local
MQTT brokers, and the Zendure cloud MQTT broker are grouped by device identity
so the operator sees each physical device once, with the source that "wins"
chosen strictly by the configured discovery priority. This never merges
unrelated low-confidence candidates and never writes the EMS config.
"""

from dataclasses import dataclass, field

from admin.discovery_preparation import (
    SOURCE_LOCAL_API,
    SOURCE_LOCAL_MQTT,
    SOURCE_ZENDURE_MQTT,
)
from admin.models import SOURCE_LOCAL_MQTT as CANDIDATE_LOCAL_MQTT
from admin.models import SOURCE_ZENDURE_CLOUD_MQTT as CANDIDATE_ZENDURE_CLOUD
from admin.observation_identity import stamp_observations
from ems.device_identity import normalize_physical_serial

CONFIDENCE_HIGH = "high"
CONFIDENCE_LOW = "low"

REASON_PRIORITY = "Selected by discovery priority"
REASON_ONLY_SOURCE = "Only source"

# Candidate ``source_type``/``source`` values map onto the preparation source
# keys used for priority. The Zendure cloud candidates carry the internal
# ``zendure_cloud_mqtt`` type but belong to the ``zendure_mqtt`` priority slot.
CANDIDATE_SOURCE_TO_KEY = {
    CANDIDATE_LOCAL_MQTT: SOURCE_LOCAL_MQTT,
    CANDIDATE_ZENDURE_CLOUD: SOURCE_ZENDURE_MQTT,
    SOURCE_LOCAL_API: SOURCE_LOCAL_API,
    SOURCE_LOCAL_MQTT: SOURCE_LOCAL_MQTT,
    SOURCE_ZENDURE_MQTT: SOURCE_ZENDURE_MQTT,
}


@dataclass
class UnifiedDiscoveryDevice:
    id: str
    serial_number: str | None
    device_id: str | None
    model_hint: str | None
    display_name: str
    role: str = "unknown"
    ip: str | None = None
    api_family: str | None = None
    device_type: str | None = None
    sources: list = field(default_factory=list)
    selected_source: str | None = None
    selected_reason: str = REASON_PRIORITY
    confidence: str = CONFIDENCE_LOW
    candidates: list = field(default_factory=list)

    def to_dict(self):
        return {
            "id": self.id,
            "serial_number": self.serial_number,
            "device_id": self.device_id,
            "model_hint": self.model_hint,
            "display_name": self.display_name,
            "role": self.role,
            "ip": self.ip,
            "api_family": self.api_family,
            "device_type": self.device_type,
            "sources": list(self.sources),
            "selected_source": self.selected_source,
            "selected_reason": self.selected_reason,
            "confidence": self.confidence,
            "candidates": list(self.candidates),
        }


def _clean(value):
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _candidate_view(source, raw):
    """Normalize one source candidate to the fields the unifier needs."""

    raw = raw if isinstance(raw, dict) else {}
    serial = _clean(raw.get("serial_number"))
    device_id = _clean(raw.get("device_id"))
    model_hint = _clean(raw.get("model_hint")) or _clean(raw.get("model"))
    display_name = (
        _clean(raw.get("display_name")) or model_hint or _clean(raw.get("device_type"))
    )
    stable_id = _clean(raw.get("id"))
    role = _clean(raw.get("role_suggestion")) or _clean(raw.get("role"))
    return {
        "source": source,
        "serial_number": serial,
        "device_id": device_id or stable_id,
        "model_hint": model_hint,
        "display_name": display_name,
        "role": role,
        "ip": _clean(raw.get("ip")),
        "api_family": _clean(raw.get("api_family")),
        "device_type": _clean(raw.get("device_type")),
        "stable_id": stable_id,
        "raw": raw,
    }


def _identity_key(view, order_index):
    """Strong identity by serial; otherwise a per-candidate weak identity.

    "Strong" is Core's answer, not this module's: a masked or placeholder serial
    normalizes to ``None`` and therefore groups nothing. Weak candidates are
    never merged with each other, so neither a missing nor a redacted serial can
    fold two unrelated devices together.
    """

    serial = normalize_physical_serial(view["serial_number"])
    if serial:
        return ("serial", serial)
    weak = view["stable_id"] or view["device_id"] or f"anon-{order_index}"
    return ("weak", view["source"], weak)


def _unified_id(identity, view):
    if identity[0] == "serial":
        return f"serial:{identity[1]}"
    token = view["stable_id"] or view["device_id"] or "unknown"
    return f"{view['source']}:{token}"


def build_unified_devices(
    candidates_by_source, priority, *, identity_token_key=None, broker_sources=None
):
    """Group per-source candidates by identity and select a source by priority.

    ``candidates_by_source`` maps a preparation source key to a list of
    candidate dicts (as produced by the source's ``to_dict``). ``priority`` is
    the ordered list of source keys; sources missing from it are appended last.
    Returns a list of unified device dicts, ordered high-confidence first then
    by display name.

    With an ``identity_token_key`` every returned device also carries the
    browser-facing ``observation_id``/``physical_device_id``/``identity_status``
    the UI keys its collections on.
    """

    ordered_sources = [s for s in priority if s in candidates_by_source]
    for source in candidates_by_source:
        if source not in ordered_sources:
            ordered_sources.append(source)

    groups = {}
    order = 0
    for source in ordered_sources:
        for raw in candidates_by_source.get(source) or []:
            view = _candidate_view(source, raw)
            identity = _identity_key(view, order)
            order += 1
            group = groups.get(identity)
            if group is None:
                group = {
                    "identity": identity,
                    "views": [],
                    "sources": [],
                }
                groups[identity] = group
            group["views"].append(view)
            if source not in group["sources"]:
                group["sources"].append(source)

    devices = []
    for group in groups.values():
        views = group["views"]
        sources = group["sources"]
        selected_source = sources[0]
        selected_view = next(
            (v for v in views if v["source"] == selected_source), views[0]
        )
        serial = next((v["serial_number"] for v in views if v["serial_number"]), None)
        model_hint = selected_view["model_hint"] or next(
            (v["model_hint"] for v in views if v["model_hint"]), None
        )
        display_name = (
            selected_view["display_name"]
            or next((v["display_name"] for v in views if v["display_name"]), None)
            or model_hint
            or "Unknown device"
        )
        role = (
            selected_view["role"]
            or next((v["role"] for v in views if v["role"]), None)
            or "unknown"
        )
        # Connection facts (IP / API family / type) classify the device by its
        # transport, so they must come from the priority-selected source only.
        # Falling back to another source would show a Zendure-MQTT-won device
        # with the local-API family and IP and make it read as an API device,
        # contradicting the selected source. Sources it was also seen in stay in
        # `sources`/`candidates`.
        ip = selected_view["ip"]
        api_family = selected_view["api_family"]
        device_type = selected_view["device_type"]
        device = UnifiedDiscoveryDevice(
            id=_unified_id(group["identity"], selected_view),
            serial_number=serial,
            device_id=selected_view["device_id"],
            model_hint=model_hint,
            display_name=display_name,
            role=role,
            ip=ip,
            api_family=api_family,
            device_type=device_type,
            sources=sources,
            selected_source=selected_source,
            selected_reason=REASON_PRIORITY if len(sources) > 1 else REASON_ONLY_SOURCE,
            confidence=CONFIDENCE_HIGH if serial else CONFIDENCE_LOW,
            candidates=[
                {
                    "source": v["source"],
                    "serial_number": v["serial_number"],
                    "device_id": v["device_id"],
                    "model_hint": v["model_hint"],
                    "display_name": v["display_name"],
                }
                for v in views
            ],
        )
        devices.append(device.to_dict())

    devices.sort(
        key=lambda d: (0 if d["confidence"] == CONFIDENCE_HIGH else 1, d["display_name"])
    )
    stamp_observations(
        devices,
        key=identity_token_key,
        broker_sources=broker_sources,
        include_connection_id=True,
    )
    return devices
