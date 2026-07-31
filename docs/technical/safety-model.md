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
state reconciliation; the MQTT transports are output-only). Do not run Zendure
HEMS, Home Assistant automations, MQTT writers, or any other controller in
parallel if they write Zendure `outputLimit`. EMS assumes exclusive write control
over `outputLimit` while active. The EMS must not run in parallel with another
controller writing Zendure `outputLimit`.

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
winter `inputLimit`, full-charge-assist `socSet`/`acMode`/`inputLimit`)
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

Runtime control may write:

```text
outputLimit
```

State reconciliation may write:

```text
minSoc
socSet
smartMode
gridOffMode for explicit offgrid socket intent
inputLimit only during winter recovery reconciliation
socSet=1000 / configured socSet restore during battery full-charge assist
acMode/inputLimit during battery full-charge assist only through runtime intent
```

Runtime output writes and persistent state reconciliation writes are separate
write paths. Output-limit writes require the device's transport gate to be
enabled; state reconciliation writes additionally require
`allow_state_reconciliation_writes=true`. State reconciliation is API-only:
Zendure MQTT control devices are output-only and are skipped by every state
reconciliation writer.

## Zendure outputLimit

`outputLimit` is the normal per-cycle control write. The calculated target can
be filtered, ramped, clamped, deadbanded, and rate-limited before an
`outputLimit` write is attempted. Writes are suppressed for disabled or offline
devices and while inside the configured deadband.

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
the reconciler runs and only behind the state-reconciliation write gate.

## acMode

Runtime AC mode intent is evaluated during the control loop. Normal output
devices target `acMode=2`; runtime AC input/charge reservations target
`acMode=1` and are excluded from normal output regulation.

The controller writes `acMode` only when telemetry differs from the desired
runtime target. Automatic startup reconciliation remains conservative: unknown
`acMode` values are not forced back to normal output automatically, and firmware
recovery/charge blockers are still honored before startup returns a device to
`acMode=2`. An explicit runtime output command, such as
`emsctl device WR1 ac-mode output`, may still write `acMode=2` from reported
`acMode=0` or from `acMode=1` with active AC charge telemetry.

`acMode`/`inputLimit` writes for the runtime AC role are owned exclusively by
the runtime intent reconciler. Legacy role names (`normal_output`,
`ac_input_charge`, `reserved`) are accepted defensively and mapped to
`ac_output`/`ac_input`.

## socSet and inputLimit

`socSet` and `inputLimit` are state-reconciliation writes:

- `socSet` — Max-SoC / full-charge-assist target; also used by battery
  full-charge assist (`socSet=1000`, then restore of the configured `socSet`).
- `inputLimit` — AC charge power, reconciled during winter recovery and during
  battery full-charge assist through the runtime AC intent reconciler.

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
