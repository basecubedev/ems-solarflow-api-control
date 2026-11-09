# Third-Party Licenses

This project's own source code is licensed under the GNU Affero General Public
License v3.0 or later (see [`LICENSE`](LICENSE)). Every third-party component
listed here keeps its original upstream license.

This file is the authoritative human-readable third-party inventory for the
repository. `tools/check_third_party_licenses.py` and
`tests/test_third_party_licenses.py` verify it against the actual manifests, so
a new dependency fails CI until it is documented here.

Per file, the repository follows the [REUSE specification](https://reuse.software/):
every source, script and configuration file the project writes names its
copyright holder and license in its first lines, and [`REUSE.toml`](REUSE.toml)
declares the rest: files that cannot carry a comment, fixtures and release
evidence whose bytes are the point, and every third-party file below with its
upstream holder. The license texts are in
[`LICENSES/`](LICENSES/), and `reuse lint` checks that nothing is left out.

## How To Read This Inventory

| Column | Meaning |
|---|---|
| Component | The package, image or asset name exactly as the manifest declares it |
| Version | What the manifest declares: a requirement's constraint, an npm package's specifier and locked version, the major version a workflow uses an action at. Where no manifest decides it, the version this inventory was verified against |
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
| `requests` | `>=2.34.2` in requirements.txt, `>=2.34.2,<3` in deploy/admin/requirements.txt | Apache-2.0 | HTTP client for Zendure local/cloud APIs, Shelly / EcoTracker / Tasmota grid meters and InfluxDB | ✅ | ✅ | https://github.com/psf/requests |
| `paho-mqtt` | `>=2.2.0` | EPL-2.0 OR BSD-3-Clause | MQTT client for local brokers and Zendure cloud MQTT telemetry and control | ✅ | ✅ | https://github.com/eclipse-paho/paho.mqtt.python |
| `cryptography` | unpinned (verified 49.0.0) | Apache-2.0 OR BSD-3-Clause | AEAD backup archives, Admin secret/credential store, self-signed HTTPS certificates | ✅ | ✅ | https://github.com/pyca/cryptography |
| `zeroconf` | `>=0.153.0` in requirements.txt, `>=0.153.0,<1` in deploy/admin/requirements.txt | LGPL-2.1-or-later | Optional live mDNS device discovery in the Admin Console; absence degrades gracefully | ✅ | ✅ | https://github.com/python-zeroconf/python-zeroconf |

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
Python packages and do not appear in any requirements file. None of the three
arrives with its license text: `influx` is copied out of an image build stage,
the Docker CLI and the Compose plugin are downloaded as bare binaries. The texts
are therefore kept in `third_party/`, and the final stage of the Dockerfile
named in `Image` copies the directory to `/usr/share/doc/<name>/` beside the
binary. The checker reads the final stage of each Dockerfile and requires a row
for every file it places under `/usr`, `/opt`, `/bin`, `/sbin` or `/lib`
(except `/usr/share` and Python package directories) through `COPY --from` or
`ADD`, or in a `RUN` step through `install`, `cp`, `mv`, `curl -o`, `wget -O`,
`tar -x -C` or `unzip -d`, also behind `if`/`for`/`while`, in `case` branches
and in `sh -c`. A binary a `RUN` step compiles, writes from a heredoc or places
with another command is not seen and needs its row by hand.

| Component | Version | License (SPDX) | Used for | Runtime | Distributed | Upstream | Image | Installed as | Notices |
|---|---|---|---|:---:|:---:|---|---|---|---|
| `influx` | 2.8.0, commit `8cdf401` (from the `influxdb:2.9` build stage) | MIT | InfluxDB backup/restore from inside the EMS container without a Docker socket | ✅ | ✅ | https://github.com/influxdata/influx-cli | `Dockerfile` | `/usr/local/bin/influx` | `third_party/influx-cli/LICENSE`, `third_party/influx-cli/THIRD_PARTY_NOTICES` |
| `docker/cli` | 27.5.1 | Apache-2.0 AND MIT | Admin Console controls the host Docker engine through the mounted socket; client only, never a daemon. The static build links musl (MIT) in | ✅ | ✅ | https://github.com/docker/cli | `deploy/admin/Dockerfile` | `/usr/local/bin/docker` | `third_party/docker-cli/LICENSE`, `third_party/docker-cli/NOTICE`, `third_party/docker-cli/THIRD_PARTY_NOTICES`, `third_party/docker-cli/MUSL.COPYRIGHT` |
| `docker/compose` | 2.32.4 | Apache-2.0 | Compose plugin for the Admin Console's container lifecycle operations | ✅ | ✅ | https://github.com/docker/compose | `deploy/admin/Dockerfile` | `/usr/local/lib/docker/cli-plugins/docker-compose` | `third_party/docker-compose/LICENSE`, `third_party/docker-compose/NOTICE`, `third_party/docker-compose/THIRD_PARTY_NOTICES` |

Provenance, verified on 2026-11-07: each `LICENSE` and `NOTICE` is
byte-identical to the file of the same name at the upstream revision in the
Version column. `MUSL.COPYRIGHT` is musl's `COPYRIGHT` at v1.2.5, the musl of
the Alpine 3.20 toolchain (GCC 13.2.1_git20240309, as the binary's `.comment`
names it) that Docker's static CLI is built and statically linked with.

| File | SHA-256 |
|---|---|
| `third_party/influx-cli/LICENSE` | `4dca58790e62edef3456a30fd971e32df1b890ac5341524813c02adbbbb1289a` |
| `third_party/docker-cli/LICENSE` | `2d81ea060825006fc8f3fe28aa5dc0ffeb80faf325b612c955229157b8c10dc0` |
| `third_party/docker-cli/NOTICE` | `a8c869fbda819afb8d80e0ac19bac52e766bc6c19cb38cf94f52d64c4be2aab6` |
| `third_party/docker-cli/MUSL.COPYRIGHT` | `f9bc4423732350eb0b3f7ed7e91d530298476f8fec0c6c427a1c04ade22655af` |
| `third_party/docker-compose/LICENSE` | `58d1e17ffe5109a7ae296caafcadfdbe6a7d176f0bc4ab01e12a689b0499d8bd` |
| `third_party/docker-compose/NOTICE` | `b7dca0a6b01fa7365e4892877a6321179ee343d72ee87a96cfc222141b99a1e6` |

The binaries are statically linked Go programs, and the modules compiled into
them travel with them too. `THIRD_PARTY_NOTICES` in each directory is generated
by `scripts/binary_notices.py generate` from the upstream source of the pinned
version: the module list of its `go.mod` (`vendor/modules.txt` for the Docker
CLI, which is built vendored), every module's license files from the Go module
proxy or the vendor tree, those of its packages as well as its root's (each
named by its package path; `testdata`, `vendor` and directories Go ignores
left out), and the Go standard library's license, each distinct text once
with the modules that use it; the list may include modules only
the upstream's tests use, which costs a few notices and omits none. The
arrangement is the project's and is given away under CC0-1.0; every text in it
stays under the terms it states. Both image builds run
`scripts/binary_notices.py check` on the binary they ship and fail when it was
built from another revision than its notices name, or holds a module they do
not list. The Docker CLI is built vendored and records no modules, so for it
the check compares the version alone. A moved `influxdb:2.9` tag or a version
bump therefore stops the build with the command that regenerates the file.

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
| `ruff` | `==0.17.4` | MIT | Lint and static checks (`ruff check .`) | ❌ | ❌ | https://github.com/astral-sh/ruff |
| `PyYAML` | `>=6.0.3` | MIT | Parsing CI workflow and compose YAML inside contract tests | ❌ | ❌ | https://github.com/yaml/pyyaml |
| `reuse` | `==6.3.0` | GPL-3.0-or-later AND Apache-2.0 AND CC0-1.0 AND CC-BY-SA-4.0 | Checks that every file names its copyright holder and license (`reuse lint`, [REUSE](https://reuse.software/)) | ❌ | ❌ | https://github.com/fsfe/reuse-tool |
| `zensical` | `==0.1.0` | MIT | Builds the documentation website from `docs/` ([docs-site.md](docs/developer/docs-site.md)) | ❌ | ❌ | https://github.com/zensical/zensical |

### Node Development Dependencies

Declared in [`package.json`](package.json); `playwright` and `playwright-core`
are resolved through [`package-lock.json`](package-lock.json). The private
`ems-admin-e2e` package carries the Playwright end-to-end tests and the builds
of the three Modern front ends, the dashboard (`npm run build:dashboard-modern`),
the Admin console (`npm run build:admin-modern`) and the Appliance Manager
(`npm run build:appliance-modern`), all made from the design system in
`frontend/shared/`. The builds run on a developer or CI machine, never in an
image or a package build; their output is committed under
`dashboard/static/modern/`, `admin/static/modern/` and
`appliance/static/modern/`, and what of it reaches a browser is listed under
Vendored Components below. Svelte, Vite and the Vite plugin are build tools;
Inter and Lucide are copied into the output.

| Component | Version | License (SPDX) | Used for | Runtime | Distributed | Upstream |
|---|---|---|---|:---:|:---:|---|
| `@playwright/test` | `^1.64.1` (locked 1.64.1) | Apache-2.0 | Admin Console end-to-end test runner | ❌ | ❌ | https://github.com/microsoft/playwright |
| `@fontsource-variable/inter` | `5.3.0` | OFL-1.1 | Source of the Inter variable font copied into the Modern builds | ❌ | ❌ | https://github.com/fontsource/font-files |
| `@sveltejs/vite-plugin-svelte` | `7.3.1` | MIT | Compiles the Modern front ends' Svelte components during the Vite build | ❌ | ❌ | https://github.com/sveltejs/vite-plugin-svelte |
| `lucide-static` | `1.69.0` | ISC | Source of the icon paths copied into the Modern sprites | ❌ | ❌ | https://github.com/lucide-icons/lucide |
| `svelte` | `5.57.3` | MIT | Component compiler of the Modern front ends; its runtime is bundled (Vendored Components) | ❌ | ❌ | https://github.com/sveltejs/svelte |
| `vite` | `8.3.6` | MIT | Builds the Modern front ends into `dashboard/static/modern/`, `admin/static/modern/` and `appliance/static/modern/` | ❌ | ❌ | https://github.com/vitejs/vite |
| `playwright` | 1.64.1 | Apache-2.0 | Browser automation library behind the test runner | ❌ | ❌ | https://github.com/microsoft/playwright |
| `playwright-core` | 1.64.1 | Apache-2.0 | Browser protocol driver used by `playwright` | ❌ | ❌ | https://github.com/microsoft/playwright |

### Browser Runtimes

Downloaded on demand by `npx playwright install` into the Playwright browser
cache, or provided by the developer's operating system. They are never
committed to this repository and never shipped in an image.

| Component | Version | License (SPDX) | Used for | Runtime | Distributed | Upstream |
|---|---|---|---|:---:|:---:|---|
| `Chromium` | Playwright build for 1.64.1 | BSD-3-Clause AND mixed | `chromium` Playwright project | ❌ | ❌ | https://www.chromium.org/Home |
| `Firefox` | Playwright build for 1.64.1, plus the host Firefox | MPL-2.0 | `firefox` Playwright project, and headless screenshot/video capture for the documentation | ❌ | ❌ | https://hg.mozilla.org/mozilla-central |
| `WebKit` | Playwright build for 1.64.1 | LGPL-2.1-or-later AND BSD-2-Clause | Optional `webkit` Playwright project | ❌ | ❌ | https://github.com/WebKit/WebKit |

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
| `svelte` | 5.57.3 (runtime) | MIT | Component runtime bundled into the Modern dashboard, Admin console and Appliance Manager | ✅ | ✅ | https://github.com/sveltejs/svelte | `dashboard/static/modern/assets/modern.js`, `admin/static/modern/assets/vendor.js`, `appliance/static/modern/assets/appliance.js`, notices in `dashboard/static/modern/licenses.json`, `admin/static/modern/licenses.json` and `appliance/static/modern/licenses.json` |
| `clsx` | 2.1.1 | MIT | Class-name helper the Svelte runtime bundles | ✅ | ✅ | https://github.com/lukeed/clsx | `dashboard/static/modern/assets/modern.js`, `admin/static/modern/assets/vendor.js`, `appliance/static/modern/assets/appliance.js`, notices in `dashboard/static/modern/licenses.json`, `admin/static/modern/licenses.json` and `appliance/static/modern/licenses.json` |
| `vite` (module-preload helper) | 8.3.6 | MIT | The few lines Vite writes into the Admin console's entry chunk to preload the page chunks it imports | ✅ | ✅ | https://github.com/vitejs/vite | `admin/static/modern/assets/admin.js` (Copyright (c) 2019-present, VoidZero Inc. and Vite contributors), notice in `admin/static/modern/Vite.LICENSE` |
| `Inter` (`@fontsource-variable/inter`) | 4.1 via 5.3.0 | OFL-1.1 | The Modern front ends' typeface, latin subset | ✅ | ✅ | https://github.com/rsms/inter | `dashboard/static/modern/assets/inter-latin-wght-normal.woff2`, `dashboard/static/modern/assets/modern.css` (`@font-face`), `dashboard/static/modern/Inter.LICENSE`, `admin/static/modern/assets/inter-latin-wght-normal.woff2`, `admin/static/modern/assets/admin.css` (`@font-face`), `admin/static/modern/Inter.LICENSE`, `appliance/static/modern/assets/inter-latin-wght-normal.woff2`, `appliance/static/modern/assets/appliance.css` (`@font-face`), `appliance/static/modern/Inter.LICENSE` |
| `Lucide` (`lucide-static`) | 1.69.0 | ISC AND MIT (Feather-derived icons) | Icon paths of the Modern front ends' inline sprites | ✅ | ✅ | https://github.com/lucide-icons/lucide | `dashboard/static/modern/index.html`, `admin/static/modern/index.html` and `appliance/static/modern/index.html` (the `<svg class="icon-sprite">` block), `dashboard/static/modern/Lucide.LICENSE`, `admin/static/modern/Lucide.LICENSE`, `appliance/static/modern/Lucide.LICENSE` |
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

No other third-party source is vendored. The Modern dashboard, the Modern
Admin console and the Modern Appliance Manager are build outputs, not
hand-written source. The dashboard's bundle
(`dashboard/static/modern/assets/modern.js`) and every stylesheet combine the
project's own AGPL-3.0-or-later code (in `frontend/shared/`,
`frontend/dashboard-modern/`, `frontend/admin-modern/` and
`frontend/appliance-modern/`) with the Svelte runtime and `clsx`; the
Appliance Manager's bundle (`appliance/static/modern/assets/appliance.js`)
carries the Svelte runtime and `clsx`, as its `licenses.json` says. The Admin console loads its pages as separate chunks: the
Svelte runtime and `clsx` are all in `admin/static/modern/assets/vendor.js`
(`admin/static/modern/assets/admin.js` loads it), and every other chunk is the
project's own code and carries its licence header. The entry chunk `admin.js`
also holds Vite's module-preload helper (MIT, row above); `licenses.json`
does not list it, because Vite injects it rather than bundling a package. Vite writes the MIT notices of every
bundled package into a `licenses.json` that ships beside each bundle, and
`tests/test_dashboard_modern.py`, `tests/test_admin_modern.py` and
`tests/test_appliance_modern.py` fail when a
package in that file has no row above or a licence outside the permissive set. The Inter font file is
unmodified (OFL-1.1 permits bundling it with software), and the Lucide icon
paths are copied verbatim into the page's inline sprite (ISC; the icons Lucide
took over from Feather stay MIT, "Copyright (c) 2013-present Cole Bemis").
Neither is bundled code, so the build copies each package's own licence file
beside them: `Inter.LICENSE` carries the OFL text and `Lucide.LICENSE` both
notices, with the Feather list, in `dashboard/static/modern/`,
`admin/static/modern/` and `appliance/static/modern/`. Classic
(`dashboard/static/`, `admin/static/` and `appliance/static/` outside their
`modern/` folders) remains
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

