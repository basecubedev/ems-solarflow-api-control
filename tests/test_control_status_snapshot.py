# SPDX-License-Identifier: AGPL-3.0-or-later
"""The live control snapshot: written by the running EMS, read by diagnose.

``diagnose --control`` and ``--control-quality`` looked in runtime-state.json
for cycle data the EMS never writes there (E7 in
docs/developer/review-coverage.md), so both reported nothing. The EMS now
publishes a non-authoritative projection of each cycle beside runtime-state,
and diagnose reads that, saying plainly when there is none or it is old.
"""

import ast
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from _emsctl_test_helpers import LiveEms, write_live_control_status
from ems import diagnostics
from ems.control_status import (
    CONTROL_STATUS_SCHEMA_VERSION,
    GRID_SAMPLE_WINDOW,
    ControlStatusWriter,
)
from ems.controller import EMSController
from ems.health import CommHealth
from ems.paths import resolve_control_status_path
from ems.runtime_state import RuntimeState, build_runtime_defaults
from tests.test_write_gates import device, state

pytestmark = [
    pytest.mark.contract,
    pytest.mark.power_control,
    pytest.mark.simulation,
]

ENTRYPOINT = Path(__file__).resolve().parents[1] / "ems-solarflow-api-control.py"
CONFIG = {
    "system": {"loop_interval": 5, "max_total_power": 800},
    "devices": [{"name": "WR1", "max_power": 800, "min_soc": 15}],
}
SNAPSHOT_KEYS = {
    "schema_version",
    "written_at",
    "loop_interval_s",
    "cycle",
    "control",
    "grid_meter",
    "devices",
    "grid_samples",
}
DEVICE_KEYS = {
    "online",
    "last_seen_at",
    "max_power_w",
    "soc",
    "min_soc",
    "pv_input_w",
    "output_w",
    "output_limit_w",
    "battery_charge_w",
    "battery_discharge_w",
    "allocated_target_w",
    "effective_target_w",
    "limiting_reason",
}


class MeteredGrid:
    """A grid meter that answers every read and keeps a real health record."""

    def __init__(self, power):
        self.power = power
        self.health = CommHealth("grid_meter")

    def get_power(self):
        self.health.record_success(latency_ms=4)
        return self.power


def run_cycle(tmp_path, load=240):
    """One real control cycle against operator state kept in ``tmp_path``."""

    runtime_path = tmp_path / "runtime-state.json"
    dev = device("WR1")
    with patch("ems.runtime_state.cfg.CONFIG", {"ha": {}, "devices": [{"name": "WR1"}]}):
        runtime_state = RuntimeState(str(runtime_path), build_runtime_defaults([dev]))
        runtime_state.load_or_create()
    controller = EMSController(
        devices=[dev],
        shelly=MeteredGrid(load),
        sleep_enabled=False,
        runtime_state=runtime_state,
    )
    controller.set_output_limit = Mock()
    with patch(
        "ems.controller.fetch_all_devices",
        return_value=[state(output=100, output_limit=100, pack_in=100)],
    ), patch("ems.controller.cfg.SYSTEM_ENABLED", True), patch(
        "ems.controller.cfg.MAX_TOTAL_POWER", 800
    ), patch("ems.controller.cfg.SOC_RECONCILE_INTERVAL", 0), patch(
        "ems.controller.cfg.MIN_OUTPUT_LIMIT", 0
    ):
        controller.run_once()
    return controller, runtime_path


def publish(controller, runtime_path):
    writer = ControlStatusWriter(resolve_control_status_path(runtime_path))
    assert writer.write(controller)
    return json.loads(writer.path.read_text())


def test_a_control_cycle_produces_the_snapshot_with_the_cycle_it_ran(tmp_path):
    controller, runtime_path = run_cycle(tmp_path, load=240)
    explanation = controller.last_control_explanation

    snapshot = publish(controller, runtime_path)

    assert set(snapshot) == SNAPSHOT_KEYS
    assert snapshot["schema_version"] == CONTROL_STATUS_SCHEMA_VERSION
    assert snapshot["loop_interval_s"] == 5
    assert diagnostics.diagnose_parse_timestamp(snapshot["written_at"]) is not None
    assert snapshot["cycle"]["failed_cycles"] == 0
    assert snapshot["cycle"]["mode"] == explanation.mode
    assert snapshot["control"] == {
        "grid_power_w": 240.0,
        "filtered_load_w": explanation.filtered_load_w,
        "commanded_total_w": explanation.commanded_total_w,
        "allocated_target_total_w": explanation.allocated_target_total_w,
        "effective_target_total_w": explanation.effective_target_total_w,
    }
    assert snapshot["grid_meter"]["consecutive_read_failures"] == 0
    measured = diagnostics.diagnose_parse_timestamp(snapshot["grid_meter"]["measured_at"])
    assert abs(time.time() - measured.timestamp()) < 5

    wr1 = snapshot["devices"]["WR1"]
    assert set(wr1) == DEVICE_KEYS
    assert wr1["online"] is True
    assert wr1["last_seen_at"] == snapshot["cycle"]["fetched_at"]
    assert (wr1["soc"], wr1["min_soc"]) == (80.0, 15.0)
    assert (wr1["output_w"], wr1["output_limit_w"]) == (100.0, 100.0)
    assert wr1["battery_discharge_w"] == 100.0
    assert wr1["allocated_target_w"] == explanation.devices["WR1"].allocated_target_w
    assert snapshot["grid_samples"] == [{
        "cycle_at": snapshot["cycle"]["fetched_at"],
        "grid_power_w": 240.0,
        "measured_at": snapshot["grid_meter"]["measured_at"],
    }]


