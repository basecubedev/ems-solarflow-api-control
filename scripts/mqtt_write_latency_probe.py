# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hardware probe for Zendure MQTT power control (latency + effectiveness).

The command channel is MQTT using the device client's exact production command
builder (atomic model-specific property set, QoS 1, non-retained, iot/… topic);
the observation channel is the device's local HTTP API, polled fast. The HTTP
read sidesteps the ~30 s MQTT telemetry cadence, so the measured latency is the
real end-to-end time from "publish" to "setpoint visible on the device".

Probe modes:

``--dry-preview``
    Resolve devices, print the operation plan (topic, QoS, properties, gates,
    current mode/limit state) and write nothing.

``--confirm-writes`` (default test)
    Setpoint landing test: alternate between two safe targets and measure the
    publish-to-HTTP-visible latency. Distinguishes per sample whether the
    setpoint landed, whether the commanded mode (acMode) landed, and — with
    ``--verify-output`` — whether the physical output reacted, was prevented by
    conditions (SOC at minimum), or did not react.

``--mode-test`` (additionally requires ``--confirm-mode-changes``)
    Only applicable when the device is currently NOT in smart AC output mode:
    proves the corrected atomic command path switches the required mode and
    lands the target. The probe never forces a device out of output mode.

Restore: the complete initial power state (smartMode/acMode/outputLimit/
inputLimit) is captured before any write and restored through the production
property-write path on success, failure, timeout and interruption, then
verified over HTTP.

This tool writes real power values to real hardware. It must never run in
parallel with the live EMS (single writer); a contention check aborts on a
foreign writer. It respects the installation's write gates and dry_run posture,
never mutates config.json and starts no control loop.

Usage (run on the device host, with the EMS stopped):

    python3 scripts/mqtt_write_latency_probe.py --dry-preview
    python3 scripts/mqtt_write_latency_probe.py --confirm-writes --samples 12
    python3 scripts/mqtt_write_latency_probe.py --confirm-writes --verify-output
    python3 scripts/mqtt_write_latency_probe.py --mode-test --confirm-writes \\
        --confirm-mode-changes
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ems.mqtt_control.command_state import property_matches  # noqa: E402
from ems.device_identity import normalize_physical_serial  # noqa: E402
from ems.external_status import mask_mqtt_topic, mask_route_identifier  # noqa: E402


def bootstrap_config(config_arg):
    """Load config like the EMS runtime bootstrap, but never write to disk.

    Mirrors ``ems.config.load_config`` minus ``perform_startup_config_upgrade`` (which
    can rewrite ``config.json`` in ``apply`` mode). ``dry_run`` and the write gates are
    populated on the module the same way the running EMS sees them (``dry_run`` defaults
    to true = no writes) so that ``cfg.control_writes_allowed`` respects the operator's
    real safety posture: a ``dry_run: true`` install refuses writes here too.
    """

    from ems import config as cfg
    from ems.paths import BASE_DIR, resolve_config_path

    base_dir = BASE_DIR or "."
    path = str(resolve_config_path(config_arg, base_dir=base_dir))
    raw = cfg._read_raw_config(path)
    runtime_config = cfg.apply_runtime_config_defaults(raw)
    config = cfg.apply_template_placeholder_safety(
        runtime_config, emit_message=lambda *_a, **_k: None
    )

    system = config.get("system", {}) if isinstance(config, dict) else {}
    cfg.CONFIG = config
    cfg.BASE_DIR = base_dir
    cfg.ARGS = argparse.Namespace(
        config=path,
        replay=False,
        simulate=False,
        self_test=False,
        no_ha=True,
        dry_run=False,
        once=False,
        preflight=False,
        duration=None,
        max_cycles=None,
    )
    cfg.DRY_RUN = bool(system.get("dry_run", True))
    cfg.SIMULATION_MODE = False
    cfg.ALLOW_HARDWARE_WRITES = bool(system.get("allow_hardware_writes", False))
    cfg.ALLOW_MQTT_LOCAL_CONTROL_WRITES = bool(
        system.get("allow_mqtt_local_control_writes", False)
    )
    cfg.ALLOW_MQTT_ZENDURE_CONTROL_WRITES = bool(
        system.get("allow_mqtt_zendure_control_writes", False)
    )
    max_device_power = system.get("max_device_power")
    if isinstance(max_device_power, (int, float)) and max_device_power > 0:
        cfg.MAX_DEVICE_POWER = int(max_device_power)
    return cfg, config, path


def _last4(value):
    text = str(value or "")
    return text[-4:] if text else ""


def _redact_candidate(dev):
    """A secret-free identity summary of one candidate device for an error list."""

    return (
        f"name={getattr(dev, 'name', None)} "
        f"source={getattr(dev, 'source', None)} "
        f"hardware_profile={getattr(dev, 'hardware_profile', None)} "
        f"broker_ref={getattr(dev, 'broker_ref', None)} "
        f"serial=…{_last4(getattr(dev, 'sn', None))} "
        f"device_id={mask_route_identifier(getattr(dev, '_device_id', None))}"
    )


def _candidate_list(devices):
    return "; ".join(_redact_candidate(d) for d in devices)


def _display_mqtt_topic(dev, topic):
    """Return a display-safe topic, failing closed for unknown cloud shapes."""

    cloud_scoped = getattr(dev, "source", None) == "zendure_cloud_mqtt"
    masked = mask_mqtt_topic(topic, cloud_scoped=cloud_scoped)
    if cloud_scoped and topic and masked == topic:
        return "<redacted-cloud-topic>"
    return masked


def _clean_serial(value):
    text = str(value).strip() if value is not None else ""
    return text or None


def _serial_match_key(value):
    """Shared case-insensitive serial key; ``None`` for masked/empty values."""

    return normalize_physical_serial(value)


def select_mqtt_device(runtime, *, name=None, serial=None, device_id=None,
                       broker_ref=None):
    """Select exactly one configured MQTT control device by explicit selectors.

    Selection uses only configured selectors (``--device-name``/``--serial``/
    ``--device-id``/``--broker-ref``). A live HTTP-reported serial is never a
    selector here — a Cloud route id and a physical serial are different identity
    domains, so that decision is made separately by the binding step. Zero matches
    is an error; more than one aborts with a redacted candidate list — the probe
    never silently writes to the first of several matching devices.
    """

    devices = list(getattr(runtime, "devices", []) or [])
    if not devices:
        return None, "no MQTT control device is configured (no write-capable zendure_mqtt entry)"

    def matches(dev):
        if name is not None and getattr(dev, "name", None) != name:
            return False
        if serial is not None and str(getattr(dev, "sn", "")) != str(serial):
            return False
        if device_id is not None and str(getattr(dev, "_device_id", "")) != str(device_id):
            return False
        if broker_ref is not None and getattr(dev, "broker_ref", None) != broker_ref:
            return False
        return True

    candidates = [d for d in devices if matches(d)]
    if len(candidates) == 1:
        return candidates[0], None
    if not candidates:
        return None, (
            "no MQTT control device matches the given selectors "
            f"(--device/--serial/--device-id/--broker-ref). Devices: {_candidate_list(devices)}"
        )
    return None, (
        f"ambiguous device selection: {len(candidates)} devices match. Narrow with "
        f"--device-name/--serial/--device-id/--broker-ref. Candidates: "
        f"{_candidate_list(candidates)}"
    )


def select_by_trusted_serial(runtime, http_serial):
    """Select the one device whose *trusted* physical serial equals the readback.

    Only a device with a configured physical serial can be matched by an
    HTTP-reported serial. A serial-less device (its ``sn`` falls back to the Cloud
    route id, a different identity domain) is never auto-selected this way and must
    be identified with explicit route selectors.
    """

    devices = list(getattr(runtime, "devices", []) or [])
    if not devices:
        return None, "no MQTT control device is configured (no write-capable zendure_mqtt entry)"
    # Serial matching folds case (shared identity rule): the physical serial is
    # the same identity whether the HTTP readback reports it upper- or lower-case.
    target = _serial_match_key(http_serial)
    candidates = [
        d for d in devices
        if (key := _serial_match_key(getattr(d, "physical_serial", None))) is not None
        and key == target
    ]
    if len(candidates) == 1:
        return candidates[0], None
    if not candidates:
        return None, (
            "no MQTT control device has a trusted physical serial matching the HTTP "
            f"readback {mask_route_identifier(http_serial)}. A serial-less Cloud "
            "device must be selected explicitly with "
            f"--device-name/--device-id/--broker-ref. Devices: {_candidate_list(devices)}"
        )
    return None, (
        f"ambiguous device selection: {len(candidates)} devices share this trusted "
        "serial. Narrow with --device-name/--device-id/--broker-ref. "
        f"Candidates: {_candidate_list(candidates)}"
    )


BINDING_VERIFIED = "verified"
BINDING_CONFLICT = "serial_conflict"
BINDING_UNBOUND = "unbound_readback"
BINDING_UNVERIFIED = "unverified_readback"


