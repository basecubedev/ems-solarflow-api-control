# SPDX-License-Identifier: AGPL-3.0-or-later
"""Preview-only EMS configuration generation for the Admin setup wizard."""

import copy
import json
from pathlib import Path

from admin.device_common_fields import materialize_common_device_defaults
from admin.install_context import detect_install_context
from admin.inverter_names import next_compact_inverter_name
from admin.releases import ReleaseError
from admin.setup_config import (
    apply_device_config_values,
    apply_setup_features,
)
from admin.zendure_mqtt_broker_profiles import (
    LOCAL_BROKER_REF as _LOCAL_BROKER_REF,
    BrokerEndpointError as _BrokerEndpointError,
    BrokerSecurityError,
    broker_endpoint as _broker_endpoint,
    broker_tls_metadata,
    default_zendure_cloud_auth_available as _default_zendure_cloud_auth_available,
    existing_broker_profiles as _existing_broker_profiles,
    resolve_broker_ref as _resolve_broker_ref,
    set_broker_profile as _set_broker_profile,
    valid_host as _valid_host,
)
from admin.zendure_mqtt_config_draft import (
    build_manual_zendure_mqtt_fragment,
    enforce_zendure_mqtt_output_control_capability,
    sanitize_zendure_mqtt_fragment,
    validate_zendure_mqtt_fragment,
)
from ems.config import (
    MQTT_GRID_METER_TYPES,
    ZENDURE_SMARTMETER_D0_GRID_METER_TYPE,
    MqttBrokerReferenceAmbiguousError,
    default_mqtt_port,
    normalize_mqtt_grid_meter_settings,
    optional_json_bool,
    parse_mqtt_port,
    resolve_grid_meter_mqtt_settings,
    zendure_smartmeter_d0_serial_from_topic,
    zendure_smartmeter_d0_topic,
)
from ems.config_catalog import (
    grid_meter_types,
    grid_meter_variant_field_spec,
)
from ems.config_mutation import strip_incompatible_grid_meter_fields
from ems.device_identity import broker_sources_from_config
from ems.influx_setup import DOCKER_FIRST_SECRET_FILE
from ems.zendure_mqtt.config_entries import (
    SOURCE_LOCAL_MQTT,
    SOURCE_ZENDURE_CLOUD_MQTT,
    config_entry_enabled,
    find_duplicate_zendure_device_identities,
    find_reserved_mqtt_broker_ref_issues,
    find_zendure_mqtt_broker_profile_issues,
    has_runtime_control_device,
    is_control_zendure_mqtt_device_config,
    is_zendure_mqtt_device_config,
    zendure_config_device_identity,
    zendure_mqtt_broker_ref,
)


_GRID_TYPES = {
    "shelly_gen2": "shelly",
    "shelly_3em_gen1": "shelly_3em_gen1",
    "ecotracker": "ecotracker",
    # Both D0 and Smart Meter 3CT are discovered as the generic local-HTTP family.
    "zendure_grid_meter_http": "zendure_grid_meter_http",
    "zendure_smartmeter_3ct_http": "zendure_grid_meter_http",
    "tasmota_http": "tasmota_http",
}
# Explicit meter types a manual (non-discovered) grid meter may declare. A manual
# entry has no api_family/device_type, so its type must be chosen, not inferred.
# Derived from the central catalog so a new grid-meter variant is accepted here
# (and in maintenance, which reuses this set) without a duplicated Admin list.
_GRID_TYPE_CHOICES = grid_meter_types()


def _issue(code, message):
    return {"code": code, "message": message}


# Cap on Zendure MQTT proposals accepted per request. Secret stripping, masked
# identifier handling and manual fragment building live in the shared
# zendure_mqtt_config_draft helper so setup and maintenance stay in sync.
_MAX_ZENDURE_MQTT_PROPOSALS = 20

def _existing_broker_refs(preview):
    return set(_existing_broker_profiles(preview))


def _upsert_zendure_mqtt_device(
    devices, base_index_by_identity, entry, resolved_ref, *, broker_sources
):
    """Add, update or leave an MQTT device for a trusted proposal ``entry``.

    Returns one of ``added`` / ``updated`` / ``unchanged``. When the proposal's
    physical identity already matches an enabled Zendure MQTT device in the base,
    that existing device is rebound in place to ``resolved_ref`` (rebind) or left
    untouched (exact re-apply) instead of appending a duplicate. Only same-family
    base entries qualify, snapshotted before the merge: an HTTP device sharing a
    serial, or a second distinct proposal in this same call, is not treated as a
    rebind and still collides under duplicate-identity validation.
    """

    identity = zendure_config_device_identity(
        entry, broker_sources=broker_sources
    )
    existing_index = (
        base_index_by_identity.get(identity) if identity is not None else None
    )
    if existing_index is None:
        # A brand-new device materializes the central common defaults, exactly
        # like a device added through Maintenance; a rebind below never touches
        # the existing device's stored values.
        devices.append(materialize_common_device_defaults(entry))
        return "added"

    device = devices[existing_index]
    mqtt = device.get("mqtt")
    if not isinstance(mqtt, dict):
        mqtt = {}
        device["mqtt"] = mqtt
    if zendure_mqtt_broker_ref(device) == resolved_ref:
        return "unchanged"
    mqtt["broker_ref"] = resolved_ref
    return "updated"


def _prune_unreferenced_new_brokers(preview, preexisting_refs):
    """Drop broker profiles this merge newly created that nothing references.

    A rebind reuses the existing device and points it at the resolved profile, so
    a profile provisioned for a proposal that then rebound the base device (or a
    profile left unused by a skipped proposal) must not linger. A pre-existing
    operator profile is never auto-removed even when it becomes unreferenced,
    because deleting user-declared connection state is not this path's decision.
    """

    zmqtt = preview.get("zendure_mqtt")
    brokers = zmqtt.get("brokers") if isinstance(zmqtt, dict) else None
    if not isinstance(brokers, dict):
        return
    referenced = set()
    for device in preview.get("devices", []):
        if is_zendure_mqtt_device_config(device):
            referenced.add(zendure_mqtt_broker_ref(device))
    grid = preview.get("grid_meter")
    if isinstance(grid, dict):
        grid_mqtt = grid.get("mqtt")
        if isinstance(grid_mqtt, dict):
            ref = grid_mqtt.get("broker_ref")
            if isinstance(ref, str) and ref.strip():
                referenced.add(ref.strip())
    for ref in list(brokers):
        if ref not in preexisting_refs and ref not in referenced:
            del brokers[ref]


