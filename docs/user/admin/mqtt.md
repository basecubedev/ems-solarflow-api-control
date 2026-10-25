# MQTT connections

## Purpose

Set up and check MQTT in the Admin Console: your own broker, the Zendure cloud
broker, several brokers side by side, MQTT grid meters, and the gates that
decide whether EMS may actually write.

This page is the **Admin Console workflow**. For what each transport *is*, how
fast it is and which hardware suits it, read
[Connection types](../connection-types.md) — that page owns the transport
semantics and the measured latency figures.

## When to use this workflow

- Your inverter has no usable local API.
- You already run a local broker and want telemetry to stay on your network.
- You want to try the software with any Zendure device via the cloud.
- You want to check why an MQTT device only reads values.

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

### 1 — Save the broker's credential

**Where:** **Settings & devices** → **Devices** → **What the search may use** →
**Local MQTT broker credentials**. The guided setup has the same card on its
**Devices** step, as *Credentials for the search*.

**What you enter:** a **Name**, the **Username** and the **Password**, then
**Save credential**. A broker that takes no sign-in needs no credential.

**What it changes:** the credential is saved encrypted on the device. *The
search tries every saved credential, and none, on the brokers it reaches.
Passwords are never shown again.* Nothing is added to the settings yet.

**Expected result:** the credential is listed as **Encrypted**. **Listen to the
brokers again** listens to every reachable broker now and says how many devices
it found.

### 2 — Add the devices on the broker

