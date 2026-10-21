# Third-Party Licenses

This project's own source code is licensed under the GNU Affero General Public
License v3.0 or later (see [`LICENSE`](LICENSE)). Every third-party component
listed here keeps its original upstream license.

This file is the authoritative human-readable third-party inventory for the
repository. `tools/check_third_party_licenses.py` and
`tests/test_third_party_licenses.py` verify it against the actual manifests, so
a new dependency fails CI until it is documented here.

## How To Read This Inventory

| Column | Meaning |
|---|---|
| Component | The package, image or asset name exactly as the manifest declares it |
| Version | The declared constraint, and the version this inventory was verified against |
| License (SPDX) | SPDX identifier or expression; `mixed` when a component aggregates many licenses |
| Used for | Why this project needs it |
| Runtime | Executed by the EMS controller, the Dashboard or the Admin Console in normal operation |
| Distributed | Shipped inside a published container image or a source/release package |
| Upstream | Project homepage or repository |

`Distributed` is the license-relevant column: components marked `❌` exist only
on a developer or CI machine and are never redistributed by this project.

Scope: this inventory covers **direct** dependencies exhaustively and the
**runtime transitive** closure that ends up inside the published images.
Development-only transitive trees (for example everything `pytest` pulls in) are
resolved by the package managers and are not enumerated here.

## Runtime Dependencies

### Python Runtime Dependencies (Direct)

Declared in [`requirements.txt`](requirements.txt) (EMS image) and
[`deploy/admin/requirements.txt`](deploy/admin/requirements.txt) (Admin image).
Both images install the same four packages; the Admin manifest pins its own
compatible ranges.

| Component | Version | License (SPDX) | Used for | Runtime | Distributed | Upstream |
|---|---|---|---|:---:|:---:|---|
| `requests` | `>=2.34.2` (verified 2.34.2) | Apache-2.0 | HTTP client for Zendure local/cloud APIs, Shelly / EcoTracker / Tasmota grid meters and InfluxDB | ✅ | ✅ | https://github.com/psf/requests |
| `paho-mqtt` | `>=2.1.0` (verified 2.1.0) | EPL-2.0 OR BSD-3-Clause | MQTT client for local brokers and Zendure cloud MQTT telemetry and control | ✅ | ✅ | https://github.com/eclipse-paho/paho.mqtt.python |
| `cryptography` | unpinned (verified 49.0.0) | Apache-2.0 OR BSD-3-Clause | AEAD backup archives, Admin secret/credential store, self-signed HTTPS certificates | ✅ | ✅ | https://github.com/pyca/cryptography |
| `zeroconf` | `>=0.150.0` (verified 0.150.0) | LGPL-2.1-or-later | Optional live mDNS device discovery in the Admin Console; absence degrades gracefully | ✅ | ✅ | https://github.com/python-zeroconf/python-zeroconf |

### Python Runtime Dependencies (Transitive)

Resolved by pip from the four direct packages above and therefore installed
into the published images.

| Component | Version | License (SPDX) | Used for | Runtime | Distributed | Upstream |
|---|---|---|---|:---:|:---:|---|
| `urllib3` | 2.7.0 | MIT | HTTP connection pooling for `requests`; also imported directly by `ems/clients.py` for its `Retry` policy | ✅ | ✅ | https://github.com/urllib3/urllib3 |
| `certifi` | 2026.5.20 | MPL-2.0 | CA trust bundle used by `requests` | ✅ | ✅ | https://github.com/certifi/python-certifi |
| `idna` | 3.18 | BSD-3-Clause | Internationalized host names for `requests` | ✅ | ✅ | https://github.com/kjd/idna |
| `charset-normalizer` | 3.4.7 | MIT | Response encoding detection for `requests` | ✅ | ✅ | https://github.com/jawah/charset_normalizer |
| `cffi` | 2.0.0 | MIT | C bindings used by `cryptography` | ✅ | ✅ | https://github.com/python-cffi/cffi |
| `pycparser` | 3.0 | BSD-3-Clause | C header parsing for `cffi` | ✅ | ✅ | https://github.com/eliben/pycparser |
| `ifaddr` | 0.2.0 | MIT | Network interface enumeration for `zeroconf` | ✅ | ✅ | https://github.com/pydron/ifaddr |
| `typing-extensions` | not installed | PSF-2.0 | Conditional `cryptography` dependency for `python_full_version < 3.11`; the images run Python 3.14, so it is never installed | ❌ | ❌ | https://github.com/python/typing_extensions |

