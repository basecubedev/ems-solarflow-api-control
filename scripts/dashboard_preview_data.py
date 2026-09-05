# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic, non-secret synthetic data for the dashboard preview server.

Shared by ``scripts/serve_dashboard_preview.py`` and
``scripts/capture_dashboard_previews.py`` so the live preview and the screenshot
helper render identical synthetic state.

Everything here is hand-built constant data. No real Zendure/Shelly/MQTT/cloud
access, no secrets, no SQLite history, and no runtime-state files are touched.
"""

import math
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
    "analytics",
    "control",
    "energy",
    "diagnose",
    "logs",
    "maintenance",
)


FROZEN_TIMESTAMP = "2026-06-17T12:00:00+00:00"


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


def _lerp_curve(hour, points):
    """Piecewise-linear interpolation over sorted ``(hour, value)`` control
    points, clamped at both ends. Deterministic helper for the SoC profile."""
    if hour <= points[0][0]:
        return points[0][1]
    if hour >= points[-1][0]:
        return points[-1][1]
    for i in range(1, len(points)):
        h0, v0 = points[i - 1]
        h1, v1 = points[i]
        if hour <= h1:
            t = (hour - h0) / (h1 - h0) if h1 > h0 else 0.0
            return v0 + (v1 - v0) * t
    return points[-1][1]


def _history(snapshot, points=96):
    """Synthetic but lively ~24h history (15-min steps) for the preview charts.

    Models how the EMS keeps the **grid meter** near zero: while a source is
    available (PV by day, or a charged battery), it balances the house load via
    solar/battery so the grid line just wobbles around ~-5 W. Only when **both**
    PV is absent **and** the battery is empty (a pre-dawn window here, after the
    overnight discharge drains the pack) does the grid rise up to the full home
    load -- because no energy source is feeding in, the meter must cover
    everything. Battery, inverter output and SoC follow the same balance, and the
    grid series is derived from it (grid = home - pv + battery). Fully
    deterministic (no RNG) so the live preview and screenshots stay reproducible.
    """

    # SoC drains overnight (battery serving the house), bottoms out empty in the
    # pre-dawn hours, then recharges from the PV surplus and plateaus midday.
    soc_curve = [
        (0.0, 30.0), (2.5, 12.0), (4.0, 5.0), (5.5, 5.0), (8.0, 45.0),
        (12.0, 84.0), (15.5, 96.0), (20.0, 84.0), (24.0, 32.0),
    ]

    items = []
    step = 900  # 15 minutes
    span = points * step
    now = time.time()
    for index in range(points):
        frac = index / (points - 1) if points > 1 else 0.0
        hour = frac * 24.0
        # PV: bell over daylight (~05:30-20:30) with two ripple frequencies to
        # mimic passing clouds.
        daylight = max(0.0, math.sin(math.pi * min(1.0, max(0.0, (hour - 5.5) / 15.0))))
        clouds = 1.0 - 0.30 * max(0.0, math.sin(index * 0.8)) - 0.14 * max(0.0, math.sin(index * 2.3))
        pv = max(0.0, 2100.0 * daylight * clouds)
        # Home load: fairly steady base + morning(~07:30) and evening(~19:00)
        # bumps + a gentle ripple so the "home level" reads as a clear line.
        morning = math.exp(-((hour - 7.5) ** 2) / 3.0)
        evening = math.exp(-((hour - 19.0) ** 2) / 5.0)
        home = 480.0 + 180.0 * morning + 240.0 * evening + 45.0 * math.sin(index * 0.7)
        home = max(360.0, home)
        # SoC profile (independent design curve, qualitatively tracks the flows).
        soc = max(5.0, min(100.0, _lerp_curve(hour, soc_curve) + 1.5 * math.sin(index * 0.5)))

        # A source is "available" when PV is producing or the battery still holds
        # charge. With no source the meter must carry the whole house.
        source_available = pv > 30.0 or soc > 10.0
        if source_available:
            # EMS balances PV + battery to the load: the grid meter only wobbles
            # around ~-5 W (between roughly -10 and +10 W).
            grid = -2.0 + 5.0 * math.sin(index * 0.7) + 3.0 * math.sin(index * 1.9 + 0.6)
            grid = max(-10.0, min(10.0, grid))
        else:
            # No PV and an empty battery: the grid rises to the full home load
            # (PV, if any, shaves a little off it).
            grid = home - pv
        # Battery follows from the same energy balance (positive == charging).
        battery = grid - home + pv
        # Inverter output = house load served locally; drops to 0 when the meter
        # is covering everything (grid == home), capped at the system limit.
        output = max(0.0, min(800.0, home - max(0.0, grid)))
        items.append(
            {
                **snapshot,
                "timestamp": time.strftime(
                    "%Y-%m-%dT%H:%M:%S+00:00",
                    time.gmtime(now - (span - index * step)),
                ),
                "pv_total_w": round(pv, 1),
                "inverter_output_w": round(output, 1),
                "home_load_w": round(home, 1),
                "battery_power_w": round(battery, 1),
                "grid_power_w": round(grid, 1),
                "average_soc": round(soc, 2),
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


def scale_devices(devices, count):
    """Return exactly ``count`` devices, cycling the scenario's own shapes.

    Benchmarks need a device count as an independent variable; the per-device
    render cost is what scales. Values are nudged per copy so no two devices are
    byte-identical, which would let a renderer dedupe work a real install has.
    """

    if count is None or count <= 0:
        return devices
    names = list(devices)
    if not names:
        return devices

    scaled = {}
    for index in range(count):
        source = devices[names[index % len(names)]]
        name = f"WR{index + 1}"
        copy = dict(source)
        copy["name"] = name
        for field, step in (("soc", 3), ("pv_input_w", 40), ("output_w", 15)):
            if isinstance(copy.get(field), (int, float)):
                copy[field] = copy[field] + step * index
        scaled[name] = copy
    return scaled


def build_scenario(scenario=DEFAULT_SCENARIO, device_count=None, freeze_timestamp=False):
    """Return all synthetic preview payloads for one scenario."""

    if scenario not in SCENARIOS:
        raise ValueError(
            f"unknown scenario {scenario!r}; choose one of {', '.join(SCENARIOS)}"
        )

    devices = scale_devices(_devices_for(scenario), device_count)
    # Grid is held very close to zero (-5 W) to show the EMS regulating tightly
    # against the house load; an offline device breaks that balance.
    grid_power_w = 120 if scenario == "offline-device" else -5
    snapshot = _snapshot(devices, grid_power_w=grid_power_w)
    # An unchanging timestamp is the A/B partner for the de-duplication work:
    # the server keeps sending, and a correct client stops re-rendering.
    if freeze_timestamp:
        snapshot["timestamp"] = FROZEN_TIMESTAMP
    return {
        "name": scenario,
        "snapshot": snapshot,
        "runtime": _runtime(devices),
        "history": _history(snapshot),
        "auth": _auth(scenario),
        "diagnose": _diagnose(scenario),
        "logs": _logs(scenario),
    }
