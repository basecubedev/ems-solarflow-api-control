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

`ems.topology` contains the optional logical inverter topology foundation. It
parses the flat `topology.links` config, validates references against
configured device names, resolves root trees and branch membership, and exposes
JSON/text diagnostics. It is read-only structure today and is not used for
runtime power allocation or hardware writes.