@dataclass(frozen=True)
class HttpBinding:
    """Cross-transport binding verdict between the MQTT device and HTTP readback."""

    status: str
    configured_serial: str | None
    http_serial: str | None

    @property
    def verified(self) -> bool:
        return self.status == BINDING_VERIFIED

    def summary(self) -> str:
        http = mask_route_identifier(self.http_serial)
        if self.status == BINDING_VERIFIED:
            return f"verified (configured serial matches HTTP readback {http})"
        if self.status == BINDING_CONFLICT:
            return (
                "CONFLICT (configured serial "
                f"{mask_route_identifier(self.configured_serial)} != HTTP readback "
                f"{http})"
            )
        if self.status == BINDING_UNVERIFIED:
            return "unverified (no HTTP readback serial available to confirm identity)"
        return f"unverified (physical serial not stored; HTTP readback reports {http})"

    def write_block_reason(self, *, acknowledged, exact_selectors) -> str | None:
        """Reason the binding forbids a hardware write, or ``None`` when allowed."""

        if self.status == BINDING_VERIFIED:
            return None
        if self.status == BINDING_CONFLICT:
            return (
                "cross-transport identity conflict: configured physical serial "
                f"{mask_route_identifier(self.configured_serial)} does not match the "
                f"HTTP readback serial {mask_route_identifier(self.http_serial)}; "
                "refusing to write to a device with a contradictory identity."
            )
        if self.status == BINDING_UNVERIFIED:
            return (
                "cross-transport binding unverified: no HTTP readback serial was "
                "available to confirm the selected device before writing."
            )
        if not exact_selectors:
            return (
                "serial-less Cloud device: an unbound HTTP readback requires exact "
                "--device-name, --device-id and --broker-ref selectors so the Cloud "
                "route is identified explicitly before any write."
            )
        if not acknowledged:
            return (
                "serial-less Cloud device: the Cloud route and the HTTP readback "
                f"serial {mask_route_identifier(self.http_serial)} are different "
                "identity domains and are not proven to be the same inverter. Pass "
                "--confirm-unbound-api-readback to accept this readback for this run "
                "(never persisted), or bind the physical serial via Admin discovery "
                "first."
            )
        return None


def evaluate_http_binding(dev, http_serial):
    """Classify the binding between a selected MQTT device and an HTTP serial."""

    # Original values are kept for display; equality folds case via the shared
    # serial key, so a case-only difference is a verified match, not a conflict.
    configured = _clean_serial(getattr(dev, "physical_serial", None))
    http = _clean_serial(http_serial)
    configured_key = _serial_match_key(getattr(dev, "physical_serial", None))
    http_key = _serial_match_key(http_serial)
    if configured is not None:
        if http is None:
            return HttpBinding(BINDING_UNVERIFIED, configured, None)
        if configured_key is not None and configured_key == http_key:
            return HttpBinding(BINDING_VERIFIED, configured, http)
        return HttpBinding(BINDING_CONFLICT, configured, http)
    if http is None:
        return HttpBinding(BINDING_UNVERIFIED, None, None)
    return HttpBinding(BINDING_UNBOUND, None, http)


def resolve_mqtt_device(runtime, name, *, serial=None, device_id=None, broker_ref=None):
    """Pick the single MQTT control device to write to; fail closed on ambiguity."""

    devices = list(getattr(runtime, "devices", []) or [])
    if name is None and serial is None and device_id is None and broker_ref is None:
        if len(devices) == 1:
            return devices[0], None
        if not devices:
            return None, "no MQTT control device is configured (no write-capable zendure_mqtt entry)"
        return None, (
            "multiple MQTT control devices; pass --device-name/--serial/--device-id/"
            f"--broker-ref. Candidates: {_candidate_list(devices)}"
        )
    return select_mqtt_device(
        runtime, name=name, serial=serial, device_id=device_id, broker_ref=broker_ref
    )


def resolve_http_reader(cfg, config, session, api_device, api_ip, mqtt_dev):
    """Build a ZendureClient that reads power state from the local HTTP API.

    The read target is the same physical inverter as the MQTT write target. It is
    resolved from ``--api-ip``, from ``--api-device`` (an HTTP device entry name), or
    by matching the MQTT device's serial number against the HTTP device entries.
    """

    from ems.clients import ZendureClient

    http_configs = cfg.http_control_device_configs(config.get("devices"))

    if api_ip:
        return ZendureClient(
            mqtt_dev.name if mqtt_dev else "api",
            api_ip,
            mqtt_dev.sn if mqtt_dev else None,
            session,
            0,
            0,
            1,
            None,
        ), None

    chosen = None
    if api_device:
        chosen = next((d for d in http_configs if d.get("name") == api_device), None)
        if chosen is None:
            names = ", ".join(sorted(str(d.get("name")) for d in http_configs))
            return None, f"no HTTP device named {api_device!r} (available: {names})"
    elif mqtt_dev is not None and mqtt_dev.sn:
        chosen = next((d for d in http_configs if d.get("sn") == mqtt_dev.sn), None)

    if chosen is None:
        return None, (
            "could not match an HTTP API device to the MQTT device; "
            "pass --api-device NAME or --api-ip IP"
        )
    if not chosen.get("ip"):
        return None, f"HTTP device {chosen.get('name')!r} has no ip"
    return ZendureClient(
        chosen.get("name"),
        chosen.get("ip"),
        chosen.get("sn"),
        session,
        chosen.get("min_soc", 0),
        chosen.get("max_soc", 0),
        chosen.get("smart_mode", 1),
        chosen.get("grid_off_mode"),
        chosen.get("max_power"),
    ), None


def fetch_api_report(ip, session):
    """GET the inverter's local HTTP ``/properties/report`` as raw JSON, or None."""

    try:
        response = session.get(f"http://{ip}/properties/report", timeout=3)
        return response.json()
    except Exception:
        return None


