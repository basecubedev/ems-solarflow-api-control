# CLI Tool

`emsctl.py` safely edits the configured runtime-state file.

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
python3 emsctl.py diagnose
```

## Diagnose

`diagnose` is designed for local support/debug output. Normal diagnose is
read-only and does not contact Zendure hardware, Home Assistant, MQTT, Shelly,
or other external services.

```bash
python3 emsctl.py diagnose
python3 emsctl.py diagnose --deep
python3 emsctl.py diagnose --json
python3 emsctl.py diagnose --support-bundle
```

Modes:

- `--json` prints the same result structure as machine-readable JSON.
- `--deep` adds local operational checks: runtime-state plausibility, SQLite
  integrity/table summaries, recent configured log patterns, Docker host hints,
  and a dashboard loopback check when enabled.
- `--hardware` performs explicit short-timeout read-only network probes for
  configured grid meters and Zendure read endpoints. It never writes hardware
  state.
- `--support-bundle` creates a redacted ZIP with `diagnose.txt`,
  `diagnose.json`, redacted config/runtime-state snapshots, recent redacted log
  lines, and project metadata.

Exit codes:

```text
0  diagnose status is ok or warning
1  at least one diagnostic error was found
2  invalid CLI usage
```

## Config Discovery

By default, `emsctl.py` uses this config lookup order:

```text
--config PATH
EMS_CONFIG_FILE
config.json
config/config.json
```

This preserves legacy local setups that keep `config.json` next to
`emsctl.py`, while allowing the recommended Docker setup to use
`/app/config/config.json` automatically.

Relative `runtime_state_path` and dashboard `auth_file` values are still
resolved relative to the application directory. The runtime-state path always
comes from the selected config unless `--runtime-state` is passed explicitly.

## Docker Usage

With the recommended Compose service name `ems`, common commands can be run
without an explicit config path:

```bash
docker compose exec ems python emsctl.py status
docker compose exec ems python emsctl.py interactive
docker compose exec ems python emsctl.py dashboard auth-status
docker compose exec ems python emsctl.py diagnose
docker compose exec ems python emsctl.py diagnose --deep
docker compose exec ems python emsctl.py diagnose --support-bundle
```

For unusual mounts or troubleshooting, an explicit config path still works:

```bash
docker compose exec ems python emsctl.py --config /app/config/config.json status
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
- same atomic runtime-state writes

## Examples Command

`examples` prints a longer read-only cookbook grouped by topic:

```bash
python3 emsctl.py examples
```

This command does not create or modify the runtime-state file.

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
python3 emsctl.py --runtime-state data/runtime-state.json status
```

`status` creates the configured runtime-state file from config defaults when
the file is missing.

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
data/runtime-state.json.<pid>.tmp -> data/runtime-state.json
```

This keeps runtime-state edits robust even when the EMS is running.
