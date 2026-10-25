# EMS Dashboard — step-by-step guides

The live operating UI (**EMS SolarFlow**, *"Control your energy."*) on port
`8080`. It shows what the EMS is measuring, what it decided and why, and, once
you sign in, lets you change a small set of runtime values, set up schedules and
use the operator tools.

![EMS Dashboard Overview with the side menu, the four status tiles, the energy flow, the storage card and the active rules](../../assets/screenshots/dashboard/dashboard-overview.png)

## Choose your guide

| You want to | Read |
| --- | --- |
| Understand the top bar, the menu and the Overview page | [Overview and navigation](overview.md) |
| Read a single inverter's state | [Devices](devices.md) |
| See how much energy was delivered, or look further back | [Energy and analytics](energy.md) |
| Understand *why* the EMS wrote that value | [Control pipeline](control.md) |
| Change a live setting, or plan one with a schedule | [Runtime settings and schedules](runtime-settings.md) |
| Collect evidence, read logs, back up | [Diagnostics and maintenance](diagnostics.md) |
| Change the colour scheme, the language or the density | [Appearance and language](appearance.md) |

## The pages

The side menu lists the pages in two groups, in this order:

```text
Overview · Devices · Energy · Analytics · Control · Schedule
System: Diagnose · Logs · Maintenance
```

| Page | What it shows |
| --- | --- |
| **Overview** | *Your energy system right now*: system status, self-sufficiency, solar and savings, the energy flow, storage, the active rules, today's energy and the power chart |
| **Devices** | *Inverters, batteries and meters*: one card per inverter, the grid meter, the energy flow per device and the power history |
| **Energy** | *Delivered energy and savings by period*: periods, months, years and the lifetime, in **Basic** or **Expert** |
| **Analytics** | *Long-range history from InfluxDB*, when InfluxDB is set up |
| **Control** | *What the EMS is doing and why*: the current decision, the runtime settings, the six stages of the control pipeline and the decision per device |
| **Schedule** | *Switch points the EMS applies by itself* |
| **Diagnose** | *Health checks and root causes* |
| **Logs** | *What the EMS has written to its log* |
| **Maintenance** | *Backups, restore and config upgrade* |

A lock beside Schedule, Diagnose, Logs and Maintenance means they need you to
be signed in. Signed out, they show *"Sign in to see this page."* with a
**Sign in** button instead of empty panels. On a phone the menu becomes a bar
at the bottom, see [Overview and navigation](overview.md#on-a-phone).

## Read-only by default

The dashboard is a view onto the EMS, not a second controller.

- Without signing in it is **read-only**. The top bar shows a **Read-only**
  pill and a **Sign in** button.
- After signing in the pill reads **Write mode** and the button **Sign out**.
  The runtime settings on the Control page and the Schedule, Diagnose, Logs
  and Maintenance pages become usable.
- The password is the **same one as the Admin Console's**, stored in
  `config/dashboard-auth.json`. If none is set, the sign-in dialog says
  *"No dashboard password is set. Set one with: python3 emsctl.py dashboard
  set-password"*.
- Every write is checked on the server for a session **and** a CSRF token. A
  visible button is never what authorizes a write.

Reading the dashboard never changes anything on the EMS, signed in or not.

## The deprecated Classic dashboard

The previous design, **Classic**, still answers at
`http://<host>:8080/classic/` and is removed in a later release. It reads the
same API, uses the same password and writes the same runtime settings, so
switching changes nothing on the EMS. **Classic view** at the foot of the side
menu, or **Switch to classic view** in the Appearance menu, opens it; Classic's
**New dashboard** link comes back.

The dashboard's address always opens the Modern design except in three cases:
demo mode (`?demo=1`), which only Classic has; `?ui=classic` from a link made
before Modern moved to the address; and a browser too old for Modern. Modern
needs Chrome or Edge 114, Firefox 125 or Safari 17 (iPadOS 17) or newer. An old
bookmark of `/modern/` opens the same page at the address.

## Related

- [Admin Console guides](../admin/index.md) — install, upgrade, devices, backups.
- [Live Dashboard reference](../../dashboard.md) — configuration, API, HTTPS,
  session lifetime, history sources.
- [Schedules](../../scheduler.md) — how the switch points work.
- [Safety](../safety.md) — before enabling live hardware writes.
- [Control logic (technical)](../../technical/control-logic.md) — the maths behind
  the Control page.
