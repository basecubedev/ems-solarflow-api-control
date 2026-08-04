# CI and release

How continuous integration and image publishing work for this repository. For
the local test commands these mirror, see [testing.md](testing.md).

## Continuous integration

Workflow: `.github/workflows/simulated-regression-tests.yml` (job name
**Continuous Integration**). It runs on every pull request and on pushes to
`main`:

- `Static checks` — Ruff lint, compile check, generated config template and
  `node --check admin/static/admin.js`
- `python-<group> (<python-version>)` — the four functional groups (`core`,
  `admin`, `mqtt`, `power-control`) across the supported Python versions
  (currently 3.11 and 3.14), each through `scripts/test-pr.sh`
- `Simulated power-control regression tests`
- `System Build compatibility gate`
- `Docker smoke test`, `Docker-first setup e2e`, `InfluxDB analytics e2e`

The groups are an exact partition of the non-Docker suite. Functional markers
overlap by design, so execution ownership is resolved by the fixed priority
`docker > power-control > mqtt > admin > core`; each group subtracts the groups
that outrank it. `tests/test_test_classification.py` fails when the union stops
covering the full collection *and* when any two groups would run the same test.
Each job prints its selection and fails when that selection unexpectedly
collects nothing.

The offline power-control regression tests are the intended required status
check for `main` in branch protection or repository rulesets. They are
deterministic and need no hardware, network, or secrets.

The former monolithic `Full Python test suite (<version>)` job moved to
`.github/workflows/nightly-full-suite.yml`; branch protection referring to it
by name has to be repointed at the group jobs.

See [testing.md](testing.md) for the marker dimensions and the matching local
commands, and [../quality-and-maintenance.md](../quality-and-maintenance.md)
for the user-facing summary of what is checked before release.

## Nightly full suite

Workflow: `.github/workflows/nightly-full-suite.yml` (schedule plus
`workflow_dispatch`). It runs what the pull-request workflow deliberately
splits or narrows:

- the full non-Docker Python suite on 3.11 and 3.14, plus the strict
  deprecation-warning run
- the complete Chromium and Firefox Admin suites
- the Docker-first tier and the System Build tier

The Admin replacement journey is deliberately **not** part of this workflow. It
may only run against immutable published digests, which
`.github/workflows/admin-replacement-canary.yml` resolves from the Development
build catalogue, verifies with `scripts/verify_development_catalogue.py` and
pulls after a GHCR login. That workflow owns the gate end to end and runs on its
own weekly schedule plus `workflow_dispatch`; a missing or mutable target fails
it as a blocked precondition instead of skipping.

**A successful Admin Replacement Canary run is part of RC readiness.** Record
the run URL together with both tags, revisions, build IDs and digests it used.

### Two immutable Development builds, no mutable source

Both sides of the replacement are published Development builds, addressed by
digest. `scripts/resolve_canary_builds.py` reads the public catalogue and
returns a pair:

- **target** — the newest installable build, or `target_tag` from
  `workflow_dispatch`
- **source** — the newest *older* installable build of the target's feature
  branch, or `source_tag` from `workflow_dispatch`

The source Admin used to be the mutable `:latest` release. That made the gate
unrunnable before the first release cut from a branch: the shared page objects
address the Admin through `data-testid` hooks (`start-fresh-install`,
`system-build-select`, `system-build-status`, `system-build-reload`,
`upgrade-system-build-reload`, `continue-button`, `admin-update-button`,
`embedded-resources-check`), and a release predating those hooks fails at the
first locator regardless of the target. Pinning the source to a published
Development build of the same branch removes the dependency on a release
without weakening anything: the resolver refuses a mutable digest, refuses a
pair whose Admin digests are equal, and blocks when no pair exists.

Test-hook compatibility is never inferred from tag naming.
`tests/e2e/admin-test-contract.json` is the single source of truth for the
version and the hook list. New catalogue entries declare it as
`admin_test_contract`, and an entry declaring a different version is dropped
during resolution. The authority for what an image actually serves is
`scripts/admin_test_contract.py`, which the runner executes against the source
and the target Admin before any container starts and which names every missing
hook. Do not answer a failure there by teaching the shared page objects a
second, legacy markup contract — that would couple every Setup spec to a
released UI generation. Pick a build that serves the hooks instead.

