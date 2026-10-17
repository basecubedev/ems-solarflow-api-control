# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from ems import config as cfg
from ems.ac_charge_control import (
    CHARGE_SILENCE_CYCLES,
    OWN_DRAIN_MARGIN_W,
    REFUSAL_MODEL_UNIDENTIFIED,
    ChargeDirectionSettings,
    ChargeDirectionState,
    allocate_charge_targets,
    charge_entries_within_limit,
    charge_headroom_weight,
    count_silent_charge_cycles,
    decide_charge_direction,
    resolve_max_charge_power_w,
    unreported_charge_inputs,
)
from ems.clients import (
    fetch_all_devices,
    zero_device_state,
)
from ems.logging_utils import log_event
from ems.mqtt_control.dispatch import (
    WriteDispatchStatus,
    dispatch_device_write,
    withheld,
)
from ems.property_writes import write_device_properties
from ems.models import BATTERY_ABSENT, BATTERY_PRESENT, DeviceCapabilities
from ems.charge_record import (
    CHARGE_EXIT_FINAL,
    CHARGE_EXIT_IN_INPUT,
    CHARGE_EXIT_PENDING,
    CHARGE_EXIT_TO_OUTPUT,
    FOUND_EXIT_ATTEMPTS,
    ChargeRecord,
)
from ems.power_direction import (
    AC_MODE_INPUT,
    AC_MODE_OUTPUT,
    derive_house_load_w,
    found_in_ac_input,
    in_ac_input_direction,
    out_of_charge,
    settling_out_of_charge,
)
from ems.runtime_intents import (
    regulator_charge_intent,
    PRIORITY_MAINTENANCE,
    DeviceRuntimeIntent,
    DeviceRuntimeRole,
    ac_input_intent,
    ac_output_intent,
    UNPROVEN_CHARGE_REASON,
    FIRMWARE_CHARGE_REASON,
    firmware_charge_intent,
    is_floor_charge,
    resolve_device_intent,
    runtime_intent_from_role,
)
from ems.state_store import BatteryFullChargeStateStore
from ems.winter_policies import MAX_RAISE_ABOVE_SOC
from ems.winter_reserve import MIN_SOC_UNMANAGED, WinterReserve
from ems.target_control import (
    ControlExplanation,
    ControlLimitExplanation,
    DeviceControlExplanation,
    apply_min_output_limit,
    battery_presence,
    calculate_targets,
    detect_capabilities,
    derive_soc_runtime_state,
    firmware_recovery_or_ac_charge_active,
    pv_power,
)


FULL_CHARGE_ASSIST_REASON = "battery_full_charge_assist"
FULL_CHARGE_ASSIST_RESTORE_REASON = "battery_full_charge_assist_restore"


def finite_grid_load(value):
    """Return a finite grid reading in W; anything else holds the target (0 W).

    The load filter keeps its history, so one NaN admitted here would poison
    every later target until restart.
    """

    try:
        load = float(value)
    except (TypeError, ValueError):
        load = math.nan
    if math.isfinite(load):
        return load
    log_event(logging.WARNING, "grid_load_not_finite", value=value)
    return 0.0


CYCLE_FAILURE_WARN_EVERY = 12
CYCLE_FAILURES_BEFORE_CHARGE_RELEASE = 3


@dataclass
class FullChargeAssistCharge:
    """The assist's grid charge on one device, on a transport's charge record.

    No transport has this charge on record -- the assist writes it through
    the state reconciler -- so the controller keeps the same
    :class:`~ems.charge_record.ChargeRecord` for it, bounded as a transport's
    is for a charge only grid draw shows, and counts the exits it sent since a
    cycle with control on last found the charge held over a known ``acMode``:
    after a switch-off, by its restore over an unknown ``acMode``, or while
    cycles fail. That cap bounds the exits to a charge the device still shows.
    """

    record: ChargeRecord
    exits: int = 0

    def charge_exit_exhausted(self):
        """Whether the charge is the device's: its exits spent on grid draw alone, a window ago."""

        return self.record.exit_exhausted(time.monotonic())

    def release_charge_record(self):
        """The charge is not the EMS's from here; the record stays, closed."""

        self.record.release()


