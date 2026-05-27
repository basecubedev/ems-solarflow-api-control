from datetime import datetime, timezone


def _rounded(value, digits=1):
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return 0


def _bool_rule(active, reason=None):
    return {
        "active": bool(active),
        "reason": reason or ("active" if active else "inactive")
    }


def _device_runtime(controller, device_name, key, default):
    if not controller.runtime_state:
        return default

    return controller.runtime_state.get_device(device_name, key, default)


def build_dashboard_snapshot(
    controller,
    load_w,
    states,
    targets,
    effective_targets,
    allocated_total_w,
    effective_total_w,
    *,
    enabled,
    max_total_power,
    min_output_limit,
    night_min_soc_idle=False,
):
    """Build a read-only telemetry snapshot from current EMS runtime data."""

    now = datetime.now(timezone.utc).isoformat()
    devices = {}
    pv_total_w = 0
    inverter_total_w = 0
    battery_total_w = 0
    soc_values = []
    offline_devices = []

    capabilities = getattr(controller, "_dashboard_capabilities", None) or []

    for index, (dev, state) in enumerate(zip(controller.devices, states)):
        name = dev.name
        pv_w = _rounded(getattr(state, "solar", 0))
        output_w = _rounded(getattr(state, "output", 0))
        # End-user EMS convention: positive means charging, negative means
        # discharging. Zendure/controller field names use the opposite view,
        # so the API publishes pack_out - pack_in exactly once here.
        battery_power_w = _rounded(
            getattr(state, "pack_out", 0) - getattr(state, "pack_in", 0)
        )
        online = bool(controller.device_online.get(name, True))

        if not online:
            offline_devices.append(name)

        pv_total_w += pv_w
        inverter_total_w += output_w
        battery_total_w += battery_power_w
        soc_values.append(_rounded(getattr(state, "soc", 0)))

        capability = capabilities[index] if index < len(capabilities) else None
        target_w = effective_targets[index] if index < len(effective_targets) else 0
        allocated_target_w = targets[index] if index < len(targets) else 0

        devices[name] = {
            "online": online,
            "enabled": bool(_device_runtime(controller, name, "enabled", True)),
            "soc": _rounded(getattr(state, "soc", 0)),
            "min_soc": _rounded(getattr(state, "min_soc", 0)),
            "max_soc": _rounded(getattr(state, "max_soc", 0)),
            "pv_input_w": pv_w,
            "pv_inputs_w": [
                _rounded(getattr(state, field, 0))
                for field in ("solar1", "solar2", "solar3", "solar4")
            ],
            "output_w": output_w,
            "battery_power_w": battery_power_w,
            "pack_input_w": _rounded(getattr(state, "pack_in", 0)),
            "pack_output_w": _rounded(getattr(state, "pack_out", 0)),
            "target_w": _rounded(target_w),
            "allocated_target_w": _rounded(allocated_target_w),
            "output_limit_w": _rounded(getattr(state, "output_limit", 0)),
            "soc_limit": int(getattr(state, "soc_limit", 0) or 0),
            "pack_state": int(getattr(state, "pack_state", 0) or 0),
            "fault_level": int(getattr(state, "fault_level", 0) or 0),
            "temperature_c": _rounded(getattr(state, "temp", 0)),
            "voltage_v": _rounded(getattr(state, "voltage", 0)),
            "rssi": int(getattr(state, "rssi", 0) or 0),
            "remain_minutes": _rounded(getattr(state, "remain_minutes", 0)),
            "smart_mode": int(getattr(state, "smart_mode", 0) or 0),
            "grid_off_mode": int(getattr(state, "grid_off_mode", 0) or 0),
            "ac_mode": int(getattr(state, "ac_mode", 0) or 0),
            "ac_status": int(getattr(state, "ac_status", 0) or 0),
            "dc_status": int(getattr(state, "dc_status", 0) or 0),
            "grid_state": int(getattr(state, "grid_state", 0) or 0),
            "capability": (
                {
                    "can_charge": capability.can_charge,
                    "can_discharge": capability.can_discharge,
                    "can_export": capability.can_export,
                    "can_ac_charge": capability.can_ac_charge,
                    "reason": capability.reason,
                }
                if capability
                else None
            ),
        }

    grid_power_w = _rounded(load_w)
    home_load_w = _rounded(max(0, inverter_total_w + grid_power_w))
    average_soc = _rounded(sum(soc_values) / len(soc_values)) if soc_values else 0

    winter_active = False
    try:
        from ems import config as cfg

        winter_active = cfg.winter_mode_active(
            datetime.now(),
            controller.runtime_state
        )
    except Exception:
        winter_active = False

    rule_states = {
        "ems_enabled": _bool_rule(enabled),
        "soc_limit_active": _bool_rule(
            any(device["soc_limit"] for device in devices.values()),
            "one or more devices report a SOC limit"
        ),
        "output_limit_active": _bool_rule(
            any(device["output_limit_w"] > 0 for device in devices.values()),
            "device outputLimit is present"
        ),
        "winter_soc_mode": _bool_rule(
            winter_active,
            "configured winter month and runtime winter toggle"
        ),
        "pv_priority_balancing": _bool_rule(
            any(
                float(_device_runtime(controller, name, "pv_priority_factor", 1.0))
                != 1.0
                for name in devices
            ),
            "per-device PV priority factor differs from 1.0"
        ),
        "battery_balancing": _bool_rule(
            any(
                abs(device["target_w"] - device["allocated_target_w"]) > 0
                for device in devices.values()
            ),
            "effective device target differs from allocated target"
        ),
        "night_min_soc_idle": _bool_rule(
            night_min_soc_idle,
            "all controllable devices are parked at the minimum output floor"
        ),
        "offline_devices": _bool_rule(
            bool(offline_devices),
            ", ".join(offline_devices) if offline_devices else "all devices online"
        ),
    }

    return {
        "timestamp": now,
        "devices": devices,
        "grid_power_w": grid_power_w,
        "home_load_w": home_load_w,
        "pv_total_w": _rounded(pv_total_w),
        "inverter_output_w": _rounded(inverter_total_w),
        "battery_power_w": _rounded(battery_total_w),
        "average_soc": average_soc,
        "controller": {
            "enabled": bool(enabled),
            "max_total_power_w": _rounded(max_total_power),
            "min_output_limit_w": _rounded(min_output_limit),
            "allocated_target_total_w": _rounded(allocated_total_w),
            "effective_target_total_w": _rounded(effective_total_w),
            "commanded_total_w": _rounded(controller.commanded_total_w),
            "filtered_load_w": _rounded(controller.filtered_load_w),
            "night_min_soc_idle": bool(night_min_soc_idle),
        },
        "rules": rule_states,
    }
