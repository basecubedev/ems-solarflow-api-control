# SPDX-License-Identifier: AGPL-3.0-or-later
import logging
import shutil
import subprocess
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from dashboard.telemetry import build_dashboard_snapshot
from ems.state_store import describe_full_charge_assist_status


ROOT = Path(__file__).resolve().parents[1]


NOW = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
CONFIG = {"assist_window_days": 7}


def test_disabled_status_when_feature_not_enabled():
    result = describe_full_charge_assist_status(CONFIG, False, True, None, NOW)
    assert result["status"] == "disabled"
    assert result["inside_assist_window"] is False


def test_ignored_no_battery_status():
    result = describe_full_charge_assist_status(CONFIG, True, False, None, NOW)
    assert result["status"] == "ignored_no_battery"


def test_unknown_status_when_record_missing():
    result = describe_full_charge_assist_status(CONFIG, True, True, None, NOW)
    assert result["status"] == "unknown"


def test_active_status():
    record = {
        "full_charge_assist_active": True,
        "restore_pending": True,
        "ac_mode_restore_pending": True,
        "next_due_at": (NOW + timedelta(days=2)).isoformat(),
        "last_seen_soc_limit": 0,
    }
    result = describe_full_charge_assist_status(CONFIG, True, True, record, NOW)
    assert result["status"] == "active"
    assert result["inside_assist_window"] is True
    assert result["days_until_due"] == 2


def test_restore_pending_status_takes_priority_over_window():
    record = {
        "full_charge_assist_active": False,
        "restore_pending": True,
        "ac_mode_restore_pending": False,
        "next_due_at": (NOW + timedelta(days=2)).isoformat(),
        "last_seen_soc_limit": 0,
    }
    result = describe_full_charge_assist_status(CONFIG, True, True, record, NOW)
    assert result["status"] == "restore_pending"


def test_completed_status_from_last_seen_soc_limit():
    record = {
        "full_charge_assist_active": False,
        "restore_pending": False,
        "ac_mode_restore_pending": False,
        "next_due_at": (NOW + timedelta(days=20)).isoformat(),
        "last_seen_soc_limit": 1,
    }
    result = describe_full_charge_assist_status(CONFIG, True, True, record, NOW)
    assert result["status"] == "completed"


def test_overdue_status_when_due_date_in_past():
    record = {
        "full_charge_assist_active": False,
        "restore_pending": False,
        "ac_mode_restore_pending": False,
        "next_due_at": (NOW - timedelta(days=1)).isoformat(),
        "last_seen_soc_limit": 0,
    }
    result = describe_full_charge_assist_status(CONFIG, True, True, record, NOW)
    assert result["status"] == "overdue"


def test_inside_assist_window_true_within_window_days():
    record = {
        "full_charge_assist_active": False,
        "restore_pending": False,
        "ac_mode_restore_pending": False,
        "next_due_at": (NOW + timedelta(days=3)).isoformat(),
        "last_seen_soc_limit": 0,
    }
    result = describe_full_charge_assist_status(CONFIG, True, True, record, NOW)
    assert result["status"] == "window"
    assert result["inside_assist_window"] is True
    assert result["days_until_due"] == 3


def test_inside_assist_window_false_outside_window_days():
    record = {
        "full_charge_assist_active": False,
        "restore_pending": False,
        "ac_mode_restore_pending": False,
        "next_due_at": (NOW + timedelta(days=20)).isoformat(),
        "last_seen_soc_limit": 0,
    }
    result = describe_full_charge_assist_status(CONFIG, True, True, record, NOW)
    assert result["status"] == "ok"
    assert result["inside_assist_window"] is False
    assert result["days_until_due"] == 20


class AssistStoreStub:
    def __init__(self, record):
        self.record = record

    def get_device_state(self, device, now=None):
        return self.record


