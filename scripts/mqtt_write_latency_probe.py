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
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ems.mqtt_control.command_state import property_matches  # noqa: E402

RESTORE_PROPERTIES = ("smartMode", "acMode", "outputLimit", "inputLimit")

# Property-type classification shared with the confirmation contract: watt-like
# properties compare within a tolerance, every other property is an enum/mode and
# compares exactly. Restore/mode verification must never apply a watt tolerance to
# a mode value.
WATT_PROPERTIES = ("outputLimit", "inputLimit")


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
        f"device_id=…{_last4(getattr(dev, '_device_id', None))}"
    )


def _candidate_list(devices):
    return "; ".join(_redact_candidate(d) for d in devices)


def select_mqtt_device(runtime, *, name=None, serial=None, device_id=None,
                       broker_ref=None, http_serial=None):
    """Select exactly one configured MQTT control device, or return an error.

    Every supplied selector must match; the HTTP-reported serial (``http_serial``)
    is treated as an additional serial filter. Zero matches is an error; more than
    one at the same specificity aborts with a redacted candidate list — the probe
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
        if http_serial is not None and str(getattr(dev, "sn", "")) != str(http_serial):
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
        if http_serial is not None:
            return None, (
                f"no MQTT control device has serial {http_serial}. "
                f"MQTT control devices: {_candidate_list(devices)}"
            )
        return None, (
            "no MQTT control device matches the given selectors "
            f"(--device/--serial/--device-id/--broker-ref). Devices: {_candidate_list(devices)}"
        )
    return None, (
        f"ambiguous device selection: {len(candidates)} devices match. Narrow with "
        f"--device-name/--serial/--device-id/--broker-ref. Candidates: "
        f"{_candidate_list(candidates)}"
    )


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
    """Anchor on the HTTP API IP: read its serial, then find the matching MQTT device.

    Returns ``(dev, reader, resolved_serial, err)``. The reported serial plus any
    explicit selectors must resolve to exactly one control device; two devices
    sharing the serial abort with a redacted candidate list rather than silently
    selecting the first.
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
    dev, err = select_mqtt_device(
        runtime, name=device_name, serial=serial, device_id=device_id,
        broker_ref=broker_ref, http_serial=reported_serial,
    )
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


def initial_restore_state(ip, session):
    """Capture the writable power properties the device actually reports."""

    report = fetch_api_report(ip, session)
    if not isinstance(report, dict):
        return None
    properties = report.get("properties")
    if not isinstance(properties, dict):
        properties = report
    captured = {}
    for key in RESTORE_PROPERTIES:
        value = properties.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        captured[key] = int(value)
    return captured or None


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
    publish_submitted: bool
    # Local Paho submit time only — never broker delivery (see broker_delivery_ms).
    local_publish_submit_ms: float
    # Observed broker PUBACK delivery (delivered/timeout/untracked/pending).
    broker_delivery_status: str
    broker_delivery_ms: float | None
    # HTTP-visible setpoint match time; None unless the target was actually matched.
    setpoint_http_ms: float | None
    last_observed_output_limit: int | None
    # Movement away from baseline is diagnostics only — never setpoint landing.
    movement_observed: bool
    matched_target: bool
    timed_out: bool
    polls: int
    mode_ok: bool | None = None
    physical: str | None = None
    physical_output_ms: float | None = None


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


def publish_target(dev, target):
    """Publish the production command; return ``(ok, local_submit_ms, mid, expected)``.

    ``local_submit_ms`` is the time to hand the message to the Paho client — local
    submission only, never broker delivery. ``mid`` correlates a later PUBACK.
    """

    message, _message_id, _operation, expected = build_command(dev, target)
    if message.retain:
        raise RuntimeError("refusing to publish a retained control command")
    start = time.monotonic()
    submission = dev._publish_message(message)
    local_submit_ms = (time.monotonic() - start) * 1000.0
    mid = getattr(submission, "mid", None)
    return bool(submission), local_submit_ms, mid, expected


def observe_broker_delivery(dev, mid, opts, *, sleep=time.sleep, now=time.monotonic):
    """Observe the broker PUBACK for a publish; return ``(status, delivery_ms)``.

    ``status`` is delivered/timeout/untracked. ``untracked`` means the transport
    exposes no mid (delivery is unobservable), never that it failed. Bounded by
    the ack timeout so the probe never blocks.
    """

    confirmed = getattr(getattr(dev, "_service", None), "delivery_confirmed", None)
    if mid is None or not callable(confirmed):
        return "untracked", None
    start = now()
    deadline = start + opts.connect_timeout
    while now() < deadline:
        if confirmed(mid):
            return "delivered", (now() - start) * 1000.0
        sleep(opts.poll_interval)
    return "timeout", None


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