**What you select:** **Search my network** on the **Devices** tab. The search
listens to the brokers it reaches; an inverter it finds there offers **Add
inverter**, a Zendure D0 meter **Use as grid meter**, and an inverter you
already have over another connection **Use this connection**. See
[Device management](device-management.md#2--find-a-new-device).

**By hand:** **Add an inverter over MQTT** asks for the broker — its **Broker
name** (saved as `local_mqtt_<name>`), host, port, security, username and
password — and adds the inverter. *For a Zendure cloud device, leave the host
empty.* Its username and password are saved as such a credential, so no
password enters the settings.

**What it changes:** your unsaved changes. Each MQTT device names exactly one
broker, so a device knows which broker it belongs to. **Check changes** and
**Save and apply** write it.

**If it differs:** a device the broker does not carry stays **Not heard yet**
or **Stale** after the save. Re-point the device first — see
[Connection types → Local MQTT](../connection-types.md#local-mqtt) for
community projects that help with re-pointing and topic decoding.

### 3 — Check the result

![System page: Device telemetry Set up with two devices, none with a problem, offline after 120 s, the brokers local_mqtt and zendure_cloud connected, and the devices INV_1 and INV_2 online](../../assets/screenshots/admin/admin-system.png)

**Where:** **System** → **Device telemetry**.

**What you see:** **Telemetry** (**Set up**, **Off** or **Not available**),
**Devices**, **Devices with a problem** and **Counts as offline after**; where
the state comes from (*As the running EMS reports it.*, or *From the settings
only*); **Brokers**, each with its address, device count and **Connected**,
**Connecting**, **Not connected** or **Off**; and **Devices**, each with its
broker, when it was last heard and how many values it sends — **Online**,
**Stale**, **Not heard yet**, **Offline** or **Invalid**.

**What it changes:** **nothing. *This view only reads; it sends no commands.***
Configured MQTT control devices may still be controlled by the EMS runtime —
that is the runtime's job.

**Expected result:** brokers **Connected**, devices **Online** with a recent
time and their values.

**If it differs:** *No MQTT telemetry is set up.* means no broker and no MQTT
device is configured. A broker's own problem is named under it, for example
*This broker has no usable credential*. Devices with `0` values or an old time
are not publishing what EMS expects.

## Zendure Cloud MQTT

### 1 — Save the API key

**Where:** **What the search may use** → **Zendure cloud API key**.

**What you enter:** your Zendure API key, then **Save key**.

**What it changes:** the key is saved **encrypted** on the device. It is never
shown back to you. **Test** checks it — *The key works: 2 devices via …* — and
**Forget key** removes it.

### 2 — Find the cloud devices

**What you select:** **Find cloud devices**, or **Search my network**, which
includes the cloud once a key is saved.

**What it changes:** the console signs in to Zendure's device list with your
key and lists your devices. This is a read step.

**Expected result:** your Zendure devices appear in the search, with **Add
inverter**, or **Use this connection** for one you already have over another
connection.

### 3 — Save a cloud device

**What it changes:** this is where the credential is provisioned. Saving
fetches fresh broker credentials with your API key, stores them encrypted, and
**checks that EMS can resolve them before the settings are written**.

**Expected result:** the broker connects over TLS and telemetry arrives.

**If it differs:** if that credential record is later missing, the broker
reports that it has no usable credential and is **never connected** — it fails
closed rather than retrying blind.

### What to expect from the cloud path

- **Internet dependency.** No connectivity, no cloud telemetry and no cloud
  writes.
- **Higher latency than Local API or Local MQTT.** See the measured figures in
  [Connection types](../connection-types.md#what-cloud-latency-means-in-daily-use).
  Use `system.loop_interval = 5` seconds with Zendure Cloud MQTT.
- **A published command is not a successful command.** EMS reports a command
  effective only after **telemetry confirmation**; publishing is only a
  transport step. See [Dashboard control](../dashboard/control.md).
- **Run only one controller.** Disable Zendure HEMS, Smart Matching and Zendure
  schedules when EMS controls a device over the cloud broker.

> The cloud broker is bidirectional only with the Zendure App / Home Assistant
> authorization credentials returned by the device-list login. The public
> read-only developer MQTT account can subscribe to telemetry, but its writes
> are silently discarded.

## Multiple brokers, and mixed operation

You can run several brokers at once, and mix transports freely:

- Each MQTT device names exactly one **broker**, so two brokers never compete
  for the same device.
- One inverter on Local API and another on Zendure MQTT is a supported, normal
  setup. They join the same EMS control loop.
- A single physical inverter reachable over several transports stays **one**
  logical device. See
  [Device management](device-management.md#one-inverter-several-connections).

The **Zendure MQTT broker** card on the **Devices** tab shows the single local
broker of an older installation, editable, or — for an installation with named
broker profiles — *Managed by the setup (named broker profiles)*, read only.

## MQTT grid meters

A Zendure D0 found on a local MQTT broker offers **Use as grid meter**.
Choosing it maps the D0's `Zendure/sensor/<serial>/totalPower` topic to the
central grid meter, reusing the broker's profile: *EMS reads the grid from the
MQTT topic … It is only read, never written.*

Only one grid meter can be active, and an existing one is replaced only when
you confirm *Replace the grid meter?*. A Zendure D0 or Smart Meter 3CT found
over local HTTP is the simpler choice when you have it — no MQTT setup at all.

## Write safety gates

Whether EMS may write to hardware is decided by **named gates**, one per
transport, on the **Safety** tab under **What EMS may change**. All three
default to on, and each can be switched off for read-only validation.

| Transport | Switch | Setting |
| --- | --- | --- |
| Local API / local HTTP | **Allow hardware writes** | `allow_hardware_writes` |
| Local MQTT broker | **Allow local MQTT control writes** | `allow_mqtt_local_control_writes` |
| Zendure cloud MQTT | **Allow Zendure cloud MQTT control writes** | `allow_mqtt_zendure_control_writes` |

Every runtime write additionally requires dry run and simulation mode off, and
not being a replay. The Overview's **Control & safety** card shows each
connection as *allowed · 2 devices*, *allowed · no device uses it* or *not
allowed*.

A gate being on is **not** enough on its own. Output control over MQTT needs
the whole chain:

- an **exact supported hardware model**, resolved from verified device
  evidence;
- a **compatible broker transport** for that model;
- a **verified write protocol** for it;
- an **enabled control capability** (the per-device `write_output_limit`
  opt-in);
- an **enabled transport write gate**.

**Topic family and generation are evidence and telemetry grouping only** — they
never authorize a write. **Unknown or conflicting model evidence** keeps a
device reading values only. Broker source is part of the decision too: the
device card reads *output control is not verified for this MQTT broker* where
the broker does not carry the write route, while the **Zendure cloud broker**
keeps control for a supported model.

Full model: [Safety](../safety.md) ·
[Safety model (technical)](../../technical/safety-model.md).

## State reconciliation is still API-only

MQTT does not yet replace every Local API function. State-reconciliation
writes — `minSoc`, `socSet`, `smartMode`, `gridOffMode`, full-charge assist —
additionally require `allow_state_reconciliation_writes=true` (**Repair device
state on startup**) and are **API-only**. MQTT control devices take the power
command only: output, and a charge from surplus where the model allows it.

If you need those features, keep at least one API path to the device.

## Zendure MQTT migration during an upgrade

A [Guided Upgrade](guided-upgrade.md) reviews the Zendure MQTT migration
before it applies anything. When a migration is needed, the checked update
says so — *The MQTT setup of 1 device is migrated to the new format.* — and
names how many devices **lose their output control**. Read that before you
start; the migration is applied through the EMS-owned migration service, in the
update's own order.

The **System** page shows the same review on its own card, **Older MQTT device
setup**, while one waits: each device with its exact model and whether control
is kept (**Control kept**) or switched off (**Control off, readings kept**),
whether the result would be valid, **Back up my settings first**, and **Apply
the migration**. A review that went out of date is read again before anything
is written.

## Limitations, honestly

- **Cloud confirmation is delayed.** Measured on a physical SolarFlow 800 Pro
  2: 165 of 165 commands confirmed, typically ~2.9 s, slowest 3.4 s. Measured
  values, not a guarantee.
- **Some legacy generations are partly reverse-engineered.** The legacy Zendure
  JSON write method is validated in the test harness; the older non-modern-API
  generations (Hub / Hyper / AIO / Ace) have **not** been confirmed on real
  hardware.
- **Not every model is validated on physical hardware.** See
  [Supported setups](../supported-setups.md) for the four support tiers and the
  evidence behind each row.
- **Unknown or conflicting models only read values** by design.

Positive reports are just as valuable as problem reports — they let a device be
marked validated for everyone. Open a
[Device compatibility report](https://github.com/basecubedev/ems-solarflow-api-control/issues/new?template=device_compatibility_report.yml)
with model, firmware, connection type and **sanitized** logs. Remove serial
numbers, tokens, passwords and public IPs first.

## Warnings and common problems

| Symptom | Meaning | What to do |
| --- | --- | --- |
| Broker **Off** or **Not connected** | Profile switched off or unreachable | Check host, port, credentials, TLS |
| *This broker has no usable credential* | The encrypted credential record is gone | Save the cloud device again to re-provision it |
| Device **Not heard yet** or an old time | It is not publishing to that broker | Re-point the device; EMS never redirects it for you |
| Device only reads values | The authorization chain is incomplete | [Why a device is read-only](device-management.md#why-a-device-is-read-only) |
| *The MQTT device ID is missing* | The write route is incomplete | Enter the device ID (and the product key) |
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