### Distributed Third-Party Binaries

Prebuilt upstream binaries copied into the published images. They are not
Python packages and do not appear in any requirements file.

| Component | Version | License (SPDX) | Used for | Runtime | Distributed | Upstream |
|---|---|---|---|:---:|:---:|---|
| `influx` | 2.9 (from the `influxdb:2.9` build stage) | MIT | InfluxDB backup/restore from inside the EMS container without a Docker socket | ✅ | ✅ | https://github.com/influxdata/influx-cli |
| `docker/cli` | 27.5.1 | Apache-2.0 | Admin Console controls the host Docker engine through the mounted socket; client only, never a daemon | ✅ | ✅ | https://github.com/docker/cli |
| `docker/compose` | 2.32.4 | Apache-2.0 | Compose plugin for the Admin Console's container lifecycle operations | ✅ | ✅ | https://github.com/docker/compose |

## Development Dependencies

Everything in this chapter is **development only** and **not distributed with
production images**. The `Dockerfile` copies neither `tests/`, `node_modules/`
nor the development requirements; only `scripts/influx_utils.py` and
`scripts/mqtt_write_latency_probe.py` are shipped, and both are project code.

### Python Development Dependencies

Declared in [`requirements-dev.txt`](requirements-dev.txt), and for the
documentation website only in [`requirements-docs.txt`](requirements-docs.txt).

| Component | Version | License (SPDX) | Used for | Runtime | Distributed | Upstream |
|---|---|---|---|:---:|:---:|---|
| `pytest` | unpinned (verified 9.1.0) | MIT | Test framework for the whole Python suite | ❌ | ❌ | https://github.com/pytest-dev/pytest |
| `ruff` | `==0.15.22` | MIT | Lint and static checks (`ruff check .`) | ❌ | ❌ | https://github.com/astral-sh/ruff |
| `PyYAML` | `>=6.0` (verified 6.0.3) | MIT | Parsing CI workflow and compose YAML inside contract tests | ❌ | ❌ | https://github.com/yaml/pyyaml |
| `zensical` | `==0.0.71` | MIT | Builds the documentation website from `docs/` ([docs-site.md](docs/developer/docs-site.md)) | ❌ | ❌ | https://github.com/zensical/zensical |

### Node Development Dependencies

Declared in [`package.json`](package.json); `playwright` and `playwright-core`
are resolved through [`package-lock.json`](package-lock.json). The private
`ems-admin-e2e` package carries the Playwright end-to-end tests and the builds
of the two Modern front ends, the dashboard (`npm run build:dashboard-modern`)
and the Admin console (`npm run build:admin-modern`), both made from the design
system in `frontend/shared/`. The builds run on a developer or CI machine,
never in an image; their output is committed under `dashboard/static/modern/`
and `admin/static/modern/`, and what of it reaches a browser is listed under
Vendored Components below. Svelte, Vite and the Vite plugin are build tools;
Inter and Lucide are copied into the output.

| Component | Version | License (SPDX) | Used for | Runtime | Distributed | Upstream |
|---|---|---|---|:---:|:---:|---|
| `@playwright/test` | `^1.61.1` (locked 1.61.1) | Apache-2.0 | Admin Console end-to-end test runner | ❌ | ❌ | https://github.com/microsoft/playwright |
| `@fontsource-variable/inter` | `5.3.0` | OFL-1.1 | Source of the Inter variable font copied into the Modern builds | ❌ | ❌ | https://github.com/fontsource/font-files |
| `@sveltejs/vite-plugin-svelte` | `7.3.1` | MIT | Compiles the Modern front ends' Svelte components during the Vite build | ❌ | ❌ | https://github.com/sveltejs/vite-plugin-svelte |
| `lucide-static` | `1.59.0` | ISC | Source of the icon paths copied into the Modern sprites | ❌ | ❌ | https://github.com/lucide-icons/lucide |
| `svelte` | `5.57.2` | MIT | Component compiler of the Modern front ends; its runtime is bundled (Vendored Components) | ❌ | ❌ | https://github.com/sveltejs/svelte |
| `vite` | `8.3.4` | MIT | Builds the Modern front ends into `dashboard/static/modern/` and `admin/static/modern/` | ❌ | ❌ | https://github.com/vitejs/vite |
| `playwright` | 1.61.1 | Apache-2.0 | Browser automation library behind the test runner | ❌ | ❌ | https://github.com/microsoft/playwright |
| `playwright-core` | 1.61.1 | Apache-2.0 | Browser protocol driver used by `playwright` | ❌ | ❌ | https://github.com/microsoft/playwright |

