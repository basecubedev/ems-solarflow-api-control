# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The bundled InfluxDB image: where it is pinned and how two of its versions compare.

``deploy/docker/compose.influxdb.yml`` is the one place the image is pinned,
to an exact ``major.minor.patch`` tag so an installation runs what was tested
and moves only on purpose. Every other file that names the image is a copy of
that line (``tools/bundled_influxdb.py`` carries it over, a test holds them
equal), and the Admin reads it from the release resources it ships with.

A compose file is read as text, as the Admin and the installers write it: one
``image: influxdb:<tag>`` line. A file with none, with several, or with a tag
this module cannot read as a version (``latest``, ``2.7-alpine``, a digest, a
registry prefix) is one an operator manages by hand, and nothing here offers
to change it. Only InfluxDB 2.x is supported; 3.x is another product.
Import-side-effect-free.
"""

import re
from dataclasses import dataclass
from pathlib import Path

PIN_FILE = "deploy/docker/compose.influxdb.yml"
SUPPORTED_MAJOR = 2

_IMAGE_LINE = re.compile(
    r"^(?P<lead>[ \t]*image:[ \t]*)(?P<quote>[\"']?)(?P<image>influxdb[:@][^\s\"'#]+)(?P=quote)[ \t]*$",
    re.MULTILINE,
)
_TAG = re.compile(r"^influxdb:(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?$")


@dataclass(frozen=True)
class InfluxVersion:
    """A version read from an image tag; ``patch`` is None for a moving ``major.minor`` tag."""

    major: int
    minor: int
    patch: int | None

    def __str__(self):
        parts = [self.major, self.minor] + ([] if self.patch is None else [self.patch])
        return ".".join(str(part) for part in parts)


def parse_image(image):
    """The InfluxDB 2.x version an ``influxdb:<tag>`` reference names, or None."""

    match = _TAG.match(image.strip()) if isinstance(image, str) else None
    if match is None or int(match["major"]) != SUPPORTED_MAJOR:
        return None
    patch = match["patch"]
    return InfluxVersion(int(match["major"]), int(match["minor"]), None if patch is None else int(patch))


def compose_image(text):
    """The one InfluxDB image a compose text names, or None when it names none or several."""

    matches = _IMAGE_LINE.findall(text) if isinstance(text, str) else []
    return matches[0][2] if len(matches) == 1 else None


def replace_image(text, image):
    """``text`` with its one InfluxDB image line naming ``image`` instead.

    Raises ValueError when the text does not name exactly one InfluxDB image,
    so a file an operator arranged differently is never half rewritten.
    """

    if parse_image(image) is None:
        raise ValueError(f"{image!r} is not an InfluxDB {SUPPORTED_MAJOR}.x image")
    matches = list(_IMAGE_LINE.finditer(text))
    if len(matches) != 1:
        raise ValueError(f"expected one InfluxDB image line, found {len(matches)}")
    match = matches[0]
    line = f"{match['lead']}{match['quote']}{image}{match['quote']}"
    return text[: match.start()] + line + text[match.end():]


def is_upgrade(current, target):
    """Whether moving from image ``current`` to image ``target`` is an upgrade within 2.x.

    A moving ``2.7`` tag stands for every 2.7 release, so only a later minor
    is an upgrade from it. Anything unreadable, and any step down, is not.
    """

    have = parse_image(current)
    want = parse_image(target)
    if have is None or want is None:
        return False
    if have.patch is None or want.patch is None:
        return want.minor > have.minor
    return (want.minor, want.patch) > (have.minor, have.patch)


def pinned_image(root):
    """The image ``PIN_FILE`` under ``root`` pins; ValueError when it pins no readable 2.x image."""

    path = Path(root) / PIN_FILE
    image = compose_image(path.read_text(encoding="utf-8"))
    version = parse_image(image) if image else None
    if version is None or version.patch is None:
        raise ValueError(f"{path} must pin one exact InfluxDB {SUPPORTED_MAJOR}.x.y image")
    return image
