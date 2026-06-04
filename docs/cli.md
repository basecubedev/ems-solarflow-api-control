# CLI Tool

`emsctl.py` safely edits `runtime-state.json`.

It does not contact Zendure hardware and does not contact Home Assistant.

## Quickstart

Running the CLI without arguments prints a short start screen:

```bash
python3 emsctl.py
```

Start with the interactive menu, built-in help, or the command cookbook:

```bash
python3 emsctl.py interactive
python3 emsctl.py --help
python3 emsctl.py examples
```

Common runtime edits:

```bash
python3 emsctl.py status
python3 emsctl.py system disable
python3 emsctl.py system max-power 1200
python3 emsctl.py device WR1 max-power 600
python3 emsctl.py device WR1 offgrid eco
python3 emsctl.py winter enable
python3 emsctl.py dashboard auth-status
```

Each command group also has focused help:

```bash
python3 emsctl.py system --help
python3 emsctl.py device --help
python3 emsctl.py dashboard --help
```

## Interactive Mode

`interactive` opens a dependency-free menu for common runtime edits:

```bash
python3 emsctl.py interactive
```

Alias:

```bash
python3 emsctl.py menu
```

The menu works directly with Python standard input/output and does not require
Bash or Zsh completion setup. It can show status, edit system limits, toggle HA
publishing/helper control, toggle winter mode, edit configured devices, and show
or manage dashboard authentication.

Interactive mode preserves the same safety rules as direct commands:

- no Zendure hardware access
- no Home Assistant access
- no dashboard server access
- no plaintext password echo
- same value validation
- same atomic `runtime-state.json` writes

## Examples Command

`examples` prints a longer read-only cookbook grouped by topic:

```bash
python3 emsctl.py examples
```

This command does not create or modify `runtime-state.json`.

## Shell Completion

Completion is optional and generated without third-party packages. It is not
required for interactive mode.

Bash, current shell:

```bash
source <(python3 emsctl.py completion bash)
```

Bash, persistent user install:

```bash
mkdir -p ~/.local/share/bash-completion/completions
python3 emsctl.py completion bash > ~/.local/share/bash-completion/completions/emsctl
```

Zsh, current shell:

```bash
source <(python3 emsctl.py completion zsh)
```

The generated completion covers top-level commands, command actions, dashboard
subcommands, offgrid modes (`off`, `eco`, `standard`), and configured device
names when `config.json` is readable. Completion generation does not create or
modify `runtime-state.json`.

Use explicit config paths when generating completion for a non-default
installation:

```bash
python3 emsctl.py --config /etc/ems/config.json completion bash
python3 emsctl.py --config /etc/ems/config.json completion zsh
```

## Status

```bash
python3 emsctl.py status
```

With explicit paths:

```bash
python3 emsctl.py --config config.json status
python3 emsctl.py --runtime-state runtime-state.json status
```

`status` creates `runtime-state.json` from config defaults when the file is
missing.

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
python3 emsctl.py device WR1 pv-priority-factor 1.3
python3 emsctl.py device WR2 pv-priority-factor 0.7
python3 emsctl.py device WR1 offgrid off
python3 emsctl.py device WR1 offgrid eco
python3 emsctl.py device WR1 offgrid standard
```

`pv-priority-factor` adjusts the device's PV-first allocation weight at
runtime. Values above `1.0` increase the device's PV-first share, values below
`1.0` reduce it. The value is still limited by real PV availability, device
state, SOC logic, and configured power limits.

## Dashboard Auth Commands

Dashboard write mode is disabled until a local admin password is configured:

```bash
python3 emsctl.py dashboard set-password
python3 emsctl.py dashboard change-password
python3 emsctl.py dashboard disable-auth
python3 emsctl.py dashboard auth-status
```

Passwords are prompted without echo. The password file contains only
PBKDF2-SHA256 hash metadata and no plaintext password. `disable-auth` removes
the password file and makes dashboard write mode unavailable again.

Hidden password automation flags exist for tests and non-interactive automation
but are intentionally omitted from normal help. Do not use them for interactive
terminal sessions because shell history and process listings can expose command
arguments.

## Validation

The CLI rejects invalid input without changing the file:

- unknown device
- negative watt values
- missing or invalid `pv-priority-factor`
- `pv-priority-factor < 0.01`
- `loop_interval <= 0`
- invalid offgrid value; allowed values are `off`, `eco`, and `standard`
- invalid runtime-state JSON
- unknown command
- dashboard password confirmation mismatch
- wrong dashboard current password

Examples:

```bash
python3 emsctl.py device UNKNOWN disable
python3 emsctl.py system max-power -1
python3 emsctl.py device WR1 pv-priority-factor 0
python3 emsctl.py device WR1 offgrid maybe
```

## Atomic Writes

The CLI writes via a temporary file and atomic rename:

```text
runtime-state.json.<pid>.tmp -> runtime-state.json
```

This keeps runtime-state edits robust even when the EMS is running.
