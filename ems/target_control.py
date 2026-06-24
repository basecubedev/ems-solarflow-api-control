# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import logging
from dataclasses import asdict, dataclass, field

from ems import config as cfg
from ems.logging_utils import log_event
from ems.models import DeviceCapabilities


@dataclass
class ControlLimitExplanation:
    name: str
    active: bool
    value: float | str | None = None
    reason: str | None = None


@dataclass
class DeviceControlExplanation:
    device: str
    online: bool
    pv_input_w: float
    output_w: float
    soc: float | None
    min_soc: float | None
    max_soc: float | None
    max_output_w: float | None = None
    can_export: bool | None = None
    can_discharge: bool | None = None
    capability_reason: str | None = None
    pv_only_limit_w: float | None = None
    pv_priority_factor: float | None = None
    pv_weight: float | None = None
    capacity_weight: float | None = None
    charge_balance_multiplier: float | None = None
    soc_gap_percent: float | None = None
    raw_target_w: float | None = None
    allocated_target_w: float | None = None
    effective_target_w: float | None = None
    adjustment_delta_w: float | None = None
    output_limit_w: float | None = None
    limiting_reason: str | None = None
    decision_reason: str | None = None
    write_decision: str | None = None
    write_reason: str | None = None
    command_target_w: float | None = None
    deadband_reference_w: float | None = None
    deadband_reference_source: str | None = None
    deadband_w: float | None = None


