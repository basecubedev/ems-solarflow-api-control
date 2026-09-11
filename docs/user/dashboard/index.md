# EMS Dashboard — step-by-step guides

The live operating UI (**EMS SolarFlow Control**, *"Read-only energy cockpit"*)
on port `8080`. It shows what EMS is measuring, what it decided, and why — and,
once you log in, lets you change a small set of runtime values.

![EMS Dashboard overview with the PV, Home, Grid, Battery and SoC tiles, the Live Flow diagram and the Rules panel](../../assets/screenshots/dashboard/dashboard-overview.png)

## Choose your guide

| You want to | Read |
| --- | --- |
| Understand the header, tiles and navigation | [Overview](overview.md) |
| Read a single inverter's state | [Devices](devices.md) |
| See how much energy was delivered | [Energy and analytics](energy.md) |
| Understand *why* EMS wrote that value | [Control pipeline](control.md) |
| Change a live setting | [Runtime settings](runtime-settings.md) |
| Collect evidence, read logs, back up | [Diagnostics and maintenance](diagnostics.md) |

## The two visual families

The dashboard deliberately uses only two card styles. Knowing which one you are
looking at tells you what it is for.

| Family | Where | What it means |
| --- | --- | --- |
| **Aggregate / Device** | Header tiles, Live Flow, Rules, device cards, History | Measured and reported state. Read-only. |
| **Control / Energy stage** | Control pipeline, Energy Delivered, Diagnose, Logs, runtime write forms | A decision, a stage of one, or an operator action. |

Control / Energy stage cards are the numbered ones: a step number, an icon, an
uppercase title and a short subtitle, with compact fact rows underneath. When you
see that shape, you are looking at EMS reasoning or at something you can change.

## Read-only by default

The dashboard is a cockpit, not a second controller.

- Without logging in it is **read-only**. The header shows a **Read-only** pill.
- After logging in the pill reads **Write mode**, and the runtime write forms and
  the operator-only Diagnose, Logs and Maintenance tabs become usable.
- Authentication uses the **same password as the Admin Console**, stored in
  `config/dashboard-auth.json`.
- Write actions are protected by authentication **and** CSRF on the server. A
  visible button is never what authorizes a write.

## Appearance

A **Theme** menu sits in the header, next to the connection pill. The twelve
palettes are the same ones the Admin Console and the Appliance Manager offer,
all dark:

| | |
| --- | --- |
| **Signal** | the default, and what the cockpit has always looked like |
| **Instrument**, **Graphite** | neutral greys, no colour cast in the background |
| **Fjord**, **Blueprint**, **Indigo** | cool blues, from slate to deep violet |
| **Viridian**, **Phosphor** | green: an instrument panel, and a CRT |
| **Copper**, **Oxide** | warm metal and rust |
| **Contrast** | the hardest separation between text and background |
| **Void** | near-black, with the accent carrying the light |

Only the colours change. The meaning of a colour does not: PV stays the warm
one, battery the green one, grid the violet one, and a blocked write stays red
on every palette.

The choice is remembered by the browser you made it in, which is what you want
for a screen that stays on in one room -- it is not installation state, so a
tablet in the hall and a laptop at the desk can each have their own. It does not
carry over from the Admin Console either: those are separate addresses, and a
browser keeps such a preference per address.

## Navigation

Eight tabs, in the order they appear:

```text
Overview · Devices · Energy · Analytics · Control · Diagnose · Logs · Maintenance
```

Diagnose, Logs and Maintenance are operator-only: unauthenticated they render a
compact "login required" message rather than empty panels.

## Related

- [Admin Console guides](../admin/index.md) — install, upgrade, devices, backups.
- [Live Dashboard reference](../../dashboard.md) — configuration, API, HTTPS,
  session lifetime, history sources.
- [Safety](../safety.md) — before enabling live hardware writes.
- [Control logic (technical)](../../technical/control-logic.md) — the maths behind
  the Control tab.