### Browser Runtimes

Downloaded on demand by `npx playwright install` into the Playwright browser
cache, or provided by the developer's operating system. They are never
committed to this repository and never shipped in an image.

| Component | Version | License (SPDX) | Used for | Runtime | Distributed | Upstream |
|---|---|---|---|:---:|:---:|---|
| `Chromium` | Playwright build for 1.61.1 | BSD-3-Clause AND mixed | `chromium` Playwright project | ❌ | ❌ | https://www.chromium.org/Home |
| `Firefox` | Playwright build for 1.61.1, plus the host Firefox | MPL-2.0 | `firefox` Playwright project, and headless screenshot/video capture for the documentation | ❌ | ❌ | https://hg.mozilla.org/mozilla-central |
| `WebKit` | Playwright build for 1.61.1 | LGPL-2.1-or-later AND BSD-2-Clause | Optional `webkit` Playwright project | ❌ | ❌ | https://github.com/WebKit/WebKit |

### External Developer Tools

Invoked as host executables by developer tooling. They are neither vendored nor
installed by any manifest in this repository.

| Component | Version | License (SPDX) | Used for | Runtime | Distributed | Upstream |
|---|---|---|---|:---:|:---:|---|
| `Docker Engine` | host install (Engine + Compose plugin) | Apache-2.0 | Docker-marked tests, image builds, Docker-first end-to-end runs | ❌ | ❌ | https://github.com/moby/moby |
| `Node.js` | 20 in CI | MIT AND mixed | Runs Playwright and the frontend contract runners under `tests/js/` | ❌ | ❌ | https://github.com/nodejs/node |
| `ffmpeg` | host install | LGPL-2.1-or-later, or GPL-2.0-or-later depending on the build | Encodes the Admin documentation videos (`scripts/render_admin_docs_video.py`) | ❌ | ❌ | https://git.ffmpeg.org/ffmpeg.git |
| `ImageMagick` | host install (`convert`) | ImageMagick | Trims and measures captured documentation screenshots | ❌ | ❌ | https://github.com/ImageMagick/ImageMagick |
| `Pillow` | host install (verified 12.2.0) | MIT-CMU | Renders the optional install demo animation (`docs/demo/build_install_demo.py`); see the License Notes below | ❌ | ❌ | https://github.com/python-pillow/Pillow |
| `DejaVu fonts` | host install | Bitstream-Vera AND Public-Domain | Glyphs for the install demo animation when present on the host | ❌ | ❌ | https://github.com/dejavu-fonts/dejavu-fonts |
| `xz-utils` | host install | GPL-2.0-or-later AND Public-Domain (liblzma) | Compresses the published appliance image (`scripts/appliance-build-rpi-image.sh`) | ❌ | ❌ | https://github.com/tukaani-project/xz |

### GitHub Actions

Used by the workflows under `.github/workflows/`. They run on GitHub-hosted
runners and are never part of a build artifact.