def test_the_snapshot_is_beside_runtime_state_and_never_inside_it(tmp_path):
    controller, runtime_path = run_cycle(tmp_path)
    operator_state = runtime_path.read_bytes()

    publish(controller, runtime_path)

    assert resolve_control_status_path(runtime_path) == tmp_path / "control-status.json"
    assert runtime_path.read_bytes() == operator_state


def test_the_live_loop_publishes_the_snapshot_after_every_cycle():
    tree = ast.parse(ENTRYPOINT.read_text())
    main = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    loop = next(node for node in ast.walk(main) if isinstance(node, ast.While))
    calls = [
        (node.func.value.id, node.func.attr)
        for statement in loop.body
        for node in ast.walk(statement)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
    ]

    assert ("control_status", "write") in calls
    assert calls.index(("control_status", "write")) > calls.index(("ems", "run_guarded"))


def test_a_failed_cycle_is_published_as_failing_without_values(tmp_path, monkeypatch):
    controller, runtime_path = run_cycle(tmp_path)

    def broken():
        controller.last_control_explanation = None
        raise ValueError("broken cycle")

    monkeypatch.setattr(controller, "run_once", broken)
    controller.run_guarded()

    snapshot = publish(controller, runtime_path)

    assert snapshot["cycle"]["failed_cycles"] == 1
    assert snapshot["control"]["grid_power_w"] is None
    report = diagnostics.diagnose_control_report(CONFIG, str(runtime_path))
    assert "control_cycles_failing" in {cause["code"] for cause in report["root_causes"]}


def test_the_sample_window_keeps_only_the_most_recent_cycles(tmp_path):
    ems = LiveEms(tmp_path)
    start = time.time()
    for index in range(GRID_SAMPLE_WINDOW + 5):
        snapshot = ems.cycle(index, start + index)

    assert len(snapshot["grid_samples"]) == GRID_SAMPLE_WINDOW
    assert snapshot["grid_samples"][-1]["grid_power_w"] == GRID_SAMPLE_WINDOW + 4


def test_an_unwritable_snapshot_never_reaches_the_control_loop(tmp_path, caplog):
    controller, _ = run_cycle(tmp_path)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    writer = ControlStatusWriter(blocker / "control-status.json")

    with caplog.at_level(logging.INFO):
        assert writer.write(controller) is False
        assert writer.write(controller) is False
        blocker.unlink()
        assert writer.write(controller) is True

    failed = [r for r in caplog.records if "control_status_write_failed" in r.getMessage()]
    recovered = [r for r in caplog.records if "control_status_write_recovered" in r.getMessage()]
    assert [r.levelno for r in failed] == [logging.WARNING]
    assert len(recovered) == 1


def test_a_snapshot_that_cannot_be_built_leaves_the_last_one_intact(tmp_path):
    controller, runtime_path = run_cycle(tmp_path)
    previous = publish(controller, runtime_path)
    writer = ControlStatusWriter(resolve_control_status_path(runtime_path))

    assert writer.write(SimpleNamespace()) is False
    controller.devices = [SimpleNamespace(name=("not", "a", "name"), max_power=800)]
    assert writer.write(controller) is False

    assert json.loads(writer.path.read_text()) == previous
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "control-status.json",
        "runtime-state.json",
    ]


def test_diagnose_control_reports_the_cycle_the_ems_just_ran(tmp_path):
    controller, runtime_path = run_cycle(tmp_path, load=240)
    publish(controller, runtime_path)

    report = diagnostics.diagnose_control_report(CONFIG, str(runtime_path))

    explanation = controller.last_control_explanation
    assert report["snapshot"]["grid_power_w"] == 240.0
    assert report["snapshot"]["target_output_w"] == explanation.effective_target_total_w
    assert report["snapshot"]["final_output_w"] == 100.0
    assert report["runtime_state"]["checked"] is True
    assert report["runtime_state"]["stale"] is False
    assert report["runtime_state"]["snapshot_status"] == "fresh"
    assert report["soc_analysis"]["devices"] == [
        {"device": "WR1", "soc": 80.0, "min_soc": 15.0, "min_soc_reached": False}
    ]
    assert report["meter_quality"]["samples"] == 1
    assert not any(cause["code"].startswith("live_control_snapshot") for cause in report["root_causes"])


