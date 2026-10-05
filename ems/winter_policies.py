# SPDX-License-Identifier: AGPL-3.0-or-later
"""Winter reserve policies: which minSoc plan applies to which kind of device.

A device belongs to one energy class -- PV only, PV with a battery, or a
battery without PV of its own -- and each class has a default policy that a
device may override. A policy is declared here, once; the config catalog, the
Admin console and the controller all read this registry.

Import-side-effect-free; must not import ``ems.config``.
"""

from dataclasses import dataclass

PV_ONLY = "pv_only"
PV_BATTERY = "pv_battery"
BATTERY_ONLY = "battery_only"
DEVICE_CLASSES = (PV_BATTERY, BATTERY_ONLY, PV_ONLY)
CONFIGURABLE_CLASSES = (PV_BATTERY, BATTERY_ONLY)

AUTO = "auto"

MAX_RAISE_ABOVE_SOC = 3

STEP_MORNING_PV = "morning_pv"
STEP_AT_HOUR = "at_hour"

SOLAR_MORNING_STEP = "solar_morning_step"
NOON_STEP = "noon_step"
NO_POLICY = "none"


@dataclass(frozen=True)
class WinterPolicy:
    """One winter reserve plan.

    ``step`` names when the daily raise happens, ``step_may_lead_soc`` whether
    the daily step may lead the SoC by up to two steps -- the risk of a
    firmware grid charge accepted for a battery with no other source -- and ``holds_export`` whether PV charges the battery before the
    house while it is below its minSoc. Every other raise leads the SoC by at
    most ``MAX_RAISE_ABOVE_SOC``.
    """

    name: str
    device_classes: tuple
    step: str | None
    step_may_lead_soc: bool
    holds_export: bool

    @property
    def step_lead(self):
        """How far the daily step may lead the SoC: two steps where that is accepted."""

        return 2 * MAX_RAISE_ABOVE_SOC if self.step_may_lead_soc else MAX_RAISE_ABOVE_SOC


POLICIES = {
    SOLAR_MORNING_STEP: WinterPolicy(
        name=SOLAR_MORNING_STEP,
        device_classes=(PV_BATTERY,),
        step=STEP_MORNING_PV,
        step_may_lead_soc=False,
        holds_export=True,
    ),
    NOON_STEP: WinterPolicy(
        name=NOON_STEP,
        device_classes=(PV_BATTERY, BATTERY_ONLY),
        step=STEP_AT_HOUR,
        step_may_lead_soc=True,
        holds_export=False,
    ),
    NO_POLICY: WinterPolicy(
        name=NO_POLICY,
        device_classes=DEVICE_CLASSES,
        step=None,
        step_may_lead_soc=False,
        holds_export=False,
    ),
}

CLASS_DEFAULT_POLICIES = {
    PV_BATTERY: SOLAR_MORNING_STEP,
    BATTERY_ONLY: NOON_STEP,
    PV_ONLY: NO_POLICY,
}


def configurable_class_defaults():
    """The class defaults ``winter.policies`` may set; PV only has nothing to choose."""

    return {name: CLASS_DEFAULT_POLICIES[name] for name in CONFIGURABLE_CLASSES}


def policy_names_for(device_class):
    """The policies a device of ``device_class`` may use, in registry order."""

    return tuple(
        name for name, policy in POLICIES.items()
        if device_class in policy.device_classes
    )


def device_policy_options():
    """Values a device's ``winter_policy`` may take: ``auto`` and every policy."""

    return (AUTO, *POLICIES)


def device_energy_class(battery_absent, configured_pv_kwp):
    """The energy class of a device.

    Battery presence comes from telemetry; a missing PV array is a statement
    only the configuration can make (``pv_kwp: 0``), because a dark or shaded
    array reads exactly like none.
    """

    if battery_absent:
        return PV_ONLY

    if _is_zero(configured_pv_kwp):
        return BATTERY_ONLY

    return PV_BATTERY


def resolve_policy(device_class, device_override=None, class_defaults=None):
    """Return ``(policy, valid)`` for a device.

    The device override wins when it names a policy for its class, then the
    configured class default, then the built-in class default. ``valid`` is
    False when a configured name was unknown or does not fit the class.
    """

    valid = True
    override = str(device_override or AUTO).strip() or AUTO

    if override != AUTO:
        policy = POLICIES.get(override)
        if policy and device_class in policy.device_classes:
            return policy, True
        valid = False

    configured = (class_defaults or {}).get(device_class)
    if configured is not None:
        policy = POLICIES.get(str(configured).strip())
        if policy and device_class in policy.device_classes:
            return policy, valid
        valid = False

    return POLICIES[CLASS_DEFAULT_POLICIES[device_class]], valid


def find_winter_policy_issues(config):
    """Return ``{code, message}`` issues for winter policies that cannot apply.

    The class is read from the configuration alone, ``pv_kwp: 0`` making a
    battery-only device; whether a battery is present is telemetry, and a
    device without one runs no winter plan whatever it names.
    """

    issues = []
    winter = config.get("winter") if isinstance(config, dict) else None
    class_defaults = winter.get("policies") if isinstance(winter, dict) else None
    if isinstance(class_defaults, dict):
        for device_class, name in class_defaults.items():
            if str(device_class).startswith("_"):
                continue
            if device_class not in CONFIGURABLE_CLASSES:
                issues.append({
                    "code": "winter_policy_device_class_unknown",
                    "message": (
                        f"winter.policies.{device_class} names no configurable device type; "
                        f"they are {', '.join(CONFIGURABLE_CLASSES)}"
                    ),
                })
                continue
            if name is None:
                continue
            if str(name).strip() not in policy_names_for(device_class):
                issues.append({
                    "code": "winter_policy_device_class",
                    "message": (
                        f"winter.policies.{device_class}: {name!r} does not fit; it may use "
                        f"{', '.join(policy_names_for(device_class))}"
                    ),
                })

    devices = config.get("devices") if isinstance(config, dict) else None
    for index, device in enumerate(devices if isinstance(devices, list) else [], start=1):
        if not isinstance(device, dict) or "winter_policy" not in device:
            continue
        name = str(device.get("winter_policy") or AUTO).strip() or AUTO
        label = str(device.get("name") or f"inverter {index}")
        if name == AUTO:
            continue
        if name not in POLICIES:
            issues.append({
                "code": "winter_policy_unknown",
                "message": f"{label}: winter policy {name!r} is unknown",
            })
            continue
        device_class = device_energy_class(False, device.get("pv_kwp"))
        if device_class not in POLICIES[name].device_classes:
            issues.append({
                "code": "winter_policy_device_class",
                "message": (
                    f"{label}: winter policy {name!r} does not fit a "
                    f"{device_class} device; it may use {', '.join(policy_names_for(device_class))}"
                ),
            })
    return issues


def _is_zero(value):
    try:
        return float(value) == 0
    except (TypeError, ValueError):
        return False
