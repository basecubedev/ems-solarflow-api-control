# SPDX-License-Identifier: AGPL-3.0-or-later
"""Central catalog for EMS configuration templates and user-facing metadata."""

import copy
import json
import re

from ems.zendure_mqtt.topics import (
    FAMILY_LEGACY_JSON,
    FAMILY_ZENDURE_CLOUD_SCALAR,
    FAMILY_ZENSDK_HA_SCALAR,
)


LEVELS = {"normal", "advanced", "expert", "deprecated", "internal"}
SCOPES = {"setup", "maintenance", "both", "hidden"}
SETUP_GROUPS = (
    {
        "id": "hardware",
        "title": "Hardware",
        "description": "Core building blocks EMS reads and controls.",
        "order": 1,
    },
    {
        "id": "features",
        "title": "Features",
        "description": "Optional behaviors you can turn on when you need them.",
        "order": 2,
    },
    {
        "id": "advanced",
        "title": "Advanced / System settings",
        "description": "System-level and expert tuning. Most users can keep the defaults.",
        "order": 3,
    },
)
RISKS = {
    "none",
    "restart_required",
    "control_stability",
    "data_loss",
    "secret",
    "deprecated",
}


def _field(
    path,
    label,
    description,
    field_type,
    *,
    level="normal",
    scope="both",
    unit=None,
    minimum=None,
    maximum=None,
    options=None,
    required=False,
    requires_restart=True,
    restart_required=None,
    backup_recommended=True,
    risk="restart_required",
    group=None,
    editable=True,
):
    if restart_required is not None:
        requires_restart = restart_required
    risk = {
        "service_restart": "restart_required",
        "secrets": "secret",
        "storage": "data_loss",
    }.get(risk, risk)
    item = {
        "path": path,
        "label": label,
        "description": description,
        "type": field_type,
        "level": level,
        "scope": scope,
        "required": required,
        "requires_restart": requires_restart,
        "restart_required": requires_restart,
        "backup_recommended": backup_recommended,
        "risk": risk,
    }
    if unit is not None:
        item["unit"] = unit
    if minimum is not None:
        item["min"] = minimum
    if maximum is not None:
        item["max"] = maximum
    if options is not None:
        item["options"] = list(options)
    if group is not None:
        item["group"] = group
    if not editable:
        item["editable"] = False
    return item


def _section(
    section_id,
    path,
    title,
    summary,
    description,
    order,
    fields,
    *,
    level="normal",
    scope="both",
    enabled_path=None,
    collapsible=True,
    groups=None,
    setup_group="features",
    kind="feature",
):
    item = {
        "id": section_id,
        "path": path,
        "title": title,
        "summary": summary,
        "description": description,
        "level": level,
        "scope": scope,
        "order": order,
        "collapsible": collapsible,
        "setup_group": setup_group,
        "kind": kind,
        "fields": fields,
    }
    if enabled_path is not None:
        item["enabled_path"] = enabled_path
    if groups is not None:
        item["groups"] = groups
    return item


def _group(
    group_id,
    path,
    title,
    summary,
    description,
    order,
    *,
    level,
    risk,
    scope="both",
):
    risk = {
        "service_restart": "restart_required",
        "secrets": "secret",
        "storage": "data_loss",
    }.get(risk, risk)
    return {
        "id": group_id,
        "path": path,
        "title": title,
        "summary": summary,
        "description": description,
        "level": level,
        "scope": scope,
        "order": order,
        "risk": risk,
        "collapsible": True,
    }


_OUTPUT_CONTROL = {
    "group": "output_control",
    "level": "expert",
    "risk": "control_stability",
}

GRID_METER_VARIANTS = {
    "shelly": {
        "label": "Shelly Pro/Plus Gen2/Gen3",
        "description": "Read power from a local Shelly Pro/Plus meter.",
        "fields": ("grid_meter.ip", "grid_meter.channels"),
        "manual_setup": True,
        "default_port": 80,
        "default_manual": True,
    },
    "shelly_3em_gen1": {
        "label": "Shelly 3EM Gen1",
        "description": "Read power from the older Shelly 3EM /status endpoint.",
        "fields": ("grid_meter.ip", "grid_meter.channels"),
        "manual_setup": True,
        "default_port": 80,
    },
    "ecotracker": {
        "label": "everHome EcoTracker",
        "description": "Read power from an EcoTracker local API.",
        "fields": ("grid_meter.ip",),
        "manual_setup": True,
        "default_port": 80,
    },
    "zendure_grid_meter_http": {
        "label": "Zendure Grid Meter via local HTTP",
        "description": "Read grid power from a Zendure D0 or Smart Meter 3CT local "
        "REST endpoint. Both serve a flat total_power at /properties/report. "
        "Internal/discovery generic type; manual setup offers the concrete 3CT "
        "and D0 local-API entries instead.",
        "fields": ("grid_meter.ip",),
        "default_port": 80,
    },
    "zendure_smartmeter_3ct_http": {
        "label": "Zendure Smart Meter 3CT — Local API",
        "description": "Read grid power from a Zendure Smart Meter 3CT over its "
        "local HTTP endpoint (flat total_power at /properties/report). Shares the "
        "Zendure local-HTTP reader with the D0 local-API meter.",
        "fields": ("grid_meter.ip",),
        "manual_setup": True,
        "default_port": 80,
    },
    "zendure_smartmeter_d0_http": {
        "label": "Zendure Smart Meter D0 — Local API",
        "description": "Read grid power from a Zendure Smart Meter D0 over its "
        "local HTTP endpoint (flat total_power at /properties/report). Shares the "
        "Zendure local-HTTP reader with the 3CT local-API meter; distinct config "
        "type so a D0 is never labelled or stored as a 3CT.",
        "fields": ("grid_meter.ip",),
        "manual_setup": True,
        "default_port": 80,
    },
    "tasmota_http": {
        "label": "Tasmota HTTP / SmartMeter reader",
        "description": "Read power from a Tasmota HTTP JSON endpoint.",
        "fields": ("grid_meter.ip", "grid_meter.url", "grid_meter.power_path"),
        "manual_setup": True,
        "default_port": 80,
    },
    "zendure_smartmeter_d0": {
        "label": "Zendure SmartMeter D0 via MQTT",
        "description": "Read grid power from a Zendure D0 MQTT topic. The totalPower "
        "topic is generated from the device serial number.",
        "fields": (
            "grid_meter.mqtt.host",
            "grid_meter.mqtt.port",
            "grid_meter.mqtt.username",
            "grid_meter.mqtt.password",
            "grid_meter.mqtt.topic",
            "grid_meter.mqtt.payload_format",
            "grid_meter.mqtt.max_age_seconds",
        ),
    },
    "mqtt": {
        "label": "Generic MQTT grid meter",
        "description": "Read grid power from a custom MQTT topic.",
        "fields": (
            "grid_meter.mqtt.host",
            "grid_meter.mqtt.port",
            "grid_meter.mqtt.username",
            "grid_meter.mqtt.password",
            "grid_meter.mqtt.topic",
            "grid_meter.mqtt.payload_format",
            "grid_meter.mqtt.value_path",
            "grid_meter.mqtt.max_age_seconds",
        ),
    },
    "ha": {
        "label": "Home Assistant grid meter",
        "description": "Legacy compatibility only; not recommended for new Admin setups.",
        "fields": (),
        "level": "deprecated",
        "scope": "maintenance",
        "risk": "deprecated",
    },
}

# Grid-meter keys every HTTP variant shares. They are not variant-specific, so a
# switch between two HTTP variants keeps them; a switch to an MQTT variant drops
# them (an MQTT meter carries none of them).
_SHARED_HTTP_GRID_METER_KEYS = ("ip", "port")


def grid_meter_variant_field_spec(grid_type):
    """Grid-meter config keys a variant may legitimately carry.

    Returns ``{"keys": frozenset(...), "mqtt_keys": frozenset(...)}`` for a known
    grid-meter ``type``, or ``None`` for an unknown one. ``keys`` are top-level
    ``grid_meter.*`` keys (``"mqtt"`` is the nested block); ``mqtt_keys`` are the
    allowed keys inside ``grid_meter.mqtt``. This is the single EMS-owned source
    of truth the Admin preview cleanup consumes, derived from
    ``GRID_METER_VARIANTS`` so it cannot drift from the catalog.
    """

    variant = GRID_METER_VARIANTS.get(grid_type)
    if variant is None:
        return None
    top = {"type"}
    mqtt_keys = set()
    for path in variant.get("fields", ()):
        if not path.startswith("grid_meter."):
            continue
        rest = path[len("grid_meter."):]
        head, _, tail = rest.partition(".")
        if head == "mqtt":
            top.add("mqtt")
            if tail:
                mqtt_keys.add(tail)
        elif head:
            top.add(head)
    if "mqtt" not in top:
        top.update(_SHARED_HTTP_GRID_METER_KEYS)
    return {"keys": frozenset(top), "mqtt_keys": frozenset(mqtt_keys)}


