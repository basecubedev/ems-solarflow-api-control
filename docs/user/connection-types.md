# Connection Types

EMS can reach your Zendure devices over three connection types. Which ones you
can use depends on your hardware — this page helps you pick the right one and
shows how to integrate it. All three converge on the same standard
`config/config.json` layout, so you can start with the easiest option and switch
later.

For the full model list and grid-meter support see
[supported-setups.md](supported-setups.md). For exact config keys see
[configuration.md](../technical/configuration.md).

## At a glance

| Connection | Works with | Latency | Setup effort |
| --- | --- | --- | --- |
| **Local API (ZenSDK)** | Newer SolarFlow / ZenSDK models with the local HTTP API | Lowest — fully local | Low (device on your LAN) |
| **Local MQTT** | Zendure devices re-pointed to your own local broker | Low — local network | Medium (manual broker redirect) |
| **Zendure MQTT (cloud)** | Any Zendure device, via your Zendure API key | Higher — runs over the internet | Low — save the API key; apply provisions the runtime credential automatically |

All three are control transports: a supported inverter is regulated by the same
EMS control loop over any of them (output control over MQTT is enabled for an
exact supported model with a verified write protocol, never from the topic family
alone). You can also combine them: a device on
the **Local API** can *additionally* be given **Zendure MQTT** as a telemetry
source, and MQTT itself can be either **local** or the **Zendure cloud** broker.

## Local API (ZenSDK)

The recommended and fastest path. Newer Zendure SolarFlow / ZenSDK-compatible
models expose a local HTTP API on your network. EMS talks to them directly, with
no cloud in the loop. It is the recommended control path (writing `outputLimit`)
and the only transport that also does full device-state reconciliation (Smart
Mode, AC Mode, SoC limits).

- **Use this when** your device is one of the ZenSDK-compatible SolarFlow models
  and the local API is reachable on your LAN.
- **Devices can add Zendure MQTT on top** for extra telemetry — the control path
  stays on the local API.

Details: [supported-setups.md](supported-setups.md) ·
[configuration.md](../technical/configuration.md)

## Local MQTT

For Zendure devices that talk MQTT on your own network. Because Zendure hardware
normally connects to Zendure's cloud broker, you have to **manually re-point the
device to your one local broker** first — after that EMS reads it locally with
low latency and no cloud dependency.

- **Use this when** you already run a local MQTT broker (e.g. Mosquitto) and want
  telemetry to stay on your network.
- **You must redirect the device** to that single local broker yourself. Several
  community projects on GitHub help with re-pointing Zendure hardware to a local
  broker and decoding its topics — search for *Zendure local MQTT* or the various
  *Zendure Home Assistant* integrations, and use one that matches your device
  generation.
- EMS then consumes the local broker as a telemetry source (see the Local MQTT
  discovery in the Admin Console).

