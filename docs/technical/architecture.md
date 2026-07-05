# Architecture

The user-facing entry point is:

```bash
python3 ems-solarflow-api-control.py
```

The EMS keeps the operating model intentionally small:

```text
one start script, one static config
```

`config.json` remains the central static installation configuration.
`runtime-state.json` is not a second static config. It is mutable local runtime
state created and updated by the EMS and by `emsctl.py`.

The release template is standalone-first: Home Assistant is disabled by
default, normal Zendure `outputLimit` writes are enabled, and required
regulation/state reconciliation is enabled after local configuration. This is a
practical starting point for standalone operation, not a universal safety
profile; operators still need to review device limits, SOC limits, Shelly
readings, and installation-specific constraints. Home Assistant status
publishing and helper reads can be enabled manually with `ha.enabled=true` and
`ha.control_enabled=true`.

## Code Structure

The entry script performs bootstrap and coordination only:

- CLI parsing
- config loading
- logging setup
- client construction
- runtime-state construction
- controller startup
- main loop handling

The implementation lives in internal modules under `ems/`.

This preserves the operational model of one start script and one static config
while avoiding a large monolithic source file.

## Runtime AC Mode Intent

The controller derives a runtime AC mode intent for each device before normal
output allocation. `ac_output` maps to Zendure `acMode=2` and allows normal
output regulation. `ac_input` maps to `acMode=1` and excludes the device from
normal `outputLimit` regulation.

`acMode` writes are owned by the runtime intent reconciler. Optional runtime AC
charge power is stored per device as `ac_charge_power_w` and is reconciled as
Zendure `inputLimit` only while the runtime role is `ac_input`. Both values are
compared against current telemetry and written only when they differ, using the
existing runtime `/properties/write` path and the normal write gates. The
startup AC mode reconcile path delegates to this same owner so there is not a
second blind writer.

When startup reconciliation targets normal output and the reported `acMode` is
not a known value (`1` or `2`), the controller logs `unknown_ac_mode` and skips
the write instead of forcing output mode from unsupported or missing telemetry.
Explicit runtime output intent, for example `emsctl device WR1 ac-mode output`,
may write `acMode=2` from reported `acMode=0`.
Legacy runtime role names from the development branch are accepted defensively:
`normal_output` is treated as `ac_output`, while `ac_input_charge` and
`reserved` are treated as `ac_input` so older blocked states never become output
providers silently.

`ac_charge_power_w` may remain in runtime-state while the role is `ac_output`;
it does not drive charging until the role becomes `ac_input`.

## Battery Full-Charge Assist

Battery full-charge assist is an optional controller lifecycle feature. It uses
`ems/state_store.py` and the core SQLite database configured by
`battery_full_charge_assist.state_database_path`; it does not depend on the
dashboard database.

The controller processes fresh device telemetry before capability filtering and
target calculation. Passive tracking records battery devices, last seen
firmware state, and `socLimit == 1` Max-SoC events. Active assist and restore
use the same safe write helpers as normal reconciliation: `socSet=1000` during
assist, current config `devices[].max_soc` during restore, and the existing
runtime AC intent reconciler for AC input/output mode transitions.

Completion is intentionally narrow: an active assist completes only when
firmware reports `socLimit == 1`. SOC percentage and configured `max_soc` are
not completion thresholds.