def _merge_zendure_mqtt_proposals(preview, proposals, validation, cloud_auth_available):
    """Upsert sanitized Zendure MQTT proposal fragments (telemetry or control).

    Each fragment is stripped of secrets and capability-gated (output control is
    preserved only where the topic family has a verified write method; an
    unsupported or forged control request is downgraded to telemetry-only), then
    validated with the matching EMS-owned validator (control or telemetry) before
    it may become a device. Its broker ref must resolve to a usable enabled broker
    profile (cloud requires external account auth, local requires a known
    host/port). A proposal whose physical device already exists in the base is
    upserted onto that device (rebind or no-op) so a trusted re-selection from a
    different broker never appends a duplicate or orphans a freshly provisioned
    profile. An invalid or unusable proposal raises a validation error so
    export/apply cannot proceed with it.
    """

    if not isinstance(proposals, list) or not proposals:
        return 0
    devices = preview.get("devices")
    if not isinstance(devices, list):
        devices = []
        preview["devices"] = devices

    # Snapshot, before merging, the base's enabled Zendure MQTT devices by
    # physical identity (for in-place upsert) and the broker refs that already
    # existed (so only profiles this call creates may be pruned).
    preexisting_refs = _existing_broker_refs(preview)
    broker_sources = broker_sources_from_config(preview)
    base_index_by_identity = {}
    disabled_base_identities = set()
    for index, device in enumerate(devices):
        if not (isinstance(device, dict) and device.get("type") == "zendure_mqtt"):
            continue
        identity = zendure_config_device_identity(
            device, broker_sources=broker_sources
        )
        if identity is None:
            continue
        if config_entry_enabled(device):
            base_index_by_identity.setdefault(identity, index)
        else:
            # A disabled base device is never a rebind target: an upsert would
            # append an enabled duplicate that later trips duplicate-identity
            # validation, so a proposal matching one is rejected instead.
            disabled_base_identities.add(identity)

    # Two selected proposals for one physical device are an ambiguous selection
    # even when that device already exists in the base: rebinding it twice would
    # silently let the last proposal win. Count valid proposal identities up front
    # so both are rejected before any broker is provisioned or any base device is
    # rebound.
    proposal_identity_counts = {}
    for proposal in proposals[:_MAX_ZENDURE_MQTT_PROPOSALS]:
        if not isinstance(proposal, dict):
            continue
        fragment = proposal.get("config_fragment")
        if not isinstance(fragment, dict):
            continue
        # A disabled selection never becomes a device, so it can never be the
        # second claim on a physical identity either.
        if proposal.get("enabled") is False:
            continue
        scan_entry = sanitize_zendure_mqtt_fragment(
            copy.deepcopy(fragment), broker_sources
        )
        if validate_zendure_mqtt_fragment(scan_entry, broker_sources):
            continue
        scan_identity = zendure_config_device_identity(
            scan_entry, broker_sources=broker_sources
        )
        if scan_identity is not None:
            proposal_identity_counts[scan_identity] = (
                proposal_identity_counts.get(scan_identity, 0) + 1
            )
    conflicting_identities = {
        identity for identity, count in proposal_identity_counts.items() if count > 1
    }
    reported_conflicts = set()

    allocation_names = [
        str(device.get("name") or "").strip()
        for device in devices
        if isinstance(device, dict)
    ]
    allocation_count = len(devices)
    added = 0
    control_added = 0
    cloud_control_added = 0
    for proposal in proposals[:_MAX_ZENDURE_MQTT_PROPOSALS]:
        if not isinstance(proposal, dict):
            continue
        fragment = proposal.get("config_fragment")
        if not isinstance(fragment, dict):
            continue
        # A disabled selection is left out of the generated config exactly like a
        # disabled Local API draft item, so the enabled state survives a
        # connection switch in both directions.
        if proposal.get("enabled") is False:
            continue
        entry = sanitize_zendure_mqtt_fragment(copy.deepcopy(fragment), broker_sources)
        if "config_name" in proposal:
            config_name = str(proposal.get("config_name") or "").strip()
        else:
            config_name = next_compact_inverter_name(
                allocation_names, allocation_count
            )
        entry["name"] = config_name
        allocation_count += 1
        if config_name:
            allocation_names.append(config_name)
        errors = validate_zendure_mqtt_fragment(entry)
        if errors:
            label = str(entry.get("name") or "Zendure MQTT device").strip()
            for issue in errors:
                validation["errors"].append(
                    _issue("zendure_mqtt_invalid", f"{label}: {issue['message']}.")
                )
            continue
        # Common (transport-independent) device values travel with the logical
        # inverter through the same catalog-filtered writer the Local API draft
        # uses; identity and connection keys stay owned by the fragment.
        apply_device_config_values(entry, proposal.get("config_values"))
        label = str(entry.get("name") or "Zendure MQTT device").strip()
        identity = zendure_config_device_identity(
            entry, broker_sources=broker_sources
        )
        if identity is not None and identity in conflicting_identities:
            if identity not in reported_conflicts:
                validation["errors"].append(
                    _issue(
                        "zendure_device_identity_duplicate",
                        f"{label}: multiple selected Zendure MQTT proposals share "
                        "one physical device identity. Select only one.",
                    )
                )
                reported_conflicts.add(identity)
            continue
        if identity is not None and identity in disabled_base_identities:
            validation["errors"].append(
                _issue(
                    "zendure_device_identity_disabled",
                    f"{label}: a disabled Zendure MQTT device with this identity "
                    "already exists. Re-enable or remove it before adding this one.",
                )
            )
            continue
        try:
            endpoint = _broker_endpoint(proposal)
        except _BrokerEndpointError as exc:
            validation["errors"].append(
                _issue(
                    "zendure_mqtt_broker_endpoint_invalid",
                    f"{label}: {exc}.",
                )
            )
            continue
        resolved_ref = _resolve_broker_ref(
            preview,
            zendure_mqtt_broker_ref(entry),
            endpoint,
            label,
            validation,
            cloud_auth_available,
        )
        if resolved_ref is None:
            continue
        # Point the device at the profile the resolver actually settled on: a
        # reused matching broker, or the distinct ref minted for a colliding one.
        mqtt = entry.get("mqtt")
        if isinstance(mqtt, dict):
            mqtt["broker_ref"] = resolved_ref
        # The broker profile the resolver settled on is the authoritative write
        # carrier — not the source a proposal fragment claimed — so capability is
        # re-enforced against it before the entry is written.
        enforce_zendure_mqtt_output_control_capability(
            entry, broker_sources_from_config(preview)
        )
        if _upsert_zendure_mqtt_device(
            devices,
            base_index_by_identity,
            entry,
            resolved_ref,
            broker_sources=broker_sources_from_config(preview),
        ) == "added":
            added += 1
            if is_control_zendure_mqtt_device_config(entry):
                control_added += 1
                broker = (preview.get("zendure_mqtt", {}).get("brokers", {})).get(
                    resolved_ref, {}
                )
                if broker.get("source") == SOURCE_ZENDURE_CLOUD_MQTT:
                    cloud_control_added += 1

    # Rebinds reuse the base device, so a profile provisioned for such a proposal
    # would otherwise linger unreferenced; prune only newly-created ones.
    _prune_unreferenced_new_brokers(preview, preexisting_refs)

    # The summary reflects each device's actual capability: control devices are
    # announced as controllable, telemetry devices as read-only.
    telemetry_added = added - control_added
    if telemetry_added:
        noun = "device" if telemetry_added == 1 else "devices"
        validation["warnings"].append(
            _issue(
                "zendure_mqtt_telemetry_only",
                f"{telemetry_added} Zendure MQTT {noun} will be written without "
                "output control. Output writes stay off for them.",
            )
        )
    if cloud_control_added:
        validation["warnings"].append(
            _issue(
                "zendure_cloud_mqtt_single_controller",
                "Zendure Cloud MQTT output control selected: use only one "
                "active controller. Disable Zendure HEMS, Smart Matching, "
                "Zendure schedules and any other system that writes inverter "
                "power.",
            )
        )
    if control_added:
        noun = "inverter" if control_added == 1 else "inverters"
        validation["warnings"].append(
            _issue(
                "zendure_mqtt_control_enabled",
                f"{control_added} Zendure MQTT {noun} will be written with "
                "output control enabled. EMS regulates them over MQTT behind "
                "the MQTT write gates.",
            )
        )
    return added