def _all_grid_meter_variant_keys():
    keys = set()
    mqtt_keys = set()
    for grid_type in GRID_METER_VARIANTS:
        spec = grid_meter_variant_field_spec(grid_type)
        keys |= spec["keys"]
        mqtt_keys |= spec["mqtt_keys"]
    keys.discard("type")  # the type key itself is never a variant field to strip
    return frozenset(keys), frozenset(mqtt_keys)


# Union of all keys any grid-meter variant may introduce. Only keys in these sets
# are eligible for removal during a variant switch, so operator-defined custom
# keys unrelated to any known variant survive untouched.
GRID_METER_KNOWN_TOP_KEYS, GRID_METER_KNOWN_MQTT_KEYS = _all_grid_meter_variant_keys()


def grid_meter_types():
    """Every valid grid-meter ``type`` the catalog knows about.

    The single source Admin (setup preview and maintenance) consumes so a new
    variant becomes selectable/validatable everywhere by editing the catalog
    only, never a duplicated Admin-side list.
    """

    return frozenset(GRID_METER_VARIANTS)


def http_grid_meter_types():
    """Grid-meter types that carry a local HTTP endpoint (require an ip/host).

    Derived from each variant's declared ``grid_meter.ip`` field, so the set
    stays in lockstep with the catalog. Excludes MQTT meters (D0 MQTT, generic
    MQTT) and the fieldless deprecated HA meter, which carry no HTTP endpoint.
    """

    return frozenset(
        grid_type
        for grid_type, variant in GRID_METER_VARIANTS.items()
        if "grid_meter.ip" in variant.get("fields", ())
    )


INVERTER_CONNECTION_VARIANTS = {
    "zendure_local_api": {
        "label": "Zendure Local API",
        "description": "Zendure SolarFlow devices reachable through the local HTTP API.",
        "default_port": 80,
        "required_fields": ("host", "port", "serial"),
        "manual_setup": True,
        "default_manual": True,
    },
}

# User-facing Zendure hardware generations for manual MQTT device setup. The UI
# only ever shows label/description; the internal ``topic_family`` and
# ``base_topic`` stay out of the UI copy. The advanced ``legacy_zendure_json_alt``
# family is deliberately absent — discovery assigns it when it sees leading-slash
# topics, but it is never a manual choice.
ZENDURE_MQTT_GENERATIONS = {
    "solarflow_zensdk": {
        "label": "New SolarFlow / ZenSDK generation",
        "description": (
            "For SolarFlow 800, 800 Pro, 800 Pro 2, 800 Plus, SolarFlow 1600 AC+, "
            "SolarFlow 2400 AC / AC+ / Pro and SolarFlow 4000 AC+."
        ),
        "topic_family": FAMILY_ZENSDK_HA_SCALAR,
        "base_topic": "Zendure",
        "product_key": False,
        "default": True,
        # Product-model tokens identifying this generation. "solarflow" is the
        # family brand and also prefixes legacy hub names, so the legacy tokens
        # below must be checked first (see _MODEL_MATCH_ORDER in the Admin
        # draft mapping).
        "model_keywords": ("solarflow",),
    },
    "hub_hyper_legacy": {
        "label": "Older Zendure Hub / Hyper generation",
        "description": (
            "For Hub 1200, Hub 2000, Hyper 2000, AIO 2400, ACE 1500 and "
            "SuperBase V devices."
        ),
        "topic_family": FAMILY_LEGACY_JSON,
        "base_topic": "iot",
        "product_key": True,
        "default": False,
        "model_keywords": ("hub", "hyper", "aio", "ace", "superbase"),
    },
    "zendure_cloud": {
        "label": "Zendure Cloud MQTT",
        "description": "For read-only telemetry from the Zendure online MQTT broker.",
        "topic_family": FAMILY_ZENDURE_CLOUD_SCALAR,
        "base_topic": None,
        "product_key": False,
        "default": False,
        # A connection kind, not a hardware line: never matched by model name.
        "model_keywords": (),
    },
}

# Compact manual Zendure MQTT broker fields for the setup wizard. A broker reads
# telemetry; output control is enabled per device when the topic family has a
# verified write method. The password is a secret and carries no default so it is
# never surfaced or persisted.
ZENDURE_MQTT_BROKER_HELP = (
    "Use this when your Zendure devices publish telemetry to a local or cloud "
    "MQTT broker. EMS reads telemetry here, and can control supported inverters "
    "over MQTT using the same control loop as the local API."
)
ZENDURE_MQTT_BROKER_FIELDS = (
    {
        "key": "name",
        "label": "Broker name",
        "description": "Optional label for this broker profile.",
        "type": "text",
        "required": False,
        "default": "local_mqtt",
    },
    {
        "key": "host",
        "label": "Host / IP address",
        "description": "Hostname or IP of the MQTT broker carrying Zendure telemetry.",
        "type": "text",
        "required": True,
    },
    {
        "key": "port",
        "label": "Port",
        "description": "TCP port of the MQTT broker.",
        "type": "number",
        "required": True,
        "default": 1883,
    },
    {
        "key": "security",
        "label": "Connection security",
        "description": "Choose plain MQTT or MQTT over TLS.",
        "type": "select",
        "required": False,
        "default": "plain",
        "options": ("plain", "tls"),
    },
    {
        "key": "username",
        "label": "Username",
        "description": "Optional broker username.",
        "type": "text",
        "required": False,
    },
    {
        "key": "password",
        "label": "Password",
        "description": "Optional broker password.",
        "type": "password",
        "required": False,
        "risk": "secret",
    },
)

LEGACY_CONFIG_PATHS = {
    "shelly.ip": {
        "replacement": "grid_meter.ip",
        "description": "Old grid meter path migrated to grid_meter.",
    },
    "grid_meter.host": {"replacement": "grid_meter.mqtt.host"},
    "grid_meter.port": {"replacement": "grid_meter.mqtt.port"},
    "grid_meter.username": {"replacement": "grid_meter.mqtt.username"},
    "grid_meter.password": {"replacement": "grid_meter.mqtt.password"},
    "grid_meter.topic": {"replacement": "grid_meter.mqtt.topic"},
    "grid_meter.payload_format": {
        "replacement": "grid_meter.mqtt.payload_format"
    },
    "grid_meter.value_path": {"replacement": "grid_meter.mqtt.value_path"},
    "grid_meter.max_age_seconds": {
        "replacement": "grid_meter.mqtt.max_age_seconds"
    },
    "/app/config.template.json": {
        "status": "runtime_compatibility",
        "replacement": "config/config.template.json",
    },
    "config.template.json": {
        "status": "legacy_release_archive",
        "replacement": "config/config.template.json",
    },
}

RUNTIME_DEVICE_FIELDS = {
    "devices[].enabled": {"type": "boolean"},
    "devices[].offgrid_socket_mode": {"type": "integer"},
    "devices[].ac_charge_power_w": {"type": "integer", "unit": "W"},
    "devices[].grid_off_mode": {"type": "integer"},
}


