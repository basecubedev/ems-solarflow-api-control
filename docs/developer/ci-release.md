# CI and release

How continuous integration and image publishing work for this repository. For
the local test commands these mirror, see [testing.md](testing.md).

## Naming

A workflow is named for the product it ships or gates, and every job it runs
carries that product as its first word. Three prefixes, and the set is closed:

| Prefix | Ships | Workflows |
|---|---|---|
| `Appliance ` | the Raspberry Pi product — Manager `.deb`, OS image, fleet index | `appliance-image.yml`, `appliance-manager-release.yml` |
| `EMS ` | the paired EMS controller and Admin console | `docker-publish.yml`, `docker-feature-publish.yml`, `docker-feature-cleanup.yml`, `admin-replacement-canary.yml`, `generated-config-template.yml` |
| `Repo ` | ships nothing, gates both | `simulated-regression-tests.yml`, `playwright-e2e.yml` |

There is deliberately no `Admin ` prefix: the two container images ship as one
`build_id`/`channel` identity and `admin/releases.py` treats them as a single
system build, so splitting them in the pipeline names would describe something
the code does not do.

The prefix is not decoration. Branch protection and the required-check picker
show a job's name with no workflow around it, and two workflows here each had a
job called `publish` — one publishing an OS image, one publishing a signed
package. They now read `Appliance image publish` and `Appliance Manager
publish`.

Job **names** are display text and free to change. Job **ids** are not: they
carry the `needs:` graph, and `tests/test_appliance_release_identity_gate.py`,
`tests/test_appliance_image_release_contents.py` and
`tests/test_docker_publish_workflow.py` index them directly. A name that has
been entered as a required status check is frozen in a third way — renaming it
orphans the check, and pull requests then block for good on a status that will
never report again.

## Tagging

Each product is released by pushing a tag, and each has its own namespace. The
namespaces are not cosmetic: `admin/releases.py` offers every non-draft release
of this repository to the operator as an EMS system build and decides which ones
by `VERSION_PATTERN.fullmatch(tag)`. There is no product marker on a release, so
the tag's shape is the only thing telling the console that an OS image is not an
EMS build it can install.

| Tag | Product | Runs | Produces |
|---|---|---|---|
| `v<x.y.z>` | EMS + Admin | `docker-publish.yml` | the two GHCR images, plus `latest` on `main` |
| `appliance-manager-v<x.y.z>` | Appliance Manager | `appliance-manager-release.yml` | a signed `.deb` and the index the fleet reads |
| `appliance-image-v<x.y.z>` | Appliance OS image | `appliance-image.yml` | one prerelease carrying an image per board |

`v<x.y.z>` is the only namespace allowed to parse as a version, and nothing else
may ever take that shape. `tests/test_appliance_image_tag_namespace.py` reads the
patterns back out of the workflows and instantiates them with values chosen to
break them, so a workflow added later cannot quietly claim the shape.

Two of these tags can never be reused. `appliance-manager-release.yml` refuses a
version whose release already exists, and the image workflow refuses a tag whose
release is already published — a published version is not rewritten, because an
appliance that already fetched it would be holding different bytes under the same
name. Cutting the wrong tag therefore costs the version, not just the run.

No product records its version in the source tree. A tag is passed into the build
and stamped into the artefact — an OCI label for the EMS images, the `Version:`
field for the Manager package — and read back from the artefact at runtime. A
build with no tag behind it is a development build and says so
(`0.0.0~dev.<revision>` for the appliance, the `latest` channel with an empty
version for EMS), in a form that sorts below every release and that `is_stable`
refuses to publish.

The image workflow still publishes weekly under `appliance-image-ci-<run>`. A tag
push is for a build worth naming — a hardware-validation round, a support thread,
a line in a document — and both forms publish as a *prerelease*: a hosted runner
is refused at signing time by design, so nothing this workflow builds is signed,
whatever started it.

`appliance-manager-index` is not in the table because it is not a release
namespace: it is one fixed tag holding the index, pinned by absolute URL inside
every flashed image, and it can never move. See
[../appliance/manager-releases.md](../appliance/manager-releases.md).

## Continuous integration

Workflow: `.github/workflows/simulated-regression-tests.yml`, named **Repo PR
gate**. It runs on every pull request and on pushes to `main`:

