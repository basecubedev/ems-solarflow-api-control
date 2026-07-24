# SPDX-License-Identifier: AGPL-3.0-or-later
"""Control-loop device adapter backed by a Zendure MQTT broker.

Presents the same duck-typed interface the EMS control loop expects from a
``ZendureClient`` (metadata attributes, ``fetch()`` -> ``DeviceState``,
``write_output_limit()``) but reads telemetry from a shared per-broker
``ZendureMqttControlService`` snapshot cache and writes ``outputLimit`` by
publishing to the broker. It carries no HTTP session and does not participate in
HTTP state reconciliation (``supports_state_reconciliation = False``).
"""

import json
import time

from ems import config as cfg
from ems.clients import parse_device
from ems.health import CommHealth
from ems.mqtt_control import dispatch
from ems.mqtt_control.command_state import (
    STATE_ACKNOWLEDGED,
    STATE_PUBLISHED,
    CommandRecord,
    apply_confirmation_timeout,
    apply_reply,
    apply_timeout,
    complete_unconfirmed,
    confirm_from_telemetry,
    mark_publish_failed,
    mark_published,
    mark_superseded,
)
from ems.mqtt_control.confirmation import (
    DEFAULT_CONFIRMATION_TIMEOUT_SECONDS,
    resolve_confirmation_policy,
)
from ems.mqtt_control.zendure_commands import (
    PowerCommandError,
    build_power_command,
    next_power_message_id,
)
from ems.mqtt_control.zendure_profiles import (
    OPERATION_CHARGE,
    OPERATION_DISCHARGE,
    OPERATION_IDLE,
    operation_for_target,
    WRITE_PROFILE_LEGACY_HUB,
    WRITE_PROFILE_LEGACY_OBJECT,
    hardware_profile_by_name,
)
from ems.zendure_mqtt.service import (
    SNAPSHOT_STALE,
    SOURCE_LOCAL_MQTT,
    SOURCE_ZENDURE_CLOUD_MQTT,
)
from ems.zendure_mqtt.write_protocols import (
    PROTOCOL_LEGACY_PROPERTIES_WRITE,
    build_output_limit_message,
    resolve_write_protocol,
)

# Legacy hub/object hardware writes go through the function/invoke command
# builder; every other case keeps the properties/write path.
_INVOKE_WRITE_PROFILES = (WRITE_PROFILE_LEGACY_HUB, WRITE_PROFILE_LEGACY_OBJECT)

# Operations the automatic EMS controller can actually emit. It commands output
# (discharge) and stop (idle) but never AC charge, so a charge-capable adapter is
# still not reachable by the automatic controller — diagnostics surface both.
_CONTROLLER_EMITTED_OPERATIONS = frozenset({OPERATION_DISCHARGE, OPERATION_IDLE})

# Safe default acknowledgement timeout: a published command with no correlated
# reply within this many seconds is timed out (never retried indefinitely).
DEFAULT_COMMAND_ACK_TIMEOUT_SECONDS = 10.0

# A changed target at least this many watts below the in-flight target (or a full
# stop to 0 W) preempts the in-flight command and publishes immediately, so a
# substantial safety reduction is never held behind an old command for its full
# timeout. Smaller changes queue as the single latest pending target.
DEFAULT_SAFETY_PREEMPT_MARGIN_W = 300


class _WriteBlocked(Exception):
    """A power write cannot be built and must fail closed (no publish)."""

    def __init__(self, field, error):
        super().__init__(error)
        self.field = field
        self.error = error


