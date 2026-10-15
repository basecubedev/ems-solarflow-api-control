# Safety Model

This is the technical safety reference for EMS write behavior. It documents the
write gates, runtime write types, and the specific Zendure fields EMS may write.

For the short user checklist, see [Safety](../user/safety.md).

Related references: [configuration.md](configuration.md),
[control-logic.md](control-logic.md), [runtime-state.md](runtime-state.md),
[admin-architecture.md](admin-architecture.md).

## EMS write ownership

EMS is the source of truth for control writes. The Admin Console is UI and
orchestration only; it never runs the control loop and never writes control
state directly. It calls the same EMS tools a shell user would run. See
[admin-architecture.md](admin-architecture.md).

At least one supported Zendure connection — Local API, Local MQTT, or Zendure
cloud MQTT — must be available for EMS control (the Local API also does full
state reconciliation; the MQTT transports carry the power command only). Do not run Zendure
HEMS, smart-home automations, MQTT writers, or any other controller in
parallel if they write Zendure `outputLimit`. EMS assumes exclusive write control
over `outputLimit` while active. The EMS must not run in parallel with another
controller writing Zendure `outputLimit`.

An external device EMS only reads over MQTT never receives a write from EMS, but
it can still be a second controller: a battery that regulates itself to zero
against the same meter reacts to the same deviation as EMS, and the two can
overshoot or push energy back and forth. EMS cannot prevent that; see
[External Devices over MQTT](configuration.md#external-devices-over-mqtt-external_mqtt).

## Write gates

Runtime `outputLimit` writes share the same safety precondition — `dry_run=false`,
`simulation_mode=false`, not replay — and then require the **named gate for the
device's transport**:

```text
API device (local HTTP)      -> allow_hardware_writes=true
Local MQTT broker device     -> allow_mqtt_local_control_writes=true
Zendure cloud MQTT device    -> allow_mqtt_zendure_control_writes=true
```

All three gates default to `true` in the template: whether a transport actually
writes is decided by configuration presence (a per-device `write_output_limit`
opt-in, a configured broker, an API key), and each gate stays editable for
read-only validation. A normal config that omits the gate keys resolves them to
the release defaults at load time — the same effective values a config upgrade
would write — without rewriting the file. Explicit `false` values always win,
the simulation/replay safe config keeps every gate off, and template
placeholder safety forces every gate off. Some hardware generations have not
been validated on physical hardware by the maintainer (see
[supported-setups](../user/supported-setups.md)). A device's transport is
chosen from its broker `source`; the write is published to
`iot/<productKey>/<deviceId>/properties/write` (or an explicit
`mqtt.write_topic`).

State reconciliation writes (`minSoc`, `socSet`, `smartMode`, `gridOffMode`,
full-charge-assist `socSet`/`acMode`/`inputLimit`)
additionally require:

```text
allow_state_reconciliation_writes=true
```

The template profile is intended for normal standalone live control after real
local values are configured and installation limits are reviewed. If required
placeholders are still present, EMS forces safe mode: control disabled, dry-run
enabled, and hardware writes blocked. Set `dry_run=true` manually when you want
a no-write validation run.

## Runtime write types

Runtime control — the power command, on the transport's own write gate — may
write:

```text
outputLimit                                           every non-negative target (local API)
smartMode=1, acMode=1, outputLimit=0, inputLimit      entering a charge
inputLimit                                            a new power inside a charge this EMS
                                                      started, the device showing its mode (local API)
smartMode=1, acMode=2, outputLimit, inputLimit=0      leaving a charge this EMS commanded, until the
                                                      device reports it left; and once per start the
                                                      one exit to AC input found on a device the EMS
                                                      could have charged, above the floor or at it,
                                                      until the device takes it (three attempts at most)
inputLimit=0                                          ending the EMS's charge on a device a claim
                                                      holds in AC input (local API)
smartMode, acMode, outputLimit, inputLimit            every power command over MQTT (ZenSDK)
```

A device the EMS could never have charged gets only `outputLimit` from the
local API, whatever direction it reports, and so does one someone else holds in
AC input after that one exit; their modes are the state reconciler's, behind the
reconciler's own gate.

State reconciliation may write:

```text
minSoc
socSet
smartMode
gridOffMode for explicit offgrid socket intent
socSet=1000 / configured socSet restore during battery full-charge assist
acMode/inputLimit during battery full-charge assist only through runtime intent
```

Runtime power writes and persistent state reconciliation writes are separate
write paths. A power write — discharge, idle or charge — requires the device's
transport gate to be enabled; state reconciliation writes additionally require
`allow_state_reconciliation_writes=true` and control switched on: with the EMS
(`system.enabled`) or the device (runtime `enabled`) switched off, no reconciler
writes, and the withheld write is logged with `control_disabled` or
`device_disabled` in `blocked_by`. Off is off; the one exception is the final
command that ends a charge the EMS started, described below.

State reconciliation is API-only: Zendure MQTT control devices carry
`supports_state_reconciliation=False` and are skipped by every state
reconciliation writer. That flag means "no separate reconciliation path", not
"cannot set a mode": a ZenSDK power command is one atomic property write
carrying `smartMode`, `acMode`, `outputLimit` and `inputLimit` together, because
a bare setpoint is ignored by a device sitting in an inactive mode. The mode
therefore travels with the power command, on the transport's own gate, in both
directions. The local API does the same where it changes a direction it set:
otherwise it keeps the single-property `outputLimit` write, but while its record
of the EMS's own charge holds, a non-negative target is sent as the atomic set
(`smartMode=1`, `acMode=2`, `outputLimit`, `inputLimit=0`), and so is the one
exit the controller decides, once per start, for AC input it cannot attribute.
A device reporting the AC-input direction for any other reason keeps the bare
`outputLimit`. Leaving a charge never waits for state
reconciliation, and a shutdown release that writes once and exits is one
complete command, sent whatever the resend window says. Inside the charge direction the local API sends only
`inputLimit` while the device shows the charge mode this EMS set (`acMode=1`,
`smartMode=1`), and the atomic set again whenever it shows anything else.

A charge is not gated separately from a discharge. What decides whether a
charge may happen at all is the permission set — the feature switch, the
device's own opt-in, the model's established charge path and current telemetry —
not a second write gate. Making the way *back* depend on an extra gate was
considered and rejected: failing closed on the return path would leave hardware
drawing from the grid.

## Zendure outputLimit

`outputLimit` is the normal per-cycle control write. The calculated target can
be filtered, ramped, clamped, deadbanded, and rate-limited before an
`outputLimit` write is attempted. Writes are suppressed for disabled or offline
devices and while inside the configured deadband. One exception: when control
(`system.enabled`) or a single device (runtime `enabled`) is switched off while
the EMS is charging that device, it gets one final command that ends the charge
— the exit to idle, `acMode=2`, `inputLimit=0`, `outputLimit=0`, on its
transport's own gate — and then nothing more. Which device is charging is read
from its transport's record of the EMS's own charge, which ends when the device
reports that it left the charge, not when it answered the command: a device that
did not carry the command out is sent it again once the resend window (30 s) has
passed, and an unreachable device gets it when it answers again (owner decision
2026-10-04). A device whose report shows no direction -- no known `acMode`, no
charging `acStatus` -- shows its charge by grid draw alone, which may be a few
watts it draws idle: after three exits that only such draw followed, the charge
is left to the device once the last resend window has passed, logged as
`ac_charge_left_to_device`. An operator park or a
maintenance claim that takes the charging device from the regulator gets the
same single command — on the local API, for a claim that holds the device in AC
input, as `inputLimit=0` alone, so the reconciler does not move the relay back
for the role — unless the claim commands a charge power of its own that reaches
the device — an AC-input role with `ac_charge_power_w`, on a transport with
state reconciliation and its gate open, and control switched on. That claim
takes the charge over: the EMS's record of it is released, and a stop of the
EMS does not end the claim's charge. A disable does not end an operator's park
either, but the full-charge assist's charge, which the EMS started, gets the
same single exit, on the rules of a transport's own charge record but sent at
most three times per switch-off (owner decision 2026-10-10; see
[battery-full-charge-assist.md](../battery-full-charge-assist.md)). A claim set
while control is off cannot write, so the EMS's charge is ended instead, and
the claim's own power goes out once control is on again.

Expected events:

```text
event=dry_run_output_limit         (no-write, dry-run/simulation/replay/safe mode)
event=write_output_limit_published (live write published to the device transport)
event=write_output_limit_queued    (target queued behind an in-flight MQTT command)
event=write_output_limit_coalesced (repeat of the in-flight target; not republished)
event=write_output_limit_rejected  (write refused: capability/limit/validation)
```

Other relevant events:

```text
control_disabled_skip_write
device_disabled_skip_write
ac_charge_ended_on_disable
ac_charge_handed_to_claim
ac_charge_left_to_device
offline_skip_write
deadband_skip_write
write_output_limit_error
```

## gridOffMode

Offgrid socket intent uses the Zendure `gridOffMode` tri-state mapping:

```text
off      -> 2
eco      -> 1
standard -> 0
```

Off-grid socket mode is a mode/state value, not power. It is written only when
the reconciler runs and only behind the state-reconciliation write gate, and
never over a report that carries no `gridOffMode`: runtime defaults seed
`offgrid_socket_mode` for every device, so nothing marks the operator's choice
apart from the default (K20).

## acMode

Runtime AC mode intent is evaluated during the control loop. Normal output
devices target `acMode=2`; runtime AC input/charge reservations target
`acMode=1` and are excluded from normal output regulation.

The controller writes `acMode` only when telemetry differs from the desired
runtime target, and only over a mode it knows: a report without `acMode`, or
with a value other than `1` or `2`, is an unknown mode, not `0`. Over an
unknown mode neither the default `ac_output` claim nor the full-charge assist
writes it: the assist writes no `acMode` there apart from its restore's exit
to idle and the exits a switch-off, a stop or a release after failed cycles
sends to the charge on its record (see
[socSet and inputLimit](#socset-and-inputlimit)); `unknown_ac_mode` is logged
once per device until the mode is known again. A role the operator set, such
as `emsctl device WR1 ac-mode output`, is still written over an unknown mode,
and over `acMode=1` with active AC charge telemetry, unless a claim above it
holds the device: while the full-charge assist's charge is on its record, the
assist's claim holds the direction over an unknown mode (see
[socSet and inputLimit](#socset-and-inputlimit)). A charge the firmware runs at the floor is held by its own claim,
which the reconciler does not write over (see
[control-logic.md](control-logic.md#who-owns-a-devices-ac-direction)).

`acMode`/`inputLimit` writes for the runtime AC role are owned exclusively by
the runtime intent reconciler. Legacy role names (`normal_output`,
`ac_input_charge`, `reserved`) are accepted defensively and mapped to
`ac_output`/`ac_input`.

## socSet and inputLimit

`socSet` and `inputLimit` are state-reconciliation writes:

- `socSet` — Max-SoC / full-charge-assist target; also used by battery
  full-charge assist (`socSet=1000`, then restore of the configured `socSet`).
- `inputLimit` — AC charge power, reconciled through the runtime AC intent
  reconciler: an operator's AC input role and battery full-charge assist.

Neither is written over a report that does not carry it, nor is `smartMode`;
the one exception is the charge power of an `ac_input` role the operator set,
as for `acMode`. `unknown_state_field` names the device and field once until
the report carries it again (K20). Any other claim's `acMode=1` and its charge
power go out together or not at all: neither over an unknown `inputLimit`, and
the power not over an unknown `acMode`. A full-charge assist owes a restore of
`acMode`, and holds a charge that a stop or a switch-off ends, only once its
`acMode=1` went out -- answered with an error or not answered included, since
the device may have carried it out; a refused one costs one exit to idle at
most -- or a report showed `acMode` 1 under its claim, and never on
a transport without state reconciliation, where a restore owed from before the
device moved there is settled unwritten. A full-charge assist does not start
over a report without `socSet`, or with its AC mode without `acMode` or
`inputLimit`, and a running one claims nothing while the report lacks a field
it still has to write -- `socSet` until its `socSet=1000` went out, `acMode`
and `inputLimit` until it entered AC input -- so it holds no device out of
output control there; its request stays pending (see
[battery-full-charge-assist.md](../battery-full-charge-assist.md#over-a-report-without-a-field-it-writes)).
A charge it entered stays its own, on the record a stop or a switch-off ends,
until a report shows the device out of it; then it needs those fields again as
an assist that never entered. Over a report without `acMode` it holds the
device only while the report shows it charging (`acStatus`); otherwise, with
control on, it sends neither its charge nor an exit, its claim holding only
the direction while the charge is on its record -- no `acMode` of its own, at
the assist's priority -- so no claim below it, a role the operator set
included, writes `acMode` into that charge. Output control it leaves as the
best of those claims has it: with the regulator under the default claim, with
nobody under an operator park; and the device is not eligible for a regulator
charge while the hold stands, so no second record of a charge opens beside the
assist's. Its record stays as it is, an exit already out included. Once the
device left the charge, it claims nothing over such a report.
Its restore never holds the device out of output control for good: over an
unknown `socSet` it stays owed without holding
the device, and the configured `max_soc` goes out at the first report that
carries `socSet` again; over an unknown `acMode`, where nothing writes
`acMode=2`, it is the assist's exit to idle on the power write gate, once per
resend window and three times at most, settled once a report shows the device
left the charge (without `acMode`, only with no grid draw) or the third exit's
resend window has passed (`full_charge_assist_ac_mode_restore_settled`, with
`left_to=device` when it charges on). Behind a shut state write gate, or with
control switched off, no restore holds the device at all: it stays owed,
nothing of it is written -- neither `socSet` nor `acMode=2` nor the exit to
idle -- and it goes out once the gate opens and control is on (K22). Nor
does the assist itself: a due one does not start behind that gate, and one
already running stays active but claims nothing until the gate opens; on an
MQTT control device, which takes no state write at all, it never starts and
claims nothing (K25, see
[battery-full-charge-assist.md](../battery-full-charge-assist.md#behind-a-shut-write-gate)).

These require `allow_state_reconciliation_writes=true`.

## Dry run and disabled states

Simulation, replay, dry-run, preflight, and safe mode never perform normal live
output control writes:

```text
--dry-run
--simulate
--replay
--preflight
```

Preflight reads live telemetry and checks prerequisites without dispatching
control writes. Bounded runs keep live tests short:

```bash
python3 -B ems-solarflow-api-control.py --preflight
python3 -B ems-solarflow-api-control.py --duration 60
python3 -B ems-solarflow-api-control.py --max-cycles 5
```

Simulation and replay never contact hardware:

```bash
python3 -B ems-solarflow-api-control.py --simulate --max-cycles 1
python3 -B ems-solarflow-api-control.py --replay /path/to/trace.jsonl --once
```

A device entry with `"enabled": false` is not part of the control loop and
receives no writes. This holds for every transport: a disabled local-API device
is skipped exactly like a disabled Zendure MQTT control device, and a
non-boolean flag is treated as disabled rather than trusted as enabled.

## Dashboard/authenticated write controls

The read-only dashboard does not perform control writes. Authenticated dashboard
actions and `emsctl.py` runtime-state edits change runtime state only; hardware
writes still pass through the write gates above. See [runtime-state.md](runtime-state.md).

## Admin Console safety boundaries

The Admin Console orchestrates EMS tooling and never duplicates EMS core control
logic:

- EMS remains the source of truth. The Admin Console is UI/orchestration.
- Do not run another controller that writes Zendure `outputLimit` in parallel.
- Admin Console backup/restore and config apply are preview-first and confirmed,
  and back up what they replace before writing.
- The deployment-capable Admin container controls the host Docker engine, which
  is effectively root-equivalent — run it only on a trusted local machine and
  never expose it to the internet.

See [admin-architecture.md](admin-architecture.md) and
[admin-discovery.md](admin-discovery.md) for the full Admin boundaries.