| Component | Version | License (SPDX) | Used for | Runtime | Distributed | Upstream |
|---|---|---|---|:---:|:---:|---|
| `actions/checkout` | v7 | MIT | Checks out the repository in every job | ❌ | ❌ | https://github.com/actions/checkout |
| `actions/setup-python` | v7 | MIT | Provisions the CI Python versions | ❌ | ❌ | https://github.com/actions/setup-python |
| `actions/setup-node` | v7 | MIT | Provisions Node for Playwright and the frontend runners | ❌ | ❌ | https://github.com/actions/setup-node |
| `actions/upload-artifact` | v7 | MIT | Publishes test reports and traces | ❌ | ❌ | https://github.com/actions/upload-artifact |
| `actions/download-artifact` | v8 | MIT | Collects the appliance build outputs between jobs for signing and publishing | ❌ | ❌ | https://github.com/actions/download-artifact |
| `actions/upload-pages-artifact` | v5 | MIT | Hands the built documentation website to GitHub Pages | ❌ | ❌ | https://github.com/actions/upload-pages-artifact |
| `actions/deploy-pages` | v5 | MIT | Publishes the documentation website on GitHub Pages | ❌ | ❌ | https://github.com/actions/deploy-pages |
| `docker/build-push-action` | v7 | Apache-2.0 | Builds and pushes the EMS and Admin images | ❌ | ❌ | https://github.com/docker/build-push-action |
| `docker/login-action` | v4 | Apache-2.0 | Authenticates against GHCR | ❌ | ❌ | https://github.com/docker/login-action |
| `docker/metadata-action` | v6 | Apache-2.0 | Derives image tags and OCI labels | ❌ | ❌ | https://github.com/docker/metadata-action |
| `docker/setup-buildx-action` | v4 | Apache-2.0 | Sets up multi-architecture BuildKit builders | ❌ | ❌ | https://github.com/docker/setup-buildx-action |

## Vendored Components

Third-party source committed into this repository. Classic, the hand-written
dashboard, has no package manager and no build step, so its chart library is
vendored verbatim from the upstream `dist/` output, and Modern references the
same copy. Modern is built with Vite from the npm packages above; its build
output, with the material it bundles or copies, is described below the table.

| Component | Version | License (SPDX) | Used for | Runtime | Distributed | Upstream | Files |
|---|---|---|---|:---:|:---:|---|---|
| `uPlot` | 1.6.31 | MIT | Canvas charts on the Dashboard History and Analytics tabs | ✅ | ✅ | https://github.com/leeoniya/uPlot | `dashboard/static/uPlot.iife.min.js`, `dashboard/static/uPlot.min.css`, `dashboard/static/uPlot.LICENSE` |
| `svelte` | 5.57.2 (runtime) | MIT | Component runtime bundled into the Modern dashboard and Admin console | ✅ | ✅ | https://github.com/sveltejs/svelte | `dashboard/static/modern/assets/modern.js`, `admin/static/modern/assets/admin.js`, notices in `dashboard/static/modern/licenses.json` and `admin/static/modern/licenses.json` |
| `clsx` | 2.1.1 | MIT | Class-name helper the Svelte runtime bundles | ✅ | ✅ | https://github.com/lukeed/clsx | `dashboard/static/modern/assets/modern.js`, `admin/static/modern/assets/admin.js`, notices in `dashboard/static/modern/licenses.json` and `admin/static/modern/licenses.json` |
| `Inter` (`@fontsource-variable/inter`) | 4.1 via 5.3.0 | OFL-1.1 | The Modern front ends' typeface, latin subset | ✅ | ✅ | https://github.com/rsms/inter | `dashboard/static/modern/assets/inter-latin-wght-normal.woff2`, `dashboard/static/modern/assets/modern.css` (`@font-face`), `dashboard/static/modern/Inter.LICENSE`, `admin/static/modern/assets/inter-latin-wght-normal.woff2`, `admin/static/modern/assets/admin.css` (`@font-face`), `admin/static/modern/Inter.LICENSE` |
| `Lucide` (`lucide-static`) | 1.59.0 | ISC AND MIT (Feather-derived icons) | Icon paths of the Modern front ends' inline sprites | ✅ | ✅ | https://github.com/lucide-icons/lucide | `dashboard/static/modern/index.html` and `admin/static/modern/index.html` (the `<svg class="icon-sprite">` block), `dashboard/static/modern/Lucide.LICENSE`, `admin/static/modern/Lucide.LICENSE` |
| `rpi-image-gen` | v2.7.0 | BSD-3-Clause | Upstream files copied verbatim as the image contract the appliance is built against (`image-rpios`) | ❌ | ❌ | https://github.com/raspberrypi/rpi-image-gen | `tests/fixtures/rpi_image_gen/` (seventeen files, listed with their SHA-256 in `source-manifest.json`), `tests/fixtures/rpi_image_gen/UPSTREAM.LICENSE` |

