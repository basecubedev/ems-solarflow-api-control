# Third-Party Licenses

This project source code is licensed under the Apache License 2.0. Third-party
dependencies and assets remain under their original upstream licenses.

## Dashboard Icons And Frontend Assets

Current status:

- Active icon library dependency: none
- Active dashboard package manager dependencies: none
- Dashboard icons: project-local inline SVG symbols in `dashboard/static/`
- Dashboard frontend runtime: plain browser HTML, CSS, SVG, and JavaScript

The current SolarFlow dashboard does not import `@phosphor-icons/react`,
`@tabler/icons-react`, `lucide-react`, or another third-party icon package.
The inline SVG dashboard icons are part of this project and are covered by the
project Apache-2.0 license. They are not copied from a third-party icon
library.

No visible in-dashboard attribution is required for the current icon set.

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
