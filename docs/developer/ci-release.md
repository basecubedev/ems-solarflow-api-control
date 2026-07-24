# CI and release

How continuous integration and image publishing work for this repository. For
the local test commands these mirror, see [testing.md](testing.md).

## Continuous integration

Workflow: `.github/workflows/simulated-regression-tests.yml` (job name
**Continuous Integration**). It runs on every pull request and on pushes to
`main`, across supported Python versions (currently 3.11 and 3.14):

- verify dependency imports
- Ruff lint (`ruff check .`)
- compile check (`python -m compileall ems dashboard scripts tests …`)
- the full Python test suite, including the offline power-control regression
  tests (`pytest -m "simulation and power_control"`)

The offline power-control regression tests are the intended required status
check for `main` in branch protection or repository rulesets. They are
deterministic and need no hardware, network, or secrets.

Other coverage that runs in CI includes Docker image smoke tests, Docker Compose
first-run checks, and InfluxDB analytics end-to-end tests. See
[../quality-and-maintenance.md](../quality-and-maintenance.md) for the
user-facing summary of what is checked before release.

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
7. Allow all release gates to complete (static, non-Docker regression,
   `mqtt_release`, `system_build`, Playwright, packaged smoke, real Mosquitto).
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