def serial_from_report(report):
    """Extract the device serial number from an HTTP ``/properties/report`` payload."""

    if not isinstance(report, dict):
        return None
    sources = [report]
    properties = report.get("properties")
    if isinstance(properties, dict):
        sources.append(properties)
    for source in sources:
        for key in ("sn", "serialNumber", "deviceSn"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def resolve_by_api_ip(runtime, session, api_ip, device_name, *, serial=None,
                      device_id=None, broker_ref=None):
    """Anchor on the HTTP API IP: read its serial, then select the MQTT device.

    Returns ``(dev, reader, resolved_serial, err)``. Explicit configured selectors
    identify the device; absent any selector the HTTP serial may auto-select only a
    device with a matching *trusted* physical serial. A serial-less Cloud device is
    never auto-bound to the HTTP serial — that binding is decided (and, when
    unverified, gated) separately by ``evaluate_http_binding``. Two devices sharing
    a serial abort with a redacted candidate list rather than silently selecting
    the first.
    """

    from ems.clients import ZendureClient

    report = fetch_api_report(api_ip, session)
    if report is None:
        return None, None, None, (
            f"could not read http://{api_ip}/properties/report "
            "(is the inverter's local HTTP API reachable at this IP?)"
        )
    reported_serial = serial_from_report(report)
    if not reported_serial:
        return None, None, None, (
            f"no serial number in the API report from {api_ip}; cannot match a device"
        )
    explicit = any(
        value is not None for value in (device_name, serial, device_id, broker_ref)
    )
    if explicit:
        dev, err = select_mqtt_device(
            runtime, name=device_name, serial=serial, device_id=device_id,
            broker_ref=broker_ref,
        )
    else:
        dev, err = select_by_trusted_serial(runtime, reported_serial)
    if err:
        return None, None, None, err
    reader = ZendureClient(dev.name, api_ip, reported_serial, session, 0, 0, 1, None)
    return dev, reader, reported_serial, None


def read_power_state(reader):
    """Return the device's current power-relevant state as a dict, or None.

    A writable property whose HTTP report value is absent (``None``) is omitted
    rather than defaulted, so mode verification can honestly report a property the
    device does not expose instead of confirming against a fabricated default.
    """

    state = reader.fetch()
    if state is None:
        return None
    result = {
        "outputHomePower": int(state.output),
        "soc": float(state.soc),
        "minSoc": float(state.min_soc),
        "solarInputPower": float(state.solar),
        "gridState": int(state.grid_state),
    }
    for key, attr in (
        ("smartMode", "smart_mode"),
        ("acMode", "ac_mode"),
        ("outputLimit", "output_limit"),
        ("inputLimit", "input_limit_w"),
    ):
        value = getattr(state, attr, None)
        if value is not None:
            result[key] = int(value)
    return result


def read_output_limit(reader):
    """Return the device's current ``outputLimit`` in watts, or None on read failure."""

    state = read_power_state(reader)
    return None if state is None else state.get("outputLimit")


def initial_restore_state(ip, session, required_properties):
    """Capture every operation-derived property from the initial HTTP report.

    Missing values stay missing.  The live preflight treats each omission as a
    hard failure; this reader must never invent a restorable value.
    """

    report = fetch_api_report(ip, session)
    if not isinstance(report, dict):
        return None
    properties = report.get("properties")
    if not isinstance(properties, dict):
        properties = report
    captured = {}
    for key in required_properties:
        value = properties.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        captured[key] = int(value)
    return captured


def pick_target(current, values):
    """Choose the next setpoint: the configured value furthest from ``current``.

    Alternating to the value with the largest delta keeps every sample a clear,
    detectable change even if the device rounds or clamps the setpoint slightly.
    """

    if current is None:
        return values[0]
    return max(values, key=lambda v: (abs(v - current), v))


def moved(observed, reference, tolerance):
    """Whether ``observed`` has moved away from ``reference`` beyond ``tolerance``."""

    if observed is None or reference is None:
        return False
    return abs(observed - reference) > tolerance


@dataclass
class Sample:
    index: int
    target: int
    baseline: int
    command_started_monotonic: float
    locally_accepted: bool
    # Time spent handing the publish to the local MQTT client.
    local_submit_duration_ms: float
    # Observed broker PUBACK delivery (delivered/timeout/untracked/pending).
    broker_delivery_status: str
    broker_delivery_from_submit_ms: float | None
    # HTTP-visible setpoint match relative to the same pre-submit origin.
    setpoint_match_from_submit_ms: float | None
    last_observed_output_limit: int | None
    # Movement away from baseline is diagnostics only — never setpoint landing.
    movement_observed: bool
    matched_target: bool
    timed_out: bool
    polls: int
    mode_ok: bool | None = None
    physical: str | None = None
    physical_reaction_from_submit_ms: float | None = None
    physical_reaction_after_setpoint_ms: float | None = None


@dataclass(frozen=True)
class OperationContract:
    """Factual state-change contract extracted from a production-built command."""

    target_w: int
    operation: str | None
    message_id: object
    topic: str
    qos: int
    retain: bool
    modified_properties: dict
    expected_properties: dict

    @property
    def restorable_properties(self):
        # The probe permits only an exact properties/write operation. Every field
        # in that atomic write may change and therefore requires an initial value.
        return tuple(self.modified_properties)


@dataclass
class WriteActivity:
    """Local transport truth used solely to decide whether restoration is needed."""

    attempted: bool = False
    locally_accepted: bool = False
    accepted_command_ids: list[str] = field(default_factory=list)
    accepted_modified_properties: list[str] = field(default_factory=list)
    # True only when HTTP observed the latest accepted command transition to its
    # target. A later accepted publish resets it until that command is observed.
    latest_accepted_state_observed: bool = False

    def record_submission(self, message_id, accepted, modified_properties=()):
        self.attempted = True
        if not accepted:
            return
        self.locally_accepted = True
        self.latest_accepted_state_observed = False
        command_id = str(message_id)
        if command_id not in self.accepted_command_ids:
            self.accepted_command_ids.append(command_id)
        for key in modified_properties:
            if key not in self.accepted_modified_properties:
                self.accepted_modified_properties.append(key)

    def record_latest_state_observed(self):
        if self.locally_accepted:
            self.latest_accepted_state_observed = True


@dataclass(frozen=True)
class TargetSubmission:
    """One local publish result and its common evidence-timeline origin."""

    command_started_monotonic: float
    locally_accepted: bool
    local_submit_duration_ms: float
    mid: int | None
    delivery_reference: object | None
    message_id: object
    expected_properties: dict
    modified_properties: tuple[str, ...]


@dataclass(frozen=True)
class CommandEvidence:
    broker_delivery_status: str
    broker_delivery_from_submit_ms: float | None
    setpoint_match_from_submit_ms: float | None
    last_observed_output_limit: int | None
    movement_observed: bool
    polls: int
    mode_ok: bool | None
    physical: str | None
    physical_reaction_from_submit_ms: float | None
    physical_reaction_after_setpoint_ms: float | None
    last_power_state: dict | None


def wait_for_broker(dev, timeout_s):
    """Block until the device's broker service reports connected, or time out."""

    deadline = time.monotonic() + timeout_s
    service = dev._service
    while time.monotonic() < deadline:
        if bool(getattr(service, "connected", False)):
            return True
        time.sleep(0.2)
    return bool(getattr(service, "connected", False))


def build_command(dev, target):
    """Build the model-correct production command without publishing it."""

    message, message_id, operation, expected = dev._build_write(target)
    return message, message_id, operation, expected


def _message_modified_properties(message):
    """Return the exact atomic ``properties`` mapping, or fail closed."""

    try:
        payload = json.loads(message.payload)
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            "production command has no inspectable property operation contract"
        ) from exc
    properties = payload.get("properties") if isinstance(payload, dict) else None
    if not isinstance(properties, dict) or not properties:
        raise RuntimeError(
            "production command does not expose the complete modified-property set"
        )
    if any(not isinstance(key, str) or not key for key in properties):
        raise RuntimeError("production command contains an invalid property name")
    return dict(properties)


def build_operation_contract(dev, target):
    """Extract the exact modified-property set from a production-built command.

    The probe intentionally does not keep a parallel model/property registry.  A
    command is probe-safe only when the production builder emits an atomic
    ``properties`` mapping whose complete effects can be captured and restored.
    Function/invoke or otherwise opaque operations fail closed here.
    """

    message, message_id, operation, expected = build_command(dev, target)
    properties = _message_modified_properties(message)
    expected_properties = dict(expected) if isinstance(expected, dict) else {}
    return OperationContract(
        target_w=target,
        operation=operation,
        message_id=message_id,
        topic=message.topic,
        qos=message.qos,
        retain=bool(message.retain),
        modified_properties=properties,
        expected_properties=expected_properties,
    )


def build_operation_contracts(dev, targets):
    """Build de-duplicated exact contracts for every command the probe may emit."""

    contracts = []
    seen = set()
    for target in targets:
        if target in seen:
            continue
        seen.add(target)
        contracts.append(build_operation_contract(dev, target))
    if not contracts:
        raise RuntimeError("no state-changing operation was planned")
    return tuple(contracts)


def required_restore_properties(contracts):
    """Return the stable union of fields any planned operation may modify."""

    return tuple(
        sorted(
            {
                key
                for contract in contracts
                for key in contract.restorable_properties
            }
        )
    )


def publish_target(dev, target, *, activity=None, now=time.monotonic):
    """Submit one production command and return explicit local transport truth.

    The monotonic origin is captured immediately before ``_publish_message`` and
    is shared by local-submit, PUBACK, HTTP setpoint and physical evidence. A
    builder failure occurs before the activity is marked attempted. A locally
    accepted publish requires restoration even when broker delivery is unknown.
    """

    message, message_id, _operation, expected = build_command(dev, target)
    if message.retain:
        raise RuntimeError("refusing to publish a retained control command")
    modified_properties = tuple(_message_modified_properties(message))
    command_started = now()
    try:
        submission = dev._publish_message(message)
    except Exception:
        if activity is not None:
            activity.record_submission(message_id, False, modified_properties)
        raise
    local_submit_ms = (now() - command_started) * 1000.0
    accepted = bool(getattr(submission, "accepted", submission))
    if activity is not None:
        activity.record_submission(message_id, accepted, modified_properties)
    mid = getattr(submission, "mid", None)
    delivery_reference = getattr(submission, "delivery_token", None) or mid
    return TargetSubmission(
        command_started_monotonic=command_started,
        locally_accepted=accepted,
        local_submit_duration_ms=local_submit_ms,
        mid=mid,
        delivery_reference=delivery_reference,
        message_id=message_id,
        expected_properties=dict(expected) if isinstance(expected, dict) else {},
        modified_properties=modified_properties,
    )


def classify_physical(power_state, target, tolerance):
    """Classify the physical reaction after a landed setpoint (honest, bounded)."""

    if power_state is None:
        return "unknown"
    output = power_state["outputHomePower"]
    if target == 0:
        return "output_reacted" if output <= tolerance else "not_reacted"
    if abs(output - target) <= tolerance:
        return "output_reacted"
    if power_state["soc"] <= power_state["minSoc"]:
        return "no_output_possible_soc_at_minimum"
    return "not_reacted"


