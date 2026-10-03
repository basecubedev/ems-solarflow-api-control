# Review coverage

A living record of what whole-system code reviews have looked at, what they
found, and what is still open. Read it before starting a review so the next one
spends its time where nobody has looked yet, and update it in the same branch
as the review's fixes.

How to use it:

- **Before a review**, pick areas from [Not yet reviewed](#not-yet-reviewed)
  and from the open register. An area marked reviewed is not "done forever";
  re-review it when its code changed substantially since the listed base.
- **During a review**, give each finding an ID (`<area letter><number>`, for
  example `K12`), and record it as fixed (with the commit subject) or open
  (with severity, size and a direction).
- **After a review**, move what the review covered into the coverage table with
  its base revision, and keep the open register honest: an item leaves it only
  with a commit that fixes it or a decision that it is not a defect.

Severity: **high** can harm hardware, data or security; **medium** is wrong
behaviour an operator will meet; **low** is friction or a rare edge. Size:
**small** fits one focused commit with a test; **large** needs a design
decision first.

## Reviews

| # | Date | Base | Branch | Method |
|---|---|---|---|---|
| 1 | 2026-10-02 | `origin/main` at `c47efe3b` | `review/full-system-review` | Round 1: eight parallel read-only reviewers per area, every finding re-verified and fixed contract-first. Round 2: the areas round one did not reach, plus an adversarial review of the round-one diff (its nine regressions were fixed before merge). Round 3: MQTT control a second time. Fixes from rounds 2–3 were made by per-area agents in separate worktrees and each re-verified. Round 4: an adversarial review of every round 2–3 commit and of the amendments folded into round 1 after its review; each finding needed a reproduction. Round 5: the same for the round 4 corrections. Round 6: the same for the round 5 corrections. Rounds 7 onwards: the same for each following correction, until a round found nothing |

## Coverage

Area letters match the finding IDs below.

| Area | Reviewed in round 1 | Depth |
|---|---|---|
| **K** EMS control core | `ems/controller.py` (`run_once` whole, stabilisation and filter, device ramp, telemetry freshness, command blocking, night/minSoC idle, runtime accessors, runtime intents, AC charge power, all full-charge assist methods, HA sync, `set_output_limit`, SoC/winter/mode reconciliation), `ems/target_control.py` (`battery_presence` to `calculate_targets`), `ems/runtime_intents.py`, `ems/runtime_state.py`, `ems/state_store.py` (schema, CRUD, observations), `ems/simulation.py` (simulated clients, `run_frames`), entry script main loop, write-gate functions in `ems/config.py` | Read in full, probes for every confirmed finding |
| **C** EMS config, clients, MQTT | `ems/clients.py` whole; `ems/config.py` loading, defaults, migrations, startup upgrade, atomic write, placeholder safety, gates and projections, MQTT port/TLS, grid-meter MQTT resolution; `ems/config_mutation.py`; `ems/models.py`, `ems/logging_utils.py`, `ems/health.py`; `ems/mqtt_credentials.py`; `ems/zendure_mqtt/` `client.py`, `control.py`, `control_runtime.py`, `snapshot.py`, `payloads.py` in full, `service.py`, `runtime.py`, `device_client.py` dispatch/publish/flush/reply/expiry | Read in full where listed |
| **E** emsctl and diagnose | `emsctl.py` whole (parser, `main`, runtime edits, interactive mode, dashboard, completion, grid-meter, backup, config, stack); `ems/diagnostics.py` contract core, redaction, support bundle, control snapshot and samples, hardware probes; `docs/cli.md` | Read in full, sandbox runs |
| **B** Dashboard | `dashboard/server.py`, `auth.py`, `https.py`, `maintenance.py`, `runtime_write.py`, `sqlite_store.py` (store, record, history, energy core), `telemetry.py`, `static_files.py`, `theme.js`, `index.html` controls; `app.js` runtime forms, auth, SSE/polling, maintenance, diagnose and logs renderers, history fetch, HTML helpers | Read in full, probes |
| **X** Admin backend | `admin/server.py` auth, backup, maintenance apply, container sync, admin update, credential delete handlers; `backup_restore.py`, `guided_upgrade.py`, `container_actions.py`, `update_apply.py`, `install_state.py`, `auth.py`, `https.py`; release extraction; setup workflow artifact cleanup; deployment markers; credential-store path handling | Targeted, reproductions in scratch |
| **F** Admin frontend | `admin.js` XSS scan over all `innerHTML` writes; auth gate; scanning; source priority; credential pool and Zendure cloud; switch confirmations and setup plan; feature fields and storage; deployment and start conflicts; MQTT migration; guided upgrade execute/reconnect/resume; backup/restore; diagnose; maintenance preview/apply/runtime reset; start paths, workflow lifecycle and recovery; system alignment recovery; `index.html` labels and dialogs | Targeted by feature area |
| **A** Appliance | `agent.py`, `protocol.py`, `validation.py`, `commands.py`, `operations.py`, `operation_schema.py`, `paths.py`, `auth.py`, `admin_deployment.py` (writes), `admin_lifecycle.py` (install plan/execute, image checks), `network.py`, `timezone_config.py`, `status.read_log`, `systemd.py`, `support_archive.py`, `agent_client.py`, `cli.py` (auto-update, restart policy), `host_config.py` (writes); all `manager_*.py`, `persistent_state.py`, install/verify scripts and units, maintainer scripts, `build-deb.sh`; release trust, attestation, artifact trust, fetch, index, inputs, releases, packages, known-good, version, registry tags, build authority, source bundle; `web.py`, `web_audit.py`, `audit.py`, `index.html`, most of `app.js` | Read in full where listed |
| **D** User documentation | README, `docs/README.md`, quickstart, first-run checklist, docker, install-docker, common commands, native Python, winter mode, full-charge assist, `docs/user/` safety, troubleshooting, config layout, docker bootstrap, hardware requirements, supported setups, connection types, index; `config/config.template.json`, compose files, entrypoint, installers, systemd template; every relative link, every emsctl command and main-script flag and every documented config key checked by script | Read in full where listed, script-checked elsewhere |

### Reviewed in rounds 2 and 3

| Area | Files |
|---|---|
| **Q/G** MQTT control | `ems/zendure_mqtt/` `config_mapping.py`, `migration.py`, `capability.py`, `topics.py`, `write_protocols.py`, `config_entries.py`, `device_client.py` confirmation and external control; `ems/mqtt_control/`; `property_writes.py`; `device_identity.py` |
| **BK** Backup | `ems/backup.py`, `ems/backup_crypto.py`, `admin/backup_restore.py` restore and rollback |
| **H** History and InfluxDB | `ems/history/`, `ems/influx_setup.py`, `dashboard` analytics provider and series route, `external_status.py`, `config_init.py`, controller `publish_to_influx`/`publish_to_ha` |
| **AM/AL** Admin backend | `system_alignment.py`, `workflow_lifecycle.py`, `setup_workflow.py`, `guided_setup_workflow.py`, `discovery.py`, `discovery_connections.py`, `mqtt_discovery.py`, `credential_store.py`, `zendure_cloud_*`, `image_retention.py`, `known_good.py`, `maintenance_config.py` merge logic, the setup and discovery `server.py` handlers |
| **AP** Appliance | `backup_*.py`, `migration.py`, `ssh_*.py`, `sshkeys.py`, `shell_access.py`, `rescue_account.py`, `admin_lifecycle.py` repair, `setup-export-root.sh`, `backup-account.sh`, `shell-account.sh`, `rescue-account.sh`, maintainer scripts |
| **FR** Frontend | Dashboard `app.js` flow and pipes, analytics charts and KPIs, device cards, `styles.css`; Admin `admin.js` mDNS, grid-meter selection, maintenance draft, logout, the remaining error paths |

### Not yet reviewed

- EMS: `log_buffer.py`, `energy_channels.py`, `cli_privilege.py`, `diagnostics.py` text renderers and plausibility checks, `controller.health_snapshot`.
- Admin backend: `guided_upgrade.py` beyond config handling, `setup_planner.py`, `setup_intent.py`, `connection_planner.py`, `mqtt_runtime_provisioning.py`, `config_preview.py`, `releases.py` and the catalogues, `mdns.py`, `gateway_probe.py`, `networks.py`, the remaining `server.py` handlers.
- Admin frontend: network/gateway rendering, manual MQTT broker, config draft and identity logic, the setup wizard stepper, system build selection, themes, `admin.css`.
- Appliance: `image_*.py`, `partition_geometry.py`, `block_probe.py`, `media_sizing.py`, `hostprobe.py`, `config_seed.py`, `rpi_image_gen.py`, `install_check.py`, `health.py`, `runtime_gates.py`, `services.py`, `admin_lifecycle.py` rollback, `grow-root.sh`, `styles.css`.
- Docs: `docs/user/admin*`, `docs/user/admin/*`, `docs/user/dashboard/*` beyond runtime settings, `docs/user/appliance/*`, `docs/home-assistant.md`, `docs/configuration-examples.md` in full, `docs/technical/troubleshooting-reference.md`.
- Not in scope of review 1 at all: `scripts/` beyond the installers and the Manager fetch script, `tools/`, CI workflows, `deploy/` beyond the Admin installer.

## Findings fixed in review 1

Each line names the commit that fixed it.

| ID | Sev. | Finding | Commit |
|---|---|---|---|
| C1 | high | A NaN or Infinity grid reading (JSON `NaN`, an MQTT `nan`) drove the target to `max_total_power` and the filter never recovered | fix(ems): a grid reading that is not a finite number is a failed read |
| C2/K3 | high | `"allow_hardware_writes": "false"` and every other quoted control flag was truthy at runtime while Admin showed it blocked | fix(config): a quoted control flag never arms a write gate |
| C10 | medium | A config without `dry_run` ran dry while the Admin projection said it controlled | same commit |
| B2/K11/E3 | high | Dashboard, HA sync and emsctl's interactive menu saved a stale in-memory copy over emsctl/Admin writes (EMS re-enabled itself, an AC-input device went back to output) | fix(runtime-state): a save keeps what another process wrote since the last load |
| K2 | high | `--simulate`/`--replay` pruned the real devices from the live runtime state and wrote the live state DB | fix(simulation): --simulate and --replay keep their state in a scratch directory |
| K4 | high | One unmanaged SoC bound (`0`) was written as `minSoc=0` or `socSet=0` | fix(ems): SoC reconciliation writes only what it manages and keeps what is running |
| K5 | high | The periodic SoC reconcile fought an active full-charge assist and completed it early | same commit |
| K6 | medium | After a restart in winter the ramped `minSoc` was written back to the summer value | same commit |
| K1/C6 | high | A grid meter that stopped answering kept serving its last value and the integrator wound output up to the maximum | fix(ems): a grid meter serving its last value holds the target |
| C3 | high | An MQTT target queued behind an in-flight command was published after the controller had stopped writing to the device | fix(mqtt): a target queued behind an in-flight command dies with the cycle that asked for it |
| E1/K12 | high | emsctl accepted any upper value (`loop-interval 100000`, `ac-charge-power 99999`) | fix(emsctl): runtime edits share the dashboard's bounds |
| B5 | medium | A 5000 W hard cap made the dashboard EMS card unusable on larger systems | same commit |
| E5 | medium | `ac-mode input` was accepted for MQTT devices whose acMode the EMS cannot switch | same commit, then the dashboard parity commit |
| E2 | high | `dashboard disable-auth` deleted the shared Admin/Dashboard password without asking | fix(emsctl): destructive and mistyped commands stop and say why |
| E8 | medium | A mistyped `--config` was read as an empty config and the command reported success | same commit |
| E10/E11 | low | Tracebacks for null sections, unwritable paths, backup errors, Ctrl-C; emsctl HA defaults differed from the EMS | same commit |
| E6/E9 | medium | The support bundle leaked device serials and landed in the unmounted `/app` in Docker | fix(diagnose): the support bundle lands on the host and masks device identities |
| C5/K13 | medium | The shared HTTP session retried POSTs three times and stalled the loop for ~10 s per unreachable device | fix(clients): retry a failed read once and never a write |
| C7, C11, C12 | medium/low | Tasmota URL credentials in logs; an HTTP error reply read as zero telemetry; a refused CONNACK read as connected | fix(clients): a failed read says so, and without the meter password |
| C8 | medium | A grid meter on a named broker ignored the broker's `tls_mode` and connected in plaintext | fix(config): a grid meter on a named broker uses the broker's TLS mode |
| K7 | medium | Re-enabling control wrote the integrator's wound-up value at once | fix(ems): switching control back on starts from the observed output |
| K9 | medium | Two writers flipped `gridOffMode` every reconcile interval | fix(ems): gridOffMode has one writer |
| Parity | — | The dashboard could not set or show the AC role and AC charge power that emsctl sets | feat(dashboard): device cards set the AC role and charge power emsctl sets |
| B3, B6, B7, B11, B13 | medium | Cards posted every shown field (stale values overwrote external changes); `step=50` blocked saving; no stale or meter-offline indication; no confirmations; bare error codes | fix(dashboard): runtime cards save what changed, ask first and say what happened |
| B1 | high | History reads decoded the whole window (seconds, hundreds of MB, unauthenticated) and the legacy endpoint held the store lock | fix(dashboard): history reads stream and thin a long window |
| B4/X7 | medium | One idle TCP connection stalled every HTTPS client of the dashboard and the Admin Console | fix(https): one idle connection no longer stalls every HTTPS client |
| B8/B9 | medium | Sessions survived a password change; concurrent guesses bypassed the login limit | fix(auth): a password change ends open sessions, and guesses count up front |
| X1 | high | Guided Upgrade replaced an unparseable config with template defaults and reported success | fix(admin): Guided Upgrade refuses a config.json it cannot read |
| X3 | medium | Container sync on an unreadable config stopped InfluxDB and recreated EMS | fix(admin): container sync leaves everything alone while config.json is unreadable |
| X4 | medium | A restore plan could run twice and concurrently | fix(admin): a restore plan runs once, and one restore at a time |
| X2 | high | Any website could set the first Admin password (no Content-Type/Origin check) | fix(admin): a page on another site cannot set the first password |
| X6 | medium | The backup diff endpoint returned raw credential files | fix(admin): the backup diff never hands a credential file to the browser |
| X8 | medium | The Admin self-update wrote compose/env non-atomically and died silently on a decode error | fix(admin): the self-update writes its files atomically and fails loudly |
| X10 | medium | An unreadable config fell back to the default auth path and offered first-password setup | fix(admin): an unreadable config.json is a recovery state, not first-password setup |
| F1 | medium | Guided Setup secrets were persisted in localStorage | fix(admin): secrets typed into Guided Setup stay out of localStorage |
| F3, F4, F7, F8, F12, F16, F17 | medium/low | Bare error codes; a delete dialog whose Cancel still deleted; credential removal and container replacement without confirmation; double escaping; vague restore hint; discarded preview error | fix(admin): errors say what happened, and deleting asks in a way Cancel can undo |
| F2, F11, F14 | medium/low | A waiting resume left the overlay up forever; failed clicks did nothing visible; overlay focus | fix(admin): a waiting resume keeps polling, and failed clicks say so |
| F5 | medium | Docs claimed Admin creates encrypted backups | docs(admin): the Admin Console reads encrypted backups but does not create them |
| A-M5 | medium | The UI called the shared password independent from the Admin password | fix(appliance): the UI no longer calls the shared password independent |
| A-M2 | medium | The agent planned against the zone read at start and could not switch back; the plan promised a container restart that never happens | fix(appliance): the timezone in force is the one reported, and nothing promises more |
| A-H3 | high | A revert by the verify deadline never reached the retention record | fix(appliance): a revert the verify deadline drove reaches the retention record |
| A-M4 | medium | Cancel in a plan dialog left the plan holding the operation lock | fix(appliance): Cancel in a plan dialog withdraws the plan |
| A-M6 | medium | The support archive could be created but not downloaded | feat(appliance): a finished support archive offers its download |
| A-M10 | medium | The image build accepted a Manager manifest signed by a revoked, expired or untrusted key | fix(appliance): the image build trusts a Manager package exactly as the appliance does |
| A-M1 | medium | The root agent chmod/chowned by path in a container-writable directory | fix(appliance): the root agent sets the password file's mode on its own descriptor |
| D1, D4, D5, D13, D14 | high/medium | Docs described a separate "enable live writes" step; placeholder list, default-on writers and meter-sign remedy were missing; template comments called enabled features optional | docs: say when EMS starts writing, and what writes on its own |
| D3, D6, D8, D9, D10, D11, D12, D16, D17, D18, D20 | high/medium/low | sudo advice that breaks the installers; v0.7.0 compose pin; `--force`/`--bind`; D0 advice; contradictory hardware claims; systemd template; supported-setups contradictions; FAQ meter list; 32-bit Pi OS; tab name | docs: install instructions that work as written |

### Rounds 2 and 3

| ID | Sev. | Finding | Commit |
|---|---|---|---|
| G1 | high | `gridOffMode` writes to HTTP devices never reached the device: the log context's `field` collided with `zendure_write`'s own | fix(clients): a property write to an HTTP device is sent when its log context names the field |
| H-1 | high | A device name in the unauthenticated analytics series route was spliced into Flux | fix(analytics): a device name from the browser cannot run Flux |
| H-2, H-3, H-4 | medium | Influx secret env rewritten in place; writer thread died on an unreadable secret; status said healthy with a half-synced schema | fix(influx): the secret file survives a power cut, the writer survives a bad one, status tells the truth |
| BK1–BK4 | high/medium/low | Database restore under a live WAL; `abort` after files were written; password error outside `BackupError`; non-string manifest path | fix(backup): a database restore reaches a live WAL database, and abort writes nothing |
| G2, G3 | high/medium | A legacy broker without `source` refused startup and the migration disabled control; a mixed-case source took the wrong write gate | fix(mqtt): a legacy single broker starts, and a cloud source in any case uses the cloud gate |
| Q1/G4 | medium | `fetch()` and `describe()` published a queued target before the controller decided | fix(mqtt): neither the next cycle's fetch nor a status read publishes a queued target |
| Q3/G5 | medium | NaN/Infinity in a device report made every fetch raise | fix(mqtt): a NaN or Infinity in a device report is a missing value |
| AM2 | medium | Image retention deleted the build that ran before the upgrade | fix(admin): image retention keeps the build that ran before the upgrade |
| AM3, AM4 | medium | IPv4-mapped IPv6 ranges and mDNS answers bypassed the LAN-only rule | fix(admin): discovery contacts only LAN addresses, however a range or answer is spelled |
| AM5 | medium | A Zendure MQTT broker change in Maintenance was reverted by the feature draft | fix(admin): a Zendure MQTT broker change in Maintenance is not reverted on apply |
| AM11 | medium | Two first credential saves created two keys; one record became undecryptable | fix(admin): two first credential saves at once agree on one encryption key |
| AM12 | medium | A deleted Zendure token came back from the legacy Admin copy | fix(admin): a deleted Zendure token does not come back from the earlier Admin copy |
| AM1 | medium | Return to the running build cancelled before checking the acknowledgement | fix(admin): returning to the running build keeps the recovery gate when acknowledgement is missing |
| AM7 | medium | Legacy broker routes overwrote or deleted runtime credentials in use | fix(admin): the legacy broker routes leave runtime credentials they do not own alone |
| AL1–AL4, AL6 | low | Validation result ignored; connection edits lost under concurrency; Local API save reset priority; TypeError on malformed body; cancel without operation id | one commit each, `fix(admin): …` |
| Harness | — | A test run wrote `config/secrets/mqtt-home.json` into the checkout | fix(tests): no test can write credentials into the checkout's config/secrets |
| AP1 | medium/high | Ownership migration at agent start followed swapped-in links | fix(appliance): ownership migration at agent start never follows a swapped-in link |
| AP2 | medium | A backup key added after a fail-closed disable was unconfined | fix(appliance): a backup key added after a fail-closed disable is refused, not unconfined |
| AP3 | medium | "Shell access disabled" was never checked against sshd | fix(appliance): shell access reads as disabled only when the running sshd refuses the account |
| AP5 | medium/low | Repair created bind directories root 0700 and called them verified | fix(appliance): a repaired bind directory belongs to the deployment and is verified as such |
| AP6 | low | Package texts named commands that do not exist | fix(appliance): the commands the package tells an operator to run exist |
| AP7, AP9 | low | sudoers for a foreign account; key store followed a symlinked `.ssh` (and leaked lines of the link target) | two `fix(appliance): …` commits |
| FR2 | medium | Analytics energy integrated across gaps | fix(dashboard): analytics energy is not integrated across a gap in the record |
| FR3 | medium | An offline meter still drew grid flow and direction | fix(dashboard): an offline grid meter draws no grid flow and no direction |
| FR5–FR8, FR10, FR13, FR14 | low | Bare codes; mDNS cause hidden; cancelled meter replacement lost the selection; destructive draft actions without confirm; failed logout shown as success; range tab and SoC bar semantics; broken CSS block | one commit each |
| Q4 | low/medium | An own target applied after its confirmation timeout was reported as external control | fix(mqtt): an own target the device applies after its confirmation timeout is not reported as external control |
| Q5/G6 | low | Migration dropped a pinned read-only model with a misleading reason | fix(mqtt): migration keeps a pinned read-only Zendure model and names it as the reason |
| Q6 | low | A device id or product key with `/`, `+` or `#` reached the topic strings | fix(mqtt): a Zendure device id or product key with topic syntax is rejected |
| Q7/G13 | low | `max_power` 0 or a non-number disabled the device ceiling (and crashed the API path) | two commits, MQTT and local API |
| G7 | low | "SolarFlow 2400 AC+" resolved with exact confidence to the AC model | fix(mqtt): a SolarFlow AC+ product string resolves to the AC Plus model, not its sibling |
| G12 | low | Per-device tuning keys unbounded | fix(mqtt): per-device command tuning keys are bounded and reject non-finite values |
| H-6 | low | The diagnose exposure check read the config differently from the dashboard; the support bundle JSON could break after redaction | two `fix(diagnostics): …` commits |
| H-7, H-8 | low | Redaction gaps in external status; `config init` preview printed the app key; out-of-range or infinite SoC bounds were written | three commits |
| H-9, H-10 | low | A rejected Influx token was cached forever; a line break or NaN broke a write batch | two commits |
| B15 | low | A closed tab held an SSE slot for up to 30 minutes | fix(dashboard): an idle event stream notices a closed browser tab |
| E12–E14 | low | Hardware probe ignored `broker_ref`/Tasmota; migrate-zendure-mqtt asked twice and mixed JSON; completion lacked grid-meter; wrong config order in the docs | three `fix(diagnose|emsctl): …` commits |
| X11 | low | A restore with nothing rolled back claimed a rollback; one bad set entry emptied the list; unreadable download said "unknown backup id" | fix(admin): backup messages say what actually happened |
| K-low | low | A failed night park was never retried; an unknown control gate borrowed the API gate | two commits |
| D19, D21 | low | Installer dry run planned for the wrong directory; `--duration 120` not marked as live | fix(admin): the installer's dry run plans for the --install-dir it was given; docs commit |

### Round 4 (review of the round 2–3 fixes)

Each line names a defect a round 2–3 fix introduced and how it was resolved;
the fix is folded into the commit it corrects.

| ID | Sev. | Defect in the earlier fix | Resolution |
|---|---|---|---|
| R4-E1 | medium | A numeric-string `max_power` ("600") fell back to the system ceiling, raising the device's hardware limit | `device_power_ceiling` accepts finite positive numbers and numeric strings |
| R4-E2 | medium | An invalid `simulation_mode` started the canned simulation and exited, so control stopped (restart loop under Docker) | An invalid `simulation_mode` keeps the loop running dry; Admin shows "calculating only" |
| R4-CI | medium | The analytics device allowlist refused names outside the current config (CI "InfluxDB analytics e2e"; renamed devices lost their history) | Replaced by a printable/length check; the Flux escape was fuzzed against InfluxDB 2.7 and is complete |
| R4-A1 | medium | AM9's strict read refused abandon for a corrupt record and also for a version-1 record from an older Admin, leaving no way out | AM9 fix withdrawn; AM9 is back in the open register |
| R4-A2 | low | Maintenance lost the editor for `zendure_mqtt.tls_insecure` and the two timeouts | Only host, port and TLS are left to the broker card |
| R4-A3 | low | The installer dry run checked existing files in the current directory | Checks the `--install-dir` |
| R4-F1 | medium | A restore onto a corrupt live database failed instead of replacing it | Falls back to replacing the file once the backup itself is verified readable |
| R4-F2 | low | An interrupted SSH key write left a fixed-name temp file that blocked every later key change | Random temp name, removed on failure |
| R4-F3 | low | The atomic Influx secret write lost the file's owner and group | Owner and group carried over where permitted |
| R4-UI1 | low | "Lost contact" stayed on screen after a backup poll recovered | Cleared on the first good answer |
| R4-UI2 | low | A logout over a dead network showed the login form and "still logged in" at once, and dropped the setup context | Polling and context are dropped only after a confirmed logout |

### Round 5 (review of the round 4 corrections)

| ID | Sev. | Defect | Resolution |
|---|---|---|---|
| R5-F1 | medium | The corrupt-database fallback also replaced a healthy live database on "read-only" and similar operational errors, losing its unsaved log | Only a file SQLite reports as corrupt is replaced; operational errors leave the database and its log alone |
| R5-F2 | medium | The fallback removed the live `-wal` before the replacement was on disk | The verified replacement is written and synced first, then swapped in |
| R5-F3 | low | The device-name check also refused long names on the local SQLite history | The check applies to InfluxDB queries only |
| R5-F4 | low | A rewritten Influx secret lost its group when the owner could not be kept; a symlinked secret lent its target's owner | Group kept on its own; a symlink lends nothing |
| R5-M1 | medium | Maintenance accepted `tls` off with "Skip TLS verification" on, which the EMS rejects at start (broker silently gone) | fix(admin): Maintenance refuses Zendure MQTT TLS settings the EMS would reject |
| R5-m | low | Admin named `simulation_mode` instead of `dry_run` as the blocking flag for an invalid `simulation_mode` | The projection matches the runtime |

### Round 6 (review of the round 5 corrections)

| ID | Sev. | Defect | Resolution |
|---|---|---|---|
| R6-F1 | low | The new Maintenance TLS check also read top-level TLS keys beside named brokers, which the EMS ignores, and then refused every apply | Checked only when the EMS itself builds a top-level broker (`legacy_default_broker_present`, the predicate the runtime uses; round 7 replaced a host-only rule that disagreed with it); a test pins Maintenance to the runtime loader; the message names the config key |
| R6-F2 | low | A restore into a WAL database with a different SQLite page size now fails ("readonly database") where it used to replace the file | Accepted limit: no code sets a page size, so every database uses 4096; replacing the file would again risk the open, healthy database R5-F1 protects. Listed in the open register |

### Rounds 7 to 9

| ID | Sev. | Defect | Resolution |
|---|---|---|---|
| R7-F1 | low | The round 6 host-only rule disagreed with the runtime: a block without named brokers and without a host was rejected by the EMS but accepted by Maintenance | Maintenance uses `legacy_default_broker_present`, the runtime's own predicate; a test compares seven shapes with the runtime loader |
| R8-F1 | low | The refusal's fix hint only fitted "TLS off, verification skipped"; for a `tls_mode` contradiction or a quoted boolean it pointed at the wrong key | The hint follows the resolver's own error (`TLS_INSECURE_WITHOUT_TLS`); other errors, including non-boolean values such as `0`, name the general rule |
| R9-F1 | low | Round 8 re-derived the pair condition, so `tls: 0` got the specific hint and a whitespace `tls_mode` the general one | Same resolution as R8-F1: the hint is chosen from the resolver's error, not re-derived |

## Open register

Suggested order for the next session: the high items first (A-H1 with the
two-step plan in its row, A-H2, A-H4, D2), then the design decisions that
block work (AM9/AM10/AM13 together, AP4, AP8, K10 with hardware evidence),
then the medium small items, then the lows. Each fix follows
`docs/developer/agent-rules.md` and moves its row to the fixed tables above.

Ordered by severity. Each needs either a focused fix commit or a recorded
decision. Rows marked *Unconfirmed* are reviewer suspicions without a
reproduction; confirm or drop them before fixing.

| ID | Sev. | Size | Finding | Direction |
|---|---|---|---|---|
| A-H1 | high | large | The appliance's root-capable password lives in `config/`, which the EMS container mounts read-write: deleting or rewriting `dashboard-auth.json` from the container reopens first-run setup or sets a known password, and the console then grants an SSH shell with sudo | Step 1 (appliance only): root actions (SSH, shell access, key add) require a root-owned record of the confirmed password, and a root-owned "password was set" marker stops a deleted file from reopening setup. Step 2: move the file to `config/auth/`, mounted read-only into the EMS container; needs a reader rule (new path wins, legacy only when absent), host-side move, compose migration for Guided Upgrade and the Manager, backup path mapping, a legacy symlink for downgrades, and docs for `set-password` outside the container |
| A-H2 | high | large | `TZ` reaches no container; every Docker install runs its hour-based windows (winter `adjust_hour`, full-charge `force_time`) on UTC | Add `TZ` to the standard and Admin-generated compose, make sure the image carries tzdata, restart on change; until then the docs say UTC |
| A-H4 | high | large | After a downgrade or revert, the older Manager is blocked by `state_schema_behind` for both update and revert | Claim only the axes the incoming package implements; amend `docs/appliance/adr/manager-self-update.md` |
| X5 | medium | large | Every saved discovery MQTT credential is sent to every discovered broker, including mDNS and port-scan candidates, in plaintext on 1883 | Bind credentials to the broker they were saved for, or require per-broker opt-in |
| K8 | medium | large | Any exception in `run_once` ends the process with inverters at their last limit; the full-charge store writes SQLite every cycle | A guarded cycle with a failure counter and a safe-park policy; write the store only on change |
| K10 | medium | small | An unknown `acMode` (missing field read as 0) gets `acMode=2` written every cycle; `run_startup_ac_mode_reconcile_once` and `reconcile_ac_mode_on_start` are dead code | Needs hardware evidence first: does any firmware report `acMode=0` for a real state? Then distinguish the default intent from an explicit role and delete or wire the startup path |
| C4 | medium | small | `ZendureMqttDeviceClient` is mutated from the paho thread, the fetch executor and the main thread without a lock | A per-device RLock with a deterministic Event/Barrier test, or drain replies on the control thread |
| C9 | medium | small | Admin config mutation passes unparsable, non-finite and out-of-range values and maps unknown boolean strings to `False`; callers ignore the issues | Return issues from coercion and make preview/apply refuse them |
| E4 | medium | large | No cross-process lock on runtime state; two writers within the same instant can still lose a change | flock in `RuntimeState` and emsctl |
| E7 | medium | large | `diagnose --control`/`--control-quality` evaluate fields the EMS never writes to runtime state; the dashboard diagnose shares this | Read a live snapshot from the running EMS, or narrow the modes and their docs |
| D2 | high | large | `diagnose` says nothing about placeholders or safe mode | A check through `template_placeholder_paths()`; a contract-tested addition to the versioned diagnose output |
| D7 | medium | large | The EMS-only install has InfluxDB enabled and warns periodically with a non-Docker hint | Default `influxdb.enabled=false` for EMS-only, Docker-aware hint |
| D5b | medium | small | The full-charge `ac_charge_power` default is 200 W in code and 600 W in the template | Owner decision on one value |
| A-M3 | medium | small | After a hostname change the web service still checks Host/Origin against the old name (ProtectHostname) and refuses every POST | Ask the agent for the hostname or restart the web unit; a 403 text that says to use the IP |
| A-M7 | medium | small | Login throttling is per exact address, IPv6 rotation bypasses it, PBKDF2 checks run unbounded in parallel | Per-/64 counting, a global cap, one or two checks at a time |
| A-M8 | medium | large | Anyone on the LAN can claim the first appliance password with an HTTP client | A one-time setup code shown on the device |
| A-M9 | medium | small–large | The 2 s poll rebuilds forms and wipes typed input | `rememberInput` for every form |
| A-M11 | medium | small | Automatic security updates never refresh the apt index | Refresh before planning, or block on a stale index |
| A-M12 | medium | small | `rollback-manager` leaves an armed deadline that can no longer revert | Re-arm with the rollback target, or say so |
| X9 | low | small | Guided Upgrade and the legacy-config migration write config.json outside `ConfigApplyService` | Route both through `apply_prepared` with `expected_revision` |
| F6 | medium | small | The transport-switch confirmation does not name the device | Resolve the name from the draft |
| F9 | low | small | Restore preview of an encrypted backup offers no password field; the field is not cleared between backups | Open the password field for locked backups, clear on select |
| F10 | low | small | MQTT migration guesses the failed stage from message text | A structured stage field |
| F13 | low | small | Resume/return/abandon buttons have no busy state | Disable while the request runs |
| F15 | low | small | Certificate paths, `token_env` and `log_redaction` are catalogued as secrets and render as password fields | Mark only real secrets |
| B10 | low | — | `/api/runtime` exposes device serials without a login | Policy decision |
| B12 | low | small | The history "7d" button with a 48 h retention | Hide or annotate ranges beyond `history_hours` |
| B14 | low | small | Login modal without dialog semantics (range tabs fixed with FR13) | Dialog role and focus handling |
| C13 | low | — | A config from a newer EMS version still starts | Policy decision: fail closed or force dry run |
| K-low | low | small | Ramp factor 0 means unlimited; HA home sensor wrong while exporting; offline device loses its runtime role for a cycle; bare `except` in `fetch_all_devices` | One by one |
| A-low | low | small | `apt-get --no-remove`; plain-http release index; prerelease disagreement; no wall-clock limit on downloads; `gate_passed` accepts any PASS; deadline left armed when the install unit fails to start; `verify-revert-attempts` not removed; `verify-manager.sh` checks version only; log fallback follows symlinks; `/etc/hosts` not updated on hostname change; web UI in-flight states | One by one |
| AM9 | medium | large | A corrupt Guided Setup record reads as "no record", so abandon takes the legacy no-ID path (cleanup still needs per-artifact ownership proof) | Decide together with AM10/AM13; a strict read must keep version-1 records readable as "none" and offer the operator a way out |
| AM10 | medium | large | A lifecycle switch to Guided Setup while a setup transition runs without a workflow record answers ok and then deadlocks on `setup_transition_owner_unproven` | Decide: refuse the switch, or adopt the transition into the new workflow; about 100 route tests assume today's start |
| AM13 | medium | large | `GuidedSetupWorkflowStore.ensure_active()` overwrites an unreadable workflow record, which is the only ownership proof for its artifacts | Same decision as AM9: strict read and a recovery state |
| AP4 | medium | large | The backup account reads `dashboard-auth.json` through the ACL mask; the config archives it is meant to read carry the same secrets | Policy: should the backup account receive secrets at all; then the export exclusion and archive format follow |
| AP8 | low | large | `rescue-account.sh` restores a published password over a deliberate `*`/`!` | Policy: deliberate lock versus the repair of older devices without a marker |
| AP10 | low | small | `BackupAccessActivation.authorized_keys()` still reads by path through a symlink | Read through the descriptor-based key store |
| AP11 | low | small | `shell-account.sh migrate_unusable_home` moves a foreign existing `ems-shell` account | Act only on the account the script created |
| BK5–BK10 | low | small | Restore forces 0600 and the restorer's owner; manifest hash and archive from two reads; InfluxDB restore in memory; v1 header without bounds; files outside the project restore to the wrong place; created files survive "rolled back" | One by one |
| FR15 | low | small | Admin mDNS renders every client-side error as `unavailable_runtime` and disables its buttons until the next poll | Keep transient errors separate |
| FR16 | low | small | Maintenance discovery counts mDNS/network failures without showing their cause | Same helper as FR6 |
| FR17 | low | small | Admin `submitLogout` stops alignment polling and drops the setup context before the request; a failed logout loses it | Clear only after a confirmed logout |
| Q8 | low | — | The MQTT message id counter restarts at 1 on every start | Left as is: the ids reach real hardware and seeding them is not hardware-validated; a reply must also match the device and arrive during a command |
| G12b | low | small | The bounds chosen for the per-device tuning keys (ack 120 s, confirmation 300 s, tolerance 200 W, preempt 1000 W) are the reviewer's choice | Owner confirms or adjusts |
| K14 | low | small | `min_soc`/`max_soc` are validated only in `apply_soc_limits`; allocation and night idle read raw values, a string raises | Validate once at config load |
| R4-u1 | low | small | A 400-digit integer in an MQTT report survives parsing and `math.isfinite` raises `OverflowError` in external-control detection (unconfirmed end to end) | Treat ints beyond float range as missing in the payload parser |
| R4-u2 | low | small | `_unconfirmed_own_targets` grows until the next confirmation; a foreign value equal to an old own target reads as ours | Keep only the last few targets |
| R4-u3 | low | small | `return_to_running_build` always passes `acknowledged=False`, so a Development known-good build can never be returned to | Pass the operator's acknowledgement through the UI |
| R4-u4 | low | — | A reverse proxy that rewrites Host without `X-Forwarded-Host` is refused for first-password setup (deliberate); the doc says proxies keep working | Narrow the doc sentence |
| AM6 | medium | small | `/api/setup/config/download` returns the config with its live secrets | Policy: redact, or require a fresh password confirmation |
| H-5 | medium | large | A slow or unreachable Home Assistant publish stalls the loop before the device writes (rest of K13) | Publish off the control thread, or with a short timeout and backoff |
| E15 | low | small | Restore output names only the archive basename and gives no restart hint | Print the full path and "restart EMS" |
| FR9, FR11, FR12 | low | small | Raw enum values in the dashboard; analytics KPI labels; chart colours ignore the theme | UI polish |
| G8–G10 | low | small | A nested `mqtt.power_write_profile` mismatch gets no migration; the reply topic prefix comes from the telemetry family; acknowledgement profiles hold a queued target for 40 s | One by one, with hardware evidence for G9/G10 |
| AL5, AL7–AL10, AL12–AL14 | low | small | Admin: discovery scan not single-flight; `release_stale_state` race; supersede and cancel with an active sidecar; install-state probe; identity cache; empty development catalogue; cloud broker TLS check without verification | One by one |
| R6-F2 | low | — | A restore into a WAL database with another page size fails instead of replacing the file | Accepted while every database uses the default page size; revisit if one ever sets `page_size` |
| R8-u1 | low | small | A Maintenance round trip turns a stored `"tls": "false"` (quoted) into `tls: true` and accepts it, so TLS is switched on silently | Read the stored value strictly in the broker card draft and surface the refusal instead |
| R6-u1 | low | small | Maintenance shows a stored legacy broker with `tls: false, tls_insecure: true` as TLS on, so even a no-op apply writes `tls: true` (silently repairs the pair by enabling TLS) | Show the stored value as it is and let the TLS check refuse it |
| R8-u2 | low | small | Maintenance does not refuse a top-level broker port the EMS rejects (`"abc"`, `0`); the EMS then runs without that broker | Same parity approach as the TLS check: validate with the runtime's own parser |
| U-1 | low | — | *Unconfirmed.* Shell-access status reads "disabled, not confirmed by sshd" when `sshd -T -C user=ems-shell` fails because the account does not exist yet | Verify on a Pi; show "account not created" instead |
| U-2 | low | — | *Unconfirmed.* The external-status redaction's cookie pattern blanks the rest of any free-text line containing `cookie:` | Anchor the pattern to header-shaped lines |
| U-3 | low | — | *Unconfirmed.* The credential key uses `os.link`, which fails on a filesystem without hard links; the atomic compose rewrite turns a symlinked compose file into a regular file | Fall back to `O_EXCL` create; resolve the symlink before replacing |
| U-4 | low | — | *Unconfirmed.* Analytics skips integration across steps over 3× the median spacing; loops stretched by fetch timeouts during an outage would undercount energy | Measure loop spacing during a device outage before changing the threshold |
| U-5 | low | — | *Unconfirmed.* A job-status 5xx with a real JSON message is treated as transient and after about 60 s replaced by "The Admin Console did not answer" | Show the server's message when the body carries one |
| D15 | low | small | `WR1` vs generated `INV_1` names in examples | Doc fix |

## Dashboard and emsctl parity

The dashboard is the daily operations UI; emsctl is the shell tool. After
review 1 both write the runtime state through the same validation in
`dashboard/runtime_write.py`.

| Function | emsctl | Dashboard |
|---|---|---|
| EMS on/off, total max power, min output limit, loop interval | `system …` | EMS / System card |
| Device on/off, max power, PV priority, offgrid socket | `device NAME …` | Device cards |
| AC role (output / AC charging) and AC charge power | `device NAME ac-mode`, `ac-charge-power` | Device cards (not for MQTT devices) |
| Winter mode on/off | `winter enable/disable` | Winter Mode card |
| HA publishing and helper control | `ha`, `ha-control` | Home Assistant card |
| Status and diagnosis | `status`, `diagnose …`, support bundle | Live views, Diagnose tab, support bundle |
| Backups and restore | `backup …` (incl. encrypted, conflict modes, diff) | Maintenance tab (unencrypted, replace) |
| Config upgrade | `config upgrade` | Maintenance tab |
| Setup and structure (init, migrate, influx, stack, passwords) | `config init`, `influx`, `stack`, `dashboard …` | not offered: Admin Console and CLI |

Min/max SoC have no runtime override in either tool; they are config values
owned by the Admin Console.
