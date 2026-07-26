# MQTT hardware control probe (write latency + effectiveness)

A developer tool that answers two questions: **does a power command written over
MQTT actually take effect on the inverter, and how fast?**

It publishes the **exact production command** the EMS control loop would build
(the atomic model-specific property set — `smartMode`/`acMode`/`outputLimit`/
`inputLimit` for ZenSDK models — at QoS 1, never retained, on the `iot/…`
topic) and reads the state back over the device's **local HTTP API**, polled
fast. This sidesteps the ~30 s MQTT telemetry cadence: the HTTP
`/properties/report` endpoint can be read several times per second, so the
measured latency is the real end-to-end time from *"publish to broker"* to *"new
`outputLimit` visible on the device"*, bounded only by the poll interval instead
of the telemetry report period.

Tool: [`scripts/mqtt_write_latency_probe.py`](../../scripts/mqtt_write_latency_probe.py).
Tests: [`tests/test_mqtt_write_latency_probe.py`](../../tests/test_mqtt_write_latency_probe.py)
(deterministic, no hardware).

This tool writes real `outputLimit` values to real power hardware. Read the
[Safety](#safety) section before running it. See also [../user/safety.md](../user/safety.md).

## What it needs (and what it does not)

You do **not** pass an API key. You **select the inverter by its local HTTP API
IP** (`--api-ip`), and everything else comes from `config.json`:

- **Pass `--api-ip <ip>` to pick the inverter under test.** The probe reads the
  serial from that inverter's local HTTP API and selects the MQTT control device
  in `config.json` with the same serial. The IP therefore identifies the device
  unambiguously — it works the same with one MQTT device or twenty. (If the same
  inverter also has an HTTP device entry, the probe can auto-match by serial with
  no `--api-ip`; an MQTT-only inverter needs `--api-ip`.)
- **Selection is fail-closed.** If the serial (or any selector) matches more than
  one configured device, the probe **aborts before connecting or publishing** and
  prints a redacted candidate list (name, source, hardware profile, broker
  reference, last-4 of serial/device id) — it never silently writes to the first
  match. Narrow it with `--device-name`, `--serial`, `--device-id` or
  `--broker-ref`. A config carrying duplicate device identities or names is
  refused up front (the same guard the live EMS enforces).
- **HTTP read-back — no key.** The local SolarFlow HTTP API
  (`http://<inverter-ip>/properties/report`) is unauthenticated. The probe reads
  it exactly the way the EMS does (`ZendureClient.fetch()`), with no token.
- **MQTT broker + credentials come from `config.json`.** Cloud MQTT resolves its
  Zendure cloud credentials, local MQTT its broker host (and optional
  credentials), the same way the running EMS does. Nothing is passed on the
  command line.

So a complete install needs the *same physical inverter reachable two ways*: over
MQTT (control) and over the local HTTP API (read). The 800 Pro 2 is — MQTT for the
write, local HTTP for the read-back.

## Safety

The probe refuses to write unless every condition below holds, and it cleans up
after itself:

- **Stop the EMS control loop first.** Two controllers writing `outputLimit` to
  one device is forbidden and also corrupts the measurement (the EMS would
  overwrite the test value within a loop). Only the `ems` service is an
  `outputLimit` writer — the Admin container does not write `outputLimit` and can
  stay up (just don't run a maintenance/apply during the test). The probe includes
  a **contention check**: if a foreign writer changes the value between samples,
  the run aborts instead of reporting polluted numbers.
- **`--confirm-writes` is required** to publish real values. Without it the probe
  resolves everything and stops.
- **The transport write gate must be satisfied** (`allow_mqtt_zendure_control_writes`
  for cloud, `allow_mqtt_local_control_writes` for local). The probe reads the
  same gate the EMS enforces and refuses when it is off.
- **`system.dry_run` is respected.** If your config has `dry_run: true` (or omits
  it — the default is dry-run), the probe refuses to write, exactly like the EMS.
  A live control install already runs with `dry_run: false`; that is what lets the
  probe measure. `--confirm-writes` does not override `dry_run`.
- **The complete initial power state is restored** — `smartMode`, `acMode`,
  `outputLimit` **and** `inputLimit`, captured before the first write — through
  the production property-write path on success, failure, timeout and
  interruption, then **verified over HTTP by property type**: watt-like values
  (`outputLimit`/`inputLimit`) within tolerance, mode/enum values (`smartMode`/
  `acMode`) **exactly**. `restore_verified` is reported only when *every* captured
  property is restored and verified. If the atomic property write is rejected and
  the probe falls back to an `outputLimit`-only write, that is reported as
  `restore_partial` — **never** a full restore. A partial, failed, or unattempted
  restore makes the run exit non-zero.
- **A non-restorable initial state fails preflight before the first write.**
  Before publishing anything, the probe verifies the selected profile can restore
  every captured mode-changing property (e.g. an initial `acMode` outside the
  profile's writable range). If it cannot, the probe reports `preflight_failed`
  and refuses to write.
- **Mode-changing tests need double confirmation.** `--mode-test` additionally
  requires `--confirm-mode-changes`, and it only runs when the device is
  currently *not* in smart AC output mode — the probe never forces a device out
  of output mode.
- **A retained control command is refused outright** (a retained setpoint would
  replay on every broker reconnect).
- **`config.json` is never modified.** The startup config-upgrade write step is
  skipped, and no control loop is started.

## What the number means

The probe reports **four distinct timing dimensions** — it never labels one as
another:

- **local submit** — time to hand the command to the Paho MQTT client. Local
  submission only; it is *not* broker delivery.
- **broker delivery** — the observed QoS 1 PUBACK for the publish
  (`delivered` / `timeout` / `untracked` when the transport exposes no message
  id). Broker delivery is *not* device acceptance.
- **setpoint HTTP-match** — MQTT publish → the **target** value visible on the
  device's local HTTP API. Only samples whose observed `outputLimit` actually
  *matched* the target (within `--match-tolerance`) count; movement toward the
  target is reported separately and never counts as a match or a latency sample.
- **physical output** — time for real output to react (with `--verify-output`).

"Setpoint landed" means `abs(observed - target) <= match_tolerance`, never merely
that the value moved away from the baseline. The measurement resolution equals
`--poll-interval`, so the true latency lies within one poll interval below the
reported value.

Why the HTTP read-back rather than an MQTT signal: the cloud-MQTT ZenSDK
`properties/write` profile (used by the 800 Pro 2) has **no command
acknowledgement**, and its only telemetry echo arrives with the periodic report
(~30 s). Measuring the MQTT echo would measure the report cadence, not the command
latency. Only the legacy `function/invoke` (hub/object) profile has a real ACK.

## Running it

The probe needs the same runtime the EMS has: Python with `requests` and
`paho-mqtt`, the `config.json` broker credentials and device IP/serial, and
network access to the broker and the inverter. Select the inverter with
`--api-ip <ip>` and append one of the three command shapes:

- `--api-ip <ip> --dry-preview` — resolve the device and print the full
  operation plan (the selected device, the **canonical effective write topic**
  and whether an obsolete `mqtt.write_topic` override is present and ignored, QoS,
  retain, exact properties, effective gates, the current
  `smartMode`/`acMode`/`inputLimit`/`outputLimit` state, the restorable and any
  non-restorable initial properties, and the single-writer advisory), writing
  nothing.
- `--api-ip <ip> --confirm-writes --poll-interval 1 --samples 12` — setpoint
  landing test: measure publish-to-target-visible latency (only a real target
  match counts) and verify the full commanded mode set landed with each sample.
- `--api-ip <ip> --confirm-writes --verify-output` — additionally classify the
  physical reaction per sample: `output_reacted`,
  `no_output_possible_soc_at_minimum` (setpoint landed but conditions allow no
  output) or `not_reacted`. A landed setpoint alone is **never** reported as
  "power control works".
- `--api-ip <ip> --mode-test --confirm-writes --confirm-mode-changes` — mode
  recovery test: from a non-output mode, prove the atomic command switches the
  required mode and lands the target (`mode_and_setpoint_verified` requires every
  expected property — `smartMode`/`acMode`/`outputLimit`/`inputLimit` — to match
  by type), then restore the initial state.
- `--api-ip <ip> --confirm-writes --poll-interval 1 --markdown` — measure and print the docs table.

A 1 s poll reports latency in 1 s steps. For sub-second resolution lower
`--poll-interval` (e.g. `0.25`), at the cost of more HTTP requests per run.

### On a Docker Compose install (no source checkout)

The script is shipped inside the EMS image, so run it as a one-off container from
the *same image* your EMS already uses — it inherits the config mount, credentials
and network. Only the `ems` service is stopped; the broker and inverter stay
reachable.

```bash
# 1. stop only the EMS control loop (single writer)
docker compose stop ems

# 2. dry run — resolves the device, writes nothing (replace 192.168.1.50 with the inverter IP)
docker compose run --rm --no-deps --entrypoint python3 ems \
  scripts/mqtt_write_latency_probe.py --api-ip 192.168.1.50 --dry-preview

# 3. measure and emit the docs table
docker compose run --rm --no-deps --entrypoint python3 ems \
  scripts/mqtt_write_latency_probe.py --api-ip 192.168.1.50 --confirm-writes --poll-interval 1 --markdown

# 4. restart the EMS
docker compose start ems
```

Adjust the service name (`ems`) to your compose file. The one-off container
inherits the `ems` service's volumes and network, so `config.json` and the
broker/inverter are reachable as usual; if `config.json` is not auto-resolved add
`--config /app/config/config.json`.

If your running image predates this script (it was added later), inject it into
the one-off container. Mount a **directory**, not the single file — single-file
bind mounts fail on some storage drivers (notably the `zfs` graph driver, with
`create mount destination … not a directory`). Put the script in its own host
directory and mount that over `/app/scripts`:

```bash
mkdir -p probe
# place the real script as probe/mqtt_write_latency_probe.py:
#   scp it from a checkout, or curl it from the project's scripts/ once pushed
docker compose run --rm --no-deps \
  -v "$PWD/probe:/app/scripts:ro" \
  --entrypoint python3 ems \
  scripts/mqtt_write_latency_probe.py --api-ip 192.168.1.50 --confirm-writes --poll-interval 1 --markdown
```

The container's working directory is `/app`, so `scripts/…` resolves from the
mounted directory and `import ems` still finds `/app/ems`.

### From a source checkout

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
docker compose stop ems   # or otherwise stop the running EMS writer
python3 scripts/mqtt_write_latency_probe.py --api-ip 192.168.1.50 --dry-preview
python3 scripts/mqtt_write_latency_probe.py --api-ip 192.168.1.50 --confirm-writes --poll-interval 1 --markdown
```

### Options

| Flag | Default | Purpose |
| --- | --- | --- |
| `--confirm-writes` | off | Required to publish real `outputLimit` values. |
| `--dry-preview` | off | Resolve devices and print the plan without writing. |
| `--markdown` | off | Also print a documentation-ready results table. |
| `--config PATH` | resolved like the EMS | Path to `config.json`. |
| `--api-ip IP` | — | Local HTTP API IP of the inverter to test; the probe reads its serial and selects the matching MQTT control device. The recommended way to pick the device. |
| `--device-name NAME` (alias `--device`) | the only one | Select the MQTT control device by name (disambiguates when several match). |
| `--serial SN` | — | Select the MQTT control device by physical serial. |
| `--device-id ID` | — | Select the MQTT control device by MQTT device id. |
| `--broker-ref REF` | — | Select the MQTT control device by broker reference. |
| `--api-device NAME` | matched by serial | HTTP device entry for the read-back when not using `--api-ip` (auto-matched by serial otherwise). |
| `--values A B` | `200 500` | Two `outputLimit` setpoints to toggle between (W). |
| `--samples N` | `10` | Number of write/observe samples. |
| `--poll-interval S` | `1.0` | HTTP poll interval, also the measurement resolution. |
| `--timeout S` | `20` | Per-sample wait for the value to land. |
| `--settle S` | `2.0` | Pause between samples. |
| `--match-tolerance W` | `5` | Watts of jitter tolerated when detecting the change. |
| `--connect-timeout S` | `15` | Wait for the MQTT broker to connect. |
| `--no-contention-check` | check on | Do not abort when a foreign writer changes the value. |
| `--verify-output` | off | After a landed setpoint, classify the physical output reaction. |
| `--output-timeout S` | `30` | Wait for the physical output to react. |
| `--output-tolerance W` | `50` | Tolerance for the physical output check. |
| `--mode-test` | off | Mode recovery test (device must currently be in a non-output mode). |
| `--confirm-mode-changes` | off | Required (with `--confirm-writes`) for mode-changing tests. |

The two `--values` must differ and must stay within the device `max_power`; the
probe toggles between them so every sample is a clearly detectable change (robust
to the device rounding or clamping the setpoint).

## Recording results

`--markdown` prints a table with samples **matched** (only target matches count),
movement-only samples, broker-delivered count, the setpoint HTTP-match latency
min / p50 / p95 / max, the local-submit and broker-delivery p50s, and the poll
resolution. Paste that block into the measured-results location and link it from
wherever the number is cited. Always keep the caveat line the tool prints: the
setpoint value is *MQTT publish → target visible on HTTP* (matched samples only),
its resolution equals the poll interval, and it was measured with the EMS stopped.

Do not claim the Cloud MQTT path works unless the canonical topic was used, the
broker delivered, the setpoint matched, the required mode properties matched, and
the restore verified. Physical output may be classified separately when
battery/grid conditions make output impossible.

## See also

- [testing.md](testing.md) — the offline test suite and compile checks.
- [../technical/control-logic.md](../technical/control-logic.md) — where
  `outputLimit` writes sit in the control pipeline.
- [../user/safety.md](../user/safety.md) — the write-gate and safety model.