`./scripts/test-rc.sh` runs the same gates locally before a release candidate.
Its `admin-replacement` gate needs the same immutable identities, so the script
names the missing variables in its preflight rather than failing an hour in.
`scripts/resolve_canary_builds.py --catalogue <file>` prints exactly those
variable values for a downloaded catalogue.

## Generated config template

Workflow: `.github/workflows/generated-config-template.yml`. `config/config.json`
is generated from `ems/config_catalog.py` via `tools/build_config_template.py`.
When a pull request changes the catalog, the builder, or the template, this
workflow regenerates and updates `config/config.template.json` so the tracked
template never drifts from the catalog.

## Docker image publishing

Workflow: `.github/workflows/docker-publish.yml` (job **Build and publish Docker
image**). It builds `linux/amd64` and `linux/arm64` images and publishes to
GHCR. It runs on:

- pushes to `main` and `v*` tags,
- a weekly schedule (so upstream base-image fixes are picked up), and
- manual `workflow_dispatch`.

Tag builds are only published if the tag commit is contained in `main`. Images
carry OCI and build-identity labels (`release_tag`, `build_serial`, `build_id`,
`channel`) derived from the build-identity step; EMS derives its runtime version
from this build identity. The Admin Console image is published as
`ghcr.io/basecubedev/ems-solarflow-admin`.

For stable installations, pin a release tag in `docker-compose.yml` rather than
`latest`.

## Development build publishing (testing only)

