# Testing

This project writes to real power hardware, so tests are the main guard against
regressions in control logic, write gates and safety reconciliation. All the
checks below run offline — no Home Assistant, Shelly, Zendure devices, InfluxDB,
secrets, or network access are required.

See also [developer-setup.md](developer-setup.md) for the source checkout and
environment, and [ci-release.md](ci-release.md) for how these run in CI.

## Development approach

This project develops complex and risky features **contract-first** with
test-driven development: for control logic, write gates, safety reconciliation,
config validation and the Admin apply/credential paths, a failing contract test
is written and shown to fail before the production change is made. Bugs get a
reproducing test before the fix. Small UI tweaks and exploratory prototypes may
stay pragmatic and gain tests as they settle. This is a working policy for the
areas that can write to real hardware, not a claim that every historical line of
the project was produced under strict test-driven development. The canonical
project requirement and scope are defined in [agent-rules.md](agent-rules.md).

## Quick local loop

Compile check (run after any change to the entry script, `ems/`, `emsctl.py` or
`scripts/check_log_events.py`):

```bash
python3 -m py_compile ems-solarflow-api-control.py ems/*.py emsctl.py scripts/check_log_events.py
```

Self-test:

```bash
python3 -B ems-solarflow-api-control.py --self-test
```

Simulation (no hardware or network):

```bash
python3 -B ems-solarflow-api-control.py --simulate --max-cycles 1
```

Replay a captured trace:

```bash
python3 -B ems-solarflow-api-control.py --replay /path/to/trace.jsonl --once
```

## Test selection

The suite is large — over 8000 Python tests and 47 Playwright specs — and the
full non-Docker run takes about 16 minutes on a developer machine against about
two for the fast tier. Run the tier that matches your change; the full suite is
a release gate, not a per-edit loop.

### Two marker dimensions

Every test module carries exactly one **execution level** and any number of
**functional areas**. The dimensions are independent and overlap on purpose.

| Level | Meaning |
|---|---|
| `unit` | isolated deterministic unit tests |
| `contract` | API, serialization, schema and frontend source contracts |
| `integration` | multiple production components without a real browser journey |
| `e2e` | complete user or service journeys |

| Execution property | Meaning |
|---|---|
| `docker` | requires a Docker daemon or Docker-first environment |
| `browser` | requires Playwright or a browser runtime |
| `slow` | exceeds the normal pull-request runtime budget |

| Functional area | Meaning |
|---|---|
| `admin` | Admin Server and Admin UI behavior |
| `setup` | Guided Setup lifecycle and installation flow |
| `maintenance` | Maintenance workflows |
| `workflow` | workflow ownership and lifecycle |
| `authority` | Device Plan, Preview, Apply and mutation authority |
| `config` | configuration generation, validation and mutation |
| `mqtt` | local MQTT, Zendure MQTT and broker workflows |
| `power_control` | power allocation and output safety |
| `backup_restore` | backup, restore and recovery workflows |
| `system_build` | System Build and deployment transitions |
| `documentation` | documentation, licensing and third-party inventory contracts |

`simulation`, `regression` and `mqtt_release` stay registered for the existing
gates. Every marker lives in `pytest.ini` and `--strict-markers` is enabled, so
a typo fails the run instead of silently selecting nothing.

### Fast developer tier

```bash
./scripts/test-fast.sh
# pytest -q -m "(unit or contract) and not docker and not browser and not slow" --maxfail=1
```

### Targeted functional tiers

```bash
./scripts/test-admin.sh authority
./scripts/test-admin.sh setup
./scripts/test-admin.sh maintenance
./scripts/test-admin.sh workflow
./scripts/test-mqtt.sh
./scripts/test-mqtt.sh integration
```

The same selections without the wrappers:

```bash
pytest -q -m "admin and authority and not slow"
pytest -q -m "admin and setup and not slow"
pytest -q -m "mqtt and not docker and not slow"
pytest -q -m "power_control and not slow"
pytest -q -m "system_build and not docker and not slow"
pytest -q -m "backup_restore"
pytest -q -m "docker"
pytest -q -m "documentation"
```

