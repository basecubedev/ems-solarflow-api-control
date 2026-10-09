# External devices over MQTT

## Purpose

Show and count an inverter or a battery that EMS cannot control: hardware with no
local API whose values something else — FHEM, ioBroker, Node-RED or a small
script — reads and publishes on your MQTT broker. EMS reads those values, shows
the device on the dashboard and counts it in the totals and the energy
statistics. It never sends the device anything.

The exact contract is in the
[configuration reference](../technical/configuration.md#external-devices-over-mqtt-external_mqtt);
this page shows how to set it up.

## When to use this

- A second inverter on your house, for example a rooftop system with only a web
  page, should appear in the dashboard and in the house load.
- A battery from another manufacturer should be visible beside your Zendure
  devices.

It does **not** make that device controllable. EMS regulates its own Zendure
devices against the grid meter; an external device only changes what the meter
sees, like any other appliance. If an external battery regulates itself to zero
against the same meter, two controllers fight over one measuring point — run it
on a schedule or against a meter of its own.

## Prerequisites

- A local MQTT broker that EMS can reach (the same kind of broker a Local MQTT
  Zendure device uses — see [Connection types](connection-types.md)).
- Something that reads the device and can publish to that broker.

## What to publish

Every topic starts with `ems-solarflow`, then a device id you choose, then a key:

| Topic | Value | |
| --- | --- | --- |
| `ems-solarflow/<id>/inverterPower` | AC power the device delivers to the house, watts, `0` or more | **Required** |
| `ems-solarflow/<id>/solarPower` | PV power at the device's input, watts, `0` or more | Optional |
| `ems-solarflow/<id>/batteryPower` | Battery power in watts: **positive while charging, negative while discharging** | Optional |
| `ems-solarflow/<id>/batterySoc` | Battery state of charge, `0` to `100` | Optional |
| `ems-solarflow/<id>/state` | All of the above at once, as one JSON object | Alternative |

Rules that decide whether a value is read:

- **Device id:** 1 to 64 letters, digits, `-` or `_`, for example
  `garage-inverter` or `WR_2`. No spaces, no dots.
- **Exact spelling:** topics and keys are case-sensitive — `InverterPower` or
  `EMS-SolarFlow` is not read.
- **A plain number:** `765`, `765.5`, `-55`. Not `765 W`, not `765,0`.
- **Send `0`, not a small negative standby value:** a negative `inverterPower`
  or `solarPower` is dropped.
- **Battery sign:** many systems count discharging as positive. Negate such a
  value before publishing it.
- **No battery, no SoC:** leave `batterySoc` out and the device counts as having
  no battery; its card shows no charge level.
- **Publish retained, at least every 30 seconds, also when nothing changed.**
  Discovery listens for a few seconds only and finds a device by its retained
  values. A device that has sent nothing readable for a minute shows as stale,
  and the energy statistics pause until it reports again — a bridge that only
  publishes on change falls silent at night.

### One value per topic

```bash
mosquitto_pub -h 192.168.1.71 -r -t ems-solarflow/garage-inverter/inverterPower -m 765
mosquitto_pub -h 192.168.1.71 -r -t ems-solarflow/garage-inverter/batterySoc -m 42
```

### Everything at once: `state`

The payload is one JSON object with the same keys. Each key is optional, and the
device is found once `inverterPower` has arrived:

```bash
mosquitto_pub -h 192.168.1.71 -r -t ems-solarflow/garage-inverter/state \
  -m '{"inverterPower": 765, "solarPower": 820, "batteryPower": -55, "batterySoc": 42}'
```

| Payload | Read? |
| --- | --- |
| `{"inverterPower": 765, "solarPower": 820, "batteryPower": -55, "batterySoc": 42}` | Yes, all four |
| `{"inverterPower": 765}` | Yes |
| `{"inverterPower": "765", "batterySoc": 42.5, "firmware": "1.2"}` | Yes; `firmware` is ignored |
| `{"inverterPower": -5, "batterySoc": 42}` | Only `batterySoc` |
| `{"InverterPower": 765}` | No — wrong spelling |
| `{"inverterPower": "765 W"}` | No — a unit in the value |
| `[765]` or `765` | No — not a JSON object |

Both forms can be mixed; each key keeps its latest value.

### Node-RED

Put a **function** node behind the node that reads your device, followed by an
**mqtt out** node for your broker. Leave the mqtt out node's *Topic* empty and its
*Retain* setting empty or `true`. The function, assuming your source delivers
`ac`, `pv`, `battery` (positive while discharging) and `soc`:

```javascript
const source = msg.payload;
return {
    topic: "ems-solarflow/garage-inverter/state",
    retain: true,
    payload: {
        inverterPower: Math.max(0, Math.round(source.ac)),
        solarPower: Math.max(0, Math.round(source.pv)),
        batteryPower: -Math.round(source.battery),
        batterySoc: Math.round(source.soc),
    },
};
```

The mqtt out node sends the object as JSON.

### A Python script

With [paho-mqtt](https://pypi.org/project/paho-mqtt/) 2.x; replace
`read_values` with whatever reads your device:

```python
import json
import time

import paho.mqtt.client as mqtt

BROKER = "192.168.1.71"
TOPIC = "ems-solarflow/garage-inverter/state"


def read_values():
    return {"inverterPower": 765, "solarPower": 820, "batteryPower": -55, "batterySoc": 42}


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.connect(BROKER, 1883)
client.loop_start()
while True:
    client.publish(TOPIC, json.dumps(read_values()), qos=1, retain=True)
    time.sleep(15)
```

### FHEM and ioBroker

Any of their MQTT modules that can publish a reading to a topic works: publish
each value to the topic above, or build the `state` object, with the retain flag
set and on a timer rather than only on change.

## Adding the device in Admin

1. Start publishing, then open **Guided Setup** (new installation) or
   **Maintenance** (existing one) and run the discovery for your broker.
2. The device appears as **External device `<id>`**, marked *takes no commands*.
   Choose **Add inverter**.
3. Give it a name if you like, then preview and apply.

In Maintenance the device then shows as **External device · read over MQTT**. You
can rename it, switch it off, move it to another broker with **Use connection**
or remove it; everything else follows from the topics.

## Expected result

The dashboard shows a card with a **Telemetry only** badge and no **Target**. Its
output counts in the house load, its PV in the PV total, its battery in the
battery total and — if it reports one — its SoC in the average.

## Warnings and common problems

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Discovery finds nothing | No `inverterPower` yet, values not retained, a spelling mistake, or a dot or space in the id | Check the topic letter by letter; publish retained |
| Card shows **Offline** | Nothing readable for about a minute | Publish at least every 30 seconds, also unchanged values |
| A value never changes | The payload is not a plain number | Remove units, use a dot as decimal separator |
| Battery shows charging while discharging | The source counts the other way round | Negate `batteryPower` |
| A removed device is still offered | Its retained topics are still on the broker | Clear each: `mosquitto_pub -h 192.168.1.71 -r -n -t ems-solarflow/<id>/state` |
| Apply refused with `external_mqtt_family_unknown` | An entry from a development build that read `KostalPiko/<serial>/solarPower` | Remove it in Maintenance, publish the reading as `inverterPower` and add the device again |

## Recovery or next steps

- Full contract and error codes:
  [configuration reference](../technical/configuration.md#external-devices-over-mqtt-external_mqtt).
- What is supported: [Supported setups](supported-setups.md#external-devices-read-only).
- Device cards: [Device cards](dashboard/devices.md).