def verify_physical(reader, target, opts, *, sleep=time.sleep, now=time.monotonic, progress=None):
    """Verify the physical output reaction; return ``(verdict, physical_ms)``.

    ``physical_ms`` is the time to a non-``not_reacted`` verdict, or ``None`` when
    the output never reacted within the window.
    """

    if progress is None:
        progress = _no_progress
    start = now()
    deadline = start + opts.output_timeout
    last = None
    while now() < deadline:
        sleep(opts.poll_interval)
        last = read_power_state(reader)
        if last is None:
            continue
        verdict = classify_physical(last, target, opts.output_tolerance)
        progress(f"  output check: outputHomePower={last['outputHomePower']}W -> {verdict}")
        if verdict != "not_reacted":
            return verdict, (now() - start) * 1000.0
    return classify_physical(last, target, opts.output_tolerance), None


def run_probe(dev, reader, values, opts, *, sleep=time.sleep, now=time.monotonic, progress=None):
    """Run the write/observe loop and return the collected samples.

    Raises ``RuntimeError`` on a foreign-writer contention detection so the caller
    can stop and restore rather than report polluted numbers. ``progress`` receives
    human-readable status lines as the run proceeds (default: no-op).
    """

    if progress is None:
        progress = _no_progress

    samples = []
    last_matched = None
    for index in range(opts.samples):
        baseline = read_output_limit(reader)
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

        ok, submit_ms, mid, expected = publish_target(dev, target)
        progress(
            f"sample {index + 1}/{opts.samples}: wrote {target}W (baseline {baseline}W) "
            f"local_submit={'ok' if ok else 'FAILED'} {submit_ms:.0f}ms"
        )
        if not ok:
            progress("  -> publish failed (broker down or write blocked); skipping")
            samples.append(
                Sample(
                    index, target, baseline, False, submit_ms, "untracked", None,
                    None, None, False, False, False, 0,
                )
            )
            sleep(opts.settle)
            continue

        delivery_status, delivery_ms = observe_broker_delivery(
            dev, mid, opts, sleep=sleep, now=now
        )
        progress(f"  broker delivery: {delivery_status}"
                 + (f" {delivery_ms:.0f}ms" if delivery_ms is not None else ""))

        start = now()
        deadline = start + opts.timeout
        observed = baseline
        polls = 0
        matched_at = None
        movement_observed = False
        while now() < deadline:
            sleep(opts.poll_interval)
            polls += 1
            observed = read_output_limit(reader)
            progress(f"  t+{polls * opts.poll_interval:.0f}s outputLimit={observed}W")
            if observed is None:
                continue
            if moved(observed, baseline, opts.match_tolerance):
                movement_observed = True
            # Setpoint landing requires the actual target, never mere movement.
            if abs(observed - target) <= opts.match_tolerance:
                matched_at = now()
                break

        if matched_at is None:
            progress(
                f"  -> TIMED OUT after {opts.timeout:.0f}s (outputLimit {observed}W, "
                f"target {target}W{', moved but never matched' if movement_observed else ''})"
            )
            samples.append(
                Sample(
                    index, target, baseline, True, submit_ms, delivery_status,
                    delivery_ms, None, observed, movement_observed, False, True, polls,
                )
            )
        else:
            setpoint_http_ms = (matched_at - start) * 1000.0
            power_state = read_power_state(reader)
            mode_ok = _mode_matches(expected, power_state, opts.match_tolerance)
            physical, physical_ms = None, None
            if opts.verify_output:
                physical, physical_ms = verify_physical(
                    reader, target, opts, sleep=sleep, now=now, progress=progress
                )
            progress(
                f"  -> matched target after {setpoint_http_ms:.0f}ms (outputLimit={observed}W"
                + (f", mode_ok={mode_ok}" if mode_ok is not None else "")
                + (f", physical={physical}" if physical else "")
                + ")"
            )
            samples.append(
                Sample(
                    index, target, baseline, True, submit_ms, delivery_status,
                    delivery_ms, setpoint_http_ms, observed, movement_observed, True,
                    False, polls, mode_ok=mode_ok, physical=physical,
                    physical_output_ms=physical_ms,
                )
            )
            last_matched = observed
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


