# SPDX-License-Identifier: AGPL-3.0-or-later
"""Setup's discovery run shows why the cloud refresh did what it did.

Review 4 (r11): Setup's "Run discovery" (``refreshUnifiedDevices(true)``)
rendered the devices and the cloud panel's device list from the run's answer,
but not its per-source ``refresh`` block. Only the cloud panel's own refresh
button wrote ``mqtt_message``, so over an install whose EMS holds the cloud
session Setup showed proposals flagged ``waiting_for_mqtt_telemetry`` and
``unknown_topic_family`` with no reason. ``syncSourcePanels`` now writes the
cloud source's outcome into the cloud panel's message line, as text.
"""

import json
import os

import pytest

from tests.test_admin_frontend import STATIC_DIR, _run_node

pytestmark = [
    pytest.mark.admin,
    pytest.mark.setup,
    pytest.mark.mqtt,
    pytest.mark.contract,
]

EMS_SESSION_MESSAGE = (
    "Live MQTT telemetry was not sampled: the EMS signs in to the Zendure cloud "
    "broker with this account's MQTT client ID, and a discovery session under it "
    "would disconnect the EMS. The devices come from the Zendure device list."
)

_HARNESS = r"""
const fs = require("fs");
const source = fs.readFileSync(__ADMIN_JS__, "utf8");
function extractFunction(name) {
  const start = source.indexOf("function " + name + "(");
  if (start === -1) throw new Error("missing " + name);
  let depth = 0;
  let cursor = source.indexOf("(", start);
  for (let i = cursor; i < source.length; i++) {
    if (source[i] === "(") depth++;
    else if (source[i] === ")" && --depth === 0) { cursor = i; break; }
  }
  depth = 0;
  for (let i = source.indexOf("{", cursor); i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error("unterminated " + name);
}
function element(text) {
  const node = { innerHTMLWrites: 0 };
  let value = text;
  Object.defineProperty(node, "textContent", {
    get: () => value, set: (next) => { value = String(next); },
  });
  Object.defineProperty(node, "innerHTML", {
    get: () => value, set: (next) => { node.innerHTMLWrites++; value = String(next); },
  });
  return node;
}
const results = [];
for (const data of CASES) {
  const els = { zendureCloudMessage: element("Discovering Zendure cloud devices…") };
  const zendureCloudDevices = [];
  const run = new Function(
    "els", "zendureCloudDevices", "data",
    '"use strict";\n' +
    "function renderZendureCloudDevices() {}\n" +
    "async function pollMdns() {}\n" +
    "async function loadMqttBrokers() {}\n" +
    "async function loadZendureCloudSettings() {}\n" +
    extractFunction("syncSourcePanels") + "\n" +
    "syncSourcePanels(data);\n"
  );
  run(els, zendureCloudDevices, data);
  results.push({
    message: els.zendureCloudMessage.textContent,
    innerHTMLWrites: els.zendureCloudMessage.innerHTMLWrites,
    devices: zendureCloudDevices.length,
  });
}
process.stdout.write(JSON.stringify(results));
"""


def _sync(*cases):
    script = _HARNESS.replace(
        "__ADMIN_JS__", json.dumps(os.path.join(STATIC_DIR, "admin.js"))
    )
    return _run_node("const CASES = " + json.dumps(list(cases)) + ";\n" + script)


def _run_answer(cloud_status):
    sources = {"local_api": {"ok": True, "error": None, "message": None}}
    if cloud_status is not None:
        sources["zendure_mqtt"] = cloud_status
    return {
        "devices": [],
        "details": {"zendure_mqtt": {"candidates": [{"serial_number": "SN-CLOUD1"}]}},
        "refresh": {"requested": True, "status": "ok", "sources": sources},
        "warnings": [],
    }


def test_the_cloud_panel_names_why_live_telemetry_was_not_sampled():
    [shown] = _sync(
        _run_answer(
            {
                "ok": True,
                "error": None,
                "message": None,
                "mqtt_status": "ems_session",
                "mqtt_message": EMS_SESSION_MESSAGE,
            }
        )
    )

    assert shown["message"] == EMS_SESSION_MESSAGE
    assert shown["devices"] == 1


def test_a_failed_cloud_refresh_is_named_in_the_cloud_panel():
    [shown] = _sync(
        _run_answer(
            {
                "ok": False,
                "error": "device_list_failed",
                "message": "Zendure deviceList request failed (code 1007).",
            }
        )
    )

    assert shown["message"] == (
        "Zendure cloud discovery failed: Zendure deviceList request failed (code 1007)."
    )


def test_a_cloud_refresh_without_a_reason_replaces_the_progress_line():
    [shown] = _sync(_run_answer({"ok": True, "error": None, "message": None,
                                 "mqtt_status": "ok"}))

    assert shown["message"] == "Zendure cloud discovery complete."


def test_a_run_that_did_not_refresh_the_cloud_leaves_its_message():
    without_cloud, without_refresh = _sync(
        _run_answer(None), {"devices": [], "details": {}}
    )

    assert without_cloud["message"] == "Discovering Zendure cloud devices…"
    assert without_refresh["message"] == "Discovering Zendure cloud devices…"


def test_the_message_is_written_as_text():
    [shown] = _sync(
        _run_answer({"ok": True, "mqtt_status": "ems_session",
                     "mqtt_message": "<img src=x onerror=alert(1)>"})
    )

    assert shown["message"] == "<img src=x onerror=alert(1)>"
    assert shown["innerHTMLWrites"] == 0