class EMSController:
    """Main EMS control loop."""

    def __init__(
        self,
        devices,
        shelly,
        sleep_enabled=True,
        runtime_state=None,
        dashboard_store=None,
        battery_full_charge_store=None,
        winter_store=None,
        influx_writer=None,
        zendure_mqtt_runtime=None,
        read_only_devices=None,
    ):
        self.devices = devices
        self.shelly = shelly
        self.sleep_enabled = sleep_enabled
        self.cycle_failures = 0
        self.cycles_through_since_release = None
        self.assist_reentries = ()
        self.assist_reentry_rate_limited = False
        self.runtime_state = runtime_state
        self.dashboard_store = dashboard_store
        self.zendure_mqtt_runtime = zendure_mqtt_runtime
        # Devices the EMS only reads (an E3/DC). They are never part of
        # ``self.devices``, so nothing that allocates, writes or reconciles can
        # reach them; the dashboard shows them beside the controlled ones.
        self.read_only_devices = read_only_devices
        # Optional native InfluxDB telemetry writer (None unless influxdb is
        # enabled). Failure-isolated and non-blocking; see ems.history.influx_writer.
        self.influx_writer = influx_writer
        self.battery_full_charge_store = (
            battery_full_charge_store
            if battery_full_charge_store is not None
            else self.build_battery_full_charge_store()
        )
        self.soc_reconcile_counter = cfg.SOC_RECONCILE_INTERVAL

        self.last_states = {}
        self.last_seen = {}
        self.last_fetch_at = None
        self.device_online = {}
        self.pv_priority_devices = None
        self.ac_mode_unknown = set()
        self.state_fields_unknown = set()
        self.commanded_total_w = None
        self.filtered_load_w = None
        self.grid_meter_holding = False
        self.control_enabled = None
        self._legacy_grid_off_mode_warned = set()
        # The total before the ramp limits it. The charge direction's exit reads
        # this, so a sudden house load does not wait for the ramp.
        self.desired_total_w = None
        self.load_history = deque(
            maxlen=cfg.safe_int(
                cfg.OUTPUT_CONTROL_CONFIG.get("median_window", 3),
                3,
                minimum=1
            )
        )
        self.commanded_device_targets = {}
        self._last_winter_active = None
        self.winter = WinterReserve(lambda: self.runtime_state, store=winter_store)
        self.min_soc_raise_waiting = {}
        self.night_min_soc_idle_active = False
        self.night_min_soc_idle_parked = set()
        self._dashboard_capabilities = []
        self._last_dashboard_publish = 0
        self._last_influx_publish = 0
        self.last_control_explanation = None
        self.runtime_intents = {}
        self.ac_input_watched = set()
        self.charges_being_left = set()
        self.full_charge_assist_charges = {}
        self.charge_direction = ChargeDirectionState()
        # What the devices that may charge could actually take, from the cycle
        # that last decided the direction, and each device's resolved ceiling.
        # Resolved once per cycle so the ramp and the clamp read one answer
        # instead of re-deriving it from telemetry they do not carry.
        self.charge_capacity_w = 0
        self.device_charge_limits = {}
        self.silent_charge_cycles = {}
        self.charge_entry_rate_limited = False
        self.charge_ceiling_unknown = set()
        self.charge_refusals_said = {}
        self.charge_blocked_by_ceiling = set()
        self.charge_capacity_below_stop = False
        for device in self.devices:
            set_observer = getattr(device, "set_dispatch_observer", None)
            if callable(set_observer):
                def observe(result, observed_device=device):
                    self._observe_deferred_write_dispatch(observed_device, result)

                set_observer(observe)

    def build_battery_full_charge_store(self):
        if not cfg.BASE_DIR:
            return None

        try:
            return BatteryFullChargeStateStore(
                cfg.battery_full_charge_state_database_path()
            )
        except Exception as e:
            log_event(
                logging.WARNING,
                "battery_full_charge_state_store_unavailable",
                error=e
            )
            return None

    def output_control_bool(self, key, default=False):
        return cfg.safe_bool(
            cfg.OUTPUT_CONTROL_CONFIG.get(key, default),
            default
        )

    def output_control_int(self, key, default=0, minimum=0):
        return cfg.safe_int(
            cfg.OUTPUT_CONTROL_CONFIG.get(key, default),
            default,
            minimum=minimum
        )

    def output_control_float(self, key, default=0.0, minimum=0.0):
        return cfg.safe_float(
            cfg.OUTPUT_CONTROL_CONFIG.get(key, default),
            default,
            minimum=minimum
        )

    def output_control_bypass_active(self, raw_load):
        import_limit = self.output_control_float(
            "large_import_bypass_w",
            600,
            minimum=0
        )
        export_limit = self.output_control_float(
            "large_export_bypass_w",
            500,
            minimum=0
        )

        return raw_load >= import_limit or raw_load <= -export_limit

    def filter_output_control_load(self, raw_load):
        if not self.output_control_bool("filter_enabled", True):
            return raw_load

        window = self.output_control_int("median_window", 3, minimum=1)

        if self.load_history.maxlen != window:
            self.load_history = deque(
                list(self.load_history)[-window:],
                maxlen=window
            )

        self.load_history.append(raw_load)
        values = sorted(self.load_history)
        mid = len(values) // 2

        if len(values) % 2:
            median = values[mid]
        else:
            median = (values[mid - 1] + values[mid]) / 2

        method = str(
            cfg.OUTPUT_CONTROL_CONFIG.get("filter_method", "median_ema")
        )

        if method != "median_ema":
            return raw_load

        alpha = min(
            1.0,
            self.output_control_float("ema_alpha", 0.65, minimum=0.0)
        )
        previous_filtered = self.filtered_load_w

        if previous_filtered is None:
            normal_filtered = median
        else:
            normal_filtered = (
                alpha * median
                + (1 - alpha) * previous_filtered
            )

        self.filtered_load_w = normal_filtered

        if not self.output_control_bool(
            "sign_change_fast_response_enabled",
            True
        ):
            return normal_filtered

        threshold = self.output_control_float(
            "sign_change_threshold_w",
            50,
            minimum=0.0
        )

        export_mismatch = raw_load <= -threshold and normal_filtered > 0
        import_mismatch = raw_load >= threshold and normal_filtered < 0

        if not export_mismatch and not import_mismatch:
            return normal_filtered

        reset_factor = min(
            1.0,
            self.output_control_float(
                "sign_change_filter_reset_factor",
                1.0,
                minimum=0.0
            )
        )
        adjusted_filtered = (
            reset_factor * raw_load
            + (1.0 - reset_factor) * normal_filtered
        )
        self.filtered_load_w = adjusted_filtered

        log_event(
            logging.DEBUG,
            "output_control_sign_change_fast_response",
            raw_load_w=round(raw_load, 1),
            previous_filtered_load_w=(
                None
                if previous_filtered is None
                else round(previous_filtered, 1)
            ),
            normal_filtered_load_w=round(normal_filtered, 1),
            adjusted_filtered_load_w=round(adjusted_filtered, 1),
            threshold_w=round(threshold, 1),
            reset_factor=round(reset_factor, 3),
            direction="export" if export_mismatch else "import"
        )

        return adjusted_filtered

    def initialize_commanded_total(self, states, max_power):
        """Seed the total from what the devices are doing.

        While charging, from what they actually feed out, never from their
        ``outputLimit``: a device parked at the standby floor at minimum SoC
        still reports that limit while delivering nothing, and seeding from it
        adds a floor that does not exist to the first desired total -- enough
        to read a marginal surplus as none and end a charge just entered.

        And less what the devices this EMS charges draw, held to the charge
        floor. A charging device feeds out nothing, so a seed of its output
        alone is zero, and the next cycle read a meter the charge itself had
        balanced as no surplus and ended the charge -- every time something
        reset the output memory, switching control back on among them.
        """

        limit_total = sum(
            state.output_limit
            for state in states
            if state.output_limit > 0
        )

        if self.charge_direction.charging:
            initial = sum(state.output for state in states) - sum(
                cfg.safe_int(getattr(state, "grid_input", 0), 0, minimum=0)
                for dev, state in zip(self.devices, states)
                if self.charge_commanded_by_ems(dev)
            )
            source = "measured_while_charging"
        elif limit_total > 0:
            initial = limit_total
            source = "output_limit"
        else:
            initial = sum(state.output for state in states)
            source = "output"

        self.commanded_total_w = max(
            self.commanded_total_floor_w(), min(max_power, initial)
        )

        log_event(
            logging.INFO,
            "output_control_state",
            initialized=True,
            initialization_source=source,
            commanded_total_w=round(self.commanded_total_w, 1),
            raw_load_w=0,
            filtered_load_w=0,
            desired_total_w=round(self.commanded_total_w, 1),
            ramped_total_w=round(self.commanded_total_w, 1)
        )

    def telemetry_max_age_seconds(self):
        """The window a device reading stays current in.

        Read here once, because two callers depend on it: the control loop slows
        its ramp outside it, and the energy statistics stop integrating outside
        it. They ask from different reference points on purpose -- see
        :meth:`telemetry_reference_time` -- but never from different numbers.
        """

        return self.output_control_float(
            "telemetry_max_age_seconds",
            10,
            minimum=0
        )

    def telemetry_reference_time(self):
        """When the last fetch cycle returned, or now if none has yet.

        The default reference for an age, because the dashboard snapshot is
        built at the end of the cycle, after the device writes. On a slow
        network those writes can outlast the staleness window, and a reading
        every device answered would then be dropped as stale. The control loop
        passes its own ``now`` instead: it asks how old a value is at the moment
        it decides a ramp, which is a different question about the same window.
        """

        return self.last_fetch_at if self.last_fetch_at else time.time()

    def telemetry_age_seconds(self, dev_name, *, now=None):
        """How long before ``now`` this device last answered, or None if never.

        ``now`` defaults to :meth:`telemetry_reference_time`, so the age is
        counted from the fetch rather than from the wall clock.
        """

        seen = self.last_seen.get(dev_name)

        if not seen:
            return None

        return (self.telemetry_reference_time() if now is None else now) - seen

    def telemetry_fresh(self, dev_name, *, now=None):
        """Whether this device's published power is still a measurement.

        A failed read does not empty the aggregates: the controller keeps
        publishing the cached state, and the control loop keeps *calculating*
        with it, for as long as it is younger than the staleness window. (It
        stops *writing* to that device at once, after a single failed read --
        a command is not a measurement.) So the statistics integrate the cached
        reading over exactly that window, and one network hiccup is not a hole.
        Past the window the value is no longer a reading, and a device that has
        never answered has no reading to cache at all.
        """

        max_age = self.telemetry_max_age_seconds()

        if max_age <= 0:
            return False

        age = self.telemetry_age_seconds(dev_name, now=now)

        if age is None:
            return False

        return age <= max_age

    def telemetry_stale(self):
        max_age = self.telemetry_max_age_seconds()

        if max_age <= 0:
            log_event(
                logging.WARNING,
                "output_control_stale_telemetry",
                device="all",
                age_s=0,
                max_age_s=max_age
            )
            return True

        now = time.time()
        stale = False

        for dev in self.devices:
            age = self.telemetry_age_seconds(dev.name, now=now)

            # A device that never answered is no ramp decision: there is
            # nothing to regulate from. telemetry_fresh judges it differently
            # on purpose.
            if age is None:
                continue

            if age > max_age:
                stale = True
                log_event(
                    logging.WARNING,
                    "output_control_stale_telemetry",
                    device=dev.name,
                    age_s=round(age, 1),
                    max_age_s=max_age
                )

        return stale

    def runtime_role_block_reason(self, dev_name):
        """Name the reservation that blocks output control on ``dev_name``."""

        intent = self.runtime_intents.get(dev_name)

        return (
            f"runtime_role_{intent.role.value}"
            if intent
            else "runtime_role_blocked"
        )

    def device_command_block_reason(self, dev):
        """Why the EMS will not command ``dev`` this cycle, or None.

        Three independent conditions, asked once, for everything that decides
        whether a write happens. What the *allocator* asks is narrower and
        deliberately so -- see :meth:`intent_filtered_capabilities`.
        """

        if not self.device_online.get(dev.name, True):
            return "offline"

        if not self.runtime_device_bool(dev.name, "enabled", True):
            return "device_disabled"

        if not self.device_output_control_allowed(dev):
            return self.runtime_role_block_reason(dev.name)

        return None

    def device_commandable(self, dev):
        """Whether the EMS will command ``dev`` at all this cycle."""

        return self.device_command_block_reason(dev) is None

    def claim_eligible_devices(self):
        """Which devices may hold the exclusive PV-first claim.

        Commandable, and on a transport whose write gate is open -- but a closed
        gate only means something while another device's gate is open. When every
        write is suppressed, by a dry run, a simulation, a replay, or an
        all-gates-off preview posture, nothing is being written anywhere and the
        gate says nothing about who should hold the claim; reading it then would
        collapse the allocation the preview exists to show.
        """

        commandable = [self.device_may_supply(dev) for dev in self.devices]

        if (
            cfg.DRY_RUN
            or cfg.SIMULATION_MODE
            or getattr(cfg.ARGS, "replay", False)
        ):
            return commandable

        gated = [
            cfg.resolve_device_write_gate(dev).gate_enabled
            for dev in self.devices
        ]

        if not any(gated):
            return commandable

        return [
            allowed and open_gate
            for allowed, open_gate in zip(commandable, gated)
        ]

    def device_active(self, dev):
        """Is the device reachable and switched on?

        The part of eligibility that has nothing to do with direction. Both
        directions need it, so it is asked once here rather than spelled out in
        each — two copies of an eligibility rule is how the two sides drift.
        """

        return bool(
            self.device_online.get(dev.name, True)
            and self.runtime_device_bool(dev.name, "enabled", True)
        )

    def device_intent_allows_command(self, dev):
        """Does whoever owns this device's AC mode leave it commandable?

        A property of the cycle rather than of the device: an operator park,
        maintenance, or a firmware-owned charge takes a device away from the
        regulator in either direction.
        """

        intent = self.runtime_intents.get(dev.name)
        return True if intent is None else intent.output_control_allowed

    def active_online_device_indexes(self):
        """Return indexes for devices that may supply the house this cycle."""

        return [
            index
            for index, dev in enumerate(self.devices)
            if self.device_may_supply(dev)
        ]

    def state_has_positive_pv(self, state):
        """Return true when any PV telemetry field is positive."""

        return pv_power(state) > 0

    def has_output_control_export_capacity(
        self,
        states,
        capabilities,
        active_indexes
    ):
        """Return true when positive load can be served by any active device."""

        for i in active_indexes:
            state = states[i]
            capability = capabilities[i]

            if capability.export_held:
                continue

            if self.state_has_positive_pv(state):
                return True

            if capability.can_discharge:
                return True

            if state.output > 0:
                return True

        return False

    def intent_filtered_capabilities(self, capabilities):
        """Return capabilities with reserved devices blocked from output control.

        Only the reservation, deliberately. An offline or operator-disabled
        device is skipped by the write path but keeps the last ``outputLimit``
        it was given, so it goes on delivering roughly its share. Taking it out
        of the allocation would hand that share to a device that is still
        running, and the plant would deliver both until the meter caught up --
        over-export, where leaving it in merely under-delivers if the device
        really has stopped.
        """

        filtered = []

        for dev, capability in zip(self.devices, capabilities):
            if not self.device_output_control_allowed(dev):
                reason = self.runtime_role_block_reason(dev.name)
            elif not self.device_may_discharge(dev):
                reason = "ac_discharge_disabled"
            else:
                filtered.append(capability)
                continue

            filtered.append(DeviceCapabilities(
                can_charge=capability.can_charge,
                can_discharge=False,
                can_export=False,
                can_ac_charge=capability.can_ac_charge,
                reason=reason
            ))

        return filtered

    def winter_filtered_capabilities(self, states, capabilities, now=None):
        """Return capabilities with devices in a winter solar charge kept from export."""

        return self.winter.filtered_capabilities(
            self.devices,
            states,
            capabilities,
            now or datetime.now(),
            online=self.device_online,
        )

    def update_grid_meter_holding(self, load):
        """Hold the commanded total while the meter serves its last value.

        Every grid-meter client returns its last good value when a read fails.
        Integrating that value each cycle would wind the commanded total up to
        ``max_total_power`` for as long as the meter is gone.
        """

        health = getattr(self.shelly, "health", None)
        holding = bool(getattr(health, "stale_used", False))
        if holding != self.grid_meter_holding:
            log_event(
                logging.WARNING if holding else logging.INFO,
                "grid_meter_unavailable_holding_target"
                if holding
                else "grid_meter_recovered",
                stale_value_w=load,
                consecutive_failures=getattr(health, "consecutive_failures", None),
                commanded_total_w=self.commanded_total_w,
            )
            self.load_history.clear()
            self.filtered_load_w = None
        self.grid_meter_holding = holding
        return holding

    def stabilized_total_target(
        self,
        raw_load,
        states,
        max_power,
        has_export_capacity=True,
        standby_total_w=0,
        active_device_count=None
    ):
        if self.commanded_total_w is None:
            self.initialize_commanded_total(states, max_power)

        filtered_load = self.filter_output_control_load(raw_load)
        desired = self.commanded_total_w
        held = False

        load_deadband = self.output_control_float(
            "load_deadband_w",
            5,
            minimum=0
        )

        if (
            raw_load > 0
            and not has_export_capacity
            # Never freeze a charge in place. The hold exists so a positive load
            # cannot ramp the discharge target up against devices that have
            # nothing to give; a charge is the opposite situation, and it is the
            # first thing that should give way when the house needs power. A
            # charging device also reports no discharge capability at night,
            # which is exactly when this would have bitten.
            and self.commanded_total_w >= 0
        ):
            clamped = min(self.commanded_total_w, standby_total_w)

            if clamped != self.commanded_total_w:
                self.commanded_total_w = clamped

            desired = self.commanded_total_w
            held = True
            log_event(
                logging.DEBUG,
                "output_control_no_export_capacity_hold",
                raw_load_w=round(raw_load, 1),
                filtered_load_w=round(filtered_load, 1),
                commanded_total_w=round(self.commanded_total_w, 1),
                standby_total_w=round(standby_total_w, 1),
                active_devices=active_device_count,
                reason="no_export_capacity"
            )
        elif abs(filtered_load) <= load_deadband:
            held = True
            log_event(
                logging.DEBUG,
                "output_control_deadband_hold",
                reason="load_deadband",
                raw_load_w=round(raw_load, 1),
                filtered_load_w=round(filtered_load, 1),
                commanded_total_w=round(self.commanded_total_w, 1),
                deadband_w=load_deadband
            )
        else:
            desired = self.commanded_total_w + filtered_load

        floor = self.commanded_total_floor_w()
        desired = max(floor, min(max_power, desired))

        target_deadband = self.output_control_float(
            "target_deadband_w",
            10,
            minimum=0
        )

        if (
            not held
            and abs(desired - self.commanded_total_w) <= target_deadband
        ):
            desired = self.commanded_total_w
            held = True
            log_event(
                logging.DEBUG,
                "output_control_deadband_hold",
                reason="target_deadband",
                raw_load_w=round(raw_load, 1),
                filtered_load_w=round(filtered_load, 1),
                commanded_total_w=round(self.commanded_total_w, 1),
                deadband_w=target_deadband
            )

        ramped = desired
        delta = desired - self.commanded_total_w
        bypass = self.output_control_bypass_active(raw_load)
        stale = self.telemetry_stale()

        if bypass:
            log_event(
                logging.DEBUG,
                "output_control_bypass",
                raw_load_w=round(raw_load, 1),
                filtered_load_w=round(filtered_load, 1)
            )

        if (
            not held
            and self.output_control_bool("ramp_enabled", True)
            and delta != 0
        ):
            if delta > 0:
                ramp_limit = self.output_control_float(
                    "ramp_up_w_per_cycle",
                    300,
                    minimum=0
                )
            else:
                ramp_limit = self.output_control_float(
                    "ramp_down_w_per_cycle",
                    500,
                    minimum=0
                )

            limited = ramp_limit > 0

            if bypass:
                ramp_limit *= self.output_control_float(
                    "bypass_ramp_multiplier",
                    1.5,
                    minimum=1.0
                )

            if stale:
                stale_limit = ramp_limit * self.output_control_float(
                    "stale_telemetry_ramp_factor",
                    0.5,
                    minimum=0.0
                )
                if self.commanded_total_w < 0 < delta:
                    stale_limit = max(
                        stale_limit, min(ramp_limit, -self.commanded_total_w)
                    )
                ramp_limit = stale_limit

            if limited and abs(delta) > ramp_limit:
                ramped = (
                    self.commanded_total_w + ramp_limit
                    if delta > 0
                    else self.commanded_total_w - ramp_limit
                )
                log_event(
                    logging.DEBUG,
                    "output_control_ramp_limited",
                    previous_total_w=round(self.commanded_total_w, 1),
                    desired_total_w=round(desired, 1),
                    ramped_total_w=round(ramped, 1),
                    ramp_limit_w=round(ramp_limit, 1)
                )

        ramped = max(floor, min(max_power, ramped))

        log_event(
            logging.DEBUG,
            "output_control_state",
            initialized=False,
            raw_load_w=round(raw_load, 1),
            filtered_load_w=round(filtered_load, 1),
            commanded_total_w=round(self.commanded_total_w, 1),
            desired_total_w=round(desired, 1),
            ramped_total_w=round(ramped, 1)
        )

        self.desired_total_w = desired
        self.commanded_total_w = ramped
        return ramped

    def apply_device_ramp(self, targets, raw_load):
        """Limit how far each device's target moves in one cycle.

        Out of the charge direction a device's discharge ramps from zero, not
        from the charge it left: the exit itself is immediate (see
        :meth:`device_charge_floor_w`), and counting the charge against the
        ramp held the first discharge after a 1000 W charge at zero while the
        total said what the house draws, which the integrator then wound up.
        """

        if not self.output_control_bool("device_ramp_enabled", True):
            adjusted_targets = []
            for dev, target in zip(self.devices, targets):
                if not self.device_output_control_allowed(dev):
                    target = 0
                self.commanded_device_targets[dev.name] = target
                adjusted_targets.append(target)
            return adjusted_targets

        bypass = self.output_control_bypass_active(raw_load)
        multiplier = (
            self.output_control_float(
                "bypass_ramp_multiplier",
                1.5,
                minimum=1.0
            )
            if bypass
            else 1.0
        )
        up_limit = (
            self.output_control_float(
                "device_ramp_up_w_per_cycle",
                250,
                minimum=0
            )
            * multiplier
        )
        down_limit = (
            self.output_control_float(
                "device_ramp_down_w_per_cycle",
                400,
                minimum=0
            )
            * multiplier
        )
        ramped_targets = []

        for dev, target in zip(self.devices, targets):
            if not self.device_output_control_allowed(dev):
                self.commanded_device_targets[dev.name] = 0
                ramped_targets.append(0)
                continue

            previous = self.commanded_device_targets.get(dev.name)

            if previous is None:
                self.commanded_device_targets[dev.name] = target
                ramped_targets.append(target)
                continue

            if previous < 0 and not self.charge_direction.charging:
                previous = 0

            delta = target - previous
            limit = up_limit if delta > 0 else down_limit
            ramped = target

            if limit > 0 and abs(delta) > limit:
                ramped = previous + limit if delta > 0 else previous - limit
                log_event(
                    logging.DEBUG,
                    "output_control_device_ramp_limited",
                    device=dev.name,
                    previous_target_w=round(previous),
                    desired_target_w=round(target),
                    ramped_target_w=round(ramped),
                    ramp_limit_w=round(limit)
                )

            ramped = max(
                self.device_charge_floor_w(dev), min(dev.max_power, ramped)
            )
            self.commanded_device_targets[dev.name] = ramped
            ramped_targets.append(ramped)

        return ramped_targets

    def log_pv_priority_change(self, explanation):
        """Record at INFO when the exclusive PV-first claim moves.

        The claim decides which devices export the whole requested total, so a
        change is worth an operator seeing. Who holds it is steady for as long
        as a battery stays full or a device stays battery-less, which is why
        the per-cycle detail stays at debug.
        """

        if explanation.mode != "pv_first":
            # The claim is only decided in the PV-first regime. Reading its
            # absence as "nobody holds it" would announce a change every time
            # a cloud pushes the plant across that boundary and back.
            return

        claimed = next(
            (
                limit.value
                for limit in explanation.limits
                if limit.name == "full_soc_pv_priority" and limit.active
            ),
            None,
        )

        try:
            indexes = json.loads(claimed) if claimed else []
        except (TypeError, ValueError):
            indexes = []

        holders = tuple(
            sorted(
                self.devices[index].name
                for index in indexes
                if 0 <= index < len(self.devices)
            )
        )

        if holders == self.pv_priority_devices:
            return

        previous = self.pv_priority_devices
        self.pv_priority_devices = holders

        if previous is None and not holders:
            return

        log_event(
            logging.INFO,
            "pv_first_priority_changed",
            devices=",".join(holders) if holders else "none",
            previous=",".join(previous) if previous else "none"
        )

    def note_control_enabled(self, enabled):
        """Start from the observed output when control is switched back on.

        The integrator, the device ramps and the charge direction keep running
        while control is disabled; re-enabling would otherwise write their
        wound-up value at once. A charge direction decided while control was
        off reached no device, so it is dropped and a charge waits for the
        entry confirmation an always-on loop waits for; the times charging was
        entered stay, so switching off and on does not reset the hourly limit.
        A charge still on the record of a device in control -- control back on
        before the device left it -- carries on, so it is not ended and
        entered again; one on a device still out of control does not count.
        """

        if enabled and self.control_enabled is False:
            self.reset_output_control_state()
            if not any(
                getattr(dev, "charge_commanded", False) is True
                and self.runtime_device_bool(dev.name, "enabled", True)
                for dev in self.devices
            ):
                self.charge_direction = ChargeDirectionState(
                    entries=self.charge_direction.entries
                )
                self.charge_capacity_w = 0
                self.device_charge_limits = {}
            log_event(logging.INFO, "control_enabled_reset_output_control")
        self.control_enabled = enabled

    def reset_output_control_state(self):
        """Reset output-control memory after a blocked operating state."""

        self.commanded_total_w = None
        self.filtered_load_w = None
        # The total before the ramp limits it. The charge direction's exit reads
        # this, so a sudden house load does not wait for the ramp.
        self.desired_total_w = None
        self.load_history.clear()
        self.commanded_device_targets = {}

    def night_min_soc_controllable_indices(self):
        """Return device indexes the night idle may park at the standby floor."""

        return [
            index
            for index, dev in enumerate(self.devices)
            if self.device_may_supply(dev)
        ]

    def state_is_strict_night_min_soc_idle(self, state):
        """Detect the exact no-PV, no-flow, min-SOC blocked idle state."""

        no_pv = (
            state.solar == 0
            and state.solar1 == 0
            and state.solar2 == 0
            and state.solar3 == 0
            and state.solar4 == 0
        )
        no_power_flow = (
            state.pack_in == 0
            and state.pack_out == 0
            and state.output == 0
        )
        # Without a battery there is nothing that could still deliver, so the
        # device never holds the plant out of idle on a SoC it does not have.
        battery_blocked = (
            battery_presence(state) == BATTERY_ABSENT
            or state.soc <= state.min_soc
            or state.soc_limit == 2
        )

        return no_pv and no_power_flow and battery_blocked

    def night_min_soc_idle_should_enter(self, states, indexes):
        """Return true when all controllable devices are strictly idle."""

        if not indexes:
            return False

        return all(
            self.state_is_strict_night_min_soc_idle(states[i])
            for i in indexes
        )

    def update_night_min_soc_idle_state(
        self,
        states,
        indexes,
        enabled,
        min_output_limit,
        charge_pending=False
    ):
        """Update night/min-SOC idle state and log transitions."""

        if not enabled or min_output_limit <= 0 or not indexes:
            if self.night_min_soc_idle_active:
                log_event(
                    logging.INFO,
                    "night_min_soc_idle_exit",
                    reason="control_unavailable",
                    enabled=enabled,
                    min_output_limit_w=min_output_limit,
                    controllable_devices=len(indexes)
                )
                self.reset_output_control_state()

            self.night_min_soc_idle_active = False
            self.night_min_soc_idle_parked.clear()
            return False

        pv_devices = [
            self.devices[i].name
            for i in indexes
            if self.state_has_positive_pv(states[i])
        ]

        if self.night_min_soc_idle_active and pv_devices:
            log_event(
                logging.INFO,
                "night_min_soc_idle_exit",
                reason="pv_returned",
                devices=",".join(pv_devices),
                min_output_limit_w=min_output_limit
            )
            self.night_min_soc_idle_active = False
            self.night_min_soc_idle_parked.clear()
            self.reset_output_control_state()
            return False

        if charge_pending:
            if self.night_min_soc_idle_active:
                log_event(
                    logging.INFO,
                    "night_min_soc_idle_exit",
                    reason="ac_charge_surplus",
                    min_output_limit_w=min_output_limit
                )
                self.night_min_soc_idle_active = False
                self.night_min_soc_idle_parked.clear()
                self.reset_output_control_state()
            return False

        should_enter = self.night_min_soc_idle_should_enter(
            states,
            indexes
        )

        if should_enter:
            if not self.night_min_soc_idle_active:
                log_event(
                    logging.INFO,
                    "night_min_soc_idle_enter",
                    devices=",".join(self.devices[i].name for i in indexes),
                    min_output_limit_w=min_output_limit
                )
                self.night_min_soc_idle_parked.clear()

            self.night_min_soc_idle_active = True
            return True

        if self.night_min_soc_idle_active:
            log_event(
                logging.INFO,
                "night_min_soc_idle_exit",
                reason="state_changed",
                min_output_limit_w=min_output_limit
            )
            self.night_min_soc_idle_active = False
            self.night_min_soc_idle_parked.clear()
            self.reset_output_control_state()

        return False

    def apply_night_min_soc_idle_control(
        self,
        states,
        indexes,
        min_output_limit
    ):
        """Park devices once and suppress further outputLimit writes.

        A device with the EMS's own charge still on record is not parked until
        it reports out of that charge: its park write is the exit, and one it
        did not apply is sent again once the transport's resend window passes.
        An AC input the EMS cannot attribute gets its one exit here too,
        whatever ``outputLimit`` the device still shows, and one the device
        answered and did not take waits for the same window. A device the idle
        does not park is still sent the exit it is owed (see
        :meth:`end_charges_the_night_idle_does_not_park`).
        """

        self.end_charges_the_night_idle_does_not_park(indexes)

        for i in indexes:
            dev = self.devices[i]
            state = states[i]
            charge_on_record = getattr(dev, "charge_commanded", False) is True
            charge_exit = self.unproven_charge_exit(dev)
            exit_owed = charge_on_record or charge_exit is not None

            if exit_owed and self.charge_exit_pending(dev):
                log_event(
                    logging.DEBUG,
                    "night_min_soc_idle_hold_skip_write",
                    device=dev.name,
                    output_limit_w=state.output_limit,
                    min_output_limit_w=min_output_limit,
                    reason=CHARGE_EXIT_PENDING
                )
                continue

            if not exit_owed and dev.name in self.night_min_soc_idle_parked:
                log_event(
                    logging.DEBUG,
                    "night_min_soc_idle_hold_skip_write",
                    device=dev.name,
                    output_limit_w=state.output_limit,
                    min_output_limit_w=min_output_limit,
                    reason="already_parked"
                )
                continue

            if not exit_owed and state.output_limit == min_output_limit:
                log_event(
                    logging.DEBUG,
                    "night_min_soc_idle_hold_skip_write",
                    device=dev.name,
                    output_limit_w=state.output_limit,
                    min_output_limit_w=min_output_limit,
                    reason="already_at_min_output_limit"
                )
                self.night_min_soc_idle_parked.add(dev.name)
                continue

            log_event(
                logging.INFO,
                "night_min_soc_idle_park_write",
                device=dev.name,
                current_output_limit_w=state.output_limit,
                target_w=min_output_limit
            )
            written = self.write_target(dev, min_output_limit, charge_exit)
            self.note_unproven_charge_exit(dev, min_output_limit, written)
            if written and not charge_on_record:
                self.night_min_soc_idle_parked.add(dev.name)

    def end_charges_the_night_idle_does_not_park(self, indexes):
        """Write the exit owed to a commandable device the night idle never parks.

        The idle parks only devices that may supply the house and ends the
        cycle before any other write path, so a device forbidden to discharge
        got neither the resend of an exit to the EMS's own charge nor its one
        exit to an AC input found at start: its charge ran on all night. Such a
        device gets the exit to idle -- ``outputLimit = 0``, the only output it
        may have -- and nothing else.
        """

        parked = set(indexes)
        for i, dev in enumerate(self.devices):
            if i in parked or not self.device_commandable(dev):
                continue
            charge_on_record = getattr(dev, "charge_commanded", False) is True
            charge_exit = self.unproven_charge_exit(dev)
            if not charge_on_record and charge_exit is None:
                continue
            if self.charge_exit_pending(dev):
                continue

            log_event(
                logging.INFO,
                "night_min_soc_idle_charge_exit_write",
                device=dev.name,
                reason=(
                    "ems_charge_on_record"
                    if charge_on_record
                    else UNPROVEN_CHARGE_REASON
                ),
            )
            written = self.write_target(dev, 0, charge_exit)
            self.note_unproven_charge_exit(dev, 0, written)

    def build_night_min_soc_idle_explanation(
        self,
        load,
        states,
        targets,
        effective_targets,
        enabled,
        max_power,
        min_output_limit,
        controllable_indexes
    ):
        """Build a dashboard explanation for the night/min-SOC idle branch."""

        devices = {}
        controllable = set(controllable_indexes)

        for i, dev in enumerate(self.devices):
            state = states[i]
            online = self.device_online.get(dev.name, True)
            target = targets[i] if i < len(targets) else 0
            effective = (
                effective_targets[i]
                if i < len(effective_targets)
                else target
            )

            block_reason = (
                self.device_command_block_reason(dev) if enabled else None
            )

            if not enabled:
                write_decision = "blocked"
                write_reason = "control_disabled"
            elif block_reason:
                write_decision = "blocked"
                write_reason = block_reason
            elif i not in controllable:
                write_decision = "blocked"
                write_reason = "not_controllable"
            elif dev.name in self.night_min_soc_idle_parked:
                write_decision = "skip"
                write_reason = "already_parked"
            elif state.output_limit == min_output_limit:
                write_decision = "skip"
                write_reason = "already_at_min_output_limit"
            else:
                write_decision = "send"
                write_reason = "park_at_min_output_limit"

            if battery_presence(state) == BATTERY_ABSENT:
                limiting_reason = "no_battery"
            elif state.soc <= state.min_soc:
                limiting_reason = "below_min_soc"
            else:
                limiting_reason = "soc_protection"

            devices[dev.name] = DeviceControlExplanation(
                device=dev.name,
                online=online,
                pv_input_w=state.solar,
                output_w=state.output,
                soc=state.soc,
                min_soc=state.min_soc,
                max_soc=state.max_soc,
                max_output_w=dev.max_power,
                pv_priority_factor=dev.pv_priority_factor,
                capacity_weight=dev.battery_kwh,
                allocated_target_w=target,
                effective_target_w=effective,
                output_limit_w=state.output_limit,
                limiting_reason=limiting_reason,
                decision_reason="night_min_soc_idle",
                write_decision=write_decision,
                write_reason=write_reason,
                command_target_w=effective,
                deadband_reference_w=state.output_limit,
                deadband_reference_source="output_limit",
                deadband_w=cfg.DEADBAND,
            )

        allocated_total = sum(targets)
        effective_total = sum(effective_targets)
        current_total = sum(state.output for state in states)

        return ControlExplanation(
            mode="night_min_soc_idle",
            requested_total_w=allocated_total,
            effective_target_total_w=effective_total,
            allocated_target_total_w=allocated_total,
            commanded_total_w=effective_total,
            devices=devices,
            limits=[
                ControlLimitExplanation(
                    "night_min_soc_idle",
                    True,
                    effective_total,
                    "battery/SOC protection idle mode is active"
                ),
                ControlLimitExplanation(
                    "min_output_limit",
                    min_output_limit > 0,
                    min_output_limit,
                    "devices are parked at the configured minimum output floor"
                ),
            ],
            notes=[
                "Battery/SOC protection idle mode is active; normal output allocation is intentionally bypassed."
            ],
            current_total_w=current_total,
            raw_requested_total_w=allocated_total,
            max_total_power_w=max_power,
            load_w=load,
            filtered_load_w=self.filtered_load_w,
            min_output_limit_w=min_output_limit,
            undistributed_target_w=0,
        )

    def effective_control_targets(self, targets, enabled, min_output_limit):
        """Return the per-device output command intent after control gates."""

        effective_targets = []

        for dev, target in zip(self.devices, targets):
            if not enabled or not self.device_commandable(dev):
                effective_targets.append(0)
                continue

            charge_floor = self.device_charge_floor_w(dev)
            if target < 0 and charge_floor < 0:
                # A charging device is not producing output, so the standby
                # output floor has nothing to say about it.
                effective_targets.append(max(charge_floor, target))
                continue

            if not self.device_may_discharge(dev):
                effective_targets.append(0)
                continue

            if min_output_limit > 0:
                target = max(target, min_output_limit)

            effective_targets.append(
                max(
                    0,
                    min(dev.max_power, target)
                )
            )

        return effective_targets

    def runtime_system_bool(self, key, default):
        if not self.runtime_state:
            return default

        return cfg.safe_bool(
            self.runtime_state.get_system(key, default),
            default
        )

    def runtime_system_int(self, key, default, minimum=0):
        if not self.runtime_state:
            return default

        return cfg.safe_int(
            self.runtime_state.get_system(key, default),
            default,
            minimum=minimum
        )

    def runtime_device_bool(self, device_name, key, default):
        if not self.runtime_state:
            return default

        return cfg.safe_bool(
            self.runtime_state.get_device(device_name, key, default),
            default
        )

    def runtime_device_int(self, device_name, key, default, minimum=0):
        if not self.runtime_state:
            return default

        return cfg.safe_int(
            self.runtime_state.get_device(device_name, key, default),
            default,
            minimum=minimum
        )

    def runtime_device_float(self, device_name, key, default, minimum=0.0):
        if not self.runtime_state:
            return default

        return cfg.safe_float(
            self.runtime_state.get_device(device_name, key, default),
            default,
            minimum=minimum
        )

    def runtime_device_str(self, device_name, key, default):
        if not self.runtime_state:
            return default

        value = self.runtime_state.get_device(device_name, key, default)
        if value is None:
            return default

        return str(value)

    def get_device_runtime_intent(self, dev, state):
        """Return the current runtime reservation intent for a device.

        A role stored in runtime state is the operator's; without one the
        device gets the default ``ac_output`` claim.
        """

        stored_role = self.runtime_device_str(dev.name, "runtime_role", "").strip()
        role = stored_role or DeviceRuntimeRole.AC_OUTPUT.value
        reason = self.runtime_device_str(
            dev.name,
            "runtime_role_reason",
            "ac_output"
        )
        intent = runtime_intent_from_role(dev.name, role, reason)

        if intent:
            intent = replace(intent, operator_role=bool(stored_role))
            if intent.role is DeviceRuntimeRole.AC_INPUT:
                return replace(
                    intent,
                    setpoint_w=self.runtime_ac_charge_setpoint(dev.name)
                )
            return intent

        log_event(
            logging.WARNING,
            "runtime_role_invalid",
            device=dev.name,
            runtime_role=role,
            reason="unknown_runtime_role"
        )
        return ac_output_intent(dev.name, "invalid_runtime_role_fallback")

    def charge_settings(self):
        return ChargeDirectionSettings(
            start_w=cfg.ac_charge_control_int("charge_start_w", 150, minimum=0),
            stop_w=cfg.ac_charge_stop_w(),
            entry_confirm_cycles=cfg.ac_charge_control_int(
                "entry_confirm_cycles", 5, minimum=1
            ),
            entry_window_cycles=cfg.ac_charge_control_int(
                "entry_window_cycles", 7, minimum=1
            ),
            max_entries_per_hour=cfg.ac_charge_control_int(
                "max_charge_entries_per_hour", 12, minimum=1
            ),
        )

    def commanded_total_floor_w(self):
        """Lower bound of the signed total.

        Zero unless the system is in charge direction, which is what keeps the
        integrator from winding down into a charge nobody decided to take.

        While charging it is the smaller of the configured cap and what the
        chargeable devices can actually accept. Commanding more than that is
        windup with a delay attached: only the deliverable part flows, the meter
        keeps reporting the unabsorbed surplus so the integrator stays pinned at
        the cap, and the exit — which reads commanded plus load — then has to
        climb the whole gap before it can fire.
        """

        if not self.charge_direction.charging:
            return 0

        return -self.usable_charge_capacity_w()

    def usable_charge_capacity_w(self):
        """What the chargeable devices can take, held to the installation cap."""

        configured = cfg.ac_charge_control_int(
            "max_total_charge_power_w", 1200, minimum=0
        )
        return min(configured, max(0, self.charge_capacity_w))

    def release_charging_devices(self, failed_cycles=None):
        """Return every device this EMS put into charge, before shutting down or after failed cycles.

        A charge outlives the process that started it: nothing in the device
        stops drawing from the grid until its own SoC ceiling does. This covers
        the clean exit only — a killed process writes nothing, and that window
        is documented rather than papered over.

        The release is the last command, so the transport sends it whatever
        its resend window or a command in flight says; inside the window it
        used to write nothing and still log the release. The event says what
        the transport took: ``ac_charge_released_on_shutdown`` when it did,
        ``ac_charge_release_failed`` when it did not, and
        ``ac_charge_release_withheld`` when the write gate kept it from the
        transport. That is a dry run or a simulation, where nothing ever
        reached a device: no charge is on the transport's record there, and
        the release runs for the regulator's target alone. Each event names
        the record the charge is known by.

        The full-charge assist's charge is on no transport's record, since the
        assist writes it through the state reconciler, so the release reads the
        controller's record of it too (:meth:`full_charge_assist_charge_held`),
        the one switching control off ends it by. Read off the transport alone,
        it ran on to a full battery after the EMS was gone (AR-1).

        ``failed_cycles`` is the release after that many failed cycles in a row
        (R1-1): the EMS goes on, so it is not the last command and keeps the
        records' resend window (:meth:`charge_release_due`), logged as
        ``ac_charge_released_after_failed_cycles``. Best effort: whatever
        raises for one device is logged as its ``ac_charge_release_failed`` and
        the others are still released. The charge direction starts afresh, its
        hourly entries kept, so a charge waits for its entry confirmation again.
        """

        for dev in self.devices:
            try:
                self.release_charging_device(dev, failed_cycles)
            except Exception as exc:
                log_event(
                    logging.WARNING,
                    "ac_charge_release_failed",
                    device=dev.name,
                    error=exc,
                    **({} if failed_cycles is None else {"failed_cycles": failed_cycles}),
                )

        self.charge_direction = ChargeDirectionState(
            entries=self.charge_direction.entries
        )
        self.charge_capacity_w = 0
        self.device_charge_limits = {}

    def release_charging_device(self, dev, failed_cycles):
        """Send ``dev`` the release if one of the EMS's records holds a charge on it."""

        regulator = self.charge_commanded_by_ems(dev)
        assist = self.full_charge_assist_charge_held(dev)
        if not regulator and not assist:
            return
        if failed_cycles is not None and not self.charge_release_due(dev, regulator):
            return

        fields = {
            "device": dev.name,
            "previous_target_w": self.commanded_device_targets.get(dev.name),
            "charge": self.released_charge(dev, regulator),
        }
        if failed_cycles is not None:
            fields["failed_cycles"] = failed_cycles
        try:
            written = self.write_target(dev, 0, CHARGE_EXIT_FINAL)
        except Exception as exc:
            written = False
            fields["error"] = exc
        if assist:
            self.note_full_charge_assist_exit(dev, written)
        if getattr(written, "status", None) is WriteDispatchStatus.WITHHELD:
            log_event(
                logging.INFO,
                "ac_charge_release_withheld",
                reason=written.reason,
                **cfg.resolve_device_write_gate(dev).as_log_fields(),
                **fields,
            )
        elif written and failed_cycles is None:
            log_event(logging.INFO, "ac_charge_released_on_shutdown", **fields)
        elif written:
            log_event(
                logging.INFO, "ac_charge_released_after_failed_cycles", **fields
            )
        else:
            log_event(logging.WARNING, "ac_charge_release_failed", **fields)
        self.commanded_device_targets[dev.name] = 0

    def charge_release_due(self, dev, regulator):
        """Whether a release that is not the last command may go out to ``dev`` now.

        By the record the charge is held on, as the exit of a switch-off is:
        the transport's or the assist's goes out again once per resend window
        while the record says the charge runs, and not to a device the last
        read did not reach; the regulator's target alone is released once,
        since the release sets it to 0.
        """

        if not self.device_online.get(dev.name, True):
            return False
        if getattr(dev, "charge_commanded", False) is True:
            return not self.charge_exit_pending(dev)
        if regulator:
            return True
        charge = self.full_charge_assist_charges[dev.name]
        return charge.record.exit_due(time.monotonic())

    def released_charge(self, dev, regulator):
        """The record a released charge is known by, for the release events."""

        if getattr(dev, "charge_commanded", False) is True:
            return "ems_charge_on_record"
        if regulator:
            return "regulator_target"
        return FULL_CHARGE_ASSIST_REASON

    def charges_held_by_ems(self):
        """The devices this EMS is charging, with the watts each is drawing.

        Named by the records the shutdown release reads:
        :meth:`charge_commanded_by_ems` and the full-charge assist's
        (:meth:`full_charge_assist_charge_held`). The draw is the commanded
        charge where the regulator still holds one, else what the device last
        reported drawing. A target behind a shut write gate (dry run,
        simulation) never reached the device, so it holds nothing.
        """

        held = {}
        for dev in self.devices:
            if not self.charge_commanded_by_ems(dev):
                if self.full_charge_assist_charge_held(dev):
                    held[dev.name] = self.last_reported_charge_w(dev)
                continue
            if (
                getattr(dev, "charge_commanded", False) is not True
                and not cfg.resolve_device_write_gate(dev).allowed
            ):
                continue
            target = self.commanded_device_targets.get(dev.name, 0)
            if target < 0:
                held[dev.name] = abs(target)
                continue
            held[dev.name] = self.last_reported_charge_w(dev)
        return held

    def last_reported_charge_w(self, dev):
        return cfg.safe_int(
            getattr(self.last_states.get(dev.name), "grid_input", 0), 0, minimum=0
        )

    def unanswered_device_intent(self, dev, previous):
        """The claim on ``dev`` for a cycle in which it did not answer.

        The claims that need no report are resolved as for a device that
        answered; the firmware's own charge, which only a report shows, is
        carried from the cycle before. Nothing is reconciled from it.
        """

        observed = (
            previous
            if previous is not None
            and previous.reason in (FIRMWARE_CHARGE_REASON, UNPROVEN_CHARGE_REASON)
            else None
        )
        return self.resolve_device_claims(
            dev,
            self.get_device_runtime_intent(dev, None),
            regulator_charge_intent(
                dev.name, ems_commanded_charge=self.charge_commanded_by_ems(dev)
            ),
            observed,
        )

    def resolve_device_claims(self, dev, runtime, regulator, observed):
        """The winning claim on ``dev``: the operator's or default, the assist's, the regulator's, an observed one.

        The assist's direction hold takes output control from the best of
        the others (:meth:`full_charge_assist_intent`).
        """

        below = resolve_device_intent([runtime, regulator, observed])
        return resolve_device_intent([
            runtime,
            self.full_charge_assist_intent(dev, below),
            regulator,
            observed,
        ])

    def charge_commanded_by_ems(self, dev):
        """Whether this EMS is charging ``dev``, by its own record.

        Two records, either is enough. The regulator steers by its last target;
        the transport knows what it last put on the wire, and that outlives
        what the target does not -- the reset of the output memory when control
        is switched back on, and the zero a device is given while it cannot be
        reached. Reading the target alone handed the EMS's own charge at the
        battery floor to the firmware claim in both cases, drawing unsupervised.

        While control or the device is switched off, the regulator goes on
        steering and its target reaches nobody, so it is no charge then: read
        as one, a stop of the EMS sent the release to a device it never
        charged, over the limit that device was holding.
        """

        return (
            (
                self.commanded_device_targets.get(dev.name, 0) < 0
                and not self.control_switched_off(dev)
            )
            or getattr(dev, "charge_commanded", False) is True
        )

    def ac_input_attributable(self, dev):
        """Whether the EMS can tell that AC input on ``dev`` is not its own charge.

        It can once it has watched the device, and for a device it could never
        have charged -- the feature or the device's switch off, or a model
        without an AC charge path -- no charge can be the EMS's own at all.
        Nor is a charge unproven on a device this process had the full-charge
        assist's charge on record for: that record owns the device's exit, with
        its own cap, open or closed (:meth:`full_charge_assist_exit_due`).
        """

        return (
            dev.name in self.ac_input_watched
            or dev.name in self.full_charge_assist_charges
            or not self.charge_permitted(dev)
        )

    def note_ac_input_provenance(self, dev_name, uncommanded, intent):
        """Remember whether the EMS can tell who put a device into AC input.

        It can once it has seen the device out of AC input, in its own charge,
        or held there by a claim that outranks the unproven one -- an
        operator's AC-input role, a maintenance routine -- and once the device
        took the exit to an AC input it could not attribute, or was asked for
        it often enough (:meth:`settle_found_ac_input_exit`). Without the
        claim, releasing a role held across a restart fired the once-per-start
        exit mid-run, where the same release without a restart got main's
        writes. It cannot tell after a restart, or while such an exit is still
        to be taken. A failed read or an offline spell changes nothing: the
        transport's record of the EMS's own charge survives it, so no charge of
        the EMS's can begin in the gap unseen. Process memory only: a regulator
        decision never goes to runtime-state.
        """

        held_by_claim = (
            intent is not None
            and intent is not uncommanded
            and intent.role is DeviceRuntimeRole.AC_INPUT
        )
        if (
            uncommanded is None
            or uncommanded.reason != UNPROVEN_CHARGE_REASON
            or held_by_claim
        ):
            self.ac_input_watched.add(dev_name)

    def write_can_end_a_charge(self, dev):
        """Whether a non-negative write to ``dev`` would end a charge it shows.

        Only then is the AC input a charging device draws the reference a target
        is measured against. A transport without state reconciliation carries
        the modes in every power command; the local API does so only for the
        EMS's own charge and for the one exit to an AC input nobody can
        attribute. Anywhere else its write is the bare ``outputLimit`` a device
        in AC input ignores, and measuring it against that device's charge
        rewrote the same value every cycle.
        """

        return (
            not getattr(dev, "supports_state_reconciliation", True)
            or self.charge_commanded_by_ems(dev)
            or self.unproven_charge_exit(dev) is not None
        )

    def unproven_charge_exit(self, dev):
        """The exit for an AC input nobody can attribute, or None.

        Owner decisions 2026-10-04: a device the EMS could have charged that it
        finds in AC input, above the floor as at it, may hold the charge of the
        process before this one -- a stop by signal leaves it running -- and gets
        the exit once per process: until it takes it, again once per resend
        window, ``FOUND_EXIT_ATTEMPTS`` times at most. The transport sends that
        exit only when asked to, so a device someone else holds in AC input
        after it keeps the plain ``outputLimit`` write.
        """

        intent = self.runtime_intents.get(dev.name)
        if intent is not None and intent.reason == UNPROVEN_CHARGE_REASON:
            return CHARGE_EXIT_TO_OUTPUT
        return None

    def note_unproven_charge_exit(self, dev, target, written):
        """An exit to an unattributed AC input that never reached a device settles it.

        With the write gate shut -- dry run, simulation, the transport's gate
        off -- the EMS cannot end a charge it finds, so the AC input is whoever
        put the device there, as before charging existed. An exit the transport
        sent, answered or held back in its resend window, settles nothing here:
        whether the device took it is its next report's to say
        (:meth:`settle_found_ac_input_exit`). Nor does one the transport could
        not deliver, which is due again at once. Which of these it was is what
        the write returned, ``withheld`` for the gate's. Read off the resend
        window a second time, an exit the transport had held back looked like
        the gate's once the window ended between the two readings, and the AC
        input was its holder's after one attempt, without a warning.
        """

        intent = self.runtime_intents.get(dev.name)
        if (
            getattr(written, "status", None) is WriteDispatchStatus.WITHHELD
            and target >= 0
            and intent is not None
            and intent.reason == UNPROVEN_CHARGE_REASON
        ):
            self.ac_input_watched.add(dev.name)

    def settle_found_ac_input_exit(self, dev, state):
        """Settle the one exit to an AC input found after a start, by the device's report.

        It counted as settled when the transport took it, so a device that
        answered it and stayed in AC input drew from the grid for good -- at
        the floor under the firmware's name. It is taken when the device
        reports it left the charge, by the transport's evidence (see
        :mod:`ems.charge_record`). Until then it goes out again once per resend
        window, ``FOUND_EXIT_ATTEMPTS`` times in all, so a firmware that puts
        its protection charge straight back is not fought for good: once the
        last window has passed, the AC input is its holder's (owner decisions
        (1) and (3) of 2026-10-04), and a warning says so once. The transport
        counts the attempts and sends none past the last, so this reading of
        the clock, earlier in the cycle than the transport's, need not agree
        with it: a window that ends between the two holds the exit back for
        that cycle, and the next one gives up.
        """

        if dev.name in self.ac_input_watched:
            return
        if getattr(dev, "found_charge_left", False) is True:
            self.ac_input_watched.add(dev.name)
            return
        exhausted = getattr(dev, "found_exit_exhausted", None)
        if (
            not callable(exhausted)
            or exhausted() is not True
            or not found_in_ac_input(state)
        ):
            return
        self.ac_input_watched.add(dev.name)
        log_event(
            logging.WARNING,
            "ac_charge_start_exit_unconfirmed",
            device=dev.name,
            attempts=dev.found_exit_attempts,
            left_to="firmware" if is_floor_charge(state) else "holder",
        )

    def leave_charge_shown_by_draw_alone(self, dev, state, holder=None):
        """Leave the EMS's charge on ``dev`` to the device once its exits are spent on grid draw alone.

        A device whose report carries no known ``acMode`` and no charging
        ``acStatus`` shows a charge only by its grid draw, and one that draws a
        few watts idle never shows it left: its transport's record stayed open
        for good, the exit due once per resend window, output targets held to
        one write per window, and a stop's release going to it. The record
        bounds those exits (see :mod:`ems.charge_record`); once the last
        window has passed, the record is released and a warning says so once.
        ``holder`` keeps the record: ``dev``'s transport, or the full-charge
        assist's charge on it (:class:`FullChargeAssistCharge`).
        """

        holder = dev if holder is None else holder
        exhausted = getattr(holder, "charge_exit_exhausted", None)
        if not callable(exhausted) or exhausted() is not True:
            return
        holder.release_charge_record()
        log_event(
            logging.WARNING,
            "ac_charge_left_to_device",
            device=dev.name,
            exits=FOUND_EXIT_ATTEMPTS,
            grid_input_w=state.grid_input,
            **({} if holder is dev else {"charge": FULL_CHARGE_ASSIST_REASON}),
        )

    def end_charges_on_disable(self, control_enabled):
        """Write the one command that ends a charge the EMS stops supervising.

        Disabling control, or a single device, means the EMS writes nothing
        more to it. A device this EMS had put into charge then went on drawing
        from the grid with nobody watching: on MQTT until its SoC ceiling, on
        the local API until the state reconciler happened to write acMode back,
        and after ``system.enabled = false`` without any write at all. The
        owner's decision of 2026-10-04 is one final command -- the exit to
        idle -- and then nothing.

        An operator park or a maintenance claim takes the device from the
        regulator the same way and is held to the same decision. A claim that
        holds the device in AC input has the charge ended by its setpoint
        alone where the transport keeps that role, so the relay is not moved
        out and back. A claim that commands a charge of its own and can write
        it takes the charge over instead: the EMS's record of it is released,
        and neither this nor a stop of the EMS ends the claim's charge.

        Keyed on the transport's record of the EMS's own charge, not on the
        regulator's target, which keeps being computed while control is off.
        The record ends when the device reports it left the charge, not when it
        answered the command, so a device that took the command and went on
        charging is sent it again once the transport's resend window has
        passed, and one that left is sent nothing more. An unreachable device
        gets it when it answers again. Runs before the night idle decides,
        which otherwise ends the cycle before any write path is reached.

        An AC input found after a start that the EMS could have charged may be
        its own charge, and the claim on it stands the state reconciler down
        until the device takes its exit. The power command skips a disabled
        device, so that exit is its final command here too (owner decisions
        (2) and (3)); without it the device got neither.
        """

        for dev in self.devices:
            if self.full_charge_assist_exit_due(dev):
                self.end_full_charge_assist_charge(dev, control_enabled)
                continue
            charge_on_record = getattr(dev, "charge_commanded", False) is True
            charge_exit = self.unproven_charge_exit(dev)
            if not charge_on_record and charge_exit is None:
                continue
            if not self.device_online.get(dev.name, True):
                continue
            intent = self.runtime_intents.get(dev.name)
            if self.claim_takes_the_charge(dev, intent):
                self.hand_charge_to_claim(dev, intent)
                continue
            ended_by = self.charge_supervision_ended_by(dev, control_enabled)
            if ended_by is None or self.charge_exit_pending(dev):
                continue

            log_event(
                logging.INFO,
                "ac_charge_ended_on_disable",
                device=dev.name,
                charge=(
                    "ems_charge_on_record"
                    if charge_on_record
                    else UNPROVEN_CHARGE_REASON
                ),
                **ended_by,
            )
            written = self.write_target(
                dev, 0, charge_exit or self.claim_charge_exit(dev, intent)
            )
            self.note_unproven_charge_exit(dev, 0, written)

    def full_charge_assist_exit_due(self, dev):
        """Whether the full-charge assist's own grid charge on ``dev`` is owed its exit now.

        The assist starts its charge through the state reconciler, so no
        transport has it on record, and with control off no reconciler writes:
        the charge ran on unwatched to a full battery and left the device in AC
        input. The owner's answer of 2026-10-10 ends it like any charge the EMS
        started, so the controller keeps the record a transport keeps
        (:class:`~ems.charge_record.ChargeRecord`). While control is on and
        the EMS holds the assist's charge -- state reconciliation allowed, the
        assist's ``acMode=1`` gone out or shown in a report under its claim
        (:meth:`full_charge_assist_held`), and the assist claiming the device
        with a setpoint, or claiming acMode 2 back, as its restore and an
        assist without its AC mode do -- the charge is on record at the
        assist's setpoint, and the record reads
        every fresh report, as a transport's does, so a clamp shown before the
        exit counts as the assist's. It reads the report before the claim puts
        the charge back on it, so an exit the device took ends the charge
        there (R14-3), and the claim does so only over a report with a known
        ``acMode``. With control on, a field the report does not carry causes
        no write (R17-1): over a report without a known ``acMode`` the running
        assist's claim holds the device only while the report shows it
        charging (``acStatus``) and otherwise writes nothing and leaves output
        control to the regulator, holding only the direction while the charge
        is on record (:meth:`full_charge_assist_intent`), and either way the
        record is left as it is -- not closed, since the charge may run on,
        and not put back on record, so an exit already out stands with its
        count; the exits still owed go out at a switch-off or a stop. After
        ``FOUND_EXIT_ATTEMPTS`` exits that only grid draw followed the charge
        is the device's, as on a transport's record
        (:meth:`leave_charge_shown_by_draw_alone`, R16-2).
        The record stays for the process once made, closed once nothing holds
        the charge: the device's AC input is then attributable
        (:meth:`ac_input_attributable`), its only exit the record's (R16-1).
        Each restore cycle with a fresh report --
        the reconciler writes acMode 2 in it -- counts as the charge's exit,
        whether that write reached the device or not, so at worst the first
        exit after a switch-off waits one resend window. A device that leaves
        the charge during the restore is released and not put back on record
        by it; one that takes the restore ends it. Over a report without a
        known ``acMode`` the reconciler writes nothing (K10), so the restore
        is owed this exit as after a switch-off, ``FOUND_EXIT_ATTEMPTS`` times
        at most (:meth:`confirm_full_charge_assist_ac_restore`); a cycle
        without a fresh report keeps the count, so an offline spell gives that
        restore no fresh three exits (K24). While failed
        cycles return the charges the record only reads the reports: the
        release is the one exit then, on its window and cap. Once control is off,
        the record is owed the exit, sent again once per resend window until
        the device reports it left the charge or shows a setpoint the assist
        never wrote, which is someone else's charge. Two things differ from a
        transport's record: a report of ``inputLimit`` 0 counts as no setpoint
        shown, since a report without one reads as 0 too, and after
        ``FOUND_EXIT_ATTEMPTS`` exits each time control goes off the charge is
        left to the device, so a firmware protection charge that shows no
        setpoint is not written to for good. An unreachable device
        gets the exit when it answers again. The record outlives the assist
        completing or being aborted while control is off; the assist, still
        due, goes on once control is back. A charge from before a restart with
        control off was never on this record: neither the running assist nor
        its restore claims the device while control is off, so it is any AC
        input found after a start (:meth:`unproven_charge_exit`), sent that
        exit where the EMS could have charged the device and otherwise left to
        it (R14-1).
        """

        if not getattr(dev, "supports_state_reconciliation", True):
            return False
        fresh = self.device_online.get(dev.name, True)
        state = self.last_states.get(dev.name) if fresh else None
        charge = self.full_charge_assist_charges.get(dev.name)
        if charge is not None and state is not None:
            self.observe_full_charge_assist_charge(charge, state)
            self.leave_charge_shown_by_draw_alone(dev, state, charge)
        if not self.control_switched_off(dev):
            if charge is not None and self.charges_returned_after_failed_cycles():
                return False
            held = self.full_charge_assist_held(dev, state)
            if held is None:
                if charge is not None and not self.full_charge_assist_stands_down(dev):
                    charge.release_charge_record()
                return False
            setpoint_w, restoring = held
            created = charge is None
            if created:
                charge = FullChargeAssistCharge(record=ChargeRecord())
                self.full_charge_assist_charges[dev.name] = charge
                charge.record.charge_sent(setpoint_w)
            if state is None:
                return False
            if created:
                self.observe_full_charge_assist_charge(charge, state)
            if not self.ac_mode_known(state):
                return (
                    restoring
                    and charge.exits < FOUND_EXIT_ATTEMPTS
                    and charge.record.exit_due(time.monotonic())
                )
            if restoring:
                charge.record.exit_sent(time.monotonic())
            else:
                charge.record.charge_sent(setpoint_w)
            charge.exits = 0
            return False
        if charge is None or state is None:
            return False
        return (
            charge.exits < FOUND_EXIT_ATTEMPTS
            and charge.record.exit_due(time.monotonic())
        )

    def full_charge_assist_held(self, dev, state):
        """``(setpoint, restoring)`` the EMS holds on ``dev``, or None.

        Only a charge the assist entered: its ``acMode=1`` went out, or a report
        showed ``acMode`` 1 under its claim, which is when it owes the restore of
        ``acMode`` (:meth:`update_full_charge_assist_ac_pending`). A claim that
        entered no AC input -- over an unknown ``acMode`` or ``inputLimit``, or
        behind a shut gate -- holds no charge.
        """

        if not self.device_state_writes_allowed(dev):
            return None
        store = self.battery_full_charge_store
        record = store.get_device_state(dev.name) if store else None
        if not record or not record.get("ac_mode_restore_pending"):
            return None
        intent = self.full_charge_assist_intent(dev)
        if intent is None:
            return None
        if intent.desired_ac_mode == AC_MODE_OUTPUT:
            return self.full_charge_assist_ac_charge_power(), True
        if intent.reason == FULL_CHARGE_ASSIST_REASON and intent.setpoint_w is not None:
            return intent.setpoint_w, False
        return None

    def full_charge_assist_stands_down(self, dev):
        """Whether the running assist can write nothing on ``dev`` for a report without a field it needs.

        Its charge record is left as it is then (R17-1): nothing was written,
        and the charge may run on. While that record is open the assist's
        claim holds the direction (:meth:`full_charge_assist_intent`).
        """

        store = self.battery_full_charge_store
        record = store.get_device_state(dev.name) if store else None
        return bool(record and record.get("full_charge_assist_active")) and (
            self.full_charge_assist_held_by(dev, record=record) == "fields_not_reported"
        )

    def full_charge_assist_holds_direction(self, dev):
        """Whether the running assist holds ``dev``'s direction only: it stands down, its charge open on record.

        Its claim then writes no ``acMode`` and keeps every claim below it
        from writing one (:meth:`full_charge_assist_intent`), and the device
        takes no regulator charge (:meth:`device_charge_allowed`).
        """

        charge = self.full_charge_assist_charges.get(dev.name)
        return (
            charge is not None
            and charge.record.open
            and self.full_charge_assist_stands_down(dev)
        )

    def full_charge_assist_charge_held(self, dev):
        """Whether the assist's charge on ``dev`` is on the controller's record, its exit still owed.

        The record :meth:`full_charge_assist_exit_due` keeps: open until the
        device reports it left the charge, and owed no exit once
        ``FOUND_EXIT_ATTEMPTS`` went out while control was off.
        """

        charge = self.full_charge_assist_charges.get(dev.name)
        return (
            charge is not None
            and charge.record.open
            and charge.exits < FOUND_EXIT_ATTEMPTS
        )

    def observe_full_charge_assist_charge(self, charge, state):
        """Let the assist's charge record read one fresh report, as a transport's does.

        A report without an ``inputLimit`` reads as 0 here, which is also the
        setpoint of a charge ended by it, so 0 counts as no setpoint shown.
        """

        charge.record.observe(
            state, setpoint_reported=bool(getattr(state, "input_limit_w", 0))
        )

    def end_full_charge_assist_charge(self, dev, control_enabled):
        """Send the exit to idle that ends the assist's charge, whatever the transport's window says.

        The window is the assist's record's (:meth:`full_charge_assist_exit_due`),
        and the transport keeps no record of this charge, so its unconditional
        exit is the one that carries the whole set. As on a transport's record,
        an exit the device answered, accepted or not, opens the window, and one
        that never reached it is due again at once.
        """

        written = self.write_target(dev, 0, CHARGE_EXIT_FINAL)
        fields = {
            "device": dev.name,
            "charge": FULL_CHARGE_ASSIST_REASON,
            **(self.charge_supervision_ended_by(dev, control_enabled) or {}),
        }
        if not self.note_full_charge_assist_exit(dev, written):
            log_event(
                logging.WARNING,
                "ac_charge_end_failed",
                delivered=False,
                **fields,
            )
            return
        write_reason = getattr(written, "reason", None)
        if getattr(written, "status", None) is WriteDispatchStatus.WITHHELD:
            log_event(
                logging.INFO,
                "ac_charge_end_withheld",
                write_reason=write_reason,
                **fields,
            )
        elif not written:
            log_event(
                logging.WARNING,
                "ac_charge_end_failed",
                delivered=True,
                write_reason=write_reason,
                **fields,
            )
        else:
            log_event(logging.INFO, "ac_charge_ended_on_disable", **fields)

    def note_full_charge_assist_exit(self, dev, written):
        """Count an exit sent to the assist's charge; return whether it reached the transport.

        As on a transport's record, an exit the device answered, accepted or
        not, opens the resend window, and one that never reached it
        (``written is False``) is due again at once.
        """

        if written is False:
            return False
        charge = self.full_charge_assist_charges[dev.name]
        charge.record.exit_sent(time.monotonic())
        charge.exits += 1
        return True

    def charge_exit_pending(self, dev):
        """Whether the exit owed to ``dev`` is out and its resend window still open.

        The exit to the EMS's own charge is, until the device reports it left;
        the one exit to AC input found after a start once the device answered
        it, accepted or refused, over MQTT while a command is in flight, and
        for good once it was answered ``FOUND_EXIT_ATTEMPTS`` times (see
        :mod:`ems.charge_record`).
        """

        if getattr(dev, "charge_commanded", False) is True:
            due = getattr(dev, "charge_exit_due", None)
        else:
            due = getattr(dev, "found_exit_due", None)
        return callable(due) and not due()

    def charge_supervision_ended_by(self, dev, control_enabled):
        """Who took ``dev`` from the regulator this cycle, as log fields, or None."""

        if not control_enabled:
            return {"reason": "control_disabled"}
        if not self.runtime_device_bool(dev.name, "enabled", True):
            return {"reason": "device_disabled"}
        intent = self.runtime_intents.get(dev.name)
        if intent is None or intent.output_control_allowed:
            return None
        return {"reason": "claimed", "claim": intent.reason}

    def claim_takes_the_charge(self, dev, intent):
        """Whether the claim holding ``dev`` takes over the charge running on it."""

        return (
            intent is not None
            and not intent.output_control_allowed
            and self.claim_writes_its_own_charge(dev, intent)
        )

    def hand_charge_to_claim(self, dev, intent):
        """Release the EMS's record of its charge to the claim that writes its own."""

        release = getattr(dev, "release_charge_record", None)
        if callable(release):
            release()
        log_event(
            logging.INFO,
            "ac_charge_handed_to_claim",
            device=dev.name,
            claim=intent.reason,
            setpoint_w=intent.setpoint_w,
        )

    def claim_charge_exit(self, dev, intent):
        """The exit for a claim that keeps the device in AC input, or None.

        Ending the EMS's charge by its setpoint leaves the device in the role
        the claim gave it. Only where the transport keeps that role: over MQTT
        nothing does, and the power command's own exit stands.
        """

        if (
            intent is not None
            and not intent.output_control_allowed
            and intent.role is DeviceRuntimeRole.AC_INPUT
            and getattr(dev, "supports_state_reconciliation", True)
        ):
            return CHARGE_EXIT_IN_INPUT
        return None

    def claim_writes_its_own_charge(self, dev, intent):
        """Whether the claim holding ``dev`` commands a charge power and can write it."""

        return (
            intent.setpoint_w is not None
            and getattr(dev, "supports_state_reconciliation", True)
            and self.device_state_writes_allowed(dev)
        )

    def device_charge_floor_w(self, dev):
        """How far negative one device's target may go right now.

        Zero unless the system is in charge direction. Without this the ramps
        would keep a device in charge for several cycles after the direction
        said stop: a per-device ramp of 400 W walking back from -900 W takes
        three loops, and every one of them draws from the grid.
        """

        if not self.charge_direction.charging:
            return 0

        return -max(0, self.device_charge_limits.get(dev.name, 0))

    def device_charge_profile(self, dev):
        """Resolve the device's hardware profile, or None.

        Transport-neutral: the HTTP client resolves its model from config and
        its own report, an MQTT control device carries a pinned one, and both
        answer through the same registry.
        """

        from ems.mqtt_control.zendure_profiles import hardware_profile_by_name

        resolve = getattr(dev, "resolved_hardware_profile", None)
        profile_id = (
            resolve() if callable(resolve) else getattr(dev, "hardware_profile", None)
        )
        return hardware_profile_by_name(profile_id) if profile_id else None

    def device_model_supports_charge(self, dev):
        """Whether the device's resolved model declares an AC charge path."""

        from ems.mqtt_control.zendure_profiles import OPERATION_CHARGE

        profile = self.device_charge_profile(dev)
        return bool(profile and profile.supports_operation(OPERATION_CHARGE))

    def device_charge_allowed(self, dev, state, capability):
        """Every permission axis for one device, intersected.

        Feature, operator, model, runtime capability, device activation, whoever
        owns the AC mode — and whether the device can absorb anything at all.
        Any one of them saying no is enough. A device whose direction the
        full-charge assist holds for its charge on record takes no charge of
        the regulator's over it (R19-1).

        Capacity belongs here and not only in the allocator: without it the
        system would enter charge direction with nothing to allocate to, and the
        integrator would wind down to its floor against a charge that never
        happens.
        """

        if not self.charge_permitted(dev):
            return False

        if not self.device_active(dev):
            return False

        if capability is not None and not capability.can_charge:
            return False

        # A device the operator parked, maintenance is using or the firmware is
        # charging is not the surplus regulator's to command.
        if not self.device_intent_allows_command(dev):
            return False

        if self.full_charge_assist_holds_direction(dev):
            return False

        if charge_headroom_weight(state, dev, capability) <= 0:
            return False

        # Something outside the EMS is taking energy out of this pack. Only a
        # charge we are not already running is refused for it: applying this to
        # a running charge ended it on a single noisy sample, and re-entry then
        # costs a full confirmation window.
        already_charging = self.charge_commanded_by_ems(dev)
        return already_charging or not self.pack_drained_from_outside(state)

    def charge_permitted(self, dev):
        """Whether the feature, the device's own switch and its model let it charge."""

        return bool(
            cfg.ac_charge_control_enabled(self.runtime_state)
            and self.runtime_device_bool(
                dev.name, "ac_charge_enabled", getattr(dev, "ac_charge_enabled", True)
            )
            and self.device_model_supports_charge(dev)
        )

    def pack_drained_from_outside(self, state):
        """Whether the pack discharges for a reason other than this EMS.

        A device feeding out under an ``outputLimit`` is following this EMS's
        own command, since the EMS is the only writer of that value. Counting
        that drain as foreign refused every charge on an installation with a
        ``min_output_limit``: the standby floor alone keeps the pack
        discharging while the surplus the charge is meant to take goes out.
        Only a drain the output accounts for is the EMS's own; what exceeds it
        by more than ``OWN_DRAIN_MARGIN_W`` goes somewhere else.
        """

        pack_in = cfg.safe_int(getattr(state, "pack_in", 0), 0, minimum=0)
        if pack_in <= 0:
            return False

        output = cfg.safe_int(getattr(state, "output", 0), 0, minimum=0)
        feeds_own_command = (
            cfg.safe_int(getattr(state, "output_limit", 0), 0, minimum=0) > 0
            and output > 0
            and pack_in <= output + OWN_DRAIN_MARGIN_W
        )
        return not feeds_own_command

    def device_charge_ceiling_w(self, dev, state):
        """One device's charge ceiling, with its model's rating as fallback.

        Bounded by ``max_power`` where the transport enforces that bound on a
        charge too, so the regulator never allocates a share that is refused
        at the write.
        """

        profile = self.device_charge_profile(dev)
        ceiling = resolve_max_charge_power_w(
            dev,
            state,
            rated_w=getattr(profile, "rated_charge_power_w", 0),
        )
        max_power = cfg.safe_int(getattr(dev, "max_power", 0), 0, minimum=0)
        if getattr(dev, "charge_bounded_by_max_power", False) and max_power > 0:
            return min(ceiling, max_power)
        return ceiling

    def resolve_charge_eligibility(self, states, capabilities):
        """Settle which devices may charge this cycle, and how much each takes.

        A permitted device with no ceiling is not chargeable: entering charge
        direction for it would leave the floor at zero and the exit firing on
        the next cycle. That is said once per device, because it is the one
        refusal an operator can fix by setting ``max_charge_power_w``. A fleet
        whose usable capacity cannot outlast the exit makes every device
        non-chargeable, for the same reason.
        """

        self.name_charge_refusals(states)
        allowed = self.chargeable_device_flags(states, capabilities)
        chargeable = []
        limits = {}
        self.charge_blocked_by_ceiling = set()
        for index, dev in enumerate(self.devices):
            ceiling = (
                self.device_charge_ceiling_w(dev, states[index])
                if allowed[index]
                else 0
            )
            if allowed[index] and ceiling <= 0:
                self.charge_blocked_by_ceiling.add(dev.name)
                if dev.name not in self.charge_ceiling_unknown:
                    self.charge_ceiling_unknown.add(dev.name)
                    reported = getattr(states[index], "charge_max_limit_w", None)
                    log_event(
                        logging.WARNING,
                        "ac_charge_ceiling_unknown",
                        device=dev.name,
                        reason=(
                            "no_reported_or_rated_charge_limit"
                            if reported is None
                            else "device_reports_zero_charge_limit"
                        ),
                    )
            elif allowed[index]:
                self.charge_ceiling_unknown.discard(dev.name)
            chargeable.append(ceiling > 0)
            limits[dev.name] = ceiling

        self.device_charge_limits = limits
        self.charge_capacity_w = sum(limits.values())
        if any(chargeable) and not self.charge_capacity_outlasts_the_exit():
            return [False] * len(chargeable)
        return chargeable

    def name_charge_refusals(self, states):
        """Say once why a device the operator lets charge never can.

        Only for what the device leaves unsaid -- see
        :func:`unreported_charge_inputs` -- and only while the operator's
        switches let it charge and it answers, so a feature that is off or a
        device that is unreachable says nothing. Said once per device and
        reason; a reason that clears is said again if it returns.
        """

        if not cfg.ac_charge_control_enabled(self.runtime_state):
            return

        for dev, state in zip(self.devices, states):
            if not self.device_active(dev) or not self.runtime_device_bool(
                dev.name, "ac_charge_enabled", getattr(dev, "ac_charge_enabled", True)
            ):
                continue

            refused = unreported_charge_inputs(state, self.device_charge_profile(dev))
            said = self.charge_refusals_said.get(dev.name, set()) & set(refused)
            for reason in refused:
                if reason in said:
                    continue
                said.add(reason)
                model = (
                    {
                        "pinned_profile": getattr(dev, "hardware_profile", None),
                        "reported_product": getattr(dev, "observed_product", None),
                    }
                    if reason == REFUSAL_MODEL_UNIDENTIFIED
                    else {}
                )
                log_event(
                    logging.WARNING,
                    "ac_charge_refused",
                    device=dev.name,
                    reason=reason,
                    **model,
                )
            self.charge_refusals_said[dev.name] = said

    def charge_capacity_outlasts_the_exit(self):
        """Whether the chargeable devices can hold a charge past the exit.

        The exit fires once the desired total rises to ``-stop_w``, and the
        floor holds the total at the usable capacity. A capacity at or below
        ``stop_w`` therefore leaves every entry on the next cycle: twelve an
        hour, then the rate limit, and nothing charged. Said once until the
        capacity is sufficient again, naming it rather than the thresholds.
        """

        usable = self.usable_charge_capacity_w()
        stop_w = self.charge_settings().stop_w
        if usable > stop_w:
            self.charge_capacity_below_stop = False
            return True

        if not self.charge_capacity_below_stop:
            self.charge_capacity_below_stop = True
            log_event(
                logging.WARNING,
                "ac_charge_capacity_below_stop",
                usable_charge_w=usable,
                charge_capacity_w=self.charge_capacity_w,
                max_total_charge_power_w=cfg.ac_charge_control_int(
                    "max_total_charge_power_w", 1200, minimum=0
                ),
                stop_w=stop_w,
            )
        return False

    def take_charge_decision(
        self,
        meter_fresh,
        *,
        commanded_total_w,
        desired_total_w,
        filtered_load_w,
        chargeable,
        releasable_output_w,
    ):
        """Decide the charge direction for this cycle; the one writer of it.

        Called from the regulating path and, while night/min-SoC idle holds,
        from the idle path. A device with no PV and an empty pack looks exactly
        like the idle's night state while the surplus it is meant to absorb is
        exported, and a decision that only ran outside the idle could never
        start that charge. In the idle nothing is commanded and the filter is
        not running, so the idle passes zero and the raw meter reading.

        Entry is judged on the surplus that is left once the chargeable
        devices stop feeding out. Their standby floor is export that vanishes
        the moment they switch direction, so counting it lowered the entry
        threshold by the floor of every device, sized the first charge past
        the real surplus into import, and ended it on the next cycle.

        A reading the meter is holding is never an entry observation, however
        young. The freshness tolerance lets a running charge survive a missed
        read; it says nothing about whether the surplus that would start one
        is still there.
        """

        if self.grid_meter_holding:
            filtered_load_w = 0
        if not meter_fresh and self.charge_direction.charging:
            log_event(
                logging.WARNING,
                "ac_charge_stopped_stale_meter",
                reason="grid_meter_reading_stale",
            )
        decision = decide_charge_direction(
            self.charge_direction,
            charging_possible=any(chargeable) and meter_fresh,
            commanded_total_w=commanded_total_w,
            desired_total_w=desired_total_w,
            filtered_load_w=filtered_load_w + max(0, releasable_output_w),
            now=time.monotonic(),
            settings=self.charge_settings(),
        )
        self.log_charge_direction(
            decision, commanded_total_w, filtered_load_w, chargeable
        )
        self.charge_direction = decision.state
        return decision

    def devices_leaving_a_charge(self, states):
        """The online devices on their way out of a charge the EMS ends, and who just left.

        A device is leaving from the cycle the EMS ends its own charge -- on
        its record until the device reports that it left -- or owes it the one
        exit to an AC input found after a start, until the current of that
        charge has run down: a device that reports the exit taken still draws
        for about two seconds (:func:`ems.power_direction.settling_out_of_charge`).
        Returns their states and whether a device stopped leaving this cycle.
        """

        leaving = []
        left = False
        for dev, state in zip(self.devices, states):
            if not self.device_online.get(dev.name, True):
                continue
            if (
                self.charge_commanded_by_ems(dev)
                or self.unproven_charge_exit(dev) is not None
            ):
                self.charges_being_left.add(dev.name)
            elif dev.name in self.charges_being_left and not (
                settling_out_of_charge(state)
                and cfg.safe_int(getattr(state, "grid_input", 0), 0) > 0
            ):
                self.charges_being_left.discard(dev.name)
                left = True
            if dev.name in self.charges_being_left:
                leaving.append(state)
        return leaving, left

    def discharge_total_leaving_a_charge(
        self,
        load,
        states,
        total,
        max_power,
        has_export_capacity=True,
        standby_total_w=0,
    ):
        """Size the discharge total by what the house draws while a charge is left.

        The integrator adds the meter to the total it commanded, and a device
        leaving a charge breaks both halves: the meter carries its AC input
        until the current stops, and it feeds out none of the discharge
        commanded to it before then. The filter kept that import for cycles
        after: on an 800 Pro 2 (2026-10-04) a 243 W charge left into a 100 W
        house was followed by 284 W and 244 W, and a restart over a 300 W
        charge sent its exit with 400 W. So while a device is leaving a
        charge, and on the cycle the last one is first seen out of it, the
        total is what the devices feed out plus the meter, less the AC input
        of the devices leaving -- the mirror of the entry, which adds back
        the output that stops when the devices switch -- and the filter starts
        again from there. Without export capacity it is held to the standby
        total, as the integrator holds it. A held meter reading sizes nothing.
        """

        if self.grid_meter_holding:
            return total
        leaving, left = self.devices_leaving_a_charge(states)
        if not leaving and not left:
            return total
        draw_w = sum(
            cfg.safe_int(getattr(state, "grid_input", 0), 0, minimum=0)
            for state in leaving
        )
        house_w = derive_house_load_w(
            sum(state.output for state in states), load, draw_w
        )
        sized = house_w if has_export_capacity else min(house_w, standby_total_w)
        self.commanded_total_w = min(max_power, sized)
        self.load_history.clear()
        self.filtered_load_w = None
        log_event(
            logging.DEBUG,
            "output_control_sized_by_house_load",
            reason="leaving_ac_charge" if leaving else "left_ac_charge",
            leaving_devices=len(leaving),
            leaving_ac_input_w=draw_w,
            raw_load_w=round(load, 1),
            house_load_w=round(house_w, 1),
            commanded_total_w=round(self.commanded_total_w, 1),
            integrated_total_w=round(total, 1),
        )
        return self.commanded_total_w

    def observe_charge_delivery(self, states):
        """Report a commanded charge that never produced any AC input.

        Most models are allowed to charge on the strength of the vendor
        catalogue rather than a measurement here, and a catalogue row can be
        wrong. The failure is quiet by nature: the command is accepted, nothing
        flows, and the surplus keeps leaving. This only observes -- it changes
        no target and refuses nothing, because the safe response to an uncertain
        row is to say so, not to override the operator's configuration.

        The telemetry just read reflects the previous cycle's command, which is
        why this runs before the new targets are calculated.
        """

        for dev, state in zip(self.devices, states):
            commanded = self.commanded_device_targets.get(dev.name, 0)
            measured = getattr(state, "grid_input", 0)
            count = count_silent_charge_cycles(
                self.silent_charge_cycles.get(dev.name, 0),
                commanded_w=commanded,
                measured_w=measured,
                online=self.device_online.get(dev.name, True),
                charging_status=getattr(state, "ac_status", 0),
            )
            self.silent_charge_cycles[dev.name] = count

            if count != CHARGE_SILENCE_CYCLES:
                continue

            profile = self.device_charge_profile(dev)
            log_event(
                logging.WARNING,
                "ac_charge_not_delivered",
                device=dev.name,
                commanded_w=round(commanded, 1),
                cycles=count,
                model=getattr(profile, "canonical_name", None),
                charge_evidence=getattr(profile, "charge_evidence", None),
            )

    def explain_charge_allocation(self, explanation, targets, chargeable):
        """Retell the decision as a charge, now that the targets are one.

        The explanation is built by the discharge allocator, which runs first
        and against a requested total of ``max(0, stabilized_total)`` -- zero
        while charging. Its *numbers* are replaced downstream, but its reasons
        are not, so the Control view answered "why is this device at -1000 W?"
        with "pv_first_allocation": the strategy that did not decide it, for a
        direction it does not describe.

        Only the wording is touched. Nothing here changes a target.
        """

        if explanation is None:
            return

        explanation.mode = "ac_charge"
        for index, dev in enumerate(self.devices):
            device_explanation = explanation.devices.get(dev.name)
            if device_explanation is None or index >= len(targets):
                continue
            if targets[index] < 0:
                device_explanation.decision_reason = "ac_charge_allocation"
            elif index < len(chargeable) and chargeable[index]:
                # Permitted, but its share was below what a direction change is
                # worth, so the charge was concentrated on the others.
                device_explanation.decision_reason = "ac_charge_share_too_small"
            elif dev.name in self.charge_blocked_by_ceiling:
                device_explanation.decision_reason = "ac_charge_no_ceiling"
            elif self.full_charge_assist_claims(dev):
                device_explanation.decision_reason = "ac_charge_held_by_full_charge_assist"
            else:
                device_explanation.decision_reason = "ac_charge_not_permitted"

    def full_charge_assist_claims(self, dev):
        """Whether the claim on ``dev`` this cycle is the full-charge assist's or its restore's.

        Every such claim keeps the device from a charge of the regulator's:
        it takes the device out of output control, or, as the direction hold,
        makes it ineligible (:meth:`device_charge_allowed`).
        """

        intent = self.runtime_intents.get(dev.name)
        return intent is not None and intent.reason in (
            FULL_CHARGE_ASSIST_REASON,
            FULL_CHARGE_ASSIST_RESTORE_REASON,
        )

    def log_charge_direction(
        self, decision, stabilized_total, filtered_load_w, chargeable
    ):
        """Log the direction decision, loudly only when it matters.

        A rate-limited entry is a warning rather than a debug line: reaching the
        cap means the thresholds do not fit the installation, and silently
        refusing would hide that. It is said **once per episode**, not once per
        cycle: the condition holds for as long as the surplus does, and a
        misconfigured band produced sixty identical lines in a two-hundred-cycle
        run, which buries every other event in the log rather than surfacing
        this one. The episode ends when the hour frees an entry again, not when
        a dip merely unconfirms the window.
        """

        fields = {
            "charging": decision.charging,
            "reason": decision.reason,
            "commanded_total_w": round(stabilized_total, 1),
            "filtered_load_w": round(filtered_load_w or 0, 1),
            "chargeable_devices": sum(1 for flag in chargeable if flag),
            "entries_last_hour": len(decision.state.entries),
        }

        if decision.rate_limited:
            if not self.charge_entry_rate_limited:
                self.charge_entry_rate_limited = True
                log_event(logging.WARNING, "ac_charge_entry_rate_limited", **fields)
            else:
                log_event(logging.DEBUG, "ac_charge_entry_rate_limited", **fields)
            return

        if len(decision.state.entries) < max(
            1, self.charge_settings().max_entries_per_hour
        ):
            self.charge_entry_rate_limited = False

        log_event(
            logging.INFO
            if decision.charging != self.charge_direction.charging
            else logging.DEBUG,
            "ac_charge_direction",
            **fields,
        )

    def grid_meter_reading_is_fresh(self):
        """Whether the load reading is a measurement rather than a held value.

        A meter client that cannot reach its hardware returns the last good
        reading and sets ``stale_used``; nothing caps how long it may do that.
        For the discharge side that is tolerable — the EMS keeps placing energy
        the operator already owns. Charging *spends*, and a held reading of
        "still exporting 900 W" is indistinguishable from a real surplus, so the
        EMS would keep drawing from the grid on a number that may be hours old.

        Tolerated for ``telemetry_max_age_seconds``, the same age the device
        telemetry is held to. A single missed read must not end a charge: doing
        that costs a full re-entry window, which is the mistake the drain guard
        already made once.
        """

        health = getattr(self.shelly, "health", None)
        if health is None or not getattr(health, "attempted", False):
            # No health tracking on this client: the reading is all there is,
            # and refusing to charge because a transport does not report health
            # would disable the feature for it entirely.
            return True

        if not getattr(health, "stale_used", False):
            return True

        max_age = self.output_control_float(
            "telemetry_max_age_seconds", 10, minimum=0
        )
        age = health.age_seconds()
        if max_age <= 0 or age is None:
            return False
        return age <= max_age

    def chargeable_device_flags(self, states, capabilities):
        return [
            self.device_charge_allowed(
                dev,
                states[index],
                capabilities[index] if index < len(capabilities) else None,
            )
            for index, dev in enumerate(self.devices)
        ]

    def device_output_control_allowed(self, dev):
        """Whether the EMS may command ``dev`` this cycle, in either direction.

        Whoever owns its AC mode right now decides that. The operator's
        standing ``ac_discharge_enabled`` is not part of it: it forbids one
        direction, which :meth:`device_may_discharge` answers.
        """

        return self.device_intent_allows_command(dev)

    def device_may_discharge(self, dev):
        """The operator's standing permission to supply the house.

        It forbids output only. A positive target becomes zero -- and is
        written, so a discharge that was running is ended -- the device takes
        no share of the output and is never parked at the standby floor, and it
        may still be charged. Blocking every write instead kept such a device
        out of a charge it was counted in, and left a running discharge running.

        A property of the device, deliberately not a claim: joining the intent
        ladder would let a higher-priority claim re-enable output on a device
        the operator forbade.
        """

        return bool(getattr(dev, "ac_discharge_enabled", True))

    def device_may_supply(self, dev):
        """Commandable, and permitted to supply the house."""

        return self.device_commandable(dev) and self.device_may_discharge(dev)

    def state_reconciliation_supported(self, dev, path):
        """Whether ``dev`` supports state reconciliation; skip explicitly if not.

        A transport without state reconciliation (MQTT control devices are
        output-only) is skipped with an explicit event and reason — never a
        silent fall-through to another transport's write path.
        """

        if getattr(dev, "supports_state_reconciliation", True):
            return True
        log_event(
            logging.DEBUG,
            "state_reconciliation_skipped",
            device=dev.name,
            path=path,
            reason="transport_state_reconciliation_unsupported",
            control_gate=getattr(dev, "control_gate", "api"),
        )
        return False

    def state_write_gate_fields(self, dev):
        """Auditable log fields for a blocked state write on ``dev``'s transport."""

        gate = cfg.resolve_state_write_gate(getattr(dev, "control_gate", "api"))
        return {
            "dry_run": cfg.DRY_RUN,
            "simulation": cfg.SIMULATION_MODE,
            "write_gate": gate.gate_name,
            "write_gate_enabled": gate.gate_enabled,
            "blocked_by": ",".join([*gate.blocked_by, *self.control_switched_off(dev)]),
            "allow_state_reconciliation_writes": (
                cfg.ALLOW_STATE_RECONCILIATION_WRITES
            ),
        }

    def control_switched_off(self, dev):
        """Which operator switch takes ``dev`` from the EMS: none, the system's, the device's."""

        switched_off = []
        if self.control_enabled is False:
            switched_off.append("control_disabled")
        if not self.runtime_device_bool(dev.name, "enabled", True):
            switched_off.append("device_disabled")
        return switched_off

    def device_state_writes_allowed(self, dev):
        """Whether a state reconciler may write to ``dev``.

        Its transport's gates must be open, and control must be on: with the EMS
        switched off, or ``dev`` taken out of its control, no reconciler writes
        (OFF-1, owner 2026-10-10: off is off). The one command that ends a
        charge the EMS started is a power command and is not held here.
        """

        return not self.control_switched_off(dev) and cfg.state_reconciliation_writes_allowed(
            getattr(dev, "control_gate", "api")
        )

    def ac_mode_intent_log_fields(self, dev, state, intent):
        return {
            "device": dev.name,
            "current_ac_mode": (
                state.ac_mode if getattr(state, "ac_mode_reported", True) else None
            ),
            "desired_ac_mode": intent.desired_ac_mode,
            "runtime_role": intent.role.value,
            "reason": intent.reason,
            "ac_status": state.ac_status,
            "soc": state.soc,
            "soc_limit": state.soc_limit,
            "output_w": state.output,
            "pack_input_w": state.pack_in,
            "output_pack_w": state.pack_out,
        }

    @staticmethod
    def ac_mode_known(state):
        """Whether ``state`` reports ``acMode`` as 1 or 2; anything else is unknown (K10)."""

        return bool(getattr(state, "ac_mode_reported", True)) and cfg.safe_int(
            getattr(state, "ac_mode", 0), 0
        ) in (AC_MODE_INPUT, AC_MODE_OUTPUT)

    def reconcile_ac_mode_intent(self, dev, state, intent):
        """Write acMode only when telemetry differs from desired runtime intent.

        A mode the report does not carry, or one other than 1 or 2, is unknown:
        only a role the operator set is written over it (K10, owner decision
        2026-10-12). Any other claim writes nothing, said once per device until
        the mode is known again. Such a claim's ``acMode=1`` goes out only with
        the charge power it commands: over an ``inputLimit`` the report does not
        carry it is not written (:meth:`reconcile_runtime_ac_charge_power`).

        Returns whether the device may be in the claimed mode: True when there
        is nothing to write or the report already shows it, and when the write
        went out, whatever its answer -- an error or a timeout can follow a
        write the device carried out, as a transport's record of a charge
        assumes. False when nothing was sent: an unknown mode or ``inputLimit``,
        a shut gate, or a device with no way to write it.
        :meth:`update_full_charge_assist_ac_pending` owes the way back from the
        assist's ``acMode=1`` on it.
        """

        if not self.state_reconciliation_supported(dev, "ac_mode_intent"):
            return True

        if intent.desired_ac_mode is None:
            return True

        fields = self.ac_mode_intent_log_fields(dev, state, intent)
        current_ac_mode = int(state.ac_mode)
        desired_ac_mode = int(intent.desired_ac_mode)
        known = self.ac_mode_known(state)

        if known:
            self.ac_mode_unknown.discard(dev.name)
            if current_ac_mode == desired_ac_mode:
                log_event(
                    logging.DEBUG,
                    "ac_mode_intent_unchanged",
                    **fields
                )
                return True
        elif not intent.operator_role:
            if dev.name not in self.ac_mode_unknown:
                self.ac_mode_unknown.add(dev.name)
                log_event(
                    logging.WARNING,
                    "unknown_ac_mode",
                    **fields
                )
            return False

        if (
            desired_ac_mode == AC_MODE_INPUT
            and intent.setpoint_w is not None
            and not intent.operator_role
            and not self.state_field_known(
                dev, state, "inputLimit", "input_limit_reported"
            )
        ):
            return False

        if not self.device_state_writes_allowed(dev):
            log_event(
                logging.INFO,
                "dry_run_ac_mode_intent_write",
                **{
                    **fields,
                    **self.state_write_gate_fields(dev),
                }
            )
            return False

        try:
            ok = write_device_properties(
                dev,
                {"acMode": desired_ac_mode},
                reason="ac_mode_intent",
                field="acMode",
                error_event="write_ac_mode_intent_error",
                log_fields=fields
            )

            if not ok:
                return getattr(ok, "status", None) is not WriteDispatchStatus.REJECTED

            log_event(
                logging.INFO,
                "write_ac_mode_intent",
                **fields
            )
            return True

        except Exception as e:
            log_event(
                logging.WARNING,
                "write_ac_mode_intent_error",
                **{
                    **fields,
                    "error": e,
                }
            )
            return True

    def runtime_ac_charge_setpoint(self, device_name):
        """Return the operator's runtime AC charge power, or None.

        None means no value is commanded: an unset, empty or invalid entry
        leaves the existing inputLimit alone rather than guessing one.
        """

        if not self.runtime_state:
            return None

        raw_value = self.runtime_state.get_device(
            device_name,
            "ac_charge_power_w",
            None
        )

        if raw_value is None or raw_value == "":
            return None

        if isinstance(raw_value, bool):
            log_event(
                logging.WARNING,
                "runtime_ac_charge_power_invalid",
                device=device_name,
                value=raw_value,
                reason="not_integer"
            )
            return None

        try:
            desired = int(str(raw_value).strip(), 10)
        except (TypeError, ValueError):
            log_event(
                logging.WARNING,
                "runtime_ac_charge_power_invalid",
                device=device_name,
                value=raw_value,
                reason="not_integer"
            )
            return None

        if desired < 0:
            log_event(
                logging.WARNING,
                "runtime_ac_charge_power_invalid",
                device=device_name,
                value=raw_value,
                reason="negative"
            )
            return None

        return desired

    def reconcile_runtime_ac_charge_power(self, dev, state, intent):
        """Write inputLimit when runtime AC input intent requests a new value.

        Over an ``inputLimit`` the report does not carry only the operator's
        charge power is written, as only the operator's role is over an
        unknown ``acMode`` (K10, K20). Any other claim's charge power and its
        ``acMode=1`` go out together or not at all: neither over an unknown
        ``inputLimit``, and the power not over an unknown ``acMode``, where
        the mode cannot be written either.
        """

        if not self.state_reconciliation_supported(dev, "runtime_ac_charge_power"):
            return True

        desired_input_limit = intent.setpoint_w
        if desired_input_limit is None:
            return True

        input_limit_known = self.state_field_known(
            dev, state, "inputLimit", "input_limit_reported"
        )
        if not intent.operator_role and not (
            input_limit_known and self.ac_mode_known(state)
        ):
            return False

        current_input_limit = cfg.safe_int(
            getattr(state, "input_limit_w", 0),
            0,
            minimum=0
        )
        fields = {
            "device": dev.name,
            "current_input_limit_w": current_input_limit,
            "desired_input_limit_w": desired_input_limit,
            "runtime_role": intent.role.value,
            "reason": intent.reason,
            "ac_mode": state.ac_mode,
            "ac_status": state.ac_status,
        }

        if current_input_limit == desired_input_limit:
            log_event(
                logging.DEBUG,
                "runtime_ac_charge_power_unchanged",
                **fields
            )
            return True

        if not self.device_state_writes_allowed(dev):
            log_event(
                logging.INFO,
                "runtime_ac_charge_power_write_skipped",
                **{
                    **fields,
                    **self.state_write_gate_fields(dev),
                }
            )
            return False

        try:
            ok = write_device_properties(
                dev,
                {"inputLimit": desired_input_limit},
                reason="runtime_ac_charge_power",
                field="inputLimit",
                error_event="runtime_ac_charge_power_write_error",
                log_fields=fields
            )

            if not ok:
                return False

            log_event(
                logging.INFO,
                "runtime_ac_charge_power_changed",
                **fields
            )
            return True

        except Exception as e:
            log_event(
                logging.WARNING,
                "runtime_ac_charge_power_write_error",
                **{
                    **fields,
                    "error": e,
                }
            )
            return False

    def full_charge_assist_config(self):
        return cfg.BATTERY_FULL_CHARGE_ASSIST_CONFIG

    def full_charge_assist_enabled(self):
        return bool(self.full_charge_assist_config().get("enabled", False))

    def full_charge_assist_has_battery(self, dev, state):
        """Return True only for telemetry-confirmed battery-backed devices."""

        return battery_presence(state) == BATTERY_PRESENT

    def parse_assist_timestamp(self, value):
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return parsed.astimezone(datetime.now().astimezone().tzinfo)

    def force_time_reached(self, now, next_due_at):
        if not next_due_at or now.date() < next_due_at.date():
            return False

        hour_text, minute_text = self.full_charge_assist_config()[
            "force_time"
        ].split(":")
        force_time = now.replace(
            hour=int(hour_text),
            minute=int(minute_text),
            second=0,
            microsecond=0
        )
        return now >= force_time

    def should_start_full_charge_assist(self, record, state, now):
        if record.get("full_charge_assist_active"):
            return False, None
        if record.get("restore_pending"):
            return False, None
        if record.get("ac_mode_restore_pending"):
            return False, None
        if int(state.soc_limit) == 1:
            return False, None

        config = self.full_charge_assist_config()
        next_due_at = self.parse_assist_timestamp(record.get("next_due_at"))
        if next_due_at is None:
            return False, None

        if self.force_time_reached(now, next_due_at):
            return True, "full_charge_assist_forced"

        window = timedelta(
            days=cfg.safe_int(config.get("assist_window_days"), 7, minimum=0)
        )
        due_soon = next_due_at is None or next_due_at <= now + window
        if (
            due_soon
            and float(state.soc) >= cfg.safe_int(
                config.get("assist_start_soc"),
                80,
                minimum=0
            )
        ):
            return True, "full_charge_assist_started"

        return False, None

    def full_charge_assist_feature_transition(self, now):
        store = self.battery_full_charge_store
        if not store:
            return {
                "current_enabled": self.full_charge_assist_enabled(),
                "first_enable": False,
                "reenabled": False,
            }

        current_enabled = self.full_charge_assist_enabled()
        previous_enabled = store.get_full_charge_feature_enabled_state()
        transition = {
            "current_enabled": current_enabled,
            "first_enable": previous_enabled is None and current_enabled,
            "reenabled": previous_enabled is False and current_enabled,
        }
        store.set_full_charge_feature_enabled_state(current_enabled, now)
        return transition

    def maybe_seed_full_charge_schedule(
        self,
        dev,
        state,
        record,
        now,
        interval_days,
        feature_transition
    ):
        store = self.battery_full_charge_store
        if not store or not feature_transition.get("current_enabled"):
            return record, False
        if store.full_charge_has_pending_state(record):
            return record, False

        first_enable = feature_transition.get("first_enable")
        reenabled = feature_transition.get("reenabled")

        if (
            first_enable
            and not record.get("last_full_charge_at")
            and not record.get("next_due_at")
        ):
            record = store.seed_full_charge_schedule(
                dev.name,
                now,
                interval_days,
                event_type="full_charge_assist_initial_state_seeded",
                message=(
                    "Initial full-charge assist schedule seeded from first "
                    "enable; assuming battery was recently full."
                ),
                state=state
            )
            log_event(
                logging.INFO,
                "full_charge_assist_initial_state_seeded",
                device=dev.name,
                next_due_at=record.get("next_due_at")
            )
            return record, True

        if reenabled:
            record = store.seed_full_charge_schedule(
                dev.name,
                now,
                interval_days,
                event_type="full_charge_assist_reenabled_schedule_seeded",
                message=(
                    "Full-charge assist schedule seeded from re-enable; "
                    "assuming battery was recently full."
                ),
                state=state
            )
            log_event(
                logging.INFO,
                "full_charge_assist_reenabled_schedule_seeded",
                device=dev.name,
                next_due_at=record.get("next_due_at")
            )
            return record, True

        return record, False

    def abort_full_charge_assist_disabled(self, dev, state, record, now):
        if not record.get("full_charge_assist_active"):
            return record
        if self.full_charge_assist_enabled():
            return record

        ac_restore_pending = bool(record.get("ac_mode_restore_pending"))
        record = self.battery_full_charge_store.update_device_state(
            dev.name,
            now,
            full_charge_assist_active=False,
            restore_pending=True,
            max_soc_request_pending=True,
            ac_input_request_pending=False,
            ac_mode_restore_pending=ac_restore_pending,
        )
        self.battery_full_charge_store.log_event(
            dev.name,
            "full_charge_assist_aborted_disabled",
            now,
            state=state
        )
        log_event(
            logging.INFO,
            "full_charge_assist_aborted_disabled",
            device=dev.name,
            ac_mode_restore_pending=ac_restore_pending
        )
        return record

    def process_battery_full_charge_assist(
        self,
        dev,
        state,
        now,
        feature_transition
    ):
        """Track and advance EMS full-charge assist for fresh telemetry.

        The assist writes ``socSet`` alone. ``minSoc`` stays with the SoC
        reconcile while it runs: the assist needs no floor to reach Max-SoC,
        and writing the configured ``min_soc`` here fought winter's target once
        per loop and raised it without a look at the SoC (K15).

        Its restore over a report without ``socSet`` cannot be written (K20)
        and stays owed, holding no device out of output control meanwhile
        (:meth:`full_charge_assist_intent`): the configured ``max_soc`` goes
        out at the first report that carries ``socSet``. A due assist starts
        only where a state reconciler may write (K25): behind a shut gate
        nothing it needs could go out, so it stays due until the gate opens,
        and on a transport without state reconciliation it never starts.
        """

        store = self.battery_full_charge_store
        if not store:
            return

        config = self.full_charge_assist_config()
        interval_days = cfg.safe_int(
            config.get("interval_days"),
            28,
            minimum=1
        )
        has_battery = self.full_charge_assist_has_battery(dev, state)
        record = store.record_observation(
            dev.name,
            state,
            has_battery,
            now,
            interval_days
        )

        if not has_battery:
            return

        if record.get("full_charge_assist_active") and int(state.soc_limit) == 1:
            record = store.mark_assist_completed(
                dev.name,
                now,
                interval_days
            )
            store.log_event(
                dev.name,
                "full_charge_assist_completed",
                now,
                state=state
            )
            log_event(
                logging.INFO,
                "full_charge_assist_completed",
                device=dev.name,
                soc=state.soc,
                soc_limit=state.soc_limit
            )

        record = self.abort_full_charge_assist_disabled(dev, state, record, now)

        record = store.get_device_state(dev.name, now)
        record, seeded = self.maybe_seed_full_charge_schedule(
            dev,
            state,
            record,
            now,
            interval_days,
            feature_transition
        )
        if seeded:
            return

        if record.get("full_charge_assist_active"):
            write_ok = self.apply_soc_limits(
                dev,
                state,
                desired_min_soc=MIN_SOC_UNMANAGED,
                desired_max_soc=100,
                reason=FULL_CHARGE_ASSIST_REASON
            )
            if write_ok:
                store.update_device_state(
                    dev.name,
                    now,
                    max_soc_request_pending=False
                )
            return

        if record.get("restore_pending"):
            write_ok = self.apply_soc_limits(
                dev,
                state,
                desired_min_soc=MIN_SOC_UNMANAGED,
                desired_max_soc=dev.max_soc,
                reason=FULL_CHARGE_ASSIST_RESTORE_REASON
            )
            if write_ok:
                store.update_device_state(
                    dev.name,
                    now,
                    restore_pending=False,
                    max_soc_request_pending=False
                )
                store.log_event(
                    dev.name, "full_charge_assist_soc_restored", now, state=state
                )
            record = store.get_device_state(dev.name, now)

        if (
            feature_transition.get("current_enabled")
            and self.full_charge_assist_held_by(dev, state, record) is None
        ):
            start, event_type = self.should_start_full_charge_assist(
                record,
                state,
                now
            )
            if start:
                ac_charge_mode = bool(config.get("enable_ac_charge_mode", True))
                record = store.mark_assist_started(
                    dev.name,
                    now,
                    ac_charge_mode
                )
                store.log_event(dev.name, event_type, now, state=state)
                log_event(
                    logging.INFO,
                    event_type,
                    device=dev.name,
                    soc=state.soc,
                    next_due_at=record.get("next_due_at"),
                    ac_charge_mode=ac_charge_mode
                )

                write_ok = self.apply_soc_limits(
                    dev,
                    state,
                    desired_min_soc=MIN_SOC_UNMANAGED,
                    desired_max_soc=100,
                    reason=FULL_CHARGE_ASSIST_REASON
                )
                if write_ok:
                    store.update_device_state(
                        dev.name,
                        now,
                        max_soc_request_pending=False
                    )

    def full_charge_assist_held_by(self, dev, state=None, record=None):
        """What keeps the assist from writing what it needs to ``dev`` now, or None.

        ``transport_state_reconciliation_unsupported`` on a transport without
        state reconciliation (MQTT control), where nothing the assist needs
        ever goes out; ``state_writes_not_allowed`` while a state write gate is
        shut or control is switched off (K25); ``fields_not_reported`` while
        the report lacks a field the assist still has to write
        (:meth:`full_charge_assist_field_unreported`). The assist's start, its
        claim and its waiting status read this. ``state`` and ``record``
        default to the device's last report and its assist record.
        """

        if not getattr(dev, "supports_state_reconciliation", True):
            return "transport_state_reconciliation_unsupported"
        if not self.device_state_writes_allowed(dev):
            return "state_writes_not_allowed"
        if state is None:
            state = self.last_states.get(dev.name)
        if record is None and self.battery_full_charge_store:
            record = self.battery_full_charge_store.get_device_state(dev.name)
        if state is not None and self.full_charge_assist_field_unreported(
            dev, state, record
        ):
            return "fields_not_reported"
        return None

    def full_charge_assist_field_unreported(self, dev, state, record):
        """Whether ``state`` lacks a field the assist still has to write.

        Nothing is written over a field the report does not carry: ``socSet``
        (K20), until the assist's ``socSet=1000`` went out, and with its AC
        charge mode ``acMode`` (K10) and ``inputLimit``, which go out together
        or not at all, until it entered AC input. A start has all of them to
        write. While the charge it entered runs and the report shows it -- a
        known ``acMode``, or without one a charging ``acStatus`` -- nothing is
        owed here: that charge is the EMS's own, on the record a switch-off or
        a stop ends it by (:meth:`full_charge_assist_charge_open`). A report
        that shows it neither way holds the device for nothing the assist
        could write (R17-1). Once the device left it, the fields are owed
        again as for an assist that never entered (R14-3).
        """

        running = bool(record and record.get("full_charge_assist_active"))
        if (
            running
            and record.get("ac_mode_restore_pending")
            and (self.ac_mode_known(state) or in_ac_input_direction(state))
            and self.full_charge_assist_charge_open(dev, state)
        ):
            return False
        if (
            not running or record.get("max_soc_request_pending")
        ) and not self.state_field_known(dev, state, "socSet", "max_soc_reported"):
            return True
        return self.full_charge_assist_ac_charge_mode() and not (
            self.ac_mode_known(state)
            and self.state_field_known(
                dev, state, "inputLimit", "input_limit_reported"
            )
        )

    def full_charge_assist_charge_open(self, dev, state):
        """Whether the charge the assist entered on ``dev`` still runs.

        By its record, as last observed: :meth:`full_charge_assist_exit_due`
        reads each report before the assist's claim puts the charge back on
        it, so an exit the device took ends it there. The record stays,
        closed, for the life of the process, so only a process that never had
        one -- after a restart -- lets ``state`` decide: the charge runs
        unless the report shows the device out of it.
        """

        charge = self.full_charge_assist_charges.get(dev.name)
        if charge is None:
            return not out_of_charge(state)
        return charge.record.open

    def full_charge_assist_max_soc(self, dev):
        """``100`` while an assist owns ``socSet`` for ``dev``, else ``None``."""

        store = self.battery_full_charge_store
        if not store:
            return None
        record = store.get_device_state(dev.name)
        if record and record.get("full_charge_assist_active"):
            return 100
        return None

    def reconcile_device_state_limits(self, dev, state, winter_active, now):
        """Periodic SoC/mode reconcile for one device.

        An active full-charge assist owns ``socSet``; writing the configured
        ``max_soc`` here flipped it back and let the firmware report
        ``socLimit == 1`` below 100 %, which completed the assist early.
        ``minSoc`` is this reconcile's alone, an assist or not: winter's
        target, or where winter plans nothing the configured ``min_soc``,
        both held to the raise rule.
        """

        desired_min_soc, _ = self.winter.reconciliation_target(
            dev,
            state,
            winter_active,
            now
        )

        self.apply_soc_limits(
            dev,
            state,
            desired_min_soc=desired_min_soc,
            desired_max_soc=self.full_charge_assist_max_soc(dev)
        )

        self.apply_device_modes(dev, state)

    def confirm_full_charge_assist_ac_restore(self, dev, state, intent, now):
        """Settle the assist's ``acMode`` restore once the device shows it done.

        A report of ``acMode`` 2 settles it. Over an unknown mode nothing writes
        ``acMode`` (K10), and the device may still sit in the assist's AC input,
        so the restore is the exit to idle on the assist's charge record
        (:meth:`full_charge_assist_exit_due`). That record reads this cycle's
        report first, then decides: settled once the device reported it left
        after an exit, or, once ``FOUND_EXIT_ATTEMPTS`` exits went out and the
        last one's resend window has passed with the device still in the
        charge, left to the device, as after a switch-off, and the record
        closes. Until then the claim
        keeps the device out of output control; after it, nothing does. A
        transport without state reconciliation never had the assist's
        ``acMode`` written, so a restore owed there -- from before the device
        moved to it -- is settled at once, unwritten.
        """

        if not self.battery_full_charge_store:
            return
        if intent.desired_ac_mode != AC_MODE_OUTPUT or intent.reason not in (
            FULL_CHARGE_ASSIST_REASON,
            FULL_CHARGE_ASSIST_RESTORE_REASON,
        ):
            return

        record = self.battery_full_charge_store.get_device_state(dev.name, now)
        if not record or not record.get("ac_mode_restore_pending"):
            return

        event = "full_charge_assist_ac_mode_restored"
        if not getattr(dev, "supports_state_reconciliation", True):
            event = "full_charge_assist_ac_mode_restore_settled"
            log_event(
                logging.INFO,
                event,
                device=dev.name,
                reason="transport_state_reconciliation_unsupported",
            )
        elif self.ac_mode_known(state):
            if int(state.ac_mode) != AC_MODE_OUTPUT:
                return
        else:
            charge = self.full_charge_assist_charges.get(dev.name)
            if charge is None:
                return
            self.observe_full_charge_assist_charge(charge, state)
            left = charge.record.open
            if left and (
                charge.exits < FOUND_EXIT_ATTEMPTS
                or not charge.record.exit_window_passed(time.monotonic())
            ):
                return
            if left:
                charge.release_charge_record()
            event = "full_charge_assist_ac_mode_restore_settled"
            log_event(
                logging.WARNING if left else logging.INFO,
                event,
                device=dev.name,
                reason="ac_mode_not_reported",
                exits=charge.exits,
                **({"left_to": "device"} if left else {}),
            )

        self.battery_full_charge_store.update_device_state(
            dev.name,
            now,
            ac_mode_restore_pending=False
        )
        self.battery_full_charge_store.log_event(
            dev.name,
            event,
            now,
            state=state
        )

    def full_charge_assist_ac_charge_power(self):
        default = cfg.BATTERY_FULL_CHARGE_ASSIST_DEFAULTS["ac_charge_power"]
        return cfg.safe_int(
            self.full_charge_assist_config().get("ac_charge_power", default),
            default,
            minimum=0
        )

    def full_charge_assist_ac_charge_mode(self):
        """Whether an active assist charges from AC now.

        Its AC mode on, and its charge not held back after failed cycles
        returned it (:meth:`assist_ac_input_withheld`): the reconciler would
        put the charge back in every cycle that reaches it, against the
        release once per resend window (R1-1).
        """

        return bool(
            self.full_charge_assist_config().get("enable_ac_charge_mode", True)
        ) and not self.assist_ac_input_withheld()

    def full_charge_assist_intent(self, dev, below=None):
        """Return the assist's claim on ``dev``, or None when it has none.

        An assist without its AC mode leaves the direction alone, except where
        it put the device into AC input before its AC mode was switched off
        (AR-1), or while failed cycles hold its charge back: it claims
        ``acMode`` 2 back, as its restore does, until the device reports it.
        While cycles fail neither claim asks for ``acMode``: the reconciler
        keeps no window, and the release after failed cycles is the way out,
        on its record's window and cap. A restore of ``socSet`` alone claims
        nothing while the device's report does not carry ``socSet``: it cannot
        be written there (K20) and waits in the store for a report that does,
        so it holds no device out of output control in the meantime. Nor does
        any restore while the state write gate is shut (K22) or control is
        switched off (R14-1, off is off): neither ``socSet`` nor ``acMode=2``
        nor the exit to idle can go out then, and the restore stays owed in
        the store until the EMS may write it again. A device in AC input after
        a restart is then any AC input found after a start. A running
        assist claims nothing while it cannot write what it needs
        (:meth:`full_charge_assist_held_by`): behind that gate or with control
        switched off (K25), on a transport without state reconciliation, or
        over a report without a field it still has to write. It stays active
        in the store and claims the device again once it can. Over such a
        report while its charge is still open on the controller's record it
        holds the direction instead (:meth:`full_charge_assist_holds_direction`),
        as the regulator's charge claim does: no ``acMode`` of its own, at its
        priority, so no claim below it -- a role the operator set included --
        writes ``acMode`` over the unknown mode into the charge on record
        (R17-1). Output control it leaves as ``below``, the best of the other
        claims (:meth:`resolve_device_claims`), has it: the regulator's under
        the default claim, nobody's under an operator park (R19-2).
        """

        store = self.battery_full_charge_store
        if not store:
            return None

        record = store.get_device_state(dev.name)
        if not record or not record.get("has_battery"):
            return None

        restore_ac_mode = (
            AC_MODE_OUTPUT
            if record.get("ac_mode_restore_pending")
            and not self.charges_returned_after_failed_cycles()
            else None
        )

        state_writes_allowed = self.device_state_writes_allowed(dev)

        if record.get("full_charge_assist_active"):
            if self.full_charge_assist_holds_direction(dev):
                return DeviceRuntimeIntent(
                    device=dev.name,
                    role=DeviceRuntimeRole.AC_INPUT,
                    reason=FULL_CHARGE_ASSIST_REASON,
                    desired_ac_mode=None,
                    output_control_allowed=(
                        below is None or below.output_control_allowed
                    ),
                    priority=PRIORITY_MAINTENANCE
                )
            if self.full_charge_assist_held_by(dev, record=record) is not None:
                return None
            if self.full_charge_assist_ac_charge_mode():
                return ac_input_intent(
                    dev.name,
                    FULL_CHARGE_ASSIST_REASON,
                    setpoint_w=self.full_charge_assist_ac_charge_power(),
                    priority=PRIORITY_MAINTENANCE
                )

            return DeviceRuntimeIntent(
                device=dev.name,
                role=DeviceRuntimeRole.AC_OUTPUT,
                reason=FULL_CHARGE_ASSIST_REASON,
                desired_ac_mode=restore_ac_mode,
                output_control_allowed=False,
                priority=PRIORITY_MAINTENANCE
            )

        if (
            record.get("ac_mode_restore_pending")
            or (
                record.get("restore_pending")
                and getattr(self.last_states.get(dev.name), "max_soc_reported", True)
            )
        ) and state_writes_allowed:
            return DeviceRuntimeIntent(
                device=dev.name,
                role=DeviceRuntimeRole.AC_OUTPUT,
                reason=FULL_CHARGE_ASSIST_RESTORE_REASON,
                desired_ac_mode=restore_ac_mode,
                output_control_allowed=False,
                priority=PRIORITY_MAINTENANCE
            )

        return None

    def update_full_charge_assist_ac_pending(self, dev, intent, write_ok, now):
        """Settle the assist's AC input request, and owe the way back from it.

        The restore of ``acMode`` is owed once the reconciler's ``acMode=1``
        went out or the report shows ``acMode`` 1 under the assist's claim
        (``write_ok``, :meth:`reconcile_ac_mode_intent`), not when the assist
        starts. A write whose answer was an error or never came is owed too:
        the device may have carried it out. One it refused costs one exit to
        idle at most, since the next report shows ``acMode`` 2 and closes the
        record that exit opened. An assist that entered no AC input owes none,
        nor does one on a transport without state reconciliation, where no
        ``acMode`` goes out. It is owed again when the assist took the device
        back into AC input after its AC mode was off for a while.
        """

        if (
            not write_ok
            or not self.battery_full_charge_store
            or intent.reason != FULL_CHARGE_ASSIST_REASON
        ):
            return

        record = self.battery_full_charge_store.get_device_state(dev.name, now)
        if not record:
            return

        changes = {}
        if record.get("ac_input_request_pending"):
            changes["ac_input_request_pending"] = False
        if (
            intent.desired_ac_mode == AC_MODE_INPUT
            and getattr(dev, "supports_state_reconciliation", True)
            and not record.get("ac_mode_restore_pending")
        ):
            changes["ac_mode_restore_pending"] = True
        if changes:
            self.battery_full_charge_store.update_device_state(
                dev.name, now, **changes
            )

    def write_target(self, dev, value, charge_exit=None):
        """``set_output_limit``, naming an exit's shape only where one was decided."""

        if charge_exit is None:
            return self.set_output_limit(dev, value)
        return self.set_output_limit(dev, value, charge_exit=charge_exit)

    def retire_pending_output_limit(self, dev, reason):
        """Tell a transport with a queued target that this cycle skips it."""

        cancel = getattr(dev, "cancel_pending_output_limit", None)
        if callable(cancel):
            cancel(reason)

    def set_output_limit(self, dev, value, charge_exit=None):
        """Write output limit to the device via its transport, behind its gate.

        Returns the transport's dispatch result, false only when the write was
        attempted and not accepted, so a caller that writes once (night/minSoC
        park) knows to try again; with the gate shut, a ``withheld`` one, true
        as well. ``charge_exit`` names the shape of an exit from a charge the
        controller decided on (see :mod:`ems.charge_record`).
        """

        gate = cfg.resolve_device_write_gate(dev)

        if not gate.allowed:
            log_event(
                logging.INFO,
                "dry_run_output_limit",
                device=dev.name,
                target_w=value,
                control_gate=gate.transport,
                dry_run=cfg.DRY_RUN,
                simulation=cfg.SIMULATION_MODE,
                **gate.as_log_fields(),
            )
            return withheld(value)

        try:
            result = dispatch_device_write(dev, int(value), charge_exit=charge_exit)
        except Exception as e:
            log_event(
                logging.WARNING,
                "write_output_limit_error",
                device=dev.name,
                error=e
            )
            return False

        self._log_write_dispatch(dev, gate, result)
        return result

    def _log_write_dispatch(self, dev, gate, result):
        """Log a structured write dispatch under its honest, distinct event.

        A queued or coalesced target was NOT published to hardware, so it never
        borrows the ``write_output_limit_published`` event. Credentials never
        appear here — only the device name, target and gate metadata.
        """

        common = {
            "device": dev.name,
            "target_w": result.target_w,
            "control_gate": gate.transport,
            "write_gate": gate.gate_name,
        }
        if result.correlation_id is not None:
            common["correlation_id"] = result.correlation_id
        status = result.status
        if status is WriteDispatchStatus.PUBLISHED:
            log_event(
                logging.INFO,
                "write_output_limit_published",
                message_id=result.message_id,
                command_state=result.command_state,
                **common,
            )
        elif status is WriteDispatchStatus.QUEUED_LATEST:
            log_event(
                logging.INFO,
                "write_output_limit_queued",
                command_state=result.command_state,
                **common,
            )
        elif status is WriteDispatchStatus.COALESCED_ACTIVE:
            log_event(
                logging.DEBUG,
                "write_output_limit_coalesced",
                command_state=result.command_state,
                **common,
            )
        elif status is WriteDispatchStatus.SUPERSEDED:
            log_event(
                logging.INFO,
                "write_output_limit_superseded",
                reason=result.reason,
                **common,
            )
        elif status is WriteDispatchStatus.REJECTED:
            log_event(
                logging.WARNING,
                "write_output_limit_rejected",
                reason=result.reason,
                **common,
            )
        else:
            # A transport failure is already surfaced by the transport/health
            # layer (and, for HTTP, its own error event), so the controller keeps
            # only a debug breadcrumb — no duplicate WARNING mislabeled as a
            # rejection.
            log_event(
                logging.DEBUG,
                "write_output_limit_failed",
                reason=result.reason,
                **common,
            )

    def _observe_deferred_write_dispatch(self, dev, result):
        """Receive the later outcome of a target initially logged as queued."""

        gate = cfg.resolve_device_write_gate(dev)
        self._log_write_dispatch(dev, gate, result)

    def apply_soc_limits(
        self,
        dev,
        state,
        desired_min_soc=None,
        desired_max_soc=None,
        reason="soc_reconcile"
    ):
        """Write the bounds of the SoC window that differ from the report, and only those.

        ``minSoc`` is what ``min_soc_to_write`` allows; ``MIN_SOC_UNMANAGED``
        as ``desired_min_soc`` leaves it alone. ``socSet`` is
        ``desired_max_soc``, or the configured ``max_soc``, and is not written
        over a ``socSet`` the report does not carry (K20); a call that wanted
        only that write then returns False. A bound the report already shows
        is not sent again with the other: an unchanged ``minSoc`` written
        onto the SoC is what makes an 800 Pro 2 draw from the grid (K21).
        """

        if not self.state_reconciliation_supported(dev, "soc_limits"):
            return True

        if battery_presence(state) == BATTERY_ABSENT:
            log_event(
                logging.DEBUG,
                "soc_limits_not_applicable",
                device=dev.name,
                reason="no_battery"
            )
            return True

        configured_max_soc = cfg.safe_soc_limit(dev.max_soc)
        effective_min_soc = self.min_soc_to_write(dev, state, desired_min_soc)
        effective_max_soc = (
            cfg.safe_soc_limit(
                cfg.safe_int(desired_max_soc, configured_max_soc, minimum=0)
            )
            if desired_max_soc is not None
            else configured_max_soc
        )

        max_soc_known = effective_max_soc <= 0 or self.state_field_known(
            dev, state, "socSet", "max_soc_reported"
        )
        managed = effective_min_soc > 0 or (effective_max_soc > 0 and max_soc_known)
        changes = {}
        written = {}
        if effective_min_soc > 0 and int(state.min_soc) != int(effective_min_soc):
            changes["minSoc"] = int(effective_min_soc * 10)
            written["min_soc"] = effective_min_soc
        if (
            effective_max_soc > 0
            and max_soc_known
            and int(state.max_soc) != int(effective_max_soc)
        ):
            changes["socSet"] = int(effective_max_soc * 10)
            written["max_soc"] = effective_max_soc
            written["max_soc_property"] = "socSet"

        if not managed:
            return max_soc_known

        if not changes:

            log_event(
                logging.DEBUG,
                "soc_limits_unchanged",
                device=dev.name,
                reason=reason
            )

            return True

        if not self.device_state_writes_allowed(dev):
            log_event(
                logging.INFO,
                "dry_run_soc_limits",
                device=dev.name,
                **written,
                reason=reason,
                **self.state_write_gate_fields(dev)
            )
            return False

        try:

            ok = write_device_properties(
                dev,
                changes,
                reason="soc_limits",
                field="/".join(changes),
                error_event="write_soc_limits_error",
                log_fields={**written, "reason": reason}
            )

            if not ok:
                return False

            log_event(
                logging.INFO,
                "write_soc_limits",
                device=dev.name,
                **written,
                reason=reason
            )
            return True

        except Exception as e:

            log_event(
                logging.WARNING,
                "write_soc_limits_error",
                device=dev.name,
                **written,
                reason=reason,
                error=e
            )
            return False

    def state_field_known(self, dev, state, field, flag):
        """Whether ``state`` carries ``field``; said once per device while it does not (K20)."""

        key = (dev.name, field)
        if getattr(state, flag, True):
            self.state_fields_unknown.discard(key)
            return True
        if key not in self.state_fields_unknown:
            self.state_fields_unknown.add(key)
            log_event(
                logging.WARNING,
                "unknown_state_field",
                device=dev.name,
                field=field,
                reason="not_reported"
            )
        return False

    def min_soc_to_write(self, dev, state, desired_min_soc):
        """The ``minSoc`` a SoC write may carry, or 0 to leave it alone.

        Nothing is written over a ``minSoc`` the report does not carry. A
        ``desired_min_soc`` is winter mode's target, which winter holds to the
        raise rule itself, with its policy's lead. The configured ``min_soc``
        is held to the same rule (``cfg.winter_min_soc_raise_limit`` with
        ``MAX_RAISE_ABOVE_SOC``): a raise is written once it leads the SoC by
        at most that and does not land on it, a lowering at once (K15).
        """

        configured = cfg.safe_soc_limit(dev.min_soc)
        if desired_min_soc is None and configured > MIN_SOC_UNMANAGED:
            if not self.state_field_known(dev, state, "minSoc", "min_soc_reported"):
                return MIN_SOC_UNMANAGED
        if not getattr(state, "min_soc_reported", True):
            return MIN_SOC_UNMANAGED
        if desired_min_soc is not None:
            return cfg.safe_soc_limit(
                cfg.safe_int(desired_min_soc, MIN_SOC_UNMANAGED, minimum=0)
            )

        held = cfg.safe_soc_limit(
            cfg.winter_min_soc_raise_limit(
                configured,
                state.min_soc,
                getattr(state, "soc", None),
                MAX_RAISE_ABOVE_SOC
            )
        )
        self.log_min_soc_raise_waiting(dev, state, configured, held)
        return held

    def log_min_soc_raise_waiting(self, dev, state, target, held):
        """Say when a configured raise starts to wait for the battery, then quietly."""

        if held == target:
            self.min_soc_raise_waiting.pop(dev.name, None)
            return

        starts = self.min_soc_raise_waiting.get(dev.name) != target
        self.min_soc_raise_waiting[dev.name] = target
        log_event(
            logging.INFO if starts else logging.DEBUG,
            "min_soc_raise_waits_for_battery",
            device=dev.name,
            current_soc=getattr(state, "soc", None),
            current_min_soc=state.min_soc,
            target_min_soc=target
        )

    def apply_device_modes(self, dev, state):
        """Apply device operating modes if required.

        ``gridOffMode`` is not one of them: the runtime ``offgrid_socket_mode``
        owns it (``apply_runtime_device_state``); a second writer here flipped
        it back every reconcile interval. ``smartMode`` is not written over a
        value the report does not carry (K20).
        """

        if not self.state_reconciliation_supported(dev, "device_modes"):
            return

        properties = {}
        fields = {
            "device": dev.name
        }

        if (
            cfg.RECONCILE_SMART_MODE
            and dev.smart_mode is not None
            and self.state_field_known(dev, state, "smartMode", "smart_mode_reported")
            and int(state.smart_mode) != int(dev.smart_mode)
        ):
            properties["smartMode"] = int(dev.smart_mode)
            fields["smart_mode"] = dev.smart_mode

        if (
            int(state.ac_mode) != AC_MODE_OUTPUT
            or firmware_recovery_or_ac_charge_active(state)
        ):
            log_event(
                logging.INFO,
                "ac_mode_firmware_control_observed",
                device=dev.name,
                ac_mode=state.ac_mode,
                ac_status=state.ac_status,
                soc=state.soc,
                min_soc=state.min_soc,
                soc_limit=state.soc_limit,
                output_w=state.output,
                pack_input_w=state.pack_in,
                output_pack_w=state.pack_out
            )

        if not properties:

            log_event(
                logging.DEBUG,
                "device_modes_unchanged",
                device=dev.name
            )

            return

        if not self.device_state_writes_allowed(dev):
            fields.update(self.state_write_gate_fields(dev))

            log_event(
                logging.INFO,
                "dry_run_device_modes",
                **fields
            )
            return

        try:
            ok = write_device_properties(
                dev,
                properties,
                reason="device_modes",
                field=",".join(properties),
                error_event="write_device_modes_error",
                log_fields=fields
            )

            if not ok:
                return

            log_event(
                logging.INFO,
                "write_device_modes",
                **fields
            )
            
        except Exception as e:

            log_event(
                logging.WARNING,
                "write_device_modes_error",
                device=dev.name,
                error=e
            )

    def refresh_read_only_devices(self):
        """Read the read-only devices once, in the same cycle as the grid meter."""

        read_only_devices = getattr(self, "read_only_devices", None)
        if read_only_devices is None:
            return
        try:
            read_only_devices.refresh()
        except Exception as e:
            log_event(logging.WARNING, "read_only_devices_refresh_failed", error=e)

    def health_snapshot(self):
        """Aggregate in-memory grid-meter and device comm health.

        Shape is export-friendly for later dashboard/InfluxDB use. Stale
        thresholds derive from the active loop interval so they scale with how
        often the EMS actually reads.
        """

        loop_interval = self.runtime_system_int(
            "loop_interval", cfg.LOOP_INTERVAL, minimum=1
        )
        stale_after = max(loop_interval * 6, 30)

        grid = None
        grid_health = getattr(self.shelly, "health", None)
        if grid_health is not None:
            grid = grid_health.snapshot(stale_after_s=stale_after)
            grid["provider"] = getattr(self.shelly, "provider", grid.get("name"))

        devices = []
        for dev in self.devices:
            read = getattr(dev, "read_health", None)
            write = getattr(dev, "write_health", None)
            devices.append({
                "name": dev.name,
                "read": read.snapshot(stale_after_s=stale_after) if read else None,
                "write": write.snapshot(stale_after_s=stale_after) if write else None,
            })

        return {"grid_meter": grid, "devices": devices}

    def log_runtime_state_unchanged(self, fields):
        """Log unchanged-state reconciliation at DEBUG.

        Unchanged state is the normal idle/stable case; logging it at INFO
        floods the default log every loop with no actionable information.
        """

        log_event(logging.DEBUG, "runtime_device_state_unchanged", **fields)

    def warn_legacy_grid_off_mode(self, dev, runtime_mode):
        """Say once when a config ``grid_off_mode`` no longer decides anything.

        The runtime ``offgrid_socket_mode`` owns ``gridOffMode``. An install
        that set the old config key keeps its runtime value, so the operator is
        told which one applies and where to change it.
        """

        legacy = getattr(dev, "grid_off_mode", None)
        if legacy is None or dev.name in self._legacy_grid_off_mode_warned:
            return
        legacy_mode = cfg.offgrid_socket_mode_for(legacy)
        if legacy_mode == runtime_mode:
            return
        self._legacy_grid_off_mode_warned.add(dev.name)
        log_event(
            logging.WARNING,
            "legacy_grid_off_mode_ignored",
            device=dev.name,
            config_grid_off_mode=legacy,
            config_mode=legacy_mode,
            runtime_mode=runtime_mode,
            hint=(
                "set the offgrid socket in the dashboard or with "
                f"emsctl.py device {dev.name} offgrid {legacy_mode}"
            ),
        )

    def apply_runtime_device_state(self, dev, state):
        """Apply runtime-state device intents through safe reconciliation.

        Runtime defaults seed ``offgrid_socket_mode`` for every device, so no
        stored value marks the operator's choice: ``gridOffMode`` is never
        written over a value the report does not carry (K20).
        """

        if not self.state_reconciliation_supported(dev, "runtime_device_state"):
            return

        if not self.runtime_state:
            return

        desired_offgrid_socket_mode = self.runtime_state.get_device(
            dev.name,
            "offgrid_socket_mode",
            None
        )

        if desired_offgrid_socket_mode is None:
            return

        desired_offgrid_socket_mode = str(
            desired_offgrid_socket_mode
        ).strip().lower()

        if desired_offgrid_socket_mode not in cfg.OFFGRID_SOCKET_MODES:
            log_event(
                logging.WARNING,
                "runtime_device_state_invalid",
                device=dev.name,
                field="offgrid_socket_mode",
                value=desired_offgrid_socket_mode,
                runtime_source="runtime-state"
            )
            return

        # Zendure gridOffMode mapping:
        # off      -> gridOffMode=2
        # eco      -> gridOffMode=1
        # standard -> gridOffMode=0
        desired_grid_off_mode = cfg.OFFGRID_SOCKET_MODES[
            desired_offgrid_socket_mode
        ]
        self.warn_legacy_grid_off_mode(dev, desired_offgrid_socket_mode)
        if not self.state_field_known(
            dev, state, "gridOffMode", "grid_off_mode_reported"
        ):
            return
        current_grid_off_mode = int(state.grid_off_mode)
        fields = {
            "device": dev.name,
            "field": "gridOffMode",
            "current_value": current_grid_off_mode,
            "desired_mode": desired_offgrid_socket_mode,
            "desired_value": desired_grid_off_mode,
            "runtime_source": "runtime-state"
        }

        if current_grid_off_mode == desired_grid_off_mode:
            self.log_runtime_state_unchanged(fields)
            return

        if not self.device_state_writes_allowed(dev):
            fields.update(self.state_write_gate_fields(dev))

            log_event(
                logging.INFO,
                "dry_run_runtime_device_state_write",
                **fields
            )
            return

        try:
            ok = write_device_properties(
                dev,
                {"gridOffMode": desired_grid_off_mode},
                reason="runtime_device_state",
                field="gridOffMode",
                error_event="write_runtime_device_state_error",
                log_fields=fields
            )

            if not ok:
                return

            log_event(
                logging.INFO,
                "write_runtime_device_state",
                **fields
            )

        except Exception as e:
            log_event(
                logging.WARNING,
                "write_runtime_device_state_error",
                device=dev.name,
                field="gridOffMode",
                current_value=current_grid_off_mode,
                desired_value=desired_grid_off_mode,
                runtime_source="runtime-state",
                error=e
            )

    def publish_to_dashboard(
        self,
        load,
        states,
        targets,
        effective_targets,
        allocated_total,
        effective_total,
        enabled,
        max_power,
        min_output_limit,
        night_min_soc_idle=False
    ):
        """Persist one read-only dashboard snapshot.

        Reuses the single telemetry collection of this cycle for both storage
        targets: the SQLite dashboard store and (when enabled) the native
        InfluxDB writer. The hardware is never polled a second time.
        """

        if not self.dashboard_store and not self.influx_writer:
            return

        now = time.time()

        # InfluxDB raw telemetry is written every EMS loop (or at the optional
        # influxdb.raw_write_interval_seconds cadence), decoupled from the
        # lower-frequency SQLite dashboard history below. This keeps the raw
        # bucket at the highest available EMS sampling resolution for spike
        # visibility without forcing SQLite to write every loop.
        self.publish_to_influx(load, states, effective_total, now=now)

        if not self.dashboard_store:
            return

        interval = cfg.safe_float(
            cfg.DASHBOARD_CONFIG.get("write_interval_seconds", 5),
            5,
            minimum=0
        )

        if interval > 0 and now - self._last_dashboard_publish < interval:
            return
        self._last_dashboard_publish = now

        try:
            from dashboard.telemetry import build_dashboard_snapshot

            snapshot = build_dashboard_snapshot(
                self,
                load,
                states,
                targets,
                effective_targets,
                allocated_total,
                effective_total,
                enabled=enabled,
                max_total_power=max_power,
                min_output_limit=min_output_limit,
                night_min_soc_idle=night_min_soc_idle
            )
            self.dashboard_store.record(snapshot)
        except Exception as e:
            log_event(
                logging.WARNING,
                "dashboard_publish_error",
                error=e
            )

    def publish_to_influx(self, load, states, target=None, now=None):
        """Enqueue this cycle's telemetry for the native InfluxDB writer.

        ``load`` is the grid/meter exchange power (positive import) and
        ``target`` the EMS effective output target; the writer derives the
        household load and emits the grid/home/target Analytics series.

        Non-blocking and failure-isolated: building line protocol is cheap and
        any writer error is contained inside the background worker, so the
        control loop is never slowed or stopped by InfluxDB.
        """

        writer = self.influx_writer
        if not writer:
            return

        if now is None:
            now = time.time()

        # Default cadence is every EMS loop (0/null). A positive
        # raw_write_interval_seconds throttles raw writes to at most once per N
        # seconds without affecting the SQLite dashboard cadence.
        influx_config = cfg.INFLUXDB_CONFIG or {}
        raw_interval = cfg.safe_float(
            influx_config.get("raw_write_interval_seconds", 0),
            0,
            minimum=0
        )
        if raw_interval > 0 and now - self._last_influx_publish < raw_interval:
            return
        self._last_influx_publish = now

        try:
            from ems.history.influx_writer import build_telemetry_lines

            read_only_devices = getattr(self, "read_only_devices", None)
            read_only_tiles = (
                read_only_devices.tiles() if read_only_devices is not None else ()
            )
            lines = build_telemetry_lines(
                self.devices,
                states,
                self.device_online,
                load,
                target,
                read_only_tiles=read_only_tiles,
            )
            writer.enqueue(lines)
        except Exception as e:
            log_event(logging.WARNING, "influx_publish_error", error=e)

    def run_guarded(self):
        """Run one cycle and survive it raising.

        An exception in ``run_once`` used to end the EMS process, and the
        shutdown returned a charging inverter on the way out. As the owner
        decided (K8 in docs/developer/review-coverage.md), the cycle now stops
        where it raised: nothing after that point is written, what came before
        it stands, and the next cycle tries again after the loop interval. A
        cycle that fails at the same point every time repeats the writes before
        it; a device it no longer reaches holds its last limit. The first
        failure is logged with its traceback, then one warning a minute while
        it lasts, and the recovery once.

        From the ``CYCLE_FAILURES_BEFORE_CHARGE_RELEASE``-th failure in a row
        each failed cycle runs the release a stop runs, best effort, on the
        records' resend window (R1-1, owner decision 2026-10-12), and the
        full-charge assist takes no AC input until as many cycles in a row have
        run through, and then within the hourly entry limit
        (:meth:`assist_ac_input_withheld`). Only an exception out
        of ``run_once`` counts: a failure the cycle handles itself (a device
        that does not answer, a write it refuses) is no failed cycle, and a
        cycle that hangs never ends to be counted.
        """

        try:
            self.run_once()
        except Exception as exc:
            self.cycle_failures += 1
            if self.cycle_failures == 1:
                logging.error(
                    "event=control_cycle_failed consecutive=1 error=%s", exc, exc_info=True
                )
            elif self.cycle_failures % CYCLE_FAILURE_WARN_EVERY == 0:
                log_event(
                    logging.WARNING,
                    "control_cycle_failed",
                    consecutive=self.cycle_failures,
                    error=exc,
                )
            if self.charges_returned_after_failed_cycles():
                self.release_charging_devices(failed_cycles=self.cycle_failures)
                self.cycles_through_since_release = 0
            elif self.cycles_through_since_release is not None:
                self.cycles_through_since_release = 0
            if self.sleep_enabled:
                time.sleep(self._loop_interval_after_failure())
            return False
        if self.cycle_failures:
            log_event(logging.INFO, "control_cycle_recovered", failed_cycles=self.cycle_failures)
            self.cycle_failures = 0
        if self.cycles_through_since_release is not None:
            self.cycles_through_since_release += 1
            if self.cycles_through_since_release >= CYCLE_FAILURES_BEFORE_CHARGE_RELEASE:
                self.end_assist_ac_input_withhold()
        return True

    def end_assist_ac_input_withhold(self):
        """Give a running full-charge assist its AC input back, within the hourly entry limit.

        Each time the hold after a failed-cycle release ends while an assist
        with its AC mode runs is one entry into AC input, counted against the
        regulator's ``max_charge_entries_per_hour``: three failed cycles and
        three that ran through, over and over, took the input back every six
        cycles (K24). While the trailing hour leaves no entry, the hold stays,
        said once as ``full_charge_assist_ac_input_rate_limited``.
        """

        if self.full_charge_assist_takes_ac_input():
            now = time.monotonic()
            settings = self.charge_settings()
            entries, allowed = charge_entries_within_limit(
                self.assist_reentries, now, settings
            )
            self.assist_reentries = entries
            if not allowed:
                if not self.assist_reentry_rate_limited:
                    self.assist_reentry_rate_limited = True
                    log_event(
                        logging.WARNING,
                        "full_charge_assist_ac_input_rate_limited",
                        entries_last_hour=len(entries),
                        max_entries_per_hour=settings.max_entries_per_hour,
                    )
                return
            self.assist_reentries = entries + (now,)
        self.assist_reentry_rate_limited = False
        self.cycles_through_since_release = None

    def full_charge_assist_takes_ac_input(self):
        """Whether a running full-charge assist with its AC mode on holds any device."""

        store = self.battery_full_charge_store
        if not store or not self.full_charge_assist_config().get(
            "enable_ac_charge_mode", True
        ):
            return False
        return any(
            (store.get_device_state(dev.name) or {}).get("full_charge_assist_active")
            for dev in self.devices
        )

    def charges_returned_after_failed_cycles(self):
        """Whether failed cycles in a row returned the EMS's charges, until one runs through."""

        return self.cycle_failures >= CYCLE_FAILURES_BEFORE_CHARGE_RELEASE

    def assist_ac_input_withheld(self):
        """Whether the full-charge assist still takes no AC input after failed cycles returned it.

        From the release until ``CYCLE_FAILURES_BEFORE_CHARGE_RELEASE`` cycles
        in a row have run through, as many as failed before it, and longer
        while the hourly entry limit leaves no entry
        (:meth:`end_assist_ac_input_withhold`). Taken back after the first
        cycle that ran through, the AC input went in and out every fourth
        cycle under three failures and one pass (R1b); the regulator's charge
        waits for its entry confirmation instead.
        """

        return (
            self.charges_returned_after_failed_cycles()
            or self.cycles_through_since_release is not None
        )

    def _loop_interval_after_failure(self):
        try:
            return self.runtime_system_int("loop_interval", cfg.LOOP_INTERVAL, minimum=1)
        except Exception:
            return max(1, int(cfg.LOOP_INTERVAL))

    def run_once(self):
        """Execute one EMS cycle."""

        start = time.time()
        self.last_control_explanation = None

        if self.runtime_state:
            self.runtime_state.load_if_changed()

        load = finite_grid_load(self.shelly.get_power())
        self.update_grid_meter_holding(load)
        self.refresh_read_only_devices()

        # =====================
        # RUNTIME cfg.CONFIG
        # =====================

        max_power = self.runtime_system_int(
            "max_total_power",
            cfg.MAX_TOTAL_POWER,
            minimum=0
        )
        enabled = self.runtime_system_bool(
            "enabled",
            cfg.SYSTEM_ENABLED
        )
        self.note_control_enabled(enabled)
        interval = self.runtime_system_int(
            "loop_interval",
            cfg.LOOP_INTERVAL,
            minimum=1
        )
        min_output_limit = self.runtime_system_int(
            "min_output_limit",
            cfg.MIN_OUTPUT_LIMIT,
            minimum=0
        )

        for dev in self.devices:
            dev.max_power = self.runtime_device_int(
                dev.name,
                "max_power",
                dev.max_power,
                minimum=0
            )
            dev.pv_priority_factor = self.runtime_device_float(
                dev.name,
                "pv_priority_factor",
                dev.pv_priority_factor,
                minimum=0.01
            )

        # =====================
        # FETCH STATES
        # =====================

        raw_states = fetch_all_devices(self.devices)
        self.last_fetch_at = time.time()

        states = []

        for dev, state in zip(self.devices, raw_states):

            #
            # Fresh state available
            #

            if state:

                self.last_states[dev.name] = state
                self.last_seen[dev.name] = self.last_fetch_at
                self.device_online[dev.name] = True

                states.append(state)

                continue

            #
            # Fallback to last known state
            #

            if dev.name in self.last_states:

                self.device_online[dev.name] = False

                cached = self.last_states[dev.name]

                age = round(
                    time.time() - self.last_seen.get(dev.name, 0),
                    1
                )

                logging.warning(
                    f"{dev.name}: using cached state "
                    f"{age}s old "
                    f"(output={cached.output}W "
                    f"solar={cached.solar}W "
                    f"soc={cached.soc}%)"
                )

                states.append(cached)

                continue

            #
            # No valid state available
            #

            logging.error(
                f"{dev.name}: no valid state available"
            )

            self.device_online[dev.name] = False

            states.append(zero_device_state())

        now = datetime.now().astimezone()
        full_charge_feature_transition = (
            self.full_charge_assist_feature_transition(now)
        )

        for dev, state in zip(self.devices, raw_states):
            if state:
                self.process_battery_full_charge_assist(
                    dev,
                    state,
                    now,
                    full_charge_feature_transition
                )

        capabilities = [
            detect_capabilities(state)
            for state in states
        ]
        previous_intents = self.runtime_intents
        self.runtime_intents = {}
        for dev, state in zip(self.devices, states):
            self.runtime_intents[dev.name] = ac_output_intent(dev.name)

        for dev, state in zip(
            self.devices,
            raw_states
        ):
            if not state:
                self.runtime_intents[dev.name] = self.unanswered_device_intent(
                    dev, previous_intents.get(dev.name)
                )
                continue

            self.leave_charge_shown_by_draw_alone(dev, state)
            ems_commanded_charge = self.charge_commanded_by_ems(dev)
            self.settle_found_ac_input_exit(dev, state)
            uncommanded = firmware_charge_intent(
                dev.name,
                state,
                ems_commanded_charge=ems_commanded_charge,
                watched=self.ac_input_attributable(dev),
            )
            intent = self.resolve_device_claims(
                dev,
                self.get_device_runtime_intent(dev, state),
                regulator_charge_intent(
                    dev.name, ems_commanded_charge=ems_commanded_charge
                ),
                uncommanded,
            )
            self.note_ac_input_provenance(dev.name, uncommanded, intent)
            self.runtime_intents[dev.name] = intent
            ac_mode_write_ok = self.reconcile_ac_mode_intent(dev, state, intent)
            self.update_full_charge_assist_ac_pending(
                dev,
                intent,
                ac_mode_write_ok,
                now
            )
            self.confirm_full_charge_assist_ac_restore(
                dev,
                state,
                intent,
                now
            )
            self.reconcile_runtime_ac_charge_power(dev, state, intent)

        capabilities = self.intent_filtered_capabilities(capabilities)
        capabilities = self.winter_filtered_capabilities(states, capabilities)
        self._dashboard_capabilities = capabilities
        active_indexes = self.active_online_device_indexes()

        for dev, state, cap in zip(
            self.devices,
            states,
            capabilities
        ):
            log_event(
                logging.DEBUG,
                "capability_detection",
                device=dev.name,
                soc_limit=state.soc_limit,
                dc_status=state.dc_status,
                ac_status=state.ac_status,
                pack_state=state.pack_state,
                fault_level=state.fault_level,
                solar_w=state.solar,
                output_w=state.output,
                output_limit_w=state.output_limit,
                pack_input_w=state.pack_in,
                soc_runtime_state=derive_soc_runtime_state(state),
                grid_reverse=state.grid_reverse,
                can_charge=cap.can_charge,
                can_discharge=cap.can_discharge,
                can_export=cap.can_export,
                can_ac_charge=cap.can_ac_charge,
                reason=cap.reason
            )

        self.observe_charge_delivery(states)

        #
        # Reconcile SOC limits
        #

        if (
            cfg.SOC_RECONCILE_INTERVAL > 0
            and not cfg.SIMULATION_MODE
            and not cfg.ARGS.replay
        ):

            self.soc_reconcile_counter += 1

            if (
                self.soc_reconcile_counter
                >= cfg.SOC_RECONCILE_INTERVAL
            ):

                self.soc_reconcile_counter = 0
                now = datetime.now()
                winter_active = cfg.winter_mode_active(now, self.runtime_state)

                if cfg.winter_feature_enabled(self.runtime_state):
                    winter_state_changed = (
                        self._last_winter_active != winter_active
                    )
                    self._last_winter_active = winter_active
                    log_event(
                        logging.INFO
                        if winter_state_changed
                        else logging.DEBUG,
                        "winter_mode_state",
                        active=winter_active,
                        month=now.month,
                        summer_min_soc=cfg.winter_summer_min_soc(),
                        winter_min_soc=cfg.winter_min_soc_percent()
                    )

                for dev, state in zip(
                    self.devices,
                    raw_states
                ):

                    if state:
                        self.reconcile_device_state_limits(
                            dev,
                            state,
                            winter_active,
                            now
                        )

        for dev, state in zip(
            self.devices,
            raw_states
        ):

            if state:

                self.apply_runtime_device_state(
                    dev,
                    state
                )

        self.end_charges_on_disable(enabled)

        chargeable = self.resolve_charge_eligibility(states, capabilities)
        meter_fresh = self.grid_meter_reading_is_fresh()
        releasable_output_w = sum(
            cfg.safe_int(getattr(state, "output", 0), 0, minimum=0)
            for state, allowed in zip(states, chargeable)
            if allowed
        )
        controllable_indexes = self.night_min_soc_controllable_indices()
        night_min_soc_idle = self.update_night_min_soc_idle_state(
            states,
            controllable_indexes,
            enabled,
            min_output_limit,
            charge_pending=self.charge_direction.charging
        )

        if night_min_soc_idle:
            raw_load = cfg.safe_float(load, 0.0)
            self.take_charge_decision(
                meter_fresh,
                commanded_total_w=0,
                desired_total_w=raw_load,
                filtered_load_w=raw_load,
                chargeable=chargeable,
                releasable_output_w=releasable_output_w,
            )
            targets = [
                min_output_limit if i in controllable_indexes else 0
                for i, _dev in enumerate(self.devices)
            ]
            effective_targets = list(targets)
            new = sum(effective_targets)

            logging.debug(
                f"Load={load}W "
                f"Target={new}W "
                f"Enabled={enabled} "
                f"NightMinSocIdle=True"
            )

            self.last_control_explanation = (
                self.build_night_min_soc_idle_explanation(
                    load,
                    states,
                    targets,
                    effective_targets,
                    enabled,
                    max_power,
                    min_output_limit,
                    controllable_indexes
                )
            )

            self.publish_to_dashboard(
                load,
                states,
                targets,
                effective_targets,
                sum(targets),
                sum(effective_targets),
                enabled,
                max_power,
                min_output_limit,
                night_min_soc_idle=True
            )

            for dev in self.devices:
                self.retire_pending_output_limit(dev, "night_min_soc_idle")

            self.apply_night_min_soc_idle_control(
                states,
                controllable_indexes,
                min_output_limit
            )

            elapsed = time.time() - start

            if self.sleep_enabled:
                time.sleep(max(0, interval - elapsed))

            return

        # =====================
        # CALCULATE TARGETS
        # =====================

        has_export_capacity = self.has_output_control_export_capacity(
            states,
            capabilities,
            active_indexes
        )
        standby_total_w = min_output_limit * len(active_indexes)

        if self.grid_meter_holding:
            if self.commanded_total_w is None:
                self.initialize_commanded_total(states, max_power)
            stabilized_total = min(self.commanded_total_w, max_power)
        else:
            stabilized_total = self.stabilized_total_target(
                load,
                states,
                max_power,
                has_export_capacity=has_export_capacity,
                standby_total_w=standby_total_w,
                active_device_count=len(active_indexes)
            )

        charge_decision = self.take_charge_decision(
            meter_fresh,
            commanded_total_w=stabilized_total,
            desired_total_w=(
                self.desired_total_w
                if self.desired_total_w is not None
                else stabilized_total
            ),
            filtered_load_w=self.filtered_load_w or 0,
            chargeable=chargeable,
            releasable_output_w=releasable_output_w,
        )

        if not charge_decision.charging:
            stabilized_total = self.discharge_total_leaving_a_charge(
                load,
                states,
                stabilized_total,
                max_power,
                has_export_capacity=has_export_capacity,
                standby_total_w=standby_total_w,
            )

        # The discharge allocator never sees a negative request: charging is a
        # different weighting of the same allocation primitive, not a sign the
        # PV-first split knows how to read.
        targets, current, new, control_explanation = calculate_targets(
            load,
            states,
            max_power,
            device_configs=self.devices,
            capabilities=capabilities,
            requested_total=max(0, stabilized_total),
            explain=True,
            online_devices=self.device_online,
            commandable=self.claim_eligible_devices()
        )

        self.log_pv_priority_change(control_explanation)

        if charge_decision.charging and stabilized_total < 0:
            targets = allocate_charge_targets(
                -stabilized_total,
                states,
                self.devices,
                capabilities,
                chargeable,
                ceilings=[
                    self.device_charge_limits.get(dev.name, 0)
                    for dev in self.devices
                ],
                charging_now=[
                    self.charge_commanded_by_ems(dev)
                    for dev in self.devices
                ],
            )
            self.explain_charge_allocation(
                control_explanation, targets, chargeable
            )

        targets = self.apply_device_ramp(
            targets,
            load
        )
        effective_targets = self.effective_control_targets(
            targets,
            enabled,
            min_output_limit
        )
        control_explanation.allocated_target_total_w = sum(targets)
        control_explanation.effective_target_total_w = sum(effective_targets)
        control_explanation.commanded_total_w = self.commanded_total_w
        control_explanation.filtered_load_w = self.filtered_load_w
        control_explanation.min_output_limit_w = min_output_limit
        for i, dev in enumerate(self.devices):
            device_explanation = control_explanation.devices.get(dev.name)
            if not device_explanation:
                continue
            if capabilities[i].export_held:
                device_explanation.limiting_reason = capabilities[i].reason
            if i < len(targets):
                device_explanation.allocated_target_w = targets[i]
            if i < len(effective_targets):
                device_explanation.effective_target_w = effective_targets[i]
                device_explanation.command_target_w = effective_targets[i]
                if device_explanation.raw_target_w is not None:
                    device_explanation.adjustment_delta_w = (
                        effective_targets[i] - device_explanation.raw_target_w
                    )

                block_reason = (
                    self.device_command_block_reason(dev) if enabled else None
                )

                if not enabled:
                    device_explanation.write_decision = "blocked"
                    device_explanation.write_reason = "control_disabled"
                elif block_reason:
                    device_explanation.write_decision = "blocked"
                    device_explanation.write_reason = block_reason
                    if not self.device_output_control_allowed(dev):
                        device_explanation.limiting_reason = (
                            self.runtime_role_block_reason(dev.name)
                        )
                else:
                    state = states[i]
                    reference = (
                        state.output_limit
                        if state.output_limit > 0
                        else state.output
                    )
                    reference_source = (
                        "output_limit"
                        if state.output_limit > 0
                        else "output"
                    )
                    device_explanation.deadband_reference_w = reference
                    device_explanation.deadband_reference_source = reference_source
                    device_explanation.deadband_w = cfg.DEADBAND
                    if abs(effective_targets[i] - reference) < cfg.DEADBAND:
                        device_explanation.write_decision = "skip"
                        device_explanation.write_reason = "deadband"
                    else:
                        device_explanation.write_decision = "send"
                        device_explanation.write_reason = "output_limit_update"
        self.last_control_explanation = control_explanation

        logging.debug(
            f"Load={load}W "
            f"ControllerTarget={new}W "
            f"AllocatedTarget={sum(targets)}W "
            f"EffectiveTarget={sum(effective_targets)}W "
            f"Enabled={enabled}"
        )

        self.publish_to_dashboard(
            load,
            states,
            targets,
            effective_targets,
            sum(targets),
            sum(effective_targets),
            enabled,
            max_power,
            min_output_limit
        )

        # =====================
        # APPLY CONTROL
        # =====================

        for i, dev in enumerate(self.devices):

            if not enabled:
                log_event(
                    logging.INFO,
                    "control_disabled_skip_write",
                    device=dev.name,
                    target_w=targets[i]
                )
                self.retire_pending_output_limit(dev, "control_disabled_skip_write")
                continue

            if not self.device_online.get(dev.name, True):

                log_event(
                    logging.WARNING,
                    "offline_skip_write",
                    device=dev.name
                )

                self.retire_pending_output_limit(dev, "offline_skip_write")
                continue

            if not self.runtime_device_bool(dev.name, "enabled", True):
                log_event(
                    logging.INFO,
                    "device_disabled_skip_write",
                    device=dev.name,
                    target_w=targets[i]
                )
                self.retire_pending_output_limit(dev, "device_disabled_skip_write")
                continue

            if not self.device_output_control_allowed(dev):
                intent = self.runtime_intents.get(dev.name)
                log_event(
                    logging.INFO,
                    "runtime_role_skip_output_limit",
                    device=dev.name,
                    target_w=targets[i],
                    runtime_role=(
                        intent.role.value
                        if intent
                        else "blocked"
                    ),
                    reason=(
                        intent.reason
                        if intent
                        else "runtime_role_blocked"
                    )
                )
                self.retire_pending_output_limit(dev, "runtime_role_skip_output_limit")
                continue

            target = effective_targets[i]

            if target != targets[i] and min_output_limit > 0:
                apply_min_output_limit(
                    targets[i],
                    dev,
                    min_output_limit
                )

            # What the device is doing now, on the same signed axis as the
            # target. A charging device reports outputLimit 0 and output 0
            # while drawing hundreds of watts, so a discharge-only reference
            # reads it as idle -- and an idle target then looks like no change
            # at all. That is not academic: after a restart the EMS would
            # decide "this device should be off", compare 0 against 0, skip the
            # write, and leave the hardware charging from the grid with the EMS
            # running and content.
            ends_a_charge = self.write_can_end_a_charge(
                dev
            ) and not settling_out_of_charge(states[i])
            charge_reference = (
                cfg.safe_int(getattr(states[i], "grid_input", 0), 0, minimum=0)
                if ends_a_charge
                else 0
            )
            if ends_a_charge and not charge_reference and cfg.safe_int(
                getattr(states[i], "ac_mode", 0), 0
            ) == AC_MODE_INPUT:
                # Commanded into charge but not yet drawing: the probe measured
                # ~2 s between the mode taking effect and current flowing.
                charge_reference = cfg.safe_int(
                    getattr(states[i], "input_limit_w", 0), 0, minimum=0
                )

            if charge_reference > 0:
                deadband_reference = -charge_reference
                deadband_reference_source = "ac_charge"
            elif states[i].output_limit > 0:
                deadband_reference = states[i].output_limit
                deadband_reference_source = "output_limit"
            else:
                deadband_reference = states[i].output
                deadband_reference_source = "output"

            charge_exit = self.unproven_charge_exit(dev)
            if (
                charge_exit is None
                and abs(target - deadband_reference) < cfg.DEADBAND
            ):
                log_event(
                    logging.DEBUG,
                    "deadband_skip_write",
                    device=dev.name,
                    target_w=target,
                    reference_w=deadband_reference,
                    reference_source=deadband_reference_source,
                    deadband_w=cfg.DEADBAND
                )
                self.retire_pending_output_limit(dev, "deadband_skip_write")
                continue

            written = self.write_target(dev, target, charge_exit)
            self.note_unproven_charge_exit(dev, target, written)

        # =====================
        # LOOP TIMING
        # =====================

        elapsed = time.time() - start

        if self.sleep_enabled:
            time.sleep(max(0, interval - elapsed))

# =====================
# SIMULATION / REPLAY
# =====================