def run_mode_recovery_test(dev, reader, target, opts, *, sleep=time.sleep, now=time.monotonic, progress=None):
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
    ok, submit_ms, _mid, expected = publish_target(dev, target)
    if not ok:
        return {"result": "publish_failed", "detail": f"publish failed after {submit_ms:.0f}ms"}
    deadline = now() + opts.timeout
    while now() < deadline:
        sleep(opts.poll_interval)
        current = read_power_state(reader)
        if current is None:
            continue
        progress(
            f"  smartMode={current.get('smartMode')} acMode={current.get('acMode')} "
            f"outputLimit={current.get('outputLimit')}W inputLimit={current.get('inputLimit')}W"
        )
        verdict = _mode_test_verdict(expected, current, opts.match_tolerance)
        if verdict is not None:
            return verdict
    final = read_power_state(reader)
    return {
        "result": "timed_out",
        "detail": f"state after {opts.timeout:.0f}s: {final}",
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


def preflight_restorable(dev, initial):
    """Return ``{key: reason}`` for captured properties this device cannot restore.

    A mode-changing test must not begin unless the complete captured state can be
    restored by the production property writer (e.g. an initial ``acMode`` outside
    the profile's writable range). An empty result means the full restore is safe.
    """

    if not initial:
        return {}
    desired = {k: v for k, v in initial.items() if k in RESTORE_PROPERTIES}
    return dev.check_property_writes(desired)


def restore_initial_state(dev, reader, initial, opts, *, sleep=time.sleep, now=time.monotonic, progress=None):
    """Restore the complete captured power state via the production write path.

    Returns a report dict. ``restore_verified`` is true only when every captured
    property is restored and verified by type (watt-like within tolerance,
    mode/enum exactly). An ``outputLimit``-only fallback (when the atomic property
    write is rejected) is reported as ``restore_partial`` — never as a full
    restore. Never raises.
    """

    if progress is None:
        progress = _no_progress
    if not initial:
        return {"restored": False, "restore_verified": False, "restore_partial": False,
                "detail": "no initial state captured"}
    desired = {k: v for k, v in initial.items() if k in RESTORE_PROPERTIES}
    partial = False
    try:
        result = dev.write_properties(desired, reason="probe_restore")
        ok = bool(result)
        detail = None if ok else getattr(result, "reason", "rejected")
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}"
    if not ok and "outputLimit" in desired:
        try:
            ok, _ms, _mid, _expected = publish_target(dev, desired["outputLimit"])
            partial = ok
            detail = f"fallback outputLimit-only restore (properties write: {detail})"
        except Exception as exc:
            return {"restored": False, "restore_verified": False, "restore_partial": False,
                    "detail": f"restore publish failed: {type(exc).__name__}"}
    if not ok:
        return {"restored": False, "restore_verified": False, "restore_partial": False,
                "detail": detail or "publish failed"}

    # A partial (outputLimit-only) fallback can only ever verify the setpoint, so
    # only the outputLimit is checked and full restoration is never claimed.
    verify_keys = {"outputLimit"} if partial else set(desired)
    deadline = now() + opts.timeout
    verified = False
    while now() < deadline:
        sleep(opts.poll_interval)
        current = read_power_state(reader)
        if current is None:
            continue
        mismatch = {
            key: (current.get(key), desired[key])
            for key in verify_keys
            if current.get(key) is None
            or not property_matches(
                key, current[key], desired[key], watt_tolerance=opts.match_tolerance
            )
        }
        if not mismatch:
            verified = True
            break
    restore_verified = verified and not partial
    if verified:
        progress(
            ("partial (outputLimit-only) restore verified: " if partial
             else "restore verified: ") + f"{desired}"
        )
    return {
        "restored": True,
        "restore_verified": restore_verified,
        "restore_partial": partial,
        "verified": restore_verified,
        "state": desired,
        "detail": (
            (detail + "; " if detail else "")
            + ("verified" if verified else "HTTP verification timed out")
        ),
    }


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
    latencies = sorted(s.setpoint_http_ms for s in matched if s.setpoint_http_ms is not None)
    submits = sorted(s.local_publish_submit_ms for s in samples if s.publish_submitted)
    deliveries = sorted(
        s.broker_delivery_ms for s in samples if s.broker_delivery_ms is not None
    )
    ok = len(latencies)
    # Mutually exclusive buckets. Movement toward the target that never matched is
    # diagnostics only and is counted separately from a sample that never moved.
    unmatched = [s for s in samples if s.publish_submitted and not s.matched_target]
    movement_only = sum(1 for s in unmatched if s.movement_observed)
    timed_out = sum(1 for s in unmatched if not s.movement_observed)
    return {
        "samples": len(samples),
        "matched": len(matched),
        "landed": len(matched),
        "movement_only": movement_only,
        "timed_out": timed_out,
        "publish_failed": sum(1 for s in samples if not s.publish_submitted),
        "broker_delivered": sum(1 for s in samples if s.broker_delivery_status == "delivered"),
        "mode_ok": sum(1 for s in samples if s.mode_ok),
        "output_reacted": sum(1 for s in samples if s.physical == "output_reacted"),
        "poll_resolution_ms": opts.poll_interval * 1000.0,
        "setpoint_http_min_ms": latencies[0] if ok else None,
        "setpoint_http_p50_ms": percentile(latencies, 0.5),
        "setpoint_http_p95_ms": percentile(latencies, 0.95),
        "setpoint_http_max_ms": latencies[-1] if ok else None,
        "local_submit_p50_ms": percentile(submits, 0.5),
        "broker_delivery_p50_ms": percentile(deliveries, 0.5) if deliveries else None,
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
        f"{'#':>2}  {'target':>7}  {'baseline':>8}  {'setpoint_ms':>11}  "
        f"{'submit_ms':>9}  {'broker':>9}  {'polls':>5}  status"
    )
    for s in samples:
        if not s.publish_submitted:
            status = "publish_failed"
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
        if s.broker_delivery_ms is not None:
            broker = f"{s.broker_delivery_ms:.0f}ms"
        lines.append(
            f"{s.index:>2}  {s.target:>7}  {s.baseline:>8}  "
            f"{_fmt_ms(s.setpoint_http_ms):>11}  {s.local_publish_submit_ms:>9.1f}  "
            f"{broker:>9}  {s.polls:>5}  {status}"
        )
    lines.append("")
    lines.append(
        f"matched {stats['matched']}/{stats['samples']}  "
        f"movement_only={stats['movement_only']}  timed_out={stats['timed_out']}  "
        f"publish_failed={stats['publish_failed']}  broker_delivered={stats['broker_delivered']}  "
        f"poll_resolution={stats['poll_resolution_ms']:.0f} ms"
    )
    lines.append(
        f"setpoint HTTP-match ms  min={_fmt_ms(stats['setpoint_http_min_ms'])}  "
        f"p50={_fmt_ms(stats['setpoint_http_p50_ms'])}  "
        f"p95={_fmt_ms(stats['setpoint_http_p95_ms'])}  "
        f"max={_fmt_ms(stats['setpoint_http_max_ms'])}  (matched samples only)"
    )
    lines.append(
        f"local submit ms p50={_fmt_ms(stats['local_submit_p50_ms'])}   "
        f"broker delivery ms p50={_fmt_ms(stats['broker_delivery_p50_ms'])}"
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
            f"| Setpoint HTTP-match min | {_fmt_ms(stats['setpoint_http_min_ms'])} ms |",
            f"| Setpoint HTTP-match p50 | {_fmt_ms(stats['setpoint_http_p50_ms'])} ms |",
            f"| Setpoint HTTP-match p95 | {_fmt_ms(stats['setpoint_http_p95_ms'])} ms |",
            f"| Setpoint HTTP-match max | {_fmt_ms(stats['setpoint_http_max_ms'])} ms |",
            f"| Local submit p50 | {_fmt_ms(stats['local_submit_p50_ms'])} ms |",
            f"| Broker delivery p50 | {_fmt_ms(stats['broker_delivery_p50_ms'])} ms |",
            f"| Poll resolution | {stats['poll_resolution_ms']:.0f} ms |",
            "",
            "_Local submit = time to hand the command to the MQTT client. Broker "
            "delivery = observed PUBACK. Setpoint HTTP-match = MQTT publish → target "
            "value visible on the device's local HTTP API (matched samples only; "
            "movement toward the target is not a match). Measured with the EMS "
            "stopped (single writer)._",
        ]
    )