_SECTIONS = [
    _section(
        "system",
        "system",
        "System basics",
        "Basic EMS runtime behavior.",
        "Controls whether EMS is allowed to run, write to hardware, and how often the control loop updates.",
        1,
        [
            _field(
                "system.enabled",
                "EMS control enabled",
                "Turns EMS live control on or off. Disable this when you want EMS installed without actively controlling devices.",
                "boolean",
            ),
            _field(
                "system.dry_run",
                "Dry run",
                "Runs the control logic without sending hardware writes. Useful for checking a setup safely.",
                "boolean",
                level="advanced",
            ),
            _field(
                "system.simulation_mode",
                "Simulation mode",
                "Uses simulated values instead of real hardware. Useful for development and testing.",
                "boolean",
                level="advanced",
            ),
            _field(
                "system.allow_hardware_writes",
                "Allow hardware writes",
                "Allows EMS to send control changes to devices over the local API. Turn this off for read-only validation.",
                "boolean",
                level="advanced",
            ),
            _field(
                "system.allow_mqtt_local_control_writes",
                "Allow local MQTT control writes",
                "Allows EMS to send output control to devices via a local MQTT broker. Turn this off for read-only validation.",
                "boolean",
                level="advanced",
            ),
            _field(
                "system.allow_mqtt_zendure_control_writes",
                "Allow Zendure cloud MQTT control writes",
                "Allows EMS to send output control via the Zendure cloud MQTT broker. Turn this off for read-only validation.",
                "boolean",
                level="advanced",
            ),
            _field(
                "system.allow_state_reconciliation_writes",
                "Repair device state on startup",
                "Allows EMS to correct known device state values when it starts.",
                "boolean",
                level="advanced",
            ),
            _field(
                "system.reconcile_ac_mode_on_start",
                "Restore AC mode on startup",
                "Checks and restores the expected AC mode when EMS starts.",
                "boolean",
                level="advanced",
            ),
            _field(
                "system.reconcile_smart_mode",
                "Restore smart mode",
                "Checks and restores the expected device smart mode for live EMS control.",
                "boolean",
                level="advanced",
            ),
            _field(
                "system.log_level",
                "Log level",
                "Controls how much detail EMS writes to the log. Use info for normal operation.",
                "select",
                level="advanced",
                options=("debug", "info", "warning", "error"),
            ),
            _field(
                "system.max_total_power",
                "Maximum system output",
                "Maximum combined output of all controlled inverters.",
                "number",
                unit="W",
                minimum=0,
                risk="control_stability",
            ),
            _field(
                "system.loop_interval",
                "Loop interval",
                "Time between two EMS control cycles.",
                "number",
                unit="s",
                minimum=0,
                risk="control_stability",
            ),
            _field(
                "system.output_control.target_deadband_w",
                "System deadband",
                "Power difference the EMS ignores before changing the total "
                "system target.",
                "number",
                unit="W",
                minimum=0,
                risk="control_stability",
            ),
            _field(
                "system.output_control.ramp_up_w_per_cycle",
                "System ramp up",
                "Maximum increase of the total system target during one control "
                "cycle.",
                "number",
                unit="W/cycle",
                minimum=0,
                risk="control_stability",
            ),
            _field(
                "system.output_control.ramp_down_w_per_cycle",
                "System ramp down",
                # A slower down-ramp reduces undershoot when inverter output
                # reacts more slowly than the EMS target.
                "Maximum reduction of the total system target during one control "
                "cycle.",
                "number",
                unit="W/cycle",
                minimum=0,
                risk="control_stability",
            ),
            _field(
                "system.deadband",
                "Device deadband",
                "Minimum target change required before the EMS sends a new value "
                "to this device.",
                "number",
                unit="W",
                minimum=0,
                risk="control_stability",
            ),
            _field(
                "system.output_control.device_ramp_up_w_per_cycle",
                "Device ramp up",
                "Maximum target increase sent to this device during one control "
                "cycle.",
                "number",
                unit="W/cycle",
                minimum=0,
                risk="control_stability",
            ),
            _field(
                "system.output_control.device_ramp_down_w_per_cycle",
                "Device ramp down",
                # A slower down-ramp avoids repeatedly lowering the target while
                # the inverter is still reacting to an earlier command.
                "Maximum target reduction sent to this device during one control "
                "cycle.",
                "number",
                unit="W/cycle",
                minimum=0,
                risk="control_stability",
            ),
            _field(
                "system.max_device_power",
                "Global per-device cap",
                "Upper safety cap applied to each device. Most users should "
                "configure the device output limit in the Devices section instead.",
                "number",
                level="advanced",
                unit="W",
                minimum=0,
                risk="control_stability",
            ),
            _field(
                "system.runtime_state_path",
                "Runtime state file",
                "File used to store EMS runtime state between restarts. Most users should keep the existing value.",
                "text",
                level="expert",
            ),
            _field(
                "system.min_output_limit",
                "Minimum output limit",
                "Lowest output value EMS should request when a device needs to stay active.",
                "number",
                level="advanced",
                unit="W",
                minimum=0,
                risk="control_stability",
            ),
            _field(
                "system.output_control.load_deadband_w",
                "Load deadband",
                "Ignores small measured load changes before calculating a new target.",
                "number",
                unit="W",
                minimum=0,
                **_OUTPUT_CONTROL,
            ),
            _field(
                "system.output_control.filter_enabled",
                "Filter load signal",
                "Smooths measured grid power before EMS reacts to it.",
                "boolean",
                **_OUTPUT_CONTROL,
            ),
            _field(
                "system.output_control.filter_method",
                "Filter method",
                "Chooses the smoothing method for grid power measurements.",
                "select",
                options=("median_ema",),
                **_OUTPUT_CONTROL,
            ),
            _field(
                "system.output_control.median_window",
                "Median window",
                "Number of samples used by the median filter. Higher values smooth more but react slower.",
                "number",
                minimum=1,
                **_OUTPUT_CONTROL,
            ),
            _field(
                "system.output_control.ema_alpha",
                "EMA strength",
                "Controls how strongly recent measurements affect the smoothed value.",
                "number",
                minimum=0,
                maximum=1,
                **_OUTPUT_CONTROL,
            ),
            _field(
                "system.output_control.sign_change_fast_response_enabled",
                "Fast response on direction change",
                "Lets EMS react faster when grid power changes from import to export or back.",
                "boolean",
                **_OUTPUT_CONTROL,
            ),
            _field(
                "system.output_control.sign_change_threshold_w",
                "Direction change threshold",
                "Minimum change needed before fast response is triggered.",
                "number",
                unit="W",
                minimum=0,
                **_OUTPUT_CONTROL,
            ),
            _field(
                "system.output_control.sign_change_filter_reset_factor",
                "Filter reset factor",
                "Controls how much the filter is reset during a fast response event.",
                "number",
                minimum=0,
                **_OUTPUT_CONTROL,
            ),
            _field(
                "system.output_control.ramp_enabled",
                "Enable output ramp",
                "Limits how quickly the total output target may change.",
                "boolean",
                **_OUTPUT_CONTROL,
            ),
            _field(
                "system.output_control.device_ramp_enabled",
                "Enable per-device ramp",
                "Limits how quickly each single device target may change.",
                "boolean",
                **_OUTPUT_CONTROL,
            ),
            _field(
                "system.output_control.large_import_bypass_w",
                "Large import bypass",
                "Lets EMS react more aggressively when the home imports a lot of power.",
                "number",
                unit="W",
                minimum=0,
                **_OUTPUT_CONTROL,
            ),
            _field(
                "system.output_control.large_export_bypass_w",
                "Large export bypass",
                "Lets EMS react more aggressively when the home exports a lot of power.",
                "number",
                unit="W",
                minimum=0,
                **_OUTPUT_CONTROL,
            ),
            _field(
                "system.output_control.bypass_ramp_multiplier",
                "Bypass ramp multiplier",
                "Multiplies ramp speed during large import or export bypass handling.",
                "number",
                minimum=0,
                **_OUTPUT_CONTROL,
            ),
            _field(
                "system.output_control.telemetry_max_age_seconds",
                "Telemetry max age",
                "Maximum age of device telemetry before EMS treats it as stale.",
                "number",
                unit="s",
                minimum=0,
                **_OUTPUT_CONTROL,
            ),
            _field(
                "system.output_control.stale_telemetry_ramp_factor",
                "Stale telemetry ramp factor",
                "Reduces ramp speed when telemetry is stale.",
                "number",
                minimum=0,
                **_OUTPUT_CONTROL,
            ),
            _field(
                "system.redistribute_clamped_power",
                "Redistribute limited power",
                "Moves unused output capacity from limited devices to devices that can still provide power.",
                "boolean",
                level="advanced",
                risk="control_stability",
            ),
            _field(
                "system.pv_kwp_weighting",
                "Weight by PV size",
                "Uses configured PV size to influence how output is shared between devices.",
                "boolean",
                level="advanced",
                risk="control_stability",
            ),
            _field(
                "system.pv_charge_balance_enabled",
                "Balance PV charging",
                "Helps keep battery-backed devices closer together while charging from PV.",
                "boolean",
                level="advanced",
                risk="control_stability",
            ),
            _field(
                "system.pv_charge_balance_deadband_percent",
                "Charge balance deadband",
                "Difference that must be exceeded before PV charge balancing reacts.",
                "number",
                level="expert",
                unit="%",
                minimum=0,
                maximum=100,
                risk="control_stability",
            ),
            _field(
                "system.pv_charge_balance_full_bias_percent",
                "Full battery bias",
                "Bias used when one battery is much closer to full than another.",
                "number",
                level="expert",
                unit="%",
                minimum=0,
                maximum=100,
                risk="control_stability",
            ),
            _field(
                "system.pv_charge_balance_strength",
                "Charge balance strength",
                "How strongly EMS shifts charging behavior to balance devices.",
                "number",
                level="expert",
                minimum=0,
                risk="control_stability",
            ),
            _field(
                "system.battery_kwh_weighting",
                "Weight by battery size",
                "Uses configured battery size to influence how output is shared between devices.",
                "boolean",
                level="advanced",
                risk="control_stability",
            ),
            _field(
                "system.soc_reconcile_interval",
                "SoC reconcile interval",
                "How often EMS refreshes and reconciles battery SoC-related state.",
                "number",
                level="expert",
                minimum=1,
                risk="control_stability",
            ),
        ],
        collapsible=False,
        setup_group="advanced",
        kind="system",
        groups=[
            _group(
                "output_control",
                "system.output_control",
                "Expert control tuning",
                "Fine tuning for smoothing, ramps, and fast response.",
                "Changes how quickly and smoothly EMS reacts. Wrong values can make control unstable.",
                1,
                level="expert",
                risk="control_stability",
            )
        ],
    ),
    _section(
        "grid_meter",
        "grid_meter",
        "Grid meter",
        "Meter used as the home power signal.",
        "Tells EMS where to read the current grid import or export value.",
        2,
        [
            _field(
                "grid_meter.type",
                "Meter type",
                "Selects the device or integration that provides the home grid power value.",
                "select",
                options=(
                    "shelly",
                    "shelly_3em_gen1",
                    "ecotracker",
                    "zendure_smartmeter_3ct_http",
                    "zendure_smartmeter_d0_http",
                    "tasmota_http",
                    "zendure_smartmeter_d0",
                    "mqtt",
                    "ha",
                ),
                required=True,
            ),
            _field(
                "grid_meter.ip",
                "Meter IP address",
                "IP address of the grid meter in your local network, if the selected meter type needs one.",
                "text",
            ),
            _field(
                "grid_meter.channels",
                "Shelly channels",
                "Optional Shelly clamp or phase selection. Leave empty to use the meter total.",
                "string_list",
                level="advanced",
            ),
            _field(
                "grid_meter.url",
                "Meter URL",
                "Full Tasmota HTTP endpoint. Use this when the default endpoint from the IP is not enough.",
                "url",
                level="advanced",
            ),
            _field(
                "grid_meter.power_path",
                "Power value path",
                "Dot-separated JSON path to the current power value.",
                "text",
            ),
            _field(
                "grid_meter.mqtt.host",
                "MQTT broker",
                "MQTT broker host.",
                "text",
            ),
            _field(
                "grid_meter.mqtt.port",
                "MQTT port",
                "MQTT broker port.",
                "number",
            ),
            _field(
                "grid_meter.mqtt.username",
                "MQTT username",
                "Optional MQTT username.",
                "text",
                level="advanced",
            ),
            _field(
                "grid_meter.mqtt.password",
                "MQTT password",
                "Optional MQTT password.",
                "password",
                level="advanced",
                risk="secret",
            ),
            _field(
                "grid_meter.mqtt.topic",
                "MQTT topic",
                "MQTT topic that contains the current grid power.",
                "text",
            ),
            _field(
                "grid_meter.mqtt.payload_format",
                "MQTT payload format",
                "Choose whether the topic payload is a plain number or JSON.",
                "select",
                level="advanced",
                options=("number", "json"),
            ),
            _field(
                "grid_meter.mqtt.value_path",
                "MQTT value path",
                "Dot-separated JSON path to the power value. Required for JSON payloads.",
                "text",
                level="advanced",
            ),
            _field(
                "grid_meter.mqtt.max_age_seconds",
                "MQTT maximum age",
                "Maximum age of the last MQTT value before EMS treats it as stale.",
                "number",
                level="advanced",
                unit="seconds",
            ),
        ],
        collapsible=False,
        setup_group="hardware",
        kind="hardware",
    ),
    _section(
        "devices",
        "devices",
        "Devices",
        "Zendure devices controlled by EMS.",
        "Add one entry for each inverter or battery-backed device EMS should control.",
        3,
        [
            _field(
                "devices[].name",
                "Device name",
                "Friendly name shown in logs, dashboard, and Admin pages.",
                "text",
                required=True,
            ),
            _field(
                "devices[].ip",
                "Device IP address",
                "Local IP address used to reach the device.",
                "text",
                required=True,
            ),
            _field(
                "devices[].sn",
                "Serial number",
                "Device serial number used by the Zendure API.",
                "text",
                required=True,
            ),
            _field(
                "devices[].smart_mode",
                "Smart mode",
                "Runtime mode used for live EMS control. Most users should keep the existing value.",
                "number",
                level="advanced",
                options=(0, 1),
                risk="control_stability",
            ),
            _field(
                "devices[].max_power",
                "Device output limit",
                "Maximum output this inverter may provide.",
                "number",
                unit="W",
                minimum=0,
                risk="control_stability",
            ),
            _field(
                "devices[].pv_kwp",
                "PV size",
                "Approximate PV power connected to this device. Used for power sharing between devices.",
                "number",
                level="advanced",
                unit="kWp",
                minimum=0,
                risk="control_stability",
            ),
            _field(
                "devices[].pv_priority_factor",
                "PV priority",
                "Gives this device more or less priority during PV-based balancing.",
                "number",
                level="advanced",
                minimum=0,
                risk="control_stability",
            ),
            _field(
                "devices[].battery_kwh",
                "Battery size",
                "Battery capacity connected to this device. Used for SoC-aware power sharing.",
                "number",
                level="advanced",
                unit="kWh",
                minimum=0,
                risk="control_stability",
            ),
            _field(
                "devices[].min_soc",
                "Minimum SoC",
                "Lowest battery level EMS should normally allow for this device.",
                "number",
                unit="%",
                minimum=0,
                maximum=100,
                risk="control_stability",
            ),
            _field(
                "devices[].max_soc",
                "Maximum SoC",
                "Highest battery level EMS should normally use for this device.",
                "number",
                unit="%",
                minimum=0,
                maximum=100,
                risk="control_stability",
            ),
        ],
        collapsible=False,
        setup_group="hardware",
        kind="hardware",
    ),
    _section(
        "winter",
        "winter",
        "Winter mode",
        "Keeps more battery reserve in winter.",
        "Raises the minimum battery reserve during selected winter months.",
        4,
        [
            _field(
                "winter.enabled",
                "Winter mode",
                "Keeps more battery reserve during winter months.",
                "boolean",
            ),
            _field(
                "winter.months",
                "Winter months",
                "Months where the higher winter reserve should be active.",
                "month_list",
            ),
            _field(
                "winter.summer_min_soc",
                "Summer minimum SoC",
                "Battery reserve used outside the configured winter months.",
                "number",
                unit="%",
                minimum=0,
                maximum=100,
                risk="control_stability",
            ),
            _field(
                "winter.winter_min_soc",
                "Winter minimum SoC",
                "Battery reserve used during the configured winter months.",
                "number",
                unit="%",
                minimum=0,
                maximum=100,
                risk="control_stability",
            ),
            _field(
                "winter.ramp_step_percent",
                "Reserve change step",
                "Maximum SoC target change per adjustment step.",
                "number",
                level="advanced",
                unit="%",
                minimum=0,
                maximum=100,
                risk="control_stability",
            ),
            _field(
                "winter.adjust_hour",
                "Adjustment hour",
                "Hour of the day when EMS adjusts the seasonal reserve. Use local time.",
                "number",
                level="advanced",
                unit="h",
                minimum=0,
                maximum=23,
            ),
            _field(
                "winter.ac_charge_power",
                "AC charge power",
                "Optional AC charging power used by the winter strategy when supported.",
                "number",
                level="advanced",
                unit="W",
                minimum=0,
                risk="control_stability",
            ),
        ],
        enabled_path="winter.enabled",
    ),
    _section(
        "battery_full_charge_assist",
        "battery_full_charge_assist",
        "Battery full-charge assist",
        "Helps batteries reach 100% periodically.",
        "Temporarily raises the device charge target so batteries can complete a full charge from time to time.",
        5,
        [
            _field(
                "battery_full_charge_assist.enabled",
                "Full-charge assist",
                "Helps batteries reach a full charge periodically.",
                "boolean",
            ),
            _field(
                "battery_full_charge_assist.interval_days",
                "Full-charge interval",
                "How often EMS should try to let a battery reach full charge.",
                "number",
                unit="days",
                minimum=1,
            ),
            _field(
                "battery_full_charge_assist.assist_window_days",
                "Assist window",
                "How many days before the target date EMS may start helping the battery reach full charge.",
                "number",
                level="advanced",
                unit="days",
                minimum=0,
            ),
            _field(
                "battery_full_charge_assist.assist_start_soc",
                "Start assist at SoC",
                "Battery level from which EMS may start the full-charge assist behavior.",
                "number",
                level="advanced",
                unit="%",
                minimum=0,
                maximum=100,
                risk="control_stability",
            ),
            _field(
                "battery_full_charge_assist.force_time",
                "Force time",
                "Time of day when EMS may force the full-charge attempt if it has not completed naturally.",
                "time",
                level="advanced",
                risk="control_stability",
            ),
            _field(
                "battery_full_charge_assist.ac_charge_power",
                "AC charge power",
                "AC charging power used during full-charge assist when supported.",
                "number",
                level="advanced",
                unit="W",
                minimum=0,
                risk="control_stability",
            ),
            _field(
                "battery_full_charge_assist.enable_ac_charge_mode",
                "Use AC charge mode",
                "Allows EMS to use AC charge mode during the assist window when supported.",
                "boolean",
                level="advanced",
                risk="control_stability",
            ),
            _field(
                "battery_full_charge_assist.state_database_path",
                "Assist state database",
                "File used to remember full-charge assist state. Most users should keep the existing value.",
                "text",
                level="expert",
                risk="storage",
            ),
        ],
        enabled_path="battery_full_charge_assist.enabled",
    ),
    _section(
        "energy_savings",
        "energy_savings",
        "Energy savings",
        "Estimates daily savings from measured output.",
        "Uses measured inverter output and your electricity price to estimate savings.",
        6,
        [
            _field(
                "energy_savings.enabled",
                "Energy savings",
                "Enables local savings statistics.",
                "boolean",
            ),
            _field(
                "energy_savings.price_per_kwh",
                "Electricity price",
                "Your electricity price per kWh, used to estimate savings.",
                "number",
                unit="currency/kWh",
                minimum=0,
            ),
            _field(
                "energy_savings.currency",
                "Currency",
                "Currency shown for estimated savings.",
                "text",
            ),
            _field(
                "energy_savings.max_sample_delta_seconds",
                "Max sample gap",
                "Maximum time gap between samples that still counts as continuous measurement.",
                "number",
                level="advanced",
                unit="s",
                minimum=0,
            ),
            _field(
                "energy_savings.timezone",
                "Time zone",
                "Time zone used for daily savings boundaries and reports.",
                "text",
                level="advanced",
            ),
        ],
        enabled_path="energy_savings.enabled",
    ),
    _section(
        "zendure_mqtt",
        "zendure_mqtt",
        "Zendure MQTT telemetry",
        "Zendure MQTT telemetry runtime (always on).",
        "Subscribes to Zendure telemetry from a local or cloud MQTT broker. Always on once its host is configured (cloud needs stored Zendure credentials). Supported inverters can also be controlled over MQTT (capability-based).",
        5,
        [
            _field(
                "zendure_mqtt.host",
                "Broker host",
                "Hostname or IP of the MQTT broker carrying Zendure telemetry.",
                "text",
            ),
            _field(
                "zendure_mqtt.port",
                "Broker port",
                "TCP port of the MQTT broker.",
                "number",
                minimum=1,
                maximum=65535,
            ),
            _field(
                "zendure_mqtt.tls",
                "Use TLS",
                "Connect to the broker over TLS.",
                "boolean",
                level="advanced",
            ),
            _field(
                "zendure_mqtt.tls_insecure",
                "Skip TLS verification",
                "Disable broker certificate verification (use only on trusted networks).",
                "boolean",
                level="advanced",
            ),
            _field(
                "zendure_mqtt.username",
                "Broker username",
                "Username for the MQTT broker, if required.",
                "text",
                level="advanced",
                risk="secret",
            ),
            _field(
                "zendure_mqtt.password",
                "Broker password",
                "Password for the MQTT broker, if required.",
                "password",
                level="advanced",
                risk="secret",
            ),
            _field(
                "zendure_mqtt.app_key",
                "Cloud app key",
                "Optional Zendure cloud app key used to derive the cloud subscription prefix.",
                "password",
                level="advanced",
                risk="secret",
            ),
            _field(
                "zendure_mqtt.connect_timeout_seconds",
                "Connect timeout",
                "Broker connection timeout.",
                "number",
                level="advanced",
                unit="s",
                minimum=1,
            ),
            _field(
                "zendure_mqtt.keepalive_seconds",
                "Keepalive interval",
                "MQTT keepalive interval.",
                "number",
                level="advanced",
                unit="s",
                minimum=2,
            ),
        ],
        scope="maintenance",
    ),
    _section(
        "dashboard",
        "dashboard",
        "Dashboard",
        "Local web dashboard.",
        "Provides the local EMS dashboard with live values, history, and optional write controls.",
        7,
        [
            _field(
                "dashboard.enabled",
                "Dashboard enabled",
                "Starts the local EMS dashboard.",
                "boolean",
            ),
            _field(
                "dashboard.host",
                "Dashboard host",
                "Network address the dashboard listens on. Most Docker setups should keep 0.0.0.0.",
                "text",
                level="advanced",
            ),
            _field(
                "dashboard.port",
                "Dashboard port",
                "Port used to open the dashboard in a browser.",
                "number",
                minimum=1,
                maximum=65535,
            ),
            _field(
                "dashboard.database_path",
                "Dashboard database",
                "Local SQLite database used for dashboard history. Most users should keep the existing value.",
                "text",
                level="expert",
                risk="storage",
            ),
            _field(
                "dashboard.history_hours",
                "Local history window",
                "Number of hours kept in local dashboard history.",
                "number",
                level="advanced",
                unit="h",
                minimum=0,
                risk="storage",
            ),
            _field(
                "dashboard.write_interval_seconds",
                "History write interval",
                "How often dashboard history is written locally.",
                "number",
                level="expert",
                unit="s",
                minimum=0,
                risk="storage",
            ),
            _field(
                "dashboard.auth_file",
                "Dashboard auth file",
                "File used to store local dashboard login data.",
                "text",
                level="expert",
                risk="secrets",
            ),
            _field(
                "dashboard.ssl_enabled",
                "Enable HTTPS",
                "Enables HTTPS for the local dashboard. Usually not needed behind a trusted reverse proxy.",
                "boolean",
                level="advanced",
            ),
            _field(
                "dashboard.ssl_cert_file",
                "HTTPS certificate file",
                "Certificate file used when HTTPS is enabled.",
                "text",
                level="expert",
                risk="secrets",
            ),
            _field(
                "dashboard.ssl_key_file",
                "HTTPS key file",
                "Private key file used when HTTPS is enabled.",
                "text",
                level="expert",
                risk="secrets",
            ),
            _field(
                "dashboard.ssl_auto_generate",
                "Auto-generate HTTPS certificate",
                "Creates a local self-signed certificate if HTTPS is enabled and no certificate exists.",
                "boolean",
                level="advanced",
            ),
            _field(
                "dashboard.session_idle_timeout_seconds",
                "Idle session timeout",
                "Logs users out after this many seconds without activity.",
                "number",
                level="advanced",
                unit="s",
                minimum=0,
            ),
            _field(
                "dashboard.session_absolute_max_seconds",
                "Maximum session length",
                "Logs users out after this maximum total session time.",
                "number",
                level="advanced",
                unit="s",
                minimum=0,
            ),
            _field(
                "dashboard.log_buffer_lines",
                "Log buffer size",
                "Number of recent log lines kept available for the dashboard.",
                "number",
                level="advanced",
                unit="lines",
                minimum=1,
                risk="storage",
            ),
            _field(
                "dashboard.log_redaction",
                "Redact logs",
                "Hides sensitive values in dashboard-visible logs when possible.",
                "boolean",
                level="advanced",
                risk="secrets",
            ),
            _field(
                "dashboard.animation_mode",
                "Animation mode",
                "Controls dashboard animation intensity. Use reduced or off on slower devices.",
                "select",
                options=("normal", "reduced", "off"),
            ),
        ],
        enabled_path="dashboard.enabled",
    ),
    _section(
        "influxdb",
        "influxdb",
        "Analytics / InfluxDB",
        "Long-term analytics storage.",
        "Stores detailed long-term history for analytics charts. Local SQLite history still works when this is disabled.",
        8,
        [
            _field(
                "influxdb.enabled",
                "Analytics enabled",
                "Enables long-term analytics storage with InfluxDB.",
                "boolean",
            ),
            _field(
                "influxdb.mode",
                "InfluxDB mode",
                "Choose bundled InfluxDB for Docker, or external for your own InfluxDB server.",
                "select",
                options=("bundled", "external"),
            ),
            _field(
                "influxdb.auto_init",
                "Auto-initialize bundled InfluxDB",
                "Lets setup create bundled InfluxDB secrets and prepare the local service.",
                "boolean",
                level="advanced",
            ),
            _field(
                "influxdb.auto_sync",
                "Auto-sync analytics schema",
                "Lets setup apply required buckets, retention rules, and downsampling tasks.",
                "boolean",
                level="advanced",
            ),
            _field(
                "influxdb.secret_file",
                "Secret file",
                "Local environment file used to store generated bundled InfluxDB secrets. Never commit this file.",
                "text",
                level="expert",
                risk="secrets",
            ),
            _field(
                "influxdb.url",
                "Container URL",
                "InfluxDB URL used by EMS when it runs inside Docker.",
                "text",
                level="advanced",
            ),
            _field(
                "influxdb.host_url",
                "Host URL",
                "InfluxDB URL used by host-side commands or native EMS runs.",
                "text",
                level="advanced",
            ),
            _field(
                "influxdb.org",
                "Organization",
                "InfluxDB organization name.",
                "text",
                level="advanced",
            ),
            _field(
                "influxdb.token",
                "Token",
                "Direct InfluxDB token. Prefer using the token environment variable or bundled secret file.",
                "password",
                level="expert",
                risk="secrets",
            ),
            _field(
                "influxdb.token_env",
                "Token environment variable",
                "Environment variable name used to read the InfluxDB token.",
                "text",
                level="expert",
                risk="secrets",
            ),
            _field(
                "influxdb.bucket_prefix",
                "Bucket prefix",
                "Prefix used for EMS analytics buckets.",
                "text",
                level="advanced",
                risk="storage",
            ),
            _field(
                "influxdb.raw_write_interval_seconds",
                "Raw write interval",
                "How often raw telemetry is written. Use 0 to write once per EMS control loop.",
                "number",
                level="advanced",
                unit="s",
                minimum=0,
                risk="storage",
            ),
            _field(
                "influxdb.retention.raw_days",
                "Raw data retention",
                "How long high-resolution raw telemetry is kept.",
                "number",
                level="advanced",
                unit="days",
                minimum=0,
                risk="storage",
                group="retention",
            ),
            _field(
                "influxdb.retention.one_minute_days",
                "1-minute retention",
                "How long 1-minute analytics data is kept.",
                "number",
                level="advanced",
                unit="days",
                minimum=0,
                risk="storage",
                group="retention",
            ),
            _field(
                "influxdb.retention.five_minute_days",
                "5-minute retention",
                "How long 5-minute analytics data is kept.",
                "number",
                level="advanced",
                unit="days",
                minimum=0,
                risk="storage",
                group="retention",
            ),
            _field(
                "influxdb.retention.one_hour_days",
                "1-hour retention",
                "How long hourly analytics data is kept.",
                "number",
                level="advanced",
                unit="days",
                minimum=0,
                risk="storage",
                group="retention",
            ),
            _field(
                "influxdb.downsampling[].source",
                "Source bucket",
                "Resolution bucket used as input for this downsampling rule.",
                "select",
                level="expert",
                options=("raw", "1m", "5m", "1h"),
                risk="storage",
                group="downsampling",
            ),
            _field(
                "influxdb.downsampling[].target",
                "Target bucket",
                "Resolution bucket written by this downsampling rule.",
                "select",
                level="expert",
                options=("raw", "1m", "5m", "1h"),
                risk="storage",
                group="downsampling",
            ),
            _field(
                "influxdb.downsampling[].window",
                "Window",
                "Time window used to aggregate data into the target bucket.",
                "duration",
                level="expert",
                risk="storage",
                group="downsampling",
            ),
            _field(
                "influxdb.query_profiles[].max_range",
                "Maximum range",
                "Largest selected time range where this profile should be used.",
                "duration",
                level="expert",
                risk="storage",
                group="query_profiles",
            ),
            _field(
                "influxdb.query_profiles[].bucket",
                "Data bucket",
                "Analytics bucket used for this time range.",
                "select",
                level="expert",
                options=("raw", "1m", "5m", "1h"),
                risk="storage",
                group="query_profiles",
            ),
            _field(
                "influxdb.query_profiles[].window",
                "Query window",
                "Aggregation window used when reading data for charts.",
                "duration",
                level="expert",
                risk="storage",
                group="query_profiles",
            ),
        ],
        enabled_path="influxdb.enabled",
        groups=[
            _group(
                "retention",
                "influxdb.retention",
                "Retention",
                "How long analytics data is kept.",
                "Controls storage periods for each analytics resolution.",
                1,
                level="advanced",
                risk="storage",
            ),
            _group(
                "downsampling",
                "influxdb.downsampling",
                "Downsampling",
                "Creates lower-resolution history for longer time ranges.",
                "Combines detailed measurements into longer time windows to reduce storage use.",
                2,
                level="expert",
                risk="storage",
            ),
            _group(
                "query_profiles",
                "influxdb.query_profiles",
                "Query profiles",
                "Chooses analytics resolution by selected time range.",
                "Selects the data resolution used when charts cover different time ranges.",
                3,
                level="expert",
                risk="storage",
            ),
        ],
    ),
    _section(
        "ha",
        "ha",
        "Home Assistant legacy integration",
        "Legacy Home Assistant integration.",
        "Older Home Assistant integration. New setups should prefer telemetry publishing instead of HA control.",
        9,
        [
            _field(
                "ha.enabled",
                "Home Assistant integration",
                "Enables the legacy Home Assistant integration. Most users should leave this disabled.",
                "boolean",
                level="deprecated",
                scope="maintenance",
                risk="deprecated",
            ),
            _field(
                "ha.control_enabled",
                "Home Assistant control",
                "Allows Home Assistant control through the legacy integration. Not recommended for new setups.",
                "boolean",
                level="deprecated",
                scope="maintenance",
                risk="deprecated",
            ),
            _field(
                "ha.url",
                "Home Assistant URL",
                "URL of the Home Assistant instance used by the legacy integration.",
                "text",
                level="deprecated",
                scope="maintenance",
                risk="deprecated",
            ),
            _field(
                "ha.token",
                "Home Assistant token",
                "Access token for the legacy integration. Add it only when this integration is required.",
                "password",
                level="deprecated",
                scope="maintenance",
                risk="secrets",
            ),
        ],
        level="deprecated",
        scope="maintenance",
        enabled_path="ha.enabled",
        setup_group="advanced",
        kind="deprecated",
    ),
    _section(
        "config_upgrade",
        "config_upgrade",
        "Config maintenance",
        "Keeps config files compatible with newer EMS versions.",
        "Checks or applies missing config keys when EMS starts after an update.",
        10,
        [
            _field(
                "config_schema_version",
                "Config schema version",
                "Internal version used by EMS to understand the config file format.",
                "readonly",
                level="expert",
                scope="maintenance",
                restart_required=False,
                backup_recommended=False,
                risk="none",
                editable=False,
            ),
            _field(
                "config_upgrade.on_startup",
                "Startup config check",
                "Chooses whether EMS reports missing config keys or adds them during startup.",
                "select",
                level="advanced",
                scope="maintenance",
                options=("disabled", "check", "apply"),
            ),
            _field(
                "config_upgrade.backup_before_apply",
                "Backup before config update",
                "Creates a backup before EMS writes missing config keys.",
                "boolean",
                level="advanced",
                scope="maintenance",
            ),
            _field(
                "config_upgrade.backup_failure_policy",
                "Backup failure behavior",
                "Chooses what EMS does if a backup cannot be created before updating the config.",
                "select",
                level="advanced",
                scope="maintenance",
                options=("continue_without_upgrade",),
            ),
        ],
        level="advanced",
        scope="maintenance",
        setup_group="advanced",
        kind="system",
    ),
]

