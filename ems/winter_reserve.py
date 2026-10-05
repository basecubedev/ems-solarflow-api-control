# SPDX-License-Identifier: AGPL-3.0-or-later
"""Winter reserve: the daily minSoc plan and the PV-first export hold, per device.

The controller owns the control loop and the writes; this module owns what
winter mode decides. Each device carries one ``WinterDeviceState``, so the
step, the target and the hold travel together. Which plan a device follows
comes from ``ems.winter_policies``.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from ems import config as cfg
from ems import winter_policies
from ems.logging_utils import log_event
from ems.models import BATTERY_ABSENT, DeviceCapabilities
from ems.state_store import WinterReserveStore
from ems.target_control import battery_presence, pv_power
from ems.winter_policies import MAX_RAISE_ABOVE_SOC

SOLAR_CHARGE_REASON = "winter_solar_charge"
HOLD_ENTRY_GAP = 1
HOLD_WINDOW = timedelta(hours=3)
DARK_PV_W = 10
MORNING_STEP_PV_W = 50
STORED_TARGET_MAX_AGE = timedelta(days=7)


def build_winter_store():
    """The persistent winter store of a live EMS, or ``None`` when unavailable.

    Built by the live entry script and handed to the controller, rather than
    by the controller itself like the full-charge store: a controller built
    in a test or a simulation must not read or write the steps of the real
    installation's database.
    """

    if not cfg.BASE_DIR:
        return None

    try:
        return WinterReserveStore(cfg.battery_full_charge_state_database_path())
    except Exception as e:
        log_event(logging.WARNING, "winter_reserve_store_unavailable", error=e)
        return None


@dataclass
class WinterDeviceState:
    """Everything winter mode remembers about one device."""

    target: int | None = None
    seen: bool = False
    step_date: str | None = None
    step_at: datetime | None = None
    step_target: int | None = None
    skipped_date: str | None = None
    pv_wait_logged: str | None = None
    raise_waiting: int | None = None
    holding: bool = False
    pv_date: str | None = None
    pv_start_at: datetime | None = None
    pv_ever: bool = False
    pv_veto_logged: str | None = None
    device_class: str | None = None
    policy: winter_policies.WinterPolicy | None = None
    policy_warned: bool = False
    dark_date: str | None = None
    store_failed: bool = False

    def reset_plan(self):
        """Forget the target and the day's step, keeping what is known of the device."""

        self.target = self.step_date = self.step_target = self.step_at = None


