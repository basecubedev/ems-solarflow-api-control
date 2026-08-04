# Device management

## Purpose

Add, edit, disable, re-enable or remove inverters and grid meters, and switch an
inverter between its available connections — without hand-editing `config.json`.

## When to use this workflow

- A new inverter arrived.
- You replaced or moved your grid meter.
- You want to take one inverter out of control temporarily.
- You want an inverter to run over a different connection (Local API ↔ Local
  MQTT ↔ Zendure MQTT).

## Prerequisites

- An installed EMS, and Maintenance open — see [Maintenance](maintenance.md).
- For a new device: it is powered on and reachable (on the LAN, or publishing to
  a broker you have configured).

## Step-by-step instructions

### 1 — Open the device list

![Configuration and hardware card showing the inverter and grid-meter configuration](../../assets/screenshots/admin/admin-maintenance-config-hardware.png)

**Where:** Maintenance → **Manual configuration / existing system** →
**Configuration & hardware** → *Hardware*.

**What you see:** the **Grid meter** section and an **Inverters / devices**
section with one card per logical device. Each card shows the EMS name (`INV_1`,
`INV_2`, …), the model, the address or route, and a short connection pill —
**API**, **MQTT** or **Zendure MQTT**.

**What it changes:** nothing. This is the current configuration.

### 2 — Edit a device

**What you can change:** the EMS name, the power limits, and connection details.

**What it changes:** the draft only. `config/config.json` is untouched until you
apply.

**Expected result:** the card shows your new values and the card summary reports
that a preview is needed.

**If it differs:** emptying a field **removes** that setting so EMS uses its own
default — that is intended. Leaving a password box blank **keeps** the stored
secret; use the explicit clear control to remove one.

### 3 — Disable, re-enable or remove

| Action | Effect | Reversible |
| --- | --- | --- |
| **Disable** | EMS stops regulating this device. It stays configured. | Yes — re-enable it |
| **Re-enable** | EMS resumes regulating it | Yes |
| **Remove** | The device is deleted from the configuration | Only by adding it again |

> **Enabled state belongs to the device, not to its connection.** Switching an
> inverter between API and MQTT **preserves** whether it is enabled: active stays
> active, and a device you deactivated stays deactivated. A transport change never
> silently turns a device on or off.

A device added through Guided Setup is **enabled by default**.

### 4 — Change the grid meter

**What you see:** the **Grid meter** subsection with a type selector.

**What you enter:** the type (Shelly, everHome EcoTracker, Tasmota, Zendure D0 /
Smart Meter 3CT over HTTP, or an MQTT meter) and its address or topic.

**What it changes:** changing the *type* removes the fields the new type cannot
use — an HTTP meter's address does not linger on an MQTT meter. Keys you added by
hand are left alone.

**Expected result:** exactly one active grid meter.

**If it differs:** only one grid meter can be active, and an existing one is
replaced only when you explicitly confirm it.

### 5 — Switch an inverter to another connection

![Connection options for a device, showing alternative connections and what happens to output control](../../assets/screenshots/admin/admin-maintenance-config-hardware.png)

**What you see:** **Add more devices** is a *connection* list, not just a device
list. It shows every discovered connection that is **not** the one currently
selected.

- A connection for an inverter you have **not** configured yet offers **Add
  inverter**.
- A second connection for an inverter you **already** configured shows
  *Already configured as INV_1 via API* and offers **Use connection**.

**What you select:** **Use connection**.

**What it changes:** that one logical inverter switches to the other connection
immediately. Clicking it *is* the confirmation — there is no second dialog, no
duplicate device, and the EMS name, enabled state and common values are
preserved.

**Expected result:** the device card's connection pill changes. The connection
you left stays listed, so you can switch back without removing the inverter.

**If it differs:** a switch the backend **refuses** is reported, not performed —
for example two different serials on one route, or an unresolved identity. That
is an *Identity conflict*, and it is a refusal on purpose.

> A manual choice is kept even if you later change discovery priority.

### 6 — Preview and apply

