# E3/DC Modbus/TCP interface (measured)

What an E3/DC energy storage system offers a local client over Modbus/TCP, and
what it costs to read. Written for whoever builds the client, not for operators.

The EMS reads an E3/DC as a **grid meter** and, separately, as a **read-only
device** on the dashboard, both configured as described in
[configuration.md](configuration.md#e3dc-storage-system-via-modbus-tcp-e3dc_modbus).
Nothing in this project writes to an E3/DC. This
document records what a read-only probe established against one physical
installation, which is what the client was designed from.

Tool that produced these numbers:
[`scripts/e3dc_modbus_probe.py`](../../scripts/e3dc_modbus_probe.py), described
in [developer/e3dc-modbus-probe.md](../developer/e3dc-modbus-probe.md). **The
field tables in [`ems/e3dc_modbus.py`](../../ems/e3dc_modbus.py) are the single
source for register addresses, lengths, datatypes and scaling**; the probe and
the EMS client both read through them. This document does not repeat them; it
records behaviour the tables cannot carry.

## Connection

| | Value | Established how |
|---|---|---|
| Transport | Modbus/TCP, port **502** | fixed by the manual; all four reference projects agree |
| Unit id | **1** | device menu field "Gerät"; probed and confirmed |
| Function code | **3** (read holding registers) | probed; 4 also accepted by the probe, unneeded |
| Address offset | **−1** | probed via the magic word, hit on the first candidate |
| Magic word | `0xE3DC` at manual register 40001 | confirmed |
| Register mapping | E3/DC Simple Mode | device menu, protocol `E3DC` |

The offset must be **probed, not assumed.** The manual states plainly that the
offset differs between Modbus client libraries and prescribes finding it with
the magic word before trusting any other register. It came out `−1` here, which
is the ordinary "documented register 40001 is wire address 40000" convention,
but a client that hard-codes it is a client that silently reads the wrong
registers on the next installation.

### The interface is off until two switches are on

This cost real time, so it belongs in the client's troubleshooting text. The
device has **two** separate toggles, and only the second one opens port 502:

1. *Hauptmenü › Smart-Funktionen › Smart Home › Funktion Modbus* — enables the
   Modbus function group. Its own on-screen text says: "Bitte aktivieren Sie
   auch den passenden Kommunikationsadapter auf den nächsten Seiten."
2. One page right: **ModBus TCP** — its own on/off switch, plus protocol
   (`E3DC`, not `SUN_SPEC`), device id and port, then *übernehmen*.

With only the first enabled, the system answers ping, RSCP and its web UI while
port 502 refuses every connection. A full 65535-port sweep found nothing
listening anywhere, so "it is on a different port" is not the explanation to
reach for.

Modbus is reachable **only from the device's own subnet** and is unencrypted.

## What the interface gives you

Semantics, signs and provenance. Addresses and datatypes live in the probe's
field tables.

### Aggregate power (manual section 3.1.2)

| Value | Sign convention | Notes |
|---|---|---|
| PV power | positive | 0 W when the strings are dark; not a fault |
| Battery power | **negative = discharge** | |
| Home consumption | positive | the system's own figure, not a sum you compute |
| Grid power at the transfer point | **negative = feed-in** | see below — this is the one the EMS wants |
| Additional producers | positive | |
| Wallbox power / solar share | positive | stood still throughout; decoding unexercised |
| Autarky / self-consumption | two percentages in one register | high byte / low byte |
| Battery SOC | percent | |

### Per-inverter block (manual sections 3.1.3 / 3.1.4)

The built-in inverter is one block; up to seven additional solar inverters
follow at a fixed stride. Apparent, active and reactive power per phase, AC
voltage/current, frequency, and DC power/voltage/current.

On a single-phase system only L1 carries values; L2 and L3 read a clean 0 rather
than noise, so "all three are zero" is a real state and not a decoding failure.

**Active power is what a client wants**; apparent power is a separate register
and the two differ (measured 1439 VA against 1453 W at one moment). Summing the
three phases is a derived figure, not a register — if the client publishes it,
it should say so.

### Power meters (manual section 3.1.6)

Seven slots, each a type register followed by three phase powers. The type
register is what makes a slot interpretable; **type 1 is the root power meter**,
which is the system's control point and normally the grid connection.

Two things a client must not assume:

- **The root meter is not necessarily slot 0.** On the measured installation it
  was slot **6**, and slots 0–5 were all type 0. A client that reads slot 0 gets
  zeros and reports a working meter measuring nothing. Read all seven and
  resolve by type.
- A CAN-connected E3/DC power meter such as the **LM3p40isp** appears here. The
  meter speaks CAN to the E3/DC system and the system republishes it in these
  registers. That is the supported way to read it. Do not tap the CAN bus.

The root meter's three phases summed to the grid register exactly, at two
different operating points:

```text
-103 + 37 + 62  = -4 W    grid register: -4 W
-698 + 522 + 457 = 281 W  grid register: 281 W
```

That is a useful self-check for a client: the int16 phase decoding and the
word-swapped int32 grid register are independent code paths that must agree.

## Timing

Measured over 180 s at a 1 s poll interval, 360 requests, no read errors:

| | |
|---|---|
| Refresh period | **1 s** |
| Changes observed | 179 in 180 s, every gap in the 1.0 s bucket |
| Gap min / median / max | 0.961 s / 1.000 s / 1.033 s |
| Round trip min / median / max | 1.5 ms / 6.2 ms / 40.7 ms |

The manual documents no refresh rate at all, which is why this was measured. The
verdict is a **lower bound**: polling was floored at 1 s, so anything faster is
unresolvable by construction. For a control loop at 3 s this is more than
settled — every read returns a value at most about a second old, and there is no
aliasing to design around.

**The phase frequency register is the exception: it refreshes every 20 s.**
Everything else that moved, moved at 1 s. Anything that ever wants grid
frequency for a control decision has to know that it is a twenty-second value
wearing the same clothes as the one-second ones.

Values that did not move during the window — SOC, emergency-power status, EMS
status, the wallbox registers — are slow or idle quantities, not stalled reads.
A client must not treat "unchanged" as "stale".

### What this implies for a client

- One connection, one reader. The probe holds a single socket and reads whole
  blocks in one request each; 40068–40085 is 18 registers and answers in about
  6 ms.
- Read a block in **one** request rather than a register at a time, so the
  values of a snapshot are consistent with each other.
- Read in step with the control loop. The loop adds each reading to what it
  already commands, so a reading it sees twice is counted twice; the EMS
  therefore reads once per control cycle and never hands one reading to the
  loop twice. The E3/DC's own one-second refresh is fast enough for any loop
  interval.
- Expect the connection to survive; also expect to reconnect. The probe
  reconnects and retries fields individually when a block read fails, which is
  what keeps one unsupported register from hiding the ones that work.

## The client in this project

An E3/DC plays two independent roles, both read-only:

- **Grid meter** (`grid_meter.type: e3dc_modbus`), because register 40074 is the
  power at the grid transfer point — measured by the root power meter — which is
  exactly the quantity the control loop consumes. `create_grid_meter_client()`
  in [`ems/clients.py`](../../ems/clients.py) builds an
  `E3dcModbusGridMeterClient`, and from there everything downstream — target
  calculation, dashboard, health, diagnostics — is the path every other meter
  takes.
- **Read-only device** (a `devices[]` entry of type `e3dc_modbus`), which puts
  PV, battery power and SOC, inverter AC power and grid power on the dashboard.
  It rides the side door the dashboard already has for telemetry-only MQTT
  devices; it is never in the controller's device set.

The sign conventions already match. [configuration.md](configuration.md) states
the EMS convention as "positive values mean grid import and negative values mean
grid export"; E3/DC register 40074 is negative for feed-in. **No inversion.** A
client that adds one would be wrong in a way that only shows up on a sunny day.
Battery power (40070) is positive while charging, which is the dashboard's own
convention.

How the open questions of the probe phase were settled:

- **The control loop drives the read.** One `E3dcModbusSession` per E3/DC
  endpoint (`ems/e3dc_runtime.py`) owns the connection. The grid meter's
  `get_power()` and the device refresh ask it for this cycle's reading, and
  whichever asks first sends the request; the other gets the same reading. So
  the poll interval is the loop interval by construction, every cycle has its
  own reading, and none is counted twice by the integrating control loop. A
  background poller at its own rate was built first and dropped for exactly
  that reason: out of step with the loop, it hands the loop one reading twice
  while health still reads fresh.
- **Failure costs the loop little.** A failed read is reported at once and the
  last good value kept. The offset search stops at the first transport error
  instead of trying every candidate, and while the device is gone the session
  backs off (1 s doubling to 30 s) without touching the network, so a missing
  E3/DC costs at most one timeout now and then, not one per cycle.
- **No new dependency.** The stdlib client moved out of `scripts/` into
  `ems/e3dc_modbus.py`, and the probe now imports it, so the EMS and the probe
  cannot disagree about a register.
- **The offset is probed at every connect,** as the manual prescribes, against
  the configured unit id with function code 3. Nothing about the offset is
  configured. A device in SunSpec mode is named as such.
- **Read-only by construction.** Neither client has a write method, and the
  Modbus request builder refuses every function code other than 3 and 4. The
  E3/DC's battery and inverter stay under the E3/DC's own control; registers
  40088 onward and the SunSpec power limit are not used. A central predicate,
  `ems.read_only_devices.is_read_only_device_config`, keeps a `devices[]` entry
  out of every control, write, runtime-state and reconciliation decision.
  Tests pin all three.
- **Two requests per cycle.** The whole 40068–40085 power block is read in one
  request; the inverter block (41000, 34 registers) is read only when a device
  tile needs it, and its three active-power phases are summed. A refused or
  failed inverter read only leaves the tile without an output value (the tile
  shows offline); it never costs the grid meter its reading, and the inverter
  block then backs off on its own, like the connection does. A reading is
  reused by the other role for half a second after it completes and never
  handed to the same role twice; the device refresh follows the grid read
  directly, so it always finds that reading.
  Identification (manufacturer, model, firmware — not the serial number) is
  read once per connect for logs and diagnostics.

The E3/DC keeps regulating its own storage towards zero at the same grid
point. Two controllers on one set point can work against each other; the
configuration reference says so to operators, and nothing here has been
observed on real hardware yet.

## What has not been proven

- Whether the E3/DC accepts a second Modbus/TCP connection while the EMS
  EMS holds one. `emsctl diagnose --hardware` and `emsctl grid-meter test`
  open their own connection next to a running EMS.
- Neither the grid meter nor the device tile has yet run against a real
  E3/DC. Their Modbus reads go through the same code that read the S10 M4
  below, and they are tested end to end against the probe's loopback device.
- One installation: an **S10 M4**, Modbus firmware 1.2, system firmware
  `S10_2026_02`. Other models, firmware and meter layouts are untested.
- "1 s" is a lower bound, not a measured period.
- The wallbox registers, emergency-power states and the EMS status bit field
  were read but stood still, so their decoding is unexercised against values
  that move.
- Register 40003 reported **1271** supported registers, an order of magnitude
  beyond the 132 the manual documents. Unexplored.
- Only the E3/DC Simple Mode mapping was read. SunSpec mode is detected by the
  probe and named, not decoded.
- RSCP, the other local interface, answers on port 5033 on this installation but
  is out of scope and was not spoken to.