class WinterReserve:
    """Winter decisions for the controller's devices.

    With a ``store`` the step and target survive a restart. Without one --
    simulation, tests, an unavailable database -- they live in memory. Either
    way, a device first seen without a record of today may already have
    stepped today, so it takes no step that day when first seen in daylight
    or after the step hour.
    """

    def __init__(self, runtime_state=lambda: None, store=None):
        self._runtime_state = runtime_state
        self.store = store
        self.devices = {}

    def device(self, name):
        """The winter state of the device named ``name``."""

        return self.devices.setdefault(name, WinterDeviceState())

    def last_step_date(self):
        """The latest day any device took a step, or ``None``."""

        return max(
            (item.step_date for item in self.devices.values() if item.step_date),
            default=None,
        )

    def policy(self, dev, state):
        """Return the winter policy that applies to ``dev`` this cycle."""

        if battery_presence(state) == BATTERY_ABSENT:
            return winter_policies.POLICIES[winter_policies.NO_POLICY]

        item = self.device(dev.name)
        if item.policy is not None:
            return item.policy

        entry = cfg.device_config_entry(dev.name) or {}
        device_class = winter_policies.device_energy_class(False, entry.get("pv_kwp"))
        policy, valid = winter_policies.resolve_policy(
            device_class,
            entry.get("winter_policy"),
            cfg.winter_policy_class_defaults(),
        )
        if not valid and not item.policy_warned:
            item.policy_warned = True
            log_event(
                logging.WARNING,
                "winter_policy_invalid",
                device=dev.name,
                device_class=device_class,
                configured_policy=entry.get("winter_policy"),
                policy=policy.name
            )
        item.device_class = device_class
        item.policy = policy
        return policy

    def reconciliation_target(self, dev, state, winter_active, now):
        """Return the winter/summer minSoc target and whether today's step was taken.

        Every winter and summer-reset target ends here. A raise over the
        device's reported minSoc is written only once it leads the SoC by at
        most ``MAX_RAISE_ABOVE_SOC`` (``cfg.winter_min_soc_raise_limit``); only
        a policy's own daily step may lead it further, and only when the
        policy says so.
        """

        policy = self.policy(dev, state)
        target, stepped = self.planned_min_soc(dev, state, winter_active, now, policy)
        item = self.device(dev.name)
        if target is None:
            return None, stepped

        soc = getattr(state, "soc", None)
        own_step = item.step_date == now.date().isoformat() and target == item.step_target
        lead = policy.step_lead if own_step else MAX_RAISE_ABOVE_SOC
        held = cfg.winter_min_soc_raise_limit(target, state.min_soc, soc, lead)
        if held != target:
            starts = item.raise_waiting != target
            item.raise_waiting = target
            log_event(
                logging.INFO if stepped or starts else logging.DEBUG,
                "winter_raise_waits_for_battery",
                device=dev.name,
                current_soc=soc,
                current_min_soc=state.min_soc,
                target_min_soc=target
            )
        else:
            item.raise_waiting = None
        return held, stepped

    def planned_min_soc(self, dev, state, winter_active, now, policy):
        """The winter/summer minSoc target before the raise limit is applied.

        In winter, minSoc rises once a day by the policy's step and, later that
        day, follows the SoC the battery reaches.
        """

        if not cfg.winter_feature_enabled(self._runtime_state()):
            item = self.device(dev.name)
            item.seen = False
            item.raise_waiting = None
            return None, False

        if not getattr(dev, "supports_state_reconciliation", True):
            return None, False

        # Left in place deliberately: a single transient `packNum: 0` would
        # otherwise drop the remembered target, and the next cycle would write
        # the configured minimum over it.
        if battery_presence(state) == BATTERY_ABSENT:
            return None, False

        if policy.step is None:
            return None, False

        item = self.device(dev.name)

        if not winter_active:
            return self.summer_min_soc(dev, state, item, now), False

        today = now.date().isoformat()
        past_step_hour = now.hour >= cfg.winter_adjust_hour()
        if not item.seen:
            item.seen = True
            self.restore(dev, state, policy, item, today, past_step_hour)
        elif item.store_failed:
            self.reload(dev, item, today)
        if item.target is None:
            item.target = self.held_min_soc(dev, state)
        self.observe_pv(dev, state, item, today, now)

        stepped_today = today in (item.step_date, item.skipped_date)

        if policy.step == winter_policies.STEP_MORNING_PV:
            if item.pv_date != today and past_step_hour and not stepped_today:
                self.log_step_waits_for_pv(dev, policy, item, today)
            due = not stepped_today and pv_power(state) >= MORNING_STEP_PV_W
        else:
            due = (
                not stepped_today
                and now.hour == cfg.winter_adjust_hour()
                and not self.reports_pv_unexpectedly(dev, item, today)
            )

        if due:
            return self.take_step(dev, state, policy, item, today, now)

        follows = (
            policy.step != winter_policies.STEP_MORNING_PV
            or pv_power(state) >= DARK_PV_W
        )
        if item.step_date == today and follows:
            self.follow_soc(dev, state, policy, item, now)

        return item.target, False

    def observe_pv(self, dev, state, item, today, now):
        """Note the day a device reported PV, and that it ever reported real PV.

        The day's first PV opens a hold window only after a dark reading the
        same day: a restart in daylight must not open a new one.
        """

        if pv_power(state) < DARK_PV_W:
            item.dark_date = today
            return
        if item.pv_date != today:
            item.pv_date = today
            item.pv_start_at = now if item.dark_date == today else None
        if not item.pv_ever and pv_power(state) >= MORNING_STEP_PV_W:
            item.pv_ever = True
            self.persist(dev, item, now)

    def reports_pv_unexpectedly(self, dev, item, today):
        """Whether a battery-only device has reported PV; then it takes no noon step.

        ``pv_kwp: 0`` long meant "not set". A PV device saved that way would
        otherwise get a step that may lead its SoC far enough for a grid
        charge. Once a device has reported PV it is remembered.
        """

        if item.device_class != winter_policies.BATTERY_ONLY or not item.pv_ever:
            return False
        if item.pv_veto_logged != today:
            item.pv_veto_logged = today
            log_event(
                logging.WARNING,
                "winter_pv_kwp_zero_but_pv_reported",
                device=dev.name,
                hint="pv_kwp 0 marks a battery without PV; this device reported PV, so it takes no noon step"
            )
        return True

    def restore(self, dev, state, policy, item, today, past_step_hour):
        """Bring back what the store kept; without a record, assume today has a step.

        A current record says whether today's step was taken. Without one --
        no store, the day an upgrade lands, a save that failed -- a device
        first seen in daylight or after the step hour may already have
        stepped, so it takes none that day.
        """

        if item.step_date == today:
            return

        if self.store is not None:
            try:
                record = self.store.load(dev.name)
                item.store_failed = False
            except Exception as e:
                record = None
                item.store_failed = True
                log_event(logging.WARNING, "winter_reserve_load_failed", device=dev.name, error=e)
            if item.store_failed or self.apply_record(dev, item, record, today):
                return
            item.reset_plan()
        elif item.target is not None and not self.record_is_current(
            {"step_date": item.step_date, "target": item.target}, today
        ):
            item.reset_plan()

        if item.step_date != today and self.start_skips_today(state, policy, past_step_hour):
            item.skipped_date = today

    def apply_record(self, dev, item, record, today):
        """Take over a stored record; return whether it was current."""

        item.pv_ever = item.pv_ever or bool(record and record.get("pv_ever"))
        if not self.record_is_current(record, today):
            return False
        item.step_date = record["step_date"]
        item.target = self.clamped_target(dev, int(record["target"]))
        if item.step_date == today:
            item.step_target = record.get("step_target")
            item.step_at = record.get("step_at")
        return True

    def record_is_current(self, record, today):
        """Whether a stored record belongs to this winter, not a past one."""

        if not record or record.get("target") is None or not record.get("step_date"):
            return False
        try:
            stored = datetime.fromisoformat(record["step_date"]).date()
            current = datetime.fromisoformat(today).date()
        except ValueError:
            return False
        return timedelta(0) <= current - stored <= STORED_TARGET_MAX_AGE

    def clamped_target(self, dev, target):
        """``target`` within the configured floor and ``winter_min_soc``."""

        floor = dev.min_soc if dev.min_soc > 0 else cfg.winter_summer_min_soc()
        ceiling = max(floor, cfg.winter_min_soc_percent())
        return max(floor, min(ceiling, target))

    def persist(self, dev, item, now):
        """Store the step and target -- only what the device can receive.

        In a dry run, or without state reconciliation writes, the plan is never
        written; storing it would hand the first live start a target the
        device never had. Returns False only when a store that should have
        kept it failed, so a step it could not keep is not taken: a restart
        would otherwise take it again.
        """

        if self.store is None:
            return True
        if not cfg.state_reconciliation_writes_allowed(getattr(dev, "control_gate", "api")):
            return True
        if item.store_failed:
            return False
        try:
            self.store.save(
                dev.name,
                now,
                step_date=item.step_date,
                step_at=item.step_at,
                step_target=item.step_target,
                target=item.target,
                pv_ever=item.pv_ever,
            )
        except Exception as e:
            log_event(logging.WARNING, "winter_reserve_save_failed", device=dev.name, error=e)
            return False
        return True

    def reload(self, dev, item, today):
        """Read the record again after a failed load, before anything is planned.

        Until it reads, nothing is saved: saving over a record that could not
        be read would replace what it held, a step taken today among it.
        """

        try:
            record = self.store.load(dev.name)
        except Exception as e:
            log_event(logging.DEBUG, "winter_reserve_load_failed", device=dev.name, error=e)
            return
        item.store_failed = False
        self.apply_record(dev, item, record, today)

    def summer_min_soc(self, dev, state, item, now):
        """Outside winter: back to ``summer_min_soc``, forgetting the winter target."""

        summer_min_soc = cfg.winter_summer_min_soc()
        had_target = item.target is not None
        if had_target or item.step_date is not None:
            item.reset_plan()
            item.skipped_date = None
            self.persist(dev, item, now)
        item.seen = False

        if had_target or int(state.min_soc) != int(summer_min_soc):
            waits = cfg.winter_min_soc_raise_limit(
                summer_min_soc,
                state.min_soc,
                getattr(state, "soc", None),
                MAX_RAISE_ABOVE_SOC
            ) != summer_min_soc
            log_event(
                logging.DEBUG if waits and not had_target else logging.INFO,
                "winter_summer_reset",
                device=dev.name,
                current_min_soc=state.min_soc,
                target_min_soc=summer_min_soc
            )

        return summer_min_soc

    def take_step(self, dev, state, policy, item, today, now):
        """Raise the target by today's step, or wait while the battery is too far below."""

        winter_min_soc = cfg.winter_min_soc_percent()
        base = state.min_soc if state.min_soc > 0 else item.target
        step = cfg.winter_step_percent()

        if policy.step == winter_policies.STEP_MORNING_PV:
            target = cfg.winter_morning_step_target(state.soc, base, winter_min_soc, step)
        else:
            target = cfg.winter_timed_step_target(state.soc, base, winter_min_soc, step)

        if target is None:
            return item.target, False

        previous = (item.step_date, item.step_at, item.step_target, item.target)
        item.step_date = today
        item.step_at = now
        item.step_target = target
        item.target = target
        if not self.persist(dev, item, now):
            item.step_date, item.step_at, item.step_target, item.target = previous
            item.skipped_date = today
            return item.target, False
        log_event(
            logging.INFO,
            "winter_step",
            device=dev.name,
            policy=policy.name,
            current_soc=state.soc,
            current_min_soc=state.min_soc,
            target_min_soc=target,
            winter_min_soc=winter_min_soc,
            estimated_days_remaining=cfg.estimate_winter_ramp_days(target)
        )
        return target, True

    def follow_soc(self, dev, state, policy, item, now):
        """After today's step, raise the target to the SoC the battery reached."""

        target = cfg.winter_daytime_follow_target(
            state.soc, item.target, cfg.winter_min_soc_percent()
        )
        if target is None:
            return

        previous = item.target
        item.target = target
        if not self.persist(dev, item, now):
            item.target = previous
            return
        log_event(
            logging.INFO,
            "winter_follow_soc",
            device=dev.name,
            policy=policy.name,
            current_soc=state.soc,
            previous_min_soc=previous,
            target_min_soc=target
        )

    def start_skips_today(self, state, policy, past_step_hour):
        """Whether a device first seen now may already have taken today's step.

        A start after the step hour, or for the morning step one in daylight,
        may follow a step this day already took; taking another would add a
        second raise the same day.
        """

        if past_step_hour:
            return True

        return (
            policy.step == winter_policies.STEP_MORNING_PV
            and pv_power(state) >= DARK_PV_W
        )

    def log_step_waits_for_pv(self, dev, policy, item, today):
        """Say once a day that a PV policy has seen no PV by the step hour.

        A battery without PV of its own that is not configured with
        ``pv_kwp: 0`` would otherwise wait for a morning that never comes.
        """

        if item.pv_wait_logged == today:
            return
        item.pv_wait_logged = today
        log_event(
            logging.INFO,
            "winter_step_waits_for_pv",
            device=dev.name,
            policy=policy.name,
            step_hour=cfg.winter_adjust_hour(),
            hint="a battery without PV of its own needs pv_kwp 0"
        )

    def held_min_soc(self, dev, state):
        """Winter minSoc to keep when no target is known.

        The device still carries its target after a restart without a record,
        so it is adopted within the configured floor and the winter ceiling
        instead of being written back down to the summer value.
        """

        return self.clamped_target(dev, int(state.min_soc))

    def ha_target(self, dev, state, active):
        """The minSoc Home Assistant shows as this device's winter target."""

        policy = self.policy(dev, state)
        own_min_soc = state.min_soc if state.min_soc > 0 else dev.min_soc
        if battery_presence(state) == BATTERY_ABSENT or policy.step is None:
            return own_min_soc, policy
        if not active:
            return cfg.winter_summer_min_soc(), policy
        target = self.device(dev.name).target
        return (self.held_min_soc(dev, state) if target is None else target), policy

    def solar_charge_candidate(self, dev, state):
        """Whether ``dev`` is a battery below its minSoc that receives PV.

        The hold starts at two points below minSoc and lasts until the battery
        holds it, so a one-point dip after minSoc followed the SoC does not
        switch the output on and off. A SoC of 0 is no reading: a report
        without ``electricLevel`` parses as 0.
        """

        if not state or battery_presence(state) == BATTERY_ABSENT:
            return False

        reading = cfg.winter_soc_reading(getattr(state, "soc", None))
        if state.min_soc <= 0 or reading is None or reading <= 0:
            return False

        entry_gap = 0 if self.device(dev.name).holding else HOLD_ENTRY_GAP

        return state.min_soc - reading > entry_gap and pv_power(state) >= DARK_PV_W

    def solar_charge_hold(self, dev, state, now):
        """Whether the device may be kept from exporting now.

        Only for a policy that holds export, only where winter mode may
        manage minSoc, and only for ``HOLD_WINDOW`` after the day's first PV
        or after today's step: the hold refills a battery the night left
        below its minSoc and the gap the step opened, and a pack that cannot
        take the charge -- cold, a BMS limit -- curtails its PV for no longer
        than that.
        """

        if not getattr(dev, "supports_state_reconciliation", True):
            return False
        if not cfg.state_reconciliation_writes_allowed(getattr(dev, "control_gate", "api")):
            return False
        if not self.policy(dev, state).holds_export:
            return False

        item = self.device(dev.name)
        today = now.date().isoformat()
        anchors = (
            item.pv_start_at if item.pv_date == today else None,
            item.step_at if item.step_date == today else None,
        )
        return any(
            anchor is not None and timedelta(0) <= now - anchor <= HOLD_WINDOW
            for anchor in anchors
        )

    def filtered_capabilities(self, devices, states, capabilities, now, online=None):
        """Return capabilities with devices in a winter solar charge kept from export.

        A held device exports at most ``min_output_limit``; the rest of its PV
        charges its battery. A device another rule already keeps from
        exporting keeps that reason, and an offline device's cached state
        decides nothing.
        """

        online = online or {}
        candidates = [
            bool(capability.can_export)
            and online.get(dev.name, True)
            and self.solar_charge_candidate(dev, state)
            for dev, state, capability in zip(devices, states, capabilities)
        ]
        if not any(candidates) or not cfg.winter_mode_active(now, self._runtime_state()):
            for item in self.devices.values():
                item.holding = False
            return capabilities

        filtered = []

        for dev, state, capability, candidate in zip(devices, states, capabilities, candidates):
            item = self.device(dev.name)
            item.holding = bool(candidate and self.solar_charge_hold(dev, state, now))
            if not item.holding:
                filtered.append(capability)
                continue

            filtered.append(DeviceCapabilities(
                can_charge=capability.can_charge,
                can_discharge=False,
                can_export=False,
                can_ac_charge=capability.can_ac_charge,
                reason=SOLAR_CHARGE_REASON,
                export_held=True
            ))

        return filtered
