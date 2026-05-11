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
