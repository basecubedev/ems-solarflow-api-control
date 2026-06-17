# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import logging
import time
from collections import deque
from datetime import datetime, timedelta

from ems import config as cfg
from ems.clients import (
    fetch_all_devices,
    zendure_write_succeeded,
    zero_device_state,
)
from ems.logging_utils import log_event
from ems.models import DeviceCapabilities
from ems.runtime_intents import (
    DeviceRuntimeIntent,
    DeviceRuntimeRole,
    ac_input_intent,
    ac_output_intent,
    runtime_intent_from_role,
)
from ems.state_store import BatteryFullChargeStateStore
from ems.target_control import (
    ControlExplanation,
    ControlLimitExplanation,
    DeviceControlExplanation,
    apply_min_output_limit,
    calculate_remaining_time_hours,
    calculate_targets,
    detect_capabilities,
    derive_soc_runtime_state,
    firmware_recovery_or_ac_charge_active,
    startup_ac_mode_initialization_blocker,
)


STARTUP_AC_MODE_RECONCILE_REASON = "startup_ac_mode_reconcile"
FULL_CHARGE_ASSIST_REASON = "battery_full_charge_assist"
FULL_CHARGE_ASSIST_RESTORE_REASON = "battery_full_charge_assist_restore"


class EMSController:
    """Main EMS control loop."""

    def __init__(
        self,
        devices,
        shelly,
        ha=None,
        sleep_enabled=True,
        runtime_state=None,
        dashboard_store=None,
        battery_full_charge_store=None,
        influx_writer=None
    ):
        self.devices = devices
        self.shelly = shelly
        self.ha = ha
        self.sleep_enabled = sleep_enabled
        self.runtime_state = runtime_state
        self.dashboard_store = dashboard_store
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
        self.device_online = {}
        self.battery_power_history = {}
        self.initial_ac_mode_reconciled = {}
        self.last_ha_seen = {}
        self.last_ha_written = {}
        self.commanded_total_w = None
        self.filtered_load_w = None
        self.load_history = deque(
            maxlen=cfg.safe_int(
                cfg.OUTPUT_CONTROL_CONFIG.get("median_window", 3),
                3,
                minimum=1
            )
        )
        self.commanded_device_targets = {}
        self.last_winter_adjust_date = None
        self.winter_min_soc_targets = {}
        self.night_min_soc_idle_active = False
        self.night_min_soc_idle_parked = set()
        self._dashboard_capabilities = []
        self._last_dashboard_publish = 0
        self._last_influx_publish = 0
        self.last_control_explanation = None
        self.runtime_intents = {}

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
            logging.INFO,
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
        limit_total = sum(
            state.output_limit
            for state in states
            if state.output_limit > 0
        )

        if limit_total > 0:
            initial = limit_total
            source = "output_limit"
        else:
            initial = sum(state.output for state in states)
            source = "output"

        self.commanded_total_w = max(0, min(max_power, initial))

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

    def telemetry_stale(self):
        max_age = self.output_control_float(
            "telemetry_max_age_seconds",
            10,
            minimum=0
        )

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
            seen = self.last_seen.get(dev.name)
            if not seen:
                continue

            age = now - seen

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

    def active_online_device_indexes(self):
        """Return indexes for devices currently eligible for EMS control."""

        indexes = []

        for i, dev in enumerate(self.devices):
            if not self.device_online.get(dev.name, True):
                continue

            if not self.runtime_device_bool(dev.name, "enabled", True):
                continue

            if not self.device_output_control_allowed_by_intent(dev.name):
                continue

            indexes.append(i)

        return indexes

    def state_has_positive_pv(self, state):
        """Return true when any PV telemetry field is positive."""

        return (
            state.solar > 0
            or state.solar1 > 0
            or state.solar2 > 0
            or state.solar3 > 0
            or state.solar4 > 0
        )

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

            if self.state_has_positive_pv(state):
                return True

            if capability.can_discharge:
                return True

            if state.output > 0:
                return True

        return False

    def intent_filtered_capabilities(self, capabilities):
        """Return capabilities with reserved devices blocked from output control."""

        filtered = []

        for dev, capability in zip(self.devices, capabilities):
            if self.device_output_control_allowed_by_intent(dev.name):
                filtered.append(capability)
                continue

            filtered.append(DeviceCapabilities(
                can_charge=capability.can_charge,
                can_discharge=False,
                can_export=False,
                can_ac_charge=capability.can_ac_charge,
                reason=f"runtime_role_{self.runtime_intents[dev.name].role.value}"
            ))

        return filtered

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
        ):
            clamped = min(self.commanded_total_w, standby_total_w)

            if clamped != self.commanded_total_w:
                self.commanded_total_w = clamped

            desired = self.commanded_total_w
            held = True
            log_event(
                logging.INFO,
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
                logging.INFO,
                "output_control_deadband_hold",
                reason="load_deadband",
                raw_load_w=round(raw_load, 1),
                filtered_load_w=round(filtered_load, 1),
                commanded_total_w=round(self.commanded_total_w, 1),
                deadband_w=load_deadband
            )
        else:
            desired = self.commanded_total_w + filtered_load

        desired = max(0, min(max_power, desired))

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
                logging.INFO,
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
                logging.INFO,
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

            if bypass:
                ramp_limit *= self.output_control_float(
                    "bypass_ramp_multiplier",
                    1.5,
                    minimum=1.0
                )

            if stale:
                ramp_limit *= self.output_control_float(
                    "stale_telemetry_ramp_factor",
                    0.5,
                    minimum=0.0
                )

            if ramp_limit > 0 and abs(delta) > ramp_limit:
                ramped = (
                    self.commanded_total_w + ramp_limit
                    if delta > 0
                    else self.commanded_total_w - ramp_limit
                )
                log_event(
                    logging.INFO,
                    "output_control_ramp_limited",
                    previous_total_w=round(self.commanded_total_w, 1),
                    desired_total_w=round(desired, 1),
                    ramped_total_w=round(ramped, 1),
                    ramp_limit_w=round(ramp_limit, 1)
                )

        ramped = max(0, min(max_power, ramped))

        log_event(
            logging.INFO,
            "output_control_state",
            initialized=False,
            raw_load_w=round(raw_load, 1),
            filtered_load_w=round(filtered_load, 1),
            commanded_total_w=round(self.commanded_total_w, 1),
            desired_total_w=round(desired, 1),
            ramped_total_w=round(ramped, 1)
        )

        self.commanded_total_w = ramped
        return ramped

    def apply_device_ramp(self, targets, raw_load):
        if not self.output_control_bool("device_ramp_enabled", True):
            adjusted_targets = []
            for dev, target in zip(self.devices, targets):
                if not self.device_output_control_allowed_by_intent(dev.name):
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
            if not self.device_output_control_allowed_by_intent(dev.name):
                self.commanded_device_targets[dev.name] = 0
                ramped_targets.append(0)
                continue

            previous = self.commanded_device_targets.get(dev.name)

            if previous is None:
                self.commanded_device_targets[dev.name] = target
                ramped_targets.append(target)
                continue

            delta = target - previous
            limit = up_limit if delta > 0 else down_limit
            ramped = target

            if limit > 0 and abs(delta) > limit:
                ramped = previous + limit if delta > 0 else previous - limit
                log_event(
                    logging.INFO,
                    "output_control_device_ramp_limited",
                    device=dev.name,
                    previous_target_w=round(previous),
                    desired_target_w=round(target),
                    ramped_target_w=round(ramped),
                    ramp_limit_w=round(limit)
                )

            ramped = max(0, min(dev.max_power, ramped))
            self.commanded_device_targets[dev.name] = ramped
            ramped_targets.append(ramped)

        return ramped_targets

    def reset_output_control_state(self):
        """Reset output-control memory after a blocked operating state."""

        self.commanded_total_w = None
        self.filtered_load_w = None
        self.load_history.clear()
        self.commanded_device_targets = {}

    def night_min_soc_controllable_indices(self):
        """Return device indexes controlled by EMS in the current cycle."""

        indexes = []

        for i, dev in enumerate(self.devices):
            if not self.device_online.get(dev.name, True):
                continue

            if not self.runtime_device_bool(dev.name, "enabled", True):
                continue

            if not self.device_output_control_allowed_by_intent(dev.name):
                continue

            indexes.append(i)

        return indexes

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
        battery_blocked = (
            state.soc <= state.min_soc
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
        min_output_limit
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
        """Park devices once and suppress further outputLimit writes."""

        for i in indexes:
            dev = self.devices[i]
            state = states[i]

            if dev.name in self.night_min_soc_idle_parked:
                log_event(
                    logging.INFO,
                    "night_min_soc_idle_hold_skip_write",
                    device=dev.name,
                    output_limit_w=state.output_limit,
                    min_output_limit_w=min_output_limit,
                    reason="already_parked"
                )
                continue

            if state.output_limit == min_output_limit:
                log_event(
                    logging.INFO,
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
            self.set_output_limit(dev, min_output_limit)
            self.night_min_soc_idle_parked.add(dev.name)

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
            runtime_enabled = self.runtime_device_bool(dev.name, "enabled", True)
            target = targets[i] if i < len(targets) else 0
            effective = (
                effective_targets[i]
                if i < len(effective_targets)
                else target
            )

            if not enabled:
                write_decision = "blocked"
                write_reason = "control_disabled"
            elif not online:
                write_decision = "blocked"
                write_reason = "offline"
            elif not runtime_enabled:
                write_decision = "blocked"
                write_reason = "device_disabled"
            elif not self.device_output_control_allowed_by_intent(dev.name):
                intent = self.runtime_intents.get(dev.name)
                write_decision = "blocked"
                write_reason = (
                    f"runtime_role_{intent.role.value}"
                    if intent
                    else "runtime_role_blocked"
                )
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

            limiting_reason = (
                "below_min_soc"
                if state.soc <= state.min_soc
                else "soc_protection"
            )

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
            if not enabled:
                effective_targets.append(0)
                continue

            if not self.device_online.get(dev.name, True):
                effective_targets.append(0)
                continue

            if not self.runtime_device_bool(dev.name, "enabled", True):
                effective_targets.append(0)
                continue

            if not self.device_output_control_allowed_by_intent(dev.name):
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

    def runtime_section_bool(self, section_name, key, default):
        if not self.runtime_state:
            return default

        return cfg.safe_bool(
            self.runtime_state.get_section(section_name, key, default),
            default
        )

    def runtime_ha_enabled(self):
        ha_config = cfg.CONFIG.get("ha", {})
        return self.runtime_section_bool(
            "ha",
            "enabled",
            ha_config.get("enabled", False)
        )

    def runtime_ha_control_enabled(self):
        ha_config = cfg.CONFIG.get("ha", {})
        return self.runtime_section_bool(
            "ha",
            "control_enabled",
            ha_config.get("control_enabled", False)
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
        """Return the current runtime reservation intent for a device."""

        role = self.runtime_device_str(
            dev.name,
            "runtime_role",
            DeviceRuntimeRole.AC_OUTPUT.value
        )
        reason = self.runtime_device_str(
            dev.name,
            "runtime_role_reason",
            "ac_output"
        )
        intent = runtime_intent_from_role(dev.name, role, reason)

        if intent:
            return intent

        log_event(
            logging.WARNING,
            "runtime_role_invalid",
            device=dev.name,
            runtime_role=role,
            reason="unknown_runtime_role"
        )
        return ac_output_intent(dev.name, "invalid_runtime_role_fallback")

    def device_output_control_allowed_by_intent(self, dev_name):
        intent = self.runtime_intents.get(dev_name)
        if intent is None:
            return True

        return intent.output_control_allowed

    def ac_mode_intent_log_fields(self, dev, state, intent):
        return {
            "device": dev.name,
            "current_ac_mode": state.ac_mode,
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

    def is_startup_ac_mode_reconcile_intent(self, intent):
        return intent.reason == STARTUP_AC_MODE_RECONCILE_REASON

    def reconcile_ac_mode_intent(self, dev, state, intent):
        """Write acMode only when telemetry differs from desired runtime intent."""

        if intent.desired_ac_mode is None:
            return True

        fields = self.ac_mode_intent_log_fields(dev, state, intent)
        current_ac_mode = int(state.ac_mode)
        desired_ac_mode = int(intent.desired_ac_mode)
        startup_reconcile = self.is_startup_ac_mode_reconcile_intent(intent)

        if current_ac_mode == desired_ac_mode:
            log_event(
                logging.DEBUG,
                "ac_mode_intent_unchanged",
                **fields
            )
            return True

        if (
            desired_ac_mode == 2
            and current_ac_mode not in (1, 2)
            and (current_ac_mode != 0 or startup_reconcile)
        ):
            log_event(
                logging.WARNING,
                "unknown_ac_mode",
                **fields
            )
            return False

        if current_ac_mode == 1 and desired_ac_mode == 2 and startup_reconcile:
            skip_reason = startup_ac_mode_initialization_blocker(state)
            if skip_reason:
                log_event(
                    logging.INFO,
                    "ac_mode_intent_skip",
                    **{
                        **fields,
                        "reason": skip_reason,
                        "runtime_role_reason": intent.reason,
                    }
                )
                return False

        if not cfg.state_reconciliation_writes_allowed():
            log_event(
                logging.INFO,
                "dry_run_ac_mode_intent_write",
                **{
                    **fields,
                    "dry_run": cfg.DRY_RUN,
                    "simulation": cfg.SIMULATION_MODE,
                    "allow_hardware_writes": cfg.ALLOW_HARDWARE_WRITES,
                    "allow_state_reconciliation_writes": (
                        cfg.ALLOW_STATE_RECONCILIATION_WRITES
                    ),
                }
            )
            return False

        try:
            response = dev.session.post(
                f"http://{dev.ip}/properties/write",
                json={
                    "sn": dev.sn,
                    "properties": {
                        "acMode": desired_ac_mode
                    }
                },
                timeout=2
            )

            if not zendure_write_succeeded(
                "write_ac_mode_intent_error",
                dev,
                response,
                **fields
            ):
                return False

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
            return False

    def desired_runtime_input_limit(self, dev, intent):
        """Return desired runtime AC charge inputLimit, or None."""

        if intent.role is not DeviceRuntimeRole.AC_INPUT:
            return None

        if intent.reason == FULL_CHARGE_ASSIST_REASON:
            return cfg.safe_int(
                cfg.BATTERY_FULL_CHARGE_ASSIST_CONFIG.get(
                    "ac_charge_power",
                    200
                ),
                200,
                minimum=0
            )

        if not self.runtime_state:
            return None

        raw_value = self.runtime_state.get_device(
            dev.name,
            "ac_charge_power_w",
            None
        )

        if raw_value is None or raw_value == "":
            return None

        if isinstance(raw_value, bool):
            log_event(
                logging.WARNING,
                "runtime_ac_charge_power_invalid",
                device=dev.name,
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
                device=dev.name,
                value=raw_value,
                reason="not_integer"
            )
            return None

        if desired < 0:
            log_event(
                logging.WARNING,
                "runtime_ac_charge_power_invalid",
                device=dev.name,
                value=raw_value,
                reason="negative"
            )
            return None

        return desired

    def reconcile_runtime_ac_charge_power(self, dev, state, intent):
        """Write inputLimit when runtime AC input intent requests a new value."""

        desired_input_limit = self.desired_runtime_input_limit(dev, intent)
        if desired_input_limit is None:
            return True

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

        if not cfg.state_reconciliation_writes_allowed():
            log_event(
                logging.INFO,
                "runtime_ac_charge_power_write_skipped",
                **{
                    **fields,
                    "dry_run": cfg.DRY_RUN,
                    "simulation": cfg.SIMULATION_MODE,
                    "allow_hardware_writes": cfg.ALLOW_HARDWARE_WRITES,
                    "allow_state_reconciliation_writes": (
                        cfg.ALLOW_STATE_RECONCILIATION_WRITES
                    ),
                }
            )
            return False

        try:
            response = dev.session.post(
                f"http://{dev.ip}/properties/write",
                json={
                    "sn": dev.sn,
                    "properties": {
                        "inputLimit": desired_input_limit
                    }
                },
                timeout=2
            )

            if not zendure_write_succeeded(
                "runtime_ac_charge_power_write_error",
                dev,
                response,
                **fields
            ):
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

        return cfg.safe_int(getattr(state, "pack_num", 0), 0, minimum=0) > 0

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

        ac_restore_pending = (
            bool(record.get("ac_mode_restore_pending"))
            or bool(record.get("ac_input_request_pending"))
        )
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
        """Track and advance EMS full-charge assist for fresh telemetry."""

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
                    dev.name,
                    "full_charge_assist_soc_restored",
                    now,
                    state=state
                )
            record = store.get_device_state(dev.name, now)

        if feature_transition.get("current_enabled"):
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
                    desired_max_soc=100,
                    reason=FULL_CHARGE_ASSIST_REASON
                )
                if write_ok:
                    store.update_device_state(
                        dev.name,
                        now,
                        max_soc_request_pending=False
                    )

    def confirm_full_charge_assist_ac_restore(self, dev, state, intent, now):
        if not self.battery_full_charge_store:
            return
        if intent.reason != FULL_CHARGE_ASSIST_RESTORE_REASON:
            return

        record = self.battery_full_charge_store.get_device_state(dev.name, now)
        if not record or not record.get("ac_mode_restore_pending"):
            return

        if int(getattr(state, "ac_mode", 0) or 0) != 2:
            return

        self.battery_full_charge_store.update_device_state(
            dev.name,
            now,
            ac_mode_restore_pending=False
        )
        self.battery_full_charge_store.log_event(
            dev.name,
            "full_charge_assist_ac_mode_restored",
            now,
            state=state
        )

    def full_charge_assist_intent(self, dev, base_intent):
        store = self.battery_full_charge_store
        if not store:
            return base_intent

        record = store.get_device_state(dev.name)
        if not record or not record.get("has_battery"):
            return base_intent

        if record.get("full_charge_assist_active"):
            if self.full_charge_assist_config().get("enable_ac_charge_mode", True):
                return ac_input_intent(dev.name, FULL_CHARGE_ASSIST_REASON)

            return DeviceRuntimeIntent(
                device=dev.name,
                role=DeviceRuntimeRole.AC_OUTPUT,
                reason=FULL_CHARGE_ASSIST_REASON,
                desired_ac_mode=None,
                output_control_allowed=False,
                priority=100
            )

        if (
            record.get("restore_pending")
            or record.get("ac_mode_restore_pending")
        ):
            return DeviceRuntimeIntent(
                device=dev.name,
                role=DeviceRuntimeRole.AC_OUTPUT,
                reason=FULL_CHARGE_ASSIST_RESTORE_REASON,
                desired_ac_mode=(
                    2 if record.get("ac_mode_restore_pending") else None
                ),
                output_control_allowed=False,
                priority=100
            )

        return base_intent

    def update_full_charge_assist_ac_pending(self, dev, intent, write_ok, now):
        if not write_ok or not self.battery_full_charge_store:
            return

        record = self.battery_full_charge_store.get_device_state(dev.name, now)
        if not record:
            return

        if (
            intent.reason == FULL_CHARGE_ASSIST_REASON
            and record.get("ac_input_request_pending")
        ):
            self.battery_full_charge_store.update_device_state(
                dev.name,
                now,
                ac_input_request_pending=False
            )

    def ha_update_runtime_field(
        self,
        entity_id,
        runtime_getter,
        runtime_setter,
        parser,
        formatter=lambda value: value
    ):
        """Synchronize one HA helper with one runtime-state field."""

        if not self.ha or not cfg.HA_CONTROL_ENABLED or not self.runtime_state:
            return False

        try:
            ha_state = self.ha.get_state(entity_id)
        except Exception as e:
            log_event(
                logging.WARNING,
                "runtime_state_ha_read_error",
                entity=entity_id,
                error=e
            )
            return False

        runtime_value = runtime_getter()

        if ha_state is None:
            return False

        try:
            parsed_ha = parser(ha_state, runtime_value)
        except Exception as e:
            log_event(
                logging.WARNING,
                "runtime_state_ha_read_error",
                entity=entity_id,
                state=ha_state,
                error=e
            )
            return False

        last_written = self.last_ha_written.get(entity_id)
        last_seen = self.last_ha_seen.get(entity_id)

        if (
            last_seen is not None
            and parsed_ha != last_seen
            and parsed_ha != last_written
        ):
            changed = runtime_setter(parsed_ha)
            self.last_ha_seen[entity_id] = parsed_ha

            if changed:
                log_event(
                    logging.INFO,
                    "runtime_state_ha_sync",
                    direction="ha_to_runtime",
                    entity=entity_id,
                    value=parsed_ha
                )

            return changed

        if parsed_ha != runtime_value and runtime_value != last_written:
            self.ha.set_state(entity_id, formatter(runtime_value))
            self.last_ha_written[entity_id] = runtime_value
            self.last_ha_seen[entity_id] = runtime_value
            log_event(
                logging.INFO,
                "runtime_state_ha_write",
                entity=entity_id,
                value=runtime_value
            )
            return False

        self.last_ha_seen[entity_id] = parsed_ha
        return False

    def sync_ha_runtime_state(self):
        """Use HA helpers as an optional UI over runtime-state."""

        if not self.ha or not cfg.HA_CONTROL_ENABLED or not self.runtime_state:
            return

        changed = False

        changed |= self.ha_update_runtime_field(
            "input_boolean.ems_solarflow_ha_enabled",
            lambda: self.runtime_ha_enabled(),
            lambda value: self.runtime_state.set_section(
                "ha",
                "enabled",
                value
            ),
            lambda value, default: cfg.safe_bool(value, default),
            lambda value: "on" if value else "off"
        )

        changed |= self.ha_update_runtime_field(
            "input_boolean.ems_solarflow_ha_control_enabled",
            lambda: self.runtime_ha_control_enabled(),
            lambda value: self.runtime_state.set_section(
                "ha",
                "control_enabled",
                value
            ),
            lambda value, default: cfg.safe_bool(value, default),
            lambda value: "on" if value else "off"
        )

        if (
            not self.runtime_ha_enabled()
            or not self.runtime_ha_control_enabled()
        ):
            if changed:
                self.runtime_state.save_atomic()
            return

        changed |= self.ha_update_runtime_field(
            "input_boolean.ems_solarflow_enable",
            lambda: self.runtime_system_bool("enabled", cfg.SYSTEM_ENABLED),
            lambda value: self.runtime_state.set_system("enabled", value),
            lambda value, default: cfg.safe_bool(value, default),
            lambda value: "on" if value else "off"
        )

        changed |= self.ha_update_runtime_field(
            "input_number.ems_solarflow_max_power",
            lambda: self.runtime_system_int(
                "max_total_power",
                cfg.MAX_TOTAL_POWER,
                minimum=0
            ),
            lambda value: self.runtime_state.set_system(
                "max_total_power",
                value
            ),
            lambda value, default: cfg.safe_int(value, default, minimum=0)
        )

        changed |= self.ha_update_runtime_field(
            "input_number.ems_solarflow_interval",
            lambda: self.runtime_system_int(
                "loop_interval",
                cfg.LOOP_INTERVAL,
                minimum=1
            ),
            lambda value: self.runtime_state.set_system(
                "loop_interval",
                value
            ),
            lambda value, default: cfg.safe_int(value, default, minimum=1)
        )

        changed |= self.ha_update_runtime_field(
            "input_number.ems_solarflow_min_output_limit",
            lambda: self.runtime_system_int(
                "min_output_limit",
                cfg.MIN_OUTPUT_LIMIT,
                minimum=0
            ),
            lambda value: self.runtime_state.set_system(
                "min_output_limit",
                value
            ),
            lambda value, default: cfg.safe_int(value, default, minimum=0)
        )

        changed |= self.ha_update_runtime_field(
            "input_boolean.ems_solarflow_winter_enabled",
            lambda: cfg.winter_feature_enabled(self.runtime_state),
            lambda value: self.runtime_state.set_section(
                "winter",
                "enabled",
                value
            ),
            lambda value, default: cfg.safe_bool(value, default),
            lambda value: "on" if value else "off"
        )

        for dev in self.devices:
            base = f"ems_solarflow_{dev.name.lower()}"

            changed |= self.ha_update_runtime_field(
                f"input_boolean.{base}_enabled",
                lambda dev=dev: self.runtime_device_bool(
                    dev.name,
                    "enabled",
                    True
                ),
                lambda value, dev=dev: self.runtime_state.set_device(
                    dev.name,
                    "enabled",
                    value
                ),
                lambda value, default: cfg.safe_bool(value, default),
                lambda value: "on" if value else "off"
            )

            changed |= self.ha_update_runtime_field(
                f"input_number.{base}_max_power",
                lambda dev=dev: self.runtime_device_int(
                    dev.name,
                    "max_power",
                    dev.max_power,
                    minimum=0
                ),
                lambda value, dev=dev: self.runtime_state.set_device(
                    dev.name,
                    "max_power",
                    value
                ),
                lambda value, default: cfg.safe_int(value, default, minimum=0)
            )

            changed |= self.ha_update_runtime_field(
                f"input_select.{base}_offgrid_socket_mode",
                lambda dev=dev: str(
                    self.runtime_state.get_device(
                        dev.name,
                        "offgrid_socket_mode",
                        "off"
                    )
                ),
                lambda value, dev=dev: self.runtime_state.set_device(
                    dev.name,
                    "offgrid_socket_mode",
                    value
                ),
                lambda value, default: (
                    str(value).strip().lower()
                    if str(value).strip().lower() in cfg.OFFGRID_SOCKET_MODES
                    else default
                )
            )

        if changed:
            self.runtime_state.save_atomic()

    def set_output_limit(self, dev, value):
        """Write output limit to device."""

        if not cfg.hardware_writes_allowed():
            log_event(
                logging.INFO,
                "dry_run_output_limit",
                device=dev.name,
                target_w=value,
                dry_run=cfg.DRY_RUN,
                simulation=cfg.SIMULATION_MODE,
                allow_hardware_writes=cfg.ALLOW_HARDWARE_WRITES
            )
            return

        try:
            response = dev.session.post(
                f"http://{dev.ip}/properties/write",
                json={
                    "sn": dev.sn,
                    "properties": {
                        "outputLimit": int(value)
                    }
                },
                timeout=2
            )

            if not zendure_write_succeeded(
                "write_output_limit_error",
                dev,
                response,
                target_w=value
            ):
                return

            log_event(
                logging.INFO,
                "write_output_limit",
                device=dev.name,
                target_w=value
            )

        except Exception as e:
            log_event(
                logging.WARNING,
                "write_output_limit_error",
                device=dev.name,
                error=e
            )

    def apply_soc_limits(
        self,
        dev,
        state,
        desired_min_soc=None,
        desired_max_soc=None,
        reason="soc_reconcile"
    ):
        """Apply configured SOC limits if required."""

        effective_min_soc = (
            cfg.safe_int(desired_min_soc, dev.min_soc, minimum=0)
            if desired_min_soc is not None
            else dev.min_soc
        )
        effective_max_soc = (
            cfg.safe_int(desired_max_soc, dev.max_soc, minimum=0)
            if desired_max_soc is not None
            else dev.max_soc
        )

        #
        # 0 = unmanaged
        #

        if effective_min_soc <= 0 and effective_max_soc <= 0:
            return True

        #
        # Already configured
        #

        if (
            int(state.min_soc) == int(effective_min_soc)
            and
            int(state.max_soc) == int(effective_max_soc)
        ):

            log_event(
                logging.INFO,
                "soc_limits_unchanged",
                device=dev.name,
                reason=reason
            )

            return True

        if not cfg.state_reconciliation_writes_allowed():
            log_event(
                logging.INFO,
                "dry_run_soc_limits",
                device=dev.name,
                min_soc=effective_min_soc,
                max_soc=effective_max_soc,
                max_soc_property="socSet",
                reason=reason,
                dry_run=cfg.DRY_RUN,
                simulation=cfg.SIMULATION_MODE,
                allow_hardware_writes=cfg.ALLOW_HARDWARE_WRITES,
                allow_state_reconciliation_writes=(
                    cfg.ALLOW_STATE_RECONCILIATION_WRITES
                )
            )
            return False

        try:

            response = dev.session.post(
                f"http://{dev.ip}/properties/write",
                json={
                    "sn": dev.sn,
                    "properties": {
                        "minSoc": int(effective_min_soc * 10),
                        "socSet": int(effective_max_soc * 10)
                    }
                },
                timeout=2
            )

            if not zendure_write_succeeded(
                "write_soc_limits_error",
                dev,
                response,
                min_soc=effective_min_soc,
                max_soc=effective_max_soc,
                max_soc_property="socSet",
                reason=reason
            ):
                return False

            log_event(
                logging.INFO,
                "write_soc_limits",
                device=dev.name,
                min_soc=effective_min_soc,
                max_soc=effective_max_soc,
                max_soc_property="socSet",
                reason=reason
            )
            return True

        except Exception as e:

            log_event(
                logging.WARNING,
                "write_soc_limits_error",
                device=dev.name,
                min_soc=effective_min_soc,
                max_soc=effective_max_soc,
                max_soc_property="socSet",
                reason=reason,
                error=e
            )
            return False

    def winter_reconciliation_target(self, dev, state, winter_active, adjust_today):
        """Return desired winter/summer minSoc target and adjustment context."""

        if not cfg.winter_feature_enabled(self.runtime_state):
            return None, False

        summer_min_soc = cfg.winter_config_int("summer_min_soc", 15, minimum=0)

        if not winter_active:
            had_target = dev.name in self.winter_min_soc_targets
            self.winter_min_soc_targets.pop(dev.name, None)

            if had_target or int(state.min_soc) != int(summer_min_soc):
                log_event(
                    logging.INFO,
                    "winter_summer_reset",
                    device=dev.name,
                    current_min_soc=state.min_soc,
                    target_min_soc=summer_min_soc
                )

            return summer_min_soc, False

        if dev.name in self.winter_min_soc_targets and not adjust_today:
            return self.winter_min_soc_targets[dev.name], False

        if not adjust_today:
            return None, False

        effective_min_soc = self.winter_min_soc_targets.get(
            dev.name,
            state.min_soc if state.min_soc > 0 else dev.min_soc
        )
        target = cfg.calculate_winter_min_soc_target(
            state.soc,
            effective_min_soc,
            winter_active
        )
        self.winter_min_soc_targets[dev.name] = target

        log_event(
            logging.INFO,
            "winter_ramp",
            device=dev.name,
            current_soc=state.soc,
            current_min_soc=state.min_soc,
            effective_min_soc=effective_min_soc,
            target_min_soc=target,
            winter_min_soc=cfg.winter_config_int("winter_min_soc", 40, minimum=0),
            estimated_days_remaining=cfg.estimate_winter_ramp_days(target)
        )

        return target, True

    def apply_winter_ac_charge_limit(self, dev):
        """Apply conservative winter AC charge input limit."""

        properties = cfg.build_winter_ac_charge_limit_payload()
        fields = {
            "device": dev.name,
            "input_limit_w": properties["inputLimit"]
        }

        if not cfg.state_reconciliation_writes_allowed():
            fields.update({
                "dry_run": cfg.DRY_RUN,
                "simulation": cfg.SIMULATION_MODE,
                "allow_hardware_writes": cfg.ALLOW_HARDWARE_WRITES,
                "allow_state_reconciliation_writes": (
                    cfg.ALLOW_STATE_RECONCILIATION_WRITES
                )
            })

            log_event(
                logging.INFO,
                "dry_run_winter_ac_charge_limit",
                **fields
            )
            return

        try:
            response = dev.session.post(
                f"http://{dev.ip}/properties/write",
                json={
                    "sn": dev.sn,
                    "properties": properties
                },
                timeout=2
            )

            if not zendure_write_succeeded(
                "write_winter_ac_charge_limit_error",
                dev,
                response,
                **fields
            ):
                return

            log_event(
                logging.INFO,
                "write_winter_ac_charge_limit",
                **fields
            )

        except Exception as e:
            log_event(
                logging.WARNING,
                "write_winter_ac_charge_limit_error",
                device=dev.name,
                input_limit_w=properties["inputLimit"],
                error=e
            )

    def run_startup_ac_mode_reconcile_once(self, dev, state):
        """Compatibility wrapper for legacy startup acMode reconciliation."""

        if self.initial_ac_mode_reconciled.get(dev.name, False):
            return

        self.initial_ac_mode_reconciled[dev.name] = True

        if not cfg.RECONCILE_AC_MODE_ON_START:
            log_event(
                logging.INFO,
                "startup_ac_mode_reconcile_disabled",
                device=dev.name
            )
            return

        self.reconcile_ac_mode_intent(
            dev,
            state,
            ac_output_intent(dev.name, STARTUP_AC_MODE_RECONCILE_REASON)
        )

    def apply_device_modes(self, dev, state):
        """Apply device operating modes if required."""

        manage_grid_off_mode = dev.grid_off_mode is not None
        properties = {}
        fields = {
            "device": dev.name
        }

        if (
            cfg.RECONCILE_SMART_MODE
            and dev.smart_mode is not None
            and int(state.smart_mode) != int(dev.smart_mode)
        ):
            properties["smartMode"] = int(dev.smart_mode)
            fields["smart_mode"] = dev.smart_mode

        if (
            manage_grid_off_mode
            and int(state.grid_off_mode) != int(dev.grid_off_mode)
        ):
            properties["gridOffMode"] = int(dev.grid_off_mode)
            fields["grid_off_mode"] = dev.grid_off_mode

        if (
            int(state.ac_mode) != 2
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
                logging.INFO,
                "device_modes_unchanged",
                device=dev.name
            )

            return

        if not cfg.state_reconciliation_writes_allowed():
            fields.update({
                "dry_run": cfg.DRY_RUN,
                "simulation": cfg.SIMULATION_MODE,
                "allow_hardware_writes": cfg.ALLOW_HARDWARE_WRITES,
                "allow_state_reconciliation_writes": (
                    cfg.ALLOW_STATE_RECONCILIATION_WRITES
                )
            })

            log_event(
                logging.INFO,
                "dry_run_device_modes",
                **fields
            )
            return

        try:
            response = dev.session.post(
                f"http://{dev.ip}/properties/write",
                json={
                    "sn": dev.sn,
                    "properties": properties
                },
                timeout=2
            )

            if not zendure_write_succeeded(
                "write_device_modes_error",
                dev,
                response,
                **fields
            ):
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

    def apply_runtime_device_state(self, dev, state):
        """Apply runtime-state device intents through safe reconciliation."""

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
            log_event(
                logging.INFO,
                "runtime_device_state_unchanged",
                **fields
            )
            return

        if not cfg.state_reconciliation_writes_allowed():
            fields.update({
                "dry_run": cfg.DRY_RUN,
                "simulation": cfg.SIMULATION_MODE,
                "allow_hardware_writes": cfg.ALLOW_HARDWARE_WRITES,
                "allow_state_reconciliation_writes": (
                    cfg.ALLOW_STATE_RECONCILIATION_WRITES
                )
            })

            log_event(
                logging.INFO,
                "dry_run_runtime_device_state_write",
                **fields
            )
            return

        try:
            response = dev.session.post(
                f"http://{dev.ip}/properties/write",
                json={
                    "sn": dev.sn,
                    "properties": {
                        "gridOffMode": desired_grid_off_mode
                    }
                },
                timeout=2
            )

            if not zendure_write_succeeded(
                "write_runtime_device_state_error",
                dev,
                response,
                **fields
            ):
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


    def publish_sensor(
        self,
        entity,
        value,
        unit=None,
        device_class=None,
        state_class="measurement",
        icon=None,
        extra=None
    ):
        self.ha.set_state(
            entity,
            value,
            unit=unit,
            device_class=device_class,
            state_class=state_class,
            icon=icon,
            extra_attributes=extra
        )

    def device_ha_availability_attributes(self, dev):
        """Return HA attributes describing device telemetry freshness."""

        online = self.device_online.get(dev.name, True)
        seen = self.last_seen.get(dev.name)
        attrs = {
            "available": online,
            "telemetry_source": "live" if online else "cached"
        }

        if seen is None:
            attrs["last_seen"] = None

            if not online:
                attrs["telemetry_source"] = "zero_fallback"

            return attrs

        attrs["last_seen"] = datetime.fromtimestamp(seen).isoformat()
        attrs["last_seen_age_s"] = round(time.time() - seen, 1)

        return attrs

    def device_ha_extra(self, dev, extra=None):
        attrs = self.device_ha_availability_attributes(dev)

        if extra:
            attrs.update(extra)

        return attrs

    def publish_winter_to_ha(self, states):
        """Publish winter-mode state and calculated targets to HA."""

        now = datetime.now()
        enabled = cfg.winter_feature_enabled(self.runtime_state)
        active = cfg.winter_mode_active(now, self.runtime_state)
        adjust_window = cfg.winter_adjustment_window_active(now)
        p = "sensor.ems_solarflow_"

        self.ha.set_state(
            "binary_sensor.ems_solarflow_winter_enabled",
            "on" if enabled else "off",
            extra_attributes={
                "months": ",".join(str(m) for m in cfg.winter_months())
            }
        )

        self.ha.set_state(
            "binary_sensor.ems_solarflow_winter_active",
            "on" if active else "off",
            extra_attributes={
                "month": now.month
            }
        )

        self.ha.set_state(
            "binary_sensor.ems_solarflow_winter_adjust_window",
            "on" if adjust_window else "off",
            extra_attributes={
                "adjust_hour": cfg.winter_config_int(
                    "adjust_hour",
                    12,
                    minimum=0
                ) % 24
            }
        )

        self.publish_sensor(
            p + "winter_summer_min_soc",
            cfg.winter_config_int("summer_min_soc", 15, minimum=0),
            "%",
            "battery"
        )

        self.publish_sensor(
            p + "winter_min_soc",
            cfg.winter_config_int("winter_min_soc", 40, minimum=0),
            "%",
            "battery"
        )

        self.publish_sensor(
            p + "winter_ramp_step",
            cfg.winter_config_int("ramp_step_percent", 5, minimum=1),
            "%",
            None
        )

        self.publish_sensor(
            p + "winter_ac_charge_power",
            cfg.winter_config_int("ac_charge_power", 200, minimum=0),
            "W",
            "power"
        )

        self.publish_sensor(
            p + "winter_last_adjust_date",
            self.last_winter_adjust_date or "never",
            state_class=None,
            icon="mdi:calendar-clock"
        )

        for dev, state in zip(self.devices, states):
            base = p + dev.name.lower() + "_winter_"
            effective_min_soc = self.winter_min_soc_targets.get(
                dev.name,
                state.min_soc if state.min_soc > 0 else dev.min_soc
            )
            target = cfg.calculate_winter_min_soc_target(
                state.soc,
                effective_min_soc,
                active
            )

            self.publish_sensor(
                base + "min_soc_target",
                target,
                "%",
                "battery",
                extra=self.device_ha_extra(
                    dev,
                    {
                        "effective_min_soc": effective_min_soc,
                        "current_soc": state.soc,
                        "winter_active": active
                    }
                )
            )

            self.publish_sensor(
                base + "estimated_ramp_days",
                cfg.estimate_winter_ramp_days(target),
                "d",
                None,
                icon="mdi:calendar-range",
                extra=self.device_ha_extra(dev)
            )

    def publish_to_ha(
        self,
        load,
        states,
        targets,
        effective_targets,
        current,
        new
    ):
        """Publish values to Home Assistant."""

        if not states:
            log_event(logging.WARNING, "ha_publish_no_devices")
            return

        p = "sensor.ems_solarflow_"

        solar_total = sum(d.solar for d in states)
        pack_in_total = sum(d.pack_in for d in states)
        pack_out_total = sum(d.pack_out for d in states)

        battery_power = pack_out_total - pack_in_total
        home = current + max(load, 0)
        allocated_target_total = sum(targets)
        effective_target_total = sum(effective_targets)

        soc_avg = round(
            sum(d.soc for d in states) / len(states),
            1
        )

        # =====================
        # GLOBAL
        # =====================

        self.publish_sensor(
            p + "load",
            round(load, 1),
            "W",
            "power"
        )

        self.publish_sensor(
            p + "target_total",
            round(effective_target_total, 1),
            "W",
            "power",
            extra={
                "controller_target_w": round(new, 1),
                "allocated_target_w": round(allocated_target_total, 1)
            }
        )

        self.publish_sensor(
            p + "solar_total",
            round(solar_total, 1),
            "W",
            "power"
        )

        self.publish_sensor(
            p + "battery_power",
            round(battery_power, 1),
            "W",
            "power"
        )

        self.publish_sensor(
            p + "home",
            round(home, 1),
            "W",
            "power"
        )

        self.publish_sensor(
            p + "soc_avg",
            soc_avg,
            "%",
            "battery"
        )

        self.publish_winter_to_ha(states)

        # =====================
        # PER DEVICE
        # =====================

        for i, dev in enumerate(self.devices):

            d = states[i]

            base = p + dev.name.lower() + "_"

            def device_extra(extra=None):
                return self.device_ha_extra(dev, extra)

            self.ha.set_state(
                f"binary_sensor.{dev.name.lower()}_available",
                "on" if self.device_online.get(dev.name, True) else "off",
                device_class="connectivity",
                extra_attributes=device_extra()
            )

            # Core
            self.publish_sensor(
                base + "soc",
                d.soc,
                "%",
                "battery",
                extra=device_extra()
            )

            self.publish_sensor(
                base + "min_soc",
                d.min_soc,
                "%",
                "battery",
                extra=device_extra()
            )

            self.publish_sensor(
                base + "max_soc",
                d.max_soc,
                "%",
                "battery",
                extra=device_extra()
            )

            self.publish_sensor(
                base + "solar",
                d.solar,
                "W",
                "power",
                extra=device_extra()
            )

            self.publish_sensor(
                base + "output",
                d.output,
                "W",
                "power",
                extra=device_extra()
            )

            self.publish_sensor(
                base + "target",
                effective_targets[i],
                "W",
                "power",
                extra=device_extra({"allocated_target_w": targets[i]})
            )

            self.publish_sensor(
                base + "output_limit",
                d.output_limit,
                "W",
                "power",
                extra=device_extra()
            )

            self.publish_sensor(
                base + "soc_limit",
                d.soc_limit,
                state_class=None,
                extra=device_extra()
            )

            self.publish_sensor(
                base + "pack_state",
                d.pack_state,
                state_class=None,
                extra=device_extra()
            )

            # Zendure API uses controller/inverter perspective:
            # outputPackPower = charging
            # packInputPower  = discharging
            #
            # EMS convention:
            # Positive = charging
            # Negative = discharging

            device_battery_power = d.pack_out - d.pack_in
            history = self.battery_power_history.setdefault(
                dev.name,
                deque(maxlen=cfg.REMAINING_TIME_POWER_SAMPLES)
            )
            history.append(device_battery_power)

            avg_battery_power = sum(history) / len(history)
            remaining_time = calculate_remaining_time_hours(
                d,
                dev,
                avg_battery_power
            )

            self.publish_sensor(
                base + "battery_power",
                round(device_battery_power, 1),
                "W",
                "power",
                extra=device_extra()
            )

            self.publish_sensor(
                base + "battery_power_avg",
                round(avg_battery_power, 1),
                "W",
                "power",
                extra=device_extra()
            )

            self.publish_sensor(
                base + "voltage",
                d.voltage,
                "V",
                "voltage",
                extra=device_extra()
            )

            self.publish_sensor(
                base + "remaining_minutes",
                d.remain_minutes,
                "min",
                state_class=None,
                icon="mdi:timer-outline",
                extra=device_extra()
            )

            self.publish_sensor(
                base + "remaining_time",
                remaining_time,
                "h",
                "duration",
                icon="mdi:timer-outline",
                extra=device_extra()
            )

            # Thermal / Signal
            self.publish_sensor(
                base + "temp",
                d.temp,
                "°C",
                "temperature",
                extra=device_extra()
            )

            self.publish_sensor(
                base + "rssi",
                d.rssi,
                "dBm",
                "signal_strength",
                extra=device_extra()
            )

            # Panels
            self.publish_sensor(
                base + "panel1",
                d.solar1,
                "W",
                "power",
                extra=device_extra()
            )

            self.publish_sensor(
                base + "panel2",
                d.solar2,
                "W",
                "power",
                extra=device_extra()
            )

            self.publish_sensor(
                base + "panel3",
                d.solar3,
                "W",
                "power",
                extra=device_extra()
            )

            self.publish_sensor(
                base + "panel4",
                d.solar4,
                "W",
                "power",
                extra=device_extra()
            )

            # Status
            self.publish_sensor(
                base + "fault_level",
                d.fault_level,
                state_class=None,
                extra=device_extra()
            )

            self.ha.set_state(
                f"binary_sensor.{dev.name.lower()}_fault",
                "on" if d.fault_level > 0 else "off",
                device_class="problem",
                extra_attributes=device_extra({"fault_level": d.fault_level})
            )

            self.ha.set_state(
                f"binary_sensor.{dev.name.lower()}_ac_active",
                "on" if d.ac_status else "off",
                device_class="power",
                extra_attributes=device_extra()
            )

            self.ha.set_state(
                f"binary_sensor.{dev.name.lower()}_dc_active",
                "on" if d.dc_status else "off",
                device_class="power",
                extra_attributes=device_extra()
            )

            self.ha.set_state(
                f"binary_sensor.{dev.name.lower()}_grid_online",
                "on" if d.grid_state else "off",
                device_class="connectivity",
                extra_attributes=device_extra()
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

            lines = build_telemetry_lines(
                self.devices, states, self.device_online, load, target
            )
            writer.enqueue(lines)
        except Exception as e:
            log_event(logging.WARNING, "influx_publish_error", error=e)

    def run_once(self):
        """Execute one EMS cycle."""

        start = time.time()
        self.last_control_explanation = None

        if self.runtime_state:
            self.runtime_state.load_if_changed()

        try:
            self.sync_ha_runtime_state()
        except Exception as e:
            log_event(
                logging.WARNING,
                "ha_runtime_sync_failed",
                error=e
            )

        load = self.shelly.get_power()

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

        states = []

        for dev, state in zip(self.devices, raw_states):

            #
            # Fresh state available
            #

            if state:

                self.last_states[dev.name] = state
                self.last_seen[dev.name] = time.time()
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
        self.runtime_intents = {}
        for dev, state in zip(self.devices, states):
            self.runtime_intents[dev.name] = ac_output_intent(dev.name)

        for dev, state in zip(
            self.devices,
            raw_states
        ):
            if not state:
                continue

            intent = self.full_charge_assist_intent(
                dev,
                self.get_device_runtime_intent(dev, state)
            )
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
                can_charge=cap.can_charge,
                can_discharge=cap.can_discharge,
                can_export=cap.can_export,
                can_ac_charge=cap.can_ac_charge,
                reason=cap.reason
            )

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
                winter_window_active = cfg.winter_adjustment_window_active(now)
                today = now.date().isoformat()
                winter_adjust_today = (
                    winter_active
                    and winter_window_active
                    and self.last_winter_adjust_date != today
                )

                if cfg.winter_feature_enabled(self.runtime_state):
                    log_event(
                        logging.INFO,
                        "winter_mode_state",
                        active=winter_active,
                        month=now.month,
                        adjust_window=winter_window_active,
                        adjust_today=winter_adjust_today,
                        last_adjust_date=self.last_winter_adjust_date,
                        summer_min_soc=cfg.winter_config_int(
                            "summer_min_soc",
                            15,
                            minimum=0
                        ),
                        winter_min_soc=cfg.winter_config_int(
                            "winter_min_soc",
                            40,
                            minimum=0
                        )
                    )

                for dev, state in zip(
                    self.devices,
                    raw_states
                ):

                    if state:
                        desired_min_soc, winter_adjustment = (
                            self.winter_reconciliation_target(
                                dev,
                                state,
                                winter_active,
                                winter_adjust_today
                            )
                        )

                        self.apply_soc_limits(
                            dev,
                            state,
                            desired_min_soc=desired_min_soc
                        )

                        self.apply_device_modes(
                            dev,
                            state
                        )

                        if winter_adjustment:
                            self.apply_winter_ac_charge_limit(dev)

                if winter_adjust_today:
                    self.last_winter_adjust_date = today

        for dev, state in zip(
            self.devices,
            raw_states
        ):

            if state:

                self.apply_runtime_device_state(
                    dev,
                    state
                )

        controllable_indexes = self.night_min_soc_controllable_indices()
        night_min_soc_idle = self.update_night_min_soc_idle_state(
            states,
            controllable_indexes,
            enabled,
            min_output_limit
        )

        if night_min_soc_idle:
            targets = [
                min_output_limit if i in controllable_indexes else 0
                for i, _dev in enumerate(self.devices)
            ]
            effective_targets = list(targets)
            current = sum(d.output for d in states)
            new = sum(effective_targets)

            logging.info(
                f"Load={load}W "
                f"Target={new}W "
                f"Enabled={enabled} "
                f"NightMinSocIdle=True"
            )

            if self.ha and self.runtime_ha_enabled():
                self.publish_to_ha(
                    load,
                    states,
                    targets,
                    effective_targets,
                    current,
                    new
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

        stabilized_total = self.stabilized_total_target(
            load,
            states,
            max_power,
            has_export_capacity=has_export_capacity,
            standby_total_w=standby_total_w,
            active_device_count=len(active_indexes)
        )

        targets, current, new, control_explanation = calculate_targets(
            load,
            states,
            max_power,
            device_configs=self.devices,
            capabilities=capabilities,
            requested_total=stabilized_total,
            explain=True,
            online_devices=self.device_online
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
            if i < len(targets):
                device_explanation.allocated_target_w = targets[i]
            if i < len(effective_targets):
                device_explanation.effective_target_w = effective_targets[i]
                device_explanation.command_target_w = effective_targets[i]
                if device_explanation.raw_target_w is not None:
                    device_explanation.adjustment_delta_w = (
                        effective_targets[i] - device_explanation.raw_target_w
                    )

                if not enabled:
                    device_explanation.write_decision = "blocked"
                    device_explanation.write_reason = "control_disabled"
                elif not self.device_online.get(dev.name, True):
                    device_explanation.write_decision = "blocked"
                    device_explanation.write_reason = "offline"
                elif not self.runtime_device_bool(dev.name, "enabled", True):
                    device_explanation.write_decision = "blocked"
                    device_explanation.write_reason = "device_disabled"
                elif not self.device_output_control_allowed_by_intent(dev.name):
                    intent = self.runtime_intents.get(dev.name)
                    device_explanation.write_decision = "blocked"
                    device_explanation.write_reason = (
                        f"runtime_role_{intent.role.value}"
                        if intent
                        else "runtime_role_blocked"
                    )
                    device_explanation.limiting_reason = (
                        f"runtime_role_{intent.role.value}"
                        if intent
                        else "runtime_role_blocked"
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

        logging.info(
            f"Load={load}W "
            f"ControllerTarget={new}W "
            f"AllocatedTarget={sum(targets)}W "
            f"EffectiveTarget={sum(effective_targets)}W "
            f"Enabled={enabled}"
        )

        # =====================
        # PUBLISH TO HA
        # =====================

        if self.ha and self.runtime_ha_enabled():
            self.publish_to_ha(
                load,
                states,
                targets,
                effective_targets,
                current,
                new
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
                continue

            if not self.device_online.get(dev.name, True):

                log_event(
                    logging.WARNING,
                    "offline_skip_write",
                    device=dev.name
                )

                continue

            if not self.runtime_device_bool(dev.name, "enabled", True):
                log_event(
                    logging.INFO,
                    "device_disabled_skip_write",
                    device=dev.name,
                    target_w=targets[i]
                )
                continue

            if not self.device_output_control_allowed_by_intent(dev.name):
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
                continue

            target = effective_targets[i]

            if target != targets[i] and min_output_limit > 0:
                apply_min_output_limit(
                    targets[i],
                    dev,
                    min_output_limit
                )

            deadband_reference = (
                states[i].output_limit
                if states[i].output_limit > 0
                else states[i].output
            )
            deadband_reference_source = (
                "output_limit"
                if states[i].output_limit > 0
                else "output"
            )

            if abs(target - deadband_reference) < cfg.DEADBAND:
                log_event(
                    logging.DEBUG,
                    "deadband_skip_write",
                    device=dev.name,
                    target_w=target,
                    reference_w=deadband_reference,
                    reference_source=deadband_reference_source,
                    deadband_w=cfg.DEADBAND
                )
                continue

            self.set_output_limit(dev, target)

        # =====================
        # LOOP TIMING
        # =====================

        elapsed = time.time() - start

        if self.sleep_enabled:
            time.sleep(max(0, interval - elapsed))

# =====================
# SIMULATION / REPLAY
# =====================