@dataclass
class ControlExplanation:
    mode: str
    requested_total_w: float
    effective_target_total_w: float
    allocated_target_total_w: float
    commanded_total_w: float | None
    devices: dict[str, DeviceControlExplanation]
    limits: list[ControlLimitExplanation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    current_total_w: float | None = None
    raw_requested_total_w: float | None = None
    max_total_power_w: float | None = None
    load_w: float | None = None
    filtered_load_w: float | None = None
    min_output_limit_w: float | None = None
    undistributed_target_w: float = 0

    def to_dict(self):
        return asdict(self)


def _device_name(index, device_config):
    name = getattr(device_config, "name", None)
    return str(name) if name is not None else str(index)


def _device_online(online_devices, index, device_name):
    if online_devices is None:
        return True

    if isinstance(online_devices, dict):
        return bool(online_devices.get(device_name, True))

    try:
        return bool(online_devices[index])
    except (IndexError, KeyError, TypeError):
        return True


def _set_limiting_reason(explanation, reason):
    if explanation and not explanation.limiting_reason:
        explanation.limiting_reason = reason


def detect_capabilities(state):
    """Derive runtime capabilities from firmware telemetry."""

    reasons = []

    fault_observed = state.fault_level > 0
    pv_evidence = state.solar > 0
    output_evidence = state.output > 0
    output_limit_evidence = state.output_limit > 0
    ac_evidence = state.ac_status != 0
    discharge_evidence = (
        state.dc_status != 0
        or state.pack_in > 0
        or output_evidence
    )
    export_evidence = (
        pv_evidence
        or output_evidence
        or output_limit_evidence
        or ac_evidence
    )
    paths_inactive = state.dc_status == 0 and state.ac_status == 0

    # faultLevel is observed firmware telemetry. Live testing showed it may be
    # present while output/PV export is still possible, so it is logged as a
    # warning signal instead of being used as a blanket capability blocker.
    can_charge = state.soc_limit != 1
    can_discharge = (
        state.soc_limit != 2
        and discharge_evidence
    )
    can_export = export_evidence or (
        state.soc_limit != 2
        and not paths_inactive
    )
    can_ac_charge = state.ac_status == 2 and can_charge

    if state.soc_limit == 1:
        reasons.append("charge_inhibit")

    if state.soc_limit == 2:
        reasons.append("discharge_inhibit")

    if state.dc_status == 0:
        reasons.append("dc_inactive")

    if state.ac_status == 0:
        reasons.append("ac_inactive")

    if state.pack_state == 0:
        reasons.append("pack_standby")

    if fault_observed:
        reasons.append("fault_observed")

    if pv_evidence:
        reasons.append("pv_evidence")

    if output_evidence:
        reasons.append("output_evidence")

    if output_limit_evidence:
        reasons.append("output_limit_evidence")

    if ac_evidence:
        reasons.append("ac_evidence")

    return DeviceCapabilities(
        can_charge=can_charge,
        can_discharge=can_discharge,
        can_export=can_export,
        can_ac_charge=can_ac_charge,
        reason=",".join(reasons) if reasons else "normal"
    )


def derive_soc_runtime_state(state):
    """Classify SOC telemetry for diagnostics only."""

    if (
        state.soc_limit == 1
        or (
            state.max_soc > 0
            and state.soc >= state.max_soc
        )
    ):
        return "soc_full"

    if state.soc_limit == 2 or state.soc <= state.min_soc:
        return "soc_empty"

    return "soc_normal"


def firmware_recovery_or_ac_charge_active(state):
    """Return True when firmware appears to be handling charge/recovery."""

    return (
        int(state.ac_status) == 2
        or int(state.soc_limit) == 2
        or (
            state.min_soc > 0
            and state.soc <= state.min_soc
        )
        or state.pack_out > 0
    )


def startup_ac_mode_initialization_blocker(state):
    """Return the reason acMode startup initialization should be skipped."""

    if int(state.ac_mode) != 1:
        return "unknown_or_unsupported_ac_mode"

    if int(state.ac_status) == 2:
        return "ac_charge_active"

    if int(state.soc_limit) == 2:
        return "discharge_cutoff"

    if state.min_soc > 0 and state.soc <= state.min_soc:
        return "soc_at_or_below_min"

    if state.output > 0:
        return "output_active"

    if state.pack_in > 0:
        return "battery_discharge_active"

    if state.pack_out > 0:
        return "battery_charge_active"

    return None

# =====================
# DEVICE CLIENTS
# =====================

def get_device_max_power(device_config):
    if device_config is None:
        return cfg.MAX_DEVICE_POWER

    return device_config.max_power


def get_device_pv_kwp(device_config):
    if device_config is None:
        return 1.0

    return max(0.01, device_config.pv_kwp)


def get_device_pv_priority_factor(device_config):
    if device_config is None:
        return 1.0

    return max(0.01, device_config.pv_priority_factor)


def get_device_battery_kwh(device_config):
    if device_config is None:
        return 1.0

    return max(0.01, device_config.battery_kwh)


def calculate_remaining_time_hours(state, device_config, avg_battery_power_w):
    """Estimate remaining charge/discharge time from smoothed battery power."""

    battery_kwh = getattr(device_config, "battery_kwh", 0) or 0

    if battery_kwh <= 0:
        return 0

    if abs(avg_battery_power_w) < cfg.REMAINING_TIME_MIN_POWER_W:
        return 0

    if avg_battery_power_w > 0:
        max_soc = getattr(device_config, "max_soc", 0) or state.max_soc or 100
        soc_delta = max(0, max_soc - state.soc)
        power_kw = avg_battery_power_w / 1000
    else:
        min_soc = getattr(device_config, "min_soc", 0) or state.min_soc or 0
        soc_delta = max(0, state.soc - min_soc)
        power_kw = abs(avg_battery_power_w) / 1000

    if soc_delta <= 0 or power_kw <= 0:
        return 0

    energy_kwh = battery_kwh * soc_delta / 100
    hours = energy_kwh / power_kw

    return round(min(cfg.REMAINING_TIME_MAX_HOURS, hours), 1)


def usable_battery_weight(state, device_config, capability):
    """Return usable discharge energy in weighted units."""

    if capability and not capability.can_discharge:
        return 0

    if state.max_soc <= 0:
        return 0

    usable_percent = max(0, state.soc - state.min_soc)

    if not cfg.BATTERY_KWH_WEIGHTING:
        return usable_percent

    return max(
        0,
        get_device_battery_kwh(device_config) * usable_percent / 100
    )


def pv_first_weight(pv_only, device_config):
    """Return the weighted PV-first allocation signal."""

    if pv_only <= 0:
        return 0

    if not cfg.PV_KWP_WEIGHTING:
        return pv_only

    return (
        pv_only
        * get_device_pv_kwp(device_config)
        * get_device_pv_priority_factor(device_config)
    )


def is_full_soc_device(state):
    """Return True when the device cannot usefully absorb more PV charge."""

    return (
        state.soc_limit == 1
        or (
            state.max_soc > 0
            and state.soc >= state.max_soc
        )
        or derive_soc_runtime_state(state) == "soc_full"
    )


def pv_charge_balance_context(states):
    """Return SOC spread data used for PV-first charge balancing."""

    soc_values = [
        state.soc
        for state in states
        if state.max_soc > 0
    ]

    if not soc_values:
        return {
            "min_soc": 0,
            "max_soc": 0,
            "soc_gap": 0,
            "balance_factor": 0,
            "balance_strength": 0
        }

    min_soc = min(soc_values)
    max_soc = max(soc_values)
    soc_gap = max_soc - min_soc
    deadband = max(0.0, cfg.PV_CHARGE_BALANCE_DEADBAND_PERCENT)
    full_bias = max(0.0, cfg.PV_CHARGE_BALANCE_FULL_BIAS_PERCENT)

    if (
        not cfg.PV_CHARGE_BALANCE_ENABLED
        or soc_gap <= deadband
    ):
        balance_factor = 0.0
    else:
        balance_factor = min(
            1.0,
            (soc_gap - deadband) / max(1.0, full_bias - deadband)
        )

    configured_strength = min(
        1.0,
        max(0.0, cfg.PV_CHARGE_BALANCE_STRENGTH)
    )

    return {
        "min_soc": min_soc,
        "max_soc": max_soc,
        "soc_gap": soc_gap,
        "balance_factor": balance_factor,
        "balance_strength": balance_factor * configured_strength
    }


def pv_charge_balance_multiplier(state, pv_only, balance_context):
    """Bias PV-first output toward fuller batteries."""

    strength = balance_context["balance_strength"]

    if (
        pv_only <= 0
        or strength <= 0
        or state.max_soc <= 0
        or balance_context["soc_gap"] <= 0
    ):
        return 1.0

    soc_position = (
        (state.soc - balance_context["min_soc"])
        / max(1.0, balance_context["soc_gap"])
    )
    soc_position = min(1.0, max(0.0, soc_position))

    return max(
        0.0,
        1.0 + strength * ((2.0 * soc_position) - 1.0)
    )


def weighted_limited_allocation(total, weights, limits):
    """Allocate a total by weights while preserving per-device limits."""

    allocation = [0] * len(weights)
    active = [
        i
        for i, limit in enumerate(limits)
        if limit > 0 and weights[i] > 0
    ]
    remaining = total

    while active and remaining > 0:
        weight_total = sum(weights[i] for i in active)

        if weight_total <= 0:
            break

        saturated = []
        proposed = {}

        for i in active:
            share = remaining * weights[i] / weight_total
            headroom = limits[i] - allocation[i]

            if share >= headroom:
                proposed[i] = headroom
                saturated.append(i)
            else:
                proposed[i] = share

        if not saturated:
            for i, value in proposed.items():
                allocation[i] += value
            break

        for i in saturated:
            moved = proposed[i]
            allocation[i] += moved
            remaining -= moved

        active = [
            i
            for i in active
            if i not in saturated and allocation[i] < limits[i]
        ]

    return allocation


def allocate_full_soc_pv_first(
    requested_total,
    states,
    pv_weights,
    pv_only_limits,
    device_configs=None,
    capabilities=None
):
    """Prioritize PV export from full batteries before normal PV balancing."""

    full_limits = []
    full_weights = []
    normal_limits = []
    normal_weights = []
    full_indices = []

    for i, state in enumerate(states):
        cap = capabilities[i] if capabilities else None
        dev_config = device_configs[i] if device_configs else None
        can_export = cap.can_export if cap else True
        max_power = get_device_max_power(dev_config)
        full_candidate = (
            can_export
            and is_full_soc_device(state)
            and pv_only_limits[i] > 0
        )

        if full_candidate:
            full_indices.append(i)
            full_limits.append(min(pv_only_limits[i], max_power))
            full_weights.append(pv_weights[i])
            normal_limits.append(0)
            normal_weights.append(0)
        else:
            full_limits.append(0)
            full_weights.append(0)
            normal_limits.append(pv_only_limits[i])
            normal_weights.append(pv_weights[i])

    if not full_indices:
        return None, []

    full_targets = weighted_limited_allocation(
        requested_total,
        full_weights,
        full_limits
    )
    remaining = max(0, requested_total - sum(full_targets))
    normal_targets = weighted_limited_allocation(
        remaining,
        normal_weights,
        normal_limits
    )

    targets = [
        full_target + normal_target
        for full_target, normal_target in zip(full_targets, normal_targets)
    ]

    return targets, full_indices


def apply_battery_topup_after_pv_first(
    targets,
    states,
    device_configs,
    capabilities,
    requested_total
):
    """Top up PV-first targets with battery power where safely available.

    Returns the updated targets and whether any battery top-up was applied.
    """

    deliverable_targets = []

    for i, target in enumerate(targets):
        dev_config = device_configs[i] if device_configs else None
        cap = capabilities[i] if capabilities else None
        max_power = get_device_max_power(dev_config)

        if cap and not cap.can_export:
            max_power = 0

        deliverable_targets.append(max(0, min(max_power, target)))

    targets = deliverable_targets
    pv_first_total = sum(targets)
    missing = max(0, requested_total - pv_first_total)

    if missing <= 0:
        return targets, False

    weights = []
    limits = []
    reasons = []

    for i, state in enumerate(states):
        dev_config = device_configs[i] if device_configs else None
        cap = capabilities[i] if capabilities else None
        device_name = dev_config.name if dev_config else i
        max_power = get_device_max_power(dev_config)
        headroom = max(0, max_power - targets[i])

        if cap and not cap.can_export:
            weights.append(0)
            limits.append(0)
            reasons.append(f"{device_name}:cannot_export")
            continue

        if cap and not cap.can_discharge:
            weights.append(0)
            limits.append(0)
            reasons.append(f"{device_name}:cannot_discharge")
            continue

        if state.soc <= state.min_soc:
            weights.append(0)
            limits.append(0)
            reasons.append(f"{device_name}:soc_at_or_below_min")
            continue

        if headroom <= 0:
            weights.append(0)
            limits.append(0)
            reasons.append(f"{device_name}:no_headroom")
            continue

        weight = usable_battery_weight(
            state,
            dev_config,
            cap
        )

        weights.append(weight)
        limits.append(headroom)

        if weight <= 0:
            reasons.append(f"{device_name}:no_usable_battery")

    topup = weighted_limited_allocation(
        missing,
        weights,
        limits
    )
    topup_total = sum(topup)

    if topup_total > 0:
        targets = [
            target + add
            for target, add in zip(targets, topup)
        ]

        log_event(
            logging.INFO,
            "pv_first_battery_topup",
            requested_total=requested_total,
            pv_first_total=round(pv_first_total),
            topup_w=round(topup_total),
            final_targets=json.dumps([round(t) for t in targets])
        )

    unmet = max(0, requested_total - sum(targets))

    if unmet > 0:
        reason = "no_topup_candidates" if topup_total <= 0 else "topup_limited"
        if reasons:
            reason = f"{reason}:{','.join(reasons)}"

        log_event(
            logging.WARNING,
            "pv_first_battery_topup_unmet",
            requested_total=requested_total,
            pv_first_total=round(pv_first_total),
            topup_w=round(topup_total),
            unmet_w=round(unmet),
            final_targets=json.dumps([round(t) for t in targets]),
            reason=reason
        )

    return targets, topup_total > 0


def apply_constraints_and_redistribute(
    targets,
    device_configs=None,
    capabilities=None,
    target_limits=None
):
    """Clamp targets and redistribute excess power to devices with headroom."""

    device_count = len(targets)
    limits = []

    for i in range(device_count):
        cap = capabilities[i] if capabilities else None
        dev_config = device_configs[i] if device_configs else None

        limit = get_device_max_power(dev_config)

        if cap and not cap.can_export:
            limit = 0

        if target_limits:
            limit = min(limit, max(0, target_limits[i]))

        limits.append(limit)

    clamped = []
    excess = 0

    for target, limit in zip(targets, limits):
        value = max(0, min(limit, round(target)))
        clamped.append(value)
        excess += max(0, round(target) - value)

    if not cfg.REDISTRIBUTE_CLAMPED_POWER or excess <= 0:
        return clamped, excess

    redistributed = clamped[:]

    while excess > 0:
        candidates = [
            i
            for i, value in enumerate(redistributed)
            if value < limits[i]
        ]

        if not candidates:
            break

        share = max(1, round(excess / len(candidates)))
        moved = 0

        for i in candidates:
            headroom = limits[i] - redistributed[i]
            add = min(headroom, share, excess)

            redistributed[i] += add
            excess -= add
            moved += add

            if excess <= 0:
                break

        if moved <= 0:
            break

    return redistributed, excess


def apply_min_output_limit(target, device, min_output_limit):
    """Apply the configured minimum outputLimit for enabled EMS control."""

    if min_output_limit <= 0:
        return target

    guarded_target = max(target, min_output_limit)

    if guarded_target != target:
        log_event(
            logging.INFO,
            "min_output_limit_applied",
            device=device.name,
            original_target_w=target,
            guarded_target_w=guarded_target,
            min_output_limit_w=min_output_limit
        )

    return guarded_target


def calculate_targets(
    load,
    devices,
    max_power,
    device_configs=None,
    capabilities=None,
    requested_total=None,
    explain=False,
    online_devices=None
):
    """
    Intelligent EMS target calculation.

    Strategy:

    Solar surplus:
    - allocate by weighted PV-only contribution

    Battery discharge:
    - allocate only confirmed usable battery energy

    This avoids:
    - cross-device battery charge/discharge churn
    - overusing nearly empty batteries
    """

    if not devices:
        log_event(
            logging.WARNING,
            "no_devices",
            load=load,
            max_power=max_power
        )
        if explain:
            explanation = ControlExplanation(
                mode="no_devices",
                requested_total_w=0,
                effective_target_total_w=0,
                allocated_target_total_w=0,
                commanded_total_w=None,
                devices={},
                limits=[
                    ControlLimitExplanation(
                        "no_devices",
                        True,
                        0,
                        "no device states were available"
                    )
                ],
                notes=["no devices available for target calculation"],
                current_total_w=0,
                raw_requested_total_w=requested_total,
                max_total_power_w=max_power,
                load_w=load
            )
            return [], 0, 0, explanation
        return [], 0, 0

    current_total = sum(d.output for d in devices)
    solar_total = sum(d.solar for d in devices)
    raw_requested_total = (
        requested_total
        if requested_total is not None
        else current_total + load
    )

    if requested_total is None:
        new_total = max(
            0,
            min(max_power, current_total + load)
        )
    else:
        new_total = max(
            0,
            min(max_power, requested_total)
        )

    targets = [0] * len(devices)
    final_target_limits = None
    battery_topup_used = False
    mode = "pv_first" if solar_total >= new_total else "battery_discharge"
    explanation_devices = []
    explanation_device_map = {}
    explanation_limits = []
    explanation_notes = []

    def add_limit(name, active, value=None, reason=None):
        if explain:
            explanation_limits.append(
                ControlLimitExplanation(name, active, value, reason)
            )

    if explain:
        add_limit(
            "max_total_power",
            raw_requested_total > max_power,
            max_power,
            (
                "requested total was clamped to the configured maximum"
                if raw_requested_total > max_power
                else "configured maximum total output"
            )
        )
        add_limit(
            "zero_floor",
            raw_requested_total < 0,
            0,
            (
                "requested total was clamped to zero"
                if raw_requested_total < 0
                else "target calculation never requests negative output"
            )
        )

        for i, d in enumerate(devices):
            dev_config = device_configs[i] if device_configs else None
            cap = capabilities[i] if capabilities else None
            name = _device_name(i, dev_config)
            entry = DeviceControlExplanation(
                device=name,
                online=_device_online(online_devices, i, name),
                pv_input_w=d.solar,
                output_w=d.output,
                soc=d.soc,
                min_soc=d.min_soc,
                max_soc=d.max_soc,
                max_output_w=get_device_max_power(dev_config),
                can_export=cap.can_export if cap else None,
                can_discharge=cap.can_discharge if cap else None,
                capability_reason=cap.reason if cap else None,
                capacity_weight=get_device_battery_kwh(dev_config),
                output_limit_w=d.output_limit,
                decision_reason=mode
            )
            if cap and not cap.can_export:
                entry.limiting_reason = "cannot_export"
            explanation_devices.append(entry)
            explanation_device_map[name] = entry

        add_limit(
            "pv_surplus_available",
            solar_total >= new_total,
            solar_total,
            (
                "total PV can cover the requested output"
                if solar_total >= new_total
                else "battery discharge is needed beyond PV output"
            )
        )
    # =====================
    # CASE 1:
    # Enough solar available
    # =====================

    if solar_total >= new_total:

        #
        # PV-first mode:
        # When total PV can cover the requested AC output,
        # never allocate more output to a device than its
        # currently available PV-only contribution.
        #
        # This avoids inefficient simultaneous battery charging
        # on one device and battery discharging on another device.
        #

        pv_only_limits = []
        pv_weights = []
        balance_context = pv_charge_balance_context(devices)

        for i, d in enumerate(devices):
            cap = capabilities[i] if capabilities else None
            dev_config = device_configs[i] if device_configs else None

            if cap and not cap.can_export:
                pv_only = 0
            else:
                #
                # pack_in = battery discharge power
                # effective PV-only contribution:
                # current solar minus current battery discharge
                #
                pv_only = max(0, d.solar - d.pack_in)

            pv_only_limits.append(pv_only)
            base_pv_weight = pv_first_weight(pv_only, dev_config)
            charge_balance_multiplier = pv_charge_balance_multiplier(
                d,
                pv_only,
                balance_context
            )
            pv_weight = base_pv_weight * charge_balance_multiplier
            pv_weights.append(pv_weight)

            if explain:
                entry = explanation_devices[i]
                entry.pv_only_limit_w = pv_only
                entry.pv_priority_factor = get_device_pv_priority_factor(
                    dev_config
                )
                entry.pv_weight = pv_weight
                entry.charge_balance_multiplier = charge_balance_multiplier
                entry.soc_gap_percent = balance_context["soc_gap"]
                entry.decision_reason = "pv_first_allocation"
                if cap and not cap.can_export:
                    _set_limiting_reason(entry, "cannot_export")
                elif pv_only <= 0:
                    _set_limiting_reason(entry, "no_pv_only_available")

            log_event(
                logging.DEBUG,
                "pv_first_limit",
                device=device_configs[i].name if device_configs else i,
                solar_w=d.solar,
                pack_input_w=d.pack_in,
                output_w=d.output,
                output_limit_w=d.output_limit,
                pv_only_limit_w=round(pv_only),
                pv_weight=round(pv_weight, 3),
                pv_kwp=get_device_pv_kwp(dev_config),
                pv_priority_factor=get_device_pv_priority_factor(dev_config),
                soc=d.soc,
                pack_state=d.pack_state,
                soc_limit=d.soc_limit,
                can_export=cap.can_export if cap else True,
                can_discharge=cap.can_discharge if cap else True
            )
            log_event(
                logging.DEBUG,
                "pv_charge_balance_weight",
                device=device_configs[i].name if device_configs else i,
                soc=d.soc,
                min_soc=balance_context["min_soc"],
                max_soc=balance_context["max_soc"],
                headroom_percent=max(0, d.max_soc - d.soc),
                pv_only_limit_w=round(pv_only),
                base_pv_weight=round(base_pv_weight, 3),
                charge_balance_multiplier=round(
                    charge_balance_multiplier,
                    3
                ),
                final_pv_weight=round(pv_weight, 3),
                soc_gap_percent=round(balance_context["soc_gap"], 3),
                balance_factor=round(balance_context["balance_factor"], 3),
                balance_strength=round(
                    balance_context["balance_strength"],
                    3
                )
            )

        pv_only_total = sum(pv_only_limits)

        if pv_only_total > 0:
            targets, full_soc_indices = allocate_full_soc_pv_first(
                new_total,
                devices,
                pv_weights,
                pv_only_limits,
                device_configs=device_configs,
                capabilities=capabilities
            )

            if targets is not None:
                if explain:
                    add_limit(
                        "full_soc_pv_priority",
                        True,
                        json.dumps(full_soc_indices),
                        "full SOC devices receive PV-first export priority"
                    )
                    for index in full_soc_indices:
                        explanation_devices[
                            index
                        ].decision_reason = "full_soc_pv_priority"
                log_event(
                    logging.INFO,
                    "pv_first_full_soc_priority",
                    requested_total=new_total,
                    devices=json.dumps(full_soc_indices),
                    full_soc_target_w=round(
                        sum(targets[i] for i in full_soc_indices)
                    ),
                    pv_only_total=round(pv_only_total)
                )
            else:
                targets = weighted_limited_allocation(
                    new_total,
                    pv_weights,
                    pv_only_limits
                )

            pv_unmet = new_total - sum(targets)
            if explain:
                add_limit(
                    "pv_only_target_limit",
                    pv_unmet > 0,
                    pv_unmet,
                    (
                        "PV-only contribution could not cover the target"
                        if pv_unmet > 0
                        else "PV-only allocation covered the target"
                    )
                )

            if pv_unmet > 0:
                log_event(
                    logging.DEBUG,
                    "pv_first_limited",
                    requested_total=new_total,
                    pv_only_total=round(pv_only_total),
                    unmet_w=round(pv_unmet)
                )

            before_topup_targets = targets[:]
            (
                targets,
                battery_topup_used
            ) = apply_battery_topup_after_pv_first(
                targets,
                devices,
                device_configs,
                capabilities,
                new_total
            )
            if explain:
                topup_w = sum(targets) - sum(before_topup_targets)
                add_limit(
                    "battery_topup_after_pv_first",
                    battery_topup_used,
                    topup_w,
                    (
                        "battery discharge topped up PV-first allocation"
                        if battery_topup_used
                        else "battery top-up was not needed or not possible"
                    )
                )
                if battery_topup_used:
                    explanation_notes.append(
                        "battery top-up was applied after PV-first allocation"
                    )
                    for index, (before, after) in enumerate(
                        zip(before_topup_targets, targets)
                    ):
                        if after > before:
                            explanation_devices[
                                index
                            ].decision_reason = "pv_first_with_battery_topup"
            if pv_only_total >= new_total and not battery_topup_used:
                final_target_limits = pv_only_limits[:]

        else:

            targets = [0] * len(devices)
            if explain:
                add_limit(
                    "pv_only_target_limit",
                    new_total > 0,
                    new_total,
                    "no PV-only contribution was available"
                )

            if new_total > 0:
                log_event(
                    logging.DEBUG,
                    "pv_first_limited",
                    requested_total=new_total,
                    pv_only_total=0,
                    unmet_w=round(new_total)
                )

                before_topup_targets = targets[:]
                (
                    targets,
                    battery_topup_used
                ) = apply_battery_topup_after_pv_first(
                    targets,
                    devices,
                    device_configs,
                    capabilities,
                    new_total
                )
                if explain:
                    topup_w = sum(targets) - sum(before_topup_targets)
                    add_limit(
                        "battery_topup_after_pv_first",
                        battery_topup_used,
                        topup_w,
                        (
                            "battery discharge topped up zero PV-only "
                            "allocation"
                            if battery_topup_used
                            else "battery top-up was not possible"
                        )
                    )

    # =====================
    # CASE 2:
    # Battery discharge required
    # =====================

    else:

        targets = []

        for i, d in enumerate(devices):
            cap = capabilities[i] if capabilities else None
            exportable_solar = d.solar if not cap or cap.can_export else 0
            targets.append(exportable_solar)
            if explain:
                entry = explanation_devices[i]
                entry.pv_only_limit_w = exportable_solar
                entry.decision_reason = "solar_plus_battery_discharge"
                if cap and not cap.can_export:
                    _set_limiting_reason(entry, "cannot_export")

        exportable_solar_total = sum(targets)
        remaining = max(0, new_total - exportable_solar_total)
        if explain:
            add_limit(
                "battery_discharge_required",
                remaining > 0,
                remaining,
                (
                    "requested target exceeds exportable PV"
                    if remaining > 0
                    else "exportable PV covers the requested target"
                )
            )

        weights = []

        for i, d in enumerate(devices):
            dev_config = device_configs[i] if device_configs else None
            cap = capabilities[i] if capabilities else None

            if d.max_soc <= 0:

                #
                # No battery available
                #

                usable_soc = 0

            else:

                usable_soc = usable_battery_weight(
                    d,
                    dev_config,
                    cap
                )

            weights.append(usable_soc)
            if explain:
                entry = explanation_devices[i]
                entry.capacity_weight = usable_soc
                if cap and not cap.can_discharge and remaining > 0:
                    _set_limiting_reason(entry, "cannot_discharge")
                elif d.max_soc <= 0 and remaining > 0:
                    _set_limiting_reason(entry, "no_battery_available")
                elif usable_soc <= 0 and remaining > 0:
                    _set_limiting_reason(entry, "no_usable_battery")

            log_event(
                logging.DEBUG,
                "balance_weight",
                device=device_configs[i].name if device_configs else i,
                mode="battery_discharge",
                solar_w=d.solar,
                soc=d.soc,
                min_soc=d.min_soc,
                usable_battery=round(usable_soc, 3),
                pv_kwp=get_device_pv_kwp(dev_config),
                battery_kwh=get_device_battery_kwh(dev_config),
                can_export=cap.can_export if cap else True,
                can_discharge=cap.can_discharge if cap else True,
                weight=round(usable_soc, 3)
            )

        weight_total = sum(weights)

        if weight_total > 0:
            for i, d in enumerate(devices):

                share = weights[i] / weight_total

                targets[i] += remaining * share
        elif remaining > 0:
            if explain:
                add_limit(
                    "discharge_capacity",
                    True,
                    0,
                    "no usable discharge capacity was available"
                )
            log_event(
                logging.WARNING,
                "no_discharge_capacity",
                requested_total=new_total,
                exportable_solar_total=exportable_solar_total,
                unmet_w=round(remaining)
            )

    raw_targets = targets[:]
    if explain:
        for entry, target in zip(explanation_devices, raw_targets):
            entry.raw_target_w = target

    if final_target_limits or battery_topup_used:
        log_event(
            logging.DEBUG,
            "pv_first_target_limits",
            topup_used=battery_topup_used,
            final_target_limits=(
                json.dumps([round(limit) for limit in final_target_limits])
                if final_target_limits
                else "none"
            )
        )
        if explain and final_target_limits:
            add_limit(
                "pv_only_final_limits",
                True,
                json.dumps([round(limit) for limit in final_target_limits]),
                "final constraints preserve PV-only target limits"
            )

    targets, undistributed = apply_constraints_and_redistribute(
        targets,
        device_configs=device_configs,
        capabilities=capabilities,
        target_limits=final_target_limits
    )
    if explain:
        rounded_raw_targets = [round(target) for target in raw_targets]
        device_limit_active = False
        for i, (entry, target) in enumerate(zip(explanation_devices, targets)):
            dev_config = device_configs[i] if device_configs else None
            cap = capabilities[i] if capabilities else None
            max_device_power = get_device_max_power(dev_config)
            final_limit = (
                final_target_limits[i]
                if final_target_limits
                else max_device_power
            )

            entry.allocated_target_w = target
            entry.effective_target_w = target

            if cap and not cap.can_export:
                _set_limiting_reason(entry, "cannot_export")

            if rounded_raw_targets[i] > target:
                device_limit_active = True
                if final_target_limits and target <= round(final_limit):
                    _set_limiting_reason(entry, "pv_only_limit")
                elif target <= max_device_power:
                    _set_limiting_reason(entry, "max_output_limit")

            if new_total > 0 and target <= 0 and not entry.limiting_reason:
                _set_limiting_reason(entry, "no_allocation")

        add_limit(
            "device_output_limits",
            device_limit_active,
            json.dumps(targets),
            (
                "one or more device targets were clamped"
                if device_limit_active
                else "device targets fit within capability limits"
            )
        )
        add_limit(
            "undistributed_target",
            undistributed > 0,
            undistributed,
            (
                "some target could not be distributed to devices"
                if undistributed > 0
                else "requested target was distributed within device limits"
            )
        )
        if undistributed > 0:
            explanation_notes.append(
                f"{round(undistributed)} W could not be distributed"
            )

    log_event(
        logging.DEBUG,
        "target_calculation",
        load=load,
        current_total=current_total,
        requested_total=new_total,
        raw_targets=json.dumps([round(t) for t in raw_targets]),
        final_targets=json.dumps(targets),
        undistributed=undistributed
    )

    if explain:
        allocated_total = sum(targets)
        explanation = ControlExplanation(
            mode=mode,
            requested_total_w=new_total,
            effective_target_total_w=allocated_total,
            allocated_target_total_w=allocated_total,
            commanded_total_w=new_total,
            devices=explanation_device_map,
            limits=explanation_limits,
            notes=explanation_notes,
            current_total_w=current_total,
            raw_requested_total_w=raw_requested_total,
            max_total_power_w=max_power,
            load_w=load,
            undistributed_target_w=undistributed
        )
        return targets, current_total, new_total, explanation

    return targets, current_total, new_total

# =====================
# EMS CONTROLLER
# =====================