_DEFAULT_TEMPLATE = {'_comment': 'Copy this file to config.json and adjust your local setup.', '_comment_docs': ['Detailed setup guide: docs/configuration.md.', 'Example profiles: docs/configuration-examples.md.'], 'config_schema_version': 3, 'config_upgrade': {'_comment': ['Optional config.json maintenance on startup.', "'check' reports missing template keys without writing.", "'apply' backs up the file and adds missing keys before EMS starts."], 'on_startup': 'check', 'backup_before_apply': True, 'backup_failure_policy': 'continue_without_upgrade'}, 'system': {'_comment': ['Standalone live-control defaults.', 'Set dry_run=true when validating a setup without hardware writes.', 'Review power and SOC limits before the first live run.'], 'enabled': True, 'dry_run': False, 'simulation_mode': False, 'allow_hardware_writes': True, 'allow_mqtt_local_control_writes': True, 'allow_mqtt_zendure_control_writes': True, 'allow_state_reconciliation_writes': True, 'reconcile_ac_mode_on_start': True, 'reconcile_smart_mode': True, 'log_level': 'info', 'max_total_power': 800, 'max_device_power': 800, 'deadband': 2, 'runtime_state_path': 'data/runtime-state.json', 'min_output_limit': 35, 'loop_interval': 5, 'output_control': {'_comment_output_control': ['Advanced output smoothing and ramp tuning.', 'Most installations should keep these defaults.'], 'load_deadband_w': 5, 'target_deadband_w': 5, 'filter_enabled': True, 'filter_method': 'median_ema', 'median_window': 2, 'ema_alpha': 0.85, 'sign_change_fast_response_enabled': True, 'sign_change_threshold_w': 50, 'sign_change_filter_reset_factor': 1.0, 'ramp_enabled': True, 'ramp_up_w_per_cycle': 500, 'ramp_down_w_per_cycle': 300, 'device_ramp_enabled': True, 'device_ramp_up_w_per_cycle': 400, 'device_ramp_down_w_per_cycle': 200, 'large_import_bypass_w': 600, 'large_export_bypass_w': 600, 'bypass_ramp_multiplier': 1.5, 'telemetry_max_age_seconds': 10, 'stale_telemetry_ramp_factor': 0.5}, 'redistribute_clamped_power': True, 'pv_kwp_weighting': True, 'pv_charge_balance_enabled': True, 'pv_charge_balance_deadband_percent': 1, 'pv_charge_balance_full_bias_percent': 15, 'pv_charge_balance_strength': 0.7, 'battery_kwh_weighting': True, 'soc_reconcile_interval': 10}, 'grid_meter': {'_comment': ['Household or grid power meter used as the EMS load signal.', 'Supported types: shelly, shelly_3em_gen1, ecotracker, tasmota_http,', 'zendure_grid_meter_http, zendure_smartmeter_d0, mqtt.', 'Shelly reads total power by default.', 'Use channels like [c] or [a,c] for selected clamps or phases.', 'Tasmota needs url or ip plus power_path.', 'Zendure grid meter via local HTTP reads total_power from', '/properties/report and needs only grid_meter.ip (recommended; covers', 'both D0 and Smart Meter 3CT). zendure_smartmeter_3ct_http is a', 'backward-compatible alias.', 'Zendure SmartMeter D0 and MQTT use grid_meter.mqtt with topic and', 'either mqtt.broker_ref or host/port; payload_format and max_age_seconds.'], 'type': 'shelly', 'ip': '192.168.1.50'}, '_comment_devices': ['One entry per Zendure device.', 'Replace IP and SN with values from your local installation.', 'Use one real device first, then add more devices after validation.'], 'devices': [{'name': 'WR1', 'ip': '192.168.1.100', 'sn': 'YOUR_SN', '_comment_smart_mode': ['Use smart_mode=1 for runtime/RAM mode.', 'This is the normal setting for live EMS control.'], 'smart_mode': 1, 'max_power': 800, 'pv_kwp': 1.0, 'pv_priority_factor': 1.0, 'battery_kwh': 1.0, '_comment_soc': ['Battery SOC limits in percent.', 'Use 0 to leave a value unmanaged.'], 'min_soc': 15, 'max_soc': 100}, {'name': 'WR2', 'ip': '192.168.1.101', 'sn': 'YOUR_SN', '_comment_smart_mode': ['Use smart_mode=1 for runtime/RAM mode.', 'This is the normal setting for live EMS control.'], 'smart_mode': 1, 'max_power': 800, 'pv_kwp': 1.0, 'pv_priority_factor': 1.0, 'battery_kwh': 1.0, '_comment_soc': ['Battery SOC limits in percent.', 'Use 0 to leave a value unmanaged.'], 'min_soc': 15, 'max_soc': 100}], 'zendure_mqtt': {'_comment': ['MQTT telemetry is always on: set host to subscribe to Zendure telemetry', 'from a local or cloud MQTT broker.', 'Supported inverters can be controlled over MQTT using the same EMS', 'control loop as the local API, when the topic family has a verified', 'write method. A control device sets capabilities.write_output_limit=true', 'and publishes only behind its write gate (allow_mqtt_local_control_writes', '/ allow_mqtt_zendure_control_writes).', 'Devices whose topic family has no verified write method stay telemetry-only.', "Zendure MQTT devices live in 'devices' with type 'zendure_mqtt'.", 'Leave host empty if no broker is available; EMS still starts normally.'], 'host': '', 'port': 1883, 'tls': False, 'tls_insecure': False, 'username': '', 'password': '', 'app_key': '', 'connect_timeout_seconds': 10.0, 'keepalive_seconds': 30}, 'winter': {'_comment': ['Optional seasonal minimum-SOC strategy.', 'Enable it when batteries need a higher winter reserve.', 'See docs/configuration-examples.md before changing advanced values.'], 'enabled': True, 'months': [10, 11, 12, 1, 2, 3], 'summer_min_soc': 15, 'winter_min_soc': 40, 'ramp_step_percent': 5, 'adjust_hour': 12, 'ac_charge_power': 200}, 'battery_full_charge_assist': {'_comment': ['Optional full-charge assist for battery-backed devices.', 'Temporarily raises device Max-SoC to 100%.', 'Use it when devices should reach firmware Max-SoC periodically.'], 'enabled': True, 'interval_days': 28, 'assist_window_days': 7, 'assist_start_soc': 80, 'force_time': '14:00', 'ac_charge_power': 600, 'enable_ac_charge_mode': True, 'state_database_path': 'data/ems_state.sqlite'}, 'energy_savings': {'_comment': ['Lightweight daily energy statistics stored in SQLite.', 'Values are based on measured inverter AC output.', 'Set price_per_kwh to estimate savings.'], 'enabled': True, 'price_per_kwh': 0.0, 'currency': 'EUR', 'max_sample_delta_seconds': 20, 'timezone': 'Europe/Berlin'}, 'dashboard': {'_comment': ['Optional live dashboard.', 'Runtime write controls need a local admin password.', 'Create it with: python3 emsctl.py dashboard set-password.'], 'enabled': True, 'host': '0.0.0.0', 'port': 8080, 'database_path': 'data/ems_dashboard.sqlite', 'history_hours': 48, 'write_interval_seconds': 5, 'auth_file': 'config/dashboard-auth.json', 'ssl_enabled': False, 'ssl_cert_file': 'config/dashboard.crt', 'ssl_key_file': 'config/dashboard.key', 'ssl_auto_generate': True, 'session_idle_timeout_seconds': 1800, 'session_absolute_max_seconds': 43200, 'log_buffer_lines': 5000, 'log_redaction': False, '_comment_animation_mode': ['Visual animation cost for the dashboard.', "'normal' keeps the full animated energy-flow view.", "'reduced' trims glows and slows pipe motion.", "'off' disables continuous pipe animations and glow filters.", 'This does not affect control behavior or authentication.'], 'animation_mode': 'normal'}, 'influxdb': {'_comment': ['Optional InfluxDB 2.x backend for long-term analytics.', 'When disabled, the dashboard still uses local SQLite history.', 'Bundled mode is the supported zero-config Docker setup.', "Run 'python3 emsctl.py influx sync' after changing schema settings."], 'enabled': True, '_comment_mode': ["'bundled' uses the docker-compose InfluxDB managed by this project.", "Run 'python3 emsctl.py influx init' for the complete setup.", "Use 'python3 emsctl.py stack up' to start InfluxDB and EMS together.", "'external' points at an InfluxDB instance you manage yourself."], 'mode': 'bundled', '_comment_auto': ['auto_init lets setup commands create bundled InfluxDB secrets.', 'It can also start the bundled container during setup.', 'The EMS control loop never starts Docker by itself.', 'auto_sync applies bucket, retention, and task schema during setup.', 'Set both values false when managing InfluxDB fully by hand.'], 'auto_init': True, 'auto_sync': True, '_comment_secret_file': ['Local gitignored env file for generated bundled secrets.', 'Path is relative to the project root.', 'Never commit this file or place secrets directly in config.json.'], 'secret_file': 'deploy/docker/influxdb.env', '_comment_url': ["'url' is used by EMS when it runs inside Docker.", "In bundled mode, host-side commands use 'host_url' instead.", "A native EMS process also uses 'host_url' in bundled mode.", "External mode always uses 'url'."], 'url': 'http://influxdb:8086', 'host_url': 'http://127.0.0.1:8086', 'org': 'ems', '_comment_token': ['Leave token empty to read it from token_env.', 'This avoids committing secrets.', 'Bundled mode fills token_env from secret_file automatically.'], 'token': '', 'token_env': 'INFLUXDB_TOKEN', 'bucket_prefix': 'ems', '_comment_raw_write_interval': ['Raw telemetry write cadence for InfluxDB.', 'Use 0 or null to write one raw sample per EMS control loop.', 'Use a positive number to throttle writes to every N seconds.', 'This is separate from dashboard.write_interval_seconds.'], 'raw_write_interval_seconds': 0, '_comment_retention': ['How long InfluxDB keeps each resolution bucket.', 'Increase values only when storage capacity is planned.'], 'retention': {'raw_days': 14, 'one_minute_days': 90, 'five_minute_days': 365, 'one_hour_days': 1825}, '_comment_downsampling': ['Rules for creating lower-resolution history buckets.', 'Keep aligned with retention and query profiles.'], 'downsampling': [{'source': 'raw', 'target': '1m', 'window': '1m'}, {'source': '1m', 'target': '5m', 'window': '5m'}, {'source': '5m', 'target': '1h', 'window': '1h'}], '_comment_query_profiles': ['Dashboard analytics query resolution by selected time range.', 'Short ranges use detailed data; long ranges use downsampled buckets.'], 'query_profiles': [{'max_range': '1h', 'bucket': 'raw', 'window': '1s'}, {'max_range': '6h', 'bucket': 'raw', 'window': '10s'}, {'max_range': '24h', 'bucket': '1m', 'window': '1m'}, {'max_range': '30d', 'bucket': '5m', 'window': '5m'}, {'max_range': '365d', 'bucket': '1h', 'window': '1h'}]}, 'ha': {'_comment': ['Legacy Home Assistant integration.', 'Disabled by default; not recommended for new Admin-guided setups.', 'Future Admin flows should prefer telemetry publishing instead of HA-based control.'], 'enabled': False, 'control_enabled': False, 'url': 'http://homeassistant.local:8123', 'token': 'YOUR_TOKEN_HERE'}}  # noqa: E501

