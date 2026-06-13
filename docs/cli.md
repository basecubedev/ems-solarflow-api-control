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
python3 emsctl.py diagnose --hardware
python3 emsctl.py diagnose --control
python3 emsctl.py diagnose --control --sample-seconds 30
python3 emsctl.py diagnose --control-quality --sample-seconds 60
python3 emsctl.py diagnose --quality --json
python3 emsctl.py diagnose --json
python3 emsctl.py diagnose --support-bundle
python3 emsctl.py diagnose --support-bundle --output /tmp/ems-support.zip
```

## Diagnose Command Matrix

| Command | Purpose |
| --- | --- |
| `diagnose` | Installation health |
| `diagnose --deep` | Advanced health checks |
| `diagnose --hardware` | Hardware connectivity |
| `diagnose --control` | Explain EMS decisions |
| `diagnose --control-quality` | Evaluate EMS quality |
| `diagnose --support-bundle` | Generate support bundle |

Use `diagnose` first when something is unclear. Use `--control` when EMS is
running but the current output looks surprising. Use `--control-quality` when
the system regulates but export/import, PV usage, or SOC balancing looks poor
over time. Use `--support-bundle` before opening a GitHub issue or forum
support request.

Modes:

- `--json` prints the same result structure as machine-readable JSON. The
  diagnose API contract is versioned with top-level `schema_version: 1`.
- Normal diagnose includes the optional topology model. Disabled topology is
  reported as disabled; enabled topology is validated and printed with its
  resolved tree and branch membership.
- `--deep` adds local operational checks: runtime-state plausibility, SQLite
  integrity/table summaries, recent configured log patterns, Docker host hints,
  and a dashboard loopback check when enabled.
- `--hardware` performs explicit short-timeout read-only network probes for
  configured grid meters and Zendure read endpoints. It never writes hardware
  state.
- `--control` explains the current regulation path from local config and
  runtime-state: grid power, filtered grid power, target output, final output,
  deadband state, device allocation, SOC protection, write-path blockers, and
  likely root causes.
- `--sample-seconds N` can be combined with `--control` to collect local
  runtime-state meter samples. The output reports average/min/max, standard
  deviation, sign changes, and stale/noisy meter hints.
- `--control-quality` or `--quality` evaluates real operation over local
  samples: export/import quality, a coarse regulation quality score, PV usage
  plausibility, SOC balancing, and higher-level root-cause hints.
- `--support-bundle` creates a redacted ZIP with a stable file layout:
  `diagnosis.json`, `diagnosis.txt`, `control-diagnostics.json`,
  `control-diagnostics.txt`, `control-quality.json`, `control-quality.txt`,
  `redacted-config.json`, `runtime-state.json`, and `bundle-metadata.json`.

Control interpretation:

- `Control disabled` means runtime control is explicitly off.
- `Dry run enabled` means EMS calculates targets but skips hardware writes.
- `Deadband active` means the filtered meter value is inside the configured
  threshold and output may be held.
- `Grid meter signal appears noisy` means frequent sign changes or high
  variance were observed in local samples.
- `Minimum SOC protection active` means at least one device is at or below its
  minimum SOC.

Control quality interpretation:

- The quality score is a coarse support indicator from 0 to 100, not a
  certified measurement. It starts at 100 and subtracts bounded penalties for
  average grid deviation, export duration, export peaks, and large import
  peaks.
- `excellent`, `good`, `acceptable`, `poor`, and `critical` are intended for
  triage. Use the detailed export/import metrics to understand the cause.
- Export peaks mean grid power went negative during the sample window. Short
  small peaks can be normal; long or large peaks indicate the zero-export
  target is not being held consistently.
- PV diagnostics can show that PV telemetry is missing, PV is likely limited by
  system/device limits, or PV is available but not used. It cannot prove a
  hardware fault without additional read-only hardware checks.
- SOC balancing warnings mean the SOC spread is high, a low-SOC device is
  contributing more than expected, or a device is protected by minimum SOC.

Exit codes:

```text
0  diagnose status is ok or warning
1  at least one diagnostic error was found
2  invalid CLI usage
```

Before opening an issue:

```bash
python3 emsctl.py diagnose --support-bundle
```

Attach the generated ZIP. The bundle is redacted and includes the diagnostic
summary, redacted config/runtime-state snapshots, bundle metadata, and
control/quality diagnostics. Control files are present even when the
corresponding mode was not enabled so automated tooling can rely on the same
file names.

Machine-readable root causes always use this shape:

```json
{
  "code": "control_disabled",
  "severity": "warning",
  "title": "Control disabled",
  "message": "Control disabled",
  "suggested_next_check": "Review the related diagnose section for details."
}
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
docker compose exec ems python3 emsctl.py status
docker compose exec ems python3 emsctl.py interactive
docker compose exec ems python3 emsctl.py dashboard auth-status
docker compose exec ems python3 emsctl.py diagnose
docker compose exec ems python3 emsctl.py diagnose --control
docker compose exec ems python3 emsctl.py diagnose --control-quality --sample-seconds 60
docker compose exec ems python3 emsctl.py diagnose --deep
docker compose exec ems python3 emsctl.py diagnose --support-bundle
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
