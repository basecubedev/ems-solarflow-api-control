import json
import logging

from ems import config as cfg
from ems.logging_utils import log_event
from ems.models import DeviceCapabilities


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

    if state.soc_limit == 1 or state.soc >= state.max_soc:
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


def apply_battery_topup_after_pv_first(
    targets,
    states,
    device_configs,
    capabilities,
    requested_total
):
    """Top up PV-first targets with battery power where safely available."""

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
        return targets

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

    return targets


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
    requested_total=None
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
        return [], 0, 0

    current_total = sum(d.output for d in devices)
    solar_total = sum(d.solar for d in devices)

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
            pv_weight = pv_first_weight(pv_only, dev_config)
            pv_weights.append(pv_weight)

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

        pv_only_total = sum(pv_only_limits)

        if pv_only_total > 0:
            targets = weighted_limited_allocation(
                new_total,
                pv_weights,
                pv_only_limits
            )

            pv_unmet = new_total - sum(targets)

            if pv_unmet > 0:
                log_event(
                    logging.WARNING,
                    "pv_first_limited",
                    requested_total=new_total,
                    pv_only_total=round(pv_only_total),
                    unmet_w=round(pv_unmet)
                )

            targets = apply_battery_topup_after_pv_first(
                targets,
                devices,
                device_configs,
                capabilities,
                new_total
            )

        else:

            targets = [0] * len(devices)

            if new_total > 0:
                log_event(
                    logging.WARNING,
                    "pv_first_limited",
                    requested_total=new_total,
                    pv_only_total=0,
                    unmet_w=round(new_total)
                )

                targets = apply_battery_topup_after_pv_first(
                    targets,
                    devices,
                    device_configs,
                    capabilities,
                    new_total
                )

    # =====================
    # CASE 2:
    # Battery discharge required
    # =====================

    else:

        targets = []

        for i, d in enumerate(devices):
            cap = capabilities[i] if capabilities else None
            targets.append(
                d.solar
                if not cap or cap.can_export
                else 0
            )

        exportable_solar_total = sum(targets)
        remaining = max(0, new_total - exportable_solar_total)

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
            log_event(
                logging.WARNING,
                "no_discharge_capacity",
                requested_total=new_total,
                exportable_solar_total=exportable_solar_total,
                unmet_w=round(remaining)
            )

    raw_targets = targets[:]

    targets, undistributed = apply_constraints_and_redistribute(
        targets,
        device_configs=device_configs,
        capabilities=capabilities
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

    return targets, current_total, new_total

# =====================
# EMS CONTROLLER
# =====================