_GRID_METER_PROPOSAL_TARGET = "grid_meter"
_ZENSDK_HA_SCALAR_FAMILY = "zensdk_ha_scalar"


def _proposal_target(proposal):
    if not isinstance(proposal, dict):
        return "device"
    return str(proposal.get("target") or "device").strip().lower()


def _http_grid_meter_selected(preview, meters):
    """True when a non-MQTT grid meter is already the chosen EMS grid meter."""

    if meters:
        return True
    grid = preview.get("grid_meter")
    if not isinstance(grid, dict):
        return False
    grid_type = str(grid.get("type") or "").strip().lower()
    return bool(grid_type) and grid_type not in MQTT_GRID_METER_TYPES


def _merge_zendure_mqtt_grid_meter_proposal(
    preview, grid_proposals, meters, validation, cloud_auth_available
):
    """Apply a selected D0 MQTT proposal as the central ``grid_meter``.

    Backend re-validates everything (source, family, exact totalPower topic,
    broker ref) and rebuilds the fragment from trusted pieces, so a frontend
    role label is never trusted. The D0 is written to ``grid_meter`` only, never
    appended to ``devices[]``. Exactly one grid meter may be active; an existing
    HTTP/Shelly grid meter is only replaced on an explicit user request.
    """

    if not isinstance(grid_proposals, list) or not grid_proposals:
        return False
    if len(grid_proposals) > 1:
        validation["errors"].append(
            _issue(
                "grid_meter_duplicate",
                "Only one EMS grid meter can be active. Select a single MQTT grid "
                "meter proposal.",
            )
        )
        return False

    proposal = grid_proposals[0]
    if not isinstance(proposal, dict):
        return False
    label = "Zendure D0 MQTT grid meter"

    source = str(proposal.get("connection_source") or "").strip().lower()
    if source == SOURCE_ZENDURE_CLOUD_MQTT:
        validation["errors"].append(
            _issue(
                "grid_meter_cloud_unsupported",
                "Zendure Cloud MQTT D0 auto-mapping is not supported yet.",
            )
        )
        return False
    if source != SOURCE_LOCAL_MQTT:
        validation["errors"].append(
            _issue(
                "grid_meter_source_invalid",
                f"{label}: only a local MQTT broker is supported.",
            )
        )
        return False

    if str(proposal.get("topic_family") or "").strip().lower() != _ZENSDK_HA_SCALAR_FAMILY:
        validation["errors"].append(
            _issue(
                "grid_meter_family_invalid",
                f"{label}: unsupported MQTT topic family for a D0 grid meter.",
            )
        )
        return False

    topic, issue = _validated_d0_topic(proposal)
    if topic is None:
        validation["errors"].append(issue)
        return False

    # Explicit replacement only: never silently replace a chosen grid meter. A
    # non-boolean flag (e.g. the string "false") is rejected, never coerced, so a
    # grid meter can never be replaced by string truthiness.
    try:
        replace = optional_json_bool(
            proposal.get("replace_grid_meter"), "replace_grid_meter", default=False
        )
    except ValueError as exc:
        validation["errors"].append(_issue("grid_meter_replace_invalid", str(exc)))
        return False
    if _http_grid_meter_selected(preview, meters) and not replace:
        validation["errors"].append(
            _issue(
                "grid_meter_conflict",
                "A grid meter is already selected. Replace it with this MQTT grid "
                "meter to continue.",
            )
        )
        return False

    ref = str(proposal.get("broker_ref") or "").strip() or _LOCAL_BROKER_REF
    try:
        endpoint = _broker_endpoint(proposal)
    except _BrokerEndpointError as exc:
        validation["errors"].append(
            _issue("zendure_mqtt_broker_endpoint_invalid", f"{label}: {exc}.")
        )
        return False
    resolved_ref = _resolve_broker_ref(
        preview, ref, endpoint, label, validation, cloud_auth_available
    )
    if resolved_ref is None:
        return False

    # Rebuild the fragment from trusted values only; force the D0 invariants. The
    # topic is derived server-side from the proposal identity, never copied from
    # the browser-supplied fragment.
    preview["grid_meter"] = {
        "type": ZENDURE_SMARTMETER_D0_GRID_METER_TYPE,
        "mqtt": {
            "broker_ref": resolved_ref,
            "topic": topic,
            "payload_format": "number",
            "max_age_seconds": 15,
        },
    }
    validation["warnings"].append(
        _issue(
            "grid_meter_mqtt_selected",
            "A Zendure D0 MQTT grid meter will be used as the EMS grid signal "
            "(read-only, no writes).",
        )
    )
    return True