Workflow: `.github/workflows/docker-feature-publish.yml` ("Docker Feature
Publish"). Manual-only (`workflow_dispatch`) build of the EMS and Admin images
from a development ref, without touching the stable release path. It never
publishes `latest`, `v*`, `stable`, or `rc`, marks its images with
`de.basecubedev.ems.channel=development`, and writes a public **development
build catalogue** entry only after the pushed images pass remote verification.

The workflow resolves the single required `ref` input to one immutable full SHA
once (in `resolve-source`); every gate, the publish job and every post-publish
job then check out that exact SHA, and the publish job re-verifies its checkout
equals the resolved SHA. A `Reject protected release refs` guard in
`resolve-source` fails the run before any gate or package write when the input —
or the commit a tag at HEAD points at — is a protected release ref (`main`,
`master`, `latest`, `stable`, `rc`, or a `v*` tag). Intended development refs
(`feature/…`, `fix/…`, `develop/…`) are allowed. Job permissions are
least-privilege: gates are `contents: read`, only `publish-feature-ghcr` holds
`packages: write`, and only the catalogue jobs hold `contents: write`.

### Pre-publish gates

`publish-feature-ghcr` depends on all four gates below, so no image is pushed
before every one of them is green. Each checks out the resolved SHA, never the
mutable `ref` input.

| Job | What it runs |
| --- | --- |
| `mqtt-release-contract` | `ruff check .`, `compileall`, `tools/build_config_template.py --check`, `node --check admin/static/admin.js`, then `pytest -m mqtt_release` and `pytest -m "simulation and power_control"` |
| `packaged-system-build-smoke` | packaged Admin three-build Chromium smoke |
| `mosquitto-lifecycle` | the real-broker contract against a live Mosquitto |
| `package-smoke` | Admin/EMS image builds plus the paired startup contract |

`mqtt-release-contract` fails when either pytest selection executes no tests, so
a renamed marker cannot turn an empty run into a green release gate.

`mosquitto-lifecycle` runs the complete documented real-broker set —
`test_zendure_mqtt_broker_mosquitto.py`, `test_mqtt_real_mosquitto.py`,
`test_mqtt_real_mosquitto_acl.py`, `test_mqtt_real_mosquitto_tls.py`,
`test_mqtt_real_legacy_flow.py`. `tests/test_mqtt_release_fail_closed.py` owns
that list and `tests/test_docker_feature_publish_workflow.py` pins the workflow
to it, so the two cannot drift. With `EMS_REQUIRE_REAL_MQTT_TESTS=1` a missing
Docker CLI, an unreachable daemon, a broker that fails to start, or an
all-skipped run fails the gate instead of passing quietly.

Both images publish under the sanitized `dev-<safe-ref>` prefix plus the
canonical immutable tag (`dev-<branch-slug>-<ref-hash>-<short-sha>-<run-id>-<attempt>`),
for both `ghcr.io/basecubedev/ems-solarflow-api-control` and
`ghcr.io/basecubedev/ems-solarflow-admin`. See
[../technical/system-build-pairing.md](../technical/system-build-pairing.md)
for the paired build identity and tag policy.

> **Warning:** Development builds are for testing only. They do not carry a
> stable release version and must not be used for stable installations.

### Safe publish order

The workflow must already exist on the repository **default branch** before it
can be dispatched reliably — a `workflow_dispatch` workflow is only selectable
once its file is present on the default branch. The bootstrap is a separate,
workflow-only commit off `main` (branch `ci/development-build-workflow-bootstrap`);
it must never carry the feature implementation. Then:

1. Merge the workflow-only bootstrap into the default branch.
2. Push the feature branch.
3. Open **Docker Feature Publish** in GitHub Actions.
4. Select the feature branch as the workflow ref.
5. Enter the same feature ref as the required `ref` input.
6. Confirm the resolved immutable SHA printed by `resolve-source`.
7. Allow all release gates to complete (see **Pre-publish gates** above).
8. The catalogue entry is written only after the pushed images are pulled back
   and the remote packaged Admin browser canary passes.

Never bypass a failed gate by editing the catalogue by hand. If any
post-push verification fails, no installable catalogue entry is created or kept;
use `.github/workflows/docker-feature-cleanup.yml` to remove the produced tags.

Cleanup: `docker-feature-cleanup.yml` runs on branch deletion. It sanitizes the
deleted branch name with the same logic and deletes only GHCR versions tagged
`dev-<safe-ref>` or `dev-<safe-ref>-*` and their catalogue entries, leaving
release tags untouched.

## Build caching

Both publish workflows use the native BuildKit GitHub Actions cache
(`type=gha`) plus the built-in pip/npm caches on the host setup steps. Caching
is a performance optimization only: a cache hit never stands in for a build,
validation, or publication gate, and never becomes part of image identity.

Four owned, stable cache scopes keep layers from leaking between images or
pipelines:

| Scope | Used by |
| --- | --- |
| `ems-release-v1` | EMS release builds (`docker-publish.yml`) |
| `admin-release-v1` | Admin release builds (`docker-publish.yml`) |
| `ems-feature-v1` | EMS feature builds (`docker-feature-publish.yml`) |
| `admin-feature-v1` | Admin feature builds (`docker-feature-publish.yml`) |

EMS and Admin never share a scope, and feature builds never write into a
release scope. Scopes carry nothing volatile (no SHA, run ID, attempt or tag),
so they stay stable across commits and reruns and actually reuse. Bump the
`-v1` suffix (defined once as workflow-level `env`) for a deliberate reset.

Per pipeline: the local single-platform validation build **imports** its scope;
the final multi-platform push is the authoritative **exporter** (`mode=max`).
In the feature pipeline, `package-smoke` runs first and exports both feature
scopes to warm them for `publish-feature-ghcr`, and the packaged Admin browser
gate is prebuilt from `admin-feature-v1`. A cache miss, empty cache, or
unavailable entry simply rebuilds; no build carries `continue-on-error`, and
digest-based remote verification of published images is unchanged.

## Release / review archives

`.gitignore` controls what Git **tracks**; `.dockerignore` controls what enters
the **Docker build context**. They are separate lists — a path excluded from
Git is not automatically excluded from the Docker context, so both must cover
local secrets and runtime data. The repository may be built from a real working
installation, not only a clean checkout, so `.dockerignore` excludes
`config/`, `data/`, `.env`, `backup/`, `deploy/docker/influxdb.env`, local
virtualenvs, Node modules and caches while still re-including the required
`config/config.template.json` and build scripts.

Build source archives with `git archive` so only tracked files are included.
This keeps local runtime data and secrets (`.venv/`, `__pycache__/`,
`data/*.sqlite`, `data/influxdb/`, `deploy/docker/influxdb.env`, …) out of the
archive, since those are all gitignored:

```bash
git archive --format=tar.gz -o ../ems-solarflow-api-control-clean.tar.gz HEAD
```

Do not hand-roll archives with `tar`/`zip` from the working tree — those pull in
ignored runtime/build artifacts and may leak local credentials. A
working-directory archive may contain local Admin/EMS credentials (Zendure
tokens, MQTT secrets, InfluxDB tokens) and must never be uploaded as a release
artifact.

## Dependencies and security scanning

Dependabot tracks Python, Docker and GitHub Actions updates. CodeQL security
scanning is enabled through GitHub default setup for this repository; it is not
a checked-in workflow file. Updates still require passing tests and review.