def _controller(*, enabled, has_battery, record, raise_error=False):
    if raise_error:
        def boom(*args, **kwargs):
            raise RuntimeError("legacy deployment without assist support")

        return SimpleNamespace(
            devices=[SimpleNamespace(name="WR1")],
            runtime_state=None,
            device_online={"WR1": True},
            commanded_total_w=0,
            filtered_load_w=0,
            _dashboard_capabilities=[],
            full_charge_assist_config=boom,
        )

    return SimpleNamespace(
        devices=[SimpleNamespace(name="WR1")],
        runtime_state=None,
        device_online={"WR1": True},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
        full_charge_assist_config=lambda: {"assist_window_days": 7},
        full_charge_assist_enabled=lambda: enabled,
        full_charge_assist_has_battery=lambda dev, state: has_battery,
        battery_full_charge_store=AssistStoreStub(record),
    )


def _state(**overrides):
    defaults = dict(
        solar=500,
        output=700,
        pack_out=220,
        pack_in=20,
        soc=64,
        soc_limit=0,
        ac_mode=2,
        ac_status=1,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _snapshot_assist(controller, state):
    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=[state],
        targets=[700],
        effective_targets=[700],
        allocated_total_w=700,
        effective_total_w=700,
        enabled=True,
        max_total_power=700,
        min_output_limit=35,
    )
    return snapshot["devices"]["WR1"]["battery_full_charge_assist"]


def test_api_includes_battery_full_charge_assist_when_available():
    controller = _controller(
        enabled=True,
        has_battery=True,
        record={
            "full_charge_assist_active": True,
            "restore_pending": True,
            "ac_mode_restore_pending": True,
            "assist_started_at": "2026-06-14T10:00:00+02:00",
            "last_full_charge_at": None,
            "next_due_at": "2026-06-16T14:00:00+02:00",
            "last_seen_soc_limit": 0,
        },
    )
    assist = _snapshot_assist(controller, _state(soc_limit=0, ac_mode=1, ac_status=2))

    assert assist["enabled"] is True
    assert assist["has_battery"] is True
    assert assist["status"] == "active"
    assert assist["restore_pending"] is True
    assert assist["ac_mode_restore_pending"] is True
    assert assist["soc_limit"] == 0
    assert assist["ac_mode"] == 1
    assert assist["ac_status"] == 2
    assert assist["assist_started_at"] == "2026-06-14T10:00:00+02:00"


def test_devices_without_battery_are_marked_ignored():
    controller = _controller(enabled=True, has_battery=False, record=None)
    assist = _snapshot_assist(controller, _state())

    assert assist["status"] == "ignored_no_battery"
    assert assist["has_battery"] is False


def test_missing_assist_state_does_not_break_device_status_api(caplog):
    controller = _controller(
        enabled=True, has_battery=True, record=None, raise_error=True
    )
    with caplog.at_level(logging.DEBUG, logger="root"):
        assist = _snapshot_assist(controller, _state())

    assert assist["status"] == "unknown"
    assert assist["enabled"] is False
    # The real cause must be recorded at DEBUG instead of vanishing silently.
    assert any(
        "event=dashboard_assist_payload_unavailable" in record.message
        and record.levelno == logging.DEBUG
        for record in caplog.records
    )


def test_disabled_feature_serializes_disabled_status():
    controller = _controller(enabled=False, has_battery=True, record=None)
    assist = _snapshot_assist(controller, _state())

    assert assist["status"] == "disabled"


class MultiAssistStoreStub:
    def __init__(self, records):
        self.records = records

    def get_device_state(self, device, now=None):
        return self.records.get(device)


def _multi_controller(records):
    names = list(records)
    return SimpleNamespace(
        devices=[SimpleNamespace(name=name) for name in names],
        runtime_state=None,
        device_online={name: True for name in names},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
        full_charge_assist_config=lambda: {"assist_window_days": 7},
        full_charge_assist_enabled=lambda: True,
        full_charge_assist_has_battery=lambda dev, state: True,
        battery_full_charge_store=MultiAssistStoreStub(records),
    )


def _full_snapshot(controller, states):
    count = len(states)
    return build_dashboard_snapshot(
        controller,
        load_w=0,
        states=states,
        targets=[0] * count,
        effective_targets=[0] * count,
        allocated_total_w=0,
        effective_total_w=0,
        enabled=True,
        max_total_power=700,
        min_output_limit=35,
    )