def _grid_meter_summary(preview):
    """Describe the effective ``preview['grid_meter']`` for the setup summary.

    Returns ``None`` when no grid meter is configured. The D0 is reported from
    ``grid_meter`` only; it is never added to ``devices[]`` and is never counted
    as an inverter/device.
    """

    grid = preview.get("grid_meter")
    if not isinstance(grid, dict):
        return None
    grid_type = str(grid.get("type") or "").strip().lower()
    if not grid_type:
        return None
    return {
        "type": grid_type,
        "transport": "mqtt" if grid_type in MQTT_GRID_METER_TYPES else "http",
    }


def _reserved_broker_ref_errors(preview, validation):
    """Reject a named broker profile that uses the reserved ``default`` ref.

    Setup never generates ``zendure_mqtt.brokers.default`` itself, but a carried
    base config or a manually edited draft can, so the same Core reservation the
    runtime enforces is applied to the preview before it can be marked ready.
    """

    for issue in find_reserved_mqtt_broker_ref_issues(preview):
        validation["errors"].append(_issue(issue["code"], issue["message"]))


def _validate_mqtt_grid_meter_via_core(preview, validation):
    """Re-validate a final MQTT grid meter through the shared EMS Core resolver.

    Admin preview and EMS Core must agree on validity, so the finished
    ``preview["grid_meter"]`` is run through the same ``resolve_grid_meter_mqtt_
    settings`` + ``normalize_mqtt_grid_meter_settings`` path EMS uses at startup.
    Any Core rejection (unknown/disabled/non-local broker ref, missing host,
    invalid port, conflicting inline settings, non-canonical topic) becomes an
    actionable preview error instead of a preview that is ``ready`` but fails at
    runtime. Core messages carry no secrets, so they are safe to surface.
    """

    grid = preview.get("grid_meter")
    if not isinstance(grid, dict):
        return
    grid_type = str(grid.get("type") or "").strip().lower()
    if grid_type not in MQTT_GRID_METER_TYPES:
        return
    try:
        resolved = resolve_grid_meter_mqtt_settings(preview)
        normalize_mqtt_grid_meter_settings(
            {"type": grid_type, "mqtt": resolved}, meter_type=grid_type
        )
    except MqttBrokerReferenceAmbiguousError as exc:
        validation["errors"].append(_issue(exc.code, str(exc)))
    except ValueError as exc:
        validation["errors"].append(
            _issue("grid_meter_mqtt_invalid", str(exc))
        )


_MQTT_TOPIC_WILDCARDS = ("+", "#")


def _validated_d0_topic(proposal):
    """Derive the trusted D0 totalPower topic from proposal identity, or an error.

    Returns ``(topic, None)`` on success or ``(None, issue)`` on rejection. The
    browser-supplied ``grid_meter_fragment`` topic is never trusted: the topic is
    rebuilt server-side from a validated serial (:func:`zendure_smartmeter_d0_topic`)
    and that exact canonical topic must appear in the observed ``seen_topics``.

    Rejections cover a serial/device-id identity mismatch, an unobserved or
    missing totalPower topic, an empty ``seen_topics``, a wildcard among the
    observed topics, and any *additional* observed totalPower topic for a foreign
    serial (browser-injected topics for a device that was never discovered).
    """

    serial, issue = _validated_d0_serial(proposal)
    if serial is None:
        return None, issue

    expected_topic = zendure_smartmeter_d0_topic(serial)

    seen = proposal.get("seen_topics")
    seen_topics = [t.strip() for t in seen if isinstance(t, str) and t.strip()] if (
        isinstance(seen, (list, tuple))
    ) else []
    if not seen_topics:
        return None, _issue(
            "grid_meter_topic_missing",
            "The selected D0 MQTT proposal has no observed totalPower topic.",
        )
    if any(any(w in topic for w in _MQTT_TOPIC_WILDCARDS) for topic in seen_topics):
        return None, _issue(
            "grid_meter_topic_untrusted",
            "Zendure D0 MQTT grid meter: observed topics contain a wildcard and "
            "cannot be trusted for a D0 grid meter.",
        )

    # Any observed canonical totalPower topic for a different serial means the
    # submitted observation set was tampered with; reject rather than pick one.
    for topic in seen_topics:
        other_serial = zendure_smartmeter_d0_serial_from_topic(topic)
        if other_serial and other_serial != serial:
            return None, _issue(
                "grid_meter_topic_identity_mismatch",
                "The selected D0 MQTT proposal reports a totalPower topic for a "
                "different device than the proposal identity.",
            )

    if expected_topic not in seen_topics:
        return None, _issue(
            "grid_meter_topic_missing",
            "The selected D0 MQTT proposal has no observed totalPower topic for "
            "its device serial.",
        )
    return expected_topic, None


def _validated_d0_serial(proposal):
    """Resolve one trusted serial for a D0 proposal, or ``(None, issue)``.

    Prefers ``serial_number``; falls back to ``device_id``. When both are present
    they must denote the same identity, so a browser that pairs a real serial with
    a foreign device id is rejected rather than trusted.
    """

    serial = proposal.get("serial_number")
    serial = serial.strip() if isinstance(serial, str) and serial.strip() else None
    device_id = proposal.get("device_id")
    device_id = device_id.strip() if isinstance(device_id, str) and device_id.strip() else None

    if serial and device_id and serial != device_id:
        return None, _issue(
            "grid_meter_identity_mismatch",
            "The selected D0 MQTT proposal has an inconsistent serial and device "
            "id and cannot be used as a grid meter.",
        )
    resolved = serial or device_id
    if not resolved:
        return None, _issue(
            "grid_meter_topic_missing",
            "The selected D0 MQTT proposal has no device serial to derive a "
            "totalPower topic from.",
        )
    try:
        zendure_smartmeter_d0_topic(resolved)
    except ValueError:
        return None, _issue(
            "grid_meter_serial_invalid",
            "The selected D0 MQTT proposal has an invalid device serial.",
        )
    return resolved, None