_ROOT_FIELDS = [
    _field(
        "_comment",
        "Template note",
        "Short note shown at the top of the generated template.",
        "text",
        level="internal",
        scope="hidden",
        requires_restart=False,
        backup_recommended=False,
        risk="none",
        editable=False,
    ),
    _field(
        "_comment_docs",
        "Documentation links",
        "Links to setup and example documentation.",
        "string_list",
        level="internal",
        scope="hidden",
        requires_restart=False,
        backup_recommended=False,
        risk="none",
        editable=False,
    ),
]


def _template_value(path):
    value = _DEFAULT_TEMPLATE
    for part in path.replace("[]", "").split("."):
        if isinstance(value, list):
            value = value[0]
        if not isinstance(value, dict) or part not in value:
            raise KeyError(path)
        value = value[part]
    return copy.deepcopy(value)


for _section_item in _SECTIONS:
    if _section_item["id"] == "grid_meter":
        _section_item["variants"] = copy.deepcopy(GRID_METER_VARIANTS)
    for _field_item in _section_item["fields"]:
        try:
            _field_item["default"] = _template_value(_field_item["path"])
            _field_item["template"] = True
        except KeyError:
            _field_item["template"] = False

for _field_item in _ROOT_FIELDS:
    _field_item["default"] = _template_value(_field_item["path"])
    _field_item["template"] = True