- `Repo static checks` — Ruff lint, compile check, generated config template and
  `node --check admin/static/admin.js`
- `Repo python <group> (<python-version>)` — the four functional groups (`core`,
  `admin`, `mqtt`, `power-control`) across the supported Python versions
  (currently 3.11 and 3.14), each through `scripts/test-pr.sh`
- `Repo power-control regression`
- `Repo System Build compatibility`
- `Repo docker smoke test`, `Repo docker-first setup e2e`,
  `Repo InfluxDB analytics e2e`
- `Repo PR gate` — waits on all of the above and fails if any of them did

The groups are an exact partition of the non-Docker suite. Functional markers
overlap by design, so execution ownership is resolved by the fixed priority
`docker > power-control > mqtt > admin > core`; each group subtracts the groups
that outrank it. `tests/test_test_classification.py` fails when the union stops
covering the full collection *and* when any two groups would run the same test.
Each job prints its selection and fails when that selection unexpectedly
collects nothing.

`Repo PR gate` is the intended required status check for `main`. Branch
protection holds a list of names and cannot say "all of them", so requiring the
jobs individually means naming every matrix cell and leaving every future cell
unrequired. The gate waits on all of them instead, which keeps the required list
at one name per workflow while new jobs are picked up by adding them to its
`needs`. `tests/test_ci_gate_covers_every_job.py` fails when a job is added
without being listed.

The offline power-control regression tests behind it are deterministic and need
no hardware, network, or secrets.

See [testing.md](testing.md) for the marker dimensions and the matching local
commands, and [../quality-and-maintenance.md](../quality-and-maintenance.md)
for the user-facing summary of what is checked before release.

## What runs on a merge and not on a pull request

There is no nightly schedule. A pull request answers in about fifteen minutes,
and the work that cannot fit in fifteen minutes runs on the push to `main`
instead — after the merge, where forty minutes costs nobody a wait. Both jobs
carry `if: github.event_name == 'push'`, so a pull request skips them and the
gate counts a skip as a pass.

In `simulated-regression-tests.yml`:

- `Repo full suite (<version>)` — the whole non-Docker collection in one
  process on 3.11 and 3.14, plus the strict deprecation-warning run
  (`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `-W error::DeprecationWarning`)

In `playwright-e2e.yml`:

- `Repo browser <browser> full` — the complete Chromium and Firefox Admin
  suites, the same projects the pull request runs `--grep`-narrowed

The full suite is not extra coverage. The five pull-request groups are an exact
partition of the same collection, so every test in it already ran. What one
process adds is the only place an interaction *between* two tests can appear —
the InfluxDB readiness timeout that failed in the full suite and passed in
isolation is what that looks like. Running it before a merge would mean paying
forty minutes for a class of failure that a merge is early enough to catch.

The Docker-first and System Build tiers used to be duplicated on the nightly
schedule. They are not duplicated anywhere now: `Repo docker-first setup e2e`
and `Repo System Build compatibility` run the identical selections on every
pull request.

`Repo pinned rpi-image-gen tree` runs on both, because it takes about twelve
seconds and the upstream-tree tests skip themselves unless a job fetches a real
tree — without it nothing exercises the contract the image is built against.

The Admin replacement journey is deliberately not duplicated. It may only run
against immutable published digests, which
`.github/workflows/admin-replacement-canary.yml` resolves from the Development
build catalogue, verifies with `scripts/verify_development_catalogue.py` and
pulls after a GHCR login. That workflow owns the gate end to end and runs on its
own weekly schedule plus `workflow_dispatch`; a missing or mutable target fails
it as a blocked precondition instead of skipping.

## Generated config template

Workflow: `.github/workflows/generated-config-template.yml`. `config/config.json`
is generated from `ems/config_catalog.py` via `tools/build_config_template.py`.
When a pull request changes the catalog, the builder, or the template, this
workflow regenerates and updates `config/config.template.json` so the tracked
template never drifts from the catalog.

## Docker image publishing

Workflow: `.github/workflows/docker-publish.yml`, named **EMS release** (job **EMS release publish
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

Workflow: `.github/workflows/docker-feature-publish.yml` ("EMS development
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