def test_runtime_state_is_no_longer_read_for_cycle_values(tmp_path):
    """The keys diagnose used to look for there are ignored, not merged."""

    runtime_path = tmp_path / "runtime-state.json"
    runtime_path.write_text(json.dumps({
        "grid_power_w": 999,
        "controller": {"effective_target_total_w": 999, "timestamp": "2026-01-01T00:00:00+00:00"},
        "control_samples": [999, 999, 999],
        "devices": {"WR1": {"enabled": True, "soc": 99, "output_w": 999}},
    }))

    report = diagnostics.diagnose_control_report(CONFIG, str(runtime_path))
    quality = diagnostics.diagnose_control_quality_report(CONFIG, str(runtime_path))

    assert report["snapshot"]["grid_power_w"] is None
    assert report["snapshot"]["target_output_w"] is None
    assert report["soc_analysis"]["devices"] == []
    assert quality["export_import"]["samples"] == 0


@pytest.mark.parametrize(
    ("prepare", "status", "code"),
    [
        (lambda path: None, "missing", "live_control_snapshot_missing"),
        (lambda path: path.write_text("{not json"), "unreadable", "live_control_snapshot_unreadable"),
        (
            lambda path: path.write_text(json.dumps({"schema_version": 99, "written_at": "2026-01-01T00:00:00+00:00"})),
            "unreadable",
            "live_control_snapshot_unreadable",
        ),
        (
            lambda path: write_live_control_status(path.parent, age_seconds=600),
            "stale",
            "live_control_snapshot_stale",
        ),
    ],
    ids=["missing", "invalid-json", "unknown-schema", "stale"],
)
def test_a_snapshot_that_is_not_fresh_is_never_reported_as_healthy(tmp_path, prepare, status, code):
    runtime_path = tmp_path / "runtime-state.json"
    runtime_path.write_text(json.dumps({"system": {"enabled": True}}))
    prepare(resolve_control_status_path(runtime_path))

    report = diagnostics.diagnose_control_report(CONFIG, str(runtime_path))
    quality = diagnostics.diagnose_control_quality_report(CONFIG, str(runtime_path))
    checks = []
    diagnostics.diagnose_control_add_checks(checks, report)

    assert report["runtime_state"]["snapshot_status"] == status
    assert report["runtime_state"]["checked"] is (status == "stale")
    assert report["snapshot"]["grid_power_w"] is None
    assert report["meter_quality"]["samples"] == 0
    cause = next(cause for cause in report["root_causes"] if cause["code"] == code)
    assert cause["severity"] == "warning"
    assert code in {cause["code"] for cause in quality["root_causes"]}
    assert quality["status"] != "ok"
    assert diagnostics.diagnose_status_from_checks(checks) != "ok"
    assert not any(check["code"] == "control_staleness_skipped" for check in checks)


def test_sampling_collects_the_cycles_fetched_inside_the_window(tmp_path, monkeypatch):
    """Protected interleaving: diagnose polls, the EMS publishes between polls.

    The EMS's cycles are driven from the poll's own sleep, so each poll sees
    exactly the cycles published before it, and none from before the window.
    """

    ems = LiveEms(tmp_path)
    started = time.time()
    ems.cycle(-500, started - 10)
    published = []

    def publish_between_polls(_seconds):
        value = 10 * (len(published) + 1)
        published.append(value)
        ems.cycle(value, started + len(published))

    monkeypatch.setattr(time, "sleep", publish_between_polls)
    runtime_path = str(tmp_path / "runtime-state.json")
    live = diagnostics.diagnose_live_snapshot(runtime_path, 5)

    samples = diagnostics.diagnose_control_samples(runtime_path, live, 3, 5)

    assert [sample["grid_power_w"] for sample in samples] == published
    assert len(published) == 2


def test_a_device_s_allocation_reason_is_the_limit_the_cycle_named():
    live = {
        "devices": {
            "WR1": {"allocated_target_w": 300, "output_limit_w": 300, "limiting_reason": "deadband"},
            "WR2": {"allocated_target_w": 500, "output_limit_w": 200},
            "WR3": {"allocated_target_w": 800},
        }
    }
    runtime = {"devices": {"WR3": {"max_power": 800}}}

    distribution = diagnostics.diagnose_control_distribution({"devices": []}, runtime, live)

    reasons = {item["device"]: item["reason"] for item in distribution["devices"]}
    assert reasons == {
        "WR1": "limited by deadband",
        "WR2": "configured allocation",
        "WR3": "limited by runtime max power",
    }
