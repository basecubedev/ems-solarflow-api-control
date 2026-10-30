# Built-in MQTT broker and MQTT mapping

## Purpose

Bring a device that speaks MQTT in a format EMS does not know — a Shelly that
measures a balcony inverter, a Tasmota plug, an OpenDTU — into EMS as an
**external device**, without writing a bridge yourself. Two parts do it:

- the **built-in MQTT broker**, a Mosquitto the Admin Console starts beside EMS
  when you switch it on;
- the **MQTT mapping**, a small service that reads the topics you choose, turns
  their values into the [external-device standard](../external-devices.md)
  and publishes them as `ems-solarflow/<id>/state`.

EMS then finds the device under **Find devices** like any bridge's output. It
only reads: an external device counts in the dashboard, the house load and the
statistics, and the regulation keeps working against the grid meter. Nothing
reaches EMS until you save a mapping.

## Prerequisites

- The Admin Console. Only the Admin switches the broker; the dashboard and
  `emsctl diagnose` only show its state.
- A fixed address for this host in your router: every device finds the broker
  by it.
- Docker Hub reachable the first time you switch the broker on (22 MB).

The mapping also works on a broker you already run (ioBroker, a Mosquitto of
your own): choose that local MQTT profile instead of the built-in broker.

## Switch the broker on

**Settings → Add-ons → Built-in MQTT broker → Switch on.** In Guided Setup the
same card is in step 02, so the broker runs before you map devices.

![Settings, Add-ons: the built-in MQTT broker running at 192.168.178.40:1883 with anonymous access off and the accounts devices and zendure](../../assets/screenshots/admin/admin-mqtt-broker.png)

The broker is off by default. The first time, a shared account `devices` with a
generated password is created and anonymous access stays off. **Show password**
shows it again whenever you need it for the next device.

| Setting | Effect |
| --- | --- |
| Anonymous access on, no accounts | an open home broker |
| Anonymous access on, with accounts | both at once |
| Anonymous access off, one account | one shared account for every device |
| Anonymous access off, several accounts | per device or per vendor, optionally limited to a topic prefix |

**What the regulation reads stays protected in every mode.** `ems-solarflow/`
(the mapping's outputs), the Zendure topics (`iot/`, `Zendure/`) and the topic
of an MQTT grid meter on this broker cannot be read or written by anonymous
clients or by accounts without the matching option:

- **May write ems-solarflow/ (bridge)** — for Node-RED or ioBroker that already
  publish the standard themselves;
- **Zendure devices** — for a Zendure device you point at this broker;
- the grid meter account chosen under the accounts.

**Stopping is always possible.** The dialog names what changes: external
devices are shown as stale after 60 s, a Zendure device on this broker is no
longer regulated and keeps its last setpoint, an MQTT grid meter on it fails.
Accounts, mappings and the image stay; switching on again needs no download.

## Connect a device

Enter the address shown on the card, the port and an account in the device's
own MQTT settings. For a Shelly Gen2, switch MQTT on, enter server, user and
password, and switch on the status updates it sends over MQTT.

## Map it

**Settings → Devices → MQTT mapping** (or Guided Setup step 02):

1. Choose the broker and **Show live topics**. Everything the broker carries
   appears, grouped by device. Shelly, Tasmota, OpenDTU and AhoyDTU are
   recognised and named; unknown topics are grouped by their topic tree.
   Devices EMS reads directly (Zendure) and the mapping's own outputs are shown
   but not offered.
2. **Map as external device** on a group, or **New external device**.
3. For each value — inverter power (required), solar power, battery power,
   state of charge — **Use as** a number from the live topics, or enter topic
   and JSON path by hand. Several sources add up; battery power is one signed
   value, or charge and discharge as two.
4. Per source: factor (for example 1000 for kW), invert the sign (a Shelly
   measures feed-in as negative), negative as 0 (standby noise) and the
   maximum age.
5. **Preview** asks the service's own calculation what it would publish, and
   why not. **Save and apply** writes the mapping; EMS does not restart.
6. Once the device publishes, add it under **Find devices** as an external
   device.

![Settings, Devices, MQTT mapping: the mapped balcony inverter published, and the live topics with a Shelly (Gen2+) group, the EMS output and an unknown heating topic](../../assets/screenshots/admin/admin-mqtt-mapping.png)

A balcony inverter without a battery is inverter power **and** solar power from
the same Shelly value, otherwise it is missing from the PV total.

![Editing the mapped balcony inverter: the Shelly's apower as inverter power with the sign inverted, negative as 0 and a maximum age of 600 s, and the preview that would publish 612.3 W as inverter and solar power](../../assets/screenshots/admin/admin-mqtt-mapping-editor.png)

## Expected result

The mapping publishes the device every 15 s, retained, with an expiry of 45 s.
It falls silent as soon as one of its sources is older than its maximum age or a
value would be refused by EMS (a negative inverter power, a charge level above
100 %); EMS then shows the device as stale after 60 s.

## Warnings and common problems

- **Would not publish: a value the EMS would refuse** — usually the sign: invert
  it.
- **A source is too old** — the device sends only on change; raise the maximum
  age.
- **Port in use** — another broker runs on this host; choose another port or
  map on that broker instead.
- After restoring a backup, set the account passwords again: like other MQTT
  credentials they are not part of the backup.
- `emsctl diagnose` reports `mqtt_broker_stopped_in_use`,
  `mqtt_broker_profile_missing` and `mqtt_mapping_device_silent`.

## Recovery or next steps

- A device stays stale: open the mapping, **Preview** it and follow the reason.
- The broker does not start: free the port or choose another, then switch on
  again; nothing was saved while it failed.
- Files, containers and accounts:
[configuration reference](../../technical/configuration.md#built-in-mqtt-broker-and-mqtt-mapping).