def _validate_power_target(value):
    """Return an explicit integer watt target, or raise ``_WriteBlocked``.

    Only a real ``int`` (never ``bool``) is accepted — a numeric string, a float
    (fractional or not), a non-finite value, ``None`` or any object is a
    programming/tampering error, rejected with ``invalid_power_target`` and never
    coerced through ``int()``.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise _WriteBlocked("outputLimit", "invalid_power_target")
    return value


def _coerce_reply(payload):
    """Parse a reply payload (bytes/str/mapping) into a dict, or ``None``.

    A malformed reply from a hostile broker never raises; it is simply ignored.
    """

    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            return None
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None

# Broker source -> named write gate the controller must satisfy for this device.
_SOURCE_GATE = {
    SOURCE_LOCAL_MQTT: "mqtt_local",
    SOURCE_ZENDURE_CLOUD_MQTT: "mqtt_zendure",
}


class ZendureMqttDeviceClient:
    """A Zendure inverter controlled over MQTT rather than the local HTTP API."""

    ip = "mqtt"
    supports_state_reconciliation = False

    def __init__(
        self,
        name,
        service,
        *,
        device_id,
        topic_family,
        source,
        broker_ref=None,
        product_key=None,
        write_topic=None,
        write_protocol=None,
        hardware_profile=None,
        power_write_profile=None,
        serial_number=None,
        min_soc=0,
        max_soc=0,
        smart_mode=1,
        grid_off_mode=None,
        max_power=None,
        pv_kwp=1.0,
        battery_kwh=1.0,
        pv_priority_factor=1.0,
        command_ack_timeout_seconds=DEFAULT_COMMAND_ACK_TIMEOUT_SECONDS,
        confirmation_timeout_seconds=DEFAULT_CONFIRMATION_TIMEOUT_SECONDS,
        confirmation_tolerance_w=None,
        safety_preempt_margin_w=DEFAULT_SAFETY_PREEMPT_MARGIN_W,
        telemetry_confirmation_supported=None,
    ):
        self.name = name
        self._service = service
        self._device_id = device_id
        self._topic_family = topic_family
        self._product_key = product_key
        self._write_topic = write_topic
        self.source = source
        self.broker_ref = broker_ref
        self.write_protocol = resolve_write_protocol(topic_family, write_protocol)
        # A pinned hardware profile (from config) selects the write adapter, never
        # the topic family. Absent a profile, the legacy properties/write path is
        # used unchanged.
        self.hardware_profile = hardware_profile
        self._power_profile = (
            hardware_profile_by_name(hardware_profile) if hardware_profile else None
        )
        # An explicitly configured hardware profile that does not resolve to a
        # known model must never fall back to a topic-family write path: the
        # device fails closed rather than publishing an unverified command. A
        # stored power_write_profile that contradicts the resolved model is the
        # same class of tampered/corrupt config and also fails closed, even if
        # config validation was bypassed.
        self._hardware_profile_invalid = (
            bool(hardware_profile) and self._power_profile is None
        ) or (
            self._power_profile is not None
            and isinstance(power_write_profile, str)
            and power_write_profile.strip()
            and power_write_profile.strip() != self._power_profile.power_write_profile
        )
        # Command lifecycle. ``_active_command`` is the single in-flight command
        # (queued/published/acknowledged) awaiting a terminal outcome. Exactly one
        # command is in flight per physical device: a changed target while one is
        # active is stored in ``_pending_target`` (a single slot, never a queue)
        # and published once the active command reaches a terminal state.
        # ``_last_command`` is the most recent record for diagnostics.
        self._active_command = None
        self._pending_target = None
        self._pending_correlation_id = None
        self._active_correlation_id = None
        self._last_command = None
        self._last_command_state = None
        self._dispatch_observer = None
        self._dispatch_sequence = 0
        try:
            self._command_ack_timeout_s = max(0.0, float(command_ack_timeout_seconds))
        except (TypeError, ValueError):
            self._command_ack_timeout_s = DEFAULT_COMMAND_ACK_TIMEOUT_SECONDS
        try:
            self._confirmation_timeout_s = max(0.0, float(confirmation_timeout_seconds))
        except (TypeError, ValueError):
            self._confirmation_timeout_s = DEFAULT_CONFIRMATION_TIMEOUT_SECONDS
        # None -> use the profile's default confirmation tolerance.
        try:
            self._confirmation_tolerance_w = (
                None
                if confirmation_tolerance_w is None
                else max(0, int(confirmation_tolerance_w))
            )
        except (TypeError, ValueError):
            self._confirmation_tolerance_w = None
        try:
            self._safety_preempt_margin_w = max(0, int(safety_preempt_margin_w))
        except (TypeError, ValueError):
            self._safety_preempt_margin_w = DEFAULT_SAFETY_PREEMPT_MARGIN_W
        # None -> resolve from the write profile; explicit False -> no reliable
        # telemetry confirmation (completed_unconfirmed after publish/ack).
        self._telemetry_confirmation_override = telemetry_confirmation_supported
        self.sn = serial_number or device_id
        self.control_gate = _SOURCE_GATE.get(source, "mqtt_local")
        self.min_soc = min_soc
        self.max_soc = max_soc
        self.smart_mode = smart_mode
        self.grid_off_mode = grid_off_mode
        self.max_power = max_power or cfg.MAX_DEVICE_POWER
        self.pv_kwp = pv_kwp or 1.0
        self.battery_kwh = battery_kwh or 1.0
        self.pv_priority_factor = pv_priority_factor or 1.0
        self.read_health = CommHealth(name, kind="read")
        self.write_health = CommHealth(name, kind="write")

    def fetch(self):
        """Map a fresh broker snapshot to a DeviceState, else signal read failure.

        Reject stale telemetry so the controller cannot act on a device whose
        broker disconnected or stopped publishing: a stale or unseen snapshot is
        reported as an unavailable read (``None``), never silently reused. A fresh
        snapshot may also confirm an acknowledged command from real telemetry.
        """

        start = time.monotonic()
        now = time.monotonic()
        # Freshness uses the service's own clock (test-injectable); command timing
        # uses this client's monotonic clock — never conflate the two.
        status = self._service.snapshot_status(self._device_id)
        state = None
        if status.is_fresh:
            state = parse_device({"properties": status.snapshot.metrics})
            # Attempt telemetry confirmation from this fresh snapshot BEFORE
            # settling timeouts, so confirming telemetry in the same fetch wins
            # over a confirmation deadline that has just elapsed.
            self._confirm_from_snapshot(state, status.snapshot, now)
        # Settle the in-flight command's deadline and flush any pending target.
        self._expire_active_command(now)
        if not status.is_fresh:
            self.read_health.record_failure(
                error="mqtt_snapshot_stale" if status.state == SNAPSHOT_STALE else "no_snapshot",
                latency_ms=(time.monotonic() - start) * 1000.0,
            )
            return None
        self.read_health.record_success((time.monotonic() - start) * 1000.0)
        return state

    def write_output_limit(self, value):
        """Publish a power write; return whether the request was accepted (bool).

        Backward-compatible wrapper over :meth:`dispatch_output_limit`. The
        structured result the controller consumes distinguishes a *published*
        target from one merely *coalesced* or *queued* behind an in-flight
        command; this wrapper collapses that to the historic boolean.
        """

        return bool(self.dispatch_output_limit(value))

    def set_dispatch_observer(self, observer):
        """Observe later outcomes for targets initially returned as queued."""

        self._dispatch_observer = observer if callable(observer) else None

    def _next_dispatch_correlation_id(self):
        self._dispatch_sequence += 1
        return f"dispatch-{self._dispatch_sequence}"

    def _notify_dispatch_observer(self, result):
        observer = self._dispatch_observer
        if observer is None:
            return
        try:
            observer(result)
        except Exception:
            # Audit delivery must never disturb the command lifecycle.
            return

    def _discard_pending_target(self, reason):
        target = self._pending_target
        correlation_id = self._pending_correlation_id
        self._pending_target = None
        self._pending_correlation_id = None
        if target is not None:
            self._notify_dispatch_observer(
                dispatch.superseded(
                    target, correlation_id=correlation_id, reason=reason
                )
            )

    def dispatch_output_limit(self, value):
        """Publish a power write and report the structured dispatch outcome.

        Every attempted write builds one :class:`CommandRecord` (queued ->
        published/rejected) — a broker publish is transport-level only, never
        acceptance. Routing is decided by the pinned hardware profile: legacy
        hub/object models build a ``function/invoke`` deviceAutomation command, a
        ZenSDK profile keeps the ``properties/write`` shape, a telemetry-only
        profile never writes. Exactly one command is in flight per physical
        device: a repeat of the in-flight target is coalesced (no republish); a
        changed non-safety target is queued as the single latest pending target; a
        safety reduction preempts the in-flight command and publishes immediately.
        The controller and its write gates are unchanged.
        """

        now = time.monotonic()
        # Settle the in-flight command's deadline first, but do not auto-flush a
        # stale pending target here — a fresh target supersedes any pending one.
        self._expire_active_command(now, flush=False)
        try:
            target = _validate_power_target(value)
        except _WriteBlocked as blocked:
            self.write_health.record_failure(
                error=blocked.error, latency_ms=0.0, field=blocked.field
            )
            return dispatch.rejected(None, reason=blocked.error)
        try:
            # Reject an unsupported/over-limit target up front so it is never
            # stored as a pending target behind an in-flight command.
            self._precheck_target(target)
        except _WriteBlocked as blocked:
            self.write_health.record_failure(
                error=blocked.error, latency_ms=0.0, field=blocked.field
            )
            return dispatch.rejected(target, reason=blocked.error)
        active = self._active_command
        if active is not None and active.is_active:
            if active.target_w == target:
                # The in-flight target is already committed; drop any stale pending.
                self._discard_pending_target("superseded_by_active_target")
                return dispatch.coalesced(
                    target,
                    message_id=active.message_id,
                    command_state=active.state,
                    correlation_id=self._active_correlation_id,
                )
            if self._should_preempt(active.target_w, target):
                # Safety preemption: retire the in-flight command out of the slot
                # and publish the safer target now, never waiting behind it. The
                # retired command is terminal, so its late reply/telemetry can
                # never confirm the replacement.
                mark_superseded(active, now_monotonic=now)
                self._last_command_state = active.state
                self._active_command = None
                self._active_correlation_id = None
                self._discard_pending_target("superseded_by_safety_target")
                return self._publish_target(target, now)
            # A changed non-safety target while a command is in flight is stored as
            # the single pending target, published once the active command settles.
            if self._pending_target == target and self._pending_correlation_id:
                correlation_id = self._pending_correlation_id
            else:
                self._discard_pending_target("superseded_by_new_target")
                correlation_id = self._next_dispatch_correlation_id()
            self._pending_target = target
            self._pending_correlation_id = correlation_id
            return dispatch.queued(
                target,
                command_state=active.state,
                correlation_id=correlation_id,
            )

        # No command in flight: the fresh target is the latest intent and takes the
        # slot immediately, superseding any pending target left from before.
        self._discard_pending_target("superseded_by_fresh_target")
        return self._publish_target(target, now)

    def _should_preempt(self, active_target, new_target):
        """Whether a changed target must preempt the in-flight command for safety.

        A full stop (0 W) always preempts any active command — including an active
        charge. Otherwise only a substantial reduction of commanded discharge
        output preempts; a rise, or any charge-side (negative) target, waits as the
        pending target instead (no broker spam). The reduction must be at least the
        safety margin AND strictly exceed the confirmation tolerance, so a
        preempting command can never be cross-confirmed by stale telemetry still
        reporting the old (superseded) target within tolerance — this holds even if
        the margin is misconfigured below the tolerance.
        """

        if new_target == active_target:
            return False
        if new_target == 0:
            return True
        if new_target < 0 or new_target >= active_target:
            return False
        reduction = active_target - new_target
        tolerance = self._confirmation_policy().confirmation_tolerance_w
        return reduction >= self._safety_preempt_margin_w and reduction > tolerance

    def _publish_target(self, target, now, *, correlation_id=None):
        """Build and publish one correlated command; return a dispatch result."""

        correlation_id = correlation_id or self._next_dispatch_correlation_id()

        try:
            topic, payload, message_id, operation = self._build_write(target)
        except _WriteBlocked as blocked:
            self.write_health.record_failure(
                error=blocked.error, latency_ms=0.0, field=blocked.field
            )
            return dispatch.rejected(
                target, reason=blocked.error, correlation_id=correlation_id
            )

        record = CommandRecord(
            message_id=message_id,
            device_id=self._device_id,
            operation=operation,
            target_w=target,
            created_monotonic=now,
            device_key=self._device_id,
            topic=topic,
        )
        start = time.monotonic()
        ok = self._service.publish_output_limit(topic, payload)
        latency_ms = (time.monotonic() - start) * 1000.0
        field = "power_command" if operation is not None and record.topic and record.topic.endswith("function/invoke") else "outputLimit"
        if ok:
            mark_published(record, now_monotonic=now)
            self.write_health.record_success(latency_ms, field=field)
            # A profile with neither a verified acknowledgement nor reliable
            # telemetry confirmation cannot be device-confirmed at all: a
            # successful publish is the strongest honest signal, so complete now
            # instead of occupying the slot until a meaningless timeout.
            if not self._reply_contract().supports_acknowledgement and not (
                self._confirmation_policy().telemetry_confirmation_supported
            ):
                complete_unconfirmed(record, now_monotonic=now)
        else:
            mark_publish_failed(record)
            self.write_health.record_failure(
                error="publish_failed", latency_ms=latency_ms, field=field
            )
        self._active_command = record if record.is_active else None
        self._active_correlation_id = correlation_id if record.is_active else None
        self._last_command = record
        self._last_command_state = record.state
        if ok:
            return dispatch.published(
                target,
                message_id=record.message_id,
                command_state=record.state,
                correlation_id=correlation_id,
            )
        return dispatch.failed(
            target,
            message_id=record.message_id,
            correlation_id=correlation_id,
        )

    def _flush_pending_target(self, now):
        """Publish the single pending target once the active slot is free."""

        if self._active_command is not None:
            return
        target = self._pending_target
        if target is None:
            return
        correlation_id = self._pending_correlation_id
        self._pending_target = None
        self._pending_correlation_id = None
        result = self._publish_target(target, now, correlation_id=correlation_id)
        self._notify_dispatch_observer(result)
        return result

    def _precheck_target(self, target_w):
        """Validate that ``target_w`` could be published now; raise if not.

        Runs every capability/limit guard WITHOUT allocating a message id or
        building a payload, so an unsupported, over-limit or non-addressable
        changed target is rejected immediately and never stored as a pending
        target. Returns the resolved operation. The requested operation is the
        sign of the target.
        """

        if self._hardware_profile_invalid:
            raise _WriteBlocked("outputLimit", "unknown_hardware_profile")
        operation = operation_for_target(target_w)
        if self._power_profile is not None:
            self._require_supported_operation(operation)
            self._enforce_power_limit(target_w, operation)
            return operation
        # No pinned profile: only the explicit custom escape hatch authorizes, and
        # a properties/write must never carry a negative outputLimit.
        if operation == OPERATION_CHARGE:
            raise _WriteBlocked("outputLimit", "charge_target_unsupported")
        self._enforce_power_limit(target_w, operation)
        if self.write_protocol is None:
            raise _WriteBlocked("outputLimit", "no_write_protocol")
        # The custom escape hatch requires an explicit, valid publish topic (never
        # an MQTT subscription filter). Fail closed on an invalid topic.
        from ems.zendure_mqtt.write_protocols import publish_topic_error

        if publish_topic_error(self._write_topic) is not None:
            raise _WriteBlocked("outputLimit", "invalid_write_topic")
        return operation

    def _build_write(self, target_w):
        """Return ``(topic, payload, message_id, operation)`` or raise ``_WriteBlocked``.

        The single place that turns a signed target into an addressed, model-aware
        publish. It never publishes and never falls back to an unverified shape.
        Validation runs first (:meth:`_precheck_target`), so a message id is
        allocated only for a supported, in-range target.
        """

        self._precheck_target(target_w)

        profile = self._power_profile
        if profile is not None:
            write_profile = profile.power_write_profile
            if write_profile in _INVOKE_WRITE_PROFILES:
                message_id = next_power_message_id()
                try:
                    command = build_power_command(
                        hardware_profile=self.hardware_profile,
                        target_w=target_w,
                        product_key=self._product_key,
                        device_id=self._device_id,
                        message_id=message_id,
                        timestamp=int(time.time()),
                    )
                except PowerCommandError as exc:
                    raise _WriteBlocked(
                        "power_command", "unsupported_power_operation"
                    ) from exc
                payload = json.dumps(command.payload).encode("utf-8")
                return command.topic, payload, message_id, command.operation
            # ZenSDK devices keep the properties/write shape, selected by the
            # profile — never inferred from the topic family. Telemetry-only
            # profiles were already rejected by the precheck above.
            return self._build_properties_write(
                target_w, PROTOCOL_LEGACY_PROPERTIES_WRITE
            )

        return self._build_properties_write(target_w, self.write_protocol)

    def _require_supported_operation(self, operation):
        """Reject an operation the pinned model does not support (no publish)."""

        from ems.mqtt_control.power_capability import (
            BLOCK_OPERATION_UNSUPPORTED,
            resolve_power_write_capability,
        )

        cap = resolve_power_write_capability(
            topic_family=self._topic_family,
            hardware_profile=self.hardware_profile,
            operation=operation,
        )
        if cap.supported:
            return
        if cap.block_reason == BLOCK_OPERATION_UNSUPPORTED:
            error = (
                "charge_target_unsupported"
                if operation == OPERATION_CHARGE
                else "unsupported_power_operation"
            )
        elif cap.block_reason == "transport_incompatible":
            error = "transport_incompatible"
        else:
            # Deferred/unknown writable profile → telemetry only.
            error = "telemetry_only_hardware"
        raise _WriteBlocked("outputLimit", error)

    def _enforce_power_limit(self, target_w, operation):
        """Bound a target to the configured safe maximum (defense in depth).

        No model-specific physical limit is invented: until exact per-model limits
        are verified the configured ``max_power`` is the required, enforced ceiling
        for both discharge and charge magnitude.
        """

        if operation == OPERATION_IDLE:
            return
        max_power = self.max_power
        if not isinstance(max_power, (int, float)) or max_power <= 0:
            # No configured safe maximum resolved (config not loaded); the model
            # capability and strict validation above remain the backstop. In a
            # running EMS ``max_power`` falls back to the loaded max_device_power.
            return
        if abs(target_w) > max_power:
            raise _WriteBlocked("outputLimit", "target_above_maximum")

    def _build_properties_write(self, target_w, protocol):
        message_id = next_power_message_id()
        message = build_output_limit_message(
            protocol,
            topic_family=self._topic_family,
            product_key=self._product_key,
            device_id=self._device_id,
            output_limit_w=target_w,
            write_topic=self._write_topic,
            message_id=message_id,
        )
        if message is None:
            raise _WriteBlocked("outputLimit", "no_write_protocol")
        return (
            message.topic,
            message.payload,
            message_id,
            operation_for_target(target_w),
        )

    # --- command lifecycle: replies, timeout, telemetry confirmation ---------

    def _reply_contract(self):
        """The verified reply contract for this device's resolved write profile.

        Selected from the pinned hardware profile's ``power_write_profile`` — never
        from the topic family. A device with no valid writable profile carries the
        no-acknowledgement contract (no reply subscription).
        """

        from ems.mqtt_control.reply_contracts import (
            NO_ACK_REPLY_CONTRACT,
            reply_contract_for_write_profile,
        )

        profile = self._power_profile
        if profile is None or self._hardware_profile_invalid:
            return NO_ACK_REPLY_CONTRACT
        return reply_contract_for_write_profile(profile.power_write_profile)

    def reply_topics(self):
        """Verified reply topic filter(s) this device's replies arrive on.

        Constructed from the resolved write profile's reply contract:
        ``function/invoke/reply`` (leading-slash family: prefixed with ``/``) for
        legacy automation, and nothing for a profile with no verified
        acknowledgement contract. Correlation is still by payload (messageId +
        deviceId), so the topic set is only a coarse subscription filter. Returns
        an empty tuple when the device is not addressable or has no ack contract.
        """

        if not (self._product_key and self._device_id):
            return ()
        contract = self._reply_contract()
        if not contract.reply_suffixes:
            return ()
        from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON_ALT

        base = "" if self._topic_family == FAMILY_LEGACY_JSON_ALT else "iot"
        prefix = f"{base}/{self._product_key}/{self._device_id}"
        return tuple(f"{prefix}/{suffix}" for suffix in contract.reply_suffixes)

    def handle_reply(self, payload):
        """Correlate a device reply to the active command. Return whether applied.

        A wrong-id, wrong-device, stale, duplicate or post-timeout reply is
        ignored and can never change a command's outcome. A broker publish is
        never treated as acceptance — only a correlated success reply is.
        """

        record = self._active_command
        if record is None:
            return False
        if not self._reply_contract().supports_acknowledgement:
            # No verified reply contract: a reply can never acknowledge here.
            return False
        reply = _coerce_reply(payload)
        if reply is None:
            return False
        now = time.monotonic()
        applied = apply_reply(record, reply, now_monotonic=now)
        if applied:
            if record.state == STATE_ACKNOWLEDGED and not (
                self._confirmation_policy().telemetry_confirmation_supported
            ):
                # No reliable telemetry confirmation for this profile: the ack is
                # the strongest honest signal — complete now, don't hold the slot.
                complete_unconfirmed(record, now_monotonic=now)
            self._last_command_state = record.state
            if record.is_terminal:
                self._active_command = None
                self._active_correlation_id = None
                self._flush_pending_target(now)
        return applied

    def _confirmation_policy(self):
        """Resolve this device's telemetry-confirmation policy from its profile."""

        profile = self._power_profile
        write_profile = (
            profile.power_write_profile
            if profile is not None and not self._hardware_profile_invalid
            else None
        )
        return resolve_confirmation_policy(
            write_profile,
            timeout_seconds=self._confirmation_timeout_s,
            tolerance_w=self._confirmation_tolerance_w,
            supported_override=self._telemetry_confirmation_override,
        )

    def _expire_active_command(self, now_monotonic, *, flush=True):
        record = self._active_command
        if record is None:
            if flush:
                self._flush_pending_target(now_monotonic)
            return
        supports_ack = self._reply_contract().supports_acknowledgement
        policy = self._confirmation_policy()
        released = False
        if supports_ack:
            # Ack-capable profile: a published command waits for its reply (ack
            # timeout), then an acknowledged command waits for confirmation.
            released = apply_timeout(
                record,
                now_monotonic=now_monotonic,
                timeout_s=self._command_ack_timeout_s,
            )
            if not released and policy.telemetry_confirmation_supported:
                released = apply_confirmation_timeout(
                    record,
                    now_monotonic=now_monotonic,
                    timeout_s=self._confirmation_timeout_s,
                )
        elif policy.telemetry_confirmation_supported:
            # No-ack, confirmable profile: no acknowledgement is ever coming, so
            # the confirmation deadline runs from the publish. It reaches
            # confirmation_timed_out — never a dishonest ack timed_out.
            released = apply_confirmation_timeout(
                record,
                now_monotonic=now_monotonic,
                timeout_s=self._confirmation_timeout_s,
                from_published=True,
            )
        if released:
            self._last_command_state = record.state
            self._active_command = None
            self._active_correlation_id = None
            if flush:
                self._flush_pending_target(now_monotonic)

    def _confirm_from_snapshot(self, state, snapshot, now_monotonic):
        record = self._active_command
        if record is None or record.confirmed:
            return
        policy = self._confirmation_policy()
        if not policy.telemetry_confirmation_supported:
            return
        # The confirmation metric must actually be present in this snapshot: a
        # missing field that ``parse_device`` defaults to 0 must never confirm a
        # command — especially a 0 W stop, which a defaulted 0 would falsely
        # confirm from telemetry that never reported the output.
        metrics = getattr(snapshot, "metrics", None)
        if not isinstance(metrics, dict) or policy.confirmation_metric not in metrics:
            return
        # An ack-capable profile only confirms an already-acknowledged command;
        # a no-ack profile confirms its published command directly from telemetry.
        allow_from_published = not self._reply_contract().supports_acknowledgement
        observed = getattr(state, "output_limit", None)
        telemetry_monotonic = getattr(snapshot, "last_seen_monotonic", None)
        if confirm_from_telemetry(
            record,
            observed,
            tolerance_w=policy.confirmation_tolerance_w,
            now_monotonic=now_monotonic,
            telemetry_monotonic=telemetry_monotonic,
            allow_from_published=allow_from_published,
        ):
            self._last_command_state = record.state
            self._active_command = None
            self._active_correlation_id = None
            self._flush_pending_target(now_monotonic)

    def _control_capability(self):
        """Resolve this device's power-write capability for diagnostics.

        Returns ``(supported, power_write_profile, supported_operations,
        block_reason)``. A pinned profile is the authority; an unknown pinned
        profile and the explicit custom escape hatch are handled explicitly.
        """

        from ems.mqtt_control.power_capability import (
            BLOCK_HARDWARE_PROFILE_MISSING,
            BLOCK_HARDWARE_PROFILE_UNKNOWN,
            resolve_power_write_capability,
        )

        if self._hardware_profile_invalid:
            return False, None, (), BLOCK_HARDWARE_PROFILE_UNKNOWN
        if self._power_profile is not None:
            cap = resolve_power_write_capability(
                topic_family=self._topic_family, hardware_profile=self.hardware_profile
            )
            return (
                cap.supported,
                cap.write_profile,
                tuple(sorted(cap.supported_operations)),
                cap.block_reason,
            )
        if self.write_protocol is not None:
            # Operator-verified explicit escape hatch: properties/write output.
            return True, None, (OPERATION_DISCHARGE, OPERATION_IDLE), None
        return False, None, (), BLOCK_HARDWARE_PROFILE_MISSING

    def _write_addressable(self):
        """Whether this device has valid write identifiers to publish a command."""

        if self._product_key and self._device_id:
            return True
        return bool(isinstance(self._write_topic, str) and self._write_topic.strip())

    def _confirmation_deadline(self, active):
        """Absolute monotonic deadline for confirming an in-flight command.

        Measured from the acknowledgement for an ack-capable profile, and from
        the publish for a no-ack, telemetry-confirmable profile (which has no
        acknowledgement to wait for).
        """

        if active is None:
            return None
        if not self._confirmation_policy().telemetry_confirmation_supported:
            return None
        if (
            active.state == STATE_ACKNOWLEDGED
            and active.acknowledged_monotonic is not None
        ):
            return active.acknowledged_monotonic + self._confirmation_timeout_s
        if (
            active.state == STATE_PUBLISHED
            and active.published_monotonic is not None
            and not self._reply_contract().supports_acknowledgement
        ):
            return active.published_monotonic + self._confirmation_timeout_s
        return None

    def describe(self, *, now_monotonic=None):
        """Credential-free control-device status for diagnostics/status output.

        Explicit ``control_requested``/``control_supported``/``control_ready``
        fields replace an ambiguous single flag, and the operations the adapter
        supports are reported separately from the operations the automatic
        controller can actually emit (it never emits AC charge).
        """

        if now_monotonic is not None:
            self._expire_active_command(now_monotonic)
        status = self._service.snapshot_status(
            self._device_id, now_monotonic=now_monotonic
        )
        gate = cfg.resolve_write_gate(self.control_gate)
        supported, write_profile, supported_ops, block_reason = (
            self._control_capability()
        )
        reachable = [op for op in supported_ops if op in _CONTROLLER_EMITTED_OPERATIONS]
        # Static eligibility (control_supported) vs live readiness (control_ready):
        # readiness additionally requires the write gate, a connected broker, valid
        # write identifiers and fresh telemetry. control_ready is never true while
        # the broker is disconnected.
        broker_connected = bool(getattr(self._service, "connected", False))
        telemetry_fresh = bool(status.is_fresh)
        identifiers_valid = self._write_addressable()
        control_ready = bool(
            supported
            and gate.gate_enabled
            and broker_connected
            and identifiers_valid
            and telemetry_fresh
        )
        active = self._active_command
        confirmation_deadline = self._confirmation_deadline(active)
        last_command = self._last_command.snapshot() if self._last_command else None
        return {
            "name": self.name,
            "broker_ref": self.broker_ref,
            "hardware_profile": self.hardware_profile,
            "hardware_generation": self._hardware_generation(),
            "power_write_profile": write_profile,
            "supported_operations": list(supported_ops),
            "controller_reachable_operations": reachable,
            "source": self.source,
            "topic_family": self._topic_family,
            # Explicit control state (control_enabled kept for compatibility).
            "control_requested": True,
            "control_supported": supported,
            "control_ready": control_ready,
            "control_block_reason": block_reason,
            # Kept for compatibility: this entry is a configured control device.
            # ``control_ready`` is the authoritative gate/capability-aware state.
            "control_enabled": True,
            # Structured correlated command result (Phase 13). last_command_state
            # is kept for compatibility; last_command carries the correlation.
            "last_command_state": self._last_command_state,
            "last_command": last_command,
            "active_command": active.snapshot() if active is not None else None,
            "pending_target": self._pending_target,
            "confirmation_deadline": confirmation_deadline,
            "command_ack_timeout_seconds": self._command_ack_timeout_s,
            "confirmation_timeout_seconds": self._confirmation_timeout_s,
            "telemetry_confirmation_supported": (
                self._confirmation_policy().telemetry_confirmation_supported
            ),
            "write_protocol": self.write_protocol,
            "write_gate": gate.gate_name,
            "write_gate_enabled": gate.gate_enabled,
            # Live readiness inputs (see control_ready above).
            "broker_connected": broker_connected,
            "telemetry_fresh": telemetry_fresh,
            "connected": broker_connected,
            "state": status.state,
            "age_seconds": round(status.age_seconds, 3) if status.age_seconds is not None else None,
        }

    def _hardware_generation(self):
        """Display/telemetry generation for this device's transport (or ``None``).

        A generation is a display/telemetry grouping only, resolved from the
        observed topic family; it never authorizes control.
        """

        from ems.config_catalog import ZENDURE_MQTT_GENERATIONS

        family = str(self._topic_family or "").strip()
        for gen_id, profile in ZENDURE_MQTT_GENERATIONS.items():
            if profile.get("topic_family") == family:
                return gen_id
        return None