### Pull-request groups

Each group runs independently and mirrors one CI job:

```bash
./scripts/test-pr.sh core
./scripts/test-pr.sh appliance
./scripts/test-pr.sh admin
./scripts/test-pr.sh mqtt
./scripts/test-pr.sh power-control
./scripts/test-pr.sh docker
./scripts/test-pr.sh chromium-critical
./scripts/test-pr.sh firefox-smoke
```

The six groups are an **exact partition**: every collected test runs in exactly
one of them, and none runs twice. Functional markers stay overlapping
descriptions of behavior — a module may be both `admin` and `mqtt`. Only
*execution ownership* is exclusive, resolved by a fixed priority:

```text
docker > power-control > mqtt > admin > appliance > core
```

| Group | Marker expression |
|---|---|
| `power-control` | `power_control and not docker` |
| `mqtt` | `mqtt and not power_control and not docker` |
| `admin` | `admin and not mqtt and not power_control and not docker` |
| `appliance` | `appliance and not admin and not mqtt and not power_control and not docker` |
| `core` | `not appliance and not admin and not mqtt and not power_control and not docker` |
| `docker` | `docker` |

`tests/test_test_classification.py` proves both directions: the union equals the
full collection, every pair of Python groups is disjoint, and each non-Docker
test has exactly one owner. To run a *functional area* rather than a CI group,
use the plain marker (`pytest -m "admin and authority"`) — that selection is
intentionally overlapping.

### Nightly

`.github/workflows/nightly-full-suite.yml` runs the full non-Docker suite on
both supported Python versions, the strict deprecation check, the complete
Chromium and Firefox Admin suites, the Docker-first tier, the System Build tier
and the Admin upgrade/recovery journey.

### Release candidate

```bash
./scripts/test-rc.sh          # every gate, in order
./scripts/test-rc.sh --list   # print the gate list
```

Gates: static checks, the full non-Docker Python suite, the
`simulation and power_control` gate, the authority regressions, the security
regressions, the System Build tier, the Docker-first tier, the full Chromium
and Firefox Admin suites, the Admin replacement/recovery suite, the generated
config template and a clean-working-tree check. The RC tier never deselects a
known failure.

### Playwright groups

The specs carry Playwright tags, so one configuration serves every group:

```bash
npx playwright test --project=chromium --grep @smoke
npx playwright test --project=chromium --grep @authority
npx playwright test --project=firefox --grep @smoke
npx playwright test --project=chromium --grep "@setup|@authority"
npx playwright test --project=chromium               # full Admin suite
```

Tags: `@smoke` (fast critical journeys), `@setup`, `@maintenance`,
`@authority`, `@workflow`, `@system-build`.

### Prerequisites

- Python tiers need only the project virtualenv.
- `docker` tiers need a reachable Docker daemon (`docker info`). Without one the
  Docker suites skip with a precise reason instead of failing.
- Playwright tiers need `npm ci` and
  `npx playwright install chromium firefox`. No tier installs dependencies.
- The RC `admin-replacement` gate replaces one published Admin container with
  another, both pinned by digest, so it needs `ADMIN_REPLACEMENT_RUNTIME`,
  `ADMIN_REPLACEMENT_EVENTS`, the source identity (`CANARY_SOURCE_TAG`,
  `CANARY_SOURCE_REVISION`, `CANARY_SOURCE_BUILD_ID`,
  `CANARY_SOURCE_ADMIN_DIGEST`) and the target identity (`CANARY_TAG`,
  `CANARY_REVISION`, `CANARY_BUILD_ID`, `CANARY_ADMIN_DIGEST`,
  `CANARY_EMS_DIGEST`). Both sides come from the Development catalogue;
  `python3 scripts/resolve_canary_builds.py --catalogue <file>` prints them and
  `./scripts/test-rc.sh` names the missing ones before running any gate.

## Classifying a new test

Declare the markers at module level — that declaration is the authority, not
the file name:

```python
pytestmark = [
    pytest.mark.admin,
    pytest.mark.authority,
    pytest.mark.integration,
]
```