_variant_defaults = {
    "grid_meter.mqtt.port": 1883,
    "grid_meter.mqtt.payload_format": "number",
    "grid_meter.mqtt.max_age_seconds": 15,
}
for _section_item in _SECTIONS:
    for _field_item in _section_item["fields"]:
        if _field_item["path"] in _variant_defaults:
            _field_item["default"] = _variant_defaults[_field_item["path"]]

_field_overrides = {
    "config_schema_version": {
        "level": "internal",
        "scope": "hidden",
        "type": "integer",
    },
    "config_upgrade.backup_failure_policy": {"level": "expert"},
    "system.dry_run": {"level": "normal"},
    "system.runtime_state_path": {
        "level": "internal",
        "scope": "hidden",
        "type": "path",
    },
    "battery_full_charge_assist.state_database_path": {
        "level": "internal",
        "scope": "hidden",
        "type": "path",
    },
    "dashboard.database_path": {
        "level": "internal",
        "scope": "hidden",
        "type": "path",
    },
    "dashboard.auth_file": {
        "level": "internal",
        "scope": "hidden",
        "type": "path",
    },
    "influxdb.secret_file": {
        "level": "internal",
        "scope": "hidden",
        "type": "path",
    },
}
for _field_item in [
    item for section in _SECTIONS for item in section["fields"]
]:
    _field_item.update(_field_overrides.get(_field_item["path"], {}))