def _observe_command_evidence(
    dev,
    reader,
    submission,
    baseline_state,
    target,
    opts,
    *,
    sleep=time.sleep,
    now=time.monotonic,
    progress=None,
):
    """Interleave PUBACK and HTTP/physical observation on one bounded timeline."""

    if progress is None:
        progress = _no_progress
    origin = submission.command_started_monotonic
    service = getattr(dev, "_service", None)
    confirmed = getattr(service, "delivery_confirmed", None)
    delivery_state = getattr(service, "delivery_status", None)
    if submission.delivery_reference is None or not callable(confirmed):
        delivery_status = (
            "pending"
            if submission.delivery_reference is not None
            and callable(delivery_state)
            else "untracked"
        )
    else:
        delivery_status = "pending"
    delivery_ms = None
    delivery_deadline = origin + opts.connect_timeout
    setpoint_deadline = origin + opts.timeout
    setpoint_done = False
    setpoint_at = None
    baseline_state = baseline_state if isinstance(baseline_state, dict) else {}
    baseline = baseline_state.get("outputLimit")
    observed = baseline
    movement_observed = False
    polls = 0
    mode_ok = None
    physical = None
    physical_at = None
    physical_deadline = None
    last_physical_verdict = None
    last_power_state = None
    physical_target_was_absent = (
        classify_physical(baseline_state, target, opts.output_tolerance)
        != "output_reacted"
    )

    while True:
        current_time = now()

        if delivery_status == "pending":
            reported_status = None
            if callable(delivery_state):
                try:
                    reported_status = str(
                        delivery_state(submission.delivery_reference)
                    ).strip().lower()
                except Exception:
                    reported_status = None
            if reported_status in {"delivered", "disconnected", "expired"}:
                delivery_status = reported_status
                if reported_status == "delivered":
                    delivery_ms = max(0.0, (current_time - origin) * 1000.0)
            try:
                delivered = bool(
                    delivery_status == "pending"
                    and callable(confirmed)
                    and confirmed(submission.delivery_reference)
                )
            except Exception:
                delivered = False
                delivery_status = "tracking_error"
            if delivered:
                delivery_status = "delivered"
                delivery_ms = max(0.0, (current_time - origin) * 1000.0)
            elif current_time >= delivery_deadline and delivery_status == "pending":
                delivery_status = "timeout"

        http_pending = not setpoint_done or (
            opts.verify_output and setpoint_at is not None and physical is None
        )
        if http_pending:
            power_state = read_power_state(reader)
            observed_at = now()
            polls += 1
            if power_state is not None:
                last_power_state = dict(power_state)
                observed = power_state.get("outputLimit")
                elapsed_s = max(0.0, observed_at - origin)
                progress(f"  t+{elapsed_s:.1f}s outputLimit={observed}W")
                if moved(observed, baseline, opts.match_tolerance):
                    movement_observed = True
                if (
                    not setpoint_done
                    and observed is not None
                    and abs(observed - target) <= opts.match_tolerance
                ):
                    setpoint_done = True
                    setpoint_at = observed_at
                    mode_ok = _mode_matches(
                        submission.expected_properties,
                        power_state,
                        opts.match_tolerance,
                    )
                    if opts.verify_output:
                        # Preserve the configured post-setpoint verification window,
                        # while the reported primary duration remains from submit.
                        physical_deadline = observed_at + opts.output_timeout

                if opts.verify_output and physical is None:
                    last_physical_verdict = classify_physical(
                        power_state, target, opts.output_tolerance
                    )
                    progress(
                        "  output check: "
                        f"outputHomePower={power_state['outputHomePower']}W -> "
                        f"{last_physical_verdict}"
                    )
                    if last_physical_verdict != "output_reacted":
                        physical_target_was_absent = True
                    if (
                        last_physical_verdict == "output_reacted"
                        and physical_target_was_absent
                    ):
                        # Physical output can lead the HTTP outputLimit field.
                        # Preserve its first attributable observation on the
                        # same pre-submit timeline instead of delaying it until
                        # the setpoint endpoint catches up.
                        physical = "output_reacted"
                        physical_at = observed_at
                    elif (
                        setpoint_at is not None
                        and last_physical_verdict
                        == "no_output_possible_soc_at_minimum"
                    ):
                        physical = last_physical_verdict

            current_time = now()

        # A match observed exactly at the deadline is evidence; timeout only after
        # giving that observation a chance to settle the setpoint.
        if not setpoint_done and current_time >= setpoint_deadline:
            setpoint_done = True
            if opts.verify_output and physical is None:
                physical = "setpoint_not_matched"
        if (
            opts.verify_output
            and setpoint_at is not None
            and physical is None
            and physical_deadline is not None
            and current_time >= physical_deadline
        ):
            if (
                last_physical_verdict == "output_reacted"
                and not physical_target_was_absent
            ):
                physical = "baseline_already_at_target"
            else:
                physical = last_physical_verdict or "unknown"

        physical_done = not opts.verify_output or physical is not None
        if delivery_status != "pending" and setpoint_done and physical_done:
            break

        deadlines = []
        if delivery_status == "pending":
            deadlines.append(delivery_deadline)
        if not setpoint_done:
            deadlines.append(setpoint_deadline)
        if opts.verify_output and setpoint_at is not None and physical is None:
            deadlines.append(physical_deadline)
        next_sleep = opts.poll_interval
        if deadlines:
            next_sleep = min(next_sleep, max(0.0, min(deadlines) - current_time))
        # A zero interval at a deadline is settled at the top of the next turn.
        if next_sleep <= 0:
            continue
        sleep(next_sleep)

    setpoint_ms = (
        None if setpoint_at is None else max(0.0, (setpoint_at - origin) * 1000.0)
    )
    physical_from_submit_ms = (
        None if physical_at is None else max(0.0, (physical_at - origin) * 1000.0)
    )
    physical_after_setpoint_ms = None
    if physical_at is not None and setpoint_at is not None and physical_at >= setpoint_at:
        physical_after_setpoint_ms = (physical_at - setpoint_at) * 1000.0
    return CommandEvidence(
        broker_delivery_status=delivery_status,
        broker_delivery_from_submit_ms=delivery_ms,
        setpoint_match_from_submit_ms=setpoint_ms,
        last_observed_output_limit=observed,
        movement_observed=movement_observed,
        polls=polls,
        mode_ok=mode_ok,
        physical=physical,
        physical_reaction_from_submit_ms=physical_from_submit_ms,
        physical_reaction_after_setpoint_ms=physical_after_setpoint_ms,
        last_power_state=last_power_state,
    )


def run_probe(
    dev,
    reader,
    values,
    opts,
    *,
    activity=None,
    sleep=time.sleep,
    now=time.monotonic,
    progress=None,
):
    """Run the write/observe loop and return the collected samples.

    Raises ``RuntimeError`` on a foreign-writer contention detection so the caller
    can stop and restore rather than report polluted numbers. ``progress`` receives
    human-readable status lines as the run proceeds (default: no-op).
    """

    if progress is None:
        progress = _no_progress
    if activity is None:
        activity = WriteActivity()

    samples = []
    last_matched = None
    for index in range(opts.samples):
        baseline_state = read_power_state(reader)
        baseline = (
            baseline_state.get("outputLimit")
            if isinstance(baseline_state, dict)
            else None
        )
        if baseline is None:
            raise RuntimeError("HTTP API read failed; cannot measure")
        if (
            opts.contention_check
            and last_matched is not None
            and moved(baseline, last_matched, opts.match_tolerance)
        ):
            raise RuntimeError(
                f"foreign writer detected: expected ~{last_matched} W, read {baseline} W. "
                "Is the EMS still running? Stop it and retry."
            )

        target = pick_target(baseline, values)
        if not moved(target, baseline, opts.match_tolerance):
            target = next((v for v in values if moved(v, baseline, opts.match_tolerance)), target)

        submission = publish_target(dev, target, activity=activity, now=now)
        progress(
            f"sample {index + 1}/{opts.samples}: submitted {target}W "
            f"(baseline {baseline}W) local_accept="
            f"{'yes' if submission.locally_accepted else 'NO'} "
            f"{submission.local_submit_duration_ms:.0f}ms"
        )
        if not submission.locally_accepted:
            progress("  -> local publish rejected; no state change assumed")
            samples.append(
                Sample(
                    index=index,
                    target=target,
                    baseline=baseline,
                    command_started_monotonic=submission.command_started_monotonic,
                    locally_accepted=False,
                    local_submit_duration_ms=submission.local_submit_duration_ms,
                    broker_delivery_status="untracked",
                    broker_delivery_from_submit_ms=None,
                    setpoint_match_from_submit_ms=None,
                    last_observed_output_limit=None,
                    movement_observed=False,
                    matched_target=False,
                    timed_out=False,
                    polls=0,
                )
            )
            sleep(opts.settle)
            continue

        evidence = _observe_command_evidence(
            dev,
            reader,
            submission,
            baseline_state,
            target,
            opts,
            sleep=sleep,
            now=now,
            progress=progress,
        )
        if (
            evidence.setpoint_match_from_submit_ms is not None
            and evidence.movement_observed
        ):
            # This is trustworthy pre-restore evidence for the latest accepted
            # command only. A subsequent accepted publish resets the flag.
            activity.record_latest_state_observed()
        progress(
            f"  broker delivery: {evidence.broker_delivery_status}"
            + (
                f" {evidence.broker_delivery_from_submit_ms:.0f}ms from submit"
                if evidence.broker_delivery_from_submit_ms is not None
                else ""
            )
        )

        if evidence.setpoint_match_from_submit_ms is None:
            progress(
                f"  -> TIMED OUT after {opts.timeout:.0f}s "
                f"(outputLimit {evidence.last_observed_output_limit}W, "
                f"target {target}W"
                f"{', moved but never matched' if evidence.movement_observed else ''})"
            )
        else:
            progress(
                "  -> matched target "
                f"{evidence.setpoint_match_from_submit_ms:.0f}ms from submit "
                f"(outputLimit={evidence.last_observed_output_limit}W"
                + (f", mode_ok={evidence.mode_ok}" if evidence.mode_ok is not None else "")
                + (f", physical={evidence.physical}" if evidence.physical else "")
                + ")"
            )
            last_matched = evidence.last_observed_output_limit
        samples.append(
            Sample(
                index=index,
                target=target,
                baseline=baseline,
                command_started_monotonic=submission.command_started_monotonic,
                locally_accepted=True,
                local_submit_duration_ms=submission.local_submit_duration_ms,
                broker_delivery_status=evidence.broker_delivery_status,
                broker_delivery_from_submit_ms=(
                    evidence.broker_delivery_from_submit_ms
                ),
                setpoint_match_from_submit_ms=(
                    evidence.setpoint_match_from_submit_ms
                ),
                last_observed_output_limit=(
                    evidence.last_observed_output_limit
                ),
                movement_observed=evidence.movement_observed,
                matched_target=evidence.setpoint_match_from_submit_ms is not None,
                timed_out=evidence.setpoint_match_from_submit_ms is None,
                polls=evidence.polls,
                mode_ok=evidence.mode_ok,
                physical=evidence.physical,
                physical_reaction_from_submit_ms=(
                    evidence.physical_reaction_from_submit_ms
                ),
                physical_reaction_after_setpoint_ms=(
                    evidence.physical_reaction_after_setpoint_ms
                ),
            )
        )
        sleep(opts.settle)
    return samples


