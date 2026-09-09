# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Maintenance control-and-safety statement, rendered by the shipped code.

This view is the one place the console answers "may EMS change my inverter right
now". It must never claim more than the backend proved: an unreadable control
block resolves to an explicit unknown, and the single-controller warning stands
whenever any transport can actually write.

The real admin.js view function runs through
tests/js/maintenance_control_state_runner.js, so these contracts hold against
the shipped renderer rather than a rebuilt copy.
"""

import json
import os
import shutil
import subprocess

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.contract,
    pytest.mark.simulation,
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(ROOT, "admin", "static")
RUNNER = os.path.join(ROOT, "tests", "js", "maintenance_control_state_runner.js")

SINGLE_CONTROLLER_NOTE = (
    "Only one controller may change inverter output. Do not run a second one."
)


def _read(name):
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def _transport(control_gate, gate, armed=True, device_count=1, blocked_by=()):
    return {
        "control_gate": control_gate,
        "gate": gate,
        "transport": control_gate,
        "armed": armed,
        "blocked_by": list(blocked_by),
        "device_count": device_count,
    }


def _control(status="may_control", **overrides):
    control = {
        "status": status,
        "enabled": True,
        "dry_run": False,
        "simulation_mode": False,
        "state_reconciliation": True,
        "transports": [
            _transport("api", "allow_hardware_writes", device_count=2),
            _transport(
                "mqtt_local",
                "allow_mqtt_local_control_writes",
                armed=False,
                device_count=0,
                blocked_by=["allow_mqtt_local_control_writes"],
            ),
            _transport(
                "mqtt_zendure",
                "allow_mqtt_zendure_control_writes",
                armed=False,
                device_count=0,
                blocked_by=["allow_mqtt_zendure_control_writes"],
            ),
        ],
        "envelope": {
            "total_output_w": 1600,
            "device_output_w": 800,
            "soc_min": 10,
            "soc_max": 100,
            "soc_uniform": True,
        },
    }
    control.update(overrides)
    return control


def _view(control):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, RUNNER],
        input=json.dumps({"control": control}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_permitted_installation_says_so_without_claiming_ems_is_running():
    """The block projects the saved config; it never observes the container."""

    view = _view(_control())
    assert view["status"] == "may_control"
    assert view["tone"] == "ok"
    assert view["verdict"] == "EMS is allowed to change your inverters."


@pytest.mark.parametrize(
    "status, verdict",
    [
        ("disabled", "EMS control is switched off."),
        (
            "simulated",
            "EMS uses simulated data and does not change your inverters.",
        ),
        (
            "calculating_only",
            "EMS only calculates and does not change your inverters.",
        ),
        ("not_writing", "Nothing may change an inverter right now."),
    ],
)
def test_every_non_writing_state_says_plainly_that_nothing_is_written(status, verdict):
    view = _view(_control(status))
    assert view["verdict"] == verdict
    assert view["tone"] != "ok"


@pytest.mark.parametrize("control", [None, {}, {"status": "made-up"}, "broken", []])
def test_an_unproven_control_state_reads_as_unknown_never_as_healthy(control):
    view = _view(control)
    assert view["status"] == "unknown"
    assert view["tone"] == "warn"
    assert view["verdict"] == "The control state of this installation is unknown."


def test_each_transport_is_named_in_plain_words_with_its_device_count():
    rows = {row["label"]: row for row in _view(_control())["transports"]}
    assert list(rows) == [
        "Local connection",
        "Your own MQTT broker",
        "Zendure cloud",
    ]
    assert rows["Local connection"]["value"] == "allowed · 2 devices"
    assert rows["Local connection"]["tone"] == "ok"
    assert rows["Your own MQTT broker"]["value"] == "not allowed"
    assert rows["Your own MQTT broker"]["tone"] is None


def test_an_allowed_transport_without_a_device_is_not_dressed_up_as_writing():
    control = _control(
        transports=[
            _transport("api", "allow_hardware_writes", device_count=0),
            _transport(
                "mqtt_local", "allow_mqtt_local_control_writes", device_count=0
            ),
            _transport(
                "mqtt_zendure", "allow_mqtt_zendure_control_writes", device_count=0
            ),
        ]
    )
    rows = {row["label"]: row for row in _view(control)["transports"]}
    assert rows["Local connection"]["value"] == "allowed · no device uses it"
    assert rows["Local connection"]["tone"] is None


def test_one_device_is_counted_in_the_singular():
    control = _control(
        transports=[_transport("api", "allow_hardware_writes", device_count=1)]
    )
    rows = {row["label"]: row for row in _view(control)["transports"]}
    assert rows["Local connection"]["value"] == "allowed · 1 device"


def test_the_single_controller_warning_stands_whenever_something_can_write():
    assert SINGLE_CONTROLLER_NOTE in _view(_control())["notes"]


def test_the_statement_says_where_it_comes_from_and_when_it_applies():
    note = (
        "This is what your saved settings allow. Changes apply after EMS restarts."
    )
    assert note in _view(_control())["notes"]
    assert note in _view(None)["notes"]


def test_the_single_controller_warning_is_absent_when_nothing_can_write():
    control = _control(
        "not_writing",
        transports=[
            _transport(
                "api",
                "allow_hardware_writes",
                armed=False,
                device_count=2,
                blocked_by=["allow_hardware_writes"],
            )
        ],
    )
    assert SINGLE_CONTROLLER_NOTE not in _view(control)["notes"]


def test_state_reconciliation_is_stated_as_its_own_permission():
    note = "EMS may restore device settings it expects, such as the minimum charge."
    assert note in _view(_control())["notes"]
    assert note not in _view(_control(state_reconciliation=False))["notes"]


def test_the_physical_envelope_is_shown_in_units_an_owner_recognizes():
    rows = {row["label"]: row["value"] for row in _view(_control())["envelope"]}
    assert rows["Maximum output"] == "1600 W total · 800 W per device"
    assert rows["Charge window"] == "10–100 %"


def test_a_mixed_charge_window_is_marked_rather_than_averaged():
    control = _control()
    control["envelope"]["soc_uniform"] = False
    rows = {row["label"]: row["value"] for row in _view(control)["envelope"]}
    assert rows["Charge window"] == "10–100 % (differs per device)"


def test_an_unknown_envelope_value_is_omitted_rather_than_guessed():
    control = _control()
    control["envelope"] = {
        "total_output_w": None,
        "device_output_w": None,
        "soc_min": None,
        "soc_max": None,
        "soc_uniform": None,
    }
    assert _view(control)["envelope"] == []


def _status_panel(html):
    panel = html.split('id="maintenance-status-panel"', 1)[1].split(
        'id="maintenance-settings-panel"', 1
    )[0]
    assert panel, "status panel slice is empty"
    return panel


def test_the_stage_is_read_only_markup_on_the_status_page():
    html = _read("index.html")
    manual = _status_panel(html)
    stage_start = manual.index('id="maintenance-control-state"')
    stage = manual[stage_start:].split("</section>", 1)[0]
    for marker in (
        'id="maintenance-control-verdict"',
        'id="maintenance-control-transports"',
        'id="maintenance-control-envelope"',
        'id="maintenance-control-notes"',
    ):
        assert marker in stage
    # It states what the saved config permits and offers no way to change it.
    for forbidden in ("<input", "<select", "<form"):
        assert forbidden not in stage
    # The one control is navigation to the page that can change these settings.
    assert stage.count("<button") == 1
    assert 'data-open-maintenance-path="settings-safety"' in stage
    assert "Change these settings" in stage


def test_the_stage_stands_above_the_collapsible_cards():
    html = _read("index.html")
    manual = _status_panel(html)
    assert manual.index('id="maintenance-control-state"') < manual.index(
        'id="maintenance-layout"'
    )


def test_the_control_answer_leads_the_page():
    """The safety statement stands above the technical status line, not below it."""

    html = _read("index.html")
    manual = _status_panel(html)
    assert manual.index('id="maintenance-control-state"') < manual.index(
        'id="maintenance-system-status"'
    )