def manual_broker_name(broker):
    """The broker-profile ref a manual broker entry maps to (with default)."""

    if not isinstance(broker, dict):
        return _LOCAL_BROKER_REF
    return str(broker.get("name") or "").strip() or _LOCAL_BROKER_REF


def manual_broker_credentials_ref(broker):
    """Deterministic external credential-store ref for a manual broker secret.

    Shared by the preview (which writes only this non-secret reference into the
    profile) and the apply transaction (which persists the username/password
    under the same ref), so the config reference and the stored secret always
    agree.
    """

    from admin.credential_store import CredentialStore

    return CredentialStore.normalize_ref(manual_broker_name(broker))


def _apply_manual_zendure_mqtt_broker(preview, broker, validation):
    """Provision the user-entered Zendure MQTT broker profile.

    Returns the broker ref manual devices should reference. An empty/invalid host
    is a friendly, actionable error rather than a silently broken broker.
    """

    if not isinstance(broker, dict) or not broker:
        return _LOCAL_BROKER_REF
    name = manual_broker_name(broker)
    host = str(broker.get("host") or "").strip()
    if not host:
        validation["errors"].append(
            _issue(
                "zendure_mqtt_broker_host_missing",
                "The Zendure MQTT broker needs a host or IP address.",
            )
        )
        return name
    if not _valid_host(host):
        validation["errors"].append(
            _issue(
                "zendure_mqtt_broker_host_invalid",
                "The Zendure MQTT broker has an invalid host or IP address.",
            )
        )
        return name
    try:
        tls, tls_insecure = broker_tls_metadata(broker)
    except BrokerSecurityError as exc:
        validation["errors"].append(
            _issue(
                "zendure_mqtt_broker_security_invalid",
                f"The Zendure MQTT broker has an unusable connection security: {exc}.",
            )
        )
        return name
    try:
        port = parse_mqtt_port(broker.get("port"), default=default_mqtt_port(tls))
    except ValueError as exc:
        validation["errors"].append(
            _issue(
                "zendure_mqtt_broker_port_invalid",
                f"The Zendure MQTT broker has an invalid port: {exc}.",
            )
        )
        return name
    profile = {
        "enabled": True,
        "source": SOURCE_LOCAL_MQTT,
        "host": host,
        "port": port,
        "tls": tls,
    }
    if tls_insecure:
        profile["tls_insecure"] = True
    username = str(broker.get("username") or "").strip()
    password = broker.get("password")
    has_password = isinstance(password, str) and bool(password)
    if has_password:
        if not username:
            validation["errors"].append(
                _issue(
                    "zendure_mqtt_broker_username_missing",
                    "The Zendure MQTT broker password needs a username.",
                )
            )
            return name
        # The secret never lands in config.json: the profile carries only a
        # non-secret credentials_ref and the username/password are persisted to
        # the external EMS credential store by the apply/write transaction.
        profile["credentials_ref"] = manual_broker_credentials_ref(broker)
    elif username:
        # A username with no password is not a secret, so it stays inline.
        profile["username"] = username
    _set_broker_profile(preview, name, profile)
    return name


def _manual_zendure_mqtt_proposals(
    manual_devices, broker_ref, validation, broker_source=None
):
    """Build config proposals from manual Zendure MQTT device inputs.

    Delegates fragment building to the shared ``zendure_mqtt_config_draft`` helper
    so manual setup and maintenance stay in sync, then reuses the discovered-
    proposal merge path (sanitize, capability validation, broker resolution,
    duplicate detection). ``broker_source`` is the provisioned broker profile's
    transport, which is a write-capability axis the manual form cannot invent.
    """

    if not isinstance(manual_devices, list) or not manual_devices:
        return []
    proposals = []
    for item in manual_devices[:_MAX_ZENDURE_MQTT_PROPOSALS]:
        if not isinstance(item, dict):
            continue
        fragment, issues = build_manual_zendure_mqtt_fragment(
            item, broker_ref, broker_source=broker_source
        )
        if fragment is None:
            for issue in issues:
                validation["errors"].append(_issue(issue["code"], issue["message"]))
            continue
        fragment_mqtt = fragment.get("mqtt") if isinstance(fragment.get("mqtt"), dict) else {}
        identifier = fragment.get("serial_number") or fragment_mqtt.get("device_id") or ""
        proposal = {
            "id": f"manual-zendure-mqtt:{identifier}",
            "config_fragment": fragment,
        }
        if "name" in item:
            proposal["config_name"] = str(item.get("name") or "").strip()
        proposals.append(proposal)
    return proposals


def _explicit_grid_type(item, features):
    # An explicit meter type (chosen manually, or selected in the feature form)
    # always wins over inference so the system never guesses hardware from IP/port.
    for key in ("grid_meter_type", "meter_type"):
        explicit = str(item.get(key) or "").strip().lower()
        if explicit in _GRID_TYPE_CHOICES:
            return explicit
    feature_type = str((features or {}).get("grid_meter.type") or "").strip().lower()
    if feature_type in _GRID_TYPE_CHOICES:
        return feature_type
    return ""


def _inferred_grid_type(item):
    """Infer a concrete type from discovery metadata, or ``""`` when unknown."""

    family = str(item.get("api_family") or "").lower()
    device_type = str(item.get("device_type") or "").lower()
    if family in _GRID_TYPES:
        return _GRID_TYPES[family]
    description = f"{family} {device_type}"
    if "ecotracker" in description:
        return "ecotracker"
    if "3em" in description and "gen1" in description:
        return "shelly_3em_gen1"
    if "tasmota" in description:
        return "tasmota_http"
    return ""


def _apply_typed_fields(device, item):
    for source, candidates in (
        ("device_type", ("device_type", "type")),
        ("api_family", ("api_family", "api_type")),
    ):
        value = item.get(source)
        if value:
            for key in candidates:
                if key in device:
                    device[key] = value
                    break


