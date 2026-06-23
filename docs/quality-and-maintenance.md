# Built for Real Hardware

EMS SolarFlow Control does more than show nice numbers on a dashboard. It reads live meter values, calculates inverter targets, and can control real Zendure hardware. That is why the project is built with a strong focus on repeatable installs, safe defaults, automated checks, and clear diagnostics.

The goal is simple: users should be able to install it, understand what is happening, and trust that changes are tested before they reach a release.

## Tested Before Release

The project uses automated checks on pull requests and on changes to `main`. At the time of this documentation update, the test suite contains 1000+ tests.

The checks include:

* Python test suite on supported Python versions
* Ruff linting
* CodeQL security analysis
* dependency and import checks
* Python compile checks
* deprecation warning checks
* control-logic regression tests
* Docker image smoke tests
* Docker Compose first-run checks
* InfluxDB analytics end-to-end tests

These checks do not make bugs impossible. But they make regressions much harder to miss, especially in the Docker-first path most users will run.

## Maintained Docker Images

Docker images are built for `linux/amd64` and `linux/arm64`.

Images are rebuilt on releases, on relevant changes to `main`, manually when needed, and at least weekly. This helps keep the image fresh and allows upstream base-image fixes to be picked up regularly.

For stable installations, pin a release tag in `docker-compose.yml`. Use `latest` only if you intentionally want the newest state from `main`.

## Safe by Default

EMS is designed to avoid accidental hardware writes during first setup.

An untouched template config will not control real devices. If required placeholders are still present, EMS switches into a safe startup mode with control disabled, dry-run enabled, and hardware writes blocked.

Before live operation, users should configure their real meter, Zendure devices, serial numbers, limits, and SOC settings, then run diagnostics and watch the first live run.

## Kept Up to Date

Dependabot tracks dependency updates for Python, Docker, and GitHub Actions. Ruff keeps code style and common issues visible. CodeQL adds another automated layer for finding potential security problems.

Updates still need tests and review. The project does not claim that every bug or vulnerability is impossible. Instead, the development process is transparent, automated, and built to catch common mistakes early.

## Honest Limitations

Every installation is different. Firmware behavior, network quality, meter direction, battery size, PV layout, wiring, and local limits can all affect the result.

Automated tests reduce risk, but users still need to validate their own setup. EMS gives them the tools to do that: clear configuration, diagnostics, dry-run mode, backup/restore, and a Docker setup that is meant to be repeatable.