What they carry from others is offered in source beside them. Every workflow
that pushes the two images (a release tag, `latest`, the weekly rebuild, a
development build) then runs `scripts/image_sources.py collect` against the
images as pushed, every platform of both, and pushes
`ghcr.io/basecubedev/ems-solarflow-sources` under the same tags and the build
id. It holds every Debian source package of the installed versions with the
sources they were built with (as the appliance image's archive collects them),
the sdist PyPI publishes of every Python distribution installed or lying in
the image as a wheel, checked against PyPI's SHA-256 (one without an sdist
stops the run unless the script names it with the reason none is owed), the
CPython release the interpreter was built from (its `readline` and `gdbm`
modules link GPL libraries), as the base image's `PYTHON_VERSION` and
`PYTHON_SHA256` name it and checked against that hash, and the zip of every
Go module the bundled notices list whose own license files at the module proxy
are copyleft, checked against sum.golang.org, with a manifest naming each
image, platform and digest and the URL every file came from. Every program in
an image has to be accounted for: owned by dpkg with the checksum the package
that lists it recorded (diversions followed), part of CPython's standard
library, one of a wheel's Python extension modules, or a Go program one of the
image's notices files passes `binary_notices.py check` for. A Go program that
links a C library in statically needs that library known and its notice beside
its own (musl's `MUSL.COPYRIGHT` for the Docker CLI); any other program or
library a wheel carries (a `*.libs` folder, a bundled executable) has to be
named in the script; an image that installs Docker's
Debian packages, whose module sources only the appliance path collects, stops
the run. `docker pull` it and copy `/sources/` out of a
container created from it. All three images are pushed by digest and tagged
only once the source is pushed and verified, the source first, so no public
tag names an image whose source is not beside it. An image of another
revision or naming no CPython build, a program nothing accounts for, a Python
distribution in a place the script does not read, a distribution without an
sdist, anything unverifiable, and a source image that cannot be pulled without
credentials fail the workflow before any tag moves.

| Component | Version | License (SPDX) | Used for | Runtime | Distributed | Upstream |
|---|---|---|---|:---:|:---:|---|
| `python:3.14-slim` | 3.14-slim | PSF-2.0 (CPython) AND mixed (Debian base) | Base layer of the EMS image and the Admin image | ✅ | ✅ | https://hub.docker.com/_/python |
| `influxdb:2.9` | 2.9 | MIT | EMS image build stage; only `/usr/local/bin/influx` is copied out of it | ❌ | ✅ | https://hub.docker.com/_/influxdb |
| `influxdb:2.7` | 2.7 | MIT | Optional bundled Analytics database in `docker-compose.yml` and `deploy/docker/compose.influxdb.yml` | ✅ | ❌ | https://hub.docker.com/_/influxdb |
| `eclipse-mosquitto:2` | 2 | EPL-2.0 OR BSD-3-Clause | Local development broker and the ephemeral broker for optional real-MQTT tests | ❌ | ❌ | https://hub.docker.com/_/eclipse-mosquitto |
| `eclipse-mosquitto:2.1.2-alpine` | 2.1.2-alpine | EPL-2.0 OR BSD-3-Clause (Mosquitto) AND mixed (Alpine base) | Optional built-in MQTT broker the Admin pulls and starts in the `ems-mqtt` project when an operator switches it on | ✅ | ❌ | https://hub.docker.com/_/eclipse-mosquitto |
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

Their source is published with the image. After the release gates pass, the
image workflow reads every installed package and its source package straight
out of the built image's dpkg database
(`scripts/appliance_image_sources.py list`). The publish job then fetches each
source package in exactly that version, verifies every file and packs them
beside the images into the same release, as
`ems-solarflow-appliance-<version>-sources-<n>-of-<N>.tar` with a
`-sources.json` manifest (`collect`). It reads the archive's current index,
then the `.dsc` in its pool, then snapshot.debian.org, and checks every file
against the index, the `.dsc` that lists it or snapshot's SHA-1; a `.dsc`
taken from a pool is trusted as the archive serves it over HTTPS. The sources a
package was built with (`Built-Using`, `Static-Built-Using`) are collected
like its own. Two kinds of package have nothing to fetch: Docker's, whose own
code is Apache-2.0 and whose repository publishes no source (the copyleft
modules and the GNU C Library compiled or linked into their programs are the
exceptions, see below), which the manifest names with their upstream, and the
Appliance Manager, whose source is this
repository at its release tag. A source that cannot be fetched and verified,
one without a `.dsc` and a tarball, and a package dpkg holds in a state that is
neither installed nor removed fail the publish job.

Docker's packages (`docker-ce`, `docker-ce-cli`, `containerd.io`, the Buildx
and Compose plugins) ship no notices for what their programs are built from:
four have no copyright file at all, and `containerd.io`'s names only
Apache-2.0. Their programs are Go builds with some four hundred modules
compiled in. Their notices go beside the image, not into it, as
`ems-solarflow-appliance-<version>-docker-notices.txt`.
`list` records what every ELF program of those packages says it is: its Go
build information, or for `docker-init` the banner of tini, which is statically
linked with the GNU C Library. The publish job then writes the file with
`scripts/binary_notices.py appliance`: every license file of each module (a
file whose name says license, copying, copyright, notice, patents or legal and
that is neither code nor data), its packages' as well as its root's, from the
Go module proxy (read with range requests, without downloading the module), a
program's own license from its module, or from the tagged GitHub tree of a
build without one (Moby's `dockerd`, the vendored Docker CLI, tini; the tree's
nested modules left to their own versions), the Go
standard library's license of each Go version, and glibc's `COPYING.LIB` and
`LICENSES` for tini, pinned by their SHA-256 for the Debian release Docker
built the package for. These fail the publish job: a tree that is not the
commit the program names, a main module whose commit at the module proxy is
not the build's `vcs.revision`, a build that reports modified sources
(`vcs.modified`, a `-dirty` banner), a Go experiment or a sanitizer, a module
without a license file at its root, a vendor tree that replaces a module with
a local directory, C code linked in statically as the program shows it (no
loader; static archives and flags in `-ldflags` or `CGO_LDFLAGS` with cgo on;
the libseccomp binding compiled in without `libseccomp.so` among the libraries
it loads), a glibc whose texts are not pinned, and a program no rule
describes. What the program does not record cannot be checked: `dockerd`,
`docker` and `docker-proxy` carry no `vcs` settings, a build with `-trimpath`
(runc is one) records no linker flags, and a static library named only in a
package's `#cgo` directives appears nowhere.