_template_section_order = {
    "config_upgrade": 1,
    "system": 2,
    "grid_meter": 3,
    "devices": 4,
    "zendure_mqtt": 5,
    "winter": 6,
    "battery_full_charge_assist": 7,
    "energy_savings": 8,
    "dashboard": 9,
    "influxdb": 10,
    "ha": 11,
}
for _section_item in _SECTIONS:
    _section_item["order"] = _template_section_order[_section_item["id"]]
_SECTIONS.sort(key=lambda item: item["order"])


def get_config_feature_sections(mode=None):
    """Return serializable config metadata, optionally filtered by Admin flow."""

    if mode not in (None, "setup", "maintenance"):
        raise ValueError("mode must be 'setup', 'maintenance', or None")
    sections = copy.deepcopy(_SECTIONS)
    if mode is None:
        return sections

    result = []
    for section in sections:
        if section["scope"] not in ("both", mode):
            continue
        section["fields"] = [
            field
            for field in section["fields"]
            if field["scope"] in ("both", mode)
        ]
        result.append(section)
    return result


def get_config_feature_field_index():
    """Return all fields keyed by their stable config path."""

    return {
        field["path"]: copy.deepcopy(field)
        for field in (
            _ROOT_FIELDS
            + [item for section in _SECTIONS for item in section["fields"]]
        )
    }


