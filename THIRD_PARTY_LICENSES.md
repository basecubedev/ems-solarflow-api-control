# Third-Party Licenses

This project source code is licensed under the GNU Affero General Public
License v3.0 or later. Third-party dependencies and assets remain under their
original upstream licenses.

## Dashboard Icons And Frontend Assets

Current status:

- Active icon library dependency: none
- Active dashboard runtime package dependencies: none
- Admin end-to-end test tooling: npm-based Playwright development dependencies
- Dashboard icons: project-local inline SVG symbols in `dashboard/static/`
- Dashboard frontend runtime: plain browser HTML, CSS, SVG, and JavaScript

The current SolarFlow dashboard does not import `@phosphor-icons/react`,
`@tabler/icons-react`, `lucide-react`, or another third-party icon package.
The inline SVG dashboard icons are part of this project and are covered by the
project AGPL-3.0-or-later license. They are not copied from a third-party icon
library.

No visible in-dashboard attribution is required for the current icon set.

## Vendored Frontend Chart Library

The dashboard history/analytics charts use **uPlot** (v1.6.31), a small,
canvas-based charting library. It is vendored verbatim under
`dashboard/static/` (no dashboard package manager or build step):

- `dashboard/static/uPlot.iife.min.js`
- `dashboard/static/uPlot.min.css`

uPlot is licensed under the **MIT License**, Copyright (c) Leon Sorokin
(https://github.com/leeoniya/uPlot). The MIT license text must be preserved
when redistributing these files.

## Python Runtime Dependencies

Direct runtime dependencies keep their upstream licenses:

- `requests`: Apache-2.0
- `cryptography`: Apache-2.0 OR BSD-3-Clause
- `paho-mqtt`: EPL-2.0 OR BSD-3-Clause
- `zeroconf`: LGPL-2.1-or-later

## Development And Test Dependencies

Python development and test dependencies:

- `pytest`: MIT
- `ruff`: MIT
- `PyYAML`: MIT

Node development and test dependencies:

- `@playwright/test`: Apache-2.0
- `playwright`: Apache-2.0
- `playwright-core`: Apache-2.0

`@playwright/test` is the direct npm development dependency declared in
`package.json`; `playwright` and `playwright-core` are resolved through
`package-lock.json`. They run the Admin end-to-end tests and are not dashboard
runtime dependencies.

## Packaging

Source distributions and release packages should include:

- `LICENSE`
- `THIRD_PARTY_LICENSES.md`
- `NOTICE` or `NOTICE.md`, if one is added later

The repository contains `package.json` and `package-lock.json`. They declare the
development and test tooling for the Admin Playwright end-to-end tests; they do
not introduce a dashboard runtime build step, and the dashboard is still served
as plain browser HTML, CSS, SVG, and JavaScript.

## Future Dashboard Dependencies

When adding new icon, font, image, chart, UI asset, or frontend package
dependencies, update this file and preserve the upstream copyright and license
notice.

Do not list unused icon libraries as active dependencies. If an icon library is
added later, include the full upstream license text in this file or in a clearly
referenced license file shipped with source and release packages.