Rules:

- Exactly one level marker per module (`unit`, `contract`, `integration`,
  `e2e`). `tests/test_test_classification.py` fails when a module declares none
  or several.
- Add every functional area that applies; overlap is intended.
- Use per-test markers only when one module genuinely mixes categories.
- Use `slow` when a test exceeds the pull-request budget, so the fast tier and
  the PR groups stay usable.
- The `mqtt_release` allowlist in `tests/conftest.py` is the one remaining
  name-based bridge and is guarded by a contract test. Classify anything new
  with markers instead.

## Required regression check

The offline power-control regression tests are the deterministic CI gate. They
must stay green:

```bash
pytest tests/ -m "simulation and power_control"
```

## Debugging selection

```bash
pytest --markers                                    # every registered marker
pytest --collect-only -q -m "admin and authority"
pytest --collect-only -q tests/test_write_gates.py
pytest -k "stale_device_plan"                       # by test-name substring
pytest tests/test_write_gates.py::test_gate_blocks_write
```

An empty selection exits with code 5. The tier scripts turn that into an
explicit error instead of a silent pass.

## Log validation

```bash
python3 scripts/check_log_events.py /tmp/ems-sim.log \
  --require startup \
  --require target_calculation
```

## Documentation and contract tests

Several tests protect docs and public contracts rather than runtime behavior.
The documentation-content ones carry the `documentation` marker, so
`pytest -q -m documentation` runs the whole set:

- `tests/test_docs_user_structure.py` — the user / technical / developer
  documentation split and README routing.
- `tests/test_docs_user_guides.py` — the step-by-step guides under
  `docs/user/admin/` and `docs/user/dashboard/`: required pages, the shared
  section shape, resolvable image and relative links, every committed screenshot
  embedded somewhere, pairwise-distinct screenshots, descriptive alt text, the
  capture manifests matching the committed files, and the no-secrets scan.
- `tests/test_docs_admin_media.py` — the Admin demo videos and their static
  screenshot fallbacks.
- `tests/test_third_party_licenses.py` — `THIRD_PARTY_LICENSES.md` against the
  requirements files, `package.json`, `package-lock.json`, the vendored static
  assets and the container base images, plus the negative cases that prove
  `tools/check_third_party_licenses.py` actually rejects drift.
- `tests/test_agent_rules_contract.py` — the canonical rule set and supported
  agent entry-point links.
- `tests/test_docker_docs_contract.py`, `tests/test_docker_first_setup.py` —
  the Docker Bootstrap installer/compose/docs promise.
- `tests/test_issue_templates.py` — issue-template documentation links.
- `tests/test_test_classification.py` — the marker registry, the documented
  tier selections and the pull-request group partition.
- `tests/test_ci_workflow_docker_split.py` — how the CI groups are split.

When you move or rename docs, update these tests (or the redirect stubs) so the
links stay honest.

## Regenerating the documentation screenshots

The user guides are screenshot-led. Regenerate every image with:

```bash
./scripts/capture-docs-screenshots.sh            # Admin + Dashboard
./scripts/capture-docs-screenshots.sh admin      # Admin Console only
./scripts/capture-docs-screenshots.sh dashboard  # EMS Dashboard only
```

Both capture scripts start their own loopback-only preview server from the
deterministic fixtures in `tests/fixtures/admin_docs/` and
`scripts/dashboard_preview_data.py`, and shut it down again when they finish.
No Docker, hardware, discovery, MQTT broker, Zendure credential, `config.json`
or runtime state is involved, unrelated containers are untouched, and nothing is
pushed. Requirements: headless `firefox` and ImageMagick `convert`.

Add a screen by extending `SCREENS` in `scripts/capture_admin_docs.py` or
`scripts/capture_dashboard_docs.py` (plus a driver in
`scripts/admin_docs_preview.js` for Admin), then embed it in a guide —
`tests/test_docs_user_guides.py` enforces that the manifest, the committed files
and the asset README stay in step.

Two traps worth knowing:

