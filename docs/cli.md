# CLI Tool

`emsctl.py` safely edits `runtime-state.json`.

It does not contact Zendure hardware and does not contact Home Assistant.

## Status

```bash
python3 emsctl.py status
```

With explicit paths:

```bash
python3 emsctl.py --config config.json status
python3 emsctl.py --runtime-state runtime-state.json status
```

## System Commands

```bash
python3 emsctl.py system enable
python3 emsctl.py system disable
python3 emsctl.py system max-power 1600
python3 emsctl.py system loop-interval 5
python3 emsctl.py system min-output-limit 30
```

## HA Commands

```bash
python3 emsctl.py ha enable
python3 emsctl.py ha disable
python3 emsctl.py ha-control enable
python3 emsctl.py ha-control disable
```

`ha` controls runtime HA publishing. `ha-control` controls runtime HA helper
sync. Neither command edits HA URL or token.

## Winter Commands

```bash
python3 emsctl.py winter status
python3 emsctl.py winter enable
python3 emsctl.py winter disable
```

This only toggles winter mode at runtime. Winter SOC targets, months, ramp
timing, and AC charge power remain static `config.json` settings.

## Device Commands

```bash
python3 emsctl.py device WR1 enable
python3 emsctl.py device WR1 disable
python3 emsctl.py device WR1 max-power 800
python3 emsctl.py device WR1 offgrid off
python3 emsctl.py device WR1 offgrid eco
python3 emsctl.py device WR1 offgrid standard
```

## Validation

The CLI rejects invalid input without changing the file:

- unknown device
- negative watt values
- `loop_interval <= 0`
- invalid offgrid value; allowed values are `off`, `eco`, and `standard`
- invalid runtime-state JSON
- unknown command

Examples:

```bash
python3 emsctl.py device UNKNOWN disable
python3 emsctl.py system max-power -1
python3 emsctl.py device WR1 offgrid maybe
```

## Atomic Writes

The CLI writes via a temporary file and atomic rename:

```text
runtime-state.json.<pid>.tmp -> runtime-state.json
```

This keeps runtime-state edits robust even when the EMS is running.