Provenance, verified on 2026-08-04 against the upstream `1.6.31` tag:

| File | SHA-256 |
|---|---|
| `dashboard/static/uPlot.iife.min.js` | `2d27e8ad3d228164525ce213f9dc716f39b4e3aee0cc773fb3491c96cf4921a2` |
| `dashboard/static/uPlot.min.css` | `df630c6a8d6f8eeaff264b50f73ce5b114f646ffd9a0bb74f049b0a00135fa04` |

Both files are byte-identical to `dist/uPlot.iife.min.js` and
`dist/uPlot.min.css` of the upstream tag. The JavaScript bundle carries the
upstream banner `/*! https://github.com/leeoniya/uPlot (v1.6.31) */`; the CSS
bundle carries no banner, which is why the full MIT text is kept next to both
files in [`dashboard/static/uPlot.LICENSE`](dashboard/static/uPlot.LICENSE)
(Copyright (c) 2022 Leon Sorokin). `Dockerfile` copies the whole `dashboard/`
tree, so the notice ships with every image.

The `rpi-image-gen` files are test fixtures: they are the upstream contract the
appliance image is built against, so a test that edited one would be testing the
project against itself. They are not imported by any runtime module and are not
copied into any image or package, which is why both Runtime and Distributed are
marked ✗. Their exact bytes and the release they came from are recorded in
[`tests/fixtures/rpi_image_gen/source-manifest.json`](tests/fixtures/rpi_image_gen/source-manifest.json);
refresh them with `scripts/appliance-fetch-rpi-image-gen.sh`.

No other third-party source is vendored. The Modern dashboard and the Modern
Admin console are build outputs, not hand-written source. Their bundles
(`dashboard/static/modern/assets/modern.js`, `admin/static/modern/assets/admin.js`)
and stylesheets combine the project's own AGPL-3.0-or-later code (in
`frontend/shared/`, `frontend/dashboard-modern/` and `frontend/admin-modern/`)
with the Svelte runtime and `clsx`. Vite writes the MIT notices of every
bundled package into a `licenses.json` that ships beside each bundle, and
`tests/test_dashboard_modern.py` and `tests/test_admin_modern.py` fail when a
package in that file has no row above or a licence outside the permissive set. The Inter font file is
unmodified (OFL-1.1 permits bundling it with software), and the Lucide icon
paths are copied verbatim into the page's inline sprite (ISC; the icons Lucide
took over from Feather stay MIT, "Copyright (c) 2013-present Cole Bemis").
Neither is bundled code, so the build copies each package's own licence file
beside them: `Inter.LICENSE` carries the OFL text and `Lucide.LICENSE` both
notices, with the Feather list, in `dashboard/static/modern/` and in
`admin/static/modern/`. Classic (`dashboard/static/` and `admin/static/`
outside their `modern/` folders) remains
project-authored HTML, CSS, inline SVG symbols and plain browser JavaScript;
they use system font stacks only and bundle no font file.

## Container Base Images

Base images and service images referenced by the `Dockerfile`s and compose
files. Each image ships its own operating-system package set with its own
licenses — this inventory documents the image, not the thousands of packages
inside it. Consult the image's own manifest (for example `dpkg -l`) or the
upstream image documentation for a package-level breakdown.

The images published by this project — `ghcr.io/basecubedev/ems-solarflow-api-control`
and `ghcr.io/basecubedev/ems-solarflow-admin` — are project artifacts under
AGPL-3.0-or-later and are not third-party components.

