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

## Release / review archives

Build source archives with `git archive` so only tracked files are included.
This keeps local runtime data and secrets (`.venv/`, `__pycache__/`,
`data/*.sqlite`, `data/influxdb/`, `deploy/docker/influxdb.env`, …) out of the
archive, since those are all gitignored:

```bash
git archive --format=tar.gz -o ../ems-solarflow-api-control-clean.tar.gz HEAD
```

Do not hand-roll archives with `tar`/`zip` from the working tree — those pull in
ignored runtime/build artifacts and may leak local InfluxDB tokens.

## Dependencies and security scanning

Dependabot tracks Python, Docker and GitHub Actions updates. CodeQL security
scanning is enabled through GitHub default setup for this repository; it is not
a checked-in workflow file. Updates still require passing tests and review.
