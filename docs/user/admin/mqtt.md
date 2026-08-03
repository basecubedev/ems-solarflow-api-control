# MQTT connections

## Purpose

Set up and check MQTT in the Admin Console: a local broker, the Zendure cloud
broker, several brokers side by side, MQTT grid meters, and the gates that decide
whether EMS may actually write.

This page is the **Admin Console workflow**. For what each transport *is*, how
fast it is and which hardware suits it, read
[Connection types](../connection-types.md) — that page owns the transport
semantics and the measured latency figures.

## When to use this workflow

- Your inverter has no usable local API.
- You already run a local broker and want telemetry to stay on your network.
- You want to try the software with any Zendure device via the cloud.
- You want to check why an MQTT device is telemetry-only.

## Prerequisites

**For Local MQTT:**

- A broker (for example Mosquitto) reachable from the EMS host.
- **The device already publishes to that broker.** Zendure hardware normally
  talks to Zendure's cloud broker, so you must re-point it yourself first.
  **EMS does not automatically redirect a device's MQTT configuration.**
- Credentials and TLS settings for the broker, if it needs them.

**For Zendure Cloud MQTT:**

- Your **Zendure API key**.
- Working internet access.
- No other controller regulating the same device.

## Local MQTT

### 1 — Add the broker

**Where:** Maintenance → **Configuration & hardware** → *Hardware* → **Local MQTT
broker (optional)**. In Guided Setup the same form is under *Add a device
manually*.

**What you enter:** host, port, credentials and TLS settings.

**What it changes:** the draft. Nothing connects until you apply.

**Expected result:** the broker appears as a configured profile that devices can
be assigned to.

### 2 — Assign devices to the broker

**What you see:** each MQTT device carries a **broker reference**, so a device
knows which broker it belongs to.

**What it changes:** which broker EMS subscribes to for that device.