Two parts of Docker's programs carry a source duty. The first are the Go
modules with a copyleft license text, such as HashiCorp's MPL-2.0 modules, the
HCL parser Buildx carries, and the MPL-2.0 part of `filepath-securejoin`: the
publish job puts each one's zip beside the image as
`ems-solarflow-appliance-<version>-docker-module-sources.tar`, exactly as the Go
module proxy serves it and refused unless it hashes to what sum.golang.org
records, and the notices file lists them. A license text counts as copyleft
when it names the MPL, GPL, LGPL, AGPL, EPL, CDDL, EUPL, CPL, OSL or MS-RL, by
name or SPDX identifier; a copyleft text in a program's own tree stops the
run. The second is glibc, LGPL-2.1-or-later, linked statically
into `docker-init`. The source archive holds the glibc source of the image's
own `libc6`, a Raspberry Pi build of Debian's. Which Debian 13 glibc Docker's
build linked is not recorded in the binary, so the archive does not claim to
hold that exact source.

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
| `@rolldown/binding-android-arm-eabi` | 1.2.16 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-android-arm64` | 1.2.16 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-darwin-arm64` | 1.2.16 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-darwin-x64` | 1.2.16 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-freebsd-x64` | 1.2.16 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-linux-arm-gnueabihf` | 1.2.16 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-linux-arm64-gnu` | 1.2.16 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-linux-arm64-musl` | 1.2.16 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-linux-ppc64-gnu` | 1.2.16 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-linux-s390x-gnu` | 1.2.16 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-linux-x64-gnu` | 1.2.16 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-linux-x64-musl` | 1.2.16 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-openharmony-arm64` | 1.2.16 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-win32-arm64-msvc` | 1.2.16 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
| `@rolldown/binding-win32-x64-msvc` | 1.2.16 | MIT | Native Rolldown bundler for this platform, used by Vite at build time | ❌ | ❌ | https://github.com/rolldown/rolldown |
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
| `docs/assets/screenshots/**`, `docs/assets/preview-*.jpg` | `scripts/capture_admin_docs.mjs`, `tests/e2e-dashboard/capture-docs.spec.ts`, `tests/e2e-appliance/capture-docs.spec.ts` (all Playwright Chromium) | none beyond the pixels the pages render: Inter glyphs and Lucide icon shapes in the Modern pages (inventoried above), chart pixels drawn by `uPlot` from project data | ❌ |
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
image. `deploy/admin/Dockerfile` copies `LICENSE` and `THIRD_PARTY_LICENSES.md`
into the Admin image beside the `admin/` tree, which carries the Modern
console's notices. Both copy the `third_party/` notices of the binaries they
ship to `/usr/share/doc/`.

## Maintaining This Inventory

Run the checker after touching any manifest, workflow, vendored asset or container image:

```bash
python tools/check_third_party_licenses.py
```

It fails when a direct dependency, an optional lockfile package, a GitHub Action a
workflow uses or a vendored static asset is missing, when a binary the final
stage of a Dockerfile installs has no row, when a row's `third_party/` files are
missing, incomplete (an Apache-2.0 component needs its `NOTICE`, unless its `Notices` cell says
`no NOTICE upstream`) or not copied
by the final stage of the Dockerfile in its `Image` cell, when a documented entry no longer exists in any
manifest, when a component is listed twice in one section, or when a table is
missing a required column. `tests/test_third_party_licenses.py` runs the same
checks under `pytest -m documentation`.

It also fails when a Version cell disagrees with the manifest that decides it,
and `--fix-versions` rewrites exactly those cells before it checks. A Python
row may keep a `(verified …)` note behind its constraint until the constraint
changes. The license of a package `package-lock.json` locks is compared with the
lockfile's and is never rewritten: a changed license is a person's decision.
On a Dependabot pull request the **Repo dependency refresh** workflow runs the
rewrite and the Modern builds for you (see
[`docs/developer/ci-release.md`](docs/developer/ci-release.md#dependencies-and-security-scanning)).

See [`docs/developer/development.md`](docs/developer/development.md) for the
full update workflow.
