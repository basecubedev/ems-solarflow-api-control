# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a trial slot has to prove before it may make itself the default.

Only a booted target slot commits itself. Nothing commits a slot on its behalf,
and nothing commits a slot that is not currently running under tryboot: the
whole safety property is that the appliance which claims to work is the one
being asked.

Three outcomes, and no fourth:

    healthy      → commit; the previous slot becomes the rollback candidate
    unhealthy    → do not touch the selector, ask for an ordinary reboot; the
                   one-shot trial simply does not survive it
    unprovable   → manual_action_required; nothing guesses which slot is safe

The health gates are bounded on purpose. An appliance whose WAN is down is not a
broken slot, and a check that treated it as one would roll back a perfectly
healthy update, so nothing here requires internet reachability.
"""

import time
from dataclasses import dataclass, field

from appliance import ab_layout, ab_persistence
from appliance.ab_boot import SelectorError, SelectorTransaction
from appliance.ab_state import FallbackRecord, SlotRecord

RESULT_HEALTHY = "healthy"
RESULT_UNHEALTHY = "unhealthy"
RESULT_NOT_A_TRIAL = "not_a_trial"
RESULT_MANUAL_ACTION_REQUIRED = "manual_action_required"

TRIAL_DETECTED = "trial_boot"
TRIAL_ABSENT = "ordinary_boot"

DEFAULT_HEALTH_WINDOW_SECONDS = 300
MIN_HEALTH_WINDOW_SECONDS = 120


class AbHealthError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Gate:
    name: str
    passed: bool
    required: bool = True
    detail: str = ""

    def to_dict(self):
        return {
            "name": self.name,
            "passed": self.passed,
            "required": self.required,
            "detail": self.detail,
        }


@dataclass
class HealthReport:
    result: str
    slot: str = ""
    tryboot: bool = False
    operation_id: str = ""
    gates: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    committed: bool = False
    checked_at: float = 0.0

    @property
    def healthy(self):
        return self.result == RESULT_HEALTHY

    def to_dict(self):
        return {
            "result": self.result,
            "slot": self.slot,
            "tryboot": self.tryboot,
            "operation_id": self.operation_id,
            "gates": [gate.to_dict() for gate in self.gates],
            "reasons": list(self.reasons),
            "committed": self.committed,
            "checked_at": self.checked_at,
            "healthy": self.healthy,
        }


class TrialHealthService:
    """Detect a trial boot, judge it, and commit or step aside."""

    def __init__(
        self,
        *,
        probe,
        state,
        selector_path,
        runner=None,
        systemd=None,
        docker=None,
        install_check=None,
        agent_socket=None,
        time_fn=None,
        health_window_seconds=DEFAULT_HEALTH_WINDOW_SECONDS,
        remount_selector=True,
    ):
        self.probe = probe
        self.state = state
        self.selector_path = selector_path
        self.runner = runner
        self.systemd = systemd
        self.docker = docker
        self.install_check = install_check
        self.agent_socket = agent_socket
        self._time = time_fn or time.time
        self.health_window_seconds = max(
            int(health_window_seconds), MIN_HEALTH_WINDOW_SECONDS
        )
        self.remount_selector = remount_selector

    # --- trial detection --------------------------------------------------

    def detect(self, layout=None):
        """Is this a trial boot of a slot somebody planned, and which one?

        Every signal must agree: the firmware's tryboot flag, the partition it
        booted, the slot the layout says that is, and the pending operation on
        the shared partition. A tryboot with no matching pending operation is not
        something to commit — it is something an operator has to look at.
        """

        layout = layout or ab_layout.discover(self.probe)
        pending = self.state.pending()

        if layout.mode != ab_layout.MODE_AB:
            return TRIAL_ABSENT, layout, pending, ["this appliance has no A/B layout"]
        if not layout.tryboot:
            return TRIAL_ABSENT, layout, pending, []

        reasons = []
        if pending is None:
            reasons.append(
                "the firmware booted this slot under tryboot, but no A/B operation is pending"
            )
        else:
            if pending.committed:
                reasons.append("the pending trial is already committed")
            if pending.target_slot != layout.active_slot:
                reasons.append(
                    f"the pending trial targets slot {pending.target_slot}, "
                    f"slot {layout.active_slot} booted"
                )
            if pending.expected_boot_partition != layout.boot_partition:
                reasons.append(
                    f"the pending trial expects boot partition "
                    f"{pending.expected_boot_partition}, the firmware booted "
                    f"{layout.boot_partition}"
                )
            if (
                pending.expected_root_partuuid
                and layout.root_partuuid
                and pending.expected_root_partuuid != layout.root_partuuid
            ):
                reasons.append("the running root filesystem is not the one the trial targeted")
            build_id = str(layout.os_build.get("build_id") or "")
            if pending.target_build_id and build_id and pending.target_build_id != build_id:
                reasons.append(
                    f"this slot reports build {build_id}, the trial wrote "
                    f"{pending.target_build_id}"
                )
        return TRIAL_DETECTED, layout, pending, reasons

    # --- health gates ------------------------------------------------------

    def gates(self, layout):
        checks = []
        mounts = self.probe.mounts()

        # image-rota roots are immutable. A writable root here would mean the
        # slot booted something other than the image that was written, and every
        # write to it would be lost at the next slot switch anyway.
        root = mounts.get("/")
        checks.append(
            Gate(
                "rootfs_immutable",
                bool(root) and "ro" in (root or {}).get("options", frozenset()),
                detail="the root filesystem is mounted read-only",
            )
        )

        persistence = ab_persistence.verify(layout, mounts)
        checks.append(
            Gate(
                "persistent_partition",
                persistence.mounted,
                detail="the shared persistent partition is mounted",
            )
        )
        checks.append(
            Gate(
                "persistent_paths",
                persistence.ok,
                detail="; ".join(persistence.problems) or "every shared path is backed by /persist",
            )
        )
        checks.append(
            Gate(
                "persistent_schema",
                layout.manifest is not None
                and layout.manifest.persistent_schema_version
                <= ab_persistence.PERSISTENT_SCHEMA_VERSION,
                detail="the persistent schema is one this appliance implements",
            )
        )

        checks.append(self._ab_state_gate(persistence))
        checks.append(self._machine_identity_gate(layout))

        network_profiles = (
            self.probe.root / "etc/NetworkManager/system-connections"
        ).is_dir()
        checks.append(
            Gate(
                "network_configuration",
                network_profiles,
                detail="the NetworkManager profile directory is readable",
            )
        )

        checks.extend(self._service_gates())
        checks.extend(self._application_gates())
        return checks

    def _ab_state_gate(self, persistence):
        """The trial record has to live where both slots can read it.

        A pending trial written inside the source slot's root filesystem is
        invisible to the slot that booted, which cannot then prove what it is.
        """

        entry = next(
            (
                item
                for item in persistence.paths
                if item["name"] == "os_update_state"
            ),
            None,
        )
        return Gate(
            "ab_state_shared",
            bool(entry and entry["shared"]),
            detail=(entry or {}).get("problem")
            or "the A/B state directory is backed by the persistent partition",
        )

    def _machine_identity_gate(self, layout):
        """One physical appliance is one machine, whichever slot booted.

        ``image-rota`` owns the synchronisation; this only proves it happened,
        because a slot that generated its own identity would present itself to
        the network as a different host after every update.
        """

        source = (
            layout.manifest.machine_id_source
            if layout.manifest is not None
            else ab_persistence.MACHINE_ID_SOURCE
        )
        shared = _read_identity(self.probe.root / str(source).lstrip("/"))
        running = _read_identity(self.probe.root / "etc/machine-id")
        return Gate(
            "machine_identity",
            bool(shared) and shared == running,
            detail=(
                f"/etc/machine-id matches {source}"
                if shared and shared == running
                else f"the running machine identity does not come from {source}"
            ),
        )

    def _service_gates(self):
        checks = []
        for unit in ("ems-appliance-agent.service", "ems-appliance-web.service"):
            active = None
            if self.systemd is not None:
                try:
                    active = bool(self.systemd.is_active(unit))
                except Exception:
                    active = False
            checks.append(
                Gate(
                    f"unit_active:{unit}",
                    bool(active),
                    detail=f"{unit} is active",
                )
            )
        socket_usable = False
        if self.agent_socket is not None:
            try:
                socket_usable = bool(self.agent_socket())
            except Exception:
                socket_usable = False
        checks.append(
            Gate("agent_socket", socket_usable, detail="the agent socket answers")
        )

        verified = None
        if self.install_check is not None:
            try:
                verified = bool(self.install_check())
            except Exception:
                verified = False
        checks.append(
            Gate("verify_install", bool(verified), detail="verify-install passes in this slot")
        )
        return checks

    def _application_gates(self):
        """The EMS installation must still be there — not necessarily running.

        Committing an OS slot does not require EMS to be actively controlling
        power: an appliance that is fine but idle is still fine. What it does
        require is proof that the installation and its data survived the slot
        switch, because that is exactly what a broken persistence contract
        destroys.
        """

        checks = []
        install_root = self.probe.root / "opt/ems-solarflow"
        checks.append(
            Gate(
                "ems_installation_present",
                install_root.is_dir(),
                detail="the EMS installation root is present",
            )
        )
        checks.append(
            Gate(
                "ems_data_present",
                (install_root / "config").is_dir() and (install_root / "data").is_dir(),
                detail="the EMS configuration and data directories survived the slot switch",
            )
        )

        docker_ok = None
        admin_ok = None
        if self.docker is not None:
            try:
                docker_ok = bool(self.docker.available())
            except Exception:
                docker_ok = False
            try:
                admin_ok = bool(self.docker.inspect_admin())
            except Exception:
                admin_ok = False
        checks.append(
            Gate(
                "docker_usable",
                bool(docker_ok),
                required=self.docker is not None,
                detail="the Docker daemon answers",
            )
        )
        # A slot whose Admin runtime cannot be reconstructed is not a slot an
        # operator can recover the appliance from, so this is required. The
        # reconstruction itself runs before health and may use a seeded image,
        # which is what keeps an offline appliance from rolling back.
        checks.append(
            Gate(
                "admin_runtime",
                bool(admin_ok),
                required=self.docker is not None,
                detail="the Admin container is available and answers",
            )
        )
        return checks

    # --- the verdict -------------------------------------------------------

    def evaluate(self):
        detection, layout, pending, reasons = self.detect()
        now = self._time()

        if detection == TRIAL_ABSENT:
            return HealthReport(
                result=RESULT_NOT_A_TRIAL,
                slot=layout.active_slot,
                tryboot=False,
                reasons=reasons,
                checked_at=now,
            )
        if reasons:
            return HealthReport(
                result=RESULT_MANUAL_ACTION_REQUIRED,
                slot=layout.active_slot,
                tryboot=True,
                operation_id=pending.operation_id if pending else "",
                reasons=reasons,
                checked_at=now,
            )

        gates = self.gates(layout)
        failed = [gate.name for gate in gates if gate.required and not gate.passed]
        if failed:
            return HealthReport(
                result=RESULT_UNHEALTHY,
                slot=layout.active_slot,
                tryboot=True,
                operation_id=pending.operation_id,
                gates=gates,
                reasons=[f"health gate failed: {name}" for name in failed],
                checked_at=now,
            )
        if pending.trial_requested_at and (
            now - pending.trial_requested_at > self.health_window_seconds
        ):
            return HealthReport(
                result=RESULT_UNHEALTHY,
                slot=layout.active_slot,
                tryboot=True,
                operation_id=pending.operation_id,
                gates=gates,
                reasons=[
                    "the trial slot did not reach its health verification inside the "
                    f"{self.health_window_seconds}s window"
                ],
                checked_at=now,
            )
        return HealthReport(
            result=RESULT_HEALTHY,
            slot=layout.active_slot,
            tryboot=True,
            operation_id=pending.operation_id,
            gates=gates,
            checked_at=now,
        )

    # --- commit ------------------------------------------------------------

    def commit(self, report=None):
        """Make this trial slot the default. Only this slot may do it.

        The selector is only touched after every element of the commit authority
        holds: currently in tryboot, the running slot is the target, the pending
        operation matches, the build marker matches, and health succeeded.
        """

        report = report or self.evaluate()
        if not report.healthy:
            raise AbHealthError(
                "commit_not_authorised",
                f"a slot may only commit itself after a healthy trial ({report.result})",
            )

        layout = ab_layout.discover(self.probe)
        pending = self.state.pending()
        if pending is None or pending.operation_id != report.operation_id:
            raise AbHealthError(
                "pending_trial_mismatch", "the pending trial changed while health was verified"
            )
        if not layout.tryboot or layout.active_slot != pending.target_slot:
            raise AbHealthError(
                "commit_not_authorised", "this slot is not the trial slot the operation targeted"
            )

        manifest = layout.manifest
        target_partition = layout.slot_devices(pending.target_slot).boot.number
        previous_partition = layout.slot_devices(pending.source_slot).boot.number

        transaction = SelectorTransaction(
            self.selector_path,
            runner=self.runner,
            mountpoint=manifest.selector_mountpoint,
            remount=self.remount_selector,
        )
        try:
            selector = transaction.commit(
                target_partition=target_partition, previous_partition=previous_partition
            )
        except SelectorError as exc:
            raise AbHealthError(exc.code, exc.message)

        record = SlotRecord(
            slot=pending.target_slot,
            release_version=pending.release_version or pending.target_release,
            build_id=pending.target_build_id,
            artifact_digest=pending.artifact_digest,
            boot_digest=pending.boot_digest,
            rootfs_digest=pending.rootfs_digest,
            committed_at=self._time(),
            health=report.to_dict(),
        )
        self.state.record_known_good(record, previous_slot=pending.source_slot)
        self.state.mark_committed(pending.operation_id)
        report.committed = True
        return {
            "committed": True,
            "slot": pending.target_slot,
            "previous_slot": pending.source_slot,
            "selector": selector.to_dict(),
            "operation_id": pending.operation_id,
            "health": report.to_dict(),
        }

    # --- failure and fallback ----------------------------------------------

    def abandon(self, report):
        """An unhealthy trial: record it, change nothing, ask for a normal boot.

        The selector is not touched. Because the tryboot flag is one-shot, an
        ordinary reboot returns to the previous default with nothing changed.
        """

        pending = self.state.pending()
        if pending is not None:
            self.state.record_fallback(
                FallbackRecord(
                    operation_id=pending.operation_id,
                    source_slot=pending.source_slot,
                    target_slot=pending.target_slot,
                    target_release=pending.target_release,
                    target_build_id=pending.target_build_id,
                    observed_at=self._time(),
                    attempt=pending.attempt,
                    known_good_slot=self.state.slots().known_good_slot,
                    last_health=report.to_dict(),
                )
            )
        if self.systemd is None:
            return {"rebooted": False, "reason": "no systemd interface is available"}
        try:
            result = self.systemd.reboot()
        except Exception as exc:
            raise AbHealthError(
                "manual_action_required",
                f"the trial slot is unhealthy and could not request a normal reboot: {exc}",
            )
        if not getattr(result, "ok", False):
            raise AbHealthError(
                "manual_action_required",
                "the trial slot is unhealthy and the normal reboot request was refused",
            )
        return {"rebooted": True, "slot": report.slot}


def _read_identity(path):
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""


def classify_fallback(probe, state, *, time_fn=None):
    """Did this ordinary boot happen because a trial did not commit?

    Runs on every boot of the source slot. It never retries: a fallback is a
    result an operator acts on, and an automatic retry loop would reboot an
    appliance into the same broken slot indefinitely.
    """

    now = (time_fn or time.time)()
    layout = ab_layout.discover(probe)
    pending = state.pending()
    if pending is None or pending.committed:
        return None
    if layout.tryboot:
        return None
    if layout.active_slot != pending.source_slot:
        return None

    history = state.slots()
    record = FallbackRecord(
        operation_id=pending.operation_id,
        source_slot=pending.source_slot,
        target_slot=pending.target_slot,
        target_release=pending.target_release,
        target_build_id=pending.target_build_id,
        observed_at=now,
        attempt=pending.attempt,
        known_good_slot=history.known_good_slot or layout.active_slot,
        last_health=(state.last_fallback().last_health if state.last_fallback() else {}),
    )
    state.record_fallback(record)
    # The pending trial is consumed: it did not commit and it will not be
    # retried. A new plan and a new confirmation are required, after the
    # inactive slot has been staged again.
    state.clear_pending()
    return record
