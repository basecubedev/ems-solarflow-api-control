# Topology

Topology is an optional logical description of inverter branches. It is a data
model foundation for future hierarchical control and currently does not change
runtime control behavior.

When `topology.enabled=false` or the section is missing, the EMS keeps the
existing parallel behavior.

`config.template.json` must ship `topology.enabled` as `false`. Examples with
`enabled=true` are documentation examples only.

## Scope

Topology describes structure only:

- root-level devices that operate directly at house/grid level
- source devices that logically feed into a target device or subsystem
- nested branches for diagnostics and future control decisions

Topology does not duplicate device settings. Keep max power, IP addresses,
serial numbers, names, PV size, battery capacity, SOC settings, and runtime
limits in `devices`.

Topology currently does not implement serial inverter power control, battery
bypass coordination, AC charging coordination, idle/wake behavior, or
branch-level power allocation.

Current status: topology is a foundation for a future bundled feature. It is
intentionally structure-only and should be merged to main together with
topology-aware control behavior and dashboard visualization, not as a
standalone runtime feature.

## Example

```json
{
  "topology": {
    "enabled": true,
    "root_mode": "parallel",
    "root_devices": ["inverter_1", "inverter_5", "inverter_6"],
    "links": [
      {
        "sources": ["inverter_2", "inverter_3"],
        "target": "inverter_1",
        "mode": "parallel"
      },
      {
        "sources": ["inverter_4"],
        "target": "inverter_2",
        "mode": "single"
      }
    ]
  }
}
```

Resolved structure:

```text
inverter_1
├─ inverter_2
│  └─ inverter_4
└─ inverter_3
inverter_5
inverter_6
```

Branch membership is deterministic: target first, then sources in config order,
recursively including nested sources before later siblings.

```text
inverter_1: inverter_1, inverter_2, inverter_4, inverter_3
inverter_2: inverter_2, inverter_4
root: inverter_1, inverter_5, inverter_6
```

## Validation

Strict validation runs only when `topology.enabled=true`.

- `root_mode` must be `parallel`.
- `root_devices` must be a non-empty list.
- every root device must exist in `devices`.
- `links` must be a list.
- every link must have non-empty `sources`, string `target`, and `mode` as
  `single` or `parallel`.
- `mode=single` must have exactly one source.
- all sources and targets must exist in `devices`.
- a device may not appear twice as a source.
- a source must not also be a root device.
- every source must resolve into one configured root device.
- cycles and self-links are rejected.
- duplicate device ids in `root_devices` or one link's `sources` are rejected.
- a target may only appear once in `topology.links`; group all sources for the
  same target into one link.

Good:

```json
{
  "sources": ["inverter_2", "inverter_3"],
  "target": "inverter_1",
  "mode": "parallel"
}
```

Bad:

```json
{
  "sources": ["inverter_2"],
  "target": "inverter_1",
  "mode": "single"
},
{
  "sources": ["inverter_3"],
  "target": "inverter_1",
  "mode": "single"
}
```

Duplicate target links make branch ordering and mode semantics ambiguous.

Use `python3 emsctl.py diagnose` or `python3 emsctl.py diagnose --json` to
inspect the configured and resolved topology.