def _active_assist_record():
    return {
        "full_charge_assist_active": True,
        "restore_pending": True,
        "ac_mode_restore_pending": True,
        "next_due_at": (NOW + timedelta(days=2)).isoformat(),
        "last_seen_soc_limit": 0,
    }


def _idle_assist_record():
    return {
        "full_charge_assist_active": False,
        "restore_pending": False,
        "ac_mode_restore_pending": False,
        "next_due_at": (NOW + timedelta(days=20)).isoformat(),
        "last_seen_soc_limit": 0,
    }


def test_full_charge_assist_rule_active_when_device_assisting():
    controller = _multi_controller({"WR1": _active_assist_record()})
    snapshot = _full_snapshot(controller, [_state()])

    rule = snapshot["rules"]["full_charge_assist_active"]
    assert rule["active"] is True
    assert "WR1" in rule["reason"]


def test_full_charge_assist_rule_inactive_without_active_device():
    controller = _multi_controller({"WR1": _idle_assist_record()})
    snapshot = _full_snapshot(controller, [_state()])

    rule = snapshot["rules"]["full_charge_assist_active"]
    assert rule["active"] is False
    assert rule["reason"] == "no device currently in full-charge assist"


def test_full_charge_assist_rule_lists_all_active_devices():
    controller = _multi_controller(
        {
            "WR1": _active_assist_record(),
            "WR2": _idle_assist_record(),
            "WR3": _active_assist_record(),
        }
    )
    snapshot = _full_snapshot(controller, [_state(), _state(), _state()])

    rule = snapshot["rules"]["full_charge_assist_active"]
    assert rule["active"] is True
    assert "WR1" in rule["reason"]
    assert "WR3" in rule["reason"]
    assert "WR2" not in rule["reason"]


def test_dashboard_rules_contract_includes_expected_keys():
    controller = _multi_controller({"WR1": _idle_assist_record()})
    snapshot = _full_snapshot(controller, [_state()])

    # These keys back the fixed label list rendered by renderRules() in app.js.
    # A backend rename/removal would silently degrade the GUI to "inactive",
    # so keep the contract explicit here.
    expected = {
        "ems_enabled",
        "soc_limit_active",
        "output_limit_active",
        "winter_soc_mode",
        "full_charge_assist_active",
        "pv_priority_balancing",
        "battery_balancing",
        "night_min_soc_idle",
        "offline_devices",
    }
    rules = snapshot["rules"]
    assert expected <= set(rules)
    for key in expected:
        rule = rules[key]
        assert set(rule) == {"active", "reason"}
        assert isinstance(rule["active"], bool)
        assert isinstance(rule["reason"], str)


