# SPDX-License-Identifier: AGPL-3.0-or-later
import logging
from datetime import datetime, timezone
from dataclasses import asdict, is_dataclass

from ems.state_store import describe_full_charge_assist_status


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


def _control_explain_payload(controller):
    explanation = getattr(controller, "last_control_explanation", None)

    if explanation is None:
        return None

    if hasattr(explanation, "to_dict"):
        return explanation.to_dict()

    if is_dataclass(explanation):
        return asdict(explanation)

    if isinstance(explanation, dict):
        return explanation

    return None


def _battery_full_charge_assist_payload(controller, dev, state, now):
    """Build the normalized battery_full_charge_assist status for one device.

    Falls back to a minimal "unknown" payload if the controller does not
    expose the full-charge assist feature (older deployments, test doubles),
    so the dashboard API and GUI never crash on missing assist state.
    """

    unknown = {
        "enabled": False,
        "has_battery": False,
        "status": "unknown",
        "inside_assist_window": False,
        "days_until_due": None,
        "assist_window_days": 0,
        "last_full_charge_at": None,
        "next_due_at": None,
        "window_starts_at": None,
        "assist_started_at": None,
        "restore_pending": False,
        "ac_mode_restore_pending": False,
        "soc_limit": 0,
        "ac_mode": 0,
        "ac_status": 0,
        "message": "Full-charge assist state unavailable",
    }

    try:
        config = controller.full_charge_assist_config()
        enabled = controller.full_charge_assist_enabled()
        has_battery = bool(controller.full_charge_assist_has_battery(dev, state))
        store = getattr(controller, "battery_full_charge_store", None)
        record = store.get_device_state(dev.name) if store else None
    except Exception:
        # Keep the dashboard stable on legacy deployments / test doubles that
        # do not expose the assist feature, but record the real cause at DEBUG
        # so genuine failures don't vanish silently behind "state unavailable".
        logging.debug(
            "event=dashboard_assist_payload_unavailable device=%s",
            getattr(dev, "name", "?"),
            exc_info=True,
        )
        return unknown

    derived = describe_full_charge_assist_status(
        config, enabled, has_battery, record, now
    )

    return {
        "enabled": enabled,
        "has_battery": has_battery,
        **derived,
        "assist_window_days": int(config.get("assist_window_days") or 0),
        "last_full_charge_at": record.get("last_full_charge_at") if record else None,
        "next_due_at": record.get("next_due_at") if record else None,
        "assist_started_at": record.get("assist_started_at") if record else None,
        "restore_pending": bool(record.get("restore_pending")) if record else False,
        "ac_mode_restore_pending": (
            bool(record.get("ac_mode_restore_pending")) if record else False
        ),
        "soc_limit": int(getattr(state, "soc_limit", 0) or 0),
        "ac_mode": int(getattr(state, "ac_mode", 0) or 0),
        "ac_status": int(getattr(state, "ac_status", 0) or 0),
    }


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

    now_dt = datetime.now().astimezone()
    now = now_dt.astimezone(timezone.utc).isoformat()
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
            "battery_full_charge_assist": _battery_full_charge_assist_payload(
                controller, dev, state, now_dt
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

    full_charge_assist_active_devices = [
        name
        for name, device in devices.items()
        if device["battery_full_charge_assist"]["status"] == "active"
    ]

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
        "full_charge_assist_active": _bool_rule(
            bool(full_charge_assist_active_devices),
            ", ".join(full_charge_assist_active_devices)
            if full_charge_assist_active_devices
            else "no device currently in full-charge assist"
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
        "control_explain": _control_explain_payload(controller),
    }
