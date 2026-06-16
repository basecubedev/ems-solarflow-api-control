# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic, non-secret synthetic data for the dashboard preview server.

Shared by ``scripts/serve_dashboard_preview.py`` and
``scripts/capture_dashboard_previews.py`` so the live preview and the screenshot
helper render identical synthetic state.

Everything here is hand-built constant data. No real Zendure/Shelly/MQTT/cloud
access, no secrets, no SQLite history, and no runtime-state files are touched.
"""

import time

SCENARIOS = (
    "normal",
    "firmware-status",
    "offline-device",
    "auth-readonly",
    "write-mode",
)
DEFAULT_SCENARIO = "normal"

# Flow views exposed by the dashboard frontend (dashboard/static/app.js).
FLOW_VIEWS = (
    "aggregated",
    "devices",
    "control",
    "energy",
    "diagnose",
    "logs",
)


def _timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


def _num(value):
    return float(value) if isinstance(value, (int, float)) else 0.0


def _device(name, **over):
    """Build one device payload with every firmware-status field populated.

    Field names and value conventions match the real dashboard telemetry
    payload (dashboard/telemetry.py) so the frontend renders them identically.
    """

    base = {
        "online": True,
        "enabled": True,
        "soc": 60,
        "min_soc": 15,
        "max_soc": 100,
        "pv_input_w": 600,
        "pv_inputs_w": [600, 0, 0, 0],
        "output_w": 400,
        "battery_power_w": 200,
        "pack_input_w": 0,
        "pack_output_w": 200,
        "target_w": 400,
        "allocated_target_w": 400,
        "output_limit_w": 400,
        "temperature_c": 24.5,
        "voltage_v": 52.4,
        "rssi": -58,
        "remain_minutes": 180,
        # Firmware status enums (see ZenSDK properties + dashboard helpers).
        "soc_limit": 0,
        "pack_state": 2,
        "fault_level": 0,
        "smart_mode": 0,
        "grid_off_mode": 0,
        "ac_mode": 2,
        "ac_status": 1,
        "dc_status": 2,
        "grid_state": 1,
        "soc_status": 0,
        "pack_num": 1,
        "input_limit_w": 0,
    }
    base.update(over)
    base["name"] = name
    return base


def _energy_stats():
    return {
        "enabled": True,
        "currency": "EUR",
        "price_per_kwh": 0.35,
        "today": {"inverter_output_kwh": 3.2, "savings_value": 1.12},
        "yesterday": {
            "inverter_output_kwh": 4.2,
            "savings_value": 1.47,
            "peak_output_w": 780,
        },
        "last_7_days": {"inverter_output_kwh": 18.4, "savings_value": 6.44},
        "last_4_weeks": {"inverter_output_kwh": 72.1, "savings_value": 25.24},
        "last_12_months": {"inverter_output_kwh": 520.0, "savings_value": 182.0},
        "best_day": {
            "date": "2026-06-14",
            "inverter_output_kwh": 8.4,
            "savings_value": 2.94,
        },
        "monthly_current_year": [
            {"month": 1, "label": "Jan", "inverter_output_kwh": 22.4, "savings_value": 7.84},
            {"month": 2, "label": "Feb", "inverter_output_kwh": 31.2, "savings_value": 10.92},
            {"month": 3, "label": "Mar", "inverter_output_kwh": 43.8, "savings_value": 15.33},
            {"month": 4, "label": "Apr", "inverter_output_kwh": 58.5, "savings_value": 20.48},
            {"month": 5, "label": "May", "inverter_output_kwh": 76.6, "savings_value": 26.81},
            {"month": 6, "label": "Jun", "inverter_output_kwh": 84.2, "savings_value": 29.47},
            {"month": 7, "label": "Jul", "inverter_output_kwh": 91.8, "savings_value": 32.13},
            {"month": 8, "label": "Aug", "inverter_output_kwh": 88.4, "savings_value": 30.94},
            {"month": 9, "label": "Sep", "inverter_output_kwh": 66.9, "savings_value": 23.42},
            {"month": 10, "label": "Oct", "inverter_output_kwh": 44.5, "savings_value": 15.58},
            {"month": 11, "label": "Nov", "inverter_output_kwh": 18.6, "savings_value": 6.51},
            {"month": 12, "label": "Dec", "inverter_output_kwh": 11.2, "savings_value": 3.92},
        ],
        "yearly": [
            {"year": 2025, "inverter_output_kwh": 320.0, "savings_value": 112.0},
            {"year": 2026, "inverter_output_kwh": 840.0, "savings_value": 294.0},
        ],
        "lifetime": {"inverter_output_kwh": 2070.0, "savings_value": 724.5},
    }


def _control_explain(devices, *, requested_total_w=800, max_total_power_w=800):
    device_explain = {}
    for name, dev in devices.items():
        target = _num(dev.get("target_w"))
        device_explain[name] = {
            "device": name,
            "online": bool(dev.get("online", True)),
            "pv_input_w": _num(dev.get("pv_input_w")),
            "output_w": _num(dev.get("output_w")),
            "output_limit_w": _num(dev.get("output_limit_w")),
            "soc": _num(dev.get("soc")),
            "min_soc": dev.get("min_soc", 15),
            "max_soc": dev.get("max_soc", 100),
            "max_output_w": 800,
            "pv_only_limit_w": _num(dev.get("pv_input_w")) - 20,
            "base_weight": 1,
            "effective_weight": 1.0,
            "pv_priority_factor": 1.0,
            "charge_balance_multiplier": 1.0,
            "raw_target_w": target,
            "allocated_target_w": _num(dev.get("allocated_target_w")),
            "effective_target_w": target,
            "adjustment_delta_w": 0,
            "decision_reason": "remaining_demand_assigned",
            "write_decision": "send" if dev.get("online", True) else "skip",
            "write_reason": "output_limit_update" if dev.get("online", True) else "ac_inactive",
            "deadband_reference_w": target,
            "command_target_w": target,
            "limiting_reason": "",
            "capability_reason": "pv_evidence",
        }
    return {
        "mode": "pv_first",
        "filtered_load_w": requested_total_w,
        "requested_total_w": requested_total_w,
        "effective_target_total_w": requested_total_w,
        "allocated_target_total_w": requested_total_w,
        "commanded_total_w": requested_total_w,
        "max_total_power_w": max_total_power_w,
        "min_output_limit_w": 0,
        "deadband_w": 5,
        "devices": device_explain,
        "limits": [
            {
                "name": "System output limit",
                "active": True,
                "value": f"{max_total_power_w} W",
                "reason": "output is capped at the configured system limit",
            }
        ],
        "notes": ["Preview mode uses synthetic, non-secret telemetry."],
    }


def _rules(devices):
    offline = [name for name, dev in devices.items() if not dev.get("online", True)]
    soc_limited = [name for name, dev in devices.items() if dev.get("soc_limit")]
    return {
        "ems_enabled": {"active": True, "reason": "preview control loop active"},
        "soc_limit_active": {
            "active": bool(soc_limited),
            "reason": ", ".join(soc_limited) if soc_limited else "no device reports a SOC limit",
        },
        "pv_priority_balancing": {
            "active": True,
            "reason": "per-device PV priority preview",
        },
        "battery_balancing": {
            "active": True,
            "reason": "devices share the configured system limit",
        },
        "offline_devices": {
            "active": bool(offline),
            "reason": ", ".join(offline) if offline else "all devices online",
        },
    }


def _snapshot(devices, *, grid_power_w=0, max_total_power_w=800):
    pv = sum(_num(d.get("pv_input_w")) for d in devices.values())
    out = sum(_num(d.get("output_w")) for d in devices.values())
    batt = sum(_num(d.get("battery_power_w")) for d in devices.values())
    socs = [
        _num(d.get("soc"))
        for d in devices.values()
        if isinstance(d.get("soc"), (int, float))
    ]
    average_soc = round(sum(socs) / len(socs), 1) if socs else 0
    home_load = max(0.0, out + grid_power_w)
    return {
        "timestamp": _timestamp(),
        "pv_total_w": round(pv, 1),
        "inverter_output_w": round(out, 1),
        "home_load_w": round(home_load, 1),
        "grid_power_w": grid_power_w,
        "battery_power_w": round(batt, 1),
        "average_soc": average_soc,
        "controller": {
            "enabled": True,
            "max_total_power_w": max_total_power_w,
            "min_output_limit_w": 0,
            "allocated_target_total_w": round(out, 1),
            "effective_target_total_w": round(out, 1),
            "commanded_total_w": round(out, 1),
            "filtered_load_w": round(home_load, 1),
            "night_min_soc_idle": False,
        },
        "rules": _rules(devices),
        "energy_stats": _energy_stats(),
        "control_explain": _control_explain(
            devices, requested_total_w=int(round(out)) or max_total_power_w,
            max_total_power_w=max_total_power_w,
        ),
        "devices": devices,
    }


def _devices_for(scenario):
    if scenario == "firmware-status":
        # Mixed firmware values across devices so every readable label plus the
        # unknown-value fallback can be checked at a glance.
        return {
            # AC output active / Normal / Discharging / DC output / grid up
            "WR1": _device(
                "WR1", soc=78, pv_input_w=1200, output_w=320, battery_power_w=860,
                pack_output_w=880, pack_input_w=20, target_w=320, output_limit_w=320,
                ac_mode=2, ac_status=1, soc_limit=0, pack_state=2, dc_status=2,
                grid_state=1, soc_status=0, pack_num=1, grid_off_mode=0,
            ),
            # AC charge active / Max-SoC reached / Charging / DC input / calibrating
            "WR2": _device(
                "WR2", soc=99, pv_input_w=0, output_w=0, battery_power_w=-280,
                pack_input_w=280, pack_output_w=0, target_w=0, output_limit_w=0,
                ac_mode=1, ac_status=2, soc_limit=1, pack_state=1, dc_status=1,
                grid_state=1, soc_status=1, pack_num=2, input_limit_w=300,
                grid_off_mode=1,
            ),
            # AC output standby / Min-SoC protection / Standby / DC standby / grid down
            "WR3": _device(
                "WR3", soc=15, pv_input_w=80, output_w=0, battery_power_w=0,
                pack_output_w=0, target_w=0, output_limit_w=0,
                ac_mode=2, ac_status=0, soc_limit=2, pack_state=0, dc_status=0,
                grid_state=0, soc_status=0, pack_num=1, grid_off_mode=2,
            ),
            # Unknown firmware values -> "Unknown ... (value X)"
            "WR4": _device(
                "WR4", soc=50, pv_input_w=300, output_w=150, battery_power_w=140,
                target_w=150, output_limit_w=150,
                ac_mode=9, ac_status=9, soc_limit=7, pack_state=5, dc_status=4,
                grid_state=3, soc_status=4, pack_num=1,
            ),
        }

    if scenario == "offline-device":
        return {
            "WR1": _device(
                "WR1", soc=64, pv_input_w=1100, output_w=480, battery_power_w=600,
                pack_output_w=620, pack_input_w=20, target_w=480, output_limit_w=480,
            ),
            # Offline / stale device with intentionally missing telemetry.
            "WR2": _device(
                "WR2", online=False, soc=None, pv_input_w=None, output_w=None,
                battery_power_w=None, pack_output_w=None, pack_input_w=None,
                target_w=0, output_limit_w=0, fault_level=2, ac_mode=2, ac_status=0,
                dc_status=0, grid_state=0, pack_state=0, remain_minutes=None,
                voltage_v=None, temperature_c=None,
            ),
        }

    # normal / auth-readonly / write-mode share a healthy two-device system.
    return {
        "WR1": _device(
            "WR1", soc=62, pv_input_w=1200, output_w=320, battery_power_w=880,
            pack_output_w=900, pack_input_w=20, target_w=320, output_limit_w=320,
        ),
        "WR2": _device(
            "WR2", soc=57, pv_input_w=650, output_w=480, battery_power_w=170,
            pack_output_w=200, pack_input_w=30, target_w=480, output_limit_w=480,
            ac_mode=2, ac_status=1, dc_status=2,
        ),
    }


def _runtime(devices):
    runtime_devices = {}
    for name in devices:
        runtime_devices[name] = {
            "enabled": True,
            "max_power": 800,
            "offgrid_socket_mode": "off",
            "pv_priority_factor": 1.0,
        }
    return {
        "system": {
            "enabled": True,
            "max_total_power": 800,
            "loop_interval": 5,
            "min_output_limit": 0,
        },
        "ha": {"enabled": False, "control_enabled": False},
        "winter": {"enabled": False},
        "devices": runtime_devices,
        "_limits": {
            "system": {"max_total_power": 5000, "min_output_limit": 5000},
            "devices": {name: 800 for name in devices},
            "fallback_device_max_power": 800,
        },
    }


def _history(snapshot, points=18):
    items = []
    base = snapshot
    for index in range(points):
        items.append(
            {
                **base,
                "timestamp": time.strftime(
                    "%Y-%m-%dT%H:%M:%S+00:00",
                    time.gmtime(time.time() - (points - 1 - index) * 300),
                ),
                "pv_total_w": round(1200 + index * 38, 1),
                "inverter_output_w": min(800, 520 + index * 18),
                "home_load_w": min(800, 540 + index * 16),
                "battery_power_w": round(720 + index * 18, 1),
                "average_soc": round(56 + index * 0.22, 2),
            }
        )
    return items


def _auth(scenario):
    if scenario == "write-mode":
        return {
            "auth_configured": True,
            "authenticated": True,
            "csrf_token": "preview-csrf-token",
            "write_mode_available": True,
            "write_mode_active": True,
        }
    if scenario == "auth-readonly":
        return {
            "auth_configured": True,
            "authenticated": False,
            "csrf_token": None,
            "write_mode_available": True,
            "write_mode_active": False,
        }
    return {
        "auth_configured": False,
        "authenticated": False,
        "csrf_token": None,
        "write_mode_available": False,
        "write_mode_active": False,
    }


def _diagnose(scenario):
    sections = [
        {"id": "environment", "title": "Environment", "status": "ok", "warnings": [], "errors": []},
        {"id": "config", "title": "Config", "status": "ok", "warnings": [], "errors": []},
        {
            "id": "hardware",
            "title": "Hardware",
            "status": "warning",
            "warnings": ["Zendure device WR2 read-only probe failed: TimeoutError"],
            "errors": [],
        },
        {
            "id": "dashboard",
            "title": "Dashboard",
            "status": "warning",
            "warnings": ["SQLite table snapshots latest row is older than 1 hour"],
            "errors": [],
        },
    ]
    warnings = [
        "Dashboard database has no recent energy rows.",
        "WR2 read-only probe returned a transient timeout.",
    ]
    errors = []
    status = "warning"
    root_causes = [
        {
            "code": "dashboard_data_gap",
            "severity": "warning",
            "title": "Dashboard data gap",
            "message": "The dashboard database is reachable, but recent telemetry rows are sparse.",
            "suggested_next_check": "Check the EMS loop and dashboard write interval.",
        }
    ]

    if scenario == "offline-device":
        status = "error"
        sections[2]["status"] = "error"
        sections[2]["errors"] = ["Zendure device WR2 is offline (no telemetry)."]
        errors = ["Zendure device WR2 is offline."]
        root_causes.insert(
            0,
            {
                "code": "device_offline",
                "severity": "error",
                "title": "Device offline",
                "message": "WR2 did not respond to the read-only telemetry probe.",
                "suggested_next_check": "Verify WR2 power, network, and local HTTP API access.",
            },
        )

    return {
        "schema_version": 1,
        "profile": "hardware",
        "status": status,
        "diagnosis": {
            "version": 1,
            "timestamp": _timestamp(),
            "status": status,
            "metrics": {"ok": 18, "warning": len(warnings), "error": len(errors), "devices": 2},
            "warnings": warnings,
            "errors": errors,
            "root_causes": root_causes,
            "sections": sections,
        },
    }


def _logs(scenario):
    lines = [
        (1, "INFO", "ems.startup event=startup dry_run=false simulation=false dashboard=true"),
        (2, "INFO", "dashboard_started host=127.0.0.1 port=8767 https=false"),
        (3, "INFO", "control_cycle filtered_load_w=792 target_total_w=800 commanded_total_w=800"),
        (4, "DEBUG", "allocation device=WR1 pv_input_w=1200 target_w=320 decision=deadband"),
        (5, "DEBUG", "allocation device=WR2 pv_input_w=650 target_w=480 decision=send"),
        (6, "WARNING", "diagnose hardware probe timeout device=WR2 endpoint=/properties/report"),
        (7, "INFO", "event=dashboard_log_level_changed level=DEBUG"),
        (8, "INFO", "runtime_state_saved path=data/runtime-state.json"),
        (9, "INFO", "control_cycle filtered_load_w=804 target_total_w=800 commanded_total_w=800"),
        (10, "INFO", "grid_meter_recovered type=shelly power_w=804"),
    ]
    if scenario == "offline-device":
        lines.append((11, "ERROR", "device_probe_failed device=WR2 reason=timeout endpoint=/properties/report"))
        lines.append((12, "WARNING", "device_marked_offline device=WR2 missed_cycles=4"))
    return lines


def build_scenario(scenario=DEFAULT_SCENARIO):
    """Return all synthetic preview payloads for one scenario."""

    if scenario not in SCENARIOS:
        raise ValueError(
            f"unknown scenario {scenario!r}; choose one of {', '.join(SCENARIOS)}"
        )

    devices = _devices_for(scenario)
    grid_power_w = 120 if scenario == "offline-device" else 0
    snapshot = _snapshot(devices, grid_power_w=grid_power_w)
    return {
        "name": scenario,
        "snapshot": snapshot,
        "runtime": _runtime(devices),
        "history": _history(snapshot),
        "auth": _auth(scenario),
        "diagnose": _diagnose(scenario),
        "logs": _logs(scenario),
    }