- Guided Setup steps 02–05 are authorized by a **server-confirmed setup
  transition**, so the preview must serve one. Without it every setup screen
  silently falls back to step 01 and the captures become byte-identical
  duplicates; the distinctness test is what catches that.
- The Admin driver only runs after the SPA's own authenticated workflow resume
  has finished, because that resume re-opens Guided Setup on step 01.

After a run, review `git status --short docs/assets/screenshots` before
committing.

## Appliance tiers that need a real machine

Three appliance claims cannot be settled by pytest, because what they assert is
a property of a booted operating system, of a package manager, or of an image
builder — not of a Python object. Each has a driver script; each reports PASS,
FAIL or NOT RUN and never reports a skipped run as a pass.

```bash
# A packaged appliance in a Debian Trixie guest that really booted.
scripts/appliance-smoke-vm-amd64.sh [--rpi-image-gen DIR] [--keep]

# The real image builder, in a guest that is thrown away afterwards.
scripts/appliance-builder-vm.sh --profile rpi5 [--profile rpi4] --output DIR

# The strict release gate, in that same guest — the only host it can pass on.
scripts/appliance-builder-vm.sh --release-gate --profile rpi5 --profile rpi4 --output DIR
```

The gate builds the images itself, so it needs the generator's prerequisites and
cannot reach `RESULT: PASS` on a workstation that deliberately lacks them.
`--release-gate` runs it where those prerequisites are, and brings the verdict
and `dist/gates/` back out. The five mounted image checks stay NOT RUN inside
the gate, because it runs unprivileged; answer them separately with
`appliance-inspect-rpi-ab-image.sh --mount` as root in the same guest.

Both need `qemu-system-x86_64`, `qemu-img`, a writable `/dev/kvm`, an ISO writer
(`genisoimage` or `xorriso`) and network access to `cloud.debian.org`. The base
image is cached under `$EMS_APPLIANCE_VM_CACHE` and verified against the
published `SHA512SUMS` on every run. Nothing is installed on the developer host:
`rpi-image-gen`'s dependency set — `mmdebstrap`, `podman`, `uidmap`, `pv`,
`btrfs-progs`, `dctrl-tools`, `python3-jsonschema`, `cryptsetup`, `flex` — and
the `qemu-aarch64` binfmt handler are installed inside the disposable guest.

### Why a container is not enough

The tier these replaced ran systemd inside a privileged container, and systemd
never finished booting there. That is not a slow test, it is an absent one: a
container that never builds a systemd transaction cannot disprove anything about
unit ordering. Two defects lived behind it — a `Requires=` on a mount unit that
only exists on an A/B image, which failed the install on every single-slot host,
and a host key generation that could never succeed on a real appliance.

### What the guest tier deliberately does not claim

`appliance-guest-ab-systemd.sh` assembles an A/B appliance inside the guest from
upstream's own generators and udev rules, and proves the six shared binds, the
fail-open catch, the unit ordering, the failure propagation and the host
identity. It does not produce a healthy A/B verdict, and says so: discovery
anchors on `/proc/device-tree/chosen/bootloader/partition`, which the Raspberry
Pi firmware writes and a generic QEMU guest does not have. Faking it would make
the verdict a statement about the fake, so that case is reported NOT RUN and
belongs to [../appliance/ab-hardware-validation.md](../appliance/ab-hardware-validation.md).

### Reading the build back

A build produces a `build-authority.json` beside the image. The inspectors
(`appliance-inspect-rpi-ab-image.sh`, `appliance-inspect-rpi-ab-update.sh`) and
the strict gate (`appliance-release-gates.sh`) read the artefacts rather than
the build log, so a build that half-succeeded is caught by the thing that reads
its output, not by the thing that produced it.

## What tests do not cover

Automated tests reduce risk but do not replace real hardware validation: dry-run
checks, watching the first live run, and per-installation review of power and
SOC limits are still required. See [../user/safety.md](../user/safety.md).

For an on-hardware measurement (not part of the offline suite) of how fast an
MQTT `outputLimit` write reaches the inverter, see
[mqtt-write-latency-probe.md](mqtt-write-latency-probe.md).