| Component | Version | License (SPDX) | Used for | Runtime | Distributed | Upstream |
|---|---|---|---|:---:|:---:|---|
| `python:3.14-slim` | 3.14-slim | PSF-2.0 (CPython) AND mixed (Debian base) | Base layer of the EMS image and the Admin image | ✅ | ✅ | https://hub.docker.com/_/python |
| `influxdb:2.9` | 2.9 | MIT | EMS image build stage; only `/usr/local/bin/influx` is copied out of it | ❌ | ✅ | https://hub.docker.com/_/influxdb |
| `influxdb:2.7` | 2.7 | MIT | Optional bundled Analytics database in `docker-compose.yml` and `deploy/docker/compose.influxdb.yml` | ✅ | ❌ | https://hub.docker.com/_/influxdb |
| `eclipse-mosquitto:2` | 2 | EPL-2.0 OR BSD-3-Clause | Local development broker and the ephemeral broker for optional real-MQTT tests | ❌ | ❌ | https://hub.docker.com/_/eclipse-mosquitto |
| `grafana/grafana-oss:latest` | latest | AGPL-3.0-only | Optional local telemetry analysis under `develop/grafana/` | ❌ | ❌ | https://github.com/grafana/grafana |
| `alpine:3.20` | 3.20 | mixed (MIT musl, GPL-2.0-only BusyBox, …) | One-shot permission helper in the `develop/grafana/` compose stack | ❌ | ❌ | https://hub.docker.com/_/alpine |

`influxdb:2.7` and `influxdb:2.9` are InfluxDB 2.x OSS, which is MIT-licensed.
InfluxDB 3.x is licensed differently; this project does not use it.

## Appliance Package Dependencies

The Debian packages `ems-appliance-manager` declares in its `Depends:` field.
The `.deb` does not ship them — apt installs them from Debian — but the
appliance image does: it is a Debian Trixie root filesystem with this set
installed.

As with the container base images, this inventory documents the declared set
and not the thousands of packages a Debian root pulls in. Each build writes the
image's complete package manifest into the image itself and its digest into the
build marker, so the exact set of any published image is recoverable from the
artefact rather than from this table.

The generator that assembles that root filesystem, `rpi-image-gen`, is listed
under Vendored Components above.

| Component | Version | License (SPDX) | Used for | Runtime | Distributed | Upstream |
|---|---|---|---|:---:|:---:|---|
| `python3` | `>=3.9` (Trixie ships 3.13) | PSF-2.0 | The manager, the agent and every appliance CLI | ✅ | ✅ | https://github.com/python/cpython |
| `systemd` | Trixie | LGPL-2.1-or-later AND mixed | Unit lifecycle, the socket boundary, slot-shared mount generators | ✅ | ✅ | https://github.com/systemd/systemd |
| `adduser` | Trixie | GPL-2.0-or-later | Creates the unprivileged web account and the backup account | ❌ | ✅ | https://salsa.debian.org/debian/adduser |
| `acl` | Trixie | LGPL-2.1-or-later AND GPL-2.0-or-later | Named-user grants on the export root | ✅ | ✅ | https://git.savannah.nongnu.org/cgit/acl.git |
| `iproute2` | Trixie | GPL-2.0-or-later | Address and link state for the network page | ✅ | ✅ | https://git.kernel.org/pub/scm/network/iproute2/iproute2.git |
| `procps` | Trixie | GPL-2.0-or-later AND LGPL-2.0-or-later | Process and memory facts for diagnostics | ✅ | ✅ | https://gitlab.com/procps-ng/procps |
| `util-linux` | Trixie | mixed (GPL-2.0-or-later, LGPL-2.1-or-later, BSD-3-Clause) | Block device and partition inspection for the A/B layout | ✅ | ✅ | https://github.com/util-linux/util-linux |
| `mount` | Trixie | GPL-2.0-or-later | Slot and shared-path mount state | ✅ | ✅ | https://github.com/util-linux/util-linux |
| `ca-certificates` | Trixie | GPL-2.0-or-later AND MPL-2.0 (certificate data) | TLS trust for release fetches and container pulls | ✅ | ✅ | https://salsa.debian.org/debian/ca-certificates |
| `passwd` | Trixie | BSD-3-Clause AND GPL-2.0-or-later | Account and shell management for the confined backup account | ❌ | ✅ | https://github.com/shadow-maint/shadow |
| `gpgv` | Trixie | GPL-3.0-or-later | Verifies the detached signature over each release manifest | ✅ | ✅ | https://github.com/gpg/gnupg |
| `openssh-client` | Trixie | BSD-3-Clause AND mixed | Host key material and the forced SFTP command policy | ✅ | ✅ | https://github.com/openssh/openssh-portable |
| `cloud-guest-utils` | Trixie | GPL-3.0-or-later | `growpart` grows the persistent partition on first boot | ✅ | ✅ | https://github.com/canonical/cloud-utils |
| `e2fsprogs` | Trixie | GPL-2.0-only AND LGPL-2.0-only (libext2fs) | `resize2fs` and `dumpe2fs` for the persistent filesystem | ✅ | ✅ | https://github.com/tytso/e2fsprogs |