def _build_grid_meter(meter, defaults, validation, features):
    """Build the grid-meter block, or ``None`` when no concrete type is resolved.

    Returning ``None`` (an unresolved neutral Zendure candidate) tells the caller
    to leave ``grid_meter`` out so the preview stays not-ready — it must never
    silently fall back to Shelly.
    """

    display_name = str(meter.get("display_name") or "").strip()
    host = str(meter.get("ip") or "").strip()
    if not display_name:
        validation["errors"].append(
            _issue("display_name_empty", "The grid meter needs a display name.")
        )

    grid_type = _explicit_grid_type(meter, features) or _inferred_grid_type(meter)
    if not grid_type:
        grid_type = str((defaults or {}).get("type") or "").strip().lower() or "shelly"

    grid = copy.deepcopy(defaults) if isinstance(defaults, dict) else {}
    grid["type"] = grid_type

    # HTTP meters need a reachable host; MQTT meters (generic/D0) do not carry
    # an IP at all, so a missing IP must not fail an MQTT grid meter.
    spec = grid_meter_variant_field_spec(grid_type)
    if spec is None or "ip" in spec["keys"]:
        if not _valid_host(host):
            validation["errors"].append(
                _issue(
                    "grid_meter_host_invalid",
                    "The grid meter has an invalid IP address or hostname.",
                )
            )
        grid["ip"] = host
        # Preserve the discovered port (default 80) so a meter advertised on a
        # non-default port keeps working at runtime instead of silently
        # falling back to port 80.
        if "port" in spec["keys"] and meter.get("port"):
            try:
                grid["port"] = int(meter["port"])
            except (TypeError, ValueError):
                grid.pop("port", None)
    return grid


def _resolve_d0_serial(meter, existing_topic):
    """Resolve the D0 serial: discovery/manual serial first, then a valid topic.

    The device IP is never used as a serial. Both a discovery-reported serial and
    a serial typed into the D0 form arrive on the meter draft as ``serial_number``.
    """

    for key in ("serial_number", "sn", "serial"):
        serial = str((meter or {}).get(key) or "").strip()
        if serial:
            return serial
    return zendure_smartmeter_d0_serial_from_topic(existing_topic)


def _apply_d0_defaults(grid, meter, validation):
    mqtt = grid.get("mqtt")
    if not isinstance(mqtt, dict):
        mqtt = {}
        grid["mqtt"] = mqtt
    existing_topic = str(mqtt.get("topic") or "").strip()
    # The serial always identifies the D0 device and provides the canonical
    # topic default; it is required even when a custom topic is supplied.
    serial = _resolve_d0_serial(meter, existing_topic)
    if not serial:
        validation["errors"].append(
            _issue(
                "grid_meter_d0_serial_missing",
                "Enter the Zendure SmartMeter D0 serial number. It is required and "
                "provides the default totalPower MQTT topic.",
            )
        )
    elif not existing_topic:
        mqtt["topic"] = zendure_smartmeter_d0_topic(serial)
    else:
        # A supplied topic is preserved exactly. Ownership (auto vs manual) is a
        # UI concern; the backend must never overwrite a non-empty topic just
        # because its shape looks canonical.
        mqtt["topic"] = existing_topic
    mqtt["payload_format"] = "number"
    mqtt.pop("value_path", None)


def _normalize_bundled_influx_secret(config):
    """Point bundled InfluxDB secrets at the mounted ``config/`` volume.

    Admin deployments are always Docker-first, where only ``/app/config`` and
    ``/app/data`` are writable. The template default (``deploy/docker/...``)
    lives in the read-only image, so bundled ``influx init`` cannot write there.
    External InfluxDB keeps its own secret path.
    """

    influx = config.get("influxdb")
    if not isinstance(influx, dict):
        return
    if influx.get("enabled") is True and influx.get("mode") == "bundled":
        influx["secret_file"] = DOCKER_FIRST_SECRET_FILE