**If it differs:** if the device is not actually publishing to that broker, EMS
will show it as unseen or stale. Re-point the device first — see
[Connection types → Local MQTT](../connection-types.md#local-mqtt) for community
projects that help with re-pointing and topic decoding.

### 3 — Check the result

![Zendure MQTT telemetry card showing a local broker and the cloud broker, both connected, with two online devices](../../assets/screenshots/admin/admin-maintenance-mqtt.png)

**Where:** Maintenance → **Zendure MQTT telemetry**.

**What you see:** runtime state, endpoint, device count, invalid devices, stale
threshold, a card per broker (source, endpoint, device count, connection status)
and a card per device (broker, source, topic family, age, metric count,
capabilities).

**What it changes:** **nothing. This panel is read-only and does not send
commands.** Configured MQTT control devices may still be controlled by the EMS
runtime — that is the runtime's job.

**Expected result:** brokers **connected**, devices **online**, a low age, and
capabilities listing what the device can do.

**If it differs:** *Unavailable* means EMS could not report live status. Devices
with `0` metrics or a large age are not publishing what EMS expects.

## Zendure Cloud MQTT

### 1 — Store the API key

**What you enter:** your Zendure API key in the Admin Console.

**What it changes:** the key is stored **encrypted** in the external secret
store. It is never shown back to you.

### 2 — Authorize and list devices

**What it changes:** the console logs in to Zendure's device-list service with
your key and lists your devices. This is a read step.

**Expected result:** your Zendure devices appear as selectable cloud connections.

### 3 — Apply a selected cloud device

**What it changes:** this is where the credential is provisioned. The Apply step
fetches fresh broker credentials with your API key, stores them encrypted in the
external secret store, and **verifies that EMS can resolve them before the config
is committed**.

**Expected result:** the broker connects over TLS and telemetry arrives.

**If it differs:** if that credential record is later missing, the broker reports
`broker_auth_missing` and is **never connected** — it fails closed rather than
retrying blind.

### What to expect from the cloud path

- **Internet dependency.** No connectivity, no cloud telemetry and no cloud
  writes.
- **Higher latency than Local API or Local MQTT.** See the measured figures in
  [Connection types](../connection-types.md#what-cloud-latency-means-in-daily-use).
  Use `system.loop_interval = 5` seconds with Zendure Cloud MQTT.
- **A published command is not a successful command.** EMS reports a command
  effective only after **telemetry confirmation**; publishing is only a transport
  step. See [Dashboard control](../dashboard/control.md).
- **Run only one controller.** Disable Zendure HEMS, Smart Matching and Zendure
  schedules when EMS controls a device over the cloud broker.

> The cloud broker is bidirectional only with the Zendure App / Home Assistant
> authorization credentials returned by the device-list login. The public
> read-only developer MQTT account can subscribe to telemetry, but its writes are
> silently discarded.

## Multiple brokers, and mixed operation

You can run several brokers at once, and mix transports freely:

- Each MQTT device carries an explicit **broker reference**, so two brokers never
  compete for the same device.
- One inverter on Local API and another on Zendure MQTT is a supported, normal
  setup. They join the same EMS control loop.
- A single physical inverter reachable over several transports stays **one**
  logical device. See
  [Device management](device-management.md#one-inverter-several-connections).

## MQTT grid meters

A Zendure D0 discovered on a local MQTT broker offers **"Use as grid meter"**.
Choosing it maps the D0's `Zendure/sensor/<serial>/totalPower` topic to the
central grid meter, reusing the selected broker profile.

Only one grid meter can be active, and an existing one is replaced only when you
explicitly confirm it. A Zendure D0 or Smart Meter 3CT found over local HTTP is
the simpler choice when you have it — no MQTT setup at all.

## Write safety gates

Whether EMS may write to hardware is decided by **named gates**, one per
transport. All three default to on, and each can be switched off for read-only
validation.

| Transport | Gate |
| --- | --- |
| Local API / local HTTP | `allow_hardware_writes` |
| Local MQTT broker | `allow_mqtt_local_control_writes` |
| Zendure cloud MQTT | `allow_mqtt_zendure_control_writes` |

Every runtime write additionally requires `dry_run=false`, `simulation_mode=false`
and not being a replay.

A gate being on is **not** enough on its own. Output control over MQTT needs the
whole chain:

- an **exact supported hardware model**, resolved from verified device evidence;
- a **compatible broker transport** for that model;
- a **verified write protocol** for it;
- an **enabled control capability** (the per-device `write_output_limit` opt-in);
- an **enabled transport write gate**.

**Topic family and generation are evidence and telemetry grouping only** — they
never authorize a write. **Unknown or conflicting model evidence** keeps a device
**telemetry-only**.

Broker source is part of the decision: a ZenSDK/**scalar** **local** generation
entered through the manual local broker form **stays telemetry-only** with the
reason *"Output control is not verified for this MQTT broker source"*, while the
**Zendure cloud broker** keeps control for a supported model.

Full model: [Safety](../safety.md) ·
[Safety model (technical)](../../technical/safety-model.md).

## State reconciliation is still API-only

MQTT does not yet replace every Local API function. State-reconciliation writes —
`minSoc`, `socSet`, `smartMode`, `gridOffMode`, winter `inputLimit`, full-charge
assist — additionally require `allow_state_reconciliation_writes=true` and are
**API-only**. MQTT control devices are output-only.

If you need those features, keep at least one API path to the device.

## Zendure MQTT migration during an upgrade

A [Guided Upgrade](guided-upgrade.md) reviews the Zendure MQTT migration before
it applies anything, and names:

- which devices the migration affects, and
- which devices **would lose control**.

Read that list before confirming. The review is read-only; the migration is
applied through the EMS-owned migration service, in the upgrade's own ordered
pipeline. Maintenance also exposes the review on its own card.

## Limitations, honestly

- **Cloud confirmation is delayed.** Measured on a physical SolarFlow 800 Pro 2:
  165 of 165 commands confirmed, typically ~2.9 s, slowest 3.4 s. Measured
  values, not a guarantee.
- **Some legacy generations are partly reverse-engineered.** The legacy Zendure
  JSON write method is validated in the test harness; the older non-modern-API
  generations (Hub / Hyper / AIO / Ace) have **not** been confirmed on real
  hardware.
- **Not every model is validated on physical hardware.** See
  [Supported setups](../supported-setups.md) for the four support tiers and the
  evidence behind each row.
- **Unknown or conflicting models stay telemetry-only** by design.

Positive reports are just as valuable as problem reports — they let a device be
marked validated for everyone. Open a
[Device compatibility report](https://github.com/basecubedev/ems-solarflow-api-control/issues/new?template=device_compatibility_report.yml)
with model, firmware, connection type and **sanitized** logs. Remove serial
numbers, tokens, passwords and public IPs first.

## Warnings and common problems

| Symptom | Meaning | What to do |
| --- | --- | --- |
| Broker card shows *disabled* / not connected | Profile disabled or unreachable | Check host, port, credentials, TLS |
| `broker_auth_missing` | The encrypted credential record is gone | Re-apply the cloud device to re-provision it |
| Device *unseen* or large age | It is not publishing to that broker | Re-point the device; EMS never redirects it for you |
| Device is telemetry-only | The authorization chain is incomplete | [Why a device is read-only](device-management.md#why-a-device-is-read-only) |
| *Not ready* naming a missing identifier | Product key or MQTT device ID missing | Complete the write route |
| Control seems slow over cloud | Expected | Use a 5 s loop interval; prefer a local transport |
| Values fight back / oscillate | A second controller is active | Disable Zendure HEMS, Smart Matching and schedules |

## Recovery or next steps

- Which transport suits your hardware → [Connection types](../connection-types.md)
- Add or re-route a device → [Device management](device-management.md)
- Before enabling live writes → [Safety](../safety.md)
- What the dashboard reports per transport →
  [Dashboard devices](../dashboard/devices.md)
- Technical detail →
  [Zendure MQTT power control](../../technical/zendure-mqtt-power-control.md) ·
  [Configuration](../../technical/configuration.md#zendure-mqtt-telemetry-and-control)