## Optional Platform Dependencies

Packages that `package-lock.json` marks optional and platform-specific. npm
installs only the one for the machine it runs on. All of them belong to the
Modern dashboard's build tools and stay on the build machine.

| Component | Version | License (SPDX) | Used for | Runtime | Distributed | Upstream |
|---|---|---|---|:---:|:---:|---|
| `@rolldown/binding-android-arm-eabi` | 1.2.14 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-android-arm64` | 1.2.14 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-darwin-arm64` | 1.2.14 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-darwin-x64` | 1.2.14 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-freebsd-x64` | 1.2.14 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-linux-arm-gnueabihf` | 1.2.14 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-linux-arm64-gnu` | 1.2.14 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-linux-arm64-musl` | 1.2.14 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-linux-ppc64-gnu` | 1.2.14 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-linux-s390x-gnu` | 1.2.14 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-linux-x64-gnu` | 1.2.14 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-linux-x64-musl` | 1.2.14 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-openharmony-arm64` | 1.2.14 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-win32-arm64-msvc` | 1.2.14 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-win32-x64-msvc` | 1.2.14 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `fsevents` | 2.3.3 | MIT | macOS file watcher, used by Vite's development server | ❌ | ❌ | https://github.com/fsevents/fsevents |
| `lightningcss-android-arm64` | 1.33.0 | MPL-2.0 | Native CSS minifier for this platform, used by Vite at build time; never modified or shipped | ❌ | ❌ | https://github.com/parcel-bundler/lightningcss |
| `lightningcss-darwin-arm64` | 1.33.0 | MPL-2.0 | Native CSS minifier for this platform, used by Vite at build time; never modified or shipped | ❌ | ❌ | https://github.com/parcel-bundler/lightningcss |
| `lightningcss-darwin-x64` | 1.33.0 | MPL-2.0 | Native CSS minifier for this platform, used by Vite at build time; never modified or shipped | ❌ | ❌ | https://github.com/parcel-bundler/lightningcss |
| `lightningcss-freebsd-x64` | 1.33.0 | MPL-2.0 | Native CSS minifier for this platform, used by Vite at build time; never modified or shipped | ❌ | ❌ | https://github.com/parcel-bundler/lightningcss |
| `lightningcss-linux-arm-gnueabihf` | 1.33.0 | MPL-2.0 | Native CSS minifier for this platform, used by Vite at build time; never modified or shipped | ❌ | ❌ | https://github.com/parcel-bundler/lightningcss |
| `lightningcss-linux-arm64-gnu` | 1.33.0 | MPL-2.0 | Native CSS minifier for this platform, used by Vite at build time; never modified or shipped | ❌ | ❌ | https://github.com/parcel-bundler/lightningcss |
| `lightningcss-linux-arm64-musl` | 1.33.0 | MPL-2.0 | Native CSS minifier for this platform, used by Vite at build time; never modified or shipped | ❌ | ❌ | https://github.com/parcel-bundler/lightningcss |
| `lightningcss-linux-x64-gnu` | 1.33.0 | MPL-2.0 | Native CSS minifier for this platform, used by Vite at build time; never modified or shipped | ❌ | ❌ | https://github.com/parcel-bundler/lightningcss |
| `lightningcss-linux-x64-musl` | 1.33.0 | MPL-2.0 | Native CSS minifier for this platform, used by Vite at build time; never modified or shipped | ❌ | ❌ | https://github.com/parcel-bundler/lightningcss |
| `lightningcss-win32-arm64-msvc` | 1.33.0 | MPL-2.0 | Native CSS minifier for this platform, used by Vite at build time; never modified or shipped | ❌ | ❌ | https://github.com/parcel-bundler/lightningcss |
| `lightningcss-win32-x64-msvc` | 1.33.0 | MPL-2.0 | Native CSS minifier for this platform, used by Vite at build time; never modified or shipped | ❌ | ❌ | https://github.com/parcel-bundler/lightningcss |