def print_preview(dev, reader, values, gate, initial):
    """Secret-free operation plan: what would be published, where, and how."""

    described = dev.describe()
    print("operation plan (dry preview):")
    print(f"  device: {dev.name}  source={dev.source}  broker_ref={dev.broker_ref}")
    print(
        f"  hardware_profile={dev.hardware_profile}  "
        f"power_write_profile={described.get('power_write_profile')}"
    )
    print(
        f"  effective write topic: {described.get('effective_write_topic')} "
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
    for value in values:
        try:
            message, _mid, operation, _expected = build_command(dev, value)
            properties = json.loads(message.payload).get("properties")
            print(
                f"  {operation} {value}W -> topic={message.topic} qos={message.qos} "
                f"retain={message.retain} properties={properties}"
            )
        except Exception as exc:
            print(f"  {value}W -> NOT BUILDABLE ({exc})")
    if initial:
        restore = {k: v for k, v in initial.items() if k in RESTORE_PROPERTIES}
        unrestorable = preflight_restorable(dev, initial)
        restorable = {k: v for k, v in restore.items() if k not in unrestorable}
        print(f"  restorable initial properties: {restorable}")
        if unrestorable:
            print(f"  NON-RESTORABLE initial properties: {unrestorable} "
                  "(a mode-changing test would be refused at preflight)")
    print("  single-writer advisory: stop the live EMS before writing "
          "(two writers to outputLimit is forbidden).")


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
    from ems.zendure_mqtt.config_entries import (
        find_duplicate_device_names,
        find_duplicate_zendure_device_identities,
    )
    from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime

    # Fail closed on a config the live EMS would refuse: duplicate device
    # identities/names make single-writer selection ambiguous.
    devices_config = config.get("devices") if isinstance(config, dict) else None
    if find_duplicate_zendure_device_identities(devices_config) or find_duplicate_device_names(
        devices_config
    ):
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

    max_power = getattr(dev, "max_power", None) or cfg.MAX_DEVICE_POWER
    for value in opts.values:
        if value < 0:
            print(f"error: negative setpoint {value} W not supported by this probe", file=sys.stderr)
            return 1
        if max_power and value > max_power:
            print(f"error: setpoint {value} W exceeds device max_power {max_power} W", file=sys.stderr)
            return 1

    gate = cfg.resolve_write_gate(dev.control_gate)
    print(f"config: {config_path}")
    print(f"mqtt device: {dev.name}  serial={matched_serial}  transport={dev.source}  gate={gate.gate_name} enabled={gate.gate_enabled}")
    print(f"http read: {reader.name} @ {reader.ip}")
    print(f"values: {opts.values} W   samples: {opts.samples}   poll: {opts.poll_interval}s   settle: {opts.settle}s")

    if opts.dry_preview:
        initial = initial_restore_state(reader.ip, session)
        print()
        print_preview(dev, reader, opts.values, gate, initial)
        print("\ndry-preview: no writes performed.")
        return 0

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
    initial = None
    exit_code = 0
    samples = []
    mode_result = None
    wrote = False
    try:
        if not wait_for_broker(dev, opts.connect_timeout):
            print("error: MQTT broker did not connect within the timeout", file=sys.stderr)
            return 1
        initial = initial_restore_state(reader.ip, session)
        if initial is None or "outputLimit" not in initial:
            print("error: could not read initial power state from HTTP API", file=sys.stderr)
            return 1
        print(f"initial state: {initial}\n")

        # Preflight: the complete captured state must be restorable before any
        # write — otherwise the test could leave the device in a changed state
        # it cannot restore. Abort before the first publish.
        unrestorable = preflight_restorable(dev, initial)
        if unrestorable:
            print(
                f"preflight_failed: this profile cannot restore the captured initial "
                f"state {unrestorable}; refusing to write.",
                file=sys.stderr,
            )
            return 1

        wrote = True
        if opts.mode_test:
            mode_result = run_mode_recovery_test(
                dev, reader, opts.values[0], opts, progress=progress
            )
            print(f"\nmode test: {mode_result['result']} — {mode_result['detail']}")
            if mode_result["result"] not in ("mode_and_setpoint_verified", "not_applicable"):
                exit_code = 1
        else:
            samples = run_probe(dev, reader, opts.values, opts, progress=progress)
    except RuntimeError as exc:
        print(f"\naborted: {exc}", file=sys.stderr)
        exit_code = 1
    except KeyboardInterrupt:
        print("\ninterrupted: restoring initial state", file=sys.stderr)
        exit_code = 130
    finally:
        # Restore only if a write actually happened (a preflight abort changed
        # nothing). Restore runs on success, timeout, exception and interrupt.
        if wrote and initial is not None:
            report = restore_initial_state(dev, reader, initial, opts, progress=progress)
            print(f"\nrestore: {report}")
            # A partial (outputLimit-only) or unverified restore is a failure, and
            # a zero exit must never hide it.
            if not report.get("restored") or not report.get("restore_verified"):
                exit_code = exit_code or 1
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