def is_secret_catalog_field(field):
    """True for a catalog field whose value must never be surfaced.

    Explicit catalog metadata, never a name guess. Admin re-exports this through
    its secret policy so both layers answer from one declaration.
    """

    return field.get("risk") == "secret" or field.get("type") == "password"


def is_editable_catalog_field(field, *, scope, allow_secret=True):
    """True for a catalog field the given workflow scope may write.

    Deprecated, hidden and read-only fields stay out of every scope. Whether a
    secret-valued field is writable differs per consumer, so it is an explicit
    argument rather than a per-module rule.
    """

    if field.get("scope") not in (scope, "both"):
        return False
    if field.get("level") == "deprecated":
        return False
    if field.get("editable") is False:
        return False
    if not allow_secret and is_secret_catalog_field(field):
        return False
    return True


def config_field_index(
    *,
    scope,
    allow_secret=True,
    prefix=None,
    flat_keys=False,
    exclude_keys=(),
    exclude_repeated=False,
    exclude_prefixes=(),
):
    """The one catalog query every Admin field index is built from.

    Filters are explicit because the consumers genuinely differ: the setup
    feature index wants full config paths, the per-device indexes want the flat
    key below ``devices[].``, and the maintenance index excludes the paths owned
    by the dedicated hardware editors. What none of them may do is re-derive
    *which* catalog fields exist or when one is editable.

    With ``prefix`` the result is keyed by the remainder of the path;
    ``flat_keys`` additionally drops nested and repeated remainders so a value
    can never reach a nested structure.
    """

    excluded = frozenset(exclude_keys)
    skipped = tuple(exclude_prefixes)
    index = {}
    for path, field in get_config_feature_field_index().items():
        if not is_editable_catalog_field(field, scope=scope, allow_secret=allow_secret):
            continue
        if exclude_repeated and "[]" in path:
            continue
        if skipped and path.startswith(skipped):
            continue
        key = path
        if prefix is not None:
            if not path.startswith(prefix):
                continue
            key = path[len(prefix) :]
            if not key:
                continue
            if flat_keys and ("." in key or "[" in key):
                continue
        if key in excluded:
            continue
        index[key] = field
    return index


def get_config_catalog():
    """Return the complete serializable catalog."""

    return {
        "root_fields": copy.deepcopy(_ROOT_FIELDS),
        "sections": copy.deepcopy(_SECTIONS),
        "grid_meter_variants": copy.deepcopy(GRID_METER_VARIANTS),
        "inverter_connection_variants": copy.deepcopy(INVERTER_CONNECTION_VARIANTS),
        "legacy_paths": copy.deepcopy(LEGACY_CONFIG_PATHS),
        "runtime_device_fields": copy.deepcopy(RUNTIME_DEVICE_FIELDS),
        "template": copy.deepcopy(_DEFAULT_TEMPLATE),
    }


def build_default_template(device_count=2):
    """Build the default manual-user template from catalog defaults."""

    if device_count < 1:
        raise ValueError("device_count must be at least 1")
    template = copy.deepcopy(_DEFAULT_TEMPLATE)
    device = template["devices"][0]
    template["devices"] = []
    for index in range(device_count):
        item = copy.deepcopy(device)
        item["name"] = f"INV_{index + 1}"
        item["ip"] = f"192.168.1.{100 + index}"
        template["devices"].append(item)
    return template


DEVICE_IDENTITY_FIELD_KEYS = ("name", "ip", "sn")


def device_common_field_keys():
    """Common (transport-independent) editable ``devices[]`` keys.

    Derived from the central catalog field index, so a future common device
    field reaches every Admin flow without another hand-written list.
    Identity/connection keys (``name``/``ip``/``sn``) stay explicitly mapped by
    each transport and are never part of the common set.
    """

    keys = []
    for path in get_config_feature_field_index():
        if not path.startswith("devices[]."):
            continue
        key = path[len("devices[].") :]
        if key not in DEVICE_IDENTITY_FIELD_KEYS:
            keys.append(key)
    return tuple(keys)


def default_device_config():
    """The template device prototype split into identity/common/comment parts.

    Returns a fresh deep copy. Sample identity values (``WR1`` /
    ``192.168.1.100`` / ``YOUR_SN``) are separated from the common tuning
    values so Admin flows can materialize real defaults without ever copying
    placeholder identity into a discovered device.
    """

    prototype = copy.deepcopy(_DEFAULT_TEMPLATE["devices"][0])
    identity = {}
    comments = {}
    common = {}
    for key, value in prototype.items():
        if str(key).startswith("_"):
            comments[key] = value
        elif key in DEVICE_IDENTITY_FIELD_KEYS:
            identity[key] = value
        else:
            common[key] = value
    return {"identity": identity, "common": common, "comments": comments}


def device_common_defaults():
    """Fresh copy of the central common tuning defaults for one device."""

    return default_device_config()["common"]


def render_default_template(device_count=2):
    """Render deterministic pretty JSON with setup-friendly root grouping."""

    template = build_default_template(device_count)
    groups = (
        ("_comment", "_comment_docs", "config_schema_version"),
        ("config_upgrade",),
        ("system",),
        ("grid_meter",),
        ("_comment_devices", "devices"),
        ("zendure_mqtt",),
        ("winter",),
        ("battery_full_charge_assist",),
        ("energy_savings",),
        ("dashboard",),
        ("influxdb",),
        ("ha",),
    )
    rendered_groups = []
    for group in groups:
        entries = []
        for key in group:
            wrapped = json.dumps({key: template[key]}, indent=2, ensure_ascii=False)
            entries.append(wrapped[2:-2])
        rendered_groups.append(",\n".join(entries))
    rendered = "{\n" + ",\n\n".join(rendered_groups) + "\n}\n"
    rendered = rendered.replace(
        '      "sn": "YOUR_SN",\n      "_comment_smart_mode":',
        '      "sn": "YOUR_SN",\n\n      "_comment_smart_mode":',
    )
    rendered = rendered.replace(
        '      "battery_kwh": 1.0,\n      "_comment_soc":',
        '      "battery_kwh": 1.0,\n\n      "_comment_soc":',
    )
    rendered = rendered.replace(
        '    "months": [\n      10,\n      11,\n      12,\n      1,\n      2,\n'
        '      3\n    ]',
        '    "months": [10, 11, 12, 1, 2, 3]',
    )
    for key in ("downsampling", "query_profiles"):
        items = template["influxdb"][key]
        compact_items = [
            "      { "
            + json.dumps(item, ensure_ascii=False)[1:-1]
            + " }"
            for item in items
        ]
        replacement = (
            f'    "{key}": [\n' + ",\n".join(compact_items) + "\n    ]"
        )
        rendered = re.sub(
            rf'    "{key}": \[\n.*?\n    \]',
            replacement,
            rendered,
            count=1,
            flags=re.DOTALL,
        )
    return rendered
