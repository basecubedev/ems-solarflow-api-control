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

## Device Commands

```bash
python3 emsctl.py device WR1 enable
python3 emsctl.py device WR1 disable
python3 emsctl.py device WR1 max-power 800
python3 emsctl.py device WR1 offgrid on
python3 emsctl.py device WR1 offgrid off
```

## Validation

The CLI rejects invalid input without changing the file:

- unknown device
- negative watt values
- `loop_interval <= 0`
- invalid offgrid value
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