def _mode_matches(expected, power_state, watt_tolerance):
    """Whether telemetry satisfies the full expected property set, or ``None``.

    Returns ``None`` when a required property is not exposed by the HTTP report
    (verification incomplete), otherwise whether every expected property matches
    by type (watt-like within tolerance, mode/enum exactly).
    """

    if not expected or power_state is None:
        return None
    for key, target in expected.items():
        if key not in power_state:
            return None
        if not property_matches(
            key, power_state[key], target, watt_tolerance=watt_tolerance
        ):
            return False
    return True


def _no_progress(_message):
    return None


def run_mode_recovery_test(
    dev,
    reader,
    target,
    opts,
    *,
    activity=None,
    sleep=time.sleep,
    now=time.monotonic,
    progress=None,
):
    """From a non-output mode, prove the atomic command lands mode + setpoint.

    Never forces a device out of output mode: when the device is already in
    smartMode 1 / acMode 2 the test is reported as not applicable.
    """

    if progress is None:
        progress = _no_progress
    state = read_power_state(reader)
    if state is None:
        return {"result": "error", "detail": "HTTP API read failed"}
    if state.get("smartMode") == 1 and state.get("acMode") == 2:
        return {
            "result": "not_applicable",
            "detail": "device already in smart AC output mode (smartMode=1, acMode=2)",
        }
    progress(
        f"mode test: starting from smartMode={state.get('smartMode')} "
        f"acMode={state.get('acMode')} outputLimit={state.get('outputLimit')}W"
    )
    submission = publish_target(dev, target, activity=activity, now=now)
    if not submission.locally_accepted:
        return {
            "result": "publish_failed",
            "detail": (
                "local publish rejected after "
                f"{submission.local_submit_duration_ms:.0f}ms"
            ),
            "local_submit_duration_ms": submission.local_submit_duration_ms,
            "broker_delivery_status": "not_submitted",
            "broker_delivery_from_submit_ms": None,
            "setpoint_match_from_submit_ms": None,
            "physical_reaction_from_submit_ms": None,
            "physical_reaction_after_setpoint_ms": None,
        }
    evidence = _observe_command_evidence(
        dev,
        reader,
        submission,
        state,
        target,
        opts,
        sleep=sleep,
        now=now,
        progress=progress,
    )
    current = evidence.last_power_state
    verdict = (
        _mode_test_verdict(
            submission.expected_properties,
            current,
            opts.match_tolerance,
        )
        if evidence.setpoint_match_from_submit_ms is not None
        and isinstance(current, dict)
        else {
            "result": "timed_out",
            "detail": f"state after {opts.timeout:.0f}s: {current}",
        }
    )
    if verdict is None:
        verdict = {
            "result": "timed_out",
            "detail": f"state after {opts.timeout:.0f}s: {current}",
        }
    if activity is not None and isinstance(current, dict) and any(
        key in state
        and key in current
        and not property_matches(
            key,
            current[key],
            state[key],
            watt_tolerance=opts.match_tolerance,
        )
        for key in submission.expected_properties
    ):
        activity.record_latest_state_observed()
    return {
        **verdict,
        "local_submit_duration_ms": submission.local_submit_duration_ms,
        "broker_delivery_status": evidence.broker_delivery_status,
        "broker_delivery_from_submit_ms": (
            evidence.broker_delivery_from_submit_ms
        ),
        "setpoint_match_from_submit_ms": evidence.setpoint_match_from_submit_ms,
        "physical_reaction_from_submit_ms": (
            evidence.physical_reaction_from_submit_ms
        ),
        "physical_reaction_after_setpoint_ms": (
            evidence.physical_reaction_after_setpoint_ms
        ),
    }


def _mode_test_verdict(expected, current, watt_tolerance):
    """A terminal mode-test verdict once telemetry settles, or ``None`` to wait.

    Verifies the *complete* expected property set by type: watt-like within
    tolerance, mode/enum exactly. A required property not exposed by the HTTP
    report yields ``setpoint_verified_mode_incomplete`` rather than a false
    full-mode success; a settled mismatch is ``mode_mismatch``.
    """

    setpoint_ok = property_matches(
        "outputLimit", current.get("outputLimit"), expected["outputLimit"],
        watt_tolerance=watt_tolerance,
    )
    if not setpoint_ok:
        return None  # keep waiting for the setpoint to land

    incomplete = [k for k in expected if k not in current]
    detail = (
        f"smartMode={current.get('smartMode')} acMode={current.get('acMode')} "
        f"outputLimit={current.get('outputLimit')}W inputLimit={current.get('inputLimit')}W"
    )
    if incomplete:
        return {
            "result": "setpoint_verified_mode_incomplete",
            "detail": f"{detail}; not reported by HTTP: {sorted(incomplete)}",
        }
    mismatches = [
        k for k, v in expected.items()
        if not property_matches(k, current[k], v, watt_tolerance=watt_tolerance)
    ]
    if mismatches:
        return {"result": "mode_mismatch", "detail": f"{detail}; mismatched: {sorted(mismatches)}"}
    return {"result": "mode_and_setpoint_verified", "detail": detail}


def preflight_restorable(dev, initial, contracts):
    """Return reasons a complete operation-derived restore cannot be guaranteed."""

    required = required_restore_properties(contracts)
    initial = initial if isinstance(initial, dict) else {}
    issues = {
        key: "initial_value_missing"
        for key in required
        if key not in initial
    }
    captured = {key: initial[key] for key in required if key in initial}
    if captured:
        try:
            issues.update(dev.check_property_writes(captured))
        except Exception as exc:
            reason = f"property_write_preflight_{type(exc).__name__}"
            issues.update({key: reason for key in captured})
    return {key: issues[key] for key in sorted(issues)}


def _restore_comparison(current, desired, property_names, watt_tolerance):
    """Classify every restore field as matched, unknown or mismatched."""

    matched = []
    unknown = []
    mismatched = {}
    for key in property_names:
        if current is None or current.get(key) is None:
            unknown.append(key)
            continue
        observed = current[key]
        if property_matches(
            key, observed, desired[key], watt_tolerance=watt_tolerance
        ):
            matched.append(key)
        else:
            mismatched[key] = {
                "observed": observed,
                "expected": desired[key],
            }
    return matched, unknown, mismatched


def _read_restore_state(reader):
    """Read restoration evidence without allowing HTTP faults to skip restore."""

    try:
        return read_power_state(reader), None
    except Exception as exc:
        return None, type(exc).__name__


def _restore_report(
    *,
    status,
    attempted,
    submitted,
    captured_properties,
    changed_properties,
    submitted_properties,
    matched_properties,
    unknown_properties,
    mismatched_properties,
    detail,
    state=None,
    accepted_command_ids=(),
    state_transition_observed=False,
):
    verified = status == "restore_verified"
    return {
        "restore_status": status,
        "restore_attempted": attempted,
        "restore_submitted": submitted,
        "restored": verified,
        "restore_verified": verified,
        # No safe partial operation exists for the known atomic ZenSDK profile.
        "restore_partial": False,
        "verified": verified,
        "state": state,
        "captured_properties": sorted(captured_properties),
        "changed_properties": sorted(changed_properties),
        "submitted_properties": sorted(submitted_properties),
        "matched_properties": sorted(matched_properties),
        "unknown_properties": sorted(unknown_properties),
        "mismatched_properties": {
            key: mismatched_properties[key] for key in sorted(mismatched_properties)
        },
        "accepted_command_ids": list(accepted_command_ids),
        "state_transition_observed": bool(state_transition_observed),
        "detail": detail,
    }