def test_frontend_renders_full_charge_assist_states_via_app_js():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for executable dashboard rendering test")

    script = textwrap.dedent(
        """
        const fs = require("fs");
        const vm = require("vm");
        const appPath = process.argv[2];
        const source = fs.readFileSync(appPath, "utf8")
          .split('document.querySelectorAll(".range-tabs button")')[0];

        class FakeClassList {
          constructor() { this.values = new Set(); }
          toggle(name, force) {
            if (force) this.values.add(name);
            else this.values.delete(name);
          }
          contains(name) { return this.values.has(name); }
        }

        class FakeElement {
          constructor(id = "") {
            this.id = id;
            this.textContent = "";
            this.innerHTML = "";
            this.className = "";
            this.children = [];
            this.attrs = {};
            this.classList = new FakeClassList();
            this.style = {
              values: {},
              setProperty: (key, value) => { this.style.values[key] = value; },
            };
          }
          setAttribute(key, value) { this.attrs[key] = value; }
          appendChild(child) { this.children.push(child); }
          querySelectorAll() { return []; }
        }

        const elements = new Map();
        function element(id) {
          if (!elements.has(id)) elements.set(id, new FakeElement(id));
          return elements.get(id);
        }

        const context = {
          console,
          document: {
            getElementById: element,
            createElement: () => new FakeElement(),
            querySelector: () => null,
            querySelectorAll: () => [],
          },
          window: { addEventListener: () => {} },
        };

        vm.createContext(context);
        vm.runInContext(source, context, { filename: appPath });

        function assert(condition, message) {
          if (!condition) throw new Error(message);
        }

        // Active assist renders a visible status pill/text.
        let html = context.renderFullChargeAssist({
          battery_full_charge_assist: {
            enabled: true,
            has_battery: true,
            status: "active",
            inside_assist_window: true,
            days_until_due: 1,
            assist_window_days: 7,
            last_full_charge_at: null,
            next_due_at: "2026-06-16T14:00:00+02:00",
            window_starts_at: "2026-06-09T14:00:00+02:00",
            assist_started_at: "2026-06-14T14:05:00+02:00",
            restore_pending: true,
            ac_mode_restore_pending: true,
            soc_limit: 0,
            ac_mode: 1,
            ac_status: 2,
            message: "Full-charge assist active",
          },
        });
        assert(html.includes("Assist active"), "active status renders a visible pill label");
        assert(html.includes("Full-charge assist active"), "active status renders backend message");
        assert(html.includes("AC-charging for monthly battery calibration support"), "active status with AC charging renders calibration support note");
        assert(html.includes("AC charge"), "active status with AC charging renders an AC charge fact row");
        // restore_pending/ac_mode_restore_pending are also set while assist is
        // still active (pending confirmation of the initial write), and mean a
        // restore is *planned* for after charging, not a current restore problem.
        assert(html.includes("Restore planned"), "active status with pending restore flags shows restore as planned follow-up");
        assert(!html.includes("Restore: restore"), "active status never renders the raw 'Restore: restore' label");
        assert(!html.includes("AC mode restore"), "active status never renders the misleading 'AC mode restore' label");
        assert(!html.includes("Max-SoC restore"), "active status does not render the post-assist Max-SoC restore fact");
        assert(!html.includes("AC output mode"), "active status does not render the post-assist AC output mode restore fact");

        // Active assist without AC charge mode renders neutral text and does
        // not claim AC charging is running.
        html = context.renderFullChargeAssist({
          battery_full_charge_assist: {
            enabled: true,
            has_battery: true,
            status: "active",
            inside_assist_window: true,
            days_until_due: 1,
            assist_window_days: 7,
            last_full_charge_at: null,
            next_due_at: "2026-06-16T14:00:00+02:00",
            window_starts_at: "2026-06-09T14:00:00+02:00",
            assist_started_at: "2026-06-14T14:05:00+02:00",
            restore_pending: false,
            ac_mode_restore_pending: false,
            soc_limit: 0,
            ac_mode: 2,
            ac_status: 1,
            message: "Full-charge assist active",
          },
        });
        assert(html.includes("Assist active"), "active status (no AC charge) renders a visible pill label");
        assert(html.includes("EMS is helping this device reach firmware Max-SoC"), "active status renders neutral assist message");
        assert(!html.includes("AC-charging"), "active status without AC charge mode does not claim AC charging");
        assert(!html.includes("AC charge"), "active status without AC charge mode renders no AC charge fact row");

        // Active assist, AC charge requested but not yet running (acStatus != 2):
        // restore is shown as planned, but AC charge running must not be claimed.
        html = context.renderFullChargeAssist({
          battery_full_charge_assist: {
            enabled: true,
            has_battery: true,
            status: "active",
            inside_assist_window: true,
            days_until_due: 1,
            assist_window_days: 7,
            last_full_charge_at: null,
            next_due_at: "2026-06-16T14:00:00+02:00",
            window_starts_at: "2026-06-09T14:00:00+02:00",
            assist_started_at: "2026-06-14T14:05:00+02:00",
            restore_pending: true,
            ac_mode_restore_pending: true,
            soc_limit: 0,
            ac_mode: 1,
            ac_status: 0,
            message: "Full-charge assist active",
          },
        });
        assert(html.includes("Assist active"), "active status (AC charge not yet running) renders a visible pill label");
        assert(html.includes("Restore planned"), "active status with pending restore flags shows restore as planned, even before AC charge starts");
        assert(!html.includes("AC charge"), "AC charge running is not shown unless acStatus == 2");
        assert(!html.includes("AC-charging"), "active status description does not claim AC charging unless acStatus == 2");

        // Overdue renders positive wording, never a negative "Due in" value.
        html = context.renderFullChargeAssist({
          battery_full_charge_assist: {
            enabled: true,
            has_battery: true,
            status: "overdue",
            inside_assist_window: false,
            days_until_due: -2,
            assist_window_days: 7,
            last_full_charge_at: "2026-05-01T13:42:00+02:00",
            next_due_at: "2026-06-12T13:42:00+02:00",
            window_starts_at: "2026-06-05T13:42:00+02:00",
            assist_started_at: null,
            restore_pending: false,
            ac_mode_restore_pending: false,
            soc_limit: 0,
            ac_mode: 2,
            ac_status: 1,
            message: "Full-charge assist overdue",
          },
        });
        assert(html.includes("Overdue by"), "overdue status renders positive 'Overdue by' wording");
        assert(html.includes("2 d"), "overdue status renders the number of overdue days");
        assert(!html.includes("Due in -2 d"), "overdue status never renders a negative 'Due in' value");
        assert(!html.includes("-2 d"), "overdue status never renders a negative day count");

        // Assist window renders due/window information.
        html = context.renderFullChargeAssist({
          battery_full_charge_assist: {
            enabled: true,
            has_battery: true,
            status: "window",
            inside_assist_window: true,
            days_until_due: 3,
            assist_window_days: 7,
            last_full_charge_at: "2026-06-01T13:42:00+02:00",
            next_due_at: "2026-06-17T13:42:00+02:00",
            window_starts_at: "2026-06-10T13:42:00+02:00",
            assist_started_at: null,
            restore_pending: false,
            ac_mode_restore_pending: false,
            soc_limit: 0,
            ac_mode: 2,
            ac_status: 1,
            message: "Assist window active",
          },
        });
        assert(html.includes("Assist window active"), "window status renders status pill label");
        assert(html.includes("3 d"), "window status renders days until due");
        assert(html.includes("2026-06-10"), "window status renders window start date");

        // Restore pending renders an explicit restore message.
        html = context.renderFullChargeAssist({
          battery_full_charge_assist: {
            enabled: true,
            has_battery: true,
            status: "restore_pending",
            inside_assist_window: false,
            days_until_due: 25,
            assist_window_days: 7,
            last_full_charge_at: "2026-06-14T13:42:00+02:00",
            next_due_at: "2026-07-12T13:42:00+02:00",
            window_starts_at: "2026-07-05T13:42:00+02:00",
            assist_started_at: "2026-06-13T14:05:00+02:00",
            restore_pending: true,
            ac_mode_restore_pending: true,
            soc_limit: 1,
            ac_mode: 1,
            ac_status: 2,
            message: "Restore pending",
          },
        });
        assert(html.includes("Restore pending"), "restore pending renders status pill label");
        assert(html.includes("acMode=2"), "ac mode restore pending renders explicit acMode=2 restore note");
        assert(html.includes("Max-SoC restore"), "restore_pending status renders a Max-SoC restore fact row");
        assert(html.includes("AC output mode"), "restore_pending status renders an AC output mode restore fact row");
        assert(!html.includes("Restore planned"), "restore_pending status does not show the active-assist 'Restore planned' wording");

        // Missing/disabled assist fields render no section instead of "undefined".
        html = context.renderFullChargeAssist({ battery_full_charge_assist: undefined });
        assert(html === "", "missing assist field renders nothing");
        assert(!html.includes("undefined"), "missing assist field never renders literal undefined");

        html = context.renderFullChargeAssist({
          battery_full_charge_assist: {
            enabled: false,
            has_battery: true,
            status: "disabled",
            inside_assist_window: false,
            days_until_due: null,
            message: "Battery full-charge assist disabled",
          },
        });
        assert(html === "", "disabled feature renders no per-device section");

        html = context.renderFullChargeAssist({
          battery_full_charge_assist: {
            enabled: true,
            has_battery: false,
            status: "ignored_no_battery",
            inside_assist_window: false,
            days_until_due: null,
            message: "No battery detected",
          },
        });
        assert(html === "", "device without battery renders no per-device section");

        // Dynamic values are escaped.
        html = context.renderFullChargeAssist({
          battery_full_charge_assist: {
            enabled: true,
            has_battery: true,
            status: "ok",
            inside_assist_window: false,
            days_until_due: 20,
            assist_window_days: 7,
            last_full_charge_at: "2026-06-01T13:42:00+02:00",
            next_due_at: "2026-06-29T13:42:00+02:00",
            window_starts_at: "2026-06-22T13:42:00+02:00",
            assist_started_at: null,
            restore_pending: false,
            ac_mode_restore_pending: false,
            soc_limit: 0,
            ac_mode: 2,
            ac_status: 1,
            message: "<script>alert(1)</script>",
          },
        });
        assert(!html.includes("<script>alert(1)</script>"), "raw message markup is escaped");
        assert(html.includes("&lt;script&gt;"), "message is HTML-escaped before insertion");
        """
    )

    result = subprocess.run(
        [node, "-", str(ROOT / "dashboard/static/app.js")],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_frontend_renders_full_charge_assist_rule_row_via_app_js():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for executable dashboard rendering test")

    script = textwrap.dedent(
        """
        const fs = require("fs");
        const vm = require("vm");
        const appPath = process.argv[2];
        const source = fs.readFileSync(appPath, "utf8")
          .split('document.querySelectorAll(".range-tabs button")')[0];

        class FakeClassList {
          constructor() { this.values = new Set(); }
          toggle(name, force) {
            if (force) this.values.add(name);
            else this.values.delete(name);
          }
          contains(name) { return this.values.has(name); }
        }

        class FakeElement {
          constructor(id = "") {
            this.id = id;
            this.textContent = "";
            this._innerHTML = "";
            this.className = "";
            this.children = [];
            this.attrs = {};
            this.classList = new FakeClassList();
          }
          // Mirror real DOM: assigning innerHTML = "" clears child nodes, which
          // renderRules() relies on to rebuild the list on each call.
          get innerHTML() { return this._innerHTML; }
          set innerHTML(value) {
            this._innerHTML = value;
            if (value === "") this.children = [];
          }
          setAttribute(key, value) { this.attrs[key] = value; }
          appendChild(child) { this.children.push(child); }
          querySelectorAll() { return []; }
        }

        const elements = new Map();
        function element(id) {
          if (!elements.has(id)) elements.set(id, new FakeElement(id));
          return elements.get(id);
        }

        const context = {
          console,
          document: {
            getElementById: element,
            createElement: () => new FakeElement(),
            querySelector: () => null,
            querySelectorAll: () => [],
          },
          window: { addEventListener: () => {} },
        };

        vm.createContext(context);
        vm.runInContext(source, context, { filename: appPath });

        function assert(condition, message) {
          if (!condition) throw new Error(message);
        }

        function rowsHtml() {
          return element("rulesList").children.map((c) => c.innerHTML).join("\\n");
        }
        function assistRow() {
          return element("rulesList").children.find(
            (c) => c.innerHTML.includes("Full-charge assist")
          );
        }

        // Active assist marks the dedicated rule row active and lists devices.
        context.renderRules({
          full_charge_assist_active: { active: true, reason: "WR1, WR5" },
        });
        assert(rowsHtml().includes("Full-charge assist"), "rules panel renders a full-charge assist row");
        assert(rowsHtml().includes("WR1, WR5"), "active assist rule renders the device reason");
        assert(assistRow(), "assist rule row exists when active");
        assert(assistRow().className.includes("active"), "active assist rule marks the row active");

        // Inactive assist keeps the row but without the active class.
        context.renderRules({
          full_charge_assist_active: {
            active: false,
            reason: "no device currently in full-charge assist",
          },
        });
        assert(assistRow(), "assist rule row exists when inactive");
        assert(!assistRow().className.includes("active"), "inactive assist rule is not marked active");

        // Reason text is HTML-escaped before insertion.
        context.renderRules({
          full_charge_assist_active: { active: true, reason: "<script>alert(1)</script>" },
        });
        assert(!rowsHtml().includes("<script>alert(1)</script>"), "rule reason markup is escaped");
        assert(rowsHtml().includes("&lt;script&gt;"), "rule reason is HTML-escaped before insertion");
        """
    )

    result = subprocess.run(
        [node, "-", str(ROOT / "dashboard/static/app.js")],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
