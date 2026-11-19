#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Carry the bundled InfluxDB pin into every file that repeats it.

``deploy/docker/compose.influxdb.yml`` pins the image (``ems/influx_image.py``).
Dependabot moves that pin; the installers, the root compose file, the
developer capture stack and the licence inventory repeat it and are rewritten
here, and CI reads it::

    python3 tools/bundled_influxdb.py --check   # exit 1 and name what differs
    python3 tools/bundled_influxdb.py --sync    # rewrite the copies
    python3 tools/bundled_influxdb.py --print   # the pinned image, for CI

``Repo dependency refresh`` runs ``--sync`` on a Dependabot branch, and
``tests/test_bundled_influxdb_pin.py`` fails while a copy differs. Standard
library only, so the refresh workflow can run it with ``python3 -I``.
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ems.influx_image import PIN_FILE, compose_image, pinned_image, replace_image  # noqa: E402

COMPOSE_COPIES = (
    "docker-compose.yml",
    "install-docker.sh",
    "install-docker.ps1",
    "develop/influxdb/docker-compose.yml",
)
INVENTORY = "THIRD_PARTY_LICENSES.md"
_INVENTORY_ROW = re.compile(
    r"^\| `(?P<image>influxdb:[^`]+)` \| (?P<version>[^|]+?) \| (?P<rest>MIT \| Optional bundled Analytics database.*)$",
    re.MULTILINE,
)


def _inventory_text(text, image):
    matches = list(_INVENTORY_ROW.finditer(text))
    if len(matches) != 1:
        raise ValueError(f"{INVENTORY}: expected one bundled InfluxDB row, found {len(matches)}")
    match = matches[0]
    version = image.split(":", 1)[1]
    row = f"| `{image}` | {version} | {match['rest']}"
    return text[: match.start()] + row + text[match.end():]


def expected_texts(root=ROOT):
    """``{relative path: (current text, text with the pin)}`` for every copy."""

    root = Path(root)
    image = pinned_image(root)
    texts = {}
    for relative in COMPOSE_COPIES:
        current = (root / relative).read_text(encoding="utf-8")
        texts[relative] = (current, replace_image(current, image))
    current = (root / INVENTORY).read_text(encoding="utf-8")
    texts[INVENTORY] = (current, _inventory_text(current, image))
    return image, texts


def differing(root=ROOT):
    """The pin and the copies that do not repeat it."""

    image, texts = expected_texts(root)
    return image, sorted(path for path, (current, wanted) in texts.items() if current != wanted)


def sync(root=ROOT):
    """Rewrite every copy that differs; returns the pin and the rewritten paths."""

    image, texts = expected_texts(root)
    changed = []
    for relative, (current, wanted) in sorted(texts.items()):
        if current != wanted:
            (Path(root) / relative).write_text(wanted, encoding="utf-8")
            changed.append(relative)
    return image, changed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="fail when a copy differs from the pin")
    mode.add_argument("--sync", action="store_true", help="rewrite the copies to the pin")
    mode.add_argument("--print", action="store_true", help="print the pinned image")
    args = parser.parse_args(argv)
    try:
        if args.print:
            print(pinned_image(ROOT))
            return 0
        if args.sync:
            image, changed = sync()
            for relative in changed:
                print(f"{relative}: now {image}")
            return 0
        image, stale = differing()
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for relative in stale:
        found = compose_image((ROOT / relative).read_text(encoding="utf-8")) or "another image"
        print(f"{relative} names {found}, {PIN_FILE} pins {image}", file=sys.stderr)
    if stale:
        print("Run: python3 tools/bundled_influxdb.py --sync", file=sys.stderr)
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