def restore_initial_state(
    dev,
    reader,
    initial,
    contracts,
    opts,
    *,
    changed_properties=None,
    accepted_command_ids=(),
    prior_state_transition_observed=False,
    sleep=time.sleep,
    now=time.monotonic,
    progress=None,
):
    """Submit and verify one complete restore; never emit a fallback command.

    ``restore_verified`` requires all potentially modified properties to have
    initial values, all of them to be present in the one locally accepted restore
    submission, and HTTP to have observed the latest accepted command transition
    before it observes the complete initial state. That transition may have been
    observed by the probe before restoration or by this restore loop. A later
    accepted command resets prior evidence. The barrier prevents an unchanged
    pre-command read from falsely verifying restore while a test command is still
    pending. Anything less is ``restore_failed`` and the caller exits non-zero.
    """

    if progress is None:
        progress = _no_progress
    planned = required_restore_properties(contracts)
    changed = tuple(sorted(set(changed_properties or planned)))
    initial = initial if isinstance(initial, dict) else {}
    captured = tuple(key for key in changed if key in initial)
    missing = tuple(key for key in changed if key not in initial)
    desired = {key: initial[key] for key in changed if key in initial}
    if missing:
        return _restore_report(
            status="restore_failed",
            attempted=True,
            submitted=False,
            captured_properties=captured,
            changed_properties=changed,
            submitted_properties=(),
            matched_properties=(),
            unknown_properties=missing,
            mismatched_properties={},
            accepted_command_ids=accepted_command_ids,
            detail=f"initial values missing for {list(missing)}; no restore submitted",
            state=desired,
        )

    before_restore, last_read_error = _read_restore_state(reader)
    _, _, before_mismatched = _restore_comparison(
        before_restore, desired, changed, opts.match_tolerance
    )
    state_transition_observed = bool(
        prior_state_transition_observed or before_mismatched
    )

    try:
        result = dev.write_properties(desired, reason="probe_restore")
        accepted = bool(result)
        rejection_reason = None if accepted else getattr(result, "reason", "rejected")
    except Exception as exc:
        accepted = False
        rejection_reason = type(exc).__name__

    if not accepted:
        current, last_read_error = _read_restore_state(reader)
        matched, unknown, mismatched = _restore_comparison(
            current, desired, changed, opts.match_tolerance
        )
        return _restore_report(
            status="restore_failed",
            attempted=True,
            submitted=False,
            captured_properties=captured,
            changed_properties=changed,
            submitted_properties=(),
            matched_properties=matched,
            unknown_properties=unknown,
            mismatched_properties=mismatched,
            accepted_command_ids=accepted_command_ids,
            detail=(
                f"full restore was not locally accepted ({rejection_reason}); "
                "no fallback command sent"
                + (
                    f"; HTTP read failed ({last_read_error})"
                    if last_read_error
                    else ""
                )
            ),
            state=desired,
        )

    deadline = now() + opts.timeout
    current = None
    matched, unknown, mismatched = [], list(changed), {}
    while True:
        current, read_error = _read_restore_state(reader)
        if read_error is not None:
            last_read_error = read_error
        matched, unknown, mismatched = _restore_comparison(
            current, desired, changed, opts.match_tolerance
        )
        if mismatched:
            state_transition_observed = True
        if state_transition_observed and not unknown and not mismatched:
            progress(f"full restore verified for properties {list(changed)}")
            return _restore_report(
                status="restore_verified",
                attempted=True,
                submitted=True,
                captured_properties=captured,
                changed_properties=changed,
                submitted_properties=changed,
                matched_properties=matched,
                unknown_properties=unknown,
                mismatched_properties=mismatched,
                accepted_command_ids=accepted_command_ids,
                state_transition_observed=True,
                detail=(
                    "full restore submitted and verified after HTTP observed "
                    "the post-command state transition"
                ),
                state=desired,
            )
        current_time = now()
        if current_time >= deadline:
            break
        sleep(min(opts.poll_interval, max(0.0, deadline - current_time)))

    return _restore_report(
        status="restore_failed",
        attempted=True,
        submitted=True,
        captured_properties=captured,
        changed_properties=changed,
        submitted_properties=changed,
        matched_properties=matched,
        unknown_properties=unknown,
        mismatched_properties=mismatched,
        accepted_command_ids=accepted_command_ids,
        state_transition_observed=state_transition_observed,
        detail=(
            "full restore submitted but HTTP verification timed out"
            if state_transition_observed
            else "full restore submitted but no post-command state transition "
            "was observed before the verification timeout"
        )
        + (
            f"; last HTTP read failed ({last_read_error})"
            if last_read_error
            else ""
        ),
        state=desired,
    )


def finalize_restoration(
    dev,
    reader,
    initial,
    contracts,
    opts,
    activity,
    *,
    sleep=time.sleep,
    now=time.monotonic,
    progress=None,
):
    """Restore iff at least one real command was accepted by the local client."""

    required = required_restore_properties(contracts)
    captured = sorted(set(initial or {}) & set(required))
    if not activity.locally_accepted:
        return _restore_report(
            status="not_attempted",
            attempted=False,
            submitted=False,
            captured_properties=captured,
            changed_properties=(),
            submitted_properties=(),
            matched_properties=(),
            unknown_properties=(),
            mismatched_properties={},
            accepted_command_ids=activity.accepted_command_ids,
            detail="no state-changing command was locally accepted",
            state=None,
        )
    changed = activity.accepted_modified_properties or list(required)
    return restore_initial_state(
        dev,
        reader,
        initial,
        contracts,
        opts,
        changed_properties=changed,
        accepted_command_ids=activity.accepted_command_ids,
        prior_state_transition_observed=(
            activity.latest_accepted_state_observed
        ),
        sleep=sleep,
        now=now,
        progress=progress,
    )


def exit_code_after_restoration(exit_code, activity, report):
    """A locally accepted write can exit zero only after a verified full restore."""

    if activity.locally_accepted and not report.get("restore_verified"):
        return exit_code or 1
    return exit_code


def percentile(sorted_values, q):
    """Linear-interpolation percentile of a pre-sorted list (q in [0, 1])."""

    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def summarize(samples, opts):
    """Aggregate matched samples into headline latency statistics.

    Only a sample that actually matched the target counts as landed; a
    movement-only or timed-out sample is reported separately and never inflates
    the setpoint-latency percentiles.
    """

    matched = [s for s in samples if s.matched_target]
    latencies = sorted(
        s.setpoint_match_from_submit_ms
        for s in matched
        if s.setpoint_match_from_submit_ms is not None
    )
    submits = sorted(s.local_submit_duration_ms for s in samples)
    deliveries = sorted(
        s.broker_delivery_from_submit_ms
        for s in samples
        if s.broker_delivery_from_submit_ms is not None
    )
    physical = sorted(
        s.physical_reaction_from_submit_ms
        for s in samples
        if s.physical_reaction_from_submit_ms is not None
    )
    physical_after_setpoint = sorted(
        s.physical_reaction_after_setpoint_ms
        for s in samples
        if s.physical_reaction_after_setpoint_ms is not None
    )
    ok = len(latencies)
    # Mutually exclusive buckets. Movement toward the target that never matched is
    # diagnostics only and is counted separately from a sample that never moved.
    unmatched = [s for s in samples if s.locally_accepted and not s.matched_target]
    movement_only = sum(1 for s in unmatched if s.movement_observed)
    timed_out = sum(1 for s in unmatched if not s.movement_observed)
    return {
        "samples": len(samples),
        "matched": len(matched),
        "landed": len(matched),
        "movement_only": movement_only,
        "timed_out": timed_out,
        "local_rejected": sum(1 for s in samples if not s.locally_accepted),
        "broker_delivered": sum(1 for s in samples if s.broker_delivery_status == "delivered"),
        "mode_ok": sum(1 for s in samples if s.mode_ok),
        "output_reacted": sum(1 for s in samples if s.physical == "output_reacted"),
        "poll_resolution_ms": opts.poll_interval * 1000.0,
        "setpoint_match_from_submit_min_ms": latencies[0] if ok else None,
        "setpoint_match_from_submit_p50_ms": percentile(latencies, 0.5),
        "setpoint_match_from_submit_p95_ms": percentile(latencies, 0.95),
        "setpoint_match_from_submit_max_ms": latencies[-1] if ok else None,
        "local_submit_duration_p50_ms": percentile(submits, 0.5),
        "broker_delivery_from_submit_p50_ms": percentile(deliveries, 0.5),
        "physical_reaction_from_submit_p50_ms": percentile(physical, 0.5),
        "physical_reaction_after_setpoint_p50_ms": percentile(
            physical_after_setpoint, 0.5
        ),
    }


def _fmt_ms(value):
    return "-" if value is None else f"{value:.0f}"