**What you select:** preview first, then apply.

**What it changes:** on apply, `config/config.json` is written. A config backup is
made before the write.

**Expected result:** *Config applied*. If the change needs a restart, the console
says so.

**If it differs:**

- **Apply is disabled after an edit** → intended. The preview must refresh first;
  the preview you looked at is exactly what gets applied.
- **Apply is refused, draft kept** → `config/config.json` changed meanwhile
  (another session, a restore, an upgrade). Choose **Review current
  configuration**, re-check, apply again.

### 7 — Restart EMS when required

Adding, removing or re-routing a device generally requires EMS to pick up the new
configuration. The console prompts when a restart is needed. Live-changeable
runtime values are a different thing — see
[Dashboard runtime settings](../dashboard/runtime-settings.md).

## One inverter, several connections

A single physical inverter can be reachable over Local API, a local MQTT broker
and Zendure cloud MQTT at the same time. That is **three connections to one
device**, not three devices.

- The console groups them into **one** logical inverter and shows the selected
  connection as a pill.
- **Discovery priority** picks the preferred connection automatically, but it
  moves a device on its own **only when the new connection keeps output
  control**. Otherwise you are asked, with both connections named.
- A serial-less Cloud MQTT inverter you selected before its serial was known is
  recognised as the **same** inverter once discovery reports that Cloud route
  carrying a physical serial. It keeps its name, stays one card, and is never
  offered as a second device to add.

## Why a device is read-only

Output control is **not a checkbox**. It follows the exact model and the write
route, and it fails closed:

- **Unknown / telemetry only**, no model, or conflicting model evidence → the
  device stays read-only.
- A hardware *generation* or topic family alone never authorises writes — but it
  never blocks one either.
- The write route is `iot/<productKey>/<deviceId>/…`. If the **Product key** or
  **MQTT device ID** is missing, readiness reports *Not ready* and names what is
  missing; the device is added as a telemetry source until the route is complete.
- The **broker source** matters too: a ZenSDK/Cloud-scalar generation entered
  through the manual *local* broker form is added as telemetry with the reason
  *"Output control is not verified for this MQTT broker source"*, while the older
  Hub/Hyper generation stays controllable.

The form shows the model's write protocol, supported operations and validation
maturity **before** control can be enabled. See
[Supported setups](../supported-setups.md) for what is validated on real
hardware.

## Two identity fields, and why

When adding a Zendure MQTT device manually there are two separate fields:

| Field | What it identifies |
| --- | --- |
| **Physical serial number** | The inverter itself — telemetry matching and duplicate detection |
| **MQTT device ID** | The exact MQTT route/payload id a control write targets |

**The physical serial is never used as the route id.** A telemetry-only device
needs only the serial.

## What happens in the background

The backend — not the browser — decides:

- **Identity** — from the trusted serial and the scoped route, never from a raw
  Cloud route id or a display name.
- **Candidate state** — which connections genuinely exist right now.
- **Workflow ownership** — that this tab is allowed to change this workflow.
- **Exact preview authority** — that the plan being applied is the one you saw.

The browser renders and collects input. It never authorises a device change.

## Warnings and common problems

| Symptom | Meaning | What to do |
| --- | --- | --- |
| *Identity conflict* | Two different serials on one route | Refused on purpose. Check which device really owns that route |
| *Stale device plan* | Discovery changed since you answered | Re-answer the connection question |
| Apply refused, draft kept | Live config changed meanwhile | **Review current configuration**, then apply |
| Device shows as telemetry only | Model or route not proven | See [Why a device is read-only](#why-a-device-is-read-only) |
| A device seems to have vanished after a switch | It did not — one device, new connection | Check the connection pill on the card |

## Recovery or next steps

- MQTT specifics → [MQTT](mqtt.md)
- What the dashboard shows per device →
  [Dashboard devices](../dashboard/devices.md)
- Before enabling live writes → [Safety](../safety.md)
- Report hardware that works (or does not) →
  [Supported setups](../supported-setups.md)
