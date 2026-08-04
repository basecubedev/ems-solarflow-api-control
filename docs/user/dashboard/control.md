# Control pipeline

## Purpose

Answer the question "why did EMS write *that* value?" — stage by stage, from the
measurements it started with to the write it finally made or suppressed.

## When to use this workflow

- Output is not what you expected.
- A device is not contributing.
- You changed a setting and want to see where it takes effect.
- Before reporting a control problem — this tab *is* the evidence.

## Prerequisites

- EMS running and the dashboard open.
- Reading the pipeline needs no login. The **Runtime settings** block at the top
  of the same tab does — see [Runtime settings](runtime-settings.md).

## How to read it

![Control tab showing the six system pipeline stages and the per-device stage cards for WR1 and WR2](../../assets/screenshots/dashboard/dashboard-control.png)

The Control tab is **Control / Energy stage** style throughout: numbered cards,
each with a title, a short subtitle, compact fact rows, and a highlighted
**RESULT** at the bottom. Read left to right; each stage's result feeds the next.

There are two levels:

1. **System stages 01–06** — one decision for the whole installation.
2. **Per-device stages 01–05** — how that decision was split and adjusted for
   each inverter.

## System stages

### 01 — Measurements

*Live values define the demand basis.*

| Fact | Meaning |
| --- | --- |
| **FILTERED LOAD** | House load after filtering and deadbands — not the raw meter reading |
| **PV TOTAL** | Combined PV input |
| **OUTPUT TOTAL** | What the inverters are currently delivering |
| **RESULT / DEMAND BASIS** | The demand EMS will work from |

**Filtered, not raw.** EMS deliberately does not chase every meter twitch.
Filtering, deadbands and ramps exist so the hardware is not commanded to
oscillate. A demand basis that lags a sudden load spike by a moment is correct
behaviour.

### 02 — Target

*Request and limits become the effective target.*

| Fact | Meaning |
| --- | --- |
| **REQUESTED** | The requested total (`commanded_total_w` plus filtered load) |
| **STRATEGY** | The allocation strategy, e.g. `pv first` |
| **RESULT / EFFECTIVE TARGET** | The system target after limits |

### 03 — Distribution

*The target is allocated across devices.*

| Fact | Meaning |
| --- | --- |
| **TARGET SPLIT** | The per-device split, e.g. `WR1: 400 W / WR2: 400 W` |
| **STRATEGY** | `pv first` |
| **RESULT / ALLOCATED TOTAL** | The sum actually allocated |

This is where a device "not contributing" is usually explained. PV-first
allocation prefers the inverter that has its own PV input.

### 04 — Limits / gates

*Limits and write gates shape commandable power.*

| Fact | Meaning |
| --- | --- |
| **ACTIVE LIMITS** | Which limit is currently binding, e.g. *System output limit* |
| **WRITE GATE** | Whether writing is permitted, e.g. `Send` |
| **RESULT / COMMANDABLE TOTAL** | What may actually be commanded |

If **ALLOCATED TOTAL** and **COMMANDABLE TOTAL** differ, a limit or a gate is the
reason — and this card names it.

### 05 — Commands

*Command state decides whether writes are needed.*

| Fact | Meaning |
| --- | --- |
| **COMMANDED** | The value being commanded |
| **WRITES** | How many writes this cycle, e.g. `Send 2` |
| **RESULT / COMMAND DECISION** | `Send`, or a suppression reason |

**No write is also a decision.** If the new value is inside the deadband, EMS
deliberately writes nothing. That is not a failure.

### 06 — Result

*Final targets become the active control state.*

| Fact | Meaning |
| --- | --- |
| **FINAL SPLIT** | The final per-device values |
| **RESULT / FINAL TOTAL** | The active control state |

### The context strip

Under the six stages, a compact strip shows the inputs that shaped the cycle:
**MODE** (`pv_first`), **MAX POWER**, **MIN OUTPUT**, **DEVICES**, **ACTIVE
GATES**.

## Per-device stages

Each inverter gets its own row: a **MEASUREMENTS** block (PV, SOC, OUTPUT), a
**CONTEXT** block (SOC RANGE, OUTPUT LIMIT, DEVICE MAX, PV PRIORITY), then five
stages.

### 01 — Inputs

*Live values from this inverter.* Result: **INPUT STATE** — `ready` when the
device's data is usable.

A device whose input state is not ready is excluded from the write decision. EMS
does not command on stale data.

### 02 — Weighting

*PV, SOC and balance produce the weight.*

| Fact | Meaning |
| --- | --- |
| **BASE WEIGHT** | Starting weight |
| **PV PRIORITY** | The PV-priority multiplier for this device |
| **CHARGE BALANCE** | The battery-balance multiplier |
| **RESULT / EFFECTIVE WEIGHT** | The weight used for the split |