def format_text_report(dev, reader, values, samples, stats):
    """Human-readable per-sample table and summary for the terminal."""

    lines = []
    lines.append(
        f"MQTT write -> HTTP-visible latency  device={dev.name} "
        f"transport={dev.source} gate={dev.control_gate} api={reader.ip} values={values}"
    )
    lines.append("")
    lines.append(
        f"{'#':>2}  {'target':>7}  {'baseline':>8}  {'setpt_from':>11}  "
        f"{'local_dur':>9}  {'puback_from':>11}  {'physical_from':>13}  "
        f"{'phys_after':>10}  {'polls':>5}  status"
    )
    for s in samples:
        if not s.locally_accepted:
            status = "local_rejected"
        elif s.matched_target:
            status = "matched"
        elif s.timed_out and s.movement_observed:
            status = f"moved_not_matched(observed={s.last_observed_output_limit})"
        elif s.timed_out:
            status = "timed_out"
        else:
            status = "no_match"
        if s.mode_ok is False:
            status += ",mode_mismatch"
        elif s.mode_ok is None and s.matched_target:
            status += ",mode_incomplete"
        if s.physical:
            status += f",{s.physical}"
        broker = s.broker_delivery_status
        if s.broker_delivery_from_submit_ms is not None:
            broker = f"{s.broker_delivery_from_submit_ms:.0f}ms"
        lines.append(
            f"{s.index:>2}  {s.target:>7}  {s.baseline:>8}  "
            f"{_fmt_ms(s.setpoint_match_from_submit_ms):>11}  "
            f"{s.local_submit_duration_ms:>9.1f}  {broker:>11}  "
            f"{_fmt_ms(s.physical_reaction_from_submit_ms):>13}  "
            f"{_fmt_ms(s.physical_reaction_after_setpoint_ms):>10}  "
            f"{s.polls:>5}  {status}"
        )
    lines.append("")
    lines.append(
        f"matched {stats['matched']}/{stats['samples']}  "
        f"movement_only={stats['movement_only']}  timed_out={stats['timed_out']}  "
        f"local_rejected={stats['local_rejected']}  "
        f"broker_delivered={stats['broker_delivered']}  "
        f"poll_resolution={stats['poll_resolution_ms']:.0f} ms"
    )
    lines.append(
        "setpoint match from submit ms  "
        f"min={_fmt_ms(stats['setpoint_match_from_submit_min_ms'])}  "
        f"p50={_fmt_ms(stats['setpoint_match_from_submit_p50_ms'])}  "
        f"p95={_fmt_ms(stats['setpoint_match_from_submit_p95_ms'])}  "
        f"max={_fmt_ms(stats['setpoint_match_from_submit_max_ms'])}  "
        "(matched samples only)"
    )
    lines.append(
        "local submit duration ms "
        f"p50={_fmt_ms(stats['local_submit_duration_p50_ms'])}   "
        "broker delivery from submit ms "
        f"p50={_fmt_ms(stats['broker_delivery_from_submit_p50_ms'])}"
    )
    lines.append(
        "physical reaction from submit ms "
        f"p50={_fmt_ms(stats['physical_reaction_from_submit_p50_ms'])}   "
        "physical reaction after setpoint ms "
        f"p50={_fmt_ms(stats['physical_reaction_after_setpoint_p50_ms'])}"
    )
    return "\n".join(lines)


def format_markdown_report(dev, reader, values, stats):
    """Documentation-ready Markdown block to link the measured numbers from docs."""

    return "\n".join(
        [
            f"**MQTT `outputLimit` write latency** — `{dev.name}` "
            f"({dev.source}, gate `{dev.control_gate}`)",
            "",
            "Command channel: MQTT. Observation channel: local HTTP API "
            f"(`{reader.ip}`), polled every {stats['poll_resolution_ms']:.0f} ms. "
            f"Toggled between {values} W. Only samples that matched the target count "
            "as landed.",
            "",
            "| Metric | Value |",
            "| --- | --- |",
            f"| Samples matched | {stats['matched']} / {stats['samples']} |",
            f"| Movement-only (not matched) | {stats['movement_only']} |",
            f"| Broker delivered | {stats['broker_delivered']} / {stats['samples']} |",
            "| Setpoint match from submit min | "
            f"{_fmt_ms(stats['setpoint_match_from_submit_min_ms'])} ms |",
            "| Setpoint match from submit p50 | "
            f"{_fmt_ms(stats['setpoint_match_from_submit_p50_ms'])} ms |",
            "| Setpoint match from submit p95 | "
            f"{_fmt_ms(stats['setpoint_match_from_submit_p95_ms'])} ms |",
            "| Setpoint match from submit max | "
            f"{_fmt_ms(stats['setpoint_match_from_submit_max_ms'])} ms |",
            "| Local submit duration p50 | "
            f"{_fmt_ms(stats['local_submit_duration_p50_ms'])} ms |",
            "| Broker delivery from submit p50 | "
            f"{_fmt_ms(stats['broker_delivery_from_submit_p50_ms'])} ms |",
            "| Physical reaction from submit p50 | "
            f"{_fmt_ms(stats['physical_reaction_from_submit_p50_ms'])} ms |",
            "| Physical reaction after setpoint p50 | "
            f"{_fmt_ms(stats['physical_reaction_after_setpoint_p50_ms'])} ms |",
            f"| Poll resolution | {stats['poll_resolution_ms']:.0f} ms |",
            "",
            "_All primary evidence durations share the monotonic origin immediately "
            "before local MQTT submission. Local submit duration is time inside the "
            "local publish call; broker delivery is observed PUBACK; setpoint match "
            "is target visibility over local HTTP; physical reaction is output "
            "visibility over local HTTP. Physical-after-setpoint is incremental only. "
            "Measured with the EMS stopped (single writer)._",
        ]
    )


def print_preview(dev, reader, values, gate, initial, contracts):
    """Print a secret-free plan and return operation-derived preflight issues."""

    described = dev.describe()
    print("operation plan (dry preview):")
    print(f"  device: {dev.name}  source={dev.source}  broker_ref={dev.broker_ref}")
    print(
        f"  hardware_profile={dev.hardware_profile}  "
        f"power_write_profile={described.get('power_write_profile')}"
    )
    print(
        "  effective write topic: "
        f"{_display_mqtt_topic(dev, described.get('effective_write_topic'))} "
        f"(source={described.get('effective_write_topic_source')})"
    )
    if described.get("write_topic_obsolete"):
        print("  note: an obsolete mqtt.write_topic override is present and IGNORED "
              "(the canonical topic is used).")
    print(
        f"  write_gate={gate.gate_name} enabled={gate.gate_enabled} "
        f"blocked_by={list(gate.blocked_by)}"
    )
    print(f"  current state: {initial}")
    by_target = {contract.target_w: contract for contract in contracts}
    for value in values:
        contract = by_target[value]
        print(
            f"  {contract.operation} {value}W -> "
            f"topic={_display_mqtt_topic(dev, contract.topic)} qos={contract.qos} "
            f"retain={contract.retain} properties={contract.modified_properties}"
        )
    required = required_restore_properties(contracts)
    unrestorable = preflight_restorable(dev, initial, contracts)
    restorable = {
        key: initial[key]
        for key in required
        if key in initial and key not in unrestorable
    }
    print(f"  operation-derived restore properties: {list(required)}")
    print(f"  restorable initial properties: {restorable}")
    if unrestorable:
        print(
            f"  NON-RESTORABLE initial properties: {unrestorable} "
            "(live probe would stop at preflight)"
        )
    print("  single-writer advisory: stop the live EMS before writing "
          "(two writers to outputLimit is forbidden).")
    return unrestorable


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Probe Zendure MQTT power control against the local HTTP API."
    )
    parser.add_argument("--config", default=None, help="Path to config.json (default: resolved like the EMS).")
    parser.add_argument("--device", "--device-name", dest="device", default=None, help="MQTT control device name (default: the only one).")
    parser.add_argument("--serial", default=None, help="Select the MQTT control device by physical serial.")
    parser.add_argument("--device-id", dest="device_id", default=None, help="Select the MQTT control device by MQTT device id.")
    parser.add_argument("--broker-ref", dest="broker_ref", default=None, help="Select the MQTT control device by broker reference.")
    parser.add_argument("--api-device", default=None, help="HTTP device entry name for the read-back (default: matched by serial).")
    parser.add_argument("--api-ip", default=None, help="Local HTTP API IP of the inverter to test. The probe reads its serial and selects the matching MQTT control device (unambiguous even with many devices).")
    parser.add_argument("--values", type=int, nargs=2, metavar=("A", "B"), default=[200, 500], help="Two outputLimit setpoints to toggle between (W).")
    parser.add_argument("--samples", type=int, default=10, help="Number of write/observe samples.")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="HTTP API poll interval in seconds (also the measurement resolution).")
    parser.add_argument("--timeout", type=float, default=20.0, help="Per-sample timeout in seconds waiting for the value to land.")
    parser.add_argument("--settle", type=float, default=2.0, help="Pause between samples in seconds.")
    parser.add_argument("--match-tolerance", type=int, default=5, help="Watts of jitter tolerated when detecting the change.")
    parser.add_argument("--connect-timeout", type=float, default=15.0, help="Seconds to wait for the MQTT broker to connect.")
    parser.add_argument("--verify-output", action="store_true", help="After a landed setpoint, verify the physical output reaction.")
    parser.add_argument("--output-timeout", type=float, default=30.0, help="Seconds to wait for the physical output to react.")
    parser.add_argument("--output-tolerance", type=int, default=50, help="Watts of tolerance for the physical output check.")
    parser.add_argument("--mode-test", action="store_true", help="Run the mode recovery test (device must currently be in a non-output mode).")
    parser.add_argument("--confirm-mode-changes", action="store_true", help="Required (with --confirm-writes) to run mode-changing tests.")
    parser.add_argument("--no-contention-check", dest="contention_check", action="store_false", help="Do not abort when a foreign writer changes the value.")
    parser.add_argument("--confirm-writes", action="store_true", help="Required to actually write to hardware.")
    parser.add_argument("--confirm-unbound-api-readback", dest="confirm_unbound_api_readback", action="store_true", help="Accept, for this run only, a serial-less Cloud device's unverified HTTP readback serial (requires exact --device-name/--device-id/--broker-ref). Never persisted.")
    parser.add_argument("--dry-preview", action="store_true", help="Resolve devices and print the plan without writing.")
    parser.add_argument("--markdown", action="store_true", help="Emit a documentation-ready Markdown table.")
    return parser.parse_args(argv)


