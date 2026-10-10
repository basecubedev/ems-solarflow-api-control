# SPDX-License-Identifier: AGPL-3.0-or-later
"""What an operator typed into the appliance console survives the poll.

The console rebuilds the page every few seconds. A field that has the cursor is
left alone, but one the operator has just left was rebuilt empty, so a typed
SSID, hostname, zone or public key vanished on the next tick. Every non-secret
field now keeps its value in the console's choice memory until the operation it
was typed for has started; a passphrase or password is never kept.
"""

import json
import shutil
import subprocess

import pytest

from tests.test_appliance_manager_frontend import extract

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.appliance]


@pytest.mark.parametrize(
    "form,remembered,forget",
    [
        (
            "renderWifiForm",
            ('"wifi-form-ssid", "value"', '"wifi-form-hidden", "checked"', '"wifi-form-select"'),
            'forget: "wifi-form-"',
        ),
        ("renderHostnameForm", ('"hostname-form-input", "value"',), 'forget: "hostname-form-"'),
        ("renderTimezoneForm", ('"timezone-form-input", "value"',), 'forget: "timezone-form-"'),
        (
            "renderKeyForm",
            ('"key-form-account"', '"key-form-value", "value"'),
            'forget: "key-form-value"',
        ),
    ],
)
def test_every_non_secret_field_survives_the_poll_until_its_operation_starts(
    form, remembered, forget
):
    body = extract(form)

    for key in remembered:
        assert key in body, key
    assert forget in body


def test_a_passphrase_is_never_kept_across_a_rebuild():
    wifi = extract("renderWifiForm")
    passphrase = wifi.split('var passInput = ', 1)[1].split(";", 1)[0]

    assert "remember" not in passphrase
    for form in ("renderPasswordForm", "renderPasswordConfirmForm"):
        assert "remember" not in extract(form), form


def test_a_network_picked_from_the_scan_is_kept_as_the_ssid():
    wifi = extract("renderWifiForm")

    assert 'state.choices["wifi-form-ssid"] = select.value' in wifi


def test_a_started_operation_forgets_what_its_form_held():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the console behaviour test")
    script = (
        extract("clearChoices")
        + "\n"
        + extract("confirmDialog")
        + """
var state = { pending: { operation: { operation_id: "op" }, confirmation_token: "t", plan: {} },
  pendingForget: "hostname-form-",
  choices: { "hostname-form-input": "ems-garage", "wifi-form-ssid": "Home" } };
function api() { return Promise.resolve({ operation: { operation_id: "op" } }); }
function closeDialog() {}
function announce() {}
function render() {}
function showReconnect() {}
var document = { getElementById: function () { return { hidden: true, textContent: "" }; } };
confirmDialog();
setTimeout(function () {
  console.log(JSON.stringify({ choices: state.choices, forget: state.pendingForget }));
}, 0);
"""
    )
    result = subprocess.run([node, "-"], input=script, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])

    assert out == {"choices": {"wifi-form-ssid": "Home"}, "forget": None}


def test_planning_records_which_choices_its_operation_takes():
    assert "state.pendingForget = options.forget || null;" in extract("planOperation")
