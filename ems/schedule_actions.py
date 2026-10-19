# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a schedule rule can do: each action is one or two emsctl commands.

Every operator control point of the EMS is a runtime-state key in the
whitelist of ``ems.runtime_commands``, and every key is either an action's or
named in ``EXCLUDED_KEYS`` with the reason. A key added to the whitelist
without a decision here fails the catalogue's test, so the scheduler covers
every control point rather than those that existed when it was written.
Import-side-effect-free.
"""

import math
from dataclasses import dataclass

from ems.device_settings import SETTINGS, UNMANAGED
from ems.runtime_commands import OFFGRID_SOCKET_MODES

GROUP_LABELS = {
    "power": "Power",
    "offgrid_socket": "Off-grid socket",
    "ac_charging": "AC charging",
    "battery": "Battery",
    "control": "EMS control",
    "device_settings": "Device settings",
    "advanced": "Advanced",
}
GROUPS = tuple(GROUP_LABELS)

EXCLUDED_KEYS = {
    "system.loop_interval": (
        "a tuning value every ramp and filter is defined against, not a control target"
    ),
}

SWITCH = "switch"
WATTS = "watts"
FACTOR = "factor"
CHOICE = "choice"
CHARGE = "charge"


@dataclass(frozen=True)
class ScheduleAction:
    """One thing a switch point sets, and the emsctl command it equals."""

    id: str
    group: str
    label: str
    help: str
    scope: str
    keys: tuple
    value_type: str
    command: str = ""
    choices: tuple = ()
    per_device: bool = False
    choice_labels: tuple = ()

    def choice_label(self, choice):
        return dict(self.choice_labels).get(choice, choice)


ACTIONS = (
    ScheduleAction(
        id="total_output_limit",
        group="power",
        label="Total output limit",
        help="The most all inverters together may feed in, in watts.",
        scope="system",
        keys=("max_total_power",),
        value_type=WATTS,
        command="max-power",
    ),
    ScheduleAction(
        id="device_output_limit",
        group="power",
        label="Device output limit",
        help="The most this inverter may feed in, in watts; 0 W keeps it from feeding in.",
        scope="device",
        keys=("max_power",),
        value_type=WATTS,
        command="max-power",
        per_device=True,
    ),
    ScheduleAction(
        id="pv_priority",
        group="power",
        label="PV priority",
        help="How strongly this inverter is preferred while PV covers the home; 1.0 is even.",
        scope="device",
        keys=("pv_priority_factor",),
        value_type=FACTOR,
        command="pv-priority-factor",
        per_device=True,
    ),
    ScheduleAction(
        id="offgrid_socket",
        group="offgrid_socket",
        label="Off-grid socket",
        help="The mode of this inverter's off-grid socket.",
        scope="device",
        keys=("offgrid_socket_mode",),
        value_type=CHOICE,
        command="offgrid",
        choices=OFFGRID_SOCKET_MODES,
        per_device=True,
    ),
    ScheduleAction(
        id="charge_from_grid",
        group="ac_charging",
        label="Charge from the grid",
        help=(
            "Charges this battery from the grid at the given watts; 0 W stops the "
            "charge. The full-charge assist and maintenance still take precedence."
        ),
        scope="device",
        keys=("runtime_role", "ac_charge_power_w"),
        value_type=CHARGE,
        per_device=True,
    ),
    ScheduleAction(
        id="surplus_charging",
        group="ac_charging",
        label="Charging from surplus",
        help="Whether the EMS may charge the batteries from surplus it would otherwise export.",
        scope="ac_charge_control",
        keys=("enabled",),
        value_type=SWITCH,
    ),
    ScheduleAction(
        id="device_surplus_charging",
        group="ac_charging",
        label="Charging from surplus, this device",
        help="Whether this battery may be charged from surplus.",
        scope="device",
        keys=("ac_charge_enabled",),
        value_type=SWITCH,
        command="ac-charge",
        per_device=True,
    ),
    ScheduleAction(
        id="winter_mode",
        group="battery",
        label="Winter mode",
        help="The seasonal minimum-SoC strategy.",
        scope="winter",
        keys=("enabled",),
        value_type=SWITCH,
    ),
    ScheduleAction(
        id="ems_control",
        group="control",
        label="EMS control",
        help=(
            "Off means the EMS lets go of every inverter: each keeps its last output "
            "value. To lower the output, use the output limits."
        ),
        scope="system",
        keys=("enabled",),
        value_type=SWITCH,
    ),
    ScheduleAction(
        id="device_control",
        group="control",
        label="Device control",
        help=(
            "Off means the EMS lets go of this inverter: it keeps its last output "
            "value. To lower the output, use the device output limit."
        ),
        scope="device",
        keys=("enabled",),
        value_type=SWITCH,
        per_device=True,
    ),
    *(
        ScheduleAction(
            id=setting.key,
            group="device_settings",
            label=setting.label,
            help=setting.help,
            scope="device",
            keys=(setting.key,),
            value_type=CHOICE,
            command=setting.command,
            choices=(*setting.words, UNMANAGED),
            per_device=True,
            choice_labels=(
                *((value.word, value.label) for value in setting.values),
                (UNMANAGED, "Device decides"),
            ),
        )
        for setting in SETTINGS
    ),
    ScheduleAction(
        id="minimum_output_limit",
        group="advanced",
        label="Minimum output limit",
        help="The lowest output value the EMS writes to an inverter it controls, in watts.",
        scope="system",
        keys=("min_output_limit",),
        value_type=WATTS,
        command="min-output-limit",
    ),
)

_BY_ID = {action.id: action for action in ACTIONS}


class ScheduleActionError(ValueError):
    """An action or value a schedule rule cannot use; the message says why."""


def schedule_action(action_id):
    if not isinstance(action_id, str) or action_id not in _BY_ID:
        raise ScheduleActionError(f"unknown schedule action {action_id}")
    return _BY_ID[action_id]


def action_key_paths(action, device=None):
    """The runtime-state paths an action sets, as ``devices.WR1.max_power``."""

    holder = f"devices.{device}" if action.scope == "device" else action.scope
    return tuple(f"{holder}.{key}" for key in action.keys)


def action_slot(action, device=None):
    """The one name for the runtime keys an action sets: what "latest wins" is per."""

    return ",".join(action_key_paths(action, device))


def action_commands(action, value):
    """The emsctl commands, in order, that a switch point of ``action`` runs.

    Each is ``(command, value)`` within the action's scope; the value is
    validated by the command path when it runs, not here.
    """

    if action.value_type == SWITCH:
        if not isinstance(value, bool):
            raise ScheduleActionError(f"{action.id} takes on or off")
        if action.command == "ac-charge":
            return (("ac-charge", "on" if value else "off"),)
        return (("enable" if value else "disable", None),)
    if action.value_type == CHARGE:
        watts = _whole_watts(action, value)
        if watts == 0:
            return (("ac-mode", "output"),)
        return (("ac-charge-power", watts), ("ac-mode", "input"))
    if action.value_type == WATTS:
        return ((action.command, _whole_watts(action, value)),)
    if action.value_type == CHOICE:
        if value not in action.choices:
            raise ScheduleActionError(f"{action.id} takes one of: {', '.join(action.choices)}")
        return ((action.command, value),)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ScheduleActionError(f"{action.id} takes a number")
    return ((action.command, value),)


def _whole_watts(action, value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ScheduleActionError(f"{action.id} takes whole watts from 0")
    return value