> **Status:** telemetry is fully supported, and a supported inverter can be
> **controlled** over a local MQTT broker using the same EMS control loop as the
> local API — a per-device `write_output_limit` opt-in behind the
> `allow_mqtt_local_control_writes` gate (on by default, can be switched off for
> read-only validation). The legacy Zendure JSON write method is validated in the
> test harness; the older, non-modern-API generations (Hub / Hyper / AIO / Ace)
> have not yet been confirmed on real hardware.
> [Reports welcome](#help-improve-compatibility).

Details: [Local MQTT discovery](../technical/admin-discovery.md#local-mqtt-discovery)
· [configuration.md](../technical/configuration.md#zendure-mqtt-telemetry-and-control)

## Zendure MQTT (cloud)

The quickest way to just try the software with **any** Zendure device, whatever
its generation. You log in to the Zendure **cloud** broker with your **Zendure
API key** and reach the devices through Zendure's own infrastructure — no local
API, no broker redirect, no reflashing.

- **Use this when** you want to explore what the software can reach for a device
  that has no usable local API / local MQTT path.
- You save your **Zendure API key** in the Admin Console (stored encrypted). It
  is used to reach the cloud broker (TLS-only) and list your devices.
- **Latency note:** because this connection runs over an online link through
  Zendure's cloud infrastructure, it is **slower and less predictable than a
  local connection**. In one 10-minute SolarFlow 800 Pro 2 measurement, the
  inverter confirmed 165 of 165 commands: typically after about 2.9 seconds,
  with a slowest result of 3.4 seconds. These are measured values, not a
  guaranteed cloud response time. For responsive, fully local control prefer
  the Local API or a Local MQTT setup where your hardware supports it.

### What cloud latency means in daily use

The EMS loop and the cloud transfer happen one after the other:

```text
load changes → wait for next EMS loop → send through Zendure Cloud → device confirms
```

Once EMS sent a command, the tested inverter usually confirmed it in about
2.9 seconds. Before sending, a load change may wait up to one configured loop
interval. With the recommended **5-second loop**, confirmation therefore
arrived about 2.9 to 7.9 seconds after a load change under the measured
conditions, before any additional filtering or ramping; the inverter may start
applying the value earlier than the confirmation arrives.

Use **5 seconds** for `system.loop_interval` with Zendure Cloud MQTT. A 3-second
loop is possible, but the measured p95 was already 3.01 seconds, leaving no
useful margin when the cloud is briefly slower. This setting does not make the
cloud itself faster; it prevents a new normal regulation cycle from immediately
overlapping the previous command's usual confirmation window. See the
[technical measurement](../technical/zendure-mqtt-power-control.md#live-cloud-latency-measurement)
for the exact test conditions and statistics, and the
[configuration reference](../technical/configuration.md#system-settings) for
the authoritative setting guidance.

> **Status: supported for telemetry; output control requires an exact supported
> model.** Applying a selected cloud device provisions the runtime MQTT
> credential automatically: the Apply step fetches fresh broker credentials
> with your API key, stores them encrypted in the external secret store and
> verifies that EMS can resolve them before the config is committed (see
> [configuration.md](../technical/configuration.md#zendure-mqtt-telemetry-and-control)).
> If that record is later missing, the broker reports `broker_auth_missing`
> and is never connected. Output **control** through the cloud broker requires
> the full authorization chain: an **exact supported hardware model** (resolved
> from verified device evidence), a **verified write protocol** for that model,
> a per-device `write_output_limit` opt-in, and the
> `allow_mqtt_zendure_control_writes` gate (on by default, can be switched off).
> **Topic family or hardware generation alone never authorizes output control**,
> and unknown or conflicting model evidence stays telemetry-only. The cloud
> broker is **bidirectional only with the Zendure App / Home Assistant
> authorization credentials** returned by the device-list login — the public
> read-only developer MQTT account can subscribe to telemetry but its writes
> are silently discarded. ZenSDK cloud commands are the atomic
> mode+power property set at QoS 1 on the `iot/…` topic; a publish is only a
> transport step, and EMS reports a command effective only after telemetry
> confirmation (see
> [zendure-mqtt-power-control.md](../technical/zendure-mqtt-power-control.md)).
> **Run only one controller:** disable Zendure HEMS, Smart Matching and
> Zendure schedules when EMS controls a device over the cloud broker. The
> corrected cloud command path is protocol-verified against reference
> implementations, captured hardware traffic and a real-broker test harness,
> and was live-validated on a physical SolarFlow 800 Pro 2 on 2026-08-01
> (165/165 commands telemetry-confirmed). Prefer a local transport for live
> control (lower latency) — [reports welcome](#help-improve-compatibility).

Details: [Zendure MQTT discovery (cloud)](../technical/admin-discovery.md#zendure-mqtt-discovery-cloud)
· [configuration.md](../technical/configuration.md#zendure-mqtt-telemetry-and-control)

## Which one should I pick?

- **Newer SolarFlow / ZenSDK device on your LAN** → **Local API** (add Zendure
  MQTT telemetry if you like).
- **Want everything local and comfortable redirecting the device** → **Local
  MQTT** with a community project.
- **Just want to try it, or an older device** → **Zendure MQTT (cloud)** with
  your API key, accepting the higher latency.

Before enabling live writes to your hardware, read the
[safety guide](safety.md).

## Help improve compatibility

The maintainer does not own hardware from every Zendure generation, so parts of
the MQTT support could not be validated on real devices yet. Feedback is very
welcome — for **both** working and problematic devices:

- Open a
  [Device compatibility report](https://github.com/basecubedev/ems-solarflow-api-control/issues/new?template=device_compatibility_report.yml)
  and include the device model, firmware version, connection type and sanitized
  logs. **Remove serial numbers, tokens, passwords and public IPs first.**
- Positive reports ("works fine") are just as useful — they let us mark a device
  as validated for everyone else.

See [supported-setups.md](supported-setups.md) for the full compatibility table.
