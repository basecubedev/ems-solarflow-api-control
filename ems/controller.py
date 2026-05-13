import json
import logging
import time
from collections import deque
from datetime import datetime

from ems import config as cfg
from ems.clients import fetch_all_devices, zendure_write_succeeded
from ems.logging_utils import log_event
from ems.target_control import (
    apply_min_output_limit,
    calculate_remaining_time_hours,
    calculate_targets,
    detect_capabilities,
    derive_soc_runtime_state,
    firmware_recovery_or_ac_charge_active,
    startup_ac_mode_initialization_blocker,
)


class EMSController:
    """Main EMS control loop."""

    def __init__(
        self,
        devices,
        shelly,
        ha=None,
        sleep_enabled=True,
        runtime_state=None
    ):
        self.devices = devices
        self.shelly = shelly
        self.ha = ha
        self.sleep_enabled = sleep_enabled
        self.runtime_state = runtime_state
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
        self.last_output_write_at = {}
        self.last_winter_adjust_date = None
        self.winter_min_soc_targets = {}
        self.night_min_soc_idle_active = False
        self.night_min_soc_idle_parked = set()

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
            for dev, target in zip(self.devices, targets):
                self.commanded_device_targets[dev.name] = target
            return targets

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
            self.last_output_write_at[dev.name] = time.time()
            self.night_min_soc_idle_parked.add(dev.name)
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
        return self.runtime_section_bool(
            "ha",
            "enabled",
            cfg.CONFIG["ha"].get("enabled", True)
        )

    def runtime_ha_control_enabled(self):
        return self.runtime_section_bool(
            "ha",
            "control_enabled",
            cfg.CONFIG["ha"].get("control_enabled", True)
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

    def apply_soc_limits(self, dev, state, desired_min_soc=None):
        """Apply configured SOC limits if required."""

        effective_min_soc = (
            cfg.safe_int(desired_min_soc, dev.min_soc, minimum=0)
            if desired_min_soc is not None
            else dev.min_soc
        )

        #
        # 0 = unmanaged
        #

        if effective_min_soc <= 0 and dev.max_soc <= 0:
            return

        #
        # Already configured
        #

        if (
            int(state.min_soc) == int(effective_min_soc)
            and
            int(state.max_soc) == int(dev.max_soc)
        ):

            log_event(
                logging.INFO,
                "soc_limits_unchanged",
                device=dev.name
            )

            return

        if not cfg.state_reconciliation_writes_allowed():
            log_event(
                logging.INFO,
                "dry_run_soc_limits",
                device=dev.name,
                min_soc=effective_min_soc,
                max_soc=dev.max_soc,
                max_soc_property="socSet",
                dry_run=cfg.DRY_RUN,
                simulation=cfg.SIMULATION_MODE,
                allow_hardware_writes=cfg.ALLOW_HARDWARE_WRITES,
                allow_state_reconciliation_writes=(
                    cfg.ALLOW_STATE_RECONCILIATION_WRITES
                )
            )
            return

        try:

            response = dev.session.post(
                f"http://{dev.ip}/properties/write",
                json={
                    "sn": dev.sn,
                    "properties": {
                        "minSoc": int(effective_min_soc * 10),
                        "socSet": int(dev.max_soc * 10)
                    }
                },
                timeout=2
            )

            if not zendure_write_succeeded(
                "write_soc_limits_error",
                dev,
                response,
                min_soc=effective_min_soc,
                max_soc=dev.max_soc,
                max_soc_property="socSet"
            ):
                return

            log_event(
                logging.INFO,
                "write_soc_limits",
                device=dev.name,
                min_soc=effective_min_soc,
                max_soc=dev.max_soc,
                max_soc_property="socSet"
            )

        except Exception as e:

            log_event(
                logging.WARNING,
                "write_soc_limits_error",
                device=dev.name,
                min_soc=effective_min_soc,
                max_soc=dev.max_soc,
                max_soc_property="socSet",
                error=e
            )

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
        """Initialize acMode=2 at most once after first valid telemetry."""

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

        if int(state.ac_mode) == 2:
            log_event(
                logging.INFO,
                "startup_ac_mode_already_ok",
                device=dev.name,
                ac_mode=state.ac_mode,
                ac_status=state.ac_status
            )
            return

        skip_reason = startup_ac_mode_initialization_blocker(state)

        if skip_reason:
            log_event(
                logging.INFO,
                "startup_ac_mode_skip",
                device=dev.name,
                ac_mode=state.ac_mode,
                ac_status=state.ac_status,
                soc=state.soc,
                min_soc=state.min_soc,
                soc_limit=state.soc_limit,
                output_w=state.output,
                pack_input_w=state.pack_in,
                output_pack_w=state.pack_out,
                reason=skip_reason
            )
            return

        if not cfg.state_reconciliation_writes_allowed():
            log_event(
                logging.INFO,
                "dry_run_startup_ac_mode_write",
                device=dev.name,
                ac_mode=2,
                dry_run=cfg.DRY_RUN,
                simulation=cfg.SIMULATION_MODE,
                allow_hardware_writes=cfg.ALLOW_HARDWARE_WRITES,
                allow_state_reconciliation_writes=(
                    cfg.ALLOW_STATE_RECONCILIATION_WRITES
                )
            )
            return

        try:
            response = dev.session.post(
                f"http://{dev.ip}/properties/write",
                json={
                    "sn": dev.sn,
                    "properties": {
                        "acMode": 2
                    }
                },
                timeout=2
            )

            if not zendure_write_succeeded(
                "startup_ac_mode_write_error",
                dev,
                response,
                ac_mode=2
            ):
                return

            log_event(
                logging.INFO,
                "startup_ac_mode_write",
                device=dev.name,
                ac_mode=2
            )

        except Exception as e:
            log_event(
                logging.WARNING,
                "startup_ac_mode_write_error",
                device=dev.name,
                error=e
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
                extra={
                    "effective_min_soc": effective_min_soc,
                    "current_soc": state.soc,
                    "winter_active": active
                }
            )

            self.publish_sensor(
                base + "estimated_ramp_days",
                cfg.estimate_winter_ramp_days(target),
                "d",
                None,
                icon="mdi:calendar-range"
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

            # Core
            self.publish_sensor(
                base + "soc",
                d.soc,
                "%",
                "battery"
            )

            self.publish_sensor(
                base + "min_soc",
                d.min_soc,
                "%",
                "battery"
            )

            self.publish_sensor(
                base + "max_soc",
                d.max_soc,
                "%",
                "battery"
            )

            self.publish_sensor(
                base + "solar",
                d.solar,
                "W",
                "power"
            )

            self.publish_sensor(
                base + "output",
                d.output,
                "W",
                "power"
            )

            self.publish_sensor(
                base + "target",
                effective_targets[i],
                "W",
                "power",
                extra={
                    "allocated_target_w": targets[i]
                }
            )

            self.publish_sensor(
                base + "output_limit",
                d.output_limit,
                "W",
                "power"
            )

            self.publish_sensor(
                base + "soc_limit",
                d.soc_limit,
                state_class=None
            )

            self.publish_sensor(
                base + "pack_state",
                d.pack_state,
                state_class=None
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
                "power"
            )

            self.publish_sensor(
                base + "battery_power_avg",
                round(avg_battery_power, 1),
                "W",
                "power"
            )

            self.publish_sensor(
                base + "voltage",
                d.voltage,
                "V",
                "voltage"
            )

            self.publish_sensor(
                base + "remaining_minutes",
                d.remain_minutes,
                "min",
                state_class=None,
                icon="mdi:timer-outline"
            )

            self.publish_sensor(
                base + "remaining_time",
                remaining_time,
                "h",
                "duration",
                icon="mdi:timer-outline"
            )

            # Thermal / Signal
            self.publish_sensor(
                base + "temp",
                d.temp,
                "°C",
                "temperature"
            )

            self.publish_sensor(
                base + "rssi",
                d.rssi,
                "dBm",
                "signal_strength"
            )

            # Panels
            self.publish_sensor(
                base + "panel1",
                d.solar1,
                "W",
                "power"
            )

            self.publish_sensor(
                base + "panel2",
                d.solar2,
                "W",
                "power"
            )

            self.publish_sensor(
                base + "panel3",
                d.solar3,
                "W",
                "power"
            )

            self.publish_sensor(
                base + "panel4",
                d.solar4,
                "W",
                "power"
            )

            # Status
            self.publish_sensor(
                base + "fault_level",
                d.fault_level,
                state_class=None
            )

            self.ha.set_state(
                f"binary_sensor.{dev.name.lower()}_fault",
                "on" if d.fault_level > 0 else "off",
                device_class="problem",
                extra_attributes={
                    "fault_level": d.fault_level
                }
            )

            self.ha.set_state(
                f"binary_sensor.{dev.name.lower()}_ac_active",
                "on" if d.ac_status else "off",
                device_class="power"
            )

            self.ha.set_state(
                f"binary_sensor.{dev.name.lower()}_dc_active",
                "on" if d.dc_status else "off",
                device_class="power"
            )

            self.ha.set_state(
                f"binary_sensor.{dev.name.lower()}_grid_online",
                "on" if d.grid_state else "off",
                device_class="connectivity"
            )

    def run_once(self):
        """Execute one EMS cycle."""

        start = time.time()

        if self.runtime_state:
            self.runtime_state.load_if_changed()

        self.sync_ha_runtime_state()

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

                self.run_startup_ac_mode_reconcile_once(dev, state)

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

        capabilities = [
            detect_capabilities(state)
            for state in states
        ]
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

        targets, current, new = calculate_targets(
            load,
            states,
            max_power,
            device_configs=self.devices,
            capabilities=capabilities,
            requested_total=stabilized_total
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

            cooldown = self.output_control_float(
                "write_cooldown_seconds",
                2,
                minimum=0
            )
            last_write = self.last_output_write_at.get(dev.name)
            bypass = self.output_control_bypass_active(load)

            if (
                cooldown > 0
                and last_write is not None
                and not bypass
            ):
                age = time.time() - last_write

                if age < cooldown:
                    log_event(
                        logging.INFO,
                        "output_control_settle_hold",
                        device=dev.name,
                        target_w=target,
                        last_write_age_s=round(age, 2),
                        cooldown_s=cooldown
                    )
                    continue

            self.set_output_limit(dev, target)
            self.last_output_write_at[dev.name] = time.time()

        # =====================
        # LOOP TIMING
        # =====================

        elapsed = time.time() - start

        if self.sleep_enabled:
            time.sleep(max(0, interval - elapsed))

# =====================
# SIMULATION / REPLAY
# =====================