def main(argv=None):
    opts = parse_args(argv)

    if opts.values[0] == opts.values[1]:
        print("error: --values must be two different setpoints", file=sys.stderr)
        return 2

    cfg, config, config_path = bootstrap_config(opts.config)

    from ems.clients import create_session
    from ems.device_identity import broker_sources_from_config
    from ems.zendure_mqtt.config_entries import (
        find_duplicate_device_names,
        find_duplicate_zendure_device_identities,
    )
    from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime

    # Fail closed on a config the live EMS would refuse: duplicate device
    # identities/names make single-writer selection ambiguous.
    devices_config = config.get("devices") if isinstance(config, dict) else None
    if find_duplicate_zendure_device_identities(
        devices_config, broker_sources=broker_sources_from_config(config)
    ) or find_duplicate_device_names(devices_config):
        print(
            "error: duplicate device identities or names in config; resolve them "
            "before probing (single-writer selection must be unambiguous)",
            file=sys.stderr,
        )
        return 1

    runtime = build_zendure_mqtt_control_runtime(config)
    session = create_session()

    if opts.api_ip:
        dev, reader, matched_serial, err = resolve_by_api_ip(
            runtime, session, opts.api_ip, opts.device,
            serial=opts.serial, device_id=opts.device_id, broker_ref=opts.broker_ref,
        )
    else:
        dev, err = resolve_mqtt_device(
            runtime, opts.device, serial=opts.serial,
            device_id=opts.device_id, broker_ref=opts.broker_ref,
        )
        reader = None
        if not err:
            reader, err = resolve_http_reader(
                cfg, config, session, opts.api_device, None, dev
            )
        matched_serial = getattr(dev, "sn", None) if dev is not None else None
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    # Cross-transport binding is decided before any write. The HTTP readback serial
    # and a Cloud route id are different identity domains; a serial-less Cloud
    # device is never silently bound to the HTTP serial.
    http_readback_serial = getattr(reader, "sn", None)
    binding = evaluate_http_binding(dev, http_readback_serial)
    exact_selectors = bool(opts.device and opts.device_id and opts.broker_ref)

    max_power = getattr(dev, "max_power", None) or cfg.MAX_DEVICE_POWER
    for value in opts.values:
        if value < 0:
            print(f"error: negative setpoint {value} W not supported by this probe", file=sys.stderr)
            return 1
        if max_power and value > max_power:
            print(f"error: setpoint {value} W exceeds device max_power {max_power} W", file=sys.stderr)
            return 1

    gate = cfg.resolve_write_gate(dev.control_gate)
    planned_targets = [opts.values[0]] if opts.mode_test else list(opts.values)
    try:
        contracts = build_operation_contracts(dev, planned_targets)
    except Exception as exc:
        print(
            "preflight_failed: production operation contract is not fully "
            f"inspectable/restorable ({type(exc).__name__}: {exc}); no write performed.",
            file=sys.stderr,
        )
        return 1
    required_properties = required_restore_properties(contracts)
    print(f"config: {config_path}")
    print(
        f"mqtt device: {dev.name}  "
        f"serial={mask_route_identifier(matched_serial)}  transport={dev.source}  "
        f"gate={gate.gate_name} enabled={gate.gate_enabled}"
    )
    print(f"http read: {reader.name} @ {reader.ip}")
    print(f"identity binding: {binding.summary()}")
    print(f"values: {opts.values} W   samples: {opts.samples}   poll: {opts.poll_interval}s   settle: {opts.settle}s")

    if opts.dry_preview:
        initial = initial_restore_state(reader.ip, session, required_properties)
        print()
        preview_issues = print_preview(
            dev, reader, planned_targets, gate, initial, contracts
        )
        write_block = binding.write_block_reason(
            acknowledged=opts.confirm_unbound_api_readback,
            exact_selectors=exact_selectors,
        )
        if binding.status == BINDING_UNBOUND:
            print(
                "  cross-transport binding: physical serial not stored; HTTP "
                f"readback reports serial {mask_route_identifier(binding.http_serial)}; "
                "binding is UNVERIFIED. A write test remains blocked until "
                "--confirm-unbound-api-readback (with exact "
                "--device-name/--device-id/--broker-ref) or an Admin serial binding."
            )
        elif write_block is not None:
            print(f"  cross-transport binding: {write_block}")
        print("\ndry-preview: no writes performed.")
        if preview_issues:
            print(
                "preflight_failed: dry preview found an incomplete or "
                "unrestorable operation-derived initial state.",
                file=sys.stderr,
            )
            return 1
        if binding.status == BINDING_CONFLICT:
            print(
                "preflight_failed: cross-transport identity conflict "
                "(configured physical serial does not match the HTTP readback).",
                file=sys.stderr,
            )
            return 1
        return 0

    write_block = binding.write_block_reason(
        acknowledged=opts.confirm_unbound_api_readback,
        exact_selectors=exact_selectors,
    )
    if write_block is not None:
        print(f"\nrefusing to write: {write_block}", file=sys.stderr)
        return 1
    if not gate.allowed:
        print(
            f"\nrefusing to write: write not permitted (blocked_by={list(gate.blocked_by)}). "
            "The probe respects the config posture the EMS enforces: set system.dry_run=false "
            "and enable the transport write gate to measure.",
            file=sys.stderr,
        )
        return 1
    if not opts.confirm_writes:
        print(
            "\nrefusing to write: pass --confirm-writes to publish real outputLimit values. "
            "Ensure the live EMS is stopped first (two writers is forbidden).",
            file=sys.stderr,
        )
        return 1
    if opts.mode_test and not opts.confirm_mode_changes:
        print(
            "\nrefusing mode test: it changes the device operating mode; pass "
            "--confirm-mode-changes in addition to --confirm-writes.",
            file=sys.stderr,
        )
        return 1

    progress = lambda message: print(message, file=sys.stderr, flush=True)  # noqa: E731

    runtime.start()
    exit_code = 0
    samples = []
    mode_result = None
    activity = WriteActivity()
    initial = {}
    try:
        if not wait_for_broker(dev, opts.connect_timeout):
            print("error: MQTT broker did not connect within the timeout", file=sys.stderr)
            return 1
        initial = initial_restore_state(reader.ip, session, required_properties)
        print(f"initial state: {initial}\n")

        # Preflight: the complete captured state must be restorable before any
        # write — otherwise the test could leave the device in a changed state
        # it cannot restore. Abort before the first publish.
        unrestorable = preflight_restorable(dev, initial, contracts)
        if unrestorable:
            print(
                "preflight_failed: complete operation-derived restore is not "
                f"possible {unrestorable}; no write performed.",
                file=sys.stderr,
            )
            return 1

        if opts.mode_test:
            mode_result = run_mode_recovery_test(
                dev,
                reader,
                opts.values[0],
                opts,
                activity=activity,
                progress=progress,
            )
            print(f"\nmode test: {mode_result['result']} — {mode_result['detail']}")
            if "local_submit_duration_ms" in mode_result:
                print(
                    "mode timing: "
                    f"local_submit={_fmt_ms(mode_result.get('local_submit_duration_ms'))} "
                    "broker_delivery_from_submit="
                    f"{_fmt_ms(mode_result.get('broker_delivery_from_submit_ms'))} "
                    "setpoint_match_from_submit="
                    f"{_fmt_ms(mode_result.get('setpoint_match_from_submit_ms'))} "
                    "physical_reaction_from_submit="
                    f"{_fmt_ms(mode_result.get('physical_reaction_from_submit_ms'))} "
                    "physical_after_setpoint="
                    f"{_fmt_ms(mode_result.get('physical_reaction_after_setpoint_ms'))}"
                )
            if mode_result["result"] not in ("mode_and_setpoint_verified", "not_applicable"):
                exit_code = 1
        else:
            samples = run_probe(
                dev,
                reader,
                opts.values,
                opts,
                activity=activity,
                progress=progress,
            )
    except RuntimeError as exc:
        print(f"\naborted: {exc}", file=sys.stderr)
        exit_code = 1
    except KeyboardInterrupt:
        print("\ninterrupted: restoring initial state", file=sys.stderr)
        exit_code = 130
    finally:
        try:
            try:
                report = finalize_restoration(
                    dev,
                    reader,
                    initial,
                    contracts,
                    opts,
                    activity,
                    progress=progress,
                )
            except Exception as exc:
                changed = activity.accepted_modified_properties or list(
                    required_properties
                )
                captured = sorted(set(initial or {}) & set(changed))
                report = _restore_report(
                    status="restore_failed",
                    attempted=activity.locally_accepted,
                    submitted=False,
                    captured_properties=captured,
                    changed_properties=changed,
                    submitted_properties=(),
                    matched_properties=(),
                    unknown_properties=changed,
                    mismatched_properties={},
                    accepted_command_ids=activity.accepted_command_ids,
                    detail=(
                        "restore orchestration failed closed "
                        f"({type(exc).__name__})"
                    ),
                    state=None,
                )
                exit_code = exit_code or 1
            print(f"\nrestore: {report}")
            # A locally accepted test command requires a fully verified restore.
            exit_code = exit_code_after_restoration(exit_code, activity, report)
        finally:
            runtime.stop()

    if samples:
        stats = summarize(samples, opts)
        print()
        print(format_text_report(dev, reader, opts.values, samples, stats))
        if opts.markdown:
            print()
            print(format_markdown_report(dev, reader, opts.values, stats))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