At the Python level, `typing-extensions` is the equivalent case: a conditional
`cryptography` dependency for `python_full_version < 3.11`, never installed on
the Python 3.14 images. It is listed with the runtime transitive packages above.

## Generated Assets

Artifacts this project generates rather than authors by hand. They contain
project content; the third-party part is the tool that produced them, which is
already inventoried above.

| Artifact | Produced by | Third-party content | Distributed |
|---|---|---|:---:|
| `config/config.template.json` | `tools/build_config_template.py` from `ems/config_catalog.py` | none | ✅ |
| `/app/release-resources` inside the Admin image | `scripts/generate_release_resources.py` at image build | none | ✅ |
| `docs/assets/screenshots/**` | `scripts/capture_admin_docs.py`, `scripts/capture_dashboard_docs.py` (headless Firefox + ImageMagick) | none; chart pixels are drawn by `uPlot` from project data | ❌ |
| `docs/assets/videos/**` | `scripts/render_admin_docs_video.py` (headless Firefox + ffmpeg) | none | ❌ |
| Install demo animation | `docs/demo/build_install_demo.py` (Pillow + host DejaVu fonts) | rendered glyphs from the host font; no output is committed | ❌ |

The screenshots and videos show only this project's own user interface with
anonymized fixture data. No third-party artwork, icon set or font file is
embedded in them.

## License Notes

- **Project license.** The project itself is AGPL-3.0-or-later. Nothing in this
  inventory is incompatible with it: Apache-2.0, MIT, BSD, MPL-2.0, EPL-2.0 and
  LGPL components may be combined with AGPL-3.0-or-later software.
- **`zeroconf` (LGPL-2.1-or-later).** Used unmodified through its public Python
  API and installed as a separate site-packages distribution inside the images,
  so the LGPL's relinking obligation is satisfied by the ordinary pip layout. Do
  not fork or patch it in-tree without revisiting this.
- **`paho-mqtt` (EPL-2.0 OR BSD-3-Clause).** Dual-licensed; this project uses it
  unmodified and relies on no specific arm of the choice.
- **`certifi` (MPL-2.0).** File-level copyleft on a CA bundle used unmodified.
- **`Pillow` is not declared in any manifest.** Only
  `docs/demo/build_install_demo.py` imports it, and no CI job installs it. The
  script is optional developer tooling and its output is not committed. Install
  it manually (`pip install Pillow`) to regenerate the demo. Adding it to
  `requirements-dev.txt` would install it on every CI job, so it is deliberately
  left out.
- **`grafana/grafana-oss` (AGPL-3.0-only).** Run as a separate unmodified
  container for local analysis; it is never linked into or shipped with this
  project.
- **Container image contents.** Base images carry hundreds of upstream packages
  under their own licenses. This inventory names the image and its upstream, as
  agreed scope; it does not enumerate the package sets inside them.
- **Undetermined licenses.** None. Every component above was resolved from
  upstream package metadata, an upstream `LICENSE` file or the upstream
  repository's declared license.

## Packaging

Source distributions and release packages include:

- [`LICENSE`](LICENSE) — the project's AGPL-3.0-or-later text
- `THIRD_PARTY_LICENSES.md` — this inventory
- [`dashboard/static/uPlot.LICENSE`](dashboard/static/uPlot.LICENSE) — the
  vendored uPlot MIT notice

`Dockerfile` copies `LICENSE`, `THIRD_PARTY_LICENSES.md` and the whole
`dashboard/` tree into the EMS image, so all three travel with every published
image.

## Maintaining This Inventory

Run the checker after touching any manifest, workflow, vendored asset or container image:

```bash
python tools/check_third_party_licenses.py
```

It fails when a direct dependency, an optional lockfile package, a GitHub Action a
workflow uses or a vendored static asset is missing, when a documented entry no longer exists in any
manifest, when a component is listed twice in one section, or when a table is
missing a required column. `tests/test_third_party_licenses.py` runs the same
checks under `pytest -m documentation`.

See [`docs/developer/development.md`](docs/developer/development.md) for the
full update workflow.
