# Supported Setups

Use this page to check whether your setup is likely to fit EMS before you start
editing config. It is a user-facing compatibility overview, not the full config
reference. For exact keys and values see
[configuration.md](../technical/configuration.md) and, for Admin Console
discovery, [admin-discovery.md](../technical/admin-discovery.md).

## Recommended Setup

- Docker Compose (Admin Console is the recommended path)
- at least one supported Zendure connection — **Local API**, **Local MQTT**, or
  **Zendure cloud MQTT** — with the credentials or network identifiers that
  transport needs (Local API is recommended for ZenSDK models because it is
  local and low-latency)
- one supported grid meter
- EMS is the only controller writing Zendure `outputLimit`
- dashboard reachable only on your trusted local network

## Device support status

Each device below carries one status. It describes **how much confidence we have
that control/telemetry works on that exact hardware** — not how good the device
is:

- **Validated** — confirmed working on real hardware. Every Validated entry is
  listed under [Validated devices](#validated-devices) with the connection(s)
  tested and who confirmed it.
- **Family-supported** — shares the exact API/reader protocol of a Validated
  device (same ZenSDK line, same meter reader). Expected to work, but not
  individually confirmed.
- **Reverse-engineered** — the write/telemetry protocol was implemented from
  external community projects or public/vendor documentation and has **not**
  been confirmed on real hardware by anyone.
- **User-reported** — a user reported it working. Once corroborated it can move
  up to Validated.

The maintainer tests on a **SolarFlow 800 Pro 2** and a **Shelly Pro** grid
meter, so only that hardware is Validated first-hand. Everything else relies on
the shared ZenSDK protocol or on community help — please [report your results](#help-improve-compatibility)
so more devices can move to Validated.

## Validated devices

Confirmed working on real hardware. This is the ledger the statuses above point
at — add your device by opening a
[device compatibility report](#help-improve-compatibility).

Every row is backed by a concrete evidence statement. Devices tested first-hand
by the maintainer are listed as **Maintainer hardware**; a device only earns a
row here once a real hardware test is recorded, so the Zendure smart meters —
whose shared local-HTTP reader is exercised only by the automated test harness —
are not listed until a physical report confirms them.

| Device | Connection(s) tested | Validated by | Evidence |
|---|---|---|---|
| SolarFlow 800 Pro 2 | Local API + Zendure cloud MQTT | Maintainer | Maintainer hardware |
| Shelly Pro (grid meter) | Local HTTP (RPC status) | Maintainer | Maintainer hardware |

## Supported Zendure Devices (Local API / ZenSDK)

EMS controls Zendure SolarFlow devices through the local Zendure API /
ZenSDK-compatible HTTP API. Known ZenSDK-compatible models:

| Model | Status | Notes |
|---|---|---|
| SolarFlow 800 Pro 2 | Validated | Maintainer hardware; ZenSDK local HTTP control **and** Zendure cloud MQTT control confirmed. |
| SolarFlow 800 | Family-supported | Same ZenSDK local HTTP protocol as the 800 Pro 2. |
| SolarFlow 800 Plus | Family-supported | Same ZenSDK local HTTP protocol as the 800 Pro 2. |
| SolarFlow 800 Pro | Family-supported | Same ZenSDK local HTTP protocol as the 800 Pro 2. |
| SolarFlow 1600 AC+ | Family-supported | Same ZenSDK local HTTP protocol as the 800 Pro 2. |
| SolarFlow 2400 AC | Family-supported | Same ZenSDK local HTTP protocol as the 800 Pro 2. |
| SolarFlow 2400 AC+ | Family-supported | Same ZenSDK local HTTP protocol as the 800 Pro 2. |
| SolarFlow 2400 Pro | Family-supported | Same ZenSDK local HTTP protocol as the 800 Pro 2. |
| SolarFlow 4000 AC+ | Family-supported | Same ZenSDK family; not individually confirmed. |

Each Zendure device configured over the **Local API** needs a local IP address,
serial number, max power, battery size, PV size / priority metadata, and min/max
SOC limits. Devices reached over **Local MQTT** or **Zendure cloud MQTT** are
identified by their broker profile, product key and device/serial instead of an
IP address.

Avoid assuming support for a model only from the name. Use tested/known setups
where possible, and open an issue with diagnostics if your device payloads look
different from the supported local API behavior.

## Zendure MQTT device generations

Devices can be connected over MQTT — either through your own local broker or the
Zendure cloud broker (see [connection-types.md](connection-types.md)) — for
telemetry and for output **control**. MQTT output control is supported where a
verified write protocol exists, and it is a first-class EMS transport: a
supported inverter joins the same control loop, target calculation,
distribution and safety gates as a local API device.

Control eligibility requires all five conditions:

1. an **exact supported hardware model** resolved from verified device evidence;
2. a **compatible broker transport**;
3. a **verified write protocol** for that exact model;
4. an **enabled control capability** (`write_output_limit`) on the device; and
5. the matching **enabled transport write gate**
   (`allow_mqtt_local_control_writes` or
   `allow_mqtt_zendure_control_writes`).

Topic family and generation are evidence and telemetry grouping only. Observing
a legacy JSON layout does not by itself enable writes or prove the hardware
model. Unknown or conflicting model evidence keeps the device telemetry-only
until you review and correct the model. ZenSDK scalar MQTT topics remain
telemetry-only until a verified writable output topic is available for the exact
model. Transport write gates are on by default and can be switched off for
read-only validation.

The **SolarFlow 800 Pro 2** MQTT output-control path is **Validated** on the
maintainer's own hardware over the Zendure cloud broker; the other ZenSDK models
share its exact write profile (Family-supported). The older Hub/Hyper generations
carry a **Reverse-engineered** write profile that has not been validated on
physical hardware. Please report results (see below).

| Generation | Example models | Connection | Status |
|---|---|---|---|
| New SolarFlow / ZenSDK | SolarFlow 800 Pro 2 (Validated); 800 / 800 Plus / 800 Pro / 1600 AC+ / 2400 AC / AC+ / Pro / 4000 AC+ (Family-supported) | Local API (preferred), plus output **control** over Zendure cloud MQTT | ZenSDK output control uses the exact-model `zensdk_properties_write` profile over the cloud/JSON-report transport; **Validated on the maintainer's 800 Pro 2** (Local API + cloud MQTT), other ZenSDK models Family-supported. Scalar HA MQTT topics stay telemetry-only. |
| Older Hub / Hyper | Hub 1200, Hub 2000, Hyper 2000, AIO 2400, Ace 1500, SuperBase V | MQTT control (local or cloud) | Reverse-engineered: exact listed models carry a legacy JSON write profile derived from external projects; control still requires capability and transport gates; not confirmed on maintainer-owned physical hardware (Ace 1500 / SuperBase are telemetry-only). |
| Any device via API key | Any Zendure device | Zendure cloud MQTT | Telemetry supported; Apply provisions the runtime broker credential; control requires an exact supported model and every control-eligibility condition above. |

## Help improve compatibility

The MQTT paths above need real-device reports. Please open a
[Device compatibility report](https://github.com/basecubedev/ems-solarflow-api-control/issues/new?template=device_compatibility_report.yml)
for **both** working and problematic devices — include the device model,
firmware version, connection type and sanitized logs (remove serial numbers,
tokens, passwords and public IPs first). Positive reports let us mark a device as
validated for everyone else.

## Supported Grid Meters

| Type | Config value | Status | Notes |
|---|---|---|---|
| Shelly Pro | `shelly` | Validated | Uses the RPC status endpoint; maintainer hardware. |
| Shelly Plus / Gen2 / Gen3 | `shelly` | Family-supported | Same RPC status reader as the Shelly Pro. |
| Shelly 3EM Gen1 | `shelly_3em_gen1` | Reverse-engineered | Uses the classic `/status` endpoint; implemented from the Gen1 protocol, not confirmed on maintainer hardware. |
| EcoTracker | `ecotracker` | Reverse-engineered | Uses the local API path implemented by EMS; not confirmed on maintainer hardware. |
| Zendure Grid Meter via local HTTP | `zendure_grid_meter_http` | Reverse-engineered | Internal/discovery generic type. Reads `total_power` from the local `/properties/report` REST endpoint; no MQTT required. Works for both a Zendure D0 and a Smart Meter 3CT — they expose the same flat report. The shared reader is exercised by the automated test harness but not confirmed on maintainer hardware. Manual setup offers the concrete 3CT and D0 local-API entries below instead. |
| Zendure Smart Meter 3CT — Local API | `zendure_smartmeter_3ct_http` | Reverse-engineered | **Simplest connection for a 3CT** (local HTTP, no MQTT). Reads `total_power` from the local `/properties/report` endpoint; needs only `ip`. Shares the Zendure local-HTTP reader with the D0 local-API meter; not yet confirmed on maintainer hardware. |
| Zendure Smart Meter D0 — Local API | `zendure_smartmeter_d0_http` | Reverse-engineered | **Simplest connection for a D0** (local HTTP, no MQTT). Reads `total_power` from the local `/properties/report` endpoint; needs only `ip`. Shares the same Zendure local-HTTP reader as the 3CT — a manually added D0 is a distinct type and is never stored or shown as a 3CT; not yet confirmed on maintainer hardware. |
| Tasmota HTTP / SmartMeter reader | `tasmota_http` | Reverse-engineered | Requires URL/IP and `power_path`. Generic config-driven reader; not confirmed on maintainer hardware. |
| Zendure Smart Meter D0 — Local MQTT | `zendure_smartmeter_d0` | Reverse-engineered | Optional alternative for a D0 already connected to a local broker. Subscribes to `Zendure/sensor/<SERIAL>/totalPower`; positive import, negative export. MQTT transport not confirmed on maintainer hardware. |
| Generic MQTT grid meter | `mqtt` | Reverse-engineered | Requires an existing broker, one topic, and a numeric or JSON power payload. Generic config-driven reader. |

Local HTTP is the recommended Zendure D0/3CT grid-meter connection: both models
provide `total_power` through `/properties/report`, so numeric `total_power`
alone makes the meter usable — no MQTT setup required. The Admin may label such a
meter **"Zendure Grid Meter via local HTTP"** without claiming a model when the
payload does not identify one reliably. (`meterType=2` / `protocolType=72` are
**not** treated as universally proven D0 identifiers, and the three
`a_/b_/c_aprt_power` phase fields are **not** reliable 3CT evidence — a D0 also
reports them.)

MQTT is an **optional alternative** for a D0 already connected to a local broker.
When both connection methods are available, EMS presents local HTTP as
recommended and MQTT as the alternative, and only **one** central grid meter may
be active. A D0 grid meter discovered on a broker reuses the named broker profile
via `broker_ref` (see [configuration](../technical/configuration.md)); the broker password
is never duplicated into the `grid_meter` block. MQTT here is a **grid meter
input** only — a read-only load signal. EMS never publishes over the grid-meter
MQTT client and does not control Zendure devices or inverters through it.

## Optional Integrations

- Home Assistant is optional.
- InfluxDB analytics is optional.
- Native Python is supported for advanced/manual installs.

At least one supported Zendure connection — Local API, Local MQTT, or Zendure
cloud MQTT — must be available for EMS control. The Local API is recommended for
ZenSDK models because it is local, low-latency, and the only transport that also
does full device-state reconciliation. Do not run Zendure HEMS, Home Assistant
automations, MQTT writers, or any other controller in parallel if they write
Zendure `outputLimit`. EMS assumes exclusive write control over `outputLimit`
while active.

## Roadmap

- Broadening physical-hardware validation for MQTT output control. The control
  path is a normal per-device opt-in (see the generation table above); the
  roadmap is about confirming it on more Zendure firmware generations, not
  adding the feature.

## Not Supported / Not Yet Verified

- MQTT output control on the **Reverse-engineered** older Hub/Hyper generations,
  which the maintainer has not been able to confirm on physical hardware (the
  ZenSDK cloud-MQTT path is Validated on the 800 Pro 2). Only an exact supported
  model with a compatible transport, verified write protocol, per-device
  `write_output_limit` capability and enabled transport write gate can publish.
  Please report both successful and failed hardware tests (see below).
- Older MQTT-only Zendure devices without the local Zendure API / ZenSDK.
- Any Zendure model not listed above, unless your own tested setup confirms the
  local API behavior.
- Running EMS alongside another controller that writes Zendure `outputLimit`.

## Not Recommended

- using placeholder IPs or `YOUR_SN`
- starting unattended before diagnose and first-run validation
- assuming InfluxDB is required for the dashboard
- exposing the dashboard publicly without a reverse proxy/auth design

## Next step

- Most users: [Admin Console](admin-console.md)
- Shell-only install: [Docker Bootstrap](docker-bootstrap.md)
- Development or source checkout: [Developer Setup](../developer/developer-setup.md)