def _load_existing_config(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None, _issue(
            "existing_config_unreadable", f"Could not read the existing EMS config at {path}."
        )
    try:
        data = json.loads(text)
    except ValueError:
        return None, _issue(
            "existing_config_invalid_json", f"The existing EMS config at {path} is not valid JSON."
        )
    if not isinstance(data, dict):
        return None, _issue(
            "existing_config_not_object", f"The existing EMS config at {path} is not a JSON object."
        )
    return data, None


class ConfigPreviewGenerator:
    """Generate a config preview, using a real EMS config as base when one exists."""

    def __init__(
        self,
        release_manager,
        install_context_provider=detect_install_context,
        zendure_cloud_auth_available=_default_zendure_cloud_auth_available,
    ):
        self.release_manager = release_manager
        self.install_context_provider = install_context_provider
        self.zendure_cloud_auth_available = zendure_cloud_auth_available

    def _install_context(self):
        # A failing install probe must not break the release-template preview path.
        try:
            return self.install_context_provider()
        except Exception:
            return None

    def _resolve_base(self, template, validation):
        context = self._install_context()
        if context is not None and context.config_exists:
            base_meta = {
                "source": "existing_config",
                "config_path": str(context.config_path),
                "config_source": context.config_source,
                "template_path": str(context.template_path),
                "template_source": context.template_source,
            }
            base_config, error = _load_existing_config(context.config_path)
            if error is not None:
                validation["errors"].append(error)
                return base_meta, None
            return base_meta, base_config
        return {"source": "release_template"}, template

    def generate(
        self,
        draft=None,
        supported_grid_meter_count=None,
        features=None,
        zendure_mqtt_proposals=None,
        zendure_mqtt_broker=None,
        zendure_mqtt_manual_devices=None,
    ):
        validation = {"errors": [], "warnings": [], "info": []}
        try:
            resource = self.release_manager.config_template()
        except ReleaseError:
            validation["errors"].append(
                _issue(
                    "release_resources_not_prepared",
                    "Prepare release resources before generating a config preview.",
                )
            )
            return {
                "ready": False,
                "release": None,
                "template_loaded": False,
                "config": None,
                "base": {"source": "release_template"},
                "validation": validation,
            }

        template = resource.get("template")
        tag = resource.get("tag")
        if not isinstance(template, dict):
            validation["errors"].append(
                _issue("template_invalid", "config.template.json is not a JSON object.")
            )
            return {
                "ready": False,
                "release": tag,
                "template_loaded": False,
                "config": None,
                "base": {"source": "release_template"},
                "validation": validation,
            }

        base_meta, base_config = self._resolve_base(template, validation)
        if base_config is None:
            return {
                "ready": False,
                "release": tag,
                "template_loaded": True,
                "config": None,
                "base": base_meta,
                "validation": validation,
            }
        existing_base = base_meta["source"] == "existing_config"
        preview = copy.deepcopy(base_config)

        items = draft if isinstance(draft, list) else []
        enabled = [item for item in items if isinstance(item, dict) and item.get("enabled", True)]
        inverters = [item for item in enabled if item.get("role") == "inverter"]
        meters = [item for item in enabled if item.get("role") == "grid_meter"]

        raw_proposals = list(zendure_mqtt_proposals) if isinstance(
            zendure_mqtt_proposals, list
        ) else []
        grid_meter_proposals = [
            p for p in raw_proposals
            if _proposal_target(p) == _GRID_METER_PROPOSAL_TARGET
        ]
        device_proposals = [
            p for p in raw_proposals
            if _proposal_target(p) != _GRID_METER_PROPOSAL_TARGET
        ]

        prototypes = template.get("devices")
        if not isinstance(prototypes, list) or not all(
            isinstance(item, dict) for item in prototypes
        ):
            validation["errors"].append(
                _issue("template_devices_invalid", "The release template has no usable devices list.")
            )
            prototypes = []
        prototype = prototypes[0] if prototypes else {}

        names = []
        if existing_base:
            self._merge_existing_devices(preview, inverters, prototype, names, validation)
        else:
            self._build_template_devices(preview, inverters, prototypes, prototype, names, validation)

        if not inverters:
            validation["warnings"].append(
                _issue("inverter_missing", "No active Zendure inverter is selected.")
            )

        self._apply_grid_meter(
            preview, meters, template, names, validation,
            supported_grid_meter_count, existing_base, features,
            mqtt_grid_meter_selected=bool(grid_meter_proposals),
        )

        # Catalog-driven feature values are applied last so setup choices (winter,
        # dashboard, InfluxDB, grid meter variant, ...) override template/base
        # defaults while device entries stay owned by the draft above.
        applied_features = apply_setup_features(preview, features)
        if applied_features:
            validation["info"].append(
                _issue(
                    "setup_features_applied",
                    f"Applied {len(applied_features)} setup feature value(s).",
                )
            )

        # Manual broker/devices reuse the discovered-proposal path: provision the
        # user's broker first so the manual devices' broker_ref resolves against
        # an already-declared profile.
        broker_ref = _apply_manual_zendure_mqtt_broker(
            preview, zendure_mqtt_broker, validation
        )
        proposals = list(device_proposals)
        proposals.extend(
            _manual_zendure_mqtt_proposals(
                zendure_mqtt_manual_devices,
                broker_ref,
                validation,
                broker_sources_from_config(preview).get(broker_ref),
            )
        )
        mqtt_devices = _merge_zendure_mqtt_proposals(
            preview, proposals, validation, self.zendure_cloud_auth_available
        )
        # MQTT device names join the same uniqueness gate as API inverters and
        # the grid meter (parity with the maintenance validator, which already
        # includes them).
        names.extend(
            str(device.get("name") or "")
            for device in (preview.get("devices") or [])
            if isinstance(device, dict) and device.get("type") == "zendure_mqtt"
        )

        # Silent-HTTP guard: cloud MQTT is connected but no device is selected
        # over it, so the inverters would be written for local HTTP only.
        mqtt_selected = any(
            isinstance(device, dict) and device.get("type") == "zendure_mqtt"
            for device in (preview.get("devices") or [])
        )
        if inverters and not mqtt_selected and self.zendure_cloud_auth_available():
            validation["warnings"].append(
                _issue(
                    "zendure_mqtt_cloud_devices_not_selected",
                    "Zendure Cloud MQTT is connected but no Zendure MQTT device "
                    "is selected. The selected inverters will be written for "
                    "local HTTP control only. Select your Zendure MQTT devices "
                    "to control them over MQTT.",
                )
            )

        # ``default`` is reserved for the implicit legacy top-level broker; a
        # carried or hand-edited named brokers.default is rejected before ready.
        _reserved_broker_ref_errors(preview, validation)

        # EMS owns broker semantics: block any enabled Zendure MQTT device whose
        # broker profile is unusable (unknown/disabled/incomplete/auth-missing),
        # including devices carried over from an existing base config.
        for issue in find_zendure_mqtt_broker_profile_issues(preview):
            validation["errors"].append(
                _issue(
                    issue["code"],
                    f"{issue['message']}. Configure the broker before applying.",
                )
            )

        # Runs after features so the final, user-selected grid-meter type drives
        # D0 topic generation and the variant-consistency cleanup.
        self._normalize_grid_meter_variant(preview, meters, validation)

        # A selected D0 MQTT grid meter is applied last so it owns the final
        # grid_meter block; it is written to grid_meter only, never to devices[].
        _merge_zendure_mqtt_grid_meter_proposal(
            preview, grid_meter_proposals, meters, validation,
            self.zendure_cloud_auth_available,
        )

        # Final parity gate: a MQTT grid meter (D0 proposal, carried base config
        # or feature-applied) must pass the same Core resolver EMS uses at
        # startup, so a "ready" preview can never fail at runtime.
        _validate_mqtt_grid_meter_via_core(preview, validation)

        _normalize_bundled_influx_secret(preview)

        duplicate_names = sorted({name for name in names if name and names.count(name) > 1})
        if any(not name for name in names):
            validation["errors"].append(
                _issue("config_name_empty", "Every selected device needs a config name.")
            )
        if duplicate_names:
            validation["errors"].append(
                _issue(
                    "config_name_duplicate",
                    f"Config names must be unique: {', '.join(duplicate_names)}.",
                )
            )

        for issue in find_duplicate_zendure_device_identities(
            preview.get("devices"),
            broker_sources=broker_sources_from_config(preview),
        ):
            validation["errors"].append(
                _issue(
                    issue["code"],
                    f"{issue['message']} Edit or remove the existing device before "
                    "adding it again.",
                )
            )

        if not has_runtime_control_device(preview):
            validation["errors"].append(
                _issue(
                    "no_control_devices",
                    "Select at least one API inverter or an enabled MQTT inverter with output control; telemetry-only MQTT devices cannot run the EMS control loop.",
                )
            )

        try:
            json.dumps(preview, allow_nan=False)
        except (TypeError, ValueError):
            validation["errors"].append(
                _issue("config_not_serializable", "The generated config is not valid JSON data.")
            )

        validation["info"].append(
            _issue("template_loaded", f"Release template {tag} loaded.")
        )
        if existing_base:
            validation["info"].append(
                _issue("existing_config_base", "Existing EMS config used as preview base.")
            )
        if not duplicate_names and names and all(names):
            validation["info"].append(
                _issue("config_names_unique", "Device config names are unique.")
            )
        grid_meter_summary = _grid_meter_summary(preview)
        return {
            "ready": not validation["errors"],
            "release": tag,
            "template_loaded": True,
            "config": preview,
            "base": base_meta,
            "summary": {
                "inverters": len(inverters),
                # Derived from the final merged preview, not the discovered draft,
                # so a selected MQTT D0 (which never appears in the meter draft)
                # is still reported as the active grid meter.
                "grid_meters": 1 if grid_meter_summary else 0,
                "grid_meter": grid_meter_summary,
                "zendure_mqtt_devices": mqtt_devices,
            },
            "validation": validation,
        }

    def _build_template_devices(self, preview, inverters, prototypes, prototype, names, validation):
        generated_devices = []
        for index, item in enumerate(inverters, 1):
            if "config_name" in item:
                name = str(item.get("config_name") or "").strip()
            else:
                name = next_compact_inverter_name(names, len(generated_devices))
            label = name or f"inverter {index}"
            display_name = str(item.get("display_name") or "").strip()
            host = str(item.get("ip") or "").strip()
            serial = str(item.get("serial_number") or "").strip()
            names.append(name)
            if not display_name:
                validation["errors"].append(_issue("display_name_empty", f"{label} needs a display name."))
            if not _valid_host(host):
                validation["errors"].append(
                    _issue("device_host_invalid", f"{label} has an invalid IP address or hostname.")
                )
            device = copy.deepcopy(prototypes[index - 1] if index <= len(prototypes) else prototype)
            if "name" in prototype or device:
                device["name"] = name
            if "ip" in prototype or device:
                device["ip"] = host
            if "sn" in device:
                device["sn"] = serial
                if not serial:
                    validation["errors"].append(
                        _issue("device_serial_missing", f"{label} requires a serial number.")
                    )
            _apply_typed_fields(device, item)
            apply_device_config_values(device, item.get("config_values"))
            generated_devices.append(device)
        preview["devices"] = generated_devices

    def _merge_existing_devices(self, preview, inverters, prototype, names, validation):
        devices = preview.get("devices")
        if not isinstance(devices, list):
            devices = []
            preview["devices"] = devices
        by_name = {}
        for device in devices:
            if isinstance(device, dict):
                key = str(device.get("name") or "")
                if key and key not in by_name:
                    by_name[key] = device

        allocation_names = [
            str(device.get("name") or "").strip()
            for device in devices
            if isinstance(device, dict)
        ]
        allocation_count = len(devices)
        for index, item in enumerate(inverters, 1):
            if "config_name" in item:
                name = str(item.get("config_name") or "").strip()
            else:
                name = next_compact_inverter_name(
                    allocation_names, allocation_count
                )
            label = name or f"inverter {index}"
            display_name = str(item.get("display_name") or "").strip()
            host = str(item.get("ip") or "").strip()
            serial = str(item.get("serial_number") or "").strip()
            names.append(name)
            allocation_count += 1
            if name:
                allocation_names.append(name)
            if not display_name:
                validation["errors"].append(_issue("display_name_empty", f"{label} needs a display name."))
            if not _valid_host(host):
                validation["errors"].append(
                    _issue("device_host_invalid", f"{label} has an invalid IP address or hostname.")
                )

            match = by_name.get(name)
            if match is not None:
                match["ip"] = host
                if serial:
                    match["sn"] = serial
                _apply_typed_fields(match, item)
                apply_device_config_values(match, item.get("config_values"))
                if "sn" in match and not str(match.get("sn") or "").strip():
                    validation["errors"].append(
                        _issue("device_serial_missing", f"{label} requires a serial number.")
                    )
                continue

            device = copy.deepcopy(prototype)
            device["name"] = name
            device["ip"] = host
            if "sn" in device:
                device["sn"] = serial
                if not serial:
                    validation["errors"].append(
                        _issue("device_serial_missing", f"{label} requires a serial number.")
                    )
            _apply_typed_fields(device, item)
            apply_device_config_values(device, item.get("config_values"))
            devices.append(device)

    def _normalize_grid_meter_variant(self, preview, meters, validation):
        grid = preview.get("grid_meter")
        if not isinstance(grid, dict):
            return
        grid_type = str(grid.get("type") or "").strip().lower()
        if not grid_type:
            # An empty type means no concrete grid meter was chosen (e.g. an
            # unresolved neutral candidate whose empty type feature leaked in).
            # Never emit a typeless grid meter.
            preview.pop("grid_meter", None)
            return
        meter = meters[0] if meters else {}
        if grid_type == ZENDURE_SMARTMETER_D0_GRID_METER_TYPE:
            _apply_d0_defaults(grid, meter, validation)
        strip_incompatible_grid_meter_fields(grid, grid_type)

    def _apply_grid_meter(
        self, preview, meters, template, names, validation,
        supported_grid_meter_count, existing_base, features,
        mqtt_grid_meter_selected=False,
    ):
        if len(meters) > 1:
            validation["errors"].append(
                _issue("grid_meter_duplicate", "Choose only one grid meter for EMS control.")
            )
            if not existing_base:
                preview.pop("grid_meter", None)
            return

        if len(meters) == 1:
            meter = meters[0]
            names.append(str(meter.get("config_name") or "").strip())
            defaults = preview.get("grid_meter") if existing_base else template.get("grid_meter", {})
            if not isinstance(defaults, dict):
                defaults = template.get("grid_meter", {})
            grid = _build_grid_meter(meter, defaults, validation, features)
            if grid is None:
                # Unresolved neutral candidate: no grid meter is generated.
                preview.pop("grid_meter", None)
            else:
                preview["grid_meter"] = grid
            return

        if existing_base and isinstance(preview.get("grid_meter"), dict):
            return

        preview.pop("grid_meter", None)
        if mqtt_grid_meter_selected:
            # A D0 MQTT grid-meter proposal supplies the grid signal instead of a
            # discovered HTTP meter, so no "missing grid meter" error is emitted.
            return
        if isinstance(supported_grid_meter_count, int) and supported_grid_meter_count >= 2:
            validation["errors"].append(
                _issue(
                    "grid_meter_ambiguous",
                    "Multiple supported grid meters were found. Choose one grid meter for EMS control.",
                )
            )
        elif "grid_meter" in template:
            validation["errors"].append(
                _issue("grid_meter_missing", "Choose a grid meter for EMS control.")
            )
        else:
            validation["warnings"].append(
                _issue("grid_meter_missing", "No grid meter is selected.")
            )
