# E3/DC Modbus/TCP probe (read-only)

A developer tool that answers three questions about a real E3/DC installation:

1. **Does the system answer Modbus/TCP, and in which register mapping?**
2. **Which documented values are actually readable** — and with which address
   offset, unit id, function code and datatype?
3. **How often does the system refresh those values?** The E3/DC manual
   documents no refresh rate, so the cadence has to be measured on the device.

All three were answered against a physical S10 M4 on 2026-09-27: offset `-1`,
unit id 1, function code 3, and a refresh rate of **1 s**. See
[Confirmed against hardware](#confirmed-against-hardware) for what that run did
and did not prove.

Tool: [`scripts/e3dc_modbus_probe.py`](../../scripts/e3dc_modbus_probe.py).
Tests: [`tests/test_e3dc_modbus_probe.py`](../../tests/test_e3dc_modbus_probe.py)
(deterministic, no hardware).

The EMS reads an E3/DC as a grid meter (`grid_meter.type: e3dc_modbus`);
[technical/e3dc-modbus-interface.md](../technical/e3dc-modbus-interface.md)
records what the interface offers, what it costs and how that client is built.

The probe is a diagnostic tool beside that integration. It reads no
`config.json`, writes no runtime state and starts no control loop. Its only
import from `ems/` is [`ems/e3dc_modbus.py`](../../ems/e3dc_modbus.py) — the
register tables, decoders and read-only Modbus client it shares with the EMS
grid meter, so the two cannot disagree about a register. That module is stdlib
only — no `pymodbus`, so nothing is added to `requirements.txt`.

## Read-only by construction

The probe issues Modbus function codes **3** (read holding registers) and **4**
(read input registers), and nothing else. The refusal is in the shared request
builder in `ems/e3dc_modbus.py` rather than in the callers:

```python
if function_code not in READ_ONLY_FUNCTION_CODES:
    raise ModbusError(f"function code {function_code} is not a read; refused")
```

`READ_ONLY_FUNCTION_CODES` is `{3, 4}`. No argument combination and no caller can
turn this into a writer, and `--function-code` only accepts those two values.
A test walks the write function codes (1, 2, 5, 6, 8, 15, 16, 22, 23) and asserts
each one is refused.

This matters beyond tidiness: register 40088 onwards (`WallBox_0_CTRL` …) is
documented **R/W**, and in SunSpec mode register 40417 offset 5 is an active
power limit. A probe that could write would be a probe that could throttle the
inverter or unlock a wallbox by a typo.

## What it needs

- The **IP address** of the E3/DC system, passed as the only positional argument.
- Modbus enabled on the device, in the **E3/DC Simple Mode** register mapping:
  *Main menu > Functions > Modbus > Modbus and Modbus TCP, protocol
  "E3/DC Simple-Mode"* — or, on other firmware, *Main menu > Smart functions >
  Smart Home > Modbus*, then the arrow to the right, then Modbus TCP with
  protocol "E3/DC". On the Quattroporte: *Smart functions > Smart Home >
  Function Modbus*, enable Modbus, then protocol "E3/DC".
- To be on the **same subnet**. The manual states Modbus is reachable only from
  the device's own subnet, and the protocol is unencrypted.

Nothing else: no API key, no credentials, no config entry.

## Usage

```bash
# Snapshot: connect, identify the mapping, read the power values
python3 scripts/e3dc_modbus_probe.py 192.168.1.50

# Add the inverter block, the seven power meters and the DC strings
python3 scripts/e3dc_modbus_probe.py 192.168.1.50 --inverter 0 --power-meters --dc-strings

# Measure the refresh cadence (3 s polling by default, 1 s floor)
python3 scripts/e3dc_modbus_probe.py 192.168.1.50 --measure-seconds 300 --interval 1

# Live page on http://localhost:8088/, updating as the values change
python3 scripts/e3dc_modbus_probe.py 192.168.1.50 --inverter 0 --serve 8088

# Machine-readable result alongside the report
python3 scripts/e3dc_modbus_probe.py 192.168.1.50 --inverter 0 --json /tmp/e3dc.json

# Offline: verify framing, offset detection, decoders and cadence maths
python3 scripts/e3dc_modbus_probe.py --self-test
```

Exit status is `0` when the mapping was identified, `1` when the device is
unreachable or the magic word was not found, `130` on interrupt.

## How the mapping is established

The manual is explicit that the address offset is **not** uniform across Modbus
software and must be probed rather than assumed (section 4.1): start at register
40001, expect `0xE3DC`, and if that does not fit, shift the register by ±1–2.
The probe does exactly that, and also varies the two things the manual leaves
open — unit id and function code:

| Axis | Candidates | Override |
|---|---|---|
| Address offset | `-1`, `0`, `-2`, `-3`, `+1` | — |
| Unit id | `1`, `0`, `255` | `--unit-id` |
| Function code | `3`, then `4` | `--function-code` |

The first combination that returns `0xE3DC` wins, and **every** subsequent
register is read through that same offset. The report prints which combination
answered; on failure it prints every attempt and its answer, which is usually
enough to tell "Modbus off" from "wrong register mapping" from "wrong subnet".

One failure mode is diagnosed by name: if a candidate answers `0x5375`, that is
the `"SunS"` well-known base address, so the device is in **SunSpec mode** and
the report says to switch it to E3/DC Simple Mode rather than reporting a dead
interface.

## Registers

Addresses below are the manual's own 1-based numbers, exactly as the probe's
field tables spell them. The wire address is `manual address + offset`, which is
usually `-1`. The tables in `ems/e3dc_modbus.py` are the single source for
this; this document does not repeat all of them.

### Power values (manual 3.1.2)

| Manual register | Value | Length | Datatype | Note |
|---|---|---|---|---|
| 40068 | PV power | 2 | int32 word-swapped | W |
| 40070 | Battery power | 2 | int32 word-swapped | negative = discharge |
| 40072 | Home consumption | 2 | int32 word-swapped | W |
| 40074 | Grid power at the transfer point | 2 | int32 word-swapped | negative = feed-in |
| 40076 | Additional producers | 2 | int32 word-swapped | W |
| 40078 | Wallbox power | 2 | int32 word-swapped | W |
| 40080 | Wallbox solar share | 2 | int32 word-swapped | W |
| 40082 | Autarky / self-consumption | 1 | uint8 + uint8 | high byte / low byte, in % |
| 40083 | Battery SOC | 1 | uint16 | % |
| 40084 | Emergency power status | 1 | uint16 | 0…4 |
| 40085 | EMS status | 1 | uint16 | bit field, manual 3.1.5 |

All eighteen registers are fetched in **one** request, so the values of a
snapshot are consistent with one another. If the block read is refused, each
field is retried on its own, so one unsupported register cannot hide the ones
that do work; such a field is reported `unavailable` with its error.

### Inverter (manual 3.1.3 / 3.1.4)

The built-in inverter is at manual register **41000**; additional solar
inverters follow at a stride of **34** (41034, 41068, …, up to index 7). Within
a block the offsets are what `--inverter N` reads: apparent power L1/L2/L3 at
0/2/4, **active power L1/L2/L3 at 6/8/10**, reactive power at 12/14/16, then AC
voltage (×0.1), AC current (×0.01), frequency (×0.01), DC power, DC voltage and
DC current.

### Power meters (manual 3.1.6)

Manual registers 40105…40132 hold seven power meters, each as a type register
followed by the three phase powers (int16, W). The type is what makes them
interpretable, so the report resolves it: type **1** is the *root power meter*,
which is the system's control point and normally the grid connection. A
CAN-connected E3/DC power meter such as the **LM3p40isp** surfaces here — the
meter itself speaks CAN to the E3/DC system, and the system republishes its
measurements in these registers. That is the supported way to read it; do not
tap the CAN bus.

## Two traps in the manual

Both cost more than they look, because either one produces plausible numbers.

**Word order.** Section 4.2's feed-in example lists

```text
40074 = 65535
40075 = 64936
```

and then computes `4294967296 − 65535 × 65536 − 64936 = 600`, substituting
`65535` for the register it calls 40075. The arithmetic therefore treats the
**lower** address as the **low** word, which is the opposite of what its own two
table lines say. The arithmetic is right: the section's first example
(`40074 = 400`, `40075 = 0` for 400 W) agrees, and so do
`stonehage/E3DC-Modbus` and `nischram/EMD_1` independently. The probe reads the
low word from the lower address and a test pins it with the manual's own
numbers, including the assertion that the transposed reading yields something
other than −600.

**Inverter phase labels.** Section 3.1.4 labels offset 4 "L2", duplicating
offset 2, and spells reactive power "Blinkleistung". Read offset 4 as L3 and
"Blindleistung" (reactive power).

## Measuring the refresh cadence

`--measure-seconds N` polls the power block (and any `--inverter` block) every
`--interval` seconds and timestamps the moment each value is first seen to have
changed. It then reports, per value and for "any value": number of changes, the
shortest, median and longest interval between changes, and a histogram of those
intervals in 100 ms buckets.

### The poll rate is 3 s by default and never below 1 s

A control loop for this class of inverter runs at about **3 s**, so that is the
default, and `--interval` is **floored at 1 s**. Polling an inverter faster buys
nothing any consumer of the data can use, and the floor is refused at argument
parsing rather than merely discouraged.

That floor has a consequence worth stating plainly: **a refresh faster than 1 s
cannot be measured by this tool, by construction.** It is not a defect. If the
values move as fast as we can look, the answer a 3 s loop needs is "at least
that often", and that is what gets reported.

### What the tool will and will not assert

The stated refresh period is the **modal** interval, and there are four verdicts:

| Confidence | Meaning |
|---|---|
| `high` | at least three intervals observed and at least 60 % of them in the modal bucket |
| `low` | fewer than three intervals so far, or the intervals are scattered |
| `at_poll_floor` | values change as fast as the 1 s floor lets us see, so the refresh is **at least** that often and is not resolved further |
| `none` | nothing changed during the window, so no period is claimed |

Above the floor, an unresolvable interval is labelled `undersampled` and the
report says to poll faster — down to the floor, not past it.

The three-interval minimum exists because "100 % of 1 interval" is
arithmetically true and evidentially worthless: a single observed change would
otherwise be reported as a confidently known period.

`none` is the ordinary night-time answer: with no sun and a battery at its
minimum, nothing moves, and a tool that invented a period from that would be
lying. Round-trip time per request is reported alongside, so the measured
instants carry their own uncertainty: a change is observed somewhere inside one
poll interval plus one round trip.

Sampling runs on an absolute grid, so a slow response does not accumulate drift
into the measured intervals; ticks missed because the device was slower than the
interval are counted and reported rather than silently folded in.

For reference, the E3/DC-facing projects settle around 5 s of continuous
polling, and the IP-Symcon module recommends 10–60 s.

## Live view

`--serve PORT` keeps the probe running and serves a page that shows the current
reading and updates itself as the values change. It is the same probe: the same
register tables, the same client, the same cadence maths. Only the output
differs.

```bash
python3 scripts/e3dc_modbus_probe.py 192.168.1.50 --inverter 0 --serve 8088
```

The page shows solar, battery, inverter, grid, home and SOC as headline tiles, a
table of everything that was read with its register and how many times it has
changed, and the **observed refresh rate**, which fills in as evidence accrues.
Battery and grid are annotated with their direction rather than leaving the
reader to interpret a minus sign: *discharging* / *charging*, *feeding in* /
*importing*.

The inverter tile is the **sum of active power across L1, L2 and L3**. It is the
one figure on the page that is not a register, so it is marked as derived and
names the three registers it came from.

### One poller, any number of viewers

A single background thread owns the Modbus connection and publishes each reading
into shared state. Browsers subscribe over server-sent events and are woken by a
generation counter; they never touch Modbus. Ten open tabs therefore cost the
E3/DC system exactly what one tab costs, and a viewer that reloads does not
provoke an extra register read.

The HTTP surface is `GET`-only and has no `do_POST`, `do_PUT`, `do_DELETE` or
`do_PATCH` — a test asserts their absence. It serves exactly three paths:

| Path | Returns |
|---|---|
| `/` | the page |
| `/api/snapshot` | the current reading as JSON, for `curl` |
| `/events` | server-sent events, one per new reading, with a keep-alive comment every 15 s |

### Binding, and why localhost is the default

`--bind` defaults to `127.0.0.1`. The page is unauthenticated and shows a
household's live power data, so exposing it on the LAN is an explicit decision:
`--bind 0.0.0.0`, which the tool then says out loud on startup.

### Poll rate

The page polls at the same 3 s default and the same 1 s floor as a measurement
run; see [the poll rate section](#the-poll-rate-is-3-s-by-default-and-never-below-1-s).
A page left open for hours is therefore already polling at a rate the
installation would see from a control loop anyway.

### Visual style: deliberately outside the dashboard system

This page does **not** use the Aggregate/Device or Control/Energy stage style
families, and that is a decision rather than an oversight. The dashboard's
visual system is a token contract enforced by contract tests over the dashboard
sources; a standalone PoC script cannot import it, and copying the tokens into a
second place is precisely what the shared-token rule forbids. So the page is
plain, self-contained CSS with its own names, no dashboard tokens and no
dashboard class names.

The consequence to respect: **do not lift this page's styling into the
dashboard, and do not treat it as a precedent.** If these values ever become a
real dashboard surface, that surface follows
[dashboard-style-guide.md](dashboard-style-guide.md) and this page is thrown
away.

## Offline self-test

`--self-test` starts a loopback Modbus/TCP server that serves a plausible S10
register image, then verifies end to end: offset detection, the identification
strings, the signed power values, the inverter block, the power-meter types, the
single-register fallback for a refused register, the refusal of write function
codes, SunSpec recognition, and — against a register that is mutated on a known
period — that the cadence maths recovers that period.

It needs no network beyond loopback and no E3/DC. `tests/test_e3dc_modbus_probe.py`
covers the same ground as 85 deterministic tests, plus the cadence maths on
synthetic timestamps and the live view's state, routes and poller, so nothing
depends on wall-clock timing.

## Sources

- *Modbus/TCP-Schnittstelle der HagerEnergy GmbH*, V1.90 (28.01.2022), section
  3.1 "E3/DC Simple Mode" and appendix 4. A copy ships inside the IP-Symcon
  module repository named below.
- [`mccrossen/iobroker-modbus-e3dc`](https://github.com/mccrossen/iobroker-modbus-e3dc)
  — register table as TSV (from manual V1.70), including the `int32sw` datatype
  and the power-meter type list.
- [`stonehage/E3DC-Modbus-ESP32-ESP8266-1`](https://github.com/stonehage/E3DC-Modbus-ESP32-ESP8266-1)
  — `REG_OFFSET -1`, the magic-word probe loop and explicit int32 arithmetic.
- [`nischram/EMD_1`](https://github.com/nischram/EMD_1) — the same offsets,
  reached independently, plus the DC string registers.
- [`Brovning/e3dc`](https://github.com/Brovning/e3dc) — IP-Symcon module;
  carries the manual as `docs/ModBus-Dokumentation.pdf` and documents how to
  enable Modbus on each firmware generation.

## Confirmed against hardware

Read from one physical installation on 2026-09-27: an **S10 M4**, Modbus
firmware 1.2, system firmware `S10_2026_02`.

| | Expected from the manual | Measured |
|---|---|---|
| Magic word | `0xE3DC` | `0xE3DC` |
| Address offset | `-1` | `-1`, hit on the first candidate |
| Unit id | 1 | 1 |
| Function code | 3 | 3 |
| Refresh rate | not documented | **1 s** |

The refresh rate came from a 180 s window at the 1 s poll floor: 179 changes,
every gap in the 1.0 s bucket, shortest 0.961 s, median 1.000 s, longest
1.033 s, round trip median 6.2 ms over 360 requests with no read errors. The
verdict is `at_poll_floor` — at least 1 Hz, deliberately not resolved further.
A control loop at 3 s therefore always reads a value at most about a second old.

Two findings the manual does not mention:

- **The phase frequency register (41024) refreshes every 20 s, not every
  second.** Everything else that moved, moved at 1 s. Anything that wants grid
  frequency for control has to know that.
- **The root power meter was at index 6, not index 0.** Reading all seven slots
  and resolving the type register is what found it; assuming index 0 would have
  returned zeros. Its three phase powers summed to the grid register exactly at
  two different operating points (`-103 + 37 + 62 = -4 W`, and
  `-698 + 522 + 457 = 281 W`), which cross-validates the int16 phase decoding
  against the word-swapped int32 grid register.

Register 40003 reported **1271** supported registers on this firmware, an order
of magnitude beyond the 132 the manual documents. Unexplored.

## Limitations

- Confirmed on one S10 M4 with one firmware. Other models, other firmware and
  other power-meter configurations are untested.
- "1 s" is a lower bound, not a measured period: anything faster is
  unresolvable at the 1 s poll floor by design.
- Only the E3/DC Simple Mode mapping is implemented. SunSpec mode is detected
  and named, not read.
- RSCP, the other local E3/DC interface, is out of scope for this probe.
- The wallbox registers, the emergency-power states and the EMS status bit field
  were read but stood still throughout, so their decoding is unexercised against
  values that move.
- The live page was rendered against the real installation, but only its normal
  path. Its stale-reading and lost-stream states have been seen against the
  loopback device only.