### 03 — Raw target

*Weight share is applied to the requested target.*

Shows **WEIGHT**, **SHARE** (this device's percentage), **REQUESTED**, and an
explicit **FORMULA** line such as `800 W x 40% = 320 W`, then **RESULT / RAW
TARGET**.

The formula row is the arithmetic, spelled out. If a device's share surprises
you, this is where to look.

### 04 — Adjustments / limits

*Limits modify the raw device target.*

| Fact | Meaning |
| --- | --- |
| **PV-ONLY LIMIT** | Cap derived from this device's own PV |
| **OUTPUT LIMIT** | The device's configured output limit |
| **DELTA** | Change against the previous cycle |
| **CAPABILITY** | Detected capability, e.g. *PV input detected* |
| **RESULT / ADJUSTED TARGET** | After the limits |

### 05 — Final target

*Adjusted target and write gate finish the decision.*

| Fact | Meaning |
| --- | --- |
| **ADJUSTED** / **FINAL** | Before and after the final step |
| **DELTA OUTPUT** | Change to be applied |
| **WRITE** | `Send` or suppressed |
| **REASON** | Why, e.g. `output limit update` |
| **RESULT / FINAL / WRITE** | The decision and the value |

## Deadbands, ramps and limits

| Mechanism | What it does | Why |
| --- | --- | --- |
| **Deadband** | Suppresses writes for changes below a threshold | Stops pointless writes and hardware wear |
| **Ramp** | Limits how fast a value may move per cycle | Avoids abrupt steps |
| **`min_output_limit`** | Floor below which output is not commanded | Some hardware behaves poorly at tiny values |
| **System output limit** | Cap for the whole installation | Your configured ceiling |
| **Per-device limit** | Cap for one inverter | Device rating or your choice |

These are why a target and an output differ for a few cycles. Persistent
divergence is different — read stage 04 to see which limit is binding.

## When EMS deliberately does not write

| Reason | Where you see it |
| --- | --- |
| Change is inside the deadband | Stage 05 command decision |
| Telemetry is stale | Per-device stage 01 input state |
| A write gate is off | Stage 04 write gate |
| `dry_run` or simulation mode | Stage 04 / the context strip |
| Night / minSoc idle | Overview **Rules** panel |
| Device disabled | Per-device stage 01 |

**Stale data blocks writes on purpose.** Commanding hardware from old
measurements is worse than not commanding it.

## Dry run, simulation, and MQTT confirmation

- **`dry_run`** — EMS computes everything and writes nothing. The pipeline still
  fills in, so you can validate a configuration safely before going live. See
  [Safety](../safety.md).
- **Simulation / replay** — synthetic or recorded input; hardware writes are
  forced off.
- **MQTT confirmation** — for an MQTT-controlled device, **publishing a command is
  not success**. A publish is only a transport step; EMS reports a command
  effective only after **telemetry confirmation** that the device took the value.
  Over the cloud broker that confirmation typically arrives a few seconds later —
  see [MQTT](../admin/mqtt.md#what-to-expect-from-the-cloud-path). A command shown
  as sent but not yet confirmed is exactly that, and the dashboard does not
  pretend otherwise.

## Read-only view

![Control tab without a session, showing the read-only state](../../assets/screenshots/dashboard/dashboard-control-readonly.png)

Without a login the pipeline is fully readable — this is diagnostic information,
not a control surface. Only the **Runtime settings** block at the top requires a
session.

## What happens in the background

- Every value here comes from EMS's own control cycle. The dashboard does not
  recompute anything.
- One control cycle runs per loop interval, in the order shown.
- Write eligibility is decided by EMS write gates and capability detection.

## Expected result

For any cycle you can say: what EMS measured, what it targeted, how it split
that, what limited it, whether it wrote, and why.

## Warnings and common problems

| Symptom | Meaning | What to do |
| --- | --- | --- |
| Target ≠ Output persistently | A limit is binding | Stage 04 names it |
| One device gets nothing | PV-first allocation, or it is not ready | Stages 03 and per-device 01 |
| `WRITES: 0` | Deadband, gate, or nothing to change | Stage 05 reason |
| Write gate shows blocked | A gate is off, or dry run/simulation | [Safety](../safety.md) |
| Values oscillate | Likely a second controller | Nothing else may write `outputLimit` |
| Command sent, hardware unchanged | Awaiting MQTT confirmation, or write ineffective | [MQTT](../admin/mqtt.md) |

## Recovery or next steps

- Per-device detail → [Devices](devices.md)
- Change a limit live → [Runtime settings](runtime-settings.md)
- Capture the same data for a report →
  [Diagnostics](diagnostics.md) (`diagnose --control`)
- The maths → [Control logic](../../technical/control-logic.md) ·
  [Control flow](../../technical/control-flow.md)
