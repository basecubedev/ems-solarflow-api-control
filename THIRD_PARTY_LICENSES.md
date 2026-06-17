# Third-Party Licenses

This project source code is licensed under the GNU Affero General Public
License v3.0 or later. Third-party dependencies and assets remain under their
original upstream licenses.

## Dashboard Icons And Frontend Assets

Current status:

- Active icon library dependency: none
- Active dashboard package manager dependencies: none
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
`dashboard/static/` (no package manager / build step in this project):

- `dashboard/static/uPlot.iife.min.js`
- `dashboard/static/uPlot.min.css`

uPlot is licensed under the **MIT License**, Copyright (c) Leon Sorokin
(https://github.com/leeoniya/uPlot). The MIT license text must be preserved
when redistributing these files.

## Python Dependencies

Direct Python dependencies keep their upstream licenses:

- `requests`: Apache-2.0
- `cryptography`: Apache-2.0 OR BSD-3-Clause
- `pytest`: MIT

## Packaging

Source distributions and release packages should include:

- `LICENSE`
- `THIRD_PARTY_LICENSES.md`
- `NOTICE` or `NOTICE.md`, if one is added later

At the time of this note, the repository does not contain a dashboard package
manifest, frontend lockfile, Dockerfile, `.dockerignore`, Python packaging
manifest, or release script that excludes these files.

## Future Dashboard Dependencies

When adding new icon, font, image, chart, UI asset, or frontend package
dependencies, update this file and preserve the upstream copyright and license
notice.

Do not list unused icon libraries as active dependencies. If an icon library is
added later, include the full upstream license text in this file or in a clearly
referenced license file shipped with source and release packages.
